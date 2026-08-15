"""What the corpus supports about one bottle, and how strongly.

    from fragrance_graph.evidence import attribute_facts, Strength
    facts = attribute_facts(conn, fragrance_id=51)

The pair product asks "do enough people connect these two bottles". This
module asks the other question — "what does the corpus support about *this*
bottle" — and it cannot reuse the pair gate to answer it.

## Why a separate gate

The pair gate is binary because a page is binary: it exists or it does not.
An attribute is not. Measured on the corpus of 2026-08-15:

    SUPPORTED (3+ people, 2+ creators)     11
    REPEATED  (2+ people)                  15
    OBSERVED  (1 person)                  417        <- 94%

Run the pair gate over that and the recommender has five bottles to choose
between. Throw the gate away and the recommender states one stranger's
opinion as fact. Neither is the product.

So a fact here carries a **strength** instead of a verdict, and the caller
decides what each strength may be used for. The rule that makes this safe
is a division of labour, not a threshold:

> Weak evidence may bring a candidate into consideration.
> Only strong evidence may make a statement about it.

"Someone said this is rosy" is a fine reason to surface a bottle when
somebody asks for rose. It is not a fine thing to print as "this is rosy".

## Canonical facts are not community facts

`Strength.CANONICAL` means a curator or an authorized feed said it — a
brand, a release year, an official note list. It is *not* the top of the
community ladder and does not compete with it. "Officially contains
raspberry" and "eight people smell raspberry" are different claims about
different things, and a bottle can easily have the first without the
second. They are kept in separate rows so a page can say which it has.

## Contested is a result, not a failure

Denials are first-class. `polarity = 'DENIED'` on the same (bottle,
attribute, value) is *opposing* evidence, and a fact with meaningful
support on both sides comes back `CONTESTED` rather than being averaged
into a lie. "What do people disagree about for Delina" is a question the
product should answer, and this is where the answer comes from.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import IntEnum

import psycopg

from fragrance_graph.attributes import known_singulars, normalize_tag
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES

log = logging.getLogger("fragrance_graph.evidence")


class Strength(IntEnum):
    """How much a fact may be used for.

    Ordered so `>=` comparisons work, with one deliberate exception:
    `CANONICAL` sits outside the community ladder rather than above it. A
    caller that wants "at least REPEATED community support" must not be
    handed an official note list instead, so it is numbered below the
    ladder and selected explicitly.
    """

    #: Too little to use for anything, including retrieval.
    INSUFFICIENT = 0
    #: A curator or authorized feed stated it. Never a perception.
    CANONICAL = 1
    #: Exactly one person said it. Enough to retrieve on, never to declare.
    OBSERVED = 2
    #: More than one person, independently.
    REPEATED = 3
    #: Meaningful evidence on both sides. Ranked *below* SUPPORTED because a
    #: contested fact must never be stated flatly, but it is richer than
    #: REPEATED and pages should surface it rather than hide it.
    CONTESTED = 4
    #: Clears the same bar a published pair page clears.
    SUPPORTED = 5

    @property
    def may_declare(self) -> bool:
        """Whether a page may state this as a fact in its own voice."""
        return self in (Strength.CANONICAL, Strength.SUPPORTED)

    @property
    def may_retrieve(self) -> bool:
        """Whether this may bring a candidate into consideration."""
        return self is not Strength.INSUFFICIENT


#: Opposing evidence has to be more than a rounding error before a fact is
#: called contested — one dissenter among nine is a caveat, not a dispute.
#: Deliberately a ratio and a floor together: two people out of three is a
#: real disagreement, two out of thirty is not.
CONTESTED_MIN_PEOPLE = 2
CONTESTED_MIN_RATIO = 0.25


class Attribution(IntEnum):
    """Which attributions of a claim to a bottle are allowed to count.

    The corpus holds two kinds. A **stated** attribution is the commenter
    naming the bottle, resolved by `resolve.entities`. An **inferred** one
    is a machine deciding that "this" meant the bottle the video was about;
    it is wrong about one time in twenty, and it lives in
    `claim_attributions` precisely so this choice exists.

    Retrieval can afford inference. A declarative sentence cannot.
    """

    #: Only what a commenter actually named. The conservative corpus.
    STATED = 0
    #: Plus inferences a person or an evaluation has accepted.
    ACCEPTED = 1
    #: Plus inferences nothing has checked yet. Retrieval and measurement
    #: only — never wire this to something that prints sentences.
    PROPOSED = 2


#: Claim types whose object text is the value ("rose", "date night").
TAGGED_TYPES = ("NOTE_DESCRIPTOR", "AESTHETIC", "OCCASION")

#: Claim types with no object: the sentiment *is* the value. These are the
#: types that aggregate well, and the reason is worth stating because it
#: drives the whole design. Longevity is not better observed than smell; it
#: has three possible answers where notes have six hundred. Agreement is
#: governed by how many strings an answer can be.
SENTIMENT_TYPES = ("LONGEVITY", "PROJECTION", "DEVELOPMENT", "REFORMULATION")

#: The attribute name each claim type contributes to.
ATTRIBUTE_OF = {
    "NOTE_DESCRIPTOR": "note",
    "AESTHETIC": "vibe",
    "OCCASION": "occasion",
    "LONGEVITY": "longevity",
    "PROJECTION": "projection",
    "DEVELOPMENT": "development",
    "REFORMULATION": "reformulation",
}

#: Attributes that are one axis with two ends rather than an open set of
#: values. Both ends aggregate into a single fact so that disagreement
#: shows up as disagreement instead of as two facts nobody compares.
SENTIMENT_AXES = frozenset(
    ATTRIBUTE_OF[t] for t in SENTIMENT_TYPES if t in ATTRIBUTE_OF
)

#: Sentiment to a value word, for the types that carry no object text.
#: "longevity = strong" reads as a fact; "longevity = POSITIVE" reads as a
#: database column, and these strings reach a page.
SENTIMENT_VALUE = {
    "longevity": {"POSITIVE": "long lasting", "NEGATIVE": "short lived"},
    "projection": {"POSITIVE": "strong", "NEGATIVE": "weak"},
    "development": {"POSITIVE": "develops well", "NEGATIVE": "develops poorly"},
    "reformulation": {"POSITIVE": "reformulated well", "NEGATIVE": "reformulated badly"},
}

#: An axis is always *named* by its positive end, because "for" and
#: "against" are defined relative to that end. Naming it by whichever end
#: more people asserted produces labels that contradict their own counts —
#: "short lived, 1 for and 3 against" was the first draft's output, which
#: reads as the opposite of what the corpus says.
POSITIVE_END = {
    attribute: values["POSITIVE"] for attribute, values in SENTIMENT_VALUE.items()
}


@dataclass(frozen=True)
class Support:
    """The independent humans behind one side of a fact."""

    people: int = 0
    creators: int = 0
    videos: int = 0
    claim_ids: tuple[int, ...] = ()

    @property
    def clears_gate(self) -> bool:
        return self.people >= MIN_COMMENTERS and self.creators >= MIN_SOURCES


@dataclass(frozen=True)
class AttributeFact:
    """One thing the corpus supports about one bottle.

    `supporting` and `opposing` are kept whole rather than netted off. A
    fact with 6 for and 4 against is not a fact with 2 for, and the product
    has to be able to say which it is.
    """

    fragrance_id: int
    attribute: str
    value: str
    supporting: Support
    opposing: Support = field(default_factory=Support)
    #: True when any contributing claim was attributed by inference rather
    #: than by the commenter naming the bottle. Travels with the fact so a
    #: renderer can never lose track of it.
    inferred: bool = False

    @property
    def strength(self) -> Strength:
        return strength_of(self.supporting, self.opposing)

    @property
    def people(self) -> int:
        return self.supporting.people

    def __str__(self) -> str:
        s = f"{self.attribute}={self.value} [{self.strength.name}]"
        if self.opposing.people:
            s += f" ({self.supporting.people} for, {self.opposing.people} against)"
        else:
            s += f" ({self.supporting.people} people)"
        return s + (" *inferred" if self.inferred else "")


def strength_of(supporting: Support, opposing: Support) -> Strength:
    """Grade one fact from its two sides.

    Contested is checked before support so a disputed fact never comes back
    as SUPPORTED — the whole point is that it must not be stated flatly.
    """
    if supporting.people == 0:
        return Strength.INSUFFICIENT
    total = supporting.people + opposing.people
    if opposing.people >= CONTESTED_MIN_PEOPLE and (
        opposing.people / total >= CONTESTED_MIN_RATIO
    ):
        return Strength.CONTESTED
    if supporting.clears_gate:
        return Strength.SUPPORTED
    if supporting.people >= 2:
        return Strength.REPEATED
    return Strength.OBSERVED


# The attributed-claim view. `stated` and `inferred` are separate columns
# rather than one COALESCE so a caller can tell which it got, and so the
# `Attribution` policy is a WHERE clause rather than a second query.
ATTRIBUTED_SQL = """
SELECT cl.id                AS claim_id,
       cl.claim_type        AS claim_type,
       cl.raw_object_text   AS raw_value,
       cl.sentiment         AS sentiment,
       cl.polarity          AS polarity,
       cl.subject_frag_id   AS stated_frag_id,
       att.fragrance_id     AS inferred_frag_id,
       att.review_status    AS inferred_status,
       COALESCE(NULLIF(c.author_id, ''), 'author:unknown') AS person,
       c.source_channel     AS creator,
       c.video_id           AS video_id
  FROM claims cl
  JOIN comments c ON c.id = cl.comment_id
  LEFT JOIN claim_attributions att
         ON att.claim_id = cl.id
        AND att.role = 'subject'
        AND att.review_status <> 'rejected'
 WHERE cl.evidence_verified = 1
   AND cl.claim_type = ANY(%(types)s)
"""


def _value_of(row: dict, plurals: frozenset[str]) -> str:
    """The normalized value this claim asserts, or '' if it asserts none."""
    attribute = ATTRIBUTE_OF.get(row["claim_type"], "")
    if row["claim_type"] in SENTIMENT_TYPES:
        return SENTIMENT_VALUE.get(attribute, {}).get(row["sentiment"], "")
    return normalize_tag(row["raw_value"], plurals=plurals)


def attribute_facts(
    conn: psycopg.Connection,
    *,
    fragrance_id: int | None = None,
    attribution: Attribution = Attribution.STATED,
) -> list[AttributeFact]:
    """Aggregate every attribute claim into graded facts.

    One pass over the claims rather than a query per fact: at this corpus
    size the whole attributed set is a few thousand rows, and the grouping
    is cheaper in Python than the same thing expressed as SQL nobody can
    read.
    """
    types = list(TAGGED_TYPES) + list(SENTIMENT_TYPES)
    rows = conn.execute(ATTRIBUTED_SQL, {"types": types}).fetchall()

    # Plurals are learned from the whole corpus, not the filtered subset, so
    # asking about one bottle normalizes exactly as asking about all of them.
    plurals = known_singulars(
        [(r["raw_value"] or "").lower().strip() for r in rows]
    )

    # (frag, attribute, value) -> side -> sets
    buckets: dict[tuple[int, str, str], dict] = {}
    for row in rows:
        frag = _attributed_to(row, attribution)
        if frag is None or (fragrance_id is not None and frag != fragrance_id):
            continue
        attribute = ATTRIBUTE_OF.get(row["claim_type"], "")
        value = _value_of(row, plurals)
        if not attribute or not value:
            continue
        # A sentiment attribute has one axis, not two independent values.
        # Grouped by value, "3 people say it fades" and "1 says it lasts"
        # come back as two unrelated OBSERVED facts and the disagreement
        # disappears — which is the disagreement a buyer most wants to see.
        # Grouped by attribute, the minority becomes opposing evidence and
        # the fact can come back CONTESTED. Denials still oppose on both
        # kinds; this adds the axis that sentiment types also have.
        key = (frag, attribute, "" if attribute in SENTIMENT_AXES else value)
        bucket = buckets.setdefault(
            key,
            {
                "for": {"people": set(), "creators": set(), "videos": set(), "ids": []},
                "against": {"people": set(), "creators": set(), "videos": set(), "ids": []},
                "inferred": False,
            },
        )
        # Two ways to oppose: denying the claim outright, or asserting the
        # other end of a sentiment axis.
        opposes = row["polarity"] != "ASSERTED"
        if attribute in SENTIMENT_AXES and row["sentiment"] == "NEGATIVE":
            opposes = not opposes
        side = bucket["against" if opposes else "for"]
        side["people"].add(row["person"])
        side["creators"].add(row["creator"])
        if row["video_id"]:
            side["videos"].add(row["video_id"])
        side["ids"].append(row["claim_id"])
        if row["stated_frag_id"] is None:
            bucket["inferred"] = True

    facts = [
        AttributeFact(
            fragrance_id=frag,
            attribute=attribute,
            value=value or POSITIVE_END[attribute],
            supporting=_support(bucket["for"]),
            opposing=_support(bucket["against"]),
            inferred=bucket["inferred"],
        )
        for (frag, attribute, value), bucket in buckets.items()
    ]
    facts.sort(key=lambda f: (-f.strength, -f.people, f.attribute, f.value))
    return facts


def _attributed_to(row: dict, attribution: Attribution) -> int | None:
    """Which bottle this claim counts for under the chosen policy.

    A stated attribution always wins over an inferred one. The commenter
    naming a bottle is better evidence than a machine reading a title, and
    where both exist and disagree, the person is right by definition.
    """
    if row["stated_frag_id"] is not None:
        return row["stated_frag_id"]
    if attribution is Attribution.STATED or row["inferred_frag_id"] is None:
        return None
    if attribution is Attribution.ACCEPTED and row["inferred_status"] != "accepted":
        return None
    return row["inferred_frag_id"]


def _support(side: dict) -> Support:
    return Support(
        people=len(side["people"]),
        creators=len(side["creators"]),
        videos=len(side["videos"]),
        claim_ids=tuple(sorted(side["ids"])),
    )


def coverage(
    conn: psycopg.Connection, *, attribution: Attribution = Attribution.STATED
) -> dict[str, int]:
    """How much of the catalogue the evidence layer can actually speak for.

    The number that matters for the recommendation product is not how many
    facts exist but how many *bottles* have one worth stating.
    """
    facts = attribute_facts(conn, attribution=attribution)
    by_strength: dict[str, int] = {s.name: 0 for s in Strength}
    for fact in facts:
        by_strength[fact.strength.name] += 1
    catalogue = conn.execute("SELECT count(*) FROM fragrances").fetchone()[0]
    return {
        "catalogue": catalogue,
        "facts": len(facts),
        **by_strength,
        "fragrances_with_any_fact": len({f.fragrance_id for f in facts}),
        "fragrances_declarable": len(
            {f.fragrance_id for f in facts if f.strength.may_declare}
        ),
    }
