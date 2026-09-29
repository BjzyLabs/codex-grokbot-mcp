"""One-shot result inbox. The worker POSTs once; requestors collect later."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from .deliver import CALLBACK_BODY_LIMIT, PacketError, canonical_inbox_origin
from .vault import VaultClient

_JSON = b"application/json"


class InboxError(ValueError):
    """A callback or expectation cannot be stored."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, newurl):
        return None


class InboxClient:
    """Authenticated HTTPS requestor for the configured one-shot result inbox."""

    def __init__(self, origin: str, requestor_token: str, ca_file: str | None = None) -> None:
        try:
            self.origin = canonical_inbox_origin(origin)
        except PacketError as error:
            raise InboxError("callback inbox origin is invalid") from error
        if (
            not isinstance(requestor_token, str)
            or not requestor_token
            or any(character.isspace() or ord(character) < 32 for character in requestor_token)
        ):
            raise InboxError("callback inbox credential is invalid")
        self._authorization = f"Bearer {requestor_token}"
        try:
            context = ssl.create_default_context(cafile=ca_file) if ca_file else None
            handlers = [_NoRedirect()]
            if context is not None:
                handlers.append(urllib.request.HTTPSHandler(context=context))
            self._opener = urllib.request.build_opener(*handlers)
        except (OSError, ssl.SSLError) as error:
            raise InboxError("callback inbox trust configuration is invalid") from error

    def _request(self, path: str, *, method: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8") if body is not None else None
        request = urllib.request.Request(  # noqa: S310 - origin is validated HTTPS; redirects are disabled
            f"{self.origin}{path}",
            data=data,
            headers={
                "Authorization": self._authorization,
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
            method=method,
        )
        try:
            with self._opener.open(request, timeout=10) as response:
                status = response.status
                raw = response.read(CALLBACK_BODY_LIMIT + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read(CALLBACK_BODY_LIMIT + 1)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise InboxError("callback inbox request outcome is unknown") from error
        if len(raw) > CALLBACK_BODY_LIMIT:
            raise InboxError("callback inbox response exceeds its limit")
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InboxError("callback inbox returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise InboxError("callback inbox response is invalid")
        return status, payload

    def register(self, job_id: str, kind: str, digest: str, deadline: datetime) -> None:
        """Register one callback expectation using only a token hash."""
        _key(job_id, kind)
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(deadline, datetime)
            or deadline.tzinfo is None
            or deadline.utcoffset() is None
            or deadline.astimezone(UTC) <= datetime.now(UTC)
        ):
            raise InboxError("callback expectation is invalid")
        status, payload = self._request(
            "/expectations",
            method="POST",
            body={
                "job_id": job_id,
                "kind": kind,
                "token_hash": digest,
                "deadline": deadline.astimezone(UTC).isoformat(),
            },
        )
        if status != 201 or payload.get("status") != "registered":
            raise InboxError("callback expectation was not registered")

    def fetch(self, job_id: str, kind: str) -> dict | None:
        """Fetch a bounded callback body for a canonical job and result kind."""
        _key(job_id, kind)
        status, payload = self._request(f"/jobs/{job_id}/{kind}", method="GET")
        if status == 404:
            return None
        if status != 200 or payload.get("job_id") != job_id or payload.get("kind") != kind:
            raise InboxError("callback inbox response does not match the request")
        stored = payload.get("stored")
        if type(stored) is not bool:
            raise InboxError("callback inbox response is invalid")
        if not stored:
            return {"stored": False}
        body_hash = payload.get("body_sha256")
        body = payload.get("body")
        if not isinstance(body_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", body_hash):
            raise InboxError("callback inbox response digest is invalid")
        if not isinstance(body, dict):
            raise InboxError("callback inbox body is invalid")
        return {"stored": True, "body_sha256": body_hash, "body": body}


class VaultInboxClient:
    """Lazy Vault-backed requestor client using the existing configured secret path."""

    def __init__(
        self,
        origin: str,
        secret_path: str,
        ca_file: str | None,
        vault_factory: Callable[[], VaultClient],
    ) -> None:
        self.origin = origin
        self.secret_path = secret_path
        self.ca_file = ca_file
        self.vault_factory = vault_factory
        self._client: InboxClient | None = None
        self._lock = threading.Lock()

    def _get_client(self) -> InboxClient:
        with self._lock:
            if self._client is None:
                try:
                    secret = self.vault_factory().read_secret(self.secret_path)
                except Exception as error:
                    raise InboxError("callback inbox credential is unavailable") from error
                token = secret.get("requestor_token")
                if not isinstance(token, str):
                    raise InboxError("callback inbox credential is unavailable")
                self._client = InboxClient(self.origin, token, self.ca_file)
            return self._client

    def register(self, job_id: str, kind: str, digest: str, deadline: datetime) -> None:
        self._get_client().register(job_id, kind, digest, deadline)

    def fetch(self, job_id: str, kind: str) -> dict | None:
        return self._get_client().fetch(job_id, kind)


@dataclass
class _Slot:
    token_hash: str
    deadline: datetime
    body: bytes | None = None
    body_hash: str | None = None


class Inbox:
    """Store the first valid body for each job path."""

    def __init__(self, requestor_token: str) -> None:
        if not requestor_token:
            raise InboxError("requestor credential is missing")
        self._requestor = requestor_token.encode("utf-8")
        self._slots: dict[tuple[str, str], _Slot] = {}
        self._lock = threading.Lock()

    def requestor_authorized(self, header: str | None) -> bool:
        return _bearer_matches(header, self._requestor)

    def register(self, job_id: str, kind: str, token_hash: str, deadline: datetime) -> None:
        key = _key(job_id, kind)
        if (
            not isinstance(token_hash, str)
            or len(token_hash) != 64
            or deadline.tzinfo is None
            or deadline.astimezone(UTC) <= datetime.now(UTC)
        ):
            raise InboxError("callback expectation is invalid")
        with self._lock:
            if key in self._slots:
                raise InboxError("callback expectation already exists")
            self._slots[key] = _Slot(token_hash, deadline.astimezone(UTC))

    def submit(
        self,
        job_id: str,
        kind: str,
        header: str | None,
        body: bytes,
        *,
        now: datetime,
    ) -> int:
        """Return the HTTP status for one worker POST."""
        if len(body) > CALLBACK_BODY_LIMIT:
            raise InboxError("callback body exceeds its limit")
        key = _key(job_id, kind)
        digest = hashlib.sha256(body).hexdigest()
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or now.astimezone(UTC) > slot.deadline:
                return 401
            presented = _bearer_value(header)
            if presented is None or not hmac.compare_digest(
                hashlib.sha256(presented).hexdigest(), slot.token_hash
            ):
                return 401
            if slot.body is None:
                slot.body = body
                slot.body_hash = digest
                return 200
            if slot.body_hash == digest:
                return 200
            return 409

    def read(self, job_id: str, kind: str) -> dict[str, Any] | None:
        key = _key(job_id, kind)
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                return None
            payload: dict[str, Any] = {
                "job_id": job_id,
                "kind": kind,
                "stored": slot.body is not None,
            }
            if slot.body is not None and slot.body_hash is not None:
                payload["body_sha256"] = slot.body_hash
                payload["body"] = json.loads(slot.body.decode("utf-8"))
            return payload


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _key(job_id: str, kind: str) -> tuple[str, str]:
    try:
        canonical = str(UUID(job_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise InboxError("job ID must be a canonical UUID") from error
    if canonical != job_id or kind not in ("result", "status"):
        raise InboxError("callback path is invalid")
    return canonical, kind


def _bearer_value(header: str | None) -> bytes | None:
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ")
    if not token or token != token.strip() or " " in token:
        return None
    return token.encode("utf-8")


def _bearer_matches(header: str | None, expected: bytes) -> bool:
    presented = _bearer_value(header)
    if presented is None:
        return False
    return hmac.compare_digest(presented, expected)


class _Handler(BaseHTTPRequestHandler):
    server: InboxServer

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length < 0 or length > CALLBACK_BODY_LIMIT:
            self._send(413, {"error": "limit"})
            return
        raw = self.rfile.read(length)
        inbox = self.server.inbox
        header = self.headers.get("Authorization")
        try:
            kind, job_id = _route(self.path)
        except InboxError:
            if self.path != "/expectations":
                self._send(404, {"error": "not_found"})
                return
            if not inbox.requestor_authorized(header):
                self._send(401, {"error": "unauthorized"})
                return
            self._register(raw)
            return
        try:
            status = inbox.submit(job_id, kind, header, raw, now=datetime.now(UTC))
        except (InboxError, PacketError):
            self._send(400, {"error": "invalid"})
            return
        self._send(status, {"status": "stored" if status == 200 else "rejected"})

    def do_GET(self) -> None:  # noqa: N802
        if not self.server.inbox.requestor_authorized(self.headers.get("Authorization")):
            self._send(401, {"error": "unauthorized"})
            return
        try:
            kind, job_id = _route(self.path)
            payload = self.server.inbox.read(job_id, kind)
        except InboxError:
            self._send(404, {"error": "not_found"})
            return
        if payload is None:
            self._send(404, {"error": "not_found"})
            return
        self._send(200, payload)

    def _register(self, raw: bytes) -> None:
        try:
            document = json.loads(raw.decode("utf-8"))
            deadline = datetime.fromisoformat(str(document["deadline"]).replace("Z", "+00:00"))
            self.server.inbox.register(
                str(document["job_id"]),
                str(document["kind"]),
                str(document["token_hash"]),
                deadline,
            )
        except (InboxError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid"})
            return
        self._send(201, {"status": "registered"})

    def _send(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def _route(path: str) -> tuple[str, str]:
    parts = urlsplit(path)
    if parts.query or parts.fragment:
        raise InboxError("callback path is invalid")
    pieces = [piece for piece in parts.path.split("/") if piece]
    if len(pieces) != 3 or pieces[0] != "jobs" or pieces[2] not in ("result", "status"):
        raise InboxError("callback path is invalid")
    return pieces[2], pieces[1]


class InboxServer(ThreadingHTTPServer):
    def __init__(self, inbox: Inbox, host: str = "127.0.0.1", port: int = 0) -> None:
        if host != "127.0.0.1":
            raise InboxError("inbox bind address must be loopback")
        self.inbox = inbox
        super().__init__((host, port), _Handler)
