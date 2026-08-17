"""Retailer catalog listings: ingest, import, resolve, report.

Two kinds of fixture. `data/fixtures/retail/nordstrom-sample.json` is a
description/review-stripped capture of the real 9-record Bright Data
sample this module was built against — every `description` field is `""`
and every `reviews` list is `[]` in the committed file itself, so no
copyrighted retailer prose sits in this repo even as test data. Most unit
tests build their own tiny records instead (`_raw_record`), the same
reasoning `test_houses.py` gives for not depending on the shape of the
real 204 houses: a future change to the real sample must not silently
break what a test is actually guarding, and a test that wants to prove
`ingest` *strips* a description needs one that starts non-empty, which
the committed fixture — clean on purpose — cannot supply.
"""

import json
from pathlib import Path

import pytest

from fragrance_graph.resolve.entities import add_fragrance
from fragrance_graph.retail import (
    ImportStats,
    IngestStats,
    coverage,
    import_listings,
    ingest,
    is_fragrance,
    main,
    parse_size_ml,
    per_fragrance_coverage,
    render_coverage,
    render_unresolved,
    resolve_listings,
    strip_concentration_suffix,
    unresolved_listings,
)

SAMPLE = Path("data/fixtures/retail/nordstrom-sample.json")


def _raw_record(**overrides):
    """A minimal, invented Bright Data-shaped record — real product names
    (facts, not prose) but no real retailer copy anywhere in it."""
    record = {
        "url": "https://www.example.test/s/some-fragrance/123",
        "item_id": "123",
        "variant_id": "V1",
        "title": "Delina Eau de Parfum",
        "description": "",
        "product_category": "Home>Beauty>Fragrance>Perfume",
        "brand": "Parfums de Marly",
        "image_url": "https://images.example.test/delina.jpg",
        "additional_image_urls": ["https://images.example.test/delina.jpg"],
        "store_name": "Nordstrom",
        "star_rating": 4.5,
        "review_count": 100,
        "reviews": [],
        "timestamp": "2026-08-17T00:00:00.000Z",
        "variants": [
            {
                "variant_type": "color",
                "variant_options": [
                    {
                        "option_id": "V1", "option_name": "NO COLOR",
                        "option_price": 250, "in_stock": True, "image": None,
                    },
                ],
            },
            {
                "variant_type": "size",
                "variant_options": [
                    {
                        "option_id": "S1", "option_name": "1 Ounce",
                        "option_price": 250, "in_stock": True, "image": None,
                    },
                    {
                        "option_id": "S2", "option_name": "2.5 Ounce",
                        "option_price": 410, "in_stock": True, "image": None,
                    },
                ],
            },
        ],
    }
    record.update(overrides)
    return record


def write_raw(tmp_path, *records):
    path = tmp_path / "raw.json"
    path.write_text(json.dumps(list(records)), encoding="utf-8")
    return path


# --- no copyrighted prose ever lands in the curated file -----------------


class TestNoDescriptionOrReviewsEverLandInTheCuratedFile:
    def test_the_committed_fixture_carries_no_description_or_reviews(self):
        """The guardrail against real copyrighted prose ever sitting in
        this repo, even as test data — checked at test time, not only
        trusted at authoring time."""
        records = json.loads(SAMPLE.read_text(encoding="utf-8"))
        assert len(records) == 9
        for record in records:
            assert record["description"] == ""
            assert record["reviews"] == []

    def test_ingest_strips_a_populated_description(self, tmp_path):
        """The real regression this whole architecture rule exists for:
        even when a raw record's `description` *is* populated, `ingest`
        must never carry it into the curated file."""
        raw = write_raw(
            tmp_path,
            _raw_record(
                description="Some invented retailer marketing copy about "
                            "notes and a fragrance story, never real prose."
            ),
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        text = curated.read_text(encoding="utf-8")
        assert "description" not in json.loads(text.splitlines()[0])
        assert "marketing copy" not in text

    def test_ingest_strips_review_content(self, tmp_path):
        raw = write_raw(
            tmp_path,
            _raw_record(reviews=[
                {"review_id": "1", "title": "x", "content": "an invented review body"}
            ]),
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        text = curated.read_text(encoding="utf-8")
        assert "invented review body" not in text
        assert "reviews" not in json.loads(text.splitlines()[0])

    def test_ingesting_the_real_sample_carries_no_prose_forward(self, tmp_path):
        curated = tmp_path / "curated.jsonl"
        ingest(SAMPLE, curated)
        text = curated.read_text(encoding="utf-8")
        assert "description" not in text
        assert "reviews" not in text


# --- size-variant extraction -----------------------------------------------


class TestSizeVariantExtraction:
    def test_only_size_type_variants_become_rows(self, tmp_path):
        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        row = json.loads(curated.read_text(encoding="utf-8").splitlines()[0])
        skus = {v["sku"] for v in row["variants"]}
        assert skus == {"S1", "S2"}, "the color/NO COLOR entry is noise, not a size"

    def test_size_display_and_usd_price_land_verbatim(self, tmp_path):
        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        row = json.loads(curated.read_text(encoding="utf-8").splitlines()[0])
        by_sku = {v["sku"]: v for v in row["variants"]}
        assert by_sku["S1"]["size_display"] == "1 Ounce"
        assert by_sku["S1"]["price_usd"] == 250
        assert by_sku["S2"]["size_display"] == "2.5 Ounce"
        assert by_sku["S2"]["price_usd"] == 410

    def test_in_stock_is_carried_as_a_bool(self, tmp_path):
        record = _raw_record()
        record["variants"][1]["variant_options"][0]["in_stock"] = False
        raw = write_raw(tmp_path, record)
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        row = json.loads(curated.read_text(encoding="utf-8").splitlines()[0])
        by_sku = {v["sku"]: v for v in row["variants"]}
        assert by_sku["S1"]["in_stock"] is False

    def test_a_listing_with_no_size_variants_ingests_with_an_empty_list(self, tmp_path):
        record = _raw_record()
        record["variants"] = [record["variants"][0]]  # color only
        raw = write_raw(tmp_path, record)
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        row = json.loads(curated.read_text(encoding="utf-8").splitlines()[0])
        assert row["variants"] == []


# --- size_ml: never computed from an ounce figure -------------------------


class TestSizeMlParsing:
    def test_an_ounce_display_is_never_converted(self):
        """The brief's own trap: 6.8 * 29.5735 = 201.1, which looks
        precise and is not trustworthy — a bottle's ounce label and its
        true metric size are independently rounded facts."""
        assert parse_size_ml("6.8 Ounce") is None

    def test_a_metric_display_needs_no_conversion_and_is_parsed(self):
        assert parse_size_ml("100 mL") == 100.0
        assert parse_size_ml("100ml") == 100.0
        assert parse_size_ml("50.5 mL") == 50.5

    def test_a_refill_or_one_size_display_is_never_parsed(self):
        assert parse_size_ml("10 Ounce Refill") is None
        assert parse_size_ml("One Size") is None

    def test_the_real_samples_ounce_variants_all_stay_null(self):
        """On the actual 9-record sample every size is ounce-labelled —
        this is the honest, currently-always-NULL state, not a gap."""
        records = json.loads(SAMPLE.read_text(encoding="utf-8"))
        for record in records:
            for group in record["variants"]:
                if group["variant_type"] != "size":
                    continue
                for option in group["variant_options"]:
                    assert parse_size_ml(option["option_name"]) is None


# --- concentration-suffix stripping for resolution -------------------------


class TestConcentrationSuffixStripping:
    @pytest.mark.parametrize("title,expected", [
        ("Baccarat Rouge 540 Eau de Parfum", "Baccarat Rouge 540"),
        ("Her Eau de Parfum", "Her"),
        ("Atomic Rose Eau de Parfum", "Atomic Rose"),
        ("Sauvage Eau de Parfum", "Sauvage"),
        ("Aventus Fragrance", "Aventus"),
        ("Delina Eau de Parfum", "Delina"),
        ("Donna Born in Roma Eau de Parfum", "Donna Born in Roma"),
        ("Libre Eau de Parfum Spray Fragrance", "Libre"),
        ("Angels' Share Fragrance", "Angels' Share"),
    ])
    def test_real_sample_title_shapes(self, title, expected):
        """All nine real titles from the sample, including the one that
        needs two passes ("Libre Eau de Parfum Spray Fragrance" carries
        both "Spray Fragrance" and "Eau de Parfum")."""
        assert strip_concentration_suffix(title) == expected

    def test_a_title_that_is_only_the_suffix_is_left_alone(self):
        """Never strip a title down to nothing."""
        assert strip_concentration_suffix("Fragrance") == "Fragrance"

    def test_a_bare_name_with_no_suffix_is_unchanged(self):
        assert strip_concentration_suffix("Baccarat Rouge 540") == "Baccarat Rouge 540"


# --- the fragrance filter ---------------------------------------------------


class TestFragranceFilter:
    def test_a_fragrance_category_is_kept_regardless_of_title(self):
        assert is_fragrance(_raw_record(
            product_category="Home>Beauty>Fragrance>Perfume", title="Whatever",
        ))

    def test_a_generic_category_with_a_fragrance_title_is_kept(self):
        """4 of the 9 real records have a category that never says
        "Fragrance"/"Perfume" at all ("Home>Designer>Men", etc.) and are
        only caught by their titles."""
        assert is_fragrance(_raw_record(
            product_category="Home>Designer>Men", title="Sauvage Eau de Parfum",
        ))

    def test_a_lipstick_shaped_row_is_rejected(self):
        lipstick = _raw_record(
            title="Velvet Matte Lipstick",
            product_category="Home>Beauty>Makeup>Lips",
            brand="Some Makeup Brand",
        )
        assert not is_fragrance(lipstick)

    def test_ingest_counts_and_reports_rejected_rows(self, tmp_path):
        lipstick = _raw_record(
            item_id="999", title="Velvet Matte Lipstick",
            product_category="Home>Beauty>Makeup>Lips",
        )
        raw = write_raw(tmp_path, _raw_record(), lipstick)
        curated = tmp_path / "curated.jsonl"
        stats = ingest(raw, curated)
        assert isinstance(stats, IngestStats)
        assert stats.total == 2
        assert stats.kept == 1
        assert stats.rejected == 1
        assert "Velvet Matte Lipstick" in stats.rejected_examples

    def test_the_real_sample_is_entirely_fragrance_shaped(self):
        """All 9 real records were curated as a fragrance search result
        set — none should be rejected by the filter."""
        records = json.loads(SAMPLE.read_text(encoding="utf-8"))
        assert all(is_fragrance(r) for r in records)


# --- resolution: conservative, brand-checked, never fuzzy -----------------


class TestResolution:
    def test_a_matching_title_and_brand_resolves(self, conn, tmp_path):
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"])
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Parfums de Marly")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        row = conn.execute(
            "SELECT fragrance_id FROM retailer_listings"
        ).fetchone()
        assert row["fragrance_id"] is not None
        name = conn.execute(
            "SELECT canonical_name FROM fragrances WHERE id = %s", (row["fragrance_id"],)
        ).fetchone()["canonical_name"]
        assert name == "Parfums de Marly Delina"

    def test_a_disagreeing_brand_never_resolves(self, conn, tmp_path):
        """The brief's exact negative case: same title, wrong brand."""
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"])
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Armaf")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        row = conn.execute("SELECT fragrance_id FROM retailer_listings").fetchone()
        assert row["fragrance_id"] is None

    def test_a_brand_with_different_case_still_agrees(self, conn, tmp_path):
        add_fragrance(conn, "Dior Sauvage", brand="Dior", aliases=["Sauvage"])
        raw = write_raw(
            tmp_path, _raw_record(title="Sauvage Eau de Parfum", brand="DIOR")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        row = conn.execute("SELECT fragrance_id FROM retailer_listings").fetchone()
        assert row["fragrance_id"] is not None

    def test_a_near_miss_title_never_fuzzily_resolves(self, conn, tmp_path):
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"])
        raw = write_raw(
            tmp_path,
            # A typo close enough that a fuzzy resolver would take it.
            _raw_record(title="Delinna Eau de Parfum", brand="Parfums de Marly"),
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        row = conn.execute("SELECT fragrance_id FROM retailer_listings").fetchone()
        assert row["fragrance_id"] is None

    def test_a_candidate_with_no_recorded_brand_cannot_be_confirmed(self, conn, tmp_path):
        """"Cannot verify" is refused, not waved through — the same
        precedent `best_match_conservative_trailing_brand` already set."""
        add_fragrance(conn, "Delina", brand=None)
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Parfums de Marly")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        row = conn.execute("SELECT fragrance_id FROM retailer_listings").fetchone()
        assert row["fragrance_id"] is None

    def test_resolve_listings_recomputes_rather_than_only_filling_nulls(self, conn, tmp_path):
        """A listing that resolved before must be free to stop resolving
        if the catalogue changed underneath it."""
        frag = add_fragrance(
            conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"]
        )
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Parfums de Marly")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)
        assert conn.execute(
            "SELECT fragrance_id FROM retailer_listings"
        ).fetchone()["fragrance_id"] == frag

        conn.execute("UPDATE fragrances SET brand = 'Someone Else' WHERE id = %s", (frag,))
        conn.commit()
        resolve_listings(conn)

        row = conn.execute("SELECT fragrance_id FROM retailer_listings").fetchone()
        assert row["fragrance_id"] is None


# --- idempotent re-ingest / re-import --------------------------------------


class TestIdempotentReingestAndReimport:
    def test_reingesting_the_same_raw_file_is_byte_stable(self, tmp_path):
        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        first = curated.read_text(encoding="utf-8")
        ingest(raw, curated)
        second = curated.read_text(encoding="utf-8")
        assert first == second

    def test_reimporting_the_same_curated_file_does_not_duplicate(self, conn, tmp_path):
        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        first = import_listings(conn, curated)
        second = import_listings(conn, curated)

        assert isinstance(first, ImportStats)
        assert first.listings == second.listings == 1
        assert first.variants == second.variants == 2
        assert conn.execute(
            "SELECT count(*) FROM retailer_listings"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT count(*) FROM retailer_variants"
        ).fetchone()[0] == 2

    def test_a_reimport_updates_price_in_place(self, conn, tmp_path):
        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        raised = _raw_record()
        raised["variants"][1]["variant_options"][0]["option_price"] = 999
        raw2 = write_raw(tmp_path, raised)
        curated2 = tmp_path / "curated2.jsonl"
        ingest(raw2, curated2)
        import_listings(conn, curated2)

        assert conn.execute(
            "SELECT count(*) FROM retailer_variants"
        ).fetchone()[0] == 2, "still one row per SKU, not a duplicate"
        price = conn.execute(
            "SELECT price_usd FROM retailer_variants WHERE sku = 'S1'"
        ).fetchone()["price_usd"]
        assert price == 999


# --- unresolved report -------------------------------------------------------


class TestUnresolvedReport:
    def test_unresolved_listings_lists_null_fragrance_id_rows(self, conn, tmp_path):
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Armaf")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        rows = unresolved_listings(conn)
        assert len(rows) == 1
        assert "Delina" in rows[0]["title_raw"]
        assert rows[0]["brand_raw"] == "Armaf"

    def test_a_resolved_listing_does_not_appear(self, conn, tmp_path):
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"])
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Parfums de Marly")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        assert unresolved_listings(conn) == []

    def test_render_unresolved_says_everything_resolved_when_empty(self):
        assert render_unresolved([]) == "Every listing resolved."

    def test_render_unresolved_names_the_retailer_title_and_brand(self, conn, tmp_path):
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Armaf")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        text = render_unresolved(unresolved_listings(conn))
        assert "Delina Eau de Parfum" in text
        assert "Armaf" in text
        assert "nordstrom" in text


# --- coverage -----------------------------------------------------------


class TestCoverage:
    def test_totals_for_one_resolved_listing_with_price_and_image(self, conn, tmp_path):
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"])
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Parfums de Marly")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        stats = coverage(conn)
        assert stats.total_listings == 1
        assert stats.resolved_listings == 1
        assert stats.listings_with_price == 1
        assert stats.listings_with_image == 1
        assert stats.fragrances_with_listing == 1
        assert stats.fragrances_with_price == 1
        assert stats.fragrances_with_image == 1
        assert stats.pct_resolved == 100.0

    def test_an_unresolved_listing_does_not_count_toward_fragrance_coverage(
        self, conn, tmp_path
    ):
        add_fragrance(conn, "Something Else")
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Armaf")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        stats = coverage(conn)
        assert stats.total_listings == 1
        assert stats.resolved_listings == 0
        assert stats.total_fragrances == 1
        assert stats.fragrances_with_listing == 0

    def test_per_fragrance_coverage_marks_bottles_with_no_listing(self, conn):
        add_fragrance(conn, "Nobody Sells This")
        rows = per_fragrance_coverage(conn)
        assert len(rows) == 1
        assert rows[0]["has_listing"] is False
        assert rows[0]["has_price"] is False
        assert rows[0]["has_image"] is False

    def test_render_coverage_reads_as_prose(self, conn, tmp_path):
        add_fragrance(conn, "Parfums de Marly Delina", brand="Parfums de Marly", aliases=["Delina"])
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Parfums de Marly")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        text = render_coverage(coverage(conn))
        assert "1 listing(s), 1 resolved (100.0%)" in text
        assert "with price: 1" in text
        assert "with image: 1" in text


# --- the corpus curated-input notice ---------------------------------------


class TestCorpusCuratedInputNotice:
    def test_empty_retailer_tables_are_named_by_the_notice(self, conn):
        from fragrance_graph.corpus import curated_input_notice

        notice = curated_input_notice(conn)
        assert "retailer_listings" in notice
        assert "retailer_variants" in notice
        assert "fragrance_graph.retail import" in notice

    def test_a_populated_import_clears_the_notice(self, conn, tmp_path):
        from fragrance_graph.corpus import curated_input_notice

        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        notice = curated_input_notice(conn)
        assert "retailer_listings" not in notice
        assert "retailer_variants" not in notice

    def test_export_corpus_never_writes_a_retail_file(self, conn, tmp_path):
        """The curated JSONL is the truth for these tables — an export
        recomputing one from the database would be a second source that
        could drift from the committed file."""
        from fragrance_graph.corpus import export_corpus

        (tmp_path / "raw_input").mkdir()
        raw = write_raw(tmp_path / "raw_input", _raw_record())
        curated = tmp_path / "raw_input" / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)

        export_dir = tmp_path / "export"
        export_dir.mkdir()
        export_corpus(conn, export_dir, force=True)

        written = {p.name for p in export_dir.iterdir()}
        assert not any("retail" in name.lower() for name in written)
        assert "retailer-listings.jsonl" not in written


# --- CLI ----------------------------------------------------------------


class TestCLI:
    def test_ingest_subcommand(self, tmp_path, capsys):
        raw = write_raw(tmp_path, _raw_record())
        out = tmp_path / "curated.jsonl"
        rc = main(["ingest", str(raw), "--out", str(out)])
        assert rc == 0
        assert "Kept 1 of 1" in capsys.readouterr().out
        assert out.exists()

    def test_ingest_reports_rejected_examples(self, tmp_path, capsys):
        lipstick = _raw_record(
            item_id="999", title="Velvet Matte Lipstick",
            product_category="Home>Beauty>Makeup>Lips",
        )
        raw = write_raw(tmp_path, _raw_record(), lipstick)
        out = tmp_path / "curated.jsonl"
        rc = main(["ingest", str(raw), "--out", str(out)])
        assert rc == 0
        text = capsys.readouterr().out
        assert "rejected 1" in text
        assert "Velvet Matte Lipstick" in text

    def test_import_subcommand(self, conn, db_url, tmp_path, capsys):
        raw = write_raw(tmp_path, _raw_record())
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        conn.close()

        rc = main(["--db-url", db_url, "import", "--path", str(curated)])
        assert rc == 0
        text = capsys.readouterr().out
        assert "Imported 1 listing(s), 2 variant(s)." in text

    def test_unresolved_subcommand(self, conn, db_url, tmp_path, capsys):
        raw = write_raw(
            tmp_path, _raw_record(title="Delina Eau de Parfum", brand="Armaf")
        )
        curated = tmp_path / "curated.jsonl"
        ingest(raw, curated)
        import_listings(conn, curated)
        conn.close()

        rc = main(["--db-url", db_url, "unresolved"])
        assert rc == 0
        assert "Delina" in capsys.readouterr().out

    def test_coverage_subcommand(self, conn, db_url, capsys):
        conn.close()
        rc = main(["--db-url", db_url, "coverage"])
        assert rc == 0
        assert "0 listing(s)" in capsys.readouterr().out

    def test_cli_rejects_db_url_after_the_subcommand(self, tmp_path):
        """Confirms --db-url is top-level, matching every sibling CLI."""
        with pytest.raises(SystemExit):
            main(["ingest", str(tmp_path / "x.json"), "--db-url", "x"])


class TestSeedingFromListings:
    """The shelf grows the catalogue, with the same guards verify() has."""

    def _listing(self, conn, item_id, brand, title):
        conn.execute(
            """INSERT INTO retailer_listings
               (retailer, retailer_item_id, url, title_raw, brand_raw,
                retrieved_at, collection_mode, rights_basis, image_ref_only)
               VALUES ('nordstrom', %s, 'https://x', %s, %s,
                       '2026-08-17T00:00:00Z', 'test', 'test', true)""",
            (item_id, title, brand),
        )
        conn.commit()

    def _house(self, conn, name):
        conn.execute(
            "INSERT INTO houses (house_id, name) VALUES (%s, %s)"
            " ON CONFLICT DO NOTHING",
            (f"h-{name.lower().replace(' ', '-')}", name),
        )
        conn.commit()

    def test_a_known_house_bottle_seeds_and_then_resolves(self, conn):
        from fragrance_graph.retail import resolve_listings, seed_from_listings

        self._house(conn, "Chanel")
        self._listing(conn, "1", "CHANEL", "Coco Mademoiselle Eau de Parfum")
        stats = seed_from_listings(conn)
        assert stats["seeded"] == 1
        row = conn.execute(
            "SELECT canonical_name, brand FROM fragrances"
        ).fetchone()
        assert row["canonical_name"] == "Chanel Coco Mademoiselle"
        resolve_listings(conn)
        linked = conn.execute(
            "SELECT count(*) FROM retailer_listings WHERE fragrance_id IS NOT NULL"
        ).fetchone()[0]
        assert linked == 1

    def test_unknown_brand_and_sets_and_junk_are_refused(self, conn):
        from fragrance_graph.retail import seed_from_listings

        self._house(conn, "Chanel")
        self._listing(conn, "1", "Mystery House", "Real Perfume Eau de Parfum")
        self._listing(conn, "2", "CHANEL", "Coco Fragrance Set $187 Value")
        self._listing(conn, "3", "CHANEL", "3:32")
        stats = seed_from_listings(conn)
        assert stats["seeded"] == 0
        assert stats["unknown_brand"] == 1
        assert stats["set_like"] == 1
        assert stats["junk_or_empty"] == 1

    def test_two_concentrations_collapse_to_one_bottle_documented(self, conn):
        from fragrance_graph.retail import seed_from_listings

        self._house(conn, "Dior")
        self._listing(conn, "1", "DIOR", "Sauvage Eau de Toilette")
        self._listing(conn, "2", "DIOR", "Sauvage Eau de Parfum")
        stats = seed_from_listings(conn)
        assert stats["seeded"] == 1
        assert stats["already_seeded_this_run"] == 1

    def test_reseeding_is_idempotent(self, conn):
        from fragrance_graph.retail import resolve_listings, seed_from_listings

        self._house(conn, "Chanel")
        self._listing(conn, "1", "CHANEL", "Coco Mademoiselle Eau de Parfum")
        seed_from_listings(conn)
        resolve_listings(conn)
        stats = seed_from_listings(conn)
        assert stats["seeded"] == 0
        n = conn.execute("SELECT count(*) FROM fragrances").fetchone()[0]
        assert n == 1

    def test_le_parfum_strips_whole_never_leaving_a_dangling_article(self):
        from fragrance_graph.retail import strip_concentration_suffix

        assert strip_concentration_suffix("Black Opium Le Parfum") == "Black Opium"
        assert strip_concentration_suffix("Paradigme Le Parfum") == "Paradigme"

    def test_wrapping_quotes_are_stripped_but_possessives_survive(self, conn):
        from fragrance_graph.retail import seed_from_listings

        self._house(conn, "Dior")
        self._house(conn, "Kilian Paris")
        self._listing(conn, "1", "DIOR", "'Hypnotic Poison' Eau de Parfum")
        self._listing(conn, "2", "Kilian Paris", "Angels' Share Eau de Parfum")
        seed_from_listings(conn)
        names = sorted(r["canonical_name"] for r in
                       conn.execute("SELECT canonical_name FROM fragrances"))
        assert names == ["Dior Hypnotic Poison", "Kilian Paris Angels' Share"]
