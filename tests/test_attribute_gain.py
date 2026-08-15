"""The experiment that must be able to say no.

An earlier run concluded "enrichment scatters" from a page count. These
tests exist so this one cannot reach a conclusion the same way: success is
defined before the run, in the units recommendation consumes, and the
diff names which opinion became agreement rather than only counting.
"""

import json

import pytest

from fragrance_graph.experiments.attribute_gain import (
    COHORT,
    before,
    diff,
)
from fragrance_graph.ingest.store import ingest
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def note(conn, i, *, frag, value, author, channel="chan_a"):
    body = f"comment {i}: {value}"
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
             subject_frag_id, object_kind, raw_object_text, sentiment,
             confidence, evidence_span, evidence_verified, polarity,
             extraction_model, created_at)
        VALUES (%s, 'NOTE_DESCRIPTOR', 'FRAGRANCE', 'it', %s, 'TAG', %s,
                'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (cid, frag, value, body),
    )
    conn.commit()


class TestTheCohort:
    def test_it_spans_the_evidence_range(self):
        """A strategy that only works on the densest bottle is a strategy
        for one bottle. The spread is what makes the result generalise."""
        assert len(COHORT) >= 10

    def test_it_includes_thin_bottles_not_only_rich_ones(self, conn):
        for name in ("Parfums de Marly Layton", "Parfums de Marly Oajan"):
            assert name in COHORT

    def test_a_missing_bottle_is_skipped_not_invented(self, conn):
        add_fragrance(conn, "Parfums de Marly Layton")
        states, _ = before(conn, ("Parfums de Marly Layton", "Nonexistent Bottle"))
        assert [s.name for s in states] == ["Parfums de Marly Layton"]


class TestSuccessIsDefinedBeforeTheRun:
    def test_a_singleton_becoming_repeated_is_the_conversion(self, conn):
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1", channel="c1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        note(conn, 2, frag=frag, value="rose", author="p2", channel="c2")
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.singleton_to_repeated == [["note", "rose"]]
        assert gain.converted == 1

    def test_a_brand_new_singleton_is_not_a_conversion(self, conn):
        """The failure mode the whole metric exists to catch: six hundred
        new one-off descriptors move `facts` and answer nothing."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        note(conn, 1, frag=frag, value="rose", author="p1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        note(conn, 2, frag=frag, value="lychee", author="p2")
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.converted == 0
        assert gain.new_facts == [["note", "lychee"]]

    def test_the_diff_names_which_opinion_became_agreement(self, conn):
        """Counting says the numbers moved. Naming tells a scheduler where
        to spend next."""
        frag = add_fragrance(conn, "Lattafa Khamrah")
        for value in ("rose", "lychee", "powder"):
            note(conn, hash(value) % 900, frag=frag, value=value, author="p1")
        (state_before,) = before(conn, ("Lattafa Khamrah",))[0]

        note(conn, 500, frag=frag, value="lychee", author="p2", channel="c2")
        (state_after,) = before(conn, ("Lattafa Khamrah",))[0]

        gain = diff(state_before, state_after)
        assert gain.singleton_to_repeated == [["note", "lychee"]]

    def test_cost_per_conversion_is_zero_when_nothing_converted(self):
        from fragrance_graph.experiments.attribute_gain import BottleGain

        gain = BottleGain(name="X", fragrance_id=1, usd=0.17)
        assert gain.usd_per_conversion == 0.0

    def test_cost_per_conversion_divides_by_conversions(self):
        from fragrance_graph.experiments.attribute_gain import BottleGain

        gain = BottleGain(
            name="X", fragrance_id=1, usd=0.34,
            singleton_to_repeated=[["note", "a"], ["note", "b"]],
        )
        assert gain.usd_per_conversion == pytest.approx(0.17)


class TestItCannotSpendWithoutTheLedger:
    def test_the_module_builds_no_client(self):
        """A paid module that can build its own client is how the cap has
        been escaped four times."""
        import ast
        import pathlib

        from fragrance_graph.experiments import attribute_gain

        tree = ast.parse(pathlib.Path(attribute_gain.__file__).read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported |= {a.name for a in node.names}
        assert not (imported & {"anthropic", "httpx", "openai", "build_client"})

    def test_before_and_plan_never_reach_a_paid_path(self, conn):
        """They are the free half and must stay free even by accident."""
        import inspect

        from fragrance_graph.experiments import attribute_gain

        source = inspect.getsource(attribute_gain.before)
        assert "Budget" not in source
        assert "extract" not in source
