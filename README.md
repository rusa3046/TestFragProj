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

Ingest Reddit comments:

```bash
uv run python -m fragrance_graph.ingest.reddit --subreddit fragrance --limit 500
```

`--limit` bounds submissions scanned per sort, not comments stored — one
submission can carry hundreds of comments. Ingest is idempotent on
`(source, source_id)` and commits in batches as it runs, so interrupting it
loses nothing already committed and re-running resumes rather than
duplicating.

## Development

```bash
uv run ruff check .
uv run pytest
```
