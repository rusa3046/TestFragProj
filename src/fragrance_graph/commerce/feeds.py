"""Importing affiliate-network product feeds into `products`.

**This is not scraping.** SPEC forbids scraping retailers, Fragrantica and
Parfumo: their terms prohibit it and this project has to be publicly
demoable. An affiliate network's product feed is the opposite case —
licensed data a network publishes to its publishers for exactly this
purpose, fetched from a URL the network gives you, under a contract you
signed. The distinction is the whole reason commerce is allowed to exist
here at all, so it is stated at the top of the module that would be the
place to quietly break it.

Feeds are CSV or XML, and every network names its columns differently.
There is no per-retailer parsing code and there must not be: a column alias
table maps whatever the feed calls a field onto the seven things we need,
and a new network is entries in that table rather than a new branch.

**Matching reuses `resolve/names.py`.** This is the same entity-resolution
problem as mapping "BR540" to a bottle — second instance, same rules, same
0.88 threshold, same preference for a visible miss over a silent false
merge. Writing a second matcher here would mean two definitions of "these
are the same fragrance" drifting apart, with the commerce one deciding
which buy link goes on the page.

Feed names carry chrome the dictionary does not:

    "Lattafa Khamrah EDP 100ml Spray Unisex"

Size, concentration and retail boilerplate are parsed off before matching,
and the raw name is stored regardless — the parse is a guess, and keeping
the original is what lets a wrong guess be found later rather than being
the only surviving copy.

**What is deliberately not stripped**: `Elixir`, `Extrait`, `Parfum` and
`Cologne`. Each is a concentration *and* a common part of a fragrance's
name, and removing them merges bottles that are not the same thing. Strip
"Elixir" and `Dior Sauvage Elixir` becomes `Dior Sauvage`, putting a buy
link for a £110 bottle under a £75 one. `EDP`/`EDT`/`EDC` and the phrase
`eau de parfum` are never part of a name, so those come off. The cost is
that `Baccarat Rouge 540 Extrait de Parfum` does not match the curated
`Baccarat Rouge 540` and lands in the unmatched report instead — which is
the trade this project makes everywhere else too.

**The match rate is reported, never buried.** An importer that silently
drops half a feed is worse than one that fails: the feed still looks
imported, the pages still render, and the missing half is invisible until
someone counts. Every unmatched name is logged and `unmatched` lists them
by frequency, so the fix is curation rather than archaeology.

Idempotent on `(retailer_id, external_id)`, like every other writer here.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, Row, get_connection, migrate
from fragrance_graph.resolve.entities import load_candidates
from fragrance_graph.resolve.names import Candidate, best_match

log = logging.getLogger("fragrance_graph.commerce.feeds")


class FeedFormatError(ValueError):
    """A feed whose columns could not be understood.

    Raised rather than returning zero rows, because "imported 0 products"
    and exit code 0 is how a renamed column goes unnoticed for a month.
    """


#: Feed column names, per field, lowercased and stripped of punctuation.
#: Drawn from Rakuten's and ShareASale's published feed specs plus the
#: generic names smaller merchants use. Adding a network is adding names
#: here; it is never a new code path.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "external_id": ("productid", "sku", "id", "productsku", "merchantsku"),
    "name": ("name", "productname", "title", "product"),
    "url": ("url", "link", "producturl", "buyurl", "productlink", "linkurl"),
    "price": ("price", "saleprice", "currentprice", "retailprice"),
    "currency": ("currency", "currencycode", "pricecurrency"),
}

#: Without a name there is nothing to match and without a URL there is
#: nowhere to send a reader, so a feed missing either is a format error
#: rather than a row-level skip.
REQUIRED_FIELDS = ("external_id", "name", "url")

#: Sizes as printed on bottles. Only ever applied to a size the feed stated
#: in ounces: "3.4 oz" is 100.55ml and is a 100ml bottle everywhere on
#: earth, and rendering "100.5ml" beside another retailer's "100ml" makes
#: one bottle look like two products. A size stated in ml is stored as
#: stated — Armaf Club de Nuit Intense Man really is 105ml, and snapping it
#: to 100 would invent a bottle that does not exist.
STANDARD_SIZES_ML = (5, 10, 15, 20, 30, 35, 50, 60, 75, 90, 100, 120, 125, 150, 200, 250)

#: How far an ounce-derived size may be from a standard one and still snap.
#: 3.3 oz is 97.6ml, 2.4% below the 100ml bottle it is printed on.
SIZE_SNAP_TOLERANCE = 0.03

ML_PER_OZ = 29.5735

SIZE_RE = re.compile(
    r"(?<![\w.])(\d{1,4}(?:[.,]\d{1,2})?)\s*"
    r"(ml\b|millilitres?\b|milliliters?\b|fl\.?\s*oz\b\.?|oz\b\.?)",
    re.IGNORECASE,
)

#: (pattern, label, strip). `strip` says whether the token may be removed
#: before matching. False means the word is also a common part of fragrance
#: names — see the module docstring; removing those merges distinct bottles.
#: Order matters: the first match wins, so "extrait de parfum" is tried
#: before "parfum" and "eau de cologne" before "cologne".
CONCENTRATIONS: tuple[tuple[str, str, bool], ...] = (
    (r"eau\s+de\s+parfum", "EDP", True),
    (r"eau\s+de\s+toilette", "EDT", True),
    (r"eau\s+de\s+cologne", "EDC", True),
    (r"edp", "EDP", True),
    (r"edt", "EDT", True),
    (r"edc", "EDC", True),
    (r"extrait\s+de\s+parfum", "EXTRAIT", False),
    (r"extrait", "EXTRAIT", False),
    (r"elixir", "ELIXIR", False),
    (r"parfum", "PARFUM", False),
    (r"cologne", "COLOGNE", False),
)

_CONCENTRATION_RES = tuple(
    (re.compile(rf"\b{pattern}\b", re.IGNORECASE), label, strip)
    for pattern, label, strip in CONCENTRATIONS
)

#: Retail boilerplate that is never part of a fragrance's name. Kept small
#: on purpose, exactly as `names.FILLER_WORDS` is: "Man", "Homme" and
#: "Intense" are all retail-sounding and all load-bearing parts of real
#: names (Armaf Club de Nuit Intense *Man*), so gendered wording is only
#: removed as a whole phrase.
FEED_NOISE = (
    r"for\s+(?:men|women|him|her)",
    r"pour\s+(?:homme|femme)",
    r"vaporisateur",
    r"unisex",
    r"tester",
    r"spray",
)

_NOISE_RE = re.compile(r"\b(?:" + "|".join(FEED_NOISE) + r")\b", re.IGNORECASE)


@dataclass(frozen=True)
class FeedProduct:
    """One row of a feed, before it is matched to anything."""

    external_id: str
    name: str
    url: str
    price: float | None = None
    currency: str | None = None

    @property
    def size_ml(self) -> float | None:
        return parse_size_ml(self.name)

    @property
    def concentration(self) -> str | None:
        return parse_concentration(self.name)


def _normalize_column(name: str) -> str:
    """Feed headers differ by case, spacing and punctuation, not meaning."""
    return re.sub(r"[^a-z0-9]", "", (name or "").casefold())


def map_columns(headers: Iterable[str]) -> dict[str, str]:
    """Map our field names onto this feed's column names.

    Raises `FeedFormatError` naming the columns it did see, because the
    failure this guards is a network renaming a column: without it the
    import succeeds, writes nothing, and reports success.
    """
    seen = {_normalize_column(h): h for h in headers if h}
    mapping = {}
    for field_name, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in seen:
                mapping[field_name] = seen[alias]
                break

    missing = [f for f in REQUIRED_FIELDS if f not in mapping]
    if missing:
        raise FeedFormatError(
            f"Feed is missing {', '.join(missing)}. Columns present: "
            f"{', '.join(sorted(seen.values()))}. Add the column name to "
            "FIELD_ALIASES if this network calls it something new."
        )
    return mapping


def _to_float(value: str | None) -> float | None:
    """Prices arrive as '84.00', '£84.00', '1,299.00' or ''."""
    if not value:
        return None
    cleaned = re.sub(r"[^\d.,]", "", value).replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _build_product(values: dict[str, str | None]) -> FeedProduct | None:
    """One feed row to a FeedProduct, or None if it is unusable."""
    if not all((values.get(f) or "").strip() for f in REQUIRED_FIELDS):
        return None
    return FeedProduct(
        external_id=values["external_id"].strip(),
        name=values["name"].strip(),
        url=values["url"].strip(),
        price=_to_float(values.get("price")),
        currency=(values.get("currency") or "").strip().upper() or None,
    )


#: A fixture has to be obviously a fixture on its face, and CSV has no
#: comment syntax. Leading `#` lines are dropped so a sample feed can say
#: what it is in the file itself. Only lines before the header are eligible,
#: so a product name starting with `#` is never affected — the collision
#: `ingest/seed.py` hit with its own `#` rule.
def _strip_leading_comments(text: str) -> str:
    lines = text.splitlines()
    start = 0
    for line in lines:
        if line.startswith("#") or not line.strip():
            start += 1
        else:
            break
    return "\n".join(lines[start:])


def parse_csv_feed(text: str) -> list[FeedProduct]:
    """Parse a delimited feed. Comma, tab and pipe are all in live use."""
    body = _strip_leading_comments(text)
    if not body.strip():
        raise FeedFormatError("Feed file is empty.")

    header = body.splitlines()[0]
    try:
        dialect = csv.Sniffer().sniff(header, delimiters=",\t|;")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(body), delimiter=delimiter)
    mapping = map_columns(reader.fieldnames or [])

    products = []
    for row in reader:
        product = _build_product({f: row.get(col) for f, col in mapping.items()})
        if product:
            products.append(product)
    return products


#: A product feed never carries a document type declaration, and a DOCTYPE
#: is what an entity-expansion bomb needs. Refusing one is cheaper and more
#: certain than reasoning about which expat settings are in force.
_DOCTYPE_RE = re.compile(r"<!DOCTYPE", re.IGNORECASE)


def parse_xml_feed(text: str) -> list[FeedProduct]:
    """Parse an XML feed: any repeated element whose children we recognise.

    The item element is found rather than hardcoded, because networks call
    it `<product>`, `<item>` and `<Product>` and that difference is not
    worth a per-retailer branch.
    """
    if _DOCTYPE_RE.search(text):
        raise FeedFormatError(
            "Feed declares a DOCTYPE. Product feeds do not; entity-expansion "
            "bombs do. Refusing to parse it."
        )

    root = ET.fromstring(text)
    items = [el for el in root if len(el)]
    if not items:
        raise FeedFormatError("Feed contains no product elements.")

    headers = {child.tag for item in items for child in item}
    mapping = map_columns(headers)
    by_normalized = {_normalize_column(tag): tag for tag in headers}

    products = []
    for item in items:
        values: dict[str, str | None] = {}
        for field_name, column in mapping.items():
            el = item.find(column)
            values[field_name] = None if el is None else (el.text or "")

        # Rakuten states the currency as an attribute of the price element
        # rather than as its own field, so a mapped-column-only reader
        # silently loses it and every price renders unlabelled.
        if not values.get("currency") and "price" in mapping:
            price_el = item.find(by_normalized.get("price", "price"))
            if price_el is not None:
                values["currency"] = price_el.get("currency")

        product = _build_product(values)
        if product:
            products.append(product)
    return products


def parse_feed(path: Path) -> list[FeedProduct]:
    """Parse a feed file, dispatching on what it contains rather than on
    its extension — networks serve `.txt` that is CSV and `.xml.gz` that
    has been unpacked to `.dat`."""
    text = Path(path).read_text(encoding="utf-8-sig")
    if _strip_leading_comments(text).lstrip().startswith("<"):
        return parse_xml_feed(text)
    return parse_csv_feed(text)


# --- parsing the messy bits out of a feed name ------------------------------


def _snap_to_standard_size(millilitres: float) -> float:
    """Nearest printed bottle size, if one is close enough."""
    for size in STANDARD_SIZES_ML:
        if abs(millilitres - size) <= size * SIZE_SNAP_TOLERANCE:
            return float(size)
    return round(millilitres, 1)


def parse_size_ml(name: str) -> float | None:
    """Bottle size in millilitres, from a name like "EDP 100ml Spray".

    Ounces are converted and snapped to a standard size; millilitres are
    taken at face value. See STANDARD_SIZES_ML for why the two are treated
    differently.
    """
    match = SIZE_RE.search(name)
    if not match:
        return None
    try:
        amount = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    if amount <= 0:
        return None

    unit = match.group(2).lower()
    if unit.startswith("ml") or unit.startswith("milli"):
        return amount
    return _snap_to_standard_size(amount * ML_PER_OZ)


def parse_concentration(name: str) -> str | None:
    """EDP / EDT / EDC / EXTRAIT / ELIXIR / PARFUM / COLOGNE, or None."""
    for pattern, label, _strip in _CONCENTRATION_RES:
        if pattern.search(name):
            return label
    return None


def match_text(name: str) -> str:
    """The part of a feed name that is meant to identify a fragrance.

    Removes size, retail boilerplate, and only those concentration tokens
    that can never be part of a name. What is left goes to
    `resolve.names.best_match` unchanged, so normalisation, aliases, the
    junk rule and the 0.88 threshold all behave exactly as they do for
    comment mentions.
    """
    text = SIZE_RE.sub(" ", name)
    for pattern, _label, strip in _CONCENTRATION_RES:
        if strip:
            text = pattern.sub(" ", text)
    text = _NOISE_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" -|,·")


def match_product(name: str, candidates: list[Candidate]):
    """Resolve a feed product name to a curated fragrance, or None."""
    cleaned = match_text(name)
    if not cleaned:
        return None
    return best_match(cleaned, candidates)


# --- writing ----------------------------------------------------------------


@dataclass
class ImportStats:
    """What an import did, including what it could not place.

    `unmatched` is a tally rather than a count, because "43% unmatched" is
    a number to worry about and "43% unmatched, and here are the eleven
    names" is a curation session.
    """

    seen: int = 0
    inserted: int = 0
    updated: int = 0
    matched: int = 0
    unmatched: Counter = field(default_factory=Counter)

    @property
    def unmatched_count(self) -> int:
        return self.seen - self.matched

    @property
    def match_rate(self) -> float:
        return self.matched / self.seen if self.seen else 0.0

    def __str__(self) -> str:
        return (
            f"{self.seen} rows, {self.inserted} new, {self.updated} updated; "
            f"{self.matched} matched a curated fragrance "
            f"({self.match_rate:.1%}), {self.unmatched_count} unmatched"
        )


UPSERT_SQL = """
INSERT INTO products
    (fragrance_id, retailer_id, name, size_ml, concentration, external_id,
     price, currency, url, last_seen)
VALUES
    (%(fragrance_id)s, %(retailer_id)s, %(name)s, %(size_ml)s, %(concentration)s, %(external_id)s,
     %(price)s, %(currency)s, %(url)s, %(last_seen)s)
ON CONFLICT (retailer_id, external_id) DO UPDATE SET
    fragrance_id = excluded.fragrance_id,
    name = excluded.name,
    size_ml = excluded.size_ml,
    concentration = excluded.concentration,
    price = excluded.price,
    currency = excluded.currency,
    url = excluded.url,
    last_seen = excluded.last_seen
"""


def find_retailer_id(conn: psycopg.Connection, name: str) -> int | None:
    """Retailers are addressed by name, never by id — a rebuilt database
    renumbers rows, and the name is what a person types."""
    row = conn.execute("SELECT id FROM retailers WHERE name = %s", (name,)).fetchone()
    return None if row is None else row["id"]


def import_products(
    conn: psycopg.Connection,
    retailer_id: int,
    products: Iterable[FeedProduct],
    *,
    now: str | None = None,
) -> ImportStats:
    """Write feed rows, matching each to a curated fragrance where possible.

    Idempotent on `(retailer_id, external_id)`: a re-import updates price,
    URL and `last_seen` in place. Re-running after curating new fragrances
    is also how an unmatched row becomes matched, with no re-download —
    matching runs on every import, not only on insert.
    """
    candidates = load_candidates(conn)
    if not candidates:
        log.warning(
            "No curated fragrances, so nothing can match. Products will still "
            "be stored and will resolve on the next import after curation."
        )

    stamp = now or datetime.now(UTC).isoformat()
    stats = ImportStats()
    cache: dict[str, int | None] = {}

    for product in products:
        stats.seen += 1
        if product.name not in cache:
            match = match_product(product.name, candidates)
            cache[product.name] = None if match is None else match.fragrance_id
        fragrance_id = cache[product.name]

        if fragrance_id is None:
            stats.unmatched[product.name] += 1
        else:
            stats.matched += 1

        existing = conn.execute(
            "SELECT id FROM products WHERE retailer_id = %s AND external_id = %s",
            (retailer_id, product.external_id),
        ).fetchone()
        conn.execute(
            UPSERT_SQL,
            {
                "fragrance_id": fragrance_id,
                "retailer_id": retailer_id,
                "name": product.name,
                "size_ml": product.size_ml,
                "concentration": product.concentration,
                "external_id": product.external_id,
                "price": product.price,
                "currency": product.currency,
                "url": product.url,
                "last_seen": stamp,
            },
        )
        if existing:
            stats.updated += 1
        else:
            stats.inserted += 1

    conn.commit()

    # Logged individually, not just counted. The whole point of reporting a
    # match rate is that the missing rows are actionable, and they are only
    # actionable if you can read their names.
    for name, count in stats.unmatched.most_common():
        log.info("unmatched (%d): %s", count, name)
    log.info("Imported: %s", stats)
    return stats


UNMATCHED_SQL = """
SELECT p.name AS name, r.name AS retailer, count(*) AS listings
  FROM products p
  JOIN retailers r ON r.id = p.retailer_id
 WHERE p.fragrance_id IS NULL
 GROUP BY p.name, r.name
 ORDER BY listings DESC, p.name
"""


def unmatched_products(conn: psycopg.Connection) -> list[Row]:
    """Stored products that match no curated fragrance, most listed first.

    The commerce twin of `resolve.entities report`: it says where curation
    would pay, ordered by how much of the catalogue it would unlock.
    """
    return conn.execute(UNMATCHED_SQL).fetchall()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import affiliate-network product feeds (CSV or XML).",
        epilog=(
            "Feeds are licensed data the network publishes to its publishers. "
            "Retailers are never scraped."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="Import a feed file for one retailer")
    imp.add_argument("file", type=Path)
    imp.add_argument("--retailer", required=True, help="Retailer name, as registered")
    imp.add_argument(
        "--min-match-rate",
        type=float,
        default=0.0,
        help=(
            "Exit non-zero if fewer than this fraction of rows matched a "
            "curated fragrance. Default 0.0: report, do not gate."
        ),
    )

    un = sub.add_parser("unmatched", help="Stored products matching no fragrance")
    un.add_argument("--limit", type=int, default=40)

    for p in (imp, un):
        p.add_argument("--db-url", default=DEFAULT_DB_URL)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        if args.command == "import":
            retailer_id = find_retailer_id(conn, args.retailer)
            if retailer_id is None:
                print(
                    f"No retailer named {args.retailer!r}. Register it first:\n"
                    "  python -m fragrance_graph.commerce.links retailer add ..."
                )
                return 1

            products = parse_feed(args.file)
            stats = import_products(conn, retailer_id, products)
            print(f"{args.file}: {stats}")
            if stats.unmatched:
                print("\nTop unmatched names (curate these to unlock listings):")
                for name, count in stats.unmatched.most_common(10):
                    print(f"  {count:>3}  {name}")
            if stats.match_rate < args.min_match_rate:
                print(
                    f"\nMatch rate {stats.match_rate:.1%} is below the required "
                    f"{args.min_match_rate:.1%}."
                )
                return 1

        elif args.command == "unmatched":
            rows = unmatched_products(conn)
            if not rows:
                print("Every stored product matches a curated fragrance.")
                return 0
            print(f"{'n':>3}  {'retailer':<20} product")
            for row in rows[: args.limit]:
                print(f"{row['listings']:>3}  {row['retailer']:<20} {row['name']}")
            print(f"\n{len(rows)} distinct unmatched products.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
