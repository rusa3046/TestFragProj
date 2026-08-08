"""Seeding hand-collected text for local testing."""

from fragrance_graph.ingest.reddit import ingest
from fragrance_graph.ingest.seed import SOURCE, make_row, parse_entries


def test_splits_on_separator_lines():
    entries = parse_entries("first comment\n---\nsecond comment")
    assert entries == ["first comment", "second comment"]


def test_multi_paragraph_entries_survive():
    entries = parse_entries("para one\n\npara two\n---\nnext")
    assert entries[0] == "para one\n\npara two"


def test_blank_entries_are_dropped():
    assert parse_entries("a\n---\n\n---\n   \n---\nb") == ["a", "b"]


def test_single_entry_with_no_separator():
    assert parse_entries("just one") == ["just one"]


def test_empty_file_yields_nothing():
    assert parse_entries("") == []


def test_same_text_produces_the_same_id():
    """Content-hash ids make re-seeding a file idempotent for free."""
    assert make_row("x", subreddit="f")["source_id"] == make_row("x", subreddit="f")["source_id"]


def test_different_text_produces_different_ids():
    a = make_row("one", subreddit="f")["source_id"]
    b = make_row("two", subreddit="f")["source_id"]
    assert a != b


def test_reseeding_the_same_file_inserts_nothing_new(conn):
    rows = [make_row(t, subreddit="fragrance") for t in ("alpha", "beta")]
    assert ingest(conn, rows, source=SOURCE).new == 2
    assert ingest(conn, rows, source=SOURCE).new == 0


def test_seeded_rows_are_distinguishable_from_reddit(conn):
    """Test data must never be mistaken for real ingest."""
    ingest(conn, [make_row("seeded", subreddit="fragrance")], source=SOURCE)
    sources = [r[0] for r in conn.execute("SELECT DISTINCT source FROM comments")]
    assert sources == ["manual"]


def test_seeded_rows_are_pending_extraction(conn):
    from fragrance_graph.extract.llm import pending_comments

    ingest(conn, [make_row("hello", subreddit="fragrance")], source=SOURCE)
    assert len(pending_comments(conn, 10)) == 1


def test_provenance_note_is_retained(conn):
    import json

    ingest(conn, [make_row("x", subreddit="fragrance", note="from r/fragrance 2026-08")], source=SOURCE)
    raw = conn.execute("SELECT raw_json FROM comments").fetchone()[0]
    assert json.loads(raw)["note"] == "from r/fragrance 2026-08"
