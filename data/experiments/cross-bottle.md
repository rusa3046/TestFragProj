# Cross-bottle acquisition

Enrichment was scored per bottle: money spent on target X, facts gained
about target X. That accounting is wrong, and it is wrong by a factor of
1.6.

## Direct yield vs total graph yield

Claims attached anywhere during each bottle's run, run 2:

```
target                           usd  direct  total  $/direct  $/total
Dior Sauvage                  0.1124      27     66    0.0042   0.0017
Parfums de Marly Delina       0.0884      36     48    0.0025   0.0018
Creed Aventus Cologne         0.0695      17     37    0.0041   0.0019
Lattafa Khamrah Qahwa         0.1060      25     46    0.0042   0.0023
French Avenue Liquid Brun     0.0940      45     46    0.0021   0.0020
Parfums de Marly Oajan        0.0909      61    100    0.0015   0.0009
Fragrance World Oud Wonder    0.0209       3      5    0.0070   0.0042
TOTAL                         0.5821     214    348    0.0027   0.0017
```

**Counting only direct yield overstates cost per claim by 1.63×.** 39% of
what enrichment buys lands on a bottle that was not the target.

## The beneficiaries are graph neighbours, not noise

```
Dior Sauvage        -> Creed Aventus (29), Sauvage Elixir (8)
Parfums de Marly Oajan -> Althair (13), Layton (12), Haltane (5), Herod (4)
Creed Aventus Cologne  -> Creed Aventus (12), CdN Intense Man (5)
Lattafa Khamrah Qahwa  -> Khamrah (10), Angels' Share (6)
Parfums de Marly Delina -> Delina Exclusif (11)
```

Two relationships, both predictable before spending:

- **same house** — Oajan's review sections are full of Althair, Layton,
  Haltane, Herod and Carlisle
- **same comparison set** — Sauvage reviews argue about Aventus; Qahwa
  reviews argue about Khamrah

Neither is a surprise once stated, and both are computable from the
existing graph without buying anything.

## 19 bottles gained evidence without ever being a target

```
Creed Aventus                      41 claims
Parfums de Marly Althair           14
Parfums de Marly Layton            12
Parfums de Marly Delina Exclusif   11
Lattafa Khamrah                    11
Dior Sauvage Elixir                 8
...19 bottles, 132 claims total
```

Against 216 claims landing on the seven targets themselves. **Spillover
is 61% the size of the direct yield.**

## Two results that invert the per-bottle model

**Layton.** Enriched directly in run 1 for $0.1941 and returned *zero*
Layton-attributed facts — the sharpest failure in the cohort. It then
gained **12 claims for free** as a side effect of enriching Oajan
($0.0909). Enriching the neighbour worked where enriching the anchor
failed outright.

**Creed Aventus.** Never enriched. Gained **41 claims**, more than any
cohort target's direct yield except Oajan. Enriching Creed Aventus
Cologne directly bought 17 direct claims for $0.0695; Dior Sauvage
bought Aventus 29 claims as a side effect.

**Khamrah.** The one relative query that became newly answerable
(Khamrah/sweet) was produced this way — Khamrah was enriched in run 1
and the count did not move; it flipped during run 2, on evidence from
Khamrah Qahwa's review sections.

## What this implies, stated as narrowly as the evidence allows

A bottle whose own review sections are exhausted may still be reachable
through its neighbours' — because a neighbour's comment section is where
people compare, and a comparison mentions both. That is consistent with
the density finding: Layton is dense precisely because its own reviews
have been read, and dense bottles are exactly the ones whose remaining
evidence lives in someone else's comment section.

So the acquisition question is not "which bottle do I want facts about"
but "which review section contains talk about the bottle I want facts
about", and those are different bottles more often than not.

**Not yet established:** whether neighbourhood *predicts* yield well
enough to schedule on. Seven targets is a small sample, the neighbour
relationships here were read off after the fact, and no run has been
designed to test a prediction. The honest statement is that the
relationship exists and is large, not that it is yet a policy.
