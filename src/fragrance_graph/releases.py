"""From "a bottle exists" to "people have said things about it".

    uv run python -m fragrance_graph.releases discover --source fixture
    uv run python -m fragrance_graph.releases probe
    uv run python -m fragrance_graph.releases status

A newly announced fragrance is worth knowing about and not yet worth
paying for. Extraction costs money per comment, and on announcement day
there are no comments — so this subsystem is mostly a machine for
*waiting* well: notice cheaply, re-probe cheaply, and hand the bottle to
enrichment only when enough separate people are actually talking.

## The rule the whole file exists to hold

> An announcement is discovery provenance. It is never smell evidence.

A press release saying "notes of raspberry, vanilla and amber" tells us a
bottle exists and what its house claims about it. It tells us nothing
about what anybody smelled. Those are different facts with different
provenance, and the moment they are allowed to mix, "the brand says
raspberry" becomes "people say raspberry" and the product is lying.

So nothing here writes to `claims`, ever. `release_candidates` is a
separate table, the evidence layer does not read it, and the note text a
source supplies is not even stored — because a stored note list is a note
list somebody will eventually join to.

The same applies to retailer feeds. A retailer knows a product exists, its
name, its concentration and its price. It does not know what it smells
like, and a thousand SKUs are a thousand catalogue rows, not a thousand
enrichment jobs.

## Why the lifecycle has six states and not a boolean

    DISCOVERED              a source mentioned it
    VERIFIED                it looks like a real, distinct product
    CATALOGED               it has a fragrance row
    WAITING_FOR_DISCUSSION  nobody is talking about it yet
    ENRICHABLE              enough separate creators are
    EVIDENCED               people's claims are attached to it

Each transition is a decision about spending something. Collapsing them
into "known / unknown" loses the only interesting question — *is this
worth money yet* — and that question is the difference between a scheduler
and a spending spree.

## Readiness counts creators, not videos

Eight videos from one channel is one room. The gate that decides whether
to spend is therefore `relevant_creators`, matching `MIN_SOURCES`, and
`relevant_videos` is recorded but never gates anything. This is the same
independence rule the publishing gate uses, applied one step earlier so
that money follows it too.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.gate import MIN_SOURCES
from fragrance_graph.resolve.names import best_match, looks_like_junk

log = logging.getLogger("fragrance_graph.releases")


class Status(StrEnum):
    DISCOVERED = "DISCOVERED"
    VERIFIED = "VERIFIED"
    CATALOGED = "CATALOGED"
    WAITING_FOR_DISCUSSION = "WAITING_FOR_DISCUSSION"
    ENRICHABLE = "ENRICHABLE"
    EVIDENCED = "EVIDENCED"
    #: Not a real distinct product — a duplicate, a set, a sample vial.
    REJECTED = "REJECTED"


#: How many distinct creators must be talking before enrichment is worth
#: paying for. The same number the publishing gate uses, because a bottle
#: that cannot clear the gate is a bottle whose evidence cannot publish.
READY_CREATORS = MIN_SOURCES

#: How long to wait before probing again, by how many times we already
#: have. A bottle nobody discusses on day one may be discussed in a month;
#: probing it daily forever spends quota to learn the same thing. Backoff
#: is capped so a release never falls off the schedule entirely.
PROBE_BACKOFF_DAYS = (1, 3, 7, 14, 30)


@dataclass(frozen=True)
class ReleaseItem:
    """One announcement, as the source described it.

    Deliberately has no note list, no accord list and no description. A
    source may well supply them; storing them would create a table the
    evidence layer could one day be joined to, and the whole point is that
    it cannot be.
    """

    source_id: str
    name: str
    brand: str = ""
    url: str = ""
    release_date: str | None = None


class ReleaseSource(Protocol):
    """Where announcements come from.

    Implementations must be *permitted* sources: RSS or Atom feeds from
    fragrance publications, authorized retailer product feeds, official
    brand announcements, licensed product feeds. Scraping sites whose
    terms forbid it is not an implementation of this protocol, and the
    absence of a live adapter here is a licensing fact rather than a
    technical gap.
    """

    source_type: str

    def discover_since(self, since: str | None) -> list[ReleaseItem]:
        """Announcements newer than `since`, oldest first."""
        ...

    def provenance(self, item: ReleaseItem) -> str:
        """A human-readable statement of where this came from."""
        ...


@dataclass
class FixtureSource:
    """A source backed by a checked-in JSONL file.

    Not a mock. It implements the same protocol production adapters will,
    the lifecycle treats it identically, and there is no branch anywhere
    that asks whether the source is a fixture — which is the only way the
    fixture proves anything about the real thing.
    """

    path: Path
    source_type: str = "fixture"

    def discover_since(self, since: str | None) -> list[ReleaseItem]:
        items = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if since and (row.get("announced_at") or "") <= since:
                continue
            items.append(
                ReleaseItem(
                    source_id=row["source_id"],
                    name=row["name"],
                    brand=row.get("brand", ""),
                    url=row.get("url", ""),
                    release_date=row.get("release_date"),
                )
            )
        items.sort(key=lambda i: i.source_id)
        return items

    def provenance(self, item: ReleaseItem) -> str:
        return f"{self.source_type}:{item.source_id} {item.url}".strip()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _next_probe(probe_count: int) -> str:
    days = PROBE_BACKOFF_DAYS[min(probe_count, len(PROBE_BACKOFF_DAYS) - 1)]
    return (datetime.now(UTC) + timedelta(days=days)).isoformat(timespec="seconds")


@dataclass
class DiscoverStats:
    seen: int = 0
    added: int = 0
    updated: int = 0
    rejected: int = 0
    reasons: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.seen} announcement(s): {self.added} new, "
            f"{self.updated} already known, {self.rejected} rejected"
        )


UPSERT_SQL = """
INSERT INTO release_candidates
    (source_type, source_id, source_url, brand, name, release_date,
     status, status_reason, first_seen, last_seen, next_probe_at)
VALUES (%(source_type)s, %(source_id)s, %(url)s, %(brand)s, %(name)s,
        %(release_date)s, 'DISCOVERED', %(reason)s, %(now)s, %(now)s, %(now)s)
ON CONFLICT (source_type, source_id) DO UPDATE
   SET last_seen = EXCLUDED.last_seen,
       source_url = EXCLUDED.source_url,
       release_date = COALESCE(EXCLUDED.release_date,
                               release_candidates.release_date)
RETURNING (xmax = 0) AS inserted
"""


def discover(
    conn: psycopg.Connection, source: ReleaseSource, *, since: str | None = None
) -> DiscoverStats:
    """Record what a source announced. Cheap, idempotent, spends nothing.

    Re-running over the same feed adds nothing and loses nothing: rows are
    keyed on (source_type, source_id), and a second sighting refreshes
    `last_seen` rather than creating a duplicate. That matters because
    feeds repeat themselves constantly — an item stays in an RSS window for
    weeks — and a subsystem that grew a row per fetch would be unusable
    within a month.
    """
    stats = DiscoverStats()
    for item in source.discover_since(since):
        stats.seen += 1
        if looks_like_junk(item.name) or len(item.name.strip()) < 3:
            stats.rejected += 1
            stats.reasons.append(f"{item.source_id}: name is not usable")
            continue
        row = conn.execute(
            UPSERT_SQL,
            {
                "source_type": source.source_type,
                "source_id": item.source_id,
                "url": item.url,
                "brand": item.brand,
                "name": item.name,
                "release_date": item.release_date,
                "reason": source.provenance(item),
                "now": _now(),
            },
        ).fetchone()
        if row["inserted"]:
            stats.added += 1
        else:
            stats.updated += 1
    conn.commit()
    log.info("%s", stats)
    return stats


@dataclass
class VerifyStats:
    verified: int = 0
    cataloged: int = 0
    merged: int = 0
    unresolved: int = 0

    def __str__(self) -> str:
        return (
            f"{self.verified} verified, {self.cataloged} cataloged, "
            f"{self.merged} merged into existing bottles, "
            f"{self.unresolved} left unresolved"
        )


#: Words that name a *concentration*, not a different perfume. "Khamrah
#: Rouge" and "Khamrah Rouge EDP" are one bottle announced twice; letting
#: the second create its own row splits every count the launch will ever
#: earn. Kept apart from `HARMLESS_SUFFIX` in `attributes` because that
#: list guards the opposite mistake — merging a genuine flanker into its
#: base.
CONCENTRATION_WORDS = frozenset({
    "edp", "edt", "edc", "parfum", "extrait", "eau", "de", "toilette",
    "cologne", "spray",
})


def _identity(name: str) -> str:
    """A name reduced to what makes it a distinct product.

    Concentration words come off; everything else stays. "Elixir" and
    "Intense" deliberately do *not* come off — those name real flankers
    that smell different, and stripping them would merge bottles the
    corpus works hard to keep apart.
    """
    import re as _re

    words = [
        w
        for w in _re.findall(r"[a-z0-9]+", name.lower())
        if w not in CONCENTRATION_WORDS
    ]
    return " ".join(words)


def _existing_by_identity(conn: psycopg.Connection, full_name: str) -> int | None:
    """A catalogue row naming the same product, ignoring concentration.

    A second line of defence behind `best_match`. Fuzzy matching compares
    whole strings, so "Khamrah Rouge" against "Khamrah Rouge EDP" can fall
    below the threshold and quietly create a duplicate — and the release
    table has no uniqueness constraint on resolved product identity to
    catch it.
    """
    wanted = _identity(full_name)
    if not wanted:
        return None
    for row in conn.execute("SELECT id, canonical_name FROM fragrances"):
        if _identity(row["canonical_name"]) == wanted:
            return row["id"]
    return None


def _catalogue(conn: psycopg.Connection):
    from fragrance_graph.resolve.entities import load_candidates

    return load_candidates(conn)


def verify(conn: psycopg.Connection, *, dry_run: bool = False) -> VerifyStats:
    """Decide what each discovered announcement actually refers to.

    Three outcomes, and the third is the important one:

    - it names a bottle already in the catalogue, so the candidate is
      **merged** onto that row rather than creating a second node. Two
      publications announcing the same launch must converge on one
      fragrance, or every downstream count is split between duplicates;
    - it names something new and well-formed enough to catalogue;
    - it is ambiguous, and it **stays unresolved**. A guess here writes a
      fragrance nobody can trace, and an unresolved candidate is a row
      somebody can look at later — which is the whole reason `fragrance_id`
      is nullable.
    """
    from fragrance_graph.resolve.entities import add_fragrance

    stats = VerifyStats()
    candidates = _catalogue(conn)
    rows = conn.execute(
        "SELECT * FROM release_candidates WHERE status = 'DISCOVERED'"
        " ORDER BY id"
    ).fetchall()

    for row in rows:
        full_name = f"{row['brand']} {row['name']}".strip()
        match = best_match(full_name, candidates) or best_match(
            row["name"], candidates
        )
        if match:
            stats.verified += 1
            stats.merged += 1
            if not dry_run:
                conn.execute(
                    "UPDATE release_candidates SET status = 'CATALOGED',"
                    " fragrance_id = %s, status_reason = %s WHERE id = %s",
                    (
                        match.fragrance_id,
                        f"already catalogued as {match.canonical_name}",
                        row["id"],
                    ),
                )
            continue

        if not row["brand"]:
            # A name with no house is not verifiable. "Amber Oud" names a
            # dozen different bottles across a dozen houses, and choosing
            # one would be inventing a fact about a product.
            stats.unresolved += 1
            if not dry_run:
                conn.execute(
                    "UPDATE release_candidates SET status_reason = %s"
                    " WHERE id = %s",
                    ("no brand given; cannot tell which product this is",
                     row["id"]),
                )
            continue

        twin = _existing_by_identity(conn, full_name)
        if twin is not None:
            stats.verified += 1
            stats.merged += 1
            if not dry_run:
                conn.execute(
                    "UPDATE release_candidates SET status = 'CATALOGED',"
                    " fragrance_id = %s, status_reason = %s WHERE id = %s",
                    (twin, "same product, different concentration wording",
                     row["id"]),
                )
            continue

        stats.verified += 1
        stats.cataloged += 1
        if not dry_run:
            frag_id = add_fragrance(conn, full_name, brand=row["brand"],
                                    aliases=[row["name"]])
            conn.execute(
                "UPDATE release_candidates SET status = 'CATALOGED',"
                " fragrance_id = %s, status_reason = %s WHERE id = %s",
                (frag_id, "new catalogue entry from announcement", row["id"]),
            )
            candidates = _catalogue(conn)

    if not dry_run:
        conn.commit()
    log.info("%s", stats)
    return stats


@dataclass
class Readiness:
    """What a cheap probe learned about whether people are talking."""

    candidate_id: int
    name: str
    videos: int
    creators: int

    @property
    def ready(self) -> bool:
        return self.creators >= READY_CREATORS

    @property
    def status(self) -> Status:
        return Status.ENRICHABLE if self.ready else Status.WAITING_FOR_DISCUSSION


def probe(
    conn: psycopg.Connection,
    searcher,
    *,
    limit: int = 10,
    now: str | None = None,
) -> list[Readiness]:
    """Ask how many separate creators cover each waiting release.

    `searcher` is any callable taking a query and returning objects with a
    `channel_id`, which is what `frontier.search_with_creators` already
    returns. Injected rather than imported so this can be exercised without
    a network, a key or a quota.

    Spends search quota and **no money**. That separation is the point: the
    expensive decision is downstream, and it is made from what this
    returns.
    """
    now = now or _now()
    rows = conn.execute(
        "SELECT rc.*, f.canonical_name FROM release_candidates rc"
        " LEFT JOIN fragrances f ON f.id = rc.fragrance_id"
        " WHERE rc.status IN ('CATALOGED', 'WAITING_FOR_DISCUSSION')"
        "   AND (rc.next_probe_at IS NULL OR rc.next_probe_at <= %s)"
        " ORDER BY rc.probe_count, rc.id LIMIT %s",
        (now, limit),
    ).fetchall()

    results = []
    for row in rows:
        query = (row["canonical_name"] or f"{row['brand']} {row['name']}").strip()
        hits = searcher(f"{query} fragrance review")
        creators = {h.channel_id for h in hits if getattr(h, "channel_id", None)}
        readiness = Readiness(
            candidate_id=row["id"],
            name=query,
            videos=len(hits),
            creators=len(creators),
        )
        conn.execute(
            "UPDATE release_candidates"
            "   SET status = %s, status_reason = %s, last_probe_at = %s,"
            "       next_probe_at = %s, probe_count = probe_count + 1,"
            "       relevant_videos = %s, relevant_creators = %s"
            " WHERE id = %s",
            (
                readiness.status,
                f"{readiness.creators} creator(s) cover it; "
                f"{READY_CREATORS} needed",
                now,
                _next_probe(row["probe_count"]),
                readiness.videos,
                readiness.creators,
                row["id"],
            ),
        )
        results.append(readiness)
    conn.commit()
    return results


def mark_evidenced(conn: psycopg.Connection) -> int:
    """Promote releases whose fragrance now carries real community claims.

    Reads `claims`, never writes them. A release becomes EVIDENCED because
    people said things about the bottle, not because a source announced it
    — which is the same boundary the rest of the file keeps, stated once
    more at the only point where the two tables meet.
    """
    # EVIDENCED means the bottle carries evidence the rest of the system
    # would take seriously — the same independent-creator bar readiness
    # uses. One stray claim used to be enough, which let a row jump
    # straight from CATALOGED to EVIDENCED and made the status useless as
    # proof that the readiness gate ever ran.
    updated = conn.execute(
        """
        UPDATE release_candidates rc
           SET status = 'EVIDENCED',
               status_reason = 'community claims from '
                               || %(needed)s || '+ creators are attached'
         WHERE rc.status IN ('ENRICHABLE', 'WAITING_FOR_DISCUSSION', 'CATALOGED')
           AND rc.fragrance_id IS NOT NULL
           AND (
                 SELECT count(DISTINCT co.source_channel)
                   FROM claims c
                   JOIN comments co ON co.id = c.comment_id
                  WHERE c.subject_frag_id = rc.fragrance_id
                    AND c.polarity = 'ASSERTED'
                    AND c.evidence_verified = 1
                    AND co.source_channel <> ''
               ) >= %(needed)s
        """,
        {"needed": READY_CREATORS},
    ).rowcount
    conn.commit()
    return updated


def render_status(conn: psycopg.Connection) -> str:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM release_candidates"
        " GROUP BY status ORDER BY status"
    ).fetchall()
    if not rows:
        return "No release candidates. Run `releases discover` first."
    lines = ["release candidates:"]
    lines += [f"  {row['status']:<24} {row['n']}" for row in rows]
    waiting = conn.execute(
        "SELECT name, brand, relevant_creators, next_probe_at"
        "  FROM release_candidates WHERE status = 'WAITING_FOR_DISCUSSION'"
        " ORDER BY next_probe_at LIMIT 10"
    ).fetchall()
    if waiting:
        lines += ["", "waiting for discussion:"]
        for row in waiting:
            lines.append(
                f"  {row['brand']} {row['name']}".rstrip()
                + f" — {row['relevant_creators']} creator(s), "
                f"re-probe {row['next_probe_at'][:10]}"
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.releases",
        description="Track announced fragrances from rumour to evidence.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    disc = sub.add_parser("discover", help="Read a source; spends nothing")
    disc.add_argument("--source", default="fixture")
    disc.add_argument(
        "--path", type=Path, default=Path("data/fixtures/releases/announcements.jsonl")
    )
    disc.add_argument("--since", default=None)

    ver = sub.add_parser("verify", help="Resolve announcements to bottles")
    ver.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="Where every candidate stands")
    sub.add_parser("evidenced", help="Promote releases that now have claims")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "discover":
            if args.source != "fixture":
                print(
                    f"No adapter for {args.source!r}. Permitted sources are "
                    "RSS/Atom feeds, authorized retailer feeds, official "
                    "brand announcements and licensed feeds; none is "
                    "configured, so only the fixture source is available."
                )
                return 2
            print(discover(conn, FixtureSource(args.path), since=args.since))
        elif args.command == "verify":
            print(verify(conn, dry_run=args.dry_run))
        elif args.command == "evidenced":
            print(f"{mark_evidenced(conn)} release(s) now EVIDENCED.")
        else:
            print(render_status(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
