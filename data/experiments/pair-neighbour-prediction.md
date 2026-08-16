# Prospective prediction — neighbour acquisition for a *pair* gap

**Frozen 2026-08-16, before any paid call.** Immutable from the first
paid call onward. If something here proves badly chosen, the run is
abandoned and a new file written — this one is not edited.

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

Counted as, for every pair `(Delina, X)` over all X:

```
U = Σ  [ Δ distinct commenters(pair) + Δ distinct creators(pair) ]
```

subject to the gates already in the code — the claim must be `ASSERTED`,
`evidence_verified = 1`, and resolved at **both** ends, and a commenter or
creator already counted for that pair contributes nothing. `U / usd` is
the primary number.

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

```
$0.10 ceiling per arm, ~$0.20 expected
observed overshoot last run: $0.1228 against a $0.10 ceiling, so budget ~$0.25
ledger at time of freezing: $1.1616 of $1.50 -> $0.3384 remaining today
YouTube: 2 searches, 200 units of 10,000
```

It fits inside today's remainder, with little room. The runner checks the
budget for both arms before starting and refuses rather than stopping
half way, so the alternative is simply to run after the UTC rollover.

## Sign-off

Nothing executes until the repository owner approves this file. Results go
to `data/experiments/pair-neighbour-result.md`, citing this file's commit.
