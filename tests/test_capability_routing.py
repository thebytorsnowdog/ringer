"""Behaviour matrix for harness capability, explicit routes, and wrapper preflight.

No live model calls, no credential reads, and no OpenRouter traffic.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ringer
from ringer_capabilities import (
    DEFAULT_AGY_BIN,
    DEFAULT_CLAUDE_BIN,
    assess_explicit_route,
    choose_unspecified_route,
    inventory_engines,
    missing_runtime_dependencies,
    subscription_probe,
)
from ringer_costs import billing_preflight
from ringer_reliability import select_openrouter_fallback_model

WRAPPER = ROOT / "engines" / "opencode-sandboxed.sh"
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)


def _load_worker(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _snapshot(used: int = 10) -> dict:
    return {
        "observed_at": NOW.isoformat(),
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {
                    "usedPercent": used,
                    "windowDurationMins": 300,
                    "resetsAt": int((NOW + timedelta(days=1)).timestamp()),
                },
                "secondary": None,
            }
        },
    }


class CapabilityMatrixTests(unittest.TestCase):
    def test_worker_default_bins_match_the_adapters(self) -> None:
        gemini = _load_worker("gemini_worker_caps", "engines/gemini_worker.py")
        openrouter = _load_worker("openrouter_worker_caps", "engines/openrouter_worker.py")
        self.assertEqual(gemini.DEFAULT_AGY_BIN, str(Path(DEFAULT_AGY_BIN).expanduser()))
        self.assertEqual(openrouter.DEFAULT_CLAUDE_BIN, str(Path(DEFAULT_CLAUDE_BIN).expanduser()))

    def test_unspecified_route_prefers_native_subscription_and_refuses_openrouter(self) -> None:
        cases = [
            ("codex", "openai", "subscription", {"codex", "opencode"}, False, "allowed", "codex", False),
            ("codex", "openai", "subscription", {"opencode"}, False, "blocked", "", False),
            ("codex", "openai", "api", {"codex", "opencode"}, False, "allowed", "codex", False),
            ("codex", "xai", "subscription", {"codex", "grok", "opencode"}, False, "allowed", "grok", False),
            ("codex", "google", "subscription", {"codex", "gemini", "opencode"}, False, "allowed", "gemini", False),
            ("codex", "google", "subscription", {"codex", "opencode"}, False, "blocked", "", False),
            ("codex", "google", "api", {"gemini", "opencode"}, False, "blocked", "gemini", False),
            ("grok", "xai", "subscription", {"grok", "opencode"}, False, "allowed", "grok", False),
            ("grok", "xai", "api", {"grok", "opencode"}, False, "blocked", "grok", False),
            ("grok", "openai", "subscription", {"grok", "codex"}, False, "allowed", "codex", False),
            ("grok", "openai", "subscription", {"grok"}, False, "blocked", "", False),
            ("gemini", "google", "subscription", {"gemini"}, False, "allowed", "gemini", False),
            ("gemini", "google", "api", {"gemini", "opencode"}, False, "blocked", "gemini", False),
            ("gemini", "openai", "subscription", {"gemini", "codex", "opencode"}, False, "allowed", "codex", False),
            ("codex", "openrouter", "subscription", {"codex", "opencode"}, False, "blocked", "", False),
            ("codex", "openrouter", "api", {"codex", "opencode"}, True, "allowed", "opencode", True),
            ("codex", "openrouter", "api", {"openrouter"}, True, "allowed", "openrouter", True),
            ("codex", "openai", "subscription", {"codex", "opencode"}, True, "allowed", "codex", False),
            ("claude", "anthropic", "subscription", {"claude", "opencode"}, False, "allowed", "claude", False),
            ("claude", "anthropic", "api", {"claude", "opencode"}, False, "blocked", "claude", False),
        ]
        for origin, provider, route, available, consent, status, engine, spends in cases:
            with self.subTest(origin=origin, provider=provider, route=route, available=sorted(available), consent=consent):
                decision = choose_unspecified_route(
                    originating=origin,
                    provider=provider,
                    available=available,
                    openrouter_consent=consent,
                    billing_route=route,
                    model="probe",
                )
                self.assertEqual(status, decision.status)
                self.assertEqual(engine, decision.engine)
                self.assertEqual(spends, decision.spends_openrouter)
                if status == "blocked":
                    self.assertIn("OpenRouter was not selected", decision.reason)
                if status == "allowed" and route == "subscription" and provider == "openai":
                    self.assertIn("ChatGPT", decision.reason)
                    self.assertIn("api_key", decision.reason)

    def test_explicit_route_is_kept_or_blocked_without_a_substitute(self) -> None:
        kept = assess_explicit_route(
            engine="opencode",
            model="openrouter/z-ai/glm-5.3-flash",
            billing_route="api",
        )
        self.assertEqual("allowed", kept.status)
        self.assertEqual("opencode", kept.engine)
        self.assertTrue(kept.spends_openrouter)

        refused = [
            assess_explicit_route(engine="opencode", model="openrouter/z-ai/glm-5.3-flash", billing_route="subscription"),
            assess_explicit_route(engine="opencode", model="openrouter/z-ai/glm-5.3-flash", billing_route=None),
            assess_explicit_route(engine="codex", model="openrouter/x-ai/grok-4.5", billing_route="subscription"),
            assess_explicit_route(engine="grok", model="gpt-5.6-terra", billing_route="subscription"),
            assess_explicit_route(engine="gemini", model="grok-4.7", billing_route="subscription"),
            assess_explicit_route(engine="gemini", model="gemini-4-argon-medium", billing_route="api"),
            assess_explicit_route(engine="codex", model="gemini-3.8-flash-low", billing_route="subscription"),
            assess_explicit_route(engine="claude", model="opus", billing_route="api"),
            assess_explicit_route(
                engine="grok", model="grok-4.7", billing_route="subscription", billing_policy_loaded=True,
            ),
            assess_explicit_route(
                engine="gemini", model="gemini-4-argon-medium", billing_route="subscription", billing_policy_loaded=True,
            ),
        ]
        for decision in refused:
            with self.subTest(engine=decision.engine, model=decision.model, route=decision.billing_route):
                self.assertEqual("blocked", decision.status)
                self.assertFalse(decision.spends_openrouter)
                self.assertIn("OpenRouter was not selected", decision.reason)

        codex = assess_explicit_route(engine="codex", model="gpt-5.6-terra", billing_route="subscription")
        self.assertEqual("allowed", codex.status)
        self.assertIn("ChatGPT", codex.reason)
        self.assertIn("api_key", codex.reason)
        api = assess_explicit_route(engine="codex", model="gpt-5.6-terra", billing_route="api")
        self.assertEqual("allowed", api.status)
        self.assertEqual("api", api.billing_route)
        gemini = assess_explicit_route(engine="gemini", model="gemini-4-argon-medium", billing_route="subscription")
        self.assertEqual("allowed", gemini.status)
        self.assertIn("agy", gemini.reason)
        mock = assess_explicit_route(engine="mock", model="openrouter/z-ai/glm-5.2", billing_route=None)
        self.assertEqual("allowed", mock.status)
        self.assertEqual("mock", mock.engine)
        self.assertFalse(mock.spends_openrouter)

    def test_codex_subscription_and_api_key_are_not_interchangeable(self) -> None:
        policy = {
            "monthly_api_cap_gbp": "1",
            "run_api_cap_gbp": "1",
            "automatic_paid_fallback": True,
            "subscription_auth_modes": ["chatgpt"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = dict(policy=policy, usage_snapshot=_snapshot(), state_dir=root, now=NOW, month="2026-11")
            fallback = billing_preflight(
                engine="codex", model="gpt-5.6-terra", observed_auth_mode="chatgpt",
                billing_route="subscription", auto_fallback_requested=True, attempt_id="fallback",
                task_spend_allowance_gbp="0.2", **common,
            )
            self.assertFalse(fallback.allow)
            self.assertIn("not authorised", fallback.reason)
            self.assertFalse((root / "cost-reservations.json").exists())

            labelled = billing_preflight(
                engine="opencode", model="openrouter/z-ai/glm-5.3-flash", observed_auth_mode=None,
                billing_route="subscription", attempt_id="labelled", task_spend_allowance_gbp="0.2", **common,
            )
            self.assertFalse(labelled.allow)
            self.assertIn("native subscription", labelled.reason)
            self.assertFalse((root / "cost-reservations.json").exists())

            wrong_auth = billing_preflight(
                engine="codex", model="gpt-5.6-terra", observed_auth_mode="api_key",
                billing_route="subscription", attempt_id="sub", **common,
            )
            self.assertFalse(wrong_auth.allow)
            self.assertIn("ChatGPT", wrong_auth.reason)

            api_with_subscription = billing_preflight(
                engine="codex", model="gpt-5.6-terra", observed_auth_mode="chatgpt",
                billing_route="api", attempt_id="api", task_spend_allowance_gbp="0.2", **common,
            )
            self.assertFalse(api_with_subscription.allow)
            self.assertIn("api_key", api_with_subscription.reason)
            self.assertFalse((root / "cost-reservations.json").exists())

            subscription = billing_preflight(
                engine="codex", model="gpt-5.6-terra", observed_auth_mode="chatgpt",
                billing_route="subscription", attempt_id="ok-sub", **common,
            )
            self.assertTrue(subscription.allow, subscription.reason)
            self.assertEqual("subscription", subscription.route)

            foreign = billing_preflight(
                engine="codex", model="openrouter/google/gemini-3.8-flash", observed_auth_mode="api_key",
                billing_route="api", attempt_id="foreign", task_spend_allowance_gbp="0.2", **common,
            )
            self.assertFalse(foreign.allow)
            self.assertIn("not admitted", foreign.reason)

    def test_null_role_candidates_does_not_raise_and_runner_does_not_call_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            assessment = state / "model-assessment"
            assessment.mkdir()
            (assessment / "assessment.json").write_text('{"role_candidates": null}', encoding="utf-8")

            class Task:
                task_type = ""

            model = select_openrouter_fallback_model(Task(), state_dir=state)
        self.assertTrue(model.startswith("openrouter/"))
        self.assertNotIn("select_openrouter_fallback_model", (ROOT / "ringer.py").read_text(encoding="utf-8"))

    def test_origin_harness_does_not_select_another_providers_engine(self) -> None:
        decision = choose_unspecified_route(
            originating="codex",
            provider="google",
            available={"codex", "gemini", "opencode"},
            openrouter_consent=False,
            billing_route="subscription",
        )
        self.assertEqual("allowed", decision.status)
        self.assertEqual("gemini", decision.engine)
        self.assertFalse(decision.spends_openrouter)
        blocked = choose_unspecified_route(
            originating="codex",
            provider="google",
            available={"codex", "opencode"},
            openrouter_consent=True,
            billing_route="subscription",
        )
        self.assertEqual("blocked", blocked.status)
        self.assertFalse(blocked.spends_openrouter)

    def test_subscription_probes_do_not_infer_billing_from_names_or_secrets(self) -> None:
        secret = "xai-test-secret-not-a-subscription"
        with patch.dict(os.environ, {"XAI_API_KEY": secret, "GEMINI_API_KEY": secret, "GOOGLE_API_KEY": secret}):
            grok = subscription_probe("grok")
            gemini = subscription_probe("gemini")
            claude = subscription_probe("claude")
            codex = subscription_probe("codex")
        self.assertTrue(codex.supported)
        self.assertIn("chatgpt", codex.reason)
        self.assertIn("api_key", codex.reason)
        for probe in (grok, gemini, claude):
            self.assertFalse(probe.supported)
            self.assertNotIn(secret, probe.reason)
        self.assertIn("XAI_API_KEY", grok.reason)
        self.assertIn("auth.json", grok.reason)
        self.assertIn("keyring", gemini.reason)
        self.assertIn("GEMINI_API_KEY", gemini.reason)
        loaded = assess_explicit_route(
            engine="grok", model="grok-4.7", billing_route="subscription", billing_policy_loaded=True,
        )
        self.assertEqual("blocked", loaded.status)
        self.assertIn("not subscription evidence", loaded.reason)
        self.assertFalse(loaded.spends_openrouter)


class DispatchFailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker = self.root / "spawned"
        gate = patch.object(ringer, "validate_manifest_model_assessment", return_value={})
        gate.start()
        self.addCleanup(gate.stop)

    def _stub(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def _policy(self) -> Path:
        path = self.root / "policy.json"
        path.write_text(json.dumps({
            "monthly_api_cap_gbp": "5",
            "run_api_cap_gbp": "5",
            "automatic_paid_fallback": True,
            "subscription_auth_modes": ["chatgpt"],
        }), encoding="utf-8")
        return path

    def _config(self, engines, policy: Path | None = None) -> ringer.AppConfig:
        return ringer.AppConfig(
            path=None, identity_default=None, state_dir=self.root / "state",
            dashboard_port_base=18787, hud_port=18788, hud_app_path=None,
            allow_full_access=False,
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
            engines=engines,
            artifact=ringer.ArtifactConfig(
                enabled=False, out_template="", report_template="", index_out=self.root / "index",
            ),
            billing_policy_path=policy,
        )

    def _run(self, manifest, config) -> ringer.RingerRunner:
        runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
        asyncio.run(runner.run())
        return runner

    def test_policy_loaded_grok_does_not_spawn_or_reserve_or_leak_api_key(self) -> None:
        secret = "xai-test-secret-not-a-subscription"
        stub = self._stub("grok", f"#!/bin/sh\necho spawned >> {self.marker}\nexit 0\n")
        other = self._stub("openrouter-bin", f"#!/bin/sh\necho paid >> {self.marker}\nexit 0\n")
        manifest = ringer.Manifest.from_obj({
            "run_name": "grok-unobserved",
            "workdir": str(self.root / "work"),
            "tasks": [{
                "key": "one", "spec": "Write out.txt", "engine": "grok", "model": "grok-4.7",
                "billing_route": "subscription", "check": "test -s out.txt", "expect_files": ["out.txt"],
            }],
        })
        config = self._config({
            "grok": ringer.EngineConfig(
                name="grok", bin=str(stub), args_template=("-p", "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None, model_default="grok-4.7",
            ),
            "openrouter": ringer.EngineConfig(
                name="openrouter", bin=str(other), args_template=("{spec}",),
                full_access_args=(), sandbox_args=(), token_regex=None,
                model_default="openrouter/x-ai/grok-4.7",
            ),
        }, self._policy())
        with patch.dict(os.environ, {"XAI_API_KEY": secret}):
            runner = self._run(manifest, config)
        runtime = runner.runtimes[0]
        self.assertEqual(0, runtime.attempts)
        self.assertEqual("blocked", runtime.status)
        self.assertEqual("grok", runtime.task.engine)
        self.assertIn("XAI_API_KEY", runtime.setup_error or "")
        self.assertNotIn(secret, runtime.setup_error or "")
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())

    def test_provider_error_does_not_switch_to_the_configured_openrouter_bin(self) -> None:
        codex = self._stub("codex", "#!/bin/sh\necho 'error: connection refused' >&2\nexit 1\n")
        paid = self._stub("paid", f"#!/bin/sh\necho paid >> {self.marker}\nprintf 'ok\\n' > out.txt\n")
        manifest = ringer.Manifest.from_obj({
            "run_name": "provider-error",
            "workdir": str(self.root / "work"),
            "tasks": [{
                "key": "one", "spec": "Write out.txt", "engine": "codex", "model": "gpt-5.6-terra",
                "billing_route": "subscription", "max_attempts": 1,
                "check": "grep -q ok out.txt", "expect_files": ["out.txt"],
            }],
        })
        config = self._config({
            "codex": ringer.EngineConfig(
                name="codex", bin=str(codex), args_template=("{spec}",),
                full_access_args=(), sandbox_args=(), token_regex=None, model_default="gpt-5.6-terra",
            ),
            "opencode": ringer.EngineConfig(
                name="opencode", bin=str(paid),
                args_template=("run", "-m", "{model}", "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None,
            ),
        })
        runner = self._run(manifest, config)
        runtime = runner.runtimes[0]
        self.assertEqual("codex", runtime.task.engine)
        self.assertNotEqual("pass", runtime.status)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())

    def test_quota_with_budget_cap_does_not_reserve_openrouter(self) -> None:
        usage = self.root / "usage"
        usage.mkdir()
        (usage / "codex.json").write_text(json.dumps({
            "limits": [{"label": "Weekly", "percent": 1.0, "resetsAt": "2099-01-01T00:00:00Z"}],
        }), encoding="utf-8")
        codex = self._stub("codex", f"#!/bin/sh\necho ran >> {self.marker}\n")
        paid = self._stub("paid", f"#!/bin/sh\necho paid >> {self.marker}\nprintf 'ok\\n' > out.txt\n")
        manifest = ringer.Manifest.from_obj({
            "run_name": "quota-no-switch",
            "workdir": str(self.root / "work"),
            "tasks": [{
                "key": "one", "spec": "Write out.txt", "engine": "codex", "model": "gpt-5.6-terra",
                "billing_route": "subscription", "task_spend_allowance_gbp": 0.25,
                "check": "grep -q ok out.txt", "expect_files": ["out.txt"],
            }],
        })
        config = self._config({
            "codex": ringer.EngineConfig(
                name="codex", bin=str(codex), args_template=("{spec}",),
                full_access_args=(), sandbox_args=(), token_regex=None,
            ),
            "openrouter": ringer.EngineConfig(
                name="openrouter", bin=str(paid), args_template=("{spec}",),
                full_access_args=(), sandbox_args=(), token_regex=None,
                model_default="openrouter/z-ai/glm-5.3-flash",
            ),
        }, self._policy())
        with patch.dict(os.environ, {"RINGER_USAGE_DIR": str(usage)}):
            runner = self._run(manifest, config)
        runtime = runner.runtimes[0]
        self.assertEqual(0, runtime.attempts)
        self.assertEqual("quota", runtime.failure_class)
        self.assertEqual("codex", runtime.task.engine)
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())

    def test_explicit_openrouter_consent_is_the_api_route_not_the_budget_cap(self) -> None:
        binary = self._stub("opencode", "#!/bin/sh\nprintf 'ok\\n' > out.txt\n")
        engines = {
            "opencode": ringer.EngineConfig(
                name="opencode", bin=str(binary),
                args_template=("run", "-m", "{model}", "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None,
            ),
        }
        refused = ringer.Manifest.from_obj({
            "run_name": "cap-is-not-consent",
            "workdir": str(self.root / "work-refused"),
            "tasks": [{
                "key": "one", "spec": "Write out.txt", "engine": "opencode",
                "model": "openrouter/z-ai/glm-5.3-flash", "billing_route": "subscription",
                "task_spend_allowance_gbp": 0.25, "check": "grep -q ok out.txt",
                "expect_files": ["out.txt"],
            }],
        })
        runner = self._run(refused, self._config(engines, self._policy()))
        self.assertEqual(0, runner.runtimes[0].attempts)
        self.assertEqual("opencode", runner.runtimes[0].task.engine)
        self.assertIn("billing_route api", runner.runtimes[0].setup_error or "")
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())
        self.assertFalse((self.root / "work-refused" / "one" / "out.txt").exists())

        allowed = ringer.Manifest.from_obj({
            "run_name": "explicit-consent",
            "workdir": str(self.root / "work-allowed"),
            "tasks": [{
                "key": "one", "spec": "Write out.txt with ok.", "engine": "opencode",
                "model": "openrouter/z-ai/glm-5.3-flash", "billing_route": "api",
                "task_spend_allowance_gbp": 0.25, "check": "grep -q ok out.txt",
                "expect_files": ["out.txt"],
            }],
        })
        runner = self._run(allowed, self._config(engines, self._policy()))
        self.assertEqual("pass", runner.runtimes[0].status)
        self.assertEqual("ADMITTED", runner.runtimes[0].billing.get("billing_status"))
        ledger = json.loads((self.root / "state" / "cost-reservations.json").read_text(encoding="utf-8"))
        self.assertTrue(ledger["reservations"])


class WrapperPreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {
            "PATH": str(self.bindir) + os.pathsep + "/usr/bin:/bin",
            "HOME": str(self.home),
        }

    def _wrapper(self) -> Path:
        path = self.root / "opencode-sandboxed.sh"
        text = WRAPPER.read_text(encoding="utf-8")
        marker = str(self.root / "invoked")
        text = text.replace("#!/bin/bash\n", f"#!/bin/bash\necho invoked >> {marker!s}\n", 1)
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_real_wrapper_exits_127_when_opencode_is_absent(self) -> None:
        taskdir = self.root / "task"
        taskdir.mkdir()
        env = os.environ.copy()
        env["PATH"] = "/usr/bin:/bin"
        env["HOME"] = str(self.home)
        result = subprocess.run(
            ["/bin/bash", str(WRAPPER), str(taskdir), "--no-sandbox", "run", "-m", "openrouter/z-ai/glm-5.3-flash", "spec"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        self.assertEqual(127, result.returncode)
        self.assertIn("opencode not found on PATH", result.stderr)

    def test_inventory_drops_the_wrapper_and_keeps_codex(self) -> None:
        codex = self.bindir / "codex"
        codex.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        codex.chmod(0o755)
        present = inventory_engines(
            {"codex": "codex", "opencode": str(self._wrapper()), "grok": "grok"},
            self.env,
        )
        self.assertEqual(("codex",), present)
        self.assertEqual(("opencode",), missing_runtime_dependencies(str(self.root / "opencode-sandboxed.sh"), self.env))

    def test_codex_preflight_does_not_require_opencode(self) -> None:
        codex = self.bindir / "codex"
        codex.write_text("#!/bin/sh\nprintf 'ok\\n' > out.txt\n", encoding="utf-8")
        codex.chmod(0o755)
        wrapper = self._wrapper()
        manifest = ringer.Manifest.from_obj({
            "run_name": "codex-only",
            "workdir": str(self.root / "work"),
            "tasks": [{
                "key": "one",
                "spec": "Write out.txt",
                "engine": "codex",
                "model": "gpt-5.6-terra",
                "billing_route": "subscription",
                "check": "grep -q ok out.txt",
                "expect_files": ["out.txt"],
            }],
        })
        config = self._config(manifest, {
            "codex": ringer.EngineConfig(
                name="codex", bin=str(codex), args_template=("{spec}",),
                full_access_args=(), sandbox_args=(), token_regex=None, model_default="gpt-5.6-terra",
            ),
            "opencode": ringer.EngineConfig(
                name="opencode", bin=str(wrapper),
                args_template=("{taskdir}", "run", "-m", "{model}", "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None,
            ),
        })
        with patch.dict(os.environ, self.env, clear=False):
            ringer.preflight_engine_bins(manifest, config)
            ringer.enforce_explicit_routes(manifest, config)
            gate = patch.object(ringer, "validate_manifest_model_assessment", return_value={})
            gate.start()
            self.addCleanup(gate.stop)
            runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
            self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual("codex", runner.runtimes[0].task.engine)
        self.assertTrue((self.root / "work" / "one" / "out.txt").exists())
        self.assertFalse((self.root / "invoked").exists())

    def test_explicit_opencode_wrapper_blocks_before_spawn_or_reservation(self) -> None:
        wrapper = self._wrapper()
        policy = self.root / "policy.json"
        policy.write_text(json.dumps({
            "monthly_api_cap_gbp": "5",
            "run_api_cap_gbp": "5",
            "automatic_paid_fallback": True,
            "subscription_auth_modes": ["chatgpt"],
        }), encoding="utf-8")
        task = {
            "key": "one",
            "spec": "Write the layout.",
            "engine": "opencode",
            "model": "openrouter/z-ai/glm-5.3-flash",
            "billing_route": "api",
            "task_spend_allowance_gbp": 0.25,
            "check": "test -s bundle/layouts/lumon-refined.json",
            "expect_files": ["bundle/layouts/lumon-refined.json"],
            "max_attempts": 1,
        }
        manifest = ringer.Manifest.from_obj({
            "run_name": "explicit-opencode",
            "workdir": str(self.root / "work"),
            "tasks": [task],
        })
        config = self._config(manifest, {
            "opencode": ringer.EngineConfig(
                name="opencode", bin=str(wrapper),
                args_template=("{taskdir}", "run", "-m", "{model}", "--format", "json", "{spec}"),
                full_access_args=(), sandbox_args=(), token_regex=None,
            ),
            "codex": ringer.EngineConfig(
                name="codex", bin=str(self.bindir / "codex"),
                args_template=("exec", "{spec}",),
                full_access_args=(), sandbox_args=(), token_regex=None,
                model_default="gpt-5.6-terra",
            ),
        }, policy)
        with patch.dict(os.environ, self.env, clear=False):
            with self.assertRaisesRegex(ValueError, "opencode"):
                ringer.preflight_engine_bins(manifest, config)
            gate = patch.object(ringer, "validate_manifest_model_assessment", return_value={})
            gate.start()
            self.addCleanup(gate.stop)
            runner = ringer.RingerRunner(manifest, config, "test", dashboard_enabled=False)
            rc = asyncio.run(runner.run())
        self.assertEqual(1, rc)
        runtime = runner.runtimes[0]
        self.assertEqual(0, runtime.attempts)
        self.assertEqual("blocked", runtime.status)
        self.assertEqual("missing_dependency", runtime.failure_class)
        self.assertEqual("opencode", runtime.task.engine)
        self.assertEqual("openrouter/z-ai/glm-5.3-flash", runtime.task.model)
        self.assertIn("opencode", runtime.setup_error or "")
        self.assertIn("OpenRouter was not selected", runtime.setup_error or "")
        self.assertFalse((self.root / "invoked").exists())
        self.assertFalse((self.root / "state" / "cost-reservations.json").exists())
        self.assertNotEqual("ADMITTED", runtime.billing.get("billing_status"))

    def _config(self, manifest, engines, policy: Path | None = None) -> ringer.AppConfig:
        del manifest
        return ringer.AppConfig(
            path=None,
            identity_default=None,
            state_dir=self.root / "state",
            dashboard_port_base=18787,
            hud_port=18788,
            hud_app_path=None,
            allow_full_access=False,
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
            engines=engines,
            artifact=ringer.ArtifactConfig(enabled=False, out_template="", report_template="", index_out=self.root / "index"),
            billing_policy_path=policy,
        )


if __name__ == "__main__":
    unittest.main()
