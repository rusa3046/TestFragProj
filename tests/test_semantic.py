"""Retrieving evidence whose wording you did not guess.

The rule under test is narrow and absolute: a vector may bring a bottle
into consideration and may never raise how strongly anything is known.
"""

import json

import pytest

from fragrance_graph.ingest.store import ingest
from fragrance_graph.resolve.entities import add_fragrance
from fragrance_graph.semantic import (
    HashedNGrams,
    backfill,
    candidates_for,
    cosine,
    nearest,
)
from tests.conftest import make_comment


def descriptor(conn, i, *, frag, value, author="p1", channel="chan_a"):
    body = f"comment {i}: {value}"
    ingest(conn, [make_comment(
        i, body=body, source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": "v1"}),
    )])
    cid = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, sentiment,
             confidence, evidence_span, evidence_verified, polarity,
             extraction_model, created_at)
        VALUES (%s, 'NOTE_DESCRIPTOR', 'FRAGRANCE', 'it', %s, 'TAG', %s,
                'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, frag, value, body),
    )
    conn.commit()


class TestTheEmbedder:
    def test_it_is_deterministic_across_calls(self):
        """Python's `hash` is salted per process. A vector that changed
        between runs would put stored and query vectors in different
        spaces, and it would look like poor recall rather than a bug."""
        assert HashedNGrams().embed("rose") == HashedNGrams().embed("rose")

    def test_vectors_are_normalised(self):
        vector = HashedNGrams().embed("rose")
        assert abs(sum(x * x for x in vector) - 1.0) < 1e-9

    def test_empty_text_does_not_explode(self):
        assert cosine(HashedNGrams().embed(""), HashedNGrams().embed("")) == 0.0

    def test_morphological_variants_are_close(self):
        e = HashedNGrams()
        assert cosine(e.embed("rosy"), e.embed("roses")) > 0.3

    def test_unrelated_words_are_not(self):
        e = HashedNGrams()
        assert cosine(e.embed("rose"), e.embed("tobacco")) < 0.2

    def test_it_does_not_pretend_to_know_meaning(self):
        """"airy" and "light" mean nearly the same thing and share no
        letters. Stated as a test so nobody mistakes this for semantics."""
        e = HashedNGrams()
        assert cosine(e.embed("airy"), e.embed("light")) < 0.2

    def test_comparing_across_models_is_an_error_not_a_number(self):
        with pytest.raises(ValueError, match="different spaces"):
            cosine([1.0, 0.0], [1.0, 0.0, 0.0])


class TestBackfill:
    def test_it_embeds_descriptive_claims(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="rose bomb")
        assert backfill(conn) == 1

    def test_it_is_idempotent(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="rose bomb")
        backfill(conn)
        backfill(conn)
        assert conn.execute(
            "SELECT count(*) FROM evidence_embeddings"
        ).fetchone()[0] == 1

    def test_a_second_model_does_not_overwrite_the_first(self, conn):
        """A rebuild must be comparable against what it replaced."""
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="rose bomb")
        backfill(conn)
        backfill(conn, HashedNGrams(name="other-model"))
        assert conn.execute(
            "SELECT count(*) FROM evidence_embeddings"
        ).fetchone()[0] == 2


class TestRetrieval:
    def test_it_finds_a_wording_the_query_did_not_use(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        (match,) = nearest(conn, "rosy")
        assert match.text == "roses"
        assert match.canonical_name == "Parfums de Marly Delina"

    def test_extra_words_dilute_the_match(self, conn):
        """A measured limit of character n-grams, pinned rather than hidden.

        "rosy" against "rose bomb" scores 0.20 — the "bomb" n-grams are
        half the vector and share nothing with the query — so it falls
        below the threshold even though a person would call them related.
        A neural embedder is what fixes this, and `Embedder` exists so one
        can be dropped in without touching storage or retrieval.
        """
        from fragrance_graph.semantic import HashedNGrams, cosine

        e = HashedNGrams()
        assert cosine(e.embed("rosy"), e.embed("roses")) > 0.4
        assert cosine(e.embed("rosy"), e.embed("rose bomb")) < 0.4

    def test_it_returns_nothing_for_words_the_corpus_never_used(self, conn):
        """The ceiling. "Hotel lobby" appears zero times in the real
        corpus and no embedder invents it."""
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="rose bomb")
        backfill(conn)
        assert nearest(conn, "hotel lobby") == []

    def test_a_match_carries_the_claim_it_came_from(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        (match,) = nearest(conn, "rosy")
        assert match.claim_id
        assert conn.execute(
            "SELECT raw_object_text FROM claims WHERE id = %s", (match.claim_id,)
        ).fetchone()[0] == "roses"

    def test_an_unattributed_match_is_not_a_candidate(self, conn):
        """Real evidence about something, but not a reason to recommend any
        particular bottle."""
        add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=None, value="roses")
        backfill(conn)
        assert nearest(conn, "rosy"), "still retrievable"
        assert candidates_for(conn, "rosy") == {}, "but not recommendable"


class TestItRetrievesAndNeverAsserts:
    def test_a_semantic_hit_is_only_ever_one_observation(self, conn):
        """However close the vectors are. A phrase resembling another
        phrase is not people agreeing, and letting similarity raise a
        Strength is the laundering the evidence model forbids."""
        from fragrance_graph.evidence import Strength
        from fragrance_graph.recommend import recommend

        frag = add_fragrance(conn, "Parfums de Marly Delina")
        for i, author in enumerate(["p1", "p2", "p3"]):
            descriptor(conn, i, frag=frag, value="rose bomb", author=author,
                       channel=f"c{i}")
        backfill(conn)
        answer = recommend(conn, "something like Rosebomb")
        hits = [
            r for c in answer.results for r in c.reasons if r.kind == "semantic"
        ]
        for hit in hits:
            assert hit.strength is Strength.OBSERVED
            assert not hit.declarable

    def test_it_never_creates_a_similarity_edge(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="rose bomb")
        backfill(conn)
        before = conn.execute(
            "SELECT count(*) FROM claims WHERE claim_type = 'SIMILAR_TO'"
        ).fetchone()[0]
        nearest(conn, "rosy")
        candidates_for(conn, "rosy")
        after = conn.execute(
            "SELECT count(*) FROM claims WHERE claim_type = 'SIMILAR_TO'"
        ).fetchone()[0]
        assert before == after == 0
