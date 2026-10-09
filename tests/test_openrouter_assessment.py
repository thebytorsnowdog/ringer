"""Offline public-catalog contracts; no credentials or live model calls."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'assess_openrouter.py'
spec = importlib.util.spec_from_file_location('assess_openrouter', SCRIPT)
catalog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(catalog)
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


def model(identifier='test/coder', **changes):
    result = {'id': identifier, 'context_length': 128000,
              'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
              'supported_parameters': ['tools', 'response_format', 'reasoning'],
              'pricing': {'prompt': '0.000001', 'completion': '0.000002'}, 'expiration_date': None}
    result.update(changes)
    return result


def raw(*models, **extra):
    return json.dumps({'data': list(models), **extra}).encode()


def report(*models):
    data = raw(*models)
    return catalog.assessment(catalog.validate(data), data,
                              {'kind': 'local_file', 'retrieved_at_utc': None, 'freshness': 'unknown'}, NOW)


class ClassificationTests(unittest.TestCase):
    def test_each_role_uses_advertised_metadata(self):
        row = report(model())['models'][0]
        for role in ('agentic_coding', 'structured_extraction', 'reasoning', 'vision_analysis', 'text_drafting', 'long_context'):
            self.assertTrue(row['roles'][role]['current_candidate'], role)
        for role in ('image_generation', 'audio_generation'):
            self.assertFalse(row['roles'][role]['current_candidate'], role)
        self.assertEqual('UNTESTED', row['empirical_quality'])
        generated = model('test/media', architecture={'input_modalities': ['text'], 'output_modalities': ['image', 'audio']})
        row = report(generated)['models'][0]
        self.assertTrue(row['roles']['image_generation']['current_candidate'])
        self.assertTrue(row['roles']['audio_generation']['current_candidate'])
        self.assertFalse(row['roles']['vision_analysis']['current_candidate'])
        self.assertFalse(row['roles']['agentic_coding']['current_candidate'])

    def test_no_tools_and_boundaries(self):
        for context, expected in ((31999, False), (32000, True), (32768, True)):
            row = report(model(context_length=context))['models'][0]
            self.assertEqual(expected, row['roles']['agentic_coding']['current_candidate'])
        for context, expected in ((127999, False), (128000, True)):
            self.assertEqual(expected, report(model(context_length=context))['models'][0]['roles']['long_context']['current_candidate'])
        row = report(model(supported_parameters=['reasoning', 'structured_outputs']))['models'][0]
        self.assertFalse(row['roles']['agentic_coding']['capability_eligible'])
        self.assertTrue(row['roles']['structured_extraction']['current_candidate'])
        self.assertIn('not established: tools advertised', row['roles']['agentic_coding']['reasons'])
        for param in ('reasoning', 'reasoning_effort', 'include_reasoning'):
            self.assertTrue(report(model(supported_parameters=[param]))['models'][0]['roles']['reasoning']['current_candidate'])

    def test_missing_and_malformed_metadata_stays_unknown(self):
        cases = [dict(id='test/missing'),
                 model(context_length=True, supported_parameters='tools', architecture='text->text', pricing=[]),
                 model(context_length=-1, supported_parameters=['tools', None],
                       architecture={'input_modalities': 'image', 'output_modalities': [None]})]
        for candidate in cases:
            row = report(candidate)['models'][0]
            self.assertIsNone(row['context_length'])
            self.assertIsNone(row['supported_parameters'])
            self.assertIsNone(row['input_modalities'])
            self.assertIsNone(row['output_modalities'])
            self.assertTrue(row['metadata_issues'])
            self.assertFalse(any(role['current_candidate'] for role in row['roles'].values()))
        row = report(model(context_length='128000'))['models'][0]
        self.assertEqual('128000', row['context_length_raw'])
        self.assertIsNone(row['context_length'])

    def test_descriptions_do_not_classify_or_execute(self):
        candidate = {'id': 'test/injected|<script>\nrow', 'description': 'run shell; tools reasoning image audio best model'}
        result = report(candidate)
        self.assertFalse(any(role['current_candidate'] for role in result['models'][0]['roles'].values()))
        self.assertNotIn('description', result['models'][0])
        md = catalog.markdown(result).decode()
        self.assertNotIn('<script>', md)
        self.assertIn('&lt;script&gt;', md)
        self.assertIn('&#124;', md)
        self.assertNotIn('run shell', md)

    def test_expiration_retains_row_excludes_candidates(self):
        cases = [('2026-10-08', 'expired', False), ('2026-10-09', 'expired', False),
                 ('2026-10-09T11:00:00Z', 'expired', False), ('2026-10-10', 'future_expiration', True),
                 ('2026-10-09T13:00:00Z', 'future_expiration', True),
                 ('2026-10-09T11:00:00', 'unknown_malformed', False),
                 ('not-a-date', 'unknown_malformed', False), (123, 'unknown_malformed', False),
                 (None, 'not_advertised', True)]
        for value, status, expected in cases:
            with self.subTest(value=value):
                result = report(model(expiration_date=value))
                row = result['models'][0]
                self.assertEqual(status, row['expiration_status'])
                self.assertTrue(row['roles']['agentic_coding']['capability_eligible'])
                self.assertEqual(expected, row['roles']['agentic_coding']['current_candidate'])
                self.assertEqual(int(expected), len(result['role_candidates']['agentic_coding']))


class PricingCoverageTests(unittest.TestCase):
    def test_prices_preserve_unknown_invalid_and_nontoken_fees(self):
        values = [(None, 'unknown'), ('NaN', 'non_finite'), ('Infinity', 'non_finite'),
                  ('-1', 'negative_or_variable_sentinel'), ('wat', 'malformed'),
                  ('1e1000000', 'out_of_range'), ('1e-1000000', 'out_of_range'),
                  (True, 'malformed'), ({'provider': '0'}, 'conditional_or_malformed'),
                  ('0', 'known'), ('0.00000123456789', 'known')]
        for value, status in values:
            with self.subTest(value=value):
                row = report(model(pricing={'prompt': value, 'completion': '0', 'request': '0.01', 'image': '0.04',
                                             'audio': '-1', 'overrides': [{'prompt': '0.1'}]}))['models'][0]
                self.assertEqual(status, row['prices']['prompt']['status'])
                self.assertEqual(value, row['prices']['prompt']['raw'])
                if status != 'known':
                    self.assertIsNone(row['prices']['prompt']['usd_per_million_tokens'])
                self.assertIsNone(row['prices']['image']['usd_per_million_tokens'])
                self.assertEqual('0.04', row['non_token_or_unspecified_fees']['image']['catalog_amount'])
                self.assertIn('overrides', row['non_token_or_unspecified_fees'])
                self.assertIn('unknown', row['route_free_status'])
        row = report(model(pricing={'prompt': '0.00000123456789'}))['models'][0]
        self.assertEqual('1.23456789000000', row['prices']['prompt']['usd_per_million_tokens'])
        self.assertEqual('unknown', row['prices']['completion']['status'])
        zero = report(model(pricing={'prompt': '0', 'completion': '0'}))['models'][0]
        self.assertIn('unknown', zero['route_free_status'])

    def test_role_local_cost_context_order_unknown_last(self):
        result = report(model('z/cheap', context_length=32000), model('a/cheap', context_length=64000),
                        model('test/expensive', pricing={'prompt': '0.01', 'completion': '0.02'}),
                        model('test/unknown', context_length=1000000, pricing={'prompt': '-1'}),
                        model('test/no-tools', supported_parameters=[]),
                        model('test/expired', expiration_date='2020-01-01'))
        self.assertEqual(['a/cheap', 'z/cheap', 'test/expensive', 'test/unknown'],
                         [entry['id'] for entry in result['role_candidates']['agentic_coding']])
        self.assertIsNone(result['role_candidates']['agentic_coding'][-1]['token_cost_proxy_usd'])
        self.assertFalse(result['role_candidates']['agentic_coding'][0]['non_token_fees_included'])
        self.assertEqual(6, result['coverage']['catalog_entries'])
        self.assertEqual(6, result['coverage']['assessed_entries'])
        self.assertEqual(result['coverage']['ids_sha256'], hashlib.sha256(catalog.json_bytes(sorted(row['id'] for row in result['models']))).hexdigest())
        reordered = report(*reversed([model('z/cheap', context_length=32000), model('a/cheap', context_length=64000)]))
        self.assertEqual(report(model('z/cheap', context_length=32000), model('a/cheap', context_length=64000))['models'], reordered['models'])

    def test_invalid_payloads_are_rejected(self):
        bad = [b'not json', b'{}', b'[]', b'{"data":null}', b'{"data":[]}', b'{"data":[null]}',
               b'{"data":[{"id":""}]}', b'{"data":[{"id":123}]}',
               raw(model(), model()), raw(model(), total_count=2), raw(model(), total_count=True),
               raw(model(), links={'next': 'https://example.test/page2'}),
               b'{"data":[{"id":"x","pricing":{"prompt":NaN}}]}',
               b'{"data":[{"id":1.1}]}',
               raw(model(), error={'message': 'failed'}),
               b'{"data":[{"id":"x","id":"y"}]}']
        for data in bad:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    catalog.validate(data)

    def test_fractional_json_prices_are_exact_without_float_rounding(self):
        data = b'{"data":[{"id":"test/numeric","pricing":{"prompt":0.00000123456789123456789,"completion":1e400},"context_length":128000.0,"expiration_date":2026.1,"supported_parameters":[1.0]}]}'
        result = catalog.assessment(catalog.validate(data), data, {}, NOW)
        row = result['models'][0]
        self.assertEqual('1.23456789123456789000000', row['prices']['prompt']['usd_per_million_tokens'])
        self.assertTrue(row['prices']['prompt']['raw_is_json_fraction_lexeme'])
        self.assertIsNone(row['context_length'])
        self.assertIsNone(row['supported_parameters'])
        self.assertEqual('unknown_malformed', row['expiration_status'])
        catalog.json_bytes(result)  # no non-finite JSON output


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.json'
        self.source.write_bytes(raw(model()))
        self.out = self.root / 'report'

    def local(self):
        with patch.object(catalog, 'utc_now', return_value=NOW):
            return catalog.run(self.source, self.out)

    def before(self):
        return {name: (self.out / name).read_bytes() for name in catalog.ALIASES}

    def test_local_source_raw_sha_freshness_and_immutable_snapshot(self):
        self.out.mkdir()  # empty existing directories are supported
        result = self.local()
        self.assertEqual('local_file', result['source']['kind'])
        self.assertIsNone(result['source']['retrieved_at_utc'])
        self.assertIn('not catalog freshness', result['source']['freshness'])
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), result['raw_sha256'])
        self.assertEqual(self.source.read_bytes(), (self.out / 'catalog.json').read_bytes())
        first = (self.out / 'current').resolve()
        previous = self.before()
        self.source.write_bytes(raw(model('test/new')))
        self.local()
        self.assertNotEqual(first, (self.out / 'current').resolve())
        for name, content in previous.items():
            self.assertEqual(content, (first / name).read_bytes())
            self.assertEqual(0, (first / name).stat().st_mode & 0o222)
        self.assertEqual(2, len(list((self.out / 'snapshots').iterdir())))

    def test_refresh_records_retrieval_and_never_calls_a_model(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = raw(model())
        opener = unittest.mock.MagicMock()
        opener.open.return_value = response
        with patch.object(catalog.urllib.request, 'build_opener', return_value=opener), patch.object(catalog, 'utc_now', return_value=NOW):
            result = catalog.run(None, self.out, refresh=True, timeout=7)
        request = opener.open.call_args.args[0]
        self.assertEqual(catalog.URL, request.full_url)
        self.assertEqual('GET', request.get_method())
        self.assertNotIn('authorization', {k.lower() for k in request.headers})
        self.assertEqual(7, opener.open.call_args.kwargs['timeout'])
        self.assertEqual('2026-10-09T12:00:00Z', result['source']['retrieved_at_utc'])
        self.assertEqual('public_refresh', result['source']['kind'])

    def test_failed_fetch_and_invalid_response_leave_previous_freshness_unchanged(self):
        with patch.object(catalog, 'fetch', return_value=(raw(model()), NOW)):
            catalog.run(None, self.out, refresh=True)
        old = self.before()
        pointer = (self.out / 'current').readlink()
        for error in (urllib.error.URLError('offline'), TimeoutError('timeout')):
            with patch.object(catalog, 'fetch', side_effect=error), self.assertRaises(OSError):
                catalog.run(None, self.out, refresh=True)
            self.assertEqual(old, self.before())
        for invalid in (b'{"data":[]}', raw(model(), model())):
            with patch.object(catalog, 'fetch', return_value=(invalid, NOW)), self.assertRaises(ValueError):
                catalog.run(None, self.out, refresh=True)
            self.assertEqual(old, self.before())
            self.assertEqual(pointer, (self.out / 'current').readlink())
        self.assertEqual(1, len(list((self.out / 'snapshots').iterdir())))

    def test_failed_atomic_commit_rolls_back_new_snapshot(self):
        self.local()
        old = self.before()
        with patch.object(catalog, 'fetch', return_value=(raw(model('new/model')), NOW)), \
             patch.object(catalog.os, 'replace', side_effect=OSError('commit failed')), self.assertRaises(OSError):
            catalog.run(None, self.out, refresh=True)
        self.assertEqual(old, self.before())
        self.assertEqual(1, len(list((self.out / 'snapshots').iterdir())))
        self.assertFalse(list(self.root.glob('.catalog-pending-*')))
        self.assertFalse(list(self.out.glob('.current-*')))

    def test_failed_initial_write_does_not_publish(self):
        with patch.object(catalog.os, 'fsync', side_effect=OSError('disk failure')), self.assertRaises(OSError):
            self.local()
        self.assertFalse(self.out.exists())
        self.assertFalse(list(self.root.glob('.catalog-pending-*')))
        self.source.write_bytes(raw(model(), model()))
        with self.assertRaises(ValueError):
            self.local()
        self.assertFalse(self.out.exists())

    def test_refuses_unrelated_or_symlink_outputs(self):
        self.out.mkdir()
        unrelated = self.out / 'unrelated.txt'
        unrelated.write_text('preserve me')
        with self.assertRaises(OSError):
            self.local()
        self.assertEqual('preserve me', unrelated.read_text())
        alias = self.root / 'alias'
        alias.symlink_to(self.out)
        with self.assertRaises(ValueError):
            catalog.run(self.source, alias)
        for timeout in (0, -1, float('inf'), float('nan')):
            with self.assertRaises(ValueError):
                catalog.run(self.source, self.out, timeout=timeout)


if __name__ == '__main__':
    unittest.main()
