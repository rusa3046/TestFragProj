# Project audit — fragrance-graph

> **This is a historical snapshot, deliberately not updated.** It records
> what was true on 2026-08-10, and it is kept because an audit rewritten
> to match the present stops being evidence of anything. Its data-layer
> findings are long superseded — there is a corpus, a catalogue, a
> similarity engine and a retail product now; see the README, SPEC.md and
> [docs/FACET.md](./docs/FACET.md) for current state. Its **architectural**
> findings, and the failure shapes it names, are still live: several of the
> guards in the codebase exist because of this document.

Read-only audit performed 2026-08-10 against commit `a28c53a` on branch
`claude/fragrance-similarity-engine-13kmjd` (working tree clean, 0 commits
ahead/behind origin). Every claim below was verified by running code, not by
reading documentation. Nothing was modified except the creation of this file.

**Headline:** this is a working claim-extraction pipeline with no fragrance
database, no corpus on disk, and no similarity engine. The thing named in the
README ("what else smells like it?") does not exist in any form. What exists is
the plumbing that would feed it.

---

## 1. Repo map

Excludes `.venv/`, `.git/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`.

```
.
├── .env.example                              16   credential template; all values blank
├── .gitignore                                 9   ignores *.db, .env, seed/local-*.txt
├── README.md                                 69   setup + usage; see §5 for accuracy
├── SPEC.md                                  212   living design doc, phases 1–3
├── pyproject.toml                            38   deps, ruff, pytest config
├── uv.lock                                    –   lockfile (committed a28c53a)
├── seed/
│   └── example.txt                           14   3 synthetic comments, --- separated
├── src/fragrance_graph/
│   ├── __init__.py                            0   empty
│   ├── db.py                                 90   connection, migration runner, `init` CLI
│   ├── models.py                            187   Pydantic Claim + 11-type taxonomy enums
│   ├── migrations/
│   │   ├── 0001_init.sql                     67   fragrances, comments, claims, eval_labels
│   │   ├── 0002_comment_raw_json.sql          7   adds comments.raw_json
│   │   ├── 0003_taxonomy_v2.sql              64   rebuilds claims; DISCARDS all v1 claims
│   │   └── 0004_source_channel.sql            9   renames comments.subreddit → source_channel
│   ├── ingest/
│   │   ├── __init__.py                        0   empty
│   │   ├── reddit.py                        282   PRAW client + the shared ingest() write loop
│   │   ├── youtube.py                       289   YouTube Data API v3 over httpx, quota tracker
│   │   └── seed.py                          105   loads hand-pasted text from a local file
│   ├── extract/
│   │   ├── __init__.py                        0   empty
│   │   └── llm.py                           632   batching, Claude call, strict parse, cost log
│   ├── evals/
│   │   ├── __init__.py                        0   empty
│   │   ├── labels.py                        161   export/import label templates, 70/30 split
│   │   └── score.py                         194   precision/recall/F1 per claim type
│   └── resolve/
│       ├── __init__.py                        0   empty
│       ├── names.py                         167   name normalisation, junk rules, fuzzy match
│       └── entities.py                      308   curation CLI, backfill, edge query
└── tests/
    ├── __init__.py                            0   empty
    ├── conftest.py                           34   in-memory DB fixture, make_comment factory
    ├── test_models.py                       216   32 tests
    ├── test_ingest.py                       116    9 tests
    ├── test_seed.py                          66   11 tests
    ├── test_youtube.py                      225   14 tests
    ├── test_extract.py                      452   39 tests
    ├── test_evals.py                        237   23 tests
    └── test_resolve.py                      329   44 tests
```

**Totals:** 2,415 lines of Python under `src/`, 147 lines of SQL, 1,675 lines of
tests, 281 lines of Markdown docs.

`uv run pytest` → **172 passed in 1.43s**. `uv run ruff check .` → **All checks
passed**. No test touches the network; every external call is mocked.

---

## 2. Data layer

### What fragrance data exists on disk right now

**In the repository: none.** The default database path is `fragrance_graph.db`
(repo root) and that file **does not exist**. `*.db` is gitignored, so no
database has ever been committed. The only fragrance-related content tracked in
git is `seed/example.txt` — 3 synthetic sentences, explicitly labelled synthetic
in the file's own header.

**Outside the repository:** ten SQLite files in this session's ephemeral
scratchpad (`/tmp/claude-0/.../scratchpad/`). That directory is reclaimed when
the container dies. Contents, verified by direct query:

| file | bytes | comments | claims | fragrances | eval_labels | what it actually is |
|---|---|---|---|---|---|---|
| `kill.db` | 151,552 | 500 | 0 | 0 | 0 | synthetic fixtures, bodies read `comment number 0…499` — from the kill -9 resume test |
| `p2.db` | 53,248 | 11 | 10 | 4 | 0 | **hand-fabricated** Phase 2 demo (see below) |
| `e.db` | 53,248 | 3 | 3 | 0 | 3 | eval demo; claims stamped `extraction_model='m'` |
| `seed.db` | 53,248 | 3 | 0 | 0 | 0 | `seed/example.txt` loaded |
| `yt.db` | 53,248 | **0** | 0 | 0 | 0 | empty — migrations only |
| `c1/c2/cred/demo/x.db` | 53,248 ea. | 0 | 0 | 0 | 0 | empty — migrations only |

**Total real fragrance discussion text on this disk: 3 rows**, and they are the
synthetic examples shipped in `seed/example.txt`. Everything else is either
empty or generated by test/demo scaffolding.

The ~274 live YouTube comments and 21 extracted claims described in earlier
sessions **are not on this machine.** Those runs happened on the user's local
machine; `yt.db` here has zero rows. UNKNOWN whether that data still exists
elsewhere.

### The p2.db demo data is fabricated — this matters

`p2.db` is the database behind the Phase 2 "it works" demonstration. Its
comment rows are not ingested content:

```
body='Red Temptation from ZARA vs BR540'  permalink='p'  created_utc=1  raw_json='{}'
body='Bujairami Lavish vs 540'            permalink='p'  created_utc=1  raw_json='{}'
body='thomas kosmala no. 4 vs BR MFK 540' permalink='p'  created_utc=1  raw_json='{}'
```

Template strings, a one-character placeholder permalink, and a Unix timestamp of
`1` (1 Jan 1970). The 10 claim rows were constructed in Python and written via
`write_claims()`, not returned by an API call. They carry
`extraction_model='claude-haiku-4-5'`, but that value is a module default
stamped at write time — **it is not evidence that any model produced them.**

The *mention strings* (`BR540`, `540`, `BR MFK 540`, `B540`) were copied from a
real earlier run, so the entity-resolution logic was exercised against realistic
input. The surrounding records were not.

### Sources: wired up vs planned

| source | code | credentials present | can run here | status |
|---|---|---|---|---|
| YouTube Data API v3 | `ingest/youtube.py`, 289 lines, real httpx calls | **no** — `YOUTUBE_API_KEY` unset | **no** | complete, unrunnable |
| Reddit (PRAW) | `ingest/reddit.py`, 282 lines | **no** — `REDDIT_CLIENT_*` unset | **no** | complete; API access was **refused by Reddit** per SPEC.md |
| Manual seed file | `ingest/seed.py` | n/a | **yes** | working |

No `.env` file exists. `ANTHROPIC_API_KEY`, `YOUTUBE_API_KEY`,
`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` are all unset in this environment.
Verified — every credentialed entry point exits with its guard message:

```
$ uv run python -m fragrance_graph.ingest.youtube --video dQw4w9WgXcQ --limit 5
YOUTUBE_API_KEY must be set. Create one at console.cloud.google.com …

$ uv run python -m fragrance_graph.ingest.reddit --subreddit fragrance --limit 1
REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set. …

$ uv run python -m fragrance_graph.extract.llm --limit 2
ANTHROPIC_API_KEY must be set. …
```

### Full schema

Schema read from a freshly migrated database (all 4 migrations applied).
Population percentages are from `p2.db`, the only file on disk with claims and
fragrances in it — an 11-comment fabricated sample, so treat these as "does the
column ever get written", not as corpus statistics.

**`fragrances`** — n=4 (all typed by hand at a CLI)

| column | type | not null | populated |
|---|---|---|---|
| `id` | INTEGER PK | – | 4/4 (100%) |
| `canonical_name` | TEXT | yes | 4/4 (100%) |
| `brand` | TEXT | no | **1/4 (25%)** |
| `house_year` | INTEGER | no | **0/4 (0%)** — never written by any code path |
| `aliases` | TEXT (JSON array) | yes | 4/4 present, **2/4 non-empty** |

**`comments`** — n=11

| column | type | not null | populated |
|---|---|---|---|
| `id` | INTEGER PK | – | 100% |
| `source` | TEXT | yes | 100% (`youtube`) |
| `source_id` | TEXT | yes | 100% — `UNIQUE(source, source_id)` is the idempotency key |
| `body` | TEXT | yes | 100% |
| `permalink` | TEXT | yes | 100% (placeholder `'p'` in this sample) |
| `created_utc` | INTEGER | yes | 100% (value `1` in this sample) |
| `source_channel` | TEXT | yes | 100% (placeholder `'c'`) |
| `score` | INTEGER | yes | 100% (all 0) |
| `extracted_at` | TEXT | no | 10/11 (90%) |
| `raw_json` | TEXT | yes | 11/11 present, **0/11 non-empty** — all `{}` |

**`claims`** — n=10

| column | type | not null | populated |
|---|---|---|---|
| `id` | INTEGER PK | – | 100% |
| `comment_id` | INTEGER FK | yes | 100% |
| `claim_type` | TEXT | yes | 100% |
| `subject_kind` | TEXT CHECK | yes | 100% (all `FRAGRANCE`) |
| `raw_subject_text` | TEXT | yes | 100% |
| `subject_frag_id` | INTEGER FK | no | **4/10 (40%)** |
| `object_kind` | TEXT CHECK | yes | 100% (9 `FRAGRANCE`, 1 `NONE`) |
| `raw_object_text` | TEXT | no | 9/10 (90%) |
| `object_frag_id` | INTEGER FK | no | 9/10 (90%) |
| `sentiment` | TEXT CHECK | yes | 100% |
| `confidence` | REAL | yes | 100% |
| `evidence_span` | TEXT | yes | 100% |
| `evidence_verified` | INTEGER CHECK | yes | 100% (all 1 in this sample) |
| `extraction_model` | TEXT | yes | 100% |
| `created_at` | TEXT | yes | 100% |

**`eval_labels`** — `(comment_id, labeler)` PK, `labeled_json`, `created_at`.
n=0 in `p2.db`; n=3 in `e.db`, all from labeler `aanya`, two of which are empty
claim lists.

**`schema_migrations`** — `filename`, `applied_at`. 4 rows.

### Notes and accords

**No structured note or accord data exists at any granularity.** There is no
notes table, no top/mid/base decomposition, and no flat note list per fragrance.

The nearest thing is the `NOTE_DESCRIPTOR` claim type, which stores a free-text
`raw_object_text` with `object_kind='TAG'` — e.g. the string `"citrusy"` hanging
off a comment. That is an uncontrolled vocabulary of whatever words commenters
happened to use, attached to a *comment*, not to a fragrance. There is no
normalisation, no controlled note list, and no aggregation from claims up to a
fragrance. In `p2.db` there are **zero** `NOTE_DESCRIPTOR` rows; the
distribution is `DUPE_OF` 5, `SIMILAR_TO` 4, `PROJECTION` 1.

### Retailer, price, product-ID, brand metadata

**None, except one field.** `fragrances.brand` is a free-text nullable column,
populated on 1 of 4 rows. `fragrances.house_year` exists in the schema and is
written by **no code path** — `add_fragrance()` does not accept it.

There is no price, no currency, no SKU, no ASIN, no EAN/UPC, no retailer, no
stock, no product URL, no size/concentration, no image. A grep across all of
`src/` for `retail|price|affiliate|utm_|cosine|embed|vector|tfidf` returns
**five hits, all of them the Anthropic token prices in `extract/llm.py`.**

---

## 3. Similarity engine

### Does it run end to end today?

**There is no similarity engine.** Nothing in this codebase computes how much
two fragrances smell alike.

The closest running artefact is a SQL `GROUP BY … COUNT(*)` over resolved
claims. Actual command, actual output, against the fabricated `p2.db`:

```
$ uv run python -m fragrance_graph.resolve.entities edges --db-path .../p2.db
  n  type         edge
  2  DUPE_OF      Thomas Kosmala No. 4 -> Baccarat Rouge 540
  1  DUPE_OF      Dossier Ambery Saffron -> Baccarat Rouge 540
  1  DUPE_OF      Zara Red Temptation -> Baccarat Rouge 540
```

That is the entire "graph" output surface. Note what it is not: it takes **no
fragrance argument**. There is no way to ask "what is similar to X" — the
command dumps every edge in the database. With 4 curated fragrances and 10
fabricated claims, dumping everything is indistinguishable from querying; at
corpus scale it would not be.

Also note the `n` values: 2, 1, 1. The "weight" is a count of how many comments
asserted the pair. With one real comment behind each, these are not scores.

### What algorithm

None of vector embedding, weighted note overlap, TF-IDF, or cosine similarity
appears anywhere. Verified by grep across `src/`: zero hits for `embed`,
`vector`, `tfidf`, `cosine`.

There is exactly one function named `similarity`, and it does not measure
fragrance similarity:

- **`resolve/names.py:93 similarity(a, b)`** — `difflib.SequenceMatcher` ratio
  over two *normalised name strings*. Used only by
  `resolve/names.py:120 best_match()` to decide whether the typo
  `"Baccarat Rouge 54O"` refers to the same bottle as `"Baccarat Rouge 540"`.
  This is string matching for entity resolution. It is not a scent model.

The similarity signal in this project is entirely **asserted, not computed**:
an LLM reads a comment, decides a human said "X is a dupe of Y", and that
assertion becomes an edge. The only aggregation applied is counting duplicates.

- **`resolve/entities.py:197 RESOLVED_EDGES_SQL`** — the query. Filters to
  `evidence_verified = 1` and `claim_type IN ('SIMILAR_TO','DUPE_OF','BETTER_THAN')`,
  joins `fragrances` twice, groups by `(subject, object, claim_type)`.
- **`resolve/entities.py:210 resolved_edges(conn)`** — the wrapper.

Two consequences of the SQL worth stating: edges are **directional and
untransformed** — "A is a dupe of B" does not create "B is like A" — and
`BETTER_THAN` is pooled into the same edge set as `SIMILAR_TO` and `DUPE_OF`
with no sign or weight distinguishing it, despite being a preference claim
rather than a similarity claim.

### Inputs and outputs — exact shapes

```python
resolved_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]
# each Row has keys: subject (str), object (str), claim_type (str), mentions (int)
# no float score, no ordering key other than mentions DESC, subject

best_match(mention: str, candidates: list[Candidate], *, threshold: float = 0.88)
    -> Match | None
# Match(fragrance_id: int, canonical_name: str, method: str, score: float)
# method is "exact" (score always 1.0) or "fuzzy" (score = SequenceMatcher ratio)
```

### Has it been evaluated

**No. Quality is unverified.**

An eval harness exists (`evals/labels.py`, `evals/score.py`, 23 tests) and runs.
But the only labelled database on disk is `e.db`, with 3 labelled comments — two
of which have empty claim lists — scored against 3 claim rows whose
`extraction_model` is the literal string `'m'`, i.e. hand-inserted demo data:

```
$ uv run python -m fragrance_graph.evals.score --db-path .../e.db
split: train
Scored against 3 labelled comments.
OVERALL                  P 0.67  R 1.00  F1 0.80  (tp 2, fp 1, fn 0)
sentiment agreement      0.50 of matched claims
```

**Those numbers describe fabricated claims matched against three hand-written
labels. They say nothing about extraction quality.** No model output has ever
been scored against human labels in this repository. SPEC.md is candid about
this and records a hard rule — "do not tune the prompt without an eval set" —
after a prompt change measurably regressed output and had to be reverted
(`6a6d113`).

Entity resolution is likewise unmeasured: `p2.db` shows 13 mentions resolved,
all by exact alias match, **0 by fuzzy**. The fuzzy path — the one with a
tunable threshold and a real false-merge risk — has never run on data that
wasn't a unit test.

---

## 4. Entry points

Eight CLI modules. No HTTP routes, no API, no notebooks, no shell scripts, no
`__main__.py`, no console_scripts entry points in `pyproject.toml`. Every
invocation is `uv run python -m fragrance_graph.<module>`.

| entry point | subcommands / key flags | works here? | notes |
|---|---|---|---|
| `fragrance_graph.db` | `init`, `--db-path` | **YES** — verified | applied 4 migrations to a new file |
| `fragrance_graph.ingest.seed` | `<file>`, `--note`, `--db-path` | **YES** — verified | loaded 3 entries from `seed/example.txt` |
| `fragrance_graph.ingest.youtube` | `--video`, `--query`, `--limit` | **NO** | exits: `YOUTUBE_API_KEY must be set` |
| `fragrance_graph.ingest.reddit` | `--subreddit`, `--sort`, `--limit` | **NO** | exits: `REDDIT_CLIENT_ID and … must be set`; Reddit denied API access |
| `fragrance_graph.extract.llm` | `--limit`, `--batch-size`, `--model` | **NO** | exits: `ANTHROPIC_API_KEY must be set` |
| `fragrance_graph.evals.labels` | `export`, `import` | **YES** — verified | exported 11 comments (8 train / 3 holdout) |
| `fragrance_graph.evals.score` | `--split`, `--labeler` | **YES** — verified | ran on `e.db`; correctly reports no labels on `p2.db` |
| `fragrance_graph.resolve.entities` | `report`, `add`, `alias`, `backfill`, `edges` | **YES** — verified | all five subcommands ran; `edges` output in §3 |

**The chain is broken in the middle.** `db init` → `seed` works. `resolve` →
`edges` works. Between them sits `extract.llm`, the only path from comment text
to claim rows, and it requires an API key that is not present. There is no
offline, no-key way to get a claim into the database through a CLI — the Phase 2
demo data had to be inserted by calling `write_claims()` from a Python script.

---

## 5. Working vs stubbed

### WORKING — ran it, output was correct

- **Migration runner** (`db.py`). Applied all 4 migrations to a new file, then
  reported already-up-to-date on re-run. Tracked in `schema_migrations`.
- **Schema.** All four tables create cleanly with their CHECK constraints,
  foreign keys, and indexes.
- **Idempotent ingest write loop** (`ingest/reddit.py:ingest`). Shared by all
  three sources. `kill.db` holds 500 rows from a documented kill -9 resume test.
- **Seed loader.** 3 entries from a real file into a real database.
- **Pydantic claim model** (`models.py`). 11 claim types, kind/object-kind
  compatibility enforcement, evidence substring verification. 32 tests.
- **Entity resolution** (`resolve/names.py`). Normalisation, junk rejection,
  alias-before-junk ordering, fuzzy matching. 44 tests, cases drawn from real
  strings.
- **Curation CLI + backfill** (`resolve/entities.py`). All five subcommands run;
  backfill is idempotent and has a dry-run.
- **Edge query.** Produces counted edges — see §3 for what that is and isn't.
- **Eval harness mechanics.** Export/import/score run; deterministic 70/30 split
  keyed on `source_id`.
- **Test suite.** 172 tests, 1.43s, ruff clean, no network.
- **Cost accounting code** (`extract/llm.py:CostTracker`). Logic is real and
  tested; per-1k-comment figure is computed from actual token usage.

### STUBBED, BROKEN, FABRICATED, OR UNREACHABLE

- **The similarity engine.** Does not exist. No scoring, no ranking, no
  per-fragrance query, no algorithm. §3.
- **The graph.** Does not exist as a structure. There is a SQL count. No
  adjacency, no traversal, no symmetry, no transitivity, no weighting.
- **The fragrance catalogue.** 4 hand-typed rows in an ephemeral temp file. No
  notes, no accords, no brands to speak of, no products, no prices, no IDs.
- **The corpus.** Zero real comments on this disk. The largest comment table is
  500 rows of `comment number N`.
- **`p2.db` demo data — fabricated.** Bodies are `X vs Y` templates,
  `permalink='p'`, `created_utc=1`, `raw_json='{}'`. Claims hand-constructed.
  The `extraction_model='claude-haiku-4-5'` stamp is a default constant, not
  proof of an API call.
- **`e.db` eval data — fabricated.** Claims stamped `extraction_model='m'`.
  The P/R/F1 numbers it produces are meaningless as a quality signal.
- **Extraction, YouTube ingest, Reddit ingest.** Fully written, zero of the
  three runnable — no credentials.
- **Reddit ingest specifically.** 282 lines against an API that **refused
  access**. Dead code unless that decision reverses.
- **`fragrances.house_year`.** In the schema since migration 0001, written by no
  code path.
- **`comments.raw_json`.** Designed to retain the source payload; 100% `{}` in
  the only populated database, because the demo rows bypassed the real ingest.
- **Fuzzy matching.** Implemented and unit-tested, but has never fired on
  non-test data — 13/13 real-string resolutions were exact alias hits.
- **Seed loader defect.** `parse_entries()` splits on `---` but does not strip
  `#` comment lines, so `seed/example.txt`'s own three-line instructional header
  is stored as part of comment #1's body:
  `'# Seed file format: entries separated by…\n\nDelina smells just like Baccarat 540…'`.
  Reproduced on a fresh database. Reported, not fixed, per audit rules.
- **README overstatement.** Line 3 says the project "builds a weighted graph
  answering: 'I love X — what else smells like it?'" — hedged with "(eventually)",
  but no weighting and no such query exists. The README also documents the
  Reddit ingest without noting that API access was denied; SPEC.md does note it.

### Proportion

Roughly **35% of the stated product is real**, and it is the back half of the
data plumbing: schema, ingest write loop, claim model, extraction machinery,
entity resolution, evals harness. The front half — a fragrance catalogue — and
the entire product surface — similarity scoring, ranking, querying, serving —
are absent. The 172 passing tests measure the plumbing honestly; they do not
measure output quality, because nothing has been scored against human labels.

---

## 6. Blockers

| # | blocker | blocks |
|---|---|---|
| 1 | **No `ANTHROPIC_API_KEY`** | All claim extraction. Nothing can move from comment text to claim rows. Blocks the entire pipeline downstream of ingest. |
| 2 | **No `YOUTUBE_API_KEY`** | All ingest from the designated primary source. Without it, no corpus can be acquired at all. |
| 3 | **Reddit API access denied** (per SPEC.md) | The original primary source, permanently. 282 lines of working code are unreachable until/unless that reverses. |
| 4 | **No corpus on disk** | Every downstream measurement. Cannot evaluate extraction, cannot exercise fuzzy matching, cannot produce a real edge, cannot estimate cost on real data. Blocked behind #1 and #2. |
| 5 | **No hand-written eval labels for real extraction output** | Any claim about extraction quality, and — per the project's own rule in SPEC.md — any prompt change. Estimated ~1 hour of human labelling. Blocked behind #4. |
| 6 | **No canonical fragrance catalogue** | Everything commerce-facing: products, retailers, prices, URLs, pages. 4 hand-typed rows is not a catalogue and the schema has no room for product identity. |
| 7 | **All databases live in an ephemeral scratchpad** | Persistence. The container is reclaimed on inactivity and `*.db` is gitignored. Any work not re-derivable from source is lost. `p2.db` and `e.db` will not survive this session. |
| 8 | **No CLI path from comment → claim without an API key** | Local development and demoing. The Phase 2 demo required hand-writing rows via a Python script. |

Not blockers: dependencies install cleanly (`uv sync --extra dev`, 26 packages),
the test suite passes, lint is clean, git is clean and synced.

Note for anyone reproducing this: `uv sync` alone does **not** install pytest —
dev tools are an optional extra, so `uv run pytest` silently falls through to a
system pytest lacking the project's dependencies and fails at import with
`ModuleNotFoundError: No module named 'pydantic'`. The correct command is
`uv sync --extra dev`, as the README states.

---

## 7. Readiness assessment

### (a) Mapping a fragrance to purchasable products at specific retailers — **L**

**Exists:** a `fragrances` table with `id`, `canonical_name`, `brand`,
`aliases`; an alias-matching layer that can map messy input text to a canonical
row.

**Missing:** essentially all of it. No product entity (size, concentration,
formulation), no retailer entity, no offer/listing entity, no price, no
currency, no availability, no product identifiers (SKU/ASIN/EAN/UPC), no
retailer integration of any kind. Two new tables minimum plus a join model. The
harder half is not code: it is acquiring retailer catalogue data and matching
noisy consumer names to it, which is a second entity-resolution problem against
a source that does not exist here yet.

**Also note:** SPEC.md carries an explicit constraint prohibiting scraping
Fragrantica, Parfumo, or retailers on ToS grounds, and it is stated as a
publicly-demoable requirement. That rules out the cheapest path to catalogue
data and pushes this toward paid feeds or retailer affiliate APIs. That is a
business decision, not an engineering one, and it gates the estimate.

### (b) Attaching outbound URLs with tracking parameters to results — **S**, but gated by (a)

**Exists:** nothing. Zero occurrences of `utm_`, `affiliate`, or URL
construction in `src/`.

**Missing:** a URL template per retailer and a parameter builder. The code is
genuinely small — a function and a column. But there is nothing to link *to*
until (a) is done, and no result surface to attach links to until (e). **S** in
isolation, worthless in isolation.

### (c) Generating static HTML pages programmatically, one per fragrance pair — **M**

**Exists:** `resolved_edges()` already returns exactly the per-pair rows such
pages would be built from — subject, object, claim type, mention count. Claims
retain `evidence_span` and `comment_id`, so a page could quote real supporting
text and link back to the source comment via `permalink`. That is the substance
of a page and it is already modelled.

**Missing:** any templating (no Jinja, no template dir, no renderer), a slug
scheme, an output writer, an index. Also missing is anything worth publishing —
with 4 fragrances and 3 edges you get 3 pages, and the mention counts are 2, 1,
1. This is **M** on mechanics and blocked on corpus for value.

### (d) Ingesting a new unstructured review source, attaching sentiment to fragrance records — **S–M**

**This is the project's genuine strength.** The architecture already absorbed
exactly this once: when Reddit refused access, YouTube was added with **zero
changes to the write path**, because uniqueness is `(source, source_id)` and the
channel column was generalised in migration 0004.

**Exists:** the source-agnostic `ingest()` loop, a `sentiment` column on
`claims` (POSITIVE/NEGATIVE/NEUTRAL) already extracted per claim, `raw_json` for
source payload retention, evidence verification, and a resolution layer that
attaches claims to `fragrance_id`. A new source needs a `normalize_comment()`
plus an iterator — that is the shape of `ingest/youtube.py`, ~290 lines
including quota accounting and error handling.

**Missing:** sentiment is stored per *claim*, never aggregated to the fragrance
level — no rollup, no counts, no net score per fragrance. That is a query and a
view, not an architecture change. **S** for the ingest, **M** if the aggregate is
part of the ask.

**Caveat:** every new source multiplies extraction cost, and extraction quality
remains unverified (§3). Adding sources before labelling scales an unmeasured
error rate.

### (e) Serving any of this over HTTP — **M**

**Exists:** nothing. No web framework in `pyproject.toml` (deps are pydantic,
anthropic, praw, httpx, python-dotenv), no routes, no ASGI app, no server. The
spec explicitly deferred this ("No web framework yet").

**Missing:** the framework, an app, and — more significantly — a query layer.
There is currently no function that answers "given fragrance X, return ranked
similar fragrances". `resolved_edges()` dumps the whole table with no argument
and no ordering beyond raw count. An HTTP endpoint needs something to call.
Pydantic models are already in place for response serialisation, which helps.
**M**, and it should follow a real ranking function rather than precede it.

---

## 8. Honest assessment

Today, for a real user with no further work, this project does **nothing** —
every entry point that touches the outside world exits immediately on a missing
credential, and there is no fragrance data on disk beyond three synthetic
example sentences and four hand-typed names in a temp file that will not survive
this container. What genuinely exists is a well-built and well-tested data
pipeline: a migration-managed schema, an idempotent resume-safe ingest loop
proven against a kill -9, a strict Pydantic claim model with evidence
verification, LLM extraction with real cost accounting, and an entity-resolution
layer that correctly collapses five spellings of Baccarat Rouge 540 into one
node. There is no similarity engine of any kind — no embeddings, no note
overlap, no TF-IDF, no scoring function; similarity is asserted by an LLM
reading comments and the only aggregation is `COUNT(*)`, and there is not even a
way to query it for one fragrance. Extraction quality has never been measured
against human labels, so the accuracy of the one thing that does produce output
is unknown, and the project's own SPEC.md correctly forbids tuning it further
until that changes. Give it an Anthropic key, a YouTube key, and an afternoon of
ingest and labelling, and it would produce a small real graph — but the product
in the README is roughly a third built, and the missing two-thirds are the
half users would actually see.
