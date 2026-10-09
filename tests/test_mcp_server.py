from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from mcp import Client, StdioServerParameters
from support import (
    CALLBACK_TOKEN,
    JOB_ID,
    OTHER_JOB_ID,
    LocalInbox,
    RecordingTransport,
    config_text,
    deadline,
    make_config,
    ok_body,
    wait_until,
    write_config_file,
)

import codex_grokbot_mcp.coordinator as coordinator_module
from codex_grokbot_mcp.inbox import token_hash
from codex_grokbot_mcp.jobs import JobStore
from codex_grokbot_mcp.server import create_server

TOOL_NAMES = {
    "grokbot_x_query",
    "grokbot_ask",
    "grokbot_status",
    "grokbot_result",
    "grokbot_active",
}
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"


class ServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        private = self.root / "private"
        private.mkdir(mode=0o700)
        self.database = private / "jobs.sqlite3"
        self.store = JobStore.open(self.database)
        self.addCleanup(self.store.close)
        self.inbox = LocalInbox()
        self.config = make_config(self.root)

    def state(self, job_id: str) -> str | None:
        record = self.store.get(job_id)
        return None if record is None else record.state

    @contextlib.contextmanager
    def transport(self, recorder):
        with mock.patch.object(coordinator_module, "WebhookTransport", new=recorder.factory):
            yield recorder

    def test_tool_surface_is_exactly_the_five_tools(self) -> None:
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                return {tool.name for tool in (await client.list_tools()).tools}

        self.assertEqual(asyncio.run(scenario()), TOOL_NAMES)

    def test_client_is_told_it_is_talking_to_x_bot(self) -> None:
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                tools = (await client.list_tools()).tools
                return client.instructions, {tool.name: tool.description for tool in tools}

        instructions, descriptions = asyncio.run(scenario())
        self.assertIn("X Bot", instructions)
        self.assertNotIn("Chief of Staff", instructions)
        for name in ("grokbot_x_query", "grokbot_ask"):
            self.assertIn("X Bot", descriptions[name])
            self.assertNotIn("Chief of Staff", descriptions[name])

    def test_x_query_round_trip_through_the_tools(self) -> None:
        recorder = RecordingTransport(
            self.inbox, responder=lambda packet: ok_body(packet["job_id"], "x_query")
        )
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                started = (
                    await client.call_tool(
                        "grokbot_x_query", {"query": "What are people saying about Gemini 4?"}
                    )
                ).structured_content
                job_id = started["job_id"]
                await wait_until(lambda: self.state(job_id) == "ready")
                status = (
                    await client.call_tool("grokbot_status", {"job_id": job_id})
                ).structured_content
                result = (
                    await client.call_tool("grokbot_result", {"job_id": job_id})
                ).structured_content
                active = (await client.call_tool("grokbot_active")).structured_content
                return started, status, result, active

        with self.transport(recorder) as transport:
            started, status, result, active = asyncio.run(scenario())

        self.assertEqual(started, {"job_id": started["job_id"], "state": "queued"})
        self.assertEqual(status["state"], "ready")
        self.assertEqual(status["job_type"], "x_query")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["answer"], "A short bounded answer.")
        self.assertEqual(result["summary"], "One line.")
        self.assertEqual(result["sources"], ["https://example.invalid/post/1"])
        self.assertEqual(active, {"jobs": []})
        self.assertEqual(transport.packet["job_type"], "x_query")

    def test_ask_tool_round_trip(self) -> None:
        recorder = RecordingTransport(
            self.inbox, responder=lambda packet: ok_body(packet["job_id"], "ask", sources=[])
        )
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                started = (
                    await client.call_tool("grokbot_ask", {"question": "Summarise the notes."})
                ).structured_content
                job_id = started["job_id"]
                await wait_until(lambda: self.state(job_id) == "ready")
                result = (
                    await client.call_tool("grokbot_result", {"job_id": job_id})
                ).structured_content
                return started, result

        with self.transport(recorder) as transport:
            started, result = asyncio.run(scenario())

        self.assertEqual(started["state"], "queued")
        self.assertEqual(transport.packet["job_type"], "ask")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["sources"], [])

    def test_status_and_result_for_an_unknown_job(self) -> None:
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                status = (
                    await client.call_tool("grokbot_status", {"job_id": OTHER_JOB_ID})
                ).structured_content
                result = (
                    await client.call_tool("grokbot_result", {"job_id": OTHER_JOB_ID})
                ).structured_content
                unusable = (
                    await client.call_tool("grokbot_status", {"job_id": "not-a-uuid"})
                ).structured_content
                return status, result, unusable

        status, result, unusable = asyncio.run(scenario())

        self.assertEqual(status, {"job_id": OTHER_JOB_ID, "state": "not_found"})
        self.assertEqual(result, {"job_id": OTHER_JOB_ID, "state": "not_found"})
        self.assertEqual(unusable, {"job_id": "not-a-uuid", "state": "not_found"})

    def test_active_lists_open_requests_only(self) -> None:
        self.store.create_request(JOB_ID, "ask", deadline())
        self.store.create_request(OTHER_JOB_ID, "x_query", deadline())
        self.store.advance(OTHER_JOB_ID, "failed")
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                return (await client.call_tool("grokbot_active")).structured_content

        active = asyncio.run(scenario())

        self.assertEqual(
            active, {"jobs": [{"job_id": JOB_ID, "job_type": "ask", "state": "uncertain"}]}
        )

    def test_invalid_prompt_returns_a_tool_error_without_journaling(self) -> None:
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server, raise_exceptions=True) as client:
                return await client.call_tool("grokbot_x_query", {"query": "   "})

        result = asyncio.run(scenario())

        self.assertTrue(result.is_error)
        self.assertEqual(self.store.list_open(), ())

    def test_session_start_resumes_a_dispatched_request(self) -> None:
        self.store.create_request(JOB_ID, "ask", deadline())
        self.inbox.inbox.register(JOB_ID, "result", token_hash(CALLBACK_TOKEN), deadline())
        self.store.advance(JOB_ID, "dispatching")
        self.store.advance(JOB_ID, "dispatched")
        self.inbox.post(
            {"job_id": JOB_ID, "context": {"callback_token": CALLBACK_TOKEN}},
            ok_body(JOB_ID, "ask"),
        )
        self.store.create_request(OTHER_JOB_ID, "x_query", deadline())
        server = create_server(self.config, self.store, inbox=self.inbox)

        async def scenario():
            async with Client(server) as client:
                await wait_until(
                    lambda: (
                        self.state(JOB_ID) == "ready" and self.state(OTHER_JOB_ID) == "uncertain"
                    )
                )
                return (
                    await client.call_tool("grokbot_result", {"job_id": JOB_ID})
                ).structured_content

        result = asyncio.run(scenario())

        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["answer"], "A short bounded answer.")
        self.assertEqual(self.state(OTHER_JOB_ID), "uncertain")


class StdioServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        private = self.root / "private"
        private.mkdir(mode=0o700)
        self.database = private / "jobs.sqlite3"
        self.store = JobStore.open(self.database)
        self.store.create_request(JOB_ID, "ask", deadline())
        self.store.close()
        self.config_file = write_config_file(self.root, config_text(self.root))

    def test_stdio_transport_serves_the_tool_surface_and_reconciles(self) -> None:
        async def scenario():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-m",
                    "codex_grokbot_mcp.server",
                    "--config",
                    str(self.config_file),
                ],
                env={"PYTHONPATH": str(SOURCE_ROOT)},
            )
            async with Client(parameters) as client:
                names = {tool.name for tool in (await client.list_tools()).tools}
                status = (
                    await client.call_tool("grokbot_status", {"job_id": JOB_ID})
                ).structured_content
                missing = (
                    await client.call_tool("grokbot_status", {"job_id": OTHER_JOB_ID})
                ).structured_content
                return names, status, missing

        names, status, missing = asyncio.run(scenario())

        self.assertEqual(names, TOOL_NAMES)
        self.assertEqual(status["state"], "uncertain")
        self.assertEqual(status["job_type"], "ask")
        self.assertIsInstance(datetime.fromisoformat(status["updated_at"]), datetime)
        self.assertEqual(missing, {"job_id": OTHER_JOB_ID, "state": "not_found"})

    def test_stdio_transport_rejects_an_unusable_configuration(self) -> None:
        broken = write_config_file(self.root, config_text(self.root), mode=0o644)

        async def scenario():
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "codex_grokbot_mcp.server", "--config", str(broken)],
                env={"PYTHONPATH": str(SOURCE_ROOT)},
            )
            async with Client(parameters) as client:
                return await client.list_tools()

        with self.assertRaises(Exception):  # noqa: B017 - the child exits before initializing
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
