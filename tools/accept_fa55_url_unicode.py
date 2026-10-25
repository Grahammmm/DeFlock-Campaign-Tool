"""Scoped offline acceptance runner for FA55 (strict UTF-8 portal URLs).

Each invocation runs exactly one --case and writes evidence to a fresh,
uniquely named directory below --output-parent (absolute, outside this
repository, no symlinked ancestor, not group/world writable):

  behavior          tests/records/test_fa55_url_unicode.py; the named A1/A2/A3
                    tests must each run and pass
  compatibility     the unchanged portal intake / legacy approval / export
                    bridge / collection pages test files
  negative-control  A4: with ONLY policy._utf8_encodable bypassed in memory the
                    surrogate regressions must fail (identity encode raises
                    UnicodeEncodeError) while the valid Unicode, signed-query
                    and preservation controls stay green; then the unpatched
                    candidate must pass the same tests
  isolation         A5: guard self-checks, redirected temp/home, no reachable
                    home-path data constants or bound defaults, no bytecode,
                    unsafe output parents refused, and the focused tests rerun
                    from a second complete source copy with module paths
                    recorded
  credentials       installed detect-secrets next to this interpreter, then the
                    unchanged tools/check_secret_report.py on that exact JSON

Before any application module is imported the runner disables bytecode
writes, points tempfile/TMPDIR/HOME/XDG at the per-run directory, removes model
endpoint variables and installs sentinels: sockets, urllib/http.client model
or network calls, an audit-hook write guard (writes, sqlite connects, renames
and removals only inside the per-run root) and a read guard (live-file
sentinel: reads only from the run root, the source root and the interpreter's
import roots; read-only directory opens are allowed, and because the "open"
audit event carries no dir_fd a relative read is judged against the cwd, so the
read sentinel is best effort). These are test guards, not a hostile-code
sandbox. A pass is REVIEW REQUIRED; it is not full-suite CI or semantic
acceptance.
"""
import argparse
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile

REPO = Path(__file__).resolve().parents[1]
REGRESSION = "tests.records.test_fa55_url_unicode"
COMPATIBILITY = ("tests.records.test_portal_intake", "tests.records.test_portal_legacy_approval",
                 "tests.records.test_export_bridge", "tests.test_records_collection_pages")
HASHED = (
    "campaign_tool/records/portal/policy.py",
    "campaign_tool/records/portal/collections.py",
    "campaign_tool/records/portal/store.py",
    "tests/records/test_fa55_url_unicode.py",
    "tools/accept_fa55_url_unicode.py",
    "tests/records/test_portal_intake.py",
    "tests/records/test_portal_legacy_approval.py",
    "tests/records/test_export_bridge.py",
    "tests/test_records_collection_pages.py",
    "tools/check_secret_report.py",
    "tools/check_public_tree.py",
)
NAMED = {
    "A1": ("A1PositiveTests.test_a1_valid_unicode_percent_and_signed_urls_preserved_exactly",
           "A1PositiveTests.test_a1_signed_query_spelling_and_order_unchanged",
           "A1PositiveTests.test_a1_json_surrogate_pair_escape_decodes_to_astral_and_is_accepted"),
    "A2": ("A2MalformedTests.test_a2_lone_surrogates_refused_at_url_parts",
           "A2MalformedTests.test_a2_lone_surrogates_refused_by_normalized",
           "A2MalformedTests.test_a2_lone_surrogate_json_escape_refused",
           "A2MalformedTests.test_a2_surrogate_in_host_is_invalid_url",
           "A2MalformedTests.test_a2_refusal_precedes_url_splitting_and_identity_hashing",
           "A2PreservationTests.test_a2_existing_negatives_keep_their_codes",
           "A2PreservationTests.test_a2_ascii_baseline_and_port_443_still_accepted"),
    "A3": ("A3InventoryTests.test_a3_document_member_with_lone_surrogate_refuses_whole_page",
           "A3InventoryTests.test_a3_collection_member_with_lone_surrogate_refuses_whole_page",
           "A3InventoryTests.test_a3_cursor_or_next_cursor_with_lone_surrogate_refuses_page"),
}
# Under the A4 bypass these must all go red; the controls must stay green.
DECIDING = ("A2MalformedTests", "A3InventoryTests")
CONTROLS = ("A1PositiveTests", "A2PreservationTests")

GUARD_HITS = []   # unexpected network/model calls
WRITE_HITS = []   # unexpected writes outside the per-run root
READ_HITS = []    # unexpected reads outside admitted roots
PROBE_HITS = []   # refusals of the runner's own inert probes (expected)
PROBE_PREFIX = "fa55-probe-"


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def git(*args):
    done = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else f"unavailable:{done.returncode}"


def tree_digest(root=REPO):
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
        for name in sorted(files):
            path = Path(directory) / name
            digest.update(str(path.relative_to(root)).encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def pycache_dirs(root=REPO):
    found = []
    for directory, dirs, _files in os.walk(root):
        if ".git" in dirs:
            dirs.remove(".git")
        found.extend(str(Path(directory, d).relative_to(root)) for d in dirs if d == "__pycache__")
    return sorted(found)


def identity():
    hashes = {}
    for relative in HASHED:
        path = REPO / relative
        hashes[relative] = sha256_bytes(path.read_bytes()) if path.is_file() else None
    return {"head": git("rev-parse", "HEAD"), "status_porcelain": git("status", "--porcelain"),
            "source_root": str(REPO), "source_sha256": hashes, "tree_sha256": tree_digest()}


def unsafe_output_parent(parent):
    """Return a refusal code for an unsafe evidence parent, else None."""
    text = os.fspath(parent)
    if not os.path.isabs(text):
        return "output_parent_must_be_absolute"
    if ".." in Path(text).parts:
        return "output_parent_dotdot"
    path = Path(os.path.normpath(text))
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            return "output_parent_symlinked_ancestor"
    if path == REPO or REPO in path.parents:
        return "output_parent_inside_repository"
    real = Path(os.path.realpath(path))
    if real == REPO or REPO in real.parents:
        return "output_parent_inside_repository"
    if path.exists():
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            return "output_parent_not_directory"
        if info.st_mode & 0o022:
            return "output_parent_group_or_world_writable"
        if info.st_uid != os.geteuid():
            return "output_parent_not_owned"
    return None


def fresh_run_dir(parent, case):
    refusal = unsafe_output_parent(parent)
    if refusal:
        raise SystemExit(refusal)
    parent = Path(os.path.normpath(parent))
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    refusal = unsafe_output_parent(parent)
    if refusal:
        raise SystemExit(refusal)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run = parent / f"{case}-{stamp}-{os.getpid()}-{secrets.token_hex(4)}"
    run.mkdir(mode=0o700, exist_ok=False)
    return run


def install_network_sentinels():
    def refuse(name):
        def guard(*args, **kwargs):
            GUARD_HITS.append(name)
            raise OSError(f"offline acceptance guard refused {name}")
        return guard
    socket.socket.connect = refuse("socket.connect")
    socket.socket.connect_ex = refuse("socket.connect_ex")
    socket.create_connection = refuse("socket.create_connection")
    socket.getaddrinfo = refuse("socket.getaddrinfo")
    import http.client
    import urllib.request
    http.client.HTTPConnection.connect = refuse("http.client.connect")
    urllib.request.urlopen = refuse("urllib.request.urlopen")


def redirect_environment(run):
    home = run / "home"
    tmp = run / "tmp"
    for path in (home, tmp):
        path.mkdir(mode=0o700, exist_ok=True)
    os.environ["HOME"] = str(home)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        os.environ[name] = str(home / name.lower())
    for name in ("TMPDIR", "TEMP", "TMP"):
        os.environ[name] = str(tmp)
    removed = []
    for name in list(os.environ):
        if name.endswith("MODEL_BASE_URL") or name.lower() in ("http_proxy", "https_proxy", "all_proxy"):
            removed.append(name)
            del os.environ[name]
    tempfile.tempdir = str(tmp)
    return {"home": str(home), "tmp": str(tmp), "removed_env": sorted(removed)}


WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
WRITE_EVENTS = {"os.mkdir": ((0, 2),), "os.rename": ((0, 2), (1, 3)), "os.replace": ((0, 2), (1, 3)),
                "os.remove": ((0, 1),), "os.rmdir": ((0, 1),), "os.truncate": ((0, None),),
                "os.symlink": ((1, 3),), "os.link": ((1, 3),), "shutil.rmtree": ((0, 1),),
                "shutil.copyfile": ((1, None),), "sqlite3.connect": ((0, None),),
                "os.chmod": ((0, 2),)}


def install_file_guards(run, source_root):
    """Audit-hook sentinels: writes only in the run root; reads only in admitted roots."""
    root = os.path.realpath(run)
    read_roots = {root, os.path.realpath(source_root)}
    for entry in [*sys.path, sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix]:
        if entry and os.path.isdir(entry):
            read_roots.add(os.path.realpath(entry))
    read_roots.update({"/dev", "/proc/self", "/etc/localtime", "/usr/share/zoneinfo"})
    read_roots.discard("/")

    def resolve(target, dir_fd=None):
        text = os.fsdecode(target)
        if text.startswith("file:"):
            text = text[5:].split("?", 1)[0]
        if isinstance(dir_fd, int) and dir_fd >= 0 and not os.path.isabs(text):
            try:
                text = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), text)
            except OSError:
                return None
        return os.path.realpath(text)

    def under(resolved, roots):
        return resolved is not None and any(resolved == r or resolved.startswith(r + os.sep) for r in roots)

    def refuse(kind, event, target):
        text = os.fsdecode(target)
        probe = os.path.basename(os.path.dirname(text)).startswith(PROBE_PREFIX)
        (PROBE_HITS if probe else (WRITE_HITS if kind == "write" else READ_HITS)).append(f"{event}:{text}")
        raise PermissionError(f"acceptance {kind} guard refused {event} outside admitted roots")

    def hook(event, args):
        if event == "open":
            path, mode, flags = (tuple(args) + (None, None, None))[:3]
            if path is None or isinstance(path, int):
                return
            if os.fsdecode(path) in ("", ":memory:", os.devnull):
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & WRITE_FLAGS)
            # Read-only directory opens (ancestor walks such as folder.secure_open)
            # expose no file content and are allowed. The "open" audit event does
            # not carry dir_fd, so a relative name is judged against the cwd: a
            # best-effort read sentinel, fail-closed for writes.
            if not writing and isinstance(flags, int) and flags & os.O_DIRECTORY:
                return
            resolved = resolve(path)
            if writing and not under(resolved, {root}):
                refuse("write", event, path)
            if not writing and not under(resolved, read_roots):
                refuse("read", event, path)
        elif event in WRITE_EVENTS:
            for p, d in WRITE_EVENTS[event]:
                if p >= len(args) or args[p] is None or isinstance(args[p], int):
                    continue
                if os.fsdecode(args[p]) in ("", ":memory:"):
                    continue
                dir_fd = args[d] if d is not None and d < len(args) else None
                if not under(resolve(args[p], dir_fd), {root}):
                    refuse("write", event, args[p])
    sys.addaudithook(hook)
    return {"write_root": root, "read_roots": sorted(read_roots)}


def guard_self_check(run):
    """Inert probes under a nonexistent top-level directory; refusals are expected."""
    probe_dir = os.path.join(os.sep, PROBE_PREFIX + secrets.token_hex(6))
    results = {}
    for name, action in (("write", lambda: open(os.path.join(probe_dir, "w"), "w").close()),
                         ("read", lambda: open(os.path.join(probe_dir, "r"), "rb").close())):
        try:
            action()
            results[name + "_refused"] = False
        except PermissionError:
            results[name + "_refused"] = True
        except OSError:
            results[name + "_refused"] = False
    for name, action in (("socket", lambda: socket.create_connection(("192.0.2.1", 9), timeout=0.01)),
                         ("urlopen", lambda: __import__("urllib.request").request.urlopen("https://192.0.2.1/"))):
        before = len(GUARD_HITS)
        try:
            action()
            results[name + "_refused"] = False
        except OSError:
            results[name + "_refused"] = len(GUARD_HITS) == before + 1
        del GUARD_HITS[before:]
    positive = run / "guard-positive.txt"
    positive.write_text("in-root write allowed\n")
    results["in_root_positive"] = positive.read_text() == "in-root write allowed\n"
    results["probe_refusals"] = len(PROBE_HITS)
    results["ok"] = all(v for k, v in results.items() if k != "probe_refusals") and len(PROBE_HITS) == 2 \
        and not WRITE_HITS and not READ_HITS
    return results


def prepare(run, source_root=REPO):
    """Everything that must happen before any application import."""
    sys.dont_write_bytecode = True
    env = redirect_environment(run)
    install_network_sentinels()
    files = install_file_guards(run, source_root)
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    check = guard_self_check(run)
    return {"environment": env, "file_guards": files, "self_check": check,
            "dont_write_bytecode": sys.dont_write_bytecode}


class Recorder:
    """Factory for a TextTestResult that records a per-test outcome."""

    @staticmethod
    def make():
        import unittest

        class Result(unittest.TextTestResult):
            RANK = {"pass": 0, "skip": 1, "fail": 2, "error": 3}

            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.outcomes, self.error_types = {}, {}

            def _mark(self, test, kind, err=None):
                case = getattr(test, "test_case", test)
                tid = case.id()
                if self.RANK[kind] >= self.RANK.get(self.outcomes.get(tid), -1):
                    self.outcomes[tid] = kind
                if err is not None:
                    self.error_types.setdefault(tid, set()).add(err[0].__name__)

            def addSuccess(self, test):
                super().addSuccess(test)
                self._mark(test, "pass")

            def addFailure(self, test, err):
                super().addFailure(test, err)
                self._mark(test, "fail", err)

            def addError(self, test, err):
                super().addError(test, err)
                self._mark(test, "error", err)

            def addSkip(self, test, reason):
                super().addSkip(test, reason)
                self._mark(test, "skip")

            def addSubTest(self, test, subtest, err):
                super().addSubTest(test, subtest, err)
                if err is not None:
                    self._mark(test, "fail" if issubclass(err[0], test.failureException) else "error", err)
        return Result


def run_suite(names, stream):
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromNames(list(names))
    runner = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=Recorder.make())
    result = runner.run(suite)
    c = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
         "skipped": len(result.skipped), "expected_failures": len(result.expectedFailures),
         "unexpected_successes": len(result.unexpectedSuccesses)}
    outcomes = {tid: kind for tid, kind in sorted(result.outcomes.items())}
    types = {tid: sorted(v) for tid, v in sorted(result.error_types.items())}
    return c, outcomes, types


def clean(c):
    return (c["tests_run"] > 0 and c["failures"] == 0 and c["errors"] == 0 and c["skipped"] == 0
            and c["expected_failures"] == 0 and c["unexpected_successes"] == 0)


def write_raw(run, name, stream):
    raw = stream.getvalue().encode("utf-8", "backslashreplace")
    (run / name).write_bytes(raw)
    return {"raw_output": name, "raw_output_sha256": sha256_bytes(raw)}


def module_paths():
    names = ("campaign_tool.records.portal.policy", "campaign_tool.records.portal.collections",
             "campaign_tool.records.portal.store", REGRESSION)
    return {name: getattr(sys.modules.get(name), "__file__", None) for name in names}


def named_check(outcomes):
    mapping = {}
    for label, tests in NAMED.items():
        mapping[label] = {t: outcomes.get(f"{REGRESSION}.{t}", "not_run") for t in tests}
    ok = all(v == "pass" for tests in mapping.values() for v in tests.values())
    return ok, mapping


def case_behavior(run):
    stream = io.StringIO()
    c, outcomes, types = run_suite([REGRESSION], stream)
    named_ok, mapping = named_check(outcomes)
    ok = clean(c) and named_ok and not GUARD_HITS
    return ok, {"modules": [REGRESSION], "counts": c, "named_assertions": mapping, "outcomes": outcomes,
                "error_types": types, "module_paths": module_paths(), **write_raw(run, "unittest.txt", stream)}


def case_compatibility(run):
    stream = io.StringIO()
    c, outcomes, types = run_suite(COMPATIBILITY, stream)
    ok = clean(c) and not GUARD_HITS and all(v == "pass" for v in outcomes.values())
    return ok, {"modules": list(COMPATIBILITY), "counts": c, "error_types": types,
                "module_paths": module_paths(), **write_raw(run, "unittest.txt", stream)}


def case_negative_control(run):
    regression = __import__(REGRESSION, fromlist=["MALFORMED"])
    from campaign_tool.records.portal import policy
    from campaign_tool.records.portal.policy import PortalError
    if not callable(getattr(policy, regression.GATE, None)):
        return False, {"stop": "gate_missing: policy." + regression.GATE}
    deciding = [f"{REGRESSION}.{name}" for name in DECIDING]
    controls = [f"{REGRESSION}.{name}" for name in CONTROLS]
    stream = io.StringIO()
    probe = {"bypassed": {}, "candidate": {}}
    stream.write("=== broken control: policy.%s bypassed (in memory only) ===\n" % regression.GATE)
    with regression.bypass_utf8_gate():
        broken, broken_outcomes, broken_types = run_suite(deciding, stream)
        stream.write("=== broken control: valid/preservation controls ===\n")
        broken_controls, control_outcomes, _ = run_suite(controls, stream)
        for name, url in regression.MALFORMED.items():
            try:
                regression.identity_encode(url)
                probe["bypassed"][name] = "encoded"
            except UnicodeEncodeError:
                probe["bypassed"][name] = "UnicodeEncodeError"
            except PortalError as exc:
                probe["bypassed"][name] = "PortalError:" + str(exc)
        valid_bypassed = {name: regression.identity_encode(url) == url.encode("utf-8")
                          for name, url in regression.VALID.items()}
    stream.write("=== candidate (unpatched): same deciding tests and controls ===\n")
    candidate, candidate_outcomes, candidate_types = run_suite(deciding + controls, stream)
    for name, url in regression.MALFORMED.items():
        try:
            regression.identity_encode(url)
            probe["candidate"][name] = "encoded"
        except UnicodeEncodeError:
            probe["candidate"][name] = "UnicodeEncodeError"
        except PortalError as exc:
            probe["candidate"][name] = "PortalError:" + str(exc)
    valid_candidate = {name: regression.identity_encode(url) == url.encode("utf-8")
                       for name, url in regression.VALID.items()}
    every_deciding_red = bool(broken_outcomes) and all(v in ("fail", "error") for v in broken_outcomes.values())
    a3_unicode = any("UnicodeEncodeError" in broken_types.get(tid, ())
                     for tid in broken_outcomes if ".A3InventoryTests." in tid)
    ok = (broken["tests_run"] > 0 and every_deciding_red and a3_unicode
          and set(probe["bypassed"].values()) == {"UnicodeEncodeError"} and all(valid_bypassed.values())
          and clean(broken_controls) and all(v == "pass" for v in control_outcomes.values())
          and clean(candidate) and all(v == "pass" for v in candidate_outcomes.values())
          and set(probe["candidate"].values()) == {"PortalError:invalid_url"} and all(valid_candidate.values())
          and not GUARD_HITS)
    return ok, {"gate": "campaign_tool.records.portal.policy." + regression.GATE,
                "broken_control": {"counts": broken, "outcomes": broken_outcomes, "error_types": broken_types,
                                   "every_deciding_test_red": every_deciding_red,
                                   "a3_unicode_encode_error_detected": a3_unicode},
                "broken_controls_green": {"counts": broken_controls, "outcomes": control_outcomes},
                "identity_encode_probe": probe,
                "valid_identity_encode": {"bypassed": valid_bypassed, "candidate": valid_candidate},
                "candidate": {"counts": candidate, "outcomes": candidate_outcomes, "error_types": candidate_types},
                "module_paths": module_paths(), **write_raw(run, "unittest.txt", stream)}


def reachable_home_bindings(original_home, allowed):
    """Module constants and bound function defaults that point into the real home."""
    import inspect
    hits = []

    def bad(value):
        if isinstance(value, (str, Path)):
            text = os.fspath(value)
            if text.startswith(original_home + os.sep) or text == original_home:
                return not any(text == a or text.startswith(a + os.sep) for a in allowed)
        return False

    for name, module in sorted(sys.modules.items()):
        if not (name == "campaign_tool" or name.startswith("campaign_tool.")) or module is None:
            continue
        for attr, value in sorted(vars(module).items()):
            if bad(value):
                hits.append(f"{name}.{attr}")
            functions = [value] if inspect.isfunction(value) else (
                [v for v in vars(value).values() if inspect.isfunction(v)] if inspect.isclass(value) else [])
            for function in functions:
                for default in (function.__defaults__ or ()) + tuple((function.__kwdefaults__ or {}).values()):
                    if bad(default):
                        hits.append(f"{name}.{attr}:{function.__name__}:default")
    return sorted(set(hits))


def committed_source_findings():
    needles = ("/" + "home" + "/", "agent" + "-work", "." + "hermes", "/" + "Users" + "/")
    findings = []
    for relative in ("tests/records/test_fa55_url_unicode.py", "tools/accept_fa55_url_unicode.py"):
        text = (REPO / relative).read_text()
        findings.extend(f"{relative}:{needle}" for needle in needles if needle in text)
    return findings


def case_isolation(run, original_home, pycache_before):
    import tempfile as tf
    stream = io.StringIO()
    c, outcomes, types = run_suite([REGRESSION], stream)
    named_ok, mapping = named_check(outcomes)
    primary_paths = module_paths()
    primary_inside = all(p and Path(p).resolve().is_relative_to(REPO) for p in primary_paths.values())
    temp_inside = Path(tf.gettempdir()).resolve().is_relative_to(run.resolve())
    home_bindings = reachable_home_bindings(original_home, [str(REPO), str(run)])
    # Unsafe evidence parents are refused before anything is written.
    link_target = run / "link-target"
    link_target.mkdir(mode=0o700)
    link = run / "linked-parent"
    os.symlink(link_target, link)
    wide = run / "wide-parent"
    wide.mkdir(mode=0o700)
    os.chmod(wide, 0o777)
    refusals = {"relative": unsafe_output_parent("evidence"),
                "dotdot": unsafe_output_parent(str(run) + "/x/../y"),
                "inside_repository": unsafe_output_parent(str(REPO / "evidence")),
                "symlinked_ancestor": unsafe_output_parent(str(link / "child")),
                "world_writable": unsafe_output_parent(str(wide)),
                "safe_positive": unsafe_output_parent(str(run / "safe-parent"))}
    refusal_ok = all(refusals[k] for k in refusals if k != "safe_positive") and refusals["safe_positive"] is None
    refused_writes = list(link_target.iterdir()) + list(wide.iterdir())
    os.chmod(wide, 0o700)
    # Second complete source copy; the child imports only from that copy.
    copy = run / "source-copy"
    shutil.copytree(REPO, copy, ignore=shutil.ignore_patterns(".git", "__pycache__"), symlinks=True)
    copy_tree = tree_digest(copy)
    child_argv = [sys.executable, str(copy / "tools" / "accept_fa55_url_unicode.py"), "--copy-child", str(run)]
    child = subprocess.run(child_argv, cwd=copy, capture_output=True, check=False)
    (run / "copy-child-stdout.txt").write_bytes(child.stdout)
    (run / "copy-child-stderr.txt").write_bytes(child.stderr)
    try:
        child_record = json.loads((run / "copy-child" / "copy-child.json").read_text())
        child_paths = child_record["module_paths"]
        child_counts = child_record["counts"]
        child_ok = (child.returncode == 0 and child_record["ok"] is True and clean(child_counts)
                    and all(p and Path(p).resolve().is_relative_to(copy) for p in child_paths.values())
                    and child_record["self_check"]["ok"] is True and not child_record["guard_hits"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        child_record, child_ok = {"stop": f"missing_or_malformed_child_evidence: {type(exc).__name__}"}, False
    pycache_after = pycache_dirs()
    findings = committed_source_findings()
    ok = (clean(c) and named_ok and primary_inside and temp_inside and not home_bindings and refusal_ok
          and not refused_writes and child_ok and copy_tree == tree_digest() and pycache_after == pycache_before
          and not list(copy.rglob("__pycache__")) and not findings and not GUARD_HITS)
    return ok, {"focused": {"counts": c, "named_assertions": mapping, "module_paths": primary_paths,
                            "module_paths_inside_source_root": primary_inside, "error_types": types},
                "tempdir": tf.gettempdir(), "tempdir_inside_run": temp_inside,
                "reachable_home_path_bindings": home_bindings,
                "output_parent_refusals": refusals, "output_parent_refusal_ok": refusal_ok,
                "pycache_before": pycache_before, "pycache_after": pycache_after,
                "committed_source_machine_path_findings": findings,
                "second_copy": {"root": str(copy), "tree_sha256": copy_tree,
                                "matches_source_tree": copy_tree == tree_digest(),
                                "argv": child_argv, "cwd": str(copy), "exit": child.returncode,
                                "stdout": "copy-child-stdout.txt", "stdout_sha256": sha256_bytes(child.stdout),
                                "stderr": "copy-child-stderr.txt", "stderr_sha256": sha256_bytes(child.stderr),
                                "record": child_record, "ok": child_ok},
                **write_raw(run, "unittest.txt", stream)}


def copy_child(run_dir):
    """Child entrypoint for the isolation case: focused tests from this source copy."""
    run = Path(run_dir)
    if unsafe_output_parent(run) or not (REPO.parent == run):
        print(json.dumps({"stop": "copy_child_unsafe_root"}))
        return 1
    work = run / "copy-child"
    work.mkdir(mode=0o700)
    setup = prepare(work, REPO)
    stream = io.StringIO()
    c, outcomes, types = run_suite([REGRESSION], stream)
    named_ok, mapping = named_check(outcomes)
    raw = write_raw(work, "unittest.txt", stream)
    record = {"source_root": str(REPO), "counts": c, "named_assertions": mapping, "error_types": types,
              "module_paths": module_paths(), "self_check": setup["self_check"],
              "guard_hits": list(GUARD_HITS), "refused_writes": list(WRITE_HITS), "refused_reads": list(READ_HITS),
              "raw_output": "copy-child/" + raw["raw_output"], "raw_output_sha256": raw["raw_output_sha256"]}
    record["ok"] = (clean(c) and named_ok and setup["self_check"]["ok"] and not GUARD_HITS
                    and not WRITE_HITS and not READ_HITS)
    (work / "copy-child.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ok": record["ok"], "counts": c}, sort_keys=True))
    return 0 if record["ok"] else 1


def case_credentials(run):
    tool = Path(sys.executable).parent / "detect-secrets"
    if not (tool.is_file() and os.access(tool, os.X_OK)):
        return False, {"stop": "missing_dependency: detect-secrets not installed next to the interpreter"}
    scan_argv = [str(tool), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"]
    report = run / "secret-scan.json"
    with open(report, "wb") as out:
        scan = subprocess.run(scan_argv, cwd=REPO, stdout=out, stderr=subprocess.PIPE, check=False)
    (run / "scan-stderr.txt").write_bytes(scan.stderr)
    details = {"scan_argv": scan_argv, "scan_cwd": str(REPO), "scan_exit": scan.returncode,
               "scan_stderr": "scan-stderr.txt", "scan_stderr_sha256": sha256_bytes(scan.stderr)}
    if scan.returncode != 0:
        return False, {**details, "stop": "scan_failed"}
    try:
        data = json.loads(report.read_bytes())
        files_with_hits = sorted(data["results"])
    except (ValueError, KeyError, TypeError):
        return False, {**details, "stop": "scan_report_malformed"}
    check_argv = [sys.executable, str(REPO / "tools" / "check_secret_report.py"), str(report)]
    check = subprocess.run(check_argv, cwd=REPO, capture_output=True, check=False)
    (run / "check-secret-report-stdout.txt").write_bytes(check.stdout)
    (run / "check-secret-report-stderr.txt").write_bytes(check.stderr)
    lines = check.stdout.decode(errors="replace").strip().splitlines()
    return check.returncode == 0, {
        **details, "scan_json": "secret-scan.json", "scan_json_sha256": sha256_bytes(report.read_bytes()),
        "detect_secrets_version": data.get("version"), "files_with_hits": files_with_hits,
        "check_argv": check_argv, "check_cwd": str(REPO), "check_exit": check.returncode,
        "check_summary": lines[-1:], "check_stdout_sha256": sha256_bytes(check.stdout),
        "check_stderr_sha256": sha256_bytes(check.stderr)}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scoped offline acceptance for FA55 strict UTF-8 URLs.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--case", choices=["behavior", "compatibility", "negative-control", "isolation", "credentials"])
    group.add_argument("--copy-child", help=argparse.SUPPRESS)
    parser.add_argument("--output-parent")
    args = parser.parse_args(argv)
    if args.copy_child:
        return copy_child(args.copy_child)
    if not args.output_parent:
        parser.error("--output-parent is required")
    original_home = os.path.realpath(os.path.expanduser("~"))
    pycache_before = pycache_dirs()
    run = fresh_run_dir(args.output_parent, args.case)
    before = identity()
    setup = prepare(run)
    try:
        if not setup["self_check"]["ok"]:
            ok, details = False, {"stop": "guard_self_check_failed"}
        elif args.case == "behavior":
            ok, details = case_behavior(run)
        elif args.case == "compatibility":
            ok, details = case_compatibility(run)
        elif args.case == "negative-control":
            ok, details = case_negative_control(run)
        elif args.case == "isolation":
            ok, details = case_isolation(run, original_home, pycache_before)
        else:
            ok, details = case_credentials(run)
    except Exception as exc:  # unknown failure stops acceptance
        ok, details = False, {"stop": f"unknown_failure: {type(exc).__name__}: {exc}"}
    after = identity()
    if after != before:
        ok = False
        details["stop"] = "candidate_identity_changed_during_run"
    if WRITE_HITS or READ_HITS or GUARD_HITS:
        ok = False
        details["stop"] = "guard_refusal_during_run"
    setup["refused_writes"] = list(WRITE_HITS)
    setup["refused_reads"] = list(READ_HITS)
    setup["network_or_model_hits"] = list(GUARD_HITS)
    record = {"case": args.case, "argv": [sys.executable, *sys.argv], "cwd": os.getcwd(),
              "interpreter": sys.executable, "python": sys.version.split()[0], "run_dir": str(run),
              "identity": before, "isolation_setup": setup,
              "result": "PASS_REVIEW_REQUIRED" if ok else "FAIL", "exit": 0 if ok else 1, **details}
    (run / "result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return record["exit"]


if __name__ == "__main__":
    raise SystemExit(main())
