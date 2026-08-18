"""Declared notes: the official half of FACET's dual truth.

Two sources, one table, strict provenance:

- **brand_declared** rows come from `data/curation/declared-notes.jsonl`,
  curated by a person reading the house's own product page. Each row
  carries the page URL and when it was read. The file may not exist yet;
  an absent file imports zero rows and says so, because inventing notes
  is the one thing this module must never do.
- **retailer_declared** rows are extracted from a retailer listing's
  explicit ``Notes:`` list by `extract_note_lists`, at retail-ingest
  time. The extraction takes the LIST and nothing else -- the marketing
  prose around it is the retailer's copyrighted voice and stays out of
  this repository entirely.

Nothing here touches the evidence layer. A declared note is a fact about
a listing, not about how the bottle smells on a wrist; `may_declare`,
the gate, and every evidence count are computed without ever reading
this table.

## Why extraction is this conservative

`extract_note_lists` was written against the nine real Nordstrom
descriptions in the first collection sample, not against imagined
formats. Everything it accepts appeared there; everything it rejects
did too:

- ``Notes : - Top: jasmine, saffron - Middle: ... - Base: ...`` (staged;
  Burberry and Valentino write ``Bottom`` for base)
- ``Notes : Cognac, hazelnut, oak wood`` (flat; stage unknown stays
  ``unspecified`` rather than guessed)
- terminators seen in the wild: ``Made in``, ``Item #``, ``How to use``,
  ``Mood :``, a sentence period
- parentheticals are explanation, not notes ("Ambroxan (amber, dry
  woody and mineral note)") and are stripped BEFORE the comma split so
  their commas cannot shred the list
- a trailing bare digit is a footnote marker ("vanilla 3"), not a note
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

log = logging.getLogger("fragrance_graph.notes")

DECLARED_NOTES_FILE = Path("data/curation/declared-notes.jsonl")

#: Where a "Notes:" list ends. Each of these appeared immediately after a
#: real note list in the first Nordstrom sample.
_TERMINATORS = re.compile(
    r"(?i)\s+(?:made in\b|item\s*#|how to use\b|mood\s*:|core prod|\.\s)"
)

_NOTES_MARKER = re.compile(r"(?i)\bnotes?\s*:\s*")

#: "- Top: a, b" / "- Middle: c" / "- Bottom: d" segments. "heart" is the
#: storage word for middle; "bottom" is Burberry's word for base.
_STAGE = re.compile(r"(?i)-\s*(top|middle|heart|base|bottom)\s*:\s*")

_STAGE_NAMES = {
    "top": "top",
    "middle": "heart",
    "heart": "heart",
    "base": "base",
    "bottom": "base",
}

_PARENTHETICAL = re.compile(r"\([^)]*\)")
_TRADEMARKS = re.compile(r"[™®]")  # ™ ®


@dataclass(frozen=True)
class DeclaredNote:
    raw_note: str
    stage: str  # top | heart | base | unspecified


def _clean_segment(segment: str) -> list[str]:
    """One stage's text -> individual notes. Parentheticals go first so
    their commas cannot split the list; then commas; then per-note trims.
    A 'note' longer than six words is a sentence that leaked past the
    terminators, and refusing it beats storing prose."""
    segment = _PARENTHETICAL.sub("", segment)
    segment = _TRADEMARKS.sub("", segment)
    out = []
    for piece in segment.split(","):
        piece = re.sub(r"\s+", " ", piece).strip(" -–—.;")
        piece = re.sub(r"\s+\d+$", "", piece)  # trailing footnote digit
        if piece and len(piece.split()) <= 6:
            out.append(piece)
    return out


def extract_note_lists(description: str) -> list[DeclaredNote]:
    """The explicit note list from a retailer description, or nothing.

    No marker, no notes -- a description that merely *mentions* notes in
    prose ("with woody ambery floral notes") yields nothing, on purpose:
    prose is opinionated copy, the list is a factual declaration.
    """
    if not description:
        return []
    marker = _NOTES_MARKER.search(description)
    if not marker:
        return []
    tail = description[marker.end():]
    cut = _TERMINATORS.search(tail)
    if cut:
        tail = tail[: cut.start()]

    notes: list[DeclaredNote] = []
    stages = list(_STAGE.finditer(tail))
    if stages:
        for i, m in enumerate(stages):
            end = stages[i + 1].start() if i + 1 < len(stages) else len(tail)
            stage = _STAGE_NAMES[m.group(1).lower()]
            for raw in _clean_segment(tail[m.end():end]):
                notes.append(DeclaredNote(raw, stage))
    else:
        for raw in _clean_segment(tail):
            notes.append(DeclaredNote(raw, "unspecified"))
    return notes


def canonical(raw_note: str) -> str:
    """Conservative: lowercase and tidy. NOT vocabulary collapse --
    'Turkish rose' stays distinct from 'rose'; merging them is a visible
    judgement for renderers, not a silent one for storage."""
    return re.sub(r"\s+", " ", raw_note).strip().lower()


def store_claims(
    conn: psycopg.Connection,
    fragrance_id: int,
    notes: list[DeclaredNote],
    *,
    claim_type: str,
    source_name: str,
    source_url: str = "",
    retrieved_at: str | None = None,
) -> int:
    """Upsert one declarer's notes for one bottle. Returns rows written."""
    written = 0
    for n in notes:
        conn.execute(
            """
            INSERT INTO fragrance_note_claim
                (fragrance_id, raw_note, canonical_note, note_stage,
                 claim_type, source_name, source_url, retrieved_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (fragrance_id, claim_type, source_name,
                         canonical_note, note_stage)
            DO UPDATE SET raw_note = excluded.raw_note,
                          source_url = excluded.source_url,
                          retrieved_at = excluded.retrieved_at
            """,
            (fragrance_id, n.raw_note, canonical(n.raw_note), n.stage,
             claim_type, source_name, source_url, retrieved_at),
        )
        written += 1
    return written


def import_brand_declared(
    conn: psycopg.Connection, path: Path = DECLARED_NOTES_FILE
) -> tuple[int, int]:
    """data/curation/declared-notes.jsonl -> brand_declared rows.

    Row shape: {"fragrance": canonical name, "raw_note": ..., "stage":
    top|heart|base|unspecified, "source_name": ..., "source_url": ...,
    "retrieved_at": ...}. An absent file is zero rows, not an error --
    the file exists once a person has curated something, and this module
    never invents its contents.
    Returns (rows written, rows skipped for an unknown fragrance).
    """
    if not path.exists():
        log.info("no %s; brand-declared notes not curated yet", path)
        return 0, 0
    ids = {
        row["canonical_name"].lower(): row["id"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }
    written = skipped = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        fid = ids.get((record.get("fragrance") or "").lower())
        if fid is None:
            skipped += 1
            log.warning("declared note for unknown fragrance: %r",
                        record.get("fragrance"))
            continue
        written += store_claims(
            conn, fid,
            [DeclaredNote(record["raw_note"],
                          record.get("stage", "unspecified"))],
            claim_type="brand_declared",
            source_name=record.get("source_name", "brand page"),
            source_url=record.get("source_url", ""),
            retrieved_at=record.get("retrieved_at"),
        )
    conn.commit()
    return written, skipped


def declared_for(
    conn: psycopg.Connection, fragrance_id: int
) -> list[dict]:
    """Every declared note for one bottle, brand rows first (the house
    outranks the retailer), then stage order, for the API's official
    block. Data fields, not sentences -- nothing here needs wording."""
    stage_order = {"top": 0, "heart": 1, "base": 2, "unspecified": 3}
    rows = [
        {
            "note": r["raw_note"],
            "stage": r["note_stage"],
            "declared_by": r["claim_type"],
            "source": r["source_name"],
        }
        for r in conn.execute(
            """SELECT raw_note, note_stage, claim_type, source_name
               FROM fragrance_note_claim WHERE fragrance_id = %s""",
            (fragrance_id,),
        )
    ]
    rows.sort(key=lambda r: (r["declared_by"] != "brand_declared",
                             stage_order[r["stage"]], r["note"].lower()))
    return rows


def declared_note_map(conn: psycopg.Connection) -> dict[int, frozenset[str]]:
    """Every fragrance's declared notes, any `claim_type`, as canonical
    strings -- one query for the whole catalogue rather than one per
    bottle, for `recommend._anchor_profile_fallback`, which needs both the
    anchor's declared set and every candidate's."""
    grouped: dict[int, set[str]] = {}
    for row in conn.execute(
        "SELECT fragrance_id, canonical_note FROM fragrance_note_claim"
    ):
        grouped.setdefault(row["fragrance_id"], set()).add(row["canonical_note"])
    return {fid: frozenset(notes) for fid, notes in grouped.items()}


#: Curated note -> axis mapping. Repo-relative for the same reason
#: `pages.BLOCKLIST_PATH` is: it is meant to be opened and argued with, not
#: shipped and forgotten. See the file's own "_comment" key for how each
#: entry was chosen and what did not survive the vocabulary check.
NOTE_AXES_FILE = Path("data/curation/note-axes.json")


def load_note_axes(
    conn: psycopg.Connection, path: Path = NOTE_AXES_FILE
) -> dict[str, frozenset[str]]:
    """axis name -> the notes that count as that axis, re-checked against
    the corpus that exists right now.

    The curated file is authored judgment, committed for review like
    `data/blocklist.txt` -- but the corpus underneath it keeps changing
    (an import can drop a retailer listing, a note can get re-canonicalised),
    so this loader does not trust the file blindly. Every note it lists is
    re-checked against `SELECT DISTINCT canonical_note FROM
    fragrance_note_claim` at load time; anything the corpus no longer
    carries is dropped, and the drop count is logged rather than silently
    swallowed -- the same shape as `pages.load_blocklist`'s missing-file
    warning, for the same reason: "nothing was dropped" and "the check
    never ran" must not look identical in the log.

    A caller asking for an axis this file does not define (a typo, or a
    genuinely uncurated axis) gets back `None` from `.get(axis)` --
    deliberately not an empty set, so the caller can tell "no notes on
    this axis" apart from "no axis by this name at all" and fall back to
    subtracting only the literal word the user typed, logging that it did
    so. See `recommend._anchor_profile_fallback`.

    An absent file logs a warning and returns `{}` -- every axis then
    resolves to "unknown", which is the same safe fallback as a typo.

    `"occasion_priors"` is skipped alongside `"_comment"`: it is a rule
    table over AXIS NAMES (`fragrance_graph.catalog_profile.
    load_occasion_priors` reads it), not a list of literal notes, and
    feeding its value through the per-note vocabulary check below would
    either crash (its value is a dict of dicts, not a list of strings) or
    -- if the shape happened to survive -- silently mis-file a curation
    rule as an unusably-empty note axis. One name, reserved, rather than
    a second `_`-prefixed convention for "metadata that is not the
    comment."
    """
    if not path.exists():
        log.warning("No note-axis mapping at %s -- axis subtraction is a no-op", path)
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    vocabulary = {
        (row["canonical_note"] or "").strip().lower()
        for row in conn.execute("SELECT DISTINCT canonical_note FROM fragrance_note_claim")
    }
    axes: dict[str, frozenset[str]] = {}
    dropped = 0
    for axis, notes in raw.items():
        if axis.startswith("_") or axis == "occasion_priors":
            continue  # "_comment", the occasion-prior rule table, and any
            # future metadata key -- not an axis of literal notes.
        kept = set()
        for note in notes:
            normalized = note.strip().lower()
            # Token containment, not substring and not fuzzy: the axis
            # entry "cinnamon" claims the corpus note "cinnamon bark"
            # (its word tokens include the entry's) but can never claim
            # "ambergris" from "amber" -- one token is not two. Exact
            # matching alone let "cinnamon bark" survive a warm
            # subtraction because the file said only "cinnamon", which
            # is under-subtraction the user notices ("remove the warm
            # notes" did not remove cinnamon).
            entry_tokens = set(normalized.split())
            matched = {
                v for v in vocabulary
                if entry_tokens <= set(v.split())
            }
            if matched:
                kept |= matched
            else:
                dropped += 1
        axes[axis] = frozenset(kept)
    if dropped:
        log.info(
            "%d note-axis entr%s dropped at load: not in the current "
            "fragrance_note_claim vocabulary",
            dropped, "y" if dropped == 1 else "ies",
        )
    return axes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m fragrance_graph.notes")
    parser.add_argument("--db-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("import", help="import brand-declared notes from curation")
    args = parser.parse_args(argv)

    from fragrance_graph.db import get_connection, migrate

    conn = get_connection(args.db_url)
    migrate(conn)
    written, skipped = import_brand_declared(conn)
    print(f"{written} brand-declared note row(s) written, "
          f"{skipped} skipped (unknown fragrance).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
