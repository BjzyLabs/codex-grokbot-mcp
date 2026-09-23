from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_grokbot_mcp.config import Config, WorkerConfig, WorkspaceRule
from codex_grokbot_mcp.control import (
    ArtifactPR,
    ControlError,
    GitHubControl,
    InstallationToken,
    WebhookTransport,
    WebhookUncertain,
)
from codex_grokbot_mcp.coordinator import Coordinator, CoordinatorError
from codex_grokbot_mcp.jobs import JobStore
from codex_grokbot_mcp.vault import (
    LeaseRecord,
    LeaseUncertain,
    VaultClient,
    VaultLeaseStore,
    WorkerBusy,
)

PATCH = "--- a/module.py\n+++ b/module.py\n@@ -1 +1,2 @@\n before\n+after\n"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", "https://github.com/example/source.git")
    (root / "module.py").write_text("before\n")
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
    return root


def configuration(tmp_path: Path, root: Path, *, opted_in: bool = True) -> Config:
    ca = tmp_path / "ca"
    ca.write_text("synthetic CA fixture\n")
    return Config(
        "https://vault.example.invalid",
        "kv",
        tmp_path / "role",
        tmp_path / "secret",
        ca,
        ca,
        ca,
        tmp_path / "private" / "jobs.sqlite3",
        "example/control",
        {"worker-a": WorkerConfig("worker-a", "webhook/a", "app/a", "leases", "shared-a")},
        {root: WorkspaceRule(opted_in, frozenset({"worker-a"}))},
    )


class FakeServices:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.packet: dict | None = None
        self.artifact: dict | None = None
        self.webhook_error: Exception | None = None
        self.lease_error: Exception | None = None
        self.renew_error: Exception | None = None
        self.result_read_error: Exception | None = None
        self.on_read = None
        self.pr_present = True
        self.deadline_past = False
        self.renew_immediately = False
        self.patch = PATCH
        self.declared_paths = ["module.py"]
        self.token_expiry = datetime.now(UTC) + timedelta(hours=1)
        self.minted_value = "synthetic-control-token"
        self.head_sha = "a" * 40
        monkeypatch.setattr(
            VaultClient, "read_secret", lambda client, path: self.read_secret(client, path)
        )
        monkeypatch.setattr(
            VaultLeaseStore,
            "acquire",
            lambda store, worker, **kw: self.acquire(store, worker, **kw),
        )
        monkeypatch.setattr(
            VaultLeaseStore, "renew", lambda store, worker, **kw: self.renew(store, worker, **kw)
        )
        monkeypatch.setattr(
            VaultLeaseStore,
            "release",
            lambda store, worker, **kw: self.release(store, worker, **kw),
        )
        monkeypatch.setattr(GitHubControl, "mint_worker_token", lambda control: self.mint(control))
        monkeypatch.setattr(
            GitHubControl, "revoke_token", lambda control, token: self.revoke(control, token)
        )
        monkeypatch.setattr(
            GitHubControl,
            "find_artifact_pr",
            lambda control, token, branch: self.find_pr(control, token, branch),
        )
        monkeypatch.setattr(
            GitHubControl,
            "read_artifact",
            lambda control, token, path, head: self.read_artifact(control, token, path, head),
        )
        monkeypatch.setattr(
            WebhookTransport, "dispatch", lambda webhook, packet: self.dispatch(webhook, packet)
        )

    def read_secret(self, _client, path: str) -> dict:
        self.events.append("vault_secret")
        assert path == "webhook/a"
        return {"webhook_url": "https://worker.example.invalid/hook", "sender_key": "synthetic"}

    def acquire(self, _store, worker: str, *, owner: str, job_id: str) -> LeaseRecord:
        self.events.append("acquire")
        assert worker == "shared-a"
        if self.lease_error:
            raise self.lease_error
        now = datetime.now(UTC)
        if self.deadline_past:
            now -= timedelta(minutes=46)
        return LeaseRecord(
            worker,
            "active",
            owner,
            job_id,
            now,
            now + (timedelta(seconds=30) if self.renew_immediately else timedelta(minutes=5)),
            now + timedelta(minutes=45),
            1,
        )

    def renew(self, _store, worker: str, *, owner: str, job_id: str) -> LeaseRecord:
        self.events.append("renew")
        if self.renew_error is not None:
            raise self.renew_error
        now = datetime.now(UTC)
        return LeaseRecord(
            worker,
            "active",
            owner,
            job_id,
            now,
            now + timedelta(minutes=5),
            now + timedelta(minutes=45),
            2,
        )

    def release(self, _store, worker: str, *, owner: str, job_id: str) -> LeaseRecord:
        self.events.append("release")
        return LeaseRecord(worker, "available", None, None, None, None, None, 3)

    def mint(self, _control) -> InstallationToken:
        self.events.append("mint")
        return InstallationToken(self.minted_value, self.token_expiry)

    def revoke(self, _control, token: str) -> None:
        self.events.append("revoke")
        assert token == self.minted_value

    def dispatch(self, _webhook, packet: dict) -> None:
        self.events.append("post")
        self.packet = packet
        if self.webhook_error:
            raise self.webhook_error

    def find_pr(self, _control, token: str, branch: str) -> ArtifactPR | None:
        self.events.append("find_pr")
        assert token == self.minted_value
        assert branch == self.packet["context"]["head_ref"]
        return ArtifactPR(1, self.head_sha) if self.pr_present else None

    def read_artifact(self, _control, token: str, path: str, head_sha: str) -> dict:
        self.events.append("read_artifact")
        if self.events.count("read_artifact") > 1 and self.result_read_error is not None:
            raise self.result_read_error
        assert token == self.minted_value
        assert path == self.packet["context"]["patch_path"]
        assert head_sha == self.head_sha
        if self.artifact is None:
            context = self.packet["context"]
            self.artifact = {
                "schema_version": "v2",
                "job_type": "coding",
                "job_id": self.packet["job_id"],
                "target_repo": context["target_repo"],
                "base_sha": context["base_sha"],
                "workspace_head": context["workspace_head"],
                "snapshot_digest": context["snapshot_digest"],
                "declared_changed_paths": self.declared_paths,
                "summary": "synthetic change",
                "patch": self.patch,
                "completed_at": datetime.now(UTC).isoformat(),
            }
        if self.on_read is not None:
            self.on_read()
        return self.artifact


async def settled(store: JobStore, job_id: str) -> str:
    for _ in range(200):
        state = store.get(job_id).state
        if state in {"ready", "uncertain", "conflict", "failed"}:
            return state
        await asyncio.sleep(0.025)
    raise AssertionError("job did not settle")


def test_successful_delegate_pins_patch_and_releases_only_after_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Add one line",
            read_paths=["module.py"],
            write_paths=["module.py"],
            acceptance_checks=["The line appears"],
            effort_hint="small",
        )
        assert submitted["state"] == "queued"
        job_id = submitted["job_id"]
        assert await settled(store, job_id) == "ready"
        assert fake.events.index("acquire") < fake.events.index("mint") < fake.events.index("post")
        assert (
            fake.events.index("read_artifact")
            < fake.events.index("revoke")
            < fake.events.index("release")
        )
        assert fake.packet["context"]["source_snapshots"] == [
            {
                "path": "module.py",
                "sha256": fake.packet["context"]["source_snapshots"][0]["sha256"],
                "content": "before\n",
            }
        ]
        assert str(root) not in str(fake.packet)
        record = store.get(job_id)
        assert record.artifact_head_sha == fake.head_sha
        result = await coordinator.result(job_id)
        assert result["state"] == "ready"
        assert result["patch"] == PATCH
        assert result["changed_paths"] == ["module.py"]
        assert (root / "module.py").read_text() == "before\n"

    asyncio.run(exercise())
    store.close()


def test_workspace_without_opt_in_never_touches_vault_or_webhook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root, opted_in=False)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    async def exercise() -> None:
        with pytest.raises(CoordinatorError, match="opt-in"):
            await Coordinator(config, store).delegate(
                root=root,
                worker_id="worker-a",
                goal="Change source",
                read_paths=["module.py"],
                write_paths=["module.py"],
                acceptance_checks=["changed"],
                effort_hint="small",
            )

    asyncio.run(exercise())
    assert fake.events == []
    store.close()


def test_busy_lease_never_mints_or_posts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.lease_error = WorkerBusy("busy")

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Change source",
            read_paths=["module.py"],
            write_paths=["module.py"],
            acceptance_checks=["changed"],
            effort_hint="small",
        )
        assert await settled(store, submitted["job_id"]) == "failed"

    asyncio.run(exercise())
    assert fake.events == ["acquire"]
    store.close()


def test_ambiguous_webhook_keeps_lease_and_never_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.webhook_error = WebhookUncertain("synthetic timeout")

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Change source",
            read_paths=["module.py"],
            write_paths=["module.py"],
            acceptance_checks=["changed"],
            effort_hint="small",
        )
        assert await settled(store, submitted["job_id"]) == "uncertain"
        assert await coordinator.result(submitted["job_id"]) == {
            "job_id": submitted["job_id"],
            "state": "uncertain",
        }

    asyncio.run(exercise())
    assert fake.events.count("post") == 1
    assert "release" not in fake.events
    store.close()


def test_official_mcp_client_can_delegate_and_fetch_pinned_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp import Client

    from codex_grokbot_mcp.server import create_server

    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    async def exercise() -> None:
        async with Client(create_server(config, store), raise_exceptions=True) as client:
            submitted = (
                await client.call_tool(
                    "grokbot_delegate",
                    {
                        "workspace": str(root),
                        "worker_id": "worker-a",
                        "goal": "Add one line",
                        "read_paths": ["module.py"],
                        "write_paths": ["module.py"],
                        "acceptance_checks": ["The line appears"],
                        "effort_hint": "small",
                    },
                )
            ).structured_content
            assert submitted["state"] == "queued"
            assert await settled(store, submitted["job_id"]) == "ready"
            response = await client.call_tool("grokbot_result", {"job_id": submitted["job_id"]})
            assert response.is_error is False, repr(response)
            result = response.structured_content
            assert result["patch"] == PATCH
            assert result["changed_paths"] == ["module.py"]
            assert str(root) not in str(result)

    asyncio.run(exercise())
    assert fake.events.count("post") == 1
    store.close()


async def submit(coordinator: Coordinator, root: Path) -> dict[str, str]:
    return await coordinator.delegate(
        root=root,
        worker_id="worker-a",
        goal="Change source",
        read_paths=["module.py"],
        write_paths=["module.py"],
        acceptance_checks=["changed"],
        effort_hint="small",
    )


@pytest.mark.parametrize(
    ("patch", "declared"),
    [
        ("--- /dev/null\n+++ b/other.py\n@@ -0,0 +1 @@\n+bad\n", ["other.py"]),
        (PATCH, ["other.py"]),
    ],
)
def test_untrusted_artifact_paths_block_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patch: str, declared: list[str]
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.patch = patch
    fake.declared_paths = declared

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "conflict"

    asyncio.run(exercise())
    assert "release" not in fake.events
    store.close()


def test_dirty_workspace_drift_blocks_return_and_lease_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    (root / "module.py").write_text("before\ndirty at dispatch\n")
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.on_read = lambda: (root / "module.py").write_text("changed after dispatch\n")

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "conflict"
        assert fake.packet["context"]["source_snapshots"][0]["content"] == (
            "before\ndirty at dispatch\n"
        )

    asyncio.run(exercise())
    assert "release" not in fake.events
    store.close()


def test_short_worker_token_stops_before_post_and_releases_verified_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.token_expiry = datetime.now(UTC) + timedelta(minutes=30)

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "failed"

    asyncio.run(exercise())
    assert "post" not in fake.events
    assert fake.events.index("revoke") < fake.events.index("release")
    store.close()


def test_expired_job_deadline_retains_lease_and_revokes_worker_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.deadline_past = True

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "uncertain"

    asyncio.run(exercise())
    assert fake.events.count("post") == 1
    assert "revoke" in fake.events
    assert "release" not in fake.events
    store.close()


def test_changed_artifact_at_pinned_head_is_rejected_on_result_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await submit(coordinator, root)
        assert await settled(store, submitted["job_id"]) == "ready"
        fake.artifact["summary"] = "changed after validation"
        with pytest.raises(CoordinatorError, match="failed validation"):
            await coordinator.result(submitted["job_id"])
        assert store.get(submitted["job_id"]).state == "conflict"

    asyncio.run(exercise())
    store.close()


def test_transient_pinned_result_read_error_keeps_ready_for_later_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await submit(coordinator, root)
        assert await settled(store, submitted["job_id"]) == "ready"
        fake.result_read_error = ControlError("synthetic GitHub failure")
        with pytest.raises(CoordinatorError, match="pinned result is unavailable"):
            await coordinator.result(submitted["job_id"])
        assert store.get(submitted["job_id"]).state == "ready"
        assert fake.events.count("revoke") == 2

    asyncio.run(exercise())
    store.close()


def test_cas_uncertainty_never_mints_or_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.lease_error = LeaseUncertain("synthetic CAS outcome")

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "uncertain"

    asyncio.run(exercise())
    assert fake.events == ["acquire"]
    store.close()


def test_vault_configuration_failure_is_journaled_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    config.vault_ca_file.unlink()

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "uncertain"

    asyncio.run(exercise())
    assert fake.events == []
    store.close()


def test_lease_renewal_uncertainty_keeps_claim_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.renew_immediately = True
    fake.renew_error = LeaseUncertain("synthetic renewal outcome")

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "uncertain"

    asyncio.run(exercise())
    assert fake.events.count("post") == 1
    assert "renew" in fake.events
    assert "release" not in fake.events
    store.close()


def test_removed_workspace_opt_in_blocks_ready_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    FakeServices(monkeypatch)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await submit(coordinator, root)
        assert await settled(store, submitted["job_id"]) == "ready"
        config.workspaces[root] = WorkspaceRule(False, frozenset({"worker-a"}))
        with pytest.raises(CoordinatorError, match="pinned result is unavailable"):
            await coordinator.result(submitted["job_id"])
        assert store.get(submitted["job_id"]).state == "conflict"

    asyncio.run(exercise())
    store.close()


def test_invalid_summary_blocks_release_before_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    def corrupt_summary() -> None:
        fake.artifact = dict(fake.artifact or {})
        fake.artifact["summary"] = ["invalid"]

    fake.on_read = corrupt_summary

    async def exercise() -> None:
        submitted = await submit(Coordinator(config, store, poll_seconds=0.01), root)
        assert await settled(store, submitted["job_id"]) == "conflict"

    asyncio.run(exercise())
    assert "release" not in fake.events
    store.close()


def test_workspace_disappearance_marks_ready_result_conflicted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    FakeServices(monkeypatch)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01)
        submitted = await submit(coordinator, root)
        assert await settled(store, submitted["job_id"]) == "ready"
        (root / "module.py").unlink()
        with pytest.raises(CoordinatorError, match="pinned result is unavailable"):
            await coordinator.result(submitted["job_id"])
        assert store.get(submitted["job_id"]).state == "conflict"

    asyncio.run(exercise())
    store.close()


def test_restart_after_post_marks_uncertain_without_retry_or_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_grokbot_mcp.server import create_server

    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.pr_present = False

    async def exercise() -> str:
        coordinator = Coordinator(config, store, poll_seconds=30)
        submitted = await submit(coordinator, root)
        for _ in range(200):
            if store.get(submitted["job_id"]).state == "dispatched":
                break
            await asyncio.sleep(0.025)
        else:
            raise AssertionError("job was not dispatched")
        assert fake.events.count("post") == 1
        coordinator._tasks[submitted["job_id"]].cancel()
        with pytest.raises(asyncio.CancelledError):
            await coordinator._tasks[submitted["job_id"]]
        return submitted["job_id"]

    job_id = asyncio.run(exercise())
    store.close()
    reopened = JobStore.open(config.job_database)
    create_server(config, reopened)
    assert reopened.get(job_id).state == "uncertain"
    assert fake.events.count("post") == 1
    assert "release" not in fake.events
    reopened.close()
