"""Connection and migration behaviour.

Most of this file used to be about SQLite creating a database file but not
the directory holding it. That failure cannot happen against a server, and
the tests that pinned it are gone rather than translated — a test kept for
its history rather than its subject is a test nobody reads.

What replaces them is the behaviour a server introduces: migrations that
must be recorded exactly once, constraints that must be enforced by the
database rather than by the code above it, and the one statement the
runner has to treat differently because Postgres will not run it inside a
transaction.
"""

from pathlib import Path

import psycopg
import pytest

from fragrance_graph.db import (
    NON_TRANSACTIONAL,
    _split_statements,
    applied_migrations,
    get_connection,
    init_db,
    migrate,
)
from fragrance_graph.resolve.entities import add_fragrance


def test_migrations_are_recorded(conn):
    recorded = applied_migrations(conn)
    assert "0001_init.sql" in recorded
    assert "0004_source_channel.sql" in recorded


def test_migrating_an_already_migrated_database_does_nothing(conn):
    assert migrate(conn) == []


def test_init_is_idempotent(_schema):
    """`init` is what a fresh container runs, and what a person re-runs
    when they are not sure whether they already did."""
    assert init_db(_schema) == []


def test_a_bad_dsn_fails_loudly(tmp_path):
    """The old failure mode was silence: a stale path created an empty
    SQLite file and the loop ran against it for a whole scheduled run."""
    with pytest.raises((psycopg.OperationalError, psycopg.ProgrammingError)):
        get_connection(str(tmp_path / "not-a-dsn.db"))


def test_foreign_keys_are_enforced(conn):
    """Claims reference comments; a dangling one must fail loudly.

    SQLite only enforced this because `get_connection` remembered to say
    `PRAGMA foreign_keys = ON` — an opt-in that any other entry point
    could have forgotten. Postgres has no such switch.
    """
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO claims (comment_id, claim_type, subject_kind, "
            "raw_subject_text, object_kind, raw_object_text, confidence, "
            "evidence_span, extraction_model, created_at) "
            "VALUES (9999, 'DUPE_OF', 'FRAGRANCE', 'x', 'FRAGRANCE', 'y', "
            "0.9, 'x', 'm', 'now')"
        )


def test_canonical_name_is_unique(conn):
    """One row per bottle, enforced where no code path can bypass it.

    Auto-curation wrote the same bottle twice on 2026-08-11 and nothing
    stopped it. The application guards catch the cases they know about;
    this catches the rest.
    """
    add_fragrance(conn, "Armaf Club de Nuit Intense Man", brand="Armaf")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_fragrance(conn, "Armaf Club de Nuit Intense Man", brand="Armaf")


def test_the_uniqueness_ignores_case(conn):
    """"Armaf Club De Nuit" and "Armaf Club de Nuit" are one bottle.

    SQLite spelled this `COLLATE NOCASE` on the column. Postgres has no
    case-insensitive collation by default, so migration 0009 indexes
    `lower(canonical_name)` instead — same rule, different mechanism, and
    this test is what says the mechanism still implements the rule.
    """
    add_fragrance(conn, "Armaf Club De Nuit", brand="Armaf")
    with pytest.raises(psycopg.errors.UniqueViolation):
        add_fragrance(conn, "armaf club de nuit", brand="Armaf")


def test_distinct_flankers_are_still_allowed(conn):
    """The constraint must not swallow real siblings."""
    add_fragrance(conn, "Parfums de Marly Layton", brand="Parfums de Marly")
    add_fragrance(conn, "Parfums de Marly Layton Exclusif",
                  brand="Parfums de Marly")
    assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 2


class TestNonTransactionalMigrations:
    """`CREATE INDEX CONCURRENTLY` cannot run inside a transaction.

    It is also the only way to add an index to a live table without
    blocking writes — so the safest form of the most common migration
    there is, is the one a naive runner cannot express. This is the part
    of the port that is a real behaviour rather than a translation.
    """

    def test_a_concurrent_index_is_recognised(self):
        assert NON_TRANSACTIONAL.search("CREATE INDEX CONCURRENTLY x ON y (z)")
        assert NON_TRANSACTIONAL.search("create unique index concurrently ...")

    def test_an_ordinary_migration_is_not(self):
        assert not NON_TRANSACTIONAL.search("CREATE INDEX x ON y (z)")
        assert not NON_TRANSACTIONAL.search("ALTER TABLE t ADD COLUMN c TEXT")

    def test_statements_split_without_breaking_string_literals(self):
        """A semicolon inside a quoted default is not a statement end."""
        sql = "ALTER TABLE t ADD COLUMN c TEXT DEFAULT 'a;b'; CREATE INDEX i ON t (c);"
        assert _split_statements(sql) == [
            "ALTER TABLE t ADD COLUMN c TEXT DEFAULT 'a;b'",
            "CREATE INDEX i ON t (c)",
        ]

    def test_a_concurrent_index_actually_builds(self, conn):
        """The path exists because a transaction-wrapped one errors out."""
        conn.commit()
        with pytest.raises(psycopg.errors.ActiveSqlTransaction):
            conn.execute("BEGIN")
            conn.execute(
                "CREATE INDEX CONCURRENTLY idx_probe ON comments (created_utc)"
            )
        conn.rollback()

        # Autocommit on this connection, not a second one from
        # `conn.info.dsn` — libpq leaves the password out of `dsn`, so
        # that reconnect only works where no password is asked for.
        conn.autocommit = True
        try:
            conn.execute(
                "CREATE INDEX CONCURRENTLY idx_probe ON comments (created_utc)"
            )
            conn.execute("DROP INDEX idx_probe")
        finally:
            conn.autocommit = False


class TestTheFirstErrorAnybodyMeets:
    """A stopped server is the first failure after a fresh clone, and
    libpq's message for it names neither Postgres nor what to do."""

    def test_a_stopped_server_says_how_to_start_one(self):
        from fragrance_graph.db import NoServer, get_connection

        # Port 1 is reserved and nothing listens on it.
        with pytest.raises(NoServer) as caught:
            get_connection("postgresql://postgres@localhost:1/whatever")
        message = str(caught.value)
        assert "brew services start" in message
        assert "FRAGRANCE_DB_URL" in message

    def test_it_is_still_an_operational_error(self):
        """Callers that already catch psycopg.OperationalError keep working."""
        from fragrance_graph.db import NoServer

        assert issubclass(NoServer, psycopg.OperationalError)

    def test_other_connection_failures_are_not_disguised(self, _schema):
        """A wrong password or a missing database is a different problem
        and must keep its own message."""
        from fragrance_graph.db import NoServer, get_connection

        missing = _schema.replace("fragrance_graph_test", "definitely_not_here")
        with pytest.raises(psycopg.OperationalError) as caught:
            get_connection(missing)
        assert not isinstance(caught.value, NoServer)
        assert "definitely_not_here" in str(caught.value)


class TestDotenvIsReadBeforeTheDefault:
    """`DEFAULT_DB_URL` is evaluated at import, and several entry points
    called `load_dotenv()` inside `main()` — after it. A FRAGRANCE_DB_URL
    line in .env therefore looked configured and did nothing."""

    def test_the_env_file_is_loaded_at_import(self, tmp_path, monkeypatch):
        import subprocess
        import sys

        (tmp_path / ".env").write_text(
            "FRAGRANCE_DB_URL=postgresql://from-the-env-file/db\n"
        )
        result = subprocess.run(
            [sys.executable, "-c",
             "from fragrance_graph.db import DEFAULT_DB_URL; print(DEFAULT_DB_URL)"],
            cwd=tmp_path, capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin",
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        assert "from-the-env-file" in result.stdout, result.stderr

    def test_an_explicit_variable_still_wins(self, tmp_path):
        import subprocess
        import sys

        (tmp_path / ".env").write_text(
            "FRAGRANCE_DB_URL=postgresql://from-the-env-file/db\n"
        )
        result = subprocess.run(
            [sys.executable, "-c",
             "from fragrance_graph.db import DEFAULT_DB_URL; print(DEFAULT_DB_URL)"],
            cwd=tmp_path, capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin",
                 "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                 "FRAGRANCE_DB_URL": "postgresql://from-the-shell/db"},
        )
        assert "from-the-shell" in result.stdout, result.stderr
