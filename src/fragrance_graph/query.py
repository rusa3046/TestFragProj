"""Answering the question the project exists for.

    similar_to(conn, fragrance_id) -> "what else smells like this, and who said so"

Everything here reads claims that people wrote. Nothing is computed from
note lists, embeddings, or distance. A result is a count of humans plus the
sentences they typed, and the ranking is only how that evidence is sorted.

Three decisions worth stating, because each is a place a plausible
implementation would be wrong:

**Ranking counts distinct commenters, never claim rows.** One person saying
"BR540 dupe" in four threads is one person. Row counts would render a loud
minority as consensus, which is precisely the failure the evidence-first
pitch is sold against. Comments with no recorded author fall back to their
comment id, so unknown authors count as separate people rather than
collapsing into one — that under-counts a single prolific anonymous poster,
and over-counting consensus is the worse error of the two.

**DUPE_OF and SIMILAR_TO are symmetric; BETTER_THAN is not.** If someone
says A is a dupe of B, a reader arriving at B wants to see A. That is the
same fact read from the other end. "A is better than B" is not: it is a
ranking, and flattening its direction would turn a fragrance's critics into
its recommendations. Direction is preserved on every result regardless, so
a page can phrase the claim the way it was actually made.

**Denials are never edges.** "Delina and Baccarat smells nothing alike"
names two fragrances and a comparison type, and asserts the edge does not
exist. Counting it would put a real person's name behind the opposite of
what they wrote — the single worst thing an evidence-first product can do.
Measured at 7.2% of similarity claims before `polarity` existed.

**Two counts, because one is misleading.** Results are grouped by (other
fragrance, claim type), so the same person appears in two rows when they
call something both a dupe and similar. Rendering "3 people said dupe, 2
said similar" invites a reader to add them. `pair_commenters` is the
distinct-people count across every claim type for the pair — the number a
page should lead with.

**Sources are counted separately from people.** Three commenters in one
video's comment section, possibly replying to each other, is not three
independent observations, and "3 people said this" implies that it is.
`sources` records how many distinct videos back a claim so a page can
decline to imply consensus that a single thread cannot support.

**Evidence is required.** Only claims whose `evidence_span` was verified
against the comment body are counted. An unverified span is a paraphrase we
cannot show a reader as something a person wrote, and a result whose
evidence cannot be displayed is not a result this product is willing to
rank.

Not here, deliberately: any notion of price, retailer, stock, or commercial
relationship. Ranking runs on a table that has no column for it — see
`test_ranking_ignores_commerce`.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate

log = logging.getLogger("fragrance_graph.query")

#: Claim types that mean "these two smell alike", read from either end.
SYMMETRIC_TYPES = ("DUPE_OF", "SIMILAR_TO")

#: Claim types that assert an ordering, and so only read one way.
DIRECTIONAL_TYPES = ("BETTER_THAN",)

RELATED_TYPES = (*SYMMETRIC_TYPES, *DIRECTIONAL_TYPES)

#: Quotes shown per result. Three is enough to show a claim is not one
#: person repeating themselves, and few enough that a page stays readable.
DEFAULT_QUOTES = 3


@dataclass
class Evidence:
    """One thing a person actually wrote, and where to read it."""

    quote: str
    permalink: str
    comment_id: int
    author_id: str
    #: The video (or subreddit) it was written under. Two quotes from one
    #: thread are weaker evidence than two from unrelated places.
    source_ref: str
    sentiment: str
    #: True when this claim names the queried fragrance as its subject.
    #: "X is a dupe of Y" reads differently depending which one you asked
    #: about, and a page that gets it backwards is quoting people wrongly.
    outbound: bool


@dataclass
class Related:
    """One fragrance the community connected to the one you asked about."""

    fragrance_id: int
    canonical_name: str
    brand: str | None
    claim_type: str
    #: Distinct people, not claim rows. This is the ranking key.
    commenters: int
    #: Distinct people connecting this pair across *every* claim type. Rows
    #: are per claim type and a person can appear in several, so summing
    #: `commenters` over the rows for one pair over-counts humans.
    pair_commenters: int
    #: Distinct videos/threads backing this row.
    sources: int
    claims: int
    sentiment: str
    sentiment_counts: dict[str, int]
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def rank_key(self) -> tuple:
        """Descending commenters, then claims, then name for stability.

        Name last so two fragrances with identical support always come back
        in the same order — an unstable tail makes generated pages churn in
        git for no reason.
        """
        return (-self.commenters, -self.claims, self.canonical_name)


def aggregate_sentiment(counts: dict[str, int]) -> str:
    """Collapse a sentiment tally to one label.

    A plurality, with ties going to NEUTRAL. Deliberately not a mean of
    +1/0/-1: "nine people loved it, nine hated it" is a genuinely divided
    reception, and averaging it to NEUTRAL says the same word as "nobody
    felt strongly", which is a different fact about a bottle.
    """
    positive = counts.get("POSITIVE", 0)
    negative = counts.get("NEGATIVE", 0)
    neutral = counts.get("NEUTRAL", 0)
    if positive > negative and positive > neutral:
        return "POSITIVE"
    if negative > positive and negative > neutral:
        return "NEGATIVE"
    return "NEUTRAL"


def _sql_list(types: tuple[str, ...]) -> str:
    """Inline a claim-type tuple as SQL literals.

    Placeholders would be tidier, but sqlite3 refuses to mix `?` with the
    `:fragrance_id` this query already needs. These are module constants
    defined a few lines up, never caller input.
    """
    return ", ".join(f"'{t}'" for t in types)


#: Both directions of every edge touching the queried fragrance, before any
#: grouping. `outbound` records which way the claim was originally made.
#:
#: Filtered to verified evidence and to claims whose *other* end resolved to
#: a real bottle — an edge to an unresolved string is not yet an answer.
EDGES_SQL = f"""
SELECT c.id            AS claim_id,
       c.claim_type    AS claim_type,
       c.sentiment     AS sentiment,
       c.evidence_span AS quote,
       c.confidence    AS confidence,
       co.id           AS comment_id,
       co.permalink    AS permalink,
       co.author_id    AS author_id,
       coalesce(json_extract(co.raw_json, '$.videoId'), co.source_channel)
                       AS source_ref,
       1               AS outbound,
       f.id            AS other_id,
       f.canonical_name AS other_name,
       f.brand          AS other_brand
  FROM claims c
  JOIN comments   co ON co.id = c.comment_id
  JOIN fragrances f  ON f.id = c.object_frag_id
 WHERE c.subject_frag_id = :fragrance_id
   AND c.evidence_verified = 1
   AND c.polarity = 'ASSERTED'
   AND c.claim_type IN ({_sql_list(RELATED_TYPES)})
   AND f.id <> :fragrance_id

UNION ALL

SELECT c.id, c.claim_type, c.sentiment, c.evidence_span, c.confidence,
       co.id, co.permalink, co.author_id,
       coalesce(json_extract(co.raw_json, '$.videoId'), co.source_channel),
       0 AS outbound,
       f.id, f.canonical_name, f.brand
  FROM claims c
  JOIN comments   co ON co.id = c.comment_id
  JOIN fragrances f  ON f.id = c.subject_frag_id
 WHERE c.object_frag_id = :fragrance_id
   AND c.evidence_verified = 1
   AND c.polarity = 'ASSERTED'
   AND c.claim_type IN ({_sql_list(SYMMETRIC_TYPES)})
   AND f.id <> :fragrance_id
"""


def _commenter_key(row: sqlite3.Row) -> str:
    """Who wrote this, for distinct-person counting.

    An empty author falls back to the comment id, so unknown authors are
    counted as separate people instead of collapsing into one.
    """
    return row["author_id"] or f"comment:{row['comment_id']}"


def similar_to(
    conn: sqlite3.Connection,
    fragrance_id: int,
    *,
    limit: int = 20,
    quotes: int = DEFAULT_QUOTES,
    min_commenters: int = 1,
    min_sources: int = 1,
) -> list[Related]:
    """What the community says smells like (or beats) this fragrance.

    Ranked by how many distinct people said it. Each result carries up to
    `quotes` verbatim spans with permalinks, so a reader can check the
    claim against the comment rather than trusting the count.

    Grouped by (other fragrance, claim type): "a dupe of X" and "similar to
    X" are different strengths of claim and a buyer reads them differently,
    so they stay separate rows rather than being summed into one. Read
    `pair_commenters`, not the sum of `commenters`, for how many humans
    connected the two bottles — the rows share people.

    `min_sources` is the guard against a single comment section looking
    like consensus. It is a claim-quality filter, not a commercial one:
    nothing here can express "only fragrances we can sell".
    """
    rows = conn.execute(EDGES_SQL, {"fragrance_id": fragrance_id}).fetchall()

    grouped: dict[tuple[int, str], list[sqlite3.Row]] = {}
    # Distinct people per *pair*, ignoring claim type, so a page can state
    # how many humans connected two bottles without adding up rows that
    # share people.
    per_pair: dict[int, set[str]] = {}
    for row in rows:
        grouped.setdefault((row["other_id"], row["claim_type"]), []).append(row)
        per_pair.setdefault(row["other_id"], set()).add(_commenter_key(row))

    results = []
    for (other_id, claim_type), group in grouped.items():
        commenters = {_commenter_key(r) for r in group}
        if len(commenters) < min_commenters:
            continue
        sources = {r["source_ref"] for r in group if r["source_ref"]}
        if len(sources) < min_sources:
            continue
        counts = Counter(r["sentiment"] for r in group)
        results.append(
            Related(
                fragrance_id=other_id,
                canonical_name=group[0]["other_name"],
                brand=group[0]["other_brand"],
                claim_type=claim_type,
                commenters=len(commenters),
                pair_commenters=len(per_pair[other_id]),
                sources=len(sources),
                claims=len(group),
                sentiment=aggregate_sentiment(counts),
                sentiment_counts=dict(counts),
                evidence=_pick_evidence(group, quotes),
            )
        )

    results.sort(key=lambda r: r.rank_key)
    return results[:limit]


def _pick_evidence(rows: list[sqlite3.Row], quotes: int) -> list[Evidence]:
    """Up to `quotes` spans, each from a different person where possible.

    One commenter's three sentences look like three people agreeing to a
    reader skimming quotes. Distinct authors come first; only once they run
    out does a second quote from the same person get used.
    """
    ordered = sorted(rows, key=lambda r: (-r["confidence"], r["comment_id"]))
    seen: set[str] = set()
    picked, spares = [], []
    for row in ordered:
        key = _commenter_key(row)
        (picked if key not in seen else spares).append(row)
        seen.add(key)

    return [
        Evidence(
            quote=row["quote"],
            permalink=row["permalink"],
            comment_id=row["comment_id"],
            author_id=row["author_id"],
            source_ref=row["source_ref"] or "",
            sentiment=row["sentiment"],
            outbound=bool(row["outbound"]),
        )
        for row in (picked + spares)[:quotes]
    ]


SENTIMENT_SQL = """
SELECT c.claim_type AS claim_type, c.sentiment AS sentiment,
       count(*) AS claims,
       count(DISTINCT CASE WHEN co.author_id <> '' THEN co.author_id
                           ELSE 'comment:' || co.id END) AS commenters
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 WHERE c.subject_frag_id = :fragrance_id
   AND c.evidence_verified = 1
   AND c.polarity = 'ASSERTED'
 GROUP BY c.claim_type, c.sentiment
"""


@dataclass
class SentimentRollup:
    """How a fragrance is spoken about, per claim type and overall."""

    fragrance_id: int
    canonical_name: str
    overall: str
    counts: dict[str, int]
    commenters: int
    by_type: dict[str, dict[str, int]]

    def sentiment_for(self, claim_type: str) -> str:
        return aggregate_sentiment(self.by_type.get(claim_type, {}))


def sentiment_rollup(
    conn: sqlite3.Connection, fragrance_id: int
) -> SentimentRollup | None:
    """Roll every verified claim about a fragrance up to the bottle.

    Per claim type as well as overall, because the aggregate alone is
    misleading in the common case: a fragrance people love the smell of and
    complain about the longevity of nets out to NEUTRAL, which describes
    nothing anybody said.

    Only claims where this fragrance is the *subject*. "X is better than Y"
    is a statement about X; counting its sentiment towards Y would let a
    fragrance's rivals colour its own summary.
    """
    row = conn.execute(
        "SELECT id, canonical_name FROM fragrances WHERE id = ?", (fragrance_id,)
    ).fetchone()
    if row is None:
        return None

    by_type: dict[str, dict[str, int]] = {}
    overall: Counter = Counter()
    commenters = 0
    for r in conn.execute(SENTIMENT_SQL, {"fragrance_id": fragrance_id}):
        by_type.setdefault(r["claim_type"], {})[r["sentiment"]] = r["claims"]
        overall[r["sentiment"]] += r["claims"]
        commenters = max(commenters, r["commenters"])

    return SentimentRollup(
        fragrance_id=row["id"],
        canonical_name=row["canonical_name"],
        overall=aggregate_sentiment(overall),
        counts=dict(overall),
        commenters=commenters,
        by_type=by_type,
    )


def find_fragrance(conn: sqlite3.Connection, needle: str) -> sqlite3.Row | None:
    """Look up a fragrance by id, exact name, or curated alias."""
    if needle.isdigit():
        row = conn.execute(
            "SELECT id, canonical_name FROM fragrances WHERE id = ?", (int(needle),)
        ).fetchone()
        if row:
            return row

    from fragrance_graph.resolve.entities import load_candidates
    from fragrance_graph.resolve.names import best_match

    match = best_match(needle, load_candidates(conn))
    if match is None:
        return None
    return conn.execute(
        "SELECT id, canonical_name FROM fragrances WHERE id = ?",
        (match.fragrance_id,),
    ).fetchone()


def render(subject: str, results: list[Related]) -> str:
    lines = [f"{subject}", ""]
    if not results:
        lines.append(
            "No claims yet. Either nobody in the corpus compared it to "
            "anything, or the mentions have not been resolved — try "
            "`resolve.entities report`."
        )
        return "\n".join(lines)

    for r in results:
        people = "person" if r.commenters == 1 else "people"
        src = "source" if r.sources == 1 else "sources"
        lines.append(
            f"{r.commenters:>3} {people}  {r.claim_type:<11} "
            f"{r.canonical_name}  [{r.sentiment.lower()}]  "
            f"({r.sources} {src}; {r.pair_commenters} for the pair)"
        )
        for ev in r.evidence:
            arrow = "→" if ev.outbound else "←"
            lines.append(f'         {arrow} "{ev.quote}"')
            lines.append(f"           {ev.permalink}")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="What does the community say smells like this?"
    )
    parser.add_argument("fragrance", help="Fragrance id, canonical name, or alias")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--quotes", type=int, default=DEFAULT_QUOTES)
    parser.add_argument(
        "--min-commenters",
        type=int,
        default=1,
        help="Hide results supported by fewer than N distinct people",
    )
    parser.add_argument(
        "--min-sources",
        type=int,
        default=1,
        help="Hide results backed by fewer than N distinct videos/threads",
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        row = find_fragrance(conn, args.fragrance)
        if row is None:
            print(
                f"No fragrance matches {args.fragrance!r}. "
                "Curated fragrances are listed by `resolve.entities report`."
            )
            return 1
        results = similar_to(
            conn,
            row["id"],
            limit=args.limit,
            quotes=args.quotes,
            min_commenters=args.min_commenters,
            min_sources=args.min_sources,
        )
        print(render(row["canonical_name"], results))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
