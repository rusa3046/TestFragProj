# The ten-bottle enrichment cohort, closed

Run 1 (2026-08-15): 3 bottles, $0.3909, stopped by the cap.
Run 2 (2026-08-16): 7 bottles, $0.5819, ran to completion.
**Total $0.9728 for 10 bottles.** Identical treatment throughout.

## Two instrumentation defects, found after the run

Both are recorded here because they changed what the first printed
report said, not because they changed what was bought.

**The funnel printed zeros for all seven bottles.** `density_before` and
`funnel` were only assigned inside the `except BudgetExhausted` branch.
Run 1 hit the cap, so it produced a correct funnel and the code looked
right; run 2 finished cleanly and recorded nothing. Diagnostics that run
only on the failure path work in the rehearsal and vanish on the take.

The knock-on was worse than the empty table: with `density_before`
unset at 0, **every bottle banded "sparse"**, including a 46-fact Dior
Sauvage — so the segmentation, the single output this cohort was
designed to produce, printed a confident and meaningless answer.

Fixed so both paths record, with a test that fails on the old code. The
funnel below is reconstructed read-only from the corpus: each bottle's
videos carry the search query that found them, so its claims are
recoverable without re-spending.

## Funnel, per bottle (run 2)

```
bottle                       facts crea  cmts  clms  rej unary pair other float
Dior Sauvage                    46    1   292   103   21    15   14    33    41
Parfums de Marly Delina         33    4   213    91    8    23   13    11    44
Creed Aventus Cologne           25    4   179    47   17     2    7    13    25
Lattafa Khamrah Qahwa           27    3   210    90   22    15    3    19    53
French Avenue Liquid Brun       17    4   199    91    7    10    4     1    76
Parfums de Marly Oajan           7    4   149   118    1    33   14    32    39
Fragrance World Oud Wonder       6    4    43    13    3     1    1     1    10

TOTAL                                  1285   553   79    99   56   110   288
                                                12%   18%  10%   20%   52%
```

Compared with run 1: **target-unary rose 7% → 18%** and floating fell
**67% → 52%**. The pipeline did not change between runs; the bottles
did. Run 1 was the dense end of the cohort, where the searches return
videos already harvested and the marginal comment is about something
else.

### A loss stage run 1 never separated

**79 claims (12% of everything emitted) were refused at schema
validation**, before attribution was ever attempted. This is upstream of
the floating bucket and invisible inside it. By reason:

```
NOTE_DESCRIPTOR object_kind {TAG}, got NONE     ~45   unary, the scarcest kind
BETTER_THAN object_kind {FRAGRANCE}, got TAG    ~13   pairwise
NOTE_DESCRIPTOR requires raw_object_text          7   unary
DUPE_OF / BETTER_THAN, got HOUSE or NONE         ~9   pairwise
OCCASION object_kind {TAG}, got NONE              2
```

Roughly half are `NOTE_DESCRIPTOR` — the unary attribute claim the
recommender is starved of — discarded because the extractor emitted a
descriptor with no object. That is a prompt/schema mismatch, not missing
information: the comment said something usable and the pipeline threw it
away. Cheaper to fix than either attribution repair or catalogue growth,
and it was hiding under a 67% headline for the whole of run 1.

Creed Aventus Cologne is the clearest case: 17 rejects against 47
accepted claims, 2 of which were about the target. It returned nothing
for $0.0695.

## Segmented by pre-experiment density

```
band       bottles    spend  conv   new    $/conv
dense            3   0.3909     2     4   $0.1955
medium           4   0.3762     2    23   $0.1881
sparse           3   0.2057     3    30   $0.0686
TOTAL           10   0.9728     7    57   $0.1390
```

**This inverts the assumption the scheduler encodes.** Enrichment is
*cheapest* on sparse bottles — $0.0686 per conversion against $0.1955 on
dense ones, a 2.9× difference — and returns more new facts there (30 vs
4). Parfums de Marly Oajan, with 7 facts before, returned 25 new facts
and a conversion for $0.0909. Parfums de Marly Layton, with 109, returned
nothing for $0.1941.

The reading is mundane once seen: a bottle with 109 facts has already had
its review sections read, so another search returns the same comments. A
bottle with 7 has not.

Three bottles per band is a small sample and the bands differ in more than
density, so treat the ordering as established and the ratio as indicative.

## Corpus, before run 1 → after run 2

```
                        before    after    delta
facts                      664      788     +124
  singleton                575      686     +111
  repeated (2+)             89      102      +13
  supported                 21       25       +4
declarable facts            15       19       +4
bottles with any fact       63       65       +2
bottles declarable           6        9       +3
attributes on 2+ bottles    84       98      +14
answerable 'less X'          2        3       +1
comments processed        9124    11291    +2167
creators                    57       86      +29
```

Run 1 alone returned +0 supported, +0 declarable, +0 answerable. Across
the full cohort those are +4, +4 and +1. **The three-bottle result was
not a small version of the ten-bottle result; it pointed the wrong way**,
because it sampled only the end of the range where enrichment works worst.

### The hypothesis under test

> Targeted learn-about-fragrance enrichment makes the attribute ×
> fragrance evidence matrix denser enough to materially improve general
> recommendation.

**Not falsified, and weakly supported.** The matrix did get denser in the
way that matters — comparable attributes +14, declarable bottles 6 → 9 —
and one relative query became answerable. Against that, 111 of 124 new
facts are singletons, so the dominant effect is still breadth rather than
agreement, and $0.97 per one newly answerable query is not obviously a
price worth paying at catalogue scale.

The honest summary is that enrichment works, on sparse bottles, slowly.

## The newly answerable query came from a bottle that was not enriched

The third answerable case is **Lattafa Khamrah / sweet** (anchor now 10
people across 6 creators; 18 candidates with less). Khamrah was enriched
in run 1, and run 1 ended with the count still at 2. It flipped during
run 2, when Khamrah itself was not touched.

The evidence came from other bottles' comment sections — Khamrah Qahwa's
reviews discuss Khamrah constantly. **That is the 20% "other bottle"
bucket paying for itself**, and it is the strongest argument against
treating that bucket as loss. It also means per-bottle attribution
understates the return: the cheapest way to strengthen an anchor may be
to enrich its neighbours.

## The five tracked benchmark comparisons

```
Delina / rose      baseline usable   4 comparable    unchanged   ANSWERABLE
BR540 / sweet      baseline THIN    14 -> 19 comp.   improved    no
Angels' Share/smoky  no evidence     0 comparable    unchanged   no
Libre / vanilla    not in catalogue                  unchanged   no
Layton / vanilla   baseline usable   8 -> 10 comp.   improved    ANSWERABLE
```

**None of the five changed status.** The +1 is Khamrah/sweet, which is
not one of them. BR540/sweet gained five comparable candidates and is
still blocked on its own anchor being one person on one creator — more
candidates cannot fix a thin anchor, which is the distinction the
acquisition report draws and this run confirms.

Recommendation benchmark: 22/22, 0 unsupported assertions. Provenance
audit: 0 violations.

## Spend

$0.9728 of the $1.50 ceiling across two UTC days, sequential throughout.
The cross-process ledger race is untouched and still means: do not run
concurrent paid workers.
