"""Proving the provenance contract holds at every surface that speaks.

    uv run python -m fragrance_graph.audit

Twenty findings across seven adversarial reviews had one shape between
them: **weak, inferred, denied or absent evidence acquiring the appearance
of stronger evidence at a downstream surface.** Not once, and not in one
place — each fix held where it was applied and the same failure reappeared
somewhere the fix did not reach.

    phase 1  inferred attribution graded declarable
    phase 3  a word in a product name asserted as an "official listing"
    phase 4  the benchmark's own note exempt from the metric it enforced
    phase 5  a claim corrected to DENIED still quoted as a description
    phase 6  a disputed baseline grounding a comparative
    phase 6  one creator's audience establishing "less rose than X"

That pattern is the argument for this module. A rule enforced by care at
each call site is a rule that will be missed at the next call site, so the
contract is asserted here **once, against every surface that can produce a
factual sentence**, and a new surface is expected to be added to the list
rather than trusted.

## What counts as a surface

Anything that turns evidence into words a person reads: recommendation
results, profiles, comparisons, semantic explanations, structured
attribute output, release status, generated pages, the CLI. Each is
exercised against the real corpus and every sentence it produces is
checked, rather than reasoning about what it would produce.

## What is checked

Each rule below names a way weak evidence has actually escaped, not a
hypothetical:

    observed -> supported      a single remark stated as consensus
    inferred -> stated         a machine's reading passed off as a person's
    denied   -> supporting     a correction resurfacing as evidence
    absent   -> negative       silence read as "it is not X"
    name     -> canonical      a product-name token as an official note
    release  -> community      an announcement as a perception
    retailer -> consensus      a catalogue row as agreement
    vector   -> fact           similarity as similarity-of-smell
    one room -> independence   several videos from one channel
    unknown  -> independence   a NULL channel as another source
    hard constraint overridden by proximity or similarity
    comparative flattened into avoidance

A surface that cannot be reached from the corpus is reported as
unexercised rather than passed. An audit that quietly skips half the
product is worse than no audit, because it reads the same.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass, field

import psycopg

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evidence import (
    Attribution,
    Strength,
    attribute_facts,
    name_facts,
)
from fragrance_graph.recommend import Reason, recommend

log = logging.getLogger("fragrance_graph.audit")

#: Wordings that disclose rather than assert. A sentence built from
#: non-declarable evidence has to carry one of these or state its own
#: counts; otherwise it is a flat claim.
DISCLOSURES = (
    "one commenter said",
    "people said",
    "say so and",
    "disagree",
    "rather than naming it",
    "the name contains",
    "someone described it as",
    "evidence recorded for it",
    "no usable baseline",
    "not a clear difference",
    "cannot be compared",
)

COUNTED = re.compile(r"\b\d+ (?:person|people) across \d+ channels?\b")

#: Phrases no surface may produce, whatever the evidence behind them. Each
#: asserts agreement this system is not entitled to claim.
FORBIDDEN = (
    "the community agrees",
    "everyone agrees",
    "most people say",
    "widely regarded",
    "consensus is that",
    "it is known",
    "generally accepted",
    "it is not ",
    "does not contain",
    "definitely",
)


def discloses(sentence: str) -> bool:
    return any(d in sentence for d in DISCLOSURES) or bool(COUNTED.search(sentence))


@dataclass
class Violation:
    surface: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.surface}] {self.rule}: {self.detail}"


@dataclass
class AuditReport:
    checked: dict[str, int] = field(default_factory=dict)
    violations: list[Violation] = field(default_factory=list)
    unexercised: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        """Clean means checked *and* unviolated.

        `unexercised` used to be ignored, so the CLI exited successfully
        whenever no violation was found — including when a surface produced
        nothing to look at. An audit that skips a surface reads exactly
        like one that cleared it.
        """
        return not self.violations and not self.unexercised

    def render(self) -> str:
        lines = ["cross-surface provenance audit", ""]
        for surface, n in sorted(self.checked.items()):
            lines.append(f"  {surface:<28} {n:>4} sentence(s) checked")
        if self.unexercised:
            lines += ["", "NOT EXERCISED (reported, not passed):"]
            lines += [f"  {s}" for s in self.unexercised]
        lines += ["", f"violations: {len(self.violations)}"]
        lines += [f"  {v}" for v in self.violations]
        if self.clean and not self.unexercised:
            lines.append("  every surface holds the contract")
        return "\n".join(lines)


def _check_reason(report: AuditReport, surface: str, reason: Reason) -> None:
    """Every rule that applies to one produced sentence."""
    sentence = reason.phrase()
    report.checked[surface] = report.checked.get(surface, 0) + 1

    for phrase in FORBIDDEN:
        if phrase in sentence.lower():
            report.violations.append(
                Violation(surface, "forbidden phrasing", f"{sentence!r}")
            )

    if not reason.declarable and not discloses(sentence):
        report.violations.append(
            Violation(
                surface,
                "observed stated as supported",
                f"non-declarable reason with no disclosure: {sentence!r}",
            )
        )

    if reason.inferred and "inferred" not in sentence and (
        "rather than naming it" not in sentence
    ):
        report.violations.append(
            Violation(
                surface,
                "inferred passed off as stated",
                f"{sentence!r} does not say the bottle was inferred",
            )
        )

    if reason.against and "disagree" not in sentence:
        report.violations.append(
            Violation(
                surface,
                "denied evidence hidden",
                f"{reason.against} opposing but the sentence does not say so",
            )
        )

    if reason.kind == "graph" and reason.declarable and reason.creators < 2:
        report.violations.append(
            Violation(
                surface,
                "one room sold as independence",
                f"{reason.people} people on {reason.creators} channel(s) "
                "graded declarable",
            )
        )

    if reason.kind == "semantic" and reason.strength is not Strength.OBSERVED:
        report.violations.append(
            Violation(
                surface,
                "vector similarity became a fact",
                f"semantic reason graded {reason.strength.name}",
            )
        )

    if reason.kind in ("catalog_fit", "catalog_avoid"):
        _check_derived_voice(report, surface, sentence)


#: Words that would make the CATALOG-DERIVED voice
#: (`catalog_profile.DerivedWording`) read as the COMMUNITY voice
#: instead — see `catalog_profile.py`'s module docstring, "the third
#: provenance voice." A catalog-derived sentence is about the declared
#: note list read through a curated family/occasion mapping; it must
#: never claim a person said or wore anything.
COMMUNITY_CLAIM_WORDS = ("wearers", "people")


def _check_derived_voice(report: AuditReport, surface: str, text: str) -> None:
    lowered = text.lower()
    for word in COMMUNITY_CLAIM_WORDS:
        if word in lowered:
            report.violations.append(
                Violation(
                    surface,
                    "derived voice claims a perception",
                    f"{word!r} in catalog-derived sentence {text!r}",
                )
            )


#: Queries chosen to reach every surface, not to look good. Each exists to
#: exercise a specific path: an anchor comparative, a hard constraint on
#: thin evidence, a vibe query that falls through to vectors, a profile, a
#: comparison, and a request the corpus cannot answer.
PROBES = (
    ("recommendation", "I love Delina but the rose is too strong"),
    ("recommendation", "a crowd-pleasing fragrance with raspberry"),
    ("recommendation", "a feminine fruity fragrance with strong projection"),
    ("recommendation", "a vanilla that doesn't smell edible"),
    # The anchor-profile fallback: an anchor with almost no community
    # evidence has no comparative baseline for "warm", so the ordinary
    # path finds zero candidates and `recommend._anchor_profile_fallback`
    # takes over, wording the answer from the anchor's declared notes
    # instead. This is the one probe that reaches `kind="declared_overlap"`
    # and `_declared_basis_note` — without it, neither is exercised by
    # this audit at all, on the real corpus or a fixture one.
    ("recommendation", "i like side effect but its too warm"),
    ("semantic", "something that smells like an expensive hotel lobby"),
    ("semantic", "something like Rosy"),
    ("profile", "what do people disagree about for Parfums de Marly Layton?"),
    ("profile", "what do people say about Lattafa Khamrah?"),
    ("comparison", "Lattafa Khamrah vs Lattafa Khamrah Qahwa"),
    ("refusal", "hello there"),
    ("refusal", "something heavy"),
)


def audit(conn: psycopg.Connection) -> AuditReport:
    """Run every surface against the real corpus and check what it says."""
    report = AuditReport()

    for surface, query in PROBES:
        answer = recommend(conn, query)
        if answer.note:
            report.checked[surface] = report.checked.get(surface, 0) + 1
            for phrase in FORBIDDEN:
                if phrase in answer.note.lower():
                    report.violations.append(
                        Violation(surface, "forbidden phrasing in note",
                                  f"{answer.note!r}")
                    )
        for candidate in answer.results:
            for reason in candidate.reasons + candidate.caveats:
                _check_reason(report, surface, reason)

    _audit_rendered_text(conn, report)
    _audit_structured(conn, report)
    _audit_name_facts(conn, report)
    _audit_releases(conn, report)
    _audit_pages(conn, report)
    _audit_commerce(conn, report)
    return report


#: Every rendering entry point. The audit used to check `Reason.phrase`
#: only, which is where wording is *chosen* — but a surface that composes
#: sentences around those phrases can still overstate, and three of them
#: were never looked at.
RENDERERS = (
    "recommendation.explain",
    "answer.render",
    "release status",
    "semantic search",
    "site ask pages",
    "site profile pages",
    "site index page",
    "api responses",
)


def _audit_rendered_text(conn: psycopg.Connection, report: AuditReport) -> None:
    """The composed output, not only the individual phrases."""
    from fragrance_graph.releases import render_status
    from fragrance_graph.semantic import nearest

    blocks: list[tuple[str, str]] = []
    for _, query in PROBES:
        answer = recommend(conn, query)
        blocks.append(("answer.render", answer.render()))
        for candidate in answer.results:
            blocks.append(("recommendation.explain", candidate.explain()))
    blocks.append(("release status", render_status(conn)))
    blocks.append((
        "semantic search",
        "\n".join(f"{m.score:.2f} {m.text} {m.canonical_name}"
                   for m in nearest(conn, "rosy", limit=5)),
    ))

    # The static site's answer and profile pages. They compose sentences
    # around `Reason.phrase()` output like every renderer above, and a
    # surface that composes is a surface that can overstate — which is the
    # lesson RENDERERS exists to record. Checked as the built HTML, since
    # that is exactly what a stranger receives.
    from fragrance_graph.ask import page_texts

    blocks += page_texts(conn)

    # The site index — the query box, its refusal block, and its inline
    # script. `render_full_index` assembles it exactly the way `build`
    # does, so this is the literal page a visitor gets, not a stand-in for
    # it. The JavaScript itself is untouched by this scan's own logic —
    # `audit.py` does not run a browser — but its *source text* is part of
    # this string like any other, so a FORBIDDEN phrase written into a
    # script comment or a template would be caught exactly as it would be
    # anywhere else on the page. See `pages.py`'s "client-side query box"
    # section for the structural argument the JS is held to instead.
    from fragrance_graph import ask as _ask
    from fragrance_graph.pages import (
        build_search_index,
        qualifying_pairs,
        render_full_index,
        reverse_indexes,
    )

    pairs = qualifying_pairs(conn)
    indexes = reverse_indexes(pairs)
    profiles = _ask.profile_pages(conn)
    asked = list(_ask.QUESTIONS)
    index_data = build_search_index(conn, pairs, profiles, asked)
    blocks.append((
        "site index page",
        render_full_index(pairs, indexes, profiles, asked, index_data),
    ))

    # The service layer (`fragrance_graph.api`) needs the optional `[api]`
    # extra (FastAPI/uvicorn) — see `pyproject.toml` and `api.py`'s module
    # docstring. A core install has no reason to carry that weight just to
    # run this audit, so the import is attempted and its absence is
    # reported as unexercised rather than raised: the `for name in
    # RENDERERS` loop just below already turns "never appended to
    # `blocks`" into exactly that report, with no special-casing needed
    # here beyond not crashing the rest of the audit over a module this
    # install never installed.
    #
    # Two probes, not one: `audited_probe_text` drives only the stateless
    # `/api/recommend` path, and every sentence the *session* endpoints
    # compose themselves (`session.WORDING`, reached through `note` and
    # `unexpressed[].reason`) is invisible to it — a real gap, found by
    # a reviewer reading `api.py`'s session handlers rather than assuming
    # one audited probe covered the whole module. See
    # `audited_session_probe_text`'s own docstring.
    try:
        from fragrance_graph.api import audited_probe_text, audited_session_probe_text
    except ImportError:
        pass
    else:
        blocks.append(("api responses", audited_probe_text(conn)))
        blocks.append(("api responses", audited_session_probe_text(conn)))

    for surface, text in blocks:
        report.checked[surface] = report.checked.get(surface, 0) + 1
        for phrase in FORBIDDEN:
            if phrase in text.lower():
                report.violations.append(
                    Violation(surface, "forbidden phrasing in rendered text",
                              f"...{phrase!r}...")
                )
    for name in RENDERERS:
        if name not in report.checked:
            report.unexercised.append(f"{name} produced no output to check")


def _audit_structured(conn: psycopg.Connection, report: AuditReport) -> None:
    """The evidence layer itself, before any wording is chosen."""
    surface = "structured attributes"
    for fact in attribute_facts(conn, attribution=Attribution.PROPOSED):
        report.checked[surface] = report.checked.get(surface, 0) + 1
        # Not `fact.inferred`, which only means *some* contributor was
        # inferred. Declarability is computed from the stated subset, so
        # the invariant is that the stated half alone clears the gate — a
        # fact three commenters named outright does not stop being sayable
        # because two more were attributed by machine.
        if fact.may_declare and not fact.stated.clears_gate:
            report.violations.append(
                Violation(surface, "declarable without stated support",
                          f"{fact.attribute}={fact.value}: "
                          f"{fact.stated.people} stated people on "
                          f"{fact.stated.creators} channel(s)")
            )
        if fact.may_declare and fact.strength is Strength.CONTESTED:
            report.violations.append(
                Violation(surface, "contested graded declarable",
                          f"{fact.attribute}={fact.value}")
            )
        if fact.may_declare and fact.stated.creators < 2:
            report.violations.append(
                Violation(
                    surface, "one room graded declarable",
                    f"{fact.attribute}={fact.value} on "
                    f"{fact.stated.creators} channel(s)",
                )
            )


def _audit_name_facts(conn: psycopg.Connection, report: AuditReport) -> None:
    """A token in a product name is not an official note listing."""
    surface = "name-derived notes"
    for fact in name_facts(conn):
        report.checked[surface] = report.checked.get(surface, 0) + 1
        if fact.may_declare or fact.strength is not Strength.INSUFFICIENT:
            report.violations.append(
                Violation(surface, "product name became canonical",
                          f"{fact.value} graded {fact.strength.name}")
            )


def _audit_releases(conn: psycopg.Connection, report: AuditReport) -> None:
    """An announcement is discovery provenance, never smell evidence."""
    surface = "release lifecycle"
    columns = {
        row["column_name"]
        for row in conn.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'release_candidates'"
        )
    }
    report.checked[surface] = len(columns)
    leaky = columns & {"notes", "accords", "description", "note_list", "scent"}
    if leaky:
        report.violations.append(
            Violation(surface, "release metadata could become evidence",
                      f"columns {sorted(leaky)}")
        )
    orphan = conn.execute(
        "SELECT count(*) FROM claims c WHERE c.extraction_model = 'release'"
    ).fetchone()[0]
    if orphan:
        report.violations.append(
            Violation(surface, "release wrote claims", f"{orphan} row(s)")
        )


def _audit_pages(conn: psycopg.Connection, report: AuditReport) -> None:
    """The original product surface, still governed by the same rules."""
    surface = "published pages"
    from fragrance_graph.pages import qualifying_pairs

    pairs = qualifying_pairs(conn)
    report.checked[surface] = len(pairs)
    for pair in pairs:
        if pair.commenters < 3 or pair.sources < 2:
            report.violations.append(
                Violation(surface, "page below the gate",
                          f"{pair.left} vs {pair.right}: "
                          f"{pair.commenters} people, {pair.sources} sources")
            )


#: Internal audit/engineering vocabulary a *customer-facing* sentence must
#: never carry — the specific defect `commerce_card.headline` exists to
#: fix (`commerce-audit.md` §1/§7: "grep the wording layer for
#: independence/bar/threshold in customer strings and eliminate"). Checked
#: only against the commerce surface, not folded into `FORBIDDEN` (which
#: every rendered surface is checked against): a genuinely honest
#: *engineering* sentence is allowed to say "insufficient comparative
#: evidence" or report a pair's own `commenters`/`sources` count
#: (`_audit_pages`, above) — only the customer-facing commerce layer must
#: never use this vocabulary at all.
INTERNAL_TERMS = ("independence", "threshold", "consensus", "clears the bar")


def _check_commerce_text(report: AuditReport, surface: str, text: str) -> None:
    lowered = text.lower()
    for phrase in FORBIDDEN:
        if phrase in lowered:
            report.violations.append(
                Violation(surface, "forbidden phrasing in commerce text", f"{text!r}")
            )
    for phrase in INTERNAL_TERMS:
        if phrase in lowered:
            report.violations.append(
                Violation(surface, "internal-audit vocabulary in commerce text",
                          f"{phrase!r} in {text!r}")
            )


def _audit_commerce(conn: psycopg.Connection, report: AuditReport) -> None:
    """The commerce presentation layer's own audited wording
    (`commerce_card.TierWording`) — a distinct surface from `Reason.
    phrase()` (checked by `_check_reason`) because a tier-templated
    sentence never becomes a `Reason` itself; see `commerce_card.py`'s
    module docstring for why this layer exists at all.

    Two passes, because a real corpus is not guaranteed to happen to
    exercise every confidence tier, attribute type, or the CONTESTED/mixed
    branch on any given run (a small or freshly-seeded fixture database
    might have nothing at `STRONG`, nothing performance-shaped, nothing
    disputed):

    1. **Synthetic** — every template in every one of `TierWording`'s
       ladders (note/vibe, performance, occasion, tradeoff), plus `mixed`,
       `preference_matched`, and both headline branches, called directly
       with a benign, fixed subject/constraint list. This is what makes
       "every template" a guarantee independent of what the corpus holds
       — a `checked["commerce card"]` count that is never zero, and a
       coverage set that never silently shrinks, just because today's
       corpus happens to have no `STRONG`-tier fact or no disputed one.
       Deliberately *not* adversarial input: this is the routine gate
       `main()` exits non-zero on, and it must stay clean on legitimate
       wording — the adversarial version of this same check (proving a
       hostile *subject* value, e.g. a user-typed note name, still gets
       caught when a template interpolates it) is a positive-control test
       (`tests/test_audit.py`), not part of the pass/fail gate itself; see
       that test for why baking adversarial input into `main()`'s own exit
       code would make the gate permanently red rather than provably
       working.
       Extended by the same shape for `catalog_profile.DerivedWording`
       (the third, CATALOG-DERIVED voice) — its own templates, called
       directly, checked for FORBIDDEN/INTERNAL_TERMS exactly like
       `TierWording`'s AND for the one rule that voice alone must hold:
       never "wearers," never "people" (`_check_derived_voice`) — a
       catalog fact must never read as a thing a person said.
    2. **Real** — every `PROBES` query's actual headline and every
       returned candidate's actual `fit_signals`/`relevant_tradeoffs`,
       exactly as a real customer response would render them — the same
       "exercised against the real corpus" discipline every other surface
       in this module holds to. `catalog_fit`/`catalog_avoid` reasons
       reached through a real candidate's `reasons`/`caveats` are already
       covered by `_check_reason`'s own `_check_derived_voice` call, in
       `audit()`'s main loop above — not repeated here.
    """
    from fragrance_graph.catalog_profile import DerivedWording
    from fragrance_graph.commerce_card import TierWording, build_card, headline

    surface = "commerce card"

    def check(text: str) -> None:
        report.checked[surface] = report.checked.get(surface, 0) + 1
        _check_commerce_text(report, surface, text)

    def check_derived(text: str) -> None:
        check(text)
        _check_derived_voice(report, surface, text)

    benign = "feminine"
    # Every confidence-tier ladder this module writes — note/vibe
    # (`strong`/.../`single_source`), performance (`*_performance`),
    # occasion (`*_occasion`), and the tradeoff ladder — plus the two
    # single-template surfaces (`preference_matched`, `mixed`). One list,
    # not four separate loops, so a fifth ladder added later only has to
    # be appended here once for this coverage guarantee to include it.
    for template in (
        TierWording.strong, TierWording.moderate, TierWording.limited,
        TierWording.single_source,
        TierWording.strong_performance, TierWording.moderate_performance,
        TierWording.limited_performance, TierWording.single_source_performance,
        TierWording.strong_occasion, TierWording.moderate_occasion,
        TierWording.limited_occasion, TierWording.single_source_occasion,
        TierWording.tradeoff_strong, TierWording.tradeoff_moderate,
        TierWording.tradeoff_limited,
        TierWording.preference_matched, TierWording.mixed,
    ):
        check(template(benign))
    check(TierWording.headline_with_results([benign], [benign]))
    check(TierWording.headline_with_results([benign], [benign], anchor=benign))
    check(TierWording.headline_with_results([], [], anchor=benign))
    check(TierWording.headline_no_results([benign]))
    check(TierWording.coverage_note([benign], 46, 548))
    check(TierWording.coverage_note([benign, benign], 46, 548))
    check(TierWording.community_coverage_moderate())
    check(TierWording.community_coverage_limited())

    # The CATALOG-DERIVED voice's own templates — `check_derived`, not
    # `check`, so every one of these is also proven to never say
    # "wearers"/"people", the one rule unique to this voice.
    check_derived(DerivedWording.note_present(benign))
    check_derived(DerivedWording.note_absent(benign))
    check_derived(DerivedWording.family_present(benign))
    check_derived(DerivedWording.family_absent(benign))
    check_derived(DerivedWording.occasion_fit("summer", [benign, "citrus"]))

    for _, query in PROBES:
        answer = recommend(conn, query)
        check(headline(answer.plan, answer.results, answer.note))
        for candidate in answer.results:
            card = build_card(candidate)
            for sentence in card.fit_signals + card.relevant_tradeoffs:
                check(sentence)
            check(card.community_coverage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fragrance_graph.audit",
        description="Prove the provenance contract at every speaking surface.",
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    conn = get_connection(args.db_url)
    try:
        report = audit(conn)
        print(report.render())
    finally:
        conn.close()
    return 0 if report.clean else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
