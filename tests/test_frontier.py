"""Bounded per-bottle investigation.

The interesting tests here are not that the loop runs. They are the four
places it is supposed to *refuse* to spend: a bottle only one creator
covers, a search that returns nothing, a comment ceiling, and a spend
ceiling — plus the early stop that makes the whole thing cheaper than the
schedule it replaces.

Nothing here reaches the network. `FakeSearch` returns queued payloads in
the shape the YouTube API returns them, and extraction is a callable so
the stopping logic can be exercised without an Anthropic key.
"""

import json

import pytest

from fragrance_graph.extract.llm import write_claims
from fragrance_graph.frontier import (
    Ceiling,
    RunLog,
    Trial,
    VideoHit,
    bucket_table,
    candidates,
    enrich_one,
    one_per_creator,
    pairs,
    probe,
    publishable_count,
    query_for,
    recorded_hits,
    render_bucket_table,
    run_probe,
    search_with_creators,
    store_hits,
    summarize,
    unsearchable,
)
from fragrance_graph.ingest.store import ingest
from fragrance_graph.ingest.youtube import QuotaTracker
from fragrance_graph.models import Claim

# --- fixtures ---------------------------------------------------------------


def comment(i, *, channel, author, video, body):
    """A YouTube-shaped comment row, with the fields the counts rest on."""
    return {
        "source_id": f"yt_{i:05d}",
        "body": body,
        "permalink": f"https://www.youtube.com/watch?v={video}&lc=yt_{i:05d}",
        "created_utc": 1700000000 + i,
        "source_channel": channel,
        "score": 1,
        "raw_json": json.dumps(
            {"videoId": video, "authorChannelId": {"value": author}}
        ),
    }


def say(conn, i, *, subject, obj, channel, author, video="v1",
        claim_type="SIMILAR_TO"):
    """One person, on one creator's video, comparing two bottles."""
    body = f"{subject} smells like {obj}"
    ingest(conn, [comment(i, channel=channel, author=author, video=video, body=body)],
           source="youtube")
    comment_id = conn.execute(
        "SELECT id FROM comments WHERE source_id = %s", (f"yt_{i:05d}",)
    ).fetchone()[0]
    write_claims(conn, comment_id, body, [
        Claim(
            claim_type=claim_type,
            subject_kind="FRAGRANCE",
            raw_subject_text=subject,
            object_kind="FRAGRANCE",
            raw_object_text=obj,
            confidence=0.9,
            evidence_span=body,
        )
    ])
    conn.commit()


def seeded(conn):
    """Four mentions of `oajan`, on two creators. Deliberately in the 4-9
    band and deliberately feasible — the cohort the experiment targets."""
    for i, (channel, author) in enumerate(
        [("UC_a", "p1"), ("UC_a", "p2"), ("UC_b", "p3"), ("UC_b", "p4")]
    ):
        say(conn, i, subject="oajan", obj=f"other {i}", channel=channel,
            author=author, video=f"v{i}")
    return conn


class FakeSearch:
    """Returns queued JSON payloads, recording what was asked for."""

    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.queries = []

    def get(self, url, params=None):
        self.queries.append((params or {}).get("q"))
        payload = self._payloads.pop(0)

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return payload

        return R()


def search_payload(*pairs_of):
    """`(video_id, channel_id)` pairs in the API's response shape."""
    return {
        "items": [
            {
                "id": {"videoId": video},
                "snippet": {"channelId": channel, "channelTitle": f"{channel} TV"},
            }
            for video, channel in pairs_of
        ]
    }


# --- what to investigate ----------------------------------------------------


class TestChoosingCandidates:
    def test_it_finds_the_band_and_counts_creators(self, conn):
        seeded(conn)
        found = candidates(conn, low=4, high=9)
        oajan = next(c for c in found if c.text == "oajan")
        assert oajan.claims == 4
        assert oajan.creators == 2
        assert oajan.people == 4
        assert not oajan.blocked_in_corpus

    def test_a_bottle_on_one_creator_is_flagged(self, conn):
        for i, author in enumerate(["p1", "p2", "p3", "p4"]):
            say(conn, i, subject="dukhan", obj=f"other {i}",
                channel="UC_only", author=author, video=f"v{i}")
        oajan = next(c for c in candidates(conn) if c.text == "dukhan")
        assert oajan.creators == 1
        assert oajan.blocked_in_corpus, (
            "MIN_SOURCES needs two creators; this one can never publish"
        )

    def test_the_band_excludes_the_well_covered_and_the_barely_mentioned(self, conn):
        seeded(conn)
        say(conn, 90, subject="once", obj="thing", channel="UC_a", author="p9")
        texts = {c.text for c in candidates(conn, low=4, high=9)}
        assert "oajan" in texts
        assert "once" not in texts, "one mention is below the band"

    def test_note_descriptors_are_not_evidence_of_demand(self, conn):
        """A bottle only ever described, never compared, cannot form an edge."""
        seeded(conn)
        for i in range(4):
            say(conn, 200 + i, subject="oajan", obj="vanilla", channel="UC_a",
                author=f"n{i}", claim_type="SIMILAR_TO")
        before = next(c for c in candidates(conn) if c.text == "oajan").claims
        assert before == 8
        # A NOTE_DESCRIPTOR claim is not in COMPARISONS, so it must not count.
        body = "oajan is sweet"
        ingest(conn, [comment(300, channel="UC_c", author="p99", video="v9",
                              body=body)], source="youtube")
        cid = conn.execute(
            "SELECT id FROM comments WHERE source_id = 'yt_00300'"
        ).fetchone()[0]
        write_claims(conn, cid, body, [
            Claim(claim_type="NOTE_DESCRIPTOR", subject_kind="FRAGRANCE",
                  raw_subject_text="oajan", object_kind="TAG",
                  raw_object_text="sweet", confidence=0.9, evidence_span=body)
        ])
        conn.commit()
        after = next(c for c in candidates(conn) if c.text == "oajan").claims
        assert after == before


# --- one search, both answers ----------------------------------------------


class TestTheProbe:
    def test_a_search_returns_creators_and_costs_one_search(self, conn):
        quota = QuotaTracker()
        http = FakeSearch([search_payload(("v1", "UC_x"), ("v2", "UC_y"))])
        hits = search_with_creators(http, "KEY", "q", limit=25, quota=quota)
        assert [h.channel_id for h in hits] == ["UC_x", "UC_y"]
        assert quota.units == 100, "search.list is 100 units, once"

    def test_items_without_a_channel_are_dropped(self):
        quota = QuotaTracker()
        payload = search_payload(("v1", "UC_x"))
        payload["items"].append({"id": {"videoId": "v2"}, "snippet": {}})
        hits = search_with_creators(
            FakeSearch([payload]), "KEY", "q", limit=25, quota=quota
        )
        assert [h.video_id for h in hits] == ["v1"]

    def test_the_query_does_not_ask_for_dupes(self):
        """The corpus is already dupe-shaped; the query must not deepen it."""
        assert "dupe" not in query_for("oajan")
        assert "oajan" in query_for("oajan")

    def test_two_creators_is_enrichable(self, conn):
        seeded(conn)
        candidate = next(c for c in candidates(conn) if c.text == "oajan")
        http = FakeSearch([search_payload(("v9", "UC_new"), ("v8", "UC_b"))])
        result, ordered = probe(
            http, "KEY", candidate, quota=QuotaTracker(),
            known=frozenset({"UC_a", "UC_b"}),
        )
        assert result.verdict == "enrichable"
        assert result.feasible
        assert ordered[0].channel_id == "UC_new", "unseen creators go first"

    def test_no_results_is_no_coverage(self, conn):
        seeded(conn)
        candidate = next(c for c in candidates(conn) if c.text == "oajan")
        result, ordered = probe(
            FakeSearch([{"items": []}]), "KEY", candidate, quota=QuotaTracker()
        )
        assert result.verdict == "no-coverage"
        assert ordered == []

    def test_one_creator_everywhere_is_refused(self, conn):
        """The corpus has one creator and the search finds only that one."""
        for i, author in enumerate(["p1", "p2", "p3", "p4"]):
            say(conn, i, subject="dukhan", obj=f"o{i}", channel="UC_only",
                author=author, video=f"v{i}")
        candidate = next(c for c in candidates(conn) if c.text == "dukhan")
        result, _ = probe(
            FakeSearch([search_payload(("v7", "UC_only"), ("v8", "UC_only"))]),
            "KEY", candidate, quota=QuotaTracker(), known=frozenset({"UC_only"}),
        )
        assert result.verdict == "one-creator"
        assert not result.feasible

    def test_feasibility_is_necessary_not_sufficient(self, conn):
        """Stated in the docstring, pinned here: a bottle can pass the
        per-bottle test while every individual pair remains stuck."""
        seeded(conn)
        candidate = next(c for c in candidates(conn) if c.text == "oajan")
        assert not candidate.blocked_in_corpus
        assert publishable_count(pairs(conn), "oajan") == 0, (
            "two creators, but each pair has one person on one creator"
        )


class TestOnePerCreator:
    def test_a_second_video_from_one_creator_is_dropped(self):
        hits = [
            VideoHit("v1", "UC_a", "A"),
            VideoHit("v2", "UC_a", "A"),
            VideoHit("v3", "UC_b", "B"),
        ]
        assert [h.video_id for h in one_per_creator(hits)] == ["v1", "v3"]

    def test_unseen_creators_come_first(self):
        hits = [VideoHit("v1", "UC_known", "K"), VideoHit("v2", "UC_new", "N")]
        ordered = one_per_creator(hits, known=frozenset({"UC_known"}))
        assert [h.channel_id for h in ordered] == ["UC_new", "UC_known"]


# --- the probe persists what enrichment needs -------------------------------


class TestProbeAndEnrichShareOneSearch:
    """The affordability argument in the module docstring, tested.

    If enrichment had to search again, each bottle would cost 200 quota
    units instead of 100 and the cohort would not fit in a day.
    """

    def test_recorded_hits_round_trip_with_their_creators(self, conn):
        hits = [VideoHit("v1", "UC_a", "A TV"), VideoHit("v2", "UC_b", "B TV")]
        store_hits(conn, hits, "oajan fragrance review", run="r1")
        back = recorded_hits(conn, "oajan fragrance review", run="r1")
        assert {(h.video_id, h.channel_id) for h in back} == {
            ("v1", "UC_a"), ("v2", "UC_b")
        }

    def test_a_different_run_does_not_see_them(self, conn):
        store_hits(conn, [VideoHit("v1", "UC_a", "A")], "q", run="r1")
        assert recorded_hits(conn, "q", run="r2") == []

    def test_enrich_refuses_when_no_probe_ran(self, conn):
        seeded(conn)
        candidate = next(c for c in candidates(conn) if c.text == "oajan")
        trial = Trial(text="oajan", claims_before=4, creators_before=2,
                      started_at="now")
        enrich_one(
            conn, FakeSearch([]), "KEY", candidate, quota=QuotaTracker(),
            ceiling=Ceiling(), run="never-probed",
            extract_fn=lambda conn, *, limit: pytest.fail("must not extract"),
            trial=trial,
        )
        assert trial.stop_reason == "no-coverage"


class TestProbeSpendsNoMoney:
    def test_a_full_probe_run_writes_a_row_per_bottle(self, conn, tmp_path):
        seeded(conn)
        cohort = [c for c in candidates(conn) if c.text == "oajan"]
        run_log = RunLog(path=tmp_path / "run.jsonl")
        trials = run_probe(
            conn, FakeSearch([search_payload(("v9", "UC_new"))]), "KEY", cohort,
            quota=QuotaTracker(), run="r1", log_to=run_log,
        )
        assert len(trials) == 1
        assert trials[0].usd == 0.0, "probing costs quota, never money"
        assert trials[0].quota_units == 100
        written = [json.loads(line) for line in
                   (tmp_path / "run.jsonl").read_text().splitlines()]
        assert written[0]["text"] == "oajan"
        assert written[0]["kind"] == "trial"

    def test_it_stops_rather_than_overspending_quota(self, conn, tmp_path):
        seeded(conn)
        cohort = [c for c in candidates(conn) if c.text == "oajan"]
        quota = QuotaTracker()
        quota.charge(9_999)
        trials = run_probe(
            conn, FakeSearch([]), "KEY", cohort, quota=quota, run="r1",
            log_to=RunLog(path=tmp_path / "run.jsonl"),
        )
        assert trials[0].stop_reason == "quota-exhausted"
        assert trials[0].searches == 0


# --- how far to go ----------------------------------------------------------


class FakeComments:
    """A search client that also serves commentThreads payloads."""

    def __init__(self, per_video):
        self.per_video = per_video
        self.videos_fetched = []

    def get(self, url, params=None):
        params = params or {}
        video = params.get("videoId")
        self.videos_fetched.append(video)
        items = [
            {
                "snippet": {
                    "topLevelComment": {
                        "id": f"{video}_c{i}",
                        "snippet": {
                            "textOriginal": body,
                            "authorChannelId": {"value": author},
                            "channelId": channel,
                            "publishedAt": "2026-08-01T12:00:00Z",
                            "likeCount": 1,
                        },
                    }
                }
            }
            for i, (body, author, channel) in enumerate(self.per_video.get(video, []))
        ]

        class R:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return {"items": items}

        return R()


class TestTheCeiling:
    """Each field is a stop the run cannot talk itself past."""

    def _candidate(self, conn):
        seeded(conn)
        return next(c for c in candidates(conn) if c.text == "oajan")

    def test_it_stops_the_moment_a_pair_publishes(self, conn):
        """The early stop. Three people on two creators already agree
        after the first video, so the second is not bought."""
        candidate = self._candidate(conn)
        store_hits(conn, [VideoHit("vA", "UC_x", "X"), VideoHit("vB", "UC_y", "Y")],
                   query_for("oajan"), run="r1")
        client = FakeComments({"vA": [], "vB": []})

        def extract_fn(conn, *, limit):
            # Stand in for the extractor: the first video's comments carry
            # the third person on a second creator.
            for i, (channel, author) in enumerate(
                [("UC_x", "n1"), ("UC_x", "n2"), ("UC_y", "n3")]
            ):
                say(conn, 500 + i, subject="oajan", obj="rifaaqat",
                    channel=channel, author=author, video="vA")
            return 0.01

        trial = Trial(text="oajan", claims_before=4, creators_before=2,
                      started_at="now")
        enrich_one(conn, client, "KEY", candidate, quota=QuotaTracker(),
                   ceiling=Ceiling(), run="r1", extract_fn=extract_fn, trial=trial)

        assert trial.stop_reason == "target-met"
        assert trial.videos_processed == 1, "the second creator was never bought"
        assert trial.newly_publishable == 1

    def test_it_stops_at_the_comment_ceiling(self, conn):
        # `w` ids, because `seeded` already ingested v0-v3 and a video
        # already in the corpus is skipped rather than refetched.
        candidate = self._candidate(conn)
        store_hits(conn, [VideoHit(f"w{i}", f"UC_{i}", "T") for i in range(4)],
                   query_for("oajan"), run="r1")
        client = FakeComments({
            f"w{i}": [(f"body {j}", f"a{i}{j}", f"UC_{i}") for j in range(3)]
            for i in range(4)
        })
        trial = Trial(text="oajan", claims_before=4, creators_before=2,
                      started_at="now")
        enrich_one(conn, client, "KEY", candidate, quota=QuotaTracker(),
                   ceiling=Ceiling(max_comments=4), run="r1",
                   extract_fn=lambda conn, *, limit: 0.0, trial=trial)
        assert trial.stop_reason == "comment-ceiling"
        assert trial.comments_seen >= 4

    def test_it_stops_at_the_spend_ceiling(self, conn):
        candidate = self._candidate(conn)
        store_hits(conn, [VideoHit(f"w{i}", f"UC_{i}", "T") for i in range(4)],
                   query_for("oajan"), run="r1")
        client = FakeComments({f"w{i}": [("b", f"a{i}", f"UC_{i}")] for i in range(4)})
        trial = Trial(text="oajan", claims_before=4, creators_before=2,
                      started_at="now")
        enrich_one(conn, client, "KEY", candidate, quota=QuotaTracker(),
                   ceiling=Ceiling(max_usd=0.02), run="r1",
                   extract_fn=lambda conn, *, limit: 0.03, trial=trial)
        assert trial.stop_reason == "spend-ceiling"

    def test_it_never_buys_more_creators_than_the_ceiling(self, conn):
        candidate = self._candidate(conn)
        store_hits(conn, [VideoHit(f"w{i}", f"UC_{i}", "T") for i in range(9)],
                   query_for("oajan"), run="r1")
        client = FakeComments({f"w{i}": [("b", f"a{i}", f"UC_{i}")] for i in range(9)})
        trial = Trial(text="oajan", claims_before=4, creators_before=2,
                      started_at="now")
        enrich_one(conn, client, "KEY", candidate, quota=QuotaTracker(),
                   ceiling=Ceiling(max_creators=2), run="r1",
                   extract_fn=lambda conn, *, limit: 0.0, trial=trial)
        assert trial.videos_processed == 2, "nine were available"
        assert trial.stop_reason == "creators-exhausted"

    def test_a_one_creator_bottle_never_reaches_the_extractor(self, conn):
        """The whole point of the feasibility filter: no LLM spend on a
        bottle that cannot satisfy MIN_SOURCES at any price."""
        for i, author in enumerate(["p1", "p2", "p3", "p4"]):
            say(conn, i, subject="dukhan", obj=f"o{i}", channel="UC_only",
                author=author, video=f"v{i}")
        candidate = next(c for c in candidates(conn) if c.text == "dukhan")
        store_hits(conn, [VideoHit("v7", "UC_only", "Only")],
                   query_for("dukhan"), run="r1")
        trial = Trial(text="dukhan", claims_before=4, creators_before=1,
                      started_at="now")
        enrich_one(
            conn, FakeComments({}), "KEY", candidate, quota=QuotaTracker(),
            ceiling=Ceiling(), run="r1",
            extract_fn=lambda conn, *, limit: pytest.fail("must not extract"),
            trial=trial,
        )
        assert trial.stop_reason == "one-creator"
        assert trial.videos_processed == 0

    def test_a_video_already_in_the_corpus_is_not_refetched(self, conn):
        candidate = self._candidate(conn)
        # v0 already carries comments from `seeded`.
        store_hits(conn, [VideoHit("v0", "UC_a", "A"), VideoHit("vNew", "UC_z", "Z")],
                   query_for("oajan"), run="r1")
        client = FakeComments({"vNew": [("b", "a1", "UC_z")]})
        trial = Trial(text="oajan", claims_before=4, creators_before=2,
                      started_at="now")
        enrich_one(conn, client, "KEY", candidate, quota=QuotaTracker(),
                   ceiling=Ceiling(), run="r1",
                   extract_fn=lambda conn, *, limit: 0.0, trial=trial)
        assert "v0" not in client.videos_fetched


# --- the measurement --------------------------------------------------------


class TestTheConversionTable:
    def test_a_pair_is_bucketed_by_its_weaker_end(self, conn):
        """A Layton comparison is only as publishable as the bottle Layton
        is compared to — which is why the minimum, not the maximum, buckets
        the pair."""
        for i in range(12):
            say(conn, i, subject="layton", obj=f"filler {i}", channel="UC_a",
                author=f"p{i}", video=f"v{i}")
        say(conn, 90, subject="layton", obj="obscure", channel="UC_b", author="p90")
        table = {row["bucket"]: row for row in bucket_table(conn)}
        assert table["1"]["pairs"] >= 1, "the obscure end puts the pair in bucket 1"

    def test_a_pair_publishes_only_with_three_people_on_two_creators(self, conn):
        for i, (channel, author) in enumerate(
            [("UC_a", "p1"), ("UC_a", "p2"), ("UC_b", "p3")]
        ):
            say(conn, i, subject="khamrah", obj="angel share", channel=channel,
                author=author, video=f"v{i}")
        found = pairs(conn)[("angel share", "khamrah")]
        assert found.people == 3 and found.creators == 2
        assert found.publishable

    def test_three_people_on_one_creator_does_not_publish(self, conn):
        for i, author in enumerate(["p1", "p2", "p3"]):
            say(conn, i, subject="khamrah", obj="angel share", channel="UC_a",
                author=author, video=f"v{i}")
        assert not pairs(conn)[("angel share", "khamrah")].publishable

    def test_the_same_person_twice_is_one_person(self, conn):
        for i in range(3):
            say(conn, i, subject="khamrah", obj="angel share",
                channel=f"UC_{i}", author="same-person", video=f"v{i}")
        found = pairs(conn)[("angel share", "khamrah")]
        assert found.people == 1, "three comments, one human"
        assert not found.publishable

    def test_rendering_is_stable(self, conn):
        seeded(conn)
        text = render_bucket_table(bucket_table(conn))
        assert "claims about it" in text
        assert text == render_bucket_table(bucket_table(conn))


class TestTheSummary:
    def test_it_reports_cost_per_newly_publishable_bottle(self):
        trials = [
            Trial(text="a", claims_before=5, creators_before=2, started_at="now",
                  publishable_before=0, publishable_after=1, usd=0.04,
                  stop_reason="target-met"),
            Trial(text="b", claims_before=5, creators_before=1, started_at="now",
                  stop_reason="one-creator"),
        ]
        out = summarize(trials)
        assert "2 bottles investigated" in out
        assert "1 produced a newly publishable pair" in out
        assert "$0.0400 per newly publishable bottle" in out
        assert "one-creator x1" in out

    def test_it_survives_a_run_that_published_nothing(self):
        """The result the experiment might legitimately return."""
        out = summarize([
            Trial(text="a", claims_before=5, creators_before=2, started_at="now",
                  usd=0.05, stop_reason="creators-exhausted")
        ])
        assert "0 produced a newly publishable pair" in out
        assert "per newly publishable" not in out, "no division by zero"


def test_the_module_never_writes_to_the_corpus_directory():
    """Trials are measurements of how the corpus was built, not corpus."""
    from fragrance_graph.frontier import EXPERIMENTS_DIR, run_path

    assert "corpus" not in str(EXPERIMENTS_DIR)
    assert str(run_path("x")).endswith("frontier-x.jsonl")


class TestWhatCannotBeSearched:
    """The mention column is full of strings that are not bottle names.

    Curation tolerates them because a human reads the queue and skips
    them. A crawler cannot: a search costs 100 quota units whether or not
    the query names anything, and on the committed corpus 14 of the 54
    candidates are unsearchable — 1,400 units, a seventh of a day.
    """

    @pytest.mark.parametrize("text,reason", [
        ("this one", "pronoun"),
        ("this fragrance", "pronoun"),
        ("clones", "category-word"),
        ("dupes", "category-word"),
        ("the og", "category-word"),
        ("original", "category-word"),
        ("limited edition", "category-word"),
        ("the extrait", "category-word"),
        ("the cologne", "category-word"),
        ("exclusif", "category-word"),
        ("both", "category-word"),
    ])
    def test_it_names_the_reason(self, text, reason):
        assert unsearchable(text) == reason

    @pytest.mark.parametrize("text", [
        "pdm layton",
        "aventus cologne",
        "sauvage elixir",
        "delina exclusif",
        "club de nuit intense man",
        "khamrah qahwa",
        "540",
        "virgin island water",
    ])
    def test_real_bottles_survive(self, text):
        """The flanker vocabulary is bottle vocabulary too.

        `aventus cologne` and `sauvage elixir` are bottles whose second
        word is a concentration. Testing *every* word rather than any word
        is what keeps them — the same trade `commerce/feeds.py` makes when
        it refuses to strip `Elixir` from a feed name.
        """
        assert unsearchable(text) == ""

    def test_a_bare_brand_is_refused_but_its_bottles_are_not(self):
        brands = frozenset({"tom ford", "al haramain"})
        assert unsearchable("tom ford", brands) == "brand-only"
        assert unsearchable("al haramain", brands) == "brand-only"
        assert unsearchable("tom ford oud wood", brands) == ""
        assert unsearchable("al haramain detour noir", brands) == ""

    def test_brands_come_from_what_has_been_curated(self, conn):
        from fragrance_graph.frontier import known_brands
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Tom Ford Oud Wood", brand="Tom Ford")
        conn.commit()
        assert "tom ford" in known_brands(conn)

    def test_the_cohort_reports_it_rather_than_hiding_it(self, conn):
        """An unsearchable candidate is skipped, not silently dropped —
        14 vanishing rows is how a filter's mistakes go unnoticed."""
        for i, author in enumerate(["p1", "p2", "p3", "p4"]):
            say(conn, i, subject="clones", obj=f"o{i}", channel=f"UC_{i % 2}",
                author=author, video=f"v{i}")
        found = next(c for c in candidates(conn) if c.text == "clones")
        assert found.unsearchable == "category-word"
        assert found.skip_reason == "category-word"
        assert not found.viable

    def test_one_creator_is_reported_as_its_own_reason(self, conn):
        """Two different problems: unsearchable is 'we cannot look', and
        one-creator is 'we looked and it can never satisfy the gate'."""
        for i, author in enumerate(["p1", "p2", "p3", "p4"]):
            say(conn, i, subject="perseus", obj=f"o{i}", channel="UC_one",
                author=author, video=f"v{i}")
        found = next(c for c in candidates(conn) if c.text == "perseus")
        assert found.unsearchable == ""
        assert found.skip_reason == "one-creator"
        assert not found.viable
