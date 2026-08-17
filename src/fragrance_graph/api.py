"""FACET's service layer: the existing engine, reachable over HTTP.

    uv pip install -e '.[api]'
    uv run uvicorn fragrance_graph.api:app --reload

No UI lives here — that is the next task. This module answers requests the
way `ask.py` answers a curated question and `pages.py` answers a typed
query box entry: by calling `recommend()` (or `recommend_plan()`, for a
compiled `session.PreferenceState`) and rendering exactly what came back.
Nothing here grades a fragrance, chooses a bottle, or writes a sentence
about evidence that `Reason.phrase()`/`digest()` did not already write —
see `_reason_json` and `_candidate_json`, the only two places a
`Recommendation` becomes JSON, and neither composes anything beyond
picking a `label` from fields `recommend()` already computed. `label`
itself is documented in `_label`'s docstring — the one piece of wording
choice this file does make, and it is a category, never a number: no
percentage anywhere in this module, on purpose.

## `note`, `unexpressed`, and where each sentence is allowed to come from

A session response carries three separately-sourced kinds of text, and
they do not mix:

- **`note`** — the engine's own wording, verbatim, and *only* that.
  Either `recommend_plan`'s own note (`recommend._note`'s wording), or —
  for the one utterance that produced this response, when it did not
  parse (`/say` only) — that utterance's own `plan.parse` refusal, which
  is equally engine wording, just attributed to a different call. Never
  composed here, never has anything glued onto it.
- **`unexpressed`** — `state.to_plan()`'s own list of
  `{"preference": ..., "reason": ...}`. `reason` text comes from
  `session.WORDING`, never an inline f-string in this file — see
  `session.py`'s module docstring for why, and `audited_session_probe_text`
  below for how the audit reaches it.
- **`state`** — the accumulated `PreferenceState`, unmodified, so a
  caller can always see exactly what was actually stored, independent of
  either of the above.

A truly empty session (nothing said, nothing set, nothing sprayed) gets
`note=""`, not an invented "nothing said yet" sentence — that used to
live here, and it was itself unaudited, API-composed prose sitting in the
one field that is supposed to be engine-only. An empty note with empty
results already says everything true; a future UI's own copy for "say
something to get started" belongs in the UI, not this layer.

## `label` is a confidence category, not a rank

`results`/`queue` are already ordered by `recommend_plan`'s own
stage-based score — the single ranking this codebase computes. `label`
(see `_label`) answers a different question: how well-evidenced is *this*
one candidate, considered on its own. The two do not have to agree, and
forcing them to would mean quietly re-deriving a second ranking out of
`label` and trusting it over the engine's — exactly the kind of second
scoring path this whole layer exists to avoid. A `STRONG_MATCH` ranking
below several `WORTH_TRYING`/`ALTERNATIVE_DIRECTION` entries is expected,
not a bug: the engine may have ranked those higher on graph or semantic
proximity, which `label` does not weigh at all. A UI renders `label` as a
badge and list order as order.

## Why sessions are event-sourced

A session is not a row this module updates in place. Every `/say`,
`/prefs` and `/feedback` call appends one `session_events` row and
nothing else; `_load_state` always rebuilds a `PreferenceState` from that
session's full event log via `session.rebuild` before answering. See
migration 0015 and `session.py`'s module docstring for why: the log is
the truth, and "current state" is a view computed from it on every read,
not a second copy that can drift from it. Because the log is the truth,
`/prefs` validates and normalizes *before* an event is ever written
(`Direction`/`Verdict` alike) — a bad value recorded first would poison
every future replay of that session, not merely this one request.

## No PII, no cookies

A session is a UUID and nothing else. It carries no name, no email, no
device identifier — an associate hands a shopper an iPad, or a kiosk sits
in a store, and the session exists only as long as the conversation does.
Nothing in this module sets a cookie or reads one.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.evidence import Attribution, attribute_facts
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.notes import declared_note_map
from fragrance_graph.plan import parse_with_corpus
from fragrance_graph.recommend import (
    Answer,
    Reason,
    Recommendation,
    _fact_reason,
    digest,
    fact_matches,
    price_floor,
    recommend,
    recommend_plan,
)
from fragrance_graph.resolve.entities import load_candidates
from fragrance_graph.resolve.names import EXACT_ONLY, best_match, similarity
from fragrance_graph.session import (
    AvoidMode,
    Direction,
    PreferenceItem,
    PreferenceState,
    Verdict,
    avoid_mode,
    item_plan_attribute,
    rebuild,
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Migrate on startup, against the real database only — `tests/
    test_api.py` never enters this app as a context manager (plain
    `TestClient(app)`, not `with TestClient(app) as client:`), which is
    what keeps the test suite from ever running this against
    `FRAGRANCE_DB_URL` at all."""
    conn = get_connection(DEFAULT_DB_URL)
    try:
        migrate(conn)
    finally:
        conn.close()
    yield


app = FastAPI(title="FACET discovery service", lifespan=_lifespan)

# The kiosk. One static file, no build step: `uvicorn fragrance_graph.api:app`
# is the entire deployment. The page composes chrome only — every sentence
# about evidence arrives from this API pre-worded by the audited layer and
# is rendered as text, never markup. See facet/static/index.html's header.
_FACET_STATIC = Path(__file__).parent / "facet" / "static"


@app.get("/", include_in_schema=False)
def facet_kiosk() -> FileResponse:
    return FileResponse(_FACET_STATIC / "index.html", media_type="text/html")

#: A fuzzy suggestion is offered as a clickable "did you mean", never
#: auto-navigated to — the cost of a bad suggestion is a shopper seeing
#: one irrelevant name once. That is a materially smaller risk than
#: `resolve.names.FUZZY_THRESHOLD` (0.88) is conservative against: an
#: automatic merge silently corrupting the comparison graph. A search box
#: is allowed to guess more loosely than a resolver that writes an edge.
SUGGESTION_THRESHOLD = 0.6

#: Reason kinds that come from a candidate actually satisfying something
#: the plan asked for (a hard constraint, a soft preference, a concept, an
#: anchor-relative comparison) — as opposed to `"graph"`/`"semantic"`/
#: `"absence"`, which surface a candidate through proximity or retrieval
#: rather than a stated match. See `_label`.
_POSITIVE_MATCH_KINDS = frozenset({"constraint", "prefer", "concept", "comparative"})


# --- wiring ------------------------------------------------------------


def get_conn() -> Iterator[psycopg.Connection]:
    """One connection per request. Tests override this with
    `app.dependency_overrides[get_conn]` to hand in the shared `conn`
    fixture instead of opening a real one — see `tests/test_api.py`."""
    conn = get_connection(DEFAULT_DB_URL)
    try:
        yield conn
    finally:
        conn.close()


#: `Annotated[..., Depends(...)]` rather than a `= Depends(get_conn)`
#: default on every endpoint — functionally identical, but the default-arg
#: spelling calls `Depends(...)` anew at every one of nine call sites
#: (`ruff`'s B008: a function call as a mutable default), where this
#: spells the wiring once.
Conn = Annotated[psycopg.Connection, Depends(get_conn)]


# --- request bodies ------------------------------------------------------


class CreateSessionRequest(BaseModel):
    mode: Literal["associate", "self_serve", "unknown"] = "unknown"


class SayRequest(BaseModel):
    text: str


class PrefsRequest(BaseModel):
    """Three shapes share this one endpoint — a chip (`attribute` +
    `direction`), an occasion, or a budget — because all three are ways of
    telling the state something structured, as opposed to `/say`'s free
    text. Any combination of the three may be set in one call and all of
    them are applied — a client that gathered a whole preferences form
    should not lose two thirds of it to whichever shape a handler happens
    to check first (an earlier version did exactly that; see the
    `prefs()` endpoint's docstring). At least one must be present."""

    attribute: str | None = None
    direction: str | None = None
    occasion: str | None = None
    budget_usd: float | None = None


class FeedbackRequest(BaseModel):
    fragrance_id: int
    verdict: str


class RecommendRequest(BaseModel):
    text: str


class ComposePreferenceIn(BaseModel):
    """One composer chip over the wire. `value` names an entity
    directly ("rose", "date night"); `entity_id` is the alternative for
    `entity_type="fragrance"` — a UI that already resolved a name via
    `/api/search` can send the id instead of re-typing the name for this
    endpoint to re-resolve. Exactly one of the two is expected for a
    fragrance item; `value` is required for every other `entity_type`
    (`PreferenceItem.__post_init__` is the actual enforcement — this
    model only shapes the JSON)."""

    bucket: str
    entity_type: str
    value: str | None = None
    entity_id: int | None = None
    mode: str | None = None
    operator: str | None = None
    amount: float | None = None


class ComposeRequest(BaseModel):
    preferences: list[ComposePreferenceIn] = []


# --- rendering: the one place a Reason/Recommendation becomes JSON -------


def _reason_json(reason: Reason) -> dict:
    """`Reason.phrase()`, verbatim, plus the fields the API layer is asked
    to expose alongside it. This function writes no words of its own."""
    return {
        "text": reason.phrase(),
        "strength": reason.strength.name,
        "declarable": reason.declarable,
        "people": reason.people,
        "creators": reason.creators,
    }


def _label(candidate: Recommendation, index: int) -> str:
    """BEST_MATCH / STRONG_MATCH / WORTH_TRYING / ALTERNATIVE_DIRECTION,
    derived only from fields `recommend()` already computed — never a new
    score, never a percentage. Checked in this order, first match wins:

    1. **BEST_MATCH** — `index == 0` (the top-ranked result) *and* it
       carries a reason that is both `declarable` and independently
       clears the same bar a published pair page clears
       (`people >= MIN_COMMENTERS and creators >= MIN_SOURCES` on that one
       reason, not merely on the card as a whole — the same floor
       `gate.py` sets everywhere else in this codebase a page asserts
       something as settled).
    2. **STRONG_MATCH** — has at least one `declarable` reason anywhere on
       the card, at any rank. Real support exists; it just is not the
       single strongest thing in the whole result set.
    3. **ALTERNATIVE_DIRECTION** — none of the candidate's `reasons` came
       from directly satisfying something the plan asked for (kind in
       `{"constraint", "prefer", "concept", "comparative"}`). Its
       presence in the results is driven entirely by comparison-graph
       proximity (`kind="graph"`), vector retrieval (`kind="semantic"`),
       or an absence note (`kind="absence"`) — a genuinely different
       direction to consider, not a match on what was typed or clicked.
    4. **WORTH_TRYING** — everything else: some real, if not independently
       declarable, evidence, reached by actually matching part of the
       request.

    `caveats` never factor into this — a caveat is something to weigh
    against a candidate, not evidence that earned it a place in the
    results, and the label is about how well the candidate answers the
    request, not how clean its record is.

    Confidence, not rank: `index` decides only the BEST_MATCH branch's
    eligibility (only rank 0 may ever earn it), it is never compared
    against label to "fix" an ordering — see the module docstring's
    "`label` is a confidence category, not a rank" section for why a
    STRONG_MATCH legitimately sorting below a WORTH_TRYING is expected
    behaviour, not a defect this function should paper over.
    """
    if index == 0 and any(
        r.declarable and r.people >= MIN_COMMENTERS and r.creators >= MIN_SOURCES
        for r in candidate.reasons
    ):
        return "BEST_MATCH"
    if candidate.declarable_reasons:
        return "STRONG_MATCH"
    if not any(r.kind in _POSITIVE_MATCH_KINDS for r in candidate.reasons):
        return "ALTERNATIVE_DIRECTION"
    return "WORTH_TRYING"


def _candidate_json(
    candidate: Recommendation, index: int, preference_status: list[dict] = (),
) -> dict:
    """`preference_status` defaults to an empty list — every caller
    without a composer `PreferenceState` in scope (the stateless
    `/api/recommend`, `/api/fragrance/{id}`, and the audit probes) has
    nothing to report, honestly, rather than omitting the key. A session
    caller (`_session_response`, `spray_queue`) always passes the real
    list from `_preference_statuses`, computed once per response — see
    that function's docstring for what `matched`/`contradicted`/
    `unknown` mean and how each is decided."""
    return {
        "fragrance_id": candidate.fragrance_id,
        "name": candidate.name,
        "label": _label(candidate, index),
        "digest": digest(candidate),
        "reasons": [_reason_json(r) for r in candidate.reasons],
        "caveats": [_reason_json(r) for r in candidate.caveats],
        "people": candidate.people,
        "creators": candidate.creators,
        "preference_status": list(preference_status),
    }


def _answer_json(answer: Answer) -> dict:
    """An `Answer`, rendered whole. If results are empty the engine's own
    refusal note is returned as `note` — never smoothed over, the same
    honesty `ask.py`'s pages hold to."""
    return {
        "note": answer.note,
        "results": [_candidate_json(c, i) for i, c in enumerate(answer.results)],
    }


def _rerank(answer: Answer, state: PreferenceState) -> Answer:
    """`answer.results`, filtered and reordered by feedback the plan
    itself has no field for — the union of `state.excluded_ids` (an
    explicit spray-verdict NO) and `state.composer_excluded_ids` (an
    active composer `avoid`+`fragrance` chip — see that property's
    docstring for why the two exclusion sources stay independently
    tracked even though they are merged here), plus
    `state.deprioritized_ids` (MAYBE). Nothing here re-scores a
    candidate: excluded ones are dropped outright, deprioritized ones are
    moved after every non-deprioritized one, and the relative order
    `recommend_plan` computed is otherwise left exactly as it was —
    Python's sort is stable, so this only ever *partitions*, never
    re-ranks within a partition.
    """
    excluded = state.excluded_ids | state.composer_excluded_ids
    if not excluded and not state.deprioritized_ids:
        return answer
    kept = [r for r in answer.results if r.fragrance_id not in excluded]
    kept.sort(key=lambda r: r.fragrance_id in state.deprioritized_ids)
    answer.results = kept
    return answer


# --- composer: per-`PreferenceItem` match status, and its tie-break ------


def _item_status(
    item: PreferenceItem,
    candidate: Recommendation,
    facts: list,
    price_floor_by_id: dict[int, float],
) -> str:
    """`"matched"` | `"contradicted"` | `"unknown"` for one active
    `PreferenceItem` against one candidate — evidence-gated, per the
    spec's MISSING DATA RULE: absence of evidence is `"unknown"`, never
    counted as either satisfying or failing the item. `facts` is that
    candidate's own `attribute_facts(...)` rows.

    - **`fragrance`** — `like`: `"matched"` only if the candidate carries
      a reason directly evidencing a connection to the anchor (`graph`,
      `declared_overlap`, `comparative` — the kinds `_score`/
      `_anchor_profile_fallback` only ever attach *because* of the
      anchor), else `"unknown"` (this candidate surfaced through some
      other signal — a note match, a semantic hit — with no stated
      connection to the liked bottle). `avoid`: always `"matched"` for
      any candidate that reaches this function at all, because
      `_rerank`/`composer_excluded_ids` already guarantee the avoided
      bottle itself never does.
    - **`budget`** — `"matched"` if `price_floor_by_id` has a real price
      for this candidate (which, by the time this runs, is a price at or
      under the budget — `_apply_budget` already removed anything over),
      else `"unknown"`. Never `"contradicted"`: an over-budget candidate
      was excluded before this function ever sees it.
    - **`occasion` + `avoid`** — `"unknown"`: not an expressible
      combination this phase (see `to_plan`'s composer-items section),
      so there is nothing to evaluate.
    - everything else (`note`/`vibe`/`scent_characteristic`/
      `performance`/`occasion`, `like`/`want`/`avoid`) — looked up via
      `item_plan_attribute(item)`, the identical `(attribute, value)` key
      `to_plan` compiled this item's `Preference`/`Constraint` under, and
      graded from the matching fact's own `supporting`/`opposing` counts:
      net support (`supporting > opposing`, and not `from_name`) means
      the candidate's evidence *has* the trait; net opposition means the
      evidence says it does *not*. `like`/`want` map "has it" ->
      `"matched"`; `avoid` maps "has it" -> `"contradicted"` (the shopper
      asked to avoid this and the evidence says the candidate has it
      anyway — a caveat, not a win) and "does not have it" ->
      `"matched"` (successfully avoided, on real evidence, not silence).
    """
    if item.entity_type == "fragrance":
        if item.bucket == "like":
            return "matched" if any(
                r.kind in ("graph", "declared_overlap", "comparative")
                for r in candidate.reasons
            ) else "unknown"
        return "matched"  # avoid: this candidate was never the avoided bottle

    if item.entity_type == "budget":
        return "matched" if candidate.fragrance_id in price_floor_by_id else "unknown"

    if item.entity_type == "occasion" and item.bucket == "avoid":
        return "unknown"

    attribute_value = item_plan_attribute(item)
    if attribute_value is None:
        return "unknown"
    attribute, value = attribute_value
    fact = next(
        (f for f in facts if not f.from_name and fact_matches(f, attribute, value)),
        None,
    )
    if fact is None:
        return "unknown"
    net_supported = fact.strength.may_retrieve and fact.supporting.people > fact.opposing.people
    net_opposed = fact.opposing.people > fact.supporting.people
    if item.bucket == "avoid":
        if net_supported:
            return "contradicted"
        if net_opposed:
            return "matched"
        return "unknown"
    # like / want
    if net_supported:
        return "matched"
    if net_opposed:
        return "contradicted"
    return "unknown"


def _preference_statuses(
    conn: psycopg.Connection, state: PreferenceState, candidates: list[Recommendation]
) -> dict[int, list[dict]]:
    """`fragrance_id -> [{bucket, entity_type, value, status}, ...]` for
    every active composer `PreferenceItem`, for every candidate —
    `preference_status` on the API's recommendation payload. Empty when
    the session has no active composer items at all (the free-text/chip
    path this module already served has no per-item status concept, and
    nothing here invents one for it).

    A fresh `attribute_facts` read per candidate, not the facts `_score`
    already computed: `Recommendation` does not carry its own facts
    forward (only the `Reason`s derived from them), and re-deriving
    status from those would mean parsing rendered sentences back into
    evidence rather than reading the evidence itself. At this product's
    scale (`DEFAULT_LIMIT` candidates per response) the extra reads are
    cheap; revisit if this endpoint ever needs to serve at a much larger
    fan-out.
    """
    active = [item for _, item in state.active_items]
    if not active or not candidates:
        return {}
    needs_price = any(item.entity_type == "budget" for item in active)
    prices = (
        price_floor(conn, [c.fragrance_id for c in candidates]) if needs_price else {}
    )
    statuses: dict[int, list[dict]] = {}
    for candidate in candidates:
        facts = attribute_facts(
            conn, fragrance_id=candidate.fragrance_id, attribution=Attribution.PROPOSED
        )
        rows = []
        for item in active:
            status = _item_status(item, candidate, facts, prices)
            rows.append(
                {
                    "bucket": item.bucket,
                    "entity_type": item.entity_type,
                    "value": item.value,
                    # A budget item's value lives in operator/amount, so
                    # its `value` is empty — clients key such rows by
                    # entity_type. `display` carries the human label the
                    # UI shows; empty for items whose value speaks.
                    "display": (
                        f"under ${item.amount:g}"
                        if item.entity_type == "budget" and item.amount
                        else ""
                    ),
                    "status": status,
                }
            )
            # A contradicted avoid must be VISIBLE, not only machine-
            # readable: the first shipped kiosk recommended Khamrah to a
            # shopper avoiding warm with the warm evidence filed under
            # "why it fits"; this layer's predecessor then carried the
            # contradiction in preference_status while the card showed
            # nothing. The caveat is the same audited `_fact_reason`
            # wording the engine uses everywhere — no new sentences —
            # and is only added when no existing caveat already carries
            # that fact.
            if status == "contradicted" and item.bucket == "avoid":
                attribute_value = item_plan_attribute(item)
                if attribute_value is None:
                    continue
                attribute, value = attribute_value
                fact = next(
                    (f for f in facts
                     if not f.from_name and fact_matches(f, attribute, value)),
                    None,
                )
                if fact is not None and not any(
                    value in c.text for c in candidate.caveats
                ):
                    candidate.caveats.append(_fact_reason(fact, "attribute"))
        statuses[candidate.fragrance_id] = rows
    return statuses


def _tiebreak_by_composer_matches(
    results: list[Recommendation],
    statuses: dict[int, list[dict]],
    state: PreferenceState,
) -> list[Recommendation]:
    """Two more ranking stages, appended after every stage
    `recommend._score` already runs — the spec's own staged ranking
    (`composer-spec-digest.md`: "... 4 WANT targets; 5 LIKE rewards ...")
    extended into the composer layer: candidates are reordered by count
    of *matched* `want` items (descending), then count of matched `like`
    items (descending). No coefficients, matching this codebase's whole
    ranking philosophy (`recommend.py`'s own module docstring) — a
    candidate with one more matched WANT always outranks one with fewer,
    regardless of either candidate's underlying evidence score.

    A no-op (returns `results` unchanged) when the session has no
    composer items at all, so the free-text/chip-only path this ranking
    did not exist for is completely unaffected. Python's `sort` is
    stable and the original index is the explicit final tiebreak, so two
    candidates with identical matched-want/like counts keep the relative
    order `recommend_plan`'s own stages already gave them — this is an
    extension of the staged ranking, not a replacement for the stages
    underneath it.
    """
    if not state.items or len(results) < 2:
        return results

    def matched_counts(candidate: Recommendation) -> tuple[int, int]:
        entries = statuses.get(candidate.fragrance_id, [])
        want = sum(1 for e in entries if e["bucket"] == "want" and e["status"] == "matched")
        like = sum(1 for e in entries if e["bucket"] == "like" and e["status"] == "matched")
        return want, like

    order = sorted(
        range(len(results)),
        key=lambda i: (
            -matched_counts(results[i])[0], -matched_counts(results[i])[1], i,
        ),
    )
    return [results[i] for i in order]


def _interpreted_preferences(
    state: PreferenceState, plan, unexpressed: list[dict]
) -> dict:
    """The composer's own read-back of what it understood — plain
    strings, never sentences composed here beyond simple f-string labels
    (`"budget <= $250"`, `"occasion: date night"`), because these are
    labels a chip UI echoes next to its own chips, not audited evidence
    prose. Derived entirely from `plan` (already compiled by `to_plan`)
    and `state.active_items` — nothing here re-derives a mapping
    `to_plan`/`avoid_mode` did not already decide.

    - **`anchors`** — `plan.anchor` (whichever source won it — free text
      or composer) first, then every active composer `like`+`fragrance`
      item's name not already counted, in add order. Bare names.
    - **`preserve`** — active `like` items, non-`fragrance`. Bare values.
    - **`reduce`** / **`exclude`** — active `avoid` items, non-`fragrance`,
      split by `avoid_mode(item)`. Bare values.
    - **`target`** — active `want` items, non-`fragrance`/`budget`/
      `occasion`. Bare values.
    - **`constraints`** — the effective budget (`effective_budget_usd`)
      and effective occasion (`effective_occasion`), if either is set,
      as short labels.
    - **`unexpressed`** — the same list `to_plan`/`_recommendations_for`
      already produced, flattened to one string per entry
      (`"{preference}: {reason}"`) since this dict's contract is "a list
      of plain strings" throughout.
    """
    anchors: list[str] = []
    if plan.anchor:
        anchors.append(plan.anchor)
    for index, item in state.active_items:
        if item.entity_type != "fragrance" or item.bucket != "like":
            continue
        name = state.display_value(index, item)
        if name not in anchors:
            anchors.append(name)

    preserve: list[str] = []
    reduce_: list[str] = []
    exclude: list[str] = []
    target: list[str] = []
    constraints: list[str] = []

    for _, item in state.active_items:
        if item.entity_type in ("fragrance", "budget"):
            continue
        if item.bucket == "like":
            preserve.append(item.value)
        elif item.bucket == "want":
            if item.entity_type == "occasion":
                continue  # folded into effective_occasion() below
            target.append(item.value)
        elif item.bucket == "avoid":
            mode = avoid_mode(item)
            (exclude if mode is AvoidMode.EXCLUDE else reduce_).append(item.value)

    budget_amount = state.effective_budget_usd()
    if budget_amount is not None:
        constraints.append(f"budget <= ${budget_amount:.0f}")
    occasion = state.effective_occasion()
    if occasion:
        constraints.append(f"occasion: {occasion}")

    return {
        "anchors": anchors,
        "preserve": preserve,
        "reduce": reduce_,
        "exclude": exclude,
        "target": target,
        "constraints": constraints,
        "unexpressed": [f"{u['preference']}: {u['reason']}" for u in unexpressed],
    }


# --- session persistence: events in, state replayed out ------------------


def _require_session(conn: psycopg.Connection, session_id: str) -> None:
    """404 for a missing session — and, before ever asking the database,
    404 for a `session_id` that cannot be a session id at all. `id` is a
    `UUID` column; handing Postgres anything else raises `invalid input
    syntax for type uuid` from inside the SELECT, which every session
    endpoint used to let escape as a 500. Parsing it in Python first turns
    "malformed" and "not found" into the identical, correct response."""
    try:
        uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}") from None
    row = conn.execute(
        "SELECT 1 FROM discovery_sessions WHERE id = %s", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No session {session_id!r}")


def _record_event(
    conn: psycopg.Connection, session_id: str, kind: str, payload: dict
) -> None:
    conn.execute(
        "INSERT INTO session_events (session_id, kind, payload) VALUES (%s, %s, %s)",
        (session_id, kind, json.dumps(payload)),
    )
    conn.execute(
        "UPDATE discovery_sessions SET updated_at = now() WHERE id = %s", (session_id,)
    )
    conn.commit()


def _load_state(conn: psycopg.Connection, session_id: str) -> PreferenceState:
    rows = conn.execute(
        "SELECT kind, payload FROM session_events WHERE session_id = %s ORDER BY id",
        (session_id,),
    ).fetchall()
    events = [
        (r["kind"], r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"]))
        for r in rows
    ]
    return rebuild(events, conn)


def _recommendations_for(
    conn: psycopg.Connection, state: PreferenceState
) -> tuple[Answer, list[dict], dict[int, list[dict]], object]:
    """`(answer, unexpressed, statuses, plan)` for the accumulated state —
    `answer.note` is engine wording only, never anything glued onto it;
    `unexpressed` is returned separately for the caller to place in its
    own top-level field; `statuses` is `fragrance_id -> preference_status
    list` from `_preference_statuses`, computed once here so every caller
    (`_session_response`, `spray_queue`) renders the identical data
    rather than recomputing it (and potentially disagreeing, since a
    recompute would re-read `attribute_facts` a second time). `plan` is
    returned too — `_session_response` needs it for
    `_interpreted_preferences`, and computing it a second time there
    would risk it silently disagreeing with the one `unexpressed` (the
    budget-corrected list, below) was actually derived from. See the
    module docstring's "`note`, `unexpressed`, and where each sentence is
    allowed to come from" section.
    """
    plan, unexpressed = state.to_plan()
    budget_usd = state.effective_budget_usd()
    if not plan.usable and not plan.refusal:
        # An empty session ("just created, nothing said yet") has nothing
        # to recommend and nothing to refuse — `recommend_plan` is not
        # called at all, rather than being handed a plan it would refuse
        # the same way `parse` refuses an empty sentence, which would be
        # a strange thing to say about a session nobody has spoken in
        # yet. `note` stays "": see the module docstring for why nothing
        # is invented here to fill it.
        answer = Answer(plan=plan, note="")
    else:
        answer = recommend_plan(
            conn, plan, attribution=Attribution.PROPOSED, budget_usd=budget_usd
        )
    if budget_usd is not None and answer.priced_candidates:
        # `to_plan()` cannot know, at compile time, whether real price
        # data exists for *this* query's candidates — only `recommend_plan`
        # can, once it has run. It did, and it found some, so the "no
        # price data yet" entry `to_plan()` wrote no longer describes what
        # this response actually did. See `recommend_plan`'s "Why budget
        # is a parameter" docstring section and `PreferenceState.to_plan`'s
        # own budget paragraph.
        unexpressed = [u for u in unexpressed if u.get("preference") != "budget"]
    answer = _rerank(answer, state)
    statuses = _preference_statuses(conn, state, answer.results)
    answer.results = _tiebreak_by_composer_matches(answer.results, statuses, state)
    return answer, unexpressed, statuses, plan


def _session_response(conn: psycopg.Connection, state: PreferenceState) -> dict:
    """The one response shape every session endpoint returns (`say`,
    `prefs`, `feedback`, `compose`, `remove_compose_item`, `get_session`).
    `interpreted_preferences` lives here — not only on `compose`/
    `remove_compose_item` as Phase A shipped it — so a client that
    re-reads state via plain `GET /api/session/{id}` (a page refresh, or
    the composer's "Edit preferences" round trip) gets the identical
    read-back a chip add would have, rather than a UI having to cache the
    last `compose` response as its one authority on a fact `GET` itself
    cannot answer. One computation (`_recommendations_for`'s `plan`), one
    field, every caller — the same "second, independently-derived copy
    risks disagreeing" reasoning this module already applies everywhere
    else (see `_preference_statuses`, `_recommendations_for`)."""
    answer, unexpressed, statuses, plan = _recommendations_for(conn, state)
    return {
        "state": state.summary(),
        "note": answer.note,
        "unexpressed": unexpressed,
        "results": [
            _candidate_json(c, i, statuses.get(c.fragrance_id, []))
            for i, c in enumerate(answer.results)
        ],
        "interpreted_preferences": _interpreted_preferences(state, plan, unexpressed),
    }


# --- endpoints -------------------------------------------------------


@app.post("/api/session")
def create_session(conn: Conn, body: CreateSessionRequest | None = None) -> dict:
    body = body or CreateSessionRequest()
    session_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO discovery_sessions (id, mode) VALUES (%s, %s)",
        (session_id, body.mode),
    )
    conn.commit()
    return {"session_id": session_id, "mode": body.mode}


#: C0 control characters and DEL, minus tab/newline/CR — an ordinary
#: shopper sentence may reasonably contain those three; nothing else in
#: this range is printable text a person typed. A literal NUL specifically
#: cannot round-trip through a `jsonb` column at all — Postgres raises
#: `UntranslatableCharacter` mid-INSERT, which surfaced as an HTTP 500 with
#: the connection reset before this existed. Stripped before anything
#: touches storage, not merely before display.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _sanitize_text(text: str) -> str:
    return _CONTROL_CHARS.sub("", text).strip()


@app.post("/api/session/{session_id}/say")
def say(session_id: str, body: SayRequest, conn: Conn) -> dict:
    """Free text, merged into the session. If *this* utterance did not
    parse — `plan.parse` refuses it, e.g. a comparative form it has no
    rule for — that refusal is surfaced as this response's `note` (real
    engine wording, not this file's own), rather than being silently
    absorbed into a session-level answer that carries no sign anything
    unusual happened. The utterance itself is still recorded either way:
    a refused sentence is something the shopper said, not something that
    did not happen.
    """
    _require_session(conn, session_id)
    text = _sanitize_text(body.text)
    if not text:
        raise HTTPException(
            status_code=422, detail="text must not be empty"
        )
    _record_event(conn, session_id, "say", {"text": text})
    state = _load_state(conn, session_id)
    response = _session_response(conn, state)
    parsed = parse_with_corpus(conn, text)
    if parsed.refusal:
        response["note"] = parsed.refusal
    return response


@app.post("/api/session/{session_id}/prefs")
def prefs(session_id: str, body: PrefsRequest, conn: Conn) -> dict:
    """Chip (`attribute` + `direction`), occasion, and budget may all be
    given in one call, and all provided fields are applied — an earlier
    version checked for `attribute`/`direction` first, then `occasion`,
    then `budget_usd`, `elif`-style, so a body naming more than one shape
    silently kept only the first and dropped the rest with no error. That
    was reachable in practice: a client resubmitting a whole preferences
    form (occasion *and* budget in the same request) lost the budget
    every time — confirmed by experiment before this fix, along with the
    matching regression test.

    `direction` is validated and case-normalized *before* the event is
    ever written (matching `/feedback`'s convention) — see the module
    docstring for why recording an unvalidated value first is the one
    mistake this endpoint cannot afford to make.
    """
    _require_session(conn, session_id)
    payload: dict = {}
    if body.attribute is not None or body.direction is not None:
        if body.attribute is None or body.direction is None:
            raise HTTPException(
                status_code=422,
                detail="attribute and direction must both be provided together.",
            )
        normalized_direction = body.direction.strip().lower()
        try:
            Direction(normalized_direction)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=(
                    "direction must be one of "
                    f"{', '.join(d.value for d in Direction)}, got {body.direction!r}"
                ),
            ) from None
        attribute = _sanitize_text(body.attribute)
        if not attribute:
            raise HTTPException(
                status_code=422, detail="attribute must not be empty"
            )
        payload["attribute"] = attribute
        payload["direction"] = normalized_direction
    if body.occasion is not None:
        # The same stripping `/say` applies, for the same reason: a NUL
        # reaching jsonb storage is a 500 at INSERT time. The rollback
        # means no poison lands (verified), but a 500 for a control
        # character is still the wrong answer to a typed preference.
        occasion = _sanitize_text(body.occasion)
        if not occasion:
            raise HTTPException(
                status_code=422, detail="occasion must not be empty"
            )
        payload["occasion"] = occasion
    if body.budget_usd is not None:
        payload["budget_usd"] = body.budget_usd
    if not payload:
        raise HTTPException(
            status_code=422,
            detail="Provide at least one of: attribute+direction, occasion, budget_usd.",
        )
    _record_event(conn, session_id, "prefs", payload)
    state = _load_state(conn, session_id)
    return _session_response(conn, state)


def _resolve_compose_value(
    conn: psycopg.Connection, pref: ComposePreferenceIn, index: int
) -> tuple[str | None, dict | None]:
    """`pref.value`, or `pref.entity_id` resolved to a canonical name for
    a `fragrance` item — `(value, error)`, exactly one non-`None`. A
    fragrance item may name either; every other `entity_type` must use
    `value` (`PreferenceItem.__post_init__` requires it, but the id-only
    path is only meaningful for a fragrance, so the unresolved-id error
    is produced here, with the request's own index, rather than as a
    generic "needs a value" from the dataclass)."""
    if pref.value:
        return pref.value, None
    if pref.entity_type == "fragrance" and pref.entity_id is not None:
        row = conn.execute(
            "SELECT canonical_name FROM fragrances WHERE id = %s", (pref.entity_id,)
        ).fetchone()
        if row is None:
            return None, {"index": index, "error": f"No fragrance {pref.entity_id!r}"}
        return row["canonical_name"], None
    return pref.value, None  # let PreferenceItem's own validation say why


@app.post("/api/session/{session_id}/compose")
def compose(session_id: str, body: ComposeRequest, conn: Conn) -> dict:
    """Add one or more structured preferences (chips) to the session in
    one call. Every item is validated *before* anything is written — a
    single bad item 400s the whole request with per-item errors and
    records nothing, the identical "validate before recording" discipline
    `/prefs` already holds `attribute`+`direction` to (see this module's
    docstring). A partial write here would mean the request body and the
    session's actual state silently disagree about which chips landed.

    Each valid item is recorded as its own `"preference_item_added"`
    event (not one event for the whole batch) so replay/removal can
    address each chip by its own stable index — see
    `PreferenceState.add_item`/`remove_item`.

    Returns the standard session response (`state`, `note`, `unexpressed`,
    `results`, each carrying `preference_status`) plus
    `interpreted_preferences` — see `_interpreted_preferences`'s
    docstring for its shape.

    Chip removal is `DELETE /api/session/{id}/compose/{index}`, not a
    field on this endpoint's body — `index` is exactly the value
    `state.summary()["items"][i]["index"]` already carries, so a client
    never has to invent or track a second id for the same chip.
    """
    _require_session(conn, session_id)
    if not body.preferences:
        raise HTTPException(status_code=422, detail="preferences must not be empty")

    payloads: list[dict] = []
    errors: list[dict] = []
    for i, pref in enumerate(body.preferences):
        value, resolve_error = _resolve_compose_value(conn, pref, i)
        if resolve_error is not None:
            errors.append(resolve_error)
            continue
        try:
            item = PreferenceItem(
                bucket=pref.bucket, entity_type=pref.entity_type,
                value=value or "", mode=pref.mode,
                operator=pref.operator, amount=pref.amount,
            )
        except ValueError as exc:
            errors.append({"index": i, "error": str(exc)})
            continue
        payloads.append(item.as_payload())

    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    for payload in payloads:
        _record_event(conn, session_id, "preference_item_added", payload)

    state = _load_state(conn, session_id)
    # `_session_response` already carries `interpreted_preferences`,
    # derived from the same corrected `unexpressed` this response's
    # top-level field holds — see that function's docstring.
    return _session_response(conn, state)


@app.delete("/api/session/{session_id}/compose/{index}")
def remove_compose_item(session_id: str, index: int, conn: Conn) -> dict:
    """Remove one chip by the stable index `state.summary()["items"][i]
    ["index"]`/`compose`'s own response already carries. Removing an
    already-removed or out-of-range index is a no-op, not a 404 —
    `PreferenceState.remove_item`'s own tolerance — because a client
    racing a double-click against itself should not see an error for
    "already gone", the same reasoning `_require_session` extends to a
    session id.

    Refinement, not a new session: this is exactly the spec's "editing
    the same `PreferenceState`" — see `composer-spec-digest.md`.
    """
    _require_session(conn, session_id)
    _record_event(conn, session_id, "preference_item_removed", {"index": index})
    state = _load_state(conn, session_id)
    return _session_response(conn, state)


@app.post("/api/session/{session_id}/feedback")
def feedback(session_id: str, body: FeedbackRequest, conn: Conn) -> dict:
    _require_session(conn, session_id)
    row = conn.execute(
        "SELECT canonical_name FROM fragrances WHERE id = %s", (body.fragrance_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"No fragrance {body.fragrance_id!r}"
        )
    try:
        verdict = body.verdict.strip().lower()
        Verdict(verdict)
    except ValueError:
        raise HTTPException(
            status_code=422, detail=f"verdict must be LOVE, MAYBE or NO, got {body.verdict!r}"
        ) from None
    _record_event(
        conn, session_id, "feedback",
        {"fragrance_id": body.fragrance_id, "name": row["canonical_name"], "verdict": verdict},
    )
    state = _load_state(conn, session_id)
    return _session_response(conn, state)


@app.get("/api/session/{session_id}")
def get_session(session_id: str, conn: Conn) -> dict:
    _require_session(conn, session_id)
    state = _load_state(conn, session_id)
    return _session_response(conn, state)


@app.get("/api/session/{session_id}/spray-queue")
def spray_queue(session_id: str, conn: Conn) -> dict:
    """Up to 3 candidates, never padded: the best match, plus up to two
    "direction-testing" alternates.

    A candidate's **dominant reason** is its own highest-ranked reason —
    `declarable` first, then `people` — the single strongest thing known
    about it, the same tiebreak the engine's own independence stage
    (`recommend._score`'s Stage 5) already ranks by. Its **dominant
    signature** is `(reason.kind, first word of reason.text)` — a coarse
    but deterministic stand-in for "what this candidate is chiefly known
    for" (`Reason` carries no separate `attribute` field to read
    directly; the first word of its `text` is what `_fact_reason` already
    puts there for exactly a note or vibe value, and is the attribute name
    itself for everything else).

    A direction-testing alternate is the next candidate, in the engine's
    own rank order, whose dominant signature *differs* from the best
    match's. Two are taken if two exist; fewer if fewer do. Nothing is
    invented to fill a third slot — an honest queue of two is a queue of
    two.
    """
    _require_session(conn, session_id)
    state = _load_state(conn, session_id)
    answer, unexpressed, statuses, plan = _recommendations_for(conn, state)
    interpreted = _interpreted_preferences(state, plan, unexpressed)
    results = answer.results
    if not results:
        return {
            "note": answer.note, "queue": [], "unexpressed": unexpressed,
            "interpreted_preferences": interpreted,
        }

    def signature(candidate: Recommendation) -> tuple[str, str] | None:
        if not candidate.reasons:
            return None
        best = max(
            candidate.reasons, key=lambda r: (r.declarable, r.people)
        )
        return (best.kind, best.text.split(" ", 1)[0])

    best_match_candidate = results[0]
    best_signature = signature(best_match_candidate)
    queue = [best_match_candidate]
    seen_signatures = {best_signature}
    for candidate in results[1:]:
        if len(queue) >= 3:
            break
        candidate_signature = signature(candidate)
        if candidate_signature in seen_signatures:
            continue
        seen_signatures.add(candidate_signature)
        queue.append(candidate)

    return {
        "note": answer.note,
        "queue": [
            _candidate_json(c, i, statuses.get(c.fragrance_id, []))
            for i, c in enumerate(queue)
        ],
        # The queue screen is where a shopper acts, so it is exactly where
        # held-but-unusable preferences (budget, a stale comparative) must
        # be visible — flagged found-but-unfixed in the M1 round, required
        # by the kiosk, pinned by tests/test_facet_ui.py.
        "unexpressed": unexpressed,
        # Composer Phase B: the results screen's "Your FACET" summary is a
        # compact echo of the same read-back the composer shows live, not
        # a second, hand-rolled label built from `state.items` a second
        # time — see `_session_response`'s identical reasoning.
        "interpreted_preferences": interpreted,
    }


@app.post("/api/recommend")
def stateless_recommend(body: RecommendRequest, conn: Conn) -> dict:
    """One-shot, no session — a direct wrap of `recommend()`."""
    answer = recommend(conn, body.text)
    return _answer_json(answer)


@app.get("/api/fragrance/{fragrance_id}")
def fragrance_profile(fragrance_id: int, conn: Conn) -> dict:
    """A bottle's profile, through the identical path `ask.py`'s static
    profile pages use — `recommend("what do people say about X?")` — so
    this endpoint cannot say anything a typed query, or the built site,
    could not. One path, one audit; see `ask.profile_pages`."""
    row = conn.execute(
        "SELECT canonical_name FROM fragrances WHERE id = %s", (fragrance_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No fragrance {fragrance_id!r}")
    answer = recommend(conn, f"what do people say about {row['canonical_name']}?")
    body = _answer_json(answer)
    body["official"] = _official_block(conn, fragrance_id)
    return body


def _official_block(conn: psycopg.Connection, fragrance_id: int) -> dict:
    """What official sources DECLARE about this bottle, strictly separate
    from what people say. Data fields, not sentences — nothing here is
    composed wording, so nothing here needs the audit's wording layer;
    what the audit must guarantee instead is the separation itself, which
    `tests/test_notes.py` pins: declared notes never appear inside
    evidence sentences and never move an evidence count.

    Absence is honest: no listing, no declared notes, no prices — the
    fields are empty lists/None, never filled with guesses. The UI renders
    "No official data yet", not a shrug of invented values.
    """
    from fragrance_graph.notes import declared_for

    listings = conn.execute(
        """SELECT l.retailer, l.url, l.image_url, l.scent_family,
                  MIN(v.price_usd) AS price_min, MAX(v.price_usd) AS price_max
           FROM retailer_listings l
           LEFT JOIN retailer_variants v ON v.listing_id = l.id
           WHERE l.fragrance_id = %s
           GROUP BY l.id, l.retailer, l.url, l.image_url, l.scent_family
           ORDER BY l.retailer""",
        (fragrance_id,),
    ).fetchall()
    return {
        "declared_notes": declared_for(conn, fragrance_id),
        "listings": [
            {
                "retailer": r["retailer"],
                "url": r["url"],
                "image_url": r["image_url"],
                "scent_family": r["scent_family"],
                "price_min_usd": float(r["price_min"]) if r["price_min"] is not None else None,
                "price_max_usd": float(r["price_max"]) if r["price_max"] is not None else None,
            }
            for r in listings
        ],
    }


def audited_probe_text(conn: psycopg.Connection) -> str:
    """Every sentence `/api/recommend` would return for `audit.PROBES` —
    the identical queries every other surface in `audit.py` is checked
    against — flattened to one string. This is what
    `audit._audit_rendered_text` scans for `audit.FORBIDDEN` phrasing,
    the same way it scans `answer.render()`, the built site's pages, and
    everything else `audit.RENDERERS` names.

    Reuses `_answer_json`/`_reason_json` rather than re-deriving what
    "the API's rendered text" means in a second place — this is exactly
    the JSON a real client of `/api/recommend` would receive, read back
    out again.

    `audit.py` imports this function lazily and tolerates its absence
    (see `_audit_rendered_text`) precisely so that a core install with no
    `[api]` extra can still run the audit; this module itself always
    needs FastAPI, because it *is* the FastAPI app.
    """
    from fragrance_graph.audit import PROBES

    lines: list[str] = []
    for _surface, query in PROBES:
        rendered = _answer_json(recommend(conn, query))
        lines.append(rendered["note"])
        for candidate in rendered["results"]:
            lines.append(candidate["digest"])
            lines += [r["text"] for r in candidate["reasons"]]
            lines += [r["text"] for r in candidate["caveats"]]
    return "\n".join(lines)


def audited_session_probe_text(conn: psycopg.Connection) -> str:
    """Every sentence a *session* response can carry beyond raw
    `Reason.phrase()`/`digest()` output — `note` (engine passthrough,
    exercised here too for completeness) and every `unexpressed[].reason`
    — rendered from the real `PreferenceState` -> `to_plan()` ->
    `_session_response()` path the session endpoints actually run, not a
    stand-in for it.

    This exists as a *separate* function from `audited_probe_text` rather
    than folded into it, because it closes a real, distinct gap:
    `audited_probe_text` drives only the stateless `/api/recommend` path
    (a bare `recommend(conn, query)` call), and every `session.WORDING`
    sentence — "not used: budget", the stale-anchor comparative message —
    is composed only on the session path. A reviewer confirmed this gap
    by construction: none of those strings can appear in
    `audited_probe_text`'s output, however many `PROBES` it is given.

    Reads real fragrances out of the catalogue by id order rather than
    naming specific ones ("Delina", etc.), so a hostile *name* anywhere
    in the catalogue is reachable from here the same way
    `pages.render_full_index` reaches one by scanning the whole
    `fragrances` table instead of a fixed list — this is exactly the
    mechanism `tests/test_audit.py`'s positive-control test relies on to
    prove the wiring is live, not merely present. On an empty catalogue
    this still runs to completion and returns whatever the empty-state
    response says; it never raises and is never skipped, so
    `checked["api responses"]` counts it regardless of what the corpus
    holds.
    """
    from fragrance_graph.session import PreferenceState

    rows = conn.execute(
        "SELECT canonical_name FROM fragrances ORDER BY id LIMIT 2"
    ).fetchall()

    probe = PreferenceState()
    if rows:
        first = rows[0]["canonical_name"]
        probe.merge_utterance(conn, f"I love {first} but the rose is too strong")
        if len(rows) > 1:
            second = rows[1]["canonical_name"]
            # A second LOVE moves the anchor on — exercises both the
            # extra-liked-bottle (F3) and the stale-anchor-comparative
            # (F2) `unexpressed` reasons in the same pass.
            probe.merge_utterance(conn, f"I love {second}")
    probe.set_occasion("a rooftop birthday")
    probe.set_budget(150.0)

    response = _session_response(conn, probe)
    lines: list[str] = [response["note"]]
    for item in response["unexpressed"]:
        lines.append(item["preference"])
        lines.append(item["reason"])
    for candidate in response["results"]:
        lines.append(candidate["digest"])
        lines += [r["text"] for r in candidate["reasons"]]
        lines += [r["text"] for r in candidate["caveats"]]
    return "\n".join(lines)


@app.get("/api/search")
def search(q: str, conn: Conn) -> dict:
    """The catalogue resolver, over names and curated aliases — exact, or
    confirmable fuzzy suggestions, never auto-fuzzy.

    Exact-or-alias only (`resolve.names.best_match` with its fuzzy branch
    disabled via `threshold=EXACT_ONLY`, the same mechanism
    `best_match_conservative_trailing_brand` already uses to mean "exact
    or nothing"). A hit is returned as `match` — a fact about the
    catalogue, safe to act on directly.

    Anything short of that returns zero or more `suggestions`: every
    candidate whose name or alias scores at or above
    `SUGGESTION_THRESHOLD` against `q` (`resolve.names.similarity`,
    the same edit-similarity the resolver itself is built on), ranked
    highest first, capped at 5, one entry per fragrance. A suggestion is
    a guess to show, not a decision to act on — the site's own
    "did you mean" never auto-navigates on a fuzzy hit, and this endpoint
    holds to the same rule: `match` is never filled from `suggestions`.

    No numeric score in the response — matching this module's own "no
    percentage anywhere in this module" rule, applied here to an
    edit-similarity number rather than an evidence one: `suggestions` is
    already sorted best-first, and a bare float next to a fragrance name
    is exactly the kind of number a UI would be tempted to display as if
    it meant something about the fragrance rather than about string
    shape.
    """
    candidates = load_candidates(conn)
    exact = best_match(q, candidates, threshold=EXACT_ONLY)
    if exact is not None:
        return {
            "query": q,
            "match": {"fragrance_id": exact.fragrance_id, "name": exact.canonical_name},
            "suggestions": [],
        }

    scored: list[tuple[float, int, str]] = []
    for candidate in candidates:
        best_for_candidate = max(
            (similarity(q, name) for name in candidate.all_names), default=0.0
        )
        if best_for_candidate >= SUGGESTION_THRESHOLD:
            scored.append((best_for_candidate, candidate.fragrance_id, candidate.canonical_name))
    scored.sort(key=lambda t: (-t[0], t[1]))

    return {
        "query": q,
        "match": None,
        "suggestions": [
            {"fragrance_id": fid, "name": name}
            for _score, fid, name in scored[:5]
        ],
    }


#: The coverage floor a value has to clear before it is offered as a
#: default chip — spec §28: "default chips only when >= N bottles have
#: usable evidence." A value with two bottles of community support is a
#: value a UI must not lead with; `/api/search`-style discovery is still
#: how a shopper reaches something rarer.
MIN_VOCABULARY_BOTTLES = 3


def _community_bottle_counts(conn: psycopg.Connection) -> dict[str, dict[str, set[int]]]:
    """`attribute -> value -> {fragrance_id, ...}`, from *usable* stated
    community evidence only — `f.strength.may_retrieve` (excludes
    `INSUFFICIENT`) and never `f.from_name` (a note read out of a product
    name is not a bottle anybody described; see `evidence.name_facts`).
    One pass over `attribute_facts`, the same evidence read every other
    surface in this codebase uses, rather than a second, bespoke
    coverage query."""
    grouped: dict[str, dict[str, set[int]]] = {}
    for fact in attribute_facts(conn, attribution=Attribution.STATED):
        if fact.from_name or not fact.strength.may_retrieve:
            continue
        grouped.setdefault(fact.attribute, {}).setdefault(fact.value, set()).add(
            fact.fragrance_id
        )
    return grouped


@app.get("/api/vocabulary")
def vocabulary(conn: Conn) -> dict:
    """Coverage-aware `supported_filter_values` (spec §28): every
    note/vibe/occasion/performance value with usable stated community
    evidence across at least `MIN_VOCABULARY_BOTTLES` bottles, plus the
    declared-note vocabulary (brand/retailer `Notes:` listings — see
    `notes.py`) with the identical bottle-count floor, each entry
    carrying its own bottle count so a UI can show "seen on 6 bottles"
    rather than a bare word list.

    Fragrances are not enumerated here — `/api/search` already covers
    them, and duplicating the whole catalogue behind a coverage floor
    would just be a second, worse version of that endpoint.

    `scent_characteristic` has no vocabulary of its own: it has no
    evidence attribute (see `session.item_plan_attribute`'s docstring) —
    a composer chip labelled "characteristic" is, once resolved, either a
    note or a vibe, and a UI groups this response's `notes`/`vibes`
    under that label using the identical `classify_attribute` mapping
    composer items already go through, rather than this endpoint
    computing a second, parallel grouping.
    """
    grouped = _community_bottle_counts(conn)

    def entries(attribute: str) -> list[dict]:
        return sorted(
            (
                {"value": value, "bottles": len(ids)}
                for value, ids in grouped.get(attribute, {}).items()
                if len(ids) >= MIN_VOCABULARY_BOTTLES
            ),
            key=lambda e: (-e["bottles"], e["value"]),
        )

    declared_counts: dict[str, int] = {}
    for notes in declared_note_map(conn).values():
        for note in notes:
            declared_counts[note] = declared_counts.get(note, 0) + 1
    declared_notes = sorted(
        (
            {"value": value, "bottles": count}
            for value, count in declared_counts.items()
            if count >= MIN_VOCABULARY_BOTTLES
        ),
        key=lambda e: (-e["bottles"], e["value"]),
    )

    return {
        "min_bottles": MIN_VOCABULARY_BOTTLES,
        "notes": entries("note"),
        "vibes": entries("vibe"),
        "occasions": entries("occasion"),
        "performance": entries("longevity") + entries("projection"),
        "declared_notes": declared_notes,
    }
