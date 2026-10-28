"""Opt-in supervision boundary for an already authorized, reviewed adapter.

This module does not discover adapters, load configuration, read credentials,
or bind a live mail/backup implementation. It is not a sandbox for untrusted code.
"""
import argparse
import contextlib
from dataclasses import dataclass
import json
import multiprocessing
import os
import platform
import signal
import time


@dataclass(frozen=True)
class Request:
    execute: bool = False
    max_items: int = 1
    notify: bool = False
    prune: bool = False
    spawn_children: bool = False


def _worker(adapter, request, deadline, connection):
    """No CLI entry; parent and worker both require the execution permit."""
    try:
        if request != Request(execute=True) or time.monotonic() >= deadline:
            connection.send('not_admitted')
            return
        os.setsid()
        remaining = min(deadline - time.monotonic(), 120)
        if remaining <= 0:
            connection.send('not_admitted')
            return
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError()))
        signal.setitimer(signal.ITIMER_REAL, remaining)
        # Adapter results/exceptions/output can contain private material. Return
        # stable aggregate status only. A reviewed adapter must honor Request.
        with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            adapter(request)
        connection.send('completed')
    except BaseException:
        connection.send('failed')
    finally:
        connection.close()


def _wait(process, seconds):
    """Wait for the worker pid itself, never for the multiprocessing sentinel.

    Every forked descendant inherits the sentinel pipe's write end, so a
    surviving accidental descendant would hold join() open until the deadline
    even after the worker has exited. exitcode polls waitpid(WNOHANG).
    """
    deadline = time.monotonic() + seconds
    while process.exitcode is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(.01, remaining))


def _stop(process):
    # Only the owned worker is guaranteed to be cleaned up. Admission requires
    # separately reviewed adapters and their dependencies to spawn no children.
    if not process.is_alive():
        return
    # A worker creates a new session before entering adapter code. Never signal
    # the supervisor's process group if setup has not finished.
    try:
        if os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    _wait(process, 2)
    if process.is_alive():
        raise RuntimeError('worker_cleanup_failed')


def run_once(adapter=None, *, execute=False, seconds=120):
    if type(execute) is not bool:
        raise ValueError('invalid_execution_opt_in')
    if type(seconds) not in (int, float) or not 0 < seconds <= 120:
        raise ValueError('invalid_deadline')
    if not execute:
        return {'status': 'plan_only', 'max_items': 1, 'notify': False, 'prune': False}
    if adapter is None or not callable(adapter):
        return {'status': 'blocked', 'reason': 'authorized_adapter_required'}
    if platform.system() != 'Linux':
        return {'status': 'blocked', 'reason': 'supported_runtime_required'}
    context = multiprocessing.get_context('fork')
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_worker, args=(adapter, Request(execute=True),
                                                   time.monotonic() + seconds, writer))
    try:
        process.start()
        writer.close()
        _wait(process, seconds)
        if process.is_alive():
            _stop(process)
            return {'status': 'timed_out', 'completion_claimed': False}
        try:
            result = reader.recv() if reader.poll() else 'failed'
        except EOFError:
            result = 'failed'
        return {'status': result, 'completion_claimed': result == 'completed'}
    finally:
        if process.pid is not None:
            _stop(process)
        reader.close()
        writer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    result = run_once(execute=args.execute)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] == 'plan_only' else 2


if __name__ == '__main__':
    raise SystemExit(main())
