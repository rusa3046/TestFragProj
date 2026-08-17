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


class TestCodexPhase5Findings:
    """Four findings from the 2026-08-15 semantic review, all confirmed."""

    def test_a_claim_corrected_to_denied_stops_being_retrievable(self, conn):
        """P1. `backfill` embedded only eligible claims but `nearest` never
        re-checked, and nothing deleted rows that stopped qualifying. A
        denial came back as "someone described it as 'roses'"."""
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        assert nearest(conn, "rosy")

        conn.execute("UPDATE claims SET polarity = 'DENIED'")
        conn.commit()
        assert nearest(conn, "rosy") == [], "a denial is not a description"

    def test_backfill_deletes_rows_that_stopped_qualifying(self, conn):
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        conn.execute("UPDATE claims SET evidence_verified = 0")
        conn.commit()
        backfill(conn)
        assert conn.execute(
            "SELECT count(*) FROM evidence_embeddings"
        ).fetchone()[0] == 0

    def test_an_edited_claim_cannot_keep_quoting_its_old_wording(self, conn):
        """The stored text was returned rather than the claim's current
        text, so a correction left the old words on the page."""
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        conn.execute("UPDATE claims SET raw_object_text = 'rosewater'")
        conn.commit()
        (match,) = nearest(conn, "rosy")
        assert match.text == "rosewater"

    def test_an_inferred_attribution_discloses_itself(self, conn):
        """P1. `nearest` accepts proposed attributions, and the sentence
        built from one said "someone described it as X" with no hint that
        nobody named the bottle."""
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=None, value="roses")
        conn.execute(
            "INSERT INTO claim_attributions (claim_id, role, fragrance_id, "
            "method, evidence, confidence, review_status, created_at) "
            "VALUES ((SELECT max(id) FROM claims), 'subject', %s, "
            "'video_subject', 'title', 0.95, 'proposed', '2026-01-01')",
            (frag,),
        )
        conn.commit()
        backfill(conn)
        (match,) = nearest(conn, "rosy")
        assert match.inferred

    def test_the_never_asserts_test_actually_exercises_a_hit(self, conn):
        """P3. The original assertion looped over hits and would pass with
        zero of them, proving nothing about the wiring."""
        from fragrance_graph.evidence import Strength
        from fragrance_graph.recommend import recommend

        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        # "rosy" scores 0.445 against "roses"; "rosies" scores 0.276 and
        # falls below MIN_SCORE, which is the diluted-match limit pinned
        # above rather than a bug.
        answer = recommend(conn, "something like Rosy")
        hits = [
            r for c in answer.results for r in c.reasons if r.kind == "semantic"
        ]
        assert hits, "the wiring must actually produce a semantic reason"
        assert all(h.strength is Strength.OBSERVED for h in hits)
        assert all(not h.declarable for h in hits)

    def test_a_rebuild_and_an_update_agree_on_what_is_embedded(self, conn):
        """Found by the phase-9 rebuild. The stale-row delete did not apply
        the six-word cap the insert does, so a database updated
        incrementally kept 18 rows a clean rebuild never produces — the two
        disagreed about what was retrievable, which only shows up when
        somebody rebuilds."""
        frag = add_fragrance(conn, "Parfums de Marly Delina")
        descriptor(conn, 1, frag=frag, value="roses")
        backfill(conn)
        conn.execute(
            "UPDATE claims SET raw_object_text = %s",
            ("a very long personal memory about a ski lodge in winter",),
        )
        conn.commit()
        backfill(conn)
        assert conn.execute(
            "SELECT count(*) FROM evidence_embeddings"
        ).fetchone()[0] == 0


class TestTheDistributionalArmWasMeasuredAndRejected:
    """Kept for reproducibility, not in use.

    The spot-checked pairs looked good and the measured eval did not. These
    pin both halves so nobody re-adopts it from the encouraging half.
    """

    @pytest.fixture
    def trained(self):
        from fragrance_graph.semantic import CorpusDistributional

        # A miniature corpus with the same structural property as the real
        # one: every fragrance word appears near every other, because
        # people discuss them together.
        corpus = [
            "the rose is lovely and the tobacco is smoky here",
            "rose and tobacco together make a nice woody blend",
            "is it airy or heavy on the skin",
            "quite airy and light and fresh on me",
            "heavy and thick and cloying after an hour",
        ] * 6
        return CorpusDistributional(dim=40).fit(corpus)

    def test_it_does_connect_words_that_share_no_letters(self, trained):
        """The thing it was built for, and it genuinely does it."""
        from fragrance_graph.semantic import cosine

        assert cosine(trained.embed("airy"), trained.embed("light")) > 0.2

    def test_but_it_also_connects_words_that_are_unrelated(self, trained):
        """Rose and tobacco smell nothing alike. They score highly because
        they are discussed in the same sentences."""
        from fragrance_graph.semantic import cosine

        assert cosine(trained.embed("rose"), trained.embed("tobacco")) > 0.2

    def test_an_unrelated_pair_can_outscore_a_related_one(self, trained):
        """The defect in one line. Rose and tobacco are more alike to this
        model than airy and light are, purely because they share more
        sentences — and that ordering is what puts wrong terms above right
        ones in a ranked list.

        Asserted as an ordering rather than a threshold: the real corpus
        put airy/heavy at 0.40, this fixture puts it at 0.18, and pinning a
        number here would be fitting the test to the story rather than to
        the mechanism.
        """
        from fragrance_graph.semantic import cosine

        related = cosine(trained.embed("airy"), trained.embed("light"))
        unrelated = cosine(trained.embed("rose"), trained.embed("tobacco"))
        assert unrelated > related

    def test_it_is_deterministic(self, trained):
        text = "rose"
        assert trained.embed(text) == trained.embed(text)

    def test_an_unknown_word_yields_a_zero_vector_not_a_guess(self, trained):
        assert not any(trained.embed("zzzzqqq"))

    def test_the_default_embedder_is_still_the_lexical_one(self):
        """The measured decision, pinned. `nearest` and `backfill` default
        to the baseline; adopting the other requires passing it."""
        import inspect

        from fragrance_graph.semantic import HashedNGrams, backfill, nearest

        for func in (backfill, nearest):
            source = inspect.getsource(func)
            assert "HashedNGrams()" in source
        assert HashedNGrams().name == "hashed-ngrams-v1"


class TestThePaidArmCannotSkipTheLedger:
    """The real charging tests live in `tests/test_evals_fuzzy.py`; these
    cover the constructor guard and the missing-key path."""

    def test_it_is_named_after_its_model(self):
        from fragrance_graph.semantic import OpenAIEmbeddings

        embedder = OpenAIEmbeddings(on_spend=lambda usd, n: None)
        assert embedder.name.startswith("openai:")

    def test_it_refuses_without_a_key_rather_than_failing_oddly(self, monkeypatch):
        from fragrance_graph.semantic import OpenAIEmbeddings

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
            OpenAIEmbeddings(api_key="", on_spend=lambda usd, n: None).embed("rose")

    def test_a_rejected_key_is_a_message_not_a_traceback(self, monkeypatch):
        """401 is a configuration fact — absent, expired or revoked key —
        and a urllib traceback buries that under thirty lines of internals.
        Nothing is charged, because the callback only fires on success."""
        import urllib.error

        from fragrance_graph.semantic import OpenAIEmbeddings

        def reject(*args, **kwargs):
            raise urllib.error.HTTPError(
                "https://api.openai.com/v1/embeddings", 401, "Unauthorized",
                {}, None,
            )

        monkeypatch.setattr("urllib.request.urlopen", reject)
        charged = []
        embedder = OpenAIEmbeddings(
            api_key="bad", on_spend=lambda usd, n: charged.append(usd)
        )
        with pytest.raises(RuntimeError, match="rejected the API key"):
            embedder.embed("rose")
        assert charged == [], "a rejected request charges nothing"

    def test_another_http_error_is_also_reported_cleanly(self, monkeypatch):
        import urllib.error

        from fragrance_graph.semantic import OpenAIEmbeddings

        def fail(*args, **kwargs):
            raise urllib.error.HTTPError("u", 500, "Server Error", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", fail)
        with pytest.raises(RuntimeError, match="returned 500"):
            OpenAIEmbeddings(api_key="k", on_spend=lambda u, n: None).embed("x")
