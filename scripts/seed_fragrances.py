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
import logging
import sys

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.resolve.entities import add_fragrance

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
]

#: Frequent mentions left out on purpose, with the reason, so the gap is a
#: recorded decision rather than an oversight.
DEFERRED = {
    "Perseus": "AMBIGUOUS ACROSS HOUSES — deliberately has no bare alias",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
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
