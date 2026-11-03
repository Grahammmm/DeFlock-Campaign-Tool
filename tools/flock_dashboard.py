#!/usr/bin/env python3
"""Build a private, static, review-required snapshot from recorded fields."""
import argparse
import contextlib
import datetime
import html
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
sys.path.insert(0, str(REPO))
from campaign_tool.law import package_status  # noqa: E402


def tool(name):
    spec = importlib.util.spec_from_file_location('_dashboard_' + name, TOOLS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state, today, publish = (tool(n) for n in ('flock_state', 'flock_today', 'flock_publish'))
BANNED = re.compile(r'\b(late|overdue)\b|violat|unlawful|illegal|non-?complian|breach', re.I)
SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,39}')
SEND = {'email-drafted', 'approval-unverified', 'sent-unverified', 'run-inconsistent'}
PUB = {'pending', 'partly-approved', 'drifted', 'invalid'}


def label(value):
    value = str(value)
    return value if SAFE.fullmatch(value) and not BANNED.search(value) else '(unsafe-label)'


def esc(value):
    return html.escape(str(value), quote=True)


def source(errors, name, fn):
    try:
        return fn()
    except Exception as exc:  # a source failure must not make another section look empty
        errors.append({'source': name, 'error': type(exc).__name__})
        return None


def collect_state(store):
    reqs, runs = state.collect(store)
    if any(s in {'run-malformed', 'link-refused'} or not mapped for _, _, s, mapped in runs): raise ValueError('state source incomplete')
    return ([{'agency': a, 'request_id': i, 'state': s, 'sent_date': 'NOT RECORDED', 'next_step': 'NOT RECORDED'} for a, i, s, _ in reqs],
            [{'agency': a, 'run': d, 'state': s} for a, d, s, _ in runs])


def collect_publish(store):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = publish.status(store)
    lines = out.getvalue().splitlines()
    if code == 2 or not lines or not lines[-1].startswith('publish | runs='):
        raise ValueError('publish source unavailable')
    rows = []
    for line in lines[:-1]:
        parts = line.split(' | ')
        if len(parts) != 5 or parts[0] != 'publish':
            raise ValueError('publish source malformed')
        counts = dict(x.split('=', 1) for x in parts[4].split())
        if parts[3] == 'link-refused':
            raise ValueError('publish source refused link')
        if parts[3] not in PUB | {'approved', 'no-export'}:
            raise ValueError('publish source stage')
        rows.append({'agency': label(parts[1]), 'run': label(parts[2]), 'stage': parts[3],
                     'pending': int(counts['pending']) if counts['pending'].isdigit() else 'NOT RECORDED'})
    return rows


def collect_reminders(store, date):
    if (store / 'agencies').is_symlink() or not (store / 'agencies').is_dir():
        raise ValueError('agencies unavailable')
    items = sorted(today.reminders(store, date), key=lambda i: (i[0], i[2], i[3], i[4]))
    return [{'kind': x[1], 'date': label(x[2]), 'agency': label(x[3]), 'request_id': label(x[4])} for x in items]


def collect_alerts(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1048576:
        raise ValueError('alerts unavailable')
    with path.open(encoding='utf-8') as f:
        doc = json.load(f)
    if not isinstance(doc, dict) or doc.get('schema') != 'flock-alerts-v1' or doc.get('review_required') is not True:
        raise ValueError('alerts schema')
    rows = doc.get('alerts')
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError('alerts list')
    keys = ('id', 'kind', 'source', 'stage', 'agency', 'request_id')
    if any(not isinstance(r, dict) or any(not isinstance(r.get(k), str) for k in keys[:3]) or
           any(k in r and not isinstance(r[k], str) for k in keys[3:]) for r in rows):
        raise ValueError('alerts row')
    return [{k: label(r[k]) for k in keys if k in r} for r in rows]


def output_path(value, store):
    out = Path(value)
    if not out.is_absolute() or str(out) != os.path.normpath(str(out)) or os.path.lexists(out):
        raise ValueError('output path invalid or exists')
    if any(p.is_symlink() for p in (out.parent, *out.parent.parents)) or not out.parent.is_dir():
        raise ValueError('output parent unavailable')
    if out == REPO or REPO in out.parents or out == store / 'agencies' or store / 'agencies' in out.parents:
        raise ValueError('output path prohibited')
    return out


def write(out, page, data):
    out.mkdir(mode=0o700)
    made = []
    try:
        for name, text in (('index.html', page), ('dashboard.json', data)):
            path = out / name
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            made.append(path)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(text)
    except BaseException:
        for path in made:
            path.unlink()
        out.rmdir()
        raise


def table(rows, keys):
    return '<table><thead><tr>' + ''.join('<th>' + esc(k.replace('_', ' ')) + '</th>' for k in keys) + \
           '</tr></thead><tbody>' + ''.join('<tr>' + ''.join('<td>' + esc(r.get(k, 'NOT RECORDED')) + '</td>'
           for k in keys) + '</tr>' for r in rows) + '</tbody></table>'


def page(data):
    s, errors = data['sections'], {e['source']: e['error'] for e in data['source_errors']}
    def fail(name):
        return '<p>NOT CURRENT: source %s failed (%s)</p>' % (esc(name), esc(errors[name]))
    def group(title, name, rows, keys, command):
        body = fail(name) if name in errors else table(rows, keys) if rows else '<p>nothing waiting (source read: %s)</p>' % esc(name)
        return '<h3>' + title + '</h3>' + body + '<p>' + esc(command) + '</p>'
    text = '<!doctype html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="default-src &#x27;none&#x27;; style-src &#x27;unsafe-inline&#x27;"><style>body{font-family:sans-serif}table{border-collapse:collapse}td,th{border:1px solid;padding:.3em}</style></head><body>'
    text += '<p>Private. As of %s. Rebuild before use; this page does not update itself. Recorded fields only; the canonical ledger wins; not a legal status.</p>' % esc(data['as_of'])
    text += '<section id="waiting"><h2>Waiting on a person</h2><p class="src">Sources: flock_state, flock_publish, law package</p>'
    text += group('Approve what goes out', 'flock_state', s['waiting_send'], ('agency', 'run', 'state'), 'flock_approve.py payload <RUNDIR>')
    text += group('Approve what gets published', 'flock_publish', s['waiting_publish'], ('agency', 'run', 'stage', 'pending'), 'flock_publish.py status STORE')
    text += '<h3>Approve the law</h3>' + (fail('law') if 'law' in errors else '<p>Law package: %s. %s</p>' % (esc(s['law']), 'Law package is draft; dates on this page are provisional reminders, not legal due dates.' if s['law'] == 'draft' else 'nothing waiting (source read: law). Dates on this page are provisional reminders, not legal due dates.')) + '<p>python3 -m campaign_tool.law show JURISDICTION</p></section>'
    text += '<section id="requests"><h2>Where every request stands</h2><p class="src">Source: flock_state</p>'
    keys = ('agency', 'request_id', 'state', 'sent_date', 'next_step')
    text += fail('flock_state') if 'flock_state' in errors else '<p>open: %s</p>' % s['open_count'] + table(s['requests'], keys) + '<details><summary>Closed and withdrawn</summary>' + table(s['closed'], keys) + '</details>'
    text += '</section><section id="reminders"><h2>Reminders</h2><p class="src">Sources: flock_today, flock_facts</p>'
    text += fail('flock_today') if 'flock_today' in errors else table(s['reminders'], ('kind', 'date', 'agency', 'request_id')) if s['reminders'] else '<p>no reminders (source read: flock_today)</p>'
    text += ''.join(fail(n) for n in ('flock_facts', 'flock_sweep') if n in errors) + '<p>provisional; not a legal status</p></section><section id="alerts"><h2>Pipeline alerts</h2><p class="src">Source: flock-alerts-v1</p>'
    text += fail('alerts') if 'alerts' in errors else '<p>alerts: not provided</p>' if s['alerts'] is None else table(s['alerts'], ('id', 'kind', 'source', 'stage', 'agency', 'request_id')) if s['alerts'] else '<p>0 recorded alerts (source read: alerts)</p>'
    return text + '</section></body></html>\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['build'])
    for flag in ('store', 'date', 'out'):
        parser.add_argument('--' + flag, required=True)
    for flag in ('watchlist', 'roster', 'alerts'):
        parser.add_argument('--' + flag)
    parser.add_argument('--jurisdiction', default='us-ca')
    a = parser.parse_args(argv)
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', a.date) or datetime.date.fromisoformat(a.date).isoformat() != a.date:
            raise ValueError()
    except ValueError:
        parser.error('--date must be YYYY-MM-DD')
    if bool(a.watchlist) != bool(a.roster):
        parser.error('--watchlist and --roster must be paired')
    try:
        store = Path(a.store).resolve()
        if store == REPO or REPO in store.parents or not store.is_dir():
            raise ValueError('store invalid')
        out = output_path(a.out, store)
    except (ValueError, OSError) as exc:
        print('dashboard: ' + str(exc), file=sys.stderr)
        return 1
    errors = []
    collected = source(errors, 'flock_state', lambda: collect_state(store))
    reqs, runs = collected if collected is not None else ([], [])
    pubs = source(errors, 'flock_publish', lambda: collect_publish(store)) or []
    reminders = source(errors, 'flock_today', lambda: collect_reminders(store, a.date)) or []
    source(errors, 'flock_facts', lambda: today.unreviewed(store))  # FACTS read only to prove it is current
    if a.watchlist:
        source(errors, 'flock_sweep', lambda: today.reviews(store, a.watchlist, a.roster, a.date))
    alerts = source(errors, 'alerts', lambda: collect_alerts(a.alerts)) if a.alerts else None
    def law():
        status, error = package_status(a.jurisdiction)
        if error or status not in ('draft', 'reviewed'):
            raise ValueError('law source unavailable')
        return status
    law_status = source(errors, 'law', law)
    closed = [r for r in reqs if r['state'] in ('closed', 'withdrawn')]
    opened = [r for r in reqs if r['state'] not in ('closed', 'withdrawn')]
    sends = sorted((r for r in runs if r['state'] in SEND), key=lambda r: (r['state'] != 'approval-unverified', r['agency'], r['run']))
    waiting = [r for r in pubs if r['stage'] in PUB]
    sections = {'waiting_send': sends, 'waiting_publish': waiting, 'law': law_status, 'requests': opened,
                'closed': closed, 'open_count': len(opened), 'reminders': reminders, 'alerts': alerts}
    data = {'schema': 'flock-dashboard-v1', 'review_required': True, 'as_of': a.date, 'sections': sections, 'source_errors': errors}
    try:
        write(out, page(data), json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + '\n')
    except OSError as exc:
        print('dashboard: output failed (%s)' % type(exc).__name__, file=sys.stderr)
        return 1
    print('dashboard | requests=%d waiting_send=%d waiting_publish=%d law=%s reminders=%d source_errors=%d | review-required; private' %
          (len(reqs), len(sends), len(waiting), law_status or 'NOT CURRENT', len(reminders), len(errors)))
    return int(bool(errors))


if __name__ == '__main__':
    sys.exit(main())
