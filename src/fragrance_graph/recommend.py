"""Choosing bottles from evidence, and saying why.

    from fragrance_graph.recommend import recommend
    for r in recommend(conn, "a crowd-pleasing fragrance with raspberry"):
        print(r.explain())

No model runs here. A plan comes in, evidence decides, and every line of
the output points at claims somebody actually wrote.

## The staged shape, and why it is not a weighted score

The obvious design is one formula with six coefficients. It is also
unfalsifiable: when it returns something wrong you cannot tell which term
did it, and tuning one weight silently moves everything. So ranking runs
in stages, each of which can be inspected on its own:

    1. hard constraints      a filter — fails are not candidates
    2. minimum evidence      a bottle nothing is known about is not a result
    3. soft preferences      matched, each contributing a named reason
    4. anchor proximity      what the comparison graph already says
    5. independence          more separate humans breaks ties

Nothing is multiplied by a magic number. A result carries the reasons that
produced it, so "why is this third" has an answer you can read.

## What may be said out loud

Every candidate carries two kinds of support and they never mix:

    reasons   — things the corpus supports, each with its strength
    caveats   — things the corpus says that the asker may not want

A reason built from `OBSERVED` evidence is phrased as "one commenter said";
a reason built from `SUPPORTED` evidence is phrased as a fact. That is not
decoration — it is the difference between reporting and asserting, and
`Recommendation.explain` is the only place the wording is chosen.

## Absence is not evidence of absence

"No raspberry evidence" means nobody in this corpus mentioned raspberry
for that bottle. It does not mean the bottle has no raspberry. The corpus
covers 76 bottles and 57 creators; it is a sample, not a census. So a
failed hard constraint removes a candidate silently rather than producing
"X does not contain raspberry", which would be a claim nobody made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from fragrance_graph.evidence import (
    AttributeFact,
    Attribution,
    Strength,
    attribute_facts,
    canonical_facts,
)
from fragrance_graph.plan import (
    CONCEPTS,
    Constraint,
    Direction,
    Intent,
    Preference,
    QueryPlan,
    parse_with_corpus,
)
from fragrance_graph.query import similar_to

log = logging.getLogger("fragrance_graph.recommend")

#: A candidate has to have *something* known about it. Without this the
#: ranking returns bottles nobody has said anything about, ordered by
#: nothing, which reads as a recommendation and is not one.
MIN_FACTS_TO_RECOMMEND = 1

#: How many bottles come back by default. Small on purpose: the corpus can
#: honestly distinguish a handful, and a list of twenty implies a precision
#: the evidence does not have.
DEFAULT_LIMIT = 5


@dataclass(frozen=True)
class Reason:
    """One evidence-backed thing to say about a candidate."""

    kind: str
    text: str
    strength: Strength
    people: int = 0
    creators: int = 0
    claim_ids: tuple[int, ...] = ()
    #: True when any contributing attribution was machine-inferred.
    inferred: bool = False

    @property
    def declarable(self) -> bool:
        return self.strength.may_declare and not self.inferred

    def phrase(self) -> str:
        """The same fact, worded to match how well it is known.

        The whole provenance discipline arrives here or nowhere. A reader
        never sees a `Strength`; they see "8 people across 4 channels say"
        or "one commenter said", and those must not be interchangeable.
        """
        if self.kind == "graph":
            return self.text
        if self.strength is Strength.CANONICAL:
            return f"{self.text} (official listing)"
        if self.declarable:
            return f"{self.text} — {_people(self.people)} across {_sources(self.creators)}"
        if self.inferred:
            # Disclosure is not conditional on the head count. Three
            # inferred contributors phrased as "3 people said" hides
            # exactly the thing a reader would want to weigh: nobody named
            # this bottle, a rule read it off the video.
            return (
                f"{_people(self.people)} said {self.text}, on videos about "
                "this bottle rather than naming it"
            )
        if self.people >= 2:
            return f"{_people(self.people)} said {self.text}"
        return f"one commenter said {self.text}"


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def _sources(n: int) -> str:
    return "1 channel" if n == 1 else f"{n} channels"


@dataclass
class Recommendation:
    """One candidate, everything behind it, and everything against it."""

    fragrance_id: int
    name: str
    reasons: list[Reason] = field(default_factory=list)
    caveats: list[Reason] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    #: Soft preferences this candidate does not satisfy. Reported rather
    #: than silently costed, so a reader can see what they are trading.
    unmatched: list[str] = field(default_factory=list)
    score: float = 0.0
    #: How many separate humans stand behind everything cited.
    people: int = 0
    creators: int = 0

    @property
    def declarable_reasons(self) -> list[Reason]:
        return [r for r in self.reasons if r.declarable]

    def explain(self) -> str:
        lines = [f"{self.name}"]
        if self.reasons:
            lines.append("  why it matches:")
            lines += [f"    - {r.phrase()}" for r in self.reasons]
        if self.caveats:
            lines.append("  worth knowing:")
            lines += [f"    - {r.phrase()}" for r in self.caveats]
        if self.unmatched:
            lines.append(f"  no evidence either way for: {', '.join(self.unmatched)}")
        lines.append(
            f"  evidence: {_people(self.people)} across {_sources(self.creators)}"
        )
        return "\n".join(lines)


@dataclass
class Answer:
    """A whole response: the plan that produced it and what came back."""

    plan: QueryPlan
    results: list[Recommendation] = field(default_factory=list)
    #: Why the answer is empty or partial, in words a person can act on.
    note: str = ""

    def render(self) -> str:
        parts = [self.plan.render(), ""]
        if self.note:
            parts += [self.note, ""]
        parts += [r.explain() for r in self.results] or ["  (no candidates)"]
        return "\n".join(parts)


def _facts_by_fragrance(
    conn: psycopg.Connection, attribution: Attribution
) -> dict[int, list[AttributeFact]]:
    grouped: dict[int, list[AttributeFact]] = {}
    # Canonical first, so a catalogue-stated note is present even for a
    # bottle nobody has commented on. They are separate rows and never
    # merge with community facts.
    for fact in canonical_facts(conn) + attribute_facts(conn, attribution=attribution):
        grouped.setdefault(fact.fragrance_id, []).append(fact)
    return grouped


def _matches(fact: AttributeFact, attribute: str, value: str) -> bool:
    """Whether a fact answers a request for (attribute, value).

    Substring rather than equality because the corpus writes "raspberry
    notes", "raspberry Berry note" and "raspberry" for one thing, and the
    normaliser deliberately does not collapse compound descriptions — the
    raw wording is evidence and gets quoted.
    """
    if fact.attribute != attribute:
        return False
    return value == fact.value or value in fact.value.split()


def _concept_matches(fact: AttributeFact, concept: str) -> bool:
    """Whether a fact expresses a concept the corpus has no word for.

    "Crowd-pleasing" is never written; "compliment" is written 29 times.
    The concept's word list is what bridges them.
    """
    words = CONCEPTS.get(concept, ())
    haystack = f"{fact.attribute} {fact.value}"
    return any(word in haystack for word in words)


def _fact_reason(fact: AttributeFact, kind: str) -> Reason:
    return Reason(
        kind=kind,
        text=f"{fact.value}" if fact.attribute in ("note", "vibe")
        else f"{fact.attribute} {fact.value}",
        strength=fact.strength,
        people=fact.supporting.people,
        creators=fact.supporting.creators,
        claim_ids=fact.supporting.claim_ids,
        inferred=fact.inferred,
    )


def _satisfies_hard(facts: list[AttributeFact], constraint: Constraint) -> AttributeFact | None:
    for fact in facts:
        if _matches(fact, constraint.attribute, constraint.value):
            if fact.strength.may_retrieve:
                return fact
    return None


def recommend(
    conn: psycopg.Connection,
    text: str,
    *,
    limit: int = DEFAULT_LIMIT,
    attribution: Attribution = Attribution.PROPOSED,
) -> Answer:
    """Answer a request from evidence, or explain why it cannot be answered.

    `attribution` defaults to PROPOSED because retrieval is where inference
    is affordable. Nothing that reaches a sentence depends on it: wording
    is chosen by `Reason.declarable`, which excludes inferred support
    regardless of this setting.
    """
    plan = parse_with_corpus(conn, text)
    answer = Answer(plan=plan)

    if plan.refusal:
        answer.note = plan.refusal
        return answer
    if plan.intent is not Intent.RECOMMEND:
        answer.note = (
            f"{plan.intent} requests are answered by `query {plan.intent}`, "
            "not by the recommender"
        )
        return answer

    grouped = _facts_by_fragrance(conn, attribution)
    names = {
        row["id"]: row["canonical_name"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    neighbours = _anchor_neighbours(conn, plan)

    candidates: list[Recommendation] = []
    rejected_by_hard = 0
    # A bottle the comparison graph connects to the anchor is a candidate
    # even when nobody has described it, because that connection is itself
    # the best-evidenced thing in the corpus. Iterating only over bottles
    # with attribute facts dropped them entirely.
    considered = set(grouped) | set(neighbours)
    for frag_id in considered:
        facts = grouped.get(frag_id, [])
        if frag_id == plan.anchor_id:
            continue  # nobody asks for the bottle they already named
        if len(facts) < MIN_FACTS_TO_RECOMMEND and frag_id not in neighbours:
            continue
        result = _score(frag_id, names.get(frag_id, "?"), facts, plan, neighbours)
        if result is None:
            rejected_by_hard += 1
            continue
        candidates.append(result)

    candidates.sort(key=lambda r: (-r.score, -r.people, r.name))
    answer.results = candidates[:limit]
    answer.note = _note(plan, candidates, rejected_by_hard, grouped)
    return answer


def _anchor_neighbours(conn: psycopg.Connection, plan: QueryPlan) -> dict[int, int]:
    """Bottles the corpus already connects to the anchor, and how strongly.

    This is the existing comparison graph doing exactly what it was built
    for. It is *evidence*, not similarity computed from attributes, so an
    anchor query stands on the same footing as everything else here.
    """
    if plan.anchor_id is None:
        return {}
    return {
        related.fragrance_id: related.pair_commenters
        for related in similar_to(conn, plan.anchor_id)
    }


def _score(
    frag_id: int,
    name: str,
    facts: list[AttributeFact],
    plan: QueryPlan,
    neighbours: dict[int, int],
) -> Recommendation | None:
    result = Recommendation(fragrance_id=frag_id, name=name)

    # Stage 1 — hard constraints filter. A miss is not a low score.
    for constraint in plan.hard:
        fact = _satisfies_hard(facts, constraint)
        if fact is None:
            return None
        result.matched.append(f"{constraint.attribute}={constraint.value}")
        result.reasons.append(_fact_reason(fact, "constraint"))

    # Stage 3 — soft preferences, each a named reason or a named gap.
    for preference in plan.soft:
        fact = _preference_fact(facts, preference)
        if fact is None:
            result.unmatched.append(f"{preference.attribute}={preference.value}")
            continue
        if preference.direction in (Direction.LOW, Direction.LESS_THAN_ANCHOR):
            # Evidence that a candidate *has* the thing being avoided is a
            # caveat, and it costs the candidate rather than helping it.
            result.caveats.append(_fact_reason(fact, "avoid"))
            result.score -= 1.0 + _weight(fact)
        else:
            result.reasons.append(_fact_reason(fact, "prefer"))
            result.score += 1.0 + _weight(fact)

    # Stage 4 — what the comparison graph already says about the anchor.
    if frag_id in neighbours:
        support = neighbours[frag_id]
        result.reasons.append(
            Reason(
                kind="graph",
                text=f"{_people(support)} compared this with {plan.anchor}",
                strength=Strength.SUPPORTED if support >= 3 else Strength.OBSERVED,
                people=support,
            )
        )
        result.score += 2.0 + min(support, 5) * 0.2

    if not result.reasons and not plan.hard:
        return None

    # Stage 5 — independence breaks ties.
    # Graph reasons count too: "3 people compared this with Delina" is
    # three humans. Excluding them reported "0 people" for a candidate
    # standing entirely on the comparison graph, which is the oldest and
    # best-evidenced thing in the corpus.
    everything = result.reasons + result.caveats
    result.people = max((r.people for r in everything), default=0)
    result.creators = max((r.creators for r in everything), default=0)
    result.score += min(result.people, 10) * 0.05
    return result


def _preference_fact(
    facts: list[AttributeFact], preference: Preference
) -> AttributeFact | None:
    if preference.attribute == "concept":
        for fact in facts:
            if _concept_matches(fact, preference.value):
                return fact
        return None
    for fact in facts:
        if _matches(fact, preference.attribute, preference.value):
            return fact
    return None


def _weight(fact: AttributeFact) -> float:
    """How much a matched fact is worth, by how well it is known.

    Deliberately coarse and monotonic. The point is that better-evidenced
    matches outrank worse-evidenced ones; the exact spacing is not a tuned
    parameter and nothing should depend on its precise value.
    """
    return {
        Strength.SUPPORTED: 1.0,
        Strength.CONTESTED: 0.3,
        Strength.REPEATED: 0.6,
        Strength.OBSERVED: 0.2,
        Strength.CANONICAL: 0.8,
        Strength.INSUFFICIENT: 0.0,
    }[fact.strength]


def _note(
    plan: QueryPlan,
    candidates: list[Recommendation],
    rejected: int,
    grouped: dict[int, list[AttributeFact]],
) -> str:
    if candidates:
        weak = [r for r in candidates if not r.declarable_reasons]
        if len(weak) == len(candidates):
            return (
                "Every match below rests on single observations. They are "
                "worth looking at, not worth trusting as consensus."
            )
        return ""
    if plan.hard:
        wanted = ", ".join(f"{c.attribute}={c.value}" for c in plan.hard)
        return (
            f"No bottle in the corpus has evidence for {wanted}. "
            f"{len(grouped)} bottles were considered and {rejected} were "
            "ruled out by that requirement. This means nobody in these "
            "comments mentioned it — not that no such fragrance exists."
        )
    return "Nothing in the corpus speaks to this request."
