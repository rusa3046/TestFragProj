"""Why 67% of purchased evidence attaches to no bottle.

    uv run python -m fragrance_graph.experiments.floating classify
    uv run python -m fragrance_graph.experiments.floating upside

Run 1 bought 403 claims and 271 of them landed nowhere. That single number
dwarfs every other loss in the funnel, and it is also the least
understood: "floating" is a description of the outcome, not of the cause.

Those 271 could be any mixture of

    a pronoun with an obvious referent      "this lasts forever"
    a reply whose parent named the bottle
    one candidate implied by the video
    several candidates, genuinely ambiguous
    no fragrance subject at all             "great video, subscribed"

and the difference decides whether attribution repair is worth building.
The first three are recoverable. The last two are not, by any method, and
counting them as upside would promise a return that cannot arrive.

## Read-only, on purpose

Nothing here writes. It classifies claims already in the corpus so the
cohort's treatment stays identical across all ten bottles — the whole
reason `attributes infer` has not been run since the experiment began.

## What "recoverable" is allowed to mean

Only that a rule could attach the claim **with disclosure**. A claim
recovered from a video's subject is evidence that somebody said something
under a review of that bottle, which is not the same as their having named
it, and the distinction survives into every sentence built from it. This
module counts what is reachable; it does not license stating any of it as
though a commenter had named the bottle.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

import psycopg

from fragrance_graph.attributes import (
    DEICTIC,
    POINTING,
    subject_of,
    surface_forms,
)
from fragrance_graph.db import DEFAULT_DB_URL, get_connection

log = logging.getLogger("fragrance_graph.experiments.floating")


class Cause:
    """Why one floating claim floats, and whether anything can fix it."""

    VIDEO_SUBJECT = "video-subject recoverable"
    REPLY_PARENT = "reply-parent recoverable"
    SINGLE_CANDIDATE = "single candidate named in the comment"
    AMBIGUOUS = "several candidates, genuinely ambiguous"
    SUBJECTLESS = "no fragrance subject at all"
    NAMED_UNKNOWN = "names a bottle the catalogue lacks"
    FLANKER = "names a flanker of a known bottle"
    CONCENTRATION = "names a concentration, not a bottle"
    CATEGORY = "names a category, not a bottle"

    #: Causes a rule could attach, with disclosure. The rest are the
    #: ceiling on what any attribution work can return.
    RECOVERABLE = (VIDEO_SUBJECT, REPLY_PARENT, SINGLE_CANDIDATE)


#: Subject text that names nothing and no rule will ever resolve without
#: outside context — as opposed to text naming a bottle we simply lack.
#: Words that make a name a *different product* rather than a longer way
#: of saying the same one. Same distinction `pages.is_flanker_pair` holds.
#:
#: These are only ever tested against the words a subject has **in excess
#: of** the name it would attach to, which is what makes a list this blunt
#: safe. "Rouge" is a flanker word and also a word in Baccarat Rouge 540;
#: testing the raw subject would suppress the bottle for containing its own
#: name. Testing the excess cannot: for subject "baccarat rouge 540" the
#: excess against that name is empty.
QUALIFIER_WORDS = frozenset(
    {
        "extrait", "elixir", "intense", "absolu", "absolue", "exclusif",
        "exclusive", "cologne", "parfum", "edp", "edt", "edc", "essence",
        "noir", "rouge", "bleu", "blue", "gold", "black", "white", "sport",
        "legacy", "supreme", "royal", "private", "limited", "vintage",
        "extreme", "concentree", "original", "summer", "night",
    }
)

#: Words that say *how strong* a bottle is without saying *which* bottle.
#: These are what a comment means by "the extrait" — a concentration, not a
#: product name — and on their own they name nothing at all.
CONCENTRATION_ONLY = frozenset(
    {"extrait", "parfum", "edp", "edt", "edc", "cologne", "concentration"}
)

#: Words naming a *category* of fragrance rather than a fragrance: "clones",
#: "dupes", "the OG". A comment saying clones last longer is a claim about
#: a market segment, and no attribution rule should give it to a bottle.
CATEGORY_WORDS = frozenset(
    {"clone", "clones", "dupe", "dupes", "og", "originals", "designer",
     "designers", "niche", "flanker", "flankers", "batch", "batches"}
)


def _excess_words(subject_text: str, canonical: str) -> set[str]:
    """The words a subject uses beyond the name it would be attached to."""
    subject = set(re.findall(r"[a-z0-9]+", (subject_text or "").lower()))
    known = set(re.findall(r"[a-z0-9]+", (canonical or "").lower()))
    return subject - known - {"the", "a", "de", "la", "le", "du", "and"}


#: Words a subject can use while still *pointing* at a bottle rather than
#: *naming* a different one: deixis, and the generic nouns people use for a
#: fragrance when the context supplies which.
#: "both" and "ones" are *plural*: "Layton and Mystery both smell sweet"
#: is a claim about two bottles, and attaching it to the one the
#: catalogue happens to recognise would be wrong in exactly the way this
#: module exists to avoid. They were in this set and counted as
#: recoverable upside.
PLURAL_WORDS = frozenset({"both", "ones", "these", "those", "them", "they"})

GENERIC_WORDS = frozenset(
    {"the", "a", "this", "that", "it", "one", "s"}
    | {"perfume", "perfumes", "fragrance", "fragrances", "scent", "scents"}
    | {"cologne", "juice", "bottle", "stuff", "smell", "thing"}
)


def _points_without_naming(subject_text: str, canonical: str) -> bool:
    """Whether attaching this subject to `canonical` is safe.

    A comment that says "this is sweet" under a Delina review points at
    Delina. A comment that says "Arianna Grandes Cloud is sweet" while
    *mentioning* Baccarat Rouge 540 elsewhere names a bottle the catalogue
    lacks, and attaching it to the one name that happens to appear is not
    recovery — it is fabrication with a citation. The first version of this
    module counted 100+ of those as upside.
    """
    words = set(re.findall(r"[a-z0-9]+", (subject_text or "").lower()))
    if words & PLURAL_WORDS:
        return False
    return not (_excess_words(subject_text, canonical) - GENERIC_WORDS)


def _is_flanker(subject_text: str, canonical: str) -> bool:
    """Whether the subject names something more specific than `canonical`.

    "Baccarat rouge extrait" in a comment mentioning Baccarat Rouge 540 is
    not recoverable to 540: Extrait is a separate bottle, and attaching it
    would be exactly the flanker merge the rest of the system refuses.
    Counting these as recoverable overstated the upside by 6 points.
    """
    return bool(_excess_words(subject_text, canonical) & QUALIFIER_WORDS)


#: Deixis in the languages this corpus actually contains. `attributes.DEICTIC`
#: is English-only, which is correct for a resolver that must not guess —
#: but counting a Spanish "this perfume" as *a fragrance the catalogue
#: lacks* is a measurement error, and a costly one: it put "este perfume"
#: (17 claims) and "esta fragancia" second and twelfth on the list of
#: bottles worth acquiring.
#:
#: Commenters write in Spanish, Portuguese, French, Italian, Indonesian
#: and Vietnamese here. Each entry below appears in the corpus.
#: The nouns a demonstrative may be followed by and still be pointing
#: rather than naming. The branches used to allow any word at all, so
#: "Ce Sauvage" — which names a bottle outright — read as deixis and was
#: counted recoverable to whatever the video was about.
_NOUN = (
    r"(?:perfume|perfumes|parfum|profumo|fragrance|fragancia|fragranza|"
    r"fragr[aâ]ncia|aroma|olor|cheiro|colonia|duft|truc|cosa)"
)

FOREIGN_DEIXIS = re.compile(
    r"^\s*(?:"
    rf"(?:est[ea]|est[eo]|es[ea]|aquell[ao])(?:\s+{_NOUN})?"      # es/pt
    rf"|(?:el|la|lo)\s+{_NOUN}"                                   # es
    rf"|(?:o|a)\s+{_NOUN}"                                        # pt
    rf"|(?:ce|cet|cette)(?:\s+{_NOUN})?"                          # fr
    rf"|(?:quest[oa]|quel[loa])(?:\s+{_NOUN})?"                   # it
    rf"|(?:dies[er]{{1,2}})(?:\s+{_NOUN})?"                       # de
    r"|(?:ini|itu)"                                               # id
    r"|(?:c[aá]i|con)?\s*n[àa]y"                                  # vi
    r")\s*$",
    re.I,
)


def _points(subject_text: str) -> bool:
    """Deixis in any language the corpus is written in.

    Defers to `attributes.POINTING` so the classifier and the rule that
    acts on it cannot drift apart -- they already had, and the divergence
    put "esse perfume" on a list of bottles worth acquiring while the
    attachment rule was happily treating it as a pronoun. The regex covers
    inflections of the same words, never new vocabulary.
    """
    text = (subject_text or "").strip().lower()
    return text in POINTING or bool(FOREIGN_DEIXIS.match(text))


NO_SUBJECT = re.compile(
    r"^\s*(video|channel|you|he|she|they|everyone|nobody|people|"
    r"comment|review|thanks?|nothing)\s*$",
    re.I,
)


@dataclass
class FloatingClaim:
    claim_id: int
    subject_text: str
    body: str
    video_id: str | None
    cause: str = ""
    detail: str = ""


@dataclass
class Classification:
    claims: list = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)

    @property
    def total(self) -> int:
        return len(self.claims)

    @property
    def recoverable(self) -> int:
        return sum(self.counts[c] for c in Cause.RECOVERABLE)

    def render(self) -> str:
        lines = [f"{self.total} floating claims", ""]
        for cause, count in self.counts.most_common():
            mark = " (recoverable)" if cause in Cause.RECOVERABLE else ""
            share = count / self.total if self.total else 0
            lines.append(f"  {cause:<42}{count:>5}  {share:>5.0%}{mark}")
        lines += [
            "",
            f"  {'RECOVERABLE, with disclosure':<42}{self.recoverable:>5}"
            f"  {self.recoverable / self.total if self.total else 0:>5.0%}",
            f"  {'ceiling on any attribution repair':<42}"
            f"{self.total - self.recoverable:>5}"
            f"  {(self.total - self.recoverable) / self.total if self.total else 0:>5.0%}",
        ]
        return "\n".join(lines)


FLOATING_SQL = """
SELECT cl.id, cl.raw_subject_text, c.body, c.video_id, c.raw_json
  FROM claims cl
  JOIN comments c ON c.id = cl.comment_id
 WHERE cl.subject_frag_id IS NULL
   AND cl.object_frag_id IS NULL
   AND cl.polarity = 'ASSERTED'
   AND cl.evidence_verified = 1
"""


def _video_context(conn: psycopg.Connection) -> dict:
    rows = conn.execute(
        "SELECT v.video_id, v.title, d.query FROM"
        " (SELECT DISTINCT ON (video_id) video_id, title FROM videos"
        "   ORDER BY video_id, title) v"
        " FULL JOIN (SELECT video_id, string_agg(DISTINCT retrieval_query, ' | ')"
        "              AS query FROM video_discoveries GROUP BY video_id) d"
        " ON d.video_id = v.video_id"
    ).fetchall()
    return {r["video_id"]: (r["title"], r["query"]) for r in rows if r["video_id"]}


def classify(
    conn: psycopg.Connection, *, limit: int | None = None
) -> Classification:
    """Sort every floating claim by why it floats.

    Ordered from cheapest recovery to most expensive, and each claim takes
    the first cause that fits — a claim whose comment names one bottle is
    counted there rather than as video-recoverable, because that is the
    attachment a rule should prefer.
    """
    from fragrance_graph.attributes import names_in

    forms = surface_forms(conn)
    context = _video_context(conn)
    result = Classification()

    rows = conn.execute(FLOATING_SQL).fetchall()
    for row in rows[: limit or len(rows)]:
        claim = FloatingClaim(
            claim_id=row["id"],
            subject_text=(row["raw_subject_text"] or "").strip(),
            body=row["body"] or "",
            video_id=row["video_id"],
        )
        named = names_in(claim.body, forms)
        # Strip articles first: "the extrait" is as much a concentration as
        # "extrait", and leaving the article in sent 40-odd of these to the
        # catalogue-gap bucket, where they read as missing bottles.
        words = set(re.findall(r"[a-z]+", claim.subject_text.lower())) - GENERIC_WORDS

        if words and words <= CATEGORY_WORDS:
            claim.cause = Cause.CATEGORY
            claim.detail = claim.subject_text
        elif words and words <= CONCENTRATION_ONLY:
            # "the extrait", "The EDP" — a concentration, not a product.
            # These were being counted as bottles the catalogue lacks,
            # which is both wrong and flattering: the catalogue is not
            # missing a fragrance called "extrait".
            claim.cause = Cause.CONCENTRATION
            claim.detail = claim.subject_text
        elif len(named) == 1 and _is_flanker(
            claim.subject_text, next(iter(named.values()))
        ):
            claim.cause = Cause.FLANKER
            claim.detail = f"{claim.subject_text} != {next(iter(named.values()))}"
        elif len(named) == 1 and _points_without_naming(
            claim.subject_text, next(iter(named.values()))
        ):
            claim.cause = Cause.SINGLE_CANDIDATE
            claim.detail = next(iter(named.values()))
        elif len(named) == 1:
            # The comment names one bottle, but the claim's subject names a
            # *different* one the catalogue lacks. Not recoverable.
            claim.cause = Cause.NAMED_UNKNOWN
            claim.detail = claim.subject_text
        elif len(named) > 1:
            claim.cause = Cause.AMBIGUOUS
            claim.detail = ", ".join(sorted(named.values()))
        elif NO_SUBJECT.match(claim.subject_text) or not claim.subject_text:
            claim.cause = Cause.SUBJECTLESS
        elif _points(claim.subject_text) and claim.video_id in context:
            # "este perfume" points exactly as hard as "this perfume". The
            # video-subject rule can attach it; only the English-only
            # DEICTIC list stopped it being recognised as pointing at all.
            title, query = context[claim.video_id]
            subject = subject_of(claim.video_id, title, query, forms)
            if subject.usable:
                claim.cause = Cause.VIDEO_SUBJECT
                claim.detail = subject.canonical_name
            else:
                claim.cause = Cause.AMBIGUOUS
                claim.detail = subject.refused
        elif _points(claim.subject_text):
            claim.cause = Cause.AMBIGUOUS
            claim.detail = "foreign deixis, no video context"
        elif claim.video_id and claim.video_id in context:
            title, query = context[claim.video_id]
            subject = subject_of(claim.video_id, title, query, forms)
            if subject.usable and _points_without_naming(
                claim.subject_text, subject.canonical_name
            ):
                claim.cause = Cause.VIDEO_SUBJECT
                claim.detail = subject.canonical_name
            elif subject.usable:
                claim.cause = Cause.NAMED_UNKNOWN
                claim.detail = claim.subject_text
            elif claim.subject_text.lower() in DEICTIC:
                claim.cause = Cause.AMBIGUOUS
                claim.detail = subject.refused
            else:
                claim.cause = Cause.NAMED_UNKNOWN
                claim.detail = claim.subject_text
        elif claim.subject_text.lower() in DEICTIC:
            claim.cause = Cause.AMBIGUOUS
            claim.detail = "no video context"
        else:
            claim.cause = Cause.NAMED_UNKNOWN
            claim.detail = claim.subject_text

        result.claims.append(claim)
        result.counts[claim.cause] += 1
    return result


def render_samples(result: Classification, *, per_cause: int = 2) -> str:
    """A couple of real claims per cause, because a table of counts is not
    checkable and these are the rows a person would have to agree with."""
    seen: Counter = Counter()
    lines = []
    for claim in result.claims:
        if seen[claim.cause] >= per_cause:
            continue
        seen[claim.cause] += 1
        body = re.sub(r"\s+", " ", claim.body)[:88]
        lines.append(
            f"  [{claim.cause}]\n"
            f"      subject {claim.subject_text!r}"
            + (f" -> {claim.detail}" if claim.detail else "")
            + f"\n      {body!r}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.experiments.floating",
        description="Why purchased evidence attaches to no bottle. Reads only.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("classify", help="Sort floating claims by cause")
    c.add_argument("--samples", action="store_true")
    sub.add_parser("upside", help="What attribution repair could return")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        result = classify(conn)
        print(result.render())
        if args.command == "classify" and args.samples:
            print("\nsamples:\n")
            print(render_samples(result))
        if args.command == "upside":
            total = conn.execute(
                "SELECT count(*) FROM claims WHERE polarity = 'ASSERTED'"
                "   AND evidence_verified = 1"
            ).fetchone()[0]
            print(
                f"\n  {result.recoverable} of {total} asserted claims could be "
                f"attached with disclosure ({result.recoverable / total:.0%} "
                "of the corpus)"
            )
            print(
                "  Recovery is retrieval-grade, not declarative: a claim "
                "attached\n  from a video's subject is evidence somebody "
                "spoke under a review\n  of that bottle, never evidence they "
                "named it."
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
