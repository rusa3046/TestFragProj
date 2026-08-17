"""The static ask and profile pages.

What these tests protect: the pages may only *carry* what the audited
wording layer produced — never restate it, never strengthen it, and never
let commenter-written text reach a browser unescaped.
"""

from fragrance_graph.ask import (
    QUESTIONS,
    profile_pages,
    render_answer,
    render_profile,
)
from fragrance_graph.evidence import Strength
from fragrance_graph.pages import build
from fragrance_graph.recommend import Answer, Reason, Recommendation


class FakePlan:
    def render(self) -> str:
        return 'intent recommend\nanchor <none>'


def answer_with(*results: Recommendation, note: str = "") -> Answer:
    return Answer(plan=FakePlan(), results=list(results), note=note)


class TestNothingReachesTheBrowserUnescaped:
    def test_markup_in_a_name_is_rendered_not_executed(self):
        hostile = Recommendation(
            fragrance_id=1,
            name='Zara <script>alert(1)</script> Red',
            reasons=[Reason(kind="attribute", text='<b>rose</b> bomb',
                            strength=Strength.OBSERVED, people=1, creators=1)],
            people=1, creators=1,
        )
        page = render_answer("q", answer_with(hostile), head=[])
        assert "<script>" not in page
        assert "&lt;script&gt;" in page
        assert "<b>rose</b>" not in page

    def test_the_plan_dump_is_escaped_too(self):
        class Plan:
            def render(self):
                return "<img src=x onerror=alert(1)>"

        page = render_answer("q", Answer(plan=Plan()), head=[])
        assert "<img" not in page


class TestTheWordingIsCarriedNotRestated:
    def test_a_one_person_fact_arrives_as_one_commenter_said(self):
        """`Reason.phrase()` words a weak fact weakly. The page must ship
        that wording verbatim — a page that re-summarised it as a plain
        assertion would undo the entire provenance discipline at the last
        step."""
        weak = Reason(kind="attribute", text="smells of rose",
                      strength=Strength.OBSERVED, people=1, creators=1)
        candidate = Recommendation(fragrance_id=1, name="X",
                                   reasons=[weak], people=1, creators=1)
        page = render_answer("q", answer_with(candidate), head=[])
        assert weak.phrase() in page.replace("&#x27;", "'")
        assert "one commenter said" in page

    def test_a_refusal_page_shows_the_note_and_no_cards(self):
        page = render_answer(
            "something heavy",
            answer_with(note="Nothing in the corpus supports this request."),
            head=[],
        )
        assert "Nothing in the corpus supports" in page
        assert "<article>" not in page

    def test_a_profile_carries_disagreement_as_disagreement(self):
        contested = Reason(kind="attribute", text="long lasting",
                           strength=Strength.CONTESTED, people=6, creators=3,
                           against=4)
        candidate = Recommendation(fragrance_id=1, name="X",
                                   reasons=[contested], people=10, creators=4)
        page = render_profile("X", answer_with(candidate), head=[])
        assert "4 disagree" in page


class TestTheBuildShipsThem:
    def test_every_curated_question_becomes_a_page_the_index_links(
        self, conn, tmp_path
    ):
        out = tmp_path / "site"
        build(conn, out)
        index = (out / "index.html").read_text(encoding="utf-8")
        for slug, _question in QUESTIONS:
            assert (out / f"ask-{slug}.html").exists()
            assert f"ask-{slug}.html" in index

    def test_an_empty_catalogue_yields_no_profile_pages(self, conn):
        assert profile_pages(conn) == []


class TestTheAuditOwnsTheseSurfaces:
    def test_the_site_surfaces_are_registered_renderers(self):
        """A surface missing from RENDERERS is a surface the audit will
        not notice going silent — the exact failure the registry records."""
        from fragrance_graph.audit import RENDERERS

        assert "site ask pages" in RENDERERS
        assert "site profile pages" in RENDERERS
