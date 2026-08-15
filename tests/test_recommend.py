"""Choosing bottles from evidence, and saying why.

The tests that matter are about what the output is *allowed to say*. A
recommendation built from one person's remark must read as one person's
remark; a hard constraint must filter rather than discount; and an empty
answer must explain itself rather than returning the whole catalogue.
"""

import json

import pytest

from fragrance_graph.evidence import Strength
from fragrance_graph.ingest.store import ingest
from fragrance_graph.recommend import Reason, recommend
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def note(conn, i, *, frag, value, author, channel="chan_a", claim_type="NOTE_DESCRIPTOR"):
    body = f"comment {i}: {value}"
    ingest(conn, [make_comment(
        i, body=body, source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": "v1"}),
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
        VALUES (%s, %s, 'FRAGRANCE', 'it', %s, %s, %s, 'POSITIVE', 0.9,
                %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, claim_type, frag, "TAG" if value else "NONE", value, body),
    )
    conn.commit()


@pytest.fixture
def catalogue(conn):
    a = add_fragrance(conn, "Kilian Angels' Share", aliases=["Angels Share"])
    b = add_fragrance(conn, "Lattafa Khamrah", aliases=["Khamrah"])
    return conn, a, b


class TestHardConstraintsFilter:
    def test_a_bottle_without_the_note_is_not_a_candidate(self, catalogue):
        conn, a, b = catalogue
        note(conn, 1, frag=a, value="raspberry", author="p1")
        note(conn, 2, frag=b, value="coffee", author="p2")
        answer = recommend(conn, "a fragrance with raspberry")
        assert [r.name for r in answer.results] == ["Kilian Angels' Share"]

    def test_missing_it_is_a_rejection_not_a_low_score(self, catalogue):
        conn, a, b = catalogue
        note(conn, 1, frag=b, value="coffee", author="p1")
        answer = recommend(conn, "a fragrance with raspberry")
        assert answer.results == []

    def test_an_empty_answer_explains_itself(self, catalogue):
        conn, a, b = catalogue
        note(conn, 1, frag=b, value="coffee", author="p1")
        answer = recommend(conn, "a fragrance with raspberry")
        assert "no bottle in the corpus" in answer.note.lower()

    def test_absence_is_not_reported_as_absence_of_the_note(self, catalogue):
        """Nobody mentioning raspberry is not the same as no raspberry, and
        the wording must not claim otherwise."""
        conn, a, b = catalogue
        note(conn, 1, frag=b, value="coffee", author="p1")
        answer = recommend(conn, "a fragrance with raspberry")
        assert "not that no such fragrance exists" in answer.note


class TestWordingMatchesEvidence:
    """The provenance discipline arrives in the phrasing or nowhere."""

    def test_one_person_reads_as_one_person(self, catalogue):
        conn, a, _ = catalogue
        note(conn, 1, frag=a, value="raspberry", author="p1")
        (result,) = recommend(conn, "a fragrance with raspberry").results
        assert "one commenter said" in result.reasons[0].phrase()

    def test_three_people_on_two_channels_reads_as_a_fact(self, catalogue):
        conn, a, _ = catalogue
        for i, (author, chan) in enumerate(
            [("p1", "c1"), ("p2", "c2"), ("p3", "c3")]
        ):
            note(conn, i, frag=a, value="raspberry", author=author, channel=chan)
        (result,) = recommend(conn, "a fragrance with raspberry").results
        phrase = result.reasons[0].phrase()
        assert "3 people across 3 channels" in phrase
        assert "one commenter" not in phrase

    def test_an_inferred_reason_never_reads_as_consensus(self):
        reason = Reason(
            kind="prefer", text="raspberry", strength=Strength.SUPPORTED,
            people=3, creators=2, inferred=True,
        )
        assert not reason.declarable
        assert "rather than naming it" in reason.phrase()
        assert "3 people said" in reason.phrase(), "the count is still honest"

    def test_a_canonical_note_is_labelled_as_official(self):
        reason = Reason(kind="prefer", text="rose", strength=Strength.CANONICAL)
        assert "official listing" in reason.phrase()

    def test_a_weak_answer_says_so(self, catalogue):
        conn, a, _ = catalogue
        note(conn, 1, frag=a, value="raspberry", author="p1")
        answer = recommend(conn, "a fragrance with raspberry")
        assert "single observations" in answer.note


class TestNegativePreferences:
    def test_the_thing_being_avoided_becomes_a_caveat(self, catalogue):
        conn, a, b = catalogue
        note(conn, 1, frag=a, value="rose", author="p1")
        note(conn, 2, frag=a, value="raspberry", author="p2")
        note(conn, 3, frag=b, value="raspberry", author="p3")
        answer = recommend(conn, "a raspberry fragrance but less rose")
        names = [r.name for r in answer.results]
        assert names.index("Lattafa Khamrah") < names.index("Kilian Angels' Share")
        rosy = next(r for r in answer.results if r.name == "Kilian Angels' Share")
        assert any("rose" in c.text for c in rosy.caveats)

    def test_a_caveat_is_never_listed_as_a_reason(self, catalogue):
        conn, a, _ = catalogue
        note(conn, 1, frag=a, value="rose", author="p1")
        note(conn, 2, frag=a, value="raspberry", author="p2")
        answer = recommend(conn, "a raspberry fragrance but less rose")
        (result,) = answer.results
        assert "rose" not in " ".join(r.text for r in result.reasons)


class TestCanonicalMetadata:
    """Official facts and perceived facts are different claims."""

    def test_a_note_in_the_name_is_canonical_evidence(self, conn):
        from fragrance_graph.evidence import canonical_facts

        add_fragrance(conn, "Swiss Arabian Rose 01")
        (fact,) = canonical_facts(conn)
        assert (fact.attribute, fact.value) == ("note", "rose")
        assert fact.strength is Strength.CANONICAL

    def test_it_needs_no_commenter(self, conn):
        from fragrance_graph.evidence import canonical_facts

        add_fragrance(conn, "Swiss Arabian Rose 01")
        (fact,) = canonical_facts(conn)
        assert fact.supporting.people == 0
        assert fact.may_declare, "the house said it; no head count applies"

    def test_it_demotes_a_bottle_the_asker_wants_less_of(self, conn):
        """The defect it was built for: with no community rose evidence,
        'Rose 01' was recommended to someone asking for less rose."""
        add_fragrance(conn, "Swiss Arabian Rose 01")
        add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=1, value="raspberry", author="p1")
        note(conn, 2, frag=2, value="raspberry", author="p2")
        answer = recommend(conn, "a raspberry fragrance but less rose")
        names = [r.name for r in answer.results]
        assert names.index("Lattafa Khamrah") < names.index("Swiss Arabian Rose 01")

    def test_official_and_perceived_stay_separate_rows(self, conn):
        from fragrance_graph.evidence import attribute_facts, canonical_facts

        frag = add_fragrance(conn, "Swiss Arabian Rose 01")
        note(conn, 1, frag=frag, value="rose", author="p1")
        community = [f for f in attribute_facts(conn) if f.value == "rose"]
        official = [f for f in canonical_facts(conn) if f.value == "rose"]
        assert len(community) == 1 and len(official) == 1
        assert community[0].strength is Strength.OBSERVED
        assert official[0].strength is Strength.CANONICAL


class TestRefusalAndIntent:
    def test_an_unparseable_request_returns_the_refusal(self, catalogue):
        conn, _, _ = catalogue
        answer = recommend(conn, "hello there")
        assert answer.results == []
        assert answer.note

    def test_a_profile_request_is_not_answered_by_the_recommender(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        answer = recommend(conn, "what do people disagree about for Delina?")
        assert answer.results == []
        assert "profile" in answer.note

    def test_the_anchor_is_never_recommended_back(self, conn):
        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        note(conn, 1, frag=anchor, value="raspberry", author="p1")
        answer = recommend(conn, "something like Delina with raspberry")
        assert "Parfums de Marly Delina" not in [r.name for r in answer.results]


class TestEvidenceCounts:
    def test_graph_support_counts_as_people(self, conn):
        """A candidate standing on the comparison graph reported 0 people,
        while resting on the best-evidenced thing in the corpus."""
        from tests.test_query import add_claim, add_comment

        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        other = add_fragrance(conn, "Maison Alhambra Delilah")
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = add_comment(conn, i, body="x", author=author)
            add_claim(conn, cid, subject=other, obj=anchor)
        answer = recommend(conn, "something like Delina")
        (result,) = [r for r in answer.results if r.name == "Maison Alhambra Delilah"]
        assert result.people == 3


class TestDisputesAreDisclosed:
    """A fact people argue about must never be cited as flat agreement."""

    def _contested(self, conn, frag, *, for_people, against_people):
        for i, author in enumerate(for_people):
            note(conn, i, frag=frag, value=None, author=author,
                 channel=f"c{i}", claim_type="PROJECTION")
        for i, author in enumerate(against_people, start=50):
            note(conn, i, frag=frag, value=None, author=author,
                 channel=f"c{i}", claim_type="PROJECTION")
            conn.execute(
                "UPDATE claims SET sentiment = 'NEGATIVE' WHERE id = "
                "(SELECT max(id) FROM claims)"
            )
        conn.commit()

    def test_a_dispute_is_stated_in_the_phrase(self, catalogue):
        conn, a, _ = catalogue
        note(conn, 90, frag=a, value="raspberry", author="p0")
        self._contested(conn, a, for_people=["p1", "p2", "p3"],
                        against_people=["p4", "p5"])
        (result,) = recommend(conn, "a raspberry with strong projection").results
        phrases = [r.phrase() for r in result.reasons + result.caveats]
        assert any("disagree" in p for p in phrases)

    def test_a_mostly_denied_preference_is_a_caveat_not_a_reason(self, catalogue):
        """More against than for cannot be a reason to pick the bottle."""
        conn, a, _ = catalogue
        note(conn, 90, frag=a, value="raspberry", author="p0")
        self._contested(conn, a, for_people=["p1", "p2"],
                        against_people=["p3", "p4", "p5"])
        (result,) = recommend(conn, "a raspberry with strong projection").results
        assert not any("projection" in r.text for r in result.reasons)
        assert any("projection" in c.text for c in result.caveats)
