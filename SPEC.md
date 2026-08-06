# Fragrance Similarity Engine — Spec

## What we're building

A system that ingests fragrance discussion from Reddit, uses an LLM to
extract structured claims about fragrance similarity, and builds a weighted
graph answering: "I love X — what else smells like it?"

## Stack

- Python 3.11+
- `uv` for dependency management
- SQLite (single file, no server)
- Pydantic for schemas
- Anthropic SDK for extraction
- No web framework yet

## Phases

Work happens in phases. Only the current phase should be implemented at any
given time — later phases are listed here for context, not to be built
early.

### Phase 1 — foundation (current)

1. **Repo scaffold**: `pyproject.toml`, `src/fragrance_graph/`, `tests/`,
   `.env.example`, `README.md`. Ruff + pytest configured.

2. **SQLite schema** in `db.py`, with migrations as plain `.sql` files:
   - `fragrances`: id, canonical_name, brand, house_year, aliases (JSON array)
   - `comments`: id, source, source_id, body, permalink, created_utc,
     subreddit, score, extracted_at (nullable)
   - `claims`: id, comment_id, claim_type, object_kind, subject_frag_id,
     object_frag_id, raw_subject_text, raw_object_text, confidence,
     evidence_span, evidence_verified, extraction_model, created_at
   - `eval_labels`: comment_id, labeled_json, labeler, created_at

   **`object_kind`** (`FRAGRANCE | TAG | NONE`) distinguishes the two shapes
   crammed into `claim_type`. `SIMILAR_TO`/`DUPE_OF`/`REMINDS_ME_OF`/
   `BETTER_THAN` are binary edges between fragrances (`FRAGRANCE`) and are
   what the graph is built from. `OCCASION`/`AESTHETIC` are unary attributes
   whose object is a non-fragrance concept — "weddings", "old money"
   (`TAG`). `LONGEVITY_COMPLAINT`/`UNMET_PRODUCT_REQUEST` have no object
   (`NONE`). It is derived from `claim_type` rather than emitted by the LLM
   (costs no output tokens, cannot be inconsistent) and stored denormalized
   so downstream queries filter without knowing the mapping. A CHECK
   constraint enforces that an object is present for `FRAGRANCE`/`TAG` and
   absent for `NONE`.

   **`evidence_verified`** records whether `evidence_span` was actually
   found in `comments.body` at write time. If the model paraphrases instead
   of quoting, the evidence is fiction and any eval number computed from it
   is meaningless — so the check runs on every write and the result is
   persisted, not assumed.

   Note that `UNMET_PRODUCT_REQUEST` is `TAG`, not `NONE`: the requested
   thing ("a body lotion") is the entire value of the claim type. Mapping it
   to `NONE` would record that someone wanted something but not what,
   killing the "body lotion: 842 mentions" rollup that justifies collecting
   it. A request with no identifiable object is now rejected at validation
   rather than stored as a contentless row.

3. **Reddit ingest** (`ingest/reddit.py`) using PRAW, read-only. Pull top +
   new from r/fragrance and r/DelugeFragrance. Idempotent on `source_id`.
   CLI: `python -m fragrance_graph.ingest.reddit --subreddit fragrance --limit 500`
   Rate-limit politely, log progress, resume cleanly if interrupted.

4. **Pydantic extraction schema** (`models.py`). One comment yields zero or
   more claims:
   - `claim_type`: `SIMILAR_TO | DUPE_OF | REMINDS_ME_OF | BETTER_THAN |
     OCCASION | AESTHETIC | LONGEVITY_COMPLAINT | UNMET_PRODUCT_REQUEST`
   - subject/object as raw strings (entity resolution is a later phase)
   - `confidence`: 0-1, how clearly the comment asserts this
   - `evidence_span`: the substring supporting the claim

5. **Extraction module** (`extract/llm.py`): batch comments, call
   `claude-haiku-4-5` with a schema-constrained prompt, parse strictly,
   write claims. Must handle malformed JSON without crashing the batch, and
   never re-extract a comment that already has `extracted_at` set. Every
   claim's `evidence_span` is checked against the comment body before write
   and the outcome stored in `evidence_verified`.

6. **Tests**: schema validation, idempotent ingest, malformed-LLM-output
   handling, evidence-span verification. Use fixtures, not live API calls.

### Deferred decisions

Recorded so they aren't rediscovered later. None block Phase 1.

- **`SIMILAR_TO` vs `REMINDS_ME_OF` are near-synonyms.** The LLM will assign
  them inconsistently. Resolve when writing the extraction prompt in
  `extract/llm.py`: either merge the two types, or write a sharp
  disambiguation rule (proposed: `SIMILAR_TO` = an olfactory claim about the
  scent itself; `REMINDS_ME_OF` = an associative/memory claim that need not
  imply smelling alike). Either way the eval set must explicitly measure
  agreement on this pair — it is the most likely source of silent label
  noise in the whole taxonomy.
- **`LONGEVITY_COMPLAINT` carries no severity or subtype.** Longevity vs.
  sillage vs. projection are different complaints and may deserve splitting,
  possibly with severity. Deferred — revisit once there is real data showing
  the mix.
- **`evidence_span` is not always a literal substring.** Verification falls
  back to whitespace/case-normalized matching, so a span can be verified
  without being byte-identical, and offset-based highlighting is not
  possible from the stored span alone. If highlighting is wanted, add
  `evidence_start`/`evidence_end`. Safe to defer: `comments.body` is
  retained, so offsets are derivable by backfill and need no re-extraction.

### Phase 2 and beyond (not built yet)

- Entity resolution (mapping raw subject/object text to canonical
  `fragrances` rows)
- The similarity graph itself
- Ranking / scoring
- Any web UI
- TikTok or other social sources

If work drifts into these areas while doing Phase 1, stop.

## Constraints

- Reddit's public API only. No scraping of Fragrantica, Parfumo, or
  retailers — their ToS forbid it and this needs to be publicly demoable.
- Extraction must be cheap: this runs over 100k+ comments eventually.
- Cost per 1k comments must be logged so it's visible.
