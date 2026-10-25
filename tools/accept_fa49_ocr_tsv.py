"""Scoped offline acceptance runner for FA49 (truncated OCR confidence TSV rows).

One invocation runs exactly one case and writes its evidence to a fresh, unique
child directory below --output-parent, which must lie outside this repository
and must not pass through a symbolic link:

  behavior          tests/records/test_fa49_ocr_tsv.py (A1 positive/boundaries,
                    A2 malformed rows, A3 real extraction consumer and replay)
  compatibility     the unchanged OCR test files named in COMPATIBILITY
  negative-control  A4: the truncated-row and consumer regressions run against
                    the complete preserved baseline ocr.py (loaded in memory
                    from git history) must ERROR with AttributeError while the
                    normal two-row positive still passes; the same selection
                    then passes against the real candidate module
  isolation         A5: guard/redirect self-probes, committed-source
                    portability scan, then the behavior case rerun as a child
                    process from a second complete source copy
  credentials       A6: the detect-secrets installed next to this interpreter,
                    then the unchanged tools/check_secret_report.py on that
                    exact report

Before any repository module is imported this runner disables bytecode writes,
redirects tempfile/TMPDIR into the per-run directory, and installs an audit
hook that refuses non-loopback network lookups/connects (loopback connects are
allowed only to sockets bound by this process or to ports with no host
listener, so a host-local model service stays unreachable), absolute-path reads
under the user's home outside the repository/run/interpreter roots, and
Python-level writes outside the per-run directory. For the focused cases it
also refuses process creation while tests run and replaces the OCR module's
bound default runner/probe callables with refusing sentinels. The open() audit
event carries no dir_fd, so relative-name opens cannot be attributed: they are
counted, and a before/after content digest of the whole repository tree (minus
.git) is the backstop. Focused cases work from a cwd inside the run directory;
compatibility keeps the invocation cwd because unchanged production children
are started with ``python -m``. This is a test guard, not a hostile-code
sandbox. A pass is REVIEW REQUIRED; it is not full-repository CI, semantic,
human or release acceptance.
"""
import argparse
import datetime
import hashlib
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
import types
import weakref

REPO = Path(__file__).resolve().parents[1]
OCR_MODULE = "campaign_tool.records.extract.ocr"
OCR_SOURCE = "campaign_tool/records/extract/ocr.py"
REGRESSION = "tests.records.test_fa49_ocr_tsv"
COMPATIBILITY = ("tests.records.test_ocr", "tests.records.test_ocr_stage", "tests.records.test_run_pdf_ocr")
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

STATE = {"run": None, "allowed_reads": (), "home": None, "deny_process": False, "bound": []}
REFUSED = {"network": [], "write": [], "read": [], "process": []}
PROBES = {"network": [], "write": [], "read": [], "process": []}
UNATTRIBUTED = {"relative_write_opens": 0}

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


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def is_probe(text):
    return PROBE_PREFIX in text


def resolve(target, dir_fd=None, follow=True):
    """Absolute real path of target; the final component is kept when follow is False."""
    if isinstance(target, int):
        return None
    text = os.fsdecode(target)
    if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
        try:
            text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
        except OSError:
            return ""  # unresolvable descriptor: caller fails closed
    if follow:
        return os.path.realpath(text)
    absolute = os.path.abspath(text)
    return os.path.join(os.path.realpath(os.path.dirname(absolute)), os.path.basename(absolute))


def under(path, root):
    return path == root or path.startswith(root + os.sep)


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


# Events whose final path component is acted on without following a symlink.
NOFOLLOW_EVENTS = {"os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.symlink",
                   "os.link", "shutil.rmtree", "shutil.move"}


def audit(event, args):
    run = STATE["run"]
    if run is None:
        return
    if event in NETWORK_EVENTS:
        if network_allowed(event, args):
            return
        detail = f"{event}:{args!r}"
        (PROBES if is_probe(detail) else REFUSED)["network"].append(detail)
        raise PermissionError(f"FA49 acceptance guard refused {event}")
    if event in PROCESS_EVENTS and STATE["deny_process"]:
        detail = f"{event}:{args!r}"
        (PROBES if is_probe(detail) else REFUSED)["process"].append(detail)
        raise PermissionError(f"FA49 acceptance guard refused {event}")
    targets, writing = [], False
    if event == "open":
        path, mode, flags = (tuple(args) + (None, None, None))[:3]
        if path is None or isinstance(path, int):
            return
        writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and bool(flags & WRITE_FLAGS))
        if not os.path.isabs(os.fsdecode(path)):
            # The open event carries no dir_fd, so a relative name cannot be attributed.
            # Count it; the before/after repository tree digest is the backstop.
            if writing:
                UNATTRIBUTED["relative_write_opens"] += 1
            return
        targets = [(path, None)]
    elif event in WRITE_EVENTS:
        writing = True
        targets = [(args[p], args[d] if d is not None and d < len(args) else None)
                   for p, d in WRITE_EVENTS[event] if p < len(args) and args[p] is not None]
    else:
        return
    for target, dir_fd in targets:
        text = os.fsdecode(target) if not isinstance(target, int) else ""
        if event == "sqlite3.connect" and text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
            if text.startswith("///"):
                text = text[2:]
            target = text
        if text in ("", ":memory:", os.devnull) and not writing:
            continue
        if text in (":memory:", os.devnull):
            continue
        resolved = resolve(target, dir_fd, follow=event not in NOFOLLOW_EVENTS)
        if resolved is None:
            continue
        if writing:
            if resolved and under(resolved, run):
                continue
            if event == "os.mkdir" and resolved and os.path.lexists(resolved):
                continue  # existing ancestor: the syscall itself fails with EEXIST, nothing is written
            (PROBES if is_probe(text) else REFUSED)["write"].append(f"{event}:{text}")
            raise PermissionError(f"FA49 acceptance write guard refused {event} outside the run root")
        home = STATE["home"]
        if (os.path.isabs(text) and home and under(resolved, home)
                and not any(under(resolved, root) for root in STATE["allowed_reads"])):
            (PROBES if is_probe(text) else REFUSED)["read"].append(f"{event}:{text}")
            raise PermissionError("FA49 acceptance live-file sentinel refused a read outside admitted roots")


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


def prepare(run, *, deny_process, work_in_run=True):
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
    sys.addaudithook(audit)
    if work_in_run:
        os.chdir(work)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    STATE["deny_process"] = deny_process
    return {"tmp": str(tmp), "cwd": os.getcwd(), "allowed_read_roots": list(STATE["allowed_reads"]),
            "process_creation_refused_during_tests": deny_process}


class SentinelError(RuntimeError):
    pass


def refusing(name):
    def sentinel(*args, **kwargs):
        REFUSED["process"].append("bound_default:" + name)
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
    return not any(REFUSED.values())


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


def isolation_probes(run):
    import unittest
    probes = {}
    probes["bytecode_disabled"] = sys.dont_write_bytecode is True
    probes["tempdir_in_run"] = under(os.path.realpath(tempfile.gettempdir()), STATE["run"])
    probes["cwd_in_run"] = under(os.path.realpath(os.getcwd()), STATE["run"])
    try:
        socket.getaddrinfo(PROBE_PREFIX + "net.invalid", 80)
        probes["network_refused"] = False
    except PermissionError:
        probes["network_refused"] = len(PROBES["network"]) == 1
    except OSError:
        probes["network_refused"] = False
    escape = os.path.join(os.sep, PROBE_PREFIX + secrets.token_hex(6), "write")
    try:
        open(escape, "w").close()
        probes["write_outside_refused"] = False
    except PermissionError:
        probes["write_outside_refused"] = len(PROBES["write"]) == 1
    except OSError:
        probes["write_outside_refused"] = False
    inside = run / "write-guard-positive.txt"
    inside.write_text("in-run write allowed\n")
    probes["write_inside_allowed"] = inside.is_file()
    live = os.path.join(STATE["home"], PROBE_PREFIX + secrets.token_hex(6))
    try:
        open(live, "rb").close()
        probes["live_read_refused"] = False
    except PermissionError:
        probes["live_read_refused"] = len(PROBES["read"]) == 1
    except OSError:
        probes["live_read_refused"] = False
    probes["repo_read_allowed"] = bool((REPO / OCR_SOURCE).read_bytes())
    ocr = redirect_bound_defaults()
    runner = ocr.extract_image_only_pages.__kwdefaults__["runner"]
    defaults = ocr.dependency_status.__defaults__
    probes["bound_defaults_redirected"] = (getattr(runner, "fa49_sentinel", None) is not None
                                          and all(getattr(d, "fa49_sentinel", None) for d in defaults))
    before = len(REFUSED["process"])
    try:
        ocr.dependency_status()
        probes["bound_default_sentinel_fires"] = False
    except SentinelError:
        probes["bound_default_sentinel_fires"] = True
    REFUSED["process"][before:] = []  # the deliberate sentinel call is a probe, not a hit
    PROBES["process"].append("bound_default:dependency_status")
    STATE["deny_process"] = True
    try:
        subprocess.run([os.path.join(os.sep, PROBE_PREFIX + "process", "absent")], check=False)
        probes["process_refused_during_tests"] = False
    except PermissionError:
        probes["process_refused_during_tests"] = len([p for p in PROBES["process"] if "subprocess" in p]) == 1
    except OSError:
        probes["process_refused_during_tests"] = False
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
    ok = (all(v for k, v in probes.items() if k != "ocr_module_path") and not hits and not mismatched
          and same_listing and child_ok and guards_clear())
    return ok, {"setup": setup, "probes": probes, "portability_hits": hits,
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
            ok, details = case_tests(run, COMPATIBILITY, focused=False)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        elif args.case == "isolation":
            ok, details = case_isolation(run)
        else:
            ok, details = case_credentials(run)
    except Exception as exc:  # any unknown failure fails closed
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}: {exc}"}
    STATE["deny_process"] = False
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    if not guards_clear():
        ok = False
        details.setdefault("stop", "guard_refusal_during_run")
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "invoked_cwd": invoked_cwd,
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "identity": before, "guard_refusals": REFUSED, "guard_probes": PROBES,
              "guard_unattributed": UNATTRIBUTED,
              "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
