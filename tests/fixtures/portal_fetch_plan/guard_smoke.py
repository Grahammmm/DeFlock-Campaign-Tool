"""Isolated pre-import guard smoke test; synthetic paths only, never a CLI."""
from pathlib import Path
import os
import sys
from urllib.parse import unquote

REPO = Path(__file__).resolve().parents[3]
OUT = Path(sys.argv[1]).resolve()
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))
READ_ROOTS = (REPO, OUT, Path('/usr'), Path('/lib'), Path('/lib64'))


def below(path, root):
    return path == root or root in path.parents


def guard(event, args):
    if event == 'sqlite3.connect':
        name = os.fsdecode(args[0])
        if name.startswith('file:'):
            name = unquote(name[5:].split('?', 1)[0])
        if not below(Path(name).absolute().resolve(), OUT):
            raise PermissionError('offline_write_boundary')
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.link', 'os.symlink', 'os.rename'):
        names = args[:2] if event in ('os.link', 'os.rename') else args[:1]
        if event == 'os.symlink':
            names = args[1:2]
        for name in names:
            if isinstance(name, (str, bytes, os.PathLike)):
                if not below(Path(os.fsdecode(name)).absolute().resolve(), OUT):
                    raise PermissionError('offline_write_boundary')
    if event == 'open':
        name, mode, flags = args
        if not isinstance(name, (str, bytes, os.PathLike)):
            return
        path = Path(os.fsdecode(name)).absolute().resolve()
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
        if writing and not below(path, OUT):
            raise PermissionError('offline_write_boundary')
        if not writing and not any(below(path, root) for root in READ_ROOTS):
            raise PermissionError('offline_read_boundary')
    if event in ('socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr'):
        raise PermissionError('offline_dns_forbidden')
    if event in ('socket.connect', 'socket.bind'):
        address = args[1]
        if not isinstance(address, tuple) or address[0] != '127.0.0.1':
            raise PermissionError('offline_nonloopback_forbidden')
    if event in ('subprocess.Popen', 'os.system', 'os.posix_spawn', 'os.fork'):
        raise PermissionError('offline_child_forbidden')


sys.addaudithook(guard)
# Test the guard before importing any production-bound clock or transport default.
for event, args, reason in (
    ('socket.connect', (None, ('nonloopback.fixture.invalid', 443)), 'offline_nonloopback_forbidden'),
    ('socket.getaddrinfo', ('fixture.invalid', 443), 'offline_dns_forbidden'),
    ('open', (str(REPO / 'forbidden-sentinel'), 'w', os.O_WRONLY | os.O_CREAT), 'offline_write_boundary'),
    ('sqlite3.connect', (str(REPO / 'forbidden.sqlite'),), 'offline_write_boundary'),
):
    try:
        guard(event, args)
    except PermissionError as exc:
        assert str(exc) == reason
    else:
        raise AssertionError('guard did not reject probe')
assert not (REPO / 'forbidden-sentinel').exists()
from campaign_tool.records.portal.download import run_download
from campaign_tool.records.portal.store import Queue
from campaign_tool.records.portal.policy import fetch_settings
assert not fetch_settings({}).apply
queue = Queue(OUT / 'guard-queue')
try:
    status = queue.status()
    assert run_download(queue, OUT / 'unused-processing') == {'mode': 'inventory_only', **status}
    assert queue.status() == status
finally:
    queue.close()
print('pre-import guards and dry default: OK')
