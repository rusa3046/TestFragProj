"""Matching raw mention text to canonical fragrance names.

Phase 1 stores what people wrote, verbatim and unnormalised. The first
live YouTube run produced these objects, all naming one fragrance:

    BR540 · 540 · BR MFK 540 · BR 540 · B540 · BR

Five correct dupe claims that are individually right and collectively
useless, because the graph sees six unrelated nodes. This module turns
mention text into a canonical fragrance, or admits it cannot.

Three layers, cheapest first:

1. **Junk rejection.** YouTube adds noise Reddit did not: video
   timestamps ("3:32"), bare numbers, single letters. These are not
   fragrances and must never become nodes.
2. **Normalised exact match.** Handles case, punctuation, and spacing —
   "Thomas Kosmala no.4" and "thomas kosmala no. 4" are the same string
   once normalised.
3. **Fuzzy match.** Handles typos and small word-order differences,
   above a deliberately high threshold.

Abbreviations are the layer that string similarity cannot reach: "BR540"
and "Baccarat Rouge 540" share almost no characters. Nothing derivable
from the text alone connects them — that is domain knowledge, and it
lives in the `fragrances.aliases` list, curated once and reused forever.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

#: Words that carry no identity — "Red Temptation from ZARA" and "Red
#: Temptation ZARA" name the same thing. Kept small on purpose: dropping
#: too much makes distinct fragrances collide.
FILLER_WORDS = frozenset({"the", "from", "by", "a", "an", "of", "version"})

#: A mention must clear this to be considered a name at all.
MIN_NAME_LENGTH = 3

#: Fuzzy-match floor. Set high because a false merge is worse than a miss:
#: an unresolved mention stays visible for curation, while two fragrances
#: wrongly merged silently corrupt every edge that touches them.
FUZZY_THRESHOLD = 0.88

#: Passed as `best_match`'s `threshold` to disable its fuzzy branch while
#: keeping the exact-on-normalised path, which `best_match` runs
#: unconditionally before `threshold` is ever consulted. Similarity scores
#: from `SequenceMatcher.ratio()` are bounded by 1.0, so nothing can ever
#: clear this floor — used wherever a match needs to be exact-or-alias
#: only, never approximate: seeding a bulk source (`releases.seed_wikidata`)
#: and retrying a mention with a trailing brand cut (below).
EXACT_ONLY = 1.01

#: "3:32" and "1:04:20" are video timestamps, which YouTube commenters use
#: constantly. Extraction reads them as subjects.
TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def fold_accents(text: str) -> str:
    """Strip diacritics via NFKD decomposition, leaving the base letters.

    "Privés" and "Prives" have to reduce to the same thing, here and
    anywhere else in the codebase that turns a name into a plain-ASCII
    token — `pages.slugify` shares this rather than re-deriving it, which
    is what fixed `profile_slug("Hermès ...")` producing "herm-s-..."
    instead of "hermes-...": a hyphen-collapse pass alone treats an
    accented letter as punctuation to replace, not a letter to keep.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_name(text: str) -> str:
    """Casefold, strip accents and punctuation, drop filler, collapse space.

    Deliberately conservative: it resolves formatting differences, not
    naming differences. Anything cleverer risks merging distinct
    fragrances, which is the one failure this module must not make.
    """
    lowered = fold_accents(text).casefold()
    # Keep alphanumerics; everything else becomes a separator, so "no.4"
    # and "no. 4" converge.
    words = re.split(r"[^a-z0-9]+", lowered)
    kept = [w for w in words if w and w not in FILLER_WORDS]
    return " ".join(kept)


def looks_like_junk(text: str) -> bool:
    """Whether a mention cannot be a fragrance name.

    Rejecting is safe: junk stays out of the graph and remains visible in
    the unresolved report. Every case here was observed in real data.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if TIMESTAMP.match(stripped):
        return True

    normalized = normalize_name(stripped)
    if len(normalized) < MIN_NAME_LENGTH:
        return True
    # A name made only of digits is a quantity or a timestamp, not a
    # fragrance. "540" alone is ambiguous even to a human reader.
    if normalized.replace(" ", "").isdigit():
        return True
    return False


def similarity(a: str, b: str) -> float:
    """Normalised similarity between two mention strings, 0.0 to 1.0."""
    return SequenceMatcher(None, normalize_name(a), normalize_name(b)).ratio()


@dataclass(frozen=True)
class Candidate:
    """A canonical fragrance and the names it answers to."""

    fragrance_id: int
    canonical_name: str
    aliases: tuple[str, ...] = ()
    #: Empty when the catalogue does not record one. Unused by `best_match`
    #: itself; it exists for `best_match_conservative_trailing_brand`,
    #: which needs to know a candidate's house to reject a mismatched
    #: trailing-brand guess.
    brand: str = ""

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.canonical_name, *self.aliases)


@dataclass(frozen=True)
class Match:
    fragrance_id: int
    canonical_name: str
    #: "exact" once normalised, or "fuzzy" above the threshold.
    method: str
    score: float


def best_match(
    mention: str,
    candidates: list[Candidate],
    *,
    threshold: float = FUZZY_THRESHOLD,
) -> Match | None:
    """Resolve a mention to one fragrance, or None if nothing is close.

    Exact-on-normalised wins outright. Otherwise the best fuzzy score
    above `threshold` wins, and ties resolve to the lowest fragrance_id so
    the result never depends on dictionary ordering.
    """
    target = normalize_name(mention)
    if not target:
        return None

    ordered = sorted(candidates, key=lambda c: c.fragrance_id)

    # Exact matches run before the junk check on purpose. "540" is a bare
    # number and looks like junk, but it was the single most common way
    # commenters wrote Baccarat Rouge 540 in the first live corpus. A
    # curated alias is a person stating a fact; the junk rule is a guess
    # about text. The guess must not overrule the fact.
    for candidate in ordered:
        for name in candidate.all_names:
            if normalize_name(name) == target:
                return Match(
                    candidate.fragrance_id, candidate.canonical_name, "exact", 1.0
                )

    # Fuzzy matching has no such warrant, so junk is refused here: a
    # timestamp that drifts within the threshold of some fragrance would
    # otherwise become an edge.
    if looks_like_junk(mention):
        return None

    best: Match | None = None
    for candidate in ordered:
        for name in candidate.all_names:
            normalized = normalize_name(name)
            if not normalized:
                continue
            score = SequenceMatcher(None, normalized, target).ratio()
            if score >= threshold and (best is None or score > best.score):
                best = Match(
                    candidate.fragrance_id, candidate.canonical_name, "fuzzy", score
                )
    return best


def debranded(candidate: str, brand: str = "") -> str:
    """A catalogue name with the house removed.

    Name similarity has the same brand problem as `distinguishing_words`:
    "Khamrah" against "Lattafa Khamrah" scores 0.64 and fails `CONFIDENT`,
    even though it is the exact bottle. Comparing against the de-branded
    name scores 1.00.
    """
    brand_words = set(normalize_name(brand or "").split())
    return " ".join(
        w for w in normalize_name(candidate).split() if w not in brand_words
    )


# --- trailing "by <brand>" -----------------------------------------------
#
# data/experiments/delina-resolution-loss.md, "resolvable alias": the
# catalogue held every one of five aliases the Delina run lost, and two —
# "atomic rose by initio", "delilah by maison alhambra" — share one shape.
# `normalize_name` already drops the filler word "by" (see FILLER_WORDS),
# but not the brand that follows it, so "atomic rose by initio" normalises
# to "atomic rose initio": three words against the catalogue's two, which
# clears neither the exact nor the fuzzy path. The diagnostic explicitly
# declined to fix this ("that needs its own review... with the false-merge
# risk weighed") because loosening the fuzzy threshold is how "Devil's
# Share" merges into "Angels' Share" — so everything below stays exact.
#
# Exact string matching on the brand alone is not enough to land the
# diagnostic's own headline case. The real catalogue records
# `brand = "Initio Parfums Prives"`; the curated houses file has "Initio
# Parfums Privés" (accented); and a commenter writes "by initio" — three
# spellings of one house, none equal as strings. Two gaps, closed below:
#
# - **accents.** `normalize_name` already folds them ("privés" ->
#   "prives"); the brand comparisons just were not routed through it.
# - **short forms.** nobody writes "atomic rose by initio parfums prives"
#   in a comment. `brand_tokens` + prefix matching accepts "initio"
#   because it is the leading run of "initio parfums prives"'s own
#   tokens — never a substring or an out-of-order subset, which would
#   accept "parfums" alone and merge unrelated houses that happen to
#   share a formula word.
#
# A prefix being *unique* is not the same question as two brands being
# *related* by prefix, and an earlier version of this file measured the
# wrong one — "217 brands, only 2 pairwise prefix collisions, both the
# same house twice" — and shipped it as the safety argument. The question
# that actually matters for the brand-consistency guard is "does this
# prefix identify exactly one house", and on the same 217 brands it does
# not, five times over: 'maison' is the leading word of six distinct
# houses (Alhambra, Crivelli, Francis Kurkdjian, Margiela, Matine,
# Tahite); 'al' of three (Haramain, Majed Oud, Wataniah); 'parfums' of two
# (de Marly, MDCI); 'marc' of two (Antoine Barrois, Jacobs); 'atelier' of
# two (Cologne, des Ors). Accepting any of those as identifying a house
# does not guard anything — it accepts whichever bottle `best_match`
# happened to return, which breaks exact ties by lowest fragrance_id, i.e.
# by insertion order. Confirmed live: "Woody Oud by Maison" resolved to
# Maison Alhambra Woody Oud only because no other catalogued bottle
# currently shares that name — the guard said yes to an ambiguous prefix
# and got lucky. `_is_known_brand` below is what actually measures
# uniqueness, not mere relatedness.


def brand_tokens(name: str) -> tuple[str, ...]:
    """A brand name reduced to comparable tokens: accents, case and
    punctuation folded via `normalize_name`, then split on whitespace.

    "Initio Parfums Privés" and "initio parfums prives" must produce the
    identical tuple — that is the whole fix for the accent mismatch
    between the curated houses file and the real catalogue's `brand`
    column, which otherwise never unify as strings.
    """
    normalized = normalize_name(name)
    return tuple(normalized.split()) if normalized else ()


def _is_known_brand(tokens: tuple[str, ...], known_brands: set[tuple[str, ...]]) -> bool:
    """Whether `tokens` may stand for a house on its own.

    Two ways to qualify:

    - **exact.** `tokens` is itself one of `known_brands`, full stop —
      "maison alhambra" names the house directly, and must never be
      refused for sharing a first word with five other houses.
    - **unambiguous prefix.** `tokens` is shorter than some known brand
      and is a leading run of *exactly one* of them. "initio" qualifies
      because only "initio parfums prives" starts with it; "maison" does
      not, because six different houses do. See the comment above this
      function for the measured count.

    Deliberately never a substring or an out-of-order subset — only ever
    a leading run — so "parfums" (the second word of "initio parfums
    prives") is refused on that basis alone even before ambiguity is
    considered; it also happens to be ambiguous in its own right (leads
    "parfums de marly" and "parfums mdci" too).
    """
    if tokens in known_brands:
        return True
    matches = [
        brand
        for brand in known_brands
        if len(brand) > len(tokens) and brand[: len(tokens)] == tokens
    ]
    return len(matches) == 1


def brands_agree(
    a: tuple[str, ...], b: tuple[str, ...], known_brands: set[tuple[str, ...]]
) -> bool:
    """Whether two brand token-sequences plausibly name the same house.

    Public — used both by the trailing-brand candidate filter below and by
    `releases.verify`'s bare-name fallback, which needs the identical rule
    for the identical reason: a brand comparison that only checks the two
    operands against each other, and not against the wider known-brand
    universe, would accept a hypothetical bare "Maison" as agreeing with
    "Maison Alhambra" merely because the letters line up.

    Equal tuples always agree. Otherwise the *shorter* one must be an
    unambiguous prefix of the longer, checked via `_is_known_brand` — so a
    brand string too vague to identify one house on its own (like a bare
    "Maison") never counts as agreeing with anything, which would defeat
    the point of checking at all.
    """
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if longer[: len(shorter)] != shorter:
        return False
    return _is_known_brand(shorter, known_brands)


#: A mention ending " by <something>". Matched case-insensitively; the
#: text on either side is returned unchanged so the caller decides what to
#: do with its case.
TRAILING_BRAND = re.compile(r"^(.*\S)\s+by\s+(\S.*)$", re.IGNORECASE)


def split_trailing_brand(
    mention: str, known_brands: set[tuple[str, ...]]
) -> tuple[str, str] | None:
    """(name, brand) if `mention` ends " by <brand>" and <brand> is known.

    `known_brands` is a set of `brand_tokens` tuples, not raw strings —
    see `known_brands` in `resolve.entities` for how it is built. The
    trailing text counts as known via `_is_known_brand`: exactly one of
    `known_brands`, or an unambiguous prefix of exactly one — so "initio"
    matches a known "initio parfums prives" (the only house it could
    mean), while "maison" does not, however many houses start with it.
    Returns None the moment the trailing text is not a *known* brand at
    all — which is the common case, and must do nothing: "Time by Pink
    Floyd" is a song, not a fragrance with a house stripped off, and the
    only thing standing between the two is that no known brand's tokens
    start with "pink floyd".
    """
    match = TRAILING_BRAND.match(mention.strip())
    if not match:
        return None
    name, brand = match.group(1).strip(), match.group(2).strip()
    if not name:
        return None
    tokens = brand_tokens(brand)
    if not tokens:
        return None
    if not _is_known_brand(tokens, known_brands):
        return None
    return name, brand


def best_match_conservative_trailing_brand(
    mention: str,
    candidates: list[Candidate],
    known_brands: set[tuple[str, ...]],
) -> Match | None:
    """`best_match`, plus one conservative retry with a trailing brand cut.

    The retry only runs at all when the ordinary match on the full mention
    fails, so it is an additional way to resolve a mention, never a
    replacement for the first attempt — a mention that already resolves
    keeps resolving exactly as before. When it does run, it is narrower
    than the first attempt in every way that matters here:

    - the stripped form is matched with fuzzy matching disabled
      (`threshold=EXACT_ONLY`) — exact and alias matches only, because a
      false merge from *guessing* at a brand-stripped name is worse than
      leaving the mention unresolved for a curator to see;
    - candidates are *filtered* to the ones whose own brand agrees with
      the brand parsed off the mention (`brands_agree`) **before**
      `best_match` ever runs, not matched-then-vetoed. That distinction
      matters in two ways an earlier version of this function got wrong:

      1. a candidate with no brand recorded at all is excluded outright,
         not waved through for lack of anything to disagree with. An
         earlier version treated "no brand" as "nothing to object to" and
         let "kismet angel by Dior" resolve to the brandless "Kismet
         Angel" row — but the mention is not silent about the house, it
         *names* one, and a different one. "Cannot verify" has to mean
         refuse, the same as an outright mismatch would, or the guard
         protects nothing from exactly the rows it has the least
         information about. Cost, measured against the real corpus: the
         one raw mention of "Kismet Angel by Maison Alhambra" (1
         occurrence, 1 claim-end) stops resolving, because that catalogue
         row genuinely records no brand. A curator recording it buys the
         match back; a false merge cannot be bought back at all.
      2. `best_match` alone always returns the *lowest-id* match among
         several candidates sharing a name or alias — so if the wrong
         one happens to sit at a lower id, the earlier match-then-veto
         version returned None (or worse, the wrong bottle, if the
         lower-id row happened to carry no brand) even when the *correct*
         one was sitting right there in the catalogue. Filtering first
         means every candidate whose brand actually agrees is reachable,
         not only whichever one `best_match` would have tried first.
    """
    direct = best_match(mention, candidates)
    if direct:
        return direct

    split = split_trailing_brand(mention, known_brands)
    if split is None:
        return None
    name, brand = split
    brand_key = brand_tokens(brand)

    retry_candidates = [
        c for c in candidates
        if c.brand and brands_agree(brand_tokens(c.brand), brand_key, known_brands)
    ]
    return best_match(name, retry_candidates, threshold=EXACT_ONLY)
