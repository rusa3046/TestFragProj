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
        """Fetch targets are template literals over the session id; reduce
        them to path shapes and demand each matches a registered route."""
        targets = set(re.findall(r"""(?:fetch|api)\(\s*[`"']([^`"']+)""", HTML))
        targets = {t for t in targets if t.startswith("/api")}
        assert targets, "the page calls no API at all — the contract test is dead"
        route_paths = {r.path for r in app.routes}
        for target in targets:
            shape = (target
                     .replace("${sid}", "X").replace("${fragranceId}", "1"))
            shape = re.sub(r"/X(?=/|$)", "/{session_id}", shape)
            shape = re.sub(r"/1(?=/|$)", "/{fragrance_id}", shape)
            assert shape in route_paths, (
                f"the page calls {target!r} but the API has no such route"
            )

    def test_every_chip_phrase_parses_without_refusal_or_unparsed(self, conn):
        """A chip is a promise that tapping it does something. Every
        data-say phrase must compile: no refusal, nothing unparsed —
        the dead-filter class of defect, pinned at the vocabulary level."""
        from fragrance_graph.plan import parse

        phrases = re.findall(r'data-say="([^"]+)"', HTML)
        assert len(phrases) >= 15, "chips went missing from the page"
        for phrase in phrases:
            plan = parse(None, phrase.replace("&amp;", "&"))
            assert not plan.refusal, f"chip {phrase!r} is refused by the parser"
            assert not plan.unparsed, f"chip {phrase!r} lands in unparsed"

    def test_labels_in_js_match_the_apis_four(self):
        for label in ("BEST_MATCH", "STRONG_MATCH", "WORTH_TRYING",
                      "ALTERNATIVE_DIRECTION"):
            assert label in HTML

    def test_no_innerhtml_anywhere(self):
        assert "innerHTML" not in HTML, (
            "API text must be rendered via textContent; innerHTML turns a "
            "hostile bottle name into markup"
        )

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
        handler = HTML.split("async function handleSay", 1)[1].split(
            "document.addEventListener", 1
        )[0]
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
