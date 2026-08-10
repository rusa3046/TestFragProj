"""Seeding hand-collected text for local testing."""

from pathlib import Path

from fragrance_graph.ingest.reddit import ingest
from fragrance_graph.ingest.seed import SOURCE, make_row, parse_entries


def entries_of(text: str) -> list[str]:
    """parse_entries returns (entries, stripped); most tests want entries."""
    return parse_entries(text)[0]


def test_splits_on_separator_lines():
    assert entries_of("first comment\n---\nsecond comment") == [
        "first comment",
        "second comment",
    ]


def test_multi_paragraph_entries_survive():
    assert entries_of("para one\n\npara two\n---\nnext")[0] == "para one\n\npara two"


def test_blank_entries_are_dropped():
    assert entries_of("a\n---\n\n---\n   \n---\nb") == ["a", "b"]


def test_single_entry_with_no_separator():
    assert entries_of("just one") == ["just one"]


def test_empty_file_yields_nothing():
    assert entries_of("") == []


# --- file comments ----------------------------------------------------------


def test_comment_lines_are_stripped_from_bodies():
    """The defect: the file's own header was stored as comment text."""
    entries = entries_of("# instructions\n# more instructions\n\nreal text")
    assert entries == ["real text"]


def test_stripped_lines_are_returned_for_reporting():
    _, stripped = parse_entries("# one\nreal\n# two")
    assert stripped == ["# one", "# two"]


def test_comments_between_entries_do_not_merge_them():
    entries = entries_of("first\n---\n# a note about the next one\nsecond")
    assert entries == ["first", "second"]


def test_an_entry_of_only_comments_is_dropped():
    assert entries_of("# nothing but chrome\n---\nreal") == ["real"]


def test_indenting_preserves_a_leading_hash():
    """The documented escape hatch for real content starting with '#'."""
    entries = entries_of(" #1 of the year for me")
    assert entries == ["#1 of the year for me"]


def test_a_hash_mid_line_is_never_touched():
    """Hashtags and rankings inside a line are ordinary content."""
    entries = entries_of("this is my #1 pick #nichefragrance")
    assert entries == ["this is my #1 pick #nichefragrance"]


def test_the_shipped_example_file_yields_only_its_three_comments():
    """Regression on the exact file that exposed the defect."""
    text = Path("seed/example.txt").read_text()
    entries, stripped = parse_entries(text)

    assert len(entries) == 3
    assert len(stripped) == 3
    assert entries[0].startswith("Delina smells just like Baccarat 540")
    assert not any(e.startswith("#") for e in entries)


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

    row = make_row("x", subreddit="fragrance", note="from r/fragrance 2026-08")
    ingest(conn, [row], source=SOURCE)
    raw = conn.execute("SELECT raw_json FROM comments").fetchone()[0]
    assert json.loads(raw)["note"] == "from r/fragrance 2026-08"
