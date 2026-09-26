from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_grokbot_mcp.control import InstallationToken
from codex_grokbot_mcp.deliver import (
    PacketError,
    build_v3_coding_packet,
    build_x_query_packet,
    callback_url,
    interpret_status,
    require_callback_target,
    validate_status_body,
    validate_x_query_result,
)
from codex_grokbot_mcp.local import DelegationContext, SourceSnapshot

JOB_ID = "1234abcd-1234-4123-8123-123456789abc"
ORIGIN = "https://inbox.example.invalid"
TOKEN = "c" * 43


def context() -> DelegationContext:
    return DelegationContext(
        root=Path("/private/local/workspace"),
        target_repo="example/target",
        head="a" * 40,
        branch="feature/example",
        read_paths=("src/a.py",),
        write_paths=("src/a.py",),
        source_snapshots=(SourceSnapshot("src/a.py", "b" * 64, "selected source\n"),),
        write_hashes=(("src/a.py", "b" * 64),),
        snapshot_digest="c" * 64,
    )


def coding_packet(**overrides):
    options = {
        "job_id": JOB_ID,
        "goal": "Add a bounded regression test",
        "context": context(),
        "control_repo": "example/private-control",
        "control_branch": "grokbot/job-coding-1234abcd",
        "artifact_path": "artifacts/patch-1234abcd.json",
        "token": InstallationToken("scoped-worker-token", datetime.now(UTC) + timedelta(hours=1)),
        "acceptance_checks": ["The focused suite passes"],
        "effort_hint": "small",
    }
    options.update(overrides)
    return build_v3_coding_packet(**options)


def ok_body(**overrides) -> dict:
    body = {
        "schema_version": "v3",
        "job_type": "x_query",
        "job_id": JOB_ID,
        "query": "What is being discussed?",
        "answer": {"top_themes": [{"theme": "Example"}]},
        "summary": "One line.",
        "sources": [],
        "read_only_attestation": True,
        "completed_at": "2026-09-26T00:00:00Z",
        "status": "ok",
        "error": None,
    }
    body.update(overrides)
    return body


def test_x_query_defaults_to_callback_without_github_fields() -> None:
    packet = build_x_query_packet(
        job_id=JOB_ID,
        goal="What is being discussed?",
        origin=ORIGIN,
        callback_token=TOKEN,
    )
    assert packet["deliver"] == "callback"
    assert packet["schema_version"] == "v3"
    assert "github_token" not in packet["context"]
    assert "control_repo" not in packet["context"]
    assert packet["context"]["callback_url"] == callback_url(ORIGIN, JOB_ID, "result")


@pytest.mark.parametrize(
    "forbidden",
    [
        {"control_repo": "example/private-control"},
        {"github_token": TOKEN},
        {"head_ref": "grokbot/job-coding-1234abcd"},
        {"answer_path": "answers/answer-1234abcd.json"},
        {"deliver": "github_pr"},
    ],
)
def test_x_query_rejects_control_repo_authority(forbidden) -> None:
    with pytest.raises(PacketError):
        build_x_query_packet(
            job_id=JOB_ID,
            goal="What is being discussed?",
            origin=ORIGIN,
            callback_token=TOKEN,
            **forbidden,
        )


def test_coding_defaults_to_github_pr_and_rejects_callback() -> None:
    packet = coding_packet()
    assert packet["deliver"] == "github_pr"
    assert packet["schema_version"] == "v3"
    assert packet["context"]["github_token"] == "scoped-worker-token"  # noqa: S105
    with pytest.raises(PacketError):
        coding_packet(deliver="callback")


@pytest.mark.parametrize(
    "origin",
    [
        "http://inbox.example.invalid",
        "https://user@inbox.example.invalid",
        "https://inbox.example.invalid/extra",
        "https://inbox.example.invalid/?q=1",
        "https://192.0.2.10",
    ],
)
def test_callback_url_rejects_unlisted_targets(origin: str) -> None:
    with pytest.raises(PacketError):
        build_x_query_packet(
            job_id=JOB_ID,
            goal="What is being discussed?",
            origin=origin,
            callback_token=TOKEN,
        )


def test_foreign_host_or_job_id_does_not_match_the_configured_callback() -> None:
    expected = callback_url(ORIGIN, JOB_ID, "result")
    foreign = expected.replace("inbox.example.invalid", "other.example.invalid")
    other_job = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    with pytest.raises(PacketError):
        require_callback_target(foreign, origin=ORIGIN, job_id=JOB_ID, kind="result")
    with pytest.raises(PacketError):
        require_callback_target(
            callback_url(ORIGIN, other_job, "result"),
            origin=ORIGIN,
            job_id=JOB_ID,
            kind="result",
        )


def test_ok_and_error_answers_follow_the_strict_shapes() -> None:
    now = datetime(2026, 9, 26, 1, tzinfo=UTC)
    assert validate_x_query_result(JOB_ID, ok_body(), now=now)["status"] == "ok"
    error = ok_body(
        answer=None,
        sources=[],
        status="error",
        error={"code": "source_unavailable", "message": "The read failed."},
    )
    assert validate_x_query_result(JOB_ID, error, now=now)["answer"] is None
    with pytest.raises(PacketError):
        validate_x_query_result(
            JOB_ID,
            ok_body(status="error", answer={"top_themes": [{"theme": "Invented"}]}),
            now=now,
        )


@pytest.mark.parametrize(
    "change",
    [
        {"read_only_attestation": False},
        {"job_id": "ffffffff-ffff-4fff-8fff-ffffffffffff"},
        {"summary": "token=abc"},
        {"answer": "I posted an update"},
        {"completed_at": "2026-09-27T00:00:00Z"},
        {"query": "x" * 2001},
    ],
)
def test_unsafe_answers_fail_closed(change) -> None:
    now = datetime(2026, 9, 26, 1, tzinfo=UTC)
    with pytest.raises(PacketError):
        validate_x_query_result(JOB_ID, ok_body(**change), now=now)


def test_status_ping_rejects_patches_and_foreign_pull_requests() -> None:
    body = {
        "schema_version": "v3",
        "job_type": "coding",
        "job_id": JOB_ID,
        "status": "blocked",
        "pr_url": None,
        "summary": "The file set cannot express the change.",
        "completed_at": "2026-09-26T00:00:00Z",
    }
    assert validate_status_body(JOB_ID, body, control_repo="example/private-control")
    with pytest.raises(PacketError):
        validate_status_body(
            JOB_ID, {**body, "patch": "diff"}, control_repo="example/private-control"
        )
    with pytest.raises(PacketError):
        validate_status_body(
            JOB_ID,
            {**body, "status": "ready", "pr_url": "https://github.com/other/repo/pull/4"},
            control_repo="example/private-control",
        )


def test_ready_ping_does_not_accept_code_and_artifact_wins() -> None:
    ready = {"status": "ready"}
    blocked = {"status": "blocked"}
    assert interpret_status(ready, artifact_present=False) == "poll"
    assert interpret_status(blocked, artifact_present=False) == "blocked"
    assert interpret_status(blocked, artifact_present=True) == "poll"
