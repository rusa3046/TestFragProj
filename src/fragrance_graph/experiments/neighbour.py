"""Does a neighbour's review section teach us about the anchor?

    uv run python -m fragrance_graph.experiments.neighbour baseline  # free
    uv run python -m fragrance_graph.experiments.neighbour run       # $0.20

The design is frozen in `data/experiments/neighbour-prediction.md` and was
committed before this module existed. Read that first; this only executes
it. The constants below are copied from it deliberately rather than made
configurable — a flag is a way to change the experiment after seeing the
results, which is the failure the prediction file exists to prevent.

## What is being tested

Spillover is already known to happen: run 2 spent money on seven targets
and 134 of 348 claims landed on some other bottle. What is *not* known is
whether it can be predicted before spending, which is the only version a
scheduler can use.

So: pick a query the system cannot answer, pick the neighbour by a rule
stated in advance, and buy both the anchor's own reviews and the
neighbour's under identical ceilings.

## Why the primary metric is so narrow

The gap is `"Baccarat Rouge 540 but less sweet"`, blocked because BR540's
sweetness rests on one person on one channel. A neighbour run that returns
plenty of BR540 facts about *longevity* would look like a win and would
not move the query one inch.

So the primary outcome is evidence on the **sweetness axis specifically**,
counted the way `coverage.relative_coverage` already counts it, and a run
passes only if it also adds an independent creator there. Everything else
— BR540 facts overall, the arm's own target, total graph yield — is
recorded as secondary, and the sparse confound is read off the third one.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg

from fragrance_graph.coverage import relative_coverage
from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evidence import Attribution, attribute_facts

log = logging.getLogger("fragrance_graph.experiments.neighbour")

# --- frozen by data/experiments/neighbour-prediction.md ---------------------

ANCHOR = "Maison Francis Kurkdjian Baccarat Rouge 540"
ATTRIBUTE = "sweet"

#: Arm A buys the anchor's own reviews; arm B buys the neighbour's. Order
#: matters and is A first, which biases *against* the hypothesis: a fact
#: both arms could have found is credited to A, because B's gain is
#: measured from a snapshot taken after A has run.
ARM_A = ANCHOR
ARM_B = "Thomas Kosmala No. 4"

#: Per arm. Identical, or the comparison means nothing.
CEILING_USD = 0.10

RESULT_PATH = Path("data/experiments/neighbour-result.md")
PREDICTION_PATH = Path("data/experiments/neighbour-prediction.md")


# --- measurement ------------------------------------------------------------


def _fragrance_id(conn: psycopg.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM fragrances WHERE lower(canonical_name) = lower(%s)",
        (name,),
    ).fetchone()
    return row["id"] if row else None


def axis_creators(conn: psycopg.Connection) -> set[str]:
    """The channels behind the anchor's sweetness evidence, by name.

    Counted through `recommend._matches`, the same predicate
    `relative_coverage` uses to pick the anchor fact, so the primary metric
    and the pass/fail gate cannot disagree about which claims are on the
    axis. Returned as a set rather than a number because "at least one
    *new* independent creator" is the condition, and a count cannot tell a
    new channel from a replaced one.

    The first version matched on `"sweet" in value or "sweet" in attribute`
    and found two creators where `relative_coverage` found one — a gate and
    a metric disagreeing about the same fact, caught on the free baseline
    before anything was bought.
    """
    from fragrance_graph.coverage import _strongest

    anchor_id = _fragrance_id(conn, ANCHOR)
    if anchor_id is None:
        return set()
    fact = _strongest(
        attribute_facts(
            conn, fragrance_id=anchor_id, attribution=Attribution.STATED
        ),
        ATTRIBUTE,
    )
    claim_ids: list[int] = list(fact.stated.claim_ids) if fact else []
    if not claim_ids:
        return set()
    return {
        row["source_channel"]
        for row in conn.execute(
            "SELECT DISTINCT co.source_channel FROM claims c "
            "JOIN comments co ON co.id = c.comment_id WHERE c.id = ANY(%s)",
            (claim_ids,),
        )
    }


def _stated_facts(conn: psycopg.Connection, fragrance_id: int | None) -> int:
    if fragrance_id is None:
        return 0
    return sum(
        1
        for fact in attribute_facts(
            conn, fragrance_id=fragrance_id, attribution=Attribution.STATED
        )
        if fact.supporting.people > 0
    )


@dataclass
class Snapshot:
    """Everything the prediction file says to record, at one moment."""

    #: PRIMARY — the frozen gap.
    sweet_people: int = 0
    sweet_creators: int = 0
    sweet_creator_ids: tuple[str, ...] = ()
    answerable: bool = False
    blocker: str = ""
    #: Secondary.
    anchor_facts: int = 0
    arm_a_target_facts: int = 0
    arm_b_target_facts: int = 0
    total_facts: int = 0


def snapshot(conn: psycopg.Connection) -> Snapshot:
    """Read-only. Spends nothing and writes nothing."""
    case = relative_coverage(conn, cases=((ANCHOR, ATTRIBUTE),))[0]
    creators = axis_creators(conn)
    return Snapshot(
        sweet_people=case.anchor_people,
        sweet_creators=case.anchor_creators,
        sweet_creator_ids=tuple(sorted(creators)),
        answerable=case.answerable,
        blocker=case.blocker,
        anchor_facts=_stated_facts(conn, _fragrance_id(conn, ANCHOR)),
        arm_a_target_facts=_stated_facts(conn, _fragrance_id(conn, ARM_A)),
        arm_b_target_facts=_stated_facts(conn, _fragrance_id(conn, ARM_B)),
        total_facts=sum(
            1
            for fact in attribute_facts(conn, attribution=Attribution.STATED)
            if fact.supporting.people > 0
        ),
    )


@dataclass
class ArmResult:
    """One arm's spend and what the corpus gained because of it."""

    arm: str
    target: str
    before: Snapshot
    after: Snapshot
    comments_read: int = 0
    usd: float = 0.0
    quota_units: int = 0
    stop_reason: str = ""
    note: str = ""

    @property
    def new_sweet_creators(self) -> tuple[str, ...]:
        return tuple(
            sorted(set(self.after.sweet_creator_ids) - set(self.before.sweet_creator_ids))
        )

    @property
    def new_sweet_people(self) -> int:
        return self.after.sweet_people - self.before.sweet_people

    @property
    def sweet_per_dollar(self) -> float:
        """The primary metric. Zero spend with zero gain is zero, not
        infinity — an arm that bought nothing did not win."""
        if self.usd <= 0:
            return 0.0
        return self.new_sweet_people / self.usd

    @property
    def new_anchor_facts(self) -> int:
        return self.after.anchor_facts - self.before.anchor_facts

    @property
    def new_target_facts(self) -> int:
        field_name = (
            "arm_a_target_facts" if self.arm == "A" else "arm_b_target_facts"
        )
        return getattr(self.after, field_name) - getattr(self.before, field_name)

    @property
    def new_total_facts(self) -> int:
        return self.after.total_facts - self.before.total_facts


def verdict(a: ArmResult, b: ArmResult) -> tuple[bool, str]:
    """Pass or fail, against the two frozen conditions and nothing else."""
    beats = b.sweet_per_dollar > a.sweet_per_dollar
    gained_creator = bool(b.new_sweet_creators)
    if beats and gained_creator:
        return True, (
            "B beat A on independent sweetness evidence per dollar and added "
            f"{len(b.new_sweet_creators)} new creator(s) to the axis."
        )
    if not beats and not gained_creator:
        return False, (
            "B did not beat A on the sweetness axis and added no creator. "
            "Neighbourhood is not a schedulable signal on this evidence."
        )
    if not beats:
        return False, (
            "B added a creator but did not beat A per dollar. Condition 1 "
            "fails; the frozen test requires both."
        )
    return False, (
        "B beat A per dollar but added no new creator, so the blocked query "
        "is no closer to answerable. Condition 2 fails."
    )


# --- the run ----------------------------------------------------------------


def run_arm(
    conn: psycopg.Connection,
    *,
    arm: str,
    target: str,
    buy: Any,
) -> ArmResult:
    """Snapshot, buy, re-derive, snapshot.

    `buy` returns `(comments_read, usd, quota_units, stop_reason)` and is
    injected so the whole measurement can be exercised without a key, a
    quota or a cent.

    The re-derivation between the two snapshots is not optional. Extraction
    writes the words a commenter typed; until `backfill` resolves those to
    a bottle the evidence layer cannot see them, and a run measured without
    it reports zero for work it actually did. That mistake has been made
    once already in this repository, and it under-reported an experiment by
    3x.
    """
    from fragrance_graph.attributes import record_inferences
    from fragrance_graph.resolve.entities import backfill

    before = snapshot(conn)
    comments_read, usd, quota_units, stop_reason = buy(target)
    backfill(conn)
    record_inferences(conn)
    conn.commit()
    return ArmResult(
        arm=arm,
        target=target,
        before=before,
        after=snapshot(conn),
        comments_read=comments_read,
        usd=usd,
        quota_units=quota_units,
        stop_reason=stop_reason,
    )


def _real_buyer(conn: psycopg.Connection, budget: Any, run_id: str) -> Any:
    """The production buyer: one search, then comments, under the ceiling."""
    from fragrance_graph.frontier import (
        Ceiling,
        Trial,
        budgeted_extractor,
        enrich_one,
        query_for,
        search_with_creators,
        store_hits,
    )
    from fragrance_graph.ingest.youtube import QuotaTracker, build_client

    client, api_key = build_client()
    quota = QuotaTracker()
    extract_fn = budgeted_extractor(_llm_client(), budget)
    ceiling = Ceiling(max_usd=CEILING_USD)

    def buy(target: str):
        from fragrance_graph.experiments.attribute_gain import _candidate_for

        match = _candidate_for(conn, target)
        if match is None:
            return 0, 0.0, 0, "not-in-catalogue"
        trial = Trial(
            text=match.text,
            claims_before=match.claims,
            creators_before=match.creators,
            started_at=datetime.now(UTC).isoformat(timespec="seconds"),
            query=query_for(match.text),
        )
        hits = search_with_creators(
            client, api_key, trial.query, limit=25, quota=quota
        )
        store_hits(conn, hits, trial.query, run=run_id)
        trial.searches = 1
        enrich_one(
            conn, client, api_key, match, quota=quota, ceiling=ceiling,
            run=run_id, extract_fn=extract_fn, trial=trial,
        )
        return trial.comments_seen, trial.usd, trial.quota_units, trial.stop_reason

    return buy


def _llm_client() -> Any:  # pragma: no cover - needs a key
    from fragrance_graph.extract.llm import build_client

    return build_client()


# --- reporting --------------------------------------------------------------


def render(a: ArmResult, b: ArmResult, *, prediction_sha: str = "") -> str:
    passed, why = verdict(a, b)
    lines = [
        "# Result — does a neighbour's review section teach us about the anchor?",
        "",
        f"Run {datetime.now(UTC).isoformat(timespec='seconds')}. Design frozen in "
        f"`{PREDICTION_PATH}`"
        + (f" at `{prediction_sha}`" if prediction_sha else "")
        + ", before any spend.",
        "",
        f"## {'PASS' if passed else 'FAIL'}",
        "",
        why,
        "",
        "## Primary — independent stated BR540 sweetness evidence",
        "",
        "```",
        f"{'':<22}{'people':>8}{'creators':>10}{'$':>9}{'people/$':>10}",
        f"{'before both arms':<22}{a.before.sweet_people:>8}"
        f"{a.before.sweet_creators:>10}{'':>9}{'':>10}",
        f"{'A  direct (' + a.target[:9] + ')':<22}{a.after.sweet_people:>8}"
        f"{a.after.sweet_creators:>10}{a.usd:>9.4f}{a.sweet_per_dollar:>10.1f}",
        f"{'B  neighbour':<22}{b.after.sweet_people:>8}"
        f"{b.after.sweet_creators:>10}{b.usd:>9.4f}{b.sweet_per_dollar:>10.1f}",
        "```",
        "",
        f"New creators on the axis — A: {list(a.new_sweet_creators) or 'none'}, "
        f"B: {list(b.new_sweet_creators) or 'none'}",
        "",
        f"`\"BR540 but less sweet\"` answerable: "
        f"{'yes' if b.after.answerable else 'no'}"
        + (f" — blocker: {b.after.blocker}" if b.after.blocker else ""),
        "",
        "## Secondary, and the sparse confound",
        "",
        "```",
        f"{'':<22}{'BR540':>8}{'own target':>12}{'any bottle':>12}"
        f"{'comments':>10}{'$':>9}",
        f"{'A  direct':<22}{a.new_anchor_facts:>8}{a.new_target_facts:>12}"
        f"{a.new_total_facts:>12}{a.comments_read:>10}{a.usd:>9.4f}",
        f"{'B  neighbour':<22}{b.new_anchor_facts:>8}{b.new_target_facts:>12}"
        f"{b.new_total_facts:>12}{b.comments_read:>10}{b.usd:>9.4f}",
        "```",
        "",
        "Read the confound here: **own target** is what the sparse effect "
        "predicts, **BR540** is what neighbourhood predicts. A large first "
        "column with an empty second is the sparse effect reproducing and "
        "the neighbourhood claim failing.",
        "",
        f"Stopped because — A: {a.stop_reason or 'n/a'}, B: {b.stop_reason or 'n/a'}",
        f"Quota — A: {a.quota_units} units, B: {b.quota_units} units",
        f"Total spend: ${a.usd + b.usd:.4f}",
    ]
    return "\n".join(lines)


def _as_row(result: ArmResult) -> dict:
    row = asdict(result)
    row["before"] = asdict(result.before)
    row["after"] = asdict(result.after)
    row["new_sweet_creators"] = list(result.new_sweet_creators)
    row["sweet_per_dollar"] = result.sweet_per_dollar
    return row


@dataclass
class RunLog:
    """Append-only record, written before the markdown so a crash between
    the two still leaves the numbers."""

    path: Path = field(default_factory=lambda: RESULT_PATH.with_suffix(".jsonl"))

    def write(self, row: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True, default=str) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.experiments.neighbour",
        description="The prospective neighbour-acquisition test.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("baseline", help="Print the current state. Free.")
    sub.add_parser("run", help=f"Execute both arms. Costs ~${2 * CEILING_USD:.2f}.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "baseline":
            state = snapshot(conn)
            print(json.dumps(asdict(state), indent=2, default=str))
            return 0

        from fragrance_graph.budget import Budget, BudgetExhausted

        budget = Budget.load(require_ledger=True)
        try:
            budget.check(2 * CEILING_USD)
        except BudgetExhausted as exc:
            print(f"Not running: {exc}")
            return 2

        run_id = f"neighbour-{datetime.now(UTC).date().isoformat()}"
        buy = _real_buyer(conn, budget, run_id)
        log_to = RunLog()

        # A first, deliberately. See the prediction file.
        a = run_arm(conn, arm="A", target=ARM_A, buy=buy)
        log_to.write(_as_row(a))
        b = run_arm(conn, arm="B", target=ARM_B, buy=buy)
        log_to.write(_as_row(b))

        report = render(a, b)
        RESULT_PATH.write_text(report + "\n", encoding="utf-8")
        print(f"\n{report}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
