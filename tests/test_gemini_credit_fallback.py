"""Tests for Gemini subscription probe and AI Studio credit fallback."""
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
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer
import ringer_billing
from ringer_capabilities import assess_explicit_route, normalize_gemini_api_model
from ringer_costs import (
    GEMINI_CREDIT_LEDGER_FILE,
    SpendLedger,
    inspect_usage_snapshot,
    validate_cost_policy,
)


def _load_module(name: str, rel_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _future_iso(hours: int = 5) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _quota_payload(five_h_remaining: float | None = 0.85, weekly_remaining: float | None = 0.90) -> dict:
    five_h = {
        "bucketId": "gemini-5h",
        "refreshWindowLabel": "5h",
        "resetTime": _future_iso(5),
    }
    if five_h_remaining is not None:
        five_h["remainingFraction"] = five_h_remaining
    weekly = {
        "bucketId": "gemini-weekly",
        "refreshWindowLabel": "weekly",
        "resetTime": _future_iso(120),
    }
    if weekly_remaining is not None:
        weekly["remainingFraction"] = weekly_remaining
    return {
        "quotaSummary": {
            "groups": [
                {"displayName": "Gemini Models", "buckets": [weekly, five_h]},
                {"displayName": "Claude and GPT models", "buckets": []},
            ]
        }
    }


class GeminiQuotaParserTests(unittest.TestCase):
    def test_healthy_snapshot_passes_inspection(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(0.75, 0.92))
        self.assertEqual("antigravity", snap["observed_auth_mode"])
        gem = snap["rateLimitsByLimitId"]["gemini"]
        self.assertAlmostEqual(25.0, gem["primary"]["usedPercent"])
        self.assertAlmostEqual(8.0, gem["secondary"]["usedPercent"])
        evidence = inspect_usage_snapshot(snap, limit_key="gemini")
        self.assertTrue(evidence.available)

    def test_proto3_omitted_zero_marks_5h_exhausted(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(None, 0.90))
        self.assertEqual(100.0, snap["rateLimitsByLimitId"]["gemini"]["primary"]["usedPercent"])
        evidence = inspect_usage_snapshot(snap, limit_key="gemini")
        self.assertFalse(evidence.available)
        self.assertIn("exhausted", evidence.reason)

    def test_weekly_limit_exhausted_blocks_even_when_5h_available(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(0.80, 0.0))
        self.assertEqual(100.0, snap["rateLimitsByLimitId"]["gemini"]["secondary"]["usedPercent"])
        evidence = inspect_usage_snapshot(snap, limit_key="gemini")
        self.assertFalse(evidence.available)
        self.assertIn("exhausted", evidence.reason)

    def test_missing_gemini_group_raises_account_evidence_error(self) -> None:
        with self.assertRaises(ringer_billing.AccountEvidenceError):
            ringer_billing.parse_gemini_quota_status({"quotaSummary": {"groups": []}})


class GeminiApiWorkerTests(unittest.TestCase):
    def test_model_normalization_and_rejection(self) -> None:
        worker = _load_module("gemini_api_worker_test", "engines/gemini_api_worker.py")
        cases = {
            "gemini-4-argon-medium": "google/gemini-4-argon",
            "gemini-4-neon-high": "google/gemini-4-neon",
            "gemini-4-carbon-low": "google/gemini-4-carbon",
            "gemini-3.8-flash-low": "google/gemini-3.8-flash",
            "google/gemini-4-argon": "google/gemini-4-argon",
            "pro": "google/gemini-4-argon",
            "flash": "google/gemini-3.8-flash",
        }
        for raw, expected in cases.items():
            self.assertEqual(expected, normalize_gemini_api_model(raw))
            self.assertEqual(expected, worker.normalize_gemini_api_model(raw))
        for bad in ("openrouter/google/gemini-4-argon", "claude-opus-5.5", "gpt-5.6-sol", "google/gemini-4-argon#high"):
            with self.assertRaises(ValueError):
                normalize_gemini_api_model(bad)
            with self.assertRaises(ValueError):
                worker.normalize_gemini_api_model(bad)

    def test_worker_loads_key_file_and_strips_openrouter(self) -> None:
        worker = _load_module("gemini_api_worker_env_test", "engines/gemini_api_worker.py")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key_path = root / ".runtime" / "gemini-api" / "gemini.key"
            key_path.parent.mkdir(parents=True)
            # Include a leading UTF-8 BOM to ensure GUI-editor saves work cleanly.
            key_path.write_text("\ufefftest-gemini-key-fixture\n", encoding="utf-8")
            cmd, env, cwd = worker.build_exec_args(
                ["--cwd", str(root), "-m", "gemini-4-argon-medium", "-p", "hello"],
                env={"OPENROUTER_API_KEY": "should-be-removed", "RINGER_OPENCODE_BIN": "/bin/echo"},
            )
            self.assertEqual(str(root), cwd)
            self.assertEqual(
                ["/bin/echo", "run", "--standalone", "--auto", "--format", "json", "-m", "google/gemini-4-argon", "hello"],
                cmd,
            )
            self.assertEqual("test-gemini-key-fixture", env["GEMINI_API_KEY"])
            self.assertEqual("test-gemini-key-fixture", env["GOOGLE_GENERATIVE_AI_API_KEY"])
            self.assertNotIn("OPENROUTER_API_KEY", env)

    def test_worker_missing_key_raises_permission_error(self) -> None:
        worker = _load_module("gemini_api_worker_missing_test", "engines/gemini_api_worker.py")
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(worker, "DEFAULT_KEY_PATHS", (".runtime/gemini-api/gemini.key",)):
                with self.assertRaises(PermissionError):
                    worker.build_exec_args(["--cwd", tmp, "-m", "google/gemini-4-argon", "-p", "hello"], env={})

    def test_assess_explicit_route_for_gemini_api(self) -> None:
        ok = assess_explicit_route(engine="gemini-api", model="google/gemini-4-argon", billing_route="api", billing_policy_loaded=True)
        self.assertEqual("allowed", ok.status)
        self.assertFalse(ok.spends_openrouter)
        bad_sub = assess_explicit_route(engine="gemini-api", model="google/gemini-4-argon", billing_route="subscription", billing_policy_loaded=True)
        self.assertEqual("blocked", bad_sub.status)
        bad_provider = assess_explicit_route(engine="gemini-api", model="openrouter/google/gemini-4-argon", billing_route="api", billing_policy_loaded=True)
        self.assertEqual("blocked", bad_provider.status)


class GeminiCreditFallbackRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.usage_dir = self.root / "usage"
        self.usage_dir.mkdir(parents=True, exist_ok=True)
        self.env_patch = patch.dict(os.environ, {"RINGER_USAGE_DIR": str(self.usage_dir)})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        gate = patch.object(ringer, "validate_manifest_model_assessment", return_value={})
        gate.start()
        self.addCleanup(gate.stop)

    def _policy(self, **overrides) -> Path:
        body = {
            "monthly_api_cap_gbp": "10",
            "run_api_cap_gbp": "1",
            "task_api_cap_gbp": "0.25",
            "automatic_paid_fallback": False,
            "subscription_auth_modes": ["chatgpt", "antigravity"],
            "gemini_credit_fallback": True,
            "monthly_gemini_api_cap_usd": "40",
            "run_gemini_api_cap_usd": "10",
            "task_gemini_api_cap_usd": "2",
            "fx": {"usd_to_gbp": "0.75", "dated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "source": "test"},
        }
        body.update(overrides)
        validate_cost_policy(body)
        path = self.root / "cost-policy.json"
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def _stub(self, name: str, script: str) -> Path:
        path = self.root / name
        path.write_text("#!/bin/sh\n" + script + "\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def _make_runner(
        self,
        *,
        policy_path: Path,
        sub_script: str,
        api_script: str,
        model: str = "gemini-4-argon-medium",
        max_attempts: int = 1,
    ) -> ringer.RingerRunner:
        sub_bin = self._stub("sub-gemini.sh", sub_script)
        api_bin = self._stub("api-gemini.sh", api_script)
        or_bin = self._stub("openrouter-never.sh", "echo OPENROUTER_CALLED > or.txt; exit 1")
        manifest = ringer.Manifest.from_obj(
            {
                "run_name": "gemini-credit-test",
                "workdir": str(self.root / "work"),
                "tasks": [
                    {
                        "key": "one",
                        "spec": "Write out.txt",
                        "engine": "gemini",
                        "model": model,
                        "billing_route": "subscription",
                        "check": "grep -q ok out.txt",
                        "expect_files": ["out.txt"],
                        "max_attempts": max_attempts,
                    }
                ],
            }
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
            engines={
                "gemini": ringer.EngineConfig(
                    name="gemini",
                    bin=str(sub_bin),
                    args_template=("-m", "{model}", "-p", "{spec}"),
                    sandbox_args=(),
                    full_access_args=(),
                    token_regex=None,
                    model_default="gemini-4-argon",
                ),
                "gemini-api": ringer.EngineConfig(
                    name="gemini-api",
                    bin=str(api_bin),
                    args_template=("-m", "{model}", "{spec}"),
                    sandbox_args=(),
                    full_access_args=(),
                    token_regex=None,
                    model_default="google/gemini-4-argon",
                ),
                "openrouter": ringer.EngineConfig(
                    name="openrouter",
                    bin=str(or_bin),
                    args_template=("-m", "{model}", "{spec}"),
                    sandbox_args=(),
                    full_access_args=(),
                    token_regex=None,
                    model_default="openrouter/z-ai/glm-5.3-flash",
                ),
            },
            artifact=ringer.ArtifactConfig(
                enabled=False,
                out_template="",
                report_template="",
                index_out=self.root / "index",
            ),
            billing_policy_path=policy_path,
        )
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        for rt in runner.runtimes:
            rt.model_assessment = {
                "engine": "gemini",
                "model": model,
                "effort": "medium",
                "billing_route": "subscription",
                "service_tier": "standard",
            }
        return runner

    def _api_pass_script(self) -> str:
        step = json.dumps(
            {
                "type": "step_finish",
                "sessionID": "ses-1",
                "part": {
                    "id": "step-1",
                    "type": "step-finish",
                    "model": "google/gemini-4-argon",
                    "tokens": {"input": 10000, "output": 2000, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
            }
        )
        return f"printf 'api-ok\\n' > out.txt\nprintf '%s\\n' '{step}'"

    def test_subscription_healthy_runs_without_touching_api_credits(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(0.80, 0.90))
        runner = self._make_runner(
            policy_path=self._policy(),
            sub_script="printf 'sub-ok\\n' > out.txt",
            api_script="echo SHOULD_NOT_RUN > api.txt; exit 1",
        )
        with patch.object(ringer_billing, "observe_gemini_account", new=AsyncMock(return_value=snap)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = asyncio.run(runner.run())
        self.assertEqual(0, rc)
        rt = runner.runtimes[0]
        self.assertEqual("gemini", rt.task.engine)
        self.assertIsNone(rt.quota_fallback)
        self.assertEqual("sub-ok\n", (self.root / "work" / "one" / "out.txt").read_text(encoding="utf-8"))
        self.assertFalse((self.root / "state" / GEMINI_CREDIT_LEDGER_FILE).exists())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())

    def test_pre_dispatch_5h_and_weekly_limits_fall_back_to_gemini_api_and_settle(self) -> None:
        for label, payload in (
            ("5h-exhausted", _quota_payload(0.0, 0.90)),
            ("weekly-exhausted", _quota_payload(0.90, None)),
        ):
            with self.subTest(case=label):
                self.setUp()
                snap = ringer_billing.parse_gemini_quota_status(payload)
                runner = self._make_runner(
                    policy_path=self._policy(),
                    sub_script="echo SUB_SHOULD_NOT_RUN > sub.txt; exit 1",
                    api_script=self._api_pass_script(),
                )
                err_buf = io.StringIO()
                with patch.object(ringer_billing, "observe_gemini_account", new=AsyncMock(return_value=snap)):
                    with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
                        rc = asyncio.run(runner.run())
                self.assertEqual(0, rc)
                rt = runner.runtimes[0]
                self.assertEqual("gemini-api", rt.task.engine)
                self.assertEqual("google/gemini-4-argon", rt.task.model)
                self.assertIsNotNone(rt.quota_fallback)
                self.assertEqual("pre_dispatch", rt.quota_fallback["trigger"])
                self.assertIn("GEMINI CREDIT FALLBACK", err_buf.getvalue())
                self.assertFalse((self.root / "work" / "one" / "sub.txt").exists())
                # Separate Gemini credit ledger settled; OpenRouter ledger untouched.
                self.assertFalse((self.root / "state" / "cost-reservations.json").exists())
                ledger_data = json.loads((self.root / "state" / GEMINI_CREDIT_LEDGER_FILE).read_text(encoding="utf-8"))
                rows = list(ledger_data["reservations"].values())
                self.assertEqual(1, len(rows))
                self.assertEqual("settled", rows[0]["state"])
                # 10k * $4/1M + 2k * $20/1M = $0.08 USD * 0.75 FX = 0.06 GBP
                self.assertEqual(Decimal("0.06"), Decimal(rows[0]["actual_gbp"]))

    def test_mid_run_quota_failure_falls_back_to_gemini_api(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(0.05, 0.50))
        runner = self._make_runner(
            policy_path=self._policy(),
            sub_script="echo 'ERROR: ResourceExhausted: 5h quota exceeded'; exit 1",
            api_script=self._api_pass_script(),
            max_attempts=1,
        )
        err_buf = io.StringIO()
        with patch.object(ringer_billing, "observe_gemini_account", new=AsyncMock(return_value=snap)):
            with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
                rc = asyncio.run(runner.run())
        self.assertEqual(0, rc)
        rt = runner.runtimes[0]
        self.assertEqual("gemini-api", rt.task.engine)
        self.assertEqual(2, rt.attempts)
        self.assertEqual("mid_run", rt.quota_fallback["trigger"])
        self.assertIn("GEMINI CREDIT FALLBACK (mid_run)", err_buf.getvalue())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())
        self.assertTrue((self.root / "state" / GEMINI_CREDIT_LEDGER_FILE).exists())

    def test_disabled_fallback_stays_blocked(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(0.0, 0.90))
        runner = self._make_runner(
            policy_path=self._policy(gemini_credit_fallback=False),
            sub_script="printf 'sub-ok\\n' > out.txt",
            api_script=self._api_pass_script(),
        )
        with patch.object(ringer_billing, "observe_gemini_account", new=AsyncMock(return_value=snap)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = asyncio.run(runner.run())
        self.assertEqual(1, rc)
        rt = runner.runtimes[0]
        self.assertEqual("blocked", rt.status)
        self.assertEqual("gemini", rt.task.engine)
        self.assertIsNone(rt.quota_fallback)
        self.assertFalse((self.root / "state" / GEMINI_CREDIT_LEDGER_FILE).exists())

    def test_exhausted_gemini_credit_cap_blocks_without_openrouter(self) -> None:
        snap = ringer_billing.parse_gemini_quota_status(_quota_payload(0.0, 0.90))
        runner = self._make_runner(
            policy_path=self._policy(monthly_gemini_api_cap_usd="1", task_gemini_api_cap_usd="2"),
            sub_script="printf 'sub-ok\\n' > out.txt",
            api_script=self._api_pass_script(),
        )
        with patch.object(ringer_billing, "observe_gemini_account", new=AsyncMock(return_value=snap)):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                rc = asyncio.run(runner.run())
        self.assertEqual(1, rc)
        rt = runner.runtimes[0]
        self.assertEqual("blocked", rt.status)
        self.assertEqual("gemini-api", rt.task.engine)
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())
        self.assertFalse((self.root / "work" / "one" / "or.txt").exists())


if __name__ == "__main__":
    unittest.main()
