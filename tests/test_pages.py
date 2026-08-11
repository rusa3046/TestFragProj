"""Phase D: the gate, and the trust rules a rendered page has to carry.

Most of this file is about pages that must *not* exist. A thin page is
worse than no page — it implies a consensus the corpus cannot support — so
the gate is the feature, and every test below that asserts an empty result
is asserting the product's central editorial rule rather than an edge case.
"""

import json

import pytest

from fragrance_graph.ingest.store import ingest
from fragrance_graph.pages import (
    MIN_COMMENTERS,
    MIN_SOURCES,
    build,
    qualifying_pairs,
    render_pair,
    slugify,
)
from fragrance_graph.query import pair_stats
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def add_comment(conn, i, *, body, author, video):
    """One comment, by a named person, under a named video."""
    ingest(
        conn,
        [
            make_comment(
                i,
                body=body,
                permalink=f"https://example.test/c/{i}",
                raw_json=json.dumps({"author": author, "videoId": video}),
            )
        ],
    )
    return conn.execute(
        "SELECT id FROM comments WHERE source_id = ?", (f"t1_fake{i:05d}",)
    ).fetchone()[0]


def add_claim(
    conn,
    comment_id,
    *,
    subject,
    obj,
    claim_type="SIMILAR_TO",
    evidence="smells the same",
    verified=1,
    polarity="ASSERTED",
):
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             polarity, extraction_model, created_at)
        VALUES (?, ?, 'FRAGRANCE', 'subject', ?, 'FRAGRANCE', 'object', ?,
                'POSITIVE', 0.9, ?, ?, ?, 'test', '2026-01-01')
        """,
        (comment_id, claim_type, subject, obj, evidence, verified, polarity),
    )
    conn.commit()


def pair_of(conn, *, people, videos, claim_type="SIMILAR_TO"):
    """Two bottles connected by `people` distinct commenters over `videos`."""
    a = add_fragrance(conn, "Aventus")
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    for i in range(people):
        cid = add_comment(
            conn,
            i,
            body=f"person {i} wrote this",
            author=f"person-{i}",
            video=f"vid-{i % videos}",
        )
        add_claim(
            conn, cid, subject=b, obj=a, claim_type=claim_type,
            evidence=f"person {i} wrote this",
        )
    return a, b


# --- the gate --------------------------------------------------------------


def test_a_pair_clearing_both_bars_gets_a_page(conn):
    pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
    pairs = qualifying_pairs(conn)
    assert len(pairs) == 1
    assert pairs[0].commenters == MIN_COMMENTERS
    assert pairs[0].sources == MIN_SOURCES


def test_too_few_commenters_gets_no_page(conn):
    """Two people agreeing is not the community saying something."""
    pair_of(conn, people=MIN_COMMENTERS - 1, videos=MIN_SOURCES)
    assert qualifying_pairs(conn) == []


def test_three_commenters_in_one_video_gets_no_page(conn):
    """The failure `min_sources` exists for.

    Three people in a single comment section may be replying to each other.
    The commenter bar is satisfied and the page must still not exist,
    because "3 people said this" would imply three independent
    observations that one thread cannot supply.
    """
    pair_of(conn, people=MIN_COMMENTERS + 2, videos=1)
    assert qualifying_pairs(conn) == []


def test_one_person_repeating_themselves_is_not_a_crowd(conn):
    """Four comments, one author, four videos. Rows would say four."""
    a = add_fragrance(conn, "Aventus")
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    for i in range(4):
        cid = add_comment(conn, i, body="x", author="superfan", video=f"vid-{i}")
        add_claim(conn, cid, subject=b, obj=a)
    assert qualifying_pairs(conn) == []


def test_denials_cannot_build_a_page(conn):
    """A denial says the edge does not exist. Counting it would quote a
    real person as evidence for the opposite of what they wrote."""
    a = add_fragrance(conn, "Aventus")
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    for i in range(4):
        cid = add_comment(conn, i, body="x", author=f"p{i}", video=f"vid-{i % 2}")
        add_claim(conn, cid, subject=b, obj=a, polarity="DENIED")
    assert qualifying_pairs(conn) == []


def test_unverified_evidence_cannot_build_a_page(conn):
    """A span that was not found in the comment is a paraphrase, and a
    page whose evidence cannot be shown is not a page."""
    a = add_fragrance(conn, "Aventus")
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    for i in range(4):
        cid = add_comment(conn, i, body="x", author=f"p{i}", video=f"vid-{i % 2}")
        add_claim(conn, cid, subject=b, obj=a, verified=0)
    assert qualifying_pairs(conn) == []


def test_a_self_edge_never_becomes_a_page(conn):
    """The corpus contains "Cedrat Boise is a dupe of Cedrat Boise" — two
    mentions that resolved to one bottle. It is not a pair."""
    only = add_fragrance(conn, "Mancera Cedrat Boise")
    for i in range(4):
        cid = add_comment(conn, i, body="x", author=f"p{i}", video=f"vid-{i % 2}")
        add_claim(conn, cid, subject=only, obj=only)
    assert qualifying_pairs(conn) == []


def test_one_pair_produces_exactly_one_page(conn, tmp_path):
    """A pair is discovered from both ends. It must not render twice, and
    the surviving orientation must not depend on iteration order."""
    pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
    pairs = build(conn, tmp_path / "site")
    assert len(pairs) == 1
    written = sorted(p.name for p in (tmp_path / "site").glob("*.html"))
    assert written == ["aventus-vs-club-de-nuit-intense-man.html", "index.html"]


# --- the pair is counted as a pair, not as one end of one ------------------


def test_pair_stats_is_direction_blind(conn):
    """`BETTER_THAN` only surfaces from the subject's end, so asking from
    one side under-counts the people who connected the two bottles.

    Three people say B is similar to A; a fourth says A beats B. Asked
    from B, that fourth person is invisible — their claim is not a
    recommendation *for* B and must not be read back as one. They still
    connected the two bottles, and the page is about the pair.
    """
    a, b = pair_of(conn, people=3, videos=2)
    cid = add_comment(conn, 90, body="a beats b", author="fourth", video="vid-9")
    add_claim(conn, cid, subject=a, obj=b, claim_type="BETTER_THAN",
              evidence="a beats b")

    assert pair_stats(conn, a, b) == (4, 3)
    assert pair_stats(conn, b, a) == (4, 3)

    pairs = qualifying_pairs(conn)
    assert len(pairs) == 1
    assert pairs[0].commenters == 4


# --- trust rules -----------------------------------------------------------


def test_no_page_can_emit_an_image(conn, tmp_path):
    """Naming a fragrance identifies it; showing a brand's bottle borrows
    its authority. This is a product rule, so it is asserted rather than
    reviewed."""
    pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
    build(conn, tmp_path / "site")
    for page in (tmp_path / "site").glob("*.html"):
        text = page.read_text(encoding="utf-8").lower()
        assert "<img" not in text
        assert "background-image" not in text
        assert "<svg" not in text


def test_a_comment_containing_markup_is_rendered_not_executed(conn, tmp_path):
    """Quotes are other people's text and reach the page verbatim by
    design, which is exactly why they are escaped."""
    a = add_fragrance(conn, "Aventus")
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    nasty = '<script>alert("xss")</script> & "quoted"'
    for i in range(MIN_COMMENTERS):
        cid = add_comment(conn, i, body=nasty, author=f"p{i}", video=f"vid-{i % 2}")
        add_claim(conn, cid, subject=b, obj=a, evidence=nasty)

    (page,) = qualifying_pairs(conn)
    html = render_pair(page)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # The words the person wrote survive; only the syntax is neutralised.
    assert "alert" in html


def test_a_fragrance_name_containing_markup_is_escaped(conn):
    """Canonical names are curated by hand, so this is defence in depth
    rather than a threat model — but a page builder that escapes only the
    fields it expects to be dangerous escapes the wrong set eventually."""
    a = add_fragrance(conn, 'Aventus <b>"real"</b> & co')
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    for i in range(MIN_COMMENTERS):
        cid = add_comment(conn, i, body="x", author=f"p{i}", video=f"vid-{i % 2}")
        add_claim(conn, cid, subject=b, obj=a, evidence="x")

    html = render_pair(qualifying_pairs(conn)[0])
    assert "<b>" not in html
    assert "&lt;b&gt;" in html


def test_pages_never_read_the_commerce_tables(conn, tmp_path):
    """The README promises result order is computed with no knowledge of
    what is monetizable. Here that is a missing capability rather than a
    promise: dropping the tables entirely changes nothing about the output.
    """
    pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
    before = render_pair(qualifying_pairs(conn)[0])

    conn.executescript("DROP TABLE IF EXISTS products; DROP TABLE IF EXISTS retailers;")
    conn.commit()

    assert render_pair(qualifying_pairs(conn)[0]) == before


# --- output stability ------------------------------------------------------


def test_rebuilding_an_unchanged_corpus_rewrites_identical_bytes(conn, tmp_path):
    """A diff in site/ should mean the corpus changed. Nothing else."""
    pair_of(conn, people=MIN_COMMENTERS + 1, videos=MIN_SOURCES)
    out = tmp_path / "site"

    build(conn, out)
    first = {p.name: p.read_bytes() for p in sorted(out.glob("*.html"))}
    build(conn, out)
    second = {p.name: p.read_bytes() for p in sorted(out.glob("*.html"))}

    assert first == second


def test_an_empty_graph_still_writes_an_index(conn, tmp_path):
    """Zero pages is a legitimate state — it is what the gate is for — and
    a build that half-succeeds is worse than one that says nothing
    qualified."""
    pairs = build(conn, tmp_path / "site")
    assert pairs == []
    index = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "0 comparisons" in index


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Kilian Angels' Share", "kilian-angels-share"),
        ("Abercrombie & Fitch Fierce", "abercrombie-fitch-fierce"),
        ("Al Haramain L'Aventure Intense", "al-haramain-l-aventure-intense"),
        ("Détour Noir", "d-tour-noir"),
        ("!!!", "unnamed"),
    ],
)
def test_slugify_survives_real_fragrance_names(name, expected):
    """Apostrophes, ampersands and accents are ordinary in this domain."""
    assert slugify(name) == expected
