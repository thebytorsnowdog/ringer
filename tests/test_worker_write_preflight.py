"""Regression tests for declared worker edit roots. No model calls."""
import asyncio
import json
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ringer
from ringer_reliability import preflight_local


class WorkerWritePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.taskdir = self.root / "task dir"
        self.taskdir.mkdir()

    def contract(self, write_paths, command, *, restricted=True):
        return SimpleNamespace(
            preflight_files=(),
            preflight_write_paths=tuple(str(path) for path in write_paths),
            source_sha256={},
            check="true",
            expect_files=(),
            restricted_codex=restricted,
            worker_command=command,
        )

    @staticmethod
    def codex_command(*roots):
        value = json.dumps([str(root) for root in roots])
        return [
            "codex", "exec", "--sandbox", "workspace-write", "-c",
            f"sandbox_workspace_write.writable_roots={value}",
        ]

    def test_rejects_legacy_task_writable_roots_with_supported_placement(self):
        with self.assertRaisesRegex(
            ValueError,
            r"writable_roots is unsupported.*engine_args.*sandbox_workspace_write\.writable_roots",
        ):
            ringer.TaskSpec.from_obj(
                dict(key="edit", spec="Edit source", check="true", writable_roots=[str(self.root)])
            )

    def test_existing_declared_path_with_spaces_is_allowed_and_probed(self):
        source = self.root / "existing source"
        source.mkdir()
        parsed = ringer.TaskSpec.from_obj(dict(
            key="edit",
            spec="Edit source",
            check="true",
            preflight_write_paths=[str(source)],
        ))
        without_write_path = ringer.TaskSpec.from_obj(
            dict(key="edit", spec="Edit source", check="true")
        )
        self.assertEqual((str(source),), parsed.preflight_write_paths)
        self.assertEqual((str(source),), asdict(parsed)["preflight_write_paths"])
        self.assertNotEqual(without_write_path.execution_binding, parsed.execution_binding)
        task = self.contract([source], self.codex_command(source))
        self.assertEqual((None, ""), preflight_local(task, self.taskdir))
        self.assertEqual([], list(source.glob(".ringer-write-probe-*")))

    def test_taskdir_write_path_requires_workspace_write_mode(self):
        failure, message = preflight_local(
            self.contract(
                [self.taskdir],
                ["codex", "exec", "--sandbox", "read-only", "-C", str(self.taskdir), "test"],
            ),
            self.taskdir,
        )
        self.assertEqual("permission", failure)
        self.assertIn("must be workspace-write", message)

    def test_forwarded_root_requires_workspace_write_mode(self):
        source = self.root / "source"
        source.mkdir()
        failure, message = preflight_local(
            self.contract([source], [
                "codex", "exec", "-s", "read-only", "-c",
                f"sandbox_workspace_write.writable_roots={json.dumps([str(source)])}",
            ]),
            self.taskdir,
        )
        self.assertEqual("permission", failure)
        self.assertIn("must be workspace-write", message)

    def test_missing_sandbox_mode_blocks_declared_write_path(self):
        failure, message = preflight_local(
            self.contract([self.taskdir], ["codex", "exec", "-C", str(self.taskdir), "test"]),
            self.taskdir,
        )
        self.assertEqual("permission", failure)
        self.assertIn("missing or ambiguous", message)

    def test_sandbox_mode_override_is_ambiguous(self):
        failure, message = preflight_local(
            self.contract([self.taskdir], [
                "codex", "exec", "--sandbox=workspace-write", "-c", 'sandbox_mode="read-only"',
            ]),
            self.taskdir,
        )
        self.assertEqual("permission", failure)
        self.assertIn("sandbox_mode config override is ambiguous", message)

    def test_workspace_write_sandbox_option_forms_are_allowed(self):
        for option in (
            ["--sandbox=workspace-write"],
            ["-s", "workspace-write"],
        ):
            with self.subTest(option=option):
                self.assertEqual(
                    (None, ""),
                    preflight_local(
                        self.contract([self.taskdir], ["codex", "exec", *option]),
                        self.taskdir,
                    ),
                )

    def test_missing_declared_path_blocks(self):
        missing = self.root / "missing"
        failure, message = preflight_local(
            self.contract([missing], self.codex_command(missing)), self.taskdir
        )
        self.assertEqual("missing_dependency", failure)
        self.assertIn("preflight write path is unavailable", message)

    def test_path_outside_taskdir_without_forwarded_root_blocks(self):
        outside = self.root / "outside"
        outside.mkdir()
        failure, message = preflight_local(
            self.contract([outside], ["codex", "exec", "--sandbox", "workspace-write"]),
            self.taskdir,
        )
        self.assertEqual("permission", failure)
        self.assertIn("outside the resolved Codex workspace-write sandbox", message)

    def test_symlink_escape_from_taskdir_blocks(self):
        outside = self.root / "outside"
        outside.mkdir()
        link = self.taskdir / "source-link"
        link.symlink_to(outside, target_is_directory=True)
        failure, message = preflight_local(
            self.contract([link], ["codex", "exec", "--sandbox", "workspace-write"]),
            self.taskdir,
        )
        self.assertEqual("permission", failure)
        self.assertIn(str(outside.resolve()), message)

    def test_malformed_forwarded_writable_roots_blocks(self):
        outside = self.root / "outside"
        outside.mkdir()
        command = [
            "codex", "exec", "-c",
            "sandbox_workspace_write.writable_roots=[/path with spaces]",
        ]
        failure, message = preflight_local(self.contract([outside], command), self.taskdir)
        self.assertEqual("permission", failure)
        self.assertIn("malformed sandbox_workspace_write.writable_roots override", message)

    def test_missing_forwarded_sandbox_root_blocks(self):
        outside = self.root / "outside"
        outside.mkdir()
        missing_root = self.root / "missing sandbox root"
        failure, message = preflight_local(
            self.contract([outside], self.codex_command(missing_root)), self.taskdir
        )
        self.assertEqual("permission", failure)
        self.assertIn("sandbox workspace-write root is unavailable", message)

    def test_runner_blocks_impossible_write_mission_before_worker_invocation(self):
        outside = self.root / "outside"
        outside.mkdir()
        manifest = ringer.Manifest.from_obj(
            dict(
                run_name="write-preflight",
                workdir=str(self.root / "work"),
                tasks=[dict(
                    key="edit",
                    spec="Edit the declared source.",
                    check="true",
                    engine="codex",
                    preflight_write_paths=[str(outside)],
                )],
            )
        )
        engine = ringer.EngineConfig(
            name="codex",
            bin="/bin/sh",
            args_template=("exec", "--sandbox", "workspace-write", "{spec}"),
            sandbox_args=(),
            full_access_args=(),
            token_regex=None,
        )
        config = ringer.AppConfig(
            path=None,
            identity_default=None,
            state_dir=self.root / "state",
            dashboard_port_base=18787,
            hud_port=18788,
            hud_app_path=None,
            allow_full_access=False,
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
            engines={"codex": engine},
            artifact=ringer.ArtifactConfig(
                enabled=False, out_template="", report_template="", index_out=self.root / "index"
            ),
        )
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        with (
            patch.object(ringer, "validate_manifest_model_assessment", return_value={}),
            patch.object(runner, "_run_worker", side_effect=AssertionError("worker must not run")),
        ):
            self.assertEqual(1, asyncio.run(runner.run()))
        runtime = runner.runtimes[0]
        self.assertEqual("blocked", runtime.status)
        self.assertEqual("permission", runtime.failure_class)
        self.assertEqual(0, runtime.attempts)


if __name__ == "__main__":
    unittest.main()
