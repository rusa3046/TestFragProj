"""Eval labelling and scoring."""

import json

import pytest

from fragrance_graph.evals.labels import (
    HOLDOUT,
    TRAIN,
    export_template,
    import_labels,
    load_labels,
    split_for,
)
from fragrance_graph.evals.score import extracted_claims, match_key, score
from fragrance_graph.ingest.store import ingest
from tests.conftest import make_comment


def label(**overrides):
    base = {
        "claim_type": "SIMILAR_TO",
        "raw_subject_text": "Delina",
        "raw_object_text": "Baccarat 540",
        "sentiment": "NEUTRAL",
    }
    return {**base, **overrides}


# --- split ------------------------------------------------------------------


def test_split_is_deterministic():
    assert split_for("t1_abc") == split_for("t1_abc")


def test_split_keys_on_source_id_not_row_id():
    """A database rebuild must not reshuffle the holdout and leak it."""
    assert split_for("t1_abc") != "" and split_for("t1_xyz") != ""
    # Same source_id, different hypothetical row ids — still the same split.
    assert split_for("t1_abc") == split_for("t1_abc")


def test_split_produces_both_buckets():
    seen = {split_for(f"t1_{i:04d}") for i in range(200)}
    assert seen == {TRAIN, HOLDOUT}


def test_split_is_roughly_the_configured_fraction():
    ids = [f"t1_{i:05d}" for i in range(2000)]
    train = sum(1 for i in ids if split_for(i) == TRAIN)
    assert 0.65 < train / len(ids) < 0.75


def test_train_fraction_is_configurable():
    ids = [f"t1_{i:05d}" for i in range(2000)]
    train = sum(1 for i in ids if split_for(i, train_fraction=0.5) == TRAIN)
    assert 0.45 < train / len(ids) < 0.55


# --- export / import --------------------------------------------------------


def test_export_produces_one_entry_per_comment_with_empty_claims(conn):
    ingest(conn, [make_comment(i) for i in range(3)])
    entries = export_template(conn)

    assert len(entries) == 3
    assert all(e["claims"] == [] for e in entries)
    assert all(e["split"] in (TRAIN, HOLDOUT) for e in entries)
    assert all(e["body"] for e in entries), "body needed to label by hand"


def test_import_round_trips(conn):
    ingest(conn, [make_comment(0)])
    entries = export_template(conn)
    entries[0]["claims"] = [label()]

    import_labels(conn, entries, labeler="aanya")
    loaded = load_labels(conn, labeler="aanya")

    only_id = conn.execute("SELECT id FROM comments").fetchone()[0]
    assert loaded[only_id] == [label()]


def test_reimport_replaces_rather_than_duplicates(conn):
    """Relabelling a comment must not leave two conflicting answers."""
    ingest(conn, [make_comment(0)])
    entries = export_template(conn)

    entries[0]["claims"] = [label()]
    import_labels(conn, entries, labeler="aanya")
    entries[0]["claims"] = [label(claim_type="DUPE_OF")]
    import_labels(conn, entries, labeler="aanya")

    rows = conn.execute("SELECT count(*) FROM eval_labels").fetchone()[0]
    assert rows == 1
    only_id = conn.execute("SELECT id FROM comments").fetchone()[0]
    assert load_labels(conn)[only_id][0]["claim_type"] == "DUPE_OF"


def test_two_labelers_coexist(conn):
    ingest(conn, [make_comment(0)])
    entries = export_template(conn)
    entries[0]["claims"] = [label()]

    import_labels(conn, entries, labeler="aanya")
    import_labels(conn, entries, labeler="colleague")

    assert conn.execute("SELECT count(*) FROM eval_labels").fetchone()[0] == 2


def test_load_can_filter_by_split(conn):
    ingest(conn, [make_comment(i) for i in range(40)])
    entries = export_template(conn)
    import_labels(conn, entries, labeler="aanya")

    train = load_labels(conn, split=TRAIN)
    holdout = load_labels(conn, split=HOLDOUT)

    assert train and holdout
    assert not (set(train) & set(holdout)), "a comment cannot be in both"
    assert len(train) + len(holdout) == 40


# --- scoring ----------------------------------------------------------------


def test_perfect_extraction_scores_one():
    labels = {1: [label()]}
    got = {1: [dict(label(), comment_id=1)]}
    report = score(got, labels)

    assert (report.overall.precision, report.overall.recall) == (1.0, 1.0)
    assert report.overall.f1 == 1.0


def test_missing_claim_is_a_false_negative():
    report = score({}, {1: [label()]})
    assert report.overall.recall == 0.0
    assert report.overall.false_negatives == 1


def test_invented_claim_is_a_false_positive():
    report = score({1: [label()]}, {1: []})
    assert report.overall.precision == 0.0
    assert report.overall.false_positives == 1


def test_unlabelled_comments_are_skipped_not_penalised():
    """An unlabelled comment is unknown, not known-empty."""
    got = {1: [label()], 99: [label(raw_subject_text="Aventus")]}
    report = score(got, {1: [label()]})

    assert report.overall.precision == 1.0
    assert report.overall.false_positives == 0


def test_matching_ignores_case_and_whitespace():
    labels = {1: [label(raw_subject_text="Delina")]}
    got = {1: [label(raw_subject_text="  delina  ")]}
    assert score(got, labels).overall.true_positives == 1


def test_wrong_claim_type_is_both_a_miss_and_a_false_positive():
    report = score({1: [label(claim_type="DUPE_OF")]}, {1: [label()]})

    assert report.overall.true_positives == 0
    assert report.overall.false_positives == 1
    assert report.overall.false_negatives == 1


def test_wrong_sentiment_still_matches_but_lowers_agreement():
    """Right claim, wrong polarity is a partial success, not a miss."""
    labels = {1: [label(sentiment="POSITIVE")]}
    got = {1: [label(sentiment="NEGATIVE")]}
    report = score(got, labels)

    assert report.overall.true_positives == 1
    assert report.overall.sentiment_accuracy == 0.0


def test_correct_sentiment_scores_full_agreement():
    report = score({1: [label(sentiment="POSITIVE")]}, {1: [label(sentiment="POSITIVE")]})
    assert report.overall.sentiment_accuracy == 1.0


def test_per_type_breakdown_isolates_the_failing_type():
    labels = {1: [label(), label(claim_type="LONGEVITY", raw_object_text=None)]}
    got = {1: [label()]}
    report = score(got, labels)

    assert report.by_type["SIMILAR_TO"].recall == 1.0
    assert report.by_type["LONGEVITY"].recall == 0.0


def test_report_renders_overall_and_per_type():
    report = score({1: [label()]}, {1: [label()]})
    rendered = report.render()

    assert "OVERALL" in rendered
    assert "SIMILAR_TO" in rendered


def test_extracted_claims_reads_from_the_database(conn):
    from fragrance_graph.extract.llm import write_claims
    from fragrance_graph.models import Claim

    ingest(conn, [make_comment(0, body="Delina smells just like Baccarat 540")])
    (comment_id,) = [r[0] for r in conn.execute("SELECT id FROM comments")]
    claim = Claim(
        claim_type="SIMILAR_TO",
        subject_kind="FRAGRANCE",
        raw_subject_text="Delina",
        object_kind="FRAGRANCE",
        raw_object_text="Baccarat 540",
        confidence=0.9,
        evidence_span="smells just like Baccarat 540",
    )
    write_claims(conn, comment_id, "Delina smells just like Baccarat 540", [claim])

    got = extracted_claims(conn)
    assert got[comment_id][0]["claim_type"] == "SIMILAR_TO"


def test_match_key_treats_absent_object_as_empty():
    a = match_key(1, {"claim_type": "LONGEVITY", "raw_subject_text": "X"})
    b = match_key(1, {"claim_type": "LONGEVITY", "raw_subject_text": "X",
                      "raw_object_text": None})
    assert a == b


def test_labels_are_stored_as_readable_json(conn):
    """A labeller must be able to inspect and correct these by hand."""
    ingest(conn, [make_comment(0)])
    entries = export_template(conn)
    entries[0]["claims"] = [label()]
    import_labels(conn, entries, labeler="aanya")

    raw = conn.execute("SELECT labeled_json FROM eval_labels").fetchone()[0]
    assert json.loads(raw)["claims"][0]["claim_type"] == "SIMILAR_TO"


# --- template identity ------------------------------------------------------


def test_template_is_keyed_on_source_id_not_autoincrement_id(conn):
    """A template exported from one database must not silently apply to another."""
    from fragrance_graph.evals.labels import export_template

    ingest(conn, [make_comment(1, body="a")])
    entry = export_template(conn)[0]

    assert "source_id" in entry and "source" in entry
    assert "comment_id" not in entry


def test_importing_a_foreign_template_raises_rather_than_mislabelling(conn):
    """The failure this guards: 291-comment database, 3155-comment corpus.

    A label whose comment is absent must not be written to whatever row
    happens to hold that id, and must not be silently skipped either.
    """
    from fragrance_graph.evals.labels import UnknownComment, import_labels

    ingest(conn, [make_comment(1, body="a")])
    foreign = [
        {"source": "youtube", "source_id": "not-in-this-corpus",
         "split": "train", "claims": []}
    ]

    with pytest.raises(UnknownComment, match="different corpus"):
        import_labels(conn, foreign, labeler="aanya")


def test_labels_attach_to_the_right_comment_after_renumbering(conn, tmp_path):
    """Export from one database, import into another where ids differ."""
    from fragrance_graph.db import get_connection, migrate
    from fragrance_graph.evals.labels import export_template, import_labels

    ingest(conn, [make_comment(7, body="the labelled one")])
    template = export_template(conn)
    template[0]["claims"] = [{"claim_type": "DUPE_OF", "raw_subject_text": "X",
                              "raw_object_text": "Y", "sentiment": "NEUTRAL"}]

    other = get_connection(tmp_path / "other.db")
    migrate(other)
    ingest(other, [make_comment(i, body="filler") for i in range(20, 30)])
    ingest(other, [make_comment(7, body="the labelled one")])

    import_labels(other, template, labeler="aanya")

    body = other.execute(
        "SELECT co.body FROM eval_labels l JOIN comments co ON co.id = l.comment_id"
    ).fetchone()[0]
    assert body == "the labelled one"
    other.close()


def test_legacy_comment_id_templates_still_import_with_a_warning(conn, caplog):
    import logging

    from fragrance_graph.evals.labels import import_labels

    ingest(conn, [make_comment(1, body="a")])
    comment_id = conn.execute("SELECT id FROM comments").fetchone()[0]

    with caplog.at_level(logging.WARNING):
        import_labels(conn, [{"comment_id": comment_id, "claims": []}],
                      labeler="aanya")

    assert "comment_id" in "\n".join(r.getMessage() for r in caplog.records)


# --- sampling ---------------------------------------------------------------


def test_sample_spreads_across_the_corpus_not_the_first_n(conn):
    """The first 50 rows are one video's comment section, not a sample."""
    from fragrance_graph.evals.labels import export_template

    ingest(conn, [make_comment(i, body=f"c{i}") for i in range(100)])

    sampled = {e["source_id"] for e in export_template(conn, sample=10)}
    first_ten = {e["source_id"] for e in export_template(conn, limit=10)}

    assert len(sampled) == 10
    assert sampled != first_ten


def test_sample_is_deterministic(conn):
    """A labelling session has to be resumable."""
    from fragrance_graph.evals.labels import export_template

    ingest(conn, [make_comment(i, body=f"c{i}") for i in range(50)])

    first = [e["source_id"] for e in export_template(conn, sample=10)]
    second = [e["source_id"] for e in export_template(conn, sample=10)]

    assert first == second


def test_sample_larger_than_corpus_returns_everything(conn):
    from fragrance_graph.evals.labels import export_template

    ingest(conn, [make_comment(i, body=f"c{i}") for i in range(5)])
    assert len(export_template(conn, sample=999)) == 5


def test_agreeing_that_nothing_was_claimed_is_not_a_zero_score():
    """Most comments assert nothing; agreeing on that is the common case.

    Reporting it as P 0.00 R 0.00 F1 0.00 reads as total failure.
    """
    from fragrance_graph.evals.score import Score

    empty = Score()
    assert empty.is_vacuous
    assert "nothing to score" in empty.line("OVERALL")
    assert "0.00" not in empty.line("OVERALL")


def test_a_real_zero_still_reports_zero():
    from fragrance_graph.evals.score import Score

    missed = Score(false_negatives=3)
    assert not missed.is_vacuous
    assert "F1 0.00" in missed.line("OVERALL")
    assert "fn 3" in missed.line("OVERALL")


def test_importing_an_unedited_template_warns(conn, caplog):
    """An exported template has empty claims everywhere; importing it
    unedited is far more likely a mistake than a corpus-wide judgement."""
    import logging

    ingest(conn, [make_comment(i) for i in range(5)])
    entries = export_template(conn)

    with caplog.at_level(logging.WARNING):
        import_labels(conn, entries, labeler="aanya")

    assert "None of the 5 entries has any claims" in "\n".join(
        r.getMessage() for r in caplog.records
    )


def test_a_partly_filled_import_does_not_warn(conn, caplog):
    import logging

    ingest(conn, [make_comment(i) for i in range(5)])
    entries = export_template(conn)
    entries[0]["claims"] = [label()]

    with caplog.at_level(logging.WARNING):
        import_labels(conn, entries, labeler="aanya")

    assert "None of the" not in "\n".join(r.getMessage() for r in caplog.records)


# --- the edge view ----------------------------------------------------------


def edge_claim(claim_type, subject="Sand+Fog Sweet Rose", obj="DE"):
    return {
        "claim_type": claim_type,
        "raw_subject_text": subject,
        "raw_object_text": obj,
        "sentiment": "POSITIVE",
    }


def test_dupe_versus_similar_is_a_match_in_the_edge_view():
    """Observed live: one assertion, typed two ways, scored as two errors."""
    from fragrance_graph.evals.score import edge_score

    got = {1: [edge_claim("SIMILAR_TO")]}
    expected = {1: [edge_claim("DUPE_OF")]}

    assert score(got, expected).overall.f1 == 0.0
    assert edge_score(got, expected).f1 == 1.0


def test_better_than_is_not_collapsed_into_the_edge_view():
    """A preference claim is not a similarity claim; the product keeps them apart."""
    from fragrance_graph.evals.score import edge_score

    got = {1: [edge_claim("BETTER_THAN")]}
    expected = {1: [edge_claim("SIMILAR_TO")]}

    assert edge_score(got, expected).f1 == 0.0


def test_edge_view_still_catches_a_wrong_object():
    """Collapsing the type must not collapse the entities."""
    from fragrance_graph.evals.score import edge_score

    got = {1: [edge_claim("SIMILAR_TO", obj="Aventus")]}
    expected = {1: [edge_claim("DUPE_OF", obj="DE")]}

    assert edge_score(got, expected).f1 == 0.0


def test_non_edge_types_are_untouched_by_collapsing():
    from fragrance_graph.evals.score import collapse_similarity

    claims = {1: [{"claim_type": "LONGEVITY", "raw_subject_text": "X",
                   "raw_object_text": None, "sentiment": "NEGATIVE"}]}
    assert collapse_similarity(claims)[1][0]["claim_type"] == "LONGEVITY"


class TestImportRefusesToDestroyGroundTruth:
    """The two imports that silently turned an eval set into model output.

    Both happened on 2026-08-11: a drafted file went in under a human
    labeler, and an unfilled file asserted that 15 comments say nothing.
    `import` only checked that the comments existed.
    """

    def _entry(self, **over):
        e = {"source": "youtube", "source_id": "abc", "claims": []}
        e.update(over)
        return e

    def test_a_drafted_file_cannot_become_human_judgement(self):
        from fragrance_graph.evals.labels import UnreviewedDraft, check_reviewable

        entries = [self._entry(
            drafted_by="claude-opus-5",
            claims=[{"claim_type": "DUPE_OF", "raw_subject_text": "a",
                     "raw_object_text": "b"}],
        )]
        with pytest.raises(UnreviewedDraft) as exc:
            check_reviewable(entries, labeler="aanya-verified")
        assert "agreement between models" in str(exc.value)
        assert "opus5-draft" in str(exc.value)

    def test_drafts_import_fine_under_the_drafter(self):
        from fragrance_graph.evals.labels import check_reviewable

        entries = [self._entry(
            drafted_by="claude-opus-5",
            claims=[{"claim_type": "DUPE_OF", "raw_subject_text": "a",
                     "raw_object_text": "b"}],
        )]
        check_reviewable(entries, labeler="opus5-draft")

    def test_dropping_the_marker_records_that_a_person_stands_behind_it(self):
        """Reviewing means removing the provenance, deliberately."""
        from fragrance_graph.evals.labels import check_reviewable

        entries = [self._entry(claims=[
            {"claim_type": "DUPE_OF", "raw_subject_text": "a",
             "raw_object_text": "b"}
        ])]
        check_reviewable(entries, labeler="aanya-verified")

    def test_an_unfilled_file_is_refused(self):
        from fragrance_graph.evals.labels import UnreviewedDraft, check_reviewable

        with pytest.raises(UnreviewedDraft) as exc:
            check_reviewable([self._entry(), self._entry(source_id="def")],
                             labeler="aanya-verified")
        assert "--allow-empty" in str(exc.value)

    def test_genuinely_empty_labels_are_allowed_when_said_so(self):
        from fragrance_graph.evals.labels import check_reviewable

        check_reviewable([self._entry()], labeler="aanya-verified",
                         allow_empty=True)

    def test_allow_empty_does_not_also_wave_through_drafts(self):
        """The two failures are separate, and the flag only excuses one."""
        from fragrance_graph.evals.labels import UnreviewedDraft, check_reviewable

        with pytest.raises(UnreviewedDraft):
            check_reviewable(
                [self._entry(drafted_by="claude-opus-5")],
                labeler="aanya-verified",
                allow_empty=True,
            )

    def test_a_partly_filled_file_passes(self):
        from fragrance_graph.evals.labels import check_reviewable

        check_reviewable(
            [self._entry(), self._entry(source_id="def", claims=[
                {"claim_type": "DUPE_OF", "raw_subject_text": "a",
                 "raw_object_text": "b"}])],
            labeler="aanya-verified",
        )
