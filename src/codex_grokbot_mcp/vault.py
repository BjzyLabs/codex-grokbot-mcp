"""Vault AppRole and KV v2 compare-and-swap worker leases.

Lease records are non-secret. No alternate credential or lease backend exists.
"""

from __future__ import annotations

import json
import re
import ssl
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

LEASE_HEARTBEAT = timedelta(minutes=5)
MAX_JOB_DURATION = timedelta(minutes=45)
MAX_RESPONSE_BYTES = 1024 * 1024


class VaultError(RuntimeError):
    """Vault denied or returned an invalid response."""


class VaultUnavailable(VaultError):
    """Vault's response is unknown after a network failure."""


class VaultHTTPError(VaultError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"Vault returned HTTP {status}")


class LeaseConfigurationError(VaultError):
    """The lease path has not been provisioned with required CAS."""


class LeaseUncertain(VaultError):
    """A write may have reached Vault; reconciliation is required."""


class WorkerBusy(VaultError):
    """A worker has an active or contested lease."""


class LeaseOwnershipError(VaultError):
    """The caller does not own the observed lease."""


def _safe_path(value: str, *, nested: bool) -> str:
    pattern = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*" if nested else r"[A-Za-z0-9_.-]+"
    if (
        not isinstance(value, str)
        or not re.fullmatch(pattern, value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise VaultError("invalid Vault path component")
    return value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise VaultError("lease time must include a timezone")
    return value.astimezone(UTC)


def _timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise VaultError("lease time is invalid")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as error:
        raise VaultError("lease time is invalid") from error


def _credential(path: Path) -> str:
    try:
        if path.is_symlink():
            raise VaultError("AppRole credential file must not be a symlink")
        details = path.stat()
        if not stat.S_ISREG(details.st_mode) or details.st_mode & 0o077:
            raise VaultError("AppRole credential file must be private and regular")
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise VaultError("AppRole credential file is unavailable") from error
    if not value:
        raise VaultError("AppRole credential file is empty")
    return value


@dataclass(frozen=True)
class VaultClient:
    address: str
    role_id_file: Path
    secret_id_file: Path
    mount: str
    ca_cert_file: Path | None = None

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.address)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username:
            raise VaultError("Vault address must be HTTPS without embedded credentials")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise VaultError("Vault address must not contain a path or query")
        _safe_path(self.mount, nested=False)
        if self.ca_cert_file is not None and not self.ca_cert_file.is_file():
            raise VaultError("Vault CA bundle file is unavailable")

    def _open(self, endpoint: str, *, method: str, payload: dict | None, token: str | None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        # The constructor enforces an HTTPS Vault address.
        request = urllib.request.Request(  # noqa: S310
            f"{self.address.rstrip('/')}/v1/{endpoint}",
            data=data,
            headers={
                "Content-Type": "application/json",
                **({"X-Vault-Token": token} if token is not None else {}),
            },
            method=method,
        )
        try:
            options = {}
            if self.ca_cert_file is not None:
                options["context"] = ssl.create_default_context(cafile=str(self.ca_cert_file))
            with urllib.request.urlopen(request, timeout=15, **options) as response:  # noqa: S310
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise VaultHTTPError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise VaultUnavailable("Vault request outcome is unknown") from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise VaultError("Vault response exceeds size limit")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise VaultError("Vault response is not valid JSON") from error
        if not isinstance(result, dict):
            raise VaultError("Vault response is not an object")
        return result

    def _login(self) -> str:
        result = self._open(
            "auth/approle/login",
            method="POST",
            payload={
                "role_id": _credential(self.role_id_file),
                "secret_id": _credential(self.secret_id_file),
            },
            token=None,
        )
        auth = result.get("auth")
        token = auth.get("client_token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token:
            raise VaultError("AppRole login returned no client token")
        return token

    def request(self, endpoint: str, *, method: str = "GET", payload: dict | None = None) -> dict:
        """Authenticate for each operation so no expired token is reused."""
        return self._open(endpoint, method=method, payload=payload, token=self._login())

    def read_secret(self, path: str) -> dict[str, Any]:
        _safe_path(path, nested=True)
        result = self.request(f"{self.mount}/data/{path}")
        envelope = result.get("data")
        secret = envelope.get("data") if isinstance(envelope, dict) else None
        if not isinstance(secret, dict):
            raise VaultError("KV v2 secret response is malformed")
        return secret


@dataclass(frozen=True)
class LeaseRecord:
    worker: str
    state: str
    owner: str | None
    job_id: str | None
    acquired_at: datetime | None
    expires_at: datetime | None
    deadline_at: datetime | None
    version: int

    @classmethod
    def from_vault(cls, worker: str, version: int, data: dict) -> LeaseRecord:
        if data.get("schema_version") != "v1" or data.get("worker") != worker:
            raise VaultError("lease record has an unsupported schema or worker")
        state = data.get("state")
        if state not in ("active", "available"):
            raise VaultError("lease state is invalid")
        owner = data.get("owner")
        job_id = data.get("job_id")
        acquired = _timestamp(data.get("acquired_at"))
        expires = _timestamp(data.get("expires_at"))
        deadline = _timestamp(data.get("deadline_at"))
        if state == "active":
            if not all(isinstance(item, str) and item for item in (owner, job_id)):
                raise VaultError("active lease has no owner or job")
            if acquired is None or expires is None or deadline is None:
                raise VaultError("active lease has incomplete times")
            if not acquired < expires <= deadline <= acquired + MAX_JOB_DURATION:
                raise VaultError("active lease exceeds the job duration")
        elif any(item is not None for item in (owner, job_id, acquired, expires, deadline)):
            raise VaultError("available lease contains active ownership")
        return cls(worker, state, owner, job_id, acquired, expires, deadline, version)

    def data(self) -> dict[str, str | None]:
        return {
            "schema_version": "v1",
            "worker": self.worker,
            "state": self.state,
            "owner": self.owner,
            "job_id": self.job_id,
            "acquired_at": self.acquired_at.isoformat() if self.acquired_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "deadline_at": self.deadline_at.isoformat() if self.deadline_at else None,
        }


@dataclass(frozen=True)
class VaultLeaseStore:
    client: VaultClient
    lease_prefix: str

    def __post_init__(self) -> None:
        _safe_path(self.lease_prefix, nested=True)

    def _path(self, worker: str) -> str:
        return f"{self.lease_prefix}/{_safe_path(worker, nested=False)}"

    def require_cas(self, worker: str) -> None:
        path = self._path(worker)
        try:
            result = self.client.request(f"{self.client.mount}/metadata/{path}")
        except VaultHTTPError as error:
            if error.status == 404:
                raise LeaseConfigurationError("lease metadata is missing") from None
            raise
        data = result.get("data")
        if not isinstance(data, dict) or data.get("cas_required") is not True:
            raise LeaseConfigurationError("lease path must require CAS")

    def read(self, worker: str) -> LeaseRecord | None:
        path = self._path(worker)
        try:
            result = self.client.request(f"{self.client.mount}/data/{path}")
        except VaultHTTPError as error:
            if error.status == 404:
                return None
            raise
        envelope = result.get("data")
        if not isinstance(envelope, dict):
            raise VaultError("KV v2 lease response is malformed")
        metadata = envelope.get("metadata")
        data = envelope.get("data")
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if not isinstance(version, int) or version < 1 or not isinstance(data, dict):
            raise VaultError("KV v2 lease version or data is invalid")
        return LeaseRecord.from_vault(worker, version, data)

    def _write(self, record: LeaseRecord, expected_version: int) -> LeaseRecord:
        path = self._path(record.worker)
        payload = {"options": {"cas": expected_version}, "data": record.data()}
        self.client.request(f"{self.client.mount}/data/{path}", method="POST", payload=payload)
        current = self.read(record.worker)
        if current is None or current.version != expected_version + 1:
            raise LeaseUncertain("lease write could not be verified")
        return current

    def acquire(
        self, worker: str, *, owner: str, job_id: str, now: datetime | None = None
    ) -> LeaseRecord:
        self.require_cas(worker)
        _safe_path(owner, nested=False)
        _safe_path(job_id, nested=False)
        timestamp = _utc(now or datetime.now(UTC))
        current = self.read(worker)
        if current is not None and current.state == "active":
            raise WorkerBusy("worker has an active lease; expired leases require reconciliation")
        version = current.version if current else 0
        record = LeaseRecord(
            worker,
            "active",
            owner,
            job_id,
            timestamp,
            timestamp + LEASE_HEARTBEAT,
            timestamp + MAX_JOB_DURATION,
            version,
        )
        try:
            return self._write(record, version)
        except VaultHTTPError as error:
            if error.status == 400:
                observed = self.read(worker)
                if observed is not None and observed.version != version:
                    raise WorkerBusy("another dispatcher changed the lease") from None
            raise
        except VaultUnavailable:
            try:
                observed = self.read(worker)
            except VaultError:
                raise LeaseUncertain("lease acquisition cannot be reconciled") from None
            if observed is not None and observed.owner == owner and observed.job_id == job_id:
                return observed
            raise LeaseUncertain("lease acquisition outcome is uncertain") from None

    def renew(
        self, worker: str, *, owner: str, job_id: str, now: datetime | None = None
    ) -> LeaseRecord:
        self.require_cas(worker)
        current = self._owned(worker, owner, job_id)
        timestamp = _utc(now or datetime.now(UTC))
        if current.deadline_at is None or timestamp >= current.deadline_at:
            raise LeaseOwnershipError("job deadline has passed")
        next_expiry = min(timestamp + LEASE_HEARTBEAT, current.deadline_at)
        if current.acquired_at is None:
            raise VaultError("active lease has no acquisition time")
        replacement = LeaseRecord(
            worker,
            "active",
            owner,
            job_id,
            current.acquired_at,
            next_expiry,
            current.deadline_at,
            current.version,
        )
        try:
            return self._write(replacement, current.version)
        except VaultUnavailable:
            try:
                observed = self.read(worker)
            except VaultError:
                raise LeaseUncertain("lease renewal cannot be reconciled") from None
            if observed is not None and observed.owner == owner and observed.job_id == job_id:
                if observed.version == current.version + 1 and observed.expires_at == next_expiry:
                    return observed
            raise LeaseUncertain("lease renewal outcome is uncertain") from None

    def release(self, worker: str, *, owner: str, job_id: str) -> LeaseRecord:
        self.require_cas(worker)
        current = self._owned(worker, owner, job_id)
        replacement = LeaseRecord(
            worker, "available", None, None, None, None, None, current.version
        )
        try:
            return self._write(replacement, current.version)
        except VaultUnavailable:
            try:
                observed = self.read(worker)
            except VaultError:
                raise LeaseUncertain("lease release cannot be reconciled") from None
            if observed is not None and observed.state == "available":
                if observed.version == current.version + 1:
                    return observed
            raise LeaseUncertain("lease release outcome is uncertain") from None

    def _owned(self, worker: str, owner: str, job_id: str) -> LeaseRecord:
        current = self.read(worker)
        if current is None or current.state != "active":
            raise LeaseOwnershipError("no active lease belongs to this job")
        if current.owner != owner or current.job_id != job_id:
            raise LeaseOwnershipError("another job owns the lease")
        return current
