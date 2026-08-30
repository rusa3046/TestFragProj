# Decision records

Each file here is one open question, the ideas an ADHD run surfaced against
it, and a verdict on every one of them written by a person. They are produced
by `scripts/adhd-widen.sh`.

    scripts/adhd-widen.sh open  <slug> "<question>" [context-file …]
    scripts/adhd-widen.sh check <slug>
    scripts/adhd-widen.sh list
    scripts/adhd-widen.sh estimate

## What this lane is for

`codex-agent.sh` sends a frozen commit to an outside model that **narrows** —
it reports what is wrong, and `/codex-checkpoint` makes a person open every
cited file before a finding is accepted. This is the mirror lane. ADHD spawns
N isolated reasoning processes under deliberately distorted cognitive frames
(a regulator, a hardware engineer, a speedrunner, an ant colony) with no
shared context between them, then runs a separate critic pass that scores,
clusters, flags traps with mechanical reasons, and deepens the survivors. It
**widens** — it reports what is possible.

Reach for it at the moment a single-pass answer would be competent and
forgettable: the stratification axes for the eval, how first-party feedback
should behave as a fourth provenance voice, what a licence-defensible
Nordstrom extraction looks like. Do not reach for it on the deploy, on a bug
with a known root cause, or on anything one search answers. The rule of thumb
from ADHD's own docs holds: if a junior would look it up, a single pass wins;
if a senior would say *let me think about this differently for a minute*,
that is the minute this replaces.

## The discipline, which is the same discipline

An ADHD idea is an argument, not an instruction — the same standing rule
`CLAUDE.md` applies to a Codex finding, and it matters more here, not less.
A Codex finding that is wrong costs the ten minutes it takes to open the file
and disagree. An ADHD idea that is wrong is *plausible*: it costs nothing to
read and a week to unbuild.

So a record is generated with every idea marked `unreviewed`, and
`adhd-widen.sh check <slug>` **fails** while any row still reads that. Rule on
each one:

| verdict | means |
| --- | --- |
| `kept` | going to be built, or already is |
| `rejected` | read it, decided against it, and the note says why |
| `parked` | real, not now — the note names the condition or the date |
| `unreviewed` | the generated state; the gate fails while any remain |

`parked` exists because forcing yes-or-no onto a good idea with no slot
produces a dishonest `rejected`, and a `rejected` row nobody believes is how
the whole record stops being read.

**A record where nothing was rejected was skimmed, not weighed.** The reason
is mechanical rather than moral: ADHD's divergence phase is *instructed to
generate without evaluating*, so a run emits its full quota of ideas whether
or not the design space holds that many good ones. Thirty survivors is not a
remarkable run. `check` warns about this and does not fail on it — making it
fatal would only teach the reader to reject one row to get past the gate. The
gate blocks on `unreviewed`, which cannot be faked without reading.

## What the run costs, and why no figure is written down

A default run is `N + K + 2` LLM calls — five isolated divergence branches,
one scoring pass, one clustering pass, three deepen passes. Call count is the
wrong unit. Each branch is a fresh isolated context by design, so **whatever
you pass as context is paid once per branch**: five branches means five copies
before a single novel token is generated. `adhd-widen.sh` prints that
multiplier before it spends anything, and it is why this lane uses the
standalone CLI rather than the in-session skill — a skill run inside Claude
Code re-loads `CLAUDE.md` and the whole tool context into every branch.

Nothing is written to `data/spend.jsonl`. That ledger is append-only and its
value is that it records money actually spent; the `adhd` CLI reports no cost,
so any figure written there would be an estimate that can never be settled
against a real one — a wrong number that, by the ledger's own rules, stays
written. **This is a known gap, recorded rather than papered over.** If the
CLI ever reports usage, `budget.record(cost, "adhd")` is the one-line fix.

The daily cap is not consulted either, and that is deliberate rather than an
oversight: `fragrance_graph.budget` guards things that run unattended, where
the failure is a cheap bug repeating on a schedule. This lane runs only
because a person typed a question.

## Two things this lane will not do

**It is not in the product path, and must not become so.** Nothing under
`scripts/` here is importable by `fragrance_graph`. The recommender is
deterministic and every card is tier-gated; the claim this whole project rests
on is that similarity is *asserted and counted, never generated*. An ideation
engine anywhere near `recommend.py` would break precisely the property FACET
sells. This lane writes markdown and nothing else.

**It does not require a clean tree,** unlike the Codex lanes. Codex reviews a
*commit*, and a moving target makes the finding meaningless. ADHD reasons
about a *question*; the tree is not its subject, and refusing on a dirty tree
would block the most valuable moment to widen — halfway into a change, having
just discovered the design is wrong. The commit the run happened at is
recorded in the record's header instead, so it can be placed in the history
later.

## Committing a record

A decision record is documentation, so the standing rule applies: if the
decision changes behaviour a reader of `README.md` or `docs/FACET.md` would be
surprised by, those files change in the same commit.

    scripts/adhd-widen.sh check <slug>     # must pass
    scripts/checkpoint.sh --quick -m "…"   # a record alone cannot move the gates

`--quick` is right for a record on its own — it touches no code, so it cannot
move the provenance audit, the benchmark or the card golden. The moment the
decision turns into an implementation, that is a full `checkpoint.sh`.
