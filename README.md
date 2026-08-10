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

**Steps 1–3 are built. Step 4 is not.** There is currently no way to ask "what
is similar to X" — the only query dumps every edge in the database.

[AUDIT.md](./AUDIT.md) is a read-only assessment of exactly what is real, what
is stubbed, and what has never been measured. **Where this README and AUDIT.md
disagree, AUDIT.md is right.** In particular:

- No corpus has been ingested into a persistent database yet.
- Extraction accuracy has never been scored against human labels, so its
  quality is unknown.
- There is no product, price, or retailer data of any kind.

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
`fragrance_graph.db` in the repo root), or `--db-path` on any command.

Ingest YouTube comments:

```bash
# by video id — cheap: 1 quota unit per 100 comments
uv run python -m fragrance_graph.ingest.youtube --video VIDEO_ID --limit 500

# or search first — costs 100 quota units per search
uv run python -m fragrance_graph.ingest.youtube --query "baccarat rouge dupe" --max-videos 5
```

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

## Measuring extraction quality

Extraction quality is judged against hand-written labels, not by reading output
and nodding. Export a template, fill in the claims each comment *should* yield,
then import and score:

```bash
uv run python -m fragrance_graph.evals.labels export labels.json
# fill in the "claims" list for each comment
uv run python -m fragrance_graph.evals.labels import labels.json --labeler you
uv run python -m fragrance_graph.evals.score
```

Comments split deterministically into `train` (70%) and `holdout`. Tune against
`train`; consult `--split holdout` only to confirm a prompt you have already
chosen. Scoring against the holdout while iterating turns it into training data
and the number stops meaning anything.

**Do not tune the extraction prompt before labels exist.** A change that looked
obviously correct once raised run-to-run variance sixfold and broke evidence
verification for the first time in the project; it had to be reverted. SPEC.md
records the measurements.

## Trust rules

These are product requirements, enforced in code and tests — not guidelines:

- **Ranking never considers commercial relationships.** Result order is
  computed with no knowledge of which fragrances are monetizable.
- **Results are never filtered to monetizable options.** A fragrance with no
  buying link ranks exactly as high as one with three.
- **Affiliate links are disclosed inline, at the link** — not once in a page
  footer where nobody reads it.
- **Text only.** Naming a fragrance identifies it; using a brand's logo or
  imagery borrows its authority.

## Development

```bash
uv run ruff check .
uv run pytest
```
