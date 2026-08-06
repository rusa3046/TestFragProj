"""Extraction schema validation and evidence-span verification."""

import pytest
from pydantic import ValidationError

from fragrance_graph.models import (
    OBJECT_KIND_BY_CLAIM_TYPE,
    Claim,
    ClaimType,
    ExtractionResult,
    ObjectKind,
)

BODY = "Honestly Delina  smells   just like Baccarat 540 to me, great for weddings."


def claim(**overrides):
    base = {
        "claim_type": ClaimType.DUPE_OF,
        "raw_subject_text": "Delina",
        "raw_object_text": "Baccarat 540",
        "confidence": 0.9,
        "evidence_span": "smells   just like Baccarat 540",
    }
    return Claim(**{**base, **overrides})


def test_every_claim_type_has_an_object_kind():
    assert set(OBJECT_KIND_BY_CLAIM_TYPE) == set(ClaimType)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_must_be_a_probability(confidence):
    with pytest.raises(ValidationError):
        claim(confidence=confidence)


@pytest.mark.parametrize("field", ["raw_subject_text", "evidence_span"])
def test_required_text_fields_reject_empty(field):
    with pytest.raises(ValidationError):
        claim(**{field: ""})


def test_unknown_claim_type_rejected():
    with pytest.raises(ValidationError):
        claim(claim_type="SMELLS_NICE")


# --- object_kind invariants ------------------------------------------------


@pytest.mark.parametrize(
    "claim_type",
    [t for t, k in OBJECT_KIND_BY_CLAIM_TYPE.items() if k is ObjectKind.FRAGRANCE],
)
def test_fragrance_claims_are_binary_edges(claim_type):
    assert claim(claim_type=claim_type).object_kind is ObjectKind.FRAGRANCE


def test_occasion_object_is_a_tag_not_a_fragrance():
    c = claim(claim_type=ClaimType.OCCASION, raw_object_text="weddings")
    assert c.object_kind is ObjectKind.TAG


def test_unmet_request_keeps_the_requested_product():
    """The requested thing is the whole point of the claim type."""
    c = claim(
        claim_type=ClaimType.UNMET_PRODUCT_REQUEST,
        raw_object_text="body lotion",
        evidence_span="great for weddings",
    )
    assert c.object_kind is ObjectKind.TAG
    assert c.raw_object_text == "body lotion"


def test_objectless_request_is_rejected():
    """A request with no 'what' is contentless and must not be stored."""
    with pytest.raises(ValidationError, match="requires raw_object_text"):
        claim(claim_type=ClaimType.UNMET_PRODUCT_REQUEST, raw_object_text=None)


def test_claim_type_with_no_object_rejects_one():
    with pytest.raises(ValidationError, match="takes no object"):
        claim(claim_type=ClaimType.LONGEVITY_COMPLAINT, raw_object_text="anything")


def test_longevity_complaint_needs_no_object():
    c = claim(claim_type=ClaimType.LONGEVITY_COMPLAINT, raw_object_text=None)
    assert c.object_kind is ObjectKind.NONE


# --- evidence verification -------------------------------------------------


def test_exact_quote_verifies():
    assert claim().evidence_matches(BODY)


def test_requoted_whitespace_and_case_still_verifies():
    c = claim(evidence_span="smells just like baccarat 540")
    assert c.evidence_matches(BODY)


def test_paraphrase_does_not_verify():
    """The whole point: unquoted evidence is fiction."""
    c = claim(evidence_span="the commenter considers it a dupe of Baccarat 540")
    assert not c.evidence_matches(BODY)


def test_span_from_a_different_comment_does_not_verify():
    c = claim(evidence_span="smells like Aventus")
    assert not c.evidence_matches(BODY)


def test_verified_claims_filters_out_paraphrase():
    good = claim()
    bad = claim(evidence_span="I am inventing this quote wholesale")
    result = ExtractionResult(comment_id=1, claims=[good, bad])

    assert len(result.claims) == 2
    assert result.verified_claims(BODY) == [good]


def test_extraction_result_defaults_to_no_claims():
    """Most comments assert nothing; that is a valid result, not an error."""
    assert ExtractionResult(comment_id=1).claims == []
