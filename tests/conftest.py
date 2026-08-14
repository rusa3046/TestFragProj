"""Shared fixtures.

## The database fixture, and why it is not one database per test

Under SQLite each test got its own file, which cost nothing. A Postgres
database costs about a tenth of a second to create, and 800 of them is
two minutes of waiting before the first assertion — the kind of tax that
quietly stops people running the suite.

So the schema is migrated once per session, and each test starts from
`TRUNCATE ... RESTART IDENTITY CASCADE`. That is fast, and `RESTART
IDENTITY` matters as much as the truncation: several tests assert on ids,
and a sequence that kept counting across tests would make them pass or
fail depending on what ran before them.

`FRAGRANCE_TEST_DB_URL` points at the server. It is deliberately separate
from `FRAGRANCE_DB_URL`: pointing the suite at your working database
would truncate it between every test, and the corpus would be the only
reason that was survivable rather than a disaster.
"""

import json
import os

import psycopg
import pytest

from fragrance_graph.db import get_connection, migrate

TEST_DB_URL = os.environ.get(
    "FRAGRANCE_TEST_DB_URL",
    "postgresql://postgres@localhost:5432/fragrance_graph_test",
)


@pytest.fixture(scope="session")
def _schema():
    """Migrate once per session, so 800 tests do not run 800 migrations."""
    try:
        connection = get_connection(TEST_DB_URL)
    except psycopg.OperationalError as exc:  # pragma: no cover - setup aid
        pytest.exit(
            f"Cannot reach the test database at {TEST_DB_URL}\n\n{exc}\n"
            "The suite needs a running PostgreSQL. See README > Setup.",
            returncode=1,
        )
    # Rebuilt from nothing each session rather than migrated onto whatever
    # is there. `schema_migrations` records 0008 as applied, so a table
    # dropped by a test that committed its DDL would never come back — a
    # whole session failing for a reason that is not in any diff. One
    # DROP SCHEMA costs a fifth of a second and removes the class.
    connection.execute("DROP SCHEMA public CASCADE")
    connection.execute("CREATE SCHEMA public")
    connection.commit()
    migrate(connection)
    connection.close()
    return TEST_DB_URL


@pytest.fixture
def conn(_schema):
    """A migrated, empty database on a connection of this test's own.

    A connection per test rather than a shared one, because a good number
    of these tests call a module's `main()`, and every `main()` closes the
    connection it was given in a `finally`. Sharing one meant the first
    CLI test poisoned every test after it with "the connection is
    closed" — 280 errors that had nothing to do with the code under test.
    """
    connection = get_connection(_schema)
    tables = [
        row["tablename"]
        for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            "   AND tablename <> 'schema_migrations'"
        )
    ]
    if tables:
        connection.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
    connection.commit()
    yield connection
    if not connection.closed:
        connection.close()


@pytest.fixture
def db_url(_schema):
    """The DSN of the test database, password and all.

    Tests that exercise a CLI need something to pass to `--db-url`, and
    `conn.info.dsn` is not it: libpq omits the password from `dsn` by
    design, so a DSN taken from a live connection reconnects only against
    a server that does not ask for one. That is every laptop and no
    deployment — 15 tests passed locally and failed on the first CI run.
    """
    return _schema


@pytest.fixture
def second_db(_schema):
    """A second, empty database — "rebuild this corpus somewhere fresh".

    Several tests prove that an export reconstructs a database it did not
    come from. Under SQLite that was another file in tmp_path; here it is
    another database on the same server, created and dropped around the
    test. Created from a connection to `postgres`, because CREATE DATABASE
    cannot run from inside the database being created and cannot run in a
    transaction either.
    """
    name = "fragrance_graph_second"
    admin_url = TEST_DB_URL.replace("/fragrance_graph_test", "/postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name}")
        admin.execute(f"CREATE DATABASE {name}")

    url = TEST_DB_URL.replace("/fragrance_graph_test", f"/{name}")
    connection = get_connection(url)
    migrate(connection)
    connection.close()
    yield url
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture
def scale_db(_schema):
    """An empty database named `*_scale`, for the synthetic-row tests.

    The name is the guard, so the fixture has to produce a real one — a
    test that asserted the refusal against a mock would be testing the
    mock.
    """
    name = "fragrance_graph_probe_scale"
    admin_url = TEST_DB_URL.replace("/fragrance_graph_test", "/postgres")
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {name}")

    url = TEST_DB_URL.replace("/fragrance_graph_test", f"/{name}")
    connection = get_connection(url)
    migrate(connection)
    connection.close()
    yield url
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def make_comment(i: int, **overrides):
    """A normalized comment row as `ingest` expects it."""
    row = {
        "source_id": f"t1_fake{i:05d}",
        "body": f"comment number {i}",
        "permalink": f"https://www.reddit.com/r/fragrance/comments/x/_/{i}",
        "created_utc": 1700000000 + i,
        "source_channel": "fragrance",
        "score": i,
        "raw_json": json.dumps({"id": f"fake{i:05d}", "unused_field": "kept anyway"}),
    }
    row.update(overrides)
    return row


@pytest.fixture
def comment_rows():
    return [make_comment(i) for i in range(10)]
