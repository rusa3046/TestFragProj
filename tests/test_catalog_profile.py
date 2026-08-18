"""`catalog_profile.py` — the L1 catalog read of a bottle, independent of
any comment. Pure-function tests (`family_tendencies`, `note_status`,
`occasion_prior`, `DerivedWording`) need no database at all; `catalog_
profile(conn)` itself is tested against a real, small fixture corpus.
"""

from __future__ import annotations

from fragrance_graph.catalog_profile import (
    FAMILY_HIGH,
    FAMILY_MEDIUM,
    CatalogProfile,
    DerivedWording,
    NoteStatus,
    Tendency,
    catalog_profile,
    family_tendencies,
    load_occasion_priors,
    note_status,
    occasion_prior,
)
from fragrance_graph.notes import DeclaredNote, store_claims
from fragrance_graph.resolve.entities import add_fragrance

# --- family_tendencies ---------------------------------------------------


class TestFamilyTendencies:
    AXES = {
        "citrus": frozenset({"lemon", "bergamot", "lime"}),
        "sweet": frozenset({"vanilla", "caramel"}),
    }

    def test_two_or_more_family_notes_is_high(self):
        assert FAMILY_HIGH == 2  # the breakpoint this test pins
        declared = frozenset({"lemon", "bergamot", "musk"})
        tendencies = family_tendencies(declared, self.AXES)
        assert tendencies["citrus"] is Tendency.HIGH

    def test_exactly_one_family_note_is_medium(self):
        assert FAMILY_MEDIUM == 1
        declared = frozenset({"lemon", "musk"})
        tendencies = family_tendencies(declared, self.AXES)
        assert tendencies["citrus"] is Tendency.MEDIUM

    def test_zero_family_notes_is_low(self):
        declared = frozenset({"musk", "leather"})
        tendencies = family_tendencies(declared, self.AXES)
        assert tendencies["citrus"] is Tendency.LOW
        assert tendencies["sweet"] is Tendency.LOW

    def test_every_axis_in_the_input_gets_a_tendency_dynamically(self):
        """No family list is hardcoded here — a caller passing a
        different axis dict (a future curation addition) gets a
        tendency for every key it defines, automatically."""
        axes = {"woody": frozenset({"cedar", "sandalwood"})}
        tendencies = family_tendencies(frozenset({"cedar", "sandalwood"}), axes)
        assert set(tendencies) == {"woody"}
        assert tendencies["woody"] is Tendency.HIGH

    def test_a_note_may_count_toward_more_than_one_family(self):
        axes = {"a": frozenset({"vanilla"}), "b": frozenset({"vanilla", "caramel"})}
        tendencies = family_tendencies(frozenset({"vanilla", "caramel"}), axes)
        assert tendencies["a"] is Tendency.MEDIUM
        assert tendencies["b"] is Tendency.HIGH


# --- note_status: the note status ladder ----------------------------------


class TestNoteStatus:
    def test_declared_present_wins_over_everything(self):
        profile = CatalogProfile(1, frozenset({"vanilla"}), has_catalog_data=True)
        assert note_status(profile, "vanilla") is NoteStatus.DECLARED_PRESENT
        # Even when community evidence is also asserted for the SAME
        # note -- the official list is never overridden by a comment.
        assert (
            note_status(profile, "vanilla", community_present=True)
            is NoteStatus.DECLARED_PRESENT
        )

    def test_perceived_present_when_declared_is_silent_but_community_speaks(self):
        profile = CatalogProfile(1, frozenset({"lemon"}), has_catalog_data=True)
        assert (
            note_status(profile, "vanilla", community_present=True)
            is NoteStatus.PERCEIVED_PRESENT
        )

    def test_not_declared_when_catalog_data_exists_and_the_note_is_absent(self):
        profile = CatalogProfile(1, frozenset({"lemon"}), has_catalog_data=True)
        assert note_status(profile, "vanilla") is NoteStatus.NOT_DECLARED

    def test_no_catalog_data_when_the_bottle_has_no_declared_list_at_all(self):
        """The two absences are distinct, by construction: a bottle
        with a real (but non-matching) declared list is `NOT_DECLARED`
        (real information); a bottle with none at all is
        `NO_CATALOG_DATA` (truly unknown) — even though both pass an
        empty `declared_notes` in this particular case, `has_catalog_
        data` is what tells them apart."""
        profile = CatalogProfile(1, frozenset(), has_catalog_data=False)
        assert note_status(profile, "vanilla") is NoteStatus.NO_CATALOG_DATA

    def test_no_profile_at_all_is_also_no_catalog_data(self):
        assert note_status(None, "vanilla") is NoteStatus.NO_CATALOG_DATA

    def test_the_two_absences_are_never_the_same_value(self):
        """Pinned directly, in case the two branches above are ever
        collapsed by accident: NOT_DECLARED and NO_CATALOG_DATA must
        never compare equal, and a caller distinguishing them by `is`
        must keep working."""
        not_declared = CatalogProfile(1, frozenset({"lemon"}), has_catalog_data=True)
        no_data = CatalogProfile(2, frozenset(), has_catalog_data=False)
        assert (
            note_status(not_declared, "vanilla")
            is not note_status(no_data, "vanilla")
        )
        assert note_status(not_declared, "vanilla") is NoteStatus.NOT_DECLARED
        assert note_status(no_data, "vanilla") is NoteStatus.NO_CATALOG_DATA


# --- occasion_prior --------------------------------------------------------


PRIORS = {
    "summer": {"high_if_any_high": ["citrus", "fresh"], "low_required": ["gourmand", "warm"]},
}


class TestOccasionPrior:
    def test_high_family_and_clear_low_required_gives_the_prior(self):
        profile = CatalogProfile(
            1, frozenset(),
            tendencies={"citrus": Tendency.HIGH, "fresh": Tendency.LOW,
                        "gourmand": Tendency.LOW, "warm": Tendency.LOW},
            has_catalog_data=True,
        )
        assert occasion_prior(profile, "summer", PRIORS) == ["citrus"]

    def test_both_qualifying_families_are_named(self):
        profile = CatalogProfile(
            1, frozenset(),
            tendencies={"citrus": Tendency.HIGH, "fresh": Tendency.HIGH,
                        "gourmand": Tendency.LOW, "warm": Tendency.LOW},
            has_catalog_data=True,
        )
        assert set(occasion_prior(profile, "summer", PRIORS)) == {"citrus", "fresh"}

    def test_a_low_required_family_at_high_silences_the_prior(self):
        """A citrus-forward bottle that is ALSO gourmand-forward is not
        a clean summer prior — silence, not a guessed rule either way."""
        profile = CatalogProfile(
            1, frozenset(),
            tendencies={"citrus": Tendency.HIGH, "fresh": Tendency.LOW,
                        "gourmand": Tendency.HIGH, "warm": Tendency.LOW},
            has_catalog_data=True,
        )
        assert occasion_prior(profile, "summer", PRIORS) is None

    def test_medium_never_counts_as_high_on_either_side(self):
        profile = CatalogProfile(
            1, frozenset(),
            tendencies={"citrus": Tendency.MEDIUM, "fresh": Tendency.LOW,
                        "gourmand": Tendency.MEDIUM, "warm": Tendency.LOW},
            has_catalog_data=True,
        )
        assert occasion_prior(profile, "summer", PRIORS) is None

    def test_no_catalog_data_gives_no_prior(self):
        profile = CatalogProfile(
            1, frozenset(), tendencies={"citrus": Tendency.HIGH}, has_catalog_data=False
        )
        assert occasion_prior(profile, "summer", PRIORS) is None

    def test_no_profile_at_all_gives_no_prior(self):
        assert occasion_prior(None, "summer", PRIORS) is None

    def test_an_uncurated_occasion_gives_no_prior(self):
        """Silence for an occasion the table does not name — never a
        guessed rule, the same MISSING DATA answer this codebase gives
        everywhere else."""
        profile = CatalogProfile(
            1, frozenset(), tendencies={"citrus": Tendency.HIGH}, has_catalog_data=True
        )
        assert occasion_prior(profile, "winter", PRIORS) is None

    def test_load_occasion_priors_reads_the_real_curated_table(self):
        priors = load_occasion_priors()
        assert "summer" in priors
        assert set(priors["summer"]["high_if_any_high"]) >= {"citrus", "aquatic", "fresh"}
        assert set(priors["summer"]["low_required"]) >= {"gourmand", "warm"}


# --- DerivedWording: the CATALOG-DERIVED voice -----------------------------


class TestDerivedWordingNeverClaimsPerception:
    """The one rule unique to this voice — see `catalog_profile.py`'s
    module docstring, "the third provenance voice." Checked exhaustively
    here (every template, several subjects) as the unit-level companion
    to `audit._check_derived_voice`'s corpus-level check."""

    def test_no_template_ever_says_wearers_or_people(self):
        subjects = ["vanilla", "sweet", "citrus", "woody"]
        sentences = []
        for subject in subjects:
            sentences.append(DerivedWording.note_present(subject))
            sentences.append(DerivedWording.note_absent(subject))
            sentences.append(DerivedWording.family_present(subject))
            sentences.append(DerivedWording.family_absent(subject))
        sentences.append(DerivedWording.occasion_fit("summer", ["citrus", "fresh"]))
        for sentence in sentences:
            lowered = sentence.lower()
            assert "wearers" not in lowered, sentence
            assert "people" not in lowered, sentence

    def test_declared_voice_names_the_declared_list_explicitly(self):
        """The DECLARED voice half of this module (literal notes) says
        "declared notes" — never a bare "vanilla present," which would
        read as an unqualified fact with no route back to "this is the
        official listing.\""""
        assert "declared notes" in DerivedWording.note_present("vanilla").lower()
        assert "declared notes" in DerivedWording.note_absent("vanilla").lower()

    def test_catalog_derived_family_voice_names_the_declared_profile(self):
        assert "declared profile" in DerivedWording.family_present("sweet").lower()
        assert "declared profile" in DerivedWording.family_absent("sweet").lower()
        assert "declared profile" in DerivedWording.occasion_fit("summer", ["citrus"]).lower()


# --- catalog_profile(conn): the T1 universe --------------------------------


class TestCatalogProfileBuild:
    def test_every_catalogue_fragrance_gets_an_entry_declared_or_not(self, conn):
        with_notes = add_fragrance(conn, "Has Notes")
        store_claims(
            conn, with_notes, [DeclaredNote("lemon", "top"), DeclaredNote("bergamot", "top")],
            claim_type="retailer_declared", source_name="test",
        )
        without_notes = add_fragrance(conn, "No Notes At All")

        profiles = catalog_profile(conn)

        assert set(profiles) >= {with_notes, without_notes}
        assert profiles[with_notes].has_catalog_data is True
        assert profiles[with_notes].declared_notes == frozenset({"lemon", "bergamot"})
        assert profiles[without_notes].has_catalog_data is False
        assert profiles[without_notes].declared_notes == frozenset()

    def test_a_bottle_with_two_citrus_notes_reads_citrus_high(self, conn):
        frag = add_fragrance(conn, "Citrus Bomb")
        store_claims(
            conn, frag, [DeclaredNote("lemon", "top"), DeclaredNote("bergamot", "top")],
            claim_type="retailer_declared", source_name="test",
        )
        profiles = catalog_profile(conn)
        assert profiles[frag].tendencies["citrus"] is Tendency.HIGH

    def test_a_bottle_with_no_declared_data_is_low_on_every_family_but_flagged_unknown(
        self, conn
    ):
        frag = add_fragrance(conn, "Mystery Bottle")
        profiles = catalog_profile(conn)
        profile = profiles[frag]
        assert profile.has_catalog_data is False
        assert all(t is Tendency.LOW for t in profile.tendencies.values())
