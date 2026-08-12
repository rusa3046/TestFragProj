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


#: Claim types whose object kind the taxonomy already determines. For
#: these, `object_kind` restates something `claim_type` fixes — the model
#: is asked for a field with exactly one legal answer, and when it gets
#: that redundant field wrong an otherwise usable claim is destroyed.
def _determined_kinds() -> dict[str, str]:
    from fragrance_graph.models import ALLOWED_OBJECT_KINDS

    return {
        claim_type.value: next(iter(kinds)).value
        for claim_type, kinds in ALLOWED_OBJECT_KINDS.items()
        if len(kinds) == 1
    }


#: Buckets, worst-understood first. Only COERCIBLE is safe to recover
#: automatically; the rest are the model disagreeing with the taxonomy,
#: which is a judgement and belongs to a person.
COERCIBLE = "coercible: object_kind is determined by claim_type"
NO_OBJECT = "not recoverable: object required, none supplied"
DISAGREEMENT = "not recoverable: model asserted a kind the type forbids"
OTHER = "not recoverable: other"


def classify(entry: dict) -> tuple[str, str | None]:
    """Which bucket a rejection falls into, and the kind it would take.

    The verdict is not reasoned about — it is **tested**. A candidate is
    only called coercible if a `Claim` actually validates once the kind is
    corrected, using the same model the extractor uses. Anything that
    still fails for a second reason stays where it is.
    """
    from pydantic import ValidationError

    from fragrance_graph.models import Claim

    claim = dict(entry.get("claim") or {})
    claim_type = claim.get("claim_type")
    determined = _determined_kinds().get(claim_type)
    if determined is None:
        # SIMILAR_TO is the only type with a real choice to make, so a
        # wrong kind there is genuinely ambiguous, not a clerical slip.
        return OTHER, None

    has_text = bool((claim.get("raw_object_text") or "").strip())
    if determined == "NONE":
        if has_text:
            return DISAGREEMENT, None
    elif not has_text:
        # "requires raw_object_text": the claim asserts nothing about an
        # object. Coercing the kind would invent content.
        return NO_OBJECT, None

    stated = claim.get("object_kind")
    if stated == determined:
        return OTHER, None

    # Only `NONE` is coercible, and the distinction is the whole safety
    # argument. `NONE` means the model declined to fill a field the
    # taxonomy already fixes — there is nothing to overrule. Any *other*
    # kind is a positive assertion about what the object is, and
    # overriding it invents content: "BETTER_THAN got TAG" on "better than
    # most vanillas" would be rewritten into a claim that "most vanillas"
    # is a bottle, which is the fabricated-entity failure this project
    # guards against everywhere else.
    if stated != "NONE":
        return DISAGREEMENT, None

    candidate = {**claim, "object_kind": determined}
    try:
        Claim.model_validate(candidate)
    except ValidationError:
        # Broken for a second reason. The verdict is tested rather than
        # reasoned about, so this stays rejected.
        return DISAGREEMENT, None
    return COERCIBLE, determined


def recoverable(conn: sqlite3.Connection) -> dict:
    """How much of the rejected pile a deterministic fix would return.

    Answers the question that decides whether the extraction prompt needs
    touching at all: are these claims the model got *wrong*, or claims it
    got right and mislabelled in a field the schema already determines?

    Costs nothing — `rejected_claims` stores the payload verbatim, so this
    reads history rather than paying to extract again.
    """
    buckets: dict[str, list[dict]] = {}
    by_type: dict[str, int] = {}
    total = 0
    for row in conn.execute(
        "SELECT r.id, r.comment_id, r.reason, r.raw_json, c.body"
        "  FROM rejected_claims r JOIN comments c ON c.id = r.comment_id"
    ):
        try:
            claim = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            claim = {}
        entry = {
            "comment_id": row["comment_id"],
            "reason": row["reason"],
            "claim": claim,
            "body": row["body"],
        }
        bucket, kind = classify(entry)
        entry["would_become"] = kind
        buckets.setdefault(bucket, []).append(entry)
        total += 1
        if bucket == COERCIBLE:
            key = f"{claim.get('claim_type')} -> {kind}"
            by_type[key] = by_type.get(key, 0) + 1
    return {"total": total, "buckets": buckets, "coercible_by_type": by_type}


def render_recoverable(result: dict, *, examples: int = 3) -> str:
    """The report, written so the decision is readable without the code."""
    total = result["total"]
    if not total:
        return (
            "No rejected claims stored.\n\n"
            "rejected_claims is not part of the committed corpus, so a "
            "database rebuilt with `corpus import` starts empty. Run an "
            "extraction first, then re-run this."
        )
    lines = [f"{total} rejected claims stored.", ""]
    order = [COERCIBLE, NO_OBJECT, DISAGREEMENT, OTHER]
    for bucket in order:
        rows = result["buckets"].get(bucket, [])
        if not rows:
            continue
        lines.append(f"{len(rows):>5}  ({100 * len(rows) / total:4.1f}%)  {bucket}")
    lines.append("")

    coercible = result["buckets"].get(COERCIBLE, [])
    if coercible:
        lines.append("Coercible, by claim type:")
        for key, n in sorted(result["coercible_by_type"].items(),
                             key=lambda kv: -kv[1]):
            lines.append(f"  {n:>4}  {key}")
        lines.append("")
        lines.append(f"Examples (first {min(examples, len(coercible))}):")
        for entry in coercible[:examples]:
            claim = entry["claim"]
            lines.append(
                f"  {claim.get('claim_type')} "
                f"subject={claim.get('raw_subject_text')!r} "
                f"object={claim.get('raw_object_text')!r} "
                f"object_kind={claim.get('object_kind')!r} "
                f"-> {entry['would_become']}"
            )
            lines.append(f"      {entry['body'][:110]!r}")
        lines.append("")
        lines.append(
            "These validate once object_kind is set from claim_type, which "
            "the taxonomy already fixes for these types. Recovering them is "
            "a deterministic parse change, testable without an API call — "
            "it does not touch the prompt and does not need the eval set."
        )
    else:
        lines.append(
            "Nothing is coercible. The rejections are the model disagreeing "
            "with the taxonomy, which needs a person, not a parse fix."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect claims that failed validation during extraction."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rep = sub.add_parser("report", help="Rejection reasons, most frequent first")

    rec = sub.add_parser(
        "recoverable",
        help="How many rejections a deterministic object_kind fix would return",
    )
    show = sub.add_parser("show", help="Individual rejections, with the comment")
    show.add_argument(
        "--reason",
        default=None,
        help="Substring of the validator message, e.g. NOTE_DESCRIPTOR",
    )
    show.add_argument("--limit", type=int, default=20)

    for p in (rep, show, rec):
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

        elif args.command == "recoverable":
            print(render_recoverable(recoverable(conn)))

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
