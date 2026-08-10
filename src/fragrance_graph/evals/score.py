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

    @property
    def is_vacuous(self) -> bool:
        """Nothing was claimed by either side, so there is nothing to score.

        Distinct from a score of zero. Most comments assert nothing, so
        agreeing that a comment is empty is the commonest correct outcome —
        and reporting it as `P 0.00 R 0.00 F1 0.00` reads as total failure.
        """
        return not (
            self.true_positives or self.false_positives or self.false_negatives
        )

    def line(self, name: str) -> str:
        if self.is_vacuous:
            return f"{name:<24} no claims on either side — nothing to score"
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


#: Claim types that produce a similarity edge. BETTER_THAN is deliberately
#: absent: it is a preference claim, and the product keeps it separate.
SIMILARITY_TYPES = frozenset({"DUPE_OF", "SIMILAR_TO"})

#: The pseudo-type both collapse to in the edge view.
SIMILARITY_EDGE = "SIMILARITY_EDGE"


def collapse_similarity(
    by_comment: dict[int, list[dict]],
) -> dict[int, list[dict]]:
    """Rewrite DUPE_OF and SIMILAR_TO to one type, leaving the rest alone.

    The per-type score punishes a disagreement the product does not care
    about. Observed on the first calibration run:

        "Sand+Fog Sweet Rose is a more wearable interpretation of DE"
        human: DUPE_OF          drafter: SIMILAR_TO

    Same subject, same object, same sentiment — one assertion, typed two
    ways. Both are edge types, so the graph is identical either way, yet
    the per-type view records it as a miss *and* an invention.

    This view answers the question the product actually asks: was the edge
    found at all. Reported alongside the per-type score, never instead of
    it — the distinction may still matter later, and hiding it would be
    how a taxonomy quietly stops being measured.
    """
    return {
        comment_id: [
            dict(claim, claim_type=SIMILARITY_EDGE)
            if claim.get("claim_type") in SIMILARITY_TYPES
            else claim
            for claim in claims
        ]
        for comment_id, claims in by_comment.items()
    }


def edge_score(
    extracted: dict[int, list[dict]], labels: dict[int, list[dict]]
) -> Score:
    """Score similarity edges with DUPE_OF and SIMILAR_TO treated as one."""
    collapsed = score(collapse_similarity(extracted), collapse_similarity(labels))
    return collapsed.by_type.get(SIMILARITY_EDGE, Score())


def extracted_claims(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    """Claims to score, which excludes denials.

    `docs/LABELLING.md` tells a human to drop a denial entirely — "khamrah
    isn't a clone" is not a claim that khamrah is a clone. So a denial the
    extractor correctly marks DENIED must not be compared against labels
    either; scoring it would make getting it right look like a false
    positive, and the fix would show up as a regression.

    This is the one place polarity changes a measurement rather than a
    result. Whether denials should eventually be labelled and scored in
    their own right is an open question — see SPEC.
    """
    rows = conn.execute(
        "SELECT comment_id, claim_type, raw_subject_text, raw_object_text, sentiment "
        "FROM claims WHERE polarity = 'ASSERTED'"
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
        extracted = extracted_claims(conn)
        report = score(extracted, labels)
        edges = edge_score(extracted, labels)
    finally:
        conn.close()

    print(f"split: {args.split}")
    print(report.render())
    print()
    print(edges.line("SIMILARITY EDGES"))
    print("  DUPE_OF and SIMILAR_TO collapsed — both build the same edge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
