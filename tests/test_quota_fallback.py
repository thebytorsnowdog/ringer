"""Tests for subscription-first routing, pre-dispatch and mid-run usage-cap fallback to OpenRouter, and worker wrappers."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ringer
from ringer_reliability import classify_failure


def _load_worker_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class QuotaFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.usage_dir = self.root / "usage"
        self.usage_dir.mkdir(parents=True, exist_ok=True)
        self.notify_log = self.root / "notify.log"
        self.notify_script = self.root / "notify_capture.sh"
        self.notify_script.write_text(
            f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {self.notify_log}\n",
            encoding="utf-8",
        )
        self.notify_script.chmod(0o755)
        self.env_patch = patch.dict(
            os.environ,
            {
                "RINGER_USAGE_DIR": str(self.usage_dir),
                "RINGER_NOTIFY_CMD": str(self.notify_script),
            },
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        gate = patch.object(ringer, "validate_manifest_model_assessment", return_value={})
        gate.start()
        self.addCleanup(gate.stop)

    def _make_runner(
        self,
        *,
        sub_engine_name: str = "gemini",
        sub_model: str = "gemini-3.8-flash-low",
        sub_cmd: str = "echo 'ERROR: ResourceExhausted: quota exceeded for gemini'; exit 1",
        or_cmd: str = "printf 'fallback-ok\\n' > out.txt; printf 'tokens used\\n150\\n'",
        effort: str = "medium",
        max_attempts: int = 1,
    ) -> ringer.RingerRunner:
        task = {
            "key": "task-one",
            "spec": "Write out.txt",
            "engine": sub_engine_name,
            "model": sub_model,
            "billing_route": "subscription",
            "check": "grep -q fallback-ok out.txt",
            "expect_files": ["out.txt"],
            "max_attempts": max_attempts,
        }
        manifest = ringer.Manifest.from_obj(
            {
                "run_name": "quota-fallback-test",
                "workdir": str(self.root / "work"),
                "tasks": [task],
            }
        )
        sub_engine = ringer.EngineConfig(
            name=sub_engine_name,
            bin="/bin/sh",
            args_template=("-c", sub_cmd),
            sandbox_args=(),
            full_access_args=(),
            token_regex=r"tokens used\n([\d,]+)",
            model_default=sub_model,
            effort_flag="--effort",
        )
        or_engine = ringer.EngineConfig(
            name="openrouter",
            bin="/bin/sh",
            args_template=("-c", or_cmd),
            sandbox_args=(),
            full_access_args=(),
            token_regex=r"tokens used\n([\d,]+)",
            model_default="openrouter/z-ai/glm-5.3",
            effort_flag="--effort",
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
            engines={sub_engine_name: sub_engine, "openrouter": or_engine},
            artifact=ringer.ArtifactConfig(
                enabled=False,
                out_template="",
                report_template="",
                index_out=self.root / "index",
            ),
        )
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        for rt in runner.runtimes:
            rt.model_assessment = {
                "engine": sub_engine_name,
                "model": sub_model,
                "effort": effort,
                "billing_route": "subscription",
                "service_tier": "standard",
            }
        return runner

    def _attempts(self) -> list[dict]:
        path = self.root / "attempts.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _lifecycle_events(self) -> list[dict]:
        path = self.root / "state" / "lifecycle.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def test_mid_run_quota_failure_reverts_to_openrouter_and_notifies_user(self):
        for sub_engine, sub_model, effort, expected_or_model in [
            ("gemini", "gemini-3.8-flash-low", "low", "openrouter/z-ai/glm-5.3-flash"),
            ("claude", "opus", "high", "openrouter/z-ai/glm-5.3"),
            ("codex", "gpt-5.4", "medium", "openrouter/z-ai/glm-5.3"),
            ("grok", "grok-4.7", "medium", "openrouter/z-ai/glm-5.3"),
        ]:
            with self.subTest(engine=sub_engine):
                self.setUp()
                with patch.object(
                    ringer,
                    "validate_manifest_model_assessment",
                    return_value={
                        "task-one": {
                            "engine": sub_engine,
                            "model": sub_model,
                            "effort": effort,
                            "billing_route": "subscription",
                            "service_tier": "standard",
                        }
                    },
                ):
                    runner = self._make_runner(
                        sub_engine_name=sub_engine,
                        sub_model=sub_model,
                        effort=effort,
                        max_attempts=1,
                    )
                    out_buf = io.StringIO()
                    err_buf = io.StringIO()
                    with redirect_stdout(out_buf), redirect_stderr(err_buf):
                        rc = asyncio.run(runner.run())
                self.assertEqual(0, rc)
                attempts = self._attempts()
                self.assertEqual(2, len(attempts))
                self.assertEqual(sub_engine, attempts[0]["worker_engine"])
                self.assertEqual("quota", attempts[0]["failure_class"])
                self.assertEqual("openrouter", attempts[1]["worker_engine"])
                self.assertEqual(expected_or_model, attempts[1]["model"])
                self.assertEqual("PASS", attempts[1]["verdict"])

                # Lifecycle event emitted
                q_events = [e for e in self._lifecycle_events() if e.get("event") == "quota_fallback"]
                self.assertEqual(1, len(q_events))
                qf = q_events[0]["quota_fallback"]
                self.assertEqual(sub_engine, qf["original_engine"])
                self.assertEqual("openrouter", qf["fallback_engine"])
                self.assertEqual(expected_or_model, qf["fallback_model"])
                self.assertEqual("mid_run", qf["trigger"])

                # State file contains quota_fallback
                state_obj = json.loads(runner.state_writer.path.read_text(encoding="utf-8"))
                task_state = state_obj["tasks"][0]
                self.assertIsNotNone(task_state.get("quota_fallback"))
                self.assertEqual("openrouter", task_state["quota_fallback"]["fallback_engine"])

                # Stderr banner + summary output + desktop notification
                self.assertIn("USAGE CAP FALLBACK", err_buf.getvalue())
                self.assertIn("usage cap fallbacks (reverted to OpenRouter):", out_buf.getvalue())
                self.assertTrue(self.notify_log.exists())
                self.assertIn("OpenRouter", self.notify_log.read_text(encoding="utf-8"))

    def test_pre_dispatch_usage_cap_reverts_immediately_without_wasting_attempt(self):
        usage_file = self.usage_dir / "antigravity.json"
        usage_file.write_text(
            json.dumps(
                {
                    "agent": "antigravity",
                    "status": "ok",
                    "updatedAt": "2026-10-17T22:00:00Z",
                    "limits": [
                        {
                            "label": "Gemini pool · 5h",
                            "percent": 1.0,
                            "resetsAt": "2099-01-01T00:00:00Z",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with patch.object(
            ringer,
            "validate_manifest_model_assessment",
            return_value={
                "task-one": {
                    "engine": "gemini",
                    "model": "gemini-4-argon-high",
                    "effort": "high",
                    "billing_route": "subscription",
                    "service_tier": "standard",
                }
            },
        ):
            runner = self._make_runner(
                sub_engine_name="gemini",
                sub_model="gemini-4-argon-high",
                sub_cmd="echo 'should-not-run'; exit 1",
                effort="high",
            )
            out_buf = io.StringIO()
            err_buf = io.StringIO()
            with redirect_stdout(out_buf), redirect_stderr(err_buf):
                rc = asyncio.run(runner.run())
        self.assertEqual(0, rc)
        attempts = self._attempts()
        # Only 1 attempt ran, directly on openrouter!
        self.assertEqual(1, len(attempts))
        self.assertEqual("openrouter", attempts[0]["worker_engine"])
        self.assertEqual("openrouter/z-ai/glm-5.3", attempts[0]["model"])
        self.assertEqual("PASS", attempts[0]["verdict"])
        q_events = [e for e in self._lifecycle_events() if e.get("event") == "quota_fallback"]
        self.assertEqual(1, len(q_events))
        self.assertEqual("pre_dispatch", q_events[0]["quota_fallback"]["trigger"])
        self.assertIn("USAGE CAP FALLBACK", err_buf.getvalue())

    def test_expired_usage_cap_window_does_not_trigger_pre_dispatch_fallback(self):
        usage_file = self.usage_dir / "antigravity.json"
        usage_file.write_text(
            json.dumps(
                {
                    "agent": "antigravity",
                    "status": "ok",
                    "updatedAt": "2026-10-17T22:00:00Z",
                    "limits": [
                        {
                            "label": "Gemini pool · 5h",
                            "percent": 1.0,
                            "resetsAt": "2020-01-01T00:00:00Z",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        runner = self._make_runner(
            sub_engine_name="gemini",
            sub_model="gemini-3.8-flash-low",
            sub_cmd="printf 'fallback-ok\\n' > out.txt; printf 'tokens used\\n99\\n'",
        )
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            rc = asyncio.run(runner.run())
        self.assertEqual(0, rc)
        attempts = self._attempts()
        self.assertEqual(1, len(attempts))
        self.assertEqual("gemini", attempts[0]["worker_engine"])
        q_events = [e for e in self._lifecycle_events() if e.get("event") == "quota_fallback"]
        self.assertEqual(0, len(q_events))

    def test_gemini_and_openrouter_worker_wrappers_build_expected_commands(self):
        gemini_mod = _load_worker_module("gemini_worker", "engines/gemini_worker.py")
        or_mod = _load_worker_module("openrouter_worker", "engines/openrouter_worker.py")

        self.assertEqual("gemini-3.8-flash-low", gemini_mod.normalize_model("gemini-3.8-flash", "low"))
        self.assertEqual("gemini-4-argon-high", gemini_mod.normalize_model("gemini-4-argon", "high"))
        self.assertEqual("gemini-3.8-flash-low", gemini_mod.normalize_model("gemini-3.8-flash-low", "medium"))

        cmd, _env, target_cwd = gemini_mod.build_exec_args(
            ["--cwd", str(self.root), "-m", "gemini-3.8-flash", "--effort", "low", "-p", "hello"]
        )
        self.assertIn("gemini-3.8-flash-low", cmd)
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertEqual(str(self.root), target_cwd)

        self.assertEqual("z-ai/glm-5.3-flash", or_mod.strip_openrouter_prefix("openrouter/z-ai/glm-5.3-flash"))
        self.assertEqual("z-ai/glm-5.3", or_mod.strip_openrouter_prefix("z-ai/glm-5.3"))

        raw = "ResourceExhausted: You have exhausted your capacity on this model."
        self.assertEqual(
            "quota",
            classify_failure(
                ringer.WorkerResult(1, False, None, raw_output=raw),
                ringer.VerifyResult(False, 1, False, "check failed"),
                worker_output=raw,
            ),
        )


if __name__ == "__main__":
    unittest.main()
