# Where pair evidence is lost — the Delina run, decomposed

Free diagnostic over the completed (and permanently INVALID) Delina run.
No spend, no policy change. The question is *why* pair-shaped claims fail
to become usable pair evidence, not how to make the number bigger.

## The funnel, per arm

```
                              A direct    B neighbour
raw claims naming Delina           179              8
  -> pair-shaped                    47              7
  -> provenance-valid               47              7
  -> both endpoints resolved        14              5
  -> usable independent pair ev.    14              5
```

Two things stand out before any classification.

**Provenance costs nothing here.** 47 of 47 and 7 of 7 pair-shaped claims
passed `polarity = ASSERTED AND evidence_verified = 1`. The extraction is
not producing unquotable or denied comparisons; whatever is lost is lost
later.

**The entire loss is entity resolution.** A direct: 47 → 14, and of the 33
that fell out, **31 had exactly one end unresolved** and 2 had neither.
Delina always resolves — it is the anchor — so in every case it is the
*other* bottle that could not be named.

## Why the other end failed — all 33 classified

Read by hand from the claim rows, checked against the catalogue rather
than from memory.

| cause | A | B | total |
|---|---:|---:|---:|
| **second fragrance absent from catalogue** | 14 | 2 | **16** |
| **ambiguous entity** (a reference, not a name) | 9 | 0 | **9** |
| **resolvable alias** (catalogue has it, resolver missed it) | 5 | 0 | **5** |
| category / not a fragrance | 2 | 0 | 2 |
| flanker / concentration distinction | 1 | 0 | 1 |
| extraction or parser defect | 0 | 0 | 0 |

### absent from catalogue — 16

`cassili` ×2, `rouge malachite` ×2, `club de nuit maleka`, `dg
l'imperatrice`, `elinah by paris corner`, `fashionably london by zara & jo
malone`, `hipnotic poison`, `hypnotic poison edt by dior`, `miss dior in
bloom`, `nirvana white`, `souvenir floral bouquet by afnan`, `your the
one`, `zimaya fatima pink`, `delina valaya`.

The long tail again, and mostly *singletons* — thirteen of the sixteen
appear once. Consistent with experiment F: 762 unresolved names at 1.5
claims each.

### ambiguous entity — 9

`the original` ×5, `exclusif` ×2, `it`, `either of its flankers`.

These are not failures of the catalogue. `the original` under a Delina
video means Delina; under an Aventus video it means Aventus. Resolving
them is the video-subject inference already measured and already confined
to `PROPOSED` — deliberately not stated evidence, and so deliberately not
gate-eligible. **This bucket is the provenance design working**, not a gap.

### resolvable alias — 5

`bacarat` ×2, `baccarat`, `atomic rose by initio`, `delilah by maison
alhambra`.

The catalogue holds every one of these bottles. Two patterns:

- **misspelling** — `bacarat`/`baccarat` for *Maison Francis Kurkdjian
  Baccarat Rouge 540*;
- **"<name> by <brand>"** — `atomic rose by initio`, `delilah by maison
  alhambra`. The resolver strips a leading brand but not a trailing
  `by <brand>`. The same shape appears in the *absent* bucket too
  (`elinah by paris corner`, `souvenir floral bouquet by afnan`), so
  handling it would help resolution generally.

**Not changed here.** Fixing the resolver to improve a number in a
diagnostic is how a measurement becomes a target. It is recorded as a
finding for its own review.

## Answering the question that was asked

> DIRECT: 51 pair-shaped → 14 usable. NEIGHBOUR: 9 pair-shaped → 5 usable.

(The 51 and 9 were counted before the provenance filter; with it the
figures are 47 and 7. The shape of the answer is the same.)

**Direct loses 70%; the neighbour loses 29%.** The difference is not a
different failure mode — it is the same one, at different rates:

```
                        A direct   B neighbour
absent from catalogue      14/33         2/2
ambiguous reference         9/33         0/2
resolvable alias            5/33         0/2
```

The direct arm's comments are people talking *about Delina* and reaching
for whatever else comes to mind — nine different houses, thirteen of them
mentioned once. The neighbour arm's comments are people comparing a dupe
to its original, so the second bottle is nearly always the anchor itself,
already catalogued.

That is a real structural observation about the two sources, and it is
**not** an A/B verdict: the arms bought 779 and 79 comments, and a rate
computed on 8 raw claims is not a rate.

## What this does not establish

- Nothing about direct versus neighbour efficiency. The run is INVALID.
- Nothing about whether the resolver *should* handle `by <brand>`. That
  needs its own review, on its own evidence, with the false-merge risk
  weighed — `resolve.names.best_match` already has a fuzzy threshold and
  loosening it is how *Devil's Share* becomes *Angels' Share*.
