"""YouTube comment ingest via the YouTube Data API v3.

Reddit API access was refused, so YouTube is the primary source. Fragrance
review videos carry dense comparison language — "smells just like Aventus",
"better than BR540" — which is exactly what the extractor is looking for.

Credentials are an API key from Google Cloud Console (enable "YouTube Data
API v3"), issued immediately with no review.

Quota is the real constraint rather than rate limiting: 10,000 units per
day by default. `commentThreads.list` costs 1 unit and returns up to 100
comments, so comment collection is cheap. `search.list` costs 100 units, so
searching is the expensive part — find video IDs once, then pull comments
by ID. Usage is logged for the same reason extraction cost is: a budget you
cannot see is a budget you will exceed.

Uses plain REST via httpx rather than google-api-python-client, which would
add a large dependency tree to make three GET requests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.ingest.store import ingest

log = logging.getLogger("fragrance_graph.ingest.youtube")

SOURCE = "youtube"
API_ROOT = "https://www.googleapis.com/youtube/v3"

#: Quota units per call, from the YouTube Data API documentation.
COST_COMMENT_THREADS = 1
COST_SEARCH = 100
DAILY_QUOTA = 10_000

#: Seconds between requests. The quota is the binding limit, but pacing
#: keeps a long run from looking like a burst.
DEFAULT_SLEEP = 0.2


@dataclass
class QuotaTracker:
    """Running quota spend, so a long run cannot silently exhaust the day."""

    units: int = 0
    requests: int = 0

    def charge(self, cost: int) -> None:
        self.units += cost
        self.requests += 1

    @property
    def remaining(self) -> int:
        return max(DAILY_QUOTA - self.units, 0)

    def __str__(self) -> str:
        return (
            f"{self.requests} requests, {self.units} quota units used, "
            f"~{self.remaining} of {DAILY_QUOTA} left today"
        )


def parse_published_at(value: str) -> int:
    """RFC 3339 timestamp to a unix int, matching `comments.created_utc`."""
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def normalize_comment(
    comment_id: str, snippet: dict[str, Any], *, video_id: str
) -> dict[str, Any]:
    """Flatten one YouTube comment into the columns `comments` expects."""
    return {
        "source_id": comment_id,
        "body": snippet.get("textOriginal") or snippet.get("textDisplay", ""),
        "permalink": f"https://www.youtube.com/watch?v={video_id}&lc={comment_id}",
        "created_utc": parse_published_at(snippet["publishedAt"]),
        "source_channel": snippet.get("channelId") or video_id,
        "score": int(snippet.get("likeCount", 0)),
        "raw_json": json.dumps(snippet, default=str, sort_keys=True),
    }


def build_client() -> tuple[Any, str]:
    """Return (httpx client, api key), checking credentials first."""
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        raise SystemExit(
            "YOUTUBE_API_KEY must be set. Create one at console.cloud.google.com "
            "(enable 'YouTube Data API v3'), then copy .env.example to .env."
        )
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise SystemExit("httpx is not installed. Run: uv sync") from exc
    return httpx.Client(timeout=30.0), api_key


#: The API key rides in the query string, so any text containing a request
#: URL is a credential leak waiting to happen.
_KEY_IN_URL = re.compile(r"(key=)[^&\s\"']+", re.IGNORECASE)


def redact_key(text: str) -> str:
    """Strip an API key out of text before it reaches a human or a log.

    httpx's own `raise_for_status()` puts the full request URL — key
    included — into the exception message, so an unhandled status code
    prints the credential to the terminal and into any transcript or bug
    report that follows. Logging was hardened against this earlier; the
    exception path was the same leak through a different exit.
    """
    return _KEY_IN_URL.sub(r"\1REDACTED", text)


def _api_error_message(response: Any) -> str:
    """The API's own human-readable message, falling back to raw text."""
    try:
        return redact_key(response.json()["error"]["message"])
    except Exception:
        return redact_key(response.text[:300])


def _get(client: Any, path: str, params: dict[str, Any]) -> dict[str, Any]:
    response = client.get(f"{API_ROOT}/{path}", params=params)

    # Verified against the live API: an invalid key is a 400 ("API key not
    # valid"), while quota exhaustion and a disabled API arrive as 403. All
    # carry a clear message in the error body — relay it instead of a
    # traceback, since a bad key in .env is the most common failure.
    if response.status_code in (400, 403):
        raise SystemExit(
            f"YouTube API refused the request "
            f"({response.status_code}): {_api_error_message(response)}"
        )

    # A 404 from commentThreads means the videoId does not exist. The
    # commonest cause is a placeholder copied out of the README rather than
    # a real id, which is worth saying outright.
    if response.status_code == 404:
        video_id = params.get("videoId")
        detail = f" No video with id {video_id!r}." if video_id else ""
        raise SystemExit(
            f"YouTube API: not found (404).{detail} A video id is the 11-character "
            "string after 'v=' in a watch URL, e.g. dQw4w9WgXcQ."
        )

    # Everything else is handled here rather than by raise_for_status(),
    # whose message embeds the request URL and therefore the API key.
    if response.status_code >= 400:
        raise SystemExit(
            f"YouTube API error ({response.status_code}): "
            f"{_api_error_message(response)}"
        )

    return response.json()


def search_video_ids(
    client: Any, api_key: str, query: str, *, limit: int, quota: QuotaTracker
) -> list[str]:
    """Find video IDs for a query. Costs 100 units per call — use sparingly."""
    payload = _get(
        client,
        "search",
        {
            "part": "id",
            "q": query,
            "type": "video",
            "maxResults": min(limit, 50),
            "key": api_key,
        },
    )
    quota.charge(COST_SEARCH)
    ids = [item["id"]["videoId"] for item in payload.get("items", [])]
    log.info("Search %r found %d videos (%s)", query, len(ids), quota)
    return ids


def iter_video_comments(
    client: Any,
    api_key: str,
    video_id: str,
    *,
    limit: int,
    quota: QuotaTracker,
    sleep: float = DEFAULT_SLEEP,
) -> Iterator[dict[str, Any]]:
    """Yield normalized comments for one video, following pagination.

    Replies arrive inside the same response, so they cost no extra quota.
    """
    page_token: str | None = None
    yielded = 0

    while yielded < limit:
        params = {
            "part": "snippet,replies",
            "videoId": video_id,
            "maxResults": min(100, limit - yielded),
            "textFormat": "plainText",
            "key": api_key,
        }
        if page_token:
            params["pageToken"] = page_token

        payload = _get(client, "commentThreads", params)
        quota.charge(COST_COMMENT_THREADS)

        for thread in payload.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            yield normalize_comment(top["id"], top["snippet"], video_id=video_id)
            yielded += 1

            for reply in thread.get("replies", {}).get("comments", []):
                yield normalize_comment(
                    reply["id"], reply["snippet"], video_id=video_id
                )
                yielded += 1

        page_token = payload.get("nextPageToken")
        if not page_token:
            break
        time.sleep(sleep)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest YouTube comments into the fragrance-graph database.",
        epilog="Searching costs 100 quota units; pulling comments costs 1 per 100.",
    )
    parser.add_argument(
        "--video",
        action="append",
        dest="videos",
        default=[],
        help="Video ID to pull comments from; repeatable",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Search for videos instead of naming them (costs 100 units)",
    )
    parser.add_argument(
        "--max-videos", type=int, default=5, help="Videos to take from a search"
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Max comments per video. Default: 500"
    )
    parser.add_argument(
        "--sleep", type=float, default=DEFAULT_SLEEP, help="Seconds between requests"
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # httpx logs every request URL at INFO, and the API key rides in the
    # query string — silence it so credentials never land in logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if not args.videos and not args.query:
        raise SystemExit("Give at least one --video ID, or a --query to search.")

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    conn = get_connection(args.db_path)
    migrate(conn)
    client, api_key = build_client()
    quota = QuotaTracker()

    video_ids = list(args.videos)
    try:
        if args.query:
            video_ids += search_video_ids(
                client, api_key, args.query, limit=args.max_videos, quota=quota
            )

        total_new = total_seen = 0
        for video_id in video_ids:
            stats = ingest(
                conn,
                iter_video_comments(
                    client,
                    api_key,
                    video_id,
                    limit=args.limit,
                    quota=quota,
                    sleep=args.sleep,
                ),
                source=SOURCE,
            )
            log.info("video %s: %s", video_id, stats)
            total_new += stats.new
            total_seen += stats.seen
    except KeyboardInterrupt:
        log.warning("Stopped early. Re-run to resume; %s", quota)
        return 130
    except Exception as exc:
        # Last line of defence. Several httpx exceptions embed the request
        # URL, and the API key rides in its query string, so an unexpected
        # error would otherwise print the credential into the terminal and
        # every transcript and bug report downstream. Only errors that
        # would actually leak are converted; everything else keeps its
        # traceback.
        message = f"{type(exc).__name__}: {exc}"
        if "key=" not in message:
            raise
        raise SystemExit(redact_key(message)) from None
    finally:
        client.close()
        conn.close()

    log.info("Done. %d seen, %d new across %d videos. %s",
             total_seen, total_new, len(video_ids), quota)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
