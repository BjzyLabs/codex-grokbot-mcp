"""Verified TLS contexts for the two HTTPS endpoints on the request path.

Some Python builds ship without a usable default trust store (the macOS
framework installer is the common case: `cert_store_stats()` reports zero CA
certificates). Requesting a verified context here fails closed instead of
silently verifying nothing, and falls back to the platform CA bundle when the
interpreter's default store is empty.
"""

from __future__ import annotations

import ssl
from pathlib import Path

SYSTEM_CA_BUNDLE = Path("/etc/ssl/cert.pem")


def verified_context() -> ssl.SSLContext:
    """Return a context with at least one CA certificate or raise SSLError."""
    context = ssl.create_default_context()
    if context.cert_store_stats()["x509_ca"] > 0:
        return context
    if SYSTEM_CA_BUNDLE.is_file():
        context.load_verify_locations(cafile=str(SYSTEM_CA_BUNDLE))
    if context.cert_store_stats()["x509_ca"] <= 0:
        raise ssl.SSLError("no trusted CA certificates are available")
    return context
