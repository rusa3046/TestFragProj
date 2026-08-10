"""Claim extraction: batch comments, call Claude, write validated claims.

Design notes:

- **Batching.** One API call covers many comments. The schema prompt is the
  fixed cost per call, so batching amortises it. Results are mapped back by
  index, not by echoing comment text (which would double the token spend).

- **Structured outputs.** The response is constrained to a JSON schema, so
  the model cannot return prose or a malformed object. Parsing is still
  defensive: refusals, truncation, and transport errors all still happen.

- **A failed batch never kills the run.** Comments in a failed batch keep
  `extracted_at = NULL` and are retried on the next run, exactly like an
  interrupted ingest.

- **`extracted_at` is set for every comment the model actually saw**,
  including ones that yielded zero claims. Most comments assert nothing;
  that is a real result, not a failure, and re-extracting it would be
  paying twice for the same answer.

- **Evidence is verified before write.** A claim whose `evidence_span` is
  not quoted from the comment body is stored with `evidence_verified = 0`
  rather than dropped, so the paraphrase rate stays measurable.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.models import (
    Claim,
    ClaimType,
    ObjectKind,
    Sentiment,
    SubjectKind,
)

log = logging.getLogger("fragrance_graph.extract.llm")

MODEL = "claude-haiku-4-5"

#: Claude Haiku 4.5 list pricing, USD per million tokens.
PRICE_INPUT_PER_MTOK = 1.00
PRICE_OUTPUT_PER_MTOK = 5.00

#: Comments per API call. Larger batches amortise the schema prompt but
#: raise the cost of a single failed call.
DEFAULT_BATCH_SIZE = 20

DEFAULT_MAX_TOKENS = 8000

#: Extraction is a measurement, so run-to-run variance is a defect, not a
#: feature. Identical input returned 4 claims on one run and 8 on the next
#: at the default sampling temperature, which makes prompt changes
#: impossible to evaluate. Sampling parameters are accepted on Haiku 4.5;
#: they are rejected with a 400 on Opus 4.7+, Sonnet 5, and Fable 5, so
#: this must be dropped if the model is ever changed to one of those.
TEMPERATURE = 0.0


# --------------------------------------------------------------------------
# Prompt
#
# The "Distinguishing the performance types" section exists because v1
# collapsed four distinct claims into LONGEVITY_COMPLAINT: anything about
# something going away landed there. Each bullet is a misclassification
# observed on real comments, quoted from the comment that produced it.
# Keep them concrete — the failures were lexical, not conceptual.
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract structured claims about fragrances from Reddit comments.

You will receive a numbered list of comments. For each one, return every
claim it asserts. Most comments assert nothing — returning an empty list
for a comment is correct and expected. Do not invent claims to fill space.

## Claim types

Comparisons between two things:

- SIMILAR_TO: the subject smells like, evokes, or resembles the object.
  The object may be another fragrance ("smells just like Baccarat 540"),
  a house ("a bit of a Serge Lutens vibe"), or a material or thing
  ("smells remarkably similar to cocoa pyrazine", "smells like a bakery").
- DUPE_OF: the subject is a cheaper stand-in for the object. Fragrance
  discussion signals this with "dupe", "clone", "impression of",
  "interpretation of", "take on", or "inspired by". The price gap is
  carried by the brands and is almost never stated out loud, so do not
  require the comment to mention cost. Use SIMILAR_TO instead when the
  comment reports a resemblance without implying substitution — "smells
  like", "reminds me of", "gives me X vibes".
- BETTER_THAN: the subject is preferred over the object.

Descriptions of one fragrance:

- NOTE_DESCRIPTOR: what it smells of. One claim per descriptor.
  ("it's soapy and citrusy" is two claims: soapy, and citrusy)
- OCCASION: a setting it suits. ("great for weddings", "for the office")
- AESTHETIC: a style or vibe it evokes. ("very old money")

Performance — how it behaves on skin. These take no object, and the
sentiment field carries whether the commenter is praising or complaining:

- LONGEVITY: how long it lasts.
  ("disappears after 15 minutes" NEGATIVE;
   "it is now Friday and it's still lingering" POSITIVE)
- PROJECTION: how far it throws — sillage, projection, strength.
  ("mostly skin scents with a low projection" NEGATIVE)
- DEVELOPMENT: how it evolves over the wear. Not about lasting or
  throwing. ("it never really develops" NEGATIVE)

Product-level:

- REFORMULATION: a version differs from an earlier one. No object.
  ("much of the original's elegant woodiness has disappeared")
- UNMET_PRODUCT_REQUEST: the commenter wants a product form that does not
  exist. Object is the form. ("wish it came in a body lotion")

## Distinguishing the performance types

These are the ones most often confused. A word about something going away
does not make a claim about longevity:

- "it never really develops" — DEVELOPMENT, not LONGEVITY. The scent is
  present the whole time; it just doesn't change.
- "the woodiness has disappeared" (comparing versions) — REFORMULATION,
  not LONGEVITY. A note was removed from the composition, not lost on skin.
- "low projection" — PROJECTION, not LONGEVITY. How far, not how long.
- "still lingering on my clothes, delightful" — LONGEVITY with sentiment
  POSITIVE. Praise is a claim, not an absence of one.

## Kinds

- subject_kind: FRAGRANCE for a named fragrance, HOUSE for a brand or
  perfumer, CATEGORY for a class ("skin scents", "80s perfumes").
- object_kind: FRAGRANCE, HOUSE, TAG for anything else, NONE for none.
  Notes, occasions, vibes, materials, and product forms are all TAG.

## Rules

- One subject per claim. If a sentence covers several fragrances, emit one
  claim each. Never put several names in one raw_subject_text.
- raw_subject_text and raw_object_text are the commenter's own words. Do
  not normalise, correct, or expand names. "BR540" stays "BR540".
- evidence_span MUST be copied verbatim from the comment body. Do not
  paraphrase, summarise, or reconstruct it. If you cannot quote the
  comment exactly, do not make the claim.
- confidence reflects how clearly the comment asserts the claim, not how
  true you think it is. Hedged language ("kinda similar I guess") is low
  confidence; a flat assertion is high.
- If a claim would need an object and there is no identifiable one, omit
  the claim entirely rather than emitting an empty string.
"""

#: Response schema. Claims are nested under a comment index so results map
#: back without the model echoing comment text.
#:
#: `raw_object_text` stays nullable on every claim type, deliberately. It was
#: once split into two `anyOf` variants — objectless types getting
#: `{"type": "null"}` — to make the objectless NOTE_DESCRIPTOR unrepresentable.
#: It worked mechanically and still made the extractor worse; SPEC.md records
#: the numbers. A claim the model cannot state badly is not a claim it
#: suddenly knows; the schema just stops recording that it guessed.
RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "comment_index": {"type": "integer"},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim_type": {
                                    "type": "string",
                                    "enum": [t.value for t in ClaimType],
                                },
                                "subject_kind": {
                                    "type": "string",
                                    "enum": [k.value for k in SubjectKind],
                                },
                                # Structured outputs support neither string
                                # constraints (minLength) nor numeric ones
                                # (minimum/maximum) — adding them is rejected
                                # and every batch fails. Empty and
                                # out-of-range values are caught by the
                                # Pydantic contract in parse_response
                                # instead, which is where they belong.
                                "raw_subject_text": {"type": "string"},
                                "object_kind": {
                                    "type": "string",
                                    "enum": [k.value for k in ObjectKind],
                                },
                                "raw_object_text": {"type": ["string", "null"]},
                                "sentiment": {
                                    "type": "string",
                                    "enum": [s.value for s in Sentiment],
                                },
                                "confidence": {"type": "number"},
                                "evidence_span": {"type": "string"},
                            },
                            "required": [
                                "claim_type",
                                "subject_kind",
                                "raw_subject_text",
                                "object_kind",
                                "raw_object_text",
                                "sentiment",
                                "confidence",
                                "evidence_span",
                            ],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["comment_index", "claims"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def render_batch(comments: Sequence[sqlite3.Row]) -> str:
    """Render comments as a numbered list for the model."""
    return "\n\n".join(
        f"[{i}] {row['body']}" for i, row in enumerate(comments)
    )


# --------------------------------------------------------------------------
# Cost accounting
# --------------------------------------------------------------------------


@dataclass
class CostTracker:
    """Running token and dollar totals for an extraction run."""

    input_tokens: int = 0
    output_tokens: int = 0
    comments: int = 0
    batches: int = 0
    failed_batches: int = 0

    def record(self, input_tokens: int, output_tokens: int, comments: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.comments += comments
        self.batches += 1

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
            + self.output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK
        )

    @property
    def cost_per_1k_comments(self) -> float:
        """The number the spec asks to surface. 0.0 before any comment lands."""
        if not self.comments:
            return 0.0
        return self.cost_usd / self.comments * 1000

    def summary(self) -> str:
        return (
            f"{self.comments} comments in {self.batches} batches "
            f"({self.failed_batches} failed) | "
            f"{self.input_tokens} in / {self.output_tokens} out tokens | "
            f"${self.cost_usd:.4f} total | "
            f"${self.cost_per_1k_comments:.4f} per 1k comments"
        )


# --------------------------------------------------------------------------
# Cost estimation (no API call)
#
# The point of this path is to answer "what will this run cost?" before
# spending anything, on a machine that may hold no API key at all. That
# rules out `client.messages.count_tokens`, which is free but still a
# network call against a credentialed endpoint.
#
# What is left is arithmetic over character counts, which is an estimate
# and is labelled as one everywhere it surfaces. Input is the solid half:
# the exact text sent is known. Output is a genuine assumption, because it
# depends on how many claims the comments turn out to assert.
# --------------------------------------------------------------------------

#: Characters per token for English prose. A rule of thumb, not a
#: tokenizer — expect a few percent of error, more on comments dense with
#: emoji, CJK, or brand names that fragment into many tokens.
CHARS_PER_TOKEN = 4.0

#: Per-call input tokens that are not the prompt or the comments: message
#: envelope, role markers, and the structured-output plumbing around the
#: schema. Small and roughly constant.
PER_CALL_OVERHEAD_TOKENS = 40

#: Output tokens per comment, averaged over comments that assert nothing
#: and comments that assert several claims.
#:
#: **Measured**, from the first full run over the YouTube corpus (see
#: MEASURED_RUNS below). The previous value of 160 was derived from
#: SPEC.md's Reddit-era cost figure and overestimated by 3.2x — Reddit
#: review posts are long and assert several claims each, while YouTube
#: comments are short and most assert nothing.
#:
#: This is per-corpus, not universal. Recalibrate from a real run before
#: trusting it on a new source.
DEFAULT_OUTPUT_TOKENS_PER_COMMENT = 50

#: Real runs, for calibrating the constants above and for sanity-checking
#: an estimate that looks surprising. Add a row after any full run.
#:
#: youtube-2026-08-09: 3155 comments, 158 batches of 20, 0 failed
#:   input   360,406 tokens  (114.2/comment; 53% of it the fixed prompt)
#:   output  158,636 tokens  (50.3/comment)
#:   claims  1,391           (0.441/comment)
#:   cost    $1.1536         ($0.3656 per 1k comments)
#:
#: The striking part is that split. YouTube comments average ~54 tokens of
#: actual text, while the system prompt plus JSON schema costs ~1,206
#: tokens on every call — so at a batch size of 20, more than half the
#: input bill is the prompt being re-sent. Output is still 69% of the total
#: bill, so claim volume remains the thing that moves cost most.
MEASURED_RUNS = "youtube-2026-08-09: $0.3656/1k comments, 0.441 claims/comment"


def estimate_tokens(text: str) -> int:
    """Approximate token count for a string. See CHARS_PER_TOKEN."""
    return int(len(text) / CHARS_PER_TOKEN)


@dataclass(frozen=True)
class CostEstimate:
    """A projected run cost. Every field is an estimate, not a measurement."""

    comments: int
    batches: int
    input_tokens: int
    output_tokens: int
    output_tokens_per_comment: int
    model: str

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
            + self.output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK
        )

    @property
    def cost_per_1k_comments(self) -> float:
        if not self.comments:
            return 0.0
        return self.cost_usd / self.comments * 1000

    def render(self) -> str:
        if not self.comments:
            return "No comments pending extraction. Nothing to estimate."
        return "\n".join(
            [
                f"ESTIMATE — no API call was made. Model: {self.model}",
                "",
                f"  comments pending        {self.comments}",
                f"  batches                 {self.batches}",
                f"  input tokens  (est.)    {self.input_tokens:,}",
                f"  output tokens (est.)    {self.output_tokens:,}",
                "",
                f"  projected cost          ${self.cost_usd:.4f}",
                f"  per 1k comments         ${self.cost_per_1k_comments:.4f}",
                "",
                "Assumptions:",
                f"  {CHARS_PER_TOKEN} chars/token; "
                f"{self.output_tokens_per_comment} output tokens per comment",
                "  Output volume scales with how many claims the comments assert,",
                "  so treat this as an order of magnitude, not a quote.",
            ]
        )


def estimate_cost(
    rows: Sequence[sqlite3.Row],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    output_tokens_per_comment: int = DEFAULT_OUTPUT_TOKENS_PER_COMMENT,
    model: str = MODEL,
) -> CostEstimate:
    """Project the cost of extracting `rows`, without calling the API.

    The fixed cost per call — system prompt plus the JSON schema, which
    structured outputs sends as input on every request — is charged once
    per batch, which is the whole argument for batching in the first place.
    """
    schema_tokens = estimate_tokens(json.dumps(RESPONSE_SCHEMA))
    prompt_tokens = estimate_tokens(SYSTEM_PROMPT)
    fixed_per_call = schema_tokens + prompt_tokens + PER_CALL_OVERHEAD_TOKENS

    input_tokens = 0
    batches = 0
    for batch in iter_batches(rows, batch_size):
        input_tokens += fixed_per_call + estimate_tokens(render_batch(batch))
        batches += 1

    return CostEstimate(
        comments=len(rows),
        batches=batches,
        input_tokens=input_tokens,
        output_tokens=len(rows) * output_tokens_per_comment,
        output_tokens_per_comment=output_tokens_per_comment,
        model=model,
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


class BatchParseError(Exception):
    """The model's response could not be turned into claims."""


@dataclass(frozen=True)
class Rejection:
    """A claim the model emitted that validation refused.

    Carries the payload verbatim. A Claim is precisely what it failed to
    become, so parsing it into one on the way out would discard the shape
    that explains the rejection.
    """

    comment_index: int
    reason: str
    raw: dict


INSERT_REJECTION_SQL = """
INSERT INTO rejected_claims (
    comment_id, reason, raw_json, extraction_model, created_at
) VALUES (?, ?, ?, ?, ?)
"""


def write_rejections(
    conn: sqlite3.Connection,
    comment_id: int,
    rejections: Sequence[Rejection],
    *,
    model: str = MODEL,
) -> int:
    """Persist the claims validation refused, for later diagnosis."""
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        INSERT_REJECTION_SQL,
        [
            (
                comment_id,
                rejection.reason,
                json.dumps(rejection.raw, sort_keys=True),
                model,
                now,
            )
            for rejection in rejections
        ],
    )
    return len(rejections)


def parse_response(
    text: str, batch_size: int, rejections: list[Rejection] | None = None
) -> dict[int, list[Claim]]:
    """Parse a model response into per-comment claim lists.

    Raises BatchParseError if the response is unusable as a whole. Claims
    that individually fail validation are dropped with a warning — one bad
    claim must not discard the rest of the batch.

    Pass a `rejections` list to collect the claims that were refused, with
    the payload that caused it. Dropping is correct — a claim violating an
    invariant is not usable — but a drop rate nobody measures is a defect
    nobody fixes, and these rejections are the model reporting where the
    taxonomy and reality disagree.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BatchParseError(f"response was not JSON: {exc}") from exc

    if not isinstance(payload, dict) or "results" not in payload:
        raise BatchParseError("response has no 'results' key")
    if not isinstance(payload["results"], list):
        raise BatchParseError("'results' is not a list")

    by_index: dict[int, list[Claim]] = {}
    for entry in payload["results"]:
        if not isinstance(entry, dict):
            log.warning("Skipping non-object result entry: %r", entry)
            continue

        index = entry.get("comment_index")
        if not isinstance(index, int) or not 0 <= index < batch_size:
            log.warning("Skipping result with out-of-range index: %r", index)
            continue

        claims: list[Claim] = []
        for raw in entry.get("claims") or []:
            try:
                claims.append(Claim.model_validate(raw))
            except ValidationError as exc:
                # Expected: the model occasionally emits a claim that
                # violates an invariant the schema can't express, such as
                # an object on a NONE-kind claim type.
                reason = str(exc.errors()[0]["msg"] if exc.errors() else exc)
                if rejections is not None and isinstance(raw, dict):
                    rejections.append(Rejection(index, reason, raw))
                log.warning(
                    "Dropping invalid claim on comment_index=%s: %s", index, reason
                )
        by_index[index] = claims

    return by_index


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

INSERT_CLAIM_SQL = """
INSERT INTO claims (
    comment_id, claim_type, subject_kind, raw_subject_text,
    object_kind, raw_object_text, sentiment,
    confidence, evidence_span, evidence_verified, extraction_model, created_at
) VALUES (
    :comment_id, :claim_type, :subject_kind, :raw_subject_text,
    :object_kind, :raw_object_text, :sentiment,
    :confidence, :evidence_span, :evidence_verified, :extraction_model, :created_at
)
"""

MARK_EXTRACTED_SQL = "UPDATE comments SET extracted_at = ? WHERE id = ?"

SELECT_PENDING_SQL = """
SELECT id, body FROM comments
WHERE extracted_at IS NULL
ORDER BY id
LIMIT ?
"""

#: Only comments someone has labelled. Re-extracting just these makes a
#: prompt change measurable for a couple of cents instead of the price of
#: the whole corpus — and every comment it touches is one the eval can
#: actually score.
SELECT_PENDING_LABELLED_SQL = """
SELECT c.id, c.body FROM comments c
WHERE c.extracted_at IS NULL
  AND EXISTS (SELECT 1 FROM eval_labels l WHERE l.comment_id = c.id)
ORDER BY c.id
LIMIT ?
"""


def reset_extraction(conn: sqlite3.Connection, *, labelled_only: bool = True) -> int:
    """Delete claims and clear extracted_at so comments re-extract.

    Destructive by design: a before/after comparison needs the "after" to
    replace the "before", not stack on top of it. Claims are derived data
    and `comments.body` is retained, so the cost of being wrong is one
    re-run — but point this at a scratch database built by `corpus import`
    rather than the working corpus, or the run you are comparing against
    is the one you just deleted.
    """
    where = (
        "WHERE EXISTS (SELECT 1 FROM eval_labels l WHERE l.comment_id = comments.id)"
        if labelled_only
        else ""
    )
    for table in ("claims", "rejected_claims"):
        conn.execute(
            f"DELETE FROM {table} WHERE comment_id IN "
            f"(SELECT id FROM comments {where})"
        )
    cursor = conn.execute(f"UPDATE comments SET extracted_at = NULL {where}")
    conn.commit()
    return cursor.rowcount


def write_claims(
    conn: sqlite3.Connection,
    comment_id: int,
    body: str,
    claims: Sequence[Claim],
    *,
    model: str = MODEL,
) -> tuple[int, int]:
    """Persist a comment's claims and mark it extracted.

    Returns (claims written, claims whose evidence failed verification).
    Marking happens even with zero claims — the comment has been seen.
    """
    now = datetime.now(UTC).isoformat()
    unverified = 0

    for claim in claims:
        verified = claim.evidence_matches(body)
        if not verified:
            unverified += 1
            # Log the span in full. Truncating it here made a real
            # investigation impossible: the elided spans looked as though
            # the model had emitted truncated text, when the log was doing
            # the truncating. A diagnostic that hides the evidence is worse
            # than none.
            log.warning(
                "Unverified evidence on comment %s.\n  span: %r\n  body: %r",
                comment_id,
                claim.evidence_span,
                body,
            )
        conn.execute(
            INSERT_CLAIM_SQL,
            {
                "comment_id": comment_id,
                "claim_type": claim.claim_type.value,
                "subject_kind": claim.subject_kind.value,
                "raw_subject_text": claim.raw_subject_text,
                "object_kind": claim.object_kind.value,
                "raw_object_text": claim.raw_object_text,
                "sentiment": claim.sentiment.value,
                "confidence": claim.confidence,
                "evidence_span": claim.evidence_span,
                "evidence_verified": int(verified),
                "extraction_model": model,
                "created_at": now,
            },
        )

    conn.execute(MARK_EXTRACTED_SQL, (now, comment_id))
    return len(claims), unverified


def pending_comments(
    conn: sqlite3.Connection, limit: int, *, labelled_only: bool = False
) -> list[sqlite3.Row]:
    """Comments that have never been extracted, oldest first."""
    sql = SELECT_PENDING_LABELLED_SQL if labelled_only else SELECT_PENDING_SQL
    return conn.execute(sql, (limit,)).fetchall()


def iter_batches(
    rows: Sequence[sqlite3.Row], size: int
) -> Iterator[Sequence[sqlite3.Row]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# --------------------------------------------------------------------------
# API call
# --------------------------------------------------------------------------


def build_client() -> Any:
    """Construct an Anthropic client, checking credentials first."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY must be set. "
            "Copy .env.example to .env and fill it in."
        )
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on install state
        raise SystemExit("anthropic is not installed. Run: uv sync") from exc
    return anthropic.Anthropic()


def call_model(
    client: Any,
    comments: Sequence[sqlite3.Row],
    *,
    model: str = MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, int, int]:
    """Send one batch. Returns (response text, input tokens, output tokens)."""
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=TEMPERATURE,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": render_batch(comments)}],
        output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
    )

    usage = response.usage
    tokens_in = getattr(usage, "input_tokens", 0)
    tokens_out = getattr(usage, "output_tokens", 0)

    if response.stop_reason == "refusal":
        raise BatchParseError("model refused the batch")
    if response.stop_reason == "max_tokens":
        raise BatchParseError(
            f"response truncated at max_tokens={max_tokens}; lower --batch-size"
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise BatchParseError("response contained no text block")

    return text, tokens_in, tokens_out


# --------------------------------------------------------------------------
# Run loop
# --------------------------------------------------------------------------


def extract(
    conn: sqlite3.Connection,
    client: Any,
    *,
    limit: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    progress_every: int = 5,
    labelled_only: bool = False,
) -> CostTracker:
    """Extract claims for up to `limit` un-extracted comments."""
    rows = pending_comments(conn, limit, labelled_only=labelled_only)
    if not rows:
        log.info("No comments pending extraction.")
        return CostTracker()

    log.info("Extracting %d comments in batches of %d.", len(rows), batch_size)
    cost = CostTracker()
    started = time.monotonic()
    total_claims = 0
    total_unverified = 0
    drops: Counter[str] = Counter()
    total_rejected = 0

    try:
        for batch in iter_batches(rows, batch_size):
            try:
                text, tokens_in, tokens_out = call_model(
                    client, batch, model=model, max_tokens=max_tokens
                )
                rejections: list[Rejection] = []
                by_index = parse_response(text, len(batch), rejections)
                drops.update(r.reason for r in rejections)
            except BatchParseError as exc:
                # The whole batch is unusable. Leave extracted_at NULL so
                # these comments are retried, and move on.
                cost.failed_batches += 1
                log.error("Batch failed, will retry on next run: %s", exc)
                continue
            except Exception as exc:  # transport, rate limit, overload
                cost.failed_batches += 1
                log.exception("Batch errored, will retry on next run: %s", exc)
                continue

            for index, row in enumerate(batch):
                claims = by_index.get(index, [])
                written, unverified = write_claims(
                    conn, row["id"], row["body"], claims, model=model
                )
                total_claims += written
                total_unverified += unverified
                total_rejected += write_rejections(
                    conn,
                    row["id"],
                    [r for r in rejections if r.comment_index == index],
                    model=model,
                )

            conn.commit()
            cost.record(tokens_in, tokens_out, len(batch))

            if cost.batches % progress_every == 0:
                log.info(
                    "%s | %d claims (%d dropped)",
                    cost.summary(),
                    total_claims,
                    sum(drops.values()),
                )
    except KeyboardInterrupt:
        log.warning("Interrupted. Committing completed batches.")
        raise
    finally:
        conn.commit()

    elapsed = time.monotonic() - started

    # "0 claims written" reads like a result. When every batch failed it is
    # not one, and the distinction is easy to miss in a filtered log.
    if cost.failed_batches and not cost.batches:
        log.error(
            "ALL %d batches failed — no comments were extracted. "
            "Re-run without filtering the log to see the cause.",
            cost.failed_batches,
        )
    elif cost.failed_batches:
        log.warning(
            "%d of %d batches failed and will be retried on the next run.",
            cost.failed_batches,
            cost.failed_batches + cost.batches,
        )

    log.info("Done in %.1fs. %s", elapsed, cost.summary())
    log.info(
        "%d claims written, %d with unverified evidence (%.1f%%).",
        total_claims,
        total_unverified,
        100 * total_unverified / total_claims if total_claims else 0.0,
    )

    # The drop breakdown is the most actionable output of a run. Each line
    # is the model reporting a place where the taxonomy and real comments
    # disagree — and the rejections are invisible in the database, since
    # dropped claims are never written.
    dropped = sum(drops.values())
    if dropped:
        emitted = total_claims + dropped
        log.info(
            "%d claims dropped in validation (%.1f%% of %d emitted). By reason:",
            dropped,
            100 * dropped / emitted if emitted else 0.0,
            emitted,
        )
        for reason, count in drops.most_common():
            log.info("  %5d  %s", count, reason)
        log.info(
            "Stored in rejected_claims. Inspect with: "
            "python -m fragrance_graph.extract.rejects show"
        )

    return cost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Extract fragrance claims from ingested comments."
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="Max comments to extract. Default: 100"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Comments per API call. Default: {DEFAULT_BATCH_SIZE}",
    )
    parser.add_argument("--model", default=MODEL, help=f"Model. Default: {MODEL}")
    parser.add_argument(
        "--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Output cap per call"
    )
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite database path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Estimate cost and exit. Makes no API call and needs no API key.",
    )
    parser.add_argument(
        "--assume-output-tokens",
        type=int,
        default=DEFAULT_OUTPUT_TOKENS_PER_COMMENT,
        metavar="N",
        help=(
            "Output tokens per comment assumed by --dry-run. "
            f"Default: {DEFAULT_OUTPUT_TOKENS_PER_COMMENT}"
        ),
    )
    parser.add_argument(
        "--only-labelled",
        action="store_true",
        help="Extract only comments that carry an eval label",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Delete existing claims and re-extract. Destructive — point it "
            "at a scratch database, not the working corpus."
        ),
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    conn = get_connection(args.db_path)
    migrate(conn)

    if args.only_labelled:
        # "Reset 0 comments" and "No comments pending" are both true when a
        # database simply has no labels in it, and neither says so. The
        # usual cause is a scratch database rebuilt from a corpus export
        # that predates the labelling.
        labelled = conn.execute("SELECT count(*) FROM eval_labels").fetchone()[0]
        if not labelled:
            conn.close()
            raise SystemExit(
                "--only-labelled was passed but this database holds no eval "
                "labels, so there is nothing to extract.\n\n"
                "If this is a scratch database, the corpus export it was "
                "built from predates the labels. Re-export from the database "
                "that has them, then rebuild:\n"
                "  python -m fragrance_graph.corpus export --db-path <real.db>\n"
                "  python -m fragrance_graph.corpus import --db-path <scratch.db>"
            )

    if args.reset and args.dry_run:
        # --dry-run is documented as "makes no API call and needs no API
        # key", which reads as "changes nothing". Combining it with --reset
        # used to delete the claims and then exit before re-extracting
        # them, so the safe-looking flag was the destructive one.
        conn.close()
        raise SystemExit(
            "--reset deletes claims and --dry-run exits before re-extracting "
            "them, so together they would leave the database emptier than "
            "they found it.\n\n"
            "To price the run first, estimate the reset separately:\n"
            "  python -m fragrance_graph.extract.llm --dry-run --limit 50\n"
            "then re-run with --reset when you are ready to pay for it."
        )

    if args.reset:
        cleared = reset_extraction(conn, labelled_only=args.only_labelled)
        log.warning(
            "Reset %d comment(s): their claims are deleted and they will be "
            "re-extracted.",
            cleared,
        )

    # The estimate runs before the credential check on purpose: deciding
    # whether a run is affordable should not require holding a key.
    if args.dry_run:
        try:
            estimate = estimate_cost(
                pending_comments(
                    conn, args.limit, labelled_only=args.only_labelled
                ),
                batch_size=args.batch_size,
                output_tokens_per_comment=args.assume_output_tokens,
                model=args.model,
            )
        finally:
            conn.close()
        print(estimate.render())
        return 0

    client = build_client()

    try:
        extract(
            conn,
            client,
            limit=args.limit,
            labelled_only=args.only_labelled,
            batch_size=args.batch_size,
            model=args.model,
            max_tokens=args.max_tokens,
        )
    except KeyboardInterrupt:
        return 130
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
