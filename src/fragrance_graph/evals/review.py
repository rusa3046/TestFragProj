"""Reviewing drafted labels one at a time.

`labels import` refuses a file whose entries still carry `drafted_by`,
because a model writing the answer key makes the score measure agreement
between models rather than accuracy. Clearing that marker is the act that
records a person standing behind a row.

Doing it in a text editor means scrolling a 17KB JSON file and deleting a
line per entry, which is tedious in exactly the way that produces a
`sed -i '/drafted_by/d'` and thirty-five rubber stamps. This makes the
honest path the easy one: read the comment, look at the claims, press a
key. The marker is dropped only on rows a person actually answered for.

    python -m fragrance_graph.evals.review eval-batch.json

Progress is saved on every answer, so a session can be stopped after ten
rows and resumed later — reviewed rows are simply no longer marked, and
this skips them.

Nothing here consults the extractor, the database, or the drafter's
confidence. The only question is what the comment says.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from fragrance_graph.models import ClaimType

log = logging.getLogger("fragrance_graph.evals.review")

MENU = """
  [a] accept        the claims are right
  [n] asserts nothing   clear the claims — the comment makes no claim
  [t] change type   fix DUPE_OF / SIMILAR_TO / ...
  [s] skip          decide later; stays marked as a draft
  [q] save and quit
"""


@dataclass
class Progress:
    reviewed: int = 0
    accepted: int = 0
    emptied: int = 0
    retyped: int = 0
    skipped: int = 0
    remaining: int = 0


def render_entry(entry: dict, *, position: int, total: int) -> str:
    """One comment and what the drafter said about it."""
    lines = [
        "",
        "=" * 72,
        f"  {position} of {total}"
        + (f"   [{entry['_stratum']}]" if entry.get("_stratum") else ""),
        "=" * 72,
        "",
        (entry.get("body") or "").strip(),
        "",
    ]
    claims = entry.get("claims") or []
    if not claims:
        lines.append("  drafted: NO CLAIMS — the drafter says this asserts nothing.")
    else:
        lines.append(f"  drafted {len(claims)} claim(s):")
        for i, claim in enumerate(claims, 1):
            subject = claim.get("raw_subject_text")
            obj = claim.get("raw_object_text")
            arrow = f" -> {obj!r}" if obj else ""
            lines.append(
                f"    {i}. {claim.get('claim_type')}: {subject!r}{arrow}"
                f"  [{claim.get('sentiment', '-')}]"
            )
    return "\n".join(lines)


def _retype(entry: dict, ask: Callable[[str], str], say: Callable[[str], None]) -> bool:
    """Change one claim's type. Returns whether anything changed."""
    claims = entry.get("claims") or []
    if not claims:
        say("  Nothing to retype — this entry has no claims.")
        return False

    which = 1
    if len(claims) > 1:
        answer = ask(f"  which claim? [1-{len(claims)}] ").strip()
        if not answer.isdigit() or not 1 <= int(answer) <= len(claims):
            say("  Not a claim number; nothing changed.")
            return False
        which = int(answer)

    options = [t.value for t in ClaimType]
    say("")
    for i, name in enumerate(options, 1):
        say(f"    {i:>2}. {name}")
    answer = ask("  new type? ").strip()
    if answer.isdigit() and 1 <= int(answer) <= len(options):
        chosen = options[int(answer) - 1]
    elif answer.upper() in options:
        chosen = answer.upper()
    else:
        say("  Not a type; nothing changed.")
        return False

    claims[which - 1]["claim_type"] = chosen
    say(f"  claim {which} is now {chosen}.")
    return True


def review(
    entries: list[dict],
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    save: Callable[[], None] = lambda: None,
) -> Progress:
    """Walk the still-drafted entries. Mutates `entries` in place.

    `save` is called after every decision rather than at the end: a
    labelling session that loses an hour to a closed terminal is a session
    nobody starts again.
    """
    progress = Progress()
    pending = [e for e in entries if e.get("drafted_by")]
    progress.remaining = len(pending)
    if not pending:
        return progress

    total = len(pending)
    for position, entry in enumerate(pending, 1):
        say(render_entry(entry, position=position, total=total))
        say(MENU)
        while True:
            choice = (ask("  > ").strip() or "s")[0].lower()
            if choice == "t":
                _retype(entry, ask, say)
                progress.retyped += 1
                # Re-show the row *and* the menu: a correction is not an
                # approval, and the next keypress is still the decision.
                say(render_entry(entry, position=position, total=total))
                say(MENU)
                continue
            if choice in {"a", "n", "s", "q"}:
                break
            say("  a, n, t, s or q.")

        if choice == "q":
            say("\nSaved. Re-run to continue where this stopped.")
            break
        if choice == "s":
            progress.skipped += 1
            continue

        if choice == "n":
            entry["claims"] = []
            progress.emptied += 1
        else:
            progress.accepted += 1
        # The marker goes only here, on a row a person just answered for.
        entry.pop("drafted_by", None)
        entry.pop("pronoun_policy", None)
        progress.reviewed += 1
        save()

    progress.remaining = sum(1 for e in entries if e.get("drafted_by"))
    return progress


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review drafted labels one at a time, and sign for them."
    )
    parser.add_argument("file", type=Path, help="A drafted template")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    entries = json.loads(args.file.read_text(encoding="utf-8"))

    def save() -> None:
        args.file.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    pending = sum(1 for e in entries if e.get("drafted_by"))
    if not pending:
        print(
            f"Nothing left to review in {args.file}: no entry is marked as a "
            "draft.\n\n"
            f"  python -m fragrance_graph.evals.labels import {args.file} "
            "--labeler you"
        )
        return 0

    print(f"{pending} of {len(entries)} entries still need a person.")
    progress = review(entries, save=save)
    save()

    print(
        f"\n{progress.reviewed} signed off "
        f"({progress.accepted} accepted, {progress.emptied} set to no claims), "
        f"{progress.skipped} skipped."
    )
    if progress.remaining:
        print(f"{progress.remaining} still marked. Re-run to continue.")
    else:
        print(
            "\nAll reviewed. Now:\n"
            f"  python -m fragrance_graph.evals.labels import {args.file} "
            "--labeler you"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
