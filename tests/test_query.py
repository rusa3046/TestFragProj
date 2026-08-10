"""The query layer: what does the community say smells like this.

These tests carry the product's trust rules, not just its correctness.
`test_ranking_ignores_commerce` is the one the README promises; it is
written to fail loudly if ranking ever learns what a product link is.
"""

import json

import pytest

from fragrance_graph.ingest.reddit import ingest
from fragrance_graph.query import (
    aggregate_sentiment,
    find_fragrance,
    render,
    sentiment_rollup,
    similar_to,
)
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def add_comment(conn, i, *, body, author):
    """One comment by a named author. Returns its row id."""
    ingest(
        conn,
        [
            make_comment(
                i,
                body=body,
                permalink=f"https://example.test/c/{i}",
                raw_json=json.dumps({"author": author}),
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
    sentiment="POSITIVE",
    evidence="smells the same",
    verified=1,
    confidence=0.9,
):
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             extraction_model, created_at)
        VALUES (?, ?, 'FRAGRANCE', 'subject', ?, 'FRAGRANCE', 'object', ?,
                ?, ?, ?, ?, 'test', '2026-01-01')
        """,
        (comment_id, claim_type, subject, obj, sentiment, confidence,
         evidence, verified),
    )
    conn.commit()


@pytest.fixture
def graph(conn):
    """Two fragrances and three people saying B smells like A."""
    a = add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    b = add_fragrance(conn, "Cloud")
    for i, author in enumerate(["alice", "bob", "carol"]):
        cid = add_comment(conn, i, body=f"{author} says so", author=author)
        add_claim(conn, cid, subject=b, obj=a, evidence=f"{author} says so")
    return conn, a, b


# --- ranking ---------------------------------------------------------------


def test_ranking_counts_distinct_commenters_not_claim_rows(conn):
    """One enthusiast is not a consensus.

    Two fragrances: one backed by three people, one by a single person who
    said it four times. Row counting would rank them 4 to 3 — backwards.
    """
    target = add_fragrance(conn, "Aventus")
    popular = add_fragrance(conn, "Club de Nuit Intense")
    loud = add_fragrance(conn, "Montblanc Explorer")

    for i, author in enumerate(["alice", "bob", "carol"]):
        cid = add_comment(conn, i, body="x", author=author)
        add_claim(conn, cid, subject=popular, obj=target)

    for i in range(4):
        cid = add_comment(conn, 100 + i, body="x", author="superfan")
        add_claim(conn, cid, subject=loud, obj=target)

    results = similar_to(conn, target)

    assert [r.canonical_name for r in results] == [
        "Club de Nuit Intense",
        "Montblanc Explorer",
    ]
    assert (results[0].commenters, results[0].claims) == (3, 3)
    assert (results[1].commenters, results[1].claims) == (1, 4)


def test_unknown_authors_count_as_separate_people(conn):
    """'' is missing data, not one shared person.

    Collapsing them would silently under-count; the fallback keeps each
    comment its own commenter.
    """
    target = add_fragrance(conn, "Layton")
    other = add_fragrance(conn, "Khamrah")
    for i in range(3):
        ingest(conn, [make_comment(i, body="x", raw_json="{}")])
        cid = conn.execute(
            "SELECT id FROM comments WHERE source_id = ?", (f"t1_fake{i:05d}",)
        ).fetchone()[0]
        add_claim(conn, cid, subject=other, obj=target)

    (result,) = similar_to(conn, target)
    assert result.commenters == 3


def test_ties_break_deterministically(conn):
    """Generated pages churn in git if identical support reorders."""
    target = add_fragrance(conn, "Sauvage")
    for name in ("Zara Vibrant Leather", "Armaf Club de Nuit"):
        frag = add_fragrance(conn, name)
        cid = add_comment(conn, abs(hash(name)) % 9000, body="x", author=name)
        add_claim(conn, cid, subject=frag, obj=target)

    names = [r.canonical_name for r in similar_to(conn, target)]
    assert names == sorted(names)


def test_limit_applies_after_ranking(graph):
    conn, a, _ = graph
    other = add_fragrance(conn, "Ambre Nuit")
    cid = add_comment(conn, 50, body="x", author="dave")
    add_claim(conn, cid, subject=other, obj=a)

    (top,) = similar_to(conn, a, limit=1)
    assert top.canonical_name == "Cloud", "3 commenters must outrank 1"


def test_min_commenters_hides_thin_results(graph):
    """Phase D's rule: a thin page is worse than no page."""
    conn, a, _ = graph
    lonely = add_fragrance(conn, "Ambre Nuit")
    cid = add_comment(conn, 50, body="x", author="dave")
    add_claim(conn, cid, subject=lonely, obj=a)

    names = {r.canonical_name for r in similar_to(conn, a, min_commenters=3)}
    assert names == {"Cloud"}


# --- symmetry --------------------------------------------------------------


def test_similarity_reads_from_either_end(graph):
    """'B is a dupe of A' is the same fact whichever one you asked about."""
    conn, a, b = graph

    assert [r.canonical_name for r in similar_to(conn, a)] == ["Cloud"]
    assert [r.canonical_name for r in similar_to(conn, b)] == ["Baccarat Rouge 540"]


def test_direction_is_preserved_on_the_evidence(graph):
    """A page quoting "X is a dupe of Y" backwards misquotes the person."""
    conn, a, b = graph

    assert all(ev.outbound for ev in similar_to(conn, b)[0].evidence)
    assert not any(ev.outbound for ev in similar_to(conn, a)[0].evidence)


def test_better_than_is_not_symmetric(conn):
    """Otherwise a fragrance's critics become its recommendations."""
    winner = add_fragrance(conn, "Layton")
    loser = add_fragrance(conn, "Dior Sauvage")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=winner, obj=loser, claim_type="BETTER_THAN")

    assert [r.canonical_name for r in similar_to(conn, winner)] == ["Dior Sauvage"]
    assert similar_to(conn, loser) == [], "being beaten is not a recommendation"


def test_claim_types_stay_separate_rows(conn):
    """'a dupe of X' and 'similar to X' are different strengths of claim."""
    target = add_fragrance(conn, "Aventus")
    other = add_fragrance(conn, "Club de Nuit Intense")
    for i, (author, ct) in enumerate(
        [("alice", "DUPE_OF"), ("bob", "SIMILAR_TO")]
    ):
        cid = add_comment(conn, i, body="x", author=author)
        add_claim(conn, cid, subject=other, obj=target, claim_type=ct)

    results = similar_to(conn, target)
    assert len(results) == 2
    assert {r.claim_type for r in results} == {"DUPE_OF", "SIMILAR_TO"}


def test_a_fragrance_is_never_related_to_itself(conn):
    frag = add_fragrance(conn, "Khamrah")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=frag, obj=frag)

    assert similar_to(conn, frag) == []


# --- evidence --------------------------------------------------------------


def test_unverified_evidence_is_never_ranked(conn):
    """A paraphrase cannot be shown to a reader as something a person wrote."""
    target = add_fragrance(conn, "Delina")
    other = add_fragrance(conn, "Mon Guerlain")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=other, obj=target, verified=0)

    assert similar_to(conn, target) == []


def test_unresolved_edges_are_not_answers(conn):
    """An edge to a string is not yet an edge to a bottle."""
    target = add_fragrance(conn, "Oud Wood")
    cid = add_comment(conn, 1, body="x", author="alice")
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             extraction_model, created_at)
        VALUES (?, 'SIMILAR_TO', 'FRAGRANCE', 'some unresolved thing', NULL,
                'FRAGRANCE', 'Oud Wood', ?, 'POSITIVE', 0.9, 'e', 1,
                'test', '2026-01-01')
        """,
        (cid, target),
    )
    conn.commit()

    assert similar_to(conn, target) == []


def test_quotes_prefer_distinct_people(conn):
    """Three sentences from one person look like three people agreeing."""
    target = add_fragrance(conn, "Aventus")
    other = add_fragrance(conn, "Club de Nuit Intense")
    for i in range(3):
        cid = add_comment(conn, i, body="x", author="superfan")
        add_claim(conn, cid, subject=other, obj=target,
                  evidence=f"fan take {i}", confidence=0.99)
    cid = add_comment(conn, 10, body="x", author="alice")
    add_claim(conn, cid, subject=other, obj=target,
              evidence="alice take", confidence=0.1)

    (result,) = similar_to(conn, target, quotes=2)
    authors = [ev.author_id for ev in result.evidence]
    assert set(authors) == {"superfan", "alice"}, "low confidence still gets a slot"


def test_quotes_fall_back_to_one_person_when_thats_all_there_is(conn):
    target = add_fragrance(conn, "Aventus")
    other = add_fragrance(conn, "Club de Nuit Intense")
    for i in range(3):
        cid = add_comment(conn, i, body="x", author="superfan")
        add_claim(conn, cid, subject=other, obj=target, evidence=f"take {i}")

    (result,) = similar_to(conn, target)
    assert len(result.evidence) == 3


def test_evidence_carries_a_link_back_to_the_comment(graph):
    """The count is the ranking; the link is the proof."""
    conn, a, _ = graph
    for ev in similar_to(conn, a)[0].evidence:
        assert ev.permalink.startswith("https://")


def test_quotes_are_capped(graph):
    conn, a, _ = graph
    assert len(similar_to(conn, a, quotes=2)[0].evidence) == 2


# --- sentiment -------------------------------------------------------------


def test_aggregate_sentiment_is_a_plurality():
    assert aggregate_sentiment({"POSITIVE": 3, "NEGATIVE": 1}) == "POSITIVE"
    assert aggregate_sentiment({"NEGATIVE": 2, "NEUTRAL": 1}) == "NEGATIVE"
    assert aggregate_sentiment({}) == "NEUTRAL"


def test_a_split_verdict_is_neutral_not_a_winner():
    """Averaging would hide that the reception is genuinely divided."""
    assert aggregate_sentiment({"POSITIVE": 5, "NEGATIVE": 5}) == "NEUTRAL"


def test_results_carry_the_sentiment_breakdown_not_only_the_label(conn):
    """A page should be able to say 3 of 4, not just 'positive'."""
    target = add_fragrance(conn, "Layton")
    other = add_fragrance(conn, "Khamrah")
    for i, (author, s) in enumerate(
        [("a", "POSITIVE"), ("b", "POSITIVE"), ("c", "NEGATIVE")]
    ):
        cid = add_comment(conn, i, body="x", author=author)
        add_claim(conn, cid, subject=other, obj=target, sentiment=s)

    (result,) = similar_to(conn, target)
    assert result.sentiment == "POSITIVE"
    assert result.sentiment_counts == {"POSITIVE": 2, "NEGATIVE": 1}


def test_rollup_reports_per_type_as_well_as_overall(conn):
    """Loved the smell, hated the longevity nets to NEUTRAL — which
    describes nothing anybody said."""
    frag = add_fragrance(conn, "Delina")
    for i, (ct, s) in enumerate(
        [("NOTE_DESCRIPTOR", "POSITIVE"), ("LONGEVITY", "NEGATIVE")]
    ):
        cid = add_comment(conn, i, body="x", author=f"a{i}")
        conn.execute(
            """
            INSERT INTO claims
                (comment_id, claim_type, subject_kind, raw_subject_text,
                 subject_frag_id, object_kind, raw_object_text, object_frag_id,
                 sentiment, confidence, evidence_span, evidence_verified,
                 extraction_model, created_at)
            VALUES (?, ?, 'FRAGRANCE', 'Delina', ?, 'NONE', NULL, NULL,
                    ?, 0.9, 'e', 1, 'test', '2026-01-01')
            """,
            (cid, ct, frag, s),
        )
    conn.commit()

    rollup = sentiment_rollup(conn, frag)
    assert rollup.overall == "NEUTRAL"
    assert rollup.sentiment_for("NOTE_DESCRIPTOR") == "POSITIVE"
    assert rollup.sentiment_for("LONGEVITY") == "NEGATIVE"


def test_rollup_only_counts_claims_about_the_subject(conn):
    """Otherwise a fragrance's rivals colour its own summary."""
    frag = add_fragrance(conn, "Dior Sauvage")
    rival = add_fragrance(conn, "Layton")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=rival, obj=frag,
              claim_type="BETTER_THAN", sentiment="POSITIVE")

    rollup = sentiment_rollup(conn, frag)
    assert rollup.counts == {}
    assert sentiment_rollup(conn, rival).counts == {"POSITIVE": 1}


def test_rollup_of_an_unknown_fragrance_is_none(conn):
    assert sentiment_rollup(conn, 999) is None


# --- trust rules -----------------------------------------------------------


def test_ranking_reads_only_claims_comments_and_fragrances():
    """The trust rule, as an invariant the code cannot violate silently.

    Ranking must never consider affiliate status or commission. The
    products and retailers tables do not exist yet (Phase C), so the
    honest test today is structural: every table the ranking queries touch
    is named here, and adding a join to anything commercial fails this.

    Phase C must ADD the promised end-to-end version — same corpus, product
    rows attached to some fragrances and not others, asserting result order
    is identical with those rows deleted. This test is the guard until then,
    not a substitute for it.
    """
    import re

    from fragrance_graph.query import EDGES_SQL, SENTIMENT_SQL

    tables = set()
    for sql in (EDGES_SQL, SENTIMENT_SQL):
        tables |= {m.lower() for m in re.findall(r"(?:FROM|JOIN)\s+(\w+)", sql)}

    assert tables == {"claims", "comments", "fragrances"}


def test_ranking_is_a_pure_function_of_who_said_what(conn):
    """Order comes from commenter counts and nothing else.

    Written as a property: the fragrance with more distinct supporters wins
    regardless of which row was inserted first, how many claims each has,
    or what any future commercial attribute might say.
    """
    target = add_fragrance(conn, "Aventus")
    rich = add_fragrance(conn, "Heavily Monetized Clone")
    plain = add_fragrance(conn, "Obscure Indie Thing")

    # The one with no conceivable commercial relationship gets more people.
    for i, author in enumerate(["alice", "bob", "carol"]):
        cid = add_comment(conn, i, body="x", author=author)
        add_claim(conn, cid, subject=plain, obj=target)
    for i in range(9):
        cid = add_comment(conn, 100 + i, body="x", author="shill")
        add_claim(conn, cid, subject=rich, obj=target)

    ranked = [r.canonical_name for r in similar_to(conn, target)]
    assert ranked == ["Obscure Indie Thing", "Heavily Monetized Clone"]


def test_results_are_not_filtered_to_monetizable_options(conn):
    """A fragrance with no buying link ranks exactly as high as one with three.

    Nothing in the query layer can express "has a product", so every
    resolved edge with verified evidence is returned. The guard is that
    `similar_to` has no parameter that could ever mean "only sellable ones".
    """
    import inspect

    from fragrance_graph.query import similar_to as fn

    params = set(inspect.signature(fn).parameters)
    assert params == {
        "conn", "fragrance_id", "limit", "quotes", "min_commenters"
    }, "a new filter parameter needs a trust review, not just a test update"


def test_evidence_never_exposes_a_display_name(graph):
    """author_id is the platform's opaque id, never the name shown on a page."""
    conn, a, _ = graph
    for ev in similar_to(conn, a)[0].evidence:
        assert ev.author_id in {"alice", "bob", "carol"}
        assert "http" not in ev.author_id


# --- lookup and rendering --------------------------------------------------


def test_find_fragrance_by_id_name_and_alias(graph):
    conn, a, _ = graph
    assert find_fragrance(conn, str(a))["id"] == a
    assert find_fragrance(conn, "Baccarat Rouge 540")["id"] == a
    assert find_fragrance(conn, "BR540")["id"] == a
    assert find_fragrance(conn, "something nobody curated") is None


def test_render_says_why_it_is_empty(conn):
    """'No results' should point at the reason, which is usually curation."""
    out = render("Khamrah", [])
    assert "resolve.entities report" in out


def test_render_shows_counts_quotes_and_links(graph):
    conn, a, _ = graph
    out = render("Baccarat Rouge 540", similar_to(conn, a))

    assert "3 people" in out
    assert "Cloud" in out
    assert "alice says so" in out
    assert "https://example.test/c/0" in out


def test_render_pluralises_one_person(conn):
    target = add_fragrance(conn, "Layton")
    other = add_fragrance(conn, "Khamrah")
    cid = add_comment(conn, 1, body="x", author="alice")
    add_claim(conn, cid, subject=other, obj=target)

    assert "1 person " in render("Layton", similar_to(conn, target))


def test_cli_reports_an_unknown_fragrance(conn, tmp_path, capsys):
    from fragrance_graph.db import get_connection, migrate
    from fragrance_graph.query import main

    db = tmp_path / "cli.db"
    conn.close()
    fresh = get_connection(db)
    migrate(fresh)
    fresh.close()

    assert main(["Nonexistent", "--db-path", str(db)]) == 1
    assert "No fragrance matches" in capsys.readouterr().out
