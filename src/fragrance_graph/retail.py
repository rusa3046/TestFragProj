"""Retailer catalog listings: what a retailer sells, never what it says.

    uv run python -m fragrance_graph.retail ingest data/external/nordstrom-fragrance.json
    uv run python -m fragrance_graph.retail import
    uv run python -m fragrance_graph.retail unresolved
    uv run python -m fragrance_graph.retail coverage

## The pipeline, and why the raw file never merges

    raw Bright Data JSON --(ingest)--> data/curation/retailer-listings.jsonl
                          --(import)--> retailer_listings / retailer_variants

A retailer's product page is two different things wearing one JSON blob.
A price, a size, a brand, a SKU, a URL, a review count — those are facts,
checkable against the retailer's own site at any time. `description` and
`reviews[].content` are the retailer's (or their customers') copyrighted
prose, and this codebase has no license to republish either. So the raw
scrape — the file Bright Data actually delivers — is not source of truth
and must never merge to `main`: it is a private input, read once by
`ingest`, and everything downstream of that read is facts only.

`data/curation/retailer-listings.jsonl` is the committed truth, the same
role `data/curation/houses.jsonl` plays for fragrance houses (see
`houses.py`): a curator can read it, diff it, and trust that nothing in it
needs a second license to keep. The database tables are derived from it
exactly the way `fragrances`/`claims` are derived from `data/corpus/` —
disposable, rebuilt by `retail import`, never a second place a fact can
drift from the file that actually means it.

**No description field, ever, at any stage.** `ingest` reads it off the
raw record only long enough to *not* copy it forward — there is no code
path in this module that writes retailer prose to disk. `tests/
test_retail.py` proves this against a fixture built from the sample even
with `description` still populated, so the guarantee does not quietly
depend on the fixture happening to have it blanked.

## Note extraction is not this module's job

A description mentions "notes of jasmine and saffron" and it is tempting
to mine it — but that is exactly the LLM-extraction discipline
`extract/llm.py` already owns, complete with its own evidence-strength
rules, and duplicating a smaller, unaudited version of it here would
create a second place "what does this smell like" could be asserted from
weaker grounds than the one the rest of this codebase holds to. That is
M4's job, deliberately not this one's; this module carries facts a
listing states about itself, not what anybody — retailer or reviewer —
says a fragrance smells like.

## Retailer-agnostic by construction

`retailer` is a column value — read off each raw record's own
`store_name` field, lowercased — never a table name, a module name, or a
schema decision. Nordstrom is the first source ingested (Sephora was the
original target; access issues moved it to Nordstrom, which is exactly
the kind of change this design should absorb for free). Nothing below
assumes Nordstrom is the only retailer, or the last.

## Resolution is conservative on purpose

Linking a listing to a catalogue fragrance is exactly the failure mode
`resolve.names.FUZZY_THRESHOLD`'s docstring warns about, at higher stakes:
a false merge here would attach a retailer's real price and real image to
the *wrong* bottle, which is worse than a listing with no price at all.
So `resolve_listings` never fuzzy-matches — `resolve.names.EXACT_ONLY` —
and additionally requires `brand_raw` to agree with the catalogue row's
own `brand` through the same `brand_tokens`/`brands_agree` machinery
`resolve.names` already built for the trailing-"by <brand>" case. A
listing that cannot be resolved this conservatively stays `fragrance_id
IS NULL`, visible in `retail unresolved` for a curator to fix the way any
other unresolved mention gets fixed — teaching the catalogue a new alias,
never by loosening this module's own match rule.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, Row, get_connection, migrate

log = logging.getLogger("fragrance_graph.retail")

DEFAULT_CURATED_PATH = Path("data/curation/retailer-listings.jsonl")

#: Which pipeline collected a row. A column on every listing (migration
#: 0016), not an assumption baked into application code — a second
#: collector (a different data broker, a manual entry) is a new value
#: here, never a silent reinterpretation of what this one already wrote.
COLLECTION_MODE = "bright_data"

#: The legal basis that makes storing a listing acceptable: facts (price,
#: size, brand, title-as-identifier, category) plus a *link* to the
#: retailer's own hosted image, never a copy of the image and never the
#: retailer's descriptive prose. See the module docstring's "pipeline"
#: section for the full argument.
RIGHTS_BASIS = "facts_and_image_url_only"


# --- the fragrance filter --------------------------------------------------

#: A retailer's own category string counts as fragrance-shaped if any of
#: these words appear in it, case-insensitively — plain substring checks,
#: exactly as specified: the raw category strings observed
#: ("Home>Beauty>Fragrance>Perfume") are short and unambiguous enough that
#: a word-boundary regex would be solving a problem this data does not
#: have.
CATEGORY_FRAGRANCE_WORDS = ("fragrance", "perfume", "cologne")

#: A title counts as fragrance-shaped if any of these appear in it — this
#: is what catches a listing whose *category* is a general one ("Home>
#: Designer>Women") but whose title plainly names a concentration
#: ("Sauvage Eau de Parfum", "Libre Eau de Parfum Spray Fragrance").
TITLE_FRAGRANCE_WORDS = ("parfum", "toilette", "cologne", "fragrance", "edp", "edt")


def is_fragrance(record: dict) -> bool:
    """Whether a raw Bright Data record is a fragrance listing at all.

    OR, not AND: `product_category` and `title` each independently earn a
    keep. Real data needs both halves — of the 9-record sample, 4 have a
    category that never says "Fragrance" or "Perfume" at all
    ("Home>Designer>Women", "Home>Designer>Men", "Home>Men>Grooming &
    Cologne>Cologne") and are only caught by their titles saying "Eau de
    Parfum".
    """
    category = (record.get("product_category") or "").lower()
    if any(word in category for word in CATEGORY_FRAGRANCE_WORDS):
        return True
    title = (record.get("title") or "").lower()
    return any(word in title for word in TITLE_FRAGRANCE_WORDS)


# --- title normalization for resolution -------------------------------

#: Concentration/format words a retailer appends to a bare fragrance name.
#: Longer phrases before their own substrings ("eau de parfum" before
#: "parfum") so a title ending in the phrase is not stripped down to a
#: trailing fragment of it in the wrong pass.
_CONCENTRATION_SUFFIXES = (
    "eau de parfum", "eau de toilette", "eau de cologne",
    "parfum", "toilette", "cologne", "fragrance", "spray",
    "edp", "edt", "edc",
)
_SUFFIXES_LONGEST_FIRST = tuple(
    sorted(_CONCENTRATION_SUFFIXES, key=len, reverse=True)
)


def strip_concentration_suffix(title: str) -> str:
    """A retailer title with its trailing concentration/format words
    removed, repeatedly, so a title carrying more than one of them in a
    row ("Libre Eau de Parfum Spray Fragrance") ends at the bare name
    ("Libre") rather than stopping after the first pass.

    Whole-token only: a suffix must be preceded by whitespace or be the
    entire remaining title, so a bottle whose real name happens to *end*
    in one of these words as part of itself (none exist in the sample,
    but nothing here should assume that stays true) is not silently
    mangled mid-word. Never strips a title down to nothing — if the
    "remainder" after a match is empty, that match is refused and
    stripping stops, on the theory that a title that IS only "Fragrance"
    is not this function's problem to solve.
    """
    text = title.strip()
    changed = True
    while changed:
        changed = False
        lowered = text.lower()
        for suffix in _SUFFIXES_LONGEST_FIRST:
            if not lowered.endswith(suffix):
                continue
            cut = len(text) - len(suffix)
            if cut > 0 and text[cut - 1] not in " \t":
                continue  # matched inside a word, not a trailing token
            candidate = text[:cut].rstrip(" \t-,")
            if not candidate:
                continue  # never strip the whole title away
            text = candidate
            changed = True
            break
    return text


#: A size display that already states an unambiguous metric size —
#: nothing else. See `parse_size_ml`.
_METRIC_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*m[Ll]\.?\s*$")


def parse_size_ml(size_display: str) -> float | None:
    """A metric size in millilitres, only when `size_display` already
    states one unambiguously — never computed from an ounce figure.

    "6.8 Ounce" -> 201.1ml by naive multiplication (`* 29.5735`). That
    number is not wrong arithmetic and it is not trustworthy: a bottle's
    "Ounce" label and its true manufactured metric size are two separate
    facts, each independently rounded by whoever wrote it down, and
    multiplying one back out does not recover the other — it produces a
    number that looks precise and asserts a size nobody measured. Storing
    that as `size_ml` would be exactly the kind of "wrong unit math worse
    than NULL" this column exists to avoid, so this function does not
    perform unit conversion in either direction, ever. It only reads a
    size that is already metric on the page (the retailer's own "100 mL"
    or "100ml"), which needs no conversion and no judgement call to be
    correct. On the real 9-record sample, every size is ounce-labelled,
    so this returns `None` for all of them today — an honest `NULL`, not
    a missing feature.
    """
    match = _METRIC_SIZE.match(size_display or "")
    if not match:
        return None
    return float(match.group(1))


def _normalize_retailer(store_name: str) -> str:
    return (store_name or "").strip().lower()


# --- ingest: raw JSON -> curated JSONL ----------------------------------


@dataclass(frozen=True)
class IngestStats:
    total: int
    kept: int
    rejected: int
    #: Up to 10 rejected titles/ids, for a human skimming the CLI output —
    #: not a full audit trail, which `retail unresolved` and the JSONL
    #: itself already are for what *was* kept.
    rejected_examples: tuple[str, ...] = ()


#: Every field a curated row carries about the listing itself. No
#: `description`, no `reviews` — the whole point of this module.
_LISTING_FIELDS = (
    "retailer", "retailer_item_id", "url", "title_raw", "brand_raw",
    "image_url", "additional_image_urls", "review_count", "star_rating",
    "category_path", "retrieved_at", "collection_mode", "rights_basis",
    "image_ref_only",
)


def _curated_row(record: dict) -> dict:
    """One raw Bright Data record, reduced to facts + provenance.

    `variants` only ever comes from a `variant_type == "size"` group —
    "color" entries (`"NO COLOR"`, `"REGULAR"`, sometimes a real color
    name) are packaging metadata with no purchasable-size fact of their
    own, and Bright Data nests them in the identical `variant_options`
    shape as real sizes, so they have to be told apart by `variant_type`,
    not by anything about an individual option.
    """
    retrieved_at = record.get("timestamp") or ""
    variants: list[dict] = []
    for group in record.get("variants") or []:
        if group.get("variant_type") != "size":
            continue
        for option in group.get("variant_options") or []:
            size_display = option.get("option_name") or ""
            variants.append({
                "sku": option.get("option_id") or "",
                "size_display": size_display,
                "size_ml": parse_size_ml(size_display),
                "price_usd": option.get("option_price"),
                "in_stock": bool(option.get("in_stock")),
                "retrieved_at": retrieved_at,
            })
    return {
        "retailer": _normalize_retailer(record.get("store_name") or ""),
        "retailer_item_id": str(record.get("item_id") or ""),
        "url": record.get("url") or "",
        "title_raw": record.get("title") or "",
        "brand_raw": record.get("brand") or "",
        "image_url": record.get("image_url"),
        "additional_image_urls": list(record.get("additional_image_urls") or []),
        "review_count": record.get("review_count"),
        "star_rating": record.get("star_rating"),
        "category_path": record.get("product_category") or "",
        "retrieved_at": retrieved_at,
        "collection_mode": COLLECTION_MODE,
        "rights_basis": RIGHTS_BASIS,
        "image_ref_only": True,
        "variants": variants,
    }


def ingest(
    raw_path: Path, curated_path: Path = DEFAULT_CURATED_PATH
) -> IngestStats:
    """Parse a raw Bright Data JSON export into the curated JSONL.

    Pure file-to-file: no database connection, so this can run — and be
    tested — with nothing else running. Rows are sorted by `(retailer,
    retailer_item_id)` and each written with `sort_keys=True`, so
    re-ingesting an unchanged raw file produces a byte-identical curated
    file: a real diff means a real change, never reordering noise.
    """
    raw_path = Path(raw_path)
    records = json.loads(raw_path.read_text(encoding="utf-8"))

    kept: list[dict] = []
    rejected = 0
    rejected_examples: list[str] = []
    for record in records:
        if not is_fragrance(record):
            rejected += 1
            if len(rejected_examples) < 10:
                rejected_examples.append(
                    record.get("title") or record.get("item_id") or "?"
                )
            continue
        kept.append(_curated_row(record))

    kept.sort(key=lambda r: (r["retailer"], r["retailer_item_id"]))

    curated_path = Path(curated_path)
    curated_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(row, sort_keys=True) for row in kept]
    curated_path.write_text(
        "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
    )

    stats = IngestStats(
        total=len(records), kept=len(kept), rejected=rejected,
        rejected_examples=tuple(rejected_examples),
    )
    log.info(
        "Ingested %s: kept %d of %d record(s), rejected %d",
        raw_path, stats.kept, stats.total, stats.rejected,
    )
    return stats


def _read_curated(path: Path) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


# --- import: curated JSONL -> tables ------------------------------------


_LISTING_UPSERT = f"""
INSERT INTO retailer_listings ({', '.join(_LISTING_FIELDS)})
VALUES ({', '.join('%(' + f + ')s' for f in _LISTING_FIELDS)})
ON CONFLICT (retailer, retailer_item_id) DO UPDATE SET
    {', '.join(
        f"{f} = excluded.{f}" for f in _LISTING_FIELDS
        if f not in ('retailer', 'retailer_item_id')
    )}
RETURNING id
"""

_VARIANT_FIELDS = ("sku", "size_display", "size_ml", "price_usd", "in_stock", "retrieved_at")

_VARIANT_UPSERT = f"""
INSERT INTO retailer_variants (listing_id, {', '.join(_VARIANT_FIELDS)})
VALUES (%(listing_id)s, {', '.join('%(' + f + ')s' for f in _VARIANT_FIELDS)})
ON CONFLICT (listing_id, sku) DO UPDATE SET
    {', '.join(f"{f} = excluded.{f}" for f in _VARIANT_FIELDS if f != 'sku')}
"""


@dataclass(frozen=True)
class ResolutionStats:
    total: int
    resolved: int
    unresolved: int


@dataclass(frozen=True)
class ImportStats:
    listings: int
    variants: int
    resolution: ResolutionStats


def import_listings(
    conn: psycopg.Connection, curated_path: Path = DEFAULT_CURATED_PATH
) -> ImportStats:
    """Load the curated JSONL into `retailer_listings`/`retailer_variants`,
    then run `resolve_listings`. Idempotent: keyed on `(retailer,
    retailer_item_id)` for listings and `(listing_id, sku)` for variants,
    both real natural keys Bright Data itself assigns, so re-importing an
    unchanged file writes the same rows back over themselves and a
    re-scrape (a price change, a restock) updates in place.
    """
    rows = _read_curated(curated_path)
    listing_count = 0
    variant_count = 0
    for row in rows:
        values = {name: row.get(name) for name in _LISTING_FIELDS}
        values["additional_image_urls"] = json.dumps(
            values["additional_image_urls"] or []
        )
        listing_id = conn.execute(_LISTING_UPSERT, values).fetchone()[0]
        listing_count += 1
        for variant in row.get("variants") or []:
            values = {"listing_id": listing_id}
            values.update({name: variant.get(name) for name in _VARIANT_FIELDS})
            conn.execute(_VARIANT_UPSERT, values)
            variant_count += 1
    conn.commit()

    resolution = resolve_listings(conn)
    log.info(
        "Imported %d listing(s), %d variant(s); resolved %d/%d",
        listing_count, variant_count, resolution.resolved, resolution.total,
    )
    return ImportStats(listings=listing_count, variants=variant_count, resolution=resolution)


# --- resolution: listings -> catalogue fragrances -----------------------


def resolve_listings(conn: psycopg.Connection) -> ResolutionStats:
    """Link every listing to a catalogue fragrance, conservatively.

    Recomputed from scratch for every listing on every call, not merely
    filled in where `fragrance_id IS NULL` — the same curated JSONL must
    always produce the same resolution result regardless of when it last
    ran, and a listing that resolved before must be free to *stop*
    resolving if the catalogue itself changed underneath it (a fragrance
    renamed, a brand corrected) rather than keeping a stale link forever.

    Two gates, both required:

    1. **Exact-after-normalization only.** `title_raw` is reduced by
       `strip_concentration_suffix`, then matched via
       `resolve.names.best_match(..., threshold=EXACT_ONLY)` — the same
       "fuzzy branch disabled" mechanism `api.search` and
       `best_match_conservative_trailing_brand` already use to mean
       "exact or nothing". A false merge here would attach a real price
       and a real image to the *wrong* bottle, which is a worse outcome
       than a listing sitting unresolved.
    2. **Brand agreement.** `brand_raw` and the matched candidate's own
       `brand` column must agree via `resolve.names.brand_tokens` +
       `brands_agree`, checked against `resolve.entities.known_brands` —
       the identical machinery `resolve.names` built for the
       trailing-"by <brand>" case. A candidate with no brand recorded at
       all fails this the same way it already fails
       `best_match_conservative_trailing_brand`'s check: "cannot verify"
       is refused, not waved through.

    A listing that fails either gate is left (or reset to)
    `fragrance_id IS NULL` — visible in `unresolved_listings` for a
    curator, never guessed at here.
    """
    from fragrance_graph.resolve.entities import known_brands, load_candidates
    from fragrance_graph.resolve.names import EXACT_ONLY, best_match, brand_tokens, brands_agree

    candidates = load_candidates(conn)
    by_id = {c.fragrance_id: c for c in candidates}
    known = known_brands(conn)

    rows = conn.execute(
        "SELECT id, title_raw, brand_raw FROM retailer_listings"
    ).fetchall()

    resolved = 0
    for row in rows:
        bare_title = strip_concentration_suffix(row["title_raw"])
        match = best_match(bare_title, candidates, threshold=EXACT_ONLY)
        fragrance_id = None
        if match is not None:
            candidate = by_id[match.fragrance_id]
            if brands_agree(
                brand_tokens(row["brand_raw"] or ""),
                brand_tokens(candidate.brand or ""),
                known,
            ):
                fragrance_id = match.fragrance_id
        conn.execute(
            "UPDATE retailer_listings SET fragrance_id = %s WHERE id = %s",
            (fragrance_id, row["id"]),
        )
        if fragrance_id is not None:
            resolved += 1
    conn.commit()
    return ResolutionStats(total=len(rows), resolved=resolved, unresolved=len(rows) - resolved)


def unresolved_listings(conn: psycopg.Connection) -> list[Row]:
    """Listings with no catalogue link, for a curator to fix by teaching
    the catalogue a new alias — never by loosening `resolve_listings`."""
    return conn.execute(
        "SELECT retailer, retailer_item_id, title_raw, brand_raw, url"
        "  FROM retailer_listings"
        " WHERE fragrance_id IS NULL"
        " ORDER BY retailer, title_raw"
    ).fetchall()


def render_unresolved(rows: list[Row]) -> str:
    if not rows:
        return "Every listing resolved."
    lines = [f"{len(rows)} unresolved listing(s):", ""]
    for row in rows:
        lines.append(
            f"  [{row['retailer']}] {row['title_raw']!r} "
            f"(brand: {row['brand_raw']!r})  {row['url']}"
        )
    return "\n".join(lines)


# --- coverage: what the retailer layer has actually filled in -----------


@dataclass(frozen=True)
class CoverageStats:
    """Totals `retail coverage` prints, plus the per-fragrance detail
    (`fragrances_with_*`) M6's completeness score can read directly
    rather than re-deriving the same joins."""

    total_listings: int
    resolved_listings: int
    listings_with_price: int
    listings_with_image: int
    total_fragrances: int
    fragrances_with_listing: int
    fragrances_with_price: int
    fragrances_with_image: int

    def _pct(self, n: int, total: int) -> float:
        return round(100 * n / total, 1) if total else 0.0

    @property
    def pct_resolved(self) -> float:
        return self._pct(self.resolved_listings, self.total_listings)

    @property
    def pct_with_price(self) -> float:
        return self._pct(self.listings_with_price, self.total_listings)

    @property
    def pct_with_image(self) -> float:
        return self._pct(self.listings_with_image, self.total_listings)

    @property
    def pct_fragrances_with_listing(self) -> float:
        return self._pct(self.fragrances_with_listing, self.total_fragrances)


def per_fragrance_coverage(conn: psycopg.Connection) -> list[Row]:
    """One row per catalogue fragrance: whether it has any retailer
    listing, a price on any of that listing's variants, and an image
    reference — the per-fragrance question `coverage()`'s totals are
    aggregated from, and what a future completeness score (M6) can read
    directly without re-deriving the same joins.
    """
    return conn.execute(
        """
        SELECT
            f.id AS fragrance_id,
            f.canonical_name,
            EXISTS (
                SELECT 1 FROM retailer_listings rl
                 WHERE rl.fragrance_id = f.id
            ) AS has_listing,
            EXISTS (
                SELECT 1 FROM retailer_listings rl
                JOIN retailer_variants rv ON rv.listing_id = rl.id
                WHERE rl.fragrance_id = f.id AND rv.price_usd IS NOT NULL
            ) AS has_price,
            EXISTS (
                SELECT 1 FROM retailer_listings rl
                 WHERE rl.fragrance_id = f.id
                   AND rl.image_url IS NOT NULL AND rl.image_url <> ''
            ) AS has_image
          FROM fragrances f
         ORDER BY f.canonical_name
        """
    ).fetchall()


def coverage(conn: psycopg.Connection) -> CoverageStats:
    per_fragrance = per_fragrance_coverage(conn)
    total_listings = conn.execute(
        "SELECT count(*) FROM retailer_listings"
    ).fetchone()[0]
    resolved_listings = conn.execute(
        "SELECT count(*) FROM retailer_listings WHERE fragrance_id IS NOT NULL"
    ).fetchone()[0]
    listings_with_price = conn.execute(
        "SELECT count(DISTINCT listing_id) FROM retailer_variants"
        " WHERE price_usd IS NOT NULL"
    ).fetchone()[0]
    listings_with_image = conn.execute(
        "SELECT count(*) FROM retailer_listings"
        " WHERE image_url IS NOT NULL AND image_url <> ''"
    ).fetchone()[0]
    return CoverageStats(
        total_listings=total_listings,
        resolved_listings=resolved_listings,
        listings_with_price=listings_with_price,
        listings_with_image=listings_with_image,
        total_fragrances=len(per_fragrance),
        fragrances_with_listing=sum(1 for r in per_fragrance if r["has_listing"]),
        fragrances_with_price=sum(1 for r in per_fragrance if r["has_price"]),
        fragrances_with_image=sum(1 for r in per_fragrance if r["has_image"]),
    )


def render_coverage(stats: CoverageStats) -> str:
    return "\n".join([
        f"{stats.total_listings} listing(s), "
        f"{stats.resolved_listings} resolved ({stats.pct_resolved}%)",
        f"  with price: {stats.listings_with_price} ({stats.pct_with_price}%)",
        f"  with image: {stats.listings_with_image} ({stats.pct_with_image}%)",
        "",
        f"{stats.total_fragrances} catalogue fragrance(s), "
        f"{stats.fragrances_with_listing} with a listing "
        f"({stats.pct_fragrances_with_listing}%)",
        f"  with price: {stats.fragrances_with_price}",
        f"  with image: {stats.fragrances_with_image}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.retail",
        description="Retailer catalog listings: ingest, import, unresolved, coverage.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)

    ing = sub.add_parser(
        "ingest", help="Parse a raw retailer JSON export into the curated JSONL."
    )
    ing.add_argument("raw_path", type=Path)
    ing.add_argument("--out", type=Path, default=DEFAULT_CURATED_PATH)

    imp = sub.add_parser("import", help="Load the curated JSONL into the database.")
    imp.add_argument("--path", type=Path, default=DEFAULT_CURATED_PATH)

    sub.add_parser("unresolved", help="Listings not yet linked to a catalogue fragrance.")
    sub.add_parser("coverage", help="Per-fragrance and aggregate retailer coverage.")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.command == "ingest":
        stats = ingest(args.raw_path, args.out)
        print(
            f"Kept {stats.kept} of {stats.total} record(s); "
            f"rejected {stats.rejected} (not fragrance-shaped)."
        )
        if stats.rejected_examples:
            print("  e.g. " + "; ".join(stats.rejected_examples))
        print(f"Wrote {args.out}")
        return 0

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        if args.command == "import":
            result = import_listings(conn, args.path)
            print(f"Imported {result.listings} listing(s), {result.variants} variant(s).")
            print(
                f"Resolved {result.resolution.resolved} of "
                f"{result.resolution.total} listing(s)."
            )
        elif args.command == "unresolved":
            print(render_unresolved(unresolved_listings(conn)))
        elif args.command == "coverage":
            print(render_coverage(coverage(conn)))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
