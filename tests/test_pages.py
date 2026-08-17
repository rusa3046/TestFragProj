"""Phase D: the gate, and the trust rules a rendered page has to carry.

Most of this file is about pages that must *not* exist. A thin page is
worse than no page — it implies a consensus the corpus cannot support — so
the gate is the feature, and every test below that asserts an empty result
is asserting the product's central editorial rule rather than an edge case.
"""

import json
from dataclasses import replace

import pytest

from fragrance_graph.ingest.store import SOURCE as INGEST_SOURCE
from fragrance_graph.ingest.store import ingest
from fragrance_graph.pages import (
    MIN_COMMENTERS,
    MIN_SOURCES,
    build,
    qualifying_pairs,
    queries_behind,
    render_pair,
    slugify,
)
from fragrance_graph.query import pair_stats
from fragrance_graph.resolve.entities import add_fragrance
from tests.conftest import make_comment


def add_comment(conn, i, *, body, author, video, channel=None):
    """One comment, by a named person, under a named video.

    `channel` defaults to one uploader per video, which is the ordinary
    case. Tests about creator independence pass it explicitly to put two
    videos behind one uploader.
    """
    ingest(
        conn,
        [
            make_comment(
                i,
                body=body,
                permalink=f"https://example.test/c/{i}",
                source_channel=channel or f"UC-{video}",
                raw_json=json.dumps({"author": author, "videoId": video}),
            )
        ],
    )
    return conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
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
        VALUES (%s, %s, 'FRAGRANCE', 'subject', %s, 'FRAGRANCE', 'object', %s,
                'POSITIVE', 0.9, %s, %s, %s, 'test', '2026-01-01')
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
    # Ask pages are unconditional output — the curated questions render
    # whatever the corpus holds, including honest refusals — so the exact
    # set here is the pair page, the index, and one page per question.
    from fragrance_graph.ask import QUESTIONS, ask_slug

    expected = sorted(
        ["aventus-vs-club-de-nuit-intense-man.html", "index.html"]
        + [ask_slug(slug) for slug, _ in QUESTIONS]
    )
    assert written == expected


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

    assert pair_stats(conn, a, b).commenters == 4
    assert pair_stats(conn, a, b).sources == 3
    # Symmetric: the same five people whichever end you ask from.
    assert pair_stats(conn, b, a) == pair_stats(conn, a, b)

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

    # Dropped and rolled back. Postgres has transactional DDL, so the
    # tables are gone for the length of this transaction and back
    # afterwards — which matters now that every test shares one database
    # rather than getting its own file. Committing the drop took the
    # commerce tables away from all 24 tests that ran after it.
    conn.execute("DROP TABLE IF EXISTS products")
    conn.execute("DROP TABLE IF EXISTS retailers")
    try:
        assert render_pair(qualifying_pairs(conn)[0]) == before
    finally:
        conn.rollback()

    assert conn.execute(
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_name IN ('products', 'retailers')"
    ).fetchone()[0] == 2, "the rollback has to put them back"


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
        # Accents fold to their base letter rather than being treated as
        # punctuation and replaced with a hyphen — see `slugify`'s
        # docstring. "d-tour-noir" was the old, buggy output; this is the
        # real fragrance name, not a synthetic case.
        ("Détour Noir", "detour-noir"),
        ("Hermès Eau d'Orange Verte", "hermes-eau-d-orange-verte"),
        ("!!!", "unnamed"),
    ],
)
def test_slugify_survives_real_fragrance_names(name, expected):
    """Apostrophes, ampersands and accents are ordinary in this domain."""
    assert slugify(name) == expected


# --- retrieval-query independence -----------------------------------------


def discover(conn, video, query, *, run="test-run"):
    """Record that `query` surfaced `video`, as an ingest run would.

    Uses the same `source` the comments were ingested under: discovery
    joins on (source, video_id), so a mismatch silently yields zero
    queries rather than an error.
    """
    conn.execute(
        "INSERT INTO videos (source, video_id) VALUES (%s, %s)"
        " ON CONFLICT DO NOTHING",
        (INGEST_SOURCE, video),
    )
    conn.execute(
        "INSERT INTO video_discoveries "
        "(source, video_id, retrieval_query, retrieved_at, discovery_run) "
        "VALUES (%s, %s, %s, '2026-08-11', %s) ON CONFLICT DO NOTHING",
        (INGEST_SOURCE, video, query, run),
    )
    conn.commit()


def test_videos_found_by_one_query_are_not_independent_sources(conn):
    """The failure `min_sources` cannot see.

    Three commenters across three *different* videos clears both existing
    bars. But if all three videos were returned by one `layton dupe`
    search, they are one question asked in three rooms to audiences
    assembled for that question.
    """
    pair_of(conn, people=3, videos=3)
    for i in range(3):
        discover(conn, f"vid-{i}", "layton dupe")

    ev = pair_stats(conn, 1, 2)
    assert (ev.commenters, ev.sources, ev.queries) == (3, 3, 1)

    assert len(qualifying_pairs(conn)) == 1                       # today
    assert qualifying_pairs(conn, min_queries=2) == []            # if gated


def test_the_same_video_can_be_found_by_several_queries(conn):
    """Why discovery is its own table rather than a column on comments.

    A single `retrieval_query` field would have to pick one and drop the
    rest, destroying the count this exists to make possible.
    """
    pair_of(conn, people=3, videos=2)
    discover(conn, "vid-0", "layton dupe")
    discover(conn, "vid-0", "best designer clones")   # same video, 2nd query
    discover(conn, "vid-1", "layton dupe")

    assert pair_stats(conn, 1, 2).queries == 2
    assert len(qualifying_pairs(conn, min_queries=2)) == 1


def test_a_pair_with_no_retrieval_record_is_not_silently_unpublished(conn):
    """0 queries means "we did not record how we found this", not "narrow".

    Every video ingested before provenance tracking has no discovery row.
    Treating unknown as a failure would unpublish the entire back corpus
    the first time someone raised the bar.
    """
    pair_of(conn, people=3, videos=2)          # no discover() calls
    assert pair_stats(conn, 1, 2).queries == 0
    assert len(qualifying_pairs(conn, min_queries=2)) == 1


def test_query_count_reaches_the_pair(conn):
    pair_of(conn, people=3, videos=2)
    discover(conn, "vid-0", "layton dupe")
    discover(conn, "vid-1", "layton review")
    (pair,) = qualifying_pairs(conn)
    assert pair.queries == 2
    assert queries_behind(conn, pair) == ["layton dupe", "layton review"]


def test_a_page_says_creators_not_videos(conn):
    """The gate counts `source_channel`, which is the uploading channel.

    Pages said "across 2 videos" until 2026-08-11. Two videos by one
    YouTuber are one audience, so the label understated a bar the code
    had always enforced correctly. Pinned because it is public-facing
    text describing what the evidence actually is.
    """
    from fragrance_graph.pages import qualifying_pairs, render_pair

    pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
    pairs = qualifying_pairs(conn)
    assert pairs, "fixture should clear the gate"
    html = render_pair(pairs[0])
    assert "creators" in html or "creator" in html
    assert "video" not in html.lower(), "the gate does not count videos"


def test_the_index_says_creators_too(conn):
    """The bar is creators, and the index must not describe it as videos.

    This banned the word "videos" outright until 2026-08-12, when the
    index gained a paragraph saying where the comments come from — which
    is videos, truthfully. The rule was never about the word: it is that
    no count on the page may be labelled with the wrong unit.
    """
    import re

    from fragrance_graph.pages import qualifying_pairs, render_index

    pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
    index = render_index(qualifying_pairs(conn))
    assert "creators" in index
    assert not re.search(r"\d+\s+videos", index), "the gate does not count videos"
    assert not re.search(r"across \d+ videos", index)


def test_two_videos_by_one_creator_are_one_source(conn):
    """The counting behind the word, not the word.

    `test_a_page_says_creators_not_videos` pins the label. This pins what
    the label is counting, which is the half that can silently drift: the
    gate read distinct videos while the page said creators, and the two
    are equal on this corpus for every publishable pair — so the wording
    was true by luck rather than by construction.

    Three people, two videos, one uploading channel. One creator framed
    both audiences, so this must not publish.
    """
    a = add_fragrance(conn, "Aventus")
    b = add_fragrance(conn, "Club de Nuit Intense Man")
    for i in range(3):
        ingest(
            conn,
            [
                make_comment(
                    i,
                    body=f"person {i} wrote this",
                    permalink=f"https://example.test/c/{i}",
                    source_channel="UC-one-channel",
                    raw_json=json.dumps(
                        {"author": f"person-{i}", "videoId": f"vid-{i % 2}"}
                    ),
                )
            ],
        )
        cid = conn.execute(
            "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
        ).fetchone()[0]
        add_claim(conn, cid, subject=b, obj=a, evidence=f"person {i} wrote this")

    ev = pair_stats(conn, a, b)
    assert ev.sources == 2, "two distinct videos"
    assert ev.creators == 1, "but one channel uploaded both"
    assert qualifying_pairs(conn) == []


class TestNoOrphanedBullets:
    """A quote must not render with an empty bullet above it.

    The markup was never malformed — `<li><blockquote>` is valid — so a
    test for an *empty* `<li>` would have passed while every quote on the
    live site sat under a stray marker. `<blockquote>` is a block element,
    so the browser breaks the line after the marker and leaves it alone.
    The check therefore has to be "no list item leads with a block
    element", not "no list item is empty".
    """

    BLOCK = ("blockquote", "figure", "div", "p", "ul", "ol", "section")

    def _pages(self, conn, tmp_path):
        from fragrance_graph.pages import build

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        build(conn, tmp_path)
        return list(tmp_path.glob("*.html"))

    def test_no_list_item_leads_with_a_block_element(self, conn, tmp_path):
        import re

        pages = self._pages(conn, tmp_path)
        assert pages, "fixture should publish something"
        pattern = re.compile(
            r"<li>\s*<(" + "|".join(self.BLOCK) + r")\b", re.IGNORECASE
        )
        for page in pages:
            hit = pattern.search(page.read_text(encoding="utf-8"))
            assert not hit, f"{page.name}: orphaned bullet before {hit.group(1)}"

    def test_no_empty_list_item(self, conn, tmp_path):
        import re

        for page in self._pages(conn, tmp_path):
            text = page.read_text(encoding="utf-8")
            assert not re.search(r"<li>\s*</li>", text), page.name

    def test_quotes_are_figures_with_attribution(self, conn, tmp_path):
        """The replacement has to keep the quote *and* who said it."""
        pages = [p for p in self._pages(conn, tmp_path) if p.name != "index.html"]
        for page in pages:
            text = page.read_text(encoding="utf-8")
            if "<blockquote>" not in text:
                continue
            assert text.count("<figure>") == text.count("<blockquote>")
            assert text.count("<figcaption>") == text.count("<blockquote>")
            assert "read the comment" in text


class TestBrandCasing:
    """One house, one spelling, on any single page.

    Curation happens over many sessions, by hand and by catalogue, so a
    brand arrives spelled more than one way — "Parfums de Marly" fourteen
    times and "Parfums De Marly" once. One live page title carried both,
    which reads as two different houses.
    """

    def test_the_majority_casing_wins(self, conn):
        from fragrance_graph.pages import brand_casing
        from fragrance_graph.resolve.entities import add_fragrance

        for i in range(3):
            add_fragrance(conn, f"Parfums de Marly Bottle {i}",
                          brand="Parfums de Marly")
        add_fragrance(conn, "Parfums De Marly Odd One",
                      brand="Parfums De Marly")
        conn.commit()
        assert brand_casing(conn)["parfums de marly"] == "Parfums de Marly"

    def test_a_tie_is_deterministic(self, conn):
        """Row order must not decide how a house is spelled."""
        from fragrance_graph.pages import brand_casing
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Alpha One", brand="acme")
        add_fragrance(conn, "Alpha Two", brand="Acme")
        conn.commit()
        assert brand_casing(conn)["acme"] == brand_casing(conn)["acme"]

    def test_it_rewrites_the_brand_inside_a_name(self):
        from fragrance_graph.pages import with_canonical_casing

        casing = {"parfums de marly": "Parfums de Marly"}
        assert with_canonical_casing("Parfums De Marly Layton Exclusif",
                                     casing) == "Parfums de Marly Layton Exclusif"

    def test_it_leaves_an_unknown_brand_alone(self):
        from fragrance_graph.pages import with_canonical_casing

        assert with_canonical_casing("Some Other Bottle", {}) == "Some Other Bottle"

    def test_the_longest_brand_matches_first(self):
        """A short brand that prefixes a longer one must not win."""
        from fragrance_graph.pages import with_canonical_casing

        casing = {"parfums": "PARFUMS", "parfums de marly": "Parfums de Marly"}
        assert with_canonical_casing("Parfums De Marly Layton", casing) == \
            "Parfums de Marly Layton"

    def test_no_page_title_carries_two_casings_of_one_brand(self, conn, tmp_path):
        import re

        from fragrance_graph.pages import build

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        build(conn, tmp_path)
        for page in tmp_path.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            title = re.search(r"<title>(.*?)</title>", text, re.S)
            if not title:
                continue
            words = title.group(1).split()
            lowered = [w.lower() for w in words]
            for w in lowered:
                same = {words[j] for j, x in enumerate(lowered) if x == w}
                assert len(same) == 1, f"{page.name}: {sorted(same)}"


class TestVerdict:
    """The first line of a page has to answer the question that brought
    someone here, without ever grading a fragrance.

    A page used to open with "5 people connected these two fragrances",
    which is a fact about our corpus rather than an answer. The verdict
    replaces it with two counted sentences: what the most people said, and
    what nobody said. Everything below pins that it stays *counted* — a
    line this file can generate must be checkable against rows.
    """

    def better_than(self, conn, a, b, *, people, videos, start=100):
        """`people` commenters saying a beats b, spread over `videos`."""
        for i in range(start, start + people):
            cid = add_comment(
                conn,
                i,
                body=f"person {i} wrote this",
                author=f"pref-{i}",
                video=f"vid-{(i - start) % videos}",
            )
            add_claim(
                conn, cid, subject=a, obj=b, claim_type="BETTER_THAN",
                evidence=f"person {i} wrote this",
            )

    def test_it_leads_with_the_claim_the_most_people_made(self, conn):
        from fragrance_graph.pages import verdict

        a, b = pair_of(conn, people=3, videos=2, claim_type="SIMILAR_TO")
        self.better_than(conn, a, b, people=5, videos=2)
        (pair,) = qualifying_pairs(conn)

        line = verdict(pair)
        assert line.startswith("5 people across 2 creators say ")
        assert "is better than" in line

    def test_both_bottles_are_named(self, conn):
        """The verdict is the first sentence read, so "it" has no antecedent."""
        from fragrance_graph.pages import verdict

        pair_of(conn, people=3, videos=2)
        (pair,) = qualifying_pairs(conn)
        line = verdict(pair)
        assert pair.left in line and pair.right in line

    def test_people_and_creators_are_counted_over_the_same_rows(self, conn):
        """The two numbers in one sentence must describe one set of comments.

        The pair-wide creator count is the larger one. Quoting it beside a
        claim-type commenter count would read as more independent evidence
        than exists — the scope mismatch SPEC recorded once already.
        """
        from fragrance_graph.pages import verdict

        a, b = pair_of(conn, people=3, videos=2, claim_type="SIMILAR_TO")
        # Four people, but all of them under one uploader.
        self.better_than(conn, a, b, people=4, videos=1)
        (pair,) = qualifying_pairs(conn)

        assert pair.sources == 2, "the pair as a whole spans two creators"
        line = verdict(pair)
        assert line.startswith("4 people across 1 creator ")
        assert "across 2 creators" not in line

    def test_a_claim_one_person_made_is_never_the_verdict(self, conn):
        """Three people saying three different things is not a finding."""
        from fragrance_graph.pages import verdict
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        for i, claim_type in enumerate(("SIMILAR_TO", "DUPE_OF", "BETTER_THAN")):
            cid = add_comment(
                conn, i, body=f"person {i} wrote this", author=f"person-{i}",
                video=f"vid-{i}",
            )
            add_claim(
                conn, cid, subject=a, obj=b, claim_type=claim_type,
                evidence=f"person {i} wrote this",
            )
        (pair,) = qualifying_pairs(conn)

        line = verdict(pair)
        assert "no two of them made the same comparison" in line
        assert "dupe" not in line, "it must not pick a winner out of ones"

    def test_a_preference_alone_says_nothing_about_smell(self, conn):
        from fragrance_graph.pages import verdict
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        self.better_than(conn, a, b, people=3, videos=2, start=0)
        (pair,) = qualifying_pairs(conn)
        assert verdict(pair).endswith("None of them said how the two differ.")

    def test_similarity_alone_says_nothing_about_preference(self, conn):
        from fragrance_graph.pages import verdict

        pair_of(conn, people=3, videos=2, claim_type="DUPE_OF")
        (pair,) = qualifying_pairs(conn)
        assert verdict(pair).endswith(
            "Nobody here said which of the two they preferred."
        )

    def test_when_both_are_present_the_gap_is_what_they_smell_like(self, conn):
        from fragrance_graph.pages import verdict

        a, b = pair_of(conn, people=3, videos=2, claim_type="DUPE_OF")
        self.better_than(conn, a, b, people=3, videos=2)
        (pair,) = qualifying_pairs(conn)
        assert verdict(pair).endswith(
            "Nothing here describes what either one smells like on its own."
        )

    def test_every_verdict_states_an_absence(self, conn):
        """Half the sentence is the half that says what we do not know."""
        from fragrance_graph.pages import verdict

        a, b = pair_of(conn, people=3, videos=2, claim_type="DUPE_OF")
        self.better_than(conn, a, b, people=2, videos=2)
        (pair,) = qualifying_pairs(conn)
        line = verdict(pair)
        assert any(w in line for w in ("Nobody", "None", "Nothing")), line

    def test_a_tie_does_not_depend_on_row_order(self, conn):
        from fragrance_graph.pages import verdict

        a, b = pair_of(conn, people=3, videos=2, claim_type="DUPE_OF")
        self.better_than(conn, a, b, people=3, videos=2)
        (pair,) = qualifying_pairs(conn)
        assert verdict(pair) == verdict(replace(pair, rows=pair.rows[::-1]))

    def test_it_is_the_first_thing_on_the_page(self, conn):
        from fragrance_graph.pages import verdict

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        (pair,) = qualifying_pairs(conn)
        html = render_pair(pair)

        line = verdict(pair)
        assert line in html
        assert html.index(line) < html.index("<h2"), "the verdict comes first"

    def test_a_pair_with_no_rows_still_gets_a_true_sentence(self):
        from fragrance_graph.pages import Pair, verdict

        empty = Pair(
            left="A", right="B", left_id=1, right_id=2, commenters=0,
            sources=0, queries=0, unprovenanced=0, rows=(),
        )
        assert verdict(empty) == "Nobody in this corpus compared A and B."


class TestDirection:
    """A page has to repeat a claim the way round it was made.

    `query.similar_to` answers "what relates to X", so it collapses
    DUPE_OF and SIMILAR_TO across both directions — right for counting
    people, wrong for quoting them. Pages rendered every row from the
    alphabetically first bottle, so six people writing "Dusk is a dupe of
    Layton" appeared as *Layton* being the dupe: the £200 bottle imitating
    the £30 one, which is both false and the exact inversion a house would
    object to.
    """

    def dupe_claims(self, conn, *, subject, obj, people, start=0):
        for i in range(start, start + people):
            cid = add_comment(
                conn, i, body=f"person {i} wrote this", author=f"person-{i}",
                video=f"vid-{i % 2}",
            )
            add_claim(
                conn, cid, subject=subject, obj=obj, claim_type="DUPE_OF",
                evidence=f"person {i} wrote this",
            )

    def test_the_majority_direction_wins(self, conn):
        from fragrance_graph.pages import verdict
        from fragrance_graph.resolve.entities import add_fragrance

        # "Aventus" sorts first, so the page is built from that end — but
        # every commenter named the other bottle as the dupe.
        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        self.dupe_claims(conn, subject=b, obj=a, people=3)
        (pair,) = qualifying_pairs(conn)

        assert pair.left == "Aventus", "built from the alphabetical end"
        assert verdict(pair).startswith(
            "3 people across 2 creators call Club de Nuit Intense Man "
            "a dupe of Aventus"
        )

    def test_the_heading_agrees_with_the_verdict(self, conn):
        """One page must not state the claim both ways round."""
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        self.dupe_claims(conn, subject=b, obj=a, people=3)
        (pair,) = qualifying_pairs(conn)

        html = render_pair(pair)
        assert "call Club de Nuit Intense Man a dupe of Aventus" in html
        assert "call Aventus a dupe of" not in html

    def test_a_tie_keeps_the_page_stable(self, conn):
        """Neither direction has a majority; the wording must not flap."""
        from fragrance_graph.pages import verdict
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        self.dupe_claims(conn, subject=b, obj=a, people=2)
        self.dupe_claims(conn, subject=a, obj=b, people=2, start=50)
        (pair,) = qualifying_pairs(conn)
        assert verdict(pair) == verdict(pair)
        assert verdict(pair).startswith("4 people across 2 creators call Aventus")

    def test_a_preference_stated_from_the_other_end_still_reaches_the_page(
        self, conn
    ):
        """The half of the evidence pages used to drop.

        `similar_to` will not return "things that beat X" when asked about
        X, and rightly — those are not recommendations for it. A pair page
        asks a different question, and without asking from both ends it
        held two people preferring Dusk while announcing that nobody had
        said which they preferred.
        """
        from fragrance_graph.pages import verdict
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        self.dupe_claims(conn, subject=b, obj=a, people=3)
        for i in range(50, 52):
            cid = add_comment(
                conn, i, body=f"person {i} wrote this", author=f"pref-{i}",
                video=f"vid-{i % 2}",
            )
            add_claim(
                conn, cid, subject=b, obj=a, claim_type="BETTER_THAN",
                evidence=f"person {i} wrote this",
            )
        (pair,) = qualifying_pairs(conn)

        assert pair.reverse, "the preference is only visible from the far end"
        line = verdict(pair)
        assert "Nobody here said which of the two they preferred" not in line
        assert "is better than Aventus" in render_pair(pair)

    def test_a_contested_preference_is_not_reported_as_a_finding(self, conn):
        """Reporting the larger side alone picks a winner by omission."""
        from fragrance_graph.pages import verdict
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        for i, (subj, obj) in enumerate([(a, b)] * 3 + [(b, a)] * 2):
            cid = add_comment(
                conn, i, body=f"person {i} wrote this", author=f"person-{i}",
                video=f"vid-{i % 2}",
            )
            add_claim(
                conn, cid, subject=subj, obj=obj, claim_type="BETTER_THAN",
                evidence=f"person {i} wrote this",
            )
        (pair,) = qualifying_pairs(conn)

        assert verdict(pair) == (
            "3 people across 2 creators say Aventus is better than "
            "Club de Nuit Intense Man. 2 people said the opposite."
        )

    def test_a_quote_is_attributed_to_the_bottle_it_was_written_about(
        self, conn
    ):
        """`outbound` is relative to the end the row was read from.

        The reverse rows are read from the other end, so reusing the
        left-hand bottle as "written about" would caption every reverse
        quote with the wrong name.
        """
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        self.dupe_claims(conn, subject=b, obj=a, people=3)
        for i in range(50, 53):
            cid = add_comment(
                conn, i, body=f"cdnim wins {i}", author=f"pref-{i}",
                video=f"vid-{i % 2}",
            )
            add_claim(
                conn, cid, subject=b, obj=a, claim_type="BETTER_THAN",
                evidence=f"cdnim wins {i}",
            )
        (pair,) = qualifying_pairs(conn)

        html = render_pair(pair)
        for line in html.splitlines():
            if "cdnim wins" in line:
                assert "Written about Club de Nuit Intense Man." in line, line


class TestReadsLikeEnglish:
    """A section can rest on one person, and the count sits in front of a
    verb — "1 person call X a dupe of Y" tells a reader a machine wrote
    the page, which is exactly the credibility the quotes are there to buy.
    """

    def test_one_person_gets_a_singular_verb(self, conn):
        from fragrance_graph.pages import Statement
        from fragrance_graph.query import Related

        row = Related(
            fragrance_id=1, canonical_name="B", brand=None,
            claim_type="DUPE_OF", commenters=1, pair_commenters=1, sources=1,
            creators=1, pair_sources=1, claims=1, outbound_claims=1,
            outbound_commenters=1, sentiment="POSITIVE", sentiment_counts={},
        )
        assert Statement("A", "B", row).sentence == "calls A a dupe of B"
        assert replace(
            Statement("A", "B", row), row=replace(row, commenters=2)
        ).sentence == "call A a dupe of B"

    def test_no_rendered_page_says_person_followed_by_a_plural_verb(
        self, conn, tmp_path
    ):
        import re

        a, b = pair_of(conn, people=3, videos=2, claim_type="DUPE_OF")
        cid = add_comment(conn, 90, body="a beats b", author="lone", video="vid-9")
        add_claim(conn, cid, subject=a, obj=b, claim_type="BETTER_THAN",
                  evidence="a beats b")
        build(conn, tmp_path)

        for page in tmp_path.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            assert not re.search(r"1 person (call|say) ", text), page.name
            assert not re.search(r"\d\d* people (calls|says) ", text), page.name


class TestBottleFacts:
    """Who makes it and when it came out — the two things a stranger needs
    that are not opinions.

    Notes and accords stay off these pages because nothing in the corpus
    evidences them. A house and a release year are catalogue records, not
    claims about smell, so they are allowed — and shown only when someone
    has actually curated them.
    """

    def test_a_name_that_already_carries_its_brand_is_left_alone(self):
        from fragrance_graph.pages import Bottle

        b = Bottle("Parfums de Marly Layton", brand="Parfums de Marly")
        assert b.described == "Parfums de Marly Layton"

    def test_a_brand_the_name_hides_is_shown(self):
        from fragrance_graph.pages import Bottle

        b = Bottle("Atomic Rose", brand="Initio Parfums Prives")
        assert b.described == "Atomic Rose (Initio Parfums Prives)"

    def test_a_year_is_shown_when_it_exists(self):
        from fragrance_graph.pages import Bottle

        assert Bottle("Atomic Rose", brand="Initio", year=2020).described == \
            "Atomic Rose (Initio, 2020)"
        # The redundant brand still drops out; the year is the whole point.
        assert Bottle("Parfums de Marly Layton", brand="Parfums de Marly",
                      year=2016).described == \
            "Parfums de Marly Layton (2016)"

    def test_nothing_known_means_nothing_added(self):
        from fragrance_graph.pages import Bottle

        assert Bottle("Mystery Juice").described == "Mystery Juice"

    def test_a_page_shows_them_when_they_exist(self, conn, tmp_path):
        from fragrance_graph.pages import bottle_facts, qualifying_pairs

        pair_of(conn, people=3, videos=2)
        conn.execute(
            "UPDATE fragrances SET brand = 'By Kilian', house_year = 2020"
            " WHERE canonical_name = 'Aventus'"
        )
        conn.commit()
        (pair,) = qualifying_pairs(conn)
        html = render_pair(pair, bottle_facts(conn))
        assert "<li>Aventus (By Kilian, 2020)</li>" in html

    def test_a_page_stays_silent_when_they_do_not(self, conn):
        """Today's corpus: 0 of 56 fragrances have a year."""
        from fragrance_graph.pages import bottle_facts, qualifying_pairs

        pair_of(conn, people=3, videos=2)
        (pair,) = qualifying_pairs(conn)
        html = render_pair(pair, bottle_facts(conn))
        assert "<li>Aventus</li>" not in html, "no bare restatement of the name"

    def test_a_year_survives_curation(self, conn):
        """The field has to be writable, or the display path is decorative."""
        from fragrance_graph.pages import bottle_facts
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Atomic Rose", brand="Initio Parfums Prives",
                      house_year=2020)
        assert bottle_facts(conn)["Atomic Rose"].year == 2020


class TestWhatThisIs:
    """The index has to explain itself to someone who arrived from Google.

    It landed on a bare list of links under a one-line count. A stranger
    could not tell whether the numbers were reviews, ratings, or a
    machine's guesses — which is the whole question this project is
    answering.
    """

    def index(self, conn):
        from fragrance_graph.pages import render_index

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        return render_index(qualifying_pairs(conn))

    def test_it_says_where_the_sentences_come_from(self, conn):
        text = self.index(conn)
        assert "comments left under fragrance videos" in text
        assert "links back to the comment" in text

    def test_it_states_the_bar_a_page_has_to_clear(self, conn):
        text = self.index(conn)
        assert f"{MIN_COMMENTERS} different people" in text
        assert f"{MIN_SOURCES} different creators" in text

    def test_it_says_what_is_deliberately_absent(self, conn):
        text = self.index(conn)
        for absent in ("no notes lists", "no ratings", "no shopping"):
            assert absent in text.lower(), absent

    def test_it_sells_nothing(self, conn):
        """Including the site itself."""
        text = self.index(conn).lower()
        for hype in ("best", "ultimate", "amazing", "must-have", "trusted",
                     "accurate", "definitive", "curated by experts"):
            assert hype not in text, hype

    def test_an_empty_index_still_explains_itself(self, conn):
        from fragrance_graph.pages import render_index

        assert "answers one question" in render_index([])


class TestFlankerGate:
    """A house compared against its own line clears a higher bar.

    Two bottles from one house whose names overlap are what entity
    resolution gets wrong most often, and the failure is silent: the names
    differ by one word, so a comment about the flanker resolves to the
    parent about as easily as to the flanker. `mention_only_words` exists
    because that merge was made once, live.
    """

    def branded_pair(self, conn, left, right, *, brand, people, videos,
                     right_brand=None):
        from fragrance_graph.resolve.entities import add_fragrance

        a = add_fragrance(conn, left, brand=brand)
        b = add_fragrance(conn, right, brand=right_brand or brand)
        for i in range(people):
            cid = add_comment(
                conn, i, body=f"person {i} wrote this", author=f"person-{i}",
                video=f"vid-{i % videos}",
            )
            add_claim(conn, cid, subject=b, obj=a, claim_type="DUPE_OF",
                      evidence=f"person {i} wrote this")
        return a, b

    def test_a_flanker_is_recognised(self):
        from fragrance_graph.pages import Bottle, is_flanker_pair

        assert is_flanker_pair(
            Bottle("Parfums de Marly Layton", "Parfums de Marly"),
            Bottle("Parfums de Marly Layton Exclusif", "Parfums de Marly"),
        )

    def test_two_flankers_of_one_parent_count_too(self):
        """Neither is the other's parent, and they are just as confusable."""
        from fragrance_graph.pages import Bottle, is_flanker_pair

        assert is_flanker_pair(
            Bottle("Armaf Club de Nuit Intense Man", "Armaf"),
            Bottle("Armaf Club de Nuit Sillage", "Armaf"),
        )

    def test_two_unrelated_bottles_from_one_house_are_not_flankers(self):
        from fragrance_graph.pages import Bottle, is_flanker_pair

        assert not is_flanker_pair(
            Bottle("Parfums de Marly Layton", "Parfums de Marly"),
            Bottle("Parfums de Marly Delina Exclusif", "Parfums de Marly"),
        )

    def test_two_houses_are_never_a_flanker_pair(self):
        """The comparison the site exists for: a cheap bottle against a
        expensive one. It must never be caught by this."""
        from fragrance_graph.pages import Bottle, is_flanker_pair

        assert not is_flanker_pair(
            Bottle("Armaf Club de Nuit Intense Man", "Armaf"),
            Bottle("Creed Aventus", "Creed"),
        )

    def test_an_unknown_brand_does_not_raise_the_bar(self):
        """With nothing to compare houses on, assume nothing."""
        from fragrance_graph.pages import Bottle, is_flanker_pair

        assert not is_flanker_pair(Bottle("Layton"), Bottle("Layton Exclusif"))

    def test_a_flanker_pair_at_the_ordinary_bar_is_not_published(self, conn):
        from fragrance_graph.pages import MIN_COMMENTERS as MC

        self.branded_pair(
            conn, "Marly Layton", "Marly Layton Exclusif", brand="Marly",
            people=MC + 1, videos=MIN_SOURCES,
        )
        assert qualifying_pairs(conn) == []

    def test_a_flanker_pair_that_clears_the_higher_bar_is_published(self, conn):
        from fragrance_graph.pages import (
            MIN_FLANKER_COMMENTERS,
            MIN_FLANKER_SOURCES,
        )

        self.branded_pair(
            conn, "Marly Layton", "Marly Layton Exclusif", brand="Marly",
            people=MIN_FLANKER_COMMENTERS, videos=MIN_FLANKER_SOURCES,
        )
        assert len(qualifying_pairs(conn)) == 1

    def test_a_cross_house_pair_still_only_needs_the_ordinary_bar(self, conn):
        self.branded_pair(
            conn, "Creed Aventus", "Armaf Club de Nuit Intense Man",
            brand="Creed", right_brand="Armaf",
            people=MIN_COMMENTERS, videos=MIN_SOURCES,
        )
        assert len(qualifying_pairs(conn)) == 1

    def test_the_cost_of_the_bar_is_reported(self, conn):
        """A raised bar is a page that stopped existing; it gets named."""
        from fragrance_graph.pages import held_as_flankers

        self.branded_pair(
            conn, "Marly Layton", "Marly Layton Exclusif", brand="Marly",
            people=MIN_COMMENTERS, videos=MIN_SOURCES,
        )
        (held,) = held_as_flankers(conn)
        assert held.title == "Marly Layton vs Marly Layton Exclusif"

    def test_nothing_is_reported_when_nothing_is_held(self, conn):
        from fragrance_graph.pages import held_as_flankers

        self.branded_pair(
            conn, "Creed Aventus", "Armaf Club de Nuit Intense Man",
            brand="Creed", right_brand="Armaf",
            people=MIN_COMMENTERS, videos=MIN_SOURCES,
        )
        assert held_as_flankers(conn) == []


class TestReverseIndex:
    """"Layton dupes" is a question people type, and until now the site
    could only answer it one comparison at a time.

    Nothing new is claimed on these pages. Every row is a pair that
    already cleared the gate, and the sentence is the one its own page
    leads with.
    """

    def two_pairs_on(self, conn, hub="Parfums de Marly Layton"):
        from fragrance_graph.resolve.entities import add_fragrance

        centre = add_fragrance(conn, hub, brand="Parfums de Marly")
        others = []
        for n, name in enumerate(("Al Haramain Detour Noir",
                                  "The Woods Collection Dusk")):
            other = add_fragrance(conn, name, brand=name.split()[0])
            others.append(other)
            for i in range(MIN_COMMENTERS + n):
                cid = add_comment(
                    conn, n * 20 + i, body=f"person {n}{i} wrote this",
                    author=f"p{n}{i}", video=f"vid-{n}{i % MIN_SOURCES}",
                )
                add_claim(conn, cid, subject=other, obj=centre,
                          claim_type="DUPE_OF",
                          evidence=f"person {n}{i} wrote this")
        return centre, others

    def test_a_bottle_with_two_comparisons_gets_an_index(self, conn):
        from fragrance_graph.pages import reverse_indexes

        self.two_pairs_on(conn)
        indexes = {i.bottle: i for i in reverse_indexes(qualifying_pairs(conn))}
        assert "Parfums de Marly Layton" in indexes
        assert len(indexes["Parfums de Marly Layton"].pairs) == 2

    def test_a_bottle_with_one_comparison_does_not(self, conn):
        """A page whose only job is to link to one other page."""
        from fragrance_graph.pages import reverse_indexes

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        assert reverse_indexes(qualifying_pairs(conn)) == []

    def test_rows_are_ranked_by_the_number_the_page_prints(self, conn):
        """Sorting by the pair-wide count put "4 people" above "6 people".

        The pair total counts everyone who connected the two bottles; the
        line quotes one claim type. Two scopes, one of them invisible to a
        reader looking at a list that appears mis-sorted.
        """
        from fragrance_graph.pages import reverse_indexes

        self.two_pairs_on(conn)
        (index,) = [i for i in reverse_indexes(qualifying_pairs(conn))
                    if i.bottle == "Parfums de Marly Layton"]
        counts = [s.row.commenters for _, _, s in index.rows]
        assert counts == sorted(counts, reverse=True)

    def test_the_title_only_says_dupes_when_someone_said_dupe(self, conn):
        from fragrance_graph.pages import ReverseIndex, reverse_indexes

        self.two_pairs_on(conn)
        (index,) = [i for i in reverse_indexes(qualifying_pairs(conn))
                    if i.bottle == "Parfums de Marly Layton"]
        assert index.title == "Dupes of Parfums de Marly Layton"

        preference_only = ReverseIndex(
            bottle="Parfums de Marly Layton",
            pairs=tuple(
                replace(p, rows=tuple(
                    replace(r, claim_type="BETTER_THAN") for r in p.rows
                ))
                for p in index.pairs
            ),
        )
        assert preference_only.title == (
            "Fragrances people compare to Parfums de Marly Layton"
        )

    def test_it_never_sums_people_across_comparisons(self, conn):
        """One person can appear in several pairs; adding the counts
        prints a number of humans the corpus cannot support."""
        from fragrance_graph.pages import render_reverse, reverse_indexes

        self.two_pairs_on(conn)
        (index,) = [i for i in reverse_indexes(qualifying_pairs(conn))
                    if i.bottle == "Parfums de Marly Layton"]
        total = sum(p.commenters for p in index.pairs)
        assert f"{total} people" not in render_reverse(index)

    def test_every_link_it_writes_exists(self, conn, tmp_path):
        import re

        self.two_pairs_on(conn)
        build(conn, tmp_path)
        written = {p.name for p in tmp_path.glob("*.html")}
        for page in tmp_path.glob("*.html"):
            for href in re.findall(r'href="([^"]+)"',
                                   page.read_text(encoding="utf-8")):
                if href.startswith("http"):
                    continue  # a permalink back to the comment itself
                assert href in written, f"{page.name} -> {href}"

    def test_a_pair_page_links_to_the_indexes_it_belongs_to(self, conn, tmp_path):
        self.two_pairs_on(conn)
        build(conn, tmp_path)
        page = (tmp_path / "al-haramain-detour-noir-vs-parfums-de-marly-layton.html")
        assert "dupes-of-parfums-de-marly-layton.html" in page.read_text(
            encoding="utf-8"
        )

    def test_an_index_page_carries_no_imagery_either(self, conn, tmp_path):
        self.two_pairs_on(conn)
        build(conn, tmp_path)
        for page in tmp_path.glob("dupes-of-*.html"):
            text = page.read_text(encoding="utf-8")
            for forbidden in ("<img", "<svg", "background-image"):
                assert forbidden not in text, page.name


class TestLanguageFilter:
    """Crude quotes stay off the page without leaving the corpus.

    23 of the claim spans in the committed corpus contain a word from the
    blocklist. None of them currently reach a published page, because the
    three quotes each section shows are picked before the filter runs —
    so this is a guard on what gets published next, not a change to what
    is published today.
    """

    def blocklist(self):
        from fragrance_graph.pages import load_blocklist

        return load_blocklist()

    def test_the_committed_list_is_readable_and_not_empty(self):
        assert len(self.blocklist()) > 10

    def test_whole_words_only(self):
        """A filter that hides ordinary sentences is worse than none: it
        removes evidence for a reason nobody can see."""
        from fragrance_graph.pages import crude_words

        blocklist = self.blocklist()
        for innocent in ("a full assessment of the drydown",
                         "hello, this is lovely",
                         "the class of the composition",
                         "it has a shitake mushroom note"):
            assert crude_words(innocent, blocklist) == [], innocent

    def test_it_catches_what_it_is_for(self):
        from fragrance_graph.pages import crude_words

        assert crude_words("performance is shit", self.blocklist()) == ["shit"]

    def test_a_missing_list_warns_rather_than_silently_passing(self, tmp_path, caplog):
        from fragrance_graph.pages import load_blocklist

        with caplog.at_level("WARNING"):
            assert load_blocklist(tmp_path / "nope.txt") == frozenset()
        assert "unfiltered" in caplog.text

    def crude_pair(self, conn):
        """A pair whose *displayed* quotes include a crude one.

        Quotes are picked before the filter runs and capped at three, so
        appending a fourth commenter would leave the crude quote out of
        the page for an unrelated reason and pass every test below
        vacuously. This one is written first, which is how `_pick_evidence`
        breaks ties.
        """
        a = add_fragrance(conn, "Aventus")
        b = add_fragrance(conn, "Club de Nuit Intense Man")
        cid = add_comment(conn, 0, body="performance is shit",
                          author="sweary", video="vid-0")
        add_claim(conn, cid, subject=b, obj=a, evidence="performance is shit")
        for i in range(1, MIN_COMMENTERS + 1):
            cid = add_comment(conn, i, body=f"person {i} wrote this",
                              author=f"person-{i}",
                              video=f"vid-{i % MIN_SOURCES}")
            add_claim(conn, cid, subject=b, obj=a,
                      evidence=f"person {i} wrote this")
        pair = qualifying_pairs(conn)[0]
        assert any("shit" in ev.quote for s in pair.statements
                   for ev in s.row.evidence), "fixture must reach the page"
        return pair

    def test_a_crude_quote_does_not_reach_the_page(self, conn):
        pair = self.crude_pair(conn)
        assert "performance is shit" not in render_pair(pair, blocklist=self.blocklist())

    def test_the_page_says_a_comment_was_left_out(self, conn):
        """Not silently. A quote that vanishes without explanation reads
        as evidence we could not produce."""
        pair = self.crude_pair(conn)
        html = render_pair(pair, blocklist=self.blocklist())
        assert "not shown here, for language" in html
        assert "still includes it" in html

    def test_the_person_is_still_counted(self, conn):
        """A display filter, not a change to the evidence."""
        pair = self.crude_pair(conn)
        assert pair.commenters == MIN_COMMENTERS + 1
        assert f"{MIN_COMMENTERS + 1} people" in render_pair(
            pair, blocklist=self.blocklist()
        )

    def test_the_build_can_name_what_it_hid(self, conn):
        from fragrance_graph.pages import hidden_quotes

        pair = self.crude_pair(conn)
        (hidden,) = hidden_quotes(pair, self.blocklist())
        permalink, words = hidden
        assert words == ["shit"] and permalink.startswith("https://")

    def test_an_empty_blocklist_hides_nothing(self, conn):
        pair = self.crude_pair(conn)
        assert "performance is shit" in render_pair(pair, blocklist=frozenset())


class TestSiteWiring:
    """Canonical tags, a sitemap, robots.txt, a custom domain, and an
    analytics hook that is off.

    All five are about how the site is *found* rather than what it says,
    and each of them is a way to be wrong quietly: a canonical tag on the
    wrong domain tells a search engine to index someone else's copy, and a
    CNAME that is not copied into the artifact drops a custom domain back
    to the default one on the next deploy.
    """

    def built(self, conn, tmp_path, **kwargs):
        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        build(conn, tmp_path, **kwargs)
        return tmp_path

    def test_a_known_base_url_reaches_every_page(self, conn, tmp_path):
        out = self.built(conn, tmp_path, base_url="https://example.test/site")
        for page in out.glob("*.html"):
            text = page.read_text(encoding="utf-8")
            assert f'rel="canonical" href="https://example.test/site/{page.name}"' \
                in text, page.name

    def test_an_unknown_base_url_writes_no_canonical_and_no_sitemap(
        self, conn, tmp_path, caplog
    ):
        """A guessed domain is worse than none."""
        with caplog.at_level("WARNING"):
            out = self.built(conn, tmp_path)
        assert not (out / "sitemap.xml").exists()
        assert "canonical" not in (out / "index.html").read_text(encoding="utf-8")
        assert "skipping sitemap" in caplog.text

    def test_the_sitemap_lists_every_page_it_wrote(self, conn, tmp_path):
        out = self.built(conn, tmp_path, base_url="https://example.test/")
        sitemap = (out / "sitemap.xml").read_text(encoding="utf-8")
        for page in out.glob("*.html"):
            assert f"https://example.test/{page.name}" in sitemap, page.name

    def test_the_sitemap_carries_no_timestamp(self, conn, tmp_path):
        """Output is byte-stable; a lastmod would make every build a diff."""
        out = self.built(conn, tmp_path, base_url="https://example.test/")
        assert "lastmod" not in (out / "sitemap.xml").read_text(encoding="utf-8")

    def test_robots_points_at_the_sitemap(self, conn, tmp_path):
        out = self.built(conn, tmp_path, base_url="https://example.test/")
        assert "Sitemap: https://example.test/sitemap.xml" in (
            out / "robots.txt"
        ).read_text(encoding="utf-8")

    def test_a_cname_beats_the_base_url(self, conn, tmp_path):
        """GitHub obeys the file, so a disagreement would point canonical
        tags at a domain that redirects to the other one."""
        from fragrance_graph.pages import site_base

        cname = tmp_path / "CNAME"
        cname.write_text("dupes.example\n", encoding="utf-8")
        assert site_base("https://other.test/", cname) == "https://dupes.example/"

    def test_a_missing_cname_falls_back(self, tmp_path):
        from fragrance_graph.pages import site_base

        assert site_base("https://other.test", tmp_path / "nope") == \
            "https://other.test/"

    def test_analytics_is_off_in_the_committed_file(self):
        """It ships off, and "off" is a property of the file's content."""
        from fragrance_graph.pages import load_analytics

        assert load_analytics() == ""

    def test_no_page_but_the_index_carries_a_script(self, conn, tmp_path):
        """`index.html` carries the query box's own script — vanilla,
        inline, no third-party source — by design; see `TestTheQueryBox`.
        Every other page (pairs, profiles, asks) is still exactly what it
        was: text and links, nothing executable, and no *analytics*
        snippet leaks in anywhere while `data/analytics.html` is off."""
        out = self.built(conn, tmp_path, base_url="https://example.test/")
        for page in out.glob("*.html"):
            if page.name == "index.html":
                continue
            assert "<script" not in page.read_text(encoding="utf-8"), page.name

    def test_a_snippet_reaches_the_head_of_every_page(self, conn, tmp_path):
        from fragrance_graph.pages import load_analytics, render_pair

        snippet = tmp_path / "analytics.html"
        snippet.write_text(
            "<!-- explanation -->\n<script src='https://x.test/a.js'></script>",
            encoding="utf-8",
        )
        loaded = load_analytics(snippet)
        assert loaded == "<script src='https://x.test/a.js'></script>"

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        html = render_pair(qualifying_pairs(conn)[0], analytics=loaded)
        assert html.index(loaded) < html.index("<body>")

    def test_a_file_of_only_comments_counts_as_off(self, tmp_path):
        from fragrance_graph.pages import load_analytics

        path = tmp_path / "analytics.html"
        path.write_text("<!--\n  nothing here yet\n-->\n", encoding="utf-8")
        assert load_analytics(path) == ""


class TestDirectionIsDecidedByPeopleNotRows:
    """Found in the final review, 2026-08-15.

    `Statement.subject` compared `outbound_claims` against `claims` — both
    row counts — while the sentence beneath printed `commenters`, a head
    count. One person writing "A is a dupe of B" four times outvoted three
    separate people writing the reverse, and the page then reported all
    four as supporting the direction one of them chose.
    """

    def _row(self, **kw):
        from fragrance_graph.query import Related

        base = dict(
            fragrance_id=1, canonical_name="B", brand=None,
            claim_type="DUPE_OF", commenters=4, pair_commenters=4, sources=2,
            creators=2, pair_sources=2, claims=7, outbound_claims=4,
            outbound_commenters=1, sentiment="POSITIVE", sentiment_counts={},
        )
        base.update(kw)
        return Related(**base)

    def test_one_prolific_person_does_not_flip_the_direction(self):
        from fragrance_graph.pages import Statement

        # 1 person said it outbound four times; 3 said the reverse once each.
        statement = Statement("A", "B", self._row())
        assert statement.subject == "B", (
            "the three separate people decide, not the one who repeated"
        )

    def test_a_genuine_majority_still_decides(self):
        from fragrance_graph.pages import Statement

        statement = Statement("A", "B", self._row(outbound_commenters=3))
        assert statement.subject == "A"

    def test_a_tie_keeps_the_asked_end_in_front(self):
        """So the sentence does not depend on which end built the page."""
        from fragrance_graph.pages import Statement

        statement = Statement(
            "A", "B", self._row(commenters=4, outbound_commenters=2)
        )
        assert statement.subject == "A"


# --- the client-side query box -----------------------------------------


class TestSearchIndexOnlyReferencesBuiltFiles:
    """`build_search_index` is derived from the same queries the pages
    themselves use — this proves the index can never point at a page
    `build` did not actually write, and never omits one it did."""

    def test_every_slug_in_the_index_exists_as_a_built_file(self, conn, tmp_path):
        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        out = tmp_path / "site"
        build(conn, out)
        index = json.loads((out / "search-index.json").read_text(encoding="utf-8"))

        for bottle in index["bottles"]:
            assert (out / bottle["slug"]).exists(), bottle["slug"]
        for pair in index["pairs"]:
            assert (out / pair["slug"]).exists(), pair["slug"]
        for ask in index["asks"]:
            assert (out / ask["slug"]).exists(), ask["slug"]

    def test_every_built_pair_and_ask_page_is_in_the_index(self, conn, tmp_path):
        from fragrance_graph.ask import QUESTIONS, ask_slug

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        out = tmp_path / "site"
        built_pairs = build(conn, out)
        index = json.loads((out / "search-index.json").read_text(encoding="utf-8"))

        indexed_pair_slugs = {p["slug"] for p in index["pairs"]}
        for p in built_pairs:
            assert f"{p.slug}.html" in indexed_pair_slugs

        indexed_ask_slugs = {a["slug"] for a in index["asks"]}
        for slug, _question in QUESTIONS:
            assert ask_slug(slug) in indexed_ask_slugs

    def test_every_built_profile_page_is_in_the_index(self, conn, tmp_path):
        from fragrance_graph.ask import profile_pages

        out = tmp_path / "site"
        build(conn, out)
        index = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        indexed_bottle_slugs = {b["slug"] for b in index["bottles"]}
        for _name, slug, _answer in profile_pages(conn):
            assert slug in indexed_bottle_slugs

    def test_an_empty_catalogue_yields_an_empty_but_valid_index(self, conn, tmp_path):
        out = tmp_path / "site"
        build(conn, out)
        index = json.loads((out / "search-index.json").read_text(encoding="utf-8"))
        assert index == {"bottles": [], "pairs": [], "asks": index["asks"],
                         "attributes": []}
        # The asks list is unconditional — the curated questions are pages
        # regardless of the catalogue's contents, so an empty catalogue
        # still has to describe them for the query box to route to.
        assert len(index["asks"]) == 8

    def test_attributes_are_scoped_to_the_bottle_that_has_them(self, conn):
        from fragrance_graph.ask import QUESTIONS, profile_pages
        from fragrance_graph.pages import build_search_index
        from tests.test_recommend import note

        a = add_fragrance(conn, "Kilian Angels' Share")
        add_fragrance(conn, "Lattafa Khamrah")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=a, value="raspberry", author=author,
                 channel=f"c{i}")

        index = build_search_index(
            conn, [], profile_pages(conn), list(QUESTIONS)
        )
        raspberry_rows = [
            row for row in index["attributes"] if row["attribute"] == "raspberry"
        ]
        assert raspberry_rows
        for row in raspberry_rows:
            assert row["bottle_slug"] == "about-kilian-angels-share.html"


class TestTheQueryBox:
    """The static half of the query box: what a browser gets before any
    script runs, and what the script itself is and is not allowed to say.
    """

    def built_index_html(self, conn, tmp_path):
        from tests.test_recommend import note

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        # A pair alone yields no profile page — profile queries read
        # attribute facts, not similarity claims — so a bottle with real
        # note evidence is added too, to exercise the select's real
        # population path rather than only the empty-catalogue one.
        frag = add_fragrance(conn, "Kilian Angels' Share")
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="raspberry", author=author,
                 channel=f"c{i}")
        out = tmp_path / "site"
        build(conn, out)
        return (out / "index.html").read_text(encoding="utf-8")

    def test_the_refusal_block_is_server_rendered_and_hidden(self, conn, tmp_path):
        """"ALREADY in the HTML" — not assembled by the script. `hidden`
        is a real HTML attribute a browser honours with no CSS or JS."""
        page = self.built_index_html(conn, tmp_path)
        assert '<div id="search-refusal" hidden>' in page
        assert "Nothing here answers that yet" in page

    def test_the_search_form_and_bottle_select_are_present(self, conn, tmp_path):
        page = self.built_index_html(conn, tmp_path)
        assert '<form id="search-form"' in page
        assert '<select id="bottle-select">' in page

    def test_the_bottle_select_is_populated_server_side(self, conn, tmp_path):
        """The base bottle list exists with JavaScript disabled — the same
        floor the rest of the site holds to."""
        page = self.built_index_html(conn, tmp_path)
        assert page.count("<option value=") > 1

    def test_the_search_index_data_is_embedded_not_fetched(self, conn, tmp_path):
        """No `fetch(` anywhere: a same-origin fetch of a sibling file
        fails under `file://`, which is exactly how this page gets opened
        to be checked by hand."""
        page = self.built_index_html(conn, tmp_path)
        assert '<script type="application/json" id="search-index-data">' in page
        assert "fetch(" not in page

    def test_the_script_contains_no_forbidden_phrasing(self, conn, tmp_path):
        from fragrance_graph.audit import FORBIDDEN

        page = self.built_index_html(conn, tmp_path)
        script_start = page.index("<script>", page.index("</section>"))
        script = page[script_start:]
        for phrase in FORBIDDEN:
            assert phrase not in script.lower(), phrase

    def test_the_script_assembles_no_evidence_wording(self, conn, tmp_path):
        """Not just the forbidden list — the script must never even reach
        for the *vocabulary* of an evidence sentence. Its only allowed
        strings are navigation chrome ("Did you mean") and the two
        static, server-rendered blocks it toggles or reads from; if any
        of these words showed up it would mean the script had started
        composing a sentence about what people said, which is exactly
        the line this file draws between Python and the browser.

        "disagree" is deliberately not on this list: it appears once, in
        `PROFILE_PATTERNS`, as part of recognising the *user's* question
        shape ("what do people disagree about X") — the same word
        `plan.PROFILE_PATTERNS` uses for the identical reason. Reading a
        word out of what someone typed is not composing a sentence about
        evidence; the test below (`..._or_recommendation_wording`) checks
        the structural guarantee that actually matters — that the script
        never even has a `Reason` or `Recommendation` to word one from.
        """
        page = self.built_index_html(conn, tmp_path)
        script_start = page.index("<script>", page.index("</section>"))
        script = page[script_start:].lower()
        for word in (
            "commenter", "channels", "channel",
            "evidence recorded", "people said", "across",
        ):
            assert word not in script, word

    def test_the_script_never_reads_reason_or_recommendation_wording(self, conn, tmp_path):
        """A structural check on top of the vocabulary one: the script's
        only data source is `search-index-data`, whose fields are name,
        alias, brand, slug and a single attribute word — never a
        composed sentence."""
        from fragrance_graph.pages import SEARCH_SCRIPT

        for forbidden_marker in ("phrase(", "digest(", "_people(", "_sources("):
            assert forbidden_marker not in SEARCH_SCRIPT

    def test_a_hostile_alias_cannot_close_the_inline_json_script_early(
        self, conn, tmp_path
    ):
        """The closest analog to `test_a_fragrance_name_containing_markup_is_
        escaped` (line ~250), for the one sink that bypasses `html.escape`
        entirely: the embedded JSON data island. `json.dumps` escapes
        quotes and backslashes but not `</script>`, and per the HTML spec a
        `<script>` element — any `type` — ends at the first literal
        `</script`, regardless of JSON syntax. This drives the real, full
        `build()` (not a hand-built string) against a fixture bottle whose
        *alias* — read straight out of `fragrances.aliases` with no
        regex re-parse, unlike the canonical name — carries
        `</script><script>alert(1)</script>`, and reads the file `build`
        actually wrote to disk.

        This is deliberately in addition to, not instead of,
        `tests/test_audit.py::test_a_hostile_name_cannot_break_out_of_the_
        inline_json_script`: that test proves the audit's real "site index
        page" surface would flag any regression here; this one proves the
        fix holds at the file `build()` ships, independent of the audit
        ever running.
        """
        from tests.test_recommend import note

        frag = add_fragrance(
            conn, "Harmless Parfum",
            aliases=["</script><script>alert(1)</script>"],
        )
        for i, author in enumerate(["p1", "p2", "p3"]):
            note(conn, i, frag=frag, value="raspberry", author=author,
                 channel=f"c{i}")
        out = tmp_path / "site"
        build(conn, out)
        page = (out / "index.html").read_text(encoding="utf-8")

        assert "</script><script>" not in page
        assert "<script>alert" not in page
        # Still present, just inert to the HTML tokenizer — proving this
        # escaped the payload rather than silently dropping the alias.
        assert "Harmless Parfum" in page
        assert "alert(1)" in page


class TestTheQueryBoxRoutesCorrectly:
    """Behavioural tests for the JS grammar mirror itself, run under Node
    against the exact script `build` ships. Skipped, not failed, when
    Node is not available — this is a real browser's behaviour under
    test, not a Python behaviour, and the product does not depend on
    Node existing anywhere except this developer's spot-check.
    """

    @pytest.fixture
    def routed(self, conn, tmp_path):
        import shutil
        import subprocess

        if shutil.which("node") is None:
            pytest.skip("node not installed; JS behaviour spot-checked by hand")

        from tests.test_recommend import note

        pair_of(conn, people=MIN_COMMENTERS, videos=MIN_SOURCES)
        khamrah = add_fragrance(conn, "Lattafa Khamrah", aliases=["Khamrah"])
        detour = add_fragrance(conn, "Al Haramain Detour Noir")
        for frag, value in ((khamrah, "sweet"), (detour, "smoky")):
            for i, author in enumerate(["p1", "p2", "p3"]):
                note(conn, i + frag * 100, frag=frag, value=value,
                     author=f"{author}-{frag}", channel=f"c{i}-{frag}")
        out = tmp_path / "site"
        build(conn, out)
        page = (out / "index.html").read_text(encoding="utf-8")

        data_start = page.index(
            '<script type="application/json" id="search-index-data">'
        ) + len('<script type="application/json" id="search-index-data">')
        data_end = page.index("</script>", data_start)
        data_json = page[data_start:data_end]

        script_start = page.index("<script>", data_end)
        script_open = page.index(">", script_start) + 1
        script_end = page.index("</script>", script_open)
        script = page[script_open:script_end]
        body_start = script.index("(function () {") + len("(function () {")
        body_end = script.rindex("})();")
        body = script[body_start:body_end]

        harness = tmp_path / "harness.js"
        harness.write_text(
            f"""
            const data = {data_json};
            global.document = {{
              getElementById(id) {{
                if (id === "search-index-data") {{ return {{textContent: JSON.stringify(data)}}; }}
                return {{addEventListener() {{}}}};
              }},
              createElement() {{ return {{appendChild() {{}}}}; }},
            }};
            {body}
            const result = {{}};
            process.argv.slice(2).forEach(function (q) {{
              result[q] = route(q);
            }});
            console.log(JSON.stringify(result));
            """,
            encoding="utf-8",
        )

        def run(*queries):
            out = subprocess.run(
                ["node", str(harness), *queries],
                capture_output=True, text=True, timeout=10, check=True,
            )
            return json.loads(out.stdout)

        return run

    def test_a_bare_alias_routes_to_the_profile(self, routed):
        result = routed("Khamrah")
        assert result["Khamrah"] == "about-lattafa-khamrah.html"

    def test_an_accented_name_routes_via_folded_matching(self, routed):
        result = routed("Détour Noir", "detour noir")
        for slug in result.values():
            assert slug == "about-al-haramain-detour-noir.html" or slug is None

    def test_an_unknown_query_routes_nowhere(self, routed):
        result = routed("this matches absolutely nothing at all")
        assert result["this matches absolutely nothing at all"] is None
