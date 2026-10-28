"""Synthetic-only support; import has no application or process-wide side effects."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile

REPO = Path(__file__).resolve().parents[3]
RUN = None
ATTEMPTS = []


def checked_directory(value):
    path = Path(os.path.abspath(value))
    if '..' in Path(value).parts:
        raise ValueError('unsafe output ancestry')
    for part in reversed((path, *path.parents)):
        if part.is_symlink() or (part.exists() and not part.is_dir()):
            raise ValueError('unsafe output ancestry')
    return path


def bootstrap(root):
    """Only a disposable file-launched child installs irreversible audit hooks."""
    global RUN
    RUN = checked_directory(root)
    RUN.mkdir(parents=True, exist_ok=True)
    sys.dont_write_bytecode = True
    for key in ('HOME', 'TMPDIR', 'TMP', 'TEMP', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'):
        target = RUN / key.lower()
        target.mkdir(exist_ok=True)
        os.environ[key] = str(target)
    for key in tuple(os.environ):
        if key.startswith(('MODEL_', 'CHALLENGE_MODEL_')):
            os.environ.pop(key)
    tempfile.tempdir = str(RUN / 'tmpdir')
    reads = (REPO, RUN, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve())
    # Audit open omits dir_fd; retain the checked full path across os.open.
    real_open, real_close = os.open, os.close
    descriptors, opening = {}, []

    def full_path(path, fd=None):
        if isinstance(path, int):
            return path
        p = Path(os.fsdecode(path))
        if not p.is_absolute() and fd is not None:
            if fd not in descriptors:
                raise RuntimeError('unbound directory descriptor')
            p = descriptors[fd] / p
        return p

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        full = full_path(path, dir_fd)
        opening.append(full)
        try:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        finally:
            opening.pop()
        descriptors[descriptor] = Path(os.path.abspath(full))
        return descriptor

    def guarded_close(fd):
        descriptors.pop(fd, None)
        return real_close(fd)

    os.open, os.close = guarded_open, guarded_close

    def allowed(path, write=False, directory=False):
        if isinstance(path, int):
            return
        if path in (':memory:', ''):
            return
        text = os.fsdecode(path)
        if text.startswith('file:'):
            text = text[5:].split('?', 1)[0]
        p = Path(os.path.abspath(text)).resolve()
        if directory and not write and RUN.is_relative_to(p):
            return  # Descriptor-only walk to the synthetic receipt, not a file read.
        if any(p.is_relative_to(r) for r in ((RUN,) if write else reads)):
            return
        ATTEMPTS.append('file-write' if write else 'file-read')
        raise RuntimeError('FA1306 live-file sentinel')

    def audit(event, args):
        if event.startswith('socket.') or event in ('urllib.Request', 'os.system', 'os.fork', 'os.posix_spawn', 'subprocess.Popen'):
            ATTEMPTS.append(event)
            raise RuntimeError('FA1306 network/process sentinel')
        if event == 'open':
            mode, flags = args[1:3]
            write = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            allowed(opening[-1] if opening else args[0], write, bool(flags & os.O_DIRECTORY))
        elif event == 'sqlite3.connect':
            allowed(args[0], True)
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.truncate'):
            fd = args[2] if event in ('os.mkdir', 'os.chmod') else (args[1] if len(args) > 1 else None)
            allowed(full_path(args[0], None if fd == -1 else fd), True)
        elif event in ('os.rename', 'os.link', 'os.symlink'):
            allowed(args[0], True)
            allowed(args[1], True)
    sys.addaudithook(audit)

    def model_guard(frame, event, arg):
        if event == 'call' and frame.f_code.co_filename.endswith('/campaign_tool/digest/model.py'):
            if frame.f_code.co_name in ('narrative', '_request', '_post', '_strict_request'):
                ATTEMPTS.append('model')
                raise RuntimeError('FA1306 model sentinel')
    sys.setprofile(model_guard)


def policy(value):
    from campaign_tool.records.ledger import store
    from campaign_tool.records.ledger.mail_resolution_policy import ApprovedMailResolutionPolicy
    raw = store.canonical(value).encode()
    return ApprovedMailResolutionPolicy(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


class World:
    def __init__(self, owner, installed=False):
        # Reuse the unchanged, fully staged producer fixture; no planner AST seams.
        from tests.records.test_original_reconciliation import OriginalReconciliationTests
        from campaign_tool.records.ledger import store, original_resolution as resolution
        self.store, self.resolution = store, resolution
        self.fixture = OriginalReconciliationTests()
        self.fixture.setUp()
        owner.addCleanup(self.fixture.doCleanups)
        self.db, self.subject = self.fixture.database, self.fixture.sha
        self.output = self.fixture.backend.output
        self.storage = str(self.output / 'blobs' / self.subject)
        raw = b'{"schema":"synthetic-verification-receipt"}'
        self.receipt = hashlib.sha256(raw).hexdigest()
        self.oid = hashlib.sha256(b'FA1306 synthetic occurrence').hexdigest()
        path = self.output / 'verification-receipts' / self.receipt
        path.parent.mkdir(mode=0o700)
        path.write_bytes(raw)
        source = dict(account='synthetic-account', folder='INBOX', uidvalidity=1, uid=7, mime_path='1.2')
        evidence = dict(receipt_sha256=self.receipt, verification_receipt_path=str(path))
        with store.ledger(self.db) as con:
            owner.assertFalse(resolution.installed(con), 'fixture must start extension-free')
            legacy = con.execute("SELECT payload_sha256 FROM legacy_rows WHERE source_table='docs'").fetchone()[0]
            con.execute('INSERT INTO occurrences VALUES(?,?,?,?,?,?,?,?)',
                        (self.oid, self.subject, 'attachment', store.canonical(source), None, None,
                         'verified_mail_export', store.canonical(evidence)))
            if installed:
                resolution.ensure_schema(con)
            con.commit()
        self.value = dict(schema='approved-mail-resolution-policy-v1', inventory_sha256='e' * 64,
                          approval_sha256='f' * 64, held=[], originals={self.subject: dict(
                              bytes=18, role='original', scope='in_scope',
                              verification_receipt_sha256=self.receipt, binding_occurrence_id=self.oid,
                              dependent_receipts=[], legacy_binding_sha256=legacy)})
        self.policy = policy(self.value)

    def append(self, con, **changes):
        args = dict(subject=self.subject, policy=self.policy, verification_receipt_sha256=self.receipt,
                    occurrence_id=self.oid, storage_path=self.storage, output_root=self.output)
        args.update(changes)
        return self.resolution.append_mail_export(con, **args)

    def snapshot(self, con):
        return list(con.iterdump())

    def extension(self, con):
        return (list(con.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name LIKE 'original_resolution%' OR tbl_name='original_resolutions' ORDER BY name")),
                list(con.execute('SELECT * FROM ledger_meta WHERE key=?', (self.resolution.KEY,))))
