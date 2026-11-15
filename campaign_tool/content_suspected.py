"""Bounded organizer-reviewed document predictions; no observed-camera inference or I/O beyond this file."""
import ipaddress
import json
import math
import os
import re
import stat
import unicodedata
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlsplit

from .content import ContentError, SOURCES_UNKNOWN, _https
from .privacy_patterns import categories

FILE = 'content/suspected-locations.json'
NOTICE = 'A suspected location is predicted by a document. It is not a confirmed camera.'
KINDS = ('records', 'permit', 'dig_ticket', 'contract', 'council_item', 'other')
REQUIRED = {'id', 'place_label', 'latitude', 'longitude', 'source_kind', 'source', 'as_of'}
KEYS = REQUIRED | {'agency_id'}
MAX_BYTES = 512 * 1024
SLUG = re.compile(r'[a-z][a-z0-9-]{0,63}')
PREFIX = re.compile(r'[0-9a-f]{12}')
DAY = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


def _document(path):
    """lstat + no-follow/nonblocking descriptor; bounded even if replaced or growing."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('not regular')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_size > MAX_BYTES):
            raise ValueError('unsafe file')
        raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('too large')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs)


def _public_source(value, sources):
    if not isinstance(value, str):
        return False
    if PREFIX.fullmatch(value):
        return sources is SOURCES_UNKNOWN or sum(s['sha256'][:12] == value for s in sources) == 1
    if len(value) > 500:
        return False
    try:
        _https(value, FILE + '.source')
        parsed = urlsplit(value)
        parsed.port  # rejects malformed/out-of-range ports
        host = (parsed.hostname or '').lower().rstrip('.')
        if host == 'localhost' or host.endswith('.localhost'):
            return False
        try:
            address = ipaddress.ip_address(host)
            return address.is_global and not address.is_multicast and not address.is_reserved
        except ValueError:
            # Reject legacy numeric IPv4 forms rather than relying on browser normalization/DNS.
            return not (re.fullmatch(r'[0-9.]+', host) or host.startswith('0x'))
    except (ContentError, ValueError):
        return False


def read_suspected(root, agencies, sources, today=None, problems=None):
    """None only if absent or refused diagnostic input; valid presence returns a public list (even [])."""
    path = Path(root) / FILE
    found = []

    def refuse(where, reason):
        found.append(f'{FILE}{where}: {reason}')

    def finish(result):
        if found:
            if problems is None:
                raise ContentError(found[0])
            problems.extend(found)
            return None
        return result

    try:
        path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        refuse('', 'cannot inspect optional file')
        return finish(None)
    try:
        document = _document(path)
    except (OSError, ValueError, RecursionError, OverflowError):
        refuse('', 'must be readable regular UTF-8 JSON without duplicate keys, at most 512 KiB (no symlinks)')
        return finish(None)
    if not isinstance(document, dict):
        refuse('', 'must be an object with schema_version and entries')
        return finish(None)
    if set(document) - {'schema_version', 'entries'}:
        refuse('', 'unknown field')
    if type(document.get('schema_version')) is not int or document['schema_version'] != 1:
        refuse('.schema_version', 'must be 1')
    rows = document.get('entries')
    if not isinstance(rows, list) or len(rows) > 200:
        refuse('.entries', 'must be a list of at most 200 entries')
        return finish(None)
    known = None if agencies is None else {a['agency_id'] for a in agencies}
    day, accepted, seen = today or date.today(), [], set()
    for index, row in enumerate(rows):
        where = f'.entries[{index}]'
        if not isinstance(row, dict):
            refuse(where, 'must be an object')
            continue
        start = len(found)
        if set(row) - KEYS:
            refuse(where, 'unknown field')
        for key in sorted(REQUIRED - set(row)):
            refuse(where + '.' + key, 'missing required field')
        output = {}
        for key, value in row.items():
            if key not in KEYS:
                continue
            valid, reason = True, 'invalid field'
            if key == 'id':
                valid = isinstance(value, str) and SLUG.fullmatch(value) is not None and value not in seen
                reason = 'must be a unique lowercase slug of 1 to 64 characters'
                if valid:
                    seen.add(value)
            elif key == 'place_label':
                valid = (isinstance(value, str) and 1 <= len(value) <= 80 and bool(value.strip())
                         and not any(c in '<>\u2028\u2029' or unicodedata.category(c) in ('Cc', 'Cf', 'Cs') for c in value))
                reason = 'must be 1 to 80 plain single-line characters'
            elif key in ('latitude', 'longitude'):
                bound = 90 if key == 'latitude' else 180
                valid = type(value) in (int, float) and -bound <= value <= bound and math.isfinite(value)
                reason = 'must be a finite number in coordinate range'
                if valid:
                    rounded = Decimal(str(value)).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
                    value = float(rounded) if rounded else 0
            elif key == 'source_kind':
                valid = isinstance(value, str) and value in KINDS
                reason = 'must be records, permit, dig_ticket, contract, council_item or other'
            elif key == 'source':
                valid = _public_source(value, sources)
                reason = 'must be a public https URL (at most 500 characters) or one published 12-hex library prefix'
            elif key == 'as_of':
                try:
                    valid = isinstance(value, str) and DAY.fullmatch(value) is not None and date.fromisoformat(value) <= day
                except ValueError:
                    valid = False
                reason = 'must be a real YYYY-MM-DD date not after the build day'
            elif key == 'agency_id':
                valid = isinstance(value, str) and bool(value) and (known is None or value in known)
                reason = 'must name a loaded campaign agency'
            if not valid:
                refuse(where + '.' + key, reason)
            elif isinstance(value, str) and (categories(value) or re.search(r'[0-9a-fA-F]{64}', value)):
                refuse(where + '.' + key, 'public text contains a contact, plate or full hash')
            output[key] = value
        if start == len(found):
            accepted.append(output)
    return finish(sorted(accepted, key=lambda r: r['id']))


def export(rows):
    return json.dumps({'schema_version': 1, 'generated_from': FILE, 'notice': NOTICE, 'entries': rows},
                      ensure_ascii=False, sort_keys=True, indent=2) + '\n'
