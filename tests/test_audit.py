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
