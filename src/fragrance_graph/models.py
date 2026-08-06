"""Pydantic schemas for LLM-extracted fragrance claims."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ClaimType(StrEnum):
    SIMILAR_TO = "SIMILAR_TO"
    DUPE_OF = "DUPE_OF"
    REMINDS_ME_OF = "REMINDS_ME_OF"
    BETTER_THAN = "BETTER_THAN"
    OCCASION = "OCCASION"
    AESTHETIC = "AESTHETIC"
    LONGEVITY_COMPLAINT = "LONGEVITY_COMPLAINT"
    UNMET_PRODUCT_REQUEST = "UNMET_PRODUCT_REQUEST"


class Claim(BaseModel):
    """A single structured claim extracted from a comment.

    Entity resolution happens later — subject/object are the raw text as
    written by the commenter, not resolved fragrance IDs.
    """

    claim_type: ClaimType
    raw_subject_text: str = Field(min_length=1)
    raw_object_text: str | None = Field(
        default=None,
        description="Second fragrance/entity in the claim, if any (e.g. the "
        "dupe target for DUPE_OF). Absent for claim types with no object, "
        "such as OCCASION or UNMET_PRODUCT_REQUEST.",
    )
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span: str = Field(
        min_length=1, description="Substring of the comment body supporting this claim"
    )


class ExtractionResult(BaseModel):
    """The full set of claims extracted from one comment."""

    comment_id: int
    claims: list[Claim] = Field(default_factory=list)
