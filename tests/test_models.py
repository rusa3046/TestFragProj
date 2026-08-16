"""Extraction schema validation and evidence-span verification."""

import pytest
from pydantic import ValidationError

from fragrance_graph.models import (
    ALLOWED_OBJECT_KINDS,
    EDGE_CLAIM_TYPES,
    Claim,
    ClaimType,
    ExtractionResult,
    ObjectKind,
    Sentiment,
    SubjectKind,
)

BODY = "Honestly Delina  smells   just like Baccarat 540 to me, great for weddings."


def claim(**overrides):
    base = {
        "claim_type": ClaimType.DUPE_OF,
        "subject_kind": SubjectKind.FRAGRANCE,
        "raw_subject_text": "Delina",
        "object_kind": ObjectKind.FRAGRANCE,
        "raw_object_text": "Baccarat 540",
        "confidence": 0.9,
        "evidence_span": "smells   just like Baccarat 540",
    }
    return Claim(**{**base, **overrides})


def test_every_claim_type_declares_allowed_object_kinds():
    assert set(ALLOWED_OBJECT_KINDS) == set(ClaimType)


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


def test_removed_v1_types_are_gone():
    """REMINDS_ME_OF merged into SIMILAR_TO; LONGEVITY_COMPLAINT split."""
    names = {t.value for t in ClaimType}
    assert "REMINDS_ME_OF" not in names
    assert "LONGEVITY_COMPLAINT" not in names


# --- object kind ------------------------------------------------------------


def test_similar_to_accepts_a_house_object():
    """v1 forced 'a Serge Lutens vibe' to be recorded as a fragrance."""
    c = claim(
        claim_type=ClaimType.SIMILAR_TO,
        object_kind=ObjectKind.HOUSE,
        raw_object_text="Serge Lutens",
    )
    assert c.object_kind is ObjectKind.HOUSE


def test_similar_to_accepts_a_material_object():
    c = claim(
        claim_type=ClaimType.SIMILAR_TO,
        object_kind=ObjectKind.TAG,
        raw_object_text="cocoa pyrazine",
    )
    assert c.object_kind is ObjectKind.TAG


def test_dupe_of_rejects_a_house_object():
    """Only SIMILAR_TO reaches beyond fragrances."""
    with pytest.raises(ValidationError, match="allows object_kind"):
        claim(
            claim_type=ClaimType.DUPE_OF,
            object_kind=ObjectKind.HOUSE,
            raw_object_text="Serge Lutens",
        )


@pytest.mark.parametrize(
    "claim_type",
    [t for t, k in ALLOWED_OBJECT_KINDS.items() if k == {ObjectKind.NONE}],
)
def test_objectless_types_reject_an_object(claim_type):
    with pytest.raises(ValidationError):
        claim(
            claim_type=claim_type,
            object_kind=ObjectKind.TAG,
            raw_object_text="anything",
        )


@pytest.mark.parametrize(
    "claim_type",
    [t for t, k in ALLOWED_OBJECT_KINDS.items() if k == {ObjectKind.NONE}],
)
def test_objectless_types_are_valid_with_no_object(claim_type):
    c = claim(claim_type=claim_type, object_kind=ObjectKind.NONE, raw_object_text=None)
    assert c.raw_object_text is None


def test_object_required_when_kind_is_not_none():
    with pytest.raises(ValidationError, match="requires raw_object_text"):
        claim(object_kind=ObjectKind.FRAGRANCE, raw_object_text=None)


# --- sentiment --------------------------------------------------------------


def test_sentiment_defaults_to_neutral():
    assert claim().sentiment is Sentiment.NEUTRAL


def test_longevity_can_be_praise():
    """v1 stored 'still lingering, delightful' as a COMPLAINT."""
    c = claim(
        claim_type=ClaimType.LONGEVITY,
        object_kind=ObjectKind.NONE,
        raw_object_text=None,
        sentiment=Sentiment.POSITIVE,
        evidence_span="great for weddings",
    )
    assert c.sentiment is Sentiment.POSITIVE


# --- subject kind and edges -------------------------------------------------


def test_category_subject_is_allowed_but_not_an_edge():
    """'skin scents' is real signal that entity resolution must skip."""
    c = claim(
        claim_type=ClaimType.LONGEVITY,
        subject_kind=SubjectKind.CATEGORY,
        raw_subject_text="skin scents",
        object_kind=ObjectKind.NONE,
        raw_object_text=None,
    )
    assert not c.is_edge


def test_fragrance_to_fragrance_comparison_is_an_edge():
    assert claim(claim_type=ClaimType.SIMILAR_TO).is_edge


def test_house_object_is_not_an_edge():
    c = claim(
        claim_type=ClaimType.SIMILAR_TO,
        object_kind=ObjectKind.HOUSE,
        raw_object_text="Serge Lutens",
    )
    assert not c.is_edge


def test_descriptor_is_not_an_edge():
    c = claim(
        claim_type=ClaimType.NOTE_DESCRIPTOR,
        object_kind=ObjectKind.TAG,
        raw_object_text="citrusy",
    )
    assert not c.is_edge


def test_edge_types_are_the_comparison_types():
    assert EDGE_CLAIM_TYPES == {
        ClaimType.SIMILAR_TO,
        ClaimType.DUPE_OF,
        ClaimType.BETTER_THAN,
    }


# --- evidence verification -------------------------------------------------


def test_exact_quote_verifies():
    assert claim().evidence_matches(BODY)


def test_requoted_whitespace_and_case_still_verifies():
    assert claim(evidence_span="smells just like baccarat 540").evidence_matches(BODY)


def test_paraphrase_does_not_verify():
    """The whole point: unquoted evidence is fiction."""
    c = claim(evidence_span="the commenter considers it a dupe of Baccarat 540")
    assert not c.evidence_matches(BODY)


def test_span_from_a_different_comment_does_not_verify():
    assert not claim(evidence_span="smells like Aventus").evidence_matches(BODY)


def test_verified_claims_filters_out_paraphrase():
    good = claim()
    bad = claim(evidence_span="I am inventing this quote wholesale")
    result = ExtractionResult(comment_id=1, claims=[good, bad])

    assert len(result.claims) == 2
    assert result.verified_claims(BODY) == [good]


def test_extraction_result_defaults_to_no_claims():
    """Most comments assert nothing; that is a valid result, not an error."""
    assert ExtractionResult(comment_id=1).claims == []


class TestTheValueMustTraceToTheComment:
    """P1 from adversarial review, confirmed.

    `evidence_is_quoted` checked the quotation and nothing checked the
    descriptor inside it. A claim could quote "It smells wonderful"
    verbatim, carry raw_object_text "rose", and store as verified
    evidence that somebody said rose.

    It matters more since the extraction prompt began requiring these
    types to carry an object: that turns "missing descriptor" into
    "descriptor the model supplied", and a verifier checking only the
    quotation cannot tell the two apart.
    """

    def test_the_reviewers_reproduction(self):
        from fragrance_graph.models import value_is_grounded

        assert not value_is_grounded("rose", "It smells wonderful")

    def test_the_case_already_in_the_corpus(self):
        from fragrance_graph.models import value_is_grounded

        assert not value_is_grounded(
            "special occasion scent for the cooler seasons",
            "It is not a daily wear at all",
        )

    def test_a_quoted_descriptor_is_grounded(self):
        from fragrance_graph.models import value_is_grounded

        assert value_is_grounded("woodsy", "It's woodsy")
        assert value_is_grounded("date nights", "great for date nights")

    def test_normalisation_still_counts(self):
        """The model should normalise, and refusing that would discard
        real evidence: "more fem" is a person saying feminine."""
        from fragrance_graph.models import value_is_grounded

        assert value_is_grounded("feminine", "yeah the original is more fem")
        assert value_is_grounded("vanilla", "so much vanilla in this")

    def test_non_latin_script_is_not_silently_refused(self):
        """The corpus has Bengali and Vietnamese commenters. A check that
        stripped their alphabets to nothing would refuse every claim they
        ever make, which is a worse bug than the one being fixed."""
        from fragrance_graph.models import value_is_grounded

        assert value_is_grounded("নারিকেল তেল", "Qahwa is নারিকেল তেল। হাজবেন্ডের")
        assert value_is_grounded("ngọt gắt", "mùi ngọt gắt quá")

    def test_an_objectless_claim_is_unaffected(self):
        from fragrance_graph.models import value_is_grounded

        assert value_is_grounded(None, "it lasts forever")
        assert value_is_grounded("", "it lasts forever")

    def test_evidence_matches_now_requires_both(self):
        from fragrance_graph.models import Claim

        claim = Claim(
            claim_type="NOTE_DESCRIPTOR", subject_kind="FRAGRANCE",
            raw_subject_text="it", object_kind="TAG", raw_object_text="rose",
            sentiment="POSITIVE", polarity="ASSERTED", confidence=0.9,
            evidence_span="It smells wonderful",
        )
        assert not claim.evidence_matches("It smells wonderful"), (
            "the span is quoted but the value is not in it"
        )


class TestGroundingDoesNotEatComparisons:
    """The scoping is the load-bearing half of the grounding fix.

    Applied to every claim type it demoted 126 rows against 9 real ones,
    almost all of them comparisons -- the scarcest evidence in the corpus.
    A comparison's object is an *entity*, and a commenter legitimately
    names it where this comment cannot see: "I have a dupe only", under a
    thread about Baccarat Rouge 540, is a real DUPE_OF.
    """

    def test_a_comparison_object_may_come_from_context(self):
        from fragrance_graph.models import Claim

        claim = Claim(
            claim_type="DUPE_OF", subject_kind="FRAGRANCE",
            raw_subject_text="it", object_kind="FRAGRANCE",
            raw_object_text="Baccarat Rouge 540", sentiment="NEUTRAL",
            polarity="ASSERTED", confidence=0.9,
            evidence_span="I have a dupe only",
        )
        assert claim.evidence_matches("Me too. Very expensive. I have a dupe only.")

    def test_a_descriptor_may_not(self):
        from fragrance_graph.models import Claim

        claim = Claim(
            claim_type="NOTE_DESCRIPTOR", subject_kind="FRAGRANCE",
            raw_subject_text="it", object_kind="TAG", raw_object_text="rose",
            sentiment="POSITIVE", polarity="ASSERTED", confidence=0.9,
            evidence_span="It smells wonderful",
        )
        assert not claim.evidence_matches("It smells wonderful")

    def test_only_the_tagged_types_are_grounded(self):
        from fragrance_graph.models import GROUNDED_TYPES

        assert GROUNDED_TYPES == {"NOTE_DESCRIPTOR", "AESTHETIC", "OCCASION"}
        for comparison in ("SIMILAR_TO", "DUPE_OF", "BETTER_THAN"):
            assert comparison not in GROUNDED_TYPES
