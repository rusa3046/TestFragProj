"""Corpus export and rebuild.

The property that matters: a database round-trips through text without
losing anything and without duplicating anything, including the links
between claims, comments, and fragrances.
"""

import json

import pytest

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


def rebuild(tmp_path, directory, second_db):
    """Import into a genuinely empty database, as after container death."""
    fresh = get_connection(second_db)
    migrate(fresh)
    import_corpus(fresh, directory)
    return fresh


def test_export_writes_a_file_per_table(conn, tmp_path):
    populate(conn)
    stats = export_corpus(conn, tmp_path)

    assert (stats.comments, stats.claims, stats.fragrances) == (1, 1, 2)
    for name in (COMMENTS_FILE, CLAIMS_FILE, FRAGRANCES_FILE, LABELS_FILE):
        assert (tmp_path / name).exists()


def test_round_trip_preserves_the_edge(conn, tmp_path, second_db):
    """The whole point: a rebuilt database answers the same question."""
    populate(conn)
    before = [dict(r) for r in resolved_edges(conn)]
    export_corpus(conn, tmp_path)

    fresh = rebuild(tmp_path, tmp_path, second_db)
    assert [dict(r) for r in resolved_edges(fresh)] == before
    fresh.close()


def test_claims_survive_renumbered_comment_ids(conn, tmp_path, second_db):
    """Claims link by (source, source_id), not by autoincrement id.

    The rebuilt database is seeded with unrelated comments first, so every
    id differs from the original. An id-based link would attach the claim
    to the wrong comment and nothing would visibly fail.
    """
    populate(conn)
    export_corpus(conn, tmp_path)

    fresh = get_connection(second_db)
    migrate(fresh)
    ingest(fresh, [make_comment(i, body="unrelated") for i in range(50, 60)])
    import_corpus(fresh, tmp_path)

    row = fresh.execute(
        "SELECT co.body FROM claims c JOIN comments co ON co.id = c.comment_id"
    ).fetchone()
    assert row["body"] == BODY
    fresh.close()


def test_import_is_idempotent(conn, tmp_path, second_db):
    populate(conn)
    export_corpus(conn, tmp_path)

    fresh = rebuild(tmp_path, tmp_path, second_db)
    import_corpus(fresh, tmp_path)
    import_corpus(fresh, tmp_path)

    counts = {
        table: fresh.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("comments", "claims", "fragrances")
    }
    assert counts == {"comments": 1, "claims": 1, "fragrances": 2}
    fresh.close()


def test_curated_aliases_survive(conn, tmp_path, second_db):
    """Aliases are human judgement and the most expensive thing here."""
    populate(conn)
    export_corpus(conn, tmp_path)
    fresh = rebuild(tmp_path, tmp_path, second_db)

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


def test_evidence_and_permalink_survive(conn, tmp_path, second_db):
    """Both are load-bearing for the product, not diagnostics."""
    populate(conn)
    export_corpus(conn, tmp_path)
    fresh = rebuild(tmp_path, tmp_path, second_db)

    row = fresh.execute(
        "SELECT c.evidence_span, c.evidence_verified, co.permalink "
        "FROM claims c JOIN comments co ON co.id = c.comment_id"
    ).fetchone()
    assert row["evidence_span"] == BODY
    assert row["evidence_verified"] == 1
    assert row["permalink"].startswith("http")
    fresh.close()


def test_extracted_at_survives_so_reimport_does_not_repay(conn, tmp_path, second_db):
    """Losing extracted_at means paying the API again for the same answers."""
    populate(conn)
    export_corpus(conn, tmp_path)
    fresh = rebuild(tmp_path, tmp_path, second_db)

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


def test_orphan_claims_are_reported_not_silently_dropped(conn, tmp_path, caplog, second_db):
    import logging

    populate(conn)
    export_corpus(conn, tmp_path)
    (tmp_path / COMMENTS_FILE).write_text("")

    fresh = get_connection(second_db)
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


def test_labels_round_trip(conn, tmp_path, second_db):
    """An hour of human judgement, and it cannot be regenerated by paying."""
    comment_id = populate(conn)
    label_a_comment(conn, comment_id)

    stats = export_corpus(conn, tmp_path)
    assert stats.labels == 1

    fresh = rebuild(tmp_path, tmp_path, second_db)
    stored = fresh.execute(
        "SELECT labeled_json, labeler FROM eval_labels"
    ).fetchone()
    assert stored["labeler"] == "aanya"
    assert "Zara Red Temptation" in stored["labeled_json"]
    fresh.close()


def test_label_import_is_idempotent(conn, tmp_path, second_db):
    comment_id = populate(conn)
    label_a_comment(conn, comment_id)
    export_corpus(conn, tmp_path)

    fresh = rebuild(tmp_path, tmp_path, second_db)
    import_corpus(fresh, tmp_path)

    assert fresh.execute("SELECT count(*) FROM eval_labels").fetchone()[0] == 1
    fresh.close()


def test_labels_survive_renumbered_comment_ids(conn, tmp_path, second_db):
    comment_id = populate(conn)
    label_a_comment(conn, comment_id)
    export_corpus(conn, tmp_path)

    fresh = get_connection(second_db)
    migrate(fresh)
    ingest(fresh, [make_comment(i, body="unrelated") for i in range(70, 80)])
    import_corpus(fresh, tmp_path)

    row = fresh.execute(
        "SELECT co.body FROM eval_labels l JOIN comments co ON co.id = l.comment_id"
    ).fetchone()
    assert row["body"] == BODY
    fresh.close()


def test_two_labelers_on_one_comment_both_survive(conn, tmp_path, second_db):
    comment_id = populate(conn)
    label_a_comment(conn, comment_id, labeler="aanya")
    label_a_comment(conn, comment_id, labeler="colleague")

    assert export_corpus(conn, tmp_path).labels == 2

    fresh = rebuild(tmp_path, tmp_path, second_db)
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

    def test_prune_survives_claims_pointing_at_the_row(self, conn, tmp_path):
        """The crash: a foreign key refuses to drop a curated fragrance.

        Pruning must un-resolve the claims, never delete them — a claim
        cost real money and its raw text is still true.
        """
        from fragrance_graph.corpus import import_corpus
        from fragrance_graph.resolve.entities import add_fragrance
        from tests.test_query import add_comment

        doomed = add_fragrance(conn, "Armaf Club De Nuit", brand="Armaf")
        keep = add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa")
        cid = add_comment(conn, 1, body="club de nuit smells like khamrah",
                          author="u1")
        conn.execute(
            "INSERT INTO claims (comment_id, claim_type, subject_kind,"
            " raw_subject_text, subject_frag_id, object_kind,"
            " raw_object_text, object_frag_id, confidence, polarity,"
            " evidence_span, evidence_verified, extraction_model, created_at)"
            " VALUES (%s, 'SIMILAR_TO', 'FRAGRANCE', 'club de nuit', %s,"
            " 'FRAGRANCE', 'khamrah', %s, 0.9, 'ASSERTED', 'x', 1,"
            " 'test', '2026-01-01')",
            (cid, doomed, keep),
        )
        conn.commit()

        import_corpus(conn, self._corpus(tmp_path, ["Lattafa Khamrah"]),
                      prune=True)

        row = conn.execute(
            "SELECT subject_frag_id, object_frag_id, raw_subject_text"
            " FROM claims"
        ).fetchone()
        assert row["subject_frag_id"] is None, "must let go of the pruned row"
        assert row["object_frag_id"] == keep, "the other end is untouched"
        assert row["raw_subject_text"] == "club de nuit", "text is preserved"


class TestRejectsRoundTrip:
    """Rejections survive a rebuild, which is the whole reason to commit them."""

    def _reject(self, conn, comment_id, reason="NOTE_DESCRIPTOR ... got NONE"):
        conn.execute(
            "INSERT INTO rejected_claims (comment_id, reason, raw_json,"
            " extraction_model, created_at)"
            " VALUES (%s, %s, '{\"claim_type\": \"NOTE_DESCRIPTOR\"}',"
            " 'test', '2026-01-01')",
            (comment_id, reason),
        )

    def test_a_rejection_survives_export_and_import(self, conn, tmp_path, second_db):
        """The loss that motivated this: a rebuild wiped ~66 rejections."""
        from fragrance_graph.corpus import export_corpus, import_corpus
        from fragrance_graph.db import get_connection, migrate
        from tests.test_query import add_comment

        cid = add_comment(conn, 1, body="layton is soft asf", author="u1")
        self._reject(conn, cid)
        conn.commit()

        stats = export_corpus(conn, tmp_path)
        assert stats.rejects == 1

        fresh = get_connection(second_db)
        migrate(fresh)
        imported = import_corpus(fresh, tmp_path)
        assert imported.rejects == 1
        row = fresh.execute(
            "SELECT reason, raw_json FROM rejected_claims"
        ).fetchone()
        assert "NOTE_DESCRIPTOR" in row["reason"]
        assert "NOTE_DESCRIPTOR" in row["raw_json"]
        fresh.close()

    def test_importing_twice_does_not_duplicate(self, conn, tmp_path):
        """A rejection has no natural key, so merging would multiply it."""
        from fragrance_graph.corpus import export_corpus, import_corpus
        from tests.test_query import add_comment

        cid = add_comment(conn, 1, body="layton is soft asf", author="u1")
        self._reject(conn, cid)
        conn.commit()
        export_corpus(conn, tmp_path)

        import_corpus(conn, tmp_path)
        import_corpus(conn, tmp_path)
        assert conn.execute(
            "SELECT count(*) FROM rejected_claims"
        ).fetchone()[0] == 1

    def test_the_analysis_works_after_a_rebuild(self, conn, tmp_path, second_db):
        """The point of committing them: `recoverable` still has data."""
        from fragrance_graph.corpus import export_corpus, import_corpus
        from fragrance_graph.db import get_connection, migrate
        from fragrance_graph.extract.rejects import recoverable
        from tests.test_query import add_comment

        cid = add_comment(conn, 1, body="layton is soft asf", author="u1")
        self._reject(conn, cid)
        conn.commit()
        export_corpus(conn, tmp_path)

        fresh = get_connection(second_db)
        migrate(fresh)
        import_corpus(fresh, tmp_path)
        assert recoverable(fresh)["total"] == 1
        fresh.close()


def test_a_claim_naming_a_missing_fragrance_is_reported(conn, tmp_path):
    """Hand-editing fragrances.jsonl leaves claims pointing at nothing.

    They import as NULL, so the claim silently stops being an edge. Five
    were committed that way on 2026-08-11.
    """
    import json

    from fragrance_graph.corpus import import_corpus
    from tests.conftest import make_comment

    d = tmp_path / "corpus"
    d.mkdir()
    (d / "fragrances.jsonl").write_text(
        json.dumps({"canonical_name": "Lattafa Khamrah", "brand": "Lattafa",
                    "house_year": None, "aliases": []}) + "\n",
        encoding="utf-8",
    )
    comment = make_comment(1)
    (d / "comments.jsonl").write_text(
        json.dumps({"source": "youtube", **comment, "extracted_at": None}) + "\n",
        encoding="utf-8",
    )
    (d / "claims.jsonl").write_text(
        json.dumps({
            "comment_source": "youtube",
            "comment_source_id": comment["source_id"],
            "claim_type": "SIMILAR_TO", "polarity": "ASSERTED",
            "subject_kind": "FRAGRANCE", "raw_subject_text": "khamrah",
            "object_kind": "FRAGRANCE", "raw_object_text": "club de nuit",
            "sentiment": "NEUTRAL", "confidence": 0.9,
            "evidence_span": "x", "evidence_verified": 1,
            "extraction_model": "test", "created_at": "2026-01-01",
            "subject_fragrance": "Lattafa Khamrah",
            "object_fragrance": "Armaf Club De Nuit",
        }) + "\n",
        encoding="utf-8",
    )

    stats = import_corpus(conn, d)
    assert stats.dangling_fragrances == {"Armaf Club De Nuit": 1}
    # The claim is still imported — it is real, it just is not an edge.
    assert stats.claims == 1


class TestExportWillNotSilentlyShrink:
    """Export writes the database over the corpus, so a stale database
    reverts committed work. That happened three times on 2026-08-11: a
    wrong curation entry returned after removal, 45 corrupted eval labels
    overwrote a restored file, and 24 discovery records were deleted twice.
    """

    def _corpus(self, tmp_path, discoveries=2):
        import json

        d = tmp_path / "corpus"
        d.mkdir(exist_ok=True)
        (d / "video_discoveries.jsonl").write_text(
            "".join(
                json.dumps({"source": "youtube", "video_id": f"v{i}",
                            "retrieval_query": "q", "retrieved_at": "2026-01-01",
                            "discovery_run": "r"}) + "\n"
                for i in range(discoveries)
            ),
            encoding="utf-8",
        )
        return d

    def test_it_refuses_to_delete_committed_rows(self, conn, tmp_path):
        from fragrance_graph.corpus import WouldLoseRows, export_corpus

        d = self._corpus(tmp_path, discoveries=24)
        with pytest.raises(WouldLoseRows) as exc:
            export_corpus(conn, d)          # empty database
        assert "24 committed -> 0" in str(exc.value)
        assert "rebuild" in str(exc.value).lower()
        # And the file is untouched.
        assert len((d / "video_discoveries.jsonl").read_text().splitlines()) == 24

    def test_force_writes_anyway(self, conn, tmp_path):
        from fragrance_graph.corpus import export_corpus

        d = self._corpus(tmp_path, discoveries=24)
        export_corpus(conn, d, force=True)
        assert (d / "video_discoveries.jsonl").read_text().strip() == ""

    def test_growing_is_never_blocked(self, conn, tmp_path):
        """The ordinary case — new work — must not need a flag."""
        from fragrance_graph.corpus import export_corpus
        from fragrance_graph.resolve.entities import add_fragrance

        d = self._corpus(tmp_path, discoveries=0)
        add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa")
        conn.commit()
        stats = export_corpus(conn, d)
        assert stats.fragrances == 1

    def test_comments_and_claims_are_not_guarded(self, conn, tmp_path):
        """A re-extraction legitimately shrinks claims; that is not loss."""
        import json

        from fragrance_graph.corpus import export_corpus

        d = self._corpus(tmp_path, discoveries=0)
        (d / "claims.jsonl").write_text(
            json.dumps({"claim_type": "DUPE_OF"}) + "\n", encoding="utf-8")
        export_corpus(conn, d)      # no raise
        assert (d / "claims.jsonl").read_text().strip() == ""
