#!/usr/bin/env python3
"""Executed boundary tests for source-bound model-fit assessments."""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer
from ringer_routing import ASSESSMENT_SCHEMA, ModelAssessmentError


SPEC = "Write out.txt in the current directory with the exact word ready. Do not modify any other file."
CHECK = "test \"$(cat out.txt 2>/dev/null)\" = ready || { echo 'FAIL: expected out.txt containing ready'; exit 1; }"


class ModelAssessmentBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / "model-process-launched"
        self.worker = self.root / "spy_worker.py"
        self.worker.write_text(
            "import pathlib,sys\n"
            "pathlib.Path(sys.argv[1]).write_text('launched')\n"
            "pathlib.Path('out.txt').write_text('ready')\n",
            encoding="utf-8",
        )

    def engine(self, name: str = "codex", *, forwards: bool = True) -> ringer.EngineConfig:
        if name == "codex":
            template = (str(self.worker), str(self.marker), "{model_args}", "{engine_args}", "{spec}")
        else:
            template = (
                str(self.worker), str(self.marker), "run", "-m", "{model}",
                "{engine_args}", "{spec}",
            )
        if not forwards:
            template = (str(self.worker), str(self.marker), "{spec}")
        return ringer.EngineConfig(
            name=name,
            bin=sys.executable,
            args_template=template,
            full_access_args=(),
            sandbox_args=(),
            token_regex=None,
        )

    def config(self, engines=None, *, policy: dict | None = None, artifact: bool = False):
        policy_path = None
        if policy is not None:
            policy_path = self.root / "policy.json"
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
        return ringer.AppConfig(
            path=None,
            identity_default=None,
            state_dir=self.root / "state",
            dashboard_port_base=18787,
            hud_port=18788,
            hud_app_path=None,
            allow_full_access=False,
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
            engines=engines or {"codex": self.engine()},
            artifact=ringer.ArtifactConfig(
                enabled=artifact,
                out_template=str(self.root / "live-{run_id}.html"),
                report_template=str(self.root / "report-{run_id}.html"),
                index_out=self.root / "index.html",
            ),
            billing_policy_path=policy_path,
            require_model_assessment=True,
        )

    def task(self, key="one", **changes):
        row = {
            "key": key,
            "spec": SPEC,
            "check": CHECK,
            "engine": "codex",
            "model": "gpt-5.6-sol",
            "billing_route": "subscription",
            "expect_files": ["out.txt"],
            "verified": "the exact output is present",
        }
        row.update(changes)
        return row

    def raw_manifest(self, tasks=None):
        return {
            "run_name": "assessment-test",
            "workdir": str(self.root / "work"),
            "max_parallel": 2,
            "tasks": tasks or [self.task()],
        }

    def assessed(self, raw, config, *, effort="high", overrides=None):
        unassessed = ringer.Manifest.from_obj(raw)
        assessment = ringer.assessment_draft_for_manifest(
            unassessed, config, coordinator="coordinator"
        )
        assessment["strategy"] = "Use the smallest route that can handle each bounded task."
        for row in assessment["tasks"].values():
            row["effort"] = row["effort"] or effort
            row["rationale"] = "The task crosses implementation and verification boundaries."
            row["alternative_considered"] = "Deterministic tooling alone cannot author the requested result."
            row["context_plan"] = "Read only the supplied task specification and local files."
            row["verification"] = "Run the independent shell checker after the worker exits."
            row["escalation"] = "Stop on missing access, quota, route mismatch or failed evidence."
            row["evidence"] = "Routing is provisional and bound to this task content."
            row["uncertainty"] = "Objective model superiority is not established."
        for task_key, values in (overrides or {}).items():
            assessment["tasks"][task_key].update(values)
        result = dict(raw)
        result["model_assessment"] = assessment
        return ringer.Manifest.from_obj(result)

    def test_missing_partial_boolean_and_null_assessments_launch_nothing(self):
        config = self.config()
        cases = [
            None,
            {"schema": ASSESSMENT_SCHEMA},
            {"schema": ASSESSMENT_SCHEMA, "coordinator": True, "strategy": "x", "tasks": {}},
            {"schema": ASSESSMENT_SCHEMA, "coordinator": "x", "strategy": None, "tasks": {}},
        ]
        for assessment in cases:
            with self.subTest(assessment=assessment):
                raw = self.raw_manifest()
                if assessment is not None:
                    raw["model_assessment"] = assessment
                runner = ringer.RingerRunner(
                    ringer.Manifest.from_obj(raw), config, "test", dashboard_enabled=False
                )
                with self.assertRaises(ModelAssessmentError):
                    asyncio.run(runner.run())
                self.assertFalse(self.marker.exists())
                self.assertFalse((self.root / "state/cost-reservations.json").exists())

    def test_valid_mixed_routes_execute_with_explicit_per_task_effort(self):
        tasks = [
            self.task("sol", engine_args=["-c", "model_reasoning_effort=high"]),
            self.task(
                "terra", engine="opencode", model="openrouter/example/terra",
                billing_route="api", task_spend_allowance_gbp=1,
                engine_args=["--variant", "medium"],
            ),
        ]
        config = self.config({"codex": self.engine(), "opencode": self.engine("opencode")})
        manifest = self.assessed(self.raw_manifest(tasks), config, effort="medium")
        validated = ringer.validate_manifest_model_assessment(manifest, config)
        self.assertEqual("high", validated["sol"]["effort"])
        self.assertEqual("medium", validated["terra"]["effort"])
        self.assertEqual("standard", validated["terra"]["service_tier"])
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertIn("model_reasoning_effort=high", runner.runtimes[0].last_worker_command)
        self.assertIn("--variant", runner.runtimes[1].last_worker_command)

    def test_spec_or_checker_change_invalidates_old_binding(self):
        config = self.config()
        raw = self.raw_manifest()
        manifest = self.assessed(raw, config)
        assessed = manifest.model_assessment
        for field, value in (("spec", SPEC + " Extra."), ("check", CHECK + " && true")):
            changed = self.raw_manifest()
            changed["tasks"][0][field] = value
            changed["model_assessment"] = assessed
            with self.subTest(field=field), self.assertRaisesRegex(ModelAssessmentError, "stale"):
                ringer.validate_manifest_model_assessment(ringer.Manifest.from_obj(changed), config)

    def test_programmatic_task_replacement_invalidates_execution_binding(self):
        config = self.config()
        manifest = self.assessed(self.raw_manifest(), config)
        changed = dataclasses.replace(manifest.tasks[0], spec=SPEC + " Different runtime task.")
        replaced = dataclasses.replace(manifest, tasks=(changed,))
        with self.assertRaisesRegex(ModelAssessmentError, "stale"):
            ringer.validate_manifest_model_assessment(replaced, config)

    def test_execution_binding_covers_model_args_and_source_hash(self):
        config = self.config()
        manifest = self.assessed(self.raw_manifest(), config)
        task = manifest.tasks[0]
        for change in (
            {"check": CHECK + " && true"},
            {"model": "gpt-5.6-terra"},
            {"engine_args": ("--variant", "medium")},
            {"source_sha256": {"input.txt": "a" * 64}},
        ):
            with self.subTest(change=change), self.assertRaisesRegex(ModelAssessmentError, "stale"):
                replaced = dataclasses.replace(manifest, tasks=(dataclasses.replace(task, **change),))
                ringer.validate_manifest_model_assessment(replaced, config)

    def test_astra_rejects_minimal_but_other_models_retain_generic_efforts(self):
        config = self.config()
        astra = self.assessed(
            self.raw_manifest([self.task(model="gpt-6-astra")]), config, effort="minimal"
        )
        with self.assertRaisesRegex(ModelAssessmentError, "not supported"):
            ringer.validate_manifest_model_assessment(astra, config)
        minimal = self.assessed(self.raw_manifest(), config, effort="minimal")
        self.assertEqual("minimal", ringer.validate_manifest_model_assessment(minimal, config)["one"]["effort"])

    def test_mock_path_is_not_an_exemption_when_used_as_an_argument(self):
        bundled = str(ROOT / "engines" / "mock_worker.py")
        self.assertFalse(ringer.is_bundled_mock_command(["codex", bundled, "request"], "request"))
        self.assertTrue(ringer.is_bundled_mock_command([sys.executable, bundled, "request"], "request"))
        self.assertFalse(ringer.is_bundled_mock_command([sys.executable, "-c", bundled, "request"], "request"))
        self.assertFalse(ringer.is_bundled_mock_command([sys.executable, "-m", bundled, "request"], "request"))
        self.assertFalse(ringer.is_bundled_mock_command(["alternate-executable", bundled, "request"], "request"))
        self.assertFalse(ringer.is_bundled_mock_command(["/tmp/python3", bundled, "request"], "request"))
        self.assertFalse(ringer.is_bundled_mock_command([sys.executable, bundled, "--option", "request"], "request"))

        real_engine = ringer.EngineConfig(
            name="codex", bin="codex", args_template=(bundled, "{spec}"),
            full_access_args=(), sandbox_args=(), token_regex=None,
        )
        with self.assertRaises(ModelAssessmentError):
            ringer.validate_manifest_model_assessment(
                ringer.Manifest.from_obj(self.raw_manifest()), self.config({"codex": real_engine})
            )

    def test_renamed_python_and_stale_assessment_cannot_bypass_mock_exemption(self):
        bundled = str(ROOT / "engines" / "mock_worker.py")
        renamed = self.root / "python"
        renamed.write_text("not a Python interpreter", encoding="utf-8")
        self.assertFalse(ringer.is_bundled_mock_command([str(renamed), bundled, SPEC], SPEC))

        engine = ringer.EngineConfig(
            name="offline", bin=str(renamed), args_template=(bundled, "{spec}"),
            full_access_args=(), sandbox_args=(), token_regex=None,
        )
        task = self.task(engine="offline")
        task.pop("model")
        task.pop("billing_route")
        raw = self.raw_manifest([task])
        raw["model_assessment"] = {"schema": ASSESSMENT_SCHEMA, "tasks": {}}
        with self.assertRaisesRegex(ModelAssessmentError, "stale|required|does not prove"):
            ringer.validate_manifest_model_assessment(
                ringer.Manifest.from_obj(raw), self.config({"offline": engine})
            )

    def test_unknown_python_and_shell_wrappers_without_assessment_launch_nothing(self):
        api_spend_spy = self.root / "api-spend-spy"
        wrapper = self.root / "unknown_wrapper.py"
        wrapper.write_text(
            "import pathlib\npathlib.Path(%r).write_text('spent')\n" % str(api_spend_spy),
            encoding="utf-8",
        )
        shell = self.root / "unknown_wrapper.sh"
        shell.write_text("#!/bin/sh\nprintf spent > \"$1\"\n", encoding="utf-8")
        shell.chmod(0o755)
        engines = {
            "python-wrapper": ringer.EngineConfig(
                name="python-wrapper", bin=sys.executable,
                args_template=(str(wrapper), "{spec}"), full_access_args=(), sandbox_args=(), token_regex=None,
            ),
            "shell-wrapper": ringer.EngineConfig(
                name="shell-wrapper", bin="/bin/sh",
                args_template=(str(shell), str(api_spend_spy), "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None,
            ),
        }
        for engine_name in engines:
            with self.subTest(engine=engine_name):
                api_spend_spy.unlink(missing_ok=True)
                task = self.task(engine=engine_name)
                task.pop("model")
                task.pop("billing_route")
                raw = self.raw_manifest([task])
                runner = ringer.RingerRunner(
                    ringer.Manifest.from_obj(raw), self.config(engines), "test", dashboard_enabled=False
                )
                with self.assertRaises(ModelAssessmentError):
                    asyncio.run(runner.run())
                self.assertFalse(api_spend_spy.exists())

    def test_assessed_low_effort_is_explicit_not_inherited_from_global_high(self):
        config = self.config()
        manifest = self.assessed(self.raw_manifest(), config, effort="low")
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        self.assertEqual(0, asyncio.run(runner.run()))
        command = runner.runtimes[0].last_worker_command
        self.assertIn("model_reasoning_effort=low", command)
        self.assertNotIn("model_reasoning_effort=high", command)

    def test_task_spec_template_tokens_are_not_recursively_expanded(self):
        literal_model = "{" + "model}"
        spec = (
            "Preserve this literal token exactly: " + literal_model
            + ". Write out.txt in the task directory."
        )
        config = self.config()
        task = self.task(spec=spec)
        engine = ringer.EngineConfig(
            name="codex",
            bin=sys.executable,
            args_template=("run", "{taskdir}", "{model}", "prefix {spec}"),
            full_access_args=(),
            sandbox_args=(),
            token_regex=None,
        )
        command = ringer.build_worker_command(
            engine,
            taskdir=self.root / "taskdir",
            spec=spec,
            full_access=False,
            model="gpt-5.6-luna",
        )
        self.assertEqual(str(self.root / "taskdir"), command[2])
        self.assertEqual("gpt-5.6-luna", command[3])
        self.assertEqual("prefix " + spec, command[4])

        standalone = ringer.build_worker_command(
            config.engines["codex"],
            taskdir=self.root / "taskdir",
            spec=spec,
            full_access=False,
            model="gpt-5.6-luna",
        )
        self.assertEqual(spec, standalone[-1])
        self.assertIn("-m", standalone)
        self.assertIn("gpt-5.6-luna", standalone)

        manifest = self.assessed(self.raw_manifest([task]), config)
        validated = ringer.validate_manifest_model_assessment(manifest, config)
        self.assertEqual("high", validated["one"]["effort"])
        self.assertEqual("standard", validated["one"]["service_tier"])

    def test_model_effort_service_overrides_and_unforwarded_template_are_rejected(self):
        cases = [
            (["-m", "other", "-c", "model_reasoning_effort=high"], {}, "model settings"),
            (["-c", "model_reasoning_effort=low"], {"effort": "high"}, "effort records"),
            (["-c", "model_reasoning_effort=high", "-c", "service_tier=fast"], {"service_tier": "standard"}, "service_tier records"),
        ]
        for args, route_override, message in cases:
            raw = self.raw_manifest([self.task(engine_args=args)])
            config = self.config()
            manifest = self.assessed(
                raw, config, effort="high", overrides={"one": route_override}
            )
            with self.subTest(args=args), self.assertRaisesRegex(ModelAssessmentError, message):
                ringer.validate_manifest_model_assessment(manifest, config)
        config = self.config({"codex": self.engine(forwards=False)})
        raw = self.raw_manifest()
        manifest = self.assessed(raw, config)
        with self.assertRaisesRegex(ModelAssessmentError, "does not prove"):
            ringer.validate_manifest_model_assessment(manifest, config)

    def test_ultra_is_refused_and_fast_needs_separate_authority(self):
        config = self.config()
        ultra = self.assessed(self.raw_manifest(), config, overrides={"one": {"effort": "ultra"}})
        with self.assertRaisesRegex(ModelAssessmentError, "ultra is blocked"):
            ringer.validate_manifest_model_assessment(ultra, config)
        fast = self.assessed(self.raw_manifest(), config, overrides={"one": {"service_tier": "fast"}})
        with self.assertRaisesRegex(ModelAssessmentError, "fast service is not authorised"):
            ringer.validate_manifest_model_assessment(fast, config)

    def test_ask_and_real_demo_paths_do_not_bypass_gate(self):
        config = self.config()
        packet = ringer.ContextPacket("request", 7, 0, (), ())
        ask = ringer.one_request_manifest(
            packet=packet, workdir=self.root / "ask", engine="codex", timeout_s=10,
            reasoning_effort="low", model="gpt-5.6-luna", redact=False,
            billing_route="subscription",
        )
        with self.assertRaises(ModelAssessmentError):
            ringer.validate_manifest_model_assessment(ask, config)
        demo = ringer.Manifest.from_path(ringer.create_demo_manifest())
        with self.assertRaises(ModelAssessmentError):
            ringer.validate_manifest_model_assessment(demo, config)

    def test_legacy_manifest_and_historical_read_remain_available(self):
        manifest = ringer.Manifest.from_obj(self.raw_manifest())
        self.assertIsNone(manifest.model_assessment)
        log = self.root / "legacy.jsonl"
        log.write_text(json.dumps({"model": "gpt-5.5", "verdict": "PASS"}) + "\n")
        rows, skipped = ringer.read_model_log_rows(log)
        self.assertEqual(1, len(rows))
        self.assertEqual(0, skipped)

    def test_state_journal_and_artifact_preserve_and_escape_decision(self):
        config = self.config(artifact=True)
        manifest = self.assessed(
            self.raw_manifest(), config,
            overrides={"one": {"rationale": "Use <script>alert(1)</script> with independent checks."}},
        )
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        self.assertEqual(0, asyncio.run(runner.run()))
        state = json.loads(runner.state_writer.path.read_text())
        decision = state["tasks"][0]["model_assessment"]
        self.assertEqual(manifest.tasks[0].execution_binding, decision["binding"])
        journal = [json.loads(line) for line in (config.state_dir / "lifecycle.jsonl").read_text().splitlines()]
        self.assertTrue(all("model_assessment" in row for row in journal))
        html = runner.state_writer.report_path.read_text()
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_wrong_source_binding_paid_zero_cap_and_missing_access_stop_without_worker(self):
        config = self.config()
        wrong = self.assessed(self.raw_manifest(), config, overrides={"one": {"binding": "sha256:" + "0" * 64}})
        with self.assertRaisesRegex(ModelAssessmentError, "stale"):
            asyncio.run(ringer.RingerRunner(wrong, config, "test", dashboard_enabled=False).run())
        self.assertFalse(self.marker.exists())

        policy = {"monthly_api_cap_gbp": 0, "run_api_cap_gbp": 0, "automatic_paid_fallback": False}
        api_config = self.config({"opencode": self.engine("opencode")}, policy=policy)
        api_raw = self.raw_manifest([
            self.task(engine="opencode", model="openrouter/example/model", billing_route="api",
                      task_spend_allowance_gbp=1, engine_args=["--variant", "medium"])
        ])
        api_manifest = self.assessed(api_raw, api_config, effort="medium")
        api_runner = ringer.RingerRunner(api_manifest, api_config, "test", dashboard_enabled=False)
        self.assertEqual(1, asyncio.run(api_runner.run()))
        self.assertEqual(0, api_runner.runtimes[0].attempts)
        self.assertFalse(self.marker.exists())
        ledger = self.root / "state/cost-reservations.json"
        if ledger.exists():
            self.assertEqual({}, json.loads(ledger.read_text())["reservations"])

        blocked_raw = self.raw_manifest([self.task(full_access=True)])
        blocked = self.assessed(blocked_raw, config)
        blocked_runner = ringer.RingerRunner(blocked, config, "test", dashboard_enabled=False)
        self.assertEqual(1, asyncio.run(blocked_runner.run()))
        self.assertEqual("blocked", blocked_runner.runtimes[0].status)
        self.assertEqual(0, blocked_runner.runtimes[0].attempts)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
