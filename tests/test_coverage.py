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


class TestDensityProfile:
    """How the vocabulary spreads over bottles — the shape that decides
    whether comparisons are possible at all."""

    def test_a_value_on_one_bottle_is_counted_apart(self, anchor_and_others):
        from fragrance_graph.coverage import density_profile

        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        profile = density_profile(conn)
        assert profile.exactly_one == 1
        assert profile.at_least[2] == 0

    def test_thresholds_are_cumulative(self, anchor_and_others):
        conn, anchor, others = anchor_and_others
        from fragrance_graph.coverage import density_profile

        for i, frag in enumerate([anchor, *others]):
            note(conn, i, frag=frag, value="rose", author=f"p{i}")
        profile = density_profile(conn)
        assert profile.at_least[2] >= profile.at_least[3] >= profile.at_least[5]

    def test_it_reports_coverage_by_attribute_family(self, anchor_and_others):
        from fragrance_graph.coverage import density_profile

        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        note(conn, 2, frag=others[0], value="lychee", author="p2")
        values, bottles = density_profile(conn).by_attribute["note"]
        assert (values, bottles) == (2, 2)


class TestComparisonCoverage:
    """How much of the catalogue a given "less X" question can reach."""

    def test_silence_is_counted_as_silence_not_as_less(self, anchor_and_others):
        from fragrance_graph.coverage import comparison_coverage

        conn, anchor, others = anchor_and_others
        for i, (author, chan) in enumerate([("p1", "c1"), ("p2", "c2")]):
            note(conn, i, frag=anchor, value="rose", author=author, channel=chan)
        (row,) = comparison_coverage(
            conn, (("Parfums de Marly Delina", "rose"),)
        )
        assert row.comparable == 0
        assert row.silent == 3
        assert row.coverage == 0.0

    def test_a_bottle_with_evidence_is_comparable_either_way(
        self, anchor_and_others
    ):
        """Comparable means the corpus can say more, less *or*
        indistinguishable — not that it happens to say less."""
        from fragrance_graph.coverage import comparison_coverage

        conn, anchor, others = anchor_and_others
        for i, (author, chan) in enumerate([("p1", "c1"), ("p2", "c2")]):
            note(conn, i, frag=anchor, value="rose", author=author, channel=chan)
        note(conn, 20, frag=others[0], value="rose", author="z1")
        (row,) = comparison_coverage(
            conn, (("Parfums de Marly Delina", "rose"),)
        )
        assert row.comparable == 1
        assert row.silent == 2

    def test_a_bottle_outside_the_catalogue_says_so(self, conn):
        """Babycat and Libre are named in the benchmark and are not in the
        catalogue. That is the answer for those rows, not a measurement
        gap to paper over."""
        from fragrance_graph.coverage import comparison_coverage

        (row,) = comparison_coverage(conn, (("Babycat", "smoky"),))
        assert not row.in_catalogue
        assert "not in the catalogue" in row.render()

    def test_a_thin_anchor_is_distinguished_from_a_usable_one(
        self, anchor_and_others
    ):
        from fragrance_graph.coverage import comparison_coverage

        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        note(conn, 2, frag=others[0], value="rose", author="z1")
        (row,) = comparison_coverage(
            conn, (("Parfums de Marly Delina", "rose"),)
        )
        assert row.anchor_evidence
        assert not row.baseline_usable
        assert "thin" in row.render()


class TestDecomposingTwoSnapshots:
    """Bucket counts cannot say what happened.

    Singleton -29 and repeated +6 is consistent with two conversions and
    with twenty, and the difference decides whether enrichment is worth
    buying. The first run could not distinguish them corpus-wide.
    """

    def test_it_names_the_singletons_that_converted(self, anchor_and_others):
        from fragrance_graph.coverage import converted, snapshot

        conn, anchor, _ = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1", channel="c1")
        first = snapshot(conn)

        note(conn, 2, frag=anchor, value="rose", author="p2", channel="c2")
        second = snapshot(conn)

        moved = converted(first, second)
        assert len(moved["converted"]) == 1
        assert "note|rose" in moved["converted"][0]

    def test_a_new_singleton_is_not_a_conversion(self, anchor_and_others):
        from fragrance_graph.coverage import converted, snapshot

        conn, anchor, _ = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        first = snapshot(conn)

        note(conn, 2, frag=anchor, value="lychee", author="p2")
        moved = converted(first, snapshot(conn))
        assert moved["converted"] == []
        assert len(moved["new_singleton"]) == 1

    def test_a_fact_arriving_already_repeated_is_its_own_category(
        self, anchor_and_others
    ):
        """Two people saying a new thing in one batch is real evidence and
        is not a conversion — counting it as one would credit enrichment
        with turning an opinion into agreement that was never an opinion."""
        from fragrance_graph.coverage import converted, snapshot

        conn, anchor, _ = anchor_and_others
        note(conn, 1, frag=anchor, value="rose", author="p1")
        first = snapshot(conn)

        note(conn, 2, frag=anchor, value="lychee", author="p2", channel="c2")
        note(conn, 3, frag=anchor, value="lychee", author="p3", channel="c3")
        moved = converted(first, snapshot(conn))
        assert moved["converted"] == []
        assert len(moved["new_repeated"]) == 1

    def test_it_sees_conversions_on_bottles_nobody_enriched(
        self, anchor_and_others
    ):
        """Cross-bottle gains are real and per-bottle diffs hide them."""
        from fragrance_graph.coverage import converted, snapshot

        conn, anchor, others = anchor_and_others
        note(conn, 1, frag=others[0], value="vanilla", author="p1", channel="c1")
        first = snapshot(conn)

        note(conn, 2, frag=others[0], value="vanilla", author="p2", channel="c2")
        assert len(converted(first, snapshot(conn))["converted"]) == 1


class TestTheAnchorIsTheStrongestFactNotTheMostPopulous:
    """`attribute_facts` sorts by strength then **people**; the baseline
    test is on **creators**. So a wording with more people on one channel
    sorts ahead of a wording with fewer people across two, and the anchor
    is judged on the one that cannot satisfy the gate.

    Surfaced on the real corpus while building the neighbour experiment,
    before it spent anything: BR540 carries `sweet` and `deliciousness
    decadence a little sweet the depth` on two different channels, and the
    experiment's gate disagreed with `relative_coverage` about the same
    bottle. Left alone, a run that added the second creator to `sweet`
    could have been reported as no gain at all.
    """

    def test_more_people_on_one_channel_does_not_outrank_two_channels(
        self, anchor_and_others
    ):
        conn, anchor, _ = anchor_and_others
        # Three people, one channel. Sorts first, cannot clear MIN_SOURCES.
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, 10 + i, frag=anchor, value="deep rose and amber",
                 author=author, channel="chan_a")
        # Two people, two channels. This is the evidence that counts.
        note(conn, 20, frag=anchor, value="rose", author="p4", channel="chan_b")
        note(conn, 21, frag=anchor, value="rose", author="p5", channel="chan_c")

        row = relative_coverage(conn, cases=CASE)[0]

        assert row.anchor_creators == 2, (
            "the two-channel fact is what the gate tests; a louder "
            "single-channel wording must not stand in for it"
        )
        assert row.baseline_usable

    def test_it_still_reports_a_genuinely_thin_anchor_as_thin(
        self, anchor_and_others
    ):
        """The fix must not manufacture strength that is not there."""
        conn, anchor, _ = anchor_and_others
        note(conn, 1, frag=anchor, value="deep rose and amber",
             author="p1", channel="chan_a")
        note(conn, 2, frag=anchor, value="rose", author="p2", channel="chan_a")

        row = relative_coverage(conn, cases=CASE)[0]

        assert row.anchor_creators == 1
        assert not row.baseline_usable
        assert "1 creator" in row.blocker
