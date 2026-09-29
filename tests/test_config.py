from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_grokbot_mcp.config import Config, ConfigError


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    for name in ("role_id", "secret_id"):
        credential = private / name
        credential.write_text("fixture-only")
        credential.chmod(0o600)
    ca = tmp_path / "trust.pem"
    ca.write_text("fixture CA")
    return workspace, ca


def config_text(workspace: Path, ca: Path) -> str:
    return f'''
version = 1
vault_address = "https://vault.example.invalid:8200"
vault_mount = "secret"
vault_role_id_file = "{workspace.parent}/private/role_id"
vault_secret_id_file = "{workspace.parent}/private/secret_id"
vault_ca_file = "{ca}"
github_ca_file = "{ca}"
webhook_ca_file = "{ca}"
job_database = "{workspace.parent}/private/jobs.sqlite3"
control_repository = "example/private-control"

[workers.coder]
webhook_secret_path = "webhooks/coder"
app_secret_path = "github/coder"
lease_prefix = "leases"
lease_worker = "coder"

[workers.reviewer]
webhook_secret_path = "webhooks/reviewer"
app_secret_path = "github/reviewer"
lease_prefix = "leases"
lease_worker = "reviewer"

[workspaces.{json.dumps(str(workspace))}]
enabled = true
workers = ["coder"]
'''


def write_config(tmp_path: Path, data: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(data)
    path.chmod(0o600)
    return path


def test_explicit_workers_and_workspace_allowlist(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    config = Config.load(write_config(tmp_path, config_text(workspace, ca)))
    assert set(config.workers) == {"coder", "reviewer"}
    assert config.require_workspace(workspace, "coder").lease_worker == "coder"
    with pytest.raises(ConfigError, match="opt-in"):
        config.require_workspace(workspace, "reviewer")
    with pytest.raises(ConfigError, match="opt-in"):
        config.require_workspace(tmp_path / "another", "coder")
    with pytest.raises(ConfigError, match="worker"):
        config.require_workspace(workspace, "absent")


def test_disabled_workspace_is_denied(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    data = config_text(workspace, ca).replace("enabled = true", "enabled = false")
    config = Config.load(write_config(tmp_path, data))
    with pytest.raises(ConfigError, match="opt-in"):
        config.require_workspace(workspace, "coder")


@pytest.mark.parametrize(
    "replacement",
    [
        ('lease_worker = "reviewer"', 'lease_worker = "coder"'),
        ("[workers.reviewer]", '[workers.reviewer]\nmodel = "alternate"'),
        ("version = 1", 'version = 1\nsecret_backend = "other"'),
        ('webhook_secret_path = "webhooks/coder"', ""),
    ],
)
def test_duplicate_lease_unknown_fields_and_missing_paths_fail(
    paths, tmp_path: Path, replacement
) -> None:
    workspace, ca = paths
    original, updated = replacement
    with pytest.raises(ConfigError):
        Config.load(write_config(tmp_path, config_text(workspace, ca).replace(original, updated)))


def test_private_regular_config_and_trust_files_required(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    file = write_config(tmp_path, config_text(workspace, ca))
    file.chmod(0o644)
    with pytest.raises(ConfigError, match="private"):
        Config.load(file)
    file.chmod(0o600)
    ca.unlink()
    with pytest.raises(ConfigError, match="CA"):
        Config.load(file)


def test_missing_approle_file_is_rejected(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    (tmp_path / "private" / "secret_id").unlink()
    with pytest.raises(ConfigError, match="credential"):
        Config.load(write_config(tmp_path, config_text(workspace, ca)))


def diagnostic_config_text(workspace: Path, ca: Path, *, account: str = "supergrok") -> str:
    source = config_text(workspace, ca)
    source = source.replace(
        'lease_worker = "coder"\n',
        'lease_worker = "coder"\n'
        'job_types = ["coding"]\n'
        f'account_id = "{account}"\n'
        'diagnostic_chief_worker_id = "chief"\n'
        'diagnostic_target_bot = "devcoder"\n',
    )
    marker = f"[workspaces.{json.dumps(str(workspace))}]"
    source = source.replace(
        marker,
        "[workers.chief]\n"
        'webhook_secret_path = "webhooks/chief"\n'
        'app_secret_path = "github/chief"\n'
        'lease_prefix = "leases"\n'
        'lease_worker = "chief"\n'
        'job_types = ["x_query", "worker_diagnostic"]\n'
        f'account_id = "{account}"\n\n'
        f"{marker}",
    )
    return source


def test_diagnostic_route_requires_explicit_same_account_bot_mapping(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    config = Config.load(write_config(tmp_path, diagnostic_config_text(workspace, ca)))
    chief, target_bot = config.require_diagnostic_route("coder")
    assert chief.worker_id == "chief"
    assert target_bot == "devcoder"


def test_diagnostic_route_rejects_cross_account_or_missing_receiver(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    config = Config.load(
        write_config(tmp_path, diagnostic_config_text(workspace, ca, account="account-a"))
    )
    config.workers["chief"] = config.workers["chief"].__class__(
        "chief",
        "webhooks/chief",
        "github/chief",
        "leases",
        "chief",
        frozenset({"x_query", "worker_diagnostic"}),
        "account-b",
    )
    with pytest.raises(ConfigError, match="account mapping"):
        config.require_diagnostic_route("coder")


def test_x_query_worker_must_be_mapped_same_account_chief(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    config = Config.load(write_config(tmp_path, diagnostic_config_text(workspace, ca)))
    chief = config.require_x_query_worker("chief")
    assert chief.worker_id == "chief"

    with pytest.raises(ConfigError, match="Chief of Staff"):
        config.require_x_query_worker("coder")


def test_x_query_worker_rejects_unmapped_or_cross_account_chief(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    config = Config.load(write_config(tmp_path, diagnostic_config_text(workspace, ca)))
    with pytest.raises(ConfigError, match="Chief of Staff"):
        config.require_x_query_worker("reviewer")

    config.workers["chief"] = config.workers["chief"].__class__(
        "chief",
        "webhooks/chief",
        "github/chief",
        "leases",
        "chief",
        frozenset({"x_query", "worker_diagnostic"}),
        "account-b",
    )
    with pytest.raises(ConfigError, match="account mapping"):
        config.require_x_query_worker("chief")


def test_partial_diagnostic_route_is_rejected(paths, tmp_path: Path) -> None:
    workspace, ca = paths
    source = config_text(workspace, ca).replace(
        'lease_worker = "coder"\n',
        'lease_worker = "coder"\naccount_id = "supergrok"\ndiagnostic_chief_worker_id = "chief"\n',
    )
    with pytest.raises(ConfigError, match="incomplete"):
        Config.load(write_config(tmp_path, source))
