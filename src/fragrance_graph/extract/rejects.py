"""Inspecting the claims validation refused.

Extraction drops claims that violate an invariant the JSON schema cannot
express — a NOTE_DESCRIPTOR with no descriptor, a DUPE_OF whose object is
a category. Dropping is right: the claim is not usable as written.

What was wrong was that the reasons were counted, logged once, and lost.
Measured at 21-24% of everything emitted and accounting for nearly every
false negative in the eval, that made the single largest defect in the
pipeline the one nobody could look at without paying to extract again.

Two questions this answers:

    report   which invariants are being violated, and how often
    show     which comment, and what did the model actually say

The second is the one that decides whether a rejection is the model being
wrong or the taxonomy being wrong. `DUPE_OF ... got TAG` reads like a model
error until you see the comment says "it's a dupe of those old-school
barbershop scents" — a category, which SIMILAR_TO already accepts and
DUPE_OF does not.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate

log = logging.getLogger("fragrance_graph.extract.rejects")

REPORT_SQL = """
SELECT reason, count(*) AS n,
       count(DISTINCT comment_id) AS comments
  FROM rejected_claims
 GROUP BY reason
 ORDER BY n DESC, reason
"""

SHOW_SQL = """
SELECT r.id, r.reason, r.raw_json, r.comment_id, c.body, c.permalink
  FROM rejected_claims r
  JOIN comments c ON c.id = r.comment_id
 ORDER BY r.id
"""


@dataclass
class ReasonCount:
    reason: str
    claims: int
    comments: int


def report(conn: sqlite3.Connection) -> list[ReasonCount]:
    """Rejections grouped by validator message, most frequent first."""
    return [
        ReasonCount(row["reason"], row["n"], row["comments"])
        for row in conn.execute(REPORT_SQL)
    ]


def rejected(
    conn: sqlite3.Connection, *, reason_like: str | None = None, limit: int = 20
) -> list[dict]:
    """Individual rejections with the comment that produced them.

    `reason_like` is a plain substring, not a pattern: the reasons are
    validator prose full of braces and commas, and making the caller escape
    them would be a worse tool than a slower one.
    """
    rows = []
    for row in conn.execute(SHOW_SQL):
        if reason_like and reason_like.lower() not in row["reason"].lower():
            continue
        rows.append(
            {
                "id": row["id"],
                "reason": row["reason"],
                "claim": json.loads(row["raw_json"]),
                "comment_id": row["comment_id"],
                "body": row["body"],
                "permalink": row["permalink"],
            }
        )
        if len(rows) >= limit:
            break
    return rows


def render(entry: dict) -> str:
    claim = entry["claim"]
    subject = claim.get("raw_subject_text")
    obj = claim.get("raw_object_text")
    return "\n".join(
        [
            f"comment {entry['comment_id']}: {entry['body']}",
            f"  rejected: {entry['reason']}",
            f"  emitted:  {claim.get('claim_type')} "
            f"subject={subject!r} object={obj!r} "
            f"object_kind={claim.get('object_kind')!r}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect claims that failed validation during extraction."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("report", help="Rejection reasons, most frequent first")

    show = sub.add_parser("show", help="Individual rejections, with the comment")
    show.add_argument(
        "--reason",
        default=None,
        help="Substring of the validator message, e.g. NOTE_DESCRIPTOR",
    )
    show.add_argument("--limit", type=int, default=20)

    for p in (rep, show):
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "report":
            counts = report(conn)
            if not counts:
                print(
                    "No rejected claims recorded. Either extraction has not "
                    "run since this table was added, or nothing was refused."
                )
                return 0
            total = sum(c.claims for c in counts)
            print(f"{'claims':>7}  {'comments':>8}  reason")
            for count in counts:
                print(f"{count.claims:>7}  {count.comments:>8}  {count.reason}")
            print(f"\n{total} rejected claims across {len(counts)} distinct reasons.")

        else:
            entries = rejected(conn, reason_like=args.reason, limit=args.limit)
            if not entries:
                print("No rejections match.")
                return 0
            for entry in entries:
                print()
                print(render(entry))
            print(f"\n{len(entries)} shown.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
