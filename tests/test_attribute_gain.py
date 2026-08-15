"""The experiment that must be able to say no.

An earlier run concluded "enrichment scatters" from a page count. These
tests exist so this one cannot reach a conclusion the same way: success is
defined before the run, in the units recommendation consumes, and the
diff names which opinion became agreement rather than only counting.
"""

import json

import pytest

from fragrance_graph.experiments.attribute_gain import (
    COHORT,
    before,
    diff,
)
from fragrance_graph.ingest.store import ingest
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def note(conn, i, *, frag, value, author, channel="chan_a"):
    body = f"comment {i}: {value}"
    ingest(conn, [make_comment(i, body=body, source_channel=channel,
                               raw_json=json.dumps({"author": author,
                                                    "videoId": "v1"}))])
    cid = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, sentiment,
             confidence, evidence_span, evidence_verified, polarity,
             extraction_model, created_at)
        VALUES (%s, 'NOTE_DESCRIPTOR', 'FRAGRANCE', 'it', %s, 'TAG', %s,
                'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, frag, value, body),
    )
    conn.commit()


class TestTheCohort:
    def test_it_spans_the_evidence_range(self):
        """A strategy that only works on the densest bottle is a strategy
        for one bottle. The spread is what makes the result generalise."""
        assert len(COHORT) >= 10

    def test_it_includes_thin_bottles_not_only_rich_ones(self, conn):
        for name in ("Parfums de Marly Layton", "Parfums de Marly Oajan"):
            assert name in COHORT

    def test_a_missing_bottle_is_skipped_not_invented(self, conn):
        add_fragrance(conn, "Parfums de Marly Layton")
        states, _ = before(conn, ("Parfums de Marly Layton", "Nonexistent Bottle"))
        assert [s.name for s in states] == ["Parfums de Marly Layton"]


class TestSuccessIsDefinedBeforeTheRun:
    def test_a_singleton_becoming_repeated_is_the_conversion(self, conn):
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1", channel="c1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        note(conn, 2, frag=frag, value="rose", author="p2", channel="c2")
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.singleton_to_repeated == [["note", "rose"]]
        assert gain.converted == 1

    def test_a_brand_new_singleton_is_not_a_conversion(self, conn):
        """The failure mode the whole metric exists to catch: six hundred
        new one-off descriptors move `facts` and answer nothing."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        note(conn, 2, frag=frag, value="lychee", author="p2")
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.converted == 0
        assert gain.new_facts == [["note", "lychee"]]

    def test_the_diff_names_which_opinion_became_agreement(self, conn):
        """Counting says the numbers moved. Naming tells a scheduler where
        to spend next."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        for value in ("rose", "lychee", "powder"):
            note(conn, hash(value) % 900, frag=frag, value=value, author="p1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        note(conn, 500, frag=frag, value="lychee", author="p2", channel="c2")
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.singleton_to_repeated == [["note", "lychee"]]

    def test_a_total_failure_has_no_unit_cost_rather_than_a_perfect_one(self):
        """This test previously asserted 0.0 and so pinned the defect.

        Money spent for zero conversions is the worst possible result, and
        returning zero made it read as the best possible unit cost — in
        the single metric this experiment exists to decide on.
        """
        from fragrance_graph.experiments.attribute_gain import BottleGain

        gain = BottleGain(name="X", fragrance_id=1, usd=0.17)
        assert gain.usd_per_conversion is None
        assert gain.converted == 0

    def test_cost_per_conversion_divides_by_conversions(self):
        from fragrance_graph.experiments.attribute_gain import BottleGain

        gain = BottleGain(
            name="X", fragrance_id=1, usd=0.34,
            singleton_to_repeated=[["note", "a"], ["note", "b"]],
        )
        assert gain.usd_per_conversion == pytest.approx(0.17)


class TestItCannotSpendWithoutTheLedger:
    def test_importing_the_module_cannot_construct_a_client(self):
        """Checked at *module* level only.

        `run` imports `build_client` inside the function on purpose: that
        is the paid path and it is supposed to reach one. The invariant is
        that merely importing this module — which `plan`, `before` and
        `report` all do — pulls in nothing that can spend. A paid module
        whose import graph reaches a client is how the cap has been escaped
        four times.
        """
        import ast
        import pathlib

        from fragrance_graph.experiments import attribute_gain

        tree = ast.parse(pathlib.Path(attribute_gain.__file__).read_text())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                top_level.add(node.module or "")
                top_level |= {a.name for a in node.names}
        assert not (
            top_level
            & {"anthropic", "httpx", "openai", "build_client", "build_llm"}
        ), f"module-level imports reach a client: {top_level}"

    def test_the_free_commands_never_reach_a_paid_import(self):
        """`plan`, `before` and `report` must stay free even by accident."""
        import inspect

        from fragrance_graph.experiments import attribute_gain

        for func in (attribute_gain.before, attribute_gain.render_report):
            source = inspect.getsource(func)
            assert "build_client" not in source
            assert "Budget" not in source

    def test_before_and_plan_never_reach_a_paid_path(self, conn):
        """They are the free half and must stay free even by accident."""
        import inspect

        from fragrance_graph.experiments import attribute_gain

        source = inspect.getsource(attribute_gain.before)
        assert "Budget" not in source
        assert "extract" not in source


class TestCodexAcquisitionFindings:
    """Five findings from the 2026-08-15 acquisition review."""

    def test_singleton_to_supported_is_actually_computed(self, conn):
        """P2. It was named as the second success criterion, displayed in
        the summary line, and never populated — so a conversion that did
        happen reported as "0 ->supported"."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1", channel="c1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        for i, (author, chan) in enumerate(
            [("p2", "c2"), ("p3", "c3")], start=10
        ):
            note(conn, i, frag=frag, value="rose", author=author, channel=chan)
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.singleton_to_supported == [["note", "rose"]]

    def test_a_run_that_converts_nothing_reports_no_unit_cost(self):
        from fragrance_graph.experiments.attribute_gain import BottleGain

        assert BottleGain(name="X", fragrance_id=1, usd=1.70).usd_per_conversion is None


class TestTheExperimentCanActuallyRun:
    """P1. The `run` branch checked the budget, printed instructions and
    exited zero — it never enriched, diffed, or recorded anything, and
    `report` was not a command. The experiment could produce neither a
    positive nor a negative result.
    """

    def test_enrich_cohort_diffs_around_a_real_run(self, conn):
        from fragrance_graph.experiments.attribute_gain import enrich_cohort

        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1", channel="c1")
        states, _ = before(conn, ("Lattafa Khamrah",))

        def run_one(name):
            note(conn, 2, frag=frag, value="rose", author="p2", channel="c2")
            return 40, 0.02, 100, "target-met"

        (gain,) = enrich_cohort(conn, states, run_one=run_one, limit=1)
        assert gain.singleton_to_repeated == [["note", "rose"]]
        assert gain.usd == 0.02
        assert gain.quota_units == 100
        assert gain.stop_reason == "target-met"
        assert gain.usd_per_conversion == pytest.approx(0.02)

    def test_it_reports_a_negative_result(self, conn):
        """The outcome the experiment must be able to reach."""
        from fragrance_graph.experiments.attribute_gain import enrich_cohort

        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1")
        states, _ = before(conn, ("Lattafa Khamrah",))

        def run_one(name):
            note(conn, 2, frag=frag, value="lychee", author="p2")
            return 40, 0.17, 100, "spend-ceiling"

        (gain,) = enrich_cohort(conn, states, run_one=run_one, limit=1)
        assert gain.converted == 0
        assert gain.usd_per_conversion is None
        assert gain.new_facts == [["note", "lychee"]]

    def test_each_bottle_is_diffed_against_its_own_fresh_baseline(self, conn):
        """An earlier bottle's enrichment can add evidence about a later
        one — they are discussed in the same comments. Diffing a late
        bottle against a file written before any of it would credit this
        run with conversions another bottle bought."""
        from fragrance_graph.experiments.attribute_gain import enrich_cohort

        first = add_fragrance(conn, "Lattafa Khamrah")
        second = add_fragrance(conn, "Parfums de Marly Layton")
        note(conn, 1, frag=second, value="vanilla", author="p1", channel="c1")
        states, _ = before(conn, ("Lattafa Khamrah", "Parfums de Marly Layton"))

        calls = []

        def run_one(name):
            calls.append(name)
            if len(calls) == 1:
                # Enriching Khamrah also produces a Layton claim.
                note(conn, 2, frag=second, value="vanilla", author="p2",
                     channel="c2")
                note(conn, 3, frag=first, value="dates", author="p3")
            return 10, 0.01, 100, "creators-exhausted"

        gains = enrich_cohort(conn, states, run_one=run_one, limit=2)
        layton = next(g for g in gains if "Layton" in g.name)
        assert layton.converted == 0, (
            "Layton's vanilla converted during Khamrah's run, not its own"
        )

    def test_the_report_renders_without_a_run(self, conn):
        from fragrance_graph.coverage import snapshot
        from fragrance_graph.experiments.attribute_gain import render_report

        now = snapshot(conn)
        text = render_report([], now, now)
        assert "nothing converted" in text
        assert "negative result" in text

    def test_report_is_a_registered_command(self):
        from fragrance_graph.experiments.attribute_gain import main

        assert main(["report"]) == 0


class TestTheCohortIsActuallyEnrichable:
    """Caught before spending. Seven of the ten cohort bottles had no row
    in `frontier.candidates`, whose 4-9 comparison-claim band answers a
    different question — Layton falls outside it for having *two hundred*
    comparison claims. The run would have skipped most of the experiment
    and reported success on what remained.
    """

    def test_a_candidate_is_built_from_the_catalogue_name(self, conn):
        from fragrance_graph.experiments.attribute_gain import _candidate_for

        frag = add_fragrance(conn, "Parfums de Marly Layton")
        note(conn, 1, frag=frag, value="vanilla", author="p1", channel="c1")
        note(conn, 2, frag=frag, value="menthol", author="p2", channel="c2")

        candidate = _candidate_for(conn, "Parfums de Marly Layton")
        assert candidate is not None
        assert candidate.text == "Parfums de Marly Layton"
        assert candidate.claims == 2
        assert candidate.creators == 2

    def test_it_does_not_depend_on_the_comparison_claim_band(self, conn):
        """A bottle with no comparison claims at all is still enrichable —
        learning what it smells like is the whole job."""
        from fragrance_graph.experiments.attribute_gain import _candidate_for

        frag = add_fragrance(conn, "Fragrance World Oud Wonder")
        note(conn, 1, frag=frag, value="oud", author="p1")
        assert _candidate_for(conn, "Fragrance World Oud Wonder") is not None

    def test_a_name_not_in_the_catalogue_returns_none(self, conn):
        from fragrance_graph.experiments.attribute_gain import _candidate_for

        assert _candidate_for(conn, "Nonexistent Bottle") is None

    def test_matching_is_case_insensitive(self, conn):
        from fragrance_graph.experiments.attribute_gain import _candidate_for

        add_fragrance(conn, "Lattafa Khamrah")
        assert _candidate_for(conn, "lattafa khamrah") is not None
