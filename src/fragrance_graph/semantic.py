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
from dataclasses import dataclass, field
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


#: How many context words define the space for `CorpusDistributional`.
#: Wide enough that two words rarely share a profile by accident, narrow
#: enough that the whole matrix is a few hundred kilobytes of Python.
CONTEXT_WORDS = 300

#: Words either side of a target that count as its context. Five is the
#: usual choice for capturing topical rather than syntactic similarity,
#: which is what "airy is like light" needs.
CONTEXT_WINDOW = 5

#: A word has to appear this often before it gets a vector. Below it the
#: co-occurrence profile is noise, and noise that looks like meaning is
#: worse than no vector at all.
MIN_WORD_COUNT = 4

STOP = frozenset(
    "the a an and or but if it its this that these those i you he she we they"
    " is are was were be been being have has had do does did will would can"
    " could should of to in on at for with from by as not no so very really"
    " just too much more most me my your their his her our them him".split()
)


@dataclass
class CorpusDistributional:
    """Word vectors learned from how words are used *in this corpus*.

    The hashed n-gram baseline is lexical by construction: "airy" and
    "light" share no letters, so nothing can make them close. This is the
    cheapest thing that can, and the mechanism is the oldest idea in
    distributional semantics — a word is described by the company it
    keeps. If "airy" and "light" both turn up near "fresh", "soft" and
    "clean" in nine thousand comments, their profiles converge whether or
    not they share a character.

    Trained on the comment bodies rather than the extracted tags, because
    tags are three words long and context needs sentences.

    No dependency, no download, no API. It also cannot know anything the
    corpus does not demonstrate, which is the honest limit: a word used
    twice has no usable profile and is excluded rather than guessed at.

    ## Measured and rejected, 2026-08-15

    Kept for reproducibility, **not** in use. On the hand-reviewed fuzzy
    set (`evals/fuzzy`, 18 cases, k=10) it lost to the lexical baseline
    on every axis:

                                recall  precision  adversarial cases hit
        hashed-ngrams-v1         0.417      0.206                      1
        corpus-distributional    0.339      0.167                      2

    The spot-checked pairs that motivated it were real and misleading.
    "airy"/"light" rose from 0.09 to 0.34 — and "rose"/"tobacco" rose from
    0.00 to 0.51, and "airy"/"heavy" from 0.26 to 0.40. Co-occurrence
    cannot separate *used together* from *similar*, and in a corpus about
    one subject almost everything is used together. Antonyms fare worst of
    all: "is it airy or heavy" puts both words in identical contexts, so
    the model that finally connects airy to light connects it to heavy
    just as firmly.

    In the eval it retrieved **"masculine" for a query of "feminine"**,
    which is the most actively wrong thing a fragrance recommender can do.
    Adopting it on the good pairs alone would have bought worse retrieval
    and a new way to be confidently wrong.
    """

    dim: int = CONTEXT_WORDS
    name: str = "corpus-distributional-v1"
    vectors: dict = field(default_factory=dict)
    contexts: tuple = ()

    def fit(self, documents) -> CorpusDistributional:
        """Learn the space. Deterministic given the same corpus."""
        counts: dict[str, int] = {}
        tokenized = []
        for text in documents:
            words = [
                w
                for w in re.findall(r"[a-z]{3,}", (text or "").lower())
                if w not in STOP
            ]
            tokenized.append(words)
            for word in words:
                counts[word] = counts.get(word, 0) + 1

        self.contexts = tuple(
            w for w, _ in sorted(
                counts.items(), key=lambda kv: (-kv[1], kv[0])
            )[:self.dim]
        )
        index = {w: i for i, w in enumerate(self.contexts)}

        co: dict[str, list[float]] = {}
        for words in tokenized:
            for position, target in enumerate(words):
                if counts[target] < MIN_WORD_COUNT:
                    continue
                row = co.setdefault(target, [0.0] * self.dim)
                lo = max(0, position - CONTEXT_WINDOW)
                hi = min(len(words), position + CONTEXT_WINDOW + 1)
                for other in words[lo:position] + words[position + 1 : hi]:
                    slot = index.get(other)
                    if slot is not None:
                        row[slot] += 1.0

        # Sublinear weighting, then L2. Raw counts let one very common
        # context word dominate every profile, which makes everything
        # look similar to everything — the failure mode that produces
        # confident nonsense rather than no answer.
        self.vectors = {}
        for word, row in co.items():
            weighted = [math.log1p(x) for x in row]
            norm = math.sqrt(sum(x * x for x in weighted))
            if norm > 0:
                self.vectors[word] = [x / norm for x in weighted]
        log.info(
            "%s: %d word vectors over %d contexts",
            self.name, len(self.vectors), len(self.contexts),
        )
        return self

    def embed(self, text: str) -> list[float]:
        words = [
            w
            for w in re.findall(r"[a-z]{3,}", (text or "").lower())
            if w not in STOP
        ]
        total = [0.0] * self.dim
        found = 0
        for word in words:
            vector = self.vectors.get(word)
            if vector is None:
                continue
            found += 1
            for i, value in enumerate(vector):
                total[i] += value
        if not found:
            return total
        norm = math.sqrt(sum(x * x for x in total))
        return [x / norm for x in total] if norm else total


@dataclass
class OpenAIEmbeddings:
    """A genuinely semantic embedder, through the same protocol.

    `text-embedding-3-small` at $0.02 per million tokens: embedding this
    corpus costs a fraction of a cent. It is nonetheless a **paid model
    call**, so it charges the same ledger as extraction — a cheap paid path
    that skips the cap is exactly how the cap has been defeated three
    times already.

    Not the default. It is measured against the free alternatives first,
    and adopted only if it earns the dependency and the spend.
    """

    api_key: str = ""
    model: str = "text-embedding-3-small"
    dim: int = 1536
    #: **Required.** Not optional, because an optional spend hook is a
    #: spend hook somebody forgets: `OpenAIEmbeddings(api_key=k).embed(x)`
    #: used to make a real paid call and record nothing. That is the
    #: fourth escape of this exact class — after a path-resolution one, a
    #: record-without-raise one, and an eval module nobody checked — and
    #: the pattern is always that the cap held everywhere it was applied.
    on_spend: object = None

    @property
    def name(self) -> str:
        return f"openai:{self.model}"

    def __post_init__(self) -> None:
        if self.on_spend is None:
            raise ValueError(
                "OpenAIEmbeddings needs an on_spend callback: every paid "
                "model call charges the daily ledger. Pass "
                "budget.guard('embeddings')."
            )

    def embed(self, text: str) -> list[float]:
        import json as _json
        import urllib.request

        key = self.api_key or __import__("os").environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set; this embedder cannot run."
            )
        payload = _json.dumps(
            {"model": self.model, "input": text or " "}
        ).encode()
        request = urllib.request.Request(
            "https://api.openai.com/v1/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            body = _json.loads(response.read())
        # Charged from the provider's own usage figure rather than an
        # estimate. The caller's projection is a pre-flight check; this is
        # what actually happened.
        tokens = body.get("usage", {}).get("total_tokens", 0)
        self.on_spend(tokens / 1_000_000 * 0.02, 1)
        return body["data"][0]["embedding"]


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
    # Rows whose claim stopped qualifying are deleted rather than left to
    # rot. `nearest` filters them too — belt and braces, because a stale
    # vector that survives a corpus rebuild is invisible until it produces
    # a wrong answer.
    removed = conn.execute(
        """
        DELETE FROM evidence_embeddings e
         WHERE e.kind = 'claim' AND e.model = %(model)s
           AND NOT EXISTS (
                 SELECT 1 FROM claims cl
                  WHERE cl.id = e.ref_id
                    AND cl.polarity = 'ASSERTED'
                    AND cl.evidence_verified = 1
                    AND cl.claim_type = ANY(%(types)s)
                    -- The same length cap the insert applies. Without it a
                    -- database updated incrementally kept 18 rows a clean
                    -- rebuild does not produce, so the two disagreed about
                    -- what was retrievable — a reproducibility failure that
                    -- only shows up when somebody rebuilds.
                    AND array_length(
                          regexp_split_to_array(trim(cl.raw_object_text), %(sep)s), 1
                        ) <= %(max_words)s
               )
        """,
        {
            "model": embedder.name,
            "types": list(DESCRIPTIVE_TYPES),
            "sep": r"\s+",
            "max_words": MAX_DESCRIPTOR_WORDS,
        },
    ).rowcount
    conn.commit()
    log.info(
        "embedded %d claim(s) with %s; removed %d stale",
        written, embedder.name, removed,
    )
    return written


@dataclass(frozen=True)
class Match:
    """One piece of evidence a query is close to, and how close."""

    claim_id: int
    text: str
    score: float
    fragrance_id: int | None
    canonical_name: str
    #: True when the bottle was worked out by machine rather than named by
    #: the commenter. Carried so the sentence built from this match can say
    #: so — the same rule the evidence layer holds, in a second place that
    #: reaches a reader.
    inferred: bool = False


#: Re-applied at *read* time, not only at embed time. An embedding row
#: outlives the claim's eligibility: correct an extraction to DENIED, or
#: fail its evidence check, and `backfill` will stop refreshing the row but
#: never deletes it — so a denial came back as "someone described it as
#: 'roses'". The stored text is likewise ignored in favour of the claim's
#: current wording, so an edited claim cannot keep quoting words nobody
#: wrote any more.
NEAREST_SQL = """
SELECT e.ref_id, cl.raw_object_text AS text, e.vector,
       cl.subject_frag_id,
       att.review_status AS attribution_status,
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
   AND cl.polarity = 'ASSERTED'
   AND cl.evidence_verified = 1
   AND cl.raw_object_text IS NOT NULL
   AND cl.claim_type = ANY(%(types)s)
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
    for row in conn.execute(
        NEAREST_SQL,
        {"model": embedder.name, "types": list(DESCRIPTIVE_TYPES)},
    ):
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
                inferred=row["subject_frag_id"] is None,
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
