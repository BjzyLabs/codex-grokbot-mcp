"""Build one bounded coding packet for the first-party Grok Bot webhook."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from uuid import UUID

from .control import (
    ARTIFACT_PATTERN,
    BRANCH_PATTERN,
    MAX_WEBHOOK_BYTES,
    REPO_PATTERN,
    TOKEN_MINIMUM,
    InstallationToken,
)
from .local import DelegationContext
from .vault import MAX_JOB_DURATION

SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
GIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
EFFORT_HINTS = {"small", "medium"}


class PacketError(ValueError):
    """A proposed worker packet is invalid or exceeds its bounded contract."""


def build_coding_packet(
    *,
    job_id: str,
    goal: str,
    context: DelegationContext,
    control_repo: str,
    control_branch: str,
    artifact_path: str,
    token: InstallationToken,
    acceptance_checks: list[str],
    effort_hint: str,
) -> dict:
    """Include only selected source snapshots and one scoped control-repo token."""
    try:
        canonical_job_id = str(UUID(job_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise PacketError("job ID must be a canonical UUID") from error
    if canonical_job_id != job_id:
        raise PacketError("job ID must be a canonical UUID")
    short = job_id[:8]
    if (
        not BRANCH_PATTERN.fullmatch(control_branch)
        or control_branch != f"grokbot/job-coding-{short}"
        or not ARTIFACT_PATTERN.fullmatch(artifact_path)
        or artifact_path != f"artifacts/patch-{short}.json"
    ):
        raise PacketError("artifact coordinates do not match the job")
    if (
        not REPO_PATTERN.fullmatch(control_repo)
        or control_repo == context.target_repo
        or not REPO_PATTERN.fullmatch(context.target_repo)
        or any(part in (".", "..") for part in control_repo.split("/"))
        or any(part in (".", "..") for part in context.target_repo.split("/"))
    ):
        raise PacketError("control and target repositories must be distinct GitHub repositories")
    if not GIT_SHA_PATTERN.fullmatch(context.head) or not SHA256_PATTERN.fullmatch(
        context.snapshot_digest
    ):
        raise PacketError("workspace snapshot identity is invalid")
    if (
        not context.read_paths
        or not context.write_paths
        or len(context.read_paths) != len(set(context.read_paths))
        or len(context.write_paths) != len(set(context.write_paths))
        or {item.path for item in context.source_snapshots} != set(context.read_paths)
        or len(context.source_snapshots) != len(context.read_paths)
    ):
        raise PacketError("selected snapshots do not match read paths")
    if not isinstance(goal, str) or not 0 < len(goal.strip()) <= 2000 or "\x00" in goal:
        raise PacketError("goal is empty or exceeds its bound")
    if (
        not isinstance(acceptance_checks, list)
        or not 1 <= len(acceptance_checks) <= 10
        or any(
            not isinstance(item, str) or not 0 < len(item.strip()) <= 300 or "\x00" in item
            for item in acceptance_checks
        )
    ):
        raise PacketError("acceptance checks are missing or exceed their bound")
    if effort_hint not in EFFORT_HINTS:
        raise PacketError("large jobs must be split into bounded tasks")
    if (
        not isinstance(token, InstallationToken)
        or not token.value
        or token.expires_at.tzinfo is None
        or token.expires_at.astimezone(UTC) - datetime.now(UTC) < TOKEN_MINIMUM
    ):
        raise PacketError("worker token is missing or expires too soon")

    packet = {
        "schema_version": "v2",
        "job_type": "coding",
        "job_id": job_id,
        "goal": goal.strip(),
        "constraints": {
            "no_secrets": True,
            "no_redelegation": True,
            "no_merge": True,
            "no_deploy": True,
            "no_workarounds": True,
        },
        "context": {
            "control_repo": control_repo,
            "head_ref": control_branch,
            "github_token": token.value,
            "target_repo": context.target_repo,
            "base_sha": context.head,
            "allowed_paths": list(context.write_paths),
            "patch_path": artifact_path,
            "workspace_head": context.head,
            "snapshot_digest": context.snapshot_digest,
            "read_paths": list(context.read_paths),
            "write_paths": list(context.write_paths),
            "source_snapshots": [item.public_record() for item in context.source_snapshots],
            "acceptance_checks": acceptance_checks,
            "effort_hint": effort_hint,
            "max_minutes": int(MAX_JOB_DURATION.total_seconds() // 60),
        },
        "instructions": [
            (
                "Use only the supplied source snapshots for target-source context; "
                "do not clone or modify the target repository."
            ),
            (
                "Produce a unified diff whose actual changed paths equal its declared "
                "changed paths and stay within write_paths."
            ),
            "Treat the effort hint as advisory; finish within 45 minutes or report a blocker.",
            (
                "If blocked, report the blocker. Do not substitute credentials, "
                "transports, hosts, models, or weaker checks."
            ),
            (
                f"Write one v2 coding patch artifact to '{artifact_path}' with "
                "schema_version, job_type, job_id, target_repo, base_sha, "
                "workspace_head, snapshot_digest, declared_changed_paths, "
                "summary, patch, and completed_at."
            ),
            (
                f"Commit the artifact on '{control_branch}' in the control "
                "repository and open a draft PR into main. Do not merge or deploy."
            ),
        ],
    }
    raw = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise PacketError("coding packet exceeds webhook size limit")
    return packet
