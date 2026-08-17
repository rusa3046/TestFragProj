"""Does a real embedder beat the lexical baseline at vibe queries?

    uv run python -m fragrance_graph.evals.fuzzy
    uv run python -m fragrance_graph.evals.fuzzy --arm distributional

The hashed n-gram embedder is lexical by construction: "airy" and "light"
mean nearly the same thing and share no characters, so nothing it does can
connect them. That is a real gap, and this measures whether the obvious
fixes actually close it or merely appear to.

## Why anecdotal pair scores are not enough

Spot-checking the corpus-derived embedder looked encouraging:

    airy / light           0.09 -> 0.34
    sweet / gourmand       0.00 -> 0.61
    clean / fresh          0.17 -> 0.59

and then, in the same run:

    rose / tobacco         0.00 -> 0.51
    airy / heavy           0.26 -> 0.40

Rose and tobacco smell nothing alike. They score highly because they are
discussed in the same sentences, and a co-occurrence model cannot tell
*used together* from *similar*. Antonyms are worse: "is it airy or heavy"
puts both words in identical contexts, so the model that finally connects
airy to light connects it to heavy just as firmly.

Choosing on the good pairs alone would have adopted a model that retrieves
the opposite of what was asked. So half this eval is **adversarial**: each
of those cases lists terms the query must *not* pull, and precision is
scored against them. A retrieval layer that offers "heavy" to somebody
asking for "airy" is worse than one that offers nothing.

## What is measured

    recall@k      how many hand-listed relevant terms are found
    precision@k   how much of what came back was relevant
    forbidden@k   how many adversarial terms were retrieved
    unsupported   sentences asserting more than their evidence

`forbidden@k` outranks the others. Recall is a convenience; retrieving a
term that means the opposite is a wrong answer with a citation attached,
which is the failure this whole system is built to avoid.
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
from fragrance_graph.semantic import (
    CorpusDistributional,
    Embedder,
    HashedNGrams,
    cosine,
)

log = logging.getLogger("fragrance_graph.evals.fuzzy")

DEFAULT_SET = Path("data/eval/fuzzy_queries.jsonl")

#: How many results a person would actually look at. Measuring at 50 would
#: flatter every arm; the product shows a handful.
DEFAULT_K = 10


@dataclass
class FuzzyCase:
    id: str
    query: str
    relevant: list[str]
    forbidden: list[str] = field(default_factory=list)
    note: str | None = None

    @property
    def adversarial(self) -> bool:
        return bool(self.forbidden)


@dataclass
class ArmResult:
    """One embedder's score on one case."""

    case: FuzzyCase
    retrieved: list[str] = field(default_factory=list)
    #: Relevant *labels* that were found. The recall numerator.
    hits: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    forbidden_hits: list[str] = field(default_factory=list)
    #: Retrieved *results* that matched some relevant label. The precision
    #: numerator, and a different number: one result can satisfy two
    #: labels, and counting labels over results gave precision above 1.0.
    relevant_results: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return len(self.hits) / len(self.case.relevant) if self.case.relevant else 0.0

    @property
    def precision(self) -> float:
        return (
            len(self.relevant_results) / len(self.retrieved)
            if self.retrieved
            else 0.0
        )


@dataclass
class ArmReport:
    name: str
    results: list[ArmResult] = field(default_factory=list)

    def _mean(self, attribute: str, only_adversarial: bool | None = None) -> float:
        rows = [
            r
            for r in self.results
            if only_adversarial is None or r.case.adversarial == only_adversarial
        ]
        if not rows:
            return 0.0
        return sum(getattr(r, attribute) for r in rows) / len(rows)

    @property
    def recall(self) -> float:
        return self._mean("recall")

    @property
    def precision(self) -> float:
        return self._mean("precision")

    @property
    def forbidden(self) -> int:
        """Total adversarial terms retrieved. The metric that outranks the
        rest: a term meaning the opposite of the query is a wrong answer
        with a citation attached."""
        return sum(len(r.forbidden_hits) for r in self.results)

    @property
    def cases_with_forbidden(self) -> int:
        return sum(1 for r in self.results if r.forbidden_hits)


def load_cases(path: Path = DEFAULT_SET) -> list[FuzzyCase]:
    return [
        FuzzyCase(**json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def descriptor_vocabulary(conn: psycopg.Connection) -> list[str]:
    """Every descriptor the corpus actually carries, deduplicated.

    Retrieval is scored over the real vocabulary rather than a curated
    list, so an arm cannot win by being good at terms nobody wrote.
    """
    rows = conn.execute(
        "SELECT DISTINCT lower(trim(raw_object_text)) AS value FROM claims"
        " WHERE polarity = 'ASSERTED' AND evidence_verified = 1"
        "   AND raw_object_text IS NOT NULL"
        "   AND claim_type IN ('NOTE_DESCRIPTOR', 'AESTHETIC', 'OCCASION')"
    ).fetchall()
    return sorted({r["value"] for r in rows if r["value"]})


def _matches_label(candidate: str, term: str) -> bool:
    """Whether one retrieved descriptor satisfies one hand-listed term.

    Word-boundary rather than bare substring. The corpus writes "coffee
    note" and "rosy-fruity" for what a reviewer lists as "coffee" and
    "rosy", so equality would score the normaliser rather than retrieval —
    but a bare substring also credits "rosewood" for "rose" and
    "lightweight" for "light", which inflates every arm and inflates the
    one with the noisiest vocabulary most.
    """
    words = re.split(r"[^a-z0-9]+", candidate.lower())
    target = term.lower().strip()
    if " " in target or "-" in target:
        return target in candidate.lower()
    return target in words


def _contains(retrieved: list[str], term: str) -> bool:
    """Whether a hand-listed term was found anywhere in the results."""
    return any(_matches_label(candidate, term) for candidate in retrieved)


def run_arm(
    embedder: Embedder,
    vocabulary: list[str],
    cases: list[FuzzyCase],
    *,
    k: int = DEFAULT_K,
) -> ArmReport:
    """Score one embedder over every case.

    The whole descriptor vocabulary is embedded once and reused, which is
    what makes three arms affordable to compare.
    """
    space = {term: embedder.embed(term) for term in vocabulary}
    report = ArmReport(name=embedder.name)

    for case in cases:
        query = embedder.embed(case.query)
        scored = sorted(
            ((cosine(query, vector), term) for term, vector in space.items()),
            key=lambda pair: (-pair[0], pair[1]),
        )
        retrieved = [term for score, term in scored[:k] if score > 0]
        result = ArmResult(case=case, retrieved=retrieved)
        result.hits = [t for t in case.relevant if _contains(retrieved, t)]
        result.misses = [t for t in case.relevant if not _contains(retrieved, t)]
        result.forbidden_hits = [
            t for t in case.forbidden if _contains(retrieved, t)
        ]
        result.relevant_results = [
            candidate
            for candidate in retrieved
            if any(_matches_label(candidate, term) for term in case.relevant)
        ]
        report.results.append(result)
    return report


def render(reports: list[ArmReport], *, k: int = DEFAULT_K) -> str:
    lines = [
        f"fuzzy retrieval, k={k}, {len(reports[0].results)} cases "
        f"({sum(1 for r in reports[0].results if r.case.adversarial)} adversarial)",
        "",
        f"  {'arm':<32}{'recall':>9}{'precision':>11}{'forbidden':>11}"
        f"{'cases hit':>11}",
        "  " + "-" * 72,
    ]
    for report in reports:
        lines.append(
            f"  {report.name:<32}{report.recall:>9.3f}{report.precision:>11.3f}"
            f"{report.forbidden:>11}{report.cases_with_forbidden:>11}"
        )
    lines += ["", "adversarial detail (terms retrieved that mean the opposite):"]
    for report in reports:
        offenders = [r for r in report.results if r.forbidden_hits]
        if not offenders:
            lines.append(f"  {report.name}: none")
            continue
        lines.append(f"  {report.name}:")
        for result in offenders:
            lines.append(
                f"    {result.case.query!r:<22} pulled "
                f"{', '.join(result.forbidden_hits)}"
            )
    return "\n".join(lines)


def build_arms(
    conn: psycopg.Connection, *, only: str | None = None
) -> list[Embedder]:
    """Every arm that can run without spending money.

    `OpenAIEmbeddings` is deliberately absent unless asked for by name: it
    is a paid model call, and a comparison that quietly charges the ledger
    to run an experiment is the same defect class as the three cap escapes
    already fixed.
    """
    arms: list[Embedder] = []
    if only in (None, "ngram"):
        arms.append(HashedNGrams())
    if only in (None, "distributional"):
        bodies = [
            row["body"]
            for row in conn.execute(
                "SELECT body FROM comments WHERE extracted_at IS NOT NULL"
            )
        ]
        arms.append(CorpusDistributional().fit(bodies))
    return arms


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.evals.fuzzy",
        description="Compare embedders on hand-reviewed vibe queries.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--set", type=Path, default=DEFAULT_SET, dest="path")
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument(
        "--arm", default=None, choices=["ngram", "distributional"],
        help="Run one arm instead of all free arms",
    )
    parser.add_argument(
        "--openai", action="store_true",
        help="Include the paid OpenAI arm; charges the daily ledger",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        cases = load_cases(args.path)
        vocabulary = descriptor_vocabulary(conn)
        arms = build_arms(conn, only=args.arm)

        if args.openai:
            from fragrance_graph.budget import Budget, BudgetExhausted
            from fragrance_graph.semantic import OpenAIEmbeddings

            budget = Budget.load(require_ledger=True)
            # Priced before it runs: ~1 token per character, and the whole
            # vocabulary plus the queries goes through once.
            projected = (
                sum(len(t) for t in vocabulary) + sum(len(c.query) for c in cases)
            ) / 4 / 1_000_000 * 0.02
            try:
                budget.check(projected)
            except BudgetExhausted as exc:
                print(f"OpenAI arm not run: {exc}")
                print(
                    f"  projected ${projected:.6f} for "
                    f"{len(vocabulary)} descriptors + {len(cases)} queries"
                )
                return 2
            arms.append(OpenAIEmbeddings(on_spend=budget.guard("fuzzy-eval")))

        reports = []
        for arm in arms:
            try:
                reports.append(run_arm(arm, vocabulary, cases, k=args.k))
            except RuntimeError as exc:
                # One arm being unavailable is not a reason to lose the
                # measurements of the others.
                print(f"arm {arm.name!r} could not run: {exc}")
        if not reports:
            return 2
        print(render(reports, k=args.k))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
