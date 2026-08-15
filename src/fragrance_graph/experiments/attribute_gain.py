"""Does buying more comments about a bottle turn opinions into agreement?

    uv run python -m fragrance_graph.experiments.attribute_gain plan
    uv run python -m fragrance_graph.experiments.attribute_gain before
    uv run python -m fragrance_graph.experiments.attribute_gain run --limit 3
    uv run python -m fragrance_graph.experiments.attribute_gain report

An earlier experiment enriched nine bottles for $1.09 and produced one
page, and the conclusion recorded was "enrichment scatters". That
conclusion is not reusable here, because it measured **pages**. The
question now is whether enrichment converts *singleton attribute
observations into repeated ones* — a different outcome that the same run
might well have produced while being scored against something else.

So this re-runs the experiment against the metric that matters for
recommendation, and it is built to be able to say **no**.

## What counts as success, decided before the run

Not new facts. A run that adds six hundred one-off descriptors moves
`facts` and answers nothing, because a comparison needs two bottles to
have said the same thing. The outcomes worth money, in order:

    singleton -> repeated     an opinion becomes a second opinion
    singleton -> supported    it clears the bar to be stated
    new comparable attribute  a second bottle joins one that exists
    newly answerable query    the benchmark moves

and the number that decides everything is **cost per repeated fact**,
against the measured $0.17 of a targeted pair verification.

## Why the cohort looks like this

Ten bottles spanning the range rather than the ten best. Layton has 81
singletons and 163 comments already read; Oajan has 7 and 12. If
enrichment only works where evidence is already dense, that is a finding
about *scheduling* — it would mean spending on well-covered bottles and
leaving thin ones alone, which is the opposite of what "under-covered"
scheduling assumes.

## Nothing here can spend without the ledger

`run` takes its extractor by injection and the CLI supplies
`budget.guard`. There is no client construction in this module, which is
checked by a test rather than promised: a paid module that can build its
own client is how the cap has been escaped four times.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from fragrance_graph.coverage import Snapshot, snapshot
from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evidence import Attribution, attribute_facts

log = logging.getLogger("fragrance_graph.experiments.attribute_gain")

RESULTS = Path("data/experiments/attribute-gain.jsonl")

#: What one bottle's enrichment is expected to cost, from the measured
#: extraction rate of ~$0.42 per 1,000 comments against a 400-comment
#: ceiling. Checked before the first bottle so the run refuses rather than
#: trimming the cohort to fit.
ESTIMATED_PER_BOTTLE = 0.17


class _NullLog:
    """A run log that discards. `enrich_one` needs somewhere to write."""

    def write(self, row: dict) -> None:  # pragma: no cover - trivial
        pass


def _llm_client():
    """The extraction client, built only inside `run`.

    Imported at call time so that importing this module — which `plan`,
    `before` and `report` all do — cannot construct anything that spends.
    """
    from fragrance_graph.extract.llm import build_client as build_llm

    return build_llm()

#: Ten bottles spanning the evidence range, chosen from the singleton
#: census on 2026-08-15 rather than hand-picked. The spread is the point:
#: a strategy that only works on Layton is a strategy for one bottle.
COHORT = (
    "Parfums de Marly Layton",           # 81 singletons, 163 comments read
    "Lattafa Khamrah",                   # 68, 75
    "Kilian Angels' Share",              # 49, 31
    "Dior Sauvage",                      # 40, 13
    "Parfums de Marly Delina",           # 31, 31
    "Creed Aventus Cologne",             # 23, 40
    "Lattafa Khamrah Qahwa",             # 20, 50
    "French Avenue Liquid Brun",         # 14, 38
    "Parfums de Marly Oajan",            # 7, 12
    "Fragrance World Oud Wonder",        # 6, 9
)


@dataclass
class BottleState:
    """One bottle's evidence, at one moment."""

    fragrance_id: int
    name: str
    facts: int = 0
    singleton: int = 0
    repeated: int = 0
    supported: int = 0
    contested: int = 0
    declarable: int = 0
    distinct_attributes: int = 0
    creators: int = 0
    comments: int = 0
    #: The exact (attribute, value) keys, so a diff can name *which*
    #: singleton became repeated rather than only counting them.
    singleton_keys: list = field(default_factory=list)
    repeated_keys: list = field(default_factory=list)
    #: Kept separately so `diff` can report the second success criterion.
    #: Without it, a singleton that reached SUPPORTED showed as
    #: "0 ->supported" for a conversion that did happen.
    supported_keys: list = field(default_factory=list)


def _bottle_state(
    conn: psycopg.Connection, fragrance_id: int, name: str
) -> BottleState:
    facts = [
        f
        for f in attribute_facts(
            conn, fragrance_id=fragrance_id, attribution=Attribution.PROPOSED
        )
        if f.supporting.people > 0
    ]
    comments = conn.execute(
        "SELECT count(DISTINCT c.id) FROM claims cl"
        "  JOIN comments c ON c.id = cl.comment_id"
        " WHERE cl.subject_frag_id = %s",
        (fragrance_id,),
    ).fetchone()[0]
    return BottleState(
        fragrance_id=fragrance_id,
        name=name,
        facts=len(facts),
        singleton=sum(1 for f in facts if f.supporting.people == 1),
        repeated=sum(1 for f in facts if f.supporting.people >= 2),
        supported=sum(1 for f in facts if f.strength.name == "SUPPORTED"),
        contested=sum(1 for f in facts if f.opposing.people > 0),
        declarable=sum(1 for f in facts if f.may_declare),
        distinct_attributes=len({(f.attribute, f.value) for f in facts}),
        creators=max((f.supporting.creators for f in facts), default=0),
        comments=comments,
        singleton_keys=sorted(
            [f.attribute, f.value] for f in facts if f.supporting.people == 1
        ),
        repeated_keys=sorted(
            [f.attribute, f.value] for f in facts if f.supporting.people >= 2
        ),
        supported_keys=sorted(
            [f.attribute, f.value]
            for f in facts
            if f.strength.name == "SUPPORTED"
        ),
    )


def catalogue_ids(conn: psycopg.Connection) -> dict[str, int]:
    return {
        row["canonical_name"]: row["id"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }


def before(
    conn: psycopg.Connection, cohort: tuple[str, ...] = COHORT
) -> tuple[list[BottleState], Snapshot]:
    """Record the state every claim in the report will be measured against."""
    ids = catalogue_ids(conn)
    states = [
        _bottle_state(conn, ids[name], name) for name in cohort if name in ids
    ]
    missing = [n for n in cohort if n not in ids]
    if missing:
        log.warning("not in the catalogue: %s", ", ".join(missing))
    return states, snapshot(conn)


@dataclass
class BottleGain:
    """What one bottle's enrichment actually converted."""

    name: str
    fragrance_id: int
    singleton_to_repeated: list = field(default_factory=list)
    singleton_to_supported: list = field(default_factory=list)
    new_facts: list = field(default_factory=list)
    new_declarable: int = 0
    new_contested: int = 0
    creators_before: int = 0
    creators_after: int = 0
    comments_before: int = 0
    comments_after: int = 0
    usd: float = 0.0
    quota_units: int = 0
    stop_reason: str = ""

    @property
    def comments_read(self) -> int:
        return max(0, self.comments_after - self.comments_before)

    @property
    def converted(self) -> int:
        return len(self.singleton_to_repeated)

    @property
    def usd_per_conversion(self) -> float | None:
        """Cost per singleton converted, or **None** when nothing converted.

        Not 0.0. Money spent for no conversions is the worst possible
        result, and returning zero made it read as the best possible unit
        cost — in the one metric this experiment exists to decide on. The
        original test pinned that behaviour, which is how it survived.
        """
        return self.usd / self.converted if self.converted else None

    def __str__(self) -> str:
        return (
            f"{self.name}: {self.converted} singleton->repeated, "
            f"{len(self.singleton_to_supported)} ->supported, "
            f"{len(self.new_facts)} new facts, "
            f"+{self.new_declarable} declarable, "
            f"{self.comments_read} comments, ${self.usd:.4f}"
        )


def diff(before_state: BottleState, after_state: BottleState) -> BottleGain:
    """What changed, named rather than counted.

    Naming which singleton became repeated is what separates "the numbers
    moved" from "this specific opinion became agreement", and only the
    second tells a scheduler where to spend next.
    """
    was_singleton = {tuple(k) for k in before_state.singleton_keys}
    now_repeated = {tuple(k) for k in after_state.repeated_keys}
    known = was_singleton | {tuple(k) for k in before_state.repeated_keys}
    now_all = {tuple(k) for k in after_state.singleton_keys} | now_repeated

    now_supported = {tuple(k) for k in after_state.supported_keys}
    return BottleGain(
        name=after_state.name,
        fragrance_id=after_state.fragrance_id,
        singleton_to_repeated=sorted(list(k) for k in was_singleton & now_repeated),
        singleton_to_supported=sorted(
            list(k) for k in was_singleton & now_supported
        ),
        new_facts=sorted(list(k) for k in now_all - known),
        new_declarable=max(0, after_state.declarable - before_state.declarable),
        new_contested=max(0, after_state.contested - before_state.contested),
        creators_before=before_state.creators,
        creators_after=after_state.creators,
        comments_before=before_state.comments,
        comments_after=after_state.comments,
    )


def enrich_cohort(
    conn: psycopg.Connection,
    states: list[BottleState],
    *,
    run_one,
    limit: int,
) -> list[BottleGain]:
    """Enrich each bottle and diff its evidence around the run.

    `run_one(name)` does the buying and returns `(comments, usd, quota,
    stop_reason)`. Injected so the whole measurement is exercisable
    without a key, a quota or a cent — and so the thing being measured and
    the thing doing the measuring are separable.

    Baselines are re-read per bottle immediately before its own run rather
    than taken from the file, because an earlier bottle's enrichment can
    add evidence about a later one: they are discussed in the same
    comments. Diffing a late bottle against a file written before any of
    it would credit this run with conversions another bottle bought.
    """
    gains = []
    for state in states[:limit]:
        fresh = _bottle_state(conn, state.fragrance_id, state.name)
        comments, usd, quota, stop = run_one(state.name)
        after = _bottle_state(conn, state.fragrance_id, state.name)
        gain = diff(fresh, after)
        gain.usd = usd
        gain.quota_units = quota
        gain.stop_reason = stop
        gain.comments_after = after.comments
        gain.comments_before = fresh.comments
        log.info("%s", gain)
        gains.append(gain)
    return gains


def render_report(
    gains: list[BottleGain], corpus_before: Snapshot, corpus_after: Snapshot
) -> str:
    """The answer, in the units the decision needs."""
    converted = sum(g.converted for g in gains)
    supported = sum(len(g.singleton_to_supported) for g in gains)
    spent = sum(g.usd for g in gains)
    comments = sum(g.comments_read for g in gains)

    lines = ["per bottle:", ""]
    for gain in gains:
        unit = (
            f"${gain.usd_per_conversion:.4f}/conversion"
            if gain.usd_per_conversion is not None
            else "no conversions"
        )
        lines.append(f"  {gain}  [{unit}]")

    lines += [
        "",
        "corpus:",
        corpus_before.render(corpus_after),
        "",
        "economics:",
        f"  spent                       ${spent:.4f}",
        f"  comments read               {comments}",
        f"  singleton -> repeated       {converted}",
        f"  singleton -> supported      {supported}",
    ]
    if converted:
        lines.append(f"  cost per repeated fact      ${spent / converted:.4f}")
    else:
        lines.append(
            "  cost per repeated fact      n/a — nothing converted, which "
            "is the negative result this was built to be able to report"
        )
    gained = (
        corpus_after.comparable_attributes - corpus_before.comparable_attributes
    )
    lines.append(f"  new comparable attributes   {gained:+}")
    lines.append(
        f"  relative cases answerable   "
        f"{corpus_before.answerable_relative} -> "
        f"{corpus_after.answerable_relative}"
    )
    return "\n".join(lines)


def render_plan(states: list[BottleState]) -> str:
    lines = [
        f"cohort of {len(states)}, spanning the evidence range",
        "",
        f"  {'bottle':<36}{'facts':>7}{'singleton':>11}{'repeated':>10}"
        f"{'creators':>10}{'comments':>10}",
    ]
    for state in states:
        lines.append(
            f"  {state.name:<36}{state.facts:>7}{state.singleton:>11}"
            f"{state.repeated:>10}{state.creators:>10}{state.comments:>10}"
        )
    lines += [
        "",
        f"  {'TOTAL':<36}{sum(s.facts for s in states):>7}"
        f"{sum(s.singleton for s in states):>11}"
        f"{sum(s.repeated for s in states):>10}",
    ]
    return "\n".join(lines)


def write_results(rows: list[dict], path: Path = RESULTS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.experiments.attribute_gain",
        description="Does enrichment turn opinions into agreement?",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="The cohort and its current evidence")
    b = sub.add_parser("before", help="Record the baseline; spends nothing")
    b.add_argument("--out", type=Path, default=Path("data/experiments/gain-before.json"))
    r = sub.add_parser("run", help="Enrich the cohort; SPENDS MONEY")
    r.add_argument("--limit", type=int, default=3)
    rep = sub.add_parser("report", help="Re-read recorded runs; spends nothing")
    rep.add_argument("--results", type=Path, default=RESULTS)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "plan":
            states, _ = before(conn)
            print(render_plan(states))
            return 0
        if args.command == "report":
            if not args.results.exists():
                print(f"No runs recorded at {args.results}.")
                return 0
            rows = [
                json.loads(line)
                for line in args.results.read_text().splitlines()
                if line.strip()
            ]
            gains = [
                BottleGain(
                    **{
                        k: v
                        for k, v in row.items()
                        if k in {f.name for f in fields(BottleGain)}
                    }
                )
                for row in rows
                if row.get("kind") == "gain"
            ]
            now = snapshot(conn)
            print(render_report(gains, now, now))
            return 0
        if args.command == "before":
            states, overall = before(conn)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(
                    {
                        "recorded_at": datetime.now(UTC).isoformat(
                            timespec="seconds"
                        ),
                        "bottles": [asdict(s) for s in states],
                        "corpus": asdict(overall),
                    },
                    indent=2,
                )
            )
            print(render_plan(states))
            print(f"\nbaseline written to {args.out}")
            return 0

        # `run` spends. Everything it needs is imported here so that
        # `plan` and `before` cannot reach a paid path even by accident.
        from fragrance_graph.budget import Budget, BudgetExhausted
        from fragrance_graph.frontier import (
            Ceiling,
            QuotaTracker,
            Trial,
            budgeted_extractor,
            candidates,
            enrich_one,
            query_for,
        )
        from fragrance_graph.ingest.youtube import build_client

        budget = Budget.load(require_ledger=True)
        try:
            budget.check(ESTIMATED_PER_BOTTLE)
        except BudgetExhausted as exc:
            print(f"Not running: {exc}")
            print(
                f"  The experiment needs roughly ${ESTIMATED_PER_BOTTLE} per "
                "bottle. Re-run after the UTC date rolls over."
            )
            return 2

        states, corpus_before = before(conn)
        client, api_key = build_client()
        quota = QuotaTracker()
        extract_fn = budgeted_extractor(_llm_client(), budget)
        ceiling = Ceiling()
        run_id = f"attribute-gain-{datetime.now(UTC).date().isoformat()}"

        def run_one(name: str):
            """Buy comments about one bottle, broadly.

            `query_for` produces "<name> fragrance review" — never "dupe",
            because this job exists to learn what a bottle is like and the
            corpus is already dupe-shaped.
            """
            pool = candidates(conn)
            match = next((c for c in pool if c.text.lower() in name.lower()), None)
            if match is None:
                log.warning("%s has no candidate row; skipping", name)
                return 0, 0.0, 0, "no-candidate"
            trial = Trial(
                text=match.text, claims_before=match.claims,
                creators_before=match.creators,
                started_at=datetime.now(UTC).isoformat(timespec="seconds"),
                query=query_for(match.text),
            )
            enrich_one(
                conn, client, api_key, match, quota=quota, ceiling=ceiling,
                run=run_id, extract_fn=extract_fn, trial=trial,
            )
            return (
                trial.comments_seen, trial.usd, trial.quota_units,
                trial.stop_reason,
            )

        try:
            gains = enrich_cohort(
                conn, states, run_one=run_one, limit=args.limit
            )
        except BudgetExhausted as exc:
            print(f"Stopped at the cap: {exc}")
            gains = []

        corpus_after = snapshot(conn)
        report = render_report(gains, corpus_before, corpus_after)
        print(report)
        write_results(
            [
                {
                    "kind": "gain",
                    "run": run_id,
                    "at": datetime.now(UTC).isoformat(timespec="seconds"),
                    **asdict(gain),
                }
                for gain in gains
            ]
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
