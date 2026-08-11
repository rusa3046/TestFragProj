"""Exporting the corpus to text, and rebuilding it.

The database is disposable; the corpus is not. Comments cost API quota,
claims cost real money, and curated fragrance aliases cost human judgement.
All three live in a SQLite file that is gitignored and, on a remote
container, deleted when the session ends.

So the durable form is newline-delimited JSON under `data/corpus/`, which
is committed. It survives container death, diffs line by line in review,
and can be read without SQLite. The `.db` file becomes a derived artifact:
throw it away, run `import`, get it back.

**Rows are linked by natural keys, never by autoincrement id.** A rebuilt
database assigns different `comments.id` values, so a claim pointing at
comment 7 would silently attach itself to a different comment. Claims
therefore reference `(source, source_id)` for their comment and
`canonical_name` for their fragrances — the same reasoning that keys the
eval train/holdout split on `source_id` rather than `id`.

One caveat worth stating plainly: these files contain other people's
comment text, retained under the source platform's API terms. Treat the
export as republication, because that is what committing it is.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.models import author_from_payload

log = logging.getLogger("fragrance_graph.corpus")

DEFAULT_CORPUS_DIR = Path("data/corpus")

COMMENTS_FILE = "comments.jsonl"
CLAIMS_FILE = "claims.jsonl"
FRAGRANCES_FILE = "fragrances.jsonl"
LABELS_FILE = "eval_labels.jsonl"
REJECTS_FILE = "rejected_claims.jsonl"

COMMENT_FIELDS = (
    "source",
    "source_id",
    "body",
    "permalink",
    "created_utc",
    "source_channel",
    "score",
    "extracted_at",
    "raw_json",
)

FRAGRANCE_FIELDS = ("canonical_name", "brand", "house_year")

CLAIM_FIELDS = (
    "claim_type",
    "polarity",
    "subject_kind",
    "raw_subject_text",
    "object_kind",
    "raw_object_text",
    "sentiment",
    "confidence",
    "evidence_span",
    "evidence_verified",
    "extraction_model",
    "created_at",
)

#: Values for claim columns an older export may not carry. ASSERTED is the
#: right default for `polarity` in the sense that it matches the pre-column
#: behaviour exactly — it is NOT a claim that those rows were checked. The
#: 36 known denials in the first corpus are still wrong until re-extracted;
#: `extract.polarity audit` is what finds them.
CLAIM_FIELD_DEFAULTS = {"polarity": "ASSERTED"}

#: Claims joined to their comment's natural key and to fragrance names.
#: Ordering is explicit so re-exporting an unchanged database produces a
#: byte-identical file and an empty diff.
EXPORT_CLAIMS_SQL = """
SELECT co.source AS comment_source,
       co.source_id AS comment_source_id,
       subj.canonical_name AS subject_fragrance,
       obj.canonical_name AS object_fragrance,
       c.*
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
  LEFT JOIN fragrances subj ON subj.id = c.subject_frag_id
  LEFT JOIN fragrances obj ON obj.id = c.object_frag_id
 ORDER BY co.source, co.source_id, c.claim_type,
          c.raw_subject_text, c.raw_object_text, c.id
"""


def _write_jsonl(path: Path, records: list[dict]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            # sort_keys and ensure_ascii=False keep diffs meaningful: stable
            # key order, and accented fragrance names stay readable.
            fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    return len(records)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


#: Rejected claims joined to their comment's natural key.
#:
#: Committed for the reason the table exists at all: its own migration
#: says the largest defect in the pipeline was one "nobody could look at
#: without paying to extract again". A rebuild from the corpus wiped the
#: table and restored exactly that problem — ~66 rejections were destroyed
#: on 2026-08-11 by a routine `import`, and answering the same question
#: again meant paying for another extraction.
#:
#: These rows are also the cheapest eval material available: they are
#: pre-selected for being where the extractor fails, which is where
#: labelling effort is worth most.
#:
#: One caveat, the same one the comment text carries. `raw_json` is model
#: output that failed validation, so it contains invented text — one
#: subject in the first committed batch reads '$300 vicks vapeorub', which
#: no human wrote. Committing it is republication of a machine's mistake
#: rather than of a person's words, which is the milder case, but it is
#: still publication.
EXPORT_REJECTS_SQL = """
SELECT co.source AS comment_source,
       co.source_id AS comment_source_id,
       r.reason, r.raw_json, r.extraction_model, r.created_at
  FROM rejected_claims r
  JOIN comments co ON co.id = r.comment_id
 ORDER BY co.source, co.source_id, r.reason, r.id
"""

REJECT_FIELDS = ("reason", "raw_json", "extraction_model", "created_at")

#: Eval labels joined to their comment's natural key.
EXPORT_LABELS_SQL = """
SELECT co.source AS comment_source,
       co.source_id AS comment_source_id,
       l.labeled_json, l.labeler, l.created_at
  FROM eval_labels l
  JOIN comments co ON co.id = l.comment_id
 ORDER BY co.source, co.source_id, l.labeler
"""


@dataclass
class CorpusStats:
    comments: int = 0
    claims: int = 0
    fragrances: int = 0
    labels: int = 0
    rejects: int = 0
    #: Fragrance names claims point at that fragrances.jsonl does not
    #: define. They import as NULL, so the claim quietly stops being an
    #: edge and nothing says so.
    dangling_fragrances: dict[str, int] = field(default_factory=dict)
    #: Names in the database the corpus does not list. Import never
    #: deletes, so these survive a rebuild and reappear on the next export.
    extra_fragrances: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.comments} comments, {self.claims} claims, "
            f"{self.fragrances} fragrances, {self.labels} eval labels, "
            f"{self.rejects} rejected claims"
        )


def export_corpus(conn: sqlite3.Connection, directory: Path) -> CorpusStats:
    """Write the corpus to `directory` as three JSONL files."""
    directory = Path(directory)

    comments = [
        {field: row[field] for field in COMMENT_FIELDS}
        for row in conn.execute(
            f"SELECT {', '.join(COMMENT_FIELDS)} FROM comments "
            "ORDER BY source, source_id"
        )
    ]

    fragrances = []
    for row in conn.execute(
        f"SELECT {', '.join(FRAGRANCE_FIELDS)}, aliases FROM fragrances "
        "ORDER BY canonical_name"
    ):
        record = {field: row[field] for field in FRAGRANCE_FIELDS}
        # Aliases are stored as a JSON string but exported as a real list,
        # so a curator can edit them in the file without escaping.
        record["aliases"] = sorted(json.loads(row["aliases"] or "[]"))
        fragrances.append(record)

    claims = []
    for row in conn.execute(EXPORT_CLAIMS_SQL):
        record = {field: row[field] for field in CLAIM_FIELDS}
        record["comment_source"] = row["comment_source"]
        record["comment_source_id"] = row["comment_source_id"]
        record["subject_fragrance"] = row["subject_fragrance"]
        record["object_fragrance"] = row["object_fragrance"]
        claims.append(record)

    # Eval labels are the most expensive rows in the database: an hour of
    # human judgement each time, and unlike claims they cannot be
    # regenerated by paying an API again.
    labels = [dict(row) for row in conn.execute(EXPORT_LABELS_SQL)]
    rejects = [dict(row) for row in conn.execute(EXPORT_REJECTS_SQL)]

    stats = CorpusStats(
        comments=_write_jsonl(directory / COMMENTS_FILE, comments),
        claims=_write_jsonl(directory / CLAIMS_FILE, claims),
        fragrances=_write_jsonl(directory / FRAGRANCES_FILE, fragrances),
        labels=_write_jsonl(directory / LABELS_FILE, labels),
        rejects=_write_jsonl(directory / REJECTS_FILE, rejects),
    )
    log.info("Exported to %s: %s", directory, stats)
    return stats


def extra_fragrances(conn: sqlite3.Connection, directory: Path) -> list[str]:
    """Fragrances in the database that the committed corpus does not have.

    Import upserts and never deletes, so a row removed from the corpus
    survives in a database built before the removal — and the next export
    writes it straight back. That is not hypothetical: a wrong curation
    entry was removed from `fragrances.jsonl` on 2026-08-11, resurrected
    itself through exactly this path, and had to be removed a second time.

    The database is a derived artifact by design ("throw it away, run
    import, get it back"), so the corpus is the authority and anything
    extra is drift. Reported rather than deleted by default, because the
    extras might equally be curation someone has not exported yet.
    """
    committed = {
        record["canonical_name"]
        for record in _read_jsonl(Path(directory) / FRAGRANCES_FILE)
    }
    return sorted(
        {
            row["canonical_name"]
            for row in conn.execute("SELECT canonical_name FROM fragrances")
            if row["canonical_name"] not in committed
        }
    )


def import_corpus(
    conn: sqlite3.Connection, directory: Path, *, prune: bool = False
) -> CorpusStats:
    """Rebuild the corpus from `directory`. Safe to re-run.

    Comments and fragrances upsert on their natural keys. A comment's
    claims are replaced wholesale rather than merged, because claims have
    no natural key of their own — merging would duplicate them on every
    import.

    Fragrances the corpus no longer lists are reported, and deleted only
    with `prune`. See `extra_fragrances` for why they are worth naming.
    """
    directory = Path(directory)
    stats = CorpusStats()

    extras = extra_fragrances(conn, directory)
    if extras:
        stats.extra_fragrances = extras
        if prune:
            # Claims resolved to these fragrances have to let go first, or
            # the foreign key refuses the delete. Clearing the ids is the
            # correct meaning of un-curating a bottle rather than a way
            # around the constraint: the claim keeps its raw text and goes
            # back to being an unresolved mention, which is where a name
            # the corpus no longer recognises belongs. The claim itself is
            # never deleted — it was paid for.
            placeholders = ",".join("?" * len(extras))
            ids = [
                row["id"]
                for row in conn.execute(
                    f"SELECT id FROM fragrances "
                    f"WHERE canonical_name IN ({placeholders})",
                    extras,
                )
            ]
            if ids:
                marks = ",".join("?" * len(ids))
                for column in ("subject_frag_id", "object_frag_id"):
                    conn.execute(
                        f"UPDATE claims SET {column} = NULL "
                        f"WHERE {column} IN ({marks})",
                        ids,
                    )
            conn.executemany(
                "DELETE FROM fragrances WHERE canonical_name = ?",
                [(name,) for name in extras],
            )
            log.warning(
                "Pruned %d fragrance(s) absent from the corpus: %s. "
                "Claims that pointed at them are unresolved mentions again.",
                len(extras), ", ".join(extras),
            )
        else:
            log.warning(
                "%d fragrance(s) in the database are NOT in the corpus and "
                "will be written back on the next export: %s. Re-run with "
                "--prune to delete them, or export if they are new curation.",
                len(extras), ", ".join(extras),
            )

    for record in _read_jsonl(directory / FRAGRANCES_FILE):
        aliases = json.dumps(sorted(set(record.get("aliases") or [])))
        existing = conn.execute(
            "SELECT id FROM fragrances WHERE canonical_name = ?",
            (record["canonical_name"],),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE fragrances SET brand = ?, house_year = ?, aliases = ? "
                "WHERE id = ?",
                (record.get("brand"), record.get("house_year"), aliases, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO fragrances (canonical_name, brand, house_year, aliases) "
                "VALUES (?, ?, ?, ?)",
                (
                    record["canonical_name"],
                    record.get("brand"),
                    record.get("house_year"),
                    aliases,
                ),
            )
        stats.fragrances += 1

    # author_id is derived, not exported. It lives inside raw_json already,
    # and duplicating it into the JSONL would rewrite all 3,155 lines of a
    # committed corpus to add a field that is a pure function of one
    # already there.
    columns = (*COMMENT_FIELDS, "author_id")
    for record in _read_jsonl(directory / COMMENTS_FILE):
        placeholders = ", ".join("?" for _ in columns)
        author = author_from_payload(json.loads(record["raw_json"] or "{}"))
        conn.execute(
            f"INSERT INTO comments ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT (source, source_id) DO UPDATE SET "
            "body = excluded.body, extracted_at = excluded.extracted_at, "
            "score = excluded.score, raw_json = excluded.raw_json, "
            "author_id = excluded.author_id",
            (*(record[field] for field in COMMENT_FIELDS), author),
        )
        stats.comments += 1

    fragrance_ids = {
        row["canonical_name"]: row["id"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    comment_ids = {
        (row["source"], row["source_id"]): row["id"]
        for row in conn.execute("SELECT id, source, source_id FROM comments")
    }

    claims = _read_jsonl(directory / CLAIMS_FILE)
    for comment_id in {
        comment_ids[key]
        for key in (
            (r["comment_source"], r["comment_source_id"]) for r in claims
        )
        if key in comment_ids
    }:
        conn.execute("DELETE FROM claims WHERE comment_id = ?", (comment_id,))

    skipped = 0
    for record in claims:
        key = (record["comment_source"], record["comment_source_id"])
        comment_id = comment_ids.get(key)
        if comment_id is None:
            # A claim whose comment is missing cannot be attached to
            # anything. Dropping it silently would corrupt counts, so it is
            # reported.
            skipped += 1
            continue

        for key_name in ("subject_fragrance", "object_fragrance"):
            named = record.get(key_name)
            if named and named not in fragrance_ids:
                # Editing fragrances.jsonl by hand leaves these behind, and
                # the import resolves them to NULL without complaint — the
                # claim silently stops being an edge. Five were committed
                # on 2026-08-11 by exactly that route.
                stats.dangling_fragrances[named] = (
                    stats.dangling_fragrances.get(named, 0) + 1
                )

        columns = ["comment_id", *CLAIM_FIELDS, "subject_frag_id", "object_frag_id"]
        values = [
            comment_id,
            # Defaults, not record[field], so an export written before a
            # column existed still imports. `polarity` was added after the
            # first real corpus was committed; without this, every claim in
            # it becomes a KeyError.
            *(record.get(field, CLAIM_FIELD_DEFAULTS.get(field))
              for field in CLAIM_FIELDS),
            fragrance_ids.get(record.get("subject_fragrance")),
            fragrance_ids.get(record.get("object_fragrance")),
        ]
        conn.execute(
            f"INSERT INTO claims ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            values,
        )
        stats.claims += 1

    rejects = _read_jsonl(directory / REJECTS_FILE)
    # Replaced wholesale per comment, for the reason claims are: a
    # rejection has no natural key of its own, so merging would duplicate
    # every row on every import.
    for comment_id in {
        comment_ids[key]
        for key in (
            (r["comment_source"], r["comment_source_id"]) for r in rejects
        )
        if key in comment_ids
    }:
        conn.execute(
            "DELETE FROM rejected_claims WHERE comment_id = ?", (comment_id,)
        )

    for record in rejects:
        key = (record["comment_source"], record["comment_source_id"])
        comment_id = comment_ids.get(key)
        if comment_id is None:
            skipped += 1
            continue
        columns = ["comment_id", *REJECT_FIELDS]
        conn.execute(
            f"INSERT INTO rejected_claims ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [comment_id, *(record.get(f) for f in REJECT_FIELDS)],
        )
        stats.rejects += 1

    for record in _read_jsonl(directory / LABELS_FILE):
        key = (record["comment_source"], record["comment_source_id"])
        comment_id = comment_ids.get(key)
        if comment_id is None:
            skipped += 1
            continue
        conn.execute(
            "INSERT INTO eval_labels (comment_id, labeled_json, labeler, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (comment_id, labeler) DO UPDATE SET "
            "labeled_json = excluded.labeled_json, created_at = excluded.created_at",
            (
                comment_id,
                record["labeled_json"],
                record["labeler"],
                record["created_at"],
            ),
        )
        stats.labels += 1

    conn.commit()
    if skipped:
        log.warning(
            "%d row(s) skipped: their comment is not in %s", skipped, COMMENTS_FILE
        )
    if stats.dangling_fragrances:
        log.warning(
            "%d claim reference(s) name a fragrance the corpus does not "
            "define, and imported as unresolved: %s. Re-export to write the "
            "corpus back consistently.",
            sum(stats.dangling_fragrances.values()),
            ", ".join(
                f"{name} ({n})"
                for name, n in sorted(
                    stats.dangling_fragrances.items(), key=lambda kv: -kv[1]
                )
            ),
        )
    log.info("Imported from %s: %s", directory, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the corpus to JSONL, or rebuild a database from it."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export", help="Write the database out to JSONL")
    imp = sub.add_parser("import", help="Rebuild a database from JSONL")
    imp.add_argument(
        "--prune", action="store_true",
        help="Delete fragrances the corpus no longer lists, instead of "
             "reporting them. The corpus is the authority.",
    )

    for name in ("export", "import"):
        p = sub.choices[name]
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)
        p.add_argument("--dir", type=Path, default=DEFAULT_CORPUS_DIR)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "export":
            export_corpus(conn, args.dir)
        else:
            import_corpus(conn, args.dir, prune=args.prune)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
