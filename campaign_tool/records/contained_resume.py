"""Opt-in mechanical completion via the existing Linux owned-child supervisor.

Not a sandbox or adapter admission. Imports still require static dependency review.
"""
import contextlib
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re
import select
import stat
import subprocess
import sys
import time

from . import bounded_resume, worker_lifecycle as lifecycle


@dataclass(frozen=True)
class AdapterIdentity:
    path: str
    sha256: str
    attribute: str


def _result(status, quiescent=False, escalated=False):
    return dict(status=status, completion_claimed=status == 'completed',
                quiescent=quiescent, escalated=escalated)


def _read_file(path, limit, *, private=False):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_size > limit
                or (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise lifecycle.Rejected()
        raw = stream.read(limit + 1)
        if len(raw) != info.st_size:
            raise lifecycle.Rejected()
        return raw


def _stdout(process, seconds):
    deadline = time.monotonic() + seconds
    raw = bytearray()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise subprocess.TimeoutExpired(process.args, seconds)
        chunk = os.read(process.stdout.fileno(), 4097 - len(raw))
        raw.extend(chunk)
        if len(raw) > 4096:
            raise lifecycle.Rejected()
        if not chunk:
            process.wait(timeout=max(0, deadline - time.monotonic()))
            return lifecycle._decode(raw)


def _receipt(state_dir, stdout):
    with contextlib.closing(lifecycle._Directory(state_dir)) as directory:
        state = lifecycle._validate_state(lifecycle._decode(directory.read('state.json')[0]))
        envelope = lifecycle._decode(directory.read('receipt.json')[0])
    if (not isinstance(envelope, dict) or set(envelope) != {'identity', 'boot', 'run_id', 'result'}
            or any(envelope[key] != state[key] for key in ('identity', 'boot', 'run_id'))
            or envelope['result'] != stdout):
        raise lifecycle.Rejected()
    value = envelope['result']
    if (not isinstance(value, dict) or set(value) != set(lifecycle._result('invalid_state'))
            or type(value['schema']) is not int or value['schema'] != 1
            or type(value['quiescent']) is not bool or type(value['escalated']) is not bool
            or (value['worker_exit'] is not None and type(value['worker_exit']) is not int)):
        raise lifecycle.Rejected()
    status = value['status']
    if status == 'completed':
        status = 'completed' if value['quiescent'] is True and value['worker_exit'] == 0 else 'not_quiescent'
    elif status == 'worker_failed':
        status = {3: 'not_admitted', 4: 'timed_out'}.get(value['worker_exit'], 'failed')
    elif status not in {'timed_out', 'not_quiescent', 'invalid_state', 'unsupported', 'capacity_exceeded'}:
        status = 'invalid_state'
    return _result(status, value['quiescent'], value['escalated'])


def run_contained(identity, *, execute=False, seconds=120, run_parent):
    """Capture one reviewed synthetic/admitted module and await a bound receipt."""
    if execute is False:
        return _result('plan_only')
    try:
        lifecycle._linux()
    except lifecycle.Unsupported:
        return _result('unsupported')
    binding = None
    try:
        if (execute is not True or type(seconds) not in (int, float)
                or not math.isfinite(seconds) or not 6 <= seconds <= 120
                or not isinstance(identity, AdapterIdentity) or not os.path.isabs(identity.path)
                or not re.fullmatch(r'[0-9a-f]{64}', identity.sha256)
                or not identity.attribute.isidentifier()):
            raise lifecycle.Rejected()
        raw = _read_file(identity.path, 256 * 1024)
        if hashlib.sha256(raw).hexdigest() != identity.sha256:
            raise lifecycle.Rejected()
        with contextlib.closing(lifecycle._Directory(run_parent)) as directory:
            name = os.urandom(16).hex()
            state_dir = os.path.join(os.fspath(run_parent), name)
            binding = os.path.join(os.fspath(run_parent), name + '.binding')
            fd = os.open(name + '.binding', os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory.fd)
            with os.fdopen(fd, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(dict(data=raw.hex(), sha256=identity.sha256, attribute=identity.attribute,
                               state_dir=state_dir, seconds=seconds - 3), stream)
        command = [sys.executable, '-B', '-m', 'campaign_tool.records.worker_lifecycle',
                   'supervise', '--state-dir', state_dir, '--wall-seconds', str(seconds),
                   '--term-seconds', '2', '--kill-seconds', '2', '--max-owned', '8', '--',
                   sys.executable, '-B', '-m', 'campaign_tool.records.contained_resume',
                   '__supervised_worker__', binding]
        try:
            with subprocess.Popen(command, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE) as process:
                try:
                    stdout = _stdout(process, seconds + 9)
                except BaseException:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
                    raise
            return _receipt(state_dir, stdout)
        except subprocess.TimeoutExpired:
            return _result('not_quiescent')
        except Exception:
            return _result('invalid_state')
    except Exception:
        return _result('not_admitted')
    finally:
        if binding is not None:
            try:
                os.unlink(binding)
            except OSError:
                pass


def _worker(binding):
    try:
        if not os.path.isabs(binding) or not binding.endswith('.binding'):
            raise lifecycle.Rejected()
        state_dir = binding.removesuffix('.binding')
        with contextlib.closing(lifecycle._Directory(state_dir)) as directory:
            state = lifecycle._validate_state(lifecycle._decode(directory.read('state.json')[0]))
        ppid = os.getppid()
        if ppid != state['identity']['pid'] or lifecycle._identity(ppid)[0] != state['identity']:
            raise lifecycle.Rejected()
        with contextlib.closing(lifecycle._Directory(os.path.dirname(binding))):
            value = json.loads(_read_file(binding, 530 * 1024, private=True))
        if value['state_dir'] != state_dir or value['seconds'] != state['wall_seconds'] - 3:
            raise lifecycle.Rejected()
        def loader(request):
            raw = bytes.fromhex(value['data'])
            if hashlib.sha256(raw).hexdigest() != value['sha256']:
                raise lifecycle.Rejected()
            namespace = {'__name__': '_contained_adapter', '__file__': '<captured-adapter>'}
            exec(compile(raw, '<captured-adapter>', 'exec'), namespace)
            adapter = namespace[value['attribute']]
            if not callable(adapter):
                raise lifecycle.Rejected()
            adapter(request)
        result = bounded_resume.run_once(loader, execute=True, seconds=value['seconds'])
        code = {'leader_completed': 0, 'not_admitted': 3, 'timed_out': 4}.get(result['status'], 5)
    except BaseException:
        code = 3
    os._exit(code)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) == 2 and arguments[0] == '__supervised_worker__' and os.path.isabs(arguments[1]):
        _worker(arguments[1])
    status = 'plan_only' if not arguments else 'authorized_adapter_required'
    print(json.dumps(_result(status), sort_keys=True))
    return 0 if not arguments else 2


if __name__ == '__main__':
    raise SystemExit(main())
