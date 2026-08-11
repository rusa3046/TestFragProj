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
    assert "name differs from the mention" in p.note


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


# --- settling flankers without fragrance knowledge ---------------------------


def test_distinguishing_words_isolates_the_flanker_word():
    from fragrance_graph.resolve.enrich import distinguishing_words

    assert distinguishing_words("Club de Nuit", "Club de Nuit Sillage") == ["sillage"]
    assert distinguishing_words("Layton", "Layton Exclusif") == ["exclusif"]
    assert distinguishing_words("Khamrah", "Lattafa Khamrah") == ["lattafa"]
    assert distinguishing_words("Layton", "Layton") == []


def test_corpus_support_counts_who_actually_wrote_the_word(conn):
    """The signal that settles a flanker without knowing any fragrance trivia.

    A reviewer should not need to know which Club de Nuit is the famous
    Aventus clone. They need to know that nobody in their corpus ever
    wrote "sillage".
    """
    from fragrance_graph.resolve.enrich import corpus_support

    target = add_fragrance(conn, "Creed Aventus")
    for i, subject in enumerate(
        ["club de nuit", "Club de Nuit Intense Man", "CDNIM"]
    ):
        cid = add_comment(conn, i, body="x", author=f"a{i}")
        conn.execute(
            """INSERT INTO claims
               (comment_id, claim_type, subject_kind, raw_subject_text,
                object_kind, raw_object_text, object_frag_id, sentiment,
                confidence, evidence_span, evidence_verified,
                extraction_model, created_at)
               VALUES (?, 'DUPE_OF', 'FRAGRANCE', ?, 'FRAGRANCE', 'Aventus',
                       ?, 'POSITIVE', 0.9, 'e', 1, 't', 'd')""",
            (cid, subject, target),
        )
    conn.commit()

    assert corpus_support(conn, ["sillage"]) == 0, "nobody wrote it"
    assert corpus_support(conn, ["intense", "man"]) == 1
    assert corpus_support(conn, []) == -1, "no distinguishing word at all"


def test_a_flanker_nobody_mentions_is_flagged_loudly(conn):
    """The case the doc's first draft got wrong.

    Explaining this via "the Aventus clone is Intense Man" needs outside
    knowledge. The corpus count needs none: the catalogue proposed
    Sillage, and the word "sillage" appears nowhere.
    """
    p = propose_for(
        "Club de Nuit",
        7,
        [
            catalogue_row("Club de Nuit Sillage", brand="Armaf"),
            catalogue_row("Club de Nuit Intense Man", brand="Armaf"),
        ],
        support={"Club de Nuit Sillage": 0, "Club de Nuit Intense Man": 9},
    )

    assert p.corpus_mentions == 0
    assert "nobody in the corpus wrote" in p.note
    assert "better supported" in p.note
    assert p.alternatives[0]["corpus_mentions"] == 9


def test_a_well_supported_match_is_not_flagged(conn):
    p = propose_for(
        "Khamrah", 25, [catalogue_row("Lattafa Khamrah")],
        support={"Lattafa Khamrah": 12},
    )
    assert "nobody in the corpus wrote" not in p.note


def test_a_name_adding_nothing_is_the_plain_bottle(conn):
    """-1 means the catalogue name is the mention, so there is no flanker
    question to answer."""
    p = propose_for("Layton", 83, [catalogue_row("Layton", brand="Parfums de Marly")],
                    support={"Layton": -1})
    assert p.corpus_mentions == -1
    assert "nobody in the corpus wrote" not in p.note


# --- the brand prefix made the auto-rule unreachable -------------------------


def test_the_house_is_not_a_flanker_qualifier():
    """The bug that made auto-curation impossible.

    People write bare names; catalogues return the house. Without the
    brand, "Layton" against "Parfums de Marly Layton" yields three words
    that read as flanker qualifiers and are nothing of the kind.
    """
    from fragrance_graph.resolve.enrich import distinguishing_words as dw

    assert dw("Layton", "Parfums de Marly Layton") == ["parfums", "de", "marly"]
    assert dw("Layton", "Parfums de Marly Layton", "Parfums de Marly") == []


@pytest.mark.parametrize(
    "mention,candidate,brand,expected",
    [
        ("Khamrah", "Lattafa Khamrah Qahwa", "Lattafa", ["qahwa"]),
        ("Layton", "Parfums de Marly Layton Exclusif", "Parfums de Marly",
         ["exclusif"]),
        ("Club de Nuit", "Armaf Club de Nuit Sillage", "Armaf", ["sillage"]),
        ("Sauvage", "Dior Eau Sauvage", "Dior", ["eau"]),
    ],
)
def test_flankers_survive_brand_exclusion(mention, candidate, brand, expected):
    """Excluding the house must not blunt the thing the rule is for.

    `Eau Sauvage` is the one that matters most: Dior's 1966 citrus shares a
    word with the 2015 Sauvage everyone discusses, and attaching modern
    clone talk to it would be the worst merge available.
    """
    from fragrance_graph.resolve.enrich import distinguishing_words

    assert distinguishing_words(mention, candidate, brand) == expected


def test_similarity_is_measured_against_the_debranded_name():
    """`CONFIDENT` had the same brand problem as the word comparison.

    "Khamrah" against "Lattafa Khamrah" scores 0.64 and fails the gate,
    despite being the exact bottle.
    """
    from fragrance_graph.resolve.enrich import CONFIDENT, debranded
    from fragrance_graph.resolve.names import similarity

    assert similarity("Khamrah", "Lattafa Khamrah") < CONFIDENT
    assert similarity("Khamrah", debranded("Lattafa Khamrah", "Lattafa")) == 1.0


def test_a_plain_bottle_reaches_the_auto_rule(conn):
    """End to end: the two gates a proposal must clear to be auto-approved."""
    from fragrance_graph.resolve.enrich import (
        CONFIDENT,
        corpus_support,
        debranded,
        distinguishing_words,
    )
    from fragrance_graph.resolve.names import similarity

    mention, name, brand = "Khamrah", "Lattafa Khamrah", "Lattafa"
    words = distinguishing_words(mention, name, brand)

    assert corpus_support(conn, words, mention=mention) == -1
    assert similarity(mention, debranded(name, brand)) >= CONFIDENT
