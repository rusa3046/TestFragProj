"""Does Fragella know the fragrances this corpus actually talks about?

Run before paying for anything. The corpus is dominated by Middle Eastern
clone houses — Lattafa, Armaf, Maison Alhambra, Orientica — and a
mainstream fragrance database may cover the designer originals well and
the clones not at all. That would solve the easy half of curation and
leave the half that matters.

Uses ~10 requests, so it fits inside the free tier's 20/month.

    export FRAGELLA_API_KEY=...        # never paste the key into a shell
    uv run python scripts/probe_fragella.py

**Fragella is a dictionary, never a similarity source.** This script only
calls /fragrances?search=. The API also exposes /fragrances/similar and
/fragrances/match, which compute similarity from notes and accords — the
exact thing SPEC dropped. Using those would replace "31 people said this"
with "an API said this" and delete the product's whole differentiation.
"""

from __future__ import annotations

import json
import os
import sys

BASE = "https://api.fragella.com/api/v1"

#: Ten mentions from the real corpus, chosen to split the question rather
#: than confirm it: five mainstream designers any database will know, and
#: five clone-house names that decide whether this is worth paying for.
PROBES = [
    ("Aventus", "designer"),
    ("Layton", "designer"),
    ("Baccarat Rouge 540", "designer"),
    ("Oud Wood", "designer"),
    ("Delina", "designer"),
    ("Khamrah", "clone house"),
    ("Club de Nuit Intense Man", "clone house"),
    ("Bade'e Al Oud", "clone house"),
    ("Detour Noir", "clone house"),
    ("Oajan", "clone house"),
]


def main() -> int:
    key = os.environ.get("FRAGELLA_API_KEY")
    if not key:
        print(
            "FRAGELLA_API_KEY is not set.\n\n"
            "  export FRAGELLA_API_KEY=your-secret-key\n\n"
            "Use the secret key from the dashboard, not the pub_ widget key.",
            file=sys.stderr,
        )
        return 1

    try:
        import httpx
    except ImportError:
        print("httpx is not installed. Run: uv sync", file=sys.stderr)
        return 1

    shown_shape = False
    seen_names: list[str] = []
    hits = {"designer": 0, "clone house": 0}
    totals = {"designer": 0, "clone house": 0}

    with httpx.Client(timeout=30.0, headers={"x-api-key": key}) as client:
        for name, kind in PROBES:
            totals[kind] += 1
            try:
                r = client.get(f"{BASE}/fragrances", params={"search": name})
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"  {name:<26} REQUEST FAILED: {type(exc).__name__}")
                continue

            if r.status_code != 200:
                # Never print the response body unredacted — some APIs echo
                # the request, and the key rides in a header here but not
                # every error path is predictable.
                print(f"  {name:<26} HTTP {r.status_code}")
                if r.status_code in (401, 403):
                    print("\nAuth failed. Check the key is the secret one.")
                    return 1
                if r.status_code == 429:
                    print("\nOut of quota for this month.")
                    return 1
                continue

            payload = r.json()
            results = payload if isinstance(payload, list) else (
                payload.get("data") or payload.get("results") or []
            )
            if not shown_shape and results:
                # Print one raw record. Guessed field names printed "?" for
                # every row on the first run, and an identical hit count for
                # every distinct query is the signature of a search param
                # being ignored — neither is visible from a tidy summary.
                print("\n--- raw first result, so the fields can be read ---")
                print(json.dumps(results[0], indent=2)[:1200])
                print(f"--- top-level keys: "
                      f"{sorted(payload) if isinstance(payload, dict) else 'list'}")
                print("--- end raw ---\n")
                shown_shape = True
            seen_names.append(
                str(results[0]) [:60] if results else "(none)"
            )
            if results:
                hits[kind] += 1
                top = results[0]
                brand = top.get("brand") or top.get("brand_name") or "?"
                title = top.get("name") or top.get("title") or "?"
                print(f"  {name:<26} -> {brand} / {title}   ({len(results)} hits)")
            else:
                print(f"  {name:<26} -> NOT FOUND")

    # An identical top result for every query means the search parameter is
    # being ignored and the hit counts mean nothing.
    if len(set(seen_names)) == 1 and seen_names[0] != "(none)":
        print("\n*** EVERY QUERY RETURNED THE SAME TOP RESULT ***")
        print("The search parameter is being ignored. The counts below are")
        print("meaningless — do not read them as coverage.\n")

    print()
    for kind in ("designer", "clone house"):
        print(f"{kind:<14} {hits[kind]}/{totals[kind]} found")

    print(
        "\nThe clone-house row is the one that decides this. The corpus is\n"
        "mostly Lattafa/Armaf/Maison Alhambra/Orientica; a database that\n"
        "only knows the designer originals solves the half that was already\n"
        "easy."
    )
    # Non-zero when the half that matters is thin, so this can gate a
    # decision rather than being something you remember to read.
    return 0 if hits["clone house"] >= 4 else 2


if __name__ == "__main__":
    raise SystemExit(main())
