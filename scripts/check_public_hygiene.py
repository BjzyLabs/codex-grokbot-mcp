#!/usr/bin/env python3
"""Fail closed when public Git content contains private identifiers.

The caller supplies an uncommitted, external denylist. Run a secret scanner as a
separate gate; this tool checks locally known private identifiers and paths.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

# Owner-approved legacy Vault-mount metadata; see docs/public-hygiene.md.
# Keys pin path and exact content; values pin the one accepted identifier.
# These exceptions never apply to staged files or filename matches.
HISTORICAL_EXCEPTIONS: dict[tuple[str, str], frozenset[str]] = {
    (
        "tests/test_coordinator.py",
        "4b1527ff3ea145f590427784ceb8cf453441ebd8ffe1825f4cf68b5e1257b2c8",
    ): frozenset({"a35ce218f7056596e61505fa89baf28e1a54fbb178900be654c6db69a85a9083"}),
    (
        "tests/test_diagnostic_coordinator.py",
        "f4b086057bec36dfa07751026fa044a8a6501f85360b3b67dc5813122ff02e7c",
    ): frozenset({"a35ce218f7056596e61505fa89baf28e1a54fbb178900be654c6db69a85a9083"}),
    (
        "tests/test_diagnostic_coordinator.py",
        "df5173e6dc0341c5bb4f14d3c46c8597687793a5553113379cd392beea4d0c89",
    ): frozenset({"a35ce218f7056596e61505fa89baf28e1a54fbb178900be654c6db69a85a9083"}),
}


def git(*args: str) -> bytes:
    # Git is a fixed executable; arguments are constructed by this module.
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        check=True,
        capture_output=True,
    ).stdout


def staged_blobs() -> list[tuple[str, bytes]]:
    blobs: list[tuple[str, bytes]] = []
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        _mode, oid, stage = metadata.split()
        if stage != b"0":
            continue
        path = raw_path.decode("utf-8", "replace")
        blobs.append((path, git("cat-file", "blob", oid.decode("ascii"))))
    return blobs


def historical_blobs() -> list[tuple[str, bytes]]:
    blobs: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for record in git("rev-list", "--objects", "--all").splitlines():
        oid, _, raw_path = record.partition(b" ")
        if oid in seen or git("cat-file", "-t", oid.decode("ascii")) != b"blob\n":
            continue
        seen.add(oid)
        path = raw_path.decode("utf-8", "replace") or "(historical blob)"
        blobs.append((path, git("cat-file", "blob", oid.decode("ascii"))))
    return blobs


def denied_paths(
    blobs: list[tuple[str, bytes]], terms: list[bytes], *, historical: bool = False
) -> set[str]:
    failures: set[str] = set()
    for path, data in blobs:
        accepted = (
            HISTORICAL_EXCEPTIONS.get((path, hashlib.sha256(data).hexdigest()), frozenset())
            if historical
            else frozenset()
        )
        for term in terms:
            if term in path.encode("utf-8").lower() or (
                term in data.lower() and hashlib.sha256(term).hexdigest() not in accepted
            ):
                failures.add(path)
                break
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--denylist", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        root = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
        denylist = args.denylist.resolve(strict=True)
        if denylist.is_relative_to(root):
            raise ValueError("denylist must be outside the repository")
        terms = [
            line.strip().encode("utf-8").lower()
            for line in denylist.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        if not terms:
            raise ValueError("denylist must contain at least one identifier")
        failures = denied_paths(staged_blobs(), terms)
        failures |= denied_paths(historical_blobs(), terms, historical=True)
        if failures:
            print(
                f"Private identifier found in Git content ({len(failures)} blob(s)); "
                "paths and values redacted.",
                file=sys.stderr,
            )
            return 1
        print("Public identifier check passed for staged content and Git history.")
        return 0
    except (FileNotFoundError, OSError, ValueError, subprocess.CalledProcessError) as error:
        if isinstance(error, subprocess.CalledProcessError):
            message = "Git content could not be inspected"
        elif isinstance(error, FileNotFoundError):
            message = "external denylist file is missing"
        else:
            message = str(error)
        print(f"Public identifier check failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
