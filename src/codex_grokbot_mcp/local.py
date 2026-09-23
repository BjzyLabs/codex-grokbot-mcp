"""Local source boundary and untrusted coding-artifact validation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

MAX_SNAPSHOT_BYTES = 256 * 1024
MAX_PATCH_BYTES = 1024 * 1024


class ContractError(ValueError):
    """A source or artifact violates the bounded delegation contract."""


def _git(cwd: Path, *args: str, input_data: bytes | None = None) -> bytes:
    try:
        result = subprocess.run(  # noqa: S603
            ["git", *args],  # noqa: S607
            cwd=cwd,
            input=input_data,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ContractError("Git validation failed") from error
    return result.stdout


def _github_repository(origin: str) -> str:
    if origin.startswith("git@github.com:"):
        slug = origin.removeprefix("git@github.com:")
    else:
        parsed = urlparse(origin)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username:
            raise ContractError("origin must be a GitHub HTTPS or SSH repository")
        slug = parsed.path.lstrip("/")
    slug = slug.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise ContractError("origin repository identity is invalid")
    return slug


def _relative_path(root: Path, raw: str) -> Path:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ContractError("invalid delegated path")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or str(pure) != raw
        or any(part in (".", "..") for part in raw.split("/"))
    ):
        raise ContractError("delegated path escapes workspace")
    if any(
        part.lower() == ".git"
        or part.lower() == ".env"
        or part.lower().startswith(".env.")
        or part.lower().endswith((".pem", ".key"))
        or part.lower().startswith(("credential", "secret"))
        or part.lower() in ("id_rsa", "id_ed25519")
        for part in pure.parts
    ):
        raise ContractError("sensitive delegated path")
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ContractError("symlinks are not delegated")
    if not current.resolve(strict=False).is_relative_to(root):
        raise ContractError("delegated path escapes workspace")
    if current.exists() and not current.is_file():
        raise ContractError("delegated path is not a regular file")
    return current


def _source_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    if len(data) > MAX_SNAPSHOT_BYTES or b"\x00" in data:
        raise ContractError("source file is binary or exceeds the snapshot limit")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContractError("source file is not UTF-8") from error
    return data


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class SourceSnapshot:
    path: str
    sha256: str
    content: str

    def public_record(self) -> dict[str, str]:
        return {"path": self.path, "sha256": self.sha256, "content": self.content}


@dataclass(frozen=True)
class DelegationContext:
    root: Path
    target_repo: str
    head: str
    branch: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    source_snapshots: tuple[SourceSnapshot, ...]
    write_hashes: tuple[tuple[str, str | None], ...]
    snapshot_digest: str

    def public_context(self) -> dict:
        """Serialize only content intentionally approved for the worker."""
        return {
            "target_repo": self.target_repo,
            "workspace_head": self.head,
            "snapshot_digest": self.snapshot_digest,
            "read_paths": list(self.read_paths),
            "write_paths": list(self.write_paths),
            "source_snapshots": [item.public_record() for item in self.source_snapshots],
        }


@dataclass(frozen=True)
class Workspace:
    root: Path
    target_repo: str
    head: str
    branch: str

    @classmethod
    def open(cls, path: Path, *, opted_in: bool) -> Workspace:
        if not opted_in:
            raise ContractError("workspace opt-in is required")
        root = Path(_git(path, "rev-parse", "--show-toplevel").decode().strip()).resolve()
        origin = _git(root, "remote", "get-url", "origin").decode().strip()
        head = _git(root, "rev-parse", "HEAD").decode().strip()
        branch = _git(root, "branch", "--show-current").decode().strip()
        return cls(root, _github_repository(origin), head, branch)

    def snapshot(self, *, read_paths: list[str], write_paths: list[str]) -> DelegationContext:
        if not read_paths or not write_paths:
            raise ContractError("read_paths and write_paths must be nonempty")
        if len(set(read_paths)) != len(read_paths) or len(set(write_paths)) != len(write_paths):
            raise ContractError("duplicate delegated path")
        snapshots: list[SourceSnapshot] = []
        read_set = set(read_paths)
        for raw in sorted(read_paths):
            path = _relative_path(self.root, raw)
            if not path.exists():
                raise ContractError("read path does not exist")
            data = _source_bytes(path)
            snapshots.append(SourceSnapshot(raw, _digest(data), data.decode("utf-8")))
        write_hashes: list[tuple[str, str | None]] = []
        for raw in sorted(write_paths):
            path = _relative_path(self.root, raw)
            if path.exists() and raw not in read_set:
                raise ContractError("existing write paths must also be readable")
            write_hashes.append((raw, _digest(_source_bytes(path)) if path.exists() else None))
        read = tuple(sorted(read_paths))
        write = tuple(sorted(write_paths))
        canonical = {
            "target_repo": self.target_repo,
            "head": self.head,
            "read_paths": read,
            "write_paths": write,
            "snapshots": [(item.path, item.sha256) for item in snapshots],
            "write_hashes": write_hashes,
        }
        digest = _digest(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode())
        return DelegationContext(
            self.root,
            self.target_repo,
            self.head,
            self.branch,
            read,
            write,
            tuple(snapshots),
            tuple(write_hashes),
            digest,
        )


@dataclass(frozen=True)
class ValidatedPatch:
    patch: str
    changed_paths: tuple[str, ...]


def _actual_patch_paths(context: DelegationContext, patch: str) -> tuple[str, ...]:
    raw_patch = patch.encode("utf-8")
    if not raw_patch or len(raw_patch) > MAX_PATCH_BYTES:
        raise ContractError("patch is empty or exceeds the size limit")
    lines = patch.splitlines()
    if any(
        line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")) for line in lines
    ):
        raise ContractError("renames and copies are unsupported")
    header_paths: set[str] = set()
    for index, line in enumerate(lines[:-1]):
        if not line.startswith("--- ") or not lines[index + 1].startswith("+++ "):
            continue
        old = line[4:].split("\t", 1)[0]
        new = lines[index + 1][4:].split("\t", 1)[0]
        if old == "/dev/null" and new == "/dev/null":
            raise ContractError("patch has no file target")
        old_path = old.removeprefix("a/") if old != "/dev/null" else None
        new_path = new.removeprefix("b/") if new != "/dev/null" else None
        if old_path and new_path and old_path != new_path:
            raise ContractError("renames are unsupported")
        target = new_path or old_path
        if target is None:
            raise ContractError("patch has no file target")
        for candidate in (old_path, new_path):
            if candidate:
                _relative_path(context.root, candidate)
        header_paths.add(target)
    if not header_paths:
        raise ContractError("patch lacks unified file headers")
    numstat = _git(context.root, "apply", "--numstat", "-z", "-", input_data=raw_patch)
    actual: list[str] = []
    for record in numstat.split(b"\x00"):
        if not record:
            continue
        pieces = record.split(b"\t", 2)
        if len(pieces) != 3 or not pieces[2] or b"\x00" in pieces[2]:
            raise ContractError("unsupported patch path encoding")
        if pieces[0] == b"-" or pieces[1] == b"-":
            raise ContractError("binary patches are unsupported")
        try:
            path = pieces[2].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContractError("patch path is not UTF-8") from error
        _relative_path(context.root, path)
        actual.append(path)
    if not actual or len(actual) != len(set(actual)) or set(actual) != header_paths:
        raise ContractError("patch path headers do not match Git's parsed paths")
    summary = _git(context.root, "apply", "--summary", "-", input_data=raw_patch)
    for line in summary.decode("utf-8", "replace").splitlines():
        match = re.fullmatch(r" (?:create|delete)(?: mode 100644)? (.+)", line)
        if match is None or match.group(1) not in actual:
            raise ContractError("patch metadata changes are unsupported")
    return tuple(sorted(actual))


def _check_workspace_drift(context: DelegationContext) -> None:
    expected = {item.path: item.sha256 for item in context.source_snapshots}
    expected.update(dict(context.write_hashes))
    for raw, sha256 in expected.items():
        path = _relative_path(context.root, raw)
        current = _digest(_source_bytes(path)) if path.exists() else None
        if current != sha256:
            raise ContractError("workspace_drift: delegated source changed")
    current_head = _git(context.root, "rev-parse", "HEAD").decode().strip()
    if current_head != context.head:
        raise ContractError("workspace_drift: HEAD changed")


def _check_patch_application(context: DelegationContext, patch: str) -> None:
    with tempfile.TemporaryDirectory(prefix="grokbot-validate-") as directory:
        worktree = Path(directory)
        _git(context.root, "worktree", "add", "--detach", "--", str(worktree), context.head)
        try:
            for item in context.source_snapshots:
                path = worktree / item.path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(item.content, encoding="utf-8")
            _git(worktree, "apply", "--check", "-", input_data=patch.encode("utf-8"))
            _git(worktree, "apply", "-", input_data=patch.encode("utf-8"))
        finally:
            _git(context.root, "worktree", "remove", "--force", "--", str(worktree))


def validate_artifact(
    context: DelegationContext, expected_job_id: str, artifact: dict
) -> ValidatedPatch:
    """Validate an untrusted v1/v2 coding artifact without changing real source."""
    if not isinstance(artifact, dict):
        raise ContractError("artifact must be an object")
    version = artifact.get("schema_version")
    if version not in ("v1", "v2") or artifact.get("job_type") != "coding":
        raise ContractError("unsupported coding artifact")
    if artifact.get("job_id") != expected_job_id:
        raise ContractError("job_id mismatch")
    if artifact.get("target_repo") != context.target_repo:
        raise ContractError("target_repo mismatch")
    if artifact.get("base_sha") != context.head:
        raise ContractError("base_sha mismatch")
    if version == "v2":
        if artifact.get("workspace_head") != context.head:
            raise ContractError("workspace_head mismatch")
        if artifact.get("snapshot_digest") != context.snapshot_digest:
            raise ContractError("snapshot_digest mismatch")
    declared = artifact.get("declared_changed_paths")
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(p, str) for p in declared)
    ):
        raise ContractError("declared_changed_paths is invalid")
    if len(declared) != len(set(declared)):
        raise ContractError("declared_changed_paths contains duplicates")
    for path in declared:
        _relative_path(context.root, path)
    patch = artifact.get("patch")
    if not isinstance(patch, str):
        raise ContractError("patch must be text")
    actual = _actual_patch_paths(context, patch)
    if set(declared) != set(actual):
        raise ContractError("declared changed paths do not equal actual patch paths")
    if not set(actual).issubset(context.write_paths):
        raise ContractError("actual patch paths exceed write_paths")
    _check_workspace_drift(context)
    _check_patch_application(context, patch)
    return ValidatedPatch(patch, actual)
