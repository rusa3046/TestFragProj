"""FACET's product-acceptance suite: six retail-mockup scenarios, each
driven through the real HTTP layer exactly the way `tests/test_api.py`
drives it — the identical `client` fixture wiring (`get_conn` overridden
with this test's own `conn`, `TestClient` never entered as a context
manager so `app.on_event("startup")` never touches the real
`FRAGRANCE_DB_URL`), and fixture corpora built with the same helpers
(`add_fragrance`, `ingest`, `tests.test_recommend.note`) every other test
module in this suite uses.

## What "acceptance" means here

Each class below states one retail scenario in the shopper's own words, as
a docstring, then proves the *honest* thing happens — not the thing the
retail mockup's copy implies, when those two differ. `plan.py`'s grammar
is a real, finite set of rules, not a language model; several of the
sentences below expose gaps between what a shopper would expect to be
understood and what the deterministic parser actually recognises today.
Where that happens the docstring says so explicitly and the assertions
pin the real behaviour, per the project's own instruction to "write to
reality, not to hope."

## The shared honesty rail

Every case below closes with `assert_honest(body)`, which enforces the
three product-wide rules that apply to *every* JSON response regardless
of scenario:

  - no numeric score/match/percent field anywhere in the payload (F9),
    found by a recursive scan rather than trusted from one known field;
  - `note` and every `unexpressed[].reason` carry no phrase from
    `audit.FORBIDDEN` — checked directly against the same list `audit.py`
    itself enforces, not a re-typed copy of it;
  - every `label` a candidate carries is one of the four the product
    actually defines (`api._label`'s docstring).
"""

from __future__ import annotations

import json
import re

import pytest

# Same reasoning as tests/test_api.py: a core-only install has no fastapi,
# these tests then skip rather than kill collection for the whole suite.
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from fragrance_graph.api import app, get_conn  # noqa: E402
from fragrance_graph.audit import FORBIDDEN  # noqa: E402
from fragrance_graph.ingest.store import ingest  # noqa: E402
from fragrance_graph.recommend import recommend_plan  # noqa: E402
from fragrance_graph.resolve.entities import add_fragrance  # noqa: E402
from fragrance_graph.session import PreferenceState  # noqa: E402
from tests.conftest import make_comment  # noqa: E402
from tests.test_recommend import note  # noqa: E402

# --- shared fixture wiring, duplicated from tests/test_api.py verbatim ---
# (that file's own docstring explains why: `get_conn` overridden per test
# with the shared `conn` fixture, `TestClient` never entered as a context
# manager so `app.on_event("startup")` never migrates the real database).


@pytest.fixture
def client(conn):
    def _override():
        yield conn

    app.dependency_overrides[get_conn] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _create(client, mode=None) -> str:
    body = {"mode": mode} if mode else {}
    resp = client.post("/api/session", json=body)
    assert resp.status_code == 200
    return resp.json()["session_id"]


def _claim(conn, i, *, frag, claim_type, value, author, channel="chan_a",
           sentiment="POSITIVE", video=None):
    """One person asserting one attribute of one bottle, with a real
    `sentiment` — the shape `tests/test_evidence.py::say` uses. Needed
    here (rather than `tests.test_recommend.note`, which hardcodes
    sentiment to POSITIVE) so a LONGEVITY/PROJECTION fixture can carry
    genuine opposing evidence, for the CONTESTED-longevity case below.
    """
    body = f"comment {i}: {value or sentiment}"
    video = video or f"v{i}"
    ingest(conn, [make_comment(
        i, body=body, source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": video}),
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
        VALUES (%s, %s, 'FRAGRANCE', 'it', %s, %s, %s, %s, 0.9, %s, 1,
                'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, claim_type, frag, "TAG" if value else "NONE", value,
         sentiment, body),
    )
    conn.commit()


# --- the shared honesty rail, applied by every case below ------------------

_SCORE_LIKE = re.compile(r"score|match|percent", re.I)

#: The only four label values `api._label` may ever produce. Any other
#: string is not a category this product defines and no response may
#: carry it.
_ALLOWED_LABELS = frozenset(
    {"BEST_MATCH", "STRONG_MATCH", "WORTH_TRYING", "ALTERNATIVE_DIRECTION"}
)


def _find_numeric_score_fields(obj, path="$") -> list[str]:
    """Every `(path, key)` where a key name suggests a numeric confidence
    score and the value actually is a plain number — recursively, so a
    field like this could be added at any depth without any one
    hand-picked assertion ever looking at that corner of the payload.
    `bool` is deliberately excluded: it is a `int` subclass in Python, and
    a JSON `true`/`false` under an unrelated key is not the leak F9 is
    about.
    """
    found: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if (
                _SCORE_LIKE.search(key)
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            ):
                found.append(f"{path}.{key} = {value!r}")
            found += _find_numeric_score_fields(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found += _find_numeric_score_fields(item, f"{path}[{i}]")
    return found


def assert_no_numeric_score(body: dict) -> None:
    leaks = _find_numeric_score_fields(body)
    assert not leaks, f"numeric score-like field(s) leaked into the response: {leaks}"


def assert_no_forbidden_phrasing(body: dict) -> None:
    """`note` and every `unexpressed[].reason` — the two places this
    layer or `session.WORDING` composes its own sentences — checked
    directly against `audit.FORBIDDEN`, the same list `audit.py` itself
    enforces on every other surface."""
    lowered_note = body.get("note", "").lower()
    for phrase in FORBIDDEN:
        assert phrase not in lowered_note, (
            f"note carries a forbidden phrase {phrase!r}: {body.get('note')!r}"
        )
    for item in body.get("unexpressed", []):
        lowered_reason = item.get("reason", "").lower()
        for phrase in FORBIDDEN:
            assert phrase not in lowered_reason, (
                f"unexpressed reason carries a forbidden phrase {phrase!r}: {item!r}"
            )


def assert_labels_are_valid(body: dict) -> None:
    for candidate in body.get("results", []) + body.get("queue", []):
        assert candidate["label"] in _ALLOWED_LABELS, (
            f"unrecognised label {candidate['label']!r} on {candidate.get('name')!r}"
        )


def assert_honest(body: dict) -> None:
    """The whole-suite discipline every case below holds every response
    to. See the module docstring's "the shared honesty rail" section."""
    assert_no_numeric_score(body)
    assert_no_forbidden_phrasing(body)
    assert_labels_are_valid(body)


# --- Case 1 ------------------------------------------------------------


class TestSomethingLikeDelinaLessRoseMoreFresh:
    """Retail mockup: "I want something like Delina but less rose and
    more fresh" — a shopper naming a bottle they already like, then
    asking to steer away from one of its notes and toward a vibe.

    Reality check against `plan.py`'s actual grammar (confirmed against
    `tests/test_plan.py::TestAComplaintIsNotARequest`, which is the
    file's own record of this rule): only the "the rose is too strong"
    phrasing compiles to a `Direction.LESS_THAN_ANCHOR` *comparative*,
    scored against the anchor's own evidence
    (`recommend._score_comparative`). "less rose" is instead caught by
    `NEGATIVE_PATTERNS`'s bare `\\bless ` trigger, which compiles to a
    plain avoid (`Direction.LOW`) soft preference on `note=rose` — not a
    comparison to Delina's own rose evidence, because `_read_complaints`
    (the only function that ever emits a comparative) only fires on the
    "X is too Y" shape. It still genuinely compiles, though: the anchor
    resolves, "less rose" becomes a real avoid preference, and "more
    fresh" becomes a real vibe preference — nothing here is refused or
    silently dropped, which is the honest sense in which this sentence
    "compiles" as of today.
    """

    def _seeded(self, conn):
        delina = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        for i, author in enumerate(["p1", "p2"]):
            note(conn, i, frag=delina, value="rose", author=author, channel=f"d{i}")
        fresh_bottle = add_fragrance(conn, "Fresh Citrus Bottle")
        for i, author in enumerate(["q1", "q2", "q3"]):
            note(conn, 10 + i, frag=fresh_bottle, value="fresh", author=author,
                 channel=f"f{i}", claim_type="AESTHETIC")
        return delina, fresh_bottle

    def test_the_anchor_resolves_and_is_excluded_from_its_own_results(
        self, client, conn
    ):
        delina, fresh_bottle = self._seeded(conn)
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/say",
            json={"text": "I want something like Delina but less rose and more fresh"},
        )
        body = resp.json()
        assert body["state"]["liked_fragrances"] == ["Parfums de Marly Delina"]
        ids = {r["fragrance_id"] for r in body["results"]}
        assert delina not in ids, "the anchor must never recommend itself"
        assert fresh_bottle in ids
        assert_honest(body)

    def test_every_reason_sentence_is_exactly_a_reason_phrase_output(
        self, client, conn
    ):
        """The shape/wording discipline `tests/test_api.py::
        TestFragranceProfile` uses: build the identical `Recommendation`
        the API used, independently, through the real engine, and diff
        the wording verbatim rather than trusting the API rendered its
        own `Reason.phrase()` output correctly."""
        self._seeded(conn)
        text = "I want something like Delina but less rose and more fresh"
        session_id = _create(client)
        resp = client.post(f"/api/session/{session_id}/say", json={"text": text})
        got = resp.json()["results"]

        state = PreferenceState()
        state.merge_utterance(conn, text)
        plan, _unexpressed = state.to_plan()
        expected = recommend_plan(conn, plan)

        assert [r["fragrance_id"] for r in got] == [
            c.fragrance_id for c in expected.results
        ]
        for got_candidate, expected_candidate in zip(got, expected.results, strict=True):
            assert [r["text"] for r in got_candidate["reasons"]] == [
                r.phrase() for r in expected_candidate.reasons
            ]
            assert [r["text"] for r in got_candidate["caveats"]] == [
                r.phrase() for r in expected_candidate.caveats
            ]


# --- Case 2 ------------------------------------------------------------


class TestFeminineExpensiveSmellingNotTooPowderyDateNight:
    """Retail mockup: "I want something feminine and expensive-smelling,
    not too powdery, good for date night."

    Fragment-by-fragment reality, confirmed against the real parser
    (`fragrance_graph.plan.parse`) rather than assumed:

    - "feminine" -> parses. `VIBE_WORDS` is a fixed list, not corpus
      -dependent, so this is always recognised — compiles to
      `attribute_prefs["vibe:feminine"] = "more"`.
    - "expensive-smelling" -> parses. `CONCEPT_PHRASES` matches the
      hyphenated spelling directly (`_read_concepts` retries with the
      space replaced by a hyphen) — compiles to
      `attribute_prefs["concept:expensive smelling"] = "more"`.
    - "good for date night" -> parses, *twice over*: `"date night"` and
      the bare `"date"` are both entries in `plan.OCCASION_WORDS`, and
      `_read_words` has no "longest match wins" rule, so both land in
      the plan — `attribute_prefs["occasion:date night"]` **and**
      `attribute_prefs["occasion:date"]`, both `"more"`.
    - "not too powdery" -> does **not** parse, and this is the real gap
      this test exists to catch rather than assume away. `powdery` is a
      corpus-learned note word (`plan.note_vocabulary`,
      `MIN_NOTE_CLAIMS = 2`); this fixture deliberately seeds no
      "powdery" claims at all, which is the honest state a real launch
      corpus can be in for any given descriptor. When a word is outside
      the recognised vocabulary entirely, `plan.py` records it nowhere:
      not `hard`, not `soft`, not `unparsed` (that list exists only for
      an anchor name `ANCHOR_PATTERNS` matched but could not resolve —
      see `_read_intent_and_anchor`). `session.py`'s `unexpressed`
      machinery cannot rescue it either, because `unexpressed` only ever
      reports what `PreferenceState` *held* and could not compile into a
      `QueryPlan` (a budget, a second liked bottle, a stale comparative,
      an occasion string outside `OCCASION_WORDS`) — a word `plan.parse`
      never turned into anything in the first place never reaches that
      layer to be reported on. So "not too powdery" is a stated
      preference this version of FACET drops with no record anywhere in
      the response. That gap is what this test proves, directly, rather
      than describing it in prose and hoping it stays true.
    """

    def test_every_recognised_fragment_compiles_and_the_unrecognised_one_is_provably_silent(
        self, client
    ):
        session_id = _create(client)
        text = (
            "I want something feminine and expensive-smelling, "
            "not too powdery, good for date night"
        )
        resp = client.post(f"/api/session/{session_id}/say", json={"text": text})
        body = resp.json()
        prefs = body["state"]["attribute_prefs"]

        assert prefs["vibe:feminine"] == "more"
        assert prefs["concept:expensive smelling"] == "more"
        assert prefs["occasion:date night"] == "more"
        assert prefs["occasion:date"] == "more"
        # Nothing else pending: no budget, no second liked bottle, no
        # stale comparative, no unrecognised occasion string — this
        # sentence's `unexpressed` list is genuinely empty, not merely
        # unchecked.
        assert body["unexpressed"] == []

        # The gap: no trace of "powdery" anywhere in the response.
        assert not any("powder" in k for k in prefs)
        assert "powder" not in body["note"].lower()

        assert_honest(body)

    def test_a_sentence_that_carries_nothing_the_parser_recognises_is_a_refusal(
        self, client
    ):
        """The other honesty rail this scenario needs: when nothing at
        all compiles, `/say` must say so via the engine's own refusal
        wording (F5) — never a quiet 200 with empty results and no
        explanation. Built from `heavy`, a word `plan.INTENSITY_WORDS`
        explicitly excludes from ever being read as a note, so this
        refuses regardless of what corpus vocabulary happens to exist —
        the same probe `audit.PROBES` itself uses for exactly this
        reason.
        """
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "something heavy"}
        )
        body = resp.json()
        assert body["note"] == (
            "nothing in this request maps to evidence the corpus holds"
        )
        assert body["results"] == []
        assert_honest(body)


# --- Case 3 ------------------------------------------------------------


class TestChipsOnlyRomanticWarmAndCozy:
    """Retail mockup: a self-serve kiosk session where the shopper never
    types a sentence at all — they only tap mood chips, "Romantic" and
    "Warm & Cozy" — shaped as the three `/prefs` attribute calls a real
    chip UI would send: one attribute per call, and "Warm & Cozy" is two
    words in `plan.VIBE_WORDS` ("warm", "cozy"), not one, so it is two
    chips and two calls, not one.
    """

    def test_no_candidate_is_fabricated_and_the_empty_state_is_honest(
        self, client, conn
    ):
        cozy_bottle = add_fragrance(conn, "Cozy Amber Blanket")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=cozy_bottle, value="cozy", author=author,
                 channel=f"c{i}", claim_type="AESTHETIC")
        for i, author in enumerate(["q1", "q2"]):
            note(conn, 10 + i, frag=cozy_bottle, value="warm", author=author,
                 channel=f"w{i}", claim_type="AESTHETIC")
        # A second, real fixture bottle the chips carry no evidence for
        # at all — proving a real id can legitimately be absent from
        # `results` rather than every fixture bottle showing up by
        # default, which would be a weaker "no fabrication" test.
        unrelated = add_fragrance(conn, "Citrus Splash")
        for i, author in enumerate(["r1", "r2"]):
            note(conn, 20 + i, frag=unrelated, value="citrus", author=author,
                 channel=f"x{i}")

        real_ids = {row["id"] for row in conn.execute("SELECT id FROM fragrances")}

        session_id = _create(client)
        resp = None
        for attribute in ("romantic", "warm", "cozy"):
            resp = client.post(
                f"/api/session/{session_id}/prefs",
                json={"attribute": attribute, "direction": "more"},
            )
            assert resp.status_code == 200

        body = resp.json()
        assert body["state"]["attribute_prefs"] == {
            "vibe:romantic": "more",
            "vibe:warm": "more",
            "vibe:cozy": "more",
        }

        # No fabrication: every id in the response is a real fixture row.
        for result in body["results"]:
            assert result["fragrance_id"] in real_ids

        # Honest either way: real results if the corpus actually
        # supports the chips, an explanatory note if it does not — never
        # a blank 200 that reads identically to "the kiosk is broken."
        assert body["results"] or body["note"]
        # The bottle with no evidence for any tapped chip must not be
        # invented as a match.
        assert unrelated not in {r["fragrance_id"] for r in body["results"]}
        assert cozy_bottle in {r["fragrance_id"] for r in body["results"]}
        assert_honest(body)


# --- Case 4 ------------------------------------------------------------


class TestPerformanceLongevityRequireAndStrongProjection:
    """Retail mockup: a shopper taps "Longevity: must-have" and then
    says "strong projection."

    The chip's real shape, confirmed against `session.classify_attribute`
    (the chip path's own mirror of `plan.parse`'s vocabulary): a bare
    `attribute="longevity"` maps to `("longevity", "longevity")` —
    `plan.ATTRIBUTE_WORDS` names the *axis*, not a value on it — which
    compiles to a hard `Constraint(attribute="longevity",
    value="longevity")` that can never be satisfied by any real fact,
    because `AttributeFact.value` for this axis is only ever "long
    lasting" or "short lived" (`evidence.SENTIMENT_VALUE`,
    `POSITIVE_END`). The chip that actually works sends the *value*
    phrase: `attribute="long lasting"` is a `PERFORMANCE_PHRASES` entry
    that classifies to the correct `("longevity", "long lasting")` pair.
    This is confirmed directly below, not assumed: the working chip gets
    the CONTESTED-evidence acceptance test; the broken one gets its own
    test proving it silently filters everything out, so the gap is
    provable rather than merely asserted in this docstring.
    """

    def _seeded(self, conn):
        loud = add_fragrance(conn, "Loud Amber Bottle")
        for i, author in enumerate(["p1", "p2", "p3"]):
            _claim(conn, i, frag=loud, claim_type="LONGEVITY", value=None,
                   author=author, channel=f"long_pos_{i}", sentiment="POSITIVE")
        for i, author in enumerate(["p4", "p5"]):
            _claim(conn, 10 + i, frag=loud, claim_type="LONGEVITY", value=None,
                   author=author, channel=f"long_neg_{i}", sentiment="NEGATIVE")
        for i, author in enumerate(["q1", "q2", "q3"]):
            _claim(conn, 20 + i, frag=loud, claim_type="PROJECTION", value=None,
                   author=author, channel=f"proj_{i}", sentiment="POSITIVE")
        return loud

    def test_the_disagreement_survives_into_the_response_wording(
        self, client, conn
    ):
        loud = self._seeded(conn)
        session_id = _create(client)
        chip = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "long lasting", "direction": "require"},
        )
        assert chip.status_code == 200
        resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "strong projection"}
        )
        body = resp.json()
        assert body["state"]["attribute_prefs"]["longevity:long lasting"] == "require"
        assert body["state"]["attribute_prefs"]["projection:strong"] == "more"

        ids = [r["fragrance_id"] for r in body["results"]]
        assert ids == [loud]
        top = body["results"][0]
        every_reason_text = " ".join(
            r["text"] for r in top["reasons"] + top["caveats"]
        )
        # Never smoothed into a plain assertion: "disagree" has to
        # actually appear, and the counts on both sides have to be
        # stated rather than netted into one number.
        assert "disagree" in every_reason_text
        assert re.search(
            r"\d+ (?:person|people) say so and \d+ disagree", every_reason_text
        )
        assert_honest(body)

    def test_the_bare_attribute_word_chip_is_a_dead_filter(self, client, conn):
        """Documented gap: a chip UI naive enough to send the axis name
        itself (`attribute="longevity"`, the label printed on the
        toggle) rather than the value phrase compiles to a hard
        constraint no fact can ever satisfy. The same bottle, evidenced
        both ways, disappears from the results entirely rather than
        surfacing with its disagreement shown — the opposite of the
        working chip's behaviour above, and worth pinning precisely
        because it looks, from the request body alone, like the same
        feature."""
        self._seeded(conn)
        session_id = _create(client)
        client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "longevity", "direction": "require"},
        )
        resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "strong projection"}
        )
        body = resp.json()
        assert body["state"]["attribute_prefs"]["longevity:longevity"] == "require"
        assert body["results"] == []
        assert_honest(body)


# --- Case 5 ------------------------------------------------------------


class TestOccasionEvidenceOneBottleNotOthers:
    """Retail mockup: a kiosk's occasion selector, set through
    `/prefs`'s `occasion` field — not typed as free text, so this
    exercises `PreferenceState.set_occasion` / `_match_occasion` rather
    than `plan._read_words`'s occasion grammar (Case 2 already covers
    the free-text path).
    """

    def test_occasion_influences_only_the_bottle_the_corpus_has_evidence_for(
        self, client, conn
    ):
        wedding_bottle = add_fragrance(conn, "Wedding Day Bloom")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=wedding_bottle, value="wedding", author=author,
                 channel=f"w{i}", claim_type="OCCASION")
        # A second, real bottle the corpus has evidence about — just not
        # for this occasion — so its absence below is evidence-driven,
        # not the only fixture row that happens to exist.
        everyday_bottle = add_fragrance(conn, "Everyday Citrus")
        for i, author in enumerate(["q1", "q2"]):
            note(conn, 10 + i, frag=everyday_bottle, value="citrus", author=author,
                 channel=f"c{i}")

        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs", json={"occasion": "wedding"}
        )
        body = resp.json()
        assert body["state"]["occasion"] == "wedding"
        assert body["unexpressed"] == []

        ids = [r["fragrance_id"] for r in body["results"]]
        assert ids == [wedding_bottle]
        reason_text = " ".join(r["text"] for r in body["results"][0]["reasons"])
        assert "wedding" in reason_text
        assert_honest(body)

    def test_an_occasion_outside_the_recognised_vocabulary_is_unexpressed_not_dropped(
        self, client
    ):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"occasion": "a rooftop birthday"},
        )
        body = resp.json()
        assert body["state"]["occasion"] == "a rooftop birthday"
        assert body["unexpressed"] == [{
            "preference": "occasion",
            "reason": "'a rooftop birthday' is not a recognised occasion word",
        }]
        assert_honest(body)


# --- Case 6 ------------------------------------------------------------


class TestBudgetAndAvoidSweetThenPersistentExclusion:
    """Retail mockup: a shopper sets an $80 budget chip, says "not too
    sweet," rejects the top suggestion, and keeps refining.

    WHEN RETAIL PRICES LAND (M3): the budget assertions in this test
    must flip from "reported as unexpressed, never filters" to "filters
    results below $80" — deliberately, by someone reading this comment
    and rewriting them, never by this test quietly starting to pass or
    fail for an unrelated reason. See `PreferenceState.to_plan`'s
    docstring: "Nothing in `plan.py` or `recommend.py` has a notion of
    price" is the M1 reality this test pins tightly on purpose.
    """

    def _seeded(self, conn):
        fresh_bottle = add_fragrance(conn, "Clean Linen Fresh")
        for i, author in enumerate(["p1", "p2"]):
            note(conn, i, frag=fresh_bottle, value="citrus", author=author,
                 channel=f"f{i}")
        sweet_bottle = add_fragrance(conn, "Vanilla Sugar Bomb")
        for i, author in enumerate(["q1", "q2", "q3"]):
            note(conn, 10 + i, frag=sweet_bottle, value="sweet", author=author,
                 channel=f"s{i}")
        return fresh_bottle, sweet_bottle

    def test_budget_unexpressed_sweet_avoid_compiles_no_stays_excluded_across_refinement(
        self, client, conn
    ):
        self._seeded(conn)
        session_id = _create(client)

        budget_resp = client.post(
            f"/api/session/{session_id}/prefs", json={"budget_usd": 80.0}
        )
        budget_body = budget_resp.json()
        assert budget_body["state"]["budget_usd"] == 80.0
        assert budget_body["unexpressed"] == [
            {"preference": "budget", "reason": "no price data yet"}
        ]
        # F4: nothing composed by the API is glued into `note`.
        assert budget_body["note"] == ""
        assert_honest(budget_body)

        say_resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "not too sweet"}
        )
        say_body = say_resp.json()
        assert say_body["state"]["attribute_prefs"]["note:sweet"] == "less"
        # Budget survives the second reply, still carried in state and
        # still reported — every time it is set, not only the first
        # time (see `to_plan`'s docstring for why silence must not be
        # mistaken for the budget having started mattering).
        assert say_body["state"]["budget_usd"] == 80.0
        assert say_body["unexpressed"] == [
            {"preference": "budget", "reason": "no price data yet"}
        ]
        assert_honest(say_body)

        results = say_body["results"]
        assert results, "fixture must actually produce a candidate to NO"
        top_id = results[0]["fragrance_id"]

        feedback_resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": top_id, "verdict": "NO"},
        )
        feedback_body = feedback_resp.json()
        assert top_id not in {r["fragrance_id"] for r in feedback_body["results"]}
        assert_honest(feedback_body)

        refine_resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "something fresh"}
        )
        refine_body = refine_resp.json()
        assert top_id not in {r["fragrance_id"] for r in refine_body["results"]}, (
            "an explicit NO must survive a later, unrelated refinement"
        )
        assert refine_body["state"]["budget_usd"] == 80.0
        assert refine_body["unexpressed"] == [
            {"preference": "budget", "reason": "no price data yet"}
        ]
        assert_honest(refine_body)
