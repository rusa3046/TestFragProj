"""Load hand-collected text into `comments` for local testing.

The Reddit API is the real ingest path. This exists so extraction can be
exercised end to end on real language before API access is available, and
so a specific troublesome comment can be replayed without a network call.

Seeded rows use `source = "manual"` rather than `"reddit"`, so they are
distinguishable in every query and never inflate ingest statistics. The
uniqueness constraint is on `(source, source_id)`, so a seeded row and a
real Reddit row can coexist even if they hold the same text.

Input format: one file, entries separated by a line containing only `---`.
Blank entries are skipped. Paste plain text, not HTML or markdown chrome.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.ingest.reddit import ingest

log = logging.getLogger("fragrance_graph.ingest.seed")

SOURCE = "manual"
SEPARATOR = "---"


def parse_entries(text: str) -> list[str]:
    """Split a seed file into entries on `---` lines, dropping blanks."""
    entries = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == SEPARATOR:
            entries.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    entries.append("\n".join(current).strip())
    return [e for e in entries if e]


def make_row(body: str, *, subreddit: str, note: str = "") -> dict:
    """Build a comment row from raw text.

    source_id is a content hash, so re-seeding the same file is idempotent
    for free — the same text always produces the same id.
    """
    digest = hashlib.sha256(body.encode()).hexdigest()[:16]
    return {
        "source_id": f"seed_{digest}",
        "body": body,
        "permalink": f"seed://{digest}",
        "created_utc": int(time.time()),
        "subreddit": subreddit,
        "score": 0,
        "raw_json": json.dumps({"seeded": True, "note": note}, sort_keys=True),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed hand-collected comments for local testing.",
        epilog="Entries in the file are separated by a line containing only ---",
    )
    parser.add_argument("file", type=Path, help="Text file of entries")
    parser.add_argument(
        "--subreddit", default="fragrance", help="Recorded subreddit. Default: fragrance"
    )
    parser.add_argument(
        "--note", default="", help="Free-text note stored in raw_json for provenance"
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite database path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.file.exists():
        raise SystemExit(f"No such file: {args.file}")

    entries = parse_entries(args.file.read_text())
    if not entries:
        raise SystemExit(f"{args.file} contained no entries (separate them with ---)")

    rows = [make_row(e, subreddit=args.subreddit, note=args.note) for e in entries]

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        stats = ingest(conn, rows, source=SOURCE)
    finally:
        conn.close()

    log.info("Seeded from %s: %s", args.file, stats)
    log.info("Next: python -m fragrance_graph.extract.llm --limit %d", len(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
