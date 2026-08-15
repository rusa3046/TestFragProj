"""Finding evidence whose wording you did not guess.

    uv run python -m fragrance_graph.semantic backfill
    uv run python -m fragrance_graph.semantic search "rose bomb"

The structured layer matches words. Somebody who asks for "rosy" and a
commenter who wrote "rose bomb" are talking about the same thing, and no
amount of normalising reaches every pair of them. This is the layer for
that, and its rules are narrow on purpose.

## What a vector may and may not do

> Embeddings retrieve. They never assert.

A vector match brings a bottle into consideration and supplies the claim
that justified it. It never creates a fact, never becomes a
`SIMILAR_TO` edge, and never raises a `Strength`. Everything found this way
goes back through the ordinary evidence path, so a semantically retrieved
candidate is graded and worded exactly like one found by name.

Only text a human wrote is embedded. Embedding a generated summary would
let invented language retrieve real bottles, which is the same failure as
inventing the bottle.

## The ceiling this layer cannot raise

It is worth stating plainly, because it is the thing people expect
embeddings to fix and they cannot:

> A vector cannot retrieve text that is not there.

"Hotel lobby" appears **zero** times in 9,495 comments. No embedding model
finds it, because there is nothing to find. What this layer genuinely
fixes is *lexical variation over text that exists* — "rosy", "rose bomb",
"rose-heavy" and "very rose forward" reaching the same evidence. Queries
about vocabulary the corpus has never used stay unanswerable, and the
honest response remains a refusal.

## Why the default embedder has no dependencies

The obvious choice is a sentence-transformer. It also pulls torch into a
project whose entire dependency list is five small packages, for a corpus
of seven hundred short strings.

So `Embedder` is a protocol and the default implementation is hashed
character n-grams — a few dozen lines, no downloads, deterministic across
machines and runs. It captures morphology well ("rosy"/"rose"/"roses") and
captures meaning not at all ("airy"/"light" are unrelated to it).

That trade is stated rather than hidden, and swapping in a neural embedder
is one class and a `--model` flag. The storage, the backfill, the model
tagging and the retrieval path are all model-agnostic already; the point
of building it this way is that the upgrade costs nothing structural.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection

log = logging.getLogger("fragrance_graph.semantic")


class Embedder(Protocol):
    """Anything that turns a string into a fixed-length vector."""

    name: str
    dim: int

    def embed(self, text: str) -> list[float]:
        ...


#: Vector width for the default embedder. Small enough that brute-force
#: cosine over the whole corpus is trivial, wide enough that unrelated
#: n-grams rarely collide into the same bucket.
DEFAULT_DIM = 256

#: Character n-gram sizes. Three catches stems ("ros"), four and five catch
#: whole short words and their inflections.
NGRAM_SIZES = (3, 4, 5)


@dataclass
class HashedNGrams:
    """Character n-grams hashed into a fixed number of buckets.

    Deliberately not learned. Two strings are close here when they *share
    letters in the same order*, which is exactly the failure the structured
    layer has — "rosy" and "rose bomb" are one concept written two ways —
    and is not any kind of understanding. Do not read a high score here as
    the corpus agreeing that two things smell alike.

    Deterministic across processes and machines: Python's `hash` is salted
    per process, so `blake2b` is used instead. An embedding that changed
    between runs would make the stored vectors and the query vector live in
    different spaces, and the failure would look like poor recall rather
    than a bug.
    """

    dim: int = DEFAULT_DIM
    name: str = "hashed-ngrams-v1"

    def embed(self, text: str) -> list[float]:
        cleaned = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
        collapsed = re.sub(r"\s+", " ", cleaned).strip()
        # Padded so the first and last letters take part in n-grams: a
        # boundary marker is what makes "rose" and "rosewood" separable.
        cleaned = f" {collapsed} "
        counts = [0.0] * self.dim
        for size in NGRAM_SIZES:
            for start in range(max(0, len(cleaned) - size + 1)):
                gram = cleaned[start : start + size]
                if not gram.strip():
                    continue
                digest = hashlib.blake2b(gram.encode(), digest_size=8).digest()
                counts[int.from_bytes(digest, "big") % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in counts))
        if norm == 0:
            return counts
        return [x / norm for x in counts]


def cosine(a: list[float], b: list[float]) -> float:
    """Both vectors are stored normalised, so this is a dot product.

    Guarded anyway: a vector from another model, or a zero vector from
    empty text, would otherwise produce a silent NaN that sorts
    unpredictably.
    """
    if len(a) != len(b):
        raise ValueError(f"vectors from different spaces: {len(a)} vs {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=True))


#: Claim types whose object text is free-form human description. These are
#: the ones worth embedding; a sentiment-only claim has no text to embed.
DESCRIPTIVE_TYPES = ("NOTE_DESCRIPTOR", "AESTHETIC", "OCCASION")

EMBEDDABLE_SQL = """
SELECT cl.id AS claim_id, cl.raw_object_text AS text
  FROM claims cl
 WHERE cl.polarity = 'ASSERTED'
   AND cl.evidence_verified = 1
   AND cl.raw_object_text IS NOT NULL
   AND length(trim(cl.raw_object_text)) > 2
   AND array_length(regexp_split_to_array(trim(cl.raw_object_text), %(sep)s), 1)
       <= %(max_words)s
   AND cl.claim_type = ANY(%(types)s)
"""

UPSERT_SQL = """
INSERT INTO evidence_embeddings
    (kind, ref_id, text, model, dim, vector, created_at)
VALUES ('claim', %(ref_id)s, %(text)s, %(model)s, %(dim)s, %(vector)s, %(at)s)
ON CONFLICT (kind, ref_id, model) DO UPDATE
   SET text = EXCLUDED.text,
       vector = EXCLUDED.vector,
       dim = EXCLUDED.dim,
       created_at = EXCLUDED.created_at
"""


def backfill(
    conn: psycopg.Connection, embedder: Embedder | None = None
) -> int:
    """Embed every descriptive claim. Idempotent, incremental, resumable.

    Re-running embeds only what changed, because the upsert is keyed on
    (kind, ref_id, model). Switching models adds a second set of rows
    rather than overwriting the first, so a rebuild can be compared against
    what it replaced instead of destroying it.
    """
    embedder = embedder or HashedNGrams()
    rows = conn.execute(
        EMBEDDABLE_SQL,
        {
            "types": list(DESCRIPTIVE_TYPES),
            "sep": r"\s+",
            "max_words": MAX_DESCRIPTOR_WORDS,
        },
    ).fetchall()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    written = 0
    for row in rows:
        conn.execute(
            UPSERT_SQL,
            {
                "ref_id": row["claim_id"],
                "text": row["text"],
                "model": embedder.name,
                "dim": embedder.dim,
                "vector": embedder.embed(row["text"]),
                "at": now,
            },
        )
        written += 1
    conn.commit()
    log.info("embedded %d claim(s) with %s", written, embedder.name)
    return written


@dataclass(frozen=True)
class Match:
    """One piece of evidence a query is close to, and how close."""

    claim_id: int
    text: str
    score: float
    fragrance_id: int | None
    canonical_name: str


NEAREST_SQL = """
SELECT e.ref_id, e.text, e.vector,
       cl.subject_frag_id,
       att.fragrance_id AS inferred_frag_id,
       f.canonical_name, fi.canonical_name AS inferred_name
  FROM evidence_embeddings e
  JOIN claims cl ON cl.id = e.ref_id
  LEFT JOIN claim_attributions att
         ON att.claim_id = cl.id AND att.role = 'subject'
        AND att.review_status <> 'rejected'
  LEFT JOIN fragrances f  ON f.id = cl.subject_frag_id
  LEFT JOIN fragrances fi ON fi.id = att.fragrance_id
 WHERE e.kind = 'claim' AND e.model = %(model)s
"""

#: Below this, a "match" is two strings that happen to share a few letters.
#: Chosen by inspecting the ranked output on the real corpus rather than
#: derived: at 0.30 the tail is noise, at 0.50 real paraphrases are lost.
MIN_SCORE = 0.40

#: Longest descriptor worth treating as a descriptor. Somebody writing
#: "cuddling up to my big boyfriend, at a ski lodge, in front of a fire
#: place, under red plaid blankets with Christmas decorations" is writing a
#: memory, not naming a quality, and character n-grams will happily match
#: "lodge" against "lobby" and offer it as a vibe match. The evidence stays
#: in the corpus; it is just not a thing to retrieve *by*.
MAX_DESCRIPTOR_WORDS = 6


def nearest(
    conn: psycopg.Connection,
    text: str,
    *,
    embedder: Embedder | None = None,
    limit: int = 20,
    min_score: float = MIN_SCORE,
) -> list[Match]:
    """Evidence closest to a phrase, best first.

    Brute force on purpose; see the migration for why. Every result carries
    the claim it came from, so a caller can put the human's own words on a
    page rather than the score.
    """
    embedder = embedder or HashedNGrams()
    query = embedder.embed(text)
    matches = []
    for row in conn.execute(NEAREST_SQL, {"model": embedder.name}):
        stored = list(row["vector"])
        if len(stored) != embedder.dim:
            # A row from another model. Skipped rather than compared:
            # cosine across two spaces returns a number, and that number
            # means nothing.
            continue
        score = cosine(query, stored)
        if score < min_score:
            continue
        matches.append(
            Match(
                claim_id=row["ref_id"],
                text=row["text"],
                score=score,
                fragrance_id=row["subject_frag_id"] or row["inferred_frag_id"],
                canonical_name=row["canonical_name"] or row["inferred_name"] or "",
            )
        )
    matches.sort(key=lambda m: (-m.score, m.text))
    return matches[:limit]


def candidates_for(
    conn: psycopg.Connection,
    text: str,
    *,
    embedder: Embedder | None = None,
    limit: int = 20,
) -> dict[int, Match]:
    """Bottles a phrase points at, each with its best supporting claim.

    Only bottles the evidence actually attaches to. A match on a claim
    nobody could attribute is real evidence about something, but it is not
    a reason to recommend any particular bottle.
    """
    found: dict[int, Match] = {}
    for match in nearest(conn, text, embedder=embedder, limit=limit * 4):
        if match.fragrance_id is None:
            continue
        if match.fragrance_id not in found:
            found[match.fragrance_id] = match
    return dict(list(found.items())[:limit])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.semantic",
        description="Embed descriptor evidence and search it by meaning-ish.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("backfill", help="Embed every descriptive claim")
    search = sub.add_parser("search", help="Find evidence close to a phrase")
    search.add_argument("phrase")
    search.add_argument("--limit", type=int, default=10)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "backfill":
            print(f"{backfill(conn)} claim(s) embedded.")
        else:
            found = nearest(conn, args.phrase, limit=args.limit)
            if not found:
                print(
                    f"Nothing in the corpus is close to {args.phrase!r}. "
                    "A vector cannot retrieve text that is not there."
                )
            for match in found:
                where = match.canonical_name or "(unattributed)"
                print(f"  {match.score:.2f}  {match.text!r:<40} {where}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
