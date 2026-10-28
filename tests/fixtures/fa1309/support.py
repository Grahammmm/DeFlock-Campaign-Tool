"""Synthetic-only FA1309 runner support; no application imports at discovery."""
import contextlib
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys
import sysconfig
import tempfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
RUN_ROOT = None
ATTEMPTS = []
_REAL_POPEN = subprocess.Popen


def safe_directory(path):
    """Reject linked ancestry before mkdir, including the supplied leaf."""
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            raise ValueError("unsafe_output_ancestry")
    return path


def fresh(path):
    path = safe_directory(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return Path(tempfile.mkdtemp(prefix="invocation-", dir=path))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def record_command(argv, cwd, destination):
    destination = safe_directory(destination)
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    # Fixed file-based Python/test commands only; never a shell or inline source.
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=120)
    (destination / "stdout.txt").write_text(result.stdout)
    (destination / "stderr.txt").write_text(result.stderr)
    receipt = {"argv": list(map(str, argv)), "cwd": str(cwd), "exit": result.returncode,
               "stdout_sha256": digest(destination / "stdout.txt"),
               "stderr_sha256": digest(destination / "stderr.txt")}
    (destination / "command.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return result


def spawn_suite(repo, mode, output):
    """Only the fixed inert child entry point, never application executables."""
    repo = Path(repo).absolute()
    output = fresh(output)
    argv = [sys.executable, "-B", str(repo / "tests/fixtures/fa1309/child.py"), mode, str(output)]
    # The suite guard rejects generic children; this narrow harness-owned launch
    # retains the real launcher solely for these fixed, source-inspectable children.
    with patch.object(subprocess, "Popen", _REAL_POPEN):
        result = record_command(argv, repo, output / "process")
    return result, output


@contextlib.contextmanager
def boundary(root):
    """Scoped sentinels, installed BEFORE application imports in the child.

    Not a hostile-code sandbox. Read closure: candidate, Python runtime and
    standard library only. All writes and temp defaults stay in this invocation.
    """
    global RUN_ROOT
    root = safe_directory(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    old_root = RUN_ROOT
    RUN_ROOT = root
    prior_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    old_temp = tempfile.tempdir
    tempfile.tempdir = str(root)
    allowed_reads = [REPO.resolve(), Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(),
                     Path(sysconfig.get_path("stdlib")).resolve()]
    real_run = subprocess.run
    existing_ocr = sys.modules.get('campaign_tool.records.extract.ocr')
    defaults = []
    dependency_defaults = None
    if existing_ocr is not None:
        for name, key in (('extract_image_only_pages', 'runner'),):
            mapping = getattr(existing_ocr, name).__kwdefaults__
            defaults.append((mapping, key, mapping[key]))
        dependency_defaults = existing_ocr.dependency_status.__defaults__
    real_open = io.open
    real_os_open = os.open

    def check(file, write=False, dir_fd=None):
        if isinstance(file, int):
            return
        p = Path(os.fsdecode(file))
        if not p.is_absolute():
            if dir_fd is not None:
                p = Path(os.readlink(f"/proc/self/fd/{dir_fd}")) / p
            else:
                p = Path.cwd() / p
        p = p.resolve()
        if p.is_relative_to(root):
            return
        if not write and any(p.is_relative_to(r) for r in allowed_reads):
            return
        ATTEMPTS.append(("file", str(p), write))
        raise RuntimeError("offline_file_sentinel")

    def guarded_open(file, mode="r", *args, **kwargs):
        check(file, any(c in mode for c in "wax+"))
        return real_open(file, mode, *args, **kwargs)

    def guarded_os_open(file, flags, *args, **kwargs):
        # OCR's descriptor walk opens '/' and ancestors read-only without
        # reading their contents; data-file opens remain checked by destination.
        directory = bool(flags & getattr(os, "O_DIRECTORY", 0))
        writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
        if not directory or writing:
            check(file, writing, kwargs.get("dir_fd"))
        return real_os_open(file, flags, *args, **kwargs)

    def denied(*args, **kwargs):
        ATTEMPTS.append(("network_or_process",))
        raise RuntimeError("offline_network_model_process_sentinel")

    def write_guard(function, paths):
        def guarded(*args, **kwargs):
            for index, dir_key in paths:
                if function.__name__ == 'mkdir':
                    p = Path(os.fsdecode(args[index]))
                    if not p.is_absolute():
                        fd = kwargs.get(dir_key)
                        p = (Path(os.readlink(f'/proc/self/fd/{fd}')) if fd is not None else Path.cwd()) / p
                    p = p.resolve()
                    # _directory(create=True) calls mkdir on EVERY ancestor.
                    # Emulate EEXIST for existing ancestors without making any
                    # out-of-fixture syscall or granting ancestor write access.
                    if p != root and root.is_relative_to(p) and p.is_dir():
                        raise FileExistsError(errno.EEXIST, 'existing fixture ancestor', str(p))
                check(args[index], True, kwargs.get(dir_key))
            return function(*args, **kwargs)
        return guarded

    with contextlib.ExitStack() as stack:
        for target, replacement in (("builtins.open", guarded_open), ("io.open", guarded_open),
                                    ("os.open", guarded_os_open), ("socket.socket", denied),
                                    ("socket.create_connection", denied), ("socket.getaddrinfo", denied),
                                    ("subprocess.Popen", denied), ("subprocess.run", denied),
                                    ("os.system", denied)):
            stack.enter_context(patch(target, replacement))
        for name, paths in (('mkdir', [(0, 'dir_fd')]), ('unlink', [(0, 'dir_fd')]),
                            ('rmdir', [(0, 'dir_fd')]),
                            ('rename', [(0, 'src_dir_fd'), (1, 'dst_dir_fd')]),
                            ('replace', [(0, 'src_dir_fd'), (1, 'dst_dir_fd')]),
                            ('link', [(0, 'src_dir_fd'), (1, 'dst_dir_fd')])):
            stack.enter_context(patch.object(os, name, write_guard(getattr(os, name), paths)))
        for mapping, key, _ in defaults:
            mapping[key] = denied
        if existing_ocr is not None:
            existing_ocr.dependency_status.__defaults__ = (dependency_defaults[0], denied, dependency_defaults[2])
        # Imported modules capture these inert subprocess defaults, never live ones.
        try:
            yield
        finally:
            for mapping, key, original in defaults:
                mapping[key] = original
            if existing_ocr is not None:
                existing_ocr.dependency_status.__defaults__ = dependency_defaults
            else:
                imported = sys.modules.get('campaign_tool.records.extract.ocr')
                if imported is not None:
                    imported.extract_image_only_pages.__kwdefaults__['runner'] = real_run
                    d = imported.dependency_status.__defaults__
                    imported.dependency_status.__defaults__ = (d[0], real_run, d[2])
            RUN_ROOT = old_root
            tempfile.tempdir = old_temp
            sys.dont_write_bytecode = prior_bytecode


def invoke_child(repo, mode, output):
    # Called from guarded tests; temporarily expose only the harness recorder's
    # stdlib launcher, with fixed argv generated in spawn_suite.
    def run(argv, **kwargs):
        with _REAL_POPEN(argv, **{k: v for k, v in kwargs.items()
                               if k not in {"capture_output", "check", "timeout"}},
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            stdout, stderr = process.communicate(timeout=120)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    with patch.object(subprocess, "run", run):
        return spawn_suite(repo, mode, output)


def disposable(repo, parent):
    target = fresh(parent) / "repo"
    shutil.copytree(repo, target, ignore=shutil.ignore_patterns(".git", ".venv", "node_modules", "__pycache__"))
    return target
