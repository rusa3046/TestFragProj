"""The unattended daily loop: collect, extract, resolve, publish, report.

    python -m fragrance_graph.daily run --queries "layton clone" "khamrah dupe"
    python -m fragrance_graph.daily run --dry-run     # no key, no spend

The loop is **demand-driven**, which SPEC settled on 2026-08-10 after
rejecting a release feed:

    1. YouTube: search fragrance discussion broadly
    2. ingest -> extract
    3. resolve.entities report  ->  newly frequent, still unnamed
    4. Fragella: resolve exactly those names
    5. auto-curate what corroborates -> backfill -> export -> pages

A supply-driven loop asks a catalogue what is new and then goes looking for
discussion of it. That answers the wrong question: a bottle launched
yesterday has no YouTube comments yet, so a release feed delivers
fragrances that *cannot* produce an edge. The corpus is already the
detector — a new release climbs the unresolved-mention report exactly when
people start talking about it, which is the first moment it can become an
edge. Fragella is then asked only the question it is genuinely better at
than the corpus: "people started writing 'Qahwa' — what is that, and
whose?"

That ordering also makes the catalogue cheap. Lookups are spent on mentions
the corpus has already proved people are discussing, rather than on a
brand's whole back catalogue.

## Curation is automatic, and narrowly so

The operator asked for auto-approval on corroboration with a summary,
rather than a review queue. `AUTO_RULE` below is that corroboration test,
and it is deliberately the narrowest defensible one: the only rows it takes
are the rows where there is no judgement to make.

`docs/CURATION.md` already names the judgement — flankers. A house ships
Layton and Layton Exclusif, Khamrah and Khamrah Qahwa, and the catalogue
returns whichever scores best. `corpus_mentions` encodes exactly that, and
its `-1` case means *the proposed name adds no word to the mention* — it is
the plain bottle, and the flanker question does not arise. Those are auto-
approved. Every other value means a human is being asked something:

    corpus_mentions == -1   no distinguishing word  -> auto-approve
    corpus_mentions == 0    a flanker nobody wrote  -> hold: pick an alternative
    corpus_mentions >  0    a flanker people discuss -> hold: probably its own entry

`corpus_mentions` only asks what the *catalogue name* adds. The mirror had
to be added after the first live run merged "Club De Nuit EDP" into "Armaf
Club De Nuit": a mention more specific than the name it matched also has
nothing distinguishing, and merging it is the same error in reverse. So
`names_agree` requires neither side to add a word the other lacks.

A merge is permanent and invisible to the test suite; a hold costs one
lookup later and stays visible in `resolve.entities report`. That asymmetry
is why the rule refuses rather than guesses, and it is the same one
`FUZZY_THRESHOLD = 0.88` encodes.

**This will still be wrong sometimes.** A hand-curated entry called
"confident" turned out to name the wrong house — two houses ship a Perseus
— which is roughly a 6% error rate on carefully checked entries. Automatic
curation will do worse than that, not better. What stops a bad merge
reaching a reader is not this rule; it is the publishing gate in
`pages.py`, which requires 3 distinct commenters across 2 creators before a
pair becomes a page. This rule only has to be good enough that the gate is
not doing all the work alone.

## Nothing here spends without the cap

Every paid step goes through `budget.Budget`, a hard $1/day stop backed by
a committed ledger. See `budget.py` for why the ledger is a file rather
than a table.

This was aspirational until 2026-08-11: extraction was metered, catalogue
lookups were not. That was survivable only while the catalogue's free tier
capped itself at 20 requests a month. On pay-per-use the loop would make
`--lookup-limit` billable calls per run, unattended, with nothing written
to the ledger — the exact failure `budget.py` exists to prevent, in the
one step the cap never saw. Lookups now bill through the same ledger at
`enrich.LOOKUP_COST_USD`, charged *before* each request.

Note the cap is per **day** and lookups are priced per request, so a large
`--lookup-limit` needs `--cap` raised to match, or splitting across days.
"""

from __future__ import annotations

import argparse
import logging
import os
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from fragrance_graph.budget import DAILY_CAP_USD, Budget, BudgetExhausted
from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.resolve.enrich import Proposal, names_agree

log = logging.getLogger("fragrance_graph.daily")

#: A mention must appear at least this often before a lookup is spent on
#: it. Below it, the mention cannot reach a 3-commenter bar even if every
#: mention were a different person, so the lookup buys nothing.
AUTO_MIN_COUNT = 3

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


def shape_mix(queries) -> Counter:
    """How many queries of each shape, most common first."""
    return Counter(query_shape(q) for q in queries)


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
    seeds, built = shape_mix(SEED_QUERIES), shape_mix(corpus)
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


def auto_approvable(proposal: Proposal, *, min_count: int = AUTO_MIN_COUNT) -> bool:
    """Whether a proposal corroborates well enough to write without a human.

    Read the conditions as "is there a decision here?" rather than "is this
    probably right?". The catalogue is usually right about the world; what
    it cannot know is which sibling bottle *these* commenters meant.
    """
    if proposal.approved is not None:
        return False  # already decided by a person; never overrule
    if not proposal.canonical_name or not proposal.brand:
        return False  # a name the catalogue never returned cannot be written
    if not proposal.confident:
        return False  # the proposed name is not the mention people wrote
    if proposal.corpus_mentions != -1:
        return False  # a flanker question exists, so a person answers it
    if not names_agree(
        proposal.mention, proposal.canonical_name, proposal.brand or ""
    ):
        # The mention is more specific than the name it matched — the
        # flanker question in reverse. `corpus_mentions == -1` cannot see
        # this, because it only asks what the *candidate* adds.
        return False
    return proposal.count >= min_count


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
    looked_up: int = 0
    auto_approved: list[str] = field(default_factory=list)
    held_for_review: list[tuple[str, str]] = field(default_factory=list)
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
        verb = "Would look up" if self.dry_run else "Looked up  "
        lines.append(f"{verb} {self.looked_up} unresolved mentions")

        if self.auto_approved:
            lines.append("")
            lines.append(f"Curated automatically ({len(self.auto_approved)}):")
            lines += [f"  + {name}" for name in self.auto_approved]
        if self.held_for_review:
            lines.append("")
            lines.append(
                f"Held for you ({len(self.held_for_review)}) — a flanker "
                "question the corpus cannot answer:"
            )
            lines += [f"  ? {m}: {why}" for m, why in self.held_for_review]

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


def newly_frequent(conn: psycopg.Connection, *, limit: int, min_count: int):
    """The detector: unresolved mentions people have started writing.

    Thin wrapper over `enrich.candidates` so the loop reads in the order
    SPEC describes it, and so the "what is new" step has a name.
    """
    from fragrance_graph.resolve.enrich import candidates

    return candidates(conn, limit, min_count=min_count)


def run(
    conn: psycopg.Connection,
    *,
    queries: list[str],
    budget: Budget,
    ingest_limit: int = DEFAULT_INGEST_LIMIT,
    lookup_limit: int = 25,
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

    _curate(conn, budget, lookup_limit, report, dry_run=dry_run)

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


def _curate(conn, budget: Budget, lookup_limit: int, report: RunReport, *,
            dry_run: bool) -> None:
    """Look up newly frequent names, write the ones with no decision in them."""
    from fragrance_graph.resolve.enrich import (
        ApplyStats,
        apply_review,
        propose,
        read_review,
    )

    wanted = newly_frequent(conn, limit=lookup_limit, min_count=AUTO_MIN_COUNT)
    report.looked_up = len(wanted)
    if dry_run or not wanted:
        return
    if not os.environ.get("FRAGELLA_API_KEY"):
        report.errors.append("FRAGELLA_API_KEY is not set; no names were looked up.")
        return

    review = Path("data/curation/auto-review.json")
    try:
        proposals = propose(
            conn, review, limit=lookup_limit, min_count=AUTO_MIN_COUNT,
            on_spend=budget.guard("catalogue"),
        )
    except BudgetExhausted as exc:
        # Not an error: the cap did its job. Rows bought before the stop
        # were still written, so they are curated below.
        report.stopped_on_budget = True
        log.warning("%s", exc)
        proposals = read_review(review) if review.exists() else []
    except SystemExit as exc:
        # Quota exhausted, or a rejected key. `propose` writes what it
        # gathered in a `finally`, so the lookups already paid for are on
        # disk — returning here threw them away. Measured: a run bought 59
        # names, hit 429 on the 60th, and applied none of them, for $3.
        report.errors.append(f"catalogue lookup failed: {exc}")
        proposals = read_review(review) if review.exists() else []
        if not proposals:
            return
    except Exception as exc:
        # Transport, DNS, TLS, an egress policy denying the host. `propose`
        # raises SystemExit only for the failures it anticipated (quota, a
        # rejected key); everything else arrives as an ordinary exception
        # and used to kill the whole run from here.
        #
        # That was the wrong shape for this loop. Curation is the last
        # paid step, and backfill, export and pages that follow it need no
        # catalogue at all — so an unreachable catalogue was discarding
        # work already done and, worse, taking the report with it. An
        # unattended loop that dies silently is the failure this module's
        # whole design is trying to avoid, so this degrades like the other
        # two steps: record it, finish the run, say so at the top.
        report.errors.append(f"catalogue unreachable: {exc!r}")
        return

    for proposal in proposals:
        if auto_approvable(proposal):
            proposal.approved = True
            report.auto_approved.append(
                f"{proposal.mention} -> {proposal.canonical_name} "
                f"[{proposal.brand}]"
            )
        else:
            report.held_for_review.append(
                (proposal.mention, proposal.note or "no catalogue match")
            )

    stats: ApplyStats = apply_review(conn, proposals)
    log.info("Auto-curation: %s", stats)


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
        default=list(SEED_QUERIES),
        help="YouTube searches. See SEED_QUERIES for the shapes and why.",
    )
    r.add_argument("--ingest-limit", type=int, default=DEFAULT_INGEST_LIMIT)
    r.add_argument("--lookup-limit", type=int, default=25)
    r.add_argument("--max-videos", type=int, default=3)
    r.add_argument("--out", default="site", type=Path)
    r.add_argument("--cap", type=float, default=DAILY_CAP_USD)
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would happen. No API key needed, nothing spent.",
    )

    cur = sub.add_parser(
        "curate",
        help="Apply an existing review file — no lookups, no spend",
    )
    cur.add_argument(
        "review", type=Path, nargs="?",
        default=Path("data/curation/auto-review.json"),
        help="A review file written by an earlier run. "
             "Default: data/curation/auto-review.json",
    )
    cur.add_argument("--out", default="site", type=Path)

    s = sub.add_parser("spend", help="Recent daily spend")
    s.add_argument("--days", type=int, default=7)

    sd = sub.add_parser(
        "seeds",
        help="The seed queries by shape, beside what the corpus was built from",
    )

    for p in (r, s, cur, sd):
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

    if args.command == "curate":
        # The lookups are already bought and on disk. A run that stopped on
        # quota used to discard them, and even with that fixed, re-running
        # `run` would query the catalogue again and bill again. This applies
        # what is already paid for and spends nothing.
        from fragrance_graph.pages import build, qualifying_pairs
        from fragrance_graph.resolve.enrich import apply_review, read_review
        from fragrance_graph.resolve.entities import backfill

        conn = get_connection(args.db_url)
        migrate(conn)
        try:
            proposals = read_review(args.review)
            report = RunReport()
            report.pages_before = len(qualifying_pairs(conn))
            for proposal in proposals:
                if auto_approvable(proposal):
                    proposal.approved = True
                    report.auto_approved.append(
                        f"{proposal.mention} -> {proposal.canonical_name} "
                        f"[{proposal.brand}]"
                    )
                else:
                    report.held_for_review.append(
                        (proposal.mention, proposal.note or "no catalogue match")
                    )
            stats = apply_review(conn, proposals)
            conn.commit()
            log.info("Curation from %s: %s", args.review, stats)
            back = backfill(conn)
            report.mentions_resolved = back.subjects_resolved + back.objects_resolved
            report.pages_after = len(qualifying_pairs(conn))
            build(conn, args.out)
            print(report.render())
        finally:
            conn.close()
        return 0

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
            queries=args.queries,
            budget=Budget.load(cap_usd=args.cap, require_ledger=True),
            ingest_limit=args.ingest_limit,
            lookup_limit=args.lookup_limit,
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
