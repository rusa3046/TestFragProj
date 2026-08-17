# Experiment B — safe attribution recovery

Offline. No YouTube requests, no LLM calls, $0.00. Re-reads evidence
already bought and asks how much of it a rule can attach to a bottle.

## A correction to the Experiment A numbers first

The cohort report quoted facts 788, comparable attributes 98, supported
25. Those were computed at `Attribution.PROPOSED` against 362 inference
rows that predated the cohort. **They were never a stated-only corpus.**
The frozen Experiment A baseline, separated by policy:

```
                        STATED   PROPOSED(stale)
facts                      629       788
repeated (2+)               72       102
supported                   19        25
declarable                  19        19
comparable attributes       82        98
answerable relative          3         3
```

The distinction matters in one direction only: `declarable` is identical
under both, because a fact's declarability is computed from the *stated*
half alone. Inference widens what the system finds and never what it
says, and this is the measurement that shows it rather than the docstring
claiming it.

## A → B

Ran `record_inferences` over the whole corpus, including the 2,167
comments the cohort added. 398 proposed attributions now exist, all
`review_status='proposed'`; none touch `claims.subject_frag_id`.

```
                        A (stated)   B (proposed)    delta
floating claims               2523          2139     -384
target unary                  1046          1430     +384
target pairwise                928           928        0
facts                          629           803     +174
  repeated (2+)                 72           105      +33
  supported                     19            25       +6
declarable facts                19            19        0
declarable bottles               8             9       +1
comparable attributes           82            99      +17
answerable relative              3             3        0
```

**Attribution recovery is a retrieval win and a declaration no-op.**
+17 comparable attributes and +33 repeated facts are real: they widen
what can be found and compared. +0 declarable facts and +0 newly
answerable relative queries are equally real, and they are the design
working rather than failing. A fact assembled from claims a machine
attached cannot be stated in the system's own voice, however many such
claims agree.

So Experiment B cannot move the failing product questions. It makes the
retrieval layer meaningfully broader underneath them.

## The corrected ceiling was accurate

The floating classifier predicted 430 video-subject recoverable claims.
`attach_by_video` recovered 384 — **89% of its own prediction**, the
shortfall being claims whose video subject the rule declines to trust.

That is worth recording because the first version of the classifier
predicted 51% recoverable corpus-wide. The achieved figure is 15% of
floating claims. Had the uncorrected estimate stood, this experiment
would have been scored a severe underperformance against a number that
was simply wrong.

## Remaining floating evidence, classified

2,523 floating at stated attribution; 2,139 after recovery.

```
names a bottle the catalogue lacks       1264   50%   -> Experiment D
video-subject recoverable                 430   17%   -> 384 recovered here
several candidates, genuinely ambiguous   388   15%   ceiling
names a flanker of a known bottle         142    6%   correct refusal
names a concentration, not a bottle       120    5%   correct refusal
single candidate named in the comment     111    4%   deliberately refused
names a category, not a bottle             55    2%   correct refusal
no fragrance subject at all                13    1%   ceiling
```

**The 111 "single candidate" claims are refused on purpose.**
`attach_by_video` declines any claim whose commenter named a fragrance,
because a comment that mentions Baccarat Rouge 540 and then says "this
is sweet" may well mean a third bottle, or the Extrait. Refusing them is
what holds the rule's measured error at about 1 in 20. Recovering them
needs a separate rule with its own hand-measured accuracy, and is worth
at most 4% of floating evidence — recorded as available, not taken.

**317 claims (13%) are correct refusals**, not losses: flankers,
concentrations and categories are cases where attaching would be wrong.
A ceiling that counts them as recoverable is measuring the wrong thing.

**The largest bucket is not an attribution problem at all.** 1,264
claims — half of everything floating — name a fragrance the 78-item
catalogue does not contain. No attribution rule reaches those. That is
Experiment D, and it is now clearly the larger prize.
