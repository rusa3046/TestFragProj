"""Score extracted claims against hand-written labels.

Turns "this output looks better" into precision, recall, and F1 — numbers
that survive a prompt change and catch a regression without anyone
re-reading the rows.

Matching is on (claim_type, subject, object) after normalisation. Sentiment
is deliberately excluded from the match key and reported separately: a
claim with the right type and entities but the wrong polarity is a partial
success, and collapsing it into a miss would hide which half broke.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.evals.labels import HOLDOUT, TRAIN, load_labels
from fragrance_graph.models import normalize_for_match

log = logging.getLogger("fragrance_graph.evals.score")

#: (comment_id, claim_type, normalized subject, normalized object)
MatchKey = tuple[int, str, str, str]


def match_key(comment_id: int, claim: dict) -> MatchKey:
    return (
        comment_id,
        claim["claim_type"],
        normalize_for_match(claim.get("raw_subject_text") or ""),
        normalize_for_match(claim.get("raw_object_text") or ""),
    )


@dataclass
class Score:
    """Precision, recall, and F1 for one slice of the claims."""

    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    sentiment_agreements: int = 0

    @property
    def precision(self) -> float:
        found = self.true_positives + self.false_positives
        return self.true_positives / found if found else 0.0

    @property
    def recall(self) -> float:
        expected = self.true_positives + self.false_negatives
        return self.true_positives / expected if expected else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def sentiment_accuracy(self) -> float:
        """Share of matched claims whose polarity also agrees."""
        if not self.true_positives:
            return 0.0
        return self.sentiment_agreements / self.true_positives

    def line(self, name: str) -> str:
        return (
            f"{name:<24} P {self.precision:.2f}  R {self.recall:.2f}  "
            f"F1 {self.f1:.2f}  "
            f"(tp {self.true_positives}, fp {self.false_positives}, "
            f"fn {self.false_negatives})"
        )


@dataclass
class Report:
    overall: Score = field(default_factory=Score)
    by_type: dict[str, Score] = field(default_factory=lambda: defaultdict(Score))
    labelled_comments: int = 0

    def render(self) -> str:
        lines = [
            f"Scored against {self.labelled_comments} labelled comments.",
            self.overall.line("OVERALL"),
        ]
        if self.overall.true_positives:
            lines.append(
                f"{'sentiment agreement':<24} "
                f"{self.overall.sentiment_accuracy:.2f} of matched claims"
            )
        lines.append("")
        for name in sorted(self.by_type):
            lines.append(self.by_type[name].line(name))
        return "\n".join(lines)


def extracted_claims(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    rows = conn.execute(
        "SELECT comment_id, claim_type, raw_subject_text, raw_object_text, sentiment "
        "FROM claims"
    ).fetchall()
    out: dict[int, list[dict]] = defaultdict(list)
    for row in rows:
        out[row["comment_id"]].append(dict(row))
    return out


def score(
    extracted: dict[int, list[dict]], labels: dict[int, list[dict]]
) -> Report:
    """Compare extraction against labels, over labelled comments only.

    Comments with no label are skipped entirely rather than counted as
    having no claims — an unlabelled comment is unknown, not empty, and
    treating it as empty would punish every correct extraction on it.
    """
    report = Report(labelled_comments=len(labels))

    for comment_id, expected in labels.items():
        got = extracted.get(comment_id, [])

        expected_by_key = {match_key(comment_id, c): c for c in expected}
        got_by_key = {match_key(comment_id, c): c for c in got}

        for key, claim in got_by_key.items():
            claim_type = claim["claim_type"]
            if key in expected_by_key:
                report.overall.true_positives += 1
                report.by_type[claim_type].true_positives += 1
                if claim.get("sentiment") == expected_by_key[key].get(
                    "sentiment", "NEUTRAL"
                ):
                    report.overall.sentiment_agreements += 1
                    report.by_type[claim_type].sentiment_agreements += 1
            else:
                report.overall.false_positives += 1
                report.by_type[claim_type].false_positives += 1

        for key, claim in expected_by_key.items():
            if key not in got_by_key:
                report.overall.false_negatives += 1
                report.by_type[claim["claim_type"]].false_negatives += 1

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score extracted claims against eval labels."
    )
    parser.add_argument(
        "--split",
        choices=[TRAIN, HOLDOUT, "all"],
        default=TRAIN,
        help=(
            "Which labels to score against. Tune on train; consult holdout "
            "only to confirm a chosen prompt. Default: train"
        ),
    )
    parser.add_argument("--labeler", default=None, help="Restrict to one labeler")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        labels = load_labels(
            conn,
            labeler=args.labeler,
            split=None if args.split == "all" else args.split,
        )
        if not labels:
            raise SystemExit(
                f"No labels found for split={args.split}. "
                "Run: python -m fragrance_graph.evals.labels export labels.json"
            )
        report = score(extracted_claims(conn), labels)
    finally:
        conn.close()

    print(f"split: {args.split}")
    print(report.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
