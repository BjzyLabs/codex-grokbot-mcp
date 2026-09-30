from __future__ import annotations

import unittest

from codex_grokbot_mcp.tls import verified_context


class TrustStoreTests(unittest.TestCase):
    def test_verified_context_has_certificate_authorities(self) -> None:
        context = verified_context()

        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)


if __name__ == "__main__":
    unittest.main()
