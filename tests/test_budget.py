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
from fragrance_graph.daily import auto_approvable
from fragrance_graph.resolve.enrich import Proposal


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


# --- the auto-curation rule ------------------------------------------------


def make_proposal(**over):
    base = dict(
        mention="Khamrah",
        count=59,
        canonical_name="Lattafa Khamrah",
        brand="Lattafa",
        confident=True,
        corpus_mentions=-1,
    )
    base.update(over)
    return Proposal(**base)


def test_a_plain_bottle_with_no_flanker_question_is_auto_approved():
    """`corpus_mentions == -1` means the proposed name adds no word to the
    mention — it *is* the plain bottle. There is no decision to delegate."""
    assert auto_approvable(make_proposal()) is True


def test_a_flanker_nobody_wrote_is_held_not_taken():
    """The catalogue offered Baccarat Rouge 540 *Extrait* for a mention of
    Baccarat Rouge 540, and nobody in the corpus wrote "extrait". The right
    answer is in `alternatives`, and picking it is a judgement."""
    assert auto_approvable(make_proposal(corpus_mentions=0)) is False


def test_a_flanker_people_genuinely_discuss_is_held():
    """Khamrah Qahwa scores 9. That means "this exists too", not "you
    picked wrong" — and it wants its own entry rather than replacing the
    plain bottle."""
    assert auto_approvable(make_proposal(corpus_mentions=9)) is False


def test_a_name_that_differs_from_the_mention_is_held():
    assert auto_approvable(make_proposal(confident=False)) is False


def test_a_mention_with_no_catalogue_match_is_never_written():
    """A name the catalogue never returned cannot be invented here."""
    assert auto_approvable(make_proposal(canonical_name=None)) is False
    assert auto_approvable(make_proposal(brand=None)) is False


def test_a_human_decision_is_never_overruled():
    """Both directions: an explicit rejection stays rejected, and an
    explicit approval is not re-derived from the rule."""
    assert auto_approvable(make_proposal(approved=False)) is False
    assert auto_approvable(make_proposal(approved=True)) is False


def test_a_rare_mention_is_not_worth_writing():
    """Below the 3-commenter bar a mention cannot reach a page even if
    every mention were a different person."""
    assert auto_approvable(make_proposal(count=2)) is False
    assert auto_approvable(make_proposal(count=3)) is True


# --- the cap, wired into extraction ---------------------------------------


def test_extraction_halts_mid_run_and_leaves_the_rest_pending(conn, ledger):
    """The whole point, end to end.

    Six comments, batches of two, a cap that runs out after the second
    batch. Extraction must stop rather than finish the run, and everything
    it did not reach must keep `extracted_at` NULL so tomorrow resumes
    instead of re-paying or skipping.
    """
    import json as _json

    from fragrance_graph.extract.llm import extract
    from fragrance_graph.ingest.store import ingest
    from tests.conftest import make_comment
    from tests.test_extract import FakeClient, FakeResponse, FakeUsage

    ingest(conn, [make_comment(i, body=f"comment {i}") for i in range(6)])

    empty = _json.dumps(
        {"results": [{"comment_index": 0, "claims": []},
                     {"comment_index": 1, "claims": []}]}
    )
    # ~$0.00035 per batch at 100 in / 50 out, so a cap of $0.0007 buys two.
    client = FakeClient(
        [FakeResponse(empty, usage=FakeUsage(100, 50)) for _ in range(3)]
    )
    budget = Budget.load(ledger, today="2026-08-11", cap_usd=0.0007)

    with pytest.raises(BudgetExhausted):
        extract(
            conn, client, limit=6, batch_size=2,
            on_spend=budget.guard("extract"),
        )

    assert client.calls == 2, "should not have called the API a third time"
    pending = conn.execute(
        "SELECT count(*) FROM comments WHERE extracted_at IS NULL"
    ).fetchone()[0]
    assert pending == 2, "the unreached batch must resume on the next run"


def test_extraction_with_no_guard_is_unchanged(conn):
    """The cap is opt-in. Attended runs answer to a person watching the
    number, and must not acquire a hidden ceiling."""
    import json as _json

    from fragrance_graph.extract.llm import extract
    from fragrance_graph.ingest.store import ingest
    from tests.conftest import make_comment
    from tests.test_extract import FakeClient, FakeResponse

    ingest(conn, [make_comment(i, body=f"comment {i}") for i in range(4)])
    empty = _json.dumps(
        {"results": [{"comment_index": 0, "claims": []},
                     {"comment_index": 1, "claims": []}]}
    )
    client = FakeClient([FakeResponse(empty) for _ in range(2)])

    cost = extract(conn, client, limit=4, batch_size=2)
    assert cost.comments == 4
    assert client.calls == 2
