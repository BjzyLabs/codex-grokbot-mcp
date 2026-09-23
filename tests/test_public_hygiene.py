from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_hygiene.py"


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def check(repo: Path, denylist: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--denylist", str(denylist)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_missing_denylist_fails_closed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("Public sample.\n")
    git(repo, "add", "README.md")

    result = check(repo, tmp_path / "missing-denylist.txt")

    assert result.returncode != 0
    assert "denylist" in result.stderr.lower()


def test_staged_private_identifier_fails_without_printing_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    marker = "private-control.example.invalid"
    (repo / "README.md").write_text(f"Connect to {marker}.\n")
    git(repo, "add", "README.md")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(marker + "\n")

    result = check(repo, denylist)

    assert result.returncode != 0
    assert "Git content" in result.stderr
    assert marker not in result.stdout + result.stderr


def test_clean_staged_tree_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "README.md").write_text("Use vault.example.invalid in docs.\n")
    git(repo, "add", "README.md")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("private-control.example.invalid\n")

    result = check(repo, denylist)

    assert result.returncode == 0, result.stderr


def test_private_filename_is_not_echoed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    marker = "private-control.example.invalid"
    (repo / marker).write_text("ordinary text\n")
    git(repo, "add", marker)
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(marker + "\n")

    result = check(repo, denylist)

    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr


def test_removed_historical_identifier_still_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    marker = "private-control.example.invalid"
    file = repo / "README.md"
    file.write_text(marker + "\n")
    git(repo, "add", "README.md")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "first",
    )
    file.write_text("Public sample.\n")
    git(repo, "add", "README.md")
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(marker + "\n")

    result = check(repo, denylist)

    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr


def test_removed_historical_filename_still_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    marker = "private-control.example.invalid"
    file = repo / marker
    file.write_text("ordinary text\n")
    git(repo, "add", marker)
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "first",
    )
    file.unlink()
    git(repo, "add", "-u")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        "remove",
    )
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(marker + "\n")

    result = check(repo, denylist)

    assert result.returncode != 0
    assert marker not in result.stdout + result.stderr
