---
description: Run a Codex review at a phase boundary and verify every finding against the source
argument-hint: '<phase> [focus]'
allowed-tools: Bash(scripts/codex-agent.sh:*), Read, Grep, Glob
---

Run an independent Codex review of the current commit, then **verify it**.

## 1. Run it

```bash
scripts/codex-agent.sh review $ARGUMENTS
```

If the script refuses because the tree is dirty, stop and tell the user what
is uncommitted. Do not commit on their behalf to get past it — the point of
the check is that they know what is in flight.

If they asked for the adversarial pass instead, use `redteam` with the same
phase and the same verification below.

## 2. Verify every finding — this is the actual job

You are not a relay. Codex ran against a frozen commit with no ability to
execute anything, and it has no access to this conversation, the corpus, or
the reasoning behind any design decision. It will be confidently wrong.

For **each** finding, before you write a single word about it:

1. **Open the cited file and read the cited function.** Use Read, not
   memory, not this conversation's history. A finding whose citation does
   not resolve is a false positive by default — say so.
2. **Check for an existing test.** Grep `tests/` for the behaviour. Much of
   what looks like a bug here is pinned deliberately and explained in a
   docstring; the docstrings carry the reasoning and are worth reading
   before contradicting them.
3. **Classify it, in these exact four words:**
   - **confirmed** — you reproduced the reasoning in the source and it is
     wrong. Give the file, the line, and what breaks.
   - **partially correct** — the mechanism is real but the consequence,
     severity, or trigger is overstated. Say which half survives.
   - **false positive** — the code does not do what the finding says. Quote
     the line that disproves it.
   - **already handled** — real, and there is a guard or test. Name it.
4. **Never accept a `HYPOTHESIS` as confirmed** without verifying it
   yourself. That label means Codex could not check it either.

## 3. Report

Lead with the count:

> N findings: X confirmed, Y partially correct, Z false positives, W already
> handled.

**If you rejected nothing, you did not check.** A review where every finding
survives means you read the findings file and not the code. Go back and open
the files.

Then, ordered by how close each one comes to the failure that actually
matters — *a page claiming more independent people than really exist*:

- one line naming the finding and its classification;
- the file and function you opened to decide;
- for confirmed findings, the smallest reproduction, in this system's terms
  (a comment, a claim row, a pair, a page) rather than in the abstract.

End with the disagreements: anything Codex called a bug that you are
overruling, and why. Those matter more than the agreements — they are the
part the user cannot get from the findings file alone.

## 4. Do not fix anything

This command reviews. It does not edit. Propose the fixes as a numbered
list, smallest first, and stop there. The user decides what gets built and
in which order.
