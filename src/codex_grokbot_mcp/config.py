"""Strict local configuration for Vault-only workers and workspace opt-in."""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control import REPO_PATTERN
from .vault import VaultClient, VaultError

TOP_KEYS = {
    "version",
    "vault_address",
    "vault_mount",
    "vault_role_id_file",
    "vault_secret_id_file",
    "vault_ca_file",
    "github_ca_file",
    "webhook_ca_file",
    "job_database",
    "control_repository",
    "workers",
    "workspaces",
}
WORKER_KEYS = {"webhook_secret_path", "app_secret_path", "lease_prefix", "lease_worker"}
WORKSPACE_KEYS = {"enabled", "workers"}
COMPONENT = re.compile(r"[A-Za-z0-9_.-]+\Z")
NESTED_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\Z")


class ConfigError(ValueError):
    """Configuration is missing, unsafe, or grants ambiguous authority."""


def _keys(value: Any, allowed: set[str], *, label: str) -> dict:
    if not isinstance(value, dict) or set(value) != allowed:
        raise ConfigError(f"{label} has missing or unknown fields")
    return value


def _component(value: Any, *, nested: bool = False) -> str:
    pattern = NESTED_PATH if nested else COMPONENT
    if (
        not isinstance(value, str)
        or not pattern.fullmatch(value)
        or any(part in (".", "..") for part in value.split("/"))
    ):
        raise ConfigError("Vault path or worker identifier is invalid")
    return value


def _absolute_path(value: Any, *, label: str) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise ConfigError(f"{label} must be an absolute path")
    return Path(value)


def _private_file(value: Any, *, label: str) -> Path:
    path = _absolute_path(value, label=label)
    try:
        details = path.lstat()
    except OSError as error:
        raise ConfigError(f"{label} credential or config file is unavailable") from error
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise ConfigError(f"{label} must be a private regular file owned by this user")
    return path


def _ca_file(value: Any, *, label: str) -> Path:
    path = _absolute_path(value, label=label)
    if not path.is_file():
        raise ConfigError(f"{label} CA bundle is unavailable")
    return path


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    webhook_secret_path: str
    app_secret_path: str
    lease_prefix: str
    lease_worker: str


@dataclass(frozen=True)
class WorkspaceRule:
    enabled: bool
    workers: frozenset[str]


@dataclass(frozen=True)
class Config:
    vault_address: str
    vault_mount: str
    vault_role_id_file: Path
    vault_secret_id_file: Path
    vault_ca_file: Path
    github_ca_file: Path
    webhook_ca_file: Path
    job_database: Path
    control_repository: str
    workers: dict[str, WorkerConfig]
    workspaces: dict[Path, WorkspaceRule]

    @classmethod
    def load(cls, path: Path) -> Config:
        config_file = _private_file(str(path), label="configuration")
        try:
            with config_file.open("rb") as handle:
                source = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise ConfigError("configuration TOML cannot be read") from error
        _keys(source, TOP_KEYS, label="configuration")
        if type(source["version"]) is not int or source["version"] != 1:
            raise ConfigError("configuration version is unsupported")
        mount = _component(source["vault_mount"])
        role = _private_file(source["vault_role_id_file"], label="AppRole role ID credential")
        secret = _private_file(source["vault_secret_id_file"], label="AppRole SecretID credential")
        vault_ca = _ca_file(source["vault_ca_file"], label="Vault")
        github_ca = _ca_file(source["github_ca_file"], label="GitHub")
        webhook_ca = _ca_file(source["webhook_ca_file"], label="webhook")
        database = _absolute_path(source["job_database"], label="job database")
        repository = source["control_repository"]
        if (
            not isinstance(repository, str)
            or not REPO_PATTERN.fullmatch(repository)
            or any(part in (".", "..") for part in repository.split("/"))
        ):
            raise ConfigError("control repository identity is invalid")
        try:
            client = VaultClient(source["vault_address"], role, secret, mount, vault_ca)
        except (VaultError, TypeError) as error:
            raise ConfigError("Vault connection configuration is invalid") from error
        workers_data = source["workers"]
        if not isinstance(workers_data, dict) or not workers_data:
            raise ConfigError("at least one explicit worker is required")
        workers: dict[str, WorkerConfig] = {}
        lease_keys: set[tuple[str, str]] = set()
        for worker_id, item in workers_data.items():
            _component(worker_id)
            _keys(item, WORKER_KEYS, label="worker")
            worker = WorkerConfig(
                worker_id,
                _component(item["webhook_secret_path"], nested=True),
                _component(item["app_secret_path"], nested=True),
                _component(item["lease_prefix"], nested=True),
                _component(item["lease_worker"]),
            )
            lease_key = (worker.lease_prefix, worker.lease_worker)
            if lease_key in lease_keys:
                raise ConfigError("two workers cannot share a lease key")
            lease_keys.add(lease_key)
            workers[worker_id] = worker
        workspace_data = source["workspaces"]
        if not isinstance(workspace_data, dict):
            raise ConfigError("workspace opt-in table is required")
        workspaces: dict[Path, WorkspaceRule] = {}
        for raw_root, item in workspace_data.items():
            root = _absolute_path(raw_root, label="workspace")
            if not root.is_dir() or root.resolve() != root:
                raise ConfigError("workspace opt-in path must be an existing canonical directory")
            _keys(item, WORKSPACE_KEYS, label="workspace")
            enabled = item["enabled"]
            allowed = item["workers"]
            if type(enabled) is not bool or not isinstance(allowed, list) or not allowed:
                raise ConfigError("workspace opt-in is incomplete")
            if any(not isinstance(name, str) or name not in workers for name in allowed):
                raise ConfigError("workspace references an unknown worker")
            if len(allowed) != len(set(allowed)) or root in workspaces:
                raise ConfigError("workspace worker grants are duplicated")
            workspaces[root] = WorkspaceRule(enabled, frozenset(allowed))
        return cls(
            client.address,
            mount,
            role,
            secret,
            vault_ca,
            github_ca,
            webhook_ca,
            database,
            repository,
            workers,
            workspaces,
        )

    def vault_client(self) -> VaultClient:
        return VaultClient(
            self.vault_address,
            self.vault_role_id_file,
            self.vault_secret_id_file,
            self.vault_mount,
            self.vault_ca_file,
        )

    def require_workspace(self, root: Path, worker_id: str) -> WorkerConfig:
        worker = self.workers.get(worker_id)
        if worker is None:
            raise ConfigError("worker is not configured")
        rule = self.workspaces.get(Path(root).resolve())
        if rule is None or not rule.enabled or worker_id not in rule.workers:
            raise ConfigError("workspace opt-in is required for this worker")
        return worker
