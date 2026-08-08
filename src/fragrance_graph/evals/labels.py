"""Ground-truth labels for measuring extraction quality.

Extraction quality has so far been judged by reading output by hand. That
does not scale past a few dozen comments and cannot detect regression: a
prompt change that raised variance sixfold and broke evidence verification
was only caught because it happened to be run three times and compared.

The workflow is export, edit, import:

    python -m fragrance_graph.evals.labels export labels.json
    # fill in the expected claims for each comment by hand
    python -m fragrance_graph.evals.labels import labels.json --labeler you

Labels record what *should* be extracted. They deliberately omit
confidence and evidence_span: those are model outputs, not facts about the
comment, and scoring them against a human guess would measure nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate

log = logging.getLogger("fragrance_graph.evals.labels")

TRAIN = "train"
HOLDOUT = "holdout"

#: Share of comments used for tuning. The rest is held out.
#:
#: With a few hundred labelled comments and enough prompt variants, some
#: variant scores well by luck. A holdout that is never consulted during
#: tuning is what distinguishes a real gain from a fitted one — so it must
#: exist before tuning starts, not after.
TRAIN_FRACTION = 0.7


def split_for(source_id: str, *, train_fraction: float = TRAIN_FRACTION) -> str:
    """Assign a comment to train or holdout, deterministically.

    Keyed on source_id rather than the autoincrement id so the split
    survives a database rebuild — otherwise re-seeding would silently
    reshuffle which comments were held out, and the holdout would leak.
    """
    digest = hashlib.sha256(source_id.encode()).hexdigest()
    bucket = int(digest[:8], 16) % 1000
    return TRAIN if bucket < train_fraction * 1000 else HOLDOUT


def export_template(conn: sqlite3.Connection, limit: int | None = None) -> list[dict]:
    """Comments with an empty claims list, ready to be filled in by hand."""
    sql = "SELECT id, source_id, body FROM comments ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [
        {
            "comment_id": row["id"],
            "split": split_for(row["source_id"]),
            "body": row["body"],
            "claims": [],
        }
        for row in conn.execute(sql)
    ]


def import_labels(
    conn: sqlite3.Connection, entries: list[dict], *, labeler: str
) -> int:
    """Store labels, replacing any previous ones by the same labeler."""
    now = datetime.now(UTC).isoformat()
    written = 0
    for entry in entries:
        comment_id = entry["comment_id"]
        payload = json.dumps(
            {"claims": entry.get("claims", []), "split": entry.get("split", TRAIN)},
            sort_keys=True,
        )
        conn.execute(
            "INSERT INTO eval_labels (comment_id, labeled_json, labeler, created_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (comment_id, labeler) DO UPDATE SET "
            "labeled_json = excluded.labeled_json, created_at = excluded.created_at",
            (comment_id, payload, labeler, now),
        )
        written += 1
    conn.commit()
    return written


def load_labels(
    conn: sqlite3.Connection, *, labeler: str | None = None, split: str | None = None
) -> dict[int, list[dict]]:
    """Labelled claims keyed by comment_id, optionally filtered by split."""
    sql = "SELECT comment_id, labeled_json FROM eval_labels"
    params: list = []
    if labeler:
        sql += " WHERE labeler = ?"
        params.append(labeler)

    out: dict[int, list[dict]] = {}
    for row in conn.execute(sql, params):
        payload = json.loads(row["labeled_json"])
        if split and payload.get("split") != split:
            continue
        out[row["comment_id"]] = payload.get("claims", [])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage extraction eval labels.")
    sub = parser.add_subparsers(dest="command", required=True)

    exp = sub.add_parser("export", help="Write a template to fill in by hand")
    exp.add_argument("file", type=Path)
    exp.add_argument("--limit", type=int, default=None)

    imp = sub.add_parser("import", help="Load a filled-in template")
    imp.add_argument("file", type=Path)
    imp.add_argument("--labeler", required=True, help="Who produced these labels")

    for p in (exp, imp):
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "export":
            entries = export_template(conn, args.limit)
            args.file.write_text(json.dumps(entries, indent=2, ensure_ascii=False))
            counts = {TRAIN: 0, HOLDOUT: 0}
            for e in entries:
                counts[e["split"]] += 1
            log.info(
                "Wrote %d comments to %s (%d train, %d holdout).",
                len(entries),
                args.file,
                counts[TRAIN],
                counts[HOLDOUT],
            )
            log.info("Fill in the claims list for each, then import.")
        else:
            entries = json.loads(args.file.read_text())
            n = import_labels(conn, entries, labeler=args.labeler)
            log.info("Imported labels for %d comments as %r.", n, args.labeler)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
