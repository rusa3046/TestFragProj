"""The commerce presentation layer — `commerce_card.py`.

No database needed anywhere in this file: every input is a hand-built
`Reason`/`Recommendation`/`QueryPlan`, the same discipline
`tests/test_api.py::TestLabelDerivation` already uses for `_label`. That is
deliberate — this module's whole job is re-arranging what `recommend.py`
already computed, so a test here should never need evidence collection
machinery to prove it.
"""

from fragrance_graph.commerce_card import (
    MAX_FIT_SIGNALS,
    MAX_TRADEOFFS,
    Card,
    EvidenceClass,
    build_card,
    classify,
    fit_signal_text,
    headline,
    mixed_text,
    result_tier,
    tradeoff_text,
)
from fragrance_graph.evidence import Strength
from fragrance_graph.plan import Constraint, Direction, Preference, QueryPlan
from fragrance_graph.recommend import Reason, Recommendation


def reason(kind, text="value", strength=Strength.SUPPORTED, people=3, creators=2,
           videos=2, against=0, attribute=""):
    return Reason(kind=kind, text=text, strength=strength, people=people,
                  creators=creators, videos=videos, against=against,
                  attribute=attribute)


# --- evidence classification -----------------------------------------------


class TestClassify:
    def test_a_matched_preference_reason_is_a_fit_signal(self):
        assert classify(reason("prefer"), in_caveats=False) is EvidenceClass.FIT_SIGNAL
        assert classify(reason("constraint"), in_caveats=False) is EvidenceClass.FIT_SIGNAL
        assert classify(reason("concept"), in_caveats=False) is EvidenceClass.FIT_SIGNAL
        assert classify(reason("comparative"), in_caveats=False) is EvidenceClass.FIT_SIGNAL
        assert classify(reason("declared_overlap"), in_caveats=False) is EvidenceClass.FIT_SIGNAL

    def test_a_semantic_reason_is_irrelevant_negative_never_a_fit_signal(self):
        """§17-27/§25: a vector-retrieved quote, verbatim, never mapped to
        a requested dimension — excluded from every customer surface on
        principle, whatever its actual content. Content-independence is
        the point: this is true whether the quoted text is flattering or
        the word "trash.\""""
        hostile = reason("semantic", text="someone described it as 'total trash'")
        flattering = reason("semantic", text="someone described it as 'gorgeous'")
        assert classify(hostile, in_caveats=False) is EvidenceClass.IRRELEVANT_NEGATIVE
        assert classify(flattering, in_caveats=False) is EvidenceClass.IRRELEVANT_NEGATIVE

    def test_graph_and_absence_reasons_are_neither_fit_signal_nor_irrelevant(self):
        """Neutral, L2-only evidence — proximity/retrieval facts that are
        real and not negative, just not preference-mapped. `classify`
        returns `None` for them, not a class a caller has to remember to
        filter."""
        assert classify(reason("graph"), in_caveats=False) is None
        assert classify(reason("absence"), in_caveats=False) is None

    def test_a_disputed_caveat_is_disagreement(self):
        assert classify(
            reason("avoid", against=2), in_caveats=True
        ) is EvidenceClass.DISAGREEMENT
        assert classify(
            reason("disputed", strength=Strength.CONTESTED), in_caveats=True
        ) is EvidenceClass.DISAGREEMENT

    def test_a_settled_caveat_is_tradeoff(self):
        assert classify(reason("avoid"), in_caveats=True) is EvidenceClass.TRADEOFF

    def test_a_materially_contested_reason_is_disagreement_not_fit_signal(self):
        """Fix-round F1: a `"prefer"` reason (a matched *want*/*like*
        dimension) with real opposition must not classify `FIT_SIGNAL`
        just because it lives in `reasons[]` — `reason.against` is only
        ever non-zero when `evidence.strength_of` already graded the
        underlying fact `CONTESTED` (the existing ratio/floor machinery),
        so this is the exact "7 people say so and 3 disagree" shape the
        real corpus produced (`commerce.json`'s Khamrah card) and used to
        render as "Wearers consistently describe it as strong
        projection.\" — an overclaim on a disputed fact."""
        contested = reason("prefer", against=3, people=7, creators=3)
        assert classify(contested, in_caveats=False) is EvidenceClass.DISAGREEMENT
        # Every positive-match kind, not only "prefer" — a hard constraint
        # or concept match with real opposition overclaims identically.
        for kind in ("constraint", "concept", "comparative"):
            assert classify(
                reason(kind, against=2, people=5, creators=2), in_caveats=False
            ) is EvidenceClass.DISAGREEMENT


# --- result_tier -------------------------------------------------------


class TestResultTier:
    def test_no_positive_reasons_is_exploratory_pick(self):
        candidate = Recommendation(1, "X", reasons=[reason("graph")])
        assert result_tier(candidate) == "EXPLORATORY_PICK"

    def test_a_single_source_match_is_partial_fit_never_rejected(self):
        """Independence gates language, never eligibility for a tier at
        all — see `commerce-audit.md` §1/§4. A `SINGLE_SOURCE` fact is
        real evidence the request was answered."""
        candidate = Recommendation(
            1, "X", reasons=[reason("prefer", people=1, creators=1, videos=0)]
        )
        assert result_tier(candidate) == "PARTIAL_FIT"

    def test_two_moderate_matches_is_strong_fit(self):
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", people=2, creators=2, videos=0),
                     reason("concept", people=3, creators=2, videos=2)],
        )
        assert result_tier(candidate) == "STRONG_FIT"

    def test_one_strong_match_alone_is_good_fit_not_strong(self):
        """spec: "#1 can be a Good Fit" — one very well-evidenced matched
        dimension is not enough for `STRONG_FIT` on its own."""
        candidate = Recommendation(
            1, "X", reasons=[reason("prefer", people=6, creators=3, videos=2)]
        )
        assert result_tier(candidate) == "GOOD_FIT"

    def test_caveats_never_influence_result_tier(self):
        candidate = Recommendation(
            1, "X", reasons=[reason("graph")],
            caveats=[reason("avoid", people=9, creators=5, videos=3)],
        )
        assert result_tier(candidate) == "EXPLORATORY_PICK"

    def test_two_strong_matches_with_no_problems_is_strong_fit(self):
        """The uncapped baseline every F5 test below is a variation on —
        confirms the two-strong-matches shape really does reach
        `STRONG_FIT` when nothing contradicts or contests it, so the
        capping tests actually prove something was capped."""
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", people=6, creators=3, videos=2),
                     reason("concept", people=6, creators=3, videos=2)],
        )
        assert result_tier(candidate) == "STRONG_FIT"

    def test_a_contested_want_caps_strong_fit_at_good_fit(self):
        """Fix-round F5: two otherwise-`STRONG_FIT`-grade matches, but one
        of them is materially disputed (`against` set) — the real corpus's
        Khamrah card (`commerce.json`): "strong projection" evidence was
        7-for/3-against, and the pre-fix `_label` still returned
        `STRONG_FIT`. A `CONTESTED` want-dimension is a genuine,
        shopper-relevant problem this candidate has; `STRONG_FIT` must not
        paper over it, whatever the card's *other* evidence looks like."""
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", people=7, creators=3, videos=2, against=3),
                     reason("concept", people=6, creators=3, videos=2)],
        )
        assert result_tier(candidate) == "GOOD_FIT"

    def test_a_contradicted_explicit_avoid_caps_strong_fit_at_good_fit(self):
        """Fix-round F5: same two strong matches, but the candidate's own
        evidence contradicts an explicit `avoid` (a `kind="avoid"`
        caveat — only ever appended when the evidence actually supports
        the avoided value). The real corpus's Khamrah card had exactly
        this (an "apple pie with cinnamon" caveat against an
        avoid-cinnamon chip) and was still labelled `STRONG_FIT` before
        this fix."""
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", people=6, creators=3, videos=2),
                     reason("concept", people=6, creators=3, videos=2)],
            caveats=[reason("avoid", text="cinnamon", people=1, creators=1)],
        )
        assert result_tier(candidate) == "GOOD_FIT"

    def test_a_contested_want_does_not_cap_good_fit_or_partial_fit(self):
        """The cap only ever pulls `STRONG_FIT` down to `GOOD_FIT` — it
        does not additionally punish a candidate that was never going to
        reach `STRONG_FIT` in the first place (spec's fix-round: "cap at
        GOOD_FIT," not "cap at PARTIAL_FIT")."""
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", people=7, creators=3, videos=2, against=3)],
        )
        # One contested reason alone: no FIT_SIGNAL reasons remain at all
        # (the only reason present was reclassified DISAGREEMENT), so this
        # is EXPLORATORY_PICK, not a capped GOOD_FIT — a distinct, correct
        # outcome from "matched but disputed."
        assert result_tier(candidate) == "EXPLORATORY_PICK"


# --- fit-signal wording (tier-gated) -----------------------------------


class TestFitSignalWording:
    def test_strong_tier_wording(self):
        text = fit_signal_text(reason("prefer", text="feminine",
                                       people=4, creators=3, videos=2))
        assert text == "Wearers consistently describe it as feminine."

    def test_moderate_tier_wording(self):
        text = fit_signal_text(reason("prefer", text="feminine",
                                       people=2, creators=2, videos=0))
        assert text == "Several wearers describe it as feminine."

    def test_limited_tier_wording(self):
        text = fit_signal_text(reason("prefer", text="feminine",
                                       people=2, creators=1, videos=0))
        assert text == "There is some evidence that it is feminine."

    def test_single_source_tier_wording(self):
        text = fit_signal_text(reason("prefer", text="feminine",
                                       people=1, creators=1, videos=0))
        assert text == "One commenter said it is feminine."

    def test_a_constraint_match_states_the_preference_not_a_tier_claim(self):
        """A hard-constraint match is a catalogue fact ("this is required
        and present"), not a graded community-confidence claim — it must
        not borrow `STRONG`/`MODERATE` wording for what is really just
        "yes, you asked for this and it's here.\""""
        text = fit_signal_text(reason("constraint", text="raspberry"))
        assert text == "Matches your preference for raspberry."


class TestAttributeAwareWording:
    """Fix-round F2: "describe it as strong projection"/"said it is
    occasion summer" were template misapplications — a performance fact
    and an occasion fact each need their own register, not the note/vibe
    one stretched to cover them. Dispatched via `Reason.attribute`
    (populated by `recommend._fact_reason` from the underlying
    `AttributeFact.attribute`), exactly the shape the real corpus produces
    (`_fact_reason`'s `"{attribute} {value}"` text for anything that is
    not a note/vibe)."""

    def test_performance_fact_uses_report_not_describe(self):
        text = fit_signal_text(reason(
            "prefer", text="projection strong", attribute="projection",
            people=4, creators=3, videos=2,
        ))
        assert text == "Wearers consistently report strong projection."

    def test_moderate_performance_wording(self):
        text = fit_signal_text(reason(
            "prefer", text="projection strong", attribute="projection",
            people=2, creators=2, videos=0,
        ))
        assert text == "Several wearers report strong projection."

    def test_limited_and_single_source_performance_wording(self):
        limited = fit_signal_text(reason(
            "prefer", text="longevity long lasting", attribute="longevity",
            people=3, creators=1, videos=0,
        ))
        assert limited == "There is some evidence of long lasting."
        single = fit_signal_text(reason(
            "prefer", text="longevity long lasting", attribute="longevity",
            people=1, creators=1, videos=0,
        ))
        assert single == "One commenter reported long lasting."

    def test_occasion_fact_uses_wear_framing_not_the_bare_attribute_name(self):
        """The exact regression: "One commenter said it is occasion
        summer." — the raw `"{attribute} {value}"` text leaking through a
        template built for a bare value."""
        text = fit_signal_text(reason(
            "prefer", text="occasion summer", attribute="occasion",
            people=1, creators=1, videos=0,
        ))
        assert text == "One commenter confirmed it for summer wear."
        assert "occasion" not in text.lower()

    def test_occasion_strong_and_limited_wording(self):
        strong = fit_signal_text(reason(
            "prefer", text="occasion summer", attribute="occasion",
            people=5, creators=3, videos=2,
        ))
        assert strong == "Wearers consistently confirm it for summer wear."
        limited = fit_signal_text(reason(
            "prefer", text="occasion summer", attribute="occasion",
            people=2, creators=1, videos=0,
        ))
        assert limited == "There is evidence for summer wear."

    def test_note_and_vibe_facts_are_unaffected_by_the_new_tables(self):
        """No `attribute` set (the default `reason()` shape every other
        test in this file already uses) still gets the original note/vibe
        wording — the attribute-aware dispatch is additive, not a
        behaviour change for the common case."""
        text = fit_signal_text(reason("prefer", text="feminine",
                                       people=4, creators=3, videos=2))
        assert text == "Wearers consistently describe it as feminine."


class TestMixedWording:
    """Fix-round F1: the mandated, and only permissible, wording for a
    materially contested dimension (spec §32)."""

    def test_performance_mixed_wording_names_the_bare_axis(self):
        """"Projection opinions are mixed across wearers." — never
        "strong projection opinions are mixed," which would silently
        assert the very side ("strong") the dispute has not settled."""
        text = mixed_text(reason(
            "prefer", text="projection strong", attribute="projection",
            people=7, creators=3, against=3,
        ))
        assert text == "Projection opinions are mixed across wearers."

    def test_note_or_vibe_mixed_wording_names_the_requested_value(self):
        text = mixed_text(reason("prefer", text="feminine", people=5,
                                  creators=3, against=2))
        assert text == "Feminine opinions are mixed across wearers."

    def test_mixed_wording_never_says_consistently_or_claims_a_match(self):
        text = mixed_text(reason(
            "prefer", text="projection strong", attribute="projection",
            people=7, creators=3, against=3,
        ))
        for overclaim in ("consistently", "matches your preference", "several wearers"):
            assert overclaim not in text.lower()


class TestTradeoffVoice:
    """Fix-round F3: L1 tradeoffs use the customer tier register, never
    `Reason.phrase()`'s internal attribution disclosure — that stays in
    `reasons[]`/`caveats[]`/L2/L3 untouched (`tradeoff_text` never reads
    or reproduces it)."""

    def test_no_attribution_clause_in_customer_tradeoff_text(self):
        internal = reason(
            "avoid", text="apple pie with cinnamon", people=1, creators=1,
            strength=Strength.OBSERVED, videos=0,
        )
        # The internal register really does carry the attribution clause
        # — confirms the fixture is representative of the real corpus
        # case (`commerce.json`'s Khamrah caveat), and that `phrase()`
        # itself is untouched by this fix.
        text = tradeoff_text(internal)
        assert "rather than naming it" not in text
        assert "on videos about" not in text

    def test_single_source_and_limited_tradeoffs_share_one_cautious_wording(self):
        limited = tradeoff_text(reason("avoid", text="cinnamon", people=3, creators=1))
        single = tradeoff_text(reason("avoid", text="cinnamon", people=1, creators=1))
        assert limited == single == "There is some evidence of cinnamon."

    def test_strong_and_moderate_tradeoffs_get_their_own_wording(self):
        assert tradeoff_text(
            reason("avoid", text="cinnamon", people=5, creators=3, videos=2)
        ) == "Wearers consistently note cinnamon."
        assert tradeoff_text(
            reason("avoid", text="cinnamon", people=2, creators=2, videos=0)
        ) == "Several wearers note cinnamon."


# --- build_card: caps, filtering, no empty bullets ----------------------


class TestBuildCard:
    def test_fit_signals_capped_and_tradeoffs_capped(self):
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", text=f"note{i}", people=3, creators=2, videos=2)
                     for i in range(8)],
            caveats=[reason("avoid", text=f"caveat{i}", people=3, creators=2)
                     for i in range(6)],
        )
        card = build_card(candidate)
        assert len(card.fit_signals) == MAX_FIT_SIGNALS
        assert len(card.relevant_tradeoffs) == MAX_TRADEOFFS

    def test_asymmetry_four_fit_signals_and_six_irrelevant_negatives(self):
        """§17/§43's asymmetry regression: 4 genuine fit signals plus 6
        unrelated (`semantic`, irrelevant-negative) reasons on the same
        candidate must produce exactly 4 fit signals and 0 tradeoffs —
        the irrelevant reasons never leak into either list, whatever their
        count."""
        candidate = Recommendation(
            1, "X",
            reasons=(
                [reason("prefer", text=f"note{i}", people=3, creators=2, videos=2)
                 for i in range(4)]
                + [reason("semantic", text=f"someone described it as 'thing {i}'")
                   for i in range(6)]
            ),
        )
        card = build_card(candidate)
        assert len(card.fit_signals) == 4
        assert card.relevant_tradeoffs == []

    def test_no_empty_bullets_even_from_a_pathological_reason(self):
        """§36's fix, at this module's own filter: a `Reason(text="")`
        (the reproduction in `commerce-audit.md` §8) must never reach
        `fit_signals`/`relevant_tradeoffs` as a blank or dash-led entry —
        `_nonempty` strips it before the list is ever built, so its length
        and its rendered content can never disagree."""
        pathological_reason = reason("prefer", text="", people=3, creators=2)
        pathological_caveat = reason("avoid", text="", against=2, people=3, creators=2)
        candidate = Recommendation(
            1, "X", reasons=[pathological_reason], caveats=[pathological_caveat],
        )
        card = build_card(candidate)
        assert card.fit_signals == []
        assert card.relevant_tradeoffs == []

    def test_result_tier_matches_the_standalone_function(self):
        candidate = Recommendation(
            1, "X", reasons=[reason("prefer", people=6, creators=3, videos=2)]
        )
        card = build_card(candidate)
        assert card.result_tier == result_tier(candidate) == "GOOD_FIT"

    def test_a_contested_fit_reason_moves_to_tradeoffs_as_mixed_wording(self):
        """Fix-round F1, at the `build_card` level: a `"prefer"` reason
        with real opposition never reaches `fit_signals`, and shows up in
        `relevant_tradeoffs` as the mandated mixed sentence — the exact
        shape of the real corpus's Khamrah card before this fix
        (`commerce.json`: "Wearers consistently describe it as strong
        projection." in `fit_signals`, nothing in `relevant_tradeoffs`
        naming the dispute at all)."""
        candidate = Recommendation(
            1, "X",
            reasons=[reason("prefer", text="projection strong", attribute="projection",
                             people=7, creators=3, videos=2, against=3)],
        )
        card = build_card(candidate)
        assert card.fit_signals == []
        assert card.relevant_tradeoffs == ["Projection opinions are mixed across wearers."]

    def test_a_caveat_tradeoff_uses_the_customer_register_not_phrase(self):
        """Fix-round F3, at the `build_card` level."""
        candidate = Recommendation(
            1, "X",
            caveats=[reason(
                "avoid", text="apple pie with cinnamon", people=1, creators=1,
                strength=Strength.OBSERVED, videos=0,
            )],
        )
        card = build_card(candidate)
        assert card.relevant_tradeoffs == ["There is some evidence of apple pie with cinnamon."]

    def test_matched_uncertain_contradicted_come_from_the_preference_matrix(self):
        """Fix-round F4: with a `preference_status` matrix supplied,
        `matched`/`uncertain`/`contradicted` read from it — the same cells
        that decided the fit signals/tradeoffs — rather than
        `candidate.matched`/`unmatched`, which `recommend._score` only
        ever populates for *hard* constraints (empty for an all-soft
        composer request, which is exactly why the real corpus's Khamrah
        card had `"matched": []` despite two genuinely matched
        preferences)."""
        candidate = Recommendation(1, "X")  # matched/unmatched left empty on purpose
        preference_status = [
            {"value": "unique", "entity_type": "vibe", "match_kind": "match"},
            {"value": "feminine", "entity_type": "vibe", "match_kind": "match"},
            {"value": "gorgeous", "entity_type": "vibe", "match_kind": "unknown"},
            {"value": "cinnamon", "entity_type": "note", "match_kind": "contradiction"},
            {"value": "strong projection", "entity_type": "performance",
             "match_kind": "partial_match"},
        ]
        card = build_card(candidate, preference_status)
        assert card.matched == ["unique", "feminine"]
        assert card.uncertain == ["gorgeous"]
        assert card.contradicted == ["cinnamon", "strong projection"]

    def test_without_a_preference_status_matrix_falls_back_honestly(self):
        """The stateless surfaces (`/api/recommend`, `/api/fragrance/
        {id}`) have no composer `PreferenceState` and so no matrix to
        pass — `build_card` still returns something, from
        `candidate.matched`/`unmatched`, rather than raising or silently
        fabricating a matrix that was never computed."""
        candidate = Recommendation(1, "X", matched=["note=raspberry"])
        card = build_card(candidate)
        assert card.matched == ["note=raspberry"]

    def test_card_is_a_plain_dataclass_with_no_numeric_score(self):
        """No field on `Card` may be a bare confidence number — the same
        "no percentage anywhere" rule `api.py`'s module docstring already
        holds `label` to, extended to the commerce card."""
        import dataclasses

        candidate = Recommendation(1, "X", reasons=[reason("prefer")])
        card = build_card(candidate)
        assert isinstance(card, Card)
        for f in dataclasses.fields(card):
            if f.name == "fragrance_id":
                continue  # an identifier, not a confidence score
            value = getattr(card, f.name)
            assert not isinstance(value, (int, float)) or isinstance(value, bool), (
                f"Card.{f.name} is a bare number: {value!r}"
            )


# --- headline (§1, §26-27) ----------------------------------------------


class TestHeadline:
    def test_positive_framing_never_the_engines_internal_vocabulary(self):
        """The §1 failure case, directly: LIKE gorgeous+unique, AVOID
        cinnamon, WANT strong+feminine, OCC summer — the exact
        combination the spec's failure case names. The headline must open
        positively and must never carry engine-internal vocabulary."""
        plan = QueryPlan(
            text="",
            hard=[],
            soft=[
                Preference("vibe", "gorgeous", Direction.HIGH, said="gorgeous"),
                Preference("vibe", "unique", Direction.HIGH, said="unique"),
                Preference("note", "cinnamon", Direction.LOW, said="cinnamon"),
                Preference("projection", "strong", Direction.HIGH, said="strong"),
                Preference("vibe", "feminine", Direction.HIGH, said="feminine"),
                Preference("occasion", "summer", Direction.HIGH, said="summer"),
            ],
        )
        results = [Recommendation(1, "Some Bottle", reasons=[reason("prefer")])]
        text = headline(plan, results)
        assert text.startswith("Your closest matches")
        lowered = text.lower()
        for jargon in ("independence", "bar", "threshold", "gate", "consensus"):
            assert jargon not in lowered, f"{jargon!r} leaked into the headline: {text!r}"
        assert "cinnamon" in text  # the deprioritized dimension is still named, honestly

    def test_empty_results_names_the_hard_constraint_not_the_refusal(self):
        plan = QueryPlan(
            text="", hard=[Constraint("note", "unobtainium", said="unobtainium")],
        )
        text = headline(plan, [])
        assert "unobtainium" in text
        lowered = text.lower()
        for jargon in ("independence", "bar", "threshold", "gate", "consensus"):
            assert jargon not in lowered

    def test_no_preferences_at_all_still_produces_a_positive_sentence(self):
        plan = QueryPlan(text="")
        results = [Recommendation(1, "X", reasons=[reason("prefer")])]
        text = headline(plan, results)
        assert text == "Your closest matches, based on what the corpus supports."

    def test_a_safe_specific_engine_note_is_kept_verbatim(self):
        """Not every `answer.note` is the problem — most of
        `recommend._note`'s branches are already specific and honest (the
        anchor-profile fallback's basis-switch sentence, a hard
        constraint's own listing). Replacing those with the generic
        "Your closest matches" would throw away real information; this
        pins the regression found by
        `tests/test_facet_cases.py::TestSideEffectButTooWarm` while this
        module was being built."""
        plan = QueryPlan(text="")
        results = [Recommendation(1, "X", reasons=[reason("prefer")])]
        engine_note = (
            "Not enough people have compared these directly; matches below "
            "share Side Effect's official notes, leaving out its warm ones."
        )
        assert headline(plan, results, engine_note) == engine_note

    def test_the_discouraging_independence_bar_note_is_still_replaced(self):
        """The one branch this module exists to fix (`commerce-audit.md`
        §1) — even though `engine_note` is non-empty, it names an internal
        audit concept a shopper has no reason to know, so it is replaced
        with the generic positive headline, not passed through."""
        plan = QueryPlan(text="")
        results = [Recommendation(1, "X", reasons=[reason("prefer")])]
        engine_note = (
            "No match below clears the independence bar — the evidence is "
            "there, but not from enough separate creators to call it "
            "consensus."
        )
        text = headline(plan, results, engine_note)
        assert text != engine_note
        assert text == "Your closest matches, based on what the corpus supports."
