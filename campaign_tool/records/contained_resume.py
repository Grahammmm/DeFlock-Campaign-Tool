"""Compose reviewed one-item execution with the existing Linux child supervisor.

No live adapter, credential supplier, installed path or scheduler is discovered.
Use a fresh, dedicated interpreter. This is not a hostile-code sandbox.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys
import types

from . import bounded_resume, worker_lifecycle

MAX_SOURCE_BYTES = 256 * 1024
CLEANUP_SECONDS = 2
MAX_OWNED = 32


def _budget(seconds):
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 3 <= seconds <= 120):
        raise ValueError('invalid_deadline')
    return seconds - CLEANUP_SECONDS


def _reviewed_source(path, expected):
    """Capture exact reviewed bytes; never execute a subsequently reopened file."""
    if (not isinstance(expected, str) or len(expected) != 64
            or any(char not in '0123456789abcdef' for char in expected)):
        raise ValueError('invalid_source_identity')
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('invalid_source_path')
    directory = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    source = None
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                            dir_fd=directory)
            os.close(directory)
            directory = child
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=directory)
        before = os.fstat(source)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or before.st_mode & 0o022
                or before.st_size > MAX_SOURCE_BYTES):
            raise ValueError('invalid_source_file')
        with os.fdopen(source, 'rb') as stream:
            source = None
            raw = stream.read(MAX_SOURCE_BYTES + 1)
            after = os.fstat(stream.fileno())
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (any(getattr(before, field) != getattr(after, field) for field in fields)
                or len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != expected):
            raise ValueError('source_identity_mismatch')
        return raw
    finally:
        if source is not None:
            os.close(source)
        os.close(directory)


def _parent_state(state_dir, work_seconds):
    """Reject an unbound worker entry before adapter source import or execution.

    This binds a trusted same-UID launch, not an adversarial same-UID caller.
    The parent supervisor owns the child tree and is responsible for reaping it.
    """
    directory = worker_lifecycle._Directory(state_dir)
    try:
        raw, fingerprint = directory.read('state.json')
        state = worker_lifecycle._validate_state(worker_lifecycle._decode(raw))
        identity, _ = worker_lifecycle._identity(os.getppid())
        if (state['identity'] != identity or state['wall_seconds'] != work_seconds
                or state['term_seconds'] != 1 or state['kill_seconds'] != 1
                or state['max_owned'] != MAX_OWNED
                or directory.read('state.json') != (raw, fingerprint)):
            raise ValueError('supervisor_binding_required')
    finally:
        directory.close()


def _worker_entry(state_dir, adapter_path, expected, seconds):
    """Internal child entry; no worker CLI or configuration/credential fallback."""
    try:
        work_seconds = _budget(float(seconds))
        _parent_state(state_dir, work_seconds)

        def deferred_adapter(request):
            # Capture/import occurs inside PR129's deadline/output fence, after
            # the outer Linux supervisor has established descendant ownership.
            raw = _reviewed_source(adapter_path, expected)
            module = types.ModuleType('reviewed_resume_adapter')
            module.__file__ = adapter_path
            exec(compile(raw, '<reviewed_resume_adapter>', 'exec'), module.__dict__)
            adapter = getattr(module, 'run', None)
            if not callable(adapter):
                raise ValueError('reviewed_entry_required')
            adapter(request)

        result = bounded_resume.run_once(deferred_adapter, execute=True, seconds=work_seconds)
        return 0 if result.get('status') == 'completed' and result.get('completion_claimed') else 10
    except BaseException:
        return 10


def run_once(adapter_path=None, *, sha256=None, state_dir=None, execute=False, seconds=120):
    """Admit one explicitly supplied, reviewed adapter in a dedicated interpreter.

    Import-time effects are part of the reviewed adapter contract. The adapter
    and dependencies must still obey PR129's one-item/no-notify/no-prune/no-child
    request. The outer supervisor additionally cleans up accidental descendants.
    """
    if type(execute) is not bool:
        raise ValueError('invalid_execution_opt_in')
    work_seconds = _budget(seconds)
    if not execute:
        return {'status': 'plan_only', 'max_items': 1, 'notify': False, 'prune': False}
    if adapter_path is None or sha256 is None or state_dir is None:
        return {'status': 'blocked', 'reason': 'reviewed_adapter_binding_required'}
    command = [sys.executable, '-B', '-c',
               'import sys; from campaign_tool.records.contained_resume import _worker_entry; '
               'raise SystemExit(_worker_entry(*sys.argv[1:]))',
               os.fspath(state_dir), os.fspath(adapter_path), sha256, str(seconds)]
    try:
        receipt = worker_lifecycle.supervise(command, state_dir, wall_seconds=work_seconds,
                                             term_seconds=1, kill_seconds=1, max_owned=MAX_OWNED)
        quiescent = receipt['quiescent'] is True
        status = receipt['status'] if quiescent else 'not_quiescent'
        complete = status == 'completed' and receipt['worker_exit'] == 0 and quiescent
        return {'status': status, 'completion_claimed': complete, 'quiescent': quiescent}
    except worker_lifecycle.Unsupported:
        return {'status': 'unsupported', 'completion_claimed': False, 'quiescent': False}
    except Exception:
        return {'status': 'invalid_state', 'completion_claimed': False, 'quiescent': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    result = run_once(execute=args.execute)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'plan_only' else 2


if __name__ == '__main__':
    raise SystemExit(main())
