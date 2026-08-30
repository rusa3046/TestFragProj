"""Turn an ADHD run into a decision record, and refuse to call it reviewed.

`scripts/adhd-widen.sh` runs the `adhd` CLI at a named decision and pipes the
`--json` `RunResult` through here. What comes out is a markdown file under
`docs/decisions/` with every surfaced idea in a table and a **verdict column
that starts empty**.

That empty column is the entire point of this module.

## Why a widening tool needs a gate at all

The Codex lanes have one: a finding is an argument, and `/codex-checkpoint`
makes a person open the cited file before anything is accepted. ADHD needs
the same discipline pointed the other way. Codex hands back ten claims that
something is *wrong*; ADHD hands back thirty claims that something is
*possible*, which is the more seductive failure — a plausible idea costs
nothing to read and a week to unbuild.

So the record is generated `unreviewed` and `check()` fails until every row
carries a verdict. The verdicts are deliberately few, and three of the four
are refusals:

    kept       going to be built, or already is
    rejected   read it, decided against it, said why in the note
    parked     real, not now — needs a date or a condition, not a shrug
    unreviewed the generated state; the gate fails while any remain

`parked` exists because forcing "yes or no" onto a good idea with no slot
produces a dishonest `rejected`, and a `rejected` row nobody believes is how
the whole record stops being read.

## The zero-rejection rule, restated for this lane

`CLAUDE.md` says a Codex checkpoint where nothing was rejected means the
files were not opened. The same shape holds here and is worth stating in its
own terms, because the reasoning is not identical. Codex reports defects, and
a commit with no defects is a real outcome you can reach honestly. ADHD's
divergence phase is *told to generate without evaluating* — the frames are
instructed to produce ideas and forbidden from ranking them, so a run
mechanically emits its full quota of ideas whether or not the design space
holds that many good ones. A record where all thirty survived is not a
remarkable run; it is an unread one.

`check()` therefore warns loudly on zero rejections rather than failing.
Warning and not failing is the deliberate half: making it fatal would just
teach the reader to reject one row to get past the gate, which converts an
honest signal into a ritual. The gate blocks on *unreviewed*, which cannot be
faked without reading, and only complains about zero-rejected, which can.

## What is not here

No cost is recorded. `fragrance_graph.budget` is an append-only ledger whose
value is that it shows what actually happened, and the `adhd` CLI does not
report what a run charged. Writing an estimate into it would put a figure
there that can never be settled against a real one — a wrong number that,
by that ledger's own rules, stays written. The lane prints an estimate to
the terminal before it spends anything instead, and `docs/decisions/README.md`
records the gap. The daily cap is not consulted either: it guards unattended
loops, and this lane only ever runs because a person typed the question.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

VERDICTS = ("kept", "rejected", "parked", "unreviewed")
UNREVIEWED = "unreviewed"

# A row in the verdict table: | id | verdict | idea | note |
# Tolerant of the whitespace a person editing markdown by hand will produce,
# and of a note containing anything except a pipe.
_ROW = re.compile(
    r"^\|\s*(?P<id>[^|]+?)\s*\|\s*(?P<verdict>[^|]+?)\s*\|\s*(?P<idea>[^|]*?)\s*\|\s*(?P<note>[^|]*?)\s*\|\s*$"
)


class MalformedRun(ValueError):
    """The JSON does not have the shape `adhd --json` emits."""


@dataclass(frozen=True)
class Row:
    idea_id: str
    verdict: str
    idea: str
    note: str


@dataclass(frozen=True)
class CheckResult:
    """The gate's answer. `ok` is what the shell exits on."""

    ok: bool
    unreviewed: list[str]
    kept: int
    rejected: int
    parked: int
    warnings: list[str]

    @property
    def total(self) -> int:
        return self.kept + self.rejected + self.parked + len(self.unreviewed)


def _text(idea: dict) -> str:
    """The one-line phrase, flattened.

    A newline inside a table cell ends the table, so an idea that arrives as
    a paragraph is collapsed rather than trusted to be one line.
    """
    raw = str(idea.get("text") or idea.get("id") or "").strip()
    # `&#124;` renders as a pipe and is inert to `_ROW`; a backslash-escaped
    # pipe would still be a literal `|` and would end the cell.
    return re.sub(r"\s+", " ", raw).replace("|", "&#124;")


def _score_cell(idea: dict) -> str:
    score = idea.get("score") or {}
    parts = [score.get("novelty"), score.get("viability"), score.get("fit")]
    if all(p is None for p in parts):
        return "—"
    return "/".join("—" if p is None else str(p) for p in parts)


def _all_ideas(run: dict) -> list[dict]:
    """Every distinct idea the run produced, root and deepened alike.

    Ideas appear in several places in a `RunResult` — inside `branches`, again
    in `shortlist`, `traps` and `nonObviousPick`, and child ideas hang off
    `deepened`. De-duplicating on `id` is what stops one idea occupying four
    rows and inflating the denominator the zero-rejection rule reads.
    """
    seen: dict[str, dict] = {}

    def take(ideas: Iterable[dict] | None) -> None:
        for idea in ideas or []:
            if not isinstance(idea, dict):
                continue
            key = str(idea.get("id") or _text(idea))
            # Later copies carry scores the raw branch copies do not, so a
            # scored duplicate replaces an unscored one.
            if key not in seen or (idea.get("score") and not seen[key].get("score")):
                seen[key] = idea

    for branch in run.get("branches") or []:
        take(branch.get("ideas"))
    take(run.get("shortlist"))
    take(run.get("traps"))
    pick = run.get("nonObviousPick")
    take([pick] if pick else [])
    for deep in run.get("deepened") or []:
        take(deep.get("childIdeas"))
    return list(seen.values())


def _trap_reason(idea: dict) -> str | None:
    return ((idea.get("score") or {}).get("trap")) or None


def render(run: dict, *, slug: str, question: str, command: str, commit: str) -> str:
    """Build the decision record. Every idea starts `unreviewed`."""
    if not isinstance(run, dict) or "branches" not in run:
        raise MalformedRun(
            "expected the object `adhd --json` emits (a RunResult with `branches`). "
            "Check that the run completed and that --json was passed."
        )

    ideas = _all_ideas(run)
    by_id = {str(i.get("id") or _text(i)): i for i in ideas}
    trap_ids = {str(t.get("id")) for t in (run.get("traps") or []) if isinstance(t, dict)}
    short_ids = {str(s.get("id")) for s in (run.get("shortlist") or []) if isinstance(s, dict)}
    pick = run.get("nonObviousPick") or {}
    pick_id = str(pick.get("id")) if pick else None

    out: list[str] = []
    add = out.append

    add(f"# {slug}")
    add("")
    add(f"**Question.** {question}")
    add("")
    add(f"- Run: `{command}`")
    add(f"- Commit at run time: `{commit}`")
    add(f"- Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    add(f"- Frames: {len(run.get('branches') or [])} · ideas surfaced: {len(ideas)}")
    if run.get("reframe"):
        add(f"- Reframed to: *{run['reframe']}*")
    add("")
    add("> Generated by `scripts/adhd-widen.sh`. Every idea below is an argument,")
    add("> not an instruction. Nothing here changes this tree until a person rules")
    add("> on it — see `docs/decisions/README.md`.")
    add("")

    add("## Decision")
    add("")
    add("<!-- Written by a person, after the table below is filled in. -->")
    add("")
    add("_Not yet decided._")
    add("")

    add("## Verdicts")
    add("")
    add("One row per idea. Set each verdict to `kept`, `rejected` or `parked` and")
    add("say why in the note. `scripts/adhd-widen.sh check` fails while any row")
    add(f"still reads `{UNREVIEWED}`.")
    add("")
    add("| id | verdict | idea | note |")
    add("| --- | --- | --- | --- |")
    for idea_id, idea in by_id.items():
        marks = []
        if idea_id == pick_id:
            marks.append("★")
        if idea_id in short_ids:
            marks.append("shortlist")
        if idea_id in trap_ids:
            marks.append("TRAP")
        label = _text(idea)
        if marks:
            label = f"{label} _({', '.join(marks)})_"
        add(f"| {idea_id} | {UNREVIEWED} | {label} | |")
    add("")

    if pick_id and pick_id in by_id:
        add("## The non-obvious pick")
        add("")
        add(f"**{_text(by_id[pick_id])}** — `{pick_id}`, scored {_score_cell(by_id[pick_id])}")
        add("(novelty/viability/fit).")
        add("")
        add("Highest-novelty viable idea, which is not the same as the best one.")
        add("It is surfaced separately because the shortlist sorts by fit, and fit")
        add("is the axis most biased toward what already exists here.")
        add("")

    if run.get("traps"):
        add("## Traps")
        add("")
        add("Ideas the critic flagged as looking good and not being good, each with")
        add("the mechanical reason. These are the rows most worth disagreeing with:")
        add("the critic has no access to this codebase's constraints.")
        add("")
        for trap in run["traps"]:
            if not isinstance(trap, dict):
                continue
            reason = _trap_reason(trap) or "no reason given"
            add(f"- **{_text(trap)}** — {reason}")
        add("")

    if run.get("clusters"):
        add("## The shape of the space")
        add("")
        add("Angle-level clusters, not keyword groups. Argue at this level first —")
        add("if a whole cluster is wrong for this project, that is one decision")
        add("instead of six.")
        add("")
        for cluster in run["clusters"]:
            if not isinstance(cluster, dict):
                continue
            ids = cluster.get("ideaIds") or []
            joined = ", ".join(map(str, ids))
            add(f"- **{cluster.get('label', 'unlabelled')}** ({len(ids)}): {joined}")
        add("")

    if run.get("deepened"):
        add("## Deepened")
        add("")
        for deep in run["deepened"]:
            if not isinstance(deep, dict):
                continue
            idea_id = str(deep.get("ideaId"))
            head = _text(by_id[idea_id]) if idea_id in by_id else idea_id
            add(f"### {head}")
            add("")
            add(f"`{idea_id}`")
            add("")
            add(str(deep.get("sketch") or "_no sketch returned._"))
            add("")
            children = deep.get("childIdeas") or []
            if children:
                add("Children:")
                add("")
                for child in children:
                    if isinstance(child, dict):
                        add(f"- {_text(child)}")
                add("")

    if run.get("provocation"):
        add("## Provocation")
        add("")
        add(f"> {run['provocation']}")
        add("")

    return "\n".join(out).rstrip() + "\n"


def parse_rows(record: str) -> list[Row]:
    """Read the verdict table back out of an edited record.

    Reads only the table under `## Verdicts`, so a pipe table someone adds to
    the Decision section cannot be mistaken for a verdict row.
    """
    rows: list[Row] = []
    in_section = False
    for line in record.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_section = stripped.lower() == "## verdicts"
            continue
        if not in_section:
            continue
        match = _ROW.match(stripped)
        if not match:
            continue
        idea_id = match["id"]
        verdict = match["verdict"].lower()
        if idea_id in {"id", "---"} or set(idea_id) == {"-"}:
            continue  # header and separator
        rows.append(Row(idea_id, verdict, match["idea"], match["note"]))
    return rows


def check(record: str) -> CheckResult:
    """Is this record reviewed? See the module docstring for what that means."""
    rows = parse_rows(record)
    warnings: list[str] = []

    unreviewed = [r.idea_id for r in rows if r.verdict == UNREVIEWED]
    unknown = sorted({r.verdict for r in rows} - set(VERDICTS))
    kept = sum(1 for r in rows if r.verdict == "kept")
    rejected = sum(1 for r in rows if r.verdict == "rejected")
    parked = sum(1 for r in rows if r.verdict == "parked")

    if not rows:
        warnings.append("no verdict rows found — is this a generated record?")
    if unknown:
        warnings.append(
            f"unrecognised verdict(s): {', '.join(unknown)}. Use one of: {', '.join(VERDICTS)}."
        )
    if rows and not unreviewed and rejected == 0:
        warnings.append(
            "nothing was rejected. Divergence is instructed to generate without "
            "evaluating, so a run emits its full quota whether or not the space "
            "holds that many good ideas — all-survived usually means the ideas "
            "were skimmed, not weighed. Re-read the table before committing."
        )
    for row in rows:
        if row.verdict in {"rejected", "parked"} and not row.note.strip():
            warnings.append(f"{row.idea_id}: {row.verdict} with no reason given.")

    return CheckResult(
        ok=not unreviewed and not unknown and bool(rows),
        unreviewed=unreviewed,
        kept=kept,
        rejected=rejected,
        parked=parked,
        warnings=warnings,
    )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)

    render_cmd = sub.add_parser("render", help="RunResult JSON on stdin -> decision record")
    render_cmd.add_argument("--slug", required=True)
    render_cmd.add_argument("--question", required=True)
    render_cmd.add_argument("--command", default="")
    render_cmd.add_argument("--commit", default="")
    render_cmd.add_argument("--out", required=True, type=Path)

    check_cmd = sub.add_parser("check", help="fail while any verdict is unreviewed")
    check_cmd.add_argument("record", type=Path)

    args = parser.parse_args(argv)

    if args.mode == "render":
        import sys

        try:
            run = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            print(f"adhd-render: stdin is not JSON ({exc}).", file=sys.stderr)
            print("  The CLI writes progress to stderr and JSON to stdout;", file=sys.stderr)
            print("  a run that failed leaves stdout empty.", file=sys.stderr)
            return 2
        try:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                render(
                    run,
                    slug=args.slug,
                    question=args.question,
                    command=args.command,
                    commit=args.commit,
                )
            )
        except MalformedRun as exc:
            print(f"adhd-render: {exc}", file=sys.stderr)
            return 2
        print(str(args.out))
        return 0

    result = check(args.record.read_text())
    for warning in result.warnings:
        print(f"warning: {warning}")
    print(
        f"{result.total} ideas — {result.kept} kept, {result.rejected} rejected, "
        f"{result.parked} parked, {len(result.unreviewed)} unreviewed"
    )
    if result.unreviewed:
        print("still unreviewed: " + ", ".join(result.unreviewed))
    return 0 if result.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
