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

**A source is a creator, not a video.** `comments.source_channel` holds the
uploading channel, so the bar is two distinct channels — every video by one
YouTuber counts once. This page called them "videos" until 2026-08-11,
which understated the bar to the reader: two videos by one creator are one
audience, and the guard is meant to be about independence rather than about
upload count. Counting creators is the stronger reading and the one the
code has always implemented; only the wording was wrong.

Both bars are measured **on the pair, across every claim type** — not on a
single claim-type row. Rows share people and rows share creators, so gating
a row-scoped source count beside a pair-scoped commenter count would produce
pages headed "5 people across 2 creators" where the two numbers count
different things. That is the same defect SPEC already recorded once, when
per-row commenter counts were being summed by readers into a total no
single fact supported.

It is not cosmetic. On the committed corpus the two scopes disagree on 8 of
21 candidate pairs, and one pair changes gate status: Club de Nuit Imperiale
<-> Delina Exclusif is 3 people across 2 creators, and a row-scoped check
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
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection, migrate
from fragrance_graph.gate import (
    MIN_COMMENTERS,
    MIN_FLANKER_COMMENTERS,
    MIN_FLANKER_SOURCES,
    MIN_QUERIES,
    MIN_SOURCES,
)
from fragrance_graph.query import Related, pair_stats, similar_to
from fragrance_graph.resolve.enrich import debranded
from fragrance_graph.resolve.names import normalize_name

#: Re-exported so `from fragrance_graph.pages import MIN_COMMENTERS` keeps
#: working; `gate.py` is where they are defined and explained.
__all__ = [
    "MIN_COMMENTERS",
    "MIN_FLANKER_COMMENTERS",
    "MIN_FLANKER_SOURCES",
    "MIN_QUERIES",
    "MIN_SOURCES",
    "Bottle",
    "Pair",
    "Statement",
    "build",
    "held_as_flankers",
    "qualifying_pairs",
    "render_index",
    "render_pair",
    "verdict",
]

log = logging.getLogger("fragrance_graph.pages")

#: How the claim types read in a sentence, with both bottles named.
#:
#: DUPE_OF and SIMILAR_TO are symmetric as facts — "B is a dupe of A" is
#: the same claim whichever bottle you arrived at — so a page states them
#: once. They are not symmetric as English, which is what these are for.
#:
#: These said "called **it** a dupe of X" until 2026-08-12, leaving "it" to
#: be inferred from the page title. That is fine directly under a title and
#: wrong in a verdict line, which is the first sentence a stranger reads —
#: and it hid a worse problem: "it" always meant the alphabetically first
#: bottle, so a page could render six people's "Dusk is a dupe of Layton"
#: as *Layton* being the dupe. Naming both ends forces the direction to be
#: decided rather than assumed.
#:
#: The verb is stored apart from the rest so it can agree with the count in
#: front of it: a section can be one person, and "1 person call X a dupe of
#: Y" is the kind of sentence that tells a reader a machine wrote the page.
PHRASING = {
    "DUPE_OF": ("call", "{subject} a dupe of {target}"),
    "SIMILAR_TO": ("say", "{subject} smells similar to {target}"),
    "BETTER_THAN": ("say", "{subject} is better than {target}"),
}

#: Tie-break order when two claim types are backed by the same number of
#: people. Strongest claim first, so a tie between "a dupe of" and "similar
#: to" leads with the one that says more; the list exists mostly so the
#: sentence does not depend on dict iteration order.
VERDICT_PRECEDENCE = ("DUPE_OF", "BETTER_THAN", "SIMILAR_TO")

#: Below this, a claim type is one person's opinion. The verdict refuses to
#: lead with it — see `verdict`.
VERDICT_MIN_COMMENTERS = 2


#: What the site is, for someone who arrived from a search engine and has
#: no idea who made this or why to believe it.
#:
#: Written to be read once and understood: what the pages are made of,
#: where the sentences came from, what has to be true before a page exists
#: at all, and what is deliberately absent. No adjectives about the
#: fragrances and none about the site — a page that had to describe itself
#: as reliable would be admitting it is not.
WHAT_THIS_IS = (
    "<p>Every page here answers one question — do people think these two "
    "fragrances smell alike? — using comments left under fragrance videos "
    "on YouTube. Nothing is computed and nothing is a review. If a page "
    "says six people called something a dupe, six different people wrote "
    "it, and every quote links back to the comment so you can read it in "
    "context.</p>"
    "<p>A comparison only gets a page once at least "
    f"{MIN_COMMENTERS} different people have made it, across videos by at "
    f"least {MIN_SOURCES} different creators. Three people replying to each "
    "other under one video are one conversation, not three opinions. Most "
    "pairs do not clear that bar, which is why this list is short.</p>"
    "<p>There are no notes lists, no ratings, no scores and no shopping "
    "links. Nothing on any page is ordered by a commercial relationship, "
    "and the quotes are shown as people typed them.</p>"
)


@dataclass(frozen=True)
class Bottle:
    """A fragrance as a page introduces it: what it is called, by whom, when.

    A stranger arriving from a search engine may not know that Detour Noir
    is an Al Haramain, or that Layton is nine years old. Both are facts
    about the bottle rather than about its smell, which is the line SPEC
    draws: notes and accords stay off these pages because we have no
    evidence for them, and a house and a year are catalogue records.

    Everything here is optional, and a missing field is simply not shown.
    `house_year` is null for all 56 curated fragrances today — nothing in
    the pipeline collects it — so no page renders a year yet. Inventing one
    would be the fabrication this project refuses everywhere else; the
    field is filled by hand during curation, and the pages take it the day
    it exists.
    """

    name: str
    brand: str | None = None
    year: int | None = None

    @property
    def described(self) -> str:
        """"Atomic Rose (Initio Parfums Prives, 2020)", minus what we lack.

        The brand is dropped when the name already carries it, which is 54
        of the 56 curated fragrances — "Parfums de Marly Layton (Parfums de
        Marly)" reads like a rendering bug, and it is one.
        """
        facts = []
        if self.brand and self.brand.lower() not in self.name.lower():
            facts.append(self.brand)
        if self.year:
            facts.append(str(self.year))
        return f"{self.name} ({', '.join(facts)})" if facts else self.name


def bottle_facts(
    conn: psycopg.Connection, casing: dict[str, str] | None = None
) -> dict[str, Bottle]:
    """Every curated fragrance, keyed by the name a page will display."""
    casing = brand_casing(conn) if casing is None else casing
    out: dict[str, Bottle] = {}
    for row in conn.execute(
        "SELECT canonical_name, brand, house_year FROM fragrances"
    ):
        name = with_canonical_casing(row["canonical_name"], casing)
        brand = row["brand"]
        out[name] = Bottle(
            name=name,
            brand=with_canonical_casing(brand, casing) if brand else None,
            year=row["house_year"],
        )
    return out


def is_flanker_pair(a: Bottle | None, b: Bottle | None) -> bool:
    """Whether two bottles are one house's own line compared to itself.

    Same brand, and their names overlap once the house is removed. The
    overlap is what separates a flanker from a stablemate: Layton and
    Layton Exclusif share "layton" and are the same fragrance twice, while
    Layton and Delina Exclusif are two unrelated Parfums de Marly bottles
    and get the ordinary gate.

    One shared word rather than a prefix rule, so that two flankers of one
    parent are caught as well — Club de Nuit Intense Man against Club de
    Nuit Sillage is neither one's parent, and it is exactly as easy to
    confuse.

    A missing brand means no: with nothing to compare houses on, this
    would raise the bar on pairs it has no evidence about.
    """
    if a is None or b is None or not a.brand or not b.brand:
        return False
    if a.brand.casefold() != b.brand.casefold():
        return False
    words_a = set(debranded(a.name, a.brand).split())
    words_b = set(debranded(b.name, b.brand).split())
    return bool(words_a & words_b)


@dataclass(frozen=True)
class Statement:
    """One claim type connecting the pair, pointed the way people said it.

    `query.similar_to` answers "what relates to X", so every row it returns
    is oriented towards whichever end it was asked from. A pair page is
    about both ends at once and has to put the subject back where the
    commenters put it.
    """

    #: The end `similar_to` was called from. `Related.outbound` is relative
    #: to this, and so is "written about" on each quote.
    asked: str
    #: The end its rows point at.
    other: str
    row: Related

    @property
    def subject(self) -> str:
        """Whichever bottle the majority of these claims were made *about*.

        BETTER_THAN only ever arrives outbound — `similar_to` refuses to
        return "things that beat X" when asked about X — so its direction
        is already `asked` beats `other`. The symmetric types collapse both
        directions into one row, and there the majority decides: six people
        wrote "Dusk is a dupe of Layton" and none wrote the reverse.

        A tie keeps `asked` in front, so the sentence does not depend on
        which end the page was built from.
        """
        if self.row.claim_type == "BETTER_THAN":
            return self.asked
        if self.row.outbound_claims * 2 < self.row.claims:
            return self.other
        return self.asked

    @property
    def target(self) -> str:
        return self.other if self.subject == self.asked else self.asked

    @property
    def sentence(self) -> str:
        """"call Detour Noir a dupe of Layton" — the count goes in front.

        Conjugated for however many people said it, so the caller can put
        "9 people" or "1 person" ahead of it and get English either way.
        """
        phrasing = PHRASING.get(self.row.claim_type)
        if phrasing is None:
            return (
                f"made a {self.row.claim_type} claim about "
                f"{self.subject} and {self.target}"
            )
        verb, rest = phrasing
        if self.row.commenters == 1:
            verb += "s"
        return f"{verb} {rest.format(subject=self.subject, target=self.target)}"

    @property
    def rank_key(self) -> tuple:
        return (-self.row.commenters, -self.row.claims, self.subject, self.target)


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
    #: Distinct uploading channels. Named `sources` because that is what
    #: the gate has always been called; it counts creators, not uploads.
    #: Distinct searches that retrieved the backing videos. Reported, and
    #: not yet gated on — see `MIN_QUERIES`.
    queries: int
    #: Backing videos with no retrieval record. While non-zero, `queries`
    #: is a lower bound and must be reported as one.
    unprovenanced: int
    #: Rows as `query.similar_to` returned them, read from `left`.
    rows: tuple[Related, ...]
    #: BETTER_THAN read from `right` — i.e. people saying the right-hand
    #: bottle wins. `similar_to` cannot return these from `left`, by
    #: design: asked "what relates to Layton", it must not hand back
    #: "things that beat Layton" as if they were recommendations for it.
    #:
    #: A pair page asks a different question, and without these it was
    #: dropping evidence and then saying so out loud — the Layton/Dusk page
    #: held two people preferring Dusk while its own verdict line said
    #: nobody had said which they preferred.
    reverse: tuple[Related, ...] = ()

    @property
    def statements(self) -> tuple[Statement, ...]:
        """Every claim type on this pair, strongest support first."""
        both = [Statement(self.left, self.right, r) for r in self.rows]
        both += [Statement(self.right, self.left, r) for r in self.reverse]
        return tuple(sorted(both, key=lambda s: s.rank_key))

    @property
    def slug(self) -> str:
        return f"{slugify(self.left)}-vs-{slugify(self.right)}"

    @property
    def title(self) -> str:
        return f"{self.left} vs {self.right}"


#: The editorial blocklist. Repo-relative rather than package-relative
#: because it is meant to be opened and argued with, not shipped and
#: forgotten.
BLOCKLIST_PATH = Path(__file__).resolve().parents[2] / "data" / "blocklist.txt"


def load_blocklist(path: Path | None = None) -> frozenset[str]:
    """Words that keep a quote off a page.

    A missing file logs a warning rather than quietly disabling the
    filter: "no crude quotes were hidden" and "the filter never ran" look
    identical on the output, and only one of them is fine.
    """
    path = BLOCKLIST_PATH if path is None else path
    if not path.exists():
        log.warning("No blocklist at %s — quotes are unfiltered", path)
        return frozenset()
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            words.add(normalize_name(line))
    words.discard("")
    return frozenset(words)


def crude_words(text: str, blocklist: frozenset[str]) -> list[str]:
    """Which blocked words a comment contains, whole-word after normalising.

    Whole words only. Substring matching turns "assessment" and "hello"
    into profanity, and a filter that hides ordinary sentences is worse
    than no filter — it removes evidence for a reason nobody can see.
    """
    if not blocklist:
        return []
    return sorted(set(normalize_name(text).split()) & blocklist)


def hidden_quotes(
    pair: Pair, blocklist: frozenset[str]
) -> list[tuple[str, list[str]]]:
    """Every quote this pair's page will not print, and why.

    Separate from rendering so the build can report it. Nothing here is
    dropped silently: `pages build` prints this list, and the page itself
    says a comment was left out.
    """
    return [
        (ev.permalink, words)
        for statement in pair.statements
        for ev in statement.row.evidence
        if (words := crude_words(ev.quote, blocklist))
    ]


#: A snippet injected into every page's <head>, or nothing. See the file.
ANALYTICS_PATH = Path(__file__).resolve().parents[2] / "data" / "analytics.html"

#: The custom domain, if the site has one. GitHub Pages reads this file
#: from the published root, so `build` copies it into the output.
CNAME_PATH = Path(__file__).resolve().parents[2] / "CNAME"


def load_analytics(path: Path | None = None) -> str:
    """The analytics snippet, or an empty string.

    Comments are stripped, so a file containing only the explanation at
    the top of `data/analytics.html` counts as off. That is the shipped
    state, and it means "off" is a property of the file's content rather
    than of a flag somebody has to remember not to pass.
    """
    path = ANALYTICS_PATH if path is None else path
    if not path.exists():
        return ""
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    return text.strip()


def custom_domain(path: Path | None = None) -> str:
    """The domain in CNAME, or "" — the same file GitHub Pages reads."""
    path = CNAME_PATH if path is None else path
    if not path.exists():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    return ""


def site_base(base_url: str = "", cname: Path | None = None) -> str:
    """Where these pages will live, with a trailing slash, or "".

    A CNAME wins over `--base-url`: it is the file the host itself obeys,
    so if the two disagree the canonical tags would point at a domain that
    redirects to the other one. Empty means unknown, and unknown means no
    canonical tags and no sitemap rather than a guessed absolute URL —
    a wrong canonical tag tells a search engine to index someone else's
    copy of the page.
    """
    domain = custom_domain(cname)
    if domain:
        return f"https://{domain}/"
    return f"{base_url.rstrip('/')}/" if base_url else ""


def _head(
    title: str,
    *,
    canonical: str = "",
    analytics: str = "",
) -> list[str]:
    """The <head> every page shares."""
    out = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(title)}</title>",
    ]
    if canonical:
        out.append(f'<link rel="canonical" href="{html.escape(canonical)}">')
    if analytics:
        out.append(analytics)
    out.append("</head>")
    return out


def render_sitemap(base: str, slugs: list[str]) -> str:
    """A sitemap of every page, in a stable order.

    No `lastmod`. The build is a pure function of the corpus and rewrites
    identical bytes when nothing changed; stamping a date in would make
    every rebuild a diff and turn "the corpus moved" into a signal nobody
    trusts.
    """
    e = html.escape
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for slug in sorted(slugs):
        out.append(f"  <url><loc>{e(base + slug)}</loc></url>")
    out.append("</urlset>")
    return "\n".join(out) + "\n"


def render_robots(base: str) -> str:
    """Everything is crawlable; the sitemap is named when we know where it is."""
    lines = ["User-agent: *", "Allow: /"]
    if base:
        lines.append(f"Sitemap: {base}sitemap.xml")
    return "\n".join(lines) + "\n"


#: Pairs a bottle must have before it gets a page of its own. One pair is
#: a page that exists to link to a single other page, which is a worse
#: version of the pair page and a page a search engine has no reason to
#: prefer over it.
MIN_REVERSE_PAIRS = 2


@dataclass(frozen=True)
class ReverseIndex:
    """Everything the community compares to one bottle, in one place.

    "Layton dupes" is a question people actually type, and the pair pages
    answer it one bottle at a time. This is the page that answers it as a
    list — which is also the shape of the question, since somebody
    shopping wants the field rather than a single comparison.

    Nothing new is claimed here. Every row is a pair that already cleared
    the gate, ranked by the same count the pair page states.
    """

    bottle: str
    pairs: tuple[Pair, ...]

    @property
    def slug(self) -> str:
        return f"dupes-of-{slugify(self.bottle)}"

    @property
    def rows(self) -> tuple[tuple[Pair, str, Statement], ...]:
        """Each pair, the *other* bottle in it, and its leading claim.

        Ordered by the number this page actually prints. Sorting by the
        pair-wide commenter count instead put "4 people" above "6 people",
        because the pair total counts everyone who connected the two
        bottles and the line quotes the claim type — two different scopes,
        one of them invisible to the reader.
        """
        out = []
        for pair in self.pairs:
            other = pair.right if pair.left == self.bottle else pair.left
            statements = pair.statements
            if statements:
                out.append((pair, other, statements[0]))
        return tuple(sorted(out, key=lambda r: (-r[2].row.commenters, r[1])))

    @property
    def calls_it_a_dupe(self) -> bool:
        """Whether anyone called anything a dupe *of this bottle*.

        The title turns on it. "Dupes of Layton" is the phrase people
        search, and it is also a claim: writing it over a list of bottles
        nobody called a dupe would be inventing the one word that matters.
        """
        return any(
            s.row.claim_type == "DUPE_OF" and s.target == self.bottle
            for pair in self.pairs
            for s in pair.statements
        )

    @property
    def title(self) -> str:
        if self.calls_it_a_dupe:
            return f"Dupes of {self.bottle}"
        return f"Fragrances people compare to {self.bottle}"


def reverse_indexes(
    pairs: list[Pair], *, min_pairs: int = MIN_REVERSE_PAIRS
) -> list[ReverseIndex]:
    """One index per bottle with enough qualifying pairs to be worth it."""
    by_bottle: dict[str, list[Pair]] = {}
    for pair in pairs:
        by_bottle.setdefault(pair.left, []).append(pair)
        by_bottle.setdefault(pair.right, []).append(pair)

    return sorted(
        (
            ReverseIndex(
                bottle=bottle,
                pairs=tuple(sorted(found, key=lambda p: (-p.commenters, p.slug))),
            )
            for bottle, found in by_bottle.items()
            if len(found) >= min_pairs
        ),
        key=lambda r: (-len(r.pairs), r.bottle),
    )


def brand_casing(conn: psycopg.Connection) -> dict[str, str]:
    """The casing each brand is written in, by majority of curated rows.

    Curation is many sessions by hand and by catalogue, so a house arrives
    spelled more than one way: "Parfums de Marly" fourteen times and
    "Parfums De Marly" once. One page title carried both at the same time,
    which reads as two different houses.

    The majority wins rather than a rule about which words to capitalise,
    because houses do not agree with each other — "de" is lowercase in
    Parfums de Marly and "By" is capitalised in By Kilian. Only the corpus
    knows, and it is nearly unanimous; a tie falls back to sorted order so
    the output does not depend on row order.

    Applied at render time. The stored rows are left alone: fixing them is
    a curation decision with a merge behind it, and this is a display
    concern.
    """
    seen: dict[str, Counter] = {}
    for row in conn.execute(
        "SELECT brand FROM fragrances WHERE brand IS NOT NULL AND brand != ''"
    ):
        seen.setdefault(row["brand"].lower(), Counter())[row["brand"]] += 1
    out: dict[str, str] = {}
    for key, counts in seen.items():
        best = max(counts.items(), key=lambda kv: (kv[1], kv[0]))
        out[key] = best[0]
    return out


def with_canonical_casing(name: str, casing: dict[str, str]) -> str:
    """Rewrite any brand inside `name` to its majority casing.

    Longest brand first, so "Parfums de Marly" is matched before a shorter
    brand that happens to be a prefix of it.
    """
    for key in sorted(casing, key=len, reverse=True):
        lowered = name.lower()
        start = lowered.find(key)
        if start != -1:
            return name[:start] + casing[key] + name[start + len(key):]
    return name


def slugify(name: str) -> str:
    """A filename that survives a fragrance name.

    Names carry apostrophes, ampersands and accents — `Kilian Angels'
    Share`, `Abercrombie & Fitch Fierce` — none of which belong in a path.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unnamed"


def qualifying_pairs(
    conn: psycopg.Connection,
    *,
    min_commenters: int = MIN_COMMENTERS,
    min_sources: int = MIN_SOURCES,
    min_queries: int = MIN_QUERIES,
    min_flanker_commenters: int = MIN_FLANKER_COMMENTERS,
    min_flanker_sources: int = MIN_FLANKER_SOURCES,
    quotes: int = 3,
) -> list[Pair]:
    """Every pair that clears its bars, each returned once.

    A pair is discovered twice — once from each end — so the canonical
    name order decides which orientation becomes the page, and the second
    sighting is dropped rather than rendered as a near-duplicate page.

    A house compared against its own line clears a higher bar; see
    `MIN_FLANKER_COMMENTERS` for why, and `held_as_flankers` for what that
    costs on the current corpus.
    """
    # Casing is normalised here, at the single point every displayed name
    # comes from, so a title, a heading and a "written about" line cannot
    # disagree with each other about how a house is spelled.
    casing = brand_casing(conn)
    facts = bottle_facts(conn, casing)
    fragrances = {
        row["id"]: with_canonical_casing(row["canonical_name"], casing)
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
            ev = pair_stats(conn, frag_id, related.fragrance_id)
            # Gate on distinct *creators*, not distinct videos. Two uploads
            # by one YouTuber are one audience framed by one person, which
            # is the independence SPEC asked for. Pages have said
            # "creators" since 2026-08-11; until now the number behind that
            # word counted videos, which are equal on this corpus for every
            # publishable pair and diverge the moment one creator backs a
            # pair twice.
            if ev.commenters < min_commenters or ev.creators < min_sources:
                continue
            if is_flanker_pair(facts.get(name), facts.get(other)) and (
                ev.commenters < min_flanker_commenters
                or ev.creators < min_flanker_sources
            ):
                continue
            # Query diversity is reported but only gates when asked for.
            # A pair with no retrieval record has queries == 0, which means
            # "unknown", not "narrow" — gating on it by default would
            # silently unpublish every pair whose videos predate
            # provenance tracking.
            if min_queries > 1 and 0 < ev.queries < min_queries:
                continue

            rows = _rows_between(
                conn, casing, frag_id, related.fragrance_id, quotes=quotes
            )
            # The other end's BETTER_THAN, which `similar_to` will only
            # hand over when asked from that side. Nothing else is taken
            # from there: the symmetric types already carry both directions
            # and would arrive twice.
            reverse = _rows_between(
                conn, casing, related.fragrance_id, frag_id, quotes=quotes,
                only=("BETTER_THAN",),
            )
            found[key] = Pair(
                left=name,
                right=other,
                left_id=frag_id,
                right_id=related.fragrance_id,
                commenters=ev.commenters,
                sources=ev.creators,
                queries=ev.queries,
                unprovenanced=ev.videos_without_provenance,
                rows=rows,
                reverse=reverse,
            )

    return sorted(found.values(), key=lambda p: (-p.commenters, -p.sources, p.slug))


def held_as_flankers(conn: psycopg.Connection, **kwargs) -> list[Pair]:
    """Pairs the general gate would publish and the flanker bar holds back.

    Raising a bar is a decision about which pages stop existing, and that
    is not something to take on the argument alone — so the cost is
    computed rather than asserted, and the CLI prints it every time.
    """
    lenient = qualifying_pairs(
        conn, **{**kwargs, "min_flanker_commenters": 0, "min_flanker_sources": 0}
    )
    published = {p.slug for p in qualifying_pairs(conn, **kwargs)}
    return [p for p in lenient if p.slug not in published]


def _rows_between(
    conn: psycopg.Connection,
    casing: dict[str, str],
    asked_id: int,
    other_id: int,
    *,
    quotes: int,
    only: tuple[str, ...] | None = None,
) -> tuple[Related, ...]:
    """`similar_to(asked)` narrowed to one other bottle, names normalised.

    Headings read the stored name directly, so they need the same casing
    pass as the title — otherwise one page says "Parfums de Marly Layton"
    in its title and "Parfums De Marly Layton" in a heading.
    """
    return tuple(
        replace(
            r,
            canonical_name=with_canonical_casing(r.canonical_name, casing),
            brand=(with_canonical_casing(r.brand, casing) if r.brand else r.brand),
        )
        for r in similar_to(conn, asked_id, quotes=quotes)
        if r.fragrance_id == other_id and (only is None or r.claim_type in only)
    )


def queries_behind(conn: psycopg.Connection, pair: Pair) -> list[str]:
    """The searches that retrieved the videos backing a pair.

    Reporting only. The count is what a gate would read; the names are what
    tells you whether a low count means weak evidence or narrow seeding.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT d.retrieval_query AS q
          FROM claims c
          JOIN comments co ON co.id = c.comment_id
          JOIN video_discoveries d
            ON d.source = co.source AND d.video_id = co.video_id
         WHERE c.evidence_verified = 1 AND c.polarity = 'ASSERTED'
           AND ((c.subject_frag_id = %(a)s AND c.object_frag_id = %(b)s)
             OR (c.subject_frag_id = %(b)s AND c.object_frag_id = %(a)s))
         ORDER BY d.retrieval_query
        """,
        {"a": pair.left_id, "b": pair.right_id},
    )
    return [r["q"] for r in rows]


def _people(n: int) -> str:
    return "1 person" if n == 1 else f"{n} people"


def _sources(n: int) -> str:
    return "1 creator" if n == 1 else f"{n} creators"


def verdict(pair: Pair) -> str:
    """What the page found, and what it did not, in two sentences.

    A stranger arriving from a search engine reads one line before
    deciding whether the rest is worth their time. Until now that line was
    "5 people connected these two fragrances", which is a fact about our
    corpus rather than an answer to their question.

    Both sentences are generated from counts. Nothing here weighs, hedges
    or interprets: the first names the claim the most people made and how
    many made it, the second names what nobody said. The register is
    deliberately flat — "9 people across 6 creators call Detour Noir a dupe
    of Layton" is a countable statement, "Detour Noir is a convincing dupe"
    is not, and this file must never be able to produce the second.

    Two rules keep it honest:

    - **People and creators are counted over the same rows.** The pair-wide
      creator count is larger than the one behind a single claim type, and
      quoting it beside a claim-type commenter count would make the line
      read as more independent than it is.
    - **A claim only one person made is never the verdict.** Below
      `VERDICT_MIN_COMMENTERS` the page says so plainly instead of picking a
      winner out of a field of ones — "3 people said three different
      things" is the true summary there, and it is also the useful one.
    """
    statements = pair.statements
    if not statements:
        # Not reachable through the gate, which requires evidence. Kept so
        # the function is total: a caller rendering an arbitrary Pair gets
        # a truthful sentence rather than an IndexError.
        return (
            f"Nobody in this corpus compared {pair.left} and {pair.right}."
        )

    ranked = sorted(
        statements,
        key=lambda s: (
            -s.row.commenters,
            VERDICT_PRECEDENCE.index(s.row.claim_type)
            if s.row.claim_type in VERDICT_PRECEDENCE
            else len(VERDICT_PRECEDENCE),
            s.row.claim_type,
            s.subject,
        ),
    )
    top = ranked[0]

    if top.row.commenters < VERDICT_MIN_COMMENTERS:
        return (
            f"{_people(pair.commenters)} across {_sources(pair.sources)} "
            f"mentioned {pair.left} and {pair.right} together, but no two of "
            "them made the same comparison. Nothing on this page was said by "
            "more than one person."
        )

    return (
        f"{_people(top.row.commenters)} across {_sources(top.row.creators)} "
        f"{top.sentence}. {_absent(pair, top)}"
    )


def _contradiction(pair: Pair, top: Statement) -> Statement | None:
    """A preference stated the other way round, if anyone stated one.

    Four people preferring Khamrah is a different fact when two people
    prefer Angels' Share, and a verdict that reports only the four has
    picked a side by omission. Both directions reach the page now — before
    2026-08-12 the reverse one was dropped before rendering — so the
    headline sentence has to account for the disagreement.
    """
    if top.row.claim_type != "BETTER_THAN":
        return None
    return next(
        (
            s
            for s in pair.statements
            if s.row.claim_type == "BETTER_THAN" and s.subject != top.subject
        ),
        None,
    )


def _absent(pair: Pair, top: Statement) -> str:
    """The half of the verdict that says what the evidence does not show.

    Derived from which claim types are present, never from reading the
    quotes. Each branch is a statement about the comments on this page, so
    it stays true no matter what else exists in the corpus — which is why
    it reads `statements` rather than `rows`. Reading `rows` looked
    equivalent and was not: it cannot see a preference stated in the other
    direction, so the Layton/Dusk page held two people preferring Dusk
    while announcing that nobody had said which they preferred.
    """
    against = _contradiction(pair, top)
    if against is not None:
        return f"{_people(against.row.commenters)} said the opposite."

    present = {s.row.claim_type for s in pair.statements}
    alike = present & {"DUPE_OF", "SIMILAR_TO"}
    preference = "BETTER_THAN" in present

    if preference and not alike:
        return "None of them said how the two differ."
    if alike and not preference:
        return "Nobody here said which of the two they preferred."
    # Both kinds of claim are on the page. What is missing then is not a
    # comparison at all: no page renders descriptor claims, so no page can
    # tell a reader what either bottle actually smells like.
    return "Nothing here describes what either one smells like on its own."


def render_pair(
    pair: Pair,
    facts: dict[str, Bottle] | None = None,
    indexes: dict[str, ReverseIndex] | None = None,
    blocklist: frozenset[str] | None = None,
    base: str = "",
    analytics: str = "",
) -> str:
    """One page, as a complete HTML document.

    Every interpolated value goes through `html.escape`, including the
    quotes, which are the whole point of the page and are also the one
    field written by someone other than us.

    `blocklist` keeps crude sentences off the page without removing the
    people who wrote them from the counts — see `load_blocklist`. Passing
    None loads the committed list; passing an empty set disables it.
    """
    e = html.escape
    blocklist = load_blocklist() if blocklist is None else blocklist
    out: list[str] = [
        *_head(
            pair.title,
            canonical=f"{base}{pair.slug}.html" if base else "",
            analytics=analytics,
        ),
        "<body>",
        f"<h1>{e(pair.title)}</h1>",
        # The answer first, then how it was counted. A reader who stops
        # after one line should still have left with something true.
        f"<p>{e(verdict(pair))}</p>",
        f"<p>In total, {e(_people(pair.commenters))} connected these two "
        f"fragrances, across {e(_sources(pair.sources))}. Every line below is "
        f"quoted from a comment, and links back to it.</p>",
    ]

    # Who makes each one, and when it came out. Omitted entirely when we
    # know neither, rather than printing a bottle's own name back at it.
    known = [(facts or {}).get(n, Bottle(n)) for n in (pair.left, pair.right)]
    if any(b.described != b.name for b in known):
        out.append(
            "<ul>"
            + "".join(f"<li>{e(b.described)}</li>" for b in known)
            + "</ul>"
        )

    for stmt in pair.statements:
        row = stmt.row
        out.append("<section>")
        # Both bottles are named in the heading, in the direction people
        # said it. A preference runs one way and so, in English, does "a
        # dupe of": the cheaper bottle imitates the expensive one, and a
        # heading that swaps them says something nobody wrote.
        out.append(f"<h2>{e(_people(row.commenters))} {e(stmt.sentence)}</h2>")
        hidden = 0
        # `<figure>` rather than `<ul><li>`. A blockquote is a block
        # element, so a browser puts the list marker on its own line and
        # starts the quote underneath it — which reads as an empty bullet
        # above every quote. The markup was never malformed, which is why
        # "no empty <li>" would have passed; the defect was only visible
        # rendered. `<figure>` + `<figcaption>` is also what the spec says
        # a quote with attribution is.
        for ev in row.evidence:
            words = crude_words(ev.quote, blocklist)
            if words:
                log.info(
                    "Hidden for language (%s): %s", ", ".join(words), ev.permalink
                )
                hidden += 1
                continue
            # `outbound` is relative to the end the row was read from, not
            # to the direction the heading states.
            said_about = stmt.asked if ev.outbound else stmt.other
            out.append(
                "<figure>"
                f"<blockquote>{e(ev.quote)}</blockquote>"
                f"<figcaption>Written about {e(said_about)}. "
                f'<a href="{e(ev.permalink)}" rel="nofollow noopener">'
                "read the comment</a></figcaption>"
                "</figure>"
            )
        if hidden:
            # Said on the page, not only in a log. A quote that vanishes
            # without explanation reads as evidence we could not produce.
            out.append(
                f"<p>{hidden} comment{'' if hidden == 1 else 's'} not shown "
                "here, for language. The count above still includes "
                f"{'it' if hidden == 1 else 'them'}.</p>"
            )
        out.append("</section>")

    # Somebody who came for one comparison usually wants the field. Only
    # bottles that have their own index are linked, so no link is a
    # promise of a page that does not exist.
    nearby = [
        (indexes or {})[name]
        for name in (pair.left, pair.right)
        if name in (indexes or {})
    ]
    if nearby:
        out.append("<nav><ul>")
        for index in nearby:
            out.append(
                f'<li><a href="{e(index.slug)}.html">{e(index.title)}</a></li>'
            )
        out.append("</ul></nav>")

    out.append(
        "<footer><p>Similarity here is asserted by people, never computed. "
        "This page counts distinct commenters and shows what they wrote; "
        "nothing on it is ordered by any commercial relationship.</p>"
        '<p><a href="index.html">All comparisons</a></p></footer>'
    )
    out.append("</body>")
    out.append("</html>")
    return "\n".join(out) + "\n"


def render_reverse(
    index: ReverseIndex,
    facts: dict[str, Bottle] | None = None,
    base: str = "",
    analytics: str = "",
) -> str:
    """One bottle, everything the community compares to it."""
    e = html.escape
    described = (facts or {}).get(index.bottle, Bottle(index.bottle)).described
    out: list[str] = [
        *_head(
            index.title,
            canonical=f"{base}{index.slug}.html" if base else "",
            analytics=analytics,
        ),
        "<body>",
        f"<h1>{e(index.title)}</h1>",
        # No total across the list. People appear in more than one
        # comparison, so summing the pair counts would print a number of
        # humans the corpus cannot support — the defect SPEC recorded when
        # readers were adding up per-row counts themselves.
        f"<p>{len(index.pairs)} comparisons involving {e(described)}, each "
        f"backed by at least {MIN_COMMENTERS} people writing across "
        f"{MIN_SOURCES} creators. Each line links to the comments behind "
        "it.</p>",
        "<ul>",
    ]
    for pair, other, statement in index.rows:
        out.append(
            f'<li><a href="{e(pair.slug)}.html">{e(other)}</a> — '
            f"{e(_people(statement.row.commenters))} {e(statement.sentence)}</li>"
        )
    out += [
        "</ul>",
        "<footer><p>Every comparison above cleared the same bar: "
        f"{MIN_COMMENTERS} people writing across {MIN_SOURCES} creators. "
        "Nothing here is ranked by any commercial relationship.</p>"
        '<p><a href="index.html">All comparisons</a></p></footer>',
        "</body>",
        "</html>",
    ]
    return "\n".join(out) + "\n"


def render_index(
    pairs: list[Pair],
    indexes: list[ReverseIndex] | None = None,
    base: str = "",
    analytics: str = "",
) -> str:
    """The list of pages, in the order the pages themselves are ranked."""
    e = html.escape
    out = [
        *_head(
            "Fragrance comparisons",
            canonical=f"{base}index.html" if base else "",
            analytics=analytics,
        ),
        "<body>",
        "<h1>Fragrance comparisons</h1>",
        WHAT_THIS_IS,
        f"<p>{len(pairs)} comparison"
        f"{'' if len(pairs) == 1 else 's'}, each backed by at least "
        f"{MIN_COMMENTERS} people writing across at least {MIN_SOURCES} "
        "creators.</p>",
        "<ul>",
    ]
    for p in pairs:
        out.append(
            f'<li><a href="{e(p.slug)}.html">{e(p.title)}</a> — '
            f"{e(_people(p.commenters))} across {e(_sources(p.sources))}</li>"
        )
    out.append("</ul>")
    if indexes:
        out.append("<h2>Fragrances with more than one comparison</h2>")
        out.append("<ul>")
        for index in indexes:
            out.append(
                f'<li><a href="{e(index.slug)}.html">{e(index.title)}</a> — '
                f"{len(index.pairs)} comparisons</li>"
            )
        out.append("</ul>")
    out += ["</body>", "</html>"]
    return "\n".join(out) + "\n"


def build(
    conn: psycopg.Connection,
    out_dir: Path,
    *,
    min_commenters: int = MIN_COMMENTERS,
    min_sources: int = MIN_SOURCES,
    base_url: str = "",
) -> list[Pair]:
    """Write a page per qualifying pair, plus an index. Returns what it wrote.

    `base_url` is where the pages will be served from. It is needed for
    canonical tags and a sitemap, and a CNAME file overrides it — see
    `site_base`. Without either, both are skipped rather than guessed.
    """
    pairs = qualifying_pairs(
        conn, min_commenters=min_commenters, min_sources=min_sources
    )
    facts = bottle_facts(conn)
    indexes = reverse_indexes(pairs)
    linked = {i.bottle: i for i in indexes}
    blocklist = load_blocklist()
    base = site_base(base_url)
    analytics = load_analytics()
    out_dir.mkdir(parents=True, exist_ok=True)

    slugs = ["index.html"]
    for pair in pairs:
        (out_dir / f"{pair.slug}.html").write_text(
            render_pair(pair, facts, linked, blocklist, base, analytics),
            encoding="utf-8",
        )
        slugs.append(f"{pair.slug}.html")
    for index in indexes:
        (out_dir / f"{index.slug}.html").write_text(
            render_reverse(index, facts, base, analytics), encoding="utf-8"
        )
        slugs.append(f"{index.slug}.html")
    (out_dir / "index.html").write_text(
        render_index(pairs, indexes, base, analytics), encoding="utf-8"
    )

    (out_dir / "robots.txt").write_text(render_robots(base), encoding="utf-8")
    if base:
        (out_dir / "sitemap.xml").write_text(
            render_sitemap(base, slugs), encoding="utf-8"
        )
    else:
        # A sitemap of relative paths is not a sitemap, and one built on a
        # guessed domain points crawlers at somebody else's copy.
        log.warning(
            "No base URL and no CNAME — skipping sitemap.xml and canonical tags"
        )

    # GitHub Pages reads CNAME from the published root, so it has to be
    # copied into the artifact on every build; a custom domain silently
    # reverts to the default one the first time it is missing.
    domain = custom_domain()
    if domain:
        (out_dir / "CNAME").write_text(f"{domain}\n", encoding="utf-8")
    return pairs


def _report_flanker_holds(conn: psycopg.Connection, **kwargs) -> None:
    """Name every page the flanker bar is keeping off the site.

    Printed on every run rather than kept in a report someone has to think
    to ask for: a raised bar is a page that stopped existing, and the only
    way that stays a decision instead of a drift is to see it each time.
    """
    held = held_as_flankers(conn, **kwargs)
    if not held:
        return
    print(
        f"\n{len(held)} pair(s) held back by the flanker bar "
        f"({MIN_FLANKER_COMMENTERS} people across {MIN_FLANKER_SOURCES} "
        "creators for a house compared with its own line):"
    )
    for pair in held:
        print(
            f"  {pair.commenters:>3} people  {pair.sources} creators  "
            f"{pair.title}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate static comparison pages.")
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="Write one page per qualifying pair")
    b.add_argument("--out", default="site", type=Path)
    b.add_argument(
        "--base-url", default="",
        help="Where the pages will be served from, e.g. "
             "https://example.github.io/repo/. Needed for canonical tags "
             "and sitemap.xml; a CNAME file overrides it.",
    )
    b.add_argument("--min-commenters", type=int, default=MIN_COMMENTERS)
    b.add_argument("--min-sources", type=int, default=MIN_SOURCES)
    b.add_argument(
        "--min-queries", type=int, default=MIN_QUERIES,
        help="Distinct search queries a pair's videos must span. Default 1 "
             "(off); see MIN_QUERIES for why it is not yet 2.",
    )

    p = sub.add_parser("pairs", help="List qualifying pairs without writing anything")
    p.add_argument("--min-commenters", type=int, default=MIN_COMMENTERS)
    p.add_argument("--min-sources", type=int, default=MIN_SOURCES)
    p.add_argument("--min-queries", type=int, default=MIN_QUERIES)
    p.add_argument(
        "--show-queries", action="store_true",
        help="Name the searches behind each pair, not just count them",
    )

    for parser_ in (b, p):
        parser_.add_argument("--db-url", default=DEFAULT_DB_URL)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    conn = get_connection(args.db_url)
    migrate(conn)
    try:
        if args.command == "pairs":
            pairs = qualifying_pairs(
                conn,
                min_commenters=args.min_commenters,
                min_sources=args.min_sources,
                min_queries=args.min_queries,
            )
            single = 0
            for pair in pairs:
                thin = pair.queries == 1 and not pair.unprovenanced
                single += thin
                print(
                    f"  {pair.commenters:>3} people  "
                    f"{pair.sources} creators  "
                    f"{pair.queries} quer{'y' if pair.queries == 1 else 'ies'}  "
                    f"{pair.title}"
                    + ("   <- one query only" if thin else "")
                    + (f"   (+{pair.unprovenanced} video(s) with no retrieval "
                       "record, so this is a lower bound)"
                       if pair.unprovenanced else "")
                )
                if args.show_queries:
                    for q in queries_behind(conn, pair):
                        print(f"         {q}")
            print(f"\n{len(pairs)} pair(s) clear the gate.")
            if single:
                print(
                    f"{single} of them rest on a single search query. Those "
                    "satisfy the creator bar while being one question asked "
                    "in several rooms — see MIN_QUERIES."
                )
            _report_flanker_holds(
                conn,
                min_commenters=args.min_commenters,
                min_sources=args.min_sources,
                min_queries=args.min_queries,
            )
            return 0

        pairs = build(
            conn,
            Path(args.out),
            min_commenters=args.min_commenters,
            min_sources=args.min_sources,
            base_url=args.base_url,
        )
        for pair in pairs:
            print(f"  {pair.slug}.html  ({pair.commenters} people, {pair.sources} sources)")
        print(f"\nWrote {len(pairs)} page(s) + index to {args.out}/")

        blocklist = load_blocklist()
        hidden = [(p, hidden_quotes(p, blocklist)) for p in pairs]
        hidden = [(p, h) for p, h in hidden if h]
        if hidden:
            count = sum(len(h) for _, h in hidden)
            print(
                f"\nHid {count} quote(s) for language across {len(hidden)} "
                f"page(s). The people who wrote them are still counted; "
                f"edit {BLOCKLIST_PATH.name} to change what is hidden:"
            )
            for pair, quotes in hidden:
                for permalink, words in quotes:
                    print(f"  {pair.slug}  [{', '.join(words)}]  {permalink}")
        _report_flanker_holds(
            conn,
            min_commenters=args.min_commenters,
            min_sources=args.min_sources,
        )
        if not pairs:
            print(
                "\nNothing cleared the gate. That is the gate working: a pair "
                "needs both ends curated, then 3 people across 2 creators. "
                "`resolve.entities report` ranks what to curate next."
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
