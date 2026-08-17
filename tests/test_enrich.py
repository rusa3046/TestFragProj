"""Two jobs that both buy comments, scored differently on purpose.

The failure this file guards against is the one that produced "1 page from
9 bottles and $1.09": scoring a learn-about run with the pair gate's
metric, concluding enrichment does not work, and stopping.
"""

import json
import pathlib

import pytest

from fragrance_graph.enrich import (
    InformationGain,
    Mode,
    PairOutcome,
    learn_about,
    near_misses,
    verify_comparison,
)
from fragrance_graph.ingest.store import ingest
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def claim(conn, i, *, subject, obj=None, tag=None, author, channel="chan_a",
          claim_type="NOTE_DESCRIPTOR"):
    body = f"comment {i}"
    ingest(conn, [make_comment(i, body=body, source_channel=channel,
                               raw_json=json.dumps({"author": author,
                                                    "videoId": "v1"}))])
    cid = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             polarity, extraction_model, created_at)
        VALUES (%s, %s, 'FRAGRANCE', 'it', %s, %s, %s, %s, 'POSITIVE', 0.9,
                %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, claim_type, subject,
         "FRAGRANCE" if obj else ("TAG" if tag else "NONE"),
         "other" if obj else tag, obj, body),
    )
    conn.commit()


@pytest.fixture
def bottles(conn):
    return (
        conn,
        add_fragrance(conn, "Parfums de Marly Delina"),
        add_fragrance(conn, "Lattafa Khamrah"),
    )


class TestTheTwoJobsAreScoredDifferently:
    def test_a_learn_run_succeeds_on_knowledge_not_pages(self, bottles):
        """The correction to the 2026-08-15 conclusion. Forty attribute
        facts and no page is a successful learn run."""
        gain = InformationGain(
            fragrance_id=1, name="X", new_facts=40, new_declarable=0,
            new_descriptors=12,
        )
        assert gain.succeeded

    def test_a_learn_run_is_not_held_to_the_consensus_bar(self):
        """Requiring `new_declarable` is exactly the mistake that made
        bottle enrichment look worthless."""
        assert InformationGain(fragrance_id=1, new_facts=5).succeeded

    def test_a_verify_run_fails_if_the_pair_did_not_move(self):
        """Forty attribute facts do not settle the pair it was sent to
        settle."""
        outcome = PairOutcome(a_id=1, b_id=2, people_before=2, people_after=2,
                              creators_before=1, creators_after=1)
        assert not outcome.succeeded
        assert outcome.verdict == "still insufficient"

    def test_a_verify_run_succeeds_when_the_pair_clears_the_gate(self):
        outcome = PairOutcome(a_id=1, b_id=2, people_before=2, people_after=3,
                              creators_before=1, creators_after=2)
        assert outcome.succeeded
        assert outcome.verdict == "supported"

    def test_getting_closer_is_reported_as_its_own_outcome(self):
        outcome = PairOutcome(a_id=1, b_id=2, people_before=1, people_after=2,
                              creators_before=1, creators_after=2)
        assert not outcome.succeeded
        assert "closer" in outcome.verdict

    def test_still_insufficient_is_a_finding_not_a_failure(self):
        """A pair that stays at two people after a targeted search means
        the comparison is not repeated elsewhere — which is what the gate
        exists to detect."""
        outcome = PairOutcome(a_id=1, b_id=2, people_before=2, people_after=2,
                              creators_before=2, creators_after=2)
        assert outcome.verdict == "still insufficient"

    def test_the_modes_are_named_separately(self):
        assert Mode.LEARN != Mode.VERIFY


class TestLearnAbout:
    def test_it_searches_broadly_not_for_dupes(self, bottles):
        """The corpus is already dupe-shaped; asking for dupes deepens the
        pair graph and teaches nothing about what a bottle smells like."""
        conn, delina, _ = bottles
        seen = {}

        def run(query):
            seen["query"] = query
            return (0, 0.0)

        learn_about(conn, delina, name="Delina", run=run)
        assert "review" in seen["query"]
        assert "dupe" not in seen["query"]
        assert "vs" not in seen["query"]

    def test_it_counts_facts_that_did_not_exist_before(self, bottles):
        conn, delina, _ = bottles
        claim(conn, 1, subject=delina, tag="rose", author="p1")

        def run(query):
            claim(conn, 2, subject=delina, tag="lychee", author="p2")
            claim(conn, 3, subject=delina, tag="powdery", author="p3")
            return (40, 0.02)

        gain = learn_about(conn, delina, name="Delina", run=run)
        assert gain.new_facts == 2
        assert gain.new_descriptors == 2
        assert gain.succeeded

    def test_a_run_that_learns_nothing_says_so(self, bottles):
        conn, delina, _ = bottles
        claim(conn, 1, subject=delina, tag="rose", author="p1")
        gain = learn_about(conn, delina, name="Delina",
                           run=lambda query: (40, 0.02))
        assert gain.new_facts == 0
        assert not gain.succeeded

    def test_it_notices_a_fact_becoming_declarable(self, bottles):
        """The most valuable single outcome: an observation becoming
        something the product may state."""
        conn, delina, _ = bottles
        claim(conn, 1, subject=delina, tag="rose", author="p1", channel="c1")

        def run(query):
            claim(conn, 2, subject=delina, tag="rose", author="p2", channel="c2")
            claim(conn, 3, subject=delina, tag="rose", author="p3", channel="c3")
            return (40, 0.02)

        gain = learn_about(conn, delina, name="Delina", run=run)
        assert gain.new_declarable == 1

    def test_it_measures_facts_not_claim_volume(self, bottles):
        """200 claims repeating one person are worth less than 12 reaching
        three, and the metric has to know that."""
        conn, delina, _ = bottles

        def run(query):
            for i in range(2, 22):
                claim(conn, i, subject=delina, tag="rose", author="p1")
            return (200, 0.10)

        gain = learn_about(conn, delina, name="Delina", run=run)
        assert gain.new_facts == 1, "twenty claims, one fact, one person"


class TestVerifyComparison:
    def test_it_searches_the_pair_specifically(self, bottles):
        conn, delina, khamrah = bottles
        seen = {}

        def run(query):
            seen["query"] = query
            return (0, 0.0)

        verify_comparison(conn, delina, khamrah, a_name="Delina",
                          b_name="Khamrah", run=run)
        assert "Delina" in seen["query"] and "Khamrah" in seen["query"]

    def test_it_reports_the_pair_moving(self, bottles):
        conn, delina, khamrah = bottles
        claim(conn, 1, subject=khamrah, obj=delina, author="p1",
              channel="c1", claim_type="SIMILAR_TO")

        def run(query):
            claim(conn, 2, subject=khamrah, obj=delina, author="p2",
                  channel="c2", claim_type="SIMILAR_TO")
            claim(conn, 3, subject=khamrah, obj=delina, author="p3",
                  channel="c3", claim_type="SIMILAR_TO")
            return (30, 0.015)

        outcome = verify_comparison(conn, delina, khamrah, a_name="Delina",
                                    b_name="Khamrah", run=run)
        assert outcome.people_before == 1
        assert outcome.people_after == 3
        assert outcome.succeeded

    def test_attribute_facts_do_not_settle_a_pair(self, bottles):
        """The distinction the whole module exists for."""
        conn, delina, khamrah = bottles

        def run(query):
            for i in range(2, 12):
                claim(conn, i, subject=khamrah, tag=f"note{i}", author=f"p{i}",
                      channel=f"c{i}")
            return (60, 0.03)

        outcome = verify_comparison(conn, delina, khamrah, a_name="Delina",
                                    b_name="Khamrah", run=run)
        assert not outcome.succeeded, "ten new facts, zero pair evidence"


class TestNearMisses:
    def test_it_finds_pairs_short_of_the_gate(self, bottles):
        conn, delina, khamrah = bottles
        for i, (author, chan) in enumerate([("p1", "c1"), ("p2", "c2")]):
            claim(conn, i, subject=khamrah, obj=delina, author=author,
                  channel=chan, claim_type="SIMILAR_TO")
        (miss,) = near_misses(conn)
        assert miss.people == 2
        assert "1 more person" in miss.needs

    def test_a_published_pair_is_not_a_near_miss(self, bottles):
        conn, delina, khamrah = bottles
        for i, (author, chan) in enumerate(
            [("p1", "c1"), ("p2", "c2"), ("p3", "c3")]
        ):
            claim(conn, i, subject=khamrah, obj=delina, author=author,
                  channel=chan, claim_type="SIMILAR_TO")
        assert near_misses(conn) == []


class TestNeitherJobCanRouteAroundTheCap:
    def test_no_module_level_client_exists(self):
        """A module that can build its own client can route around
        `budget.guard`, which has already been defeated twice.

        Checked against the parsed imports rather than the file text, so
        the docstring explaining the rule does not trip the test enforcing
        it."""
        import ast

        import fragrance_graph.enrich as module

        tree = ast.parse(pathlib.Path(module.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported |= {a.name for a in node.names}
        forbidden = {"anthropic", "build_client", "httpx", "openai"}
        assert not (imported & forbidden), (
            f"enrich.py can build its own client: {imported & forbidden}"
        )

    def test_spending_is_injected_not_imported(self, bottles):
        """Both entry points take the spending callable as an argument, so
        a caller without one cannot spend anything."""
        import inspect

        for func in (learn_about, verify_comparison):
            assert "run" in inspect.signature(func).parameters


class TestCodexPhase78Findings:
    """Three findings from the 2026-08-15 enrichment/scheduler review."""

    def test_three_people_in_one_room_is_a_near_miss(self, bottles):
        """P2. The head-count bound hid the most targetable case of all:
        three people agreeing inside one creator's section, which needs one
        more channel and was invisible to the work list."""
        conn, delina, khamrah = bottles
        for i, author in enumerate(["p1", "p2", "p3"]):
            claim(conn, i, subject=khamrah, obj=delina, author=author,
                  channel="one_room", claim_type="SIMILAR_TO")
        (miss,) = near_misses(conn)
        assert (miss.people, miss.creators) == (3, 1)
        assert "creator" in miss.needs

    def test_a_published_pair_is_still_excluded(self, bottles):
        """The widened query must not start returning pairs that already
        publish."""
        conn, delina, khamrah = bottles
        for i, (author, chan) in enumerate(
            [("p1", "c1"), ("p2", "c2"), ("p3", "c3")]
        ):
            claim(conn, i, subject=khamrah, obj=delina, author=author,
                  channel=chan, claim_type="SIMILAR_TO")
        assert near_misses(conn) == []

    def test_denials_alone_are_not_new_knowledge(self, bottles):
        """P2. A fact with only opposing evidence records that people
        denied something. That is worth keeping and is not gain."""
        conn, delina, _ = bottles

        def run(query):
            for i, author in enumerate(["p1", "p2"], start=5):
                claim(conn, i, subject=delina, tag="rose", author=author,
                      channel=f"c{i}")
                conn.execute(
                    "UPDATE claims SET polarity = 'DENIED' "
                    "WHERE id = (SELECT max(id) FROM claims)"
                )
            conn.commit()
            return (40, 0.02)

        gain = learn_about(conn, delina, name="Delina", run=run)
        assert gain.new_facts == 0
        assert not gain.succeeded

    def test_inferred_only_facts_are_counted_apart(self, bottles):
        """A fact resting on unreviewed machine attribution is knowledge
        about a claim, not yet about a bottle."""
        conn, delina, _ = bottles

        def run(query):
            claim(conn, 5, subject=None, tag="lychee", author="p5")
            conn.execute(
                "INSERT INTO claim_attributions (claim_id, role, fragrance_id,"
                " method, evidence, confidence, review_status, created_at)"
                " VALUES ((SELECT max(id) FROM claims), 'subject', %s,"
                " 'video_subject', 'title', 0.95, 'proposed', '2026-01-01')",
                (delina,),
            )
            conn.commit()
            return (40, 0.02)

        gain = learn_about(conn, delina, name="Delina", run=run)
        assert gain.new_facts == 0, "not advertised as checked knowledge"
        assert gain.new_inferred_facts == 1, "but recorded"
