"""Pydantic schemas for LLM-extracted fragrance claims."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ClaimType(StrEnum):
    SIMILAR_TO = "SIMILAR_TO"
    DUPE_OF = "DUPE_OF"
    REMINDS_ME_OF = "REMINDS_ME_OF"
    BETTER_THAN = "BETTER_THAN"
    OCCASION = "OCCASION"
    AESTHETIC = "AESTHETIC"
    LONGEVITY_COMPLAINT = "LONGEVITY_COMPLAINT"
    UNMET_PRODUCT_REQUEST = "UNMET_PRODUCT_REQUEST"


class ObjectKind(StrEnum):
    """What sort of thing raw_object_text names, if anything.

    FRAGRANCE claims are binary edges between two fragrances and are what
    the similarity graph is built from. TAG claims are unary attributes of
    the subject, where the object is a non-fragrance concept ("weddings",
    "old money"). NONE claims have no object at all.
    """

    FRAGRANCE = "FRAGRANCE"
    TAG = "TAG"
    NONE = "NONE"


#: Which shape each claim type takes. Derived rather than model-emitted:
#: it costs no output tokens and the LLM cannot get it inconsistent.
OBJECT_KIND_BY_CLAIM_TYPE: dict[ClaimType, ObjectKind] = {
    ClaimType.SIMILAR_TO: ObjectKind.FRAGRANCE,
    ClaimType.DUPE_OF: ObjectKind.FRAGRANCE,
    ClaimType.REMINDS_ME_OF: ObjectKind.FRAGRANCE,
    ClaimType.BETTER_THAN: ObjectKind.FRAGRANCE,
    ClaimType.OCCASION: ObjectKind.TAG,
    ClaimType.AESTHETIC: ObjectKind.TAG,
    ClaimType.LONGEVITY_COMPLAINT: ObjectKind.NONE,
    ClaimType.UNMET_PRODUCT_REQUEST: ObjectKind.NONE,
}


def normalize_for_match(text: str) -> str:
    """Collapse whitespace and case so quoting differences don't fail a match."""
    return " ".join(text.split()).casefold()


class Claim(BaseModel):
    """A single structured claim extracted from a comment.

    Entity resolution happens later — subject/object are the raw text as
    written by the commenter, not resolved fragrance IDs.
    """

    claim_type: ClaimType
    raw_subject_text: str = Field(min_length=1)
    raw_object_text: str | None = Field(
        default=None,
        description="Second entity in the claim. Another fragrance for "
        "SIMILAR_TO/DUPE_OF/REMINDS_ME_OF/BETTER_THAN; a non-fragrance tag "
        "for OCCASION/AESTHETIC; omitted for LONGEVITY_COMPLAINT and "
        "UNMET_PRODUCT_REQUEST.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span: str = Field(
        min_length=1,
        description="Substring of the comment body supporting this claim. Quote "
        "the comment verbatim; do not paraphrase.",
    )

    @property
    def object_kind(self) -> ObjectKind:
        return OBJECT_KIND_BY_CLAIM_TYPE[self.claim_type]

    @model_validator(mode="after")
    def check_object_matches_claim_type(self) -> Claim:
        kind = self.object_kind
        if kind is ObjectKind.NONE and self.raw_object_text is not None:
            raise ValueError(
                f"{self.claim_type} takes no object, got {self.raw_object_text!r}"
            )
        if kind is not ObjectKind.NONE and not self.raw_object_text:
            raise ValueError(f"{self.claim_type} requires raw_object_text")
        return self

    def evidence_matches(self, body: str) -> bool:
        """Whether evidence_span is genuinely quoted from the comment body.

        Exact substring first, then a whitespace/case-normalized comparison
        so trivial requoting differences still count. A False here means the
        model paraphrased, and the claim's evidence cannot be audited.
        """
        if self.evidence_span in body:
            return True
        return normalize_for_match(self.evidence_span) in normalize_for_match(body)


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
