from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_grokbot_mcp.jobs import JobStateError, JobStore
from codex_grokbot_mcp.local import Workspace


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def context(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", "https://github.com/example/source.git")
    (root / "module.py").write_text("private source marker\n")
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
    return Workspace.open(root, opted_in=True).snapshot(
        read_paths=["module.py"], write_paths=["module.py"]
    )


def new_store(tmp_path: Path) -> JobStore:
    return JobStore.open(tmp_path / "private" / "jobs.sqlite3")


def create_job(store: JobStore, context) -> None:
    store.create(
        "job-123",
        "worker-a",
        context,
        lease_owner="codex-grokbot-mcp",
        control_branch="grokbot/job-coding-1234abcd",
        artifact_path="artifacts/patch-1234abcd.json",
    )


def test_private_store_and_no_source_content_survive_reopen(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    store.close()

    db = tmp_path / "private" / "jobs.sqlite3"
    assert stat.S_IMODE(db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    assert b"private source marker" not in db.read_bytes()
    reopened = JobStore.open(db)
    record = reopened.get("job-123")
    assert record is not None
    assert record.state == "queued"
    assert record.worker_id == "worker-a"
    assert record.snapshot_digest == context.snapshot_digest
    assert record.control_branch == "grokbot/job-coding-1234abcd"
    assert record.lease_owner == "codex-grokbot-mcp"
    assert record.read_paths == ("module.py",)
    reopened.close()


def test_transitions_are_atomic_and_restart_is_uncertain(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    with pytest.raises(JobStateError):
        store.advance("job-123", "dispatching")
    assert store.get("job-123").state == "queued"
    store.advance("job-123", "lease_held")
    store.record_token_expiry("job-123", datetime.now(UTC) + timedelta(hours=1))
    store.advance("job-123", "dispatching")
    store.close()

    reopened = JobStore.open(tmp_path / "private" / "jobs.sqlite3")
    assert reopened.reconcile_restart() == ("job-123",)
    assert reopened.get("job-123").state == "uncertain"
    with pytest.raises(JobStateError):
        reopened.advance("job-123", "dispatching")
    reopened.close()


def test_dispatched_job_is_uncertain_after_restart_and_never_requeued(
    tmp_path: Path, context
) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    store.advance("job-123", "lease_held")
    store.record_token_expiry("job-123", datetime.now(UTC) + timedelta(hours=1))
    for state in ("dispatching", "dispatched"):
        store.advance("job-123", state)
    store.close()

    reopened = JobStore.open(tmp_path / "private" / "jobs.sqlite3")
    assert reopened.reconcile_restart() == ("job-123",)
    assert reopened.get("job-123").state == "uncertain"
    assert reopened.reconcile_restart() == ()
    reopened.close()


def test_workspace_drift_marks_conflict_without_losing_evidence(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    assert store.verify_workspace("job-123") is True
    (context.root / "module.py").write_text("changed after dispatch\n")
    assert store.verify_workspace("job-123") is False
    record = store.get("job-123")
    assert record.state == "conflict"
    assert record.snapshot_digest == context.snapshot_digest
    with pytest.raises(JobStateError):
        store.advance("job-123", "ready")
    store.close()


def test_rejects_unsafe_store_paths_and_duplicate_jobs(tmp_path: Path, context) -> None:
    db_dir = tmp_path / "private"
    db_dir.mkdir(mode=0o700)
    link = db_dir / "jobs.sqlite3"
    link.symlink_to(tmp_path / "outside")
    with pytest.raises(JobStateError):
        JobStore.open(link)
    link.unlink()
    os.chmod(db_dir, 0o755)  # noqa: S103
    with pytest.raises(JobStateError):
        JobStore.open(link)
    os.chmod(db_dir, 0o700)
    store = JobStore.open(link)
    create_job(store, context)
    with pytest.raises(JobStateError):
        create_job(store, context)
    store.close()


def test_invalid_dispatch_coordinates_are_rejected(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    with pytest.raises(JobStateError):
        store.create(
            "job-123",
            "worker-a",
            context,
            lease_owner="codex-grokbot-mcp",
            control_branch="../other",
            artifact_path="artifacts/patch-1234abcd.json",
        )
    assert store.get("job-123") is None
    store.close()


def test_queued_job_remains_queued_after_restart(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    store.close()
    reopened = JobStore.open(tmp_path / "private" / "jobs.sqlite3")
    assert reopened.reconcile_restart() == ()
    assert reopened.get("job-123").state == "queued"
    reopened.close()


def test_existing_unversioned_store_is_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    db = directory / "jobs.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE unrelated (value TEXT)")
    connection.close()
    os.chmod(db, 0o600)
    with pytest.raises(JobStateError, match="schema"):
        JobStore.open(db)


def test_database_open_failure_is_reported(tmp_path: Path, monkeypatch) -> None:
    def denied(*_args, **_kwargs):
        raise sqlite3.OperationalError("denied")

    monkeypatch.setattr(sqlite3, "connect", denied)
    with pytest.raises(JobStateError, match="schema"):
        new_store(tmp_path)


def test_two_jobs_cannot_reuse_a_control_branch_or_artifact(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    with pytest.raises(JobStateError, match="coordinates"):
        store.create(
            "job-456",
            "worker-a",
            context,
            lease_owner="codex-grokbot-mcp",
            control_branch="grokbot/job-coding-1234abcd",
            artifact_path="artifacts/patch-1234abcd.json",
        )
    assert store.get("job-456") is None
    store.close()


def test_dispatch_requires_durable_nonsecret_token_expiry(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    store.advance("job-123", "lease_held")
    with pytest.raises(JobStateError, match="expiry"):
        store.advance("job-123", "dispatching")
    expires = datetime.now(UTC) + timedelta(hours=1)
    store.record_token_expiry("job-123", expires)
    assert store.get("job-123").token_expires_at == expires
    with pytest.raises(JobStateError):
        store.record_token_expiry("job-123", expires)
    store.advance("job-123", "dispatching")
    store.close()

    reopened = JobStore.open(tmp_path / "private" / "jobs.sqlite3")
    assert reopened.reconcile_restart() == ("job-123",)
    assert reopened.get("job-123").state == "uncertain"
    assert reopened.get("job-123").token_expires_at == expires
    reopened.close()


def test_rejects_naive_or_short_token_expiry(tmp_path: Path, context) -> None:
    store = new_store(tmp_path)
    create_job(store, context)
    store.advance("job-123", "lease_held")
    for expires in (datetime.now(), datetime.now(UTC) + timedelta(minutes=5)):
        with pytest.raises(JobStateError, match="expiry"):
            store.record_token_expiry("job-123", expires)
    assert store.get("job-123").token_expires_at is None
    store.close()


@pytest.mark.parametrize("prior_state", ["queued", "dispatching"])
def test_schema_v1_journal_migrates_without_losing_jobs(
    tmp_path: Path, context, prior_state: str
) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    db = directory / "jobs.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    connection.execute("INSERT INTO schema_version VALUES (1)")
    connection.execute(
        """CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
            lease_owner TEXT NOT NULL, root TEXT NOT NULL, target_repo TEXT NOT NULL,
            head TEXT NOT NULL, branch TEXT NOT NULL, read_paths TEXT NOT NULL,
            write_paths TEXT NOT NULL, snapshot_digest TEXT NOT NULL,
            control_branch TEXT NOT NULL, artifact_path TEXT NOT NULL,
            state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )"""
    )
    now = datetime.now(UTC).isoformat()
    connection.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "job-123",
            "worker-a",
            "codex-grokbot-mcp",
            str(context.root),
            context.target_repo,
            context.head,
            context.branch,
            '["module.py"]',
            '["module.py"]',
            context.snapshot_digest,
            "grokbot/job-coding-1234abcd",
            "artifacts/patch-1234abcd.json",
            prior_state,
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()
    db.chmod(0o600)

    store = JobStore.open(db)
    assert store.get("job-123").snapshot_digest == context.snapshot_digest
    assert store.get("job-123").token_expires_at is None
    assert store.reconcile_restart() == (("job-123",) if prior_state == "dispatching" else ())
    assert store.get("job-123").state == ("uncertain" if prior_state == "dispatching" else "queued")
    store.close()
    with sqlite3.connect(db) as check:
        assert check.execute("SELECT version FROM schema_version").fetchone()[0] == 2
