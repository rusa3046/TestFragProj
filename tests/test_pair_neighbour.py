"""The capped progress metric for the pair-gap experiment.

The arithmetic here decides a paid experiment, so it is tested apart from
the buying. The distinction being protected is the one the previous run
failed on: **30 extracted edges is not 30 useful edges**, and a metric that
cannot tell them apart will call a useless run a success.
"""

import pytest

from fragrance_graph.experiments.pair_neighbour import (
    ArmResult,
    Credit,
    PairState,
    Snapshot,
    credit,
    verdict,
)


def pair(name, *, people, creators, flanker=False):
    return PairState(
        partner=name,
        people=frozenset(f"p{i}" for i in range(people)),
        creators=frozenset(f"c{i}" for i in range(creators)),
        flanker=flanker,
    )


def grown(base, *, people=0, creators=0, tag="a"):
    """The same pair after a run added independent people and channels.

    `tag` distinguishes one arm's arrivals from another's; without it the
    second arm's "new" people reuse the first arm's names, the set diff is
    empty, and a test of the cap silently tests nothing.
    """
    return PairState(
        partner=base.partner,
        people=base.people | {f"{tag}_p{i}" for i in range(people)},
        creators=base.creators | {f"{tag}_c{i}" for i in range(creators)},
        flanker=base.flanker,
    )


class TestTheCapIsTheDistanceToTheGate:
    def test_a_pair_one_creator_short_earns_one_for_a_creator(self):
        before = {"X": pair("X", people=3, creators=1)}
        after = {"X": grown(before["X"], creators=1)}
        earned = credit(before, before, after)
        assert (earned.people, earned.creators) == (0, 1)

    def test_extra_creators_beyond_the_shortfall_score_nothing(self):
        """Three channels arriving on a pair that needed one is graph yield,
        not progress. The uncapped figure still records all three."""
        before = {"X": pair("X", people=3, creators=1)}
        after = {"X": grown(before["X"], creators=3)}
        earned = credit(before, before, after)
        assert earned.creators == 1, "capped at the one it was missing"
        assert earned.uncapped_creators == 3, "and the raw number survives"

    def test_a_pair_already_at_the_gate_earns_nothing(self):
        before = {"X": pair("X", people=3, creators=2)}
        after = {"X": grown(before["X"], people=5, creators=4)}
        earned = credit(before, before, after)
        assert earned.capped_total == 0
        assert earned.uncapped_total == 9

    def test_a_flanker_pair_is_capped_at_its_higher_bar(self):
        """5 people and 3 creators, so a flanker at 3p/1c is missing 2 and 2
        — a flanker has further to go and the metric must say so."""
        before = {"X": pair("X", people=3, creators=1, flanker=True)}
        after = {"X": grown(before["X"], people=4, creators=4)}
        earned = credit(before, before, after)
        assert (earned.people, earned.creators) == (2, 2)

    def test_a_partner_that_did_not_exist_is_capped_at_its_own_thresholds(self):
        """A genuinely new comparison is progress, but only as far as the
        gate — an entirely new pair cannot score more than 3 + 2."""
        before: dict = {}
        after = {"X": pair("X", people=9, creators=9)}
        earned = credit(before, before, after)
        assert (earned.people, earned.creators) == (3, 2)
        assert earned.partners_gained == ("X",)


class TestTheCapIsConsumedNotReset:
    """Whatever the first arm pays for is no longer available to the second.

    This deepens the declared order bias — the direct arm runs first and
    eats the cap first — and it is the conservative direction, because the
    hypothesis predicts the neighbour wins.
    """

    def test_the_second_arm_cannot_be_paid_for_the_same_shortfall(self):
        frozen = {"X": pair("X", people=1, creators=1)}
        after_a = {"X": grown(frozen["X"], people=2)}
        arm_a = credit(frozen, frozen, after_a)
        assert arm_a.people == 2

        after_b = {"X": grown(after_a["X"], people=2, tag="b")}
        arm_b = credit(frozen, after_a, after_b, already_spent=arm_a)
        assert arm_b.people == 0, (
            "the pair was two people short and A supplied both; B is adding "
            "a fourth and fifth, which is yield rather than progress"
        )
        assert arm_b.uncapped_people == 2


class TestTheVerdictRefusesVolumeWithoutIndependence:
    """The previous run's failure, in pair-shaped form. 30 comparisons that
    add no independent creator move nothing, and must not read as success.
    """

    def _arm(self, name, *, usd, capped, raw, creators_on=()):
        empty = Snapshot()
        return ArmResult(
            arm=name, target="t", before=empty, after=Snapshot(raw_anchor_claims=raw),
            earned=Credit(
                people=capped, creators=0, creator_gained_on=tuple(creators_on)
            ),
            usd=usd,
        )

    def test_many_raw_comparisons_and_no_new_creator_is_a_failure(self):
        a = self._arm("A", usd=0.05, capped=1, raw=0, creators_on=("X",))
        b = self._arm("B", usd=0.10, capped=6, raw=30)
        passed, why = verdict(a, b)
        assert not passed
        assert "Volume without independence" in why

    def test_beating_on_progress_with_a_new_creator_passes(self):
        a = self._arm("A", usd=0.10, capped=1, raw=2, creators_on=("X",))
        b = self._arm("B", usd=0.10, capped=5, raw=6, creators_on=("Y",))
        passed, _ = verdict(a, b)
        assert passed

    def test_the_direct_arm_matching_the_neighbour_falsifies_it(self):
        a = self._arm("A", usd=0.05, capped=5, raw=2, creators_on=("X",))
        b = self._arm("B", usd=0.10, capped=5, raw=4, creators_on=("Y",))
        passed, why = verdict(a, b)
        assert not passed
        assert "not the efficient instrument" in why

    def test_neither_arm_producing_usable_evidence_is_a_failure(self):
        a = self._arm("A", usd=0.05, capped=0, raw=1)
        b = self._arm("B", usd=0.10, capped=0, raw=2)
        passed, why = verdict(a, b)
        assert not passed
        assert "not reachable" in why

    def test_zero_spend_scores_zero_rather_than_infinity(self):
        arm = self._arm("B", usd=0.0, capped=5, raw=0)
        assert arm.progress_per_dollar == 0.0


class TestTheBaselineMatchesTheFrozenDesign:
    """A guard on the constants, so an edit to the module cannot quietly
    retarget an experiment whose design is committed elsewhere."""

    def test_the_anchor_and_arms_are_the_frozen_ones(self):
        from fragrance_graph.experiments.pair_neighbour import (
            ANCHOR,
            ARM_A,
            ARM_B,
            CEILING_USD,
        )

        assert ANCHOR == "Parfums de Marly Delina"
        assert ARM_A == ANCHOR
        assert ARM_B == "Maison Alhambra Delilah"
        assert CEILING_USD == pytest.approx(0.10)
