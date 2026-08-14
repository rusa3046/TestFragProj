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

#: "3:32" and "1:04:20" are video timestamps, which YouTube commenters use
#: constantly. Extraction reads them as subjects.
TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def normalize_name(text: str) -> str:
    """Casefold, strip accents and punctuation, drop filler, collapse space.

    Deliberately conservative: it resolves formatting differences, not
    naming differences. Anything cleverer risks merging distinct
    fragrances, which is the one failure this module must not make.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    lowered = without_accents.casefold()
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
