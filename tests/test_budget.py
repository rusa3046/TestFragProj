"""The daily spending cap, and the rule that curates without a human.

Both exist because the loop runs unattended. The thing being guarded
against is not one expensive day — it is a cheap mistake repeating on a
schedule, which is why the cap is a hard stop and the curation rule refuses
rather than guesses.
"""

import json

import pytest

from fragrance_graph.budget import (
    DAILY_CAP_USD,
    Budget,
    BudgetExhausted,
    spent_on,
    summary,
)


@pytest.fixture
def ledger(tmp_path):
    return tmp_path / "spend.jsonl"


# --- the cap ---------------------------------------------------------------


def test_a_fresh_day_starts_with_the_whole_cap(ledger):
    budget = Budget.load(ledger, today="2026-08-11")
    assert budget.spent_usd == 0.0
    assert budget.remaining_usd == DAILY_CAP_USD
    budget.check(0.5)  # does not raise


def test_spend_accumulates_across_processes(ledger):
    """The case a per-run counter gets wrong.

    Every scheduled run is a fresh container, so a cap held in memory or in
    the database resets each time and "$1 per day" quietly becomes "$1 per
    run" — useless exactly when something is looping.
    """
    # Expressed against the configured ceiling rather than a hardcoded
    # 0.60/0.40, so raising the cap does not silently turn this into a
    # test of arithmetic that no longer applies.
    most_of_it = DAILY_CAP_USD * 0.6
    first = Budget.load(ledger, today="2026-08-11")
    first.record(most_of_it, "extract")

    second = Budget.load(ledger, today="2026-08-11")
    assert second.spent_usd == pytest.approx(most_of_it)
    assert second.remaining_usd == pytest.approx(DAILY_CAP_USD - most_of_it)
    with pytest.raises(BudgetExhausted):
        second.check(DAILY_CAP_USD * 0.5)


def test_yesterdays_spend_does_not_count_against_today(ledger):
    Budget.load(ledger, today="2026-08-10").record(DAILY_CAP_USD, "extract")
    today = Budget.load(ledger, today="2026-08-11")
    assert today.remaining_usd == DAILY_CAP_USD
    assert not today.exhausted


def test_the_guard_stops_extraction_the_moment_the_cap_is_crossed(ledger):
    """Enforced between batches, not only before the run.

    A pre-flight estimate cannot see a batch that costs more than
    projected, and the projection is explicitly documented as an order of
    magnitude rather than a quote.
    """
    budget = Budget.load(ledger, today="2026-08-11", cap_usd=0.10)
    guard = budget.guard("extract")

    guard(0.04, 20)  # fine
    guard(0.04, 20)  # fine, 0.08
    with pytest.raises(BudgetExhausted, match="stopping"):
        guard(0.05, 20)  # 0.13 > 0.10

    # The spend that crossed the line is still recorded. It was really
    # spent — the batch had already committed before the guard ran.
    assert Budget.load(ledger, today="2026-08-11").spent_usd == pytest.approx(0.13)


def test_a_spend_is_on_disk_before_it_is_counted(ledger):
    """A crash between writing and counting must over-report, never under.

    Under-reporting is the direction that spends money twice.
    """
    budget = Budget.load(ledger, today="2026-08-11")
    budget.record(0.25, "extract", comments=20)

    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert records[0]["usd"] == 0.25
    assert records[0]["date"] == "2026-08-11"
    assert records[0]["comments"] == 20
    assert spent_on(ledger, "2026-08-11") == pytest.approx(0.25)


def test_a_corrupt_ledger_line_does_not_stop_the_cap_working(ledger, caplog):
    """The ledger's job is to prevent overspending. An unreadable line from
    some older format must not become a reason to spend nothing forever,
    nor a reason to spend freely."""
    ledger.write_text(
        '{"date": "2026-08-11", "usd": 0.4, "what": "extract"}\n'
        "not json at all\n"
        '{"date": "2026-08-11", "usd": 0.3, "what": "extract"}\n'
    )
    assert spent_on(ledger, "2026-08-11") == pytest.approx(0.7)


def test_negative_spend_is_rejected(ledger):
    """A refund is not a thing this ledger models, and allowing one would
    let a bug buy back budget it never spent."""
    with pytest.raises(ValueError):
        Budget.load(ledger, today="2026-08-11").record(-1.0, "extract")


def test_summary_flags_the_days_that_hit_the_cap(ledger):
    Budget.load(ledger, today="2026-08-10").record(DAILY_CAP_USD, "extract")
    Budget.load(ledger, today="2026-08-11").record(0.02, "extract")
    text = summary(ledger)
    assert "2026-08-10" in text and "at cap" in text
    assert "2026-08-11" in text
    assert text.index("2026-08-11") < text.index("2026-08-10")  # newest first


class TestEveryPaidPathIsGuarded:
    """Found during phase-9 readiness, not by a reviewer.

    `evals/autolabel` computed its own cost and printed it for months
    without ever telling the ledger, so drafting eval labels was a normal
    paid CLI path that walked past the daily cap. The cap has now been
    defeated three times: by a path-resolution escape, by recording
    without raising, and by a module nobody checked.
    """

    def test_autolabel_charges_the_ledger(self, tmp_path):
        from fragrance_graph.budget import Budget
        from fragrance_graph.evals.autolabel import draft

        ledger = tmp_path / "spend.jsonl"
        budget = Budget(cap_usd=10.0, ledger=ledger)

        class FakeClient:
            def __init__(self):
                self.messages = self

            def create(self, **kw):
                class R:
                    stop_reason = "end_turn"
                    content = [
                        type("T", (), {"type": "text",
                                       "text": '{"0": {"claims": []}}'})()
                    ]
                    usage = type("U", (), {"input_tokens": 1000,
                                           "output_tokens": 500})()
                return R()

        entries = [{"comment_id": 1, "body": "x", "claims": []}]
        draft(FakeClient(), entries, on_spend=budget.guard("autolabel"))
        assert ledger.exists(), "the run reached the ledger"
        assert budget.spent_usd > 0

    def test_autolabel_stops_at_the_cap(self, tmp_path):
        from fragrance_graph.budget import Budget, BudgetExhausted
        from fragrance_graph.evals.autolabel import draft

        budget = Budget(cap_usd=0.0001, ledger=tmp_path / "spend.jsonl")

        class FakeClient:
            def __init__(self):
                self.messages = self

            def create(self, **kw):
                class R:
                    stop_reason = "end_turn"
                    content = [
                        type("T", (), {"type": "text",
                                       "text": '{"0": {"claims": []}}'})()
                    ]
                    usage = type("U", (), {"input_tokens": 100_000,
                                           "output_tokens": 100_000})()
                return R()

        entries = [{"comment_id": i, "body": "x", "claims": []} for i in range(20)]
        with pytest.raises(BudgetExhausted):
            draft(FakeClient(), entries, batch_size=1,
                  on_spend=budget.guard("autolabel"))

    def test_no_paid_module_calls_the_model_without_an_on_spend_hook(self):
        """Structural: every function that calls `client.messages.create`
        must accept a spend callback, so a caller cannot spend silently."""
        import ast
        import pathlib as _p

        for name in ("extract/llm.py", "evals/autolabel.py"):
            path = _p.Path("src/fragrance_graph") / name
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                body = ast.dump(node)
                if "messages" in body and "create" in body:
                    continue  # the low-level caller itself
            assert "on_spend" in path.read_text(), (
                f"{name} has no spend hook"
            )


class TestTheConfiguredCeiling:
    """Raised from $1.00 to $1.50 on 2026-08-15.

    The change had to be a *configuration* change rather than an exemption
    for the experiment that wanted it, and these pin the difference. A cap
    raised by editing the constant stays enforced everywhere and stays
    testable; a cap raised by special-casing a caller is neither.
    """

    def test_the_configured_ceiling_is_the_authorized_figure(self):
        assert DAILY_CAP_USD == pytest.approx(1.50)

    def test_it_is_enforced_at_the_new_figure(self, ledger):
        budget = Budget.load(ledger, today="2026-08-16")
        budget.record(1.49, "extract")
        budget.check(0.005)
        with pytest.raises(BudgetExhausted):
            budget.check(0.02)

    def test_spend_already_on_the_ledger_counts_against_it(self, ledger):
        """The authorization was to raise the ceiling, not to clear the
        meter. $1.1104 already spent leaves $0.3896, not $1.50."""
        Budget.load(ledger, today="2026-08-15").record(1.1104, "earlier-run")
        after = Budget.load(ledger, today="2026-08-15")
        assert after.spent_usd == pytest.approx(1.1104)
        assert after.remaining_usd == pytest.approx(0.3896)

    def test_raising_the_ceiling_does_not_reset_the_ledger(self, ledger):
        """Simulated directly: the same ledger read under two ceilings has
        the same spend and only a different remainder."""
        Budget.load(ledger, today="2026-08-16").record(1.10, "extract")
        low = Budget.load(ledger, cap_usd=1.00, today="2026-08-16")
        high = Budget.load(ledger, cap_usd=1.50, today="2026-08-16")
        assert low.spent_usd == high.spent_usd == pytest.approx(1.10)
        assert low.remaining_usd == pytest.approx(0.0)
        assert high.remaining_usd == pytest.approx(0.40)

    def test_exceeding_the_new_ceiling_is_refused_by_the_guard(self, ledger):
        """Not only by `check`. `guard` is what stops a run mid-batch, and
        it is the path every paid caller actually uses."""
        budget = Budget.load(ledger, today="2026-08-16")
        charge = budget.guard("extract")
        charge(1.40, 1000)
        with pytest.raises(BudgetExhausted):
            charge(0.20, 200)
        assert budget.spent_usd == pytest.approx(1.60), (
            "the overshooting batch is still recorded; the ledger records "
            "what was spent, not what was permitted"
        )

    def test_yesterdays_spend_still_does_not_carry_over(self, ledger):
        Budget.load(ledger, today="2026-08-15").record(1.49, "extract")
        assert Budget.load(
            ledger, today="2026-08-16"
        ).remaining_usd == pytest.approx(DAILY_CAP_USD)


class TestEveryPaidPathStillUsesTheSameGuard:
    """The invariant the cap change must not disturb: every paid path ->
    the same guarded ledger -> one configured ceiling."""

    PAID_MODULES = (
        "extract/llm.py",
        "frontier.py",
        "daily.py",
        "evals/autolabel.py",
    )

    def test_each_paid_module_charges_through_the_guard(self):
        import pathlib

        for name in self.PAID_MODULES:
            text = (pathlib.Path("src/fragrance_graph") / name).read_text()
            assert "on_spend" in text, f"{name} has no spend hook"

    def test_none_of_them_hardcodes_a_ceiling(self):
        """A module carrying its own number is a module that keeps the old
        one after a configuration change."""
        import pathlib
        import re

        for name in (*self.PAID_MODULES, "experiments/attribute_gain.py"):
            text = (pathlib.Path("src/fragrance_graph") / name).read_text()
            assert not re.search(r"cap_usd\s*=\s*[0-9]", text), (
                f"{name} hardcodes a cap"
            )

    def test_the_paid_embedder_still_requires_a_hook(self):
        from fragrance_graph.semantic import OpenAIEmbeddings

        with pytest.raises(ValueError, match="on_spend"):
            OpenAIEmbeddings(api_key="k")

    def test_the_scheduler_still_cannot_charge_anything(self):
        import ast
        import pathlib

        import fragrance_graph.scheduler as module

        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "guard" not in called and "record" not in called


class TestConcurrentProcesses:
    """What overlapping runs can and cannot do to the cap.

    This class used to pin the opposite: `TestTheKnownConcurrencyGap`
    asserted that two runs together reached $1.80 against a $1.50 cap and
    documented it as a limit. The limit is now narrowed to one batch on the
    post-hoc path and removed entirely on the reserving path, so the tests
    assert the new behaviour and keep the old figure as the thing being
    prevented.

    The distinction that matters is *when the cost is known*:

        reserve -> pay -> settle     cost estimated first, cannot overshoot
        pay -> record                cost known only after, overshoots once

    Extraction is the second kind. No lock can un-spend an API call that
    already returned, so the guarantee there is "stops after one batch"
    rather than "never exceeds".
    """

    def test_the_post_hoc_path_stops_after_one_batch_instead_of_running_on(
        self, ledger
    ):
        """`guard` learns a batch's cost after paying it, so the first
        overshoot is unavoidable. What changed is that the second process
        now *sees* the first and halts, instead of continuing to spend."""
        first = Budget.load(ledger, today="2026-08-16")
        second = Budget.load(ledger, today="2026-08-16")

        first.guard("extract")(0.90, 1000)
        assert second.spent_usd == pytest.approx(0.0), "stale cache at load"

        with pytest.raises(BudgetExhausted):
            second.guard("extract")(0.90, 1000)

        assert second.spent_usd == pytest.approx(1.80), (
            "the second process re-read the ledger and saw the first's spend"
        )

    def test_a_reservation_refuses_when_another_process_already_committed(
        self, ledger
    ):
        """The reserving path has no overshoot at all: the money is on the
        ledger before the call, so the second arrival is refused."""
        first = Budget.load(ledger, today="2026-08-16")
        second = Budget.load(ledger, today="2026-08-16")

        first.reserve(0.90, "embed")
        with pytest.raises(BudgetExhausted, match="Nothing was reserved"):
            second.reserve(0.90, "embed")

        assert Budget.load(ledger, today="2026-08-16").spent_usd == pytest.approx(
            0.90
        ), "the refused reservation wrote nothing"

    def test_the_lock_makes_a_second_process_wait(self, ledger, tmp_path):
        """The proof that the lock excludes, rather than that it exists.

        Written this way because the obvious test does not work. Starting
        two processes together and asserting exactly one wins **passes with
        the lock removed** — process startup takes milliseconds and the
        window between reading the ledger and appending to it is
        microseconds, so the two almost never overlap. It was written,
        verified against a version with locking disabled, and found to pin
        nothing.

        This holds the lock in the parent for a wall-clock interval the
        child cannot miss, and asserts the child's reservation landed only
        after the parent let go.
        """
        import subprocess
        import sys
        import textwrap
        import time

        from fragrance_graph.budget import _locked

        finished = tmp_path / "child-finished"
        script = tmp_path / "racer.py"
        script.write_text(
            textwrap.dedent(
                f"""
                import time
                from fragrance_graph.budget import Budget
                budget = Budget.load({str(ledger)!r}, cap_usd=1.50,
                                     today="2026-08-16")
                budget.reserve(0.90, "race")
                open({str(finished)!r}, "w").write(repr(time.time()))
                """
            ),
            encoding="utf-8",
        )

        with _locked(ledger):
            child = subprocess.Popen([sys.executable, str(script)])
            time.sleep(0.5)
            assert not finished.exists(), (
                "the child reserved while the parent held the lock — "
                "the lock is not excluding anything"
            )
            released_at = time.time()

        assert child.wait(timeout=30) == 0
        assert float(finished.read_text()) >= released_at

    def test_two_processes_cannot_both_reserve_the_same_remainder(
        self, ledger, tmp_path
    ):
        """End-to-end, in real processes rather than one interpreter.

        Weaker than the exclusion test above — it would pass unlocked — but
        it exercises the whole path a paid worker takes, which the
        exclusion test deliberately does not.
        """
        import subprocess
        import sys
        import textwrap

        script = tmp_path / "worker.py"
        script.write_text(
            textwrap.dedent(
                f"""
                from fragrance_graph.budget import Budget, BudgetExhausted
                budget = Budget.load({str(ledger)!r}, cap_usd=1.50,
                                     today="2026-08-16")
                try:
                    budget.reserve(0.90, "race")
                    print("WON")
                except BudgetExhausted:
                    print("REFUSED")
                """
            ),
            encoding="utf-8",
        )

        runs = [
            subprocess.Popen(
                [sys.executable, str(script)], stdout=subprocess.PIPE, text=True
            )
            for _ in range(2)
        ]
        results = sorted(p.communicate()[0].strip() for p in runs)

        assert results == ["REFUSED", "WON"], results
        assert Budget.load(ledger, today="2026-08-16").spent_usd <= DAILY_CAP_USD

    def test_settling_corrects_the_estimate_without_rewriting_it(self, ledger):
        """Append-only: the estimate stays on the ledger and a second line
        carries the difference, so the history of the call survives."""
        budget = Budget.load(ledger, today="2026-08-16")
        reservation = budget.reserve(0.50, "embed")
        reservation.settle(0.02)

        assert Budget.load(ledger, today="2026-08-16").spent_usd == pytest.approx(
            0.02
        )
        lines = [
            json.loads(line)
            for line in ledger.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert [row["kind"] for row in lines] == ["reservation", "settlement"]
        assert lines[0]["usd"] == pytest.approx(0.50), "the estimate is not edited"

    def test_an_unsettled_reservation_stays_charged(self, ledger):
        """A process that dies mid-call leaves its claim standing. The day
        looks more expensive than it was, which is the safe direction."""
        budget = Budget.load(ledger, today="2026-08-16")
        with budget.reserve(0.40, "embed"):
            pass  # no settle — as if the process had died here

        assert Budget.load(ledger, today="2026-08-16").spent_usd == pytest.approx(
            0.40
        )

    def test_a_reservation_cannot_be_settled_twice(self, ledger):
        budget = Budget.load(ledger, today="2026-08-16")
        reservation = budget.reserve(0.10, "embed")
        reservation.settle(0.05)
        with pytest.raises(RuntimeError, match="already settled"):
            reservation.settle(0.05)

    def test_a_process_that_reloads_sees_the_other_and_stops(self, ledger):
        """The pre-existing mitigation, still true."""
        Budget.load(ledger, today="2026-08-16").guard("extract")(1.40, 1000)
        reloaded = Budget.load(ledger, today="2026-08-16")
        with pytest.raises(BudgetExhausted):
            reloaded.check(0.20)


class TestSmallChargesSurviveTheLedger:
    """A cap that cannot see many small calls is not a cap.

    The OpenAI embedding arm charges per descriptor: 1,092 calls at about
    $0.00000005 each. `record` rounded every line to six places, so each
    one persisted as 0.0 and the ledger showed $0.000001 against roughly
    $0.000058 charged. `spent_usd` was right in-process; the *file* was
    not, and the file is what the next container reads.

    Trivial in dollars. Not trivial in mechanism: this is the fifth
    escape of the class where real spend leaves no ledger trace.
    """

    def test_a_sub_micro_charge_is_not_rounded_to_nothing(self, tmp_path):
        from fragrance_graph.budget import Budget, spent_on

        ledger = tmp_path / "spend.jsonl"
        budget = Budget(cap_usd=1.0, ledger=ledger, today="2026-08-16")
        budget.record(0.00000005, "embed")
        assert spent_on(ledger, "2026-08-16") > 0, (
            "a charge that reaches the API must reach the ledger"
        )

    def test_many_small_charges_accumulate_on_disk(self, tmp_path):
        """The failure only shows in aggregate, which is exactly how it
        escaped notice: one call is a rounding error, a thousand is the
        whole bill."""
        from fragrance_graph.budget import Budget, spent_on

        ledger = tmp_path / "spend.jsonl"
        budget = Budget(cap_usd=1.0, ledger=ledger, today="2026-08-16")
        for _ in range(1092):
            budget.record(0.00000005, "embed")

        on_disk = spent_on(ledger, "2026-08-16")
        assert on_disk == pytest.approx(budget.spent_usd, rel=1e-6), (
            "what the file says must match what the process spent"
        )
        assert on_disk > 0.00005

    def test_a_reloaded_budget_sees_them(self, tmp_path):
        from fragrance_graph.budget import Budget

        ledger = tmp_path / "spend.jsonl"
        budget = Budget(cap_usd=1.0, ledger=ledger, today="2026-08-16")
        for _ in range(1000):
            budget.record(0.0000001, "embed")

        reloaded = Budget.load(ledger, today="2026-08-16")
        assert reloaded.spent_usd == pytest.approx(budget.spent_usd, rel=1e-6)
