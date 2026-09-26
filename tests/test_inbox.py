from __future__ import annotations

import json
import threading
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from codex_grokbot_mcp.inbox import CALLBACK_BODY_LIMIT, Inbox, InboxError, InboxServer, token_hash

JOB_ID = "1234abcd-1234-4123-8123-123456789abc"
REQUESTOR = "requestor-fixture-token"
WORKER = "w" * 43


def _request(url: str, *, method: str = "GET", token: str, body: bytes | None = None):
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310
    request.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


@pytest.fixture
def server():
    inbox = Inbox(REQUESTOR)
    httpd = InboxServer(inbox)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield inbox, base
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def test_first_body_is_stored_replay_matches_and_a_different_body_conflicts(server) -> None:
    _inbox, base = server
    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    status, _payload = _request(
        f"{base}/expectations",
        method="POST",
        token=REQUESTOR,
        body=json.dumps(
            {
                "job_id": JOB_ID,
                "kind": "result",
                "token_hash": token_hash(WORKER),
                "deadline": deadline,
            }
        ).encode(),
    )
    assert status == 201
    body = b'{"status":"ok"}'
    first, _ = _request(f"{base}/jobs/{JOB_ID}/result", method="POST", token=WORKER, body=body)
    replay, _ = _request(f"{base}/jobs/{JOB_ID}/result", method="POST", token=WORKER, body=body)
    conflict, _ = _request(
        f"{base}/jobs/{JOB_ID}/result", method="POST", token=WORKER, body=b'{"status":"error"}'
    )
    assert (first, replay, conflict) == (200, 200, 409)
    stored, raw = _request(f"{base}/jobs/{JOB_ID}/result", token=REQUESTOR)
    assert stored == 200
    assert json.loads(raw)["body"] == {"status": "ok"}


def test_bad_or_expired_token_is_rejected(server) -> None:
    inbox, _base = server
    deadline = datetime.now(UTC) + timedelta(minutes=5)
    inbox.register(JOB_ID, "result", token_hash(WORKER), deadline)
    assert inbox.submit(JOB_ID, "result", "Bearer nope", b"{}", now=datetime.now(UTC)) == 401
    expired = inbox.submit(
        JOB_ID,
        "result",
        f"Bearer {WORKER}",
        b"{}",
        now=deadline + timedelta(seconds=1),
    )
    assert expired == 401
    with pytest.raises(InboxError):
        inbox.register(
            "not-a-uuid",
            "result",
            token_hash(WORKER),
            datetime.now(UTC) + timedelta(minutes=1),
        )


def test_oversized_body_is_rejected(server) -> None:
    _inbox, base = server
    deadline = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    _request(
        f"{base}/expectations",
        method="POST",
        token=REQUESTOR,
        body=json.dumps(
            {
                "job_id": JOB_ID,
                "kind": "status",
                "token_hash": token_hash(WORKER),
                "deadline": deadline,
            }
        ).encode(),
    )
    huge = b"{" + b"x" * (CALLBACK_BODY_LIMIT + 10)
    status, _ = _request(f"{base}/jobs/{JOB_ID}/status", method="POST", token=WORKER, body=huge)
    assert status == 413
