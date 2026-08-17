"""Composer Phase A — the structured-preference foundation, exercised at
the API level exactly the way `tests/test_facet_cases.py` exercises the
free-text/chip surfaces: the real HTTP layer, real fixture corpora, the
same `client`/`_create` wiring. See that module's own docstring for why
the wiring is duplicated (`_create`/`client`) rather than imported —
`assert_honest`, `_claim` and `_price` genuinely are imported from there,
since duplicating them would risk the two suites' honesty rails silently
drifting apart.

Spec cases covered (`composer-spec-digest.md` §32): single anchor;
anchor + avoid-note (exclude); anchor + want; the full composition; no
anchor; only wants; multiple anchors held with the second in
`unexpressed`; avoid-fragrance excluded from results; the MISSING DATA
rule for avoid-warm, want-strong-projection and budget; budget really
filtering; an invalid item 400ing without poisoning the session;
event-sourced rebuild reproducing chip state; the vocabulary endpoint's
coverage floor; and the honesty rail applied to `/compose` responses.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from fragrance_graph.api import app, get_conn  # noqa: E402
from fragrance_graph.ingest.store import ingest  # noqa: E402
from fragrance_graph.resolve.entities import add_fragrance  # noqa: E402
from fragrance_graph.session import rebuild  # noqa: E402
from tests.conftest import make_comment  # noqa: E402
from tests.test_facet_cases import _claim, _create, _price, assert_honest  # noqa: E402
from tests.test_recommend import note  # noqa: E402

# Duplicated from `tests/test_facet_cases.py` rather than imported — that
# module's own docstring explains why: importing a fixture function makes
# every test's own `client` parameter read (to a linter) as redefining
# the import, and duplicating three lines is cheaper than fighting that.


@pytest.fixture
def client(conn):
    def _override():
        yield conn

    app.dependency_overrides[get_conn] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _similar(conn, i, *, anchor, other, author, channel="chan"):
    """One person connecting `other` to `anchor` in the comparison graph
    — the same shape `tests/test_query.py::add_claim` builds, with a
    `channel` parameter that helper lacks, needed here so a fixture can
    clear `MIN_SOURCES` across independent commenters."""
    ingest(conn, [make_comment(
        i, body=f"comment {i}: similar", source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": f"v{i}"}),
    )])
    cid = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             polarity, extraction_model, created_at)
        VALUES (%s, 'SIMILAR_TO', 'FRAGRANCE', 'it', %s, 'FRAGRANCE',
                'other', %s, 'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test',
                '2026-01-01')
        """,
        (cid, anchor, other, f"comment {i}: similar"),
    )
    conn.commit()


def _compose(client, session_id, preferences, expect_status=200):
    resp = client.post(
        f"/api/session/{session_id}/compose", json={"preferences": preferences}
    )
    assert resp.status_code == expect_status, resp.text
    return resp


def _status_by_value(candidate: dict) -> dict:
    """`value -> status`, keyed by `entity_type` instead when `value` is
    empty — true only for `budget` items, which carry no bare value of
    their own (`operator`/`amount` instead; see `PreferenceItem`)."""
    return {
        s["value"] or s["entity_type"]: s["status"]
        for s in candidate["preference_status"]
    }


# --- single anchor -------------------------------------------------------


class TestSingleAnchor:
    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Solo Anchor Bottle", aliases=["Solo Anchor"])
        neighbour = add_fragrance(conn, "Iris Cousin Bottle")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=neighbour, author=author,
                     channel=f"ch{i}")
        return anchor, neighbour

    def test_a_single_like_fragrance_item_becomes_the_anchor(self, client, conn):
        anchor, neighbour = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "Solo Anchor"},
        ]).json()

        assert body["state"]["items"] == [{
            "index": 0, "bucket": "like", "entity_type": "fragrance",
            "value": "Solo Anchor Bottle", "mode": None, "operator": None,
            "amount": None,
        }]
        assert body["interpreted_preferences"]["anchors"] == ["Solo Anchor Bottle"]
        assert body["interpreted_preferences"]["unexpressed"] == []

        ids = {r["fragrance_id"] for r in body["results"]}
        assert anchor not in ids, "the anchor must never recommend itself"
        assert neighbour in ids
        assert_honest(body)


# --- anchor + avoid-note (exclude) ---------------------------------------


class TestAnchorPlusAvoidNoteExclude:
    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Anchor For Exclude", aliases=["Exclude Anchor"])
        oud_bottle = add_fragrance(conn, "Oud Heavy Cousin")
        clean_bottle = add_fragrance(conn, "Clean Cousin Bottle")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=oud_bottle, author=author,
                     channel=f"o{i}")
        for i, author in enumerate(["d1", "d2", "d3"]):
            _similar(conn, 10 + i, anchor=anchor, other=clean_bottle, author=author,
                     channel=f"cl{i}")
        for i, author in enumerate(["e1", "e2"]):
            note(conn, 20 + i, frag=oud_bottle, value="oud", author=author,
                 channel=f"n{i}")
        return anchor, oud_bottle, clean_bottle

    def test_a_candidate_with_real_evidence_for_the_excluded_note_is_filtered_out(
        self, client, conn
    ):
        anchor, oud_bottle, clean_bottle = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "Exclude Anchor"},
            {"bucket": "avoid", "entity_type": "note", "value": "oud"},
        ]).json()

        assert body["interpreted_preferences"]["exclude"] == ["oud"]
        ids = {r["fragrance_id"] for r in body["results"]}
        assert oud_bottle not in ids, "real evidence for the excluded note must hard-filter it"
        assert clean_bottle in ids
        assert_honest(body)


# --- anchor + want ---------------------------------------------------------


class TestAnchorPlusWant:
    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Anchor For Want", aliases=["Want Anchor"])
        loud_bottle = add_fragrance(conn, "Projection Cousin")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=loud_bottle, author=author,
                     channel=f"p{i}")
        for i, author in enumerate(["q1", "q2", "q3"]):
            _claim(conn, i, frag=loud_bottle, claim_type="PROJECTION", value=None,
                   author=author, channel=f"proj{i}", sentiment="POSITIVE")
        return anchor, loud_bottle

    def test_a_want_performance_item_credits_a_candidate_with_real_evidence(
        self, client, conn
    ):
        anchor, loud_bottle = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "Want Anchor"},
            {"bucket": "want", "entity_type": "performance", "value": "strong projection"},
        ]).json()

        assert body["interpreted_preferences"]["target"] == ["strong projection"]
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert loud_bottle in by_id
        assert _status_by_value(by_id[loud_bottle])["strong projection"] == "matched"
        assert_honest(body)


# --- the full composition --------------------------------------------------


class TestFullComposition:
    """`Side Effect + vanilla + strong projection / warm reduce + saffron
    / feminine + date night + <=250 budget` — every chip type the
    composer maps, composed together against a real corpus.
    """

    def _seeded(self, conn):
        anchor = add_fragrance(
            conn, "Full Composition Anchor", aliases=["Composition Anchor"]
        )

        # Matches everything: connected to the anchor, real vanilla,
        # projection, feminine and date-night evidence, priced under
        # budget, and — deliberately — no warm and no saffron evidence at
        # all, to prove those two items are neither credited nor blamed
        # for what nobody has said.
        good = add_fragrance(conn, "Full Composition Good Match")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=good, author=author, channel=f"s{i}")
        for i, author in enumerate(["v1", "v2"]):
            note(conn, 10 + i, frag=good, value="vanilla", author=author, channel=f"v{i}")
        for i, author in enumerate(["p1", "p2", "p3"]):
            _claim(conn, 20 + i, frag=good, claim_type="PROJECTION", value=None,
                   author=author, channel=f"p{i}", sentiment="POSITIVE")
        for i, author in enumerate(["f1", "f2"]):
            note(conn, 30 + i, frag=good, value="feminine", author=author,
                 channel=f"f{i}", claim_type="AESTHETIC")
        for i, author in enumerate(["o1", "o2", "o3"]):
            note(conn, 40 + i, frag=good, value="date night", author=author,
                 channel=f"o{i}", claim_type="OCCASION")
        _price(conn, good, "good-1", 200.0)

        # Same evidence, but priced over budget — proves the budget item
        # really filters within a full composition, not only alone.
        over_budget = add_fragrance(conn, "Full Composition Over Budget")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, 50 + i, anchor=anchor, other=over_budget, author=author,
                     channel=f"ob{i}")
        for i, author in enumerate(["v1", "v2"]):
            note(conn, 60 + i, frag=over_budget, value="vanilla", author=author,
                 channel=f"obv{i}")
        _price(conn, over_budget, "over-1", 999.0)

        # Real saffron evidence — proves the exclude item really excludes
        # within a full composition too.
        saffron = add_fragrance(conn, "Full Composition Saffron")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, 70 + i, anchor=anchor, other=saffron, author=author,
                     channel=f"pw{i}")
        for i, author in enumerate(["w1", "w2"]):
            note(conn, 80 + i, frag=saffron, value="saffron", author=author,
                 channel=f"pwn{i}")
        _price(conn, saffron, "pow-1", 100.0)

        return anchor, good, over_budget, saffron

    def test_every_chip_type_compiles_and_the_real_corpus_answers_honestly(
        self, client, conn
    ):
        anchor, good, over_budget, saffron = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "Composition Anchor"},
            {"bucket": "want", "entity_type": "note", "value": "vanilla"},
            {"bucket": "want", "entity_type": "performance", "value": "strong projection"},
            {"bucket": "avoid", "entity_type": "scent_characteristic", "value": "warm",
             "mode": "reduce"},
            {"bucket": "avoid", "entity_type": "note", "value": "saffron"},
            {"bucket": "want", "entity_type": "scent_characteristic", "value": "feminine"},
            {"bucket": "want", "entity_type": "occasion", "value": "date night"},
            {"bucket": "want", "entity_type": "budget", "operator": "<=", "amount": 250},
        ]).json()

        interpreted = body["interpreted_preferences"]
        assert interpreted["anchors"] == ["Full Composition Anchor"]
        assert set(interpreted["target"]) == {"vanilla", "strong projection", "feminine"}
        assert interpreted["reduce"] == ["warm"]
        assert interpreted["exclude"] == ["saffron"]
        assert "budget <= $250" in interpreted["constraints"]
        assert "occasion: date night" in interpreted["constraints"]

        ids = {r["fragrance_id"] for r in body["results"]}
        assert anchor not in ids
        assert good in ids
        assert over_budget not in ids, "real evidence does not buy a way past budget"
        assert saffron not in ids, "real saffron evidence must hard-exclude"
        # Every candidate returned respects the $250 budget or carries no
        # price at all — never a known price above it.
        assert body["unexpressed"] == []

        by_id = {r["fragrance_id"]: r for r in body["results"]}
        statuses = _status_by_value(by_id[good])
        assert statuses["vanilla"] == "matched"
        assert statuses["strong projection"] == "matched"
        assert statuses["feminine"] == "matched"
        assert statuses["date night"] == "matched"
        # MISSING DATA: nobody said anything about warmth or powderiness
        # for this bottle, so neither is credited or blamed.
        assert statuses["warm"] == "unknown"
        assert statuses["saffron"] == "unknown"
        assert_honest(body)


# --- no anchor / only wants -------------------------------------------------


class TestNoAnchorOnlyWants:
    def _seeded(self, conn):
        vanilla_bottle = add_fragrance(conn, "Vanilla Only Bottle")
        for i, author in enumerate(["v1", "v2"]):
            note(conn, i, frag=vanilla_bottle, value="vanilla", author=author,
                 channel=f"v{i}")
        return vanilla_bottle

    def test_want_items_alone_compile_and_match_with_no_anchor_at_all(self, client, conn):
        vanilla_bottle = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "want", "entity_type": "note", "value": "vanilla"},
        ]).json()

        assert body["interpreted_preferences"]["anchors"] == []
        assert body["interpreted_preferences"]["target"] == ["vanilla"]
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert vanilla_bottle in by_id
        assert _status_by_value(by_id[vanilla_bottle])["vanilla"] == "matched"
        assert_honest(body)


# --- multiple anchors --------------------------------------------------


class TestMultipleAnchorsHeld:
    def _seeded(self, conn):
        first = add_fragrance(conn, "Multi Anchor Delina", aliases=["MADelina"])
        second = add_fragrance(conn, "Multi Anchor Lira", aliases=["MALira"])
        return first, second

    def test_the_second_liked_fragrance_is_held_in_unexpressed_not_dropped(
        self, client, conn
    ):
        first, second = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "MADelina"},
            {"bucket": "like", "entity_type": "fragrance", "value": "MALira"},
        ]).json()

        assert body["state"]["items"][0]["value"] == "Multi Anchor Delina"
        assert body["state"]["items"][1]["value"] == "Multi Anchor Lira"
        assert body["unexpressed"] == [{
            "preference": "liked bottle: Multi Anchor Lira",
            "reason": "multiple anchors not yet ranked",
        }]
        # Both are still named as anchors in the read-back — nothing
        # about the second bottle is dropped, only its ranking role.
        assert body["interpreted_preferences"]["anchors"] == [
            "Multi Anchor Delina", "Multi Anchor Lira",
        ]
        assert_honest(body)


# --- avoid fragrance excluded -------------------------------------------


class TestAvoidFragranceExcluded:
    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Avoid Fragrance Anchor", aliases=["AvoidAnchor"])
        avoided = add_fragrance(conn, "Avoided Neighbour Bottle")
        kept = add_fragrance(conn, "Kept Neighbour Bottle")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=avoided, author=author, channel=f"a{i}")
        for i, author in enumerate(["d1", "d2", "d3"]):
            _similar(conn, 10 + i, anchor=anchor, other=kept, author=author, channel=f"k{i}")
        return anchor, avoided, kept

    def test_an_avoided_fragrance_never_appears_in_results(self, client, conn):
        anchor, avoided, kept = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "AvoidAnchor"},
            {"bucket": "avoid", "entity_type": "fragrance", "value": "Avoided Neighbour Bottle"},
        ]).json()

        ids = {r["fragrance_id"] for r in body["results"]}
        assert avoided not in ids
        assert kept in ids
        assert_honest(body)


# --- MISSING DATA ------------------------------------------------------


class TestMissingDataNeverCountsAsSatisfying:
    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Missing Data Anchor", aliases=["MDAnchor"])
        # Real evidence for a completely unrelated axis, so this bottle
        # is a candidate at all — but nothing about warmth or projection
        # is ever said about it.
        blank = add_fragrance(conn, "Missing Data Blank Bottle")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=blank, author=author, channel=f"m{i}")
        note(conn, 20, frag=blank, value="lemon", author="l1", channel="lem")
        return anchor, blank

    def test_avoid_warm_gives_no_credit_when_nobody_said_anything_about_warmth(
        self, client, conn
    ):
        anchor, blank = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "MDAnchor"},
            {"bucket": "avoid", "entity_type": "scent_characteristic", "value": "warm"},
        ]).json()
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert blank in by_id
        assert _status_by_value(by_id[blank])["warm"] == "unknown"
        assert_honest(body)

    def test_want_strong_projection_gives_no_credit_when_nobody_said_anything(
        self, client, conn
    ):
        anchor, blank = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "MDAnchor"},
            {"bucket": "want", "entity_type": "performance", "value": "strong projection"},
        ]).json()
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert blank in by_id
        assert _status_by_value(by_id[blank])["strong projection"] == "unknown"
        assert_honest(body)

    def test_budget_gives_no_credit_and_no_exclusion_when_nobody_priced_it(
        self, client, conn
    ):
        anchor, blank = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "MDAnchor"},
            {"bucket": "want", "entity_type": "budget", "operator": "<=", "amount": 50},
        ]).json()
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert blank in by_id, "an unpriced candidate must survive a budget filter"
        assert _status_by_value(by_id[blank])["budget"] == "unknown"
        assert_honest(body)


# --- budget really filters ----------------------------------------------


class TestBudgetReallyFilters:
    def _seeded(self, conn):
        cheap = add_fragrance(conn, "Budget Cheap Bottle")
        for i, author in enumerate(["c1", "c2"]):
            note(conn, i, frag=cheap, value="citrus", author=author, channel=f"c{i}")
        _price(conn, cheap, "cheap-1", 40.0)

        pricey = add_fragrance(conn, "Budget Pricey Bottle")
        for i, author in enumerate(["p1", "p2"]):
            note(conn, 10 + i, frag=pricey, value="citrus", author=author, channel=f"p{i}")
        _price(conn, pricey, "pricey-1", 500.0)
        return cheap, pricey

    def test_priced_over_budget_excluded_priced_under_kept(self, client, conn):
        cheap, pricey = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "want", "entity_type": "note", "value": "citrus"},
            {"bucket": "want", "entity_type": "budget", "operator": "<=", "amount": 100},
        ]).json()

        ids = {r["fragrance_id"] for r in body["results"]}
        assert cheap in ids
        assert pricey not in ids
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert _status_by_value(by_id[cheap])["budget"] == "matched"
        assert_honest(body)


# --- invalid item: 400, session not poisoned -----------------------------


class TestInvalidComposeItemDoesNotPoisonTheSession:
    def test_an_unknown_bucket_400s_and_records_nothing(self, client, conn):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/compose",
            json={"preferences": [
                {"bucket": "sort-of-like", "entity_type": "note", "value": "rose"},
            ]},
        )
        assert resp.status_code == 400
        errors = resp.json()["detail"]["errors"]
        assert errors[0]["index"] == 0
        assert "bucket" in errors[0]["error"]

        # The session is alive and empty — nothing was recorded.
        follow_up = client.get(f"/api/session/{session_id}")
        assert follow_up.status_code == 200
        assert follow_up.json()["state"]["items"] == []

        # It still accepts a good item afterward.
        good = client.post(
            f"/api/session/{session_id}/compose",
            json={"preferences": [
                {"bucket": "want", "entity_type": "note", "value": "rose"},
            ]},
        )
        assert good.status_code == 200
        assert good.json()["state"]["items"][0]["value"] == "rose"

    def test_one_bad_item_in_a_batch_rejects_the_whole_batch(self, client, conn):
        session_id = _create(client)
        resp = client.post(
            f"/api/session/{session_id}/compose",
            json={"preferences": [
                {"bucket": "want", "entity_type": "note", "value": "rose"},
                {"bucket": "want", "entity_type": "budget", "operator": ">=", "amount": 10},
            ]},
        )
        assert resp.status_code == 400
        follow_up = client.get(f"/api/session/{session_id}")
        assert follow_up.json()["state"]["items"] == [], (
            "a batch with any invalid item must record none of it"
        )


# --- event-sourced rebuild -----------------------------------------------


class TestEventSourcedRebuildReproducesChipState:
    def test_rebuild_from_session_events_matches_the_live_active_items(self, client, conn):
        vanilla_bottle = add_fragrance(conn, "Rebuild Vanilla Bottle")
        session_id = _create(client)
        _compose(client, session_id, [
            {"bucket": "want", "entity_type": "note", "value": "vanilla"},
            {"bucket": "want", "entity_type": "occasion", "value": "wedding"},
        ])
        remove_resp = client.delete(f"/api/session/{session_id}/compose/0")
        assert remove_resp.status_code == 200
        assert remove_resp.json()["state"]["items"] == [{
            "index": 1, "bucket": "want", "entity_type": "occasion",
            "value": "wedding", "mode": None, "operator": None, "amount": None,
        }]

        rows = conn.execute(
            "SELECT kind, payload FROM session_events WHERE session_id = %s ORDER BY id",
            (session_id,),
        ).fetchall()
        events = [
            (
                r["kind"],
                r["payload"] if isinstance(r["payload"], dict)
                else json.loads(r["payload"]),
            )
            for r in rows
        ]
        rebuilt = rebuild(events, conn)
        active = [
            (i, item.bucket, item.entity_type, item.value)
            for i, item in rebuilt.active_items
        ]
        assert active == [(1, "want", "occasion", "wedding")]
        del vanilla_bottle  # fixture only needed to prove the catalogue exists


# --- ranking tie-break: matched WANT beats matched LIKE, both beat neither --


class TestComposerTiebreakByMatchedWant:
    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Tiebreak Anchor", aliases=["TBAnchor"])
        strong = add_fragrance(conn, "Tiebreak Strong Match")
        weak = add_fragrance(conn, "Tiebreak Weak Match")
        # Both connected to the anchor with an *identical* connection
        # strength, so nothing but the composer tie-break could separate
        # them.
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, i, anchor=anchor, other=strong, author=author, channel=f"s{i}")
        for i, author in enumerate(["c1", "c2", "c3"]):
            _similar(conn, 10 + i, anchor=anchor, other=weak, author=author, channel=f"w{i}")
        for i, author in enumerate(["v1", "v2"]):
            note(conn, 20 + i, frag=strong, value="vanilla", author=author, channel=f"v{i}")
        return anchor, strong, weak

    def test_a_candidate_matching_more_want_items_outranks_an_equal_evidence_peer(
        self, client, conn
    ):
        anchor, strong, weak = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "TBAnchor"},
            {"bucket": "want", "entity_type": "note", "value": "vanilla"},
        ]).json()

        ids = [r["fragrance_id"] for r in body["results"]]
        assert strong in ids and weak in ids
        assert ids.index(strong) < ids.index(weak)
        assert_honest(body)


# --- vocabulary endpoint --------------------------------------------------


class TestVocabularyEndpoint:
    def _seeded(self, conn):
        # "vocabrare" seen on 2 bottles -- below the floor.
        for n, name in enumerate(["Vocab Rare A", "Vocab Rare B"]):
            frag = add_fragrance(conn, name)
            note(conn, n, frag=frag, value="vocabrare", author=f"r{n}", channel=f"rr{n}")
        # "vocabcommon" seen on 3 bottles -- at the floor.
        for n, name in enumerate(["Vocab Common A", "Vocab Common B", "Vocab Common C"]):
            frag = add_fragrance(conn, name)
            note(conn, 10 + n, frag=frag, value="vocabcommon", author=f"c{n}",
                 channel=f"cc{n}")

    def test_a_value_below_the_bottle_floor_is_absent_a_value_at_it_is_present(
        self, client, conn
    ):
        self._seeded(conn)
        resp = client.get("/api/vocabulary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["min_bottles"] == 3

        note_values = {e["value"]: e["bottles"] for e in body["notes"]}
        assert "vocabrare" not in note_values
        assert note_values.get("vocabcommon") == 3
        assert_honest(body)


class TestAContradictedAvoidIsVisibleNotOnlyMachineReadable:
    """The lineage of this test is the product's founding bug, twice.

    First the kiosk recommended Khamrah to a shopper avoiding warm with
    the warm evidence filed under "why it fits" (the TOO_X parser fix).
    Then the composer carried `warm -> contradicted` in
    `preference_status` while the card's caveats stayed empty — the same
    evidence, invisible again, one layer deeper. A contradicted avoid
    must surface as a caveat, in the engine's own audited wording.
    """

    def _seeded(self, conn):
        anchor = add_fragrance(conn, "Caveat Anchor", aliases=["CavAnchor"])
        warm_bottle = add_fragrance(conn, "Warm Anyway Bottle")
        for i, author in enumerate(["s1", "s2", "s3"]):
            _similar(conn, i, anchor=anchor, other=warm_bottle,
                     author=author, channel=f"cv{i}")
        # AESTHETIC, not NOTE_DESCRIPTOR: "warm" as a chip classifies to
        # the vibe attribute (plan's vocabulary), so the contradiction
        # must live on a vibe fact to be found — exactly where Khamrah's
        # real "rich warm addictive" evidence lives.
        note(conn, 30, frag=warm_bottle, value="warm", author="w1",
             channel="wc", claim_type="AESTHETIC")
        return anchor, warm_bottle

    def test_the_warm_evidence_lands_in_caveats_with_contradicted_status(
        self, client, conn
    ):
        anchor, warm_bottle = self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "CavAnchor"},
            {"bucket": "avoid", "entity_type": "scent_characteristic",
             "value": "warm"},
        ]).json()
        by_id = {r["fragrance_id"]: r for r in body["results"]}
        assert warm_bottle in by_id
        card = by_id[warm_bottle]
        assert _status_by_value(card)["warm"] == "contradicted"
        assert any("warm" in c["text"] for c in card["caveats"]), (
            "contradicted avoid evidence must be visible in caveats, "
            "not only in preference_status"
        )
        assert all("warm" not in r["text"] for r in card["reasons"]), (
            "the avoided attribute may never appear as a reason"
        )
        assert_honest(body)

    def test_a_budget_status_row_names_its_amount(self, client, conn):
        """An empty-string value rendered as a nameless chip in live
        verification; the row must say what the constraint is."""
        self._seeded(conn)
        session_id = _create(client)
        body = _compose(client, session_id, [
            {"bucket": "like", "entity_type": "fragrance", "value": "CavAnchor"},
            {"bucket": "want", "entity_type": "budget",
             "operator": "<=", "amount": 250},
        ]).json()
        for r in body["results"]:
            budget_rows = [s for s in r["preference_status"]
                           if s["entity_type"] == "budget"]
            assert budget_rows and budget_rows[0]["display"] == "under $250"
        assert_honest(body)
