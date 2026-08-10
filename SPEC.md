# Fragrance Similarity Engine — Spec

## What we're building

Given a fragrance, return the fragrances the community says are dupes or
smell similar — ranked, each backed by verbatim quotes and links to the
comments they came from.

**Amendment (2026-08-10).** This previously read "builds a weighted graph"
answering the same question, with weight intended to come from note
overlap. That goal is dropped.

Similarity is **asserted, never computed.** The system does not model what
a fragrance smells like; it extracts what people claimed and counts how
many distinct people claimed it. Two reasons, and the second matters more:

1. Structured note data (top/mid/base pyramids) is only available from
   sites whose terms forbid scraping — see Constraints — so the input for
   note overlap cannot be obtained legitimately.
2. Note overlap answers a question buyers were not asking. Someone
   deciding on a £120 bottle is not served by a similarity score of 0.87;
   they are served by "31 people called this a dupe of Baccarat Rouge 540,
   and here is what nine of them said." The evidence is the product.
   Ranking is only how the evidence gets sorted.

Consequences that follow, and that later phases must respect: evidence
spans and permalinks are load-bearing rather than diagnostic, ranking is
by **distinct commenter count** rather than row count, and a pair with too
few distinct commenters is not a weak result — it is not a result.

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
- **Measured cost.** Two corpora, and they differ by 3x, so quote the one
  that matches the source:

  | corpus | comments | cost / 1k | claims / comment |
  |---|---|---|---|
  | Reddit v2 sample | 17 | $1.14-1.22 | ~1.5 |
  | YouTube, 2026-08-09 | 3,155 | **$0.3656** | 0.441 |

  Output tokens are 69% of the bill, so cost tracks claim volume rather
  than comment volume — Reddit review posts are long and assert several
  claims each; YouTube comments are short and most assert nothing. At the
  YouTube rate, 100k comments is about **$37**, or ~$18 on the Batch API.

  A third of the input bill is avoidable: the system prompt plus JSON
  schema costs ~1,206 tokens on every call, against comments averaging ~54
  tokens of text, so at batch size 20 more than half the input spend is
  re-sending the prompt. Raising the batch size to 40 would cut the total
  ~8%. Not done yet: batch size changes how many comments share a context
  window, which changes extraction behaviour, and that cannot be evaluated
  before the eval set exists.

### The first measured prompt change (2026-08-10)

Sharpening `DUPE_OF` — see the taxonomy note above — measured against 13
human-labelled comments, re-extracted with `--only-labelled --reset`. Two
runs after the change, one baseline before it:

| | before | after (1) | after (2) |
|---|---|---|---|
| OVERALL F1 | 0.50 | **0.62** | **0.62** |
| `DUPE_OF` recall | 0.33 | **0.67** | **0.67** |
| `NOTE_DESCRIPTOR` F1 | 0.00 | **0.67** | **0.67** |
| `BETTER_THAN` F1 | 0.67 | 0.00 | 0.00 |
| SIMILARITY EDGES F1 | 1.00 | 0.91 | 1.00 |
| unverified evidence | 0% | 0% | 0% |
| claims written | — | 22 | 23 |

**Kept.** The target moved and held: `DUPE_OF` recall doubled across both
runs, and overall F1 rose the same amount twice. Variance stayed at about
one claim, unlike the reverted v2 edit which raised it sixfold — the two
runs' OVERALL rows are identical and only the DUPE_OF/SIMILAR_TO split
jitters by one claim between them.

**The edge guard was mis-specified.** "Edges must stay at 1.00" was set
before the run; on a five-edge sample one extra claim moves precision by
0.17, so the guard tripped in run 1 and cleared in run 2 on pure noise. A
threshold that a single claim can trip is not a threshold. Guards on this
eval need to be stated in claims, not in F1, until the labelled set is much
larger.

**`BETTER_THAN` did not regress because of the prompt.** Both runs show
`BETTER_THAN allows object_kind {FRAGRANCE}, got NONE` in the drop log: the
model emitted the claim and validation deleted it. See below.

### Validation drops are now the dominant defect

Across the two runs, **21-24% of every claim the model emits is deleted by
our own validator**, and those deletions account for nearly every remaining
false negative:

| reason | run 1 | run 2 |
|---|---|---|
| `NOTE_DESCRIPTOR … got NONE` | 4 | 3 |
| `DUPE_OF … got NONE` / `got TAG` | 2 | 2 |
| `BETTER_THAN … got NONE` | 1 | 1 |

Two different problems wearing one uniform:

- **Objectless descriptors and comparisons** — the model asserts a
  descriptor with no descriptor, or a dupe with nothing to be a dupe of.
  Dropping is right; the question is why it emits them.
- **`DUPE_OF … got TAG`** — the model wants to say a fragrance is a dupe of
  a *category*. `SIMILAR_TO` already accepts TAG for exactly this reason,
  so this may be the taxonomy being wrong rather than the model.

The drop breakdown used to be printed and then lost. It is now persisted to
`rejected_claims` and readable with `extract.rejects report` / `show`, which
is what made the next section possible: "which comment, and what did it
actually say" no longer costs an extraction run to answer.

### NOTE_DESCRIPTOR became the new magnet — and the fix was reverted (2026-08-10)

Reading the persisted rejections settled what the counts could not. Of six
drops in one run, three were claims the model saw content for and lost, and
three were genuinely unusable:

| comment | text | verdict |
|---|---|---|
| "The OG can be a bit **overwhelming**" | object present | lost |
| "it's just **like water**" | object present | lost |
| "tom ford bitter (dupe lol)" | subject unnamed | correct drop |
| "better than **I imagined**" | object is an expectation | correct drop |
| "Al Haramein keeps making perfect **clones**" | clones of nothing named | correct drop |

The lost ones are the tell. "Overwhelming" is PROJECTION; "just like water"
is SIMILAR_TO. Neither is a note descriptor — yet both were emitted as
`NOTE_DESCRIPTOR` with a null object. **NOTE_DESCRIPTOR had become where
the model lands when it senses a comment says something about a fragrance
but has not worked out what**, exactly as `LONGEVITY_COMPLAINT` did in v1.
The failure is lexical again, not conceptual.

**The fix was attempted in the schema, not the prompt.** The prompt already
said "if a claim would need an object and there is no identifiable one, omit
the claim entirely" — an instruction being ignored, not a missing one, and
repeating it louder is the change that raised variance sixfold. So the
response schema was split into two claim shapes discriminated on
`claim_type`: a type that needs an object got `raw_object_text: {"type":
"string"}` and could not emit the null at all. Split by object requirement
rather than one variant per type, because per-type variants cost 1,703
schema tokens against 474 — on every call, forever, against the cheapness
constraint — to prevent a `DUPE_OF … got TAG` violation seen once in three
runs.

**It worked exactly as designed, and it was still worse.** Three runs on the
13 human-labelled train comments:

| | before | run 1 | run 2 | run 3 |
|---|---|---|---|---|
| `… got NONE` drops | 6 | 0 | 0 | 0 |
| Total dropped | 6 (20.7%) | 2 (7.4%) | 1 (3.7%) | 1 (3.8%) |
| Claims emitted | 29 | 27 | 27 | 26 |
| Claims written | 23 | 25 | 26 | 25 |
| OVERALL F1 | 0.62 | 0.59 | 0.59 | 0.59 |
| SIMILARITY EDGES F1 | 1.00 | 0.91 | 0.91 | 0.91 |
| `LONGEVITY` false positives | 1 | 2 | 2 | 2 |
| `NOTE_DESCRIPTOR` | tp 1 / fn 1 | tp 1 / fn 1 | tp 1 / fn 1 | tp 1 / fn 1 |

The mechanism is not in question. The objectless claim became
unrepresentable and vanished; the model invented nothing to replace it —
emitted claims *fell*, 29 to 26. Reproducible across three runs.

But every eval metric that moved, moved down, and the two rows that did not
move say why:

- **`NOTE_DESCRIPTOR` is unchanged at tp 1 / fn 1.** The premise was that
  "overwhelming" and "just like water" were recoverable content — that
  forced to choose, the model would find PROJECTION and SIMILAR_TO. It did
  not. Those comments were not scored better; they were scored the same,
  by a different route.
- **`LONGEVITY` false positives rose 1 → 2.** The magnet did not disappear.
  It moved to the objectless types, which the schema left free. Closing one
  drain in a taxonomy that absorbs uncertainty relocates the uncertainty.

`SIMILARITY EDGES` falling from 1.00 to 0.91 is the cost stated plainly: the
one metric the product actually sells got worse.

**Reverted.** `raw_object_text` is nullable again on every claim type, with
a test asserting it so the split is not re-applied without reading this.

The generalisable lesson, and the reason this is recorded at length:
**forcing a valid shape does not create knowledge — it converts a visible
failure into an invisible one.** A drop is logged, counted, and inspectable
in `rejected_claims`. A claim coerced into a legal shape is stored as fact
and indistinguishable from a good one. For a product whose entire pitch is
"here is what people actually said", the honest refusal is worth more than
the plausible guess, and the 20.7% drop rate was never waste — it was the
system correctly declining to store things it had not worked out.

The rejections persistence built alongside this experiment is kept. It is
what made the experiment legible, and it paid for itself within one run.

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

### DUPE_OF vs SIMILAR_TO — sharpened, not merged (2026-08-10)

**Resolved.** The trigger fired: on the 13 human-labelled comments in
train, `SIMILARITY EDGES` scored a perfect 1.00 while every single
comparison-type error was this confusion. Three occurrences, all one
directional — the human said `DUPE_OF` where both the extractor and the
Opus drafter said `SIMILAR_TO`, never the reverse.

That direction is the diagnosis. The old definition read *"a cheaper
substitute"*, so both models looked for a price signal; commenters never
state the price gap, because it is carried by the brands. The words they
use instead are "dupe", "clone", "impression of", "interpretation of",
"inspired by" — now `DUPE_SIGNAL_WORDS` in `models.py`, with a test
asserting both the extractor prompt and the label-drafter prompt carry
every one, so the two cannot drift.

The types were **not** merged. "Is this a cheap alternative" and "does this
smell similar" are different buyer questions, and merging would discard a
distinction the product wants. The `SIMILARITY EDGES` line already covers
the case where only the edge matters.

**This is the first prompt change the project has been allowed to make**,
and it changes exactly one thing — unlike the reverted v2 edit, which
addressed three defects at once and made attribution impossible.

#### Historic note — the original observation



The first calibration run surfaced exactly one disagreement between the
human labeller and the Opus drafter, on this comment:

> "Sand+Fog Sweet Rose is a more wearable **interpretation** of DE to my
> nose."

Human: `DUPE_OF`. Drafter: `SIMILAR_TO`. Same subject, same object, same
sentiment — one assertion, typed two ways. The taxonomy defines `DUPE_OF`
as *a cheaper substitute*, and the comment never mentions price; "cheaper"
comes from knowing Sand+Fog is a budget brand. On the text alone the
drafter has the better case.

**Both are edge types, so the graph is identical either way.** The scorer
therefore reports a `SIMILARITY EDGES` line with the two collapsed,
alongside the per-type breakdown — the product's question is whether the
edge was found at all.

This is the same shape as the v1 `SIMILAR_TO` / `REMINDS_ME_OF` split,
which was merged because the distinction was not learnable. Do **not**
merge these on one disagreement. The trigger to revisit is the first real
extractor score: if DUPE_OF/SIMILAR_TO confusion dominates the errors while
the collapsed edge score stays high, the distinction is costing more than
it earns.

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

### Phase 2: entity resolution (in progress)

Mapping raw subject/object text to canonical `fragrances` rows, so that
the graph has things for nodes instead of strings. `resolve/names.py`
does the matching, `resolve/entities.py` the curation and backfill.

Three layers, cheapest first: junk rejection (video timestamps, bare
numbers, stubs), normalised exact match (case, punctuation, accents,
filler words), then fuzzy match above a deliberately high threshold
(0.88). A false merge silently corrupts every edge that touches it,
whereas a miss stays visible in the unresolved report — so the threshold
is set to prefer misses.

Abbreviations are the layer no string comparison reaches: "BR540" and
"Baccarat Rouge 540" score 0.43. That is domain knowledge, and it lives
in curated `fragrances.aliases`.

**Exact matching runs before the junk check**, on purpose. `540` is a
bare number and the junk rule rejects it, but it was the single most
common way commenters wrote Baccarat Rouge 540 in the first live corpus.
A curated alias is a person stating a fact; the junk rule is a guess
about text, and the guess must not overrule the fact. Junk still blocks
fuzzy matching, which carries no such warrant.

Curation is human work by design. `report` ranks unresolved mentions by
frequency, so effort goes where the corpus actually is. Workflow:

    report  →  add / alias  →  backfill  →  edges

### Phase 3 and beyond (not built yet)

- **The query layer.** `similar_to(conn, fragrance_id)` — the missing
  product surface. `resolved_edges()` takes no fragrance argument, so
  there is currently no way to ask the question the project exists to
  answer. DUPE_OF and SIMILAR_TO are symmetric (an A→B edge must surface
  when querying B); BETTER_THAN is a preference claim and stays separate.
  Ranked by distinct commenter count, so one prolific commenter cannot
  manufacture an edge.
- **Sentiment rollup** from claim level to fragrance level.
- **Commerce.** `products` / `retailers` tables kept separate from
  `fragrances` (one fragrance, many products), populated from affiliate
  network product feeds — CSV/XML, never scraping. Feed product names get
  matched with `resolve/names.py`; this is the same entity-resolution
  problem, second instance.
- **Comparison pages.** One static page per pair, generated from the query
  layer, gated at 3+ distinct commenters. Thin pages are worse than none.
- Any web UI, TikTok or other social sources.

**Trust requirements, enforced in code:** ranking never considers
affiliate status or commission — there is a test asserting result order is
identical with product links stripped. Results always include options with
no affiliate relationship. Affiliate links are disclosed inline at the
link, not in a footer. Pages are text only: naming a fragrance identifies
it, using brand imagery borrows its authority.

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
