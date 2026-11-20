"""Local fixtures for the collector and verification filesystem boundaries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer
from ringer_file_scope import copy_output, open_directory, open_regular
from ringer_reliability import read_export_evidence


class FileScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.work = self.root / "work"
        self.state = self.root / "state"
        self.external = self.root / "outside"
        self.external.mkdir()
        self.fixture = self.external / "fixture.txt"
        self.fixture.write_text("inert outside fixture\n", encoding="utf-8")

    def runner(self, expected=(), worktrees=False):
        manifest = ringer.Manifest(
            run_name="file scope fixture", workdir=self.work, max_parallel=1,
            worktrees=worktrees, repo=self.root / "repo" if worktrees else None,
            tasks=(ringer.TaskSpec("worker", "Write an inert fixture.", "printf checked",
                                   expect_files=expected),),
        )
        config = ringer.AppConfig(
            path=None, identity_default=None, state_dir=self.state,
            dashboard_port_base=8787, hud_port=8700, hud_app_path=None, allow_full_access=False,
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "eval.jsonl"), engines={},
            artifact=ringer.ArtifactConfig(enabled=False, out_template="", report_template="",
                                           index_out=self.root / "index.html"),
        )
        runner = ringer.RingerRunner(manifest, config, "fixture", dashboard_enabled=False)
        runtime = runner.runtimes[0]
        runtime.taskdir.mkdir(parents=True)
        return runner, runtime

    def test_implicit_collection_ignores_links_and_special_files(self):
        runner, runtime = self.runner()
        (runtime.taskdir / "good.md").write_text("ordinary output\n", encoding="utf-8")
        (runtime.taskdir / "linked.md").symlink_to(self.fixture)
        os.link(self.fixture, runtime.taskdir / "hard.md")
        os.mkfifo(runtime.taskdir / "pipe.md")
        runner._harvest_deliverables_on_pass(runtime)
        self.assertEqual("PASS", runtime.export_state)
        self.assertEqual(["good.md"], [item["name"] for item in runtime.deliverables])

    def test_declared_collection_rejects_leaf_parent_and_traversal(self):
        invalid_paths = ("leaf.txt", "dir-link/fixture.txt", "../../outside/fixture.txt",
                         str(self.external / "absolute-link.txt"))
        runner, runtime = self.runner((*invalid_paths, str(self.fixture), "safe/report.txt"))
        (runtime.taskdir / "leaf.txt").symlink_to(self.fixture)
        (runtime.taskdir / "dir-link").symlink_to(self.external, target_is_directory=True)
        (self.external / "absolute-link.txt").symlink_to(self.fixture)
        (runtime.taskdir / "safe").mkdir()
        (runtime.taskdir / "safe/report.txt").write_text("ordinary report", encoding="utf-8")
        self.assertEqual(invalid_paths, ringer.Verifier._missing_expected_files(runtime.task, runtime.taskdir))
        runner._harvest_deliverables_on_pass(runtime)
        self.assertEqual("NEEDS_CHANGE", runtime.export_state)
        self.assertEqual(["fixture.txt", "report.txt"], [item["name"] for item in runtime.deliverables])
        for item in runtime.deliverables:
            data = Path(item["path"]).read_bytes()
            self.assertEqual(item["bytes"], len(data))
            self.assertEqual(item["source_sha256"], hashlib.sha256(data).hexdigest())
            self.assertEqual(item["sha256"], item["source_sha256"])

    def test_copy_keeps_the_open_handle_when_a_parent_name_changes(self):
        task = self.root / "task"
        (task / "sub").mkdir(parents=True)
        (task / "sub/fixture.txt").write_text("approved bytes", encoding="utf-8")
        target = self.root / "publish"
        target.mkdir()
        with open_directory(task) as source_fd, open_directory(target) as target_fd:
            with open_regular(source_fd, ("sub", "fixture.txt")) as (stream, info):
                (task / "sub").rename(task / "moved")
                (task / "sub").symlink_to(self.external, target_is_directory=True)
                size, digest = copy_output(stream, info, target_fd, "fixture.txt")
        self.assertEqual(b"approved bytes", (target / "fixture.txt").read_bytes())
        self.assertEqual(14, size)
        self.assertEqual(hashlib.sha256(b"approved bytes").hexdigest(), digest)

    def test_worktree_report_snapshot_and_legacy_wrapper_reject_links(self):
        runner, runtime = self.runner(worktrees=True)
        linked = runtime.taskdir / "report.md"
        linked.symlink_to(self.fixture)
        report = runtime.taskdir / "report.html"
        report.write_text("<p>inert report</p>", encoding="utf-8")
        runner._snapshot_worktree_reports(runtime)
        self.assertEqual({"report.html"}, runtime.report_paths.keys())
        self.assertEqual(report.read_bytes(), runtime.report_paths["report.html"].read_bytes())
        renderer = ringer.ArtifactRenderer(self.state / "run.html")
        with self.assertRaises(OSError):
            renderer.link_for_source(linked, task_key="worker")
        self.assertFalse((self.state / "view").exists())

    def test_historical_export_shape_and_linked_readback(self):
        historical = read_export_evidence([{"path": str(self.fixture)}])[0]
        self.assertIsNone(historical["matches"])
        old_location = self.root / "legacy.txt"
        old_location.symlink_to(self.fixture)
        redirected = read_export_evidence([{"path": str(old_location)}])[0]
        self.assertFalse(redirected["matches"])
        self.assertNotIn("actual_sha256", redirected)

    def test_copy_failure_leaves_export_evidence_unpublished(self):
        runner, runtime = self.runner(("report.txt",))
        (runtime.taskdir / "report.txt").write_text("inert report")
        with patch("ringer.copy_output", side_effect=OSError("fixture copy failure")):
            runner._harvest_deliverables_on_pass(runtime)
        self.assertEqual("NEEDS_CHANGE", runtime.export_state)
        self.assertEqual([], runtime.deliverables)
        self.assertIn("fixture copy failure", runtime.deliverable_notes[0])
        self.assertFalse(list(self.state.rglob("harvest-manifest.json")))


class VerificationScopeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="ringer fixture ")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.task = self.root / "task"
        self.source = self.root / "approved source"
        self.task.mkdir()
        self.source.mkdir()
        self.sibling = self.root / "ungranted.txt"
        self.sibling.write_text("inert sibling", encoding="utf-8")

    def verify(self, check, *, expected=(), inputs=()):
        task = ringer.TaskSpec.from_obj({
            "key": "worker", "spec": "Use local fixtures only.", "check": check,
            "expect_files": list(expected), "check_read_paths": [str(path) for path in inputs],
        })
        return asyncio.run(ringer.Verifier().verify(task, self.task))

    def test_shared_verifier_executes_code_in_disposable_workspace(self):
        approved = self.source / "number.txt"
        approved.write_text("42", encoding="utf-8")
        checker = self.task / "fixture.py"
        checker.write_text(f'''from pathlib import Path
import os
import socket
assert "RINGER_TEST_MARKER" not in os.environ
assert Path({str(approved)!r}).read_text() == "42"
try:
    Path({str(self.sibling)!r}).read_bytes()
except OSError:
    pass
else:
    raise AssertionError("ungranted file was readable")
try:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
except OSError:
    pass
else:
    raise AssertionError("network socket unexpectedly available")
try:
    Path({str(approved)!r}).write_text("would change source")
except OSError:
    pass
else:
    raise AssertionError("source unexpectedly writable")
Path("scratch-only.txt").write_text("discarded")
Path("nested").mkdir()
Path("nested/result.txt").write_text("verified")
''', encoding="utf-8")
        with patch.dict(os.environ, RINGER_TEST_MARKER="inert fixture"):
            result = self.verify("python3 fixture.py", expected=("nested/result.txt",), inputs=(self.source,))
        self.assertTrue(result.ok, result.raw_output_excerpt)
        self.assertEqual("42", approved.read_text())
        self.assertFalse((self.task / "scratch-only.txt").exists())
        self.assertEqual("verified", (self.task / "nested/result.txt").read_text())

    @unittest.skipUnless(sys.platform == "linux", "Linux supports mapped absolute checker outputs")
    def test_exact_absolute_outputs_preserve_legacy_manifests(self):
        output = self.root / "new-output-dir" / "result.txt"
        declined = output.parent / "scratch-only.txt"
        command = f"mkdir -p {shlex.quote(str(output.parent))}; " + shlex.join([
            "python3", "-c",
            f"from pathlib import Path; Path({str(output)!r}).write_text('published'); "
            f"Path({str(declined)!r}).write_text('discarded')",
        ])
        result = self.verify(command, expected=(str(output),))
        self.assertTrue(result.ok, result.raw_output_excerpt)
        self.assertEqual("published", output.read_text())
        self.assertFalse(declined.exists())

    def test_unavailable_sandbox_never_runs_checker(self):
        with patch.object(sys, "platform", "unsupported-fixture-os"):
            result = self.verify("printf populated > fixture.txt", expected=("fixture.txt",))
        self.assertFalse(result.ok)
        self.assertIn("no verification sandbox", result.raw_output_excerpt)
        self.assertFalse((self.task / "fixture.txt").exists())

    def test_git_check_uses_a_private_index_for_a_linked_checkout(self):
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        metadata = self.source / ".git"
        (self.task / ".git").write_text(f"gitdir: {metadata}\n")
        payload = self.task / "fixture.txt"
        payload.write_text("version from fixture")
        task = ringer.TaskSpec("worker", "Git fixture.",
                               'git add fixture.txt && test "$(git show :fixture.txt)" = "version from fixture"')

        async def verify():
            verifier = ringer.Verifier()
            await verifier.register_worktree(task, self.task)
            return await verifier.verify(task, self.task)

        result = asyncio.run(verify())
        self.assertTrue(result.ok, result.raw_output_excerpt)
        self.assertFalse((metadata / "index").exists())

    def doc_check(self, language, runner, examples, *, nested=False):
        doc = self.task / "fixture.md"
        examples_text = "\n".join(f"```{language}\n{block}\n```\n" for block in examples)
        doc.write_text(f"# Documented Symbols\n\n`fixture_token`\n\n# Examples\n\n{examples_text}",
                       encoding="utf-8")
        checker = ROOT / "templates/doc-swarm/checks/doc_check.py"
        args = ["python3", str(checker), "--doc-path", "fixture.md", "--source-root", str(self.source),
                "--required-sections", "Documented Symbols;Examples", "--min-words", "2",
                "--min-section-words", "1", "--symbol-section", "Documented Symbols",
                "--runnable-language", language, "--example-runner", runner,
                "--example-cwd", str(self.source)]
        if nested:
            modules = tuple(ROOT / filename for filename in
                            ("ringer_check_sandbox.py", "ringer_file_scope.py", "ringer_reliability.py"))
            result = self.verify(shlex.join(args), expected=("fixture.md",),
                                 inputs=(self.source, checker, *modules))
            self.assertTrue(result.ok, result.raw_output_excerpt)
            return result.raw_output_excerpt
        result = subprocess.run([sys.executable, *args[1:]], cwd=self.task, text=True, capture_output=True, timeout=15)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout

    def test_doc_checker_python_examples_use_fresh_scratch_and_read_only_source(self):
        module = self.source / "fixture_module.py"
        module.write_text("fixture_token = 42\n", encoding="utf-8")
        first = f'''from pathlib import Path
import os
import socket
from fixture_module import fixture_token
assert fixture_token == 42
assert "RINGER_TEST_MARKER" not in os.environ
for action in (lambda: socket.socket().bind(("127.0.0.1", 0)), lambda: Path("fixture_module.py").write_text("changed"),
               lambda: Path({str(self.sibling)!r}).read_bytes()):
    try:
        action()
    except OSError:
        pass
    else:
        raise AssertionError("confinement check failed")
Path("example-local.txt").write_text("example scratch")
'''
        second = 'from pathlib import Path\nassert not Path("example-local.txt").exists()'
        with patch.dict(os.environ, RINGER_TEST_MARKER="inert fixture"):
            output = self.doc_check("python", "python3 {file}", (first, second))
        self.assertIn("2 runnable examples executed", output)
        self.assertEqual("fixture_token = 42\n", module.read_text())
        self.assertFalse((self.source / "example-local.txt").exists())

    def test_doc_checker_node_runs_via_shared_verifier(self):
        (self.source / "fixture.cjs").write_text("exports.fixture_token = 42;\n", encoding="utf-8")
        example = '''const assert = require("node:assert/strict");
const fs = require("node:fs");
assert.equal(require("./fixture.cjs").fixture_token, 42);
assert.throws(() => fs.writeFileSync("fixture.cjs", "changed"));
fs.writeFileSync("example-local.txt", "example scratch");
'''
        output = self.doc_check("javascript", 'node "{file}"', (example,), nested=True)
        self.assertIn("1 runnable examples executed", output)
        self.assertFalse((self.source / "example-local.txt").exists())

    def test_checker_timeout_keeps_output_and_discards_workspace(self):
        check = "printf scratch > note.txt; printf 'inert start\\n'; sleep 10"
        task = ringer.TaskSpec("worker", "Timeout fixture.", check, check_timeout_s=0.5)
        result = asyncio.run(ringer.Verifier().verify(task, self.task))
        self.assertTrue(result.check_timed_out, result.raw_output_excerpt)
        self.assertIn("inert start", result.raw_output_excerpt)
        self.assertFalse((self.task / "note.txt").exists())

    def test_checker_cancellation_keeps_host_log_and_discards_workspace(self):
        log = self.root / "checker.log"
        command = "printf 'started fixture\\n'; printf scratch > local.txt; sleep 10"

        async def run():
            check = asyncio.create_task(ringer.Verifier._run_check(command, self.task, log_path=log))
            try:
                async with asyncio.timeout(5):
                    while not log.exists() or "started fixture" not in log.read_text():
                        await asyncio.sleep(0.01)
            finally:
                check.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(check, 3)

        asyncio.run(run())
        self.assertEqual("started fixture\n", log.read_text())
        self.assertFalse((self.task / "local.txt").exists())

    def test_standard_demo_check_commands_with_offline_files(self):
        manifest_path = ringer.create_demo_manifest()
        self.addCleanup(manifest_path.parent.rmdir)
        self.addCleanup(manifest_path.unlink)
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        for raw in original["tasks"]:
            with self.subTest(task=raw["key"]):
                task = ringer.TaskSpec.from_obj(raw)
                (self.task / task.expect_files[0]).write_text(f"{task.key} ready\n", encoding="utf-8")
                result = asyncio.run(ringer.Verifier().verify(task, self.task))
                self.assertTrue(result.ok, result.raw_output_excerpt)


if __name__ == "__main__":
    unittest.main()
