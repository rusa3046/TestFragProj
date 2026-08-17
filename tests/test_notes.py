"""Declared notes: the official half of the dual truth, kept separate.

What these tests protect, in order of importance: the SEPARATION (a
declared note must never enter an evidence count or an evidence
sentence), the extraction's refusal to carry prose, and the plumbing
(upserts, syncing after resolution, honest absence).

Every extraction fixture below is the shape of a real Nordstrom
description from the first collection sample — staged lists, flat lists,
"Bottom" for base, a parenthetical explaining a molecule, a trailing
footnote digit — so the extractor is tested against reality, not against
formats invented to pass.
"""

import json

from fragrance_graph.notes import (
    DeclaredNote,
    canonical,
    declared_for,
    extract_note_lists,
    import_brand_declared,
    load_note_axes,
    store_claims,
)
from fragrance_graph.resolve.entities import add_fragrance


class TestExtractionTakesTheListAndNothingElse:
    def test_a_staged_list_lands_with_stages(self):
        # Parfums de Marly Delina, verbatim shape from the sample.
        d = ("Notes : - Top: rhubarb, lychee, bergamot essence "
             "- Middle: Turkish rose, peony, vanilla "
             "- Base: cashmeran, musk, vetiver Made in France Item #7621312")
        notes = extract_note_lists(d)
        assert ("rhubarb", "top") in [(n.raw_note, n.stage) for n in notes]
        assert ("Turkish rose", "heart") in [(n.raw_note, n.stage) for n in notes]
        assert ("vetiver", "base") in [(n.raw_note, n.stage) for n in notes]
        # The terminator worked: nothing after "Made in France" survived.
        assert not any("France" in n.raw_note or "Item" in n.raw_note
                       for n in notes)

    def test_bottom_is_base_because_burberry_says_bottom(self):
        d = ("Notes : - Top: black currant, blueberry - Middle: jasmine accord "
             "- Bottom: musk, dry amber Mood : Sensual")
        stages = {n.raw_note: n.stage for n in extract_note_lists(d)}
        assert stages["musk"] == "base"
        assert stages["dry amber"] == "base"
        assert "Sensual" not in stages

    def test_a_flat_list_is_unspecified_not_guessed(self):
        # Kilian Paris shape: no stages given, so no stages invented.
        notes = extract_note_lists("Notes : Cognac, hazelnut, oak wood")
        assert [(n.raw_note, n.stage) for n in notes] == [
            ("Cognac", "unspecified"),
            ("hazelnut", "unspecified"),
            ("oak wood", "unspecified"),
        ]

    def test_a_parenthetical_is_explanation_not_notes(self):
        # MFK shape: the parenthetical's commas must not shred the list.
        d = ("Notes : - Top: Jasmine, saffron - Middle: mossy woody accord "
             "- Base: Ethyl-maltol Ambroxan™ (amber, dry woody and "
             "mineral note) Made in France")
        notes = [n.raw_note for n in extract_note_lists(d)]
        assert "Ethyl-maltol Ambroxan" in notes
        assert not any("amber, dry" in n or "(" in n for n in notes)

    def test_a_trailing_footnote_digit_is_not_part_of_a_note(self):
        # Initio shape: "...Madagascar vanilla 3" — the 3 is a footnote.
        notes = [n.raw_note for n in extract_note_lists(
            "Notes : Italian bergamot, Madagascar vanilla 3")]
        assert "Madagascar vanilla" in notes
        assert not any(n.endswith(" 3") for n in notes)

    def test_prose_that_merely_mentions_notes_yields_nothing(self):
        """The refusal that defines the module: 'with woody ambery floral
        notes' is the retailer's voice, not a declaration. No marker, no
        extraction — an extractor that read prose would be laundering
        copy into facts."""
        assert extract_note_lists(
            "A universal, luminous fragrance with woody ambery floral "
            "notes, a one-of-a-kind olfactory signature."
        ) == []

    def test_empty_and_absent_descriptions_yield_nothing(self):
        assert extract_note_lists("") == []

    def test_canonical_lowercases_but_never_collapses_vocabulary(self):
        assert canonical("Turkish  Rose") == "turkish rose"
        # NOT collapsed to "rose": the distinction is a visible judgement.
        assert canonical("Turkish rose") != canonical("rose")


class TestStorageIsAnUpsertWithProvenance:
    def test_reimport_does_not_duplicate(self, conn):
        fid = add_fragrance(conn, "Lattafa Khamrah")
        notes = [DeclaredNote("dates", "top")]
        for _ in range(2):
            store_claims(conn, fid, notes, claim_type="retailer_declared",
                         source_name="nordstrom", source_url="https://x")
        n = conn.execute(
            "SELECT count(*) FROM fragrance_note_claim"
        ).fetchone()[0]
        assert n == 1

    def test_an_absent_curation_file_imports_zero_rows_without_error(
        self, conn, tmp_path
    ):
        written, skipped = import_brand_declared(
            conn, tmp_path / "does-not-exist.jsonl"
        )
        assert (written, skipped) == (0, 0)

    def test_brand_declared_rows_import_and_unknown_bottles_are_skipped(
        self, conn, tmp_path
    ):
        add_fragrance(conn, "Lattafa Khamrah")
        f = tmp_path / "declared-notes.jsonl"
        f.write_text(
            json.dumps({"fragrance": "Lattafa Khamrah", "raw_note": "dates",
                        "stage": "top", "source_name": "lattafa.com",
                        "source_url": "https://lattafa.com/khamrah"}) + "\n"
            + json.dumps({"fragrance": "Bottle Nobody Curated",
                          "raw_note": "rose"}) + "\n",
            encoding="utf-8",
        )
        written, skipped = import_brand_declared(conn, f)
        assert (written, skipped) == (1, 1)

    def test_declared_for_puts_the_house_before_the_retailer(self, conn):
        fid = add_fragrance(conn, "Lattafa Khamrah")
        store_claims(conn, fid, [DeclaredNote("vanilla", "base")],
                     claim_type="retailer_declared", source_name="nordstrom")
        store_claims(conn, fid, [DeclaredNote("dates", "top")],
                     claim_type="brand_declared", source_name="lattafa.com")
        rows = declared_for(conn, fid)
        assert rows[0]["declared_by"] == "brand_declared"
        assert rows[-1]["declared_by"] == "retailer_declared"


class TestTheSeparationHolds:
    """The load-bearing tests: declared notes must be invisible to the
    evidence layer. A bottle with a declared 'rose' and zero community
    claims has an official block and NO evidence — not one borrowed
    sentence, not one count moved."""

    def test_declared_notes_never_enter_evidence_counts(self, conn):
        from fragrance_graph.evidence import attribute_facts

        fid = add_fragrance(conn, "Parfums de Marly Delina")
        store_claims(conn, fid, [DeclaredNote("Turkish rose", "heart")],
                     claim_type="retailer_declared", source_name="nordstrom")
        facts = attribute_facts(conn, fragrance_id=fid)
        assert facts == [], (
            "a declared note produced an evidence fact — the separation "
            "that defines the product is broken"
        )

    def test_the_official_block_carries_data_while_evidence_stays_empty(
        self, conn
    ):
        import pytest

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from fragrance_graph.api import app, get_conn

        fid = add_fragrance(conn, "Parfums de Marly Delina")
        store_claims(conn, fid, [DeclaredNote("Turkish rose", "heart")],
                     claim_type="retailer_declared", source_name="nordstrom")
        def _override():
            yield conn

        app.dependency_overrides[get_conn] = _override
        try:
            body = TestClient(app).get(f"/api/fragrance/{fid}").json()
        finally:
            app.dependency_overrides.clear()
        assert body["official"]["declared_notes"][0]["note"] == "Turkish rose"
        # And the evidence half stayed honestly empty: no results were
        # conjured from the declaration.
        assert body["results"] == []
        joined = json.dumps(body["results"]) + body.get("note", "")
        assert "Turkish rose" not in joined

    def test_absence_is_empty_fields_never_guesses(self, conn):
        import pytest

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from fragrance_graph.api import app, get_conn

        fid = add_fragrance(conn, "Lattafa Khamrah")
        def _override():
            yield conn

        app.dependency_overrides[get_conn] = _override
        try:
            body = TestClient(app).get(f"/api/fragrance/{fid}").json()
        finally:
            app.dependency_overrides.clear()
        assert body["official"] == {"declared_notes": [], "listings": []}


class TestCompletenessIsOperationalOnly:
    def test_the_score_counts_components_and_sorts_worklist_last(self, conn):
        from fragrance_graph.retail import completeness

        rich = add_fragrance(conn, "Lattafa Khamrah")
        store_claims(conn, rich, [DeclaredNote("dates", "top")],
                     claim_type="brand_declared", source_name="lattafa.com")
        add_fragrance(conn, "Empty Bottle")
        rows = completeness(conn)
        assert rows[0]["name"] == "Lattafa Khamrah"
        assert rows[0]["components"]["declared notes"] is True
        assert rows[-1]["name"] == "Empty Bottle"
        assert rows[-1]["score"] == 0.0

    def test_no_api_response_carries_a_completeness_score(self, conn):
        """Completeness is a worklist, not product truth. If it ever
        shows up in an API payload it has become a fake match score."""
        import pytest

        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from fragrance_graph.api import app, get_conn

        fid = add_fragrance(conn, "Lattafa Khamrah")

        def _override():
            yield conn

        app.dependency_overrides[get_conn] = _override
        try:
            body = TestClient(app).get(f"/api/fragrance/{fid}").json()
        finally:
            app.dependency_overrides.clear()
        assert "completeness" not in json.dumps(body).lower()


class TestNoteAxisLoader:
    """`load_note_axes` — the curated note -> axis mapping
    (`data/curation/note-axes.json`) that `recommend._anchor_profile_
    fallback` subtracts an avoided axis's notes with. The file is
    authored judgment, committed for review; this loader does not trust
    it blindly — every entry is re-checked against whatever
    `fragrance_note_claim` actually holds *right now*, on the connection
    it was given, because the corpus underneath the file keeps changing
    after the file was written.
    """

    def test_entries_absent_from_the_corpus_vocabulary_are_dropped_with_a_count(
        self, conn, caplog, tmp_path
    ):
        frag = add_fragrance(conn, "Some Bottle")
        store_claims(
            conn, frag, [DeclaredNote("amber", "unspecified")],
            claim_type="retailer_declared", source_name="test",
        )
        axes_file = tmp_path / "note-axes.json"
        axes_file.write_text(json.dumps({
            "warm": ["amber", "not-in-this-corpus-at-all"],
        }))

        with caplog.at_level("INFO", logger="fragrance_graph.notes"):
            axes = load_note_axes(conn, path=axes_file)

        # The survivor: "amber" really is in this test's own
        # fragrance_note_claim table.
        assert axes["warm"] == frozenset({"amber"})
        # The count of what did not survive, logged rather than silently
        # dropped — "nothing was dropped" and "the check never ran" must
        # not read the same in the log.
        assert any("1 note-axis entr" in r.message for r in caplog.records)

    def test_an_axis_the_file_never_curated_is_none_not_an_empty_set(
        self, conn, tmp_path
    ):
        """`recommend._anchor_profile_fallback` tells "no axis by this
        name" apart from "a curated axis with nothing on it" by this
        distinction: `.get(axis)` is `None` for the former, `frozenset()`
        for the latter. Collapsing them would turn a typo'd axis name
        into "this axis subtracts nothing", identical to a real,
        deliberately empty axis — the wrong silent failure for a typo."""
        axes_file = tmp_path / "note-axes.json"
        axes_file.write_text(json.dumps({"warm": []}))

        axes = load_note_axes(conn, path=axes_file)

        assert axes.get("sweet") is None
        assert axes.get("warm") == frozenset()

    def test_a_metadata_key_is_not_read_as_an_axis(self, conn, tmp_path):
        frag = add_fragrance(conn, "Some Bottle")
        store_claims(
            conn, frag, [DeclaredNote("amber", "unspecified")],
            claim_type="retailer_declared", source_name="test",
        )
        axes_file = tmp_path / "note-axes.json"
        axes_file.write_text(json.dumps({
            "_comment": ["curation notes, not an axis"],
            "warm": ["amber"],
        }))

        axes = load_note_axes(conn, path=axes_file)

        assert set(axes) == {"warm"}

    def test_a_missing_file_warns_and_every_axis_resolves_as_unknown(
        self, conn, tmp_path, caplog
    ):
        """The same shape as `pages.load_blocklist`'s missing-file
        handling, for the identical reason: an absent curation file must
        not read, downstream, as "nothing to subtract because nobody
        asked" — it is "nobody has curated this yet"."""
        missing = tmp_path / "does-not-exist.json"

        with caplog.at_level("WARNING", logger="fragrance_graph.notes"):
            axes = load_note_axes(conn, path=missing)

        assert axes == {}
        assert axes.get("warm") is None
        assert any("No note-axis mapping" in r.message for r in caplog.records)

    def test_the_real_curated_file_only_carries_survivors(self, conn):
        """The file this repo ships, loaded against a corpus with none of
        its notes declared: every entry must vanish, proving the loader
        actually filters against the connection it is given rather than
        trusting the file's own — already pre-filtered — contents."""
        axes = load_note_axes(conn)  # default path, empty test corpus
        assert axes.get("warm") == frozenset()
        assert axes.get("sweet") == frozenset()
        assert axes.get("fresh") == frozenset()
