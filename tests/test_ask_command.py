#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import ringer
import ringer_billing
from ringer import TaskSpec


ROOT = Path(__file__).resolve().parents[1]


def toml_string(value: object) -> str:
    return json.dumps(str(value))


def cli_env(home: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["RINGER_NO_SELF_UPDATE"] = "1"
    env["RINGER_NO_CATALOG_REFRESH"] = "1"
    if home is not None:
        env["HOME"] = str(home)
    return env


class AskCommandTests(unittest.TestCase):
    def write_config(
        self,
        root: Path,
        worker: Path,
        *,
        engine_name: str = "answer-mock",
        artifact_enabled: bool = False,
    ) -> Path:
        config = root / "config.toml"
        config.write_text(
            "\n".join(
                [
                    f"state_dir = {toml_string(root / 'state')}",
                    "",
                    "[eval]",
                    'backend = "jsonl"',
                    f"jsonl_path = {toml_string(root / 'runs.jsonl')}",
                    "",
                    "[artifact]",
                    f"enabled = {'true' if artifact_enabled else 'false'}",
                    "",
                    f"[engines.{engine_name}]",
                    f"bin = {toml_string(sys.executable)}",
                    "args_template = [",
                    f"  {toml_string(worker)},",
                    '  "{spec}",',
                    '  "fixture-answer-worker",',
                    '  "{model_args}",',
                    '  "{engine_args}",',
                    "]",
                    "sandbox_args = []",
                    "full_access_args = []",
                    'model_default = "fixture-answer-model"',
                ]
            ),
            encoding="utf-8",
        )
        return config

    def run_cli(
        self,
        args: list[str],
        *,
        home: Path | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "ringer.py", *args],
            cwd=ROOT,
            env=cli_env(home),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    def run_in_process(
        self,
        args: list[str],
        *,
        home: Path,
    ) -> subprocess.CompletedProcess[str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, cli_env(home), clear=True),
            mock.patch.object(ringer, "ensure_hud_running"),
            mock.patch.object(ringer.Dashboard, "start", return_value=8787),
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            returncode = ringer.main(args)
        return subprocess.CompletedProcess(
            args,
            returncode,
            stdout.getvalue(),
            stderr.getvalue(),
        )

    def assessed_ask_args(self, args: list[str], config_path: Path, root: Path) -> list[str]:
        """Bind the real ask packet and route before its subprocess is launched."""
        parsed = ringer.build_parser().parse_args([*args, "--billing-route", "subscription"])
        config = ringer.AppConfig.load(config_path)
        packet = ringer.build_context_packet(
            parsed.request,
            sources=parsed.source,
            state_files=parsed.state,
            max_packet_bytes=parsed.max_packet_bytes,
            max_file_bytes=parsed.max_file_bytes,
            max_files=parsed.max_files,
        )
        engine = config.engines[parsed.engine]
        manifest = ringer.one_request_manifest(
            packet=packet,
            workdir=parsed.workdir,
            engine=parsed.engine,
            timeout_s=parsed.timeout_s,
            reasoning_effort=parsed.reasoning_effort,
            model=parsed.model or engine.model_default,
            redact=parsed.redact,
            billing_route=parsed.billing_route,
            task_spend_allowance_gbp=parsed.task_spend_allowance_gbp,
        )
        assessment = ringer.assessment_draft_for_manifest(manifest, config, coordinator="ask-test")
        assessment["strategy"] = "Exercise the source-bound local ask fixture through the real assessment gate."
        for row in assessment["tasks"].values():
            row.update(
                effort="medium",
                rationale="The local worker fixture exercises one-request admission and output handling.",
                alternative_considered="A parser-only test would not execute the real ask runner boundary.",
                context_plan="Use only the generated request packet and local worker script.",
                verification="Keep the original answer, redaction, one-attempt and state assertions.",
                escalation="Stop if the bound route or assessment no longer validates.",
                evidence="The packet, worker and assessment are deterministic local test fixtures.",
                uncertainty="No live model quality, entitlement or provider call is implied.",
            )
        assessment_path = root / "assessment.json"
        assessment_path.write_text(json.dumps(assessment), encoding="utf-8")
        return [*args, "--billing-route", "subscription", "--assessment", str(assessment_path)]

    def test_dry_run_selects_source_without_spawning_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            source = root / "long-notes.md"
            workdir = root / "request"
            source.write_text(
                ("Unrelated notes.\n" * 1_000)
                + "The launch decision is Wednesday with a smaller scope.\n"
                + ("More unrelated notes.\n" * 1_000),
                encoding="utf-8",
            )
            proc = self.run_cli(
                [
                    "ask",
                    "What was the launch decision?",
                    "--source",
                    str(source),
                    "--max-packet-bytes",
                    "3000",
                    "--workdir",
                    str(workdir),
                    "--keep-packet",
                    "--dry-run",
                ]
            )

            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            self.assertIn("No model call was made.", proc.stdout)
            self.assertIn(str(source.resolve()), proc.stdout)
            self.assertIn(
                "Wednesday with a smaller scope",
                (workdir / "packet.txt").read_text(),
            )
            report = json.loads(
                (workdir / "packet-report.json").read_text(encoding="utf-8")
            )
            self.assertLessEqual(report["packet_bytes"], 3_000)
            self.assertFalse((workdir / "answer").exists())

    def test_default_keeps_request_visible_and_run_is_watched(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = root / "home"
            workdir = root / "request"
            source = root / "notes.md"
            worker = root / "answer_worker.py"
            home.mkdir()
            source.write_text(
                "The answer is: ship Wednesday.\n",
                encoding="utf-8",
            )
            worker.write_text(
                "from pathlib import Path\n"
                "Path('answer.md').write_text('Ship Wednesday.\\n', encoding='utf-8')\n"
                "print('RAW WORKER OUTPUT: mock answer complete')\n",
                encoding="utf-8",
            )
            config = self.write_config(
                root,
                worker,
                artifact_enabled=True,
            )
            request = "What is the visible decision?"
            proc = self.run_in_process(
                self.assessed_ask_args([
                    "ask",
                    request,
                    "--source",
                    str(source),
                    "--engine",
                    "answer-mock",
                    "--config",
                    str(config),
                    "--workdir",
                    str(workdir),
                    "--identity",
                    "ask-test",
                ], config, root),
                home=home,
            )

            combined = proc.stdout + proc.stderr
            self.assertEqual(0, proc.returncode, combined)
            self.assertEqual(
                "Ship Wednesday.\n",
                (workdir / "answer" / "answer.md").read_text(),
            )
            self.assertIn("Ship Wednesday.", proc.stdout)
            worker_log = (workdir / "answer" / "worker.log").read_text(
                encoding="utf-8"
            )
            self.assertEqual(
                1,
                worker_log.count("[ringer.py] attempt 1 started"),
            )
            self.assertNotIn("[ringer.py] attempt 2 started", worker_log)
            self.assertIn(request, worker_log)
            self.assertIn("RAW WORKER OUTPUT: mock answer complete", worker_log)
            state_files = list((root / "state" / "runs").glob("*.json"))
            self.assertEqual(1, len(state_files))
            state = json.loads(state_files[0].read_text(encoding="utf-8"))
            self.assertIn(request, state["tasks"][0]["spec"])
            self.assertEqual(1, state["tasks"][0]["max_attempts"])
            self.assertIsInstance(state["dashboard_port"], int)
            self.assertIsNotNone(state["artifact_path"])
            self.assertIn(
                request,
                (root / "runs.jsonl").read_text(encoding="utf-8"),
            )
            library = json.loads(
                (root / "state" / "artifacts" / "library.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("one-request", library["artifacts"])
            self.assertFalse((workdir / "packet.txt").exists())

    def test_redact_hides_request_metadata_but_preserves_worker_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = root / "home"
            workdir = root / "request"
            worker = root / "answer_worker.py"
            home.mkdir()
            worker.write_text(
                "from pathlib import Path\n"
                "Path('answer.md').write_text('Redacted answer.\\n', encoding='utf-8')\n"
                "print('RAW WORKER OUTPUT MUST REMAIN')\n",
                encoding="utf-8",
            )
            config = self.write_config(root, worker)
            request = "PRIVATE REQUEST PHRASE 82"
            proc = self.run_in_process(
                self.assessed_ask_args([
                    "ask",
                    request,
                    "--redact",
                    "--engine",
                    "answer-mock",
                    "--config",
                    str(config),
                    "--workdir",
                    str(workdir),
                    "--identity",
                    "ask-redaction-test",
                ], config, root),
                home=home,
            )

            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            worker_log = (workdir / "answer" / "worker.log").read_text(
                encoding="utf-8"
            )
            state_path = next((root / "state" / "runs").glob("*.json"))
            state_text = state_path.read_text(encoding="utf-8")
            eval_text = (root / "runs.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(request, worker_log)
            self.assertNotIn(request, state_text)
            self.assertNotIn(request, eval_text)
            self.assertIn("[request packet omitted]", worker_log)
            self.assertIn("[redacted request packet]", state_text)
            self.assertIn("[redacted request packet]", eval_text)
            self.assertIn("RAW WORKER OUTPUT MUST REMAIN", worker_log)

    def test_existing_answer_directory_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            workdir = Path(temp_root) / "request"
            (workdir / "answer").mkdir(parents=True)
            proc = self.run_cli(
                [
                    "ask",
                    "Answer this.",
                    "--workdir",
                    str(workdir),
                    "--dry-run",
                ]
            )
            self.assertEqual(2, proc.returncode)
            self.assertIn("refusing to reuse", proc.stderr)

    def test_missing_explicit_source_stops_before_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = root / "home"
            worker = root / "worker.py"
            marker = root / "started.txt"
            home.mkdir()
            worker.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('started')\n",
                encoding="utf-8",
            )
            config = self.write_config(root, worker)
            proc = self.run_cli(
                [
                    "ask",
                    "Answer from the source.",
                    "--source",
                    str(root / "missing.md"),
                    "--engine",
                    "answer-mock",
                    "--config",
                    str(config),
                    "--workdir",
                    str(root / "request"),
                ],
                home=home,
            )
            self.assertEqual(2, proc.returncode)
            self.assertIn("no model call was made", proc.stderr)
            self.assertFalse(marker.exists())

    def test_failed_worker_starts_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = root / "home"
            worker = root / "worker.py"
            counter = root / "counter.txt"
            home.mkdir()
            worker.write_text(
                "from pathlib import Path\n"
                f"p = Path({str(counter)!r})\n"
                "n = int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n + 1))\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            config = self.write_config(root, worker)
            proc = self.run_in_process(
                self.assessed_ask_args([
                    "ask",
                    "Answer this.",
                    "--engine",
                    "answer-mock",
                    "--config",
                    str(config),
                    "--workdir",
                    str(root / "request"),
                    "--identity",
                    "ask-failure-test",
                ], config, root),
                home=home,
            )
            self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
            self.assertEqual("1", counter.read_text())

    def test_timed_out_worker_starts_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_root:
            root = Path(temp_root)
            home = root / "home"
            worker = root / "worker.py"
            counter = root / "counter.txt"
            home.mkdir()
            worker.write_text(
                "from pathlib import Path\n"
                "import time\n"
                f"p = Path({str(counter)!r})\n"
                "n = int(p.read_text()) if p.exists() else 0\n"
                "p.write_text(str(n + 1))\n"
                "time.sleep(10)\n",
                encoding="utf-8",
            )
            config = self.write_config(root, worker)
            proc = self.run_in_process(
                self.assessed_ask_args([
                    "ask",
                    "Answer this.",
                    "--engine",
                    "answer-mock",
                    "--config",
                    str(config),
                    "--timeout-s",
                    "1",
                    "--workdir",
                    str(root / "request"),
                    "--identity",
                    "ask-timeout-test",
                ], config, root),
                home=home,
            )
            self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
            self.assertEqual("1", counter.read_text())

    def test_max_attempts_parses_defaults_and_validates_positive(self) -> None:
        base = {
            "key": "one",
            "spec": "Do the work.",
            "check": "true",
        }
        self.assertEqual(2, TaskSpec.from_obj(base).max_attempts)
        self.assertEqual(
            3,
            TaskSpec.from_obj({**base, "max_attempts": 3}).max_attempts,
        )
        with self.assertRaisesRegex(ValueError, "max_attempts must be positive"):
            TaskSpec.from_obj({**base, "max_attempts": 0})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NewFieldTypeStrictnessTests(unittest.TestCase):
    """`max_attempts` and `redact_spec` reject truthy stand-ins.

    `bool("false")` is True and `int(1.5)` is 1 — both would change what the
    manifest author asked for without saying anything.
    """

    def _task(self, **extra: object) -> dict[str, object]:
        base: dict[str, object] = {
            "key": "t",
            "spec": "a self-contained spec long enough to pass validation " * 2,
            "check": "true",
        }
        base.update(extra)
        return base

    def test_fractional_max_attempts_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            TaskSpec.from_obj(self._task(max_attempts=1.5))
        self.assertIn("max_attempts must be an integer", str(caught.exception))

    def test_string_max_attempts_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TaskSpec.from_obj(self._task(max_attempts="2"))

    def test_string_redact_spec_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            TaskSpec.from_obj(self._task(redact_spec="false"))
        self.assertIn("redact_spec must be true or false", str(caught.exception))

    def test_real_values_still_work(self) -> None:
        task = TaskSpec.from_obj(self._task(max_attempts=1, redact_spec=True))
        self.assertEqual(1, task.max_attempts)
        self.assertTrue(task.redact_spec)

    def test_defaults_are_unchanged(self) -> None:
        task = TaskSpec.from_obj(self._task())
        self.assertEqual(2, task.max_attempts)
        self.assertFalse(task.redact_spec)


class AskBillingTests(unittest.TestCase):
    """Exercise ask and real runner admission with local worker/account fixtures."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='ask-billing-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.binary = self.root / 'fixture-worker'
        self.binary.write_text('#!' + sys.executable + '\n'
            'import json, pathlib, sys\n'
            'assert sys.argv[1] in ("exec", "run"), "no installed account probe"\n'
            'pathlib.Path(__file__).with_name("argv.json").write_text(json.dumps(sys.argv[1:]))\n'
            'pathlib.Path("answer.md").write_text("Fixture answer.\\n")\n'
            'print("RAW LOCAL FIXTURE")\n')
        self.binary.chmod(0o755)
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps(dict(monthly_api_cap_gbp=0, run_api_cap_gbp=0)))
        self.config = ringer.AppConfig(path=None, identity_default='fixture', state_dir=self.root/'state',
            require_model_assessment=True,
            dashboard_port_base=18787, hud_port=18788, hud_app_path=None, allow_full_access=False,
            eval=ringer.EvalConfig(backend='jsonl', jsonl_path=self.root/'attempts.jsonl'),
            engines={'codex': replace(ringer.built_in_codex_engine(), bin=str(self.binary),
                                      model_default='gpt-5.6-terra')},
            artifact=ringer.ArtifactConfig(enabled=False, out_template='', report_template='', index_out=self.root/'index'),
            billing_policy_path=self.policy)
        self.account = dict(observed_auth_mode='chatgpt', plan_type='prolite',
            observed_at=datetime.now(timezone.utc).isoformat(),
            rateLimitsByLimitId={'codex': {'primary': {'usedPercent': 10, 'windowDurationMins': 300,
                'resetsAt': int(datetime.now(timezone.utc).timestamp()) + 3600}, 'secondary': None}})

    def run_ask(self, *extra):
        args = ringer.build_parser().parse_args(['ask', 'What is the fixture answer?',
            '--workdir', str(self.root/'request'), '--identity', 'fixture', *extra])
        engine = self.config.engines.get(args.engine)
        model = args.model or (engine.model_default if engine else '')
        route = args.billing_route or ('subscription' if args.engine == 'codex' else None)
        if model and route:
            packet = ringer.build_context_packet(
                args.request, sources=[], state_files=[], max_packet_bytes=args.max_packet_bytes,
                max_file_bytes=args.max_file_bytes, max_files=args.max_files,
            )
            draft_manifest = ringer.one_request_manifest(
                packet=packet, workdir=args.workdir, engine=args.engine,
                timeout_s=args.timeout_s, reasoning_effort=args.reasoning_effort,
                model=model, redact=args.redact, billing_route=route,
                task_spend_allowance_gbp=args.task_spend_allowance_gbp,
            )
            assessment = ringer.assessment_draft_for_manifest(
                draft_manifest, self.config, coordinator='fixture'
            )
            assessment['strategy'] = 'Exercise the bounded synthetic ask route.'
            for row in assessment['tasks'].values():
                row['effort'] = row['effort'] or args.reasoning_effort
                row.update(
                    rationale='The fixture needs the selected route to exercise ask admission.',
                    alternative_considered='A pure parser test would not cover the runner boundary.',
                    context_plan='Use only the one bounded request packet.',
                    verification='Check answer, argv, state and admission evidence.',
                    escalation='Stop on missing route, authority or quota.',
                    evidence='The worker and account responses are local fixtures.',
                    uncertainty='No live provider quality or entitlement is implied.',
                )
            assessment_path = self.root / 'assessment.json'
            assessment_path.write_text(json.dumps(assessment))
            args.assessment = assessment_path
        with (mock.patch.object(ringer, 'ringer_home', return_value=self.root/'registry'),
              mock.patch.object(ringer, 'ensure_hud_running'),
              mock.patch.object(ringer.Dashboard, 'start', return_value=18787),
              mock.patch.object(ringer_billing, 'observe_codex_account',
                                new_callable=mock.AsyncMock, return_value=self.account) as account,
              mock.patch.object(ringer, 'admit', wraps=ringer_billing.admit) as admission,
              mock.patch.object(ringer, 'one_request_manifest', wraps=ringer.one_request_manifest) as builder,
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            result = ringer.run_one_request(self.config, args)
        self.account_probe = account
        self.admission = admission
        self.manifest_builder = builder
        return result

    def task_state(self):
        states = list((self.root/'state/runs').glob('*.json'))
        self.assertEqual(1, len(states))
        return json.loads(states[0].read_text())['tasks'][0]

    def test_codex_default_route_and_configured_model_reach_real_admission(self):
        self.assertEqual(0, self.run_ask())
        self.assertEqual('subscription', self.admission.call_args.kwargs['route'])
        self.assertEqual('gpt-5.6-terra', self.admission.call_args.kwargs['model'])
        self.assertIsNone(self.admission.call_args.kwargs['allowance'])
        self.assertEqual('ADMITTED', self.task_state()['billing_status'])
        self.assertEqual(1, self.account_probe.await_count)
        self.assertEqual('Fixture answer.\n', (self.root/'request/answer/answer.md').read_text())
        self.assertFalse((self.root/'state/cost-reservations.json').exists())

    def test_explicit_codex_model_is_respected(self):
        self.assertEqual(0, self.run_ask('--model', 'gpt-5.6-sol', '--billing-route', 'subscription'))
        self.assertEqual('gpt-5.6-sol', self.admission.call_args.kwargs['model'])
        argv = json.loads((self.root/'argv.json').read_text())
        self.assertEqual('gpt-5.6-sol', argv[argv.index('-m') + 1])
        self.assertIn('forced_login_method="chatgpt"', argv)
        self.assertIn('workspace-write', argv)

    def test_missing_codex_model_refuses_before_admission_or_worker(self):
        self.config.engines['codex'] = replace(self.config.engines['codex'], model_default='')
        with self.assertRaisesRegex(ValueError, '--model or engines.codex.model_default'):
            self.run_ask()
        self.assertFalse((self.root/'argv.json').exists())
        self.assertFalse((self.root/'state').exists())

    def test_billing_disabled_retains_missing_route_and_model_without_account_probe(self):
        self.config = replace(self.config, billing_policy_path=None)
        self.config.engines['codex'] = replace(self.config.engines['codex'], model_default='')
        with self.assertRaisesRegex(ValueError, 'ask requires --assessment'):
            self.run_ask()
        self.assertFalse((self.root/'argv.json').exists())

    def test_api_allowance_is_forwarded_but_zero_cap_still_blocks(self):
        self.account['observed_auth_mode'] = 'api_key'
        self.assertEqual(1, self.run_ask('--billing-route', 'api', '--task-spend-allowance-gbp', '0.25'))
        self.assertEqual('api', self.admission.call_args.kwargs['route'])
        self.assertEqual(.25, self.admission.call_args.kwargs['allowance'])
        self.assertEqual('BLOCKED', self.task_state()['billing_status'])
        self.assertIn('allowance unavailable', self.task_state()['billing_reason'])
        self.assertEqual(0, self.task_state()['attempts'])
        self.assertFalse((self.root/'argv.json').exists())

    def test_api_route_does_not_invent_an_allowance(self):
        self.account['observed_auth_mode'] = 'api_key'
        self.assertEqual(1, self.run_ask('--billing-route', 'api'))
        self.assertIsNone(self.admission.call_args.kwargs['allowance'])
        self.assertIn('explicit task spend allowance', self.task_state()['billing_reason'])
        self.assertFalse((self.root/'argv.json').exists())

    def test_default_subscription_never_falls_back_when_quota_is_exhausted(self):
        self.account['rateLimitsByLimitId']['codex']['primary']['usedPercent'] = 100
        self.assertEqual(1, self.run_ask('--task-spend-allowance-gbp', '0.25'))
        self.assertEqual(1, self.admission.call_count)
        self.assertEqual('subscription', self.task_state()['billing_route'])
        self.assertEqual('BLOCKED', self.task_state()['billing_status'])
        self.assertEqual(0, self.task_state()['attempts'])
        self.assertFalse((self.root/'argv.json').exists())

    def add_opencode(self):
        self.config.engines['opencode'] = ringer.EngineConfig(name='opencode', bin=str(self.binary),
            args_template=('run', '--model', '{model}', '{engine_args}', '{spec}'),
            full_access_args=(), sandbox_args=(), token_regex=None,
            model_default='openrouter/z-ai/glm-5.2')

    def test_other_engines_never_default_to_a_paid_route(self):
        self.add_opencode()
        with self.assertRaisesRegex(ValueError, 'explicit --billing-route'):
            self.run_ask('--engine', 'opencode')
        self.assertFalse((self.root/'argv.json').exists())
        self.assertFalse((self.root/'state').exists())

    def test_opencode_explicit_route_and_model_reach_real_admission(self):
        self.add_opencode()
        self.assertEqual(1, self.run_ask('--engine', 'opencode', '--billing-route', 'api',
            '--model', 'openrouter/z-ai/glm-5.3-flash', '--task-spend-allowance-gbp', '0.25'))
        self.assertEqual('openrouter/z-ai/glm-5.3-flash', self.admission.call_args.kwargs['model'])
        self.assertEqual('api', self.admission.call_args.kwargs['route'])
        self.account_probe.assert_not_awaited()
        self.assertIn('allowance unavailable', self.task_state()['billing_reason'])
        self.assertFalse((self.root/'argv.json').exists())

    def test_invalid_allowance_refuses_before_admission(self):
        for value in ('0', '-1', 'nan', 'inf', '-inf'):
            with self.subTest(value=value), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                with self.assertRaisesRegex(ValueError, 'finite positive'):
                    self.run_ask('--task-spend-allowance-gbp=' + value, '--workdir', str(Path(tmp)/'request'))
        self.assertFalse((self.root/'argv.json').exists())

    def test_existing_manifest_callers_keep_optional_billing_defaults(self):
        packet = ringer.build_context_packet('A local question.', sources=[], state_files=[],
            max_packet_bytes=16000, max_file_bytes=4000000, max_files=200)
        options = dict(packet=packet, workdir=self.root/'request', engine='codex',
                       timeout_s=30, reasoning_effort='low', model=None, redact=True)
        task = ringer.one_request_manifest(**options).tasks[0]
        self.assertIsNone(task.billing_route)
        self.assertIsNone(task.task_spend_allowance_gbp)
        self.assertEqual('', task.model)
        self.assertEqual(1, task.max_attempts)
        self.assertTrue(task.redact_spec)
        with self.assertRaisesRegex(ValueError, 'only by the codex engine'):
            ringer.one_request_manifest(**{**options, 'engine': 'opencode', 'model': 'some-model'})
