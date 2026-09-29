from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from codex_grokbot_mcp.deliver import (
    PacketError,
    build_worker_diagnostic_packet,
    validate_worker_diagnostic_result,
)

JOB_ID = "1234abcd-1234-4123-8123-123456789abc"
TARGET_JOB_ID = "2345abcd-1234-4123-8123-123456789abc"


def test_diagnostic_packet_has_only_callback_authority() -> None:
    packet = build_worker_diagnostic_packet(
        job_id=JOB_ID,
        target_job_id=TARGET_JOB_ID,
        target_bot="devcoder",
        origin="https://inbox.example.invalid",
        callback_token="c" * 43,
    )

    assert packet == {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "goal": (
            f"Message devcoder and ask whether coding job {TARGET_JOB_ID} is still working, "
            "stopped, or finished. Do not start, stop, retry, or change any job, files, or "
            "GitHub PRs. Never forward callback credentials."
        ),
        "constraints": {
            "read_only": True,
            "no_job_changes": True,
            "no_credentials": True,
            "no_secrets": True,
        },
        "context": {
            "callback_url": f"https://inbox.example.invalid/jobs/{JOB_ID}/result",
            "callback_token": "c" * 43,
        },
        "deliver": "callback",
        "max_wait_seconds": 120,
    }


@pytest.mark.parametrize(
    ("job_id", "target_job_id", "target_bot"),
    [
        ("not-a-uuid", TARGET_JOB_ID, "devcoder"),
        (JOB_ID, "not-a-uuid", "devcoder"),
        (JOB_ID, TARGET_JOB_ID, "unknown"),
    ],
)
def test_diagnostic_packet_rejects_ambiguous_identity(job_id, target_job_id, target_bot) -> None:
    with pytest.raises(PacketError):
        build_worker_diagnostic_packet(
            job_id=job_id,
            target_job_id=target_job_id,
            target_bot=target_bot,
            origin="https://inbox.example.invalid",
            callback_token="c" * 43,
        )


def test_diagnostic_result_accepts_exact_advisory_response() -> None:
    now = datetime.now(UTC)
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "replied",
        "reply": "Finished; PR 139 is open.",
        "completed_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "read_only_attestation": True,
    }

    assert (
        validate_worker_diagnostic_result(JOB_ID, TARGET_JOB_ID, "devcoder", body, now=now) == body
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(job_id=TARGET_JOB_ID),
        lambda body: body.update(target_job_id=JOB_ID),
        lambda body: body.update(status="finished"),
        lambda body: body.update(reply="token: secret-value"),
        lambda body: body.update(reply="crsr_" + "x" * 24),
        lambda body: body.update(read_only_attestation=False),
        lambda body: body.update({"callback_token": "leak"}),
    ],
)
def test_diagnostic_result_rejects_identity_or_secret_mismatch(mutate) -> None:
    now = datetime.now(UTC)
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "replied",
        "reply": "Finished.",
        "completed_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "read_only_attestation": True,
    }
    mutate(body)

    with pytest.raises(PacketError):
        validate_worker_diagnostic_result(JOB_ID, TARGET_JOB_ID, "devcoder", body, now=now)


def test_diagnostic_no_reply_requires_null_reply() -> None:
    now = datetime.now(UTC)
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "no_reply",
        "reply": None,
        "completed_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "read_only_attestation": True,
    }

    assert (
        validate_worker_diagnostic_result(JOB_ID, TARGET_JOB_ID, "devcoder", body, now=now) == body
    )


def test_diagnostic_result_rejects_future_time_and_long_reply() -> None:
    now = datetime.now(UTC)
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "replied",
        "reply": "x" * 2001,
        "completed_at": (now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "read_only_attestation": True,
    }

    with pytest.raises(PacketError):
        validate_worker_diagnostic_result(JOB_ID, TARGET_JOB_ID, "devcoder", body, now=now)


def test_diagnostic_result_rejects_exact_callback_credential() -> None:
    now = datetime.now(UTC)
    credential = "synthetic-callback-token-value"
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "replied",
        "reply": f"The Bot echoed {credential}.",
        "completed_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "read_only_attestation": True,
    }

    with pytest.raises(PacketError, match="credential"):
        validate_worker_diagnostic_result(
            JOB_ID,
            TARGET_JOB_ID,
            "devcoder",
            body,
            now=now,
            forbidden_values=(credential,),
        )


@pytest.mark.parametrize("prefix", ["ghp_", "ghs_", "ghu_", "gho_", "ghr_", "github_pat_"])
def test_diagnostic_result_rejects_github_token_prefixes(prefix: str) -> None:
    now = datetime.now(UTC)
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": JOB_ID,
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "replied",
        "reply": f"The Bot reported a credential: {prefix}{'a' * 36}",
        "completed_at": (now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
        "read_only_attestation": True,
    }

    with pytest.raises(PacketError):
        validate_worker_diagnostic_result(JOB_ID, TARGET_JOB_ID, "devcoder", body, now=now)
