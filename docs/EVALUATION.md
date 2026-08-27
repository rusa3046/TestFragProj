# How this project is evaluated, and where the measurement is weakest

An audit of the eval layers as they stand at `c42527b`, and a ranked
argument about where the next hours of eval work should go. Nothing here
changes behaviour; it is the reasoning a later change should be able to
point back at.

## What exists

Five layers, each answering a different question. They are listed in the
order a defect would pass through them.

| Layer | Question | Gate |
|---|---|---|
| `tests/` (~40 modules) | does each function do what it says | `checkpoint.sh` step 2 |
| `evals/score.py` | did the model read the comment correctly | manual |
| `audit.py` | does every surface cite what it claims to | `checkpoint.sh` step 3 |
| `evals/recommend.py` | does the product answer without overclaiming | `checkpoint.sh` step 4 |
| `evals/cards.py` | did the sentence a shopper reads change | `checkpoint.sh` step 5 |
| `evals/fuzzy.py` | does retrieval pull the opposite of what was asked | manual |

Four design choices in here are worth naming, because the recommendations
below are mostly about applying them more evenly rather than replacing
them.

**One metric outranks the rest, explicitly.** `evals/recommend.py` fails a
run on a single unsupported assertion and treats recall as diagnostic;
`evals/fuzzy.py` ranks `forbidden@k` above `recall@k`. Both encode the
same judgement — a confident wrong answer is a different kind of failure
from a missing one — in the exit status, not in a comment.

**Refusing is scored as correct.** `answerable: false` rows make silence
the passing answer. An eval that scored those the other way would reward
exactly the behaviour the SPEC exists to prevent.

**Adversarial negatives are first-class.** `fuzzy.py`'s forbidden terms
caught a corpus-derived embedder that connected `airy` to `heavy`. The
good pairs alone would have shipped it.

**Receipts are re-derived, not trusted.** `unmet_constraints` rebuilds
each constraint check from the candidate's own cited reasons instead of
reading `candidate.matched`, because `_score` writes that string itself —
so a regression in `_satisfies_hard` would have been certified by the code
that caused it.

That last one is the single best idea in the eval suite, and it is applied
in one place. Most of what follows is an argument for applying it in a
second.

## The gaps, ranked by what I would fix first

### 1. The unsupported-assertion check never inspects a declarable reason

`run_case` skips them outright:

```python
for reason in candidate.reasons + candidate.caveats:
    phrase = reason.phrase()
    if reason.declarable:
        continue
```

`Reason.declarable` is `strength.may_declare and not inferred`. So the
primary metric of the primary gate polices the *renderer* — did weak
evidence get worded strongly — and takes the *strength computation* on
faith. A regression that let a two-person fact clear `may_declare`
produces a flat, unhedged, entirely confident sentence, and the benchmark
scores it zero unsupported. The check is structurally blind to the half of
the pipeline that decides what may be declared at all.

This is the `candidate.matched` mistake in a new place: the same code that
would make the error also writes the field the eval reads to detect it.

**Fix, in the shape the eval already uses elsewhere:** for every
*declarable* reason, re-derive the people and channel counts from
`reason.claim_ids` against the database and assert they genuinely clear
the declaration bar. Cheap — the claim ids are already carried on the
reason — and it closes the gate's largest hole. This is the one item here
that is hours of work rather than evenings, and it protects the metric the
whole project is gated on.

### 2. Extraction has no statistical power, and it is the binding constraint

`sample.py` states the problem plainly: 13 hand-verified train comments
against a ±1 claim noise floor. The holdout batch is no better —
`eval-batch.json` holds 35 reviewed comments, and **27 of them assert
nothing**. Eight informative rows, carrying on the order of fifteen
scoreable claims.

At that size a single claim moves F1 by roughly five points. So the number
cannot distinguish a real prompt improvement from drift, which is the
precise failure the eval was built after — a prompt edit that raised
variance sixfold and took three runs and a revert to notice.

Two changes, in order:

- **Label to a claim target, not a comment target.** Roughly 150–200
  scoreable claims, drawn through `sample.py`'s existing strata so the
  hour buys discrimination rather than empty lists. The `control` stratum
  stays, and `coverage` keeps reporting the split, so it is still
  possible to say which numbers estimate the corpus and which estimate the
  hard cases.
- **Print an interval, not just a point.** `score.py` should report a 95%
  bootstrap CI on F1 alongside the estimate — resampling *comments*, not
  claims, since claims within one comment are not independent. Then
  "0.71 → 0.74" is visibly not a result, and nobody has to remember that
  it isn't.

The second is an afternoon and makes the first's absence legible in the
meantime, which is a good reason to do it first.

### 3. Nothing measures whether the answer key is right

`autolabel.py` gets this more right than most projects: drafts import
under their own labeler, and blind calibration on 15 comments earns the
right to lean on them. But that is a one-shot gate, and there is no
recorded agreement figure, no re-calibration cadence, and no ceiling
reported next to the score.

**Fix:** keep a permanent double-labelled slice — 20 or so comments held
under two independent human passes — and report agreement on every eval
run. That number is the ceiling. An F1 of 0.82 against labels whose
annotators agree 0.80 is measuring annotation noise, and the honest way to
prevent that reading is to print both numbers on the same screen.

### 4. `DISCLOSED_COUNTS` accepts a sentence on the strength of a substring

`discloses()` passes any phrase containing "N people across M channels"
anywhere in it. The reasoning behind it is sound — a stated head count
discloses as completely as a hedge word, and the first version of the
check flagged thirteen honest sentences for lacking one. But the test is
positional-blind:

> "the definitive dupe — 2 people across 1 channel compared this with Delina"

discloses and overclaims in the same breath, and passes.

The `HEDGES` allowlist has no equivalent problem: new wording without a
hedge fails, which is the safe direction. **Fix is small:** run the
existing `OVERCLAIMS` list against reason phrases too, not only against
`answer.note`. The vocabulary is already written.

### 5. The commit gate does not gate on the pass rate

`evals/recommend.main` returns non-zero only on `unsupported`.
`checkpoint.sh` prints the `passed overall` line and ignores it. So a
parsing regression that drops a case from pass to fail — intent
misparsed, anchor lost, a hard constraint returned without evidence —
prints "21/22" and commits.

CLAUDE.md already treats 22/22 as the expected state (it is how a
stale-derived-table clone is diagnosed). The gate should enforce what the
docs already assume.

**Fix:** ratchet. Store the expected pass count and fail on a decrease,
separately from `unsupported == 0`. Raising it is a deliberate edit;
lowering it requires saying so out loud in a diff.

### 6. The card golden measures drift, and 818 lines of it get skimmed

This is not a design flaw — `cards.py` is explicit that it asserts the
cards are what a person last approved, not that they are good, and that
framing is correct. The practical problem is volume. Any intentional
wording change rewrites a large fraction of an 818-line file, and a large
diff reviewed at the end of a long change is reviewed the way large diffs
are. That is the exact conditions under which the seventh defect of that
week's six lands.

**Fix:** keep the golden for drift and add a handful of *invariants* over
rendered cards, which is the six defects generalized rather than
enumerated:

- no card cites a note and its own negation ("less sweet" as evidence for
  sweet)
- every note sentence carries at least one claim id
- no chip contributes to a score twice
- no declared-note fact outranks a community fact of greater strength

An invariant survives a rewording; a snapshot does not. The two are
complementary, and only one of them keeps working after `--update`.

### 7. Nothing scores the corpus-level claim the SPEC is built on

"31 people called this a dupe of BR540" is the product. No layer measures
whether the pipeline finds the dupe pairs the corpus actually contains —
`score.py` measures per-comment extraction, `recommend.py` measures the
answer's honesty given whatever was extracted. A corpus regression that
silently halved edge recall (the quiet retail-import failure in CLAUDE.md
is one such shape) passes every gate now.

Building a gold pair list from the extractor's own output is circular, so
the honest version is small and manual: two dozen pairs found by reading
raw comments, checked in, scored end-to-end. Twenty-four rows is not a
recall estimate, and it should not be reported as one. It is a smoke
alarm, and there is currently none.

### 8. Ranking is unscored

`hard_ok` / `soft_ok` / `answered` are set-membership tests. Order is the
product's other half — stage 5 exists to break ties by independence — and
no case asserts it. NDCG here would be false precision against 22 rows.
A `must_outrank: [["A", "B"]]` field in the case file costs almost
nothing and catches the regression that matters.

## What I would actually do, in order

1. **(§1) declarable re-derivation** — hours, closes the biggest hole in
   the gate everything else is committed through.
2. **(§5) ratchet the pass count** and **(§4) run `OVERCLAIMS` over
   phrases** — an afternoon together, both purely additive.
3. **(§2) bootstrap CI in `score.py`** — an afternoon, and it makes the
   labelling shortfall visible on every run instead of only in a docstring.
4. **(§2) label to ~150–200 claims** and **(§3) the double-labelled
   slice** — evenings, and the ceiling on every extraction decision until
   they are done.

§6 and §7 are worth doing and are not urgent. §8 is worth doing when
ranking next changes.

The pattern in the first three: this suite's best instinct — do not let a
component certify its own work — is currently applied to hard constraints
and nowhere else. Applying it to declaration strength, to the pass rate,
and to the disclosure check is most of the value available, and none of it
requires a new eval layer.
