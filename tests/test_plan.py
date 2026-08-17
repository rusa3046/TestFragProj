"""Reading a request into something a test can assert on.

Most of these run with no database and no vocabulary, because the rules
are the thing under test. The ones that need a corpus are marked by taking
the `conn` fixture.

The failure this file is really guarding against is a plan that quietly
means the opposite of the sentence — "the rose is too strong" read as a
request *for* rose. That returns the rosiest bottles in the corpus to
somebody who just said rose is the problem.
"""

import pytest

from fragrance_graph.plan import (
    Direction,
    Intent,
    parse,
    parse_with_corpus,
)
from fragrance_graph.resolve.entities import add_fragrance

NOTES = frozenset({
    "rose", "raspberry", "vanilla", "smoky", "sweet", "fruity", "edible",
    "coffee", "leather", "citrus",
})


def plan(text, **kw):
    return parse(None, text, notes=NOTES, **kw)


# --- intent ----------------------------------------------------------------


class TestIntent:
    def test_a_request_for_bottles_is_a_recommendation(self):
        assert plan("something with vanilla").intent is Intent.RECOMMEND

    def test_asking_why_is_an_explanation(self):
        assert plan("why did you recommend this?").intent is Intent.EXPLAIN

    def test_asking_how_many_is_an_explanation(self):
        assert plan("how many people said that?").intent is Intent.EXPLAIN

    def test_two_bottles_is_a_comparison(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        add_fragrance(conn, "Parfums de Marly Delina Exclusif",
                      aliases=["Delina Exclusif"])
        got = parse_with_corpus(conn, "Delina vs Delina Exclusif")
        assert got.intent is Intent.COMPARE
        assert got.anchor == "Parfums de Marly Delina"
        assert got.other == "Parfums de Marly Delina Exclusif"

    def test_asking_about_disagreement_is_a_profile(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        got = parse_with_corpus(conn, "What do people disagree about for Delina?")
        assert got.intent is Intent.PROFILE
        assert got.anchor == "Parfums de Marly Delina"


# --- the sentence that names the product -----------------------------------


class TestAComplaintIsNotARequest:
    """"I love Delina but the rose is too strong" is the shape everything
    else is a variation on, and reading it forwards inverts it."""

    def test_too_strong_asks_for_less_not_more(self):
        got = plan("i love delina but the rose is too strong")
        (pref,) = got.soft
        assert pref.value == "rose"
        assert pref.direction is Direction.LESS_THAN_ANCHOR
        assert not got.hard, "nothing here is a requirement"

    def test_the_intensity_word_is_not_an_ingredient(self):
        """"strong" is how much rose, not a note to go and find."""
        got = plan("i love delina but the rose is too strong")
        assert "strong" not in {c.value for c in got.hard}
        assert "strong" not in {p.value for p in got.soft}

    def test_the_complaint_does_not_swallow_the_whole_clause(self):
        """An unbounded left edge captured "delina but the rose"."""
        got = plan("i love delina but the rose is too strong")
        assert got.soft[0].value == "rose"

    def test_a_comparative_knows_it_needs_an_anchor(self):
        got = plan("i love delina but the rose is too strong")
        assert got.soft[0].relative_to_anchor

    def test_too_much_reads_the_same_as_too_strong(self):
        got = plan("like delina but the vanilla is too much")
        assert got.soft[0].value == "vanilla"
        assert got.soft[0].direction is Direction.LESS_THAN_ANCHOR

    def test_the_mirror_shape_its_too_warm_is_also_a_complaint(self):
        """"i like side effect but its too warm" — pronoun subject, the
        attribute AFTER "too". The first shipped kiosk read `warm` as a
        positive vibe preference and recommended the warmest bottle it
        could cite: a user filed it as a bug, correctly. The two "too"
        shapes must land in the same place."""
        got = plan("i like delina but its too warm")
        (pref,) = got.soft
        assert pref.value == "warm"
        assert pref.direction is Direction.LESS_THAN_ANCHOR

    def test_too_x_without_an_anchor_is_a_plain_avoid(self):
        got = plan("something not heavy, and definitely not too sweet")
        assert all(p.direction is not Direction.HIGH for p in got.soft)

    def test_not_too_x_stays_with_the_negation_reader(self):
        """"not too sweet" was already read by the negation patterns;
        the new TOO_X rule must not double-fire on it."""
        got = plan("khamrah but not too sweet")
        sweet = [p for p in got.soft if p.value == "sweet"]
        assert len(sweet) == 1
        assert sweet[0].direction is Direction.LOW

    def test_too_much_alone_names_no_attribute(self):
        """"it's too much" carries no attribute; nothing may be read
        from it."""
        got = plan("i love delina but its too much")
        assert "much" not in {p.value for p in got.soft}


# --- negation --------------------------------------------------------------


class TestNegation:
    def test_less_x_avoids_x(self):
        got = plan("something like that but less smoky")
        (pref,) = got.soft
        assert (pref.value, pref.direction) == ("smoky", Direction.LOW)

    def test_doesnt_smell_x_avoids_x(self):
        got = plan("a vanilla that doesn't smell edible")
        assert {c.value for c in got.hard} == {"vanilla"}
        assert {(p.value, p.direction) for p in got.soft} == {
            ("edible", Direction.LOW)
        }

    def test_negation_stops_at_the_clause_boundary(self):
        """"not too sweet, with vanilla" must not avoid vanilla."""
        got = plan("not too sweet, with vanilla")
        assert ("sweet", Direction.LOW) in {(p.value, p.direction) for p in got.soft}
        assert "vanilla" in {c.value for c in got.hard}

    def test_without_x_avoids_x(self):
        got = plan("a fruity scent without rose")
        assert ("rose", Direction.LOW) in {(p.value, p.direction) for p in got.soft}


# --- the concept layer -----------------------------------------------------


class TestConcepts:
    def test_crowd_pleasing_asks_about_mass_appeal(self):
        """Rewritten after the concept audit. "Crowd-pleasing" is how
        people ask about mass appeal, and it used to be folded together
        with compliment evidence — answering a question the corpus cannot
        speak to using evidence for a different one."""
        from fragrance_graph.plan import CONCEPTS

        got = plan("a crowd-pleasing fragrance with raspberry")
        assert ("concept", "mass appeal") in {
            (p.attribute, p.value) for p in got.soft
        }
        assert "compliment" not in " ".join(CONCEPTS["mass appeal"])

    def test_the_hyphenated_spelling_is_the_same_concept(self):
        assert plan("crowd-pleasing").soft[0].value == "mass appeal"
        assert plan("crowd pleasing").soft[0].value == "mass appeal"

    def test_expensive_smelling_is_a_concept(self):
        got = plan("something expensive smelling and airy")
        assert ("concept", "expensive smelling") in {
            (p.attribute, p.value) for p in got.soft
        }


# --- attributes ------------------------------------------------------------


class TestAttributes:
    def test_a_conjunction_splits_into_its_parts(self):
        got = plan("a feminine fruity fragrance with strong projection for a wedding")
        assert {c.value for c in got.hard} == {"fruity"}
        by = {(p.attribute, p.value) for p in got.soft}
        assert ("projection", "strong") in by
        assert ("occasion", "wedding") in by
        assert ("vibe", "feminine") in by

    def test_a_note_is_a_requirement_not_a_preference(self):
        """Raspberry means bottles without raspberry evidence are not worse
        candidates — they are not candidates."""
        got = plan("a fragrance with raspberry")
        assert [c.value for c in got.hard] == ["raspberry"]
        assert not got.soft

    def test_stopwords_never_become_requirements(self):
        got = plan("something with vanilla please")
        assert {c.value for c in got.hard} == {"vanilla"}

    def test_an_unknown_word_is_not_a_requirement(self):
        """A typo must not become a filter that silently returns nothing."""
        got = plan("a fragrance with rasberry")
        assert not got.hard


class TestVocabularyComesFromTheCorpus:
    def test_a_rare_note_is_not_filterable(self, conn):
        """One person writing "peons" is evidence about a bottle. It is not
        a word anyone will ask for, and admitting it lets noise become a
        hard constraint."""
        from fragrance_graph.plan import note_vocabulary

        add_fragrance(conn, "Parfums de Marly Delina")
        assert "peons" not in note_vocabulary(conn)

    def test_the_anchors_own_name_is_not_a_note(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        got = parse_with_corpus(conn, "i love delina but less sweet")
        assert "delina" not in {c.value for c in got.hard}
        assert "marly" not in {c.value for c in got.hard}


# --- refusing ---------------------------------------------------------------


class TestRefusal:
    def test_a_sentence_with_nothing_to_match_is_refused(self):
        got = plan("hello there")
        assert got.refusal
        assert not got.usable

    def test_a_refusal_is_not_an_empty_plan(self):
        """The difference matters: an empty plan retrieves everything."""
        got = plan("hello there")
        assert not got.hard and not got.soft
        assert got.refusal, "and it says so rather than matching all bottles"

    def test_an_unresolvable_anchor_is_kept_not_dropped(self, conn):
        """It is the signal the catalogue grows from."""
        got = parse_with_corpus(conn, "something like Babycat")
        assert got.unparsed == ["babycat"]


class TestRendering:
    def test_a_plan_reads_back_as_the_request(self):
        text = plan("i love delina but the rose is too strong").render()
        assert "less than anchor note = rose" in text

    @pytest.mark.parametrize("sentence", [
        "a crowd-pleasing fragrance with raspberry",
        "a vanilla that doesn't smell edible",
        "something like that but less smoky",
    ])
    def test_every_plan_renders_without_error(self, sentence):
        assert plan(sentence).render()


class TestCodexPhase2Findings:
    """Six findings from the 2026-08-15 planner review, all confirmed."""

    def test_plain_not_negates(self):
        """P1. "not too loud" was handled; "not loud" read `loud`
        positively and asked for the loudest thing in the corpus."""
        (pref,) = plan("not loud").soft
        assert (pref.value, pref.direction) == ("strong", Direction.LOW)

    def test_plain_not_negates_a_concept_too(self):
        assert ("cheap smelling", Direction.LOW) in {
            (p.value, p.direction) for p in plan("not cheap smelling").soft
        }

    def test_an_intensity_word_is_not_a_note(self, conn):
        """P1. `strong` and `heavy` occur often enough in note text to pass
        the frequency floor, so "something heavy" became a hard filter for
        a note nothing carries."""
        from fragrance_graph.plan import note_vocabulary

        add_fragrance(conn, "Parfums de Marly Delina")
        vocabulary = note_vocabulary(conn)
        assert not {"strong", "heavy", "loud", "intense"} & vocabulary

    def test_an_intensity_request_is_refused_not_filtered(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina")
        got = parse_with_corpus(conn, "something heavy")
        assert not got.hard
        assert got.refusal

    def test_every_intent_can_be_refused_not_only_recommend(self):
        """P1. Refusal was applied only to RECOMMEND, so an EXPLAIN with no
        context came back empty and unrefused — and an empty plan matches
        everything downstream rather than nothing."""
        got = plan("how many people said that?")
        assert got.intent is Intent.EXPLAIN
        assert got.refusal
        assert not got.usable

    def test_a_refusal_says_what_would_help(self):
        assert "recommendation" in plan("why did you recommend this?").refusal

    def test_a_complaint_without_an_anchor_degrades_rather_than_lying(self):
        """P2. `LESS_THAN_ANCHOR` with no anchor is uninterpretable. It
        still clearly means "avoid rose", so it becomes that."""
        (pref,) = plan("the rose is too strong").soft
        assert pref.direction is Direction.LOW
        assert not pref.relative_to_anchor

    def test_the_same_complaint_with_an_anchor_stays_comparative(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        (pref,) = parse_with_corpus(
            conn, "i love delina but the rose is too strong"
        ).soft
        assert pref.direction is Direction.LESS_THAN_ANCHOR

    def test_a_conjunction_is_not_part_of_the_complaint(self, conn):
        """P2. Without "the", TOO_MUCH started at "but" and captured the
        two-word note "but rose"."""
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        (pref,) = parse_with_corpus(
            conn, "i love delina but rose is too strong"
        ).soft
        assert pref.value == "rose"

    def test_a_two_letter_alias_is_never_an_anchor(self, conn):
        """P2. The catalogue really carries "as" as an alias of Angels'
        Share, so "i like as, but less sweet" anchored on a bottle nobody
        named."""
        add_fragrance(conn, "Kilian Angels' Share", aliases=["AS"])
        got = parse_with_corpus(conn, "i like as, but less sweet")
        assert got.anchor is None

    def test_a_real_short_name_still_resolves(self, conn):
        """The floor is three characters, not "short names are suspicious"."""
        add_fragrance(conn, "Maison Francis Kurkdjian Baccarat Rouge 540",
                      aliases=["BR540"])
        assert parse_with_corpus(
            conn, "something like BR540"
        ).anchor == "Maison Francis Kurkdjian Baccarat Rouge 540"

    def test_negation_scanning_is_linear_enough_to_be_safe(self):
        """P2 HYPOTHESIS, confirmed as a real shape and fixed cheaply.
        Boundaries are found once for the string rather than rescanned per
        trigger."""
        import time

        started = time.monotonic()
        plan("no " * 400 + "rose")
        assert time.monotonic() - started < 1.0

    def test_an_anchor_name_stops_at_a_requirement(self, conn):
        """Found while testing the recommender. The lazy capture ran to
        " but ", so "something like Delina with raspberry but less rose"
        took the anchor as "delina with raspberry" — which resolved to
        nothing and swallowed the raspberry constraint on the way."""
        add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        got = parse_with_corpus(
            conn, "something like Delina with raspberry but less rose"
        )
        assert got.anchor == "Parfums de Marly Delina"
        assert [c.value for c in got.hard] == ["raspberry"]
        assert ("rose", Direction.LOW) in {(p.value, p.direction) for p in got.soft}
