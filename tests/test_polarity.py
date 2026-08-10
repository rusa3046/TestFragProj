"""Denials must never become edges.

The defect this guards: "I just try latafa it is nothing like angel share"
was stored as DUPE_OF latafa -> angel share. On a product that sells
verbatim evidence, quoting someone as support for the claim they rejected
is worse than any accuracy number — so these are trust tests, not
correctness tests.
"""

import pytest
from pydantic import ValidationError

from fragrance_graph.extract.polarity import (
    DENIAL_PATTERN,
    audit,
    denied_without_flag,
    main,
)
from fragrance_graph.models import Claim, Polarity
from fragrance_graph.query import similar_to
from fragrance_graph.resolve.entities import add_fragrance
from tests.test_query import add_claim, add_comment


def claim(**overrides):
    base = dict(
        claim_type="SIMILAR_TO",
        subject_kind="FRAGRANCE",
        raw_subject_text="Khamrah",
        object_kind="FRAGRANCE",
        raw_object_text="Angels' Share",
        sentiment="NEGATIVE",
        confidence=0.9,
        evidence_span="nothing alike",
    )
    return Claim(**{**base, **overrides})


# --- the model ------------------------------------------------------------


def test_claims_are_asserted_unless_said_otherwise():
    """Most claims are assertions; the default must not require thought."""
    assert claim().polarity is Polarity.ASSERTED


def test_a_denial_is_not_an_edge():
    assert claim().is_edge
    assert not claim(polarity="DENIED").is_edge


def test_polarity_is_independent_of_sentiment():
    """The whole reason this is its own field.

    All 36 measured denials were NEGATIVE, but so were five genuine edges
    — "worst dupe of (540)" asserts the dupe and dislikes it. Folding
    polarity into sentiment would have discarded those five.
    """
    negative_but_real = claim(sentiment="NEGATIVE", polarity="ASSERTED")
    assert negative_but_real.is_edge, "'worst dupe of 540' is still a dupe"

    denied_but_neutral = claim(sentiment="NEUTRAL", polarity="DENIED")
    assert not denied_but_neutral.is_edge


def test_polarity_rejects_unknown_values():
    with pytest.raises(ValidationError):
        claim(polarity="MAYBE")


# --- the query layer ------------------------------------------------------


def test_denied_claims_never_reach_a_reader(conn):
    """The live bug: a denial ranking as a similarity, quoting the denier."""
    target = add_fragrance(conn, "Angels' Share")
    other = add_fragrance(conn, "Khamrah")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=other, obj=target,
              evidence="it is nothing like angel share")
    conn.execute("UPDATE claims SET polarity = 'DENIED'")
    conn.commit()

    assert similar_to(conn, target) == []
    assert similar_to(conn, other) == []


def test_a_denial_does_not_suppress_a_real_edge(conn):
    """One person's denial must not delete everyone else's assertion."""
    target = add_fragrance(conn, "Angels' Share")
    other = add_fragrance(conn, "Khamrah")
    for i, author in enumerate(["alice", "bob"]):
        cid = add_comment(conn, i, body="x", author=author)
        add_claim(conn, cid, subject=other, obj=target, evidence=f"{author} agrees")
    denier = add_comment(conn, 9, body="x", author="carol")
    add_claim(conn, denier, subject=other, obj=target, evidence="nothing alike")
    conn.execute("UPDATE claims SET polarity = 'DENIED' WHERE comment_id = ?",
                 (denier,))
    conn.commit()

    (result,) = similar_to(conn, target)
    assert result.commenters == 2, "the denier must not be counted as support"
    quotes = [e.quote for e in result.evidence]
    assert "nothing alike" not in quotes


def test_sentiment_rollup_ignores_denials(conn):
    from fragrance_graph.query import sentiment_rollup

    frag = add_fragrance(conn, "Khamrah")
    other = add_fragrance(conn, "Angels' Share")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=frag, obj=other, sentiment="NEGATIVE")
    conn.execute("UPDATE claims SET polarity = 'DENIED'")
    conn.commit()

    assert sentiment_rollup(conn, frag).counts == {}


# --- the audit instrument -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "it is nothing like angel share",
        "Delina and Baccarat smells nothing alike",
        "Cedrat boise is not even similar to aventus",
        "Tommy Hilfiger Impact Intense smells completely different than Layton",
        "Khamra doesn't even smell close to Angels' Share",
        "far from being 1:1 clone",
        "There's no clone of TF oud wood that captures the scent",
        "They smell way different than CDNIMs",
    ],
)
def test_denial_pattern_catches_the_real_phrasings(text):
    """Every case here is a verbatim span from the corpus, not invented."""
    assert DENIAL_PATTERN.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "worst dupe of (540)",
        "it smells exactly like Axe Phoenix",
        "smells just like Baccarat 540",
    ],
)
def test_denial_pattern_leaves_genuine_assertions_alone(text):
    assert not DENIAL_PATTERN.search(text), text


def test_audit_separates_missed_from_caught(conn):
    target = add_fragrance(conn, "Angels' Share")
    other = add_fragrance(conn, "Khamrah")
    for i, (evidence, polarity) in enumerate(
        [
            ("it is nothing like angel share", "ASSERTED"),  # missed
            ("Delina and Baccarat smells nothing alike", "DENIED"),  # caught
            ("smells just like it", "ASSERTED"),  # not a denial
        ]
    ):
        cid = add_comment(conn, i, body="x", author=f"a{i}")
        add_claim(conn, cid, subject=other, obj=target, evidence=evidence)
        conn.execute(
            "UPDATE claims SET polarity = ? WHERE comment_id = ?", (polarity, cid)
        )
    conn.commit()

    result = audit(conn)
    assert len(result["missed"]) == 1
    assert len(result["caught"]) == 1


def test_audit_flags_denials_the_pattern_did_not_predict(conn):
    """A silent recall loss no other check would catch: the model calling
    ordinary claims denials."""
    target = add_fragrance(conn, "Angels' Share")
    other = add_fragrance(conn, "Khamrah")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=other, obj=target, evidence="smells just like it")
    conn.execute("UPDATE claims SET polarity = 'DENIED'")
    conn.commit()

    (odd,) = denied_without_flag(conn)
    assert odd["evidence_span"] == "smells just like it"


def test_audit_exits_nonzero_while_denials_are_live(conn, tmp_path, capsys):
    """So it can gate a re-extraction instead of being something you
    remember to read."""
    from fragrance_graph.db import get_connection, migrate

    db = tmp_path / "audit.db"
    conn.close()
    fresh = get_connection(db)
    migrate(fresh)
    target = add_fragrance(fresh, "Angels' Share")
    other = add_fragrance(fresh, "Khamrah")
    cid = add_comment(fresh, 1, body="x", author="alice")
    add_claim(fresh, cid, subject=other, obj=target,
              evidence="it is nothing like angel share")
    fresh.close()

    assert main(["audit", "--db-path", str(db)]) == 1
    assert "still stored as assertions" in capsys.readouterr().out


def test_audit_on_a_clean_corpus_exits_zero(conn, tmp_path, capsys):
    from fragrance_graph.db import get_connection, migrate

    db = tmp_path / "clean.db"
    conn.close()
    fresh = get_connection(db)
    migrate(fresh)
    fresh.close()

    assert main(["audit", "--db-path", str(db)]) == 0


# --- the eval -------------------------------------------------------------


def test_denials_are_not_scored(conn):
    """LABELLING.md tells a human to drop denials, so a correctly-marked
    denial must not be compared against labels — otherwise getting it right
    reads as a false positive and the fix looks like a regression."""
    from fragrance_graph.evals.score import extracted_claims

    target = add_fragrance(conn, "Angels' Share")
    other = add_fragrance(conn, "Khamrah")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=other, obj=target, evidence="nothing alike")
    conn.execute("UPDATE claims SET polarity = 'DENIED'")
    conn.commit()

    assert extracted_claims(conn) == {}
