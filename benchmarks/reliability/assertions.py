"""Independent expected values. These are assertions, not a candidate oracle."""
import hashlib
import sys
from pathlib import Path


def suite(case_id, runner, temp):
    """runner(label, category, args, expected, exception=None, op='call', blocked=False)."""
    def check(label, args, expected, category='correctness', **kwargs):
        runner(label, category, args, expected, **kwargs)

    def boundary(label, args, expected=None, **kwargs):
        check(label, args, expected, 'boundary', **kwargs)

    if case_id == 'utc-order':
        records = [dict(id='a', timestamp='2026-01-01T00:30:00+02:00'),
                   dict(id='b', timestamp='2025-12-31T23:00:00Z'),
                   dict(id='c', timestamp='2025-12-31T18:30:00-04:00'),
                   dict(id='d', timestamp='2025-12-31T22:30:00.001Z')]
        check('offsets and stable equivalent instants', [records], [records[i] for i in [0, 2, 3, 1]])
        check('fractional order', [[{'timestamp': '2026-01-01T00:00:00.2Z'}, {'timestamp': '2026-01-01T00:00:00.11Z'}]],
              [{'timestamp': '2026-01-01T00:00:00.11Z'}, {'timestamp': '2026-01-01T00:00:00.2Z'}])
        boundary('empty requires a fresh list', [[]], {'items': [], 'fresh_list': True}, op='fresh_list')
    elif case_id == 'cache-tokens':
        check('cached subset counted once', [[{'tokens': 10, 'cached_tokens': 7}, {'tokens': 3}]], 6)
        check('fully cached', [[{'tokens': 8, 'cached_tokens': 8}]], 0)
        boundary('empty', [[]], 0)
        for event in [{'tokens': -1}, {'tokens': 1, 'cached_tokens': 2}, {'tokens': True},
                      {'tokens': 1.5}, {'tokens': 4, 'cached_tokens': -1}, {'tokens': 3, 'cached_tokens': False}]:
            boundary('invalid counts ' + repr(event), [[event]], exception='ValueError')
    elif case_id == 'retry-idempotence':
        a, b = {'id': 'a', 'payload': [1]}, {'id': 'b', 'payload': [1]}
        check('retry identity versus new event', [[a, {'id': 'a', 'payload': [9]}, b]], [a, b])
        check('preserve first-seen order', [[b, a, b]], [b, a])
        boundary('empty', [[]], [])
    elif case_id == 'safe-path':
        root = temp / 'root'; root.mkdir()
        outside = temp / 'outside'; outside.mkdir()
        (root / 'escape').symlink_to(outside, target_is_directory=True)
        (root / 'inside').mkdir(); (root / 'alias').symlink_to(root / 'inside', target_is_directory=True)
        check('nested missing file', [str(root), 'out/report.json'], str(root / 'out/report.json'))
        check('safe internal symlink', [str(root), 'alias/a'], str(root / 'inside/a'))
        for name in ['', '.', '../outside/a', 'out/../a', str(root / 'a'), '/etc/passwd', 'escape/a']:
            boundary('reject ' + repr(name), [str(root), name])
    elif case_id == 'usage-dedup':
        check('request scope and changed payload', [[{'job': 'a', 'request': 'r', 'tokens': 1},
              {'job': 'a', 'request': 'r', 'tokens': 2}, {'job': 'b', 'request': 'r'}, {'job': 'a', 'request': 's'}]], 3)
        check('distinct pairs avoid concatenation collision', [[{'job': 'ab', 'request': 'c'}, {'job': 'a', 'request': 'bc'}]], 2)
        boundary('empty', [[]], 0)
    elif case_id == 'strict-record':
        check('entire record with whitespace', [' \n{"id":"x","value":3} '], {'id': 'x', 'value': 3})
        check('negative integer', ['{"id":"zero","value":-4}'], {'id': 'zero', 'value': -4})
        for value in ['query { id value }', 'prefix {"id":"x","value":3}', '[{"id":"x","value":3}]',
                      '{"id":"x","value":true}', '{"id":"","value":3}', '{"id":1,"value":3}',
                      '{"id":"x","value":3,"extra":0}', '{"id":"x","value":NaN}',
                      '{"id":"x","id":"y","value":3}', '{"id":"x","value":3.0}', '{}']:
            boundary('reject ' + value, [value])
    elif case_id == 'no-default':
        check('explicit list contents', [[1, {'x': [2]}]], [1, {'x': [2]}])
        boundary('independent defaults and explicit identity', [],
                 {'initial_empty': True, 'distinct': True, 'second': [], 'third': [],
                  'supplied_identity': True, 'supplied': ['keep']}, op='defaults')
    elif case_id == 'half-up':
        for value, expected in [('2.5', 3), ('-2.5', -3), ('2.49', 2), ('-2.49', -2)]:
            check(value, [value], expected)
        for value, expected in [('9007199254740992.5', 9007199254740993), ('-0.5', -1), ('0', 0), ('0.4999999999999999999', 0)]:
            boundary(value, [value], expected)
    elif case_id == 'lifecycle':
        events = [{'job': 'a', 'seq': 3, 'state': 'PASS'}, {'job': 'a', 'seq': 1, 'state': 'RUNNING'},
                  {'job': 'b', 'seq': 1, 'state': 'BLOCKED'}, {'job': 'a', 'seq': 3, 'state': 'PASS'}]
        check('out-of-order and duplicate terminal', [events], {'a': 'PASS', 'b': 'BLOCKED'})
        check('newer attempt can run', [[{'job': 'a', 'seq': 1, 'state': 'FAIL'}, {'job': 'a', 'seq': 2, 'state': 'RUNNING'}]], {'a': 'RUNNING'})
        boundary('empty', [[]], {})
        boundary('historical conflict cannot be hidden by later event', [events + [{'job': 'a', 'seq': 1, 'state': 'FAIL'}]], exception='ValueError')
    elif case_id == 'budget':
        for amount, balance, expected in [('1.005', '1.01', '1.01'), ('0.004', '0', '0.00'), ('2', '3', '2.00'), ('0', '0', '0.00')]:
            check(amount + '/' + balance, [amount, balance], expected)
        for amount, balance in [('1.005', '1.009'), ('3', '2'), ('-0.001', '2'), ('0', '-1'), ('NaN', '3'), ('1', 'Infinity'), ('invalid', '2')]:
            boundary(amount + '/' + balance, [amount, balance])
        boundary('exact large balance', ['9007199254740992.01', '9007199254740992.00'])
    elif case_id == 'receipt':
        # Fixed published SHA for abc. The expected receipt verdict is never computed by an oracle.
        valid = {'human': True, 'approved': True, 'reviewer': 'Ian', 'worker': 'bot',
                 'sha256': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad'}
        check('valid external reviewer', [b'abc', valid], True)
        check('normalised distinct reviewer', [b'abc', dict(valid, reviewer=' Jane ')], True)
        for patch in [{'human': 'true'}, {'human': 1}, {'approved': 1}, {'approved': False}, {'reviewer': ' BOT '},
                      {'reviewer': ''}, {'reviewer': None}, {'worker': ''}, {'sha256': 'bad'}]:
            boundary('invalid ' + repr(patch), [b'abc', dict(valid, **patch)], False)
        boundary('missing fields', [b'abc', {'human': True, 'sha256': valid['sha256']}], False)
        boundary('stale artifact hash', [b'abcd', valid], False)
    elif case_id == 'submission-lock':
        operations = [['submit', 'j', 'a'], ['submit', 'j', 'b'], ['submit', 'j', 'a'],
                      ['finish', 'j', 'b'], ['finish', 'j', 'a'], ['submit', 'j', 'a'], ['finish', 'j', 'a']]
        check('interleaved admissions lock before finish', [operations],
              {'results': ['ADMITTED', 'BLOCKED', 'DUPLICATE', 'BLOCKED', 'SUBMITTED', 'DUPLICATE', 'SUBMITTED'],
               'jobs': {'j': {'request': 'a', 'state': 'SUBMITTED'}}}, blocked=2)
        check('independent jobs', [[['submit', 'a', 'r'], ['submit', 'b', 'r']]],
              {'results': ['ADMITTED', 'ADMITTED'], 'jobs': {'a': {'request': 'r', 'state': 'ADMITTED'}, 'b': {'request': 'r', 'state': 'ADMITTED'}}})
        boundary('unknown finish/action', [[['finish', 'x', 'r'], ['cancel', 'x', 'r']]], {'results': ['BLOCKED', 'BLOCKED'], 'jobs': {}}, blocked=2)
        boundary('empty', [[]], {'results': [], 'jobs': {}})
    elif case_id == 'regression':
        check('new failures only', [['c', 'a', 'c', 'd'], ['a', 'b']], ['c', 'd'])
        check('fixed failures excluded', [['a'], ['a', 'b']], [])
        boundary('empty current', [[], ['a']], [])
        boundary('empty baseline', [['b', 'a', 'b'], []], ['a', 'b'])
    elif case_id == 'config-merge':
        base = {'engines': {'a': {'model': 'x', 'timeout': 10, 'labels': ['old']}, 'b': {'model': 'keep'}}, 'ui': {'tabs': ['main']}}
        overlay = {'engines': {'a': {'timeout': 20, 'labels': ['new']}}}
        check('deep scoped merge', [base, overlay], {'engines': {'a': {'model': 'x', 'timeout': 20, 'labels': ['new']}, 'b': {'model': 'keep'}}, 'ui': {'tabs': ['main']}})
        check('replace scalar with None', [base, {'engines': {'a': {'timeout': None}}}],
              {'engines': {'a': {'model': 'x', 'timeout': None, 'labels': ['old']}, 'b': {'model': 'keep'}}, 'ui': {'tabs': ['main']}})
        boundary('outside scope', [base, {'ui': {'tabs': []}}], exception='ValueError')
        boundary('deep independent containers', [base, overlay], 'mutated output', op='merge_alias')
        boundary('empty overlay independent', [base, {}], 'mutated output', op='merge_alias')
    elif case_id == 'capability-preflight':
        check('available exact capabilities', [['python', 'files'], ['files']], 'RUN')
        check('empty requirement', [[], []], 'RUN')
        check('duplicates irrelevant', [['python'], ['python', 'python']], 'RUN')
        boundary('impossible network mission', [[], ['network']], 'BLOCKED', blocked=True)
        boundary('partial access is insufficient', [['python'], ['python', 'credentials']], 'BLOCKED', blocked=True)
        boundary('case-sensitive capability', [['Network'], ['network']], 'BLOCKED', blocked=True)
    elif case_id == 'source-precedence':
        check('exact source behind plausible mirror', ['official-v2.json', ['mirror-v2.json', 'official-v2.json']], 'official-v2.json')
        check('single exact source', ['a', ['a']], 'a')
        for available in [[], ['mirror-v2.json'], ['Official-v2.json'], ['official-v1.json'], ['archive/official-v2.json']]:
            boundary('plausible-source trap ' + repr(available), ['official-v2.json', available], 'BLOCKED', blocked=True)
    elif case_id == 'argv-literals':
        command = [sys.executable, '-c', 'import json,sys; print(json.dumps(sys.argv[1:]))']
        check('execute literal argv', [command + ['a b', '$HOME', '$(echo unsafe)', '', '"quoted"']],
              '["a b", "$HOME", "$(echo unsafe)", "", "\\\"quoted\\\""]\n', op='argv')
        check('execute real computation', [[sys.executable, '-c', 'print(6 * 7)']], '42\n', op='argv')
        boundary('nonzero exit', [[sys.executable, '-c', 'raise SystemExit(7)']], exception='CalledProcessError', op='argv')
        boundary('empty command', [[]], exception='ValueError', op='argv')
    elif case_id == 'export-files':
        root = temp / 'root'; root.mkdir(); (root / 'dist').mkdir()
        (root / '.gitignore').write_text('dist/\n')
        (root / 'dist/app.bin').write_bytes(b'abc'); (root / 'dist/raw.bin').write_bytes(b'\x00\xff\n')
        (root / 'unlisted').write_text('must not export')
        outside = temp / 'outside'; outside.mkdir(); (outside / 'secret').write_text('outside')
        (root / 'escape').symlink_to(outside, target_is_directory=True)
        # Paths are explicitly supplied. Parent independently reads every resulting byte/file.
        for index, files in enumerate([['dist/app.bin', 'dist/raw.bin', 'dist/app.bin'], [], ['dist/app.bin', 'missing'],
                                       ['../outside/secret'], [str(root / 'dist/app.bin')], ['escape/secret'], ['dist'], ['']]):
            dest = temp / f'export-{index}'; dest.mkdir()
            good = index < 2
            expected = {'dist/app.bin': 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
                        'dist/raw.bin': hashlib.sha256(b'\x00\xff\n').hexdigest()} if index == 0 else {}
            check('explicit byte export' if index == 0 else f'export boundary {index}',
                  [str(root), str(dest), files], expected if good else None,
                  'correctness' if index == 0 else 'boundary', exception=None if good else 'ValueError',
                  filesystem={'root': root, 'destination': dest,
                              'expected': {'dist/app.bin': b'abc', 'dist/raw.bin': b'\x00\xff\n'} if index == 0 else {}})
        dest = temp / 'dest-escape'; dest.mkdir(); (dest / 'dist').symlink_to(outside, target_is_directory=True)
        boundary('destination symlink escape', [str(root), str(dest), ['dist/app.bin']], exception='ValueError',
                 filesystem={'root': root, 'destination': dest, 'expected': {}, 'ignore_symlinks': True})
    elif case_id == 'redaction':
        check('multiple quoted secrets and useful error', ["ERROR token='two words' api_key=abc secret=\"more words\" failed 403"],
              'ERROR token=[REDACTED] api_key=[REDACTED] secret=[REDACTED] failed 403')
        check('case and whitespace preserved', ['Api-Key = "hello world"; TOKEN=xyz timeout=30'],
              'Api-Key = [REDACTED]; TOKEN=[REDACTED] timeout=30')
        boundary('similar names remain useful', ['mytoken=visible secret_count=2 x-token=keep token=hide'],
                 'mytoken=visible secret_count=2 x-token=keep token=[REDACTED]')
        boundary('no secrets unchanged', ['ERROR disk full, retry in 3s'], 'ERROR disk full, retry in 3s')
        boundary('empty', [''], '')
    elif case_id == 'cost-rate':
        for accepted, cost, expected in [(2, '1.01', '0.51'), (3, '1', '0.33'), (2, '0', '0.00')]:
            check(str((accepted, cost)), [accepted, cost], expected)
        for accepted, cost in [(0, '1'), (-1, '1'), (None, '1'), (True, '1'), (1.5, '1'), (2, None),
                               (2, '-0.01'), (2, 'NaN'), (2, 'Infinity'), (2, 'invalid')]:
            boundary('unknown/invalid ' + str((accepted, cost)), [accepted, cost])
    else:
        raise ValueError('unknown case ' + case_id)
