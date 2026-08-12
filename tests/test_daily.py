"""The unattended daily loop.

`daily.py` is the only module that runs with nobody watching, which makes
its failure behaviour the thing worth testing. The rules it encodes —
what gets curated without a human, and what a broken dependency does to
the rest of the run — are both here.
"""


import pytest

from fragrance_graph.budget import Budget
from fragrance_graph.daily import AUTO_MIN_COUNT, RunReport, auto_approvable, run
from fragrance_graph.resolve.enrich import Proposal


def _noop(*args, **kwargs):
    return None


def _blocked_host(*args, **kwargs):
    """What an egress policy denying the catalogue actually looks like."""
    raise OSError("403 Forbidden")


def _quota_exhausted(*args, **kwargs):
    raise SystemExit("Catalogue quota exhausted. Re-run to continue.")


def proposal(**overrides) -> Proposal:
    """A proposal that `auto_approvable` accepts, so tests vary one field."""
    row = {
        "mention": "khamrah",
        "count": AUTO_MIN_COUNT,
        "canonical_name": "Khamrah",
        "brand": "Lattafa",
        "confident": True,
        "corpus_mentions": -1,
    }
    row.update(overrides)
    return Proposal(**row)


class TestAutoApprovable:
    """The narrow rule: only rows with no judgement in them."""

    def test_accepts_a_plain_bottle_people_discuss(self):
        assert auto_approvable(proposal()) is True

    @pytest.mark.parametrize("mentions", [0, 1, 5, 29])
    def test_refuses_every_flanker_case(self, mentions):
        """-1 is the only auto-approving value.

        0 means the catalogue offered a bottle whose distinguishing word
        nobody wrote; >0 means a flanker people genuinely discuss and which
        probably deserves its own entry. Both are questions for a person.
        """
        assert auto_approvable(proposal(corpus_mentions=mentions)) is False

    def test_never_overrules_a_person(self):
        for decided in (True, False):
            assert auto_approvable(proposal(approved=decided)) is False

    def test_refuses_a_name_the_catalogue_never_returned(self):
        assert auto_approvable(proposal(canonical_name=None)) is False
        assert auto_approvable(proposal(brand=None)) is False

    def test_refuses_when_the_name_is_not_what_people_wrote(self):
        assert auto_approvable(proposal(confident=False)) is False

    def test_refuses_below_the_commenter_bar(self):
        assert auto_approvable(proposal(count=AUTO_MIN_COUNT - 1)) is False


class TestDegradedDependencies:
    """A broken dependency reports; it does not take the run with it."""

    def test_unreachable_catalogue_does_not_kill_the_run(self, conn, tmp_path,
                                                         monkeypatch):
        """The regression that motivated this file.

        `propose` raises SystemExit only for failures it anticipated. A
        blocked host arrives as an ordinary exception, which used to
        propagate out of `run` — discarding the backfill, the export and
        the report itself, on a loop whose entire job is to report.

        So this asserts two things: the error is recorded, and the steps
        *after* curation still ran.
        """
        import fragrance_graph.resolve.enrich as enrich

        monkeypatch.setenv("FRAGELLA_API_KEY", "present-but-host-is-blocked")
        monkeypatch.setattr(enrich, "propose", _blocked_host)
        monkeypatch.setattr(
            "fragrance_graph.daily.newly_frequent",
            lambda conn, *, limit, min_count: [("khamrah", 4)],
        )
        # The two paid steps either side are not what this test is about.
        monkeypatch.setattr("fragrance_graph.daily._collect", _noop)
        monkeypatch.setattr("fragrance_graph.daily._extract", _noop)

        out_dir = tmp_path / "site"
        report = run(
            conn,
            queries=[],
            budget=Budget(cap_usd=1.0, ledger=tmp_path / "spend.jsonl"),
            out_dir=out_dir,
            dry_run=False,
        )

        assert any("catalogue unreachable" in e for e in report.errors), report.errors
        assert not report.auto_approved
        # The run continued past curation: export happened.
        assert out_dir.exists()

    def test_a_quota_stop_still_reports_its_own_message(self, conn, tmp_path,
                                                        monkeypatch):
        """SystemExit keeps its wording — it is written for the operator."""
        import fragrance_graph.resolve.enrich as enrich

        monkeypatch.setenv("FRAGELLA_API_KEY", "k")
        monkeypatch.setattr(enrich, "propose", _quota_exhausted)
        monkeypatch.setattr(
            "fragrance_graph.daily.newly_frequent",
            lambda conn, *, limit, min_count: [("khamrah", 4)],
        )
        monkeypatch.setattr("fragrance_graph.daily._collect", _noop)
        monkeypatch.setattr("fragrance_graph.daily._extract", _noop)

        report = run(
            conn,
            queries=[],
            budget=Budget(cap_usd=1.0, ledger=tmp_path / "spend.jsonl"),
            out_dir=tmp_path / "site",
            dry_run=False,
        )
        assert any("Catalogue quota exhausted" in e for e in report.errors)

    def test_missing_key_is_named_rather_than_guessed_at(self, conn, tmp_path,
                                                         monkeypatch):
        monkeypatch.delenv("FRAGELLA_API_KEY", raising=False)
        monkeypatch.setattr(
            "fragrance_graph.daily.newly_frequent",
            lambda conn, *, limit, min_count: [("khamrah", 4)],
        )
        monkeypatch.setattr("fragrance_graph.daily._collect", _noop)
        monkeypatch.setattr("fragrance_graph.daily._extract", _noop)

        report = run(
            conn,
            queries=[],
            budget=Budget(cap_usd=1.0, ledger=tmp_path / "spend.jsonl"),
            out_dir=tmp_path / "site",
            dry_run=False,
        )
        assert any("FRAGELLA_API_KEY is not set" in e for e in report.errors)

    def test_report_renders_with_errors_present(self):
        report = RunReport(errors=["catalogue unreachable: OSError('403')"])
        rendered = report.render()
        assert "Problems:" in rendered
        assert "catalogue unreachable" in rendered
        # Worst news first: the operator reads this on a phone.
        assert rendered.index("catalogue unreachable") < rendered.index("Collected")


class TestReportRendering:
    def test_held_rows_explain_themselves(self):
        report = RunReport(
            held_for_review=[("club de nuit", "nobody in the corpus wrote 'sillage'")]
        )
        rendered = report.render()
        assert "Held for you (1)" in rendered
        assert "sillage" in rendered

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


class TestReverseFlanker:
    """A mention more specific than the name it matched must be held.

    `corpus_mentions == -1` only asks what the catalogue name adds, so on
    its own it auto-merges every flanker whose qualifier the commenter
    supplied. This fired on the first live run.
    """

    @staticmethod
    def _p(mention, name, brand):
        return Proposal(
            mention=mention, count=9, canonical_name=name, brand=brand,
            confident=True, corpus_mentions=-1,
        )

    @pytest.mark.parametrize("mention,name,brand", [
        ("Layton Exclusif", "Parfums de Marly Layton", "Parfums de Marly"),
        ("Khamrah Qahwa", "Lattafa Khamrah", "Lattafa"),
        ("Club de Nuit Sillage", "Armaf Club de Nuit", "Armaf"),
        ("Aventus Absolu", "Creed Aventus", "Creed"),
        # The one that actually happened, 2026-08-11.
        ("Club De Nuit EDP", "Armaf Club De Nuit", "Armaf"),
    ])
    def test_reverse_flankers_are_held(self, mention, name, brand):
        assert auto_approvable(self._p(mention, name, brand)) is False

    @pytest.mark.parametrize("mention,name,brand", [
        ("Khamrah", "Lattafa Khamrah", "Lattafa"),
        ("Layton", "Parfums de Marly Layton", "Parfums de Marly"),
        ("Aventus", "Creed Aventus", "Creed"),
        ("oud wonder", "Fragrance World Oud Wonder", "Fragrance World"),
        # Genuinely the same bottle, qualifier present on both sides.
        ("Club de nuit Iconic", "Armaf Club De Nuit Iconic", "Armaf"),
    ])
    def test_exact_bottles_still_auto_approve(self, mention, name, brand):
        assert auto_approvable(self._p(mention, name, brand)) is True

    def test_the_reason_reaches_the_review_file(self):
        from fragrance_graph.resolve.enrich import propose_for

        p = propose_for("Layton Exclusif", 9, [
            {"Name": "Parfums de Marly Layton", "Brand": "Parfums de Marly",
             "Year": "2016"},
        ])
        assert "exclusif" in p.note
        assert "flanker" in p.note


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


class TestFreeResolutionComesBeforePaidLookups:
    """Never pay a catalogue for a name the dictionary already answers."""

    def test_backfill_runs_before_the_catalogue_is_billed(
        self, conn, tmp_path, monkeypatch
    ):
        """The ordering bug that cost real money.

        `_curate` reads *unresolved* mentions, so running it before
        `backfill` offers the catalogue every mention in the comments
        ingested this run — including ones existing aliases already cover.
        On 2026-08-11 that billed $0.05 each to be told about "Khamrah"
        (12 mentions) and "club de nuit" (10), both long since curated;
        both resolved for free the moment backfill ran.

        Asserted as an order of operations rather than a dollar figure,
        because the waste scales with every future run: fresh comments
        mention curated bottles constantly.
        """
        calls: list[str] = []

        def _spy_backfill(c, **kw):
            calls.append("backfill")
            from fragrance_graph.resolve.entities import BackfillStats

            return BackfillStats(0, 0, 0, 0)

        def _spy_curate(*a, **kw):
            calls.append("curate")

        monkeypatch.setattr("fragrance_graph.daily._collect", _noop)
        monkeypatch.setattr("fragrance_graph.daily._extract", _noop)
        monkeypatch.setattr("fragrance_graph.daily._curate", _spy_curate)
        monkeypatch.setattr(
            "fragrance_graph.resolve.entities.backfill", _spy_backfill
        )

        run(
            conn,
            queries=["x"],
            budget=Budget.load(tmp_path / "s.jsonl", today="2026-08-11"),
            out_dir=tmp_path / "site",
        )

        assert calls[0] == "backfill", (
            "the free step must run first; paying to resolve a name the "
            "dictionary already holds is pure waste"
        )
        assert "curate" in calls


class TestQuotaDoesNotDiscardWhatWasPaidFor:
    """A 429 arrives as SystemExit, and that path used to return early.

    `propose` writes what it gathered in a `finally`, so the lookups
    already billed were on disk. Measured 2026-08-12: a run bought 59
    names, hit the catalogue's quota on the 60th, applied none of them,
    and cost $3.00 for nothing.
    """

    def test_the_rows_bought_before_the_quota_stop_are_curated(
        self, conn, tmp_path, monkeypatch
    ):
        import fragrance_graph.resolve.enrich as enrich
        from fragrance_graph.resolve.enrich import Proposal, write_review

        review = tmp_path / "auto-review.json"
        write_review(review, [Proposal(
            mention="liquid brun", count=5,
            canonical_name="Maison Alhambra Liquid Brun",
            brand="Maison Alhambra", confident=True, corpus_mentions=-1,
        )])

        def quota_stop(*args, **kwargs):
            raise SystemExit("Catalogue quota exhausted.")

        monkeypatch.setenv("FRAGELLA_API_KEY", "k")
        monkeypatch.setattr(enrich, "propose", quota_stop)
        monkeypatch.setattr("fragrance_graph.daily.Path", lambda *a: review)
        monkeypatch.setattr(
            "fragrance_graph.daily.newly_frequent",
            lambda conn, *, limit, min_count: [("liquid brun", 5)],
        )
        monkeypatch.setattr("fragrance_graph.daily._collect", _noop)
        monkeypatch.setattr("fragrance_graph.daily._extract", _noop)

        report = run(
            conn, queries=[],
            budget=Budget(cap_usd=5.0, ledger=tmp_path / "spend.jsonl"),
            out_dir=tmp_path / "site", dry_run=False,
        )
        assert any("quota" in e.lower() for e in report.errors)
        assert report.auto_approved, "paid-for rows must still be curated"
        assert conn.execute(
            "SELECT count(*) FROM fragrances"
        ).fetchone()[0] == 1

    def test_nothing_on_disk_still_returns_cleanly(
        self, conn, tmp_path, monkeypatch
    ):
        import fragrance_graph.resolve.enrich as enrich

        monkeypatch.setenv("FRAGELLA_API_KEY", "k")
        monkeypatch.setattr(
            enrich, "propose",
            lambda *a, **k: (_ for _ in ()).throw(SystemExit("rejected key")),
        )
        monkeypatch.setattr("fragrance_graph.daily.Path",
                            lambda *a: tmp_path / "missing.json")
        monkeypatch.setattr(
            "fragrance_graph.daily.newly_frequent",
            lambda conn, *, limit, min_count: [("x", 5)],
        )
        monkeypatch.setattr("fragrance_graph.daily._collect", _noop)
        monkeypatch.setattr("fragrance_graph.daily._extract", _noop)

        report = run(
            conn, queries=[],
            budget=Budget(cap_usd=5.0, ledger=tmp_path / "spend.jsonl"),
            out_dir=tmp_path / "site", dry_run=False,
        )
        assert any("rejected key" in e for e in report.errors)
        assert not report.auto_approved


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
