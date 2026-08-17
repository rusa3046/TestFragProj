# Prospective prediction — neighbour acquisition for a *pair* gap

**Frozen 2026-08-16, before any paid call.** Immutable from the first
paid call onward. If something here proves badly chosen, the run is
abandoned and a new file written — this one is not edited.

> **Amendment, 2026-08-16 20:27 UTC, pre-run, by the repository owner.
> Nothing had been spent.**
>
> 1. The primary metric was uncapped `Δ commenters + Δ creators`. It is now
>    **capped progress toward the publishing gate**: for each pair, new
>    people and creators are credited only up to what that pair was missing
>    at the frozen pre-run snapshot. Uncapped independent evidence and raw
>    claims stay, as secondary graph-yield metrics. Rationale: an uncapped
>    sum rewards piling a fourth, fifth and sixth commenter onto a pair that
>    already cleared its bar, which is graph yield rather than progress on
>    the frozen query.
> 2. `~$0.25 worst case` was an estimate presented as a bound, and is
>    withdrawn. See **Cost**, rewritten below: no provable maximum exists
>    under the current guard, so the run waits for the UTC rollover.
>
> Neighbour selection, arms, falsification logic and the frozen query are
> unchanged.

> **Second amendment, 2026-08-16 20:45 UTC, pre-run, by the repository
> owner. Still nothing spent.**
>
> 1. **The two arms are scored independently against the identical frozen
>    baseline.** The first amendment made the cap *consumed*, so whichever
>    arm ran first could exhaust a pair's shortfall and leave the other
>    scoring zero on evidence it genuinely supplied — turning the A/B
>    comparison into a measurement of run order. Both arms are now credited
>    against the same frozen shortfall with no deduction.
> 2. **Sequential marginal yield is kept as a secondary measurement.** What
>    the second arm added *given* the corpus the first left behind is a real
>    question; it is just not the comparison.
> 3. **Each arm's ceiling is now enforced by the ledger.** See **Cost**.
>
> Neighbour selection, arms, falsification logic and the frozen query remain
> unchanged.

## What is being tested, and what is not

The previous experiment (`neighbour-prediction.md`, `neighbour-result.md`)
falsified neighbour acquisition for a **unary attribute** gap, and the
diagnostic said why:

```
272 comments bought from a neighbour
 30 claims naming the anchor  ->  30 comparisons TO it, 0 descriptions OF it
```

The neighbour's audience compares to the anchor rather than describing it,
because that is why they are there. That killed the attribute hypothesis
and raised a different one, which is the only thing tested here:

> **Comparison-oriented neighbour acquisition is an efficient way to
> acquire pairwise evidence about an anchor.**

Narrow on purpose. Nothing here generalises to unary attributes; that
question is settled and settled negatively. A win here says only that
buying a dupe's comment section is a good way to buy *edges*.

## The anchor and the exact blocked query

**Anchor:** `Parfums de Marly Delina`

**Query:** *"What is similar to Delina?"* — operationally, **does any Delina
pair clear the publishing gate**, giving Delina its first published
comparison page. `pages.qualifying_pairs` is the authority; there is no
judgement in the verdict.

## Why it is blocked — pairwise evidence, not a missing attribute

Every comparison Delina has is below the gate, and the shortfall is
overwhelmingly **creators**, not people:

```
partner                          people creators videos  edge kinds        claims held
Parfums de Marly Delina Exclusif      3        1      1  BETTER_THAN                29   flanker
Swiss Arabian Rose 01                 2        2      2  BETTER_THAN, DUPE_OF        5
Maison Alhambra Delilah               2        1      1  SIMILAR_TO                  4
Armaf Club de Nuit Imperiale          1        1      1  DUPE_OF                    18
Maison Francis Kurkdjian BR540        1        1      1  BETTER_THAN                45
Parfums de Marly Delina La Rosée      1        1      1  SIMILAR_TO                 11   flanker
```

Gate: 3 people across 2 creators (5 across 3 for a flanker pair). Nothing
reaches it. Four of the six rest on **one video**.

This is a pair gap in the strict sense the brief asks for: Delina's own
attribute evidence is not the blocker, and no amount of unary description
would publish a page. Only more independent people, on more channels,
connecting Delina to something.

## Direct acquisition target

`Parfums de Marly Delina` — its own review sections.

Already searched three ways, and this is stated because it is **not**
symmetric with the neighbour arm:

```
"delina fragrance review"            22 videos
"delina exclusif fragrance review"   36 videos
"parfums de marly delina dupe"        3 videos
```

The previous experiment predicted a searched anchor would be exhausted and
was wrong — the direct arm returned 5 new facts for $0.034 against a
prediction of "zero or one". No exhaustion is assumed here.

## Neighbour acquisition target

`Maison Alhambra Delilah` — never searched.

## The rule that chose it, stated before applying it

> Among the anchor's comparison partners, take those that are **(i) not
> flankers of the anchor** and **(ii) connected by a similarity-type edge
> (`SIMILAR_TO` or `DUPE_OF`)**. Of those, choose the partner with the
> **most distinct commenters** on that edge. Break ties by **fewest claims
> already held about the partner.**

Each clause earns its place. Flankers are excluded because their pairs are
held to a higher bar and would confound a gate-crossing result.
`BETTER_THAN` is excluded because it is a ranking, not a similarity
relation — the audience of a bottle merely ranked against the anchor has
no particular reason to compare. The tie-break is the sparse lever, which
is measured.

Applied, showing the working:

```
Delina Exclusif        excluded — flanker
Delina La Rosée        excluded — flanker
BR540                  excluded — BETTER_THAN only
Armaf CdN Imperiale    eligible, 1 commenter
Swiss Arabian Rose 01  eligible, 2 commenters, 5 claims held
Maison Alhambra Delilah eligible, 2 commenters, 4 claims held   <- fewest, selected
```

## Equal rules for both arms

```
max_creators             4
max_videos_per_creator   1
max_comments           400
max_usd               0.10
search               "<name> fragrance review"   (broad, never "dupe")
stop                 first ceiling hit, or gate crossed
order                direct first, then neighbour
```

Order again biases **against** the hypothesis: any evidence both arms
could find is credited to the direct arm, because the neighbour's gain is
measured from a snapshot taken after the direct arm has run.

## Primary metric

**New usable independent pair evidence about Delina, per dollar.**

Not raw extracted comparison claims. The BR540 run produced 30 extracted
edges and moved the product by nothing, and that gap is the thing this
metric exists to close.

Counted as **capped progress toward the gate**. At the frozen pre-run
snapshot, every Delina pair has a shortfall:

```
missing_people(p)   = max(0, threshold_people(p)   - people(p))
missing_creators(p) = max(0, threshold_creators(p) - creators(p))
```

with thresholds from `gate.py` — 3 people and 2 creators, or 5 and 3 for a
flanker pair, decided by `pages.is_flanker_pair`. Then for each arm:

```
G = Σ  [ min(Δ people(p),   remaining cap) + min(Δ creators(p), remaining cap) ]
```

and `G / usd` is the primary number.

The cap is fixed once, at the frozen pre-run snapshot, and is **consumed**:
whatever the direct arm credits is no longer available to the neighbour
arm. That deepens the order bias already declared — the direct arm runs
first and eats the cap first — and it is the conservative direction, since
the hypothesis predicts the neighbour wins.

Uncapped, an eleventh commenter on a pair that cleared its bar three
purchases ago would score the same as the one commenter that carries a
pair over it. That is graph yield, and graph yield has its own line below.

The provenance gates already in the code still apply and are not relaxed:
the claim must be `ASSERTED`, `evidence_verified = 1`, and resolved at
**both** ends, and a commenter or creator already counted for that pair
contributes nothing.

**Hard condition, both required:**

1. `U/$` for the neighbour arm strictly exceeds `U/$` for the direct arm.
2. At least one Delina pair gains an **independent creator**. Creators are
   the binding constraint — four of six partners sit on a single channel —
   and a run that adds only people to a one-creator pair has not moved the
   gate.

## Secondary metrics, recorded and reported, not decisive

```
raw extracted comparison claims naming Delina   <- the "30 extracted" number
total new pair edges (any bottle)
independent commenters and creators added, per edge
pair transitions: below-gate -> repeated -> gate-clearing
new partners made legitimately comparable to Delina
total graph yield: new stated facts about any bottle
"what is similar to Delina" answerable — i.e. a page exists
actual cost, comments read, stop reason, quota units
```

The first and the primary metric are reported side by side on purpose.
`30 extracted edges` and `30 useful independent edges` are different
numbers and the experiment is decided on the second.

## Falsification

The hypothesis fails if **any** of these holds:

- **The direct arm matches or beats the neighbour arm** on `U/$`. Then
  comparison-oriented neighbour acquisition is not the efficient
  instrument for pair gaps, and pair scheduling should target anchors
  directly.
- **The neighbour arm produces many raw comparisons but little usable pair
  evidence** — concretely, ≥10 raw Delina comparison claims and **zero**
  new independent creators on any Delina pair. This is the BR540 failure
  in its pair-shaped form: volume without independence, which the gate
  correctly refuses and which no scheduler should be built to maximise.
- **Both arms produce no new independent pair evidence at all.** Then
  Delina's comparison neighbourhood is not reachable by buying comments,
  and the right response is to stop rather than spend more.

A gate crossing is **not** required for success. Two people short of the
bar can be one purchase from it, and demanding a page from $0.20 would
make a true hypothesis look false.

## Scope of whatever this shows

- **n = 1 anchor, 1 neighbour, 2 arms.** It can kill the hypothesis
  cheaply or earn a larger test. It cannot establish a policy.
- The neighbour is **fresh** and the anchor is **searched**. A neighbour
  win is therefore confounded with novelty, and the honest reading of a
  win is *"buying an unsearched dupe's comments is an efficient source of
  edges"* — which is still a schedulable rule, since novelty is knowable
  before spending.
- Nothing here speaks to unary attribute gaps. That question is closed.

## Cost

**There is no provable worst case, and the earlier `~$0.25` was an
estimate dressed as one.** Working, from the code rather than from the
last run:

A single batch *is* bounded. `DEFAULT_MAX_TOKENS = 8000` caps output and
`call_model` raises rather than continuing if it is hit, so output costs at
most `8000 × $5/Mtok = $0.040`. Input is 20 comments; at YouTube's 10,000
character limit that is ~50,000 tokens, `$0.050`. **A batch cannot exceed
about $0.09.**

A *video* is not bounded, and that is the problem. `enrich_one` checks
`trial.usd >= ceiling.max_usd` **between videos, not between batches**, so
once a video starts, extraction runs to the end of its comments. At the
frozen `max_comments = 400` that is up to 20 batches — **up to $1.80 for a
single video** in the pathological case, and around $0.20 at the rate this
corpus actually extracts at.

So the per-arm `$0.10` ceiling bounds where a run *starts* a video, not
what it spends. Two arms cannot be shown to fit inside today's remaining
`$0.3384`.

What would happen is not an overspend — `budget.guard` re-reads the ledger
and raises, so the daily cap holds to within one batch. It is worse than
that for this purpose: the run would stop **mid-experiment**, and a
truncated arm B is not a result, it is a wasted $0.20 and a file that has
to be thrown away.

```
ledger at freezing: $1.1616 of $1.50  ->  $0.3384 remaining
UTC rollover:       ~3.5 hours away
```

**Therefore the run waits for the UTC rollover.** The alternative — raising
the cap or shrinking `max_comments` — would either weaken the cap or change
the frozen arms, and neither is worth three hours.

### The ceiling is now enforced, not just declared

Two mechanisms, added by the second amendment and neither of which changes
the arms:

**Admission control, via `Budget.reserve`.** Both arms' money is committed
to the ledger *before the first paid batch*, so the experiment cannot begin
unless it can finish and no concurrent process can take the headroom out
from under it. The hold is released — settled to zero — at the end rather
than settled to the real cost, because the real cost is already on the
ledger: `guard` records every batch as it happens. Settling the hold to the
actual spend would charge the run twice. Verified end to end: hold $0.20,
charge $0.11, release, ledger reads $0.11.

**A per-arm cap.** Each arm loads its own `Budget` whose cap is *today's
ledger plus that arm's ceiling*, read fresh at the arm's start.
`budgeted_extractor` charges through `guard`, which re-reads the ledger and
raises the moment that arm-scoped cap is crossed. So an arm now overshoots
by **at most one batch** — about $0.09, bounded by `DEFAULT_MAX_TOKENS` —
rather than by one whole video.

**A truncated arm is not a result.** If either arm stops because it hit its
own ceiling rather than running out of creators or comments, `verdict`
returns `INVALID` and scores neither arm. An arm cut short did not receive
the budget the design promised it, and comparing a full purchase against a
partial one measures the interruption rather than the hypothesis.

## Sign-off

Nothing executes until the repository owner approves this file. Results go
to `data/experiments/pair-neighbour-result.md`, citing this file's commit.
