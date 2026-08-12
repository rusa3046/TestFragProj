# Curation verification

Research artifact for review. **Nothing here has been applied** — no source
module, no seed script, no corpus file was modified. `data/curation/verified.json`
holds the per-mention findings; this file is the summary.

## Headline

**One of the sixteen committed entries is wrong: `Perseus`.** Details below.

The other fifteen are correct. Three of the four mentions that
`scripts/seed_fragrances.py` deferred had a *guess* recorded in the
`DEFERRED` comment, and two of those guesses were wrong — deferring rather
than seeding them was the right call.

## Counts

| | n |
|---|---|
| Mentions researched | 25 |
| **high** confidence | 24 |
| **conflicting** | 1 (`Perseus`) |
| **unknown** | 0 |
| Entries carrying at least one source URL | 25 / 25 |
| Sources from Fragrantica or Parfumo | 0 |

Part A (9 previously unresolved) resolved 9/9 with high confidence.
Part B (16 already curated) confirmed 15 correct, found 1 wrong.

`year` is recorded as `"unknown"` for most entries — see *Limitations*.

---

## The finding that matters: `Perseus` is wrong

`scripts/seed_fragrances.py` line:

```python
("Parfums de Marly Perseus", "Parfums de Marly", ["Perseus"]),
```

**Two different houses ship a fragrance called Perseus.**

- **Parfums de Marly Perseus** — a 2024 release, an original.
- **Maison Alhambra Perseus** — a clone house's clone of Parfums de Marly
  **Pegasus**. Different house, same name, and it clones a *different*
  bottle than the one it shares a name with.

Of the 16 `Perseus` mentions in the corpus, **seven explicitly name Maison
Alhambra**:

> "How about Perseus by Maison Alhambra? It's a Pegasus clone"
> "Perseus by Maison Alhambra is a great clone of Pegasus. Have had Perseus
> for some time now and smelled Pegasus the other day and couldn't tell a
> difference"
> "I have Maison Alhambra's Perseus which is a clone of PDM Pegasus"
> "For Haltane, get Bade'e Al Oud & for Pegasus get Maison Alhambra - Perseus"
> "Alhambra Perseus is a great clone for Layton exclusif too"
> "Hercules (Herod), Perseus (Pegasus), Cassius (Carlyle) are all excellent dupes"
> "Maison Alhambra clones … like Kalos Hercules, Perseus ans the others???"

About four read as the Parfums de Marly bottle (all treating it as an
*original* that gets cloned — "French Avenue Pierce is a great clone of
Perseus!", "Zara Vetiver Pamplemousse = Perseus"). Five are undecidable
("Perseus?", "Perseus is ABSOLUTELY GARBAGE").

### Why this is worse than an ordinary flanker mix-up

A flanker error attaches a claim to a sibling bottle. This error attaches it
to a **different house's clone of a third bottle**. The bare alias
`["Perseus"]` currently captures all sixteen mentions, so a comment saying
*"Perseus is a great clone of Pegasus"* resolves to **Parfums de Marly
Perseus** and produces an edge asserting that **Parfums de Marly Perseus is
a dupe of Parfums de Marly Pegasus** — a false claim about two of that
house's own bottles, published as a quote from a real person who said
something else. That is precisely the outcome `docs/CURATION.md` calls "the
worst thing this product can do".

### Recommendation

Apply CURATION.md's own rule — *when in doubt, reject*. Remove the bare
`"Perseus"` alias. Either drop the entry until the corpus disambiguates, or
split it in two, keyed only on qualified aliases (`"Maison Alhambra Perseus"`,
`"Alhambra Perseus"`, `"PDM Perseus"`), leaving bare `Perseus` unresolved.
An unresolved mention stays visible in `resolve.entities report`; this merge
would not.

One caveat on weight: 15 of the 16 mentions come from a **single video**
(`OPxVZKffBAw`). This is one comment section's vocabulary driving a
catalogue entry, which `data/corpus/PROVENANCE.md` already warns about.

### Why the same trap did *not* catch the others

Maison Alhambra normally gives its clones deliberately non-colliding names:
Leyden (Layton), Hercules (Herod), Cassius (Carlisle), Galatea (Godolphin),
Kalos. `Perseus` is the exception where the clone house reused a name the
original house also uses.

There was one near miss. A commenter wrote *"Maison Alhambra's clones of PDM
fragrances such as Hercules (Herod), **Pegasus (Percival)**, Cassius
(Carlisle)"*, which would imply a second collision on `Pegasus`. It is not
real — **the same commenter posts a correction in the reply thread**: *"I
made a mistake on what I thought I had. I have Maison Alhambra's Perseus
which is a clone of PDM Pegasus."* Reading the reply thread rather than the
isolated comment is what settles it.

---

## `Sauvage`: the flagged risk does not materialise, but a different one does

The committed mapping of bare `Sauvage` → **Dior Sauvage** is **correct**.
Across all 36 corpus comments containing "sauvage", the string **"eau
sauvage" appears zero times**. Nobody here is discussing the 1966 Roudnitska
citrus. The corpus was seeded from three `dior sauvage dupe` videos, which
is why.

Two things a reviewer should still act on:

1. **The protection is the corpus, not the code.** Eau Sauvage (1966) and
   Sauvage (2015) are unrelated fragrances sharing a house and most of a
   name. A corpus drawn from r/fragrance or vintage discussion would mix
   them freely. Recommend an explicit negative guard — *"Eau Sauvage" must
   never fuzzy-match "Sauvage"* — and a test for it, since at
   `FUZZY_THRESHOLD = 0.88` the two strings are close.

2. **The concentration split is live right now.** The single `Dior Sauvage`
   entry will silently absorb claims about Elixir and Parfum:

   > "Lattafa Asad is a Dior Sauvage **elixir** clone … Neeb is only
   > comparing Dior Sauvage EDT/EDP dupes **NOT the Elixir**."
   > "I'm really looking for a good Dior Sauvage **Parfum** clone"

   Sauvage Elixir has a different note profile (cinnamon, licorice, nutmeg)
   and its own distinct clone set. Merging it into Sauvage makes the dupe
   graph wrong in a way no test will catch.

---

## Part A: the nine unresolved mentions

All nine resolved with high confidence. The `DEFERRED` dict in
`scripts/seed_fragrances.py` recorded a guess for four of them; **two of
those guesses were wrong and one was blank**:

| mention | `DEFERRED` said | actually is | verdict |
|---|---|---|---|
| `Oajan` | "probably **Lattafa**" | **Parfums de Marly** Oajan | guess was wrong |
| `Detour Noir` | "probably **Armaf**" | **Al Haramain** Detour Noir | guess was wrong |
| `Zenith Blue` | "unrecognised" | **French Avenue** Zenith Blue (2025) | now identified |
| `Woody Oud` | "several houses use this name" | **Maison Alhambra** Woody Oud | corpus names the house outright |

**`Oajan` is the instructive one.** Guessing Lattafa would have inverted the
direction of every edge involving it: in this corpus Oajan is the *original*
that people ask for clones of ("We NEED an Oajan clone", "Can you find some
oajan dupes"), not a clone. And the evidence to avoid the guess was already
in the repo — the corpus literally contains the string **"pdm oajan"**.

The remaining five:

| mention | resolved to |
|---|---|
| `Imperiale` | Armaf Club de Nuit Imperiale (women's; Delina/Delina Exclusif dupe) |
| `Amethyst` | Lattafa Bade'e Al Oud Amethyst |
| `Orientica Royal Bleu` | Orientica Luxury Collection Royal Bleu |
| `Qahwa` | Lattafa Khamrah Qahwa |
| `oud wonder` | Fragrance World Oud Wonder |

### Sub-brand attribution is the main source of apparent ambiguity

Three of the nine looked ambiguous across houses and were not. Each is a
parent/sub-brand pair, not two products:

- **Lattafa → Maison Alhambra** (`Woody Oud` is sold under both labels; the
  MAISON ALHAMBRA trademark is registered to Lattafa Perfumes Ind. LLC)
- **Fragrance World → French Avenue** (`oud wonder` — one commenter says
  Fragrance World, another says French Avenue; both are right)
- **Al Haramain → Orientica** (sister brands, mixed freely in the same
  threads)

Worth encoding as a known-aliases table rather than rediscovering per
mention.

### One genuinely ambiguous mention, resolved only by corpus context

**`Qahwa`.** Fragrance World — a house that already appears in this corpus
via Oud Wonder — ships its own fragrance simply called "Qahwa". Outside a
Khamrah thread a bare "Qahwa" is undecidable. It is decidable *here* only
because every one of the 8 hits sits adjacent to the word "khamrah".
Recommend resolving `Qahwa` **only when "khamrah" co-occurs**, and leaving a
standalone `Qahwa` unresolved. ("Qahwa" is also just the Arabic word for
coffee, so it appears in note descriptions too.)

`Amethyst` has a comparable cross-house collision (Lalique ships an
"Amethyst") that this corpus resolves cleanly but a general one would not.

---

## `docs/CURATION.md` has two defects a reviewer will hit

The `corpus_mentions` method is sound, but the doc teaches it in a way that
misfires on this corpus.

### 1. The worked example's central number is wrong

CURATION.md states, as the demonstration of the whole method:

> "The word that makes it Sillage rather than plain Club de Nuit is
> `"sillage"`. **That word appears zero times in your corpus** — nobody
> discussing this has ever typed it."

**It appears ten times.** Nine of the ten are the ordinary English perfume
noun — *"what about performance and sillage?"*, *"a sillage/projection
monster"*, *"how much time longevity and sillage and projections"*. Exactly
one is the product (*"This or Club De Nuit Sillage?"*).

The conclusion survives — nobody is meaningfully discussing Club de Nuit
Sillage, and `Club de Nuit` → **Armaf Club de Nuit Intense Man** is correct
(the alias `CDNIM` alone appears 37 times). But the arithmetic the doc
teaches would mislead a reviewer, and "sillage" is the worst possible choice
of example because it is core fragrance vocabulary.

### 2. Distinguishing words must be counted *adjacent to the mention*

The method breaks whenever the distinguishing word is also a common word.
Free-floating counts vs. counts adjacent to the mention:

| mention | distinguishing word | free-floating | actually that flanker | what the rest are |
|---|---|---|---|---|
| `Layton` | "exclusif" | **44** | **6** | Delina Exclusif, Pegasus Exclusif, Amber Oud Exclusif, Xclusif Oud Bleu |
| `Baccarat Rouge 540` | "extrait" | **39** | **~2** | "Electimuss Baroque Extrait", "CDNIM EXTRAIT", the concentration noun |
| `Club de Nuit` | "sillage" | **10** | **1** | the English word |
| `Sauvage` | "elixir" | **20** | **2** | Le Male Elixir, Urban Man Elixir, Nocturno Elixir |
| `Imperiale` | "white" | **14** | **0** | Lalique White in Black |

A reviewer following the doc literally would move `Layton` to Layton
Exclusif on a count of 44, and `Baccarat Rouge 540` to the Extrait on a
count of 39. Both would be wrong.

**Recommendation:** score the distinguishing word only when it occurs
adjacent to the mention, and add the prefix case below.

### 3. Flanker qualifiers are not always suffixes

A suffix-only check misses these entirely:

- Creed **Absolu Aventus** (not "Aventus Absolu")
- Mancera **Intense Cedrat Boise** (not "Cedrat Boise Intense")
- Dior **Eau Sauvage** (the whole 1966 problem)

Match the qualifier on **either side** of the mention.

---

## Other things worth fixing, in priority order

1. **`Perseus`** — see above. Live and wrong.
2. **`Angels' Share` carries the alias `"AS"`.** Defensible today only
   because matching is case-sensitive and whole-token: `AS` appears in 3
   comments, 2 genuinely meaning Angels' Share. But 161 corpus comments
   contain the word "as". Any lowercasing normalisation, or a fuzzy match
   against so short a string, turns this into a firehose. Recommend dropping
   it and accepting the loss of two mentions — exactly the asymmetry
   CURATION.md argues for.
3. **Add `Delina Exclusif` as its own entry.** Highest flanker rate in the
   set: 9 of 34 `Delina` comments attach "Exclusif" (26%, vs 4% for Layton).
   It is entangled with the new `Imperiale` entry — commenters disagree
   whether Club de Nuit Imperiale clones Delina or Delina Exclusif. With one
   node those become contradictory edges instead of two coherent claims.
4. **Add a negative guard + test for `Eau Sauvage` vs `Sauvage`.**
5. **Keep `Sauvage Elixir` / `Sauvage Parfum` out of the `Dior Sauvage`
   entry.** Both are discussed as distinct bottles today.
6. **Record the parent/sub-brand table** (Lattafa/Maison Alhambra,
   Fragrance World/French Avenue, Al Haramain/Orientica).

---

## Limitations

- **`year` is `"unknown"` for most entries, deliberately.** For most of these
  bottles the only crisp launch-year source is Fragrantica or Parfumo, which
  SPEC forbids. Years are recorded only where an allowed source carried them
  (Dior Sauvage 2015 and Eau Sauvage 1966 via Wikipedia; Khamrah Qahwa 2023;
  Zenith Blue 2025; Khamrah 2022). This does not affect any brand or
  canonical-name finding.
- **`WebFetch` was blocked by the environment's egress proxy** for every
  domain tried (brand sites, retailers, Wikipedia). Evidence therefore comes
  from search-result titles, URLs and snippets rather than direct page reads.
  Every cited URL was returned by search with a title corroborating the
  claim, and brand-owned domains were preferred (parfums-de-marly.com,
  armaf.com, lattafa.com, tomfordbeauty.com, bykilian.com, creedfragrance.com,
  montblanc.com, manceraparfums.us, orienticaperfumes.com,
  alharamainperfumes.com, franciskurkdjian.com). A reviewer wanting
  page-level confirmation should spot-check the brand URLs.
- **No Fragrantica or Parfumo URL appears in `verified.json`** (verified
  programmatically). They appeared in search results and are not cited.
- **The corpus is one week of YouTube comments on 24 videos**, eight
  fragrances, six of eight queries containing the word "dupe". See
  `data/corpus/PROVENANCE.md`. Several findings here — especially `Qahwa`
  and `Perseus` — are true *of this corpus* and would need rechecking
  against a broader one.
