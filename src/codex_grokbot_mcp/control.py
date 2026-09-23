"""Vault-backed GitHub control transport and one Grok Bot webhook path.

Installation tokens and sender keys stay in process memory. Responses are untrusted.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .vault import MAX_JOB_DURATION, VaultClient

GITHUB_API = "https://api.github.com"
TOKEN_PERMISSIONS = {"contents": "write", "pull_requests": "write"}
MAX_API_BYTES = 2 * 1024 * 1024
MAX_WEBHOOK_BYTES = 2 * 1024 * 1024
MAX_ARTIFACT_BYTES = 1536 * 1024
TOKEN_MINIMUM = MAX_JOB_DURATION + timedelta(minutes=5)
REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
BRANCH_PATTERN = re.compile(r"grokbot/job-coding-[0-9a-f]{8}\Z")
ARTIFACT_PATTERN = re.compile(r"artifacts/patch-[0-9a-f]{8}\.json\Z")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}\Z")


class ControlError(RuntimeError):
    """A transport response or configuration cannot be trusted."""


class GitHubHTTPError(ControlError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"GitHub returned HTTP {status}")


class GitHubUnavailable(ControlError):
    """A GitHub operation failed with uncertain network outcome."""


class TokenScopeError(ControlError):
    """GitHub did not grant exactly the requested worker authority."""


class WebhookUncertain(ControlError):
    """The webhook may have accepted the job; reconcile before retry or release."""


@dataclass(frozen=True)
class InstallationToken:
    value: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class ArtifactPR:
    number: int
    head_sha: str


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Sign a short-lived App JWT with OpenSSL; never write the PEM to disk."""
    if not app_id.isdecimal() or not private_key_pem.startswith("-----BEGIN "):
        raise ControlError("GitHub App signing credentials are invalid")
    now = int(time.time())
    header = _base64url(b'{"alg":"RS256","typ":"JWT"}')
    claims = _base64url(
        json.dumps(
            {"iat": now - 60, "exp": now + 540, "iss": app_id}, separators=(",", ":")
        ).encode()
    )
    message = f"{header}.{claims}".encode("ascii")
    openssl = shutil.which("openssl")
    if openssl is None or not os.path.isabs(openssl):
        raise ControlError("OpenSSL is unavailable for GitHub App signing")
    key_read, key_write = os.pipe()
    try:
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed argv, private key via pipe
                [openssl, "dgst", "-sha256", "-sign", f"/dev/fd/{key_read}"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(key_read,),
            )
        except OSError as error:
            os.close(key_write)
            raise ControlError("OpenSSL is unavailable for GitHub App signing") from error
    finally:
        os.close(key_read)
    try:
        with os.fdopen(key_write, "wb") as handle:
            handle.write(private_key_pem.encode("ascii"))
        signature, _error = process.communicate(message, timeout=15)
    except (OSError, UnicodeEncodeError, subprocess.TimeoutExpired) as error:
        process.kill()
        process.communicate()
        raise ControlError("GitHub App JWT signing failed") from error
    if process.returncode != 0 or not signature:
        raise ControlError("GitHub App JWT signing failed")
    return f"{header}.{claims}.{_base64url(signature)}"


@dataclass
class GitHubControl:
    vault: VaultClient
    app_secret_path: str
    control_repository: str
    ca_cert_file: Path | None = None

    def __post_init__(self) -> None:
        if not REPO_PATTERN.fullmatch(self.control_repository) or any(
            part in (".", "..") for part in self.control_repository.split("/")
        ):
            raise ControlError("control repository must be an owner/name slug")
        if not isinstance(self.app_secret_path, str) or not self.app_secret_path:
            raise ControlError("GitHub App Vault path is required")
        if self.ca_cert_file is not None and not self.ca_cert_file.is_file():
            raise ControlError("GitHub CA bundle file is unavailable")

    def _request(
        self, endpoint: str, *, method: str = "GET", auth: str, payload: dict | None = None
    ) -> tuple[int, Any]:
        request = urllib.request.Request(  # noqa: S310
            f"{GITHUB_API}{endpoint}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {auth}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            method=method,
        )
        options = {}
        if self.ca_cert_file is not None:
            options["context"] = ssl.create_default_context(cafile=str(self.ca_cert_file))
        try:
            with urllib.request.urlopen(request, timeout=30, **options) as response:  # noqa: S310
                status = response.status
                raw = response.read(MAX_API_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise GitHubHTTPError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise GitHubUnavailable("GitHub request outcome is uncertain") from error
        if len(raw) > MAX_API_BYTES:
            raise ControlError("GitHub response exceeds size limit")
        if not raw:
            return status, None
        try:
            return status, json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlError("GitHub response is malformed") from error

    def mint_worker_token(self) -> InstallationToken:
        secret = self.vault.read_secret(self.app_secret_path)
        app_id = secret.get("github_app_id")
        installation_id = secret.get("github_app_installation_id")
        pem = secret.get("github_app_private_key")
        if not all(isinstance(item, str) and item for item in (app_id, installation_id, pem)):
            raise ControlError("GitHub App secret is incomplete")
        if not installation_id.isdecimal():
            raise ControlError("GitHub App installation ID is invalid")
        jwt = sign_app_jwt(app_id, pem)
        status, response = self._request(
            f"/app/installations/{installation_id}/access_tokens",
            method="POST",
            auth=jwt,
            payload={
                "repositories": [self.control_repository.split("/", 1)[1]],
                "permissions": TOKEN_PERMISSIONS,
            },
        )
        if status != 201 or not isinstance(response, dict):
            if isinstance(response, dict) and isinstance(response.get("token"), str):
                self.revoke_token(response["token"])
            raise ControlError("GitHub did not confirm an installation token")
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise ControlError("GitHub issued no installation token")
        try:
            result = self._validated_token(token, response)
        except ControlError as error:
            try:
                self.revoke_token(token)
            except ControlError as revoke_error:
                raise TokenScopeError(
                    "token scope invalid and revocation uncertain"
                ) from revoke_error
            raise TokenScopeError("token scope or lifetime is invalid") from error
        return result

    def _validated_token(self, token: str, response: dict) -> InstallationToken:
        repositories = response.get("repositories")
        if (
            not isinstance(repositories, list)
            or len(repositories) != 1
            or not isinstance(repositories[0], dict)
            or repositories[0].get("full_name") != self.control_repository
        ):
            raise TokenScopeError("installation token repository scope is invalid")
        permissions = response.get("permissions")
        if not isinstance(permissions, dict) or any(
            permissions.get(key) != value for key, value in TOKEN_PERMISSIONS.items()
        ):
            raise TokenScopeError("installation token permissions are invalid")
        if any(
            key not in (*TOKEN_PERMISSIONS, "metadata") or (key == "metadata" and value != "read")
            for key, value in permissions.items()
        ):
            raise TokenScopeError("installation token has excess permissions")
        expiry = response.get("expires_at")
        if not isinstance(expiry, str):
            raise TokenScopeError("installation token expiry is absent")
        try:
            expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError as error:
            raise TokenScopeError("installation token expiry is invalid") from error
        if (
            expires_at.tzinfo is None
            or expires_at.astimezone(UTC) - datetime.now(UTC) < TOKEN_MINIMUM
        ):
            raise TokenScopeError("installation token expires too soon")
        return InstallationToken(token, expires_at.astimezone(UTC))

    def revoke_token(self, token: str) -> None:
        status, _body = self._request("/installation/token", method="DELETE", auth=token)
        if status != 204:
            raise ControlError("GitHub did not confirm token revocation")

    def find_artifact_pr(self, token: str, branch: str) -> ArtifactPR | None:
        if not BRANCH_PATTERN.fullmatch(branch):
            raise ControlError("artifact branch name is invalid")
        owner = self.control_repository.split("/", 1)[0]
        query = urllib.parse.urlencode(
            {"state": "all", "head": f"{owner}:{branch}", "per_page": 10}
        )
        status, response = self._request(
            f"/repos/{self.control_repository}/pulls?{query}", auth=token
        )
        if status != 200 or not isinstance(response, list):
            raise ControlError("GitHub PR listing is malformed")
        for entry in response:
            if not isinstance(entry, dict):
                continue
            head, base = entry.get("head"), entry.get("base")
            if not isinstance(head, dict) or not isinstance(base, dict):
                continue
            head_repo, base_repo = head.get("repo"), base.get("repo")
            if not isinstance(head_repo, dict) or not isinstance(base_repo, dict):
                continue
            sha, number = head.get("sha"), entry.get("number")
            if (
                entry.get("state") == "open"
                and entry.get("draft") is True
                and head.get("ref") == branch
                and head_repo.get("full_name") == self.control_repository
                and base.get("ref") == "main"
                and base_repo.get("full_name") == self.control_repository
                and isinstance(number, int)
                and number > 0
                and isinstance(sha, str)
                and SHA_PATTERN.fullmatch(sha)
            ):
                return ArtifactPR(number, sha)
        return None

    def read_artifact(self, token: str, path: str, head_sha: str) -> dict:
        if not ARTIFACT_PATTERN.fullmatch(path) or not SHA_PATTERN.fullmatch(head_sha):
            raise ControlError("artifact path or immutable ref is invalid")
        query = urllib.parse.urlencode({"ref": head_sha})
        status, response = self._request(
            f"/repos/{self.control_repository}/contents/{path}?{query}", auth=token
        )
        if status != 200 or not isinstance(response, dict):
            raise ControlError("GitHub artifact response is malformed")
        content = response.get("content")
        if response.get("type") != "file" or response.get("encoding") != "base64":
            raise ControlError("GitHub artifact is not a base64 file")
        if not isinstance(content, str):
            raise ControlError("GitHub artifact content is absent")
        try:
            raw = base64.b64decode(content.replace("\n", ""), validate=True)
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise ControlError("artifact exceeds size limit")
            artifact = json.loads(raw.decode("utf-8"))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControlError("GitHub artifact content is malformed") from error
        if not isinstance(artifact, dict):
            raise ControlError("GitHub artifact is not an object")
        return artifact


@dataclass(frozen=True)
class WebhookTransport:
    url: str
    sender_key: str = field(repr=False)
    ca_cert_file: Path | None = None

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlparse(self.url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.fragment:
            raise ControlError("worker webhook URL must be HTTPS without embedded credentials")
        if not self.sender_key:
            raise ControlError("worker webhook sender key is missing")
        if self.ca_cert_file is not None and not self.ca_cert_file.is_file():
            raise ControlError("webhook CA bundle file is unavailable")

    def dispatch(self, packet: dict) -> None:
        body = json.dumps(packet, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_WEBHOOK_BYTES:
            raise ControlError("worker packet exceeds size limit")
        request = urllib.request.Request(  # noqa: S310
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.sender_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        options = {}
        if self.ca_cert_file is not None:
            options["context"] = ssl.create_default_context(cafile=str(self.ca_cert_file))
        try:
            with urllib.request.urlopen(request, timeout=30, **options) as response:  # noqa: S310
                if response.status < 200 or response.status >= 300:
                    raise WebhookUncertain(f"webhook returned HTTP {response.status}")
                response.read(1024)
        except urllib.error.HTTPError as error:
            raise WebhookUncertain(f"webhook returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise WebhookUncertain("webhook outcome is uncertain") from error
