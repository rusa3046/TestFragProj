# Experiment D — catalogue-gap recovery

The floating classification said half of all unattached evidence names a
fragrance the 78-item catalogue lacks, and I called it "the larger
prize". Ranking it by stranded evidence shows that was wrong, and shows
why in a way the aggregate could not.

## First, the bucket was contaminated

Ranking the candidates put **`este perfume` second** with 17 claims,
`esta fragancia` twelfth, and `el perfume`, `esto`, `ini` and `con này`
in the top 25.

Those are Spanish, Portuguese, Indonesian and Vietnamese for "this
perfume". `attributes.DEICTIC` is English-only — correct for a resolver
that must not guess — so every non-English pronoun was being counted as
a fragrance worth going out and acquiring.

Fixed by adding `FOREIGN_DEICTIC`, every entry an exact translation of a
form already in the English list, so the original caution is untouched:
none of them names a bottle. `attach_by_video` now gates on the union.
The classifier defers to the same constant rather than keeping its own
list, because the two had already drifted — `esse perfume` was on the
acquisition list while the attachment rule was treating it as a pronoun.

Effect on Experiment B, offline and free:

```
                    B (before)   B (multilingual)
attributions               398                475
floating claims           2139               2065
facts (proposed)           803                844
supported                   25                 28
comparable attributes       99                100
```

## The ranked gap, after cleaning

1,178 claims name something the catalogue lacks, across **762 distinct
names**. Labelled by what they actually are:

```
902  plausible new bottle
105  flanker of a bottle we hold      ("Exclusif", "Paradis", "the original")
 75  probably a bottle we hold        ("Liquid Brun de la casa de French")
 36  a house, not a fragrance         ("Creed", "Lattafa", "pdm")
 23  noise                            ("Mine", "these", "the drydown")
 19  a concentration                  ("MFK Extrait", "the eau de parfum")
```

## The result: a long tail, not a prize

```
plausible candidates with >=3 claims        44      184 claims
of those, clearing the publishing gate       7       47 claims
```

The gate is 3 commenters and 2 distinct creators. **Only 7 candidate
bottles clear it**, and they carry 47 claims between them — 4% of the
1,178 that looked recoverable, and about 1% of the corpus.

```
candidate                 claims  people  creators  gate
Naxos                         23      18         4  YES
La Roseé                       6       6         3  YES
Cloud                          4       4         3  YES
Vulcan Feu                     4       4         2  YES
hawas fire                     7       7         1  no
BDC                            7       5         1  no
Diplomat by Amazing Creation   7       1         1  no
Fievre Verte                   6       1         1  no
```

The 1,178 claims are spread across 762 names at an average of 1.5 claims
each. Adding an entity recovers its own claims and nothing else, so the
economics are per-bottle and the tail is worthless: 700-odd bottles would
have to be researched and verified to recover one or two claims apiece.

**Naxos is the exception and worth doing on its own.** 23 claims from 18
commenters across 4 creators is a bottle that arrives already past the
publishing gate — better evidence than most of the 78 the catalogue
already holds, and it costs nothing to attach because the comments are
bought.

## Identity is not decided here

Fuzzy matching generated and *suppressed* candidates; it decided nothing.
Every name above needs a person or a canonical source to confirm what it
refers to, and the classes the labelling separates are exactly the
confusions that matter:

- **house vs fragrance** — "Creed" is not a bottle
- **flanker vs base** — "Exclusif" is a different product from its base
- **concentration vs product** — "MFK Extrait" is not "Baccarat Rouge 540"
- **similar names** — "Devil's Share" is not "Angels' Share", and a 0.72
  fuzzy cutoff pairs them

No entity was added to the catalogue. Doing so requires confirming
identity, and the one reachable permitted source charges $0.05 a lookup —
$0.35 to verify the seven that clear the gate. That is a spending
decision, not a measurement, and it is left for a person.

## What this does to the strategy comparison

Catalogue expansion is **not** the large offline win the floating
histogram implied. It is one clearly good acquisition (Naxos), a handful
of marginal ones, and a very long tail that is not worth researching.
