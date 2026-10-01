"""Private, non-secret journal of local webhook requests.

The journal holds no credential and no prompt text. It exists so a restart can
resume polling for a request that was already posted, and so an ambiguous
outcome stays visible instead of being retried.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from codex_grokbot_mcp.deliver import ERROR_TEXT_MAX, JOB_TYPES, SUMMARY_MAX

SCHEMA_VERSION = 2
RESUMABLE_STATES = ("dispatched",)
NEXT_STATES: dict[str, set[str]] = {
    "queued": {"dispatching", "failed", "uncertain", "conflict"},
    "dispatching": {"dispatched", "failed", "uncertain", "conflict"},
    "dispatched": {"ready", "failed", "uncertain", "conflict"},
    "ready": {"conflict"},
    "uncertain": {"conflict"},
    "conflict": set(),
    "failed": set(),
}
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_JOB_COLUMNS = (
    "job_id TEXT PRIMARY KEY",
    "job_type TEXT NOT NULL",
    "state TEXT NOT NULL",
    "created_at TEXT NOT NULL",
    "updated_at TEXT NOT NULL",
    "deadline_at TEXT NOT NULL",
    "summary TEXT",
    "answer_json TEXT",
    "sources_json TEXT",
    "error_code TEXT",
    "error_message TEXT",
    "body_sha256 TEXT",
)
_LEGACY_TABLES = ("jobs", "worker_diagnostics")


class JobStateError(RuntimeError):
    """Journal state is inconsistent or a transition is not allowed."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _timestamp(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str):
        raise JobStateError(f"stored {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise JobStateError(f"stored {label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise JobStateError(f"stored {label} lacks a timezone")
    return parsed.astimezone(UTC)


def _canonical(value: Any) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise JobStateError("job ID must be a canonical UUID") from error
    if canonical != value:
        raise JobStateError("job ID must be a canonical UUID")
    return canonical


def _optional_text(value: Any, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise JobStateError(f"{label} is invalid")
    if any(ord(character) < 32 and not character.isspace() for character in value):
        raise JobStateError(f"{label} is unsafe")
    return value


def _private_path(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or path.name in ("", ".", ".."):
        raise JobStateError("job store path must be absolute")
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    details = parent.lstat()
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise JobStateError("job store directory must be private and owned by this user")
    if path.is_symlink():
        raise JobStateError("job store must not be a symlink")
    try:
        details = path.lstat()
    except FileNotFoundError:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError as error:
            raise JobStateError("job store path changed during creation") from error
        else:
            os.close(descriptor)
        details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise JobStateError("job store file must be private, regular, and owned by this user")
    return path


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    job_type: str
    state: str
    created_at: str
    updated_at: str
    deadline_at: datetime
    summary: str | None = None
    answer_json: str | None = None
    sources_json: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    body_sha256: str | None = None


class JobStore:
    """Journal one request per job ID without retaining credentials or prompts."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> JobStore:
        safe_path = _private_path(path)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(safe_path, timeout=5, isolation_level="IMMEDIATE")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            rows = connection.execute("SELECT version FROM schema_version").fetchall()
            current = rows[0][0] if len(rows) == 1 else None
            if current != SCHEMA_VERSION:
                # Older canary history is disposable; rebuild the journal instead of
                # migrating state the single-request design no longer uses.
                for table in _LEGACY_TABLES:
                    connection.execute(f"DROP TABLE IF EXISTS {table}")
                connection.execute("DELETE FROM schema_version")
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            connection.execute(f"CREATE TABLE IF NOT EXISTS jobs ({', '.join(_JOB_COLUMNS)})")
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            required = {declaration.split()[0] for declaration in _JOB_COLUMNS}
            if not required.issubset(columns):
                raise JobStateError("job store columns are missing")
            connection.commit()
        except (sqlite3.Error, JobStateError) as error:
            if connection is not None:
                connection.close()
            raise JobStateError("job store schema is unavailable or unsupported") from error
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def create_request(self, job_id: str, job_type: str, deadline: datetime) -> None:
        """Record one request before any webhook POST is attempted."""
        canonical = _canonical(job_id)
        if job_type not in JOB_TYPES:
            raise JobStateError("job type is invalid")
        if (
            not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
            or deadline.astimezone(UTC) <= datetime.now(UTC)
        ):
            raise JobStateError("job deadline is invalid")
        now = _now()
        try:
            with self._connection:
                self._connection.execute(
                    """INSERT INTO jobs
                    (job_id, job_type, state, created_at, updated_at, deadline_at)
                    VALUES (?, ?, 'queued', ?, ?, ?)""",
                    (canonical, job_type, now, now, deadline.astimezone(UTC).isoformat()),
                )
        except sqlite3.IntegrityError as error:
            raise JobStateError("job identity already exists") from error

    def get(self, job_id: str) -> JobRecord | None:
        """Return one stored job; an unusable identifier is simply not found."""
        try:
            canonical = _canonical(job_id)
        except JobStateError:
            return None
        row = self._connection.execute("SELECT * FROM jobs WHERE job_id=?", (canonical,)).fetchone()
        if row is None:
            return None
        if row["state"] not in NEXT_STATES or row["job_type"] not in JOB_TYPES:
            raise JobStateError("stored job identity is invalid")
        digest = row["body_sha256"]
        if digest is not None and not _DIGEST_PATTERN.fullmatch(digest):
            raise JobStateError("stored callback digest is invalid")
        if row["state"] == "ready" and digest is None:
            raise JobStateError("a completed job lacks its callback digest")
        return JobRecord(
            job_id=row["job_id"],
            job_type=row["job_type"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deadline_at=_timestamp(row["deadline_at"], label="deadline"),
            summary=row["summary"],
            answer_json=row["answer_json"],
            sources_json=row["sources_json"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            body_sha256=digest,
        )

    def list_open(self) -> tuple[JobRecord, ...]:
        """Return requests that have not reached a terminal state."""
        rows = self._connection.execute(
            """SELECT job_id FROM jobs WHERE state IN
               ('queued', 'dispatching', 'dispatched', 'uncertain')
               ORDER BY created_at, job_id"""
        ).fetchall()
        records = []
        for row in rows:
            record = self.get(row["job_id"])
            if record is None:
                raise JobStateError("job list changed during read")
            records.append(record)
        return tuple(records)

    def advance(self, job_id: str, state: str) -> None:
        canonical = _canonical(job_id)
        record = self.get(canonical)
        if record is None or state not in NEXT_STATES.get(record.state, set()):
            raise JobStateError("job state transition is not allowed")
        if state == "ready" and record.body_sha256 is None:
            raise JobStateError("a completed job requires its callback digest")
        with self._connection:
            result = self._connection.execute(
                "UPDATE jobs SET state=?, updated_at=? WHERE job_id=? AND state=?",
                (state, _now(), canonical, record.state),
            )
            if result.rowcount != 1:
                raise JobStateError("job state changed concurrently")

    def record_result(
        self,
        job_id: str,
        body_sha256: str,
        summary: Any,
        answer: Any,
        sources: Any,
    ) -> None:
        """Pin a validated answer to its callback digest before marking it ready."""
        canonical = _canonical(job_id)
        if not isinstance(body_sha256, str) or not _DIGEST_PATTERN.fullmatch(body_sha256):
            raise JobStateError("callback digest is invalid")
        summary_text = _optional_text(summary, label="callback summary", maximum=SUMMARY_MAX)
        if not isinstance(sources, list):
            raise JobStateError("callback sources are invalid")
        try:
            answer_json = json.dumps(answer, separators=(",", ":"), sort_keys=True)
            sources_json = json.dumps(sources, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as error:
            raise JobStateError("callback answer is not canonical JSON") from error
        with self._connection:
            result = self._connection.execute(
                """UPDATE jobs SET body_sha256=?, summary=?, answer_json=?, sources_json=?,
                   updated_at=?
                   WHERE job_id=? AND state='dispatched' AND body_sha256 IS NULL""",
                (body_sha256, summary_text, answer_json, sources_json, _now(), canonical),
            )
            if result.rowcount != 1:
                raise JobStateError("callback result was already recorded or state changed")

    def record_error(self, job_id: str, code: Any, message: Any) -> None:
        """Pin one validated error callback before marking the request failed."""
        canonical = _canonical(job_id)
        code_text = _optional_text(code, label="error code", maximum=ERROR_TEXT_MAX)
        message_text = _optional_text(message, label="error message", maximum=ERROR_TEXT_MAX)
        with self._connection:
            result = self._connection.execute(
                """UPDATE jobs SET error_code=?, error_message=?, updated_at=?
                   WHERE job_id=? AND state='dispatched' AND error_code IS NULL""",
                (code_text, message_text, _now(), canonical),
            )
            if result.rowcount != 1:
                raise JobStateError("error result was already recorded or state changed")

    def reconcile_restart(self) -> tuple[str, ...]:
        """Mark interrupted pre-dispatch work uncertain and return resumable jobs."""
        with self._connection:
            rows = self._connection.execute(
                "SELECT job_id FROM jobs WHERE state=? ORDER BY created_at, job_id",
                (RESUMABLE_STATES[0],),
            ).fetchall()
            resumable = tuple(row["job_id"] for row in rows)
            self._connection.execute(
                "UPDATE jobs SET state='uncertain', updated_at=? WHERE state IN ('queued', ?)",
                (_now(), "dispatching"),
            )
        return resumable
