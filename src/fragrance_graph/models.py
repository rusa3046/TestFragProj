"""Pydantic schemas for LLM-extracted fragrance claims.

The taxonomy here is v2, revised after running v1 over a real r/fragrance
sample. Every change below is a response to an observed defect, recorded
so the reasoning survives:

- `LONGEVITY_COMPLAINT` absorbed anything about something going away:
  a fragrance that "never really develops", a reformulation where a note
  "has disappeared", and a "low projection" remark all landed there.
  Split into LONGEVITY / PROJECTION / DEVELOPMENT / REFORMULATION.
- The type name presumed a complaint, so "still lingering on my clothes,
  delightful" was stored as a complaint at 0.3 confidence. Polarity is now
  a separate `sentiment` field and the type names are neutral.
- `REMINDS_ME_OF` never fired. The one clear case in the sample — "I got a
  bit of a Serge Lutens vibe" — was labelled SIMILAR_TO on every run.
  Merged into SIMILAR_TO rather than kept as a distinction the extractor
  cannot make.
- `object_kind` was derived from `claim_type`, which forced "Serge Lutens"
  (a house) to be recorded as a fragrance. It is now stated per claim and
  validated against the kinds each type permits.
- Subjects were sometimes categories ("skin scents") or several fragrances
  in one string. `subject_kind` marks the first so entity resolution can
  skip it; the prompt forbids the second.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ClaimType(StrEnum):
    # Comparisons between fragrances — the graph is built from these.
    SIMILAR_TO = "SIMILAR_TO"
    DUPE_OF = "DUPE_OF"
    BETTER_THAN = "BETTER_THAN"

    # Descriptions of a single fragrance.
    NOTE_DESCRIPTOR = "NOTE_DESCRIPTOR"
    OCCASION = "OCCASION"
    AESTHETIC = "AESTHETIC"

    # Performance, each with its own sentiment.
    LONGEVITY = "LONGEVITY"
    PROJECTION = "PROJECTION"
    DEVELOPMENT = "DEVELOPMENT"

    # Product-level observations.
    REFORMULATION = "REFORMULATION"
    UNMET_PRODUCT_REQUEST = "UNMET_PRODUCT_REQUEST"


class SubjectKind(StrEnum):
    """What the claim is about."""

    FRAGRANCE = "FRAGRANCE"
    HOUSE = "HOUSE"
    #: A class of fragrances rather than a specific one — "skin scents",
    #: "80s perfumes". Real signal, but not resolvable to a bottle.
    CATEGORY = "CATEGORY"


class ObjectKind(StrEnum):
    """What the claim points at, if anything."""

    FRAGRANCE = "FRAGRANCE"
    HOUSE = "HOUSE"
    #: Any non-entity object: a note, accord, occasion, vibe, product form,
    #: or comparison to something that isn't a fragrance ("a bakery").
    TAG = "TAG"
    NONE = "NONE"


class Sentiment(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"


class Polarity(StrEnum):
    """Whether the commenter says the relationship holds, or says it does not.

    Added after measuring that 36 of 499 similarity claims — roughly one
    edge in thirteen — were *denials* stored as assertions. "I just try
    latafa it is nothing like angel share" was recorded as a DUPE_OF edge,
    so a page would have quoted that person as evidence for the very claim
    they were rejecting.

    This is deliberately NOT folded into `sentiment`, which was the first
    instinct and is wrong for the same reason `LONGEVITY_COMPLAINT` was
    wrong in v1: it collapses two independent facts into one field. All 36
    denials were NEGATIVE, but so were five genuine edges — "worst dupe of
    (540)" asserts the dupe and dislikes it. Sentiment says how the
    commenter feels; polarity says whether they are claiming the thing at
    all.

    A denial is real information — "nine people say Khamrah is nothing like
    Angels' Share" is worth knowing — so denials are stored, not dropped.
    They are simply never counted as edges.
    """

    ASSERTED = "ASSERTED"
    DENIED = "DENIED"


#: Which object kinds each claim type may carry. A type mapped to {NONE}
#: takes no object at all.
ALLOWED_OBJECT_KINDS: dict[ClaimType, set[ObjectKind]] = {
    # Comparisons reach beyond fragrances in practice: people compare to a
    # house ("a Serge Lutens vibe") and to raw materials ("cocoa pyrazine").
    ClaimType.SIMILAR_TO: {ObjectKind.FRAGRANCE, ObjectKind.HOUSE, ObjectKind.TAG},
    ClaimType.DUPE_OF: {ObjectKind.FRAGRANCE},
    ClaimType.BETTER_THAN: {ObjectKind.FRAGRANCE},
    ClaimType.NOTE_DESCRIPTOR: {ObjectKind.TAG},
    ClaimType.OCCASION: {ObjectKind.TAG},
    ClaimType.AESTHETIC: {ObjectKind.TAG},
    ClaimType.UNMET_PRODUCT_REQUEST: {ObjectKind.TAG},
    ClaimType.LONGEVITY: {ObjectKind.NONE},
    ClaimType.PROJECTION: {ObjectKind.NONE},
    ClaimType.DEVELOPMENT: {ObjectKind.NONE},
    ClaimType.REFORMULATION: {ObjectKind.NONE},
}

#: Types whose object is another fragrance — the edges the graph is built
#: from. Kept as a set so downstream queries need not restate the mapping.
EDGE_CLAIM_TYPES = frozenset(
    {ClaimType.SIMILAR_TO, ClaimType.DUPE_OF, ClaimType.BETTER_THAN}
)

#: The language fragrance discussion uses to mean "a cheaper stand-in",
#: which is what DUPE_OF records.
#:
#: The first eval measured a one-directional confusion, three times out of
#: three: the human labelled DUPE_OF where both the extractor and the Opus
#: drafter said SIMILAR_TO, and never the reverse. The cause is the old
#: definition — "a cheaper substitute" — which made both models look for a
#: price signal. Commenters never state the price gap; it is carried by
#: the brands, and these words are how they say it instead.
#:
#: Lives here, not in either prompt, because the extractor and the label
#: drafter must encode the same boundary. A test asserts both prompts
#: carry every word, so the two cannot drift apart silently.
DUPE_SIGNAL_WORDS = (
    "dupe",
    "clone",
    "impression of",
    "interpretation of",
    "inspired by",
)


#: Typographic characters folded to their ASCII equivalents before
#: comparing quoted evidence to a comment body.
#:
#: Phone keyboards substitute curly quotes automatically, so a YouTube
#: comment says "don't" with U+2019 while the model quotes it back with an
#: ASCII apostrophe. The quote is verbatim; only the glyph differs. Without
#: this fold, that claim is recorded as a paraphrase and its evidence is
#: treated as unauditable — which silently inflates the measured paraphrase
#: rate and, worse, would drop real evidence from the product surface.
#:
#: Deliberately limited to punctuation that has a genuine ASCII twin.
#: Accents are NOT stripped: "Wulóng Chá" and "Wulong Cha" are different
#: strings and evidence should not match across them.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "‘": "'",  # left single quote
        "’": "'",  # right single quote / apostrophe
        "‚": "'",  # single low quote
        "‛": "'",  # single high-reversed quote
        "′": "'",  # prime
        "“": '"',  # left double quote
        "”": '"',  # right double quote
        "„": '"',  # double low quote
        "‟": '"',  # double high-reversed quote
        "″": '"',  # double prime
        "‐": "-",  # hyphen
        "‑": "-",  # non-breaking hyphen
        "‒": "-",  # figure dash
        "–": "-",  # en dash
        "—": "-",  # em dash
        "―": "-",  # horizontal bar
        "−": "-",  # minus sign
    }
)


def author_from_payload(payload: dict) -> str:
    """The comment author's stable pseudonymous id, or '' if absent.

    Every source hides this somewhere different — YouTube under
    `authorChannelId.value`, Reddit under `author`. Both are checked for
    every payload rather than dispatching on source, so a corpus exported
    before this existed re-imports identically to a fresh ingest.

    Returns the platform id, never the display name. Ranking needs to know
    two comments came from one person; it does not need to know who, and a
    display name is the field that ends up on a public page by accident.
    """
    channel = payload.get("authorChannelId")
    if isinstance(channel, dict) and channel.get("value"):
        return str(channel["value"])
    author = payload.get("author")
    return str(author) if author else ""


def video_from_payload(payload: dict) -> str | None:
    """The video this comment was written under, or None.

    Derived from the payload for the same reason `author_id` is: it was
    already inside `raw_json`, and leaving it there meant every
    independence count reaching for it through an unindexed,
    source-specific JSON path in the hottest query in the system.

    None for sources that have no such container — a Reddit comment
    belongs to a submission, not a video, and `source_channel` already
    carries whatever subdivision a source uses.
    """
    video = payload.get("videoId")
    return str(video) if video else None


def normalize_for_match(text: str) -> str:
    """Collapse whitespace, case, and typographic punctuation for matching.

    Used only to compare quoted evidence against a comment body. It is
    deliberately lossy in ways that cannot change meaning, and deliberately
    conservative everywhere else.
    """
    folded = text.translate(_PUNCTUATION_FOLD).replace("…", "...")
    return " ".join(folded.split()).casefold()


#: A word this long or longer, shared as a prefix, is treated as the same
#: word said differently — "fem" for "feminine". Shorter prefixes match far
#: too much.
GROUNDING_PREFIX = 3

#: Claim types whose object must appear in the comment, because for these
#: the object **is** the claim: a NOTE_DESCRIPTOR is nothing but its
#: descriptor.
#:
#: Comparison types are deliberately excluded, and the exclusion is the
#: load-bearing part. Their object is an *entity*, and a commenter
#: legitimately names it somewhere this comment cannot see — a reply
#: saying "I have a dupe only" under a thread about Baccarat Rouge 540 is
#: a real DUPE_OF whose object lives in the parent. Applying grounding to
#: those demoted 126 claims against 9 real ones, and comparisons are the
#: scarcest evidence in the corpus.
GROUNDED_TYPES = frozenset({"NOTE_DESCRIPTOR", "AESTHETIC", "OCCASION"})


def value_is_grounded(value: str | None, body: str) -> bool:
    """Whether a claim's *value* traces back to what the commenter wrote.

    `evidence_is_quoted` checks the quotation. Nothing checked the
    descriptor inside it, so a claim could quote "It smells wonderful"
    verbatim, carry `raw_object_text = "rose"`, and be stored as verified
    evidence that somebody said rose. The adversarial review found this,
    and the corpus already held a milder case: evidence span "It is not a
    daily wear at all" with the occasion recorded as "special occasion
    scent for the cooler seasons".

    It matters more now than it did. The extraction prompt was just
    changed to require these types to carry an object, which turns
    "missing descriptor" into "descriptor the model supplied" — and a
    verifier that only checks the quotation cannot tell that from
    something a person wrote.

    Deliberately generous, because the model normalises and should:
    "more fem" grounds "feminine", "smells good on a man" does not ground
    "masculine" and is correctly refused. Any word of the value sharing a
    3-character prefix with any word of the comment is enough. Unicode
    aware — the corpus has Bengali and Vietnamese comments, and a check
    that stripped them to nothing would refuse every claim from those
    commenters.
    """
    if not value:
        return True
    words = re.findall(r"\w+", value.lower(), re.UNICODE)
    if not words:
        return True
    body_words = re.findall(r"\w+", (body or "").lower(), re.UNICODE)
    for word in words:
        head = word[:GROUNDING_PREFIX]
        if any(
            other.startswith(head) or word.startswith(other[:GROUNDING_PREFIX])
            for other in body_words
        ):
            return True
    return False


def evidence_is_quoted(span: str, body: str) -> bool:
    """Whether `span` is genuinely quoted from `body`.

    Exact substring first, then a normalized comparison so trivial
    requoting differences still count. False means the model paraphrased,
    and the claim's evidence cannot be shown to a reader as something a
    person actually wrote.

    Lives at module level, not only on `Claim`, so stored rows can be
    re-verified without reconstructing a full validated model.
    """
    if span in body:
        return True
    return normalize_for_match(span) in normalize_for_match(body)


class Claim(BaseModel):
    """A single structured claim extracted from a comment.

    Entity resolution happens later — subject and object are the raw text
    as written by the commenter, not resolved fragrance IDs.
    """

    claim_type: ClaimType

    subject_kind: SubjectKind
    raw_subject_text: str = Field(min_length=1)

    object_kind: ObjectKind
    raw_object_text: str | None = Field(default=None)

    sentiment: Sentiment = Field(
        default=Sentiment.NEUTRAL,
        description="Whether the commenter frames this positively or negatively. "
        "Carries the tone that a type name must not presume.",
    )
    polarity: Polarity = Field(
        default=Polarity.ASSERTED,
        description="Whether the commenter says this relationship holds "
        "(ASSERTED) or says it does not (DENIED).",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span: str = Field(
        min_length=1,
        description="Substring of the comment body supporting this claim.",
    )

    @model_validator(mode="after")
    def check_object_matches_claim_type(self) -> Claim:
        allowed = ALLOWED_OBJECT_KINDS[self.claim_type]
        if self.object_kind not in allowed:
            names = ", ".join(sorted(k.value for k in allowed))
            raise ValueError(
                f"{self.claim_type} allows object_kind {{{names}}}, "
                f"got {self.object_kind}"
            )
        if self.object_kind is ObjectKind.NONE and self.raw_object_text is not None:
            raise ValueError(
                f"{self.claim_type} takes no object, got {self.raw_object_text!r}"
            )
        if self.object_kind is not ObjectKind.NONE and not self.raw_object_text:
            raise ValueError(f"{self.claim_type} requires raw_object_text")
        return self

    @property
    def is_edge(self) -> bool:
        """Whether this claim is a fragrance-to-fragrance edge.

        A denial is not an edge. "Delina and Baccarat smells nothing alike"
        names two fragrances and a comparison type, and asserts that the
        edge does not exist — counting it would put the commenter's name
        behind the opposite of what they wrote.
        """
        return (
            self.claim_type in EDGE_CLAIM_TYPES
            and self.subject_kind is SubjectKind.FRAGRANCE
            and self.object_kind is ObjectKind.FRAGRANCE
            and self.polarity is Polarity.ASSERTED
        )

    def evidence_matches(self, body: str) -> bool:
        """Whether evidence_span is quoted and its value traces back."""
        if not evidence_is_quoted(self.evidence_span, body):
            return False
        if self.claim_type.value not in GROUNDED_TYPES:
            return True
        return value_is_grounded(self.raw_object_text, body)


class ExtractionResult(BaseModel):
    """The full set of claims extracted from one comment.

    comment_id is the local integer `comments.id` primary key, not the
    Reddit base-36 fullname — that lives in `comments.source_id`.
    """

    comment_id: int
    claims: list[Claim] = Field(default_factory=list)

    def verified_claims(self, body: str) -> list[Claim]:
        """Claims whose evidence_span is actually quoted from `body`."""
        return [c for c in self.claims if c.evidence_matches(body)]
