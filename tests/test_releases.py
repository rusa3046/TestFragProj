"""From "a bottle exists" to "people have said things about it".

Most of this file guards one boundary: an announcement is discovery
provenance and never smell evidence. A press release saying "notes of
raspberry" must not be able to become "people say this smells of
raspberry", by any route, at any later date.

The lifecycle test at the bottom drives the real production functions —
`discover`, `verify`, `probe`, `mark_evidenced` — with a fixture source
and an injected searcher. There is no second, mocked lifecycle; a fixture
that took a different code path would prove nothing about the real one.
"""

import json

import pytest

from fragrance_graph.ingest.store import ingest
from fragrance_graph.releases import (
    READY_CREATORS,
    FixtureSource,
    ReleaseItem,
    Status,
    discover,
    mark_evidenced,
    probe,
    verify,
)
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


@pytest.fixture
def fixture_file(tmp_path):
    def write(*rows):
        path = tmp_path / "announcements.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return FixtureSource(path)
    return write


class Hit:
    """What `frontier.search_with_creators` returns, minus the network."""

    def __init__(self, video_id, channel_id):
        self.video_id = video_id
        self.channel_id = channel_id


def searcher(*channels):
    """A searcher returning one video per named channel."""
    def search(query):
        return [Hit(f"v{i}", c) for i, c in enumerate(channels)]
    return search


def claim_about(conn, i, frag, value="rose"):
    body = f"comment {i}: {value}"
    ingest(conn, [make_comment(i, body=body, raw_json=json.dumps(
        {"author": f"p{i}", "videoId": "v1"}))])
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


# --- the boundary ----------------------------------------------------------


class TestAnAnnouncementIsNotEvidence:
    """Invariants 1, 2 and 3."""

    def test_discovery_writes_no_claims(self, conn, fixture_file):
        source = fixture_file({
            "source_id": "a1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0

    def test_a_release_row_carries_no_note_list(self, conn, fixture_file):
        """`ReleaseItem` has nowhere to put one. A stored note list is a
        note list somebody eventually joins to."""
        assert not hasattr(ReleaseItem("a", "b"), "notes")
        columns = {
            row["column_name"]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = 'release_candidates'"
            )
        }
        assert not columns & {"notes", "accords", "description", "note_list"}

    def test_an_announced_note_cannot_become_a_perception(self, conn, fixture_file):
        """Invariant 3. The source says raspberry; the corpus must still
        say nothing about raspberry."""
        from fragrance_graph.evidence import attribute_facts

        source = fixture_file({
            "source_id": "a1", "name": "Raspberry Dream", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        frag = conn.execute(
            "SELECT fragrance_id FROM release_candidates WHERE source_id = 'a1'"
        ).fetchone()[0]
        assert attribute_facts(conn, fragrance_id=frag) == []

    def test_a_retailer_row_cannot_become_a_community_claim(self, conn, fixture_file):
        """Invariant 2. Same path, different source_type — the boundary is
        a property of the table, not of which adapter wrote it."""
        source = fixture_file({
            "source_id": "sku-1", "name": "Amber Oud Gold", "brand": "Al Haramain",
            "announced_at": "2026-08-01",
        })
        source.source_type = "retailer_feed"
        discover(conn, source)
        verify(conn)
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0

    def test_a_release_never_produces_a_canonical_fact(self, conn, fixture_file):
        """Invariant 4. Canonical facts need an authorized note list. A
        product name is not one, and neither is an announcement."""
        from fragrance_graph.evidence import Strength, name_facts

        source = fixture_file({
            "source_id": "a1", "name": "Rose Absolue", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        for fact in name_facts(conn):
            assert fact.strength is Strength.INSUFFICIENT
            assert not fact.may_declare


# --- resolution ------------------------------------------------------------


class TestResolvingAnnouncements:
    """Invariants 7, 8 and 9."""

    def test_two_sources_for_one_launch_make_one_bottle(self, conn, fixture_file):
        """Invariant 7. Split a launch across two rows and every count
        downstream is halved."""
        source = fixture_file(
            {"source_id": "rss-1", "name": "Delina La Rosee",
             "brand": "Parfums de Marly", "announced_at": "2026-08-01"},
            {"source_id": "shop-9", "name": "Delina La Rosée",
             "brand": "Parfums de Marly", "announced_at": "2026-08-02"},
        )
        discover(conn, source)
        verify(conn)
        ids = {
            row["fragrance_id"]
            for row in conn.execute("SELECT fragrance_id FROM release_candidates")
        }
        assert len(ids) == 1

    def test_an_announcement_of_a_known_bottle_merges(self, conn, fixture_file):
        existing = add_fragrance(conn, "Parfums de Marly Layton",
                                 brand="Parfums de Marly", aliases=["Layton"])
        source = fixture_file({
            "source_id": "rss-1", "name": "Layton", "brand": "Parfums de Marly",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        row = conn.execute("SELECT * FROM release_candidates").fetchone()
        assert row["fragrance_id"] == existing
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == 1, "no second node for a bottle we already have"

    def test_an_ambiguous_name_stays_unresolved(self, conn, fixture_file):
        """Invariant 9. "Amber Oud" names a dozen bottles across a dozen
        houses. Choosing one invents a fact about a product."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Amber Oud", "brand": "",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        row = conn.execute("SELECT * FROM release_candidates").fetchone()
        assert row["status"] == Status.DISCOVERED
        assert row["fragrance_id"] is None
        assert "cannot tell which product" in row["status_reason"]

    def test_rediscovery_is_idempotent(self, conn, fixture_file):
        """Invariant 8. Feeds repeat themselves for weeks."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        discover(conn, source)
        discover(conn, source)
        assert conn.execute(
            "SELECT count(*) FROM release_candidates"
        ).fetchone()[0] == 1

    def test_reverification_creates_no_duplicates(self, conn, fixture_file):
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        verify(conn)
        assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 1

    def test_malformed_names_never_enter(self, conn, fixture_file):
        source = fixture_file({
            "source_id": "rss-1", "name": "??", "brand": "",
            "announced_at": "2026-08-01",
        })
        stats = discover(conn, source)
        assert stats.rejected == 1
        assert conn.execute(
            "SELECT count(*) FROM release_candidates"
        ).fetchone()[0] == 0


# --- spending --------------------------------------------------------------


class TestNothingExpensiveHappensOnAnnouncement:
    """Invariants 5, 6 and 10."""

    def test_a_new_release_is_not_immediately_enrichable(self, conn, fixture_file):
        """Invariant 5. On announcement day there are no comments; paying
        to read them buys silence."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        row = conn.execute("SELECT status FROM release_candidates").fetchone()
        assert row["status"] == Status.CATALOGED
        assert row["status"] != Status.ENRICHABLE

    def test_readiness_counts_creators_not_videos(self, conn, fixture_file):
        """Invariant 6. Eight videos from one channel is one room."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        (readiness,) = probe(conn, searcher("chan_a", "chan_a", "chan_a",
                                            "chan_a", "chan_a"))
        assert readiness.videos == 5
        assert readiness.creators == 1
        assert not readiness.ready
        assert readiness.status is Status.WAITING_FOR_DISCUSSION

    def test_enough_separate_creators_makes_it_enrichable(self, conn, fixture_file):
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        channels = [f"chan_{i}" for i in range(READY_CREATORS)]
        (readiness,) = probe(conn, searcher(*channels))
        assert readiness.ready
        assert readiness.status is Status.ENRICHABLE

    def test_no_discussion_is_not_evidence_of_a_bad_fragrance(
        self, conn, fixture_file
    ):
        """Invariant 10. Silence means nobody has spoken, not that the
        bottle is poor — so the row waits rather than being rejected."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        probe(conn, searcher())
        row = conn.execute("SELECT * FROM release_candidates").fetchone()
        assert row["status"] == Status.WAITING_FOR_DISCUSSION
        assert row["status"] != Status.REJECTED
        assert row["next_probe_at"], "and it is scheduled to be asked again"

    def test_probing_backs_off(self, conn, fixture_file):
        """Probing daily forever spends quota to learn the same thing."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        probe(conn, searcher(), now="2030-01-01T00:00:00+00:00")
        first = conn.execute(
            "SELECT next_probe_at, probe_count FROM release_candidates"
        ).fetchone()
        assert first["probe_count"] == 1
        # The next probe is in the future, so a second run finds nothing due.
        assert probe(conn, searcher()) == []


# --- the whole thing -------------------------------------------------------


class TestTheLifecycleEndToEnd:
    """Every state, driven through the production functions."""

    def test_discovered_to_evidenced(self, conn, fixture_file):
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "url": "https://news.test/khamrah-rouge",
            "announced_at": "2026-08-01", "release_date": "2026-09-15",
        })

        def status():
            return conn.execute(
                "SELECT status FROM release_candidates"
            ).fetchone()[0]

        # 1. A source mentions it. Costs nothing.
        discover(conn, source)
        assert status() == Status.DISCOVERED

        # 2 & 3. It resolves to a real, distinct product and gets a row.
        verify(conn)
        assert status() == Status.CATALOGED
        frag = conn.execute(
            "SELECT fragrance_id FROM release_candidates"
        ).fetchone()[0]
        assert frag is not None

        # 4. Nobody is talking yet. Quota spent, no money.
        probe(conn, searcher("chan_a"))
        assert status() == Status.WAITING_FOR_DISCUSSION

        # 5. Later, enough separate creators are.
        channels = [f"chan_{i}" for i in range(READY_CREATORS)]
        probe(conn, searcher(*channels), now="2030-01-01T00:00:00+00:00")
        assert status() == Status.ENRICHABLE

        # 6. Enrichment runs and claims attach.
        for i in range(3):
            claim_about(conn, i, frag, value="dates")
        assert mark_evidenced(conn) == 1
        assert status() == Status.EVIDENCED

        # 7. And the bottle is now queryable like any other.
        from fragrance_graph.recommend import recommend

        answer = recommend(conn, "a fragrance with dates")
        assert "Lattafa Khamrah Rouge" in [r.name for r in answer.results]

    def test_a_bottle_with_no_claims_never_reaches_evidenced(
        self, conn, fixture_file
    ):
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        assert mark_evidenced(conn) == 0

    def test_a_fixture_source_takes_the_same_path_as_any_other(self):
        """No branch anywhere asks whether the source is a fixture. That is
        the only thing that makes the fixture prove something about the
        production adapters that will replace it."""
        import inspect

        from fragrance_graph import releases

        for name in ("discover", "verify", "probe", "mark_evidenced"):
            body = inspect.getsource(getattr(releases, name))
            assert "fixture" not in body.lower(), (
                f"{name} special-cases the fixture source"
            )
