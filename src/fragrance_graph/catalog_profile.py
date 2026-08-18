"""L1 CATALOG: what the retailer's own listing supports about a bottle,
independent of whether anybody has ever commented on it.

    from fragrance_graph.catalog_profile import catalog_profile
    profiles = catalog_profile(conn)
    profiles[51].tendencies["citrus"]   # Tendency.HIGH / MEDIUM / LOW
    profiles[51].has_catalog_data       # False for a bottle with no
                                        # declared notes at all

## Why this module exists (the defect it fixes)

Before this module, `recommend.recommend_plan`'s candidate pool was built
from three community-evidence sources only — `grouped` (attribute claims),
`neighbours` (the comparison graph), `semantic` (vector retrieval) — and
nothing else. A 547-bottle catalogue with community claims on a few dozen
of them meant "less sweet, less vanilla, summer" could only ever return
bottles somebody had already commented on: four commenters were the entry
ticket to a 547-bottle retail catalogue. That is backwards for a retail
recommender, and it is the literal, screenshotted defect this module and
`recommend.py`'s new T2 stage exist to fix.

This module answers a narrower question than `evidence.py` does, and
answers it for **every** catalogue bottle, commented-on or not: "what does
the retailer's own declared note list, read through a curated family
mapping, suggest?" It never reads a claim, a comment, or a commenter — see
`notes.py`'s module docstring for why that data is kept in a separate
table from the evidence layer in the first place. A `CatalogProfile` is
therefore always available, cheap, and can never be `CONTESTED`,
`inferred`, or attributed to a person — it is a catalog fact or it is
`NO_CATALOG_DATA`, nothing in between.

## The two catalog-derived measures

- **`tendencies`** — one `Tendency` per curated family axis
  (`notes.load_note_axes`'s keys: `warm`/`sweet`/`fresh`/`gourmand`/
  `citrus`/`woody`/`floral`/`aquatic`), derived ONLY from how many of that
  family's notes appear in the bottle's own declared list — see
  `family_tendencies` for the exact, documented breakpoints. A "sweet"
  tendency of `HIGH` is a claim about the declared list, never a claim
  that a person finds the bottle sweet.
- **occasion priors** (`load_occasion_priors`/`occasion_prior`) — a
  curated rule table (`data/curation/note-axes.json`'s `occasion_priors`
  key) mapping a HIGH-family combination to a conservative catalog
  suitability signal for one occasion word ("summer"). Deliberately
  narrow: an occasion absent from the table gets no prior at all, not a
  guessed one.

## The note status ladder

For a single literal note (as opposed to a family), `note_status` answers
a four-way question a plain "matched / not matched" flattens: a bottle
whose OFFICIAL list carries vanilla (`DECLARED_PRESENT`) is a different
fact from one nobody has ever mentioned vanilla for
(`NO_CATALOG_DATA`) — and both are different again from one whose
official list demonstrably does NOT carry vanilla (`NOT_DECLARED`, real
information: a modest positive signal for somebody avoiding it) or one a
commenter has smelled it on (`PERCEIVED_PRESENT`, community evidence, not
a catalog fact at all). `recommend.py`'s Stage 3 (`_catalog_signal`)
consults this ladder only when the COMMUNITY evidence for that note is
silent — a person who has actually commented always outranks what the
box says, so `PERCEIVED_PRESENT` is only ever assigned by a caller that
already knows the community said so; nothing in this module reads a
claim to produce it.

## `DerivedWording`: the third provenance voice

FACET's audited wording layer already has two voices — COMMUNITY
("wearers"/"people", `commerce_card.TierWording`) and DECLARED (a literal
official note, `recommend._declared_overlap_text`). This module adds the
third: CATALOG-DERIVED, about the declared list read through the curated
family/occasion mapping, and it must never read as either of the other
two. Concretely: never "wearers"/"people" (that would claim a perception
nobody made), and never bare, e.g. "vanilla present" with no reference to
the declared list (that would blur it into an unqualified declared-fact
statement with no route back to "this is a curated mapping, not the
brand's own words about how it wears"). `audit.py`'s `_audit_commerce`
checks every template here, synthetically and against the real corpus,
for exactly this.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import psycopg

from fragrance_graph.notes import NOTE_AXES_FILE, declared_note_map, load_note_axes

log = logging.getLogger("fragrance_graph.catalog_profile")


class Tendency(StrEnum):
    """How strongly a bottle's DECLARED list leans toward one curated
    family — never a claim about how the bottle actually wears."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


#: The declared-note-count breakpoints for `family_tendencies` — deliberately
#: coarse and small-integer, the same "graded, not tuned" spirit as
#: `recommend._weight`: the point is that two declared family notes reads
#: as a real lean and zero does not, not that these exact numbers were
#: fit to anything. A bottle whose declared list names two-or-more of a
#: family's notes (e.g. lemon AND bergamot) is `HIGH`; exactly one is
#: `MEDIUM` (present, but not enough to call the bottle "forward" in that
#: family); zero is `LOW` — "or-absent," per the spec: `LOW` is also what
#: a bottle with zero declared notes at all gets for every family, which
#: is why `CatalogProfile.has_catalog_data` exists as a *separate* flag —
#: see that field's docstring for why collapsing the two would be a real
#: information loss, not a simplification.
FAMILY_HIGH = 2
FAMILY_MEDIUM = 1


def family_tendencies(
    declared: frozenset[str], axes: dict[str, frozenset[str]]
) -> dict[str, Tendency]:
    """One `Tendency` per axis in `axes` (`notes.load_note_axes`'s
    output), from how many of that axis's notes `declared` contains.

    Pure and axis-set-driven rather than hardcoding the family list: a
    future curation addition to `note-axes.json` (a new family, or
    `occasion_priors` extended to a new occasion referencing it) needs no
    change here at all — every key `load_note_axes` returns gets a
    tendency, automatically.
    """
    tendencies: dict[str, Tendency] = {}
    for axis, notes in axes.items():
        count = len(declared & notes)
        if count >= FAMILY_HIGH:
            tendencies[axis] = Tendency.HIGH
        elif count >= FAMILY_MEDIUM:
            tendencies[axis] = Tendency.MEDIUM
        else:
            tendencies[axis] = Tendency.LOW
    return tendencies


@dataclass(frozen=True)
class CatalogProfile:
    """The L1 catalog read of one bottle — declared notes, family
    tendencies, and whether there is any declared data at all."""

    fragrance_id: int
    declared_notes: frozenset[str]
    tendencies: dict[str, Tendency] = field(default_factory=dict)
    #: False for a bottle with NO declared notes at all — not "every
    #: tendency happens to be LOW," a materially different fact.
    #: `family_tendencies` returns `LOW` for a bottle with zero declared
    #: notes exactly as it would for one that declares ten notes from
    #: unrelated families, and the two are not the same claim: one has
    #: been read and found not to lean that way, the other has never been
    #: read at all. Every caller that turns a `LOW` tendency into a
    #: customer-visible "isn't X-leaning" positive MUST check this first
    #: — see `recommend._catalog_signal` — or a genuinely uncatalogued
    #: bottle gets credited for absence it was never shown to have.
    has_catalog_data: bool = False


def catalog_profile(conn: psycopg.Connection) -> dict[int, CatalogProfile]:
    """Every catalogue bottle's `CatalogProfile` — the T1 universe of
    `recommend.py`'s three-tier candidate generation: literally every row
    in `fragrances`, not only the ones with a declared note list. A
    bottle absent from `notes.declared_note_map` still gets an entry here
    (`has_catalog_data=False`, every tendency `LOW`), because "we have no
    catalog data for this bottle" is itself the fact `recommend.py` needs
    to make the NO_CATALOG_DATA branch of the note-status ladder honest —
    a `dict.get(frag_id)` returning `None` would look identical to "this
    id does not exist," which is a different, wrong claim for one of the
    547 bottles.
    """
    declared_map = declared_note_map(conn)
    axes = load_note_axes(conn)
    profiles: dict[int, CatalogProfile] = {}
    for row in conn.execute("SELECT id FROM fragrances"):
        frag_id = row["id"]
        declared = declared_map.get(frag_id, frozenset())
        profiles[frag_id] = CatalogProfile(
            fragrance_id=frag_id,
            declared_notes=declared,
            tendencies=family_tendencies(declared, axes),
            has_catalog_data=frag_id in declared_map,
        )
    return profiles


class NoteStatus(StrEnum):
    """The note status ladder (catalog-first spec) — replaces a flat
    MATCH/UNKNOWN for one literal note requested by a preference.

    `DECLARED_PRESENT` and `NOT_DECLARED` are catalog facts, always
    known when `has_catalog_data` is true, and mutually exclusive with
    every other value. `PERCEIVED_PRESENT` is never computed by this
    function from catalog data — it is supplied by a caller that already
    knows a community claim exists (`recommend._catalog_signal` only
    calls this when the community fact for the note IS `None`, so in
    practice `community_present` is always `False` at that call site;
    the parameter exists so a future caller with both signals in hand —
    e.g. a page rendering the full four-way status for every note at
    once — can ask this one function rather than re-deriving the
    priority order a second time).
    """

    DECLARED_PRESENT = "DECLARED_PRESENT"
    PERCEIVED_PRESENT = "PERCEIVED_PRESENT"
    NOT_DECLARED = "NOT_DECLARED"
    NO_CATALOG_DATA = "NO_CATALOG_DATA"


def note_status(
    profile: CatalogProfile | None,
    note: str,
    *,
    community_present: bool = False,
) -> NoteStatus:
    """Where one literal `note` stands for one bottle, catalog first.

    Priority, deliberately in this order: the OFFICIAL list wins over
    everything (a brand's own listing is never overridden by absence
    elsewhere); failing that, a community perception is real olfactive
    information the catalog's silence should not erase; failing that, the
    catalog's own "not on the list" is real information in its own right,
    available only when there IS a list to read (`has_catalog_data`);
    absent all three, the honest answer is that nothing here is known.
    """
    if profile is not None and note in profile.declared_notes:
        return NoteStatus.DECLARED_PRESENT
    if community_present:
        return NoteStatus.PERCEIVED_PRESENT
    if profile is not None and profile.has_catalog_data:
        return NoteStatus.NOT_DECLARED
    return NoteStatus.NO_CATALOG_DATA


def load_occasion_priors(path: Path = NOTE_AXES_FILE) -> dict[str, dict]:
    """`data/curation/note-axes.json`'s `occasion_priors` key, raw —
    `{occasion: {"high_if_any_high": [...], "low_required": [...]}}`.

    No corpus revalidation, unlike `notes.load_note_axes`: this table
    names AXES (already checked against the corpus when the axes
    themselves loaded), not literal notes, so there is nothing here for a
    per-note vocabulary check to do. An absent file or an absent key
    returns `{}` — every occasion then has no prior, the same safe
    fallback `load_note_axes` gives an unrecognised axis name.
    """
    if not path.exists():
        log.warning(
            "No note-axis mapping at %s -- no occasion priors available", path
        )
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    priors = raw.get("occasion_priors", {})
    return {k: v for k, v in priors.items() if not k.startswith("_")}


def occasion_prior(
    profile: CatalogProfile | None,
    occasion: str,
    priors: dict[str, dict],
) -> list[str] | None:
    """The families that justify a catalog-derived prior for `occasion`
    on this bottle, or `None` when the curated table has no rule for
    `occasion`, the bottle has no catalog data, or the rule's conditions
    are not met (any `low_required` family at `HIGH`, or no
    `high_if_any_high` family at `HIGH` — `MEDIUM` never counts as `HIGH`
    on either side, per the rule table's own docstring in
    `note-axes.json`).

    Returns the list of `HIGH` families that satisfied `high_if_any_high`
    (never empty when not `None`) so the caller can name them in the
    sentence — "leans fresh" is a different, checkable claim from "leans
    fresh/citrus," and the wording should say which families actually
    qualified rather than repeating the rule table's full candidate list.
    """
    if profile is None or not profile.has_catalog_data:
        return None
    rule = priors.get(occasion)
    if rule is None:
        return None
    low_required = rule.get("low_required", ())
    if any(profile.tendencies.get(f) is Tendency.HIGH for f in low_required):
        return None
    high = [
        f for f in rule.get("high_if_any_high", ())
        if profile.tendencies.get(f) is Tendency.HIGH
    ]
    return high or None


class DerivedWording:
    """The CATALOG-DERIVED voice — see the module docstring's "the third
    provenance voice" section. Every template returns a complete
    sentence, fully composed, the same "already audited, never
    re-derived downstream" shape `recommend._declared_overlap_text`
    already uses for the DECLARED voice — a caller embeds the result
    verbatim (`recommend._catalog_signal`, `commerce_card.fit_signal_text`
    /`tradeoff_text`'s `catalog_fit`/`catalog_avoid` branches), never
    re-wraps it in a community tier template. Registered with
    `audit.py`'s `_audit_commerce` for the FORBIDDEN/INTERNAL_TERMS
    checks every other wording surface gets, plus one this voice alone
    must hold: never "wearers," never "people" — see
    `audit._check_derived_voice`.
    """

    @staticmethod
    def note_present(note: str) -> str:
        return f"{note.capitalize()} is among the declared notes."

    @staticmethod
    def note_absent(note: str) -> str:
        return f"{note.capitalize()} isn't among the declared notes."

    @staticmethod
    def family_present(family: str) -> str:
        return f"{family.capitalize()}-forward declared profile."

    @staticmethod
    def family_absent(family: str) -> str:
        return f"Declared profile isn't {family}-leaning."

    @staticmethod
    def occasion_fit(occasion: str, families: list[str]) -> str:
        joined = "/".join(sorted(families))
        return (
            f"Declared profile leans {joined} — a conservative catalog "
            f"fit for {occasion} wear."
        )
