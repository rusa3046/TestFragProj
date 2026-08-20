# How the pieces fit together

Written because the project has separate systems that look like one, and
the commands for all of them get run from the same terminal in the same
session. Confusing them is the default state, not a lapse.

## There are three systems

**System 1 builds the evidence graph.** Comments in, ranked answers out.

```
YouTube comments  →  claims  →  fragrance dictionary  →  ranked answers
    11,219           4,861        548 catalogued          118 pairs
    (ingest)        (extract)       (resolve)               (query)
```

**System 2 asks whether System 1 is any good.** It never touches the product.

```
sampled comments  →  drafted labels  →  checked by hand  →  a score
                       (autolabel)        (blind)           (score)
```

**System 3 is FACET, the retail product.** It reads System 1's output and
a retail catalogue, and sells from both.

```
retail catalogue  →  candidates  →  community rerank  →  commerce cards
   773 listings      (catalog fit)    (System 1)         (audited wording)
```

You can have a perfect System 2 and no product. You can have a product built
on garbage and a System 2 that tells you so. They are measured separately
because they answer different questions.

The load-bearing rule between 1 and 3: **System 1 decides what can be
claimed; System 3 decides what is worth recommending.** System 1's evidence
bars are strict and stay strict. System 3 never inherits them as
eligibility gates — a bottle nobody has discussed is still recommendable
on catalogue facts, described in language that says exactly how little is
known. [FACET.md](./FACET.md) carries that argument in full.

## System 1, piece by piece

### Comments (`comments` table, `data/corpus/comments.jsonl`)

What people wrote, verbatim, with a permalink back to the original. Costs
YouTube API quota. Never edited.

### Claims (`claims` table)

What each comment asserts, extracted by Claude Haiku. One comment yields
zero, one, or several. Most yield none — that is the expected result, not a
failure.

A claim is:

| field | example |
|---|---|
| `claim_type` | `DUPE_OF` |
| `raw_subject_text` | `"CDNIM"` — the commenter's own words |
| `raw_object_text` | `"Aventus"` |
| `sentiment` | how they feel about it |
| `polarity` | whether they are claiming it **or denying it** |
| `evidence_span` | the exact words they used, verified against the body |

Costs money — $0.37-$0.50 per 1,000 comments, rising as searches get
more specific. Fully automatic.

### The fragrance dictionary (`fragrances` table)

**This is the piece people misread as a scope limit. It is not.**

Your corpus contains `BR540`, `540`, `Baccarat Rouge 540`, `baccarat 540`.
To a database those are four unrelated strings. A dictionary entry says:
these four are one bottle, and its name is Maison Francis Kurkdjian
Baccarat Rouge 540.

Without it you have claims about *strings*. With it you have claims about
*bottles*, which is the first point at which a page can exist.

Adding one is a single line, any time, with no re-extraction and no cost:

```bash
uv run python -m fragrance_graph.resolve.entities add "Lattafa Asad" --alias Asad
uv run python -m fragrance_graph.resolve.entities backfill
```

Curating more never invalidates work already done. `backfill` is idempotent
and only fills in what is newly resolvable.

### Answers (`query.similar_to`)

Given a fragrance, the fragrances people said smell like it — ranked by how
many **distinct people** said so, each with up to three verbatim quotes and
links. Fully automatic once the dictionary exists.

### Declared notes (`fragrance_note_claim`, `retailer_listings`)

What an official source *says* is in the bottle, imported from licensed
retailer listing data (`data/curation/retailer-listings.jsonl` — 773
listings, 2,778 note rows across 411 bottles). Deliberately separate from claims,
because a brand listing "rose" is a fact about the listing, not evidence
the bottle smells rose-forward. Nothing here ever enters an evidence
count, and the two are worded differently everywhere they meet.

## System 2, piece by piece

### Why it exists

The extractor is a language model reading slang. Nothing about its output
proves it read correctly. The only way to know is to write down what a
comment *should* yield and compare.

### The files, and why there are three

| file | what it is |
|---|---|
| `labels-draft.json` | Claude Opus's guess at the right answers for 50 comments |
| `labels-blind.json` | the same comments with the answers stripped out |
| `eval_labels.jsonl` | both, committed, tagged by who wrote them |

The blind step is the whole point. If you read Opus's drafts before writing
your own, you would agree with most of them — and the score would measure
how much two models agree rather than whether either is right. Filling in
the blind copy first is what makes your labels ground truth.

Labels are stored under the name of whoever wrote them, so a draft can
never be mistaken for a human judgement.

### What a score means

```
SIMILARITY EDGES  P 1.00  R 0.80  F1 0.89   (tp 4, fp 0, fn 1)
```

Of the similarity edges in the labelled comments, the extractor found 4,
invented 0, and missed 1. `SIMILARITY EDGES` collapses `DUPE_OF` and
`SIMILAR_TO`, because both build the same edge and the product's question
is whether the connection was found at all.

**A score is only as sharp as the number of labels behind it.** At 13
comments, one claim moves F1 by 0.13, so a change smaller than one claim
cannot be measured. SPEC.md records a threshold that misfired for exactly
this reason.

## System 3, piece by piece

FACET is a FastAPI service (`api.py`) plus a single static kiosk file. Its
four moving parts, each documented in [FACET.md](./FACET.md):

| piece | module | what it does |
|---|---|---|
| Preference composer | `session.py` | three buckets of typed `PreferenceItem`s, event-sourced, compiled to a `QueryPlan` |
| Catalog profiles | `catalog_profile.py` | declared notes → note-family tendencies and occasion priors, the note-status ladder |
| Recommender | `recommend.py` | three-tier generation: catalogue → plausible set → community rerank |
| Commerce cards | `commerce_card.py` | tier-gated wording, fit signals, tradeoffs, result labels |

**It adds no new source of truth.** Sessions are the only thing FACET
writes, and nothing in the pipeline reads them. Delete the service and the
graph is unchanged — which is the property that lets the product move fast
without risking the corpus.

## What needs a human, and how much

Automatic, every time: ingest, extraction, evidence verification, denial
detection, ranking, scoring, catalog profiling, card wording.

Human, and **bounded**:

**Curating the dictionary.** Measured when the corpus held 603 distinct
fragrance names across 1,123 mention slots in comparison claims:

| curate top N | coverage | est. resolvable claims |
|---|---|---|
| 20 | 29% | ~49 |
| 50 | 40% | ~97 |
| 100 | 51% | ~152 |
| 200 | 64% | ~243 |

The curve flattens because **452 of the 603 names were mentioned exactly
once** — three quarters of the list is a tail that can never clear a
3-commenter bar. Fifty entries is most of the value; a few hundred is the
whole job, ever. A new corpus needs only a top-up, since existing aliases
keep resolving.

**Most of that job got done by import rather than by hand.** The catalogue
holds 548 bottles because `retail seed-from-listings` catalogues an
unresolved retailer listing when its house is already known and the row is
not a gift set — a mechanical case with no judgement in it. Hand curation
is now the exception-handling path, not the main one.

**Labelling.** Fifty comments is a working eval; the project has 86 with
labels, fewer hand-verified. This does not scale with corpus size — you
label a sample, not the corpus. Growing past ~50 only matters if the
taxonomy changes or a defect appears that the current sample cannot see.

Neither is a treadmill. Both are front-loaded.

## Is this training? No.

**Nothing in this system learns from human input.** There is no
fine-tuning step, and the labels never reach the extractor. They are a
ruler, not a teacher: they measure whether it read a comment correctly and
have no effect on how it reads the next one.

That matters for predicting the ongoing cost, because maintenance and
training have very different shapes.

**Curation is permanent but incremental.** A dictionary needs new words
when new words appear. A fragrance launches, someone mentions it, you add
one line — once. Existing entries are never re-checked; "Layton = Parfums
de Marly Layton" stays true. The effort scales with **new releases
entering conversation**, not with corpus size: ingest 100,000 more
comments about fragrances already curated and the manual work is zero.
Spelling variants are handled automatically by fuzzy matching (58 of 474
resolutions in one run), so what gets curated is the bottle, not every way
people type it.

**Labels do not expire.** They test reading comprehension — does the model
understand "X is a dupe of Y" — not which bottles exist. A comment about a
2027 release parses the same way as one about Aventus. Revisit them when
the taxonomy changes, the model changes, or the instrument needs to be
sharper. Not on a schedule.

**Skipping curation degrades gracefully.** An uncurated fragrance simply
does not appear in results; its claims are still extracted and stored,
waiting. Curate the name a year later and every claim about it lights up
retroactively — `backfill` is idempotent and applies to everything already
in the database, with no re-extraction and no cost. Nothing deferred is
lost.

### The lever, if curation ever becomes a burden

Most curation is mechanical rather than a judgement call — "Khamrah" ->
"Lattafa Khamrah" is a lookup. A stronger model could draft canonical
names and brands for unresolved mentions, with a human approving or
correcting, exactly as `autolabel` does for labels.

Not built, deliberately. It needs the same safeguard labelling needed: a
model confidently inventing a brand is worse than an empty dictionary, so
it would want a blind-check step measuring drafter-to-human agreement
before the drafts could be trusted in bulk. Worth building at a few
hundred entries. Not worth it at fifty.

## Where the money goes

| | cost |
|---|---|
| Ingest | free (YouTube quota, 10,000 units/day) |
| Extraction | $0.37-$0.50 per 1,000 comments (measured 2026-08-11) |
| Label drafting | ~$0.15 per 50 comments (Opus) |
| Curation lookups | $0.05 per request on pay-per-use; metered by the cap |
| Scoring, ranking, pages | free |

Re-extracting the whole corpus is ~$2 and rising with it, which is what a
prompt change costs to evaluate properly. Re-extracting only the labelled
comments is ~$0.02, which is what most experiments should use.
