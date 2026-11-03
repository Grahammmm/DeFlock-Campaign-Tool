#!/usr/bin/env python3
"""Detect originals/ledger drift without repair, delivery or legal conclusions."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
from collections import Counter
from datetime import date
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import store_check
from campaign_tool.records import run as records_run
from campaign_tool.records.ledger import stages

spec = importlib.util.spec_from_file_location('_reconcile_flock', REPO / 'tools/flock_store_check.py')
if spec is None or spec.loader is None:
    raise RuntimeError('store checker unavailable')
flock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flock)


def safe_path(value, *, target=False):
    path = Path(value)
    if not path.is_absolute() or os.path.normpath(value) != value:
        raise ValueError('path must be absolute and normalized')
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('link refused')
    if path == REPO or REPO in path.parents:
        raise ValueError('inside repository')
    if target:
        if os.path.lexists(path) or not path.parent.is_dir():
            raise ValueError('output unavailable')
    elif not path.is_dir():
        raise ValueError('root unavailable')
    return path


def alert(root, code, severity='action', count=1, **details):
    return {'id': f'reconcile:{root}:{code}:1', 'source': 'reconcile-auto',
            'kind': code, 'severity': severity, 'count': count, **details}


def campaign(root):
    if (root / 'private' / 'ledger.sqlite').is_symlink():
        raise ValueError('ledger link refused')
    if not (root / 'private' / 'ledger.sqlite').is_file():
        return [alert('campaign', 'ledger-unreadable')]
    try:
        problems = store_check.check_store(root)['problems']
    except ValueError:
        return [alert('campaign', 'ledger-unreadable')]
    allowed = {'invalid_ledger_row', 'missing_file', 'not_a_regular_file', 'size_mismatch',
               'hash_mismatch', 'leftover_temp', 'no_ledger_row', 'unexpected_entry', 'loose_mode'}
    codes = Counter(p['problem'] for p in problems)
    if not set(codes) <= allowed:
        raise ValueError('unknown checker code')
    return [alert('campaign', 'object-' + code, count=n) for code, n in sorted(codes.items())]


def records(root):
    result = records_run.status(root)
    if not result['ledger']:
        return [alert('records', 'records-ledger-missing')]
    out = []
    for key, kind, severity in (('drifted_proposals', 'proposal-drifted', 'action'),
                                ('review_retirement_pending', 'review-retirement-pending', 'info')):
        if result[key]:
            out.append(alert('records', kind, severity, len(result[key])))
    if result['blocked']:
        names = sorted({x['stage'] for x in result['blocked'] if x['stage'] in stages.STAGES})
        out.append(alert('records', 'stage-blocked', count=len(result['blocked']), stages=names))
    if result['alerts']:
        # Only known structured keys can be exposed: arbitrary keys may contain private text.
        keys = set()
        for item in result['alerts']:
            key = item.get('key')
            if not isinstance(key, str):
                continue
            match = re.fullmatch(r'(extract:)([0-9a-f]{64}):[a-z0-9_,-]+', key)
            if match:
                keys.add(match.group(1) + match.group(2)[:12])
                continue
            match = re.fullmatch(r'(run:lease_recovered:)([0-9a-f]{64}):([a-z_]+)', key)
            if match and match.group(3) in stages.STAGES:
                keys.add((match.group(1) + match.group(2)[:12])[:24])
        out.append(alert('records', 'ledger-alert-open', 'info', len(result['alerts']), keys=sorted(keys)))
    return out


def flock_store(root):
    output = io.StringIO()
    args = SimpleNamespace(backups=None, max_backup_age_days=None, today=date.today(), section=None, json=False)
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
        flock.run(root, args)
    lines = output.getvalue().splitlines()
    out = []
    for section in flock.SECTIONS:
        prefix = section + ' | '
        own = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        counts = [int(x[7:]) for x in own if re.fullmatch(r'errors=\d+', x)]
        if len(counts) != 1:
            raise ValueError('section did not report')
        if any(x.startswith('failed: ') for x in own):
            out.append(alert('flock-store', 'flock-store-' + section + '-failed'))
        elif counts[0]:
            out.append(alert('flock-store', 'flock-store-' + section, count=counts[0]))
    return out


def write_new(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
    except BaseException:
        os.unlink(path)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='verb', required=True)
    check = sub.add_parser('check')
    check.add_argument('--campaign')
    check.add_argument('--records-root')
    check.add_argument('--flock-store')
    check.add_argument('--json-out')
    args = parser.parse_args(argv)
    roots = [('campaign', args.campaign, campaign), ('records', args.records_root, records),
             ('flock-store', args.flock_store, flock_store)]
    roots = [(name, value, fn) for name, value, fn in roots if value is not None]
    if not roots:
        check.error('at least one root is required')
    try:
        target = safe_path(args.json_out, target=True) if args.json_out else None
        if target and any(target == Path(value) or Path(value) in target.parents for _, value, _ in roots):
            raise ValueError('output inside root')
    except (ValueError, OSError):
        print('reconcile | refused | json-out unavailable')
        return 1
    out, failed = [], 0
    for name, value, fn in roots:
        try:
            out.extend(fn(safe_path(value)))
        except Exception:
            failed += 1
            out.append(alert(name, 'checker-failed:' + name))
    if target and not failed:
        try:
            payload = {'schema': 'flock-alerts-v1', 'review_required': True, 'alerts': out}
            write_new(target, (json.dumps(payload, sort_keys=True, indent=2) + '\n').encode())
        except OSError:
            print('reconcile | refused | json-out not written')
            return 1
    for item in out:
        print('reconcile | {id} | {kind} | {severity} | count={count}'.format(**item))
    print(f'reconcile | roots={len(roots)} alerts={len(out)} info={sum(a["severity"] == "info" for a in out)} '
          f'checkers_failed={failed} | detection only; no repair; not a legal status; the canonical ledger wins')
    return 1 if failed else (3 if out else 0)


if __name__ == '__main__':
    sys.exit(main())
