#!/usr/bin/env python3
"""Fail closed when public Git content contains private identifiers.

The caller supplies an uncommitted, external denylist. Run a secret scanner as a
separate gate; this tool checks locally known private identifiers and paths.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--denylist", type=Path, required=True)
    args = parser.parse_args()
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
        failures: set[str] = set()
        for path, data in staged_blobs() + historical_blobs():
            if any(term in path.encode("utf-8").lower() or term in data.lower() for term in terms):
                failures.add(path)
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
