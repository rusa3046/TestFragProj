"""Synthetic rows, and the guards that keep them out of the corpus.

`scripts/scale.py` fabricates millions of rows so there is a database big
enough to have query plans worth reading. Every claim this project makes
rests on no row being invented, so the interesting tests here are not that
the loader works — they are the three that say where it cannot reach.
"""

import json

import psycopg
import pytest

from fragrance_graph.corpus import ScaleDatabase, export_corpus, is_scale_database
from fragrance_graph.db import get_connection
from fragrance_graph.extract.llm import write_claims
from fragrance_graph.ingest.store import ingest
from fragrance_graph.models import Claim
from scripts.scale import (
    SYNTHETIC_PREFIX,
    RefusedTarget,
    database_name,
    load,
)
from tests.conftest import make_comment


def seed_source(conn, n=3):
    """A small real corpus for the loader to resample from."""
    for i in range(n):
        body = f"comment {i} is a dupe of aventus"
        ingest(conn, [make_comment(i, body=body)])
        comment_id = conn.execute(
            "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
        ).fetchone()[0]
        write_claims(conn, comment_id, body, [
            Claim(
                claim_type="DUPE_OF",
                subject_kind="FRAGRANCE",
                raw_subject_text=f"comment {i}",
                object_kind="FRAGRANCE",
                raw_object_text="aventus",
                confidence=0.9,
                evidence_span=body,
            )
        ])
    conn.commit()
    return conn


class TestItCannotReachTheCorpus:
    """The three guards, each tested against a real database rather than
    a mock — the guard *is* the database's name, so a mock would be
    testing itself."""

    def test_it_refuses_a_database_not_named_scale(self, conn):
        seed_source(conn)
        before = conn.execute("SELECT count(*) FROM comments").fetchone()[0]

        with pytest.raises(RefusedTarget, match="not a scale database"):
            load(conn, conn, comments=10)

        assert conn.execute(
            "SELECT count(*) FROM comments"
        ).fetchone()[0] == before, "nothing was written"

    def test_export_refuses_a_scale_database(self, scale_db, tmp_path):
        """The only path from a database into the committed corpus."""
        connection = get_connection(scale_db)
        try:
            with pytest.raises(ScaleDatabase, match="fabricated"):
                export_corpus(connection, tmp_path)
        finally:
            connection.close()
        assert list(tmp_path.iterdir()) == [], "not one line was written"

    def test_export_refuses_it_with_force_too(self, scale_db, tmp_path):
        """`force` exists to overrule the shrink guard, which is a
        judgement call. This is not one."""
        connection = get_connection(scale_db)
        try:
            with pytest.raises(ScaleDatabase):
                export_corpus(connection, tmp_path, force=True)
        finally:
            connection.close()

    def test_export_is_happy_with_an_ordinary_database(self, conn, tmp_path):
        seed_source(conn)
        export_corpus(conn, tmp_path)
        assert (tmp_path / "comments.jsonl").exists()

    def test_a_synthetic_row_is_identifiable_on_sight(self, conn, scale_db):
        seed_source(conn)
        target = get_connection(scale_db)
        try:
            load(target, conn, comments=5)
            ids = [
                row["source_id"]
                for row in target.execute("SELECT source_id FROM comments")
            ]
        finally:
            target.close()
        assert ids and all(i.startswith(SYNTHETIC_PREFIX) for i in ids)


class TestTheNameRule:
    @pytest.mark.parametrize("dbname,expected", [
        ("fragrance_graph_scale", True),
        ("anything_scale", True),
        ("fragrance_graph", False),
        ("fragrance_graph_test", False),
        ("scale_fragrance_graph", False),
    ])
    def test_only_a_suffix_counts(self, dbname, expected):
        dsn = f"postgresql://postgres@localhost:5432/{dbname}"
        connection = type("C", (), {"info": type("I", (), {"dsn": dsn})()})()
        assert is_scale_database(connection) is expected
        assert database_name(dsn) == dbname


class TestTheLoader:
    def test_it_writes_comments_and_claims(self, conn, scale_db):
        seed_source(conn)
        target = get_connection(scale_db)
        try:
            comments, claims = load(target, conn, comments=200, seed=1)
            assert comments == 200
            assert claims > 0
            assert target.execute(
                "SELECT count(*) FROM claims"
            ).fetchone()[0] == claims
        finally:
            target.close()

    def test_a_second_run_grows_the_table(self, conn, scale_db):
        """Finding the size at which a plan changes means loading in
        stages. A loader that only worked on an empty table would make
        that a drop-and-reload every time."""
        seed_source(conn)
        target = get_connection(scale_db)
        try:
            load(target, conn, comments=50, seed=1)
            load(target, conn, comments=50, seed=2)
            assert target.execute(
                "SELECT count(*) FROM comments"
            ).fetchone()[0] == 100
        finally:
            target.close()

    def test_claims_are_not_duplicated_on_the_second_run(self, conn, scale_db):
        """The second pass must only claim the ids it just wrote."""
        seed_source(conn)
        target = get_connection(scale_db)
        try:
            _, first = load(target, conn, comments=100, seed=1)
            _, second = load(target, conn, comments=100, seed=2)
            total = target.execute("SELECT count(*) FROM claims").fetchone()[0]
            assert total == first + second
        finally:
            target.close()

    def test_it_refuses_a_source_with_nothing_to_resample(self, conn, scale_db):
        target = get_connection(scale_db)
        try:
            with pytest.raises(RefusedTarget, match="no verified claims"):
                load(target, conn, comments=10)
        finally:
            target.close()

    def test_people_and_creators_are_not_one_per_row(self, conn, scale_db):
        """Every count that matters in this project is a distinct-person
        or distinct-creator count. Data where each row is a new person
        makes those queries fast for a reason that will not hold."""
        seed_source(conn)
        target = get_connection(scale_db)
        try:
            load(target, conn, comments=2000, seed=3)
            people = target.execute(
                "SELECT count(DISTINCT author_id) FROM comments"
            ).fetchone()[0]
            channels = target.execute(
                "SELECT count(DISTINCT source_channel) FROM comments"
            ).fetchone()[0]
        finally:
            target.close()
        assert channels < 2000, "creators must repeat"
        assert people <= 2000

    def test_bodies_come_from_real_comments(self, conn, scale_db):
        """Resampled, not generated: the length distribution is what makes
        a plan on this data resemble a plan on the real thing."""
        seed_source(conn)
        real = {
            row["body"] for row in conn.execute("SELECT body FROM comments")
        }
        target = get_connection(scale_db)
        try:
            load(target, conn, comments=50, seed=4)
            bodies = {
                row["body"] for row in target.execute("SELECT body FROM comments")
            }
        finally:
            target.close()
        assert bodies <= real

    def test_the_rows_are_valid_by_the_schema_that_ships(self, conn, scale_db):
        """Synthetic data that could not have been produced by the real
        pipeline would teach the wrong lesson about plans."""
        seed_source(conn)
        target = get_connection(scale_db)
        try:
            load(target, conn, comments=100, seed=5)
            row = target.execute(
                "SELECT raw_json, polarity, evidence_verified FROM comments"
                " JOIN claims ON claims.comment_id = comments.id LIMIT 1"
            ).fetchone()
        finally:
            target.close()
        assert json.loads(row["raw_json"]) == {}
        assert row["polarity"] == "ASSERTED"
        assert row["evidence_verified"] == 1


def test_the_loader_never_appears_in_the_shipped_package():
    """It lives in scripts/ because it is not part of the product."""
    import fragrance_graph

    with pytest.raises((ImportError, ModuleNotFoundError)):
        __import__("fragrance_graph.scale")
    assert fragrance_graph  # the package itself imports fine


def test_psycopg_is_the_only_thing_that_knows_the_dsn_shape():
    """`database_name` parses with libpq's own parser rather than a regex,
    so every DSN form a person might type is read the same way the server
    reads it."""
    for dsn in (
        "postgresql://u@h:5432/mydb_scale",
        "postgresql:///mydb_scale?host=/tmp",
        "dbname=mydb_scale host=localhost",
    ):
        assert database_name(dsn) == "mydb_scale", dsn
    with pytest.raises(psycopg.ProgrammingError):
        database_name("not a dsn at all")
