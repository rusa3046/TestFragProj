"""Extraction parsing, malformed-output handling, and write behaviour.

No live API calls: `extract` takes an injected client, so a fake one drives
the whole run loop.
"""

import json

import pytest

from fragrance_graph.extract.llm import (
    BatchParseError,
    CostTracker,
    call_model,
    extract,
    iter_batches,
    parse_response,
    pending_comments,
    render_batch,
    write_claims,
)
from fragrance_graph.ingest.reddit import ingest
from fragrance_graph.models import ClaimType, ObjectKind, Sentiment, SubjectKind
from tests.conftest import make_comment

BODY = "Delina smells just like Baccarat 540 honestly, great for weddings"


def claim_json(**overrides):
    base = {
        "claim_type": "DUPE_OF",
        "subject_kind": "FRAGRANCE",
        "raw_subject_text": "Delina",
        "object_kind": "FRAGRANCE",
        "raw_object_text": "Baccarat 540",
        "sentiment": "NEUTRAL",
        "confidence": 0.9,
        "evidence_span": "smells just like Baccarat 540",
    }
    return {**base, **overrides}


def response_json(*, index=0, claims=None):
    # `claims or [...]` would treat an intentional empty list as "use default".
    if claims is None:
        claims = [claim_json()]
    return json.dumps({"results": [{"comment_index": index, "claims": claims}]})


# --- parsing ---------------------------------------------------------------


def test_parses_well_formed_response():
    parsed = parse_response(response_json(), batch_size=1)
    assert list(parsed) == [0]
    assert parsed[0][0].claim_type is ClaimType.DUPE_OF


def test_empty_claims_list_is_a_valid_result():
    """Most comments assert nothing. That is a result, not a failure."""
    parsed = parse_response(response_json(claims=[]), batch_size=1)
    assert parsed[0] == []


@pytest.mark.parametrize(
    "text",
    [
        "not json at all",
        "{{{",
        '{"wrong_key": []}',
        '{"results": "not a list"}',
        "",
    ],
)
def test_unusable_responses_raise_batch_parse_error(text):
    with pytest.raises(BatchParseError):
        parse_response(text, batch_size=1)


def test_truncated_json_raises_rather_than_half_parsing():
    truncated = response_json()[:-15]
    with pytest.raises(BatchParseError):
        parse_response(truncated, batch_size=1)


def test_one_invalid_claim_does_not_discard_the_others():
    """A single bad claim must not cost us the rest of the batch."""
    text = json.dumps(
        {
            "results": [
                {
                    "comment_index": 0,
                    "claims": [
                        claim_json(),
                        claim_json(confidence=42),  # out of range
                        claim_json(claim_type="SIMILAR_TO"),
                    ],
                }
            ]
        }
    )
    parsed = parse_response(text, batch_size=1)
    assert len(parsed[0]) == 2


def test_claim_violating_object_invariant_is_dropped():
    """Schema can't express 'LONGEVITY takes no object'; the validator can."""
    text = json.dumps(
        {
            "results": [
                {
                    "comment_index": 0,
                    "claims": [
                        claim_json(
                            claim_type="LONGEVITY",
                            object_kind="TAG",
                            raw_object_text="should not be here",
                        )
                    ],
                }
            ]
        }
    )
    assert parse_response(text, batch_size=1)[0] == []


def test_disallowed_object_kind_for_claim_type_is_dropped():
    """DUPE_OF must not accept a house, even though SIMILAR_TO does."""
    text = json.dumps(
        {
            "results": [
                {
                    "comment_index": 0,
                    "claims": [
                        claim_json(object_kind="HOUSE", raw_object_text="Serge Lutens")
                    ],
                }
            ]
        }
    )
    assert parse_response(text, batch_size=1)[0] == []


@pytest.mark.parametrize("index", [-1, 5, 99, "zero", None])
def test_out_of_range_comment_index_is_skipped(index):
    text = json.dumps({"results": [{"comment_index": index, "claims": [claim_json()]}]})
    assert parse_response(text, batch_size=3) == {}


def test_non_object_entries_are_skipped():
    text = json.dumps({"results": ["garbage", {"comment_index": 0, "claims": []}]})
    assert list(parse_response(text, batch_size=1)) == [0]


# --- API-call error surfaces ----------------------------------------------


class FakeUsage:
    def __init__(self, i=100, o=50):
        self.input_tokens = i
        self.output_tokens = o


class FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class FakeResponse:
    def __init__(self, text="", stop_reason="end_turn", usage=None):
        self.content = [FakeBlock(text)] if text is not None else []
        self.stop_reason = stop_reason
        self.usage = usage or FakeUsage()


class FakeClient:
    """Returns queued responses; records how many calls were made."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_refusal_is_reported_as_a_batch_error():
    client = FakeClient([FakeResponse("", stop_reason="refusal")])
    with pytest.raises(BatchParseError, match="refused"):
        call_model(client, [{"body": "x"}])


def test_truncation_is_reported_with_actionable_advice():
    client = FakeClient([FakeResponse("{}", stop_reason="max_tokens")])
    with pytest.raises(BatchParseError, match="batch-size"):
        call_model(client, [{"body": "x"}])


def test_response_with_no_text_block_is_a_batch_error():
    client = FakeClient([FakeResponse(None)])
    with pytest.raises(BatchParseError, match="no text block"):
        call_model(client, [{"body": "x"}])


# --- writing ---------------------------------------------------------------


def seed(conn, n=1, body=BODY):
    ingest(conn, [make_comment(i, body=body) for i in range(n)])
    return [r["id"] for r in conn.execute("SELECT id FROM comments ORDER BY id")]


def test_write_marks_extracted_and_stores_object_kind(conn):
    (comment_id,) = seed(conn)
    claims = parse_response(response_json(), batch_size=1)[0]
    write_claims(conn, comment_id, BODY, claims)

    row = conn.execute("SELECT * FROM claims").fetchone()
    assert row["subject_kind"] == SubjectKind.FRAGRANCE.value
    assert row["object_kind"] == ObjectKind.FRAGRANCE.value
    assert row["sentiment"] == Sentiment.NEUTRAL.value
    assert row["evidence_verified"] == 1
    assert row["extraction_model"]

    extracted = conn.execute(
        "SELECT extracted_at FROM comments WHERE id = ?", (comment_id,)
    ).fetchone()[0]
    assert extracted is not None


def test_zero_claim_comment_is_still_marked_extracted(conn):
    """Otherwise we pay to re-ask the same question forever."""
    (comment_id,) = seed(conn)
    write_claims(conn, comment_id, BODY, [])

    assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    assert not pending_comments(conn, 10)


def test_paraphrased_evidence_is_stored_but_flagged(conn):
    (comment_id,) = seed(conn)
    claims = parse_response(
        response_json(claims=[claim_json(evidence_span="the user thinks it is a dupe")]),
        batch_size=1,
    )[0]
    written, unverified = write_claims(conn, comment_id, BODY, claims)

    assert (written, unverified) == (1, 1)
    assert conn.execute("SELECT evidence_verified FROM claims").fetchone()[0] == 0


# --- run loop --------------------------------------------------------------


def test_extract_processes_and_marks_all_comments(conn):
    seed(conn, n=3)
    client = FakeClient([FakeResponse(response_json(index=i)) for i in range(1)])
    cost = extract(conn, client, limit=10, batch_size=3)

    assert cost.batches == 1
    assert not pending_comments(conn, 10)


def test_failed_batch_leaves_comments_pending_for_retry(conn):
    seed(conn, n=2)
    client = FakeClient([FakeResponse("total garbage")])
    cost = extract(conn, client, limit=10, batch_size=2)

    assert cost.failed_batches == 1
    assert len(pending_comments(conn, 10)) == 2, "must be retried, not lost"


def test_one_failed_batch_does_not_stop_the_run(conn):
    """The spec's core requirement: malformed output can't kill the batch run."""
    seed(conn, n=4)
    client = FakeClient([FakeResponse("garbage"), FakeResponse(response_json())])
    cost = extract(conn, client, limit=10, batch_size=2)

    assert client.calls == 2, "second batch must still be attempted"
    assert cost.failed_batches == 1
    assert len(pending_comments(conn, 10)) == 2, "only the failed batch retries"


def test_transport_exception_is_caught_and_batch_retried(conn):
    seed(conn, n=2)
    client = FakeClient([RuntimeError("connection reset")])
    cost = extract(conn, client, limit=10, batch_size=2)

    assert cost.failed_batches == 1
    assert len(pending_comments(conn, 10)) == 2


def test_already_extracted_comments_are_never_re_sent(conn):
    seed(conn, n=2)
    client = FakeClient([FakeResponse(response_json())])
    extract(conn, client, limit=10, batch_size=2)

    second = FakeClient([FakeResponse(response_json())])
    cost = extract(conn, second, limit=10, batch_size=2)

    assert second.calls == 0, "no pending comments means no API spend"
    assert cost.comments == 0


# --- cost accounting -------------------------------------------------------


def test_cost_per_1k_uses_haiku_pricing():
    cost = CostTracker()
    cost.record(input_tokens=1_000_000, output_tokens=1_000_000, comments=1000)
    assert cost.cost_usd == pytest.approx(6.00)  # $1 in + $5 out
    assert cost.cost_per_1k_comments == pytest.approx(6.00)


def test_cost_per_1k_scales_from_a_partial_run():
    cost = CostTracker()
    cost.record(input_tokens=140_000, output_tokens=100_000, comments=1000)
    assert cost.cost_per_1k_comments == pytest.approx(0.64)


def test_cost_per_1k_is_zero_before_any_comments():
    assert CostTracker().cost_per_1k_comments == 0.0


def test_summary_reports_cost_per_1k(conn):
    seed(conn, n=2)
    client = FakeClient([FakeResponse(response_json())])
    cost = extract(conn, client, limit=10, batch_size=2)
    assert "per 1k comments" in cost.summary()


# --- batching --------------------------------------------------------------


def test_iter_batches_covers_every_row_exactly_once():
    rows = list(range(7))
    batches = list(iter_batches(rows, 3))
    assert [len(b) for b in batches] == [3, 3, 1]
    assert [r for b in batches for r in b] == rows


def test_render_batch_numbers_comments_from_zero():
    rendered = render_batch([{"body": "first"}, {"body": "second"}])
    assert rendered.startswith("[0] first")
    assert "[1] second" in rendered


# --- determinism ------------------------------------------------------------


class RecordingClient(FakeClient):
    """Captures the kwargs of each request."""

    def __init__(self, responses):
        super().__init__(responses)
        self.kwargs = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        return super().create(**kwargs)


def test_requests_pin_temperature_for_reproducibility():
    """Identical input returned 4 claims then 8 at default temperature."""
    from fragrance_graph.extract.llm import TEMPERATURE

    client = RecordingClient([FakeResponse(response_json())])
    call_model(client, [{"body": "x"}])

    assert client.kwargs[0]["temperature"] == TEMPERATURE == 0.0


def test_schema_uses_no_unsupported_constraints():
    """Structured outputs reject minLength/minimum/maximum.

    Adding them made every batch fail with an empty result rather than an
    obvious error, so this is a regression guard, not a style check.
    """
    from fragrance_graph.extract.llm import RESPONSE_SCHEMA

    banned = {"minLength", "maxLength", "minimum", "maximum", "multipleOf", "pattern"}
    found = set()

    def walk(node):
        if isinstance(node, dict):
            found.update(banned & node.keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(RESPONSE_SCHEMA)
    assert not found, f"structured outputs reject these: {sorted(found)}"


def test_total_failure_is_reported_as_an_error_not_a_result(conn, caplog):
    """'0 claims written' must not read like a successful empty extraction."""
    import logging

    seed(conn, n=2)
    client = FakeClient([FakeResponse("garbage")])
    with caplog.at_level(logging.ERROR):
        extract(conn, client, limit=10, batch_size=2)

    assert any("ALL 1 batches failed" in r.message for r in caplog.records)


def test_sentiment_round_trips_to_the_database(conn):
    """Polarity must survive the write; v1 had nowhere to put it."""
    (comment_id,) = seed(conn)
    claims = parse_response(
        response_json(
            claims=[
                claim_json(
                    claim_type="LONGEVITY",
                    object_kind="NONE",
                    raw_object_text=None,
                    sentiment="POSITIVE",
                )
            ]
        ),
        batch_size=1,
    )[0]
    write_claims(conn, comment_id, BODY, claims)

    assert conn.execute("SELECT sentiment FROM claims").fetchone()[0] == "POSITIVE"


def test_unverified_evidence_log_is_not_truncated(conn, caplog):
    """A truncated diagnostic made a real paraphrase investigation impossible."""
    import logging

    long_span = "x" * 200
    (comment_id,) = seed(conn, body="unrelated body text")
    claims = parse_response(
        response_json(claims=[claim_json(evidence_span=long_span)]), batch_size=1
    )[0]

    with caplog.at_level(logging.WARNING):
        write_claims(conn, comment_id, "unrelated body text", claims)

    logged = "\n".join(r.message % r.args for r in caplog.records)
    assert long_span in logged, "full span needed to diagnose the mismatch"
    assert "unrelated body text" in logged, "body needed for comparison"
