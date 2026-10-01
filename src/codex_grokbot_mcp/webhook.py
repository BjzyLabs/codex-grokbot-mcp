"""One-shot HTTPS webhook transport for the single configured Grok Bot.

The sender key stays in process memory and only ever appears in the request's
Authorization header. Responses are untrusted.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .deliver import MAX_WEBHOOK_BYTES
from .tls import verified_context


class WebhookError(RuntimeError):
    """The packet cannot be delivered to the configured webhook."""


class WebhookUncertain(WebhookError):
    """The webhook may have accepted the packet; do not retry or reconcile blindly."""


@dataclass(frozen=True)
class WebhookTransport:
    url: str
    sender_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise WebhookError("webhook URL is invalid")
        parsed = urllib.parse.urlparse(self.url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise WebhookError("webhook URL must be HTTPS without embedded credentials")
        if (
            not isinstance(self.sender_key, str)
            or not self.sender_key
            or any(character.isspace() or ord(character) < 32 for character in self.sender_key)
        ):
            raise WebhookError("webhook sender key is missing or unsafe")

    def dispatch(self, packet: dict) -> None:
        body = json.dumps(packet, separators=(",", ":")).encode("utf-8")
        if len(body) > MAX_WEBHOOK_BYTES:
            raise WebhookError("webhook packet exceeds size limit")
        try:
            context = verified_context()
        except (OSError, ssl.SSLError) as error:
            raise WebhookError("webhook trust configuration is invalid") from error
        request = urllib.request.Request(  # noqa: S310 - URL is validated HTTPS
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.sender_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(  # noqa: S310 - URL is validated HTTPS
                request, timeout=30, context=context
            ) as response:
                if response.status < 200 or response.status >= 300:
                    raise WebhookUncertain(f"webhook returned HTTP {response.status}")
                response.read(1024)
        except urllib.error.HTTPError as error:
            raise WebhookUncertain(f"webhook returned HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise WebhookUncertain("webhook outcome is uncertain") from error
