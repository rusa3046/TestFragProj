"""Choosing *which* comments to label next.

The eval set is the blocker on every extraction change: at 13 hand-verified
train comments against a ±1 claim noise floor, no prompt edit can be
distinguished from drift. The constraint on growing it is human hours, not
corpus size — there are 4,866 comments and only so many evenings.

So the question this module answers is not "label more" but "label *what*".

`labels.export_template --sample` draws uniformly, which is right for
estimating how the extractor does on a typical comment and wrong for almost
everything else. Most fragrance comments assert nothing — measured, ~0.44
claims per comment, and the majority of that comes from a minority of
comments — so a uniform hour is mostly spent typing empty lists. The same
hour aimed at comments that *discriminate between prompt versions* buys
several times the statistical power.

## The strata, and why each earns its place

    rejected   extraction produced something the schema refused
    silent     extraction produced nothing, but the comment talks like a
               comparison — the only way to see a false negative
    edge       produced a SIMILAR_TO / DUPE_OF / BETTER_THAN claim
    control    uniform random

`silent` is the one the current eval structurally cannot see. Scoring
compares labels against extracted claims; a comment the extractor passed
over in silence contributes nothing to inspect, so its false negatives are
invisible no matter how many claims are read. The only way to count them is
to label comments the extractor said nothing about, and the only affordable
way to find candidates is to look for the words people use when comparing.

`rejected` is nearly free now that `rejected_claims` is committed: those
comments are pre-selected for being where extraction already failed.

## Why `control` is not optional

A set built only from failures measures failure. Precision on ordinary
comments — the number that says whether the extractor is usable — can only
come from a sample that was not chosen for being hard. `CONTROL_FRACTION`
keeps that stratum, and `coverage` reports the split so a later reader can
tell which numbers are estimates of the corpus and which are estimates of
the hard cases.

Nothing here writes a label. It chooses rows and says why; a person still
decides what each comment asserts.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.evals.labels import sample_rank, split_for
from fragrance_graph.models import DUPE_SIGNAL_WORDS

log = logging.getLogger("fragrance_graph.evals.sample")

REJECTED = "rejected"
SILENT = "silent"
EDGE = "edge"
CONTROL = "control"

#: Share of a batch drawn uniformly rather than targeted. Held at a third
#: because the targeted strata answer "did this change help?" while only
#: this one answers "is the extractor any good?", and a set that cannot
#: answer the second is not an eval set.
CONTROL_FRACTION = 1 / 3

#: How a comment talks when it is comparing two fragrances. Deliberately
#: wider than `DUPE_SIGNAL_WORDS`, which encodes the DUPE_OF/SIMILAR_TO
#: boundary for the prompts; this only has to be a cheap filter for "worth
#: a human's attention", so a false positive here costs one read.
COMPARISON_LANGUAGE = (
    *DUPE_SIGNAL_WORDS,
    "smells like",
    "smells just like",
    "reminds me of",
    "similar to",
    "better than",
    "instead of",
    "alternative",
    "same as",
    "vs",
)

_COMPARISON = re.compile(
    "|".join(re.escape(word) for word in COMPARISON_LANGUAGE), re.IGNORECASE
)


def talks_like_a_comparison(body: str) -> bool:
    """Whether a comment is worth checking for a missed comparison."""
    return bool(_COMPARISON.search(body or ""))


@dataclass
class Pick:
    """One comment chosen for labelling, and the reason it was chosen."""

    source: str
    source_id: str
    body: str
    stratum: str
    why: str

    @property
    def split(self) -> str:
        return split_for(self.source_id)

    def as_template_entry(self) -> dict:
        """The shape `labels import` expects, plus its own justification.

        `_stratum` and `_why` are prefixed and ignored by the importer.
        They exist so a reviewer knows why a row is in front of them —
        "the extractor said nothing about this one" changes how carefully
        it gets read.
        """
        return {
            "source": self.source,
            "source_id": self.source_id,
            "split": self.split,
            "_stratum": self.stratum,
            "_why": self.why,
            "body": self.body,
            "claims": [],
        }


SELECT_SQL = """
SELECT c.id, c.source, c.source_id, c.body, c.extracted_at,
       (SELECT count(*) FROM claims cl WHERE cl.comment_id = c.id) AS claims,
       (SELECT count(*) FROM claims cl WHERE cl.comment_id = c.id
          AND cl.claim_type IN ('SIMILAR_TO', 'DUPE_OF', 'BETTER_THAN')
       ) AS edges,
       (SELECT count(*) FROM rejected_claims r WHERE r.comment_id = c.id
       ) AS rejects
  FROM comments c
"""


def _already_labelled(conn: sqlite3.Connection, labeler: str | None) -> set[str]:
    sql = (
        "SELECT co.source_id FROM eval_labels l "
        "JOIN comments co ON co.id = l.comment_id"
    )
    params: tuple = ()
    if labeler:
        sql += " WHERE l.labeler = ?"
        params = (labeler,)
    return {row["source_id"] for row in conn.execute(sql, params)}


def select(
    conn: sqlite3.Connection,
    count: int,
    *,
    labeler: str | None = None,
    control_fraction: float = CONTROL_FRACTION,
) -> list[Pick]:
    """The next `count` comments worth a human's time, most useful first.

    Deterministic: the same corpus yields the same batch, so a labelling
    session can be stopped and resumed, and two people can be given
    disjoint halves of one plan.
    """
    skip = _already_labelled(conn, labeler)
    rows = [row for row in conn.execute(SELECT_SQL) if row["source_id"] not in skip]
    rows.sort(key=lambda r: sample_rank(r["source_id"]))

    buckets: dict[str, list[Pick]] = {REJECTED: [], SILENT: [], EDGE: [], CONTROL: []}
    for row in rows:
        body = row["body"] or ""
        if row["rejects"]:
            buckets[REJECTED].append(Pick(
                row["source"], row["source_id"], body, REJECTED,
                f"extraction refused {row['rejects']} claim(s) here",
            ))
        elif row["extracted_at"] and not row["claims"] and talks_like_a_comparison(body):
            buckets[SILENT].append(Pick(
                row["source"], row["source_id"], body, SILENT,
                "extractor found nothing, but this reads like a comparison "
                "— a false negative is invisible anywhere else",
            ))
        elif row["edges"]:
            buckets[EDGE].append(Pick(
                row["source"], row["source_id"], body, EDGE,
                f"produced {row['edges']} edge claim(s), which is the output "
                "the product actually publishes",
            ))
        else:
            buckets[CONTROL].append(Pick(
                row["source"], row["source_id"], body, CONTROL,
                "drawn uniformly, so precision on an ordinary comment stays "
                "measurable",
            ))

    control_wanted = round(count * control_fraction)
    picks = buckets[CONTROL][:control_wanted]

    # Targeted strata are interleaved rather than concatenated so a partly
    # finished batch is still balanced — labelling stops when the evening
    # does, not when a stratum is exhausted.
    targeted = [buckets[REJECTED], buckets[SILENT], buckets[EDGE]]
    cursors = [0, 0, 0]
    while len(picks) < count and any(
        cursors[i] < len(targeted[i]) for i in range(len(targeted))
    ):
        for i, bucket in enumerate(targeted):
            if len(picks) >= count:
                break
            if cursors[i] < len(bucket):
                picks.append(bucket[cursors[i]])
                cursors[i] += 1

    # Top up from control if the targeted strata ran dry.
    if len(picks) < count:
        picks += buckets[CONTROL][control_wanted:control_wanted + count - len(picks)]
    return picks[:count]


@dataclass
class Coverage:
    """What the eval set currently contains, and what it cannot yet see."""

    labelled: dict[str, int] = field(default_factory=dict)
    by_split: dict[tuple[str, str], int] = field(default_factory=dict)
    claim_types: Counter = field(default_factory=Counter)
    silent_unlabelled: int = 0
    rejected_unlabelled: int = 0
    comments: int = 0


def coverage(conn: sqlite3.Connection) -> Coverage:
    """Read the eval set's shape, including the part it is blind to."""
    out = Coverage()
    out.comments = conn.execute("SELECT count(*) FROM comments").fetchone()[0]

    for row in conn.execute(
        "SELECT l.labeler, co.source_id, l.labeled_json FROM eval_labels l "
        "JOIN comments co ON co.id = l.comment_id"
    ):
        labeler = row["labeler"]
        out.labelled[labeler] = out.labelled.get(labeler, 0) + 1
        key = (labeler, split_for(row["source_id"]))
        out.by_split[key] = out.by_split.get(key, 0) + 1
        try:
            payload = json.loads(row["labeled_json"])
        except (TypeError, ValueError):
            continue
        # Stored as {"claims": [...]}, but a bare list is accepted too so a
        # hand-written label file does not have to know which it is.
        claims = payload.get("claims", []) if isinstance(payload, dict) else payload
        for claim in claims or []:
            if isinstance(claim, dict):
                out.claim_types[claim.get("claim_type", "?")] += 1

    labelled_ids = _already_labelled(conn, None)
    for row in conn.execute(SELECT_SQL):
        if row["source_id"] in labelled_ids:
            continue
        if row["rejects"]:
            out.rejected_unlabelled += 1
        elif (
            row["extracted_at"]
            and not row["claims"]
            and talks_like_a_comparison(row["body"] or "")
        ):
            out.silent_unlabelled += 1
    return out


def render_coverage(cov: Coverage) -> str:
    lines = [f"{cov.comments} comments in the corpus.", "", "Labelled:"]
    if not cov.labelled:
        lines.append("  nothing yet")
    for labeler, n in sorted(cov.labelled.items(), key=lambda kv: -kv[1]):
        train = cov.by_split.get((labeler, "train"), 0)
        holdout = cov.by_split.get((labeler, "holdout"), 0)
        lines.append(f"  {labeler:<20} {n:>4}  ({train} train, {holdout} holdout)")

    lines += ["", "Claim types across all labels:"]
    if not cov.claim_types:
        lines.append("  none recorded")
    for claim_type, n in cov.claim_types.most_common():
        lines.append(f"  {claim_type:<24} {n:>4}")

    lines += [
        "",
        "Not yet labelled, and worth the most:",
        f"  {cov.rejected_unlabelled:>4}  comments where extraction was refused",
        f"  {cov.silent_unlabelled:>4}  comments the extractor passed over that "
        "read like comparisons",
        "",
        "The second number is the eval's blind spot: scoring compares labels "
        "to extracted claims, so a comment the extractor said nothing about "
        "contributes nothing to inspect and its false negatives cannot be "
        "counted at all.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Choose which comments to label next, and why."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="Write a labelling template, targeted")
    plan.add_argument("out", type=Path, help="Template file to write")
    plan.add_argument("-n", "--count", type=int, default=50,
                      help="Comments to include. Default: 50")
    plan.add_argument("--labeler", default=None,
                      help="Skip comments this labeler has already done")
    plan.add_argument("--control-fraction", type=float, default=CONTROL_FRACTION,
                      help="Share drawn uniformly. Default: 1/3")

    sub.add_parser("coverage", help="What the eval set contains, and cannot see")

    for p in (plan, sub.choices["coverage"]):
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "coverage":
            print(render_coverage(coverage(conn)))
            return 0

        picks = select(
            conn, args.count, labeler=args.labeler,
            control_fraction=args.control_fraction,
        )
        if not picks:
            print("Nothing left to label for this labeler.")
            return 0
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps([p.as_template_entry() for p in picks], indent=2,
                       ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        counts = Counter(p.stratum for p in picks)
        splits = Counter(p.split for p in picks)
        print(f"Wrote {len(picks)} comments to {args.out}.\n")
        for stratum in (REJECTED, SILENT, EDGE, CONTROL):
            if counts[stratum]:
                print(f"  {counts[stratum]:>4}  {stratum}")
        print(f"\n  split: {splits['train']} train, {splits['holdout']} holdout")
        print(
            "\nFill in `claims` for each. `_why` says why the row is there;\n"
            "the `silent` rows are the ones worth reading slowly, since a\n"
            "missed claim there is invisible to every other measurement.\n\n"
            f"  python -m fragrance_graph.evals.labels import {args.out} "
            "--labeler you"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
