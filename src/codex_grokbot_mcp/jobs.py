"""Private, non-secret job journal for fail-closed restart recovery."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from codex_grokbot_mcp.control import ARTIFACT_PATTERN, BRANCH_PATTERN, TOKEN_MINIMUM
from codex_grokbot_mcp.local import ContractError, DelegationContext, Workspace
from codex_grokbot_mcp.vault import MAX_JOB_DURATION

SCHEMA_VERSION = 2
ACTIVE_STATES = ("lease_held", "dispatching", "dispatched", "artifact_received", "validated")
NEXT_STATES = {
    "queued": {"lease_held", "failed", "conflict"},
    "lease_held": {"dispatching", "uncertain", "conflict"},
    "dispatching": {"dispatched", "uncertain", "conflict"},
    "dispatched": {"artifact_received", "uncertain", "conflict"},
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
            elif len(rows) != 1 or rows[0][0] not in (1, SCHEMA_VERSION):
                raise JobStateError("job store schema version is unsupported")
            needs_migration = bool(rows and rows[0][0] == 1)
            connection.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, worker_id TEXT NOT NULL,
                    lease_owner TEXT NOT NULL, root TEXT NOT NULL, target_repo TEXT NOT NULL,
                    head TEXT NOT NULL, branch TEXT NOT NULL,
                    read_paths TEXT NOT NULL, write_paths TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    control_branch TEXT NOT NULL, artifact_path TEXT NOT NULL,
                    state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    token_expires_at TEXT
                )"""
            )
            if needs_migration:
                connection.execute("ALTER TABLE jobs ADD COLUMN token_expires_at TEXT")
                connection.execute("UPDATE schema_version SET version=?", (SCHEMA_VERSION,))
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "token_expires_at" not in columns:
                raise JobStateError("job store expiry column is missing")
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
                    """INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
        )

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

    def advance(self, job_id: str, state: str) -> None:
        record = self.get(job_id)
        if record is None or state not in NEXT_STATES.get(record.state, set()):
            raise JobStateError("job state transition is not allowed")
        if state == "dispatching" and (
            record.token_expires_at is None
            or record.token_expires_at.astimezone(UTC) - datetime.now(UTC) < MAX_JOB_DURATION
        ):
            raise JobStateError("worker token expiry must be durable before dispatch")
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
                "SELECT job_id FROM jobs WHERE state IN (?, ?, ?, ?, ?) ORDER BY job_id",
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
