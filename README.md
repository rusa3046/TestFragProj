# claim-graph

**The repository is named for what the system does: it turns unstructured
text into a graph of attributed claims.** Fragrance is the domain it does
that in — `fragrance_graph` is the package and the evidence graph it
builds; **FACET** is the retail product built on top. The technique
(typed claims, verified quotes, counted humans, polarity, provenance
tiers) is domain-agnostic; the discipline it enforces is not negotiable
in any domain.

Two things live here, one built on the other:

**The evidence graph.** Given a fragrance, what the community actually says
about it — dupes, similarity, notes, performance — extracted from YouTube
comments as typed claims, each backed by a verbatim quote linking to the
comment a real person wrote.

**FACET**, the retail product on top: a structured preference composer
("I like / I avoid / I want"), a deterministic recommender that candidates
from a 548-bottle retail catalogue and reranks with community evidence, and
a commerce presentation layer with audited, tier-gated wording. See
[docs/FACET.md](./docs/FACET.md) for the product decisions and
[FACET — the demo that answers back](#facet--the-demo-that-answers-back)
below to run it.

Similarity and perception here are *asserted*, never computed. Nothing in
this system models what a fragrance smells like. It reads what people said,
extracts structured claims, resolves the names to real bottles, and counts
how many distinct people made the same claim.

A buyer deciding whether to spend £120 is not served by a cosine distance
of 0.87. They are served by *"31 people called this a dupe of Baccarat
Rouge 540, and here is what nine of them said."* The evidence is the
feature; the ranking is just how the evidence is sorted.

The system now also holds **declared notes** — what a brand or retailer
lists as being in the bottle — imported from licensed retailer listing
data, never scraped (Fragrantica-style pyramids remain off limits; see
Constraints in [SPEC.md](./SPEC.md)). Declared and perceived are kept in
separate tables and separate wording on purpose: a brand listing "rose" is
a fact about the listing; nine wearers calling it rose-forward is evidence.
The contrast between the two is part of the product.

## How it works

```
YouTube comments → claim extraction → entity resolution → ranked answers
  (Data API v3)    (Claude Haiku 4.5)   (name → bottle)     (+ evidence)
                                              ↑
                                     offline curation
                                  (a person, no network)

retail catalogue (declared notes, families, priors) → candidates
                    community evidence  →  rerank + annotate   → FACET
```

1. **Ingest.** Official platform APIs only. Comments land in PostgreSQL,
   idempotent on `(source, source_id)`, resumable mid-run.
2. **Extract.** Claude reads batched comments and returns typed claims —
   `DUPE_OF`, `SIMILAR_TO`, `NOTE_DESCRIPTOR`, `LONGEVITY`, and seven more.
   Every claim carries an `evidence_span` quoted from the comment, verified
   against the comment body before it is stored, plus a `polarity` recording
   whether the commenter asserted the relationship **or denied it**.
3. **Resolve.** `BR540`, `540` and `Baccarat Rouge` are one bottle. Curated
   aliases plus conservative fuzzy matching collapse them into a single node.
   What is left is named by a person, offline: `resolve.entities batch`
   writes a review file carrying two real comments and the video titles
   behind each mention, ordered by how many pages naming it would publish
   — see [docs/CURATION.md](./docs/CURATION.md). A catalogue API used to
   do this and was removed on 2026-08-14: 60 lookups, $3.00, 5 names, 0
   pages. It does not carry the small houses this corpus discusses.
4. **Answer.** Ranked by distinct commenters, with quotes and permalinks.
5. **Sell.** FACET generates candidates from the retail catalogue, reranks
   them with whatever the graph knows, and words each card to match how
   much that is. A bottle nobody has discussed is still recommendable —
   on its declared chemistry, and saying so.

### Which sources are live

| source | status |
|---|---|
| **YouTube Data API v3** | **live** — the entire comment corpus |
| **Anthropic API** | **live** — extraction, and eval-label drafting |
| **Nordstrom retailer listings (via Bright Data)** | **live** — 773 listings collected 2026-08-16 under an API licence, curated to facts-only JSONL (`data/curation/retailer-listings.jsonl`); raw scraped prose never merges |
| Wikidata | **live** — house registry and release seeding (`data/curation/`) |
| Fragella | **removed 2026-08-14.** 60 lookups produced 5 names and 0 pages |
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

**The whole stack is built and has run on real data**: the pipeline, the
static pages, and the FACET service with its composer, commerce cards and
catalog-first recommender. The corpus, the claims, the eval labels, the
catalogue, the retailer listings and the retrieval provenance are all
committed, so a clean clone reproduces every number on this page.

**The catalogue no longer gates on curation.** 548 bottles are candidates
for every FACET answer; community evidence — currently covering 129 of
them — reranks and annotates, but a bottle nobody has commented on can
still be recommended on its declared chemistry. Comparison *pages* still
gate hard, by design: 23 of 120 resolved pairs clear the 3-commenter /
2-creator bar.

Corpus as of 2026-09-05 (see [data/corpus/PROVENANCE.md](./data/corpus/PROVENANCE.md)):

| | |
|---|---|
| Comments | 13,267 across 1,091 videos / 404 channels |
| Claims | 5,543 |
| Extraction cost | $0.3656-$0.4410 per 1k comments, and it moves with the query |
| Catalogue | 548 bottles; 146 with community evidence |
| Retailer listings | 773 (Nordstrom); 475 resolve to a catalogue bottle |
| Declared notes | 2,778 rows covering 411 of 548 bottles |
| Labelled comments | 86 distinct comments labelled (165 label rows across labelers) |
| Extractor score | `SIMILARITY EDGES` F1 **0.57** (P 0.60, R 0.55); OVERALL F1 0.40 — measured 2026-08-11 |
| Denials caught | 35 of 38 flagged (92%), plus 32 the pattern missed |
| Spent to date | $7.43 — under a $1.50/day cap enforced from a committed ledger |

### The edge funnel — where the graph actually is

Counted on the 2026-08-20 corpus (11,632 comments, 4,964 claims); the
funnel is re-measured when the pages are rebuilt, not on every daily run:

```
4,964  all claims
1,734  comparison types      (SIMILAR_TO / DUPE_OF / BETTER_THAN)
1,540  FRAGRANCE -> FRAGRANCE
1,387  ASSERTED              (-153 denials)
1,379  evidence verified     (-8)
  319  both ends resolved    <- the dictionary is the constraint here
  120  distinct pairs
   23  pages published       <- 3+ commenters AND 2+ creators
```

**An edge needs *both* its subject and its object to be a resolved bottle**,
which is why 1,379 verified claims produce 319. Every filter above works;
the graph grows exactly as fast as resolution does. (At 17 curated entries
the both-ends line read 18; at 56 it read 109.) Note the funnel is the
*pages* product — FACET's recommender candidates from the catalogue and is
not bounded by it.

**The last step is the publishing gate, and it is meant to be lossy.** 120
pairs become 23 pages because a pair backed by two people, or by three
people under one creator, cannot honestly be headed "people say this". See
`pages.py` for why both bars are measured on the pair rather than on a
single claim type.

Resolution yield is superlinear because both ends must land — an early
table counting it on a 4,866-comment corpus showed the top 60–80 curated
names resolving 3–4× what the first 16 did, and the shape has held as the
corpus doubled. Most of the catalogue's 548 entries came from the retailer
import (`retail seed-from-listings`) rather than hand curation, which is
why the both-ends line grew from 109 to 304 without a proportional hour
count.

What the numbers above do **not** yet establish:

- **Extraction accuracy is measured on a small sample and is dated.** The
  published F1 comes from 46 hand-verified train comments, measured
  2026-08-11 — before the corpus doubled. OVERALL F1 once moved 0.50 →
  0.62 → 0.75 across code states whose differences account for about one
  claim each, so the eval cannot resolve a change smaller than itself.
  SPEC.md says which conclusions survive that and which do not.
- **Most comparison mentions are still unresolved.** 319 of 1,379 verified
  fragrance-to-fragrance claims have both ends resolved. The head of the
  unresolved-name list is short and repetitive, so the first hour of
  curation is worth far more than the last.
- **One curated entry was wrong and shipped.** `Perseus` is made by two
  houses; the bare alias pointed at the wrong one, producing an edge that
  misquoted three commenters. Found by research, fixed, recorded in SPEC.
  That is a ~6% error rate on entries called "confident", and it is the
  reason `--min-sources` and the 3-commenter bar exist.
- **`DUPE_OF` is over-firing since the polarity re-extraction.** 37 claims
  moved from `SIMILAR_TO`, and only 14 carry dupe language. Both are edges
  so the graph is unaffected, but "dupe" is a stronger claim than "similar"
  and page copy will repeat it. Unmeasured — the eval scores `DUPE_OF`
  precision 1.00 on its small sample and cannot see this.
- **Retailer data is real; affiliate links are not.** 773 Nordstrom
  listings with prices and declared notes are imported and drive
  catalog-first candidacy. The affiliate link builder still exists and is
  tested, but no affiliate account has been opened, so no page carries a
  live buying link.
- **Declared-note coverage is partial.** 411 of 548 catalogue bottles have
  declared notes; the gap is partly listings without a notes list and
  partly the 298 listings that resolve to no catalogued bottle. Bottles
  without notes fall to `NO_CATALOG_DATA` in the note-status ladder and
  say so, rather than being treated as note-free — which is the whole
  point of having four states instead of two.

New to the project? [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md) explains
the systems — the pipeline that builds the graph, the eval that measures
it, and the FACET service that sells from it — and which parts need a
human. [docs/FACET.md](./docs/FACET.md) records the product decisions.

[AUDIT.md](./AUDIT.md) is a read-only assessment of what is real, what is
stubbed, and what has never been measured. It predates this corpus, so its
data-layer findings are now out of date; its architectural ones are not.

## Setup

```bash
uv sync --extra dev          # --extra dev is required; plain `uv sync` omits pytest
cp .env.example .env         # fill in the keys below
```

### PostgreSQL

#### Why Postgres, when SQLite ran this fine

It *was* SQLite, until 2026-08-13. The move was not about size, and
saying otherwise would be the easiest number here to disprove: 11,632
comments and 4,964 claims is nowhere near SQLite's limits, and it would
still be comfortable a thousandfold up.

**What changed was the access pattern, not the row count.** Putting a web
service in front of the graph (FACET) means a shopper's session is being
written at the same moment a recommendation query is reading. SQLite
serializes writes at the level of the whole database file, so concurrent
readers and one writer is the shape it handles worst; Postgres is built
for exactly that shape. The migration happened when the second process
appeared, not when a table got big.

The cost of starting on SQLite and moving later was one afternoon of
porting and a migration set — cheaper than running a server for the
months when nothing was contending for the data. `docs/POSTGRES.md`
records the operational exercises run afterwards against a copy of this
schema at 3,000,000 comments and 1,200,198 claims, which is where the
scale questions actually got answered.

Every command needs a running server. Either path works; **the connection
string differs between them**, which is the one thing to get right.

**Homebrew.** The superuser is *your macOS username*, not `postgres` — so
the DSN leaves the user out and lets libpq default to it:

```bash
brew install postgresql@16
brew services start postgresql@16
createdb fragrance_graph
createdb fragrance_graph_test
```

```bash
# in .env
FRAGRANCE_DB_URL=postgresql:///fragrance_graph
FRAGRANCE_TEST_DB_URL=postgresql:///fragrance_graph_test
```

**Docker**, if you would rather not install a server. Here the user really
is `postgres`, and it has a password:

```bash
docker run -d --name fragrance-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=fragrance_graph postgres:16
docker exec fragrance-pg createdb -U postgres fragrance_graph_test
```

```bash
# in .env
FRAGRANCE_DB_URL=postgresql://postgres:postgres@localhost:5432/fragrance_graph
FRAGRANCE_TEST_DB_URL=postgresql://postgres:postgres@localhost:5432/fragrance_graph_test
```

If you get `role "postgres" does not exist`, you installed with Homebrew
and are using the Docker DSN. That is the whole error.

**The database is disposable and there is no backup, by design.** It is
rebuilt from `data/corpus/*.jsonl` in seconds. Drop it, bloat it, lock it
up — nothing of value is in there. That is also why it is a good place to
practise the operational side of Postgres against real data.

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

The connection string comes from `FRAGRANCE_DB_URL`, or `--db-url` on any
command. The default is:

```bash
export FRAGRANCE_DB_URL=postgresql://postgres@localhost:5432/fragrance_graph
```

The test suite uses a **separate** database, `FRAGRANCE_TEST_DB_URL`
(default `.../fragrance_graph_test`). It is deliberately not the same one:
the suite drops and rebuilds the schema at the start of every session, and
truncates every table between tests.

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
a database at all, and round-trip losslessly
— rows link by natural keys (`source` + `source_id`, and `canonical_name`),
never by autoincrement id, so a rebuilt database re-numbering its rows cannot
silently reattach a claim to the wrong comment. Export is byte-stable, so an
unchanged corpus produces an empty diff. Import is idempotent.

Export after any run that costs money or judgement, and commit the result.
Note that these files contain other people's comment text, retained under the
source platform's API terms — committing the export is republication, so treat
it as such.

### A rebuild needs a few more commands

`corpus import` restores everything the corpus holds — and nothing else.
Two tables are *computed from* the corpus rather than stored in it, and a
rebuild leaves them empty or silently stale:

```bash
uv run python -m fragrance_graph.attributes infer      # claim_attributions
uv run python -m fragrance_graph.semantic backfill     # evidence_embeddings
```

Three more tables aren't computed from the corpus at all — they are
*curated input*, committed separately under `data/curation/`, the same
category as the corpus JSONL but not part of it. `corpus import` never
reads them, so a rebuild leaves them empty too, for a different reason
than the two above:

```bash
uv run python -m fragrance_graph.houses import         # houses
uv run python -m fragrance_graph.retail import         # retailer_listings + declared notes
uv run python -m fragrance_graph.notes import          # brand-declared notes, if curated
```

All five commands are free and take seconds. `corpus import` prints a
notice naming whichever still need running, right after the import summary.
Skipping the retail import is the quiet failure worth naming: FACET's
recommender candidates from the catalogue, so a database without
`retailer_listings` still answers every query — from community evidence
alone, which is exactly the failure mode catalog-first generation was
built to remove. The scheduled workflow runs all five and refuses to
proceed if the listings did not land.

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
1. YouTube: search on seeds the catalogue chooses — the popular,
   note-carrying bottles the corpus cannot speak about yet
2. ingest -> extract
3. backfill: resolve everything the curated dictionary already covers
4. export -> pages -> report
```

Asking a catalogue what is **new** and *then* looking for discussion of it
answers the wrong question: a bottle launched yesterday has no YouTube
comments, so a release feed delivers fragrances that cannot produce an edge.
That argument stands, and SPEC records it in full.

**What changed on 2026-08-21 is which end of the catalogue gets asked.**
The seeds used to be ten bottles named by hand when the catalogue held 56.
It now holds 548, and a fixed list has no way to learn that — every run
poured more evidence onto Aventus and Layton while 419 bottles stayed
unrecommendable for want of a single claim. `daily.catalogue_seeds` picks
the seeds instead: catalogued bottles that carry declared notes, have no
community evidence, have never been searched before, and — the condition
that makes it work — carry a large **retailer review count**.

That last one is what keeps this demand-driven rather than a release feed.
A bottle with 15,227 Nordstrom reviews is not a bottle nobody discusses; it
is one *this corpus* has never asked about, which is a different thing.
Retail popularity is the only signal available that predicts YouTube
coverage without already being YouTube coverage. One bottle per brand, so
three flankers of the same scent cannot take three of ten slots, and the
shapes cycle through review / honest thoughts / worth it / smells like / vs
— no dupe shape, since the corpus is already saturated with it.

Rotation falls out of the same query: a bottle whose name appears in a past
`retrieval_query` is not asked again, so the corpus grows into the
catalogue rather than circling. `daily seeds` prints what the next run will
ask, beside what the corpus was actually built from.

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
   19 people  7 creators  12 queries  Al Haramain Detour Noir vs Parfums de Marly Layton
   12 people  6 creators   7 queries  Kilian Angels' Share vs Lattafa Khamrah
   11 people  7 creators  11 queries  Armaf Club de Nuit Intense Man vs Creed Aventus
   11 people  6 creators   7 queries  Lattafa Khamrah vs Lattafa Khamrah Qahwa
   11 people  4 creators   4 queries  Creed Aventus vs Creed Aventus Cologne
   10 people  4 creators   6 queries  Parfums de Marly Layton vs The Woods Collection Dusk
```

Twenty-three pairs clear the gate today. Each line also reports videos with
no retrieval record, so a query count is always a lower bound rather than
a claim.

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

**When this was first measured, six of the eight publishing pairs rested
on a single search query.** Three different `parfums de marly layton dupe`
videos are three separate comment sections, so the creator bar passes them
— but they are three rooms in which the same question was put to an
audience assembled for that question. The guard against one comment
section does not guard against one *query*.

**Broader seeding fixed most of it, and the counts show the work.** Of the
23 pairs publishing today, 4 rest on a single recorded query, 2 have no
retrieval record at all, and the other 17 are backed by two or more
distinct searches — the top pair by 12. Only 4 of 978 videos now predate
discovery tracking, down from 15 of 39, so the "lower bound" caveat has
nearly stopped mattering. Undocumented videos are never given an invented
query; they converge on the truth as recorded searches re-find them.

`--min-queries 2` still defaults to **1 — off**. The original reason was
that the number could not distinguish "these edges are weak" from "the
eight seed queries were too narrow", and those want opposite fixes: six of
those eight seeds contained the word "dupe" (see
[PROVENANCE](./data/corpus/PROVENANCE.md)). `daily.SEED_QUERIES` now
carries ten, only one of which is a bare "dupe" query, and the diversity
counts above moved accordingly — so raising the bar has become defensible
and is simply not yet done. Turning it on would unpublish the 4 pairs
resting on a single query; the 2 with no record at all pass through as
unknown, by the rule below.

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
  --base-url https://rusa3046.github.io/claim-graph/
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

## The demo surface

`pages build` writes three kinds of static page, all pure functions of the
corpus, all covered by the provenance audit:

- **Ask pages** — curated natural-language questions ("I love Delina but
  the rose is too strong") answered by the deterministic recommender, with
  the parsed plan shown and every evidence count worded by strength. One
  page is a deliberate refusal, because declining is part of the product.
- **Bottle profiles** — "what people say about X", one per bottle the
  corpus can actually speak about.
- **Pair pages** — the original comparisons, each behind the 3-people /
  2-creators gate.

No server: the recommender parses without a model, so an answer page costs
nothing to serve and nothing to rebuild.

## FACET — the demo that answers back

The static pages answer curated questions; FACET answers *yours*. It is
the same deterministic recommender behind a FastAPI service and a
single-file kiosk UI:

```bash
uv sync --extra api
uv run uvicorn fragrance_graph.api:app --host 0.0.0.0
# then open http://localhost:8000/
```

### Or as one container, database included

```bash
docker build -t facet .
docker run -p 8000:8000 facet
```

**The database is built during `docker build`, not at boot.** That
follows from something this README already argues: the database is
disposable and the corpus is not, so there is nothing in it worth
keeping that is not already in git. Measured on this corpus the full
seven-command rebuild takes ~56s — fine once at build time, much too
slow on every cold start with a visitor waiting. The image ships with
the data directory already populated, and boot is just "start postgres,
start uvicorn".

The build refuses to produce an image whose `retailer_listings` came out
empty, for the reason recorded above: that failure publishes a *working*
site that quietly answers from community evidence alone.

`fly.toml` deploys it (`fly launch --no-deploy --copy-config` then `fly
deploy`). No managed database, no connection string, no secret — which
is the practical dividend of keeping the corpus as the source of truth.
Sessions do not survive a redeploy, since they are event-sourced into
that same disposable database; for a demo that is the right trade, and a
real deployment would point `FRAGRANCE_DB_URL` at a managed instance
instead (`docker/entrypoint.sh` already honours it).

What you get, and where each piece is documented in
[docs/FACET.md](./docs/FACET.md):

- **A structured preference composer** — three buckets ("I like / I avoid
  / I want") filled with chips, plus a free-text box that compiles into
  the same preferences. A live rail shows how FACET read what you said,
  and every refused or unparsed utterance is visible rather than silent.
- **Catalog-first recommendations** — every one of the 548 catalogue
  bottles is a candidate; declared notes, derived note families and
  occasion priors generate the plausible set, and community evidence
  reranks within it. A bottle with zero comments can be recommended, and
  says so honestly ("Community insight: limited so far").
- **Commerce cards with audited wording** — tier-gated language
  ("Wearers consistently…" only above the strong-evidence bar), at most
  two shopper-relevant tradeoffs, counts and source attribution behind
  the full-story view, never on the card. A contradiction of something
  you explicitly avoided always surfaces as a caveat. Five tier labels,
  graded on catalogue fit and community fit separately; a card that
  answered nothing you asked, or whose tradeoffs outnumber what it did
  answer, is labelled *Closest Available* rather than *Worth
  Discovering*.
- **Event-sourced sessions** — every preference change is validated, then
  recorded; a session rebuilds from its log, and refining a preference
  re-runs the same state instead of starting over. *Love it* re-anchors
  the results on the loved bottle and the headline says so.
- **A page that states its own limits** — when a request leans on
  longevity or projection, which only wearers can report and only a few
  dozen bottles have reports on, the headline says so in one sentence
  rather than letting the shopper conclude the product knows fifteen
  perfumes. Budget chips name the size that cleared the bar.

Sessions live in the same PostgreSQL database and nothing about them is
required by the pipeline — drop the service and the graph is untouched.

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

2. ~~**Give the rest of the corpus a query.**~~ **Done, as FACET.**
   `NOTE_DESCRIPTOR`, `LONGEVITY`, `PROJECTION`, `AESTHETIC` and
   `OCCASION` are all reachable through the composer and the free-text
   parser, with denials excluded from every count and low-value negatives
   kept off customer surfaces by the wording audit.

3. **First-party feedback as a fourth evidence voice.** FACET already
   records "Love it / Maybe / Not for me" as session events. The plan of
   record (see [docs/FACET.md](./docs/FACET.md)): aggregate those into
   FACET's own evidence source with its own tier gating — silent until
   volume justifies speech, never blended into YouTube counts. YouTube
   bootstraps the cold start; the weighting shifts as first-party data
   accumulates.

4. **Finish the eval set.** 86 comments carry labels, fewer are
   hand-verified, and the published score predates the corpus doubling.
   One claim moves F1 by ~0.13, so the instrument cannot resolve a change
   smaller than itself, and two thresholds have already fired on noise.
   Target 200-500, stratified across denials, pronouns, flankers and
   multi-fragrance comments. **Do not tune the extraction prompt before
   this.**

5. ~~**Broaden the discovery seeds.**~~ **Done twice.** First by hand —
   ten mixed shapes replacing eight of which six said "dupe" — and then
   by removing the hand: the seeds now come from the catalogue
   (`catalogue_seeds`), which is the only version that keeps working as
   the catalogue grows. What is *not* done is the consequence:
   `--min-queries` is still off, because the diversity it would gate on
   has only just started improving. Raise it after a few runs of
   catalogue-seeded collection, not before.

6. **Context-aware entity resolution.** Video titles are now stored, so
   "Perseus" under *"Maison Alhambra Perseus Review"* can resolve
   correctly — turning curation from human decisions with automation help
   into automatic resolution with human exception handling.

7. **Open an affiliate account.** The link builder, the disclosure
   fields and the feed importer are all tested against fixtures; nothing
   has ever carried a live buying link. Needs a public site first, which
   exists now.

Deliberately not planned: Neo4j (the graph is not the constraint; curation
is), a new-release crawler (a bottle launched yesterday has no
discussion), video transcripts (owner-gated, and every workaround is the
scraping this project refuses), and computed similarity from notes —
declared notes generate *candidates* and are worded as catalog facts;
they are never converted into a similarity score.

## Running it without you

`.github/workflows/daily.yml` runs the loop on GitHub, Mondays and
Thursdays at 06:00 UTC, and on a button (`workflow_dispatch`). It rebuilds
the database from the committed corpus *and the curated inputs*, collects,
extracts, resolves, rebuilds pages, and commits the corpus back. It then
asserts the rebuild landed — fragrances present, retailer listings
present, derived tables fresh — and refuses to run on anything less,
because every one of those has failed silently at least once and a
scheduled run has nobody to read a warning.

The `collect` input splits publishing from spending: `collect: false`
rebuilds and republishes the site without making a single paid call,
which is what a deploy of a code change should cost.

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

Commits go through `scripts/checkpoint.sh`, which runs five gates as
separate checked steps and refuses to commit if any fails — it exists
because a pipeline's exit status is its last command's, and a commit
landed with failing tests twice from exactly that shape:

1. **ruff** — seconds, catches most mechanical breakage
2. **pytest** — the full suite
3. **provenance audit** — the cross-surface wording contract
4. **recommendation benchmark** — unsupported assertions must be 0
5. **card golden** — the assembled sentences a shopper reads, diffed
   against `data/eval/cards.golden.txt`

The fifth is the newest and checks a different *kind* of thing: the
first four check rules, it checks the artifact. Six defects reached a
person's screen in one week with all 1,815 unit tests passing, because
each lived in the assembled card — a composition of a dozen correct
functions plus the corpus plus the ordering. `evals/cards.py` renders 21
real compositions through the same `api._session_response` the kiosk
calls; when a customer-visible sentence changes, the golden moves, and a
person reads the diff and commits it **with** the change that caused it
(`uv run python -m fragrance_graph.evals.cards --update`).

**Documentation moves with the code, in the same commit.** README,
[docs/FACET.md](./docs/FACET.md) and the rest of `docs/` describe the
current state of the project, not the state at some earlier milestone —
a doc that lags its code is worse than no doc, because it reads as
authority while being wrong. A behaviour change that would surprise a
reader of these files is not done until the files say otherwise.

### The score dropped because the measurement got honest

Until 2026-08-11 this table published `SIMILARITY EDGES` F1 **0.89** and
overall **0.50**. Against 46 hand-verified train comments the same
extractor scores **0.57** and **0.40**.

Nothing regressed. The old figures came from **13** labelled comments, few
enough that a single claim moved F1 by more than a tenth, and they were
computed against a mixture of human and model labels — `load_labels` keyed
results by comment, so with three labelers per comment every row but the
last silently vanished and which one survived depended on the order the
database returned rows. The same corpus scored 0.41 and 0.38 on two machines the
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
