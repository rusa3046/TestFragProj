"""Proposing canonical names from a catalogue.

The safety property is that a name the catalogue never returned cannot be
written. Everything else here is about making a reviewer's job small
enough that curating 60-80 fragrances is an afternoon rather than a week.
"""

import json
from pathlib import Path

import pytest

from fragrance_graph.resolve.enrich import (
    CONFIDENT,
    FORBIDDEN_PATHS,
    KEPT_FIELDS,
    Proposal,
    apply_review,
    candidates,
    is_pronoun,
    propose_for,
    read_review,
    write_review,
)
from fragrance_graph.resolve.entities import add_fragrance
from tests.test_query import add_comment


def catalogue_row(name, brand="Lattafa", year="2022", **extra):
    """A catalogue response, including the fields the product must drop."""
    return {
        "_id": name.lower().replace(" ", "-"),
        "Name": name,
        "Brand": brand,
        "Year": year,
        "rating": "4.13",
        "Price": "284.99",
        "Image URL": "https://cdn.example/x.jpg",
        "Longevity": "Moderate",
        "Sillage": "Strong",
        "General Notes": ["Bergamot", "Apple"],
        "Main Accords": ["Fruity", "Woody"],
        "Purchase URL": "https://affiliate.example/buy",
        **extra,
    }


# --- the safety property ----------------------------------------------------


def test_only_catalogue_fields_survive():
    """Notes, accords, images, ratings and the affiliate link are dropped.

    Every one of those is something SPEC forbids: computed-similarity
    inputs, a text-only trust rule, and a Purchase URL that would hand a
    third party the commission Phase C exists to earn.
    """
    p = propose_for("Khamrah", 25, [catalogue_row("Lattafa Khamrah")])

    stored = {p.canonical_name, p.brand, p.year}
    assert stored == {"Lattafa Khamrah", "Lattafa", "2022"}
    blob = json.dumps(p.__dict__)
    for leaked in ("Image URL", "Purchase URL", "Main Accords", "Longevity"):
        assert leaked not in blob, f"{leaked} must not survive"


def test_kept_fields_are_exactly_three():
    assert KEPT_FIELDS == ("Name", "Brand", "Year")


class FakeResponse:
    status_code = 200

    def json(self):
        return []


class RecordingClient:
    """Captures the URLs requested, so the guard tests behaviour not text."""

    def __init__(self):
        self.urls = []

    def get(self, url, params=None, headers=None):
        self.urls.append(url)
        return FakeResponse()


def test_only_the_search_endpoint_is_ever_requested():
    """The endpoints that would replace 'people said' with 'an API said'.

    Checked by recording the requests rather than grepping the source: an
    earlier version of this test matched its own docstring, which is the
    classic way a guard passes while guarding nothing.
    """
    from fragrance_graph.resolve.enrich import BASE_URL, SEARCH_PATH, _search

    client = RecordingClient()
    _search(client, "k", "Khamrah")

    assert client.urls == [f"{BASE_URL}{SEARCH_PATH}"]
    for url in client.urls:
        for forbidden in FORBIDDEN_PATHS:
            assert forbidden not in url


def test_the_forbidden_paths_are_named_so_the_boundary_is_greppable():
    assert FORBIDDEN_PATHS == ("/fragrances/similar", "/fragrances/match")


def test_a_name_the_catalogue_did_not_return_cannot_be_written(conn):
    """The whole point: no proposal, nothing to approve, nothing written."""
    p = propose_for("Zenith Blue", 5, [])
    assert p.canonical_name is None
    assert p.note == "no catalogue match"

    p.approved = True  # even approved, there is nothing to add
    stats = apply_review(conn, [p])
    assert stats.added == 0
    assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 0


# --- review flow ------------------------------------------------------------


def test_nothing_is_written_until_a_human_approves(conn):
    p = propose_for("Khamrah", 25, [catalogue_row("Lattafa Khamrah")])
    assert p.approved is None, "review starts undecided"

    stats = apply_review(conn, [p])
    assert (stats.added, stats.skipped_unapproved) == (0, 1)

    p.approved = True
    assert apply_review(conn, [p]).added == 1


def test_rejection_is_recorded_not_silently_skipped(conn):
    p = propose_for("Khamrah", 25, [catalogue_row("Wrong Bottle")])
    p.approved = False
    stats = apply_review(conn, [p])
    assert (stats.rejected, stats.added) == (1, 0)


def test_the_mention_becomes_an_alias(conn):
    """The commenter's spelling is what the resolver has to match."""
    p = propose_for("CDNIM", 23, [catalogue_row("Club de Nuit Intense Man",
                                                brand="Armaf")])
    p.approved = True
    apply_review(conn, [p])

    aliases = json.loads(
        conn.execute("SELECT aliases FROM fragrances").fetchone()["aliases"]
    )
    assert "CDNIM" in aliases


def test_an_existing_fragrance_is_not_duplicated(conn):
    add_fragrance(conn, "Lattafa Khamrah", brand="Lattafa")
    p = propose_for("Khamrah", 25, [catalogue_row("lattafa  khamrah")])
    p.approved = True

    stats = apply_review(conn, [p])
    assert (stats.added, stats.skipped_existing) == (0, 1)


# --- flagging what needs a human --------------------------------------------


def test_a_close_name_is_marked_confident():
    p = propose_for("Khamrah", 25, [catalogue_row("Khamrah")])
    assert p.confident and p.score >= CONFIDENT


def test_a_distant_name_asks_to_be_read():
    """A mention that resolves to something unlike it is where a catalogue
    quietly attaches the wrong bottle."""
    p = propose_for("Oajan", 8, [catalogue_row("Ocean Breeze")])
    assert not p.confident
    assert "check this one" in p.note


def test_alternatives_are_carried_so_a_fix_needs_no_second_lookup():
    p = propose_for("Layton", 83, [
        catalogue_row("Layton Exclusif", brand="Parfums de Marly"),
        catalogue_row("Layton", brand="Parfums de Marly"),
        catalogue_row("Layton Royal Essence", brand="Parfums de Marly"),
    ])
    names = [a["Name"] for a in p.alternatives]
    assert "Layton" in names


# --- what is worth a request ------------------------------------------------


@pytest.mark.parametrize(
    "text", ["it", "this", "This", "this one", "this fragrance", "the og",
             "they", "the same", "the dupes"],
)
def test_pronouns_are_never_looked_up(text):
    """~148 unresolved mentions are pronouns. None can name a bottle, so a
    lookup for them is a guaranteed miss that costs a request."""
    assert is_pronoun(text)


@pytest.mark.parametrize("text", ["Khamrah", "Oud Wood", "BR540", "Layton"])
def test_real_names_are_not_mistaken_for_pronouns(text):
    assert not is_pronoun(text)


def test_candidates_skip_pronouns_and_one_offs(conn):
    target = add_fragrance(conn, "Creed Aventus")
    for i, (subject, times) in enumerate(
        [("Khamrah", 3), ("this one", 4), ("Obscure Thing", 1)]
    ):
        for n in range(times):
            cid = add_comment(conn, i * 10 + n, body="x", author=f"a{i}{n}")
            conn.execute(
                """INSERT INTO claims
                   (comment_id, claim_type, subject_kind, raw_subject_text,
                    object_kind, raw_object_text, object_frag_id, sentiment,
                    confidence, evidence_span, evidence_verified,
                    extraction_model, created_at)
                   VALUES (?, 'DUPE_OF', 'FRAGRANCE', ?, 'FRAGRANCE',
                           'Aventus', ?, 'POSITIVE', 0.9, 'e', 1, 't', 'd')""",
                (cid, subject, target),
            )
    conn.commit()

    picked = [m for m, _ in candidates(conn, limit=10)]
    assert "Khamrah" in picked
    assert "this one" not in picked, "pronoun"
    assert "Obscure Thing" not in picked, "mentioned once"


def test_candidates_are_ordered_by_frequency(conn):
    """Review effort should follow where the corpus actually is."""
    target = add_fragrance(conn, "Creed Aventus")
    for i, (subject, times) in enumerate([("Rare", 2), ("Common", 5)]):
        for n in range(times):
            cid = add_comment(conn, i * 20 + n, body="x", author=f"b{i}{n}")
            conn.execute(
                """INSERT INTO claims
                   (comment_id, claim_type, subject_kind, raw_subject_text,
                    object_kind, raw_object_text, object_frag_id, sentiment,
                    confidence, evidence_span, evidence_verified,
                    extraction_model, created_at)
                   VALUES (?, 'DUPE_OF', 'FRAGRANCE', ?, 'FRAGRANCE',
                           'Aventus', ?, 'POSITIVE', 0.9, 'e', 1, 't', 'd')""",
                (cid, subject, target),
            )
    conn.commit()

    assert [m for m, _ in candidates(conn, limit=10)][0] == "Common"


# --- the review file --------------------------------------------------------


def test_review_file_round_trips(tmp_path: Path):
    original = [
        propose_for("Khamrah", 25, [catalogue_row("Lattafa Khamrah")]),
        propose_for("Zenith Blue", 5, []),
    ]
    path = tmp_path / "review.json"
    write_review(path, original)

    assert read_review(path) == original


def test_review_file_is_stable_so_a_rerun_diffs_cleanly(tmp_path: Path):
    proposals = [propose_for("Khamrah", 25, [catalogue_row("Lattafa Khamrah")])]
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_review(a, proposals)
    write_review(b, proposals)
    assert a.read_text() == b.read_text()


def test_apply_says_why_nothing_happened(conn, tmp_path, capsys):
    """An unreviewed file applying nothing is correct but looks broken."""
    from fragrance_graph.db import get_connection, migrate
    from fragrance_graph.resolve.enrich import main

    db = tmp_path / "e.db"
    conn.close()
    fresh = get_connection(db)
    migrate(fresh)
    fresh.close()

    review = tmp_path / "r.json"
    write_review(review, [propose_for("Khamrah", 9, [catalogue_row("Lattafa Khamrah")])])

    main(["apply", str(review), "--db-path", str(db)])
    assert "nothing is approved" in capsys.readouterr().out


def test_proposal_equality_is_by_value():
    a = propose_for("Khamrah", 25, [catalogue_row("Lattafa Khamrah")])
    b = propose_for("Khamrah", 25, [catalogue_row("Lattafa Khamrah")])
    assert a == b
    assert isinstance(a, Proposal)
