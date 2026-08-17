"""The unattended daily loop.

`daily.py` is the only module that runs with nobody watching, which makes
its failure behaviour the thing worth testing. The rules it encodes —
what gets curated without a human, and what a broken dependency does to
the rest of the run — are both here.
"""


import pytest

from fragrance_graph.budget import Budget
from fragrance_graph.daily import RunReport, run


class TestReportRendering:
    def test_nothing_reports_a_lookup_any_more(self):
        """The catalogue step is gone; the report must not imply one ran."""
        rendered = RunReport().render()
        assert "look" not in rendered.lower()
        assert "Held for you" not in rendered

    def test_dry_run_says_so_before_anything_else(self):
        assert RunReport(dry_run=True).render().startswith("DRY RUN")

    def test_pages_delta_is_silent_when_unchanged(self):
        assert "(no change)" in RunReport(pages_before=4, pages_after=4).render()
        assert "(+2 today)" in RunReport(pages_before=4, pages_after=6).render()


class TestCredentialPlumbing:
    """The keys have to actually arrive, which is where local runs fail."""

    def test_alt_key_is_used_when_the_standard_name_is_reserved(self,
                                                                monkeypatch):
        """A runner that reserves ANTHROPIC_API_KEY must not block extraction."""
        from fragrance_graph.extract.llm import ALT_KEY_ENV, anthropic_api_key

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv(ALT_KEY_ENV, "sk-alt")
        assert anthropic_api_key() == "sk-alt"

    def test_the_standard_name_still_wins(self, monkeypatch):
        from fragrance_graph.extract.llm import ALT_KEY_ENV, anthropic_api_key

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-standard")
        monkeypatch.setenv(ALT_KEY_ENV, "sk-alt")
        assert anthropic_api_key() == "sk-standard"

    def test_neither_set_is_still_a_clean_refusal(self, monkeypatch):
        from fragrance_graph.extract.llm import ALT_KEY_ENV, anthropic_api_key

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv(ALT_KEY_ENV, raising=False)
        assert anthropic_api_key() is None

    def test_daily_loads_dotenv(self, tmp_path, monkeypatch):
        """`daily run` must read .env like every other entrypoint."""
        import fragrance_graph.daily as daily

        called = []
        monkeypatch.setattr(
            "dotenv.load_dotenv", lambda *a, **k: called.append(True) or True
        )
        monkeypatch.setattr(daily, "summary", lambda **k: "", raising=False)
        daily.main(["spend", "--days", "1"])
        assert called, "daily.main did not load .env"


class TestTheReportDoesNotLie:
    """A false alarm is the most expensive kind of wrong in a phone summary."""

    def test_a_run_that_did_nothing_does_not_report_deleting_pages(
        self, conn, tmp_path, monkeypatch
    ):
        """The exhausted-budget early return left pages_after at 0.

        With six pages published that rendered as "Pages 0 (-6 today)": a
        run that touched nothing claiming it destroyed the site.
        """
        monkeypatch.setattr(
            "fragrance_graph.pages.qualifying_pairs", lambda conn, **k: ["p"] * 6
        )
        budget = Budget(cap_usd=1.0, ledger=tmp_path / "spend.jsonl",
                        spent_usd=5.0)
        report = run(
            conn, queries=[], budget=budget,
            out_dir=tmp_path / "site", dry_run=False,
        )
        assert report.pages_before == 6
        assert report.pages_after == 6, "a no-op run must not lose pages"
        rendered = report.render()
        assert "Pages       6" in rendered
        assert "-6" not in rendered

    def test_the_cap_it_names_is_the_cap_it_ran_under(self, conn, tmp_path):
        """render() printed DAILY_CAP_USD, not the --cap actually passed."""
        budget = Budget(cap_usd=1.50, ledger=tmp_path / "spend.jsonl",
                        spent_usd=9.0)
        report = run(
            conn, queries=[], budget=budget,
            out_dir=tmp_path / "site", dry_run=False,
        )
        rendered = report.render()
        assert "$1.50 daily cap" in rendered
        assert "$1.00 daily cap" not in rendered


class TestIngestBudget:
    """The ingest limit caps extraction queued, so it counts new comments."""

    def _run_collect(self, conn, videos, per_video, monkeypatch, *,
                     ingest_limit=400):
        """Drive _collect over fake videos, returning the ids actually read."""
        import fragrance_graph.daily as daily
        from fragrance_graph.ingest import youtube

        read: list[str] = []

        def fake_comments(client, key, video_id, *, limit, quota):
            read.append(video_id)
            return per_video(video_id)[:limit]

        monkeypatch.setattr(youtube, "build_client", lambda: (None, "k"))
        monkeypatch.setattr(
            youtube, "search_video_ids",
            lambda c, k, q, *, limit, quota: list(videos),
        )
        monkeypatch.setattr(youtube, "iter_video_comments", fake_comments)
        monkeypatch.setattr(youtube, "QuotaTracker", lambda: None)

        report = RunReport()
        daily._collect(conn, ["q"], 10, ingest_limit, report)
        return read, report

    def test_already_stored_videos_do_not_consume_the_budget(
        self, conn, tmp_path, monkeypatch
    ):
        """The bug: 18 videos found, budget spent re-reading the first three.

        Video A is fully stored already, so re-reading it must not stop the
        run reaching B — the new creator the publishing gate needs.
        """
        from tests.conftest import make_comment

        stored = [make_comment(i) for i in range(300)]
        fresh = [make_comment(1000 + i) for i in range(5)]

        # Store A's comments up front, so the second read yields 0 new.
        from fragrance_graph.ingest.store import ingest
        ingest(conn, stored, source="youtube")
        conn.commit()

        per_video = {"A": stored, "B": fresh}
        read, report = self._run_collect(
            conn, ["A", "B"], lambda v: per_video[v], monkeypatch,
            ingest_limit=100,
        )

        assert "B" in read, "stopped before reaching the new creator"
        assert report.comments_ingested == 5

    def test_the_limit_still_binds_on_genuinely_new_comments(
        self, conn, tmp_path, monkeypatch
    ):
        """It is still a cap: fresh comments must stop the run."""
        from tests.conftest import make_comment

        a = [make_comment(i) for i in range(60)]
        b = [make_comment(500 + i) for i in range(60)]
        read, report = self._run_collect(
            conn, ["A", "B"], lambda v: {"A": a, "B": b}[v], monkeypatch,
            ingest_limit=50,
        )
        assert report.comments_ingested == 50
        assert read == ["A"], "budget spent, must not fetch the next video"


class TestSeedQueries:
    """Six of the eight original seeds contained "dupe".

    That is a leading question asked of an audience assembled to answer
    it, and 71% of the searches behind the committed corpus have that
    shape. A seed list is the one place a sampling bias can be fixed
    cheaply, and the one place nobody looks unless a test does.
    """

    def test_the_dupe_shape_is_a_minority(self):
        from fragrance_graph.daily import SEED_QUERIES, shape_mix

        mix = shape_mix(SEED_QUERIES)
        assert mix["dupe/clone"] * 2 < sum(mix.values())

    def test_it_still_asks_the_question_that_works(self):
        """Broadening is not abandoning: dupe searches built this corpus."""
        from fragrance_graph.daily import SEED_QUERIES, shape_mix

        assert shape_mix(SEED_QUERIES)["dupe/clone"] >= 1

    def test_several_shapes_are_represented(self):
        from fragrance_graph.daily import SEED_QUERIES, shape_mix

        assert len(shape_mix(SEED_QUERIES)) >= 5

    @pytest.mark.parametrize("query,shape", [
        ("creed aventus vs", "head to head"),
        ("is parfums de marly layton worth it", "worth it"),
        ("smells like baccarat rouge 540", "smells like"),
        ("better than dior sauvage", "better than"),
        ("alternative to tom ford oud wood", "alternative to"),
        ("aventus clone", "dupe/clone"),
        ("khamrah review", "review"),
        ("parfums de marly layton", "bare name"),
    ])
    def test_shapes_are_read_from_the_phrase(self, query, shape):
        from fragrance_graph.daily import query_shape

        assert query_shape(query) == shape

    def test_the_report_shows_the_plan_beside_what_happened(self, conn):
        """Changing the seeds does nothing until an ingest runs, and a
        report that showed only the new list would hide that."""
        from fragrance_graph.daily import render_seed_diversity

        out = render_seed_diversity(conn)
        assert "seeds now" in out and "corpus so far" in out
        assert "No retrieval records yet" in out

    def test_the_scheduled_run_uses_them(self):
        """The workflow inlined its own list, so the seeds could be
        broadened in code and the schedule would go on asking the old
        question twice a week."""
        from pathlib import Path

        workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
        run_step = workflow.split("Run the loop", 1)[1].split("- name:", 1)[0]
        commands = [
            line for line in run_step.splitlines()
            if not line.strip().startswith("#")
        ]
        assert "--queries" not in "\n".join(commands), "the loop must take the seeds"


class TestTheRunSummary:
    """The notification has to answer "how big is this thing now".

    Deltas say whether the loop did anything; totals say what it has.
    Someone reading a summary on a phone is asking the second, and a
    report with only the first sends them to a laptop to find out.
    """

    def test_it_reports_the_corpus_totals(self, conn):
        from fragrance_graph.daily import RunReport, _snapshot
        from fragrance_graph.resolve.entities import add_fragrance

        add_fragrance(conn, "Creed Aventus")
        report = RunReport()
        _snapshot(conn, report)

        assert report.total_fragrances == 1
        assert report.total_comments == 0
        assert "1 fragrances" in report.render()

    def test_totals_are_absent_rather_than_zero_on_an_empty_corpus(self):
        """A line reading "0 comments, 0 claims" on a dry run is noise."""
        from fragrance_graph.daily import RunReport

        assert "Corpus" not in RunReport().render()

    def test_the_workflow_posts_every_run(self):
        """The decision issue is deliberately silent on a quiet run. The
        log is the opposite, and they must not be the same step."""
        from pathlib import Path

        workflow = Path(".github/workflows/daily.yml").read_text(encoding="utf-8")
        summary = workflow.split("Post the run summary", 1)[1].split("- name:", 1)[0]
        assert "if: always()" in summary
        assert "loop-log" in summary
        assert "Loop needs a decision" not in summary


def _workflow() -> str:
    from pathlib import Path

    return Path(".github/workflows/daily.yml").read_text(encoding="utf-8")


def _step(name: str) -> str:
    return _workflow().split(name, 1)[1].split("- name:", 1)[0]


class TestCIInstallsWhatTheSuiteImports:
    """fastapi lives in the [api] extra on purpose — a core install must
    not grow it. But the suite contains the API tests, so the TEST
    environment needs the extra. The first M1 CI run died at collection
    (ModuleNotFoundError: fastapi) because ci.yml synced only dev.

    tests/test_api.py now importorskips fastapi so a core-only install
    skips instead of killing collection — which means a CI that quietly
    dropped the extra would SKIP 60+ API tests and stay green. This test
    lives here, in a module with no fastapi import, precisely so that
    regression cannot skip its own detector."""

    def test_ci_syncs_the_api_extra(self):
        from pathlib import Path

        ci = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
        assert "--extra api" in ci, (
            "ci.yml no longer installs the [api] extra; the API tests "
            "will silently skip and their coverage is gone"
        )


class TestThePublishedSiteIsBuiltFromACompleteDatabase:
    """`corpus import` restores everything the corpus *stores*. Two tables
    are computed from it instead, and the scheduled rebuild skipped both.

    Nothing errored. The site built, deployed, and served answers — just
    fewer of them, because `claim_attributions` was empty and every row in
    `evidence_embeddings` pointed at a claim id the import had deleted.
    Measured on this corpus: two of the eight curated questions rendered
    "no candidates met the evidence bar" for no reason but this.

    That is the failure worth a test. A deploy that breaks gets fixed; a
    deploy that quietly publishes less looks like a corpus with less to
    say.
    """

    def test_the_rebuild_recomputes_what_an_import_cannot_restore(self):
        from fragrance_graph.corpus import DERIVED_TABLES

        rebuild = _step("Rebuild the database from the committed corpus")
        commands = "\n".join(
            line for line in rebuild.splitlines()
            if not line.strip().startswith("#")
        )
        for _table, (command, _live_sql) in DERIVED_TABLES.items():
            assert command in commands, (
                f"the scheduled rebuild never runs `{command}`, so the "
                "published site is missing answers it has the evidence for"
            )

    def test_a_stale_rebuild_stops_the_run_rather_than_publishing(self):
        """The same notice the import CLI prints, made fatal. A scheduled
        run has nobody to read a warning."""
        check = _step("Check the rebuild actually landed")
        assert "rebuild_notice" in check and "derived_state" in check
        assert "sys.exit(notice)" in check


class TestPublishingDoesNotRequireSpending:
    """Publishing and collecting used to be one button, so shipping a code
    change to the live site meant running the paid loop. Pages are a pure
    derivation from the committed corpus; the two are now separable.
    """

    def test_every_paid_or_writing_step_is_gated(self):
        for name in ("Run the loop", "Export the corpus", "Commit anything new"):
            assert "env.COLLECT == 'true'" in _step(name), (
                f"{name!r} would run on a publish-only dispatch"
            )

    def test_building_and_publishing_are_not_gated(self):
        """The whole point of the flag is that these still happen."""
        for name in ("Build the pages", "Package the pages"):
            assert "env.COLLECT" not in _step(name)

    def test_a_scheduled_run_still_collects(self):
        """A schedule carries no inputs. If the expression got that wrong
        the unattended loop would quietly stop doing the only thing it
        exists to do, and the site would keep deploying and look fine."""
        workflow = _workflow()
        assert (
            "COLLECT: ${{ github.event_name != 'workflow_dispatch' "
            "|| inputs.collect }}"
        ) in workflow
