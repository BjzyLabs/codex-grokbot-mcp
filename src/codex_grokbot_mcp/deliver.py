"""v3 delivery: one callback for answers, a pinned draft PR for code."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from .control import MAX_WEBHOOK_BYTES, REPO_PATTERN
from .protocol import PacketError, build_coding_packet

CALLBACK_BODY_LIMIT = 64 * 1024
_QUERY_MAX = 2000
_SUMMARY_MAX = 500
_ANSWER_STRING_MAX = 4000
_ANSWER_TOTAL_MAX = 20000
_SOURCE_MAX = 512
_SOURCES_MAX = 32
_ERROR_MAX = 300
_CREDENTIAL_RE = re.compile(
    r"(?i)(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|ghp_[A-Za-z0-9]{20,}|github_pat_|"
    r"\b(?:bearer|token|secret|password)\b\s*[:=]\s*\S+)"
)
_X_WRITE_CLAIM_RE = re.compile(
    r"\b(?:i|we)\s+(?:have\s+)?(?:posted|tweeted|replied|dm'?d|direct[ -]?messaged"
    r"|followed|blocked|muted|liked|retweeted|quote[ -]?tweeted)\b",
    re.IGNORECASE,
)
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)\Z")
StatusDecision = Literal["poll", "blocked", "error"]


def _canonical_job_id(job_id: str) -> str:
    try:
        canonical = str(UUID(job_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise PacketError("job ID must be a canonical UUID") from error
    if canonical != job_id:
        raise PacketError("job ID must be a canonical UUID")
    return canonical


def _goal(goal: str) -> str:
    if not isinstance(goal, str) or not 0 < len(goal.strip()) <= _QUERY_MAX or "\x00" in goal:
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


def require_callback_target(value: str, *, origin: str, job_id: str, kind: str) -> str:
    """Reject any callback target other than the one configured for this job."""
    expected = callback_url(origin, job_id, kind)
    if value != expected:
        raise PacketError("callback URL does not match the job")
    return expected


def callback_url(origin: str, job_id: str, kind: str) -> str:
    """Build the one callback or status URL for a job."""
    if kind not in ("result", "status"):
        raise PacketError("callback path is invalid")
    canonical = _canonical_job_id(job_id)
    base = canonical_inbox_origin(origin)
    return f"{base}/jobs/{canonical}/{kind}"


def _token(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 32
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise PacketError("callback token is missing or unsafe")
    return value


def build_x_query_packet(
    *,
    job_id: str,
    goal: str,
    origin: str,
    callback_token: str,
    deliver: str | None = None,
    **forbidden: object,
) -> dict:
    """Build one read-only query packet with no control-repository authority."""
    if forbidden:
        raise PacketError("callback packet cannot carry a control-repo field")
    canonical = _canonical_job_id(job_id)
    selected = "callback" if deliver is None else deliver
    if selected != "callback":
        raise PacketError("x_query deliver must be callback")
    packet = {
        "schema_version": "v3",
        "job_type": "x_query",
        "job_id": canonical,
        "goal": _goal(goal),
        "constraints": {
            "read_only": True,
            "no_x_writes": True,
            "no_credentials": True,
            "no_redelegation": True,
            "no_secrets": True,
        },
        "context": {
            "callback_url": callback_url(origin, canonical, "result"),
            "callback_token": _token(callback_token),
        },
        "instructions": [
            "Do read-only research. Do not post, reply, message, follow, or modify an account.",
            (
                "POST the result once to callback_url using "
                "Authorization: Bearer and the callback token."
            ),
            "Do not follow redirects or use any other URL.",
            "Do not open a pull request or use a GitHub token.",
            "On failure set status to error, leave answer null, and do not invent themes.",
        ],
        "deliver": "callback",
    }
    raw = json.dumps(packet, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_WEBHOOK_BYTES:
        raise PacketError("coding packet exceeds webhook size limit")
    return packet


def build_v3_coding_packet(
    *,
    deliver: str | None = None,
    status_callback: dict | None = None,
    **coding: Any,
) -> dict:
    """Keep the v2 coding route and optionally add one status ping."""
    selected = "github_pr" if deliver is None else deliver
    if selected != "github_pr":
        raise PacketError("coding deliver must be github_pr")
    packet = build_coding_packet(**coding)
    packet["schema_version"] = "v3"
    packet["deliver"] = "github_pr"
    if status_callback is not None:
        job_id = packet["job_id"]
        origin = status_callback.get("origin")
        token = status_callback.get("token")
        if not isinstance(origin, str) or not isinstance(token, str):
            raise PacketError("status callback is incomplete")
        packet["context"]["status_callback_url"] = callback_url(origin, job_id, "status")
        packet["context"]["status_callback_token"] = _token(token)
        packet["instructions"] = [
            *packet["instructions"],
            "POST one status ping to status_callback_url. Do not include the patch or token.",
        ]
    return packet


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


def _safe_text(value: str, label: str, maximum: int) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
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
        if any(ord(character) < 32 for character in text) or _CREDENTIAL_RE.search(text):
            raise PacketError(f"{label} is unsafe")
        if _X_WRITE_CLAIM_RE.search(text):
            raise PacketError(f"{label} claims a write action")


def validate_x_query_result(job_id: str, body: dict, *, now: datetime) -> dict:
    """Accept one callback body or reject it closed."""
    if not isinstance(body, dict):
        raise PacketError("callback body is invalid")
    canonical = _canonical_job_id(job_id)
    if body.get("schema_version") != "v3" or body.get("job_type") != "x_query":
        raise PacketError("callback body does not match the job")
    if body.get("job_id") != canonical:
        raise PacketError("callback body does not match the job")
    if body.get("read_only_attestation") is not True:
        raise PacketError("callback body must attest read-only")
    _safe_text(body.get("query"), "query", _QUERY_MAX)
    _safe_text(body.get("summary"), "summary", _SUMMARY_MAX)
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
        _safe_text(error["code"], "error code", 80)
        _safe_text(error["message"], "error message", _ERROR_MAX)
    else:
        raise PacketError("callback status is invalid")
    return body


def _validate_ok_answer(answer: Any) -> None:
    if isinstance(answer, str):
        _safe_text(answer, "answer", _ANSWER_STRING_MAX)
        if _X_WRITE_CLAIM_RE.search(answer):
            raise PacketError("answer claims a write action")
        return
    if isinstance(answer, dict):
        themes = answer.get("top_themes")
        if not isinstance(themes, list) or not themes:
            raise PacketError("structured answer must include themes")
        texts = _strings(answer)
        if sum(len(text) for text in texts) > _ANSWER_TOTAL_MAX:
            raise PacketError("answer exceeds the total size bound")
        _reject_answer_strings(texts, "answer")
        return
    raise PacketError("answer must be a string or object")


def _validate_sources(sources: Any) -> None:
    if not isinstance(sources, list) or len(sources) > _SOURCES_MAX:
        raise PacketError("sources exceed the bound")
    for source in sources:
        if isinstance(source, str):
            _safe_text(source, "source", _SOURCE_MAX)
        elif isinstance(source, dict):
            texts = _strings(source)
            if not texts:
                raise PacketError("source is unsafe")
            for text in texts:
                _safe_text(text, "source", _SOURCE_MAX)
        else:
            raise PacketError("source is unsafe")


def validate_status_body(job_id: str, body: dict, *, control_repo: str) -> dict:
    """Reject a status ping that carries a patch or a foreign pull request."""
    if not isinstance(body, dict):
        raise PacketError("status body is invalid")
    if "patch" in body or "github_token" in body or "diff" in body:
        raise PacketError("status body cannot carry a patch")
    canonical = _canonical_job_id(job_id)
    if (
        body.get("schema_version") != "v3"
        or body.get("job_type") != "coding"
        or body.get("job_id") != canonical
    ):
        raise PacketError("status body does not match the job")
    if body.get("status") not in ("ready", "blocked", "error"):
        raise PacketError("status body is invalid")
    _safe_text(body.get("summary"), "summary", _SUMMARY_MAX)
    _utc(body.get("completed_at"), "completed_at")
    pr_url = body.get("pr_url")
    if pr_url is not None:
        match = _PR_URL_RE.fullmatch(pr_url) if isinstance(pr_url, str) else None
        if (
            match is None
            or match.group(1) != control_repo
            or not REPO_PATTERN.fullmatch(control_repo)
        ):
            raise PacketError("status pull request is foreign")
    elif body.get("status") == "ready":
        raise PacketError("ready status requires the draft pull request")
    return body


def interpret_status(body: dict, *, artifact_present: bool) -> StatusDecision:
    """A ready ping only asks for a poll. An artifact outranks a blocker."""
    status = body.get("status")
    if artifact_present or status == "ready":
        return "poll"
    if status == "blocked":
        return "blocked"
    if status == "error":
        return "error"
    raise PacketError("status body is invalid")


def body_digest(body: bytes) -> str:
    if len(body) > CALLBACK_BODY_LIMIT:
        raise PacketError("callback body exceeds its limit")
    return hashlib.sha256(body).hexdigest()
