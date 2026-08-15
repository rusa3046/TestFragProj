"""Deciding what to buy next, and refusing to buy the wrong things.

The tests that matter are the refusals. A scheduler that spends is easy;
one that declines to enrich a release nobody is discussing, and keeps
probing it for free, is the thing worth pinning.
"""

import json

import pytest

from fragrance_graph.budget import Budget
from fragrance_graph.ingest.store import ingest
from fragrance_graph.releases import FixtureSource, discover, verify
from fragrance_graph.resolve.entities import add_fragrance
from fragrance_graph.scheduler import (
    ESTIMATED_JOB_USD,
    Priority,
    plan,
)
from tests.conftest import make_comment


@pytest.fixture
def rich_budget():
    return Budget(cap_usd=10.0, spent_usd=0.0)


@pytest.fixture
def broke_budget():
    return Budget(cap_usd=1.0, spent_usd=1.0)


@pytest.fixture
def announced(conn, tmp_path):
    def announce(name="Khamrah Rouge", brand="Lattafa"):
        path = tmp_path / "a.jsonl"
        path.write_text(json.dumps({
            "source_id": "rss-1", "name": name, "brand": brand,
            "announced_at": "2026-08-01",
        }) + "\n")
        discover(conn, FixtureSource(path))
        verify(conn)
        return conn.execute(
            "SELECT id, fragrance_id FROM release_candidates"
        ).fetchone()
    return announce


def pair_claim(conn, i, *, subject, obj, author, channel):
    body = f"comment {i}"
    ingest(conn, [make_comment(i, body=body, source_channel=channel,
                               raw_json=json.dumps({"author": author}))])
    cid = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             polarity, extraction_model, created_at)
        VALUES (%s, 'SIMILAR_TO', 'FRAGRANCE', 'it', %s, 'FRAGRANCE',
                'other', %s, 'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test',
                '2026-01-01')
        """,
        (cid, subject, obj, body),
    )
    conn.commit()


class TestLifecycleAwareness:
    def test_an_undiscussed_release_is_probed_not_enriched(
        self, conn, announced, rich_budget
    ):
        """Extraction would buy silence. The right job is a free probe."""
        announced()
        result = plan(conn, budget=rich_budget)
        probes = [t for t in result.tasks if t.priority is Priority.READINESS_PROBE]
        assert probes
        assert not probes[0].spends_money
        assert not [
            t for t in result.tasks if t.priority is Priority.NEW_RELEASE
        ]

    def test_a_discussed_release_becomes_a_money_job(
        self, conn, announced, rich_budget
    ):
        row = announced()
        conn.execute(
            "UPDATE release_candidates SET status = 'ENRICHABLE',"
            " relevant_creators = 4 WHERE id = %s", (row["id"],)
        )
        conn.commit()
        result = plan(conn, budget=rich_budget)
        releases = [t for t in result.tasks if t.priority is Priority.NEW_RELEASE]
        assert releases
        assert releases[0].spends_money

    def test_one_launch_announced_twice_is_probed_once(
        self, conn, tmp_path, rich_budget
    ):
        """Two outlets, one thing to find out. Probing twice spends a
        hundred quota units to learn the same number twice."""
        path = tmp_path / "a.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in [
            {"source_id": "rss-1", "name": "Delina La Rosee",
             "brand": "Parfums de Marly", "announced_at": "2026-08-01"},
            {"source_id": "shop-2", "name": "Delina La Rosée",
             "brand": "Parfums de Marly", "announced_at": "2026-08-02"},
        ]) + "\n")
        discover(conn, FixtureSource(path))
        verify(conn)
        result = plan(conn, budget=rich_budget)
        probes = [t for t in result.tasks if t.priority is Priority.READINESS_PROBE]
        assert len(probes) == 1


class TestPriorityOrder:
    def test_free_work_comes_before_paid_work(self, conn, announced, rich_budget):
        announced()
        add_fragrance(conn, "Lattafa Khamrah")
        tasks = plan(conn, budget=rich_budget).tasks
        priorities = [t.priority for t in tasks]
        assert priorities == sorted(priorities), "classes are served in order"
        assert tasks[0].priority is Priority.READINESS_PROBE

    def test_near_misses_outrank_general_learning(self, conn, rich_budget):
        """The cheapest possible page: a pair one person short."""
        a = add_fragrance(conn, "Creed Aventus")
        b = add_fragrance(conn, "Nishane Hacivat")
        for i, (author, chan) in enumerate([("p1", "c1"), ("p2", "c2")]):
            pair_claim(conn, i, subject=b, obj=a, author=author, channel=chan)
        tasks = plan(conn, budget=rich_budget).tasks
        kinds = [t.priority for t in tasks]
        assert Priority.PAIR_NEAR_MISS in kinds
        assert kinds.index(Priority.PAIR_NEAR_MISS) < kinds.index(
            Priority.UNDER_COVERED
        )

    def test_a_well_covered_bottle_is_last(self, conn, rich_budget):
        add_fragrance(conn, "Lattafa Khamrah")
        tasks = plan(conn, budget=rich_budget, limit=99).tasks
        assert tasks[-1].priority in (Priority.UNDER_COVERED, Priority.REFRESH)


class TestBudgetsAreHardAndSeparate:
    def test_an_exhausted_wallet_stops_money_work(self, conn, announced,
                                                  broke_budget):
        row = announced()
        conn.execute(
            "UPDATE release_candidates SET status = 'ENRICHABLE',"
            " relevant_creators = 4 WHERE id = %s", (row["id"],)
        )
        conn.commit()
        result = plan(conn, budget=broke_budget)
        assert result.money_tasks == []
        assert any("daily cap" in s for s in result.skipped)

    def test_but_free_probes_still_run(self, conn, announced, broke_budget):
        """The point of tracking the two apart: a day with no money left is
        still a day the system can learn something."""
        announced()
        result = plan(conn, budget=broke_budget)
        assert [t for t in result.tasks if t.priority is Priority.READINESS_PROBE]

    def test_exhausted_quota_stops_everything(self, conn, announced,
                                              rich_budget):
        announced()
        result = plan(conn, budget=rich_budget, quota_used=10_000)
        assert result.tasks == []
        assert any("quota" in s for s in result.skipped)

    def test_the_plan_never_exceeds_the_remaining_budget(self, conn, rich_budget):
        for i in range(30):
            add_fragrance(conn, f"Test Bottle {i}")
        budget = Budget(cap_usd=0.50, spent_usd=0.0)
        result = plan(conn, budget=budget, limit=99)
        assert result.projected_usd <= budget.remaining_usd

    def test_scheduling_spends_nothing_by_itself(self, conn, announced,
                                                 rich_budget, tmp_path):
        """`plan` reads and returns a list. Executing it is somebody
        else's call."""
        ledger = tmp_path / "spend.jsonl"
        budget = Budget(cap_usd=10.0, ledger=ledger)
        announced()
        plan(conn, budget=budget)
        assert not ledger.exists(), "no ledger line was written"

    def test_the_budget_override_cannot_raise_the_real_cap(self):
        """`--budget` is a what-if. Enforcement lives in `Budget.guard`,
        which nothing in the scheduler can reach."""
        import ast
        import pathlib

        import fragrance_graph.scheduler as module

        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert "guard" not in called
        assert "record" not in called


class TestTheClassesAreReadable:
    def test_every_task_says_why(self, conn, announced, rich_budget):
        announced()
        add_fragrance(conn, "Lattafa Khamrah")
        for task in plan(conn, budget=rich_budget).tasks:
            assert task.reason, f"{task.target} has no stated reason"

    def test_every_task_says_what_it_costs(self, conn, announced, rich_budget):
        announced()
        for task in plan(conn, budget=rich_budget).tasks:
            assert task.quota_units or task.estimated_usd

    def test_a_money_job_costs_the_measured_rate(self, conn, announced,
                                                 rich_budget):
        row = announced()
        conn.execute(
            "UPDATE release_candidates SET status = 'ENRICHABLE' WHERE id = %s",
            (row["id"],),
        )
        conn.commit()
        (task,) = [
            t for t in plan(conn, budget=rich_budget).tasks
            if t.priority is Priority.NEW_RELEASE
        ]
        assert task.estimated_usd == ESTIMATED_JOB_USD
