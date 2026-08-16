"""Re-checking stored evidence against the comments it was quoted from.

`evidence_verified` is decided once, at write time, by whatever matching
rules were in force that day. When those rules change, every row written
before the change carries a stale verdict — and because the flag gates
what the product is allowed to show a reader, a stale 0 silently withholds
real evidence.

This module recomputes the flag for stored claims. It is pure
re-measurement: it re-reads `claims.evidence_span` against
`comments.body`, and touches nothing else. No API call, no cost.

The reason it exists: `normalize_for_match` originally folded whitespace
and case but not typographic punctuation. Phone keyboards write "don't"
with a curly apostrophe; the model quoted it back with an ASCII one. Those
claims were verbatim quotes recorded as paraphrases.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.models import (
    GROUNDED_TYPES,
    evidence_is_quoted,
    value_is_grounded,
)

log = logging.getLogger("fragrance_graph.extract.verify")

SELECT_SQL = """
SELECT c.id, c.evidence_span, c.evidence_verified, c.raw_object_text,
       c.claim_type,
       co.body
  FROM claims c
  JOIN comments co ON co.id = c.comment_id
 ORDER BY c.id
"""


@dataclass
class VerifyStats:
    total: int = 0
    verified_before: int = 0
    verified_after: int = 0
    promoted: int = 0
    demoted: int = 0

    @property
    def rate_before(self) -> float:
        return self.verified_before / self.total if self.total else 0.0

    @property
    def rate_after(self) -> float:
        return self.verified_after / self.total if self.total else 0.0

    def __str__(self) -> str:
        if not self.total:
            return "No claims to verify."
        return (
            f"{self.total} claims | "
            f"verified {self.verified_before} -> {self.verified_after} "
            f"({self.rate_before:.1%} -> {self.rate_after:.1%}) | "
            f"{self.promoted} promoted, {self.demoted} demoted"
        )


def reverify(conn: psycopg.Connection, *, dry_run: bool = False) -> VerifyStats:
    """Recompute `evidence_verified` for every stored claim.

    Reports promotions and demotions separately. A demotion is the
    interesting direction — it means a rule got stricter and something
    previously shown as quoted no longer qualifies.
    """
    stats = VerifyStats()
    updates: list[tuple[int, int]] = []

    for row in conn.execute(SELECT_SQL):
        stats.total += 1
        before = bool(row["evidence_verified"])
        # Both halves, matching what `write_claims` now stores: the
        # quotation has to be real *and* the value has to trace back to
        # the comment. Checking only the quotation left this blind to the
        # defect it exists to catch — a verbatim span with a descriptor
        # the commenter never wrote — and reported 0 demotions against
        # rows that carry exactly that.
        after = evidence_is_quoted(row["evidence_span"], row["body"]) and (
            row["claim_type"] not in GROUNDED_TYPES
            or value_is_grounded(row["raw_object_text"], row["body"])
        )

        stats.verified_before += before
        stats.verified_after += after

        if before == after:
            continue
        if after:
            stats.promoted += 1
        else:
            stats.demoted += 1
            log.debug("Demoted claim %s: %r", row["id"], row["evidence_span"])
        updates.append((int(after), row["id"]))

    if updates and not dry_run:
        conn.cursor().executemany(
            "UPDATE claims SET evidence_verified = %s WHERE id = %s", updates
        )
        conn.commit()

    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-check stored evidence spans against their comment bodies."
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report without writing changes"
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        stats = reverify(conn, dry_run=args.dry_run)
    finally:
        conn.close()

    print(("Would update: " if args.dry_run else "") + str(stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
