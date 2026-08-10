"""Commerce: the products/retailers schema, feed import, and outbound links.

The trust rules live here alongside the plumbing, because the plumbing is
what would violate them. `tests/test_query.py` carries the other half — that
ranking cannot see any of this.
"""

import sqlite3

import pytest

from fragrance_graph.resolve.entities import add_fragrance


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
