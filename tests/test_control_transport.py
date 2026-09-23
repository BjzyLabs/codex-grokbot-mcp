"""GitHub control-repository and Grok Bot webhook contracts."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import pytest

from codex_grokbot_mcp.control import (
    ArtifactPR,
    ControlError,
    GitHubControl,
    TokenScopeError,
    WebhookTransport,
    WebhookUncertain,
)

REPO = "example-org/artifact-control"
SECRET = {
    "github_app_id": "17",
    "github_app_installation_id": "42",
    "github_app_private_key": "synthetic-test-key",
}


class VaultStub:
    def read_secret(self, path: str) -> dict:
        assert path == "apps/worker"
        return SECRET


class Reply(io.BytesIO):
    def __init__(self, value: object, status: int = 200) -> None:
        super().__init__(json.dumps(value).encode())
        self.status = status

    def __enter__(self) -> Reply:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def token_reply(*, repositories: list[str] | None = None, minutes: int = 60) -> dict:
    return {
        "token": "synthetic-installation-token",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(),
        "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        "repositories": [{"full_name": name} for name in (repositories or [REPO])],
    }


def test_mint_requests_exact_control_repo_and_permissions(tmp_path: Path) -> None:
    captured = {}

    def open_request(request, timeout=0, **_kwargs):
        captured["method"] = request.get_method()
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        return Reply(token_reply(), status=201)

    (tmp_path / "ca.pem").write_text("synthetic CA fixture")
    control = GitHubControl(VaultStub(), "apps/worker", REPO, ca_cert_file=tmp_path / "ca.pem")
    with (
        mock.patch("codex_grokbot_mcp.control.sign_app_jwt", return_value="signed-jwt"),
        mock.patch("ssl.create_default_context", return_value=object()),
        mock.patch("urllib.request.urlopen", side_effect=open_request),
    ):
        token = control.mint_worker_token()
    assert token.value == "synthetic-installation-token"
    assert "synthetic-installation-token" not in repr(token)
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/app/installations/42/access_tokens")
    assert captured["body"] == {
        "repositories": ["artifact-control"],
        "permissions": {"contents": "write", "pull_requests": "write"},
    }


def test_mint_rejects_wider_scope_and_revokes() -> None:
    control = GitHubControl(VaultStub(), "apps/worker", REPO)
    with (
        mock.patch("codex_grokbot_mcp.control.sign_app_jwt", return_value="signed-jwt"),
        mock.patch(
            "urllib.request.urlopen",
            return_value=Reply(token_reply(repositories=[REPO, "example-org/other"]), status=201),
        ),
        mock.patch.object(control, "revoke_token") as revoke,
    ):
        with pytest.raises(TokenScopeError):
            control.mint_worker_token()
    revoke.assert_called_once_with("synthetic-installation-token")


def test_mint_rejects_token_too_close_to_expiry_and_revokes() -> None:
    control = GitHubControl(VaultStub(), "apps/worker", REPO)
    with (
        mock.patch("codex_grokbot_mcp.control.sign_app_jwt", return_value="signed-jwt"),
        mock.patch(
            "urllib.request.urlopen", return_value=Reply(token_reply(minutes=30), status=201)
        ),
        mock.patch.object(control, "revoke_token") as revoke,
    ):
        with pytest.raises(TokenScopeError):
            control.mint_worker_token()
    revoke.assert_called_once()


def test_reads_only_expected_open_draft_pr_at_immutable_head() -> None:
    control = GitHubControl(VaultStub(), "apps/worker", REPO)
    head_sha = "a" * 40
    artifact = {"schema_version": "v1", "job_id": "job", "patch": "text"}
    requested = []

    def open_request(request, timeout=0, **_kwargs):
        requested.append(request.full_url)
        if "/pulls?" in request.full_url:
            return Reply(
                [
                    {
                        "number": 8,
                        "state": "open",
                        "draft": True,
                        "head": {
                            "ref": "grokbot/job-coding-1234abcd",
                            "sha": head_sha,
                            "repo": {"full_name": REPO},
                        },
                        "base": {"ref": "main", "repo": {"full_name": REPO}},
                    }
                ]
            )
        return Reply(
            {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(json.dumps(artifact).encode()).decode(),
            }
        )

    with mock.patch("urllib.request.urlopen", side_effect=open_request):
        pr = control.find_artifact_pr("token", "grokbot/job-coding-1234abcd")
        assert pr == ArtifactPR(8, head_sha)
        assert (
            control.read_artifact("token", "artifacts/patch-1234abcd.json", pr.head_sha) == artifact
        )
    assert requested[1].endswith("?ref=" + head_sha)


def test_rejects_non_draft_or_foreign_pr() -> None:
    control = GitHubControl(VaultStub(), "apps/worker", REPO)
    with mock.patch(
        "urllib.request.urlopen",
        return_value=Reply(
            [
                {
                    "number": 8,
                    "state": "open",
                    "draft": False,
                    "head": {
                        "ref": "grokbot/job-coding-1234abcd",
                        "sha": "a" * 40,
                        "repo": {"full_name": "example-org/foreign"},
                    },
                    "base": {"ref": "main", "repo": {"full_name": REPO}},
                }
            ]
        ),
    ):
        assert control.find_artifact_pr("token", "grokbot/job-coding-1234abcd") is None


def test_webhook_timeout_is_uncertain_and_never_retried() -> None:
    webhook = WebhookTransport("https://worker.example.invalid/hook", "synthetic-sender-key")
    with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")) as send:
        with pytest.raises(WebhookUncertain):
            webhook.dispatch({"job_id": "job"})
    send.assert_called_once()


def test_webhook_rejects_http_and_non_https_without_exposing_key() -> None:
    with pytest.raises(ControlError):
        WebhookTransport("http://worker.example.invalid/hook", "synthetic-sender-key")
    webhook = WebhookTransport("https://worker.example.invalid/hook", "synthetic-sender-key")
    error = urllib.error.HTTPError(
        "https://worker.example.invalid", 500, "error", {}, io.BytesIO(b"secret")
    )
    with mock.patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(WebhookUncertain) as caught:
            webhook.dispatch({"job_id": "job"})
    assert "synthetic-sender-key" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_webhook_uses_explicit_verified_ca_bundle(tmp_path: Path) -> None:
    ca_file = tmp_path / "public-ca.pem"
    ca_file.write_text("synthetic CA fixture")
    webhook = WebhookTransport(
        "https://worker.example.invalid/hook", "synthetic-sender-key", ca_cert_file=ca_file
    )
    context = object()
    seen = []

    class WebhookReply:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            return b""

    def open_request(_request, timeout=0, **kwargs):
        seen.append(kwargs.get("context"))
        return WebhookReply()

    with (
        mock.patch("ssl.create_default_context", return_value=context) as defaults,
        mock.patch("urllib.request.urlopen", side_effect=open_request),
    ):
        webhook.dispatch({"job_id": "job"})
    defaults.assert_called_once_with(cafile=str(ca_file))
    assert seen == [context]


def test_real_app_jwt_signature_uses_in_memory_private_key(tmp_path: Path) -> None:
    import subprocess

    from codex_grokbot_mcp.control import sign_app_jwt

    generated = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        capture_output=True,
        check=True,
    )
    private_key = generated.stdout
    public_key = subprocess.run(
        ["openssl", "pkey", "-pubout"], input=private_key, capture_output=True, check=True
    ).stdout
    token = sign_app_jwt("17", private_key.decode("ascii"))
    header, claims, signature = token.split(".")
    public_path = tmp_path / "public.pem"
    signature_path = tmp_path / "signature.bin"
    public_path.write_bytes(public_key)
    signature_path.write_bytes(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)))
    verified = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_path),
            "-signature",
            str(signature_path),
        ],
        input=f"{header}.{claims}".encode("ascii"),
        capture_output=True,
        check=True,
    )
    assert b"Verified OK" in verified.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == ["public.pem", "signature.bin"]


def test_control_repository_rejects_dot_segments() -> None:
    with pytest.raises(ControlError):
        GitHubControl(VaultStub(), "apps/worker", "../artifact-control")


def test_unexpected_token_status_revokes_issued_token() -> None:
    control = GitHubControl(VaultStub(), "apps/worker", REPO)
    with (
        mock.patch("codex_grokbot_mcp.control.sign_app_jwt", return_value="signed-jwt"),
        mock.patch("urllib.request.urlopen", return_value=Reply(token_reply(), status=200)),
        mock.patch.object(control, "revoke_token") as revoke,
    ):
        with pytest.raises(ControlError):
            control.mint_worker_token()
    revoke.assert_called_once_with("synthetic-installation-token")
