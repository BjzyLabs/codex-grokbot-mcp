from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from support import (
    CALLBACK_TOKEN,
    INBOX_ORIGIN,
    JOB_ID,
    OTHER_JOB_ID,
    SENDER_KEY,
    WEBHOOK_URL,
    LocalInbox,
    RecordingTransport,
    deadline,
    make_config,
    ok_body,
    wait_until,
)

import codex_grokbot_mcp.coordinator as coordinator_module
from codex_grokbot_mcp.coordinator import Coordinator, CoordinatorError
from codex_grokbot_mcp.deliver import callback_url
from codex_grokbot_mcp.inbox import InboxError, token_hash
from codex_grokbot_mcp.jobs import JobStore
from codex_grokbot_mcp.webhook import WebhookUncertain


class BrokenInbox(LocalInbox):
    """Fails before any webhook POST can be attempted."""

    def register(self, job_id: str, kind: str, digest: str, deadline: datetime) -> None:
        raise InboxError("callback expectation was not registered")


class ChangedInbox(LocalInbox):
    """Returns a differently digested body than the one that was validated."""

    def fetch(self, job_id: str, kind: str) -> dict | None:
        payload = super().fetch(job_id, kind)
        if payload and payload.get("stored"):
            return {**payload, "body_sha256": "f" * 64}
        return payload


class FlakyInbox(LocalInbox):
    """Fails the first fetch, then behaves normally."""

    def __init__(self) -> None:
        super().__init__()
        self.failures = 1

    def fetch(self, job_id: str, kind: str) -> dict | None:
        if self.failures > 0:
            self.failures -= 1
            self.events.append(f"fetch-error:{kind}")
            raise InboxError("temporary inbox failure")
        return super().fetch(job_id, kind)


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        private = self.root / "private"
        private.mkdir(mode=0o700)
        self.store = JobStore.open(private / "jobs.sqlite3")
        self.addCleanup(self.store.close)
        self.inbox = LocalInbox()
        self.config = make_config(self.root)

    def coordinator(self, *, poll_seconds: float = 0.01) -> Coordinator:
        return Coordinator(self.config, self.store, inbox=self.inbox, poll_seconds=poll_seconds)

    def state(self, job_id: str) -> str | None:
        record = self.store.get(job_id)
        return None if record is None else record.state

    def run_scenario(self, transport, scenario, *, job_deadline=None):
        patches = [mock.patch.object(coordinator_module, "WebhookTransport", new=transport.factory)]
        if job_deadline is not None:
            patches.append(mock.patch.object(coordinator_module, "JOB_DEADLINE", job_deadline))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return asyncio.run(scenario())

    def test_x_query_round_trip_becomes_ready(self) -> None:
        transport = RecordingTransport(
            self.inbox, responder=lambda packet: ok_body(packet["job_id"], "x_query")
        )

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("x_query", "  What is X saying about Gemini 4?  ")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "ready")
            result = await coordinator.result(job_id)
            await coordinator.shutdown()
            return started, result

        started, result = self.run_scenario(transport, scenario)

        self.assertEqual(started["state"], "queued")
        self.assertEqual(len(started["job_id"]), 36)
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["answer"], "A short bounded answer.")
        self.assertEqual(result["summary"], "One line.")
        self.assertEqual(result["sources"], ["https://example.invalid/post/1"])
        packet = transport.packet
        self.assertEqual(packet["job_id"], started["job_id"])
        self.assertEqual(packet["job_type"], "x_query")
        self.assertEqual(packet["deliver"], "callback")
        self.assertEqual(packet["goal"], "What is X saying about Gemini 4?")
        self.assertEqual(packet["schema_version"], "v3")
        self.assertEqual(
            packet["context"]["callback_url"],
            callback_url(INBOX_ORIGIN, started["job_id"], "result"),
        )
        self.assertEqual(transport.url, WEBHOOK_URL)
        self.assertEqual(transport.sender_key, SENDER_KEY)
        self.assertEqual(self.inbox.events.count("register:result"), 1)

    def test_ask_round_trip_uses_the_ask_job_type(self) -> None:
        transport = RecordingTransport(
            self.inbox, responder=lambda packet: ok_body(packet["job_id"], "ask", sources=[])
        )

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "Summarise the release notes.")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "ready")
            result = await coordinator.result(job_id)
            record = self.store.get(job_id)
            await coordinator.shutdown()
            return result, record

        result, record = self.run_scenario(transport, scenario)

        self.assertEqual(transport.packet["job_type"], "ask")
        self.assertEqual(result["sources"], [])
        self.assertEqual(self.state(transport.packet["job_id"]), "ready")
        assert record is not None
        self.assertEqual(record.job_type, "ask")
        self.assertEqual(record.body_sha256 is not None, True)

    def test_transient_inbox_error_is_retried_until_the_answer_arrives(self) -> None:
        self.inbox = FlakyInbox()
        transport = RecordingTransport(
            self.inbox, responder=lambda packet: ok_body(packet["job_id"], "ask")
        )

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "ready")
            result = await coordinator.result(job_id)
            await coordinator.shutdown()
            return result

        result = self.run_scenario(transport, scenario)

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["answer"], "A short bounded answer.")
        self.assertIn("fetch-error:result", self.inbox.events)

    def test_error_callback_marks_the_request_failed(self) -> None:
        transport = RecordingTransport(
            self.inbox,
            responder=lambda packet: {
                **ok_body(packet["job_id"], "ask"),
                "answer": None,
                "sources": [],
                "status": "error",
                "error": {"code": "x_auth_required", "message": "Reconnect X and retry."},
            },
        )

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "failed")
            result = await coordinator.result(job_id)
            await coordinator.shutdown()
            return result

        result = self.run_scenario(transport, scenario)

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "x_auth_required")
        self.assertEqual(result["error_message"], "Reconnect X and retry.")
        self.assertNotIn("answer", result)

    def test_invalid_callback_body_becomes_conflict(self) -> None:
        transport = RecordingTransport(
            self.inbox,
            responder=lambda packet: ok_body(packet["job_id"], "ask", summary="token=abc"),
        )

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "conflict")
            result = await coordinator.result(job_id)
            await coordinator.shutdown()
            return result

        result = self.run_scenario(transport, scenario)

        self.assertEqual(result, {"job_id": transport.packet["job_id"], "state": "conflict"})

    def test_ambiguous_dispatch_is_uncertain_and_never_retried(self) -> None:
        transport = RecordingTransport(self.inbox, error=WebhookUncertain("lost response"))

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("x_query", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "uncertain")
            await coordinator.shutdown()
            return job_id

        job_id = self.run_scenario(transport, scenario)

        self.assertEqual(self.state(job_id), "uncertain")
        self.assertEqual(len(transport.packets), 1)
        self.assertEqual([record.job_id for record in self.store.list_open()], [job_id])

    def test_failure_before_the_post_is_failed_not_uncertain(self) -> None:
        self.inbox = BrokenInbox()
        transport = RecordingTransport(self.inbox)

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "failed")
            await coordinator.shutdown()
            return job_id

        job_id = self.run_scenario(transport, scenario)

        self.assertEqual(self.state(job_id), "failed")
        self.assertEqual(transport.packets, [])
        record = self.store.get(job_id)
        assert record is not None
        self.assertIsNone(record.body_sha256)
        self.assertIsNone(record.error_code)

    def test_no_callback_becomes_uncertain_at_the_deadline(self) -> None:
        transport = RecordingTransport(self.inbox)

        async def scenario():
            coordinator = self.coordinator(poll_seconds=0.005)
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "uncertain")
            await coordinator.shutdown()
            return job_id

        job_id = self.run_scenario(transport, scenario, job_deadline=timedelta(milliseconds=60))

        self.assertEqual(self.state(job_id), "uncertain")
        self.assertEqual(len(transport.packets), 1)

    def test_restart_resumes_a_dispatched_request(self) -> None:
        transport = RecordingTransport(self.inbox)
        self.store.create_request(JOB_ID, "ask", deadline())
        self.inbox.inbox.register(JOB_ID, "result", token_hash(CALLBACK_TOKEN), deadline())
        self.store.advance(JOB_ID, "dispatching")
        self.store.advance(JOB_ID, "dispatched")
        self.assertEqual(
            self.inbox.post(
                {"job_id": JOB_ID, "context": {"callback_token": CALLBACK_TOKEN}},
                ok_body(JOB_ID, "ask"),
            ),
            200,
        )

        async def scenario():
            coordinator = self.coordinator()
            resumed = await coordinator.resume_dispatched()
            await wait_until(lambda: self.state(JOB_ID) == "ready")
            result = await coordinator.result(JOB_ID)
            await coordinator.shutdown()
            return resumed, result

        resumed, result = self.run_scenario(transport, scenario)

        self.assertEqual(resumed, (JOB_ID,))
        self.assertEqual(result["state"], "ready")
        self.assertEqual(transport.packets, [])

    def test_resume_marks_interrupted_pre_dispatch_requests_uncertain(self) -> None:
        transport = RecordingTransport(self.inbox)
        self.store.create_request(JOB_ID, "ask", deadline())

        async def scenario():
            coordinator = self.coordinator()
            resumed = await coordinator.resume_dispatched()
            await coordinator.shutdown()
            return resumed

        resumed = self.run_scenario(transport, scenario)

        self.assertEqual(resumed, ())
        self.assertEqual(self.state(JOB_ID), "uncertain")

    def test_result_detects_a_changed_callback_body(self) -> None:
        transport = RecordingTransport(
            self.inbox, responder=lambda packet: ok_body(packet["job_id"], "ask")
        )

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "ready")
            coordinator.inbox = ChangedInbox()
            with self.assertRaises(CoordinatorError):
                await coordinator.result(job_id)
            await coordinator.shutdown()
            return job_id

        job_id = self.run_scenario(transport, scenario)

        self.assertEqual(self.state(job_id), "conflict")

    def test_result_reports_state_only_until_terminal(self) -> None:
        self.store.create_request(JOB_ID, "ask", deadline())

        async def scenario():
            coordinator = self.coordinator()
            queued = await coordinator.result(JOB_ID)
            missing = await coordinator.result(OTHER_JOB_ID)
            await coordinator.shutdown()
            return queued, missing

        queued, missing = self.run_scenario(RecordingTransport(self.inbox), scenario)

        self.assertEqual(queued, {"job_id": JOB_ID, "state": "queued"})
        self.assertEqual(missing, {"job_id": OTHER_JOB_ID, "state": "not_found"})

    def test_concurrent_requests_each_keep_their_own_answer(self) -> None:
        transport = RecordingTransport(
            self.inbox,
            responder=lambda packet: ok_body(
                packet["job_id"], packet["job_type"], answer=f"answer for {packet['job_id']}"
            ),
        )

        async def scenario():
            coordinator = self.coordinator()
            first = await coordinator.start("x_query", "First question.")
            second = await coordinator.start("ask", "Second question.")
            ids = [first["job_id"], second["job_id"]]
            await wait_until(lambda: all(self.state(job_id) == "ready" for job_id in ids))
            results = [await coordinator.result(job_id) for job_id in ids]
            await coordinator.shutdown()
            return ids, results

        ids, results = self.run_scenario(transport, scenario)

        self.assertEqual(len(set(ids)), 2)
        for job_id, result in zip(ids, results, strict=True):
            self.assertEqual(result["state"], "ready")
            self.assertEqual(result["answer"], f"answer for {job_id}")

    def test_invalid_prompt_is_rejected_before_journaling(self) -> None:
        transport = RecordingTransport(self.inbox)

        async def scenario():
            coordinator = self.coordinator()
            rejected = (("x_query", "   "), ("coding", "anything"), ("ask", "x" * 2001))
            for job_type, prompt in rejected:
                with self.assertRaises(CoordinatorError):
                    await coordinator.start(job_type, prompt)
            await coordinator.shutdown()
            return self.store.list_open()

        self.assertEqual(self.run_scenario(transport, scenario), ())
        self.assertEqual(transport.packets, [])

    def test_only_the_issued_callback_token_is_accepted(self) -> None:
        transport = RecordingTransport(self.inbox)

        async def scenario():
            coordinator = self.coordinator()
            started = await coordinator.start("ask", "What changed today?")
            job_id = started["job_id"]
            await wait_until(lambda: self.state(job_id) == "dispatched")
            now = datetime.now(UTC)
            foreign = self.inbox.inbox.submit(
                job_id, "result", f"Bearer {'z' * 43}", b"{}", now=now
            )
            issued = self.inbox.inbox.submit(
                job_id,
                "result",
                f"Bearer {transport.callback_token}",
                json.dumps(ok_body(job_id, "ask")).encode("utf-8"),
                now=now,
            )
            await wait_until(lambda: self.state(job_id) == "ready")
            await coordinator.shutdown()
            return foreign, issued

        foreign, issued = self.run_scenario(transport, scenario)

        self.assertEqual(foreign, 401)
        self.assertEqual(issued, 200)


if __name__ == "__main__":
    unittest.main()
