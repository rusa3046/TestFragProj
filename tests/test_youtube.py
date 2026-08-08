"""YouTube ingest: normalization, pagination, quota accounting.

No live API calls — a fake transport plays the API's role.
"""

import json

import pytest

from fragrance_graph.ingest.reddit import ingest
from fragrance_graph.ingest.youtube import (
    COST_COMMENT_THREADS,
    DAILY_QUOTA,
    SOURCE,
    QuotaTracker,
    iter_video_comments,
    normalize_comment,
    parse_published_at,
    search_video_ids,
)


def yt_snippet(**overrides):
    base = {
        "textOriginal": "This smells just like Aventus",
        "textDisplay": "This smells just like Aventus",
        "publishedAt": "2026-08-01T12:00:00Z",
        "channelId": "UCabc123",
        "likeCount": 7,
    }
    return {**base, **overrides}


def thread(comment_id="Ugxabc", snippet=None, replies=()):
    item = {
        "snippet": {
            "topLevelComment": {"id": comment_id, "snippet": snippet or yt_snippet()}
        }
    }
    if replies:
        item["replies"] = {
            "comments": [
                {"id": f"{comment_id}.r{i}", "snippet": s} for i, s in enumerate(replies)
            ]
        }
    return item


class FakeHttp:
    """Returns queued JSON payloads; records requested params."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.params_seen = []

    def get(self, url, params=None):
        self.params_seen.append(params)
        payload = self._payloads.pop(0)

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        return R()


# --- normalization ----------------------------------------------------------


def test_normalize_maps_to_comment_columns():
    row = normalize_comment("Ugx1", yt_snippet(), video_id="vid42")

    assert row["source_id"] == "Ugx1"
    assert row["body"] == "This smells just like Aventus"
    assert row["permalink"] == "https://www.youtube.com/watch?v=vid42&lc=Ugx1"
    assert row["source_channel"] == "UCabc123"
    assert row["score"] == 7
    assert row["created_utc"] == parse_published_at("2026-08-01T12:00:00Z")


def test_normalize_prefers_text_original():
    """textDisplay carries HTML entities; textOriginal is the raw text."""
    row = normalize_comment(
        "Ugx1",
        yt_snippet(textOriginal="a & b", textDisplay="a &amp; b"),
        video_id="v",
    )
    assert row["body"] == "a & b"


def test_normalize_retains_full_snippet_in_raw_json():
    row = normalize_comment("Ugx1", yt_snippet(likeCount=3), video_id="v")
    assert json.loads(row["raw_json"])["likeCount"] == 3


def test_published_at_is_unix_utc():
    assert parse_published_at("1970-01-01T00:00:00Z") == 0


# --- pagination and quota ---------------------------------------------------


def test_replies_are_yielded_and_cost_no_extra_quota():
    http = FakeHttp([{"items": [thread(replies=[yt_snippet(), yt_snippet()])]}])
    quota = QuotaTracker()

    rows = list(iter_video_comments(http, "key", "vid", limit=10, quota=quota))

    assert len(rows) == 3  # top-level + 2 replies
    assert quota.units == COST_COMMENT_THREADS


def test_pagination_follows_next_page_token():
    http = FakeHttp(
        [
            {"items": [thread("Ugx1")], "nextPageToken": "page2"},
            {"items": [thread("Ugx2")]},
        ]
    )
    quota = QuotaTracker()

    rows = list(
        iter_video_comments(http, "key", "vid", limit=10, quota=quota, sleep=0)
    )

    assert [r["source_id"] for r in rows] == ["Ugx1", "Ugx2"]
    assert http.params_seen[1]["pageToken"] == "page2"
    assert quota.units == 2 * COST_COMMENT_THREADS


def test_limit_stops_pagination_early():
    http = FakeHttp(
        [{"items": [thread(f"Ugx{i}") for i in range(3)], "nextPageToken": "more"}]
    )
    rows = list(
        iter_video_comments(http, "key", "vid", limit=3, quota=QuotaTracker(), sleep=0)
    )
    assert len(rows) == 3
    assert len(http.params_seen) == 1, "limit reached; next page must not be fetched"


def test_search_charges_the_expensive_rate():
    http = FakeHttp([{"items": [{"id": {"videoId": "v1"}}, {"id": {"videoId": "v2"}}]}])
    quota = QuotaTracker()

    ids = search_video_ids(http, "key", "aventus review", limit=5, quota=quota)

    assert ids == ["v1", "v2"]
    assert quota.units == 100
    assert quota.remaining == DAILY_QUOTA - 100


def test_quota_tracker_never_reports_negative_remaining():
    quota = QuotaTracker()
    quota.charge(DAILY_QUOTA + 500)
    assert quota.remaining == 0


# --- integration with the shared write path ---------------------------------


def test_youtube_rows_ingest_alongside_reddit(conn):
    """Both sources share one table; (source, source_id) keeps them apart."""
    yt = normalize_comment("SAME_ID", yt_snippet(), video_id="v")
    reddit_row = dict(yt, permalink="https://reddit/x")

    assert ingest(conn, [yt], source=SOURCE).new == 1
    assert ingest(conn, [reddit_row], source="reddit").new == 1

    sources = sorted(
        r[0] for r in conn.execute("SELECT DISTINCT source FROM comments")
    )
    assert sources == ["reddit", "youtube"]


def test_youtube_rows_are_pending_extraction(conn):
    from fragrance_graph.extract.llm import pending_comments

    ingest(conn, [normalize_comment("Ugx1", yt_snippet(), video_id="v")], source=SOURCE)
    assert len(pending_comments(conn, 10)) == 1


def test_missing_key_exits_with_instructions(monkeypatch):
    from fragrance_graph.ingest.youtube import build_client

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="YOUTUBE_API_KEY"):
        build_client()
