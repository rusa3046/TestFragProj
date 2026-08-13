"""Building outbound buying links, and disclosing them.

Every affiliate network deep-links differently: some want the destination
URL percent-encoded into a query parameter, some want a merchant id and a
SKU, all of them want the publisher's tracking id somewhere. That variation
lives in `retailers.url_template` as data. **There is no per-retailer code
here and there must not be** — the moment one shop needs a special case in
Python, the next twenty do too, and the rules about disclosure and ranking
end up enforced in nineteen places instead of one.

Two things this module refuses to build:

**A template with no tracking id.** The link still works, the reader still
clicks, and the commission report is empty at the end of the month with
nothing anywhere recording why. It fails at registration instead.

**A template with no destination.** Every product would link to the same
landing page, which reads as a working link and is not one.

**Disclosure is not optional and not separable.** `Link` carries the
disclosure text as a field, so nothing can obtain a URL from this module
without also holding the words that have to appear beside it, and
`render_link` puts them on the same line. The trust rule is "inline, at the
link" specifically because a footer disclosure is one nobody reads, and a
renderer that has to go and fetch the wording separately is a renderer that
will one day forget to.

**Text only.** Nothing here emits markup, and the schema has no column that
could hold a logo — naming a fragrance identifies it, showing a brand's
imagery borrows its authority.

Ordering is by what a buyer pays, never by what we earn. That is not a
promise being kept by care: `retailers` has no commission column, so
ordering by commission is not something this code could express.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from string import Formatter
from urllib.parse import quote

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate

log = logging.getLogger("fragrance_graph.commerce.links")

#: The words that appear beside every outbound link. Short enough to sit
#: inline without being skipped, explicit enough to be a disclosure.
DISCLOSURE = "affiliate link"

#: Placeholders a `url_template` may use.
#:
#: `url` is the destination percent-encoded for use as a query parameter,
#: which is what deep-link trackers expect; `raw_url` is the same value
#: unencoded, for the networks that take it as a path segment.
TEMPLATE_FIELDS = frozenset({"affiliate_id", "url", "raw_url", "external_id"})

#: Without one of these the template is a constant, and every product on
#: every page links to the same shop front page.
DESTINATION_FIELDS = frozenset({"url", "raw_url", "external_id"})


class TemplateError(ValueError):
    """A url_template that cannot produce a correct outbound link."""


def template_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def validate_template(template: str) -> None:
    """Reject a template before it is stored, not when a page renders it.

    A bad template found at render time has already been used to build
    however many links were rendered before someone looked.
    """
    fields = template_fields(template)

    unknown = fields - TEMPLATE_FIELDS
    if unknown:
        raise TemplateError(
            f"Unknown placeholder(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(TEMPLATE_FIELDS))}."
        )
    if "affiliate_id" not in fields:
        raise TemplateError(
            "Template has no {affiliate_id}. The link would work, the click "
            "would happen, and the commission report would be empty with "
            "nothing recording why."
        )
    if not fields & DESTINATION_FIELDS:
        raise TemplateError(
            "Template names no destination. Add one of "
            f"{', '.join(sorted(DESTINATION_FIELDS))}, or every product links "
            "to the same page."
        )


@dataclass(frozen=True)
class Retailer:
    """A shop, and how to construct a tracked link into it."""

    id: int
    name: str
    network: str
    affiliate_id: str
    url_template: str


@dataclass(frozen=True)
class Link:
    """One buying option, with the disclosure attached to it.

    `disclosure` is a field rather than a constant a renderer is trusted to
    remember, so a link and the words that must appear beside it cannot be
    separated by anybody downstream.
    """

    url: str
    retailer: str
    product_name: str
    price: float | None = None
    currency: str | None = None
    size_ml: float | None = None
    concentration: str | None = None
    disclosure: str = DISCLOSURE


def build_url(retailer: Retailer, product_url: str, external_id: str = "") -> str:
    """The tracked outbound URL for one listing at one retailer."""
    validate_template(retailer.url_template)
    return retailer.url_template.format(
        affiliate_id=quote(retailer.affiliate_id, safe=""),
        url=quote(product_url, safe=""),
        raw_url=product_url,
        external_id=quote(external_id, safe=""),
    )


def add_retailer(
    conn: psycopg.Connection,
    name: str,
    *,
    network: str,
    affiliate_id: str,
    url_template: str,
) -> int:
    """Register a retailer. Validates the template first."""
    validate_template(url_template)
    cur = conn.execute(
        "INSERT INTO retailers (name, network, affiliate_id, url_template) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (name, network, affiliate_id, url_template),
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    return new_id


def load_retailer(conn: psycopg.Connection, name: str) -> Retailer | None:
    """By name, because a rebuilt database renumbers ids."""
    row = conn.execute(
        "SELECT id, name, network, affiliate_id, url_template FROM retailers "
        "WHERE name = %s",
        (name,),
    ).fetchone()
    return None if row is None else Retailer(**dict(row))


#: Buying options for one fragrance.
#:
#: Ordered by price, cheapest first, with unpriced listings last and the
#: retailer name breaking ties so the order never churns. Nothing in the
#: ORDER BY — or anywhere in this schema — knows what a listing pays us.
BUYING_LINKS_SQL = """
SELECT p.name AS product_name, p.size_ml, p.concentration, p.price,
       p.currency, p.url, p.external_id,
       r.id AS id, r.name AS name, r.network, r.affiliate_id, r.url_template
  FROM products p
  JOIN retailers r ON r.id = p.retailer_id
 WHERE p.fragrance_id = %(fragrance_id)s
 ORDER BY p.price IS NULL, p.price, r.name, p.external_id
"""


def buying_links(conn: psycopg.Connection, fragrance_id: int) -> list[Link]:
    """Where this fragrance can be bought, cheapest first.

    Note what this function is not: it is never consulted by ranking, and
    ranking never calls it. A fragrance with no rows here appears in
    `similar_to` results exactly as high as one with three — see
    `test_ranking_is_identical_with_and_without_products`.
    """
    links = []
    for row in conn.execute(BUYING_LINKS_SQL, {"fragrance_id": fragrance_id}):
        retailer = Retailer(
            id=row["id"],
            name=row["name"],
            network=row["network"],
            affiliate_id=row["affiliate_id"],
            url_template=row["url_template"],
        )
        links.append(
            Link(
                url=build_url(retailer, row["url"], row["external_id"]),
                retailer=retailer.name,
                product_name=row["product_name"],
                price=row["price"],
                currency=row["currency"],
                size_ml=row["size_ml"],
                concentration=row["concentration"],
            )
        )
    return links


def render_link(link: Link) -> str:
    """One buying option as a line of text.

    The disclosure sits between the description and the URL — in the same
    line, in the same sentence a reader is reading when they decide to
    click. Text only: no markup, and no image of any kind, because a brand's
    logo beside a fragrance's name is that brand's authority borrowed
    without asking.
    """
    parts = [link.retailer]
    if link.size_ml:
        size = int(link.size_ml) if float(link.size_ml).is_integer() else link.size_ml
        parts.append(f"{size}ml")
    if link.concentration:
        parts.append(link.concentration)
    if link.price is not None:
        parts.append(f"{link.price:.2f} {link.currency or ''}".strip())
    return f"{' · '.join(parts)} ({link.disclosure}) {link.url}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Register retailers and build tracked outbound links."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    retailer = sub.add_parser("retailer", help="Manage retailers")
    retailer_sub = retailer.add_subparsers(dest="retailer_command", required=True)

    add = retailer_sub.add_parser("add", help="Register a retailer")
    add.add_argument("name")
    add.add_argument("--network", required=True, help="e.g. rakuten, shareasale")
    add.add_argument("--affiliate-id", required=True)
    add.add_argument(
        "--url-template",
        required=True,
        help=(
            "Placeholders: {affiliate_id} (required), and one of {url} "
            "(percent-encoded destination), {raw_url}, {external_id}"
        ),
    )

    listing = retailer_sub.add_parser("list", help="Registered retailers")

    links = sub.add_parser("links", help="Buying options for a fragrance")
    links.add_argument("fragrance", help="Canonical name or alias")

    for p in (add, listing, links):
        p.add_argument("--db-url", default=DEFAULT_DB_URL)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        if args.command == "retailer" and args.retailer_command == "add":
            try:
                add_retailer(
                    conn,
                    args.name,
                    network=args.network,
                    affiliate_id=args.affiliate_id,
                    url_template=args.url_template,
                )
            except TemplateError as exc:
                print(f"Refusing that template: {exc}")
                return 1
            print(f"Registered {args.name} ({args.network})")

        elif args.command == "retailer":
            rows = conn.execute(
                "SELECT name, network, url_template FROM retailers ORDER BY name"
            ).fetchall()
            if not rows:
                print("No retailers registered.")
                return 0
            for row in rows:
                print(f"{row['name']}  [{row['network']}]  {row['url_template']}")

        elif args.command == "links":
            from fragrance_graph.query import find_fragrance

            row = find_fragrance(conn, args.fragrance)
            if row is None:
                print(f"No fragrance matches {args.fragrance!r}.")
                return 1
            found = buying_links(conn, row["id"])
            if not found:
                # Said plainly, because "no links" is a normal and permitted
                # state: results are never filtered to what can be sold.
                print(f"{row['canonical_name']}: no buying links stored.")
                return 0
            print(row["canonical_name"])
            for link in found:
                print(f"  {render_link(link)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
