"""Offline regression tests for dispatch controls and spend reconciliation."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ringer
from ringer_billing import usage_evidence
from ringer_costs import SpendLedger


CODEX_STUB = """\
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
settings = json.loads((Path(__file__).parent / "cli-settings.json").read_text())
for index, arg in enumerate(args[:-1]):
    if arg == "-c":
        key, _, value = args[index + 1].partition("=")
        settings[key] = value.strip('"')
api_inputs_present = {
    name: name in os.environ for name in ("OPENAI_API_KEY", "OPENAI_FEDERATION_RULE_ID")
}
Path("cli-invocation.json").write_text(json.dumps({
    "args": args, "settings": settings, "api_inputs_present": api_inputs_present,
}))
if args[0] != "exec" or settings["forced_login_method"] != settings["logged_in_as"]:
    raise SystemExit(1)
Path("result.txt").write_text("fixture output")
"""

OPENROUTER_STUB = """\
import io
import sys
from pathlib import Path

sys.path.insert(0, {root!r})
from engines import openrouter_worker as worker

class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

def open_fixture(request, *, timeout):
    assert request.get_method() == "POST"
    assert b'"usage": {{"include": true}}' in request.data
    with (Path(__file__).parent / "requests.log").open("a") as calls:
        calls.write(Path.cwd().name + "\\n")
    return Response((Path(__file__).parent / "response.json").read_bytes())

worker.load_openrouter_key = lambda *_: "inert-fixture"
code, row = worker.execute(sys.argv[1:], env={{}}, opener=open_fixture)
print(row)
raise SystemExit(code)
"""


class RingerControlsTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.state_dir = self.root / "state"
        self.month = datetime.now(timezone.utc).strftime("%Y-%m")

    def executable(self, name, source):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def codex(self, logged_in_as):
        (self.root / "cli-settings.json").write_text(json.dumps({
            "logged_in_as": logged_in_as,
            "forced_login_method": "api",
            "model_provider": "local-default",
            "service_tier": "fast",
        }))
        return replace(ringer.built_in_codex_engine(), bin=self.executable("codex-fixture", CODEX_STUB))

    def openrouter(self, usage):
        (self.root / "response.json").write_text(json.dumps({
            "id": "generation-fixture",
            "choices": [{"message": {"content": "fixture output"}}],
            "usage": usage,
        }))
        return ringer.EngineConfig(
            name="openrouter",
            bin=self.executable("openrouter-fixture", OPENROUTER_STUB.format(root=str(ROOT))),
            args_template=("--max-input-tokens", "128", "--max-tokens", "8",
                           "{model_args}", "{engine_args}", "{spec}"),
            full_access_args=(),
            sandbox_args=(),
            token_regex=None,
        )

    def policy(self):
        path = self.root / "policy.json"
        path.write_text(json.dumps({
            "monthly_api_cap_gbp": "1",
            "run_api_cap_gbp": "0.5",
            "task_api_cap_gbp": "0.25",
            "paid_retries": False,
            "fx": {"usd_to_gbp": "0.8", "dated": datetime.now(timezone.utc).date().isoformat()},
        }))
        return path

    def task(self, key, engine, **fields):
        paid = engine == "openrouter"
        output = "openrouter-output.txt" if paid else "result.txt"
        task = dict(key=key, engine=engine, spec="Write the synthetic output.",
                    check=f"test -s {output}", expect_files=[output])
        if paid:
            task.update(billing_route="api", task_class="bounded-detail",
                        task_spend_allowance_gbp=0.25)
        task.update(fields)
        return task

    def runner(self, engine, *tasks, policy=None, allow_fast=False):
        config = ringer.AppConfig(
            path=None, identity_default=None, state_dir=self.state_dir,
            dashboard_port_base=18787, hud_port=18788, hud_app_path=None,
            allow_full_access=False, engines={engine.name: engine},
            eval=ringer.EvalConfig(backend="jsonl", jsonl_path=self.root / "attempts.jsonl"),
            artifact=ringer.ArtifactConfig(enabled=False, out_template="", report_template="",
                                           index_out=self.root / "index"),
            billing_policy_path=policy, allow_fast_service=allow_fast, require_model_assessment=False,
        )
        manifest = ringer.Manifest.from_obj({
            "run_name": "ringer-controls", "workdir": str(self.root / "work"),
            "max_parallel": 1, "tasks": list(tasks),
        })
        return ringer.RingerRunner(manifest, config, "fixture", dashboard_enabled=False)

    def invocations(self):
        return (self.root / "requests.log").read_text().splitlines()

    def reservations(self):
        return json.loads((self.state_dir / "cost-reservations.json").read_text())["reservations"]

    def test_no_policy_codex_cannot_inherit_api_login(self):
        engine = self.codex(logged_in_as="api")
        task = self.task("one", "codex", billing_route="subscription", model="gpt-5.6-sol", max_attempts=1)
        runner = self.runner(engine, task)
        self.assertEqual(1, asyncio.run(runner.run()))
        runtime = runner.runtimes[0]
        invocation = json.loads((runtime.taskdir / "cli-invocation.json").read_text())
        self.assertEqual("chatgpt", invocation["settings"]["forced_login_method"])
        self.assertEqual("openai", invocation["settings"]["model_provider"])
        self.assertEqual("api", invocation["settings"]["logged_in_as"])
        self.assertFalse((runtime.taskdir / "result.txt").exists())
        self.assertFalse((self.state_dir / "cost-reservations.json").exists())

    def test_legacy_native_subscription_inherits_the_model_and_uses_standard_service(self):
        runner = self.runner(self.codex(logged_in_as="chatgpt"), self.task("one", "codex"))
        with patch.dict(os.environ, {"OPENAI_API_KEY": "inert", "OPENAI_FEDERATION_RULE_ID": "inert"}):
            self.assertEqual(0, asyncio.run(runner.run()))
        invocation = json.loads((runner.runtimes[0].taskdir / "cli-invocation.json").read_text())
        self.assertEqual("default", invocation["settings"]["service_tier"])
        self.assertEqual("chatgpt", invocation["settings"]["forced_login_method"])
        self.assertNotIn("-m", invocation["args"])
        self.assertFalse(any(invocation["api_inputs_present"].values()))

    def test_dry_run_previews_native_subscription_command(self):
        runner = self.runner(self.codex(logged_in_as="chatgpt"), self.task("one", "codex"))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            ringer.dry_run(runner.manifest, runner.config, "fixture",
                           dashboard_enabled=False, force_browser=False)
        self.assertIn('forced_login_method="chatgpt"', output.getvalue())
        self.assertIn("service_tier=default", output.getvalue())
        self.assertFalse(runner.manifest.workdir.exists())

    def test_unassessed_fast_is_blocked_before_dispatch(self):
        engine = self.codex(logged_in_as="chatgpt")
        for args in (
            ["-c", "service_tier=fast"],
            ["-c=service_tier=fast"],
            ['--config="service_tier"="fast"'],
            ["--service-tier=fast", "--service-tier=standard"],
        ):
            with self.subTest(args=args):
                runner = self.runner(engine, self.task("one", "codex", engine_args=args, spec="fast"))
                self.assertIsNone(runner.manifest.model_assessment)
                with self.assertRaisesRegex(ringer.ModelAssessmentError, "fast service is not authorised"):
                    asyncio.run(runner.run())
                self.assertFalse(runner.manifest.workdir.exists())

    def test_fast_in_an_automatically_resolved_command_is_blocked(self):
        engine = self.codex(logged_in_as="chatgpt")
        task = self.task("one", "auto", task_class="coordination", model="gpt-5.6-sol",
                         engine_args=["-c", "service_tier=fast"])
        runner = self.runner(engine, task)
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertIn("fast service is not authorised", runner.runtimes[0].setup_error)
        self.assertEqual(0, runner.runtimes[0].attempts)

    def test_explicit_fast_opt_in_preserves_the_selected_service(self):
        engine = self.codex(logged_in_as="chatgpt")
        task = self.task("one", "codex", engine_args=["-c", 'service_tier="fast"'])
        runner = self.runner(engine, task, allow_fast=True)
        self.assertEqual(0, asyncio.run(runner.run()))
        invocation = json.loads((runner.runtimes[0].taskdir / "cli-invocation.json").read_text())
        self.assertEqual("fast", invocation["settings"]["service_tier"])

    def test_openrouter_cost_estimate_stays_distinct_from_cash(self):
        engine = self.openrouter({"prompt_tokens": 2, "completion_tokens": 1})
        tasks = [self.task(key, "openrouter") for key in ("one", "two")]
        runner = self.runner(engine, *tasks, policy=self.policy())
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual(["one", "two"], self.invocations())
        for row in self.reservations().values():
            self.assertEqual(("reserved", True, None), (row["state"], row["started"], row["actual_gbp"]))
        evidence = runner.runtimes[0].billing
        self.assertGreater(Decimal(evidence["catalogue_estimate_usd"]), 0)
        self.assertIsNone(evidence["actual_cost_usd"])
        self.assertEqual("unknown", evidence["cost_coverage"])

    def test_openrouter_settles_the_reported_charge_and_stops_on_cash_overspend(self):
        engine = self.openrouter({"prompt_tokens": 2, "completion_tokens": 1, "cost": 0.00125})
        runner = self.runner(engine, self.task("one", "openrouter"), self.task("two", "openrouter"),
                             policy=self.policy())
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(["one"], self.invocations())
        row, = self.reservations().values()
        self.assertEqual(("settled", "0.001000", True), (row["state"], row["actual_gbp"], row["overspend"]))
        self.assertIn("OpenRouter response usage.cost", row["settlement_source"])
        evidence = runner.runtimes[0].billing
        self.assertEqual("provider_actual", evidence["cost_coverage"])
        self.assertNotEqual(Decimal(evidence["catalogue_estimate_usd"]), Decimal(evidence["actual_cost_usd"]))

    def test_openrouter_stops_for_validated_overrun_with_unknown_cost(self):
        engine = self.openrouter({"prompt_tokens": 2, "completion_tokens": 9, "cost": True})
        runner = self.runner(engine, self.task("one", "openrouter"), self.task("two", "openrouter"),
                             policy=self.policy())
        ledger = SpendLedger(self.state_dir)
        self.assertTrue(ledger.reserve("queued", "0.01", "1", self.month))
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(["one"], self.invocations())
        identity = f"{runner.run_id}/one/1"
        row = self.reservations()[identity]
        self.assertEqual(("reserved", True, None), (row["state"], row["started"], row["actual_gbp"]))
        self.assertTrue(row["token_bound_exceeded"])
        self.assertFalse(ledger.mark_started("queued"))
        self.assertIn("token bound overrun", runner.runtimes[1].billing["billing_reason"])
        self.assertTrue(ledger.settle(identity, "0.000001", source="authoritative reconciliation fixture"))
        row = self.reservations()[identity]
        self.assertEqual(("settled", "0.000001", False), (row["state"], row["actual_gbp"], row["overspend"]))
        self.assertTrue(row["token_bound_exceeded"])
        self.assertFalse(ledger.reserve("later", "0.01", "1", self.month))

    def test_openrouter_settles_reported_cost_even_when_token_bound_fails(self):
        engine = self.openrouter({"prompt_tokens": 129, "completion_tokens": None, "cost": 0})
        runner = self.runner(engine, self.task("one", "openrouter"), self.task("two", "openrouter"),
                             policy=self.policy())
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(["one"], self.invocations())
        row, = self.reservations().values()
        self.assertEqual(("settled", "0.0", False), (row["state"], row["actual_gbp"], row["overspend"]))
        self.assertTrue(row["token_bound_exceeded"])
        self.assertEqual("FAIL", runner.runtimes[0].final_verdict)

    def test_legacy_v2_rows_remain_readable_and_enforce_existing_stops(self):
        self.state_dir.mkdir()
        legacy = dict(month=self.month, state="settled", run_id=None, started=True,
                      amount_gbp="0.10", actual_gbp="0.20", overspend=True,
                      settlement_source="legacy source", release_evidence=None)
        path = self.state_dir / "cost-reservations.json"
        path.write_text(json.dumps({"schema": "ringer-spend/v2", "reservations": {"legacy": legacy}}))
        ledger = SpendLedger(self.state_dir)
        self.assertFalse(ledger.reserve("later", "0.01", "1", self.month))
        self.assertEqual({"legacy": legacy}, self.reservations())

    def test_legacy_worker_usage_uses_the_admitted_bounds(self):
        row = {
            "type": "error",
            "model": "openrouter/z-ai/glm-5.3-flash",
            "usage": {"input_tokens": 2, "output_tokens": 9},
            "max_input_tokens": 128,
            "max_output_tokens": 16,
        }
        evidence = usage_evidence(json.dumps(row), row["model"], complete=False,
                                  openrouter_bounds=(128, 8))
        self.assertTrue(evidence["token_bound_exceeded"])
        self.assertIsNone(evidence["actual_cost_usd"])


if __name__ == "__main__":
    unittest.main()
