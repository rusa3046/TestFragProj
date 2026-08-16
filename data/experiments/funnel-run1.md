# Where run 1's money went

Three bottles, $0.3909, 403 claims extracted.

| stage | claims | share |
|---|---|---|
| unary, about the target bottle | 30 | 7% |
| pairwise, about the target | 39 | 10% |
| about some other bottle | 63 | 16% |
| unattributed / floating | 271 | 67% |

Of the 30 target-unary claims: 11 became new attribute facts, 2 converted
a singleton to repeated, 0 reached supported.

## What this does and does not establish

It decomposes **where the current pipeline loses what it buys.** It does
*not* establish the root cause of the floating bucket, and an earlier
draft of this file claimed it answered that question. It does not.

271 floating claims could be any mixture of:

    a pronoun with an obvious referent      "this lasts forever"
    a reply whose parent named the bottle
    one candidate implied by the video
    several candidates, genuinely ambiguous
    no fragrance subject at all             "great video, subscribed"

Those have very different upside. The first three are recoverable; the
last two are not, and no amount of attribution work touches them.
**Classifying the floating bucket is a prerequisite for knowing how much
repair is worth**, and it is done after the cohort so the classification
covers all ten bottles under one treatment.

### First classification, corpus-wide

Run read-only across all 2,245 floating claims in the corpus, not just
run 1's 271, so the shape is measured on more than one cohort's worth.

```
names a bottle the catalogue lacks       1071   48%
video-subject recoverable                 390   17%  <- recoverable
several candidates, genuinely ambiguous   368   16%
names a flanker of a known bottle         126    6%
names a concentration, not a bottle       118    5%
single candidate named in the comment     109    5%  <- recoverable
names a category, not a bottle             51    2%
no fragrance subject at all                12    1%

RECOVERABLE, with disclosure              499   22%
ceiling on any attribution repair        1746   78%
```

**This inverts the working hypothesis.** The bottleneck was framed as
attachment — the system failing to connect what people say to the bottle
they obviously mean. Attachment repair is real but bounded at 22% of
floating claims, 13% of the corpus. The larger cause, at 48%, is that
**people are talking about bottles the 78-item catalogue does not
contain** — `Naxos` alone accounts for 21 claims. No attribution rule
reaches those; only catalogue growth does.

Two buckets are refusals rather than failures. A flanker is a *different
bottle*: attaching "Baccarat rouge extrait" to Baccarat Rouge 540 is the
merge the rest of the system exists to refuse, and the 126 there are
correctly floating. The same holds for 118 concentration references
("the extrait") and 51 category references ("clones", "dupes").

### How much to trust this number

Less than a clean table suggests. The first version of the classifier
reported **51% recoverable**; it is 22% after four corrections, each
found by reading sampled rows rather than by reasoning about the code:

- it attached `'Arianna Grandes Cloud'` — a bottle the catalogue lacks —
  to Baccarat Rouge 540 because the *comment* mentioned BR540 elsewhere;
- it counted flankers as recoverable to their base bottle;
- it counted `'extrait'` and `'The EDP'` as missing bottles;
- it counted `'clones'` and `'dupes'` the same way.

Every one inflated the upside. That is the direction a measurement drifts
when the person writing it wants the answer to be large, so the residual
22% should be read as an upper bound that has been corrected downward
four times and not yet audited by anyone else.

Fuzzy-matching the catalogue-gap bucket does **not** rescue it: at a 0.72
cutoff only 91 of 1,071 match a known bottle, and reading them shows the
matcher pairing `Devil's Share` with `Angels' Share` and `Dior Sauvage
Extrait` with `Dior Sauvage Elixir` — different bottles both times. Real
misspellings of catalogued bottles are a handful.

Also measured while checking: **140 of 3,964 asserted claims (4%) are
duplicates** on `(comment, subject, type, span)`. Small, but it inflates
every count in this document by about that much, and it is recorded here
rather than fixed mid-cohort.

## 7% is not the useful fraction

An earlier reading of this table treated the other 93% as waste. That is
wrong, and it matters because it makes YouTube look far worse than it is.

    7%   target unary          exactly what the recommender needs most
    10%  target pairwise       real relationship evidence; the pair
                               product is built on precisely this
    16%  another bottle        "Atomic Rose is way smoother" under a
                               Delina review is not a diversion — it is
                               frontier evidence about a bottle nobody
                               searched for
    67%  floating              partly recoverable, unknown fraction

The 16% needs splitting too — useful comparison, useful unary evidence
about the other bottle, and genuine diversion are three different things
and only the third is loss.

So the honest summary is: **17% directly usable today, plus an unmeasured
recoverable share of 83%.** Not "7% useful".

## Ranked losses, current pipeline

1. **subject attribution — 67%.** Larger than every other stage combined.
2. **off-target retrieval — 16%.** Some fraction of it genuinely useful.
3. **pairwise where unary was wanted — 10%.** Not waste, wrong job.
4. **conversion — 2 from 30 usable claims.** Genuine scarcity of repeated
   agreement, operating on a seventh of the input.

Source scarcity is *not* implicated: 25 videos returned per search and the
spend ceiling stopped each run, not a shortage of material.

## Deliberately not acted on

`attributes infer` recovers some floating claims from the video subject.
Running it between cohort members would change the treatment mid-cohort
and make the ten bottles incomparable.

Everything this table suggests — narrower queries, target-relevance
filtering on videos, extraction prompted to resolve pronouns — is recorded
here and applied to nothing until the cohort closes.
