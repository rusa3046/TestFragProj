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
  | YouTube, 2026-08-11 | 864 | **$0.4410** | 0.436 |

  Output tokens are 69% of the bill, so cost tracks claim volume rather
  than comment volume — Reddit review posts are long and assert several
  claims each; YouTube comments are short and most assert nothing. At the
  YouTube rate, 100k comments is about **$44**, or ~$22 on the Batch API.

  **The rate moves with the question, not just the source.** The three
  runs behind the 2026-08-11 row spanned $0.3730-$0.5020 per 1k on one
  source and one prompt, rising as the searches narrowed from "fragrance
  dupe" to named bottles like "cedrat boise". A thread about one specific
  fragrance asserts more claims per comment than a general one, and
  output dominates the bill. Since targeted queries are exactly what the
  publishing gate needs — it wants two distinct creators per pair — the
  cheap searches and the useful ones are not the same searches. Budget
  for the top of the range, not the average.

  A third of the input bill is avoidable: the system prompt plus JSON
  schema costs ~1,206 tokens on every call, against comments averaging ~54
  tokens of text, so at batch size 20 more than half the input spend is
  re-sending the prompt. Raising the batch size to 40 would cut the total
  ~8%. Not done yet: batch size changes how many comments share a context
  window, which changes extraction behaviour, and that cannot be evaluated
  before the eval set exists.

### `NOTE_DESCRIPTOR` is the new magnet (2026-08-11)

Measured, not predicted. Every claim dropped in a 47-comment run — 5 of 16
emitted, 31% — failed the same way: `NOTE_DESCRIPTOR` with no object at
all. Reading the five rejects back out of `rejected_claims`:

| comment says | what it actually is |
|---|---|
| "layton is **soft asf**" | projection, and `PROJECTION` takes no object |
| "I rather not smell like **every guy on the party**" | ubiquity — no slot exists for it |
| "smells like **fruity pebbles & Vicks vapor rub**" | the descriptors are present and were dropped |
| "bought Layton… maybe it's fake… I love it" | no descriptor at all; the claim is invented |

This is the v1 `LONGEVITY_COMPLAINT` failure again, in a new type: a
lexically broad category collecting anything that sounds like description.
The fix that worked then was naming the misclassified quotes in the
prompt, and the four above are that evidence, kept here for when the eval
can measure a change.

**The tell is `object_kind`.** For "layton is soft" the model emitted
`NONE`, which is *invalid for `NOTE_DESCRIPTOR` and valid for
`PROJECTION`* — it chose the wrong type while emitting the object kind the
right type requires. So this is a type-selection failure, not a
field-filling one.

**A deterministic fix was considered and ruled out by measurement.**
Because `object_kind` is determined by `claim_type` for 11 of 12 types,
the hypothesis was that the model was mislabelling a redundant field and
the claims could be recovered by coercing the kind. `rejects recoverable`
tests that against stored payloads without paying to extract again, and
answered 0% coercible: every one of these has no `raw_object_text` to
keep. The content is in the comment, not in the payload, so nothing can
be recovered by re-parsing. Cost of finding out: $0.02.

Two smaller observations from the same five:

- One comment produced the **identical claim twice**, so duplicate
  emission is real and currently costs output tokens rather than
  correctness.
- One subject was `'$300 vicks vapeorub'`, which is not a fragrance. The
  subject extraction fails on the same comments the type selection does.

**Not fixed.** The eval is still 13 comments, and the ±1 noise floor
recorded above means a change this size cannot be distinguished from
drift. These rejects are, however, exactly the labelled examples the eval
needs — which makes growing it the unlock for this and for the batch-size
question above.

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

### Post-revert baseline, and why it is not good news yet (2026-08-10)

The 50 labelled comments were re-extracted with the reverted schema. The
result is the project's best score, and it beat the prediction on every
line, which is the reason to read it carefully rather than bank it.

| | predicted | measured |
|---|---|---|
| `SIMILARITY EDGES` F1 | 1.00 | 1.00 |
| OVERALL F1 | 0.62 | **0.75** |
| `DUPE_OF` recall | 0.67 | **1.00** |
| Drop rate | ~20% | **6.7%** (2 of 30) |
| Unverified evidence | — | **0.0%** |
| Cost | — | $0.0212 for 50 comments |

Reproduces byte-for-byte from a clean clone of `data/corpus/`.

**What this establishes.** `SIMILARITY EDGES` at 1.00 for the third
independent time, across the `DUPE_OF` sharpening, the anyOf experiment,
and the revert. Every edge found, none invented. `DUPE_OF` and
`SIMILAR_TO` both at perfect precision and recall — the sharpening works.
Evidence verification at 100%, against 91.4% on the full corpus run.

**What it does not establish.** OVERALL is `tp 6, fp 2, fn 2`; the two runs
that scored 0.62 were `tp 5, fp 3, fn 3`. That is **one claim**. On 13
labelled comments one claim is worth ~0.13 of F1, and the code state here
is identical to the state that scored 0.62 twice. The honest reading is
that run-to-run variance is wider than two runs suggested, not that the
extractor improved. The same applies to the drop rate: 20.7% to 6.7% with
no schema change is four claims.

**This is the eval telling you it is too small.** Every conclusion above is
one claim from reversing. The instrument, not the extractor, is now the
limiting factor — 13 verified comments in train, of a 50-comment sample
that was drafted but never fully reviewed.

Do not tune anything further against 13 comments. Finish the labels first.

**The four surviving errors**, three of them unchanged across every run
ever measured:

- `LONGEVITY` fp 1 — the magnet type, still absorbing uncertainty
- `NOTE_DESCRIPTOR` tp 1 / fp 1 / fn 1 — identical in every run to date
- `BETTER_THAN` fn 1
- Both drops are `DUPE_OF … got NONE` — the "Al Haramein keeps making
  perfect clones" shape, a dupe claim naming no target. Taxonomy gap, not
  a model error. See Deferred decisions.

### The polarity re-extraction, and a badly chosen threshold (2026-08-10)

Full corpus re-extracted with `polarity`. 3,155 comments, $1.30, 0 failed
batches. **The fix worked and the eval fell**, and untangling those took
longer than building it.

#### The fix worked

| | before | after |
|---|---|---|
| Denial-shaped evidence flagged | 34 | 38 |
| Stored as DENIED (correct) | 0 | **35 (92%)** |
| Denials the regex never flagged, found by the model | — | **32** |

The real denial count is ~67, not the 36 enumeration found. The model
catches roughly twice what the pattern can, which retires the worry that
`extract.polarity audit` was measuring its own regex.

**It costs four false denials**, read by hand out of the 32:

| quote | marked | actually |
|---|---|---|
| *"Maison Alhambra Delilah **is identical**…"* | DENIED | an assertion |
| *"It's **exactly the same** as the original Exclusif"* | DENIED | an assertion |
| *"No thats **a clone of pegasus** mate"* | DENIED | "No" opens it; it asserts |
| *"you said it was amazing, **better than delina**"* | DENIED | reported speech |

Four genuine edges lost against ~63 backwards ones excluded. Favourable,
and a new failure mode that did not exist before.

#### The eval fell, and the threshold was the wrong instrument

OVERALL F1 0.75 -> 0.50, against a pre-registered "stop below 0.65". By
that criterion the run failed. The criterion was badly chosen, and that is
worth recording more than the number is.

Comparing claim types across the two corpora by identical `evidence_span`:

| movement | claims |
|---|---|
| `SIMILAR_TO` -> `DUPE_OF` | **37** |
| `SIMILAR_TO` -> `NOTE_DESCRIPTOR` | 7 |
| `NOTE_DESCRIPTOR` -> `SIMILAR_TO` | 5 |
| `DUPE_OF` -> `SIMILAR_TO` | 4 |

**`SIMILAR_TO` and `DUPE_OF` are both edges.** The dominant movement is
invisible to the graph and invisible to `SIMILARITY EDGES` — which is the
line that exists precisely to see past this distinction, and which SPEC
already records as a confusion the project tolerates rather than merges.

OVERALL F1 mixes that distinction back in. Setting the stop-threshold on
it meant the test fired on exactly the noise it was designed to ignore.
**A threshold on an aggregate that averages over a known-tolerated
ambiguity cannot detect anything else.** Future gates go on
`SIMILARITY EDGES` and on the specific defect being fixed.

The graph itself:

| | before | after |
|---|---|---|
| Similarity claims | 499 | 521 |
| Of which usable as edges | ~463 (36 were backwards) | **456, denials excluded** |

Flat in size, materially more correct in content.

#### Two real regressions, neither worth reverting for

**`DUPE_OF` is over-firing.** Of the 37 claims that moved from
`SIMILAR_TO`, only 14 carry a `DUPE_SIGNAL_WORD`. Reading the other 23,
several are plainly similarity rather than substitution — *"how close this
is to Oud Wood"*, *"he asked me are you wearing Creed Aventus"*. This is a
hand reading, not a measurement: on the 13 labelled comments `DUPE_OF`
scored precision 1.00, so the eval does not see it. It matters because
"dupe" is a stronger claim than "similar" and page copy will repeat it.

**`NOTE_DESCRIPTOR` leaked 7 similarity claims** — but four of those are
*improvements*: *"Layton smells like a dentist"*, *"smells like cough
medicine"*, *"like hawaiian tropic tanning spray"* are descriptors, not
fragrance comparisons, and were wrong before. The genuine losses are
*"Eternal Oud … (smells like Grand Soir)"* and two copies of *"fusion of
ultra male and tabbaco vanilla"*, where the object is a real bottle.

That single Grand Soir row is the entire `SIMILARITY EDGES` regression
(1.00 -> 0.89, fn 1). One claim.

**Kept.** The change removes ~63 backwards edges and costs one real edge
plus four false denials, on a graph that stayed the same size. The
per-type churn is the DUPE_OF/SIMILAR_TO ambiguity the taxonomy already
declines to resolve.

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

### Denials are stored as assertions (2026-08-10) — FIXED IN CODE, corpus pending

*"I just try latafa it is nothing like angel share"* is stored as
`DUPE_OF latafa → angel share, NEGATIVE`. The commenter is **denying** the
similarity; the graph records them asserting it, and `query.similar_to()`
will quote them as supporting evidence for the edge they rejected.

Measured across the 499 similarity claims in the corpus:

| | count | share |
|---|---|---|
| Denials stored as edges | 36 | 7.2% |
| Conditionals (*"if X smells like Y then…"*) — assert nothing | 3 | 0.6% |
| Genuine edges with negative tone (*"worst dupe of (540)"*) | 5 | 1.0% |
| Misfiled descriptors (*"smells like a dentist"*) — object is a TAG, never surfaced | 11 | 2.2% |

**Roughly one similarity edge in thirteen says the opposite of what the
person wrote.** For a product that sells verbatim evidence, this outranks
every F1 number recorded above: a wrong claim type is a mislabelled fact,
but a denial stored as an assertion is a fabricated one, quoting a real
person against themselves.

**Sentiment is not the fix.** All 36 denials are NEGATIVE, but so are 5
genuine edges. Filtering `NEGATIVE` from ranking trades 36 wrong edges for
5 right ones — a defensible 7:1, but it silently discards "worst dupe of
540", which is exactly the kind of thing a buyer wants told to them.

**The eval is not blind to this; it is too small to have met it.**
`docs/LABELLING.md` already instructs labellers to drop denials, so a
denial extracted as `SIMILAR_TO` scores as a false positive. At ~7%
incidence and 13 labelled comments, the expected count in the sample is
0.4. `SIMILARITY EDGES 1.00` was measured on five edges, none of them a
denial. The score is honest; the sample simply has not encountered the
failure mode that most threatens the product.

This is the strongest argument for finishing the 35 unreviewed labels —
not because labels repair denials, but because at 35-50 comments the eval
starts containing them and the number starts meaning something.

**Not fixed with a louder prompt.** That is precisely the move that raised
variance sixfold. `polarity` (ASSERTED / DENIED) is a new claim field —
migration 0007 — because negation is a property of the claim, not of its
sentiment, and overloading `sentiment` to carry both is the same
collapse-two-facts-into-one-field error as v1's `LONGEVITY_COMPLAINT`.

**Denials are stored, never dropped.** "Nine people say Khamrah is nothing
like Angels' Share" is a fact a buyer wants. They are excluded from edges
(`Claim.is_edge`, and `polarity = 'ASSERTED'` in every ranking query), not
deleted.

#### Verified by enumeration, not by F1

The eval cannot adjudicate this: at ~7% incidence and 13 labelled
comments, the expected number of denials in the sample is 0.4. But denial
language is lexically narrow, so the failure is **enumerable** —
`extract.polarity audit` flags comparison claims whose evidence reads as a
denial and reports how many were stored as assertions. That turns "wait
for a bigger eval" into "look at thirty-four rows".

The pattern is a recall instrument, tuned to over-flag: a false flag costs
a glance, a missed denial ships. **It never writes polarity.** Only the
model does, so the corpus never records this project's regex guesses as
though a model had judged them. `audit` exits non-zero while any denial is
live, so it gates rather than informs.

It also reports the other direction — claims marked DENIED that the
pattern did not flag. Those are either phrasings worth adding, or the
model denying ordinary claims, which is a silent recall loss no other
check would catch.

**Baseline before re-extraction: 34 flagged, 0 caught (0%).** The corpus
predates the field, so every one is currently wrong. That is the number
the re-extraction has to move.

**One measurement changed, not just a result.** `evals.score` now excludes
DENIED claims. `docs/LABELLING.md` already tells a human to drop denials,
so scoring a correctly-marked denial would make getting it right read as a
false positive and the fix look like a regression. Open question: whether
denials should eventually be labelled and scored in their own right — they
are real information, and nothing currently measures whether the extractor
finds them.

### Fragella is a dictionary, never a similarity source (2026-08-10)

An external fragrance API (Fragella) is under evaluation for resolving
mention text to canonical bottles. Two of its endpoints are off limits and
this is a product boundary, not a preference:

| endpoint | use |
|---|---|
| `/fragrances?search=` | **allowed** — is "Khamrah" a real bottle, and whose? |
| `/brands/{brand}` | **allowed** — same question from the other end |
| `/fragrances/similar?name=` | **forbidden** |
| `/fragrances/match?accords=` | **forbidden** |

The response payload needs the same fence, because the allowed endpoint
returns far more than a name:

| field | use |
|---|---|
| `Name`, `Brand`, `Year` | **allowed** — this is the dictionary |
| `General Notes`, `Main Accords` | **forbidden** — computed-similarity inputs |
| `Image URL`, `Image URL Transparent` | **forbidden** — trust rules are text only |
| `Longevity`, `Sillage`, `rating`, `Price Value` | **forbidden** — see below |
| `Price` | **forbidden** — not a retailer price; Phase C feeds carry the real one |

`Longevity: "Moderate"` and `Sillage: "Moderate"` deserve their own note,
because they look harmless and are not. They are aggregated opinion, and
they would sit on a page beside `LONGEVITY` and `PROJECTION` claims
extracted from named commenters with quotes and permalinks. A reader
cannot tell which is which, and the aggregate is smoother and more
confident-looking than eleven people disagreeing — so it wins by default.
That is the "an API said this" failure arriving through a side door.

`Purchase URL` is a fourth trap and the docs name it plainly: "a direct
**affiliate link**". Shipping it would hand Fragella the commission Phase C
exists to earn, and would put an undisclosed affiliate link on a page whose
trust rules require disclosure inline at the link.

The dictionary needs three fields. Store three.

#### What the published methodology settles (2026-08-10)

Reading the API docs removed the remaining doubt about using this as a
similarity source. Fragella states:

> "For fields where this data is sparse, we employ machine learning models
> to analyze related data and **predict missing values (like Accords and
> Notes) with an estimated 80% confidence level**."

alongside a claimed fill rate of "Notes & Main Accords: 100% complete."
A total fill rate over an admittedly sparse source means the gaps are
model output. `/fragrances/similar` then scores "based on shared accords
(primary) and notes (secondary)" — **computed similarity over partly
imputed data**, two inference layers from anything a person said.

Set against `evidence_verified`, which checks a quoted span against the
comment body at write time, this is not a close comparison. It is also the
answer to "does Fragella make the corpus redundant": no, and the docs are
the argument.

#### Ubiquity is the commercial reason, not just the principled one

Fragella's own product is a `<script>` tag that renders notes, accords,
ratings and bottle images into any Shopify or WooCommerce store, billed per
shopper pageview. Their data is designed to be everywhere.

**Differentiation cannot be built on something sold as a drop-in widget.**
A page showing Fragella data competes with every store that pasted two
lines of HTML. A page showing what named people wrote, with links, competes
with nothing, because nobody else has the corpus.

#### One field earns its keep beyond the dictionary

`Popularity` (tiers by user count: "Very high" = 1,000+ users) and
`/brands/:brandName?limit=50` are usable for **ingest targeting** — listing
what is popular, then checking which of those the corpus holds no claims
about, and pointing YouTube collection at the gaps. That closes the bias
recorded in `data/corpus/PROVENANCE.md`, where six of eight search queries
contained "dupe" and the corpus skewed heavily to Parfums de Marly.

This uses Fragella to decide *what to go and collect evidence about*. It
never puts their data in front of a reader, which is the line that
matters.

The forbidden two compute similarity from notes and accords. That is
precisely the approach this SPEC dropped, for reasons recorded at the top:
similarity here is asserted by people, and the evidence is the product. A
result sourced from an accord overlap cannot be backed by a quote, so it
would turn "31 people said this" into "an API said this" and delete the
only thing that distinguishes this from every other fragrance site.

The temptation will be real, because those endpoints return results for
pairs the corpus has no claims about. Filling a thin page with computed
similarity is exactly the failure the 3-commenter threshold exists to
prevent.

**One-time enrichment, not a runtime dependency.** Whatever the API
returns is written into `fragrances` and committed to
`data/corpus/fragrances.jsonl`. If the service changes terms, raises
prices, or disappears, the dictionary survives — and the graph never
depends on a third party being up.

**Open before adopting:**

- Coverage of the clone houses the corpus actually discusses. Run
  `scripts/probe_fragella.py` first; it costs 10 of the free tier's 20
  monthly requests and reports designer and clone-house hit rates
  separately.
- Terms of use for storing and redistributing canonical names and brands.
  Internal normalisation is one question; putting their data on a public
  page is another, and the answer must be checked before Phase D.

### Two ways "3 people said this" lied (2026-08-10)

The first real query output exposed both. Neither is an extraction defect;
both are counting defects, and on a product whose entire claim is *how many
people said it*, they matter more than F1.

**Rows share people.** Results group by (other fragrance, claim type), so
Aventus/CDNIM rendered as `3 people` DUPE_OF, `2 people` SIMILAR_TO,
`1 person` BETTER_THAN. A reader adds those to six. It is **five** — one
commenter called it both a dupe and similar. `Related.pair_commenters` now
carries distinct people across every claim type for the pair, and that is
the number a page should lead with.

**Three commenters can be one comment section.** Montblanc Explorer /
CDNIM showed three distinct commenters, all from video `ZOd2QEVJX8c` —
possibly replying to each other. "3 people said this" implies three
independent observations and a single thread cannot support that.
`Related.sources` counts distinct creators (the uploading channel), and
`min_sources` filters on it.

Measured on the eight resolved pairs: **two of eight are single-source.**
A quarter of the graph's current pairs would have implied a consensus that
does not exist.

`min_sources` defaults to 1, so nothing is hidden by default and the
number is always visible. **Phase D should generate at `min_sources >= 2`
alongside the 3-commenter bar** — a thin page is worse than no page, and a
page built from one comment section is thinner than its count suggests.

Both filters are claim-quality, not commercial, which the trust test
asserts by naming the full parameter set: neither can express "only
fragrances we can sell".

### Commerce: feeds, not scraping (2026-08-10)

`products` and `retailers` (migration 0008), a feed importer, and a
template-driven link builder. Nothing real has been imported: the only
feeds read so far are the invented fixtures in `tests/fixtures/feeds/`.

**Matching is `resolve/names.py`, not a second implementation.** Mapping
`Lattafa Khamrah EDP 100ml Spray Unisex` to a bottle is the same problem as
mapping `BR540`, and two definitions of "same fragrance" drifting apart —
with the commerce one choosing which buy link appears on a page — is a
worse outcome than any accuracy loss from sharing one.

**Which words may be stripped from a feed name is the load-bearing
decision.** `EDP`, `EDT`, `EDC` and the phrase *eau de parfum* are never
part of a fragrance's name, so they come off before matching. `Elixir`,
`Extrait`, `Parfum` and `Cologne` are each a concentration *and* a common
part of a name, so they stay:

| stripping it | consequence |
|---|---|
| `Elixir` | `Dior Sauvage Elixir` becomes `Dior Sauvage` — a £110 bottle's buy link under a £75 one |
| `Parfum` | `Parfums de Marly` stops existing, taking the largest house in this corpus with it |

The cost is paid on purpose: `Baccarat Rouge 540 Extrait de Parfum` does
not match the curated `Baccarat Rouge 540` and lands in the unmatched
report. That is the same trade as the 0.88 fuzzy threshold — a visible miss
over a silent false merge — and here a false merge sells someone the wrong
bottle rather than corrupting a count.

**Measured on the fixtures**: 13 of 20 rows matched (65%) against the 16
seeded fragrances — 9 of 14 in the CSV, 4 of 6 in the XML. The seven
misses are four distinct causes, one of which (`PDM Herod`) is an
abbreviation only a curated alias can reach, exactly as with mention text.

**Unmatched rows are stored, never dropped**, and matching re-runs on every
import, so curating a name later resolves rows already imported with no
re-download. Same bargain `claims.subject_frag_id` makes.

**Products are not in `data/corpus/`.** The corpus holds what cost API
quota, money or human judgement. Feed rows cost none of those, go stale in
a day, and are a retailer's catalogue rather than something irreplaceable.
A test asserts the export is byte-identical before and after an import, so
commerce cannot make the committed files churn.

**The trust rules are now measured rather than asserted.** The end-to-end
ranking test the earlier structural guard asked for exists: the same
corpus, with three listings on the *weakest* result and none on the
strongest, returns results identical to the same corpus with the product
rows deleted — and identical again after the retailer itself is removed.
Result-filtering is tested separately, because filtering to sellable
options is the version of this failure that leaves the order untouched.

### The corpus is the new-release detector (2026-08-10)

A daily loop wants to know what fragrances are new. The obvious answer is
a release feed — Fragella by brand, or a directory site like Basenotes.
Rejected, for two reasons, the second stronger than the first.

**Basenotes has no public API**, so using it means scraping, which
Constraints already forbids for Fragrantica and Parfumo on identical
grounds: their terms prohibit it and this project must stay publicly
demoable. Fragella has an API but no recency endpoint — `/brands/:name`
returns a catalogue, not a timeline.

**And a release feed answers the wrong question.** A fragrance launched
yesterday has no YouTube discussion; review videos take weeks and comment
threads accumulate over months. A feed therefore delivers bottles that
*cannot yet produce an edge*, which is the same error as curating from a
catalogue rather than from mentions: the dictionary does not create edges,
comments do.

`resolve.entities report` already is the detector. It ranks unresolved
mentions by frequency, so a new release climbs it exactly when people
start discussing it — which is the first moment it can become an edge.

The loop is therefore demand-driven, not supply-driven:

```
1. YouTube: search fragrance content broadly
2. ingest -> extract
3. resolve.entities report  ->  newly frequent, unnamed
4. Fragella: resolve those names        <- the job it is actually good at
5. backfill -> export -> commit
```

One fewer dependency than the supply-driven design, no scraping question,
and Fragella is left doing the only thing it does better than the corpus:
answering "people started writing 'Qahwa' — what is that, and whose?"

### Which sources are live, and which never will be (2026-08-10)

| source | status |
|---|---|
| YouTube Data API v3 | **live** — the entire corpus, 3,155 comments |
| Anthropic API | **live** — extraction and eval-label drafting |
| Fragella | **live** — mention text to canonical bottle, names and brands only |
| Reddit | **dead.** API access refused to this project |
| Affiliate feeds (Rakuten, ShareASale) | built and tested against fixtures; no account opened |
| Fragrantica, Parfumo, Basenotes | **never.** No public API; scraping breaches their terms |

**Reddit code was deleted rather than left dormant.** `ingest/reddit.py`
held the generic `ingest()` that YouTube, the seed loader and every test
imports, alongside PRAW paths that could not run. That put the codebase's
most-imported function in a module named after its one broken source, and
a reader reasonably concluded Reddit was still part of the pipeline.

The writer moved to `ingest/store.py`, the PRAW functions and the `praw`
dependency were removed, and the single test covering PRAW object shaping
went with them. Code that cannot run is worse than absent code: it reads
as an option, and it invites someone to "fix" a path that has no
credentials to fix it with.

`scripts/probe_fragella.py` was deleted for the same reason. It existed to
answer one question — does the catalogue cover the Middle Eastern clone
houses this corpus discusses — and it answered yes. `resolve.enrich` now
makes the same call for real, so the probe was a second copy of a request
already under test.

### Phase D: the gate is measured on the pair (2026-08-11)

Comparison pages exist (`pages.py`), gated as this SPEC required: 3+
distinct commenters **and** `min_sources >= 2`. Building it surfaced a
question the requirement did not answer — *three commenters and two sources
of what?* — and the two available answers disagree on real data.

`Related` is grouped by `(other fragrance, claim type)`, so `sources` counts
videos behind **one claim-type row**. `pair_commenters` already counts
people behind **the whole pair**, because SPEC recorded that rows share
people and readers were summing them. Rows share videos for exactly the same
reason.

Gating a row-scoped source count beside a pair-scoped commenter count would
have printed page headings like *"5 people across 2 creators"* where the two
numbers count different things — the original defect, re-introduced one
field over. So `pair_sources` was added alongside `pair_commenters`, and the
gate reads both.

**It is not cosmetic.** On the committed corpus the scopes disagree on 8 of
21 candidate pairs, and one changes gate status: Club de Nuit Imperiale <->
Delina Exclusif is 3 people across 2 creators, which is precisely what the bar
asks for, and a row-scoped check refused it.

#### Counting a pair from one end under-counts it

A second, sharper version of the same error. `EDGES_SQL` answers "what
relates to X", and its inbound arm is restricted to the symmetric types on
purpose: an inbound `BETTER_THAN` means someone said the *other* bottle
wins, and surfacing it as a recommendation for X would turn a fragrance's
critics into its endorsements.

Correct for a query. Wrong for a page, which is about a pair rather than
about one end of one. Aventus/CDNIM counts **5 people asked from Aventus and
4 asked from CDNIM**, because one commenter's "Aventus beats CDNIM" is
invisible from the CDNIM side. Both orderings describe the same five people.

`query.pair_stats(conn, a, b)` counts them once, direction-blind and
claim-type-blind. It is a separate query rather than a flag on `EDGES_SQL`,
because the two are answering different questions and collapsing them would
put the directional guard one boolean away from being switched off.

#### What a page may contain

The trust rules were stated for a system that emitted no markup at all, so
one of them needed re-grounding rather than restating. "Nothing in the
codebase emits markup" was evidence for **text only**, not the rule itself;
`pages.py` now emits markup, so the rule is carried by a test asserting no
generated page contains `<img`, `<svg` or `background-image`.

Quotes are escaped. Comment text is written by other people and reaches a
page verbatim by design — that is the product — so it is escaped rather than
trusted, and a comment containing `<script>` renders as the characters that
person typed.

Pages are not committed, for the reason products are not: no quota, no
money, no judgement, and a pure function of `data/corpus/`. Output is
byte-stable, so a diff under `site/` could only mean the corpus moved.

### The unattended loop: a hard cap and a narrow auto-curation rule (2026-08-11)

`daily.py` implements the demand-driven loop specified above. Two decisions
in it are worth recording, because both are about what an *unattended*
process is allowed to do.

#### The cap is a hard stop, enforced between batches, backed by a file

$1/day. The risk an unattended loop carries is not one expensive day — it
is a cheap mistake repeating on a schedule — so the cap raises rather than
warns, and `extract()` takes an `on_spend` callback that can stop the run
mid-way. A pre-flight estimate cannot see a batch that costs more than
projected, and this SPEC already describes the estimator as an order of
magnitude rather than a quote.

Stopping mid-run is safe here and only here: a batch commits before the
callback fires, and anything unreached still has `extracted_at` NULL, so
tomorrow resumes rather than re-paying or skipping.

**The ledger is `data/spend.jsonl`, and it is committed.** Every scheduled
run gets a fresh container — repo cloned, database rebuilt from
`data/corpus/`, filesystem reclaimed. A cap tracked in the database or in
`/tmp` therefore resets every run, turning "$1 per day" into "$1 per run",
which is the one reading that makes the cap useless exactly when something
is looping. It is committed for the same reason the corpus is: it records
money actually spent and cannot be regenerated. Dates are UTC, because a
cap whose window moves with the runner's timezone is not a cap.

#### Auto-curation takes only the rows with no decision in them

The operator asked for auto-approval on corroboration plus a summary,
rather than a review queue. The rule is the narrowest defensible one, and
it is expressed entirely in `corpus_mentions` — the signal `docs/CURATION.md`
already teaches as the thing that settles a flanker:

| `corpus_mentions` | meaning | action |
|---|---|---|
| `-1` | the proposed name adds no word — it *is* the plain bottle | auto-approve |
| `0` | a flanker whose distinguishing word nobody wrote | hold; the answer is in `alternatives` |
| `n` | a flanker people genuinely discuss | hold; it likely wants its own entry |

Plus: `confident` must hold, the catalogue must actually have returned a
name and brand, the mention must clear 3 slots, and an explicit human
`approved` is never overruled in either direction.

**This will be wrong sometimes, and that is accounted for.** A
hand-curated entry called "confident" named the wrong house — two houses
ship a Perseus — which is ~6% on carefully checked entries; automatic
curation will do worse. What keeps a bad merge off a page is not this rule
but the Phase D gate: 3 distinct commenters across 2 creators. The rule only
has to be good enough that the gate is not carrying the whole load alone.

### Videos are not independent samples (2026-08-11)

`min_sources >= 2` was written against one failure: three commenters in a
single comment section, possibly replying to each other, reading as three
independent observations. It does stop that. It does not stop the failure
one level up.

`data/corpus/PROVENANCE.md` recorded which search query surfaced which
video, as prose. Read as data, it says this about the six pairs that
currently publish:

| pair | people | creators | queries |
|---|---|---|---|
| Detour Noir <-> Layton | 9 | 6 | 2 |
| Angels' Share <-> Khamrah | 9 | 3 | **1** |
| Layton <-> Dusk | 8 | 4 | **1** |
| CDNIM <-> Aventus | 7 | 4 | 2 |
| Aventus <-> Explorer | 5 | 3 | **1** |
| Royal Bleu <-> Layton | 5 | 3 | **1** |
| Imperiale <-> Delina Exclusif | 3 | 2 | **1** |
| Lalique White in Black <-> Layton | 3 | 2 | **1** |

The table above is the **first 24 videos**, where every video has a
retrieval record: **four of six rested on a single query**. That is the
clean measurement, and it is the one to reason from.

On the merged 39-video corpus the counts are **lower bounds**. 15 videos
were ingested by runs predating discovery tracking; their queries are not
recoverable, and inventing plausible ones would be the fabrication refused
everywhere else here. Only 1 of 8 pairs is confirmed single-query and 7 are
unknown — which is worse than it sounds, not better, because unknown is
where a narrow seed would hide. `PairEvidence.videos_without_provenance`
carries the gap and the CLI prints it, so a lower bound can never be read
as a measurement.

This is also why the gate stays off: raising a bar against a lower bound
would unpublish pairs for lacking evidence about our own bookkeeping. Three different `parfums de marly
layton dupe` videos are three comment sections — the video bar is honestly
satisfied — but they are three rooms in which the same question was put to
an audience gathered for that question. The independence the headline
number implies is not there.

Query diversity is the *deterministic* version of this concern, which is
why it is built first. Whether a given comment was spontaneous or prompted
by its video's framing is a fuzzier question needing a classifier and an
eval; whether two different searches found the evidence is a fact.

**The gate is off by default (`MIN_QUERIES = 1`).** On the fully
provenanced 24-video corpus, enforcing 2 cut six pages to two, and that number cannot distinguish two opposite diagnoses:
the edges are weak, or the seeds were narrow. Six of the eight seed queries
contain "dupe", so narrow seeding is the live hypothesis and broadening
discovery is the fix that should be tried first. Measured and shown now;
enforced once the seeds are broader.

`queries == 0` means no retrieval record, not narrow retrieval. The gate
treats it as unknown and passes it, so raising the bar cannot silently
unpublish everything ingested before provenance existed.

#### The creator bar counted uploads, not creators

A related error, found while wiring the above and fixed with it. Pages have
said "N creators" since the wording change on 2026-08-11, on the stated
grounds that `source_channel` holds the uploading channel and the
`video_id` fallback had never fired.

The fallback had never fired in the *other* direction. The expression was
`coalesce(video_id, source_channel)`, which returns the video whenever a
video is present — and all 4,866 comments carry one, so the gate counted
**uploads** while the page said **creators**. The corpus holds 39 videos
across 29 channels, so the two genuinely differ.

The argument behind the wording was right: two uploads by one YouTuber are
one audience framed by one person, which is the independence this SPEC
wanted. So the code now does what the page already claimed — the gate reads
distinct channels. No page changes on today's corpus, because every
publishable pair happens to have one video per channel; the defect was
latent rather than live, and this is the cheapest moment to close it.

The lesson is narrower than "check your SQL": the test added with the
wording change pinned *the word on the page*, not the number behind it. A
test that asserts rendered text cannot notice that the text has become
true by luck. `test_two_videos_by_one_creator_are_one_source` pins the
counting instead.

#### Schema: discovery is many-to-many, on purpose

Migration 0009 adds `videos` and `video_discoveries`, and promotes
`comments.video_id` out of `raw_json` — the same move as `author_id` in
0006, for the same reason: every independence count was reaching for it
through an unindexed, source-specific JSON path in the hottest query.

A single `retrieval_query` column on comments would have been simpler and
wrong. The same video is legitimately found by several queries, and later
runs re-find videos already known; one column has to pick one and discard
the rest, destroying the count the table exists to support.

`video_discoveries` is committed to `data/corpus/` because it is
unrepeatable — re-running a search next week returns a different ranking,
so a discovery not recorded at search time cannot be reconstructed. Titles
are committed for the weaker reason that a deleted video takes its title
with it.

#### Transcripts stay out, and OAuth does not change that

Reviewer statements in the video itself are richer than most comments, and
they are not obtainable. `captions.list` and `captions.download` are gated
on being the video's **owner**, not merely authenticated — so no OAuth
scope available to this project unlocks them. Every working alternative
(`youtube-transcript-api`, `yt-dlp`, transcript vendors) reaches the
internal `timedtext` endpoint, which is the same category of access
Constraints already forbids for Fragrantica and Parfumo.

What *is* available with the existing API key is `videos.list?part=snippet`
— title, description, channel — at **1 quota unit per 50 videos**. That
covers the entity-resolution context ("Perseus" under a video titled
"Maison Alhambra Perseus Review") without touching the source rules.

If titles or descriptions are ever mined for claims, they are a distinct
evidence class: one creator with a megaphone, never counted toward the
3-commenter bar. Letting the person who framed the question also vote on
the answer is this section's failure in its purest form.

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
- **Commerce.** Built — see below. `products` / `retailers` are separate
  from `fragrances`, populated from affiliate network product feeds, and
  feed names are matched with `resolve/names.py`.
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
