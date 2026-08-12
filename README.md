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
YouTube comments → claim extraction → entity resolution → ranked answers
  (Data API v3)    (Claude Haiku 4.5)   (name → bottle)     (+ evidence)
                                              ↑
                                    Fragella catalogue
                                   (names and brands only)
```

1. **Ingest.** Official platform APIs only. Comments land in SQLite, idempotent
   on `(source, source_id)`, resumable mid-run.
2. **Extract.** Claude reads batched comments and returns typed claims —
   `DUPE_OF`, `SIMILAR_TO`, `NOTE_DESCRIPTOR`, `LONGEVITY`, and seven more.
   Every claim carries an `evidence_span` quoted from the comment, verified
   against the comment body before it is stored, plus a `polarity` recording
   whether the commenter asserted the relationship **or denied it**.
3. **Resolve.** `BR540`, `540` and `Baccarat Rouge` are one bottle. Curated
   aliases plus conservative fuzzy matching collapse them into a single node.
   The [Fragella](https://api.fragella.com/) catalogue proposes canonical
   names and brands for unresolved mentions — see
   [docs/CURATION.md](./docs/CURATION.md). **Names and brands only:** its
   notes, accords, ratings and computed-similarity endpoints are off limits,
   because a result sourced from an accord overlap cannot be backed by a
   quote. SPEC.md records the boundary field by field.
4. **Answer.** Ranked by distinct commenters, with quotes and permalinks.

### Which sources are live

| source | status |
|---|---|
| **YouTube Data API v3** | **live** — the entire corpus |
| **Anthropic API** | **live** — extraction, and eval-label drafting |
| **Fragella** | **live** — name → canonical bottle, nothing else |
| Reddit | **not used.** API access was refused to this project |
| Affiliate feeds (Rakuten, ShareASale) | built, no account yet — Phase C |
| Fragrantica / Parfumo / Basenotes | **never.** No API, and scraping breaches their terms |

No `REDDIT_*` credentials are needed and `praw` is not a dependency.

The shared writer lives in `ingest/store.py` — source-agnostic, idempotent on
`(source, source_id)`, committing as it goes so an interrupt loses nothing.
It was `ingest/reddit.py` until 2026-08-10, which put the codebase's
most-imported function in a module named after the one source that does not
work. The PRAW paths went with the rename, along with the `praw` dependency:
code that cannot run is worse than absent code, because it reads as an
option.

## Status

**All four steps are built and have run on real data**, and Phase D renders
pages from them. The corpus, the claims, the eval labels, the 56 curated
fragrances and the retrieval provenance are committed, so a clean clone
reproduces every number on this page.

**Curation is still the binding constraint on the graph** — 56 entries
yield 37 pairs, of which **8 clear the publishing gate** of 3+ commenters
across 2+ creators (distinct uploading channels, not distinct videos).

**But the query surface is now the binding constraint on the product.**
57% of the corpus is extracted, paid for, stored, and reachable by no
query — see [What's next](#whats-next).

Corpus as of 2026-08-11 (see [data/corpus/PROVENANCE.md](./data/corpus/PROVENANCE.md)):

| | |
|---|---|
| Comments | 4,866 across 39 videos / 29 channels |
| Claims | 2,118 |
| Extraction cost | $0.3656-$0.4410 per 1k comments, and it moves with the query |
| Fragrances curated | 56 — 41 of them answer a query |
| Labelled comments | **65 verified by hand** (46 in train), plus 50 drafted |
| Extractor score | `SIMILARITY EDGES` F1 **0.57** (P 0.60, R 0.55); OVERALL F1 0.40 |
| Denials caught | 35 of 38 flagged (92%), plus 32 the pattern missed |
| Spent to date | $3.11 — [a $1/day cap that leaked, since fixed](./SPEC.md) |

### The edge funnel — where the graph actually is

```
2,118  all claims
  902  comparison types      (SIMILAR_TO / DUPE_OF / BETTER_THAN)
  809  FRAGRANCE -> FRAGRANCE
  717  ASSERTED              (-92 denials)
  711  evidence verified     (-6)
  109  both ends resolved    <- 56 fragrances curated
   37  distinct pairs
    8  pages published       <- 3+ commenters AND 2+ creators
```

**An edge needs *both* its subject and its object to be a curated bottle**,
which is why 711 verified claims produce 109. Every filter above works; the
graph is small because the dictionary is. At 17 curated entries this line
read 18, and nothing but curation changed to move it.

**The last step is the publishing gate, and it is meant to be lossy.** 37
pairs become 8 pages because a pair backed by two people, or by three people
under one creator, cannot honestly be headed "people say this". See
`pages.py` for why both bars are measured on the pair rather than on a
single claim type.

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
- **Most of the corpus is still unresolved.** 56 fragrances are curated
  against many hundreds of distinct names in comparison claims, and 15 of
  those 56 answer nothing yet. The head of that list is short and
  repetitive, so the first hour of curation is worth far more than the
  last — but 8 pages is a demo, not a product.
- **One curated entry was wrong and shipped.** `Perseus` is made by two
  houses; the bare alias pointed at the wrong one, producing an edge that
  misquoted three commenters. Found by research, fixed, recorded in SPEC.
  That is a ~6% error rate on entries called "confident", and it is the
  reason `--min-sources` and the 3-commenter bar exist.
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
cp .env.example .env         # fill in the keys below
```

| variable | needed for | notes |
|---|---|---|
| `YOUTUBE_API_KEY` | ingest | Google Cloud console, issued instantly. 10,000 units/day |
| `ANTHROPIC_API_KEY` | extraction, label drafting | $0.37-$0.50 per 1,000 comments, see below |
| `FRAGELLA_API_KEY` | curation (`resolve.enrich`) | $0.05 per request on pay-per-use; billed to the same cap |

Reddit refused API access to this project, which is why YouTube is the only
comment source. `ingest/reddit.py` still exists — see the naming wart above —
but its PRAW paths cannot run and no `REDDIT_*` credentials are required.

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

- **sources** — how many distinct creators back it. Three commenters in one
  comment section is not three independent observations, and two of the
  eight currently resolved pairs are single-source. `--min-sources 2`
  filters them out.
- **for the pair** — distinct people connecting the two bottles across
  *all* claim types. Rows share people, so summing the per-row counts
  over-counts humans: Aventus/CDNIM reads 3 + 2 + 1 but is 5 people, not 6.

## The daily loop

```bash
uv run python -m fragrance_graph.daily run --dry-run   # no keys, nothing spent
uv run python -m fragrance_graph.daily run
uv run python -m fragrance_graph.daily spend           # recent daily totals
```

It is **demand-driven**, and that is a decision rather than an
implementation detail:

```
1. YouTube: search fragrance discussion broadly
2. ingest -> extract
3. resolve.entities report  ->  newly frequent, still unnamed
4. Fragella: resolve exactly those names
5. auto-curate what corroborates -> backfill -> export -> pages
```

Asking a catalogue what is new and *then* looking for discussion of it
answers the wrong question: a bottle launched yesterday has no YouTube
comments, so a release feed delivers fragrances that cannot produce an edge.
The corpus is already the detector — a new release climbs the
unresolved-mention report exactly when people start discussing it. SPEC
records the full argument. It also makes the catalogue cheap, since lookups
go only to names the corpus has proved people are writing.

**Spending is capped at $1/day, hard.** `data/spend.jsonl` is an
append-only committed ledger; every scheduled run gets a fresh container, so
a cap held in the database or in `/tmp` would reset each run and quietly
become "$1 per *run*". The cap is enforced between extraction batches, not
just before the run — a batch commits before the check, and anything
unreached keeps `extracted_at` NULL, so a stop resumes tomorrow rather than
re-paying or skipping.

**Curation is automatic only where there is no decision to make.** A
proposal is written without asking when the catalogue's name adds no word to
the mention (`corpus_mentions == -1`) — it is the plain bottle, so the
flanker question does not arise. Anything else is held and listed in the run
summary. That rule will still be wrong sometimes; what keeps a bad merge off
a page is the publishing gate below, not the rule.

## Comparison pages

One static page per pair, built from the query layer:

```bash
uv run python -m fragrance_graph.pages pairs           # what qualifies, no writes
uv run python -m fragrance_graph.pages build --out site/
```

```
    7 people  4 sources  Al Haramain Detour Noir vs Parfums de Marly Layton
    7 people  3 sources  Parfums de Marly Layton vs The Woods Collection Dusk
    7 people  2 sources  Kilian Angels' Share vs Lattafa Khamrah
    5 people  3 sources  Armaf Club de Nuit Intense Man vs Creed Aventus
    4 people  2 sources  Orientica Luxury Collection Royal Bleu vs Parfums de Marly Layton
    3 people  2 sources  Armaf Club de Nuit Imperiale vs Parfums de Marly Delina Exclusif
```

**A page is generated only at 3+ distinct commenters *and* 2+ distinct
videos.** Both bars are measured on the pair across every claim type, not on
one claim-type row — rows share people *and* videos, so a row-scoped source
count printed beside a pair-scoped commenter count would be two numbers
counting different things. On this corpus the scopes disagree on 8 of 21
candidate pairs and one pair changes gate status.

### Videos are not independent samples

`--show-queries` names the searches behind each pair, and the result is
uncomfortable:

```
9 people  6 creators  2 queries  Al Haramain Detour Noir vs Parfums de Marly Layton   (+2 video(s) with no retrieval record, so this is a lower bound)
9 people  3 creators  1 query  Kilian Angels' Share vs Lattafa Khamrah   (+1 video(s) with no retrieval record, so this is a lower bound)
8 people  4 creators  1 query  Parfums de Marly Layton vs The Woods Collection Dusk   (+1 video(s) with no retrieval record, so this is a lower bound)
7 people  4 creators  2 queries  Armaf Club de Nuit Intense Man vs Creed Aventus   (+1 video(s) with no retrieval record, so this is a lower bound)
5 people  3 creators  1 query  Creed Aventus vs Montblanc Explorer   (+1 video(s) with no retrieval record, so this is a lower bound)
5 people  3 creators  1 query  Orientica Luxury Collection Royal Bleu vs Parfums de Marly Layton   (+1 video(s) with no retrieval record, so this is a lower bound)
3 people  2 creators  1 query  Armaf Club de Nuit Imperiale vs Parfums de Marly Delina Exclusif   <- one query only
3 people  2 creators  1 query  Lalique White in Black vs Parfums de Marly Layton   (+1 video(s) with no retrieval record, so this is a lower bound)
```

**The clean measurement is on the first 24 videos, where provenance is
complete: six of the eight pairs publishing now rest on a single search
query.** Three different `parfums de marly layton dupe` videos are three
separate comment sections, so the creator bar passes them — but they are
three rooms in which the same question was put to an audience assembled for
that question. The guard against one comment section does not guard against
one *query*.

**On today's larger corpus the number is a lower bound, and says so.**
15 of 39 videos were ingested before discovery tracking existed and their
queries cannot be reconstructed — inventing a plausible one would be the
fabrication this project refuses everywhere else. So only 1 of 8 pairs is
*confirmed* single-query; the other 7 are unknown, not innocent. The counts
converge on the truth as undocumented videos are re-found by recorded
searches.

`--min-queries 2` defaults to **1 — off** —
because the number cannot yet distinguish "these edges are weak" from "the
eight seed queries were too narrow", and those want opposite fixes. Six of
the eight seeds contain the word "dupe" (see
[PROVENANCE](./data/corpus/PROVENANCE.md)), so narrow seeding is the live
hypothesis. Broaden discovery first, then raise the bar.

A pair with `0` queries means *no retrieval record*, not *narrow*. Gating
treats 0 as unknown and lets it through, so raising the bar cannot silently
unpublish a back corpus ingested before provenance was tracked.

Retrieval provenance is many-to-many (`video_discoveries`): the same video
can be found by several searches, and a single `retrieval_query` column on
comments would have to pick one and discard the rest — destroying the count.

`query.pair_stats` is what the gate reads, and it is deliberately
direction-blind: `BETTER_THAN` only surfaces from the subject's end, so
asking "who connected these two bottles" from one side alone under-counts.
Aventus/CDNIM reads 5 people from Aventus and 4 from CDNIM. It is five
people.

Pages are **not committed**. They cost no quota, no money and no judgement,
and they are a pure function of `data/corpus/` — the same reasoning that
keeps products out of the corpus. `site/` is gitignored; rebuilding an
unchanged corpus rewrites identical bytes, so a diff there would only ever
mean the corpus moved.

### Where the site lives, and who may watch it

Three files decide, none of them code:

```bash
uv run python -m fragrance_graph.pages build --out site/ \
  --base-url https://rusa3046.github.io/TestFragProj/
```

- **`--base-url`** is what canonical tags and `sitemap.xml` are built
  from. Without it — and without a `CNAME` — both are skipped and the
  build says so. A guessed domain is worse than none: a canonical tag
  pointing at the wrong host tells a search engine to index somebody
  else's copy of the page. The scheduled workflow passes the Pages URL
  GitHub derives from the repository, so a rename cannot break it.

- **`CNAME`** in the repository root turns on a custom domain. One line,
  the bare domain, no scheme:

  ```
  dupes.example.com
  ```

  Then point a DNS `CNAME` record for that name at
  `rusa3046.github.io.` and set the domain in the repository's Pages
  settings. `build` copies the file into `site/` on every build, which is
  the part that is easy to miss — GitHub Pages reads `CNAME` from the
  published root, so a build that does not carry it forward silently
  drops the custom domain on the next deploy. A `CNAME` also **overrides
  `--base-url`**, because it is the file the host itself obeys.

- **`data/analytics.html`** is injected verbatim into the `<head>` of
  every page, and ships empty. Paste a provider's snippet there and
  rebuild to turn analytics on; delete it to turn them off. It is a file
  rather than a flag so the tag is reviewable in a diff — a script on
  these pages can see every visitor, and *which script, added when, by
  whom* should be answerable from git alone. Nothing in it is validated
  or escaped, unlike comment text, so paste only what the provider gave
  you.

`robots.txt` is always written and always allows everything; it names the
sitemap when there is one.

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

### Label the comments that are worth an evening

Uniform sampling is right for estimating typical performance and wasteful for
everything else: at ~0.44 claims per comment, most of a uniform hour is spent
typing empty lists. `evals.sample` picks the rows that discriminate instead:

```bash
uv run python -m fragrance_graph.evals.sample coverage        # what you have
uv run python -m fragrance_graph.evals.sample plan next.json -n 50
# fill in "claims"; `_why` says why each row is there
uv run python -m fragrance_graph.evals.labels import next.json --labeler you
```

Four strata, and one of them is the point:

| stratum | why |
|---|---|
| `rejected` | extraction produced something the schema refused |
| `silent` | extraction produced **nothing**, but the comment talks like a comparison |
| `edge` | produced a SIMILAR_TO / DUPE_OF / BETTER_THAN claim — what actually gets published |
| `control` | uniform, so precision on an ordinary comment stays measurable |

**`silent` is the eval's structural blind spot.** Scoring compares labels
against extracted claims, so a comment the extractor passed over in silence
contributes nothing to inspect and its false negatives cannot be counted — no
matter how many claims you read. On the current corpus `coverage` reports
**402** such comments. The only way to see them is to label comments the
extractor said nothing about.

`control` is not optional and is held at a third of each batch. A set built
only from failures measures failure; precision on ordinary comments — the
number that says whether the extractor is usable at all — can only come from
rows that were not chosen for being hard.

### Drafting, and the line a draft must not cross

A stronger model can draft the labels so the hour is spent reviewing rather
than authoring — but **only for the strata where agreement means something**:

```bash
python3 -c "
import json; d=json.load(open('next50.json'))
json.dump([e for e in d if e['_stratum']=='silent'], open('silent.json','w'), indent=2)
json.dump([e for e in d if e['_stratum']!='silent'], open('todraft.json','w'), indent=2)"

uv run python -m fragrance_graph.evals.autolabel draft drafted.json --from todraft.json
```

`--from` drafts the rows you chose. Without it, `draft` samples fresh and
**overwrites its output path**, which discards a targeted plan.

**Do not draft the `silent` rows.** They exist to find claims the extractor
missed; asking a second model whether the first model missed something, then
accepting "no", records an empty label and learns nothing. Label those cold.

Then review them one at a time:

```bash
uv run python -m fragrance_graph.evals.review drafted.json
```

Each row shows the comment and what the drafter said; `a` accepts, `n` records
that it asserts nothing, `t` fixes a claim type, `s` defers, `q` saves and
quits. Progress is written after every answer, so ten rows now and the rest
later is fine.

Drafts carry `drafted_by`, and `labels import` refuses to store them under a
non-draft labeler. Reviewing means deleting that marker — a deliberate act
that records a person standing behind the row. An all-empty file is refused
too, needing `--allow-empty`. Both guards exist because both failures
happened: 50 unreviewed drafts and 15 unread comments were imported as
ground truth on 2026-08-11, overwriting two hand-labelled rows, one with its
subject and object reversed.

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
  image. Until Phase D nothing in the codebase emitted markup at all; now
  that `pages.py` does, the rule is carried by
  `test_no_page_can_emit_an_image`, which asserts no generated page
  contains `<img`, `<svg` or a `background-image` — and by there being no
  image to reach for if one wanted to.
- **Every quote on a page is escaped.** Comment text is written by other
  people and reaches a page verbatim by design, which is exactly why it is
  escaped rather than trusted — `test_a_comment_containing_markup_is_
  rendered_not_executed`.
- **Nothing that ranks can be sorted by what it pays.** There is no
  commission column anywhere in the schema, which makes the rule a missing
  capability rather than a promise.

## What's next

Ordered by what unblocks the most. [SPEC.md](./SPEC.md) carries the full
argument for each; this is the short form.

1. ~~**Close the spend cap.**~~ **Done.** `$3.11` went out on a `$1.00`
   day. Every paid call now goes through `Budget`, the ledger resolves
   against the repo root rather than the working directory, and a missing
   ledger blocks spending instead of reading as zero. The loop also paid
   $0.05 a time for names it already knew, because `backfill` ran after
   the catalogue lookups instead of before — 48 lookups, 0 approved, $2.40
   for nothing. Fixed, and pinned by tests.

2. **Finish the eval set.** 15 comments verified by hand. One claim moves
   F1 by ~0.13, so the instrument cannot resolve a change smaller than
   itself, and two thresholds have already fired on noise. Target 200-500,
   stratified across denials, pronouns, flankers and multi-fragrance
   comments. **Do not tune the extraction prompt before this.**

3. **Broaden the discovery seeds.** Six of eight seed queries contain
   "dupe". That is why query diversity is low and why `--min-queries` is
   not enforced — raising the bar would punish edges for a bias in our own
   sampling. The loop is automated; choosing seeds that don't inherit our
   search bias is the part that isn't.

4. **Give the other 57% of the corpus a query.** `NOTE_DESCRIPTOR` is the
   largest claim type in the corpus and nothing can ask for it. Same for
   `LONGEVITY`, `PROJECTION`, `AESTHETIC`, `OCCASION`, and for 79 stored
   denials. `sentiment_rollup` is built and wired to no CLI. No new data,
   no API key. Note that a page built from `AESTHETIC` is a different
   editorial proposition from one built from `DUPE_OF` — real rows include
   *"smells like a prostitute"*.

5. **Semantic retrieval over community language.** 35 claims compare
   fragrances to everyday things — *"walking through a forest"*, *"a
   grandma cologne"* — which is the on-ramp for someone who has never
   smelled a fragrance and has no "I love X" to start from. The rule that
   keeps this honest: **embeddings retrieve; people's evidence decides.**
   Nothing computed from proximity is ever stated as similarity.

6. **Context-aware entity resolution.** Video titles are now stored, so
   "Perseus" under *"Maison Alhambra Perseus Review"* can resolve
   correctly — turning curation from human decisions with automation help
   into automatic resolution with human exception handling.

Deliberately not planned: Postgres or Neo4j (SQLite is nowhere near the
constraint), a new-release crawler (a bottle launched yesterday has no
discussion), video transcripts (owner-gated, and every workaround is the
scraping this project refuses), and computed similarity from notes.

## Running it without you

`.github/workflows/daily.yml` runs the loop on GitHub, Mondays and
Thursdays at 06:00 UTC, and on a button (`workflow_dispatch`). It rebuilds
the database from the committed corpus, collects, extracts, resolves,
rebuilds pages, and commits the corpus back.

Two things it deliberately does not do:

- **No catalogue lookups.** Measured 2026-08-12: 60 lookups converted 5
  names, because the catalogue does not carry the small-house bottles this
  corpus discusses. An unattended run must not spend on an 8% yield, so
  lookups stay manual — `daily curate` applies a review file with no
  network and no spend.
- **No news unless a person is needed.** A run that collected comments and
  published nothing is not worth an interruption. It opens *one* issue,
  labelled `loop`, and comments on it, when curation is holding rows or
  something failed. A fresh issue per run would train the reader to ignore
  it, which defeats the point.

Cost is about $0.20 a run, inside the $1/day cap — which now holds, since
the ledger is committed and read from the repository root rather than the
working directory.

The same run publishes `site/` to GitHub Pages, so the comparisons are a
URL rather than files on one laptop. Publishing is a separate job gated on
`needs: run`, which is the guarantee that matters: a run whose export was
refused never reaches it, so a broken run cannot replace the live site.

Pages stay gitignored. They cost no quota, no money and no judgement, so
they are rebuilt from the database rather than stored — which makes an
artifact exactly the right shape for them.

### Setup

Add three repository secrets under **Settings → Secrets and variables →
Actions**:

| secret | why |
|---|---|
| `YOUTUBE_API_KEY` | collecting comments |
| `FRAGRANCE_ANTHROPIC_API_KEY` | extraction. **Not** `ANTHROPIC_API_KEY` — some runners reserve that name and decline to pass a user-supplied one through |
| `FRAGELLA_API_KEY` | optional; unused by the schedule, since lookups are manual |

Then, under **Settings → Pages**, set **Source** to **GitHub Actions**.
Without that, the publish job fails with a permissions error however
correct the workflow is.

Then run it once by hand from the **Actions** tab to confirm it works
before trusting the schedule.

## Development

```bash
uv run ruff check .
uv run pytest
```

### The score dropped because the measurement got honest

Until 2026-08-11 this table published `SIMILARITY EDGES` F1 **0.89** and
overall **0.50**. Against 46 hand-verified train comments the same
extractor scores **0.57** and **0.40**.

Nothing regressed. The old figures came from **13** labelled comments, few
enough that a single claim moved F1 by more than a tenth, and they were
computed against a mixture of human and model labels — `load_labels` keyed
results by comment, so with three labelers per comment every row but the
last silently vanished and which one survived depended on the order SQLite
returned rows. The same corpus scored 0.41 and 0.38 on two machines the
day this was found. `score` now refuses to run without `--labeler` when
labelers collide, and the numbers above are `--labeler aanya-verified`.

What the honest numbers say, in product terms:

- **Precision 0.60** — when the extractor says two fragrances smell alike,
  it is right about six times in ten.
- **Recall 0.55** — of the comparisons people actually made, it finds
  about half. **The rest are missed silently**, and until the eval set
  included comments the extractor had said *nothing* about, those misses
  could not be counted at all.

Three claim types score **0.00**: `NOTE_DESCRIPTOR`, `LONGEVITY` and
`PROJECTION`. The first is diagnosed in SPEC as a magnet type and is the
next thing to fix.

The sample is still small — 46 comments yielding 18 label claims — so read
these as "roughly six in ten", not as three significant figures.
