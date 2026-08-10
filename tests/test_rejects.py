"""Inspecting claims that failed validation.

These rejections are the largest measured defect in the pipeline — 21-24%
of everything the extractor emits — and until they were persisted the only
way to look at one was to pay to extract again.
"""

import json

from fragrance_graph.extract.llm import Rejection, write_rejections
from fragrance_graph.extract.rejects import main, rejected, render, report
from fragrance_graph.ingest.reddit import ingest
from tests.conftest import make_comment

DESCRIPTOR_REASON = (
    "Value error, NOTE_DESCRIPTOR allows object_kind {TAG}, got NONE"
)
DUPE_REASON = "Value error, DUPE_OF allows object_kind {FRAGRANCE}, got TAG"


def seed_rejection(conn, *, reason, body="it smells incredible", claim=None):
    ingest(conn, [make_comment(abs(hash((reason, body))) % 90000, body=body)])
    comment_id = conn.execute(
        "SELECT id FROM comments ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    write_rejections(
        conn,
        comment_id,
        [
            Rejection(
                0,
                reason,
                claim
                or {
                    "claim_type": "NOTE_DESCRIPTOR",
                    "raw_subject_text": "Khamrah",
                    "raw_object_text": None,
                    "object_kind": "NONE",
                },
            )
        ],
    )
    conn.commit()
    return comment_id


def test_report_groups_by_reason_most_frequent_first(conn):
    for _ in range(3):
        seed_rejection(conn, reason=DESCRIPTOR_REASON, body=f"body {_}")
    seed_rejection(conn, reason=DUPE_REASON, body="a dupe of barbershop scents")

    counts = report(conn)

    assert counts[0].reason == DESCRIPTOR_REASON
    assert counts[0].claims == 3
    assert counts[0].comments == 3
    assert counts[1].claims == 1


def test_report_counts_comments_separately_from_claims(conn):
    """One comment can produce several rejections; both numbers matter."""
    comment_id = seed_rejection(conn, reason=DESCRIPTOR_REASON)
    write_rejections(
        conn,
        comment_id,
        [Rejection(0, DESCRIPTOR_REASON, {"claim_type": "NOTE_DESCRIPTOR"})],
    )
    conn.commit()

    (count,) = report(conn)
    assert (count.claims, count.comments) == (2, 1)


def test_show_returns_the_comment_and_the_emitted_claim(conn):
    """The reason alone cannot say whether the model or taxonomy is wrong."""
    seed_rejection(conn, reason=DUPE_REASON, body="basically a dupe of old barbershop")

    (entry,) = rejected(conn)

    assert entry["reason"] == DUPE_REASON
    assert entry["body"] == "basically a dupe of old barbershop"
    assert entry["claim"]["claim_type"] == "NOTE_DESCRIPTOR"
    assert entry["permalink"]


def test_show_filters_by_reason_substring(conn):
    seed_rejection(conn, reason=DESCRIPTOR_REASON, body="one")
    seed_rejection(conn, reason=DUPE_REASON, body="two")

    assert len(rejected(conn, reason_like="NOTE_DESCRIPTOR")) == 1
    assert len(rejected(conn, reason_like="dupe_of")) == 1, "match is case-insensitive"
    assert len(rejected(conn)) == 2


def test_reason_filter_takes_a_substring_not_a_pattern(conn):
    """Reasons are validator prose full of braces; escaping them would be
    a worse tool than a slower one."""
    seed_rejection(conn, reason=DESCRIPTOR_REASON)

    assert len(rejected(conn, reason_like="{TAG}, got NONE")) == 1


def test_show_respects_the_limit(conn):
    for i in range(5):
        seed_rejection(conn, reason=DESCRIPTOR_REASON, body=f"body {i}")

    assert len(rejected(conn, limit=2)) == 2


def test_render_shows_what_was_emitted(conn):
    seed_rejection(conn, reason=DESCRIPTOR_REASON, body="soapy and clean")

    rendered = render(rejected(conn)[0])

    assert "soapy and clean" in rendered
    assert "NOTE_DESCRIPTOR" in rendered
    assert "object_kind='NONE'" in rendered


def test_payload_survives_verbatim(conn):
    """A Claim is what it failed to become; normalising it loses the shape."""
    odd = {
        "claim_type": "DUPE_OF",
        "raw_subject_text": "Sand+Fog",
        "raw_object_text": "old-school barbershop scents",
        "object_kind": "TAG",
        "confidence": 0.4,
    }
    seed_rejection(conn, reason=DUPE_REASON, claim=odd)

    assert rejected(conn)[0]["claim"] == odd


def test_report_on_an_empty_table_says_so(conn, tmp_path, capsys):
    from fragrance_graph.db import get_connection, migrate

    db = tmp_path / "empty.db"
    conn.close()
    fresh = get_connection(db)
    migrate(fresh)
    fresh.close()

    main(["report", "--db-path", str(db)])

    assert "No rejected claims recorded" in capsys.readouterr().out


def test_cli_report_prints_counts(conn, tmp_path, capsys):
    from fragrance_graph.db import get_connection, migrate

    db = tmp_path / "cli.db"
    conn.close()
    fresh = get_connection(db)
    migrate(fresh)
    ingest(fresh, [make_comment(1, body="smells nice")])
    comment_id = fresh.execute("SELECT id FROM comments").fetchone()[0]
    write_rejections(
        fresh, comment_id, [Rejection(0, DESCRIPTOR_REASON, {"claim_type": "X"})]
    )
    fresh.commit()
    fresh.close()

    main(["report", "--db-path", str(db)])

    out = capsys.readouterr().out
    assert "NOTE_DESCRIPTOR" in out
    assert "1 rejected claims across 1 distinct reasons" in out


def test_rejected_claims_are_json_not_a_python_repr(conn):
    """A curator has to be able to read and grep these."""
    seed_rejection(conn, reason=DESCRIPTOR_REASON)

    raw = conn.execute("SELECT raw_json FROM rejected_claims").fetchone()[0]
    assert json.loads(raw)["raw_subject_text"] == "Khamrah"
