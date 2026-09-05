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

## A rebuilt database needs five more commands

`corpus import` restores everything the corpus holds, and nothing else.
Two tables are computed from it rather than stored in it, and neither
survives:

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

Three more tables are *curated input* under `data/curation/`, which
`corpus import` never reads:

```
uv run python -m fragrance_graph.houses import         # houses
uv run python -m fragrance_graph.retail import         # retailer_listings + declared notes
uv run python -m fragrance_graph.notes import          # brand-declared notes
```

**The retail import is the one that fails quietly.** Catalog-first
candidate generation reads `retailer_listings` and the declared-note map
directly, so a database without them still answers every query — from
community evidence alone, which is precisely the failure catalog-first
generation exists to prevent. Nothing errors. The answers just get small
again.

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

## Where things stand, and what the environment will do to you

Written 2026-08-27 so a session starting cold does not have to re-derive
any of it. The product decisions live in [docs/FACET.md](docs/FACET.md);
this is the operational half.

### The repository was renamed

`TestFragProj` → **`claim-graph`**, 2026-08-27. GitHub redirects the old
URL permanently, so old clones and links keep working. What does *not*
follow the rename is this environment's credential, which is scoped to
the **old** name on both surfaces: the GitHub MCP tools answer
`owner="rusa3046", repo="TestFragProj"` and return `403 GitHub access to
this repository is not enabled for this session` for `claim-graph`, and
`git push` to the new URL is refused with "not in this session's
authorized repository set" while the same push to
`https://github.com/rusa3046/TestFragProj` succeeds through the
redirect. So: keep `origin` on the old URL, open and merge PRs with the
old name, and expect a stop-hook "unpushed commits" warning after a push
by URL until `git fetch` refreshes the tracking ref. `add_repo` with
`access: "push"` for `claim-graph` needs the operator to approve it and
has not been.

### What the five gates are for

`scripts/checkpoint.sh` runs ruff, the suite, the provenance audit, the
recommendation benchmark, and — added 2026-08-27 — the **card golden**.
The first four check rules. The fifth checks the artifact, and it exists
because six defects reached a person's screen in one week with all
1,815 tests passing: an inverted note match, a catalog sentence spliced
into a community frame, a chip counted twice, a declared note scored at a
quarter of a comment, a performance chip compiled as a note, and a
headcount line reading "0 people across 0 channels".

Every one of them lived in the *assembled card*, which no unit test
looked at. If you change anything a customer reads, expect
`data/eval/cards.golden.txt` to move; read the diff, decide whether it is
an improvement, then `uv run python -m fragrance_graph.evals.cards
--update` and commit the golden **with** the change that caused it.

A golden that renders its own copy of a card would drift from the product
and pass forever, so `evals/cards.py` goes through `api._session_response`
— the same function the kiosk's handlers return. Keep it that way.

### The asymmetry that explains most "bad recommendation" reports

Notes, families and occasions are answerable from the **catalogue**;
longevity, projection and vibes exist **only** in community evidence.
So a request weighted toward performance will always favour well-discussed
bottles, however good the catalog data is — 47 of 548 bottles carry
longevity evidence. That is a data-coverage fact, not a ranking bug, and
it is why `catalogue_seeds` now aims collection at popular, note-carrying
bottles the corpus cannot speak about. Check coverage before assuming the
ranker is wrong.

### The full gate takes ~12 minutes

Longer than the 10-minute shell timeout, so run it detached with a log
and poll the log — do not chain sleeps. Two more traps that have each
cost a session: `pkill` in the same command as anything else kills the
tool's own process group (exit 144), and two `pytest` runs at once
clobber the shared test database and produce failures that are not real.
One suite run at a time.

Real-corpus tests skip silently unless `FRAGRANCE_DB_URL` points at a
populated developer database. A "1801 passed, 14 skipped" is not the same
run as "1815 passed" — check which you got before trusting it.

### Open, in rough priority order

1. **Deploy.** `Dockerfile`/`fly.toml` are written and the bake sequence
   is verified natively, but no daemon existed in that session so the
   image has never been built. First build is the risk.
2. **The eval.** 86 labelled comments, and the published F1 predates the
   corpus doubling. Target 200-500 stratified. A model must never write
   its own answer key — see the blind-check discipline in the README.
3. **First-party feedback** as a fourth provenance voice, silent until
   volume justifies speech, never blended into YouTube counts.
4. **Nordstrom review text.** 2,192 reviews sit on 352 comment-less
   bottles in the raw collection and are *deliberately* not imported:
   shopper prose carries no republication licence. A claims-only
   extraction (typed claims, no stored sentence, no quote on the card) is
   the version that would be defensible, and it is a decision for the
   owner, not a task to pick up.

## Documentation moves with the code

Standing instruction from the owner (2026-08-27): **every commit leaves
README.md, docs/FACET.md and the rest of docs/ describing the current
state of the project.** A behaviour change that would surprise a reader
of those files is not done until the files say otherwise — update them in
the same commit as the change, not in a sweep afterwards. The docs-drift
this rule ended: a README still describing an eight-page dupes site three
product generations after FACET shipped.
