"""The recommendation benchmark, and the invariant it exists to protect.

These run against the *real* corpus rather than a fixture, because the
question the benchmark asks — does this product say anything nobody said —
is only meaningful against real evidence. A fixture would prove the
harness works and nothing about the system.
"""

import pytest

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evals.recommend import (
    DEFAULT_SET,
    Report,
    discloses,
    evaluate,
    load_cases,
)


@pytest.fixture(scope="module")
def corpus():
    """The developer database, skipped if it is not built."""
    try:
        conn = get_connection(DEFAULT_DB_URL)
    except Exception:  # noqa: BLE001 - any connection failure means skip
        pytest.skip("no developer database; run `corpus import` first")
    if conn.execute("SELECT count(*) FROM claims").fetchone()[0] < 100:
        pytest.skip("developer database is empty; run `corpus import` first")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def report(corpus) -> Report:
    return evaluate(corpus, DEFAULT_SET)


class TestTheBenchmarkItself:
    def test_every_case_loads(self):
        cases = load_cases(DEFAULT_SET)
        assert len(cases) >= 20
        assert len({c.id for c in cases}) == len(cases), "ids are unique"

    def test_it_covers_every_required_category(self):
        categories = {c.category for c in load_cases(DEFAULT_SET)}
        assert {
            "anchor_subtraction", "anchor_addition", "attribute_conjunction",
            "vibe", "occasion", "comparison", "evidence", "refusal",
            "negation", "new_release",
        } <= categories

    def test_some_cases_expect_a_refusal(self):
        """A benchmark where every question has an answer rewards guessing."""
        cases = load_cases(DEFAULT_SET)
        assert sum(1 for c in cases if not c.answerable) >= 4


class TestTheInvariant:
    def test_no_unsupported_assertions(self, report):
        """The one metric that outranks the others. A single failure here
        means the product asserted something nobody said."""
        assert report.unsupported == 0, "\n".join(
            problem for r in report.results for problem in r.unsupported
        )

    def test_no_hard_constraint_violations(self, report):
        assert report.violations == 0, "\n".join(
            problem for r in report.results for problem in r.violations
        )

    def test_every_case_passes(self, report):
        """Recorded 2026-08-15 at 22/22.

        Asserted as "all of them" rather than ">= 22". A fixed floor lets a
        newly added case fail silently forever, which turns growing the
        benchmark into a way of hiding failures — the opposite of what a
        benchmark is for. Adding a case that does not pass must break the
        build until it is fixed or deliberately marked unanswerable.
        """
        failed = [r for r in report.results if not r.passed]
        assert not failed, report.render(failures_only=True)


class TestDisclosureCheck:
    """The check the invariant rests on, tested on its own."""

    def test_a_hedge_counts_as_disclosure(self):
        assert discloses("one commenter said rose")

    def test_explicit_counts_count_as_disclosure(self):
        assert discloses("2 people across 1 channel compared this with Delina")

    def test_a_flat_assertion_does_not(self):
        assert not discloses("smells of rose")

    def test_a_bare_adjective_does_not(self):
        assert not discloses("rose")


class TestCodexPhase4Findings:
    """Five findings from the 2026-08-15 harness review, all confirmed."""

    def test_an_overclaiming_note_is_caught(self):
        """P1. `Answer.note` is printed above every result and was exempt
        from the primary metric entirely."""
        from fragrance_graph.evals.recommend import _asserts

        assert _asserts("The community agrees this is a dupe")
        assert _asserts("Consensus is that it lasts all day")

    def test_an_honest_caveat_is_not_an_overclaim(self):
        """The first version of this check ran the other way and flagged
        twelve careful sentences."""
        from fragrance_graph.evals.recommend import _asserts

        assert not _asserts(
            "No match below clears the independence bar — the evidence is "
            "there, but not from enough separate creators."
        )
        assert not _asserts("Every match below rests on a single person's remark.")

    def test_a_crowded_single_channel_is_not_called_a_single_observation(self):
        """P1. `_note` equated 'no declarable reason' with 'one person', so
        nine people in one creator's section were described as a single
        observation — contradicting the result printed beneath it."""
        from fragrance_graph.evidence import Strength
        from fragrance_graph.plan import QueryPlan
        from fragrance_graph.recommend import Reason, Recommendation, _note

        candidate = Recommendation(
            fragrance_id=1,
            name="X",
            reasons=[Reason(kind="graph", text="9 people across 1 channel",
                            strength=Strength.OBSERVED, people=9, creators=1)],
        )
        note = _note(QueryPlan(text="q"), [candidate], 0, {})
        assert "single" not in note
        assert "independence" in note

    def test_a_forged_match_marker_does_not_certify_itself(self):
        """P1. The violation check read `candidate.matched`, which `_score`
        writes immediately after filtering — so the same code that made a
        mistake also wrote the receipt saying it had not."""
        from fragrance_graph.evals.recommend import unmet_constraints
        from fragrance_graph.plan import Constraint
        from fragrance_graph.recommend import Recommendation

        forged = Recommendation(
            fragrance_id=1, name="X",
            matched=["note=raspberry"],   # claims it matched
            reasons=[],                   # cites nothing
        )
        assert unmet_constraints(forged, [Constraint("note", "raspberry")])

    def test_a_genuine_match_passes_the_check(self):
        from fragrance_graph.evals.recommend import unmet_constraints
        from fragrance_graph.evidence import Strength
        from fragrance_graph.plan import Constraint
        from fragrance_graph.recommend import Reason, Recommendation

        real = Recommendation(
            fragrance_id=1, name="X", matched=["note=raspberry"],
            reasons=[Reason(kind="constraint", text="raspberry",
                            strength=Strength.OBSERVED, people=1)],
        )
        assert not unmet_constraints(real, [Constraint("note", "raspberry")])

    def test_the_ratchet_admits_no_new_failures(self, report):
        """P2. A `>= 22` floor let a newly added case fail forever, turning
        growing the benchmark into a way of hiding failures."""
        assert all(r.passed for r in report.results)
