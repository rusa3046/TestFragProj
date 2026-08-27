#!/usr/bin/env bash
#
# checkpoint.sh — commit only if the tree is actually green.
#
#   scripts/checkpoint.sh -m "message"
#   scripts/checkpoint.sh -F message-file
#   scripts/checkpoint.sh --quick -m "message"     # skip the slow gates
#
# ---------------------------------------------------------------------------
# Why this exists
#
# Twice now a commit has landed with a failing test, both times from the
# same shape:
#
#     uv run pytest -q 2>&1 | tail -3 && git commit ...
#
# A pipeline's exit status is the *last* command's, so that reads as "if
# `tail` succeeded, commit" — and `tail` always succeeds. The tests were
# printed, the failure was visible in the output, and the commit went
# through anyway.
#
# Knowing that about `$?` and pipelines does not prevent it. Getting it
# right requires remembering, at the moment of typing, in a long command
# assembled for another purpose. This script does the remembering: the
# gates run as separate checked steps and a non-zero exit anywhere stops
# the commit.
#
# The gates, in the order that fails fastest:
#
#   1. ruff        — seconds, catches most mechanical breakage
#   2. pytest      — the full suite
#   3. audit       — the cross-surface provenance contract
#   4. benchmark   — recommendation eval; unsupported assertions must be 0
#   5. cards       — the assembled sentences a shopper reads, vs the golden
#
# 3, 4 and 5 need a populated developer database and are skipped with a
# loud notice when one is absent, rather than silently passing.
#
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

QUICK=0
GIT_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) QUICK=1; shift ;;
    *) GIT_ARGS+=("$1"); shift ;;
  esac
done

if [[ ${#GIT_ARGS[@]} -eq 0 ]]; then
  echo "checkpoint: nothing to pass to git commit (need -m or -F)" >&2
  exit 2
fi

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
fail() { printf '\n\033[31mcheckpoint: %s — NOT committing\033[0m\n' "$1" >&2; exit 1; }

step "ruff"
uv run ruff check . || fail "lint failed"

step "pytest"
# No pipe. A pipeline would hand its own exit status to the `if`, which is
# the entire bug this script exists to prevent.
uv run pytest -q || fail "tests failed"

if [[ "$QUICK" == "1" ]]; then
  echo "(--quick: skipping audit, benchmark and card golden)"
else
  step "provenance audit"
  if uv run python -m fragrance_graph.audit >/tmp/checkpoint-audit.txt 2>&1; then
    tail -2 /tmp/checkpoint-audit.txt
  else
    tail -20 /tmp/checkpoint-audit.txt
    fail "the provenance audit found violations"
  fi

  step "recommendation benchmark"
  if uv run python -m fragrance_graph.evals.recommend \
        >/tmp/checkpoint-bench.txt 2>&1; then
    sed -n '/passed overall/p;/UNSUPPORTED/p' /tmp/checkpoint-bench.txt
  else
    tail -20 /tmp/checkpoint-bench.txt
    # The benchmark reads `claim_attributions` and `evidence_embeddings`,
    # and a `corpus import` leaves the first empty and the second full of
    # rows pointing at claim ids it just deleted. A rebuilt database
    # therefore scores 20/22 for reasons that have nothing to do with the
    # commit being attempted. Say so here rather than let it read as "the
    # benchmark broke".
    uv run python - <<'PY' || true
from fragrance_graph.corpus import derived_state, rebuild_notice
from fragrance_graph.db import DEFAULT_DB_URL, get_connection
conn = get_connection(DEFAULT_DB_URL)
notice = rebuild_notice(derived_state(conn))
if notice:
    print(f"\n{notice}")
conn.close()
PY
    fail "the benchmark reported unsupported assertions"
  fi

  step "card golden"
  # The other three gates check rules. This one checks the artifact: the
  # assembled sentences a shopper actually reads, rendered through the
  # same function the kiosk calls. Six defects reached a screen in one
  # week with every unit test passing, because a card is a composition of
  # a dozen correct functions plus the corpus plus the ordering.
  if uv run python -m fragrance_graph.evals.cards >/tmp/checkpoint-cards.txt 2>&1; then
    tail -1 /tmp/checkpoint-cards.txt
  else
    tail -40 /tmp/checkpoint-cards.txt
    fail "the customer-visible cards drifted from data/eval/cards.golden.txt"
  fi
fi

step "committing"
git commit "${GIT_ARGS[@]}"
printf '\n\033[32mcheckpoint: green, committed\033[0m\n'
