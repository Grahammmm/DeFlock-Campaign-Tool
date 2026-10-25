"""FA50 acceptance runner: malformed OCR receipt status uses the existing typed refusal.

Exactly one --case per invocation. Every invocation creates a fresh, uniquely named child
directory of --output-parent (which must be outside this source tree and free of symlinks)
and records the actual child argv/cwd/exit, raw stdout/stderr with hashes, test counts and
the source identity before and after.

  behavior          tests.records.test_fa50_ocr_state A1/A2/A3 classes          (A1, A2, A3)
  compatibility     unchanged test_ocr, test_ocr_closed_cas, test_ocr_stage     (compatibility)
  negative-control  test_fa50_ocr_state.A4GateControl; passes only when the copy of the
                    staged module with ONLY the new guard removed raises TypeError for
                    list/object status while a valid blocked receipt still passes (A4)
  isolation         every FA50 class from this source tree AND from a fresh complete copy
                    under the invocation directory, with module paths recorded      (A5)
  credentials       detect-secrets beside this interpreter with the repository arguments,
                    then the unchanged tools/check_secret_report.py on that report  (A6)

Test cases run in an ordinary file-based child (this same file, --child-spec). Before any
application import the child disables bytecode writes, redirects tempfile/TMPDIR/HOME below
its own root and installs an audit-hook guard. In strict mode (all FA50 cases) the guard
refuses network, child processes, writes outside the child root and reads outside the source
tree, the interpreter and system directories; the OCR module's bound default runners are
then replaced by refusing sentinels, and each sentinel is self-tested once in a recorded
"sentinel-self-test" phase. Compatibility runs the unchanged suites with network and
out-of-root writes refused (they start their own interpreter children). Every child command
in compatibility is classified before launch: ordinary file- or module-based children run;
inline interpreter source (-c), interpreter/shell stdin and shell bodies are refused before
launch and recorded with the test that asked for them. A run whose only defects are such
refusals exits 2 (UNMET, pending the existing operator's disposition) - never 0, and never a
silent skip - and records a supplementary file-based limit probe as offered substitute
evidence. The guard is an offline guard for trusted synthetic tests, not a hostile-code sandbox.

A pass is REVIEW REQUIRED: scoped evidence, not full-repository CI, semantic, legal, human,
stage or release acceptance.
"""
import sys

sys.dont_write_bytecode = True

import argparse  # noqa: E402 - bytecode writes are disabled before any further import
import datetime  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
import re  # noqa: E402
import secrets  # noqa: E402
import shutil  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

SOURCE = Path(__file__).resolve().parents[1]
SELF = "tools/accept_fa50_ocr_state.py"
FA50 = "tests.records.test_fa50_ocr_state"
CLASSES = {"A1": FA50 + ".A1ProducerStatuses", "A2": FA50 + ".A2MalformedStatus",
           "A3": FA50 + ".A3PersistedReplay", "A4": FA50 + ".A4GateControl", "A5": FA50 + ".A5Portability"}
COMPATIBILITY = ("tests.records.test_ocr", "tests.records.test_ocr_closed_cas", "tests.records.test_ocr_stage")
SELECTORS = {
    "behavior": (CLASSES["A1"], CLASSES["A2"], CLASSES["A3"]),
    "compatibility": COMPATIBILITY,
    "negative-control": (CLASSES["A4"],),
    "isolation": tuple(CLASSES.values()),
}
CASES = ("behavior", "compatibility", "negative-control", "isolation", "credentials")
MODES = {"behavior": "strict", "negative-control": "strict", "isolation": "strict", "compatibility": "compat"}
# Named assertions each test case must execute (test method ids are checked, not just counts).
REQUIRED_TESTS = {
    "behavior": {
        CLASSES["A1"] + ".test_a1_status_vocabulary_is_unchanged",
        CLASSES["A1"] + ".test_a1_every_producer_status_validates_with_its_exact_artifact_set",
        CLASSES["A1"] + ".test_a1_valid_receipts_replay_from_cache_with_unchanged_hashes",
        CLASSES["A2"] + ".test_a2_malformed_status_is_refused_before_artifact_traversal",
        CLASSES["A2"] + ".test_a2_missing_status_stays_a_schema_mismatch",
        CLASSES["A2"] + ".test_a2_unrelated_schema_and_binding_refusals_keep_their_order",
        CLASSES["A3"] + ".test_a3_persisted_malformed_status_is_refused_by_every_consumer",
        CLASSES["A3"] + ".test_a3_valid_receipt_cache_reuse_still_works",
    },
    "negative-control": {CLASSES["A4"] + ".test_a4_disabled_guard_is_detected_and_valid_control_still_passes"},
}
REQUIRED_TESTS["isolation"] = REQUIRED_TESTS["behavior"] | REQUIRED_TESTS["negative-control"] | {
    CLASSES["A5"] + ".test_a5_authored_sources_contain_no_machine_paths",
    CLASSES["A5"] + ".test_a5_application_and_tests_come_from_the_same_source_tree",
    CLASSES["A5"] + ".test_a5_fixture_roots_stay_below_the_active_temporary_root",
    CLASSES["A5"] + ".test_a5_runner_refuses_the_pinned_inline_interpreter_child_shape",
    CLASSES["A5"] + ".test_a5_runner_permits_file_and_module_children",
}
EXPECTED_CONTROL = {("control", "list"): "TypeError", ("control", "object"): "TypeError",
                    ("control", "valid_blocked"): "passed", ("staged", "list"): "passed",
                    ("staged", "object"): "passed", ("staged", "valid_blocked"): "passed"}
EXPECTED_PROBES = ("network", "process", "write_outside", "read_outside", "list_outside", "bound_default")
AUTHORED = ("campaign_tool/records/extract/ocr.py", "tests/records/test_fa50_ocr_state.py", SELF)
REFERENCED = ("tests/records/test_ocr.py", "tests/records/test_ocr_closed_cas.py", "tests/records/test_ocr_stage.py",
              "tests/records/ocr_fixtures.py", "tests/records/ocr_stage_fixtures.py",
              "tools/check_secret_report.py", "tools/check_public_tree.py")
LABEL = "REVIEW REQUIRED - scoped offline acceptance, not full-repository CI or semantic acceptance"
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
SYSTEM_READ_ROOTS = ("/usr", "/lib", "/lib64", "/etc", "/proc", "/dev", "/sys")
DROPPED_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT", "PYTHONSAFEPATH")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def repo_modules():
    return sorted(n for n in sys.modules if n.split(".")[0] in ("campaign_tool", "tests"))


# ----------------------------------------------------------------------------- child commands

FORBIDDEN_CHILD = "forbidden_child_command"
UNMET_EXIT = 2  # compatibility only: forbidden child refused before launch; NOT a pass, awaits disposition
FORK = object()  # marker: a process event without an argv (fork); never a command by itself
INTERPRETER_NAME = re.compile(r"^(python|pypy)[0-9.]*[a-z]?$")
SHELL_NAMES = ("sh", "bash", "dash", "zsh", "ksh", "ash", "fish")
PYTHON_VALUE_FLAGS = "WX"  # short options whose value follows (attached or as the next argument)
PYTHON_VALUE_LONG = ("--check-hash-based-pycs",)


def process_argv(event, args):
    """The argv a process audit event is about to run (subprocess/exec/spawn), or FORK."""
    if event in ("os.fork", "os.forkpty"):
        return FORK
    if event in ("os.system", "os.startfile", "pty.spawn"):
        return os.fsdecode(args[0]) if args and isinstance(args[0], (str, bytes)) else repr(args)[:120]
    # subprocess.Popen: (executable, args, cwd, env); os.exec/os.posix_spawn/os.spawn: (path, argv, env)
    command = args[1] if len(args) > 1 else None
    if command is None:
        return repr(args)[:120]
    if isinstance(command, (str, bytes, os.PathLike)):
        return os.fsdecode(command)
    try:
        return [os.fsdecode(a) for a in command]
    except TypeError:
        return repr(command)[:120]


def child_command_violation(argv):
    """None for an ordinary file- or module-based child; otherwise why it is forbidden.

    Forbidden (frozen card contract: no inline interpreter source, no interpreter stdin or
    heredoc, no nested shell bodies): an interpreter given -c (alone or bundled, e.g. -Bc),
    an interpreter reading its program from stdin ('-' or no script), a shell given -c or
    reading stdin, and any unparsed shell-string command. Inspection only; never executes.
    """
    if not isinstance(argv, list):
        return "shell_string_command"
    if not argv:
        return "empty_argv"
    name = os.path.basename(argv[0])
    rest = list(argv[1:])
    if name == "env":
        while rest and (rest[0].startswith("-") or "=" in rest[0]):
            rest.pop(0)
        return child_command_violation(rest) if rest else "env_without_command"
    if name in SHELL_NAMES:
        for token in rest:
            if token == "--" or not token.startswith("-"):
                return None  # a script file
            if not token.startswith("--") and "c" in token[1:]:
                return "shell_inline_command"
        return "shell_reads_stdin"
    if not INTERPRETER_NAME.match(name):
        return None  # an ordinary tool or script file
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "-":
            return "interpreter_reads_stdin"
        if token == "--":
            return None if index + 1 < len(rest) else "interpreter_reads_stdin"
        if not token.startswith("-"):
            return None  # the script file
        if token.startswith("--"):
            index += 2 if token in PYTHON_VALUE_LONG else 1
            continue
        for position, flag in enumerate(token[1:], start=1):
            if flag == "c":
                return "interpreter_inline_source"
            if flag == "m":
                return None  # an importable module from the source tree, not inline source
            if flag in PYTHON_VALUE_FLAGS:
                if position == len(token) - 1:
                    index += 1  # the value is the next argument
                break
        index += 1
    return "interpreter_reads_stdin"


def limit_probe():
    """File-based child for the supplementary resource-limit evidence (no inline source)."""
    import resource
    sys.stdout.write("%d %d\n" % (resource.getrlimit(resource.RLIMIT_AS)[0],
                                  resource.getrlimit(resource.RLIMIT_FSIZE)[0]))
    return 0


# ----------------------------------------------------------------------------- child

class Guard:
    """Audit hook for trusted synthetic tests (offline guard, not a sandbox)."""
    NETWORK = ("socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
               "socket.bind", "socket.sendto", "socket.sendmsg", "urllib.Request", "http.client.connect",
               "ftplib.connect", "smtplib.connect", "imaplib.open", "poplib.connect", "telnetlib.Telnet.open")
    PROCESS = ("subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
               "os.forkpty", "os.startfile", "pty.spawn")
    # event -> ((path argument index, dir_fd argument index or None), ...)
    WRITES = {"os.mkdir": ((0, 2),), "os.remove": ((0, 1),), "os.rmdir": ((0, 1),),
              "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)), "os.chmod": ((0, 2),),
              "os.chown": ((0, 3),), "os.symlink": ((1, 2),), "os.link": ((0, 2), (1, 3)),
              "os.truncate": ((0, None),), "os.utime": ((0, 3),), "shutil.rmtree": ((0, 1),),
              "os.chflags": ((0, None),), "os.mkfifo": ((0, 2),), "os.mknod": ((0, 3),)}

    def __init__(self, mode, write_roots, read_roots):
        self.mode = mode
        self.write_roots = [Path(os.path.realpath(r)) for r in write_roots]
        self.read_roots = [Path(os.path.realpath(r)) for r in read_roots]
        self.active = False
        self.phase = "setup"
        self.refusals = []
        self.children = []
        self.current_test = None
        self.local = threading.local()

    @property
    def strict(self):
        return self.mode == "strict"

    def absolute(self, value, dir_fd=None, follow_leaf=True):
        text = os.fsdecode(value)
        if not os.path.isabs(text):
            base = os.readlink("/proc/self/fd/" + str(dir_fd)) if isinstance(dir_fd, int) and dir_fd >= 0 \
                else os.getcwd()
            text = os.path.join(base, text)
        if follow_leaf:
            return Path(os.path.realpath(text))
        # Unlink/rename/link/mkdir act on the entry itself, never on a symlink's target.
        head, leaf = os.path.split(os.path.normpath(text))
        return Path(os.path.realpath(head)) / leaf

    @staticmethod
    def under(path, roots):
        return any(path == root or path.is_relative_to(root) for root in roots)

    def readable(self, path):
        # Inside a read root, or a directory component on the way to one (fd-based traversal).
        return self.under(path, self.read_roots + self.write_roots) or any(
            root.is_relative_to(path) for root in self.read_roots + self.write_roots)

    def refuse(self, event, detail):
        self.refusals.append({"phase": self.phase, "test": self.current_test, "event": event,
                              "detail": str(detail)[:240]})
        raise PermissionError("FA50 offline guard refused " + event)

    LOOPBACK = ("127.0.0.1", "::1", "localhost")

    def loopback(self, event, args):
        if event in ("socket.bind", "socket.connect", "socket.sendto"):
            address = args[1] if len(args) > 1 else None
            host = address[0] if isinstance(address, tuple) and address else None
        elif event in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"):
            host = args[0] if args else None
        elif event == "urllib.Request":
            host = urlsplit(args[0]).hostname if args and isinstance(args[0], str) else None
        elif event == "http.client.connect":
            host = args[1] if len(args) > 1 else None
        else:
            return False
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        return host in self.LOOPBACK

    def check_open(self, event, value, flags_or_mode, dir_fd=None):
        if value is None or isinstance(value, int):
            return
        try:
            path = self.absolute(value, dir_fd)
        except (TypeError, ValueError, OSError):
            self.refuse(event, "unresolvable path")
        if isinstance(flags_or_mode, str):
            writing = any(c in flags_or_mode for c in "wax+")
        else:
            writing = bool(flags_or_mode & WRITE_FLAGS)
        if writing and not self.under(path, self.write_roots) and str(path) != os.devnull:
            self.refuse(event, path)
        if not writing and self.strict and not self.readable(path):
            self.refuse(event, path)

    def __call__(self, event, args):
        if not self.active:
            return
        if event in self.NETWORK:
            if not self.strict and self.loopback(event, args):
                return  # compatibility suites run their own loopback fakes
            self.refuse(event, "")
        elif event in self.PROCESS or event.startswith("os.exec") or event.startswith("os.spawn"):
            if self.strict:
                self.refuse(event, args[:1])
            argv = process_argv(event, args)
            reason = None if argv is FORK else child_command_violation(argv)  # an exec is checked itself
            shown = argv if isinstance(argv, list) else ["<fork>" if argv is FORK else argv]
            entry = {"phase": self.phase, "test": self.current_test, "event": event,
                     "argv_head": [str(a)[:120] for a in shown[:3]],
                     "verdict": reason or "permitted_file_or_module_child"}
            self.children.append(entry)
            if reason:
                self.refuse(FORBIDDEN_CHILD, {"test": self.current_test, "reason": reason,
                                              "argv_head": entry["argv_head"]})
        elif event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes)) and not os.path.isabs(os.fsdecode(path)) and \
                    getattr(self.local, "dir_fd", None) is not None:
                return  # relative to a descriptor: already checked by the os.open wrapper
            self.check_open(event, path, mode if isinstance(mode, str) else (flags or 0))
        elif event in self.WRITES:
            for index, fd_index in self.WRITES[event]:
                if index >= len(args) or args[index] is None or isinstance(args[index], int):
                    continue
                dir_fd = args[fd_index] if fd_index is not None and fd_index < len(args) else None
                path = self.absolute(args[index], dir_fd, follow_leaf=False)
                if event == "os.mkdir" and os.path.lexists(path):
                    continue  # an existing component: mkdir only fails with FileExistsError
                if not self.under(path, self.write_roots):
                    self.refuse(event, path)
        elif event in ("os.listdir", "os.scandir") and self.strict:
            target = args[0] if args else None
            if target is None or isinstance(target, int):
                return
            path = self.absolute(target)
            if not self.under(path, self.read_roots + self.write_roots):
                self.refuse(event, path)


def install_open_wrapper(guard):
    real_open = os.open

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        if guard.active and dir_fd is not None and not os.path.isabs(os.fsdecode(path)):
            guard.check_open("os.open", path, flags, dir_fd)
            guard.local.dir_fd = dir_fd
            try:
                return real_open(path, flags, mode, dir_fd=dir_fd)
            finally:
                guard.local.dir_fd = None
        return real_open(path, flags, mode, dir_fd=dir_fd)

    os.open = guarded_open


class Sentinel:
    def __init__(self, name):
        self.name = name

    def __call__(self, *args, **kwargs):
        raise PermissionError("FA50 sentinel refused bound default " + self.name)


def sentinel_probes(guard, ocr, probe_target):
    """Each probe must be refused before any effect; targets are absent, loopback or inert."""
    import socket
    guard.phase = "sentinel-self-test"
    outcomes = {}

    def attempt(name, action):
        before = len(guard.refusals)
        try:
            action()
            outcomes[name] = {"outcome": "not_refused"}
        except PermissionError as error:
            outcomes[name] = {"outcome": "PermissionError", "guard_refusals": len(guard.refusals) - before,
                              "message": str(error)[:120]}
        except Exception as error:  # an effect was attempted: never acceptable
            outcomes[name] = {"outcome": type(error).__name__}

    def network():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.connect(("127.0.0.1", 9))
    attempt("network", network)
    attempt("process", lambda: subprocess.Popen([str(probe_target)]))
    attempt("write_outside", lambda: open(probe_target, "xb"))
    attempt("read_outside", lambda: open(probe_target, "rb"))
    attempt("list_outside", lambda: os.listdir(probe_target.parent.parent))
    attempt("bound_default", lambda: ocr.dependency_status())
    outcomes["bound_default"]["guard_refusals"] = 0  # refused by the sentinel itself, not the hook
    guard.phase = "tests"
    return outcomes


def iter_tests(suite):
    import unittest
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_tests(item)
        else:
            yield item


def child_main(spec_path):
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    root = Path(spec["child_root"])
    mode = spec["mode"]
    facts = {"case": spec["case"], "mode": mode, "source_root": str(SOURCE), "executable": sys.executable,
             "python": sys.version, "cwd": os.getcwd(), "repo_modules_at_start": repo_modules()}
    tmp, home = root / "tmp", root / "home"
    tmp.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    tempfile.tempdir = str(tmp)
    for key in ("TMPDIR", "TEMP", "TMP"):
        os.environ[key] = str(tmp)
    os.environ["HOME"] = str(home)
    for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
        os.environ[key] = str(home / key.lower())
    read_roots = [SOURCE, root, sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix,
                  *SYSTEM_READ_ROOTS, *(p for p in sys.path if p and os.path.isdir(p))]
    guard = Guard(mode, [root], read_roots)
    probe_target = Path(spec["probe_target"])
    facts["probe_target_outside_roots"] = not guard.readable(Path(os.path.realpath(probe_target))) \
        and not guard.under(Path(os.path.realpath(probe_target)), guard.write_roots) \
        and not os.path.lexists(probe_target.parent)
    sys.addaudithook(guard)
    install_open_wrapper(guard)
    guard.active = True
    facts["guard_installed_before_app_import"] = repo_modules() == []
    while str(SOURCE) in sys.path:
        sys.path.remove(str(SOURCE))
    sys.path.insert(0, str(SOURCE))
    import unittest
    from campaign_tool.records.extract import ocr
    probes = {}
    if mode == "strict":
        ocr.extract_image_only_pages.__kwdefaults__["runner"] = Sentinel("extract_image_only_pages.runner")
        defaults = list(ocr.dependency_status.__defaults__)
        defaults[0], defaults[1] = Sentinel("dependency_status.which"), Sentinel("dependency_status.probe")
        ocr.dependency_status.__defaults__ = tuple(defaults)
        facts["bound_defaults"] = {
            "extract_image_only_pages.runner": type(ocr.extract_image_only_pages.__kwdefaults__["runner"]).__name__,
            "dependency_status.which": type(ocr.dependency_status.__defaults__[0]).__name__,
            "dependency_status.probe": type(ocr.dependency_status.__defaults__[1]).__name__}
        if facts["probe_target_outside_roots"]:
            probes = sentinel_probes(guard, ocr, probe_target)
    guard.phase = "tests"
    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(name) for name in spec["selectors"])
    selected = [test.id() for test in iter_tests(suite)]
    sys.stderr.write("selected %d tests from %s\n" % (len(selected), ", ".join(spec["selectors"])))

    class TrackingResult(unittest.TextTestResult):
        def startTest(self, test):
            guard.current_test = test.id()
            super().startTest(test)

        def stopTest(self, test):
            super().stopTest(test)
            guard.current_test = None

    result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=TrackingResult).run(suite)
    supplementary = None
    if mode == "compat":
        guard.phase = "supplementary-limit-probe"
        supplementary = supplementary_limit_probe(ocr)
    guard.active = False
    counts = {"tests_run": result.testsRun, "selected": selected,
              "failures": [t.id() for t, _ in result.failures], "errors": [t.id() for t, _ in result.errors],
              "skipped": [[t.id(), reason] for t, reason in result.skipped],
              "expected_failures": [t.id() for t, _ in result.expectedFailures],
              "unexpected_successes": [t.id() for t in result.unexpectedSuccesses]}
    loaded = {}
    for name in repo_modules():
        module_file = getattr(sys.modules[name], "__file__", None)
        loaded[name] = os.path.realpath(module_file) if module_file else None
    outside = sorted(n for n, f in loaded.items() if f is not None and not Path(f).is_relative_to(SOURCE))
    fa50 = sys.modules.get(FA50)
    facts.update({
        "counts": counts, "repo_modules_loaded": loaded, "modules_outside_source": outside,
        "ocr_module_file": os.path.realpath(ocr.__file__), "dont_write_bytecode": sys.dont_write_bytecode,
        "tempdir": tempfile.gettempdir(), "home": os.environ["HOME"], "sentinel_probes": probes,
        "guard_refusals": guard.refusals, "child_commands": guard.children,
        "supplementary_limit_probe": supplementary,
        "control_outcomes": list(getattr(fa50, "CONTROL_OUTCOMES", [])) if fa50 else [],
    })
    test_refusals = [r for r in guard.refusals if r["phase"] != "sentinel-self-test"]
    forbidden = [r for r in test_refusals if r["event"] == FORBIDDEN_CHILD]
    unmet = sorted({r["test"] for r in forbidden if r["test"]})
    probe_ok = mode != "strict" or (
        set(probes) == set(EXPECTED_PROBES)
        and all(p["outcome"] == "PermissionError" for p in probes.values())
        and all(p["guard_refusals"] == 1 for k, p in probes.items() if k != "bound_default")
        and len(guard.refusals) - len(test_refusals) == len(EXPECTED_PROBES) - 1)
    environment_ok = bool(
        result.testsRun > 0 and result.testsRun == len(selected) and not counts["skipped"]
        and not counts["expected_failures"] and not counts["unexpected_successes"] and not outside and probe_ok
        and facts["guard_installed_before_app_import"] and facts["probe_target_outside_roots"]
        and facts["dont_write_bytecode"] and Path(facts["tempdir"]).is_relative_to(root)
        and Path(facts["ocr_module_file"]).is_relative_to(SOURCE)
        and (supplementary is None or supplementary.get("matches") is True))
    facts["child_ok"] = bool(environment_ok and not counts["failures"] and not counts["errors"]
                             and not test_refusals)
    # Fail closed, distinctly: a run whose ONLY defects are guard-refused forbidden child commands
    # (each refusal attributed to a test, those tests exactly the erroring set) is UNMET, not a pass.
    facts["unmet_forbidden_child"] = {
        "tests": unmet, "commands": [{"test": r["test"], "detail": r["detail"]} for r in forbidden],
        "only_defect": bool(environment_ok and forbidden and len(forbidden) == len(test_refusals)
                            and all(r["test"] for r in forbidden) and not counts["failures"]
                            and sorted(counts["errors"]) == unmet)}
    if facts["child_ok"]:
        facts["status"] = "PASS"
    elif facts["unmet_forbidden_child"]["only_defect"]:
        facts["status"] = "UNMET_FORBIDDEN_CHILD_PENDING_OPERATOR_DISPOSITION"
    else:
        facts["status"] = "FAIL"
    Path(spec["result"]).write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"case": spec["case"], "child_ok": facts["child_ok"], "status": facts["status"],
                                 "tests_run": result.testsRun, "source_root": str(SOURCE)}, sort_keys=True) + "\n")
    return 0 if facts["child_ok"] else (UNMET_EXIT if facts["status"].startswith("UNMET") else 1)


def supplementary_limit_probe(ocr):
    """Offered substitute evidence for the pinned -c limit test: an ordinary file-based child
    (this file, --limit-probe) under the real ocr._limit_child. It does NOT replace or count as
    the pinned test; it is recorded for the operator's admission disposition."""
    argv = [sys.executable, "-B", str(SOURCE / SELF), "--limit-probe"]
    try:
        done = subprocess.run(argv, cwd=SOURCE, capture_output=True, text=True, check=False, timeout=120,
                              preexec_fn=ocr._limit_child)
    except Exception as error:  # recorded and failed closed
        return {"argv": argv, "error": type(error).__name__ + ": " + str(error)[:200], "matches": False}
    expected = [str(ocr.CHILD_MEMORY_BYTES), str(ocr.CHILD_FILE_BYTES)]
    return {"label": "supplementary substitute evidence; not the pinned test_ocr assertion",
            "argv": argv, "cwd": str(SOURCE), "exit": done.returncode, "stdout": done.stdout[:200],
            "stderr_sha256": sha256_bytes(done.stderr.encode()), "expected": expected,
            "matches": done.returncode == 0 and done.stdout.split() == expected}


# ---------------------------------------------------------------------------- parent

def git(root, *args):
    done = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if done.returncode != 0:
        raise RuntimeError("git " + args[0] + " failed")
    return done.stdout.decode()


def identity():
    status = git(SOURCE, "status", "--porcelain", "--untracked-files=all")
    return {"head": git(SOURCE, "rev-parse", "HEAD").strip(), "dirty_files": status.splitlines(),
            "clean_tree": status == "",
            "authored_sha256": {p: sha256_file(SOURCE / p) for p in AUTHORED if (SOURCE / p).is_file()},
            "referenced_sha256": {p: sha256_file(SOURCE / p) for p in REFERENCED}}


def safe_parent(raw):
    """Absolute, symlink-free, outside this source tree; missing components are created 0700."""
    parent = Path(os.path.abspath(raw))
    if parent == SOURCE or parent.is_relative_to(SOURCE) or SOURCE.is_relative_to(parent):
        raise ValueError("output parent must be outside (and not an ancestor of) the source tree")
    current = Path(parent.anchor)
    for part in parent.parts[1:]:
        if part in (".", ".."):
            raise ValueError("unsafe output parent component")
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current, 0o700)
            info = os.lstat(current)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("output parent has a linked or non-directory component: " + str(current))
    if Path(os.path.realpath(parent)) != parent:
        raise ValueError("output parent does not resolve to itself")
    return parent


def tree_hashes(parent, skip):
    found = {}
    for dirpath, dirnames, filenames in os.walk(parent):
        if Path(dirpath) == skip or Path(dirpath).is_relative_to(skip):
            dirnames[:] = []
            continue
        for name in filenames:
            path = Path(dirpath) / name
            found[str(path.relative_to(parent))] = "<symlink>" if path.is_symlink() else sha256_file(path)
    return found


def bytecode_files(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*.pyc"))


def run_child(case, source_root, run_dir, label, probe_target):
    child_root = run_dir / label
    child_root.mkdir(mode=0o700)
    spec = {"case": case, "mode": MODES[case], "selectors": list(SELECTORS[case]), "child_root": str(child_root),
            "result": str(child_root / "child-result.json"), "probe_target": str(probe_target)}
    spec_path = run_dir / (label + "-spec.json")
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    argv = [sys.executable, "-B", str(source_root / SELF), "--child-spec", str(spec_path)]
    env = {k: v for k, v in os.environ.items() if k not in DROPPED_ENV}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out_path, err_path = run_dir / (label + "-stdout.txt"), run_dir / (label + "-stderr.txt")
    with out_path.open("xb") as out, err_path.open("xb") as err:
        done = subprocess.run(argv, cwd=source_root, env=env, stdout=out, stderr=err, check=False, timeout=1500)
    record = {"label": label, "argv": argv, "cwd": str(source_root), "exit": done.returncode,
              "env_removed": [k for k in DROPPED_ENV if k in os.environ], "spec": str(spec_path),
              "spec_sha256": sha256_file(spec_path),
              "stdout": str(out_path), "stdout_sha256": sha256_file(out_path),
              "stderr": str(err_path), "stderr_sha256": sha256_file(err_path)}
    try:
        result = json.loads(Path(spec["result"]).read_text(encoding="utf-8"))
        if not isinstance(result, dict) or "counts" not in result or "child_ok" not in result:
            raise ValueError("malformed child result")
        record["result"] = result
        record["result_sha256"] = sha256_file(spec["result"])
    except (OSError, ValueError) as error:
        record["result_error"] = type(error).__name__ + ": " + str(error)[:200]
    return record


def summarize(record):
    counts = record.get("result", {}).get("counts", {})
    return {"label": record["label"], "exit": record["exit"], "tests_run": counts.get("tests_run"),
            "failures": len(counts.get("failures", [])), "errors": len(counts.get("errors", [])),
            "skipped": len(counts.get("skipped", [])), "child_ok": record.get("result", {}).get("child_ok"),
            "status": record.get("result", {}).get("status")}


def child_passed(case, record, source_root, allow_unmet=False):
    result = record.get("result")
    expected_exit, expected_status = (UNMET_EXIT, "UNMET_FORBIDDEN_CHILD_PENDING_OPERATOR_DISPOSITION") \
        if allow_unmet else (0, "PASS")
    if record["exit"] != expected_exit or not result or result.get("status") != expected_status:
        return False
    if not allow_unmet and not result.get("child_ok"):
        return False
    selected = set(result["counts"]["selected"])
    if case in REQUIRED_TESTS and not REQUIRED_TESTS[case] <= selected:
        return False
    if case == "compatibility":
        if any(not any(t.startswith(m + ".") for t in selected) for m in COMPATIBILITY):
            return False
    return result["source_root"] == str(source_root) and Path(result["ocr_module_file"]).is_relative_to(source_root)


def control_passed(record):
    outcomes = record.get("result", {}).get("control_outcomes", [])
    observed = {(o["module"], o["case"]): o["outcome"] for o in outcomes}
    return len(outcomes) == len(EXPECTED_CONTROL) and observed == EXPECTED_CONTROL


def copy_source(run_dir):
    listing = git(SOURCE, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    names = sorted(n for n in listing.split("\0") if n)
    copy_root = run_dir / "source-copy"
    copy_root.mkdir(mode=0o700)
    manifest = {}
    for name in names:
        source = SOURCE / name
        info = os.lstat(source)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("refusing to copy a non-regular source entry: " + name)
        target = copy_root / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(source, target, follow_symlinks=False)
        os.chmod(target, stat.S_IMODE(info.st_mode))
        manifest[name] = sha256_file(source)
    copied = {name: sha256_file(copy_root / name) for name in names}
    manifest_bytes = (json.dumps(manifest, sort_keys=True) + "\n").encode()
    (run_dir / "source-copy-manifest.json").write_bytes(manifest_bytes)
    return copy_root, {"files": len(names), "identical": copied == manifest,
                       "manifest": str(run_dir / "source-copy-manifest.json"),
                       "manifest_sha256": sha256_bytes(manifest_bytes)}


def run_credentials(run_dir, record, log):
    scanner = Path(sys.executable).parent / "detect-secrets"
    record["detect_secrets"] = str(scanner)
    if not (scanner.is_file() and os.access(scanner, os.X_OK)):
        record["stop_reason"] = "missing_dependency:detect-secrets beside the interpreter"
        log.append("detect-secrets is not installed beside this interpreter; nothing installed or substituted")
        return False
    report = run_dir / "secret-scan.json"
    scan_argv = [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    scan_err = run_dir / "detect-secrets-stderr.txt"
    with report.open("xb") as out, scan_err.open("xb") as err:
        scan = subprocess.run(scan_argv, cwd=SOURCE, stdout=out, stderr=err, check=False, timeout=1500)
    check_argv = [sys.executable, "-B", str(SOURCE / "tools/check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=SOURCE, capture_output=True, check=False, timeout=600)
    (run_dir / "check-secret-report-stdout.txt").write_bytes(check.stdout)
    (run_dir / "check-secret-report-stderr.txt").write_bytes(check.stderr)
    try:
        results = json.loads(report.read_bytes())["results"]
        raw_hits = sum(len(v) for v in results.values())
    except (ValueError, KeyError, TypeError, AttributeError):
        raw_hits = None
    match = re.search(rb"potential secrets: (\d+);", check.stdout)
    potential = int(match.group(1)) if match else None
    log.append("detect-secrets exit %d; raw hits %s; checker exit %d; checker potential secrets %s"
               % (scan.returncode, raw_hits, check.returncode, potential))
    record["scan"] = {"argv": scan_argv, "cwd": str(SOURCE), "exit": scan.returncode, "report": str(report),
                      "report_sha256": sha256_file(report), "stderr_sha256": sha256_file(scan_err),
                      "raw_hits": raw_hits}
    record["check"] = {"argv": check_argv, "cwd": str(SOURCE), "exit": check.returncode,
                       "stdout_sha256": sha256_bytes(check.stdout), "stderr_sha256": sha256_bytes(check.stderr),
                       "potential_secrets": potential}
    return scan.returncode == 0 and check.returncode == 0 and raw_hits is not None and potential == 0


def parent_main(case, output_parent):
    parent = safe_parent(output_parent)
    token = secrets.token_hex(4)
    run_dir = parent / "-".join((case, stamp(), str(os.getpid()), token))
    run_dir.mkdir(mode=0o700)
    prior = tree_hashes(parent, run_dir)
    start = identity()
    pyc_before = bytecode_files(SOURCE)
    probe_target = parent.parent / ("fa50-absent-" + token) / "probe"
    record = {"label": LABEL, "case": case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version, "run_dir": str(run_dir),
              "identity_before": start, "selectors": list(SELECTORS.get(case, ())), "children": []}
    log = []
    try:
        if case == "credentials":
            passed = run_credentials(run_dir, record, log)
        else:
            child = run_child(case, SOURCE, run_dir, "source", probe_target)
            record["children"].append(child)
            passed = child_passed(case, child, SOURCE)
            if case == "compatibility" and not passed and child_passed(case, child, SOURCE, allow_unmet=True):
                unmet = child["result"]["unmet_forbidden_child"]
                record["unmet_requirement"] = {
                    "status": child["result"]["status"], "tests": unmet["tests"], "commands": unmet["commands"],
                    "executed_and_passed": child["result"]["counts"]["tests_run"] - len(unmet["tests"]),
                    "supplementary_limit_probe": child["result"].get("supplementary_limit_probe"),
                    "owner": "existing operator (admission disposition); full discovery and exact-head CI "
                             "remain with their existing owners",
                    "note": "pinned test left unedited and unskipped; its forbidden child was refused before "
                            "launch, so this is NOT a full compatibility pass"}
                log.append("compatibility UNMET (exit %d): forbidden child refused before launch in %s"
                           % (UNMET_EXIT, ", ".join(unmet["tests"])))
            if case == "negative-control":
                passed = passed and control_passed(child)
            if case == "isolation":
                copy_root, copy_facts = copy_source(run_dir)
                copy_pyc_before = bytecode_files(copy_root)
                second = run_child(case, copy_root, run_dir, "copy", probe_target)
                record["children"].append(second)
                copy_facts["bytecode_before"] = copy_pyc_before
                copy_facts["bytecode_after"] = bytecode_files(copy_root)
                record["source_copy"] = copy_facts
                modules = second.get("result", {}).get("repo_modules_loaded", {})
                copy_facts["module_files_in_copy"] = bool(modules) and all(
                    f is None or Path(f).is_relative_to(copy_root) for f in modules.values())
                passed = (passed and child_passed(case, second, copy_root) and copy_facts["identical"]
                          and copy_facts["module_files_in_copy"] and copy_facts["bytecode_after"] == []
                          and control_passed(child) and control_passed(second))
            for child_record in record["children"]:
                log.append("child %s: %s" % (child_record["label"], json.dumps(summarize(child_record), sort_keys=True)))
    except Exception as error:  # recorded and failed closed, never an expected red
        log.append("unexpected runner fault: " + type(error).__name__ + ": " + str(error)[:300])
        record["runner_fault"] = type(error).__name__
        passed = False
    end = identity()
    record["identity_after"] = end
    record["bytecode_written_in_source"] = sorted(set(bytecode_files(SOURCE)) - set(pyc_before))
    after = tree_hashes(parent, run_dir)
    record["prior_evidence_files"] = len(prior)
    record["prior_evidence_unchanged"] = all(after.get(k) == v for k, v in prior.items())
    record["probe_target_absent_after"] = not os.path.lexists(probe_target.parent)
    passed = bool(passed and record["prior_evidence_unchanged"] and not record["bytecode_written_in_source"]
                  and record["probe_target_absent_after"] and start == end)
    unmet_ok = bool("unmet_requirement" in record and record["prior_evidence_unchanged"]
                    and not record["bytecode_written_in_source"] and record["probe_target_absent_after"]
                    and start == end and "runner_fault" not in record)
    record["exit"] = 0 if passed else (UNMET_EXIT if unmet_ok else 1)
    summary = {"case": case, "exit": record["exit"], "run_dir": str(run_dir), "head": end["head"],
               "clean_tree": end["clean_tree"], "children": [summarize(c) for c in record["children"]],
               "label": LABEL}
    if case == "compatibility":
        summary["compatibility_status"] = "PASS" if passed else (
            record["unmet_requirement"]["status"] if unmet_ok else "FAIL")
        if unmet_ok:
            summary["unmet_tests"] = record["unmet_requirement"]["tests"]
            summary["executed_and_passed"] = record["unmet_requirement"]["executed_and_passed"]
            summary["supplementary_limit_probe_matches"] = \
                (record["unmet_requirement"]["supplementary_limit_probe"] or {}).get("matches")
    if case == "credentials":
        summary["scan_exit"] = record.get("scan", {}).get("exit")
        summary["check_exit"] = record.get("check", {}).get("exit")
        summary["potential_secrets"] = record.get("check", {}).get("potential_secrets")
    log_bytes = ("\n".join(log) + "\n").encode()
    (run_dir / "runner-log.txt").write_bytes(log_bytes)
    record["runner_log_sha256"] = sha256_bytes(log_bytes)
    out = (json.dumps(summary, sort_keys=True) + "\n").encode()
    (run_dir / "summary.json").write_bytes(out)
    record["summary_sha256"] = sha256_bytes(out)
    (run_dir / "receipt.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    sys.stderr.write(log_bytes.decode())
    sys.stdout.write(out.decode())
    return record["exit"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=CASES)
    parser.add_argument("--output-parent")
    parser.add_argument("--child-spec", help=argparse.SUPPRESS)
    parser.add_argument("--limit-probe", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.limit_probe:
        if args.case or args.output_parent or args.child_spec:
            parser.error("--limit-probe is used alone")
        return limit_probe()
    if args.child_spec:
        if args.case or args.output_parent:
            parser.error("--child-spec is used alone")
        return child_main(args.child_spec)
    if not args.case or not args.output_parent:
        parser.error("--case and --output-parent are required")
    return parent_main(args.case, args.output_parent)


if __name__ == "__main__":
    raise SystemExit(main())
