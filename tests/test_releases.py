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
from pathlib import Path

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
    seed_wikidata,
    verify,
)
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment

WIKIDATA_RELEASES = Path("data/curation/wikidata-releases.jsonl")


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


def claim_about(conn, i, frag, value="rose", channel="fragrance"):
    body = f"comment {i}: {value}"
    ingest(conn, [make_comment(i, body=body, source_channel=channel,
                               raw_json=json.dumps(
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

    def test_the_bare_name_fallback_does_not_cross_houses(self, conn, fixture_file):
        """Reviewer finding 2, reproduced exactly. `best_match(full_name)`
        fails ("Guerlain Vetiver" matches nothing), and the old bare-name
        fallback then matched on "Vetiver" alone with no brand check at
        all — merging Guerlain's announcement onto Molinard's catalogued
        bottle, aliased "Vetiver". "Vetiver", "Dune", "Obsession",
        "Aventus" and "Mitsouko" are all names more than one real house
        uses; a bare-name match with no brand check will hit one of them
        as the catalogue grows."""
        molinard = add_fragrance(
            conn, "Molinard Vetiver", brand="Molinard", aliases=["Vetiver"]
        )
        source = fixture_file({
            "source_id": "rss-1", "name": "Vetiver", "brand": "Guerlain",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)

        row = conn.execute("SELECT * FROM release_candidates").fetchone()
        assert row["fragrance_id"] != molinard, "must not link across houses"
        assert row["status"] == Status.CATALOGED
        new_name = conn.execute(
            "SELECT canonical_name FROM fragrances WHERE id = %s",
            (row["fragrance_id"],),
        ).fetchone()[0]
        assert new_name == "Guerlain Vetiver"
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == 2, "Guerlain's Vetiver is its own node, not a merge"

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

        # 6. Enrichment runs and claims attach, from separate creators —
        #    EVIDENCED holds the same independence bar readiness does.
        for i in range(3):
            claim_about(conn, i, frag, value="dates", channel=f"chan_{i}")
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


class TestCodexPhase6ReleaseFindings:
    """Two P2s from the 2026-08-15 review, both confirmed."""

    def test_a_concentration_suffix_is_not_a_new_product(self, conn, fixture_file):
        """`best_match` compares whole strings, so "Khamrah Rouge" against
        "Khamrah Rouge EDP" can fall below the fuzzy threshold and quietly
        create a duplicate. Every count the launch earns then splits."""
        source = fixture_file(
            {"source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
             "announced_at": "2026-08-01"},
            {"source_id": "shop-2", "name": "Khamrah Rouge Eau de Parfum",
             "brand": "Lattafa", "announced_at": "2026-08-02"},
        )
        discover(conn, source)
        verify(conn)
        assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 1
        ids = {
            row["fragrance_id"]
            for row in conn.execute("SELECT fragrance_id FROM release_candidates")
        }
        assert len(ids) == 1

    def test_a_real_flanker_is_still_its_own_product(self, conn, fixture_file):
        """The fix must not merge bottles that genuinely smell different.
        "Elixir" and "Intense" name real flankers and are deliberately not
        treated as concentration words."""
        source = fixture_file(
            {"source_id": "rss-1", "name": "Khamrah", "brand": "Lattafa",
             "announced_at": "2026-08-01"},
            {"source_id": "rss-2", "name": "Khamrah Elixir", "brand": "Lattafa",
             "announced_at": "2026-08-02"},
        )
        discover(conn, source)
        verify(conn)
        assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 2

    def test_one_stray_claim_does_not_make_a_release_evidenced(
        self, conn, fixture_file
    ):
        """A row could jump CATALOGED -> EVIDENCED on a single claim, which
        made the status useless as proof the readiness gate ever ran."""
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        frag = conn.execute(
            "SELECT fragrance_id FROM release_candidates"
        ).fetchone()[0]
        claim_about(conn, 1, frag)
        assert mark_evidenced(conn) == 0
        assert conn.execute(
            "SELECT status FROM release_candidates"
        ).fetchone()[0] == Status.CATALOGED

    def test_claims_from_enough_creators_do(self, conn, fixture_file):
        source = fixture_file({
            "source_id": "rss-1", "name": "Khamrah Rouge", "brand": "Lattafa",
            "announced_at": "2026-08-01",
        })
        discover(conn, source)
        verify(conn)
        frag = conn.execute(
            "SELECT fragrance_id FROM release_candidates"
        ).fetchone()[0]
        for i in range(READY_CREATORS):
            claim_about(conn, i, frag, channel=f"chan_{i}")
        assert mark_evidenced(conn) == 1


# --- seeding from Wikidata ---------------------------------------------------


class TestSeedWikidata:
    """A one-time CC0 batch, not a feed — but it seeds into DISCOVERED, the
    same state `discover()` leaves a feed item in, and leaves resolution
    entirely to `verify()`. An earlier version matched against the
    catalogue itself and wrote CATALOGED/VERIFIED directly; see
    `seed_wikidata`'s docstring for why that duplicated — worse — what
    `verify()` already does, and why VERIFIED was a dead end nothing ever
    read back out of.
    """

    def test_it_inserts_every_well_formed_row_as_discovered(self, conn):
        stats = seed_wikidata(conn, WIKIDATA_RELEASES)
        assert stats.seen == 70
        assert stats.inserted == 69  # one row's name is a bare Wikidata QID
        assert stats.skipped == 1
        statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT DISTINCT status FROM release_candidates"
                " WHERE source_type = 'wikidata'"
            )
        }
        assert statuses == {"DISCOVERED"}

    def test_a_bare_wikidata_qid_name_is_skipped_not_seeded(self, conn):
        """The committed file has one row whose product name is a raw
        Wikidata entity id (house Chanel) — the batch's own matching
        failed to find a real name, and the id leaked into the field
        meant for one. Seeding it unchecked would let `verify()`'s
        auto-catalogue path create a fragrance literally named
        "Chanel Q3697345"."""
        seed_wikidata(conn, WIKIDATA_RELEASES)
        assert conn.execute(
            "SELECT count(*) FROM release_candidates WHERE name = 'Q3697345'"
        ).fetchone()[0] == 0

    def test_reseeding_is_a_no_op(self, conn):
        seed_wikidata(conn, WIKIDATA_RELEASES)
        again = seed_wikidata(conn, WIKIDATA_RELEASES)

        assert again.inserted == 0
        assert again.duplicate == 69
        assert conn.execute(
            "SELECT count(*) FROM release_candidates"
        ).fetchone()[0] == 69

    def test_a_status_change_survives_reseeding(self, conn):
        """After the first insert the lifecycle owns the row. A re-seed
        must not silently rewind a curator's — or `verify`'s — decision
        back to the Wikidata snapshot."""
        seed_wikidata(conn, WIKIDATA_RELEASES)
        conn.execute(
            "UPDATE release_candidates SET status = 'REJECTED'"
            " WHERE source_type = 'wikidata'"
        )
        conn.commit()

        seed_wikidata(conn, WIKIDATA_RELEASES)

        statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT DISTINCT status FROM release_candidates"
                " WHERE source_type = 'wikidata'"
            )
        }
        assert statuses == {"REJECTED"}

    def test_status_reason_names_the_licence(self, conn):
        seed_wikidata(conn, WIKIDATA_RELEASES)
        reason = conn.execute(
            "SELECT status_reason FROM release_candidates LIMIT 1"
        ).fetchone()[0]
        assert "Wikidata" in reason
        assert "CC0" in reason

    def test_it_round_trips_through_the_corpus(self, conn, tmp_path, second_db):
        """`release_candidates` already exports/imports via `corpus.py` — a
        seeded row must survive that the same way any other candidate
        does, `source_type` included."""
        from fragrance_graph.corpus import export_corpus, import_corpus
        from fragrance_graph.db import get_connection, migrate

        seed_wikidata(conn, WIKIDATA_RELEASES)
        export_corpus(conn, tmp_path)

        fresh = get_connection(second_db)
        migrate(fresh)
        import_corpus(fresh, tmp_path)

        before = conn.execute(
            "SELECT source_type, source_id, status FROM release_candidates"
            " WHERE source_id = 'Q140499690'"
        ).fetchone()
        after = fresh.execute(
            "SELECT source_type, source_id, status FROM release_candidates"
            " WHERE source_id = 'Q140499690'"
        ).fetchone()
        assert dict(after) == dict(before)
        assert after["source_type"] == "wikidata"
        assert after["status"] == "DISCOVERED"
        fresh.close()


# --- seeding, then resolving through the real lifecycle ---------------------


class TestSeedThenVerify:
    """Resolution is `verify()`'s job, not the seeder's. This is the exact
    two-step sequence the CLI recommends (`seed-wikidata`, then `verify`),
    driven through both real functions rather than asserted against a
    stub — see `seed_wikidata`'s docstring for why the seeder no longer
    does any matching of its own.
    """

    def test_an_already_catalogued_bottle_gets_merged(self, conn):
        """R0038 in the real file: Lattafa Khamrah, wikidata_qid
        Q140499690. A bottle already in the catalogue must not sit next to
        it unresolved, and merging must not create a second node for it."""
        add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa", aliases=["Khamrah"])

        seed_wikidata(conn, WIKIDATA_RELEASES)
        verify(conn)

        row = conn.execute(
            "SELECT r.status, f.canonical_name FROM release_candidates r"
            " JOIN fragrances f ON f.id = r.fragrance_id"
            " WHERE r.source_id = 'Q140499690'"
        ).fetchone()
        assert row["status"] == "CATALOGED"
        assert row["canonical_name"] == "Lattafa Khamrah"
        assert conn.execute(
            "SELECT count(*) FROM fragrances WHERE canonical_name = 'Lattafa Khamrah'"
        ).fetchone()[0] == 1

    def test_well_formed_new_releases_get_cataloged_through_the_real_lifecycle(
        self, conn
    ):
        """Into an empty catalogue every seeded, well-formed release is
        new — `verify` auto-catalogues each one with its brand and alias
        recorded. This is the real behaviour change fix 3 buys: these 69
        rows now actually enter the machine described in the module
        docstring, rather than sitting inert at VERIFIED forever.

        60 distinct fragrances rather than 69, measured against the real
        file: three (house, name) pairs are seeded twice
        (Calvin Klein/Eternity for Men, Mugler/Angel Nova, Mugler/Angel
        Muse) and the rest of the gap is `verify`'s own dedup machinery
        (identity matching, fuzzy full-name matching) correctly merging
        near-duplicates within the batch itself — not a defect in either
        seeding or verification."""
        seed_wikidata(conn, WIKIDATA_RELEASES)
        stats = verify(conn)

        assert stats.verified == 69
        assert stats.unresolved == 0
        statuses = {
            row["status"]
            for row in conn.execute(
                "SELECT DISTINCT status FROM release_candidates"
                " WHERE source_type = 'wikidata'"
            )
        }
        assert statuses == {"CATALOGED"}
        assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 60
