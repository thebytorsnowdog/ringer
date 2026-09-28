"""Offline independent replay, real manifest lint and real StateWriter schema tests."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from benchmarks.reliability.cases import CASES, case_digest
from benchmarks.reliability.checker import digest, evaluate, source_digest
from benchmarks.reliability.fixtures.references import REFERENCE, BAD
from benchmarks.reliability.reporting import build_report
import ringer

SCRIPT = ROOT / 'scripts/ringer_benchmark.py'
SPEC = importlib.util.spec_from_file_location('benchmark_cli', SCRIPT)
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def cli(*args):
    return subprocess.run([sys.executable, '-B', str(SCRIPT), *map(str, args)], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)


class BenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='benchmark-contract-')
        self.directory = Path(self.temp.name).resolve()
        self.addCleanup(self.temp.cleanup)

    def generate(self, cases='1,15', models='codex:gpt-5.6-luna'):
        out = self.directory / "bundle with spaces 'quotes' $literal"
        result = cli('generate', '--out', out, '--models', models, '--cases', cases,
                     '--max-parallel', '2', '--timeout-s', '90')
        self.assertEqual(0, result.returncode, result.stderr)
        return out, json.loads((out / 'manifest.json').read_text())

    def candidate(self, case_id, code):
        candidate = self.directory / (case_id + '.py')
        candidate.write_text(code)
        return candidate

    def test_all_twenty_reference_candidates_and_fixed_defects(self):
        self.assertEqual(20, len(CASES))
        self.assertEqual({'code-fix': 8, 'code-feature': 6, 'operational': 6},
                         {family: sum(case.task_type == family for case in CASES)
                          for family in ('code-fix', 'code-feature', 'operational')})
        for case in CASES:
            with self.subTest(case=case.case_id):
                self.assertIn(case.signature, case.spec)
                compile(case.starter, '<starter>', 'exec')
                compile(BAD[case.case_id], '<fixed defective variant>', 'exec')
                good = evaluate(case.case_id, self.candidate(case.case_id, REFERENCE[case.case_id]))
                self.assertEqual('PASS', good['resultstate'], json.dumps(good, indent=2))
                if case.case_id == 'submission-lock':
                    self.assertEqual(4, good['correctly_blocked_decisions'])
                self.assertGreater(good['correctness_checks']['total'], 0)
                self.assertGreater(good['boundary_checks']['total'], 0)
                self.assertEqual(good['checks']['total'], good['checks']['passed'])
                bad = evaluate(case.case_id, self.candidate(case.case_id, BAD[case.case_id]))
                self.assertEqual('FAIL', bad['resultstate'], case.case_id)
                self.assertTrue(any(not check['passed'] and not check.get('skipped') for check in bad['details']))
                self.assertFalse(any(check.get('exception') == 'NameError' for check in bad['details']), case.case_id)

    def test_starters_are_named_callable_defects_not_answers(self):
        for case in CASES:
            with self.subTest(case=case.case_id):
                result = evaluate(case.case_id, self.candidate(case.case_id, case.starter))
                self.assertEqual('FAIL', result['resultstate'])
                self.assertFalse(any(check.get('exception') == 'NameError' for check in result['details']))

    def test_normal_and_boundary_scores_are_distinct(self):
        candidate = self.candidate('no-default', BAD['no-default'])
        result = evaluate('no-default', candidate)
        self.assertEqual(1, result['correctness_score'])
        self.assertEqual(0, result['boundary_score'])

    def test_expected_blocked_decisions_are_separate_from_product(self):
        result = evaluate('capability-preflight', self.candidate('capability-preflight', REFERENCE['capability-preflight']))
        self.assertEqual('PASS', result['resultstate'])
        self.assertEqual(3, result['correctly_blocked_decisions'])
        self.assertEqual(0, result['completed_deliverables_from_blocked_decisions'])
        self.assertTrue(result['nohumanpromotion'])
        self.assertEqual('FAIL', evaluate('capability-preflight', self.candidate('capability-preflight', BAD['capability-preflight']))['resultstate'])

    def test_timeout_import_failure_and_stdout_junk_are_structured_failures(self):
        for code, failure in [('while True: pass\n', 'TIMEOUT'), ('raise RuntimeError("import crash")\n', 'ASSERTION'),
                              ('print("junk")\n' + REFERENCE['no-default'], 'OUTPUT_PROTOCOL')]:
            candidate = self.candidate('no-default', code)
            result = cli('check', '--case', 'no-default', '--candidate', candidate, '--timeout-s', '.15')
            self.assertEqual(1, result.returncode, result.stderr)
            score = json.loads(result.stdout)
            self.assertEqual('FAIL', score['resultstate'])
            self.assertEqual(failure, score['failure_class'])
            self.assertLess(score['boundary_score'], 1)

    def test_input_mutation_is_rejected(self):
        candidate = self.candidate('regression', 'def new_failures(current,baseline):\n current[:]=sorted(set(current)-set(baseline))\n return current\n')
        result = evaluate('regression', candidate)
        self.assertEqual('FAIL', result['resultstate'])
        self.assertTrue(any(check.get('input_preserved') is False for check in result['details']))

    def test_all_models_real_ringer_lint_and_matched_hashes(self):
        out, manifest = self.generate(cases=','.join(str(index) for index in range(1, 21)),
                                      models='codex:gpt-5.6-luna,codex:gpt-5.6-terra')
        self.assertEqual(40, len(manifest['tasks']))
        config_path = out / 'lint-config.toml'
        config_path.write_text(
            f'state_dir = {json.dumps(str(out / "state"))}\n'
            '[engines.codex]\n'
            'bin = "codex"\n'
            'args_template = ["exec", "{access_args}", "{model_args}", "{engine_args}", "{spec}"]\n'
            'sandbox_args = ["--sandbox", "workspace-write"]\n'
            'full_access_args = ["--dangerously-bypass-approvals-and-sandbox"]\n'
        )
        config = ringer.AppConfig.load(config_path)
        unassessed = ringer.Manifest.from_path(out / 'manifest.json')
        assessment = ringer.assessment_draft_for_manifest(
            unassessed, config, coordinator='benchmark-test'
        )
        assessment['strategy'] = 'Hold prompts, checks and effort constant across the matched model cells.'
        for row in assessment['tasks'].values():
            row.update(
                rationale='This model is one of the explicitly selected matched benchmark routes.',
                alternative_considered='A deterministic checker verifies output but cannot produce the candidate.',
                context_plan='Use only the bounded case specification and starter file.',
                verification='Run the pinned independent benchmark checker.',
                escalation='Stop on route, access, source-hash or checker failure.',
                evidence='This is a controlled comparison, not a production routing claim.',
                uncertainty='Comparative performance remains provisional until results are reviewed.',
            )
        manifest['model_assessment'] = assessment
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        parsed = ringer.Manifest.from_path(out / 'manifest.json')
        self.assertEqual(40, len(parsed.tasks))
        self.assertEqual(2, parsed.max_parallel)
        lint = subprocess.run([sys.executable, '-B', str(ROOT / 'ringer.py'), '--config', str(config_path), 'lint', str(out / 'manifest.json')],
                              cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(0, lint.returncode, lint.stdout + lint.stderr)
        for task in manifest['tasks']:
            self.assertEqual(1, task['max_attempts'])
            self.assertEqual(90, task['timeout_s'])
            for flag in ['forced_login_method="chatgpt"', 'service_tier="default"', 'model_reasoning_effort="medium"']:
                self.assertIn(flag, task['engine_args'])
            directory = Path(manifest['workdir']) / task['key']
            self.assertTrue(directory.is_relative_to(out))
            self.assertEqual({'solution.py', 'notes.md'}, {path.name for path in directory.iterdir()})
            self.assertNotIn('REFERENCE', task['spec'])
            self.assertTrue(task['source_sha256'])
        for index in range(20):
            self.assertEqual(manifest['tasks'][index]['benchmark'], manifest['tasks'][index + 20]['benchmark'])
            self.assertEqual(manifest['tasks'][index]['spec'], manifest['tasks'][index + 20]['spec'])

    def test_models_explicit_all_selectors_and_case_ids(self):
        result = cli('generate', '--out', self.directory / 'no-model')
        self.assertNotEqual(0, result.returncode)
        out, manifest = self.generate(cases='utc-order,20', models=','.join(CLI.MODELS))
        self.assertEqual(14, len(manifest['tasks']))
        self.assertEqual({'utc-order', 'cost-rate'}, {task['benchmark']['case_id'] for task in manifest['tasks']})
        self.assertTrue(all(not task['engine_args'] for task in manifest['tasks'] if task['engine'] == 'opencode'))

    def test_atomic_refusal_idempotency_and_output_containment(self):
        out, manifest = self.generate()
        tracked = {path: path.read_bytes() for path in out.rglob('*') if path.is_file()}
        self.generate()
        self.assertEqual(tracked, {path: path.read_bytes() for path in tracked})
        last = Path(manifest['workdir']) / manifest['tasks'][-1]['key'] / 'solution.py'
        last.write_text('user modification')
        result = cli('generate', '--out', out, '--models', 'codex:gpt-5.6-luna', '--cases', '1,15', '--max-parallel', '2', '--timeout-s', '90')
        self.assertNotEqual(0, result.returncode)
        self.assertEqual('user modification', last.read_text())
        for path, content in tracked.items():
            if path != last:
                self.assertEqual(content, path.read_bytes())
        (out / 'manifest.json').write_text('{"user":"modified"}')
        self.assertNotEqual(0, cli('generate', '--out', out, '--models', 'codex:gpt-5.6-luna').returncode)
        self.assertEqual('{"user":"modified"}', (out / 'manifest.json').read_text())
        link = self.directory / 'symlink'; link.symlink_to(self.directory / 'outside')
        self.assertNotEqual(0, cli('generate', '--out', link, '--models', 'codex:gpt-5.6-luna').returncode)
        self.assertNotEqual(0, cli('generate', '--out', 'relative', '--models', 'codex:gpt-5.6-luna').returncode)
        unrelated = self.directory / 'unrelated'; unrelated.mkdir(); (unrelated / 'keep').write_text('keep')
        self.assertNotEqual(0, cli('generate', '--out', unrelated, '--models', 'codex:gpt-5.6-luna').returncode)
        self.assertEqual('keep', (unrelated / 'keep').read_text())

    def test_no_partial_bundle_after_publish_error(self):
        root = self.directory / 'atomic'
        real_link = CLI.os.link
        def fail_second(source, destination):
            if Path(destination).name == 'manifest.json':
                raise OSError('simulated interruption')
            real_link(source, destination)
        with patch.object(CLI.os, 'link', side_effect=fail_second):
            with self.assertRaises(OSError):
                CLI.publish_files(root, {'solution.py': b'new', 'manifest.json': b'new'})
        self.assertFalse(any(path.is_file() for path in root.rglob('*')))

    def test_checker_pins_aggregate_sources_and_candidate_sha(self):
        candidate = self.candidate('no-default', REFERENCE['no-default'])
        expected = source_digest()
        result = cli('check', '--case', 'no-default', '--candidate', candidate, '--expected-source-sha256', '0' * 64)
        self.assertEqual(1, result.returncode)
        self.assertEqual('SOURCE_MISMATCH', json.loads(result.stdout)['failure_class'])
        result = cli('check', '--case', 'no-default', '--candidate', candidate, '--expected-source-sha256', expected)
        self.assertEqual(0, result.returncode, result.stderr)
        score = json.loads(result.stdout)
        self.assertEqual(digest(candidate), score['candidate_source_sha256'])
        # Do not mutate active source files; a separate byte copy proves source changes alter the aggregate.
        with patch('benchmarks.reliability.checker.source_files', return_value=[candidate]), patch('benchmarks.reliability.checker.ROOT', self.directory):
            before = source_digest()
            candidate.write_text(candidate.read_text() + '# changed\n')
            self.assertNotEqual(before, source_digest())

    def fixture_run(self, out, manifest, task_index=0, case_source=None, header_model=None, run_suffix='test', status='pass', legacy_log=False):
        """Build the fixture using Ringer's actual runtime and StateWriter, not an invented schema."""
        task_obj = manifest['tasks'][task_index]
        task = ringer.TaskSpec.from_obj(task_obj)
        directory = Path(manifest['workdir']) / task.key
        case_id = task_obj['benchmark']['case_id']
        (directory / 'solution.py').write_text(case_source or REFERENCE[case_id])
        (directory / 'notes.md').write_text('Local implementation and limitations reviewed by coordinator.\n')
        log = directory / 'worker.log'
        start = '2026-09-03T09:00:01+00:00'
        text = f'[ringer.py] attempt 1 started {start}\n[ringer.py] engine: codex\n'
        if legacy_log:
            text += '[ringer.py] command: ' + ringer.shell_command_for_display(
                ['codex', 'exec', '-m', task.model, task.spec]) + ' < /dev/null\n'
            text += 'WARNING: proceeding, even though we could not update PATH: Operation not permitted (os error 1)\n'
        if header_model:
            text += f'OpenAI Codex v1.0\n--------\nworkdir: {directory}\nmodel: {header_model}\n--------\n'
        else:
            text += 'reported_model: fabricated-model\nThe model is fake.\n'
        text += '[ringer.py] attempt 1 exited rc=0\n'
        log.write_text(text)
        # Execute the actual generated command, with real shell quoting, but never the worker/model command.
        process = subprocess.run(task.check, shell=True, cwd=directory, capture_output=True, text=True, timeout=60)
        if not legacy_log:
            with log.open('a') as stream:
                stream.write(process.stdout)
        runtime = ringer.TaskRuntime(task, directory, log)
        runtime.attempts = 1
        runtime.status = status
        runtime.final_verdict = 'PASS' if status == 'pass' else 'INTERRUPTED'
        runtime.last_check_returncode = process.returncode
        runtime.last_check_output = process.stdout
        runtime.failure_class = None if status == 'pass' else 'interrupted'
        state_dir = out / 'state'
        writer = ringer.StateWriter('ringer-model-comparison-' + run_suffix, manifest['run_name'], 'offline-test',
                                    state_dir, {}, datetime(2026, 9, 3, 9, tzinfo=timezone.utc), [runtime], threading.RLock(),
                                    job_id=manifest['job_id'])
        with patch.object(ringer.ProcessTree, 'read', return_value=({}, {})):
            state = writer.snapshot()
        state['finished'] = True
        state['state'] = 'finished'
        (state_dir / 'runs').mkdir(parents=True, exist_ok=True)
        state_path = state_dir / 'runs' / (state['run_id'] + '.json')
        state_path.write_text(json.dumps(state))
        return state_dir, state_path, state, runtime, json.loads(process.stdout)

    def test_generated_routes_do_not_invent_paid_allowances(self):
        out, manifest = self.generate(cases='1', models=','.join(CLI.MODELS))
        for task in manifest['tasks']:
            self.assertEqual('subscription' if task['engine'] == 'codex' else 'api', task.get('billing_route'))
            self.assertNotIn('task_spend_allowance_gbp', task)

    def test_gemini_selector_is_a_bounded_paid_api_cell(self):
        selector = 'opencode:openrouter/google/gemini-3.8-flash'
        out = self.directory / 'gemini-with-allowance'
        result = cli('generate', '--out', out, '--models', selector, '--cases', '1',
                     '--task-spend-allowance-gbp', '0.25')
        self.assertEqual(0, result.returncode, result.stderr)
        task = ringer.Manifest.from_path(out / 'manifest.json').tasks[0]
        self.assertEqual('openrouter/google/gemini-3.8-flash', task.model)
        self.assertEqual('opencode', task.engine)
        self.assertEqual('api', task.billing_route)
        self.assertEqual(1, task.max_attempts)
        self.assertEqual(.25, task.task_spend_allowance_gbp)

        without_allowance = self.directory / 'gemini-without-allowance'
        result = cli('generate', '--out', without_allowance, '--models', selector, '--cases', '1')
        self.assertEqual(0, result.returncode, result.stderr)
        task = ringer.Manifest.from_path(without_allowance / 'manifest.json').tasks[0]
        self.assertIsNone(task.task_spend_allowance_gbp)
        self.assertIn('Paid API cells still require an explicit --task-spend-allowance-gbp', result.stderr)

    def test_explicit_paid_allowance_is_positive_finite_and_scoped_to_api_cells(self):
        for value in ('0', '-1', 'nan', 'inf', '-inf', 'no'):
            out = self.directory / ('bad-allowance-' + value)
            result = cli('generate', '--out', out, '--models', ','.join(CLI.MODELS),
                         '--cases', '1', '--task-spend-allowance-gbp=' + value)
            self.assertEqual(2, result.returncode)
            self.assertFalse(out.exists())
        out = self.directory / 'explicit-allowance'
        result = cli('generate', '--out', out, '--models', ','.join(CLI.MODELS),
                     '--cases', '1', '--task-spend-allowance-gbp', '0.25')
        self.assertEqual(0, result.returncode, result.stderr)
        tasks = ringer.Manifest.from_path(out / 'manifest.json').tasks
        for task in tasks:
            self.assertEqual(.25 if task.engine == 'opencode' else None, task.task_spend_allowance_gbp)

    def test_legacy_worker_log_uses_independently_captured_state_check_pointer(self):
        out, manifest = self.generate(cases='1')
        state_dir, path, state, runtime, pointer = self.fixture_run(
            out, manifest, legacy_log=True, header_model='gpt-5.6-luna')
        self.assertIn(manifest['tasks'][0]['spec'], runtime.log_path.read_text())
        self.assertNotIn('benchmark_score_path', runtime.log_path.read_text())
        self.assertIn('benchmark_score_path', state['tasks'][0]['check_output_tail'])
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('PASS', row['resultstate'])
        self.assertTrue(row['quality_pass'])
        self.assertEqual(0, row['first_try']['worker_returncode'])
        # Unrecognised pre-header text is not permission to search the worker body.
        self.assertEqual('DECLARED', row['identity_status'])

    def test_legacy_state_pointer_retains_exit_check_and_ambiguity_gates(self):
        out, manifest = self.generate(cases='1')
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest, legacy_log=True)
        original_log = runtime.log_path.read_text()
        original_task = deepcopy(state['tasks'][0])
        cases = [
            ('nonzero worker', original_log.replace('exited rc=0', 'exited rc=7'), {}, 'FAIL'),
            ('missing exit', original_log.replace('[ringer.py] attempt 1 exited rc=0\n', ''), {}, 'FAIL'),
            ('check failure', original_log, {'check_returncode': 1}, 'FAIL'),
            ('check timeout', original_log, {'check_timed_out': True}, 'FAIL'),
            ('state interrupted', original_log, {'status': 'interrupted', 'verdict': 'INTERRUPTED'}, 'FAIL'),
            ('duplicate attempt', original_log + original_log, {}, 'AMBIGUOUS_CHECK_EVIDENCE'),
            ('duplicate state pointer', original_log, {'check_output_tail': runtime.last_check_output * 2}, 'AMBIGUOUS_CHECK_EVIDENCE'),
            ('orphan index', original_log, {'attempts': 0}, 'ORPHAN_ATTEMPT'),
            ('wrong check', original_log, {'check': 'echo different'}, 'CHECK_COMMAND_MISMATCH'),
            ('wrong spec', original_log, {'spec': 'different'}, 'STATE_SPEC_OR_SOURCE_MISMATCH'),
            ('wrong source', original_log, {'source_sha256': {}}, 'STATE_SPEC_OR_SOURCE_MISMATCH'),
        ]
        for name, log, changes, expected in cases:
            with self.subTest(case=name):
                runtime.log_path.write_text(log)
                state['tasks'][0] = {**original_task, **changes}
                path.write_text(json.dumps(state))
                row = build_report(out / 'manifest.json', state_dir)['cells'][0]
                self.assertEqual(expected, row['resultstate'])
                self.assertFalse(row['quality_pass'])

    def test_legacy_pointer_is_bound_to_exact_logged_execution(self):
        out, manifest = self.generate(cases='1')
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest, legacy_log=True)
        score_path = Path(pointer['benchmark_score_path'])
        original = json.loads(score_path.read_text())
        for field, wrong in [('attempt_index', 2), ('taskdir', '/wrong'),
                             ('started_at', '2026-09-03T09:00:02+00:00')]:
            with self.subTest(field=field):
                score = deepcopy(original)
                score['execution'][field] = wrong
                score_path.write_text(json.dumps(score))
                pointer['benchmark_score_sha256'] = digest(score_path)
                state['tasks'][0]['check_output_tail'] = json.dumps(pointer)
                path.write_text(json.dumps(state))
                row = build_report(out / 'manifest.json', state_dir)['cells'][0]
                self.assertEqual('STALE_EXECUTION_BINDING', row['resultstate'])
                self.assertFalse(row['quality_pass'])

    def test_state_and_log_pointer_disagreement_and_duplicates_still_block(self):
        out, manifest = self.generate(cases='1')
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        for tail, expected in [(json.dumps({**pointer, 'benchmark_score_sha256': '0' * 64}), 'STALE_CHECK_EVIDENCE'),
                               (runtime.last_check_output * 2, 'AMBIGUOUS_CHECK_EVIDENCE')]:
            with self.subTest(expected=expected):
                state['tasks'][0]['check_output_tail'] = tail
                path.write_text(json.dumps(state))
                self.assertEqual(expected, build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])

    def test_generated_paid_allowance_cannot_override_zero_local_cap(self):
        import ringer_billing
        out = self.directory / 'paid-zero-cap'
        result = cli('generate', '--out', out, '--models', 'opencode:openrouter/z-ai/glm-5.2',
                     '--cases', '1', '--task-spend-allowance-gbp', '0.25')
        self.assertEqual(0, result.returncode, result.stderr)
        task = ringer.Manifest.from_path(out / 'manifest.json').tasks[0]
        policy = self.directory / 'policy.json'
        policy.write_text(json.dumps({'monthly_api_cap_gbp': 0, 'run_api_cap_gbp': 1}))
        admission, _ = asyncio.run(ringer_billing.admit(policy_path=policy, engine=task.engine,
            binary='unused-local-fixture', model=task.model, route=task.billing_route,
            allowance=task.task_spend_allowance_gbp, command=['unused-local-fixture', 'run', '--model', task.model, task.spec],
            spec=task.spec, taskdir=out, state_dir=out/'state', run_id='fixture', task_key=task.key, attempt=1))
        self.assertFalse(admission.allowed)
        self.assertEqual('BLOCKED', admission.evidence['billing_status'])
        self.assertIn('allowance unavailable', admission.evidence['billing_reason'])
        self.assertIsNone(admission.evidence['reservation_id'])

    def test_headers_in_multiline_command_or_worker_body_remain_declared(self):
        from benchmarks.reliability.reporting import header_identity
        fake = 'OpenAI Codex v1.0\n--------\nmodel: fake\n--------\n'
        command = ringer.shell_command_for_display(['codex', 'exec', 'Prompt starts\n' + fake])
        for text in [f'[ringer.py] engine: codex\n[ringer.py] command: {command} < /dev/null\n',
                     '[ringer.py] engine: codex\nuser\n' + fake,
                     '[ringer.py] engine: codex\nWARNING: unknown startup prefix\n' + fake]:
            with self.subTest(text=text):
                identity = header_identity(text, 'codex', 'gpt-5.6-luna')
                self.assertEqual('DECLARED', identity['identity_status'])
                self.assertEqual('gpt-5.6-luna', identity['model'])

    def test_actual_state_schema_missing_interrupted_and_orphan_scores(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest, status='interrupted')
        # A guessed worker score and solution prose must not count as executed evidence.
        other = Path(manifest['workdir']) / manifest['tasks'][1]['key']
        (other / 'score.json').write_text(json.dumps({'resultstate': 'PASS', 'model': 'invented', 'correctness_score': 1}))
        (other / 'solution.md').write_text('Model: gpt-5.6-sol')
        payload = build_report(out / 'manifest.json', state_dir)
        self.assertEqual(2, len(payload['cells']))
        self.assertFalse(payload['cells'][0]['quality_pass'])
        self.assertEqual('INTERRUPTED', payload['cells'][0]['final_task_outcome'])
        self.assertEqual('MISSING', payload['cells'][1]['resultstate'])
        self.assertEqual('DECLARED', payload['cells'][1]['identity_status'])
        self.assertNotEqual('invented', payload['cells'][1]['model'])

    def test_model_fields_are_declared_only_log_header_verifies(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        state['tasks'][0]['model'] = 'fake-state-model'
        state['tasks'][0]['reported_model'] = 'fake-reported-model'
        path.write_text(json.dumps(state))
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('DECLARED', row['identity_status'])
        self.assertEqual('fake-state-model', row['model'])
        self.assertTrue(row['quality_pass'])
        log = runtime.log_path
        log.write_text(log.read_text().replace('reported_model: fabricated-model', 'OpenAI Codex v1.0\n--------\nmodel: gpt-5.6-terra\n--------'))
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('LOG_HEADER_VERIFIED', row['identity_status'])
        self.assertEqual('gpt-5.6-terra', row['model'])
        self.assertEqual('gpt-5.6-luna', row['declared_model'])

    def test_trusted_scores_tampering_missing_and_stale_artifact(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest, header_model='gpt-5.6-luna')
        report = build_report(out / 'manifest.json', state_dir)
        self.assertTrue(report['cells'][0]['quality_pass'])
        score_path = Path(pointer['benchmark_score_path'])
        original = score_path.read_bytes()
        score_path.write_text('{}')
        self.assertEqual('SCORE_DIGEST_MISMATCH', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])
        score_path.write_bytes(original)
        score_path.unlink()
        self.assertEqual('MISSING_OR_INVALID_SCORE', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])
        score_path.write_bytes(original)
        (runtime.taskdir / 'solution.py').write_text('# stale candidate\n')
        self.assertEqual('STALE_CANDIDATE', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])

    def test_wrong_source_spec_and_execution_binding_rejected(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        score_path = Path(pointer['benchmark_score_path'])
        score = json.loads(score_path.read_text())
        score['execution']['attempt_index'] = 2
        score_path.write_text(json.dumps(score))
        pointer['benchmark_score_sha256'] = digest(score_path)
        lines = runtime.log_path.read_text().splitlines()
        lines[-1] = json.dumps(pointer)
        runtime.log_path.write_text('\n'.join(lines) + '\n')
        state['tasks'][0]['check_output_tail'] = json.dumps(pointer)
        path.write_text(json.dumps(state))
        self.assertEqual('STALE_EXECUTION_BINDING', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])
        manifest['benchmark']['checker_source_sha256'] = 'tampered'
        (out / 'manifest.json').write_text(json.dumps(manifest))
        self.assertEqual('SOURCE_OR_SPEC_MISMATCH', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])

    def test_first_and_final_attempts_preserved_and_blocked_counted(self):
        out, manifest = self.generate(cases='15')
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest, case_source=BAD['capability-preflight'])
        first_stdout = runtime.last_check_output
        (runtime.taskdir / 'solution.py').write_text(REFERENCE['capability-preflight'])
        with runtime.log_path.open('a') as stream:
            stream.write('[ringer.py] attempt 2 started 2026-09-03T09:01:00+00:00\n[ringer.py] engine: codex\n[ringer.py] attempt 2 exited rc=0\n')
        second = subprocess.run(runtime.task.check, shell=True, cwd=runtime.taskdir, capture_output=True, text=True, timeout=60)
        self.assertEqual(0, second.returncode, second.stderr)
        with runtime.log_path.open('a') as stream:
            stream.write(second.stdout)
        state['tasks'][0].update(attempts=2, check_returncode=0, check_output_tail=second.stdout)
        path.write_text(json.dumps(state))
        payload = build_report(out / 'manifest.json', state_dir)
        row = payload['cells'][0]
        self.assertEqual('FAIL', row['first_try']['outcome'])
        self.assertEqual('PASS', row['resultstate'])
        self.assertEqual(2, len(row['runs'][0]['attempts']))
        self.assertEqual(3, payload['summary']['correctly_blocked_decisions'])
        self.assertEqual(0, payload['summary']['completed_deliverables_from_blocked_decisions'])

    def test_unique_run_task_matching_and_invalid_state_preserved_as_issue(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        state['tasks'][0]['taskdir'] = '/wrong/place'
        path.write_text(json.dumps(state))
        (state_dir / 'runs/broken.json').write_text('{interrupted write')
        payload = build_report(out / 'manifest.json', state_dir)
        self.assertTrue(all(row['resultstate'] == 'MISSING' for row in payload['cells']))
        self.assertEqual('UNREADABLE_STATE', payload['issues'][0]['issue'])

    def test_report_markdown_json_and_atomic_no_overwrite(self):
        out, manifest = self.generate()
        state_dir, *_ = self.fixture_run(out, manifest)
        target = out / 'report.json'
        run = cli('report', '--manifest', out / 'manifest.json', '--state-dir', state_dir, '--out', target)
        self.assertEqual(0, run.returncode, run.stderr)
        self.assertTrue(target.with_suffix('.md').is_file())
        data = json.loads(target.read_text())
        self.assertEqual([], data['historical_sources'])
        self.assertEqual(2, len(data['cells']))
        before = target.read_bytes()
        target.with_suffix('.md').write_text('human edits')
        self.assertNotEqual(0, cli('report', '--manifest', out / 'manifest.json', '--state-dir', state_dir, '--out', target).returncode)
        self.assertEqual(before, target.read_bytes())
        self.assertEqual('human edits', target.with_suffix('.md').read_text())


    def test_checker_source_and_reference_tamper_on_separate_copy(self):
        from benchmarks.reliability.checker import source_files
        copied = self.directory / 'checker-copy'
        for source in source_files():
            target = copied / source.relative_to(ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
        candidate = self.candidate('no-default', REFERENCE['no-default'])
        pinned = source_digest()
        for relative in ['benchmarks/reliability/checker.py', 'benchmarks/reliability/cases.py',
                         'benchmarks/reliability/fixtures/references.py']:
            path = copied / relative
            original = path.read_bytes()
            path.write_bytes(original + b'\n# tamper fixture\n')
            run = subprocess.run([sys.executable, '-B', str(copied / 'scripts/ringer_benchmark.py'), 'check',
                                  '--case', 'no-default', '--candidate', str(candidate),
                                  '--expected-source-sha256', pinned], capture_output=True, text=True, timeout=10)
            self.assertEqual(1, run.returncode, run.stderr)
            self.assertEqual('SOURCE_MISMATCH', json.loads(run.stdout)['failure_class'])
            path.write_bytes(original)

    def test_no_file_side_effects_and_argv_shell_boundary(self):
        code = 'from pathlib import Path\n' + REFERENCE['regression'].replace(
            '    return ', "    Path('unlisted').write_text('bad')\n    return ")
        self.assertEqual('FAIL', evaluate('regression', self.candidate('regression', code))['resultstate'])
        code = REFERENCE['argv-literals'].replace('shell=False', 'shell=True')
        self.assertEqual('FAIL', evaluate('argv-literals', self.candidate('argv-literals', code))['resultstate'])

    def test_unexecuted_state_pass_and_check_timeout_cannot_pass_quality(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        state['tasks'][0].update(attempts=0, check_output_tail='', check_returncode=None)
        runtime.log_path.write_text('')
        path.write_text(json.dumps(state))
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('NO_CHECK_EVIDENCE', row['resultstate'])
        self.assertFalse(row['quality_pass'])
        state['tasks'][0].update(attempts=1, check_output_tail=json.dumps(pointer), check_returncode=0, check_timed_out=True)
        path.write_text(json.dumps(state))
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertFalse(row['quality_pass'])

    def test_scores_never_override_identity_or_promote(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        score_path = Path(pointer['benchmark_score_path'])
        score = json.loads(score_path.read_text())
        score.update(model='invented', actual_model='invented', identity_status='VERIFIED')
        def update_evidence():
            score_path.write_text(json.dumps(score))
            pointer['benchmark_score_sha256'] = digest(score_path)
            lines = runtime.log_path.read_text().splitlines()
            lines[-1] = json.dumps(pointer)
            runtime.log_path.write_text('\n'.join(lines) + '\n')
            state['tasks'][0]['check_output_tail'] = json.dumps(pointer)
            path.write_text(json.dumps(state))
        update_evidence()
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('gpt-5.6-luna', row['model'])
        self.assertEqual('DECLARED', row['identity_status'])
        score['resultstate'] = 'READY'
        update_evidence()
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('INVALID_SCORE', row['resultstate'])
        self.assertFalse(row['quality_pass'])

    def test_repeated_run_ids_and_old_execution_evidence(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        second_state = deepcopy(state)
        second_state['run_id'] += '-repeat'
        second_state['started_at'] = '2026-09-03T10:00:00+00:00'
        second_state['tasks'][0].update(status='interrupted', verdict='INTERRUPTED', attempts=1,
                                        check_returncode=None, check_output_tail='')
        second_path = path.with_name(second_state['run_id'] + '.json')
        second_path.write_text(json.dumps(second_state))
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual(2, len(row['runs']))
        self.assertNotEqual(row['runs'][0]['run_id'], row['runs'][1]['run_id'])
        self.assertEqual('PASS', row['first_try']['outcome'])
        self.assertEqual('INTERRUPTED', row['final_task_outcome'])
        self.assertFalse(row['quality_pass'])
        # Reusing a previous score in a later run's state is stale, even for identical source bytes.
        second_state['tasks'][0]['check_output_tail'] = json.dumps(pointer)
        second_path.write_text(json.dumps(second_state))
        self.assertEqual('STALE_EXECUTION_BINDING', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])

    def test_historical_results_only_explicit_and_separate(self):
        out, manifest = self.generate()
        state_dir = out / 'state'; state_dir.mkdir()
        historical = self.directory / 'old-audit.json'
        historical.write_text('{"model":"historical-only","result":"PASS"}')
        payload = build_report(out / 'manifest.json', state_dir, [str(historical)])
        self.assertEqual(1, len(payload['historical_sources']))
        self.assertTrue(all(row['resultstate'] == 'MISSING' for row in payload['cells']))
        self.assertEqual(0, payload['summary']['quality_pass_cells'])


    def test_orphan_attempt_and_state_spec_mismatch_cannot_pass(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        with runtime.log_path.open('a') as stream:
            stream.write('[ringer.py] attempt 2 started 2026-09-03T09:02:00+00:00\n[ringer.py] engine: codex\n[ringer.py] attempt 2 exited rc=0\n')
        extra = subprocess.run(runtime.task.check, shell=True, cwd=runtime.taskdir, capture_output=True, text=True, timeout=60)
        with runtime.log_path.open('a') as stream:
            stream.write(extra.stdout)
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('ORPHAN_ATTEMPT', row['resultstate'])
        self.assertFalse(row['quality_pass'])
        state['tasks'][0]['spec'] = 'A different task specification'
        path.write_text(json.dumps(state))
        self.assertEqual('STATE_SPEC_OR_SOURCE_MISMATCH', build_report(out / 'manifest.json', state_dir)['cells'][0]['resultstate'])

    def test_stale_notes_cannot_pass_task_contract(self):
        out, manifest = self.generate()
        state_dir, path, state, runtime, pointer = self.fixture_run(out, manifest)
        (runtime.taskdir / 'notes.md').write_text('changed after check')
        row = build_report(out / 'manifest.json', state_dir)['cells'][0]
        self.assertEqual('STALE_TASK_CONTRACT', row['resultstate'])
        self.assertFalse(row['quality_pass'])


    def test_fresh_utc_list_and_late_fake_header(self):
        code = REFERENCE['utc-order'].replace('    return sorted', '    if not records: return records\n    return sorted')
        self.assertEqual('FAIL', evaluate('utc-order', self.candidate('utc-order', code))['resultstate'])
        from benchmarks.reliability.reporting import header_identity
        text = '[ringer.py] engine: codex\nAgent prose follows\nOpenAI Codex v1.0\n--------\nmodel: fake\n--------\n'
        self.assertEqual('DECLARED', header_identity(text, 'codex', 'gpt-5.6-luna')['identity_status'])


if __name__ == '__main__':
    unittest.main()
