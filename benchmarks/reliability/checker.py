"""Coordinator-side assertions, timeouts and content-bound evidence."""
import base64
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from .assertions import suite
from .cases import case_digest, get_case

ROOT = Path(__file__).resolve().parents[2]
CHECKER_ID = 'ringer-reliability-v2'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_files():
    paths = list((ROOT / 'benchmarks/reliability').rglob('*.py'))
    paths += list((ROOT / 'benchmarks/reliability').rglob('*.json'))
    paths += [ROOT / 'scripts/ringer_benchmark.py', ROOT / 'tests/test_benchmark_contract.py']
    return sorted(paths)


def source_hashes():
    return {str(path.relative_to(ROOT)): digest(path) for path in source_files()}


def source_digest():
    return hashlib.sha256(json.dumps(source_hashes(), sort_keys=True).encode()).hexdigest()


def encode(value):
    if isinstance(value, bytes):
        return {'__bytes__': base64.b64encode(value).decode()}
    if isinstance(value, (list, tuple)):
        return [encode(item) for item in value]
    if isinstance(value, dict):
        return {key: encode(item) for key, item in value.items()}
    return value


def snapshot(root):
    return {str(path.relative_to(root)): ('symlink', os.readlink(path)) if path.is_symlink()
            else ('file', digest(path)) if path.is_file() else ('directory', None)
            for path in root.rglob('*')}


def execute(candidate, request, timeout, cwd):
    """No score comes from the child. Enforce a bounded process group and JSON-only output."""
    process = subprocess.Popen([sys.executable, '-B', '-I', str(ROOT / 'benchmarks/reliability/probe.py'), str(candidate)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, cwd=cwd, start_new_session=True)
    try:
        stdout, stderr = process.communicate(json.dumps(encode(request)), timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate(timeout=1)
        return None, 'TIMEOUT'
    # A descendant must not outlive the bounded operation.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.returncode != 0:
        return None, 'PROCESS_ERROR'
    if stderr or len(stdout) > 100000:
        return None, 'OUTPUT_PROTOCOL'
    try:
        payload = json.loads(stdout)
        if not isinstance(payload, dict) or set(payload) != {'value', 'exception', 'input_preserved', 'calls'}:
            raise ValueError('invalid probe shape')
        return payload, None
    except (ValueError, TypeError):
        return None, 'OUTPUT_PROTOCOL'


def evaluate(case_id, candidate, expected_source=None, timeout=2, require_notes=False):
    case = get_case(case_id)
    candidate = Path(candidate).absolute()
    initial_source = source_digest()
    result = {'checker_id': CHECKER_ID, 'checker_source_sha256': initial_source,
              'expected_source_sha256': expected_source or initial_source,
              'case_id': case_id, 'case_digest': case_digest(case), 'spec_digest': case.spec_digest,
              'seed': case.seed, 'check_digest': initial_source, 'candidate_source_sha256': None,
              'nohumanpromotion': True, 'human_quality': 'NOT_ASSESSED', 'details': []}
    failure = None
    if expected_source is not None and expected_source != initial_source:
        failure = 'SOURCE_MISMATCH'
    elif candidate.is_symlink() or not candidate.is_file():
        failure = 'MISSING_CANDIDATE'
    else:
        result['candidate_source_sha256'] = digest(candidate)
    abort = failure

    def run(label, category, args, expected, exception=None, op='call', blocked=False, filesystem=None):
        nonlocal abort
        entry = {'label': label, 'category': category, 'expected_blocked': bool(blocked), 'blocked_decisions': int(blocked), 'passed': False}
        if abort:
            entry.update(failure_class=abort, skipped=True)
            result['details'].append(entry)
            return
        work_before = snapshot(temp)
        before = snapshot(filesystem['root']) if filesystem else None
        outside_before = snapshot(temp / 'outside') if filesystem else None
        actual, error = execute(candidate, {'function': case.signature.split('(')[0], 'args': args, 'op': op}, timeout, temp)
        if error:
            abort = error
            entry['failure_class'] = error
        else:
            # JSON equality alone would accept True == 1. Compare serialised values instead.
            value_matches = json.dumps(actual['value'], sort_keys=True) == json.dumps(expected, sort_keys=True)
            passed = actual['exception'] == exception and value_matches and actual['input_preserved'] is True
            if op == 'argv':
                expected_calls = 0 if not args[0] else 1
                passed = passed and len(actual['calls']) == expected_calls and all(all(call.values()) for call in actual['calls'])
            if not filesystem:
                passed = passed and snapshot(temp) == work_before
            if filesystem:
                dest = filesystem['destination']
                expected_files = filesystem['expected']
                found = {str(path.relative_to(dest)): path.read_bytes() for path in dest.rglob('*')
                         if path.is_file() and not path.is_symlink()}
                passed = passed and found == expected_files and snapshot(filesystem['root']) == before
                passed = passed and snapshot(temp / 'outside') == outside_before
                destination_prefix = str(dest.relative_to(temp)) + '/'
                outside_destination = lambda state: {key: value for key, value in state.items()
                                                     if not key.startswith(destination_prefix)}
                passed = passed and outside_destination(snapshot(temp)) == outside_destination(work_before)
            entry.update(passed=bool(passed), input_preserved=actual['input_preserved'],
                         expected=repr(expected), actual=repr(actual['value']), exception=actual['exception'],
                         failure_class=None if passed else 'ASSERTION')
        result['details'].append(entry)

    with tempfile.TemporaryDirectory(prefix='ringer-check-') as directory:
        temp = Path(directory).resolve()
        suite(case_id, run, temp)
    if source_digest() != initial_source:
        failure = 'SOURCE_CHANGED_DURING_CHECK'
    elif result['candidate_source_sha256'] and (not candidate.is_file() or digest(candidate) != result['candidate_source_sha256']):
        failure = 'CANDIDATE_CHANGED_DURING_CHECK'
    if require_notes:
        notes = candidate.parent / 'notes.md'
        notes_ok = notes.is_file() and not notes.is_symlink() and bool(notes.read_text().strip())
        result['task_contract_state'] = 'PASS' if notes_ok else 'NEEDS_CHANGE'
        result['notes_sha256'] = digest(notes) if notes_ok else None
    else:
        notes_ok = True
        result['task_contract_state'] = 'NOT_ASSESSED'
    details = result['details']
    for category in ('correctness', 'boundary'):
        selected = [entry for entry in details if entry['category'] == category]
        passed = sum(entry['passed'] for entry in selected)
        result[category + '_checks'] = {'passed': passed, 'total': len(selected)}
        result[category + '_score'] = passed / len(selected) if selected and not failure else 0.0
    passed = sum(entry['passed'] for entry in details)
    result.update(checks={'passed': passed, 'total': len(details)},
                  resultstate='PASS' if passed == len(details) and not failure and notes_ok else 'FAIL',
                  correctly_blocked_decisions=sum(entry['blocked_decisions'] for entry in details if entry['passed']),
                  expected_blocked_decisions=sum(entry['blocked_decisions'] for entry in details),
                  failure_class=failure or abort or (None if passed == len(details) and notes_ok else 'ASSERTION' if notes_ok else 'TASK_CONTRACT'))
    # The callable passes mixed tests; correctly refused decisions never become delivered products.
    result['completed_deliverables_from_blocked_decisions'] = 0
    return result
