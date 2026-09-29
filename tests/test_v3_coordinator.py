from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from test_coordinator import FakeServices, configuration, settled, workspace

from codex_grokbot_mcp.config import WorkerConfig
from codex_grokbot_mcp.control import ControlError, WebhookUncertain, WebhookTransport
from codex_grokbot_mcp.coordinator import Coordinator, CoordinatorError
from codex_grokbot_mcp.jobs import JobStore
from codex_grokbot_mcp.vault import LeaseUncertain

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


def _configure_mapped_chief(config) -> None:
    chief = config.workers["worker-a"]
    config.workers["worker-a"] = WorkerConfig(
        chief.worker_id,
        chief.webhook_secret_path,
        chief.app_secret_path,
        "GrokBot/Leases",
        "chief-of-staff-supergrok",
        frozenset({"x_query", "worker_diagnostic"}),
        "account-a",
    )
    config.workers["coder"] = WorkerConfig(
        "coder",
        "webhooks/coder",
        "github/coder",
        "leases",
        "coder",
        frozenset({"coding"}),
        "account-a",
        "worker-a",
        "devcoder",
    )


def _digest(body: dict) -> str:
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()


def test_callback_dispatch_does_not_mint_and_uncertain_webhook_is_not_retried(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _configure_mapped_chief(config)
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
    assert fake.lease_workers == ["chief-of-staff-supergrok"]
    assert fake.released_workers == []


def test_callback_answer_becomes_ready_without_a_pull_request(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _configure_mapped_chief(config)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    body = {
        "schema_version": "v3",
        "job_type": "x_query",
        "job_id": "",
        "query": "What is being discussed?",
        "answer": "Topics:\n\n- First point\n- Second point",
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
        assert result["answer"] == "Topics:\n\n- First point\n- Second point"

    asyncio.run(exercise())
    assert "mint" not in fake.events
    assert "release" in fake.events
    assert fake.lease_workers == ["chief-of-staff-supergrok"]
    assert fake.released_workers == ["chief-of-staff-supergrok"]


def test_x_query_to_unmapped_worker_is_rejected_before_job_or_webhook(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    inbox = FakeInbox(None)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        with pytest.raises(CoordinatorError, match="Chief of Staff"):
            await coordinator.delegate(
                root=root,
                worker_id="worker-a",
                goal="What is being discussed?",
                read_paths=[],
                write_paths=[],
                acceptance_checks=[],
                effort_hint="small",
                job_type="x_query",
            )

    asyncio.run(exercise())
    assert fake.events == []
    assert inbox.events == []
    assert store.list_open() == ()


def test_invalid_x_query_callback_retains_exact_chief_lease(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _configure_mapped_chief(config)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)

    class InvalidInbox(FakeInbox):
        def fetch(self, job_id: str, kind: str) -> dict | None:
            self.events.append(f"fetch:{kind}")
            return {"stored": True, "body": {"schema_version": "v3"}, "body_sha256": "ab" * 32}

    inbox = InvalidInbox(None)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Read only research",
            read_paths=[],
            write_paths=[],
            acceptance_checks=[],
            effort_hint="small",
            job_type="x_query",
        )
        assert await settled(store, submitted["job_id"]) == "conflict"

    asyncio.run(exercise())
    assert fake.lease_workers == ["chief-of-staff-supergrok"]
    assert fake.released_workers == []


def test_uncertain_x_query_lease_acquisition_is_not_compensated(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _configure_mapped_chief(config)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    fake.lease_error = LeaseUncertain("CAS outcome could not be read back")
    inbox = FakeInbox(None)

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=inbox)
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Read only research",
            read_paths=[],
            write_paths=[],
            acceptance_checks=[],
            effort_hint="small",
            job_type="x_query",
        )
        assert await settled(store, submitted["job_id"]) == "uncertain"

    asyncio.run(exercise())
    assert fake.lease_workers == ["chief-of-staff-supergrok"]
    assert fake.released_workers == []
    assert "post" not in fake.events


def test_pre_post_x_query_control_error_releases_the_owned_chief_lease(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = workspace(tmp_path)
    config = configuration(tmp_path, root)
    _configure_mapped_chief(config)
    object.__setattr__(config, "result_inbox_base_url", "https://inbox.example.invalid")
    store = JobStore.open(config.job_database)
    fake = FakeServices(monkeypatch)
    monkeypatch.setattr(
        WebhookTransport,
        "dispatch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ControlError("request rejected locally")),
    )

    async def exercise() -> None:
        coordinator = Coordinator(config, store, poll_seconds=0.01, inbox=FakeInbox(None))
        submitted = await coordinator.delegate(
            root=root,
            worker_id="worker-a",
            goal="Read only research",
            read_paths=[],
            write_paths=[],
            acceptance_checks=[],
            effort_hint="small",
            job_type="x_query",
        )
        assert await settled(store, submitted["job_id"]) == "failed"

    asyncio.run(exercise())
    assert fake.lease_workers == ["chief-of-staff-supergrok"]
    assert fake.released_workers == ["chief-of-staff-supergrok"]
    assert "post" not in fake.events


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
