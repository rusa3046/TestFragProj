# Operating rules — fragrance-graph

## Single writer

**Claude is the only agent that writes to this working tree.** Not "the
primary" writer, not "usually" — the only one. Every commit on every branch
in this repository is authored through this session.

That invariant is what makes the git history readable: when something breaks,
`git log` is a record of decisions someone can reconstruct, not a merge of
two agents' half-understandings. It is cheap to keep and expensive to
recover once lost.

Codex is a reviewer. It reads a frozen commit in a sibling directory and
reports. It does not write here, ever.

## Codex is advisory by default

A Codex finding is an argument, not an instruction. It runs against one
commit with no access to this conversation, the corpus decisions, or the
reasoning in the docstrings — which is exactly what makes it useful, and
exactly why it is confidently wrong a good fraction of the time.

Before acting on any finding: open the cited file, read the cited function,
check `tests/` for an existing guard. Classify it — confirmed / partially
correct / false positive / already handled — and report how many were
rejected. **A checkpoint where nothing was rejected means the files were not
opened.**

Nothing Codex reports changes this tree without a person deciding it should.

## Phase checkpoints go through the script, not the plugin

Use `scripts/codex-agent.sh`. The plugin runs `codex` against whatever
directory this session was launched in, which is this repository, which
breaks single-writer on the first invocation.

```
scripts/codex-agent.sh review   <phase> [focus]   # read-only, frozen commit
scripts/codex-agent.sh redteam  <phase>           # read-only, adversarial
scripts/codex-agent.sh delegate <phase> <file>    # write lane, in a clone
scripts/codex-agent.sh status
scripts/codex-agent.sh close    <phase> [--force]
```

Wrapped by `/codex-checkpoint` and `/codex-delegate`. The script enforces
what a prompt cannot: `-C` always points at a sibling directory and aborts if
it resolves to this repo, read lanes pass `--sandbox read-only`, the write
lane uses a clone rather than a worktree, and both lanes refuse to start on a
dirty tree.

## Two plugin commands to never run unasked

- **`/codex:rescue`** writes to *this* directory. It is the single fastest
  way to lose the invariant above.
- **`/codex:transfer`** ships this session's transcript to OpenAI. The
  transcript contains the corpus decisions, the spend ledger reasoning, and
  whatever the user has said in it.

Ask before either. Never as a step inside a larger plan the user approved in
general terms — approval for the plan is not approval for these.

The plugin's stop-time review gate stays **off**. It installs a `Stop` hook
that fires on every turn.

## Commit through the checkpoint script

Use `scripts/checkpoint.sh -m "..."`. It runs ruff, the full suite, the
provenance audit and the recommendation benchmark as separate checked
steps, and refuses to commit if any fails.

This exists because a commit landed with failing tests twice, both from
the same shape:

    uv run pytest -q 2>&1 | tail -3 && git commit ...

A pipeline's exit status is the last command's, so that reads as "if
`tail` succeeded, commit". Knowing this does not prevent it; the mistake
happens while assembling a long command for another purpose. The script
does the remembering.

`--quick` skips the audit and benchmark for work that cannot affect them.

## A rebuilt database needs two more commands

`corpus import` restores everything the corpus holds. Two tables are
computed from it rather than stored in it, and neither survives:

```
uv run python -m fragrance_graph.attributes infer      # claim_attributions
uv run python -m fragrance_graph.semantic backfill     # evidence_embeddings
```

Both are free and take seconds. The import prints this itself when either
is empty **or stale**, and stale is the case worth knowing about: claims
have no natural key, so an import deletes and re-inserts them and every id
changes. `claim_attributions` cascades and goes visibly empty;
`evidence_embeddings` has no foreign key, so every row survives pointing at
a claim that no longer exists. Counting rows says that table is fine.

Skipping them costs 22/22 on the recommendation benchmark, which
`checkpoint.sh` gates commits on — so a fresh clone cannot commit, for a
reason unrelated to whatever it changed.

## Never merge Codex work

Harvest with `git fetch <clone-path> <branch>`, then read each diff and
cherry-pick, or reimplement. Never `git merge` — it takes the whole branch
and its history in one act that is awkward to unpick, and it puts commits in
this tree that nobody read.

A returned commit is a patch from a stranger with no context on this
codebase. Read the whole diff, confirm any new test fails against the old
code, run `uv run ruff check . && uv run pytest -q` yourself rather than
trusting a reported result, and reject anything touching `data/corpus/` —
that is the source of truth and it is not regenerable.
