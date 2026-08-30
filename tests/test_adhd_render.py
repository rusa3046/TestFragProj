"""The gate on a widening run, and the three ways it could fail open.

`scripts/adhd_render.py` turns an ADHD run into a decision record and refuses
to call it reviewed until a person has ruled on every idea. The tests worth
having are not that markdown gets written — they are the ones that say the
gate cannot be passed by accident:

  * an unreviewed row fails, because that is the whole mechanism;
  * one idea appearing in four places in the JSON is one row, because the
    zero-rejection warning reads a denominator and an inflated one makes an
    unread record look thorough;
  * a table in the Decision prose is not a verdict table, because the record
    is edited by hand and a person writing a comparison will reach for pipes.
"""

import json

import pytest

from scripts.adhd_render import (
    UNREVIEWED,
    MalformedRun,
    check,
    main,
    parse_rows,
    render,
)


def idea(idea_id, text, **score):
    out = {"id": idea_id, "frameId": "regulator", "text": text, "depth": 0}
    if score:
        out["score"] = {"novelty": 5, "viability": 5, "fit": 5, "total": 15, **score}
    return out


@pytest.fixture
def run():
    """A RunResult shaped the way `adhd --json` emits one.

    Deliberately has the same idea in `branches`, `shortlist` and
    `nonObviousPick` — that overlap is what `_all_ideas` has to collapse.
    """
    keeper = idea("i1", "typed claims only, never the sentence")
    scored = idea("i1", "typed claims only, never the sentence", novelty=9)
    trap = idea(
        "i2", "cache the reviews verbatim", trap="republication licence does not cover it"
    )
    return {
        "problem": "how should Nordstrom review text be used?",
        "branches": [{"frameId": "regulator", "ideas": [keeper, trap, idea("i3", "ask legal")]}],
        "clusters": [{"label": "remove-the-text plays", "ideaIds": ["i1", "i3"]}],
        "shortlist": [scored],
        "nonObviousPick": scored,
        "traps": [trap],
        "deepened": [
            {"ideaId": "i1", "sketch": "Extract, discard, store the claim.", "childIdeas": []}
        ],
        "provocation": "what if the shopper never sees a quote at all?",
    }


def rendered(run):
    return render(run, slug="nordstrom", question="how?", command="adhd …", commit="abc123")


class TestRender:
    def test_every_idea_starts_unreviewed(self, run):
        rows = parse_rows(rendered(run))
        assert {r.idea_id for r in rows} == {"i1", "i2", "i3"}
        assert all(r.verdict == UNREVIEWED for r in rows)

    def test_an_idea_in_four_places_is_one_row(self, run):
        """The denominator the zero-rejection warning reads must be honest.

        `i1` arrives in `branches`, `shortlist` and `nonObviousPick`. Three
        rows for one idea would let a record with a single real judgement
        report as three ideas weighed.
        """
        rows = parse_rows(rendered(run))
        assert [r.idea_id for r in rows].count("i1") == 1

    def test_the_scored_copy_wins_over_the_bare_one(self, run):
        """Branch copies carry no score; the critic's copies do."""
        record = rendered(run)
        assert "9/5/5" in record  # the novelty=9 copy, not the unscored branch one

    def test_traps_carry_their_reason(self, run):
        assert "republication licence does not cover it" in rendered(run)

    def test_a_newline_in_an_idea_does_not_end_the_table(self):
        run = {"branches": [{"frameId": "f", "ideas": [idea("i1", "one\nline\n\nbroken")]}]}
        rows = parse_rows(rendered(run))
        assert len(rows) == 1
        assert rows[0].idea == "one line broken"

    def test_a_pipe_in_an_idea_does_not_split_the_cell(self):
        run = {"branches": [{"frameId": "f", "ideas": [idea("i1", "claims | not sentences")]}]}
        rows = parse_rows(rendered(run))
        assert len(rows) == 1
        assert rows[0].idea_id == "i1"

    def test_json_that_is_not_a_run_is_refused(self):
        with pytest.raises(MalformedRun):
            render({"error": "rate limited"}, slug="s", question="q", command="", commit="")


class TestCheck:
    def test_unreviewed_fails(self, run):
        result = check(rendered(run))
        assert not result.ok
        assert set(result.unreviewed) == {"i1", "i2", "i3"}

    def test_ruled_on_passes(self, run):
        record = rendered(run).replace(
            f"| i1 | {UNREVIEWED} |", "| i1 | kept |"
        ).replace(
            f"| i2 | {UNREVIEWED} |", "| i2 | rejected |"
        ).replace(
            f"| i3 | {UNREVIEWED} |", "| i3 | parked |"
        )
        # Reasons, so the no-reason warning does not fire.
        record = record.replace("_(TRAP)_ | |", "_(TRAP)_ | licence |").replace(
            "| ask legal | |", "| ask legal | after the eval |"
        )
        result = check(record)
        assert result.ok
        assert (result.kept, result.rejected, result.parked) == (1, 1, 1)
        assert result.warnings == []

    def test_all_kept_warns_but_still_passes(self, run):
        """The zero-rejection signal complains; it does not block.

        Blocking would teach the reader to reject one row to get past the
        gate, which turns an honest signal into a ritual. The gate blocks on
        `unreviewed`, which cannot be faked without reading.
        """
        record = rendered(run)
        for idea_id in ("i1", "i2", "i3"):
            record = record.replace(f"| {idea_id} | {UNREVIEWED} |", f"| {idea_id} | kept |")
        result = check(record)
        assert result.ok
        assert any("nothing was rejected" in w for w in result.warnings)

    def test_a_rejection_with_no_reason_warns(self, run):
        record = rendered(run).replace(f"| i1 | {UNREVIEWED} |", "| i1 | rejected |")
        assert any(w.startswith("i1: rejected with no reason") for w in check(record).warnings)

    def test_an_invented_verdict_fails(self, run):
        record = rendered(run).replace(f"| i1 | {UNREVIEWED} |", "| i1 | maybe |")
        result = check(record)
        assert not result.ok
        assert any("unrecognised verdict" in w for w in result.warnings)

    def test_a_table_outside_the_verdicts_section_is_not_a_verdict(self, run):
        """Records are edited by hand, and prose about a decision grows tables."""
        record = rendered(run).replace(
            "_Not yet decided._",
            "| option | cost |\n| --- | --- |\n| ship it | high |",
        )
        rows = parse_rows(record)
        assert {r.idea_id for r in rows} == {"i1", "i2", "i3"}

    def test_an_empty_record_is_not_reviewed(self):
        assert not check("# nothing here\n").ok


class TestCli:
    def test_render_then_check_round_trips(self, tmp_path, run, capsys):
        raw = tmp_path / "run.json"
        raw.write_text(json.dumps(run))
        out = tmp_path / "decisions" / "nordstrom.md"

        import sys

        stdin = sys.stdin
        sys.stdin = raw.open()
        try:
            argv = ["render", "--slug", "nordstrom", "--question", "how?", "--out", str(out)]
            assert main(argv) == 0
        finally:
            sys.stdin.close()
            sys.stdin = stdin

        assert out.exists()
        assert main(["check", str(out)]) == 1  # unreviewed
        assert "still unreviewed" in capsys.readouterr().out

    def test_render_refuses_non_json_stdin(self, tmp_path, capsys):
        import io
        import sys

        stdin = sys.stdin
        sys.stdin = io.StringIO("adhd: request failed\n")
        try:
            argv = ["render", "--slug", "s", "--question", "q", "--out", str(tmp_path / "s.md")]
            code = main(argv)
        finally:
            sys.stdin = stdin
        assert code == 2
        assert "not JSON" in capsys.readouterr().err
        assert not (tmp_path / "s.md").exists()
