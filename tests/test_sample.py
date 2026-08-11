"""Choosing which comments to label next.

The eval set is the blocker on every extraction change and human hours are
the constraint, so what matters here is that a batch is worth the evening
it costs: balanced, resumable, and pointed at what nothing else can see.
"""

import json

import pytest

from fragrance_graph.evals.labels import import_labels, split_for
from fragrance_graph.evals.sample import (
    CONTROL,
    EDGE,
    REJECTED,
    SILENT,
    Pick,
    coverage,
    render_coverage,
    select,
    talks_like_a_comparison,
)
from tests.test_query import add_comment


def add_claim_row(conn, comment_id, claim_type="SIMILAR_TO", subject="khamrah"):
    conn.execute(
        "INSERT INTO claims (comment_id, claim_type, subject_kind,"
        " raw_subject_text, object_kind, raw_object_text, confidence,"
        " polarity, evidence_span, evidence_verified, extraction_model,"
        " created_at)"
        " VALUES (?, ?, 'FRAGRANCE', ?, 'FRAGRANCE', 'aventus', 0.9,"
        " 'ASSERTED', 'x', 1, 'test', '2026-01-01')",
        (comment_id, claim_type, subject),
    )


def add_reject(conn, comment_id):
    conn.execute(
        "INSERT INTO rejected_claims (comment_id, reason, raw_json,"
        " extraction_model, created_at)"
        " VALUES (?, 'NOTE_DESCRIPTOR ... got NONE', '{}', 'test',"
        " '2026-01-01')",
        (comment_id,),
    )


def mark_extracted(conn, comment_id):
    conn.execute(
        "UPDATE comments SET extracted_at = '2026-01-01' WHERE id = ?",
        (comment_id,),
    )


class TestComparisonLanguage:
    @pytest.mark.parametrize("body", [
        "this is a dupe of aventus",
        "smells like fruity pebbles",
        "reminds me of Layton",
        "way better than Sauvage",
        "Khamrah vs Angels Share",
        "an inspired by version",
    ])
    def test_comparison_talk_is_caught(self, body):
        assert talks_like_a_comparison(body) is True

    @pytest.mark.parametrize("body", [
        "the bottle is gorgeous",
        "bought it last month at the mall",
        "",
    ])
    def test_ordinary_talk_is_not(self, body):
        assert talks_like_a_comparison(body) is False


class TestSelect:
    def test_a_silent_comparison_is_chosen(self, conn):
        """The stratum nothing else can see.

        Extracted, produced no claims, and talks like a comparison — a
        false negative here is invisible to scoring, which only ever
        compares labels against claims that exist.
        """
        cid = add_comment(conn, 1, body="this is a dupe of aventus", author="u")
        mark_extracted(conn, cid)
        conn.commit()

        picks = select(conn, 5)
        assert [p.stratum for p in picks] == [SILENT]

    def test_an_unextracted_comment_is_not_silent(self, conn):
        """Silence only means something once the extractor has spoken."""
        add_comment(conn, 1, body="this is a dupe of aventus", author="u")
        conn.commit()
        assert [p.stratum for p in select(conn, 5)] == [CONTROL]

    def test_a_refused_comment_outranks_the_rest(self, conn):
        cid = add_comment(conn, 1, body="layton is soft asf", author="u")
        mark_extracted(conn, cid)
        add_reject(conn, cid)
        conn.commit()
        picks = select(conn, 5)
        assert picks[0].stratum == REJECTED
        assert "refused" in picks[0].why

    def test_an_edge_producing_comment_is_its_own_stratum(self, conn):
        cid = add_comment(conn, 1, body="khamrah is like aventus", author="u")
        mark_extracted(conn, cid)
        add_claim_row(conn, cid)
        conn.commit()
        assert [p.stratum for p in select(conn, 5)] == [EDGE]

    def test_control_is_never_squeezed_out(self, conn):
        """A set built only from failures cannot measure ordinary precision."""
        for i in range(30):
            cid = add_comment(conn, i, body="this is a dupe of aventus",
                              author=f"u{i}")
            mark_extracted(conn, cid)
        for i in range(30, 60):
            add_comment(conn, i, body="the bottle is gorgeous", author=f"u{i}")
        conn.commit()

        picks = select(conn, 30, control_fraction=1 / 3)
        controls = [p for p in picks if p.stratum == CONTROL]
        assert len(controls) == 10

    def test_already_labelled_comments_are_skipped(self, conn):
        cid = add_comment(conn, 1, body="khamrah is like aventus", author="u")
        add_comment(conn, 2, body="also a dupe of aventus", author="u2")
        conn.commit()
        import_labels(
            conn,
            [{"source": "reddit", "source_id": "t1_fake00001", "claims": []}],
            labeler="me",
        )
        conn.commit()

        picked = {p.source_id for p in select(conn, 10, labeler="me")}
        assert "t1_fake00001" not in picked
        assert "t1_fake00002" in picked
        assert cid  # the first comment exists; it is simply already done

    def test_the_batch_is_deterministic(self, conn):
        for i in range(20):
            add_comment(conn, i, body=f"comment {i} smells like aventus",
                        author=f"u{i}")
        conn.commit()
        assert [p.source_id for p in select(conn, 8)] == [
            p.source_id for p in select(conn, 8)
        ]

    def test_it_never_returns_more_than_asked(self, conn):
        for i in range(40):
            add_comment(conn, i, body="the bottle is gorgeous", author=f"u{i}")
        conn.commit()
        assert len(select(conn, 7)) == 7


class TestTemplateRoundTrip:
    def test_a_plan_imports_through_the_normal_path(self, conn):
        """The batch has to be usable by the tooling that already exists.

        `_stratum` and `_why` are annotations for the reviewer; the
        importer must ignore them rather than choke.
        """
        add_comment(conn, 1, body="khamrah is a dupe of aventus", author="u")
        conn.commit()

        picks = select(conn, 1)
        entries = [p.as_template_entry() for p in picks]
        assert "_why" in entries[0]

        # Round-trip through JSON, as a real labelling session would.
        entries = json.loads(json.dumps(entries))
        entries[0]["claims"] = [
            {"claim_type": "DUPE_OF", "raw_subject_text": "khamrah",
             "raw_object_text": "aventus", "sentiment": "POSITIVE"}
        ]
        assert import_labels(conn, entries, labeler="me") == 1

    def test_the_split_matches_the_shared_rule(self, conn):
        pick = Pick("youtube", "abc123", "body", CONTROL, "why")
        assert pick.split == split_for("abc123")


class TestCoverage:
    def test_it_names_the_blind_spot(self, conn):
        cid = add_comment(conn, 1, body="this is a dupe of aventus", author="u")
        mark_extracted(conn, cid)
        add_comment(conn, 2, body="the bottle is gorgeous", author="u2")
        conn.commit()

        cov = coverage(conn)
        assert cov.silent_unlabelled == 1
        assert "blind spot" in render_coverage(cov)

    def test_it_counts_labels_by_labeler_and_split(self, conn):
        add_comment(conn, 1, body="khamrah is a dupe of aventus", author="u")
        conn.commit()
        import_labels(
            conn,
            [{"source": "reddit", "source_id": "t1_fake00001",
              "claims": [{"claim_type": "DUPE_OF",
                          "raw_subject_text": "khamrah",
                          "raw_object_text": "aventus"}]}],
            labeler="me",
        )
        conn.commit()

        cov = coverage(conn)
        assert cov.labelled["me"] == 1
        assert cov.claim_types["DUPE_OF"] == 1
        assert sum(cov.by_split.values()) == 1

    def test_an_empty_set_renders_without_crashing(self, conn):
        assert "nothing yet" in render_coverage(coverage(conn))
