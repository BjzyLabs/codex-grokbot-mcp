"""Read-only reconciliation evidence for a recorded coding job."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from codex_grokbot_mcp.config import Config, ConfigError
from codex_grokbot_mcp.control import GitHubControl, GitHubHTTPError, InspectionPR
from codex_grokbot_mcp.jobs import JobRecord
from codex_grokbot_mcp.vault import VaultError, VaultLeaseStore


class InspectionError(ValueError):
    """Job inspection could not produce trustworthy GitHub evidence."""


@dataclass(frozen=True)
class ArtifactClassification:
    classification: str
    expected_schema: str
    observed_schema: str


def _classify_artifact(record: JobRecord, artifact: dict[str, Any]) -> ArtifactClassification:
    version = artifact.get("schema_version")
    observed = version if version in ("v1", "v2") else "unknown"
    expected = record.packet_schema
    if version != expected:
        classification = (
            "schema_v1_expected_v2"
            if version == "v1" and expected == "v2"
            else "schema_version_mismatch"
        )
    elif artifact.get("job_type") != "coding":
        classification = "job_type_mismatch"
    elif artifact.get("job_id") != record.job_id:
        classification = "job_id_mismatch"
    elif artifact.get("target_repo") != record.target_repo:
        classification = "target_repository_mismatch"
    elif artifact.get("base_sha") != record.head:
        classification = "base_revision_mismatch"
    else:
        classification = "identity_fields_match"
    return ArtifactClassification(classification, expected, observed)


def _pull_request_evidence(pr: InspectionPR) -> dict[str, object]:
    if pr.merged_at is not None:
        lifecycle = "merged"
    elif pr.state == "closed":
        lifecycle = "closed_unmerged"
    elif pr.draft and pr.base_ref == "main" and pr.base_repository == pr.head_repository:
        lifecycle = "open_draft_to_main"
    elif pr.draft:
        lifecycle = "open_draft_other_base"
    else:
        lifecycle = "open_ready_for_review"
    return {
        "number": pr.number,
        "url": f"https://github.com/{pr.head_repository}/pull/{pr.number}",
        "state": pr.state,
        "draft": pr.draft,
        "merged_at": pr.merged_at,
        "lifecycle": lifecycle,
        "head_ref": pr.head_ref,
        "head_repository": pr.head_repository,
        "head_sha": pr.head_sha,
        "base_ref": pr.base_ref,
        "base_repository": pr.base_repository,
    }


def _lease_evidence(config: Config, record: JobRecord) -> dict[str, object]:
    worker = config.workers.get(record.worker_id)
    if worker is None:
        return {
            "read_status": "unavailable",
            "state": "unavailable",
            "version": None,
            "owner_matches_job": None,
            "job_matches": None,
            "error_code": "worker_configuration_missing",
        }
    try:
        lease_store = VaultLeaseStore(config.vault_client(), worker.lease_prefix)
        lease_store.require_cas(worker.lease_worker)
        lease = lease_store.read(worker.lease_worker)
    except (ConfigError, VaultError):
        return {
            "read_status": "unavailable",
            "state": "unavailable",
            "version": None,
            "owner_matches_job": None,
            "job_matches": None,
            "error_code": "vault_read_failed",
        }
    if lease is None:
        return {
            "read_status": "read",
            "state": "missing",
            "version": None,
            "owner_matches_job": False,
            "job_matches": False,
        }
    if lease.state == "available":
        owner_matches = False
        job_matches = False
    else:
        owner_matches = lease.owner == record.lease_owner
        job_matches = lease.job_id == record.job_id
    return {
        "read_status": "read",
        "state": lease.state,
        "version": lease.version,
        "owner_matches_job": owner_matches,
        "job_matches": job_matches,
    }


def inspect_coding_job(config: Config, record: JobRecord | None, job_id: str) -> dict[str, object]:
    """Recompute bounded journal, Vault, and control-repository evidence without mutation."""
    if record is None:
        return {"job_id": job_id, "state": "not_found"}
    if record.job_type != "coding":
        raise InspectionError("job inspection supports recorded coding jobs only")
    worker = config.workers.get(record.worker_id)
    if worker is None:
        raise InspectionError("recorded worker configuration is unavailable")

    lease = _lease_evidence(config, record)
    control: GitHubControl | None = None
    token = None
    operation_error: Exception | None = None
    pull_request: dict[str, object] | None = None
    artifact_status: str | None = None
    artifact_classification: str | None = None
    expected_schema = record.packet_schema
    observed_schema: str | None = None
    try:
        control = GitHubControl(
            config.vault_client(),
            worker.app_secret_path,
            config.control_repository,
            config.github_ca_file,
        )
        token = control.mint_read_token()
        search = control.find_inspection_prs(token.value, record.control_branch)
        pr_evidence = [_pull_request_evidence(pr) for pr in search.matches]
        if not search.matches:
            status = (
                "no_matching_exact_head_pr"
                if search.complete
                else "search_incomplete_no_match"
            )
            pull_request = {
                "status": status,
                "url": None,
                "number": None,
                "head_sha": None,
                "state": None,
                "draft": None,
                "merged_at": None,
                "matches": [],
                "search_complete": search.complete,
                "pages_scanned": search.pages_scanned,
                "matches_truncated": search.matches_truncated,
            }
            artifact_status = "not_checked"
            artifact_classification = status
        else:
            pr = search.matches[0]
            summary = pr_evidence[0]
            pull_request = {
                "status": "found",
                **summary,
                "matches": pr_evidence,
                "search_complete": search.complete,
                "pages_scanned": search.pages_scanned,
                "matches_truncated": search.matches_truncated,
            }
            try:
                raw_artifact = control.read_artifact(token.value, record.artifact_path, pr.head_sha)
            except GitHubHTTPError as error:
                artifact_status = "missing" if error.status == 404 else "unreadable"
                artifact_classification = (
                    "artifact_not_found" if error.status == 404 else "github_read_failed"
                )
            except Exception:
                artifact_status = "unreadable"
                artifact_classification = "artifact_unreadable"
            else:
                classified = _classify_artifact(record, raw_artifact)
                artifact_status = "read"
                artifact_classification = classified.classification
                expected_schema = classified.expected_schema
                observed_schema = classified.observed_schema
    except Exception as error:
        operation_error = error
    finally:
        if token is not None and control is not None:
            try:
                control.revoke_token(token.value)
            except Exception as error:
                raise InspectionError(
                    "GitHub read token revocation could not be confirmed"
                ) from error
    if operation_error is not None:
        raise InspectionError("GitHub artifact evidence is unavailable") from operation_error
    if pull_request is None or artifact_status is None or artifact_classification is None:
        raise InspectionError("GitHub artifact evidence is incomplete")
    artifact = {
        "status": artifact_status,
        "classification": artifact_classification,
        "expected_schema": expected_schema,
        "observed_schema": observed_schema,
        "validation_scope": "preliminary_schema_and_identity_fields_only",
    }
    return {
        "job_id": record.job_id,
        "journal_state": record.state,
        "worker_id": record.worker_id,
        "packet_schema": record.packet_schema,
        "lease": lease,
        "pull_request": pull_request,
        "artifact": artifact,
        "coding_acceptance": "not_accepted_by_inspection",
        "interpretation": (
            "This is preliminary evidence only. PR state and artifact identity do not accept a "
            "coding result or validate its patch. It does not prove that the Bot is idle or that "
            "the job is complete."
        ),
    }
