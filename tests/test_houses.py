"""Curated fragrance houses: import, and what the resolver may see.

`data/curation/houses.jsonl` is fixed and committed — these tests build
their own tiny files rather than depending on the shape of the 204 real
rows, so a future edit to the curated data cannot silently break what this
file is actually guarding: idempotent import, the case-insensitive
uniqueness guard, and `known_house_names` including houses not yet cleared
for display.
"""

import json

import psycopg
import pytest

from fragrance_graph.houses import import_houses, known_house_names, main


def write_houses(tmp_path, *rows):
    path = tmp_path / "houses.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def house_row(**overrides):
    row = {
        "house_id": "H001",
        "name": "Initio Parfums Privés",
        "category": "Niche, heritage & independent",
        "wikidata_qid": "Q140002803",
        "official_website": "https://initioparfums.com/",
        "source_url": "https://www.wikidata.org/wiki/Q140002803",
        "resolution_status": "matched",
        "match_basis": "exact_label",
        "publish_status": "publish",
        "wikidata_description": "French perfume brand",
    }
    row.update(overrides)
    return row


# --- import -------------------------------------------------------------


def test_import_is_idempotent(conn, tmp_path):
    path = write_houses(tmp_path, house_row())
    first = import_houses(conn, path)
    second = import_houses(conn, path)

    assert first == 1 and second == 1
    assert conn.execute("SELECT count(*) FROM houses").fetchone()[0] == 1


def test_a_changed_row_updates_rather_than_duplicates(conn, tmp_path):
    path = write_houses(tmp_path, house_row(wikidata_description="French brand"))
    import_houses(conn, path)

    path = write_houses(
        tmp_path, house_row(wikidata_description="French perfume brand, corrected")
    )
    import_houses(conn, path)

    assert conn.execute("SELECT count(*) FROM houses").fetchone()[0] == 1
    row = conn.execute(
        "SELECT wikidata_description FROM houses WHERE house_id = 'H001'"
    ).fetchone()
    assert row["wikidata_description"] == "French perfume brand, corrected"


def test_a_null_source_url_does_not_break_the_import(conn, tmp_path):
    """43 of the 204 real rows have no Wikidata item to link a source to."""
    path = write_houses(
        tmp_path, house_row(source_url=None, official_website=None, wikidata_qid=None)
    )
    assert import_houses(conn, path) == 1
    row = conn.execute("SELECT source_url FROM houses").fetchone()
    assert row["source_url"] is None


def test_a_row_missing_a_not_null_field_imports_with_the_default(conn, tmp_path):
    """A JSONL row missing `category` entirely used to fail the import
    outright: `record.get("category")` yields `None`, and the INSERT names
    every column explicitly, so the column's own `DEFAULT ''` never fires
    — that only applies when a column is omitted from the statement, not
    when an explicit NULL is bound to it. `_house_values` restores the
    default in Python instead. The committed `houses.jsonl` never omits a
    key, so this is a trap for the next hand-edit, not today's data."""
    path = tmp_path / "houses.jsonl"
    row = house_row()
    del row["category"]
    path.write_text(json.dumps(row) + "\n")

    assert import_houses(conn, path) == 1
    imported = conn.execute(
        "SELECT category FROM houses WHERE house_id = 'H001'"
    ).fetchone()
    assert imported["category"] == ""


# --- uniqueness -----------------------------------------------------------


def test_unique_name_guard_is_case_insensitive(conn, tmp_path):
    """Mirrors migration 0009's guard on `fragrances`: two rows differing
    only by case are one house, and the resolver must not have to guess
    which row is the real one."""
    path = write_houses(tmp_path, house_row())
    import_houses(conn, path)

    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(
            "INSERT INTO houses (house_id, name) VALUES (%s, %s)",
            ("H999", "INITIO PARFUMS PRIVÉS"),
        )
    conn.rollback()


# --- known_house_names ------------------------------------------------------


def test_known_house_names_includes_review_houses(conn, tmp_path):
    """publish_status governs the site's own house pages, not resolution.
    A house nobody has cleared to display is still a real house."""
    path = write_houses(
        tmp_path,
        house_row(house_id="H001", name="Published House", publish_status="publish"),
        house_row(house_id="H002", name="Unpublished House", publish_status="review"),
    )
    import_houses(conn, path)

    names = known_house_names(conn)
    assert "published house" in names
    assert "unpublished house" in names


def test_known_house_names_is_lowercased(conn, tmp_path):
    path = write_houses(tmp_path, house_row(name="Maison Alhambra"))
    import_houses(conn, path)

    names = known_house_names(conn)
    assert "maison alhambra" in names
    assert "Maison Alhambra" not in names


# --- CLI --------------------------------------------------------------------


def test_cli_import_takes_db_url_like_siblings(db_url, tmp_path, capsys):
    """The convention every sibling CLI follows: `--db-url` is a top-level
    flag, not one scoped to a subcommand — see attributes.py, semantic.py."""
    path = write_houses(tmp_path, house_row())

    rc = main(["--db-url", db_url, "import", "--path", str(path)])

    assert rc == 0
    assert "Imported 1 house(s)" in capsys.readouterr().out


def test_cli_rejects_db_url_after_the_subcommand(tmp_path):
    """Confirms --db-url really is top-level, the way it is documented to
    be: passing it after the subcommand is a parse error, not a silent
    accept."""
    with pytest.raises(SystemExit):
        main(["import", "--path", str(tmp_path / "x.jsonl"), "--db-url", "x"])
