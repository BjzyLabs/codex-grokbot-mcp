from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import pytest

from codex_grokbot_mcp.vault import (
    LeaseConfigurationError,
    LeaseOwnershipError,
    LeaseUncertain,
    VaultClient,
    VaultError,
    VaultLeaseStore,
    WorkerBusy,
)


class FakeVault:
    def __init__(self) -> None:
        self.cas_required = True
        self.metadata_exists = True
        self.fail_data_reads = 0
        self.fail_reconciliation = False
        self.fail_read_after_write = False
        self.records: dict[str, tuple[int, dict]] = {}
        self.secrets = {
            "webhooks/coder": {
                "webhook_url": "https://worker.example.invalid/hook",
                "sender_key": "redacted",
            }
        }
        self.before_write = None
        self.ambiguous_write = False
        self.reject_data = False
        self.logins = 0

    @staticmethod
    def _reply(payload: dict) -> io.BytesIO:
        return io.BytesIO(json.dumps(payload).encode())

    @staticmethod
    def _error(request, status: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(
            request.full_url, status, "Vault error", {}, io.BytesIO(b"{}")
        )

    def urlopen(self, request, timeout: int = 0):
        path = urlsplit(request.full_url).path
        method = request.get_method()
        if path == "/v1/auth/approle/login":
            self.logins += 1
            return self._reply({"auth": {"client_token": f"short-lived-{self.logins}"}})
        if self.reject_data:
            raise self._error(request, 403)
        if path.startswith("/v1/secret/metadata/"):
            if not self.metadata_exists:
                raise self._error(request, 404)
            key = path.removeprefix("/v1/secret/metadata/")
            if key not in self.records and not self.cas_required:
                return self._reply({"data": {"cas_required": False}})
            return self._reply({"data": {"cas_required": self.cas_required}})
        if not path.startswith("/v1/secret/data/"):
            raise AssertionError(f"unexpected Vault path: {path}")
        key = path.removeprefix("/v1/secret/data/")
        if method == "GET":
            if self.fail_data_reads:
                self.fail_data_reads -= 1
                raise urllib.error.URLError("read unavailable")
            if key in self.secrets:
                return self._reply(
                    {"data": {"data": self.secrets[key], "metadata": {"version": 1}}}
                )
            if key not in self.records:
                raise self._error(request, 404)
            version, data = self.records[key]
            return self._reply({"data": {"data": data, "metadata": {"version": version}}})
        if method != "POST":
            raise AssertionError(f"unexpected Vault method: {method}")
        if self.before_write is not None:
            hook = self.before_write
            self.before_write = None
            hook(key)
        payload = json.loads(request.data)
        expected = self.records[key][0] if key in self.records else 0
        if payload["options"]["cas"] != expected:
            raise self._error(request, 400)
        self.records[key] = (expected + 1, payload["data"])
        if self.fail_read_after_write:
            self.fail_read_after_write = False
            self.fail_data_reads = 2
        if self.ambiguous_write:
            self.ambiguous_write = False
            if self.fail_reconciliation:
                self.fail_data_reads = 2
            raise urllib.error.URLError("timed out after write")
        return self._reply({"data": {"version": expected + 1}})


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    role = tmp_path / "role-id"
    secret = tmp_path / "secret-id"
    role.write_text("role-id-value")
    secret.write_text("secret-id-value")
    role.chmod(0o600)
    secret.chmod(0o600)
    fake = FakeVault()
    monkeypatch.setattr("urllib.request.urlopen", fake.urlopen)
    client = VaultClient(
        address="https://vault.example.invalid",
        role_id_file=role,
        secret_id_file=secret,
        mount="secret",
    )
    return fake, client, VaultLeaseStore(client, lease_prefix="leases")


def test_approle_reads_existing_secret_without_printing_values(setup, capsys) -> None:
    fake, client, _store = setup
    secret = client.read_secret("webhooks/coder")
    assert secret["sender_key"] == "redacted"
    assert fake.logins == 1
    assert "redacted" not in capsys.readouterr().out


def test_lease_acquire_renew_and_release_use_current_cas_version(setup) -> None:
    fake, _client, store = setup
    now = datetime(2026, 1, 1, tzinfo=UTC)

    first = store.acquire("coder", owner="client-a", job_id="job-1", now=now)
    assert fake.records["leases/coder"][0] == 1
    assert first.state == "active"
    assert first.deadline_at == now + timedelta(minutes=45)
    renewed = store.renew("coder", owner="client-a", job_id="job-1", now=now + timedelta(minutes=2))
    assert fake.records["leases/coder"][0] == 2
    assert renewed.expires_at <= first.deadline_at
    released = store.release("coder", owner="client-a", job_id="job-1")
    assert fake.records["leases/coder"][0] == 3
    assert released.state == "available"
    assert released.owner is None
    assert released.job_id is None


def test_concurrent_cas_claim_has_only_one_winner(setup) -> None:
    fake, _client, store = setup
    now = datetime(2026, 1, 1, tzinfo=UTC)
    competitor = {
        "schema_version": "v1",
        "state": "active",
        "worker": "coder",
        "owner": "client-b",
        "job_id": "job-2",
        "acquired_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "deadline_at": (now + timedelta(minutes=45)).isoformat(),
    }
    fake.before_write = lambda key: fake.records.__setitem__(key, (1, competitor))

    with pytest.raises(WorkerBusy):
        store.acquire("coder", owner="client-a", job_id="job-1", now=now)
    assert fake.records["leases/coder"][1]["owner"] == "client-b"


def test_expired_active_lease_is_not_taken_over(setup) -> None:
    fake, _client, store = setup
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fake.records["leases/coder"] = (
        1,
        {
            "schema_version": "v1",
            "state": "active",
            "worker": "coder",
            "owner": "previous",
            "job_id": "job-old",
            "acquired_at": (now - timedelta(hours=1)).isoformat(),
            "expires_at": (now - timedelta(minutes=55)).isoformat(),
            "deadline_at": (now - timedelta(minutes=15)).isoformat(),
        },
    )
    with pytest.raises(WorkerBusy):
        store.acquire("coder", owner="client-a", job_id="job-1", now=now)
    assert fake.records["leases/coder"][0] == 1


def test_missing_required_cas_configuration_blocks_dispatch(setup) -> None:
    fake, _client, store = setup
    fake.cas_required = False
    with pytest.raises(LeaseConfigurationError):
        store.acquire("coder", owner="client-a", job_id="job-1")
    assert fake.records == {}


def test_ambiguous_write_is_reconciled_without_retry(setup) -> None:
    fake, _client, store = setup
    fake.ambiguous_write = True
    record = store.acquire("coder", owner="client-a", job_id="job-1")
    assert record.owner == "client-a"
    assert fake.records["leases/coder"][0] == 1


def test_unconfirmed_ambiguous_write_stays_uncertain(setup) -> None:
    fake, _client, store = setup
    original = fake.urlopen

    def timeout_before_write(request, timeout=0):
        if request.get_method() == "POST" and "/data/" in request.full_url:
            raise urllib.error.URLError("timed out before write")
        return original(request, timeout)

    with mock.patch("urllib.request.urlopen", side_effect=timeout_before_write):
        with pytest.raises(LeaseUncertain):
            store.acquire("coder", owner="client-a", job_id="job-1")
    assert fake.records == {}


def test_wrong_owner_cannot_release(setup) -> None:
    fake, _client, store = setup
    store.acquire("coder", owner="client-a", job_id="job-1")
    with pytest.raises(LeaseOwnershipError):
        store.release("coder", owner="client-b", job_id="job-1")
    assert fake.records["leases/coder"][1]["state"] == "active"


def test_expired_or_denied_vault_token_fails_closed(setup) -> None:
    fake, client, _store = setup
    fake.reject_data = True
    with pytest.raises(VaultError):
        client.read_secret("webhooks/coder")
    assert fake.logins == 1


def test_missing_lease_metadata_blocks_first_write(setup) -> None:
    fake, _client, store = setup
    fake.metadata_exists = False
    with pytest.raises(LeaseConfigurationError):
        store.acquire("coder", owner="client-a", job_id="job-1")
    assert fake.records == {}


def test_unavailable_reconciliation_retains_uncertain_lease(setup) -> None:
    fake, _client, store = setup
    fake.ambiguous_write = True
    fake.fail_reconciliation = True
    with pytest.raises(LeaseUncertain):
        store.acquire("coder", owner="client-a", job_id="job-1")
    assert fake.records["leases/coder"][1]["state"] == "active"


def test_world_readable_approle_file_is_rejected(setup) -> None:
    fake, client, _store = setup
    client.secret_id_file.chmod(0o644)
    with pytest.raises(VaultError, match="private"):
        client.read_secret("webhooks/coder")
    assert fake.logins == 0


def test_successful_write_without_verifiable_read_is_uncertain(setup) -> None:
    fake, _client, store = setup
    fake.fail_read_after_write = True
    with pytest.raises(LeaseUncertain):
        store.acquire("coder", owner="client-a", job_id="job-1")
    assert fake.records["leases/coder"][1]["owner"] == "client-a"


def test_valid_dotted_vault_secret_path_is_supported(setup) -> None:
    fake, client, _store = setup
    fake.secrets["services/dev.worker"] = {"value": "present"}
    assert client.read_secret("services/dev.worker")["value"] == "present"


def test_explicit_ca_bundle_is_used_for_vault_tls(setup, tmp_path: Path) -> None:
    fake, client, _store = setup
    ca_file = tmp_path / "vault-ca.pem"
    ca_file.write_text("synthetic certificate fixture")
    configured = VaultClient(
        address=client.address,
        role_id_file=client.role_id_file,
        secret_id_file=client.secret_id_file,
        mount=client.mount,
        ca_cert_file=ca_file,
    )
    captured = []
    context = object()

    def open_with_context(request, timeout, **kwargs):
        captured.append(kwargs.get("context"))
        return fake.urlopen(request, timeout)

    with (
        mock.patch("ssl.create_default_context", return_value=context) as defaults,
        mock.patch("urllib.request.urlopen", side_effect=open_with_context),
    ):
        configured.read_secret("webhooks/coder")
    defaults.assert_called_with(cafile=str(ca_file))
    assert captured == [context, context]
