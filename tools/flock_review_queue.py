#!/usr/bin/env python3
"""Show a private review queue; machine validation is not human approval."""
import argparse
import collections
import contextlib
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]

def load(name):
    spec = importlib.util.spec_from_file_location('_queue_' + name, Path(__file__).with_name('flock_' + name + '.py'))
    if spec is None or spec.loader is None:
        raise ImportError('required flock module unavailable')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

publish = load('publish')
verify = load('verify')


def inside(path, root):
    return path == root or root in path.parents


def linked(path):
    return any(p.is_symlink() for p in (path, *path.parents))


def safe_name(value):
    return value if re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) else '?'


def fenced(text):
    # A longer fence than any source backtick run keeps raw HTML and fake headings inert.
    fence = '`' * max(3, max((len(x) for x in re.findall(r'`+', str(text))), default=0) + 1)
    return fence + '\n' + str(text) + '\n' + fence


def output_path(raw, store):
    out = Path(raw)
    if not out.is_absolute() or os.path.normpath(raw) != raw or os.path.lexists(out):
        raise ValueError('output must be an absent normalized absolute path')
    if linked(out.parent) or not out.parent.is_dir():
        raise ValueError('output parent unavailable')
    if inside(out, REPO) or inside(out, store / 'agencies'):
        raise ValueError('output location refused')
    return out


def build(store, agency_filter, run_filter=None):
    agencies = store / 'agencies'
    if agencies.is_symlink() or not agencies.is_dir():
        raise ValueError('store agencies unavailable')
    built = datetime.date.today().isoformat()
    lines = ['Private review queue. Built %s. Approving is a human decision recorded in PUBLISH.json; this file changes nothing.'
             % (built + ('; run ' + run_filter if run_filter else '')), '']
    rows, counts, waiting, blocked = [], collections.Counter(), [], []
    for agency in sorted(agencies.iterdir()):
        if agency_filter and agency.name != agency_filter:
            continue
        runs = agency / 'runs'
        if agency.is_symlink() or runs.is_symlink():
            counts['not_reviewable'] += 1
            blocked.append({'agency': safe_name(agency.name), 'run': None, 'reason': 'link-refused'})
            lines += ['## NOT REVIEWABLE: link-refused — fix with flock_publish recheck before approving', '']
            continue
        if not runs.is_dir():
            continue
        for run in sorted(runs.iterdir()):
            if run_filter and run.name != run_filter:
                continue
            target = run / 'PUBLISH.json'
            if run.is_symlink() or target.is_symlink():
                counts['not_reviewable'] += 1
                blocked.append({'agency': safe_name(agency.name), 'run': safe_name(run.name), 'reason': 'link-refused'})
                lines += ['## NOT REVIEWABLE: link-refused — fix with flock_publish recheck before approving', '']
                continue
            if not run.is_dir() or not target.is_file():
                continue
            stage, _ = publish.stage(run)  # check and re-derive before displaying any entry
            if stage in ('invalid', 'drifted', 'no-export'):
                counts['not_reviewable'] += 1
                blocked.append({'agency': safe_name(agency.name), 'run': safe_name(run.name), 'reason': stage})
                lines += ['## NOT REVIEWABLE: %s — fix with flock_publish recheck before approving' % stage, '']
                continue
            doc = json.loads(target.read_text(encoding='utf-8'))
            _, _, _, challenges = verify.load(run)
            reasons = collections.Counter(v['reason'] for v in challenges.values() if v['reason'])
            entries = sorted(doc['findings'], key=lambda f: (f['review']['status'] != 'needs-review', f['finding_id']))
            for entry in entries:
                review = entry['review']
                if review['status'] == 'approved':
                    counts['approved'] += 1
                    continue
                if review['status'] not in ('pending', 'needs-review'):
                    continue
                fid = entry['finding_id']
                challenge = challenges.get(fid)
                warnings, codes = [], []
                if not challenge:
                    warnings.append('VERIFY row missing')
                    codes.append('verify-row-missing')
                if verify.family(run):
                    warnings.append('verifier family is not independent')
                    codes.append('verifier-not-independent')
                if challenge:
                    reason = challenge['reason']
                    if len(reason) < 20:
                        warnings.append('challenge reason under 20 characters')
                        codes.append('reason-under-20')
                    if challenge['verdict'] != 'confirm':
                        warnings.append('verdict is not confirm')
                        codes.append('verdict-not-confirm')
                    if reasons[reason] >= 3:
                        warnings.append('identical challenge text on %d findings' % reasons[reason])
                        codes.append('identical-reason')
                counts['waiting'] += 1
                counts['needs_review'] += review['status'] == 'needs-review'
                counts['warnings'] += len(warnings)
                verdict = challenge['verdict'] if challenge else 'missing'
                verdict = verdict if verdict in ('confirm', 'downgrade', 'reject') else 'missing'
                waiting.append({'agency': safe_name(agency.name), 'run': safe_name(run.name), 'finding_id': safe_name(fid),
                                'status': review['status'], 'verdict': verdict, 'warnings': codes})
                rows.append('queue | %s | %s | %s | %s | verdict=%s | warnings=%d' %
                            (safe_name(agency.name), safe_name(run.name), safe_name(fid), review['status'],
                             verdict, len(warnings)))
                lines += ['## finding %s · %s · run %s' % (safe_name(fid), safe_name(agency.name), safe_name(run.name)),
                          '', '**Claim**', fenced(entry['text']), 'Rating: %s; claim_class: %s' %
                          (entry['rating'], entry['claim_class']), '', '**Independent challenge**',
                          'Written by %s; challenged by %s' % (doc['author_family'], doc['verifier_family'])]
                if challenge:
                    lines += ['Verdict: %s; re-read fact: %s; page: %s; new rating: %s' %
                              (challenge['verdict'], challenge['reread_fact_id'], challenge['reread_page'], challenge['new_rating']),
                              'Records sought:', fenced(challenge['records_sought']), 'Reason:', fenced(challenge['reason'])]
                else:
                    lines += ['VERIFY row missing']
                lines += ['WARNING: ' + warning for warning in warnings]
                lines += ['', '**Cites**']
                for cite in entry['cites']:
                    label = cite.get('review', '')
                    if label == 'needs_attorney_review':
                        label = 'needs attorney review — cannot be approved here'
                    lines += [cite['kind'] + (' (' + label + ')' if label else '') + ':', fenced(cite['label'])]
                    if cite['kind'] == 'law':
                        lines += [fenced(cite['url'])]
                empty = [field for field in ('approved_by', 'approved_on', 'approval_ref') if not review[field].strip()]
                lines += ['', '**Receipt** — empty: ' + (', '.join(empty) if empty else 'none')]
                if review['status'] == 'needs-review':
                    lines += ['approved earlier by %s on %s; moved to needs-review by recheck' %
                              (review['approved_by'], review['approved_on'])]
                lines += ['', '**To approve** — human review only: fill approved_by, approved_on and approval_ref,',
                          'and set status to approved in PUBLISH.json per skills/flock-publish.', '']
    lines += ['Approved entries (counted only): %d' % counts['approved'], '']
    rows.append('queue | %swaiting=%d needs_review=%d not_reviewable=%d warnings=%d | review-required; only a human approves' %
                ('run=%s ' % run_filter if run_filter else '', counts['waiting'], counts['needs_review'],
                 counts['not_reviewable'], counts['warnings']))
    data = {'schema': 'flock-review-queue-v1', 'review_required': True, 'built': built,
            'note': 'only a human approves; this file changes nothing', 'agency_filter': agency_filter or None,
            'waiting': waiting, 'not_reviewable': blocked,
            'counts': {k: counts[k] for k in ('waiting', 'needs_review', 'approved', 'not_reviewable', 'warnings')}}
    if run_filter:
        data['run'] = run_filter
    return '\n'.join(lines), rows, bool(counts['not_reviewable'] or counts['warnings']), data


def write(out, text, data=None):
    out.mkdir(mode=0o700)
    files = {'review-queue.md': text}
    if data is not None:
        files['review-queue.json'] = json.dumps(data, sort_keys=True, indent=2, ensure_ascii=True) + '\n'
    try:
        for name, body in files.items():
            fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as handle:
                handle.write(body.encode('utf-8'))
    except BaseException:
        with contextlib.suppress(OSError):
            for name in files:
                (out / name).unlink(missing_ok=True)
            out.rmdir()
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['show'])
    parser.add_argument('store')
    parser.add_argument('--agency')
    parser.add_argument('--run', help='one run folder of --agency (STORE/agencies/SLUG/runs/RUN) only')
    parser.add_argument('--out', required=True)
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    try:
        store = Path(args.store).resolve()
        if inside(store, REPO):
            raise ValueError('store inside repository')
        if args.agency and not publish.SLUG.fullmatch(args.agency):
            raise ValueError('invalid agency')
        if args.run is not None:  # a named, real run folder of the named agency, checked before --out is made
            if not args.agency or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run):
                raise ValueError('invalid run')
            run_dir = store / 'agencies' / args.agency / 'runs' / args.run
            if linked(run_dir) or not run_dir.is_dir():
                raise ValueError('run unavailable')
        out = output_path(args.out, store)
        text, rows, failed, data = build(store, args.agency, args.run)
        write(out, text, data if args.json else None)
    except (OSError, ValueError, KeyError, TypeError):
        print('queue refused: invalid input or output')
        return 1
    print('\n'.join(rows))
    return int(failed)


if __name__ == '__main__':
    sys.exit(main())
