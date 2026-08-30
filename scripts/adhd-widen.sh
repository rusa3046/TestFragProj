#!/usr/bin/env bash
#
# adhd-widen.sh — widen a decision before committing to the first plausible
# answer, and refuse to call the result reviewed until a person has ruled on
# every idea.
#
#   open  <slug> <question> [context-file …]   run ADHD, write a decision record
#   check <slug>                               fail while any verdict is unreviewed
#   list                                       every decision record and its state
#   estimate [context-bytes]                   what a run would cost, without running
#
# ---------------------------------------------------------------------------
# What this lane is, next to the other one
#
# `codex-agent.sh` sends a frozen commit to an outside model that **narrows**:
# it reports what is wrong, and `/codex-checkpoint` makes a person open every
# cited file before a finding is accepted. This is the mirror. ADHD spawns N
# isolated reasoning processes under deliberately distorted frames — a
# regulator, a hardware engineer, a speedrunner — with no shared context, then
# a separate critic scores, clusters, flags traps and deepens the survivors.
# It **widens**: it reports what is possible.
#
# The discipline is the same discipline because the risk is worse. A Codex
# finding that is wrong costs you the ten minutes it takes to open the file
# and disagree. An ADHD idea that is wrong is plausible, costs nothing to
# read, and costs a week to unbuild. So the record lands with every idea
# marked `unreviewed` and `check` fails until each one carries a verdict and
# a reason. See scripts/adhd_render.py for why the gate is shaped that way.
#
# ---------------------------------------------------------------------------
# Four things this lane deliberately does not do
#
#   1. **It never runs inside the product path.** Nothing here is importable
#      by `fragrance_graph`. The recommender is deterministic and every card
#      is tier-gated; the whole claim of this project is that similarity is
#      asserted and counted, never generated. An ideation engine anywhere
#      near `recommend.py` would break exactly the property FACET sells.
#      This is a dev-loop tool that writes markdown to `docs/decisions/`.
#
#   2. **It does not use the in-session skill.** ADHD's own docs are explicit
#      that each divergence branch is a fresh isolated context, so the base
#      substrate — CLAUDE.md, tool context, the session's history — is paid
#      once *per branch*. This repo's CLAUDE.md is several thousand words of
#      operating doctrine, which is exactly the substrate you do not want
#      multiplied by N. The standalone CLI carries only the problem, the
#      frame, and whatever `--context` you hand it deliberately.
#
#   3. **It does not write to the spend ledger.** `data/spend.jsonl` is
#      append-only and its value is that it records money actually spent;
#      the `adhd` CLI reports no cost, so anything written there would be an
#      estimate that can never be settled against a real figure — a wrong
#      number that, by that ledger's own rules, stays written. The estimate
#      is printed here instead, before anything is spent.
#
#   4. **It does not consult the daily cap.** `fragrance_graph.budget` guards
#      things that run unattended, where the failure is a cheap bug repeating
#      on a schedule. This lane only ever runs because a person typed a
#      question, and blocking a deliberate $0.40 decision run against a cap
#      reserved for the scheduler would be the cap doing the opposite of its
#      job.
#
# ---------------------------------------------------------------------------
# On a dirty tree
#
# Unlike the Codex lanes, this one does not require a clean tree, and the
# difference is not an oversight. Codex reviews a *commit* — a moving target
# makes the finding meaningless, and the lane refuses so the review describes
# something real. ADHD reasons about a *question*. The state of the tree is
# not the subject, and refusing here would block the most valuable moment to
# widen: halfway into a change, when you have discovered the design is wrong.
#
# What is recorded instead is the commit the run happened at, in the record's
# header, so a record read later can be placed in the history.
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
  echo "adhd-widen: not inside a git repository." >&2
  exit 2
fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"

DECISIONS_DIR="$REPO_ROOT/docs/decisions"
RUNS_DIR="$REPO_ROOT/.adhd-runs"
RENDER="$REPO_ROOT/scripts/adhd_render.py"

# Tunable per run. The defaults are the CLI's own, restated here so the cost
# estimate below is computed from the numbers actually passed.
FRAMES="${ADHD_FRAMES:-5}"
IDEAS="${ADHD_IDEAS:-6}"
TOP="${ADHD_TOP:-3}"
# A different critic family decorrelates critic errors from generator errors —
# the same reasoning as the blind-check discipline in the README, and the fix
# for the same-model-judging limitation ADHD documents against itself. Left
# empty by default because it needs a model name the caller can actually reach.
CRITIC_MODEL="${ADHD_CRITIC_MODEL:-}"

# Print the command instead of running it, so the shape of this lane can be
# checked without spending a request or holding credentials. Same purpose as
# CODEX_AGENT_DRY_RUN next door.
DRY_RUN="${ADHD_WIDEN_DRY_RUN:-0}"

die() { echo "adhd-widen: $*" >&2; exit 1; }

require_adhd() {
  command -v adhd >/dev/null 2>&1 || die "adhd CLI not found. npm install -g adhd-agent
     Then authenticate: export ANTHROPIC_API_KEY=… (or inherit from a local Claude Code install)."
}

# Slugs become filenames and are echoed into a shell command. Constrained
# rather than quoted: a slug is a name someone types, and the set of good
# names does not include the ones that need escaping.
require_slug() {
  [[ "$1" =~ ^[a-z0-9][a-z0-9-]{1,60}$ ]] || die "slug must be lowercase letters, digits and dashes: got '$1'"
}

record_path() { echo "$DECISIONS_DIR/$1.md"; }

# The honest version of "how much will this cost", which is a count of calls
# and a warning about the multiplier, not a dollar figure. Token price moves,
# context size is knowable only at run time, and a made-up number here would
# be quoted back later as though it had been measured.
print_estimate() {
  local context_bytes="$1"
  local calls=$(( FRAMES + TOP + 2 ))
  echo "estimate"
  echo "  frames        : $FRAMES  (parallel, isolated — no shared context)"
  echo "  ideas/frame   : $IDEAS"
  echo "  deepened      : $TOP"
  echo "  LLM calls     : ~$calls  ($FRAMES diverge + 1 score + 1 cluster + $TOP deepen)"
  echo "  context/branch: $context_bytes bytes, re-sent to each of the $FRAMES branches"
  echo "                  (~$(( context_bytes * FRAMES )) bytes of context total — this is the"
  echo "                   multiplier the '~$calls calls' framing hides)"
  echo "  cost          : cents to low dollars. Not recorded to data/spend.jsonl —"
  echo "                  see the header of this script for why."
}

cmd_open() {
  [[ $# -ge 2 ]] || die "usage: open <slug> <question> [context-file …]"
  local slug="$1"; shift
  local question="$1"; shift
  require_slug "$slug"
  require_adhd

  local record; record="$(record_path "$slug")"
  if [[ -e "$record" ]]; then
    die "$record already exists.
     A decision record is a record. Pick a new slug for a fresh question, or
     edit that file if you are still ruling on the same one."
  fi

  # Context files are concatenated with a header each, so the model can tell
  # where one file ends and the next begins. The CLI's --context takes one path.
  local context_file="" context_bytes=0
  if [[ $# -gt 0 ]]; then
    mkdir -p "$RUNS_DIR"
    context_file="$RUNS_DIR/$slug.context"
    : > "$context_file"
    local path
    for path in "$@"; do
      [[ -f "$path" ]] || die "context file not found: $path"
      case "$(cd "$(dirname "$path")" && pwd -P)/" in
        "$REPO_ROOT"/*|"$REPO_ROOT"/) ;;
        *) die "context file is outside this repo: $path" ;;
      esac
      {
        echo "===== $(realpath --relative-to="$REPO_ROOT" "$path" 2>/dev/null || echo "$path") ====="
        cat "$path"
        echo
      } >> "$context_file"
    done
    context_bytes="$(wc -c < "$context_file" | tr -d ' ')"
  fi

  local head; head="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  local stamp; stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  local raw="$RUNS_DIR/$slug-$stamp.json"
  mkdir -p "$RUNS_DIR"

  local -a args=(
    "$question"
    --frames "$FRAMES"
    --ideas "$IDEAS"
    --top "$TOP"
    --json
  )
  [[ -n "$context_file" ]]  && args+=(--context "$context_file")
  [[ -n "$CRITIC_MODEL" ]]  && args+=(--critic-model "$CRITIC_MODEL")

  echo "slug     : $slug"
  echo "question : $question"
  echo "commit   : $head"
  echo "record   : $record"
  echo "raw run  : $raw"
  [[ -n "$CRITIC_MODEL" ]] && echo "critic   : $CRITIC_MODEL (decorrelated from the generator)"
  echo
  print_estimate "$context_bytes"
  echo

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY RUN — would execute:"
    printf '  adhd'; printf ' %q' "${args[@]}"; printf '\n'
    echo "  then: python scripts/adhd_render.py render --slug $slug --out $record"
    return 0
  fi

  # No pipe into the renderer: a pipeline's exit status is the last command's,
  # so `adhd … | python render` reports success whenever the *renderer* worked,
  # including on the empty stdout a failed run leaves behind. That is the exact
  # shape that put two failing-test commits in this repo's history. The run
  # lands in a file, the file is checked, then it is rendered.
  echo "running — this takes a minute or two, and stderr below is progress."
  if ! adhd "${args[@]}" > "$raw"; then
    die "the adhd run failed. Raw output (possibly empty) is at $raw"
  fi
  [[ -s "$raw" ]] || die "the adhd run wrote no JSON to $raw. Nothing to render."

  python3 "$RENDER" render \
    --slug "$slug" \
    --question "$question" \
    --command "adhd --frames $FRAMES --ideas $IDEAS --top $TOP" \
    --commit "$head" \
    --out "$record" < "$raw" >/dev/null

  echo
  echo "Decision record written to ${record#"$REPO_ROOT"/}"
  echo
  echo "Every idea is marked 'unreviewed'. Rule on each one — kept / rejected /"
  echo "parked, with a reason — then:"
  echo "  scripts/adhd-widen.sh check $slug"
}

cmd_check() {
  [[ $# -ge 1 ]] || die "usage: check <slug>"
  require_slug "$1"
  local record; record="$(record_path "$1")"
  [[ -f "$record" ]] || die "no decision record at $record"
  python3 "$RENDER" check "$record"
}

cmd_list() {
  local path slug found=0
  if compgen -G "$DECISIONS_DIR/*.md" >/dev/null; then
    for path in "$DECISIONS_DIR"/*.md; do
      slug="$(basename "$path" .md)"
      # README.md is the lane's own documentation, not a decision.
      [[ "$slug" == "README" ]] && continue
      found=1
    done
  fi
  if [[ "$found" == "0" ]]; then
    echo "no decision records yet."
    return 0
  fi
  for path in "$DECISIONS_DIR"/*.md; do
    slug="$(basename "$path" .md)"
    [[ "$slug" == "README" ]] && continue
    printf '%-28s ' "$slug"
    if python3 "$RENDER" check "$path" >/dev/null 2>&1; then
      python3 "$RENDER" check "$path" 2>/dev/null | tail -1
    else
      python3 "$RENDER" check "$path" 2>/dev/null | grep -E '^[0-9]+ ideas' || echo "unreadable"
    fi
  done
}

cmd_estimate() {
  print_estimate "${1:-0}"
}

usage() {
  sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

[[ $# -ge 1 ]] || usage
command="$1"; shift
case "$command" in
  open)     cmd_open     "$@" ;;
  check)    cmd_check    "$@" ;;
  list)     cmd_list     "$@" ;;
  estimate) cmd_estimate "$@" ;;
  *)        usage ;;
esac
