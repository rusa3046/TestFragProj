"""Resolving claim mentions to `fragrances` rows.

Reads the mention text Phase 1 stored, matches it against the canonical
fragrance list, and fills in `claims.subject_frag_id` / `object_frag_id`.
Those two columns have existed since the first migration and been NULL
ever since; populating them is what turns edges between strings into
edges between things.

`subject_kind` and `object_kind` earn their keep here. Only FRAGRANCE
mentions are resolved: a CATEGORY subject ("skin scents") and a HOUSE
object ("Serge Lutens") are real signal, but neither is a bottle, and
trying to resolve them would either fail noisily or — worse — succeed
wrongly.

Curation is human work, deliberately. `report` ranks unresolved mentions
by how often they appear, so the twenty minutes spent naming the top of
that list resolves most of the corpus.

`value` ranks the same list by what naming each one would *publish*, which
is a different order and the one worth working down. Frequency puts "this"
(138 mentions) above "liquid brun" (11); the first unlocks nothing and the
second unlocks a page.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.resolve.names import (
    Candidate,
    Match,
    best_match,
    looks_like_junk,
    normalize_name,
)

log = logging.getLogger("fragrance_graph.resolve.entities")


def load_candidates(conn: sqlite3.Connection) -> list[Candidate]:
    """Every canonical fragrance, with the names it answers to."""
    candidates = []
    for row in conn.execute("SELECT id, canonical_name, aliases FROM fragrances"):
        try:
            aliases = tuple(json.loads(row["aliases"] or "[]"))
        except json.JSONDecodeError:
            log.warning("Fragrance %s has unparseable aliases; ignoring", row["id"])
            aliases = ()
        candidates.append(Candidate(row["id"], row["canonical_name"], aliases))
    return candidates


def add_fragrance(
    conn: sqlite3.Connection,
    canonical_name: str,
    *,
    brand: str | None = None,
    aliases: list[str] | None = None,
    house_year: int | None = None,
) -> int:
    """Create a canonical fragrance. Returns its id.

    `house_year` is the release year the house gives. Nothing in the
    pipeline collects it — it is null for every curated fragrance today —
    and pages show it only when a curator has supplied one.
    """
    cur = conn.execute(
        "INSERT INTO fragrances (canonical_name, brand, house_year, aliases)"
        " VALUES (?, ?, ?, ?)",
        (
            canonical_name,
            brand,
            house_year,
            json.dumps(sorted(set(aliases or []))),
        ),
    )
    conn.commit()
    return cur.lastrowid


def add_alias(conn: sqlite3.Connection, fragrance_id: int, alias: str) -> list[str]:
    """Teach an existing fragrance another name it answers to.

    This is where abbreviations live. No amount of string comparison
    connects "BR540" to "Baccarat Rouge 540"; a person stating it once
    resolves every future mention.
    """
    row = conn.execute(
        "SELECT aliases FROM fragrances WHERE id = ?", (fragrance_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"No fragrance with id {fragrance_id}")

    aliases = sorted(set(json.loads(row["aliases"] or "[]")) | {alias})
    conn.execute(
        "UPDATE fragrances SET aliases = ? WHERE id = ?",
        (json.dumps(aliases), fragrance_id),
    )
    conn.commit()
    return aliases


@dataclass
class Mention:
    """A distinct piece of mention text and how often it was written."""

    text: str
    count: int
    is_junk: bool


UNRESOLVED_SQL = """
SELECT text, count(*) AS n FROM (
    SELECT raw_subject_text AS text FROM claims
     WHERE subject_kind = 'FRAGRANCE' AND subject_frag_id IS NULL
    UNION ALL
    SELECT raw_object_text AS text FROM claims
     WHERE object_kind = 'FRAGRANCE' AND object_frag_id IS NULL
       AND raw_object_text IS NOT NULL
)
GROUP BY text ORDER BY n DESC, text
"""


def unresolved_mentions(
    conn: sqlite3.Connection, *, include_junk: bool = False
) -> list[Mention]:
    """Unresolved fragrance mentions, most frequent first.

    Frequency ordering is the point: curation effort should go where the
    corpus actually is, not down an alphabetical list.
    """
    mentions = [
        Mention(row["text"], row["n"], looks_like_junk(row["text"]))
        for row in conn.execute(UNRESOLVED_SQL)
    ]
    if include_junk:
        return mentions
    return [m for m in mentions if not m.is_junk]


#: The claim types that can become a page. A mention that only ever
#: appears in a NOTE_DESCRIPTOR unlocks nothing, however often it is
#: written, so it must not compete for a curator's evening.
EDGE_TYPES = ("SIMILAR_TO", "DUPE_OF", "BETTER_THAN")

#: Every claim where one end is an unresolved fragrance mention and the
#: other is already a curated bottle, one row per backing comment.
#:
#: The join to `comments` is what makes the gate computable: naming a
#: mention is only worth an evening if the pairs it unlocks have enough
#: distinct people behind them to publish, and that needs the author and
#: the uploading channel, not the claim count.
UNLOCKABLE_SQL = f"""
SELECT c.raw_subject_text AS mention, c.object_frag_id AS other_id,
       co.author_id AS author_id, co.id AS comment_id,
       co.source_channel AS channel
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 WHERE c.subject_kind = 'FRAGRANCE' AND c.subject_frag_id IS NULL
   AND c.raw_subject_text IS NOT NULL
   AND c.object_frag_id IS NOT NULL
   AND c.evidence_verified = 1 AND c.polarity = 'ASSERTED'
   AND c.claim_type IN ({", ".join(f"'{t}'" for t in EDGE_TYPES)})

UNION ALL

SELECT c.raw_object_text, c.subject_frag_id, co.author_id, co.id,
       co.source_channel
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 WHERE c.object_kind = 'FRAGRANCE' AND c.object_frag_id IS NULL
   AND c.raw_object_text IS NOT NULL
   AND c.subject_frag_id IS NOT NULL
   AND c.evidence_verified = 1 AND c.polarity = 'ASSERTED'
   AND c.claim_type IN ({", ".join(f"'{t}'" for t in EDGE_TYPES)})
"""


@dataclass(frozen=True)
class Unlock:
    """One pair that naming a mention would create."""

    other_id: int
    other_name: str
    commenters: int
    creators: int

    def clears(
        self,
        min_commenters: int = MIN_COMMENTERS,
        min_sources: int = MIN_SOURCES,
    ) -> bool:
        return self.commenters >= min_commenters and self.creators >= min_sources


@dataclass(frozen=True)
class MentionValue:
    """An unresolved mention, ranked by what naming it would publish.

    Frequency is the wrong queue. "it" is written more often than any real
    bottle and unlocks nothing; a mention appearing four times against four
    different curated fragrances is four pairs. The corpus holds 379 claims
    blocked by one unnamed end, and the difference between an evening spent
    at the top of this list and one spent at the top of the frequency list
    is the difference between pages and no pages.
    """

    #: The most-written spelling, which is what a curator will recognise.
    text: str
    #: Every spelling that normalises to the same thing, `text` included.
    #: One bottle is the unit of work, not one string: a curator names it
    #: once and `backfill` resolves "Creed", "creed" and "CREED " together,
    #: so counting them apart would understate the evening's value and
    #: scatter one decision across three rows of the queue.
    variants: tuple[str, ...]
    occurrences: int
    unlocks: tuple[Unlock, ...]

    @property
    def pairs(self) -> int:
        return len(self.unlocks)

    @property
    def publishable(self) -> int:
        """Unlocked pairs that would clear the gate on their own evidence.

        A **lower bound**, deliberately. It counts only the claims that
        name this mention, so if the curator resolves it to a bottle the
        corpus already knows — the ordinary case, since most unresolved
        text is a spelling of something curated — the real pair is this
        evidence plus whatever that bottle already had.
        """
        return sum(1 for u in self.unlocks if u.clears())

    @property
    def rank_key(self) -> tuple:
        return (-self.publishable, -self.pairs, -self.occurrences, self.text)


def mention_values(
    conn: sqlite3.Connection, *, include_junk: bool = False
) -> list[MentionValue]:
    """Unresolved mentions, most pages-unlocked first."""
    names = {
        row["id"]: row["canonical_name"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    # Spellings collapse to one row: `backfill` resolves them together, so
    # the queue counts the bottle rather than the string.
    spellings: dict[str, Counter] = {}
    for m in unresolved_mentions(conn, include_junk=True):
        spellings.setdefault(normalize_name(m.text), Counter())[m.text] += m.count

    # (bottle being named, bottle it connects to) -> people, and channels.
    people: dict[tuple[str, int], set[str]] = {}
    channels: dict[tuple[str, int], set[str]] = {}
    for row in conn.execute(UNLOCKABLE_SQL):
        key = (normalize_name(row["mention"]), row["other_id"])
        people.setdefault(key, set()).add(
            row["author_id"] or f"comment:{row['comment_id']}"
        )
        if row["channel"]:
            channels.setdefault(key, set()).add(row["channel"])

    grouped: dict[str, list[Unlock]] = {}
    for (key, other_id), authors in people.items():
        grouped.setdefault(key, []).append(
            Unlock(
                other_id=other_id,
                other_name=names.get(other_id, f"#{other_id}"),
                commenters=len(authors),
                creators=len(channels.get((key, other_id), ())),
            )
        )

    values = []
    for key, unlocks in grouped.items():
        counts = spellings.get(key, Counter())
        if not counts:
            continue
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        values.append(
            MentionValue(
                text=ranked[0][0],
                variants=tuple(text for text, _ in ranked),
                occurrences=sum(counts.values()),
                unlocks=tuple(
                    sorted(unlocks, key=lambda u: (-u.commenters, u.other_name))
                ),
            )
        )
    if not include_junk:
        values = [v for v in values if not looks_like_junk(v.text)]
    return sorted(values, key=lambda v: v.rank_key)


@dataclass
class BackfillStats:
    subjects_resolved: int = 0
    objects_resolved: int = 0
    exact: int = 0
    fuzzy: int = 0

    @property
    def total(self) -> int:
        return self.subjects_resolved + self.objects_resolved

    def __str__(self) -> str:
        return (
            f"{self.total} mentions resolved "
            f"({self.subjects_resolved} subjects, {self.objects_resolved} objects; "
            f"{self.exact} exact, {self.fuzzy} fuzzy)"
        )


def backfill(conn: sqlite3.Connection, *, dry_run: bool = False) -> BackfillStats:
    """Resolve every unresolved FRAGRANCE mention that matches a candidate.

    Idempotent: rows already carrying an id are skipped, so this can be
    re-run after each curation pass and only does the new work.
    """
    candidates = load_candidates(conn)
    stats = BackfillStats()
    if not candidates:
        log.warning("No fragrances defined yet — nothing can resolve.")
        return stats

    cache: dict[str, Match | None] = {}

    def resolve(text: str | None) -> Match | None:
        if not text:
            return None
        if text not in cache:
            cache[text] = best_match(text, candidates)
        return cache[text]

    rows = conn.execute(
        "SELECT id, subject_kind, raw_subject_text, subject_frag_id,"
        "       object_kind, raw_object_text, object_frag_id FROM claims"
    ).fetchall()

    for row in rows:
        if row["subject_kind"] == "FRAGRANCE" and row["subject_frag_id"] is None:
            match = resolve(row["raw_subject_text"])
            if match:
                if not dry_run:
                    conn.execute(
                        "UPDATE claims SET subject_frag_id = ? WHERE id = ?",
                        (match.fragrance_id, row["id"]),
                    )
                stats.subjects_resolved += 1
                setattr(stats, match.method, getattr(stats, match.method) + 1)

        if row["object_kind"] == "FRAGRANCE" and row["object_frag_id"] is None:
            match = resolve(row["raw_object_text"])
            if match:
                if not dry_run:
                    conn.execute(
                        "UPDATE claims SET object_frag_id = ? WHERE id = ?",
                        (match.fragrance_id, row["id"]),
                    )
                stats.objects_resolved += 1
                setattr(stats, match.method, getattr(stats, match.method) + 1)

    if not dry_run:
        conn.commit()
    return stats


RESOLVED_EDGES_SQL = """
SELECT a.canonical_name AS subject, b.canonical_name AS object,
       c.claim_type, count(*) AS mentions
FROM claims c
JOIN fragrances a ON a.id = c.subject_frag_id
JOIN fragrances b ON b.id = c.object_frag_id
WHERE c.evidence_verified = 1
  AND c.claim_type IN ('SIMILAR_TO', 'DUPE_OF', 'BETTER_THAN')
GROUP BY a.canonical_name, b.canonical_name, c.claim_type
ORDER BY mentions DESC, subject
"""


def resolved_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Fragrance-to-fragrance edges, grouped and counted.

    This is the first query in the project that answers the original
    question in any form: how many people said these two smell alike.
    """
    return conn.execute(RESOLVED_EDGES_SQL).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve claim mentions to canonical fragrances."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("report", help="Unresolved mentions, most frequent first")
    rep.add_argument("--limit", type=int, default=40)
    rep.add_argument(
        "--include-junk", action="store_true", help="Also show rejected mentions"
    )

    val = sub.add_parser(
        "value", help="Unresolved mentions ranked by the pages naming them unlocks"
    )
    val.add_argument("--limit", type=int, default=25)
    val.add_argument("--include-junk", action="store_true")
    val.add_argument(
        "--show-pairs", action="store_true",
        help="Name the bottles each mention would connect to",
    )

    add = sub.add_parser("add", help="Create a canonical fragrance")
    add.add_argument("canonical_name")
    add.add_argument("--brand", default=None)
    add.add_argument(
        "--alias", action="append", dest="aliases", default=[], help="Repeatable"
    )

    alias = sub.add_parser("alias", help="Add an alias to an existing fragrance")
    alias.add_argument("fragrance_id", type=int)
    alias.add_argument("alias")

    fill = sub.add_parser("backfill", help="Resolve mentions into claim rows")
    fill.add_argument(
        "--dry-run", action="store_true", help="Report without writing ids"
    )

    edges = sub.add_parser(
        "edges", help="Resolved fragrance-to-fragrance edges, counted"
    )

    # Iterate the registered subparsers rather than a hand-written list —
    # the hand-written one silently omitted `edges`, so the subcommand
    # rejected --db-path.
    for p in (rep, val, add, alias, fill, edges):
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(getattr(args, "db_path", DEFAULT_DB_PATH))
    migrate(conn)
    try:
        if args.command == "report":
            mentions = unresolved_mentions(conn, include_junk=args.include_junk)
            if not mentions:
                print("No unresolved fragrance mentions.")
                return 0
            print(f"{'count':>5}  mention")
            for m in mentions[: args.limit]:
                flag = "  [junk]" if m.is_junk else ""
                print(f"{m.count:>5}  {m.text}{flag}")
            print(f"\n{len(mentions)} distinct unresolved mentions.")

        elif args.command == "value":
            values = mention_values(conn, include_junk=args.include_junk)
            if not values:
                print(
                    "Nothing to unlock: no unresolved mention sits opposite a "
                    "curated bottle. Either everything is resolved, or both "
                    "ends of the remaining claims are unnamed."
                )
                return 0
            print(f"{'pages':>5} {'pairs':>5} {'seen':>5}  mention")
            for v in values[: args.limit]:
                print(f"{v.publishable:>5} {v.pairs:>5} {v.occurrences:>5}  {v.text}")
                if args.show_pairs:
                    for u in v.unlocks:
                        mark = "*" if u.clears() else " "
                        print(
                            f"      {mark} {u.commenters} people, "
                            f"{u.creators} creators  -> {u.other_name}"
                        )
            total = sum(v.publishable for v in values)
            print(
                f"\n{len(values)} mentions could be named. Naming all of them "
                f"would publish at least {total} pair(s); 'pages' is a lower "
                "bound, counting only the evidence already blocked on that "
                "mention and none the bottle may already have."
            )

        elif args.command == "add":
            frag_id = add_fragrance(
                conn, args.canonical_name, brand=args.brand, aliases=args.aliases
            )
            print(f"Added #{frag_id}: {args.canonical_name}")
            if args.aliases:
                print(f"  aliases: {', '.join(args.aliases)}")

        elif args.command == "alias":
            aliases = add_alias(conn, args.fragrance_id, args.alias)
            print(f"#{args.fragrance_id} now answers to: {', '.join(aliases)}")

        elif args.command == "backfill":
            stats = backfill(conn, dry_run=args.dry_run)
            prefix = "Would resolve" if args.dry_run else "Resolved"
            print(f"{prefix}: {stats}")

        elif args.command == "edges":
            rows = resolved_edges(conn)
            if not rows:
                print("No resolved edges yet. Add fragrances, then backfill.")
                return 0
            print(f"{'n':>3}  {'type':<12} edge")
            for row in rows:
                print(
                    f"{row['mentions']:>3}  {row['claim_type']:<12} "
                    f"{row['subject']} -> {row['object']}"
                )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
