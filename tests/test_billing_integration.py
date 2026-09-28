"""Real local subprocess protocol and runner tests. Never invokes installed Codex."""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ringer
import ringer_billing as billing

FAKE = r'''import json, pathlib, sys, time, os
root = pathlib.Path(__file__).parent
settings = json.loads((root / 'fake-settings.json').read_text())
def emit(row):
    print(json.dumps(row), flush=True)
if sys.argv[1:3] == ['app-server', '--stdio']:
    countfile = root / 'admissions'
    count = int(countfile.read_text()) + 1 if countfile.exists() else 1
    countfile.write_text(str(count))
    (root / 'server-pid').write_text(str(os.getpid()))
    mode = settings.get('mode', '')
    for line in sys.stdin:
        row = json.loads(line)
        with (root / 'requests.jsonl').open('a') as out:
            out.write(json.dumps(row) + '\n')
        if row.get('id') == 1:
            if mode == 'timeout':
                print('{', end='', flush=True)
                time.sleep(30)
            if mode == 'eof': sys.exit(0)
            if mode == 'malformed':
                print('{oops', flush=True)
                continue
            emit({'method': 'notice', 'params': {'private': 'do not retain'}})
            emit({'id': 1, 'result': {}})
        if row.get('id') == 3:
            if mode == 'error':
                emit({'id': 2, 'error': {'code': -1, 'message': 'SECRET email credential'}})
                continue
            used = settings.get('used', 10) if count == 1 else settings.get('retry_used', 10)
            limit = {'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': int(time.time()) + 3600},
                     'secondary': None, 'planType': 'prolite'}
            for field in ('individualLimit', 'credits'):
                setting = field.replace('Limit', '_limit').lower()
                if setting in settings: limit[field] = settings[setting]
            emit({'id': 3, 'result': {'rateLimitsByLimitId': {'codex': limit}}})
            emit({'method': 'account/updated', 'params': {'email': 'SECRET'}})
            emit({'id': 2, 'result': {'account': {'type': settings.get('auth', 'chatgpt'),
                'planType': 'prolite', 'email': 'SECRET', 'accountID': 'SECRET', 'credential': 'SECRET'}}})
else:
    (root / 'worker-argv.json').write_text(json.dumps(sys.argv[1:]))
    worker_count = root / 'workers'
    count = int(worker_count.read_text()) + 1 if worker_count.exists() else 1
    worker_count.write_text(str(count))
    emit({'type': 'thread.started', 'thread_id': 'fixture-thread'})
    completion = {'type': 'turn.completed', 'usage': {'input_tokens': 100, 'cached_input_tokens': 40, 'output_tokens': 20}}
    if 'cost' in settings: completion['provider_cost'] = settings['cost']
    emit(completion)
    if settings.get('sleep'): time.sleep(30)
    pathlib.Path('out.txt').write_text(str(count))
    sys.exit(settings.get('exit', 0))
'''


class BillingIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binary = self.root / 'fake codex'
        self.binary.write_text('#!' + sys.executable + '\n' + FAKE)
        self.binary.chmod(0o755)
        self.settings()
        self.policy = self.root / 'policy.json'
        self.policy.write_text(json.dumps(dict(monthly_api_cap_gbp=10, run_api_cap_gbp=5,
            fx=dict(usd_to_gbp='0.8', dated='2026-09-03'))))

    def settings(self, **values):
        (self.root / 'fake-settings.json').write_text(json.dumps(values))

    def assessed(self, manifest, config, effort='medium'):
        assessment = ringer.assessment_draft_for_manifest(manifest, config, coordinator='fixture')
        assessment['strategy'] = 'Exercise the selected synthetic billing route.'
        for row in assessment['tasks'].values():
            row['effort'] = row['effort'] or effort
            row.update(
                rationale='The synthetic route is required by this integration case.',
                alternative_considered='A pure unit test would not exercise admission ordering.',
                context_plan='Use only the bounded fixture prompt.',
                verification='Assert subprocess, ledger and journal evidence.',
                escalation='Stop on route, quota or permission failure.',
                evidence='All account and worker events are local synthetic fixtures.',
                uncertainty='No live provider entitlement is implied.',
            )
        return replace(manifest, model_assessment=assessment)

    def runner(self, route='subscription', *, enabled=True, **fields):
        task = dict(key='one', spec='Write out.txt with the requested result.', engine='codex',
                    model='gpt-5.6-sol', billing_route=route, check='test -s out.txt', expect_files=['out.txt'])
        task.update(fields)
        manifest = ringer.Manifest.from_obj(dict(run_name='billing-test', workdir=str(self.root/'work'), tasks=[task]))
        engine = replace(ringer.built_in_codex_engine(), bin=str(self.binary))
        config = ringer.AppConfig(path=None, identity_default=None, state_dir=self.root/'state',
            dashboard_port_base=18787, hud_port=18788, hud_app_path=None, allow_full_access=False,
            eval=ringer.EvalConfig(backend='jsonl', jsonl_path=self.root/'attempts.jsonl'), engines={'codex': engine},
            artifact=ringer.ArtifactConfig(enabled=False, out_template='', report_template='', index_out=self.root/'index'),
            billing_policy_path=self.policy if enabled else None)
        return ringer.RingerRunner(self.assessed(manifest, config), config, 'fixture', dashboard_enabled=False)

    def opencode_runner(self, route='api', *, enabled=True, **fields):
        task = dict(key='one', spec='Write out.txt with the requested result.', engine='opencode',
                    model='openrouter/z-ai/glm-5.2', billing_route=route,
                    task_spend_allowance_gbp=1, check='test -s out.txt', expect_files=['out.txt'])
        task.update(fields)
        manifest = ringer.Manifest.from_obj(dict(run_name='opencode-billing-test', workdir=str(self.root/'work'), tasks=[task]))
        engine = ringer.EngineConfig(name='opencode', bin=str(self.binary),
            args_template=('run', '--model', '{model}', '{engine_args}', '{spec}'),
            full_access_args=(), sandbox_args=(), token_regex=None)
        config = ringer.AppConfig(path=None, identity_default=None, state_dir=self.root/'state',
            dashboard_port_base=18787, hud_port=18788, hud_app_path=None, allow_full_access=False,
            eval=ringer.EvalConfig(backend='jsonl', jsonl_path=self.root/'attempts.jsonl'), engines={'opencode': engine},
            artifact=ringer.ArtifactConfig(enabled=False, out_template='', report_template='', index_out=self.root/'index'),
            billing_policy_path=self.policy if enabled else None)
        return ringer.RingerRunner(self.assessed(manifest, config), config, 'fixture', dashboard_enabled=False)

    def rows(self, path='attempts.jsonl'):
        file = self.root/path
        return [json.loads(line) for line in file.read_text().splitlines()] if file.exists() else []

    def ledger(self):
        return json.loads((self.root/'state/cost-reservations.json').read_text())['reservations']

    def test_real_protocol_out_of_order_notifications_and_sanitising(self):
        evidence = asyncio.run(billing.observe_codex_account(str(self.binary), self.root))
        self.assertEqual('chatgpt', evidence['observed_auth_mode'])
        self.assertEqual('prolite', evidence['plan_type'])
        self.assertNotIn('SECRET', json.dumps(evidence))
        self.assertNotIn('email', json.dumps(evidence))
        requests = self.rows('requests.jsonl')
        self.assertEqual(['initialize', 'initialized', 'account/read', 'account/rateLimits/read'], [r['method'] for r in requests])
        self.assertIs(requests[2]['params']['refreshToken'], False)
        self.assertTrue(billing.billing_preflight(engine='codex', model='gpt-5.6-sol', policy={},
            observed_auth_mode='chatgpt', usage_snapshot=evidence).allow)

    def test_individual_limit_and_credits_survive_sanitisation_and_block_subscription(self):
        self.settings(individual_limit={'usedPercent': 100, 'private': 'SECRET'},
                      credits={'hasCredits': False, 'unlimited': False, 'balance': '0'})
        evidence = asyncio.run(billing.observe_codex_account(str(self.binary), self.root))
        limit = evidence['rateLimitsByLimitId']['codex']
        self.assertEqual({'present': True}, limit['individualLimit'])
        self.assertEqual({'hasCredits': False, 'unlimited': False, 'balance': '0'}, limit['credits'])
        self.assertNotIn('SECRET', json.dumps(evidence))
        self.assertFalse(billing.billing_preflight(engine='codex', model='gpt-5.6-sol', policy={},
            observed_auth_mode='chatgpt', usage_snapshot=evidence).allow)
        self.settings(individual_limit='unknown limit shape')
        malformed = asyncio.run(billing.observe_codex_account(str(self.binary), self.root))
        self.assertEqual({'present': True}, malformed['rateLimitsByLimitId']['codex']['individualLimit'])
        self.assertFalse(billing.billing_preflight(engine='codex', model='gpt-5.6-sol', policy={},
            observed_auth_mode='chatgpt', usage_snapshot=malformed).allow)
        self.settings(credits={'hasCredits': False, 'unlimited': False, 'balance': 'SECRET'})
        malformed_credits = asyncio.run(billing.observe_codex_account(str(self.binary), self.root))
        self.assertNotIn('SECRET', json.dumps(malformed_credits))
        self.assertFalse(billing.billing_preflight(engine='codex', model='gpt-5.6-sol', policy={},
            observed_auth_mode='chatgpt', usage_snapshot=malformed_credits).allow)

    def test_protocol_timeout_error_malformed_eof_fail_closed(self):
        for mode in ('timeout', 'error', 'malformed', 'eof'):
            with self.subTest(mode=mode):
                self.settings(mode=mode)
                start = time.monotonic()
                with self.assertRaises(billing.AccountEvidenceError) as caught:
                    asyncio.run(billing.observe_codex_account(str(self.binary), self.root, timeout_s=.3))
                self.assertLess(time.monotonic()-start, 2)
                self.assertNotIn('SECRET', str(caught.exception))
                pid = int((self.root/'server-pid').read_text())
                with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_cancel_probe_reaps_process(self):
        self.settings(mode='timeout')
        async def exercise():
            task = asyncio.create_task(billing.observe_codex_account(str(self.binary), self.root))
            for _ in range(100):
                if (self.root/'server-pid').exists(): break
                await asyncio.sleep(.01)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        asyncio.run(exercise())
        with self.assertRaises(ProcessLookupError): os.kill(int((self.root/'server-pid').read_text()), 0)

    def test_subscription_integrates_auth_command_tokens_and_evidence(self):
        runner = self.runner()
        self.assertEqual(0, asyncio.run(runner.run()))
        argv = json.loads((self.root/'worker-argv.json').read_text())
        self.assertIn('forced_login_method="chatgpt"', argv)
        self.assertIn('workspace-write', argv)
        self.assertEqual('gpt-5.6-sol', argv[argv.index('-m')+1])
        row = self.rows()[0]
        self.assertEqual(120, row['worker_tokens'])
        self.assertEqual('fixture-thread', row['thread_id'])
        self.assertEqual('command-attested', row['model_evidence'])
        self.assertIsNone(row['reported_model'])
        self.assertEqual('subscription', row['billing_route'])
        self.assertNotIn('SECRET', json.dumps(row))
        self.assertFalse((self.root/'state/cost-reservations.json').exists())

    def test_retry_runs_fresh_quota_admission_without_fictitious_attempt(self):
        self.settings(retry_used=100)
        runner = self.runner(check='test "$(cat out.txt)" = 2')
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual('2', (self.root/'admissions').read_text())
        self.assertEqual('1', (self.root/'workers').read_text())
        self.assertEqual(1, len(self.rows()))
        terminal = self.rows('state/lifecycle.jsonl')[-1]
        self.assertEqual(('BLOCKED', 1, 2, 'quota'), (terminal['verdict'], terminal['attempt_index'], terminal['billing_attempt_index'], terminal['failure_class']))
        self.assertIn('quota exhausted', terminal['billing_reason'])
        self.assertEqual(1, sum(e['event']=='attempt_started' for e in self.rows('state/lifecycle.jsonl')))

    def test_unknown_account_blocks_zero_attempts(self):
        self.settings(auth='unknown')
        runner = self.runner()
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(0, runner.runtimes[0].attempts)
        self.assertEqual([], self.rows())
        self.assertEqual('blocked', runner.runtimes[0].status)

    def test_api_login_cannot_be_renamed_subscription(self):
        self.settings(auth='apiKey')
        runner = self.runner()
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual('subscription requires observed ChatGPT authentication', runner.runtimes[0].billing['billing_reason'])
        self.assertFalse((self.root/'workers').exists())

    def test_opencode_explicit_api_route_reserves_without_an_account_probe(self):
        runner = self.opencode_runner()
        self.assertEqual(0, asyncio.run(runner.run()))
        argv = json.loads((self.root/'worker-argv.json').read_text())
        self.assertEqual(['run', '--model', 'openrouter/z-ai/glm-5.2'], argv[:3])
        self.assertFalse((self.root/'admissions').exists())
        row = self.rows()[0]
        self.assertEqual(('api', None, 'unresolved'),
                         (row['billing_route'], row['observed_auth_mode'], row['reservation_status']))
        self.assertEqual([f'{runner.run_id}/one/1'], list(self.ledger()))

    def test_opencode_requires_explicit_api_route_and_unambiguous_model_arguments(self):
        for fields in (dict(billing_route='subscription'), dict(engine_args=['--model', 'openrouter/other/model']),
                       dict(engine_args=['--provider', 'openrouter'])):
            with self.subTest(fields=fields):
                runner = self.opencode_runner(**fields)
                if 'engine_args' in fields and '--model' in fields['engine_args']:
                    with self.assertRaisesRegex(ValueError, 'model settings'):
                        asyncio.run(runner.run())
                else:
                    self.assertEqual(1, asyncio.run(runner.run()))
                self.assertEqual(0, runner.runtimes[0].attempts)
                self.assertFalse((self.root/'workers').exists())
                self.assertFalse((self.root/'admissions').exists())

    def test_opencode_api_route_enforces_the_monthly_and_run_allowance_caps(self):
        self.policy.write_text(json.dumps(dict(monthly_api_cap_gbp='.5', run_api_cap_gbp=5)))
        runner = self.opencode_runner()
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(0, runner.runtimes[0].attempts)
        self.assertIn('allowance unavailable', runner.runtimes[0].billing['billing_reason'])
        self.assertFalse((self.root/'workers').exists())

    def test_no_allowance_no_paid_dispatch(self):
        self.settings(auth='apiKey')
        runner = self.runner(route='api')
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual('paid API requires explicit task spend allowance', runner.runtimes[0].billing['billing_reason'])
        self.assertEqual(0, runner.runtimes[0].attempts)

    def test_api_unknown_cost_stays_held_and_retry_respects_run_cap(self):
        self.settings(auth='apiKey')
        runner = self.runner(route='api', task_spend_allowance_gbp=3, check='test "$(cat out.txt)" = 2')
        self.assertEqual(1, asyncio.run(runner.run()))
        reservations = self.ledger()
        self.assertEqual([f'{runner.run_id}/one/1'], list(reservations))
        row = next(iter(reservations.values()))
        self.assertEqual(('reserved', True, None), (row['state'], row['started'], row['actual_gbp']))
        self.assertIn('allowance unavailable', runner.runtimes[0].billing['billing_reason'])
        self.assertEqual('unknown', runner.runtimes[0].failure_class) # budget is outside taxonomy
        self.assertEqual('api', json.loads((self.root/'worker-argv.json').read_text())[-4].split('=')[1].strip('"'))

    def test_api_actual_usd_and_dated_fx_settle(self):
        self.settings(auth='apiKey', cost='0.25')
        runner = self.runner(route='api', task_spend_allowance_gbp=1)
        self.assertEqual(0, asyncio.run(runner.run()))
        row = next(iter(self.ledger().values()))
        self.assertEqual('settled', row['state'])
        self.assertEqual('0.200', row['actual_gbp'])
        self.assertIn('2026-09-03', row['settlement_source'])
        self.assertEqual('settled', self.rows()[0]['reservation_status'])

    def test_no_fx_and_nonzero_exit_never_settle(self):
        self.settings(auth='apiKey', cost='0.25', exit=1)
        runner = self.runner(route='api', task_spend_allowance_gbp=1, max_attempts=1)
        self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual('reserved', next(iter(self.ledger().values()))['state'])

    def test_cancellation_keeps_started_reservation_and_usage(self):
        self.settings(auth='apiKey', sleep=True, cost='0.25')
        runner = self.runner(route='api', task_spend_allowance_gbp=1)
        async def exercise():
            job = asyncio.create_task(runner.run())
            for _ in range(300):
                if (self.root/'workers').exists(): break
                await asyncio.sleep(.01)
            await asyncio.sleep(.05)
            job.cancel()
            with self.assertRaises(asyncio.CancelledError): await asyncio.wait_for(job, 3)
        asyncio.run(exercise())
        row = next(iter(self.ledger().values()))
        self.assertEqual(('reserved', True, None), (row['state'], row['started'], row['actual_gbp']))
        self.assertEqual('unresolved', self.rows()[0]['reservation_status'])
        self.assertEqual('fixture-thread', self.rows()[0]['thread_id'])

    def test_billing_disabled_never_observes_account(self):
        runner = self.runner(enabled=False)
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertFalse((self.root/'admissions').exists())

    def test_route_model_and_auth_conflicts_block_before_account(self):
        for fields in (dict(model=''), dict(billing_route=None), dict(engine_args=['-c', 'forced_login_method="api"']), dict(engine_args=['-m', 'other'])):
            with self.subTest(fields=fields):
                runner = self.runner(**fields)
                if fields.get('model') == '' or fields.get('billing_route', 'present') is None or '-m' in fields.get('engine_args', []):
                    with self.assertRaises(ValueError):
                        asyncio.run(runner.run())
                else:
                    self.assertEqual(1, asyncio.run(runner.run()))
                self.assertEqual(0, runner.runtimes[0].attempts)
                self.assertFalse((self.root/'admissions').exists())

    def test_command_must_forward_model(self):
        runner = self.runner()
        runner.config.engines['codex'] = replace(runner.config.engines['codex'], args_template=('exec', '{access_args}', '{spec}'))
        with self.assertRaisesRegex(ValueError, 'does not prove it forwards'):
            asyncio.run(runner.run())
        self.assertFalse((self.root/'admissions').exists())

    def test_task_fields_and_config_boundary(self):
        for allowance in (True, False, '1', 0, -1, float('inf'), float('nan'), {}, []):
            with self.subTest(allowance=allowance), self.assertRaises(ValueError):
                ringer.TaskSpec.from_obj(dict(key='t', spec='s', check='c', task_spend_allowance_gbp=allowance))
        for route in ('credits', True, 1, [], ''):
            with self.subTest(route=route), self.assertRaises(ValueError):
                ringer.TaskSpec.from_obj(dict(key='t', spec='s', check='c', billing_route=route))
        config = self.root/'ringer.toml'
        config.write_text('[billing]\npolicy_path="policy.json"\n')
        self.assertEqual(self.policy.resolve(), ringer.AppConfig.load(config).billing_policy_path)

    def test_missing_run_cap_and_exhausted_monthly_cap_block_dispatch(self):
        self.settings(auth='apiKey')
        for policy in ({'monthly_api_cap_gbp':10}, {'monthly_api_cap_gbp':0, 'run_api_cap_gbp':5}):
            with self.subTest(policy=policy):
                self.policy.write_text(json.dumps(policy))
                runner=self.runner(route='api', task_spend_allowance_gbp=1)
                self.assertEqual(1, asyncio.run(runner.run()))
                self.assertEqual(0, runner.runtimes[0].attempts)
                self.assertFalse((self.root/'workers').exists())

    def test_actual_cost_without_fx_remains_unresolved(self):
        self.settings(auth='apiKey', cost='.25')
        self.policy.write_text(json.dumps(dict(monthly_api_cap_gbp=10, run_api_cap_gbp=5)))
        runner=self.runner(route='api', task_spend_allowance_gbp=1)
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual('provider_actual', self.rows()[0]['cost_coverage'])
        self.assertEqual('unresolved', self.rows()[0]['reservation_status'])
        self.assertEqual('reserved', next(iter(self.ledger().values()))['state'])

    def test_failed_spawn_after_started_marker_does_not_invent_release_proof(self):
        self.settings(auth='apiKey')
        runner=self.runner(route='api', task_spend_allowance_gbp=1, max_attempts=1)
        original=asyncio.create_subprocess_exec
        async def fail_worker(*args, **kwargs):
            if len(args)>1 and args[1]=='exec':
                raise OSError('fixture launch failed')
            return await original(*args, **kwargs)
        with patch.object(asyncio, 'create_subprocess_exec', fail_worker):
            self.assertEqual(1, asyncio.run(runner.run()))
        row=next(iter(self.ledger().values()))
        self.assertEqual(('reserved', True, None), (row['state'], row['started'], row['actual_gbp']))
        self.assertFalse((self.root/'workers').exists())

    def test_final_ledger_start_gate_blocks_without_model_attempt(self):
        self.settings(auth='apiKey')
        runner=self.runner(route='api', task_spend_allowance_gbp=1)
        with patch.object(ringer.SpendLedger, 'mark_started', return_value=False):
            self.assertEqual(1, asyncio.run(runner.run()))
        self.assertEqual(0, runner.runtimes[0].attempts)
        self.assertEqual([], self.rows())
        self.assertEqual('released', next(iter(self.ledger().values()))['state'])
        self.assertFalse(any(row['event']=='attempt_started' for row in self.rows('state/lifecycle.jsonl')))

    def test_two_paid_attempts_have_separate_reservations_and_usage(self):
        self.settings(auth='apiKey', cost='.25')
        runner=self.runner(route='api', task_spend_allowance_gbp=1, check='test "$(cat out.txt)" = 2')
        self.assertEqual(0, asyncio.run(runner.run()))
        self.assertEqual({f'{runner.run_id}/one/1', f'{runner.run_id}/one/2'}, set(self.ledger()))
        self.assertEqual([120,120], [row['worker_tokens'] for row in self.rows()])
        self.assertEqual(240, runner.runtimes[0].tokens)
        self.assertTrue(all(row['state']=='settled' for row in self.ledger().values()))

    def test_json_provider_identity_is_distinct_from_requested_identity(self):
        output=json.dumps(dict(type='turn.completed', model='different-model', usage=dict(input_tokens=1, cached_input_tokens=0, output_tokens=1)))
        self.assertEqual('different-model', ringer.parse_reported_model(output, None))
        self.assertEqual('thread-only', billing.usage_evidence(json.dumps(dict(type='thread.started', thread_id='thread-only')), 'm', complete=False)['thread_id'])
        runner=self.runner(enabled=False)
        runtime=runner.runtimes[0]
        runtime.billing=billing.usage_evidence(output, runtime.task.model, complete=False)
        # A later matching model cannot erase a conflicting earlier provider event.
        runner._log_attempt(runtime, runtime.task.spec, False,
            ringer.WorkerResult(0, False, 2, reported_model=runtime.task.model),
            ringer.VerifyResult(True, 0, False, 'ok'), 'PASS', 1)
        self.assertTrue(self.rows()[0]['identity_mismatch'])
        self.assertEqual(runtime.task.model, self.rows()[0]['expected_model'])

    def test_failed_turn_and_missing_cache_do_not_claim_complete_cost_or_usage(self):
        completed=dict(type='turn.completed', provider_cost='.25', usage=dict(input_tokens=100, output_tokens=20))
        evidence=billing.usage_evidence(json.dumps(completed), 'm', complete=True)
        self.assertEqual('input_output', evidence['usage_coverage'])
        self.assertEqual(120, evidence['worker_tokens'])
        output=json.dumps(completed)+'\n'+json.dumps(dict(type='turn.failed', error={'message':'fixture'}))
        self.assertIsNone(billing.usage_evidence(output, 'm', complete=True)['actual_cost_usd'])

    def test_direct_worker_dispatch_cannot_bypass_billing_admission(self):
        runner=self.runner()
        runtime=runner.runtimes[0]
        with self.assertRaisesRegex(ValueError, 'billing admission required'):
            asyncio.run(runner._run_worker(runtime, runtime.task.spec, 1))
        self.assertFalse((self.root/'workers').exists())

    def test_structured_usage_cache_reasoning_and_event_dedup(self):
        part = dict(type='step_finish', sessionID='s', part=dict(id='p', tokens=dict(input=10, output=2, reasoning=3, cache=dict(read=4, write=1)), cost=.1))
        output = '\n'.join(json.dumps(part) for _ in range(2))
        self.assertEqual(20, ringer.parse_token_count(output))
        usage = billing.usage_evidence(output, 'm', complete=True)
        self.assertIsNone(usage['actual_cost_usd'])
        self.assertEqual('0.1', usage['usage_events'][0]['harness_estimate_usd'])
        self.assertEqual(1234, ringer.parse_token_count('tokens used\n1,234\n'))
        self.assertEqual(120, ringer.parse_token_count(json.dumps(dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=40, output_tokens=20)))))
        mixed=json.dumps(dict(type='turn.completed', usage=dict(input_tokens=100, cached_input_tokens=40, output_tokens=20)))+'\ntokens used\n120\n'
        self.assertEqual(120, ringer.parse_token_count(mixed))
        self.assertEqual('unknown', billing.usage_evidence(json.dumps(dict(type='turn.completed', provider_cost='.1')), 'm', complete=True)['usage_coverage'])

    def test_default_command_service_tier_is_recorded_as_standard_for_estimates(self):
        runner = self.runner(engine_args=['-c', 'service_tier="default"'])
        self.assertEqual(0, asyncio.run(runner.run()))
        row = self.rows()[0]
        self.assertEqual('standard', row['service_tier'])
        self.assertEqual('standard', row['usage_events'][0]['service_tier'])
        self.assertEqual('estimate', row['cost_estimates'][0]['status'])
        unknown = billing.usage_evidence(json.dumps(dict(type='turn.completed', usage=dict(
            input_tokens=100, cached_input_tokens=0, output_tokens=20))), 'gpt-5.6-sol', complete=True)
        self.assertIsNone(unknown['service_tier'])
        self.assertEqual('unknown', unknown['cost_estimates'][0]['status'])

if __name__ == '__main__': unittest.main()
