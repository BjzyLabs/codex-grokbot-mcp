from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from support import (
    INBOX_ORIGIN,
    REQUESTOR_BEARER,
    SENDER_KEY,
    WEBHOOK_URL,
    config_text,
    make_config,
    write_config_file,
)

from codex_grokbot_mcp.config import Config, ConfigError


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    def load(self, text: str) -> Config:
        return Config.load(write_config_file(self.root, text))

    def test_flat_configuration_loads_with_the_exact_fields(self) -> None:
        config = self.load(config_text(self.root))

        self.assertEqual(config.job_database, self.root / "private" / "jobs.sqlite3")
        self.assertEqual(config.inbox_base_url, INBOX_ORIGIN)
        self.assertEqual(config.webhook_url, WEBHOOK_URL)
        self.assertEqual(config.inbox_requestor_token, REQUESTOR_BEARER)
        self.assertEqual(config.sender_key, SENDER_KEY)

    def test_secret_values_never_appear_in_repr(self) -> None:
        rendered = repr(self.load(config_text(self.root)))

        for private_value in (REQUESTOR_BEARER, SENDER_KEY):
            self.assertNotIn(private_value, rendered)
            self.assertNotIn(private_value, repr(make_config(self.root)))

    def test_config_file_must_be_private(self) -> None:
        path = write_config_file(self.root, config_text(self.root), mode=0o644)

        with self.assertRaisesRegex(ConfigError, "private"):
            Config.load(path)

    def test_missing_configuration_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unavailable"):
            Config.load(self.root / "absent.toml")

    def test_missing_field_is_rejected(self) -> None:
        text = config_text(self.root).replace(f'sender_key = "{SENDER_KEY}"\n', "")

        with self.assertRaisesRegex(ConfigError, "missing or unknown"):
            self.load(text)

    def test_extra_field_is_rejected(self) -> None:
        text = config_text(self.root) + 'vault_address = "https://vault.example.invalid"\n'

        with self.assertRaisesRegex(ConfigError, "missing or unknown"):
            self.load(text)

    def test_nested_table_is_rejected(self) -> None:
        text = config_text(self.root) + '[workers.chief]\nsender_key = "x"\n'

        with self.assertRaisesRegex(ConfigError, "missing or unknown"):
            self.load(text)

    def test_unsupported_versions_are_rejected(self) -> None:
        for version in ("1", "4", '"2"', "2.0"):
            with self.subTest(version=version):
                with self.assertRaisesRegex(ConfigError, "version"):
                    self.load(config_text(self.root, version=version))

    def test_relative_job_database_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "absolute"):
            self.load(config_text(self.root, job_database='"relative/jobs.sqlite3"'))

    def test_inbox_origin_rules_are_enforced(self) -> None:
        rejected = (
            "http://inbox.example.invalid",
            "https://user@inbox.example.invalid",
            "https://inbox.example.invalid/extra",
            "https://inbox.example.invalid/?q=1",
            "https://192.0.2.10",
            "",
        )
        for origin in rejected:
            with self.subTest(origin=origin):
                with self.assertRaisesRegex(ConfigError, "origin"):
                    self.load(config_text(self.root, inbox_base_url=f'"{origin}"'))

    def test_webhook_url_rules_are_enforced(self) -> None:
        rejected = (
            "http://webhook.example.invalid",
            "https://user@webhook.example.invalid",
            "https://webhook.example.invalid/#fragment",
            "https:///missing-host",
        )
        for url in rejected:
            with self.subTest(url=url):
                with self.assertRaisesRegex(ConfigError, "webhook"):
                    self.load(config_text(self.root, webhook_url=f'"{url}"'))

    def test_token_and_sender_key_must_be_safe(self) -> None:
        rejected = ('""', '"with space"', '"with\ttab"')
        for value in rejected:
            with self.subTest(field="inbox_requestor_token", value=value):
                with self.assertRaisesRegex(ConfigError, "credential"):
                    self.load(config_text(self.root, inbox_requestor_token=value))
            with self.subTest(field="sender_key", value=value):
                with self.assertRaisesRegex(ConfigError, "sender key"):
                    self.load(config_text(self.root, sender_key=value))

    def test_error_never_echoes_a_secret_value(self) -> None:
        private_value = "sender-value-that-must-not-leak"
        unsafe = json.dumps(f"{private_value}\tunsafe")

        with self.assertRaisesRegex(ConfigError, "sender key") as caught:
            self.load(config_text(self.root, sender_key=unsafe))

        self.assertNotIn(private_value, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
