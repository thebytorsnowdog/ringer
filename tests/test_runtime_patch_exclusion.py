"""Regression: Claude engine .runtime/ must not enter exported patches.

Removing the exclusion (plain `git add -A`, or copying ignored .runtime trees)
makes these tests fail. With the exclusion they pass, and the owned edit is
still exported.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIX_SWARM = ROOT / "templates" / "fix-swarm" / "checks" / "fix-swarm.py"
MIGRATION_CHECK = ROOT / "templates" / "migration-swarm" / "checks" / "migration_patch_check.py"
MIGRATION_MANIFEST = ROOT / "templates" / "migration-swarm" / "manifest.json"
HARDENING_CHECK = ROOT / "templates" / "test-hardening" / "checks" / "test_hardening_check.py"
SECRET = "SECRET-CREDENTIAL-DO-NOT-EXPORT"


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimePatchExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="runtime-export-")
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-b", "overnight/probe")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test User")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(f"git {args} failed ({result.returncode}): {result.stdout}")
        return result

    def commit_base(self) -> None:
        (self.repo / "file.txt").write_text("base\n", encoding="utf-8")
        self.git("add", "file.txt")
        self.git("commit", "-m", "base")

    def plant_runtime(self) -> None:
        for rel in (".runtime/.credentials.json", "sub/.runtime/.credentials.json"):
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(SECRET + "\n", encoding="utf-8")
        # A worker, or a previous add, may already have staged the engine tree.
        self.git("add", "-f", "--", ".runtime", "sub/.runtime")

    def assert_patch_omits_runtime(self, patch: str) -> None:
        self.assertNotIn(SECRET, patch)
        self.assertNotIn(".runtime/", patch)
        self.assertNotIn(".credentials.json", patch)
        self.assertIn("file.txt", patch)

    def test_fix_swarm_export_omits_runtime_credentials(self) -> None:
        self.commit_base()
        (self.repo / "file.txt").write_text("owned edit\n", encoding="utf-8")
        self.plant_runtime()
        (self.repo / "fix-summary.md").write_text(
            "\n".join(
                [
                    "# Fix Summary",
                    "",
                    "## Summary",
                    "Exclusion regression.",
                    "",
                    "## Files Changed",
                    "file.txt",
                    "",
                    "## Verification",
                    "The exported patch omits .runtime.",
                    "",
                    "## Assumptions",
                    "None.",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        patch = self.root / "task.patch"
        result = subprocess.run(
            [
                sys.executable,
                str(FIX_SWARM),
                "--verify-command",
                f"{shlex.quote(sys.executable)} -c {shlex.quote('raise SystemExit(0)')}",
                "--patch",
                str(patch),
                "--summary",
                "fix-summary.md",
                "--exported-summary",
                str(self.root / "task.summary.md"),
                "--owned-files",
                "file.txt",
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(patch.is_file(), result.stdout)
        self.assert_patch_omits_runtime(patch.read_text(encoding="utf-8"))

    def test_migration_stage_command_omits_runtime_credentials(self) -> None:
        self.commit_base()
        (self.repo / "file.txt").write_text("owned edit\n", encoding="utf-8")
        self.plant_runtime()
        check = json.loads(MIGRATION_MANIFEST.read_text(encoding="utf-8"))["tasks"][0]["check"]
        marker = "git diff --cached >"
        self.assertIn(marker, check)
        stage = check.split(marker, 1)[0].strip()
        if stage.endswith("&&"):
            stage = stage[:-2].strip()
        self.assertNotIn("git add -A &&", stage)
        result = subprocess.run(
            stage,
            cwd=self.repo,
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        diff = self.git("diff", "--cached")
        self.assert_patch_omits_runtime(diff.stdout)

    def test_migration_checker_drops_runtime_from_ignored_exports_and_staged(self) -> None:
        self.commit_base()
        (self.repo / "file.txt").write_text("owned edit\n", encoding="utf-8")
        ignored = self.repo / "pkg" / ".runtime" / ".credentials.json"
        ignored.parent.mkdir(parents=True)
        ignored.write_text(SECRET + "\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("pkg/.runtime/\n", encoding="utf-8")
        self.git("add", "file.txt")
        patch = self.root / "mig.patch"
        patch.write_text("not empty\n", encoding="utf-8")
        export_dir = self.root / "export"
        result = subprocess.run(
            [
                sys.executable,
                str(MIGRATION_CHECK),
                "--task-key",
                "mig",
                "--patch",
                str(patch),
                "--export-dir",
                str(export_dir),
                "--owned-files",
                "file.txt;pkg",
                "--ignored-exports",
                "AUTO",
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout)
        copied = list(export_dir.rglob(".credentials.json")) if export_dir.exists() else []
        self.assertEqual(copied, [], result.stdout)
        self.assertNotIn(SECRET, result.stdout)

        self.git("add", "-f", "--", "pkg/.runtime")
        refused = subprocess.run(
            [
                sys.executable,
                str(MIGRATION_CHECK),
                "--task-key",
                "mig",
                "--patch",
                str(patch),
                "--export-dir",
                str(export_dir),
                "--owned-files",
                "file.txt;pkg",
                "--ignored-exports",
                "AUTO",
            ],
            cwd=self.repo,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        self.assertEqual(refused.returncode, 1, refused.stdout)
        self.assertIn("refusing to export Claude engine .runtime/", refused.stdout)

    def test_test_hardening_export_omits_runtime_credentials(self) -> None:
        self.commit_base()
        (self.repo / "file.txt").write_text("owned edit\n", encoding="utf-8")
        self.plant_runtime()
        module = load_module(HARDENING_CHECK)
        previous = Path.cwd()
        os.chdir(self.repo)
        try:
            visible = module.changed_paths_from_status()
            patch = self.root / "hardening.patch"
            exported = module.export_patch(patch, ["file.txt"], [])
        finally:
            os.chdir(previous)
        self.assertFalse(any(".runtime" in path for path in visible))
        self.assertTrue(exported)
        self.assertTrue(patch.is_file())
        self.assert_patch_omits_runtime(patch.read_text(encoding="utf-8"))

    def test_gitignore_keeps_runtime_out_of_a_plain_add(self) -> None:
        rule = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".runtime/", rule)
        self.commit_base()
        (self.repo / ".gitignore").write_text(rule, encoding="utf-8")
        (self.repo / "file.txt").write_text("owned edit\n", encoding="utf-8")
        cred = self.repo / ".runtime" / ".credentials.json"
        cred.parent.mkdir()
        cred.write_text(SECRET + "\n", encoding="utf-8")
        self.git("add", "-A")
        staged = self.git("diff", "--cached", "--name-only").stdout
        self.assertNotIn(".runtime", staged)
        self.assertNotIn(SECRET, self.git("diff", "--cached").stdout)


if __name__ == "__main__":
    unittest.main()
