"""Scoped offline acceptance runner for FA49 (truncated OCR confidence TSV rows).

One invocation runs exactly one case and writes its evidence to a fresh, unique
child directory below --output-parent, which must lie outside this repository
and must not pass through a symbolic link:

  behavior          tests/records/test_fa49_ocr_tsv.py (A1 positive/boundaries,
                    A2 malformed rows, A3 real extraction consumer and replay)
  compatibility     the unchanged OCR test files named in COMPATIBILITY, with a
                    receipt (argv, cwd, exit, captured stdout/stderr and their
                    hashes) for every child process they start
  negative-control  A4: the truncated-row and consumer regressions run against
                    the complete preserved baseline ocr.py (loaded in memory
                    from git history) must ERROR with AttributeError while the
                    normal two-row positive still passes; the same selection
                    then passes against the real candidate module
  isolation         A5: inert guard decision self-tests, real guard/redirect
                    probes, committed-source portability scan, then the
                    behavior case rerun as a child process from a second
                    complete source copy
  credentials       A6: the detect-secrets installed next to this interpreter,
                    then the unchanged tools/check_secret_report.py on that
                    exact report

Before any repository module is imported this runner disables bytecode writes,
redirects tempfile/TMPDIR into the per-run directory, and installs an audit
hook that refuses non-loopback network lookups/connects (loopback connects are
allowed only to sockets bound by this process or to ports with no host
listener, so a host-local model service stays unreachable), reads under the
user's home outside the repository/run/interpreter roots, and Python-level
writes outside the per-run directory. Relative paths are resolved against the
current working directory; ``os.open`` with ``dir_fd`` is wrapped so the name
is resolved against that descriptor before the call, and a path that cannot be
resolved is refused. For the focused cases it also refuses process creation
while tests run and replaces the OCR module's bound default runner/probe
callables with refusing sentinels.

Every refusal is fatal unless it is the single exact (kind, event, target)
announced by a deliberate self-probe phase; an announced probe that does not
fire exactly once is also fatal. Focused cases work from a cwd inside the run
directory; compatibility keeps the invocation cwd because unchanged production
children are started with ``python -m``. This is a test guard, not a
hostile-code sandbox. A pass is REVIEW REQUIRED; it is not full-repository CI,
semantic, human or release acceptance.
"""
import argparse
import contextlib
import datetime
import hashlib
import inspect
import io
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import types
import weakref

REPO = Path(__file__).resolve().parents[1]
OCR_MODULE = "campaign_tool.records.extract.ocr"
OCR_SOURCE = "campaign_tool/records/extract/ocr.py"
REGRESSION = "tests.records.test_fa49_ocr_tsv"
COMPATIBILITY = ("tests.records.test_ocr", "tests.records.test_ocr_stage", "tests.records.test_run_pdf_ocr")
# Real children the unchanged compatibility files must start (basename of argv[0]).
COMPATIBILITY_REQUIRED_TOOLS = ("pdftoppm", "tesseract")
NEG_BROKEN = (f"{REGRESSION}.MalformedConfidenceTests.test_a2_truncated_last_row_has_no_text_cell",
              f"{REGRESSION}.ConsumerTests.test_a3_truncated_page_blocked_while_peer_page_unreviewed")
NEG_POSITIVE = f"{REGRESSION}.PositiveConfidenceTests.test_a1_two_valid_word_rows_mean"
HASHED = (OCR_SOURCE, "tests/records/test_fa49_ocr_tsv.py", "tools/accept_fa49_ocr_tsv.py",
          "tests/records/test_ocr.py", "tests/records/test_ocr_stage.py",
          "tests/records/test_run_pdf_ocr.py", "tools/check_secret_report.py")
PORTABILITY_FILES = ("tests/records/test_fa49_ocr_tsv.py", "tools/accept_fa49_ocr_tsv.py")
# Built by concatenation so this file does not match its own scan.
PORTABILITY_NEEDLES = ("/" + "home" + "/", "/" + "Users" + "/", "agent" + "-work", ".her" + "mes",
                       "/" + "tmp" + "/", "kan" + "ban")
PROBE_PREFIX = "fa49-probe-"

STATE = {"run": None, "allowed_reads": (), "home": None, "deny_process": False, "bound": [],
         "record_children": False, "children_dir": None}
COUNTERS = {"dir_fd_opens_attributed": 0}
LOCAL = threading.local()
REAL_OS_OPEN = os.open
REAL_POPEN = subprocess.Popen

WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
# audit event -> ((path arg index, dir_fd arg index or None), ...)
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 2),), "os.link": ((1, 3),), "os.chmod": ((0, 2),),
                "os.chown": ((0, 3),), "os.utime": ((0, 3),), "shutil.rmtree": ((0, 1),),
                "shutil.copyfile": ((1, None),), "shutil.copytree": ((1, None),),
                "shutil.move": ((1, None),), "sqlite3.connect": ((0, None),)}
NETWORK_EVENTS = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                  "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg", "socket.bind")
PROCESS_EVENTS = ("subprocess.Popen", "os.posix_spawn", "os.spawn", "os.exec", "os.fork",
                  "os.forkpty", "os.system", "os.startfile", "pty.spawn")
# Events whose final path component is acted on without following a symlink.
NOFOLLOW_EVENTS = {"os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.symlink",
                   "os.link", "shutil.rmtree", "shutil.move"}


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


class GuardLedger:
    """Refusal accounting. A refusal counts as a probe only when it is the one exact
    (kind, event, target) announced by the currently open expect() phase; every other
    refusal, a second matching refusal, and an announced probe that never fires are fatal."""

    KINDS = ("network", "write", "read", "process")

    def __init__(self):
        self.refused = {kind: [] for kind in self.KINDS}
        self.probes = {kind: [] for kind in self.KINDS}
        self.unmet = []
        self.expected = None

    @contextlib.contextmanager
    def expect(self, kind, event, target):
        if self.expected is not None:
            raise RuntimeError("nested probe phase")
        slot = {"kind": kind, "event": event, "target": target, "hits": 0}
        self.expected = slot
        try:
            yield slot
        finally:
            self.expected = None
            if slot["hits"] != 1:
                self.unmet.append(f"{kind}:{event}:{target}")

    def record(self, kind, event, target):
        slot = self.expected
        entry = f"{event}:{target}"
        if (slot is not None and slot["hits"] == 0
                and (slot["kind"], slot["event"], slot["target"]) == (kind, event, target)):
            slot["hits"] = 1
            self.probes[kind].append(entry)
            return "probe"
        self.refused[kind].append(entry)
        return "refused"

    def clear(self):
        return not any(self.refused.values()) and not self.unmet


LEDGER = GuardLedger()


def resolve(target, dir_fd=None, follow=True, cwd=None):
    """Absolute real path of target, or "" when it cannot be attributed (callers refuse).

    Relative names are joined to the directory behind dir_fd when one is given,
    otherwise to cwd (default: the process working directory). When follow is False
    the final component is kept unresolved, as the nofollow syscalls act on it."""
    if isinstance(target, int):
        return None
    text = os.fsdecode(target)
    if not os.path.isabs(text):
        if dir_fd is not None:
            if not isinstance(dir_fd, int) or isinstance(dir_fd, bool) or dir_fd < 0:
                return ""
            try:
                base = os.readlink(f"/proc/self/fd/{dir_fd}")
            except OSError:
                return ""
            if not os.path.isabs(base) or not os.path.isdir(base):
                return ""
        else:
            base = cwd if cwd is not None else os.getcwd()
        text = os.path.join(base, text)
    if follow:
        return os.path.realpath(text)
    head, tail = os.path.split(text)
    if tail in ("", ".", ".."):
        return os.path.realpath(text)
    return os.path.join(os.path.realpath(head), tail)


def under(path, root):
    return bool(path) and bool(root) and (path == root or path.startswith(root + os.sep))


def decide_path(event, target, dir_fd, writing, state, cwd=None):
    """None when allowed, else the refusal (kind, event, target text). Reads the
    filesystem only to resolve the path; records nothing."""
    if isinstance(target, int):
        return None  # descriptor-based call: the descriptor was checked when it was opened
    text = os.fsdecode(target)
    if event == "sqlite3.connect":
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
            if text.startswith("///"):
                text = text[2:]
            target = text
        if text in ("", ":memory:"):
            return None
    if text == os.devnull:
        return None
    if text == "":
        return None if not writing else ("write", event, text)
    resolved = resolve(target, dir_fd, follow=event not in NOFOLLOW_EVENTS, cwd=cwd)
    if resolved is None:
        return None
    if writing:
        if under(resolved, state["run"]):
            return None
        if event == "os.mkdir" and resolved and os.path.lexists(resolved):
            return None  # existing ancestor: the syscall itself fails with EEXIST, nothing is written
        return ("write", event, text)
    if not resolved:
        return ("read", event, text)
    home = state["home"]
    if under(resolved, home) and not any(under(resolved, root) for root in state["allowed_reads"]):
        if any(under(root, resolved) for root in state["allowed_reads"]):
            return None  # an ancestor directory of an admitted root, opened to walk down by dir_fd
        return ("read", event, text)
    return None


def decide(event, args, state, cwd=None, preattributed=None):
    """Filesystem decision for one audit event: None or the first refusal."""
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if path is None or isinstance(path, int):
            return None
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & WRITE_FLAGS))
        text = os.fsdecode(path)
        if preattributed is not None and not os.path.isabs(text) and text == preattributed:
            return None  # already decided against its dir_fd by guarded_os_open
        return decide_path("open", path, None, writing, state, cwd)
    if event in WRITE_EVENTS:
        for p, d in WRITE_EVENTS[event]:
            if p >= len(args) or args[p] is None:
                continue
            dir_fd = args[d] if d is not None and d < len(args) else None
            refusal = decide_path(event, args[p], dir_fd, True, state, cwd)
            if refusal:
                return refusal
    return None


def loopback(host):
    if host is None or host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def in_process_ports():
    ports = set()
    for ref in list(STATE["bound"]):
        sock = ref()
        if sock is None:
            continue
        try:
            ports.add(sock.getsockname()[1])
        except (OSError, IndexError, TypeError):
            continue
    return ports


def host_listening_ports():
    """Ports with a LISTEN socket anywhere on this host (read-only /proc view)."""
    ports = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table) as handle:
                rows = handle.read().splitlines()[1:]
        except OSError:
            return None  # unknown: caller fails closed
        for row in rows:
            fields = row.split()
            if len(fields) > 3 and fields[3] == "0A":
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def network_allowed(event, args):
    """Loopback lookups; loopback connects only to this process's own sockets or to an
    unused port (which can only be refused). Any other host-local listener, such as a
    model service, and every non-loopback address stay refused."""
    if event == "socket.bind":
        try:
            STATE["bound"].append(weakref.ref(args[0]))
        except TypeError:
            STATE["bound"].append(lambda sock=args[0]: sock)
        return True
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
        host = args[0] if args else None
        return loopback(os.fsdecode(host) if isinstance(host, bytes) else host)
    if event == "socket.connect" and len(args) > 1:
        address = args[1]
        if isinstance(address, tuple) and len(address) >= 2 and loopback(address[0]):
            if address[1] in in_process_ports():
                return True
            listening = host_listening_ports()
            return listening is not None and address[1] not in listening
    return False


def network_target(event, args):
    if event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr") and args:
        host = args[0]
        return os.fsdecode(host) if isinstance(host, bytes) else str(host)
    return repr(tuple(args)[1:])


def process_target(event, args):
    if event == "subprocess.Popen" and len(args) > 1:
        executable, argv = args[0], args[1]
        if executable:
            return os.fsdecode(executable)
        if isinstance(argv, (list, tuple)) and argv:
            return os.fsdecode(argv[0])
        if isinstance(argv, (str, bytes, os.PathLike)):
            return os.fsdecode(argv)
        return repr(argv)
    return repr(args[0]) if args else ""


def refuse(kind, event, target):
    LEDGER.record(kind, event, target)
    raise PermissionError(f"FA49 acceptance guard refused {kind} {event}")


def audit(event, args):
    if STATE["run"] is None:
        return
    if event in NETWORK_EVENTS:
        if not network_allowed(event, args):
            refuse("network", event, network_target(event, args))
        return
    if event in PROCESS_EVENTS:
        if STATE["record_children"]:
            note_spawn(event, args)
        if STATE["deny_process"]:
            refuse("process", event, process_target(event, args))
        return
    refusal = decide(event, args, STATE, preattributed=getattr(LOCAL, "preattributed", None))
    if refusal:
        refuse(*refusal)


def guarded_os_open(path, flags, mode=0o777, *, dir_fd=None):
    """os.open with the relative name attributed to its dir_fd before the call; the
    open audit event that follows carries no dir_fd and is told it is already decided."""
    if STATE["run"] is not None and dir_fd is not None and not isinstance(path, int):
        text = os.fsdecode(path)
        if not os.path.isabs(text):
            refusal = decide_path("open", path, dir_fd, bool(flags & WRITE_FLAGS), STATE)
            if refusal:
                refuse(*refusal)
            COUNTERS["dir_fd_opens_attributed"] += 1
            previous = getattr(LOCAL, "preattributed", None)
            LOCAL.preattributed = text
            try:
                return REAL_OS_OPEN(path, flags, mode, dir_fd=dir_fd)
            finally:
                LOCAL.preattributed = previous
    return REAL_OS_OPEN(path, flags, mode, dir_fd=dir_fd)


# ---------------------------------------------------------------- child receipts

RECEIPTS = []
SPAWNS = []
POPENS = []
RECEIPT_LOCK = threading.Lock()
POPEN_SIGNATURE = inspect.signature(REAL_POPEN.__init__)
DISPOSITIONS = ("inherit", "pipe", "devnull", "stdout", "file")


def disposition(value):
    if value is None:
        return "inherit"
    if value is subprocess.PIPE or value == subprocess.PIPE:
        return "pipe"
    if value == subprocess.DEVNULL:
        return "devnull"
    if value == subprocess.STDOUT:
        return "stdout"
    return "file"


def argv_text(raw):
    if isinstance(raw, (str, bytes, os.PathLike)):
        return [os.fsdecode(raw)]
    try:
        return [os.fsdecode(item) for item in raw]
    except TypeError:
        return None


def note_spawn(event, args):
    SPAWNS.append({"event": event, "target": process_target(event, args),
                   "receipt": getattr(LOCAL, "in_popen", None)})


class RecordingPopen(REAL_POPEN):
    """subprocess.Popen that keeps an external receipt of argv, cwd, stream
    dispositions, captured stdout/stderr bytes (files + hashes) and the exit status."""

    def __init__(self, *args, **kwargs):
        bound = POPEN_SIGNATURE.bind(self, *args, **kwargs)
        bound.apply_defaults()
        given = bound.arguments
        cwd = given.get("cwd")
        with RECEIPT_LOCK:
            receipt = {"index": len(RECEIPTS), "argv": argv_text(given.get("args")),
                       "shell": bool(given.get("shell")),
                       "executable": os.fsdecode(given["executable"]) if given.get("executable") else None,
                       "cwd": os.path.realpath(os.fsdecode(cwd)) if cwd else os.getcwd(),
                       "stdin": disposition(given.get("stdin")),
                       "stdout": {"disposition": disposition(given.get("stdout")), "captured": []},
                       "stderr": {"disposition": disposition(given.get("stderr")), "captured": []},
                       "text": bool(given.get("text") or given.get("universal_newlines")
                                    or given.get("encoding") or given.get("errors")),
                       "spawned": None, "exit": None, "communicate_timeouts": 0}
            RECEIPTS.append(receipt)
        self._fa49_receipt = receipt
        previous = getattr(LOCAL, "in_popen", None)
        LOCAL.in_popen = receipt["index"]
        try:
            super().__init__(*args, **kwargs)
        except BaseException as exc:
            receipt["spawned"] = False
            receipt["spawn_error"] = type(exc).__name__
            raise
        finally:
            LOCAL.in_popen = previous
        receipt["spawned"] = True
        receipt["pid"] = self.pid
        POPENS.append(self)

    def communicate(self, input=None, timeout=None):
        try:
            out, err = super().communicate(input, timeout)
        except subprocess.TimeoutExpired:
            self._fa49_receipt["communicate_timeouts"] += 1
            raise
        store_streams(self._fa49_receipt, out, err)
        return out, err


def store_streams(receipt, out, err):
    folder = STATE["children_dir"]
    for name, data in (("stdout", out), ("stderr", err)):
        if data is None or receipt[name]["disposition"] != "pipe" or folder is None:
            continue
        raw = data.encode() if isinstance(data, str) else bytes(data)
        captured = receipt[name]["captured"]
        path = Path(folder) / f"{receipt['index']:04d}-{name}-{len(captured)}.bin"
        path.write_bytes(raw)
        captured.append({"file": str(path.relative_to(STATE["run"])), "sha256": sha256_bytes(raw),
                         "bytes": len(raw)})


def finalize_receipts():
    for proc in POPENS:
        if proc.returncode is None:
            proc.poll()
        proc._fa49_receipt["exit"] = proc.returncode


def validate_receipts(receipts, spawns, run, required_tools=(), required_from=0):
    """Problems with a receipt set (empty list when complete and well-formed)."""
    problems = []
    run = os.path.realpath(run)
    if not isinstance(receipts, list) or not receipts:
        return ["no_child_receipts"]
    indexes = set()
    for position, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            problems.append(f"receipt_not_object:{position}")
            continue
        index = receipt.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index in indexes:
            problems.append(f"receipt_bad_index:{position}")
            continue
        indexes.add(index)
        argv = receipt.get("argv")
        if not (isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv) and argv[0]):
            problems.append(f"receipt_bad_argv:{index}")
        cwd = receipt.get("cwd")
        if not (isinstance(cwd, str) and os.path.isabs(cwd)):
            problems.append(f"receipt_bad_cwd:{index}")
        spawned = receipt.get("spawned")
        if spawned is False:
            if not receipt.get("spawn_error"):
                problems.append(f"receipt_unspawned_without_error:{index}")
            continue
        if spawned is not True:
            problems.append(f"receipt_spawn_state_unknown:{index}")
            continue
        if not isinstance(receipt.get("exit"), int) or isinstance(receipt.get("exit"), bool):
            problems.append(f"receipt_missing_exit:{index}")
        for name in ("stdout", "stderr"):
            stream = receipt.get(name)
            if not isinstance(stream, dict) or stream.get("disposition") not in DISPOSITIONS:
                problems.append(f"receipt_bad_{name}:{index}")
                continue
            captured = stream.get("captured")
            if not isinstance(captured, list):
                problems.append(f"receipt_bad_{name}_capture:{index}")
                continue
            if stream["disposition"] == "pipe" and not captured and not receipt.get("communicate_timeouts"):
                problems.append(f"receipt_{name}_pipe_not_captured:{index}")
            for item in captured:
                file = item.get("file") if isinstance(item, dict) else None
                path = os.path.realpath(os.path.join(run, file)) if isinstance(file, str) else ""
                if not under(path, run) or not os.path.isfile(path):
                    problems.append(f"receipt_{name}_file_missing:{index}")
                    continue
                data = Path(path).read_bytes()
                if item.get("sha256") != sha256_bytes(data) or item.get("bytes") != len(data):
                    problems.append(f"receipt_{name}_hash_mismatch:{index}")
    announced = set()
    for spawn in spawns:
        owner = spawn.get("receipt") if isinstance(spawn, dict) else None
        if owner not in indexes:
            problems.append(f"unreceipted_spawn:{spawn.get('event') if isinstance(spawn, dict) else spawn}")
        elif spawn.get("event") == "subprocess.Popen":
            announced.add(owner)
    for receipt in receipts:
        if isinstance(receipt, dict) and receipt.get("spawned") is True and receipt.get("index") not in announced:
            problems.append(f"receipt_without_spawn_event:{receipt.get('index')}")
    for tool in required_tools:
        if not any(isinstance(r, dict) and isinstance(r.get("index"), int) and r["index"] >= required_from
                   and r.get("spawned") is True and r.get("exit") == 0 and isinstance(r.get("argv"), list)
                   and r["argv"] and os.path.basename(r["argv"][0]) == tool for r in receipts):
            problems.append(f"required_child_missing:{tool}")
    return problems


# ---------------------------------------------------------------- run setup

def safe_output_parent(raw):
    parent = Path(raw)
    if not parent.is_absolute():
        raise SystemExit("output_parent_must_be_absolute")
    parts = Path(os.path.normpath(parent)).parts
    if ".." in parent.parts or os.path.normpath(parent) != str(parent).rstrip(os.sep):
        raise SystemExit("output_parent_not_normalized")
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            raise SystemExit("output_parent_symlink_refused")
        if current.exists() and not current.is_dir():
            raise SystemExit("output_parent_not_directory")
    if os.path.realpath(parent) != str(parent).rstrip(os.sep):
        raise SystemExit("output_parent_not_canonical")
    if under(str(parent), str(REPO)):
        raise SystemExit("output_parent_inside_repository")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if parent.is_symlink() or os.path.realpath(parent) != str(parent).rstrip(os.sep):
        raise SystemExit("output_parent_changed_during_validation")
    return parent


def fresh_run_dir(parent, case):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = parent / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run.mkdir(mode=0o700, exist_ok=False)
    info = os.lstat(run)
    if not os.path.isdir(run) or os.path.islink(run) or info.st_uid != os.geteuid():
        raise SystemExit("run_directory_unsafe")
    return run


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def tree_digest():
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(REPO):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
            digest.update(str(path.relative_to(REPO)).encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def identity():
    hashes = {}
    for relative in HASHED:
        path = REPO / relative
        hashes[relative] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    toplevel = git("rev-parse", "--show-toplevel")
    checkout = toplevel is not None and os.path.realpath(toplevel) == str(REPO)
    return {"repo": str(REPO), "git_checkout": checkout,
            "head": git("rev-parse", "HEAD") if checkout else None,
            "status_porcelain": git("status", "--porcelain") if checkout else None,
            "source_sha256": hashes, "tree_sha256": tree_digest()}


def prepare(run, *, deny_process, work_in_run=True, record_children=False):
    """Guards and redirects; must run before any repository import.

    Focused cases work from a cwd inside the run directory. The compatibility case
    keeps the invocation cwd because unchanged production children are started
    with ``python -m`` and resolve the package from their inherited cwd."""
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    tmp = run / "tmp"
    tmp.mkdir(mode=0o700)
    work = run / "cwd"
    work.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(tmp)
    home = os.path.realpath(os.path.expanduser("~"))
    roots = {str(REPO), os.path.realpath(run), os.path.realpath(sys.prefix),
             os.path.realpath(sys.base_prefix), os.path.realpath(sys.exec_prefix)}
    STATE.update(run=os.path.realpath(run), home=home, allowed_reads=tuple(sorted(roots)))
    if record_children:
        children = run / "children"
        children.mkdir(mode=0o700)
        STATE["children_dir"] = str(children)
        subprocess.Popen = RecordingPopen  # before any repository import binds subprocess.run
        STATE["record_children"] = True
    os.open = guarded_os_open
    os.supports_dir_fd.add(guarded_os_open)
    sys.addaudithook(audit)
    if work_in_run:
        os.chdir(work)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    STATE["deny_process"] = deny_process
    return {"tmp": str(tmp), "cwd": os.getcwd(), "allowed_read_roots": list(STATE["allowed_reads"]),
            "process_creation_refused_during_tests": deny_process, "child_receipts": record_children}


class SentinelError(RuntimeError):
    pass


def refusing(name):
    def sentinel(*args, **kwargs):
        LEDGER.record("process", "bound_default", name)
        raise SentinelError(f"FA49 bound-default sentinel refused {name}")
    sentinel.fa49_sentinel = name
    return sentinel


def redirect_bound_defaults():
    import importlib
    ocr = importlib.import_module(OCR_MODULE)
    kw = dict(ocr.extract_image_only_pages.__kwdefaults__)
    kw["runner"] = refusing("extract_image_only_pages.runner")
    ocr.extract_image_only_pages.__kwdefaults__ = kw
    ocr.dependency_status.__defaults__ = (refusing("dependency_status.which"),
                                          refusing("dependency_status.probe"),
                                          refusing("dependency_status.module_probe"))
    return ocr


def counts(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses)}


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def run_names(names, stream):
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    selected = suite.countTestCases()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    c = counts(result)
    c["selected"] = selected
    return c, result


def module_paths(names):
    """Actual imported file of the OCR module and of each selected test module."""
    paths = {}
    for name in (OCR_MODULE, *names):
        parts = name.split(".")
        module = next((sys.modules[".".join(parts[:n])] for n in range(len(parts), 0, -1)
                       if ".".join(parts[:n]) in sys.modules), None)
        paths[getattr(module, "__name__", name)] = getattr(module, "__file__", None)
    return paths


def guards_clear():
    return LEDGER.clear()


def write_raw(run, stream):
    raw = stream.getvalue().encode()
    (run / "unittest.txt").write_bytes(raw)
    return {"raw_output": "unittest.txt", "raw_output_sha256": sha256_bytes(raw)}


def case_tests(run, names, *, focused):
    setup = prepare(run, deny_process=False, work_in_run=focused)
    if focused:
        redirect_bound_defaults()
        STATE["deny_process"] = True
        setup["process_creation_refused_during_tests"] = True
    stream = io.StringIO()
    c, _ = run_names(names, stream)
    STATE["deny_process"] = False
    paths = module_paths(names)
    inside = all(p and under(os.path.realpath(p), str(REPO)) for p in paths.values())
    ok = clean(c) and c["selected"] == c["tests_run"] and guards_clear() and inside
    return ok, {"setup": setup, "modules": list(names), "counts": c, "module_paths": paths,
                "module_paths_inside_repo": inside, **write_raw(run, stream)}


def receipt_self_test(run):
    """Synthetic receipt sets: a complete one must validate and each broken one must not."""
    root = run / "receipt-self-test"
    root.mkdir(mode=0o700)
    data = b"synthetic child stdout\n"
    (root / "out.bin").write_bytes(data)
    rel = str((root / "out.bin").relative_to(run))

    def receipt(**changes):
        base = {"index": 0, "argv": ["synthetic-tool", "-v"], "cwd": str(root), "spawned": True, "exit": 0,
                "stdout": {"disposition": "pipe", "captured": [{"file": rel, "sha256": sha256_bytes(data),
                                                                 "bytes": len(data)}]},
                "stderr": {"disposition": "devnull", "captured": []}, "communicate_timeouts": 0}
        base.update(changes)
        return base

    spawn = [{"event": "subprocess.Popen", "target": "synthetic-tool", "receipt": 0}]
    broken = {
        "missing_exit": ([receipt(exit=None)], spawn, ()),
        "hash_mismatch": ([receipt(stdout={"disposition": "pipe", "captured": [
            {"file": rel, "sha256": "0" * 64, "bytes": len(data)}]})], spawn, ()),
        "file_missing": ([receipt(stdout={"disposition": "pipe", "captured": [
            {"file": "receipt-self-test/absent.bin", "sha256": sha256_bytes(data), "bytes": len(data)}]})],
            spawn, ()),
        "file_outside_run": ([receipt(stdout={"disposition": "pipe", "captured": [
            {"file": "../outside.bin", "sha256": sha256_bytes(data), "bytes": len(data)}]})], spawn, ()),
        "pipe_not_captured": ([receipt(stdout={"disposition": "pipe", "captured": []})], spawn, ()),
        "bad_argv": ([receipt(argv=[])], spawn, ()),
        "relative_cwd": ([receipt(cwd="relative")], spawn, ()),
        "unreceipted_spawn": ([receipt()], spawn + [{"event": "os.posix_spawn", "target": "x", "receipt": None}],
                              ()),
        "receipt_without_spawn_event": ([receipt()], [], ()),
        "required_tool_missing": ([receipt()], spawn, ("pdftoppm",)),
        "empty": ([], [], ()),
    }
    results = {"complete_valid": validate_receipts([receipt()], spawn, run, ("synthetic-tool",)) == []}
    for name, (receipts, spawns, required) in broken.items():
        results[f"rejects_{name}"] = bool(validate_receipts(receipts, spawns, run, required))
    return results


def receipt_live_controls(run):
    """Real recorded children of this interpreter: a success, a nonzero exit and a
    spawn failure. Each receipt must carry the actual argv, cwd, exit and stream hash."""
    checks = {}
    cwd = os.path.realpath(run)
    ok_argv = [sys.executable, "-B", "--version"]
    done = subprocess.run(ok_argv, cwd=cwd, capture_output=True, check=False, timeout=60)
    r = RECEIPTS[-1]
    checks["success_receipt"] = (r["argv"] == ok_argv and r["cwd"] == cwd and r["spawned"] is True
                                 and r["stdout"]["captured"]
                                 and r["stdout"]["captured"][-1]["sha256"] == sha256_bytes(done.stdout)
                                 and r["stderr"]["captured"][-1]["sha256"] == sha256_bytes(done.stderr)
                                 and done.returncode == 0)
    absent_script = os.path.join(cwd, "receipt-control-absent-script.py")
    fail_argv = [sys.executable, "-B", absent_script]
    done = subprocess.run(fail_argv, cwd=cwd, capture_output=True, check=False, timeout=60)
    r = RECEIPTS[-1]
    checks["nonzero_receipt"] = (r["argv"] == fail_argv and done.returncode != 0 and bool(done.stderr)
                                 and r["stderr"]["captured"][-1]["sha256"] == sha256_bytes(done.stderr))
    missing = os.path.join(cwd, "receipt-control-absent-executable")
    try:
        subprocess.run([missing], cwd=cwd, capture_output=True, check=False, timeout=60)
        checks["spawn_error_receipt"] = False
    except FileNotFoundError:
        r = RECEIPTS[-1]
        checks["spawn_error_receipt"] = r["argv"] == [missing] and r["spawned"] is False and bool(r["spawn_error"])
    finalize_receipts()
    checks["exits_recorded"] = [r["exit"] for r in RECEIPTS[:2]] == [0, 2]
    return checks


def case_compatibility(run):
    setup = prepare(run, deny_process=False, work_in_run=False, record_children=True)
    self_test = receipt_self_test(run)
    live = receipt_live_controls(run)
    start = len(RECEIPTS)
    stream = io.StringIO()
    c, _ = run_names(COMPATIBILITY, stream)
    finalize_receipts()
    STATE["record_children"] = False
    problems = validate_receipts(list(RECEIPTS), list(SPAWNS), run, COMPATIBILITY_REQUIRED_TOOLS, start)
    receipts_doc = {"receipts": RECEIPTS, "spawn_events": SPAWNS, "tests_start_index": start,
                    "problems": problems}
    raw = (json.dumps(receipts_doc, indent=2, sort_keys=True) + "\n").encode()
    (run / "child-receipts.json").write_bytes(raw)
    paths = module_paths(COMPATIBILITY)
    inside = all(p and under(os.path.realpath(p), str(REPO)) for p in paths.values())
    test_receipts = RECEIPTS[start:]
    ok = (clean(c) and c["selected"] == c["tests_run"] and guards_clear() and inside and not problems
          and all(self_test.values()) and all(live.values()) and bool(test_receipts))
    return ok, {"setup": setup, "modules": list(COMPATIBILITY), "counts": c, "module_paths": paths,
                "module_paths_inside_repo": inside, "receipt_self_test": self_test,
                "receipt_live_controls": live, "child_receipts": "child-receipts.json",
                "child_receipts_sha256": sha256_bytes(raw), "child_receipt_problems": problems,
                "child_receipts_total": len(RECEIPTS), "child_receipts_from_tests": len(test_receipts),
                "children_by_tool": sorted({os.path.basename(r["argv"][0]) for r in test_receipts
                                            if r.get("argv")}),
                **write_raw(run, stream)}


def baseline_source():
    """Newest first-parent ancestor whose ocr.py differs from the working tree."""
    current = sha256_bytes((REPO / OCR_SOURCE).read_bytes())
    revisions = (git("rev-list", "--first-parent", "--max-count=200", "HEAD") or "").split()
    for revision in revisions:
        done = subprocess.run(["git", "-C", str(REPO), "show", f"{revision}:{OCR_SOURCE}"],
                              capture_output=True, check=False)
        if done.returncode == 0 and sha256_bytes(done.stdout) != current:
            return revision, done.stdout
    return None, None


def error_types(result):
    found = {}
    for test, trace in result.errors + result.failures:
        last = trace.strip().splitlines()[-1] if trace.strip() else ""
        found[test.id()] = last.split(":", 1)[0]
    return found


def case_negative_control(run):
    revision, source = baseline_source()  # git runs before process creation is refused
    setup = prepare(run, deny_process=False)
    if revision is None:
        return False, {"setup": setup, "stop": "preserved_baseline_unavailable"}
    ocr = redirect_bound_defaults()
    baseline = types.ModuleType("fa49_preserved_baseline_ocr")
    baseline.__file__ = f"<git {revision}:{OCR_SOURCE}>"
    exec(compile(source, baseline.__file__, "exec"), baseline.__dict__)
    from unittest import mock
    STATE["deny_process"] = True
    setup["process_creation_refused_during_tests"] = True
    stream = io.StringIO()
    stream.write("=== broken control: ocr._confidence -> complete preserved baseline module ===\n")
    with mock.patch.object(ocr, "_confidence", baseline._confidence):
        broken, broken_result = run_names(NEG_BROKEN, stream)
        stream.write("=== broken control: normal two-row positive stays valid ===\n")
        broken_positive, _ = run_names([NEG_POSITIVE], stream)
        patched = ocr._confidence is baseline._confidence
    stream.write("=== candidate module: same selection ===\n")
    candidate, _ = run_names([*NEG_BROKEN, NEG_POSITIVE], stream)
    STATE["deny_process"] = False
    kinds = error_types(broken_result)
    detected = (broken["tests_run"] == len(NEG_BROKEN) and broken["errors"] == len(NEG_BROKEN)
                and broken["failures"] == 0 and sorted(kinds) == sorted(NEG_BROKEN)
                and set(kinds.values()) == {"AttributeError"})
    restored = ocr._confidence is not baseline._confidence and ocr._confidence.__module__ == OCR_MODULE
    paths = module_paths([REGRESSION])
    ok = (patched and detected and restored and clean(broken_positive) and clean(candidate)
          and candidate["tests_run"] == len(NEG_BROKEN) + 1 and guards_clear())
    return ok, {"setup": setup, "baseline_revision": revision,
                "baseline_source_sha256": sha256_bytes(source),
                "candidate_source_sha256": sha256_bytes((REPO / OCR_SOURCE).read_bytes()),
                "broken_selection": list(NEG_BROKEN), "positive_selection": NEG_POSITIVE,
                "broken_control": broken, "broken_error_types": kinds, "broken_detected": detected,
                "broken_positive": broken_positive, "candidate": candidate,
                "candidate_restored": restored, "module_paths": paths, **write_raw(run, stream)}


def portability_scan():
    hits = []
    for relative in PORTABILITY_FILES:
        text = (REPO / relative).read_text()
        for number, line in enumerate(text.splitlines(), 1):
            for needle in PORTABILITY_NEEDLES:
                if needle in line:
                    hits.append({"file": relative, "line": number, "needle": needle})
    return hits


def guard_self_test(run):
    """Inert decision checks: synthetic paths and state, no access is attempted.

    A synthetic home holds the synthetic run root and an outside sibling; real
    descriptors are opened only on synthetic directories inside this run."""
    base = run / "guard-self-test"
    srun = base / "run"
    deep = srun / "tmp" / "a" / "b"
    deep.mkdir(parents=True, mode=0o700)
    (srun / "cwd").mkdir(mode=0o700)
    (base / "outside").mkdir(mode=0o700)
    state = {"run": os.path.realpath(srun), "home": os.path.realpath(base),
             "allowed_reads": (os.path.realpath(srun),)}
    cwd = os.path.realpath(srun / "cwd")
    outside_abs = os.path.join(os.path.realpath(base), "outside", "ordinary.txt")
    rel_escape = os.path.join("..", "..", "outside", "ordinary.txt")
    checks = {}

    def write(name, dir_fd=None):
        return decide_path("open", name, dir_fd, True, state, cwd)

    def read(name, dir_fd=None):
        return decide_path("open", name, dir_fd, False, state, cwd)

    checks["relative_write_in_run_allowed"] = write("ordinary.txt") is None
    checks["relative_read_in_run_allowed"] = read("ordinary.txt") is None
    checks["relative_ancestor_write_refused"] = write(rel_escape) == ("write", "open", rel_escape)
    checks["relative_ancestor_read_refused"] = read(rel_escape) == ("read", "open", rel_escape)
    checks["ancestor_directory_walk_read_allowed"] = read(os.path.join("..", "..")) is None
    checks["ancestor_directory_write_refused"] = write(os.path.join("..", "..")) is not None
    checks["sibling_directory_of_ancestor_read_refused"] = read(os.path.join("..", "..", "outside")) is not None
    checks["absolute_outside_write_refused"] = write(outside_abs) == ("write", "open", outside_abs)
    checks["absolute_outside_read_refused"] = read(outside_abs) == ("read", "open", outside_abs)
    checks["audit_open_relative_write_refused"] = decide(
        "open", (rel_escape, "w", os.O_WRONLY | os.O_CREAT), state, cwd) == ("write", "open", rel_escape)
    checks["audit_open_relative_read_refused"] = decide(
        "open", (rel_escape, "r", os.O_RDONLY), state, cwd) == ("read", "open", rel_escape)
    checks["audit_preattributed_name_only"] = (
        decide("open", ("x.txt", "w", os.O_WRONLY), state, cwd, preattributed="x.txt") is None
        and decide("open", (rel_escape, "w", os.O_WRONLY), state, cwd, preattributed="x.txt") is not None)
    deep_fd = REAL_OS_OPEN(str(deep), os.O_RDONLY | os.O_DIRECTORY)
    run_fd = REAL_OS_OPEN(str(srun), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # cwd attribution would refuse this in-run name and allow the escape below.
        in_run = os.path.join("..", "..", "dirfd-positive.txt")
        escape = os.path.join("..", "outside", "ordinary.txt")
        checks["dir_fd_write_in_run_allowed"] = write(in_run, deep_fd) is None
        checks["dir_fd_read_in_run_allowed"] = read(in_run, deep_fd) is None
        checks["dir_fd_write_escape_refused"] = write(escape, run_fd) == ("write", "open", escape)
        checks["dir_fd_read_escape_refused"] = read(escape, run_fd) == ("read", "open", escape)
        checks["dir_fd_mkdir_escape_refused"] = decide(
            "os.mkdir", (escape, 0o700, run_fd), state, cwd) == ("write", "os.mkdir", escape)
        checks["dir_fd_replace_destination_escape_refused"] = decide(
            "os.replace", ("a", escape, deep_fd, run_fd), state, cwd) == ("write", "os.replace", escape)
        checks["dir_fd_replace_in_run_allowed"] = decide(
            "os.replace", ("a", "b", deep_fd, deep_fd), state, cwd) is None
    finally:
        os.close(deep_fd)
        os.close(run_fd)
    closed = REAL_OS_OPEN(str(srun), os.O_RDONLY | os.O_DIRECTORY)
    os.close(closed)
    checks["dir_fd_unresolvable_write_refused"] = write("x.txt", closed) == ("write", "open", "x.txt")
    checks["dir_fd_unresolvable_read_refused"] = read("x.txt", closed) == ("read", "open", "x.txt")
    checks["dir_fd_negative_refused"] = write("x.txt", -5) == ("write", "open", "x.txt")
    checks["nofollow_dotdot_refused"] = decide(
        "os.rmdir", (str(srun / "tmp" / ".."), None), dict(state, run=os.path.realpath(srun / "tmp")),
        cwd) is not None

    ledger = GuardLedger()
    with ledger.expect("write", "open", "/synthetic/fa49-probe-exact"):
        outcome = ledger.record("write", "open", "/synthetic/fa49-probe-exact")
    checks["ledger_exact_probe_in_phase_is_probe"] = outcome == "probe" and ledger.clear()
    ledger = GuardLedger()
    ledger.record("write", "open", "/synthetic/fa49-probe-unannounced.txt")
    checks["ledger_same_prefix_out_of_phase_fatal"] = not ledger.clear() and not any(ledger.probes.values())
    ledger = GuardLedger()
    with ledger.expect("write", "open", "/synthetic/fa49-probe-exact"):
        ledger.record("write", "open", "/synthetic/fa49-probe-other")
    checks["ledger_same_prefix_other_target_fatal"] = not ledger.clear()
    ledger = GuardLedger()
    with ledger.expect("write", "open", "/synthetic/fa49-probe-exact"):
        ledger.record("read", "open", "/synthetic/fa49-probe-exact")
    checks["ledger_other_kind_fatal"] = not ledger.clear()
    ledger = GuardLedger()
    with ledger.expect("write", "open", "/synthetic/fa49-probe-exact"):
        ledger.record("write", "open", "/synthetic/fa49-probe-exact")
        ledger.record("write", "open", "/synthetic/fa49-probe-exact")
    checks["ledger_extra_refusal_in_phase_fatal"] = not ledger.clear() and len(ledger.probes["write"]) == 1
    ledger = GuardLedger()
    with ledger.expect("process", "bound_default", "dependency_status.module_probe"):
        pass
    checks["ledger_unfired_probe_fatal"] = not ledger.clear() and bool(ledger.unmet)
    ledger = GuardLedger()
    with ledger.expect("process", "bound_default", "dependency_status.module_probe"):
        ledger.record("process", "bound_default", "dependency_status.module_probe")
    ledger.record("process", "bound_default", "dependency_status.module_probe")
    checks["ledger_bound_default_after_phase_fatal"] = not ledger.clear()
    return checks


def refused_probe(kind, event, target, action):
    """Run action inside an exact expect() phase; True only when the guard refused it
    with a PermissionError and the ledger matched that exact refusal once."""
    refused = False
    with LEDGER.expect(kind, event, target) as slot:
        try:
            action()
        except PermissionError:
            refused = True
        except OSError:
            refused = False
    return refused and slot["hits"] == 1


def isolation_probes(run):
    import unittest
    probes = {}
    probes["bytecode_disabled"] = sys.dont_write_bytecode is True
    probes["tempdir_in_run"] = under(os.path.realpath(tempfile.gettempdir()), STATE["run"])
    probes["cwd_in_run"] = under(os.path.realpath(os.getcwd()), STATE["run"])
    probes["os_open_wrapped"] = os.open is guarded_os_open
    host = PROBE_PREFIX + "net.invalid"
    probes["network_refused"] = refused_probe("network", "socket.getaddrinfo", host,
                                              lambda: socket.getaddrinfo(host, 80))
    absolute = os.path.join(os.sep, PROBE_PREFIX + secrets.token_hex(6), "write")
    probes["write_outside_refused"] = refused_probe("write", "open", absolute,
                                                    lambda: open(absolute, "w").close())
    # cwd is <run>/cwd, so two levels up is the output parent, outside the run root.
    relative = os.path.join("..", "..", PROBE_PREFIX + secrets.token_hex(6), "write")
    probes["relative_ancestor_write_refused"] = refused_probe("write", "open", relative,
                                                              lambda: open(relative, "w").close())
    probes["relative_ancestor_read_refused"] = refused_probe("read", "open", relative,
                                                             lambda: open(relative, "rb").close())
    run_fd = os.open(str(run), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # From cwd this name would land inside the run root; from run_fd it is outside.
        escape = os.path.join("..", PROBE_PREFIX + secrets.token_hex(6), "write")
        probes["dir_fd_write_escape_refused"] = refused_probe(
            "write", "open", escape,
            lambda: os.close(os.open(escape, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=run_fd)))
        probes["dir_fd_read_escape_refused"] = refused_probe(
            "read", "open", escape, lambda: os.close(os.open(escape, os.O_RDONLY, dir_fd=run_fd)))
    finally:
        os.close(run_fd)
    closed = os.open(str(run), os.O_RDONLY | os.O_DIRECTORY)
    os.close(closed)
    name = PROBE_PREFIX + "unresolvable"
    probes["dir_fd_unresolvable_refused"] = refused_probe(
        "write", "open", name, lambda: os.close(os.open(name, os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=closed)))
    inside = run / "write-guard-positive.txt"
    inside.write_text("in-run write allowed\n")
    probes["write_inside_allowed"] = inside.is_file()
    with open("relative-guard-positive.txt", "w") as handle:
        handle.write("relative in-run write allowed\n")
    probes["relative_write_inside_allowed"] = (Path(os.getcwd()) / "relative-guard-positive.txt").is_file()
    deep = run / "dirfd-positive" / "a" / "b"
    deep.mkdir(parents=True, mode=0o700)
    deep_fd = os.open(str(deep), os.O_RDONLY | os.O_DIRECTORY)
    try:
        # From cwd this name would land outside the run root; from deep_fd it is inside.
        handle = os.open(os.path.join("..", "..", "dirfd-positive.txt"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=deep_fd)
        os.write(handle, b"dir_fd in-run write allowed\n")
        os.close(handle)
    finally:
        os.close(deep_fd)
    probes["dir_fd_write_inside_allowed"] = (run / "dirfd-positive" / "dirfd-positive.txt").is_file()
    live = os.path.join(STATE["home"], PROBE_PREFIX + secrets.token_hex(6))
    probes["live_read_refused"] = refused_probe("read", "open", live, lambda: open(live, "rb").close())
    probes["repo_read_allowed"] = bool((REPO / OCR_SOURCE).read_bytes())
    ocr = redirect_bound_defaults()
    runner = ocr.extract_image_only_pages.__kwdefaults__["runner"]
    defaults = ocr.dependency_status.__defaults__
    probes["bound_defaults_redirected"] = (getattr(runner, "fa49_sentinel", None) is not None
                                          and all(getattr(d, "fa49_sentinel", None) for d in defaults))
    fired = False
    with LEDGER.expect("process", "bound_default", "dependency_status.module_probe") as slot:
        try:
            ocr.dependency_status()
        except SentinelError:
            fired = True
    probes["bound_default_sentinel_fires"] = fired and slot["hits"] == 1
    STATE["deny_process"] = True
    absent = os.path.join(os.sep, PROBE_PREFIX + "process", "absent")
    probes["process_refused_during_tests"] = refused_probe(
        "process", "subprocess.Popen", absent, lambda: subprocess.run([absent], check=False))
    STATE["deny_process"] = False
    probes["ocr_module_path"] = getattr(ocr, "__file__", None)
    probes["ocr_module_inside_repo"] = under(os.path.realpath(ocr.__file__), str(REPO))
    selected = unittest.defaultTestLoader.loadTestsFromName(REGRESSION).countTestCases()
    return probes, selected


def complete_copy(run):
    destination = run / "source-copy" / REPO.name
    shutil.copytree(REPO, destination, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    mismatched = [rel for rel in HASHED
                  if sha256_bytes((REPO / rel).read_bytes()) != sha256_bytes((destination / rel).read_bytes())]
    original = sorted(str(p.relative_to(REPO)) for p in REPO.rglob("*")
                      if ".git" not in p.relative_to(REPO).parts and "__pycache__" not in p.parts
                      and not p.name.endswith(".pyc"))
    copied = sorted(str(p.relative_to(destination)) for p in destination.rglob("*"))
    return destination, mismatched, original == copied, len(copied)


def case_isolation(run):
    setup = prepare(run, deny_process=False)
    self_test = guard_self_test(run)
    probes, selected = isolation_probes(run)
    hits = portability_scan()
    copy, mismatched, same_listing, entries = complete_copy(run)
    child_parent = run / "copy-evidence"
    child_argv = [sys.executable, "-B", str(copy / "tools" / "accept_fa49_ocr_tsv.py"),
                  "--case", "behavior", "--output-parent", str(child_parent)]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    child = subprocess.run(child_argv, cwd=copy, capture_output=True, env=env, check=False, timeout=600)
    (run / "child-stdout.json").write_bytes(child.stdout)
    (run / "child-stderr.txt").write_bytes(child.stderr)
    try:
        record = json.loads(child.stdout)
    except ValueError:
        record = {}
    child_paths = record.get("module_paths", {})
    child_inside = bool(child_paths) and all(p and under(os.path.realpath(p), str(copy))
                                             for p in child_paths.values())
    child_counts = record.get("counts", {})
    child_ok = (child.returncode == 0 and record.get("result") == "PASS_REVIEW_REQUIRED"
                and record.get("identity", {}).get("repo") == str(copy)
                and bool(child_counts) and clean(child_counts)
                and child_counts.get("tests_run") == selected and child_inside)
    ok = (all(self_test.values()) and all(v for k, v in probes.items() if k != "ocr_module_path")
          and not hits and not mismatched and same_listing and child_ok and guards_clear())
    return ok, {"setup": setup, "guard_self_test": self_test, "probes": probes, "portability_hits": hits,
                "copy_root": str(copy), "copy_entries": entries, "copy_listing_matches": same_listing,
                "copy_hash_mismatches": mismatched, "focused_tests_selected": selected,
                "child": {"argv": child_argv, "cwd": str(copy), "exit": child.returncode,
                          "stdout": "child-stdout.json", "stdout_sha256": sha256_bytes(child.stdout),
                          "stderr": "child-stderr.txt", "stderr_sha256": sha256_bytes(child.stderr),
                          "result": record.get("result"), "counts": child_counts,
                          "module_paths": child_paths, "module_paths_inside_copy": child_inside,
                          "run_dir": record.get("run_dir")}}


def case_credentials(run):
    setup = prepare(run, deny_process=False)
    tool = Path(sys.executable).parent / "detect-secrets"
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        return False, {"setup": setup, "stop": "missing_dependency: detect-secrets not installed next to "
                       "the interpreter; no install or substitution"}
    scan_argv = [str(tool), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    details = {"setup": setup, "scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
               "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes()),
               "scan_stderr_sha256": sha256_bytes(scan.stderr)}
    if scan.returncode != 0:
        details["stop"] = "scan_failed"
        return False, details
    try:
        data = json.loads(report.read_bytes())
        files_with_hits = sorted(data["results"])
    except (ValueError, KeyError, TypeError):
        details["stop"] = "scan_report_malformed"
        return False, details
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    raw = check.stdout + check.stderr
    (run / "check-secret-report.txt").write_bytes(raw)
    details.update({"detect_secrets_version": data.get("version"), "files_with_hits": files_with_hits,
                    "check_argv": check_argv, "check_cwd": str(REPO), "check_exit": check.returncode,
                    "check_output_tail": raw.decode(errors="replace").strip().splitlines()[-1:],
                    "check_output": "check-secret-report.txt", "check_output_sha256": sha256_bytes(raw)})
    return check.returncode == 0 and guards_clear(), details


CASES = ("behavior", "compatibility", "negative-control", "isolation", "credentials")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA49 OCR TSV rows.")
    parser.add_argument("--case", required=True, choices=CASES)
    parser.add_argument("--output-parent", required=True)
    args = parser.parse_args(argv)
    invoked_cwd = os.getcwd()
    run = fresh_run_dir(safe_output_parent(args.output_parent), args.case)
    before = identity()
    try:
        if args.case == "behavior":
            ok, details = case_tests(run, [REGRESSION], focused=True)
        elif args.case == "compatibility":
            ok, details = case_compatibility(run)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        elif args.case == "isolation":
            ok, details = case_isolation(run)
        else:
            ok, details = case_credentials(run)
    except Exception as exc:  # any unknown failure fails closed
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}: {exc}"}
    STATE["deny_process"] = False
    STATE["record_children"] = False
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    if not guards_clear():
        ok = False
        details.setdefault("stop", "guard_refusal_during_run")
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "invoked_cwd": invoked_cwd,
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "identity": before, "guard_refusals": LEDGER.refused, "guard_probes": LEDGER.probes,
              "guard_unmet_probes": LEDGER.unmet, "guard_counters": COUNTERS,
              "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
