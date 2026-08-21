"""Accumulating what one person wants, over a discovery session.

    from fragrance_graph.session import PreferenceState
    state = PreferenceState()
    state.merge_utterance(conn, "I love Delina but the rose is too strong")
    plan, unexpressed = state.to_plan()
    answer = recommend_with_plan(conn, plan)   # see api.py

`plan.py` turns *one sentence* into a `QueryPlan`. `recommend.py` turns
*one plan* into judged results. Neither has any notion of a conversation —
a second sentence, a chip click, a person spraying a bottle and saying
"no, not this one." This module is that accumulator, and nothing more: it
owns no evidence, grades nothing, and chooses no bottle. Every fact this
module ever states about a fragrance still has to come from `recommend()`
— `to_plan()`'s whole job is compiling what has piled up into the one
shape `recommend()` already knows how to judge.

## Why a chip and a sentence converge on the same key

A chip ("sweet", direction MORE) and a sentence about the identical word
are two ways of saying the same thing, and a person switching between
them mid-session should not produce two competing preferences that
silently fight. So both paths — `merge_utterance` for free text,
`merge_preference` for the structured/chip path — resolve a descriptor
word to the exact `(attribute, value)` pair `plan.py`'s own vocabulary
would give it (see `classify_attribute`), and store the result under one
shared key. Whichever call happened most recently wins: a chip for
"feminine" set to LESS, then a sentence containing the unnegated word
"feminine", overrides it back to MORE — verified in
`tests/test_session.py::TestChipAndFreeFormConverge`. This is a real
promise for vocabulary both paths recognise; it is **not** a promise that
every pair of sentences a person might consider "opposites" both parse at
all. "Less sweet" then "actually sweeter is fine" does not exercise it:
`plan.parse` has no rule for the comparative "sweeter" in isolation, so
the second sentence refuses to parse (see `api.py`'s `/say`, which
surfaces that refusal rather than swallowing it) and nothing merges — not
because the override rule failed, but because there was nothing on the
second sentence's side to override with. That is a `plan.py` vocabulary
gap, not a `session.py` bug, and it is out of scope here to widen
`plan.py`'s extraction vocabulary to close it.

## Why some things never make it into the plan

`QueryPlan` has one anchor, no price field, and no "I said no to this
one" list. A session can easily hold more than a `QueryPlan` can carry —
two liked bottles when only one can be the comparison anchor, a budget
nothing in the corpus prices, an occasion word nobody catalogued, a
comparison whose anchor has since changed. Rather than silently keep the
first and drop the rest, or silently recompile against whatever anchor
happens to be current now, `to_plan()` returns a second list,
`unexpressed`: `{"preference": ..., "reason": ...}` for exactly what
could not be compiled and why.

## Every sentence this layer composes itself lives in one place

`Reason.phrase()`/`digest()` are the audited layer for evidence sentences;
this module needs a handful of sentences of its own — "not used: budget",
"was said about X, current anchor is Y" — that are not about evidence at
all, only about what state a `QueryPlan` cannot hold. Scattering those as
f-strings through `api.py`'s handlers would put unaudited prose next to
audited prose with no seam between them, so every one of them is a named
function below (`WORDING`) that `api.audited_session_probe_text` renders
and `audit.py` checks exactly like every other surface — see that
function's docstring for why coverage here specifically closes a gap
`api.audited_probe_text` alone could not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

import psycopg

from fragrance_graph.plan import ATTRIBUTE_WORDS as _ATTRIBUTE_WORDS
from fragrance_graph.plan import CONCEPT_PHRASES as _CONCEPT_PHRASES
from fragrance_graph.plan import OCCASION_WORDS as _OCCASION_WORDS
from fragrance_graph.plan import PERFORMANCE_PHRASES as _PERFORMANCE_PHRASES
from fragrance_graph.plan import VIBE_WORDS as _VIBE_WORDS
from fragrance_graph.plan import Constraint, Intent, Preference, QueryPlan, parse, parse_with_corpus
from fragrance_graph.plan import Direction as PlanDirection
from fragrance_graph.query import find_fragrance

log = logging.getLogger("fragrance_graph.session")


class Direction(StrEnum):
    """The vocabulary a chip or a merged phrase is stored under — distinct
    from `plan.Direction`, which is the vocabulary the parser thinks in.
    `to_plan` is where the two get translated; nowhere else needs to know
    both exist.
    """

    #: "I want more of this."
    MORE = "more"
    #: "I want less of this," said mildly.
    LESS = "less"
    #: "I want less of this," said as a hard no. Compiles identically to
    #: LESS today — see `to_plan`'s docstring for why, and why the
    #: distinction is kept in the state even though the engine cannot
    #: currently act on it differently.
    AVOID = "avoid"
    #: "A candidate without this is not a candidate." Compiles to a hard
    #: `Constraint`, a filter rather than a score.
    REQUIRE = "require"


class Verdict(StrEnum):
    """What a spray earned. Mirrors the wording a person on the floor
    would actually use, not an internal grade."""

    LOVE = "love"
    MAYBE = "maybe"
    NO = "no"


class WORDING:
    """Every sentence `session.py`/`api.py` compose themselves, in one
    place — see the module docstring's "Every sentence this layer
    composes itself" section for why this exists at all. A plain
    namespace of `staticmethod`s rather than a module-level function per
    line: `api.audited_session_probe_text` needs to be able to name "all
    of this module's own wording" as one thing to render, and a class
    with everything on it is easier to iterate/import as a unit than a
    scatter of same-named-pattern module functions.

    Nothing here is a `Reason`, a `Strength`, or a claim about a
    fragrance's evidence — every string is either fixed prose about what
    `to_plan` could not compile, or a caller-supplied value (an occasion
    someone typed) echoed back honestly rather than dropped silently.
    Echoing that value is itself the reason `audit.py`'s "api responses"
    surface has to reach these functions: caller-supplied text landing in
    a response unaudited is exactly the gap a hostile occasion string
    could exploit, and `tests/test_audit.py`'s positive-control test
    proves the audit still catches it here.
    """

    @staticmethod
    def budget_unused() -> str:
        return "no price data yet"

    @staticmethod
    def occasion_unrecognised(occasion: str) -> str:
        return f"{occasion!r} is not a recognised occasion word"

    @staticmethod
    def extra_liked_bottle() -> str:
        return (
            "only the most recently liked bottle drives anchor "
            "comparison in this version"
        )

    @staticmethod
    def comparative_no_anchor() -> str:
        return "no liked bottle is currently set to compare against"

    @staticmethod
    def comparative_stale_anchor(parsed_against: str, current_anchor: str) -> str:
        return f"was said about {parsed_against}, current anchor is {current_anchor}"

    # --- composer (`PreferenceItem`) wording — see `to_plan`'s "composer
    # items" section for where each of these is used. ---------------------

    @staticmethod
    def multiple_anchors_not_ranked() -> str:
        return "multiple anchors not yet ranked"

    @staticmethod
    def want_fragrance_not_supported() -> str:
        return "wanting a specific fragrance is not a supported preference; use like instead"

    # `like_performance_not_supported` lived here through Composer Phase B.
    # Removed: `like`+`performance` now compiles like `want`+`performance`
    # (see `to_plan`'s "Composer items" section) — it was a bucket-priority
    # mistake read as a usability one, not a genuine gap `unexpressed`
    # should still report. See `commerce-audit.md` §5.

    @staticmethod
    def avoid_occasion_not_supported() -> str:
        return "avoiding an occasion is not supported; set the occasion you want instead"

    @staticmethod
    def budget_only_via_want() -> str:
        return "a budget is only recognised in the want bucket"

    @staticmethod
    def fragrance_not_found(name: str) -> str:
        return f"{name!r} does not match any fragrance in the catalogue"


#: `plan.Direction.HIGH`/`LOW` -> this module's `MORE`/`LESS`. There is no
#: entry for `LESS_THAN_ANCHOR`/`MORE_THAN_ANCHOR`: those are handled
#: separately, in `_comparative`, because collapsing them here would throw
#: away the anchor they are relative to. See `merge_utterance`.
_PLAN_TO_STATE = {
    PlanDirection.HIGH: Direction.MORE,
    PlanDirection.LOW: Direction.LESS,
}


def classify_attribute(word: str) -> tuple[str, str]:
    """Which `(plan.py attribute, value)` a bare descriptor word belongs
    to — the chip path's mirror of what `plan.parse` already does for the
    identical word inside a sentence.

    Reuses `plan.py`'s own vocabulary tables rather than inventing a
    second classification that could quietly disagree with the first one:
    if `PERFORMANCE_PHRASES` or `VIBE_WORDS` ever grows a word, a chip for
    that word is reclassified for free, with no edit needed here.

    Order matters and mirrors `plan.parse`'s own reading order — specific
    tables (performance phrases, attribute-name words, vibes, concepts)
    are checked before the open-ended fallback, because a word could in
    principle appear in more than one table and the specific meaning
    should win over "we don't know, so call it a note."

    Falls through to `("note", word)` for anything unrecognised. That is
    the same bucket `plan._read_notes` uses for an ordinary descriptor
    ("raspberry", "rose") once every more specific table has had a look,
    and it is a real, working bucket — `_score`'s hard-constraint and
    soft-preference paths both read `attribute="note"` facts every day.
    """
    lowered = word.strip().lower()
    if lowered in _PERFORMANCE_PHRASES:
        attribute, value, _direction = _PERFORMANCE_PHRASES[lowered]
        return attribute, value
    if lowered in _ATTRIBUTE_WORDS:
        return _ATTRIBUTE_WORDS[lowered], lowered
    if lowered in _VIBE_WORDS:
        return "vibe", lowered
    if lowered in _CONCEPT_PHRASES:
        return "concept", _CONCEPT_PHRASES[lowered]
    return "note", lowered


def _match_occasion(text: str) -> str | None:
    """The known `plan.OCCASION_WORDS` entry `text` names, or None.

    Exact match first ("wedding" -> "wedding"), then substring, so a UI
    that lets someone type a free-form occasion ("a friend's wedding
    this fall") still lands on the word `plan.py` itself would recognise
    inside a sentence — the same word `_read_words` would have pulled out
    of that sentence if it had been typed as free text instead of set via
    a field.
    """
    lowered = text.strip().lower()
    if lowered in _OCCASION_WORDS:
        return lowered
    for word in _OCCASION_WORDS:
        if word in lowered:
            return word
    return None


class Bucket(StrEnum):
    """Which of the composer's three buckets a `PreferenceItem` sits in —
    the product's own vocabulary (see `composer-spec-digest.md`), distinct
    from `Direction` above: a bucket is where the chip was dropped, a
    `Direction` is which way a compiled preference pushes, and the two do
    not map one-to-one (a `want` and a `like` on the same note both push
    the same way; see `PreferenceState.to_plan`'s "composer items"
    section for exactly where they stop being the same)."""

    #: "I like this" — preserve/seek. A fragrance becomes the anchor; a
    #: note/characteristic/vibe becomes something to keep.
    LIKE = "like"
    #: "I don't want this" — reduce it or exclude it outright, `mode`
    #: says which (see `AvoidMode`). A fragrance is excluded from results.
    AVOID = "avoid"
    #: "Get me more of this than a mere like would" — a target, stronger
    #: than LIKE. Occasion and budget are only ever expressed here.
    WANT = "want"


class EntityType(StrEnum):
    """What kind of thing a `PreferenceItem.value` names."""

    FRAGRANCE = "fragrance"
    NOTE = "note"
    #: A characteristic word ("warm", "sweet", "powdery") whose
    #: `plan.py` attribute is ambiguous until looked up — see
    #: `item_plan_attribute`.
    SCENT_CHARACTERISTIC = "scent_characteristic"
    PERFORMANCE = "performance"
    VIBE = "vibe"
    OCCASION = "occasion"
    BUDGET = "budget"


class AvoidMode(StrEnum):
    """How an `avoid` bucket item should push — only meaningful there;
    `PreferenceItem.mode` is `None` for `like`/`want` items."""

    #: A soft push away from the value — compiles to `Direction.LOW`, or
    #: `Direction.LESS_THAN_ANCHOR` when an anchor exists (mirrors
    #: `plan.py`'s own `TOO_X` reading of "too warm").
    REDUCE = "reduce"
    #: A hard filter: a candidate with real evidence *for* the value is
    #: not a candidate at all. Compiles to `Constraint(..., exclude=True)`.
    EXCLUDE = "exclude"


#: Which `AvoidMode` an `avoid` item defaults to when it names none of its
#: own. The spec states two of these explicitly — `note` excludes,
#: `scent_characteristic`/`vibe` reduce — and this generalises the same
#: choice to every other attribute-shaped entity type as REDUCE, the
#: softer of the two mechanisms, since nothing in the spec asks for a
#: stricter default anywhere else. `fragrance` and `budget` are absent on
#: purpose: a fragrance avoid never reaches this (it excludes via the
#: existing feedback/exclusion machinery, not a `Constraint`), and
#: avoiding a budget is not an expressible preference at all — see
#: `to_plan`'s composer-items section.
_AVOID_DEFAULT_MODE: dict[str, AvoidMode] = {
    EntityType.NOTE.value: AvoidMode.EXCLUDE,
    EntityType.SCENT_CHARACTERISTIC.value: AvoidMode.REDUCE,
    EntityType.VIBE.value: AvoidMode.REDUCE,
    EntityType.PERFORMANCE.value: AvoidMode.REDUCE,
    EntityType.OCCASION.value: AvoidMode.REDUCE,
}


def avoid_mode(item: PreferenceItem) -> AvoidMode | None:
    """The effective `AvoidMode` for an `avoid`-bucket item: its own
    `mode` if it named one, else `_AVOID_DEFAULT_MODE`'s entry for its
    `entity_type`, else `None` for an entity type with no default
    (`fragrance`, `budget` — both handled elsewhere in `to_plan`).
    Public so `api.py` can render "reduce" vs "exclude" for
    `interpreted_preferences` using the exact rule `to_plan` compiled by,
    rather than a second guess at the same default table.
    """
    if item.mode is not None:
        return AvoidMode(item.mode)
    return _AVOID_DEFAULT_MODE.get(item.entity_type)


@dataclass(frozen=True)
class PreferenceItem:
    """One chip: a structured preference from the composer, as opposed to
    a sentence (`merge_utterance`) or a bare descriptor+direction
    (`merge_preference`). See `composer-spec-digest.md` for the product
    shape this mirrors.

    `value` is a canonical string — a fragrance's `canonical_name` for
    `entity_type="fragrance"`, the bare descriptor word otherwise.
    Resolving a raw user string (a typed name, a UI's `entity_id`) to
    that canonical form is the caller's job (`api.py`'s `/compose`
    handler); this dataclass only validates the *shape* of what it is
    handed.

    Validation happens in `__post_init__`, which means an invalid item
    can never exist — the M1 poison-event lesson applied at the
    strongest possible point: there is no code path that records an
    event before construction, because construction itself is where
    validation happens. `rebuild` still wraps `PreferenceItem(**payload)`
    in the same `try/except ValueError` every other replay path uses, for
    a row that reached `session_events` some other way (see `rebuild`'s
    own docstring for why that is not a contradiction).
    """

    #: "like" | "avoid" | "want" — see `Bucket`.
    bucket: str
    #: "fragrance" | "note" | "scent_characteristic" | "performance" |
    #: "vibe" | "occasion" | "budget" — see `EntityType`.
    entity_type: str
    value: str = ""
    #: "reduce" | "exclude" | None — see `AvoidMode`. Meaningful only for
    #: `bucket="avoid"`; ignored (never validated against the bucket)
    #: everywhere else, exactly like a UI sending a stray field.
    mode: str | None = None
    #: Budget only: always "<=" today. A field rather than a hard-coded
    #: assumption so a future ">=" ("at least this expensive") is a new
    #: valid value here, not a redesign.
    operator: str | None = None
    #: Budget only: a positive USD amount.
    amount: float | None = None

    def __post_init__(self) -> None:
        bucket = (self.bucket or "").strip().lower()
        entity_type = (self.entity_type or "").strip().lower()
        object.__setattr__(self, "bucket", bucket)
        object.__setattr__(self, "entity_type", entity_type)
        try:
            Bucket(bucket)
        except ValueError:
            raise ValueError(
                f"unknown bucket {self.bucket!r}; must be one of "
                f"{', '.join(b.value for b in Bucket)}"
            ) from None
        try:
            EntityType(entity_type)
        except ValueError:
            raise ValueError(
                f"unknown entity_type {self.entity_type!r}; must be one of "
                f"{', '.join(e.value for e in EntityType)}"
            ) from None
        if self.mode is not None:
            mode = self.mode.strip().lower()
            object.__setattr__(self, "mode", mode)
            try:
                AvoidMode(mode)
            except ValueError:
                raise ValueError(
                    f"unknown mode {self.mode!r}; must be one of "
                    f"{', '.join(m.value for m in AvoidMode)}, or omitted"
                ) from None

        if entity_type == EntityType.BUDGET.value:
            if self.operator != "<=":
                raise ValueError(
                    f"a budget item's operator must be '<=', got {self.operator!r}"
                )
            if self.amount is None or self.amount <= 0:
                raise ValueError(
                    f"a budget item needs a positive amount, got {self.amount!r}"
                )
        else:
            value = (self.value or "").strip()
            object.__setattr__(self, "value", value)
            if not value:
                raise ValueError(f"a {entity_type} preference needs a value")

    def as_payload(self) -> dict:
        """This item's fields, as the exact keyword shape its own
        constructor accepts — `PreferenceItem(**item.as_payload()) ==
        item` — used as the `session_events` payload for
        `"preference_item_added"` and reconstructed the identical way by
        `rebuild`."""
        return {
            "bucket": self.bucket, "entity_type": self.entity_type,
            "value": self.value, "mode": self.mode,
            "operator": self.operator, "amount": self.amount,
        }


def item_plan_attribute(item: PreferenceItem) -> tuple[str, str] | None:
    """The `(plan.py attribute, value)` pair a composer item's `value`
    compiles to, for the entity types that are ordinary evidence
    attributes. Returns `None` for `fragrance` and `budget`, which are
    not evidence-attribute lookups at all — a fragrance names a bottle,
    not a fact about one, and a budget is commerce data `plan.py` has no
    attribute for (see `to_plan`'s composer-items section and
    `recommend.price_floor`).

    `note`, `vibe` and `occasion` are taken literally: the composer
    already states which one it means, so there is nothing to
    disambiguate. `scent_characteristic` and `performance` are
    genuinely ambiguous by design — a shopper choosing "warm" from a
    characteristics picker has not said whether that is a note or a
    vibe — and are resolved by `classify_attribute`, the identical
    vocabulary tables `plan.parse` itself reads for the same word typed
    as a sentence. That is the same convergence promise the module
    docstring makes for the free-text/chip path, extended to the
    composer: a chip for "warm" and a sentence about "warm" land on the
    same `(attribute, value)` key regardless of which of the three
    input paths produced them.

    Public so `api.py` can look candidates' evidence up by the identical
    key `to_plan` compiled the item's `Preference`/`Constraint` under —
    for `preference_status`, a second, independently-derived key would
    risk silently disagreeing with the one that actually decided the
    candidate.
    """
    entity_type = EntityType(item.entity_type)
    if entity_type is EntityType.NOTE:
        return "note", item.value.strip().lower()
    if entity_type is EntityType.VIBE:
        return "vibe", item.value.strip().lower()
    if entity_type is EntityType.OCCASION:
        return "occasion", item.value.strip().lower()
    if entity_type in (EntityType.SCENT_CHARACTERISTIC, EntityType.PERFORMANCE):
        attribute, value = classify_attribute(item.value)
        if (
            entity_type is EntityType.PERFORMANCE
            and attribute not in ("longevity", "projection")
        ):
            # A chip in the PERFORMANCE picker is a performance claim by
            # declaration — the shopper has already answered the question
            # `classify_attribute`'s note-fallback exists for. Found live:
            # the picker offers a bare "strong", the phrase table only
            # knows "strong projection", so the chip compiled to
            # `("note", "strong")` and real PROJECTION evidence could
            # never match it — the shopper's most explicit signal, worth
            # nothing. Retry the word as its own performance phrase
            # before accepting a non-performance reading; the phrase's
            # canonical value ("strong") is kept, and `_matches`' split
            # rule means it reaches both "strong" and "strong projection"
            # facts.
            phrase = _PERFORMANCE_PHRASES.get(f"{value} projection")
            if phrase is not None:
                attribute, value, _direction = phrase
        return attribute, value
    return None


def _resolve_item_fragrance(
    conn: psycopg.Connection | None, value: str
) -> tuple[str, int | None]:
    """`value` (a typed name, or an already-canonical one) resolved
    against the catalogue — mirrors `plan._resolve`'s job for the
    composer's own fragrance-lookup path, using the identical resolver
    (`query.find_fragrance`) rather than a second one that could
    disagree about the same name. `conn=None` (no database — the same
    shape `merge_utterance` accepts for vocabulary-only tests) leaves
    the name unresolved rather than raising, matching `record_feedback`'s
    existing tolerance of an id-less name.
    """
    if conn is None:
        return value, None
    row = find_fragrance(conn, value)
    if row is None:
        return value, None
    return row["canonical_name"], row["id"]


@dataclass
class PreferenceState:
    """Everything accumulated about what one person wants, over one
    discovery session (an associate's iPad, or a self-serve kiosk).

    Public fields are the contract `api.py` reads and serialises directly.
    Fields prefixed `_` are bookkeeping this class needs in order to
    compile a plan later — the current anchor's resolved id, and
    anchor-relative preferences that only mean something paired with the
    anchor that produced them — and are not part of that contract.
    """

    #: Canonical names, in the order first liked. A name can appear here
    #: and later be removed — a LOVE reverses an earlier NO on the same
    #: bottle and vice versa, "later overrides earlier" applied to
    #: contradictions about a single bottle exactly as it is applied to
    #: attributes.
    liked_fragrances: list[str] = field(default_factory=list)
    disliked_fragrances: list[str] = field(default_factory=list)
    #: Keyed `"<plan.py attribute>:<value>"` — e.g. `"note:raspberry"`,
    #: `"vibe:feminine"`, `"projection:strong"`, `"concept:mass appeal"` —
    #: never the bare word a person typed or clicked. See the module
    #: docstring for why a chip and a sentence about the same word have to
    #: land on the same key.
    attribute_prefs: dict[str, Direction] = field(default_factory=dict)
    occasion: str | None = None
    #: Stored, not acted on. `to_plan` always reports this in
    #: `unexpressed` — see its docstring.
    budget_usd: float | None = None
    #: Every free-form utterance, in order, unedited. Not deduplicated:
    #: the count and order are themselves part of the session's history.
    free_text_history: list[str] = field(default_factory=list)
    #: `(fragrance_id, verdict)` in the order given. An id's *current*
    #: verdict is its most recent entry — see `excluded_ids`,
    #: `deprioritized_ids`. Kept as a log rather than a dict because the
    #: log is exactly what `session_events` replays to rebuild this
    #: object; collapsing it early would throw away what replay needs.
    feedback: list[tuple[int, str]] = field(default_factory=list)

    #: Every `PreferenceItem` ever added, in add order, append-only —
    #: never mutated or removed in place. A removal (`remove_item`) marks
    #: an *index* into this list as inactive rather than deleting from
    #: it, so an id a UI is holding (the position it rendered a chip at)
    #: stays valid even after some other chip is removed. See
    #: `active_items`.
    items: list[PreferenceItem] = field(default_factory=list)
    #: Indices into `items` that have been removed. A `set`, not a
    #: filtered copy of `items`, for the same reason `excluded_ids` is
    #: computed from `feedback` rather than tracked separately — one
    #: source of truth, recomputed on read.
    _removed_item_indices: set[int] = field(default_factory=set, repr=False)
    #: index into `items` -> resolved fragrance id, for every item whose
    #: `entity_type` is `"fragrance"`. Resolved once, at `add_item` time,
    #: and cached here rather than re-resolved on every `to_plan()` call —
    #: a name that later stops resolving (a fragrance renamed, a fixture
    #: torn down) must not retroactively un-anchor a session that already
    #: had a real id for it.
    _item_fragrance_ids: dict[int, int | None] = field(default_factory=dict, repr=False)
    #: index into `items` -> the resolved catalogue `canonical_name`, for
    #: every fragrance item that *did* resolve. A shopper may type an
    #: alias ("Delina" for "Parfums de Marly Delina") or a different
    #: case; the plan's anchor, and everything read back to the shopper
    #: (`summary()`, `interpreted_preferences`), must name the bottle the
    #: way the catalogue does, the same discipline `merge_utterance`'s
    #: own anchor resolution already holds to. See `display_value`.
    _item_fragrance_names: dict[int, str] = field(default_factory=dict, repr=False)

    _anchor_id: int | None = field(default=None, repr=False)
    _anchor_name: str | None = field(default=None, repr=False)
    #: name -> resolved id, for every bottle currently in `liked_fragrances`
    #: — not only the anchor. Exists so a NO on the *current anchor* can
    #: fall back to the next most recently liked bottle
    #: (`liked_fragrances[-1]` after removal) with its real id, rather than
    #: only ever being able to clear the anchor outright. See
    #: `record_feedback`.
    _liked_ids: dict[str, int | None] = field(default_factory=dict, repr=False)
    #: Same key convention as `attribute_prefs`, holding only the entries
    #: `merge_utterance` could not fold into it: `(attribute, value,
    #: plan.Direction, anchor_name)` for a preference relative to whatever
    #: anchor was current *at the moment it was parsed* — carried
    #: precisely so `to_plan` can tell a comparative apart from one that
    #: has since drifted onto a different anchor. See `to_plan`.
    _comparative: dict[str, tuple[str, str, PlanDirection, str]] = field(
        default_factory=dict, repr=False
    )

    # --- merging ----------------------------------------------------

    def merge_utterance(
        self, conn: psycopg.Connection | None, text: str
    ) -> QueryPlan:
        """Parse one free-form utterance and merge it into the state.

        Returns the `QueryPlan` `plan.parse`/`parse_with_corpus` produced
        for *this one utterance alone* — useful to a caller that wants to
        show "how this was read" the way `ask.py`'s pages already do.
        The accumulated, compiled plan is a separate thing; call
        `to_plan()` for that.
        """
        self.free_text_history.append(text)
        parsed = parse_with_corpus(conn, text) if conn is not None else parse(None, text)

        # An anchor can surface from three different grammars in
        # `plan.py` — `ANCHOR_PATTERNS` ("I love X", a genuine expression
        # of liking), `PROFILE_PATTERNS` ("what do people say about X")
        # and `COMPARE_PATTERN` ("X vs Y") — and only the first is a
        # positive preference signal; the other two set their own
        # `plan.intent`. So an anchor surviving with `intent is RECOMMEND`
        # can only have come from the like/love/fan-of grammar.
        if parsed.intent is Intent.RECOMMEND and parsed.anchor:
            self._like(parsed.anchor, parsed.anchor_id)

        for constraint in parsed.hard:
            self._set(constraint.attribute, constraint.value, Direction.REQUIRE)
        for preference in parsed.soft:
            if preference.relative_to_anchor:
                # `parsed.anchor` here is *this utterance's own* anchor —
                # the one `_read_complaints` measured the comparative
                # against a moment ago, inside this same `parse()` call —
                # never the session's anchor as it stands after this call
                # returns. Carrying it is what lets `to_plan` tell "still
                # valid" apart from "drifted" later. `parsed.anchor` is
                # never None here: `relative_to_anchor` only becomes true
                # when `_read_complaints` saw an anchor to measure against.
                key = f"{preference.attribute}:{preference.value}"
                self._comparative[key] = (
                    preference.attribute, preference.value, preference.direction,
                    parsed.anchor,
                )
                continue
            state_direction = _PLAN_TO_STATE.get(preference.direction, Direction.LESS)
            self._set(preference.attribute, preference.value, state_direction)
        return parsed

    def merge_preference(self, attribute: str, direction: Direction | str) -> None:
        """The chip path: a bare descriptor word and a direction, with no
        sentence around it. Classified into the identical `(attribute,
        value)` shape `merge_utterance` would produce for the same word
        (`classify_attribute`) and stored under the same key, so a chip
        and a sentence about "sweet" contend for the one slot rather than
        stacking.

        Raises `ValueError` for an unrecognised `direction` — deliberately
        not caught here. `api.py`'s `/prefs` handler must validate before
        it ever writes the event to `session_events` (an unvalidated value
        recorded first would poison every future replay); `rebuild` below
        is the second, independent line of defence for a bad value that
        reaches the log some other way.
        """
        direction = Direction(direction)
        plan_attribute, value = classify_attribute(attribute)
        self._set(plan_attribute, value, direction)

    def set_occasion(self, occasion: str) -> None:
        self.occasion = occasion

    def set_budget(self, budget_usd: float) -> None:
        self.budget_usd = budget_usd

    def record_feedback(
        self, fragrance_id: int, name: str, verdict: Verdict | str
    ) -> None:
        """A spray's verdict. LOVE makes the bottle the new anchor — the
        discovery loop's whole point is that "more like the one I just
        smelled" is a *better* signal than anything typed beforehand. NO
        excludes it (`excluded_ids`), removes it from `liked_fragrances`
        — a bottle just told NO cannot stay "liked", whatever it was
        called before — and, if it was the anchor, falls back to the next
        most recently liked bottle that is not itself disliked, rather
        than merely clearing the anchor while a perfectly good liked
        bottle sits right there in `liked_fragrances`. Only once nothing
        remains liked does the anchor actually go to `None`. MAYBE only
        ever touches `feedback` — see `deprioritized_ids`; there is no
        `QueryPlan` field for "rank this one lower," so that reads
        `feedback` directly rather than going through a plan at all.
        """
        verdict = Verdict(verdict)
        self.feedback.append((fragrance_id, verdict.value))
        if verdict is Verdict.LOVE:
            self._like(name, fragrance_id)
        elif verdict is Verdict.NO:
            was_anchor = self._anchor_name == name
            if name in self.liked_fragrances:
                self.liked_fragrances.remove(name)
            self._liked_ids.pop(name, None)
            if name not in self.disliked_fragrances:
                self.disliked_fragrances.append(name)
            if was_anchor:
                if self.liked_fragrances:
                    fallback = self.liked_fragrances[-1]
                    self._anchor_name = fallback
                    self._anchor_id = self._liked_ids.get(fallback)
                else:
                    self._anchor_name = None
                    self._anchor_id = None

    def _like(self, name: str, fragrance_id: int | None) -> None:
        if name in self.disliked_fragrances:
            self.disliked_fragrances.remove(name)
        if name not in self.liked_fragrances:
            self.liked_fragrances.append(name)
        self._liked_ids[name] = fragrance_id
        self._anchor_name = name
        self._anchor_id = fragrance_id

    def _set(self, attribute: str, value: str, direction: Direction) -> None:
        self.attribute_prefs[f"{attribute}:{value}"] = direction

    # --- composer (`PreferenceItem`) -----------------------------------

    def add_item(self, conn: psycopg.Connection | None, item: PreferenceItem) -> int:
        """Record one composer chip. Returns the index it can later be
        removed by (`remove_item`) — stable across future adds and
        removes because `items` is append-only; see the field's
        docstring.

        Unlike `merge_preference`/`set_occasion`, this does **not**
        mutate `attribute_prefs`/`occasion`/`budget_usd`/the anchor
        fields in place. `to_plan()` reads `active_items` fresh every
        time it compiles instead — see its "composer items" section for
        why: an in-place mutation here would have to be *undone* by
        `remove_item`, precisely the kind of hand-written inverse that
        event-sourcing exists to avoid needing.

        The one exception is fragrance resolution, which happens once,
        here, and is cached in `_item_fragrance_ids` — resolving against
        the catalogue is a database read `to_plan()` cannot afford to
        repeat on every compile, and a name's resolution does not change
        based on which other items are currently active.
        """
        index = len(self.items)
        self.items.append(item)
        if item.entity_type == EntityType.FRAGRANCE.value:
            name, fragrance_id = _resolve_item_fragrance(conn, item.value)
            self._item_fragrance_ids[index] = fragrance_id
            if fragrance_id is not None:
                self._item_fragrance_names[index] = name
        return index

    def display_value(self, index: int, item: PreferenceItem) -> str:
        """The value to compile/show for this item: the resolved
        catalogue `canonical_name` for a fragrance item that resolved
        (falls back to whatever was typed, unresolved, if it did not —
        `to_plan`'s own `plan.unparsed`/`unexpressed` handling is what
        decides what happens to an item in that state; this method only
        answers "what name"), and the item's own `value` for everything
        else, unchanged."""
        if item.entity_type == EntityType.FRAGRANCE.value:
            return self._item_fragrance_names.get(index, item.value)
        return item.value

    def remove_item(self, index: int) -> None:
        """Deactivate the item at `index` (chip removal). An out-of-range
        index is a no-op rather than an error — the same tolerance
        `record_feedback` already gives an unresolved fragrance id — so a
        replayed session that had an item removed after a since-reverted
        schema change does not brick on replay."""
        if 0 <= index < len(self.items):
            self._removed_item_indices.add(index)

    @property
    def active_items(self) -> list[tuple[int, PreferenceItem]]:
        """`(index, item)` for every composer item that has not been
        removed, in add order — what `to_plan()`, `composer_excluded_ids`,
        `effective_budget_usd` and `effective_occasion` all compile from."""
        return [
            (i, item) for i, item in enumerate(self.items)
            if i not in self._removed_item_indices
        ]

    @property
    def composer_excluded_ids(self) -> frozenset[int]:
        """Fragrance ids to exclude purely from active `avoid`+`fragrance`
        composer items — computed fresh from `active_items`, so removing
        such a chip un-excludes the bottle without needing to invert any
        other state (mirrors `excluded_ids`, computed fresh from
        `feedback` for the identical reason).

        Kept deliberately separate from `excluded_ids` (spray-feedback
        NO) rather than routed through `record_feedback`: that would make
        removing a composer chip require appending a compensating
        LOVE/undo event to a log that represents a *different* kind of
        signal (a shopper's verdict on a bottle they smelled), and would
        conflate two things that should stay independently readable. A
        caller that wants "every id that must never appear" reads the
        union of both — see `api._rerank`.
        """
        return frozenset(
            fid
            for i, item in self.active_items
            if item.bucket == Bucket.AVOID.value and item.entity_type == EntityType.FRAGRANCE.value
            and (fid := self._item_fragrance_ids.get(i)) is not None
        )

    def effective_budget_usd(self) -> float | None:
        """The budget that actually governs this session: an explicit
        `/prefs` `budget_usd` always wins when set (matching this
        module's own "whichever call happened most recently" convergence
        rule for the chip/free-text path — see the module docstring);
        otherwise the first active composer `want`+`budget` item's
        amount, or `None` if neither exists.
        """
        if self.budget_usd is not None:
            return self.budget_usd
        for _, item in self.active_items:
            if item.entity_type == EntityType.BUDGET.value and item.bucket == Bucket.WANT.value:
                return item.amount
        return None

    def effective_occasion(self) -> str | None:
        """The occasion that actually governs this session — same
        precedence rule as `effective_budget_usd`: an explicit `/prefs`
        `occasion` always wins; otherwise the first active composer
        occasion item (`want` or `like`; see `to_plan`'s composer-items
        section for why `avoid`+`occasion` is not expressible), or `None`.
        """
        if self.occasion is not None:
            return self.occasion
        for _, item in self.active_items:
            if item.entity_type == EntityType.OCCASION.value and item.bucket in (
                Bucket.WANT.value, Bucket.LIKE.value,
            ):
                return item.value
        return None

    # --- reading ------------------------------------------------------

    @property
    def excluded_ids(self) -> frozenset[int]:
        """Fragrance ids that must never appear in a future result: an
        explicit, *current* NO. Computed fresh from `feedback` rather than
        tracked separately, so a later LOVE or MAYBE on the same id
        un-excludes it — one source of truth, the log itself, the same
        reasoning `to_plan`'s "later overrides earlier" already runs on.
        """
        latest: dict[int, str] = dict(self.feedback)
        return frozenset(fid for fid, v in latest.items() if v == Verdict.NO.value)

    @property
    def deprioritized_ids(self) -> frozenset[int]:
        """Fragrance ids whose current verdict is MAYBE — eligible, but a
        caller ranking results should sink these relative to everything
        else. See `record_feedback`'s docstring for why this is read
        directly from `feedback` rather than carried through a plan."""
        latest: dict[int, str] = dict(self.feedback)
        return frozenset(fid for fid, v in latest.items() if v == Verdict.MAYBE.value)

    def to_plan(self) -> tuple[QueryPlan, list[dict]]:
        """Compile the accumulated state into the same `QueryPlan`
        `recommend()` already knows how to judge, plus `unexpressed`: a
        list of `{"preference": ..., "reason": ...}` for everything the
        state holds that a `QueryPlan` has no field for. `reason` text
        always comes from `WORDING`, never an inline f-string — see the
        module docstring.

        What compiles, and how:

        - **`attribute_prefs`** — `REQUIRE` becomes a hard `Constraint`
          (a filter: no match, not a candidate). `MORE` becomes a soft
          `Preference` with `Direction.HIGH`. `LESS` and `AVOID` both
          become a soft `Preference` with `Direction.LOW` — the engine
          has exactly one soft-avoidance mechanism
          (`recommend._score`'s Stage 3, which costs a candidate rather
          than filtering it), and no hard "must not have" filter exists
          for either to reach instead. The distinction between them is
          kept in the *state* — a UI can still show "avoiding" more
          insistently than "less" — it is simply not one the compiled
          plan, or the engine underneath it, currently has two answers
          for.
        - **`_comparative`** entries compile only if the *current* anchor
          is both set and identical to the anchor they were parsed
          against. Neither is optional:

          - no anchor at all (nothing liked, or the anchor bottle got a
            NO with nothing left to fall back to) -> `unexpressed`, via
            `WORDING.comparative_no_anchor`.
          - an anchor is set, but it is not the one this comparative was
            measured against (a new LOVE moved the anchor on; a stale
            entry from before F3's NO-fallback logic ran) ->
            `unexpressed`, via `WORDING.comparative_stale_anchor`, naming
            both the anchor it was said about and the one currently
            live. Compiling it against the new anchor instead would
            silently turn "less rose than Delina" into "less rose than
            whatever is anchored now" — a comparison nobody drew.
        - **the anchor** — the most recently liked fragrance still not
          disliked (`record_feedback`'s NO-fallback keeps this true even
          after the original anchor is dropped — see its docstring).
          Only one `QueryPlan` anchor exists; if more than one bottle is
          currently liked, the others are named in `unexpressed` via
          `WORDING.extra_liked_bottle`, one entry per bottle, rather than
          silently dropped.
        - **`occasion`** — compiled to a soft `Preference("occasion",
          ...)` if it names a word `plan.OCCASION_WORDS` recognises
          (`_match_occasion`); otherwise `unexpressed`, via
          `WORDING.occasion_unrecognised`, so a hand-typed occasion
          nothing in `plan.py`'s vocabulary covers is reported rather
          than quietly discarded. This is also what "occasion when no
          occasion intent exists" means in this module's own docstring:
          `QueryPlan` has no dedicated occasion field at all, only this
          one soft preference shape, so an occasion that cannot become
          one has nothing else to become.
        - **`budget_usd`**, or a composer `want`+`budget` item when
          `budget_usd` itself is unset (`effective_budget_usd`) — always
          reported here via `WORDING.budget_unused`, because `to_plan`
          has no visibility into whether real price data exists for
          *this* query's candidates; that is only known once
          `recommend_plan` has actually run. `api._recommendations_for`
          is what removes this entry after the fact when
          `Answer.priced_candidates` comes back true — see
          `recommend_plan`'s "Why budget is a parameter" docstring
          section. Reported every time it is set, not only the first
          time, so a caller cannot mistake silence after the first report
          for the budget having started mattering.

        ## Composer items (`self.items`, via `active_items`)

        Every active `PreferenceItem` compiles independently of the
        free-text/chip machinery above — the two coexist in one
        `QueryPlan` rather than one overriding the other, per the module
        docstring's "chips and utterances coexist in one state." The
        mapping, exhaustively:

        - **`like` + `fragrance`** — the *first* active such item becomes
          the anchor, but only if no free-text/feedback anchor
          (`_anchor_name`) is already set; a free-text LOVE always wins,
          matching that path's own "most recent wins" rule applied to a
          slot the composer is contesting from outside it. Every
          *further* active `like`+`fragrance` item — whether it lost to a
          free-text anchor or to an earlier composer one — is named in
          `unexpressed` via `WORDING.multiple_anchors_not_ranked`:
          ranking against more than one anchor at once is a later phase
          (see `composer-spec-digest.md`'s "Multiple anchors" section);
          this phase holds every one of them rather than dropping any.
          A name that does not resolve to a catalogue id is added to
          `plan.unparsed` instead (never to the anchor slot) so the
          vector-retrieval path (`recommend._semantic_candidates`) still
          gets a chance at it, the same treatment `plan._read_intent_and_
          anchor` already gives an unresolved free-text anchor name.
        - **`avoid` + `fragrance`** — excluded via `composer_excluded_ids`
          (`api._rerank` unions it with spray-feedback `excluded_ids`),
          never through `record_feedback`: see that property's docstring
          for why the two exclusion sources are kept independent. A name
          that does not resolve is reported in `unexpressed` via
          `WORDING.fragrance_not_found` — nothing to exclude by id, and
          silently ignoring a stated avoid would be exactly the kind of
          drop this module's invariant forbids.
        - **`like`/`want` + (`note`|`vibe`|`scent_characteristic`|
          `performance`)** — a soft `Preference` at `Direction.HIGH`,
          keyed by `item_plan_attribute(item)`. `want` and `like` compile
          *identically* here — both mean "push toward this" — the
          distinction the spec draws between them ("WANT is stronger
          than LIKE") is not a different `Direction`, it is which
          `PreferenceItem.bucket` a later "matched" credit counts under
          (see the ranking tie-break in `api._tiebreak_by_composer_
          matches`), because `plan.Preference` has no field to carry a
          bucket and inventing one would be a second, parallel encoding
          of exactly what `self.items` already records. `like` +
          `performance` used to be refused as "not expressible"
          (`WORDING.like_performance_not_supported`, now removed) — that
          was a bucket-*priority* rule (LIKE is a softer positive, per the
          spec's own bucket table) mistaken for a usability gap, not a
          genuine structural limit; see `commerce-audit.md` §5. It now
          compiles exactly like every other `like`/`want` attribute pair
          above.
        - **`avoid` + (`note`|`vibe`|`scent_characteristic`|`performance`
          |`occasion`)** — `avoid_mode(item)` decides the mechanism:
          `AvoidMode.EXCLUDE` compiles to a hard
          `Constraint(..., exclude=True)` (see `plan.Constraint.exclude`
          and `recommend._score`'s Stage 1); `AvoidMode.REDUCE` compiles
          to a soft `Preference` at `Direction.LESS_THAN_ANCHOR` when an
          anchor is set, else `Direction.LOW` — the exact mirror of how
          `plan._read_complaints` reads "too warm" (`TOO_X`). The default
          mode per entity type is `_AVOID_DEFAULT_MODE`; a `note` defaults
          to exclude, everything else defaults to reduce, both
          overridable by the item's own `mode`.
        - **`want` + `occasion`**, or **`like` + `occasion`** — folds into
          `effective_occasion()` exactly like a `/prefs` `occasion` field
          would, and compiles through the *same* code path immediately
          below (`_match_occasion`) — one occasion mechanism, two ways to
          set it. **`avoid` + `occasion`** is not expressible (there is
          only one occasion slot, so "avoid" and "want" cannot coexist on
          it) and is reported via `WORDING.avoid_occasion_not_supported`.
        - **`want` + `budget`**, or **`avoid`/`like` + `budget`** — only
          `want` is expressible; folds into `effective_budget_usd()`
          exactly like a `/prefs` `budget_usd` field. `avoid`/`like` +
          `budget` is reported via `WORDING.budget_only_via_want`.
        - **`want` + `fragrance`** — not expressible ("I want a specific
          bottle" is not a preference over the corpus's evidence);
          reported via `WORDING.want_fragrance_not_supported`.
        """
        plan = QueryPlan(
            text=self.free_text_history[-1] if self.free_text_history else "",
            anchor=self._anchor_name,
            anchor_id=self._anchor_id,
        )
        unexpressed: list[dict] = []

        for key, direction in self.attribute_prefs.items():
            attribute, _, value = key.partition(":")
            if direction is Direction.REQUIRE:
                plan.hard.append(Constraint(attribute, value, said=value))
            elif direction is Direction.MORE:
                plan.soft.append(
                    Preference(attribute, value, PlanDirection.HIGH, said=value)
                )
            else:  # LESS or AVOID — see the docstring above.
                plan.soft.append(
                    Preference(attribute, value, PlanDirection.LOW, said=value)
                )

        for attribute, value, plan_direction, parsed_against in self._comparative.values():
            preference_label = f"{value} relative to {parsed_against}"
            if plan.anchor is None:
                unexpressed.append({
                    "preference": preference_label,
                    "reason": WORDING.comparative_no_anchor(),
                })
                continue
            if plan.anchor != parsed_against:
                unexpressed.append({
                    "preference": preference_label,
                    "reason": WORDING.comparative_stale_anchor(parsed_against, plan.anchor),
                })
                continue
            plan.soft.append(Preference(attribute, value, plan_direction, said=value))

        if len(self.liked_fragrances) > 1:
            for name in self.liked_fragrances:
                if name != plan.anchor:
                    unexpressed.append({
                        "preference": f"liked bottle: {name}",
                        "reason": WORDING.extra_liked_bottle(),
                    })

        # --- composer items — see the docstring's "Composer items"
        # section above for the full mapping this loop implements.
        #
        # Two passes, not one, and the split matters: every `fragrance`
        # item is resolved first so the anchor is fully settled — exactly
        # the order `plan.parse` itself already enforces
        # (`_read_intent_and_anchor` runs before `_read_complaints`) —
        # before any `avoid`+`mode=reduce` item below asks "is there an
        # anchor" to decide between `LESS_THAN_ANCHOR` and `LOW`. A
        # single pass in item-add order would make that answer depend on
        # whether a shopper happened to tap "avoid warm" before or after
        # "like Side Effect", which is not a distinction the product
        # means to draw.
        composer_anchor_taken = plan.anchor is not None
        for index, item in self.active_items:
            if item.entity_type != EntityType.FRAGRANCE.value:
                continue
            fragrance_id = self._item_fragrance_ids.get(index)
            name = self.display_value(index, item)
            if item.bucket == Bucket.LIKE.value:
                if fragrance_id is None:
                    if name not in plan.unparsed:
                        plan.unparsed.append(name)
                    continue
                if not composer_anchor_taken:
                    plan.anchor, plan.anchor_id = name, fragrance_id
                    composer_anchor_taken = True
                else:
                    unexpressed.append({
                        "preference": f"liked bottle: {name}",
                        "reason": WORDING.multiple_anchors_not_ranked(),
                    })
            elif item.bucket == Bucket.AVOID.value:
                if fragrance_id is None:
                    unexpressed.append({
                        "preference": f"avoid: {name}",
                        "reason": WORDING.fragrance_not_found(name),
                    })
                # else: handled by `composer_excluded_ids`.
            else:  # want + fragrance
                unexpressed.append({
                    "preference": f"want: {name}",
                    "reason": WORDING.want_fragrance_not_supported(),
                })

        for _index, item in self.active_items:
            entity_type = item.entity_type
            bucket = item.bucket
            if entity_type == EntityType.FRAGRANCE.value:
                continue  # already handled above

            if entity_type == EntityType.BUDGET.value:
                if bucket != Bucket.WANT.value:
                    unexpressed.append({
                        "preference": f"{bucket}: budget",
                        "reason": WORDING.budget_only_via_want(),
                    })
                continue  # want+budget folds into effective_budget_usd()

            if entity_type == EntityType.OCCASION.value:
                if bucket == Bucket.AVOID.value:
                    unexpressed.append({
                        "preference": f"avoid: {item.value}",
                        "reason": WORDING.avoid_occasion_not_supported(),
                    })
                continue  # want/like + occasion folds into effective_occasion()

            attribute, value = item_plan_attribute(item)  # type: ignore[misc]

            if bucket == Bucket.LIKE.value:
                # `like`+`performance` used to be refused into
                # `unexpressed` (`WORDING.like_performance_not_supported`)
                # — a bucket-priority mistake read as a usability one (see
                # `commerce-audit.md` §5): the spec's own bucket table
                # makes LIKE a *softer positive*, never "not used." It now
                # compiles identically to `want`+`performance` two lines
                # below — the bucket distinction is not lost, it simply
                # never lived in `Direction` to begin with (see this
                # method's own "Composer items" docstring section for
                # `want`/`like` already compiling identically for note/
                # vibe/characteristic); it is read back downstream by
                # `api._tiebreak_by_composer_matches`, which already keys
                # off `item.bucket`, not off whether the item compiled.
                plan.soft.append(Preference(attribute, value, PlanDirection.HIGH, said=item.value))
            elif bucket == Bucket.WANT.value:
                plan.soft.append(Preference(attribute, value, PlanDirection.HIGH, said=item.value))
            else:  # avoid
                mode = avoid_mode(item)
                if mode is AvoidMode.EXCLUDE:
                    plan.hard.append(Constraint(attribute, value, said=item.value, exclude=True))
                else:  # AvoidMode.REDUCE
                    direction = (
                        PlanDirection.LESS_THAN_ANCHOR if plan.anchor
                        else PlanDirection.LOW
                    )
                    plan.soft.append(Preference(attribute, value, direction, said=item.value))

        effective_occasion = self.effective_occasion()
        if effective_occasion:
            matched = _match_occasion(effective_occasion)
            if matched:
                plan.soft.append(
                    Preference("occasion", matched, PlanDirection.HIGH, said=effective_occasion)
                )
            else:
                unexpressed.append({
                    "preference": "occasion",
                    "reason": WORDING.occasion_unrecognised(effective_occasion),
                })

        if self.effective_budget_usd() is not None:
            unexpressed.append({
                "preference": "budget",
                "reason": WORDING.budget_unused(),
            })

        return plan, unexpressed

    def summary(self) -> dict:
        """The public fields, as plain JSON-safe data — what `api.py`
        sends back for "current state." `_anchor_id`/`_anchor_name`/
        `_comparative`/`_item_fragrance_ids`/`_removed_item_indices` are
        deliberately absent: they are compilation bookkeeping, not
        something a caller reads.

        `items` lists only *active* items (`active_items`) — a removed
        chip is gone from a UI's point of view, even though its index is
        still reserved and cannot be reused by a later add. `index` is
        exactly the value `DELETE /api/session/{id}/compose/{index}`
        expects, so a client never has to compute it separately.
        """
        return {
            "liked_fragrances": list(self.liked_fragrances),
            "disliked_fragrances": list(self.disliked_fragrances),
            "attribute_prefs": {k: v.value for k, v in self.attribute_prefs.items()},
            "occasion": self.occasion,
            "budget_usd": self.budget_usd,
            "free_text_history": list(self.free_text_history),
            "feedback": [
                {"fragrance_id": fid, "verdict": v} for fid, v in self.feedback
            ],
            "items": [
                {
                    "index": index, "bucket": item.bucket,
                    "entity_type": item.entity_type,
                    "value": self.display_value(index, item),
                    "mode": item.mode, "operator": item.operator,
                    "amount": item.amount,
                }
                for index, item in self.active_items
            ],
        }


def rebuild(
    events: list[tuple[str, dict]], conn: psycopg.Connection | None = None
) -> PreferenceState:
    """The state event-sourcing produces: replay `session_events` rows, in
    order, against a fresh `PreferenceState`.

    This is the only path that ever constructs a state from history rather
    than mutating one in place — `api.py` calls it on every request that
    reads or changes a session, so the database rows *are* the truth and
    there is no second, driftable copy of "current state" held anywhere
    between requests.

    `events` is `(kind, payload)` pairs in write order — exactly
    `session_events.kind, session_events.payload`, oldest first. Five
    kinds:

    - `"say"` — `{"text": ...}`, replayed through `merge_utterance`.
    - `"prefs"` — any combination of `{"attribute": ..., "direction":
      ...}`, `{"occasion": ...}`, `{"budget_usd": ...}` in one payload,
      each replayed through `merge_preference` / `set_occasion` /
      `set_budget` respectively — independent checks, not
      mutually-exclusive branches, so a body that set more than one shape
      at once (`api.py`'s `/prefs`) replays all of them.
    - `"feedback"` — `{"fragrance_id": ..., "name": ..., "verdict": ...}`,
      replayed through `record_feedback`.
    - `"preference_item_added"` — exactly `PreferenceItem.as_payload()`'s
      shape, replayed through `PreferenceItem(**payload)` then
      `add_item`. Reconstructing the item is what actually validates it
      on replay (`PreferenceItem.__post_init__`) — the same "validate
      before/at the point of use" discipline `merge_preference`/
      `record_feedback` already give their own inputs, applied to a
      dataclass rather than an enum conversion.
    - `"preference_item_removed"` — `{"index": ...}`, replayed through
      `remove_item`.

    An unrecognised `kind`, a `"prefs"` payload matching none of the three
    shapes, or an event whose values reconstruct into something invalid
    (`ValueError` — an invalid `Direction`/`Verdict` string, or an invalid
    `PreferenceItem`) is skipped, with a warning logged, rather than
    raising. `/prefs` and `/compose` both validate before they ever write
    an event (see `api.py`), so a bad value reaching this function at all
    means it got into `session_events` some other way — a manual insert, a
    future direct-DB write, a value that was valid when written and
    stopped being valid after a `Direction`/`Verdict`/`Bucket`/
    `EntityType` member was renamed. Either way, the append-only log is
    supposed to be history, and history must never be able to brick the
    present: one row this function cannot make sense of should cost that
    row, not the session.
    """
    state = PreferenceState()
    for kind, payload in events:
        try:
            if kind == "say":
                state.merge_utterance(conn, payload["text"])
            elif kind == "prefs":
                # Independent `if`s, not `elif` — a single "prefs" event
                # may carry more than one shape at once (F11: `api.py`'s
                # `/prefs` now merges every field a body provides into
                # one event), and replay has to apply all of them, not
                # stop at the first match.
                if "attribute" in payload and "direction" in payload:
                    state.merge_preference(payload["attribute"], payload["direction"])
                if "occasion" in payload:
                    state.set_occasion(payload["occasion"])
                if "budget_usd" in payload:
                    state.set_budget(payload["budget_usd"])
            elif kind == "feedback":
                state.record_feedback(
                    payload["fragrance_id"], payload["name"], payload["verdict"]
                )
            elif kind == "preference_item_added":
                state.add_item(conn, PreferenceItem(**payload))
            elif kind == "preference_item_removed":
                state.remove_item(payload["index"])
        except ValueError as exc:
            log.warning(
                "skipping unreplayable session event kind=%r payload=%r: %s",
                kind, payload, exc,
            )
    return state
