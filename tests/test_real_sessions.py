"""Every utterance from a real user's session log, pinned.

The log arrived 2026-08-17 from the project owner's own kiosk sessions
(`my-sessions.json`, session_events on their machine). It is the most
valuable test data this project has: not what we imagined a shopper
typing, but what one actually typed — including "insane longevity"
entered three times in eleven seconds while the parser refused it, which
is what a silent failure looks like from the other side of the glass.

Each test states what the utterance must compile to NOW. Where the honest
answer is still a refusal (typo'd anchors), the refusal is pinned as the
expectation — these tests write to reality, not to hope, and an
improvement that changes one of these plans should have to update the pin
deliberately.
"""

import pytest

from fragrance_graph.plan import Direction, parse_with_corpus
from fragrance_graph.resolve.entities import add_fragrance


@pytest.fixture
def catalogue(conn):
    """The bottles the real log talks about, under their real names."""
    add_fragrance(conn, "Initio Parfums Privés Side Effect",
                  aliases=["Side Effect"], brand="Initio Parfums Privés")
    add_fragrance(conn, "Maison Francis Kurkdjian Oud Satin Mood",
                  aliases=["Oud Satin Mood"],
                  brand="Maison Francis Kurkdjian")
    add_fragrance(conn, "Lattafa Khamrah", aliases=["Khamrah"],
                  brand="Lattafa")
    return conn


def plan(conn, text):
    return parse_with_corpus(conn, text)


class TestThePerformancePhrasesPeopleActuallyType:
    def test_insane_longevity_typed_three_times_now_parses(self, catalogue):
        """The log's most repeated utterance. Three sends in eleven
        seconds means the shopper watched nothing happen twice and tried
        again — the definitive silent failure."""
        got = plan(catalogue, "insane longevity")
        assert not got.refusal
        assert ("longevity", "long lasting", Direction.HIGH) in {
            (p.attribute, p.value, p.direction) for p in got.soft
        }

    def test_the_other_intensifiers_parse_too(self, catalogue):
        for text in ("crazy longevity", "amazing longevity",
                     "insane projection", "beast mode"):
            assert not plan(catalogue, text).refusal, text


class TestLessAndMoreAndComparatives:
    def test_less_bright_parses_like_too_bright_always_did(self, catalogue):
        """'too bright' worked; 'less bright' refused — the two readers
        disagreed about whether "bright" needed to be vocabulary."""
        got = plan(catalogue, "less bright")
        assert not got.refusal
        (pref,) = got.soft
        assert (pref.value, pref.direction) == ("bright", Direction.LOW)

    def test_brighter_alone_is_a_preference_not_a_refusal(self, catalogue):
        got = plan(catalogue, "brighter")
        assert not got.refusal
        (pref,) = got.soft
        assert (pref.value, pref.direction) == ("bright", Direction.HIGH)

    def test_summer_is_an_occasion_never_a_mangled_comparative(self, catalogue):
        """"summer" ends in -er; the comparative reader must not stem it
        to "summ". Same for "amber" if it ever arrives alone."""
        got = plan(catalogue, "summer")
        assert ("occasion", "summer") in {
            (p.attribute, p.value) for p in got.soft
        }
        assert "summ" not in {p.value for p in got.soft}

    def test_more_citrus_with_an_anchor(self, catalogue):
        got = plan(catalogue, "i like side effect but more citrus")
        assert got.anchor == "Initio Parfums Privés Side Effect"
        assert ("citrus", Direction.HIGH) in {
            (p.value, p.direction) for p in got.soft
        }

    def test_more_of_a_oud_note_no_longer_vanishes(self, catalogue):
        """The log's version silently dropped everything after the
        anchor: the plan held the anchor and nothing else."""
        got = plan(catalogue, "i like side effect but i like more of a oud note")
        assert got.anchor == "Initio Parfums Privés Side Effect"
        assert ("oud", Direction.HIGH) in {
            (p.value, p.direction) for p in got.soft
        }


class TestBareNameAnchors:
    def test_side_effect_is_nice_but_too_warm_keeps_its_anchor(self, catalogue):
        """The log's phrasing. "is nice" polluted the name capture, so
        the complaint compiled anchorless and lost the comparison."""
        got = plan(catalogue, "side effect is nice but too warm")
        assert got.anchor == "Initio Parfums Privés Side Effect"
        assert ("warm", Direction.LESS_THAN_ANCHOR) in {
            (p.value, p.direction) for p in got.soft
        }

    def test_khamrah_but_not_too_sweet_anchors_too(self, catalogue):
        got = plan(catalogue, "khamrah but not too sweet")
        assert got.anchor == "Lattafa Khamrah"

    def test_two_names_stay_ambiguous_and_anchorless(self, catalogue):
        """The bare-name pass is the video-subject rule's conservatism:
        exactly one catalogue surface form or nothing. ("X or Y" is
        pre-existing COMPARE territory and keeps that meaning; this pin
        uses a non-compare shape with two names.)"""
        got = plan(catalogue, "too sweet for me, khamrah and side effect both")
        assert got.anchor is None


class TestTheOudTooStrongMisparse:
    def test_the_complained_note_is_oud_not_strong(self, catalogue):
        """"i like oud satin mood but the oud too strong" (no "is") read
        as less-STRONG — a projection complaint — instead of less-OUD."""
        got = plan(catalogue, "i like oud satin mood but the oud too strong")
        assert got.anchor == "Maison Francis Kurkdjian Oud Satin Mood"
        assert ("oud", Direction.LESS_THAN_ANCHOR) in {
            (p.value, p.direction) for p in got.soft
        }
        assert "strong" not in {p.value for p in got.soft}


class TestWhatStillRefusesAndWhy:
    def test_the_typo_biut_still_loses_the_anchor_visibly(self, catalogue):
        """"like side effect biut more fresh" — 'biut' breaks the name
        boundary. Deterministic parsing does not guess at typos; the pin
        here is that the failure stays VISIBLE (unparsed carries the
        fragment) and the salvageable half still compiles."""
        got = plan(catalogue, "like side effect biut more fresh")
        assert ("fresh", Direction.HIGH) in {
            (p.value, p.direction) for p in got.soft
        }
        assert got.unparsed or got.anchor is None

    def test_every_single_vibe_from_the_log_parses(self, catalogue):
        for text in ("subtle", "fresh", "elegant", "feminine",
                     "dark and mysterious", "warm and cozy", "night out",
                     "strong projection"):
            got = plan(catalogue, text)
            assert not got.refusal, text
            assert got.soft, text

    def test_the_full_log_never_dies_silently(self, catalogue):
        """Whole-log sweep: every utterance either compiles something or
        refuses out loud. An empty, non-refused plan — the silent
        nothing — is the one outcome no utterance may produce."""
        log = [
            "insane longevity", "i want something citrusy", "less bright",
            "brighter", "like side effect biut more fresh",
            "side effect is nice but too warm",
            "i like side effect but more citrus", "subtle", "fresh",
            "warm and cozy", "elegant", "too sweet", "too bright",
            "i like oud satin mood but the oud too strong",
            "i like side effect but i like more of a oud note",
            "dark and mysterious", "night out", "strong projection",
            "feminine",
        ]
        for text in log:
            got = plan(catalogue, text)
            assert got.refusal or got.usable, text
