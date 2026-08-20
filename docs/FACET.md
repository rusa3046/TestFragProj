# FACET — the retail product built on the evidence graph

The pipeline documented in the README turns YouTube comments into an
evidence graph: typed claims, verified quotes, counted humans. FACET is
what a shopper touches. It is a preference composer, a deterministic
recommender, and a commerce presentation layer, served by FastAPI
(`api.py`) with a single-file kiosk UI (`facet/static/index.html`).

This document records the three product decisions FACET is built on, in
the order they were made, because each one corrects a real failure the
previous state produced. The specs were authored by the product owner;
dates are when each landed.

## One sentence per layer

- **Evidence layer** answers *what can FACET responsibly claim* — neutral,
  tiered by independence, never softened.
- **Recommendation layer** answers *what is worth trying* — graded, and
  never gated on evidence volume.
- **Commerce layer** answers *what helps this shopper decide* — selective
  about what surfaces, honest about what does.

Keeping them separate is the whole design. The failure modes below are all
one layer's rules leaking into another's job.

## 1. The structured preference composer (2026-08-17)

Free text was the first interface and it failed quietly: a parse the
system half-understood produced recommendations with no visible connection
to what was typed. The composer replaces guessing with structure — three
buckets a shopper fills with chips:

| bucket | holds | meaning |
|---|---|---|
| **I like** | fragrances (anchors), notes, characteristics, performance, vibes | preserve and seek |
| **I avoid** | the same, plus fragrances as negative anchors | reduce, or exclude outright |
| **I want** | target characteristics, occasions, budget | stronger than a like — these are the point of the visit |

Two rules with teeth:

- **Buckets are priority, never usability.** "Like: strong projection" is
  a softer positive than "Want: strong projection" — it is never *ignored*
  because it sat in the wrong bucket.
- **Like Delina + avoid rose ≠ like Delina-and-rose.** An anchor loads its
  full profile (declared notes ∪ perceived notes ∪ vibes ∪ performance ∪
  occasions), the avoided axis is subtracted, and the remainder is what
  gets preserved. The anchor-profile arithmetic is first-class, not a
  fallback.

Everything compiles deterministically to the same `QueryPlan` the free-text
parser emits (`session.to_plan()`), so there is one engine, not two. Free
text survives as a secondary adapter into the same `PreferenceItem`s.

The missing-data rule is mandatory and tested: **absence of evidence never
counts as satisfying a preference.** No projection data means no credit for
"want: strong projection". Every preference reports back as
`matched | contradicted | unknown`, and the UI shows which.

Chips are coverage-aware: `/api/vocabulary` offers a default chip only when
at least 3 bottles have usable evidence for it, so the composer cannot
promise a filter the corpus cannot answer.

## 2. The commerce recommendation layer (2026-08-17)

The recommender inherited the evidence layer's independence bar as an
eligibility gate, and the result was a selling tool that opened with *"No
match below clears the independence bar."* The correction, verbatim from
the spec because it is the sentence the layer is built on:

> Evidence independence controls confidence in a claim, not whether a
> product is worth recommending.

Three concepts that were conflated and are now separate per candidate:

- **Relevance** — how well it fits the request.
- **Coverage** — how many requested dimensions have usable information.
- **Confidence** — evidence strength per dimension that *is* known.

High relevance with medium coverage beats low relevance with perfect
coverage. Nothing is excluded for being under-discussed — except by a hard
constraint (budget, explicit note exclusion, availability), and
hard-constraint exhaustion is the *only* honest empty result.

**Confidence tiers gate language, not eligibility** (`evidence.tier_of`,
enforced by the wording audit):

| tier | customer language |
|---|---|
| STRONG | "Wearers consistently describe…" |
| MODERATE | "Several wearers describe…" |
| LIMITED | "There is some evidence that…" |
| SINGLE_SOURCE | weak signal, rarely a headline reason |
| UNKNOWN | says nothing — and costs almost nothing |

Material opposition caps a tier at CONTESTED: 7-for / 3-against projection
is "opinions are mixed", never "wearers consistently". That rule exists
because the first implementation said "consistently" over exactly that
split.

**Presentation is three layers** (`commerce_card.py`): the card (3–5
preference-specific fit signals, at most 1–2 *shopper-relevant* tradeoffs),
the full story (normalized insights, grouped), and debug (everything —
counts, source ids, tiers). Customer surfaces never show thresholds,
independence bars, or raw low-value negatives ("smells like a dentist
office" stays internal; "some wearers find it medicinal" may surface if
relevant). The asymmetry is by design and by test: four fit signals and six
irrelevant negatives render as four and zero.

A contradiction of an explicit avoid is never hidden to make a sale — it
surfaces as a caveat on the card. Negative evidence prevents bad
recommendations and surfaces relevant tradeoffs; it does not dominate
merchandising.

## 3. Catalog-first candidate generation (2026-08-18)

The proving failure: "less sweet + less vanilla + summer" against a
548-bottle catalogue returned **two** bottles, because candidates were
generated from community evidence — the comment corpus was acting as the
universe of recommendable products. Backwards for retail: most of any real
catalogue is under-discussed on YouTube, permanently.

Two knowledge layers, with an ordering rule:

- **Catalog** (broad — covers nearly every bottle): declared notes,
  derived note-family tendencies, occasion priors, price, brand. Answers
  *what could plausibly fit*.
- **Community** (sparse, rich): perceived notes, performance, vibes,
  comparisons, contradictions. Answers *what extra confidence wearers add*.

**Community evidence enriches and reranks; it never gates candidacy.**
Generation is three tiers: every available bottle → catalog-profile
relevance (a plausible set of ~25–80) → community rerank *within* that
set. The structure makes the old failure impossible rather than penalized:
tier 3 reorders a set it cannot shrink.

Because "sweet" is a tendency rather than an ingredient, note families are
a curated mapping (`data/curation/note-axes.json` — warm, sweet, fresh,
gourmand, citrus, woody, floral, aquatic, plus occasion priors), matched by
token containment so "cinnamon" claims "cinnamon bark" but "amber" never
claims "ambergris". Derived tendencies are worded as **catalog-derived**
("Declared profile leans citrus…"), a third provenance voice beside
community ("wearers…") and declared ("…among the declared notes") — the
audit rejects "wearers" appearing in derived text.

Literal notes get a four-state ladder instead of present/unknown:

```
DECLARED_PRESENT    the official list has it
PERCEIVED_PRESENT   the community says it's there
NOT_DECLARED        catalog data exists and the note is absent from it
NO_CATALOG_DATA     truly unknown
```

The point of the ladder is that **catalog absence is information and
community absence is not**: "vanilla isn't among the declared notes" is a
modest positive for a vanilla-avoider, while nobody having mentioned
vanilla in comments means nothing. Two different absences, two meanings.

Result labels make community coverage information rather than
disqualification: **Best overall fit** (catalog + community agree),
**Strong profile fit** (excellent catalog match, light coverage),
**Community favorite** (strong wearer evidence), **Worth discovering**
(good profile, limited data). Measured on the proving query: 2 results
became 5, four of them bottles with no comments at all, each explained in
catalog voice.

## The wording audit holds all of it together

Every customer-visible sentence comes from a registered wording function,
and `audit.py` sweeps the pages, the API responses, and the commerce cards:
tier-gated phrases cannot appear below their tier, internal jargon
(independence, thresholds, gates) cannot appear at all, derived voice
cannot borrow community authority, and counts shown must equal items
rendered — because "Why it fits (4)" over four empty bullets shipped once
and was declared release-blocking.

## Running FACET

```bash
uv run uvicorn fragrance_graph.api:app --host 0.0.0.0
```

Then open `http://localhost:8000/`. The kiosk is one static file; the API
is documented at `/docs`. Sessions are event-sourced (validate before
record, rebuild from the log), so a refinement is an edit to the same
`PreferenceState`, re-run — never a fresh conversation.

A rebuilt database needs the curation imports as well as the corpus — see
"A rebuild needs a few more commands" in the README. Catalog-first
candidacy reads `retailer_listings` and the declared-note map directly, so
a rebuild that skips them silently degrades every answer to community-only
candidacy.

## What's next: first-party feedback

The plan of record for the feedback loop ("Love it / Maybe / Not for me"
plus an optional "What stood out?" follow-up): aggregate it from session
events into a **fourth provenance voice** with its own tier gating — silent
until volume justifies speech, and never blended into YouTube counts.
YouTube bootstraps the cold start; the weighting evolves toward first-party
evidence as it accumulates. Not built yet; recorded here so the decision
survives the session that made it.
