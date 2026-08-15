"""Deciding what to buy next, and refusing to buy the wrong things.

    uv run python -m fragrance_graph.scheduler plan
    uv run python -m fragrance_graph.scheduler plan --budget 0.40

Two scarce things, and they are not interchangeable. YouTube search is
rationed by a daily quota of 10,000 units, of which one search costs 100 —
so the real budget is a hundred searches a day. Reading comments with a
language model is rationed by money, capped at a dollar. Confusing them is
what produced a run that probed forty bottles for free and then spent the
whole cap on nine.

## Why classes and not a score

The tempting design is one formula: value times freshness over cost,
tuned. It is also unfalsifiable — when it schedules something silly you
cannot say which term did it — and nothing here has been measured well
enough to justify coefficients. So work is sorted into a handful of named
classes in a fixed order, and *within* a class by one honest number.

    1. releases due a re-probe        costs quota, never money
    2. new releases now discussed     the freshest thing anybody wants
    3. pairs one person short         cheapest possible page
    4. under-covered known bottles    widens what can be answered
    5. refresh well-covered bottles   only when nothing else is due

A person can read that list and predict what the scheduler will do, which
is the property a weighted score gives up first. When measurements exist
to justify a real ranking, the classes are the thing to replace — and the
comparison to beat.

## The scheduler understands lifecycle

A new release with nobody talking about it is not a cheap job, it is the
*wrong* job: extraction would buy silence. That release belongs in class 1
paying quota for a re-probe, and it cannot reach class 2 until enough
separate creators exist. This is the whole reason `releases.py` has six
states rather than a boolean.

## Nothing here spends anything

`plan` reads and returns a list. Executing it is somebody else's call, and
every task carries what it would cost so that call can be made with the
numbers in front of it.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum

import psycopg

from fragrance_graph.budget import DAILY_CAP_USD, Budget
from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.enrich import Mode, near_misses
from fragrance_graph.evidence import Attribution, attribute_facts
from fragrance_graph.ingest.youtube import COST_SEARCH, DAILY_QUOTA
from fragrance_graph.releases import READY_CREATORS

log = logging.getLogger("fragrance_graph.scheduler")


class Priority(IntEnum):
    """Named classes, in the order they are served."""

    READINESS_PROBE = 1
    NEW_RELEASE = 2
    PAIR_NEAR_MISS = 3
    UNDER_COVERED = 4
    REFRESH = 5


#: A bottle with fewer facts than this is under-covered enough that
#: learning about it is likely to add something. Not tuned — it is the
#: median fact count of the bottles the recommender can currently say
#: anything about, so it means "below what already works".
UNDER_COVERED_FACTS = 8

#: Rough cost of a learn or verify job, from the measured extraction rate
#: of ~$0.42 per 1,000 comments against a 400-comment ceiling.
ESTIMATED_JOB_USD = 0.17


@dataclass(frozen=True)
class Task:
    """One thing worth doing, and what it would cost to do it."""

    priority: Priority
    kind: str
    target: str
    reason: str
    fragrance_id: int | None = None
    pair: tuple[int, int] | None = None
    release_id: int | None = None
    quota_units: int = 0
    estimated_usd: float = 0.0

    @property
    def spends_money(self) -> bool:
        return self.estimated_usd > 0

    def __str__(self) -> str:
        cost = (
            f"${self.estimated_usd:.2f}"
            if self.spends_money
            else f"{self.quota_units} quota"
        )
        return f"[{self.priority.name:<15}] {self.target:<44} {cost:>10}  {self.reason}"


@dataclass
class Plan:
    """What the scheduler would do, and what it refused to do."""

    tasks: list[Task] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    budget_remaining: float = 0.0
    quota_remaining: int = 0

    @property
    def money_tasks(self) -> list[Task]:
        return [t for t in self.tasks if t.spends_money]

    @property
    def projected_usd(self) -> float:
        return sum(t.estimated_usd for t in self.tasks)

    @property
    def projected_quota(self) -> int:
        return sum(t.quota_units for t in self.tasks)

    def render(self) -> str:
        lines = [
            f"budget remaining ${self.budget_remaining:.4f}, "
            f"quota remaining {self.quota_remaining} units",
            "",
        ]
        lines += [str(task) for task in self.tasks] or ["  (nothing to do)"]
        lines += [
            "",
            f"would spend ${self.projected_usd:.4f} and "
            f"{self.projected_quota} quota units",
        ]
        if self.skipped:
            lines += ["", "not scheduled:"]
            lines += [f"  {reason}" for reason in self.skipped]
        return "\n".join(lines)


def _probe_due(conn: psycopg.Connection, now: str) -> list[Task]:
    rows = conn.execute(
        "SELECT rc.id, rc.fragrance_id, rc.brand, rc.name,"
        "       rc.relevant_creators, rc.probe_count, f.canonical_name"
        "  FROM release_candidates rc"
        "  LEFT JOIN fragrances f ON f.id = rc.fragrance_id"
        " WHERE rc.status IN ('CATALOGED', 'WAITING_FOR_DISCUSSION')"
        "   AND (rc.next_probe_at IS NULL OR rc.next_probe_at <= %s)"
        # One probe per *bottle*, not per announcement. Two outlets
        # covering one launch is two rows and one thing to find out, and
        # probing it twice spends a hundred quota units to learn the same
        # number twice.
        " ORDER BY COALESCE(rc.fragrance_id, -rc.id), rc.probe_count, rc.id",
        (now,),
    ).fetchall()
    seen: set[int] = set()
    unique = []
    for row in rows:
        key = row["id"] if row["fragrance_id"] is None else row["fragrance_id"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return [
        Task(
            priority=Priority.READINESS_PROBE,
            kind="probe-release",
            target=row["canonical_name"] or f"{row['brand']} {row['name']}".strip(),
            reason=(
                f"{row['relevant_creators']}/{READY_CREATORS} creators at last "
                f"probe (#{row['probe_count']}); costs quota, not money"
            ),
            release_id=row["id"],
            quota_units=COST_SEARCH,
        )
        for row in unique
    ]


def _ready_releases(conn: psycopg.Connection) -> list[Task]:
    rows = conn.execute(
        "SELECT rc.id, rc.fragrance_id, rc.relevant_creators, f.canonical_name"
        "  FROM release_candidates rc"
        "  JOIN fragrances f ON f.id = rc.fragrance_id"
        " WHERE rc.status = 'ENRICHABLE'"
        " ORDER BY rc.relevant_creators DESC, rc.id"
    ).fetchall()
    return [
        Task(
            priority=Priority.NEW_RELEASE,
            kind=Mode.LEARN,
            target=row["canonical_name"],
            reason=(
                f"new release, {row['relevant_creators']} creators already "
                "discussing it"
            ),
            fragrance_id=row["fragrance_id"],
            release_id=row["id"],
            quota_units=COST_SEARCH,
            estimated_usd=ESTIMATED_JOB_USD,
        )
        for row in rows
    ]


def _near_miss_tasks(conn: psycopg.Connection) -> list[Task]:
    return [
        Task(
            priority=Priority.PAIR_NEAR_MISS,
            kind=Mode.VERIFY,
            target=f"{miss.a_name} vs {miss.b_name}",
            reason=f"{miss.people} people, {miss.creators} creators — needs "
                   f"{miss.needs}",
            pair=(miss.a_id, miss.b_id),
            quota_units=COST_SEARCH,
            estimated_usd=ESTIMATED_JOB_USD,
        )
        for miss in near_misses(conn)
    ]


def _coverage_tasks(conn: psycopg.Connection) -> tuple[list[Task], list[Task]]:
    """Bottles split by whether the corpus can already say much about them."""
    counts: dict[int, int] = {}
    for fact in attribute_facts(conn, attribution=Attribution.PROPOSED):
        counts[fact.fragrance_id] = counts.get(fact.fragrance_id, 0) + 1

    thin, thick = [], []
    for row in conn.execute(
        "SELECT id, canonical_name FROM fragrances ORDER BY canonical_name"
    ):
        facts = counts.get(row["id"], 0)
        task = Task(
            priority=(
                Priority.UNDER_COVERED if facts < UNDER_COVERED_FACTS
                else Priority.REFRESH
            ),
            kind=Mode.LEARN,
            target=row["canonical_name"],
            reason=f"{facts} attribute fact(s) recorded",
            fragrance_id=row["id"],
            quota_units=COST_SEARCH,
            estimated_usd=ESTIMATED_JOB_USD,
        )
        (thin if facts < UNDER_COVERED_FACTS else thick).append(task)
    thin.sort(key=lambda t: t.reason)
    return thin, thick


def plan(
    conn: psycopg.Connection,
    *,
    budget: Budget | None = None,
    quota_used: int = 0,
    limit: int = 12,
    now: str | None = None,
) -> Plan:
    """What to do next, in order, within both budgets.

    Money and quota are checked separately and a task is dropped for
    whichever runs out first. A readiness probe survives an exhausted
    wallet because it costs no money — which is the point of tracking them
    apart, and the reason a day with no budget left is still a day the
    system can learn something.
    """
    now = now or datetime.now(UTC).isoformat(timespec="seconds")
    budget = budget or Budget.load()
    result = Plan(
        budget_remaining=budget.remaining_usd,
        quota_remaining=max(0, DAILY_QUOTA - quota_used),
    )

    thin, thick = _coverage_tasks(conn)
    ordered = (
        _probe_due(conn, now)
        + _ready_releases(conn)
        + _near_miss_tasks(conn)
        + thin
        + thick
    )

    spent = 0.0
    quota = 0
    for task in ordered:
        if len(result.tasks) >= limit:
            result.skipped.append(
                f"{len(ordered) - len(result.tasks)} more task(s) beyond the "
                f"limit of {limit}"
            )
            break
        if quota + task.quota_units > result.quota_remaining:
            result.skipped.append(f"{task.target}: no search quota left")
            continue
        if task.spends_money and spent + task.estimated_usd > budget.remaining_usd:
            result.skipped.append(
                f"{task.target}: ${task.estimated_usd:.2f} would cross the "
                f"daily cap (${budget.remaining_usd:.4f} left)"
            )
            continue
        result.tasks.append(task)
        spent += task.estimated_usd
        quota += task.quota_units
    return result


def render_status(conn: psycopg.Connection) -> str:
    """The two scarce things, side by side."""
    budget = Budget.load()
    lines = [
        f"money   ${budget.spent_usd:.4f} spent of ${budget.cap_usd:.2f} "
        f"(${budget.remaining_usd:.4f} left)",
        f"quota   {DAILY_QUOTA} units/day = "
        f"{DAILY_QUOTA // COST_SEARCH} searches",
        "",
    ]
    for row in conn.execute(
        "SELECT status, count(*) AS n FROM release_candidates GROUP BY status"
        " ORDER BY status"
    ):
        lines.append(f"releases  {row['status']:<24} {row['n']}")
    lines.append(f"near-miss pairs: {len(near_misses(conn))}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.scheduler",
        description="Decide what to buy next. Reads only; spends nothing.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan", help="What the scheduler would do next")
    p.add_argument("--limit", type=int, default=12)
    p.add_argument(
        "--budget", type=float, default=None,
        help=f"Override remaining budget for a what-if (cap ${DAILY_CAP_USD})",
    )
    p.add_argument("--quota-used", type=int, default=0)
    sub.add_parser("status", help="Money and quota side by side")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "status":
            print(render_status(conn))
            return 0
        budget = None
        if args.budget is not None:
            # A what-if, not a way to raise the cap: `Budget.guard` is what
            # enforces spending and nothing here can reach it.
            budget = Budget(cap_usd=args.budget, spent_usd=0.0)
        print(
            plan(
                conn, budget=budget, quota_used=args.quota_used, limit=args.limit
            ).render()
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
