#!/usr/bin/env python3
"""Assess public catalog metadata only: stdlib, no inference, no credentials."""
import argparse
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import html
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import urllib.request
import uuid

URL = 'https://openrouter.ai/api/v1/models'
FORMAT = 'ringer-public-catalog-v1\n'
ROLES = ('agentic_coding', 'structured_extraction', 'reasoning', 'vision_analysis',
         'text_drafting', 'long_context', 'image_generation', 'audio_generation')
TOKEN_PRICES = {'prompt', 'completion', 'input_cache_read', 'input_cache_write',
                'input_cache_write_1h', 'internal_reasoning'}
SOURCES = ['https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties',
           'https://learn.chatgpt.com/docs/auth']
ALIASES = ('catalog.json', 'assessment.json', 'assessment.md')


def utc_now():
    return datetime.now(timezone.utc)


def stamp(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')


def reject_constant(value):
    raise ValueError('non-standard JSON number: ' + value)


class JsonFraction(str):
    """Preserve JSON fractional-number lexemes without lossy binary floats.

    Raw bytes remain the authoritative representation; row raw fields encode these
    lexemes as strings. They must not be accepted as IDs or metadata strings.
    """


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key: ' + key)
        result[key] = value
    return result


def validate(raw):
    """Reject invalid envelopes/identities, preserve incomplete entry metadata."""
    payload = json.loads(raw, parse_constant=reject_constant, parse_float=JsonFraction,
                         object_pairs_hook=unique_keys)
    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list) or not payload['data']:
        raise ValueError('expected a non-empty data array')
    if 'error' in payload:
        raise ValueError('API error envelope is not a catalog')
    ids = set()
    for entry in payload['data']:
        if not isinstance(entry, dict) or type(entry.get('id')) is not str or not entry['id'].strip():
            raise ValueError('every catalog entry requires a non-empty string id')
        if entry['id'] in ids:
            raise ValueError('duplicate model id: ' + entry['id'])
        ids.add(entry['id'])
    if 'total_count' in payload:
        count = payload['total_count']
        if type(count) is not int or count != len(ids):
            raise ValueError('total_count does not match the full data array')
    # This command promises a full catalog; never silently assess a paginated subset.
    links = payload.get('links')
    if links is not None and (not isinstance(links, dict) or links.get('next')):
        raise ValueError('malformed links or incomplete paginated catalog')
    return payload


def strings(value):
    if not isinstance(value, list) or any(type(v) is not str or not v for v in value):
        return None
    return sorted(set(value))


def price(value, token=False):
    result = {'raw': value, 'raw_is_json_fraction_lexeme': isinstance(value, JsonFraction),
              'status': 'unknown', 'usd_per_million_tokens': None,
              'catalog_amount': None, 'unit': 'USD/token' if token else 'catalog native; unit not inferred'}
    if value is None:
        return result
    if isinstance(value, (dict, list)):
        result['status'] = 'conditional_or_malformed'
        return result
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        result['status'] = 'malformed'
        return result
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        result['status'] = 'malformed'
        return result
    if not number.is_finite():
        result['status'] = 'non_finite'
    elif number < 0:
        result['status'] = 'negative_or_variable_sentinel'
    elif abs(number.as_tuple().exponent) > 1000 or len(number.as_tuple().digits) > 1000:
        # Avoid decimal overflow or unbounded arithmetic on malformed public metadata.
        result['status'] = 'out_of_range'
    else:
        result['status'] = 'known'
        result['catalog_amount'] = str(number)
        if token:
            with localcontext() as ctx:
                ctx.prec = max(28, len(number.as_tuple().digits) + 8)
                result['usd_per_million_tokens'] = str(number * Decimal(1000000))
    return result


def expiry(value, now):
    if value is None:
        return 'not_advertised'
    try:
        if type(value) is not str:
            raise ValueError('not a string')
        if len(value) == 10:
            # Conservatively exclude from the start of the advertised expiry date, UTC.
            expired = date.fromisoformat(value) <= now.date()
        else:
            instant = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if instant.tzinfo is None:
                raise ValueError('timezone missing')
            expired = instant <= now
        return 'expired' if expired else 'future_expiration'
    except ValueError:
        return 'unknown_malformed'


def assess_entry(entry, now):
    arch = entry.get('architecture')
    arch = arch if isinstance(arch, dict) else {}
    inputs, outputs = strings(arch.get('input_modalities')), strings(arch.get('output_modalities'))
    params = strings(entry.get('supported_parameters'))
    context = entry.get('context_length')
    context = context if type(context) is int and context > 0 else None
    status = expiry(entry.get('expiration_date'), now)
    current = status not in ('expired', 'unknown_malformed')
    text_in, text_out = 'text' in (inputs or []), 'text' in (outputs or [])
    p = set(params or [])
    requirements = {
        'agentic_coding': [(text_in, 'text input advertised'), (text_out, 'text output advertised'),
                           ('tools' in p, 'tools advertised'), (context is not None and context >= 32000, 'context >=32000')],
        'structured_extraction': [(text_in, 'text input advertised'), (text_out, 'text output advertised'),
                                  (bool(p & {'response_format', 'structured_outputs'}), 'response_format or structured_outputs advertised')],
        'reasoning': [(bool(p & {'reasoning', 'reasoning_effort', 'include_reasoning'}), 'reasoning parameter advertised')],
        'vision_analysis': [('image' in (inputs or []), 'image input advertised'), (text_out, 'text output advertised')],
        'text_drafting': [(text_in, 'text input advertised'), (text_out, 'text output advertised')],
        'long_context': [(context is not None and context >= 128000, 'context >=128000')],
        'image_generation': [('image' in (outputs or []), 'image output advertised')],
        'audio_generation': [('audio' in (outputs or []), 'audio output advertised')],
    }
    roles = {}
    for role, checks in requirements.items():
        eligible = all(ok for ok, _reason in checks)
        roles[role] = {'capability_eligible': eligible, 'current_candidate': eligible and current,
                       'reasons': [('met: ' if ok else 'not established: ') + reason for ok, reason in checks]}
        if not current:
            roles[role]['reasons'].append('excluded from current candidates: ' + status)
    pricing = entry.get('pricing')
    pricing = pricing if isinstance(pricing, dict) else {}
    prices = {key: price(value, key in TOKEN_PRICES) for key, value in sorted(pricing.items())}
    for key in ('prompt', 'completion'):
        prices.setdefault(key, price(None, True))
    return {'id': entry['id'], 'canonical_slug': entry.get('canonical_slug'),
            'context_length': context, 'context_length_raw': entry.get('context_length'),
            'supported_parameters': params, 'supported_parameters_raw': entry.get('supported_parameters'),
            'input_modalities': inputs, 'output_modalities': outputs, 'architecture_raw': entry.get('architecture'),
            'expiration_date': entry.get('expiration_date'), 'expiration_status': status,
            'pricing_metadata_status': 'advertised' if isinstance(entry.get('pricing'), dict) else 'unknown_or_malformed',
            'prices': prices, 'pricing_raw': entry.get('pricing'),
            'non_token_or_unspecified_fees': {k: v for k, v in prices.items() if k not in TOKEN_PRICES},
            'route_free_status': 'unknown; token prices alone do not establish a free route',
            'roles': roles, 'empirical_quality': 'UNTESTED',
            'metadata_issues': [name + ' missing or malformed' for name, value in
                                [('context_length', context), ('supported_parameters', params),
                                 ('input_modalities', inputs), ('output_modalities', outputs)] if value is None]}


def token_cost(row):
    prices = [row['prices'][key]['usd_per_million_tokens'] for key in ('prompt', 'completion')]
    if any(value is None for value in prices):
        return None
    numbers = [Decimal(value) for value in prices]
    with localcontext() as ctx:
        ctx.prec = max(28, *(len(n.as_tuple().digits) + abs(n.as_tuple().exponent) + 8 for n in numbers))
        return sum(numbers, Decimal(0))


def assessment(payload, raw, source, now):
    rows = sorted((assess_entry(entry, now) for entry in payload['data']), key=lambda row: row['id'])
    rankings = {}
    for role in ROLES:
        candidates = [row for row in rows if row['roles'][role]['current_candidate']]
        candidates.sort(key=lambda row: (token_cost(row) is None, token_cost(row) or Decimal(0),
                                         -(row['context_length'] or 0), row['id']))
        rankings[role] = [{'id': row['id'], 'order': index,
                           'token_cost_proxy_usd': str(token_cost(row)) if token_cost(row) is not None else None,
                           'context_length': row['context_length'],
                           'non_token_fees_included': False, 'empirical_quality': 'UNTESTED'}
                          for index, row in enumerate(candidates, 1)]
    return {'schema_version': 1, 'assessed_at_utc': stamp(now), 'source': source,
            'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'coverage': {'catalog_entries': len(payload['data']), 'assessed_entries': len(rows),
                         'unique_ids': len(rows), 'catalog_reported_total': payload.get('total_count'),
                         'ids_sha256': hashlib.sha256(json_bytes([row['id'] for row in rows])).hexdigest(),
                         'candidate_counts': {role: len(ranking) for role, ranking in rankings.items()}},
            'ranking_criteria': 'Within each advertised role only: known prompt+completion USD per million token sum ascending (1M input + 1M output proxy), unknown cost last, context descending, exact ID ascending. Not total task cost; ignores non-token, override and cache fees. No quality or best-model ranking.',
            'limitations': ['Snapshot capability inference only; empirical quality is UNTESTED for every entry.',
                            'User-specific access, providers, quotas, effective prices and availability are unknown.',
                            'No inference, credentials, spending, model selection or human promotion.'],
            'sources': SOURCES, 'models': rows, 'role_candidates': rankings}


def cell(value):
    if value is None:
        return 'unknown'
    if not isinstance(value, str):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return html.escape(value).replace('|', '&#124;').replace('\r', ' ').replace('\n', ' ')


def markdown(report):
    lines = ['# Public OpenRouter catalog assessment', '',
             'Capability inference only. Empirical quality: **UNTESTED for every model**. No benchmark winner or promotion.', '',
             'Source: ' + cell(report['source']), '', 'Raw JSON SHA-256: `' + report['raw_sha256'] + '`', '',
             'Coverage: ' + cell(report['coverage']), '', report['ranking_criteria'], '',
             'User-specific access, quotas, provider availability and effective costs are unknown. Token zero does not mean a free route.', '']
    for role, ranking in report['role_candidates'].items():
        lines.extend(['## ' + role, '', 'Candidate order: ' + (', '.join(cell(r['id']) for r in ranking) or 'none'), ''])
    lines.extend(['## Every catalog entry', '',
                  '| ID | Context | Input → output | Parameters | Pricing (USD; token amounts per million) | Non-token/unspecified fees | Expiration | Roles and reasons | Empirical quality |',
                  '|---|---:|---|---|---|---|---|---|---|'])
    for row in report['models']:
        prices = {k: {'status': v['status'], 'usd_per_million_tokens': v['usd_per_million_tokens'],
                      'raw': v['raw']} for k, v in row['prices'].items() if k in TOKEN_PRICES}
        reasons = {k: {'candidate': v['current_candidate'], 'reasons': v['reasons']} for k, v in row['roles'].items()}
        values = [row['id'], row['context_length'], cell(row['input_modalities']) + ' → ' + cell(row['output_modalities']),
                  row['supported_parameters'], prices, row['non_token_or_unspecified_fees'],
                  row['expiration_status'] + ': ' + str(row['expiration_date']), reasons, row['empirical_quality']]
        # Modalities have already been escaped; escaping twice is harmless but avoid it for clarity.
        rendered = [cell(value) for value in values]
        rendered[2] = values[2]
        lines.append('| ' + ' | '.join(rendered) + ' |')
    lines.extend(['', 'Sources: ' + ', '.join('[' + url + '](' + url + ')' for url in SOURCES), ''])
    return '\n'.join(lines).encode('utf-8')


def fetch(timeout):
    # Public unauthenticated GET only. Redirects are refused; no env/config/key reads.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError('catalog redirect refused')
    request = urllib.request.Request(URL, headers={'Accept': 'application/json', 'User-Agent': 'ringer-public-catalog/1'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError('catalog response status must be 200')
        raw = response.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise ValueError('catalog response exceeds 32 MiB safety limit')
    return raw, utc_now()


def write_bundle(root, raw, report):
    """Immutable bundle plus one atomic pointer switch; prior snapshot is untouched.

    Stable root aliases resolve through current, so failed preparation never updates
    report freshness. Readers needing a consistent multi-file view pin current once.
    """
    root = Path(root).absolute()
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError('symlink output directory or parent refused')
    initial = not root.exists() or (root.is_dir() and not any(root.iterdir()))
    if not initial:
        if not root.is_dir() or (root / '.catalog-format').read_text() != FORMAT:
            raise ValueError('output is not an owned catalog directory')
        for name in ALIASES:
            if not (root / name).is_symlink() or os.readlink(root / name) != 'current/' + name:
                raise ValueError('modified output alias: ' + name)
        current = root / 'current'
        if not current.is_symlink() or not os.readlink(current).startswith('snapshots/'):
            raise ValueError('modified current pointer')
        if not current.resolve().is_relative_to(root / 'snapshots') or (root / 'snapshots').is_symlink():
            raise ValueError('snapshot pointer escapes output')
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.catalog-pending-', dir=root.parent))
    published = None
    pointer = None
    try:
        bundle_name = report['raw_sha256'] + '-' + uuid.uuid4().hex
        bundle = staging / 'snapshots' / bundle_name
        bundle.mkdir(parents=True)
        for name, data in {'catalog.json': raw, 'assessment.json': json_bytes(report), 'assessment.md': markdown(report)}.items():
            path = bundle / name
            with path.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            path.chmod(0o444)
        if initial:
            (staging / '.catalog-format').write_text(FORMAT)
            for name in ALIASES:
                (staging / name).symlink_to('current/' + name)
            (staging / 'current').symlink_to('snapshots/' + bundle_name)
            # rename replaces an empty directory atomically, or creates a new one.
            os.rename(staging, root)
        else:
            published = root / 'snapshots' / bundle_name
            os.rename(bundle, published)
            pointer = root / ('.current-' + uuid.uuid4().hex)
            pointer.symlink_to('snapshots/' + bundle_name)
            os.replace(pointer, root / 'current')  # commit point
            published = None
    finally:
        if pointer is not None:
            pointer.unlink(missing_ok=True)
        if published is not None:
            shutil.rmtree(published)
        if staging.exists():
            shutil.rmtree(staging)


def run(source, out, refresh=False, timeout=30):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    if refresh:
        raw, retrieved = fetch(timeout)
        provenance = {'kind': 'public_refresh', 'url': URL, 'retrieved_at_utc': stamp(retrieved),
                      'freshness': 'retrieved_at_utc is verified for this fetch; recheck age before selection'}
        now = retrieved
    else:
        raw = Path(source).read_bytes()
        now = utc_now()
        provenance = {'kind': 'local_file', 'path': str(Path(source).absolute()), 'retrieved_at_utc': None,
                      'freshness': 'unknown retrieval time; assessed_at_utc is not catalog freshness'}
    payload = validate(raw)
    report = assessment(payload, raw, provenance, now)
    write_bundle(out, raw, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--refresh', action='store_true')
    mode.add_argument('--source', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--timeout', type=float, default=30, help='public GET timeout in seconds (default 30)')
    args = parser.parse_args(argv)
    try:
        report = run(args.source, args.out, args.refresh, args.timeout)
    except (OSError, ValueError, InvalidOperation) as exc:
        print('Catalog assessment failed; previous snapshot remains unchanged: ' + str(exc), file=sys.stderr)
        return 1
    print(json.dumps({'out': str(args.out.absolute()), 'coverage': report['coverage'], 'raw_sha256': report['raw_sha256'],
                      'source': report['source'], 'empirical_quality': 'UNTESTED'}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
