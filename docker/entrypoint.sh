#!/usr/bin/env bash
#
# Start FACET: the database the image already carries, then the service.
#
# Boot is deliberately dumb. Everything expensive — the migrations, the
# corpus import, the derived tables, the curated imports — happened during
# `docker build`, so there is nothing to compute here and nothing that can
# half-succeed while a host's health check is counting.
set -euo pipefail

PORT="${PORT:-8000}"

# An external database wins. Hosts that provide managed Postgres set
# FRAGRANCE_DB_URL for you, and in that case the baked-in server is not
# just unnecessary, starting it would leave the app talking to a database
# nobody asked for. The seven rebuild commands are NOT run here for that
# case on purpose: pointing at a managed instance means deciding when it
# gets rebuilt, and doing it silently on every boot would wipe live
# sessions on each redeploy.
if [[ -n "${FRAGRANCE_EXTERNAL_DB:-}" ]]; then
  echo "facet: using external database from FRAGRANCE_DB_URL"
else
  export PGDATA="${PGDATA:-/opt/facet/pgdata}"
  export FRAGRANCE_DB_URL="${FRAGRANCE_DB_URL:-postgresql://postgres@:5432/fragrance_graph?host=/var/run/postgresql}"
  pg_ctl -D "$PGDATA" -o "-c listen_addresses=''" -w start
  echo "facet: database up"
fi

# The venv's own uvicorn, not `uv run`. `uv run` re-resolves the project
# on every invocation and wants a writable cache; at runtime the image is
# already built, the environment is already correct, and the only thing
# that resolution can do is fail in a container that has no business
# talking to a package index at all.
#
# `exec` so uvicorn is PID 1 and receives the host's SIGTERM directly.
# Without it the shell holds PID 1, swallows the signal, and every deploy
# waits out the platform's kill timeout instead of shutting down.
exec /app/.venv/bin/uvicorn fragrance_graph.api:app --host 0.0.0.0 --port "$PORT"
