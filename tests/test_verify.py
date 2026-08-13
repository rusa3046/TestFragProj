"""Evidence verification: punctuation folding, and re-verifying stored rows.

Every case here is a real string from the first full extraction run over
the YouTube corpus.
"""

import pytest

from fragrance_graph.extract.llm import write_claims
from fragrance_graph.extract.verify import reverify
from fragrance_graph.ingest.store import ingest
from fragrance_graph.models import Claim, evidence_is_quoted, normalize_for_match
from tests.conftest import make_comment

# Observed live on comment 2522. The body's apostrophe is U+2019 because
# the commenter typed it on a phone; the model quoted it back as ASCII.
CURLY_BODY = (
    "I got Khamrah today and I smelled Angel Share in store and for me they "
    "really are similar, about 75 percent, but I don’t liek any as I "
    "don’t like sweet fragrances hahaha"
)
ASCII_SPAN = "I don't like sweet fragrances"


# --- punctuation folding ----------------------------------------------------


def test_curly_apostrophe_is_not_a_paraphrase():
    """The bug: a verbatim quote recorded as unauditable evidence."""
    assert ASCII_SPAN not in CURLY_BODY, "precondition: exact match must fail"
    assert evidence_is_quoted(ASCII_SPAN, CURLY_BODY)


@pytest.mark.parametrize(
    "span,body",
    [
        ("it's great", "honestly it’s great"),
        ('he said "wow"', "he said “wow”"),
        ("a top-tier pick", "a top–tier pick"),
        ("wait...", "wait…"),
        ("don't", "don‘t"),
    ],
)
def test_typographic_variants_still_verify(span, body):
    assert evidence_is_quoted(span, body)


def test_folding_is_symmetric():
    """Either side may carry the fancy glyph."""
    assert evidence_is_quoted("don’t like it", "i don't like it")
    assert evidence_is_quoted("don't like it", "i don’t like it")


def test_accents_are_not_stripped():
    """Folding must not reach past punctuation into letters."""
    assert not evidence_is_quoted("Wulong Cha", "I love Wulóng Chá")
    assert normalize_for_match("Chá") != normalize_for_match("Cha")


def test_a_real_paraphrase_is_still_caught():
    """Observed live on comment 2580: the model corrected the typo.

    Body says "om a sucker"; the model wrote "im a sucker". Fixing the
    punctuation fold must not start excusing this.
    """
    body = "I dont like clones, buuut om a sucker for cinnamon gourmands"
    span = "im a sucker for cinnamon gourmands"
    assert not evidence_is_quoted(span, body)


def test_invented_evidence_is_still_caught():
    assert not evidence_is_quoted("this smells like vanilla", "unrelated text")


# --- re-verifying stored claims ---------------------------------------------


def store_claim(conn, *, span, body, verified):
    ingest(conn, [make_comment(abs(hash((span, body))) % 90000, body=body)])
    comment_id = conn.execute(
        "SELECT id FROM comments ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    write_claims(
        conn,
        comment_id,
        body,
        [
            Claim(
                claim_type="NOTE_DESCRIPTOR",
                subject_kind="FRAGRANCE",
                raw_subject_text="Khamrah",
                object_kind="TAG",
                raw_object_text="sweet",
                confidence=0.8,
                evidence_span=span,
            )
        ],
    )
    conn.execute(
        "UPDATE claims SET evidence_verified = %s WHERE comment_id = %s",
        (verified, comment_id),
    )
    conn.commit()
    return comment_id


def test_reverify_promotes_a_stale_unverified_row(conn):
    """The reason this tool exists: correcting rows written under old rules."""
    store_claim(conn, span=ASCII_SPAN, body=CURLY_BODY, verified=0)

    stats = reverify(conn)

    assert (stats.promoted, stats.demoted) == (1, 0)
    assert conn.execute("SELECT evidence_verified FROM claims").fetchone()[0] == 1


def test_reverify_demotes_a_row_that_no_longer_qualifies(conn):
    store_claim(conn, span="invented text", body="unrelated body", verified=1)

    stats = reverify(conn)

    assert (stats.promoted, stats.demoted) == (0, 1)
    assert conn.execute("SELECT evidence_verified FROM claims").fetchone()[0] == 0


def test_reverify_is_idempotent(conn):
    store_claim(conn, span=ASCII_SPAN, body=CURLY_BODY, verified=0)

    reverify(conn)
    second = reverify(conn)

    assert (second.promoted, second.demoted) == (0, 0)


def test_dry_run_reports_without_writing(conn):
    store_claim(conn, span=ASCII_SPAN, body=CURLY_BODY, verified=0)

    stats = reverify(conn, dry_run=True)

    assert stats.promoted == 1
    assert conn.execute("SELECT evidence_verified FROM claims").fetchone()[0] == 0


def test_rates_are_reported_before_and_after(conn):
    store_claim(conn, span=ASCII_SPAN, body=CURLY_BODY, verified=0)
    store_claim(conn, span="invented", body="unrelated", verified=0)

    stats = reverify(conn)

    assert stats.total == 2
    assert stats.rate_before == 0.0
    assert stats.rate_after == 0.5
    assert "0.0% -> 50.0%" in str(stats)


def test_empty_database_is_harmless(conn):
    stats = reverify(conn)
    assert stats.total == 0
    assert "No claims" in str(stats)
