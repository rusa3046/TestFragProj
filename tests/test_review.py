"""Reviewing drafted labels.

The property that matters: `drafted_by` is removed only on rows a person
actually answered for. Everything else here is about that being true even
when the session is interrupted.
"""

import json

from fragrance_graph.evals.labels import UnreviewedDraft, check_reviewable
from fragrance_graph.evals.review import render_entry, review


def drafted(**over):
    entry = {
        "source": "youtube",
        "source_id": "a",
        "body": "khamrah is basically a dupe of angels share",
        "_stratum": "edge",
        "claims": [{
            "claim_type": "SIMILAR_TO",
            "raw_subject_text": "khamrah",
            "raw_object_text": "angels share",
            "sentiment": "POSITIVE",
        }],
        "drafted_by": "claude-opus-5",
        "pronoun_policy": "skip",
    }
    entry.update(over)
    return entry


def answers(*keys):
    """A canned reviewer."""
    queue = list(keys)
    return lambda _prompt: queue.pop(0) if queue else "q"


class TestSigningOff:
    def test_accepting_clears_the_marker(self):
        entries = [drafted()]
        progress = review(entries, ask=answers("a"), say=lambda _: None)
        assert "drafted_by" not in entries[0]
        assert "pronoun_policy" not in entries[0]
        assert progress.accepted == 1
        # And the file now passes the import guard.
        check_reviewable(entries, labeler="aanya-verified")

    def test_skipping_leaves_it_marked(self):
        entries = [drafted()]
        review(entries, ask=answers("s"), say=lambda _: None)
        assert entries[0]["drafted_by"] == "claude-opus-5"
        # Still refused, which is the point of skipping.
        try:
            check_reviewable(entries, labeler="aanya-verified")
        except UnreviewedDraft:
            pass
        else:  # pragma: no cover - the guard must fire
            raise AssertionError("a skipped row must not import")

    def test_asserting_nothing_is_a_decision_not_a_skip(self):
        """"This comment makes no claim" is a real label, and signed for."""
        entries = [drafted()]
        progress = review(entries, ask=answers("n"), say=lambda _: None)
        assert entries[0]["claims"] == []
        assert "drafted_by" not in entries[0]
        assert progress.emptied == 1

    def test_quitting_stops_without_signing_the_rest(self):
        entries = [drafted(source_id="a"), drafted(source_id="b"),
                   drafted(source_id="c")]
        review(entries, ask=answers("a", "q"), say=lambda _: None)
        assert "drafted_by" not in entries[0]
        assert entries[1]["drafted_by"]
        assert entries[2]["drafted_by"]

    def test_a_resumed_session_skips_what_is_done(self):
        entries = [drafted(source_id="a"), drafted(source_id="b")]
        review(entries, ask=answers("a", "q"), say=lambda _: None)
        progress = review(entries, ask=answers("a"), say=lambda _: None)
        assert progress.reviewed == 1, "should only revisit the marked row"
        assert all("drafted_by" not in e for e in entries)

    def test_progress_is_saved_after_every_answer(self):
        """An hour lost to a closed terminal is an hour nobody redoes."""
        saves = []
        entries = [drafted(source_id="a"), drafted(source_id="b")]
        review(entries, ask=answers("a", "a"), say=lambda _: None,
               save=lambda: saves.append(len(
                   [e for e in entries if "drafted_by" not in e])))
        assert saves == [1, 2]


class TestRetyping:
    def test_a_claim_type_can_be_corrected(self):
        """DUPE_OF vs SIMILAR_TO is the disagreement calibration found."""
        entries = [drafted()]
        review(entries, ask=answers("t", "DUPE_OF", "a"), say=lambda _: None)
        assert entries[0]["claims"][0]["claim_type"] == "DUPE_OF"
        assert "drafted_by" not in entries[0]

    def test_retyping_alone_does_not_sign_the_row(self):
        """A correction is not an approval; the row still needs answering."""
        entries = [drafted()]
        review(entries, ask=answers("t", "DUPE_OF", "s"), say=lambda _: None)
        assert entries[0]["claims"][0]["claim_type"] == "DUPE_OF"
        assert entries[0]["drafted_by"], "skipped after editing stays a draft"

    def test_a_nonsense_type_changes_nothing(self):
        entries = [drafted()]
        review(entries, ask=answers("t", "NOT_A_TYPE", "a"), say=lambda _: None)
        assert entries[0]["claims"][0]["claim_type"] == "SIMILAR_TO"

    def test_choosing_by_number_works(self):
        from fragrance_graph.models import ClaimType

        entries = [drafted()]
        review(entries, ask=answers("t", "1", "a"), say=lambda _: None)
        assert entries[0]["claims"][0]["claim_type"] == list(ClaimType)[0].value


class TestRendering:
    def test_it_shows_the_comment_and_the_claims(self):
        out = render_entry(drafted(), position=1, total=35)
        assert "1 of 35" in out
        assert "khamrah is basically a dupe" in out
        assert "SIMILAR_TO" in out
        assert "edge" in out

    def test_an_empty_draft_says_so_loudly(self):
        """The drafter finding nothing is the case worth reading closely."""
        out = render_entry(drafted(claims=[]), position=2, total=3)
        assert "NO CLAIMS" in out

    def test_an_unmarked_file_is_a_no_op(self):
        entries = [drafted()]
        entries[0].pop("drafted_by")
        progress = review(entries, ask=answers("a"), say=lambda _: None)
        assert progress.reviewed == 0


def test_a_reviewed_file_round_trips_as_json(tmp_path):
    entries = [drafted()]
    review(entries, ask=answers("a"), say=lambda _: None)
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    assert "drafted_by" not in json.loads(path.read_text())[0]


class TestPolarity:
    """A denial recorded as an assertion is the worst defect this corpus had.

    The drafter emits no polarity at all, so every drafted claim arrives as
    an implicit assertion — including "there's no clone of TF oud wood that
    captures the scent", which drafts as two claims saying those houses
    *are* dupes.
    """

    def test_a_denial_is_visible_before_it_is_signed(self):
        entry = drafted()
        entry["claims"][0]["polarity"] = "DENIED"
        out = render_entry(entry, position=1, total=1)
        assert "DENIED" in out

    def test_flipping_marks_the_claim_denied(self):
        entries = [drafted()]
        review(entries, ask=answers("p", "a"), say=lambda _: None)
        assert entries[0]["claims"][0]["polarity"] == "DENIED"
        assert "drafted_by" not in entries[0]

    def test_flipping_twice_returns_to_asserted(self):
        entries = [drafted()]
        review(entries, ask=answers("p", "p", "a"), say=lambda _: None)
        assert entries[0]["claims"][0]["polarity"] == "ASSERTED"

    def test_all_claims_can_be_flipped_at_once(self):
        """The real case: one denial covering several bottles."""
        entries = [drafted(claims=[
            {"claim_type": "DUPE_OF", "raw_subject_text": "Maison Alhambra",
             "raw_object_text": "TF Oud Wood", "sentiment": "NEGATIVE"},
            {"claim_type": "DUPE_OF", "raw_subject_text": "Afnan",
             "raw_object_text": "TF Oud Wood", "sentiment": "NEGATIVE"},
        ])]
        review(entries, ask=answers("p", "all", "a"), say=lambda _: None)
        assert [c["polarity"] for c in entries[0]["claims"]] == \
            ["DENIED", "DENIED"]

    def test_flipping_alone_does_not_sign_the_row(self):
        entries = [drafted()]
        review(entries, ask=answers("p", "s"), say=lambda _: None)
        assert entries[0]["claims"][0]["polarity"] == "DENIED"
        assert entries[0]["drafted_by"], "a correction is not an approval"


def test_the_drafter_is_asked_for_polarity():
    """The schema must let a denial be expressed, or it never will be."""
    from fragrance_graph.evals.autolabel import RESPONSE_SCHEMA, build_prompt

    claim = (RESPONSE_SCHEMA["properties"]["results"]["items"]["properties"]
             ["claims"]["items"])
    assert "polarity" in claim["properties"]
    assert "polarity" in claim["required"]
    assert set(claim["properties"]["polarity"]["enum"]) == {"ASSERTED", "DENIED"}

    prompt = build_prompt("skip")
    assert "DENIED" in prompt
    assert "not sentiment" in prompt


class TestRecheckingSignedRows:
    """Rows signed before polarity existed carry denials as assertions.

    Once a row is signed its marker is gone, so the normal pass skips it.
    28 rows were signed that way on 2026-08-11.
    """

    def _signed(self, body, claims):
        return {"source": "youtube", "source_id": "a", "body": body,
                "claims": claims}

    def test_a_signed_denial_is_found(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entry = self._signed(
            "I just try latafa it is nothing like angel share",
            [{"claim_type": "SIMILAR_TO", "raw_subject_text": "latafa",
              "raw_object_text": "angel share", "sentiment": "NEGATIVE"}],
        )
        assert looks_like_a_denial(entry) is True

    def test_one_already_marked_denied_is_left_alone(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entry = self._signed(
            "nothing like angel share",
            [{"claim_type": "SIMILAR_TO", "raw_subject_text": "latafa",
              "raw_object_text": "angel share", "polarity": "DENIED"}],
        )
        assert looks_like_a_denial(entry) is False

    def test_an_ordinary_row_is_not_reopened(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entry = self._signed(
            "khamrah is a great dupe of angels share",
            [{"claim_type": "DUPE_OF", "raw_subject_text": "khamrah",
              "raw_object_text": "angels share"}],
        )
        assert looks_like_a_denial(entry) is False

    def test_a_row_with_no_claims_is_not_reopened(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        assert looks_like_a_denial(
            self._signed("nothing like angel share", [])
        ) is False

    def test_the_selector_drives_which_rows_are_shown(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entries = [
            self._signed("nothing like angel share", [
                {"claim_type": "SIMILAR_TO", "raw_subject_text": "latafa",
                 "raw_object_text": "angel share"}]),
            self._signed("khamrah is a lovely dupe of angels share", [
                {"claim_type": "DUPE_OF", "raw_subject_text": "khamrah",
                 "raw_object_text": "angels share"}]),
        ]
        review(entries, ask=answers("p", "a"), say=lambda _: None,
               select=looks_like_a_denial)
        assert entries[0]["claims"][0]["polarity"] == "DENIED"
        assert "polarity" not in entries[1]["claims"][0], "untouched"


class TestRecheckTerminates:
    """Accepting a re-opened row must take it out of the filter.

    The first version tested for DENIED alone, so accepting left polarity
    absent, the row still matched, and it came back on every re-run —
    forever. Observed live on "Khamrah is nothing like angel share btw".
    """

    def _signed(self):
        return {"source": "youtube", "source_id": "a",
                "body": "Khamrah is nothing like angel share btw",
                "claims": [{"claim_type": "SIMILAR_TO",
                            "raw_subject_text": "Khamrah",
                            "raw_object_text": "angel share",
                            "sentiment": "NEGATIVE"}]}

    def test_accepting_ends_the_loop(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entries = [self._signed()]
        assert looks_like_a_denial(entries[0]) is True
        review(entries, ask=answers("a"), say=lambda _: None,
               select=looks_like_a_denial)
        assert looks_like_a_denial(entries[0]) is False, "would loop forever"
        assert entries[0]["claims"][0]["polarity"] == "ASSERTED"

    def test_flipping_also_ends_it(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entries = [self._signed()]
        review(entries, ask=answers("p", "a"), say=lambda _: None,
               select=looks_like_a_denial)
        assert looks_like_a_denial(entries[0]) is False
        assert entries[0]["claims"][0]["polarity"] == "DENIED"

    def test_emptying_also_ends_it(self):
        from fragrance_graph.evals.review import looks_like_a_denial

        entries = [self._signed()]
        review(entries, ask=answers("n"), say=lambda _: None,
               select=looks_like_a_denial)
        assert looks_like_a_denial(entries[0]) is False

    def test_skipping_deliberately_keeps_it(self):
        """Only skipping should bring a row back."""
        from fragrance_graph.evals.review import looks_like_a_denial

        entries = [self._signed()]
        review(entries, ask=answers("s"), say=lambda _: None,
               select=looks_like_a_denial)
        assert looks_like_a_denial(entries[0]) is True

    def test_an_accepted_assertion_is_explicitly_recorded(self):
        """"Confirmed asserted" and "never looked at" must differ."""
        from fragrance_graph.evals.review import stamp_polarity

        entry = self._signed()
        assert "polarity" not in entry["claims"][0]
        stamp_polarity(entry)
        assert entry["claims"][0]["polarity"] == "ASSERTED"

    def test_stamping_never_overwrites_a_denial(self):
        from fragrance_graph.evals.review import stamp_polarity

        entry = self._signed()
        entry["claims"][0]["polarity"] = "DENIED"
        stamp_polarity(entry)
        assert entry["claims"][0]["polarity"] == "DENIED"


class TestUnreadRows:
    """A silent batch arrives with no draft and no claims.

    Neither the draft marker nor the denial filter selects those, so they
    look finished and import as "these comments assert nothing" — which is
    a real claim about the corpus that nobody made. 15 were imported that
    way on 2026-08-11.
    """

    def _blank(self, body="Khamrah smells just like Angels Share"):
        return {"source": "youtube", "source_id": "a", "_stratum": "silent",
                "body": body, "claims": []}

    def test_a_blank_row_needs_reading(self):
        from fragrance_graph.evals.review import needs_reading

        assert needs_reading(self._blank()) is True

    def test_answering_takes_it_out_of_the_set(self):
        from fragrance_graph.evals.review import needs_reading

        entries = [self._blank()]
        review(entries, ask=answers("n"), say=lambda _: None,
               select=needs_reading)
        assert needs_reading(entries[0]) is False, "would be asked forever"
        assert entries[0]["_reviewed"] is True

    def test_skipping_keeps_it(self):
        from fragrance_graph.evals.review import needs_reading

        entries = [self._blank()]
        review(entries, ask=answers("s"), say=lambda _: None,
               select=needs_reading)
        assert needs_reading(entries[0]) is True

    def test_a_claim_can_be_typed_in(self):
        from fragrance_graph.evals.review import needs_reading

        entries = [self._blank()]
        review(entries,
               ask=answers("+", "Khamrah", "Angels Share", "2", "y", "a"),
               say=lambda _: None, select=needs_reading)
        claim = entries[0]["claims"][0]
        assert claim["claim_type"] == "SIMILAR_TO"
        assert claim["raw_subject_text"] == "Khamrah"
        assert claim["raw_object_text"] == "Angels Share"
        assert claim["polarity"] == "ASSERTED"

    def test_a_typed_denial_is_recorded_as_one(self):
        from fragrance_graph.evals.review import needs_reading

        entries = [self._blank("Khamrah is nothing like Angels Share")]
        review(entries,
               ask=answers("+", "Khamrah", "Angels Share", "2", "n", "a"),
               say=lambda _: None, select=needs_reading)
        assert entries[0]["claims"][0]["polarity"] == "DENIED"

    def test_a_dupe_can_be_typed_in(self):
        from fragrance_graph.evals.review import needs_reading

        entries = [self._blank()]
        review(entries,
               ask=answers("+", "Khamrah", "Angels Share", "1", "y", "a"),
               say=lambda _: None, select=needs_reading)
        assert entries[0]["claims"][0]["claim_type"] == "DUPE_OF"

    def test_an_empty_subject_adds_nothing(self):
        from fragrance_graph.evals.review import needs_reading

        entries = [self._blank()]
        review(entries, ask=answers("+", "", "n"), say=lambda _: None,
               select=needs_reading)
        assert entries[0]["claims"] == []
        assert entries[0]["_reviewed"] is True
