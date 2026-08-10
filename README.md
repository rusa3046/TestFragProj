# fragrance-graph

**Given a fragrance, return the fragrances the community says are dupes or
smell similar — ranked, and backed by verbatim quotes linking to the comments
people actually wrote.**

Similarity here is *asserted*, never computed. Nothing in this system models
what a fragrance smells like. It reads what people said about it, extracts
structured claims, resolves the names to real bottles, and counts how many
distinct people made the same claim.

That is the product, not a limitation of it. A buyer deciding whether to spend
£120 is not served by a cosine distance of 0.87. They are served by *"31 people
called this a dupe of Baccarat Rouge 540, and here is what nine of them said."*
The evidence is the feature; the ranking is just how the evidence is sorted.

Structured note data (top/mid/base pyramids) is deliberately not used. It lives
behind sites whose terms forbid scraping — see Constraints in
[SPEC.md](./SPEC.md) — and note overlap answers a question buyers were not
asking.

## How it works

```
YouTube comments  →  LLM claim extraction  →  entity resolution  →  ranked answers
   (public API)        (Claude Haiku 4.5)      (name → bottle)       (+ evidence)
```

1. **Ingest.** Public platform APIs only. Comments land in SQLite, idempotent
   on `(source, source_id)`, resumable mid-run.
2. **Extract.** Claude reads batched comments and returns typed claims —
   `DUPE_OF`, `SIMILAR_TO`, `NOTE_DESCRIPTOR`, `LONGEVITY`, and seven more.
   Every claim carries an `evidence_span` quoted from the comment, and that
   span is verified against the comment body before it is stored. A claim whose
   evidence cannot be found is kept but flagged, so the paraphrase rate stays
   measurable instead of invisible.
3. **Resolve.** `BR540`, `540`, `B540` and `BR MFK 540` are one bottle. Curated
   aliases plus conservative fuzzy matching collapse them into a single node.
4. **Answer.** Ranked results with quotes and permalinks.

## Status

**Steps 1, 2 and 4 are built and have run on real data. Step 3 is built but
uncurated**, which is the only thing standing between the corpus and real
answers: 18 fragrances curated in a scratch database resolved 504 of the
corpus's mentions and produced the `query` output shown below. None of that
curation is committed — see `resolve.entities report`.

First real corpus, 2026-08-09 (see [data/corpus/PROVENANCE.md](./data/corpus/PROVENANCE.md)):

| | |
|---|---|
| Comments | 3,155 across 24 YouTube videos |
| Claims | 1,409 (0.447 per comment) |
| Extraction cost | $1.15 total, $0.3656 per 1k comments |
| Failed batches | 0 of 158 |
| Fragrances curated | 0 — nothing is resolved yet |
| Labelled comments | 50 drafted, 15 verified by hand (13 in train) |
| Extractor score | `SIMILARITY EDGES` F1 **0.89**; OVERALL F1 0.50 |
| Denials caught | 35 of 38 flagged (92%), plus 32 the pattern missed |

### The edge funnel — where the graph actually is

Measured from the committed corpus, 2026-08-10:

```
1,409  all claims
  639  comparison types      (SIMILAR_TO / DUPE_OF / BETTER_THAN)
  591  FRAGRANCE -> FRAGRANCE
  529  ASSERTED              (-62 denials)
  524  evidence verified     (-5)
    0  both ends resolved    <- nothing is curated
```

**Zero queryable edges out of the box.** Every filter works; none of it
reaches a reader, because an edge needs *both* its subject and its object
to be a curated bottle.

**How far curation has to go**, measured on the 467 edge-eligible claims
whose two ends are both nameable — not modelled, counted:

| curate top N | claims resolved | distinct pairs |
|---|---|---|
| 16 (`scripts/seed_fragrances.py`) | 10 | 4 |
| 25 | 29 | 14 |
| 40 | 46 | 26 |
| 60 | 74 | 45 |
| 80 | 97 | 61 |
| 120 | 148 | 97 |
| 200 | 217 | 161 |

Both ends must land, so the yield is superlinear and the first entries are
worth the least. **16 sits at the bottom of a steep curve** — it exists to
make the pipeline demonstrable end to end, not to be a result. Sixty to
eighty is where this becomes a product.

An earlier version of this table estimated the yield as coverage squared
and overshot by roughly 3x, because mentions cluster by video rather than
pairing independently. The numbers above are counted.

What that does **not** yet establish:

- **Extraction accuracy is measured, but on 13 comments.** Every similarity
  edge in that sample was found and none invented — three times running.
  But OVERALL F1 has moved 0.50 → 0.62 → 0.75 across code states whose
  differences account for about one claim each, so the eval currently
  cannot resolve a change smaller than itself. SPEC.md says which
  conclusions survive that and which do not.
- **Nothing is resolved in the committed corpus.** `fragrances.jsonl` is
  empty, so out of the box the claims are edges between strings. 846
  distinct unresolved mentions; the head of that list is short and
  repetitive (Layton 99, Khamrah 44, Aventus 57 across casings), so the
  first hour of curation is worth far more than the last.
- **`DUPE_OF` is over-firing since the polarity re-extraction.** 37 claims
  moved from `SIMILAR_TO`, and only 14 carry dupe language. Both are edges
  so the graph is unaffected, but "dupe" is a stronger claim than "similar"
  and page copy will repeat it. Unmeasured — the eval scores `DUPE_OF`
  precision 1.00 on 13 comments and cannot see this.
- **No real product, price, or retailer data has been imported.** The
  tables, the feed importer and the link builder exist and are tested, but
  the only feeds they have ever read are the invented fixtures under
  `tests/fixtures/feeds/`, which import at a 65% match rate against the 16
  seeded fragrances. No affiliate account has been opened.

New to the project? [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) explains
the two systems — the pipeline that builds the product, and the eval that
measures it — and which parts need a human.

[AUDIT.md](./AUDIT.md) is a read-only assessment of what is real, what is
stubbed, and what has never been measured. It predates this corpus, so its
data-layer findings are now out of date; its architectural ones are not.

## Setup

```bash
uv sync --extra dev          # --extra dev is required; plain `uv sync` omits pytest
cp .env.example .env         # fill in YOUTUBE_API_KEY and ANTHROPIC_API_KEY
```

Both keys are needed to build a corpus. YouTube keys are issued instantly from
the Google Cloud console; Reddit refused API access to this project, which is
why YouTube is the primary source.

## Usage

Initialize the database:

```bash
uv run python -m fragrance_graph.db init
```

The database path comes from `FRAGRANCE_DB_PATH` (default
`fragrance_graph.db` in the repo root), or `--db-path` on any command. The
working corpus lives at **`data/fragrance_graph.db`**:

```bash
export FRAGRANCE_DB_PATH=data/fragrance_graph.db
```

### The database is disposable; the corpus is not

`*.db` is gitignored, and on a remote container the whole filesystem is
reclaimed when the session ends. Comments cost API quota, claims cost real
money, and curated aliases cost human judgement — none of that should die with
a container. So the durable form is newline-delimited JSON under
**`data/corpus/`**, which *is* committed:

```bash
uv run python -m fragrance_graph.corpus export    # db  → data/corpus/*.jsonl
uv run python -m fragrance_graph.corpus import    # db  ← data/corpus/*.jsonl
```

Four files: `comments.jsonl`, `claims.jsonl`, `fragrances.jsonl`, and
`eval_labels.jsonl`. They diff line by line in review, are readable without
SQLite, and round-trip losslessly
— rows link by natural keys (`source` + `source_id`, and `canonical_name`),
never by autoincrement id, so a rebuilt database re-numbering its rows cannot
silently reattach a claim to the wrong comment. Export is byte-stable, so an
unchanged corpus produces an empty diff. Import is idempotent.

Export after any run that costs money or judgement, and commit the result.
Note that these files contain other people's comment text, retained under the
source platform's API terms — committing the export is republication, so treat
it as such.

Ingest YouTube comments:

```bash
# by video id — cheap: 1 quota unit per 100 comments. Repeat --video freely.
uv run python -m fragrance_graph.ingest.youtube --video dQw4w9WgXcQ --limit 500

# or search first — costs 100 quota units per search
uv run python -m fragrance_graph.ingest.youtube --query "baccarat rouge dupe" --max-videos 5
```

A video id is the 11-character string after `v=` in a watch URL —
`youtube.com/watch?v=dQw4w9WgXcQ` → `dQw4w9WgXcQ`. Not the title, not the URL.

The daily YouTube quota is 10,000 units and the ingest tracks spend as it goes.
Ingest is idempotent and commits in batches, so interrupting it loses nothing
already committed and re-running resumes rather than duplicating.

Check what extraction will cost before paying for it:

```bash
uv run python -m fragrance_graph.extract.llm --dry-run --limit 500
```

No API call, no key required. It prints its own assumptions — output token
volume depends on how many claims the comments turn out to assert, so treat it
as an order of magnitude, not a quote.

Extract claims:

```bash
uv run python -m fragrance_graph.extract.llm --limit 500
```

Extraction drops claims that violate an invariant the JSON schema cannot
express. Those are persisted, not just logged. The rate has ranged from
6.7% to 24% of everything emitted across runs of the same code, which is
itself worth knowing — and the drops account for most remaining false
negatives in the eval:

```bash
uv run python -m fragrance_graph.extract.rejects report              # by reason
uv run python -m fragrance_graph.extract.rejects show --reason DUPE_OF
```

`show` prints the comment alongside the claim the model emitted, which is
what decides whether a rejection is the model being wrong or the taxonomy
being wrong.

Check that denials are being recorded as denials:

```bash
uv run python -m fragrance_graph.extract.polarity audit
```

A denial stored as an assertion — *"it is nothing like angel share"* filed
as a dupe — puts a real person's name behind the opposite of what they
wrote. `polarity` (ASSERTED / DENIED) records which, and `similar_to()`
never ranks a denial. This is deliberately not folded into `sentiment`:
all 36 measured denials were NEGATIVE, but so were five genuine edges
(*"worst dupe of (540)"* asserts the dupe and dislikes it).

`audit` exits non-zero while any denial is still stored as an assertion,
so it can gate a re-extraction. It is a recall instrument, tuned to
over-flag — it never writes polarity, so the corpus records only what a
model judged.

Resolve names to bottles:

```bash
uv run python -m fragrance_graph.resolve.entities report      # what needs naming, by frequency
uv run python -m fragrance_graph.resolve.entities add "Baccarat Rouge 540" --alias BR540 --alias 540
uv run python -m fragrance_graph.resolve.entities backfill    # apply to claims
uv run python -m fragrance_graph.resolve.entities edges       # all edges, counted
```

Curation is human work by design. `report` ranks unresolved mentions by how
often they appear, so twenty minutes spent at the top of that list resolves most
of the corpus.

### Proposing names from a catalogue

Most curation is a lookup, not a judgement — "Khamrah" is "Lattafa Khamrah".
An external catalogue can propose, and you approve:

```bash
export FRAGELLA_API_KEY=...     # secret key, not the pub_ widget key
uv run python -m fragrance_graph.resolve.enrich propose review.json --limit 60
# set "approved": true or false on each row
uv run python -m fragrance_graph.resolve.enrich apply review.json
uv run python -m fragrance_graph.resolve.entities backfill
```

One request per mention. Pronouns and single-mention names are skipped, since
neither can ever name a bottle that clears a 3-commenter bar.

See [docs/CURATION.md](./docs/CURATION.md) for what you are actually judging
— in short, "is this the bottle the commenters meant?", and the failure that
really happens is flankers (`Layton` vs `Layton Exclusif`). Each row carries
two real comment spans so the quotes can settle it.

**A name the catalogue never returned cannot be written.** Every proposal is
a row it actually returned, and `approved` starts null so an unreviewed file
adds nothing. Only `Name`, `Brand` and `Year` are kept — the response also
carries notes, accords, images, ratings and an affiliate `Purchase URL`, all
of which SPEC forbids. The similarity endpoints are never called; a test
records the requested URLs and asserts it.

Ask the question the project exists for:

```bash
uv run python -m fragrance_graph.query "Creed Aventus" --limit 5 --quotes 2
```

```
  3 people  SIMILAR_TO  Armaf Club de Nuit Intense Man  [neutral]
         ← "he asked me are you wearing Creed Aventus"
           https://www.youtube.com/watch?v=ZOd2QEVJX8c&lc=UgxRN4jz...
  2 people  BETTER_THAN Armaf Club de Nuit Intense Man  [positive]
         → "CDNIM smells like a dollar store fragrance compared to Aventus"
```

`→` means the claim was written about the fragrance you asked for; `←`
means it was written about the other one. `DUPE_OF` and `SIMILAR_TO` read
from either end — "B is a dupe of A" is the same fact whichever bottle you
arrived at. `BETTER_THAN` does not, because being beaten is not a
recommendation.

**Ranking is distinct commenters, never claim rows.** One person saying
"BR540 dupe" in four threads is one person, and counting rows would render
a loud minority as consensus.

Each row also reports `(N sources; M for the pair)`:

- **sources** — how many distinct videos back it. Three commenters in one
  comment section is not three independent observations, and two of the
  eight currently resolved pairs are single-source. `--min-sources 2`
  filters them out.
- **for the pair** — distinct people connecting the two bottles across
  *all* claim types. Rows share people, so summing the per-row counts
  over-counts humans: Aventus/CDNIM reads 3 + 2 + 1 but is 5 people, not 6.

## Buying links

Where a bottle can be bought, from affiliate-network product feeds. **This
is not scraping.** Retailers, Fragrantica and Parfumo are off limits because
their terms forbid it; a network feed is licensed data published to its
publishers, fetched from a URL the network hands you.

```bash
uv run python -m fragrance_graph.commerce.links retailer add "Example Scent Co" \
    --network shareasale --affiliate-id aff-2 \
    --url-template 'https://network.test/r.cfm?b={external_id}&u={affiliate_id}&urllink={raw_url}'

uv run python -m fragrance_graph.commerce.feeds import feed.csv --retailer "Example Scent Co"
uv run python -m fragrance_graph.commerce.feeds unmatched
uv run python -m fragrance_graph.commerce.links links "Creed Aventus"
```

```
tests/fixtures/feeds/example_scent_co.csv: 14 rows, 14 new, 0 updated;
9 matched a curated fragrance (64.3%), 5 unmatched
```

**The match rate is the number to watch.** Feed names are messy — `Lattafa
Khamrah EDP 100ml Spray Unisex` — and matching them to bottles is the same
entity-resolution problem as `BR540`, solved by the same code in
`resolve/names.py`. An importer that silently drops a third of a feed looks
exactly like one that imported all of it, so every unmatched name is logged
and `unmatched` ranks them by how many listings curating each would unlock.

Unmatched rows are stored, not discarded. Matching runs on every import, so
an alias added today resolves rows imported last month with no re-download.

Products are deliberately **not** part of `data/corpus/`. Feed rows are
re-downloadable, go stale within a day, and are a retailer's catalogue
rather than something that cost money or judgement to obtain.

## Measuring extraction quality

Extraction quality is judged against hand-written labels, not by reading output
and nodding. Export a template, fill in the claims each comment *should* yield,
then import and score:

```bash
uv run python -m fragrance_graph.evals.labels export labels.json --sample 50
# fill in the "claims" list for each comment
uv run python -m fragrance_graph.evals.labels import labels.json --labeler you
uv run python -m fragrance_graph.evals.score
```

`--sample N` spreads the selection across the whole corpus. Without it you get
the first N rows by id, which are N comments from one video's comment section —
a labelled set that measures that video rather than the corpus. The sample is
deterministic, so a labelling session can be resumed.

Templates are keyed on `(source, source_id)`. Importing one into a database it
was not exported from fails loudly rather than attaching your labels to
whatever rows happen to hold those ids.

Comments split deterministically into `train` (70%) and `holdout`. Tune against
`train`; consult `--split holdout` only to confirm a prompt you have already
chosen. Scoring against the holdout while iterating turns it into training data
and the number stops meaning anything.

**Do not tune the extraction prompt before labels exist.** A change that looked
obviously correct once raised run-to-run variance sixfold and broke evidence
verification for the first time in the project; it had to be reverted. SPEC.md
records the measurements.

### Drafting labels with a stronger model

Most comments assert nothing, so most of a labelling session is typing empty
lists. A stronger model (Claude Opus 5) can draft the labels so you *review*
rather than author — roughly 3× faster.

**A drafted label is not ground truth.** The extractor is Haiku 4.5; if a model
writes the answer key, the score measures agreement between models rather than
accuracy, and their errors correlate. Two safeguards make drafts usable:

```bash
# 1. Draft. Costs ~$0.15 for 50 comments.
uv run python -m fragrance_graph.evals.autolabel draft labels-draft.json --sample 50

# 2. Pull a calibration set — same comments, claims stripped.
uv run python -m fragrance_graph.evals.autolabel blind labels-draft.json labels-blind.json --n 15

# 3. Label labels-blind.json BY HAND, without opening labels-draft.json.
#    See docs/LABELLING.md for what a claim is and worked examples.
uv run python -m fragrance_graph.evals.labels import labels-blind.json --labeler you
uv run python -m fragrance_graph.evals.labels import labels-draft.json --labeler opus5-draft

# 4. Measure how far the drafter is from you, on those 15.
uv run python -m fragrance_graph.evals.autolabel agreement --human you
```

High agreement earns the right to lean on the drafts for the rest. Low
agreement means label everything by hand — and you've learned that cheaply.
Skipping step 3 is how pre-filled annotation quietly turns a model's opinion
into "ground truth": the drafts look plausible, so they get rubber-stamped.

Drafts import under their own labeler (`opus5-draft`), never a person's name.
`eval_labels` is keyed on `(comment_id, labeler)`, so a draft can never
overwrite or be mistaken for a human judgement, and `score --labeler` picks
which one to trust.

`--pronoun-policy` is an explicit choice, not a default to ignore. Two of three
comments sampled from the live corpus had a pronoun subject (*"It's not a super
strong fragrance"*), so whether those count as claims materially moves the
score. `skip` (the default) omits them, since a pronoun can never resolve to a
bottle; `literal` keeps them. Apply the same rule in your hand labels or the
disagreement you measure is your own drift.

## Trust rules

These are product requirements, enforced in code and tests — not guidelines.
Each names the test that would fail:

- **Ranking never considers commercial relationships.** Result order is
  computed with no knowledge of which fragrances are monetizable. Measured
  end to end: the same corpus ranked with product rows on the *weakest*
  result and none on the strongest returns results identical to the same
  corpus with those rows deleted —
  `test_ranking_is_identical_with_and_without_products`. A structural test
  alongside it names every table the ranking queries may touch.
- **Results are never filtered to monetizable options.** A fragrance with no
  buying link ranks exactly as high as one with three —
  `test_a_fragrance_with_no_buying_link_ranks_as_high_as_one_with_three`.
  Filtering is the version of this failure that leaves the order untouched,
  so it is tested separately.
- **Affiliate links are disclosed inline, at the link** — not once in a page
  footer where nobody reads it. The disclosure is a field on the link
  object, so no renderer can obtain a URL without also holding the words
  that go beside it.
- **Text only.** Naming a fragrance identifies it; using a brand's logo or
  imagery borrows its authority. No table has a column that could hold an
  image, and nothing in the codebase emits markup.
- **Nothing that ranks can be sorted by what it pays.** There is no
  commission column anywhere in the schema, which makes the rule a missing
  capability rather than a promise.

## Development

```bash
uv run ruff check .
uv run pytest
```
