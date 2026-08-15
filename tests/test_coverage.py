"""Why a question the system understands still cannot be answered.

The distinction these tests protect: **absence of evidence is never
evidence of less**. Four buckets exist instead of a percentage precisely
so that "nobody mentioned rose" cannot be quietly counted as "has less
rose", which would make every sparse bottle look like a perfect answer.
"""

import json

import pytest

from fragrance_graph.coverage import (
    Verdict,
    density,
    relative_coverage,
    snapshot,
)
from fragrance_graph.ingest.store import ingest
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def note(conn, i, *, frag, value, author, channel="chan_a"):
    body = f"comment {i}: {value}"
    ingest(conn, [make_comment(i, body=body, source_channel=channel,
                               raw_json=json.dumps({"author": author,
                                                    "videoId": "v1"}))])
    cid = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, sentiment,
             confidence, evidence_span, evidence_verified, polarity,
             extraction_model, created_at)
        VALUES (%s, 'NOTE_DESCRIPTOR', 'FRAGRANCE', 'it', %s, 'TAG', %s,
                'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, frag, value, body),
    )
    conn.commit()


@pytest.fixture
def anchor_and_others(conn):
    anchor = add_fragrance(conn, "Parfums de Marly Delina")
    others = [add_fragrance(conn, f"Bottle {i}") for i in range(3)]
    return conn, anchor, others


CASE = (("Parfums de Marly Delina", "rose"),)


class TestAbsenceIsNotLess:
    def test_no_evidence_is_its_own_bucket(self, anchor_and_others):
        conn, anchor, others = anchor_and_others
        for i, (author, chan) in enumerate([("p1", "c1"), ("p2", "c2")]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=chan)
        (row,) = relative_coverage(conn, CASE)
        assert row.counts[Verdict.NO_EVIDENCE] == 3
        assert row.counts[Verdict.LESS] == 0, (
            "three silent bottles are not three bottles with less rose"
        )

    def test_a_case_with_nothing_to_compare_is_not_answerable(
        self, anchor_and_others
    ):
        conn, anchor, _ = anchor_and_others
        for i, (author, chan) in enumerate([("p1", "c1"), ("p2", "c2")]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=chan)
        (row,) = relative_coverage(conn, CASE)
        assert not row.answerable
        assert "no rose evidence" in row.blocker

    def test_a_candidate_with_genuinely_less_is_counted(self, anchor_and_others):
        conn, anchor, others = anchor_and_others
        for i, (author, chan) in enumerate(
            [("p1", "c1"), ("p2", "c2"), ("p3", "c3"), ("p4", "c4")]
        ):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=chan)
        note(conn, 20, frag=others[0], value="rose", author="z1")
        (row,) = relative_coverage(conn, CASE)
        assert row.counts[Verdict.LESS] == 1
        assert row.answerable


class TestBaselineProblemsAreNamed:
    def test_a_thin_anchor_is_reported_as_the_blocker(self, anchor_and_others):
        """"BR540 but less sweet" fails here, not at the candidates."""
        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        note(conn, 2, frag=others[0], value="rose", author="z1")
        (row,) = relative_coverage(conn, CASE)
        assert not row.baseline_usable
        assert "1 creator" in row.baseline_problem
        assert "anchor" in row.blocker

    def test_a_missing_anchor_attribute_is_reported(self, anchor_and_others):
        conn, _, others = anchor_and_others
        note(conn, 1, frag=others[0], value="rose", author="z1")
        (row,) = relative_coverage(conn, CASE)
        assert row.anchor_people == 0
        assert "anchor has no rose evidence" in row.blocker

    def test_three_failures_are_distinguished(self, anchor_and_others):
        """Thin anchor, missing attribute and catalogue sparsity all read
        as "not answerable" and need different money spent on them."""
        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        (thin,) = relative_coverage(conn, CASE)
        assert "anchor" in thin.blocker and "creator" in thin.blocker


class TestDensityIsTheMetricThatMatters:
    def test_an_attribute_on_one_bottle_is_not_comparable(
        self, anchor_and_others
    ):
        conn, anchor, _ = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        (row,) = [r for r in density(conn) if r.value == "rose"]
        assert row.bottles == 1
        assert not row.comparable

    def test_two_bottles_sharing_it_are(self, anchor_and_others):
        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        note(conn, 2, frag=others[0], value="rose", author="p2")
        (row,) = [r for r in density(conn) if r.value == "rose"]
        assert row.bottles == 2
        assert row.comparable

    def test_a_snapshot_counts_shared_attributes_not_only_facts(
        self, anchor_and_others
    ):
        """A run adding six hundred one-off descriptors moves `facts` and
        answers nothing. The snapshot has to be able to tell."""
        conn, anchor, others = anchor_and_others
        for i, value in enumerate(["rose", "lychee", "powder", "tart"]):
            note(conn, i, frag=anchor, value=value, author=f"p{i}")
        before = snapshot(conn)
        assert before.facts == 4
        assert before.comparable_attributes == 0, (
            "four facts, no attribute shared with any other bottle"
        )

        note(conn, 40, frag=others[0], value="rose", author="z1")
        after = snapshot(conn)
        assert after.facts == 5
        assert after.comparable_attributes == 1, "one shared attribute now"

    def test_the_snapshot_renders_a_before_after_diff(self, anchor_and_others):
        conn, anchor, _ = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        before = snapshot(conn)
        note(conn, 2, frag=anchor, value="lychee", author="p2")
        after = snapshot(conn)
        rendered = before.render(after)
        assert "before" in rendered and "after" in rendered
        assert "+1" in rendered
