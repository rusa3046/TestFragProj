"""Scoring the recommender against a checked-in query set.

    uv run python -m fragrance_graph.evals.recommend
    uv run python -m fragrance_graph.evals.recommend --failures

The extraction eval (`evals/score.py`) asks whether the model read a
comment correctly. This one asks a different question: given a corpus,
does the *product* answer a person's question without saying anything
nobody said.

## The metric that outranks the others

    unsupported assertions = 0

Everything else here is diagnostic. Recall can be low because the corpus
is thin, parsing can miss a phrasing nobody anticipated, and both are
fixable later. A sentence asserting more than its evidence is a different
kind of failure — it is the product being wrong about the one thing it
exists to be right about — so it is counted separately and a single one is
a failing run.

## How an unsupported assertion is detected

Not by reading the prose with another model, which would only move the
problem. `recommend.Reason.phrase` is the single place wording is chosen,
and it is chosen from the fact's own strength, so the check is structural:

> Every phrase built from a non-declarable reason must carry a hedge.

A hedge is one of a fixed set of markers — "one commenter said", "people
said", "disagree", "rather than naming it", "the name contains". If a
reason cannot be declared and its phrase contains none of them, the
renderer has laundered weak evidence into a flat statement, and the run
fails whatever else it scored.

## Answerable is a property of the corpus, not of the question

Several rows are marked `answerable: false` on purpose — "an expensive
hotel lobby at night" is beyond the deterministic vocabulary, "why did you
recommend this" has nothing in scope to explain, "what new releases are
people comparing" needs a subsystem that does not exist yet. For those,
**refusing is the correct answer** and answering is the failure. Scoring
them the other way round would reward exactly the behaviour this whole
system is built to avoid.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.plan import Direction, Intent, parse_with_corpus
from fragrance_graph.recommend import recommend

log = logging.getLogger("fragrance_graph.evals.recommend")

DEFAULT_SET = Path("data/eval/recommendations.jsonl")

#: Phrases that mark a sentence as reporting rather than asserting. A
#: non-declarable reason whose wording contains none of these has been
#: laundered into a statement of fact.
HEDGES = (
    "one commenter said",
    "people said",
    "say so and",
    "disagree",
    "rather than naming it",
    "the name contains",
)

#: A phrase that states its own head count discloses just as completely as
#: a hedge word does. "2 people across 1 channel compared this with Delina"
#: asserts nothing beyond the two numbers in it. The invariant is
#: *disclosure*, not the presence of a particular English phrase, and the
#: first version of this check confused the two and flagged thirteen honest
#: sentences.
DISCLOSED_COUNTS = re.compile(
    r"\b\d+ (?:person|people) across \d+ channels?\b"
)


def discloses(phrase: str) -> bool:
    return any(h in phrase for h in HEDGES) or bool(DISCLOSED_COUNTS.search(phrase))


@dataclass
class Case:
    id: str
    category: str
    query: str
    intent: str
    anchor: str | None
    hard: list[list[str]]
    soft: list[list[str]]
    answerable: bool
    other: str | None = None
    note: str | None = None


@dataclass
class CaseResult:
    case: Case
    intent_ok: bool = False
    anchor_ok: bool = False
    hard_ok: bool = False
    soft_ok: bool = False
    answered: bool = False
    #: The primary metric. Any value above zero fails the run.
    unsupported: list[str] = field(default_factory=list)
    #: A returned candidate that does not satisfy a stated requirement.
    violations: list[str] = field(default_factory=list)
    results: int = 0
    detail: str = ""

    @property
    def parsing_ok(self) -> bool:
        return self.intent_ok and self.anchor_ok and self.hard_ok and self.soft_ok

    @property
    def answering_ok(self) -> bool:
        """Answered when it should, refused when it should."""
        return self.answered == self.case.answerable

    @property
    def passed(self) -> bool:
        return (
            self.parsing_ok
            and self.answering_ok
            and not self.unsupported
            and not self.violations
        )


def load_cases(path: Path = DEFAULT_SET) -> list[Case]:
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [Case(**row) for row in rows]


def _soft_key(attribute: str, value: str, direction: str) -> tuple[str, str, str]:
    return (attribute, value, direction)


def run_case(conn: psycopg.Connection, case: Case) -> CaseResult:
    result = CaseResult(case=case)
    plan = parse_with_corpus(conn, case.query)

    result.intent_ok = plan.intent == Intent(case.intent)
    result.anchor_ok = plan.anchor == case.anchor
    result.hard_ok = {(c.attribute, c.value) for c in plan.hard} == {
        (a, v) for a, v in case.hard
    }
    expected_soft = {_soft_key(a, v, d) for a, v, d in case.soft}
    got_soft = {
        _soft_key(p.attribute, p.value, str(Direction(p.direction))) for p in plan.soft
    }
    # The planner may find *more* than the case names — a query mentioning
    # "summer wedding" legitimately yields both. It may not find less.
    result.soft_ok = expected_soft <= got_soft

    answer = recommend(conn, case.query)
    result.results = len(answer.results)
    result.answered = bool(answer.results)

    for candidate in answer.results:
        for reason in candidate.reasons + candidate.caveats:
            phrase = reason.phrase()
            if reason.declarable or reason.kind == "graph" and reason.declarable:
                continue
            if not discloses(phrase):
                result.unsupported.append(f"{candidate.name}: {phrase}")
        for constraint in plan.hard:
            if f"{constraint.attribute}={constraint.value}" not in candidate.matched:
                result.violations.append(
                    f"{candidate.name} returned without "
                    f"{constraint.attribute}={constraint.value}"
                )

    result.detail = _detail(result, plan, expected_soft, got_soft)
    return result


def _detail(result, plan, expected_soft, got_soft) -> str:
    bits = []
    if not result.intent_ok:
        bits.append(f"intent {plan.intent} != {result.case.intent}")
    if not result.anchor_ok:
        bits.append(f"anchor {plan.anchor!r} != {result.case.anchor!r}")
    if not result.hard_ok:
        bits.append(
            f"hard {sorted((c.attribute, c.value) for c in plan.hard)} "
            f"!= {sorted(tuple(x) for x in result.case.hard)}"
        )
    if not result.soft_ok:
        bits.append(f"soft missing {sorted(expected_soft - got_soft)}")
    if not result.answering_ok:
        bits.append(
            "answered when it should have refused"
            if result.answered
            else "refused when it should have answered"
        )
    return "; ".join(bits)


@dataclass
class Report:
    results: list[CaseResult]

    @property
    def unsupported(self) -> int:
        return sum(len(r.unsupported) for r in self.results)

    @property
    def violations(self) -> int:
        return sum(len(r.violations) for r in self.results)

    def rate(self, attribute: str) -> str:
        got = sum(1 for r in self.results if getattr(r, attribute))
        return f"{got}/{len(self.results)}"

    def render(self, *, failures_only: bool = False) -> str:
        lines = [
            f"{len(self.results)} cases",
            "",
            f"  intent parsed        {self.rate('intent_ok')}",
            f"  anchor parsed        {self.rate('anchor_ok')}",
            f"  hard constraints     {self.rate('hard_ok')}",
            f"  soft preferences     {self.rate('soft_ok')}",
            f"  answered or refused  {self.rate('answering_ok')}",
            f"  passed overall       {self.rate('passed')}",
            "",
            f"  hard-constraint violations   {self.violations}",
            f"  UNSUPPORTED ASSERTIONS       {self.unsupported}"
            + ("   <- must be 0" if self.unsupported else "   ok"),
        ]
        shown = [r for r in self.results if not r.passed or not failures_only]
        if shown:
            lines += ["", "by case:"]
            for r in shown:
                mark = "pass" if r.passed else "FAIL"
                lines.append(f"  [{mark}] {r.case.id:<34} {r.detail}")
                for problem in r.unsupported:
                    lines.append(f"           unsupported: {problem}")
                for problem in r.violations:
                    lines.append(f"           violation:   {problem}")
        by_category: dict[str, list[CaseResult]] = {}
        for r in self.results:
            by_category.setdefault(r.case.category, []).append(r)
        lines += ["", "by category:"]
        for category, rows in sorted(by_category.items()):
            passed = sum(1 for r in rows if r.passed)
            lines.append(f"  {category:<22} {passed}/{len(rows)}")
        return "\n".join(lines)


def evaluate(conn: psycopg.Connection, path: Path = DEFAULT_SET) -> Report:
    return Report([run_case(conn, case) for case in load_cases(path)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.evals.recommend",
        description="Score the recommender against the checked-in query set.",
    )
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, dest="path")
    parser.add_argument("--failures", action="store_true", help="Only failing cases")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        report = evaluate(conn, args.path)
        print(report.render(failures_only=args.failures))
    finally:
        conn.close()
    # A single unsupported assertion fails the run, whatever else scored.
    return 1 if report.unsupported else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
