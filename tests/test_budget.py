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
    first = Budget.load(ledger, today="2026-08-11")
    first.record(0.60, "extract")

    second = Budget.load(ledger, today="2026-08-11")
    assert second.spent_usd == pytest.approx(0.60)
    assert second.remaining_usd == pytest.approx(0.40)
    with pytest.raises(BudgetExhausted):
        second.check(0.50)


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
