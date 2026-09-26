from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_coordinator import FakeServices, configuration, settled, workspace

from codex_grokbot_mcp.config import WorkerConfig
from codex_grokbot_mcp.control import WebhookUncertain
from codex_grokbot_mcp.coordinator import Coordinator
from codex_grokbot_mcp.jobs import JobStore

JOB_ID = "1234abcd-1234-4123-8123-123456789abc"


class FakeInbox:
    def __init__(self, payload: dict | None) -> None:
        self.events: list[str] = []
        self.payload = payload

    def register(self, job_id: str, kind: str, digest: str, deadline: datetime) -> None:
        self.events.append(f"register:{kind}")
        assert len(digest) == 64
        assert deadline > datetime.now(UTC)

    def fetch(self, job_id: str, kind: str) -> dict | None:
        self.events.append(f"fetch:{kind}")
        return self.payload


def _digest(body: dict) -> str:
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def test_callback_dispatch_does_not_mint_and_uncertain_webhook_is_not_retried(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    worker = config.workers["worker-a"]
    config.workers["worker-a"] = WorkerConfig(
        worker.worker_id,
        worker.webhook_secret_path,
        worker.app_secret_path,
        worker.lease_prefix,
        worker.lease_worker,
        frozenset({"x_query"}),
    )
    object.__setattr__(
        config,
        "result_inbox_base_url",
        "https://inbox.example.invalid",
    )
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.webhook_error = WebhookUncertain("lost response")
    inbox = FakeInbox(None)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="What is being discussed?",
            read_paths=[],
            write_paths=[],
            acceptance_checks=[],
            effort_hint="small",
            job_type="x_query",
        )
        assert await settled(store, submitted["job_id"]) == "uncertain"

    asyncio.run(exercise())
    assert "mint" not in fake.events
    assert fake.events.count("post") == 1
    assert "github_token" not in fake.packet["context"]
    assert inbox.events[0] == "register:result"
    assert fake.events.index("post") > fake.events.index("acquire")


def test_callback_answer_becomes_ready_without_a_pull_request(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    worker = config.workers["worker-a"]
    config.workers["worker-a"] = WorkerConfig(
        worker.worker_id,
        worker.webhook_secret_path,
        worker.app_secret_path,
        worker.lease_prefix,
        worker.lease_worker,
        frozenset({"x_query"}),
    )
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    body = {
        "schema_version": "v3",
        "job_type": "x_query",
        "job_id": "",
        "query": "What is being discussed?",
        "answer": {"top_themes": [{"theme": "Example"}]},
        "summary": "One line.",
        "sources": [],
        "read_only_attestation": True,
        "completed_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        "status": "ok",
        "error": None,
    }

    class AnswerInbox(FakeInbox):
        def fetch(self, job_id: str, kind: str) -> dict | None:
            body["job_id"] = job_id
            self.payload = {"stored": True, "body": body, "body_sha256": _digest(body)}
            return super().fetch(job_id, kind)

    inbox = AnswerInbox(None)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="What is being discussed?",
            read_paths=[],
            write_paths=[],
            acceptance_checks=[],
            effort_hint="small",
            job_type="x_query",
        )
        assert await settled(store, submitted["job_id"]) == "ready"
        result = await coordinator.result(submitted["job_id"])
        assert result["summary"] == "One line."

    asyncio.run(exercise())
    assert "mint" not in fake.events
    assert "release" in fake.events


def test_blocked_status_without_a_pull_request_does_not_become_ready(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.pr_present = False
    status = {
        "schema_version": "v3",
        "job_type": "coding",
        "job_id": JOB_ID,
        "status": "blocked",
        "pr_url": None,
        "summary": "The file set cannot express the change.",
        "completed_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    }

    class StatusInbox(FakeInbox):
        def fetch(self, job_id: str, kind: str) -> dict | None:
            status["job_id"] = job_id
            self.payload = {"stored": True, "body": status, "body_sha256": "ab" * 32}
            return super().fetch(job_id, kind)

    inbox = StatusInbox(None)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Add one line",
            read_paths=["module.py"],
            write_paths=["module.py"],
            acceptance_checks=["The line appears"],
            effort_hint="small",
        )
        assert await settled(store, submitted["job_id"]) == "failed"

    asyncio.run(exercise())
    assert "mint" in fake.events
    assert "release" in fake.events
    assert inbox.events[0] == "register:status"
