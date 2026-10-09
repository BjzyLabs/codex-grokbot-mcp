from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import INBOX_ORIGIN, REQUESTOR_BEARER, SENDER_KEY, WEBHOOK_URL, write_config_file

from codex_grokbot_mcp.config import Config, ConfigError


class VaultConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.text = (
            "version = 3\n"
            f'job_database = "{self.root}/private/jobs.sqlite3"\n'
            f'inbox_base_url = "{INBOX_ORIGIN}"\n'
            'vault_webhook_path = "kvExample/Bots/XBot"\n'
            'vault_inbox_path = "kvExample/Bots/inbox"\n'
        )
        environment = mock.patch.dict(os.environ, {"VAULT_ADDR": "https://vault.example.invalid"})
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("VAULT_SKIP_VERIFY", None)
        executable = mock.patch("shutil.which", return_value="/usr/local/bin/vault")
        executable.start()
        self.addCleanup(executable.stop)
        self.responses = [
            self.response({"data": {"policies": ["requestor"], "ttl": 600}}),
            self.response(
                {"data": {"data": {"webhook_url": WEBHOOK_URL, "sender_key": SENDER_KEY}}}
            ),
            self.response({"data": {"data": {"requestor_token": REQUESTOR_BEARER}}}),
        ]

    @staticmethod
    def response(body: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["vault"], 0, json.dumps(body), "")

    def load(self, text: str | None = None) -> Config:
        return Config.load(write_config_file(self.root, self.text if text is None else text))

    def test_vault_values_load_without_credentials_in_local_config(self) -> None:
        with mock.patch("subprocess.run", side_effect=self.responses) as run:
            config = self.load()
        self.assertEqual(config.webhook_url, WEBHOOK_URL)
        self.assertEqual(config.sender_key, SENDER_KEY)
        self.assertEqual(config.inbox_requestor_token, REQUESTOR_BEARER)
        self.assertEqual(config.inbox_base_url, INBOX_ORIGIN)
        self.assertEqual(config.job_database, self.root / "private/jobs.sqlite3")
        self.assertEqual(
            run.call_args_list[0].args[0],
            ["/usr/local/bin/vault", "token", "lookup", "-format=json"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["/usr/local/bin/vault", "kv", "get", "-format=json", "kvExample/Bots/XBot"],
        )
        self.assertEqual(run.call_args_list[2].args[0][-1], "kvExample/Bots/inbox")
        for call in run.call_args_list:
            self.assertTrue(call.kwargs["capture_output"])
            self.assertEqual(call.kwargs["timeout"], 20)
        for value in (WEBHOOK_URL, SENDER_KEY, REQUESTOR_BEARER):
            self.assertNotIn(value, repr(config))
            self.assertNotIn(value, self.text)

    def test_vault_errors_do_not_expose_stdout_stderr_or_exception_details(self) -> None:
        marker = "credential-that-must-not-be-printed"
        failures = (
            subprocess.CompletedProcess(["vault"], 1, marker, marker),
            FileNotFoundError(marker),
            subprocess.TimeoutExpired([marker], 20, output=marker, stderr=marker),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                effect = failure if isinstance(failure, Exception) else [failure]
                with mock.patch("subprocess.run", side_effect=effect):
                    with self.assertRaises(ConfigError) as caught:
                        self.load()
                self.assertNotIn(marker, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)

    def test_failed_auth_stops_before_secret_reads(self) -> None:
        for session in (
            {"policies": ["root"], "ttl": 0},
            {"policies": ["requestor"], "ttl": 0},
            {},
        ):
            with self.subTest(session=session):
                with mock.patch(
                    "subprocess.run", return_value=self.response({"data": session})
                ) as run:
                    with self.assertRaises(ConfigError):
                        self.load()
                    self.assertEqual(run.call_count, 1)

    def test_missing_invalid_or_unsafe_secret_values_fail_closed(self) -> None:
        for data in (
            {},
            {"sender_key": SENDER_KEY},
            {"webhook_url": WEBHOOK_URL},
            {"webhook_url": "http://webhook.example.invalid", "sender_key": SENDER_KEY},
            {"webhook_url": WEBHOOK_URL, "sender_key": ""},
        ):
            with self.subTest(fields=sorted(data)):
                responses = [self.responses[0], self.response({"data": {"data": data}})]
                with mock.patch("subprocess.run", side_effect=responses):
                    with self.assertRaises(ConfigError):
                        self.load()
        for value in (None, "", "has space", 123):
            with self.subTest(token_type=type(value).__name__):
                responses = self.responses[:2] + [
                    self.response({"data": {"data": {"requestor_token": value}}})
                ]
                with mock.patch("subprocess.run", side_effect=responses):
                    with self.assertRaises(ConfigError):
                        self.load()

    def test_malformed_responses_fail_without_echoing_payload(self) -> None:
        for payload in ("private-invalid-json", "[]", "null", '{"data":[]}'):
            with self.subTest(payload=payload):
                response = subprocess.CompletedProcess(["vault"], 0, payload, "")
                with mock.patch("subprocess.run", return_value=response):
                    with self.assertRaises(ConfigError) as caught:
                        self.load()
                self.assertNotIn(payload, str(caught.exception))

    def test_local_credentials_cannot_be_mixed_with_vault_references(self) -> None:
        with mock.patch("subprocess.run") as run:
            with self.assertRaises(ConfigError):
                self.load(self.text + 'sender_key = "local-value"\n')
            run.assert_not_called()

    def test_invalid_vault_paths_are_rejected_before_any_reads(self) -> None:
        for path in ("", "-option", "kvExample", "kvExample/../secret", "kvExample/with space"):
            with self.subTest(path=path):
                with mock.patch("subprocess.run") as run:
                    with self.assertRaises(ConfigError):
                        self.load(self.text.replace("kvExample/Bots/XBot", path))
                    run.assert_not_called()

    def test_unsafe_vault_transport_is_rejected_before_any_reads(self) -> None:
        for address in ("", "http://vault.example.invalid", "https://user@vault.example.invalid"):
            with self.subTest(address=address):
                with mock.patch.dict(os.environ, {"VAULT_ADDR": address}):
                    with mock.patch("subprocess.run") as run:
                        with self.assertRaises(ConfigError):
                            self.load()
                        run.assert_not_called()
        with mock.patch.dict(os.environ, {"VAULT_SKIP_VERIFY": "true"}):
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(ConfigError):
                    self.load()
                run.assert_not_called()

    def test_missing_vault_executable_stops_without_attempting_a_process(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            with mock.patch("subprocess.run") as run:
                with self.assertRaisesRegex(ConfigError, "unavailable"):
                    self.load()
                run.assert_not_called()

    def test_secret_read_failure_never_echoes_output(self) -> None:
        marker = "private-payload-that-must-not-leak"
        failed = subprocess.CompletedProcess(["vault"], 1, marker, marker)
        for prefix in (self.responses[:1], self.responses[:2]):
            with self.subTest(stage=len(prefix)):
                with mock.patch("subprocess.run", side_effect=[*prefix, failed]):
                    with self.assertRaises(ConfigError) as caught:
                        self.load()
                self.assertNotIn(marker, str(caught.exception))

    def test_malformed_webhook_response_is_a_safe_configuration_error(self) -> None:
        for url in ("https://[invalid", "https://webhook.example.invalid/\nsecret"):
            with self.subTest(kind="malformed-webhook"):
                response = self.response(
                    {"data": {"data": {"webhook_url": url, "sender_key": SENDER_KEY}}}
                )
                with mock.patch("subprocess.run", side_effect=[self.responses[0], response]):
                    with self.assertRaises(ConfigError) as caught:
                        self.load()
                self.assertNotIn(url, str(caught.exception))
