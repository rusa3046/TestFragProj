"""The provenance contract, asserted once against every speaking surface.

This file exists because twenty findings across seven reviews had one
shape: a rule holding where it was applied and failing at the next surface
along. Care at each call site did not work. This is the check that does
not depend on care.
"""

import pytest

from fragrance_graph.audit import (
    FORBIDDEN,
    Violation,
    _check_reason,
    audit,
    discloses,
)
from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evidence import Strength
from fragrance_graph.recommend import Reason
from fragrance_graph.resolve.entities import add_fragrance


@pytest.fixture(scope="module")
def corpus():
    try:
        conn = get_connection(DEFAULT_DB_URL)
    except Exception:  # noqa: BLE001
        pytest.skip("no developer database; run `corpus import` first")
    if conn.execute("SELECT count(*) FROM claims").fetchone()[0] < 100:
        pytest.skip("developer database is empty")
    yield conn
    conn.close()


def _rendered_index_page(conn) -> str:
    """The real `index.html` `pages.build` would write for `conn`, built
    the identical way `audit._audit_rendered_text` builds it — one
    assembly, used by both, so a test driving this function is driving
    exactly what a visitor and the audit both see."""
    from fragrance_graph import ask as _ask
    from fragrance_graph.pages import (
        build_search_index,
        qualifying_pairs,
        render_full_index,
        reverse_indexes,
    )

    pairs = qualifying_pairs(conn)
    indexes = reverse_indexes(pairs)
    profiles = _ask.profile_pages(conn)
    asked = list(_ask.QUESTIONS)
    index_data = build_search_index(conn, pairs, profiles, asked)
    return render_full_index(pairs, indexes, profiles, asked, index_data)


class TestTheContractHolds:
    def test_no_surface_violates_it(self, corpus):
        report = audit(corpus)
        assert report.clean, report.render()

    def test_every_surface_is_actually_exercised(self, corpus):
        """An audit that quietly skips half the product reads the same as
        one that passes."""
        report = audit(corpus)
        assert not report.unexercised, report.unexercised
        for surface in (
            "recommendation", "profile", "comparison", "semantic",
            "structured attributes", "name-derived notes",
            "release lifecycle", "published pages",
        ):
            assert report.checked.get(surface), f"{surface} produced nothing"

    def test_it_checks_a_meaningful_number_of_sentences(self, corpus):
        report = audit(corpus)
        assert sum(report.checked.values()) > 500


class TestTheAuditCatchesWhatItClaims:
    """Each rule, proven against a deliberately bad Reason.

    A checker nobody has seen fail is a checker nobody knows works.
    """

    def _violations(self, reason) -> list[Violation]:
        from fragrance_graph.audit import AuditReport

        report = AuditReport()
        _check_reason(report, "test", reason)
        return report.violations

    def test_it_catches_an_undisclosed_observation(self):
        """`Reason.phrase` is careful enough that it cannot produce this,
        which is the point — so the checker is tested against a surface
        that *is* careless, standing in for the next one somebody writes.
        """
        class Careless(Reason):
            def phrase(self):
                return "smells of rose"

        bad = Careless(kind="prefer", text="rose",
                       strength=Strength.OBSERVED, people=1)
        assert any("observed stated as supported" in v.rule
                   for v in self._violations(bad))

    def test_it_catches_inference_passed_off_as_stated(self):
        bad = Reason(kind="graph", text="3 people compared this with X",
                     strength=Strength.OBSERVED, people=3, creators=2,
                     inferred=True)
        assert any("inferred passed off" in v.rule
                   for v in self._violations(bad))

    def test_it_catches_hidden_disagreement(self):
        bad = Reason(kind="graph", text="3 people across 2 channels said so",
                     strength=Strength.OBSERVED, people=3, creators=2,
                     against=5)
        assert any("denied evidence hidden" in v.rule
                   for v in self._violations(bad))

    def test_it_catches_one_room_sold_as_independence(self):
        bad = Reason(kind="graph", text="3 people across 1 channel",
                     strength=Strength.SUPPORTED, people=3, creators=1)
        assert any("one room" in v.rule for v in self._violations(bad))

    def test_it_catches_a_vector_becoming_a_fact(self):
        bad = Reason(kind="semantic", text="close to 'rosy'",
                     strength=Strength.SUPPORTED, people=1, creators=1)
        assert any("vector similarity" in v.rule
                   for v in self._violations(bad))

    def test_it_catches_forbidden_phrasing(self):
        bad = Reason(kind="graph", text="the community agrees this is a dupe",
                     strength=Strength.SUPPORTED, people=9, creators=4)
        assert any("forbidden" in v.rule for v in self._violations(bad))

    def test_absence_may_never_be_stated_as_negative(self):
        """"It is not rosy" is a claim nobody made. The corpus is a sample
        of 57 creators, not a census."""
        assert "it is not " in FORBIDDEN
        assert "does not contain" in FORBIDDEN

    def test_a_good_reason_produces_nothing(self):
        good = Reason(kind="prefer", text="rose", strength=Strength.SUPPORTED,
                      people=8, creators=4)
        assert self._violations(good) == []


class TestDisclosure:
    def test_a_hedge_discloses(self):
        assert discloses("one commenter said rose")

    def test_explicit_counts_disclose(self):
        assert discloses("8 people across 4 channels")

    def test_a_flat_claim_does_not(self):
        assert not discloses("smells of rose")


class TestCodexFinalFindings:
    """Three findings from the final review, all confirmed."""

    def test_the_audit_reaches_composed_output_not_only_phrases(self, corpus):
        """P2. It checked `Reason.phrase`, where wording is chosen, and
        never the surfaces that compose sentences around those phrases."""
        report = audit(corpus)
        for surface in ("recommendation.explain", "answer.render",
                        "release status", "semantic search"):
            assert report.checked.get(surface), f"{surface} unchecked"

    def test_an_unexercised_surface_is_not_a_pass(self):
        """P2. `clean` ignored `unexercised`, so the CLI exited zero when a
        surface produced nothing to look at. That reads exactly like
        clearing it."""
        from fragrance_graph.audit import AuditReport

        report = AuditReport(checked={"x": 1})
        assert report.clean
        report.unexercised.append("y produced no output")
        assert not report.clean

    def test_a_comparative_reports_counts_not_a_conclusion(self, corpus):
        """P1. "less rose than Delina" is a sentence nobody wrote — it is
        inferred from mention counts, which are a coarse proxy for
        prominence. The evidence is stated; the reader draws the
        comparison."""
        from fragrance_graph.recommend import recommend

        answer = recommend(corpus, "i love Delina but the rose is too strong")
        for candidate in answer.results:
            for reason in candidate.reasons:
                if reason.kind == "comparative":
                    assert "mentioned by" in reason.text
                    assert not reason.text.startswith("less ")


class TestTheSiteIndexIsAuditedToo:
    """The client-side query box's refusal block, and the inline JSON data
    island, are server-rendered precisely so the audit can see them — this
    proves it actually does, against the real renderer rather than a stub.
    """

    def test_the_site_index_page_is_a_registered_renderer(self):
        from fragrance_graph.audit import RENDERERS

        assert "site index page" in RENDERERS

    def test_the_site_index_page_is_checked(self, corpus):
        report = audit(corpus)
        assert report.checked.get("site index page"), "site index page unchecked"

    def test_a_forbidden_phrase_in_a_bottle_name_is_caught_through_the_real_renderer(
        self, conn
    ):
        """Reviewer finding 3: the previous version of this test hardcoded
        `blocks = [("site index page", "the community agrees on rose")]`
        and reimplemented the FORBIDDEN scan inline — it proved only that
        `"the community agrees" in "...the community agrees..."`, never
        that the real pipeline reaches a poisoned name at all.

        This drives the actual path a real name would take: a bottle
        named with forbidden wording, real note evidence so it clears
        `profile_pages`' gate and reaches `build_search_index`'s `bottles`
        list, then the real `audit(conn)` — the same sequence the
        reviewer used by hand against a scratch database — and asserts
        the violation the real audit raises, not one this test computes
        itself.
        """
        from tests.test_recommend import note

        frag = add_fragrance(conn, "the community agrees Parfum")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="rose", author=author,
                 channel=f"c{i}")

        report = audit(conn)
        matches = [
            v for v in report.violations
            if v.surface == "site index page" and "forbidden" in v.rule
        ]
        assert matches, (
            f"no forbidden-phrasing violation on site index page; "
            f"violations were: {report.violations}"
        )
        assert "the community agrees" in matches[0].detail.lower()

    def test_a_hostile_name_cannot_break_out_of_the_inline_json_script(
        self, conn
    ):
        """The blocker: `json.dumps` escapes quotes and backslashes but
        not `</script>`, and a `<script>` element — any `type`, including
        `application/json` — ends at the first literal `</script`
        regardless of JSON syntax. A bottle name or alias containing
        `</script><script>alert(1)</script>` would terminate the data
        island early and the rest would parse as live markup, the same
        class of defect `TestNothingReachesTheBrowserUnescaped` exists to
        catch for every other surface.

        The *canonical name* stays clean here on purpose: `plan.py`'s
        anchor patterns capture from a restricted character class
        (`[a-z0-9'’\\.\\- ]`) that a `</script>`-shaped name cannot survive
        round-tripping through — `profile_pages` would simply never build
        a page for it, which would make this test pass for the wrong
        reason (the hostile string never reaching the payload at all, not
        the escaping holding). The *alias* is where the hostile string
        lives instead — aliases are read straight out of
        `fragrances.aliases` with no re-parse — which is both a realistic
        route (a curator recording an exact quoted spelling, as
        `resolve.entities.add_alias` already does throughout this corpus)
        and the one that actually exercises `build_search_index` /
        `render_query_box`'s escaping. Built through the real
        `render_full_index` against a fixture DB, matching the reviewer's
        own verification method rather than asserting against a hand-built
        string.
        """
        from tests.test_recommend import note

        frag = add_fragrance(
            conn, "Evil Parfum",
            aliases=["</script><script>alert(1)</script>"],
        )
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="rose", author=author,
                 channel=f"c{i}")

        page = _rendered_index_page(conn)
        assert "</script><script>" not in page
        assert "<script>alert" not in page
        # The data must still be present and intact, just inert to the
        # HTML tokenizer — proving this escaped the payload rather than
        # silently dropping the hostile alias from it.
        assert "Evil Parfum" in page
        assert "alert(1)" in page


class TestTheApiResponsesSurfaceIsAudited:
    """`fragrance_graph.api` — FACET's service layer — is only reachable
    with the optional `[api]` extra (FastAPI/uvicorn) installed; a core
    install has neither, and must not need them just to run this audit.
    """

    def test_api_responses_is_a_registered_renderer(self):
        from fragrance_graph.audit import RENDERERS

        assert "api responses" in RENDERERS

    def test_it_is_checked_with_fastapi_installed(self, corpus):
        """FastAPI *is* installed in this environment — this repo's own
        `.venv` carries the `[api]` extra — so the real audit must reach
        it and find nothing to flag, the same as every other surface."""
        report = audit(corpus)
        assert report.checked.get("api responses"), "api responses unchecked"
        assert not [v for v in report.violations if v.surface == "api responses"]
        assert not any("api responses" in u for u in report.unexercised)

    def test_it_is_reported_unexercised_not_crashed_without_fastapi(
        self, conn, monkeypatch
    ):
        """The other half of the contract: a core install with no `[api]`
        extra still gets a complete, non-crashing audit — "api responses"
        just shows up as unexercised rather than checked. Simulated by
        making `import fragrance_graph.api` raise `ImportError`, the
        documented way to fake a missing module (`sys.modules[name] =
        None`) without actually uninstalling FastAPI out from under the
        rest of the suite.
        """
        import sys

        monkeypatch.setitem(sys.modules, "fragrance_graph.api", None)
        report = audit(conn)
        assert "api responses" not in report.checked
        assert any("api responses" in u for u in report.unexercised)
        assert not [v for v in report.violations if v.surface == "api responses"]
