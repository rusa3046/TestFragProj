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

Lines whose first non-space character is `#` are file comments and are
stripped before storing. Without this, `seed/example.txt`'s own three-line
instructional header was stored as part of the first entry's body and
handed to the extractor as if a person had written it.

That rule collides with real content: commenters write "#1 of the year"
and "#nichefragrance". A stripped line is therefore logged, and the count
is reported at INFO, so a collision is visible rather than silent. If a
seed entry genuinely needs a leading `#`, indent it by one space.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.ingest.store import ingest

log = logging.getLogger("fragrance_graph.ingest.seed")

SOURCE = "manual"
SEPARATOR = "---"
COMMENT_PREFIX = "#"


def is_file_comment(line: str) -> bool:
    """Whether a line is file chrome rather than collected content.

    Deliberately strict: the `#` must be the first character, with no
    leading whitespace. Indenting a line by one space is the documented
    escape hatch for content that really does start with a hash.
    """
    return line.startswith(COMMENT_PREFIX)


def parse_entries(text: str) -> tuple[list[str], list[str]]:
    """Split a seed file into entries on `---` lines, dropping blanks.

    Returns `(entries, stripped_comment_lines)`. The second element is
    returned rather than discarded so the caller can report it — silently
    deleting input lines is how the header ended up inside entry one in the
    first place, only in reverse.
    """
    entries = []
    stripped: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if is_file_comment(line):
            stripped.append(line)
        elif line.strip() == SEPARATOR:
            entries.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    entries.append("\n".join(current).strip())
    return [e for e in entries if e], stripped


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
        "source_channel": subreddit,
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
    parser.add_argument("--db-url", default=DEFAULT_DB_URL, help="SQLite database path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not args.file.exists():
        raise SystemExit(f"No such file: {args.file}")

    entries, stripped = parse_entries(args.file.read_text())
    if not entries:
        raise SystemExit(f"{args.file} contained no entries (separate them with ---)")

    if stripped:
        log.info(
            "Stripped %d file-comment line(s) starting with '%s'. "
            "If any was real content, indent it by one space and re-run.",
            len(stripped),
            COMMENT_PREFIX,
        )
        for line in stripped:
            log.debug("  stripped: %s", line)

    rows = [make_row(e, subreddit=args.subreddit, note=args.note) for e in entries]

    conn = get_connection(args.db_url)
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
