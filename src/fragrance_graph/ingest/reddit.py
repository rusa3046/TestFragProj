"""Reddit comment ingest via PRAW, read-only.

Idempotent on (source, source_id), so an interrupted run resumes by simply
being run again: already-stored comments are skipped and only genuinely new
ones are inserted. Writes are committed in batches as the run proceeds, not
once at the end, so a kill -9 keeps everything fetched up to that point.

The PRAW iteration (`iter_subreddit_comments`) is deliberately separate from
the write loop (`ingest`), which consumes any iterable of plain dicts. That
keeps the resume/idempotency logic testable without live API calls.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.models import author_from_payload

log = logging.getLogger("fragrance_graph.ingest.reddit")

SOURCE = "reddit"
DEFAULT_SUBREDDITS = ("fragrance", "DelugeFragrance")
DEFAULT_SORTS = ("top", "new")

#: Seconds to wait between submission fetches. PRAW already respects
#: Reddit's rate limit headers, but that is the API's limit, not politeness.
#: This is the politeness margin on top of it.
DEFAULT_SLEEP = 1.0

#: Identifies the client to Reddit. A descriptive agent with a contact URL
#: is what keeps a read-only bot from being treated as abuse.
DEFAULT_USER_AGENT = (
    "fragrance-graph/0.1 (research; +https://github.com/rusa3046/TestFragProj)"
)

INSERT_SQL = """
INSERT INTO comments
    (source, source_id, body, permalink, created_utc, source_channel, score,
     author_id, raw_json)
VALUES
    (:source, :source_id, :body, :permalink, :created_utc, :source_channel, :score,
     :author_id, :raw_json)
ON CONFLICT (source, source_id) DO NOTHING
"""


@dataclass
class IngestStats:
    """Outcome of an ingest run."""

    new: int = 0
    skipped: int = 0

    @property
    def seen(self) -> int:
        return self.new + self.skipped

    def __str__(self) -> str:
        return f"{self.seen} seen, {self.new} new, {self.skipped} already stored"


def normalize_comment(comment: Any) -> dict[str, Any]:
    """Flatten a PRAW comment into the columns `comments` expects.

    The full payload is kept in raw_json — every non-private attribute PRAW
    has populated, stringified where it isn't JSON-native.
    """
    raw = {k: v for k, v in vars(comment).items() if not k.startswith("_")}
    return {
        "source_id": comment.fullname,
        "body": comment.body,
        "permalink": f"https://www.reddit.com{comment.permalink}",
        "created_utc": int(comment.created_utc),
        "source_channel": str(comment.subreddit),
        "score": int(comment.score),
        "raw_json": json.dumps(raw, default=str, sort_keys=True),
    }


def ingest(
    conn: sqlite3.Connection,
    comments: Iterable[dict[str, Any]],
    *,
    source: str = SOURCE,
    commit_every: int = 25,
    progress_every: int = 100,
) -> IngestStats:
    """Write comments idempotently, committing as we go.

    Returns counts of genuinely new vs. already-stored rows. The commit in
    the `finally` block is what makes resume real: an interrupt keeps
    everything fetched so far rather than rolling the whole run back.
    """
    stats = IngestStats()
    started = time.monotonic()
    try:
        for row in comments:
            # author_id is derived here rather than in each source's
            # normalizer, so it cannot depend on which code path built the
            # row. A row whose payload names no author is unknown-author,
            # which the ranking counts as its own distinct commenter.
            cur = conn.execute(
                INSERT_SQL,
                {
                    "source": source,
                    "author_id": author_from_payload(
                        json.loads(row.get("raw_json") or "{}")
                    ),
                    **row,
                },
            )
            if cur.rowcount:
                stats.new += 1
            else:
                stats.skipped += 1

            if stats.seen % commit_every == 0:
                conn.commit()
            if stats.seen % progress_every == 0:
                rate = stats.seen / max(time.monotonic() - started, 1e-9)
                log.info("%s (%.1f comments/s)", stats, rate)
    except KeyboardInterrupt:
        log.warning("Interrupted. Committing %d comments already fetched.", stats.new)
        raise
    finally:
        conn.commit()
    return stats


def build_reddit(user_agent: str | None = None) -> Any:
    """Construct a read-only PRAW client from environment credentials."""
    # Checked before importing praw so a missing .env reports as a missing
    # .env rather than as whatever happens to fail first.
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set. "
            "Copy .env.example to .env and fill them in."
        )

    try:
        import praw
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise SystemExit("praw is not installed. Run: uv sync") from exc

    reddit = praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent or os.environ.get("REDDIT_USER_AGENT", DEFAULT_USER_AGENT),
    )
    reddit.read_only = True
    return reddit


def iter_subreddit_comments(
    reddit: Any,
    subreddit_name: str,
    *,
    sorts: Iterable[str] = DEFAULT_SORTS,
    limit: int = 500,
    sleep: float = DEFAULT_SLEEP,
    time_filter: str = "all",
) -> Iterator[dict[str, Any]]:
    """Yield normalized comments from a subreddit's submission listings.

    `limit` bounds submissions scanned per sort, not comments yielded — a
    single submission can carry hundreds of comments.
    """
    subreddit = reddit.subreddit(subreddit_name)

    for sort in sorts:
        listing_fn = getattr(subreddit, sort)
        listing = (
            listing_fn(limit=limit, time_filter=time_filter)
            if sort == "top"
            else listing_fn(limit=limit)
        )
        log.info("r/%s: scanning %s (limit=%s)", subreddit_name, sort, limit)

        for n, submission in enumerate(listing, start=1):
            try:
                # limit=0 drops "load more comments" stubs rather than
                # spending a request each to expand them.
                submission.comments.replace_more(limit=0)
                comments = submission.comments.list()
            except Exception:
                log.exception("r/%s: skipping submission %s", subreddit_name, submission.id)
                continue

            for comment in comments:
                if getattr(comment, "body", None) in (None, "[deleted]", "[removed]"):
                    continue
                yield normalize_comment(comment)

            log.debug(
                "r/%s %s: submission %d/%s (%d comments)",
                subreddit_name, sort, n, limit, len(comments),
            )
            time.sleep(sleep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest Reddit comments into the fragrance-graph database."
    )
    parser.add_argument(
        "--subreddit",
        action="append",
        dest="subreddits",
        help=f"Subreddit to ingest; repeatable. Default: {', '.join(DEFAULT_SUBREDDITS)}",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Submissions to scan per sort (not comments). Default: 500",
    )
    parser.add_argument(
        "--sort",
        action="append",
        dest="sorts",
        choices=["top", "new", "hot", "rising"],
        help=f"Listing to pull; repeatable. Default: {', '.join(DEFAULT_SORTS)}",
    )
    parser.add_argument(
        "--time-filter",
        default="all",
        choices=["all", "year", "month", "week", "day", "hour"],
        help="Time window for --sort top. Default: all",
    )
    parser.add_argument(
        "--sleep",
        type=float,
        default=DEFAULT_SLEEP,
        help=f"Seconds between submission fetches. Default: {DEFAULT_SLEEP}",
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    subreddits = args.subreddits or list(DEFAULT_SUBREDDITS)
    sorts = args.sorts or list(DEFAULT_SORTS)

    conn = get_connection(args.db_path)
    migrate(conn)
    reddit = build_reddit()

    totals = IngestStats()
    try:
        for name in subreddits:
            stats = ingest(
                conn,
                iter_subreddit_comments(
                    reddit,
                    name,
                    sorts=sorts,
                    limit=args.limit,
                    sleep=args.sleep,
                    time_filter=args.time_filter,
                ),
            )
            log.info("r/%s: %s", name, stats)
            totals.new += stats.new
            totals.skipped += stats.skipped
    except KeyboardInterrupt:
        log.warning("Stopped early. Re-run to resume: %s", totals)
        return 130
    finally:
        conn.close()

    log.info("Done. %s", totals)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
