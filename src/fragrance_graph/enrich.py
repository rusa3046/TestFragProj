"""Two jobs that both buy comments, and are not the same job.

    from fragrance_graph.enrich import learn_about, verify_comparison

The experiment on 2026-08-15 spent $1.09 enriching nine bottles and
produced one page, and reading the trial rows showed why: pouring comments
onto a bottle scatters. Delina went from 9 mentions to 20, spread across
17 different partner bottles, 14 of them at exactly one person. Measured
as *pages*, that run mostly failed. Measured as *what the corpus now
knows*, it was one of the better runs so far.

So there are two jobs here with two success metrics, and the whole point
is that they are not interchangeable:

**Learn about a fragrance.** Search broadly, take whatever people say —
notes, vibe, performance, occasion, complaints, comparisons — and count
the useful attributable evidence that did not exist before. A run that
produces no page and forty new attribute facts has succeeded.

**Verify a comparison.** Search the pair specifically and move one
relationship from insufficient toward supported or contradicted. A run
that produces forty attribute facts and leaves the pair at two people has
failed at the job it was given.

Optimising the first with the second's metric is what produced the "1 page
from 9 bottles" result and the conclusion that enrichment does not work.
Enrichment works; it was being scored against the wrong thing.

## What is deliberately shared

Both jobs use the same ceilings, the same creator-diversity rule and the
same budget guard, because those protect the operator rather than serving
either objective. `frontier.Ceiling` already encodes them and is reused
rather than reimplemented — a second set of limits is a second set to get
wrong, and the cap has been defeated twice already.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from fragrance_graph.evidence import Attribution, attribute_facts
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.query import pair_stats

log = logging.getLogger("fragrance_graph.enrich")


class Mode:
    """The two jobs, named so a scheduler can ask for one."""

    LEARN = "learn-about-fragrance"
    VERIFY = "verify-comparison"


@dataclass
class InformationGain:
    """What a learn-about run actually added.

    Counted in the units the recommendation product consumes, not in
    pages. `new_declarable` is the headline — facts that crossed from
    "somebody mentioned it" to "several independent people agree" — but a
    new descriptor nobody else has said is genuine gain too: it is a
    bottle becoming findable by a word that previously matched nothing.
    """

    fragrance_id: int
    name: str = ""
    new_facts: int = 0
    new_declarable: int = 0
    new_contested: int = 0
    new_descriptors: int = 0
    #: Facts that exist only because a machine decided which bottle a
    #: comment was about. Reported apart from `new_facts` so a run cannot
    #: advertise knowledge nobody has checked.
    new_inferred_facts: int = 0
    #: Distinct creators whose comments contributed, before and after.
    creators_before: int = 0
    creators_after: int = 0
    comments_read: int = 0
    usd: float = 0.0

    @property
    def succeeded(self) -> bool:
        """Any new attributable knowledge is a success for this job.

        Deliberately generous, and deliberately not `new_declarable > 0`:
        holding a learn run to the consensus bar is exactly the mistake
        that made bottle enrichment look worthless.
        """
        return self.new_facts > 0 or self.new_descriptors > 0

    @property
    def usd_per_fact(self) -> float:
        return self.usd / self.new_facts if self.new_facts else 0.0

    def __str__(self) -> str:
        return (
            f"{self.name}: +{self.new_facts} facts "
            f"(+{self.new_declarable} declarable, +{self.new_contested} "
            f"contested), +{self.new_descriptors} new descriptors, "
            f"+{self.new_inferred_facts} inferred-only, "
            f"creators {self.creators_before}->{self.creators_after}, "
            f"{self.comments_read} comments, ${self.usd:.4f}"
        )


@dataclass
class PairOutcome:
    """What a verify-comparison run settled, if anything."""

    a_id: int
    b_id: int
    label: str = ""
    people_before: int = 0
    people_after: int = 0
    creators_before: int = 0
    creators_after: int = 0
    comments_read: int = 0
    usd: float = 0.0

    @property
    def verdict(self) -> str:
        """Three outcomes, and "still insufficient" is one of them.

        A pair that stayed at two people after a targeted search is not a
        failed run — it is a finding. It means the comparison people keep
        repeating in one place is not repeated elsewhere, which is exactly
        what the gate exists to detect.
        """
        if self.people_after >= MIN_COMMENTERS and self.creators_after >= MIN_SOURCES:
            return "supported"
        if self.people_after > self.people_before:
            return "still insufficient, but closer"
        return "still insufficient"

    @property
    def succeeded(self) -> bool:
        return self.verdict == "supported"

    def __str__(self) -> str:
        return (
            f"{self.label}: {self.people_before}->{self.people_after} people, "
            f"{self.creators_before}->{self.creators_after} creators — "
            f"{self.verdict} (${self.usd:.4f})"
        )


def _snapshot(conn: psycopg.Connection, fragrance_id: int) -> dict:
    """The evidence layer as it stands, split by what counts as knowledge.

    Two exclusions from the headline. A fact with only *opposing* evidence
    is a record that people denied something — worth keeping, and not
    "new attributable knowledge" about what the bottle is like. And a fact
    resting entirely on unreviewed machine attribution is knowledge about a
    claim, not yet about a bottle; it is counted separately so a learn run
    cannot advertise gain that nobody has checked.
    """
    facts = attribute_facts(
        conn, fragrance_id=fragrance_id, attribution=Attribution.PROPOSED
    )
    asserted = [f for f in facts if f.supporting.people > 0]
    return {
        "keys": {(f.attribute, f.value) for f in asserted if not f.inferred},
        "inferred_keys": {
            (f.attribute, f.value) for f in asserted if f.inferred
        },
        "declarable": sum(1 for f in facts if f.may_declare),
        "contested": sum(1 for f in facts if f.opposing.people),
        "creators": max((f.supporting.creators for f in facts), default=0),
    }


def measure_learning(
    conn: psycopg.Connection,
    fragrance_id: int,
    before: dict,
    *,
    name: str = "",
    comments_read: int = 0,
    usd: float = 0.0,
) -> InformationGain:
    """Diff the evidence layer around a run. Reads only; spends nothing.

    Deliberately measured *from the evidence layer* rather than from claim
    counts. Claims are what was bought; facts are what became usable, and
    the gap between them is where a run's real value lives — 200 claims
    that all repeat one person's opinion are worth less than 12 that reach
    three separate people.
    """
    after = _snapshot(conn, fragrance_id)
    fresh = after["keys"] - before["keys"]
    fresh_inferred = after["inferred_keys"] - before["inferred_keys"]
    return InformationGain(
        fragrance_id=fragrance_id,
        name=name,
        new_facts=len(fresh),
        new_declarable=max(0, after["declarable"] - before["declarable"]),
        new_contested=max(0, after["contested"] - before["contested"]),
        new_descriptors=len({value for _, value in fresh}),
        new_inferred_facts=len(fresh_inferred),
        creators_before=before["creators"],
        creators_after=after["creators"],
        comments_read=comments_read,
        usd=usd,
    )


def learn_about(
    conn: psycopg.Connection,
    fragrance_id: int,
    *,
    name: str,
    run,
) -> InformationGain:
    """Buy comments about one bottle and report what the corpus learned.

    `run` does the buying — it is `frontier.enrich_one` in production and a
    fake in tests — and returns `(comments_read, usd)`. Injected so this
    module can be exercised without a key, a quota or a cent, and so the
    measurement is testable independently of the thing being measured.

    The search is deliberately broad: `"<name> fragrance review"`, not
    `"<name> dupe"`. Asking for dupes returns comparisons, which deepens
    the pair graph and teaches nothing about what the bottle smells like —
    the corpus is already dupe-shaped and this job exists to widen it.
    """
    before = _snapshot(conn, fragrance_id)
    comments_read, usd = run(query=f"{name} fragrance review")
    gain = measure_learning(
        conn, fragrance_id, before, name=name,
        comments_read=comments_read, usd=usd,
    )
    log.info("%s", gain)
    return gain


def verify_comparison(
    conn: psycopg.Connection,
    a_id: int,
    b_id: int,
    *,
    a_name: str,
    b_name: str,
    run,
) -> PairOutcome:
    """Buy comments about one *pair* and report whether it settled.

    The search names both bottles, which is the whole difference from
    `learn_about`. A broad search on one of them scatters its evidence
    across every bottle anybody compares it to; this one is aimed at a
    single edge, and its success metric is that edge moving.
    """
    before = pair_stats(conn, a_id, b_id)
    comments_read, usd = run(query=f"{a_name} vs {b_name}")
    after = pair_stats(conn, a_id, b_id)
    outcome = PairOutcome(
        a_id=a_id,
        b_id=b_id,
        label=f"{a_name} vs {b_name}",
        people_before=before.commenters,
        people_after=after.commenters,
        creators_before=before.creators,
        creators_after=after.creators,
        comments_read=comments_read,
        usd=usd,
    )
    log.info("%s", outcome)
    return outcome


@dataclass
class NearMiss:
    """A pair one or two people short of publishing."""

    a_id: int
    b_id: int
    a_name: str
    b_name: str
    people: int
    creators: int

    @property
    def needs(self) -> str:
        wants = []
        if self.people < MIN_COMMENTERS:
            wants.append(f"{MIN_COMMENTERS - self.people} more person(s)")
        if self.creators < MIN_SOURCES:
            wants.append(f"{MIN_SOURCES - self.creators} more creator(s)")
        return " and ".join(wants) or "nothing"


NEAR_MISS_SQL = """
SELECT LEAST(c.subject_frag_id, c.object_frag_id)    AS a_id,
       GREATEST(c.subject_frag_id, c.object_frag_id) AS b_id,
       count(DISTINCT COALESCE(NULLIF(co.author_id, ''), 'author:unknown'))
           AS people,
       count(DISTINCT NULLIF(co.source_channel, ''))  AS creators
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 WHERE c.claim_type IN ('SIMILAR_TO', 'DUPE_OF', 'BETTER_THAN')
   AND c.polarity = 'ASSERTED'
   AND c.evidence_verified = 1
   AND c.subject_frag_id IS NOT NULL
   AND c.object_frag_id IS NOT NULL
   AND c.subject_frag_id <> c.object_frag_id
 GROUP BY 1, 2
HAVING count(DISTINCT COALESCE(NULLIF(co.author_id, ''), 'author:unknown'))
           >= %(low)s
   AND (
         -- Short on people, or short on creators, or both. Bounding only
         -- the head count hid the most targetable case of all: three
         -- people agreeing inside one creator's comment section, which
         -- needs exactly one more channel to publish and was invisible.
         count(DISTINCT COALESCE(NULLIF(co.author_id, ''), 'author:unknown'))
             < %(min_people)s
      OR count(DISTINCT NULLIF(co.source_channel, '')) < %(min_creators)s
       )
"""


def near_misses(
    conn: psycopg.Connection, *, low: int = 2
) -> list[NearMiss]:
    """Pairs close enough to the gate that one targeted search might settle
    them — the natural work list for `verify_comparison`.

    A pair can fall short on *either* axis. Three people agreeing inside
    one creator's comment section is as much a near miss as two people
    across two channels, and it is the cheaper of the two to fix: it needs
    one new channel rather than one new person.

    Ordered by how close they are, because the cheapest thing to buy is
    the pair that needs one more of something rather than three.
    """
    names = {
        row["id"]: row["canonical_name"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    found = [
        NearMiss(
            a_id=row["a_id"],
            b_id=row["b_id"],
            a_name=names.get(row["a_id"], "?"),
            b_name=names.get(row["b_id"], "?"),
            people=row["people"],
            creators=row["creators"],
        )
        for row in conn.execute(
            NEAR_MISS_SQL,
            {
                "low": low,
                "min_people": MIN_COMMENTERS,
                "min_creators": MIN_SOURCES,
            },
        )
    ]
    found.sort(key=lambda m: (-m.people, -m.creators, m.a_name))
    return found


@dataclass
class ModeReport:
    """Side-by-side outcomes, so the two metrics stay visibly different."""

    learned: list[InformationGain] = field(default_factory=list)
    verified: list[PairOutcome] = field(default_factory=list)

    def render(self) -> str:
        lines = []
        if self.learned:
            lines += ["learn-about-fragrance:"]
            lines += [f"  {gain}" for gain in self.learned]
            wins = sum(1 for g in self.learned if g.succeeded)
            spent = sum(g.usd for g in self.learned)
            facts = sum(g.new_facts for g in self.learned)
            lines.append(
                f"  -> {wins}/{len(self.learned)} added knowledge; "
                f"{facts} new facts for ${spent:.4f}"
            )
        if self.verified:
            lines += ["", "verify-comparison:"]
            lines += [f"  {outcome}" for outcome in self.verified]
            wins = sum(1 for o in self.verified if o.succeeded)
            spent = sum(o.usd for o in self.verified)
            lines.append(
                f"  -> {wins}/{len(self.verified)} pairs settled "
                f"for ${spent:.4f}"
            )
        return "\n".join(lines) or "nothing ran"


def _unused(client: Any) -> None:  # pragma: no cover - documentation only
    """Both jobs take their spending callable by injection.

    There is deliberately no module-level Anthropic or YouTube client here.
    Every paid path in this system goes through `budget.guard` via
    `frontier.budgeted_extractor`, and a module that could build its own
    client is a module that could route around the cap — which has already
    happened twice.
    """
