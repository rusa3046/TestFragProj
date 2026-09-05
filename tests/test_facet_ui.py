"""The FACET kiosk page: chrome that composes nothing.

Three contracts pinned here:

1. **Every endpoint the page calls exists.** The JS's fetch targets are
   read out of the shipped HTML and checked against the app's real
   routes, so the page and the API cannot drift apart silently.
2. **Every chip phrase parses.** The six-case suite proved a chip
   sending a bare attribute word ("longevity") compiles to a filter no
   fact can satisfy — a dead filter behind a friendly button. So every
   `data-say` phrase shipped in the HTML must go through the real parser
   without refusal and without landing in `unparsed`.
3. **The page renders API text as text.** The JS builds DOM via
   `textContent` (asserted the cheap way: no `innerHTML` assignment
   anywhere in the file), so a hostile bottle name arriving from the API
   stays words, exactly as the site pages already guarantee.
"""

import re
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from fragrance_graph.api import app, get_conn  # noqa: E402

HTML = (
    Path("src/fragrance_graph/facet/static/index.html")
    .read_text(encoding="utf-8")
)


def _next_function_body(html: str, marker: str) -> str:
    """Everything after `marker` up to (not including) the next top-level
    `function`/`async function` declaration — a boundary that survives
    reordering the file's functions, unlike splitting on a specific
    following line, which several of this module's wiring pins used to
    do before Composer Phase B moved things around."""
    after = html.split(marker, 1)[1]
    return re.split(r"\nasync function |\nfunction ", after, maxsplit=1)[0]


@pytest.fixture
def client(conn):
    def _override():
        yield conn

    app.dependency_overrides[get_conn] = _override
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


class TestTheKioskIsServed:
    def test_root_serves_the_kiosk(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "FACET" in resp.text
        assert "Spray these first" in resp.text

    def test_the_kiosk_and_the_shipped_file_are_the_same_bytes(self, client):
        assert client.get("/").text == HTML


class TestThePageAndTheApiCannotDrift:
    def test_every_fetch_target_is_a_real_route(self):
        """Fetch targets are template literals over the session id (or,
        for `/api/search`, a query string); reduce them to path shapes
        and demand each matches a registered route.

        The `?query=...` stripping is new in Composer Phase B, which
        added the page's first fetch with a query string
        (`/api/search?q=${...}`) — the same tolerance this test already
        gave `${sid}`/`${fragranceId}` path segments, extended to a query
        suffix rather than weakened: a target still has to resolve to a
        real, registered path once the part that varies per call is
        removed.
        """
        targets = set(re.findall(r"""(?:fetch|api)\(\s*[`"']([^`"']+)""", HTML))
        targets = {t for t in targets if t.startswith("/api")}
        assert targets, "the page calls no API at all — the contract test is dead"
        route_paths = {r.path for r in app.routes}
        for target in targets:
            shape = target.split("?", 1)[0]
            shape = (shape
                     .replace("${sid}", "X").replace("${fragranceId}", "1")
                     .replace("${index}", "7").replace("${candidate.fragrance_id}", "1"))
            shape = re.sub(r"/X(?=/|$)", "/{session_id}", shape)
            shape = re.sub(r"/1(?=/|$)", "/{fragrance_id}", shape)
            shape = re.sub(r"/7(?=/|$)", "/{index}", shape)
            assert shape in route_paths, (
                f"the page calls {target!r} but the API has no such route"
            )

    #: Words a shopper's chip could plausibly name, that must never be
    #: baked into the shipped page — every composer chip comes from
    #: GET /api/vocabulary (notes/vibes/performance/occasions) or
    #: GET /api/search (fragrances) at runtime. A future PR that adds a
    #: convenience `<button>Vanilla</button>` instead of wiring it
    #: through vocabulary must fail this test.
    _CANARY_ATTRIBUTE_WORDS = (
        "vanilla", "sandalwood", "bergamot", "patchouli", "feminine",
        "masculine", "vetiver", "cedarwood",
    )

    def test_no_hardcoded_attribute_chip_values(self):
        """Composer Phase B replaced the kiosk's `data-say` vibe/attribute
        tiles (Phase A's `test_every_chip_phrase_parses_without_refusal_
        or_unparsed`, retired with this test) with vocabulary-driven
        chips: nothing that names a note, characteristic, vibe,
        performance value or occasion may be a literal string in the
        shipped page — only `GET /api/vocabulary`'s own response
        populates those chips at runtime. Budget is the sole, spec-named
        exception (four fixed dollar presets are UI-constructed, not
        vocabulary), so budget numbers are not canaries here."""
        assert "data-say" not in HTML, (
            "a data-say chip reappeared — Composer Phase B chips must go "
            "through /compose, never the free-text parser"
        )
        lowered = HTML.lower()
        for word in self._CANARY_ATTRIBUTE_WORDS:
            assert word not in lowered, (
                f"{word!r} appears in the shipped HTML — composer chips "
                "must be vocabulary-driven, never hardcoded"
            )

    def test_drawer_category_set_per_bucket_matches_spec(self):
        """Fragrance is a like/avoid anchor, never a want; budget is a
        want only. Parsed out of the live `CATEGORY_BY_BUCKET` constant
        so a future edit that quietly adds "budget" to "like" fails here
        rather than only in a manual click-through."""
        match = re.search(r"CATEGORY_BY_BUCKET\s*=\s*\{(.*?)\n\};", HTML, re.S)
        assert match, "CATEGORY_BY_BUCKET constant not found in the page"
        block = match.group(1)

        def categories_for(bucket: str) -> set[str]:
            row = re.search(rf"\b{bucket}:\s*\[(.*?)\]", block)
            assert row, f"no category list for bucket {bucket!r}"
            return set(re.findall(r'"([a-z_]+)"', row.group(1)))

        assert categories_for("like") == {
            "fragrance", "note", "scent_characteristic", "performance", "vibe",
        }
        assert categories_for("avoid") == {"fragrance", "note", "scent_characteristic"}
        assert categories_for("want") == {
            "note", "scent_characteristic", "vibe", "performance", "occasion", "budget",
        }
        assert "budget" not in categories_for("like")
        assert "budget" not in categories_for("avoid")

    def test_the_readback_panel_rerenders_on_every_chip_add(self):
        """Design mockup 1a: the "how FACET reads this" rail is live, not
        only refreshed at Find My Matches — it must re-render inside the
        same function that adds a chip, the wiring-pin style
        `test_the_start_box_advances_to_the_results_it_computed` already
        uses for the say box."""
        body = _next_function_body(HTML, "async function composeMany")
        assert "renderBuckets" in body
        assert "renderReadback" in body

    def test_find_my_matches_advances_to_the_results_it_computed(self):
        """The composer's primary CTA must not be a silent button — the
        same lesson `sayThenShow` already carries, applied to the new
        primary action."""
        assert "findMatches" in HTML.split('$("#find-matches")', 1)[1].split(";", 1)[0]
        definition = _next_function_body(HTML, "async function findMatches")
        assert 'show("results")' in definition
        assert "refreshQueue" in definition

    def test_bottle_detail_has_three_blocks_in_order_with_disclaimer(self):
        """Design mockup 1b: evidence, then this bottle's own chip
        statuses, then the official/declared block — in that order —
        ending in the mockup's fixed disclaimer sentence."""
        body = _next_function_body(HTML, "async function loadDetail")
        evidence_at = body.index('"Evidence"')
        chips_at = body.index('"Your chips"')
        official_at = body.index("what the house declares")
        assert evidence_at < chips_at < official_at
        assert (
            "Declared notes are the house's own list. "
            "They never count toward the evidence above."
        ) in body

    def test_empty_state_renders_the_api_note_verbatim(self):
        """Design mockup 1c: a chrome heading, but the explanation under
        it must be the API's own `note`, verbatim — never a page-authored
        substitute when the API actually said something. The heading
        itself carries no internal-audit vocabulary ("bar"/"threshold"/
        "gate"/"independence"/"consensus") — `commerce-audit.md` §7/§1's
        fix, extended to the one piece of static chrome copy that used to
        echo it ("clears the bar")."""
        assert "Nothing matches this combination yet." in HTML
        for jargon in ("clears the bar", "independence", "threshold", "consensus"):
            assert jargon not in HTML.lower()
        body = _next_function_body(HTML, "function renderResultsState")
        assert 'emptyNote.textContent = body.note' in body

    def test_labels_in_js_match_the_apis_five(self):
        """Catalog-first spec: the two-axis label vocabulary
        (`commerce_card.result_tier`'s docstring) replaces the old
        single-axis `STRONG_FIT`/`GOOD_FIT`/`PARTIAL_FIT`/
        `EXPLORATORY_PICK` four, plus `CLOSEST_AVAILABLE` for the card
        that answered nothing the shopper asked."""
        for label in ("BEST_OVERALL_FIT", "STRONG_PROFILE_FIT",
                      "COMMUNITY_FAVORITE", "WORTH_DISCOVERING",
                      "CLOSEST_AVAILABLE"):
            assert label in HTML

    def test_no_innerhtml_anywhere(self):
        assert "innerHTML" not in HTML, (
            "API text must be rendered via textContent; innerHTML turns a "
            "hostile bottle name into markup"
        )

    def test_no_internal_terminology_strings_in_the_html(self):
        """Spec §7/§26: the customer surface never shows independence
        bars, thresholds, gates or consensus terminology — checked here
        the same way `audit._audit_commerce` checks the API's own
        composed sentences, extended to the shipped page's own chrome
        text (headings, labels) rather than only its API-sourced content."""
        lowered = HTML.lower()
        for phrase in ("independence", "threshold", "consensus", "clears the bar"):
            assert phrase not in lowered, f"internal terminology {phrase!r} leaked into the HTML"

    def test_commerce_sections_count_from_the_same_filtered_list_they_render(self):
        """§36's fix, pinned at the UI layer: `commerceSection` must build
        its `(N)` count from the identical array it iterates to build
        `<li>`s — not `candidate.reasons.length` (the raw, unfiltered
        payload count the old, buggy version used) against a separately
        filtered render loop. Both `shown.length` (the count) and
        `shown.forEach` (the render loop) reading the *same* `shown`
        binding is what makes the two structurally unable to disagree."""
        body = _next_function_body(HTML, "function commerceSection")
        assert "shown.length" in body
        assert "shown.forEach" in body
        # And the array itself is always the filtered one, never the raw
        # `items` parameter rendered directly.
        assert "nonEmptyStrings(items)" in body

    def test_responsive_layout_rule_present(self):
        """Spec: kiosk width (<1024px) is untouched; >=1024px widens
        `main` and lays the composer buckets and the "how FACET reads
        this" rail out side by side (mockup 1a's original side-panel
        design), with results as a two-column grid."""
        assert "@media (min-width: 1024px)" in HTML
        wide = HTML.split("@media (min-width: 1024px)", 1)[1]
        assert "#buckets-col" in wide and "#readback" in wide
        assert "grid-template-columns" in wide

    def test_no_numeric_match_score_is_rendered(self):
        """The UI shows labels, never percentages. A '% match' string in
        the page would mean someone started faking calibration."""
        assert not re.search(r"%\s*match", HTML, re.I)

    def test_the_start_box_advances_to_the_results_it_computed(self):
        """The first shipped kiosk had a working endpoint behind a silent
        button: 'Tell FACET' updated the session (HTTP 200, five results
        ready) and changed nothing on screen, because only the results
        screen's refine box carried an after-action. Silent success reads
        as broken — the user reported it as exactly that. The start box
        must advance to the results it just computed."""
        assert "sayThenShow" in HTML.split('$("#say-go")', 1)[1].split(";", 1)[0], (
            "the start box's click handler no longer routes through "
            "sayThenShow"
        )
        definition = HTML.split("const sayThenShow", 1)[1].split(
            '$("#say-go")', 1
        )[0]
        assert 'show("results")' in definition
        assert "refreshQueue" in definition

    def test_a_failed_say_is_visible_not_console_only(self):
        """fetch failures inside handleSay must surface on the page; a
        console-only error is indistinguishable from a dead button."""
        handler = _next_function_body(HTML, "async function handleSay")
        assert "catch" in handler
        assert "renderNote(" in handler.split("catch", 1)[1]


class TestTheKioskFlowsEndToEnd:
    def test_a_chip_a_sentence_and_a_queue(self, client, conn):
        """The full self-serve loop through the real endpoints the page
        uses, in the order the page calls them."""
        from fragrance_graph.resolve.entities import add_fragrance
        from tests.test_recommend import note

        fid = add_fragrance(conn, "Lattafa Khamrah")
        for i, (author, channel) in enumerate(
            [("a", "c1"), ("b", "c2"), ("c", "c3")]
        ):
            note(conn, 100 + i, frag=fid, value="warm",
                 author=author, channel=channel)

        sid = client.post("/api/session", json={"mode": "self_serve"}).json()["session_id"]
        client.post(f"/api/session/{sid}/say", json={"text": "warm and cozy"})
        client.post(f"/api/session/{sid}/prefs", json={"budget_usd": 100})
        body = client.get(f"/api/session/{sid}/spray-queue").json()
        assert "queue" in body
        assert any(u["preference"] == "budget" for u in body.get("unexpressed", []))


class TestTheComposerFlowsEndToEnd:
    """Composer Phase B, exercised through the same TestClient path the
    page's drawer/bucket JS actually calls — the endpoints, not the DOM,
    since jsdom is not part of this suite's toolchain; the static-analysis
    tests above pin that the JS wires those endpoints correctly."""

    def test_add_chip_state_has_it_results_render_remove_it_gone(self, client, conn):
        """Add chip -> state has it -> results render -> remove -> gone,
        in the order the composer's "+ Add" chip and its "×" both call."""
        from fragrance_graph.resolve.entities import add_fragrance
        from tests.test_recommend import note as _note

        fid = add_fragrance(conn, "Composer Roundtrip Rose")
        for i, (author, channel) in enumerate([("a", "c1"), ("b", "c2"), ("c", "c3")]):
            _note(conn, 300 + i, frag=fid, value="rose", author=author, channel=channel)

        sid = client.post("/api/session", json={"mode": "self_serve"}).json()["session_id"]
        added = client.post(f"/api/session/{sid}/compose", json={
            "preferences": [{"bucket": "like", "entity_type": "note", "value": "rose"}],
        }).json()
        assert any(it["value"] == "rose" for it in added["state"]["items"])
        assert added["interpreted_preferences"]["preserve"] == ["rose"]
        assert any(c["fragrance_id"] == fid for c in added["results"])

        index = next(it["index"] for it in added["state"]["items"] if it["value"] == "rose")
        removed = client.delete(f"/api/session/{sid}/compose/{index}").json()
        assert removed["state"]["items"] == []
        assert removed["interpreted_preferences"]["preserve"] == []

    def test_edit_preferences_preserves_session(self, client, conn):
        """"Edit preferences" is a screen switch, not a new session: a
        chip added, then read back through plain GET (the round trip
        "Edit preferences" relies on), must still be there."""
        from fragrance_graph.resolve.entities import add_fragrance

        fid = add_fragrance(conn, "Preserved Session Bottle")
        sid = client.post("/api/session", json={"mode": "self_serve"}).json()["session_id"]
        client.post(f"/api/session/{sid}/compose", json={
            "preferences": [
                {"bucket": "like", "entity_type": "fragrance", "value": "Preserved Session Bottle"},
            ],
        })

        again = client.get(f"/api/session/{sid}").json()
        assert any(
            it["entity_type"] == "fragrance" and it["value"] == "Preserved Session Bottle"
            for it in again["state"]["items"]
        )
        # GET carries the identical read-back a compose response would —
        # the fix that let "Edit preferences" round-trip without the
        # composer's read-back panel going blank on the way back.
        assert again["interpreted_preferences"]["anchors"] == ["Preserved Session Bottle"]
        assert fid  # the bottle really exists; not asserting on a typo

    def test_status_strip_renders_all_three_statuses(self, client, conn):
        """One candidate carrying `matched`, `contradicted` and `unknown`
        simultaneously — the fixture `_preference_statuses` is built to
        produce honestly: real supporting evidence for a `want`+note
        (matched), real evidence *for* something actively avoided as a
        soft reduce (contradicted — avoid+vibe defaults to reduce, a
        soft push, so the candidate can still surface), and a `want`
        with no evidence at all for this bottle (unknown, never invented
        as either)."""
        from fragrance_graph.resolve.entities import add_fragrance
        from tests.test_recommend import note as _note

        fid = add_fragrance(conn, "Three Status Bottle")
        for i, (author, channel) in enumerate([("a", "c1"), ("b", "c2"), ("c", "c3")]):
            _note(conn, 400 + i, frag=fid, value="rose", author=author, channel=channel)
        for i, (author, channel) in enumerate([("d", "c4"), ("e", "c5"), ("f", "c6")]):
            _note(conn, 410 + i, frag=fid, value="warm", author=author, channel=channel,
                  claim_type="AESTHETIC")

        sid = client.post("/api/session", json={"mode": "self_serve"}).json()["session_id"]
        body = client.post(f"/api/session/{sid}/compose", json={
            "preferences": [
                {"bucket": "want", "entity_type": "note", "value": "rose"},
                {"bucket": "avoid", "entity_type": "vibe", "value": "warm"},
                {"bucket": "want", "entity_type": "vibe", "value": "fresh"},
            ],
        }).json()

        candidate = next(c for c in body["results"] if c["fragrance_id"] == fid)
        statuses = {
            (s["entity_type"], s["value"]): s["status"] for s in candidate["preference_status"]
        }
        assert statuses[("note", "rose")] == "matched"
        assert statuses[("vibe", "warm")] == "contradicted"
        assert statuses[("vibe", "fresh")] == "unknown"

    def test_the_honest_empty_state_surfaces_the_engines_own_note(self, client, conn):
        """When nothing clears the evidence bar, `spray-queue`'s `note`
        is the engine's own refusal wording — the exact string the empty
        state's JS is pinned (statically, above) to render verbatim."""
        sid = client.post("/api/session", json={"mode": "self_serve"}).json()["session_id"]
        client.post(f"/api/session/{sid}/compose", json={
            "preferences": [
                {"bucket": "want", "entity_type": "note", "value": "a-note-nobody-wrote"},
            ],
        })
        body = client.get(f"/api/session/{sid}/spray-queue").json()
        assert body["queue"] == []
        # Not asserting a literal sentence — that belongs to session.py's
        # WORDING, not this test — only that an empty queue still carries
        # *something* for the empty state to show, honestly, rather than
        # a blank the JS would have to paper over with invented copy.
        assert isinstance(body["note"], str)
