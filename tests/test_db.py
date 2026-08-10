"""Connection and migration behaviour."""

import sqlite3

import pytest

from fragrance_graph.db import applied_migrations, get_connection, init_db, migrate


def test_nested_path_creates_its_directory(tmp_path):
    """A fresh clone follows the README straight into this.

    sqlite3 creates the database file but not the directory holding it, and
    the error when the directory is missing — "unable to open database
    file" — names neither the path nor the cause.
    """
    db_path = tmp_path / "data" / "fragrance_graph.db"
    assert not db_path.parent.exists()

    conn = get_connection(db_path)
    conn.close()

    assert db_path.exists()


def test_deeply_nested_path_is_created(tmp_path):
    conn = get_connection(tmp_path / "a" / "b" / "c" / "x.db")
    conn.close()
    assert (tmp_path / "a" / "b" / "c" / "x.db").exists()


def test_bare_filename_still_works(tmp_path, monkeypatch):
    """Path('x.db').parent is '.', which must not be treated as missing."""
    monkeypatch.chdir(tmp_path)
    conn = get_connection("bare.db")
    conn.close()
    assert (tmp_path / "bare.db").exists()


def test_in_memory_database_still_works():
    conn = get_connection(":memory:")
    migrate(conn)
    assert conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 0
    conn.close()


def test_existing_directory_is_not_disturbed(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "keep.txt").write_text("keep")

    conn = get_connection(tmp_path / "data" / "x.db")
    conn.close()

    assert (tmp_path / "data" / "keep.txt").read_text() == "keep"


def test_foreign_keys_are_enforced(tmp_path):
    """Claims reference comments; a dangling one must fail loudly."""
    conn = get_connection(tmp_path / "fk.db")
    migrate(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO claims (comment_id, claim_type, subject_kind, "
            "raw_subject_text, object_kind, raw_object_text, confidence, "
            "evidence_span, extraction_model, created_at) "
            "VALUES (9999, 'DUPE_OF', 'FRAGRANCE', 'x', 'FRAGRANCE', 'y', "
            "0.9, 'x', 'm', 'now')"
        )
    conn.close()


def test_init_is_idempotent(tmp_path):
    db_path = tmp_path / "data" / "init.db"

    first = init_db(db_path)
    second = init_db(db_path)

    assert first, "first run should apply migrations"
    assert second == [], "second run should apply nothing"


def test_migrations_are_recorded(tmp_path):
    conn = get_connection(tmp_path / "m.db")
    migrate(conn)

    recorded = applied_migrations(conn)
    assert "0001_init.sql" in recorded
    assert "0004_source_channel.sql" in recorded
    conn.close()
