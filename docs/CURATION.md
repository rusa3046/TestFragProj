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

## The signal that settles it: `corpus_mentions`

A name alone cannot tell you which sibling people meant, and neither can
fragrance knowledge you may not have. **Your own corpus can.**

Every catalogue name is scored by how often the corpus uses the words that
*distinguish it from the mention*:

```json
{
  "mention": "Baccarat Rouge 540",
  "count": 26,
  "canonical_name": "Baccarat Rouge 540 Extrait",
  "corpus_mentions": 0,
  "note": "nobody in the corpus wrote 'baccarat rouge 540 extrait'",
  "alternatives": [
    {"Name": "Baccarat Rouge 540", "corpus_mentions": -1}
  ],
  "approved": null
}
```

Read it as arithmetic, not expertise. `corpus_mentions` counts how many
mentions contain **the mention's own words plus the distinguishing word**:

- The catalogue proposed the **Extrait**. Mentions containing both
  "baccarat rouge 540" and "extrait": **zero**.
- The alternative adds nothing, so it is the plain bottle (`-1`).

Nobody is discussing the Extrait. Take the plain one and approve.

These are real numbers from this corpus. So are these:

| mention | candidate | `corpus_mentions` |
|---|---|---|
| `Layton` | Layton **Exclusif** | 5 |
| `Khamrah` | Khamrah **Qahwa** | 9 |
| `Sauvage` | **Eau** Sauvage | 0 |

`Khamrah Qahwa` at 9 is a real bottle people genuinely discuss — it should
get its **own** entry, not replace plain Khamrah. A high count on a flanker
means "this exists too", not "you picked wrong".

`Eau Sauvage` at 0 is the reassuring one: Dior's 1966 citrus shares a word
with the 2015 Sauvage everyone is actually talking about, and nobody in
this corpus means it.

**Counting is scoped to the mention, and getting that wrong was a real
bug.** An earlier version counted the distinguishing word free-floating
across all mentions. "exclusif" scored **29** that way — Delina Exclusif,
Club de Nuit Imperial, others — and a reviewer following this page would
have moved plain Layton, the corpus's most-discussed fragrance, onto a
flanker five people mentioned.

### The brand is not a flanker

`corpus_mentions` compares what a catalogue name adds **beyond the bottle
you named** — and the house does not count. People write bare names;
catalogues return them qualified:

```
"Layton"  ->  "Parfums de Marly Layton"
```

Read literally that adds three words, and an earlier version of this rule
treated them as flanker qualifiers. Measured against the 25 hand-verified
entries in `data/curation/verified.json`, **zero** cleared the auto-rule —
not because it was strict, but because it was unreachable.

With the house excluded, 19 of those 25 clear it and the 6 held are exactly
the family this page is about: `Club de Nuit` → Intense Man, `Qahwa` →
Khamrah Qahwa, `Imperiale`, `Amethyst`, `Orientica Royal Bleu`, and the
deliberately-ambiguous `Perseus`.

`corpus_mentions` values:

| value | meaning |
|---|---|
| `-1` | the name adds no word — it *is* the plain bottle, no flanker question |
| `0` | **the loud one.** A bottle whose distinguishing word nobody wrote |
| `n` | that many corpus mentions use the distinguishing words |

## `examples` are context, not proof

Each row also carries up to two real spans people wrote:

```json
"examples": ["club de nuit is the best aventus clone for the money"]
```

Useful for seeing how a word gets used — but **often it will not settle
anything.** That quote only identifies the bottle if you happen to know
which Club de Nuit is the famous Aventus clone, and you should not have to.
Treat the examples as colour and `corpus_mentions` as the evidence.

When neither settles it, reject. See below.

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

## Measured 2026-08-11: the auto-rule cannot currently fire

`daily.AUTO_RULE` auto-approves a proposal only when
`corpus_mentions == -1` — "the proposed name adds no word to the mention,
so the flanker question does not arise". Replayed against the 25
hand-verified entries in `data/curation/verified.json`, entries a human
already approved:

    auto-approved   0 / 25
    held           25 / 25

Not narrow — **unreachable**. The cause is not the `-1` threshold. It is
that `distinguishing_words` compares the mention against the catalogue's
*brand-qualified* name, so the brand counts as a distinguishing word:

    mention "Layton"  vs  "Parfums de Marly Layton"
      distinguishing_words -> ["parfums", "de", "marly"]
      corpus_mentions      -> 0, not -1
      similarity           -> 0.414, so `confident` is false too

Both gates fail, for the same reason, on a brand prefix that is not a
flanker at all. Commenters write the bare bottle name; catalogues return
the house with it. So the rule asks a flanker question about "Creed" and
"Dior" and then refuses to answer it.

`Brand` is already a separate field, already returned, already stored.
Excluding the brand's own words from `distinguishing_words` before
computing support changes the same 24 entries to:

    corpus_mentions == -1   19 / 24
    confident (>= 0.80)     19 / 24
    both -> auto-approved    19 / 24

and the 5 it still holds are exactly the flanker family this document is
about:

| mention | catalogue name | held because |
|---|---|---|
| `Club de Nuit` | Club de Nuit **Intense Man** | `intense man`, 8 corpus mentions |
| `Qahwa` | **Khamrah** Qahwa | `khamrah`, 9 corpus mentions |
| `Imperiale` | **Club de Nuit** Imperiale | `club de`, 8 corpus mentions |
| `Amethyst` | **Bade'e Al Oud** Amethyst | `bade e al oud`, 1 mention |
| `Orientica Royal Bleu` | **Luxury Collection** Royal Bleu | `luxury collection`, 0 mentions |

That is the rule behaving as its docstring describes: it takes the rows
with no judgement in them and holds the ones with a real question. The
25th entry, `Perseus`, is the known-ambiguous one — two houses ship a
Perseus — and is excluded from the count.

**This change has not been made.** It is a change to logic that writes
permanent, silent merges, and it could not be validated end to end here
because `api.fragella.com` is blocked by the runner's egress policy (403
on CONNECT). It should be made against a live catalogue, not a replay.

Worth noting for the target: 19 auto-approvals would take curation from
50 to ~69, inside the 60-80 band, without a human deciding anything the
corpus had not already settled.

## Lookups are spent before they are filtered

`candidates` drops pronouns and anything mentioned once, but not text that
cannot name a bottle for other reasons. Of the 25 mentions the
2026-08-11 run queued:

- 4 are unnameable — `this stuff`, `extrait`, `Bought it`, `Club`
- 3 are bare houses, not bottles — `Tom Ford`, `Armaf`, `Alhambra`; a
  search for a house returns whichever bottle it likes
- `Creed` and `creed` are queued as two separate lookups

The catalogue's free tier is **20 requests per month** and the default
`--lookup-limit` is 25, so one run overspends the month before any of
this is weighed. Filter the candidate list, or lower the limit, before
the next live run.
