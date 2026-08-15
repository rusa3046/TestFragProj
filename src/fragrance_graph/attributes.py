"""Making attribute claims countable.

    uv run python -m fragrance_graph.attributes subjects    # what "this" meant
    uv run python -m fragrance_graph.attributes vocabulary  # what "sweet" meant
    uv run python -m fragrance_graph.attributes agreement   # what it all buys

An attribute claim is a claim about *one* bottle — "Delina is rosy", "it
lasts nine hours" — as opposed to a comparison claim, which is about two.
There are more of them in the corpus than comparisons, and almost none of
them are usable. This module measures why, in the two places the loss
happens, and changes nothing: every function here reads.

## Why measure rather than fix

Both repairs below are guesses about what a human meant, and a guess
applied to 1,639 rows is 1,639 rows of invented provenance. The publishing
gate exists so that a page never says "3 people" about a fact 1 person
asserted; a repair that silently attaches claims to the wrong bottle
defeats it just as thoroughly as a bad extraction would. So the rules here
are deliberately conservative, they report what they refused as loudly as
what they kept, and applying them is a separate decision.

## The two losses

**Nobody says which bottle.** A commenter under a Delina review writes
"this is too rosy". They are right not to repeat the name — the video said
it — but the claim stores the literal subject, `raw_subject_text = "this"`,
and no resolver can match that to a fragrance. Roughly three quarters of
attribute claims are floating like this. `subjects` measures how many could
be recovered from what the video is about, and refuses the ones where
"this" is genuinely ambiguous.

**Everybody says it differently.** The gate wants three people to agree.
Agreement is impossible when the vocabulary is open: the corpus holds 616
distinct note strings across 994 note claims, so the average note is said
about 1.6 times *in total*, across every bottle. `coffee`, `coffee note`
and `burnt coffee` are three facts nobody agrees on. `vocabulary` measures
how much of that gap is spelling rather than disagreement.

The second loss is the one worth understanding, because it is not about
notes:

    longevity / projection    97 facts, 22 reach three people   (23%)
    notes / vibes / occasions 374 facts, 12 reach three people    (3%)

Longevity is not a better-observed property than smell. It has *three
possible answers* — good, bad, neither — and notes have six hundred. What
decides whether people agree is how many strings the answer can be.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.gate import MIN_COMMENTERS, MIN_SOURCES
from fragrance_graph.resolve.entities import load_candidates

log = logging.getLogger("fragrance_graph.attributes")

#: Claim types that describe one bottle. The complement of `COMPARISONS`.
ATTRIBUTES = (
    "NOTE_DESCRIPTOR",
    "AESTHETIC",
    "OCCASION",
    "LONGEVITY",
    "PROJECTION",
    "DEVELOPMENT",
    "REFORMULATION",
    "UNMET_PRODUCT_REQUEST",
)

#: The subset carrying a free-text tag. The rest record only a sentiment,
#: which is why they aggregate and these do not.
TAGGED = ("NOTE_DESCRIPTOR", "AESTHETIC", "OCCASION")


# --- what "this" meant -------------------------------------------------------


#: Subjects that name nothing and therefore *can* be filled in from the
#: video. Deliberately a closed list rather than a pattern: if a commenter
#: typed any word at all for the bottle — "oud for glory", "the EdP", "the
#: original" — they meant something more specific than the video's headline
#: and guessing over the top of them is how a claim ends up on the wrong
#: bottle. Bare "fragrance" is excluded for the same reason; measured on a
#: sample it attached general remarks about wearing fragrance at all.
DEICTIC = frozenset(
    {
        "this", "it", "that", "this one", "that one", "this fragrance",
        "this perfume", "this scent", "the fragrance", "the perfume",
        "the scent", "this stuff", "this juice", "the juice", "this cologne",
        "the cologne", "this bottle",
    }
)

#: Titles and queries that mean the video has no single subject. A comment
#: saying "this one is the best" under a nine-clone blind test names one of
#: the nine, not the fragrance in the title — and the title's fragrance is
#: usually the *original* being cloned, which is the one thing the
#: commenter definitely did not mean. Measured: adding the bare words
#: "dupe" and "clone" here cut the sample's error rate roughly in half.
AMBIGUOUS_VIDEO = re.compile(
    r"\b(vs|versus|clone war|battle|ranked|top \d|blind test|which one|"
    r"which should|instead|comparison|compared|dupe|dupes|clone|clones|"
    r"alternative|alternatives|my \d+|best \d+)\b"
)

#: Words that may follow a catalogue name without changing which bottle is
#: meant. Anything else — "Khamrah *Waha*", "Bade'e al Oud *Sublime*" — is
#: a flanker the catalogue does not have, and attaching its comments to the
#: base bottle is exactly the confusion `pages.is_flanker_pair` exists to
#: prevent.
HARMLESS_SUFFIX = frozenset(
    {
        "", "fragrance", "fragrances", "perfume", "cologne", "review",
        "reviews", "eau", "edp", "edt", "edc", "parfum", "the", "a", "is",
        "are", "by", "from", "and", "in", "on", "for", "worth", "it", "this",
        "that", "de", "new", "cheap", "best", "amazing", "honest", "first",
    }
)

#: Below this, a catalogue alias matches too much ordinary English to be
#: used as a substring. The corpus has "as" as an alias of Angels' Share.
MIN_ALIAS_LEN = 4


def _flatten(text: str | None) -> str:
    """Lowercase, punctuation to spaces, padded so `\\bword\\b` is a slice."""
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip() + " "


@dataclass(frozen=True)
class SurfaceForm:
    text: str
    fragrance_id: int
    canonical_name: str


def surface_forms(conn: psycopg.Connection) -> list[SurfaceForm]:
    """Every spelling the catalogue answers to, longest first.

    Longest first so "parfums de marly layton exclusif" is consumed before
    "layton" can claim half of it.
    """
    forms = [
        SurfaceForm(_flatten(spelling).strip(), c.fragrance_id, c.canonical_name)
        for c in load_candidates(conn)
        for spelling in (c.canonical_name, *c.aliases)
    ]
    forms = [f for f in forms if len(f.text) >= MIN_ALIAS_LEN]
    forms.sort(key=lambda f: -len(f.text))
    return forms


def names_in(text: str | None, forms: list[SurfaceForm]) -> dict[int, str]:
    """Every distinct fragrance named in `text`, without double-counting.

    A match inside an already-claimed span is skipped, so "Layton Exclusif"
    is one fragrance rather than Layton and Layton Exclusif both.
    """
    haystack = _flatten(text)
    found: dict[int, str] = {}
    claimed: list[tuple[int, int]] = []
    for form in forms:
        for m in re.finditer(rf"(?<= ){re.escape(form.text)}(?= )", haystack):
            if any(m.start() < end and start < m.end() for start, end in claimed):
                continue
            claimed.append((m.start(), m.end()))
            found.setdefault(form.fragrance_id, form.canonical_name)
    return found


@dataclass(frozen=True)
class VideoSubject:
    """What one video is about, or why that could not be decided."""

    video_id: str
    fragrance_id: int | None
    canonical_name: str
    refused: str = ""

    @property
    def usable(self) -> bool:
        return self.fragrance_id is not None


def subject_of(
    video_id: str, title: str | None, query: str | None, forms: list[SurfaceForm]
) -> VideoSubject:
    """The one bottle a video is about, or a refusal naming the doubt.

    Title first, search query second. The query records what *we* asked
    YouTube for, not what YouTube returned, so it is the weaker evidence —
    but 24 of the 67 videos holding floating claims have no stored title,
    and a query is better than nothing on those.
    """

    def refuse(why: str) -> VideoSubject:
        return VideoSubject(video_id, None, "", why)

    for text in (title, query):
        if not text:
            continue
        haystack = _flatten(text)
        found = names_in(text, forms)
        if len(found) != 1:
            continue
        if AMBIGUOUS_VIDEO.search(haystack):
            return refuse("several bottles in play")
        frag_id, canonical = next(iter(found.items()))
        form = next(
            f for f in forms if f.fragrance_id == frag_id and f.text in haystack
        )
        after = haystack.split(form.text, 1)[1].strip().split(" ")[0]
        if after not in HARMLESS_SUFFIX and not after.isdigit():
            return refuse(f"name runs on into {after!r} — a flanker we lack")
        return VideoSubject(video_id, frag_id, canonical)
    return refuse("no catalogue name in the title or query")


FLOATING_SQL = """
SELECT cl.id, cl.claim_type, cl.raw_subject_text, cl.raw_object_text,
       c.video_id, c.body,
       COALESCE(NULLIF(c.author_id, ''), 'author:unknown') AS person,
       c.source_channel AS creator
  FROM claims cl
  JOIN comments c ON c.id = cl.comment_id
 WHERE cl.polarity = 'ASSERTED'
   AND cl.subject_frag_id IS NULL
   AND cl.claim_type = ANY(%(types)s)
   AND c.video_id IS NOT NULL
"""

VIDEO_CONTEXT_SQL = """
SELECT v.video_id, v.title, d.query
  FROM (SELECT DISTINCT ON (video_id) video_id, title
          FROM videos ORDER BY video_id, title) v
  FULL JOIN (SELECT video_id, string_agg(DISTINCT retrieval_query, ' | ') AS query
               FROM video_discoveries GROUP BY video_id) d
    ON d.video_id = v.video_id
"""


@dataclass
class Attachment:
    """One floating claim and the bottle its video says it is about."""

    claim_id: int
    claim_type: str
    tag: str | None
    fragrance_id: int
    canonical_name: str
    person: str
    creator: str
    #: What the rule read to decide this, so a person auditing the row does
    #: not have to re-derive it from a video id.
    evidence: str = ""


@dataclass
class SubjectReport:
    floating: int = 0
    attached: list[Attachment] = field(default_factory=list)
    refused: Counter = field(default_factory=Counter)

    @property
    def recovered(self) -> int:
        return len(self.attached)

    def render(self) -> str:
        lines = [
            f"{self.floating} attribute claims name no bottle.",
            f"{self.recovered} can be recovered from what the video is about.",
            "",
            "refused:",
        ]
        for why, n in self.refused.most_common():
            lines.append(f"  {n:>5}  {why}")
        return "\n".join(lines)


def attach_by_video(conn: psycopg.Connection) -> SubjectReport:
    """Which floating claims a video's subject could fill in — read only.

    Three refusals, each measured against a hand-read sample rather than
    reasoned about: the video has no single subject, the commenter named a
    bottle themselves, or the comment names some *other* catalogue bottle
    and "this" may well be that one.
    """
    forms = surface_forms(conn)
    context = {
        row["video_id"]: (row["title"], row["query"])
        for row in conn.execute(VIDEO_CONTEXT_SQL)
        if row["video_id"]
    }
    subjects: dict[str, VideoSubject] = {}
    report = SubjectReport()

    for row in conn.execute(FLOATING_SQL, {"types": list(ATTRIBUTES)}):
        report.floating += 1
        video = row["video_id"]
        if video not in subjects:
            title, query = context.get(video, (None, None))
            subjects[video] = subject_of(video, title, query, forms)
        subject = subjects[video]
        if not subject.usable:
            report.refused[subject.refused] += 1
            continue
        if (row["raw_subject_text"] or "").strip().lower() not in DEICTIC:
            report.refused["the commenter named a bottle themselves"] += 1
            continue
        if set(names_in(row["body"], forms)) - {subject.fragrance_id}:
            report.refused["the comment names a different bottle"] += 1
            continue
        title, query = context.get(video, (None, None))
        report.attached.append(
            Attachment(
                claim_id=row["id"],
                claim_type=row["claim_type"],
                tag=row["raw_object_text"],
                fragrance_id=subject.fragrance_id,
                canonical_name=subject.canonical_name,
                person=row["person"],
                creator=row["creator"],
                evidence=f"video {video}: {(title or query or '')[:120]}",
            )
        )
    return report


#: The sampled accuracy of `attach_by_video`, measured by hand-reading two
#: samples of ~20 attachments on 2026-08-15. The first rule put 4 of 18 on
#: the wrong bottle; refusing comparison videos and claims where the
#: commenter named anything took it to about 1 in 20. It is written here
#: rather than inferred because every row this method writes inherits it,
#: and a confidence nobody measured is worse than none.
VIDEO_SUBJECT_CONFIDENCE = 0.95

#: The method name stored on every row this writes. Consumers filter on it,
#: so it is a constant rather than a literal at the insert site.
VIDEO_SUBJECT_METHOD = "video_subject"

INSERT_ATTRIBUTION_SQL = """
INSERT INTO claim_attributions
    (claim_id, role, fragrance_id, method, evidence, confidence,
     review_status, created_at)
VALUES (%(claim_id)s, 'subject', %(fragrance_id)s, %(method)s, %(evidence)s,
        %(confidence)s, 'proposed', %(created_at)s)
ON CONFLICT (claim_id, role) DO UPDATE
   SET fragrance_id = EXCLUDED.fragrance_id,
       evidence     = EXCLUDED.evidence,
       confidence   = EXCLUDED.confidence,
       created_at   = EXCLUDED.created_at
 WHERE claim_attributions.review_status = 'proposed'
"""


def record_inferences(conn: psycopg.Connection) -> int:
    """Write what `attach_by_video` worked out, as *proposed* attributions.

    Returns rows written. Nothing here touches `claims.subject_frag_id`:
    this method is wrong about one time in twenty, and a guess written into
    the column meaning "the commenter named this" is indistinguishable from
    a person's own words forever after.

    Idempotent, and it refuses to overwrite a row somebody has already
    accepted or rejected — a re-run must not quietly undo a human decision.
    Re-running after the rule improves updates only unruled rows.
    """
    report = attach_by_video(conn)
    written = 0
    for attachment in report.attached:
        cur = conn.execute(
            INSERT_ATTRIBUTION_SQL,
            {
                "claim_id": attachment.claim_id,
                "fragrance_id": attachment.fragrance_id,
                "method": VIDEO_SUBJECT_METHOD,
                "evidence": attachment.evidence,
                "confidence": VIDEO_SUBJECT_CONFIDENCE,
                "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
        )
        written += cur.rowcount
    conn.commit()
    log.info(
        "%d floating claims, %d attributable, %d rows written",
        report.floating, report.recovered, written,
    )
    return written


# --- what "sweet" meant ------------------------------------------------------


#: Nouns that add nothing after a tag. "coffee note" and "coffee" are the
#: same claim typed by two people, and counting them apart is the whole
#: problem.
TRAILING_NOISE = ("note", "notes", "accord", "accords", "smell", "scent",
                  "vibe", "vibes", "opening", "drydown", "dry down")

#: Spellings and languages observed in this corpus, written out rather than
#: inferred. Every entry is a pair that appears in `data/corpus` today; the
#: list is short on purpose, because a long one is a taxonomy nobody
#: reviewed. Spanish and Portuguese are here because commenters write in
#: them: `vainilla` and `doce` are not typos.
SPELLINGS = {
    "smokey": "smoky", "vainilla": "vanilla", "vanila": "vanilla",
    "doce": "sweet", "dulce": "sweet", "dulces": "sweet",
    "freshness": "fresh", "citrusy": "citrus", "musky": "musk",
    "powdery": "powder", "spicey": "spicy", "chocolatey": "chocolate",
    "chocolaty": "chocolate", "smoke": "smoky", "florals": "floral",
    "feminino": "feminine", "adventus": "aventus",
}

#: Tags that are verdicts, not descriptions. A page can say "eight people
#: called it sweet"; "eight people called it amazing" is a review score
#: wearing a note's clothes, and the extractor files these as
#: NOTE_DESCRIPTOR because a commenter wrote them where a note goes.
VERDICTS = frozenset(
    {
        "amazing", "good", "great", "best", "delicious", "horrible", "terrible",
        "trash", "garbage", "shit", "stinks", "cheap", "expensive", "bland",
        "generic", "boring", "outstanding", "fantastic", "elite", "beautiful",
        "gorgeous", "nice", "awful", "disgusting", "lovely", "perfect", "meh",
        "overrated", "underrated", "worth it", "mid",
    }
)

#: Comparatives with nothing to compare to. "Sweeter" is a claim about two
#: bottles that lost one of them on the way into a one-bottle claim type.
DANGLING = re.compile(r"^(much |a bit |slightly |way )?\w+(er|ier)$")


def normalize_tag(text: str | None, *, plurals: frozenset[str] = frozenset()) -> str:
    """One tag, reduced to the concept two people would have to share.

    `plurals` is the set of singular forms known to exist elsewhere in the
    corpus. Trailing "s" is stripped only against it, because blind
    de-pluralising turns `dates` — the fruit, in nine claims — into `date`,
    the occasion.
    """
    tag = re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())
    tag = re.sub(r"\s+", " ", tag).strip()
    if not tag:
        return ""
    for noise in TRAILING_NOISE:
        if tag.endswith(" " + noise):
            tag = tag[: -len(noise) - 1].strip()
    tag = re.sub(r"^(a|an|the|some|very|really|super) ", "", tag)
    words = [SPELLINGS.get(w, w) for w in tag.split(" ")]
    tag = " ".join(words)
    if tag.endswith("s") and tag[:-1] in plurals:
        tag = tag[:-1]
    return SPELLINGS.get(tag, tag)


def known_singulars(tags: list[str]) -> frozenset[str]:
    """Tags that appear without a trailing "s", for `normalize_tag`."""
    return frozenset(t for t in tags if t and not t.endswith("s"))


@dataclass
class VocabularyReport:
    claims: int = 0
    distinct_before: int = 0
    distinct_after: int = 0
    verdicts: int = 0
    dangling: int = 0
    merged: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def collapse(self) -> float:
        if not self.distinct_before:
            return 0.0
        return 1 - self.distinct_after / self.distinct_before

    def render(self) -> str:
        lines = [
            f"{self.claims} tagged attribute claims",
            f"  {self.distinct_before} distinct tags before normalising",
            f"  {self.distinct_after} distinct tags after  "
            f"({self.collapse:.0%} fewer)",
            "",
            f"  {self.verdicts} are verdicts, not descriptions "
            "(amazing, trash, cheap)",
            f"  {self.dangling} are comparatives with nothing to compare to "
            "(sweeter, softer)",
        ]
        if self.merged:
            lines += ["", "biggest merges:"]
            for canonical, variants in self.merged[:12]:
                lines.append(f"  {canonical:<22} <- {', '.join(sorted(variants))}")
        return "\n".join(lines)


def vocabulary(conn: psycopg.Connection) -> VocabularyReport:
    """How much of the tag vocabulary is spelling rather than disagreement."""
    raw = [
        (row["raw_object_text"] or "").lower().strip()
        for row in conn.execute(
            "SELECT raw_object_text FROM claims"
            " WHERE polarity = 'ASSERTED' AND claim_type = ANY(%(types)s)"
            "   AND raw_object_text IS NOT NULL",
            {"types": list(TAGGED)},
        )
    ]
    raw = [t for t in raw if t]
    plurals = known_singulars(raw)
    groups: dict[str, set[str]] = defaultdict(set)
    verdicts = dangling = 0
    for tag in raw:
        norm = normalize_tag(tag, plurals=plurals)
        if not norm:
            continue
        groups[norm].add(tag)
        if norm in VERDICTS:
            verdicts += 1
        elif DANGLING.match(norm):
            dangling += 1
    merged = sorted(
        ((k, sorted(v)) for k, v in groups.items() if len(v) > 1),
        key=lambda kv: -len(kv[1]),
    )
    return VocabularyReport(
        claims=len(raw),
        distinct_before=len(set(raw)),
        distinct_after=len(groups),
        verdicts=verdicts,
        dangling=dangling,
        merged=merged,
    )


# --- what it all buys --------------------------------------------------------


@dataclass(frozen=True)
class Fact:
    """One thing said about one bottle, and how many people said it."""

    fragrance_id: int
    tag: str
    people: int
    creators: int

    @property
    def publishable(self) -> bool:
        return self.people >= MIN_COMMENTERS and self.creators >= MIN_SOURCES


RESOLVED_SQL = """
SELECT cl.subject_frag_id AS fragrance_id, cl.claim_type,
       cl.raw_object_text AS tag,
       COALESCE(NULLIF(c.author_id, ''), 'author:unknown') AS person,
       c.source_channel AS creator
  FROM claims cl
  JOIN comments c ON c.id = cl.comment_id
 WHERE cl.polarity = 'ASSERTED'
   AND cl.evidence_verified = 1
   AND cl.subject_frag_id IS NOT NULL
   AND cl.raw_object_text IS NOT NULL
   AND cl.claim_type = ANY(%(types)s)
"""


def facts(
    conn: psycopg.Connection,
    *,
    attach: bool = False,
    collapse: bool = False,
) -> list[Fact]:
    """Attribute facts under either repair, or neither.

    Four combinations, one function, because the only interesting number
    is the difference between them.
    """
    rows = [
        (r["fragrance_id"], r["tag"], r["person"], r["creator"])
        for r in conn.execute(RESOLVED_SQL, {"types": list(TAGGED)})
    ]
    if attach:
        rows += [
            (a.fragrance_id, a.tag, a.person, a.creator)
            for a in attach_by_video(conn).attached
            if a.claim_type in TAGGED and a.tag
        ]
    plurals = known_singulars([(t or "").lower().strip() for _, t, _, _ in rows])
    grouped: dict[tuple[int, str], tuple[set[str], set[str]]] = {}
    for frag_id, tag, person, creator in rows:
        key_tag = (
            normalize_tag(tag, plurals=plurals) if collapse else (tag or "").lower()
        )
        if not key_tag:
            continue
        people, creators = grouped.setdefault((frag_id, key_tag), (set(), set()))
        people.add(person)
        creators.add(creator)
    return [
        Fact(frag_id, tag, len(people), len(creators))
        for (frag_id, tag), (people, creators) in grouped.items()
    ]


def agreement(conn: psycopg.Connection) -> str:
    """The measurement both repairs exist to move."""
    header = (
        f"{'':<26}{'facts':>7}{'3+ people':>11}{'publishable':>13}\n"
        + "-" * 57
    )
    lines = [header]
    for label, kwargs in (
        ("as it stands", {}),
        ("+ video subjects", {"attach": True}),
        ("+ one vocabulary", {"collapse": True}),
        ("+ both", {"attach": True, "collapse": True}),
    ):
        found = facts(conn, **kwargs)
        three = sum(1 for f in found if f.people >= MIN_COMMENTERS)
        good = sum(1 for f in found if f.publishable)
        lines.append(f"{label:<26}{len(found):>7}{three:>11}{good:>13}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.attributes",
        description="Measure why attribute claims are not queryable. Reads only.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("subjects", help="How many floating claims a video could fill in")
    sub.add_parser("vocabulary", help="How much of the tag vocabulary is spelling")
    sub.add_parser("agreement", help="What either repair does to the gate")
    sub.add_parser(
        "infer",
        help="Record video-subject attributions as proposed (writes; "
             "never touches claims.subject_frag_id)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        if args.command == "subjects":
            print(attach_by_video(conn).render())
        elif args.command == "infer":
            written = record_inferences(conn)
            print(f"{written} proposed attribution(s) written.")
            print("Excluded from every count until accepted; see "
                  "evidence.Attribution")
        elif args.command == "vocabulary":
            print(vocabulary(conn).render())
        else:
            print(agreement(conn))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
