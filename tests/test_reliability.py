"""Deterministic reliability regressions. All workers are local shell processes."""
import asyncio
from dataclasses import replace
import json
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ringer


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
    def runner(self, command="echo worker-output; printf yes > out.txt", tasks=None, **fields):
        task = dict(key="one", spec="Write the requested output file.", engine="local",
                    check="test -s out.txt", expect_files=["out.txt"],
                    model="fixture-local-model", billing_route="subscription")
        task.update(fields)
        raw_manifest = dict(run_name="reliability", workdir=str(self.root / "work"),
                            tasks=tasks or [task])
        for raw_task in raw_manifest["tasks"]:
            raw_task.setdefault("model", "fixture-local-model")
            raw_task.setdefault("billing_route", "subscription")
        manifest = ringer.Manifest.from_obj(raw_manifest)
        engine = ringer.EngineConfig(name="local", bin="/bin/sh", args_template=("-c", command, "fixture-shell", "{model_args}", "{engine_args}"),
                                    sandbox_args=(), full_access_args=(), token_regex=None)
        config = ringer.AppConfig(path=None, identity_default=None, state_dir=self.root / "state",
                                 dashboard_port_base=18787, hud_port=18788, hud_app_path=None,
                                 allow_full_access=False,
                                 eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
                                 engines={"local": engine},
                                 artifact=ringer.ArtifactConfig(enabled=False, out_template="", report_template="", index_out=self.root / "index"))
        assessment = ringer.assessment_draft_for_manifest(manifest, config, coordinator="test")
        assessment["strategy"] = "Exercise deterministic local shell reliability fixtures through the real gate."
        for row in assessment["tasks"].values():
            row.update(
                effort="medium",
                rationale="The fixture needs the selected local route to exercise lifecycle handling.",
                alternative_considered="Patching validation would not cover the runner boundary.",
                context_plan="Use only local shell fixture inputs and outputs.",
                verification="Assert original lifecycle, retry and file evidence.",
                escalation="Stop if route validation or local setup fails.",
                evidence="All worker commands and assessment rows are deterministic local fixtures.",
                uncertainty="No live model execution, quality or billing evidence is implied.",
            )
        return ringer.RingerRunner(replace(manifest, model_assessment=assessment), config, "test", dashboard_enabled=False)

    def rows(self, name="attempts.jsonl"):
        path = self.root / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_cancellation_preserves_active_attempt_evidence(self):
        runner = self.runner("echo cancellation-raw-output; echo $$ > worker.pid; sleep 30")
        async def exercise():
            job = asyncio.create_task(runner.run())
            marker = self.root / "work/one/worker.pid"
            for _ in range(200):
                if marker.exists():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(marker.exists())
            job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(job, 4)
        asyncio.run(exercise())
        rows = self.rows()
        self.assertEqual(1, len(rows), "an interrupted model attempt must remain in the journal")
        self.assertEqual("INTERRUPTED", rows[0]["verdict"])
        self.assertIn("cancellation-raw-output", runner.runtimes[0].log_path.read_text())

    def test_checker_timeout_never_retries_product_by_default(self):
        runner = self.runner()
        async def timeout(*args, **kwargs):
            return 1, True, "checker timed out"
        with patch.object(ringer.Verifier, "_run_check", timeout):
            self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(1, len(self.rows()), "checker timeout must not trigger a product rewrite")
        self.assertEqual("checker_timeout", self.rows()[0]["failure_class"])
        self.assertEqual("TIMEOUT", self.rows()[0]["verdict"])

class ExtendedReliabilityTests(ReliabilityTests):
    def test_event_ids_are_idempotent_and_do_not_rewrite_history(self):
        from ringer_reliability import LifecycleJournal, read_jsonl
        path = self.root / "events.jsonl"
        journal = LifecycleJournal(path)
        row = dict(run_id="r", job_id="j", task_key="t", attempt_index=1, event="attempt_started", status="running")
        event_id = journal.append(row)
        original = path.read_bytes()
        self.assertEqual(event_id, journal.append(dict(row, status="changed")))
        self.assertEqual(original, path.read_bytes())
        with path.open("ab") as stream:
            stream.write(b'{"torn":')
        journal.append(dict(row, event="attempt_finished"))
        rows, bad = read_jsonl(path)
        self.assertEqual(2, len(rows))
        self.assertEqual(1, bad)
        self.assertTrue(path.read_bytes().startswith(original))

    def test_validation_rejects_invalid_preflight_retry_and_timeout_fields(self):
        for field, values in {
            "check_timeout_s": [0, -1, float("inf"), float("nan"), True, "70"],
            "preflight_timeout_s": [0, float("inf")],
            "preflight_python_modules": [["json"], ["bad-name"]],
            "preflight_files": ["input.txt", [42]],
            "preflight_python": [None, ""],
            "preflight_command": [[], ""],
            "source_sha256": [[], {"x": "wrong"}],
            "retry_policy": ["always"],
            "retry_classes": [["quota"], ["permission"], ["interrupted"], ["typo"]],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    ringer.TaskSpec.from_obj(dict(key="t", spec="Work", check="true", **{field: value}))

    def test_configurable_timeout_over_sixty_is_forwarded_without_waiting(self):
        runner = self.runner(check_timeout_s=125)
        observed = []
        async def check(command, taskdir, **kwargs):
            observed.append(kwargs["timeout_s"])
            return 0, False, "executed fixture check"
        with patch.object(ringer.Verifier, "_run_check", staticmethod(check)):
            self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual([125], observed)
        state = runner.state_writer.snapshot()["tasks"][0]
        self.assertEqual(125, state["check_timeout_s"])
        self.assertEqual("BLOCKED", state["promotion_state"])
        self.assertEqual("PASS", state["task_contract_state"])
        self.assertEqual("PASS", state["product_state"])

    def test_product_retry_uses_actual_failure_then_harvests_matching_bytes(self):
        runner = self.runner(command="if test -f tried; then echo yes > out.txt; else touch tried; echo wrong > out.txt; fi",
                             check="grep -q yes out.txt || { echo 'actual output is wrong'; exit 1; }")
        self.assertEqual(0, asyncio.run(runner.run()))
        rows = self.rows()
        self.assertEqual(["FAIL", "PASS"], [r["verdict"] for r in rows])
        self.assertIn("actual output is wrong", rows[1]["spec"])
        self.assertEqual("product", rows[0]["failure_class"])
        self.assertEqual([1, 2], [r["attempt_index"] for r in rows])
        self.assertEqual("BLOCKED", rows[-1]["promotion_state"])
        from ringer_reliability import read_export_evidence
        self.assertTrue(all(r["matches"] for r in read_export_evidence(rows[-1]["harvested_files"])))
        events = self.rows("state/lifecycle.jsonl")
        self.assertEqual(["queued", "attempt_started", "attempt_finished", "attempt_started", "attempt_finished", "terminal"], [e["event"] for e in events])
        self.assertEqual(0, events[0]["attempt_index"])

    def test_preflight_blocks_missing_sources_modules_interpreter_checker_and_authority(self):
        cases = [
            ({"preflight_files": ["missing.txt"]}, "missing_dependency"),
            ({"preflight_python_modules": ["ringer_nonexistent_dependency"], "preflight_python": sys.executable}, "missing_dependency"),
            ({"preflight_python": str(self.root / "missing-python")}, "missing_dependency"),
            ({"source_sha256": {"missing.txt": "0" * 64}}, "missing_dependency"),
            ({"check": "python3 missing_checker.py"}, "missing_checker"),
            ({"full_access": True}, "permission"),
        ]
        for fields, failure_class in cases:
            with self.subTest(fields=fields):
                runner = self.runner(**fields)
                with patch.object(runner, "_run_worker", side_effect=AssertionError("worker must not run")):
                    self.assertEqual(1, asyncio.run(runner.run()))
                runtime = runner.runtimes[0]
                self.assertEqual("blocked", runtime.status)
                self.assertEqual(failure_class, runtime.failure_class)
                self.assertEqual(0, runtime.attempts)
                self.assertIsNone(runtime.last_check_returncode)
        self.assertEqual([], self.rows())

    def test_plausible_source_trap_blocks_changed_bytes(self):
        source = self.root / "source.txt"
        source.write_text("plausible substitute")
        runner = self.runner(source_sha256={str(source): ringer.hashlib.sha256(b"requested source").hexdigest()})
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertIn("SHA-256 mismatch", runner.runtimes[0].setup_error)
        self.assertEqual([], self.rows())

    def test_preflight_uses_taskdir_and_runs_once_before_product_retry(self):
        taskdir = self.root / "work/one"
        taskdir.mkdir(parents=True)
        (taskdir / "local_module.py").write_text("value = 5\n")
        (taskdir / "input.txt").write_text("original")
        from ringer_reliability import sha256_file
        runner = self.runner(check="echo product-failure; exit 1", preflight_files=["input.txt"],
                             preflight_python=sys.executable, preflight_python_modules=["local_module"],
                             source_sha256={"input.txt": sha256_file(taskdir / "input.txt")},
                             preflight_command="echo preflight >> preflight-count")
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual("preflight\n", (taskdir / "preflight-count").read_text())
        self.assertEqual(2, len(self.rows()))

    def test_preflight_does_not_require_check_created_outputs(self):
        runner = self.runner(command="echo worker", expect_files=["new-dir/export.txt"],
                             check="mkdir new-dir; echo contents > new-dir/export.txt")
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual("PASS", runner.runtimes[0].export_state)

    def test_preflight_output_permission_failure_blocks_without_attempt(self):
        runner = self.runner()
        from ringer_reliability import preflight_local
        taskdir = self.root / "probe"
        taskdir.mkdir()
        with patch("ringer_reliability.tempfile.mkstemp", side_effect=PermissionError("permission denied")):
            with self.assertRaises(PermissionError):
                preflight_local(runner.runtimes[0].task, taskdir)
        async def denied_probe(*args, **kwargs):
            return 0, False, json.dumps(["permission", "permission denied"])
        with patch.object(ringer, "run_bounded", denied_probe):
            self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual("permission", runner.runtimes[0].failure_class)
        self.assertEqual([], self.rows())

    def test_preflight_timeout_has_own_finite_bound(self):
        runner = self.runner(preflight_command="echo preflight-raw; sleep 30", preflight_timeout_s=.12)
        start = time.monotonic()
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertLess(time.monotonic()-start, 3)
        self.assertIn("preflight timed out", runner.runtimes[0].setup_error)
        self.assertEqual([], self.rows())
        self.assertIn("preflight-raw", runner.runtimes[0].log_path.read_text())

    def test_real_checker_timeout_keeps_stdout_and_does_not_retry(self):
        runner = self.runner(check="echo checker-output; sleep 30", check_timeout_s=.04)
        start = time.monotonic()
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertLess(time.monotonic()-start, 3)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual("checker_timeout", self.rows()[0]["failure_class"])
        self.assertIn("checker-output", self.rows()[0]["notes"])
        self.assertIn("checker-output", runner.runtimes[0].log_path.read_text())
        self.assertEqual("UNKNOWN", runner.runtimes[0].product_state)

    def test_worker_timeout_has_no_default_retry(self):
        runner = self.runner(command="echo partial; sleep 30", timeout_s=1)
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(1, len(self.rows()))
        self.assertEqual("worker_timeout", self.rows()[0]["failure_class"])

    def test_quota_permission_unknown_and_dependency_are_not_retried(self):
        for output, expected in (("quota exceeded", "quota"), ("permission denied", "permission"),
                                 ("service unavailable", "provider/runtime"), ("unexpected stop", "unknown"),
                                 ("ModuleNotFoundError: No module named missing", "missing_dependency")):
            with self.subTest(output=output):
                runner = self.runner(command="echo " + shlex.quote(output) + "; exit 1", check="true", expect_files=[], retry_policy="legacy" if expected in {"quota", "permission"} else "product")
                before = len(self.rows())
                self.assertEqual(1, asyncio.run(runner.run()))
                self.assertEqual(before+1, len(self.rows()))
                self.assertEqual(expected, self.rows()[-1]["failure_class"])

    def test_explicit_retry_class_is_bounded(self):
        runner = self.runner(command="echo unexpected; exit 1", check="true", expect_files=[], retry_classes=["unknown"], max_attempts=3)
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(3, len(self.rows()))
        self.assertTrue(all(r["failure_class"] == "unknown" for r in self.rows()))

    def test_failed_harvest_is_export_failure_and_keeps_product_evidence(self):
        runner = self.runner()
        with patch.object(ringer.shutil, "copy2", side_effect=OSError("export disk unavailable")):
            self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(1, len(self.rows()))
        row = self.rows()[0]
        self.assertEqual("export", row["failure_class"])
        self.assertEqual("PASS", row["product_state"])
        self.assertEqual("PASS", row["task_contract_state"])
        self.assertEqual("NEEDS_CHANGE", row["export_state"])
        self.assertIn("export disk unavailable", runner.runtimes[0].deliverable_notes[0])

    def test_external_ignored_export_is_preserved_and_tamper_is_visible(self):
        import subprocess
        from ringer_reliability import read_export_evidence
        source_dir = self.root / "export-repo"
        source_dir.mkdir()
        subprocess.run(["git", "init", "-q", str(source_dir)], check=True)
        (source_dir / ".gitignore").write_text("dist/\n")
        output = source_dir / "dist/result.txt"
        runner = self.runner(command="echo worker", expect_files=[str(output)],
                             check=f"mkdir -p {shlex.quote(str(output.parent))}; echo exported > {shlex.quote(str(output))}")
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual(0, subprocess.run(["git", "check-ignore", "-q", "dist/result.txt"], cwd=source_dir).returncode)
        files = runner.runtimes[0].deliverables
        self.assertTrue(read_export_evidence(files)[0]["matches"])
        self.assertEqual("exported\n", Path(files[0]["path"]).read_text())
        Path(files[0]["path"]).write_text("changed")
        self.assertFalse(read_export_evidence(files)[0]["matches"])
        self.assertEqual("exported\n", output.read_text())

    def test_reconciliation_keeps_93_omitted_states_and_partial_retry(self):
        from ringer_reliability import reconcile_outcomes
        old = dict(run_id="old", run_name="job", finished=True, tasks=[dict(key=f"t{i}", status="fail", attempts=0) for i in range(93)])
        new = dict(run_id="new", run_name="job", finished=True, tasks=[dict(key="retried", status="fail", attempts=2)])
        attempt = dict(run_id="new", task_key="retried", model="model-a", verdict="FAIL", failure_class="product", attempt_index=1)
        result = reconcile_outcomes([old, new], [attempt], [])
        self.assertEqual(94, result["totals"]["tasks"])
        self.assertEqual(93, result["totals"]["state_only"])
        self.assertEqual(94, result["totals"]["unknown_verification"])
        self.assertEqual(1, result["totals"]["partial_attempt_evidence"])
        self.assertEqual(1, result["totals"]["missing_attempt_rows"])
        self.assertEqual(0, result["totals"]["success_coverage"])
        self.assertEqual({"job"}, {t["job_id"] for t in result["tasks"]})
        self.assertEqual("BLOCKED", result["tasks"][-1]["promotion_state"])

    def test_false_pass_has_unknown_contract_and_blocked_promotion(self):
        runner = self.runner(command="echo claims-done", check="true", expect_files=[])
        self.assertEqual(0, asyncio.run(runner.run()))
        state = runner.state_writer.snapshot()["tasks"][0]
        self.assertEqual("PASS", state["product_state"])
        self.assertEqual("UNKNOWN", state["task_contract_state"])
        self.assertEqual("UNKNOWN", state["export_state"])
        self.assertEqual("BLOCKED", state["promotion_state"])

    def test_job_id_survives_manifest_roundtrips_and_unique_run_ids(self):
        obj = dict(run_name="round", job_id="human-job", workdir=str(self.root), tasks=[dict(key="t", spec="s", check="true")])
        path = self.root / "manifest.json"
        path.write_text(json.dumps(obj))
        self.assertEqual("human-job", ringer.Manifest.from_path(path).with_max_parallel(2).job_id)
        self.assertNotEqual(ringer.build_run_id("round"), ringer.build_run_id("round"))

    def test_cli_sigterm_during_checker_kills_tree_and_records_queued_task(self):
        import subprocess
        config = self.root / "config.toml"
        config.write_text(f'''state_dir = {json.dumps(str(self.root / "state"))}
[artifact]
enabled = false
[eval]
backend = "jsonl"
jsonl_path = {json.dumps(str(self.root / "attempts.jsonl"))}
[engines.local]
bin = "/bin/sh"
args_template = ["-c", "echo worker-raw; echo yes > out.txt", "fixture-shell", "{{model_args}}", "{{engine_args}}"]
sandbox_args = []
full_access_args = []
''')
        marker = self.root / "child.pid"
        check = f"echo checker-raw; sleep 30 & echo $! > {shlex.quote(str(marker))}; wait"
        manifest = self.root / "manifest.json"
        raw_manifest = dict(
            run_name="cancel-check",
            job_id="same-job",
            workdir=str(self.root / "work"),
            max_parallel=1,
            tasks=[
                dict(
                    key=k,
                    engine="local",
                    spec="Write output",
                    expect_files=["out.txt"],
                    check=check,
                    model="fixture-local-model",
                    billing_route="subscription",
                )
                for k in ["active", "queued"]
            ],
        )
        config_obj = ringer.AppConfig.load(config)
        parsed_manifest = ringer.Manifest.from_obj(raw_manifest)
        assessment = ringer.assessment_draft_for_manifest(
            parsed_manifest, config_obj, coordinator="test"
        )
        assessment["strategy"] = "Exercise local CLI signal cleanup through the real route gate."
        for row in assessment["tasks"].values():
            row.update(
                effort="medium",
                rationale="The fixture needs local shell workers to exercise signal cleanup.",
                alternative_considered="A parent-process validation patch would skip subprocess coverage.",
                context_plan="Use the two local shell tasks and their checker only.",
                verification="Assert original signal, lifecycle and child-process evidence.",
                escalation="Stop if the runner rejects the matching fixture route.",
                evidence="The command, manifest and assessment are deterministic local fixtures.",
                uncertainty="No live provider execution or entitlement is implied.",
            )
        raw_manifest["model_assessment"] = assessment
        manifest.write_text(json.dumps(raw_manifest))
        env = dict(os.environ, RINGER_HOME=str(self.root / "ringer-home"), RINGER_NO_SELF_UPDATE="1", RINGER_NO_CATALOG_REFRESH="1")
        proc = subprocess.Popen([sys.executable, str(Path(ringer.__file__)), "--config", str(config), "run", str(manifest), "--identity", "test", "--no-dashboard"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        child = None
        try:
            deadline = time.monotonic()+4
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(.01)
            self.assertTrue(marker.exists())
            child = int(marker.read_text())
            proc.send_signal(signal.SIGTERM)
            stdout, _ = proc.communicate(timeout=4)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate(timeout=2)
            if child:
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)
        self.assertEqual(130, proc.returncode, stdout)
        self.assertIn("worker-raw", stdout)
        self.assertEqual(1, len(self.rows()))
        self.assertEqual("INTERRUPTED", self.rows()[0]["verdict"])
        events = self.rows("state/lifecycle.jsonl")
        terminals = {e["task_key"]: e for e in events if e["event"] == "terminal"}
        self.assertEqual("interrupted", terminals["active"]["status"])
        self.assertEqual("not_started", terminals["queued"]["status"])
        self.assertEqual(0, terminals["queued"]["attempt_index"])
        self.assertEqual("same-job", terminals["active"]["job_id"])
        self.assertIn("checker-raw", (self.root / "work/active/worker.log").read_text())
        cli = subprocess.run([sys.executable, str(Path(ringer.__file__)), "--config", str(config), "outcomes", "--json", "--job-id", "same-job"], capture_output=True, text=True, env=env, timeout=3)
        self.assertEqual(0, cli.returncode, cli.stderr)
        report = json.loads(cli.stdout)
        self.assertEqual(2, report["totals"]["tasks"])
        self.assertEqual(1, report["totals"]["interruptions"])

    def test_checker_ignoring_term_is_killed_with_bounded_stdout_drain(self):
        marker = self.root / "checker.pid"
        command = f"trap '' TERM; echo $$ > {shlex.quote(str(marker))}; echo resistant-checker; while :; do sleep 1; done"
        start = time.monotonic()
        rc, timed_out, output = asyncio.run(ringer.Verifier._run_check(command, self.root, timeout_s=.08))
        self.assertTrue(timed_out)
        self.assertLess(time.monotonic()-start, 3)
        self.assertNotEqual(0, rc)
        self.assertIn("resistant-checker", output)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(marker.read_text()), 0)

    def test_false_pass_is_not_counted_as_successful_delivery(self):
        from ringer_reliability import read_outcomes
        runner = self.runner(command="echo claimed", check="true", expect_files=[])
        self.assertEqual(0, asyncio.run(runner.run()))
        report = read_outcomes(self.root / "state", self.root / "attempts.jsonl")
        self.assertEqual(1, report["totals"]["passed_checks"])
        self.assertEqual(0, report["totals"]["passed"])
        self.assertEqual(1, report["totals"]["unknown_contract"])
        self.assertEqual(0, report["totals"]["success_coverage"])
        self.assertEqual("executed_check", runner.runtimes[0].evidence_level())

    def test_lifecycle_uses_reported_model_identity(self):
        runner = self.runner()
        from dataclasses import replace
        runner.config.engines["local"] = replace(runner.config.engines["local"],
            args_template=("-c", "echo model: actual-model; echo yes > out.txt", "fixture-shell", "{model_args}", "{engine_args}"),
            model_report_regex=r"(?m)^model: (.+)$")
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual("actual-model", self.rows()[-1]["model"])
        self.assertEqual("actual-model", self.rows("state/lifecycle.jsonl")[-1]["model"])
        self.assertEqual("actual-model", runner.state_writer.snapshot()["tasks"][0]["model"])

    def test_source_hash_is_verified_after_preflight_commands(self):
        source = self.root / "pinned.txt"
        source.write_text("requested")
        runner = self.runner(source_sha256={str(source): ringer.hashlib.sha256(b"requested").hexdigest()},
                             preflight_command=f"echo substitute > {shlex.quote(str(source))}")
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual([], self.rows())
        self.assertEqual("blocked", runner.runtimes[0].status)
        self.assertIn("SHA-256 mismatch", runner.runtimes[0].setup_error)


    def test_outcomes_cli_read_back_reports_changed_export_and_unknown_old_hash(self):
        import subprocess
        from ringer_reliability import read_export_evidence
        runner = self.runner()
        self.assertEqual(0, asyncio.run(runner.run()))
        output = Path(runner.runtimes[0].deliverables[0]["path"])
        self.assertIsNone(read_export_evidence([dict(path=str(output), bytes=output.stat().st_size)])[0]["matches"])
        output.write_text("tampered")
        config = self.root / "outcomes-config.toml"
        config.write_text("")
        command = [sys.executable, str(Path(ringer.__file__)), "--config", str(config), "outcomes",
                   "--state-dir", str(self.root / "state"), "--log", str(self.root / "attempts.jsonl"),
                   "--read-back", "--json"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=3,
                                env=dict(os.environ, RINGER_NO_SELF_UPDATE="1"))
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(1, report["export_read_back_totals"]["mismatched_or_missing"])
        self.assertEqual("BLOCKED", report["tasks"][0]["promotion_state"])



if __name__ == "__main__":
    unittest.main()
