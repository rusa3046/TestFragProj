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
        - **`budget_usd`** — always `unexpressed`, via
          `WORDING.budget_unused`. Nothing in `plan.py` or
          `recommend.py` has a notion of price; `data/corpus/` records
          what people said about how a fragrance smells and performs, not
          what it costs. Reported every time it is set, not only the
          first time, so a caller cannot mistake silence after the first
          report for the budget having started mattering.
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

        if self.occasion:
            matched = _match_occasion(self.occasion)
            if matched:
                plan.soft.append(
                    Preference("occasion", matched, PlanDirection.HIGH, said=self.occasion)
                )
            else:
                unexpressed.append({
                    "preference": "occasion",
                    "reason": WORDING.occasion_unrecognised(self.occasion),
                })

        if self.budget_usd is not None:
            unexpressed.append({
                "preference": "budget",
                "reason": WORDING.budget_unused(),
            })

        return plan, unexpressed

    def summary(self) -> dict:
        """The public fields, as plain JSON-safe data — what `api.py`
        sends back for "current state." `_anchor_id`/`_anchor_name`/
        `_comparative` are deliberately absent: they are compilation
        bookkeeping, not something a caller reads."""
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
    `session_events.kind, session_events.payload`, oldest first. Three
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

    An unrecognised `kind`, a `"prefs"` payload matching none of the three
    shapes, or an event whose values `merge_preference`/`record_feedback`
    reject (`ValueError` — an invalid `Direction` or `Verdict` string) is
    skipped, with a warning logged, rather than raising. `/prefs` validates
    before it ever writes an event (see `api.py`), so a bad value reaching
    this function at all means it got into `session_events` some other
    way — a manual insert, a future direct-DB write, a value that was
    valid when written and stopped being valid after a `Direction`/
    `Verdict` member was renamed. Either way, the append-only log is
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
        except ValueError as exc:
            log.warning(
                "skipping unreplayable session event kind=%r payload=%r: %s",
                kind, payload, exc,
            )
    return state
