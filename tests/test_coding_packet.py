from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codex_grokbot_mcp.control import InstallationToken
from codex_grokbot_mcp.local import DelegationContext, SourceSnapshot
from codex_grokbot_mcp.protocol import PacketError, build_coding_packet

JOB_ID = "1234abcd-1234-4123-8123-123456789abc"


@pytest.fixture
def context() -> DelegationContext:
    return DelegationContext(
        root=Path("/private/local/workspace"),
        target_repo="example/target",
        head="a" * 40,
        branch="feature/example",
        read_paths=("src/a.py",),
        write_paths=("src/a.py", "tests/test_a.py"),
        source_snapshots=(SourceSnapshot("src/a.py", "b" * 64, "selected source\n"),),
        write_hashes=(("src/a.py", "b" * 64), ("tests/test_a.py", None)),
        snapshot_digest="c" * 64,
    )


def packet(context: DelegationContext, **overrides) -> dict:
    options = {
        "job_id": JOB_ID,
        "goal": "Add a bounded regression test",
        "context": context,
        "control_repo": "example/private-control",
        "control_branch": "grokbot/job-coding-1234abcd",
        "artifact_path": "artifacts/patch-1234abcd.json",
        "token": InstallationToken("scoped-worker-token", datetime.now(UTC) + timedelta(hours=1)),
        "acceptance_checks": [
            "The regression test fails before the fix",
            "The focused suite passes",
        ],
        "effort_hint": "medium",
    }
    options.update(overrides)
    return build_coding_packet(**options)


def test_packet_preserves_v1_route_and_sends_only_selected_snapshot(context) -> None:
    result = packet(context)
    data = json.dumps(result)
    assert result["schema_version"] == "v2"
    assert result["job_type"] == "coding"
    assert result["context"]["control_repo"] == "example/private-control"
    assert result["context"]["head_ref"] == "grokbot/job-coding-1234abcd"
    assert result["context"]["patch_path"] == "artifacts/patch-1234abcd.json"
    assert result["context"]["github_token"] == "scoped-worker-token"  # noqa: S105
    assert result["context"]["base_sha"] == "a" * 40
    assert result["context"]["allowed_paths"] == ["src/a.py", "tests/test_a.py"]
    assert result["context"]["source_snapshots"] == [
        {"path": "src/a.py", "sha256": "b" * 64, "content": "selected source\n"}
    ]
    assert result["context"]["snapshot_digest"] == "c" * 64
    assert result["context"]["max_minutes"] == 45
    assert result["context"]["effort_hint"] == "medium"
    assert "/private/local/workspace" not in data
    assert "model" not in result and "model" not in result["context"]
    assert result["constraints"]["no_merge"] is True
    assert any("draft PR" in item for item in result["instructions"])


@pytest.mark.parametrize(
    "change",
    [
        {"control_branch": "grokbot/job-coding-deadbeef"},
        {"artifact_path": "artifacts/patch-deadbeef.json"},
        {"control_repo": "example/target"},
        {"control_repo": "../control"},
        {"effort_hint": "large"},
        {"acceptance_checks": []},
        {"token": InstallationToken("soon", datetime.now(UTC) + timedelta(minutes=10))},
    ],
)
def test_invalid_or_unbounded_packet_is_rejected(context, change) -> None:
    with pytest.raises(PacketError):
        packet(context, **change)


def test_oversized_packet_is_rejected(context) -> None:
    huge = DelegationContext(
        **{
            **context.__dict__,
            "source_snapshots": (SourceSnapshot("src/a.py", "b" * 64, "x" * (2 * 1024 * 1024)),),
        }
    )
    with pytest.raises(PacketError, match="size"):
        packet(huge)
