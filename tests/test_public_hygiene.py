from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_public_hygiene.py"
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "codex_grokbot_mcp"

REQUESTOR_MODULES = {
    "__init__",
    "config",
    "coordinator",
    "deliver",
    "inbox",
    "jobs",
    "server",
    "tls",
    "webhook",
}
REMOVED_MODULES = {"control", "inspection", "local", "protocol", "vault"}


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)  # noqa: S603


def check(repo: Path, denylist: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(SCRIPT), "--denylist", str(denylist)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


class PublicHygieneScriptTests(unittest.TestCase):
    """The public denylist gate fails closed without echoing the private value."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.tmp = Path(self._temporary.name)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")

    def commit(self, message: str) -> None:
        git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            message,
        )

    def denylist(self, text: str) -> Path:
        path = self.tmp / "denylist.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_denylist_fails_closed(self) -> None:
        (self.repo / "README.md").write_text("Public sample.\n", encoding="utf-8")
        git(self.repo, "add", "README.md")

        result = check(self.repo, self.tmp / "missing-denylist.txt")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("denylist", result.stderr.lower())

    def test_staged_private_identifier_fails_without_printing_it(self) -> None:
        marker = "private-control.example.invalid"
        (self.repo / "README.md").write_text(f"Connect to {marker}.\n", encoding="utf-8")
        git(self.repo, "add", "README.md")

        result = check(self.repo, self.denylist(marker + "\n"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Git content", result.stderr)
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_clean_staged_tree_passes(self) -> None:
        (self.repo / "README.md").write_text(
            "Use vault.example.invalid in docs.\n", encoding="utf-8"
        )
        git(self.repo, "add", "README.md")

        result = check(self.repo, self.denylist("private-control.example.invalid\n"))

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_private_filename_is_not_echoed(self) -> None:
        marker = "private-control.example.invalid"
        (self.repo / marker).write_text("ordinary text\n", encoding="utf-8")
        git(self.repo, "add", marker)

        result = check(self.repo, self.denylist(marker + "\n"))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_removed_historical_identifier_still_fails(self) -> None:
        marker = "private-control.example.invalid"
        path = self.repo / "README.md"
        path.write_text(marker + "\n", encoding="utf-8")
        git(self.repo, "add", "README.md")
        self.commit("first")
        path.write_text("Public sample.\n", encoding="utf-8")
        git(self.repo, "add", "README.md")

        result = check(self.repo, self.denylist(marker + "\n"))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)

    def test_removed_historical_filename_still_fails(self) -> None:
        marker = "private-control.example.invalid"
        path = self.repo / marker
        path.write_text("ordinary text\n", encoding="utf-8")
        git(self.repo, "add", marker)
        self.commit("first")
        path.unlink()
        git(self.repo, "add", "-u")
        self.commit("remove")

        result = check(self.repo, self.denylist(marker + "\n"))

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)


class RequestorModuleSetTests(unittest.TestCase):
    """The webhook-only requestor keeps exactly one module per responsibility."""

    def test_module_set_is_exactly_the_requestor_modules(self) -> None:
        modules = {path.stem for path in PACKAGE.glob("*.py")}

        self.assertEqual(modules, REQUESTOR_MODULES)

    def test_removed_modules_are_absent(self) -> None:
        self.assertEqual(REQUESTOR_MODULES & REMOVED_MODULES, set())
        for name in REMOVED_MODULES:
            with self.subTest(module=name):
                self.assertFalse((PACKAGE / f"{name}.py").exists())

    def test_request_path_imports_only_local_modules(self) -> None:
        for name in sorted(REQUESTOR_MODULES - {"__init__"}):
            text = (PACKAGE / f"{name}.py").read_text(encoding="utf-8")
            with self.subTest(module=name):
                self.assertNotIn("from .vault", text)
                self.assertNotIn("from .control", text)
                self.assertNotIn("from .local", text)
                self.assertNotIn("from .inspection", text)
                self.assertNotIn("from .protocol", text)


if __name__ == "__main__":
    unittest.main()
