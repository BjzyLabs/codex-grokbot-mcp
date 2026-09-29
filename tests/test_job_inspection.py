from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_grokbot_mcp.config import Config, WorkerConfig
from codex_grokbot_mcp.control import (
    ControlError,
    GitHubHTTPError,
    InspectionPR,
    InspectionPRSearch,
)
from codex_grokbot_mcp.inspection import InspectionError, inspect_coding_job
from codex_grokbot_mcp.jobs import JobStore
from codex_grokbot_mcp.local import Workspace
from codex_grokbot_mcp.vault import LeaseRecord, VaultLeaseStore, VaultUnavailable

JOB_ID = "ae9af390-8fb2-4701-9d3d-57ac7e5ce158"
HEAD_SHA = "a" * 40


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _setup(tmp_path: Path) -> tuple[Config, JobStore]:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", "https://github.com/example/source.git")
    (root / "module.py").write_text("private source marker\n")
    _git(root, "add", "module.py")
    _git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    context = Workspace.open(root, opted_in=True).snapshot(
        read_paths=["module.py"], write_paths=["module.py"]
    )
    store = JobStore.open(tmp_path / "private" / "jobs.sqlite3")
    store.create(
        JOB_ID,
        "devcoder",
        context,
        lease_owner="codex-grokbot-mcp",
        control_branch="grokbot/job-coding-ae9af390",
        artifact_path="artifacts/patch-ae9af390.json",
    )
    store.advance(JOB_ID, "conflict")
    config = Config(
        "https://vault.example.invalid:8200",
        "kv",
        tmp_path / "role",
        tmp_path / "secret",
        tmp_path / "ca",
        tmp_path / "ca",
        tmp_path / "ca",
        tmp_path / "private" / "jobs.sqlite3",
        "BjzyLabs/grokbot-twitter-dispatch",
        {
            "devcoder": WorkerConfig(
                "devcoder", "webhook/devcoder", "apps/devcoder", "leases", "devcoder"
            )
        },
        {},
    )
    return config, store


class GitHubStub:
    def __init__(self, *_args) -> None:
        self.events: list[str] = []
        self.search = InspectionPRSearch(
            (
                InspectionPR(
                    139,
                    "open",
                    True,
                    None,
                    HEAD_SHA,
                    "grokbot/job-coding-ae9af390",
                    "BjzyLabs/grokbot-twitter-dispatch",
                    "main",
                    "BjzyLabs/grokbot-twitter-dispatch",
                ),
            ),
            True,
            1,
            False,
        )
        self.artifact = {
            "schema_version": "v1",
            "job_type": "coding",
            "job_id": JOB_ID,
            "target_repo": "example/source",
            "base_sha": "b" * 40,
            "patch": "do not expose this patch text",
        }
        self.artifact_error: Exception | None = None
        self.revoke_error = False

    def mint_read_token(self):
        self.events.append("mint_read_token")
        return SimpleNamespace(value="synthetic-read-token")

    def find_inspection_prs(self, token, branch):
        assert token == "synthetic-read-token"  # noqa: S105
        assert branch == "grokbot/job-coding-ae9af390"
        self.events.append("find_inspection_prs")
        return self.search

    def read_artifact(self, token, path, head_sha):
        assert token == "synthetic-read-token"  # noqa: S105
        assert path == "artifacts/patch-ae9af390.json"
        assert head_sha == HEAD_SHA
        self.events.append("read_artifact")
        if self.artifact_error is not None:
            raise self.artifact_error
        return self.artifact

    def revoke_token(self, token):
        assert token == "synthetic-read-token"  # noqa: S105
        self.events.append("revoke_token")
        if self.revoke_error:
            raise ControlError("synthetic revocation failure")


def _patch_reads(monkeypatch, github: GitHubStub, lease: LeaseRecord | None) -> None:
    import codex_grokbot_mcp.inspection as inspection

    monkeypatch.setattr(inspection, "GitHubControl", lambda *args: github)
    monkeypatch.setattr(Config, "vault_client", lambda self: object())
    monkeypatch.setattr(VaultLeaseStore, "require_cas", lambda self, worker: None)
    monkeypatch.setattr(VaultLeaseStore, "read", lambda self, worker: lease)


def test_inspection_reports_schema_v1_mismatch_pr_and_exact_lease_match(
    tmp_path: Path, monkeypatch
) -> None:
    config, store = _setup(tmp_path)
    now = datetime.now(UTC)
    lease = LeaseRecord(
        "devcoder",
        "active",
        "codex-grokbot-mcp",
        JOB_ID,
        now,
        now + timedelta(minutes=5),
        now + timedelta(minutes=45),
        24,
    )
    github = GitHubStub()
    _patch_reads(monkeypatch, github, lease)

    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["journal_state"] == "conflict"
    assert result["lease"] == {
        "read_status": "read",
        "state": "active",
        "version": 24,
        "owner_matches_job": True,
        "job_matches": True,
    }
    assert result["pull_request"] == {
        "status": "found",
        "url": "https://github.com/BjzyLabs/grokbot-twitter-dispatch/pull/139",
        "number": 139,
        "state": "open",
        "draft": True,
        "merged_at": None,
        "lifecycle": "open_draft_to_main",
        "head_ref": "grokbot/job-coding-ae9af390",
        "head_repository": "BjzyLabs/grokbot-twitter-dispatch",
        "head_sha": HEAD_SHA,
        "base_ref": "main",
        "base_repository": "BjzyLabs/grokbot-twitter-dispatch",
        "matches": [
            {
                "number": 139,
                "url": "https://github.com/BjzyLabs/grokbot-twitter-dispatch/pull/139",
                "state": "open",
                "draft": True,
                "merged_at": None,
                "lifecycle": "open_draft_to_main",
                "head_ref": "grokbot/job-coding-ae9af390",
                "head_repository": "BjzyLabs/grokbot-twitter-dispatch",
                "head_sha": HEAD_SHA,
                "base_ref": "main",
                "base_repository": "BjzyLabs/grokbot-twitter-dispatch",
            }
        ],
        "search_complete": True,
        "pages_scanned": 1,
        "matches_truncated": False,
    }
    assert result["artifact"] == {
        "status": "read",
        "classification": "schema_v1_expected_v2",
        "expected_schema": "v2",
        "observed_schema": "v1",
        "validation_scope": "preliminary_schema_and_identity_fields_only",
    }
    assert "do not expose this patch text" not in str(result)
    assert "private source marker" not in str(result)
    assert github.events == [
        "mint_read_token",
        "find_inspection_prs",
        "read_artifact",
        "revoke_token",
    ]
    assert store.get(JOB_ID).state == "conflict"
    store.close()


def test_inspection_labels_no_matching_exact_head_pr(tmp_path: Path, monkeypatch) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    github.search = InspectionPRSearch((), True, 1, False)
    _patch_reads(monkeypatch, github, None)

    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["pull_request"] == {
        "status": "no_matching_exact_head_pr",
        "url": None,
        "number": None,
        "head_sha": None,
        "state": None,
        "draft": None,
        "merged_at": None,
        "matches": [],
        "search_complete": True,
        "pages_scanned": 1,
        "matches_truncated": False,
    }
    assert result["artifact"]["classification"] == "no_matching_exact_head_pr"
    assert "pr_url" not in result
    assert result["lease"]["state"] == "missing"
    assert "do not accept a coding result" in result["interpretation"]
    assert github.events == ["mint_read_token", "find_inspection_prs", "revoke_token"]
    store.close()


def test_inspection_marks_vault_read_failure_unavailable(tmp_path: Path, monkeypatch) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    _patch_reads(monkeypatch, github, None)

    def unavailable(self, worker):
        raise VaultUnavailable("synthetic vault outage")

    monkeypatch.setattr(VaultLeaseStore, "read", unavailable)
    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["lease"] == {
        "read_status": "unavailable",
        "state": "unavailable",
        "version": None,
        "owner_matches_job": None,
        "job_matches": None,
        "error_code": "vault_read_failed",
    }
    assert "synthetic vault outage" not in str(result)
    store.close()


def test_inspection_fails_closed_if_github_token_revocation_fails(
    tmp_path: Path, monkeypatch
) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    github.revoke_error = True
    _patch_reads(monkeypatch, github, None)

    with pytest.raises(InspectionError, match="revocation could not be confirmed"):
        inspect_coding_job(config, store.get(JOB_ID), JOB_ID)
    assert github.events[-1] == "revoke_token"
    store.close()


def test_inspection_retains_verified_pr_when_artifact_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    github.artifact_error = GitHubHTTPError(404)
    _patch_reads(monkeypatch, github, None)

    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["pull_request"]["number"] == 139
    assert result["pull_request"]["head_sha"] == HEAD_SHA
    assert result["artifact"]["status"] == "missing"
    assert result["artifact"]["classification"] == "artifact_not_found"
    assert github.events[-1] == "revoke_token"
    store.close()


@pytest.mark.parametrize(
    ("state", "merged_at", "lifecycle"),
    [
        ("closed", None, "closed_unmerged"),
        ("closed", "2026-09-28T12:00:00Z", "merged"),
    ],
)
def test_inspection_reports_closed_and_merged_exact_head_without_accepting_them(
    tmp_path: Path, monkeypatch, state: str, merged_at: str | None, lifecycle: str
) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    github.search = InspectionPRSearch(
        (
            InspectionPR(
                139,
                state,
                False,
                merged_at,
                HEAD_SHA,
                "grokbot/job-coding-ae9af390",
                config.control_repository,
                "main",
                config.control_repository,
            ),
        ),
        True,
        1,
        False,
    )
    _patch_reads(monkeypatch, github, None)

    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["pull_request"]["state"] == "closed"
    assert result["pull_request"]["merged_at"] == merged_at
    assert result["pull_request"]["lifecycle"] == lifecycle
    assert result["artifact"]["status"] == "read"
    assert result["coding_acceptance"] == "not_accepted_by_inspection"
    assert "do not accept a coding result" in result["interpretation"]
    store.close()


def test_inspection_reports_incomplete_search_without_claiming_no_match(
    tmp_path: Path, monkeypatch
) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    github.search = InspectionPRSearch((), False, 5, False)
    _patch_reads(monkeypatch, github, None)

    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["pull_request"]["status"] == "search_incomplete_no_match"
    assert result["pull_request"]["search_complete"] is False
    assert result["artifact"]["classification"] == "search_incomplete_no_match"
    store.close()


def test_inspection_labels_matching_schema_as_preliminary_only(tmp_path: Path, monkeypatch) -> None:
    config, store = _setup(tmp_path)
    github = GitHubStub()
    github.artifact.update(
        {
            "schema_version": "v2",
            "base_sha": store.get(JOB_ID).head,
        }
    )
    _patch_reads(monkeypatch, github, None)

    result = inspect_coding_job(config, store.get(JOB_ID), JOB_ID)

    assert result["artifact"]["classification"] == "identity_fields_match"
    assert result["artifact"]["validation_scope"] == "preliminary_schema_and_identity_fields_only"
    assert "do not accept a coding result" in result["interpretation"]
    store.close()


def test_inspection_does_not_mint_token_for_missing_job(tmp_path: Path, monkeypatch) -> None:
    config, store = _setup(tmp_path)
    import codex_grokbot_mcp.inspection as inspection

    monkeypatch.setattr(
        inspection,
        "GitHubControl",
        lambda *_args: pytest.fail("missing job must not access GitHub"),
    )
    assert inspect_coding_job(config, None, "unknown-job") == {
        "job_id": "unknown-job",
        "state": "not_found",
    }
    store.close()
