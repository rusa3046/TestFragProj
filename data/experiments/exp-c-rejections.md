# Experiment C — schema-rejection recovery

308 claims sit in `rejected_claims`. The cohort saw 79 of them, ~12% of
what it emitted. This asks what they are and whether the prompt and the
schema disagree.

## Every rejection, by exact reason

```
179  NOTE_DESCRIPTOR allows object_kind {TAG}, got NONE
 22  BETTER_THAN allows object_kind {FRAGRANCE}, got NONE
 17  NOTE_DESCRIPTOR requires raw_object_text
 13  BETTER_THAN requires raw_object_text
 12  DUPE_OF allows object_kind {FRAGRANCE}, got NONE
 11  BETTER_THAN allows object_kind {FRAGRANCE}, got TAG
  9  AESTHETIC allows object_kind {TAG}, got NONE
  9  BETTER_THAN allows object_kind {FRAGRANCE}, got HOUSE
  7  OCCASION allows object_kind {TAG}, got NONE
  7  DUPE_OF allows object_kind {FRAGRANCE}, got HOUSE
  6  SIMILAR_TO allows object_kind {FRAGRANCE, HOUSE, TAG}, got NONE
  5  DUPE_OF requires raw_object_text
  3  OCCASION requires raw_object_text
  3  SIMILAR_TO requires raw_object_text
  2  DUPE_OF allows object_kind {FRAGRANCE}, got TAG
  3  LONGEVITY / PROJECTION given an object that takes none
```

Classified against the categories asked for:

| category | count | share |
|---|---|---|
| **prompt/schema disagreement** | 215 | 70% |
| legitimately invalid evidence | 90 | 29% |
| taxonomy mismatch | 3 | 1% |
| malformed value, unsupported type, polarity, parser defect | 0 | — |

## The one genuine disagreement

All 215 are the same shape: a tagged type (`NOTE_DESCRIPTOR`, `OCCASION`,
`AESTHETIC`) emitted with `object_kind: NONE` and no object text.

**The value was not missing. It was in the wrong field.** Reading the
payloads:

```
evidence_span "It's woodsy"                      raw_object_text null
evidence_span "it's spicy like ginger"           raw_object_text null
evidence_span "Gold is a powdery gourmand"       raw_object_text null
evidence_span "I find that the extrait has a Tobacco scent to it"   null
```

The model found the claim and quoted it correctly. The prompt listed
notes as `TAG` under "Kinds" but never said these types *require* an
object, and the model resolved the ambiguity by putting the descriptor in
the evidence span alone.

**Fixed in the prompt, not the schema.** The prompt now states the
requirement and shows where the value goes. The schema is unchanged and a
test pins that it still refuses an objectless descriptor — accepting one
would raise the acceptance rate while recording that a comment mentioned
a smell without recording which, which is a fact with nothing in it.

## The other 90 are correct refusals

Every `BETTER_THAN`/`DUPE_OF`/`SIMILAR_TO` rejection with `NONE` was
checked: **all 34 have no object text either**. These are "it's a clone"
with no target named. Unusable, and refusing them is right.

`BETTER_THAN ... got HOUSE` (9) and `got TAG` (11) are a taxonomy
question rather than a defect — "better than Dior" compares against a
house. Recording it would need a claim type that admits a house on the
object side, which is a schema change with real design consequences and
is not made here.

## Replay result

Replayed the 272 stored comments that produced a rejection. No comments
bought; extraction only, and `write_claims` was never called, so the
corpus was not touched.

```
                        original      replay
claims emitted               ~308         333
accepted                        —         301
rejected                      308          32
rejection rate                  —        9.6%
NOTE_DESCRIPTOR w/ value        0         183
tagged-type rejections        215           0
```

**The dominant rejection class went to zero and 183 descriptors came
back with their values.** Every one of the 32 remaining rejections is a
pairwise type with no object — the legitimately-invalid kind.

Cost: $0.26 across the pilot and the full replay, from stored comments.

## Coverage gains, measured

Persisting the recovered claims means *replacing* the existing ones for
those comments, not stacking on them, which is a destructive write. Done
in a scratch database built by `corpus import` — the pattern
`reset_extraction`'s own docstring recommends — with an assertion that
refuses to run against any URL without "scratch" in it. The working
corpus was not touched.

200 comments replaced, against the committed corpus:

```
claims on those comments      92 ->  237    +145
rejected claims              229 ->   23    -206

STATED (the clean measure)
  facts                      538 ->  569     +31
  repeated (2+)               64 ->   67      +3
  supported                   16 ->   16       0
  declarable facts            16 ->   16       0
  declarable bottles           6 ->    7      +1
  comparable attributes       73 ->   75      +2
  answerable 'less X'          2 ->    2       0
```

**Read the STATED column only.** The scratch database began with no
inferred attributions, so its PROPOSED figures (facts 538 → 751) fold in
the whole of Experiment B and cannot be credited to this fix.

So schema recovery is worth **31 stated facts, 3 of them repeated, and
one more bottle with a declarable fact** — for $0.16 of replay against
comments already bought. Real, cheap, and small. It does not move
declarable facts or any relative comparison, which by now is the
expected shape: every offline recovery widens the corpus and leaves the
gate where it was.

The 145 new claims are far more than the 31 new facts, because most
recovered descriptors are values only one person has ever used. That is
the same singleton problem enrichment has, arriving by a different
route.

The 183 recovered descriptors are mostly good and not uniformly so:
`Khamrah Dukhan -> tobacco`, `Layton -> candy`, `Qahwa -> sweet` are
usable; `Senorita perfume -> mosquitoes` and `Just -> fruity pebbles` are
the extractor being literal about odd comments. Normalisation handles
some of that, and a recovered claim is still a claim that has to survive
the same evidence ladder as any other.
