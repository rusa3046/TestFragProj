# What to buy next, and what the measurements say

Recorded 2026-08-15. Every number here is measured against the committed
corpus unless it is marked **estimated** or **unmeasured**, and the
distinction is load-bearing: two of the five strategies have never
actually been run, and one of those is the one an earlier report
recommended.

## The constraint, stated precisely

```
78 fragrances          664 attribute facts        63 bottles with any fact
575 singleton (87%)     89 repeated               15 declarable, 6 bottles
452 distinct values     84 on two or more bottles
2 of 6 relative comparisons answerable
```

The system retrieves broadly and asserts narrowly, and the reason is not
fact volume. **A comparison needs two bottles to have said the same
thing**, and only 84 of 452 attribute values appear on more than one
bottle. 116 values sit on exactly one bottle and can never participate in
a comparison no matter how well the ranker works.

That reframes what enrichment has to achieve. Adding six hundred more
one-off descriptors would move `facts` by six hundred and
`comparable_attributes` by zero.

### Four "not answerable" results, three different causes

| question | blocker |
|---|---|
| Delina but less rose | 69 of 77 other bottles have no rose evidence; 0 have less |
| BR540 but less sweet | the anchor itself is 1 person on 1 creator |
| Angels' Share but less smoky | the anchor has no smoky evidence |
| Layton but less vanilla | **answerable** — 8 candidates with less |

Only the last is a ranking question. Catalogue sparsity, a thin anchor and
a missing attribute all present identically as "no answer" and each needs
different money spent on it.

## The five strategies

| | cost | measured? | what it buys | provenance risk | scales? |
|---|---|---|---|---|---|
| **A** targeted pair verification | $0.17/pair **est.** | ❌ never run | one pair edge | low | 22 pairs, then dry |
| **B** learn-about-fragrance | $0.17/bottle **est.** | ❌ blocked today | attribute density | low | whole catalogue |
| **C** canonical metadata | $0.05/lookup | ✅ 60 → 5 names, 0 pages | identity only | low | poor |
| **D** semantic retrieval | $0 | ✅ baseline wins | nothing | — | done |
| **E** new-release acquisition | $0 discovery | ✅ 2 found, 0 catalogued | catalogue growth | low | feed-limited |

### A — targeted pair verification

22 near-miss pairs exist. **17 of them are short on creators, not people** —
they need a different channel rather than another commenter, which is the
harder thing to buy with one search.

The $0.17 is `ESTIMATED_JOB_USD`, not an observed conversion. **Pair
verification has never been run.** The earlier recommendation to spend
here rested on the cost estimate and on 22 being a satisfying number, not
on a measured conversion rate.

Ceiling: 22 pairs. Even at a perfect conversion rate that is 22 pages and
no change to attribute density — so it cannot move "Delina but less rose"
at all.

### B — learn-about-fragrance

**Run on 2026-08-15 under the raised $1.50 ceiling. Three bottles,
$0.3909, stopped by the cap mid-cohort.**

```
facts                  664 -> 699   +35, of which 29 net-new singletons
repeated (2+)           89 ->  95    +6
supported               21 ->  21     0
declarable              15 ->  15     0
attributes on >=2       84 ->  89    +5
attributes on >=3       39 ->  39     0
answerable 'less X'      2 ->   2     0

singleton -> repeated conversions   2   ($0.1955 each)
singleton -> supported              0
cost per newly answerable query     n/a
```

Read narrowly: on this cohort, at this cost, on this corpus, enrichment
did not densify the matrix usefully. Read as a general verdict it is not
supported — two conversions is a sample of two, and the three bottles that
ran were the *dense* end. The thin end is untested, so "enrichment works
only where evidence is already dense" and "enrichment works nowhere"
remain unseparated.

The sharpest single number: **Layton took $0.1941 and returned zero
Layton-attributed facts.** Paying to read a bottle's own review section
does not reliably produce evidence about that bottle. That is a finding
about the mechanism, not about the cohort, and it is the one most likely
to generalise.

An earlier draft of this document said the experiment was "one command
from running". It was not — the `run` command checked the budget, printed
instructions, and did nothing, which the adversarial review caught. It is
now wired end to end through `frontier.enrich_one` and records results,
with each bottle diffed against a baseline re-read immediately before its
own run so that one bottle cannot be credited with conversions another
bought.

The prior conclusion "enrichment scatters" **does not transfer**. It was
measured on pages, and Delina's 9→20 mentions across 17 partners is a bad
page result and an unknown attribute result. The cohort spans 81
singletons/163 comments (Layton) down to 6/9 (Oud Wonder) precisely so the
answer distinguishes "enrichment works" from "enrichment works where
evidence is already dense" — and the second would invert what
under-covered scheduling assumes.

Ceiling: the whole catalogue, and it is the only strategy that can move
attribute density.

### C — canonical metadata

Measured and poor. The one reachable permitted source, `api.fragella.com`,
costs $0.05 per lookup and a prior run spent $3.00 on 60 lookups for 5
names and 0 pages — **$0.60 per name**.

Decisively for the motivating query: it returns **Brand, Name, Year and no
note lists**. Canonical raspberry is therefore unavailable from any
permitted source now reachable, so "crowd-pleasing raspberry" cannot be
improved from this direction. `Strength.CANONICAL` stays reserved and
unused rather than filled with a guess, and `name_facts` stays
`INSUFFICIENT`.

### D — semantic retrieval

Measured, free, and the answer is no. Over 18 hand-reviewed vibe queries
at k=10, five deliberately adversarial:

```
arm                        recall  precision  forbidden  cases hit
hashed-ngrams-v1            0.406      0.494          2          1
corpus-distributional-v1    0.317      0.478          2          2
```

The distributional arm retrieved **"masculine" for a "feminine" query**.
Co-occurrence cannot separate *used together* from *similar*, and in a
single-subject corpus nearly everything is used together.

The OpenAI arm is implemented and **unmeasured**: it projects at
**$0.000058** for the whole vocabulary and the cap refused it. That
refusal of six hundredths of a cent is the enforcement working
unconditionally. It remains the open question and it is the cheapest
outstanding experiment in this document by four orders of magnitude.

### E — new-release acquisition

Working and free at the discovery end. Now Smell This yields parseable
announcements; 2 were discovered, deduplicated, and **0 catalogued**
because neither Calvin Klein nor Memo Paris is a brand this 78-bottle
catalogue knows.

The measured limitation is **catalogue brand coverage, not the adapter** —
a test proves a known house flows to CATALOGED from the same real payload.

## Recommendation

**1. Run the OpenAI embedding arm first.** $0.000058, already built,
answers a question this document leaves open. Anything that costs less
than a thousandth of a cent should not stay unmeasured.

**2. Then run the attribute-enrichment experiment (B) on the ten-bottle
cohort**, ~$1.70 across two ledger days. It is the only strategy that can
move attribute density, which is the measured binding constraint, and its
economics are currently unknown rather than known-bad.

**3. Do not spend on pair verification (A) until B has reported.** Its
conversion rate is equally unmeasured, its ceiling is 22 pairs, 17 of
those need a new creator rather than a new commenter, and success there
cannot move a single relative comparison. The earlier recommendation to
start here was reasoning from a cost estimate, not from data.

**4. Do not spend on canonical metadata (C).** Measured at $0.60 per name,
and it supplies no note lists, so it cannot reach the queries that are
failing.

**5. Leave semantic retrieval (D) alone** pending the OpenAI measurement.

### What the scheduler should prioritise

Its current priority classes are sound, with one correction: **under-covered
bottles are not obviously the best learn targets.** The B experiment is
designed to settle whether enrichment converts singletons where evidence
is already dense or where it is thin, and until it reports, the
`UNDER_COVERED` class ahead of `REFRESH` is an assumption rather than a
finding.

## The true binding constraint

Not fact volume, not the ranker, not the parser. **It is that 87% of
attribute facts rest on one person and 116 attribute values exist on
exactly one bottle.** Every failing product question traces to that, and
only strategy B can address it — which is why it is worth $1.70 to learn
whether it does.

## Technical debt, carried deliberately

**The budget ledger has a cross-process race.** Each process reads the
ledger at start, so two paid workers running concurrently can each believe
the full remainder is theirs and together exceed the ceiling. Demonstrated
by `tests/test_budget.py:TestTheKnownConcurrencyGap`, which shows $1.80
charged against a $1.50 cap.

The ledger itself stays accurate — every batch is recorded — so the
overspend is visible after the fact rather than hidden. But the cap is
per-process, not per-day, whenever runs overlap.

**Do not run concurrent paid workers.** Every experiment so far has been
sequential, which is why the cap has held since the guard was fixed.

Before unattended scheduling is enabled in production, this needs an
atomic reservation: lock the ledger file, re-read, check, append, release
— and a test that runs two real processes against one ledger and asserts
the second is refused. That is a change to the enforcement mechanism
rather than to a caller, which is why it was not done during an experiment
that depends on the mechanism holding still.

The funnel instrumentation relies on the same assumption: it takes
autoincrement high-water marks and attributes everything above them to the
bottle being run, which is exact only while nothing else writes.
