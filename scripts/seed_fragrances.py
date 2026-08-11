"""The first curated fragrances, drafted from the corpus and checked by hand.

`fragrances.jsonl` is empty on a fresh clone, which means zero queryable
edges no matter how good extraction is: an edge needs *both* its subject
and its object to be a curated bottle. This is the file that takes the
funnel off zero.

Ranked by how often each appears in comparison claims, subject or object,
with case and spelling variants merged. Sixteen entries covering ~29% of
mention slots.

**Four frequent mentions are deliberately absent** — Detour Noir, Woody
Oud, Oajan, Zenith Blue. Their brands could not be established without
guessing, and a wrong brand is invisible in a test suite and wrong on a
page. They are 36 of 1,123 mention slots, about 3%, and can be added in
one line each once known.

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
]

#: Frequent mentions left out on purpose, with the reason, so the gap is a
#: recorded decision rather than an oversight.
DEFERRED = {
    "Detour Noir": "brand unverified — my guess of Armaf is contradicted by "
                   "research; see data/curation/verified.json",
    "Woody Oud": "several houses use this name; unclear which is meant",
    "Oajan": "brand unverified — my guess of Lattafa is contradicted by "
             "research; see data/curation/verified.json",
    "Zenith Blue": "unrecognised by me; research proposes a house",
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
