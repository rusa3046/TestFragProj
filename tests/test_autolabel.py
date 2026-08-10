"""Drafting eval labels with a stronger model.

The properties that matter are about trust, not accuracy: a draft must
never be able to pass as a human judgement, a failed batch must never look
like "no claims", and the calibration set must not leak the drafts.

No live API calls — a fake client drives the whole path.
"""

import json

import pytest

from fragrance_graph.evals.autolabel import (
    DRAFT_LABELER,
    MODEL,
    PRONOUN_POLICIES,
    RESPONSE_SCHEMA,
    agreement,
    build_prompt,
    calibration_subset,
    draft,
    parse_batch,
)
from fragrance_graph.evals.labels import export_template, import_labels
from fragrance_graph.ingest.reddit import ingest
from tests.conftest import make_comment

BODY = "Zara Red Temptation is a dupe of BR540"


def claim(**overrides):
    base = {
        "claim_type": "DUPE_OF",
        "raw_subject_text": "Zara Red Temptation",
        "raw_object_text": "BR540",
        "sentiment": "NEUTRAL",
    }
    return {**base, **overrides}


def response_json(*, count=1, claims=None):
    if claims is None:
        claims = [claim()]
    return json.dumps(
        {"results": [{"comment_index": i, "claims": claims} for i in range(count)]}
    )


class FakeUsage:
    input_tokens = 100
    output_tokens = 50


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, text="", stop_reason="end_turn"):
        self.content = [FakeBlock(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.usage = FakeUsage()


class FakeClient:
    """Returns queued responses and records the kwargs it was called with."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def seeded_entries(conn, n=2):
    ingest(conn, [make_comment(i, body=f"{BODY} number {i}") for i in range(n)])
    return export_template(conn)


# --- the prompt -------------------------------------------------------------


def test_taxonomy_is_generated_from_the_models_module():
    """A hand-typed taxonomy drifts from the validator that enforces it."""
    prompt = build_prompt()

    assert "DUPE_OF" in prompt and "UNMET_PRODUCT_REQUEST" in prompt
    assert "LONGEVITY (no object)" in prompt
    assert "[builds the graph]" in prompt


def test_pronoun_policy_is_inlined_and_switchable():
    """Two of three sampled live comments had a pronoun subject."""
    assert PRONOUN_POLICIES["skip"] in build_prompt("skip")
    assert PRONOUN_POLICIES["literal"] in build_prompt("literal")
    assert build_prompt("skip") != build_prompt("literal")


def test_prompt_does_not_ask_for_extractor_only_fields():
    """Confidence and evidence are model outputs, not facts about a comment."""
    prompt = build_prompt()
    assert "evidence_span" not in prompt
    assert "confidence" not in prompt.split("Do not include")[0]


def test_schema_carries_no_unsupported_constraints():
    """Structured outputs reject these; the extractor learned it the hard way."""
    banned = {"minLength", "maxLength", "minimum", "maximum", "pattern", "multipleOf"}

    def walk(node):
        if isinstance(node, dict):
            assert not (banned & node.keys()), f"unsupported key in {node}"
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(RESPONSE_SCHEMA)


# --- the API call -----------------------------------------------------------


def test_no_sampling_parameters_are_sent(conn):
    """temperature/top_p/top_k are rejected with a 400 on Claude Opus 5."""
    client = FakeClient([FakeResponse(response_json(count=2))])
    draft(client, seeded_entries(conn), batch_size=10)

    sent = client.calls[0]
    assert not ({"temperature", "top_p", "top_k"} & sent.keys())
    assert sent["model"] == MODEL


def test_drafting_model_differs_from_the_extractor(conn):
    """A labeller at the extractor's capability measures self-consistency."""
    from fragrance_graph.extract.llm import MODEL as EXTRACTOR_MODEL

    assert MODEL != EXTRACTOR_MODEL


def test_refusal_is_reported_not_indexed(conn):
    """A refusal is a 200 with empty content; content[0] would IndexError."""
    client = FakeClient([FakeResponse("", stop_reason="refusal")])
    drafted, stats = draft(client, seeded_entries(conn), batch_size=10)

    assert stats.failed_batches == 1
    assert all(e["claims"] == [] for e in drafted)


def test_truncation_is_reported_with_actionable_advice(conn, caplog):
    import logging

    client = FakeClient([FakeResponse("{}", stop_reason="max_tokens")])
    with caplog.at_level(logging.ERROR):
        draft(client, seeded_entries(conn), batch_size=10)

    assert "batch-size" in "\n".join(r.getMessage() for r in caplog.records)


def test_a_failed_batch_does_not_masquerade_as_no_claims(conn):
    """An empty claims list is a real label; a failure must be countable."""
    client = FakeClient([ValueError("transport blew up")])
    drafted, stats = draft(client, seeded_entries(conn), batch_size=10)

    assert stats.failed_batches == 1
    assert stats.comments == 0, "failed comments must not count as drafted"
    assert all(e["claims"] == [] for e in drafted)


def test_cost_is_reported(conn):
    client = FakeClient([FakeResponse(response_json(count=2))])
    _, stats = draft(client, seeded_entries(conn), batch_size=10)

    assert stats.input_tokens == 100 and stats.output_tokens == 50
    assert stats.cost_usd > 0
    assert "$" in str(stats)


# --- parsing ----------------------------------------------------------------


def test_parses_claims_by_index():
    parsed = parse_batch(response_json(count=2), batch_size=2)
    assert list(parsed) == [0, 1]
    assert parsed[0][0]["claim_type"] == "DUPE_OF"


@pytest.mark.parametrize("index", [-1, 5, 99, "zero", None])
def test_out_of_range_index_is_skipped(index):
    text = json.dumps({"results": [{"comment_index": index, "claims": [claim()]}]})
    assert parse_batch(text, batch_size=2) == {}


def test_empty_claims_list_is_preserved():
    """Most comments assert nothing — that is the answer, not a failure."""
    assert parse_batch(response_json(claims=[]), batch_size=1)[0] == []


# --- provenance -------------------------------------------------------------


def test_drafts_are_stamped_with_their_origin(conn):
    client = FakeClient([FakeResponse(response_json(count=2))])
    drafted, _ = draft(client, seeded_entries(conn), batch_size=10)

    assert all(e["drafted_by"] == MODEL for e in drafted)
    assert all(e["pronoun_policy"] == "skip" for e in drafted)


def test_drafts_and_human_labels_coexist_without_overwriting(conn):
    """eval_labels is keyed on (comment_id, labeler) — both must survive."""
    entries = seeded_entries(conn, n=1)
    client = FakeClient([FakeResponse(response_json(count=1))])
    drafted, _ = draft(client, entries, batch_size=10)

    import_labels(conn, drafted, labeler=DRAFT_LABELER)
    human = [dict(entries[0], claims=[claim(claim_type="SIMILAR_TO")])]
    import_labels(conn, human, labeler="aanya")

    stored = {
        row["labeler"]: row["labeled_json"]
        for row in conn.execute("SELECT labeler, labeled_json FROM eval_labels")
    }
    assert set(stored) == {DRAFT_LABELER, "aanya"}
    assert "DUPE_OF" in stored[DRAFT_LABELER]
    assert "SIMILAR_TO" in stored["aanya"]


def test_draft_labeler_is_not_a_person(conn):
    assert "draft" in DRAFT_LABELER


# --- calibration ------------------------------------------------------------


def test_blind_set_strips_the_drafted_claims(conn):
    """The reviewer must form a judgement, not ratify one."""
    client = FakeClient([FakeResponse(response_json(count=2))])
    drafted, _ = draft(client, seeded_entries(conn), batch_size=10)

    blind = calibration_subset(drafted, n=2)

    assert all(e["claims"] == [] for e in blind)
    assert not any("drafted_by" in e for e in blind)
    assert "DUPE_OF" not in json.dumps(blind)


def test_blind_set_is_a_subset_of_what_was_drafted(conn):
    client = FakeClient([FakeResponse(response_json(count=4))])
    drafted, _ = draft(client, seeded_entries(conn, n=4), batch_size=10)

    blind = calibration_subset(drafted, n=2)
    drafted_ids = {e["source_id"] for e in drafted}

    assert len(blind) == 2
    assert {e["source_id"] for e in blind} <= drafted_ids


def test_blind_set_is_deterministic(conn):
    client = FakeClient([FakeResponse(response_json(count=4))])
    drafted, _ = draft(client, seeded_entries(conn, n=4), batch_size=10)

    first = [e["source_id"] for e in calibration_subset(drafted, n=2)]
    second = [e["source_id"] for e in calibration_subset(drafted, n=2)]
    assert first == second


def test_blind_set_keeps_the_body_and_key(conn):
    client = FakeClient([FakeResponse(response_json(count=2))])
    drafted, _ = draft(client, seeded_entries(conn), batch_size=10)

    entry = calibration_subset(drafted, n=1)[0]
    assert entry["body"] and entry["source_id"] and entry["source"]


# --- agreement --------------------------------------------------------------


def test_perfect_agreement_scores_one(conn):
    entries = seeded_entries(conn, n=1)
    labelled = [dict(entries[0], claims=[claim()])]
    import_labels(conn, labelled, labeler=DRAFT_LABELER)
    import_labels(conn, labelled, labeler="aanya")

    report = agreement(conn, human="aanya")
    assert report.overall.f1 == 1.0


def test_disagreement_is_visible(conn):
    entries = seeded_entries(conn, n=1)
    import_labels(
        conn,
        [dict(entries[0], claims=[claim(claim_type="SIMILAR_TO")])],
        labeler=DRAFT_LABELER,
    )
    import_labels(conn, [dict(entries[0], claims=[claim()])], labeler="aanya")

    report = agreement(conn, human="aanya")
    assert report.overall.f1 == 0.0
    assert report.overall.false_positives == 1
    assert report.overall.false_negatives == 1


def test_agreement_uses_only_comments_both_labelled(conn):
    """The human labels a subset; unlabelled comments are unknown, not empty."""
    entries = seeded_entries(conn, n=3)
    import_labels(conn, [dict(e, claims=[claim()]) for e in entries],
                  labeler=DRAFT_LABELER)
    import_labels(conn, [dict(entries[0], claims=[claim()])], labeler="aanya")

    report = agreement(conn, human="aanya")
    assert report.labelled_comments == 1
    assert report.overall.false_positives == 0


def test_agreement_without_overlap_refuses_to_guess(conn):
    entries = seeded_entries(conn, n=2)
    import_labels(conn, [dict(entries[0], claims=[])], labeler=DRAFT_LABELER)
    import_labels(conn, [dict(entries[1], claims=[])], labeler="someone-else")

    with pytest.raises(SystemExit, match="both"):
        agreement(conn, human="nobody")


def test_unknown_pronoun_policy_raises(conn):
    """Inlining the key would produce a prompt reading '- skip' and nobody
    downstream would notice the rule had gone missing."""
    with pytest.raises(ValueError, match="Unknown pronoun policy"):
        build_prompt("whatever")


def test_agreement_refuses_an_unlabelled_human_side(conn):
    """The blind template imports with empty claims; comparing against it
    yields a scoreboard of pure false positives that reads as a real score.
    """
    entries = seeded_entries(conn, n=3)
    import_labels(conn, [dict(e, claims=[claim()]) for e in entries],
                  labeler=DRAFT_LABELER)
    import_labels(conn, [dict(e, claims=[]) for e in entries], labeler="aanya")

    with pytest.raises(SystemExit, match="nothing to compare"):
        agreement(conn, human="aanya")


def test_agreement_allows_a_human_who_labelled_some_comments_empty(conn):
    """Most comments assert nothing — only an all-empty side is refused."""
    entries = seeded_entries(conn, n=3)
    import_labels(conn, [dict(e, claims=[claim()]) for e in entries],
                  labeler=DRAFT_LABELER)
    human = [dict(entries[0], claims=[claim()])] + [
        dict(e, claims=[]) for e in entries[1:]
    ]
    import_labels(conn, human, labeler="aanya")

    report = agreement(conn, human="aanya")
    assert report.overall.true_positives == 1
    assert report.overall.false_positives == 2


# --- diagnosing the disagreements -------------------------------------------


def test_disagreements_name_the_claims_not_just_the_count(conn):
    """An F1 is a verdict; acting on it needs the specific claims."""
    from fragrance_graph.evals.autolabel import disagreements

    entries = seeded_entries(conn, n=2)
    import_labels(conn, [dict(e, claims=[claim()]) for e in entries],
                  labeler=DRAFT_LABELER)
    human = [
        dict(entries[0], claims=[claim(claim_type="SIMILAR_TO")]),
        dict(entries[1], claims=[claim()]),
    ]
    import_labels(conn, human, labeler="aanya")

    rows = disagreements(conn, human="aanya")

    assert len(rows) == 1, "the agreeing comment must not be listed"
    assert rows[0]["only_yours"][0]["claim_type"] == "SIMILAR_TO"
    assert rows[0]["only_drafted"][0]["claim_type"] == "DUPE_OF"
    assert rows[0]["body"]


def test_disagreements_are_empty_when_both_sides_match(conn):
    from fragrance_graph.evals.autolabel import disagreements

    entries = seeded_entries(conn, n=2)
    labelled = [dict(e, claims=[claim()]) for e in entries]
    import_labels(conn, labelled, labeler=DRAFT_LABELER)
    import_labels(conn, labelled, labeler="aanya")

    assert disagreements(conn, human="aanya") == []


def test_render_claim_shows_objectless_types_without_an_arrow():
    from fragrance_graph.evals.autolabel import render_claim

    rendered = render_claim(
        {"claim_type": "LONGEVITY", "raw_subject_text": "Khamrah",
         "raw_object_text": None, "sentiment": "NEGATIVE"}
    )
    assert "->" not in rendered
    assert "LONGEVITY: Khamrah" in rendered and "NEGATIVE" in rendered
