"""Measuring why attribute claims are not queryable.

The tests that matter here are the refusals. Filling in "this" from the
video is a guess, and a guess applied in bulk writes provenance nobody
asserted — so every case where the module declines to guess is pinned,
and the two it declines hardest (a comparison video, a flanker the
catalogue does not have) are the two that were wrong when a sample of the
first version was read by hand.
"""

import json

from fragrance_graph.attributes import (
    DANGLING,
    VERDICTS,
    attach_by_video,
    facts,
    known_singulars,
    names_in,
    normalize_tag,
    subject_of,
    surface_forms,
    vocabulary,
)
from fragrance_graph.extract.llm import write_claims
from fragrance_graph.ingest.store import ingest
from fragrance_graph.models import Claim
from fragrance_graph.resolve.entities import add_fragrance

# --- fixtures ---------------------------------------------------------------


def catalogue(conn):
    """Three bottles, one of them a flanker of another."""
    add_fragrance(conn, "Parfums de Marly Layton", aliases=["Layton"])
    add_fragrance(conn, "Lattafa Khamrah", aliases=["Khamrah"])
    add_fragrance(conn, "Creed Aventus", aliases=["Aventus"])
    return surface_forms(conn)


def video(conn, video_id, *, title=None, query=None):
    conn.execute(
        "INSERT INTO videos (source, video_id, title) VALUES ('youtube', %s, %s)"
        " ON CONFLICT (source, video_id) DO UPDATE SET title = EXCLUDED.title",
        (video_id, title),
    )
    if query:
        conn.execute(
            "INSERT INTO video_discoveries (source, video_id, retrieval_query)"
            " VALUES ('youtube', %s, %s)",
            (video_id, query),
        )
    conn.commit()


def remark(conn, i, *, body, subject, tag, video_id, author="p1",
           channel="UC_a", claim_type="NOTE_DESCRIPTOR"):
    """One person saying one thing about one bottle, on one video."""
    ingest(conn, [{
        "source_id": f"yt_{i:05d}",
        "body": body,
        "permalink": f"https://www.youtube.com/watch?v={video_id}&lc=yt_{i:05d}",
        "created_utc": 1700000000 + i,
        "source_channel": channel,
        "score": 1,
        "raw_json": json.dumps(
            {"videoId": video_id, "authorChannelId": {"value": author}}
        ),
    }], source="youtube")
    comment_id = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"yt_{i:05d}",)
    ).fetchone()[0]
    write_claims(conn, comment_id, body, [
        Claim(
            claim_type=claim_type,
            subject_kind="FRAGRANCE",
            raw_subject_text=subject,
            object_kind="TAG",
            raw_object_text=tag,
            confidence=0.9,
            evidence_span=body,
        )
    ])
    conn.commit()


# --- reading a name out of a title ------------------------------------------


class TestFindingNamesInText:
    def test_it_finds_a_catalogue_name(self, conn):
        forms = catalogue(conn)
        assert list(names_in("Layton review", forms).values()) == [
            "Parfums de Marly Layton"
        ]

    def test_a_longer_name_is_not_double_counted(self, conn):
        """"Parfums de Marly Layton" contains "Layton". It is one bottle."""
        forms = catalogue(conn)
        found = names_in("Parfums de Marly Layton Review", forms)
        assert len(found) == 1

    def test_a_two_letter_alias_is_never_matched_as_a_substring(self, conn):
        """The corpus has "as" as an alias of Angels' Share. Matching it
        would put half of English on one bottle."""
        add_fragrance(conn, "Kilian Angels' Share", aliases=["AS"])
        forms = surface_forms(conn)
        assert names_in("this is as good as it gets", forms) == {}

    def test_it_finds_every_distinct_bottle(self, conn):
        forms = catalogue(conn)
        assert len(names_in("Layton vs Khamrah", forms)) == 2


class TestDecidingWhatAVideoIsAbout:
    def test_a_plain_review_has_one_subject(self, conn):
        forms = catalogue(conn)
        subject = subject_of("v1", "Parfums de Marly Layton Review", None, forms)
        assert subject.usable
        assert subject.canonical_name == "Parfums de Marly Layton"

    def test_it_falls_back_to_the_search_query(self, conn):
        """24 of the videos holding floating claims have no stored title."""
        forms = catalogue(conn)
        subject = subject_of("v1", None, "layton fragrance review", forms)
        assert subject.canonical_name == "Parfums de Marly Layton"

    def test_a_comparison_video_is_refused(self, conn):
        forms = catalogue(conn)
        assert not subject_of("v1", "Layton vs Khamrah", None, forms).usable

    def test_a_clone_video_is_refused_even_naming_one_bottle(self, conn):
        """The single worst case. "Aventus Clone War" names Aventus, and a
        commenter writing "this one lasts forever" means one of the clones
        — never the original in the title. Reading a sample by hand, this
        was most of the errors."""
        forms = catalogue(conn)
        refused = subject_of("v1", "Aventus Clone War | Which is closest?",
                             None, forms)
        assert not refused.usable
        assert "several bottles" in refused.refused

    def test_a_flanker_the_catalogue_lacks_is_refused(self, conn):
        """"Khamrah Waha" is not Khamrah. Attaching its comments to Khamrah
        is the confusion the flanker gate exists to prevent."""
        forms = catalogue(conn)
        refused = subject_of("v1", "NEW Lattafa Khamrah Waha review", None, forms)
        assert not refused.usable
        assert "waha" in refused.refused

    def test_an_ordinary_word_after_the_name_is_harmless(self, conn):
        forms = catalogue(conn)
        assert subject_of("v1", "Layton fragrance review", None, forms).usable

    def test_an_unknown_bottle_is_refused(self, conn):
        forms = catalogue(conn)
        refused = subject_of("v1", "Liquid Brun review", None, forms)
        assert not refused.usable
        assert "no catalogue name" in refused.refused


# --- filling in "this" ------------------------------------------------------


class TestAttachingFloatingClaims:
    def test_a_bare_pronoun_takes_the_videos_subject(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        remark(conn, 1, body="this is very sweet", subject="this",
               tag="sweet", video_id="v1")
        report = attach_by_video(conn)
        assert report.recovered == 1
        assert report.attached[0].canonical_name == "Parfums de Marly Layton"

    def test_a_commenter_who_named_a_bottle_is_left_alone(self, conn):
        """"go for oud for glory, it's good in cold weather" is a claim
        about Oud for Glory, on a video about something else. Guessing over
        the top of a name the commenter typed is how it lands on the wrong
        bottle."""
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        remark(conn, 1, body="oud for glory is the sweet one", tag="sweet",
               subject="oud for glory", video_id="v1")
        report = attach_by_video(conn)
        assert report.recovered == 0
        assert report.refused["the commenter named a bottle themselves"] == 1

    def test_a_comment_naming_another_bottle_is_refused(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        remark(conn, 1, body="I prefer Khamrah, this is too sweet",
               subject="this", tag="sweet", video_id="v1")
        report = attach_by_video(conn)
        assert report.recovered == 0
        assert report.refused["the comment names a different bottle"] == 1

    def test_it_writes_nothing(self, conn):
        """Every repair here is a guess, so applying one is a decision
        somebody makes, not something a measurement does on its way past."""
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        remark(conn, 1, body="this is very sweet", subject="this",
               tag="sweet", video_id="v1")
        attach_by_video(conn)
        still_null = conn.execute(
            "SELECT count(*) FROM claims WHERE subject_frag_id IS NULL"
        ).fetchone()[0]
        assert still_null == 1

    def test_a_resolved_claim_is_not_floating(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        remark(conn, 1, body="Layton is very sweet", subject="Layton",
               tag="sweet", video_id="v1")
        conn.execute("UPDATE claims SET subject_frag_id = 1")
        conn.commit()
        assert attach_by_video(conn).floating == 0


# --- one vocabulary ---------------------------------------------------------


class TestNormalisingATag:
    def test_a_trailing_noun_is_dropped(self):
        assert normalize_tag("coffee note") == "coffee"
        assert normalize_tag("citrus opening") == "citrus"

    def test_a_spelling_variant_lands_on_one_form(self):
        assert normalize_tag("smokey") == normalize_tag("smoky") == "smoky"

    def test_another_language_is_not_a_typo(self):
        """Commenters write in Spanish and Portuguese. "vainilla" is
        vanilla spelled correctly."""
        assert normalize_tag("vainilla") == "vanilla"
        assert normalize_tag("doce") == "sweet"

    def test_emoji_reduce_to_nothing_rather_than_to_a_fact(self):
        assert normalize_tag("🔥🔥🔥") == ""

    def test_a_plural_collapses_only_against_a_known_singular(self):
        """"dates" is the fruit, in nine claims, and "date" is an evening
        out. Blind de-pluralising merges them."""
        plurals = known_singulars(["rose", "spice"])
        assert normalize_tag("roses", plurals=plurals) == "rose"
        assert normalize_tag("dates", plurals=plurals) == "dates"

    def test_a_verdict_is_recognised_as_one(self):
        assert normalize_tag("amazing") in VERDICTS
        assert normalize_tag("sweet") not in VERDICTS

    def test_a_comparative_with_no_baseline_is_recognised(self):
        assert DANGLING.match(normalize_tag("sweeter"))
        assert not DANGLING.match(normalize_tag("sweet"))


class TestTheVocabularyReport:
    def test_it_counts_the_collapse(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        for i, tag in enumerate(["coffee", "coffee note", "coffee notes"]):
            remark(conn, i, body=f"it is {tag}", subject="this", tag=tag,
                   video_id="v1", author=f"p{i}")
        report = vocabulary(conn)
        assert report.distinct_before == 3
        assert report.distinct_after == 1
        assert report.merged[0][0] == "coffee"


# --- what it buys -----------------------------------------------------------


class TestWhetherPeopleAgree:
    def _three_spellings(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        for i, (tag, author, channel) in enumerate([
            ("coffee", "p1", "UC_a"),
            ("coffee note", "p2", "UC_b"),
            ("coffee notes", "p3", "UC_c"),
        ]):
            remark(conn, i, body=f"it is {tag}", subject="this", tag=tag,
                   video_id="v1", author=author, channel=channel)

    def test_spellings_apart_are_three_facts_nobody_agrees_on(self, conn):
        self._three_spellings(conn)
        found = facts(conn, attach=True, collapse=False)
        assert len(found) == 3
        assert not any(f.publishable for f in found)

    def test_one_vocabulary_turns_them_into_one_fact_three_people_share(
        self, conn
    ):
        self._three_spellings(conn)
        found = facts(conn, attach=True, collapse=True)
        assert len(found) == 1
        assert found[0].people == 3
        assert found[0].publishable, "3 people across 3 creators clears the gate"

    def test_without_the_video_subject_there_is_no_fact_at_all(self, conn):
        """The claims exist either way. Unattached, they are about nothing."""
        self._three_spellings(conn)
        assert facts(conn, attach=False, collapse=True) == []

    def test_one_person_saying_it_three_ways_is_still_one_person(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        for i, tag in enumerate(["coffee", "coffee note", "coffee notes"]):
            remark(conn, i, body=f"it is {tag}", subject="this", tag=tag,
                   video_id="v1", author="p1", channel="UC_a")
        found = facts(conn, attach=True, collapse=True)
        assert [f.people for f in found] == [1]


class TestRecordingInferences:
    """Writing an inference must never look like something a person said."""

    def _floating_claim(self, conn):
        catalogue(conn)
        video(conn, "v1", title="Parfums de Marly Layton Review")
        remark(conn, 1, body="this is very sweet", subject="this",
               tag="sweet", video_id="v1")
        return conn.execute("SELECT max(id) FROM claims").fetchone()[0]

    def test_it_writes_a_proposed_row_and_leaves_the_claim_alone(self, conn):
        from fragrance_graph.attributes import record_inferences

        claim_id = self._floating_claim(conn)
        assert record_inferences(conn) == 1

        row = conn.execute(
            "SELECT * FROM claim_attributions WHERE claim_id = %s", (claim_id,)
        ).fetchone()
        assert row["role"] == "subject"
        assert row["method"] == "video_subject"
        assert row["review_status"] == "proposed"
        assert row["confidence"] == 0.95
        assert "v1" in row["evidence"], "the audit trail says what it read"

        assert conn.execute(
            "SELECT subject_frag_id FROM claims WHERE id = %s", (claim_id,)
        ).fetchone()[0] is None, (
            "the column meaning 'the commenter named this' is untouched"
        )

    def test_re_running_writes_nothing_new(self, conn):
        from fragrance_graph.attributes import record_inferences

        self._floating_claim(conn)
        record_inferences(conn)
        record_inferences(conn)
        assert conn.execute(
            "SELECT count(*) FROM claim_attributions"
        ).fetchone()[0] == 1

    def test_it_refuses_to_undo_a_human_decision(self, conn):
        """A re-run after someone rejected a row must not quietly revive it."""
        from fragrance_graph.attributes import record_inferences

        claim_id = self._floating_claim(conn)
        record_inferences(conn)
        conn.execute(
            "UPDATE claim_attributions SET review_status = 'rejected' "
            "WHERE claim_id = %s", (claim_id,)
        )
        conn.commit()
        record_inferences(conn)
        assert conn.execute(
            "SELECT review_status FROM claim_attributions WHERE claim_id = %s",
            (claim_id,),
        ).fetchone()[0] == "rejected"

    def test_a_refused_claim_gets_no_row_at_all(self, conn):
        """The refusals are the safety property; they must not leak through
        as low-confidence rows."""
        from fragrance_graph.attributes import record_inferences

        catalogue(conn)
        video(conn, "v1", title="Layton vs Khamrah")
        remark(conn, 1, body="this is very sweet", subject="this",
               tag="sweet", video_id="v1")
        assert record_inferences(conn) == 0
        assert conn.execute(
            "SELECT count(*) FROM claim_attributions"
        ).fetchone()[0] == 0


class TestDeixisIsNotEnglishOnly:
    """"este perfume" points exactly as hard as "this perfume".

    The corpus has Spanish, Portuguese, Indonesian and Vietnamese
    commenters. With only English deixis recognised, their claims were not
    merely unattached -- the floating classifier counted them as naming
    *fragrances the catalogue lacks*, and "este perfume" (17 claims)
    ranked second on a list of bottles worth acquiring.
    """

    def test_foreign_deixis_points(self):
        from fragrance_graph.attributes import POINTING

        for form in ("este perfume", "esta fragancia", "ce parfum",
                     "questo profumo", "ini", "con này", "o perfume"):
            assert form in POINTING

    def test_it_still_refuses_anything_that_names_a_bottle(self):
        """The caution the English list was built with survives."""
        from fragrance_graph.attributes import POINTING

        for named in ("the extrait", "the original", "oud for glory",
                      "the EdP", "fragrance", "baccarat rouge"):
            assert named not in POINTING

    def test_english_deixis_is_unchanged(self):
        from fragrance_graph.attributes import DEICTIC, POINTING

        assert DEICTIC <= POINTING
        assert "this" in POINTING and "it" in POINTING

    def test_the_attachment_rule_uses_the_wider_set(self):
        import inspect

        from fragrance_graph.attributes import attach_by_video

        source = inspect.getsource(attach_by_video)
        assert "POINTING" in source, (
            "attach_by_video must gate on POINTING, not DEICTIC, or the "
            "foreign forms are recognised nowhere that matters"
        )
