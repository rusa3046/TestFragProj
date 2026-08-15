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

    def test_the_recorded_baseline_holds(self, report):
        """Recorded 2026-08-15 at 22/22. Ratcheted rather than pinned: this
        may only go up, and a drop is a regression worth failing on."""
        passed = sum(1 for r in report.results if r.passed)
        assert passed >= 22, report.render(failures_only=True)


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
