"""Proposing canonical names from a catalogue, for a human to approve.

Curation is the bottleneck: an edge needs *both* ends resolved, so the
committed corpus yields zero queryable edges until fragrances are named.
Measured, sixteen entries give 4 pairs and eighty give 61 — the work is
bounded but the first entries are worth the least, which makes doing it
entirely by hand a poor trade.

Most of it is mechanical. "Khamrah" -> "Lattafa Khamrah" is a lookup, not
a judgement. This module does the lookup against a real catalogue and
writes a review file; a person approves, corrects, or rejects each row.

**The model never authors a name.** Every proposal is a row the catalogue
returned, carried through verbatim. A canonical name that exists in no
catalogue is unrepresentable here — not merely unlikely. That is the same
property `evidence_verified` gives claims: the output has to check out
against a source that is not the thing generating it, which is the one
pattern in this project that has held up every time.

Three rules this enforces, from SPEC:

1. **Only `/fragrances?search=`.** The catalogue also exposes
   `/fragrances/similar` and `/fragrances/match`, which compute similarity
   from notes and accords. Using those would replace "31 people said this"
   with "an API said this". `FORBIDDEN_PATHS` and a test guard it.
2. **Three fields.** `Name`, `Brand`, `Year`. The response also carries
   notes, accords, images, ratings, longevity, sillage and an affiliate
   `Purchase URL` — every one of which the product must not use.
3. **One-time enrichment, never a runtime dependency.** Results land in
   `fragrances` and are committed to `data/corpus/fragrances.jsonl`. If
   the service changes terms or disappears, the dictionary survives.

    propose   unresolved mentions -> review file (costs one request each)
    apply     reviewed file -> fragrances table
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.resolve.entities import add_fragrance, unresolved_mentions
from fragrance_graph.resolve.names import normalize_name, similarity

log = logging.getLogger("fragrance_graph.resolve.enrich")

BASE_URL = "https://api.fragella.com/api/v1"

#: The only endpoint this module may call. Anything computing similarity
#: is off limits — see the module docstring and SPEC.
SEARCH_PATH = "/fragrances"

#: Named so a test can assert they appear nowhere in this module's source.
FORBIDDEN_PATHS = ("/fragrances/similar", "/fragrances/match")

#: The only fields copied out of a catalogue response.
KEPT_FIELDS = ("Name", "Brand", "Year")

#: Pronouns and bare determiners the extractor records as subjects. They
#: dominate the unresolved list — "this", "this one" and "this fragrance"
#: account for ~148 mentions — and none can ever name a bottle, so
#: proposing lookups for them would spend requests on guaranteed misses.
PRONOUN = re.compile(
    r"^(it|its|this|that|these|those|they|them|one|both|all|others?|"
    r"the (og|original|others?|dupes?|fake ones?|same|rest)|this one|"
    r"that one|this fragrance|this perfume|the perfume|the same|"
    r"mine|yours|everything|anything|something|nothing)$",
    re.IGNORECASE,
)

#: Above this, the catalogue's name matches the mention closely enough that
#: a reviewer is confirming rather than deciding. Below it, the row is
#: flagged. Deliberately lower than the resolver's own FUZZY_THRESHOLD:
#: this only sets how loudly a row asks to be read, and a human sees every
#: row either way.
CONFIDENT = 0.80


def is_pronoun(text: str) -> bool:
    return bool(PRONOUN.match(text.strip()))


@dataclass
class Proposal:
    """One mention, and what the catalogue offered for it."""

    mention: str
    #: How often the mention appears — review effort should follow it.
    count: int
    canonical_name: str | None = None
    brand: str | None = None
    year: str | None = None
    #: Name similarity between the mention and the proposed name.
    score: float = 0.0
    #: True when the reviewer is confirming rather than deciding.
    confident: bool = False
    #: Other catalogue rows for the same query, so a reviewer can correct
    #: without a second lookup. Each carries `corpus_mentions`: how often
    #: the corpus uses the words that distinguish it from the mention.
    #: -1 means the name adds no word, i.e. it *is* the plain bottle.
    alternatives: list[dict] = field(default_factory=list)
    #: Same count for the top match. 0 is the loud signal: the catalogue
    #: proposed a bottle whose distinguishing word nobody ever wrote.
    corpus_mentions: int = -1
    #: A couple of real comment spans using this mention, so the reviewer
    #: can see how people meant it. The dangerous case is flankers — a
    #: house ships Layton and Layton Exclusif, Khamrah and Khamrah Qahwa,
    #: Club de Nuit Intense Man and four siblings — and a fuzzy search
    #: returns whichever it likes. What people wrote is the only evidence
    #: of which one they meant.
    examples: list[str] = field(default_factory=list)
    #: Set by a human during review. Nothing is written without it.
    approved: bool | None = None
    note: str = ""


def _kept(record: dict) -> dict:
    """Strip a catalogue row to the three fields the product may store."""
    return {f: record.get(f) for f in KEPT_FIELDS}


def distinguishing_words(mention: str, candidate: str) -> list[str]:
    """The words in a catalogue name that the mention does not have.

    For mention "Club de Nuit" against "Club de Nuit Sillage" this is
    ["sillage"] — the word that makes it a different bottle. Flankers are
    the failure that actually happens in review, and the extra word is
    what separates them.
    """
    mention_words = set(normalize_name(mention).split())
    return [w for w in normalize_name(candidate).split() if w not in mention_words]


def corpus_support(
    conn: sqlite3.Connection, words: list[str], *, mention: str = ""
) -> int:
    """Mentions containing the distinguishing words **and the mention**.

    This is the signal that settles a flanker without fragrance knowledge:
    if the catalogue offers "Club de Nuit Sillage" and nobody ever wrote
    "club de nuit sillage", people are not talking about that bottle.

    **The mention is required, and leaving it out was a real bug.** Counting
    a distinguishing word free-floating measures "does this word exist
    anywhere", not "do people use it about *this* fragrance". Measured on
    the live corpus, "exclusif" appears in 29 mentions — Delina Exclusif,
    Club de Nuit Imperial, and others. Only a handful are Layton Exclusif.
    A reviewer following the flanker rule on 29 would have moved plain
    Layton, the corpus's single most-discussed fragrance, to a flanker
    almost nobody mentioned.

    Qualifiers are not always suffixes either — Eau Sauvage, Absolu
    Aventus, Intense Cedrat Boise — so this asks whether the words
    co-occur, not whether one follows the other.
    """
    if not words:
        # No distinguishing word means the catalogue name is the mention.
        return -1
    required = [*words, *(normalize_name(mention).split() if mention else [])]
    rows = conn.execute(
        "SELECT raw_subject_text AS t FROM claims WHERE raw_subject_text IS NOT NULL"
        " UNION ALL "
        "SELECT raw_object_text FROM claims WHERE raw_object_text IS NOT NULL"
    ).fetchall()
    n = 0
    for row in rows:
        normalized = normalize_name(row["t"])
        if all(w in normalized for w in required):
            n += 1
    return n


EXAMPLES_SQL = """
SELECT DISTINCT c.evidence_span AS span
  FROM claims c
 WHERE c.raw_subject_text = :mention OR c.raw_object_text = :mention
 ORDER BY length(c.evidence_span) DESC
 LIMIT 2
"""


def examples_for(conn: sqlite3.Connection, mention: str) -> list[str]:
    """Real spans using this mention, longest first.

    Longest rather than most recent: a longer span carries more context,
    and context is exactly what distinguishes "Club de Nuit" the Intense
    Man from "Club de Nuit" the Sillage.
    """
    return [
        row["span"]
        for row in conn.execute(EXAMPLES_SQL, {"mention": mention})
    ]


def propose_for(
    mention: str, count: int, results: list[dict], *,
    examples: list[str] | None = None,
    support: dict[str, int] | None = None,
) -> Proposal:
    """Build a review row. Pure, so it is testable without a network.

    `support` maps a catalogue name to how often the corpus uses the words
    distinguishing it from the mention — see `corpus_support`.
    """
    proposal = Proposal(mention=mention, count=count,
                        examples=list(examples or []))
    if not results:
        proposal.note = "no catalogue match"
        return proposal
    support = support or {}

    top = _kept(results[0])
    proposal.canonical_name = top["Name"]
    proposal.brand = top["Brand"]
    proposal.year = top["Year"]
    proposal.score = round(similarity(mention, top["Name"] or ""), 3)
    proposal.confident = proposal.score >= CONFIDENT
    proposal.corpus_mentions = support.get(top["Name"], -1)
    proposal.alternatives = [
        {**_kept(r), "corpus_mentions": support.get(r.get("Name"), -1)}
        for r in results[1:4]
    ]

    notes = []
    if not proposal.confident:
        notes.append("name differs from the mention")
    if proposal.corpus_mentions == 0:
        # The decisive one. A flanker whose distinguishing word appears
        # nowhere in the corpus is a bottle nobody was talking about.
        better = [a for a in proposal.alternatives
                  if a["corpus_mentions"] != 0]
        notes.append(
            "nobody in the corpus wrote "
            f"{' '.join(distinguishing_words(mention, top['Name'] or ''))!r}"
            + (f" — see alternatives ({len(better)} better supported)"
               if better else "")
        )
    proposal.note = "; ".join(notes)
    return proposal


def candidates(
    conn: sqlite3.Connection, limit: int, *, min_count: int = 2
) -> list[tuple[str, int]]:
    """Unresolved mentions worth spending a lookup on, most frequent first.

    Pronouns are dropped, and so is anything mentioned once: 75% of
    distinct names appear exactly once and none of them can ever clear a
    3-commenter bar, so a lookup for them buys nothing.
    """
    out = []
    for mention in unresolved_mentions(conn):
        if mention.count < min_count or is_pronoun(mention.text):
            continue
        out.append((mention.text, mention.count))
        if len(out) >= limit:
            break
    return out


def _search(client, key: str, mention: str, *, limit: int = 4) -> list[dict]:
    r = client.get(
        f"{BASE_URL}{SEARCH_PATH}",
        params={"search": mention, "limit": limit},
        headers={"x-api-key": key},
    )
    if r.status_code == 429:
        raise SystemExit(
            "Catalogue quota exhausted. Remaining mentions were not "
            "queried; re-run to continue where this stopped."
        )
    if r.status_code in (401, 403):
        raise SystemExit("Catalogue rejected the API key.")
    if r.status_code == 404:
        return []
    if r.status_code != 200:
        # Never echo the body: some APIs reflect the request, and the key
        # travels in a header that an error page may render.
        log.warning("HTTP %s for %r", r.status_code, mention)
        return []
    payload = r.json()
    return payload if isinstance(payload, list) else payload.get("data", [])


def _propose_one(conn, client, key: str, mention: str, count: int) -> Proposal:
    results = _search(client, key, mention)
    support = {
        r["Name"]: corpus_support(
            conn, distinguishing_words(mention, r["Name"]), mention=mention
        )
        for r in results
        if r.get("Name")
    }
    return propose_for(
        mention,
        count,
        results,
        examples=examples_for(conn, mention),
        support=support,
    )


def propose(
    conn: sqlite3.Connection, out_path: Path, *, limit: int, min_count: int
) -> list[Proposal]:
    """Query the catalogue for unresolved mentions and write a review file."""
    key = os.environ.get("FRAGELLA_API_KEY")
    if not key:
        raise SystemExit(
            "FRAGELLA_API_KEY is not set.\n\n"
            "  export FRAGELLA_API_KEY=your-secret-key\n\n"
            "Use the secret key from the dashboard, not the pub_ widget key."
        )
    try:
        import httpx
    except ImportError as exc:  # pragma: no cover - depends on install
        raise SystemExit("httpx is not installed. Run: uv sync") from exc

    wanted = candidates(conn, limit, min_count=min_count)
    if not wanted:
        raise SystemExit(
            "No unresolved mentions worth a lookup. Either everything "
            "frequent is curated, or --min-count is set too high."
        )

    log.info("Querying %d mentions (1 request each).", len(wanted))
    proposals = []
    with httpx.Client(timeout=30.0) as client:
        for mention, count in wanted:
            proposals.append(
                _propose_one(conn, client, key, mention, count)
            )

    write_review(out_path, proposals)
    return proposals


def write_review(path: Path, proposals: list[Proposal]) -> None:
    """Write the review file. Sorted and stable so a re-run diffs cleanly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [asdict(p) for p in proposals]
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_review(path: Path) -> list[Proposal]:
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Proposal(**r) for r in records]


@dataclass
class ApplyStats:
    added: int = 0
    skipped_unapproved: int = 0
    skipped_existing: int = 0
    rejected: int = 0

    def __str__(self) -> str:
        return (
            f"{self.added} added, {self.rejected} rejected, "
            f"{self.skipped_unapproved} not reviewed, "
            f"{self.skipped_existing} already present"
        )


def apply_review(conn: sqlite3.Connection, proposals: list[Proposal]) -> ApplyStats:
    """Add the approved proposals as fragrances.

    `approved` starts as null and nothing is written until a human sets it.
    An unreviewed file therefore applies nothing, which is the intended
    failure: the catalogue proposes, a person decides.
    """
    stats = ApplyStats()
    existing = {
        normalize_name(row["canonical_name"])
        for row in conn.execute("SELECT canonical_name FROM fragrances")
    }
    for p in proposals:
        if p.approved is None:
            stats.skipped_unapproved += 1
            continue
        if not p.approved:
            stats.rejected += 1
            continue
        if not p.canonical_name:
            log.warning("Approved but has no name: %r", p.mention)
            stats.skipped_unapproved += 1
            continue
        if normalize_name(p.canonical_name) in existing:
            # Still teach the existing entry the mention it missed.
            stats.skipped_existing += 1
            continue
        # The mention is an alias by construction: it is the text real
        # commenters wrote, which is what the resolver has to match.
        add_fragrance(
            conn, p.canonical_name, brand=p.brand, aliases=[p.mention]
        )
        existing.add(normalize_name(p.canonical_name))
        stats.added += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Propose canonical fragrance names from a catalogue."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    prop = sub.add_parser("propose", help="Query the catalogue, write a review file")
    prop.add_argument("out", type=Path, help="Review file to write")
    prop.add_argument("--limit", type=int, default=60,
                      help="Mentions to look up. One request each.")
    prop.add_argument("--min-count", type=int, default=2,
                      help="Skip mentions appearing fewer than N times")

    app = sub.add_parser("apply", help="Add the approved rows as fragrances")
    app.add_argument("review", type=Path)

    for p in (prop, app):
        p.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "propose":
            proposals = propose(
                conn, args.out, limit=args.limit, min_count=args.min_count
            )
            unsure = [p for p in proposals if not p.confident]
            found = [p for p in proposals if p.canonical_name]
            print(f"\n{len(found)}/{len(proposals)} mentions matched the catalogue.")
            print(f"{len(unsure)} need a closer look.\n")
            for p in proposals[:15]:
                mark = "  " if p.confident else "??"
                name = p.canonical_name or "(no match)"
                print(f"{mark} {p.count:>4}x  {p.mention[:26]:<27} -> "
                      f"{p.brand or '?'} / {name}")
            print("\n?? = the catalogue's name differs from what people "
                  "wrote. Read those first.")
            print(f"\nWrote {args.out}. Set \"approved\": true or false on each "
                  f"row, then:\n"
                  f"  python -m fragrance_graph.resolve.enrich apply {args.out}")

        else:
            proposals = read_review(args.review)
            stats = apply_review(conn, proposals)
            conn.commit()
            print(stats)
            if stats.skipped_unapproved and not stats.added:
                print(
                    "\nNothing was added because nothing is approved. Every "
                    'row starts with "approved": null — the catalogue '
                    "proposes, you decide."
                )
            else:
                print("\nNext: resolve.entities backfill")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
