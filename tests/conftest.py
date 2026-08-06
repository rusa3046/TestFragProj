import json

import pytest

from fragrance_graph.db import get_connection, migrate


@pytest.fixture
def conn(tmp_path):
    """A migrated, empty database scoped to one test."""
    connection = get_connection(tmp_path / "test.db")
    migrate(connection)
    yield connection
    connection.close()


def make_comment(i: int, **overrides):
    """A normalized comment row as `ingest` expects it."""
    row = {
        "source_id": f"t1_fake{i:05d}",
        "body": f"comment number {i}",
        "permalink": f"https://www.reddit.com/r/fragrance/comments/x/_/{i}",
        "created_utc": 1700000000 + i,
        "subreddit": "fragrance",
        "score": i,
        "raw_json": json.dumps({"id": f"fake{i:05d}", "unused_field": "kept anyway"}),
    }
    row.update(overrides)
    return row


@pytest.fixture
def comment_rows():
    return [make_comment(i) for i in range(10)]
