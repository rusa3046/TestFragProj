"""Writing comments to the database, whatever fetched them.

Every source funnels through `ingest()`: YouTube, the manual seed loader,
and the tests. It is deliberately source-agnostic — it takes rows already
flattened into the columns `comments` expects and does the two things that
must be identical for all of them.

**Idempotent on `(source, source_id)`.** Re-running an ingest is free, so
an interrupted run is resumed rather than restarted, and a comment can
never be counted twice in a distinct-commenter total.

**Commits as it goes.** The commit in the `finally` block is what makes
resume real: an interrupt keeps everything fetched so far instead of
rolling the whole run back. Comments cost API quota; losing an hour of
them to a Ctrl-C is not acceptable.

`author_id` is derived here rather than in each source's normalizer, so it
cannot depend on which code path built the row.

Named `store` because it stores. This lived in `ingest/reddit.py` until
2026-08-10, which meant the most-imported function in the codebase was in
a module named after the one source that does not work — Reddit refused
API access to this project. The PRAW paths were deleted with it; they
could not run, and dead code that cannot run is worse than absent code
because it reads as an option.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from fragrance_graph.models import author_from_payload, video_from_payload

log = logging.getLogger("fragrance_graph.ingest.store")

#: Default for rows that do not name their own source. Every real caller
#: passes one — youtube passes "youtube", seed passes "manual" — so this
#: only covers direct calls in tests.
SOURCE = "reddit"


INSERT_SQL = """
INSERT INTO comments
    (source, source_id, body, permalink, created_utc, source_channel, score,
     author_id, video_id, raw_json)
VALUES
    (:source, :source_id, :body, :permalink, :created_utc, :source_channel, :score,
     :author_id, :video_id, :raw_json)
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
            payload = json.loads(row.get("raw_json") or "{}")
            # author_id is derived here rather than in each source's
            # normalizer, so it cannot depend on which code path built the
            # row. A row whose payload names no author is unknown-author,
            # which the ranking counts as its own distinct commenter.
            cur = conn.execute(
                INSERT_SQL,
                {
                    "source": source,
                    "author_id": author_from_payload(payload),
                    "video_id": video_from_payload(payload),
                    **row,
                },
            )
            if cur.rowcount:
                stats.new += 1
            else:
                stats.skipped += 1

            # A video is known to exist the moment a comment arrives from
            # it, whether or not its title has been fetched. Recorded even
            # when the comment was a duplicate, so a resumed run still
            # registers containers it saw.
            video_id = video_from_payload(payload)
            if video_id:
                conn.execute(
                    "INSERT OR IGNORE INTO videos (source, video_id, channel_id) "
                    "VALUES (?, ?, ?)",
                    (source, video_id, payload.get("channelId")),
                )

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
