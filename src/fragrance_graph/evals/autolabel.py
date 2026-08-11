"""Drafting eval labels with a stronger model, for a human to review.

Hand-labelling a few hundred comments is the bottleneck on measuring
extraction quality, and most comments assert nothing — so most of the hour
goes into typing empty lists. This module has a stronger model draft the
labels so the human reviews rather than authors.

**A drafted label is not ground truth.** The extractor is Claude Haiku 4.5;
if a model writes the answer key, the score measures agreement between
models, not accuracy. Two safeguards make the drafts usable anyway:

1. **Provenance is permanent.** Drafts import under their own `labeler`
   (`opus5-draft` by default), never the human's name. `eval_labels` is
   keyed on `(comment_id, labeler)`, so a draft can never be mistaken for,
   or silently overwrite, a human judgement.
2. **Blind calibration.** The human labels a subset *without seeing the
   drafts*, and `agreement` measures how far the drafter is from them.
   High agreement earns the right to lean on the drafts; low agreement is
   the signal to label everything by hand. Skipping this step is how
   pre-filled annotation quietly turns a model's opinion into "ground
   truth" — the drafts look plausible, so they get rubber-stamped.

The drafting prompt is written independently of the extraction prompt. It
adjudicates what a comment asserts rather than restating how to extract,
because a labeller that echoes the extractor's instructions guarantees the
correlated errors this whole exercise exists to detect.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.evals.labels import export_template, load_labels
from fragrance_graph.evals.score import edge_score, match_key, score
from fragrance_graph.models import ALLOWED_OBJECT_KINDS, EDGE_CLAIM_TYPES, ClaimType

log = logging.getLogger("fragrance_graph.evals.autolabel")

#: Drafting model. Deliberately a stronger model than the extractor: a
#: labeller at the extractor's own capability measures self-consistency.
MODEL = "claude-opus-5"

#: Claude Opus 5 list pricing, USD per million tokens.
PRICE_INPUT_PER_MTOK = 5.00
PRICE_OUTPUT_PER_MTOK = 25.00

#: Comments per call. Smaller than the extractor's batch: Opus 5 thinks by
#: default and `max_tokens` caps thinking plus output together.
DEFAULT_BATCH_SIZE = 10
DEFAULT_MAX_TOKENS = 16000

#: The labeler name drafts are stored under. Never a person's name.
DRAFT_LABELER = "opus5-draft"

#: Comments held back for the human to label blind. Enough to detect a
#: badly-miscalibrated drafter, small enough to be ten minutes' work.
DEFAULT_CALIBRATION = 15

#: How to treat a claim whose subject is a pronoun ("It's not strong",
#: "This is a clone of X"). Two of three comments sampled from the live
#: corpus had one, so the policy materially changes the numbers — and an
#: inconsistent policy reads as model error when it is really labeller
#: drift. It is an explicit input rather than a silent default.
PRONOUN_POLICIES = {
    "skip": (
        'If the subject or object is a pronoun ("it", "this", "that one"): '
        "when the fragrance it refers to is named elsewhere in the same "
        "comment, use that name, spelled as the commenter spelled it. When "
        "the fragrance is named nowhere in the comment, do NOT emit the "
        "claim at all — it can never be resolved to a bottle, so it cannot "
        "become an edge."
    ),
    "literal": (
        "If the subject of a claim is a pronoun or a bare reference "
        '("it", "this"), emit the claim with the pronoun as the subject '
        "text, exactly as written."
    ),
}
DEFAULT_PRONOUN_POLICY = "skip"


def _taxonomy_block() -> str:
    """The claim types, generated from the models module.

    Written from `ALLOWED_OBJECT_KINDS` rather than typed out, so the
    labeller and the validator can never disagree about what is legal.
    """
    lines = []
    for claim_type in ClaimType:
        kinds = sorted(k.value for k in ALLOWED_OBJECT_KINDS[claim_type])
        takes = "no object" if kinds == ["NONE"] else f"object: {', '.join(kinds)}"
        edge = "  [builds the graph]" if claim_type in EDGE_CLAIM_TYPES else ""
        lines.append(f"- {claim_type.value} ({takes}){edge}")
    return "\n".join(lines)


def build_prompt(pronoun_policy: str = DEFAULT_PRONOUN_POLICY) -> str:
    """The labelling instructions, with the chosen pronoun policy inlined.

    `pronoun_policy` is a key of PRONOUN_POLICIES, resolved to its text
    here. Passing an unknown key raises rather than quietly inlining the
    key itself — a prompt that reads "- skip" instead of the rule would
    still produce plausible labels, and nothing downstream would notice.
    """
    try:
        policy_text = PRONOUN_POLICIES[pronoun_policy]
    except KeyError:
        known = ", ".join(sorted(PRONOUN_POLICIES))
        raise ValueError(
            f"Unknown pronoun policy {pronoun_policy!r}. Known: {known}"
        ) from None

    return f"""\
You are producing a gold-standard answer key for evaluating a claim
extraction system. You are not the extractor. Your job is to decide what a
careful human annotator would say each comment actually asserts.

You will receive numbered fragrance comments. For each, list the claims a
reasonable annotator would agree are asserted.

## Claim types

{_taxonomy_block()}

## What counts as a claim

Be conservative. A claim exists when the commenter states it, not when it
could be inferred. "I bought Khamrah today" asserts nothing about how
Khamrah smells. "Khamrah is basically Angels Share" asserts SIMILAR_TO.

Most comments assert nothing at all — praise, questions, greetings, and
prices are not claims. An empty list is the correct and common answer, and
a wrong claim costs more than a missed one, because this is the answer key
that everything else is measured against.

## Rules

- Subject and object are the commenter's own words, copied verbatim.
  Do not expand, correct, or normalise names: "BR540" stays "BR540".
- DUPE_OF means the subject is a cheaper stand-in for the object.
  Fragrance discussion signals that with "dupe", "clone", "impression of",
  "interpretation of", or "inspired by" — the price gap is carried by the
  brands and is almost never stated, so do not require the comment to
  mention cost. Use SIMILAR_TO when a resemblance is reported without
  implying substitution: "smells like", "reminds me of", "X vibes".
- One subject per claim. A sentence naming three fragrances is three
  claims, never one claim with three names crammed into the subject.
- {policy_text}
- sentiment is POSITIVE, NEGATIVE, or NEUTRAL, and describes how the
  commenter frames it. A complaint about weak projection is PROJECTION
  with NEGATIVE sentiment, not a separate claim type.
- Only use object kinds the type permits, as listed above.

Do not include confidence or supporting quotes. Those are properties of an
extractor's output, not facts about the comment, and scoring a human
judgement against them would measure nothing.
"""


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
                                "raw_subject_text": {"type": "string"},
                                "raw_object_text": {"type": ["string", "null"]},
                                "sentiment": {
                                    "type": "string",
                                    "enum": ["POSITIVE", "NEGATIVE", "NEUTRAL"],
                                },
                            },
                            "required": [
                                "claim_type",
                                "raw_subject_text",
                                "raw_object_text",
                                "sentiment",
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


@dataclass
class DraftStats:
    comments: int = 0
    claims: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    failed_batches: int = 0

    @property
    def cost_usd(self) -> float:
        return (
            self.input_tokens / 1_000_000 * PRICE_INPUT_PER_MTOK
            + self.output_tokens / 1_000_000 * PRICE_OUTPUT_PER_MTOK
        )

    def __str__(self) -> str:
        return (
            f"{self.comments} comments drafted, {self.claims} claims "
            f"({self.failed_batches} batches failed) | "
            f"{self.input_tokens} in / {self.output_tokens} out tokens | "
            f"${self.cost_usd:.4f}"
        )


def build_client() -> Any:
    if not __import__("os").environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY must be set. Copy .env.example to .env."
        )
    import anthropic

    return anthropic.Anthropic()


def call_model(
    client: Any,
    entries: list[dict],
    *,
    prompt: str,
    model: str = MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> tuple[str, int, int]:
    """Draft one batch. Returns (response text, input tokens, output tokens).

    No `temperature`: sampling parameters are rejected with a 400 on Claude
    Opus 5. Reproducibility comes from the prompt, not from pinning a
    sampler that no longer exists.
    """
    rendered = "\n\n".join(
        f"[{i}] {entry['body']}" for i, entry in enumerate(entries)
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=prompt,
        messages=[{"role": "user", "content": rendered}],
        output_config={"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}},
    )

    # Claude Opus 5 can decline a request outright; that arrives as a
    # successful 200 with an empty content list, so reading content[0]
    # first would raise IndexError instead of explaining what happened.
    if response.stop_reason == "refusal":
        raise RuntimeError("model declined to label this batch")
    if response.stop_reason == "max_tokens":
        raise RuntimeError(
            f"response truncated at max_tokens={max_tokens}; lower --batch-size"
        )

    text = next((b.text for b in response.content if b.type == "text"), None)
    if text is None:
        raise RuntimeError("response contained no text block")

    usage = response.usage
    return text, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)


def parse_batch(text: str, batch_size: int) -> dict[int, list[dict]]:
    """Map a response back to per-comment claim lists, keyed by index."""
    payload = json.loads(text)
    out: dict[int, list[dict]] = {}
    for entry in payload.get("results", []):
        index = entry.get("comment_index")
        if not isinstance(index, int) or not 0 <= index < batch_size:
            log.warning("Skipping out-of-range comment_index: %r", index)
            continue
        out[index] = [c for c in entry.get("claims") or [] if isinstance(c, dict)]
    return out


def draft(
    client: Any,
    entries: list[dict],
    *,
    pronoun_policy: str = DEFAULT_PRONOUN_POLICY,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = MODEL,
) -> tuple[list[dict], DraftStats]:
    """Fill the `claims` list on each template entry. Returns (entries, stats).

    A failed batch leaves its entries with empty claims and is counted, not
    retried silently — an empty list is a meaningful label, so a batch that
    failed must never be indistinguishable from one that found nothing.
    """
    prompt = build_prompt(pronoun_policy)
    stats = DraftStats()
    drafted = [dict(e) for e in entries]

    for start in range(0, len(drafted), batch_size):
        batch = drafted[start : start + batch_size]
        try:
            text, tokens_in, tokens_out = call_model(
                client, batch, prompt=prompt, model=model
            )
            by_index = parse_batch(text, len(batch))
        except Exception as exc:
            stats.failed_batches += 1
            log.error("Batch at offset %d failed: %s", start, exc)
            continue

        stats.input_tokens += tokens_in
        stats.output_tokens += tokens_out
        for index, claims in by_index.items():
            batch[index]["claims"] = claims
            stats.claims += len(claims)
        stats.comments += len(batch)
        log.info("Drafted %d/%d comments", stats.comments, len(drafted))

    for entry in drafted:
        entry["drafted_by"] = model
        entry["pronoun_policy"] = pronoun_policy
    return drafted, stats


def calibration_subset(entries: list[dict], n: int) -> list[dict]:
    """The entries a human should label blind, with claims stripped.

    Taken from the drafted set so the two are directly comparable, and
    emptied so the reviewer forms a judgement instead of ratifying one.
    """
    from fragrance_graph.evals.labels import sample_rank

    chosen = sorted(entries, key=lambda e: sample_rank(e["source_id"]))[:n]
    return [
        {k: v for k, v in e.items() if k not in ("drafted_by", "pronoun_policy")}
        | {"claims": []}
        for e in sorted(chosen, key=lambda e: (e["source"], e["source_id"]))
    ]


def agreement(
    conn: sqlite3.Connection, *, human: str, drafter: str = DRAFT_LABELER
) -> Any:
    """How closely the drafter matches the human, on comments both labelled.

    Reuses the extraction scorer with the drafts standing in for extracted
    claims, so "precision" is the share of drafted claims the human agrees
    with and "recall" is the share of the human's claims the drafter found.
    """
    human_labels = load_labels(conn, labeler=human)
    draft_labels = load_labels(conn, labeler=drafter)
    overlap = set(human_labels) & set(draft_labels)
    if not overlap:
        raise SystemExit(
            f"No comment carries labels from both {human!r} and {drafter!r}. "
            "Import the blind set and the drafts before comparing."
        )

    # A side with no claims at all makes the comparison vacuous: every
    # drafted claim becomes a false positive and the report reads as a
    # catastrophic score rather than as "one side is unlabelled". The
    # overwhelmingly likely cause is a blind template that was imported
    # before it was filled in.
    if not any(human_labels[k] for k in overlap):
        raise SystemExit(
            f"{human!r} has no claims on any of the {len(overlap)} shared "
            f"comments, so there is nothing to compare — every drafted claim "
            f"would score as a false positive.\n\n"
            f"If the blind set is still unedited, fill in its claims lists "
            f"and re-import as {human!r} (importing replaces, so nothing is "
            f"lost). If you genuinely judged all of them to assert nothing, "
            f"widen the calibration set instead: a sample with no claims in "
            f"it cannot tell you whether the drafter finds claims correctly."
        )

    return score(
        {k: v for k, v in draft_labels.items() if k in overlap},
        {k: v for k, v in human_labels.items() if k in overlap},
    )


def disagreements(
    conn: sqlite3.Connection, *, human: str, drafter: str = DRAFT_LABELER
) -> list[dict]:
    """The specific claims the two sides differ on, per comment.

    An F1 is a verdict, not a diagnosis. A false positive and a false
    negative on the same comment usually mean the two sides picked
    different types for one assertion — a boundary disagreement worth
    resolving — while a lone false positive means the drafter invented a
    claim. The counts cannot tell those apart; this can.
    """
    human_labels = load_labels(conn, labeler=human)
    draft_labels = load_labels(conn, labeler=drafter)
    bodies = {
        row["id"]: row["body"]
        for row in conn.execute("SELECT id, body FROM comments")
    }

    out = []
    for comment_id in sorted(set(human_labels) & set(draft_labels)):
        mine = {match_key(comment_id, c): c for c in human_labels[comment_id]}
        theirs = {match_key(comment_id, c): c for c in draft_labels[comment_id]}
        missed = [c for k, c in mine.items() if k not in theirs]
        invented = [c for k, c in theirs.items() if k not in mine]
        if missed or invented:
            out.append(
                {
                    "comment_id": comment_id,
                    "body": bodies.get(comment_id, ""),
                    "only_yours": missed,
                    "only_drafted": invented,
                }
            )
    return out


def render_claim(claim: dict) -> str:
    obj = claim.get("raw_object_text")
    arrow = f" -> {obj}" if obj else ""
    return (
        f"{claim['claim_type']}: {claim.get('raw_subject_text')}{arrow} "
        f"[{claim.get('sentiment', 'NEUTRAL')}]"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Draft eval labels with a stronger model, for human review."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("draft", help="Draft labels for review")
    d.add_argument("out", type=Path, help="Where to write the drafted template")
    d.add_argument(
        "--from", dest="from_template", type=Path, default=None, metavar="FILE",
        help=(
            "Draft the comments in an existing template rather than a fresh "
            "sample — e.g. one written by `evals.sample plan`, so drafting "
            "and targeted selection compose. Ignores --sample."
        ),
    )
    d.add_argument("--sample", type=int, default=50, help="Comments to draft")
    d.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    d.add_argument("--model", default=MODEL)
    d.add_argument(
        "--pronoun-policy",
        choices=sorted(PRONOUN_POLICIES),
        default=DEFAULT_PRONOUN_POLICY,
        help=(
            "How to label claims whose subject is a pronoun. 'skip' omits "
            "them (they cannot resolve to a bottle); 'literal' keeps the "
            f"pronoun as the subject. Default: {DEFAULT_PRONOUN_POLICY}"
        ),
    )

    b = sub.add_parser("blind", help="Extract a calibration set for blind labelling")
    b.add_argument("drafted", type=Path, help="The drafted template")
    b.add_argument("out", type=Path, help="Where to write the blind set")
    b.add_argument("--n", type=int, default=DEFAULT_CALIBRATION)

    a = sub.add_parser("agreement", help="Compare a human labeler to the drafter")
    a.add_argument("--human", required=True, help="The human labeler's name")
    a.add_argument("--drafter", default=DRAFT_LABELER)
    a.add_argument(
        "--quiet",
        action="store_true",
        help="Counts only. By default the differing claims are printed too.",
    )

    for p in (d, b, a):
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.command == "blind":
        entries = json.loads(args.drafted.read_text())
        blind = calibration_subset(entries, args.n)
        args.out.write_text(json.dumps(blind, indent=2, ensure_ascii=False))
        log.info(
            "Wrote %d comments to %s. Label these WITHOUT reading %s.",
            len(blind),
            args.out,
            args.drafted,
        )
        return 0

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "agreement":
            report = agreement(conn, human=args.human, drafter=args.drafter)
            print(f"Drafter {args.drafter!r} vs human {args.human!r}")
            print(report.render())

            human_labels = load_labels(conn, labeler=args.human)
            draft_labels = load_labels(conn, labeler=args.drafter)
            shared = set(human_labels) & set(draft_labels)
            edges = edge_score(
                {k: v for k, v in draft_labels.items() if k in shared},
                {k: v for k, v in human_labels.items() if k in shared},
            )
            print()
            print(edges.line("SIMILARITY EDGES"))
            print("  DUPE_OF and SIMILAR_TO collapsed — both build the same edge.")

            if not args.quiet:
                differing = disagreements(
                    conn, human=args.human, drafter=args.drafter
                )
                print(f"\n--- {len(differing)} comment(s) you disagree on ---")
                for row in differing:
                    print(f"\ncomment {row['comment_id']}: {row['body'][:160]}")
                    for claim in row["only_yours"]:
                        print(f"  you  only: {render_claim(claim)}")
                    for claim in row["only_drafted"]:
                        print(f"  draft only: {render_claim(claim)}")
                if differing:
                    print(
                        "\nA 'you only' and a 'draft only' on the same comment "
                        "is one assertion typed two ways, not a miss."
                    )
            return 0

        if args.from_template:
            # Drafting a chosen set, not a fresh sample. Without this the
            # targeted plan and the drafting shortcut cannot compose: `draft`
            # took an output path only, so pointing it at a plan silently
            # overwrote the plan with 50 uniformly-sampled comments. That
            # happened on 2026-08-11 and cost the selection it was meant to
            # act on.
            entries = json.loads(args.from_template.read_text())
            if not isinstance(entries, list) or not entries:
                raise SystemExit(f"{args.from_template} holds no entries.")
            already = [e for e in entries if e.get("claims")]
            if already:
                raise SystemExit(
                    f"{len(already)} of {len(entries)} entries in "
                    f"{args.from_template} already carry claims. Drafting "
                    "would overwrite them — point --from at an unlabelled "
                    "template, or drop those rows first."
                )
            log.info("Drafting %d comments from %s.",
                     len(entries), args.from_template)
        else:
            entries = export_template(conn, sample=args.sample)
        if not entries:
            raise SystemExit("No comments to label. Ingest a corpus first.")
    finally:
        conn.close()

    client = build_client()
    drafted, stats = draft(
        client,
        entries,
        pronoun_policy=args.pronoun_policy,
        batch_size=args.batch_size,
        model=args.model,
    )
    args.out.write_text(json.dumps(drafted, indent=2, ensure_ascii=False))
    log.info("%s", stats)
    log.info("Wrote %s. Review every entry before importing.", args.out)
    if stats.failed_batches:
        log.warning(
            "%d batch(es) failed; those comments have empty claims that are "
            "NOT a judgement. Re-run or label them by hand.",
            stats.failed_batches,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
