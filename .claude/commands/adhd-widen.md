---
description: Widen an open decision with ADHD, then rule on every idea it surfaces
argument-hint: '<slug> "<question>" [context-file …]'
allowed-tools: Bash(scripts/adhd-widen.sh:*), Read, Grep, Glob, Edit
---

Widen a decision before committing to the first plausible answer, then **rule
on what comes back**. This is the mirror of `/codex-checkpoint`: that lane
sends an outside model to narrow, this one sends it to widen.

## 0. Check it is the right tool first

Before running anything, decide whether the question is ADHD-shaped. Use it
for open design decisions, naming, API surface, migration planning, "what
could go wrong here beyond the checklist", and anything of the form *give me
a few ways to…*. Do **not** use it for a bug with a known root cause, a
lookup, the deploy, or anything with one correct answer. If the question is
not ADHD-shaped, say so and answer it directly — running the lane anyway
spends real money to produce thirty variations on the obvious.

## 1. Run it

```bash
scripts/adhd-widen.sh open $ARGUMENTS
```

Pass context files only when they change the answer. The context is re-sent
to **every** branch — five frames means five copies — so a whole module
passed "for background" multiplies straight into the bill. One file that
carries the real constraint beats four that carry atmosphere.

The script prints the multiplier before it spends. If the estimate looks
wrong for the question, stop and say so rather than running it.

## 2. Rule on every idea — this is the actual job

The record lands with every idea marked `unreviewed`, and `check` fails while
any row still reads that. You are not a relay. ADHD ran with no access to this
codebase's constraints, the corpus decisions, the provenance tiers, or the
reasoning in the docstrings — which is what makes it useful and what makes it
confidently wrong.

The failure mode here is the opposite of Codex's and worse. Codex hands back
claims that something is *wrong*; a false one costs ten minutes. ADHD hands
back claims that something is *possible*; a plausible one costs nothing to
read and a week to unbuild.

For **each** row, before writing a verdict:

1. **Open the code it would touch.** Read, not memory. An idea that would
   land in `recommend.py`, `commerce_card.py` or anything under `facet/` has
   to be weighed against the tier-gating and determinism rules those files
   are built on, not against how good the idea sounds.
2. **Check `tests/` and the docstrings.** A surprising amount of what ADHD
   proposes is already here, deliberately not here, or pinned with a comment
   explaining why. Name the test or the docstring in the note.
3. **Write one of three verdicts, with a reason:**
   - `kept` — going to be built, or already is.
   - `rejected` — you read it and decided against it. The note says why.
   - `parked` — real, not now. The note names the condition or the date, not
     a shrug.
4. **Treat the trap list as the most argued-over rows, in both directions.**
   The critic flags traps with mechanical reasons but has no idea what this
   project's constraints are — it will call something a trap that this
   codebase already solved, and it will miss the one that would break the
   provenance tiers. Disagree with it explicitly where you do.

Argue at the cluster level first where you can. If a whole angle is wrong for
this project, that is one decision with one reason, not six.

## 3. Gate it

```bash
scripts/adhd-widen.sh check <slug>
```

It fails while anything is `unreviewed`. It warns — and does not fail — when
**nothing was rejected**, and that warning should almost always be treated as
a finding about the review rather than about the run: divergence is instructed
to generate without evaluating, so a run emits its full quota of ideas whether
or not the space holds that many good ones. If every idea survived, go back
and open the files.

## 4. Report

Lead with the count:

> N ideas across F frames: X kept, Y rejected, Z parked.

Then, ordered by what they would actually change:

- the kept ideas, each with the file it would touch and the first step;
- the non-obvious pick and whether you kept it — it is the highest-*novelty*
  viable idea, not the best one, and rejecting it is a perfectly good outcome
  as long as the note says why;
- the disagreements with the critic: traps you overruled, and ideas it ranked
  highly that are wrong for this codebase. Those matter most — they are the
  part the user cannot get from the record alone.

## 5. Write the Decision, then stop

Fill in the `## Decision` section in the record — what is being done, in a
paragraph. Then stop. This command widens and rules; it does not implement.
The user decides what gets built and in which order.

If the decision changes behaviour a reader of `README.md` or `docs/FACET.md`
would be surprised by, say so — that is a same-commit docs change under the
standing rule, not a follow-up.
