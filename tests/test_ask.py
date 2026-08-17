"""The static ask and profile pages.

What these tests protect: the pages may only *carry* what the audited
wording layer produced — never restate it, never strengthen it, and never
let commenter-written text reach a browser unescaped.
"""

from fragrance_graph.ask import (
    QUESTIONS,
    profile_pages,
    profile_slug,
    render_answer,
    render_profile,
)
from fragrance_graph.evidence import Strength
from fragrance_graph.pages import build
from fragrance_graph.recommend import Answer, Reason, Recommendation


def test_profile_slug_folds_accents():
    """`profile_slug` used to run its own regex and produce
    "about-herm-s-eau-d-orange-verte.html" — an accented letter treated
    as punctuation. It now delegates to `pages.slugify`, which folds
    accents first; see that function's docstring for the real page this
    changed and why it is safe to change."""
    assert profile_slug("Hermès Eau d'Orange Verte") == (
        "about-hermes-eau-d-orange-verte.html"
    )


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

    def test_the_digest_is_exactly_what_the_page_ships(self):
        """Not a page that says roughly what `digest()` says — the same
        string, byte for byte once escaped. A page free to reword its own
        summary is a second place the wording could drift from the rule
        `Reason.phrase()` enforces."""
        import html as _html

        from fragrance_graph.recommend import digest

        strong = Reason(kind="prefer", text="raspberry",
                        strength=Strength.SUPPORTED, people=8, creators=4)
        candidate = Recommendation(fragrance_id=1, name="X",
                                   reasons=[strong], people=8, creators=4)
        expected = digest(candidate)
        assert expected  # sanity: this scenario does produce a digest

        page = render_answer("q", answer_with(candidate), head=[])
        assert _html.escape(expected) in page

    def test_the_full_reason_list_is_collapsed_behind_details(self):
        """The digest leads; the full phrase list — what used to be the
        whole card — is still present in the HTML, just behind a
        <details> a reader opens rather than scrolls past."""
        reasons = [
            Reason(kind="prefer", text=f"note-{i}", strength=Strength.SUPPORTED,
                   people=8, creators=4)
            for i in range(5)
        ]
        candidate = Recommendation(fragrance_id=1, name="X", reasons=reasons,
                                   people=8, creators=4)
        page = render_answer("q", answer_with(candidate), head=[])
        assert "<details>" in page
        assert "show the comments" in page
        for r in reasons:
            assert r.phrase() in page


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
