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
