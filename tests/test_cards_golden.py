"""The committed cards still match what the product renders.

Six defects reached a person's screen in one week — an inverted note
match, a catalog sentence in a community frame, a chip counted twice, a
declared note worth a quarter of a comment, a performance chip compiled
as a note, and a headcount line reading "0 people across 0 channels" —
and all six passed the whole suite. They were invisible for one reason:
the suite tested functions, and every one of those bugs lived in the
*assembled card*, which is the only thing a shopper ever sees.

`evals/cards.py` renders those cards through `api._session_response` —
the same function the kiosk's HTTP handlers return — and commits the
result. This module is the gate: if the rendered cards drift from the
committed file, something changed what a customer reads, and a person
has to look at it and say whether that was the point.

A failure here is not automatically a regression. Most drift will be
intended, and the fix is `--update` plus reading the diff. The value is
that the change is *seen*, in review, instead of reaching a shopper
first.

Skipped without the developer database, exactly as
`tests/test_evals_recommend.py` is and for the same reason: these ask
what the product says about real evidence, and a fixture would prove the
renderer runs while proving nothing about the answers.
"""

import pytest

from fragrance_graph.db import DEFAULT_DB_URL, get_connection
from fragrance_graph.evals.cards import (
    DEFAULT_CASES,
    DEFAULT_GOLDEN,
    load_cases,
    render_all,
)


@pytest.fixture(scope="module")
def corpus():
    """The developer database, skipped if it is not built."""
    try:
        conn = get_connection(DEFAULT_DB_URL)
    except Exception:  # noqa: BLE001 - any connection failure means skip
        pytest.skip("no developer database; run `corpus import` first")
    if conn.execute("SELECT count(*) FROM claims").fetchone()[0] < 100:
        pytest.skip("developer database is empty; run `corpus import` first")
    yield conn
    conn.close()


class TestTheCaseSetItself:
    def test_every_case_loads(self):
        assert load_cases(DEFAULT_CASES)

    def test_every_case_says_why_it_is_here(self):
        """A pinned card nobody can explain is a card nobody will dare
        change. The `why` is what makes a diff reviewable by somebody who
        was not in the room when the bug was found."""
        for case in load_cases(DEFAULT_CASES):
            assert case.get("why"), case["id"]

    def test_case_ids_are_unique(self):
        ids = [case["id"] for case in load_cases(DEFAULT_CASES)]
        assert len(ids) == len(set(ids))

    def test_the_defects_that_reached_a_screen_are_all_covered(self):
        """One case per bug a person had to find by eye. Named
        explicitly so deleting a case has to be a deliberate act rather
        than a tidy-up."""
        ids = {case["id"] for case in load_cases(DEFAULT_CASES)}
        assert {
            "regression-less-sweet-inversion",
            "regression-less-sweet-as-an-avoid",
            "regression-catalog-sentence-in-community-frame",
            "regression-duplicate-chip",
            "regression-performance-chip-is-performance",
            "weighting-declared-note-vs-one-commenter",
        } <= ids


class TestTheCardsHaveNotDrifted:
    def test_rendered_cards_match_the_committed_golden(self, corpus):
        rendered = render_all(corpus, load_cases(DEFAULT_CASES))
        committed = DEFAULT_GOLDEN.read_text(encoding="utf-8")
        assert rendered == committed, (
            "The cards a customer reads have changed.\n"
            "Run `uv run python -m fragrance_graph.evals.cards --update`, "
            "read the diff, and commit it with the change that caused it."
        )
