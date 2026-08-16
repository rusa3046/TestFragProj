# Prospective prediction — does a neighbour's review section teach us about the anchor?

**Frozen 2026-08-16, before any paid call.** Nothing in the "Prediction"
or "Design" sections may change after the first dollar is spent. If
something here turns out to be badly chosen, the run is abandoned and a
new file is written — it is not edited.

> **Amendment, 2026-08-16, pre-run, by the repository owner. Nothing had
> been spent.**
>
> The primary outcome was "new stated facts about BR540 per dollar". It is
> now **new independent stated BR540 *sweetness* evidence per dollar**,
> because sweetness is the frozen query gap and a neighbour win on
> unrelated BR540 facts would not validate query-gap acquisition. All-BR540
> facts and total graph yield drop to secondary. Success now requires B > A
> on the sweetness axis **and** at least one new independent creator on
> BR540 sweetness.
>
> Recorded here rather than applied silently: the correction tightens the
> test, and a reader a month from now needs to see that it was made before
> the run and not after the numbers came in. Everything else is unchanged.

## Why this is written down in advance

The cross-bottle result is retrospective. Run 2 spent money on seven
targets and 134 of 348 claims landed on some *other* bottle; the
relationships were read off afterwards. That establishes spillover
happens. It does not establish that spillover is **predictable**, and a
scheduler needs the second thing:

```
predict   source B will teach us about anchor A
then      buy B
then      check whether it did
```

not

```
buy B  ->  notice B happened to teach us about A  ->  call it a signal
```

Choosing the neighbour after seeing the results is how the first shape
turns into the second without anyone intending it. Hence this file.

## The gap being targeted

`"Baccarat Rouge 540 but less sweet"` — **blocked**, and blocked on the
anchor rather than on the candidates:

```
anchor evidence     1 person / 1 creator
candidates          13 have sweetness at unknown prominence, 3 have more
blocker             the anchor's sweet evidence is only 1 creator
```

More candidates cannot fix this. Only evidence about BR540 can.

The whole of BR540's sweetness evidence is one sentence, from one person,
on one channel:

> "a fully dried down scent of deliciousness, decadence, a little sweet,
> the depth"

## The two arms, chosen now

Both use `enrich.learn_about`, the same broad `"<name> fragrance review"`
search as the measured cohort, under **identical ceilings**:

```
max_creators             4
max_videos_per_creator   1
max_comments           400
max_usd               0.10
stop                   first ceiling hit, or target met
```

| | arm | target | why it was chosen |
|---|---|---|---|
| **A** | direct | Maison Francis Kurkdjian Baccarat Rouge 540 | the anchor's own review sections |
| **B** | neighbour | Thomas Kosmala No. 4 | strongest comparison edge to BR540 (3 people, 2 creators) that has never been searched |

BR540's neighbours, by edge strength, with what the corpus already holds
about each:

```
Thomas Kosmala No. 4            3 people / 2 creators edge     6 claims,  2 creators read
Franck Olivier Pure Addiction   2 people / 1 creator  edge     3 claims,  1 creator  read
```

Thomas Kosmala No. 4 is chosen over Franck Olivier because its edge to the
anchor rests on two creators rather than one. That is the rule, stated
before the run: **the neighbour is the bottle with the strongest
independent comparison edge to the anchor.** Not "the one that looks
promising".

## Why the neighbour is predicted to help

Mechanism, stated concretely enough to be wrong:

Thomas Kosmala No. 4 is discussed almost entirely *as a BR540 alternative*.
People arriving in its comment section are there to compare, and the
sentence they write is of the form "this is BR540 but ___". That is
comparative sweetness evidence **about BR540**, produced in somebody
else's comment section, and it is the exact shape the blocked query needs.

The direct arm is predicted to return little because BR540's own review
sections have already been read three separate ways:

```
"540 fragrance review"        22 videos
"br 540 fragrance review"     21 videos
"baccarat rouge 540 dupe"      4 videos
```

Thomas Kosmala No. 4 has never been searched at all.

## Prediction

**Primary — the sweetness axis, because that is the frozen gap.**

Measured as `coverage.relative_coverage` reports it for the case
`("Maison Francis Kurkdjian Baccarat Rouge 540", "sweet")`: the number of
independent people and creators supporting BR540's sweetness, before and
after each arm, divided by that arm's spend.

1. **Arm B adds more independent stated BR540 sweetness evidence per
   dollar than arm A.**
2. **Arm B adds at least one creator** to BR540's sweetness axis — a
   channel not already among the one that supports it today.

**Secondary, recorded but not decisive:**

3. Arm A produces zero or one new stated BR540 facts of any kind, because
   its ground has been covered three times.
4. New stated facts about BR540 overall, per arm.
5. Total graph yield — new stated facts about *any* bottle, per arm.

Success requires **1 and 2 together**. A neighbour that returns plenty of
unrelated BR540 facts and nothing about sweetness has not validated
query-gap acquisition; it has only reproduced the spillover already known
to happen, which is the thing this run exists to go beyond.

## What would falsify it

- **Arm A ≥ arm B** on new independent BR540 *sweetness* evidence per
  dollar. Then neighbourhood is not a schedulable signal on this evidence,
  and the query-gap scheduler does not get built on it.
- **Arm B adds no new creator to the sweetness axis**, whatever else it
  returns. Condition 2 fails and the result is negative, because the
  blocked query needs a second independent creator and nothing else will
  unblock it.
- **Both arms produce zero BR540 sweetness evidence.** Then the anchor is
  not reachable by buying comments at all, and the right move is to stop
  trying to answer this query rather than to spend more.

Either outcome is worth $0.20. The failure mode this file exists to
prevent is spending $0.20 and then deciding afterwards which number made
the case.

## The confound, and how it is separated

Thomas Kosmala No. 4 is **both** a neighbour of the anchor **and** sparse
(6 claims, 2 creators read). Sparse bottles were already measured to
convert 2.9× cheaper than dense ones. So "arm B did better" is ambiguous
between two explanations, and the metric has to tell them apart:

```
sparse effect        predicts B yields more facts ABOUT THOMAS KOSMALA No. 4
neighbourhood effect predicts B yields more facts ABOUT BR540
```

Both are recorded. **Only the second counts as support for the
hypothesis.** If arm B returns 20 new Thomas Kosmala facts and no BR540
facts, that is the sparse effect reproducing and the neighbourhood claim
failing, and it will be reported that way.

## Scope of whatever this shows

Narrow, deliberately.

- **n = 1 anchor, 1 neighbour, 2 arms.** This cannot establish a policy.
  It can kill the hypothesis cheaply, or justify a larger test.
- The direct arm is **already exhausted** — searched three ways. So a win
  for arm B supports *"when an anchor's own reviews are exhausted, buy its
  neighbour's"*, not *"prefer neighbours in general"*.
- Order is **A first, then B**, and that biases against the hypothesis: any
  fact both arms could have found is credited to A, since B's gain is
  measured from a snapshot taken after A ran. Chosen for that reason.

## What gets recorded, per arm

```
BR540 sweetness: people and creators, before/after   <- PRIMARY
new creators on BR540's sweetness axis               <- the pass/fail gate
"BR540 but less sweet" ANSWERABLE?                   <- the query unblocking
new stated facts about BR540 overall                 <- secondary
new stated facts about the arm's own target          <- the sparse confound
new stated facts about any bottle                    <- total graph yield
comments read, USD, stop reason, quota units
```

## Cost

```
$0.10 ceiling per arm, $0.20 total
today's ledger: $1.0044 of $1.50, leaving $0.4956
YouTube: 2 searches, 200 units of 10,000
```

## Sign-off

Nothing above is executed until the repository owner approves this file.
The run writes its results to `data/experiments/neighbour-result.md`,
which cites this file by commit hash.
