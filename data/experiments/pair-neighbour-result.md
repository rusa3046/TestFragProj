# Result — neighbour acquisition for a pair gap

Run 2026-08-16T21:44:43+00:00. Design frozen in `data/experiments/pair-neighbour-prediction.md` before any spend.

## FAIL

INVALID — arm A was truncated at its ceiling ($0.0828) rather than running to a natural stop. It did not receive the budget the frozen design promised, so neither arm is scored. Re-run with the ceiling the design states.

## Frozen shortfall (caps the primary metric)

```
  Armaf Club de Nuit Imperiale           1p/1c  needs 2p/1c
  Maison Alhambra Delilah                2p/1c  needs 1p/1c
  Maison Francis Kurkdjian Baccarat Roug 1p/1c  needs 2p/1c
  Parfums de Marly Delina Exclusif       3p/1c  needs 2p/2c  flanker
  Parfums de Marly Delina La Rosée       1p/1c  needs 4p/2c  flanker
  Swiss Arabian Rose 01                  2p/2c  needs 1p/0c
```

## Primary — capped progress toward the gate, per dollar

```
                  people  creators  progress        $    per $
A  direct              3         3         6   0.0828     72.4
B  neighbour           3         0         3   0.0988     30.4
```

Independent creator added on — A: ['Maison Francis Kurkdjian Baccarat Rouge 540', 'Parfums de Marly Delina Exclusif'], B: none

Both arms are scored against the identical frozen shortfall, with no deduction for what the other earned, so the comparison measures the two acquisitions rather than the order they ran in.

## Secondary — sequential marginal yield

What each arm added *given* the corpus the previous one left. Real, and a different question from the one above.

```
                  people  creators  marginal
A  direct              3         3         6
B  neighbour           0         0         0
```

## Secondary — graph yield, and raw versus usable

```
                  raw claims  uncapped  partners  any bottle  comments
A  direct                 14         6         0          22       443
B  neighbour              11         5         0          35        16
```

`raw claims` is what extraction produced; `progress` above is what the gate can use. The distance between them is the point.

Anchor pages before: 0, after: 2
Pairs clearing the gate — before 0, after 2
Stopped because — A: arm-ceiling, B: arm-ceiling
Total spend: $0.1816

---

## Post-run diagnostic

Written after reading the data. The verdict above stands unchanged: the
run is **INVALID** and no winner is inferred from it.

### Why it is invalid, precisely

Both arms stopped on their own ceiling rather than on a natural bound, and
they did so having received wildly unequal data:

```
                    comments bought   mentioning Delina
A  direct                       779                 270
B  neighbour                     79                  14
```

Arm A read roughly ten times what arm B did. The $0.10 ceiling was
enforced for the first time in this experiment, and it bound arm A partway
through its first video's comments and arm B almost immediately after.
Comparing them would measure the interruption, not the hypothesis. That is
the case the INVALID rule was written for, and it fired correctly.

The per-arm `usd` figures in the table above are also unreliable: arm A
reports $0.0828 and arm B $0.0988, while the ledger moved $1.1616 →
$1.3694, a true total of **$0.2078**. When `guard` raises mid-batch the
accumulated figure in `trial.usd` is left partial, so the ledger is the
authority and the per-arm split is not trustworthy. A separate defect,
found by this run, and it must be fixed before a re-run — a per-dollar
metric cannot rest on a per-arm dollar figure that is wrong.

### What the evidence looked like, arm by arm

The funnel from what extraction produced to what the gate can use:

```
                       raw naming   pair-shaped   passing        usable pair
                           Delina                 provenance     evidence
A  direct                     185            51          175              14
B  neighbour                   10             9            8               5
```

Two things worth keeping from this, neither of which decides the frozen
hypothesis:

**The neighbour's evidence is pair-shaped, as the BR540 diagnostic
predicted.** 9 of arm B's 10 claims naming Delina are comparisons, against
51 of 185 for the direct arm. Buying a dupe's comment section really does
buy edges rather than descriptions. That was the reasoning behind this
experiment and it survives.

**Raw volume still collapses on its way to the gate.** Arm A turned 185
raw claims into 14 usable pair claims — a 13:1 loss, almost all of it at
resolution rather than provenance (175 of 185 passed the provenance gate;
only 14 had *both* ends resolved to catalogued bottles). The distance
between "extracted" and "usable" is the thing the capped metric exists to
expose, and it is large.

### What the run did produce

Not a verdict, but a real product outcome. Delina went from **0 comparison
pages to 2**:

```
Parfums de Marly Delina | Parfums de Marly Delina Exclusif   6p/3c   (flanker bar: 5p/3c)
MFK Baccarat Rouge 540  | Parfums de Marly Delina            6p/2c
```

The first cleared the *flanker* bar, which is the higher one. The frozen
query — "does any Delina pair clear the publishing gate" — is now answered
yes. It was answered by a run that cannot say which arm deserves the
credit.

### What has to change before a re-run

1. **Fix the per-arm spend accounting** so `trial.usd` survives a
   mid-batch raise. A per-dollar primary metric is meaningless otherwise.
2. **Raise the per-arm ceiling, or lower `max_comments`,** so an arm can
   reach a natural stop inside its budget. At 779 comments and $0.46–0.51
   per thousand, $0.10 buys roughly 200 comments; the frozen
   `max_comments = 400` needs about $0.20 an arm.
3. Re-freeze as a **new** prediction file. This one is spent: its baseline
   no longer exists, because this run moved it.
