"""PostgreSQL connection and migration runner for fragrance-graph.

Migrations live as plain .sql files in migrations/, applied in filename
order. Applied migrations are tracked in a schema_migrations table so
re-running init is a no-op.

## Why Postgres, when SQLite was enough

It was enough. The database here is **derived and disposable** — the
corpus in `data/corpus/*.jsonl` is the source of truth, and every
scheduled run rebuilds the database from it in a fresh container. Nothing
about six thousand comments needs a server.

What a server buys is the operational surface: query plans worth reading,
autovacuum, locks, replication, and schema changes that have to happen
while something is running. Those are learnable only against a real
engine, and this repo is an unusually safe place to learn them precisely
because the database is disposable. Drop it, bloat it, lock it up; the
corpus is untouched and `corpus import` puts it back.

## Connections are DSNs, not paths

`FRAGRANCE_DB_URL`, or `--db-url` on every CLI. The old `FRAGRANCE_DB_PATH`
is gone rather than aliased: a stale path silently created an empty SQLite
file once, and the loop ran against it for a full scheduled run. A wrong
DSN fails loudly, which is the behaviour worth keeping.

## Transactions, and the one statement that cannot have one

`CREATE INDEX CONCURRENTLY` — the only way to add an index to a live table
without blocking writes — **cannot run inside a transaction block**. A
migration runner that wraps every file in BEGIN/COMMIT therefore cannot
express the safest form of the most common migration there is, and fails
with a message about the transaction rather than about the index.

So a file containing CONCURRENTLY is run with autocommit on, statement by
statement, and is not atomic. That is the real trade and it is stated
here rather than discovered: a CONCURRENTLY build that fails leaves an
INVALID index behind, which has to be dropped by hand before retrying.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _load_env() -> None:
    """Read `.env` before anything reads the environment.

    Every entry point in this project imports this module, and several of
    them do their own `load_dotenv()` inside `main()` — which is too late:
    `DEFAULT_DB_URL` below is evaluated at import, so a `.env` loaded
    afterwards has no effect on it. The result was a `FRAGRANCE_DB_URL`
    line in `.env` that looked configured and was silently ignored, and a
    person prefixing every single command with the variable instead.

    `load_dotenv` does not override variables already in the environment,
    so an explicit `FRAGRANCE_DB_URL=... uv run ...` still wins.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a dependency
        return
    load_dotenv()


_load_env()

#: Default connection string. Deliberately a full DSN rather than a
#: hostname: a reader who has never seen libpq should be able to copy this
#: and change one word.
DEFAULT_DB_URL = os.environ.get(
    "FRAGRANCE_DB_URL", "postgresql://postgres@localhost:5432/fragrance_graph"
)

#: Statements that cannot run inside a transaction block. Postgres rejects
#: them with "cannot run inside a transaction block", which reads like a
#: bug in the runner rather than a property of the statement.
NON_TRANSACTIONAL = re.compile(r"\bCONCURRENTLY\b", re.IGNORECASE)


class Row(dict):
    """A result row readable by name *or* position, like `sqlite3.Row`.

    The codebase reads columns by name almost everywhere, which is the
    habit worth keeping when a query grows a join. It also has a handful
    of `fetchone()[0]` for single-value queries, where naming the column
    would be noise.

    `dict_row` alone breaks the second form with a KeyError on 0. Rather
    than rewrite those call sites into something wordier, this keeps both
    — dicts preserve column order, so position means what it meant.
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def row_factory(cursor):
    columns = [c.name for c in cursor.description or ()]

    def make(values):
        return Row(zip(columns, values, strict=False))

    return make


class NoServer(psycopg.OperationalError):
    """The server is not running, or is not where the DSN says it is.

    A separate type so the message can say what to do. libpq's own is
    accurate and unhelpful: it reports "Connection refused" for both
    127.0.0.1 and ::1 across nine lines, and mentions neither Postgres
    being stopped nor which database was being asked for — which is the
    first error anybody meets after a fresh clone.
    """


def get_connection(db_url: str = DEFAULT_DB_URL) -> psycopg.Connection:
    """Open a connection whose rows behave like `sqlite3.Row`."""
    try:
        return psycopg.connect(db_url, row_factory=row_factory)
    except psycopg.OperationalError as exc:
        if "refused" not in str(exc) and "starting up" not in str(exc):
            raise
        raise NoServer(
            f"No PostgreSQL server answered at {db_url!r}.\n"
            "  Start one:   brew services start postgresql@16\n"
            "               (or: docker start fragrance-pg)\n"
            "  Then check:  pg_isready\n"
            "  The DSN comes from FRAGRANCE_DB_URL in .env, or --db-url.\n"
            "  See README > Setup."
        ) from exc


def _ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def applied_migrations(conn: psycopg.Connection) -> set[str]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row["filename"] for row in rows}


def _split_statements(sql: str) -> list[str]:
    """Split a migration into statements, ignoring semicolons in strings.

    Only needed for the non-transactional path, where each statement is
    sent on its own. Deliberately simple: these are our own files, and
    the alternative — a SQL parser — would be more code than the runner.
    """
    statements, current, in_string = [], [], False
    for char in sql:
        if char == "'":
            in_string = not in_string
        if char == ";" and not in_string:
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
    statements.append("".join(current))
    return [s.strip() for s in statements if s.strip()]


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply any pending .sql migrations. Returns filenames applied."""
    _ensure_migrations_table(conn)
    already = applied_migrations(conn)
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already:
            continue
        sql = path.read_text()

        if NON_TRANSACTIONAL.search(sql):
            # Not atomic, and it cannot be. See the module docstring.
            conn.commit()
            with psycopg.connect(conn.info.dsn, autocommit=True) as side:
                for statement in _split_statements(sql):
                    side.execute(statement)
        else:
            conn.execute(sql)

        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
        )
        conn.commit()
        applied.append(path.name)
    return applied


def init_db(db_url: str = DEFAULT_DB_URL) -> list[str]:
    """Create or update the schema at `db_url`. Returns migrations applied.

    The database itself must exist. Creating it is a `CREATE DATABASE`,
    which needs a connection to some *other* database and is a decision
    about where data lives — see README for the one-line setup.
    """
    conn = get_connection(db_url)
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description="fragrance-graph database CLI")
    parser.add_argument("command", choices=["init"])
    parser.add_argument(
        "--db-url", default=DEFAULT_DB_URL, help="PostgreSQL connection string"
    )
    args = parser.parse_args()

    if args.command == "init":
        applied = init_db(args.db_url)
        if applied:
            print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
        else:
            print("Database already up to date.")


if __name__ == "__main__":
    main()
