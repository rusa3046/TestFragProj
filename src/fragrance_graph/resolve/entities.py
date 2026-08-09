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
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.resolve.names import Candidate, Match, best_match, looks_like_junk

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
) -> int:
    """Create a canonical fragrance. Returns its id."""
    cur = conn.execute(
        "INSERT INTO fragrances (canonical_name, brand, aliases) VALUES (?, ?, ?)",
        (canonical_name, brand, json.dumps(sorted(set(aliases or [])))),
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
    for p in (rep, add, alias, fill, edges):
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
