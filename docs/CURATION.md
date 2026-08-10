# What you are approving

One question, per row:

> **Is this the bottle the commenters meant?**

That is the whole judgement. You are not deciding whether the brand is
correct in the world — the catalogue asserts that, and it is usually
right. You are deciding whether the *match* is right.

## Why it matters

A fragrance entry is what turns claims about strings into claims about
bottles. Approve a wrong one and every claim mentioning that word attaches
to the wrong fragrance — silently, permanently, and invisibly to the test
suite. It will show up as a quote from a real person on a page about a
bottle they were not talking about.

That is the worst thing this product can do, so the review exists.

## The failure that actually happens: flankers

Houses ship families. The catalogue knows all of them; a fuzzy search
returns whichever scores best.

| people wrote | the catalogue may offer | are they the same? |
|---|---|---|
| `Layton` | Layton **Exclusif** | **No.** Different bottle, different price |
| `Khamrah` | Khamrah **Qahwa** | **No.** Qahwa is the coffee flanker |
| `Club de Nuit` | Club de Nuit **Sillage** | **No.** People mean Intense Man |
| `Oud Wood` | Oud Wood **Intense** | **No.** |

**The rule for a bare mention: pick the plain one.** When commenters write
"Layton" with no qualifier they mean Layton, not Layton Exclusif. If they
wanted the flanker they would have named it — and when they do, it is a
separate mention and gets its own entry.

`alternatives` in the review file holds the next three catalogue rows, so
when the top match is a flanker you can usually correct it without another
lookup: copy the right `Name` and `Brand` up into `canonical_name` and
`brand`, then approve.

## Read `examples` first

Every row carries up to two real spans people wrote using that mention:

```json
{
  "mention": "Club de Nuit",
  "count": 7,
  "canonical_name": "Club de Nuit Sillage",
  "brand": "Armaf",
  "confident": false,
  "examples": [
    "club de nuit is the best aventus clone for the money",
    "I have club de nuit intense man and it lasts all day"
  ],
  "alternatives": [{"Name": "Club de Nuit Intense Man", "Brand": "Armaf", ...}],
  "approved": null
}
```

The examples settle it. People are talking about the Aventus clone, which
is Intense Man — so the top match is wrong, and the fix is in
`alternatives`. Without the quotes you would be guessing from a name.

## How to work through the file

1. **Sort your attention by `count`.** A mention appearing 83 times decides
   far more edges than one appearing twice. The file is already ordered
   this way.
2. **Read every row where `confident` is `false`.** Those carry
   `"note": "name differs from the mention — check this one"`. This is
   where a catalogue quietly attaches the wrong bottle.
3. **Skim the `confident: true` rows.** They are usually a formality —
   `Khamrah` → `Lattafa Khamrah` needs no thought. Flankers can still hide
   here, since `Layton` vs `Layton Exclusif` scores high.
4. Set `"approved": true` or `false`. Correct `canonical_name` and `brand`
   in place when the right answer is in `alternatives`.

## When in doubt, reject

The two errors are not symmetric.

- **Rejecting a good match** leaves the mention unresolved. It stays in
  `resolve.entities report`, visible, costing you one lookup to redo. You
  lose a few edges until you get to it.
- **Approving a wrong match** merges two different fragrances forever.
  Every edge touching either one is now wrong, nothing warns you, and
  finding it later means re-reading quotes one at a time.

This is the same asymmetry `names.py` encodes in `FUZZY_THRESHOLD = 0.88`:
*a false merge is worse than a miss.* Apply it here too. If the examples
do not make you confident, reject and move on.

## What you are *not* deciding

- **Whether the fragrance is good.** Irrelevant.
- **Whether the brand is spelled the house's preferred way.** Cosmetic.
- **Whether it belongs in the product.** Every fragrance people discuss
  belongs. There is no editorial filter and there must not be one.
- **Anything about notes, accords, price or images.** Those never leave
  the catalogue response — only `Name`, `Brand` and `Year` are stored.

## Nothing happens until you decide

`approved` starts `null`, and `apply` writes nothing for a null row. An
unreviewed file adds zero fragrances. If you run `apply` and nothing
happens, that is the guard working, not a bug.
