"""The commerce presentation layer: evidence, turned into a shopper's card.

    from fragrance_graph.commerce_card import build_card, headline
    card = build_card(candidate, plan)
    print(headline(plan, results))

FACET is a selling tool (spec: "make the best recommendation under
incomplete evidence, explain why worth trying, surface only relevant
tradeoffs"). `recommend.py` judges evidence and writes sentences *about*
that evidence — its own module docstring is explicit that nothing there
has a notion of a shopper's screen. `session.py` accumulates state and
compiles a plan, and is equally explicit that it "owns no evidence, grades
nothing, chooses no bottle." Neither is the right place for "what does a
shopper need to see," so this module is the third leg: it takes what
`recommend.py` already computed — `Recommendation.reasons`/`caveats`,
already audited, already worded — and re-sorts, tier-templates and caps
it into the object a storefront actually renders. It never invents a fact;
every sentence here traces back to a `Reason` `recommend.py` already built,
the same discipline `recommend.digest()` already holds itself to for the
evidence-detail summary.

## Independence lives in the CLAIM tier, never in eligibility

The audit (`commerce-audit.md` §1, §3) found that `gate.MIN_COMMENTERS`/
`MIN_SOURCES` already do not exclude a candidate from `recommend_plan`'s
`results` — a single-source fact is enough to satisfy a hard constraint and
become a candidate. What independence actually governs is *how confidently
a sentence may be worded* about one fact (`evidence.ConfidenceTier`,
`Strength.may_declare`), and, before this module existed, the one
customer-visible sentence that read as a gate: `recommend._note`'s
"No match below clears the independence bar" being handed to the shopper
verbatim. This module is the fix for that — but narrowly: `headline`
passes `answer.note` through whenever it is *already* safe (most of
`_note`'s branches are specific and honest — an anchor-profile fallback
explaining a basis switch, a hard constraint naming what was required —
and replacing them with a generic sentence would throw away real
information). Only the discouraging branches are replaced; see
`headline`'s own docstring for exactly which. `build_card` always writes
tier-gated sentences regardless — a `SINGLE_SOURCE` fact still earns a
card slot, just in single-commenter language, never a rejection.

## Evidence classes (spec §17-27)

Every `Reason` a candidate carries is classified once, from fields
`recommend.py` already computed — no new evidence collection here:

    FIT_SIGNAL          reasons[] built from a matched request dimension
                         (kind in `_POSITIVE_MATCH_KINDS`) — the request
                         asked for this and the corpus answered, on
                         evidence that is not itself materially disputed.
    TRADEOFF             caveats[] that are not a live dispute — a
                         relevant thing to weigh, on settled-enough
                         evidence.
    DISAGREEMENT          any reason or caveat where the evidence itself
                         is split (`Reason.against`, or
                         `Strength.CONTESTED`) — "opinions differ," never
                         "here is what's wrong," and never a clean fit
                         signal either, however it started out. A
                         `"prefer"` reason with real opposition (spec
                         §32's "2-for/4-against" example, generalised) is
                         reclassified out of `reasons[]`'s normal
                         `FIT_SIGNAL` fate and into this class — material
                         opposition is checked before anything else in
                         `classify`, in either list.
    IRRELEVANT_NEGATIVE   `kind == "semantic"` reasons — a vector-retrieved
                         quote, verbatim from one commenter, never filtered
                         for tone and never mapped to a requested
                         dimension (the audit's Q9). Excluded from every
                         customer surface this module builds, on principle,
                         not because any specific quote happens to look bad
                         — the raw claim stays in the evidence store and in
                         `candidate.reasons` for the debug view.

`graph`/`absence`/`comparative`(unmatched) reasons are neither FIT_SIGNAL
nor IRRELEVANT_NEGATIVE — they are real, neutral evidence (anchor
proximity, an honest "nothing recorded") that belongs in the L2 "full
story," not in a capped, preference-mapped L1 list. `classify` returns
`None` for them; callers that only want L1 content filter those out simply
by discarding `None`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fragrance_graph.evidence import SENTIMENT_AXES, ConfidenceTier, Support, tier_of
from fragrance_graph.gate import MIN_SOURCES
from fragrance_graph.plan import Direction, QueryPlan
from fragrance_graph.recommend import Reason, Recommendation, _join_and

#: Reason kinds that exist *because* a candidate satisfied something the
#: plan actually asked for, as opposed to `"graph"`/`"semantic"`/
#: `"absence"`, which surface a candidate through proximity or retrieval.
#: The single source of truth for this list — `api._label` used to keep
#: its own copy; it now imports this one, so the two can never drift about
#: which kinds count as "answered the request."
POSITIVE_MATCH_KINDS = frozenset(
    {
        "constraint", "prefer", "concept", "comparative", "declared_overlap",
        # `catalog_fit` (`recommend._catalog_signal`/`_catalog_occasion_
        # signal`): the T2 CATALOG-DERIVED positive signal (a declared
        # note absent for an avoider, a family/occasion tendency match) —
        # answers a requested dimension exactly as `prefer`/`constraint`
        # do, on catalog rather than community evidence. See
        # `result_tier`'s docstring for why it is graded on its own axis
        # rather than folded into the community count.
        "catalog_fit",
    }
)

#: `fit_signals`/`relevant_tradeoffs` caps — spec §17-27: "3-5 fit signals,
#: at most 1-2 tradeoffs, asymmetric by design." Named constants so a test
#: can assert against them rather than a bare literal, and so the rationale
#: (not "we picked a number," a genuine asymmetry: a customer needs several
#: reasons to say yes and at most a couple of things to weigh against it)
#: is documented once.
MAX_FIT_SIGNALS = 5
MIN_FIT_SIGNALS_PREFERRED = 3
MAX_TRADEOFFS = 2


class EvidenceClass(StrEnum):
    FIT_SIGNAL = "fit_signal"
    TRADEOFF = "tradeoff"
    DISAGREEMENT = "disagreement"
    IRRELEVANT_NEGATIVE = "irrelevant_negative"


def classify(reason: Reason, *, in_caveats: bool) -> EvidenceClass | None:
    """One `Reason`'s evidence class — see the module docstring's "Evidence
    classes" section. `in_caveats` tells the caller which of a candidate's
    two lists `reason` came from, because the same `kind` means a different
    thing in each (a `"comparative"` reason in `reasons` is a satisfied
    comparative; the same kind in `caveats` is a disputed or too-close one
    — see `recommend._score_comparative`).

    Material opposition (`reason.against`, only ever non-zero when
    `evidence.strength_of` already graded the underlying fact `CONTESTED`
    — the existing ratio/floor machinery, `CONTESTED_MIN_PEOPLE`/
    `CONTESTED_MIN_RATIO`, not a second rule invented here) wins over
    everything else, in *either* list: a fact 7 people support and 3
    contradict is not a clean fit signal just because it started life in
    `reasons` — spec §32's own example ("2-for/4-against ... never MATCH")
    generalised. `commerce-audit.md`'s fix-round found this: a `"prefer"`
    reason with `against=3` was classified `FIT_SIGNAL` and worded
    "Wearers consistently describe it as strong projection," which is
    exactly the overclaim the whole `Strength.CONTESTED` grade exists to
    prevent one layer down. Checking `reason.against` first, before the
    `in_caveats` branch, is what makes this impossible to reintroduce by
    adding a new positive-match kind later without remembering the
    contested case."""
    if reason.against:
        return EvidenceClass.DISAGREEMENT
    if not in_caveats:
        if reason.kind == "semantic":
            return EvidenceClass.IRRELEVANT_NEGATIVE
        if reason.kind in POSITIVE_MATCH_KINDS:
            return EvidenceClass.FIT_SIGNAL
        return None  # graph / absence: neutral, L2-only
    # caveats are always preference-driven in this codebase today (the
    # audit's Q9 traced every append site) — the open question is only
    # whether the evidence itself is disputed (`reason.against`, handled
    # above) or `strength.name == "CONTESTED"` on its own — a comparative
    # caveat (`kind="comparative"`, `candidate_level.contested`) carries
    # `Strength.CONTESTED` without ever setting `against` (there is no
    # single opposing headcount for "people disagree about this bottle,
    # so it cannot be compared"), so both checks stay.
    if reason.strength.name == "CONTESTED":
        return EvidenceClass.DISAGREEMENT
    return EvidenceClass.TRADEOFF


# --- tier-gated fit-signal wording (spec §22-23) --------------------------
#
# A NEW, audited template set — distinct from `Reason.phrase()`, which is
# the engineering register ("8 people across 4 channels say"). These four
# are the shopper register: what a storefront says instead of a citation
# count. Registered with `audit.py` (`_audit_commerce`) exactly like every
# other wording surface in this codebase, so a hostile subject string
# reaching a template is still checked for `FORBIDDEN` phrasing and cannot
# silently drift into an unaudited path.


class TierWording:
    """Every tier-gated customer sentence this module composes, in one
    place — mirrors `session.WORDING`'s reasoning (see that class's
    docstring) for why a scattered f-string per call site is the wrong
    shape: `audit.py` has to be able to name "every commerce sentence" as
    one thing to check, and a class of `staticmethod`s is what makes that
    possible without a second, hand-maintained list of call sites."""

    @staticmethod
    def strong(subject: str) -> str:
        return f"Wearers consistently describe it as {subject}."

    @staticmethod
    def moderate(subject: str) -> str:
        return f"Several wearers describe it as {subject}."

    @staticmethod
    def limited(subject: str) -> str:
        return f"There is some evidence that it is {subject}."

    @staticmethod
    def single_source(subject: str) -> str:
        # Deliberately the same register `Reason.phrase()` already uses for
        # a lone remark ("one commenter said X") — SINGLE_SOURCE keeps its
        # existing wording rather than earning a new template, per spec
        # §23: "rarely a headline reason," not a promoted claim.
        return f"One commenter said it is {subject}."

    # --- performance axis (longevity/projection/development/
    # reformulation) — spec §31: "describe it as X" reads backwards for a
    # sentiment axis ("describe it as strong projection" implies the note
    # or vibe *is named* "strong projection", which is not what a
    # performance fact says). "Report" is the verb a performance claim
    # actually earns — people are reporting how the bottle performed for
    # them, not describing what it smells like. -----------------------

    @staticmethod
    def strong_performance(subject: str) -> str:
        return f"Wearers consistently report {subject}."

    @staticmethod
    def moderate_performance(subject: str) -> str:
        return f"Several wearers report {subject}."

    @staticmethod
    def limited_performance(subject: str) -> str:
        return f"There is some evidence of {subject}."

    @staticmethod
    def single_source_performance(subject: str) -> str:
        return f"One commenter reported {subject}."

    # --- occasion — spec §31: neither "describe" nor "report" fits "people
    # wore this to a wedding"; the shopper-relevant claim is "does this
    # suit the occasion I named," so the templates say that directly. ---

    @staticmethod
    def strong_occasion(subject: str) -> str:
        return f"Wearers consistently confirm it for {subject} wear."

    @staticmethod
    def moderate_occasion(subject: str) -> str:
        return f"Several wearers confirm it for {subject} wear."

    @staticmethod
    def limited_occasion(subject: str) -> str:
        return f"There is evidence for {subject} wear."

    @staticmethod
    def single_source_occasion(subject: str) -> str:
        return f"One commenter confirmed it for {subject} wear."

    # --- tradeoffs (L1 "worth knowing") — spec §31/F3: the internal
    # attribution register ("on videos about this bottle rather than
    # naming it") is engineering disclosure, correct in `reasons[]`/
    # `caveats[]`/L2/L3, but not the card's summary voice. A tradeoff gets
    # its own three-step ladder rather than reusing the fit-signal one:
    # `LIMITED` and `SINGLE_SOURCE` collapse into one wording
    # ("there is some evidence of X") on purpose — a caveat is something to
    # weigh, not a claim to calibrate as finely as a reason to buy, so the
    # two weakest tiers say the same cautious thing rather than drawing a
    # distinction a shopper has no use for. ------------------------------

    @staticmethod
    def tradeoff_strong(subject: str) -> str:
        return f"Wearers consistently note {subject}."

    @staticmethod
    def tradeoff_moderate(subject: str) -> str:
        return f"Several wearers note {subject}."

    @staticmethod
    def tradeoff_limited(subject: str) -> str:
        return f"There is some evidence of {subject}."

    @staticmethod
    def mixed(topic: str) -> str:
        """Spec §32's mandated wording for a CONTESTED dimension —
        `topic` is the bare attribute/value name (never a value that
        presupposes which side of the dispute is right; see
        `_topic_for_mixed`), so "Projection opinions are mixed across
        wearers" never becomes "Strong projection opinions are mixed,"
        which would silently take a side in the dispute it is reporting."""
        return f"{topic} opinions are mixed across wearers."

    @staticmethod
    def preference_matched(said: str) -> str:
        """A hard-constraint/composer match with no natural "subject"
        phrase of its own (a note/vibe request the candidate simply has) —
        used when a `FIT_SIGNAL` reason's own text is already the value
        (e.g. `"raspberry"`, `"feminine"`), so no tier verb is needed
        beyond naming what was asked for and confirming it is there."""
        return f"Matches your preference for {said}."

    @staticmethod
    def headline_with_results(
        prioritized: list[str], deprioritized: list[str], anchor: str | None = None
    ) -> str:
        """§1's positive-framing fix, the whole point of this module: what
        used to open "No match below clears the independence bar" now
        opens with what FACET actually did, named plainly, never with
        audit vocabulary a shopper has no reason to know
        ("independence"/"bar"/"threshold"/"gate"/"consensus" — checked by
        `audit._audit_commerce`).

        `anchor` is the bottle the results are built around — a typed "I
        love Delina", or the one the shopper just pressed *Love it* on.
        Before this parameter existed, pressing Love it re-anchored the
        engine and reshuffled the list while the headline went on saying
        "I prioritized sweet and masculine first", word for word as
        before; the loved card vanished (an anchor never recommends
        itself) and nothing on the page said why. A button whose only
        visible effect is that things quietly change teaches people the
        buttons do nothing. Naming the anchor is the whole fix.
        """
        if anchor:
            lead = f"Building on {anchor}"
            if prioritized:
                lead += f" — I kept {_join_and(prioritized)} in mind"
            if deprioritized:
                lead += (
                    f"{',' if prioritized else ' — I'} looked for less "
                    f"{_join_and(deprioritized)}"
                )
            lead += (
                ", and" if prioritized or deprioritized else " —"
            ) + " these are the bottles wearers connect to it."
            return lead
        if not prioritized and not deprioritized:
            return "Your closest matches, based on what the corpus supports."
        lead = "Your closest matches"
        if prioritized:
            lead += f" — I prioritized {_join_and(prioritized)} first"
        if deprioritized:
            joiner = ", then looked for distinctive options with less" if prioritized \
                else " — I looked for options with less"
            lead += f"{joiner} {_join_and(deprioritized)} evidence"
        return lead + "."

    @staticmethod
    def coverage_note(dimensions: list[str], covered: int, total: int) -> str:
        """Why a performance-led request keeps returning the well-known
        bottles — said once, at the top, instead of left for the shopper
        to infer that the product only knows fifteen perfumes.

        Longevity, projection and vibe exist only in wearer reports; no
        retailer publishes them. On this corpus that is 46, 38 and 31 of
        548 bottles. A request leaning on one of them is answered from
        that slice however good the catalogue data is, and the honest
        thing is to say so. `covered` is the largest of the named
        dimensions' counts, so "at most" is true for every one of them.
        """
        what = _join_and(dimensions)
        if len(dimensions) == 1:
            return (
                f"Wearer reports on {what} cover {covered} of {total} bottles so "
                "far, so these lean toward the well-known ones."
            )
        return (
            f"Wearer reports on {what} cover at most {covered} of {total} bottles "
            "each so far, so these lean toward the well-known ones."
        )

    # --- community-coverage chrome (catalog-first spec) — a small,
    # always-present line distinct from any fit signal or tradeoff: "how
    # much wearer evidence stands behind this card at all," independent
    # of whether that evidence happened to match anything asked for.
    # Fixed strings keyed only off `evidence.ConfidenceTier`, audited like
    # every other customer sentence in this class. Exists because, once a
    # card can carry zero community reasons at all (a T2 catalog-only
    # candidate), the L1 surface needs an honest, non-alarming way to say
    # "here is what wearers have said, if anything" without inventing a
    # sentence out of an empty list. ------------------------------------

    @staticmethod
    def community_coverage_moderate() -> str:
        return "Community evidence: moderate"

    @staticmethod
    def community_coverage_limited() -> str:
        # Deliberately the one wording for LIMITED, SINGLE_SOURCE, and
        # UNKNOWN (zero community reasons) alike — the same three-tiers-
        # collapse-to-one-cautious-phrase shape `tradeoff_limited`
        # already uses, and for the same reason: a shopper has no use for
        # the difference between "one remark" and "nothing at all" here,
        # only for "not much to go on yet."
        return "Community insight: limited so far"

    @staticmethod
    def headline_no_results(hard_constraints: list[str]) -> str:
        """Spec §35: only hard-constraint exhaustion is a true empty
        result. Named plainly — what was required, not why the corpus
        failed to impress."""
        if not hard_constraints:
            return "Nothing in the catalogue matches this combination yet."
        return (
            f"Nothing in the catalogue currently meets {_join_and(hard_constraints)}. "
            "Try loosening one of those."
        )


class AttributeType(StrEnum):
    """Which wording table (`TierWording`'s three ladders) a `Reason`'s
    fact belongs under — spec §31: a template table per attribute type,
    not one table stretched over every kind of claim. Dispatched from
    `Reason.attribute` (populated by `recommend._fact_reason`/
    `_score_comparative` from the underlying `AttributeFact.attribute` —
    never guessed from `text`, which `_subject` used to do and which a
    reworded sentence could silently break)."""

    NOTE_OR_VIBE = "note_or_vibe"
    PERFORMANCE = "performance"
    OCCASION = "occasion"


def _attribute_type(reason: Reason) -> AttributeType:
    if reason.attribute in SENTIMENT_AXES:
        return AttributeType.PERFORMANCE
    if reason.attribute == "occasion":
        return AttributeType.OCCASION
    return AttributeType.NOTE_OR_VIBE


_TIER_TEMPLATES: dict[AttributeType, dict[ConfidenceTier, object]] = {
    AttributeType.NOTE_OR_VIBE: {
        ConfidenceTier.STRONG: TierWording.strong,
        ConfidenceTier.MODERATE: TierWording.moderate,
        ConfidenceTier.LIMITED: TierWording.limited,
        ConfidenceTier.SINGLE_SOURCE: TierWording.single_source,
    },
    AttributeType.PERFORMANCE: {
        ConfidenceTier.STRONG: TierWording.strong_performance,
        ConfidenceTier.MODERATE: TierWording.moderate_performance,
        ConfidenceTier.LIMITED: TierWording.limited_performance,
        ConfidenceTier.SINGLE_SOURCE: TierWording.single_source_performance,
    },
    AttributeType.OCCASION: {
        ConfidenceTier.STRONG: TierWording.strong_occasion,
        ConfidenceTier.MODERATE: TierWording.moderate_occasion,
        ConfidenceTier.LIMITED: TierWording.limited_occasion,
        ConfidenceTier.SINGLE_SOURCE: TierWording.single_source_occasion,
    },
}


def _tier_sentence(tier: ConfidenceTier, subject: str, attr_type: AttributeType) -> str:
    table = _TIER_TEMPLATES[attr_type]
    return table.get(tier, table[ConfidenceTier.SINGLE_SOURCE])(subject)


def _reason_tier(reason: Reason) -> ConfidenceTier:
    return tier_of(Support(people=reason.people, creators=reason.creators,
                            videos=reason.videos))


def community_coverage_text(candidate: Recommendation) -> str:
    """The card's community-coverage chrome line — `TierWording.
    community_coverage_*`, graded from the candidate's own overall
    `people`/`creators` (`Recommendation.people`/`.creators`, already the
    max across every reason/caveat `recommend._score` produced — the
    same counts the card's own "N people across M channels" line uses).
    Never reads a single reason's tier: this is a whole-card summary,
    not a per-fact one.

    Two tiers, not `ConfidenceTier`'s four: `Recommendation` carries no
    whole-candidate video count for `STRONG`'s multi-video requirement to
    read (only individual `Reason`s do), and a shopper-facing coverage
    line has no use for `LIMITED`/`SINGLE_SOURCE`/`UNKNOWN` as three
    different sentences — the same "several tiers collapse to one
    cautious phrase" shape `TierWording.tradeoff_limited` already uses.
    `creators >= gate.MIN_SOURCES` (independent corroboration — the same
    bar `ConfidenceTier.MODERATE` reads) is what separates the two.
    """
    if candidate.creators >= MIN_SOURCES and candidate.people >= 2:
        return TierWording.community_coverage_moderate()
    return TierWording.community_coverage_limited()


#: `_fact_reason` (`recommend.py`) writes a sentiment-axis reason's `text`
#: as `"{attribute} {value}"` — "projection strong", "longevity long
#: lasting" — which reads fine as the tail of an engineering sentence
#: ("... 8 people across 4 channels say projection strong") but not as a
#: shopper-facing subject on its own ("Wearers consistently describe it as
#: projection strong" is backwards English). A note/vibe reason's `text`
#: is already just the value ("feminine", "raspberry") and needs no
#: adjustment. Two of the four sentiment axes have self-describing values
#: (`"long lasting"`, `"develops well"`) and only need the attribute-name
#: prefix dropped; `projection`'s values (`"strong"`/`"weak"`) are bare
#: adjectives that read as sentence fragments alone ("Several wearers
#: describe it as strong" — strong *what*?), so that one axis keeps its
#: name, reordered to read as English ("strong projection") rather than
#: `_fact_reason`'s engineering order ("projection strong"). `occasion`
#: has the identical `"{attribute} {value}"` shape ("occasion summer") for
#: the same reason (`_fact_reason`'s attribute is neither `"note"` nor
#: `"vibe"`) and needs the same prefix dropped — the occasion templates
#: (`TierWording.*_occasion`) already supply their own "for ... wear"
#: framing, so the bare value ("summer") is the correct subject, not
#: "occasion summer wear."
def _subject(reason: Reason) -> str:
    for axis in SENTIMENT_AXES:
        prefix = f"{axis} "
        if reason.text.startswith(prefix):
            value = reason.text[len(prefix):]
            return f"{value} {axis}" if axis == "projection" else value
    if reason.attribute == "occasion" and reason.text.startswith("occasion "):
        return reason.text[len("occasion "):]
    return reason.text


def fit_signal_text(reason: Reason) -> str:
    """One `FIT_SIGNAL` reason, worded for a shopper. `declared_overlap`
    and `constraint` reasons already read as a plain statement of fact
    (official notes, or "this is required and present") — restating them
    through a tier template would claim a *community* confidence level
    for a *catalogue* fact, so they keep their own text. Every other kind
    (`prefer`/`concept`/`comparative`) is community evidence and gets the
    tier template for its own attribute type (`_attribute_type` — spec
    §31: performance and occasion facts do not read as "describe it as").

    Never called on a `DISAGREEMENT`-classified reason — `classify`
    routes those out of `FIT_SIGNAL` entirely (spec §32), so there is no
    "consistently"/"several" branch here that could overclaim a contested
    fact; see `build_card` and `mixed_text` for where a contested reason's
    text actually comes from.

    `catalog_fit` (catalog-first spec) is a third case alongside
    `constraint`/`declared_overlap`: its `text` is already a fully-
    composed `catalog_profile.DerivedWording` sentence carrying zero
    people by design (a catalog fact, not a headcount) — routing it
    through `_tier_sentence` would grade an ungraded fact `UNKNOWN` ->
    `SINGLE_SOURCE` and print "One commenter said it is ..." for
    something no commenter said anything about.
    """
    if reason.kind in ("constraint", "declared_overlap"):
        return TierWording.preference_matched(_subject(reason))
    if reason.kind == "catalog_fit":
        return reason.text
    if reason.kind == "comparative":
        # A satisfied comparative's text is already the whole report --
        # "rose mentioned by 1 person here, 6 people across 4 channels for
        # Delina (fewer -- which is why it ranks here)" -- the two counts
        # the ranking actually used, stated so the reader can draw the
        # comparison themselves. The tier template turned it into "One
        # commenter said it is rose mentioned by 1 person here, ...", on a
        # real card, the first time the corpus grew a one-person
        # comparison. Same rule as `Reason.phrase`: composed text is
        # returned as itself.
        return reason.text[:1].upper() + reason.text[1:] + "."
    return _tier_sentence(_reason_tier(reason), _subject(reason), _attribute_type(reason))


def _topic_for_mixed(reason: Reason) -> str:
    """The bare, side-neutral noun `TierWording.mixed` names — never a
    value that already picks a side of the dispute (`_subject`'s
    "strong projection" cannot be it: the whole point of "Projection
    opinions are mixed" is that nobody has settled *whether* it is strong
    or weak). A performance axis is named by its attribute
    (`"projection"`, `"longevity"`) since that is the neutral question;
    everything else is named by its own requested value (`"feminine"`,
    `"cinnamon"`) — there is no bipolar axis to stay neutral between, the
    dispute is over whether the descriptor applies at all."""
    if reason.attribute in SENTIMENT_AXES:
        return reason.attribute.capitalize()
    return _subject(reason).capitalize()


def mixed_text(reason: Reason) -> str:
    """The customer sentence for a `DISAGREEMENT`-classified reason or
    caveat — spec §32's mandated wording, the *only* permissible language
    for a dimension the evidence itself is split on: never "consistently,"
    never a plain match or plain contradiction claim."""
    return TierWording.mixed(_topic_for_mixed(reason))


def tradeoff_text(reason: Reason) -> str:
    """One `TRADEOFF`-classified caveat, worded for a shopper — spec
    §31/F3: the card's summary voice, not `Reason.phrase()`'s engineering
    register (which discloses attribution — "on videos about this bottle
    rather than naming it" — a detail that belongs in `reasons[]`/
    `caveats[]`/L2/L3, not the L1 tradeoff line). Only `STRONG`/
    `MODERATE` get their own wording; `LIMITED` and `SINGLE_SOURCE`
    collapse into one cautious phrase — see `TierWording.tradeoff_limited`
    for why. Never called on a `DISAGREEMENT` item — those use
    `mixed_text` instead, via `build_card`'s dispatch.

    `catalog_avoid` (catalog-first spec) bypasses the tier ladder
    entirely, for the identical reason `fit_signal_text`'s `catalog_fit`
    branch does: its `text` is already the complete, audited
    `DerivedWording` sentence ("Vanilla is among the declared notes."),
    carrying no people/creators for a tier to grade.
    """
    if reason.kind == "catalog_avoid":
        return reason.text
    subject = _subject(reason)
    tier = _reason_tier(reason)
    if tier is ConfidenceTier.STRONG:
        return TierWording.tradeoff_strong(subject)
    if tier is ConfidenceTier.MODERATE:
        return TierWording.tradeoff_moderate(subject)
    return TierWording.tradeoff_limited(subject)


def _nonempty(texts: list[str]) -> list[str]:
    """§36's fix, generalised beyond `Reason.phrase()`: never let a blank
    or whitespace-only string reach a count-bearing list. Filtering here —
    where the list is *built* — means every caller downstream (JSON
    serialization, the UI) sees a list whose length already is its
    rendered content; there is no second place that could re-introduce the
    mismatch by forgetting to check.

    This is the second, structural-output half of the fix — the first
    half is `Reason.renderable`, checked *before* a reason is even asked
    to produce text (`build_card` filters on it). Both exist because
    checking output alone misses the dash-led case
    (`Reason.renderable`'s own docstring); this one stays as a backstop
    for any string built some other way (a `TierWording` call with a
    blank subject would still slip past a `renderable`-only guard if the
    caller forgot it — this catches that too).
    """
    return [t.strip() for t in texts if t and t.strip()]


@dataclass(frozen=True)
class Card:
    """The customer-safe object one candidate renders as. Every string
    here is either a `TierWording`/`Reason.phrase()` sentence (audited) or
    a bare preference value (`matched`/`uncertain`/`contradicted` —
    already-normalized corpus vocabulary, not free text)."""

    fragrance_id: int
    name: str
    result_tier: str
    fit_signals: list[str]
    relevant_tradeoffs: list[str]
    matched: list[str]
    uncertain: list[str]
    contradicted: list[str]
    #: The community-coverage chrome line (catalog-first spec) —
    #: `community_coverage_text`'s output, always present (never empty):
    #: even a T2 catalog-only candidate with zero community reasons gets
    #: an honest "limited so far" rather than no line at all.
    community_coverage: str = ""


def _has_contested_want(candidate: Recommendation) -> bool:
    """Whether any reason directly answering a requested dimension is
    itself `DISAGREEMENT`-classified — material opposition on something
    the shopper asked about, not merely present somewhere on the card.
    Read from `candidate.reasons` only: a caveat's own dispute (a
    comparative "people disagree about this bottle") is a `DISAGREEMENT`
    too, but it never claimed to answer a *requested* dimension in the
    first place, so it does not belong in this specific check — see
    `result_tier`'s F5 rule."""
    return any(
        classify(r, in_caveats=False) is EvidenceClass.DISAGREEMENT
        for r in candidate.reasons
    )


def _has_contradicted_avoid(candidate: Recommendation) -> bool:
    """Whether an explicit `avoid` preference is contradicted by real
    evidence — `kind == "avoid"` caveats are only ever appended by
    `recommend._score`'s Stage 3 (a soft avoid) or the anchor-profile
    fallback, and only when the candidate's own evidence supports the
    avoided value; see `result_tier`'s F5 rule for why this caps the top
    tier rather than only showing up as a tradeoff line.

    `kind == "catalog_avoid"` (`recommend._catalog_signal`) is the
    identical shape on catalog evidence instead of community evidence —
    an avoided note the OFFICIAL list declares, or an avoided family the
    declared profile leans toward — and caps the same way: a candidate
    whose declared list carries the very thing asked to avoid has a real
    problem, whether a comment said so or the brand's own listing did.
    """
    return any(c.kind in ("avoid", "catalog_avoid") for c in candidate.caveats)


def result_tier(candidate: Recommendation) -> str:
    """BEST_OVERALL_FIT / STRONG_PROFILE_FIT / COMMUNITY_FAVORITE /
    WORTH_DISCOVERING / CLOSEST_AVAILABLE — replaces the single-axis
    `STRONG_FIT`/`GOOD_FIT`/
    `PARTIAL_FIT`/`EXPLORATORY_PICK` label (spec §16's predecessor) with
    one built from TWO separately-graded axes, per the catalog-first
    spec: how well the CATALOG (declared notes + the curated family/
    occasion mapping — `kind="catalog_fit"` reasons, never community
    evidence) answers the request, and how well the COMMUNITY (every
    other `FIT_SIGNAL` reason kind) does. The two are never averaged or
    summed into one number — a candidate can be excellent on one axis and
    silent on the other, and the label says which. No `index`/rank
    parameter exists for this function to take, exactly as before:
    "never position-based" stays a fact about the signature, not a
    convention.

    The rule, stated once so a test can pin it precisely:

    - **catalog axis** — `strong` when the candidate carries two-or-more
      `catalog_fit` `FIT_SIGNAL` reasons (a declared-note or family/
      occasion match is a binary catalog fact, never confidence-graded
      by a headcount, so "strong" here means *several independent
      catalog dimensions answered*, the direct analogue of the community
      axis's own two-or-more bar).
    - **community axis** — `strong` under the *identical* bar the old
      `STRONG_FIT` used: two-or-more non-catalog `FIT_SIGNAL` reasons,
      the best of them at least `MODERATE` confidence.
    - **BEST_OVERALL_FIT** — both axes `strong`, and nothing capped (see
      below). Requires real confirmation from both L1 catalog data and
      L2 community evidence at once — the top of the label set for
      exactly the reason the spec's example target bottle (declared
      profile fits, zero comments) is `STRONG_PROFILE_FIT`, not this.
    - **STRONG_PROFILE_FIT** — catalog axis `strong`, community axis not.
      The label a T2 catalog-only candidate with real declared-note fit
      and zero (or thin) community evidence reaches — the exact shape
      the catalog-first spec's failure case exists to make possible: "a
      bottle with zero community claims but strong profile fit MUST be
      able to reach the spray queue."
    - **COMMUNITY_FAVORITE** — community axis `strong`, catalog axis not
      — a candidate real wearer evidence backs strongly, whatever (or
      however little) the declared list itself suggests.
    - **WORTH_DISCOVERING** — everything else that still has at least one
      `FIT_SIGNAL` on some axis (or a wearer link for an anchor request)
      that the caveats do not outnumber: every one-match/thin-evidence
      case that used to be `GOOD_FIT`/`PARTIAL_FIT`. "Good profile
      match, limited data" per the spec's own description.
    - **CLOSEST_AVAILABLE** (2026-09-05) — nothing answered the request
      (semantic proximity only — the old `EXPLORATORY_PICK`), or the
      tradeoffs and disagreements outnumber what did. Split off the
      bottom rung because the catch-all was handing "worth discovering"
      to cards that matched nothing at all; see the comment at the
      bottom of this function.

    **Caps** (unchanged in shape from the §16-era rule, just carried onto
    the new top label): a contradicted explicit avoid
    (`_has_contradicted_avoid` — now also catching `catalog_avoid`
    caveats, a declared-present note or family the request asked to
    avoid) or a `CONTESTED`/mixed want-dimension (`_has_contested_want`)
    pulls a candidate that would otherwise be `BEST_OVERALL_FIT` down one
    step to `STRONG_PROFILE_FIT` — never all the way to the bottom rung.
    When both axes are strong the catalog axis is the one kept
    (a fixed, documented tie-break, not a per-cause judgement of which
    axis the contradiction actually came from): a `STRONG_PROFILE_FIT`
    label never overclaims either way, since a catalog-strong candidate
    really is one whatever the community evidence turns out to say.
    """
    catalog_positive = [
        r for r in candidate.reasons
        if r.kind == "catalog_fit"
        and classify(r, in_caveats=False) is EvidenceClass.FIT_SIGNAL
    ]
    community_positive = [
        r for r in candidate.reasons
        if r.kind != "catalog_fit"
        and classify(r, in_caveats=False) is EvidenceClass.FIT_SIGNAL
    ]
    strong_catalog = len(catalog_positive) >= 2
    strong_community = bool(community_positive) and (
        len(community_positive) >= 2
        and max(_reason_tier(r) for r in community_positive) >= ConfidenceTier.MODERATE
    )
    capped = _has_contradicted_avoid(candidate) or _has_contested_want(candidate)

    if strong_catalog and strong_community:
        return "STRONG_PROFILE_FIT" if capped else "BEST_OVERALL_FIT"
    if strong_catalog:
        return "STRONG_PROFILE_FIT"
    if strong_community:
        return "COMMUNITY_FAVORITE"
    # The bottom rung is "good profile match, limited wearer data" — the
    # spec's own definition — and it was being handed to cards that
    # matched nothing at all. Photographed live: Parfums de Marly Layton
    # labelled WORTH_DISCOVERING for "sandalwood + long lasting + strong"
    # with sandalwood unknown, longevity contradicted, and one commenter
    # behind "strong". A label that means the opposite of what it says is
    # worse than no label. When nothing answered the request, or the
    # tradeoffs and disagreements outnumber what did, the honest word is
    # that this is the closest thing available — not a discovery.
    # A graph link counts as answering the request: for "more like
    # Delina", wearers connecting a bottle to Delina *is* the fit, even
    # though `classify` keeps it out of the L1 bullet list as neutral.
    graph_links = [r for r in candidate.reasons if r.kind == "graph"]
    fit = len(catalog_positive) + len(community_positive) + (1 if graph_links else 0)
    against = sum(
        1 for c in candidate.caveats
        if classify(c, in_caveats=True)
        in (EvidenceClass.TRADEOFF, EvidenceClass.DISAGREEMENT)
    )
    if fit == 0 or against >= fit:
        return "CLOSEST_AVAILABLE"
    return "WORTH_DISCOVERING"


def build_card(
    candidate: Recommendation, preference_status: list[dict] | None = None
) -> Card:
    """The whole customer card for one candidate — fit signals, capped
    tradeoffs, the tier label, and the three preference-name lists a UI
    needs for its own status strip without re-deriving evidence classes a
    second time.

    `preference_status` is the per-`PreferenceItem` matrix
    `api._preference_statuses` already computed (`{"value", "entity_type",
    "match_kind", ...}` rows — see `api._item_status`'s docstring) — the
    *same* cells that decided which fit signals/tradeoffs render, so
    `matched`/`uncertain`/`contradicted` here can never disagree with what
    the card actually shows. When it is not available (the stateless
    `/api/recommend`/`/api/fragrance/{id}` surfaces, which have no
    composer `PreferenceState` at all — `commerce-audit.md`'s "empty
    list" fixture-round finding: `candidate.matched`/`unmatched` are only
    ever populated for *hard* constraints by `recommend._score`, never for
    a matched *soft* preference, which is why a card built from an
    all-soft composer request such as the §1 failure case always showed
    an empty `matched` list before this fix), this falls back to
    `candidate.matched`/`unmatched` plus caveat classification — a
    strictly worse but still honest approximation, never a fabrication.
    `partial_match` (contested) preferences land in `contradicted`: from a
    shopper's perspective a disputed dimension is not a clean match, and
    the underlying mixed-evidence line is already visible in
    `relevant_tradeoffs`.
    """
    fit_reasons = sorted(
        (r for r in candidate.reasons
         if r.renderable and classify(r, in_caveats=False) is EvidenceClass.FIT_SIGNAL),
        key=lambda r: (-_reason_tier(r), -r.people),
    )
    fit_signals = _nonempty([fit_signal_text(r) for r in fit_reasons])[:MAX_FIT_SIGNALS]

    # Tradeoffs draw from two sources: caveats classified TRADEOFF/
    # DISAGREEMENT (as before), *and* reasons `classify` pulled out of
    # FIT_SIGNAL for being materially contested (spec §32/F1 — a want
    # dimension the evidence disputes is not a clean fit signal, but it is
    # exactly the kind of thing "worth knowing" exists to surface).
    tradeoff_items: list[tuple[Reason, EvidenceClass]] = [
        (r, EvidenceClass.DISAGREEMENT) for r in candidate.reasons
        if r.renderable and classify(r, in_caveats=False) is EvidenceClass.DISAGREEMENT
    ]
    for r in candidate.caveats:
        if not r.renderable:
            continue
        cls = classify(r, in_caveats=True)
        if cls in (EvidenceClass.TRADEOFF, EvidenceClass.DISAGREEMENT):
            tradeoff_items.append((r, cls))
    tradeoff_items.sort(
        # Disagreements first: "opinions differ on X" is more actionable
        # for a shopper than a settled tradeoff, and the cap is tight
        # enough (spec: 1-2) that what gets cut matters.
        key=lambda pair: (pair[1] is not EvidenceClass.DISAGREEMENT, -pair[0].people)
    )
    relevant_tradeoffs = _nonempty([
        mixed_text(r) if cls is EvidenceClass.DISAGREEMENT else tradeoff_text(r)
        for r, cls in tradeoff_items
    ])[:MAX_TRADEOFFS]

    if preference_status:
        matched = [
            row.get("value") or row.get("entity_type", "")
            for row in preference_status if row.get("match_kind") == "match"
        ]
        uncertain = [
            row.get("value") or row.get("entity_type", "")
            for row in preference_status if row.get("match_kind") == "unknown"
        ]
        contradicted = [
            row.get("value") or row.get("entity_type", "")
            for row in preference_status
            if row.get("match_kind") in ("contradiction", "partial_match")
        ]
    else:
        matched = list(candidate.matched)
        uncertain = list(candidate.unmatched)
        # A preference this candidate actively works against: an explicit
        # `avoid` the evidence says it has anyway (`kind == "avoid"`), or a
        # genuinely disputed dimension (`DISAGREEMENT`) — the union, not
        # either alone, since a shopper's "what's contradicted" list should
        # not lose one just because the other happened to be empty.
        contradicted = list(dict.fromkeys(
            r.text for r in candidate.caveats
            if r.kind == "avoid" or classify(r, in_caveats=True) is EvidenceClass.DISAGREEMENT
        ))

    return Card(
        fragrance_id=candidate.fragrance_id,
        name=candidate.name,
        result_tier=result_tier(candidate),
        fit_signals=fit_signals,
        relevant_tradeoffs=relevant_tradeoffs,
        matched=matched,
        uncertain=uncertain,
        contradicted=contradicted,
        community_coverage=community_coverage_text(candidate),
    )


# --- the headline (spec §1, §26-27) ---------------------------------------


def _bucket_labels(plan: QueryPlan) -> tuple[list[str], list[str]]:
    """`(prioritized, deprioritized)` bare value phrases from one plan's
    hard constraints and soft preferences — the vocabulary `headline`
    names, built from the plan's own buckets (spec §7: "built from the
    plan's buckets via ONE wording function") rather than a second,
    hand-picked summary of what mattered."""
    prioritized = [c.said or c.value for c in plan.hard if not c.exclude]
    deprioritized = [c.said or c.value for c in plan.hard if c.exclude]
    for pref in plan.soft:
        label = pref.said or pref.value
        if pref.direction in (Direction.LOW, Direction.LESS_THAN_ANCHOR):
            deprioritized.append(label)
        elif pref.direction in (Direction.HIGH, Direction.MORE_THAN_ANCHOR):
            prioritized.append(label)
    return prioritized, deprioritized


#: The two phrases that actually made `recommend._note` unsafe for a
#: customer (`commerce-audit.md` §1/§8): "clears the independence bar"
#: (`recommend.py`'s weak-evidence note) and "consensus" (both that note
#: and its single-person-remark sibling). Deliberately specific phrases,
#: not single words like "bar" or "gate" checked as bare substrings — a
#: substring check that loose would flag genuinely customer-safe engine
#: wording that happens to contain "bar" or "gate" as part of an unrelated
#: word (there is none in this codebase's current `WORDING`/`_note`
#: strings, but a check this loose would be one rename away from a false
#: positive). Every other `_note`/`WORDING` sentence this codebase writes
#: today is already plain, honest, and specific — see
#: `commerce-audit.md`'s audit of every branch of `_note` — so this list
#: names the two, not everything.
_DISCOURAGING_ENGINE_PHRASES = ("independence bar", "consensus")


def _engine_note_is_customer_safe(note: str) -> bool:
    lowered = note.lower()
    return not any(phrase in lowered for phrase in _DISCOURAGING_ENGINE_PHRASES)


def headline(
    plan: QueryPlan,
    results: list[Recommendation],
    engine_note: str = "",
    coverage: tuple[dict[str, int], int] | None = None,
) -> str:
    """The one sentence every customer response leads with.

    `engine_note` (`answer.note`, `recommend._note`'s wording) is passed
    through **verbatim** when it is both non-empty and customer-safe — see
    `_DISCOURAGING_ENGINE_PHRASES`. Most of `_note`'s branches already say
    something specific, honest, and non-discouraging (the anchor-profile
    fallback's "matches below share X's official notes, leaving out its Y
    ones," a hard constraint's "no bottle in the corpus has evidence for
    X") — replacing those with a generic "Your closest matches" would
    throw away real information for no reason, and lose whichever fixture
    (`tests/test_facet_cases.py::TestSideEffectButTooWarm`) depends on it
    verbatim.

    The generic, positively-framed sentence (`TierWording.
    headline_with_results`, spec §1's actual fix) is used only when
    `engine_note` is empty *or* discouraging — the "No match below clears
    the independence bar" / "single person's remark ... not worth trusting
    as consensus" cases audit §1/§8 exist to fix. Those are the two
    branches of `_note` that read as a rejection when `results` plainly
    is not empty, and are the only ones this function overrides.

    A truly empty `results` (spec §35's one genuine no-results case) still
    falls back to naming what was required, never engine vocabulary —
    unaffected by `engine_note`, which for that path is usually itself
    already a plain, specific refusal (a hard constraint's own listing);
    this function does not read it there at all, for the same reason it
    is not trusted as the *positive* headline: consistency, not because
    that branch is known to be unsafe.
    """
    if not results:
        prioritized, _ = _bucket_labels(plan)
        return TierWording.headline_no_results(prioritized)
    if engine_note and _engine_note_is_customer_safe(engine_note):
        lead = engine_note
    else:
        prioritized, deprioritized = _bucket_labels(plan)
        lead = TierWording.headline_with_results(prioritized, deprioritized, plan.anchor)
    note = _coverage_sentence(plan, coverage)
    return f"{lead} {note}" if note else lead


#: Below this share of the catalogue, a community-only dimension is thin
#: enough that the results visibly cluster on the well-discussed bottles
#: and the headline should say why. Longevity (46/548 ≈ 8%) trips it;
#: notes never reach this function at all, since the catalogue answers
#: them.
THIN_COVERAGE_SHARE = 0.25

#: The dimensions the coverage sentence speaks about. Vibes are
#: community-only too (`evidence.COMMUNITY_ONLY_ATTRIBUTES`), but a
#: "masculine" or "fresh" request does not visibly cluster the way a
#: performance one does, and a first draft that included them put the
#: sentence on nearly every result page — at which point it is wallpaper,
#: not information. Performance is where a shopper actually notices.
PERFORMANCE_ATTRIBUTES = frozenset({"longevity", "projection"})


def _coverage_sentence(
    plan: QueryPlan, coverage: tuple[dict[str, int], int] | None
) -> str:
    """The `coverage_note` sentence for this plan, or `""`.

    Fires only for dimensions in `evidence.COMMUNITY_ONLY_ATTRIBUTES` the
    plan actually asks about, and only when their coverage is thin. Notes
    and occasions never trigger it — the catalogue answers those for
    every bottle, so the results do not cluster and nothing needs saying.
    """
    if coverage is None:
        return ""
    counts, total = coverage
    if not total:
        return ""
    asked: list[str] = []
    for item in [*plan.hard, *plan.soft]:
        attribute = item.attribute
        if attribute in PERFORMANCE_ATTRIBUTES and attribute not in asked:
            asked.append(attribute)
    thin = [a for a in asked if counts.get(a, 0) / total < THIN_COVERAGE_SHARE]
    if not thin:
        return ""
    return TierWording.coverage_note(
        thin, max(counts.get(a, 0) for a in thin), total
    )
