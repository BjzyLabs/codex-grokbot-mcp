from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_coordinator import configuration, workspace

from codex_grokbot_mcp import coordinator as coordinator_module
from codex_grokbot_mcp.config import WorkerConfig
from codex_grokbot_mcp.control import GitHubControl, VaultClient, WebhookTransport
from codex_grokbot_mcp.coordinator import Coordinator, CoordinatorError
from codex_grokbot_mcp.jobs import JobStore

TARGET_JOB_ID = "1234abcd-1234-4123-8123-123456789abc"


class FakeInbox:
    def __init__(self, callback: bool = True) -> None:
        self.payload = None
        self.callback = callback
        self.events: list[str] = []
        self.deadline = None

    def register(self, job_id, kind, digest, deadline) -> None:
        self.events.append("register")
        assert kind == "result"
        assert len(digest) == 64
        assert deadline > datetime.now(UTC)
        self.deadline = deadline

    def fetch(self, job_id, kind):
        self.events.append("fetch")
        return self.payload if self.callback else None


def _account_routing(config) -> None:
    target = config.workers["worker-a"]
    config.workers["worker-a"] = WorkerConfig(
        target.worker_id,
        target.webhook_secret_path,
        target.app_secret_path,
        target.lease_prefix,
        target.lease_worker,
        frozenset({"coding"}),
        "test-account",
        "chief",
        "devcoder",
    )
    config.workers["chief"] = WorkerConfig(
        "chief",
        "webhook/chief",
        "app/chief",
        "leases",
        "shared-chief",
        frozenset({"x_query", "worker_diagnostic"}),
        "test-account",
    )
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")


def _create_target(store: JobStore, root) -> None:
    from codex_grokbot_mcp.local import Workspace

    context = Workspace.open(root, opted_in=True).snapshot(
        read_paths=["module.py"], write_paths=["module.py"]
    )
    store.create(
        TARGET_JOB_ID,
        "worker-a",
        context,
        lease_owner="codex-grokbot-mcp",
        control_branch="grokbot/job-coding-1234abcd",
        artifact_path="artifacts/patch-1234abcd.json",
    )


def test_diagnostic_dispatches_without_coding_lease_or_github_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _account_routing(config)
    store = JobStore.open(config.job_database)
    _create_target(store, root)
    inbox = FakeInbox()
    events: list[str] = []
    body = {
        "schema_version": "v3",
        "job_type": "worker_diagnostic",
        "job_id": "",
        "target_job_id": TARGET_JOB_ID,
        "target_bot": "devcoder",
        "status": "replied",
        "reply": "Finished; draft PR 139 is open.",
        "completed_at": (datetime.now(UTC) - timedelta(seconds=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "read_only_attestation": True,
    }

    def read_secret(_vault, path):
        events.append(f"secret:{path}")
        assert path == "webhook/chief"
        return {"webhook_url": "https://chief.example.invalid/hook", "sender_key": "sender"}

    def dispatch(_webhook, packet):
        events.append("dispatch")
        assert packet["job_type"] == "worker_diagnostic"
        assert "callback_token" in packet["context"]
        assert packet["target_job_id"] == TARGET_JOB_ID
        body["job_id"] = packet["job_id"]
        encoded = json.dumps(body, separators=(",", ":")).encode()
        inbox.payload = {
            "stored": True,
            "body": body,
            "body_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    def forbidden(*_args, **_kwargs):
        pytest.fail("diagnostic must not acquire a coding lease or mint a GitHub token")

    monkeypatch.setattr(VaultClient, "read_secret", read_secret)
    monkeypatch.setattr(WebhookTransport, "dispatch", dispatch)
    monkeypatch.setattr(GitHubControl, "mint_worker_token", forbidden)

    async def exercise():
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.diagnose(TARGET_JOB_ID)
        for _ in range(100):
            result = store.get_diagnostic(submitted["job_id"])
            if result.state in {"replied", "uncertain", "delivery_failed"}:
                return submitted, result
            await asyncio.sleep(0.01)
        raise AssertionError("diagnostic did not settle")

    started_at = datetime.now(UTC)
    submitted, result = asyncio.run(exercise())

    assert result.state == "replied"
    assert result.reply == "Finished; draft PR 139 is open."
    assert "register" in inbox.events
    assert timedelta(seconds=205) < inbox.deadline - started_at < timedelta(seconds=212)
    assert events == ["secret:webhook/chief", "dispatch"]
    assert store.get(TARGET_JOB_ID).state == "queued"
    store.close()


def test_diagnostic_requires_configured_account_route(tmp_path) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    _create_target(store, root)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, inbox=FakeInbox())
        with pytest.raises(CoordinatorError, match="account routing"):
            await coordinator.diagnose(TARGET_JOB_ID)

    asyncio.run(exercise())
    store.close()


def test_missing_callback_is_uncertain_not_no_reply(tmp_path, monkeypatch) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _account_routing(config)
    store = JobStore.open(config.job_database)
    _create_target(store, root)
    inbox = FakeInbox(callback=False)
    monkeypatch.setattr(coordinator_module, "DIAGNOSTIC_MAX_WAIT_SECONDS", 0)
    monkeypatch.setattr(coordinator_module, "DIAGNOSTIC_CALLBACK_GRACE_SECONDS", 0.05)
    monkeypatch.setattr(
        VaultClient,
        "read_secret",
        lambda _vault, _path: {
            "webhook_url": "https://chief.example.invalid/hook",
            "sender_key": "sender",
        },
    )
    monkeypatch.setattr(WebhookTransport, "dispatch", lambda _webhook, _packet: None)

    async def exercise():
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.diagnose(TARGET_JOB_ID)
        for _ in range(100):
            record = store.get_diagnostic(submitted["job_id"])
            if record.state in {"uncertain", "no_reply"}:
                return record
            await asyncio.sleep(0.01)
        raise AssertionError("diagnostic callback timeout did not settle")

    result = asyncio.run(exercise())
    assert result.state == "uncertain"
    assert result.callback_body_sha256 is None
    store.close()


def test_unexpected_pre_dispatch_failure_leaves_diagnostic_uncertain(tmp_path, monkeypatch) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _account_routing(config)
    store = JobStore.open(config.job_database)
    _create_target(store, root)

    async def fail_before_dispatch(*_args, **_kwargs):
        raise RuntimeError("synthetic pre-dispatch failure")

    monkeypatch.setattr(Coordinator, "_execute_diagnostic", fail_before_dispatch)

    async def exercise():
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=FakeInbox())
        submitted = await coordinator.diagnose(TARGET_JOB_ID)
        for _ in range(100):
            result = store.get_diagnostic(submitted["job_id"])
            if result.state == "uncertain":
                return result
            await asyncio.sleep(0.01)
        raise AssertionError("unexpected diagnostic failure did not settle")

    result = asyncio.run(exercise())
    assert result.state == "uncertain"
    assert result.callback_body_sha256 is None
    store.close()
