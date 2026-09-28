"""Read Ringer StateWriter snapshots and executed-check evidence, retaining gaps."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex

from .cases import case_digest, get_case
from .checker import CHECKER_ID, digest, source_digest, source_hashes

START = re.compile(r'^\[ringer.py\] attempt (\d+) started (\S+)\s*$', re.M)


def load_json(path):
    return json.loads(path.read_text())


def instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError, AttributeError):
        return None


def pointers(text):
    found = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and 'benchmark_score_path' in row:
            found.append(row)
    return found


def verified_score(pointer, manifest, task, final, execution, execution_bounds=None):
    """Validate provenance and contents without merging arbitrary fields into metadata."""
    meta = manifest['benchmark']
    benchmark = task['benchmark']
    source = source_digest()
    expected_root = Path(meta['output_root']) / 'evidence'
    path = Path(str(pointer.get('benchmark_score_path', '')))
    if not path.is_absolute() or path.is_symlink() or path.parent != expected_root or path.resolve().parent != expected_root.resolve():
        return None, 'UNTRUSTED_SCORE_PATH'
    try:
        if digest(path) != pointer.get('benchmark_score_sha256'):
            return None, 'SCORE_DIGEST_MISMATCH'
        score = load_json(path)
        recorded_execution = score.get('execution')
        if execution is not None:
            if recorded_execution != execution:
                return None, 'STALE_EXECUTION_BINDING'
        elif execution_bounds and isinstance(recorded_execution, dict):
            taskdir, attempt_index, earliest, latest = execution_bounds
            stamp = instant(recorded_execution.get('started_at'))
            if (recorded_execution.get('taskdir') != taskdir or recorded_execution.get('attempt_index') != attempt_index
                    or stamp is None or earliest is None or stamp < earliest or (latest and stamp >= latest)):
                return None, 'STALE_EXECUTION_BINDING'
        else:
            return None, 'STALE_EXECUTION_BINDING'
        expected = {'checker_id': CHECKER_ID, 'checker_source_sha256': source, 'expected_source_sha256': source,
                    'check_digest': source, 'case_id': benchmark['case_id'], 'case_digest': benchmark['case_digest'],
                    'spec_digest': benchmark['spec_digest'], 'seed': benchmark['seed'], 'cell_id': task['key'],
                    'generation_id': meta['generation_id'], 'nohumanpromotion': True}
        if any(score.get(key) != value for key, value in expected.items()):
            return None, 'SCORE_BINDING_MISMATCH'
        archive = Path(score.get('candidate_snapshot', ''))
        if archive.parent != expected_root or archive.is_symlink() or archive.resolve().parent != expected_root.resolve():
            return None, 'MISSING_CANDIDATE_SNAPSHOT'
        if digest(archive) != score['candidate_source_sha256']:
            return None, 'ARTIFACT_DIGEST_MISMATCH'
        candidate = Path(manifest['workdir']) / task['key'] / 'solution.py'
        current_match = candidate.is_file() and not candidate.is_symlink() and digest(candidate) == score['candidate_source_sha256']
        if final and not current_match:
            return None, 'STALE_CANDIDATE'
        if final and score.get('task_contract_state') == 'PASS':
            notes = candidate.parent / 'notes.md'
            if not notes.is_file() or notes.is_symlink() or digest(notes) != score.get('notes_sha256'):
                return None, 'STALE_TASK_CONTRACT'
        details = score['details']
        if not isinstance(details, list) or not details:
            return None, 'INVALID_SCORE'
        for category in ('correctness', 'boundary'):
            checks = [entry for entry in details if entry['category'] == category]
            passed = sum(entry.get('passed') is True and not entry.get('skipped', False) for entry in checks)
            if not checks or score[category + '_checks'] != {'passed': passed, 'total': len(checks)}:
                return None, 'INVALID_SCORE'
            if score[category + '_score'] != passed / len(checks):
                return None, 'INVALID_SCORE'
        all_passed = all(entry.get('passed') is True and not entry.get('skipped', False) for entry in details)
        if score['resultstate'] == 'PASS' and (not all_passed or score['task_contract_state'] != 'PASS'):
            return None, 'INVALID_SCORE'
        if score['resultstate'] not in ('PASS', 'FAIL'):
            return None, 'INVALID_SCORE'
        selected = {key: score[key] for key in ('checker_id', 'checker_source_sha256', 'case_digest', 'spec_digest',
                    'check_digest', 'candidate_source_sha256', 'checks', 'correctness_checks', 'boundary_checks',
                    'correctness_score', 'boundary_score', 'resultstate', 'correctly_blocked_decisions',
                    'expected_blocked_decisions', 'task_contract_state', 'failure_class')}
        selected.update(evidence_path=str(path), candidate_snapshot=str(archive), current_artifact_matches=current_match,
                        source_spec_artifact_digests_match=True)
        return selected, None
    except (OSError, ValueError, TypeError, KeyError):
        return None, 'MISSING_OR_INVALID_SCORE'


def header_identity(worker_text, declared_engine, declared_model):
    identity = {'engine': declared_engine, 'model': declared_model, 'identity_status': 'DECLARED'}
    engine = re.search(r'^\[ringer.py\] engine: (\S+)\s*$', worker_text, re.M)
    if engine:
        identity['engine'] = engine.group(1)
        identity['engine_evidence'] = 'RINGER_EXECUTION_LOG'
    # Only the bounded Codex CLI header proves a model. Task names, command flags,
    # reported_model fields and prose model claims do not establish execution identity.
    header_text = re.sub(r'\A(?:\s*\n|\[ringer.py\] engine:[^\n]*\n)*', '', worker_text)
    if header_text.startswith('[ringer.py] command: '):
        line, _, rest = header_text.partition('\n')
        command = line.removeprefix('[ringer.py] command: ')
        suffix = ' < /dev/null'
        # Require a whole canonical display command. Otherwise a header inside a
        # multiline quoted prompt could be mistaken for the CLI's own header.
        header_text = ''
        if command.endswith(suffix):
            command = command[:-len(suffix)]
            try:
                if shlex.join(shlex.split(command)) == command:
                    header_text = rest
            except ValueError:
                pass
    # Multiline commands and unrecognised startup warnings remain DECLARED.
    # Never search later worker prose for a plausible header.
    header = re.match(r'OpenAI Codex v[^\n]+\n--------\n(.{0,4096}?)\n--------(?:\n|$)', header_text, re.S)
    if header and identity['engine'] == 'codex':
        match = re.search(r'^model: ([^\s]+)\s*$', header.group(1), re.M)
        if match:
            identity.update(model=match.group(1), identity_status='LOG_HEADER_VERIFIED')
    identity['model_matches_manifest'] = identity['model'] == declared_model
    return identity


def attempts_from_state(manifest, task, state, runtime, next_start=None):
    declared_engine = runtime.get('engine') or task['engine']
    declared_model = runtime.get('model') or task['model']
    try:
        log_path = Path(runtime['log_path'])
        # StateWriter names the path. Never guess worker.log or scan arbitrary score files.
        log = log_path.read_text(errors='replace')
    except (OSError, KeyError, TypeError):
        log = ''
    run_start = instant(state.get('started_at'))
    matches = list(START.finditer(log))
    segments = []
    for index, match in enumerate(matches):
        start = instant(match.group(2))
        if start is None or run_start is None or start < run_start or (next_start and start >= next_start):
            continue
        segments.append((int(match.group(1)), match.group(2), log[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(log)]))
    by_index = {}
    duplicate_indices = set()
    execution_by_index = {}
    for index, started_at, segment in segments:
        if index in by_index:
            duplicate_indices.add(index)
        by_index[index] = segment
        execution_by_index[index] = {'taskdir': runtime.get('taskdir'), 'attempt_index': index, 'started_at': started_at}
    count = runtime.get('attempts', 0)
    if type(count) is not int or count < 0 or count > 100:
        count = 0
    results = []
    for index in range(1, max(count, max(by_index, default=0)) + 1):
        segment = by_index.get(index, '')
        exit_match = re.search(r'^\[ringer.py\] attempt ' + str(index) + r' exited rc=(-?\d+)\s*$', segment, re.M)
        worker_text = segment[:exit_match.start()] if exit_match else segment
        identity = header_identity(worker_text, declared_engine, declared_model)
        row = {'attempt_index': index, **identity, 'worker_returncode': int(exit_match.group(1)) if exit_match else None,
               'outcome': 'INTERRUPTED' if not exit_match else 'NO_CHECK_EVIDENCE', 'quality_pass': False,
               'failure_class': 'missing_attempt_log' if not segment else runtime.get('failure_class'), 'score': None}
        if index > count:
            row.update(outcome='ORPHAN_ATTEMPT', failure_class='attempt_not_in_state')
            results.append(row)
            continue
        final = index == count
        candidates = pointers(segment[exit_match.end():]) if exit_match else []
        # The final check output is also captured independently in the state snapshot.
        # Old Ringer retains checker output only in state, even with a full worker
        # log. Bind that final pointer to the exact logged execution when present.
        state_pointers = pointers(runtime.get('check_output_tail', '')) if final else []
        if not candidates and state_pointers:
            candidates = state_pointers
        if index in duplicate_indices or len(candidates) > 1 or len(state_pointers) > 1:
            row.update(outcome='AMBIGUOUS_CHECK_EVIDENCE', failure_class='duplicate_attempt_or_score')
        elif candidates:
            if final and state_pointers and candidates[-1] != state_pointers[-1]:
                row.update(outcome='STALE_CHECK_EVIDENCE', failure_class='state_log_disagreement')
            else:
                score, error = verified_score(candidates[0], manifest, task, final and next_start is None,
                                              execution_by_index.get(index),
                                              (runtime.get('taskdir'), index, run_start, next_start))
                if error:
                    row.update(outcome=error, failure_class=error)
                else:
                    row['score'] = score
                    valid_exit = row['worker_returncode'] == 0 or (not segment and final and runtime.get('verdict') == 'PASS')
                    final_gate = not final or (runtime.get('check_returncode') == 0 and not runtime.get('check_timed_out')
                                              and runtime.get('status') == 'pass' and runtime.get('verdict') == 'PASS')
                    passed = score['resultstate'] == 'PASS' and valid_exit and final_gate
                    row.update(outcome='PASS' if passed else 'FAIL', quality_pass=passed,
                               failure_class=None if passed else score['failure_class'] or runtime.get('failure_class') or 'runner_gate')
        results.append(row)
    return results


def build_report(manifest_path, state_dir, historical=()):
    manifest = load_json(manifest_path)
    meta = manifest['benchmark']
    source_matches = meta['checker_source_sha256'] == source_digest() and meta['source_hashes'] == source_hashes()
    states, issues = [], []
    runs_dir = state_dir / 'runs' if (state_dir / 'runs').is_dir() else state_dir
    for path in sorted(runs_dir.glob('*.json')):
        try:
            state = load_json(path)
            if not isinstance(state, dict) or state.get('run_name') != manifest['run_name']:
                continue
            if state.get('run_id') != path.stem or not isinstance(state.get('tasks'), list) or instant(state.get('started_at')) is None:
                issues.append({'source': str(path), 'issue': 'INVALID_RUN_STATE'})
                continue
            if state.get('job_id', manifest['job_id']) != manifest['job_id']:
                continue
            states.append(state)
        except (OSError, ValueError, TypeError):
            issues.append({'source': str(path), 'issue': 'UNREADABLE_STATE'})
    states.sort(key=lambda state: (instant(state['started_at']), state['run_id']))
    cells = []
    for task in manifest['tasks']:
        benchmark = task['benchmark']
        case = get_case(benchmark['case_id'])
        expected = {'case_id': case.case_id, 'seed': case.seed, 'case_digest': case_digest(case),
                    'spec_digest': case.spec_digest, 'check_digest': source_digest()}
        valid_contract = source_matches and benchmark == expected and task['spec'] == case.spec
        row = {'cell_id': task['key'], 'case_id': case.case_id, 'task_type': task['task_type'],
               'checker_id': CHECKER_ID, 'checker_source_sha256': source_digest(),
               'generation_id': meta['generation_id'], 'declared_engine': task['engine'], 'declared_model': task['model'],
               'engine': task['engine'], 'model': task['model'], 'identity_status': 'DECLARED',
               **benchmark, 'runs': [], 'resultstate': 'MISSING', 'quality_pass': False,
               'first_try': None, 'final_task_outcome': 'MISSING', 'nohumanpromotion': True,
               'source_spec_digests_match': valid_contract}
        matching = []
        for state in states:
            tasks = [item for item in state['tasks'] if item.get('key') == task['key']]
            if len(tasks) > 1:
                issues.append({'run_id': state['run_id'], 'cell_id': task['key'], 'issue': 'DUPLICATE_TASK_ID'})
            elif tasks:
                runtime = tasks[0]
                taskdir = str(Path(manifest['workdir']) / task['key'])
                if runtime.get('taskdir') != taskdir:
                    continue
                matching.append((state, runtime))
        for index, (state, runtime) in enumerate(matching):
            check_match = runtime.get('check') == task['check']
            state_spec_matches = runtime.get('spec') == task['spec']
            state_source_matches = runtime.get('source_sha256', task['source_sha256']) == task['source_sha256']
            if runtime.get('check_sha256'):
                check_match = check_match and runtime['check_sha256'] == hashlib.sha256(task['check'].encode()).hexdigest()
            next_start = instant(matching[index + 1][0]['started_at']) if index + 1 < len(matching) else None
            attempts = attempts_from_state(manifest, task, state, runtime, next_start) if check_match and state_spec_matches and state_source_matches and valid_contract else []
            run = {'run_id': state['run_id'], 'task_key': task['key'], 'status': runtime.get('status'),
                   'verdict': runtime.get('verdict'), 'failure_class': runtime.get('failure_class'),
                   'check_returncode': runtime.get('check_returncode'), 'check_timed_out': runtime.get('check_timed_out'),
                   'check_command_matches': check_match, 'state_spec_matches': state_spec_matches,
                   'state_source_hashes_match': state_source_matches, 'attempts': attempts,
                   'first_try': attempts[0] if attempts else None,
                   'final_task_outcome': runtime.get('verdict') or runtime.get('status', 'MISSING'),
                   'worker_tokens': runtime.get('tokens'), 'cost': None}
            row['runs'].append(run)
        if row['runs']:
            first, last = row['runs'][0], row['runs'][-1]
            row['first_try'] = first['first_try']
            row['final_task_outcome'] = last['final_task_outcome']
            if last['attempts']:
                attempt = last['attempts'][-1]
                row.update({key: attempt[key] for key in ('engine', 'model', 'identity_status', 'quality_pass')})
                row['resultstate'] = attempt['outcome']
                if attempt['score']:
                    row['quality'] = attempt['score']
            else:
                row['resultstate'] = 'CHECK_COMMAND_MISMATCH' if not last['check_command_matches'] else (last['status'] or 'MISSING').upper()
                if not last['state_spec_matches'] or not last['state_source_hashes_match']:
                    row['resultstate'] = 'STATE_SPEC_OR_SOURCE_MISMATCH'
                if row['resultstate'] in {'PASS', 'READY', 'USED'}:
                    row['resultstate'] = 'NO_CHECK_EVIDENCE'
        if not valid_contract:
            row.update(resultstate='SOURCE_OR_SPEC_MISMATCH', quality_pass=False)
        cells.append(row)
    historical_sources = []
    for value in historical:
        path = Path(value).resolve()
        historical_sources.append({'source': str(path), 'sha256': digest(path), 'content': path.read_text(),
                                   'use': 'separate historical context, excluded from matched cells'})
    return {'run_name': manifest['run_name'], 'job_id': manifest['job_id'], 'manifest_sha256': digest(manifest_path),
            'checker_id': CHECKER_ID, 'checker_source_sha256': source_digest(), 'cells': cells, 'issues': issues,
            'summary': {'manifest_cells': len(cells), 'quality_pass_cells': sum(row['quality_pass'] for row in cells),
                        'correctly_blocked_decisions': sum(row.get('quality', {}).get('correctly_blocked_decisions', 0) for row in cells if row['quality_pass']),
                        'completed_deliverables_from_blocked_decisions': 0},
            'comparability': {'source_matches': source_matches,
                              'same_case_spec_seed_check': all(row['source_spec_digests_match'] for row in cells)},
            'human_quality': 'NOT_ASSESSED', 'nohumanpromotion': True, 'historical_sources': historical_sources,
            'cost_note': 'Cost unavailable unless explicit per-request money metadata is separately supplied. Total tokens and subscription allocation are not prices.',
            'limitations': 'Microbenchmark evidence does not prove real task success or superiority. At least 20 distinct jobs per candidate and 30 per task family are required before any promotion review.'}


def markdown(report):
    def safe(value):
        return str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['# Matched model microbenchmark', '', report['limitations'], '',
             'Human readability and maintainability: not assessed. No model promotion is inferred.', '',
             '| Model | Identity evidence | Case | First try | Final task | Evidence outcome | Correctness | Boundary | Correct refusals |',
             '|---|---|---|---|---|---|---:|---:|---:|']
    for row in report['cells']:
        quality = row.get('quality', {})
        values = [row['model'], row['identity_status'], row['case_id'], (row['first_try'] or {}).get('outcome', 'MISSING'),
                  row['final_task_outcome'], row['resultstate'], quality.get('correctness_score', ''),
                  quality.get('boundary_score', ''), quality.get('correctly_blocked_decisions', '')]
        lines.append('| ' + ' | '.join(safe(value) for value in values) + ' |')
    lines += ['', report['cost_note'], '', 'Correct refusals are decisions, not completed deliverables. All run IDs, attempts, failure classes and digest checks are in the companion JSON.', '',
              'Checker: ' + report['checker_id'] + '; source SHA-256: ' + report['checker_source_sha256'] + '.', '',
              'Next step: coordinator reviews replay evidence before executing a matched subscription comparison.']
    if report['historical_sources']:
        lines += ['', 'Historical sources (excluded from comparison):']
        lines += ['- ' + safe(item['source']) + ' (SHA-256 ' + item['sha256'] + ')' for item in report['historical_sources']]
    return '\n'.join(lines) + '\n'
