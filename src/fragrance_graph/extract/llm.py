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
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.models import Claim, ClaimType

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
# NOTE: the claim-type definitions below are the taxonomy as currently
# specified. The SIMILAR_TO / REMINDS_ME_OF boundary is the one line that
# should be revisited against real comments before any large extraction run
# — see SPEC.md, "Deferred decisions". Everything else in this module is
# independent of how that resolves.
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You extract structured claims about fragrances from Reddit comments.

You will receive a numbered list of comments. For each one, return every
claim it asserts. Most comments assert nothing — returning an empty list
for a comment is correct and expected. Do not invent claims to fill space.

Claim types:

- SIMILAR_TO: an olfactory claim that two fragrances smell alike.
  ("Delina smells just like Baccarat 540")
- REMINDS_ME_OF: an associative or memory claim. The subject evokes
  something; this need not mean they smell alike.
  ("Reminds me of my grandmother's house")
- DUPE_OF: a claim that one fragrance is a cheaper substitute for another.
- BETTER_THAN: a preference claim between two fragrances.
- OCCASION: the fragrance suits a setting or event. Object is the setting.
  ("great for weddings")
- AESTHETIC: the fragrance evokes a style or vibe. Object is the vibe.
  ("very old money")
- LONGEVITY_COMPLAINT: the fragrance fades or projects poorly. No object.
- UNMET_PRODUCT_REQUEST: the commenter wants a product form that does not
  exist. Object is the requested form. ("wish it came in a body lotion")

Rules:

- raw_subject_text and raw_object_text are the commenter's own words. Do
  not normalise, correct, or expand names. "BR540" stays "BR540".
- evidence_span MUST be copied verbatim from the comment body. Do not
  paraphrase, summarise, or reconstruct it. If you cannot quote the
  comment exactly, do not make the claim.
- confidence reflects how clearly the comment asserts the claim, not how
  true you think it is. Hedged language ("kinda similar I guess") is low
  confidence; a flat assertion is high.
- Claims requiring an object (all except LONGEVITY_COMPLAINT) must have
  one. If there is no identifiable object, omit the claim entirely.
"""

#: Response schema. Claims are nested under a comment index so results map
#: back without the model echoing comment text.
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
                                # minLength mirrors the Pydantic contract, so
                                # empty strings are refused at generation
                                # rather than validated away after we have
                                # already paid to generate them.
                                "raw_subject_text": {"type": "string", "minLength": 1},
                                "raw_object_text": {"type": ["string", "null"]},
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "evidence_span": {"type": "string", "minLength": 1},
                            },
                            "required": [
                                "claim_type",
                                "raw_subject_text",
                                "raw_object_text",
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
# Parsing
# --------------------------------------------------------------------------


class BatchParseError(Exception):
    """The model's response could not be turned into claims."""


def parse_response(
    text: str, batch_size: int
) -> dict[int, list[Claim]]:
    """Parse a model response into per-comment claim lists.

    Raises BatchParseError if the response is unusable as a whole. Claims
    that individually fail validation are dropped with a warning — one bad
    claim must not discard the rest of the batch.
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
                log.warning(
                    "Dropping invalid claim on comment_index=%s: %s",
                    index,
                    exc.errors()[0]["msg"] if exc.errors() else exc,
                )
        by_index[index] = claims

    return by_index


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

INSERT_CLAIM_SQL = """
INSERT INTO claims (
    comment_id, claim_type, object_kind, raw_subject_text, raw_object_text,
    confidence, evidence_span, evidence_verified, extraction_model, created_at
) VALUES (
    :comment_id, :claim_type, :object_kind, :raw_subject_text, :raw_object_text,
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
            log.warning(
                "Unverified evidence on comment %s: %r not found in body",
                comment_id,
                claim.evidence_span[:60],
            )
        conn.execute(
            INSERT_CLAIM_SQL,
            {
                "comment_id": comment_id,
                "claim_type": claim.claim_type.value,
                "object_kind": claim.object_kind.value,
                "raw_subject_text": claim.raw_subject_text,
                "raw_object_text": claim.raw_object_text,
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
    conn: sqlite3.Connection, limit: int
) -> list[sqlite3.Row]:
    """Comments that have never been extracted, oldest first."""
    return conn.execute(SELECT_PENDING_SQL, (limit,)).fetchall()


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
) -> CostTracker:
    """Extract claims for up to `limit` un-extracted comments."""
    rows = pending_comments(conn, limit)
    if not rows:
        log.info("No comments pending extraction.")
        return CostTracker()

    log.info("Extracting %d comments in batches of %d.", len(rows), batch_size)
    cost = CostTracker()
    started = time.monotonic()
    total_claims = 0
    total_unverified = 0

    try:
        for batch in iter_batches(rows, batch_size):
            try:
                text, tokens_in, tokens_out = call_model(
                    client, batch, model=model, max_tokens=max_tokens
                )
                by_index = parse_response(text, len(batch))
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

            conn.commit()
            cost.record(tokens_in, tokens_out, len(batch))

            if cost.batches % progress_every == 0:
                log.info("%s | %d claims", cost.summary(), total_claims)
    except KeyboardInterrupt:
        log.warning("Interrupted. Committing completed batches.")
        raise
    finally:
        conn.commit()

    elapsed = time.monotonic() - started
    log.info("Done in %.1fs. %s", elapsed, cost.summary())
    log.info(
        "%d claims written, %d with unverified evidence (%.1f%%).",
        total_claims,
        total_unverified,
        100 * total_unverified / total_claims if total_claims else 0.0,
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
    client = build_client()

    try:
        extract(
            conn,
            client,
            limit=args.limit,
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
