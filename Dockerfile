# FACET, as one image: the kiosk, the API, and a database built from the
# committed corpus at *build* time.
#
#   docker build -t facet .
#   docker run -p 8000:8000 facet
#   open http://localhost:8000/
#
# ## Why the database lives inside the image
#
# This project's README already argues that the database is disposable and
# the corpus is not: `data/corpus/*.jsonl` is the source of truth and a
# rebuild takes seconds. That makes the usual deployment question — which
# managed Postgres, provisioned how, migrated when — mostly beside the
# point for a demo. There is nothing in the database worth keeping that is
# not already in git.
#
# So the rebuild runs during `docker build` and the finished data
# directory is baked into the image. Measured on this corpus the full
# sequence takes ~56s, which is fine once at build time and much too slow
# on every container start, especially behind a host's health check. Boot
# is then just "start postgres, start uvicorn".
#
# The consequence, stated plainly because it is a real one: discovery
# sessions do not survive a restart. They are event-sourced into the same
# disposable database, so a redeploy drops them. For a demo that is the
# right trade; a real deployment would point `FRAGRANCE_DB_URL` at a
# managed instance and run the same seven commands against it (see
# `docker/entrypoint.sh`, which already honours that variable).
#
# ## Why postgres:16 rather than python:slim
#
# The project pins Postgres 16 and its migrations are written against it.
# Starting from the official image and adding Python is one apt install;
# starting from Python and adding a specific Postgres is a third-party
# apt repository and a version to keep in step with CI.
FROM postgres:16

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependencies first, so a source edit does not re-resolve the world.
# `--extra api` is required: FastAPI lives in that extra and a core
# install deliberately does not carry it (see pyproject.toml).
COPY pyproject.toml uv.lock ./
RUN uv sync --extra api --frozen --no-install-project

COPY src/ src/
COPY data/ data/
RUN uv sync --extra api --frozen

# NOT /var/lib/postgresql/data. The base image declares that path a
# VOLUME, and anything written to a declared volume during `docker build`
# is discarded when the layer is committed — the database would appear to
# build correctly and then be empty at runtime, which is precisely the
# kind of silent failure this repository keeps writing tests about.
ENV PGDATA=/opt/facet/pgdata
ENV FRAGRANCE_DB_URL="postgresql://postgres@:5432/fragrance_graph?host=/var/run/postgresql"

# Build the database. Postgres refuses to run as root, so every step here
# is the `postgres` user; the whole sequence is one RUN so the running
# server never has to survive between layers.
#
# The seven commands are the documented rebuild, in the documented order —
# the same list `.github/workflows/daily.yml` runs and for the same
# reason: `corpus import` restores what the corpus holds and nothing else.
# `attributes`/`semantic` are computed from it, and houses/retail/notes
# are curated input it never reads. Skipping the retail import in
# particular leaves a database that answers every query from community
# evidence alone — no error, just quietly worse answers.
RUN mkdir -p /opt/facet && chown -R postgres:postgres /opt/facet /app \
 && su postgres -c '\
      initdb -D "$PGDATA" >/dev/null \
   && pg_ctl -D "$PGDATA" -o "-c listen_addresses=" -w start \
   && createdb fragrance_graph \
   && cd /app \
   && uv run python -m fragrance_graph.db init \
   && uv run python -m fragrance_graph.corpus import \
   && uv run python -m fragrance_graph.attributes infer \
   && uv run python -m fragrance_graph.semantic backfill \
   && uv run python -m fragrance_graph.houses import \
   && uv run python -m fragrance_graph.retail import \
   && uv run python -m fragrance_graph.notes import \
   && pg_ctl -D "$PGDATA" -w stop' \
 # Refuse to ship an image whose catalogue did not land. The failure this
 # guards is not hypothetical: a deploy published a working site with an
 # empty `retailer_listings` and every answer silently fell back to
 # community-only candidacy.
 && su postgres -c '\
      pg_ctl -D "$PGDATA" -o "-c listen_addresses=" -w start \
   && test "$(psql -d fragrance_graph -tAc "select count(*) from retailer_listings")" -gt 0 \
   && test "$(psql -d fragrance_graph -tAc "select count(*) from claims")" -gt 100 \
   && pg_ctl -D "$PGDATA" -w stop'

COPY docker/entrypoint.sh /usr/local/bin/facet-entrypoint
RUN chmod +x /usr/local/bin/facet-entrypoint

EXPOSE 8000
USER postgres
ENTRYPOINT ["/usr/local/bin/facet-entrypoint"]
