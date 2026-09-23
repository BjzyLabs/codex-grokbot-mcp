from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mcp import Client, StdioServerParameters

from codex_grokbot_mcp.config import Config, WorkerConfig
from codex_grokbot_mcp.jobs import JobStore
from codex_grokbot_mcp.local import Workspace
from codex_grokbot_mcp.server import create_server
from codex_grokbot_mcp.vault import LeaseRecord, VaultLeaseStore, VaultUnavailable


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def journal(tmp_path: Path) -> JobStore:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", "https://github.com/example/source.git")
    (root / "module.py").write_text("private source marker\n")
    git(root, "add", "module.py")
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
    context = Workspace.open(root, opted_in=True).snapshot(
        read_paths=["module.py"], write_paths=["module.py"]
    )
    store = JobStore.open(tmp_path / "private" / "jobs.sqlite3")
    store.create(
        "job-123",
        "worker-a",
        context,
        lease_owner="test-owner",
        control_branch="grokbot/job-coding-1234abcd",
        artifact_path="artifacts/patch-1234abcd.json",
    )
    return store


def configuration(tmp_path: Path) -> Config:
    (tmp_path / "ca").write_text("synthetic CA placeholder\n")
    return Config(
        "https://vault.example.invalid:8200",
        "kv",
        tmp_path / "role",
        tmp_path / "secret",
        tmp_path / "ca",
        tmp_path / "ca",
        tmp_path / "ca",
        tmp_path / "private" / "jobs.sqlite3",
        "example/control",
        {"worker-a": WorkerConfig("worker-a", "webhook/a", "app/a", "leases", "shared-a")},
        {},
    )


def test_protocol_status_is_minimal_and_restart_reconciles(tmp_path: Path) -> None:
    store = journal(tmp_path)
    store.advance("job-123", "lease_held")
    config = configuration(tmp_path)

    async def exercise() -> None:
        async with Client(create_server(config, store), raise_exceptions=True) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert tools == {"grokbot_status", "grokbot_active"}
            found = (
                await client.call_tool("grokbot_status", {"job_id": "job-123"})
            ).structured_content
            assert found == {
                "job_id": "job-123",
                "worker_id": "worker-a",
                "state": "uncertain",
                "updated_at": store.get("job-123").updated_at,
            }
            assert "private source marker" not in str(found)
            assert str(tmp_path) not in str(found)
            missing = (
                await client.call_tool("grokbot_status", {"job_id": "missing"})
            ).structured_content
            assert missing == {"job_id": "missing", "state": "not_found"}

    asyncio.run(exercise())
    store.close()


def test_active_reports_vault_lease_and_local_uncertainty(tmp_path: Path, monkeypatch) -> None:
    store = journal(tmp_path)
    store.advance("job-123", "lease_held")
    config = configuration(tmp_path)
    now = datetime.now(UTC)
    lease = LeaseRecord(
        "shared-a",
        "active",
        "test-owner",
        "job-123",
        now,
        now + timedelta(minutes=5),
        now + timedelta(minutes=45),
        1,
    )
    monkeypatch.setattr(VaultLeaseStore, "require_cas", lambda self, worker: None)
    monkeypatch.setattr(VaultLeaseStore, "read", lambda self, worker: lease)

    async def exercise() -> None:
        async with Client(create_server(config, store), raise_exceptions=True) as client:
            result = (await client.call_tool("grokbot_active")).structured_content
            assert result == {
                "workers": [{"worker_id": "worker-a", "state": "busy"}],
                "jobs": [{"job_id": "job-123", "worker_id": "worker-a", "state": "uncertain"}],
            }

    asyncio.run(exercise())
    store.close()


def test_active_fails_closed_when_vault_is_unavailable(tmp_path: Path, monkeypatch) -> None:
    store = journal(tmp_path)
    config = configuration(tmp_path)

    def unavailable(self, worker):
        raise VaultUnavailable("synthetic private failure")

    monkeypatch.setattr(VaultLeaseStore, "require_cas", unavailable)

    async def exercise() -> None:
        async with Client(create_server(config, store), raise_exceptions=True) as client:
            result = await client.call_tool("grokbot_active")
            assert result.is_error is True
            assert "synthetic private failure" not in str(result)
            assert result.structured_content is None

    asyncio.run(exercise())
    store.close()


def test_real_stdio_transport_serves_status(tmp_path: Path) -> None:
    store = journal(tmp_path)
    store.close()
    config = configuration(tmp_path)
    for name in ("role", "secret"):
        path = tmp_path / name
        path.write_text("synthetic-only\n")
        os.chmod(path, 0o600)
    (tmp_path / "ca").write_text("synthetic CA placeholder\n")
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        "version = 1\n"
        f'vault_address = "{config.vault_address}"\n'
        'vault_mount = "kv"\n'
        f'vault_role_id_file = "{tmp_path / "role"}"\n'
        f'vault_secret_id_file = "{tmp_path / "secret"}"\n'
        f'vault_ca_file = "{tmp_path / "ca"}"\n'
        f'github_ca_file = "{tmp_path / "ca"}"\n'
        f'webhook_ca_file = "{tmp_path / "ca"}"\n'
        f'job_database = "{config.job_database}"\n'
        'control_repository = "example/control"\n'
        "[workers.worker-a]\n"
        'webhook_secret_path = "webhook/a"\n'
        'app_secret_path = "app/a"\n'
        'lease_prefix = "leases"\n'
        'lease_worker = "shared-a"\n'
        "[workspaces]\n"
    )
    os.chmod(config_file, 0o600)

    async def exercise() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "codex_grokbot_mcp.server", "--config", str(config_file)],
        )
        async with Client(params) as client:
            result = (
                await client.call_tool("grokbot_status", {"job_id": "job-123"})
            ).structured_content
            assert result == {
                "job_id": "job-123",
                "worker_id": "worker-a",
                "state": "queued",
                "updated_at": result["updated_at"],
            }

    asyncio.run(exercise())


def test_available_lease_does_not_hide_uncertain_local_job(tmp_path: Path, monkeypatch) -> None:
    store = journal(tmp_path)
    store.advance("job-123", "lease_held")
    config = configuration(tmp_path)
    monkeypatch.setattr(VaultLeaseStore, "require_cas", lambda self, worker: None)
    monkeypatch.setattr(VaultLeaseStore, "read", lambda self, worker: None)

    async def exercise() -> None:
        async with Client(create_server(config, store), raise_exceptions=True) as client:
            result = (await client.call_tool("grokbot_active")).structured_content
            assert result["workers"] == [
                {"worker_id": "worker-a", "state": "reconciliation_required"}
            ]
            assert result["jobs"][0]["state"] == "uncertain"

    asyncio.run(exercise())
    store.close()
