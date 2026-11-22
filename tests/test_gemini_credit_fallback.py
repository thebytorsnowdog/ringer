"""Exercise the paid fallback with shell workers and synthetic quota evidence."""
from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, patch

import ringer
import ringer_billing
from engines import gemini_api_worker as worker
from ringer_capabilities import assess_explicit_route, normalize_gemini_api_model
from ringer_costs import GEMINI_CREDIT_LEDGER_FILE, validate_cost_policy
from tests.test_antigravity_quota_probe import quota_buckets, quota_payload


class GeminiApiWorkerTests(unittest.TestCase):
    def test_model_normalization_and_routing(self):
        for raw, canonical in (
            ("gemini-4-argon-medium", "google/gemini-4-argon"),
            ("gemini-4-neon-high", "google/gemini-4-neon"),
            ("gemini-4-carbon-low", "google/gemini-4-carbon"),
            ("flash", "google/gemini-3.8-flash"),
        ):
            self.assertEqual(canonical, normalize_gemini_api_model(raw))
            decision = assess_explicit_route(engine="gemini-api", model=raw,
                                             billing_route="api", billing_policy_loaded=True)
            self.assertEqual(("allowed", canonical), (decision.status, decision.model))
            self.assertFalse(decision.spends_openrouter)
        for model, route, loaded in (
            ("google/gemini-4-argon", "subscription", True),
            ("google/gemini-4-argon", "api", False),
            ("openrouter/google/gemini-4-argon", "api", True),
            ("google/gemini-4-argon#high", "api", True),
            ("gpt-5.6-sol", "api", True),
        ):
            decision = assess_explicit_route(engine="gemini-api", model=model,
                                             billing_route=route, billing_policy_loaded=loaded)
            self.assertEqual("blocked", decision.status)

    def test_worker_builds_google_only_command_from_mocked_key_file(self):
        with (
            patch.object(worker, "DEFAULT_KEY_PATHS", (".runtime/gemini-api/gemini.key",)),
            patch.object(Path, "is_file", return_value=True),
            patch.object(Path, "read_text", return_value="\ufeffsynthetic-fixture\n") as read,
            patch.object(worker, "resolve_opencode", return_value="/bin/echo"),
        ):
            cmd, env, cwd = worker.build_exec_args(
                ["--cwd", "/fixture", "-m", "pro", "-p", "-prompt-starting-with-dash"],
                env={"OPENROUTER_API_KEY": "synthetic-openrouter"},
            )
        read.assert_called_once_with(encoding="utf-8-sig")
        self.assertEqual("/fixture", cwd)
        self.assertEqual(
            ["/bin/echo", "run", "--standalone", "--auto", "--format", "json", "-m",
             "google/gemini-4-argon", "--", "-prompt-starting-with-dash"], cmd,
        )
        self.assertEqual("synthetic-fixture", env["GEMINI_API_KEY"])
        self.assertEqual("synthetic-fixture", env["GOOGLE_GENERATIVE_AI_API_KEY"])
        self.assertNotIn("OPENROUTER_API_KEY", env)

    def test_missing_key_and_routing_overrides_block(self):
        with patch.object(worker, "DEFAULT_KEY_PATHS", ()):
            with self.assertRaises(PermissionError):
                worker.build_exec_args(["-m", "pro", "prompt"], env={})
        for flags in (["--auth", "synthetic-value"], ["--model=flash"], ["--provider=other"]):
            with self.subTest(flags=flags), self.assertRaises(ValueError):
                ringer_billing.enforce_gemini_api_command(
                    ["worker", "-m", "pro", *flags, "prompt"], "pro", "prompt",
                    taskdir=Path("/fixture"), full_access=False,
                )

    def test_restricted_adapter_requires_a_wrapper(self):
        command = ["gemini_api_worker.py", "-m", "pro", "prompt"]
        with self.assertRaisesRegex(ValueError, "requires --sandbox-wrapper"):
            ringer_billing.enforce_gemini_api_command(
                command, "pro", "prompt", taskdir=Path("/fixture"), full_access=False,
            )
        ringer_billing.enforce_gemini_api_command(
            command[:-1] + ["--sandbox-wrapper", "/fixture/wrapper", "prompt"],
            "pro", "prompt", taskdir=Path("/fixture"), full_access=False,
        )


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

    def _policy(self, **overrides) -> Path:
        body = {
            "monthly_api_cap_gbp": "10",
            "run_api_cap_gbp": "1",
            "task_api_cap_gbp": "0.25",
            "automatic_paid_fallback": True,
            "subscription_auth_modes": ["chatgpt", "antigravity"],
            "gemini_credit_fallback": True,
            "monthly_gemini_api_cap_usd": "40",
            "run_gemini_api_cap_usd": "10",
            "task_gemini_api_cap_usd": "2",
            "fx": {"usd_to_gbp": "0.75", "dated": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "source": "test"},
        }
        body.update(overrides)
        body = {key: value for key, value in body.items() if value is not None}
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
        allowance: float | None = None,
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
                        "max_attempts": 1,
                        "task_spend_allowance_gbp": allowance,
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
        return ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)

    def _api_pass_script(self) -> str:
        step = json.dumps(
            {
                "type": "step_finish",
                "sessionID": "ses-1",
                "part": {
                    "id": "step-1",
                    "cost": 0.01,
                    "type": "step-finish",
                    "model": "google/gemini-4-argon",
                    "tokens": {"input": 10000, "output": 2000, "reasoning": 0, "cache": {"read": 0, "write": 0}},
                },
            }
        )
        return f"printf 'api-ok\\n' > out.txt\nprintf '%s\\n' '{step}'"

    def _run(self, *, payload=None, sub_script="exit 1", api_script=None, allowance=None, **policy):
        runner = self._make_runner(
            policy_path=self._policy(**policy), sub_script=sub_script,
            api_script=self._api_pass_script() if api_script is None else api_script,
            allowance=allowance,
        )

        async def observe(**_kwargs):
            return ringer_billing.parse_gemini_quota_status(payload)

        assessment = {"engine": "gemini", "model": runner.runtimes[0].task.model,
                      "effort": "medium", "billing_route": "subscription", "service_tier": "standard"}
        stderr = io.StringIO()
        with (
            patch.object(ringer, "validate_manifest_model_assessment", return_value={"one": assessment}),
            patch.object(ringer_billing, "observe_gemini_account", new=AsyncMock(side_effect=observe)) as probe,
            redirect_stdout(io.StringIO()),
            redirect_stderr(stderr),
        ):
            code = asyncio.run(runner.run())
        runtime = runner.runtimes[0]
        self.assertFalse((runtime.taskdir / "or.txt").exists())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())
        return code, runtime, stderr.getvalue(), probe.call_count

    def _ledger_rows(self):
        path = self.root / "state" / GEMINI_CREDIT_LEDGER_FILE
        if not path.exists():
            return []
        return list(json.loads(path.read_text())["reservations"].values())

    def _assert_settled(self, result, *, trigger="pre_dispatch", attempts=1):
        code, runtime, stderr, _ = result
        self.assertEqual(0, code)
        self.assertEqual(("gemini-api", "google/gemini-4-argon"), (runtime.task.engine, runtime.task.model))
        self.assertEqual(attempts, runtime.attempts)
        self.assertEqual(trigger, runtime.quota_fallback["trigger"])
        self.assertEqual("gemini", runtime.model_assessment["engine"])
        self.assertIn(f"GEMINI CREDIT FALLBACK ({trigger})", stderr)
        self.assertEqual("dated_estimate", runtime.billing["cost_coverage"])
        self.assertIsNone(runtime.billing["actual_cost_usd"])
        row, = self._ledger_rows()
        self.assertEqual("settled", row["state"])
        self.assertTrue(row["started"])
        self.assertEqual(Decimal("1.50"), Decimal(row["amount_gbp"]))
        self.assertEqual(Decimal("0.06"), Decimal(row["actual_gbp"]))
        self.assertIn("dated pricing estimate", row["settlement_source"])

    def test_five_hour_exhaustion_falls_back_and_settles(self):
        self._assert_settled(self._run(payload=quota_payload(primary=0)))

    def test_weekly_exhaustion_falls_back_and_settles(self):
        self._assert_settled(self._run(payload=quota_payload(weekly=0)))

    def test_cached_quota_cap_falls_back_before_probe(self):
        (self.usage_dir / "antigravity.json").write_text(json.dumps({"limits": [{"percent": 1}]}))
        result = self._run()
        self.assertEqual(0, result[3])
        self._assert_settled(result)

    def test_mid_run_quota_gets_one_fallback_attempt(self):
        result = self._run(payload=quota_payload(), sub_script="echo 'ERROR: ResourceExhausted: 5h quota exceeded'; exit 1")
        self._assert_settled(result, trigger="mid_run", attempts=2)

    def test_healthy_subscription_never_reserves_credits(self):
        code, runtime, _, _ = self._run(payload=quota_payload(primary=1e-20), sub_script="echo sub-ok > out.txt")
        self.assertEqual(0, code)
        self.assertEqual("gemini", runtime.task.engine)
        self.assertIsNone(runtime.quota_fallback)
        self.assertEqual([], self._ledger_rows())

    def test_both_flags_are_required_and_absent_keys_are_off(self):
        for policy in (
            {"gemini_credit_fallback": False},
            {"automatic_paid_fallback": False},
            dict.fromkeys(("gemini_credit_fallback", "automatic_paid_fallback",
                          "monthly_gemini_api_cap_usd", "run_gemini_api_cap_usd", "task_gemini_api_cap_usd")),
        ):
            with self.subTest(policy=policy):
                code, runtime, _, _ = self._run(payload=quota_payload(primary=0), **policy)
                self.assertEqual((1, "blocked", "gemini"), (code, runtime.status, runtime.task.engine))
                self.assertIsNone(runtime.quota_fallback)
                self.assertEqual([], self._ledger_rows())

    def test_missing_or_expired_weekly_evidence_never_triggers_fallback(self):
        missing = quota_payload(primary=0)
        quota_buckets(missing).pop()
        expired = quota_payload(primary=0)
        quota_buckets(expired)[1]["resetTime"] = "2020-01-01T00:00:00Z"
        for payload in (missing, expired):
            with self.subTest(payload=payload):
                code, runtime, _, _ = self._run(payload=payload)
                self.assertEqual((1, "quota", "gemini"), (code, runtime.failure_class, runtime.task.engine))
                self.assertIsNone(runtime.quota_fallback)
                self.assertEqual([], self._ledger_rows())

    def test_caps_block_paid_dispatch(self):
        for cap, amount in (("monthly", "1"), ("run", "1"), ("task", "0")):
            with self.subTest(cap=cap):
                code, runtime, _, _ = self._run(payload=quota_payload(primary=0), **{f"{cap}_gemini_api_cap_usd": amount})
                self.assertEqual((1, "blocked", 0), (code, runtime.status, runtime.attempts))
                self.assertEqual([], self._ledger_rows())

    def test_paid_quota_failure_holds_reservation_and_never_retries(self):
        code, runtime, _, _ = self._run(
            payload=quota_payload(primary=0),
            api_script=self._api_pass_script() + "\necho 'ERROR: ResourceExhausted: quota exceeded'; exit 1",
        )
        self.assertEqual((1, "blocked", 1), (code, runtime.status, runtime.attempts))
        self.assertEqual("unresolved", runtime.billing["reservation_status"])
        row, = self._ledger_rows()
        self.assertTrue(row["started"])
        self.assertEqual("reserved", row["state"])
        self.assertIsNone(row["actual_gbp"])

    def test_explicit_allowance_cannot_override_gemini_task_cap(self):
        code, runtime, _, _ = self._run(payload=quota_payload(primary=0), allowance=1.51)
        self.assertEqual((1, "blocked", 0), (code, runtime.status, runtime.attempts))
        self.assertIn("exceeds the Gemini AI Studio task cap", runtime.billing["billing_reason"])
        self.assertEqual([], self._ledger_rows())
