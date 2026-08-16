# Result — does a neighbour's review section teach us about the anchor?

Run 2026-08-16T20:02:56+00:00. Design frozen in `data/experiments/neighbour-prediction.md`, before any spend.

## FAIL

B did not beat A on the sweetness axis and added no creator. Neighbourhood is not a schedulable signal on this evidence.

## Primary — independent stated BR540 sweetness evidence

```
                        people  creators        $  people/$
before both arms             1         1                   
A  direct (Maison Fr)        1         1   0.0344       0.0
B  neighbour                 1         1   0.1228       0.0
```

New creators on the axis — A: none, B: none

`"BR540 but less sweet"` answerable: no — blocker: the anchor's sweet evidence is only 1 creator(s)

## Secondary, and the sparse confound

```
                         BR540  own target  any bottle  comments        $
A  direct                    5           5           5        76   0.0344
B  neighbour                 0           3           3       272   0.1228
```

Read the confound here: **own target** is what the sparse effect predicts, **BR540** is what neighbourhood predicts. A large first column with an empty second is the sparse effect reproducing and the neighbourhood claim failing.

Stopped because — A: creators-exhausted, B: creators-exhausted
Quota — A: 0 units, B: 0 units
Total spend: $0.1572

---

## Post-run diagnostic — why B returned zero

Written after reading the data. The design above was frozen; this was not
part of it, and it is the reason the failure is worth more than the
prediction would have been.

**The predicted mechanism happened.** Thomas Kosmala No. 4's audience does
talk about the anchor, and heavily:

```
272  comments bought from the neighbour
 28  of them mention BR540 by some spelling
 30  claims naming BR540 were extracted and resolved
```

So the neighbour was chosen correctly and the comment sections contained
what the prediction said they would.

**What they contained was the wrong shape.** Of the 30 claims naming
BR540:

```
comparison TO the anchor    30   "this is similar to BR540", "better than BR540"
description OF the anchor     0
```

Nobody in a Thomas Kosmala comment section describes BR540. They compare
*to* it. BR540 is the reference point, not the subject — which is exactly
why they are there.

Sweetness evidence was produced by that run — five claims — and every one
of them describes **Thomas Kosmala**, the bottle the video was about.

## The rule this establishes

> A neighbour's comment section yields **comparisons to** the anchor, not
> **descriptions of** it.

That splits query-gap acquisition cleanly in two:

| question shape | example | neighbour acquisition |
|---|---|---|
| what is like X | "something like BR540" | **works** — 30 edges for $0.12 |
| how X is Y | "BR540 but less sweet" | **does not** — 0 descriptions |

The blocked query is the second kind. No amount of neighbour spending
reaches it, because the people in the neighbour's comments have no reason
to describe the bottle everyone already knows.

## The direct arm also refuted its own prediction

Prediction 3 said arm A would return "zero or one" new BR540 facts,
because its review sections had been searched three separate ways across
43 videos. It returned **five, for $0.0344** — a quarter of the neighbour's
cost. The anchor was not exhausted. "A bottle's own review section stops
paying once it has been read" is not supported here, and the earlier
cohort's version of that claim should be re-read with this in mind.

## What follows

- Do not build neighbour-based scheduling for attribute gaps. Measured, at
  $0.16, before anything was built on it.
- The cheapest route to BR540's sweetness remains its own reviews, which
  are still paying.
- Worth testing separately, and not tested here: whether neighbour
  acquisition is the right instrument for *pair* gaps, where 30 edges for
  $0.12 looks strong. That is a different experiment and needs its own
  frozen prediction.
