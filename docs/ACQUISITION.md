# What to buy next, and what the measurements say

Rewritten 2026-08-16, after the ten-bottle enrichment cohort and three
offline recovery experiments. Every number is measured against the
committed corpus unless marked **estimated**, **projected** or
**blocked**, and that distinction carries most of the weight here: the
strategy an earlier version of this document recommended first has still
never been run.

> **Amendment, 2026-08-18 — catalog-first changed the denominator, not
> the analysis.** Everything below measures what the *evidence graph* can
> declare, and all of it still holds. What changed is that declarability
> stopped being the gate on whether FACET can answer: candidates now come
> from the retail catalogue, so "less sweet, for summer" is answerable
> across 548 bottles on declared notes even where no bottle clears an
> evidence bar. The numbers here are now a measure of **how much community
> texture an answer can carry**, not of whether an answer exists. The
> ranking of the seven strategies is unaffected — buying comments about
> thin anchors is still the only lever that moved a product question, and
> a catalog fit with nothing said about it is still a weaker card than one
> wearers confirm.

## The constraint, restated

```
                        STATED   PROPOSED     what the difference is
facts                      629        844     inferred attribution
repeated (2+)               72        106
supported                   19         28
declarable                  19         19     <- identical, by design
declarable bottles           8          9
comparable attributes       82        100
answerable 'less X'          3          3     <- identical
```

`declarable` is the same under both policies because a fact's
declarability is computed from the *stated* half alone. **Every offline
recovery below moves the left column's neighbours and not the gate.**
That is the single most useful thing learned this week: attribution work
widens retrieval and cannot, by construction, make the system able to say
anything new.

Relative comparisons, all six:

```
ANSWERABLE  Delina / rose          4 comparable
ANSWERABLE  Layton / vanilla      10 comparable
ANSWERABLE  Khamrah / sweet       18 comparable   <- new, and free
   --       BR540 / sweet         19 comparable, anchor is 1 person/1 creator
   --       Angels' Share / smoky  anchor has no smoky evidence
   --       Creed Aventus / fruity anchor is 1 person/1 creator
```

Three of the six now answerable, up from two. **The three that fail all
fail on the anchor, not the candidates.** BR540 has 19 comparable
candidates and cannot be used, because the bottle everything is compared
*to* rests on one person. More candidates cannot fix that; only evidence
about the anchor can.

## The seven strategies

| | cost | measured? | useful facts | supported/declarable | density | relative | total graph yield | provenance risk | scales? |
|---|---|---|---|---|---|---|---|---|---|
| **A** pair verification | $0.17/pair **est.** | ❌ never run | 1 edge/pair | none | none | none | unmeasured | low | 22 pairs |
| **B** learn-about-fragrance | $0.097/bottle | ✅ 10 bottles | +124 facts | +4 / +4 | +14 attrs | +1 | **1.6× direct** | low | catalogue |
| **C** canonical metadata | $0.05/lookup | ✅ 60 lookups | 5 names | none | none | none | none | low | poor |
| **D** semantic retrieval | $0 | ✅ 18 queries | none | none | none | none | none | — | done |
| **E** new-release | $0 discovery | ✅ 2 found | 0 catalogued | none | none | none | none | low | feed |
| **F** catalogue expansion | $0 + verification | ✅ offline | 47 claims / 7 bottles | unmeasured | small | unmeasured | **medium** | tail |
| **G** attribution + schema recovery | $0.42 one-off | ✅ offline | +458 attached, +31 stated facts | **0 / 0** | +20 attrs | **0** | n/a | low–medium | done once |

### A — targeted pair verification

Still never run. 22 near-miss pairs, 17 short on *creators* rather than
commenters. $0.17 is `ESTIMATED_JOB_USD`, not an observed conversion.

Its ceiling is 22 pages and it cannot move attribute density or any
relative comparison. **The cross-bottle result weakens it further**: pair
edges arrive as a side effect of enrichment anyway — Sauvage's run
produced 29 Aventus claims including comparisons, unpaid-for.

### B — paid learn-about-fragrance enrichment

**Measured across ten bottles, $0.9728.** Segmented, because the
cohort-wide average hides the finding:

```
band       bottles    spend  conv   new facts    $/conversion
dense            3   0.3909     2           4        $0.1955
medium           4   0.3762     2          23        $0.1881
sparse           3   0.2057     3          30        $0.0686
```

**Sparse bottles convert 2.9× cheaper and return 7× the new facts.**
Oajan (7 facts before) returned 25 new facts and a conversion for
$0.0909. Layton (109 facts before) returned nothing for $0.1941.

The mechanism is mundane: a bottle with 109 facts has had its review
sections read already, so another search returns comments the corpus
holds. This inverts what the scheduler's `UNDER_COVERED`-before-`REFRESH`
ordering assumed — correctly, as it turns out, but for a reason nobody
had measured.

Corpus effect: facts 664 → 788, supported +4, declarable +4, declarable
bottles 6 → 9, comparable attributes +14, one relative query answerable.

**Read narrowly.** 111 of 124 new facts are singletons. The dominant
effect is still breadth, and $0.97 per newly answerable query is not
obviously worth paying at catalogue scale.

### C — canonical metadata

Measured and poor. $0.05/lookup; a prior run spent $3.00 for 5 names and
0 pages — **$0.60 per name**. Returns Brand, Name, Year and **no note
lists**, so it cannot reach "crowd-pleasing raspberry" or any note
question. `Strength.CANONICAL` stays reserved and unused.

It has one new use: **identity verification for F**. $0.35 would confirm
the seven catalogue candidates that clear the publishing gate.

### D — semantic retrieval

**Now measured, all three arms.** 18 hand-reviewed vibe queries at k=10,
five deliberately adversarial. Same query set, scoring and retrieval
rules as before.

```
arm                              recall  precision  forbidden  cases hit
hashed-ngrams-v1                  0.372      0.550          2          1
corpus-distributional-v1          0.317      0.556          1          1
openai:text-embedding-3-small     0.461      0.528          0          0
```

**The OpenAI arm wins on the axis that matters and loses slightly on
precision.** +24% recall over the lexical baseline, and — the reason to
care — it is the only arm that retrieved **no forbidden term** and failed
**no adversarial case**. The other two each pulled an antonym:
hashed-ngrams gave *heavy* and *strong* for "airy"; the distributional
arm gave *masculine* for "feminine".

Measured cost for the whole vocabulary: **$0.0000736** (1,074
descriptors + 18 queries).

The free-arm figures differ slightly from the earlier run (recall was
0.406 for hashed-ngrams) because the corpus grew by 2,167 comments in
between, changing the descriptor vocabulary. The three arms above were
run against the same corpus in the same invocation and are comparable to
each other; they are not comparable to the pre-cohort numbers.

**Embeddings stay retrieval-only.** Nothing here creates or strengthens a
smell assertion; the arm ranks descriptors and the evidence ladder is
untouched.

Worth adopting: it is better on the failure mode that produces
embarrassing recommendations, and it costs seven thousandths of a cent
per full pass.

### E — new-release acquisition

Working and free at the discovery end; 2 announcements parsed, 0
catalogued, because neither house is in the catalogue. The limitation is
brand coverage, not the adapter — a test proves a known house flows to
CATALOGED from the same real payload.

### F — catalogue expansion (new)

The floating histogram said half of unattached evidence names a bottle
the catalogue lacks, which looked like the largest prize. Ranked by
stranded evidence, it is a long tail.

```
1178 claims across 762 distinct names        1.5 claims per name
  44 candidates with >=3 claims               184 claims
   7 candidates clearing the publishing gate   47 claims
```

Adding an entity recovers its own claims and nothing else, so 700-odd
names would need researching for one or two claims each.

**Naxos is the exception and should be done**: 23 claims, 18 commenters,
4 creators — it arrives already past the gate, on evidence already
bought. La Roseé, Cloud and Vulcan Feu are marginal.

Identity decisions are **not** made by fuzzy matching here. House vs
fragrance ("Creed"), flanker vs base ("Exclusif"), and concentration vs
product ("MFK Extrait") all still need a person or a canonical lookup.

### G — attribution + schema recovery (new)

Two offline recoveries against evidence already paid for.

**Attribution (B-replay).** `record_inferences` over the whole corpus,
with multilingual deixis added:

```
floating claims       2523 -> 2065     -458
facts (proposed)       629 ->  844     +215
repeated                72 ->  106      +34
supported               19 ->   28       +9
comparable attributes   82 ->  100      +18
declarable facts        19 ->   19        0
answerable relative      3 ->    3        0
```

The zeroes are the design holding. A useful, bounded, free win for
retrieval that cannot answer a product question.

**A provenance defect was found here and fixed**, by adversarial review
rather than by the experiment. `evidence_verified` checked that the
quotation was real and never that the *value inside it* was: a claim
could quote "It smells wonderful" verbatim and store `rose`. The
requirement above — that tagged types always carry an object — is
exactly what makes that dangerous, since it converts "missing
descriptor" into "descriptor the model supplied".

Grounding now applies to the three tagged types, and **only** to them.
Applied to every type it demoted 135 claims, 126 of them comparisons
whose object a commenter legitimately names in a parent comment. Scoped,
it demotes 9, and the corpus aggregate moves by one repeated fact. Cheap
to fix, and it would not have been cheap to find later.

Found here: `attributes.DEICTIC` was English-only, so Spanish,
Portuguese, Indonesian and Vietnamese pronouns were being counted as
*fragrances the catalogue lacks*. "este perfume" ranked second on the
acquisition list. Fixed; worth 81 claims and it cost nothing.

**Schema rejection.** 308 rejected claims, and 215 (70%) were one
prompt/schema disagreement: tagged types emitted with `object_kind: NONE`
while the descriptor sat in `evidence_span`. The model had found the
claim and quoted it — "It's woodsy", "Gold is a powdery gourmand" — and
the prompt never said the value belonged in `raw_object_text`.

Fixed in the prompt; **the schema was not widened**, and a test pins that
it still refuses an objectless descriptor. Replaying 272 stored comments:
rejections 308 → 32, tagged-type rejections 215 → **0**, and 183
descriptors recovered with values. $0.26, no comments bought.

The remaining 32 are pairwise types with no object — "it's a clone" with
no target — and refusing them is correct.

Coverage measured by persisting the recovery in a **scratch database**
(a destructive replace, refused against any URL without "scratch" in it,
so the working corpus was untouched). 200 comments replaced:

```
claims on those comments   92 -> 237   +145
rejected claims           229 ->  23   -206
STATED facts              538 -> 569    +31
  repeated                 64 ->  67     +3
  declarable bottles        6 ->   7     +1
  comparable attributes    73 ->  75     +2
  declarable facts         16 ->  16      0
```

Read the STATED column only: the scratch database began with no inferred
attributions, so its PROPOSED figures fold in the whole of Experiment B.

**31 stated facts for $0.16.** Real, cheap, small — and 145 new claims
produced only 31 new facts, because most recovered descriptors are
values one person has used once. The singleton problem arriving by a
different road.

## Cross-bottle: the accounting was wrong by 1.6×

Enrichment was scored as money-on-X for facts-about-X. Of 348 claims
attached during run 2, **134 landed on a bottle that was not the
target**.

```
cost per direct claim   $0.0027
cost per total claim    $0.0017
```

19 bottles gained evidence without ever being a target (132 claims, 61%
of the direct yield). The beneficiaries are graph neighbours — same house
(Oajan → Althair, Layton, Haltane, Herod) or same comparison set
(Sauvage → Aventus, Qahwa → Khamrah) — and both relations are computable
before spending.

Two cases invert the per-bottle model outright:

- **Layton** returned zero when enriched directly for $0.1941, then
  gained 12 claims free from the Oajan run.
- **Creed Aventus** was never enriched and gained 41 claims, more than
  any target's direct yield except Oajan.
- **Khamrah/sweet**, the one newly answerable query, was produced this
  way.

**Not established:** that neighbourhood predicts yield well enough to
schedule on. Seven targets, relationships read off after the fact, and no
run designed to test a prediction.

## Recommendation

**1. Run the OpenAI embedding arm.** $0.000058, built, blocked only on a
credential. Nothing this cheap should stay unmeasured.

**2. Add Naxos, and verify the other six gate-clearing candidates**
($0.35 in canonical lookups). It is the only place where already-bought
evidence converts to a publishable bottle at near-zero marginal cost.

**3. Spend enrichment on sparse bottles, not dense ones.** 2.9× cheaper
per conversion, 7× the new facts. This is now measured rather than
assumed, and it is the reverse of scoring by how much a bottle is
"missing".

**4. Target the anchors that fail, through their neighbours.** The three
unanswerable comparisons all fail on a thin anchor. BR540's own reviews
have been read; the evidence about BR540 is in the comment sections of
what people compare it *to*. Design one run to test this prediction
before making it a policy.

**5. Do not spend on pair verification (A) or canonical metadata (C) for
their own sake.** A's ceiling is 22 pages and enrichment produces pair
edges as spillover; C supplies no note lists.

**6. Treat G as done.** Both recoveries are one-off and applied. There is
no recurring return.

## The true binding constraint, revised

It is no longer "87% of facts rest on one person" — that is true and it
is downstream. **The binding constraint is that the anchors of the
questions people ask are thin, and a bottle's own review section stops
paying once it has been read.**

Every failing comparison fails on its anchor. Every offline recovery
widens retrieval and leaves declaration untouched. The only lever that
moved a product question was buying new comments — and the cheapest
version of that was buying them from a *neighbour's* review section.

## Technical debt, carried deliberately

**The budget ledger cross-process race is fixed** (2026-08-16). Every
read-modify-write now runs under an exclusive `flock`, and the running
total is re-read from the file inside that lock rather than trusted from
the process's own cache. `Budget.reserve` commits an estimate before a
paid call and settles it to the real figure afterwards, by appending the
difference rather than editing the line.

Two things this does **not** claim. The post-hoc path — `record` and
`guard`, which learn a batch's cost only after paying it — still overshoots
by one batch; what changed is that the second process now sees the first
and stops instead of running on. And `reserve` is wired into no caller
yet: the concurrency fix applies to every existing path automatically,
but estimate-first protection is opt-in and nothing opts in.

Pinned by `TestConcurrentProcesses::test_the_lock_makes_a_second_process_wait`,
which holds the lock in the parent and asserts the child's reservation
lands only after release. The obvious version of that test — start two
processes, assert one loses — passes with the lock removed, and was
discarded for that reason.

**Instrumentation ran only on the failure path.** `density_before` and
`funnel` were assigned inside the `except BudgetExhausted` branch, so run
1 (which hit the cap) produced a correct funnel and run 2 (which did not)
recorded zeros for all seven bottles — banding a 46-fact Dior Sauvage as
"sparse" and emptying the segmentation the cohort existed to produce.
Fixed and tested on both paths; worth remembering as a class.

**4% of claims are duplicates** on `(comment, subject, type, span)`.
Small, inflates every count here by about that much, not fixed
mid-experiment.
