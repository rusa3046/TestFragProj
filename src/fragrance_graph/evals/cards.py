"""The cards a shopper actually reads, rendered and checked in.

    uv run python -m fragrance_graph.evals.cards            # check
    uv run python -m fragrance_graph.evals.cards --update   # rewrite the golden

## Why this exists

Six real defects shipped in one week and every one was found by a person
looking at a screenshot, not by the suite:

    "less sweet" counted as evidence FOR sweet
    "People call it Sandalwood is among the declared notes"
    one chip tapped twice scored twice and printed its caveat twice
    a declared note scored 0.5 against a community match's 2.0
    the "strong" chip compiled to a note, so projection evidence never matched
    "under $200" on a bottle whose 3.3oz is $510  (judged correct, see below)

All of them passed 1,815 tests. That is not a gap in coverage, it is a
gap in *kind*: the suite tests functions, and a card is a composition of
a dozen functions plus the corpus plus the ordering. Every unit was
right; the sentence a customer read was wrong.

This project's whole claim is that you can trust what it says. That claim
is checked here — on the assembled sentences, against the committed
corpus, in the customer's own words — or it is not really checked at all.

## How it works, and the one rule that makes it worth anything

Cases live in `data/eval/cards.jsonl`. Each is a real composition (chips,
or a typed sentence) and renders through **`api._session_response`** —
the identical function the kiosk's own HTTP handlers return. Not a
reimplementation of it. A golden file that rendered its own copy of a
card would drift from the product silently and pass forever, which is
precisely the failure it was built to catch.

The output is committed at `data/eval/cards.golden.txt`. A change to
wording, ordering, scoring, chip status or coverage shows up as a diff in
review, where a human decides whether it is an improvement or the seventh
bug of this kind. Nothing here asserts a card is *good*; it asserts the
cards are what a person last read and approved.

## Reading a failure

A diff is not automatically a regression — most will be intended, and the
fix is `--update` plus reading what changed. The suite's value is that
the change is *visible* and has to be looked at, rather than reaching a
shopper first.

## Determinism

`recommend_plan` sorts on `(-score, -people, name)` and the composer
tie-break is a stable sort with the original index as its final key, so
the ordering is a pure function of the corpus. The corpus is committed.
Therefore a clean clone that has run the documented rebuild reproduces
this file byte for byte — the same property `corpus export` holds, and
for the same reason: a golden that only reproduces on one machine is a
diary, not a test.

Scores are deliberately **not** rendered. They are an implementation
detail that moves whenever a weight is tuned, and printing them would
make every legitimate ranking change a wall of noise around the one line
that matters. Order carries the ranking; the sentences carry the claim.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.session import PreferenceItem, PreferenceState

DEFAULT_CASES = Path("data/eval/cards.jsonl")
DEFAULT_GOLDEN = Path("data/eval/cards.golden.txt")

_RULE = "=" * 72
_THIN = "-" * 72


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    """Cases in file order — the order they render in, so a reader can
    scan the golden top to bottom against the source."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _state_for(conn: psycopg.Connection, case: dict) -> PreferenceState:
    """One case's `PreferenceState`, built through the same two public
    entry points the API uses — `merge_utterance` for a typed sentence,
    `add_item` for a chip — rather than by setting fields directly. A
    state assembled by hand could hold a combination neither entry point
    can produce, and then this file would be pinning cards no shopper can
    reach.
    """
    state = PreferenceState()
    for text in case.get("says", []):
        state.merge_utterance(conn, text)
    for chip in case.get("chips", []):
        state.add_item(conn, PreferenceItem(
            bucket=chip["bucket"],
            entity_type=chip["entity_type"],
            value=chip.get("value", ""),
            mode=chip.get("mode"),
            operator=chip.get("operator"),
            amount=chip.get("amount"),
        ))
    return state


def _describe_input(case: dict) -> list[str]:
    lines = []
    for text in case.get("says", []):
        lines.append(f'  said: "{text}"')
    for chip in case.get("chips", []):
        label = chip.get("value", "")
        if chip["entity_type"] == "budget":
            label = f"{chip.get('operator', '')} {chip.get('amount', '')}".strip()
        mode = f" ({chip['mode']})" if chip.get("mode") else ""
        lines.append(f"  chip: {chip['bucket']} {chip['entity_type']} {label}{mode}")
    return lines


def _render_case(conn: psycopg.Connection, case: dict) -> str:
    # Imported here, not at module scope: `api` pulls in FastAPI, which
    # lives in the `[api]` extra. A core install must still be able to
    # import every `evals` module, and only this function needs it.
    from fragrance_graph.api import _session_response

    state = _state_for(conn, case)
    body = _session_response(conn, state)

    out = [_RULE, f"CASE {case['id']}"]
    if case.get("why"):
        out.append(f"  why: {case['why']}")
    out += _describe_input(case)
    out.append(_THIN)
    out.append(f"HEADLINE: {body['note'] or '(none)'}")

    interpreted = body["interpreted_preferences"]
    for field in ("anchors", "preserve", "reduce", "exclude", "target", "constraints"):
        values = interpreted.get(field) or []
        if values:
            out.append(f"  {field}: {', '.join(str(v) for v in values)}")
    # Already flattened to "preference: reason" strings by
    # `_interpreted_preferences` — rendered as-is rather than re-joined
    # here, so the golden pins the sentence a client actually receives.
    for entry in interpreted.get("unexpressed") or []:
        out.append(f"  NOT USED: {entry}")

    cards = body["commerce"]["cards"]
    if not cards:
        out.append("  (no results)")
    for position, card in enumerate(cards, start=1):
        out.append("")
        out.append(f"[{position}] {card['result_tier']}  {card['name']}")
        chips = body["results"][position - 1].get("preference_status") or []
        if chips:
            # `display` is the label a UI actually shows — for a budget chip,
            # "from $100 (0.33 oz)" — so it is what the golden pins.
            rendered = "  ".join(
                f"{chip.get('display') or chip['value'] or chip['entity_type']}"
                f"={chip['status']}"
                for chip in chips
            )
            out.append(f"    chips: {rendered}")
        digest = body["results"][position - 1].get("digest")
        if digest:
            out.append(f"    digest: {digest}")
        for signal in card["fit_signals"]:
            out.append(f"    why:  {signal}")
        for tradeoff in card["relevant_tradeoffs"]:
            out.append(f"    know: {tradeoff}")
        out.append(f"    coverage: {card['community_coverage']}")
    out.append("")
    return "\n".join(out)


def render_all(conn: psycopg.Connection, cases: list[dict]) -> str:
    header = [
        "FACET card golden file.",
        "",
        "Rendered by `python -m fragrance_graph.evals.cards --update` from",
        "data/eval/cards.jsonl, through the same `api._session_response` the",
        "kiosk calls. Committed so any change to a customer-visible sentence,",
        "to result ordering, or to a chip's status shows up as a reviewable",
        "diff instead of as a screenshot from somebody using the product.",
        "",
        "Scores are deliberately absent — order carries the ranking.",
        "",
    ]
    return "\n".join(header) + "\n".join(_render_case(conn, case) for case in cases)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.evals.cards",
        description="Render the customer-visible cards and diff them "
                    "against the committed golden file.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--golden", type=Path, default=DEFAULT_GOLDEN)
    parser.add_argument(
        "--update", action="store_true",
        help="Rewrite the golden file. Read the diff before committing it.",
    )
    args = parser.parse_args(argv)

    conn = get_connection(args.db_url)
    try:
        rendered = render_all(conn, load_cases(args.cases))
    finally:
        conn.close()

    if args.update:
        args.golden.parent.mkdir(parents=True, exist_ok=True)
        args.golden.write_text(rendered, encoding="utf-8")
        print(f"wrote {args.golden}")
        return 0

    if not args.golden.exists():
        print(f"no golden file at {args.golden}; run with --update", file=sys.stderr)
        return 1

    committed = args.golden.read_text(encoding="utf-8")
    if committed == rendered:
        print(f"cards: {len(load_cases(args.cases))} case(s), no drift")
        return 0

    diff = difflib.unified_diff(
        committed.splitlines(), rendered.splitlines(),
        fromfile="committed", tofile="rendered", lineterm="",
    )
    print("\n".join(diff))
    print(
        "\nThe cards changed. If that is the intended improvement, re-run "
        "with --update and commit the new golden alongside the change.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
