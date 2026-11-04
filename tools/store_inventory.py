#!/usr/bin/env python3
"""Measure a synthetic or admitted private store; outputs remain private and require review."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import sys
from datetime import date, datetime, timezone

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import store_check  # noqa: E402


def sibling(name):
    spec = importlib.util.spec_from_file_location('_inventory_' + name, REPO / 'tools' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan = sibling('flock_privacy_scan')
flock = sibling('flock_store_check')
KNOWN_AREAS = {'agencies', 'regions', 'private', 'content', 'kit'}
EXTENSIONS = {'.csv', '.json', '.md', '.txt', '.pdf', '.sqlite', '.eml', '.mbox', '.html', '.xml'}
PROTECTIONS = [
    {'file': 'campaign_tool/cli.py', 'symbol': 'main', 'protection': 'restrictive CLI umask'},
    {'file': 'campaign_tool/store_check.py', 'symbol': 'check_store', 'protection': 'object integrity and mode checks'},
    {'file': 'campaign_tool/store_check.py', 'symbol': 'fix_modes', 'protection': 'explicit mode repair without following links'},
    {'file': 'tools/flock_store_check.py', 'symbol': 'collect', 'protection': 'structural checks'},
    {'file': 'tools/flock_privacy_scan.py', 'symbol': 'Scan', 'protection': 'pattern counts without matched text'},
    {'file': 'tools/flock_privacy_scan.py', 'symbol': 'write_receipt', 'protection': 'exclusive owner-only scan receipt'},
    {'file': 'campaign_tool/records/public_scan.py', 'symbol': 'scan', 'protection': 'public tree path and credential checks'},
]


class Refused(Exception):
    pass


def inside(path, base):
    return path == base or base in path.parents


def validate(store, out):
    if not all(os.path.isabs(p) and os.path.normpath(p) == p for p in (store, out)):
        raise Refused('absolute_normalized_paths_required')
    store, out = Path(store), Path(out)
    if any(p.is_symlink() for p in (store, *store.parents)) or not store.is_dir():
        raise Refused('store_unavailable')
    if os.path.lexists(out) or not out.parent.is_dir() or any(p.is_symlink() for p in (out.parent, *out.parent.parents)):
        raise Refused('output_unavailable')
    if inside(out, REPO) or inside(out, store) or inside(store, out):
        raise Refused('output_overlaps_store_or_repo')
    if inside(store, REPO):
        raise Refused('store_in_repo')
    return store, out


def area_stats(root, store, label):
    result = {'area': label, 'files': 0, 'directories': 0, 'bytes': 0,
              'oldest_mtime': None, 'newest_mtime': None, 'modes': {}, 'owners': [],
              'group_or_world_readable': False, 'outside_symlink': False,
              'extensions': {}, 'category_counts': dict.fromkeys(('email', 'phone', 'plate'), 0),
              'scan_skipped': 0, 'hidden_directories_not_scanned': 0}
    owners, times = set(), []
    def visit(path):
        st = path.lstat()
        mode = format(stat.S_IMODE(st.st_mode), '04o')
        result['modes'][mode] = result['modes'].get(mode, 0) + 1
        owners.add((st.st_uid, st.st_gid))
        times.append(st.st_mtime)
        result['group_or_world_readable'] |= bool(st.st_mode & 0o044)
        if stat.S_ISLNK(st.st_mode):
            result['outside_symlink'] |= not inside(Path(os.path.realpath(path)), store)
        elif stat.S_ISDIR(st.st_mode):
            result['directories'] += 1
            if path.name.startswith('.'):
                result['hidden_directories_not_scanned'] += 1
            with os.scandir(path) as entries:
                for entry in entries:
                    visit(Path(entry.path))
        elif stat.S_ISREG(st.st_mode):
            result['files'] += 1
            result['bytes'] += st.st_size
            ext = path.suffix.lower()
            ext = ext if ext in EXTENSIONS else '(other)' if ext else '(none)'
            result['extensions'][ext] = result['extensions'].get(ext, 0) + 1
    visit(root)
    result['owners'] = [{'uid': uid, 'gid': gid} for uid, gid in sorted(owners)]
    if times:
        result['oldest_mtime'] = datetime.fromtimestamp(min(times), timezone.utc).isoformat()
        result['newest_mtime'] = datetime.fromtimestamp(max(times), timezone.utc).isoformat()
    return result


def scan_receipt(store, parent):
    target = str(parent / ('.inventory-scan-' + secrets.token_hex(16)))
    if scan.out_problem(target, [str(store)]):
        raise Refused('scan_output_unavailable')
    written = False
    original_write = scan.write_receipt
    def tracked_write(path, document):
        nonlocal written
        written = bool(original_write(path, document))
        return written
    scan.write_receipt = tracked_write
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = scan.main([str(store), '--by-category', '--json-out', target])
        if code not in (0, 3):
            raise Refused('scan_failed')
        with open(target, encoding='ascii') as handle:
            receipt = json.load(handle)
        if receipt.get('schema') != 'flock-privacy-scan-v1' or receipt['totals']['failed']:
            raise Refused('scan_failed')
        return receipt
    finally:
        scan.write_receipt = original_write
        if written:
            os.unlink(target)  # the explicitly admitted, path-bearing temporary receipt only


def ledger_rows(store):
    """Query only an in-memory copy; even SQLite mode=ro can create sidecars."""
    path = store / 'private' / 'ledger.sqlite'
    if any(os.path.lexists(str(path) + '-' + suffix) for suffix in ('journal', 'wal', 'shm')):
        # The database file alone may not contain the committed state.
        raise ValueError('ledger sidecars present')
    if not path.exists():
        return []
    data = bytearray(path.read_bytes())
    if data[:16] == b'SQLite format 3\0' and data[18:20] == b'\x02\x02':
        # As in cli._ledger_lookup: no sidecars, so query the complete copy as rollback mode.
        data[18:20] = b'\x01\x01'
    try:
        with contextlib.closing(sqlite3.connect(':memory:')) as db:
            db.deserialize(bytes(data))
            return db.execute('SELECT sha256, byte_count FROM objects ORDER BY sha256').fetchall()
    except sqlite3.Error:
        raise ValueError('ledger unreadable') from None


def inventory(store, out):
    paths = sorted(store.iterdir(), key=lambda p: p.name)
    areas = []
    for index, path in enumerate(paths, 1):
        label = path.name if path.name in KNOWN_AREAS and path.is_dir() and not path.is_symlink() else 'area-' + str(index)
        areas.append(area_stats(path, store, label))
    receipt = scan_receipt(store, out.parent)
    for path, area in zip(paths, areas):
        # The full-store scanner silently excludes hidden directories at every depth.
        if path.name.startswith('.') and path.is_dir() and not path.is_symlink():
            continue
        if path.is_symlink():
            # The whole-store receipt records the link without following its target.
            entry = next((r for r in receipt['files'] if r['path'] == str(path)), None)
            if not entry or entry['outcome'] != 'skipped' or entry['reason'] != 'link':
                raise Refused('scan_count_mismatch')
            area['scan_skipped'] = 1
            continue
        area_receipt = scan_receipt(path, out.parent)
        area['category_counts'] = area_receipt['by_category']
        area['scan_skipped'] = area_receipt['totals']['skipped']
    for kind, count in receipt['by_category'].items():
        if sum(a['category_counts'][kind] for a in areas) != count:
            raise Refused('scan_count_mismatch')
    if sum(a['scan_skipped'] for a in areas) != receipt['totals']['skipped']:
        raise Refused('scan_count_mismatch')
    errors = []
    # The existing structural tools may follow links internally; do not invoke them on a linked tree.
    if any(a['outside_symlink'] for a in areas) or any(p.is_symlink() for p in store.rglob('*')):
        errors.append('structural checks not run: links present')
    else:
        try:
            if not (store / 'private').is_dir() and not (store / 'agencies').is_dir() and not (store / 'regions').is_dir():
                errors.append('structural checks not applicable to this store layout')
            if (store / 'private').is_dir():
                result = store_check.check_store(store, rows=ledger_rows(store))
                if result['problems']:
                    errors.append('campaign store check found ' + str(len(result['problems'])) + ' problems')
            if (store / 'agencies').is_dir() or (store / 'regions').is_dir():
                args = argparse.Namespace(section=None, backups=None, max_backup_age_days=None, today=date.today())
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    sections = flock.collect(store, args)
                if sum(s['errors'] for s in sections):
                    errors.append('flock store check found ' + str(sum(s['errors'] for s in sections)) + ' problems')
        except (OSError, ValueError):
            errors.append('structural check failed')
    return {'schema': 'store-inventory-v1', 'review_required': True,
            'as_of': datetime.now(timezone.utc).isoformat(), 'areas': areas,
            'enforced_by_code': PROTECTIONS, 'source_errors': errors}


def write_outputs(out, result):
    body = json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + '\n'
    lines = ['# Private inventory — review required', '',
             'Counts are per (line, category), not per matched value. A clean scan is not a privacy review.',
             'Skipped entries and hidden directories mean coverage is partial, not a current complete scan.', '']
    for area in result['areas']:
        lines.append('## ' + area['area'])
        for key, value in area.items():
            if key != 'area':
                lines.append('- ' + key + ': ' + json.dumps(value, sort_keys=True))
    lines += ['', '## Structural source errors', json.dumps(result['source_errors']),
              '', '## Enforced by code', json.dumps(result['enforced_by_code']), '']
    outputs = [('inventory.json', body), ('INVENTORY.md', '\n'.join(lines))]
    out.mkdir(mode=0o700)
    for name, content in outputs:
        fd = os.open(out / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(content.encode('utf-8'))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    try:
        store, out = validate(args.store, args.out)
        result = inventory(store, out)
        write_outputs(out, result)
    except (Refused, OSError, ValueError, KeyError, TypeError):
        print(json.dumps({'schema': 'store-inventory-v1', 'status': 'refused'}))
        return 2
    print(json.dumps({'schema': 'store-inventory-v1', 'status': 'review_required'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
