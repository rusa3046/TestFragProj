"""Real, permitted places new fragrances get announced.

    uv run python -m fragrance_graph.releases discover --source nst

One adapter, against one publication that syndicates its own content and
is not on the prohibited list. It implements `ReleaseSource` exactly as
`FixtureSource` does, so the lifecycle cannot tell them apart — which is
what the fixture was built to guarantee.

## Which sources are permitted, and why this one

`SPEC.md` forbids scraping Fragrantica, Parfumo and retailers: their terms
disallow it and the project has to be publicly demoable. RSS and Atom are
a different thing entirely — a publisher generating a machine-readable
feed *for machines to read* — so an editorial publication's own feed is
consumption as intended rather than extraction against terms.

Four were checked and reachable. **Now Smell This** was chosen because it
is the only one whose new-release posts follow a rigid convention:

    Memo Paris Darling Point ~ new fragrance
    Calvin Klein Euphoria Signature Elixir ~ new fragrance

That ` ~ new fragrance` marker is doing real work. The same feed carries
essays, "scent of the day" posts and giveaways, and without a marker every
adapter becomes a guess about whether a headline names a product. Here the
publication has already made that distinction and says so.

Fragrantica's FeedBurner mirror answered 200 and is deliberately not used.
The constraint names Fragrantica specifically, and reading its syndication
to route around that would be honouring the letter while ignoring what was
asked.

## Where the brand ends and the name begins

"Memo Paris Darling Point" has no delimiter, and no rule recovers the
split without knowing the houses. So the adapter matches the longest known
brand from the catalogue and takes the rest as the name; failing that it
hands the whole string over with **no brand at all**, which
`releases.verify` already treats as unresolvable rather than guessing.

That is the correct outcome and not a shortfall. An announcement the
system cannot place becomes a row somebody can look at, not a fabricated
fragrance.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime

import psycopg

from fragrance_graph.releases import ReleaseItem

log = logging.getLogger("fragrance_graph.release_sources")

#: Identifies this client to the publisher, with somewhere to complain to.
#: A feed reader that will not say who it is is indistinguishable from a
#: scraper, whatever its intentions.
USER_AGENT = (
    "fragrance-graph/0.1 (research feed reader; "
    "https://github.com/rusa3046/TestFragProj)"
)

#: The marker Now Smell This puts on posts announcing a product, as
#: opposed to essays, scent-of-the-day posts and giveaways.
NEW_RELEASE_MARKER = re.compile(r"\s*~\s*new (?:fragrance|perfume|flanker)s?\s*$", re.I)

#: Titles that mention a product but are not announcing one.
NOT_A_RELEASE = re.compile(
    r"\b(scent of the day|giveaway|winner|review|profiles in|"
    r"the daily lemming|poll|reader)\b",
    re.I,
)

ITEM = re.compile(r"<item>(.*?)</item>", re.S)
FIELD = {
    "title": re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S),
    "link": re.compile(r"<link>(.*?)</link>", re.S),
    "guid": re.compile(r"<guid[^>]*>(.*?)</guid>", re.S),
    "date": re.compile(r"<pubDate>(.*?)</pubDate>", re.S),
}


def _unescape(text: str) -> str:
    import html

    return html.unescape(text).strip()


def _iso(pubdate: str) -> str | None:
    try:
        return parsedate_to_datetime(pubdate).date().isoformat()
    except (TypeError, ValueError):
        return None


def known_brands(conn: psycopg.Connection) -> list[str]:
    """Brands the catalogue already knows, longest first.

    Longest first so "Parfums de Marly" is tried before "Parfums", and a
    two-word house is not truncated into a one-word one.
    """
    brands = {
        row["brand"]
        for row in conn.execute(
            "SELECT DISTINCT brand FROM fragrances WHERE brand IS NOT NULL"
            "  AND brand <> ''"
        )
    }
    return sorted(brands, key=len, reverse=True)


def split_brand(title: str, brands: list[str]) -> tuple[str, str]:
    """(brand, name) from a headline, or ('', whole title) if unknown.

    Returning an empty brand is a real answer. `releases.verify` refuses to
    catalogue a nameless-house announcement precisely because "Amber Oud"
    names a dozen bottles across a dozen brands, and this adapter must not
    smuggle a guess past that check by inventing a split.
    """
    for brand in brands:
        if title.lower().startswith(brand.lower() + " "):
            return brand, title[len(brand) :].strip()
    return "", title


@dataclass
class RSSSource:
    """A publication's own syndication feed, read as syndication.

    `fetch` is injectable so the parsing can be exercised against a stored
    payload without a network call — the tests read a checked-in fixture of
    a real response, which keeps them honest about the shape while staying
    offline and free.
    """

    url: str
    source_type: str
    conn: psycopg.Connection | None = None
    fetch: object = None
    _brands: list[str] = field(default_factory=list)

    def _payload(self) -> str:
        if self.fetch is not None:
            return self.fetch(self.url)
        request = urllib.request.Request(
            self.url, headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            log.warning("%s unreachable: %s", self.url, exc)
            return ""

    def discover_since(self, since: str | None) -> list[ReleaseItem]:
        xml = self._payload()
        if not xml:
            return []
        brands = self._brands or (
            known_brands(self.conn) if self.conn is not None else []
        )
        items = []
        for block in ITEM.findall(xml):
            found = {
                key: (pattern.search(block).group(1) if pattern.search(block) else "")
                for key, pattern in FIELD.items()
            }
            title = _unescape(found["title"])
            if not NEW_RELEASE_MARKER.search(title):
                continue
            if NOT_A_RELEASE.search(title):
                continue
            product = NEW_RELEASE_MARKER.sub("", title).strip()
            if len(product) < 3:
                continue
            published = _iso(_unescape(found["date"]))
            if since and published and published <= since:
                continue
            brand, name = split_brand(product, brands)
            items.append(
                ReleaseItem(
                    source_id=_unescape(found["guid"]) or _unescape(found["link"]),
                    name=name,
                    brand=brand,
                    url=_unescape(found["link"]),
                    release_date=published,
                )
            )
        items.sort(key=lambda i: i.source_id)
        log.info("%s: %d release announcement(s)", self.source_type, len(items))
        return items

    def provenance(self, item: ReleaseItem) -> str:
        return f"{self.source_type} feed {self.url} item {item.source_id}"


#: The adapters that may be named on the command line. Adding one is a
#: licensing decision before it is a technical one, so they are listed
#: rather than discovered.
PERMITTED = {
    "nst": ("https://nstperfume.com/feed/", "rss:nowsmellthis"),
}


def build(name: str, conn: psycopg.Connection, **kwargs) -> RSSSource:
    if name not in PERMITTED:
        raise KeyError(
            f"{name!r} is not a permitted source. Available: "
            f"{', '.join(sorted(PERMITTED))}"
        )
    url, source_type = PERMITTED[name]
    return RSSSource(url=url, source_type=source_type, conn=conn, **kwargs)
