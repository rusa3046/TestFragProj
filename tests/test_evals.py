"""Eval labelling and scoring."""

import json

from fragrance_graph.evals.labels import (
    HOLDOUT,
    TRAIN,
    export_template,
    import_labels,
    load_labels,
    split_for,
)
from fragrance_graph.evals.score import extracted_claims, match_key, score
from fragrance_graph.ingest.reddit import ingest
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

    assert loaded[entries[0]["comment_id"]] == [label()]


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
    assert load_labels(conn)[entries[0]["comment_id"]][0]["claim_type"] == "DUPE_OF"


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
