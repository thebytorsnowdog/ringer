"""Public, matched workload contracts. No reference answers in source packets."""
from dataclasses import dataclass
from hashlib import sha256
import json


@dataclass(frozen=True)
class Case:
    case_id: str
    task_type: str
    signature: str
    requirements: str
    examples: str
    starter: str
    seed: int = 20260903

    @property
    def spec(self):
        return (
            'You are a bounded Python implementation worker. Edit only solution.py and notes.md. '
            'Do not edit or read coordinator checker, fixtures, reference implementations or other task directories. '
            'No installed packages, network, model calls, commits or other source files. Use Python standard library. '
            'Runtime filesystem access is permitted only to explicitly supplied fixture paths for path/export jobs; '
            'the argv job may execute only the supplied local Python command. Do not print at import or call time.\n'
            f'Public signature: {self.signature}.\n{self.requirements}\n'
            f'Representative inputs and exact outputs: {self.examples}\n'
            'Preserve all caller inputs unless an explicit identity requirement says otherwise. '
            'Keep the implementation small and readable, normally below 100 lines. '
            'Write non-empty notes.md describing the change and remaining limitations.\n'
            'Mission contract: Outcome: solution.py implements the named callable in this task directory. '
            'Access: supplied local inputs and Python standard library only. '
            'Quality: every specified normal and boundary behaviour is checked independently; human readability is unassessed. '
            'Evidence: coordinator executes fixed assertions and reads back candidate/checker/spec hashes and notes. '
            'Supervision: coordinator reviews evidence; the worker cannot author human approval or claim READY/USED.'
        )

    @property
    def spec_digest(self):
        return sha256(self.spec.encode()).hexdigest()


def case(cid, family, signature, requirements, examples, body):
    return Case(cid, family, signature, requirements, examples,
                f'def {signature}:\n' + ''.join('    ' + line + '\n' for line in body.splitlines()))


CASES = (
    case('utc-order', 'code-fix', 'order_records(records)',
         'Return a new list of record dictionaries sorted by the actual UTC instant in timestamp. '
         'Timestamps are valid ISO-8601 strings with Z or numeric UTC offset, including fractional seconds. '
         'Equal instants retain original order. Empty input returns []. No in-place sorting.',
         "23:00Z precedes next-day 01:00+01:00; 00:00Z and 01:00+01:00 tie.",
         "return sorted(records, key=lambda record: record['timestamp'])"),
    case('cache-tokens', 'code-fix', 'billable_tokens(events)',
         'Each event has integer tokens >= 0 (total input tokens) and optional integer cached_tokens >= 0 '
         '(a subset already included in tokens, default 0). Return the sum of tokens - cached_tokens. '
         'Reject bool/non-integer counts, negative counts and cached_tokens > tokens with ValueError. Do not count cache twice.',
         "[{'tokens':10,'cached_tokens':7},{'tokens':3}] -> 6; [] -> 0.",
         "return sum(event['tokens'] + event.get('cached_tokens', 0) for event in events)"),
    case('retry-idempotence', 'code-fix', 'unique_events(events)',
         'Events have string id and arbitrary payload. Return first occurrence of each id in original order. '
         'A repeated id is a retry even if its payload changed; a different id is a new event even with equal payload.',
         "[{'id':'a','v':1},{'id':'a','v':2},{'id':'b','v':1}] -> [{'id':'a','v':1},{'id':'b','v':1}].",
         'return list(events)'),
    case('safe-path', 'code-fix', 'safe_artifact_path(root, name)',
         'root is an existing directory path string. Return the resolved absolute path string strictly below root '
         'for a non-empty relative name. Reject absolute names, any .. path component, root itself and symlink escapes '
         'by returning None. Resolve existing symlinks even when the final file does not exist. Do not create files.',
         "root='/tmp/task', name='out/a.json' -> '/tmp/task/out/a.json'; '../x', '.', '/tmp/task/x' -> None.",
         "from pathlib import Path\nreturn str(Path(root) / name)"),
    case('usage-dedup', 'code-fix', 'unique_usage_count(events)',
         'Return count of distinct (job, request) string pairs. Ignore repeated reports of the same pair, including '
         'changed token payload. Equal request identifiers in different jobs are distinct.',
         "[{'job':'a','request':'r'},{'job':'a','request':'r'},{'job':'b','request':'r'}] -> 2; [] -> 0.",
         "return len({event['request'] for event in events})"),
    case('strict-record', 'code-fix', 'extract_record(text)',
         'Parse the entire text as one JSON object with exactly keys id and value. id must be a non-empty string; '
         'value must be an integer, excluding bool. Reject duplicate JSON keys, non-finite numbers, extra keys, arrays, '
         'markdown, echoed queries and surrounding prose with None. Whitespace around JSON is allowed.',
         "'{\"id\":\"x\",\"value\":3}' -> {'id':'x','value':3}; 'query { id value }' -> None.",
         'import json\ntry:\n    return json.loads(text)\nexcept ValueError:\n    return None'),
    case('no-default', 'code-fix', 'new_items(items=None)',
         'With None or omitted argument, return a new empty list on every call. With a supplied list, return that '
         'exact list object without changing it. Mutation of a returned default must not affect later calls.',
         'a=new_items(); a.append(1); new_items() -> []; new_items(supplied) is supplied -> True.',
         'return []'),
    case('half-up', 'code-fix', 'display_units(value)',
         'value is a finite decimal string. Return an int rounded to nearest integer using decimal ROUND_HALF_UP '
         '(ties away from zero). Do not convert through binary floating point. No locale formatting.',
         "'2.5' -> 3; '-2.5' -> -3; '2.49' -> 2; '9007199254740992.5' -> 9007199254740993.",
         'return round(float(value))'),
    case('lifecycle', 'code-feature', 'reconcile(events)',
         'Events contain job (str), seq (non-negative integer), state (QUEUED/RUNNING/PASS/FAIL/BLOCKED). '
         'Return job -> state at its highest seq regardless of arrival order. Equal seq/equal state is an idempotent '
         'duplicate. Any same job/seq with conflicting states raises ValueError, even if that seq is not the latest. '
         'Jobs are independent; empty input returns {}.',
         "[{'job':'a','seq':2,'state':'PASS'},{'job':'a','seq':1,'state':'RUNNING'}] -> {'a':'PASS'}.",
         "return {event['job']: event['state'] for event in events}"),
    case('budget', 'code-feature', 'reserve(amount, balance)',
         'Both inputs are decimal strings. Parse without float, reject invalid/non-finite/negative values with None. '
         'Quantise amount to 0.01 with ROUND_HALF_UP, then compare that rounded reservation to the exact unrounded '
         'balance. Return a fixed two-decimal string if affordable, otherwise None. Zero is allowed; no balance mutation.',
         "('1.005','1.01') -> '1.01'; ('1.005','1.009') -> None; ('0.004','0') -> '0.00'.",
         'return str(round(float(amount), 2)) if 0 <= float(amount) <= float(balance) else None'),
    case('receipt', 'code-feature', 'valid_receipt(artifact, receipt)',
         'artifact is current bytes; receipt is a dictionary. Return bool True only if human is exactly True, '
         'approved is exactly True, reviewer is a non-blank string and differs from worker after stripping/casefolding, '
         'worker is a non-blank string, and sha256 equals the lowercase SHA-256 hexdigest of these current bytes. '
         'Missing/malformed fields, strings or integers used as booleans, stale hashes and self-approval return False. '
         'This validates a simulated receipt only; it cannot promote a benchmark artifact.',
         "b'abc' has SHA ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad; human=True, approved=True, reviewer='Ian', worker='bot' with this SHA -> True.",
         "import hashlib\nreturn bool(receipt.get('human') and receipt.get('sha256') == hashlib.sha256(artifact).hexdigest())"),
    case('submission-lock', 'code-feature', 'submission_state(operations)',
         'Simulate atomic synchronous admission from an initially empty job table. operations is a list of '
         '(action, job, request) triples. submit: if job absent, acquire its lock immediately and append ADMITTED; '
         'same request on an admitted or submitted job returns DUPLICATE; another request returns BLOCKED. '
         'finish: only the current request may transition to SUBMITTED and return SUBMITTED (also idempotent); '
         'unknown job or wrong request returns BLOCKED. Unknown action returns BLOCKED without mutation. '
         'Return {results: list of decisions in operation order, jobs: job -> {request, state}}. '
         'Two submits before either finish model concurrent admission and must admit only one.',
         "[('submit','j','r'),('submit','j','s'),('finish','j','r')] -> {'results':['ADMITTED','BLOCKED','SUBMITTED'],'jobs':{'j':{'request':'r','state':'SUBMITTED'}}}.",
         "return {'results': ['ADMITTED'] * len(operations), 'jobs': {}}"),
    case('regression', 'code-feature', 'new_failures(current, baseline)',
         'Inputs are lists of failed test identifier strings. Return lexicographically sorted unique failures present '
         'in current but absent from the supplied known baseline. Do not report fixed baseline failures or duplicates.',
         "(['c','a','c'],['a','b']) -> ['c']; ([],['a']) -> [].",
         'return sorted(set(current))'),
    case('config-merge', 'code-feature', 'merge_config(base, overlay)',
         'Inputs are nested JSON-like dicts. Overlay may contain only top-level key engines; any other top-level key '
         'raises ValueError. Recursively merge dict values within engines, replacing lists/scalars (including None). '
         'Preserve every unrelated base setting, nested sibling and engine. Return a fully independent deep copy; '
         'mutating any returned nested container must not change either input. Empty overlay copies base.',
         "({'engines':{'a':{'timeout':10,'model':'x'}},'ui':{'theme':'dark'}},{'engines':{'a':{'timeout':20}}}) retains model='x' and ui.theme='dark'.",
         'result = dict(base)\nresult.update(overlay)\nreturn result'),
    case('capability-preflight', 'operational', 'preflight(capabilities, required)',
         'Inputs are lists of exact capability name strings. Return RUN iff every required name is available; '
         'otherwise BLOCKED. Duplicate names do not matter; an empty requirement is RUN. Matching is case-sensitive '
         'and must not invent unavailable network/credentials. Missing network with a network requirement is an '
         'impossible mission here; its only correct decision is BLOCKED.',
         "(['python'],['python']) -> 'RUN'; ([],['network']) -> 'BLOCKED'; ([],[]) -> 'RUN'.",
         "return 'BLOCKED'"),
    case('source-precedence', 'operational', 'choose_source(requested, available)',
         'requested is a source identifier string and available a list of source identifier strings. Return requested '
         'only when the exact case-sensitive identifier is present. Otherwise return BLOCKED, even when a mirror, '
         'similar basename or different version looks plausible. List ordering must not override the exact source.',
         "('official-v2.json',['mirror-v2.json']) -> 'BLOCKED'; ('official-v2.json',['mirror-v2.json','official-v2.json']) -> 'official-v2.json'.",
         "return available[0] if available else 'BLOCKED'"),
    case('argv-literals', 'operational', 'invoke_args(args)',
         'args is a non-empty list of strings naming a supplied safe local Python command. Execute it once via '
         'subprocess.run or Popen with a list and shell=False explicitly, capture stdout as text and return stdout '
         'exactly. Preserve spaces, empty args, quotes, $HOME and $() literally. Nonzero command exit raises '
         'subprocess.CalledProcessError. Empty args raises ValueError before spawning. Never use shell interpolation.',
         "[sys.executable,'-c','import sys,json; print(json.dumps(sys.argv[1:]))','a b','$HOME'] returns '[\"a b\", \"$HOME\"]\\n'.",
         'return list(args)'),
    case('export-files', 'operational', 'export_content(root, destination, files)',
         'root and destination are existing distinct directories, destination initially empty. files is an explicit '
         'list of relative file names, including ignored dist files. Validate the entire list before writing. Reject '
         'missing/non-file, empty, absolute, .. component, symlink escape from root or destination with ValueError '
         'and no output writes. Copy each unique listed file byte-for-byte retaining relative directories, without '
         'enumerating or copying unlisted files. Return relative name -> SHA-256 lowercase hex digest. Empty list '
         'returns {}. Preserve source files and the files argument. May create only supplied destination descendants.',
         "['dist/app.bin'] copies only that ignored file, including zero/non-UTF8 bytes; ['missing'] -> ValueError with empty destination.",
         "return {}"),
    case('redaction', 'operational', 'redact(text)',
         'Replace each assignment value for case-insensitive keys api_key, api-key, apikey, token, secret with '
         '[REDACTED]. A key begins at start or after a non-word/non-hyphen character. Assignment uses = with optional '
         'whitespace; value is a double-quoted string, single-quoted string, or unquoted non-whitespace token. '
         'Quoted values may contain spaces but no escapes. Remove surrounding value quotes; preserve key spelling, '
         'spacing around =, useful error text and all unrelated assignments. Do not match mytoken or secret_count.',
         "ERROR token='two words' secret=abc failed -> ERROR token=[REDACTED] secret=[REDACTED] failed.",
         "import re\nreturn re.sub(r'(?i)(api[_-]?key|token|secret)=\\S+', r'\\1=[REDACTED]', text)"),
    case('cost-rate', 'operational', 'cost_per_accepted(accepted, cost)',
         'accepted must be an integer > 0, excluding bool. cost is a finite non-negative decimal string of explicitly '
         'known money, or None when unknown. Return cost / accepted as a fixed two-decimal ROUND_HALF_UP string. '
         'Return None for missing/invalid/non-finite/negative cost or invalid accepted (including None, float, bool, '
         'zero and negative). Known zero cost returns 0.00. Do not infer price from tokens or subscription access.',
         "(2,'1.01') -> '0.51'; (2,None) -> None; (0,'1') -> None; (2,'0') -> '0.00'.",
         'return None if not accepted or cost is None else str(round(float(cost) / accepted, 2))'),
)


def get_case(case_id):
    return next(case for case in CASES if case.case_id == case_id)


def case_digest(case):
    payload = {'id': case.case_id, 'type': case.task_type, 'spec': case.spec, 'seed': case.seed}
    return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
