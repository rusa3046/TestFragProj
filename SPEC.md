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
     source_channel, score, extracted_at (nullable), raw_json
   - `claims`: id, comment_id, claim_type, subject_kind, raw_subject_text,
     subject_frag_id, object_kind, raw_object_text, object_frag_id,
     sentiment, confidence, evidence_span, evidence_verified,
     extraction_model, created_at
   - `eval_labels`: comment_id, labeled_json, labeler, created_at

   **`evidence_verified`** records whether `evidence_span` was actually
   found in `comments.body` at write time. If the model paraphrases instead
   of quoting, the evidence is fiction and any eval number computed from it
   is meaningless — so the check runs on every write and the result is
   persisted, not assumed. Measured 0% paraphrase across every run so far.

## Taxonomy

Version 2, revised after running v1 over a real r/fragrance sample of 17
posts. Every change below answers an observed defect rather than a
prediction; the v1 sample produced roughly one usable claim in seven.

| Type | Object | Notes |
|---|---|---|
| `SIMILAR_TO` | FRAGRANCE / HOUSE / TAG | Absorbed `REMINDS_ME_OF` |
| `DUPE_OF` | FRAGRANCE | |
| `BETTER_THAN` | FRAGRANCE | |
| `NOTE_DESCRIPTOR` | TAG | New — the largest gap in v1 |
| `OCCASION` | TAG | |
| `AESTHETIC` | TAG | |
| `LONGEVITY` | none | Was `LONGEVITY_COMPLAINT` |
| `PROJECTION` | none | New |
| `DEVELOPMENT` | none | New |
| `REFORMULATION` | none | New |
| `UNMET_PRODUCT_REQUEST` | TAG | |

**What v1 got wrong, and why each change exists:**

- **`LONGEVITY_COMPLAINT` was a magnet.** Anything about something going
  away landed there: a fragrance that "never really develops"
  (development), a reformulation where a note "has disappeared"
  (composition, not skin), and "low projection" (throw, not duration).
  The failure was lexical, not conceptual — which is why the prompt now
  distinguishes them with the exact misclassified quotes.
- **The type name presumed polarity.** "It is now Friday and the scent is
  still lingering… delightful" was stored as a complaint at 0.3
  confidence. `sentiment` is now a separate field and the type names are
  neutral.
- **`REMINDS_ME_OF` never fired.** The one clear case in the sample —
  "I got a bit of a Serge Lutens vibe" — was labelled `SIMILAR_TO` on
  every run. Merged rather than kept as a distinction the extractor cannot
  make. Revisit only with labelled data showing the split is learnable.
- **`object_kind` was derived from `claim_type`**, which forced "Serge
  Lutens" (a house) to be recorded as a fragrance. It is now stated per
  claim and validated against the kinds each type permits.
- **Subjects were sometimes categories** ("skin scents") **or several
  fragrances in one string.** `subject_kind` marks the first so entity
  resolution can skip it; the prompt forbids the second.

`Claim.is_edge` is the single place that decides what the graph is built
from: a comparison type with a FRAGRANCE subject and a FRAGRANCE object.

## Extraction reliability

- **`temperature = 0.0`.** Two identical runs over the same 17 comments
  returned 4 claims and then 8. Prompt changes cannot be evaluated against
  that. Pinning it narrowed the spread to 6/6/7 — residual jitter of about
  one claim, so **treat a change of ±1 as noise**.
- **Structured outputs reject `minLength`, `minimum`, and `maximum`.**
  Adding them made every batch fail while still logging "0 claims
  written". A regression test now walks the schema for them, and a total
  failure logs at ERROR rather than looking like an empty result.
- **Measured cost** on the v2 sample: **$1.14-1.22 per 1k comments** at
  Haiku 4.5 list pricing, roughly half that on the Batch API. 100k
  comments is therefore about $115-122, or ~$60 batched. Output tokens
  dominate, so the figure moves with claim volume — the earlier $0.47
  figure was an artefact of v1 missing most claims.

### Do not tune the prompt without an eval set

A prompt change that looked obviously correct made things measurably
worse, and this is recorded because the reasoning is easy to forget.

Three real defects were observed identically across three v2 runs: a
descriptor emitted with no object, a comma-separated list stored as one
object, and a pronoun stored as a subject. The prompt was amended to
address all three. The result:

| | before | after |
|---|---|---|
| Claims per run | 29 / 29 / 26 | 22 / 18 / 30 |
| Spread | ~10% | ~66% |
| Unverified evidence | 0% | 3.3% |
| Dropped claims | 2/run | 3/run |

Variance rose sixfold, mean recall fell, evidence verification broke for
the first time in the project, and the defect being fixed did not go away
— its error message merely changed. The change was reverted.

The lesson is not that those defects are unfixable. It is that at this
model size, adding conditional rules to a prompt trades against stability
in ways that cannot be predicted by reading the prompt, and that a
three-run sample plus one person's reading is too coarse an instrument to
tune against. **Build the eval set first** — see Deferred decisions.

### Deferred decisions

Recorded so they aren't rediscovered later. None block Phase 1.

- **`evidence_span` is not always a literal substring.** Verification falls
  back to whitespace/case-normalized matching, so a span can be verified
  without being byte-identical, and offset-based highlighting is not
  possible from the stored span alone. If highlighting is wanted, add
  `evidence_start`/`evidence_end`. Safe to defer: `comments.body` is
  retained, so offsets are derivable by backfill and need no re-extraction.
- **Ingest covers comments, not submission bodies.** The richest claim
  density in the sample was a long review post, and `ingest/reddit.py`
  reads comments from submissions while discarding submission selftext.
  Decide whether submissions should be stored as rows too.
- **The eval set has a harness but no labels.**
  `fragrance_graph.evals` exports a template, imports labels, and scores
  precision/recall/F1 per claim type with a fixed 70/30 train/holdout
  split. What is missing is the labelling itself — roughly an hour of
  human judgement for a couple of hundred comments. Until that exists,
  prompt changes cannot be evaluated (see above).

**Resolved by the v1 sample** (kept for the record, since the reasoning
matters more than the outcome):

- `SIMILAR_TO` vs `REMINDS_ME_OF` — merged. The distinction was not
  learnable by the extractor; see Taxonomy above.
- `LONGEVITY_COMPLAINT` subtypes — split into LONGEVITY / PROJECTION /
  DEVELOPMENT / REFORMULATION, with polarity moved to `sentiment`.

### Phase 2 and beyond (not built yet)

- Entity resolution (mapping raw subject/object text to canonical
  `fragrances` rows)
- The similarity graph itself
- Ranking / scoring
- Any web UI
- TikTok or other social sources

If work drifts into these areas while doing Phase 1, stop.

## Constraints

- Official platform APIs only. No scraping of Fragrantica, Parfumo, or
  retailers — their ToS forbid it and this needs to be publicly demoable.
- **Amendment:** the original constraint read "Reddit's public API only".
  Reddit refused API access, so YouTube Data API v3 is the primary source
  (`ingest/youtube.py`) and the Reddit ingest remains in place should
  access ever be granted. The schema anticipated this: uniqueness is
  `(source, source_id)`, and `source_channel` (renamed from `subreddit` in
  migration 0004) holds whatever subdivision the source uses.
- Extraction must be cheap: this runs over 100k+ comments eventually.
- Cost per 1k comments must be logged so it's visible.
