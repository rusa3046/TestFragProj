"""Bounded, instrumented investigation of one bottle at a time.

## Why this exists

Discovery so far has been topic-shaped: ten broad seed queries, 47 videos,
6,504 comments spread thin across 895 bottles. The result is a corpus that
knows a great deal about Layton and almost nothing about anything else, and
a measurement that says why:

    claims about the less-discussed bottle in a pair -> does the pair publish?
              1      550 pairs        0 published     0.0%
            2-3      155 pairs        1 published     0.6%
            4-9       72 pairs        7 published     9.7%
            10+       23 pairs        6 published    26.1%

550 of 800 pairs are dead not because the corpus is small but because
nothing ever went back to the same bottle twice. Depth per bottle, not
volume, is what converts a pair into a page.

So this module does the opposite of seed-based discovery: it takes **one
named bottle** and spends a bounded amount of quota and money finding out
whether the community has said anything publishable about it.

## The two decisions

Everything here is one of two questions, and they are deliberately
separate — collapsing them is what produced the six-term priority formula
this module exists instead of.

**What to investigate** is answered by the corpus, not by a score. A bottle
people already mention 4-9 times is demand-proven; `candidates` ranks by
that and nothing else, because that is the only variable measured to
predict publication. `resolve.entities value` remains the richer queue for
curation work — this one is about evidence collection.

**How far to investigate it** is `Ceiling`, and it is the part with real
money attached. A bottle gets enough work to establish that it can or
cannot produce independent evidence, and then it stops — on success, on
futility, or on a hard per-bottle bound, whichever comes first.

## The feasibility gate, and the corpus-shaped mistake it nearly encoded

`gate.MIN_SOURCES` requires a pair's commenters to span two distinct
creators. A bottle only ever discussed by one creator therefore cannot
appear in *any* publishable pair, no matter how many comments are bought.
That makes creator count a **necessary** condition for publication, which
is what licenses using it as a filter: failing it proves futility, while
passing it proves nothing. The gate is per-pair and the test is per-bottle,
so a bottle can clear it and still have every individual pair stuck on one
creator. `probe` therefore reports feasibility, never success.

**The count that matters is the world's, not the corpus's**, and this
module was first written the other way round. Nine of the 54 candidates
appear on a single creator in our 47 videos, and it seemed obvious those
nine could never publish. Probing them says otherwise: `br 540` has 21
creators available, `dukhan` 21, `perseus` 20, `ultra male` 21. All nine
came back enrichable, and so did every other searchable candidate — 40 of
40, a median of 21 creators each, 13 of them new to the corpus.

So `blocked_in_corpus` gates *spending money*, never *searching*. Gating
the search on it would have made the corpus's blind spots permanent by
construction: the bottles we know least about are exactly the ones a
corpus-derived filter is most confident about. What the filter does screen
is `unsearchable` — strings like `dupes` and `the og` that name no bottle
at all, 14 of the 54, which is 1,400 quota units.

The whole arrangement is affordable because one `search.list` costs 100 of
the 10,000 daily units and returns the uploading channel alongside each
video id, so **the probe and the enrichment share a single search** —
feasibility is established as a side effect of the call the enrichment
needed anyway. A design that probes and then searches again would double
the cost of the scarcest thing here.

## What the run records

Every bottle writes one row to `data/experiments/`, carrying searches,
creators found, videos and comments processed, spend, claims added, pairs
touched, pairs newly publishable, and which stopping condition fired. The
point of the exercise is the conversion rate, and a run that reported only
"N became publishable" would answer the headline while destroying the
dataset needed to design the scheduler that comes after it.

Those rows are committed for the same reason `video_discoveries` is: a
search ranking is not reproducible, so a probe result not written down at
the moment it happened cannot be reconstructed later.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.ingest.store import ingest
from fragrance_graph.ingest.youtube import (
    COST_SEARCH,
    SOURCE,
    QuotaTracker,
    _get,
    iter_video_comments,
    record_discovery,
)
from fragrance_graph.resolve.entities import backfill
from fragrance_graph.resolve.names import normalize_name

log = logging.getLogger("fragrance_graph.frontier")

#: Where trial rows land. Not `data/corpus/` — the corpus is what the
#: product is built from, and these are measurements of how it was built.
EXPERIMENTS_DIR = Path("data/experiments")

#: The mention band the first experiment targets. Chosen from the
#: conversion table above: below 4 the observed rate is ~0%, and above 9
#: the bottle is already well covered, so 4-9 is where one pass is most
#: likely to change an outcome.
DEFAULT_LOW = 4
DEFAULT_HIGH = 9

#: Comparison claim types. A NOTE_DESCRIPTOR mention cannot become an edge,
#: so it is not evidence that a bottle is worth investigating.
COMPARISONS = ("SIMILAR_TO", "DUPE_OF", "BETTER_THAN")

#: Words that describe a bottle's category or variant without naming one.
#:
#: A curation queue can carry these because a human reads it and skips
#: them. A crawler cannot: `search.list` costs 100 units whether the query
#: names a fragrance or not, and "clones fragrance review" buys a topic
#: search of exactly the kind this module exists to stop doing.
#:
#: The rule is that a mention made *entirely* of these words names nothing.
#: "absolu" alone is unsearchable; "aventus absolu" is a bottle, because
#: `aventus` is not in this set. That is why the test is on every word
#: rather than on any word — the flanker vocabulary is real bottle
#: vocabulary too, and stripping it is how `Dior Sauvage Elixir` becomes a
#: buy link for the wrong bottle in `commerce/feeds.py`.
CATEGORY_WORDS = frozenset(
    """
    clone clones dupe dupes og original originals both all
    limited edition special anniversary vintage batch reformulation
    extrait parfum cologne elixir absolu exclusif intense edp edt edc
    eau de toilette version formulation concentration
    """.split()
)


def unsearchable(text: str, brands: frozenset[str] = frozenset()) -> str:
    """Why this mention cannot be searched as a bottle, or '' if it can.

    Three ways a string in the mention column names no bottle:

    - a pronoun, which `resolve.entities` already defines — reused rather
      than restated, because two definitions of "this names nothing" would
      drift and the curation queue would disagree with the crawler;
    - nothing but category or variant words (`clones`, `the og`, `limited
      edition`);
    - a bare brand. "Tom Ford" is searchable, but what comes back is the
      house's catalogue — a topic search wearing an entity's clothes, and
      the comments under it discuss thirty bottles rather than one.
    """
    from fragrance_graph.resolve.entities import is_pronoun

    if is_pronoun(text):
        return "pronoun"
    words = normalize_name(text).split()
    if not words:
        return "empty"
    if all(word in CATEGORY_WORDS for word in words):
        return "category-word"
    if " ".join(words) in brands:
        return "brand-only"
    return ""


def known_brands(conn: psycopg.Connection) -> frozenset[str]:
    """Normalized brand names already curated, for the brand-only test."""
    return frozenset(
        normalize_name(row["brand"])
        for row in conn.execute(
            "SELECT DISTINCT brand FROM fragrances WHERE brand IS NOT NULL"
        )
        if row["brand"]
    )


# --- what the corpus already knows -----------------------------------------


CANDIDATES_SQL = """
WITH mentions AS (
    SELECT lower(trim(c.raw_subject_text)) AS text,
           co.source_channel AS channel,
           co.author_id AS author
      FROM claims c
      JOIN comments co ON co.id = c.comment_id
     WHERE c.claim_type = ANY(%(types)s)
       AND c.object_kind = 'FRAGRANCE'
       AND c.polarity = 'ASSERTED'
       AND c.evidence_verified = 1
       AND trim(c.raw_subject_text) <> ''
    UNION ALL
    SELECT lower(trim(c.raw_object_text)),
           co.source_channel,
           co.author_id
      FROM claims c
      JOIN comments co ON co.id = c.comment_id
     WHERE c.claim_type = ANY(%(types)s)
       AND c.object_kind = 'FRAGRANCE'
       AND c.polarity = 'ASSERTED'
       AND c.evidence_verified = 1
       AND trim(c.raw_object_text) <> ''
)
SELECT text,
       count(*) AS claims,
       count(DISTINCT channel) AS creators,
       count(DISTINCT author) AS people
  FROM mentions
 GROUP BY text
HAVING count(*) BETWEEN %(low)s AND %(high)s
 ORDER BY count(*) DESC, count(DISTINCT channel) DESC, text
"""


@dataclass(frozen=True)
class Candidate:
    """A bottle the community already mentions, and its current coverage.

    `creators` is the count that decides whether this bottle can ever
    publish. It is reported from the corpus here and re-established
    against live search by `probe`, because a bottle stuck on one creator
    in our 47 videos may well have four more we never looked at — that is
    the entire premise of enrichment.
    """

    text: str
    claims: int
    creators: int
    people: int
    #: Why this string cannot be searched as a bottle, or '' if it can.
    unsearchable: str = ""

    @property
    def blocked_in_corpus(self) -> bool:
        """True when nothing already collected could satisfy MIN_SOURCES."""
        return self.creators < MIN_SOURCES

    @property
    def searchable(self) -> bool:
        """Worth spending a search on: the string names a bottle.

        Deliberately *not* also `not blocked_in_corpus`. A bottle on one
        creator in our 47 videos may have twenty in the world, and the
        search is the only thing that can tell — gating the probe on the
        corpus's own count would make the corpus's blind spots permanent.
        """
        return not self.unsearchable

    @property
    def viable(self) -> bool:
        """Worth spending money on: searchable, and not already known to
        be stuck on one creator. Enrichment's filter, not the probe's."""
        return self.searchable and not self.blocked_in_corpus

    @property
    def skip_reason(self) -> str:
        if self.unsearchable:
            return self.unsearchable
        return "one-creator" if self.blocked_in_corpus else ""

    def __str__(self) -> str:
        return (
            f"{self.text} ({self.claims} claims, {self.people} people, "
            f"{self.creators} creator{'s' if self.creators != 1 else ''})"
        )


def candidates(
    conn: psycopg.Connection,
    *,
    low: int = DEFAULT_LOW,
    high: int = DEFAULT_HIGH,
) -> list[Candidate]:
    """Bottles mentioned between `low` and `high` times, most-mentioned first."""
    rows = conn.execute(
        CANDIDATES_SQL,
        {"types": list(COMPARISONS), "low": low, "high": high},
    ).fetchall()
    brands = known_brands(conn)
    return [
        Candidate(
            text=row["text"],
            claims=row["claims"],
            creators=row["creators"],
            people=row["people"],
            unsearchable=unsearchable(row["text"], brands),
        )
        for row in rows
    ]


# --- the conversion table this experiment moves ----------------------------


PAIRS_SQL = """
SELECT lower(trim(c.raw_subject_text)) AS a,
       lower(trim(c.raw_object_text)) AS b,
       co.author_id AS author,
       co.source_channel AS channel
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 WHERE c.claim_type = ANY(%(types)s)
   AND c.object_kind = 'FRAGRANCE'
   AND c.polarity = 'ASSERTED'
   AND c.evidence_verified = 1
   AND trim(c.raw_subject_text) <> ''
   AND trim(c.raw_object_text) <> ''
"""


@dataclass(frozen=True)
class Pair:
    """Two bottle strings and the independent evidence connecting them."""

    a: str
    b: str
    people: int
    creators: int

    @property
    def publishable(self) -> bool:
        """The base gate. Flanker pairs are held to more; see `gate`.

        This deliberately applies the ordinary bar rather than detecting
        flankers, because it measures *movement* in a cohort rather than
        deciding what renders. `pages.py` remains the only thing that
        decides what a stranger sees.
        """
        return self.people >= MIN_COMMENTERS and self.creators >= MIN_SOURCES


def pairs(conn: psycopg.Connection) -> dict[tuple[str, str], Pair]:
    """Every comparison pair in the corpus, keyed by its two names sorted."""
    seen: dict[tuple[str, str], tuple[set, set]] = {}
    for row in conn.execute(PAIRS_SQL, {"types": list(COMPARISONS)}):
        a, b = row["a"], row["b"]
        if not a or not b or a == b:
            continue
        key = (a, b) if a < b else (b, a)
        people, creators = seen.setdefault(key, (set(), set()))
        people.add(row["author"])
        creators.add(row["channel"])
    return {
        key: Pair(a=key[0], b=key[1], people=len(p), creators=len(c))
        for key, (p, c) in seen.items()
    }


def pairs_for(all_pairs: dict[tuple[str, str], Pair], text: str) -> list[Pair]:
    return [p for key, p in all_pairs.items() if text in key]


BUCKETS = ((1, 1, "1"), (2, 3, "2-3"), (4, 9, "4-9"), (10, 10**9, "10+"))


def bucket_table(conn: psycopg.Connection) -> list[dict[str, Any]]:
    """The conversion table, recomputed. This is the experiment's outcome.

    Each pair is bucketed by the coverage of its *less*-discussed end,
    because that end is what limits the pair — a Layton comparison is only
    as publishable as the bottle Layton is compared to.
    """
    counts: dict[str, int] = {}
    for row in conn.execute(
        CANDIDATES_SQL.replace("HAVING count(*) BETWEEN %(low)s AND %(high)s", ""),
        {"types": list(COMPARISONS), "low": 0, "high": 0},
    ):
        counts[row["text"]] = row["claims"]

    table = [{"bucket": label, "pairs": 0, "published": 0} for _, _, label in BUCKETS]
    for pair in pairs(conn).values():
        weaker = min(counts.get(pair.a, 0), counts.get(pair.b, 0))
        for (lo, hi, _label), entry in zip(BUCKETS, table, strict=True):
            if lo <= weaker <= hi:
                entry["pairs"] += 1
                entry["published"] += int(pair.publishable)
                break
    for entry in table:
        entry["rate"] = entry["published"] / entry["pairs"] if entry["pairs"] else 0.0
    return table


def render_bucket_table(table: Sequence[dict[str, Any]]) -> str:
    lines = [f"{'claims about it':>16} {'pairs':>7} {'published':>10} {'rate':>7}"]
    for entry in table:
        lines.append(
            f"{entry['bucket']:>16} {entry['pairs']:>7} {entry['published']:>10} "
            f"{entry['rate']:>6.1%}"
        )
    return "\n".join(lines)


# --- one search, both answers ----------------------------------------------


@dataclass(frozen=True)
class VideoHit:
    """A video and the channel that uploaded it."""

    video_id: str
    channel_id: str
    channel_title: str


def search_with_creators(
    client: Any, api_key: str, query: str, *, limit: int, quota: QuotaTracker
) -> list[VideoHit]:
    """Search, returning the uploading channel alongside each video id.

    `search.list` costs 100 units whether `part` is `id` or `snippet`, and
    `snippet` carries `channelId`. Asking for it makes creator diversity
    free rather than a second `videos.list` round-trip — and, more to the
    point, lets the feasibility test run before any comment is fetched.
    """
    payload = _get(
        client,
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "maxResults": min(limit, 50),
            "key": api_key,
        },
    )
    quota.charge(COST_SEARCH)
    hits = []
    for item in payload.get("items", []):
        snippet = item.get("snippet", {})
        video_id = item.get("id", {}).get("videoId")
        channel_id = snippet.get("channelId")
        if not video_id or not channel_id:
            continue
        hits.append(
            VideoHit(
                video_id=video_id,
                channel_id=channel_id,
                channel_title=snippet.get("channelTitle", ""),
            )
        )
    log.info(
        "Search %r found %d videos across %d creators (%s)",
        query,
        len(hits),
        len({h.channel_id for h in hits}),
        quota,
    )
    return hits


def query_for(text: str) -> str:
    """The search string for a bottle.

    Deliberately *not* "<bottle> dupe". A query that asks for dupes
    returns comment sections assembled to discuss dupes, and the corpus
    already carries that bias: dupe-shaped seeds were 71% of discovery
    before broadening and 46% after. The evidence should say a bottle is a
    dupe of something; the query should not have gone looking for people
    willing to say so.
    """
    return f"{text} fragrance review"


def one_per_creator(
    hits: Iterable[VideoHit], *, known: frozenset[str] = frozenset()
) -> list[VideoHit]:
    """The first video from each distinct creator, unseen creators first.

    Two videos by one YouTuber are one audience under `MIN_SOURCES`, so a
    second is worth nothing to the gate and costs the same as the first.
    Creators absent from the corpus are ordered ahead of ones already in
    it, because a pair short of `MIN_SOURCES` is short of *creators* and
    only a new one can settle it.
    """
    best: dict[str, VideoHit] = {}
    for hit in hits:
        best.setdefault(hit.channel_id, hit)
    return sorted(best.values(), key=lambda h: (h.channel_id in known, h.channel_id))


def corpus_creators(conn: psycopg.Connection) -> frozenset[str]:
    return frozenset(
        row["source_channel"]
        for row in conn.execute(
            "SELECT DISTINCT source_channel FROM comments"
            " WHERE source_channel IS NOT NULL"
        )
    )


@dataclass(frozen=True)
class Probe:
    """What one search says about whether a bottle could ever publish."""

    text: str
    query: str
    videos: int
    creators: tuple[str, ...]
    fresh_creators: tuple[str, ...]
    corpus_creators: int
    quota_units: int

    @property
    def reachable_creators(self) -> int:
        """Distinct creators across the corpus and this search combined."""
        return self.corpus_creators + len(self.fresh_creators)

    @property
    def feasible(self) -> bool:
        """Necessary, never sufficient — see the module docstring."""
        return self.reachable_creators >= MIN_SOURCES

    @property
    def verdict(self) -> str:
        if not self.videos:
            return "no-coverage"
        return "enrichable" if self.feasible else "one-creator"


def probe(
    client: Any,
    api_key: str,
    candidate: Candidate,
    *,
    quota: QuotaTracker,
    known: frozenset[str] = frozenset(),
    limit: int = 25,
) -> tuple[Probe, list[VideoHit]]:
    """One search. Returns the verdict and the videos it can be enriched from."""
    query = query_for(candidate.text)
    before = quota.units
    hits = search_with_creators(client, api_key, query, limit=limit, quota=quota)
    ordered = one_per_creator(hits, known=known)
    found = tuple(h.channel_id for h in ordered)
    return (
        Probe(
            text=candidate.text,
            query=query,
            videos=len(hits),
            creators=found,
            fresh_creators=tuple(c for c in found if c not in known),
            corpus_creators=candidate.creators,
            quota_units=quota.units - before,
        ),
        ordered,
    )


# --- how far to go ---------------------------------------------------------


@dataclass(frozen=True)
class Ceiling:
    """The hard bound on one bottle's investigation.

    Every field is a stop the run cannot talk itself past. The defaults
    are sized so a 45-bottle cohort fits inside one day of YouTube quota
    (45 searches = 4,500 of 10,000 units) and inside the $1 daily cap with
    room to spare, because an experiment that needs the cap raised is an
    experiment that cannot be repeated cheaply.
    """

    max_searches: int = 1
    max_creators: int = 4
    max_videos_per_creator: int = 1
    max_comments: int = 400
    max_usd: float = 0.08

    def __str__(self) -> str:
        return (
            f"<={self.max_searches} search, <={self.max_creators} creators, "
            f"<={self.max_comments} comments, <=${self.max_usd:.2f}"
        )


#: Why a bottle's investigation ended. Recorded per bottle, because "how
#: often does futility stop us before the budget does" is the question the
#: scheduler after this will be built to answer.
STOP_REASONS = (
    "target-met",  # a pair involving it now clears the gate
    "one-creator",  # cannot satisfy MIN_SOURCES at any price
    "no-coverage",  # search returned nothing
    "creators-exhausted",  # every creator found has been processed
    "comment-ceiling",
    "spend-ceiling",
    "quota-exhausted",
    "daily-cap",  # the run stopped, not the bottle
)


@dataclass
class Trial:
    """Everything one bottle's investigation consumed and produced.

    Deliberately wider than the headline. "18 of 45 became publishable" is
    the answer to today's question; searches, creators, comments, spend and
    stop reason are what the scheduler is designed from afterwards.
    """

    text: str
    claims_before: int
    creators_before: int
    started_at: str
    query: str = ""
    verdict: str = ""
    searches: int = 0
    quota_units: int = 0
    creators_found: int = 0
    fresh_creators: int = 0
    videos_processed: int = 0
    comments_seen: int = 0
    comments_new: int = 0
    claims_added: int = 0
    mentions_resolved: int = 0
    pairs_before: int = 0
    pairs_after: int = 0
    publishable_before: int = 0
    publishable_after: int = 0
    site_pages_before: int = 0
    site_pages_after: int = 0
    usd: float = 0.0
    stop_reason: str = ""
    note: str = ""

    @property
    def newly_publishable(self) -> int:
        """Movement in *string* pairs — `pairs` groups by the words people
        typed, so "delina" and "the original delina" are two bottles here."""
        return self.publishable_after - self.publishable_before

    @property
    def new_site_pages(self) -> int:
        """Movement in pages a stranger could actually open.

        Not the same number as `newly_publishable`, and the difference is
        the point. `pages.qualifying_pairs` counts *resolved fragrances*,
        so it pools every spelling of a bottle into one and applies the
        flanker rule; `pairs` counts distinct strings and does neither.
        The first experiment reported `newly_publishable` alone and read
        it as pages, which under-counted: pooling spellings puts more
        people behind one pair, and pairs sitting one person short of the
        gate cross it when their two halves are recognised as the same
        bottle. Both are recorded because a gap between them is a
        resolution problem, and silently reporting one as the other is
        how that problem stays invisible.
        """
        return self.site_pages_after - self.site_pages_before

    def as_row(self) -> dict[str, Any]:
        return {"kind": "trial", **self.__dict__}


def publishable_count(all_pairs: dict[tuple[str, str], Pair], text: str) -> int:
    return sum(1 for p in pairs_for(all_pairs, text) if p.publishable)


def site_pages(conn: psycopg.Connection) -> int:
    """How many pair pages the site would build from the corpus right now.

    Imported here rather than at module scope because `pages` pulls in the
    whole rendering stack, and `frontier` is also used to plan runs on a
    machine that never builds a site. Whole-corpus rather than per-bottle:
    during a serial experiment the delta across one bottle *is* that
    bottle's effect, and the alternative — reimplementing the flanker rule
    here — would recreate the divergence this field exists to expose.
    """
    from fragrance_graph.pages import qualifying_pairs

    return len(qualifying_pairs(conn))


@dataclass
class RunLog:
    """Append-only record of one experiment run."""

    path: Path
    rows: list[dict[str, Any]] = field(default_factory=list)

    def write(self, row: dict[str, Any]) -> None:
        self.rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def run_path(run: str, directory: Path = EXPERIMENTS_DIR) -> Path:
    return directory / f"frontier-{run}.jsonl"


def default_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def store_hits(conn: psycopg.Connection, hits: Sequence[VideoHit], query: str, *, run: str) -> int:
    """Persist a search's results, creators included.

    `record_discovery` writes the video and the query that found it, but
    not who uploaded it — nothing needed that until creator diversity
    became a scheduling decision. Writing `channel_id` here is what lets
    the enrichment stage pick one video per creator *without searching
    again*, which is the whole reason the probe is affordable.
    """
    written = record_discovery(conn, [h.video_id for h in hits], query, run=run)
    for hit in hits:
        conn.execute(
            "UPDATE videos SET channel_id = %s, channel_title = %s"
            "  WHERE source = %s AND video_id = %s AND channel_id IS NULL",
            (hit.channel_id, hit.channel_title, SOURCE, hit.video_id),
        )
    conn.commit()
    return written


def recorded_hits(conn: psycopg.Connection, query: str, *, run: str) -> list[VideoHit]:
    """The videos a previous probe found for this query, with their creators."""
    rows = conn.execute(
        "SELECT v.video_id AS video_id, v.channel_id AS channel_id,"
        "       coalesce(v.channel_title, '') AS channel_title"
        "  FROM video_discoveries d"
        "  JOIN videos v ON v.source = d.source AND v.video_id = d.video_id"
        " WHERE d.source = %s AND d.retrieval_query = %s AND d.discovery_run = %s"
        "   AND v.channel_id IS NOT NULL"
        " ORDER BY v.video_id",
        (SOURCE, query, run),
    ).fetchall()
    return [
        VideoHit(
            video_id=row["video_id"],
            channel_id=row["channel_id"],
            channel_title=row["channel_title"],
        )
        for row in rows
    ]


def already_ingested(conn: psycopg.Connection, video_id: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM comments WHERE video_id = %s LIMIT 1", (video_id,)
        ).fetchone()
    )


# --- stage 1: probe (quota only, no money) ---------------------------------


def run_probe(
    conn: psycopg.Connection,
    client: Any,
    api_key: str,
    cohort: Sequence[Candidate],
    *,
    quota: QuotaTracker,
    run: str,
    log_to: RunLog,
    limit_per_search: int = 25,
) -> list[Trial]:
    """Search once per bottle, record what was found, spend no money.

    Separated from enrichment because the two consume different scarce
    things — this stage burns quota, the next burns the daily cap — and
    because the verdict is worth having on its own: a bottle only one
    creator ever covers should never reach an LLM.
    """
    known = corpus_creators(conn)
    before_pairs = pairs(conn)
    trials: list[Trial] = []

    for candidate in cohort:
        trial = Trial(
            text=candidate.text,
            claims_before=candidate.claims,
            creators_before=candidate.creators,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            pairs_before=len(pairs_for(before_pairs, candidate.text)),
            publishable_before=publishable_count(before_pairs, candidate.text),
        )
        # A probe fetches no comments, so nothing it does can move a pair.
        # Carrying `before` across keeps the delta at zero instead of
        # reporting -1 for every bottle that already had a page.
        trial.pairs_after = trial.pairs_before
        trial.publishable_after = trial.publishable_before
        if quota.remaining < COST_SEARCH:
            trial.stop_reason = "quota-exhausted"
            trial.verdict = "unprobed"
            trials.append(trial)
            log_to.write(trial.as_row())
            log.warning("Quota exhausted before %r; %s", candidate.text, quota)
            break

        result, ordered = probe(
            client, api_key, candidate, quota=quota, known=known,
            limit=limit_per_search,
        )
        store_hits(conn, ordered, result.query, run=run)

        trial.query = result.query
        trial.searches = 1
        trial.quota_units = result.quota_units
        trial.creators_found = len(result.creators)
        trial.fresh_creators = len(result.fresh_creators)
        trial.verdict = result.verdict
        trial.stop_reason = "" if result.feasible else result.verdict
        trials.append(trial)
        log_to.write(trial.as_row())
        log.info(
            "%s -> %s (%d videos, %d creators, %d new)",
            candidate.text, result.verdict, result.videos,
            len(result.creators), len(result.fresh_creators),
        )
    return trials


# --- stage 2: enrich (costs money) -----------------------------------------


def enrich_one(
    conn: psycopg.Connection,
    client: Any,
    api_key: str,
    candidate: Candidate,
    *,
    quota: QuotaTracker,
    ceiling: Ceiling,
    run: str,
    extract_fn: Any,
    trial: Trial,
) -> Trial:
    """Pull and extract one bottle's evidence, stopping at the first bound.

    The early stop is the point. A bottle whose pair crosses the gate after
    two creators does not need the third, and one whose search only ever
    returned a single creator does not need any — both stop here rather
    than running a schedule to completion.
    """
    hits = recorded_hits(conn, query_for(candidate.text), run=run)
    if not hits:
        trial.stop_reason = "no-coverage"
        return trial

    known = corpus_creators(conn)
    ordered = one_per_creator(hits, known=known)[: ceiling.max_creators]
    if candidate.creators + len({h.channel_id for h in ordered} - known) < MIN_SOURCES:
        trial.stop_reason = "one-creator"
        return trial

    trial.creators_found = len(ordered)
    trial.fresh_creators = len({h.channel_id for h in ordered} - known)
    claims_before = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
    trial.site_pages_before = site_pages(conn)
    trial.site_pages_after = trial.site_pages_before

    for hit in ordered:
        if trial.comments_seen >= ceiling.max_comments:
            trial.stop_reason = "comment-ceiling"
            break
        if trial.usd >= ceiling.max_usd:
            trial.stop_reason = "spend-ceiling"
            break
        if already_ingested(conn, hit.video_id):
            log.info("Video %s already ingested; skipping", hit.video_id)
            continue

        room = ceiling.max_comments - trial.comments_seen
        stats = ingest(
            conn,
            iter_video_comments(
                client, api_key, hit.video_id, limit=room, quota=quota
            ),
        )
        trial.videos_processed += 1
        trial.comments_seen += stats.seen
        trial.comments_new += stats.new

        spent = extract_fn(conn, limit=room)
        trial.usd += spent

        # Extraction stores the *text* a commenter typed; `subject_frag_id`
        # stays NULL until a mention is matched to a fragrance. `pairs`
        # below does not care — it groups by text — but `pages.py` counts
        # only resolved ids, so an unresolved corpus renders nothing no
        # matter how much was extracted. Running it here rather than at the
        # end of the experiment is what lets `site_pages` be measured per
        # bottle. Idempotent: re-running only does the new work.
        trial.mentions_resolved += backfill(conn).total

        # The stop that matters: has this bottle's evidence crossed the bar?
        now = pairs(conn)
        if publishable_count(now, candidate.text) > trial.publishable_before:
            trial.stop_reason = "target-met"
            break
    else:
        trial.stop_reason = trial.stop_reason or "creators-exhausted"

    trial.claims_added = (
        conn.execute("SELECT count(*) FROM claims").fetchone()[0] - claims_before
    )
    after = pairs(conn)
    trial.pairs_after = len(pairs_for(after, candidate.text))
    trial.publishable_after = publishable_count(after, candidate.text)
    trial.site_pages_after = site_pages(conn)
    return trial


def budgeted_extractor(
    client: Any, budget: Any, model_kwargs: dict[str, Any] | None = None
) -> Any:
    """An `extract_fn` that charges the daily ledger and stops at the cap.

    `enrich_one` takes extraction as a callable rather than importing it,
    so the stopping logic can be tested without an Anthropic key and
    without inventing a fake for the whole extraction pipeline. This is the
    real one.

    **It must delegate to `budget.guard`, not to `budget.record`.** The
    first version here called `record` and built its own callback around
    it. `record` appends a line to the ledger and returns; `guard` records
    *and raises* when the cap is crossed, and that raise is the entire
    mechanism. With `record` alone the ledger stayed perfectly accurate
    while the cap enforced nothing — a run on 2026-08-15 reached $1.11
    against a $1.00 cap and would have continued, and the ledger recorded
    every cent of it correctly.

    That is the same defect class the cap was written for: `budget.py`'s
    own docstring records the day the ledger reached $3.11 against a $1.00
    cap. A number that is measured but not enforced is not a cap.
    """
    from fragrance_graph.extract.llm import extract

    charge = budget.guard("frontier")

    def run(conn: psycopg.Connection, *, limit: int) -> float:
        spent = 0.0

        def on_spend(usd: float, comments: int) -> None:
            nonlocal spent
            spent += usd
            # Records first, then raises if that crossed the cap. The
            # increment above happens before the raise on purpose: the
            # trial has to report money that was genuinely spent even on
            # the batch that stopped the run.
            charge(usd, comments)

        extract(conn, client, limit=limit, on_spend=on_spend, **(model_kwargs or {}))
        return spent

    return run


# --- CLI -------------------------------------------------------------------


def render_cohort(cohort: Sequence[Candidate]) -> str:
    lines = [f"{'claims':>6} {'people':>7} {'creators':>9}  bottle"]
    for c in cohort:
        flag = f"  <- skip: {c.skip_reason}" if c.skip_reason else ""
        lines.append(f"{c.claims:>6} {c.people:>7} {c.creators:>9}  {c.text}{flag}")
    return "\n".join(lines)


def render_trials(trials: Sequence[Trial]) -> str:
    if not trials:
        return "No trials."
    lines = [
        f"{'verdict':>12} {'creators':>9} {'new':>4} {'comments':>9} "
        f"{'claims':>7} {'pages':>6} {'usd':>7}  bottle"
    ]
    for t in trials:
        lines.append(
            f"{t.verdict or t.stop_reason:>12} {t.creators_found:>9} "
            f"{t.fresh_creators:>4} {t.comments_new:>9} {t.claims_added:>7} "
            f"{t.newly_publishable:>+6} {t.usd:>7.4f}  {t.text}"
        )
    return "\n".join(lines)


def summarize(trials: Sequence[Trial]) -> str:
    """The headline, plus the parts that make it interpretable."""
    if not trials:
        return "No trials."
    reasons: dict[str, int] = {}
    for t in trials:
        reasons[t.stop_reason or "none"] = reasons.get(t.stop_reason or "none", 0) + 1
    moved = sum(1 for t in trials if t.newly_publishable > 0)
    usd = sum(t.usd for t in trials)
    quota = sum(t.quota_units for t in trials)
    lines = [
        f"{len(trials)} bottles investigated",
        f"  {moved} produced a newly publishable pair",
        f"  {sum(t.claims_added for t in trials)} claims added, "
        f"{sum(t.comments_new for t in trials)} new comments",
        f"  {quota} quota units, ${usd:.4f} spent",
    ]
    if moved:
        lines.append(f"  ${usd / moved:.4f} per newly publishable bottle")
    lines.append("  stopped because: " + ", ".join(
        f"{reason} x{count}" for reason, count in sorted(reasons.items())
    ))
    return "\n".join(lines)


def _cohort(conn: psycopg.Connection, args: argparse.Namespace) -> list[Candidate]:
    cohort = candidates(conn, low=args.low, high=args.high)
    if getattr(args, "viable_only", False):
        # The probe only needs the string to name a bottle; enrichment
        # additionally skips bottles the corpus shows on one creator.
        keep = "searchable" if args.command == "probe" else "viable"
        cohort = [c for c in cohort if getattr(c, keep)]
    if args.limit:
        cohort = cohort[: args.limit]
    return cohort


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Investigate one bottle at a time, under a hard ceiling.",
        epilog=(
            "probe costs YouTube quota and no money; enrich costs money. "
            "Run probe first — enrich reads the videos it recorded."
        ),
    )
    subs = parser.add_subparsers(dest="command", required=True)

    listing = subs.add_parser("candidates", help="Bottles worth investigating")
    probe_cmd = subs.add_parser("probe", help="One search each: can this ever publish?")
    enrich_cmd = subs.add_parser("enrich", help="Pull and extract; costs money")
    report = subs.add_parser("report", help="The conversion table, recomputed")

    for p in (listing, probe_cmd, enrich_cmd):
        p.add_argument("--low", type=int, default=DEFAULT_LOW)
        p.add_argument("--high", type=int, default=DEFAULT_HIGH)
        p.add_argument("--limit", type=int, default=0, help="0 = the whole cohort")
    for p in (probe_cmd, enrich_cmd):
        p.add_argument("--run", default="", help="Run id; defaults to a timestamp")
        p.add_argument(
            "--viable-only", action="store_true",
            help="Skip pronouns, category words, bare brands and one-creator bottles",
        )
    enrich_cmd.add_argument("--max-creators", type=int, default=Ceiling.max_creators)
    enrich_cmd.add_argument("--max-comments", type=int, default=Ceiling.max_comments)
    enrich_cmd.add_argument("--max-usd", type=float, default=Ceiling.max_usd)
    enrich_cmd.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be enriched and stop before spending anything",
    )
    for p in (listing, probe_cmd, enrich_cmd, report):
        p.add_argument("--db-url", default=DEFAULT_DB_URL)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    conn = get_connection(args.db_url)
    try:
        migrate(conn)

        if args.command == "report":
            print(render_bucket_table(bucket_table(conn)))
            return 0

        if args.command == "candidates":
            cohort = _cohort(conn, args)
            print(render_cohort(cohort))
            reasons: dict[str, int] = {}
            for c in cohort:
                if c.skip_reason:
                    reasons[c.skip_reason] = reasons.get(c.skip_reason, 0) + 1
            viable = sum(1 for c in cohort if c.viable)
            print(f"\n{len(cohort)} bottles, {viable} viable.")
            for reason, count in sorted(reasons.items()):
                print(f"  {count} skipped: {reason}")
            print(
                f"\nA search costs {COST_SEARCH} quota units, so probing the "
                f"viable ones costs {viable * COST_SEARCH} of 10,000 today."
            )
            return 0

        run = args.run or default_run_id()
        cohort = _cohort(conn, args)
        run_log = RunLog(path=run_path(run))

        if args.command == "probe":
            from fragrance_graph.ingest.youtube import build_client

            client, api_key = build_client()
            quota = QuotaTracker()
            print(
                f"Probing {len(cohort)} bottles: {len(cohort) * COST_SEARCH} of "
                f"{quota.remaining} quota units, no money.\n"
            )
            trials = run_probe(
                conn, client, api_key, cohort, quota=quota, run=run, log_to=run_log
            )
            print("\n" + render_trials(trials))
            print(f"\n{quota}")
            print(f"Recorded {len(trials)} trials in {run_log.path}")
            return 0

        # enrich
        ceiling = Ceiling(
            max_creators=args.max_creators,
            max_comments=args.max_comments,
            max_usd=args.max_usd,
        )
        planned = [
            c for c in cohort if recorded_hits(conn, query_for(c.text), run=run)
        ]
        if not planned:
            print(
                f"No probe results for run {run!r}. Run `frontier probe --run {run}` "
                "first — enrich never searches, so there is nothing to enrich from."
            )
            return 1
        print(f"Ceiling per bottle: {ceiling}")
        print(f"{len(planned)} of {len(cohort)} bottles have probe results.")
        if args.dry_run:
            print("\n".join(f"  would enrich: {c}" for c in planned))
            print("\n--dry-run: nothing fetched, nothing spent.")
            return 0

        from fragrance_graph.budget import Budget, BudgetExhausted
        from fragrance_graph.extract.llm import build_client as build_llm
        from fragrance_graph.ingest.youtube import build_client

        client, api_key = build_client()
        budget = Budget.load()
        extract_fn = budgeted_extractor(build_llm(), budget)
        quota = QuotaTracker()
        before_pairs = pairs(conn)
        trials = []
        for candidate in planned:
            trial = Trial(
                text=candidate.text,
                claims_before=candidate.claims,
                creators_before=candidate.creators,
                started_at=datetime.now(UTC).isoformat(timespec="seconds"),
                query=query_for(candidate.text),
                pairs_before=len(pairs_for(before_pairs, candidate.text)),
                publishable_before=publishable_count(before_pairs, candidate.text),
            )
            trial.pairs_after = trial.pairs_before
            trial.publishable_after = trial.publishable_before
            try:
                # Checked before the bottle rather than only inside the
                # extractor, so the run does not pay for a video whose
                # comments it cannot afford to read.
                budget.check(ceiling.max_usd)
                enrich_one(
                    conn, client, api_key, candidate, quota=quota, ceiling=ceiling,
                    run=run, extract_fn=extract_fn, trial=trial,
                )
            except BudgetExhausted as exc:
                trial.stop_reason = "daily-cap"
                trial.note = str(exc)[:200]
                trials.append(trial)
                run_log.write(trial.as_row())
                print(f"\nDaily cap reached before {candidate.text!r}: {exc}")
                break
            except Exception as exc:  # noqa: BLE001 - recorded, then the run stops
                trial.stop_reason = "error"
                trial.note = str(exc)[:200]
                trials.append(trial)
                run_log.write(trial.as_row())
                print(f"\nStopped at {candidate.text!r}: {exc}")
                break
            trials.append(trial)
            run_log.write(trial.as_row())
            log.info("%s", render_trials([trial]).splitlines()[-1])

        print("\n" + render_trials(trials))
        print("\n" + summarize(trials))
        print("\nConversion table now:\n" + render_bucket_table(bucket_table(conn)))
        print(f"\nRecorded {len(trials)} trials in {run_log.path}")
        return 0
    finally:
        if not conn.closed:
            conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
