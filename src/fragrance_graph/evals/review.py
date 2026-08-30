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

#: How a commenter says the relationship does *not* hold. Used only to
#: find rows worth a second look — a hit means "read this again", never
#: "this is a denial".
DENIAL_LANGUAGE = (
    "nothing like", "not like", "no clone", "not a clone", "not a dupe",
    "isn't a dupe", "is not a dupe", "doesn't smell", "does not smell",
    "far from", "not similar", "nowhere near", "not even close",
    "smells nothing", "no dupe", "not the same",
)


def looks_like_a_denial(entry: dict) -> bool:
    """Whether a signed row may have recorded a denial as an assertion.

    Rows signed before the reviewer could express polarity carry claims
    that are implicitly ASSERTED. This finds the ones whose comment reads
    like a rejection so they can be re-opened, rather than re-reviewing
    everything.
    """
    claims = entry.get("claims") or []
    if not claims:
        return False
    if any(c.get("polarity") for c in claims):
        # An explicit polarity — either value — means a person has already
        # answered for this row. Testing for DENIED alone was wrong and
        # produced a loop: accepting a row left its polarity absent, so it
        # still matched and came back on every re-run, forever.
        return False
    body = (entry.get("body") or "").lower()
    return any(word in body for word in DENIAL_LANGUAGE)


def needs_reading(entry: dict) -> bool:
    """An entry nobody has answered for, drafted or not.

    `silent.json` rows carry no draft and no claims, so neither the draft
    marker nor the denial filter selects them — and an unread file imports
    as "these 15 comments assert nothing", which is a real claim about the
    corpus that nobody made. Signing sets `_reviewed`, so answering a row
    takes it out of this set the way clearing `drafted_by` does.
    """
    return not entry.get("_reviewed") and not entry.get("drafted_by")


def stamp_polarity(entry: dict) -> None:
    """Make an accepted row's polarity explicit.

    Absent polarity reads as ASSERTED everywhere downstream, so writing it
    changes no meaning. What it changes is whether the row can be told
    apart from one nobody has looked at — which is the difference between
    "confirmed as asserted" and "not yet answered".
    """
    for claim in entry.get("claims") or []:
        claim.setdefault("polarity", "ASSERTED")

log = logging.getLogger("fragrance_graph.evals.review")

#: Shown for rows that arrive with nothing on them — no draft to accept,
#: so the only answers are "it says nothing" or a claim typed by hand.
EMPTY_MENU = """
  [n] says nothing about two fragrances smelling alike
  [+] add a claim   type what this comment actually says
  [s] skip          decide later
  [q] save and quit
"""

MENU = """
  [a] accept        the claims are right
  [n] asserts nothing   clear the claims — the comment makes no claim
  [t] change type   fix DUPE_OF / SIMILAR_TO / ...
  [p] flip polarity ASSERTED <-> DENIED — "X is NOT a dupe of Y"
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
        # Only a row a drafter actually saw may report a drafter's
        # opinion. A `sample plan` batch arrives with no draft at all, and
        # telling its labeller "the drafter says this asserts nothing"
        # invents a second opinion out of an empty list — then anchors
        # them toward the `n` key with it. That is precisely the
        # model-writes-the-answer-key failure the blind step exists to
        # prevent, arriving through the one door nobody was watching: the
        # `silent` stratum, whose whole purpose is finding claims the
        # extractor missed.
        lines.append(
            "  drafted: NO CLAIMS — the drafter says this asserts nothing."
            if entry.get("drafted_by")
            else "  no draft — nobody has read this yet."
        )
    else:
        lines.append(f"  drafted {len(claims)} claim(s):")
        for i, claim in enumerate(claims, 1):
            subject = claim.get("raw_subject_text")
            obj = claim.get("raw_object_text")
            arrow = f" -> {obj!r}" if obj else ""
            # Polarity is shown even when absent, and loudly when DENIED.
            # A denial stored as an assertion quotes someone as evidence
            # for the claim they were rejecting — the worst defect this
            # corpus has had, and invisible if the field is not on screen.
            polarity = claim.get("polarity", "ASSERTED")
            mark = "  ** DENIED **" if polarity == "DENIED" else ""
            lines.append(
                f"    {i}. {claim.get('claim_type')}: {subject!r}{arrow}"
                f"  [{claim.get('sentiment', '-')}]{mark}"
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


def _flip_polarity(
    entry: dict, ask: Callable[[str], str], say: Callable[[str], None]
) -> bool:
    """Toggle a claim between ASSERTED and DENIED.

    The drafter does not emit polarity at all, so every drafted claim
    arrives as an implicit assertion — including the denials. "There is no
    clone of TF Oud Wood that captures the scent" drafts as two claims
    saying those houses *are* dupes, which is precisely backwards.
    """
    claims = entry.get("claims") or []
    if not claims:
        say("  Nothing to flip — this entry has no claims.")
        return False

    which = 1
    if len(claims) > 1:
        answer = ask(f"  which claim? [1-{len(claims)}, or 'all'] ").strip().lower()
        if answer in {"all", "a", "*"}:
            for claim in claims:
                claim["polarity"] = (
                    "DENIED" if claim.get("polarity", "ASSERTED") == "ASSERTED"
                    else "ASSERTED"
                )
            say(f"  flipped all {len(claims)} claims.")
            return True
        if not answer.isdigit() or not 1 <= int(answer) <= len(claims):
            say("  Not a claim number; nothing changed.")
            return False
        which = int(answer)

    claim = claims[which - 1]
    claim["polarity"] = (
        "DENIED" if claim.get("polarity", "ASSERTED") == "ASSERTED" else "ASSERTED"
    )
    say(f"  claim {which} is now {claim['polarity']}.")
    return True


def _add_claim(
    entry: dict, ask: Callable[[str], str], say: Callable[[str], None]
) -> bool:
    """Type a claim in, for a comment that arrived with none."""
    say("\n  Which two things is the comment comparing?")
    subject = ask("  the fragrance being talked about: ").strip()
    if not subject:
        say("  Nothing entered; no claim added.")
        return False
    obj = ask("  the one it is compared to:        ").strip()
    if not obj:
        say("  Nothing entered; no claim added.")
        return False

    say("")
    say("    1. DUPE_OF     a cheaper stand-in: 'dupe', 'clone', 'inspired by'")
    say("    2. SIMILAR_TO  just smells alike: 'reminds me of', 'smells like'")
    answer = ask("  which? [1/2] ").strip()
    claim_type = "DUPE_OF" if answer.startswith("1") else "SIMILAR_TO"

    say("")
    say("  Is the person saying these DO smell alike, or that they DON'T?")
    answer = ask("  [y] they do  /  [n] they don't: ").strip().lower()
    polarity = "DENIED" if answer.startswith("n") else "ASSERTED"

    entry.setdefault("claims", []).append({
        "claim_type": claim_type,
        "raw_subject_text": subject,
        "raw_object_text": obj,
        "sentiment": "NEUTRAL",
        "polarity": polarity,
    })
    say(f"  added: {claim_type} {subject!r} -> {obj!r} [{polarity}]")
    return True


def review(
    entries: list[dict],
    *,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    save: Callable[[], None] = lambda: None,
    select: Callable[[dict], bool] = lambda e: bool(e.get("drafted_by")),
) -> Progress:
    """Walk the still-drafted entries. Mutates `entries` in place.

    `save` is called after every decision rather than at the end: a
    labelling session that loses an hour to a closed terminal is a session
    nobody starts again.
    """
    progress = Progress()
    pending = [e for e in entries if select(e)]
    progress.remaining = len(pending)
    if not pending:
        return progress

    total = len(pending)
    for position, entry in enumerate(pending, 1):
        # A row with no draft and no claims has nothing to accept, so it
        # gets the menu whose answers actually apply to it.
        blank = not entry.get("drafted_by") and not (entry.get("claims") or [])
        say(render_entry(entry, position=position, total=total))
        say(EMPTY_MENU if blank else MENU)
        while True:
            choice = (ask("  > ").strip() or "s")[0].lower()
            if choice in {"+", "e"}:
                _add_claim(entry, ask, say)
                say(render_entry(entry, position=position, total=total))
                say(MENU if entry.get("claims") else EMPTY_MENU)
                continue
            if choice == "p":
                _flip_polarity(entry, ask, say)
                say(render_entry(entry, position=position, total=total))
                say(MENU)
                continue
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
            say("  n, +, s or q." if blank else "  a, n, t, p, s or q.")

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
            stamp_polarity(entry)
            progress.accepted += 1
        # Records that a person answered for this row even when the answer
        # left it empty, which `drafted_by` alone cannot express.
        entry["_reviewed"] = True
        # The marker goes only here, on a row a person just answered for.
        entry.pop("drafted_by", None)
        entry.pop("pronoun_policy", None)
        progress.reviewed += 1
        save()

    progress.remaining = sum(1 for e in entries if select(e))
    return progress


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review drafted labels one at a time, and sign for them."
    )
    parser.add_argument("file", type=Path, help="A drafted template")
    parser.add_argument(
        "--recheck-denials", action="store_true",
        help=(
            "Re-open rows already signed whose comment reads like a "
            "rejection but whose claims are all ASSERTED. For batches "
            "signed before the reviewer could express polarity."
        ),
    )
    parser.add_argument(
        "--all", action="store_true", dest="review_all",
        help="Re-open every row, signed or not.",
    )
    parser.add_argument(
        "--unread", action="store_true",
        help=(
            "Read rows that arrived with no draft and no claims — a "
            "`sample plan` silent batch. Without this they look finished "
            "and import as 'these comments assert nothing'."
        ),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    entries = json.loads(args.file.read_text(encoding="utf-8"))

    def save() -> None:
        args.file.write_text(
            json.dumps(entries, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    if args.review_all:
        select = lambda e: True  # noqa: E731
        what = "every row"
    elif args.unread:
        select = needs_reading  # type: ignore[assignment]
        what = "rows nobody has read yet"
    elif args.recheck_denials:
        select = looks_like_a_denial  # type: ignore[assignment]
        what = "rows that read like a denial but were signed as asserted"
    else:
        select = lambda e: bool(e.get("drafted_by"))  # noqa: E731
        what = "entries still marked as drafts"

    pending = sum(1 for e in entries if select(e))
    if not pending:
        print(
            f"Nothing to review in {args.file}: no {what}.\n\n"
            f"  python -m fragrance_graph.evals.labels import {args.file} "
            "--labeler you"
        )
        return 0

    print(f"{pending} of {len(entries)} entries: {what}.")
    progress = review(entries, save=save, select=select)
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
