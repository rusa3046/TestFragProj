"""Entity resolution: normalisation, junk rejection, matching, backfill.

Cases are drawn from the first live YouTube run rather than invented —
the six spellings of Baccarat Rouge 540, the video timestamp read as a
subject, the case-and-spacing variants of Thomas Kosmala no.4.
"""

import json

import pytest

from fragrance_graph.extract.llm import write_claims
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.ingest.store import ingest
from fragrance_graph.models import Claim
from fragrance_graph.resolve.entities import (
    add_alias,
    add_fragrance,
    backfill,
    load_candidates,
    resolved_edges,
    unresolved_mentions,
)
from fragrance_graph.resolve.names import (
    Candidate,
    best_match,
    looks_like_junk,
    normalize_name,
    similarity,
)
from tests.conftest import make_comment

BR540 = Candidate(1, "Baccarat Rouge 540", ("BR540", "BR 540", "B540", "BR MFK 540"))
KOSMALA = Candidate(2, "Thomas Kosmala No. 4")


# --- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        ("Thomas Kosmala no.4", "thomas kosmala no. 4"),
        ("Red Temptation from ZARA", "Red Temptation ZARA"),
        ("Baccarat Rouge 540", "baccarat  rouge  540"),
        ("Wulóng Chá", "Wulong Cha"),
    ],
)
def test_formatting_differences_converge(a, b):
    assert normalize_name(a) == normalize_name(b)


def test_normalisation_does_not_merge_distinct_fragrances():
    """The one failure this module must never make."""
    assert normalize_name("Aventus") != normalize_name("Aventus Cologne")
    assert normalize_name("Dior Homme") != normalize_name("Dior Homme Intense")


# --- junk -------------------------------------------------------------------


@pytest.mark.parametrize("text", ["3:32", "1:04:20", "12:00"])
def test_video_timestamps_are_junk(text):
    """Observed live: '3:32' was extracted as a PROJECTION subject."""
    assert looks_like_junk(text)


@pytest.mark.parametrize("text", ["540", "  ", "", "BR", "a"])
def test_bare_numbers_and_stubs_are_junk(text):
    assert looks_like_junk(text)


@pytest.mark.parametrize(
    "text", ["Untold", "BR540", "Bujairami Lavish", "Ariana cloud"]
)
def test_plausible_names_are_not_junk(text):
    assert not looks_like_junk(text)


def test_a_curated_alias_overrules_the_junk_heuristic():
    """'540' looks like a bare number and was the commonest live spelling.

    The junk rule is a guess about text; an alias is a person stating a
    fact. Observed live: '540' appeared three times, more than any other
    mention, all meaning Baccarat Rouge 540.
    """
    assert looks_like_junk("540")

    aliased = Candidate(1, "Baccarat Rouge 540", ("540",))
    match = best_match("540", [aliased])
    assert match is not None and match.method == "exact"


def test_junk_still_blocks_fuzzy_matching():
    """Without an alias, junk must not drift into a match."""
    assert best_match("540", [BR540]) is None
    assert best_match("3:32", [BR540]) is None


# --- matching ---------------------------------------------------------------


def test_exact_match_after_normalisation():
    match = best_match("thomas kosmala no. 4", [BR540, KOSMALA])
    assert (match.fragrance_id, match.method) == (2, "exact")


def test_alias_resolves_an_abbreviation_string_similarity_cannot():
    """'BR540' vs 'Baccarat Rouge 540' scores 0.43 — far below any threshold."""
    assert similarity("BR540", "Baccarat Rouge 540") < 0.5

    match = best_match("BR540", [BR540])
    assert (match.fragrance_id, match.method) == (1, "exact")


@pytest.mark.parametrize("mention", ["BR540", "BR 540", "B540", "BR MFK 540"])
def test_every_observed_spelling_resolves_to_one_fragrance(mention):
    """The five dupe claims that were individually right, collectively useless."""
    match = best_match(mention, [BR540])
    assert match is not None and match.fragrance_id == 1


def test_fuzzy_match_catches_a_typo():
    match = best_match("Baccarat Rouge 54O", [BR540])  # letter O for zero
    assert match is not None and match.method == "fuzzy"


def test_unrelated_mention_does_not_match():
    assert best_match("Aventus", [BR540, KOSMALA]) is None


def test_threshold_is_respected():
    assert best_match("Baccarat", [BR540], threshold=0.99) is None


def test_match_is_stable_regardless_of_candidate_order():
    forward = best_match("BR540", [BR540, KOSMALA])
    reverse = best_match("BR540", [KOSMALA, BR540])
    assert forward.fragrance_id == reverse.fragrance_id


# --- database round trip ----------------------------------------------------


def seed_claim(conn, *, subject, obj, claim_type="DUPE_OF", subject_kind="FRAGRANCE",
               object_kind="FRAGRANCE", body=None):
    body = body or f"{subject} is a dupe of {obj}"
    ingest(conn, [make_comment(abs(hash((subject, obj))) % 90000, body=body)])
    comment_id = conn.execute(
        "SELECT id FROM comments ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    claim = Claim(
        claim_type=claim_type,
        subject_kind=subject_kind,
        raw_subject_text=subject,
        object_kind=object_kind,
        raw_object_text=obj,
        confidence=0.9,
        evidence_span=body,
    )
    write_claims(conn, comment_id, body, [claim])
    return comment_id


def test_add_fragrance_and_load_candidates(conn):
    frag_id = add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    candidates = load_candidates(conn)

    assert candidates[0].fragrance_id == frag_id
    assert "BR540" in candidates[0].aliases


def test_add_alias_is_idempotent_and_sorted(conn):
    frag_id = add_fragrance(conn, "Baccarat Rouge 540")
    add_alias(conn, frag_id, "BR540")
    aliases = add_alias(conn, frag_id, "BR540")

    assert aliases == ["BR540"]


def test_add_alias_to_missing_fragrance_raises(conn):
    with pytest.raises(KeyError):
        add_alias(conn, 999, "x")


def test_unresolved_mentions_are_ranked_by_frequency(conn):
    seed_claim(conn, subject="Zara Red Temptation", obj="BR540")
    seed_claim(conn, subject="Dossier Ambery Saffron", obj="BR540")
    seed_claim(conn, subject="Bujairami Lavish", obj="BR 540")

    mentions = unresolved_mentions(conn)
    assert mentions[0].text == "BR540" and mentions[0].count == 2


def test_junk_is_hidden_from_the_report_but_available(conn):
    seed_claim(conn, subject="3:32", obj="BR540")

    assert "3:32" not in [m.text for m in unresolved_mentions(conn)]
    with_junk = unresolved_mentions(conn, include_junk=True)
    assert any(m.text == "3:32" and m.is_junk for m in with_junk)


def test_non_fragrance_kinds_are_never_reported_for_resolution(conn):
    """A HOUSE object and CATEGORY subject are signal, but not bottles."""
    seed_claim(
        conn,
        subject="skin scents",
        obj="Serge Lutens",
        claim_type="SIMILAR_TO",
        subject_kind="CATEGORY",
        object_kind="HOUSE",
    )
    assert unresolved_mentions(conn) == []


def test_backfill_populates_both_ends(conn):
    frag = add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    zara = add_fragrance(conn, "Zara Red Temptation")
    seed_claim(conn, subject="Zara Red Temptation", obj="BR540")

    stats = backfill(conn)
    row = conn.execute(
        "SELECT subject_frag_id, object_frag_id FROM claims"
    ).fetchone()

    assert (row["subject_frag_id"], row["object_frag_id"]) == (zara, frag)
    assert stats.total == 2


def test_backfill_is_idempotent(conn):
    add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    add_fragrance(conn, "Zara Red Temptation")
    seed_claim(conn, subject="Zara Red Temptation", obj="BR540")

    backfill(conn)
    assert backfill(conn).total == 0, "already-resolved rows must be skipped"


def test_dry_run_reports_without_writing(conn):
    add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    seed_claim(conn, subject="Unknown Thing", obj="BR540")

    stats = backfill(conn, dry_run=True)
    stored = conn.execute("SELECT object_frag_id FROM claims").fetchone()[0]

    assert stats.total == 1
    assert stored is None


def test_backfill_with_no_fragrances_resolves_nothing(conn):
    seed_claim(conn, subject="Zara Red Temptation", obj="BR540")
    assert backfill(conn).total == 0


def test_unparseable_aliases_do_not_crash_loading(conn):
    add_fragrance(conn, "Broken")
    conn.execute("UPDATE fragrances SET aliases = 'not json'")
    conn.commit()

    candidates = load_candidates(conn)
    assert candidates[0].aliases == ()


# --- the payoff -------------------------------------------------------------


def test_separate_spellings_collapse_into_one_counted_edge(conn):
    """The whole point of Phase 2, in one test.

    Three commenters, three spellings of the same fragrance. Before
    resolution these are three unrelated nodes; after, one edge with a
    weight of three.
    """
    add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540", "BR 540", "B540"])
    add_fragrance(conn, "Thomas Kosmala No. 4")

    for spelling in ("BR540", "BR 540", "B540"):
        seed_claim(conn, subject="Thomas Kosmala no.4", obj=spelling)

    backfill(conn)
    edges = resolved_edges(conn)

    assert len(edges) == 1, "three spellings must collapse to one edge"
    assert edges[0]["subject"] == "Thomas Kosmala No. 4"
    assert edges[0]["object"] == "Baccarat Rouge 540"
    assert edges[0]["mentions"] == 3


def test_edges_exclude_unverified_evidence(conn):
    add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    add_fragrance(conn, "Zara Red Temptation")
    comment_id = seed_claim(conn, subject="Zara Red Temptation", obj="BR540")
    conn.execute("UPDATE claims SET evidence_verified = 0 WHERE comment_id = %s",
                 (comment_id,))
    conn.commit()

    backfill(conn)
    assert resolved_edges(conn) == []


def test_edges_exclude_non_comparison_claims(conn):
    add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540"])
    add_fragrance(conn, "Zara Red Temptation")
    seed_claim(
        conn,
        subject="Zara Red Temptation",
        obj="BR540",
        claim_type="SIMILAR_TO",
    )
    seed_claim(
        conn,
        subject="Zara Red Temptation",
        obj="citrusy",
        claim_type="NOTE_DESCRIPTOR",
        object_kind="TAG",
    )

    backfill(conn)
    edges = resolved_edges(conn)
    assert [e["claim_type"] for e in edges] == ["SIMILAR_TO"]


def test_aliases_are_stored_as_readable_json(conn):
    """A curator must be able to inspect and correct these by hand."""
    frag_id = add_fragrance(conn, "Baccarat Rouge 540", aliases=["BR540", "540 MFK"])
    raw = conn.execute(
        "SELECT aliases FROM fragrances WHERE id = %s", (frag_id,)
    ).fetchone()[0]

    assert json.loads(raw) == ["540 MFK", "BR540"]


# --- what naming a mention would publish ------------------------------------


def seed_edge(conn, i, *, mention, other_id, author, channel,
              claim_type="DUPE_OF"):
    """One unresolved mention sitting opposite an already-curated bottle."""
    body = f"{mention} is a dupe of something"
    ingest(conn, [make_comment(
        i, body=body, source_channel=channel,
        raw_json=json.dumps({"author": author, "videoId": f"vid-{channel}"}),
    )])
    comment_id = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"t1_fake{i:05d}",)
    ).fetchone()[0]
    conn.execute(
        """
        INSERT INTO claims
            (comment_id, claim_type, subject_kind, raw_subject_text,
             subject_frag_id, object_kind, raw_object_text, object_frag_id,
             sentiment, confidence, evidence_span, evidence_verified,
             polarity, extraction_model, created_at)
        VALUES (%s, %s, 'FRAGRANCE', %s, NULL, 'FRAGRANCE', 'other', %s,
                'POSITIVE', 0.9, %s, 1, 'ASSERTED', 'test', '2026-01-01')
        """,
        (comment_id, claim_type, mention, other_id, body),
    )
    conn.commit()
    return comment_id


class TestMentionValue:
    """Curation time is the scarce resource, so the queue has to be ordered
    by what naming something *publishes* rather than by how often it was
    typed. On the committed corpus those two orders barely overlap: "this"
    is written 138 times and unlocks nothing, "liquid brun" 11 times and
    unlocks a page.
    """

    def test_frequency_alone_does_not_win(self, conn):
        from fragrance_graph.resolve.entities import mention_values

        aventus = add_fragrance(conn, "Creed Aventus")
        layton = add_fragrance(conn, "Parfums de Marly Layton")
        # Written far more often, always against the same bottle.
        for i in range(6):
            seed_edge(conn, i, mention="loud one", other_id=aventus,
                      author=f"p{i}", channel="UC-a")
        # Written twice, but across two different bottles.
        seed_edge(conn, 20, mention="quiet one", other_id=aventus,
                  author="q1", channel="UC-a")
        seed_edge(conn, 21, mention="quiet one", other_id=layton,
                  author="q2", channel="UC-b")

        values = {v.text: v for v in mention_values(conn)}
        assert values["loud one"].occurrences == 6
        assert values["loud one"].pairs == 1
        assert values["quiet one"].pairs == 2
        assert [v.text for v in mention_values(conn)][0] == "quiet one"

    def test_it_counts_pairs_that_would_actually_publish(self, conn):
        from fragrance_graph.resolve.entities import mention_values

        aventus = add_fragrance(conn, "Creed Aventus")
        for i in range(MIN_COMMENTERS):
            seed_edge(conn, i, mention="mystery juice", other_id=aventus,
                      author=f"person-{i}", channel=f"UC-{i % MIN_SOURCES}")

        (value,) = [v for v in mention_values(conn) if v.text == "mystery juice"]
        assert value.publishable == 1

    def test_one_comment_section_does_not_count_as_publishable(self, conn):
        """The same bar a page clears: three people in one room are one room."""
        from fragrance_graph.resolve.entities import mention_values

        aventus = add_fragrance(conn, "Creed Aventus")
        for i in range(MIN_COMMENTERS + 2):
            seed_edge(conn, i, mention="mystery juice", other_id=aventus,
                      author=f"person-{i}", channel="UC-one")

        (value,) = [v for v in mention_values(conn) if v.text == "mystery juice"]
        assert value.pairs == 1 and value.publishable == 0

    def test_spellings_of_one_bottle_are_one_row(self, conn):
        """A curator names a bottle, not a string, and `backfill` resolves
        every spelling of it at once."""
        from fragrance_graph.resolve.entities import mention_values

        aventus = add_fragrance(conn, "Creed Aventus")
        seed_edge(conn, 1, mention="Liquid Brun", other_id=aventus,
                  author="a", channel="UC-a")
        seed_edge(conn, 2, mention="liquid brun", other_id=aventus,
                  author="b", channel="UC-b")
        seed_edge(conn, 3, mention="liquid  brun", other_id=aventus,
                  author="c", channel="UC-c")

        (value,) = [v for v in mention_values(conn)
                    if v.text.lower().startswith("liquid")]
        assert value.occurrences == 3
        assert len(value.variants) == 3
        assert value.publishable == 1, "three people, three creators"

    def test_a_mention_with_no_curated_neighbour_is_absent(self, conn):
        """Naming it publishes nothing today: both ends are unknown."""
        from fragrance_graph.resolve.entities import mention_values

        seed_claim(conn, subject="Unknown A", obj="Unknown B")
        assert mention_values(conn) == []

    def test_claims_that_can_never_be_a_page_are_ignored(self, conn):
        """A NOTE_DESCRIPTOR unlocks nothing, however often it is written."""
        from fragrance_graph.resolve.entities import mention_values

        aventus = add_fragrance(conn, "Creed Aventus")
        for i in range(4):
            seed_edge(conn, i, mention="smells woody", other_id=aventus,
                      author=f"p{i}", channel=f"UC-{i}",
                      claim_type="NOTE_DESCRIPTOR")
        assert mention_values(conn) == []

    def test_the_order_is_stable(self, conn):
        from fragrance_graph.resolve.entities import mention_values

        aventus = add_fragrance(conn, "Creed Aventus")
        for i in range(6):
            seed_edge(conn, i, mention=f"bottle {i}", other_id=aventus,
                      author=f"p{i}", channel=f"UC-{i}")
        assert [v.text for v in mention_values(conn)] == [
            v.text for v in mention_values(conn)
        ]


class TestBatchFile:
    """The offline curation loop: a file a person fills in on a train.

    No network, no spend. The catalogue experiment on 2026-08-12 cost
    $3.00 for 60 lookups and produced 5 names and 0 pages, because the
    catalogue does not carry the small houses this corpus is about. A
    person who knows fragrance reads two comments and knows the answer.
    """

    def seeded(self, conn, *, mention="mystery juice", people=3, channels=2):
        other = add_fragrance(conn, "Creed Aventus")
        for i in range(people):
            seed_edge(conn, i, mention=mention, other_id=other,
                      author=f"person-{i}", channel=f"UC-{i % channels}")
        return other

    def test_a_row_carries_the_evidence_to_decide_on(self, conn, tmp_path):
        from fragrance_graph.resolve.entities import batch_rows

        self.seeded(conn)
        (row,) = batch_rows(conn)
        assert row.mention == "mystery juice"
        assert row.occurrences == 3
        assert row.pairs_unlocked == 1
        assert row.would_publish == 1
        assert row.connects_to == ["Creed Aventus (3 people, 2 creators)"]
        assert row.examples, "a reviewer needs the sentences people wrote"

    def test_the_decision_fields_start_empty(self, conn):
        from fragrance_graph.resolve.entities import batch_rows

        self.seeded(conn)
        (row,) = batch_rows(conn)
        assert row.canonical_name == "" and row.brand == ""
        assert row.aliases == [] and row.skip is False

    def test_pronouns_never_reach_the_file(self, conn):
        """"this" is the most frequent unresolved mention in the corpus."""
        from fragrance_graph.resolve.entities import batch_rows

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(4):
            seed_edge(conn, i, mention="this", other_id=other,
                      author=f"p{i}", channel=f"UC-{i}")
        assert batch_rows(conn) == []

    def test_a_mention_written_once_never_reaches_the_file(self, conn):
        from fragrance_graph.resolve.entities import batch_rows

        other = add_fragrance(conn, "Creed Aventus")
        seed_edge(conn, 1, mention="heard of it once", other_id=other,
                  author="p", channel="UC-a")
        assert batch_rows(conn) == []

    def test_a_mention_unlocking_nothing_never_reaches_the_file(self, conn):
        from fragrance_graph.resolve.entities import batch_rows

        seed_claim(conn, subject="Unknown A", obj="Unknown B")
        seed_claim(conn, subject="Unknown A", obj="Unknown C")
        assert batch_rows(conn) == []

    def test_a_page_today_outranks_more_pairs_later(self, conn):
        """The queue is ordered by what publishes, then by what connects."""
        from fragrance_graph.resolve.entities import batch_rows

        aventus = add_fragrance(conn, "Creed Aventus")
        layton = add_fragrance(conn, "Parfums de Marly Layton")
        # One pair, but enough people behind it to publish.
        for i in range(MIN_COMMENTERS):
            seed_edge(conn, i, mention="one strong pair", other_id=aventus,
                      author=f"a{i}", channel=f"UC-{i}")
        # Two pairs, two people each: more reach, nothing publishable yet.
        for i, other in enumerate((aventus, layton, aventus, layton)):
            seed_edge(conn, 50 + i, mention="two thin pairs", other_id=other,
                      author=f"b{i}", channel=f"UC-{i}")

        assert [r.mention for r in batch_rows(conn)] == [
            "one strong pair", "two thin pairs",
        ]

    def test_among_equals_the_wider_reach_wins(self, conn):
        from fragrance_graph.resolve.entities import batch_rows

        aventus = add_fragrance(conn, "Creed Aventus")
        layton = add_fragrance(conn, "Parfums de Marly Layton")
        for i in range(2):
            seed_edge(conn, i, mention="one thin pair", other_id=aventus,
                      author=f"a{i}", channel=f"UC-{i}")
        for i, other in enumerate((aventus, layton)):
            seed_edge(conn, 50 + i, mention="two thin pairs", other_id=other,
                      author=f"b{i}", channel=f"UC-{i}")

        assert [r.mention for r in batch_rows(conn)][0] == "two thin pairs"

    def test_it_round_trips_through_disk(self, conn, tmp_path):
        from fragrance_graph.resolve.entities import (
            batch_rows,
            read_batch,
            write_batch,
        )

        self.seeded(conn)
        path = tmp_path / "curate.json"
        write_batch(path, batch_rows(conn))
        assert read_batch(path) == batch_rows(conn)

    def test_a_field_the_reviewer_invented_does_not_break_it(self, conn, tmp_path):
        """People edit these by hand. A stray key is not a crash."""
        from fragrance_graph.resolve.entities import read_batch

        path = tmp_path / "curate.json"
        path.write_text(json.dumps([{
            "mention": "x", "variants": ["x"], "occurrences": 2,
            "pairs_unlocked": 1, "would_publish": 0, "connects_to": [],
            "examples": [], "videos": [], "canonical_name": "Some Bottle",
            "my_own_note": "check this one",
        }]), encoding="utf-8")
        (row,) = read_batch(path)
        assert row.canonical_name == "Some Bottle"


class TestApplyBatch:
    def rows_for(self, conn, **overrides):
        from fragrance_graph.resolve.entities import batch_rows

        rows = batch_rows(conn)
        for row in rows:
            for key, value in overrides.items():
                setattr(row, key, value)
        return rows

    def test_a_named_bottle_becomes_a_fragrance_and_resolves_its_claims(
        self, conn
    ):
        from fragrance_graph.resolve.entities import apply_batch, batch_rows

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(3):
            seed_edge(conn, i, mention="mystery juice", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")

        stats = apply_batch(conn, self.rows_for(
            conn, canonical_name="Armaf Club de Nuit Intense Man",
        ))
        assert stats.added == 1
        assert stats.resolved == 3
        assert batch_rows(conn) == [], "nothing left blocked on it"

    def test_a_bottle_already_curated_gains_an_alias_not_a_second_node(
        self, conn
    ):
        """The failure this tool exists to avoid.

        Most unresolved text is a spelling of something already curated. A
        second node splits that bottle's edges across both halves, and
        nothing downstream can tell which one a name means.
        """
        from fragrance_graph.resolve.entities import apply_batch

        other = add_fragrance(conn, "Creed Aventus")
        existing = add_fragrance(conn, "Al Haramain Detour Noir",
                                 brand="Al Haramain")
        for i in range(3):
            seed_edge(conn, i, mention="Detour", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")

        stats = apply_batch(conn, self.rows_for(
            conn, canonical_name="Al Haramain Detour Noir",
        ))
        assert stats.added == 0 and stats.aliased == 1
        assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 2
        aliases = json.loads(conn.execute(
            "SELECT aliases FROM fragrances WHERE id = %s", (existing,)
        ).fetchone()[0])
        assert "Detour" in aliases

    def test_applying_twice_changes_nothing(self, conn):
        from fragrance_graph.resolve.entities import apply_batch

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(3):
            seed_edge(conn, i, mention="mystery juice", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")

        rows = self.rows_for(conn, canonical_name="Mystery Juice")
        first = apply_batch(conn, rows)
        second = apply_batch(conn, rows)
        assert first.added == 1 and second.added == 0
        assert second.aliased == 0 and second.unchanged == 1
        assert second.resolved == 0

    def test_an_unreviewed_row_stops_the_whole_file(self, conn):
        """Half a file applied is a corpus state nobody can describe.

        A drafted review file was imported as ground truth on 2026-08-11
        and overwrote two hand-made labels. Everything touching curated
        data has refused ambiguity since.
        """
        import pytest

        from fragrance_graph.resolve.entities import (
            UnreviewedRows,
            apply_batch,
            batch_rows,
        )

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(3):
            seed_edge(conn, i, mention="first", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")
        for i in range(10, 13):
            seed_edge(conn, i, mention="second", other_id=other,
                      author=f"q{i}", channel=f"UC-{i % 2}")

        rows = batch_rows(conn)
        rows[0].canonical_name = "Named One"
        before = conn.execute("SELECT count(*) FROM fragrances").fetchone()[0]

        with pytest.raises(UnreviewedRows):
            apply_batch(conn, rows)
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == before, "nothing was written"

    def test_a_skipped_row_is_allowed_and_writes_nothing(self, conn):
        from fragrance_graph.resolve.entities import apply_batch

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(3):
            seed_edge(conn, i, mention="not a bottle", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")

        stats = apply_batch(conn, self.rows_for(conn, skip=True))
        assert stats.skipped == 1 and stats.added == 0
        assert conn.execute("SELECT count(*) FROM fragrances").fetchone()[0] == 1

    def test_every_spelling_becomes_an_alias(self, conn):
        from fragrance_graph.resolve.entities import apply_batch

        other = add_fragrance(conn, "Creed Aventus")
        for i, spelling in enumerate(("Liquid Brun", "liquid brun", "LIQUID BRUN")):
            seed_edge(conn, i, mention=spelling, other_id=other,
                      author=f"p{i}", channel=f"UC-{i}")

        apply_batch(conn, self.rows_for(
            conn, canonical_name="Fragrance World Liquid Brun",
            brand="Fragrance World",
        ))
        aliases = json.loads(conn.execute(
            "SELECT aliases FROM fragrances WHERE canonical_name = %s",
            ("Fragrance World Liquid Brun",),
        ).fetchone()[0])
        assert "liquid brun" in aliases and "LIQUID BRUN" in aliases

    def test_a_year_supplied_by_the_reviewer_is_kept(self, conn):
        from fragrance_graph.resolve.entities import apply_batch

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(3):
            seed_edge(conn, i, mention="mystery juice", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")

        apply_batch(conn, self.rows_for(
            conn, canonical_name="Mystery Juice", house_year=2021,
        ))
        assert conn.execute(
            "SELECT house_year FROM fragrances WHERE canonical_name = 'Mystery Juice'"
        ).fetchone()[0] == 2021


class TestDraftGuard:
    """A draft is worth having. What it must never be is mistaken for a
    review.

    On 2026-08-11 a drafted label file was imported as ground truth and
    overwrote two hand-made labels, one with its subject and object the
    wrong way round. That was a person mistyping a command, not a bug,
    which is why the guard lives in the data rather than in a habit.
    """

    def drafted(self, conn, **overrides):
        from fragrance_graph.resolve.entities import batch_rows

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(3):
            seed_edge(conn, i, mention="mystery juice", other_id=other,
                      author=f"p{i}", channel=f"UC-{i % 2}")
        rows = batch_rows(conn)
        for row in rows:
            row.canonical_name = "Armaf Club de Nuit Intense Man"
            row.drafted_by = "model"
            for key, value in overrides.items():
                setattr(row, key, value)
        return rows

    def test_an_unconfirmed_draft_writes_nothing(self, conn):
        import pytest

        from fragrance_graph.resolve.entities import UnconfirmedDraft, apply_batch

        rows = self.drafted(conn)
        before = conn.execute("SELECT count(*) FROM fragrances").fetchone()[0]
        with pytest.raises(UnconfirmedDraft):
            apply_batch(conn, rows)
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == before

    def test_the_refusal_names_the_rows(self, conn):
        import pytest

        from fragrance_graph.resolve.entities import UnconfirmedDraft, apply_batch

        with pytest.raises(UnconfirmedDraft, match="mystery juice"):
            apply_batch(conn, self.drafted(conn))

    def test_a_confirmed_draft_applies(self, conn):
        from fragrance_graph.resolve.entities import apply_batch

        stats = apply_batch(conn, self.drafted(conn, confirmed=True))
        assert stats.added == 1

    def test_a_row_a_person_typed_needs_no_confirmation(self, conn):
        """`drafted_by` empty means a human filled it in; that *is* the
        confirmation."""
        from fragrance_graph.resolve.entities import apply_batch

        stats = apply_batch(conn, self.drafted(conn, drafted_by=""))
        assert stats.added == 1

    def test_a_drafted_skip_needs_no_confirmation(self, conn):
        """It writes nothing, so there is nothing to stand behind."""
        from fragrance_graph.resolve.entities import apply_batch

        stats = apply_batch(
            conn, self.drafted(conn, canonical_name="", skip=True)
        )
        assert stats.skipped == 1 and stats.added == 0

    def test_the_flag_survives_the_round_trip(self, conn, tmp_path):
        """A draft written to disk must still be a draft when read back."""
        from fragrance_graph.resolve.entities import (
            read_batch,
            write_batch,
        )

        path = tmp_path / "curate.json"
        write_batch(path, self.drafted(conn))
        (row,) = read_batch(path)
        assert row.drafted_by == "model" and row.confirmed is False


class TestPronounPhrases:
    """"this stuff" is the same non-answer as "this", one noun longer.

    It reached a review file on 2026-08-13 and cost a row of a reviewer's
    attention, which is the only budget this tool spends.
    """

    @pytest.mark.parametrize("text", [
        "this", "It", "that", "this stuff", "This stuff", "that one",
        "this fragrance", "the stuff", "these",
    ])
    def test_a_stand_in_is_recognised(self, text):
        from fragrance_graph.resolve.entities import is_pronoun

        assert is_pronoun(text) is True

    @pytest.mark.parametrize("text", [
        "Detour", "liquid brun", "Aventus Cologne", "One Million",
        "this is the best khamrah I own", "Kismet Angel",
    ])
    def test_a_name_is_not(self, text):
        """Longer phrases are left alone: only a pronoun plus a word for
        "a fragrance" names nothing. Anything more may be a real name."""
        from fragrance_graph.resolve.entities import is_pronoun

        assert is_pronoun(text) is False

    def test_it_keeps_them_out_of_the_review_file(self, conn):
        from fragrance_graph.resolve.entities import batch_rows

        other = add_fragrance(conn, "Creed Aventus")
        for i in range(4):
            seed_edge(conn, i, mention="this stuff", other_id=other,
                      author=f"p{i}", channel=f"UC-{i}")
        assert batch_rows(conn) == []
