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
   - `claims`: id, comment_id, claim_type, subject_frag_id, object_frag_id,
     raw_subject_text, raw_object_text, confidence, extraction_model,
     created_at
   - `eval_labels`: comment_id, labeled_json, labeler, created_at

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
   never re-extract a comment that already has `extracted_at` set.

6. **Tests**: schema validation, idempotent ingest, malformed-LLM-output
   handling. Use fixtures, not live API calls.

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
