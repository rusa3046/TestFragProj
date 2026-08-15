"""Why a question the system understands still cannot be answered.

    uv run python -m fragrance_graph.coverage relative
    uv run python -m fragrance_graph.coverage density
    uv run python -m fragrance_graph.coverage snapshot

The recommender parses "Delina but less rose" correctly, finds a usable
baseline, and returns nothing useful. That is not a parsing failure or a
ranking failure, and chasing it as either would waste money. This module
measures the actual cause so acquisition can be aimed at it.

## The finding this exists to make legible

A relative comparison needs evidence about the *same attribute* at **both
ends**. Measured on the corpus of 2026-08-15:

    "Delina but less rose"        61 of 65 other bottles have no rose
                                  evidence at all; 0 have less
    "BR540 but less sweet"        the anchor itself is 1 person on 1
                                  creator — no usable baseline
    "Angels' Share but less smoky" the anchor has no smoky evidence
    "Layton but less vanilla"     works: 8 candidates with less

Three different failures wearing the same face. Only the last is a
ranking question; the first is catalogue-wide sparsity, the second is a
thin anchor, the third is a missing attribute.

## Why density, not volume

705 facts across 66 bottles sounds like coverage. It is not, because a
comparison needs two bottles to have said something about *one shared
attribute*, and 571 of those facts are singletons scattered across 600-odd
distinct values. The number that predicts whether questions get answered
is **how many bottles share an attribute**, and that is what `density`
reports.

This is the metric an enrichment experiment should be judged on. Counting
new facts rewards a run that adds six hundred more one-off descriptors,
which is exactly the outcome that looks like progress and answers nothing.
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from dataclasses import dataclass, field

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evidence import (
    AttributeFact,
    Attribution,
    Strength,
    attribute_facts,
)
from fragrance_graph.gate import MIN_SOURCES
from fragrance_graph.recommend import _matches

log = logging.getLogger("fragrance_graph.coverage")

#: The comparisons the product is judged on, plus enough others to stop a
#: single lucky case looking like coverage.
RELATIVE_CASES = (
    ("Parfums de Marly Delina", "rose"),
    ("Maison Francis Kurkdjian Baccarat Rouge 540", "sweet"),
    ("Kilian Angels' Share", "smoky"),
    ("Parfums de Marly Layton", "vanilla"),
    ("Lattafa Khamrah", "sweet"),
    ("Creed Aventus", "fruity"),
)

#: How far apart two head counts must be before the difference means
#: anything. Same margin the recommender uses.
COMPARATIVE_MARGIN = 2


class Verdict:
    """Why one candidate could or could not be compared."""

    LESS = "candidate has less"
    MORE = "candidate has more"
    UNKNOWN_PROMINENCE = "candidate has it, prominence unknown"
    NO_EVIDENCE = "no evidence either way"
    NO_BASELINE = "anchor has no usable baseline"


@dataclass
class RelativeCoverage:
    """One "X but less Y" question, and what stands between it and an
    answer."""

    anchor: str
    attribute: str
    anchor_people: int = 0
    anchor_creators: int = 0
    baseline_usable: bool = False
    baseline_problem: str = ""
    counts: Counter = field(default_factory=Counter)

    @property
    def answerable(self) -> bool:
        return self.baseline_usable and self.counts[Verdict.LESS] > 0

    @property
    def blocker(self) -> str:
        """The single thing that would have to change. Named rather than
        implied, because "not answerable" is not an instruction."""
        if not self.anchor_people:
            return f"the anchor has no {self.attribute} evidence"
        if not self.baseline_usable:
            return f"the anchor's {self.attribute} evidence is {self.baseline_problem}"
        if not self.counts[Verdict.LESS]:
            missing = self.counts[Verdict.NO_EVIDENCE]
            return (
                f"{missing} bottles have no {self.attribute} evidence, so "
                "nothing can be shown to have less"
            )
        return ""

    def render(self) -> str:
        head = f'"{self.anchor} but less {self.attribute}"'
        lines = [f"{head}  ->  {'ANSWERABLE' if self.answerable else 'blocked'}"]
        lines.append(
            f"    anchor {self.anchor_people} people / "
            f"{self.anchor_creators} creators"
            + ("" if self.baseline_usable else f"  ({self.baseline_problem})")
        )
        for verdict in (
            Verdict.LESS, Verdict.MORE, Verdict.UNKNOWN_PROMINENCE,
            Verdict.NO_EVIDENCE,
        ):
            lines.append(f"      {verdict:<34} {self.counts[verdict]}")
        if self.blocker:
            lines.append(f"    blocker: {self.blocker}")
        return "\n".join(lines)


def _baseline_problem(fact: AttributeFact | None) -> tuple[bool, str]:
    if fact is None:
        return False, "no evidence"
    if fact.strength is Strength.CONTESTED:
        return False, "contested"
    if fact.supporting.creators < MIN_SOURCES:
        return False, f"only {fact.supporting.creators} creator(s)"
    return True, ""


def relative_coverage(
    conn: psycopg.Connection,
    cases: tuple[tuple[str, str], ...] = RELATIVE_CASES,
    *,
    attribution: Attribution = Attribution.PROPOSED,
) -> list[RelativeCoverage]:
    """For each "X but less Y", classify every other bottle.

    Absence is counted as absence of *evidence*, never as evidence of
    less. That distinction is the whole reason this returns four buckets
    instead of a percentage.
    """
    names = {
        row["id"]: row["canonical_name"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    by_id: dict[int, list[AttributeFact]] = {}
    for fact in attribute_facts(conn, attribution=attribution):
        by_id.setdefault(fact.fragrance_id, []).append(fact)

    results = []
    for anchor_name, attribute in cases:
        anchor_id = next((i for i, n in names.items() if n == anchor_name), None)
        anchor_facts = by_id.get(anchor_id, []) if anchor_id else []
        anchor = next(
            (f for f in anchor_facts if _matches(f, "note", attribute)), None
        )
        usable, problem = _baseline_problem(anchor)
        row = RelativeCoverage(
            anchor=anchor_name,
            attribute=attribute,
            anchor_people=anchor.supporting.people if anchor else 0,
            anchor_creators=anchor.supporting.creators if anchor else 0,
            baseline_usable=usable,
            baseline_problem=problem,
        )
        # Over the whole catalogue, not only bottles that already have
        # facts. Iterating `by_id` hid every bottle the corpus says
        # nothing about at all — which is the largest group and the one
        # the answer most depends on.
        for frag_id in names:
            if frag_id == anchor_id:
                continue
            candidate = next(
                (
                    f
                    for f in by_id.get(frag_id, [])
                    if _matches(f, "note", attribute)
                ),
                None,
            )
            if candidate is None:
                row.counts[Verdict.NO_EVIDENCE] += 1
            elif candidate.strength is Strength.CONTESTED or not anchor:
                row.counts[Verdict.UNKNOWN_PROMINENCE] += 1
            elif (
                abs(anchor.supporting.people - candidate.supporting.people)
                < COMPARATIVE_MARGIN
            ):
                row.counts[Verdict.UNKNOWN_PROMINENCE] += 1
            elif candidate.supporting.people < anchor.supporting.people:
                row.counts[Verdict.LESS] += 1
            else:
                row.counts[Verdict.MORE] += 1
        results.append(row)
    return results


@dataclass
class AttributeDensity:
    """How many bottles the corpus can say the same thing about."""

    attribute: str
    value: str
    bottles: int
    bottles_repeated: int
    bottles_declarable: int

    @property
    def comparable(self) -> bool:
        """Two bottles with evidence on one attribute is the minimum for a
        comparison to exist at all."""
        return self.bottles >= 2


def density(
    conn: psycopg.Connection,
    *,
    attribution: Attribution = Attribution.PROPOSED,
    limit: int = 25,
) -> list[AttributeDensity]:
    """Shared attributes, most widely covered first.

    The number that predicts whether questions get answered. Counting
    facts rewards a run that adds six hundred more one-off descriptors,
    which looks like progress and answers nothing.
    """
    shared: dict[tuple[str, str], list[AttributeFact]] = {}
    for fact in attribute_facts(conn, attribution=attribution):
        if fact.supporting.people == 0:
            continue
        shared.setdefault((fact.attribute, fact.value), []).append(fact)

    rows = [
        AttributeDensity(
            attribute=attribute,
            value=value,
            bottles=len({f.fragrance_id for f in facts}),
            bottles_repeated=len(
                {f.fragrance_id for f in facts if f.supporting.people >= 2}
            ),
            bottles_declarable=len(
                {f.fragrance_id for f in facts if f.may_declare}
            ),
        )
        for (attribute, value), facts in shared.items()
    ]
    rows.sort(key=lambda r: (-r.bottles, -r.bottles_repeated, r.value))
    return rows[:limit]


#: Bottle-count thresholds the density report buckets by. Two is the
#: minimum for a comparison to exist at all; three and five mark the point
#: where an attribute starts being able to *rank* rather than merely
#: distinguish a pair.
DENSITY_BUCKETS = (1, 2, 3, 5)


@dataclass
class DensityProfile:
    """How the attribute vocabulary is distributed across the catalogue.

    The single most decision-relevant shape in the corpus. A vocabulary
    where every value sits on one bottle supports no comparison at all,
    however many values there are.
    """

    total_values: int = 0
    #: value count at each threshold, keyed by minimum bottles
    at_least: dict = field(default_factory=dict)
    exactly_one: int = 0
    #: (attribute, bottles) — coverage by attribute family rather than value
    by_attribute: dict = field(default_factory=dict)

    def render(self) -> str:
        lines = [f"  {'distinct attribute values':<34}{self.total_values:>7}"]
        lines.append(f"  {'  on exactly 1 bottle':<34}{self.exactly_one:>7}")
        for threshold in sorted(self.at_least):
            if threshold == 1:
                continue
            lines.append(
                f"  {f'  on >= {threshold} bottles':<34}"
                f"{self.at_least[threshold]:>7}"
            )
        lines += ["", f"  {'attribute family':<24}{'values':>8}{'bottles':>9}"]
        for attribute, (values, bottles) in sorted(
            self.by_attribute.items(), key=lambda kv: -kv[1][1]
        ):
            lines.append(f"  {attribute:<24}{values:>8}{bottles:>9}")
        return "\n".join(lines)


def density_profile(
    conn: psycopg.Connection,
    *,
    attribution: Attribution = Attribution.PROPOSED,
) -> DensityProfile:
    """Bucket the vocabulary by how many bottles carry each value."""
    shared: dict[tuple[str, str], set[int]] = {}
    families: dict[str, tuple[set[str], set[int]]] = {}
    for fact in attribute_facts(conn, attribution=attribution):
        if fact.supporting.people == 0:
            continue
        shared.setdefault((fact.attribute, fact.value), set()).add(
            fact.fragrance_id
        )
        values, bottles = families.setdefault(fact.attribute, (set(), set()))
        values.add(fact.value)
        bottles.add(fact.fragrance_id)

    counts = [len(ids) for ids in shared.values()]
    return DensityProfile(
        total_values=len(shared),
        at_least={
            threshold: sum(1 for n in counts if n >= threshold)
            for threshold in DENSITY_BUCKETS
        },
        exactly_one=sum(1 for n in counts if n == 1),
        by_attribute={
            attribute: (len(values), len(bottles))
            for attribute, (values, bottles) in families.items()
        },
    )


#: The comparisons the benchmark and the product are judged on. Two of
#: these name bottles the catalogue does not have, which is itself the
#: answer for those rows rather than a gap in the measurement.
BENCHMARK_COMPARISONS = (
    ("Parfums de Marly Delina", "rose"),
    ("Maison Francis Kurkdjian Baccarat Rouge 540", "sweet"),
    ("Babycat", "smoky"),
    ("Libre", "vanilla"),
    ("Parfums de Marly Layton", "vanilla"),
)


@dataclass
class ComparisonCoverage:
    """For one anchor and attribute: how many bottles can be *compared*.

    Comparable means the corpus has evidence for that attribute on the
    other bottle — enough to say more, less, or indistinguishable. It
    deliberately excludes bottles the corpus is silent about, because
    silence is not a low score and counting it as one would make every
    sparse catalogue look fully covered.
    """

    anchor: str
    attribute: str
    in_catalogue: bool = True
    anchor_evidence: bool = False
    baseline_usable: bool = False
    comparable: int = 0
    silent: int = 0
    catalogue: int = 0

    @property
    def coverage(self) -> float:
        return self.comparable / self.catalogue if self.catalogue else 0.0

    def render(self) -> str:
        label = f"{self.anchor} / {self.attribute}"
        if not self.in_catalogue:
            return f"  {label:<46} not in the catalogue"
        if not self.anchor_evidence:
            return f"  {label:<46} anchor has no {self.attribute} evidence"
        state = "usable" if self.baseline_usable else "thin"
        return (
            f"  {label:<46} baseline {state}; {self.comparable} comparable, "
            f"{self.silent} silent ({self.coverage:.1%})"
        )


def comparison_coverage(
    conn: psycopg.Connection,
    cases: tuple[tuple[str, str], ...] = BENCHMARK_COMPARISONS,
    *,
    attribution: Attribution = Attribution.PROPOSED,
) -> list[ComparisonCoverage]:
    """How much of the catalogue each benchmark comparison can reach."""
    names = {
        row["id"]: row["canonical_name"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    lookup = {n.lower(): i for i, n in names.items()}
    by_id: dict[int, list[AttributeFact]] = {}
    for fact in attribute_facts(conn, attribution=attribution):
        by_id.setdefault(fact.fragrance_id, []).append(fact)

    results = []
    for anchor_name, attribute in cases:
        anchor_id = lookup.get(anchor_name.lower())
        if anchor_id is None:
            anchor_id = next(
                (i for n, i in lookup.items() if anchor_name.lower() in n), None
            )
        row = ComparisonCoverage(
            anchor=anchor_name,
            attribute=attribute,
            in_catalogue=anchor_id is not None,
            catalogue=len(names) - 1,
        )
        if anchor_id is None:
            results.append(row)
            continue
        anchor = next(
            (f for f in by_id.get(anchor_id, []) if _matches(f, "note", attribute)),
            None,
        )
        row.anchor_evidence = anchor is not None
        row.baseline_usable = _baseline_problem(anchor)[0]
        for frag_id in names:
            if frag_id == anchor_id:
                continue
            candidate = next(
                (
                    f
                    for f in by_id.get(frag_id, [])
                    if _matches(f, "note", attribute)
                ),
                None,
            )
            if candidate is None:
                row.silent += 1
            else:
                row.comparable += 1
        results.append(row)
    return results


@dataclass
class Snapshot:
    """Everything an acquisition experiment should be judged against.

    Deliberately includes `comparable_attributes` and `answerable_relative`
    alongside the raw counts. A run that doubles `facts` and moves neither
    has bought nothing the product can use.
    """

    fragrances: int = 0
    facts: int = 0
    singleton: int = 0
    repeated: int = 0
    supported: int = 0
    contested: int = 0
    declarable: int = 0
    bottles_with_facts: int = 0
    bottles_declarable: int = 0
    distinct_values: int = 0
    comparable_attributes: int = 0
    answerable_relative: int = 0
    comments: int = 0
    creators: int = 0
    #: Every (fragrance, attribute, value) key with its head count, so a
    #: later snapshot can say *which* singletons converted rather than
    #: only that the buckets moved. Without it the corpus-wide numbers
    #: cannot be decomposed: singleton -29 and repeated +6 is consistent
    #: with two conversions and with twenty, and the difference decides
    #: whether enrichment is worth buying.
    keys: dict = field(default_factory=dict)

    def render(self, other: Snapshot | None = None) -> str:
        rows = [
            ("fragrances", self.fragrances),
            ("comments processed", self.comments),
            ("creators", self.creators),
            ("facts", self.facts),
            ("  singleton (1 person)", self.singleton),
            ("  repeated (2+)", self.repeated),
            ("  supported", self.supported),
            ("  contested", self.contested),
            ("declarable facts", self.declarable),
            ("bottles with any fact", self.bottles_with_facts),
            ("bottles declarable", self.bottles_declarable),
            ("distinct attribute values", self.distinct_values),
            ("attributes on 2+ bottles", self.comparable_attributes),
            ("answerable 'less X' cases", self.answerable_relative),
        ]
        if other is None:
            return "\n".join(f"  {k:<30} {v}" for k, v in rows)
        lines = [f"  {'':<30}{'before':>9}{'after':>9}{'delta':>9}"]
        for (k, before), (_, after) in zip(
            rows, _rows_of(other), strict=True
        ):
            delta = after - before
            mark = f"{delta:+}" if delta else "-"
            lines.append(f"  {k:<30}{before:>9}{after:>9}{mark:>9}")
        return "\n".join(lines)


def converted(before: Snapshot, after: Snapshot) -> dict[str, list[str]]:
    """Decompose two snapshots into what actually happened.

    Bucket counts alone cannot: singleton -29 and repeated +6 is
    consistent with two conversions and with twenty. This names each one.
    """
    result = {"converted": [], "new_singleton": [], "new_repeated": []}
    for key, people in after.keys.items():
        was = before.keys.get(key)
        if was is None:
            result["new_repeated" if people >= 2 else "new_singleton"].append(key)
        elif was == 1 and people >= 2:
            result["converted"].append(key)
    for values in result.values():
        values.sort()
    return result


def _rows_of(snap: Snapshot):
    return [
        ("fragrances", snap.fragrances),
        ("comments processed", snap.comments),
        ("creators", snap.creators),
        ("facts", snap.facts),
        ("  singleton (1 person)", snap.singleton),
        ("  repeated (2+)", snap.repeated),
        ("  supported", snap.supported),
        ("  contested", snap.contested),
        ("declarable facts", snap.declarable),
        ("bottles with any fact", snap.bottles_with_facts),
        ("bottles declarable", snap.bottles_declarable),
        ("distinct attribute values", snap.distinct_values),
        ("attributes on 2+ bottles", snap.comparable_attributes),
        ("answerable 'less X' cases", snap.answerable_relative),
    ]


def snapshot(
    conn: psycopg.Connection,
    *,
    attribution: Attribution = Attribution.PROPOSED,
) -> Snapshot:
    """One reading of everything an experiment could move."""
    facts = [
        f
        for f in attribute_facts(conn, attribution=attribution)
        if f.supporting.people > 0
    ]
    shared: dict[tuple[str, str], set[int]] = {}
    for fact in facts:
        shared.setdefault((fact.attribute, fact.value), set()).add(
            fact.fragrance_id
        )
    relative = relative_coverage(conn, attribution=attribution)
    return Snapshot(
        keys={
            f"{f.fragrance_id}|{f.attribute}|{f.value}": f.supporting.people
            for f in facts
        },
        fragrances=conn.execute("SELECT count(*) FROM fragrances").fetchone()[0],
        facts=len(facts),
        singleton=sum(1 for f in facts if f.supporting.people == 1),
        repeated=sum(1 for f in facts if f.supporting.people >= 2),
        supported=sum(1 for f in facts if f.strength is Strength.SUPPORTED),
        contested=sum(1 for f in facts if f.strength is Strength.CONTESTED),
        declarable=sum(1 for f in facts if f.may_declare),
        bottles_with_facts=len({f.fragrance_id for f in facts}),
        bottles_declarable=len(
            {f.fragrance_id for f in facts if f.may_declare}
        ),
        distinct_values=len(shared),
        comparable_attributes=sum(1 for ids in shared.values() if len(ids) >= 2),
        answerable_relative=sum(1 for r in relative if r.answerable),
        comments=conn.execute(
            "SELECT count(*) FROM comments WHERE extracted_at IS NOT NULL"
        ).fetchone()[0],
        creators=conn.execute(
            "SELECT count(DISTINCT source_channel) FROM comments"
            " WHERE source_channel <> ''"
        ).fetchone()[0],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.coverage",
        description="Why understood questions still cannot be answered.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("relative", help='"X but less Y" answerability')
    d = sub.add_parser("density", help="Attributes shared across bottles")
    d.add_argument("--limit", type=int, default=25)
    sub.add_parser("snapshot", help="Everything an experiment could move")
    sub.add_parser("profile", help="How the vocabulary spreads over bottles")
    sub.add_parser("comparisons", help="Benchmark comparison reachability")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "relative":
            rows = relative_coverage(conn)
            for row in rows:
                print(row.render())
                print()
            answerable = sum(1 for r in rows if r.answerable)
            print(f"{answerable}/{len(rows)} relative comparisons answerable")
        elif args.command == "density":
            print(f"  {'attribute = value':<34}{'bottles':>9}{'2+ people':>11}"
                  f"{'declarable':>12}")
            for row in density(conn, limit=args.limit):
                label = f"{row.attribute} = {row.value}"
                print(f"  {label:<34}{row.bottles:>9}{row.bottles_repeated:>11}"
                      f"{row.bottles_declarable:>12}")
        elif args.command == "profile":
            print(density_profile(conn).render())
        elif args.command == "comparisons":
            rows = comparison_coverage(conn)
            for row in rows:
                print(row.render())
            reachable = sum(1 for r in rows if r.baseline_usable and r.comparable)
            print(f"\n  {reachable}/{len(rows)} benchmark comparisons reachable")
        else:
            print(snapshot(conn).render())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
