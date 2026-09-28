#!/usr/bin/env python3
"""Generate, independently check and report bounded Ringer micro-jobs. No model calls."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import uuid

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.reliability.cases import CASES, case_digest, get_case
from benchmarks.reliability.checker import ROOT, digest, evaluate, source_digest, source_hashes

MODELS = ('codex:gpt-5.6-luna', 'codex:gpt-5.6-terra', 'codex:gpt-5.6-sol',
          'opencode:openrouter/moonshotai/kimi-k2.7-code', 'opencode:openrouter/z-ai/glm-5.2',
          'opencode:openrouter/z-ai/glm-5.3-flash', 'opencode:openrouter/google/gemini-3.8-flash')
SCRIPT = Path(__file__).resolve()


def json_bytes(data):
    return (json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + '\n').encode()


def absolute_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError('output paths must be absolute')
    system_aliases = {'/tmp': '/private/tmp', '/var': '/private/var'}
    if any(parent.is_symlink() and system_aliases.get(str(parent)) != str(parent.resolve())
           for parent in (path, *path.parents)):
        raise ValueError('symlink output paths are not allowed')
    return path.resolve()


def publish_files(root, files):
    """Preflight the whole transaction before any write; publish each new file atomically.

    Existing identical bundles are read-only no-ops. Mixed/modified bundles are refused.
    An interrupted initial creation can leave staging files, but never replaces existing files.
    """
    root = absolute_path(str(root))
    targets = {root / name: content for name, content in files.items()}
    for path in targets:
        if not path.is_relative_to(root) or '..' in path.relative_to(root).parts:
            raise ValueError('output escapes explicit root')
        absolute_path(str(path))
    present = [path.exists() for path in targets]
    if any(present):
        if not all(present) or any(not path.is_file() or path.read_bytes() != data for path, data in targets.items()):
            raise ValueError('refusing partial or modified output bundle; choose a new output path')
        return
    root.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for path, data in targets.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.pending')
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            staged.append((temporary, path))
        for temporary, path in staged:
            # link is an atomic no-clobber publish, unlike replace; manifest is ordered last.
            os.link(temporary, path)
    except BaseException:
        # Remove only files this invocation published, never pre-existing user content.
        for temporary, path in staged:
            if temporary.exists() and path.exists() and os.path.samefile(temporary, path):
                path.unlink()
        raise
    finally:
        for temporary, _path in staged:
            temporary.unlink(missing_ok=True)


def selected_cases(value):
    if not value:
        return list(CASES)
    selected = []
    for item in value.split(','):
        if item.isdigit():
            number = int(item)
            if not 1 <= number <= len(CASES):
                raise ValueError('case numbers must be 1 through 20')
            case = CASES[number - 1]
        else:
            try:
                case = get_case(item)
            except StopIteration:
                raise ValueError('unknown case: ' + item) from None
        if case in selected:
            raise ValueError('duplicate case: ' + item)
        selected.append(case)
    return selected


def generate(args):
    models = args.models.split(',')
    if len(set(models)) != len(models) or not set(models) <= set(MODELS):
        raise ValueError('supply unique explicit supported model selectors')
    if args.max_parallel <= 0 or args.timeout_s <= 0:
        raise ValueError('parallelism and timeout must be positive')
    allowance = getattr(args, 'task_spend_allowance_gbp', None)
    if allowance is not None and (type(allowance) not in (int, float) or not math.isfinite(allowance) or allowance <= 0):
        raise ValueError('task spend allowance must be a finite positive GBP amount')
    out = absolute_path(args.out)
    cases = selected_cases(args.cases)
    contract = {'models': models, 'cases': [case_digest(case) for case in cases], 'out': str(out),
                'source': source_digest(), 'timeout_s': args.timeout_s, 'max_parallel': args.max_parallel,
                'task_spend_allowance_gbp': allowance}
    generation_id = hashlib.sha256(json_bytes(contract)).hexdigest()[:24]
    workdir = out / 'tasks'
    manifest = {'run_name': 'ringer-model-comparison', 'job_id': 'ringer-model-comparison-' + generation_id,
                'workdir': str(workdir), 'worktrees': False, 'max_parallel': args.max_parallel,
                'benchmark': {'version': 2, 'generation_id': generation_id, 'output_root': str(out),
                              'checker_source_sha256': source_digest(), 'source_hashes': source_hashes()}, 'tasks': []}
    files = {}
    for selector in models:
        engine, model = selector.split(':', 1)
        for case in cases:
            key = engine + '__' + model.replace('/', '_') + '__' + case.case_id
            taskdir = workdir / key
            command = [sys.executable, str(SCRIPT), 'check', '--case', case.case_id,
                       '--candidate', str(taskdir / 'solution.py'), '--expected-source-sha256', source_digest(),
                       '--score-dir', str(out / 'evidence'), '--cell-id', key,
                       '--generation-id', generation_id, '--require-notes']
            task = {'key': key, 'spec': case.spec, 'engine': engine, 'model': model,
                    'billing_route': 'subscription' if engine == 'codex' else 'api',
                    'task_type': case.task_type, 'timeout_s': args.timeout_s, 'max_attempts': 1,
                    'check_timeout_s': 60, 'check': shlex.join(command),
                    'expect_files': ['solution.py', 'notes.md'], 'verified': 'Fixed normal and boundary assertions, input preservation and pinned source hashes; no human quality or promotion.',
                    'source_sha256': {str(ROOT / name): sha for name, sha in source_hashes().items()},
                    'engine_args': ['-c', 'forced_login_method="chatgpt"', '-c', 'service_tier="default"',
                                    '-c', 'model_reasoning_effort="medium"'] if engine == 'codex' else [],
                    'benchmark': {'case_id': case.case_id, 'seed': case.seed, 'case_digest': case_digest(case),
                                  'spec_digest': case.spec_digest, 'check_digest': source_digest()}}
            if engine == 'opencode' and allowance is not None:
                task['task_spend_allowance_gbp'] = allowance
            manifest['tasks'].append(task)
            files[str(Path('tasks') / key / 'solution.py')] = case.starter.encode()
            files[str(Path('tasks') / key / 'notes.md')] = b''
    files['manifest.json'] = json_bytes(manifest)
    # Refuse an unrelated non-empty root even if it happens to contain no colliding target.
    if out.exists() and any(out.iterdir()) and not (out / 'manifest.json').is_file():
        raise ValueError('output directory is not an existing benchmark bundle or empty')
    publish_files(out, files)
    if allowance is None and any(task['billing_route'] == 'api' for task in manifest['tasks']):
        print('Paid API cells still require an explicit --task-spend-allowance-gbp and available policy budget before dispatch.',
              file=sys.stderr)
    print(json.dumps({'manifest': str(out / 'manifest.json'), 'cells': len(manifest['tasks']), 'generated_only': True}))
    return 0


def check(args):
    if args.timeout_s <= 0 or args.timeout_s > 10:
        raise ValueError('candidate timeout must be > 0 and <= 10 seconds per assertion')
    candidate_dir = Path(args.candidate).absolute().parent
    execution = None
    try:
        starts = list(re.finditer(r'^\[ringer.py\] attempt (\d+) started (\S+)\s*$',
                                  (candidate_dir / 'worker.log').read_text(), re.M))
        if starts:
            execution = {'taskdir': str(candidate_dir), 'attempt_index': int(starts[-1].group(1)),
                         'started_at': starts[-1].group(2)}
    except OSError:
        pass
    result = evaluate(args.case, args.candidate, args.expected_source_sha256, args.timeout_s, args.require_notes)
    result['execution'] = execution
    if args.score_dir:
        if not args.cell_id or not args.generation_id or not args.expected_source_sha256:
            raise ValueError('published evidence requires cell, generation and pinned checker identity')
        score_dir = absolute_path(args.score_dir)
        candidate = Path(args.candidate).absolute()
        if score_dir == candidate.parent or score_dir.is_relative_to(candidate.parent):
            raise ValueError('score output must be outside worker task directory')
        identifier = uuid.uuid4().hex
        result.update(cell_id=args.cell_id, generation_id=args.generation_id)
        files = {}
        if result['candidate_source_sha256'] and candidate.is_file() and digest(candidate) == result['candidate_source_sha256']:
            snapshot = score_dir / (identifier + '.candidate.py')
            result['candidate_snapshot'] = str(snapshot)
            files[snapshot.name] = candidate.read_bytes()
        path = score_dir / (identifier + '.json')
        files[path.name] = json_bytes(result)
        publish_files(score_dir, files)
        summary = {key: result[key] for key in ('checker_id', 'case_id', 'resultstate', 'checks', 'correctness_score',
                   'boundary_score', 'correctly_blocked_decisions', 'nohumanpromotion', 'failure_class')}
        summary.update(benchmark_score_path=str(path), benchmark_score_sha256=digest(path))
        print(json.dumps(summary, sort_keys=True))
    else:
        print(json.dumps(result, sort_keys=True))
    return 0 if result['resultstate'] == 'PASS' else 1


def report(args):
    from benchmarks.reliability.reporting import build_report, markdown
    manifest_path = Path(args.manifest).resolve()
    payload = build_report(manifest_path, Path(args.state_dir).resolve(), args.historical)
    output = absolute_path(args.out)
    if output.suffix != '.json':
        raise ValueError('report --out must name an absolute .json file')
    publish_files(output.parent, {output.name: json_bytes(payload), output.with_suffix('.md').name: markdown(payload).encode()})
    print(json.dumps({'report': str(output), 'markdown': str(output.with_suffix('.md')), 'cells': len(payload['cells'])}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    generator = commands.add_parser('generate')
    generator.add_argument('--out', required=True)
    generator.add_argument('--models', required=True, help='comma-separated explicit selectors')
    generator.add_argument('--cases', help='comma-separated case IDs or 1-based numbers; default all 20')
    generator.add_argument('--max-parallel', type=int, default=1)
    generator.add_argument('--timeout-s', type=int, default=180)
    generator.add_argument('--task-spend-allowance-gbp', type=float,
                           help='explicit finite positive GBP allowance per paid API cell; no default, policy caps still apply')
    generator.set_defaults(func=generate)
    checker = commands.add_parser('check')
    checker.add_argument('--case', required=True, choices=[case.case_id for case in CASES])
    checker.add_argument('--candidate', required=True)
    checker.add_argument('--expected-source-sha256')
    checker.add_argument('--timeout-s', type=float, default=2)
    checker.add_argument('--score-dir')
    checker.add_argument('--cell-id')
    checker.add_argument('--generation-id')
    checker.add_argument('--require-notes', action='store_true')
    checker.set_defaults(func=check)
    reporter = commands.add_parser('report')
    reporter.add_argument('--manifest', required=True)
    reporter.add_argument('--state-dir', required=True, help='Ringer state directory or its runs/ directory')
    reporter.add_argument('--out', required=True)
    reporter.add_argument('--historical', action='append', default=[], help='explicit historical source, kept separate')
    reporter.set_defaults(func=report)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
