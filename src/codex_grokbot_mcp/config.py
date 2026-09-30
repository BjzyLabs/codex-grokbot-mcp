"""Strict owner-only configuration for the one Grok Bot webhook requestor."""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .deliver import PacketError, canonical_inbox_origin

CURRENT_VERSION = 2
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
    if not isinstance(value, str):
        raise ConfigError("webhook URL is invalid")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ConfigError("webhook URL must be HTTPS without embedded credentials")
    return value


@dataclass(frozen=True)
class Config:
    """Everything one requestor needs; the two secrets never appear in repr()."""

    job_database: Path
    inbox_base_url: str
    webhook_url: str
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
        if not isinstance(source, dict) or set(source) != CONFIG_KEYS:
            raise ConfigError("configuration has missing or unknown fields")
        if type(source["version"]) is not int or source["version"] != CURRENT_VERSION:
            raise ConfigError("configuration version is unsupported")
        return cls(
            job_database=_absolute_path(source["job_database"], label="job database"),
            inbox_base_url=_inbox_origin(source["inbox_base_url"]),
            webhook_url=_webhook_url(source["webhook_url"]),
            inbox_requestor_token=_secret(
                source["inbox_requestor_token"], label="callback inbox credential"
            ),
            sender_key=_secret(source["sender_key"], label="webhook sender key"),
        )
