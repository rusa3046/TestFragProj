"""FACET's HTTP layer, exercised the way a real client would call it.

`get_conn` is overridden per test to hand the FastAPI app the same `conn`
fixture every other test in this suite uses — one transaction, truncated
between tests, no second database to keep in sync. `app.on_event("startup")`
never runs here: the `TestClient` below is never entered as a context
manager, which is exactly the trick that keeps it from trying to migrate
`FRAGRANCE_DB_URL` (the *real* database) on every test collection.
"""

import json

import pytest

# A core-only install (`uv sync --extra dev`, no [api]) has no fastapi by
# design; these tests then skip rather than kill collection for the whole
# suite. CI installs both extras so they always actually run there — a
# workflow test below pins that, because this exact failure shipped once.
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from fragrance_graph.api import app, get_conn  # noqa: E402
from fragrance_graph.evidence import Strength
from fragrance_graph.ingest.store import ingest
from fragrance_graph.recommend import Reason, Recommendation
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment
from tests.test_recommend import note


def _add_comment(conn, i, *, author, video, channel=None):
    ingest(
        conn,
        [
            make_comment(
                i,
                body=f"person {i} wrote this",
                permalink=f"https://example.test/c/{i}",
                source_channel=channel or f"UC-{video}",
                raw_json=json.dumps({"author": author, "videoId": video}),
            )
        ],
    )
    return conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]


def _add_similar_to(conn, comment_id, *, subject, obj):
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             polarity, extraction_model, created_at)
        VALUES (%s, 'SIMILAR_TO', 'FRAGRANCE', 'subject', %s, 'FRAGRANCE',
                'object', %s, 'POSITIVE', 0.9, 'smells the same', 1,
                'ASSERTED', 'test', '2026-01-01')
        """,
        (comment_id, subject, obj),
    )
    conn.commit()


@pytest.fixture
def client(conn):
    def _override():
        yield conn

    app.dependency_overrides[get_conn] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def khamrah(conn):
    """A bottle real enough to be recommended, resolved by more than one
    spelling — mirrors the real corpus's own curated aliases."""
    frag = add_fragrance(conn, "Lattafa Khamrah", aliases=["Khamrah"])
    for i, author in enumerate(["p1", "p2", "p3", "p4"]):
        note(conn, i, frag=frag, value="dates", author=author, channel=f"c{i}")
    return frag


def _create(client, mode=None) -> str:
    body = {"mode": mode} if mode else {}
    resp = client.post("/api/session", json=body)
    assert resp.status_code == 200
    return resp.json()["session_id"]


class TestCreateSession:
    def test_creates_a_session_with_a_uuid_and_default_mode(self, client):
        resp = client.post("/api/session", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["mode"] == "unknown"
        import uuid

        uuid.UUID(body["session_id"])  # does not raise

    def test_accepts_an_explicit_mode(self, client):
        resp = client.post("/api/session", json={"mode": "associate"})
        assert resp.json()["mode"] == "associate"

    def test_rejects_an_unrecognised_mode(self, client):
        resp = client.post("/api/session", json={"mode": "wholesale"})
        assert resp.status_code == 422


class TestSayEndpoint:
    def test_merges_free_text_and_returns_state_plus_recommendations(
        self, client, khamrah
    ):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "something with dates"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"]["free_text_history"] == ["something with dates"]
        assert any(r["name"] == "Lattafa Khamrah" for r in body["results"])

    def test_a_second_utterance_accumulates_rather_than_replaces(self, client):
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "first thing"})
        resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "second thing"}
        )
        assert resp.json()["state"]["free_text_history"] == ["first thing", "second thing"]

    def test_an_unknown_session_id_is_404(self, client):
        resp = client.post(
            "/api/session/00000000-0000-0000-0000-000000000000/say",
            json={"text": "hello"},
        )
        assert resp.status_code == 404

    # --- F5: a refused utterance must not be silently swallowed ----------

    def test_a_refused_utterance_surfaces_the_engine_refusal_as_note(self, client):
        """F5, reproduced: `plan.parse` refuses "actually sweeter is
        fine" (no rule for the bare comparative "sweeter" — F8's docstring
        fix explains why). The old behaviour answered with an unrelated,
        API-composed "Nothing said yet" sentence; the fix surfaces the
        engine's own refusal wording instead, since something genuinely
        was said and was not understood.
        """
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/say",
            json={"text": "actually sweeter is fine"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["note"]
        assert "nothing said yet" not in body["note"].lower()
        assert body["note"] == (
            "nothing in this request maps to evidence the corpus holds"
        )

    def test_the_refused_utterance_still_appears_in_history(self, client):
        session_id = _create(client)
        client.post(
            f"/api/session/{session_id}/say",
            json={"text": "actually sweeter is fine"},
        )
        state = client.get(f"/api/session/{session_id}").json()["state"]
        assert state["free_text_history"] == ["actually sweeter is fine"]

    # --- F10: control characters must never reach storage -----------------

    def test_a_nul_character_is_stripped_rather_than_500ing(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/say",
            json={"text": "sweet \x00 thing"},
        )
        assert resp.status_code == 200
        state = resp.json()["state"]
        assert state["free_text_history"] == ["sweet  thing"]
        assert "\x00" not in state["free_text_history"][0]

    def test_text_that_is_only_control_characters_is_422(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/say", json={"text": "\x00\x01\x02"}
        )
        assert resp.status_code == 422

    def test_an_empty_text_is_422(self, client):
        session_id = _create(client)
        resp = client.post(f"/api/session/{session_id}/say", json={"text": "   "})
        assert resp.status_code == 422

    def test_a_session_survives_a_nul_character_and_keeps_working(self, client):
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "sweet \x00 thing"})
        followup = client.get(f"/api/session/{session_id}")
        assert followup.status_code == 200


class TestPrefsEndpoint:
    def test_a_control_character_in_occasion_is_stripped_not_a_500(self, client):
        """`/say` strips control characters before storage because a NUL
        in jsonb is a 500 at INSERT time. `/prefs` carries free text down
        the same path and had no such guard — found flagged-but-unfixed in
        the fix round, confirmed by experiment (500, session survives),
        fixed by applying the same sanitizer."""
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"occasion": "date \x00 night"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"]["occasion"] == "date  night"

    def test_an_occasion_of_only_control_characters_is_422(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs", json={"occasion": "\x00\x01"}
        )
        assert resp.status_code == 422

    def test_an_attribute_chip_merges_into_state(self, client, khamrah):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "dates", "direction": "require"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"]["attribute_prefs"] == {"note:dates": "require"}

    def test_an_occasion_body_sets_occasion_not_an_attribute_pref(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs", json={"occasion": "wedding"}
        )
        body = resp.json()
        assert body["state"]["occasion"] == "wedding"
        assert body["state"]["attribute_prefs"] == {}

    def test_a_budget_body_is_stored_and_reported_in_unexpressed_not_note(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs", json={"budget_usd": 150.0}
        )
        body = resp.json()
        assert body["state"]["budget_usd"] == 150.0
        assert body["unexpressed"] == [
            {"preference": "budget", "reason": "no price data yet"}
        ]
        # F4: nothing composed by the API is glued into `note`.
        assert body["note"] == ""

    def test_a_body_with_none_of_the_three_shapes_is_422(self, client):
        session_id = _create(client)
        resp = client.post(f"/api/session/{session_id}/prefs", json={})
        assert resp.status_code == 422

    def test_a_later_chip_overrides_an_earlier_one_on_the_same_word(self, client):
        session_id = _create(client)
        client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "feminine", "direction": "less"},
        )
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "feminine", "direction": "more"},
        )
        assert resp.json()["state"]["attribute_prefs"] == {"vibe:feminine": "more"}

    def test_direction_is_case_insensitive_like_feedbacks_verdict(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "feminine", "direction": "MORE"},
        )
        assert resp.status_code == 200
        assert resp.json()["state"]["attribute_prefs"] == {"vibe:feminine": "more"}

    # --- F1: an invalid direction must never poison the session ----------

    def test_an_invalid_direction_is_422_with_a_clear_message(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "sweet", "direction": "wibble"},
        )
        assert resp.status_code == 422
        assert "wibble" in resp.json()["detail"]
        assert "direction" in resp.json()["detail"].lower()

    def test_an_invalid_direction_never_reaches_the_event_log(self, client, conn):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "sweet", "direction": "wibble"},
        )
        assert resp.status_code == 422
        row = conn.execute(
            "SELECT count(*) FROM session_events WHERE session_id = %s", (session_id,)
        ).fetchone()
        assert row[0] == 0

    def test_a_rejected_direction_leaves_the_session_alive_for_later_requests(
        self, client
    ):
        """F1's exact failing scenario, reproduced and fixed: a bad
        `direction` used to be recorded *before* validation, so every
        subsequent request replayed the poisoned event and 500'd forever
        — including a perfectly valid follow-up `/say`. Now the bad value
        is rejected before it is ever written, so nothing downstream is
        affected.
        """
        session_id = _create(client)
        bad = client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "sweet", "direction": "wibble"},
        )
        assert bad.status_code == 422

        still_alive = client.get(f"/api/session/{session_id}")
        assert still_alive.status_code == 200

        followup = client.post(
            f"/api/session/{session_id}/say", json={"text": "something with dates"}
        )
        assert followup.status_code == 200

    def test_all_provided_fields_in_one_body_are_applied(self, client):
        """F11, confirmed by experiment before this fix: `occasion` and
        `budget_usd` sent in the same body silently kept only `occasion`
        (`elif` branch order) — which is exactly the shape of the bug
        the coordinator hit manually ("I set budget_usd 250 ... session
        stored None"). All three shapes may now be combined in one call
        and all of them are applied.
        """
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={
                "attribute": "feminine", "direction": "more",
                "occasion": "wedding", "budget_usd": 250.0,
            },
        )
        assert resp.status_code == 200
        state = resp.json()["state"]
        assert state["attribute_prefs"] == {"vibe:feminine": "more"}
        assert state["occasion"] == "wedding"
        assert state["budget_usd"] == 250.0

    def test_occasion_and_budget_together_no_longer_drops_budget(self, client):
        """The coordinator's exact repro, minus the attribute/direction
        shape: occasion and budget in one call, both must survive."""
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/prefs",
            json={"occasion": "yacht regatta afterparty", "budget_usd": 80.0},
        )
        state = resp.json()["state"]
        assert state["occasion"] == "yacht regatta afterparty"
        assert state["budget_usd"] == 80.0


class TestFeedbackEndpoint:
    def test_love_is_recorded_and_becomes_the_anchor(self, client, khamrah):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": khamrah, "verdict": "LOVE"},
        )
        assert resp.status_code == 200
        state = resp.json()["state"]
        assert state["liked_fragrances"] == ["Lattafa Khamrah"]
        assert state["feedback"] == [{"fragrance_id": khamrah, "verdict": "love"}]

    def test_no_excludes_the_bottle_from_the_returned_results(self, client, conn, khamrah):
        other = add_fragrance(conn, "Some Other Bottle")
        for i, author in enumerate(["q1", "q2", "q3", "q4"]):
            note(conn, 10 + i, frag=other, value="dates", author=author, channel=f"d{i}")
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "something with dates"})
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": khamrah, "verdict": "NO"},
        )
        names = [r["name"] for r in resp.json()["results"]]
        assert "Lattafa Khamrah" not in names

    def test_maybe_deprioritizes_rather_than_excludes(self, client, conn, khamrah):
        other = add_fragrance(conn, "Some Other Bottle")
        for i, author in enumerate(["q1", "q2", "q3", "q4"]):
            note(conn, 10 + i, frag=other, value="dates", author=author, channel=f"d{i}")
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "something with dates"})
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": khamrah, "verdict": "MAYBE"},
        )
        names = [r["name"] for r in resp.json()["results"]]
        assert "Lattafa Khamrah" in names  # still eligible
        assert names.index("Lattafa Khamrah") == len(names) - 1  # sunk, not excluded

    def test_an_unknown_fragrance_id_is_404(self, client):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": 999999, "verdict": "LOVE"},
        )
        assert resp.status_code == 404

    def test_an_invalid_verdict_is_422(self, client, khamrah):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": khamrah, "verdict": "MEH"},
        )
        assert resp.status_code == 422

    def test_a_no_on_the_anchor_falls_back_to_the_previous_liked_bottle(
        self, client, conn, khamrah
    ):
        """F3: `liked_fragrances` and the served anchor must never
        disagree. Loves Khamrah first, then a second bottle (making it
        the anchor), then NOs the second bottle — Khamrah, still liked,
        must become the anchor again rather than the state claiming
        nothing is anchored while Khamrah sits right there in
        `liked_fragrances`."""
        second = add_fragrance(conn, "Second Bottle")
        session_id = _create(client)
        client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": khamrah, "verdict": "LOVE"},
        )
        client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": second, "verdict": "LOVE"},
        )
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": second, "verdict": "NO"},
        )
        state = resp.json()["state"]
        assert state["liked_fragrances"] == ["Lattafa Khamrah"]
        assert "Second Bottle" in state["disliked_fragrances"]

    def test_a_stale_comparative_does_not_recompile_against_a_new_anchor(
        self, client, conn
    ):
        """F2, the reviewer's exact scenario, driven through the real
        HTTP layer: love Delina (with a comparative about its rose), NO
        Delina, LOVE a different bottle (BR540). BR540 is now a real,
        current anchor — but it is not the one "less rose" was measured
        against, and the response must not silently judge "less rose
        than BR540" instead.
        """
        delina = add_fragrance(conn, "Parfums de Marly Delina", aliases=["Delina"])
        br540 = add_fragrance(
            conn, "Maison Francis Kurkdjian Baccarat Rouge 540", aliases=["BR540"]
        )
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=delina, value="rose", author=author, channel=f"c{i}")

        session_id = _create(client)
        client.post(
            f"/api/session/{session_id}/say",
            json={"text": "I love Delina but the rose is too strong"},
        )
        client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": delina, "verdict": "NO"},
        )
        resp = client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": br540, "verdict": "LOVE"},
        )
        body = resp.json()
        assert body["state"]["liked_fragrances"] == [
            "Maison Francis Kurkdjian Baccarat Rouge 540"
        ]
        matches = [
            u for u in body["unexpressed"]
            if u["preference"] == "rose relative to Parfums de Marly Delina"
        ]
        assert matches, f"expected a stale-comparative entry; got {body['unexpressed']}"
        assert matches[0]["reason"] == (
            "was said about Parfums de Marly Delina, current anchor is "
            "Maison Francis Kurkdjian Baccarat Rouge 540"
        )


class TestGetSession:
    def test_reflects_everything_merged_so_far(self, client, khamrah):
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "something with dates"})
        client.post(
            f"/api/session/{session_id}/feedback",
            json={"fragrance_id": khamrah, "verdict": "MAYBE"},
        )
        resp = client.get(f"/api/session/{session_id}")
        state = resp.json()["state"]
        assert state["free_text_history"] == ["something with dates"]
        assert state["feedback"] == [{"fragrance_id": khamrah, "verdict": "maybe"}]

    def test_state_is_rebuilt_from_events_not_cached_between_calls(self, client, khamrah):
        """Two independent GETs for the same session must agree — there is
        no in-process cache this test could accidentally be reading
        instead of the event log."""
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "something with dates"})
        first = client.get(f"/api/session/{session_id}").json()
        second = client.get(f"/api/session/{session_id}").json()
        assert first["state"] == second["state"]

    def test_an_unknown_session_id_is_404(self, client):
        resp = client.get("/api/session/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


class TestStatelessRecommend:
    def test_wraps_recommend_directly(self, client, khamrah):
        resp = client.post("/api/recommend", json={"text": "something with dates"})
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()["results"]]
        assert "Lattafa Khamrah" in names

    def test_a_refusal_is_returned_as_note_with_empty_results(self, client):
        resp = client.post("/api/recommend", json={"text": "hello there"})
        body = resp.json()
        assert body["results"] == []
        assert body["note"]


class TestFragranceProfile:
    def test_matches_the_ask_py_profile_path(self, client, conn, khamrah):
        from fragrance_graph.ask import profile_pages

        [(_name, _slug, expected)] = [
            p for p in profile_pages(conn) if p[0] == "Lattafa Khamrah"
        ]
        resp = client.get(f"/api/fragrance/{khamrah}")
        body = resp.json()
        assert [c["name"] for c in body["results"]] == [r.name for r in expected.results]
        got_texts = [r["text"] for r in body["results"][0]["reasons"]]
        want_texts = [r.phrase() for r in expected.results[0].reasons]
        assert got_texts == want_texts

    def test_an_unknown_id_is_404(self, client):
        resp = client.get("/api/fragrance/999999")
        assert resp.status_code == 404


class TestSearch:
    def test_an_exact_name_returns_a_match_and_no_suggestions(self, client, khamrah):
        resp = client.get("/api/search", params={"q": "Lattafa Khamrah"})
        body = resp.json()
        assert body["match"] == {"fragrance_id": khamrah, "name": "Lattafa Khamrah"}
        assert body["suggestions"] == []

    def test_a_curated_alias_is_also_an_exact_match(self, client, khamrah):
        resp = client.get("/api/search", params={"q": "khamrah"})
        assert resp.json()["match"]["fragrance_id"] == khamrah

    def test_a_near_miss_is_offered_as_a_suggestion_not_a_match(self, client, khamrah):
        resp = client.get("/api/search", params={"q": "khamra"})
        body = resp.json()
        assert body["match"] is None
        assert any(s["fragrance_id"] == khamrah for s in body["suggestions"])

    def test_an_unrelated_query_returns_no_suggestions(self, client, khamrah):
        resp = client.get("/api/search", params={"q": "zzz totally unrelated qqq"})
        body = resp.json()
        assert body["match"] is None
        assert body["suggestions"] == []

    def test_suggestions_carry_no_numeric_score(self, client, khamrah):
        """F9: the brief's rule is no numeric score in any response
        field. `suggestions` used to carry an edit-similarity float next
        to each name — already sorted best-first, so the number added
        nothing but an invitation for a UI to show it as if it meant
        something about the fragrance."""
        resp = client.get("/api/search", params={"q": "khamra"})
        body = resp.json()
        assert body["suggestions"], "fixture must actually produce a suggestion"
        for suggestion in body["suggestions"]:
            assert suggestion.keys() == {"fragrance_id", "name"}


class TestSprayQueue:
    def test_returns_at_most_three_never_padded(self, client, conn):
        # Only one candidate exists in the whole catalogue.
        frag = add_fragrance(conn, "Only Bottle")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="dates", author=author, channel=f"c{i}")
        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "something with dates"})
        resp = client.get(f"/api/session/{session_id}/spray-queue")
        assert len(resp.json()["queue"]) == 1

    def test_an_empty_session_returns_an_empty_queue_and_no_invented_note(self, client):
        """F4: `note` is engine wording only. A session nothing has been
        said in has no engine note to show — an empty string, not an
        API-composed "nothing said yet" sentence that used to live here
        and was itself unaudited."""
        session_id = _create(client)
        resp = client.get(f"/api/session/{session_id}/spray-queue")
        body = resp.json()
        assert body["queue"] == []
        assert body["note"] == ""

    def test_alternates_have_a_different_dominant_signature_than_the_best_match(
        self, client, conn
    ):
        """A candidate reached purely through the comparison graph
        (`kind="graph"`) versus one reached by directly matching a soft
        preference (`kind="prefer"`) have different dominant signatures —
        exactly the "different direction" `spray_queue`'s docstring
        describes. This needs a *soft* preference for the direct match,
        not a hard one: `plan.py`'s grammar only ever emits a hard
        `Constraint` for an unnegated note word typed as free text, and a
        hard constraint would filter the graph-only neighbour out before
        the queue ever saw it — so the direct match is set up via the
        chip endpoint's `MORE` direction, which compiles to a genuine
        soft preference (`session.classify_attribute`'s whole reason for
        existing).
        """
        anchor = add_fragrance(conn, "Dates Bottle", aliases=["Dates Bottle"])
        best = add_fragrance(conn, "Coffee Best")
        neighbour = add_fragrance(conn, "Graph Neighbour")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=best, value="coffee", author=author, channel=f"c{i}")
        for i, author in enumerate(["q1", "q2", "q3"]):
            cid = _add_comment(conn, 10 + i, author=author, video=f"v{i}")
            _add_similar_to(conn, cid, subject=neighbour, obj=anchor)

        session_id = _create(client)
        client.post(f"/api/session/{session_id}/say", json={"text": "I love Dates Bottle"})
        client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "coffee", "direction": "more"},
        )
        resp = client.get(f"/api/session/{session_id}/spray-queue")
        queue = resp.json()["queue"]
        names = [c["name"] for c in queue]
        assert "Coffee Best" in names
        assert "Graph Neighbour" in names
        assert len(queue) <= 3


class TestLabelDerivation:
    """Direct unit tests on `_label` (`commerce_card.result_tier`), no
    database needed — every input is a hand-built `Reason`/`Recommendation`.

    Rewritten twice now. First for the (then-new) commerce result tiers
    (`STRONG_FIT`/`GOOD_FIT`/`PARTIAL_FIT`/`EXPLORATORY_PICK`, spec §16,
    `commerce-audit.md` §7), replacing the old four-value, rank-coupled
    `BEST_MATCH`/`STRONG_MATCH`/`WORTH_TRYING`/`ALTERNATIVE_DIRECTION`
    label along with its `index` parameter. Rewritten again for the
    catalog-first spec's two-axis label set (`BEST_OVERALL_FIT`/
    `STRONG_PROFILE_FIT`/`COMMUNITY_FAVORITE`/`WORTH_DISCOVERING`) — see
    `commerce_card.result_tier`'s current docstring for the exact rule.
    `_label`/`result_tier` still take no `index`/rank parameter at all,
    across both rewrites, which is what makes "never position-based"
    (spec) a fact about the function's signature, not merely its current
    behaviour.
    """

    def _strong(self, people=8, creators=4, videos=3, kind="prefer"):
        return Reason(
            kind=kind, text="rose", strength=Strength.SUPPORTED,
            people=people, creators=creators, videos=videos,
        )

    def _moderate(self, kind="prefer"):
        # 2 people, 2 creators, 0 videos — clears MODERATE (`creators >=
        # MIN_SOURCES`) but not STRONG (`evidence.tier_of` also needs
        # `videos >= MIN_SOURCES`).
        return Reason(
            kind=kind, text="feminine", strength=Strength.REPEATED,
            people=2, creators=2, videos=0,
        )

    def _single_source(self, kind="prefer"):
        return Reason(
            kind=kind, text="rose", strength=Strength.OBSERVED, people=1, creators=1,
        )

    def test_two_moderate_matched_dimensions_is_community_favorite(self):
        """The old `STRONG_FIT` bar, reached on community evidence alone
        (no `catalog_fit` reason anywhere) — now `COMMUNITY_FAVORITE`,
        not `BEST_OVERALL_FIT`, since only one of the two axes is strong."""
        from fragrance_graph.api import _label

        candidate = Recommendation(
            1, "X", reasons=[self._moderate(), self._strong()]
        )
        assert _label(candidate) == "COMMUNITY_FAVORITE"

    def test_one_strong_matched_dimension_is_worth_discovering(self):
        """A single matched dimension never earns `COMMUNITY_FAVORITE`,
        however well-evidenced: the community axis requires *two or
        more* answered request dimensions, the identical bar the old
        `STRONG_FIT` used, not one very confident one. With no second
        axis strong either, this is the new bottom rung."""
        from fragrance_graph.api import _label

        candidate = Recommendation(1, "X", reasons=[self._strong()])
        assert _label(candidate) == "WORTH_DISCOVERING"

    def test_two_single_source_matched_dimensions_is_worth_discovering(self):
        from fragrance_graph.api import _label

        candidate = Recommendation(
            1, "X", reasons=[self._single_source(), self._single_source(kind="concept")]
        )
        assert _label(candidate) == "WORTH_DISCOVERING"

    def test_one_single_source_matched_dimension_is_worth_discovering(self):
        """A `SINGLE_SOURCE` match is real evidence the request was
        answered — never rejected — just not enough to call more than
        `WORTH_DISCOVERING`. Independence is not a gate on eligibility
        for a tier at all (`commerce-audit.md` §1/§4)."""
        from fragrance_graph.api import _label

        candidate = Recommendation(1, "X", reasons=[self._single_source()])
        assert _label(candidate) == "WORTH_DISCOVERING"

    def test_only_graph_or_semantic_reasons_is_worth_discovering(self):
        """No `FIT_SIGNAL` on either axis at all — the old bottom rung
        (`EXPLORATORY_PICK`, only graph/semantic proximity) folds into
        the new catch-all `WORTH_DISCOVERING`."""
        from fragrance_graph.api import _label

        candidate = Recommendation(
            1, "X", reasons=[self._strong(kind="graph")]
        )
        assert _label(candidate) == "WORTH_DISCOVERING"

    def test_caveats_never_influence_the_label(self):
        from fragrance_graph.api import _label

        candidate = Recommendation(
            1, "X",
            reasons=[self._strong(kind="graph")],
            caveats=[self._strong(kind="avoid")],
        )
        assert _label(candidate) == "WORTH_DISCOVERING"

    def test_two_catalog_fit_reasons_is_strong_profile_fit(self):
        """The catalog axis's own analogue of `COMMUNITY_FAVORITE` — real
        catalog-derived fit (`kind="catalog_fit"`, zero people/creators
        by design), with no strong community evidence at all. This is
        the exact shape the catalog-first spec's failure case exists to
        make reachable: a bottle with zero comments must still be able
        to earn a real label."""
        from fragrance_graph.api import _label
        from fragrance_graph.evidence import Strength

        candidate = Recommendation(
            1, "X",
            reasons=[
                Reason(kind="catalog_fit", text="a", strength=Strength.CANONICAL),
                Reason(kind="catalog_fit", text="b", strength=Strength.CANONICAL),
            ],
        )
        assert _label(candidate) == "STRONG_PROFILE_FIT"

    def test_label_takes_no_rank_parameter(self):
        """Structural, not behavioural: the old label needed `index` to
        gate `BEST_MATCH`. The new one has no parameter a caller could
        even pass a rank through — "never position-based" is enforced by
        the signature, not by a test asserting two calls agree."""
        import inspect

        from fragrance_graph.api import _label

        assert list(inspect.signature(_label).parameters) == ["candidate"]


class TestAuditSurfaceRegistration:
    def test_api_responses_is_a_registered_renderer_surface(self):
        from fragrance_graph.audit import RENDERERS

        assert "api responses" in RENDERERS

    def test_the_real_audit_exercises_it_when_fastapi_is_installed(self, conn):
        """FastAPI is installed in this environment (the `[api]` extra),
        so the audit's lazy import must succeed and this surface must be
        checked, not merely reported as unexercised — that fallback exists
        for a core install with no `[api]` extra, which this is not."""
        from fragrance_graph.audit import audit

        report = audit(conn)
        assert "api responses" in report.checked
        assert not any(
            "api responses" in u for u in report.unexercised
        )
        assert not [v for v in report.violations if v.surface == "api responses"]

    # --- F6: session-composed wording must be reachable, and the audit
    # --- must actually be sensitive to a real poisoned value ------------

    def test_audited_session_probe_text_carries_session_composed_wording(self, conn):
        """Coverage half of F6: `audited_probe_text` alone only ever
        drives the stateless `/api/recommend` path and cannot reach a
        single sentence `session.WORDING` produces — confirmed by
        construction, since no `PROBES` query ever sets a budget or an
        occasion. This proves `audited_session_probe_text` — the new,
        second probe wired into `_audit_rendered_text` — actually renders
        that wording, not merely that the audit runs without crashing.
        """
        from fragrance_graph.api import audited_session_probe_text

        text = audited_session_probe_text(conn)
        assert "no price data yet" in text

    def test_a_forbidden_phrase_reaching_the_api_through_a_real_fragrance_is_caught(
        self, conn
    ):
        """Sensitivity half of F6, the reviewer's own finding: with
        `audited_probe_text`/`audited_session_probe_text` replaced by a
        hardcoded benign string, the *existing* three tests above all
        still pass — `checked['api responses']` is still incremented,
        still zero violations, never reported unexercised. That is
        exactly indistinguishable from a live, correctly-wired renderer,
        which means none of them would notice the renderer going dead.

        This test is the fix: it plants a real forbidden phrase through
        the real path — a fragrance named with one, made the lowest-id
        row so `audited_session_probe_text`'s own catalogue-order probe
        (not a fixed name like "Delina") picks it up automatically — and
        asserts the *real* `audit(conn)` flags it. A hardcoded-string
        renderer cannot pass this: the phrase only ever reaches the
        output by `merge_utterance` genuinely resolving this fragrance as
        an anchor and `_session_response` genuinely rendering its name.
        """
        from fragrance_graph.audit import audit

        frag = add_fragrance(conn, "the community agrees Parfum")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="rose", author=author, channel=f"c{i}")

        report = audit(conn)
        matches = [
            v for v in report.violations
            if v.surface == "api responses" and "forbidden" in v.rule
        ]
        assert matches, (
            "expected a forbidden-phrasing violation on api responses; "
            f"violations were: {report.violations}"
        )
        assert "the community agrees" in matches[0].detail.lower()


class TestMalformedSessionId:
    """F7: `session_id` is a `UUID` column, and handing Postgres anything
    else used to raise `invalid input syntax for type uuid` from inside
    the SELECT — every one of these used to be a 500."""

    BAD_ID = "not-a-uuid"

    def test_get_session(self, client):
        resp = client.get(f"/api/session/{self.BAD_ID}")
        assert resp.status_code == 404

    def test_say(self, client):
        resp = client.post(
            f"/api/session/{self.BAD_ID}/say", json={"text": "hello"}
        )
        assert resp.status_code == 404

    def test_prefs(self, client):
        resp = client.post(
            f"/api/session/{self.BAD_ID}/prefs",
            json={"attribute": "sweet", "direction": "more"},
        )
        assert resp.status_code == 404

    def test_feedback(self, client):
        resp = client.post(
            f"/api/session/{self.BAD_ID}/feedback",
            json={"fragrance_id": 1, "verdict": "LOVE"},
        )
        assert resp.status_code == 404

    def test_spray_queue(self, client):
        resp = client.get(f"/api/session/{self.BAD_ID}/spray-queue")
        assert resp.status_code == 404


class TestAPrehandcraftedPoisonEventStillRebuilds:
    """F1's other half: `/prefs` validating before it writes an event
    means a bad `direction` can no longer get into `session_events`
    through the API — but the append-only log is still a log, and
    `rebuild` is the thing actually responsible for never letting one bad
    row brick a whole session, regardless of how it got there. Simulated
    by inserting the poison row directly, bypassing the validated API
    path entirely — the case a hand-edited row, or a future direct-DB
    write, would produce.
    """

    def test_a_hand_inserted_bad_direction_event_does_not_500_a_later_request(
        self, client, conn
    ):
        session_id = _create(client)
        client.post(
            f"/api/session/{session_id}/prefs",
            json={"attribute": "sweet", "direction": "more"},
        )
        conn.execute(
            "INSERT INTO session_events (session_id, kind, payload) "
            "VALUES (%s, 'prefs', %s)",
            (session_id, json.dumps({"attribute": "loud", "direction": "wibble"})),
        )
        conn.commit()

        resp = client.get(f"/api/session/{session_id}")
        assert resp.status_code == 200
        state = resp.json()["state"]
        assert state["attribute_prefs"] == {"note:sweet": "more"}
        assert not any("loud" in k for k in state["attribute_prefs"])

    def test_the_session_keeps_working_after_a_poison_event(self, client, conn):
        session_id = _create(client)
        conn.execute(
            "INSERT INTO session_events (session_id, kind, payload) "
            "VALUES (%s, 'prefs', %s)",
            (session_id, json.dumps({"attribute": "loud", "direction": "wibble"})),
        )
        conn.commit()

        followup = client.post(
            f"/api/session/{session_id}/say", json={"text": "something with dates"}
        )
        assert followup.status_code == 200
