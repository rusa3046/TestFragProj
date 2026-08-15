"""Turning a sentence into something you can argue with.

    from fragrance_graph.plan import parse
    parse(conn, "I love Delina but the rose is too strong")

    QueryPlan(anchor='Parfums de Marly Delina',
              negative=[Preference(attribute='note', value='rose', ...)],
              ...)

The recommender never sees the sentence. It sees this, and this is a plain
data structure a person can read, diff and write a test against.

## Why the model does not choose fragrances

An LLM asked "what smells like Delina but less rosy" will answer, fluently,
from what it absorbed about perfume during training. That answer is not
evidence, it cannot be traced to anybody, and it is exactly the thing this
whole corpus exists to replace. So the division is strict:

> Language models translate the request. Evidence chooses the bottles.

The planner's only job is to work out *what was asked*. It may not know
which bottles exist beyond resolving an anchor name, it never scores, and
it never ranks. If it produces a plan nothing can satisfy, that is a
correct plan and the answer is "no bottle in the corpus matches".

## Why the deterministic parser comes first

Nothing here calls a model yet. A rule-based parser over a real vocabulary
handles the shapes people actually type — "like X but less Y", "crowd
pleasing raspberry", "something for a summer wedding" — and every one of
those is a test that runs in milliseconds for free.

A model layer belongs on top later, for the sentences rules cannot reach,
and it will emit *this same structure* so the recommender never learns
where a plan came from. Building the structure first is what makes that
substitution safe: the contract exists before anything fills it.

## Comparatives need an anchor to compare to

"Less rose" is meaningless alone — less than what? A comparative
preference therefore carries `relative_to_anchor`, and the planner refuses
to emit one without an anchor. That refusal is a feature: "something less
sweet" with no anchor is genuinely ambiguous and the honest response is to
say so rather than to guess a baseline.
"""

from __future__ import annotations

import re
import weakref
from dataclasses import dataclass, field
from enum import StrEnum

import psycopg

from fragrance_graph.evidence import SENTIMENT_AXES
from fragrance_graph.query import find_fragrance


class Intent(StrEnum):
    """What kind of answer the sentence is asking for."""

    #: "something like X", "a crowd-pleasing raspberry" — return bottles.
    RECOMMEND = "recommend"
    #: "what do people say about X" — return one bottle's evidence.
    PROFILE = "profile"
    #: "how is X different from Y" — return the evidence between two.
    COMPARE = "compare"
    #: "why did you recommend that", "how many people said so".
    EXPLAIN = "explain"


class Direction(StrEnum):
    """Which way a preference pushes."""

    HIGH = "high"
    LOW = "low"
    #: Compared against the anchor rather than against an absolute level.
    LESS_THAN_ANCHOR = "less_than_anchor"
    MORE_THAN_ANCHOR = "more_than_anchor"


@dataclass(frozen=True)
class Constraint:
    """Something a candidate must have to be considered at all.

    A hard constraint is a filter, not a score. "Raspberry" means bottles
    with no raspberry evidence are not worse candidates — they are not
    candidates. Getting this wrong is the difference between a
    recommendation and a list.
    """

    attribute: str
    value: str
    #: The words the user actually typed, kept so an explanation can quote
    #: the request back rather than paraphrasing it.
    said: str = ""


@dataclass(frozen=True)
class Preference:
    """Something that should push a candidate up or down the ranking."""

    attribute: str
    value: str
    direction: Direction
    said: str = ""

    @property
    def relative_to_anchor(self) -> bool:
        return self.direction in (
            Direction.LESS_THAN_ANCHOR,
            Direction.MORE_THAN_ANCHOR,
        )


@dataclass
class QueryPlan:
    """The whole request, in a form a test can assert on."""

    text: str
    intent: Intent = Intent.RECOMMEND
    #: Canonical name of the bottle being compared against, if any.
    anchor: str | None = None
    anchor_id: int | None = None
    #: A second bottle, for COMPARE.
    other: str | None = None
    other_id: int | None = None
    hard: list[Constraint] = field(default_factory=list)
    soft: list[Preference] = field(default_factory=list)
    #: Terms recognised as fragrance language but not mapped to any known
    #: attribute. These are what semantic retrieval is for; until that
    #: exists they are the honest record of what was not understood.
    unparsed: list[str] = field(default_factory=list)
    #: Set when the sentence asks something the plan cannot express.
    refusal: str = ""

    @property
    def usable(self) -> bool:
        return not self.refusal and bool(
            self.hard or self.soft or self.anchor or self.unparsed
        )

    def render(self) -> str:
        """The plan as a person would check it."""
        lines = [f'"{self.text}"', f"  intent    {self.intent}"]
        if self.anchor:
            lines.append(f"  anchor    {self.anchor}")
        if self.other:
            lines.append(f"  other     {self.other}")
        for c in self.hard:
            lines.append(f"  must      {c.attribute} = {c.value}")
        for p in self.soft:
            arrow = {
                Direction.HIGH: "prefer",
                Direction.LOW: "avoid",
                Direction.LESS_THAN_ANCHOR: "less than anchor",
                Direction.MORE_THAN_ANCHOR: "more than anchor",
            }[p.direction]
            lines.append(f"  {arrow:<9} {p.attribute} = {p.value}")
        for term in self.unparsed:
            lines.append(f"  unparsed  {term!r}")
        if self.refusal:
            lines.append(f"  REFUSED   {self.refusal}")
        return "\n".join(lines)


# --- the vocabulary ---------------------------------------------------------

#: Concepts the community expresses in words that share no substring.
#: "Crowd-pleasing" appears **zero** times in 9,495 comments; people write
#: "compliment", "everyone asks", "easy to like". A recommender that
#: string-matches the user's phrase against the corpus answers nothing, so
#: the planner maps the phrase to the concept and the concept carries the
#: words the corpus actually uses.
CONCEPTS: dict[str, tuple[str, ...]] = {
    "crowd pleasing": ("compliment", "compliments", "complimented", "crowd pleaser",
                       "easy to like", "people love", "everyone loves", "mass appeal",
                       "safe blind buy", "crowd pleasing"),
    "polarizing": ("polarizing", "love it or hate it", "divisive"),
    "expensive smelling": ("expensive", "luxurious", "smells expensive", "rich"),
    "cheap smelling": ("cheap", "smells cheap", "synthetic"),
}

#: Words that name a whole attribute rather than one of its values.
ATTRIBUTE_WORDS = {
    "longevity": "longevity",
    "lasting": "longevity",
    "lasts": "longevity",
    "projection": "projection",
    "sillage": "projection",
    "performance": "projection",
}

#: Phrases that set an attribute's direction outright.
PERFORMANCE_PHRASES = {
    "strong projection": ("projection", "strong", Direction.HIGH),
    "good projection": ("projection", "strong", Direction.HIGH),
    "beast mode": ("projection", "strong", Direction.HIGH),
    "loud": ("projection", "strong", Direction.HIGH),
    "quiet": ("projection", "weak", Direction.HIGH),
    "subtle": ("projection", "weak", Direction.HIGH),
    "skin scent": ("projection", "weak", Direction.HIGH),
    "long lasting": ("longevity", "long lasting", Direction.HIGH),
    "lasts all day": ("longevity", "long lasting", Direction.HIGH),
    "good longevity": ("longevity", "long lasting", Direction.HIGH),
}

#: Occasions and seasons. The corpus writes these plainly, so they need no
#: concept indirection — but they are listed rather than inferred so a
#: typo lands in `unparsed` instead of silently becoming a filter.
OCCASION_WORDS = (
    "wedding", "office", "work", "date night", "date", "club", "gym",
    "everyday", "daily", "formal", "casual", "summer", "winter", "spring",
    "autumn", "fall", "cold weather", "warm weather", "school", "night out",
)

#: Words for how a fragrance reads, as opposed to what it smells of.
VIBE_WORDS = (
    "feminine", "masculine", "unisex", "sexy", "clean", "cozy", "mysterious",
    "playful", "mature", "youthful", "dark", "airy", "elegant", "classy",
    "sophisticated", "fresh", "warm", "romantic", "professional", "fun",
    "innocent", "seductive", "refined", "old money", "clean girl",
)

#: Negation, in the shapes people write it.
NEGATIVE_PATTERNS = (
    r"\bbut (?:not|less|without|nothing) (?:too |so )?",
    r"\bwithout ",
    r"\bnot? (?:too|very|overly) ",
    # Plain "not X". Without this "not loud" read `loud` positively and
    # asked for the strongest projection in the corpus.
    r"\bnot ",
    r"\bdoes ?n[o']?t smell (?:like )?",
    r"\bthat is ?n[o']?t ",
    r"\bthat does ?n[o']?t ",
    r"\bisn[o']?t ",
    r"\bless ",
    r"\bavoid ",
    r"\bhate ",
    r"\bno ",
)

#: "too much X" is a complaint about the anchor, not a request for X.
TOO_MUCH = re.compile(
    # At most two words before "is too". An unbounded left edge captured
    # "delina but the rose" out of "I love Delina but the rose is too
    # strong", which then looked for bottles whose note is that sentence.
    r"(?:^|\b(?:but|and|though|however)\s+)?(?:the |its |it's |that )?"
    r"((?:[a-z]+ )?[a-z]+) is (?:too|way too) "
    r"(?:much|strong|overpowering|dominant|intense|loud|heavy)\b"
)

#: "like X but ..." / "similar to X but ..."
ANCHOR_PATTERNS = (
    re.compile(r"\b(?:something |anything )?(?:like|similar to|reminiscent of) "
               r"([a-z0-9'’\.\- ]{2,40}?)(?:\s+but\b|\s+that\b|\s*[,\.]|$)"),
    re.compile(r"\bi (?:really )?(?:love|like|enjoy|adore) "
               r"([a-z0-9'’\.\- ]{2,40}?)(?:\s+but\b|\s*[,\.]|$)"),
    re.compile(r"\b(?:fan of|into) ([a-z0-9'’\.\- ]{2,40}?)"
               r"(?:\s+but\b|\s*[,\.]|$)"),
)

COMPARE_PATTERN = re.compile(
    r"\b([a-z0-9'’\.\- ]{2,40}?) (?:vs\.?|versus|compared to|or) "
    r"([a-z0-9'’\.\- ]{2,40}?)(?:\s*[\?\.,]|$)"
)

PROFILE_PATTERNS = (
    re.compile(r"\bwhat do people (?:say|think) about ([a-z0-9'’\.\- ]{2,40})"),
    re.compile(r"\bwhat (?:do people |does anyone )?disagree about "
               r"(?:for |on |about )?([a-z0-9'’\.\- ]{2,40})"),
    re.compile(r"\btell me about ([a-z0-9'’\.\- ]{2,40})"),
    re.compile(r"\bprofile (?:of |for )?([a-z0-9'’\.\- ]{2,40})"),
)

EXPLAIN_PATTERNS = (
    r"\bwhy (?:did you |do you )?(?:recommend|suggest|pick)",
    r"\bhow (?:many|strong)",
    r"\bwho said",
    r"\bwhat evidence",
)


#: Why each kind of request could not be planned. Phrased as something a
#: person could act on rather than as an error code.
REFUSALS = {
    Intent.RECOMMEND: "nothing in this request maps to evidence the corpus holds",
    Intent.EXPLAIN: "an explanation needs a recommendation to explain; ask for "
                    "one first, or name the fragrance",
    Intent.PROFILE: "no fragrance in the catalogue matches that name",
    Intent.COMPARE: "both fragrances have to be in the catalogue to compare them",
}


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _negated_spans(text: str) -> list[tuple[int, int]]:
    """Where in the sentence negation is in force.

    A negation runs from its trigger to the next clause boundary. Crude, and
    deliberately so: the alternative is a parser nobody can predict, and a
    wrong span here shows up as a preference pointing the wrong way, which
    a test catches immediately.
    """
    # Boundary positions are found once for the whole string rather than
    # rescanned per negation. The old shape did five suffix scans for every
    # trigger, which is quadratic on input like "no no no no ...".
    boundaries = sorted(
        m.start()
        for pattern in (r",", r"\band\b", r"\bbut\b", r"\.", r";")
        for m in re.finditer(pattern, text)
    )
    spans = []
    for pattern in NEGATIVE_PATTERNS:
        for match in re.finditer(pattern, text):
            end = next(
                (b for b in boundaries if b >= match.end()), len(text)
            )
            spans.append((match.start(), end))
    return spans


def _is_negated(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def parse(
    conn: psycopg.Connection | None,
    text: str,
    *,
    notes: frozenset[str] = frozenset(),
) -> QueryPlan:
    """Read a request into a plan. Never touches the recommender.

    `conn` resolves anchor names against the catalogue and may be None, in
    which case anchors stay as the raw text the user typed. `notes` is the
    note vocabulary to recognise, passed in rather than fetched so the whole
    rule set is testable with no database at all.
    """
    cleaned = _clean(text)
    plan = QueryPlan(text=text)

    if any(re.search(p, cleaned) for p in EXPLAIN_PATTERNS):
        plan.intent = Intent.EXPLAIN

    negated = _negated_spans(cleaned)
    _read_intent_and_anchor(conn, cleaned, plan)
    consumed = _read_complaints(cleaned, plan)
    _read_concepts(cleaned, plan, negated)
    _read_performance(cleaned, plan, negated)
    _read_words(cleaned, plan, negated)
    _read_notes(cleaned, plan, negated, notes, consumed)

    if not plan.usable and not plan.refusal:
        # Every intent, not only RECOMMEND. An empty plan is not a plan
        # that matches nothing — downstream it matches *everything*, so
        # silence here is the dangerous default.
        plan.refusal = REFUSALS.get(
            plan.intent, "nothing in this request maps to evidence the corpus holds"
        )
    return plan


def _resolve(conn: psycopg.Connection | None, name: str) -> tuple[str | None, int | None]:
    name = name.strip(" ,.'\"")
    # The catalogue really does carry "as" as an alias of Angels' Share, so
    # "i like as, but less sweet" resolved to a bottle nobody named. An
    # anchor has to look like a name before it is worth resolving.
    if len(name) < 3 or name in STOPWORDS:
        return None, None
    if not name:
        return None, None
    if conn is None:
        return name, None
    row = find_fragrance(conn, name)
    if row is None:
        return None, None
    return row["canonical_name"], row["id"]


def _read_intent_and_anchor(
    conn: psycopg.Connection | None, text: str, plan: QueryPlan
) -> None:
    match = COMPARE_PATTERN.search(text)
    if match and plan.intent is not Intent.EXPLAIN:
        left, left_id = _resolve(conn, match.group(1))
        right, right_id = _resolve(conn, match.group(2))
        if left and right:
            plan.intent = Intent.COMPARE
            plan.anchor, plan.anchor_id = left, left_id
            plan.other, plan.other_id = right, right_id
            return

    for pattern in PROFILE_PATTERNS:
        found = pattern.search(text)
        if found:
            name, frag_id = _resolve(conn, found.group(1))
            if name:
                plan.intent = Intent.PROFILE
                plan.anchor, plan.anchor_id = name, frag_id
                return

    for pattern in ANCHOR_PATTERNS:
        found = pattern.search(text)
        if found:
            name, frag_id = _resolve(conn, found.group(1))
            if name:
                plan.anchor, plan.anchor_id = name, frag_id
                return
            # A name we cannot resolve is worth keeping: it is exactly the
            # signal the frontier subsystem grows the catalogue from.
            raw = found.group(1).strip(" ,.")
            if raw and raw not in plan.unparsed:
                plan.unparsed.append(raw)
            return


def _read_complaints(text: str, plan: QueryPlan) -> list[tuple[int, int]]:
    """"the rose is too strong" — a complaint about the anchor.

    This is the shape the whole product was named for and it inverts: the
    user is naming something they like about a bottle *in order to ask for
    less of it*. Read as a positive preference it returns the rosiest
    things in the corpus, which is the opposite of the request.
    """
    consumed: list[tuple[int, int]] = []
    for match in TOO_MUCH.finditer(text):
        # The whole phrase is spoken for. Without this, "the rose is too
        # strong" also yielded `note = strong`, asking for the intensity
        # word as if it were an ingredient.
        consumed.append((match.start(), match.end()))
        term = match.group(1).strip()
        term = re.sub(r"^(the|its|it's|that|but|and|though|however) ", "", term)
        attribute = "note"
        if term in SENTIMENT_AXES or term in ATTRIBUTE_WORDS:
            attribute = ATTRIBUTE_WORDS.get(term, term)
        # "Less rose" needs something to be less than. With an anchor the
        # preference is comparative; without one it still means "avoid
        # rose", which is a weaker but honest reading. Refusing outright
        # would throw away a request the user stated perfectly clearly.
        direction = (
            Direction.LESS_THAN_ANCHOR if plan.anchor else Direction.LOW
        )
        plan.soft.append(
            Preference(
                attribute=attribute,
                value=term,
                direction=direction,
                said=match.group(0),
            )
        )
    return consumed


def _add(plan: QueryPlan, pref: Preference | Constraint) -> None:
    existing = plan.soft if isinstance(pref, Preference) else plan.hard
    if not any(
        p.attribute == pref.attribute and p.value == pref.value for p in existing
    ):
        existing.append(pref)


def _read_concepts(text: str, plan: QueryPlan, negated: list) -> None:
    for concept in CONCEPTS:
        position = text.find(concept)
        if position == -1:
            # Also match the hyphenated spelling people actually type.
            position = text.find(concept.replace(" ", "-"))
        if position == -1:
            continue
        _add(
            plan,
            Preference(
                attribute="concept",
                value=concept,
                direction=Direction.LOW if _is_negated(position, negated)
                else Direction.HIGH,
                said=concept,
            ),
        )


def _read_performance(text: str, plan: QueryPlan, negated: list) -> None:
    for phrase, (attribute, value, direction) in PERFORMANCE_PHRASES.items():
        position = text.find(phrase)
        if position == -1:
            continue
        if _is_negated(position, negated):
            direction = Direction.LOW
        _add(plan, Preference(attribute, value, direction, said=phrase))


def _read_words(text: str, plan: QueryPlan, negated: list) -> None:
    for word in OCCASION_WORDS:
        position = re.search(rf"\b{re.escape(word)}\b", text)
        if position:
            _add(plan, Preference(
                "occasion", word,
                Direction.LOW if _is_negated(position.start(), negated)
                else Direction.HIGH,
                said=word,
            ))
    for word in VIBE_WORDS:
        position = re.search(rf"\b{re.escape(word)}\b", text)
        if position:
            _add(plan, Preference(
                "vibe", word,
                Direction.LOW if _is_negated(position.start(), negated)
                else Direction.HIGH,
                said=word,
            ))


#: Note words the corpus actually carries. Built from the claim vocabulary
#: rather than from a fixed ontology, because the corpus is the authority on
#: what people say and a hand-written note list would be a second, worse
#: one that drifts.
def note_vocabulary(conn: psycopg.Connection) -> frozenset[str]:
    rows = conn.execute(
        "SELECT lower(raw_object_text) AS tag FROM claims "
        " WHERE claim_type = 'NOTE_DESCRIPTOR' AND raw_object_text IS NOT NULL"
        "   AND polarity = 'ASSERTED'"
    ).fetchall()
    counts: dict[str, int] = {}
    for row in rows:
        for word in set(re.findall(r"[a-z]{4,}", row["tag"] or "")):
            if word not in STOPWORDS and word not in INTENSITY_WORDS:
                counts[word] = counts.get(word, 0) + 1
    from fragrance_graph.evidence import CANONICAL_NAME_NOTES

    # Union with the notes the catalogue itself names. "Raspberry" is a
    # note whether or not this corpus happens to carry two comments about
    # it, and the frequency floor exists to keep *noise* out, not to make
    # the system forget words it already knows.
    return frozenset(
        w for w, n in counts.items() if n >= MIN_NOTE_CLAIMS
    ) | CANONICAL_NAME_NOTES


#: Words that appear inside note tags but never *name* a note. Without
#: this, "a crowd-pleasing fragrance with raspberry" asks for bottles whose
#: notes include "fragrance" and "with", and nothing in the corpus matches
#: because nothing ever will.
STOPWORDS = frozenset({
    "with", "this", "that", "these", "those", "less", "more", "very", "really",
    "love", "like", "want", "need", "give", "something", "anything", "smell",
    "smells", "smelling", "fragrance", "fragrances", "perfume", "perfumes",
    "cologne", "scent", "scents", "note", "notes", "bottle", "please",
    "would", "could", "should", "there", "their", "about", "which", "what",
    "when", "where", "does", "doesn", "isn", "have", "been", "being", "much",
    "just", "also", "than", "then", "them", "they", "some", "good", "best",
    "better", "great", "nice", "well", "make", "makes", "made", "will",
    "from", "into", "over", "only", "same", "other", "another",
    "recommend", "suggest", "buy", "wear", "wearing", "still", "even",
    "people", "someone", "anyone", "everyone", "thing", "things", "kind",
    "type", "sort", "look", "looking", "find", "finding", "know", "think",
})

#: Words that appear all over note text but describe *how much*, not *what
#: of*. Admitting them turned "not loud" and "something heavy" into hard
#: note filters. Measured: `strong` appears in 6 note tags, `heavy` in 4.
INTENSITY_WORDS = frozenset({
    "strong", "stronger", "strongest", "weak", "weaker", "heavy", "heavier",
    "light", "lighter", "intense", "intensity", "loud", "louder", "soft",
    "softer", "subtle", "faint", "powerful", "mild", "overpowering",
    "dominant", "prominent", "overwhelming", "sharp", "smooth", "smoother",
    "sweeter", "fresher", "deeper", "richer",
})

#: A word has to name a note in more than one claim before the planner will
#: filter on it. One person writing "peons" is evidence about a bottle; it
#: is not a word anybody will ever ask for, and admitting it lets a typo in
#: the request become a hard constraint that silently returns nothing.
MIN_NOTE_CLAIMS = 2

#: Cached per connection *object*, not per `id()`. CPython reuses ids after
#: collection, so an id-keyed cache handed one connection's vocabulary to a
#: later, unrelated connection — which in tests meant one fixture's notes
#: leaking into the next.
_NOTE_CACHE: weakref.WeakKeyDictionary[psycopg.Connection, frozenset[str]] = (
    weakref.WeakKeyDictionary()
)


def _read_notes(
    text: str,
    plan: QueryPlan,
    negated: list,
    vocabulary: frozenset[str],
    consumed: list[tuple[int, int]] = (),
) -> None:
    """Notes named in the request.

    Deliberately last: a word already claimed as a vibe, occasion or
    performance term is not re-read as a note.
    """
    claimed = {p.value for p in plan.soft} | {c.value for c in plan.hard}
    # The anchor's own name is not a request for a note. "Babycat" must not
    # become `note = babycat`, and neither must any word of a resolved
    # canonical name.
    if plan.anchor:
        claimed |= set(re.findall(r"[a-z]{4,}", plan.anchor.lower()))
    for term in plan.unparsed:
        claimed |= set(re.findall(r"[a-z]{4,}", term.lower()))
    for match in re.finditer(r"[a-z]{4,}", text):
        word = match.group(0)
        if word in claimed or word in STOPWORDS or word not in vocabulary:
            continue
        if any(start <= match.start() < end for start, end in consumed):
            continue
        position = match.start()
        if _is_negated(position, negated):
            _add(plan, Preference("note", word, Direction.LOW, said=word))
        else:
            _add(plan, Constraint("note", word, said=word))


def parse_with_corpus(conn: psycopg.Connection, text: str) -> QueryPlan:
    """`parse`, with note words read from the corpus vocabulary.

    Cached per connection so parsing a thirty-query benchmark does not run
    thirty identical scans of the claim table.
    """
    vocabulary = _NOTE_CACHE.get(conn)
    if vocabulary is None:
        vocabulary = _NOTE_CACHE[conn] = note_vocabulary(conn)
    return parse(conn, text, notes=vocabulary)
