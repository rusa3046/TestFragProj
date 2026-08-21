"""The unattended daily loop: collect, extract, resolve, publish, report.

    python -m fragrance_graph.daily run
    python -m fragrance_graph.daily run --dry-run     # no key, no spend

    1. YouTube: search fragrance discussion, on seeds the catalogue
       chooses (`catalogue_seeds`) — the popular, note-carrying bottles
       the corpus cannot speak about yet
    2. ingest -> extract
    3. backfill: resolve every mention the curated dictionary already covers
    4. export -> pages -> report

## There used to be a fifth step, and it bought nothing

Between them, steps 3 and 4 once had a catalogue lookup: ask Fragella
about mentions people had newly started writing, and auto-curate the ones
where no judgement was required. It was a good idea and it did not work.

Measured 2026-08-12: **60 lookups, $3.00 charged, 5 names, 0 pages.** The
catalogue does not carry the small houses this corpus actually discusses,
so the mentions worth resolving were exactly the ones it could not answer.
Read against the ledger, it was $1.45 of the $2.78 this project has ever
spent -- more than half the money, for nothing that reached a reader.

It was removed on 2026-08-14. What replaced it is `resolve.entities
batch` / `apply`: an offline review file with two real comment spans and
the video titles behind each mention, which a person fills in with no
network and no spend. One sitting of that produced 4 bottles, 2 aliases,
49 resolved mentions and a page.

The reasoning the lookup path encoded is not lost -- SPEC records the
flanker rule and the negative result, and `docs/CURATION.md` records what
a curator is deciding. Only the code that could not pay for itself is
gone.

## Nothing here spends without the cap

Every paid step goes through `budget.Budget`, a hard $1/day stop backed by
a committed ledger. See `budget.py` for why the ledger is a file rather
than a table. With the catalogue gone, extraction is the only thing left
that costs money.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from fragrance_graph.budget import DAILY_CAP_USD, Budget, BudgetExhausted
from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate

log = logging.getLogger("fragrance_graph.daily")

#: How the phrase a search was made with shapes what comes back.
#:
#: Ordered longest-first where prefixes overlap, so "better than" is not
#: read as the bare word "than".
QUERY_SHAPES = (
    ("dupe/clone", ("dupe", "dupes", "clone", "clones")),
    ("smells like", ("smells like", "smell like")),
    ("better than", ("better than",)),
    ("alternative to", ("alternative to", "alternatives to", "instead of")),
    ("compared to", ("compared to", "comparison")),
    ("worth it", ("worth it", "worth the")),
    ("head to head", (" vs ", " vs. ", " versus ")),
    ("review", ("review", "honest thoughts")),
)


def query_shape(query: str) -> str:
    """Which kind of question a search phrase is asking.

    Reporting only. It exists because "how diverse are the seeds" was
    being answered by reading a list and counting in your head, which is
    exactly the kind of thing that quietly stops happening.
    """
    lowered = f" {query.lower()} "
    for name, needles in QUERY_SHAPES:
        if any(needle in lowered for needle in needles):
            return name
    return "bare name"


#: The searches the scheduled loop makes when nothing else is asked for.
#:
#: **Six of the eight original seeds contained "dupe".** That is a leading
#: question asked of an audience assembled to answer it, and the corpus
#: shows it: every published pair is a dupe claim or sits beside one, and
#: `MIN_QUERIES` cannot be raised to 2 because four of the six pairs that
#: published in August rested on a single query.
#:
#: A dupe search finds people agreeing that A imitates B. It does not find
#: the person who says B is worth the money, or the one who says A smells
#: like C instead — claims the taxonomy already models and the corpus
#: barely contains. The fix is not a better prompt; it is asking a
#: different question.
#:
#: So the shapes are mixed deliberately, and the dupe shape is now a
#: minority of the list rather than three quarters of it. The bottles are
#: the ones the corpus already has evidence about, because a broader
#: question about a bottle nobody discusses returns nothing either way.
SEED_QUERIES = (
    "creed aventus vs",
    "parfums de marly layton vs",
    "lattafa khamrah vs",
    "is parfums de marly layton worth it",
    "smells like baccarat rouge 540",
    "better than dior sauvage",
    "alternative to tom ford oud wood",
    "compared to creed aventus",
    "fragrance dupe",
    "aventus clone",
)

#: Comments to pull per scheduled run. The cap is the real limit; this
#: stops a single run from queueing far more extraction than a day's budget
#: can pay for, which would otherwise just be re-read as pending each run.
DEFAULT_INGEST_LIMIT = 400

#: How many catalogue-derived seeds a run asks for by default. Matches the
#: length of `SEED_QUERIES` so the quota cost of a run is unchanged: this
#: replaces the fixed list rather than adding to it.
DEFAULT_CATALOGUE_SEEDS = 10

#: Question shapes applied to a catalogue bottle, cycled in this order so
#: a run's seeds are not ten of the same question.
#:
#: "review" leads because it is the shape most likely to *exist* for a
#: bottle nobody has compared yet — the failure mode of a comparison
#: search against an undiscussed bottle is zero results, and a seed that
#: returns nothing costs a quota unit and teaches nothing. The dupe shape
#: is deliberately absent: `SEED_QUERIES`' docstring records why the
#: corpus is already saturated with it, and nothing about seeding from the
#: catalogue changes that argument.
CATALOGUE_SHAPES = (
    "{name} review",
    "{name} honest thoughts",
    "is {name} worth it",
    "smells like {name}",
    "{name} vs",
)


def shape_mix(queries) -> Counter:
    """How many queries of each shape, most common first."""
    return Counter(query_shape(q) for q in queries)


#: SQL behind `catalogue_seeds`. Kept beside it rather than inline so the
#: four conditions can be read as the argument they are.
_CATALOGUE_SEED_SQL = """
WITH pool AS (
    SELECT f.id,
           f.canonical_name,
           MAX(rl.review_count) AS retail_reviews,
           ROW_NUMBER() OVER (
               PARTITION BY lower(f.brand)
               ORDER BY MAX(rl.review_count) DESC, f.canonical_name
           ) AS rank_in_brand
      FROM fragrances f
      JOIN retailer_listings rl ON rl.fragrance_id = f.id
     WHERE rl.review_count IS NOT NULL
       AND EXISTS (SELECT 1 FROM fragrance_note_claim n
                    WHERE n.fragrance_id = f.id)
       AND NOT EXISTS (SELECT 1 FROM claims c
                        WHERE c.subject_frag_id = f.id
                           OR c.object_frag_id = f.id)
       -- strpos, not a LIKE with wildcard concatenation: psycopg scans
       -- the whole statement for placeholders -- comments included, which
       -- is how an explanatory note here caused the very error it was
       -- describing -- so a literal percent sign anywhere in a
       -- parameterised statement is rejected outright. See
       -- catalogue_seeds' own comment for the full story.
       AND NOT EXISTS (SELECT 1 FROM video_discoveries d
                        WHERE strpos(lower(d.retrieval_query),
                                     lower(f.canonical_name)) > 0)
     GROUP BY f.id, f.canonical_name, f.brand
)
SELECT canonical_name, retail_reviews
  FROM pool
 WHERE rank_in_brand = 1
 ORDER BY retail_reviews DESC, canonical_name
 LIMIT %s
"""


def catalogue_seeds(conn, limit: int = DEFAULT_CATALOGUE_SEEDS) -> list[str]:
    """Searches aimed at catalogued bottles the corpus cannot speak about.

    `SEED_QUERIES` names ten bottles chosen when the catalogue held 56.
    The catalogue now holds 548, and the fixed list has no way to learn
    that: every run pours more evidence onto Aventus and Layton while 419
    bottles stay unrecommendable for want of a single claim. This asks the
    catalogue what is missing instead.

    Four conditions, each doing a specific job:

    - **A retailer listing with a review count.** The count is a proxy
      for "would YouTube bother", and it is the condition that makes this
      work at all. `SEED_QUERIES`' docstring already records the trap —
      *a broader question about a bottle nobody discusses returns nothing
      either way* — and seeding straight from the catalogue would walk
      into it, since most of a 548-bottle retail catalogue is obscure. A
      bottle with 15,227 Nordstrom reviews is not obscure. Retail
      popularity is the only signal here that predicts YouTube coverage
      without already having YouTube coverage.
    - **Declared notes.** A bottle whose notes we hold can be recommended
      the moment it has any perceptual evidence, so collecting for it
      converts directly. One with neither notes nor comments needs two
      things and is a worse buy for the same quota unit.
    - **No community claims.** The point. A bottle with evidence is not
      what this is for, however popular it is.
    - **Never searched before.** Rotation, and honest about its own
      limits: it matches the bottle's name against past
      `retrieval_query` rows, so a run that searched a bottle and found
      nothing does not search it again next Thursday. The corpus grows
      into the catalogue instead of circling.

    One bottle per brand, because three La Vie est Belle flankers would
    otherwise take three of ten slots and return overlapping videos —
    measured on this catalogue, the unpartitioned top ten held three of
    that family and two MYSLF variants.

    Deterministic: same database, same seeds. The tie-break on name
    exists so a run is reproducible rather than dependent on how Postgres
    felt about equal review counts.

    Returns query strings, not names — `_collect` searches text, and the
    shape a bottle is asked about matters as much as which bottle it is
    (see `CATALOGUE_SHAPES`).
    """
    try:
        rows = list(conn.execute(_CATALOGUE_SEED_SQL, (limit,)))
    except psycopg.errors.UndefinedTable:
        # Only this one. A database without the retail tables — a fresh
        # clone that ran `corpus import` and not `retail import` — is an
        # ordinary state, and the caller falls back to SEED_QUERIES.
        #
        # Deliberately not `except Exception`: the first version caught
        # everything and turned a real bug (a literal % in a
        # parameterised statement, see the SQL) into a warning and a
        # silent fallback to the very list this function exists to
        # replace. The loop would have gone on collecting Aventus
        # comments forever, reporting success. A broken query is not an
        # ordinary state and must be loud.
        conn.rollback()
        log.warning("no retail tables; falling back to SEED_QUERIES")
        return []
    seeds = []
    for index, row in enumerate(rows):
        name = row["canonical_name"] if hasattr(row, "keys") else row[0]
        seeds.append(CATALOGUE_SHAPES[index % len(CATALOGUE_SHAPES)].format(name=name))
    return seeds


def resolve_queries(
    conn, explicit: list[str] | None, source: str = "catalogue"
) -> list[str]:
    """What this run will actually search for.

    Precedence is explicit > catalogue > fixed, and the fallback is the
    part that matters: `catalogue_seeds` returns `[]` on a database with
    no retail tables *and* on one where every qualifying bottle has
    already been searched. Both are ordinary states, not failures — the
    first is a fresh clone, the second is the loop having done its job —
    and neither should turn a scheduled run into a no-op. It falls back to
    `SEED_QUERIES`, which still collects something useful.
    """
    if explicit:
        return list(explicit)
    if source == "fixed":
        return list(SEED_QUERIES)
    seeds = catalogue_seeds(conn)
    if seeds:
        return seeds
    log.info(
        "no catalogue seeds available (no retail data, or every qualifying "
        "bottle already searched); using SEED_QUERIES"
    )
    return list(SEED_QUERIES)


def render_seed_diversity(conn) -> str:
    """The seeds beside the searches the corpus was actually built from.

    Two columns, because the seeds are a plan and the corpus is what
    happened. Changing the plan does nothing until an ingest runs, and a
    report that showed only the new list would read as though the corpus
    had already broadened.
    """
    corpus = [
        row["retrieval_query"]
        for row in conn.execute(
            "SELECT DISTINCT retrieval_query FROM video_discoveries"
            " WHERE retrieval_query IS NOT NULL AND retrieval_query != ''"
        )
    ]
    # The seeds a run would actually use, not the fixed constant. Reading
    # `SEED_QUERIES` here would have reported the plan the loop stopped
    # following the day seeding moved to the catalogue — and this report
    # exists precisely so "how diverse are the seeds" is answered by the
    # code rather than from memory.
    upcoming = resolve_queries(conn, None)
    seeds, built = shape_mix(upcoming), shape_mix(corpus)
    shapes = sorted(set(seeds) | set(built))

    lines = [
        f"{'shape':<16} {'seeds now':>9} {'corpus so far':>14}",
    ]
    for shape in shapes:
        lines.append(f"{shape:<16} {seeds.get(shape, 0):>9} {built.get(shape, 0):>14}")
    lines.append(f"{'total':<16} {sum(seeds.values()):>9} {sum(built.values()):>14}")

    dupe_seeds = seeds.get("dupe/clone", 0)
    dupe_built = built.get("dupe/clone", 0)
    lines.append("")
    lines.append(
        f"Dupe-shaped: {_share(dupe_seeds, sum(seeds.values()))} of the seeds, "
        f"{_share(dupe_built, sum(built.values()))} of the searches behind the "
        "corpus."
    )
    if not corpus:
        lines.append(
            "No retrieval records yet — the corpus column fills in as "
            "provenance-recording ingest runs."
        )
    return "\n".join(lines)


def _share(part: int, whole: int) -> str:
    return "0%" if not whole else f"{round(100 * part / whole)}%"


@dataclass
class RunReport:
    """What one run did, in the terms the operator asked to hear about."""

    dry_run: bool = False
    videos_searched: int = 0
    comments_ingested: int = 0
    comments_extracted: int = 0
    claims_written: int = 0
    spend_usd: float = 0.0
    budget_remaining_usd: float = DAILY_CAP_USD
    #: The cap this run actually ran under. `render` used to print the
    #: module default, so a run started with --cap 1.50 reported being
    #: stopped by a $1.00 cap that was not in force.
    cap_usd: float = DAILY_CAP_USD
    stopped_on_budget: bool = False
    mentions_resolved: int = 0
    pages_before: int = 0
    pages_after: int = 0
    #: The corpus after this run. Deltas answer "did the loop do
    #: anything"; totals answer "how big is this thing now", which is the
    #: question someone reading a notification on a phone is actually
    #: asking. Both, because neither is the other.
    total_comments: int = 0
    total_claims: int = 0
    total_fragrances: int = 0
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The summary. Written to be read on a phone, worst news first."""
        lines: list[str] = []
        if self.dry_run:
            lines.append("DRY RUN — nothing was ingested, extracted or spent.")
        if self.errors:
            lines.append("Problems:")
            lines += [f"  ! {e}" for e in self.errors]
        if self.stopped_on_budget:
            lines.append(
                f"  ! Stopped on the ${self.cap_usd:.2f} daily cap. "
                "Un-extracted comments resume tomorrow."
            )

        lines.append("")
        lines.append(
            f"Collected   {self.comments_ingested} new comments "
            f"from {self.videos_searched} videos"
        )
        lines.append(
            f"Extracted   {self.comments_extracted} comments "
            f"-> {self.claims_written} claims"
        )
        lines.append(
            f"Spent       ${self.spend_usd:.4f} "
            f"(${self.budget_remaining_usd:.4f} left today)"
        )

        lines.append("")
        lines.append(f"Resolved    {self.mentions_resolved} mentions into claims")
        if self.total_comments or self.total_claims or self.total_fragrances:
            lines.append(
                f"Corpus      {self.total_comments:,} comments, "
                f"{self.total_claims:,} claims, "
                f"{self.total_fragrances} fragrances"
            )
        delta = self.pages_after - self.pages_before
        lines.append(
            f"Pages       {self.pages_after}"
            + (f"  ({delta:+d} today)" if delta else "  (no change)")
        )
        return "\n".join(lines)


def run(
    conn: psycopg.Connection,
    *,
    queries: list[str],
    budget: Budget,
    ingest_limit: int = DEFAULT_INGEST_LIMIT,
    max_videos: int = 3,
    out_dir: Path = Path("site"),
    dry_run: bool = False,
) -> RunReport:
    """One pass of the loop. Every paid step is guarded by `budget`."""
    from fragrance_graph.pages import build, qualifying_pairs

    report = RunReport(dry_run=dry_run, cap_usd=budget.cap_usd)
    report.pages_before = len(qualifying_pairs(conn))
    report.budget_remaining_usd = budget.remaining_usd

    if budget.exhausted and not dry_run:
        report.stopped_on_budget = True
        report.errors.append(
            f"Today's ${budget.cap_usd:.2f} was already spent before this run."
        )
        # Nothing ran, so nothing changed. Without this the default 0
        # renders as "Pages 0 (-6 today)" — a run that did nothing at all
        # reporting that it destroyed every published page. On the loop
        # whose whole contract is an honest summary, a false alarm is the
        # most expensive kind of wrong.
        report.pages_after = report.pages_before
        _snapshot(conn, report)
        return report

    from fragrance_graph.resolve.entities import backfill

    if not dry_run:
        _collect(conn, queries, max_videos, ingest_limit, report)
        _extract(conn, budget, ingest_limit, report)

        # Resolve with the dictionary we already have *before* paying the
        # catalogue for names it may already cover. Fresh comments mention
        # curated bottles constantly, and `newly_frequent` reads unresolved
        # mentions — so running this afterwards meant billing $0.05 each to
        # be told about "Khamrah" and "club de nuit", both already curated.
        # Backfill is free and idempotent; the lookup is neither.
        stats = backfill(conn)
        report.mentions_resolved = stats.subjects_resolved + stats.objects_resolved

    # Read after curation, not before: catalogue lookups are billed too, and
    # reporting the total before the last paid step understated it by
    # exactly the amount the operator most wanted to see.
    report.spend_usd = budget.spent_usd
    report.budget_remaining_usd = budget.remaining_usd

    if not dry_run:
        # Again, to apply anything auto-curation just wrote.
        stats = backfill(conn)
        report.mentions_resolved += stats.subjects_resolved + stats.objects_resolved

    report.pages_after = len(qualifying_pairs(conn))
    if not dry_run:
        build(conn, out_dir)
    _snapshot(conn, report)
    return report


def _collect(conn, queries, max_videos, ingest_limit, report: RunReport) -> None:
    from fragrance_graph.ingest.store import ingest
    from fragrance_graph.ingest.youtube import (
        SOURCE,
        QuotaTracker,
        build_client,
        fetch_video_metadata,
        iter_video_comments,
        record_discovery,
        search_video_ids,
        store_video_metadata,
        videos_missing_titles,
    )

    try:
        client, api_key = build_client()
    except SystemExit as exc:
        report.errors.append(f"YouTube unavailable: {exc}")
        return

    quota = QuotaTracker()
    run_id = datetime.now(UTC).strftime("run-%Y%m%dT%H%M%SZ")
    seen_videos: list[str] = []
    for query in queries:
        try:
            found = search_video_ids(
                client, api_key, query, limit=max_videos, quota=quota
            )
        except Exception as exc:  # quota, transport, disabled API
            report.errors.append(f"search {query!r} failed: {exc}")
            continue
        seen_videos += found
        # Recorded before any comment is pulled, because this is the only
        # moment it exists: the same search next week ranks differently.
        # Query diversity across an edge is computed from these rows.
        record_discovery(conn, found, query, run=run_id)

    seen_videos = list(dict.fromkeys(seen_videos))
    report.videos_searched = len(seen_videos)

    # Budgeted in **new** comments, not comments seen.
    #
    # Decrementing by rows fetched made an already-ingested video cost the
    # same as a fresh one. Measured 2026-08-11: a run over six new queries
    # found 18 videos, spent the whole 400 re-reading the first three —
    # "200 seen, 0 new, 200 already stored" — and stopped before reaching
    # any of the new creators. It collected 29 comments and published
    # nothing.
    #
    # That is the opposite of what the limit is for. It exists to cap how
    # much extraction one run can queue, and a comment already stored
    # queues none. Re-reading is nearly free (1 YouTube unit per 100
    # comments against a 10,000/day allowance) while *not* reaching a new
    # creator is the expensive outcome: the publishing gate needs two
    # distinct sources, so new creators are the only thing that turns
    # existing pairs into pages.
    remaining = ingest_limit
    for video_id in seen_videos:
        if remaining <= 0:
            break
        try:
            rows = list(
                iter_video_comments(
                    client, api_key, video_id, limit=remaining, quota=quota
                )
            )
        except Exception as exc:
            report.errors.append(f"comments for {video_id} failed: {exc}")
            continue
        # `ingest` still defaults to source="reddit" for historical reasons.
        # Passing SOURCE explicitly is not optional: uniqueness is
        # (source, source_id), so the wrong label would file YouTube
        # comments under a source that cannot be re-fetched.
        stats = ingest(conn, rows, source=SOURCE)
        report.comments_ingested += stats.new
        remaining -= stats.new

    # One `videos.list` call covers fifty videos for a single quota unit,
    # and titles are what a later resolver needs to tell one house's
    # Perseus from another's. Cheap enough to do every run; failure here
    # must never cost the comments already stored.
    try:
        missing = videos_missing_titles(conn)
        if missing:
            store_video_metadata(
                conn, fetch_video_metadata(client, api_key, missing, quota=quota)
            )
    except Exception as exc:
        report.errors.append(f"video metadata fetch failed: {exc}")


def _extract(conn, budget: Budget, limit: int, report: RunReport) -> None:
    from fragrance_graph.extract.llm import build_client, extract

    try:
        client = build_client()
    except SystemExit as exc:
        report.errors.append(f"Anthropic unavailable: {exc}")
        return

    before = _claim_count(conn)
    try:
        cost = extract(conn, client, limit=limit, on_spend=budget.guard("extract"))
        report.comments_extracted = cost.comments
    except BudgetExhausted as exc:
        report.stopped_on_budget = True
        log.warning("%s", exc)
    except Exception as exc:
        report.errors.append(f"extraction failed: {exc}")
    report.claims_written = _claim_count(conn) - before


def _snapshot(conn, report: RunReport) -> None:
    """Record how big the corpus is now, for the run summary."""
    report.total_comments = conn.execute(
        "SELECT count(*) FROM comments"
    ).fetchone()[0]
    report.total_claims = _claim_count(conn)
    report.total_fragrances = conn.execute(
        "SELECT count(*) FROM fragrances"
    ).fetchone()[0]


def _claim_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM claims").fetchone()[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="One pass of the daily loop")
    r.add_argument(
        "--queries",
        nargs="+",
        default=None,
        help="YouTube searches. Default: derived from the catalogue by "
             "`catalogue_seeds` — the popular, note-carrying bottles the "
             "corpus cannot speak about yet. Pass this to override.",
    )
    r.add_argument(
        "--seed-source",
        choices=("catalogue", "fixed"),
        default="catalogue",
        help="Where default seeds come from. 'fixed' restores the old "
             "SEED_QUERIES list. Ignored when --queries is given.",
    )
    r.add_argument("--ingest-limit", type=int, default=DEFAULT_INGEST_LIMIT)
    r.add_argument("--max-videos", type=int, default=3)
    r.add_argument("--out", default="site", type=Path)
    r.add_argument("--cap", type=float, default=DAILY_CAP_USD)
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen. No API key needed, nothing spent.",
    )

    s = sub.add_parser("spend", help="Recent daily spend")
    s.add_argument("--days", type=int, default=7)

    sd = sub.add_parser(
        "seeds",
        help="The seed queries by shape, beside what the corpus was built from",
    )

    for p in (r, s, sd):
        p.add_argument("--db-url", default=DEFAULT_DB_URL)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Every other entrypoint does this; daily.py did not, so a local run
    # silently ignored .env and reported both keys missing while the file
    # sat right there. The loop is the entrypoint most likely to be run
    # by a scheduler that has no shell profile to inherit from.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if args.command == "spend":
        from fragrance_graph.budget import summary

        print(summary(days=args.days))
        return 0

    if args.command == "seeds":
        conn = get_connection(args.db_url)
        migrate(conn)
        try:
            print(render_seed_diversity(conn))
        finally:
            conn.close()
        return 0

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        report = run(
            conn,
            queries=resolve_queries(conn, args.queries, args.seed_source),
            budget=Budget.load(cap_usd=args.cap, require_ledger=True),
            ingest_limit=args.ingest_limit,
            max_videos=args.max_videos,
            out_dir=Path(args.out),
            dry_run=args.dry_run,
        )
        print(report.render())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
