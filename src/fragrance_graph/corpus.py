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
from dataclasses import dataclass
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.models import author_from_payload, video_from_payload

log = logging.getLogger("fragrance_graph.corpus")

DEFAULT_CORPUS_DIR = Path("data/corpus")

COMMENTS_FILE = "comments.jsonl"
CLAIMS_FILE = "claims.jsonl"
FRAGRANCES_FILE = "fragrances.jsonl"
LABELS_FILE = "eval_labels.jsonl"

#: Video metadata, and *why we went looking there*.
#:
#: Discoveries are the load-bearing half. "This video was found by
#: searching `parfums de marly layton dupe`" is a historical fact about our
#: own sampling that nothing can recompute — re-running the search a month
#: later returns a different ranking. It is committed for the same reason
#: eval labels are: unrepeatable, and the basis of a published number.
#:
#: Titles are only nearly-regenerable. One `videos.list` call refetches
#: fifty of them for a single quota unit, but a deleted video takes its
#: title with it, which is the same argument that keeps comment bodies in
#: the corpus rather than re-fetching them on demand.
VIDEOS_FILE = "videos.jsonl"
DISCOVERIES_FILE = "video_discoveries.jsonl"

VIDEO_FIELDS = (
    "source",
    "video_id",
    "title",
    "channel_id",
    "channel_title",
    "published_at",
    "fetched_at",
)

DISCOVERY_FIELDS = ("source", "video_id", "retrieval_query", "retrieved_at",
                    "discovery_run")

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
    videos: int = 0
    discoveries: int = 0

    def __str__(self) -> str:
        return (
            f"{self.comments} comments, {self.claims} claims, "
            f"{self.fragrances} fragrances, {self.labels} eval labels, "
            f"{self.videos} videos, {self.discoveries} discoveries"
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

    videos = [
        {field: row[field] for field in VIDEO_FIELDS}
        for row in conn.execute(
            f"SELECT {', '.join(VIDEO_FIELDS)} FROM videos "
            "ORDER BY source, video_id"
        )
    ]
    discoveries = [
        {field: row[field] for field in DISCOVERY_FIELDS}
        for row in conn.execute(
            f"SELECT {', '.join(DISCOVERY_FIELDS)} FROM video_discoveries "
            "ORDER BY source, video_id, retrieval_query, discovery_run"
        )
    ]

    stats = CorpusStats(
        comments=_write_jsonl(directory / COMMENTS_FILE, comments),
        claims=_write_jsonl(directory / CLAIMS_FILE, claims),
        fragrances=_write_jsonl(directory / FRAGRANCES_FILE, fragrances),
        labels=_write_jsonl(directory / LABELS_FILE, labels),
        videos=_write_jsonl(directory / VIDEOS_FILE, videos),
        discoveries=_write_jsonl(directory / DISCOVERIES_FILE, discoveries),
    )
    log.info("Exported to %s: %s", directory, stats)
    return stats


def import_corpus(conn: sqlite3.Connection, directory: Path) -> CorpusStats:
    """Rebuild the corpus from `directory`. Safe to re-run.

    Comments and fragrances upsert on their natural keys. A comment's
    claims are replaced wholesale rather than merged, because claims have
    no natural key of their own — merging would duplicate them on every
    import.
    """
    directory = Path(directory)
    stats = CorpusStats()

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
    # video_id is derived on the same terms as author_id: a pure function
    # of raw_json, so exporting it would rewrite all 3,155 committed lines
    # to add a field already present in one of them.
    columns = (*COMMENT_FIELDS, "author_id", "video_id")
    for record in _read_jsonl(directory / COMMENTS_FILE):
        placeholders = ", ".join("?" for _ in columns)
        payload = json.loads(record["raw_json"] or "{}")
        author = author_from_payload(payload)
        video = video_from_payload(payload)
        conn.execute(
            f"INSERT INTO comments ({', '.join(columns)}) "
            f"VALUES ({placeholders}) "
            "ON CONFLICT (source, source_id) DO UPDATE SET "
            "body = excluded.body, extracted_at = excluded.extracted_at, "
            "score = excluded.score, raw_json = excluded.raw_json, "
            "author_id = excluded.author_id, video_id = excluded.video_id",
            (*(record[field] for field in COMMENT_FIELDS), author, video),
        )
        stats.comments += 1

    # Videos the comments imply, so an import lands the same state a fresh
    # ingest would. Rows in videos.jsonl below overwrite these with real
    # metadata where we have it.
    conn.execute(
        "INSERT OR IGNORE INTO videos (source, video_id) "
        "SELECT DISTINCT source, video_id FROM comments "
        "WHERE video_id IS NOT NULL AND video_id <> ''"
    )

    for record in _read_jsonl(directory / VIDEOS_FILE):
        conn.execute(
            f"INSERT INTO videos ({', '.join(VIDEO_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in VIDEO_FIELDS)}) "
            "ON CONFLICT (source, video_id) DO UPDATE SET "
            "title = excluded.title, channel_id = excluded.channel_id, "
            "channel_title = excluded.channel_title, "
            "published_at = excluded.published_at, "
            "fetched_at = excluded.fetched_at",
            tuple(record[field] for field in VIDEO_FIELDS),
        )
        stats.videos += 1

    for record in _read_jsonl(directory / DISCOVERIES_FILE):
        conn.execute(
            f"INSERT OR IGNORE INTO video_discoveries "
            f"({', '.join(DISCOVERY_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in DISCOVERY_FIELDS)})",
            tuple(record[field] for field in DISCOVERY_FIELDS),
        )
        stats.discoveries += 1

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
    log.info("Imported from %s: %s", directory, stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the corpus to JSONL, or rebuild a database from it."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export", help="Write the database out to JSONL")
    sub.add_parser("import", help="Rebuild a database from JSONL")

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
            import_corpus(conn, args.dir)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
