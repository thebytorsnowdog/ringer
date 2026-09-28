"""Known-good replay implementations, coordinator-only; never copied to task packets."""
import textwrap

REFERENCE = {
'utc-order': '''
from datetime import datetime
def order_records(records):
    return sorted(records, key=lambda row: datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')))
''',
'cache-tokens': '''
def billable_tokens(events):
    total = 0
    for event in events:
        tokens, cached = event['tokens'], event.get('cached_tokens', 0)
        if type(tokens) is not int or type(cached) is not int or not 0 <= cached <= tokens:
            raise ValueError('invalid token counts')
        total += tokens - cached
    return total
''',
'retry-idempotence': '''
def unique_events(events):
    seen, result = set(), []
    for event in events:
        if event['id'] not in seen:
            seen.add(event['id'])
            result.append(event)
    return result
''',
'safe-path': '''
from pathlib import Path
def safe_artifact_path(root, name):
    root = Path(root).resolve()
    name = Path(name) if name else None
    if name is None or name.is_absolute() or '..' in name.parts:
        return None
    target = (root / name).resolve()
    return str(target) if root in target.parents else None
''',
'usage-dedup': '''
def unique_usage_count(events):
    return len({(row['job'], row['request']) for row in events})
''',
'strict-record': '''
import json
def extract_record(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        value = json.loads(text, object_pairs_hook=pairs)
    except ValueError:
        return None
    if not isinstance(value, dict) or set(value) != {'id', 'value'}:
        return None
    if not isinstance(value['id'], str) or not value['id'] or type(value['value']) is not int:
        return None
    return value
''',
'no-default': '''
def new_items(items=None):
    return [] if items is None else items
''',
'half-up': '''
from decimal import Decimal, ROUND_HALF_UP
def display_units(value):
    return int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP))
''',
'lifecycle': '''
def reconcile(events):
    seen, latest = {}, {}
    for event in events:
        job, seq, state = event['job'], event['seq'], event['state']
        if (job, seq) in seen and seen[job, seq] != state:
            raise ValueError('conflicting event')
        seen[job, seq] = state
        if job not in latest or seq > latest[job][0]:
            latest[job] = (seq, state)
    return {job: item[1] for job, item in latest.items()}
''',
'budget': '''
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
def reserve(amount, balance):
    try:
        amount, balance = Decimal(amount), Decimal(balance)
        if not amount.is_finite() or not balance.is_finite() or amount < 0 or balance < 0:
            return None
        rounded = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        return format(rounded, '.2f') if rounded <= balance else None
    except InvalidOperation:
        return None
''',
'receipt': '''
import hashlib
def valid_receipt(artifact, receipt):
    reviewer, worker = receipt.get('reviewer'), receipt.get('worker')
    return (receipt.get('human') is True and receipt.get('approved') is True
            and isinstance(reviewer, str) and bool(reviewer.strip())
            and isinstance(worker, str) and bool(worker.strip())
            and reviewer.strip().casefold() != worker.strip().casefold()
            and receipt.get('sha256') == hashlib.sha256(artifact).hexdigest())
''',
'submission-lock': '''
def submission_state(operations):
    jobs, results = {}, []
    for action, job, request in operations:
        current = jobs.get(job)
        result = 'BLOCKED'
        if action == 'submit':
            if current is None:
                jobs[job] = {'request': request, 'state': 'ADMITTED'}
                result = 'ADMITTED'
            elif current['request'] == request:
                result = 'DUPLICATE'
        elif action == 'finish' and current is not None and current['request'] == request:
            current['state'] = 'SUBMITTED'
            result = 'SUBMITTED'
        results.append(result)
    return {'results': results, 'jobs': jobs}
''',
'regression': '''
def new_failures(current, baseline):
    return sorted(set(current) - set(baseline))
''',
'config-merge': '''
from copy import deepcopy
def merge_config(base, overlay):
    if set(overlay) - {'engines'}:
        raise ValueError('outside engines scope')
    def merge(left, right):
        result = deepcopy(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = deepcopy(value)
        return result
    return merge(base, overlay)
''',
'capability-preflight': '''
def preflight(capabilities, required):
    return 'RUN' if set(required) <= set(capabilities) else 'BLOCKED'
''',
'source-precedence': '''
def choose_source(requested, available):
    return requested if requested in available else 'BLOCKED'
''',
'argv-literals': '''
import subprocess
def invoke_args(args):
    if not args:
        raise ValueError('empty command')
    return subprocess.run(list(args), shell=False, check=True, capture_output=True, text=True).stdout
''',
'export-files': '''
import hashlib
from pathlib import Path
def export_content(root, destination, files):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    validated = {}
    for name in files:
        relative = Path(name)
        if not name or relative.is_absolute() or '..' in relative.parts:
            raise ValueError('unsafe name')
        source, target = (root / relative).resolve(), (destination / relative).resolve()
        if root not in source.parents or destination not in target.parents or not source.is_file():
            raise ValueError('unsafe source or target')
        validated[name] = (source, target)
    result = {}
    for name, (source, target) in validated.items():
        content = source.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        result[name] = hashlib.sha256(content).hexdigest()
    return result
''',
'redaction': '''
import re
def redact(text):
    pattern = r''' + "'''" + r'''(?i)(?<![\w-])(api[_-]?key|token|secret)(\s*=\s*)(?:"[^"]*"|'[^']*'|\S+)''' + "'''" + '''
    return re.sub(pattern, lambda match: match.group(1) + match.group(2) + '[REDACTED]', text)
''',
'cost-rate': '''
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
def cost_per_accepted(accepted, cost):
    if type(accepted) is not int or accepted <= 0 or not isinstance(cost, str):
        return None
    try:
        cost = Decimal(cost)
        if not cost.is_finite() or cost < 0:
            return None
        return format((cost / accepted).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP), '.2f')
    except InvalidOperation:
        return None
''',
}
REFERENCE = {key: textwrap.dedent(value).lstrip() for key, value in REFERENCE.items()}

# Fixed plausible defects, independent of the starter strings. Every variant executes.
BAD = {
'utc-order': "def order_records(records): return sorted(records, key=lambda row: row['timestamp'])\n",
'cache-tokens': "def billable_tokens(events): return sum(row['tokens'] for row in events)\n",
'retry-idempotence': "def unique_events(events): return list({row['id']: row for row in events}.values())\n",
'safe-path': "from pathlib import Path\ndef safe_artifact_path(root, name):\n p=Path(root)/name\n return str(p.absolute()) if '..' not in Path(name).parts else None\n",
'usage-dedup': "def unique_usage_count(events): return len({row['request'] for row in events})\n",
'strict-record': "import json\ndef extract_record(text):\n try:\n  row=json.loads(text); return row if isinstance(row,dict) and {'id','value'}<=row.keys() else None\n except ValueError: return None\n",
'no-default': "shared=[]\ndef new_items(items=None): return shared if items is None else items\n",
'half-up': "def display_units(value): return round(float(value))\n",
'lifecycle': "def reconcile(events): return {row['job']:row['state'] for row in events}\n",
'budget': "def reserve(amount,balance): return round(float(amount),2) if 0<=float(amount)<=float(balance) else None\n",
'receipt': "import hashlib\ndef valid_receipt(artifact,receipt): return bool(receipt.get('human') and receipt.get('sha256')==hashlib.sha256(artifact).hexdigest())\n",
'submission-lock': REFERENCE['submission-lock'].replace("if current is None:", "if current is None or current['state'] == 'ADMITTED':"),
'regression': "def new_failures(current,baseline): return sorted(set(current)^set(baseline))\n",
'config-merge': REFERENCE['config-merge'].replace("result = deepcopy(left)", "result = dict(left)").replace("result[key] = deepcopy(value)", "result[key] = value"),
'capability-preflight': "def preflight(capabilities,required): return 'BLOCKED'\n",
'source-precedence': "def choose_source(requested,available): return 'BLOCKED'\n",
'argv-literals': "def invoke_args(args): return str(args)\n",
'export-files': REFERENCE['export-files'].replace("for name in files:", "for name in [str(path.relative_to(root)) for path in root.rglob('*') if path.is_file()]:"),
'redaction': "import re\ndef redact(text): return re.sub(r'(?i)(api[_-]?key|token|secret)=\\S+',r'\\1=[REDACTED]',text)\n",
'cost-rate': "def cost_per_accepted(accepted,cost): return None if not accepted or cost is None else format(float(cost)/accepted,'.2f')\n",
}
