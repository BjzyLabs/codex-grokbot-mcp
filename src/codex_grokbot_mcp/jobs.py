"""Private, non-secret job journal for fail-closed restart recovery."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from codex_grokbot_mcp.control import ARTIFACT_PATTERN, BRANCH_PATTERN, TOKEN_MINIMUM
from codex_grokbot_mcp.local import ContractError, DelegationContext, Workspace
from codex_grokbot_mcp.vault import MAX_JOB_DURATION

SCHEMA_VERSION = 4
ACTIVE_STATES = (
    "queued",
    "lease_held",
    "dispatching",
    "dispatched",
    "artifact_received",
    "validated",
)
NEXT_STATES = {
    "queued": {"lease_held", "failed", "uncertain", "conflict"},
    "lease_held": {"dispatching", "failed", "uncertain", "conflict"},
    "dispatching": {"dispatched", "uncertain", "conflict"},
    "dispatched": {"artifact_received", "uncertain", "conflict", "failed", "ready"},
    "artifact_received": {"validated", "uncertain", "conflict"},
    "validated": {"ready", "uncertain", "conflict"},
    "ready": {"conflict"},
    "uncertain": {"conflict"},
    "conflict": set(),
    "failed": set(),
}


class JobStateError(RuntimeError):
    """Job state or its private storage is unsafe or inconsistent."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _add_column(connection: sqlite3.Connection, name: str, declaration: str) -> None:
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
    if name not in columns:
        connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")


def artifact_digest(artifact: dict[str, object]) -> str:
    """Bind a validated artifact to deterministic JSON without retaining its patch."""
    if not isinstance(artifact, dict):
        raise JobStateError("artifact is not an object")
    try:
        encoded = json.dumps(
            artifact, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise JobStateError("artifact is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
        raise JobStateError("invalid job or worker identifier")
    return value


def _private_path(path: Path) -> tuple[Path, bool]:
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
    created = False
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
        created = True
        details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise JobStateError("job store file must be private, regular, and owned by this user")
    return path, created


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    worker_id: str
    lease_owner: str
    root: Path
    target_repo: str
    head: str
    branch: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    snapshot_digest: str
    control_branch: str
    artifact_path: str
    state: str
    created_at: str
    updated_at: str
    token_expires_at: datetime | None
    artifact_head_sha: str | None
    artifact_sha256: str | None
    packet_schema: str = "v2"
    job_type: str = "coding"
    deliver: str = "github_pr"
    callback_expected: bool = False
    inbox_body_sha256: str | None = None
    terminal_reason: str | None = None


class JobStore:
    """Journal jobs without retaining delegated source or credentials."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: Path) -> JobStore:
        safe_path, created = _private_path(path)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(safe_path, timeout=5, isolation_level="IMMEDIATE")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
            )
            rows = connection.execute("SELECT version FROM schema_version").fetchall()
            if not rows:
                if not created:
                    raise JobStateError("existing job store has no schema version")
                connection.execute(
                    "INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,)
                )
            elif len(rows) != 1 or rows[0][0] not in (1, 2, 3, SCHEMA_VERSION):
                raise JobStateError("job store schema version is unsupported")
            old_version = rows[0][0] if rows else SCHEMA_VERSION
            connection.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
                    lease_owner TEXT NOT NULL, root TEXT NOT NULL, target_repo TEXT NOT NULL,
                    head TEXT NOT NULL, branch TEXT NOT NULL,
                    read_paths TEXT NOT NULL, write_paths TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    control_branch TEXT NOT NULL, artifact_path TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    token_expires_at TEXT, artifact_head_sha TEXT, artifact_sha256 TEXT,
                    packet_schema TEXT NOT NULL DEFAULT 'v2',
                    job_type TEXT NOT NULL DEFAULT 'coding',
                    deliver TEXT NOT NULL DEFAULT 'github_pr',
                    callback_expected INTEGER NOT NULL DEFAULT 0,
                    inbox_body_sha256 TEXT, terminal_reason TEXT
                )"""
            )
            if old_version == 1:
                _add_column(connection, "token_expires_at", "TEXT")
            if old_version < 3:
                _add_column(connection, "artifact_head_sha", "TEXT")
                _add_column(connection, "artifact_sha256", "TEXT")
                connection.execute(
                    """UPDATE jobs SET state='uncertain', updated_at=?
                       WHERE state IN ('validated', 'ready')""",
                    (_now(),),
                )
            if old_version < SCHEMA_VERSION:
                _add_column(connection, "packet_schema", "TEXT NOT NULL DEFAULT 'v2'")
                _add_column(connection, "job_type", "TEXT NOT NULL DEFAULT 'coding'")
                _add_column(connection, "deliver", "TEXT NOT NULL DEFAULT 'github_pr'")
                _add_column(connection, "callback_expected", "INTEGER NOT NULL DEFAULT 0")
                _add_column(connection, "inbox_body_sha256", "TEXT")
                _add_column(connection, "terminal_reason", "TEXT")
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            required = {
                "token_expires_at",
                "artifact_head_sha",
                "artifact_sha256",
                "packet_schema",
                "job_type",
                "deliver",
                "callback_expected",
            }
            if not required.issubset(columns):
                raise JobStateError("job store identity or expiry columns are missing")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_control_branch ON jobs(control_branch)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_artifact_path ON jobs(artifact_path)"
            )
            connection.commit()
        except (sqlite3.Error, JobStateError) as error:
            if connection is not None:
                connection.close()
            raise JobStateError("job store schema is unavailable or unsupported") from error
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    def create(
        self,
        job_id: str,
        worker_id: str,
        context: DelegationContext,
        *,
        lease_owner: str,
        control_branch: str,
        artifact_path: str,
    ) -> None:
        _identifier(job_id)
        _identifier(worker_id)
        _identifier(lease_owner)
        if not BRANCH_PATTERN.fullmatch(control_branch) or not ARTIFACT_PATTERN.fullmatch(
            artifact_path
        ):
            raise JobStateError("dispatch coordinates are required")
        now = _now()
        try:
            with self._connection:
                self._connection.execute(
                    """INSERT INTO jobs (
                        job_id, worker_id, lease_owner, root, target_repo, head, branch,
                        read_paths, write_paths, snapshot_digest, control_branch, artifact_path,
                        state, created_at, updated_at, token_expires_at, artifact_head_sha,
                        artifact_sha256
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    (
                        job_id,
                        worker_id,
                        lease_owner,
                        str(context.root),
                        context.target_repo,
                        context.head,
                        context.branch,
                        json.dumps(context.read_paths),
                        json.dumps(context.write_paths),
                        context.snapshot_digest,
                        control_branch,
                        artifact_path,
                        "queued",
                        now,
                        now,
                        None,
                        None,
                        None,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise JobStateError("job identity or artifact coordinates already exist") from error

    def get(self, job_id: str) -> JobRecord | None:
        _identifier(job_id)
        row = self._connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            return None
        expiry_raw = row["token_expires_at"]
        try:
            expiry = datetime.fromisoformat(expiry_raw) if expiry_raw is not None else None
        except (ValueError, TypeError) as error:
            raise JobStateError("stored token expiry is invalid") from error
        if expiry is not None and (expiry.tzinfo is None or expiry.utcoffset() is None):
            raise JobStateError("stored token expiry lacks a timezone")
        artifact_head_sha = row["artifact_head_sha"]
        artifact_sha256 = row["artifact_sha256"]
        if (artifact_head_sha is None) != (artifact_sha256 is None):
            raise JobStateError("stored artifact identity is incomplete")
        if artifact_head_sha is not None and (
            not re.fullmatch(r"[0-9a-f]{40}", artifact_head_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
        ):
            raise JobStateError("stored artifact identity is invalid")
        packet_schema = row["packet_schema"]
        job_type = row["job_type"]
        deliver = row["deliver"]
        callback_expected = bool(row["callback_expected"])
        inbox_body_sha256 = row["inbox_body_sha256"]
        terminal_reason = row["terminal_reason"]
        if row["state"] in ("validated", "ready") and artifact_head_sha is None:
            if not (job_type == "x_query" and inbox_body_sha256 and row["state"] == "ready"):
                raise JobStateError("stored validated job lacks artifact identity")
        return JobRecord(
            row["job_id"],
            row["worker_id"],
            row["lease_owner"],
            Path(row["root"]),
            row["target_repo"],
            row["head"],
            row["branch"],
            tuple(json.loads(row["read_paths"])),
            tuple(json.loads(row["write_paths"])),
            row["snapshot_digest"],
            row["control_branch"],
            row["artifact_path"],
            row["state"],
            row["created_at"],
            row["updated_at"],
            expiry,
            artifact_head_sha,
            artifact_sha256,
            packet_schema,
            job_type,
            deliver,
            callback_expected,
            inbox_body_sha256,
            terminal_reason,
        )

    def list_open(self) -> tuple[JobRecord, ...]:
        """Return nonterminal jobs, including uncertain jobs awaiting reconciliation."""
        rows = self._connection.execute(
            """SELECT job_id FROM jobs
               WHERE state IN ('queued', 'lease_held', 'dispatching', 'dispatched',
                               'artifact_received', 'validated', 'uncertain')
               ORDER BY created_at, job_id"""
        ).fetchall()
        records = tuple(self.get(row["job_id"]) for row in rows)
        if any(record is None for record in records):
            raise JobStateError("job list changed during read")
        return cast(tuple[JobRecord, ...], records)

    def create_callback_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        lease_owner: str,
        root: Path,
        deadline: datetime,
    ) -> None:
        """Journal one callback job without control-repository coordinates."""
        _identifier(job_id)
        _identifier(worker_id)
        _identifier(lease_owner)
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise JobStateError("callback deadline is invalid")
        now = _now()
        try:
            with self._connection:
                self._connection.execute(
                    """INSERT INTO jobs (
                        job_id, worker_id, lease_owner, root, target_repo, head, branch,
                        read_paths, write_paths, snapshot_digest, control_branch, artifact_path,
                        state, created_at, updated_at, token_expires_at,
                        packet_schema, job_type, deliver, callback_expected
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    (
                        job_id,
                        worker_id,
                        lease_owner,
                        str(root),
                        "local/none",
                        "0" * 40,
                        "none",
                        "[]",
                        "[]",
                        "0" * 64,
                        f"callback/{job_id}",
                        f"callbacks/{job_id}.json",
                        "queued",
                        now,
                        now,
                        deadline.astimezone(UTC).isoformat(),
                        "v3",
                        "x_query",
                        "callback",
                        1,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise JobStateError("job identity or artifact coordinates already exist") from error

    def record_callback_result(self, job_id: str, digest: str, reason: str) -> None:
        """Pin the callback body digest before a terminal callback transition."""
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or reason not in ("ok", "error", "blocked"):
            raise JobStateError("callback result identity is invalid")
        record = self.get(job_id)
        if record is None or record.state != "dispatched" or record.inbox_body_sha256 is not None:
            raise JobStateError("callback result cannot be recorded in this state")
        with self._connection:
            result = self._connection.execute(
                """UPDATE jobs SET inbox_body_sha256=?, terminal_reason=?, updated_at=?
                   WHERE job_id=? AND state='dispatched' AND inbox_body_sha256 IS NULL""",
                (digest, reason, _now(), job_id),
            )
            if result.rowcount != 1:
                raise JobStateError("callback result was already recorded or state changed")

    def record_token_expiry(self, job_id: str, expires_at: datetime) -> None:
        """Store only the worker token deadline before an irreversible webhook POST."""
        if (
            not isinstance(expires_at, datetime)
            or expires_at.tzinfo is None
            or expires_at.utcoffset() is None
            or expires_at.astimezone(UTC) - datetime.now(UTC) < TOKEN_MINIMUM
        ):
            raise JobStateError("worker token expiry is invalid or too soon")
        with self._connection:
            result = self._connection.execute(
                """UPDATE jobs SET token_expires_at=?, updated_at=?
                   WHERE job_id=? AND state='lease_held' AND token_expires_at IS NULL""",
                (expires_at.astimezone(UTC).isoformat(), _now(), _identifier(job_id)),
            )
            if result.rowcount != 1:
                raise JobStateError("worker token expiry cannot be recorded in this state")

    def record_artifact_identity(self, job_id: str, head_sha: str, artifact_sha256: str) -> None:
        """Pin one validated artifact's immutable commit and content digest."""
        if (
            not isinstance(head_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", head_sha) is None
            or not isinstance(artifact_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None
        ):
            raise JobStateError("artifact identity is invalid")
        record = self.get(job_id)
        if record is None or record.state != "artifact_received":
            raise JobStateError("artifact identity cannot be recorded in this state")
        if record.artifact_head_sha is not None:
            raise JobStateError("artifact identity was already recorded")
        with self._connection:
            result = self._connection.execute(
                """UPDATE jobs SET artifact_head_sha=?, artifact_sha256=?, updated_at=?
                   WHERE job_id=? AND state='artifact_received' AND artifact_head_sha IS NULL""",
                (head_sha, artifact_sha256, _now(), job_id),
            )
            if result.rowcount != 1:
                raise JobStateError("artifact identity was already recorded or state changed")

    def advance(self, job_id: str, state: str) -> None:
        record = self.get(job_id)
        if record is None or state not in NEXT_STATES.get(record.state, set()):
            raise JobStateError("job state transition is not allowed")
        if state == "dispatching":
            if record.token_expires_at is None:
                raise JobStateError("worker token expiry must be durable before dispatch")
            remaining = record.token_expires_at.astimezone(UTC) - datetime.now(UTC)
            if record.job_type == "x_query":
                if remaining <= timedelta(0):
                    raise JobStateError("worker token expiry must be durable before dispatch")
            elif remaining < MAX_JOB_DURATION:
                raise JobStateError("worker token expiry must be durable before dispatch")
        if state == "ready" and record.state == "dispatched" and record.job_type != "x_query":
            raise JobStateError("coding result still requires the pinned artifact")
        if state in ("validated", "ready") and record.artifact_head_sha is None:
            if not (record.job_type == "x_query" and record.inbox_body_sha256 and state == "ready"):
                raise JobStateError("artifact identity must be durable before validation")
        with self._connection:
            result = self._connection.execute(
                "UPDATE jobs SET state=?, updated_at=? WHERE job_id=? AND state=?",
                (state, _now(), job_id, record.state),
            )
            if result.rowcount != 1:
                raise JobStateError("job state changed concurrently")

    def reconcile_restart(self) -> tuple[str, ...]:
        """Retain evidence and block another POST or lease release after a crash."""
        with self._connection:
            rows = self._connection.execute(
                "SELECT job_id FROM jobs WHERE state IN (?, ?, ?, ?, ?, ?) ORDER BY job_id",
                ACTIVE_STATES,
            ).fetchall()
            ids = tuple(row[0] for row in rows)
            self._connection.executemany(
                "UPDATE jobs SET state='uncertain', updated_at=? WHERE job_id=?",
                ((_now(), job_id) for job_id in ids),
            )
        return ids

    def verify_workspace(self, job_id: str) -> bool:
        """Recreate the selected snapshot; a drifted workspace cannot receive a patch."""
        record = self.get(job_id)
        if record is None:
            raise JobStateError("job does not exist")
        try:
            workspace = Workspace.open(record.root, opted_in=True)
            current = workspace.snapshot(
                read_paths=list(record.read_paths), write_paths=list(record.write_paths)
            )
            valid = (
                current.target_repo == record.target_repo
                and current.head == record.head
                and current.branch == record.branch
                and current.snapshot_digest == record.snapshot_digest
            )
        except (ContractError, OSError):
            valid = False
        if not valid and record.state != "conflict":
            self.advance(job_id, "conflict")
        return valid
