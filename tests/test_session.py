"""Accumulating a session's state, and compiling it back into a plan.

Two kinds of test live here. Merge-logic tests use `merge_utterance(None,
...)` against `plan.py`'s static vocabulary tables (vibes, occasions,
performance phrases) — no database needed, because those tables are not
corpus-derived. Anything that needs a real note word ("raspberry"), a real
anchor to resolve, or a round trip through `recommend_plan` uses the `conn`
fixture and seeds real claims, the same way `test_recommend.py` does.
"""

import pytest

from fragrance_graph.evidence import Strength
from fragrance_graph.plan import Constraint, Intent, Preference
from fragrance_graph.plan import Direction as PlanDirection
from fragrance_graph.recommend import digest, recommend, recommend_plan
from fragrance_graph.resolve.entities import add_fragrance
from fragrance_graph.session import (
    Direction,
    PreferenceState,
    Verdict,
    classify_attribute,
    rebuild,
)
from tests.test_recommend import note


class TestClassifyAttribute:
    """The chip path's mirror of `plan.py`'s own vocabulary tables."""

    def test_a_performance_phrase_resolves_to_its_attribute_and_value(self):
        assert classify_attribute("loud") == ("projection", "strong")
        assert classify_attribute("quiet") == ("projection", "weak")
        assert classify_attribute("long lasting") == ("longevity", "long lasting")

    def test_an_attribute_name_word_resolves_to_its_own_attribute(self):
        assert classify_attribute("longevity") == ("longevity", "longevity")
        assert classify_attribute("projection") == ("projection", "projection")

    def test_a_vibe_word_resolves_to_the_vibe_attribute(self):
        assert classify_attribute("feminine") == ("vibe", "feminine")
        assert classify_attribute("Feminine") == ("vibe", "feminine")

    def test_a_concept_phrase_resolves_to_the_concept_it_names(self):
        assert classify_attribute("crowd pleasing") == ("concept", "mass appeal")
        assert classify_attribute("compliment getter") == (
            "concept", "compliment getting",
        )

    def test_an_unrecognised_word_falls_back_to_note(self):
        """No table claims it, so it lands in the same open-ended bucket
        `plan._read_notes` uses for an ordinary descriptor."""
        assert classify_attribute("raspberry") == ("note", "raspberry")

    def test_an_occasion_word_is_deliberately_not_special_cased_here(self):
        """Occasion has its own field and its own `/prefs` body shape
        (`{"occasion": ...}`) — a bare descriptor chip for an occasion
        word is not the documented path, so this falls to the same
        fallback an unrecognised word does, rather than silently
        guessing what the caller meant."""
        assert classify_attribute("wedding") == ("note", "wedding")


class TestMergingFreeForm:
    def test_an_unnegated_vibe_word_is_a_soft_more_preference(self):
        state = PreferenceState()
        state.merge_utterance(None, "something feminine please")
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE

    def test_a_negated_vibe_word_is_a_soft_less_preference(self):
        state = PreferenceState()
        state.merge_utterance(None, "not feminine, please")
        assert state.attribute_prefs["vibe:feminine"] is Direction.LESS

    def test_a_later_utterance_overrides_an_earlier_one_on_the_same_key(self):
        """The exact scenario the brief describes: "less X" then "actually
        more X is fine" ends at MORE, not both."""
        state = PreferenceState()
        state.merge_utterance(None, "not feminine, please")
        assert state.attribute_prefs["vibe:feminine"] is Direction.LESS
        state.merge_utterance(None, "actually, feminine is great")
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE
        assert len(state.attribute_prefs) == 1

    def test_free_text_history_records_every_utterance_in_order(self):
        state = PreferenceState()
        state.merge_utterance(None, "something feminine")
        state.merge_utterance(None, "not too loud")
        assert state.free_text_history == ["something feminine", "not too loud"]

    def test_merge_utterance_returns_the_single_utterances_own_plan(self):
        state = PreferenceState()
        parsed = state.merge_utterance(None, "something feminine please")
        assert parsed.soft[0].attribute == "vibe"
        assert parsed.soft[0].value == "feminine"


class TestChipAndFreeFormConverge:
    def test_a_chip_and_a_later_sentence_about_the_same_word_share_one_slot(self):
        state = PreferenceState()
        state.merge_preference("feminine", Direction.MORE)
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE
        state.merge_utterance(None, "not feminine, please")
        assert state.attribute_prefs["vibe:feminine"] is Direction.LESS
        assert len(state.attribute_prefs) == 1

    def test_a_require_chip_compiles_to_a_hard_constraint(self):
        state = PreferenceState()
        state.merge_preference("raspberry", Direction.REQUIRE)
        plan, _ = state.to_plan()
        assert plan.hard == [Constraint("note", "raspberry", said="raspberry")]

    def test_a_string_direction_is_accepted_like_the_enum(self):
        state = PreferenceState()
        state.merge_preference("feminine", "more")
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE


class TestLoveAndNoEffects:
    def test_love_appends_to_liked_and_becomes_the_anchor(self):
        state = PreferenceState()
        state.record_feedback(1, "Kilian Angels' Share", Verdict.LOVE)
        assert state.liked_fragrances == ["Kilian Angels' Share"]
        plan, _ = state.to_plan()
        assert plan.anchor == "Kilian Angels' Share"
        assert plan.anchor_id == 1

    def test_no_excludes_and_is_never_the_anchor(self):
        state = PreferenceState()
        state.record_feedback(1, "Kilian Angels' Share", Verdict.NO)
        assert state.disliked_fragrances == ["Kilian Angels' Share"]
        assert 1 in state.excluded_ids
        plan, _ = state.to_plan()
        assert plan.anchor is None

    def test_a_no_on_the_current_anchor_clears_it_when_nothing_else_is_liked(self):
        state = PreferenceState()
        state.record_feedback(2, "X", Verdict.LOVE)
        plan, _ = state.to_plan()
        assert plan.anchor_id == 2
        state.record_feedback(2, "X", Verdict.NO)
        plan, _ = state.to_plan()
        assert plan.anchor is None
        assert plan.anchor_id is None
        assert 2 in state.excluded_ids

    def test_a_no_on_the_current_anchor_falls_back_to_the_previous_liked_bottle(self):
        """F3: a NO on the anchor must not strand `liked_fragrances`
        holding a bottle the served plan claims nothing is anchored to.
        Falls back to `liked_fragrances[-1]` after removal, with its own
        real id — not merely `None` while a perfectly good liked bottle
        sits right there."""
        state = PreferenceState()
        state.record_feedback(1, "Delina", Verdict.LOVE)
        state.record_feedback(34, "Kilian", Verdict.LOVE)
        plan, _ = state.to_plan()
        assert plan.anchor == "Kilian"

        state.record_feedback(34, "Kilian", Verdict.NO)
        assert state.liked_fragrances == ["Delina"]
        plan, unexpressed = state.to_plan()
        assert plan.anchor == "Delina"
        assert plan.anchor_id == 1
        assert unexpressed == []

    def test_a_love_reverses_an_earlier_no_on_the_same_bottle(self):
        state = PreferenceState()
        state.record_feedback(1, "X", Verdict.NO)
        assert 1 in state.excluded_ids
        state.record_feedback(1, "X", Verdict.LOVE)
        assert 1 not in state.excluded_ids
        assert "X" not in state.disliked_fragrances
        assert "X" in state.liked_fragrances

    def test_a_no_reverses_an_earlier_love_on_the_same_bottle(self):
        state = PreferenceState()
        state.record_feedback(1, "X", Verdict.LOVE)
        state.record_feedback(1, "X", Verdict.NO)
        assert "X" not in state.liked_fragrances
        assert "X" in state.disliked_fragrances

    def test_maybe_deprioritizes_without_excluding(self):
        state = PreferenceState()
        state.record_feedback(3, "Y", Verdict.MAYBE)
        assert 3 in state.deprioritized_ids
        assert 3 not in state.excluded_ids
        assert "Y" not in state.liked_fragrances
        assert "Y" not in state.disliked_fragrances

    def test_excluded_and_deprioritized_read_the_most_recent_verdict_only(self):
        state = PreferenceState()
        state.record_feedback(4, "Z", Verdict.NO)
        state.record_feedback(4, "Z", Verdict.MAYBE)
        assert 4 in state.deprioritized_ids
        assert 4 not in state.excluded_ids


class TestToPlanCompilation:
    def test_more_compiles_to_a_soft_high_preference(self):
        state = PreferenceState()
        state.merge_preference("feminine", Direction.MORE)
        plan, unexpressed = state.to_plan()
        assert plan.soft == [
            Preference("vibe", "feminine", PlanDirection.HIGH, said="feminine")
        ]
        assert unexpressed == []

    def test_less_and_avoid_both_compile_to_a_soft_low_preference(self):
        """Documented, not accidental: the engine has one soft-avoidance
        mechanism and no hard exclusion, so both directions land the same
        place — see `to_plan`'s docstring."""
        less_state = PreferenceState()
        less_state.merge_preference("feminine", Direction.LESS)
        avoid_state = PreferenceState()
        avoid_state.merge_preference("feminine", Direction.AVOID)
        less_plan, _ = less_state.to_plan()
        avoid_plan, _ = avoid_state.to_plan()
        assert less_plan.soft == avoid_plan.soft
        assert less_plan.soft[0].direction is PlanDirection.LOW

    def test_budget_is_always_unexpressed(self):
        state = PreferenceState()
        state.set_budget(150.0)
        _plan, unexpressed = state.to_plan()
        assert unexpressed == [{"preference": "budget", "reason": "no price data yet"}]

    def test_a_recognised_occasion_compiles_to_a_soft_preference(self):
        state = PreferenceState()
        state.set_occasion("wedding")
        plan, unexpressed = state.to_plan()
        assert any(
            p.attribute == "occasion" and p.value == "wedding" for p in plan.soft
        )
        assert unexpressed == []

    def test_an_unrecognised_occasion_is_reported_not_dropped(self):
        state = PreferenceState()
        state.set_occasion("a Tuesday afternoon")
        plan, unexpressed = state.to_plan()
        assert not any(p.attribute == "occasion" for p in plan.soft)
        assert len(unexpressed) == 1
        assert unexpressed[0]["preference"] == "occasion"
        assert "Tuesday" in unexpressed[0]["reason"]
        assert "not a recognised occasion word" in unexpressed[0]["reason"]

    def test_only_the_most_recently_liked_bottle_drives_the_anchor(self):
        state = PreferenceState()
        state.record_feedback(1, "A", Verdict.LOVE)
        state.record_feedback(2, "B", Verdict.LOVE)
        plan, unexpressed = state.to_plan()
        assert plan.anchor == "B"
        assert len(unexpressed) == 1
        assert unexpressed[0]["preference"] == "liked bottle: A"
        assert "only the most recently liked bottle" in unexpressed[0]["reason"]

    def test_an_empty_state_compiles_to_an_empty_unusable_plan(self):
        state = PreferenceState()
        plan, unexpressed = state.to_plan()
        assert not plan.usable
        assert unexpressed == []


class TestComparativePreferencesNeedALiveAnchor:
    """"Less rose than Delina" only means something paired with the anchor
    that produced it — these need a real corpus so `plan.parse` actually
    resolves an anchor and produces a `relative_to_anchor` preference."""

    @pytest.fixture
    def delina(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="rose", author=author, channel=f"c{i}")
        return conn, frag

    def test_a_comparative_compiles_when_the_anchor_is_still_current(self, delina):
        conn, _frag = delina
        state = PreferenceState()
        state.merge_utterance(conn, "I love Delina but the rose is too strong")
        plan, unexpressed = state.to_plan()
        assert plan.anchor == "Parfums de Marly Delina"
        assert any(
            p.attribute == "note" and p.value == "rose" and p.relative_to_anchor
            for p in plan.soft
        )
        assert unexpressed == []

    def test_a_comparative_is_unexpressed_once_nothing_is_left_to_anchor_to(self, delina):
        """Delina is the *only* liked bottle here, so a NO on it leaves
        nothing to fall back to (F3) and the anchor genuinely goes to
        None — the "no anchor at all" branch, not the "wrong anchor"
        branch `test_a_stale_comparative_is_unexpressed_not_recompiled`
        covers below.
        """
        conn, frag = delina
        state = PreferenceState()
        state.merge_utterance(conn, "I love Delina but the rose is too strong")
        state.record_feedback(frag, "Parfums de Marly Delina", Verdict.NO)
        plan, unexpressed = state.to_plan()
        assert plan.anchor is None
        assert not any(p.relative_to_anchor for p in plan.soft)
        assert len(unexpressed) == 1
        assert unexpressed[0]["preference"] == "rose relative to Parfums de Marly Delina"
        assert unexpressed[0]["reason"] == (
            "no liked bottle is currently set to compare against"
        )

    def test_a_stale_comparative_is_unexpressed_not_recompiled(self, conn):
        """The reviewer's F2 scenario, reproduced exactly: love Delina
        (with a comparative about its rose), NO Delina, then LOVE a
        *different* bottle. The anchor is real and current (BR540) — this
        is not the "no anchor" case above — but it is not the anchor the
        comparative was measured against, and recompiling "less rose than
        Delina" as "less rose than Baccarat Rouge 540" would judge a
        comparison nobody drew. Fixed behaviour: the comparative is
        `unexpressed`, naming both bottles; it must not appear in
        `plan.soft` at all.
        """
        delina = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        br540 = add_fragrance(
            conn, "Maison Francis Kurkdjian Baccarat Rouge 540", aliases=["BR540"]
        )
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=delina, value="rose", author=author, channel=f"c{i}")

        state = PreferenceState()
        state.merge_utterance(conn, "I love Delina but the rose is too strong")
        state.record_feedback(delina, "Parfums de Marly Delina", Verdict.NO)
        state.record_feedback(
            br540, "Maison Francis Kurkdjian Baccarat Rouge 540", Verdict.LOVE
        )
        plan, unexpressed = state.to_plan()

        assert plan.anchor == "Maison Francis Kurkdjian Baccarat Rouge 540"
        assert not any(
            p.attribute == "note" and p.value == "rose" for p in plan.soft
        ), "the stale comparative must not silently recompile against BR540"
        assert len(unexpressed) == 1
        assert unexpressed[0]["preference"] == "rose relative to Parfums de Marly Delina"
        assert unexpressed[0]["reason"] == (
            "was said about Parfums de Marly Delina, current anchor is "
            "Maison Francis Kurkdjian Baccarat Rouge 540"
        )


class TestAnchorOnlyComesFromTheLikeGrammar:
    """`plan.anchor` also surfaces from PROFILE and COMPARE questions,
    which are not expressions of liking something."""

    @pytest.fixture
    def catalogue(self, conn):
        a = add_fragrance(
            conn, "Kilian Angels' Share", aliases=["Angels' Share", "Angels Share"]
        )
        b = add_fragrance(conn, "Lattafa Khamrah", aliases=["Khamrah"])
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=a, value="raspberry", author=author, channel=f"c{i}")
        return conn, a, b

    def test_a_profile_question_does_not_add_a_liked_fragrance(self, catalogue):
        conn, a, _b = catalogue
        state = PreferenceState()
        parsed = state.merge_utterance(conn, "what do people say about Angels' Share?")
        assert parsed.intent is Intent.PROFILE
        assert state.liked_fragrances == []

    def test_a_compare_question_does_not_add_a_liked_fragrance(self, catalogue):
        conn, a, b = catalogue
        for i, author in enumerate(["q1", "q2", "q3"]):
            note(conn, 10 + i, frag=b, value="coffee", author=author, channel=f"d{i}")
        state = PreferenceState()
        parsed = state.merge_utterance(
            conn, "Angels' Share vs Lattafa Khamrah"
        )
        assert parsed.intent is Intent.COMPARE
        assert state.liked_fragrances == []

    def test_a_love_grammar_anchor_does_add_a_liked_fragrance(self, catalogue):
        conn, a, _b = catalogue
        state = PreferenceState()
        state.merge_utterance(conn, "I love Angels' Share")
        assert state.liked_fragrances == ["Kilian Angels' Share"]


class TestToPlanRoundTripsThroughTheRealEngine:
    """`to_plan()`'s whole point is compiling into the exact shape
    `recommend_plan` already judges. A session built from two separate
    calls (an "I love Delina" utterance, then a "less rose" chip) should
    be judged the same way one sentence saying both at once would be."""

    @pytest.fixture
    def delina_and_alt(self, conn):
        delina = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        alt = add_fragrance(conn, "Some Other Rose")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=delina, value="rose", author=author, channel=f"c{i}")
        return conn, delina, alt

    def test_a_session_built_from_one_utterance_matches_the_equivalent_sentence(
        self, delina_and_alt
    ):
        conn, _delina, _alt = delina_and_alt
        text = "I love Delina but the rose is too strong"
        one_shot = recommend(conn, text)

        state = PreferenceState()
        state.merge_utterance(conn, text)
        plan, unexpressed = state.to_plan()
        compiled = recommend_plan(conn, plan)

        assert unexpressed == []
        assert [r.name for r in compiled.results] == [r.name for r in one_shot.results]

    def test_a_chip_avoidance_is_plain_not_anchor_relative(self, delina_and_alt):
        """A genuine, documented distinction rather than a bug: "the rose
        is too strong" inside a sentence *with* an anchor present is read
        by `plan.py` as a comparison against that anchor
        (`Direction.LESS_THAN_ANCHOR`, `_score_comparative`'s margin
        logic). A chip only ever says "less rose, plainly"
        (`Direction.LOW`, plain avoidance costing a candidate rather than
        comparing it) — `classify_attribute` has no anchor to reason
        about, and manufacturing a comparative preference from a bare
        chip would assert a comparison nobody drew. The two are not
        interchangeable, and this session — built with a LOVE plus a
        LESS chip for the same word a comparative sentence would use —
        is deliberately not expected to match the one-shot sentence
        above.
        """
        conn, _delina, _alt = delina_and_alt
        state = PreferenceState()
        state.merge_utterance(conn, "I love Delina")
        state.merge_preference("rose", Direction.LESS)
        plan, unexpressed = state.to_plan()
        assert unexpressed == []
        assert not any(p.relative_to_anchor for p in plan.soft)
        assert any(
            p.attribute == "note" and p.value == "rose"
            and p.direction is PlanDirection.LOW
            for p in plan.soft
        )

    def test_a_compiled_plans_recommendation_still_passes_digest_and_phrase(
        self, delina_and_alt
    ):
        """Not a new claim about wording — `to_plan`'s output is judged
        by the unmodified `recommend_plan`, so anything true of the
        provenance discipline elsewhere is true here too."""
        conn, _delina, _alt = delina_and_alt
        state = PreferenceState()
        state.merge_utterance(conn, "I love Delina")
        state.merge_preference("rose", Direction.LESS)
        plan, _ = state.to_plan()
        answer = recommend_plan(conn, plan)
        for candidate in answer.results:
            digest(candidate)  # must not raise
            for reason in candidate.reasons + candidate.caveats:
                assert reason.phrase()
                if not reason.declarable:
                    assert reason.strength is not Strength.SUPPORTED or reason.inferred


class TestRebuildReplaysEventsToTheSameState:
    def test_replaying_say_and_feedback_events_matches_calling_them_directly(self):
        direct = PreferenceState()
        direct.merge_utterance(None, "something feminine")
        direct.record_feedback(1, "X", Verdict.LOVE)
        direct.record_feedback(2, "Y", Verdict.NO)

        events = [
            ("say", {"text": "something feminine"}),
            ("feedback", {"fragrance_id": 1, "name": "X", "verdict": "love"}),
            ("feedback", {"fragrance_id": 2, "name": "Y", "verdict": "no"}),
        ]
        replayed = rebuild(events)

        assert replayed.summary() == direct.summary()

    def test_replaying_prefs_events_covers_all_three_shapes(self):
        events = [
            ("prefs", {"attribute": "feminine", "direction": "more"}),
            ("prefs", {"occasion": "wedding"}),
            ("prefs", {"budget_usd": 120.0}),
        ]
        state = rebuild(events)
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE
        assert state.occasion == "wedding"
        assert state.budget_usd == 120.0

    def test_a_single_prefs_event_can_carry_all_three_shapes_at_once(self):
        """F11: `/prefs` now merges every field a body provides into one
        event rather than recording only the first shape it finds — this
        is what `rebuild` has to be able to replay for that fix to mean
        anything."""
        events = [
            ("prefs", {
                "attribute": "feminine", "direction": "more",
                "occasion": "wedding", "budget_usd": 250.0,
            }),
        ]
        state = rebuild(events)
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE
        assert state.occasion == "wedding"
        assert state.budget_usd == 250.0

    def test_an_unrecognised_event_kind_is_skipped_not_raised(self):
        events = [
            ("say", {"text": "something feminine"}),
            ("some-future-kind", {"whatever": "shape"}),
        ]
        state = rebuild(events)
        assert state.attribute_prefs["vibe:feminine"] is Direction.MORE

    def test_a_poison_event_with_an_invalid_direction_is_skipped_with_a_warning(
        self, caplog
    ):
        """F1's second half: `rebuild` must never let one bad value in
        the log make the whole session unreadable. `/prefs` validates
        before writing (see `api.py`), so this simulates a value that
        reached `session_events` some other way — a hand-inserted row, a
        renamed enum member — the case `rebuild`'s own defence is for.
        """
        events = [
            ("prefs", {"attribute": "sweet", "direction": "more"}),
            ("prefs", {"attribute": "loud", "direction": "wibble"}),
            ("prefs", {"attribute": "feminine", "direction": "less"}),
        ]
        state = rebuild(events)
        assert state.attribute_prefs["note:sweet"] is Direction.MORE
        assert state.attribute_prefs["vibe:feminine"] is Direction.LESS
        assert not any("wibble" in str(v) for v in state.attribute_prefs.values())
        assert any("wibble" in r.message for r in caplog.records)

    def test_a_poison_feedback_event_is_skipped_leaving_valid_ones_intact(self):
        events = [
            ("feedback", {"fragrance_id": 1, "name": "X", "verdict": "love"}),
            ("feedback", {"fragrance_id": 2, "name": "Y", "verdict": "sideways"}),
            ("feedback", {"fragrance_id": 3, "name": "Z", "verdict": "no"}),
        ]
        state = rebuild(events)
        assert state.liked_fragrances == ["X"]
        assert state.disliked_fragrances == ["Z"]
        assert "Y" not in state.liked_fragrances
        assert "Y" not in state.disliked_fragrances

    def test_replay_order_determines_the_final_override(self):
        events = [
            ("prefs", {"attribute": "feminine", "direction": "more"}),
            ("say", {"text": "not feminine, please"}),
        ]
        state = rebuild(events)
        assert state.attribute_prefs["vibe:feminine"] is Direction.LESS
