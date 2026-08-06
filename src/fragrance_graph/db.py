"""SQLite connection and migration runner for fragrance-graph.

Migrations live as plain .sql files in migrations/, applied in filename
order. Applied migrations are tracked in a schema_migrations table so
re-running init is a no-op.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

DEFAULT_DB_PATH = os.environ.get("FRAGRANCE_DB_PATH", "fragrance_graph.db")


def get_connection(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def applied_migrations(conn: sqlite3.Connection) -> set[str]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row["filename"] for row in rows}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply any pending .sql migrations. Returns filenames applied."""
    _ensure_migrations_table(conn)
    already = applied_migrations(conn)
    applied = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in already:
            continue
        sql = path.read_text()
        conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (?)", (path.name,)
        )
        conn.commit()
        applied.append(path.name)
    return applied


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> list[str]:
    """Create or update the database at db_path. Returns migrations applied."""
    conn = get_connection(db_path)
    try:
        applied = migrate(conn)
    finally:
        conn.close()
    return applied


def main() -> None:
    parser = argparse.ArgumentParser(description="fragrance-graph database CLI")
    parser.add_argument("command", choices=["init"])
    parser.add_argument(
        "--db-path", default=DEFAULT_DB_PATH, help="Path to SQLite database file"
    )
    args = parser.parse_args()

    if args.command == "init":
        applied = init_db(args.db_path)
        if applied:
            print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
        else:
            print("Database already up to date.")


if __name__ == "__main__":
    main()
