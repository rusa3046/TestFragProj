#!/usr/bin/env bash
#
# codex-agent.sh — run Codex against this repo without letting it near this
# working tree.
#
#   review   <phase> [focus]      read-only review of a frozen commit
#   redteam  <phase>              read-only adversarial pass
#   delegate <phase> <task-file>  write lane, in a clone, harvested by fetch
#   status                        where every lane currently stands
#   close    <phase> [--force]    retire a lane and archive its findings
#
# ---------------------------------------------------------------------------
# Why this exists rather than the plugin
#
# The Codex plugin runs `codex` against the directory Claude was launched in,
# and `/codex:rescue` writes there. Both break the single-writer invariant:
# exactly one agent may modify this working tree, and that agent is Claude.
# Everything structured goes through this script instead, which never lets
# Codex see this directory at all.
#
# Five properties carry that guarantee. Each is enforced here, not requested
# in a prompt, because a prompt is a suggestion and a flag is not:
#
#   1. Every `codex` invocation passes `-C` at a *sibling* directory. A guard
#      resolves both paths with `cd … && pwd -P` and aborts if they are the
#      same, or if the target sits underneath this repo.
#   2. review and redteam pass `--sandbox read-only`. That is OS-enforced.
#   3. delegate uses a git **clone**, never a worktree. A worktree keeps its
#      objects in the primary `.git`, so `--sandbox workspace-write` cannot
#      commit; the usual workaround is `--add-dir` on the primary `.git`,
#      which would hand Codex write access to this repo's refs and objects.
#      A clone has its own object store and needs no exception.
#   4. Both lanes refuse to start unless `git status --porcelain` is empty,
#      so the commit under review is immutable and nothing is in flight.
#   5. review re-pins its worktree to current HEAD on every run and writes
#      findings to `.codex-findings/` via `-o`.
#
# `.codex-findings/` is in `.gitignore` on purpose. `git status --porcelain`
# lists untracked files but not ignored ones, so without that entry the first
# review would dirty the tree and the second would refuse to run under (4).
#
set -euo pipefail

# --- where things live ------------------------------------------------------

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$REPO_ROOT" ]]; then
  echo "codex-agent: not inside a git repository." >&2
  exit 2
fi
REPO_ROOT="$(cd "$REPO_ROOT" && pwd -P)"
REPO_NAME="$(basename "$REPO_ROOT")"

REVIEW_DIR="$(dirname "$REPO_ROOT")/${REPO_NAME}-codex-review"
WORK_DIR="$(dirname "$REPO_ROOT")/${REPO_NAME}-codex-work"
FINDINGS_DIR="$REPO_ROOT/.codex-findings"
PROMPT_DIR="$REPO_ROOT/docs/codex"

# Set to 1 to print the exact codex command line instead of running it. The
# isolation guarantee is a property of that command line, so this is how the
# guarantee gets tested without spending a request or needing credentials.
DRY_RUN="${CODEX_AGENT_DRY_RUN:-0}"

die() { echo "codex-agent: $*" >&2; exit 1; }

# --- guards -----------------------------------------------------------------

# The whole point of the script. Resolves symlinks on both sides — a sibling
# that is a symlink back to the repo would otherwise pass a string compare.
#
# **Call this before touching the path in any way.** The first version only
# called it just before `codex`, by which point `pin_review_worktree` had
# already run `git checkout --detach`, `reset --hard` and `clean -fd`
# against it. Pointing the sibling at the repo by symlink therefore detached
# HEAD in the real repository — the exact outcome the guard exists to
# prevent, reached through the guard's own script. A path that has not been
# checked is not a path you may run git against either.
#
# A non-existent target is fine and returns cleanly: the lane creates it,
# and the caller checks again once it exists.
assert_outside_repo() {
  local target="$1" resolved
  if [[ ! -e "$target" ]]; then
    resolved="$(cd "$(dirname "$target")" && pwd -P)/$(basename "$target")"
  else
    [[ -d "$target" ]] || die "REFUSING: $target exists and is not a directory."
    resolved="$(cd "$target" && pwd -P)"
  fi
  if [[ "$resolved" == "$REPO_ROOT" ]]; then
    die "REFUSING: codex workdir resolved to this repo ($resolved).
     Single-writer is broken if Codex runs here. Nothing was executed."
  fi
  case "$resolved/" in
    "$REPO_ROOT"/*)
      die "REFUSING: codex workdir $resolved is inside this repo.
     A sandbox rooted under the working tree is not isolation."
      ;;
  esac
}

require_clean_tree() {
  local dirty
  dirty="$(git -C "$REPO_ROOT" status --porcelain)"
  if [[ -n "$dirty" ]]; then
    echo "codex-agent: working tree is not clean; refusing to start." >&2
    echo "  Codex reviews a commit, not a moving target." >&2
    echo "$dirty" | sed 's/^/    /' >&2
    exit 1
  fi
}

require_codex() {
  command -v codex >/dev/null 2>&1 || die "codex CLI not found. npm install -g @openai/codex"
  if [[ "$DRY_RUN" == "1" ]]; then return 0; fi
  if ! codex login status >/dev/null 2>&1; then
    die "codex is installed but not authenticated. Run: codex login
     (or 'codex login --device-auth' if a browser cannot open here)."
  fi
}

require_prompt() {
  [[ -f "$1" ]] || die "missing prompt file: $1"
}

# --- lane setup -------------------------------------------------------------

# Read lane: a detached worktree, re-pinned to HEAD on every run so a review
# always describes the commit you are actually on.
pin_review_worktree() {
  local head="$1"
  assert_outside_repo "$REVIEW_DIR"
  if [[ ! -d "$REVIEW_DIR" ]]; then
    git -C "$REPO_ROOT" worktree add --detach --quiet "$REVIEW_DIR" "$head"
  else
    git -C "$REVIEW_DIR" checkout --detach --quiet "$head"
    git -C "$REVIEW_DIR" reset --hard --quiet "$head"
    git -C "$REVIEW_DIR" clean -fdq
  fi
}

# Write lane: a real clone. --no-hardlinks so the clone's object store is
# genuinely its own on disk and nothing Codex does can touch ours.
prepare_work_clone() {
  local branch="$1" head="$2"
  assert_outside_repo "$WORK_DIR"
  if [[ ! -d "$WORK_DIR/.git" ]]; then
    git clone --no-hardlinks --quiet "$REPO_ROOT" "$WORK_DIR"
  else
    git -C "$WORK_DIR" fetch --quiet origin
  fi
  git -C "$WORK_DIR" checkout --quiet -B "$branch" "$head"
}

timestamp() { date -u +%Y%m%dT%H%M%SZ; }

findings_path() {
  mkdir -p "$FINDINGS_DIR"
  echo "$FINDINGS_DIR/$1-$2-$(timestamp).md"
}

# --- running codex ----------------------------------------------------------

# One place where `codex` is spelled, so there is one place to audit. The
# prompt always arrives on stdin: it is long, it contains backticks and
# quotes, and passing it as argv is how a shell mangles a checklist.
run_codex() {
  local workdir="$1" sandbox="$2" outfile="$3"
  assert_outside_repo "$workdir"

  if [[ "$DRY_RUN" == "1" ]]; then
    echo "DRY RUN — would execute:"
    echo "  codex exec -C $workdir --sandbox $sandbox -o $outfile -"
    echo "  repo root : $REPO_ROOT"
    echo "  workdir   : $(cd "$workdir" && pwd -P)"
    echo "  isolated  : yes (workdir != repo root, and not beneath it)"
    cat >/dev/null   # drain the prompt so the caller's pipe does not SIGPIPE
    return 0
  fi

  codex exec -C "$workdir" --sandbox "$sandbox" -o "$outfile" -
}

# --- subcommands ------------------------------------------------------------

cmd_read_lane() {
  local kind="$1" phase="$2" focus="${3:-}"
  local prompt_file="$PROMPT_DIR/${kind}-prompt.md"
  require_prompt "$prompt_file"
  require_codex
  require_clean_tree

  local head out
  head="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  pin_review_worktree "$head"
  out="$(findings_path "$phase" "$kind")"

  echo "phase    : $phase"
  echo "commit   : $head"
  echo "lane     : $kind (read-only)"
  echo "workdir  : $REVIEW_DIR"
  echo "findings : $out"
  echo

  {
    cat "$prompt_file"
    echo
    echo "## This run"
    echo
    echo "- Phase under review: **$phase**"
    echo "- Frozen commit: \`$head\`"
    echo "- Repository: \`$REPO_NAME\` (you are reading a detached worktree pinned to that commit)"
    if [[ -n "$focus" ]]; then
      echo
      echo "### Focus for this run"
      echo
      echo "$focus"
    fi
  } | run_codex "$REVIEW_DIR" "read-only" "$out"

  echo
  echo "Findings written to $out"
  echo "Next: /codex-checkpoint $phase — every cited file gets opened before anything is accepted."
}

cmd_review()  { [[ $# -ge 1 ]] || die "usage: review <phase> [focus]";  cmd_read_lane review  "$1" "${2:-}"; }
cmd_redteam() { [[ $# -ge 1 ]] || die "usage: redteam <phase>";         cmd_read_lane redteam "$1" "${2:-}"; }

cmd_delegate() {
  [[ $# -ge 2 ]] || die "usage: delegate <phase> <task-file>"
  local phase="$1" task_file="$2"
  [[ -f "$task_file" ]] || die "task file not found: $task_file"
  require_codex
  require_clean_tree

  local head branch out
  head="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  branch="codex/$phase"
  prepare_work_clone "$branch" "$head"
  out="$(findings_path "$phase" "delegate")"

  echo "phase    : $phase"
  echo "commit   : $head"
  echo "lane     : delegate (workspace-write, in a CLONE)"
  echo "workdir  : $WORK_DIR"
  echo "branch   : $branch"
  echo "findings : $out"
  echo

  {
    cat "$task_file"
    echo
    echo "## Ground rules for this task"
    echo
    echo "- You are in a **clone** at \`$WORK_DIR\`, branched \`$branch\` from \`$head\`."
    echo "- Commit your work on that branch. Do not push; there is nowhere to push to."
    echo "- Do not touch \`data/corpus/\` — it is the source of truth and is not regenerable."
    echo "- Run \`uv run ruff check .\` and \`uv run pytest -q\` before you finish, and report the result honestly."
  } | run_codex "$WORK_DIR" "workspace-write" "$out"

  echo
  echo "Harvest — fetch, never merge:"
  echo "  git fetch \"$WORK_DIR\" $branch"
  echo "  git log --oneline HEAD..FETCH_HEAD"
  echo "  git cherry-pick <sha>       # one commit at a time, after reading it"
}

cmd_status() {
  echo "repo root  : $REPO_ROOT"
  echo "HEAD       : $(git -C "$REPO_ROOT" rev-parse HEAD)"
  echo "branch     : $(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)"
  local dirty
  dirty="$(git -C "$REPO_ROOT" status --porcelain)"
  echo "tree       : $([[ -z "$dirty" ]] && echo clean || echo "DIRTY — both lanes will refuse")"
  echo
  echo "codex      : $(command -v codex >/dev/null 2>&1 && codex --version 2>&1 || echo 'not installed')"
  echo "auth       : $(codex login status 2>&1 | head -1 || true)"
  echo
  echo "review lane:"
  if [[ -d "$REVIEW_DIR" ]]; then
    echo "  dir      : $REVIEW_DIR"
    echo "  pinned to: $(git -C "$REVIEW_DIR" rev-parse HEAD 2>/dev/null || echo '?')"
  else
    echo "  (not created yet)"
  fi
  echo
  echo "work lane:"
  if [[ -d "$WORK_DIR/.git" ]]; then
    echo "  dir      : $WORK_DIR"
    echo "  branch   : $(git -C "$WORK_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
    local unharvested
    unharvested="$(git -C "$WORK_DIR" log --oneline "$(git -C "$REPO_ROOT" rev-parse HEAD)"..HEAD 2>/dev/null | wc -l | tr -d ' ')"
    echo "  unharvested commits: $unharvested"
  else
    echo "  (not created yet)"
  fi
  echo
  echo "worktrees:"
  git -C "$REPO_ROOT" worktree list | sed 's/^/  /'
  echo
  echo "findings:"
  if compgen -G "$FINDINGS_DIR/*.md" >/dev/null; then
    ls -1t "$FINDINGS_DIR"/*.md | sed 's|.*/|  |'
  else
    echo "  (none)"
  fi
}

cmd_close() {
  [[ $# -ge 1 ]] || die "usage: close <phase> [--force]"
  local phase="$1" force="${2:-}"

  if [[ -d "$WORK_DIR/.git" ]]; then
    local unharvested
    unharvested="$(git -C "$WORK_DIR" log --oneline "$(git -C "$REPO_ROOT" rev-parse HEAD)"..HEAD 2>/dev/null | wc -l | tr -d ' ')"
    if [[ "$unharvested" != "0" && "$force" != "--force" ]]; then
      echo "codex-agent: $unharvested commit(s) in the work clone are not in this repo." >&2
      git -C "$WORK_DIR" log --oneline "$(git -C "$REPO_ROOT" rev-parse HEAD)"..HEAD | sed 's/^/    /' >&2
      die "Harvest them, or pass --force to discard."
    fi
  fi

  local archive="$FINDINGS_DIR/closed/$phase"
  if compgen -G "$FINDINGS_DIR/$phase-*.md" >/dev/null; then
    mkdir -p "$archive"
    mv "$FINDINGS_DIR/$phase-"*.md "$archive/"
    echo "archived findings -> $archive"
  fi

  if [[ -d "$REVIEW_DIR" ]]; then
    git -C "$REPO_ROOT" worktree remove --force "$REVIEW_DIR"
    echo "removed review worktree $REVIEW_DIR"
  fi
  if [[ -d "$WORK_DIR" ]]; then
    rm -rf "$WORK_DIR"
    echo "removed work clone $WORK_DIR"
  fi
  git -C "$REPO_ROOT" worktree prune
  echo "phase $phase closed."
}

# --- dispatch ---------------------------------------------------------------

usage() {
  sed -n '3,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

[[ $# -ge 1 ]] || usage
command="$1"; shift
case "$command" in
  review)   cmd_review   "$@" ;;
  redteam)  cmd_redteam  "$@" ;;
  delegate) cmd_delegate "$@" ;;
  status)   cmd_status   "$@" ;;
  close)    cmd_close    "$@" ;;
  *)        usage ;;
esac
