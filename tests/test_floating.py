"""What the floating classifier is allowed to call recoverable.

Every test here pins a defect the first version of the module actually
had. The first run reported **51% of floating claims recoverable**. After
these four rules it reports 22%, and the difference was not a rounding
question — it was the classifier crediting attachments that would have
been wrong to make.

That matters more than the usual regression risk, because this number is
the one deciding whether attribution repair gets built. A classifier that
flatters itself here recommends spending on a return that cannot arrive.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from fragrance_graph.experiments import floating
from fragrance_graph.experiments.floating import (
    Cause,
    _is_flanker,
    _points_without_naming,
)


class TestAttachingIsNotGuessing:
    """A subject that *names* something is not recoverable to a bottle that
    merely appears nearby."""

    def test_deixis_points_and_is_recoverable(self):
        # "this is sweet" under a Delina review points at Delina.
        assert _points_without_naming("this fragrance", "Parfums de Marly Delina")
        assert _points_without_naming("it", "Parfums de Marly Delina")
        assert _points_without_naming("the perfume", "Parfums de Marly Delina")

    def test_a_different_bottle_is_not_recoverable(self):
        # The defect that mattered most: 'Arianna Grandes Cloud' was being
        # attached to Baccarat Rouge 540 because the *comment* mentioned
        # BR540 somewhere. That is fabrication with a citation attached.
        assert not _points_without_naming(
            "Arianna Grandes Cloud", "Maison Francis Kurkdjian Baccarat Rouge 540"
        )

    def test_the_bottles_own_name_still_points_at_it(self):
        assert _points_without_naming(
            "baccarat rouge 540", "Maison Francis Kurkdjian Baccarat Rouge 540"
        )


class TestFlankersAreNotTheirBaseBottle:
    def test_extrait_is_a_different_bottle(self):
        assert _is_flanker(
            "Baccarat rouge extrait", "Maison Francis Kurkdjian Baccarat Rouge 540"
        )

    def test_a_name_is_not_a_flanker_of_itself(self):
        # 'rouge' is a qualifier word *and* a word in this bottle's name.
        # Testing the raw subject rather than the excess would suppress the
        # bottle for containing its own name.
        assert not _is_flanker(
            "Baccarat Rouge 540", "Maison Francis Kurkdjian Baccarat Rouge 540"
        )


class TestWordsThatNameNoBottleAtAll:
    @pytest.mark.parametrize("subject", ["extrait", "the extrait", "The EDP"])
    def test_concentrations_are_not_missing_bottles(self, subject):
        words = set(subject.lower().split()) - floating.GENERIC_WORDS
        assert words and words <= floating.CONCENTRATION_ONLY, (
            "the catalogue is not missing a fragrance called 'extrait'; "
            "counting these as catalogue gaps overstates the gap"
        )

    @pytest.mark.parametrize("subject", ["clones", "dupes", "the OG"])
    def test_categories_are_not_missing_bottles(self, subject):
        words = set(subject.lower().split()) - floating.GENERIC_WORDS
        assert words and words <= floating.CATEGORY_WORDS


class TestRecoverableMeansRecoverable:
    def test_only_three_causes_count_as_upside(self):
        assert set(Cause.RECOVERABLE) == {
            Cause.VIDEO_SUBJECT,
            Cause.REPLY_PARENT,
            Cause.SINGLE_CANDIDATE,
        }

    @pytest.mark.parametrize(
        "cause",
        [
            Cause.AMBIGUOUS,
            Cause.SUBJECTLESS,
            Cause.NAMED_UNKNOWN,
            Cause.FLANKER,
            Cause.CONCENTRATION,
            Cause.CATEGORY,
        ],
    )
    def test_everything_else_is_the_ceiling(self, cause):
        assert cause not in Cause.RECOVERABLE


class TestItDoesNotWrite:
    """The cohort's treatment has to stay identical across all ten bottles,
    which means nothing may run `attributes infer` — or anything else that
    writes — while the experiment is open."""

    def test_every_statement_is_a_select(self):
        tree = ast.parse(pathlib.Path(floating.__file__).read_text())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            first = node.value.strip().split(None, 1)[0].upper() if node.value.strip() else ""
            if first in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}:
                pytest.fail(f"floating.py contains a writing statement: {node.value[:60]}")

    def test_it_does_not_commit(self):
        source = pathlib.Path(floating.__file__).read_text()
        tree = ast.parse(source)
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "commit" not in called
        assert "record" not in called, "no ledger writes: this module spends nothing"


class TestTheCeilingIsNotInflated:
    """Two P3 findings from adversarial review, both confirmed.

    Neither writes evidence -- the module reads -- but both overstate the
    recoverable ceiling that justifies building attribution repair, which
    is the number the whole experiment exists to produce.
    """

    def test_a_plural_subject_is_not_recoverable_to_one_bottle(self):
        """"Layton and Mystery both smell sweet" is a claim about two
        bottles. Attaching it to the one the catalogue recognises is
        exactly the error this module exists to avoid."""
        from fragrance_graph.experiments.floating import _points_without_naming

        for plural in ("both", "these", "those", "they", "the ones"):
            assert not _points_without_naming(plural, "Parfums de Marly Layton")

    def test_singular_deixis_still_points(self):
        from fragrance_graph.experiments.floating import _points_without_naming

        for singular in ("this", "it", "this one", "the fragrance"):
            assert _points_without_naming(singular, "Parfums de Marly Layton")

    def test_a_demonstrative_plus_a_bottle_name_is_not_deixis(self):
        """The regex allowed a demonstrative plus *any* word, so
        "Ce Sauvage" -- which names a bottle outright -- read as pointing
        and was counted recoverable to whatever the video was about."""
        from fragrance_graph.experiments.floating import FOREIGN_DEIXIS

        assert not FOREIGN_DEIXIS.match("Ce Sauvage")
        assert not FOREIGN_DEIXIS.match("este Sauvage")
        assert not FOREIGN_DEIXIS.match("questo Layton")

    def test_a_demonstrative_plus_a_generic_noun_still_is(self):
        from fragrance_graph.experiments.floating import FOREIGN_DEIXIS

        for form in ("este perfume", "ce parfum", "questo profumo",
                     "esta fragancia", "ini", "con này"):
            assert FOREIGN_DEIXIS.match(form)
