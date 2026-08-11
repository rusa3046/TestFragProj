"""Retrieval provenance for the first corpus, recovered from PROVENANCE.md.

`data/corpus/PROVENANCE.md` recorded which search query surfaced which
video, as a markdown table, for a human to read. That table turns out to
carry the answer to a question the product needs to ask programmatically:
**how many genuinely different searches back this edge?**

Measured across the six pairs that currently clear the publishing gate,
four are supported entirely by videos retrieved by a *single* query. Three
different `parfums de marly layton dupe` videos are three separate comment
sections — so `min_sources >= 2` passes them — but they are three rooms in
which the same question was put to an audience gathered for that question.

This script moves that fact out of prose and into `video_discoveries`, so
it can be counted rather than remembered.

Recorded as `run-1-2026-08-09` because it is one ingest run. Later runs
record their own discoveries as they search, and a video re-found by a
different query gains a *second* row rather than overwriting the first —
which is the whole reason discovery is its own table.

    uv run python scripts/seed_video_discoveries.py

Safe to re-run: the unique constraint makes every insert idempotent.
"""

from __future__ import annotations

import argparse
import logging
import sys

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate

log = logging.getLogger("seed_video_discoveries")

SOURCE = "youtube"
RUN = "run-1-2026-08-09"
RETRIEVED_AT = "2026-08-09"

#: (retrieval_query, video_id), transcribed from the run-1 table in
#: data/corpus/PROVENANCE.md. Order matches that table.
DISCOVERIES: list[tuple[str, str]] = [
    ("baccarat rouge 540 dupe", "sN9pXNod5iE"),
    ("baccarat rouge 540 dupe", "Zh-Cx8nFKbQ"),
    ("baccarat rouge 540 dupe", "1YP1acQbsGQ"),
    ("creed aventus dupe", "xrZs6rAEq-w"),
    ("creed aventus dupe", "b4SEOsiRydw"),
    ("creed aventus dupe", "ZOd2QEVJX8c"),
    ("dior sauvage dupe", "tP4jhMqUYCU"),
    ("dior sauvage dupe", "QTdp7ZZ7OM8"),
    ("dior sauvage dupe", "5VT9WmQKb9w"),
    ("tom ford oud wood dupe", "q0Q_VDgJV9s"),
    ("tom ford oud wood dupe", "qytnonMTe-U"),
    ("tom ford oud wood dupe", "tDr7FON9jUg"),
    ("parfums de marly delina dupe", "7ETjZDC1aEU"),
    ("parfums de marly delina dupe", "kpDNoS1og6Q"),
    ("parfums de marly delina dupe", "JP7zSQ-bR10"),
    ("parfums de marly layton dupe", "MmmVbQGP1qk"),
    ("parfums de marly layton dupe", "gdVAjqsK28I"),
    ("parfums de marly layton dupe", "OPxVZKffBAw"),
    ("lattafa khamrah review", "fqHC5kLs0MY"),
    ("lattafa khamrah review", "00mVJV-HkTc"),
    ("lattafa khamrah review", "wqNw73QZxJk"),
    ("lattafa bade'e al oud review", "_Gs_z7_AlGw"),
    ("lattafa bade'e al oud review", "HS_YHabwTC4"),
    ("lattafa bade'e al oud review", "S7iJz3NA3Vs"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        known = {
            row["video_id"]
            for row in conn.execute(
                "SELECT video_id FROM videos WHERE source = ?", (SOURCE,)
            )
        }
        added = 0
        for query, video_id in DISCOVERIES:
            if video_id not in known:
                # The corpus should already hold comments from every video
                # in this table. A miss means the two have drifted, which
                # is worth saying out loud rather than silently inserting a
                # discovery for a video nothing references.
                log.warning(
                    "%s is in PROVENANCE.md but has no comments in this "
                    "database; recording it anyway", video_id
                )
                conn.execute(
                    "INSERT OR IGNORE INTO videos (source, video_id) VALUES (?, ?)",
                    (SOURCE, video_id),
                )
            cur = conn.execute(
                "INSERT OR IGNORE INTO video_discoveries "
                "(source, video_id, retrieval_query, retrieved_at, discovery_run) "
                "VALUES (?, ?, ?, ?, ?)",
                (SOURCE, video_id, query, RETRIEVED_AT, RUN),
            )
            added += cur.rowcount
        conn.commit()

        queries = {q for q, _ in DISCOVERIES}
        print(
            f"{added} discovery row(s) added "
            f"({len(DISCOVERIES)} in the table, {len(queries)} distinct queries)."
        )
        print(
            "\nNext:\n"
            "  uv run python -m fragrance_graph.pages pairs --show-queries\n"
            "  uv run python -m fragrance_graph.corpus export"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
