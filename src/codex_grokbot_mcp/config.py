"""Owner-only configuration with optional runtime Vault loading for X Bot."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .deliver import PacketError, canonical_inbox_origin

CURRENT_VERSION = 2
VAULT_VERSION = 3
VAULT_TIMEOUT = 20
CONFIG_KEYS = frozenset(
    {
        "version",
        "job_database",
        "inbox_base_url",
        "inbox_requestor_token",
        "webhook_url",
        "sender_key",
    }
)

VAULT_CONFIG_KEYS = frozenset(
    {"version", "job_database", "inbox_base_url", "vault_webhook_path", "vault_inbox_path"}
)


class ConfigError(ValueError):
    """Configuration is missing, unsafe, or unsupported."""


def _private_file(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    path = Path(value)
    try:
        details = path.lstat()
    except OSError as error:
        raise ConfigError(f"{label} is unavailable") from error
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise ConfigError(f"{label} must be a private regular file owned by this user")
    return path


def _absolute_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    return Path(value)


def _secret(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ConfigError(f"{label} is missing or unsafe")
    return value


def _inbox_origin(value: Any) -> str:
    try:
        return canonical_inbox_origin(value)
    except PacketError as error:
        raise ConfigError("callback inbox origin is invalid") from error


def _webhook_url(value: Any) -> str:
    if not isinstance(value, str) or any(
        character.isspace() or ord(character) < 32 for character in value
    ):
        raise ConfigError("webhook URL is invalid")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError:
        raise ConfigError("webhook URL is invalid") from None
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ConfigError("webhook URL must be HTTPS without embedded credentials")
    return value


def _vault_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value.split("/")) < 2
        or any(part in ("", ".", "..") or part.startswith("-") for part in value.split("/"))
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ConfigError("Vault path must name an exact mount and secret")
    return value


def _vault_json(arguments: list[str]) -> dict[str, Any]:
    """Capture all CLI output; errors must never expose Vault payloads."""
    executable = shutil.which("vault")
    if executable is None:
        raise ConfigError("Vault CLI is unavailable")
    try:
        result = subprocess.run(  # noqa: S603 -- resolved CLI, validated path, no shell
            [executable, *arguments], capture_output=True, text=True, timeout=VAULT_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        raise ConfigError("Vault CLI is unavailable or timed out") from None
    if result.returncode:
        raise ConfigError("Vault read failed; check session and exact-path access")
    try:
        body = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ConfigError("Vault returned an invalid response") from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise ConfigError("Vault returned an invalid response")
    return body["data"]


def _vault_credentials(webhook_path: str, inbox_path: str) -> tuple[str, str, str]:
    """Use existing CLI authentication; never log in or persist fetched values."""
    address = os.environ.get("VAULT_ADDR", "")
    try:
        _webhook_url(address)
    except (ConfigError, ValueError):
        raise ConfigError("VAULT_ADDR must be a verified HTTPS endpoint") from None
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() not in ("", "false", "0"):
        raise ConfigError("Vault TLS verification cannot be disabled")
    session = _vault_json(["token", "lookup", "-format=json"])
    policies = session.get("policies")
    ttl = session.get("ttl")
    if (
        not isinstance(policies, list)
        or not policies
        or any(not isinstance(policy, str) for policy in policies)
        or "root" in policies
        or type(ttl) is not int
        or ttl <= 0
    ):
        raise ConfigError("Vault requires an active non-root session with a positive TTL")
    webhook = _vault_json(["kv", "get", "-format=json", webhook_path]).get("data")
    if not isinstance(webhook, dict):
        raise ConfigError("Vault webhook entry is invalid")
    webhook_url = _webhook_url(webhook.get("webhook_url"))
    sender_key = _secret(webhook.get("sender_key"), label="webhook sender key")
    inbox = _vault_json(["kv", "get", "-format=json", inbox_path]).get("data")
    if not isinstance(inbox, dict):
        raise ConfigError("Vault inbox entry is invalid")
    requestor_token = _secret(inbox.get("requestor_token"), label="callback inbox credential")
    return webhook_url, sender_key, requestor_token


@dataclass(frozen=True)
class Config:
    """Runtime values; credentials and the private webhook URL never appear in repr()."""

    job_database: Path
    inbox_base_url: str
    webhook_url: str = field(repr=False)
    inbox_requestor_token: str = field(repr=False)
    sender_key: str = field(repr=False)

    @classmethod
    def load(cls, path: Path) -> Config:
        config_file = _private_file(str(path), label="configuration")
        try:
            with config_file.open("rb") as handle:
                source = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError("configuration TOML cannot be read") from error
        version = source.get("version")
        if type(version) is not int or version not in (CURRENT_VERSION, VAULT_VERSION):
            raise ConfigError("configuration version is unsupported")
        expected = VAULT_CONFIG_KEYS if version == VAULT_VERSION else CONFIG_KEYS
        if set(source) != expected:
            raise ConfigError("configuration has missing or unknown fields")
        database = _absolute_path(source["job_database"], label="job database")
        origin = _inbox_origin(source["inbox_base_url"])
        if version == VAULT_VERSION:
            webhook_path = _vault_path(source["vault_webhook_path"])
            inbox_path = _vault_path(source["vault_inbox_path"])
            webhook_url, sender_key, requestor_token = _vault_credentials(webhook_path, inbox_path)
        else:
            webhook_url = _webhook_url(source["webhook_url"])
            sender_key = _secret(source["sender_key"], label="webhook sender key")
            requestor_token = _secret(
                source["inbox_requestor_token"], label="callback inbox credential"
            )
        return cls(
            job_database=database,
            inbox_base_url=origin,
            webhook_url=webhook_url,
            inbox_requestor_token=requestor_token,
            sender_key=sender_key,
        )
