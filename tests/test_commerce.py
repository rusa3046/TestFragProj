"""Commerce: the products/retailers schema, feed import, and outbound links.

The trust rules live here alongside the plumbing, because the plumbing is
what would violate them. `tests/test_query.py` carries the other half — that
ranking cannot see any of this.

The feed fixtures under `fixtures/feeds/` are invented, and say so in their
own first lines. They are shaped after real network feed formats, but no row
in them came from a retailer or a network.
"""

import logging
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from fragrance_graph.commerce.feeds import (
    FeedFormatError,
    ImportStats,
    import_products,
    map_columns,
    match_product,
    match_text,
    parse_concentration,
    parse_csv_feed,
    parse_feed,
    parse_size_ml,
    parse_xml_feed,
    unmatched_products,
)
from fragrance_graph.commerce.links import (
    Link,
    Retailer,
    TemplateError,
    build_url,
    buying_links,
    render_link,
    validate_template,
)
from fragrance_graph.commerce.links import add_retailer as add_retailer_validated
from fragrance_graph.resolve.entities import add_fragrance, load_candidates
from scripts.seed_fragrances import SEED

FEEDS = Path(__file__).parent / "fixtures" / "feeds"
CSV_FEED = FEEDS / "example_scent_co.csv"
XML_FEED = FEEDS / "example_parfumerie.xml"


def columns(conn, table):
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_retailer(
    conn,
    name="Example Scent Co",
    *,
    network="rakuten",
    affiliate_id="aff-0001",
    url_template="https://click.example-network.test/d?id={affiliate_id}&url={url}",
):
    cur = conn.execute(
        "INSERT INTO retailers (name, network, affiliate_id, url_template) "
        "VALUES (?, ?, ?, ?)",
        (name, network, affiliate_id, url_template),
    )
    conn.commit()
    return cur.lastrowid


def add_product(conn, retailer_id, external_id, *, fragrance_id=None, name="A bottle"):
    conn.execute(
        "INSERT INTO products (fragrance_id, retailer_id, name, external_id, url, "
        "                      last_seen) "
        "VALUES (?, ?, ?, ?, 'https://shop.test/p', '2026-08-10')",
        (fragrance_id, retailer_id, name, external_id),
    )
    conn.commit()


# --- schema ----------------------------------------------------------------


def test_products_and_retailers_are_separate_tables(conn):
    """One fragrance, many products. Merging them would split the count.

    Folding a product into `fragrances` means a row per bottle size, which
    turns "31 people said this" into three rows of ten — the one number the
    product sells, divided by shelf inventory.
    """
    assert columns(conn, "retailers") >= {
        "name", "network", "affiliate_id", "url_template", "created_at"
    }
    assert columns(conn, "products") >= {
        "fragrance_id", "retailer_id", "name", "size_ml", "concentration",
        "external_id", "price", "currency", "url", "last_seen",
    }


def test_fragrances_gains_no_commercial_column(conn):
    """The dependency runs one way only.

    If `fragrances` ever grows an `affiliate_id` or a `price`, every ranking
    query in query.py can reach commerce through a join it already makes.
    """
    assert columns(conn, "fragrances") == {
        "id", "canonical_name", "brand", "house_year", "aliases"
    }


def test_no_table_can_hold_brand_imagery(conn):
    """Text only: naming a fragrance identifies it, a logo borrows its authority.

    Feeds ship image URLs on every row and the cheapest way to render a
    listing is to pass one through. There is nowhere to put it.
    """
    for table in ("products", "retailers", "fragrances"):
        for column in columns(conn, table):
            assert not any(
                token in column.lower() for token in ("image", "img", "logo", "thumb")
            ), f"{table}.{column} would carry brand imagery"


def test_products_are_unique_per_retailer_and_external_id(conn):
    """The idempotency key, so re-importing a feed updates instead of doubling."""
    retailer = add_retailer(conn)
    add_product(conn, retailer, "SKU-1")
    with pytest.raises(sqlite3.IntegrityError):
        add_product(conn, retailer, "SKU-1")


def test_the_same_external_id_at_two_retailers_is_two_products(conn):
    """Shops number their own catalogues; SKU-1 is not a global identifier."""
    a = add_retailer(conn, "Shop A")
    b = add_retailer(conn, "Shop B")
    add_product(conn, a, "SKU-1")
    add_product(conn, b, "SKU-1")
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 2


def test_an_unmatched_product_is_storable(conn):
    """A feed row we cannot place is kept, so curating later costs no re-download."""
    retailer = add_retailer(conn)
    add_product(conn, retailer, "SKU-1", fragrance_id=None)
    assert conn.execute(
        "SELECT count(*) FROM products WHERE fragrance_id IS NULL"
    ).fetchone()[0] == 1


def test_retiring_a_fragrance_does_not_delete_the_shop_listing(conn):
    """The shop still sells the bottle; only our dictionary changed."""
    frag = add_fragrance(conn, "Lattafa Khamrah")
    retailer = add_retailer(conn)
    add_product(conn, retailer, "SKU-1", fragrance_id=frag)

    conn.execute("DELETE FROM fragrances WHERE id = ?", (frag,))
    conn.commit()

    (row,) = conn.execute("SELECT fragrance_id FROM products").fetchall()
    assert row["fragrance_id"] is None


def test_ending_a_retailer_relationship_removes_its_listings(conn):
    """Every one of them carries an affiliate link that is no longer ours."""
    retailer = add_retailer(conn)
    add_product(conn, retailer, "SKU-1")

    conn.execute("DELETE FROM retailers WHERE id = ?", (retailer,))
    conn.commit()

    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 0


def test_retailer_name_is_a_natural_key(conn):
    """Feed imports name their retailer in a shell command, not by row id."""
    add_retailer(conn, "Example Scent Co")
    with pytest.raises(sqlite3.IntegrityError):
        add_retailer(conn, "Example Scent Co")


# --- feed parsing ----------------------------------------------------------


@pytest.fixture
def curated(conn):
    """The dictionary as `scripts/seed_fragrances.py` actually seeds it.

    Imported rather than retyped so the match rates measured below are the
    rates a real run would get, and so a curation change shows up here.
    """
    for name, brand, aliases in SEED:
        add_fragrance(conn, name, brand=brand, aliases=aliases)
    return conn


def test_csv_feed_parses(curated):
    products = parse_csv_feed(CSV_FEED.read_text())
    assert len(products) == 14
    first = products[0]
    assert first.external_id == "FRG-1001"
    assert first.name == "Lattafa Khamrah Eau de Parfum 3.4 oz Spray Unisex"
    assert first.price == 32.95
    assert first.currency == "GBP"
    assert first.url.startswith("https://")


def test_xml_feed_parses(curated):
    products = parse_xml_feed(XML_FEED.read_text())
    assert len(products) == 6
    assert products[0].external_id == "RK-2001"


def test_currency_stated_as_an_attribute_is_not_lost(curated):
    """Rakuten hangs it off <price>, so a fields-only reader drops it and
    every price renders unlabelled."""
    products = parse_xml_feed(XML_FEED.read_text())
    assert {p.currency for p in products} == {"GBP"}


def test_parse_feed_dispatches_on_content_not_extension(tmp_path):
    """Networks serve .txt that is CSV and .dat that is XML."""
    disguised = tmp_path / "feed.dat"
    disguised.write_text(XML_FEED.read_text())
    assert len(parse_feed(disguised)) == 6

    disguised2 = tmp_path / "feed.xml"
    disguised2.write_text(CSV_FEED.read_text())
    assert len(parse_feed(disguised2)) == 14


@pytest.mark.parametrize("delimiter", [",", "\t", "|", ";"])
def test_delimiters_in_live_use_all_parse(delimiter):
    header = delimiter.join(["productid", "name", "price", "link"])
    row = delimiter.join(["A1", "Creed Aventus EDP 100ml", "235.00", "https://x.test"])
    (product,) = parse_csv_feed(f"{header}\n{row}\n")
    assert product.external_id == "A1"


def test_unrecognised_columns_fail_loudly(curated):
    """'Imported 0 products' with exit code 0 is how a renamed column goes
    unnoticed for a month."""
    with pytest.raises(FeedFormatError) as exc:
        parse_csv_feed("thing,cost,where\nA1,10,https://x.test\n")
    assert "external_id" in str(exc.value)
    # It names what it did see, so the fix is one line in FIELD_ALIASES.
    assert "thing" in str(exc.value)


def test_column_names_match_regardless_of_case_and_punctuation():
    assert map_columns(["Product ID", "PRODUCT_NAME", "Link"]) == {
        "external_id": "Product ID",
        "name": "PRODUCT_NAME",
        "url": "Link",
    }


def test_a_doctype_is_refused():
    """Product feeds never carry one; entity-expansion bombs need one."""
    with pytest.raises(FeedFormatError):
        parse_xml_feed(
            '<!DOCTYPE lol [<!ENTITY a "aa">]><feed><product>'
            "<productID>1</productID><name>x</name><URL>u</URL>"
            "</product></feed>"
        )


def test_fixture_header_comments_are_not_read_as_products(curated):
    """The fixture has to be able to say it is a fixture in its own text."""
    assert CSV_FEED.read_text().startswith("# SYNTHETIC FIXTURE")
    assert all("SYNTHETIC" not in p.name for p in parse_csv_feed(CSV_FEED.read_text()))


def test_a_row_missing_a_url_is_dropped_not_stored():
    """There is nowhere to send a reader, so the listing is not a listing."""
    feed = (
        "productid,name,link\n"
        "A1,Creed Aventus EDP 100ml,https://x.test\n"
        "A2,Tom Ford Oud Wood EDP 50ml,\n"
    )
    assert [p.external_id for p in parse_csv_feed(feed)] == ["A1"]


# --- reading size and concentration out of a feed name ---------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Creed Aventus EDP 100ml", 100.0),
        ("Parfums de Marly Layton Eau de Parfum 125 ml", 125.0),
        # Stated in ml, kept as stated: Club de Nuit Intense Man really is
        # 105ml and snapping it to 100 would invent a bottle.
        ("Armaf Club de Nuit Intense Man EDT 105ml Spray", 105.0),
        # Ounces are converted, then snapped: 3.4 oz is 100.55ml and is
        # printed on a 100ml bottle. "100.5ml" next to another retailer's
        # "100ml" makes one bottle look like two products.
        ("Lattafa Khamrah EDP 3.4 oz", 100.0),
        ("Tom Ford Oud Wood 1.7 fl oz", 50.0),
        ("Dior Sauvage 3.3 oz Spray", 100.0),
        ("Xerjoff Naxos", None),
    ],
)
def test_size_parsing(name, expected):
    assert parse_size_ml(name) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Creed Aventus EDP 100ml", "EDP"),
        ("Dior Sauvage Eau de Toilette 60ml", "EDT"),
        ("Guerlain Shalimar Eau de Cologne", "EDC"),
        # Checked before the bare "parfum" pattern, or every extrait in
        # every feed reads as a plain parfum.
        ("Baccarat Rouge 540 Extrait de Parfum 70ml", "EXTRAIT"),
        ("Dior Sauvage Elixir 60ml", "ELIXIR"),
        ("Zara Red Temptation 100ml", None),
    ],
)
def test_concentration_parsing(name, expected):
    assert parse_concentration(name) == expected


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Lattafa Khamrah EDP 100ml Spray Unisex", "Lattafa Khamrah"),
        ("Creed Aventus Eau de Parfum 3.4 oz for Men", "Creed Aventus"),
        ("Tom Ford Oud Wood EDP 50ml Tester", "Tom Ford Oud Wood"),
    ],
)
def test_match_text_strips_feed_chrome(name, expected):
    assert match_text(name) == expected


def test_match_text_keeps_concentrations_that_are_part_of_names():
    """Strip "Elixir" and a £110 bottle's buy link lands under a £75 one."""
    assert match_text("Dior Sauvage Elixir 60ml") == "Dior Sauvage Elixir"
    assert match_text("Baccarat Rouge 540 Extrait de Parfum 70ml").endswith(
        "Extrait de Parfum"
    )


def test_match_text_does_not_eat_a_brand_name():
    """"Parfum" as a strippable word would destroy Parfums de Marly, which is
    the largest house in this corpus."""
    assert match_text("Parfums de Marly Layton Eau de Parfum 125 ml") == (
        "Parfums de Marly Layton"
    )


def test_match_text_does_not_eat_gendered_words_that_are_part_of_a_name():
    """Armaf Club de Nuit Intense *Man* is the bottle's name, not chrome."""
    assert match_text("Armaf Club de Nuit Intense Man EDT 105ml Spray") == (
        "Armaf Club de Nuit Intense Man"
    )


# --- matching --------------------------------------------------------------


def test_matching_goes_through_the_curated_aliases(curated):
    """Proof this uses resolve/names.py rather than a second matcher: an
    abbreviation no string comparison can reach still resolves, because a
    human wrote it down as an alias."""
    match = match_product("BR540 Eau de Parfum 70ml Spray", load_candidates(curated))
    assert match.canonical_name == "Maison Francis Kurkdjian Baccarat Rouge 540"


def test_a_flanker_does_not_match_its_parent(curated):
    """The failure this is guarding is a wrong buy link, not a wrong count.

    Sauvage Elixir and Khamrah Qahwa are different bottles at different
    prices. Matching either to its parent sells a reader the wrong thing.
    """
    candidates = load_candidates(curated)
    assert match_product("Dior Sauvage Elixir 60ml", candidates) is None
    assert match_product("Lattafa Khamrah Qahwa EDP 100ml", candidates) is None


def test_junk_names_resolve_to_nothing(curated):
    assert match_product("100ml Spray Unisex", load_candidates(curated)) is None


# --- importing -------------------------------------------------------------


def import_fixture(conn, path, retailer_name, **kwargs):
    retailer = conn.execute(
        "SELECT id FROM retailers WHERE name = ?", (retailer_name,)
    ).fetchone()
    retailer_id = retailer["id"] if retailer else add_retailer(conn, retailer_name)
    return import_products(conn, retailer_id, parse_feed(path), **kwargs)


def test_the_fixture_feeds_report_their_match_rate(curated):
    """65% across both fixtures, and the shortfall is five nameable rows.

    The number matters less than the fact that it exists: an importer that
    silently drops a third of a feed looks exactly like one that imported
    all of it.
    """
    csv_stats = import_fixture(curated, CSV_FEED, "Example Scent Co")
    xml_stats = import_fixture(curated, XML_FEED, "Example Parfumerie")

    assert (csv_stats.seen, csv_stats.matched) == (14, 9)
    assert (xml_stats.seen, xml_stats.matched) == (6, 4)
    assert round(csv_stats.match_rate, 3) == 0.643
    assert round(xml_stats.match_rate, 3) == 0.667


def test_every_unmatched_name_is_logged_not_just_counted(curated, caplog):
    """A rate is a thing to worry about; a list of names is a curation task."""
    with caplog.at_level(logging.INFO, logger="fragrance_graph.commerce.feeds"):
        stats = import_fixture(curated, CSV_FEED, "Example Scent Co")

    logged = "\n".join(caplog.messages)
    for name in stats.unmatched:
        assert name in logged


def test_unmatched_rows_are_stored_not_dropped(curated):
    """Kept so curating the bottle later costs no re-download."""
    import_fixture(curated, CSV_FEED, "Example Scent Co")
    assert curated.execute("SELECT count(*) FROM products").fetchone()[0] == 14

    names = {row["name"] for row in unmatched_products(curated)}
    assert "PDM Herod 125ml Spray" in names


def test_curating_a_fragrance_resolves_it_on_the_next_import(curated):
    """The promise the nullable fragrance_id exists to keep.

    Matching runs on every import, not only on insert, so an alias added
    today lights up rows imported last month without re-downloading a feed.
    """
    import_fixture(curated, CSV_FEED, "Example Scent Co")
    assert curated.execute(
        "SELECT fragrance_id FROM products WHERE external_id = 'FRG-1013'"
    ).fetchone()["fragrance_id"] is None

    herod = curated.execute(
        "SELECT id FROM fragrances WHERE canonical_name = 'Parfums de Marly Herod'"
    ).fetchone()["id"]
    from fragrance_graph.resolve.entities import add_alias

    add_alias(curated, herod, "PDM Herod")

    import_fixture(curated, CSV_FEED, "Example Scent Co")
    assert curated.execute(
        "SELECT fragrance_id FROM products WHERE external_id = 'FRG-1013'"
    ).fetchone()["fragrance_id"] == herod


def test_reimporting_updates_in_place(curated):
    """Idempotent on (retailer_id, external_id), like every writer here."""
    first = import_fixture(curated, CSV_FEED, "Example Scent Co")
    second = import_fixture(curated, CSV_FEED, "Example Scent Co")

    assert (first.inserted, first.updated) == (14, 0)
    assert (second.inserted, second.updated) == (0, 14)
    assert curated.execute("SELECT count(*) FROM products").fetchone()[0] == 14


def test_a_price_change_is_picked_up(curated):
    retailer = add_retailer(curated, "Example Scent Co")
    products = parse_feed(CSV_FEED)
    import_products(curated, retailer, products)

    cheaper = [
        replace(p, price=19.99) if p.external_id == "FRG-1001" else p
        for p in products
    ]
    import_products(curated, retailer, cheaper)

    assert curated.execute(
        "SELECT price FROM products WHERE external_id = 'FRG-1001'"
    ).fetchone()["price"] == 19.99


def test_last_seen_moves_and_a_vanished_listing_is_not_deleted(curated):
    """A page losing its only buying link with no record of ever having one
    is worse than a stale date."""
    retailer = add_retailer(curated, "Example Scent Co")
    products = parse_feed(CSV_FEED)
    import_products(curated, retailer, products, now="2026-08-01T00:00:00+00:00")

    survivors = [p for p in products if p.external_id != "FRG-1001"]
    import_products(curated, retailer, survivors, now="2026-08-10T00:00:00+00:00")

    seen = {
        row["external_id"]: row["last_seen"]
        for row in curated.execute("SELECT external_id, last_seen FROM products")
    }
    assert len(seen) == 14, "the delisted product is still on record"
    assert seen["FRG-1001"].startswith("2026-08-01")
    assert seen["FRG-1002"].startswith("2026-08-10")


def test_an_empty_dictionary_still_stores_products(conn):
    """Curation order must not decide whether a feed import is worth running."""
    stats = import_fixture(conn, CSV_FEED, "Example Scent Co")
    assert stats.seen == 14
    assert stats.matched == 0
    assert conn.execute("SELECT count(*) FROM products").fetchone()[0] == 14


def test_import_stats_reads_as_a_sentence():
    stats = ImportStats(seen=20, inserted=20, matched=13)
    assert "13 matched" in str(stats)
    assert "65.0%" in str(stats)
    assert "7 unmatched" in str(stats)


def test_match_rate_of_an_empty_feed_is_not_a_zero_division():
    assert ImportStats().match_rate == 0.0


# --- CLI -------------------------------------------------------------------


def test_cli_refuses_an_unregistered_retailer(tmp_path, capsys):
    from fragrance_graph.commerce.feeds import main

    db = tmp_path / "cli.db"
    assert main(["import", str(CSV_FEED), "--retailer", "Nobody",
                 "--db-path", str(db)]) == 1
    assert "No retailer named" in capsys.readouterr().out


def test_cli_gates_on_match_rate_when_asked(tmp_path, capsys):
    """Optional, and off by default: a gate nobody set is not a promise.

    But an importer that can only report is one nobody can put in a
    pipeline, and 65% shipping unnoticed is the failure being guarded.
    """
    from fragrance_graph.commerce.feeds import main
    from fragrance_graph.db import get_connection, migrate

    db = tmp_path / "cli.db"
    setup = get_connection(db)
    migrate(setup)
    for name, brand, aliases in SEED:
        add_fragrance(setup, name, brand=brand, aliases=aliases)
    add_retailer(setup, "Example Scent Co")
    setup.close()

    args = ["import", str(CSV_FEED), "--retailer", "Example Scent Co",
            "--db-path", str(db)]
    assert main([*args, "--min-match-rate", "0.5"]) == 0
    assert main([*args, "--min-match-rate", "0.9"]) == 1
    assert "below the required" in capsys.readouterr().out


# --- outbound links --------------------------------------------------------


RAKUTEN_TEMPLATE = (
    "https://click.example-network.test/deeplink?id={affiliate_id}&murl={url}"
)
SHAREASALE_TEMPLATE = (
    "https://example-network.test/r.cfm"
    "?b={external_id}&u={affiliate_id}&urllink={raw_url}"
)


def test_link_construction_is_template_driven(conn):
    """Two networks, two shapes, one code path.

    The failure guarded is the first `if retailer.name == ...`: after one,
    the disclosure and ranking rules end up enforced in nineteen places.
    """
    rakuten = Retailer(1, "Example Parfumerie", "rakuten", "aff-1", RAKUTEN_TEMPLATE)
    shareasale = Retailer(2, "Example Scent Co", "shareasale", "aff-2",
                          SHAREASALE_TEMPLATE)

    assert build_url(rakuten, "https://shop.test/p/1") == (
        "https://click.example-network.test/deeplink?id=aff-1"
        "&murl=https%3A%2F%2Fshop.test%2Fp%2F1"
    )
    assert build_url(shareasale, "https://shop.test/p/2", "SKU-2") == (
        "https://example-network.test/r.cfm?b=SKU-2&u=aff-2"
        "&urllink=https://shop.test/p/2"
    )


def test_the_destination_is_percent_encoded_for_query_use(conn):
    """A raw '&' in the destination truncates it into the tracker's own
    query string, and the reader lands on the shop's home page."""
    retailer = Retailer(1, "R", "rakuten", "a", RAKUTEN_TEMPLATE)
    url = build_url(retailer, "https://shop.test/p?id=1&size=100")
    assert "murl=https%3A%2F%2Fshop.test%2Fp%3Fid%3D1%26size%3D100" in url


def test_a_template_without_a_tracking_id_is_refused():
    """It works, the click happens, and the report is empty at month end."""
    with pytest.raises(TemplateError, match="affiliate_id"):
        validate_template("https://shop.test/p?u={url}")


def test_a_template_without_a_destination_is_refused():
    """Every product would link to the same page, and look fine doing it."""
    with pytest.raises(TemplateError, match="destination"):
        validate_template("https://click.test/?id={affiliate_id}")


def test_an_unknown_placeholder_is_refused():
    with pytest.raises(TemplateError, match="commission"):
        validate_template("https://click.test/?id={affiliate_id}&c={commission}&u={url}")


def test_a_bad_template_is_rejected_before_it_is_stored(conn):
    """Found at render time, it has already built every link rendered so far."""
    with pytest.raises(TemplateError):
        add_retailer_validated(
            conn, "Broken", network="rakuten", affiliate_id="a",
            url_template="https://shop.test/{url}",
        )
    assert conn.execute("SELECT count(*) FROM retailers").fetchone()[0] == 0


# --- disclosure and text-only ----------------------------------------------


def test_a_link_cannot_be_obtained_without_its_disclosure(conn):
    """Not a constant a renderer is trusted to remember."""
    link = Link(url="https://click.test/x", retailer="R", product_name="p")
    assert link.disclosure == "affiliate link"


def test_the_disclosure_is_inline_at_the_link_not_in_a_footer(conn):
    """A footer disclosure is one nobody reads."""
    link = Link(url="https://click.test/x", retailer="Example Scent Co",
                product_name="Creed Aventus EDP 100ml", price=235.0,
                currency="GBP", size_ml=100.0, concentration="EDP")
    rendered = render_link(link)

    assert "(affiliate link)" in rendered
    assert rendered.count("\n") == 0, "the disclosure is on the link's own line"
    assert rendered.index("affiliate link") < rendered.index(link.url), (
        "read before the thing it describes, not after"
    )


def test_rendering_emits_no_markup_and_no_imagery(conn):
    """Text only: a brand's logo beside its name borrows its authority."""
    link = Link(url="https://click.test/x", retailer="R",
                product_name="Creed Aventus", price=1.0, currency="GBP")
    rendered = render_link(link)
    for token in ("<img", "<a ", "<", ">", "src=", "logo"):
        assert token not in rendered.lower()


# --- reading buying links --------------------------------------------------


@pytest.fixture
def shop(conn):
    """One fragrance, three listings at two retailers, plus a bare fragrance."""
    frag = add_fragrance(conn, "Creed Aventus")
    add_fragrance(conn, "Obscure Indie Thing")

    a = add_retailer_validated(conn, "Example Scent Co", network="shareasale",
                               affiliate_id="aff-2",
                               url_template=SHAREASALE_TEMPLATE)
    b = add_retailer_validated(conn, "Example Parfumerie", network="rakuten",
                               affiliate_id="aff-1", url_template=RAKUTEN_TEMPLATE)
    for retailer, sku, price in ((a, "S-1", 235.0), (b, "R-1", 219.0), (a, "S-2", None)):
        conn.execute(
            "INSERT INTO products (fragrance_id, retailer_id, name, external_id, "
            "                      price, currency, url, last_seen) "
            "VALUES (?, ?, 'Creed Aventus EDP 100ml', ?, ?, 'GBP', "
            "        'https://shop.test/p', '2026-08-10')",
            (frag, retailer, sku, price),
        )
    conn.commit()
    return conn, frag


def test_buying_links_are_cheapest_first(shop):
    """Ordered by what a buyer pays. There is no column for what we earn."""
    conn, frag = shop
    links = buying_links(conn, frag)
    assert [link.price for link in links] == [219.0, 235.0, None]


def test_every_buying_link_is_tracked_and_disclosed(shop):
    conn, frag = shop
    for link in buying_links(conn, frag):
        assert "example-network.test" in link.url, "goes through the network"
        assert link.disclosure


def test_a_fragrance_with_no_listings_is_not_an_error(shop):
    """No buying link is a normal state, not a missing one."""
    conn, _ = shop
    other = conn.execute(
        "SELECT id FROM fragrances WHERE canonical_name = 'Obscure Indie Thing'"
    ).fetchone()["id"]
    assert buying_links(conn, other) == []


def test_ordering_by_commission_is_not_expressible(conn):
    """The strongest form of the trust rule: not a promise, a missing column."""
    from fragrance_graph.commerce.links import BUYING_LINKS_SQL

    forbidden = ("commission", "payout", "epc", "revenue", "margin")
    assert not any(word in BUYING_LINKS_SQL.lower() for word in forbidden)
    assert not any(
        word in column.lower()
        for column in columns(conn, "retailers") | columns(conn, "products")
        for word in forbidden
    )


# --- links CLI -------------------------------------------------------------


def test_cli_registers_a_retailer_and_refuses_a_bad_template(tmp_path, capsys):
    from fragrance_graph.commerce.links import main

    db = tmp_path / "cli.db"
    ok = main(["retailer", "add", "Example Scent Co", "--network", "shareasale",
               "--affiliate-id", "aff-2", "--url-template", RAKUTEN_TEMPLATE,
               "--db-path", str(db)])
    assert ok == 0

    bad = main(["retailer", "add", "Broken", "--network", "x",
                "--affiliate-id", "y", "--url-template", "https://shop.test/{url}",
                "--db-path", str(db)])
    assert bad == 1
    assert "Refusing that template" in capsys.readouterr().out


def test_cli_says_plainly_when_a_fragrance_has_no_links(tmp_path, capsys):
    from fragrance_graph.commerce.links import main
    from fragrance_graph.db import get_connection, migrate

    db = tmp_path / "cli.db"
    setup = get_connection(db)
    migrate(setup)
    add_fragrance(setup, "Obscure Indie Thing")
    setup.close()

    assert main(["links", "Obscure Indie Thing", "--db-path", str(db)]) == 0
    assert "no buying links stored" in capsys.readouterr().out
