# How to label a comment

Keep this open while you fill in `labels-blind.json`.

## What you're doing, and why

The system reads a YouTube comment and writes down the assertions it thinks
the commenter made. It has no idea whether it's any good at that. **You are
writing the answer key.** For each comment you decide what a careful human
says it asserts; the scorer then compares the machine's output against yours
and reports precision and recall.

That number is the only thing standing between this project and prompt
changes made on vibes. It has already cost the project once: a prompt edit
that looked obviously correct raised run-to-run variance sixfold and broke
evidence verification, and it took three runs and a revert to notice.

You are labelling 15 comments as a **calibration set**: enough to check
whether Opus's drafts are trustworthy enough to lean on for the other 35.

## What a claim is

**One thing a person said about one fragrance.**

Not a summary, not your opinion, not what's true — what the *commenter
asserted*. If someone writes "Khamrah smells just like Angels Share", they
asserted one claim, and it stays a claim even if they're wrong.

A claim has four parts:

| Part | Meaning |
|---|---|
| `claim_type` | Which kind of assertion (list below) |
| `raw_subject_text` | **What's being talked about** — the fragrance, in their spelling |
| `raw_object_text` | **What it's being compared or attributed to** — or `null` |
| `sentiment` | `POSITIVE`, `NEGATIVE`, or `NEUTRAL` — how they frame it |

Subject and object are copied **verbatim**. If they wrote `BR540`, you write
`BR540`. Not "Baccarat Rouge 540". Fixing spellings is a separate step of the
pipeline and it needs to see what people actually type.

## The claim types

**The three that matter most** — these become the product. Given a
fragrance, the site answers "what else smells like this" by counting these:

- **`DUPE_OF`** — subject is a cheaper stand-in for object.
  *"Zara Red Temptation is a dupe of BR540"*
- **`SIMILAR_TO`** — subject smells like / reminds them of object.
  *"Khamrah is basically Angels Share"*
- **`BETTER_THAN`** — subject is preferred over object.
  *"Quaeed Lattafa is ten times better"*

**Describing one fragrance** (object is a word or phrase, not a bottle):

- **`NOTE_DESCRIPTOR`** — what it smells of. *"a lime twist"* → `lime`
- **`OCCASION`** — where you'd wear it. *"great for weddings"* → `weddings`
- **`AESTHETIC`** — the vibe. *"very old money"* → `old money`

**How it behaves on skin** (no object — set `raw_object_text` to `null`):

- **`LONGEVITY`** — how long it lasts
- **`PROJECTION`** — how far it throws
- **`DEVELOPMENT`** — how it changes over the wear

**Product-level:**

- **`REFORMULATION`** — a new version differs from the old (no object)
- **`UNMET_PRODUCT_REQUEST`** — wants a form that doesn't exist.
  *"wish it came in a body lotion"* → `body lotion`

## The procedure

For each comment, three questions:

1. **Is a specific fragrance named?** No name anywhere → almost certainly
   `[]`. Move on.
2. **Does the comment say something *about* that fragrance?** Buying it,
   asking about it, or liking it is not a claim about how it smells.
3. **For each thing it says: which type, what's the subject, what's the
   object?** One assertion = one claim. Two assertions = two claims.

**`"claims": []` is the most common correct answer.** Roughly two-thirds of
your 15 should end up empty. Leaving one empty is a real judgement, not a
skip — it tells the scorer the machine should have found nothing there.

## Worked examples

All real comments from your corpus.

---

**"The Lattafa Maahir Legacy is so good! It's Sedley with a lime twist. I love it! A must buy for $32 bucks"**

Two claims. The fragrance is named, and two separate things are said about it.

```json
[
  {"claim_type": "SIMILAR_TO", "raw_subject_text": "Lattafa Maahir Legacy",
   "raw_object_text": "Sedley", "sentiment": "POSITIVE"},
  {"claim_type": "NOTE_DESCRIPTOR", "raw_subject_text": "Lattafa Maahir Legacy",
   "raw_object_text": "lime", "sentiment": "POSITIVE"}
]
```

`"$32 bucks"` is a price, and `"so good"` is praise with no content. Neither
is a claim.

---

**"I got Khamrah today and I smelled Angel Share in store and for me they really are similar, about 75 percent, but I don't liek any as I don't like sweet fragrances hahaha"**

One claim. Both fragrances are named and the comparison is explicit.

```json
[
  {"claim_type": "SIMILAR_TO", "raw_subject_text": "Khamrah",
   "raw_object_text": "Angel Share", "sentiment": "NEUTRAL"}
]
```

`NEUTRAL`, not positive — they're saying the two are alike while disliking
both. `"I don't like sweet fragrances"` is about the *person*, not about a
fragrance, so it's not a claim.

---

**"It smells amazing. Only downside is that you can't wear this in small closed areas or in the summer."**

```json
[]
```

Nothing is named. `"It"` and `"this"` refer to the video's fragrance, which we
don't store — so these can never be attached to a bottle. Empty.

---

**"Yeah but khamrah isn't a clone it's more of a inspired fragrance and it's smells better than kismet angel imho"**

One claim, not two.

```json
[
  {"claim_type": "BETTER_THAN", "raw_subject_text": "khamrah",
   "raw_object_text": "kismet angel", "sentiment": "POSITIVE"}
]
```

`"isn't a clone"` is a **denial**. They are refusing the DUPE_OF claim, not
making it — do not record it. Note `khamrah` stays lowercase: their spelling,
not yours.

> **Why your labels have no denials in them but the database does.** The
> extractor now records denials explicitly, as a claim with
> `polarity: "DENIED"`. That is not a contradiction of the rule above: a
> denied claim is excluded from the graph *and* from scoring, so leaving it
> out of your labels and the extractor marking it DENIED are the same
> answer arrived at two ways. Keep dropping them.
>
> This exists because 36 of the corpus's 499 similarity claims were denials
> stored as assertions — *"it is nothing like angel share"* filed as a dupe
> edge, which would have quoted that person as evidence for the claim they
> rejected.

---

**"Lattafa debuted a new version of their OG Khamrah scent called 'Qahwa'... it's basically Lattafa Khamrah but with an additional coffee note."**

```json
[
  {"claim_type": "SIMILAR_TO", "raw_subject_text": "Qahwa",
   "raw_object_text": "Lattafa Khamrah", "sentiment": "NEUTRAL"},
  {"claim_type": "NOTE_DESCRIPTOR", "raw_subject_text": "Qahwa",
   "raw_object_text": "coffee", "sentiment": "NEUTRAL"}
]
```

The subject is written as `"it"`, but `Qahwa` is named earlier in the same
comment — so use `Qahwa`. This is the pronoun rule's other half.

---

**"Fire Your Desire - Emir by Paris Corner is an even better impression of Angels' Share by Kilian. Packaging is even more impressive than the Original Fragrance."**

```json
[
  {"claim_type": "DUPE_OF", "raw_subject_text": "Fire Your Desire - Emir by Paris Corner",
   "raw_object_text": "Angels' Share by Kilian", "sentiment": "POSITIVE"}
]
```

`"impression of"` means dupe. The second sentence is about **packaging**, not
smell — there's no claim type for that, and inventing one would be wrong.

---

**"How can a man wear this, it's smells like stronger with you... Smelt like a cinnamon roll pie. Quaeed Lattafa is ten times better"**

```json
[]
```

Harsh but correct. `"this"` is never named, so the SIMILAR_TO claims have no
usable subject. `"Quaeed Lattafa is ten times better"` has a named *subject* —
but the thing it's better *than* is the same unnamed `"this"`, so the object
is unusable too. Both ends have to be attachable.

## Traps

| Looks like a claim | Why it isn't |
|---|---|
| *"isn't a clone of X"* | A denial. They're rejecting the claim, not making it. |
| *"nothing like X"*, *"not even similar to X"* | Same — a denial, however phrased. |
| *"is this similar to X?"* | A question. Nothing is asserted. |
| *"What about X as a clone of Y?"* | Still a question, even with the comparison inside it. Asking is not claiming. |
| *"if X smells like Y then I'll stay away"* | A hypothetical. They have not said it does. |
| *"I bought Khamrah today"* | About the purchase, not the smell. |
| *"I don't like sweet fragrances"* | About the person, not a fragrance. |
| *"$32 bucks"*, *"packaging is nicer"* | Price and packaging aren't smell. |
| *"link in bio"*, *"first!"*, *"great video"* | Nothing at all. |

**Hedged claims still count.** *"It's supposed to be a ysl tuxedo clone"* is
a real DUPE_OF — they're reporting it as true. Hedging affects the
extractor's `confidence` field, which labels deliberately don't carry.

## The pronoun rule

The drafts were made under **`skip`**, so apply the same rule or the
disagreement you measure will be your own drift rather than the model's error:

- Fragrance named **elsewhere in the same comment** → use that name.
- Named **nowhere** in the comment → **do not record the claim.**

The second half feels wasteful, and it removes a lot. It's deliberate:
entity resolution cannot turn `"It"` into a bottle, so such a claim can never
become an edge. Counting it as correct would reward the extractor for output
that dies two steps later.

## When you're done

```bash
uv run python -m fragrance_graph.evals.labels import labels-blind.json --labeler aanya
uv run python -m fragrance_graph.evals.autolabel agreement --human aanya
```

Import replaces by labeler, so re-importing after a fix overwrites cleanly.
If the file has a JSON syntax error the import fails before writing anything
— nothing ends up half-loaded.

## A house named where a bottle belongs

Real comment:

> Bad advice this is straight ass. There's no clone of TF oud wood that
> captures the scent. I tried this brand, Maison Alhambra's, and Afnan and
> all were very offputting

The drafter produced two `DUPE_OF` claims with `Maison Alhambra` and
`Afnan` as subjects. Both are houses, and neither is a bottle — the
commenter means "Maison Alhambra's oud wood clone" and elides the name.

**Label it anyway, as a denial.** Two `DUPE_OF` claims against
`TF oud wood`, both `DENIED`, sentiment `NEGATIVE`, subjects left as the
commenter wrote them.

Three reasons, in the order they matter:

1. **The denial is the part that can go wrong.** "There's no clone that
   captures the scent" recorded as an assertion puts this person's name
   behind the opposite of what they wrote. That is the defect `polarity`
   exists to prevent, and it is the only one of the three that is scored.
2. **A house subject cannot be recorded.** Labels carry no `subject_kind`,
   and `match_key` is `(comment, claim_type, subject, object)`. There is
   nowhere to put "this is a house", so leaving the text as written is the
   only available answer, not a compromise.
3. **A denial is never an edge.** `is_edge` requires `ASSERTED`, so a
   `DENIED` claim cannot reach a page whatever its subject is. Recording it
   costs nothing downstream.

**Do not answer this one with "asserts nothing".** The comment asserts a
great deal; it just asserts the negative. Marking it empty teaches the eval
that silence is correct here, and silence loses the denial altogether —
which is how the corpus ended up with 36 of them stored as assertions.

The general rule: **label the relationship the commenter is talking about,
then say whether they affirm or reject it.** Do not drop a claim because
its subject is imprecise. Imprecision fails later, at entity resolution,
visibly. A missing denial fails silently.
