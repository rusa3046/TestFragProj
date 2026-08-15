"""The harness behind the semantic decision.

A benchmark that scores itself wrongly is worse than none, because the
wrong number gets quoted in a commit message and then in a decision. Two
of these tests exist because exactly that happened.
"""

import pytest

from fragrance_graph.evals.fuzzy import (
    ArmResult,
    FuzzyCase,
    _matches_label,
    load_cases,
    run_arm,
)
from fragrance_graph.semantic import HashedNGrams


class TestScoring:
    def test_precision_counts_results_not_labels(self):
        """One retrieved item can satisfy two hand-listed labels. Counting
        labels over results gave precision above 1.0 — the committed
        number was 0.206 and the true figure is 0.494."""
        case = FuzzyCase(id="x", query="coffee",
                         relevant=["coffee", "coffee note"])
        result = ArmResult(
            case=case,
            retrieved=["coffee note"],
            hits=["coffee", "coffee note"],
            relevant_results=["coffee note"],
        )
        assert result.precision == 1.0
        assert result.recall == 1.0

    def test_precision_can_never_exceed_one(self):
        case = FuzzyCase(id="x", query="rose",
                         relevant=["rose", "rosy", "roses"])
        result = ArmResult(
            case=case, retrieved=["rosy-fruity"],
            hits=["rosy"], relevant_results=["rosy-fruity"],
        )
        assert 0.0 <= result.precision <= 1.0

    def test_an_empty_result_scores_zero_rather_than_dividing_by_zero(self):
        case = FuzzyCase(id="x", query="q", relevant=["a"])
        assert ArmResult(case=case).precision == 0.0
        assert ArmResult(case=case).recall == 0.0

    def test_recall_is_over_the_labels_not_the_results(self):
        case = FuzzyCase(id="x", query="q", relevant=["a", "b", "c", "d"])
        result = ArmResult(case=case, retrieved=["a"], hits=["a"],
                           relevant_results=["a"])
        assert result.recall == 0.25


class TestLabelMatching:
    def test_a_compound_descriptor_satisfies_its_head_word(self):
        """The corpus writes "coffee note" for what a reviewer lists as
        "coffee"; equality would score the normaliser, not retrieval."""
        assert _matches_label("coffee note", "coffee")
        assert _matches_label("rosy-fruity", "rosy")

    def test_a_longer_word_does_not_satisfy_a_shorter_one(self):
        """Bare substring credited "rosewood" for "rose" and
        "lightweight" for "light", inflating every arm — and inflating
        most the arm with the noisiest vocabulary."""
        assert not _matches_label("rosewood", "rose")
        assert not _matches_label("lightweight", "light")

    def test_a_multiword_label_still_matches_as_a_phrase(self):
        assert _matches_label("burnt coffee note", "coffee note")

    def test_matching_is_case_insensitive(self):
        assert _matches_label("Coffee Note", "coffee")


class TestTheEvalSet:
    def test_it_has_adversarial_cases(self):
        """Half the point. Choosing on relevance alone adopted a model that
        retrieves the opposite of what was asked."""
        cases = load_cases()
        adversarial = [c for c in cases if c.adversarial]
        assert len(adversarial) >= 5

    def test_every_adversarial_case_names_what_must_not_come_back(self):
        for case in load_cases():
            if case.adversarial:
                assert case.forbidden
                assert not set(case.relevant) & set(case.forbidden), (
                    f"{case.id} lists a term as both relevant and forbidden"
                )

    def test_the_antonym_cases_are_present(self):
        """The specific failures the spot-check hinted at."""
        ids = {c.id for c in load_cases()}
        assert {"adv-airy-not-heavy", "adv-feminine-not-masculine"} <= ids

    def test_every_case_has_a_stated_rationale(self):
        for case in load_cases():
            assert case.note, f"{case.id} does not say why it exists"


class TestRunningAnArm:
    def test_it_scores_without_a_database(self):
        vocabulary = ["rose", "roses", "tobacco", "coffee", "coffee note"]
        cases = [FuzzyCase(id="x", query="rose", relevant=["rose", "roses"],
                           forbidden=["tobacco"])]
        report = run_arm(HashedNGrams(), vocabulary, cases, k=3)
        (result,) = report.results
        assert "rose" in result.retrieved
        assert result.recall > 0

    def test_forbidden_terms_are_counted_separately(self):
        """"airy" really does pull "heavy" from the lexical baseline on the
        live corpus, which is why that adversarial case exists."""
        vocabulary = ["airy", "heavy"]
        cases = [FuzzyCase(id="x", query="airy", relevant=["airy"],
                           forbidden=["heavy"])]
        report = run_arm(HashedNGrams(), vocabulary, cases, k=2)
        assert report.forbidden == 1
        assert report.cases_with_forbidden == 1

    def test_a_zero_scoring_term_is_never_retrieved(self):
        """"rose" and "tobacco" share no n-grams at all, so the baseline
        scores them 0.0 and the score>0 filter drops them — which is the
        filter doing its job rather than a gap in the harness."""
        vocabulary = ["rose", "tobacco"]
        cases = [FuzzyCase(id="x", query="tobacco", relevant=["tobacco"],
                           forbidden=["rose"])]
        report = run_arm(HashedNGrams(), vocabulary, cases, k=2)
        assert report.results[0].retrieved == ["tobacco"]
        assert report.forbidden == 0

    def test_an_arm_returning_fewer_than_k_is_scored_on_what_it_returned(self):
        vocabulary = ["rose"]
        cases = [FuzzyCase(id="x", query="rose", relevant=["rose"])]
        (result,) = run_arm(HashedNGrams(), vocabulary, cases, k=10).results
        assert len(result.retrieved) == 1
        assert result.precision == 1.0


class TestFittingIsDeterministic:
    """P3. The original test compared two reads of one fitted object and
    would have passed had `fit` chosen contexts randomly."""

    CORPUS = [
        "rose and tobacco and smoke together",
        "airy light fresh clean soft",
        "heavy thick cloying strong",
    ] * 5

    def test_two_independent_fits_agree(self):
        from fragrance_graph.semantic import CorpusDistributional

        a = CorpusDistributional(dim=20).fit(self.CORPUS)
        b = CorpusDistributional(dim=20).fit(self.CORPUS)
        assert a.contexts == b.contexts
        assert a.embed("rose") == b.embed("rose")

    def test_document_order_does_not_change_the_space(self):
        from fragrance_graph.semantic import CorpusDistributional

        a = CorpusDistributional(dim=20).fit(self.CORPUS)
        b = CorpusDistributional(dim=20).fit(list(reversed(self.CORPUS)))
        assert a.contexts == b.contexts
        assert a.embed("airy") == b.embed("airy")

    def test_refitting_the_same_instance_does_not_accumulate(self):
        from fragrance_graph.semantic import CorpusDistributional

        model = CorpusDistributional(dim=20)
        first = model.fit(self.CORPUS).embed("rose")
        second = model.fit(self.CORPUS).embed("rose")
        assert first == second


class TestThePaidArmIsGuardedForReal:
    """P1. The previous test supplied a callback and never called `embed`,
    so it passed while the default path spent money silently."""

    def test_constructing_it_without_a_ledger_hook_fails(self):
        from fragrance_graph.semantic import OpenAIEmbeddings

        with pytest.raises(ValueError, match="on_spend"):
            OpenAIEmbeddings(api_key="k")

    def test_a_call_charges_the_amount_the_provider_reports(self, monkeypatch):
        import json as _json

        from fragrance_graph import semantic

        charged = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return _json.dumps({
                    "data": [{"embedding": [0.1, 0.2]}],
                    "usage": {"total_tokens": 1_000_000},
                }).encode()

        monkeypatch.setattr(
            "urllib.request.urlopen", lambda *a, **k: FakeResponse()
        )
        embedder = semantic.OpenAIEmbeddings(
            api_key="k", on_spend=lambda usd, n: charged.append(usd)
        )
        embedder.embed("rose")
        assert charged == [pytest.approx(0.02)], (
            "a million tokens at $0.02/M must charge $0.02"
        )
