"""Phase D: one static page per fragrance pair.

    python -m fragrance_graph.pages build --out site/

A page exists to answer one question — *"is X a dupe of Y?"* — with the
sentences real people wrote and links back to them. Nothing here computes
similarity, ranks by anything commercial, or renders an image.

## The gate, and why it is measured on the pair

A page is generated only when the pair clears **both** bars:

    3+ distinct commenters   AND   2+ distinct sources

SPEC records why each exists. Three commenters is the point below which
"people say this" is not a fact about a community; two sources is the guard
against a single comment section, where three people replying to each other
look like three independent observations.

Both bars are measured **on the pair, across every claim type** — not on a
single claim-type row. Rows share people and rows share videos, so gating a
row-scoped source count beside a pair-scoped commenter count would produce
pages headed "5 people across 2 videos" where the two numbers count
different things. That is the same defect SPEC already recorded once, when
per-row commenter counts were being summed by readers into a total no
single fact supported.

It is not cosmetic. On the committed corpus the two scopes disagree on 8 of
21 candidate pairs, and one pair changes gate status: Club de Nuit Imperiale
<-> Delina Exclusif is 3 people across 2 videos, and a row-scoped check
would refuse it on a technicality while its own evidence satisfies exactly
what the bar was written to require.

## What a page may contain

Text, counts, quotes and permalinks. The trust rules in README are enforced
here structurally rather than by review:

- **No imagery.** `render_pair` has no code path that emits `<img`, and
  `test_no_page_can_emit_an_image` asserts it against every generated page.
  Naming a fragrance identifies it; showing a brand's bottle borrows its
  authority.
- **Nothing is ordered or filtered by what it pays.** This module never
  reads `products` or `retailers`. It cannot: the ranking it renders comes
  from `query.similar_to`, which SPEC's trust test already pins to a named
  set of tables that does not include them.
- **Every quote is escaped.** Comment bodies are text other people wrote,
  and they reach the page verbatim by design. `html.escape` runs on every
  interpolated value, quotes included, so a comment containing markup is
  rendered as the characters that person typed rather than executed.

## Output is byte-stable

Regenerating from an unchanged corpus rewrites identical bytes: results are
already sorted by `Related.rank_key`, pairs are emitted in a canonical name
order, and nothing stamps a timestamp into a page. A diff in `site/` means
the corpus changed, which is the only reason a diff there is worth reading.

Pages are **not committed**, for the reason products are not: they cost no
API quota, no money and no human judgement, and they are a pure function of
`data/corpus/`. The corpus is the durable artifact; `site/` is a build
output and is gitignored.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from fragrance_graph.db import DEFAULT_DB_PATH, get_connection, migrate
from fragrance_graph.query import Related, pair_stats, similar_to

log = logging.getLogger("fragrance_graph.pages")

#: Distinct people who must connect a pair before it gets a page. Below
#: this, "people say this" is one person and an echo.
MIN_COMMENTERS = 3

#: Distinct videos those people must span. Three commenters in one comment
#: section is one conversation, not three observations.
MIN_SOURCES = 2

#: How the claim types read in a sentence, and which direction they run.
#: DUPE_OF and SIMILAR_TO are symmetric — "B is a dupe of A" is the same
#: fact whichever bottle you arrived at — so a page states them once.
#: BETTER_THAN is not, and its direction is preserved in the wording.
PHRASING = {
    "DUPE_OF": "called it a dupe of",
    "SIMILAR_TO": "said it smells similar to",
    "BETTER_THAN": "said it beats",
}


@dataclass(frozen=True)
class Pair:
    """Two bottles a page is about, plus the evidence connecting them.

    `left` and `right` are ordered by canonical name so that one pair
    yields one page regardless of which end it was discovered from.
    """

    left: str
    right: str
    left_id: int
    right_id: int
    commenters: int
    sources: int
    #: Rows as `query.similar_to` returned them, read from `left`.
    rows: tuple[Related, ...]

    @property
    def slug(self) -> str:
        return f"{slugify(self.left)}-vs-{slugify(self.right)}"

    @property
    def title(self) -> str:
        return f"{self.left} vs {self.right}"


def slugify(name: str) -> str:
    """A filename that survives a fragrance name.

    Names carry apostrophes, ampersands and accents — `Kilian Angels'
    Share`, `Abercrombie & Fitch Fierce` — none of which belong in a path.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


def qualifying_pairs(
    conn: sqlite3.Connection,
    *,
    min_commenters: int = MIN_COMMENTERS,
    min_sources: int = MIN_SOURCES,
    quotes: int = 3,
) -> list[Pair]:
    """Every pair that clears both bars, each returned once.

    A pair is discovered twice — once from each end — so the canonical
    name order decides which orientation becomes the page, and the second
    sighting is dropped rather than rendered as a near-duplicate page.
    """
    fragrances = {
        row["id"]: row["canonical_name"]
        for row in conn.execute("SELECT id, canonical_name FROM fragrances")
    }

    found: dict[tuple[int, int], Pair] = {}
    for frag_id, name in sorted(fragrances.items(), key=lambda kv: kv[1]):
        for related in similar_to(conn, frag_id, quotes=quotes):
            if related.fragrance_id == frag_id:
                # A claim whose two ends resolved to the same bottle. It is
                # in the corpus — "Cedrat Boise is a dupe of Cedrat Boise"
                # — and it is not a pair, so it can never be a page.
                continue

            other = fragrances.get(related.fragrance_id, related.canonical_name)
            key: tuple[int, int] = (
                min(frag_id, related.fragrance_id),
                max(frag_id, related.fragrance_id),
            )
            if key in found:
                continue
            # Render from whichever end sorts first, so one pair is one
            # page and its wording does not depend on iteration order.
            if name > other:
                continue

            # Both bars are measured on the pair, direction-blind. Reading
            # `related.pair_commenters` here would count only the people
            # visible from this end, which drops an inbound BETTER_THAN and
            # can gate out a pair that clears the bar from the other side.
            commenters, sources = pair_stats(conn, frag_id, related.fragrance_id)
            if commenters < min_commenters or sources < min_sources:
                continue

            rows = tuple(
                r
                for r in similar_to(conn, frag_id, quotes=quotes)
                if r.fragrance_id == related.fragrance_id
            )
            found[key] = Pair(
                left=name,
                right=other,
                left_id=frag_id,
                right_id=related.fragrance_id,
                commenters=commenters,
                sources=sources,
                rows=rows,
            )

    return sorted(found.values(), key=lambda p: (-p.commenters, -p.sources, p.slug))


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def _sources(n: int) -> str:
    return "1 video" if n == 1 else f"{n} videos"


def render_pair(pair: Pair) -> str:
    """One page, as a complete HTML document.

    Every interpolated value goes through `html.escape`, including the
    quotes, which are the whole point of the page and are also the one
    field written by someone other than us.
    """
    e = html.escape
    out: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{e(pair.title)}</title>",
        "</head>",
        "<body>",
        f"<h1>{e(pair.title)}</h1>",
        f"<p>{e(_people(pair.commenters))} connected these two fragrances, "
        f"across {e(_sources(pair.sources))}. Every line below is quoted from "
        f"a comment, and links back to it.</p>",
    ]

    for row in pair.rows:
        phrase = PHRASING.get(row.claim_type, row.claim_type)
        out.append("<section>")
        out.append(
            f"<h2>{e(_people(row.commenters))} {e(phrase)} "
            f"{e(row.canonical_name)}</h2>"
        )
        if row.claim_type == "BETTER_THAN":
            # A preference runs one way. Saying it symmetrically would turn
            # a bottle's critics into its recommendations.
            out.append(
                f"<p>Stated about {e(pair.left)}, not about "
                f"{e(row.canonical_name)}.</p>"
            )
        out.append("<ul>")
        for ev in row.evidence:
            said_about = pair.left if ev.outbound else row.canonical_name
            out.append(
                "<li>"
                f"<blockquote>{e(ev.quote)}</blockquote>"
                f'<p>Written about {e(said_about)}. '
                f'<a href="{e(ev.permalink)}" rel="nofollow noopener">'
                "read the comment</a></p>"
                "</li>"
            )
        out.append("</ul>")
        out.append("</section>")

    out.append(
        "<footer><p>Similarity here is asserted by people, never computed. "
        "This page counts distinct commenters and shows what they wrote; "
        "nothing on it is ordered by any commercial relationship.</p>"
        '<p><a href="index.html">All comparisons</a></p></footer>'
    )
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def render_index(pairs: list[Pair]) -> str:
    """The list of pages, in the order the pages themselves are ranked."""
    e = html.escape
    out = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Fragrance comparisons</title>",
        "</head>",
        "<body>",
        "<h1>Fragrance comparisons</h1>",
        f"<p>{len(pairs)} comparison"
        f"{'' if len(pairs) == 1 else 's'}, each backed by at least "
        f"{MIN_COMMENTERS} people writing across at least {MIN_SOURCES} "
        "videos.</p>",
        "<ul>",
    ]
    for p in pairs:
        out.append(
            f'<li><a href="{e(p.slug)}.html">{e(p.title)}</a> — '
            f"{e(_people(p.commenters))} across {e(_sources(p.sources))}</li>"
        )
    out += ["</ul>", "</body>", "</html>"]
    return "\n".join(out) + "\n"


def build(
    conn: sqlite3.Connection,
    out_dir: Path,
    *,
    min_commenters: int = MIN_COMMENTERS,
    min_sources: int = MIN_SOURCES,
) -> list[Pair]:
    """Write a page per qualifying pair, plus an index. Returns what it wrote."""
    pairs = qualifying_pairs(
        conn, min_commenters=min_commenters, min_sources=min_sources
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    for pair in pairs:
        (out_dir / f"{pair.slug}.html").write_text(render_pair(pair), encoding="utf-8")
    (out_dir / "index.html").write_text(render_index(pairs), encoding="utf-8")
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate static comparison pages.")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Write one page per qualifying pair")
    b.add_argument("--out", default="site", type=Path)
    b.add_argument("--min-commenters", type=int, default=MIN_COMMENTERS)
    b.add_argument("--min-sources", type=int, default=MIN_SOURCES)

    p = sub.add_parser("pairs", help="List qualifying pairs without writing anything")
    p.add_argument("--min-commenters", type=int, default=MIN_COMMENTERS)
    p.add_argument("--min-sources", type=int, default=MIN_SOURCES)

    for parser_ in (b, p):
        parser_.add_argument("--db-path", default=DEFAULT_DB_PATH)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_path)
    migrate(conn)
    try:
        if args.command == "pairs":
            pairs = qualifying_pairs(
                conn,
                min_commenters=args.min_commenters,
                min_sources=args.min_sources,
            )
            for pair in pairs:
                print(
                    f"  {pair.commenters:>3} people  "
                    f"{pair.sources} sources  {pair.title}"
                )
            print(f"\n{len(pairs)} pair(s) clear the gate.")
            return 0

        pairs = build(
            conn,
            Path(args.out),
            min_commenters=args.min_commenters,
            min_sources=args.min_sources,
        )
        for pair in pairs:
            print(f"  {pair.slug}.html  ({pair.commenters} people, {pair.sources} sources)")
        print(f"\nWrote {len(pairs)} page(s) + index to {args.out}/")
        if not pairs:
            print(
                "\nNothing cleared the gate. That is the gate working: a pair "
                "needs both ends curated, then 3 people across 2 videos. "
                "`resolve.entities report` ranks what to curate next."
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
