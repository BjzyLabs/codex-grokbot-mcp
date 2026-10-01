"""The package version is single-sourced and semver-shaped."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

import codex_grokbot_mcp

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_version_is_semver(self) -> None:
        self.assertRegex(codex_grokbot_mcp.__version__, r"^\d+\.\d+\.\d+$")

    def test_pyproject_takes_the_version_from_the_package(self) -> None:
        pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        project = pyproject["project"]
        self.assertEqual(project.get("dynamic"), ["version"])
        self.assertNotIn("version", project)
        self.assertEqual(
            pyproject["tool"]["hatch"]["version"]["path"],
            "src/codex_grokbot_mcp/__init__.py",
        )
