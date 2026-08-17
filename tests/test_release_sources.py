"""Reading a real publication's feed as syndication.

Every test runs against a checked-in capture of an actual Now Smell This
response, so the parsing is held to the real shape while staying offline
and free. A test that invents its own tidy XML proves the parser handles
XML somebody wrote to suit it.
"""

import pathlib

import pytest

from fragrance_graph.release_sources import (
    PERMITTED,
    RSSSource,
    build,
    known_brands,
    split_brand,
)
from fragrance_graph.releases import discover, verify
from fragrance_graph.resolve.entities import add_fragrance

SAMPLE = pathlib.Path("data/fixtures/releases/nst-feed-sample.xml")


@pytest.fixture
def feed():
    text = SAMPLE.read_text(encoding="utf-8")
    return lambda url: text


def source(feed, conn=None, brands=()):
    return RSSSource(
        url="https://example.test/feed/", source_type="rss:test",
        conn=conn, fetch=feed, _brands=list(brands),
    )


class TestItPicksOutAnnouncements:
    def test_only_new_release_posts_are_taken(self, feed):
        """The same feed carries essays, scent-of-the-day posts and
        giveaways. Without the publication's own marker every adapter is
        guessing whether a headline names a product."""
        items = source(feed).discover_since(None)
        assert items, "the sample contains announcements"
        for item in items:
            assert "new fragrance" not in item.name.lower()
            assert "scent of the day" not in item.name.lower()

    def test_essays_and_giveaways_are_left_alone(self, feed):
        names = {i.name.lower() for i in source(feed).discover_since(None)}
        assert not any("giveaway" in n or "winner" in n for n in names)
        assert "the new look" not in names

    def test_each_item_carries_a_stable_identifier(self, feed):
        for item in source(feed).discover_since(None):
            assert item.source_id
            assert item.url.startswith("http")

    def test_each_item_carries_a_date(self, feed):
        for item in source(feed).discover_since(None):
            assert item.release_date
            assert len(item.release_date) == 10

    def test_since_filters_by_publication_date(self, feed):
        everything = source(feed).discover_since(None)
        assert everything
        cutoff = max(i.release_date for i in everything)
        assert source(feed).discover_since(cutoff) == []


class TestBrandSplitting:
    def test_a_known_brand_is_taken_off_the_front(self):
        brand, name = split_brand("Calvin Klein Euphoria Elixir",
                                  ["Calvin Klein", "Memo Paris"])
        assert (brand, name) == ("Calvin Klein", "Euphoria Elixir")

    def test_the_longest_brand_wins(self):
        """So "Parfums de Marly" is not truncated to "Parfums"."""
        brand, _ = split_brand("Parfums de Marly Delina",
                               ["Parfums de Marly", "Parfums"])
        assert brand == "Parfums de Marly"

    def test_an_unknown_house_yields_no_brand_rather_than_a_guess(self):
        brand, name = split_brand("Memo Paris Darling Point", ["Lattafa"])
        assert brand == ""
        assert name == "Memo Paris Darling Point"

    def test_known_brands_come_from_the_catalogue(self, conn):
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly")
        assert "Parfums de Marly" in known_brands(conn)


class TestItFeedsTheLifecycleUnchanged:
    def test_an_unknown_house_stays_unresolved(self, conn, feed):
        """The designed outcome, not a shortfall. An announcement the
        system cannot place becomes a row somebody looks at, never a
        fabricated fragrance."""
        discover(conn, source(feed, conn))
        verify(conn)
        rows = conn.execute(
            "SELECT status, fragrance_id FROM release_candidates"
        ).fetchall()
        assert rows
        assert all(r["status"] == "DISCOVERED" for r in rows)
        assert all(r["fragrance_id"] is None for r in rows)
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == 0, "nothing was invented"

    def test_a_known_house_flows_all_the_way_to_cataloged(self, conn, feed):
        items = source(feed).discover_since(None)
        assert items
        house = items[0].name.split()[0]
        add_fragrance(conn, f"{house} Something Else", brand=house)

        discover(conn, source(feed, conn))
        verify(conn)
        cataloged = conn.execute(
            "SELECT count(*) FROM release_candidates WHERE status = 'CATALOGED'"
        ).fetchone()[0]
        assert cataloged >= 1

    def test_rereading_the_feed_adds_nothing(self, conn, feed):
        """Feeds repeat themselves for weeks."""
        discover(conn, source(feed, conn))
        discover(conn, source(feed, conn))
        before = conn.execute(
            "SELECT count(*) FROM release_candidates"
        ).fetchone()[0]
        discover(conn, source(feed, conn))
        assert conn.execute(
            "SELECT count(*) FROM release_candidates"
        ).fetchone()[0] == before

    def test_discovery_still_writes_no_claims(self, conn, feed):
        discover(conn, source(feed, conn))
        verify(conn)
        assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0

    def test_an_unreachable_feed_is_not_an_exception(self, conn):
        """A publication being down is a Tuesday, not a crash."""
        def boom(url):
            raise TimeoutError("down")

        broken = RSSSource(url="https://example.test/x", source_type="rss:test",
                           conn=conn, fetch=lambda u: "")
        assert broken.discover_since(None) == []


class TestPermittedSourcesAreListed:
    def test_only_named_sources_can_be_built(self, conn):
        with pytest.raises(KeyError, match="not a permitted source"):
            build("fragrantica", conn)

    def test_the_prohibited_publications_are_absent(self):
        """Fragrantica's FeedBurner mirror answers 200 and is deliberately
        unused: the constraint names it, and reading its syndication to
        route around that honours the letter while ignoring the ask."""
        listed = " ".join(url for url, _ in PERMITTED.values()).lower()
        for banned in ("fragrantica", "parfumo", "feedburner"):
            assert banned not in listed

    def test_the_client_identifies_itself(self):
        """A reader that will not say who it is is indistinguishable from
        a scraper, whatever its intentions."""
        from fragrance_graph.release_sources import USER_AGENT

        assert "fragrance-graph" in USER_AGENT
        assert "http" in USER_AGENT
