"""Private OCR human-review queue; canonical extraction evidence is read-only."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path

from .run_stages import LedgerBusy, ledger_ro

BINDING = ('original_sha256', 'page_no', 'confidence', 'derivative_sha256')
FIELDS = set(BINDING) | {'schema', 'decision', 'reviewer_label', 'reviewed_at', 'prev_line_sha256'}
LABEL = re.compile(r'^[a-z][a-z0-9_-]{0,31}$')
SHA = re.compile(r'^[0-9a-f]{64}$')


class ChainBroken(ValueError):
    def __init__(self):
        super().__init__('ocr_review_chain_broken')


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def binding(row):
    return tuple(row[k] for k in BINDING)


def decode(raw):
    records, previous = [], None
    try:
        for line in raw.splitlines(keepends=True):
            row = json.loads(line)
            if (set(row) != FIELDS or canonical(row) != line or row['schema'] != 'ocr-review-v1'
                    or row['prev_line_sha256'] != previous or not SHA.fullmatch(row['original_sha256'])
                    or type(row['page_no']) is not int or row['page_no'] < 1
                    or row['decision'] not in ('confirmed', 'rejected')
                    or not LABEL.fullmatch(row['reviewer_label'])
                    or (row['confidence'] is not None and type(row['confidence']) not in (int, float))
                    or (row['derivative_sha256'] is not None and not SHA.fullmatch(row['derivative_sha256']))
                    or not row['reviewed_at'].endswith('Z')):
                raise ChainBroken()
            datetime.fromisoformat(row['reviewed_at'])
            if any(binding(r) == binding(row) for r in records):
                raise ChainBroken()
            records.append(row)
            previous = hashlib.sha256(line).hexdigest()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ChainBroken() from None
    return records, previous


def read_reviews(path):
    path = Path(path)
    if path.parent.is_symlink():
        raise ChainBroken()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, 'rb') as stream:
        fcntl.flock(stream, fcntl.LOCK_SH)
        return decode(stream.read())[0]


def bucket(confidence):
    return 'spot_check' if confidence is not None and confidence >= 80 else 'hold'


def pages(db):
    return [dict(zip(BINDING, row)) for row in db.execute(
        'SELECT original_sha256,page_no,confidence,derivative_sha256 '
        'FROM page_state WHERE needs_visual_review=1 ORDER BY original_sha256,page_no')]


def pending(db, reviews):
    reviewed = {binding(r) for r in read_reviews(reviews)}
    return [r for r in pages(db) if binding(r) not in reviewed]


def page_use_allowed(row, reviews):
    matched = next((r for r in read_reviews(reviews) if binding(r) == binding(row)), None)
    if matched:
        return (False, 'review_rejected') if matched['decision'] == 'rejected' else (True, 'review_confirmed')
    if bucket(row['confidence']) == 'spot_check':
        return True, 'read_with_flag'
    return False, 'review_required_lt80'


def export(db, reviews, out):
    reviewed = {binding(r): r['decision'] for r in read_reviews(reviews)}
    counts = dict(pending_spot_check=0, pending_hold=0, reviewed_confirmed=0, reviewed_rejected=0)
    rows = []
    for row in pages(db):
        decision = reviewed.get(binding(row))
        counts['reviewed_' + decision if decision else 'pending_' + bucket(row['confidence'])] += 1
        if not decision:
            rows.append(dict(original_sha256=row['original_sha256'], page_no=row['page_no'],
                             bucket=bucket(row['confidence'])))
    payload = dict(schema='ocr-review-queue-v1', counts=counts, rows=rows)
    with os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
        stream.write(canonical(payload))
        stream.flush()
        os.fsync(stream.fileno())


def mark(db, reviews, sha, page, decision, label):
    if not SHA.fullmatch(sha) or page < 1 or decision not in ('confirmed', 'rejected') or not LABEL.fullmatch(label):
        raise ValueError('ocr_review_invalid_mark')
    # Refuse invalid/nonpending marks before creating any review file.
    candidates = [r for r in pending(db, reviews) if (r['original_sha256'], r['page_no']) == (sha, page)]
    if not candidates:
        raise ValueError('ocr_review_not_pending')
    reviews.parent.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(reviews.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if stat.S_IMODE(os.fstat(directory).st_mode) != 0o700:
            raise ValueError('ocr_review_unsafe_directory')
        fd = os.open(reviews.name, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'r+b') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError('ocr_review_unsafe_file')
        records, previous = decode(stream.read())
        if any(binding(r) == binding(candidates[0]) for r in records):
            raise ValueError('ocr_review_not_pending')
        row = dict(candidates[0], schema='ocr-review-v1', decision=decision, reviewer_label=label,
                   reviewed_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
                   prev_line_sha256=previous)
        stream.write(canonical(row))
        stream.flush()
        os.fsync(stream.fileno())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    exp = sub.add_parser('export')
    exp.add_argument('--project', required=True, type=Path)
    exp.add_argument('--out', required=True, type=Path)
    marker = sub.add_parser('mark')
    marker.add_argument('--project', required=True, type=Path)
    marker.add_argument('--original-sha256', required=True)
    marker.add_argument('--page', required=True, type=int)
    marker.add_argument('--decision', required=True)
    marker.add_argument('--reviewer-label', required=True)
    args = parser.parse_args(argv)
    project = args.project.resolve()
    reviews = project / 'ocr-review-v1' / 'reviews.jsonl'
    try:
        with ledger_ro(project / 'processing-v1') as db:
            if args.command == 'mark':
                mark(db, reviews, args.original_sha256, args.page, args.decision, args.reviewer_label)
            else:
                out = args.out.resolve()
                if any(out.is_relative_to(project / name) for name in ('processing-v1', 'ocr-review-v1')):
                    raise ValueError('ocr_review_unsafe_output')
                export(db, reviews, out)
    except ChainBroken:
        print('ocr_review_chain_broken')
        return 1
    except (ValueError, OSError, sqlite3.Error, LedgerBusy):
        print('ocr_review_refused')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
