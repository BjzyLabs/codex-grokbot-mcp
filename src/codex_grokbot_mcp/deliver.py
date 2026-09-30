"""v3 delivery: one HTTPS callback carries the answer for both request types."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

CALLBACK_BODY_LIMIT = 64 * 1024
MAX_WEBHOOK_BYTES = 2 * 1024 * 1024
CALLBACK_KINDS = ("result", "status")
JOB_TYPES = ("x_query", "ask")
QUERY_MAX = 2000
SUMMARY_MAX = 500
ANSWER_STRING_MAX = 4000
ANSWER_TOTAL_MAX = 20000
SOURCE_MAX = 512
SOURCES_MAX = 32
ERROR_TEXT_MAX = 300

_CALLBACK_CONSTRAINTS = {
    "read_only": True,
    "no_x_writes": True,
    "no_credentials": True,
    "no_redelegation": True,
    "no_secrets": True,
}
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|gh(?:p|s|u|o|r)_[A-Za-z0-9]{20,}|github_pat_|"
    r"crsr_[A-Za-z0-9_-]{16,}|"
    r"\b(?:bearer|token|secret|password)\b\s*[:=]\s*\S+)"
)
_X_WRITE_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:posted|tweeted|replied|dm'?d|direct[ -]?messaged"
    r"|followed|blocked|muted|liked|retweeted|quote[ -]?tweeted)\b",
    re.IGNORECASE,
)

_X_QUERY_INSTRUCTIONS = (
    "Do read-only research. Do not post, reply, message, follow, or modify an account.",
    "POST the result once to callback_url using Authorization: Bearer and the callback token.",
    "Do not follow redirects or use any other URL.",
    "Do not open a pull request or use a GitHub token.",
    "On failure set status to error, leave answer null, and do not invent themes.",
)
_ASK_INSTRUCTIONS = (
    "Answer the question directly. X access is optional; this is not an X-only request.",
    "Do not post, reply, message, follow, or modify any account.",
    "POST the result once to callback_url using Authorization: Bearer and the callback token.",
    "Do not follow redirects or use any other URL.",
    "Do not open a pull request or use a GitHub token.",
    "On failure set status to error, leave answer null, and do not invent sources.",
)
_INSTRUCTIONS = {"x_query": _X_QUERY_INSTRUCTIONS, "ask": _ASK_INSTRUCTIONS}


class PacketError(ValueError):
    """A proposed packet or returned callback body is invalid."""


def canonical_job_id(job_id: str) -> str:
    try:
        canonical = str(UUID(job_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise PacketError("job ID must be a canonical UUID") from error
    if canonical != job_id:
        raise PacketError("job ID must be a canonical UUID")
    return canonical


def clean_goal(goal: str) -> str:
    if not isinstance(goal, str) or not 0 < len(goal.strip()) <= QUERY_MAX or "\x00" in goal:
        raise PacketError("goal is empty or exceeds its bound")
    return goal.strip()


def canonical_inbox_origin(value: str) -> str:
    """Return the only origin a callback URL may use."""
    if not isinstance(value, str):
        raise PacketError("callback origin is invalid")
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in ("", "/")
        or not parts.hostname
        or parts.hostname != parts.netloc
    ):
        raise PacketError("callback origin is invalid")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise PacketError("callback origin is invalid")
    return f"https://{parts.hostname}"


def callback_url(origin: str, job_id: str, kind: str) -> str:
    """Build the one callback or status URL for a job."""
    if kind not in CALLBACK_KINDS:
        raise PacketError("callback path is invalid")
    canonical = canonical_job_id(job_id)
    base = canonical_inbox_origin(origin)
    return f"{base}/jobs/{canonical}/{kind}"


def require_callback_target(value: str, *, origin: str, job_id: str, kind: str) -> str:
    """Reject any callback target other than the one configured for this job."""
    expected = callback_url(origin, job_id, kind)
    if value != expected:
        raise PacketError("callback URL does not match the job")
    return expected


def _token(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 32
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise PacketError("callback token is missing or unsafe")
    return value


def _build_callback_packet(
    *,
    job_type: str,
    job_id: str,
    goal: str,
    origin: str,
    callback_token: str,
    deliver: str | None,
    forbidden: dict[str, object],
) -> dict:
    """Build one read-only callback packet with no control-repository authority."""
    if forbidden:
        raise PacketError("callback packet cannot carry a control-repo field")
    canonical = canonical_job_id(job_id)
    selected = "callback" if deliver is None else deliver
    if selected != "callback":
        raise PacketError(f"{job_type} deliver must be callback")
    packet = {
        "schema_version": "v3",
        "job_type": job_type,
        "job_id": canonical,
        "goal": clean_goal(goal),
        "constraints": dict(_CALLBACK_CONSTRAINTS),
        "context": {
            "callback_url": callback_url(origin, canonical, "result"),
            "callback_token": _token(callback_token),
        },
        "instructions": list(_INSTRUCTIONS[job_type]),
        "deliver": "callback",
    }
    raw = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise PacketError(f"{job_type} packet exceeds webhook size limit")
    return packet


def build_x_query_packet(
    *,
    job_id: str,
    goal: str,
    origin: str,
    callback_token: str,
    deliver: str | None = None,
    **forbidden: object,
) -> dict:
    """Build one read-only X research request that answers by callback."""
    return _build_callback_packet(
        job_type="x_query",
        job_id=job_id,
        goal=goal,
        origin=origin,
        callback_token=callback_token,
        deliver=deliver,
        forbidden=forbidden,
    )


def build_ask_packet(
    *,
    job_id: str,
    goal: str,
    origin: str,
    callback_token: str,
    deliver: str | None = None,
    **forbidden: object,
) -> dict:
    """Build one read-only general question that answers by callback."""
    return _build_callback_packet(
        job_type="ask",
        job_id=job_id,
        goal=goal,
        origin=origin,
        callback_token=callback_token,
        deliver=deliver,
        forbidden=forbidden,
    )


def _utc(value: str, label: str) -> datetime:
    if not isinstance(value, str):
        raise PacketError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PacketError(f"{label} is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PacketError(f"{label} must use a canonical UTC offset")
    return parsed.astimezone(UTC)


def _safe_text(value: Any, label: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 and not character.isspace() for character in value)
        or _CREDENTIAL_RE.search(value)
    ):
        raise PacketError(f"{label} is unsafe")


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_strings(item))
        return found
    raise PacketError("answer contains a non-text value")


def _reject_answer_strings(texts: list[str], label: str) -> None:
    for text in texts:
        if any(ord(character) < 32 and not character.isspace() for character in text):
            raise PacketError(f"{label} is unsafe")
        if _CREDENTIAL_RE.search(text):
            raise PacketError(f"{label} is unsafe")
        if _X_WRITE_CLAIM_RE.search(text):
            raise PacketError(f"{label} claims a write action")


def _validate_ok_answer(answer: Any) -> None:
    if isinstance(answer, str):
        _safe_text(answer, "answer", ANSWER_STRING_MAX)
        if _X_WRITE_CLAIM_RE.search(answer):
            raise PacketError("answer claims a write action")
        return
    if isinstance(answer, dict):
        themes = answer.get("top_themes")
        if not isinstance(themes, list) or not themes:
            raise PacketError("structured answer must include themes")
        texts = _strings(answer)
        if sum(len(text) for text in texts) > ANSWER_TOTAL_MAX:
            raise PacketError("answer exceeds the total size bound")
        _reject_answer_strings(texts, "answer")
        return
    raise PacketError("answer must be a string or object")


def _validate_sources(sources: Any) -> None:
    if not isinstance(sources, list) or len(sources) > SOURCES_MAX:
        raise PacketError("sources exceed the bound")
    for source in sources:
        if isinstance(source, str):
            _safe_text(source, "source", SOURCE_MAX)
        elif isinstance(source, dict):
            texts = _strings(source)
            if not texts:
                raise PacketError("source is unsafe")
            for text in texts:
                _safe_text(text, "source", SOURCE_MAX)
        else:
            raise PacketError("source is unsafe")


def validate_result(
    job_id: str,
    expected_job_type: str,
    body: Any,
    *,
    now: datetime,
) -> dict:
    """Accept one callback body for the expected request type, or reject it closed."""
    if expected_job_type not in JOB_TYPES:
        raise PacketError("expected job type is invalid")
    if not isinstance(body, dict):
        raise PacketError("callback body is invalid")
    canonical = canonical_job_id(job_id)
    if body.get("schema_version") != "v3" or body.get("job_type") != expected_job_type:
        raise PacketError("callback body does not match the job")
    if body.get("job_id") != canonical:
        raise PacketError("callback body does not match the job")
    if body.get("read_only_attestation") is not True:
        raise PacketError("callback body must attest read-only")
    _safe_text(body.get("query"), "query", QUERY_MAX)
    _safe_text(body.get("summary"), "summary", SUMMARY_MAX)
    completed = _utc(body.get("completed_at"), "completed_at")
    if completed > now.astimezone(UTC):
        raise PacketError("callback completion time is in the future")
    status = body.get("status")
    if status == "ok":
        if body.get("error") is not None:
            raise PacketError("successful callback cannot include an error")
        _validate_ok_answer(body.get("answer"))
        _validate_sources(body.get("sources"))
    elif status == "error":
        if body.get("answer") is not None or body.get("sources") != []:
            raise PacketError("error callback must not include an answer")
        error = body.get("error")
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise PacketError("error callback is incomplete")
        _safe_text(error["code"], "error code", ERROR_TEXT_MAX)
        _safe_text(error["message"], "error message", ERROR_TEXT_MAX)
    else:
        raise PacketError("callback status is invalid")
    return body


def body_digest(body: bytes) -> str:
    if len(body) > CALLBACK_BODY_LIMIT:
        raise PacketError("callback body exceeds its limit")
    return hashlib.sha256(body).hexdigest()
