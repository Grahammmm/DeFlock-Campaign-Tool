#!/usr/bin/env python3
"""Private text redaction: propose, payload, signoff, status, apply. Never publishes.

Every command takes STORE SLUG SOURCE_SHA256 DOCUMENT (absolute private UTF-8 text).
Propose also requires --scan-receipt FILE; signoff requires --signature FILE.
Patterns are not exhaustive: the human must inspect the private original.
"""
import argparse
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from datetime import datetime, timezone

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool import approval  # noqa: E402
_spec = importlib.util.spec_from_file_location('_redact_record', REPO / 'tools/flock_redaction_record.py')
_spec.loader.exec_module(record := importlib.util.module_from_spec(_spec))
NOTE = ('Patterns are not exhaustive: bare 10-digit runs, plain-word email, names, addresses and image text '
        'need human review of the private original. Eligibility is not publication approval.')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + '\n').encode('ascii')


def stamp():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def private_dir(store, folder, create=False):
    record._contained(store, str(folder / '.private-check'))
    parts = [folder]
    while parts[-1] != store:
        parts.append(parts[-1].parent)
    for path in reversed(parts):
        if create and not path.exists():
            path.mkdir(mode=0o700)
        if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise ValueError('private_directory_required')


def write_new(path, raw):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise


def install(path, raw, check):
    # Publish one complete file without ever replacing a prior record.
    with tempfile.TemporaryDirectory(prefix='.redact-', dir=path.parent) as tmp:
        staged = Path(tmp) / 'record'
        write_new(staged, raw)
        check()
        os.link(staged, path)
        try:
            check()
        except BaseException:
            path.unlink()
            raise


def read_signature(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError('signature_link') from None
        raise ValueError('signature_unreadable') from None
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('signature_special')
        raw = stream.read(approval.MAX_SIGNATURE + 1)
    if len(raw) > approval.MAX_SIGNATURE:
        raise ValueError('signature_oversized')
    try:
        text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    except UnicodeError:
        raise ValueError('signature_encoding') from None
    lines = text.split('\n')
    begin, end = '-----BEGIN SSH SIGNATURE-----', '-----END SSH SIGNATURE-----'
    if begin not in lines or end not in lines[lines.index(begin) + 1:]:
        raise ValueError('signature_invalid')
    return text


def spans(raw):
    text = raw.decode('utf-8')
    if '\0' in text:
        raise ValueError('text_required')
    found = []
    for category, pattern in record.privacy.PATTERNS:
        for match in pattern.finditer(text):
            start = match.start()
            found.append(dict(category=category, locator=dict(line=text.count('\n', 0, start) + 1,
                offset=len(text[text.rfind('\n', 0, start) + 1:start].encode('utf-8')),
                length=len(match[0].encode('utf-8')))))
    return sorted(found, key=lambda item: (item['locator']['line'], item['locator']['offset'], item['category']))


def derivative(raw, proposed):
    starts = [0]
    starts.extend(i + 1 for i, byte in enumerate(raw) if byte == 10)
    ranges = []
    for item in proposed:
        loc = item['locator']
        start = starts[loc['line'] - 1] + loc['offset']
        ranges.append((start, start + loc['length'], item['category'], loc['length']))
    # Overlaps form one removal union; retain each approved category/byte length.
    groups = []
    for start, end, category, length in sorted(ranges):
        marker = f'[{category}:{length} bytes removed]'.encode('ascii')
        if groups and start < groups[-1][1]:
            groups[-1][1] = max(groups[-1][1], end)
            groups[-1][2] += marker
        else:
            groups.append([start, end, marker])
    result, cursor = bytearray(), 0
    for start, end, marker in groups:
        result.extend(raw[cursor:start])
        result.extend(marker)
        cursor = end
    return bytes(result) + raw[cursor:]


def source(args):
    path = record._contained(args.store, args.document)
    raw = record._bytes(path)
    if digest(raw) != args.sha:
        raise ValueError('source_mismatch')
    return path, raw


def proposal(args, raw):
    value, encoded = record._record(args.folder, 'PROPOSAL.json', 'redaction-proposal')
    if (value['source_sha256'] != args.sha or value['document_id'] != args.sha
            or value['proposed'] != spans(raw) or value['redaction_required'] != bool(value['proposed'])
            or encoded != encode(value)):
        raise ValueError('proposal_mismatch')
    return value, encoded


def payload(raw, sha):
    return approval.payload('flock-redaction', sha, raw.decode('utf-8'))


def signed(args, raw):
    if not os.path.lexists(args.folder / 'SIGNOFF.json'):
        raise ValueError('unsigned')
    sign, encoded = record._record(args.folder, 'SIGNOFF.json', 'redaction-signoff')
    if any(sign[k] != v for k, v in (('document_id', args.sha), ('source_sha256', args.sha),
                                     ('proposal_sha256', digest(raw)))):
        raise ValueError('proposal_mismatch')
    if approval.verify(payload(raw, args.sha), sign['signature']) != sign['signed_by']:
        raise ValueError('signer_untrusted')
    return sign, encoded


def apply(args, path, raw, prop, prop_raw):
    sign, sign_raw = signed(args, prop_raw)  # fresh verifier; never permission from derivative_missing
    if not prop['redaction_required']:
        raise ValueError('not_required')
    target = path.with_name(args.sha + '.redacted.txt')
    final = args.folder / 'ELIGIBILITY.json'
    record._contained(args.store, str(target))
    private_dir(args.store, target.parent)
    if os.path.lexists(target) or os.path.lexists(final):
        raise FileExistsError()
    created = []
    with tempfile.TemporaryDirectory(prefix='.redact-', dir=args.store) as tmp:
        stage = Path(tmp)
        folder = record.record_dir(stage, args.slug, args.sha)
        private_dir(stage, folder, create=True)
        staged_derivative = stage / 'derivative.txt'
        write_new(folder / 'PROPOSAL.json', prop_raw)
        write_new(folder / 'SIGNOFF.json', sign_raw)
        write_new(staged_derivative, derivative(raw, prop['proposed']))
        applied = {k: sign[k] for k in ('document_id', 'source_sha256', 'proposal_sha256', 'signed_by', 'signed_at')}
        applied.update(schema_version=1, derivative_path=str(staged_derivative),
                       derivative_sha256=record._hash(staged_derivative), applied_at=stamp())
        write_new(folder / 'ELIGIBILITY.json', encode(applied))
        state, reason = record.eligibility(stage, args.slug, args.sha, digest(source(args)[1]))
        if (state, reason) != ('eligible', 'ok'):
            raise ValueError(reason)
        # Only the relocation field changes after staged validation. Recheck all inputs
        # before immutable promotion; rollback our own new links on ANY later failure.
        applied['derivative_path'] = str(target)
        promoted_record = stage / 'final.json'
        write_new(promoted_record, encode(applied))
        try:
            source(args)
            if record._bytes(args.folder / 'PROPOSAL.json') != prop_raw or record._bytes(args.folder / 'SIGNOFF.json') != sign_raw:
                raise ValueError('proposal_mismatch')
            os.link(staged_derivative, target)
            created.append(target)
            os.link(promoted_record, final)
            created.append(final)
            state, reason = record.eligibility(args.store, args.slug, args.sha, digest(source(args)[1]))
            if (state, reason) != ('eligible', 'ok'):
                raise ValueError(reason)
            source(args)
        except BaseException:
            for new in reversed(created):
                new.unlink()
            raise


def parser():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    for command in ('propose', 'payload', 'signoff', 'status', 'apply'):
        child = sub.add_parser(command)
        for name in ('store', 'slug', 'sha', 'document'):
            child.add_argument(name)
        if command == 'propose':
            child.add_argument('--scan-receipt', required=True)
        if command == 'signoff':
            child.add_argument('--signature', required=True)
    return ap


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        args.store = Path(args.store)
        args.folder = record.record_dir(args.store, args.slug, args.sha)
        record._contained(args.store, str(args.folder))
        if args.command == 'status':
            current = record.original_hash(args.store, [args.document])
            state, reason = record.eligibility(args.store, args.slug, args.sha, current)
            label = ('none' if state == 'no_record' else 'applied' if reason == 'ok' else
                     'proposed' if reason in ('unsigned', 'not_required') else
                     'signed' if reason == 'derivative_missing' and not os.path.lexists(args.folder / 'ELIGIBILITY.json') else 'stale')
            print(f'{args.sha}: {label}' + (f' {reason}' if label == 'stale' else ''))
            counts = {key: int(label == key) for key in ('proposed', 'signed', 'applied', 'stale')}
            print('redact | documents=1 ' + ' '.join(f'{k}={v}' for k, v in counts.items())
                  + f' unsigned={int(reason == "unsigned")} | human sign-off required; originals unchanged')
            return int(label == 'stale')
        path, raw = source(args)
        if args.command == 'propose':
            receipt_path = record._contained(args.store, args.scan_receipt)
            receipt_raw = record._bytes(receipt_path)
            receipt = json.loads(receipt_raw)
            if (not isinstance(receipt, dict) or not isinstance(receipt.get('totals'), dict)
                    or not isinstance(receipt.get('files'), list)
                    or any(not isinstance(f, dict) for f in receipt['files'])):
                raise ValueError('scan_receipt_mismatch')
            if (receipt.get('schema') != 'flock-privacy-scan-v1' or receipt.get('allow_file_used') is not False
                    or receipt.get('totals', {}).get('failed') is not False
                    or set(receipt.get('categories', ('email', 'phone', 'plate'))) != {'email', 'phone', 'plate'}
                    or not any(f.get('path') == str(path) and f.get('outcome') == 'scanned'
                               and f.get('sha256') == args.sha for f in receipt.get('files', []))):
                raise ValueError('scan_receipt_mismatch')
            proposed = spans(raw)
            value = dict(schema_version=1, document_id=args.sha, source_sha256=args.sha,
                         redaction_required=bool(proposed), proposed=proposed, not_detected_note=NOTE,
                         scan_receipt=digest(receipt_raw), created_at=stamp())
            private_dir(args.store, args.folder, create=True)
            source(args)
            install(args.folder / 'PROPOSAL.json', encode(value), lambda: source(args))
        else:
            private_dir(args.store, args.folder)
            prop, prop_raw = proposal(args, raw)
            if args.command == 'payload':
                sys.stdout.write(payload(prop_raw, args.sha).decode('utf-8'))
                return 0
            if args.command == 'signoff':
                sig = read_signature(args.signature)
                who = approval.verify(payload(prop_raw, args.sha), sig)
                value = dict(schema_version=1, scope='flock-redaction', document_id=args.sha, source_sha256=args.sha,
                             proposal_sha256=digest(prop_raw), signed_by=who, signed_at=stamp(), signature=sig)
                if not isinstance(who, str) or not who.strip():
                    raise ValueError('signer_untrusted')
                source(args)
                if record._bytes(args.folder / 'PROPOSAL.json') != prop_raw:
                    raise ValueError('proposal_mismatch')
                install(args.folder / 'SIGNOFF.json', encode(value), lambda: source(args))
            else:
                apply(args, path, raw, prop, prop_raw)
        print(f'redact: {args.command} recorded; human sign-off required; originals unchanged')
        return 0
    except FileExistsError:
        reason = 'already_exists'
    except approval.ApprovalError:
        reason = 'signer_untrusted'
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        # Exception text from filenames, JSON and decoders can contain personal data.
        reason = str(exc) if type(exc) is ValueError and str(exc) in {
            'source_mismatch', 'proposal_mismatch', 'unsigned', 'signer_untrusted', 'not_required',
            'private_directory_required', 'signature_link', 'signature_unreadable', 'signature_special',
            'signature_oversized', 'signature_encoding', 'signature_invalid', 'text_required',
            'scan_receipt_mismatch', 'derivative_missing', 'derivative_mismatch'} else 'invalid_input'
    print('redact: refused ' + reason)
    return 1


if __name__ == '__main__':
    sys.exit(main())
