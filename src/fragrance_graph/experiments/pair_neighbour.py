"""Neighbour acquisition for a *pair* gap.

    uv run python -m fragrance_graph.experiments.pair_neighbour baseline
    uv run python -m fragrance_graph.experiments.pair_neighbour run

Design frozen in `data/experiments/pair-neighbour-prediction.md`, committed
before this module existed. Constants are copied from it rather than made
configurable: a flag is a way to change an experiment after seeing its
results.

## What this tests, and what it does not

The attribute version of this hypothesis is dead. A neighbour's audience
produced 30 comparisons *to* the anchor and 0 descriptions *of* it, which
is exactly why they were there. That killed neighbour acquisition for
unary gaps and raised one narrower question, tested here and nowhere
generalised:

> Comparison-oriented neighbour acquisition is an efficient way to acquire
> **pairwise** evidence about an anchor.

## Why the metric is capped

`30 extracted edges` and `30 useful independent edges` are different
numbers, and the previous run scored well on the first while moving the
product by nothing. So the primary metric counts only evidence that closes
the distance to the publishing gate, and only up to the distance that
actually existed:

    a pair two creators short  ->  the third creator scores nothing
    a pair already at the gate ->  nothing it gains scores at all

Uncapped totals are still reported, under graph yield, because they are
worth knowing and worth not confusing with progress.
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

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.gate import (
    MIN_COMMENTERS,
    MIN_FLANKER_COMMENTERS,
    MIN_FLANKER_SOURCES,
    MIN_SOURCES,
)

log = logging.getLogger("fragrance_graph.experiments.pair_neighbour")

# --- frozen by data/experiments/pair-neighbour-prediction.md ----------------

ANCHOR = "Parfums de Marly Delina"
ARM_A = ANCHOR
ARM_B = "Maison Alhambra Delilah"
CEILING_USD = 0.10

RESULT_PATH = Path("data/experiments/pair-neighbour-result.md")

#: Every comparison claim connecting the anchor to another bottle, with the
#: independent people and channels behind it. Both ends must be resolved —
#: an edge to an unresolved name is not a pair the gate can ever pass.
PAIRS_SQL = """
SELECT other.id            AS partner_id,
       other.canonical_name AS partner,
       COALESCE(NULLIF(co.author_id, ''), 'author:unknown') AS person,
       co.source_channel   AS creator
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
  JOIN fragrances anchor
    ON anchor.id IN (c.subject_frag_id, c.object_frag_id)
   AND lower(anchor.canonical_name) = lower(%(anchor)s)
  JOIN fragrances other
    ON other.id = CASE WHEN c.subject_frag_id = anchor.id
                       THEN c.object_frag_id ELSE c.subject_frag_id END
 WHERE c.polarity = 'ASSERTED'
   AND c.evidence_verified = 1
   AND c.claim_type IN ('SIMILAR_TO', 'DUPE_OF', 'BETTER_THAN')
   AND c.subject_frag_id IS NOT NULL
   AND c.object_frag_id IS NOT NULL
"""


@dataclass(frozen=True)
class PairState:
    """One anchor pair: who supports it, and what the gate still wants."""

    partner: str
    people: frozenset[str]
    creators: frozenset[str]
    flanker: bool

    @property
    def threshold_people(self) -> int:
        return MIN_FLANKER_COMMENTERS if self.flanker else MIN_COMMENTERS

    @property
    def threshold_creators(self) -> int:
        return MIN_FLANKER_SOURCES if self.flanker else MIN_SOURCES

    @property
    def missing_people(self) -> int:
        return max(0, self.threshold_people - len(self.people))

    @property
    def missing_creators(self) -> int:
        return max(0, self.threshold_creators - len(self.creators))

    @property
    def clears_gate(self) -> bool:
        return self.missing_people == 0 and self.missing_creators == 0


def pair_states(conn: psycopg.Connection) -> dict[str, PairState]:
    """Every pair the anchor has, keyed by partner name."""
    from fragrance_graph.pages import bottle_facts, brand_casing, is_flanker_pair

    facts = bottle_facts(conn, brand_casing(conn))
    anchor_bottle = facts.get(ANCHOR)

    people: dict[str, set[str]] = {}
    creators: dict[str, set[str]] = {}
    for row in conn.execute(PAIRS_SQL, {"anchor": ANCHOR}):
        people.setdefault(row["partner"], set()).add(row["person"])
        creators.setdefault(row["partner"], set()).add(row["creator"])

    return {
        partner: PairState(
            partner=partner,
            people=frozenset(people[partner]),
            creators=frozenset(creators.get(partner, ())),
            flanker=bool(is_flanker_pair(anchor_bottle, facts.get(partner))),
        )
        for partner in people
    }


@dataclass
class Snapshot:
    pairs: dict[str, PairState] = field(default_factory=dict)
    published: int = 0
    total_facts: int = 0
    raw_anchor_claims: int = 0

    @property
    def partners(self) -> int:
        return len(self.pairs)

    @property
    def gate_clearing(self) -> int:
        return sum(1 for p in self.pairs.values() if p.clears_gate)


def _raw_anchor_claims(conn: psycopg.Connection) -> int:
    """Comparison claims naming the anchor, resolved or not.

    The "30 extracted" number. Deliberately looser than `PAIRS_SQL`: it
    counts what extraction produced, so the gap between it and the primary
    metric is visible rather than inferred.
    """
    return conn.execute(
        "SELECT count(*) FROM claims c "
        " WHERE c.polarity = 'ASSERTED'"
        "   AND c.claim_type IN ('SIMILAR_TO','DUPE_OF','BETTER_THAN')"
        "   AND (c.raw_subject_text ILIKE %(like)s"
        "     OR c.raw_object_text ILIKE %(like)s)",
        {"like": "%delina%"},
    ).fetchone()[0]


def snapshot(conn: psycopg.Connection) -> Snapshot:
    """Read-only. Spends nothing, writes nothing."""
    from fragrance_graph.evidence import Attribution, attribute_facts
    from fragrance_graph.pages import qualifying_pairs

    return Snapshot(
        pairs=pair_states(conn),
        published=sum(
            1
            for p in qualifying_pairs(conn)
            if ANCHOR in (p.left, p.right)
        ),
        total_facts=sum(
            1
            for f in attribute_facts(conn, attribution=Attribution.STATED)
            if f.supporting.people > 0
        ),
        raw_anchor_claims=_raw_anchor_claims(conn),
    )


# --- the capped primary metric ----------------------------------------------


@dataclass
class Credit:
    """What one arm earned against the frozen shortfall."""

    people: int = 0
    creators: int = 0
    uncapped_people: int = 0
    uncapped_creators: int = 0
    partners_gained: tuple[str, ...] = ()
    creator_gained_on: tuple[str, ...] = ()

    @property
    def capped_total(self) -> int:
        return self.people + self.creators

    @property
    def uncapped_total(self) -> int:
        return self.uncapped_people + self.uncapped_creators


def credit(
    frozen: dict[str, PairState],
    before: dict[str, PairState],
    after: dict[str, PairState],
    already_spent: Credit | None = None,
) -> Credit:
    """Progress toward the gate, capped at the frozen shortfall.

    `frozen` fixes the caps once, before either arm ran. `already_spent`
    is the earlier arm's credit, subtracted from the cap so the same
    shortfall cannot be paid for twice — which deepens the declared order
    bias, since the arm that runs first consumes the cap first.

    A partner that did not exist at the frozen snapshot is a genuinely new
    comparison and is capped at its own thresholds.
    """
    used_people = already_spent.people if already_spent else 0
    used_creators = already_spent.creators if already_spent else 0
    out = Credit()
    gained_partners: list[str] = []
    gained_creator_on: list[str] = []

    for partner, now in after.items():
        was = before.get(partner)
        base = frozen.get(partner)
        if was is None:
            gained_partners.append(partner)
            new_people = len(now.people)
            new_creators = len(now.creators)
            cap_people, cap_creators = now.threshold_people, now.threshold_creators
        else:
            new_people = len(now.people - was.people)
            new_creators = len(now.creators - was.creators)
            reference = base or was
            cap_people = reference.missing_people
            cap_creators = reference.missing_creators
        if new_creators:
            gained_creator_on.append(partner)
        out.uncapped_people += new_people
        out.uncapped_creators += new_creators
        out.people += min(new_people, max(0, cap_people))
        out.creators += min(new_creators, max(0, cap_creators))

    out.people = max(0, out.people - used_people) if already_spent else out.people
    out.creators = (
        max(0, out.creators - used_creators) if already_spent else out.creators
    )
    out.partners_gained = tuple(sorted(gained_partners))
    out.creator_gained_on = tuple(sorted(gained_creator_on))
    return out


@dataclass
class ArmResult:
    arm: str
    target: str
    before: Snapshot
    after: Snapshot
    #: PRIMARY. Credited against the frozen pre-experiment shortfall, with
    #: no deduction for what the other arm earned. Both arms are scored
    #: against the identical baseline, so the A/B comparison measures the
    #: two acquisitions rather than the order they ran in.
    earned: Credit
    #: SECONDARY. The same credit with the earlier arm's share removed —
    #: what this arm added *given* the corpus the previous one left behind.
    #: Real and worth knowing, and not the comparison: it answers "what did
    #: the second purchase add", not "which purchase was better".
    marginal: Credit = field(default_factory=Credit)
    #: True when the arm stopped because it hit its own ceiling rather than
    #: running out of creators or comments. A truncated arm did not get the
    #: budget the design promised it, so it is not a valid observation.
    truncated: bool = False
    comments_read: int = 0
    usd: float = 0.0
    quota_units: int = 0
    stop_reason: str = ""

    @property
    def progress_per_dollar(self) -> float:
        """The primary number. No spend and no gain is zero, not infinity."""
        if self.usd <= 0:
            return 0.0
        return self.earned.capped_total / self.usd

    @property
    def new_raw_claims(self) -> int:
        return self.after.raw_anchor_claims - self.before.raw_anchor_claims

    @property
    def new_total_facts(self) -> int:
        return self.after.total_facts - self.before.total_facts


def verdict(a: ArmResult, b: ArmResult) -> tuple[bool, str]:
    """Against the two frozen conditions, and the two frozen failures.

    A truncated arm is checked first and returns no verdict either way. An
    arm cut off before its stated budget did not run the experiment that
    was designed, and scoring it would compare a full purchase against a
    partial one.
    """
    for arm in (a, b):
        if arm.truncated:
            return False, (
                f"INVALID — arm {arm.arm} was truncated at its ceiling "
                f"(${arm.usd:.4f}) rather than running to a natural stop. "
                "It did not receive the budget the frozen design promised, "
                "so neither arm is scored. Re-run with the ceiling the "
                "design states."
            )
    beats = b.progress_per_dollar > a.progress_per_dollar
    gained_creator = bool(b.earned.creator_gained_on)

    if b.new_raw_claims >= 10 and not gained_creator:
        return False, (
            f"B extracted {b.new_raw_claims} raw comparisons naming the anchor "
            "and added no independent creator to any pair. Volume without "
            "independence — the previous failure in pair-shaped form."
        )
    if a.earned.capped_total == 0 and b.earned.capped_total == 0:
        return False, (
            "Neither arm produced usable pair evidence. The anchor's "
            "comparison neighbourhood is not reachable by buying comments."
        )
    if beats and gained_creator:
        return True, (
            "B beat A on capped progress toward the gate per dollar and added "
            f"an independent creator on {len(b.earned.creator_gained_on)} pair(s)."
        )
    if not beats:
        return False, (
            "The direct arm matched or beat the neighbour on capped progress "
            "per dollar. Comparison-oriented neighbour acquisition is not the "
            "efficient instrument for pair gaps."
        )
    return False, (
        "B beat A per dollar but added no independent creator, and creators "
        "are the binding constraint. Condition 2 fails."
    )


def run_arm(
    conn: psycopg.Connection,
    *,
    arm: str,
    target: str,
    buy: Any,
    frozen: dict[str, PairState],
    already_spent: Credit | None = None,
) -> ArmResult:
    """Snapshot, buy, re-derive, snapshot.

    The re-derivation is not optional: extraction stores the words a
    commenter typed, and until `backfill` resolves both ends of an edge the
    pair does not exist for `PAIRS_SQL`. Measuring without it reports zero
    for work that was done.
    """
    from fragrance_graph.resolve.entities import backfill

    before = snapshot(conn)
    comments_read, usd, quota_units, stop_reason, truncated = buy(target)
    backfill(conn)
    conn.commit()
    after = snapshot(conn)
    return ArmResult(
        arm=arm,
        target=target,
        before=before,
        after=after,
        # Independent: same frozen caps for both arms, no deduction.
        earned=credit(frozen, before.pairs, after.pairs),
        # Sequential: what this arm added on top of the previous one.
        marginal=credit(frozen, before.pairs, after.pairs, already_spent),
        truncated=truncated,
        comments_read=comments_read,
        usd=usd,
        quota_units=quota_units,
        stop_reason=stop_reason,
    )


def _real_buyer(conn: psycopg.Connection, run_id: str) -> Any:
    """A buyer whose per-arm ceiling is enforced by the ledger itself.

    The previous experiment's arms were bounded only *between videos*:
    `enrich_one` tests `trial.usd >= ceiling.max_usd` before starting each
    one, so once a video began, extraction ran to the end of its comments.
    At `max_comments = 400` that is up to twenty batches, and the stated
    $0.10 ceiling bounded where an arm *started* a video rather than what
    it spent.

    Here each arm gets its own `Budget` whose cap is *today's ledger plus
    the arm's ceiling*, read fresh at the arm's start. `budgeted_extractor`
    charges through `guard`, which re-reads the ledger and raises the
    moment that arm-scoped cap is crossed — so an arm now overshoots by at
    most one batch (about $0.09, bounded by `DEFAULT_MAX_TOKENS`) instead
    of by one video.

    An arm stopped that way is **truncated**: it did not receive the budget
    the design promised, and `verdict` refuses to score the run rather than
    compare a full purchase against a partial one.
    """
    from fragrance_graph.budget import (
        DAILY_CAP_USD,
        Budget,
        BudgetExhausted,
        spent_by,
    )
    from fragrance_graph.experiments.neighbour import _llm_client
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

    def buy(target: str):
        from fragrance_graph.experiments.attribute_gain import _candidate_for

        match = _candidate_for(conn, target)
        if match is None:
            return 0, 0.0, 0, "not-in-catalogue", False

        arm_budget = Budget.load(require_ledger=True)
        # Clamped to the daily cap, not merely offset from it. The first
        # version wrote `spent + CEILING_USD`, which *replaces* the daily
        # ceiling with a per-arm one — with the ledger at $1.3616 the second
        # arm's cap computed to $1.5616, above the $1.50 the whole system is
        # supposed to hold to. A per-arm ceiling may only ever tighten the
        # daily cap; it must never be able to lift it.
        arm_budget.cap_usd = min(
            DAILY_CAP_USD, arm_budget.spent_usd + CEILING_USD
        )
        # Every batch this arm charges is labelled with the run and the
        # arm, so its cost can be read back out of the ledger instead of
        # accumulated in memory. See below for why that matters.
        label = f"pair-neighbour:{run_id}:{target}"
        extract_fn = budgeted_extractor(_llm_client(), arm_budget, label=label)

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
        truncated = False
        try:
            enrich_one(
                conn, client, api_key, match, quota=quota,
                ceiling=Ceiling(max_usd=CEILING_USD),
                run=run_id, extract_fn=extract_fn, trial=trial,
            )
        except BudgetExhausted as exc:
            truncated = True
            trial.stop_reason = "arm-ceiling"
            trial.note = str(exc)[:200]
            log.warning("arm for %s hit its own ceiling: %s", target, exc)
        # Spend comes from the ledger, not from `trial.usd`.
        #
        # `budgeted_extractor` accumulates a running total through its
        # `on_spend` callback, and a `BudgetExhausted` raised inside a batch
        # unwinds past the addition — so the batch that stopped the arm is
        # charged to the ledger and missing from the counter. Measured on
        # the Delina run: the arms reported $0.0828 and $0.0988 against a
        # true $0.2078.
        #
        # `guard` records before it raises, so the ledger has every charged
        # batch including the last. Under the one-paid-worker invariant this
        # label is unique to this arm, so summing it is exact.
        charged = spent_by(arm_budget.ledger, arm_budget.today, label)
        return (
            trial.comments_seen, charged, trial.quota_units,
            trial.stop_reason, truncated,
        )

    return buy


def render(a: ArmResult, b: ArmResult) -> str:
    passed, why = verdict(a, b)
    frozen = a.before.pairs
    shortfall = "\n".join(
        f"  {p.partner[:38]:<38} {len(p.people)}p/{len(p.creators)}c  "
        f"needs {p.missing_people}p/{p.missing_creators}c"
        + ("  flanker" if p.flanker else "")
        for p in sorted(frozen.values(), key=lambda s: s.partner)
    )
    return "\n".join([
        "# Result — neighbour acquisition for a pair gap",
        "",
        f"Run {datetime.now(UTC).isoformat(timespec='seconds')}. Design frozen in "
        "`data/experiments/pair-neighbour-prediction.md` before any spend.",
        "",
        f"## {'PASS' if passed else 'FAIL'}",
        "",
        why,
        "",
        "## Frozen shortfall (caps the primary metric)",
        "",
        "```",
        shortfall,
        "```",
        "",
        "## Primary — capped progress toward the gate, per dollar",
        "",
        "```",
        f"{'':<16}{'people':>8}{'creators':>10}{'progress':>10}{'$':>9}{'per $':>9}",
        f"{'A  direct':<16}{a.earned.people:>8}{a.earned.creators:>10}"
        f"{a.earned.capped_total:>10}{a.usd:>9.4f}{a.progress_per_dollar:>9.1f}",
        f"{'B  neighbour':<16}{b.earned.people:>8}{b.earned.creators:>10}"
        f"{b.earned.capped_total:>10}{b.usd:>9.4f}{b.progress_per_dollar:>9.1f}",
        "```",
        "",
        f"Independent creator added on — A: {list(a.earned.creator_gained_on) or 'none'}, "
        f"B: {list(b.earned.creator_gained_on) or 'none'}",
        "",
        "Both arms are scored against the identical frozen shortfall, with "
        "no deduction for what the other earned, so the comparison measures "
        "the two acquisitions rather than the order they ran in.",
        "",
        "## Secondary — sequential marginal yield",
        "",
        "What each arm added *given* the corpus the previous one left. Real, "
        "and a different question from the one above.",
        "",
        "```",
        f"{'':<16}{'people':>8}{'creators':>10}{'marginal':>10}",
        f"{'A  direct':<16}{a.marginal.people:>8}{a.marginal.creators:>10}"
        f"{a.marginal.capped_total:>10}",
        f"{'B  neighbour':<16}{b.marginal.people:>8}{b.marginal.creators:>10}"
        f"{b.marginal.capped_total:>10}",
        "```",
        "",
        "## Secondary — graph yield, and raw versus usable",
        "",
        "```",
        f"{'':<16}{'raw claims':>12}{'uncapped':>10}{'partners':>10}"
        f"{'any bottle':>12}{'comments':>10}",
        f"{'A  direct':<16}{a.new_raw_claims:>12}{a.earned.uncapped_total:>10}"
        f"{len(a.earned.partners_gained):>10}{a.new_total_facts:>12}"
        f"{a.comments_read:>10}",
        f"{'B  neighbour':<16}{b.new_raw_claims:>12}{b.earned.uncapped_total:>10}"
        f"{len(b.earned.partners_gained):>10}{b.new_total_facts:>12}"
        f"{b.comments_read:>10}",
        "```",
        "",
        "`raw claims` is what extraction produced; `progress` above is what "
        "the gate can use. The distance between them is the point.",
        "",
        f"Anchor pages before: {a.before.published}, after: {b.after.published}",
        f"Pairs clearing the gate — before {a.before.gate_clearing}, "
        f"after {b.after.gate_clearing}",
        f"Stopped because — A: {a.stop_reason or 'n/a'}, B: {b.stop_reason or 'n/a'}",
        f"Total spend: ${a.usd + b.usd:.4f}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.experiments.pair_neighbour",
        description="The prospective pair-gap neighbour test.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("baseline", help="Current shortfall. Free.")
    sub.add_parser("run", help=f"Both arms. Up to ${2 * CEILING_USD:.2f} plus overshoot.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "baseline":
            state = snapshot(conn)
            for pair in sorted(state.pairs.values(), key=lambda s: s.partner):
                print(
                    f"  {pair.partner[:40]:<40} "
                    f"{len(pair.people)}p/{len(pair.creators)}c  "
                    f"needs {pair.missing_people}p/{pair.missing_creators}c"
                    + ("  flanker" if pair.flanker else "")
                )
            print(f"\n{state.partners} partners, {state.gate_clearing} at the gate, "
                  f"{state.published} published page(s), "
                  f"{state.raw_anchor_claims} raw claims naming the anchor")
            return 0

        from fragrance_graph.budget import Budget, BudgetExhausted

        budget = Budget.load(require_ledger=True)
        try:
            budget.check(2 * CEILING_USD)
        except BudgetExhausted as exc:
            print(f"Not running: {exc}")
            return 2

        run_id = f"pair-neighbour-{datetime.now(UTC).date().isoformat()}"
        buy = _real_buyer(conn, run_id)
        frozen = snapshot(conn).pairs

        # Admission control is `budget.check` above — a read, not a write.
        #
        # An earlier version held the money with `budget.reserve`, and it was
        # wrong here twice over. A reservation that is never settled stays on
        # the ledger for the rest of the UTC day: `try/finally` releases it on
        # an exception, but not on SIGKILL or a reclaimed container, which are
        # this environment's actual failure modes. Demonstrated — reserve
        # $0.20, charge $0.05, `os._exit`, and the day reads $0.25 forever.
        #
        # And the hold consumed exactly the headroom the arms needed. With the
        # ledger at $1.3616 after the hold, the second arm had $0.0384 to
        # spend and would have truncated at once, producing an INVALID verdict
        # and wasting the first arm's money.
        #
        # `reserve` is right for a paid call whose cost is unknown until it
        # returns. It is wrong as a lock around a run that records its own
        # spending, which this does.
        a = run_arm(conn, arm="A", target=ARM_A, buy=buy, frozen=frozen)
        b = run_arm(
            conn, arm="B", target=ARM_B, buy=buy, frozen=frozen,
            already_spent=a.earned,
        )

        log_path = RESULT_PATH.with_suffix(".jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            for result in (a, b):
                fh.write(json.dumps(asdict(result), sort_keys=True, default=str) + "\n")

        report = render(a, b)
        RESULT_PATH.write_text(report + "\n", encoding="utf-8")
        print(f"\n{report}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
