"""Corpus export and rebuild.

The property that matters: a database round-trips through text without
losing anything and without duplicating anything, including the links
between claims, comments, and fragrances.
"""

import json

from fragrance_graph.corpus import (
    CLAIMS_FILE,
    COMMENTS_FILE,
    FRAGRANCES_FILE,
    LABELS_FILE,
    export_corpus,
    import_corpus,
)
from fragrance_graph.db import get_connection, migrate
from fragrance_graph.extract.llm import write_claims
from fragrance_graph.ingest.store import ingest
from fragrance_graph.models import Claim
from fragrance_graph.resolve.entities import add_fragrance, backfill, resolved_edges
from tests.conftest import make_comment

BODY = "Zara Red Temptation is honestly a dupe of BR540"


def populate(conn):
    """A small corpus exercising every link the export has to preserve."""
    add_fragrance(conn, "Baccarat Rouge 540", brand="MFK", aliases=["BR540", "540"])
    add_fragrance(conn, "Zara Red Temptation", brand="Zara")

    ingest(conn, [make_comment(1, body=BODY)])
    comment_id = conn.execute("SELECT id FROM comments").fetchone()[0]
    write_claims(
        conn,
        comment_id,
        BODY,
        [
            Claim(
                claim_type="DUPE_OF",
                subject_kind="FRAGRANCE",
                raw_subject_text="Zara Red Temptation",
                object_kind="FRAGRANCE",
                raw_object_text="BR540",
                confidence=0.9,
                evidence_span=BODY,
            )
        ],
    )
    backfill(conn)
    return comment_id


def rebuild(tmp_path, directory):
    """Import into a genuinely empty database, as after container death."""
    fresh = get_connection(tmp_path / "rebuilt.db")
    migrate(fresh)
    import_corpus(fresh, directory)
    return fresh


def test_export_writes_a_file_per_table(conn, tmp_path):
    populate(conn)
    stats = export_corpus(conn, tmp_path)

    assert (stats.comments, stats.claims, stats.fragrances) == (1, 1, 2)
    for name in (COMMENTS_FILE, CLAIMS_FILE, FRAGRANCES_FILE, LABELS_FILE):
        assert (tmp_path / name).exists()


def test_round_trip_preserves_the_edge(conn, tmp_path):
    """The whole point: a rebuilt database answers the same question."""
    populate(conn)
    before = [dict(r) for r in resolved_edges(conn)]
    export_corpus(conn, tmp_path)

    fresh = rebuild(tmp_path, tmp_path)
    assert [dict(r) for r in resolved_edges(fresh)] == before
    fresh.close()


def test_claims_survive_renumbered_comment_ids(conn, tmp_path):
    """Claims link by (source, source_id), not by autoincrement id.

    The rebuilt database is seeded with unrelated comments first, so every
    id differs from the original. An id-based link would attach the claim
    to the wrong comment and nothing would visibly fail.
    """
    populate(conn)
    export_corpus(conn, tmp_path)

    fresh = get_connection(tmp_path / "shifted.db")
    migrate(fresh)
    ingest(fresh, [make_comment(i, body="unrelated") for i in range(50, 60)])
    import_corpus(fresh, tmp_path)

    row = fresh.execute(
        "SELECT co.body FROM claims c JOIN comments co ON co.id = c.comment_id"
    ).fetchone()
    assert row["body"] == BODY
    fresh.close()


def test_import_is_idempotent(conn, tmp_path):
    populate(conn)
    export_corpus(conn, tmp_path)

    fresh = rebuild(tmp_path, tmp_path)
    import_corpus(fresh, tmp_path)
    import_corpus(fresh, tmp_path)

    counts = {
        table: fresh.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("comments", "claims", "fragrances")
    }
    assert counts == {"comments": 1, "claims": 1, "fragrances": 2}
    fresh.close()


def test_curated_aliases_survive(conn, tmp_path):
    """Aliases are human judgement and the most expensive thing here."""
    populate(conn)
    export_corpus(conn, tmp_path)
    fresh = rebuild(tmp_path, tmp_path)

    aliases = fresh.execute(
        "SELECT aliases FROM fragrances WHERE canonical_name = 'Baccarat Rouge 540'"
    ).fetchone()[0]
    assert json.loads(aliases) == ["540", "BR540"]
    fresh.close()


def test_aliases_export_as_a_list_not_a_json_string(conn, tmp_path):
    """A curator has to be able to hand-edit these without escaping."""
    populate(conn)
    export_corpus(conn, tmp_path)

    line = (tmp_path / FRAGRANCES_FILE).read_text().splitlines()[0]
    assert isinstance(json.loads(line)["aliases"], list)


def test_evidence_and_permalink_survive(conn, tmp_path):
    """Both are load-bearing for the product, not diagnostics."""
    populate(conn)
    export_corpus(conn, tmp_path)
    fresh = rebuild(tmp_path, tmp_path)

    row = fresh.execute(
        "SELECT c.evidence_span, c.evidence_verified, co.permalink "
        "FROM claims c JOIN comments co ON co.id = c.comment_id"
    ).fetchone()
    assert row["evidence_span"] == BODY
    assert row["evidence_verified"] == 1
    assert row["permalink"].startswith("http")
    fresh.close()


def test_extracted_at_survives_so_reimport_does_not_repay(conn, tmp_path):
    """Losing extracted_at means paying the API again for the same answers."""
    populate(conn)
    export_corpus(conn, tmp_path)
    fresh = rebuild(tmp_path, tmp_path)

    pending = fresh.execute(
        "SELECT count(*) FROM comments WHERE extracted_at IS NULL"
    ).fetchone()[0]
    assert pending == 0
    fresh.close()


def test_export_is_byte_stable(conn, tmp_path):
    """An unchanged corpus must produce an empty diff, or review is noise."""
    populate(conn)
    first = tmp_path / "a"
    second = tmp_path / "b"
    export_corpus(conn, first)
    export_corpus(conn, second)

    for name in (COMMENTS_FILE, CLAIMS_FILE, FRAGRANCES_FILE, LABELS_FILE):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_orphan_claims_are_reported_not_silently_dropped(conn, tmp_path, caplog):
    import logging

    populate(conn)
    export_corpus(conn, tmp_path)
    (tmp_path / COMMENTS_FILE).write_text("")

    fresh = get_connection(tmp_path / "orphan.db")
    migrate(fresh)
    with caplog.at_level(logging.WARNING):
        stats = import_corpus(fresh, tmp_path)

    assert stats.claims == 0
    assert "skipped" in "\n".join(r.getMessage() for r in caplog.records)
    fresh.close()


def test_importing_an_empty_directory_is_harmless(conn, tmp_path):
    stats = import_corpus(conn, tmp_path / "nothing")
    assert (stats.comments, stats.claims, stats.fragrances) == (0, 0, 0)


def test_unicode_names_stay_readable_in_the_file(conn, tmp_path):
    add_fragrance(conn, "Wulóng Chá")
    export_corpus(conn, tmp_path)

    assert "Wulóng Chá" in (tmp_path / FRAGRANCES_FILE).read_text(encoding="utf-8")


# --- eval labels ------------------------------------------------------------


def label_a_comment(conn, comment_id, *, labeler="aanya"):
    from fragrance_graph.evals.labels import import_labels

    import_labels(
        conn,
        [
            {
                "comment_id": comment_id,
                "split": "train",
                "claims": [
                    {
                        "claim_type": "DUPE_OF",
                        "raw_subject_text": "Zara Red Temptation",
                        "raw_object_text": "BR540",
                        "sentiment": "NEUTRAL",
                    }
                ],
            }
        ],
        labeler=labeler,
    )


def test_labels_round_trip(conn, tmp_path):
    """An hour of human judgement, and it cannot be regenerated by paying."""
    comment_id = populate(conn)
    label_a_comment(conn, comment_id)

    stats = export_corpus(conn, tmp_path)
    assert stats.labels == 1

    fresh = rebuild(tmp_path, tmp_path)
    stored = fresh.execute(
        "SELECT labeled_json, labeler FROM eval_labels"
    ).fetchone()
    assert stored["labeler"] == "aanya"
    assert "Zara Red Temptation" in stored["labeled_json"]
    fresh.close()


def test_label_import_is_idempotent(conn, tmp_path):
    comment_id = populate(conn)
    label_a_comment(conn, comment_id)
    export_corpus(conn, tmp_path)

    fresh = rebuild(tmp_path, tmp_path)
    import_corpus(fresh, tmp_path)

    assert fresh.execute("SELECT count(*) FROM eval_labels").fetchone()[0] == 1
    fresh.close()


def test_labels_survive_renumbered_comment_ids(conn, tmp_path):
    comment_id = populate(conn)
    label_a_comment(conn, comment_id)
    export_corpus(conn, tmp_path)

    fresh = get_connection(tmp_path / "shifted_labels.db")
    migrate(fresh)
    ingest(fresh, [make_comment(i, body="unrelated") for i in range(70, 80)])
    import_corpus(fresh, tmp_path)

    row = fresh.execute(
        "SELECT co.body FROM eval_labels l JOIN comments co ON co.id = l.comment_id"
    ).fetchone()
    assert row["body"] == BODY
    fresh.close()


def test_two_labelers_on_one_comment_both_survive(conn, tmp_path):
    comment_id = populate(conn)
    label_a_comment(conn, comment_id, labeler="aanya")
    label_a_comment(conn, comment_id, labeler="colleague")

    assert export_corpus(conn, tmp_path).labels == 2

    fresh = rebuild(tmp_path, tmp_path)
    labelers = [r[0] for r in fresh.execute("SELECT labeler FROM eval_labels ORDER BY 1")]
    assert labelers == ["aanya", "colleague"]
    fresh.close()


class TestCorpusIsTheAuthority:
    """Import upserts, so a row removed from the corpus can resurrect."""

    def _corpus(self, tmp_path, names):
        import json
        d = tmp_path / "corpus"
        d.mkdir(exist_ok=True)
        (d / "fragrances.jsonl").write_text(
            "".join(
                json.dumps({"canonical_name": n, "brand": "B",
                            "house_year": None, "aliases": []}) + "\n"
                for n in names
            ),
            encoding="utf-8",
        )
        return d

    def test_a_row_the_corpus_dropped_is_reported(self, conn, tmp_path):
        """The resurrection path, named so it cannot happen silently."""
        from fragrance_graph.corpus import import_corpus
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Armaf Club De Nuit", brand="Armaf")
        add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa")
        conn.commit()

        stats = import_corpus(conn, self._corpus(tmp_path, ["Lattafa Khamrah"]))
        assert stats.extra_fragrances == ["Armaf Club De Nuit"]
        # Reported, not deleted: it might be curation not yet exported.
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == 2

    def test_prune_deletes_them(self, conn, tmp_path):
        from fragrance_graph.corpus import import_corpus
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Armaf Club De Nuit", brand="Armaf")
        add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa")
        conn.commit()

        import_corpus(conn, self._corpus(tmp_path, ["Lattafa Khamrah"]),
                      prune=True)
        names = {r["canonical_name"]
                 for r in conn.execute("SELECT canonical_name FROM fragrances")}
        assert names == {"Lattafa Khamrah"}

    def test_a_matching_database_reports_nothing(self, conn, tmp_path):
        from fragrance_graph.corpus import import_corpus
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa")
        conn.commit()
        stats = import_corpus(conn, self._corpus(tmp_path, ["Lattafa Khamrah"]))
        assert stats.extra_fragrances == []
