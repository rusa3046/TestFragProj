"""Grading what the corpus supports about one bottle.

The interesting tests are the ones about *restraint*: a fact backed by one
person must never come back declarable, a disputed fact must never come
back as agreement, and an inferred attribution must never be able to
masquerade as something a commenter said.
"""

import json

import pytest

from fragrance_graph.evidence import (
    Attribution,
    Strength,
    Support,
    attribute_facts,
    coverage,
    strength_of,
)
from fragrance_graph.ingest.store import ingest
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def say(conn, i, *, subject_id, claim_type, value, author, channel="chan_a",
        sentiment="POSITIVE", polarity="ASSERTED", video="v1"):
    """One person asserting one attribute of one bottle."""
    body = f"comment {i}: {value or sentiment}"
    ingest(conn, [make_comment(
        i, body=body, source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": video}),
    )])
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
        VALUES (%s, %s, 'FRAGRANCE', 'it', %s, %s, %s, %s, 0.9, %s, 1, %s,
                'test', '2026-01-01')
        """,
        (cid, claim_type, subject_id,
         "TAG" if value else "NONE", value, sentiment, body, polarity),
    )
    conn.commit()
    return conn.execute("SELECT max(id) FROM claims").fetchone()[0]


def floating(conn, i, *, claim_type, value, author, channel="chan_a", video="v1"):
    """A claim whose subject nobody named — the "this one" case."""
    body = f"comment {i}: this is {value}"
    ingest(conn, [make_comment(
        i, body=body, source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": video}),
    )])
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
        VALUES (%s, %s, 'FRAGRANCE', 'this', NULL, 'TAG', %s, 'POSITIVE',
                0.9, %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, claim_type, value, body),
    )
    conn.commit()
    return conn.execute("SELECT max(id) FROM claims").fetchone()[0]


def infer(conn, claim_id, fragrance_id, *, status="proposed"):
    conn.execute(
        """
        INSERT INTO claim_attributions
            (claim_id, role, fragrance_id, method, evidence, confidence,
             review_status, created_at)
        VALUES (%s, 'subject', %s, 'video_subject', 'title', 0.95, %s,
                '2026-01-01')
        """,
        (claim_id, fragrance_id, status),
    )
    conn.commit()


@pytest.fixture
def bottle(conn):
    return conn, add_fragrance(conn, "Parfums de Marly Delina")


# --- grading ---------------------------------------------------------------


class TestGrading:
    def test_one_person_is_observed_not_declarable(self):
        s = strength_of(Support(people=1, creators=1), Support())
        assert s is Strength.OBSERVED
        assert s.may_retrieve, "one person is a reason to look"
        assert not s.may_declare, "one person is not a reason to assert"

    def test_two_people_repeat_but_still_cannot_declare(self):
        s = strength_of(Support(people=2, creators=2), Support())
        assert s is Strength.REPEATED
        assert not s.may_declare

    def test_the_pair_gate_is_the_declarable_bar(self):
        s = strength_of(Support(people=3, creators=2), Support())
        assert s is Strength.SUPPORTED
        assert s.may_declare

    def test_three_people_on_one_creator_is_one_room(self):
        """Same rule the pair product uses: one audience is one source."""
        assert strength_of(Support(people=3, creators=1), Support()) is Strength.REPEATED

    def test_nothing_is_insufficient(self):
        s = strength_of(Support(), Support())
        assert s is Strength.INSUFFICIENT
        assert not s.may_retrieve

    def test_a_dispute_outranks_the_gate(self):
        """9 for and 8 against is not agreement, however many said it."""
        s = strength_of(Support(people=9, creators=4), Support(people=8, creators=3))
        assert s is Strength.CONTESTED
        assert not s.may_declare, "a disputed fact must never be stated flatly"

    def test_a_lone_dissenter_is_a_caveat_not_a_dispute(self):
        s = strength_of(Support(people=9, creators=4), Support(people=1, creators=1))
        assert s is Strength.SUPPORTED

    def test_two_dissenters_among_thirty_is_still_not_a_dispute(self):
        """The ratio matters as well as the count."""
        s = strength_of(Support(people=30, creators=9), Support(people=2, creators=2))
        assert s is Strength.SUPPORTED

    def test_canonical_never_outranks_community_evidence(self):
        """Official metadata is a different kind of fact, not a stronger one.
        A caller asking for consensus must not be handed a note list."""
        assert Strength.CANONICAL < Strength.OBSERVED
        assert Strength.CANONICAL.may_declare


# --- aggregation -----------------------------------------------------------


class TestAggregation:
    def test_three_people_on_two_creators_becomes_supported(self, bottle):
        conn, frag = bottle
        for i, (author, chan) in enumerate(
            [("p1", "chan_a"), ("p2", "chan_a"), ("p3", "chan_b")]
        ):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=chan)
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert (fact.attribute, fact.value) == ("note", "rose")
        assert fact.strength is Strength.SUPPORTED
        assert fact.supporting.people == 3
        assert len(fact.supporting.claim_ids) == 3, "provenance is kept"

    def test_one_person_saying_it_three_ways_is_one_person(self, bottle):
        conn, frag = bottle
        for i, value in enumerate(["rose", "rose note", "roses"]):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value=value, author="p1", channel="chan_a")
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.supporting.people == 1
        assert fact.strength is Strength.OBSERVED

    def test_spellings_of_one_note_are_one_fact(self, bottle):
        conn, frag = bottle
        for i, (value, author) in enumerate(
            [("coffee", "p1"), ("coffee note", "p2"), ("coffee notes", "p3")]
        ):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value=value, author=author, channel=f"chan_{i}")
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.value == "coffee"
        assert fact.supporting.people == 3

    def test_a_denial_opposes_rather_than_supports(self, bottle):
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=f"chan_{i}")
        for i, author in enumerate(["p4", "p5"], start=10):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=f"chan_{i}",
                polarity="DENIED")
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.supporting.people == 3
        assert fact.opposing.people == 2
        assert fact.strength is Strength.CONTESTED


class TestSentimentAxes:
    """Longevity has two ends, not two unrelated values."""

    def test_the_two_ends_become_one_contested_fact(self, bottle):
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2"]):
            say(conn, i, subject_id=frag, claim_type="LONGEVITY", value=None,
                author=author, channel=f"chan_{i}", sentiment="POSITIVE")
        for i, author in enumerate(["p3", "p4"], start=10):
            say(conn, i, subject_id=frag, claim_type="LONGEVITY", value=None,
                author=author, channel=f"chan_{i}", sentiment="NEGATIVE")
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.attribute == "longevity"
        assert fact.supporting.people == 2
        assert fact.opposing.people == 2
        assert fact.strength is Strength.CONTESTED

    def test_an_axis_is_named_by_its_positive_end(self, bottle):
        """Naming it by the majority end produced labels that contradicted
        their own counts — "short lived, 1 for and 3 against"."""
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject_id=frag, claim_type="LONGEVITY", value=None,
                author=author, channel=f"chan_{i}", sentiment="NEGATIVE")
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.value == "long lasting"
        assert (fact.supporting.people, fact.opposing.people) == (0, 3)
        assert fact.strength is Strength.INSUFFICIENT, (
            "nobody asserted the positive end, so there is nothing to assert"
        )


# --- the provenance boundary -----------------------------------------------


class TestInferredAttributionIsNotStatedAttribution:
    """The rule the whole module exists to hold."""

    def test_stated_only_ignores_an_inference(self, bottle):
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            infer(conn, cid, frag)
        assert attribute_facts(conn, fragrance_id=frag) == []

    def test_proposed_inferences_can_retrieve(self, bottle):
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            infer(conn, cid, frag)
        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )
        assert fact.supporting.people == 3
        assert fact.inferred, "and the fact says so, permanently"

    def test_accepted_excludes_the_merely_proposed(self, bottle):
        conn, frag = bottle
        cid = floating(conn, 0, claim_type="NOTE_DESCRIPTOR", value="rose",
                       author="p1")
        infer(conn, cid, frag, status="proposed")
        assert attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.ACCEPTED
        ) == []

    def test_a_rejected_inference_never_counts(self, bottle):
        conn, frag = bottle
        cid = floating(conn, 0, claim_type="NOTE_DESCRIPTOR", value="rose",
                       author="p1")
        infer(conn, cid, frag, status="rejected")
        assert attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        ) == []

    def test_what_a_commenter_said_beats_what_a_machine_inferred(self, bottle):
        """Where both exist and disagree, the person is right by definition."""
        conn, frag = bottle
        other = add_fragrance(conn, "Lattafa Khamrah")
        cid = say(conn, 0, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                  value="rose", author="p1")
        infer(conn, cid, other)
        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )
        assert fact.fragrance_id == frag
        assert not fact.inferred
        assert attribute_facts(
            conn, fragrance_id=other, attribution=Attribution.PROPOSED
        ) == []

    def test_a_fact_built_only_from_inference_is_flagged(self, bottle):
        conn, frag = bottle
        say(conn, 0, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
            value="rose", author="p1")
        cid = floating(conn, 1, claim_type="NOTE_DESCRIPTOR", value="rose",
                       author="p2", channel="chan_b")
        infer(conn, cid, frag)
        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )
        assert fact.supporting.people == 2
        assert fact.inferred, "one inferred contributor taints the whole fact"


class TestCoverage:
    def test_it_counts_bottles_not_only_facts(self, bottle):
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=f"chan_{i}")
        report = coverage(conn)
        assert report["fragrances_with_any_fact"] == 1
        assert report["fragrances_declarable"] == 1
        assert report["SUPPORTED"] == 1


class TestInferenceCannotBeLaundered:
    """Codex P1, 2026-08-15. `Strength` counts heads; only the fact knows
    where the heads came from. A fact standing entirely on machine
    attribution can clear the head count and must still never be stated."""

    def _inferred_supported(self, conn, frag):
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            infer(conn, cid, frag)

    def test_an_inferred_fact_clears_the_head_count(self, bottle):
        conn, frag = bottle
        self._inferred_supported(conn, frag)
        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )
        assert fact.strength is Strength.SUPPORTED
        assert fact.strength.may_declare, "the head count alone says yes"

    def test_but_the_fact_still_refuses_to_be_declared(self, bottle):
        conn, frag = bottle
        self._inferred_supported(conn, frag)
        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )
        assert not fact.may_declare, (
            "counting heads does not make a machine's reading of a video "
            "title into something a commenter said"
        )

    def test_accepting_the_inference_does_not_make_it_declarable(self, bottle):
        """Acceptance moves a claim into the counts. It does not change who
        attributed it."""
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            infer(conn, cid, frag, status="accepted")
        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.ACCEPTED
        )
        assert fact.strength is Strength.SUPPORTED
        assert not fact.may_declare

    def test_coverage_does_not_count_it_as_declarable(self, bottle):
        conn, frag = bottle
        self._inferred_supported(conn, frag)
        report = coverage(conn, attribution=Attribution.PROPOSED)
        assert report["SUPPORTED"] == 1, "it is still strong evidence"
        assert report["fragrances_declarable"] == 0, "it is still not sayable"
        assert report["facts_resting_on_inference"] == 1

    def test_a_wholly_stated_fact_is_unaffected(self, bottle):
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=f"chan_{i}")
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.may_declare


class TestInferenceAttributesBothSidesOfADispute:
    """Codex P2, 2026-08-15, raised as P2 and treated as P1 here.

    `FLOATING_SQL` filtered `polarity = 'ASSERTED'`, so only the supporting
    half of a disagreement ever received inferred attribution. The bias runs
    one way — inference could make a fact look stronger and never weaker —
    which is exactly the direction the whole module exists to prevent.
    """

    def test_a_floating_denial_is_attributed_too(self, bottle):
        from fragrance_graph.attributes import record_inferences

        conn, frag = bottle
        conn.execute(
            "INSERT INTO videos (source, video_id, title) "
            "VALUES ('youtube', 'v1', 'Parfums de Marly Delina Review')"
        )
        conn.commit()
        for i, author in enumerate(["p1", "p2", "p3"]):
            floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                     author=author, channel=f"chan_{i}")
        for i, author in enumerate(["p4", "p5"], start=10):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            conn.execute(
                "UPDATE claims SET polarity = 'DENIED' WHERE id = %s", (cid,)
            )
        conn.commit()

        record_inferences(conn)
        attributed = conn.execute(
            "SELECT count(*) FROM claim_attributions a JOIN claims c "
            "ON c.id = a.claim_id WHERE c.polarity = 'DENIED'"
        ).fetchone()[0]
        assert attributed == 2, "a denial names its bottle like anything else"

    def test_the_dispute_survives_into_the_fact(self, bottle):
        from fragrance_graph.attributes import record_inferences

        conn, frag = bottle
        conn.execute(
            "INSERT INTO videos (source, video_id, title) "
            "VALUES ('youtube', 'v1', 'Parfums de Marly Delina Review')"
        )
        conn.commit()
        for i, author in enumerate(["p1", "p2", "p3"]):
            floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                     author=author, channel=f"chan_{i}")
        for i, author in enumerate(["p4", "p5"], start=10):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            conn.execute(
                "UPDATE claims SET polarity = 'DENIED' WHERE id = %s", (cid,)
            )
        conn.commit()
        record_inferences(conn)

        (fact,) = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )
        assert (fact.supporting.people, fact.opposing.people) == (3, 2)
        assert fact.strength is Strength.CONTESTED, (
            "3 for and 2 against is a dispute, not a consensus"
        )

    def test_inference_never_removes_declarability_it_only_adds_reach(self, bottle):
        """The correction to the first fix. Three commenters who named the
        bottle themselves clear the gate; two more attributed by machine
        must not take that away."""
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=f"chan_{i}")
        for i, author in enumerate(["p4", "p5"], start=10):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            infer(conn, cid, frag)

        stated_only = attribute_facts(conn, fragrance_id=frag)[0]
        with_inference = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )[0]

        assert stated_only.may_declare
        assert with_inference.may_declare, "the stated three still clear it"
        assert with_inference.supporting.people == 5, "and reach is wider"
        assert with_inference.stated.people == 3
        assert with_inference.inferred

    def test_an_inferred_denial_may_block_a_statement(self, bottle):
        """Asymmetric on purpose: inference cannot support a claim but may
        raise enough doubt to stop one. Failing toward silence is safe."""
        conn, frag = bottle
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject_id=frag, claim_type="NOTE_DESCRIPTOR",
                value="rose", author=author, channel=f"chan_{i}")
        for i, author in enumerate(["p4", "p5"], start=20):
            cid = floating(conn, i, claim_type="NOTE_DESCRIPTOR", value="rose",
                           author=author, channel=f"chan_{i}")
            conn.execute(
                "UPDATE claims SET polarity = 'DENIED' WHERE id = %s", (cid,)
            )
            infer(conn, cid, frag)
        conn.commit()

        fact = attribute_facts(
            conn, fragrance_id=frag, attribution=Attribution.PROPOSED
        )[0]
        assert fact.strength is Strength.CONTESTED
        assert not fact.may_declare
