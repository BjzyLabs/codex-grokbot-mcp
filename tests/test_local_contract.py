from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codex_grokbot_mcp.local import ContractError, Workspace, validate_artifact


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", "https://github.com/example/project.git")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("value = 1\n")
    (root / "src" / "other.py").write_text("other = 1\n")
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "base",
    )
    return root


def artifact(context, patch: str, paths: list[str], *, version: str = "v2") -> dict:
    result = {
        "schema_version": version,
        "job_type": "coding",
        "job_id": "job-123",
        "target_repo": "example/project",
        "base_sha": context.head,
        "declared_changed_paths": paths,
        "patch": patch,
        "summary": "Update module",
        "completed_at": "2026-01-01T00:00:00Z",
    }
    if version == "v2":
        result["workspace_head"] = context.head
        result["snapshot_digest"] = context.snapshot_digest
    return result


def test_opt_in_and_dirty_snapshot_are_bound_to_public_context(repo: Path) -> None:
    (repo / "src" / "module.py").write_text("value = 2\n")
    with pytest.raises(ContractError, match="opt-in"):
        Workspace.open(repo, opted_in=False)

    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py", "src/other.py"], write_paths=["src/module.py"]
    )
    public = context.public_context()

    assert public["target_repo"] == "example/project"
    assert public["source_snapshots"][0]["content"] == "value = 2\n"
    assert public["write_paths"] == ["src/module.py"]
    assert str(repo) not in str(public)
    assert len(public["snapshot_digest"]) == 64


@pytest.mark.parametrize(
    "path", ["../outside", "/outside/absolute", ".git/config", ".env", "src/secret.key"]
)
def test_sensitive_or_escaping_source_is_rejected(repo: Path, path: str) -> None:
    with pytest.raises(ContractError):
        Workspace.open(repo, opted_in=True).snapshot(read_paths=[path], write_paths=[path])


def test_symlink_and_binary_sources_are_rejected(repo: Path) -> None:
    (repo / "src" / "link.py").symlink_to(repo / "src" / "module.py")
    (repo / "src" / "binary.py").write_bytes(b"a\x00b")
    for path in ("src/link.py", "src/binary.py"):
        with pytest.raises(ContractError):
            Workspace.open(repo, opted_in=True).snapshot(read_paths=[path], write_paths=[path])


def test_dirty_snapshot_patch_is_checked_without_applying_to_workspace(repo: Path) -> None:
    file = repo / "src" / "module.py"
    file.write_text("value = 2\n")
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/module.py"]
    )
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-value = 2\n+value = 3\n"

    accepted = validate_artifact(context, "job-123", artifact(context, patch, ["src/module.py"]))

    assert accepted.changed_paths == ("src/module.py",)
    assert file.read_text() == "value = 2\n"


def test_declared_paths_must_equal_actual_paths(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py", "src/other.py"], write_paths=["src/module.py", "src/other.py"]
    )
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    for declared in ([], ["src/module.py", "src/other.py"]):
        with pytest.raises(ContractError, match="declared"):
            validate_artifact(context, "job-123", artifact(context, patch, declared))


def test_actual_paths_must_be_within_write_paths(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py", "src/other.py"], write_paths=["src/module.py"]
    )
    patch = "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-other = 1\n+other = 2\n"
    with pytest.raises(ContractError, match="write_paths"):
        validate_artifact(context, "job-123", artifact(context, patch, ["src/other.py"]))


def test_workspace_drift_rejects_result(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/module.py"]
    )
    (repo / "src" / "module.py").write_text("value = 9\n")
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    with pytest.raises(ContractError, match="workspace_drift"):
        validate_artifact(context, "job-123", artifact(context, patch, ["src/module.py"]))


def test_v1_artifact_and_identity_checks(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/module.py"]
    )
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    result = artifact(context, patch, ["src/module.py"], version="v1")
    assert validate_artifact(context, "job-123", result).changed_paths == ("src/module.py",)
    result["job_id"] = "other"
    with pytest.raises(ContractError, match="job_id"):
        validate_artifact(context, "job-123", result)


def test_v2_snapshot_digest_and_rename_are_rejected(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/module.py"]
    )
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    result = artifact(context, patch, ["src/module.py"])
    result["snapshot_digest"] = "0" * 64
    with pytest.raises(ContractError, match="snapshot_digest"):
        validate_artifact(context, "job-123", result)
    rename = (
        "diff --git a/src/module.py b/src/moved.py\n"
        "rename from src/module.py\n"
        "rename to src/moved.py\n"
    )
    with pytest.raises(ContractError):
        validate_artifact(context, "job-123", artifact(context, rename, ["src/moved.py"]))


def test_new_file_patch_is_allowed_only_when_declared(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/new.py"]
    )
    patch = "--- /dev/null\n+++ b/src/new.py\n@@ -0,0 +1 @@\n+value = 1\n"
    result = artifact(context, patch, ["src/new.py"])

    assert validate_artifact(context, "job-123", result).changed_paths == ("src/new.py",)
    assert not (repo / "src" / "new.py").exists()


def test_traversal_patch_path_is_rejected(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/module.py"]
    )
    patch = "--- a/../outside\n+++ b/../outside\n@@ -1 +1 @@\n-old\n+new\n"
    with pytest.raises(ContractError):
        validate_artifact(context, "job-123", artifact(context, patch, ["src/module.py"]))


def test_patch_must_apply_to_exact_snapshot(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py"], write_paths=["src/module.py"]
    )
    patch = "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-wrong = 1\n+value = 2\n"
    with pytest.raises(ContractError, match="Git validation"):
        validate_artifact(context, "job-123", artifact(context, patch, ["src/module.py"]))


def test_second_actual_patch_path_cannot_be_omitted_from_declaration(repo: Path) -> None:
    context = Workspace.open(repo, opted_in=True).snapshot(
        read_paths=["src/module.py", "src/other.py"],
        write_paths=["src/module.py", "src/other.py"],
    )
    patch = (
        "--- a/src/module.py\n+++ b/src/module.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
        "--- a/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n-other = 1\n+other = 2\n"
    )
    with pytest.raises(ContractError, match="declared"):
        validate_artifact(context, "job-123", artifact(context, patch, ["src/module.py"]))
