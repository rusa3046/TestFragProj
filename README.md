# fragrance-graph

Ingests fragrance discussion from Reddit, uses an LLM to extract structured
claims about fragrance similarity, and (eventually) builds a weighted graph
answering: "I love X — what else smells like it?"

See [SPEC.md](./SPEC.md) for the full design and phased build plan.

## Setup

```bash
uv sync --extra dev
cp .env.example .env  # fill in REDDIT_* and ANTHROPIC_API_KEY
```

## Usage

Initialize the database:

```bash
uv run python -m fragrance_graph.db init
```

Ingest YouTube comments (primary source — Reddit refused API access):

```bash
# by video id (cheap: 1 quota unit per 100 comments)
uv run python -m fragrance_graph.ingest.youtube --video VIDEO_ID --limit 500

# or search first (costs 100 quota units per search)
uv run python -m fragrance_graph.ingest.youtube --query "baccarat rouge dupe" --max-videos 5
```

Ingest Reddit comments (kept in case API access is ever granted):

```bash
uv run python -m fragrance_graph.ingest.reddit --subreddit fragrance --limit 500
```

`--limit` bounds submissions scanned per sort, not comments stored — one
submission can carry hundreds of comments. Ingest is idempotent on
`(source, source_id)` and commits in batches as it runs, so interrupting it
loses nothing already committed and re-running resumes rather than
duplicating.

## Measuring extraction quality

Extraction quality is judged against hand-written labels, not by reading
output. Export a template, fill in the claims each comment should yield,
then import:

```bash
uv run python -m fragrance_graph.evals.labels export labels.json
# fill in the "claims" list for each comment
uv run python -m fragrance_graph.evals.labels import labels.json --labeler you
uv run python -m fragrance_graph.evals.score
```

Comments are split deterministically into `train` (70%) and `holdout`.
Tune against `train`; consult `--split holdout` only to confirm a prompt
you have already chosen. Scoring against the holdout while iterating turns
it into training data and the number stops meaning anything.

## Development

```bash
uv run ruff check .
uv run pytest
```
