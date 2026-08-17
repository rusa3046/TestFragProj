"""Fragrance houses, curated once from Wikidata rather than pipeline-derived.

    uv run python -m fragrance_graph.houses import

`data/curation/houses.jsonl` is a committed, one-time batch of 204 houses
pulled from Wikidata under CC0 and matched to this corpus by hand — see
`data/curation/VERIFICATION.md` for how each match was made. This module
does one thing: load that file into the `houses` table so the rest of the
codebase can ask "is <X> a real fragrance house" without a network call.

The immediate reason it exists is the trailing-brand fix in
`resolve.names`: the Delina diagnostic
(`data/experiments/delina-resolution-loss.md`) found "atomic rose by
initio" and "delilah by maison alhambra" sitting in the catalogue,
unresolved, because the resolver strips a *leading* brand but not a
trailing "by <brand>". Fixing that safely needs a way to tell "by Initio"
(strip it, a real house) from "by Pink Floyd" (leave it, a song) — and
that distinction has to come from curated data, not from guessing at
capitalisation or string shape.

## Houses do not survive a corpus rebuild — and that is fine, for now

The production database is disposable: `corpus import` rebuilds it from
`data/corpus/*.jsonl`, the committed source of truth (see `corpus.py`).
Houses are not corpus. They cost nobody a comment fetch or an extraction
dollar — they are a second committed file, `data/curation/houses.jsonl`,
that `import_corpus` never reads and never will, because it lives under
`data/curation/` rather than `data/corpus/` on purpose: it is curated
*input*, the same category as the JSONL a person hand-edits, not a record
of anything this codebase observed.

So after any rebuild, `houses` is empty until this module's `import` is
run again. That is not the same failure `corpus.DERIVED_TABLES` guards
against — those tables are *computed from* the corpus and can go quietly
stale (rows present, every one pointing at a claim that no longer
exists), which is why they carry a liveness query as well as a row count.
Houses have no such staleness concept: there is no claim id for a house
to dangle against, so the only question worth asking is "is it there at
all". `corpus.CURATED_INPUT_TABLES` asks exactly that — a bare row count,
printed by `corpus import` right after the derived-tables notice, right
where a fresh clone or a post-rebuild session will actually see it.

Run `python -m fragrance_graph.houses import` after any rebuild. It is
free, and idempotent — re-running it is a no-op if the file has not
changed, and updates in place if it has.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate

log = logging.getLogger("fragrance_graph.houses")

DEFAULT_HOUSES_PATH = Path("data/curation/houses.jsonl")

#: Columns kept close to the JSONL's own field names on purpose — a
#: curator reading houses.jsonl next to `\d houses` should not have to
#: learn a second vocabulary for the same ten facts.
HOUSE_FIELDS = (
    "house_id",
    "name",
    "category",
    "wikidata_qid",
    "official_website",
    "source_url",
    "resolution_status",
    "match_basis",
    "publish_status",
    "wikidata_description",
)

UPSERT_SQL = f"""
INSERT INTO houses ({', '.join(HOUSE_FIELDS)})
VALUES ({', '.join('%(' + f + ')s' for f in HOUSE_FIELDS)})
ON CONFLICT (house_id) DO UPDATE SET
    {', '.join(f"{f} = excluded.{f}" for f in HOUSE_FIELDS if f != 'house_id')}
"""


def import_houses(
    conn: psycopg.Connection, path: Path = DEFAULT_HOUSES_PATH
) -> int:
    """Load `houses.jsonl` into the `houses` table. Idempotent.

    Keyed on `house_id` rather than `name`: the file assigns that id once
    and keeps it, so re-importing an unchanged file writes the same rows
    back over themselves, and a row whose name or wikidata_qid was
    corrected in a later commit updates in place instead of leaving a
    stale duplicate behind under the old spelling. Migration 0014's
    case-insensitive unique index on `name` is the backstop that catches
    it if two *different* house_ids ever named the same house.
    """
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        conn.execute(UPSERT_SQL, _house_values(record))
        n += 1
    conn.commit()
    log.info("Imported %d house(s) from %s", n, path)
    return n


def _house_values(record: dict) -> dict:
    """One JSONL row, ready to bind — every column in `UPSERT_SQL` names
    every `HOUSE_FIELDS` column explicitly, so a column's own SQL-level
    `DEFAULT` never fires: that only applies when a column is omitted from
    the INSERT entirely, never when an explicit `NULL` is bound to it,
    which is what `record.get(field)` alone would do for any key a row
    happens to be missing.

    `house_id` and `name` stay strict — direct indexing, so a row missing
    either raises immediately rather than writing a house nobody can
    identify or match against. The committed `houses.jsonl` never omits
    them; a hand-edit that did should fail loudly, atomically (nothing
    commits until every row in the file has been read), not silently
    write a broken row.

    The `NOT NULL DEFAULT` columns restore the value their own SQL-level
    default was meant to provide, since binding an explicit `None` would
    otherwise violate the constraint instead of falling back to it.
    `publish_status` also carries a CHECK — 'review' is what the column's
    own DEFAULT already means ("not yet cleared for display"), so a
    missing value gets that rather than an empty string that would fail
    the CHECK just as hard as a raised NotNullViolation would.

    `wikidata_qid`, `official_website`, `source_url` and
    `wikidata_description` are left as `record.get(field)` — genuinely
    nullable columns, because 43 of 204 real rows have no source URL at
    all, and `None` is the correct way to record "not known" there.
    Converting a missing value to `""` would erase that distinction; see
    migration 0014.
    """
    return {
        "house_id": record["house_id"],
        "name": record["name"],
        "category": record.get("category") or "",
        "resolution_status": record.get("resolution_status") or "",
        "match_basis": record.get("match_basis") or "",
        "publish_status": record.get("publish_status") or "review",
        "wikidata_qid": record.get("wikidata_qid"),
        "official_website": record.get("official_website"),
        "source_url": record.get("source_url"),
        "wikidata_description": record.get("wikidata_description"),
    }


def known_house_names(conn: psycopg.Connection) -> set[str]:
    """Every house name the resolver may treat as real, lowercased.

    Includes 'review' houses as well as 'publish' ones, deliberately.
    `publish_status` governs whether a house's *own page* is fit to show
    on the site — 82 of the 204 are not there yet. But the resolver asking
    "is 'Initio' a real house" so it can retry a stripped mention is an
    internal use, the same category as curation and audit tooling, not a
    display one. Filtering to 'publish' here would refuse to resolve a
    perfectly real bottle for a reason that has nothing to do with
    resolution — the house's Wikipedia blurb not being ready is not
    evidence the house doesn't exist.
    """
    return {row["name"].lower() for row in conn.execute("SELECT name FROM houses")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.houses",
        description="Import curated fragrance houses from data/curation/houses.jsonl.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="Load houses.jsonl. Free, idempotent.")
    imp.add_argument("--path", type=Path, default=DEFAULT_HOUSES_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        if args.command == "import":
            n = import_houses(conn, args.path)
            print(f"Imported {n} house(s) from {args.path}.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
