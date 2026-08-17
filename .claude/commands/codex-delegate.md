---
description: Hand a self-contained task to Codex in an isolated clone, then review what comes back
argument-hint: '<phase> <task-file>'
allowed-tools: Bash(scripts/codex-agent.sh:*), Bash(git:*), Read, Grep, Glob
---

Give Codex a write task in a **clone**, then harvest it commit by commit.

This is the lane that can produce code. Treat everything it returns as a
patch from a stranger who has never seen this repository before, because
that is exactly what it is.

## 1. Check the task file is actually self-contained

Read `$2` before running anything. Codex gets the repo at one commit and
nothing else — no conversation, no corpus decisions, no history of what was
already tried. If the task file assumes any of that, say so and stop; a
vague task produces a plausible diff that has to be thrown away.

A good task file names the module, the expected behaviour, the tests that
must still pass, and what is explicitly out of scope.

## 2. Run it

```bash
scripts/codex-agent.sh delegate $ARGUMENTS
```

The script clones to a sibling directory and works on `codex/<phase>`. It
refuses to start on a dirty tree. It never touches this working tree.

## 3. Harvest — fetch, never merge

```bash
git fetch ../<repo>-codex-work codex/<phase>
git log --oneline HEAD..FETCH_HEAD
```

Then, for each commit, **read the diff before taking it**:

```bash
git show <sha>
git cherry-pick <sha>
```

Never `git merge`. A merge brings the whole branch and its history in one
irreversible act, and it makes Codex a co-author of this tree's shape. Cherry-pick
one commit at a time, or reimplement it yourself if the diff is close but
not right — reimplementing is often faster than arguing with it.

## 4. Verify before you keep any of it

For every commit you are considering:

- **Read the whole diff.** Not the commit message. The diff.
- **Check it did not touch `data/corpus/`.** That is the source of truth and
  it is not regenerable. Any change there is an automatic reject.
- **Check the tests it added actually fail against the old code.** A test
  that passes before and after pins nothing. Revert the source change,
  run the test, confirm it fails, restore.
- **Run the full suite and ruff yourself.** Do not trust a reported result:

```bash
uv run ruff check . && uv run pytest -q
```

- **Check the trust rules still hold** — the ones in `README.md` under
  *Trust rules*, particularly that ranking cannot see commercial tables and
  that no page can emit an image.

## 5. Report

Say how many commits came back, how many you kept, how many you rewrote,
and how many you dropped — with the reason for each drop. If you kept
everything unchanged, explain why that was right rather than treating it as
the normal outcome.

## 6. Close the lane when you are done

```bash
scripts/codex-agent.sh close <phase>
```

This refuses if the clone still has commits that never reached this repo.
Pass `--force` only when you have decided to discard them deliberately.
