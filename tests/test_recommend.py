"""Choosing bottles from evidence, and saying why.

The tests that matter are about what the output is *allowed to say*. A
recommendation built from one person's remark must read as one person's
remark; a hard constraint must filter rather than discount; and an empty
answer must explain itself rather than returning the whole catalogue.
"""

import json

import pytest

from fragrance_graph.catalog_profile import CatalogProfile
from fragrance_graph.evidence import Strength
from fragrance_graph.ingest.store import ingest
from fragrance_graph.notes import DeclaredNote, store_claims
from fragrance_graph.plan import Direction, Preference, QueryPlan
from fragrance_graph.recommend import (
    Reason,
    Recommendation,
    _catalog_signal,
    digest,
    recommend,
    recommend_plan,
)
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


def declare(conn, frag, *raw_notes, claim_type="retailer_declared", source="test retailer"):
    """One bottle's declared notes, for `TestAnchorProfileFallback` —
    `fragrance_note_claim` rows, the retailer/brand half of the corpus,
    never a community claim."""
    store_claims(
        conn, frag, [DeclaredNote(n, "unspecified") for n in raw_notes],
        claim_type=claim_type, source_name=source,
    )


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


class TestDigest:
    """`digest()` — the two-sentence summary `ask._card` leads with.
    Composed only from what a card already carries; see the function's
    own docstring for the exclusions and rules that keep it safe and
    honestly worded rather than merely short.
    """

    def test_a_one_person_fact_renders_weakly(self):
        """User feedback asked for a summary; it did not ask for a
        one-person remark to start sounding like consensus. The whole
        provenance discipline `Reason.phrase()` enforces has to survive
        being folded into two sentences, or the digest undoes it."""
        weak = Reason(kind="prefer", text="raspberry",
                      strength=Strength.OBSERVED, people=1, creators=1)
        rec = Recommendation(fragrance_id=1, name="X", reasons=[weak],
                             people=1, creators=1)
        text = digest(rec)
        assert "one commenter said" in text
        assert "People mostly" not in text

    def test_no_distribution_word_is_used_anywhere(self):
        """"Mostly" claims a share of the card's reasons this function
        never computes — it only names the *declarable* subset, which can
        be three people out of a card whose largest facts are fifteen,
        non-declarable, and sitting right below it. Checked against every
        digest shape the other tests in this class produce, not just one."""
        cases = [
            Recommendation(fragrance_id=1, name="X", reasons=[
                Reason(kind="prefer", text="raspberry",
                       strength=Strength.SUPPORTED, people=8, creators=4),
            ], people=8, creators=4),
            Recommendation(fragrance_id=1, name="X", reasons=[
                Reason(kind="graph",
                       text="9 people across 6 channels compared this with Delina",
                       strength=Strength.SUPPORTED, people=9, creators=6),
            ], people=9, creators=6),
        ]
        for rec in cases:
            text = digest(rec)
            for word in ("mostly", "mainly", "generally"):
                assert word not in text.lower()

    def test_a_contested_facts_disagreement_number_appears(self):
        contested = Reason(kind="profile", text="longevity long lasting",
                           strength=Strength.CONTESTED, people=6, creators=3,
                           against=4)
        rec = Recommendation(fragrance_id=1, name="X", caveats=[contested],
                             people=6, creators=3)
        text = digest(rec)
        assert "4 of 10 disagree" in text

    def test_the_disagreement_clause_names_the_attribute_not_the_value(self):
        """"disagree on projection strong" is not English — the clause
        must read against the attribute alone. `Reason.text` for a
        sentiment axis is `"{attribute} {value}"`; the fix takes the
        first word rather than building a per-attribute phrase table."""
        declarable = Reason(kind="prefer", text="dates",
                            strength=Strength.SUPPORTED, people=3, creators=3)
        contested = Reason(kind="profile", text="projection strong",
                           strength=Strength.CONTESTED, people=7, creators=4,
                           against=3)
        rec = Recommendation(
            fragrance_id=1, name="X", reasons=[declarable],
            caveats=[contested], people=15, creators=6,
        )
        text = digest(rec)
        assert "disagree on projection" in text
        assert "disagree on projection strong" not in text

    def test_the_tail_attributes_the_total_to_the_card_not_the_named_fact(self):
        """Sentence one may name a 3-person fact; sentence two states what
        the whole card has behind it — a different, larger number — so it
        must not read as if the total stood behind the one named fact.
        Reuses the wording the old collapsed `<p class=ev>` line used."""
        declarable = Reason(kind="prefer", text="dates",
                            strength=Strength.SUPPORTED, people=3, creators=3)
        rec = Recommendation(fragrance_id=1, name="X", reasons=[declarable],
                             people=15, creators=6)
        text = digest(rec)
        assert "15 people across 6 channels behind everything cited." in text
        assert "are behind that" not in text
        assert "15 people are behind" not in text

    def test_two_sentence_max_is_enforced(self):
        """Count sentence terminators directly rather than trust the
        shape of the code that produces them — the guarantee that matters
        is in the output, not in how confident the implementation looks.
        'Chanel No. 4' as the anchor is the reviewer's exact case: a real
        catalogue name shape carrying a period, reaching the digest
        through the graph clause exactly the way a real comparison card
        would."""
        declarable = Reason(kind="prefer", text="raspberry",
                            strength=Strength.SUPPORTED, people=8, creators=4)
        graph = Reason(
            kind="graph",
            text="9 people across 6 channels compared this with Chanel No. 4",
            strength=Strength.SUPPORTED, people=9, creators=6,
        )
        contested = Reason(kind="profile", text="longevity long lasting",
                           strength=Strength.CONTESTED, people=6, creators=3,
                           against=4)
        rec = Recommendation(
            fragrance_id=1, name="X", reasons=[declarable, graph],
            caveats=[contested], people=31, creators=12,
        )
        text = digest(rec)
        assert text.count(".") == 2
        assert "compare this to Chanel No" in text
        assert "call it raspberry" in text
        assert "31 people across 12 channels behind everything cited." in text

    def test_a_period_bearing_declarable_fact_does_not_break_the_cap(self):
        """The same defect, reached through the "call it ..." clause
        instead of the graph clause — a note value or catalogue-derived
        text carrying a period must not add a sentence either."""
        declarable = Reason(kind="prefer", text="No. 5 vibes",
                            strength=Strength.SUPPORTED, people=8, creators=4)
        rec = Recommendation(fragrance_id=1, name="X", reasons=[declarable],
                             people=8, creators=4)
        text = digest(rec)
        assert text.count(".") == 2
        assert "No 5 vibes" in text or "No5 vibes" in text

    def test_a_period_bearing_fallback_does_not_break_the_cap(self):
        """The weak-fallback path embeds `Reason.phrase()` verbatim, which
        can itself carry a period-bearing name for a `kind="graph"` or
        `"comparative"` reason — the fallback is not exempt from the cap
        just because it is the safe branch."""
        weak_graph = Reason(
            kind="graph",
            text="1 person across 1 channel compared this with Chanel No. 4",
            strength=Strength.OBSERVED, people=1, creators=1,
        )
        rec = Recommendation(fragrance_id=1, name="X", reasons=[weak_graph],
                             people=1, creators=1)
        text = digest(rec)
        assert text.count(".") == 2

    def test_a_semantic_quote_never_reaches_the_digest_as_a_named_fact(self):
        """A semantic reason quotes a commenter's own words and can carry
        arbitrary punctuation — exactly what the two-sentence guarantee
        cannot survive if it leaked in. Excluded outright; see `digest`'s
        docstring."""
        quote = Reason(
            kind="semantic", text="someone described it as 'rich. warm. cozy.'",
            strength=Strength.OBSERVED, people=1, creators=1,
        )
        rec = Recommendation(fragrance_id=1, name="X", reasons=[quote],
                             people=1, creators=1)
        assert digest(rec) == ""

    def test_no_facts_at_all_yields_an_empty_digest(self):
        rec = Recommendation(fragrance_id=1, name="X")
        assert digest(rec) == ""

    def test_a_declarable_fact_is_stated_as_settled(self):
        strong = Reason(kind="prefer", text="raspberry",
                        strength=Strength.SUPPORTED, people=8, creators=4)
        rec = Recommendation(fragrance_id=1, name="X", reasons=[strong],
                             people=8, creators=4)
        text = digest(rec)
        assert "People call it raspberry." in text
        assert "8 people across 4 channels behind everything cited." in text


class TestTheAuditCatchesAViolatingDigest:
    """`digest()` composes from `Reason.text` values it does not invent —
    if an upstream `Reason` ever carried forbidden wording (which
    `Reason.phrase()` is itself careful never to produce; see
    `test_audit.TestTheAuditCatchesWhatItClaims` for the identical
    argument applied to `phrase()`), the digest inherits it rather than
    laundering it away. `_card` embeds the digest verbatim into the pages
    `audit._audit_rendered_text` scans for `FORBIDDEN` phrasing, so this
    is the same backstop every other composed sentence on the site gets —
    proven directly against it, the same way the phrase-level rules are.
    """

    def test_forbidden_wording_reaching_digest_is_still_flagged(self):
        from fragrance_graph.audit import FORBIDDEN

        bad = Reason(kind="prefer", text="the community agrees on rose",
                    strength=Strength.SUPPORTED, people=8, creators=4)
        rec = Recommendation(fragrance_id=1, name="X", reasons=[bad],
                             people=8, creators=4)
        sentence = digest(rec)
        assert any(phrase in sentence.lower() for phrase in FORBIDDEN)

    def test_the_rendered_card_carries_it_through_to_what_the_audit_scans(self):
        """Not just the string `digest()` returns — the actual HTML
        `page_texts` hands the audit, so a violation could not be fixed
        by `_card` quietly declining to embed it."""
        from fragrance_graph.ask import render_answer
        from fragrance_graph.audit import FORBIDDEN

        class FakePlan:
            def render(self) -> str:
                return "intent recommend"

        bad = Reason(kind="prefer", text="the community agrees on rose",
                    strength=Strength.SUPPORTED, people=8, creators=4)
        rec = Recommendation(fragrance_id=1, name="X", reasons=[bad],
                             people=8, creators=4)
        from fragrance_graph.recommend import Answer

        page = render_answer(
            "q", Answer(plan=FakePlan(), results=[rec]), head=[]
        )
        assert any(phrase in page.lower() for phrase in FORBIDDEN)


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


class TestAnchorProfileFallback:
    """"I like Side Effect but it's too warm" — real-corpus bug (user
    verbatim: "i said i like side effect but its too warm ... why it
    fits" — the engine used to recommend the *warmest* bottle it could
    find). Post-`TOO_X` the sentence compiles honestly to an anchor plus
    a comparative `warm` preference, but the anchor there has almost no
    community evidence, so `_score_comparative` has no baseline and the
    ordinary scoring loop returns zero candidates — a true refusal, but
    a needless one: the anchor still carries a DECLARED note profile
    (`fragrance_note_claim`, retailer data) this fallback can answer
    from instead.

    Fixture: the anchor declares {amber, vanilla, iris, musk}. "warm" is
    a curated axis covering {amber, vanilla, ...} (see
    `data/curation/note-axes.json`), so subtracting it leaves exactly
    {iris, musk} — the profile every assertion below is checked against.
    """

    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Fallback Test Anchor")
        declare(conn, anchor, "amber", "vanilla", "iris", "musk")

        # A: shares both remaining notes, nothing else — ranks first.
        candidate_a = add_fragrance(conn, "Iris Musk Twin")
        declare(conn, candidate_a, "iris", "musk")

        # B: shares only a *subtracted* note — must not appear at all.
        candidate_b = add_fragrance(conn, "Amber Only Bottle")
        declare(conn, candidate_b, "amber")

        # C: shares the same two notes as A, but the community itself
        # calls it warm — the exact thing being avoided. Must still
        # qualify (2 shared notes), must rank below A, and that warm
        # evidence must land in caveats, never reasons.
        candidate_c = add_fragrance(conn, "Iris Musk But Warm")
        declare(conn, candidate_c, "iris", "musk")
        note(conn, 900, frag=candidate_c, value="warm", author="w1",
             channel="warm_chan", claim_type="AESTHETIC")

        return anchor, candidate_a, candidate_b, candidate_c

    def _plan(self, anchor_id):
        return QueryPlan(
            text="i like fallback test anchor but its too warm",
            anchor="Fallback Test Anchor",
            anchor_id=anchor_id,
            soft=[Preference("vibe", "warm", Direction.LESS_THAN_ANCHOR,
                              said="too warm")],
        )

    def test_a_note_only_in_the_avoided_axis_does_not_qualify_a_candidate(
        self, conn
    ):
        anchor, _a, candidate_b, _c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan(anchor))
        assert candidate_b not in {r.fragrance_id for r in answer.results}

    def test_the_clean_match_ranks_first_and_declared_overlap_names_exactly_the_shared_notes(
        self, conn
    ):
        anchor, candidate_a, _b, _c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan(anchor))
        assert answer.results, "the fallback must produce a result to rank at all"
        assert answer.results[0].fragrance_id == candidate_a

        (overlap,) = [
            r for r in answer.results[0].reasons if r.kind == "declared_overlap"
        ]
        assert overlap.text == (
            "official notes share iris and musk with Fallback Test Anchor"
        )
        # Declared notes contribute zero people — nothing here was said
        # by anybody, and the wording must not imply otherwise.
        assert overlap.people == 0
        assert overlap.creators == 0

    def test_the_avoided_attribute_never_reaches_reasons_anywhere_in_the_response(
        self, conn
    ):
        """THE assertion this whole path exists to keep true: scanned
        across every candidate, not trusted from the one fixture case
        that happens to exercise the caveat branch."""
        anchor, *_ = self._seeded(conn)
        answer = recommend_plan(conn, self._plan(anchor))
        assert answer.results
        for result in answer.results:
            for reason in result.reasons:
                assert "warm" not in reason.text.lower()

    def test_a_candidate_the_community_calls_warm_is_demoted_with_a_caveat(
        self, conn
    ):
        anchor, candidate_a, _b, candidate_c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan(anchor))
        ids = [r.fragrance_id for r in answer.results]
        assert candidate_c in ids, "still a real match on 2 shared notes"
        assert ids.index(candidate_a) < ids.index(candidate_c), (
            "otherwise equal on shared notes; the avoided evidence "
            "demotes C below A"
        )
        c_result = next(r for r in answer.results if r.fragrance_id == candidate_c)
        assert any("warm" in cav.text for cav in c_result.caveats)
        assert not any("warm" in r.text.lower() for r in c_result.reasons)

    def test_zero_community_candidate_reports_honest_counts_not_fabricated_people(
        self, conn
    ):
        """Every note behind candidate A is declared, not perceived — the
        card must say zero people and zero creators, never invent some."""
        anchor, candidate_a, _b, _c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan(anchor))
        top = next(r for r in answer.results if r.fragrance_id == candidate_a)
        assert top.people == 0
        assert top.creators == 0

    def test_the_response_note_explains_the_basis_switch(self, conn):
        anchor, *_ = self._seeded(conn)
        answer = recommend_plan(conn, self._plan(anchor))
        assert "official notes" in answer.note
        assert "warm" in answer.note
        assert "Fallback Test Anchor" in answer.note

    def test_fallback_does_not_override_a_comparative_path_that_already_succeeded(
        self, conn
    ):
        """The pre-existing machinery this whole fallback defers to:
        `TestAComparativeIsNotAnAvoidance`'s own scenario, reused rather
        than re-derived, run through unmodified so a regression in
        either path shows up as a failure here too."""
        anchor = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        other = add_fragrance(conn, "Lattafa Khamrah")
        for i, author in enumerate(["p1", "p2", "p3", "p4", "p5"]):
            note(conn, i, frag=anchor, value="rose", author=author, channel=f"c{i}")
        note(conn, 20, frag=other, value="rose", author="p9")
        note(conn, 21, frag=other, value="dates", author="p8")

        answer = recommend(conn, "i love Delina but the rose is too strong")

        assert answer.results, "the comparative path already answers this"
        assert not any(
            r.kind == "declared_overlap"
            for result in answer.results
            for r in result.reasons
        )

    def test_no_anchor_means_no_fallback_attempted(self, conn):
        """`plan.anchor_id is None` is checked first and unconditionally
        — an anchor-less LOW preference must fall straight through to
        the ordinary refusal, never into note-profile matching that has
        no anchor to build a profile from."""
        plan = QueryPlan(
            text="not too warm",
            soft=[Preference("vibe", "warm", Direction.LOW, said="not too warm")],
        )
        answer = recommend_plan(conn, plan)
        assert answer.results == []


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


class TestSocialConceptsAreNotSynonyms:
    """Somebody being complimented is a fact about them and their circle.
    A fragrance being broadly likable is a fact about everyone. They
    correlate; they are not interchangeable, and the difference is exactly
    what a buyer asking for a safe blind buy wants to know.

    Measured on the corpus: compliment 29 claims / 10 attached, mass
    appeal 2 / 1. Folding them together answered a question the corpus
    cannot speak to using evidence for a different one.
    """

    def _bottle(self, conn, value):
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="sweet", author="p1")
        note(conn, 2, frag=frag, value=value, author="p2",
             claim_type="AESTHETIC")
        return frag

    def test_crowd_pleasing_asks_about_mass_appeal(self, conn):
        from fragrance_graph.plan import parse_with_corpus

        add_fragrance(conn, "Lattafa Khamrah")
        plan = parse_with_corpus(conn, "a crowd-pleasing fragrance")
        assert [(p.attribute, p.value) for p in plan.soft] == [
            ("concept", "mass appeal")
        ]

    def test_direct_evidence_is_named_as_itself(self, conn):
        self._bottle(conn, "mass appeal")
        (result,) = recommend(conn, "a crowd-pleasing sweet fragrance").results
        concepts = [r for r in result.reasons if "mass appeal" in r.text]
        assert concepts
        assert "related to" not in concepts[0].text

    def test_compliment_evidence_is_named_as_compliments(self, conn):
        """It may retrieve, and the sentence must say which concept it is
        evidence for."""
        self._bottle(conn, "compliments from women")
        (result,) = recommend(conn, "a crowd-pleasing sweet fragrance").results
        concepts = [r for r in result.reasons if "compliment" in r.text]
        assert concepts
        assert "related to mass appeal" in concepts[0].text
        assert "does not speak to directly" in concepts[0].text

    def test_a_related_match_scores_below_a_direct_one(self, conn):
        direct = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=direct, value="sweet", author="p1")
        note(conn, 2, frag=direct, value="mass appeal", author="p2",
             claim_type="AESTHETIC")
        related = add_fragrance(conn, "Al Haramain Detour Noir")
        note(conn, 3, frag=related, value="sweet", author="p3")
        note(conn, 4, frag=related, value="compliments", author="p4",
             claim_type="AESTHETIC")
        names = [r.name for r in recommend(
            conn, "a crowd-pleasing sweet fragrance"
        ).results]
        assert names.index("Lattafa Khamrah") < names.index(
            "Al Haramain Detour Noir"
        )

    def test_no_social_evidence_says_so_rather_than_substituting(self, conn):
        """With a note requirement met and no social evidence at all, the
        concept is reported as unmatched rather than filled in from
        something adjacent."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        # Two claims so "sweet" clears the vocabulary floor and becomes a
        # requirement; otherwise the query has nothing to retrieve on.
        note(conn, 1, frag=frag, value="sweet", author="p1", channel="c1")
        note(conn, 2, frag=frag, value="sweet", author="p2", channel="c2")
        answer = recommend(conn, "a crowd-pleasing sweet fragrance")
        assert answer.results, "the sweet requirement is still met"
        result = answer.results[0]
        assert any("mass appeal" in item for item in result.unmatched)
        assert not any("mass appeal" in reason.text for reason in result.reasons)

    def test_a_concept_with_no_evidence_anywhere_returns_nothing(self, conn):
        """And the answer says so rather than inventing a match."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="mysterious", author="p1")
        answer = recommend(conn, "a crowd-pleasing fragrance")
        assert answer.results == []
        assert answer.note

    def test_polarizing_is_not_reachable_through_compliments(self, conn):
        """The expansion is directional. Compliment evidence is weak
        support for mass appeal and no support at all for polarizing."""
        from fragrance_graph.plan import RELATED_CONCEPTS

        assert "compliment getting" not in RELATED_CONCEPTS.get("polarizing", ())
        assert "polarizing" not in RELATED_CONCEPTS.get("mass appeal", ())

    def test_the_concepts_have_separate_vocabularies(self):
        from fragrance_graph.plan import CONCEPTS

        assert "compliment" not in " ".join(CONCEPTS["mass appeal"])
        assert "crowd" not in " ".join(CONCEPTS["compliment getting"])


class TestCommerceHeadlineIntegration:
    """`commerce_card.headline`, driven by real `Answer`s from `recommend()`
    — spec §35 ("almost always return something": only hard-constraint
    exhaustion is a true no-results) and §1 (the failure case's positive
    framing), exercised end to end rather than only at the unit level
    `tests/test_commerce_card.py` already covers.
    """

    def test_hard_constraint_exhaustion_is_the_one_true_no_results_case(self, catalogue):
        """A hard "must have raspberry" that nothing in the corpus
        supports really is empty — and the headline names the
        requirement honestly, in plain words, never the engine's internal
        refusal vocabulary."""
        from fragrance_graph.commerce_card import headline

        conn, a, b = catalogue
        note(conn, 1, frag=b, value="coffee", author="p1")
        answer = recommend(conn, "a fragrance with raspberry")
        assert answer.results == []
        text = headline(answer.plan, answer.results)
        assert "raspberry" in text
        lowered = text.lower()
        for jargon in ("independence", "threshold", "gate", "consensus"):
            assert jargon not in lowered

    def test_lack_of_consensus_never_produces_no_results(self, catalogue):
        """A single person's remark is real evidence a request was
        answered — the exact case `commerce-audit.md` §1 traces: nothing
        in `recommend._score` drops a candidate merely because nobody else
        corroborated it. §35's "almost always return something" holds
        here, and `commerce_card.result_tier` still assigns a real tier
        (any of the catalog-first spec's four, never rejection) rather
        than excluding it."""
        from fragrance_graph.commerce_card import result_tier

        conn, a, b = catalogue
        note(conn, 1, frag=a, value="raspberry", author="the-only-person")
        answer = recommend(conn, "a fragrance with raspberry")
        assert len(answer.results) == 1
        assert result_tier(answer.results[0]) in (
            "WORTH_DISCOVERING", "STRONG_PROFILE_FIT",
            "COMMUNITY_FAVORITE", "BEST_OVERALL_FIT",
        )


class TestFailureCaseCompositionReturnsPositivelyFramedResults:
    """spec §1, reproduced end to end against a real, freshly-seeded
    corpus: LIKE gorgeous+unique / AVOID cinnamon / WANT strong+feminine /
    OCC summer — the exact combination named in the spec's own failure
    case. No bottle in this fixture satisfies every dimension at once
    (the one candidate with strong/feminine/summer evidence also carries
    real cinnamon evidence and is hard-excluded by the avoid), which is
    precisely the shape that used to open with "No match below clears the
    independence bar." It must now come back with real, if partial,
    results and a positively-framed headline.
    """

    def _seeded(self, conn):
        # Carries everything WANTed, but also the AVOIDed note — excluded
        # outright by `avoid`+`note` (default mode EXCLUDE), leaving no
        # full match in the corpus at all.
        excluded = add_fragrance(conn, "Warm Cinnamon Feminine Bottle")
        for i, author in enumerate(["e1", "e2", "e3"]):
            note(conn, i, frag=excluded, value="feminine", author=author,
                 channel=f"ef{i}", claim_type="AESTHETIC")
        for i, author in enumerate(["e4", "e5", "e6"]):
            note(conn, 10 + i, frag=excluded, value="cinnamon", author=author,
                 channel=f"ec{i}")

        # A real, partial match: feminine, on solid evidence, nothing else.
        partial = add_fragrance(conn, "Feminine Only Bottle")
        for i, author in enumerate(["f1", "f2", "f3"]):
            note(conn, 20 + i, frag=partial, value="feminine", author=author,
                 channel=f"pf{i}", claim_type="AESTHETIC")

        # A weaker, single-source match on the LIKE-side descriptors.
        weak = add_fragrance(conn, "Gorgeous Unique Bottle")
        note(conn, 30, frag=weak, value="gorgeous", author="w1", channel="w0")
        note(conn, 31, frag=weak, value="unique", author="w2", channel="w1")
        return excluded, partial, weak

    def test_the_response_is_positively_framed_with_real_partial_results(self, conn):
        from fragrance_graph.commerce_card import build_card, headline
        from fragrance_graph.session import PreferenceState

        excluded, partial, weak = self._seeded(conn)
        state = PreferenceState()
        state.merge_preference("gorgeous", "more")
        state.merge_preference("unique", "more")
        state.merge_preference("cinnamon", "avoid")
        state.merge_preference("strong projection", "more")
        state.merge_preference("feminine", "more")
        state.set_occasion("summer")

        plan, _unexpressed = state.to_plan()
        answer = recommend_plan(conn, plan)

        # The request is not left empty just because nothing matched
        # every dimension at once (spec §35/§1) — a real partial match
        # comes back even though no bottle in this fixture satisfies
        # LIKE+WANT+AVOID all at the same time.
        assert answer.results, "a real partial match must still come back"
        # And the bottle carrying real cinnamon evidence, if it is
        # returned at all, must carry it as a visible caveat rather than
        # silently costing it nothing — the soft-avoid path
        # (`merge_preference`, the free-text/chip mechanism this fixture
        # exercises) penalises rather than hard-excludes; see
        # `session.AvoidMode` for the composer's stricter `exclude` mode.
        by_id = {r.fragrance_id: r for r in answer.results}
        if excluded in by_id:
            assert any("cinnamon" in c.text for c in by_id[excluded].caveats)

        text = headline(plan, answer.results)
        assert text.startswith("Your closest matches")
        lowered = text.lower()
        for jargon in ("independence", "bar", "threshold", "gate", "consensus", "clears"):
            assert jargon not in lowered
        # The deprioritized dimension is still named, honestly — this is
        # transparency, not a euphemism for what happened.
        assert "cinnamon" in text

        cards = {c.fragrance_id: build_card(c) for c in answer.results}
        # Every returned card carries a real, tier-worded reason — never
        # an empty "why try this" list standing in for a rejection.
        for card in cards.values():
            assert card.fit_signals, f"{card.name!r} has no fit signals at all"
            assert card.result_tier in (
                "BEST_OVERALL_FIT", "STRONG_PROFILE_FIT",
                "COMMUNITY_FAVORITE", "WORTH_DISCOVERING",
            )


# --- catalog-first spec: T1/T2/T3 candidate generation --------------------
#
# The proven defect: "less sweet + less vanilla + summer" against a
# 547-bottle catalogue returned two results, because candidacy required a
# comment. These tests build the plan directly (`QueryPlan`/`Preference`,
# fed straight to `recommend_plan` — the identical path a parsed sentence
# takes, per that function's own docstring) rather than through the text
# parser, because the text parser's note vocabulary requires >= 2 corpus
# claims per word (`plan.MIN_NOTE_CLAIMS`) and these fixtures are
# deliberately tiny — a plan carries exactly the same `Preference`
# objects either way.


class TestCatalogFirstCandidateGeneration:
    """The acceptance case from the catalog-first spec, verbatim: bottle
    A (declared citrus/woody profile, ZERO community claims) must reach
    the results for "avoid sweet, avoid vanilla, want summer" — the exact
    query shape the screenshot proved broken."""

    def _seeded(self, conn):
        # A: strong catalog fit, no comments at all.
        a = add_fragrance(conn, "Catalog Only Bottle")
        declare(conn, a, "lemon", "neroli", "white musk", "cedar")

        # B: declares the very things being avoided, PLUS real community
        # evidence for one of them (sweet) — exercises both the
        # unchanged community-avoid path (sweet) and the new catalog
        # ladder (vanilla, which nobody has commented on).
        b = add_fragrance(conn, "Declared Sweet Bottle")
        declare(conn, b, "vanilla", "caramel")
        note(conn, 1, frag=b, value="sweet", author="b1")

        # C: no catalog data at all, but real community summer evidence.
        c = add_fragrance(conn, "No Catalog Data Bottle")
        note(conn, 2, frag=c, value="summer", author="c1",
             claim_type="OCCASION")

        return a, b, c

    def _plan(self):
        return QueryPlan(
            text="avoid sweet, avoid vanilla, summer",
            soft=[
                Preference("note", "sweet", Direction.LOW, said="sweet"),
                Preference("note", "vanilla", Direction.LOW, said="vanilla"),
                Preference("occasion", "summer", Direction.HIGH, said="summer"),
            ],
        )

    def test_the_zero_comment_bottle_reaches_results(self, conn):
        a, b, c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan())
        ids = {r.fragrance_id for r in answer.results}
        assert a in ids, "bottle A (zero community claims) must not be dropped"

    def test_the_zero_comment_bottle_carries_the_not_declared_vanilla_signal(
        self, conn
    ):
        from fragrance_graph.commerce_card import result_tier

        a, b, c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan())
        result_a = next(r for r in answer.results if r.fragrance_id == a)

        assert result_a.people == 0 and result_a.creators == 0
        vanilla_reasons = [
            r for r in result_a.reasons
            if r.kind == "catalog_fit" and "vanilla" in r.text.lower()
        ]
        assert vanilla_reasons, result_a.reasons
        assert vanilla_reasons[0].text == "Vanilla isn't among the declared notes."
        # Real catalog fit, zero community evidence -- exactly the label
        # the catalog-first spec's failure case exists to make reachable.
        assert result_tier(result_a) == "STRONG_PROFILE_FIT"

    def test_the_zero_comment_bottle_shows_limited_community_coverage(self, conn):
        from fragrance_graph.commerce_card import build_card

        a, b, c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan())
        result_a = next(r for r in answer.results if r.fragrance_id == a)
        card = build_card(result_a)
        assert card.community_coverage == "Community insight: limited so far"

    def test_the_declared_present_bottle_is_penalized_and_ranks_below(self, conn):
        a, b, c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan())
        ids = [r.fragrance_id for r in answer.results]
        assert b in ids, "a penalized candidate is still a real result, not excluded"
        result_b = next(r for r in answer.results if r.fragrance_id == b)
        catalog_avoid = [c for c in result_b.caveats if c.kind == "catalog_avoid"]
        assert catalog_avoid, result_b.caveats
        assert catalog_avoid[0].text == "Vanilla is among the declared notes."
        # A's real catalog fit outranks B's declared-and-penalized profile.
        assert ids.index(a) < ids.index(b)

    def test_the_community_summer_bottle_is_present_via_the_community_path(self, conn):
        a, b, c = self._seeded(conn)
        answer = recommend_plan(conn, self._plan())
        ids = {r.fragrance_id for r in answer.results}
        assert c in ids
        result_c = next(r for r in answer.results if r.fragrance_id == c)
        assert any(
            r.kind == "prefer" and r.attribute == "occasion"
            for r in result_c.reasons
        )

    def test_real_corpus_more_than_two_results_with_a_zero_community_bottle(self):
        """The literal end-to-end regression: the same query, against the
        real, committed corpus (skipped where that database is not
        reachable) — before this fix, `recommend()` returned candidates
        only when a comment already existed, which on the real 547-548
        bottle catalogue meant a handful of results at most."""
        from fragrance_graph.db import DEFAULT_DB_URL, get_connection

        try:
            real_conn = get_connection(DEFAULT_DB_URL)
        except Exception:  # noqa: BLE001
            pytest.skip("no developer database; run `corpus import` first")
        if real_conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] < 100:
            pytest.skip("developer database is too small for this probe")
        try:
            answer = recommend(real_conn, "less sweet, less vanilla, summer")
            assert len(answer.results) > 2, (
                f"only {len(answer.results)} result(s) — candidacy is still "
                "gated by community evidence"
            )
            zero_community = [r for r in answer.results if r.people == 0]
            assert zero_community, (
                "no zero-community bottle reached results — T2 catalog "
                "candidacy is not actually contributing"
            )
        finally:
            real_conn.close()


class TestT2CandidatesNeverShrinkInT3Scoring:
    """"Reorders, never shrinks to comment-having bottles" — the
    structural guarantee: `_score` appends whatever `_catalog_candidates`
    already found a signal for directly into `reasons`/`caveats` before
    its own "nothing to say" rejection guard runs, so a T2 candidate that
    was included FOR having a catalog signal can never then be rejected
    for lacking one."""

    def test_a_pure_catalog_match_with_no_community_facts_is_not_rejected(self, conn):
        # Named to avoid `evidence.name_facts`' CANONICAL_NAME_NOTES
        # collision (a fragrance literally named "Citrus ..." would pick
        # up a pre-existing, unrelated `from_name` fact for "citrus" that
        # `_preference_fact` does not exclude — a real gap, but not this
        # spec's to fix; the fixture just steers around it).
        frag = add_fragrance(conn, "Fixture Bottle One")
        declare(conn, frag, "lemon", "bergamot")  # citrus: HIGH
        plan = QueryPlan(
            text="", soft=[Preference("note", "citrus", Direction.HIGH, said="citrus")]
        )
        answer = recommend_plan(conn, plan)
        assert frag in {r.fragrance_id for r in answer.results}
        result = next(r for r in answer.results if r.fragrance_id == frag)
        assert any(r.kind == "catalog_fit" for r in result.reasons)


class TestTheTwoAbsencesAreDistinct:
    """catalog-first spec: "CATALOG absence is real information; two
    different absences, two meanings" — `NOT_DECLARED` (checked, and
    genuinely absent from the official list) is never the same claim as
    the pre-existing COMMUNITY absence (`kind="absence"`, "nobody in this
    corpus's comments has mentioned it")."""

    def test_not_declared_is_a_positive_catalog_signal(self):
        profile = CatalogProfile(1, frozenset({"lemon"}), has_catalog_data=True)
        hit = _catalog_signal(
            profile, Preference("note", "vanilla", Direction.LOW), frozenset()
        )
        assert hit is not None
        kind, reason = hit
        assert kind == "positive"
        assert reason.kind == "catalog_fit"
        assert reason.text == "Vanilla isn't among the declared notes."

    def test_no_catalog_data_is_silence_not_a_disguised_positive(self):
        """The OTHER absence -- truly unknown -- must produce no signal
        at all, never `NOT_DECLARED`'s modest-positive wording borrowed
        for a bottle that was never actually checked."""
        profile = CatalogProfile(1, frozenset(), has_catalog_data=False)
        hit = _catalog_signal(
            profile, Preference("note", "vanilla", Direction.LOW), frozenset()
        )
        assert hit is None

    def test_the_community_absence_path_is_a_different_kind_and_sentence(self, conn):
        """The pre-existing COMMUNITY absence (`recommend._score`'s "no
        X evidence recorded" `kind="absence"` reason, silence in the
        CORPUS OF COMMENTS) fires here instead, because this bottle has
        no catalog data at all for "sweet" either -- and its wording
        never borrows the catalog ladder's "declared notes" phrase."""
        frag = add_fragrance(conn, "No Catalog Data At All")
        note(conn, 1, frag=frag, value="citrus", author="p1")
        plan = QueryPlan(
            text="", soft=[Preference("note", "sweet", Direction.LOW, said="sweet")]
        )
        answer = recommend_plan(conn, plan)
        assert len(answer.results) == 1
        result = answer.results[0]
        absence_reasons = [r for r in result.reasons if r.kind == "absence"]
        assert absence_reasons, result.reasons
        assert "no sweet evidence recorded" in absence_reasons[0].text
        assert "declared notes" not in absence_reasons[0].text
