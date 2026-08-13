"""Checking whether the extractor gets denials right.

A denial stored as an assertion is the worst defect this project has
measured: "I just try latafa it is nothing like angel share" recorded as
`DUPE_OF latafa -> angel share` puts a real person's name behind the
opposite of what they wrote, on a product that sells verbatim evidence.

Measured at 36 of 499 similarity claims — 7.2% — before `polarity` existed.

**Why this exists instead of an eval metric.** At ~7% incidence and 13
labelled comments, the expected number of denials in the eval sample is
0.4. F1 cannot adjudicate a fix for a failure it has not met. But the
failure is *enumerable*: denial language is lexically narrow, so the
suspects can be listed and checked by eye. That converts "wait for a
bigger eval" into "look at forty rows", which is a fix you can verify
today.

    audit    rows whose evidence reads like a denial, with their polarity

The regex is a *recall* instrument, not a classifier. It is tuned to
over-flag: a false flag costs a glance, a missed denial ships. It is
never used to write polarity — only the model does that, so the corpus
never records this file's guesses as though a model had judged them.
"""

from __future__ import annotations

import argparse
import logging
import re

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, Row, get_connection, migrate

log = logging.getLogger("fragrance_graph.extract.polarity")

#: Language observed denying a comparison, drawn from the 36 real cases in
#: the first corpus rather than invented. Deliberately broad.
DENIAL_PATTERN = re.compile(
    r"\b("
    r"nothing (?:a)?like|not (?:even )?(?:a)?like|isn'?t (?:a)?like|"
    r"do(?:es)?n'?t smell (?:any)?(?:thing )?(?:a)?like|"
    r"does not smell|no clone|not a (?:1[:\-]?1 )?(?:dupe|clone)|"
    r"far (?:from|away)|nowhere near|not even (?:close|similar)|"
    r"isn'?t a|is not a|doesn'?t even (?:come close|smell)|"
    r"smells? (?:completely|totally|way) different|"
    r"completely different|won'?t be similar|"
    r"don'?t smell a ?like|smell nothing alike|smells nothing alike"
    r")\b",
    re.IGNORECASE,
)

#: Only comparison types can be denied in a way that matters — a denied
#: LONGEVITY claim ("it doesn't last") is an ordinary negative-sentiment
#: assertion, not a denial that the fragrance has longevity at all.
COMPARISON_TYPES = ("SIMILAR_TO", "DUPE_OF", "BETTER_THAN")

AUDIT_SQL = f"""
SELECT c.id, c.claim_type, c.polarity, c.sentiment, c.evidence_span,
       c.raw_subject_text, c.raw_object_text, co.permalink
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 WHERE c.claim_type IN ({",".join(f"'{t}'" for t in COMPARISON_TYPES)})
 ORDER BY c.id
"""


def suspects(conn: psycopg.Connection) -> list[Row]:
    """Comparison claims whose evidence reads like a denial."""
    return [
        row
        for row in conn.execute(AUDIT_SQL)
        if DENIAL_PATTERN.search(row["evidence_span"] or "")
    ]


def audit(conn: psycopg.Connection) -> dict[str, list[Row]]:
    """Split flagged rows by whether the extractor already caught them.

    `missed` is the list that matters: evidence that reads as a denial but
    was stored as an assertion, so it is live in the graph right now.
    `caught` is the model agreeing with the regex — reassuring, not proof.
    """
    flagged = suspects(conn)
    return {
        "missed": [r for r in flagged if r["polarity"] == "ASSERTED"],
        "caught": [r for r in flagged if r["polarity"] == "DENIED"],
    }


def denied_without_flag(conn: psycopg.Connection) -> list[Row]:
    """Claims the model called denials that the regex did not flag.

    These are the interesting ones in the other direction: either the model
    found phrasing the pattern misses (good, and the pattern should learn
    it), or it is marking ordinary claims as denials (bad, and that is a
    silent recall loss no other check would catch).
    """
    flagged = {row["id"] for row in suspects(conn)}
    return [
        row
        for row in conn.execute(AUDIT_SQL)
        if row["polarity"] == "DENIED" and row["id"] not in flagged
    ]


def render(row: Row) -> str:
    return "\n".join(
        [
            f"  [{row['claim_type']}/{row['polarity']}/{row['sentiment']}] "
            f"{row['raw_subject_text']} -> {row['raw_object_text']}",
            f'      "{row["evidence_span"]}"',
            f"      {row['permalink']}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check whether the extractor is recording denials as denials."
    )
    parser.add_argument("command", choices=["audit"], nargs="?", default="audit")
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument(
        "--show-caught",
        action="store_true",
        help="Also print the denials the extractor got right",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        result = audit(conn)
        missed, caught = result["missed"], result["caught"]
        unflagged = denied_without_flag(conn)
        total = len(missed) + len(caught)

        if not total and not unflagged:
            print(
                "No comparison claims read as denials. Either the corpus "
                "holds none, or extraction has not run since polarity was "
                "added."
            )
            return 0

        print(f"{total} claim(s) whose evidence reads like a denial.\n")
        print(f"  stored as DENIED (correct):   {len(caught)}")
        print(f"  stored as ASSERTED (wrong):   {len(missed)}")
        if total:
            print(f"  caught: {len(caught) / total:.0%}\n")

        if missed:
            print("Denials still stored as assertions — these are live edges:\n")
            for row in missed:
                print(render(row))
                print()

        if unflagged:
            print(
                f"\n{len(unflagged)} claim(s) marked DENIED that the pattern "
                "did not flag. Check these by eye: either the model found "
                "phrasing worth adding to DENIAL_PATTERN, or it is denying "
                "ordinary claims.\n"
            )
            for row in unflagged:
                print(render(row))
                print()

        if args.show_caught and caught:
            print("\nCorrectly denied:\n")
            for row in caught:
                print(render(row))
                print()

        # Exit non-zero while denials are still live, so this can gate a
        # re-extraction rather than being something you remember to read.
        return 1 if missed else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
