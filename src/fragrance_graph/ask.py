"""The questions a visitor can ask, answered in static HTML.

Rendered by `pages.build` alongside the pair pages. Nothing here decides
what may be said: every sentence about evidence comes out of
`recommend.Reason.phrase()`, which is the single place wording is chosen
and the place `audit.py` checks. This module lays those phrases on a page
and escapes them; it never composes a stronger sentence around them.

## Why static answers and not a search box

The recommender is deterministic — `plan.py` parses without a model and
`recommend.py` reads only the corpus — so an answer page is a pure
function of the corpus, exactly like a pair page. Rendering a curated set
of questions keeps the site a build artifact: no server, no per-query
cost, nothing to fall over during a demo. The questions chosen are the
ones the product exists to answer, plus one the corpus *cannot* answer,
because the refusal is as much the product as the recommendation — a
system that answers everything is a system whose answers mean nothing.

## Profiles are the same machinery

A bottle profile is `recommend("what do people say about X?")` — the same
audited path a typed query takes. Bottles whose answers come back empty
get no page rather than a page of filler.
"""

from __future__ import annotations

import html
import logging

import psycopg

from fragrance_graph.pages import slugify
from fragrance_graph.recommend import (
    Answer,
    Recommendation,
    _people,
    _sources,
    digest,
    recommend,
)

log = logging.getLogger("fragrance_graph.ask")

#: The curated questions, each exercising a path the audit already probes.
#: Slugs are stable — they become URLs.
QUESTIONS: tuple[tuple[str, str], ...] = (
    ("delina-but-less-rose", "I love Delina but the rose is too strong"),
    ("layton-but-less-vanilla", "Parfums de Marly Layton but less vanilla"),
    ("khamrah-but-less-sweet", "Lattafa Khamrah but less sweet"),
    ("crowd-pleasing-raspberry", "a crowd-pleasing fragrance with raspberry"),
    ("expensive-hotel-lobby", "something that smells like an expensive hotel lobby"),
    ("khamrah-vs-qahwa", "Lattafa Khamrah vs Lattafa Khamrah Qahwa"),
    ("layton-disagreements", "what do people disagree about for Parfums de Marly Layton?"),
    ("something-heavy", "something heavy"),
)


def ask_slug(slug: str) -> str:
    return f"ask-{slug}.html"


def profile_slug(name: str) -> str:
    """Delegates to `pages.slugify` rather than running its own copy of
    the same regex — the two used to disagree on accents until
    `pages.slugify` grew `fold_accents`; see its docstring for what that
    fixed and what it cost."""
    return f"about-{slugify(name)}.html"


def _card(candidate: Recommendation) -> list[str]:
    """One recommendation: `digest()` up front, everything else behind a
    `<details>` a reader opens on purpose.

    User feedback, verbatim: "when someone selects a query they should
    get a summary max 2 sent that summarizes the comments and then they
    can select if they want the comments. its messy and annoying to read
    rn." `digest()` is the only new wording on this page — it is produced
    by the audited layer, exactly like every phrase this function already
    rendered, and this function still never composes a sentence of its
    own around any of it. The full reason/caveat list is unchanged; it is
    only relocated, and its own counts are what `digest`'s second
    sentence already states, carried rather than restated a second way.
    """
    e = html.escape
    out = [f"<article><h3>{e(candidate.name)}</h3>"]
    summary = digest(candidate)
    if summary:
        out.append(f"<p class=digest>{e(summary)}</p>")
    else:
        # `digest` returns "" whenever every reason on the card is a
        # `semantic` quote — real today, not a hypothetical: "an
        # expensive hotel lobby" retrieves candidates through the vector
        # layer alone, and a card standing entirely on one quoted remark
        # has nothing else to summarise. See `digest`'s own docstring for
        # why quotes are excluded rather than folded in. The counts still
        # have to stay visible on the collapsed card either way.
        out.append(
            f"<p class=digest>{e(_people(candidate.people))} across "
            f"{e(_sources(candidate.creators))} behind everything cited.</p>"
        )
    details: list[str] = []
    if candidate.reasons:
        details.append("<p>Why it matches:</p><ul>")
        details += [f"<li>{e(r.phrase())}</li>" for r in candidate.reasons]
        details.append("</ul>")
    if candidate.caveats:
        details.append("<p>Worth knowing:</p><ul>")
        details += [f"<li>{e(r.phrase())}</li>" for r in candidate.caveats]
        details.append("</ul>")
    if candidate.unmatched:
        details.append(
            "<p><em>No evidence either way for: "
            f"{e(', '.join(candidate.unmatched))}</em></p>"
        )
    if details:
        out.append(
            f"<details><summary>show the comments "
            f"({e(str(candidate.people))})</summary>"
        )
        out += details
        out.append("</details>")
    out.append("</article>")
    return out


def render_answer(question: str, answer: Answer, *, head: list[str]) -> str:
    """A question, what the system understood, and what it will say.

    The plan is shown because "what the system understood" is part of the
    honesty: a wrong parse should be visible, not smoothed over. An empty
    result renders the refusal note prominently — that page exists to show
    the system declining, and hiding the decline would defeat it.
    """
    e = html.escape
    out = list(head)
    out += [
        "<body>",
        '<p><a href="index.html">&larr; all questions</a></p>',
        f"<h1>&ldquo;{e(question)}&rdquo;</h1>",
        "<details><summary>How this was read</summary>"
        f"<pre>{e(answer.plan.render())}</pre></details>",
    ]
    if answer.note:
        out.append(f"<p class=note>{e(answer.note)}</p>")
    if answer.results:
        for candidate in answer.results:
            out += _card(candidate)
    elif not answer.note:
        out.append("<p class=note>No candidates met the evidence bar.</p>")
    out += [
        "<footer><p>Every count above is distinct humans in public "
        "YouTube comments; nothing is computed from note lists or "
        "official descriptions. Weakly-evidenced facts are worded as "
        "such on purpose.</p></footer>",
        "</body></html>",
    ]
    return "\n".join(out)


def render_ask_index_section(rendered: list[tuple[str, str]]) -> str:
    """The <section> the site index embeds. Links only what was built."""
    e = html.escape
    out = ["<section><h2>Ask</h2><ul>"]
    for slug, question in rendered:
        out.append(f'<li><a href="{e(ask_slug(slug))}">{e(question)}</a></li>')
    out.append("</ul></section>")
    return "\n".join(out)


def profile_pages(
    conn: psycopg.Connection,
) -> list[tuple[str, str, Answer]]:
    """(name, slug, answer) for every bottle the corpus can say things about.

    Goes through `recommend()` with a profile-shaped query rather than
    reading the evidence layer directly, so a profile page cannot say
    anything a typed query could not — one path, one audit. A name the
    parser cannot read or a bottle with nothing to say is skipped, not
    padded.
    """
    pages = []
    for row in conn.execute(
        "SELECT canonical_name FROM fragrances ORDER BY canonical_name"
    ):
        name = row["canonical_name"]
        try:
            answer = recommend(conn, f"what do people say about {name}?")
        except Exception:  # noqa: BLE001 - a parser miss is a skip, not a crash
            log.info("profile skipped, parser could not read: %s", name)
            continue
        if not answer.results:
            continue
        pages.append((name, profile_slug(name), answer))
    return pages


def render_profile(name: str, answer: Answer, *, head: list[str]) -> str:
    e = html.escape
    out = list(head)
    out += [
        "<body>",
        '<p><a href="index.html">&larr; all bottles</a></p>',
        f"<h1>What people say about {e(name)}</h1>",
    ]
    if answer.note:
        out.append(f"<p class=note>{e(answer.note)}</p>")
    for candidate in answer.results:
        out += _card(candidate)
    out += [
        "<footer><p>Counts are distinct commenters across distinct "
        "channels. One person's remark is reported as one person's "
        "remark.</p></footer>",
        "</body></html>",
    ]
    return "\n".join(out)


def page_texts(conn: psycopg.Connection) -> list[tuple[str, str]]:
    """(surface, rendered html) for the audit. Everything this module can
    emit, produced the way `pages.build` produces it."""
    out = []
    for _slug, question in QUESTIONS:
        out.append(
            ("site ask pages",
             render_answer(question, recommend(conn, question), head=[]))
        )
    for name, _, answer in profile_pages(conn):
        out.append(("site profile pages", render_profile(name, answer, head=[])))
    return out
