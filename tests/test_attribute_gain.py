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

    def test_the_cap_landing_mid_cohort_keeps_what_was_bought(self, conn):
        """Wrapping the whole loop discarded six completed bottles when the
        cap landed on the seventh — paying for measurements and throwing
        them away, which is the worst of both outcomes."""
        from fragrance_graph.budget import BudgetExhausted
        from fragrance_graph.experiments.attribute_gain import enrich_cohort

        first = add_fragrance(conn, "Lattafa Khamrah")
        add_fragrance(conn, "Parfums de Marly Layton")
        note(conn, 1, frag=first, value="rose", author="p1", channel="c1")
        states, _ = before(conn, ("Lattafa Khamrah", "Parfums de Marly Layton"))

        calls = []

        def run_one(name):
            calls.append(name)
            if len(calls) == 1:
                note(conn, 2, frag=first, value="rose", author="p2",
                     channel="c2")
                return 40, 0.08, 100, "target-met"
            raise BudgetExhausted("daily cap reached")

        gains = enrich_cohort(conn, states, run_one=run_one, limit=2)
        assert len(gains) == 2, "the completed bottle is kept"
        assert gains[0].converted == 1
        assert gains[0].usd == 0.08
        assert gains[1].stop_reason == "daily-cap"

    def test_spend_is_read_from_the_ledger_not_the_runner(self, conn):
        """On the first real run the cap landed mid-bottle, the exception
        escaped before the trial's spend was read, and $0.082 of charged
        money vanished from the totals — understating cost by 21% in the
        one number the experiment exists to produce."""
        from fragrance_graph.budget import BudgetExhausted
        from fragrance_graph.experiments.attribute_gain import enrich_cohort

        frag = add_fragrance(conn, "Lattafa Khamrah")
        add_fragrance(conn, "Parfums de Marly Layton")
        note(conn, 1, frag=frag, value="rose", author="p1", channel="c1")
        states, _ = before(conn, ("Lattafa Khamrah", "Parfums de Marly Layton"))

        charged = [0.0]

        def ledger_spend():
            return charged[0]

        calls = []

        def run_one(name):
            calls.append(name)
            if len(calls) == 1:
                charged[0] += 0.11
                note(conn, 2, frag=frag, value="rose", author="p2",
                     channel="c2")
                return 40, 0.11, 100, "target-met"
            # Money is spent, then the cap raises before it is returned.
            charged[0] += 0.082
            raise BudgetExhausted("daily cap reached")

        gains = enrich_cohort(
            conn, states, run_one=run_one, limit=2, ledger_spend=ledger_spend
        )
        assert gains[1].usd == pytest.approx(0.082), (
            "the interrupted bottle's charged spend is still attributed"
        )
        assert sum(g.usd for g in gains) == pytest.approx(0.192)

    def test_the_ledger_wins_when_the_runner_disagrees(self, conn):
        """The runner reports what it thinks it spent; the ledger reports
        what was billed. Where they differ the ledger is right."""
        from fragrance_graph.experiments.attribute_gain import enrich_cohort

        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1")
        states, _ = before(conn, ("Lattafa Khamrah",))
        charged = [0.0]

        def run_one(name):
            charged[0] += 0.20
            return 10, 0.05, 100, "target-met"   # under-reports

        (gain,) = enrich_cohort(
            conn, states, run_one=run_one, limit=1,
            ledger_spend=lambda: charged[0],
        )
        assert gain.usd == pytest.approx(0.20)


class TestTheFunnelIsObservationalOnly:
    """The cohort is only comparable if the treatment is identical. The
    first three bottles ran without this instrumentation, so it must
    change nothing about what is searched, bought or extracted."""

    def test_it_only_reads(self):
        """Checked against the SQL it executes, not against substrings —
        `claims_extracted` is a field name, not an operation."""
        import ast
        import inspect

        from fragrance_graph.experiments.attribute_gain import measure_funnel

        tree = ast.parse(inspect.getsource(measure_funnel).lstrip())
        statements = [
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        sql = [t for t in statements if "FROM" in t.upper()]
        assert sql, "the function does query something"
        for statement in sql:
            head = statement.strip().split()[0].upper()
            assert head == "SELECT", f"not a read: {statement[:60]}"

    def test_it_reaches_no_network_or_extraction_code(self):
        import ast
        import inspect

        from fragrance_graph.experiments.attribute_gain import measure_funnel

        tree = ast.parse(inspect.getsource(measure_funnel).lstrip())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not (called & {"urlopen", "create", "guard", "record", "commit"})

    def test_it_classifies_only_claims_written_after_the_mark(self, conn):
        from fragrance_graph.experiments.attribute_gain import (
            _high_water,
            measure_funnel,
        )

        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1")
        marks = _high_water(conn)
        note(conn, 2, frag=frag, value="lychee", author="p2")

        funnel = measure_funnel(
            conn, frag, queries=["q"], videos=1, creators=1, **marks
        )
        assert funnel.claims_extracted == 1, "the earlier claim is not counted"
        assert funnel.target_claims == 1

    def test_it_separates_target_from_other_and_floating(self, conn):
        from fragrance_graph.experiments.attribute_gain import (
            _high_water,
            measure_funnel,
        )

        target = add_fragrance(conn, "Lattafa Khamrah")
        other = add_fragrance(conn, "Parfums de Marly Layton")
        marks = _high_water(conn)
        note(conn, 1, frag=target, value="rose", author="p1")
        note(conn, 2, frag=other, value="vanilla", author="p2")
        note(conn, 3, frag=None, value="sweet", author="p3")

        funnel = measure_funnel(
            conn, target, queries=["q"], videos=2, creators=2, **marks
        )
        assert funnel.claims_extracted == 3
        assert funnel.target_claims == 1
        assert funnel.other_bottle_claims == 1
        assert funnel.floating_claims == 1
        assert funnel.target_share == pytest.approx(1 / 3)

    def test_a_pairwise_target_claim_is_counted_apart_from_unary(self, conn):
        """"Khamrah is like Layton" is evidence about Khamrah and is not an
        attribute of it. Merging them would hide that a run bought
        comparisons when it was sent to learn descriptions."""
        from fragrance_graph.experiments.attribute_gain import (
            _high_water,
            measure_funnel,
        )

        target = add_fragrance(conn, "Lattafa Khamrah")
        other = add_fragrance(conn, "Parfums de Marly Layton")
        marks = _high_water(conn)
        note(conn, 1, frag=target, value="rose", author="p1")
        conn.execute(
            """
            INSERT INTO claims
                (comment_id, claim_type, subject_kind, raw_subject_text,
                 subject_frag_id, object_kind, raw_object_text, object_frag_id,
                 sentiment, confidence, evidence_span, evidence_verified,
                 polarity, extraction_model, created_at)
            VALUES ((SELECT max(id) FROM comments), 'SIMILAR_TO', 'FRAGRANCE',
                    'it', %s, 'FRAGRANCE', 'other', %s, 'POSITIVE', 0.9, 'x',
                    1, 'ASSERTED', 'test', '2026-01-01')
            """,
            (target, other),
        )
        conn.commit()

        funnel = measure_funnel(
            conn, target, queries=["q"], videos=1, creators=1, **marks
        )
        assert funnel.target_unary == 1
        assert funnel.target_pairwise == 1


class TestDensitySegmentation:
    def test_bottles_are_banded_by_pre_experiment_evidence(self):
        from fragrance_graph.experiments.attribute_gain import band_of

        assert band_of(6) == "sparse"
        assert band_of(33) == "medium"
        assert band_of(103) == "dense"

    def test_the_report_never_shows_only_an_aggregate(self):
        """A mechanism behaving differently by density is not described by
        its mean."""
        from fragrance_graph.experiments.attribute_gain import (
            BottleGain,
            render_segmented,
        )

        rows = [
            BottleGain(name="dense", fragrance_id=1, density_before=103,
                       usd=0.19, singleton_to_repeated=[]),
            BottleGain(name="sparse", fragrance_id=2, density_before=6,
                       usd=0.10, singleton_to_repeated=[["note", "oud"]]),
        ]
        text = render_segmented(rows)
        assert "sparse" in text and "dense" in text
        assert "n/a" in text, "the band with no conversions has no unit cost"


class TestTheCohortResumesRatherThanRestarts:
    """The cap landed mid-cohort, so the run has to be resumable.

    `enrich_cohort` took `states[:limit]` off a fixed ordered list, which
    means a second invocation started again at bottle one. That is not
    just wasted money: the second purchase of Layton would diff against a
    baseline that already contains what the first purchase added, and
    report a conversion rate for spend that bought nothing new.
    """

    def test_recorded_bottles_are_remembered(self, tmp_path):
        from fragrance_graph.experiments.attribute_gain import completed_names

        results = tmp_path / "gain.jsonl"
        results.write_text(
            '{"kind": "gain", "name": "Parfums de Marly Layton", "usd": 0.19}\n'
            '{"kind": "gain", "name": "Lattafa Khamrah", "usd": 0.11}\n'
            '{"kind": "other", "name": "Not A Bottle"}\n'
        )
        assert completed_names(results) == {
            "Parfums de Marly Layton",
            "Lattafa Khamrah",
        }

    def test_no_ledger_of_runs_means_nothing_is_skipped(self, tmp_path):
        from fragrance_graph.experiments.attribute_gain import completed_names

        assert completed_names(tmp_path / "absent.jsonl") == set()

    def test_a_torn_line_does_not_silently_skip_a_bottle(self, tmp_path):
        """A half-written line must not be read as "this bottle is done" —
        that would drop it from the cohort without anyone noticing."""
        from fragrance_graph.experiments.attribute_gain import completed_names

        results = tmp_path / "gain.jsonl"
        results.write_text('{"kind": "gain", "name": "Dior Sauv\n')
        assert completed_names(results) == set()

    def test_resuming_leaves_the_treatment_alone(self):
        """Skipping decides *which* bottles run, never how one is enriched.

        If resume logic ever reached into run_one the ten bottles would
        stop being comparable, which is the whole point of the cohort.
        """
        import inspect

        from fragrance_graph.experiments.attribute_gain import completed_names

        source = inspect.getsource(completed_names)
        for forbidden in ("run_one", "enrich_one", "extract", "budget"):
            assert forbidden not in source


class TestDiagnosticsRunOnBothPaths:
    """Instrumentation that only fires when the run fails is not
    instrumentation.

    density_before and funnel were set inside the BudgetExhausted branch
    only. Run 1 hit the cap and produced a correct funnel; run 2 finished
    cleanly and produced zeros for all seven bottles, banding a 46-fact
    bottle as sparse and quietly emptying the segmentation the cohort
    exists to produce.
    """

    def test_a_clean_run_still_records_density_and_funnel(self, monkeypatch):
        from fragrance_graph.experiments import attribute_gain as ag

        state = ag.BottleState(fragrance_id=7, name="Dior Sauvage", facts=46)
        captured = {}

        def fake_instrument(conn, gain, st, fresh, marks, last_run):
            captured["density"] = fresh.facts
            gain.density_before = fresh.facts
            gain.funnel = {"claims_extracted": 12}

        monkeypatch.setattr(ag, "_instrument", fake_instrument)
        monkeypatch.setattr(ag, "_bottle_state", lambda c, i, n: state)
        monkeypatch.setattr(ag, "_high_water", lambda c: {})

        gains = ag.enrich_cohort(
            None,
            [state],
            run_one=lambda name: (5, 0.01, 100, "done"),
            limit=1,
        )
        assert gains[0].density_before == 46, "clean runs must record density"
        assert gains[0].funnel, "clean runs must record a funnel"

    def test_a_dense_bottle_is_not_banded_sparse(self):
        from fragrance_graph.experiments.attribute_gain import band_of

        assert band_of(46) == "medium"
        assert band_of(109) == "dense"
        assert band_of(6) == "sparse"
        # The zero that the unset field produced, banded as if measured.
        assert band_of(0) == "sparse"
