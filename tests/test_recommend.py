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

    def test_a_one_person_answer_says_it_is_one_person(self, catalogue):
        conn, a, _ = catalogue
        note(conn, 1, frag=a, value="raspberry", author="p1")
        answer = recommend(conn, "a fragrance with raspberry")
        assert "single person" in answer.note

    def test_several_people_in_one_room_is_not_called_one_person(self, catalogue):
        """Reworded after the phase-4 review: "no declarable reason" was
        being described as "single observations", which contradicted a
        result printed directly beneath saying "3 people across 1 channel"."""
        conn, a, _ = catalogue
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=a, value="raspberry", author=author,
                 channel="one_channel")
        answer = recommend(conn, "a fragrance with raspberry")
        assert "single" not in answer.note
        assert "independence" in answer.note


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


class TestNameDerivedNotesDemoteButNeverAssert:
    """Rewritten after the phase-3 review.

    This class used to pin the opposite behaviour: a note word found in a
    product name was graded CANONICAL and printed as "(official listing)".
    On the real catalogue that produced "Creed Green Irish Tweed -> green
    (official listing)", which asserts a provenance nobody has. The signal
    is kept, the assertion is not.
    """

    def test_a_note_in_the_name_is_recognised(self, conn):
        from fragrance_graph.evidence import name_facts

        add_fragrance(conn, "Swiss Arabian Rose 01")
        (fact,) = name_facts(conn)
        assert (fact.attribute, fact.value) == ("note", "rose")
        assert fact.from_name

    def test_it_is_never_declarable(self, conn):
        from fragrance_graph.evidence import name_facts

        add_fragrance(conn, "Creed Green Irish Tweed")
        (fact,) = name_facts(conn)
        assert fact.strength is Strength.INSUFFICIENT
        assert not fact.may_declare, (
            "nobody official listed a green note in Green Irish Tweed"
        )

    def test_it_cannot_satisfy_a_hard_requirement(self, conn):
        """A word in a name is not evidence anybody smelled it."""
        add_fragrance(conn, "Swiss Arabian Rose 01")
        answer = recommend(conn, "a fragrance with rose")
        assert answer.results == []

    def test_it_still_demotes_what_the_asker_wants_less_of(self, conn):
        """The defect it was built for, and the only job it keeps."""
        add_fragrance(conn, "Swiss Arabian Rose 01")
        add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=1, value="raspberry", author="p1")
        note(conn, 2, frag=2, value="raspberry", author="p2")
        answer = recommend(conn, "a raspberry fragrance but less rose")
        names = [r.name for r in answer.results]
        assert names.index("Lattafa Khamrah") < names.index("Swiss Arabian Rose 01")

    def test_a_name_note_and_a_perceived_note_stay_separate(self, conn):
        from fragrance_graph.evidence import attribute_facts, name_facts

        frag = add_fragrance(conn, "Swiss Arabian Rose 01")
        note(conn, 1, frag=frag, value="rose", author="p1")
        community = [f for f in attribute_facts(conn) if f.value == "rose"]
        from_name = [f for f in name_facts(conn) if f.value == "rose"]
        assert len(community) == 1 and len(from_name) == 1
        assert community[0].strength is Strength.OBSERVED
        assert from_name[0].strength is Strength.INSUFFICIENT


class TestRefusalAndIntent:
    def test_an_unparseable_request_returns_the_refusal(self, catalogue):
        conn, _, _ = catalogue
        answer = recommend(conn, "hello there")
        assert answer.results == []
        assert answer.note

    def test_a_profile_request_returns_a_profile(self, conn):
        """Changed in phase 4: profile used to be declined outright. It is
        now answered from the same evidence, led by what is contested."""
        frag = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        note(conn, 1, frag=frag, value="rose", author="p1")
        answer = recommend(conn, "what do people disagree about for Delina?")
        (result,) = answer.results
        assert result.name == "Parfums de Marly Delina"

    def test_a_profile_of_an_undescribed_bottle_says_so(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        answer = recommend(conn, "what do people disagree about for Delina?")
        assert answer.results == []
        assert "Nothing in the corpus describes" in answer.note

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


class TestCodexPhase3Findings:
    """Five findings from the 2026-08-15 recommender review, all confirmed."""

    def test_an_unknown_channel_is_not_a_second_channel(self, conn):
        """P1. A NULL `source_channel` joined the creator set, so two
        comments from one channel plus one unattributed comment cleared
        MIN_SOURCES and declared consensus."""
        from fragrance_graph.evidence import attribute_facts

        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1", channel="chan_a")
        note(conn, 2, frag=frag, value="rose", author="p2", channel="chan_a")
        note(conn, 3, frag=frag, value="rose", author="p3", channel="chan_a")
        conn.execute(
            "UPDATE comments SET source_channel = '' WHERE source_id = 't1_fake00003'"
        )
        conn.commit()
        (fact,) = attribute_facts(conn, fragrance_id=frag)
        assert fact.supporting.people == 3
        assert fact.supporting.creators == 1, "one known channel is one room"
        assert not fact.may_declare

    def test_a_mostly_denied_fact_cannot_satisfy_a_requirement(self, catalogue):
        """P1. `_satisfies_hard` accepted any retrievable strength, so a
        bottle three people call weak satisfied a demand for strong.

        Asserted against `_satisfies_hard` directly: "strong projection"
        parses to a soft preference, not a hard constraint, so going
        through `recommend` would exercise the soft path instead — which
        has its own test below.
        """
        from fragrance_graph.evidence import attribute_facts
        from fragrance_graph.plan import Constraint
        from fragrance_graph.recommend import _satisfies_hard

        conn, a, _ = catalogue
        for i, author in enumerate(["p1", "p2"]):
            note(conn, i, frag=a, value=None, author=author, channel=f"c{i}",
                 claim_type="PROJECTION")
        for i, author in enumerate(["p3", "p4", "p5"], start=50):
            note(conn, i, frag=a, value=None, author=author, channel=f"c{i}",
                 claim_type="PROJECTION")
            conn.execute(
                "UPDATE claims SET sentiment = 'NEGATIVE' "
                "WHERE id = (SELECT max(id) FROM claims)"
            )
        conn.commit()
        facts = attribute_facts(conn, fragrance_id=a)
        assert _satisfies_hard(facts, Constraint("projection", "strong")) is None

    def test_a_mostly_denied_preference_is_a_caveat(self, catalogue):
        """The soft path equivalent, through the whole recommender."""
        conn, a, _ = catalogue
        note(conn, 80, frag=a, value="raspberry", author="p0")
        for i, author in enumerate(["p1", "p2"]):
            note(conn, i, frag=a, value=None, author=author, channel=f"c{i}",
                 claim_type="PROJECTION")
        for i, author in enumerate(["p3", "p4", "p5"], start=50):
            note(conn, i, frag=a, value=None, author=author, channel=f"c{i}",
                 claim_type="PROJECTION")
            conn.execute(
                "UPDATE claims SET sentiment = 'NEGATIVE' "
                "WHERE id = (SELECT max(id) FROM claims)"
            )
        conn.commit()
        (result,) = recommend(conn, "a raspberry with strong projection").results
        assert not any("projection" in r.text for r in result.reasons)
        assert any("projection" in c.text for c in result.caveats)

    def test_graph_evidence_respects_creator_independence(self, conn):
        """P1. Three commenters in one creator's section were graded
        SUPPORTED, skipping the independence bar the product rests on."""
        from tests.test_query import add_claim, add_comment

        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        other = add_fragrance(conn, "Maison Alhambra Delilah")
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = add_comment(conn, i, body="x", author=author)
            add_claim(conn, cid, subject=other, obj=anchor)
        (result,) = recommend(conn, "something like Delina").results
        graph = result.reasons[0]
        assert graph.people == 3
        assert graph.creators == 1
        assert graph.strength is Strength.OBSERVED, "one room is not consensus"
        assert not graph.declarable

    def test_avoiding_a_trait_outranks_being_near_the_anchor(self, conn):
        """P2. Anchor proximity (+2.6) outweighed an avoided observed trait
        (-1.2), so the rosy bottle beat the one with no rose."""
        from tests.test_query import add_claim, add_comment

        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        rosy = add_fragrance(conn, "Kilian Angels' Share")
        clean = add_fragrance(conn, "Lattafa Khamrah")
        for i, author in enumerate(["p1", "p2", "p3"]):
            cid = add_comment(conn, i, body="x", author=author)
            add_claim(conn, cid, subject=rosy, obj=anchor)
        note(conn, 20, frag=rosy, value="rose", author="p9")
        note(conn, 21, frag=rosy, value="raspberry", author="p8")
        note(conn, 22, frag=clean, value="raspberry", author="p7")

        answer = recommend(conn, "something like Delina with raspberry but less rose")
        names = [r.name for r in answer.results]
        assert names.index("Lattafa Khamrah") < names.index("Kilian Angels' Share")


class TestAComparativeIsNotAnAvoidance:
    """"Less rose than Delina" and "avoid rose" are different requests.

    Somebody who loves Delina wants a bottle like it with the rose dialled
    down. The avoidance reading throws away everything they said they
    liked and returns bottles with no rose and nothing else in common.
    """

    def _pair(self, conn):
        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        other = add_fragrance(conn, "Lattafa Khamrah")
        return anchor, other

    def test_it_compares_against_the_anchors_own_evidence(self, conn):
        anchor, other = self._pair(conn)
        for i, author in enumerate(["p1", "p2", "p3", "p4", "p5"]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=f"c{i}")
        note(conn, 20, frag=other, value="rose", author="p9")
        note(conn, 21, frag=other, value="dates", author="p8")

        (result,) = [
            r for r in recommend(
                conn, "i love Delina but the rose is too strong"
            ).results if r.name == "Lattafa Khamrah"
        ]
        comparative = [r for r in result.reasons if r.kind == "comparative"]
        assert comparative, "the comparison is the reason, not an avoidance"
        # Reworded in the final review: the sentence reports the two counts
        # rather than asserting "less rose than Delina", which is a
        # conclusion nobody wrote and the system inferred from mentions.
        assert "rose mentioned by" in comparative[0].text
        assert "Parfums de Marly Delina" in comparative[0].text
        assert "fewer" in comparative[0].text

    def test_a_similar_level_is_not_a_difference(self, conn):
        """Two against three is noise dressed as a comparison."""
        anchor, other = self._pair(conn)
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=f"c{i}")
        for i, author in enumerate(["p4", "p5"], start=20):
            note(conn, i, frag=other, value="rose", author=author,
                 channel=f"c{i}")
        note(conn, 40, frag=other, value="dates", author="p9")

        (result,) = [
            r for r in recommend(
                conn, "i love Delina but the rose is too strong"
            ).results if r.name == "Lattafa Khamrah"
        ]
        assert any(r.kind == "comparative" for r in result.caveats)
        assert "not a clear difference" in result.caveats[0].text

    def test_no_anchor_evidence_means_no_baseline(self, conn):
        """Not 'avoid rose'. There is nothing to be less than."""
        anchor, other = self._pair(conn)
        note(conn, 20, frag=other, value="rose", author="p9")
        answer = recommend(conn, "i love Delina but the rose is too strong")
        assert "Insufficient comparative evidence" in answer.note
        assert "not been answered as a different, easier one" in answer.note

    def test_absence_on_the_candidate_is_not_a_low_score(self, conn):
        """Nobody mentioning rose is not evidence there is less of it."""
        anchor, other = self._pair(conn)
        for i, author in enumerate(["p1", "p2", "p3", "p4", "p5"]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=f"c{i}")
        note(conn, 20, frag=other, value="dates", author="p9")

        results = recommend(
            conn, "i love Delina but the rose is too strong"
        ).results
        khamrah = [r for r in results if r.name == "Lattafa Khamrah"]
        if khamrah:
            assert not any(r.kind == "comparative" for r in khamrah[0].reasons)
            assert any("either way" in u for u in khamrah[0].unmatched)

    def test_more_x_than_anchor_works_the_other_way(self, conn):
        anchor, other = self._pair(conn)
        note(conn, 1, frag=anchor, value="vanilla", author="p1")
        for i, author in enumerate(["p2", "p3", "p4", "p5"], start=10):
            note(conn, i, frag=other, value="vanilla", author=author,
                 channel=f"c{i}")
        from fragrance_graph.plan import Direction, Preference
        from fragrance_graph.recommend import _prominence

        pref = Preference("note", "vanilla", Direction.MORE_THAN_ANCHOR)
        from fragrance_graph.evidence import attribute_facts

        assert _prominence(
            attribute_facts(conn, fragrance_id=other), pref
        ).people == 4
        assert _prominence(
            attribute_facts(conn, fragrance_id=anchor), pref
        ).people == 1


class TestCodexPhase6ComparativeFindings:
    """Two P1s from the 2026-08-15 release review, both confirmed.

    The independence rule held everywhere except the one path that
    produces a comparative sentence.
    """

    def _pair(self, conn):
        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        return anchor, add_fragrance(conn, "Lattafa Khamrah")

    def test_a_disputed_baseline_cannot_ground_a_comparison(self, conn):
        """3 say Delina is rosy, 10 say it is not. "Less rose than Delina"
        has no agreed left-hand side, and the dissenters were vanishing
        from both the arithmetic and the sentence."""
        anchor, other = self._pair(conn)
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=f"c{i}")
        for i, author in enumerate([f"q{n}" for n in range(10)], start=20):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=f"d{i}")
            conn.execute(
                "UPDATE claims SET polarity = 'DENIED' "
                "WHERE id = (SELECT max(id) FROM claims)"
            )
        conn.commit()
        note(conn, 60, frag=other, value="rose", author="z1")
        note(conn, 61, frag=other, value="dates", author="z2")

        results = recommend(
            conn, "i love Delina but the rose is too strong"
        ).results
        for result in results:
            assert not any(
                r.kind == "comparative" and "less rose" in r.text
                for r in result.reasons
            )

    def test_one_creators_audience_cannot_establish_a_difference(self, conn):
        """Three commenters in one channel is one room, everywhere else in
        this system. It was establishing "less rose" and earning a ranking
        bonus for it."""
        anchor, other = self._pair(conn)
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel="one_room")
        note(conn, 60, frag=other, value="rose", author="z1")
        note(conn, 61, frag=other, value="dates", author="z2")

        results = recommend(
            conn, "i love Delina but the rose is too strong"
        ).results
        for result in results:
            assert not any(r.kind == "comparative" for r in result.reasons)
        khamrah = [r for r in results if r.name == "Lattafa Khamrah"]
        if khamrah:
            assert any("no usable baseline" in u for u in khamrah[0].unmatched)

    def test_a_properly_independent_baseline_still_works(self, conn):
        """The fix must not disable comparatives, only unfounded ones."""
        anchor, other = self._pair(conn)
        for i, author in enumerate(["p1", "p2", "p3", "p4", "p5"]):
            note(conn, i, frag=anchor, value="rose", author=author,
                 channel=f"c{i}")
        note(conn, 60, frag=other, value="rose", author="z1")
        note(conn, 61, frag=other, value="dates", author="z2")

        (result,) = [
            r for r in recommend(
                conn, "i love Delina but the rose is too strong"
            ).results if r.name == "Lattafa Khamrah"
        ]
        (comparative,) = [r for r in result.reasons if r.kind == "comparative"]
        assert "rose mentioned by" in comparative.text
        assert "across 5 channels" in comparative.text, (
            "and the sentence shows the baseline's independence"
        )
