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
    name_facts,
)
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.plan import (
    CONCEPTS,
    RELATED_CONCEPTS,
    Constraint,
    Direction,
    Intent,
    Preference,
    QueryPlan,
    parse_with_corpus,
)
from fragrance_graph.query import pair_stats, similar_to

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
    #: How many people said the opposite. Carried so a disputed fact cannot
    #: be cited as flat support — "14 people say it projects" read as
    #: agreement when 8 of the 22 said the opposite.
    against: int = 0

    @property
    def declarable(self) -> bool:
        return self.strength.may_declare and not self.inferred

    def phrase(self) -> str:
        """The same fact, worded to match how well it is known.

        The whole provenance discipline arrives here or nowhere. A reader
        never sees a `Strength`; they see "8 people across 4 channels say"
        or "one commenter said", and those must not be interchangeable.
        """
        if self.kind in ("graph", "semantic", "absence"):
            return self.text
        if self.strength is Strength.CANONICAL:
            return f"{self.text} (official listing)"
        # Inference is disclosed in *every* branch. It used to be handled
        # only where the evidence was too weak to declare, so a contested
        # or well-supported fact whose counts included machine-attributed
        # contributors read as "14 people say so" with nothing to say some
        # of the fourteen never named the bottle. Found by the audit in
        # `audit.py`, which is the seventh instance of a rule holding in
        # one branch and not its neighbours.
        inferred_note = (
            ", some inferred from the video rather than named"
            if self.inferred
            else ""
        )
        if self.against:
            return (
                f"{self.text} — {_people(self.people)} say so and "
                f"{self.against} disagree{inferred_note}"
            )
        if self.declarable:
            return (
                f"{self.text} — {_people(self.people)} across "
                f"{_sources(self.creators)}{inferred_note}"
            )
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
    # Name-derived notes first. They are separate rows, never merge with
    # community facts, and are INSUFFICIENT — present only so a bottle
    # called "Rose 01" can be demoted for somebody asking for less rose.
    for fact in name_facts(conn) + attribute_facts(conn, attribution=attribution):
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
    """Whether a fact is evidence for *this* concept — not a related one."""
    haystack = f"{fact.attribute} {fact.value}"
    return any(word in haystack for word in CONCEPTS.get(concept, ()))


def _concept_evidence(
    facts: list[AttributeFact], wanted: str
) -> tuple[AttributeFact, str] | None:
    """The best evidence for a concept, and **which concept it is for**.

    Direct evidence wins. Failing that, a related concept may bring the
    candidate into consideration — "crowd-pleasing" is a mass-appeal
    question, the corpus has one attached mass-appeal claim and ten
    compliment claims, so refusing to look at compliments answers nothing.

    What must not happen is the substitution going unmentioned. Somebody
    being complimented is a fact about them and their circle; a fragrance
    being broadly likable is a fact about everyone. The returned concept
    name is the one the *evidence* supports, and `_score` puts that name
    in the sentence rather than the one the user typed.
    """
    for fact in facts:
        if _concept_matches(fact, wanted):
            return fact, wanted
    for related in RELATED_CONCEPTS.get(wanted, ()):
        for fact in facts:
            if _concept_matches(fact, related):
                return fact, related
    return None


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
        against=fact.opposing.people
        if fact.strength is Strength.CONTESTED else 0,
    )


def _satisfies_hard(
    facts: list[AttributeFact], constraint: Constraint
) -> AttributeFact | None:
    """The best evidence that a candidate really has what was required.

    Two exclusions beyond "may_retrieve". A fact most people contradict
    does not satisfy a requirement for the thing they contradict — asking
    for strong projection must not return a bottle three people call weak
    and two call strong. And a note read out of the product name is not
    evidence anybody smelled it, so it cannot satisfy a requirement either;
    `_score` handles the soft path equivalently.
    """
    for fact in facts:
        if not _matches(fact, constraint.attribute, constraint.value):
            continue
        if fact.from_name or not fact.strength.may_retrieve:
            continue
        if fact.opposing.people > fact.supporting.people:
            continue
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
    if plan.intent is Intent.PROFILE:
        return _profile(conn, plan)
    if plan.intent is Intent.COMPARE:
        return _compare(conn, plan)
    if plan.intent is not Intent.RECOMMEND:
        answer.note = plan.refusal or (
            "an explanation needs a recommendation to explain"
        )
        return answer

    # Words the rules could not place go to the vector layer. It retrieves
    # and never asserts: what comes back is a *claim*, which is then graded
    # and worded by the ordinary evidence path like anything else.
    semantic = _semantic_candidates(conn, plan)

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
    considered = set(grouped) | set(neighbours) | set(semantic)
    for frag_id in considered:
        facts = grouped.get(frag_id, [])
        if frag_id == plan.anchor_id:
            continue  # nobody asks for the bottle they already named
        if (
            len(facts) < MIN_FACTS_TO_RECOMMEND
            and frag_id not in neighbours
            and frag_id not in semantic
        ):
            continue
        result = _score(
            frag_id, names.get(frag_id, "?"), facts, plan, neighbours,
            semantic.get(frag_id), grouped.get(plan.anchor_id or -1, []),
        )
        if result is None:
            rejected_by_hard += 1
            continue
        candidates.append(result)

    candidates.sort(key=lambda r: (-r.score, -r.people, r.name))
    answer.results = candidates[:limit]
    answer.note = _note(plan, candidates, rejected_by_hard, grouped)
    return answer


def _profile(conn: psycopg.Connection, plan: QueryPlan) -> Answer:
    """Everything the corpus supports about one bottle, graded.

    Contested facts lead. "What do people disagree about for Delina" is a
    question the corpus can answer better than most, and burying the
    disputes under the agreements would waste the one thing this evidence
    model does that a note list cannot.
    """
    answer = Answer(plan=plan)
    facts = attribute_facts(conn, fragrance_id=plan.anchor_id,
                            attribution=Attribution.PROPOSED)
    if not facts:
        answer.note = f"Nothing in the corpus describes {plan.anchor}."
        return answer

    result = Recommendation(fragrance_id=plan.anchor_id or 0, name=plan.anchor or "?")
    for fact in facts:
        reason = _fact_reason(fact, "profile")
        if fact.strength is Strength.CONTESTED:
            result.caveats.append(reason)
        elif fact.strength.may_retrieve:
            result.reasons.append(reason)
    result.reasons.sort(key=lambda x: (-x.people, x.text))
    result.people = max((x.people for x in result.reasons + result.caveats), default=0)
    result.creators = max(
        (x.creators for x in result.reasons + result.caveats), default=0
    )
    answer.results = [result]
    if not result.caveats:
        answer.note = f"Nothing in the corpus is contested about {plan.anchor}."
    return answer


def _compare(conn: psycopg.Connection, plan: QueryPlan) -> Answer:
    """What the corpus says connects two bottles, from the pair evidence.

    Delegates to `query.pair_stats` rather than reimplementing it: the pair
    product has been right about this since before the recommender existed,
    and a second counter would be a second answer.
    """
    answer = Answer(plan=plan)
    if plan.anchor_id is None or plan.other_id is None:
        answer.note = "both fragrances have to be in the catalogue to compare them"
        return answer

    evidence = pair_stats(conn, plan.anchor_id, plan.other_id)
    result = Recommendation(
        fragrance_id=plan.other_id,
        name=f"{plan.anchor} vs {plan.other}",
        people=evidence.commenters,
        creators=evidence.creators,
    )
    if evidence.commenters:
        result.reasons.append(
            Reason(
                kind="graph",
                text=(
                    f"{_people(evidence.commenters)} across "
                    f"{_sources(evidence.creators)} connected these two"
                ),
                strength=Strength.SUPPORTED
                if evidence.commenters >= MIN_COMMENTERS
                and evidence.creators >= MIN_SOURCES
                else Strength.OBSERVED,
                people=evidence.commenters,
                creators=evidence.creators,
            )
        )
    for frag_id, label in ((plan.anchor_id, plan.anchor), (plan.other_id, plan.other)):
        for fact in attribute_facts(conn, fragrance_id=frag_id,
                                    attribution=Attribution.PROPOSED):
            if fact.strength.may_declare or fact.strength is Strength.CONTESTED:
                reason = _fact_reason(fact, "profile")
                result.reasons.append(
                    Reason(**{**reason.__dict__, "text": f"{label}: {reason.text}"})
                )
    if not result.reasons:
        answer.note = "Nobody in the corpus has connected or described these two."
        return answer
    answer.results = [result]
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
        related.fragrance_id: (related.pair_commenters, related.pair_sources)
        for related in similar_to(conn, plan.anchor_id)
    }


def _semantic_candidates(conn: psycopg.Connection, plan: QueryPlan) -> dict:
    """Bottles the vector layer points at for words the rules could not.

    Only for `unparsed` terms. A word the structured layer understood is
    better served by the structured layer — it knows the attribute, the
    direction and the strength, and a vector knows none of those.
    """
    if not plan.unparsed:
        return {}
    from fragrance_graph.semantic import candidates_for

    found = {}
    for term in plan.unparsed:
        for frag_id, match in candidates_for(conn, term).items():
            best = found.get(frag_id)
            if best is None or match.score > best.score:
                found[frag_id] = match
    return found


def _score(
    frag_id: int,
    name: str,
    facts: list[AttributeFact],
    plan: QueryPlan,
    neighbours: dict[int, int],
    semantic=None,
    anchor_facts: list[AttributeFact] | None = None,
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
        if preference.relative_to_anchor:
            _score_comparative(
                result, facts, preference, plan, anchor_facts or []
            )
            continue
        if preference.attribute == "concept":
            _score_concept(result, facts, preference)
            continue
        fact = _preference_fact(facts, preference)
        if fact is None:
            result.unmatched.append(f"{preference.attribute}={preference.value}")
            continue
        if preference.direction in (Direction.LOW, Direction.LESS_THAN_ANCHOR):
            # Evidence that a candidate *has* the thing being avoided is a
            # caveat, and it costs the candidate rather than helping it.
            # Must dominate the anchor bonus. Graph proximity tops out at
            # +2.0 and this starts at -3.0, so a candidate carrying the very
            # trait the user asked to avoid can never be lifted above one
            # that does not by being close to the anchor. Somebody who says
            # "less rose" has ruled on the trade-off already.
            result.caveats.append(_fact_reason(fact, "avoid"))
            result.score -= 3.0 + _weight(fact)
        elif fact.opposing.people > fact.supporting.people:
            # Asked for strong projection, and more people say it is weak
            # than strong. Citing that as a reason to pick the bottle
            # inverts the evidence; it belongs in what the reader should
            # know, and it costs rather than helps.
            result.caveats.append(_fact_reason(fact, "disputed"))
            result.score -= 0.5
        else:
            result.reasons.append(_fact_reason(fact, "prefer"))
            result.score += 1.0 + _weight(fact)

    # Stage 3b — what the vector layer found, quoting the human's words.
    # Capped at OBSERVED whatever the underlying fact scores: a phrase
    # being close to another phrase is not people agreeing, and letting a
    # similarity score raise a Strength is the exact laundering the
    # evidence model forbids.
    if semantic is not None:
        result.reasons.append(
            Reason(
                kind="semantic",
                text=(
                    f"someone described it as {semantic.text!r}"
                    + (
                        ", on a video about this bottle rather than naming it"
                        if semantic.inferred
                        else ""
                    )
                ),
                strength=Strength.OBSERVED,
                people=1,
                creators=1,
                claim_ids=(semantic.claim_id,),
                inferred=semantic.inferred,
            )
        )
        result.score += 0.5

    # Stage 4 — what the comparison graph already says about the anchor.
    if frag_id in neighbours:
        support, sources = neighbours[frag_id]
        # Graded by the same rule as everything else. Three people in one
        # creator's comment section is one room, and calling that SUPPORTED
        # let anchor results skip the independence bar the whole product
        # rests on.
        strong = support >= MIN_COMMENTERS and sources >= MIN_SOURCES
        result.reasons.append(
            Reason(
                kind="graph",
                text=(
                    f"{_people(support)} across {_sources(sources)} compared "
                    f"this with {plan.anchor}"
                ),
                strength=Strength.SUPPORTED if strong else Strength.OBSERVED,
                people=support,
                creators=sources,
            )
        )
        # Capped below the avoidance penalty on purpose; see `_score`.
        result.score += 1.0 + min(support, 5) * 0.2

    # A request made only of things to avoid still has candidates: the
    # bottles nobody has called smoky. But a bottle nobody has said
    # *anything* about is not one of them — returning it ranks the unknown
    # above the known and produces a list ordered by nothing.
    if not result.reasons and not result.caveats:
        described = [f for f in facts if not f.from_name and f.strength.may_retrieve]
        if not described or not plan.soft:
            return None
        # Only plainly avoided things. An attribute under a *comparative*
        # is owned by `_score_comparative`, which has already reported that
        # the corpus cannot settle it — claiming "no rose evidence" as a
        # reason to pick the bottle while also saying "no evidence either
        # way" is the answer contradicting itself in two lines.
        avoided = ", ".join(
            p.value for p in plan.soft if p.direction is Direction.LOW
        )
        if not avoided:
            return None
        best = max(f.supporting.people for f in described)
        sources = max(f.supporting.creators for f in described)
        result.reasons.append(
            Reason(
                kind="absence",
                text=(
                    # Deliberately "no comment in this corpus calls it X"
                    # rather than "it is not X". The corpus is a sample of
                    # 57 creators, not a census, and the difference is the
                    # whole absence-is-not-evidence rule. The counts are
                    # stated so the reader can weigh how much silence is
                    # worth: silence from 21 people means more than silence
                    # from one.
                    # "No evidence recorded for X" rather than "no comment
                    # calls it X". The check behind this is token matching
                    # over normalised values, so a commenter writing
                    # "smoke" where the request said "smoky" is missed —
                    # the sentence would have been false, not merely
                    # cautious. What is true is that nothing matched.
                    f"no {avoided} evidence recorded for it; "
                    f"{_people(best)} across {_sources(sources)} "
                    "have described it"
                ),
                strength=Strength.OBSERVED,
                people=best,
                creators=sources,
            )
        )
        result.score += len([f for f in described if f.strength.may_declare]) * 0.3

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


#: How much less of something counts as "less". Two people describing a
#: bottle as rosy against eight is a real difference; two against three is
#: noise dressed as a comparison.
COMPARATIVE_MARGIN = 2


def _score_concept(
    result: Recommendation, facts: list[AttributeFact], preference: Preference
) -> None:
    """Score a social-reaction concept, naming what the evidence supports.

    The substitution is the whole point of the separate path: a request
    for mass appeal answered from compliment evidence must say
    "compliments", not "crowd pleasing".
    """
    found = _concept_evidence(facts, preference.value)
    if found is None:
        result.unmatched.append(f"concept={preference.value}")
        return
    fact, actual = found
    reason = _fact_reason(fact, "concept")
    if actual == preference.value:
        text = f"{actual}: {reason.text}"
        bonus = 1.0 + _weight(fact)
    else:
        # Related, not the same. Said in the sentence, and worth less.
        text = (
            f"{actual}: {reason.text} — related to {preference.value}, "
            "which the corpus does not speak to directly"
        )
        bonus = 0.4
    scored = Reason(**{**reason.__dict__, "text": text})
    if preference.direction is Direction.LOW:
        result.caveats.append(scored)
        result.score -= bonus
    else:
        result.reasons.append(scored)
        result.score += bonus


def _score_comparative(
    result: Recommendation,
    facts: list[AttributeFact],
    preference: Preference,
    plan: QueryPlan,
    anchor_facts: list[AttributeFact],
) -> None:
    """"Less rose than Delina" — a comparison, not an avoidance.

    Collapsing this to "avoid rose" answers a different question. Somebody
    who loves Delina wants a bottle *like* it with the rose dialled down;
    the avoidance reading throws away everything they said they liked and
    returns bottles with no rose and nothing else in common.

    So the comparison is made against the anchor's own rose evidence, and
    when the corpus cannot establish both sides the honest outcome is to
    say so. "Insufficient comparative evidence" is a real answer;
    silently downgrading to a different constraint is not.
    """
    anchor_level = _prominence(anchor_facts, preference)
    candidate_level = _prominence(facts, preference)

    if anchor_level is None:
        result.unmatched.append(
            f"{preference.value} vs {plan.anchor}: nobody has recorded how "
            f"much {preference.value} {plan.anchor} has, so there is no "
            "baseline to compare against"
        )
        return
    if not anchor_level.usable_as_baseline:
        why = (
            "people disagree about it"
            if anchor_level.contested
            else f"the evidence comes from {_sources(anchor_level.creators)}"
        )
        result.unmatched.append(
            f"{preference.value} vs {plan.anchor}: no usable baseline — {why}"
        )
        return
    if candidate_level is None:
        # Absence is not a low score. Nobody having mentioned rose here is
        # not evidence there is less of it, so this neither helps nor
        # hurts — it is reported as the open question it is.
        result.unmatched.append(
            f"{preference.value} vs {plan.anchor}: no {preference.value} "
            "evidence recorded for this bottle either way"
        )
        return

    if candidate_level.contested:
        result.caveats.append(
            Reason(
                kind="comparative",
                text=(
                    f"{preference.value}: people disagree about this bottle, "
                    "so it cannot be compared"
                ),
                strength=Strength.CONTESTED,
                people=candidate_level.people,
            )
        )
        return

    wants_less = preference.direction is Direction.LESS_THAN_ANCHOR
    difference = anchor_level.people - candidate_level.people
    if not wants_less:
        difference = -difference
    if difference < COMPARATIVE_MARGIN:
        result.caveats.append(
            Reason(
                kind="comparative",
                text=(
                    f"{preference.value}: {_people(candidate_level.people)} "
                    f"here against {_people(anchor_level.people)} for "
                    f"{plan.anchor} — not a clear difference"
                ),
                strength=Strength.OBSERVED,
                people=candidate_level.people,
            )
        )
        result.score -= 1.0
        return

    result.reasons.append(
        Reason(
            kind="comparative",
            text=(
                # Reports the evidence, not the conclusion drawn from it.
                # "Less rose than Delina" is a sentence nobody wrote — it
                # is inferred from how many people mentioned rose on each
                # side, which is a coarse proxy for prominence. Stating the
                # two counts lets the reader draw the comparison the system
                # used for ranking without the system asserting it.
                f"{preference.value} mentioned by "
                f"{_people(candidate_level.people)} here, "
                f"{_people(anchor_level.people)} across "
                f"{_sources(anchor_level.creators)} for {plan.anchor} "
                f"({'fewer' if wants_less else 'more'} — which is why it "
                "ranks here)"
            ),
            strength=Strength.OBSERVED,
            people=max(candidate_level.people, 1),
        )
    )
    result.score += 2.0


@dataclass(frozen=True)
class Level:
    """How prominent something is for one bottle, and whether that is
    solid enough to compare with."""

    people: int
    creators: int
    contested: bool

    @property
    def usable_as_baseline(self) -> bool:
        """A baseline has to be at least as well established as anything
        else this system states.

        Two ways it fails. A **contested** fact cannot ground a comparison:
        if three people say Delina is rosy and ten say it is not, "less
        rose than Delina" has no agreed left-hand side, and the ten
        dissenters vanish from both the arithmetic and the sentence.

        A fact confined to **one creator's audience** cannot either. The
        whole system treats one channel as one room, and letting that room
        establish a directional difference — while handing the result a
        ranking bonus — is the independence rule holding everywhere except
        the one place that produces a comparative sentence.
        """
        return not self.contested and self.creators >= MIN_SOURCES


def _prominence(
    facts: list[AttributeFact], preference: Preference
) -> Level | None:
    """How prominent this quality is for one bottle, or None if nobody has
    spoken either way.

    Head count stands in for prominence. It is a proxy and a coarse one —
    ten people mentioning rose means rose is noticeable, not that it
    dominates — and it is the only one this corpus supports, because
    nobody records intensity. Stated rather than dressed up as a measure.

    Opposing evidence is carried rather than dropped, because a comparison
    built on a disputed premise is worse than no comparison.
    """
    for fact in facts:
        if fact.from_name:
            continue
        if _matches(fact, preference.attribute, preference.value):
            return Level(
                people=fact.supporting.people,
                creators=fact.supporting.creators,
                contested=fact.strength is Strength.CONTESTED,
            )
    return None


def _preference_fact(
    facts: list[AttributeFact], preference: Preference
) -> AttributeFact | None:
    if preference.attribute == "concept":
        found = _concept_evidence(facts, preference.value)
        return found[0] if found else None
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
        if len(weak) != len(candidates):
            return ""
        # "No declarable reason" is not "one person". Nine people in a
        # single creator's comment section fail the independence bar while
        # being nine people, and the note used to call that a single
        # observation — contradicting the result printed directly beneath
        # it, which honestly said "9 people across 1 channel".
        people = [
            max((x.people for x in c.reasons + c.caveats), default=0)
            for c in candidates
        ]
        if max(people, default=0) == 0:
            return (
                "Nothing below has evidence bearing on this request. These "
                "are bottles the corpus describes that nobody has ruled out."
            )
        if all(n <= 1 for n in people):
            return (
                "Every match below rests on a single person's remark. They "
                "are worth looking at, not worth trusting as consensus."
            )
        return (
            "No match below clears the independence bar — the evidence is "
            "there, but not from enough separate creators to call it "
            "consensus."
        )
    comparatives = [p for p in plan.soft if p.relative_to_anchor]
    if comparatives and not candidates:
        wanted = ", ".join(f"{p.value} than {plan.anchor}" for p in comparatives)
        return (
            f"Insufficient comparative evidence for {wanted}. The corpus "
            "would need people describing that quality in both bottles to "
            "compare them, and it does not have that. This request has not "
            "been answered as a different, easier one."
        )
    if plan.hard:
        wanted = ", ".join(f"{c.attribute}={c.value}" for c in plan.hard)
        return (
            f"No bottle in the corpus has evidence for {wanted}. "
            f"{len(grouped)} bottles were considered and {rejected} were "
            "ruled out by that requirement. This means nobody in these "
            "comments mentioned it — not that no such fragrance exists."
        )
    return "Nothing in the corpus speaks to this request."
