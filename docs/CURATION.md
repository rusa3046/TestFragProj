# What you are deciding

One question, per row:

> **Is this the bottle the commenters meant?**

That is the whole judgement. Not whether the fragrance is good, not
whether the house spells its own name that way — whether the *match* is
right.

## Why it matters

A fragrance entry is what turns claims about strings into claims about
bottles. Name a wrong one and every claim mentioning that word attaches
to the wrong fragrance — silently, permanently, and invisibly to the test
suite. It shows up as a quote from a real person on a page about a bottle
they were not talking about.

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


## The evidence you get

`resolve.entities batch curate.json` writes a row per bottle worth naming.
Everything above `canonical_name` is evidence; everything below it is your
decision.

```json
{
  "mention": "Detour",
  "occurrences": 6,
  "pairs_unlocked": 1,
  "would_publish": 1,
  "connects_to": ["Parfums de Marly Layton (3 people, 2 creators)"],
  "examples": ["Detour was very close when I smelled the OG",
               "Not a big fan of Detour"],
  "videos": ["3 Amazing PDM Layton Alternatives On A Budget"],
  "canonical_name": "",
  "brand": "",
  "skip": false
}
```

**The comments and the video titles are the evidence, and they are usually
enough.** "Detour" under a Layton-alternatives video is not decidable from
the string — Al Haramain ship more than one Detour — and is obvious from
the context. Where they are not enough, that is what `skip` is for.

The rows are ordered by what naming them would publish, not by how often
they appear. "this" is written 138 times and unlocks nothing.

## How to work through the file

1. **Start at the top.** The first rows are the ones that turn into pages.
2. **Read the two comments and the video titles.** Ask the one question:
   do I know which bottle this person meant?
3. **Yes** → put the full name in `canonical_name`, and the house in
   `brand`. Both, even though the house appears twice: the name is what a
   page prints, and the brand is what the matcher strips off in order to
   recognise "Detour" as "Al Haramain Detour Noir".
4. **No, or it is not a bottle** → set `"skip": true` and leave the name
   blank.
5. If a row was drafted for you, it carries `drafted_by`. Read it and set
   `"confirmed": true`, or change it, or skip it.

Then `resolve.entities apply curate.json`.

## When in doubt, skip

The two errors are not symmetric.

- **Skipping a good match** leaves the mention unresolved. It reappears in
  the next batch file, and you lose a few edges until you get to it.
- **Naming a wrong match** merges two different fragrances forever. Every
  edge touching either one is now wrong, nothing warns you, and finding it
  later means re-reading quotes one at a time.

This is the same asymmetry `names.py` encodes in `FUZZY_THRESHOLD = 0.88`:
*a false merge is worse than a miss.* If the comments do not make you
confident, skip and move on.

## What you are *not* deciding

- **Whether the fragrance is good.** Irrelevant.
- **Whether the brand is spelled the house's preferred way.** Cosmetic —
  `pages.brand_casing` normalises spelling at render time.
- **Whether it belongs in the product.** Every fragrance people discuss
  belongs. There is no editorial filter and there must not be one.
- **Anything about notes, accords, price or images.** None of those are
  stored, and no page may show them.

## Nothing happens until you decide

`apply` refuses a file with any row that is neither named nor skipped, and
refuses any drafted row a person has not confirmed — before writing
anything, naming the rows. An unreviewed file adds zero fragrances. If you
run `apply` and it refuses, that is the guard working.

The reason it is that strict: on 2026-08-11 a *drafted* label file was
imported as ground truth and overwrote two hand-made labels, one with its
subject and object the wrong way round. That was a person mistyping a
command, not a bug, which is why the guard lives in the data.

---

# History: the catalogue path, removed 2026-08-14

Curation used to work the other way round. The Fragella catalogue proposed
a canonical name for each unresolved mention and a person approved or
rejected it; `corpus_mentions` scored how often the corpus used the words
that distinguished the proposal from what people wrote, and the daily loop
auto-approved the rows where that score meant there was no judgement to
make.

**It was removed because it did not pay for itself.** Measured 2026-08-12:
60 lookups, $3.00, 5 names, **0 pages** — the catalogue does not carry the
small houses this corpus discusses, so the mentions worth resolving were
exactly the ones it could not answer. Against the ledger it was $1.45 of
the $2.78 this project has ever spent.

The sections below are kept because they are measurements, and because the
failure modes they describe are properties of *fragrance naming* rather
than of any particular tool — flankers still bite, and the reverse flanker
below is still the worst merge this project can make.

## Measured 2026-08-11: why the auto-rule could not fire

*This is the measurement that prompted the brand-exclusion fix above. It
is kept as the record of how the defect was found; the behaviour it
describes is the behaviour **before** that fix.*

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

**This change has since been made** — see the brand-exclusion section
above, which implements the brand exclusion and carries its own tests.
It has still never run against the live catalogue: `api.fragella.com` is
blocked by the runner's egress policy (403 on CONNECT), so both the
measurement and the fix rest on an offline replay against known-good
entries. The first live run is still the real test.

Worth noting for the target: 19 auto-approvals would take curation from
50 to ~69, inside the 60-80 band, without a human deciding anything the
corpus had not already settled. That is a projection from the replay, not
an observed result.

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

## The reverse flanker, found on the first live run (2026-08-11)

Excluding the brand made the auto-rule reachable. It also made a second
defect reachable, and that one merged a bottle.

`distinguishing_words` asks what the *catalogue name* adds. It never asked
what the *mention* adds. So a mention more specific than the name it
matched has no distinguishing words, scores `-1`, and auto-approves:

    "Club De Nuit EDP"  vs  "Armaf Club De Nuit"   -> [] -> merged
    "Layton Exclusif"   vs  "Parfums de Marly Layton"
    "Khamrah Qahwa"     vs  "Lattafa Khamrah"
    "Aventus Absolu"    vs  "Creed Aventus"

The first of those actually happened. It created `Armaf Club De Nuit`
alongside the hand-curated `Armaf Club de Nuit Intense Man`, so
`Club De Nuit EDP` and `Club de Nuit` pointed at different nodes for the
same bottle and its edges split across both. The row has been removed and
the mention returned to unresolved.

Of the two rows that first run auto-approved, one was this. **A 50% error
rate**, against the module docstring's estimate that automatic curation
would do somewhat worse than the 6% measured on hand-checked entries.

A qualifier is a qualifier on whichever side it appears, so auto-approval
now requires agreement in both directions — `names_agree`. Neither the
mention nor the name may add a word the other lacks. `Club de nuit Iconic`
→ `Armaf Club De Nuit Iconic` still auto-approves, because the qualifier
is present on both sides and there is genuinely nothing to decide.

The general lesson is the one the publishing gate already encodes: the
auto-rule is not the thing keeping bad merges away from readers, and it
should not be trusted as though it were.
