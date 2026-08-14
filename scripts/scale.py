"""Inflate the corpus into a database big enough to have query plans.

    createdb fragrance_graph_scale
    python -m scripts.scale load --comments 10000000

The real corpus is 6,077 comments. At that size Postgres sequentially
scans everything and is right to: every plan is the same plan, autovacuum
never has anything to do, and no index changes a timing. You cannot learn
the operational side of a database from a table that fits in one page of
cache.

So this makes a big one out of the small one. Every synthetic comment is a
real comment's text re-attached to a new author, video and timestamp, so
the *distributions* stay plausible — the same skew of long and short
bodies, the same handful of claim types — while the row count goes up by
four orders of magnitude.

## It cannot touch the corpus, by construction

Synthetic rows are fabricated. This project's entire claim is that no row
is, so a fabricated row reaching `data/corpus/` or a published page would
be the worst defect it could have.

Three guards, in order of how hard they are to bypass:

1. **It refuses any database not named `*_scale`.** Not a flag with a
   default, not a confirmation prompt — a hard stop on the connection
   string. There is no argument that makes this write to
   `fragrance_graph`.
2. **`corpus export` refuses a `*_scale` database.** The export is the
   only path from a database into the committed corpus, and it now checks
   the name it is reading from before it writes a line.
3. **Synthetic source ids are prefixed `synthetic-`.** So even a row that
   somehow escaped is identifiable by eye and by query, rather than
   sitting in the corpus indistinguishable from a real comment.

## Why COPY rather than INSERT

Ten million INSERTs is an afternoon; ten million rows through COPY is a
couple of minutes. It is also the tool you would actually reach for in
production, and the difference between the two is itself worth seeing
once.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate

#: A database this tool will write to. Enforced on the connection string,
#: because "be careful which database you point it at" is not a guard.
SCALE_SUFFIX = "_scale"

#: Prefixes every fabricated `source_id`, so a synthetic row is
#: identifiable by eye in a corpus file and by `LIKE` in a query.
SYNTHETIC_PREFIX = "synthetic-"

#: Distinct authors and videos in the generated set. Deliberately not one
#: per comment: the counts that matter here are *distinct people* and
#: *distinct creators*, and a corpus where every comment is a new person
#: makes every de-duplicating query trivially fast for the wrong reason.
AUTHORS = 250_000
VIDEOS = 40_000
CHANNELS = 8_000


def database_name(dsn: str) -> str:
    """The database a DSN points at, whatever form the DSN takes."""
    return psycopg.conninfo.conninfo_to_dict(dsn).get("dbname", "")


def is_scale_database(dsn: str) -> bool:
    return database_name(dsn).endswith(SCALE_SUFFIX)


class RefusedTarget(Exception):
    """Raised when asked to fabricate rows outside a scale database."""


def _templates(source: psycopg.Connection) -> list[tuple[str, str, str]]:
    """Real comment bodies and evidence spans, to resample from.

    Returned as tuples, explicitly. Rows here are dict-like, and
    unpacking a dict yields its **keys** — so `body, span, claim_type =
    row` silently bound the literal strings "body", "evidence_span" and
    "claim_type", and every one of five million synthetic comments had a
    four-character body. Nothing raised; the load looked fine; the whole
    point of resampling real text is the length distribution, and it was
    gone. `test_bodies_come_from_real_comments` is what caught it.
    """
    rows = source.execute(
        "SELECT co.body, c.evidence_span, c.claim_type"
        "  FROM claims c JOIN comments co ON co.id = c.comment_id"
        " WHERE c.evidence_verified = 1"
    ).fetchall()
    if not rows:
        raise RefusedTarget(
            "The source database has no verified claims to resample. "
            "Run `corpus import` against your working database first."
        )
    return [(r["body"], r["evidence_span"], r["claim_type"]) for r in rows]


def load(
    conn: psycopg.Connection,
    source: psycopg.Connection,
    *,
    comments: int,
    claims_per_comment: float = 0.4,
    seed: int = 0,
    batch: int = 100_000,
) -> tuple[int, int]:
    """Fabricate `comments` rows and their claims. Returns what it wrote."""
    if not is_scale_database(conn.info.dsn):
        raise RefusedTarget(
            f"{database_name(conn.info.dsn)!r} is not a scale database. "
            f"This writes fabricated rows and will only do it to a database "
            f"whose name ends in {SCALE_SUFFIX!r}."
        )

    rng = random.Random(seed)
    templates = _templates(source)
    now = int(datetime.now(UTC).timestamp())

    # Numbering continues from whatever is already there, so a second run
    # grows the table rather than colliding on `(source, source_id)`.
    # Growing a table in stages is the normal way to find the size at
    # which a plan changes, and a loader that only worked once would make
    # that a drop-and-reload each time.
    start = conn.execute(
        "SELECT count(*) FROM comments WHERE source_id LIKE %s",
        (f"{SYNTHETIC_PREFIX}%",),
    ).fetchone()[0]

    written = 0
    with conn.cursor() as cur:
        with cur.copy(
            "COPY comments (source, source_id, body, permalink, created_utc,"
            " source_channel, score, author_id, video_id, raw_json)"
            " FROM STDIN"
        ) as copy:
            for i in range(comments):
                body, _span, _type = templates[rng.randrange(len(templates))]
                video = f"vid{rng.randrange(VIDEOS):06d}"
                copy.write_row(
                    (
                        "youtube",
                        f"{SYNTHETIC_PREFIX}{start + i:012d}",
                        body,
                        f"https://example.invalid/c/{i}",
                        now - rng.randrange(3 * 365 * 86400),
                        f"UC{rng.randrange(CHANNELS):05d}",
                        rng.randrange(200),
                        f"author{rng.randrange(AUTHORS):07d}",
                        video,
                        "{}",
                    )
                )
                written += 1
                if written % batch == 0:
                    print(f"  {written:,} comments", file=sys.stderr)
    conn.commit()

    # Claims reference comments, so they are generated in a second pass
    # over ids the database has already assigned.
    claim_rows = 0
    with conn.cursor() as cur:
        # Only the ids this call wrote. Selecting every synthetic row
        # would give the rows from earlier runs a second set of claims.
        ids = conn.execute(
            "SELECT id FROM comments WHERE source_id >= %s AND source_id LIKE %s",
            (f"{SYNTHETIC_PREFIX}{start:012d}", f"{SYNTHETIC_PREFIX}%"),
        ).fetchall()
        with cur.copy(
            "COPY claims (comment_id, claim_type, subject_kind,"
            " raw_subject_text, object_kind, raw_object_text, sentiment,"
            " confidence, evidence_span, evidence_verified, polarity,"
            " extraction_model, created_at) FROM STDIN"
        ) as copy:
            for row in ids:
                if rng.random() > claims_per_comment:
                    continue
                body, span, claim_type = templates[rng.randrange(len(templates))]
                copy.write_row(
                    (
                        row["id"],
                        claim_type,
                        "FRAGRANCE",
                        f"mention{rng.randrange(5000):05d}",
                        "FRAGRANCE",
                        f"mention{rng.randrange(5000):05d}",
                        "NEUTRAL",
                        0.9,
                        span,
                        1,
                        "ASSERTED",
                        "synthetic",
                        "2026-01-01",
                    )
                )
                claim_rows += 1
    conn.commit()
    return written, claim_rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    ld = sub.add_parser("load", help="Fabricate rows into a *_scale database")
    ld.add_argument("--comments", type=int, default=1_000_000)
    ld.add_argument("--claims-per-comment", type=float, default=0.4)
    ld.add_argument("--seed", type=int, default=0)
    ld.add_argument(
        "--scale-url",
        default=DEFAULT_DB_URL + SCALE_SUFFIX,
        help="The scale database. Must be named *_scale.",
    )
    ld.add_argument(
        "--source-url", default=DEFAULT_DB_URL,
        help="The real database to resample bodies and spans from.",
    )

    args = parser.parse_args(argv)
    if args.command != "load":  # pragma: no cover - argparse enforces
        return 2

    try:
        conn = get_connection(args.scale_url)
    except psycopg.OperationalError as exc:
        print(f"Cannot reach {args.scale_url}: {exc}")
        print(f"Create it first:  createdb {database_name(args.scale_url)}")
        return 1

    migrate(conn)
    source = get_connection(args.source_url)
    try:
        comments, claims = load(
            conn, source,
            comments=args.comments,
            claims_per_comment=args.claims_per_comment,
            seed=args.seed,
        )
    except RefusedTarget as exc:
        print(f"Refusing: {exc}")
        return 1
    finally:
        source.close()
        conn.close()

    print(f"Wrote {comments:,} synthetic comments and {claims:,} claims.")
    print("Nothing here may be exported: `corpus export` refuses a scale database.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
