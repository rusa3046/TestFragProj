"""The first curated fragrances, drafted from the corpus and checked by hand.

`fragrances.jsonl` is empty on a fresh clone, which means zero queryable
edges no matter how good extraction is: an edge needs *both* its subject
and its object to be a curated bottle. This is the file that takes the
funnel off zero.

Ranked by how often each appears in comparison claims, subject or object,
with case and spelling variants merged.

Twenty-six entries. The first seventeen were curated by hand; the last nine
come from `data/curation/verified.json` and were applied on 2026-08-11 after
each brand was independently re-corroborated — see the comment above them.
Four of those nine (Detour Noir, Woody Oud, Oajan, Zenith Blue) were
deliberately absent until then, because their brands could not be
established without guessing and a wrong brand is invisible in a test suite
and wrong on a page. Three of the four guesses recorded at the time turned
out to be wrong, so the deferral was the right call.

`Perseus` remains deferred, and always will while the corpus stays
ambiguous: two houses ship one.

Safe to re-run: `add` is skipped for a canonical name that already exists,
so this can be extended rather than replaced.

    uv run python scripts/seed_fragrances.py
    uv run python -m fragrance_graph.resolve.entities backfill
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.resolve.entities import add_alias, add_fragrance

log = logging.getLogger("seed_fragrances")

#: (canonical_name, brand, aliases). Aliases are the spellings the corpus
#: actually contains — not every conceivable variant, since fuzzy matching
#: already handles case and small typos.
SEED: list[tuple[str, str, list[str]]] = [
    ("Parfums de Marly Layton", "Parfums de Marly", ["Layton"]),
    ("Creed Aventus", "Creed", ["Aventus", "Creed Aventus"]),
    (
        "Kilian Angels' Share",
        "By Kilian",
        ["Angel Share", "Angels Share", "Angel's Share", "Angel Shares",
         "Angels' Share", "AS"],
    ),
    (
        "Maison Francis Kurkdjian Baccarat Rouge 540",
        "Maison Francis Kurkdjian",
        ["BR540", "540", "Baccarat Rouge 540", "Baccarat 540", "Baccarat Rouge"],
    ),
    ("Lattafa Khamrah", "Lattafa", ["Khamrah", "Kamrah", "Khamra"]),
    (
        "Armaf Club de Nuit Intense Man",
        "Armaf",
        ["CDNIM", "Club de Nuit", "Club de Nuit Intense",
         "Club de Nuit Intense Man"],
    ),
    ("Tom Ford Oud Wood", "Tom Ford", ["Oud Wood"]),
    ("Parfums de Marly Percival", "Parfums de Marly", ["Percival"]),
    ("Parfums de Marly Delina", "Parfums de Marly", ["Delina"]),
    ("Dior Sauvage", "Dior", ["Sauvage", "Dior Sauvage"]),
    ("Parfums de Marly Althair", "Parfums de Marly", ["Althair"]),
    ("Montblanc Explorer", "Montblanc", ["Mont Blanc Explorer", "Explorer"]),
    ("Parfums de Marly Pegasus", "Parfums de Marly", ["Pegasus"]),
    ("Parfums de Marly Herod", "Parfums de Marly", ["Herod"]),
    ("Mancera Cedrat Boise", "Mancera", ["Cedrat Boise"]),
    # Two houses ship a Perseus, and the bare alias attached every mention
    # to the wrong one. In this corpus people overwhelmingly mean Maison
    # Alhambra's — "Perseus by Maison Alhambra is a great clone of
    # Pegasus", "I have Maison Alhambra's Perseus which is a clone of PDM
    # Pegasus" — so the bare alias produced an edge asserting PdM Perseus
    # was a dupe of PdM Pegasus: a false claim about one house's own two
    # bottles, attributed to people who said the opposite.
    #
    # Both entries exist; neither claims the bare word. Mentions that say
    # only "Perseus" stay unresolved and visible, which is the right
    # outcome for a genuinely ambiguous name.
    ("Parfums de Marly Perseus", "Parfums de Marly", ["PdM Perseus"]),
    (
        "Maison Alhambra Perseus",
        "Maison Alhambra",
        ["Alhambra Perseus", "Maison Alhambra's Perseus",
         "Perseus by Maison Alhambra", "Maison Alhambra - Perseus"],
    ),
    # --- Applied from data/curation/verified.json, 2026-08-11 ---------------
    #
    # The nine mentions Part A of the research resolved. Each brand was
    # re-corroborated before being written here, because the research ran
    # with WebFetch blocked by the egress proxy and its evidence was search
    # snippets rather than pages it read. That block is still in place, so
    # the standard applied instead was **agreement across independent
    # sellers, preferring the house's own storefront** — and, where the name
    # collides across houses, the corpus's own co-occurrence counts, which
    # docs/CURATION.md treats as the evidence that actually settles a
    # flanker.
    #
    # Three of the four brands the DEFERRED notes guessed at were wrong or
    # blank, which is why they were deferred rather than seeded.
    ("Parfums de Marly Oajan", "Parfums de Marly", ["Oajan", "PDM Oajan"]),
    ("Al Haramain Detour Noir", "Al Haramain", ["Detour Noir", "Détour Noir"]),
    ("French Avenue Zenith Blue", "French Avenue", ["Zenith Blue"]),
    # Retailers split on the label because Maison Alhambra is a Lattafa
    # sub-brand, so "Lattafa Woody Oud" is the same bottle and is carried as
    # an alias rather than a second entry.
    (
        "Maison Alhambra Woody Oud",
        "Maison Alhambra",
        ["Woody Oud", "Lattafa Woody Oud", "Alhambra Woody Oud"],
    ),
    (
        "Armaf Club de Nuit Imperiale",
        "Armaf",
        ["Imperiale", "Club de Nuit Imperiale", "CDN Imperiale"],
    ),
    (
        "Lattafa Bade'e Al Oud Amethyst",
        "Lattafa",
        ["Amethyst", "BAO Amethyst", "Bade'e Al Oud Amethyst"],
    ),
    (
        "Orientica Luxury Collection Royal Bleu",
        "Orientica",
        ["Orientica Royal Bleu", "Royal Bleu"],
    ),
    ("Fragrance World Oud Wonder", "Fragrance World", ["Oud Wonder"]),
    # Word order varies in the corpus and normalisation does not reorder
    # tokens, so both spellings are listed. Taking the bare "Qahwa" is a
    # judgement about *this* corpus: 6 of its 7 mentions sit beside
    # "khamrah", and one spells the relationship out — "lattafa debuted a
    # new version of their OG Khamrah scent called 'Qahwa'". Fragrance World
    # also ships a bare "Qahwa", and the word is just Arabic for coffee, so
    # a broader corpus should re-check this one. It also stops "Khamrah
    # Qahwa" collapsing into plain Khamrah, which is what happens today.
    (
        "Lattafa Khamrah Qahwa",
        "Lattafa",
        ["Khamrah Qahwa", "Qahwa Khamrah", "Qahwa"],
    ),
    # --- Second pass, 2026-08-11 -------------------------------------------
    #
    # Everything below clears 3 mention slots, which is the floor for ever
    # reaching the 3-commenter bar. Two kinds of evidence were allowed, and
    # no others:
    #
    #   1. The mention names its own house. "Darcy by Parfums de Marly" and
    #      "al haramain amber oud ruby" need no lookup — the commenter
    #      already did it. This is the job Fragella would otherwise be paid
    #      to do, and for these mentions the corpus does it for free.
    #   2. Independent sellers agree, preferring the house's own storefront.
    #
    # Mentions that cleared neither were left unresolved on purpose, and
    # stay visible in `resolve.entities report`: Fresh Agar (4), Aether (4),
    # EXCLUSIF BLUE (4), dream love 1000 (4), Craze (3), Atomic Rose (3).
    # Searching found no house for them, so they get no guess. Same for
    # "Armaf Untold" (5) — the house is certain but the bottle's full name
    # is not, and a canonical name is not a thing to approximate.
    #
    # Parfums de Marly, corroborated as one line: Haltane (2021, a Harrods
    # exclusive), Sedley (2019), Kalan (2019), Carlisle (2015), Darcy.
    ("Parfums de Marly Haltane", "Parfums de Marly", ["Haltane"]),
    ("Parfums de Marly Sedley", "Parfums de Marly", ["Sedley"]),
    ("Parfums de Marly Kalan", "Parfums de Marly", ["Kalan"]),
    # The corpus spells this "Carlyle"; the house spells it Carlisle.
    (
        "Parfums de Marly Carlisle",
        "Parfums de Marly",
        ["Carlyle", "Carlisle", "PDM Carlisle"],
    ),
    ("Parfums de Marly Darcy", "Parfums de Marly", ["Darcy", "Darcy by Parfums de Marly"]),
    # Recommended by data/curation/VERIFICATION.md: 9 of 34 Delina comments
    # attach "Exclusif", the highest flanker rate in the corpus. With one
    # node, commenters disagreeing about whether Club de Nuit Imperiale
    # clones Delina or Delina Exclusif become contradictory edges instead of
    # two coherent claims.
    ("Parfums de Marly Delina Exclusif", "Parfums de Marly", ["Delina Exclusif"]),
    # A different bottle from Parfums de Marly Carlisle above — this is the
    # clone, and the corpus always names the house when it means this one.
    (
        "Fragrance World Carlisle",
        "Fragrance World",
        ["fragrance world carlisle", "Fragrance World Carlisle"],
    ),
    ("Lattafa Maahir Legacy", "Lattafa", ["Maahir Legacy"]),
    ("Lattafa Rave Now", "Lattafa", ["Rave Now", "Lataffa Rave Now"]),
    # Not a Middle Eastern clone house, which is why searching mattered: the
    # corpus discusses it purely as a Layton clone, in the same breath as
    # Detour Noir.
    ("The Woods Collection Dusk", "The Woods Collection", ["Dusk"]),
    # Maison Alhambra's clones, each named by the corpus itself: "clone
    # hercules from maison alhambra", "Maison Alhambra Delilah", "tobacco
    # touch ... (alhambra clone of tobacco vanille same line as woody oud)".
    (
        "Maison Alhambra Hercules",
        "Maison Alhambra",
        ["Hercules", "Hercules (Herod)", "clone hercules from maison alhambra"],
    ),
    ("Maison Alhambra Delilah", "Maison Alhambra", ["Delilah", "Maison Alhambra Delilah"]),
    ("Maison Alhambra Tobacco Touch", "Maison Alhambra", ["tobacco touch"]),
    # House named in the mention text.
    ("Creed Centaurus", "Creed", ["Creed Centaurus", "Centaurus"]),
    ("Khadlaj Azure Velvet", "Khadlaj", ["Khadlaj Azure Velvet", "Azure Velvet"]),
    (
        "Al Haramain Amber Oud Black Edition",
        "Al Haramain",
        ["Amber Oud Black Edition by Al Haramain", "Amber Oud Black Edition"],
    ),
    (
        "Al Haramain Amber Oud Ruby",
        "Al Haramain",
        ["al haramain amber oud ruby", "Amber Oud Ruby"],
    ),
    (
        "Al Haramain L'Aventure Intense",
        "Al Haramain",
        ["Al Haramain L'Aventure Intense", "L'Aventure Intense"],
    ),
    ("Afnan N.O.I", "Afnan", ["Afnan N.O.I", "N.O.I"]),
    (
        "Dumont Nitro Black Intense",
        "Dumont",
        ["Dumont nitro black intense", "Nitro Black Intense"],
    ),
    ("Lalique White in Black", "Lalique", ["Lalique White in Black"]),
    (
        "Thomas Kosmala No. 4",
        "Thomas Kosmala",
        ["Thomas Kosmala no.4", "Thomas Kosmala No. 4"],
    ),
    ("Swiss Arabian Rose 01", "Swiss Arabian", ["Rose 01 by swiss arabian", "Rose 01"]),
    ("Abercrombie & Fitch Fierce", "Abercrombie & Fitch", ["Abercromie and Fitch Fierce"]),
]

#: Spellings the corpus uses for bottles already seeded above. Kept separate
#: so the entry list stays a list of bottles rather than of strings.
EXTRA_ALIASES: list[tuple[str, list[str]]] = [
    ("Parfums de Marly Layton", ["PDM Layton"]),
    ("Parfums de Marly Percival", ["PDM Percival"]),
    ("Maison Alhambra Woody Oud", ["Woody Oud by Maison Alhambra"]),
]

#: Frequent mentions left out on purpose, with the reason, so the gap is a
#: recorded decision rather than an oversight.
DEFERRED = {
    "Perseus": "AMBIGUOUS ACROSS HOUSES — deliberately has no bare alias",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        existing = {
            row["canonical_name"]
            for row in conn.execute("SELECT canonical_name FROM fragrances")
        }
        added = skipped = 0
        for name, brand, aliases in SEED:
            if name in existing:
                skipped += 1
                continue
            add_fragrance(conn, name, brand=brand, aliases=aliases)
            print(f"  + {name}  ({', '.join(aliases)})")
            added += 1

        print(f"\n{added} added, {skipped} already present.")

        # Idempotent: add_alias unions into the stored set, so re-running
        # this script neither duplicates an alias nor drops one curated by
        # hand afterwards.
        taught = 0
        for name, aliases in EXTRA_ALIASES:
            row = conn.execute(
                "SELECT id, aliases FROM fragrances WHERE canonical_name = %s", (name,)
            ).fetchone()
            if row is None:
                log.warning("no fragrance named %r; cannot teach %s", name, aliases)
                continue
            before = set(json.loads(row["aliases"] or "[]"))
            for alias in aliases:
                if alias not in before:
                    add_alias(conn, row["id"], alias)
                    print(f"  ~ {name}  += {alias!r}")
                    taught += 1
        if taught:
            print(f"\n{taught} alias(es) taught to existing entries.")

        if DEFERRED:
            print(f"\n{len(DEFERRED)} frequent mentions deliberately skipped:")
            for mention, why in DEFERRED.items():
                print(f"  - {mention}: {why}")
        print(
            "\nNext:\n"
            "  uv run python -m fragrance_graph.resolve.entities backfill\n"
            "  uv run python -m fragrance_graph.query 'Creed Aventus'"
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
