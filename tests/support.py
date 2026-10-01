"""Shared helpers for the webhook-only requestor tests.

Both `python -m pytest` and `python -m unittest discover -s tests` collect these
files, so tests stay plain unittest classes with no runner-specific fixtures.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from codex_grokbot_mcp.config import Config
from codex_grokbot_mcp.inbox import Inbox

INBOX_ORIGIN = "https://inbox.example.invalid"
WEBHOOK_URL = "https://webhook.example.invalid/route"
REQUESTOR_BEARER = "requestor-fixture-token"
SENDER_KEY = "sender-fixture-key"
CALLBACK_TOKEN = "c" * 43
JOB_ID = "1234abcd-1234-4123-8123-123456789abc"
OTHER_JOB_ID = "ffffffff-ffff-4fff-8fff-ffffffffffff"
NOW = datetime(2026, 9, 26, 1, tzinfo=UTC)
COMPLETED_AT = "2026-09-26T00:00:30Z"


def make_config(root: Path, **overrides: Any) -> Config:
    """Build a Config object without touching the filesystem."""
    values: dict[str, Any] = {
        "job_database": root / "private" / "jobs.sqlite3",
        "inbox_base_url": INBOX_ORIGIN,
        "webhook_url": WEBHOOK_URL,
        "inbox_requestor_token": REQUESTOR_BEARER,
        "sender_key": SENDER_KEY,
    }
    values.update(overrides)
    return Config(**values)


def config_text(root: Path, **overrides: str) -> str:
    """Render one flat version 2 TOML document for a temporary directory."""
    values = {
        "version": "2",
        "job_database": json.dumps(str(root / "private" / "jobs.sqlite3")),
        "inbox_base_url": json.dumps(INBOX_ORIGIN),
        "inbox_requestor_token": json.dumps(REQUESTOR_BEARER),
        "webhook_url": json.dumps(WEBHOOK_URL),
        "sender_key": json.dumps(SENDER_KEY),
    }
    values.update(overrides)
    return "".join(f"{key} = {value}\n" for key, value in values.items())


def write_config_file(root: Path, text: str, *, mode: int = 0o600) -> Path:
    path = root / "config.toml"
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)
    return path


def ok_body(job_id: str = JOB_ID, job_type: str = "x_query", **overrides: Any) -> dict:
    body: dict[str, Any] = {
        "schema_version": "v3",
        "job_type": job_type,
        "job_id": job_id,
        "query": "What is being discussed?",
        "answer": "A short bounded answer.",
        "summary": "One line.",
        "sources": ["https://example.invalid/post/1"],
        "read_only_attestation": True,
        "completed_at": COMPLETED_AT,
        "status": "ok",
        "error": None,
    }
    body.update(overrides)
    return body


def error_body(job_id: str = JOB_ID, job_type: str = "x_query", **overrides: Any) -> dict:
    body: dict[str, Any] = {
        "schema_version": "v3",
        "job_type": job_type,
        "job_id": job_id,
        "query": "What is being discussed?",
        "answer": None,
        "summary": "The upstream read failed.",
        "sources": [],
        "read_only_attestation": True,
        "completed_at": COMPLETED_AT,
        "status": "error",
        "error": {"code": "source_unavailable", "message": "The upstream read failed."},
    }
    body.update(overrides)
    return body


class LocalInbox:
    """In-process adapter with the same contract as the HTTPS inbox client."""

    def __init__(self) -> None:
        self.inbox = Inbox(REQUESTOR_BEARER)
        self.events: list[str] = []

    def register(self, job_id: str, kind: str, digest: str, deadline: datetime) -> None:
        self.events.append(f"register:{kind}")
        self.inbox.register(job_id, kind, digest, deadline)

    def fetch(self, job_id: str, kind: str) -> dict | None:
        self.events.append(f"fetch:{kind}")
        return self.inbox.read(job_id, kind)

    def post(self, packet: dict, body: dict) -> int:
        """Store one callback body exactly as the worker would post it."""
        raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
        token = packet["context"]["callback_token"]
        return self.inbox.submit(
            packet["job_id"], "result", f"Bearer {token}", raw, now=datetime.now(UTC)
        )


class RecordingTransport:
    """Stands in for the webhook: records one packet and optionally replies."""

    def __init__(
        self,
        inbox: LocalInbox | None = None,
        responder: Callable[[dict], dict | None] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.inbox = inbox
        self.responder = responder
        self.error = error
        self.packets: list[dict] = []
        self.url = ""
        self.sender_key = ""

    def factory(self, url: str, sender_key: str) -> RecordingTransport:
        self.url = url
        self.sender_key = sender_key
        return self

    def dispatch(self, packet: dict) -> None:
        self.packets.append(packet)
        if self.error is not None:
            raise self.error
        if self.inbox is None or self.responder is None:
            return
        body = self.responder(packet)
        if body is None:
            return
        status = self.inbox.post(packet, body)
        if status != 200:
            raise AssertionError(f"callback body was not stored: HTTP {status}")

    @property
    def packet(self) -> dict:
        return self.packets[-1]

    @property
    def callback_token(self) -> str:
        return str(self.packet["context"]["callback_token"])


async def wait_until(
    predicate: Callable[[], bool], *, attempts: int = 500, interval: float = 0.01
) -> None:
    """Let the coordinator's background task reach an observable state."""
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("expected state was not reached in time")


def deadline(minutes: int = 30) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)
