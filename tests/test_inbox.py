from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

from support import JOB_ID, OTHER_JOB_ID, REQUESTOR_BEARER

from codex_grokbot_mcp.inbox import (
    CALLBACK_BODY_LIMIT,
    Inbox,
    InboxClient,
    InboxError,
    InboxServer,
    token_hash,
)

WORKER_TOKEN = "w" * 43


def request(
    url: str, *, method: str = "GET", bearer: str, body: bytes | None = None
) -> tuple[int, bytes]:
    message = urllib.request.Request(url, data=body, method=method)  # noqa: S310
    message.add_header("Authorization", f"Bearer {bearer}")
    if body is not None:
        message.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(message) as response:  # noqa: S310
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class InboxServerTests(unittest.TestCase):
    """Exercise the real loopback inbox that the deployed requestor talks to."""

    def setUp(self) -> None:
        self.inbox = Inbox(REQUESTOR_BEARER)
        try:
            self.httpd = InboxServer(self.inbox)
        except PermissionError as error:  # pragma: no cover - restricted sandboxes
            raise unittest.SkipTest(f"loopback sockets are unavailable: {error}") from error
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()

    def register(self, job_id: str = JOB_ID, kind: str = "result") -> int:
        status, _payload = request(
            f"{self.base}/expectations",
            method="POST",
            bearer=REQUESTOR_BEARER,
            body=json.dumps(
                {
                    "job_id": job_id,
                    "kind": kind,
                    "token_hash": token_hash(WORKER_TOKEN),
                    "deadline": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                }
            ).encode("utf-8"),
        )
        return status

    def test_first_body_is_stored_replay_matches_and_a_different_body_conflicts(self) -> None:
        self.assertEqual(self.register(), 201)
        body = b'{"status":"ok"}'

        first, _ = request(
            f"{self.base}/jobs/{JOB_ID}/result", method="POST", bearer=WORKER_TOKEN, body=body
        )
        replay, _ = request(
            f"{self.base}/jobs/{JOB_ID}/result", method="POST", bearer=WORKER_TOKEN, body=body
        )
        conflict, _ = request(
            f"{self.base}/jobs/{JOB_ID}/result",
            method="POST",
            bearer=WORKER_TOKEN,
            body=b'{"status":"error"}',
        )

        self.assertEqual((first, replay, conflict), (200, 200, 409))
        stored, raw = request(f"{self.base}/jobs/{JOB_ID}/result", bearer=REQUESTOR_BEARER)
        self.assertEqual(stored, 200)
        self.assertEqual(json.loads(raw)["body"], {"status": "ok"})
        self.assertEqual(json.loads(raw)["body_sha256"], token_hash('{"status":"ok"}'))

    def test_unregistered_unknown_and_unauthorised_reads_are_rejected(self) -> None:
        self.assertEqual(
            request(f"{self.base}/jobs/{JOB_ID}/result", bearer=REQUESTOR_BEARER)[0], 404
        )
        self.assertEqual(request(f"{self.base}/jobs/{JOB_ID}/result", bearer=WORKER_TOKEN)[0], 401)
        self.assertEqual(
            request(f"{self.base}/jobs/{JOB_ID}/patch", bearer=REQUESTOR_BEARER)[0], 404
        )
        self.assertEqual(self.register(kind="result"), 201)
        self.assertEqual(
            request(f"{self.base}/jobs/{JOB_ID}/result", bearer="foreign-token")[0], 401
        )

    def test_registration_requires_the_requestor_token(self) -> None:
        status, _payload = request(
            f"{self.base}/expectations",
            method="POST",
            bearer="foreign-token",
            body=json.dumps({"job_id": JOB_ID, "kind": "result"}).encode("utf-8"),
        )

        self.assertEqual(status, 401)

    def test_bad_or_expired_worker_token_is_rejected(self) -> None:
        self.inbox.register(
            JOB_ID, "result", token_hash(WORKER_TOKEN), datetime.now(UTC) + timedelta(minutes=5)
        )
        expiry = datetime.now(UTC) + timedelta(minutes=5)

        self.assertEqual(
            self.inbox.submit(JOB_ID, "result", "Bearer nope", b"{}", now=datetime.now(UTC)), 401
        )
        self.assertEqual(
            self.inbox.submit(
                JOB_ID, "result", f"Bearer {WORKER_TOKEN}", b"{}", now=expiry + timedelta(seconds=1)
            ),
            401,
        )
        with self.assertRaises(InboxError):
            self.inbox.register("not-a-uuid", "result", token_hash(WORKER_TOKEN), datetime.now(UTC))

    def test_oversized_body_is_rejected(self) -> None:
        self.assertEqual(self.register(kind="status"), 201)
        huge = b"{" + b"x" * (CALLBACK_BODY_LIMIT + 10)

        status, _payload = request(
            f"{self.base}/jobs/{JOB_ID}/status", method="POST", bearer=WORKER_TOKEN, body=huge
        )

        self.assertEqual(status, 413)


class _Response:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int) -> bytes:
        return self._payload


class _Opener:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return self.response


class InboxClientTests(unittest.TestCase):
    def client(self, response: _Response) -> tuple[InboxClient, _Opener]:
        client = InboxClient("https://inbox.example.invalid", REQUESTOR_BEARER)
        opener = _Opener(response)
        client._opener = opener
        return client, opener

    def test_registration_sends_only_the_token_hash(self) -> None:
        client, opener = self.client(_Response(201, b'{"status":"registered"}'))

        client.register(
            JOB_ID, "result", token_hash(WORKER_TOKEN), datetime.now(UTC) + timedelta(minutes=2)
        )

        self.assertEqual(opener.request.full_url, "https://inbox.example.invalid/expectations")
        self.assertEqual(opener.request.get_method(), "POST")
        self.assertEqual(opener.request.get_header("Authorization"), f"Bearer {REQUESTOR_BEARER}")
        self.assertEqual(opener.timeout, 10)
        self.assertNotIn(WORKER_TOKEN.encode(), opener.request.data)
        self.assertIn(token_hash(WORKER_TOKEN).encode(), opener.request.data)

    def test_registration_fails_closed_on_an_unexpected_response(self) -> None:
        client, _opener = self.client(_Response(401, b'{"error":"unauthorized"}'))

        with self.assertRaisesRegex(InboxError, "not registered"):
            client.register(
                JOB_ID, "result", token_hash(WORKER_TOKEN), datetime.now(UTC) + timedelta(minutes=2)
            )

    def test_fetch_normalises_missing_and_stored_bodies(self) -> None:
        missing, _opener = self.client(_Response(404, b'{"error":"not_found"}'))
        self.assertIsNone(missing.fetch(JOB_ID, "result"))

        digest = token_hash("body")
        stored, _opener = self.client(
            _Response(
                200,
                json.dumps(
                    {
                        "job_id": JOB_ID,
                        "kind": "result",
                        "stored": True,
                        "body_sha256": digest,
                        "body": {"ok": 1},
                    }
                ).encode("utf-8"),
            )
        )
        self.assertEqual(
            stored.fetch(JOB_ID, "result"),
            {"stored": True, "body_sha256": digest, "body": {"ok": 1}},
        )

        empty, _opener = self.client(
            _Response(
                200,
                json.dumps({"job_id": JOB_ID, "kind": "result", "stored": False}).encode("utf-8"),
            )
        )
        self.assertEqual(empty.fetch(JOB_ID, "result"), {"stored": False})

    def test_client_rejects_a_non_https_origin_and_unsafe_token(self) -> None:
        with self.assertRaisesRegex(InboxError, "origin"):
            InboxClient("http://inbox.example.invalid", REQUESTOR_BEARER)
        with self.assertRaisesRegex(InboxError, "credential"):
            InboxClient("https://inbox.example.invalid", "token with space")

    def test_client_rejects_a_response_for_another_job(self) -> None:
        client, _opener = self.client(
            _Response(
                200,
                json.dumps(
                    {
                        "job_id": OTHER_JOB_ID,
                        "kind": "result",
                        "stored": False,
                    }
                ).encode("utf-8"),
            )
        )

        with self.assertRaisesRegex(InboxError, "does not match"):
            client.fetch(JOB_ID, "result")


if __name__ == "__main__":
    unittest.main()
