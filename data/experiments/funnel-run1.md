# Where run 1's money went

Three bottles, $0.3909, 403 claims extracted.

| stage | claims | share |
|---|---|---|
| **unary, about the target bottle** | **30** | **7%** |
| pairwise, about the target | 39 | 10% |
| about some other bottle | 63 | 16% |
| unattributed / floating | **271** | **67%** |

Of the 30 target-unary claims: 11 became new attribute facts, 2 converted
a singleton to repeated, 0 reached supported.

## Reading

The dominant loss is **subject attribution**, not source scarcity. Two
thirds of everything bought could not be placed on any bottle, because
commenters under a review write "this", "it" and "the original" — they
are not being careless, the video already said which bottle.

Ranked by size, the losses are:

1. **subject attribution — 67%.** The single biggest, larger than every
   other stage combined.
2. **wrong target — 16%.** A search for one bottle returns videos whose
   comments discuss others. Real evidence, just not what was paid for.
3. **pairwise rather than unary — 10%.** The job asked what a bottle is
   like and got comparisons, on a corpus already dupe-shaped.
4. **attribute conversion — of 30 usable claims, 2 conversions.** Genuine
   lack of repeated agreement, but operating on a seventh of the input.

Source scarcity is *not* implicated: 25 videos were returned per search
and the ceiling stopped the runs, not a shortage of material.

## Deliberately not acted on yet

`attributes infer` recovers some floating claims from the video's subject,
and 362 such attributions already exist from before the experiment.
Running it between cohort members would change the treatment mid-cohort
and make the ten bottles incomparable. It runs **after** all ten, measured
as its own intervention.

Same for every improvement this table suggests — narrower queries, a
target-relevance filter on videos, extraction prompted to resolve
pronouns. Recorded, not applied.
