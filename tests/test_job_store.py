from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from support import JOB_ID, OTHER_JOB_ID, deadline

from codex_grokbot_mcp.jobs import NEXT_STATES, SCHEMA_VERSION, JobStateError, JobStore

DIGEST = "a" * 64
OTHER_DIGEST = "b" * 64


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.path = self.private / "jobs.sqlite3"
        self.store = JobStore.open(self.path)
        self.addCleanup(self.store.close)

    def request(self, job_id: str = JOB_ID, job_type: str = "x_query") -> str:
        self.store.create_request(job_id, job_type, deadline())
        return job_id

    def dispatched(self, job_id: str = JOB_ID, job_type: str = "x_query") -> str:
        self.request(job_id, job_type)
        self.store.advance(job_id, "dispatching")
        self.store.advance(job_id, "dispatched")
        return job_id

    def test_round_trip_records_both_request_types(self) -> None:
        self.request(JOB_ID, "x_query")
        self.request(OTHER_JOB_ID, "ask")

        first = self.store.get(JOB_ID)
        second = self.store.get(OTHER_JOB_ID)

        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertEqual((first.job_type, first.state), ("x_query", "queued"))
        self.assertEqual((second.job_type, second.state), ("ask", "queued"))
        self.assertEqual(first.created_at, first.updated_at)
        self.assertGreater(first.deadline_at, datetime.now(UTC))
        self.assertIsNone(first.body_sha256)

    def test_unknown_or_unusable_identifier_is_not_found(self) -> None:
        self.assertIsNone(self.store.get(OTHER_JOB_ID))
        self.assertIsNone(self.store.get("not-a-uuid"))
        self.assertIsNone(self.store.get(JOB_ID.upper()))

    def test_creation_validates_identity_type_and_deadline(self) -> None:
        with self.assertRaises(JobStateError):
            self.store.create_request("not-a-uuid", "x_query", deadline())
        with self.assertRaises(JobStateError):
            self.store.create_request(JOB_ID, "coding", deadline())
        with self.assertRaises(JobStateError):
            self.store.create_request(JOB_ID, "x_query", datetime(2026, 1, 1, tzinfo=UTC))
        with self.assertRaises(JobStateError):
            self.store.create_request(JOB_ID, "x_query", datetime.now(UTC))
        with self.assertRaises(JobStateError):
            self.store.create_request(JOB_ID, "x_query", deadline())
            self.store.create_request(JOB_ID, "x_query", deadline())

    def test_state_graph_is_enforced(self) -> None:
        self.request(JOB_ID)

        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "dispatched")
        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "nonsense")
        self.store.advance(JOB_ID, "dispatching")
        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "queued")
        self.store.advance(JOB_ID, "dispatched")
        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "dispatching")

    def test_ready_is_reachable_only_with_a_recorded_digest(self) -> None:
        self.dispatched()

        with self.assertRaisesRegex(JobStateError, "digest"):
            self.store.advance(JOB_ID, "ready")
        self.store.record_result(JOB_ID, DIGEST, "One line.", "answer", ["source"])
        self.store.advance(JOB_ID, "ready")

        record = self.store.get(JOB_ID)
        assert record is not None
        self.assertEqual(record.state, "ready")
        self.assertEqual(record.body_sha256, DIGEST)
        self.assertEqual(record.summary, "One line.")
        self.assertEqual(record.answer_json, '"answer"')
        self.assertEqual(record.sources_json, '["source"]')
        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "ready")
        self.store.advance(JOB_ID, "conflict")
        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "ready")

    def test_state_graph_dictionary_matches_the_documented_terminal_states(self) -> None:
        self.assertEqual(NEXT_STATES["conflict"], set())
        self.assertEqual(NEXT_STATES["failed"], set())
        self.assertEqual(NEXT_STATES["ready"], {"conflict"})
        self.assertEqual(NEXT_STATES["dispatched"], {"ready", "failed", "uncertain", "conflict"})

    def test_failed_requests_may_carry_a_validated_error(self) -> None:
        self.dispatched(JOB_ID, "ask")

        with self.assertRaises(JobStateError):
            self.store.record_error(JOB_ID, "", "message")
        self.store.record_error(JOB_ID, "x_auth_required", "The X connection needs attention.")
        with self.assertRaises(JobStateError):
            self.store.record_error(JOB_ID, "again", "again")
        self.store.advance(JOB_ID, "failed")

        record = self.store.get(JOB_ID)
        assert record is not None
        self.assertEqual(record.state, "failed")
        self.assertEqual(record.error_code, "x_auth_required")
        self.assertIn("X connection", record.error_message)
        with self.assertRaises(JobStateError):
            self.store.advance(JOB_ID, "ready")

    def test_result_and_error_can_only_be_recorded_while_dispatched(self) -> None:
        self.request(JOB_ID)

        with self.assertRaises(JobStateError):
            self.store.record_result(JOB_ID, DIGEST, "summary", "answer", [])
        with self.assertRaises(JobStateError):
            self.store.record_error(JOB_ID, "code", "message")
        self.store.advance(JOB_ID, "dispatching")
        with self.assertRaises(JobStateError):
            self.store.record_result(JOB_ID, DIGEST, "summary", "answer", [])
        self.store.advance(JOB_ID, "dispatched")
        with self.assertRaises(JobStateError):
            self.store.record_result(JOB_ID, "not-a-digest", "summary", "answer", [])
        with self.assertRaises(JobStateError):
            self.store.record_result(JOB_ID, DIGEST, "summary", "answer", "not-a-list")

    def test_list_open_excludes_terminal_states(self) -> None:
        self.request(JOB_ID)
        self.request(OTHER_JOB_ID, "ask")
        self.store.advance(OTHER_JOB_ID, "dispatching")

        self.assertEqual(
            {record.job_id for record in self.store.list_open()}, {JOB_ID, OTHER_JOB_ID}
        )

        self.store.advance(JOB_ID, "failed")
        self.assertEqual({record.job_id for record in self.store.list_open()}, {OTHER_JOB_ID})

        self.store.advance(OTHER_JOB_ID, "dispatched")
        self.store.record_result(OTHER_JOB_ID, DIGEST, "summary", "answer", [])
        self.store.advance(OTHER_JOB_ID, "ready")
        self.assertEqual(self.store.list_open(), ())

    def test_list_open_keeps_uncertain_requests_visible(self) -> None:
        self.request(JOB_ID)
        self.store.advance(JOB_ID, "uncertain")

        self.assertEqual([record.state for record in self.store.list_open()], ["uncertain"])

    def test_reconcile_restart_is_idempotent(self) -> None:
        self.request(JOB_ID)
        self.request(OTHER_JOB_ID, "ask")
        self.store.advance(OTHER_JOB_ID, "dispatching")

        self.assertEqual(self.store.reconcile_restart(), ())
        assert self.store.get(JOB_ID) is not None
        self.assertEqual(self.store.get(JOB_ID).state, "uncertain")
        self.assertEqual(self.store.get(OTHER_JOB_ID).state, "uncertain")
        self.assertEqual(self.store.reconcile_restart(), ())

    def test_reconcile_restart_returns_dispatched_jobs_only(self) -> None:
        self.dispatched(JOB_ID, "ask")
        self.request(OTHER_JOB_ID)

        self.assertEqual(self.store.reconcile_restart(), (JOB_ID,))

        record = self.store.get(JOB_ID)
        assert record is not None
        self.assertEqual(record.state, "dispatched")
        self.assertEqual(self.store.get(OTHER_JOB_ID).state, "uncertain")

    def test_job_database_must_be_private(self) -> None:
        self.path.chmod(0o644)
        with self.assertRaisesRegex(JobStateError, "private"):
            JobStore.open(self.path)

        self.path.chmod(0o600)
        self.private.chmod(0o755)
        with self.assertRaisesRegex(JobStateError, "directory"):
            JobStore.open(self.path)

    def test_relative_job_database_is_rejected(self) -> None:
        with self.assertRaisesRegex(JobStateError, "absolute"):
            JobStore.open(Path("relative/jobs.sqlite3"))

    def test_legacy_schema_is_rebuilt(self) -> None:
        legacy = self.private / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version(version) VALUES (5)")
        connection.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, worker_id TEXT)")
        connection.execute("CREATE TABLE worker_diagnostics (diagnostic_id TEXT PRIMARY KEY)")
        connection.commit()
        connection.close()
        legacy.chmod(0o600)

        store = JobStore.open(legacy)
        self.addCleanup(store.close)

        check = sqlite3.connect(legacy)
        try:
            version = check.execute("SELECT version FROM schema_version").fetchall()
            tables = {
                row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            columns = {row[1] for row in check.execute("PRAGMA table_info(jobs)")}
        finally:
            check.close()

        self.assertEqual(version, [(SCHEMA_VERSION,)])
        self.assertNotIn("worker_diagnostics", tables)
        self.assertIn("deadline_at", columns)
        self.assertNotIn("worker_id", columns)
        store.create_request(JOB_ID, "ask", deadline())
        self.assertEqual(store.get(JOB_ID).job_type, "ask")


if __name__ == "__main__":
    unittest.main()
