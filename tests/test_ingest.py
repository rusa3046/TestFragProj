"""Ingest idempotency and interrupt-resume behaviour.

No live API calls: `ingest` consumes plain dicts, so these drive it with
fixtures directly.
"""

import json
import sqlite3

import pytest

from fragrance_graph.ingest.reddit import ingest, normalize_comment
from tests.conftest import make_comment


def test_ingest_inserts_new_comments(conn, comment_rows):
    stats = ingest(conn, comment_rows)
    assert (stats.new, stats.skipped) == (10, 0)
    assert conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 10


def test_reingest_is_idempotent(conn, comment_rows):
    ingest(conn, comment_rows)
    stats = ingest(conn, comment_rows)

    assert (stats.new, stats.skipped) == (0, 10), "re-ingest must insert nothing"
    assert conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 10


def test_partial_overlap_counts_only_genuinely_new(conn):
    """The resume case: first 5 already stored, next 10 offered."""
    ingest(conn, [make_comment(i) for i in range(5)])
    stats = ingest(conn, [make_comment(i) for i in range(10)])

    assert (stats.new, stats.skipped) == (5, 5)
    assert conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 10


def test_interrupted_ingest_keeps_committed_work(conn):
    """A generator that raises mid-run leaves earlier batches durable."""

    def exploding_rows():
        for i in range(10):
            yield make_comment(i)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ingest(conn, exploding_rows(), commit_every=5)

    stored = conn.execute("SELECT count(*) FROM comments").fetchone()[0]
    assert stored == 10, "work before the interrupt must survive"

    # ...and resuming adds only what's actually missing.
    stats = ingest(conn, [make_comment(i) for i in range(12)])
    assert (stats.new, stats.skipped) == (2, 10)


def test_source_id_uniqueness_is_enforced_by_the_database(conn):
    """Idempotency rests on the constraint, not just the INSERT clause."""
    ingest(conn, [make_comment(1)])
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO comments (source, source_id, body, permalink,"
            " created_utc, source_channel, score) VALUES"
            " ('reddit', 't1_fake00001', 'b', 'p', 1, 'fragrance', 0)"
        )


def test_raw_payload_is_retained(conn):
    ingest(conn, [make_comment(42)])
    raw = conn.execute(
        "SELECT raw_json FROM comments WHERE source_id = 't1_fake00042'"
    ).fetchone()[0]
    assert json.loads(raw)["unused_field"] == "kept anyway"


def test_same_source_id_across_sources_is_not_a_collision(conn):
    """Uniqueness is (source, source_id), so another source may reuse an id."""
    ingest(conn, [make_comment(1)], source="reddit")
    stats = ingest(conn, [make_comment(1)], source="hackernews")
    assert stats.new == 1


def test_extracted_at_starts_null(conn, comment_rows):
    """Extraction eligibility depends on this being unset at ingest."""
    ingest(conn, comment_rows)
    pending = conn.execute(
        "SELECT count(*) FROM comments WHERE extracted_at IS NULL"
    ).fetchone()[0]
    assert pending == 10


class FakePrawComment:
    """Minimal stand-in for a PRAW Comment, including junk to be retained."""

    def __init__(self):
        self.fullname = "t1_kd8fj2q"
        self.body = "Delina smells just like Baccarat 540"
        self.permalink = "/r/fragrance/comments/abc/_/kd8fj2q/"
        self.created_utc = 1700000000.7
        self.subreddit = "fragrance"
        self.score = 42
        self.total_awards_received = 3
        self._reddit = object()  # private: must not reach raw_json


def test_normalize_comment_shapes_praw_object():
    row = normalize_comment(FakePrawComment())

    assert row["source_id"] == "t1_kd8fj2q"
    assert row["permalink"].startswith("https://www.reddit.com/")
    assert row["created_utc"] == 1700000000, "float timestamp truncated to int"

    raw = json.loads(row["raw_json"])
    assert raw["total_awards_received"] == 3, "unused fields retained for later"
    assert not any(k.startswith("_") for k in raw), "private attrs excluded"
