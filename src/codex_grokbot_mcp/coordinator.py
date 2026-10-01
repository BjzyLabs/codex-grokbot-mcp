"""Webhook-only request coordinator for the configured Grok Bot Chief of Staff.

One request becomes one webhook POST plus one callback. Concurrency is safe
because every request is keyed by its own job ID and the inbox stores at most
one body per job: an identical replay is accepted and a different second body
is rejected. No lease, repository, or file transfer is involved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from codex_grokbot_mcp.config import Config
from codex_grokbot_mcp.deliver import (
    PacketError,
    build_ask_packet,
    build_x_query_packet,
    clean_goal,
    validate_result,
)
from codex_grokbot_mcp.inbox import InboxError, token_hash
from codex_grokbot_mcp.jobs import JobRecord, JobStateError, JobStore
from codex_grokbot_mcp.webhook import WebhookError, WebhookTransport, WebhookUncertain

LOGGER = logging.getLogger(__name__)
JOB_DEADLINE = timedelta(minutes=45)
CALLBACK_TOKEN_BYTES = 32
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_BUILDERS = {"x_query": build_x_query_packet, "ask": build_ask_packet}


class CoordinatorError(ValueError):
    """A local request is unsafe or its result cannot be trusted."""


class Coordinator:
    """Post one request, then wait for its single callback."""

    def __init__(
        self,
        config: Config,
        store: JobStore,
        *,
        inbox: Any,
        poll_seconds: float = 10.0,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll interval must be positive")
        self.config = config
        self.store = store
        self.inbox = inbox
        self.poll_seconds = poll_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self, job_type: str, prompt: str) -> dict[str, str]:
        """Journal one request and return before the webhook call completes."""
        if job_type not in _BUILDERS:
            raise CoordinatorError("job type is invalid")
        try:
            goal = clean_goal(prompt)
        except PacketError as error:
            raise CoordinatorError("prompt is empty or exceeds its bound") from error
        job_id = str(uuid4())
        deadline = datetime.now(UTC) + JOB_DEADLINE
        try:
            self.store.create_request(job_id, job_type, deadline)
        except JobStateError as error:
            raise CoordinatorError("job could not be recorded before dispatch") from error
        self._spawn(job_id, self._dispatch(job_id, job_type, goal), prefix="request")
        return {"job_id": job_id, "state": "queued"}

    async def resume_dispatched(self) -> tuple[str, ...]:
        """Resume polling for requests that were posted before a restart."""
        resumed = self.store.reconcile_restart()
        for job_id in resumed:
            self._spawn(job_id, self._poll(job_id), prefix="resume")
        return resumed

    async def shutdown(self) -> None:
        """Cancel outstanding work so a closed session leaves no pending tasks."""
        pending = list(self._tasks.values())
        self._tasks.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def result(self, job_id: str) -> dict[str, Any]:
        """Return state only until the request is complete and revalidated."""
        try:
            record = self.store.get(job_id)
        except JobStateError as error:
            raise CoordinatorError("job status is unavailable") from error
        if record is None:
            return {"job_id": job_id, "state": "not_found"}
        if record.state == "ready":
            return await self._ready_result(record)
        if record.state == "failed":
            return {
                "job_id": record.job_id,
                "state": "failed",
                "error_code": record.error_code,
                "error_message": record.error_message,
            }
        return {"job_id": record.job_id, "state": record.state}

    def _spawn(self, job_id: str, work: Awaitable[None], *, prefix: str) -> None:
        if job_id in self._tasks:
            close = getattr(work, "close", None)
            if close is not None:
                close()
            return
        task = asyncio.create_task(work, name=f"grokbot-{prefix}-{job_id[:8]}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda _completed: self._tasks.pop(job_id, None))

    async def _dispatch(self, job_id: str, job_type: str, goal: str) -> None:
        try:
            await self._execute_dispatch(job_id, job_type, goal)
        except Exception:
            self._settle_state(job_id, "uncertain")
            LOGGER.error("request %s stopped; its outcome is uncertain", job_id[:8])

    async def _execute_dispatch(self, job_id: str, job_type: str, goal: str) -> None:
        record = self.store.get(job_id)
        if record is None or record.state != "queued":
            raise JobStateError("job record is not queued")
        callback_token = secrets.token_urlsafe(CALLBACK_TOKEN_BYTES)
        try:
            await asyncio.to_thread(
                self.inbox.register,
                job_id,
                "result",
                token_hash(callback_token),
                record.deadline_at,
            )
            packet = _BUILDERS[job_type](
                job_id=job_id,
                goal=goal,
                origin=self.config.inbox_base_url,
                callback_token=callback_token,
            )
            transport = WebhookTransport(self.config.webhook_url, self.config.sender_key)
            self.store.advance(job_id, "dispatching")
        except (InboxError, PacketError, WebhookError, JobStateError) as error:
            LOGGER.warning("request %s failed before dispatch: %s", job_id[:8], error)
            self._settle_state(job_id, "failed")
            return
        try:
            await asyncio.to_thread(transport.dispatch, packet)
        except WebhookUncertain as error:
            LOGGER.warning("request %s webhook outcome uncertain: %s", job_id[:8], error)
            self._settle_state(job_id, "uncertain")
            return
        except WebhookError:
            self._settle_state(job_id, "failed")
            return
        try:
            self.store.advance(job_id, "dispatched")
        except JobStateError:
            self._settle_state(job_id, "uncertain")
            return
        await self._poll(job_id)

    async def _poll(self, job_id: str) -> None:
        try:
            await self._execute_poll(job_id)
        except Exception:
            self._settle_state(job_id, "uncertain")
            LOGGER.error("request %s stopped while waiting; outcome uncertain", job_id[:8])

    async def _execute_poll(self, job_id: str) -> None:
        record = self.store.get(job_id)
        if record is None or record.state != "dispatched":
            return
        deadline = record.deadline_at
        while True:
            if datetime.now(UTC) >= deadline:
                self._settle_state(job_id, "uncertain")
                return
            try:
                payload = await asyncio.to_thread(self.inbox.fetch, job_id, "result")
            except InboxError:
                # Fetching is idempotent: retry a transient inbox failure until the
                # deadline instead of stranding a posted request as uncertain.
                await asyncio.sleep(self.poll_seconds)
                continue
            if payload and payload.get("stored"):
                self._settle(record, payload)
                return
            await asyncio.sleep(self.poll_seconds)

    def _settle(self, record: JobRecord, payload: dict) -> None:
        """Apply one stored callback body to the journal, failing closed."""
        body = payload.get("body")
        digest = payload.get("body_sha256")
        if not isinstance(digest, str) or not _DIGEST_PATTERN.fullmatch(digest):
            self._settle_state(record.job_id, "uncertain")
            return
        try:
            validated = validate_result(record.job_id, record.job_type, body, now=datetime.now(UTC))
        except (PacketError, TypeError):
            self._settle_state(record.job_id, "conflict")
            return
        try:
            if validated["status"] == "ok":
                self.store.record_result(
                    record.job_id,
                    digest,
                    validated["summary"],
                    validated["answer"],
                    validated["sources"],
                )
                self.store.advance(record.job_id, "ready")
            else:
                error = validated["error"]
                self.store.record_error(record.job_id, error["code"], error["message"])
                self.store.advance(record.job_id, "failed")
        except JobStateError:
            self._settle_state(record.job_id, "uncertain")

    async def _ready_result(self, record: JobRecord) -> dict[str, Any]:
        try:
            payload = await asyncio.to_thread(self.inbox.fetch, record.job_id, "result")
        except InboxError as error:
            raise CoordinatorError("callback result is unavailable") from error
        if (
            not payload
            or not payload.get("stored")
            or payload.get("body_sha256") != record.body_sha256
        ):
            self._settle_state(record.job_id, "conflict")
            raise CoordinatorError("callback result changed since it was validated")
        try:
            body = validate_result(
                record.job_id, record.job_type, payload.get("body"), now=datetime.now(UTC)
            )
        except (PacketError, TypeError) as error:
            self._settle_state(record.job_id, "conflict")
            raise CoordinatorError("callback result failed validation") from error
        if body["status"] != "ok":
            self._settle_state(record.job_id, "conflict")
            raise CoordinatorError("callback result is not a completed answer")
        answer = body["answer"]
        if not isinstance(answer, str):
            answer = json.dumps(answer, separators=(",", ":"), sort_keys=True)
        return {
            "job_id": record.job_id,
            "state": "ready",
            "summary": body["summary"],
            "answer": answer,
            "sources": list(body["sources"]),
        }

    def _settle_state(self, job_id: str, state: str) -> None:
        try:
            self.store.advance(job_id, state)
        except JobStateError:
            LOGGER.debug("request %s cannot move to %s", job_id[:8], state)
