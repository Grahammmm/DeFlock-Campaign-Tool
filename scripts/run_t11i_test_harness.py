#!/usr/bin/env python3
"""Run the T12 harness scenarios against a sandbox copy of TEST (T11i, strict since T12s).

Host-side driver. It never touches the live records project or TEST itself:

1. Copies TEST (mail-history, processing-v1, portal-queue-v1 and the TEST
   adoption config) into a fresh sandbox directory, inside the Flock container
   as the ledger owner, and marks the copy with the sandbox marker that
   ``scripts/t12_fault_inject.py`` requires.
2. Renders the runtime templates with ``targets/test-container.json.in``:
   every stage is a ``docker exec`` of the stage adapter in the Flock
   container against the sandbox copy (``docker run`` is not available to
   agents here). The portal allowlist is only read; fetch is a dry run.
3. Runs ``run-records.sh --target TEST`` N >= 3 times in a row, then the fault
   runs: duplicate receipt, portal 404 and corrupt PDF (both injected through
   the engine's own adapters by ``t12_fault_inject.py`` and detected by the
   real ``run_stages`` adapters, not the stub backend), and docker down.
   Notify always points at a closed loopback port with a fake token
   (telegram-down on every run), so no message can leave the host.
4. Records a metadata-only manifest (file count, bytes, hash of
   path/size/mtime) of the live records project and of TEST before and after,
   and asserts both are unchanged.
5. Writes a numbers-and-codes-only receipt (no paths, URLs or record text) and
   evaluates it with ``evaluate`` (below). The receipt says what it is:
   ``target_mode`` is ``TEST_SANDBOX_EVIDENCE`` and ``live_acceptance`` is
   false. A passing receipt is TEST evidence, never live acceptance, and a
   machine-valid run is review-required, not semantic acceptance.

Strictness (T13 must-fix 2): the harness fails on any run that exits non-zero,
lacks a valid run report, has a failed or skipped work stage, or warns with a
code outside the explicit expected-gap set. It also fails when adoption does
not match the explicit ``--expect-adopted`` / ``--expect-coded-gaps`` (there
are no zero defaults), when the ledger does not conserve the adopted messages,
when a rerun adopts anything, when integrity is not ok, when extract shows no
advancement (``EXTRACTED`` > 0) or drops a document (outcomes must sum to the
input), when ``PARSER_MISSING`` appears while native parsers are required (the
default), and when any required fault was not exercised and detected.

Fault runs are just as strict (T12s review): each fault scenario has an explicit
set of expected stage results (``FAULT_SCENARIOS``: the injected fault plus any
documented persistent earlier fault), every other work stage must be ok or an
expected-gap warning, and the fault runs get the same integrity, report,
extract-accounting and parser checks as the baseline runs. A detected fault
never excuses an unrelated warning or failure in the same run.

Exit codes: 0 every assertion held, 1 an assertion failed, 2 setup refused.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

CONTAINER = None       # set from --container (no host values in source)
CONTAINER_WORK = None  # set from --container-work
TEST_DIR = LIVE_DIR = None
RECEIPT_VERSION = 2
TARGET_MODE = "TEST_SANDBOX_EVIDENCE"  # never "live acceptance"
SANDBOX_MARKER = ".t12-sandbox-v1"     # scripts/t12_fault_inject.py refuses a project without it
MANIFEST = """
import hashlib, os
root = "%s"
h = hashlib.sha256(); n = size = 0
for base, dirs, files in sorted(os.walk(root)):
    dirs.sort()
    for name in sorted(files):
        p = os.path.join(base, name)
        try: s = os.lstat(p)
        except OSError: continue
        n += 1; size += s.st_size
        h.update(("%%s\\\\0%%d\\\\0%%d\\\\n" %% (os.path.relpath(p, root), s.st_size, s.st_mtime_ns)).encode())
print(n, size, h.hexdigest())
"""
COPY_ITEMS = ("mail-history", "processing-v1", "portal-queue-v1", ".t4-adoption-config-v1.json")
WORK_STAGES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs")
COUNT_KEYS = ("input", "output", "failed", "skipped")
LEDGER_COUNTS = """
import json, sqlite3
p = "%s"
db = sqlite3.connect("file:" + p + "/processing-v1/ledger.sqlite?mode=ro&immutable=1", uri=True)
q = sqlite3.connect("file:" + p + "/portal-queue-v1/portal.sqlite?mode=ro&immutable=1", uri=True)
t = {r[0] for r in q.execute("select name from sqlite_master where type='table'")}
print(json.dumps({"mail_messages": db.execute("select count(*) from mail_messages").fetchone()[0],
  "originals": db.execute("select count(*) from originals").fetchone()[0],
  "runs": db.execute("select count(*) from runs").fetchone()[0],
  "quick_check": db.execute("pragma quick_check").fetchone()[0],
  "foreign_key_violations": len(db.execute("pragma foreign_key_check").fetchall()),
  "portal_quick_check": q.execute("pragma quick_check").fetchone()[0] if t else "ok",
  "portal_items": q.execute("select count(*) from portal_items").fetchone()[0] if "portal_items" in t else 0,
  "portal_refusals": q.execute("select count(*) from portal_refusals").fetchone()[0] if "portal_refusals" in t else 0}))
"""
# Faults every strict harness receipt must show exercised and detected.
REQUIRED_FAULTS = ("telegram_down", "duplicate_receipt", "portal_404", "corrupt_pdf", "docker_down")
# Warnings that are expected on the restored TEST baseline (explicit; extend with --allow-warn).
# ADOPTION_CODED_GAPS: G option c (2026-10-19 16:35), checked against --expect-coded-gaps.
# EXTRACT_WORKBOOK_POLICY_REQUIRED: XLSX is always held for the owner's workbook policy (T11k).
# GATE_REVIEW_REQUIRED: everything past privacy needs the owner; review-required by design.
DEFAULT_ALLOWED_WARNINGS = (("adoption", "ADOPTION_CODED_GAPS"),
                            ("extract", "EXTRACT_WORKBOOK_POLICY_REQUIRED"),
                            ("digest_gates", "GATE_REVIEW_REQUIRED"))
PARSER_MISSING_CODES = ("PARSER_MISSING", "EXTRACT_NATIVE_PARSER_MISSING")
DOCKER_DOWN_EXIT = 75
ADOPTION_BLOCKED_EXIT = 10 + WORK_STAGES.index("adoption")  # run_orchestrator: 10 + first failed stage
# Per-scenario stage results (T12s review fix). Each fault run must show EXACTLY these
# (status, error_code) on the named work stages; every other work stage must be ok or warn
# with a code in the run's expected-gap set (the same set as the baseline runs). Anything
# else, including an unrelated warning next to a detected fault, fails the harness.
# The faults run in this order on one sandbox, so a fault that persists by design is part
# of every later scenario:
#   duplicate_receipt: the adopter is fail-closed (ADOPTION_BLOCKED, exit names adoption)
#     and delta reports the same receipt conflict. The duplicate is moved out afterwards,
#     so neither persists.
#   portal_404: the real portal stage reports the blocked item as PORTAL_HTTP_404.
#   corrupt_pdf: extract codes the damaged document (EXTRACT_DOCUMENT_FAILURES). The item
#     blocked by portal_404 stays blocked until resolved, so portal still warns
#     PORTAL_HTTP_404 (persistent prior fault).
FAULT_SCENARIOS = {
    "duplicate_receipt": {"exit": ADOPTION_BLOCKED_EXIT,
                          "stages": {"delta": ("warn", "DELTA_BLOCKING_CONFLICTS"),
                                     "adoption": ("fail", "ADOPTION_BLOCKED")}},
    "portal_404": {"exit": 0, "stages": {"portal": ("warn", "PORTAL_HTTP_404")}},
    "corrupt_pdf": {"exit": 0, "stages": {"portal": ("warn", "PORTAL_HTTP_404"),
                                          "extract": ("warn", "EXTRACT_DOCUMENT_FAILURES")}},
}


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw).stdout


def docker(args, docker_bin):
    return sh([docker_bin, *args], timeout=600)


PARSER_VENV_PYTHON = "/opt/campaign-tool/venv/bin/python"  # container/flock-ocr/Dockerfile


def container_interpreter(docker_bin):
    """The pinned image's parser venv (pypdf/openpyxl) when present, else system python3."""
    probe = subprocess.run([docker_bin, "exec", CONTAINER, "test", "-x", PARSER_VENV_PYTHON],
                           capture_output=True, timeout=60)
    return PARSER_VENV_PYTHON if probe.returncode == 0 else "/usr/bin/python3"


def ledger_counts(docker_bin, project):
    out = subprocess.run([docker_bin, "exec", "-i", "-u", "coder", CONTAINER, "python3", "-B", "-"],
                         input=LEDGER_COUNTS % project, capture_output=True, text=True,
                         check=True, timeout=600).stdout
    return json.loads(out)


def ledger_counts_local(project, python=None):
    """The same count/integrity probe as ``ledger_counts``, run locally (synthetic tests)."""
    out = subprocess.run([python or sys.executable, "-B", "-"], input=LEDGER_COUNTS % project,
                         capture_output=True, text=True, check=True, timeout=600).stdout
    return json.loads(out)


def report_is_valid(report):
    """run-report v1 (schema + cross-field rules); False for a missing or invalid report."""
    if not report:
        return False
    from campaign_tool.records import run_report
    try:
        run_report.validate_run_report(report)
    except (ValueError, KeyError, TypeError):
        return False
    return True


def summarize(report, exit_code):
    stages = {}
    for stage in report.get("stages", []):
        entry = {"status": stage.get("status"), "error_code": stage.get("error_code"),
                 "counts": stage.get("counts")}
        if "outcomes" in stage:
            entry["outcomes"] = {item["code"]: item["count"] for item in stage["outcomes"]}
        stages[stage.get("name")] = entry
    return {"exit": exit_code, "report_present": bool(report), "report_valid": report_is_valid(report),
            "overall_status": report.get("overall_status"),
            "notify_failed": report.get("notify_failed"), "stages": stages,
            "new_data": report.get("new_data"), "review_required": report.get("review_required_count"),
            "needs_g": report.get("needs_g")}


# --- evaluation (pure; unit-tested with synthetic and adversarial receipts) --------------

def _stage(run, name):
    stage = (run.get("stages") or {}).get(name)
    return stage if isinstance(stage, dict) else {}


def _counts(run, name):
    """A stage's counts, or None when any count is missing: never defaulted to zero."""
    counts = _stage(run, name).get("counts")
    if not isinstance(counts, dict):
        return None
    if not all(type(counts.get(key)) is int and counts[key] >= 0 for key in COUNT_KEYS):
        return None
    return counts


def _needs(run, code, stage):
    """Count of a needs-G item; producers drop zero-count items, so absent means 0."""
    return sum(item.get("count", 0) for item in (run.get("needs_g") or [])
               if isinstance(item, dict) and item.get("code") == code and item.get("stage") == stage)


def _outcomes(run, name="extract"):
    outcomes = _stage(run, name).get("outcomes")
    return outcomes if isinstance(outcomes, dict) else {}


def _clean_run(run, allowed, expected_stages=None, exit_code=0):
    """The expected exit, a valid report, and every work stage as expected.

    A stage named in ``expected_stages`` must show exactly that (status, error_code).
    Every other work stage must be ok, or warn with a (stage, code) in ``allowed``.
    """
    expected_stages = expected_stages or {}
    if run.get("exit") != exit_code or not run.get("report_present") or not run.get("report_valid"):
        return False
    for name in WORK_STAGES:
        stage = _stage(run, name)
        result = (stage.get("status"), stage.get("error_code"))
        if name in expected_stages:
            if result != tuple(expected_stages[name]):
                return False
            continue
        if stage.get("status") == "ok":
            continue
        if stage.get("status") != "warn" or (name, stage.get("error_code")) not in allowed:
            return False
    return True


def _ran(run, *names):
    return all(_stage(run, name).get("status") in ("ok", "warn") for name in names)


def _extract_accounted(run):
    """Every pending document has exactly one outcome; AWAITING_OCR matches its needs-G item."""
    counts = _counts(run, "extract")
    return bool(counts is not None and sum(_outcomes(run).values()) == counts["input"]
                and _outcomes(run).get("AWAITING_OCR", 0) == _needs(run, "AWAITING_OCR", "extract"))


def _adopts_nothing(run, last_ledger, injected_messages):
    """A fault run with no new mail adopts nothing and fails nothing; the ledger's mail grows
    only by the messages the injector itself ingested (corrupt_pdf goes through the intake)."""
    adoption = _counts(run, "adoption")
    return bool(adoption is not None and adoption["output"] == 0 and adoption["failed"] == 0
                and type(injected_messages) is int and injected_messages >= 0
                and type(last_ledger.get("mail_messages")) is int
                and (run.get("ledger_after") or {}).get("mail_messages")
                == last_ledger["mail_messages"] + injected_messages)


def _fault_checks(faults, runs, allowed, required):
    checks = {}
    last = runs[-1] if runs else {}
    last_ledger = last.get("ledger_after") or {}
    # Strict per-scenario stage results: a detected fault never excuses an unrelated
    # warning or failure elsewhere in the same run (FAULT_SCENARIOS).
    for fault, scenario in FAULT_SCENARIOS.items():
        if fault in required:
            checks[f"fault_{fault}_only_expected_results"] = _clean_run(
                faults.get(fault) or {}, allowed, scenario["stages"], scenario["exit"])
    if "telegram_down" in required:
        # Notify points at a closed port on every run: the notify stage fails, the exit does not.
        checks["fault_telegram_down"] = bool(runs) and all(
            run.get("notify_failed") is True and _stage(run, "notify").get("status") == "fail"
            and run.get("exit") == 0 for run in runs)
    if "duplicate_receipt" in required:
        # The adopter is fail-closed on a duplicate receipt identity: it writes nothing, codes
        # the receipt gap and blocks (ADOPTION_BLOCKED). Adoption's plan policy is
        # on_fail=continue, so the later stages still run; the exit names the adoption stage.
        dup = faults.get("duplicate_receipt") or {}
        adoption = _counts(dup, "adoption")
        checks["fault_duplicate_receipt"] = bool(
            dup.get("injected") is True and dup.get("restored") is True
            and dup.get("exit") == ADOPTION_BLOCKED_EXIT and dup.get("report_valid")
            and _stage(dup, "adoption").get("status") == "fail"
            and _stage(dup, "adoption").get("error_code") == "ADOPTION_BLOCKED"
            and _needs(dup, "ADOPTION_RECEIPT_GAPS", "adoption") >= 1
            and adoption is not None and adoption["output"] == 0
            and _ran(dup, "portal", "extract", "digest_gates", "outputs")
            and (dup.get("ledger_after") or {}).get("mail_messages") == last_ledger.get("mail_messages")
            and last_ledger.get("mail_messages") is not None)
    if "portal_404" in required:
        run = faults.get("portal_404") or {}
        injection = run.get("injection") or {}
        portal = _stage(run, "portal")
        # Portal plan policy is on_fail=continue; a blocked 404 is a warn and later stages run.
        checks["fault_portal_404"] = bool(
            injection.get("status") == "injected" and injection.get("http_status") == 404
            and run.get("exit") == 0 and run.get("report_valid")
            and portal.get("status") == "warn" and portal.get("error_code") == "PORTAL_HTTP_404"
            and _needs(run, "PORTAL_HTTP_404", "portal") >= 1
            and _adopts_nothing(run, last_ledger, 0)
            and _ran(run, "extract", "digest_gates", "outputs"))
    if "corrupt_pdf" in required:
        run = faults.get("corrupt_pdf") or {}
        injection = run.get("injection") or {}
        extract = _stage(run, "extract")
        # Extract plan policy is on_fail=stop; a corrupt document is a coded warn, not a stage
        # failure, so digest_gates and outputs still run.
        checks["fault_corrupt_pdf"] = bool(
            injection.get("status") == "injected" and injection.get("documents", 0) >= 1
            and run.get("exit") == 0 and run.get("report_valid")
            and _outcomes(run).get("CORRUPT_DOCUMENT", 0) >= 1
            and extract.get("status") == "warn" and extract.get("error_code") == "EXTRACT_DOCUMENT_FAILURES"
            and _needs(run, "EXTRACT_CORRUPT_DOCUMENT", "extract") >= 1
            and _adopts_nothing(run, last_ledger, injection.get("messages"))
            and _ran(run, "digest_gates", "outputs"))
    if "docker_down" in required:
        down = faults.get("docker_down") or {}
        checks["fault_docker_down"] = down.get("exit") == DOCKER_DOWN_EXIT and down.get("ledger_unchanged") is True
    return checks


def evaluate(receipt, *, required_faults=REQUIRED_FAULTS, isolation=True):
    """Return ``(checks, failures)`` for a harness receipt. ``failures`` empty means pass.

    ``required_faults`` and ``isolation`` exist for the synthetic real-adapter test,
    which has no wrapper (docker/notify) and no live/TEST trees; the harness itself
    always uses the defaults.
    """
    expect = receipt.get("expect") or {}
    runs = receipt.get("runs") or []
    faults = receipt.get("faults") or {}
    before = receipt.get("sandbox_before") or {}
    allowed = {tuple(pair) for pair in expect.get("allowed_warnings") or ()}
    adopted, coded = expect.get("adopted"), expect.get("coded_gaps")
    require_parsers = expect.get("require_native_parsers")
    checks = {}

    checks["target_mode_is_test_evidence"] = (receipt.get("target_mode") == TARGET_MODE
                                              and receipt.get("live_acceptance") is False)
    checks["expectations_explicit"] = (
        type(adopted) is int and adopted >= 1 and type(coded) is int and coded >= 0
        and type(require_parsers) is bool and type(expect.get("runs")) is int and expect["runs"] >= 3
        and not (require_parsers and any(code in PARSER_MISSING_CODES for _, code in allowed)))
    checks["three_successful_runs"] = (len(runs) >= 3 and len(runs) == expect.get("runs")
                                       and all(_clean_run(run, allowed) for run in runs))
    checks["every_run_reported_all_stages"] = bool(runs) and all(
        set(WORK_STAGES) <= set(run.get("stages") or {}) for run in runs)

    first = runs[0] if runs else {}
    adoption_counts = [_counts(run, "adoption") for run in runs]
    delta_counts = [_counts(run, "delta") for run in runs]
    checks["adoption_counts_present"] = bool(runs) and all(
        counts is not None for counts in adoption_counts + delta_counts)
    a1, d1 = (adoption_counts[0], delta_counts[0]) if runs else (None, None)
    checks["adoption_matches_expected"] = bool(
        a1 is not None and d1 is not None and a1["output"] == adopted and d1["output"] == adopted)
    checks["coded_gaps_match_expected"] = bool(runs) and all(
        _needs(run, "ADOPTION_CODED_GAPS", "adoption") == coded for run in runs)
    # Option-c accounting: every receipt is either already in the ledger, adopted now, or a
    # coded gap (adopted + coded gaps == pending), and the ledger grew by exactly the adopted.
    ledger1 = first.get("ledger_after") or {}
    checks["adoption_conserved"] = bool(
        a1 is not None and type(coded) is int and type(before.get("mail_messages")) is int
        and a1["failed"] == 0 and a1["input"] == a1["output"] + a1["skipped"]
        and a1["input"] == before["mail_messages"] + a1["output"] + coded
        and ledger1.get("mail_messages") == before["mail_messages"] + a1["output"])
    checks["rerun_adopts_zero"] = len(runs) >= 3 and all(
        counts is not None and counts["output"] == 0 and counts["failed"] == 0 for counts in adoption_counts[1:]
    ) and all(counts is not None and counts["output"] == 0 for counts in delta_counts[1:]) and all(
        (run.get("new_data") or {}).get("documents") == 0 for run in runs[1:])
    checks["ledger_stable_after_first_run"] = bool(runs) and ledger1.get("mail_messages") is not None and all(
        (run.get("ledger_after") or {}).get(key) == ledger1.get(key)
        for run in runs[1:] for key in ("mail_messages", "originals"))
    # The fault runs that executed stages (not docker_down) get the same integrity, extract
    # accounting and parser checks as the baseline runs.
    fault_runs = [faults[name] for name in FAULT_SCENARIOS
                  if isinstance(faults.get(name), dict) and faults[name].get("stages")]
    every = runs + fault_runs
    checks["integrity_ok"] = bool(runs) and all(
        (run.get("ledger_after") or {}).get("quick_check") == "ok"
        and (run.get("ledger_after") or {}).get("foreign_key_violations") == 0
        and (run.get("ledger_after") or {}).get("portal_quick_check") == "ok" for run in every)
    checks["run_reports_valid"] = bool(runs) and all(run.get("report_valid") is True for run in every)

    # Advancement (T11k outcomes): the first run extracts native text; every run accounts for
    # every pending document with exactly one outcome code (AWAITING_OCR counted, never dropped).
    checks["extract_advanced"] = bool(runs) and _outcomes(first).get("EXTRACTED", 0) > 0 and (
        (first.get("new_data") or {}).get("extracted_units", 0) > 0)
    checks["extract_outcomes_account_for_every_document"] = bool(runs) and all(
        _extract_accounted(run) for run in every)
    checks["native_parsers_present"] = (require_parsers is False) or (
        require_parsers is True and not any(
            _outcomes(run).get("PARSER_MISSING", 0)
            or _stage(run, "extract").get("error_code") == "EXTRACT_NATIVE_PARSER_MISSING"
            or _needs(run, "EXTRACT_NATIVE_PARSER_MISSING", "extract") for run in every))

    checks.update(_fault_checks(faults, runs, allowed, required_faults))
    for fault in required_faults:
        checks.setdefault("fault_" + fault, False)  # an unknown required fault cannot pass
    if isolation:
        checks["live_tree_unchanged"] = receipt.get("live_tree_unchanged") is True
        checks["test_tree_unchanged"] = receipt.get("test_tree_unchanged") is True
        checks["test_untouched"] = (receipt.get("test_after") is not None
                                    and receipt.get("test_after") == receipt.get("test_before"))
    failures = sorted(name for name, ok in checks.items() if ok is not True)
    return checks, failures


# --- driver -------------------------------------------------------------------------------

def render(engine, runtime, values):
    values_file = runtime.parent / (runtime.name + "-values.json")
    values_file.write_text(json.dumps(values), encoding="utf-8")
    sh([sys.executable, "-B", str(engine / "deploy/records-runtime/render.py"), "--values", str(values_file),
        "--out", str(runtime), "--target",
        "TEST=" + str(engine / "deploy/records-runtime/targets/test-container.json.in")])


def manifest(docker_bin, root):
    out = subprocess.run([docker_bin, "exec", "-i", "-u", "coder", CONTAINER, "python3", "-B", "-"],
                         input=MANIFEST % root, capture_output=True, text=True,
                         check=True, timeout=600).stdout.split()
    return {"files": int(out[0]), "bytes": int(out[1]), "sha256": out[2]}


def run_once(runtime, target_dir):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "RECORDS_NOTIFY_API_BASE": "http://127.0.0.1:9", "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
           "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")}
    env = {k: v for k, v in env.items() if v}
    proc = subprocess.run([str(runtime / "run-records.sh"), "--target", "TEST"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3000)
    report_path = target_dir / "run-report.json"
    report = {}
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text())
        except ValueError:
            report = {}
        # Keep the evidence, but a later run that writes no report must not reuse this one.
        os.replace(report_path, report_path.with_name(f"run-report-{time.time_ns()}.json"))
    return proc.returncode, report


def inject(docker_bin, base, fault, *extra):
    """Run t12_fault_inject.py in the container; its last stdout line, or a refusal."""
    script = f"{base['CONTAINER_ENGINE_DIR']}/scripts/t12_fault_inject.py"
    proc = subprocess.run([docker_bin, "exec", "-u", "coder", CONTAINER, "env",
                           f"PYTHONPATH={base['CONTAINER_ENGINE_DIR']}", base["CONTAINER_PYTHON"], "-B",
                           script, fault, "--project", base["CONTAINER_PROJECT_DIR"], *extra],
                          capture_output=True, text=True, timeout=600)
    for line in reversed(proc.stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return {"status": "refused", "code": "injector_no_result"}


def allowed_warning(text):
    stage, sep, code = text.partition(":")
    if not sep or stage not in WORK_STAGES or not code or not code.replace("_", "").isalnum():
        raise argparse.ArgumentTypeError("expected STAGE:CODE with a work stage name")
    return stage, code


def positive(text):
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("expected an integer >= 1")
    return value


def non_negative(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("expected an integer >= 0")
    return value


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--engine", required=True, help="host path of the pinned engine tree (git archive)")
    p.add_argument("--release-commit", required=True)
    p.add_argument("--host-work", default=str(Path.home() / "deflockslo-workspace/work"))
    p.add_argument("--scratch", required=True, help="host scratch dir for runtime + receipt (outside Git)")
    p.add_argument("--docker", default=shutil.which("docker") or "/usr/bin/docker")
    p.add_argument("--runs", type=int, default=3, help="consecutive successful baseline runs (>= 3)")
    p.add_argument("--receipt", required=True)
    p.add_argument("--container", required=True, help="Flock container name")
    p.add_argument("--container-work", required=True, help="work directory inside the container")
    p.add_argument("--container-python", help="stage interpreter inside the container (absolute); default: "
                   "the image's parser venv when present, else /usr/bin/python3")
    p.add_argument("--expect-adopted", type=positive, required=True,
                   help="messages the first run must adopt (restored TEST baseline: 36)")
    p.add_argument("--expect-coded-gaps", type=non_negative, required=True,
                   help="coded-gap messages every run must report (option c; TEST baseline: 115)")
    p.add_argument("--require-native-parsers", action=argparse.BooleanOptionalAction, default=True,
                   help="PARSER_MISSING fails the harness (default on)")
    p.add_argument("--allow-warn", type=allowed_warning, action="append", default=[], metavar="STAGE:CODE",
                   help="add an expected warning to the defaults " + ", ".join(
                       f"{stage}:{code}" for stage, code in DEFAULT_ALLOWED_WARNINGS))
    args = p.parse_args(argv)
    allowed = sorted(set(DEFAULT_ALLOWED_WARNINGS) | set(args.allow_warn))
    if args.runs < 3:
        print(json.dumps({"status": "refused", "code": "runs_below_three"}))
        return 2
    if args.require_native_parsers and any(code in PARSER_MISSING_CODES for _, code in allowed):
        print(json.dumps({"status": "refused", "code": "parser_missing_cannot_be_expected"}))
        return 2
    global CONTAINER, CONTAINER_WORK, TEST_DIR, LIVE_DIR
    CONTAINER, CONTAINER_WORK = args.container, args.container_work.rstrip("/")
    TEST_DIR, LIVE_DIR = CONTAINER_WORK + "/records-project-TEST", CONTAINER_WORK + "/records-project"
    engine, scratch = Path(args.engine).resolve(), Path(args.scratch).resolve()
    if not (engine / "campaign_tool/records/run_stages.py").is_file() or not (
            engine / "scripts/t12_fault_inject.py").is_file():
        print(json.dumps({"status": "refused", "code": "engine_tree_missing"}))
        return 2
    if engine.parent != Path(args.host_work).resolve():
        print(json.dumps({"status": "refused", "code": "engine_not_under_host_work"}))
        return 2
    sys.path.insert(0, str(engine))  # run-report v1 validation from the pinned engine
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sandbox_rel = f".t11i-sandbox-{stamp}"
    container_project = f"{CONTAINER_WORK}/{sandbox_rel}/records-project"
    # 1. Sandbox copy of TEST, made inside the container as the ledger owner, plus the marker.
    copy = " ".join(f"'{TEST_DIR}/{item}'" for item in COPY_ITEMS)
    docker(["exec", "-u", "coder", CONTAINER, "sh", "-c",
            f"umask 077 && mkdir -p '{container_project}' && cp -a {copy} '{container_project}/' && "
            f"printf '%s\\n' '{{\"sandbox_marker_version\":1}}' > '{container_project}/{SANDBOX_MARKER}'"],
           args.docker)
    test_before = ledger_counts(args.docker, TEST_DIR)
    sandbox_before = ledger_counts(args.docker, container_project)
    image = docker(["inspect", "--format", "{{.Image}}", CONTAINER], args.docker).strip()
    container_python = args.container_python or container_interpreter(args.docker)
    scratch.mkdir(parents=True, exist_ok=True)
    env_file = scratch / "fake-telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=000000:SYNTHETIC_NOT_A_TOKEN\nTELEGRAM_HOME_CHANNEL=-100\n")
    env_file.chmod(0o600)
    before_live = manifest(args.docker, LIVE_DIR)
    before_test_tree = manifest(args.docker, TEST_DIR)
    base = {
        "RELEASE_COMMIT": args.release_commit, "ENGINE_DIR": str(engine), "DOCKER": args.docker,
        "TIMEOUT": shutil.which("timeout"), "FLOCK": shutil.which("flock"), "CONTAINER": CONTAINER,
        "EXPECTED_IMAGE": image, "RUN_UID": str(os.getuid()), "HOST_PYTHON": sys.executable,
        "NOTIFY_PY": str(engine / "deploy/records-runtime/notify.py"), "NOTIFY_ENV_FILE": str(env_file),
        "NOTIFY_LABEL": "T11i-TEST", "DOCKER_WAIT_ATTEMPTS": "3", "DOCKER_WAIT_DELAY": "1",
        "DOCKER_WAIT_MAX_DELAY": "2", "DOCKER_CMD_TIMEOUT": "20", "DOCKER_WAIT_DEADLINE": "60",
        "NOTIFY_TIMEOUT": "20", "RUN_BUDGET_SECONDS": "2640", "MIN_FREE_BYTES": "1073741824",
        "DEFAULT_TARGET": "TEST",
        "SCHEDULED_TARGET": "TEST", "CONTAINER_USER": "coder",
        "CONTAINER_ENGINE_DIR": f"{CONTAINER_WORK}/{engine.name}",
        "CONTAINER_PROJECT_DIR": container_project,
        "CONTAINER_APPROVAL_FILE": f"{CONTAINER_WORK}/.flock-portal-intake/approval.json",
        "CONTAINER_PYTHON": container_python,
        "ADOPTION_CONFIG": f"{container_project}/.t4-adoption-config-v1.json",
    }
    runtime = scratch / "runtime"
    render(engine, runtime, {**base, "RUNTIME_DIR": str(runtime), "RUN_RECORDS_SH": str(runtime / "run-records.sh")})
    receipt = {"receipt_version": RECEIPT_VERSION, "target_mode": TARGET_MODE, "live_acceptance": False,
               "evidence_note": "sandbox copy of TEST; not live acceptance; review-required",
               "engine_commit": args.release_commit, "sandbox": sandbox_rel,
               "expect": {"adopted": args.expect_adopted, "coded_gaps": args.expect_coded_gaps,
                          "require_native_parsers": args.require_native_parsers, "runs": args.runs,
                          "allowed_warnings": [list(pair) for pair in allowed],
                          "required_faults": list(REQUIRED_FAULTS)},
               "test_before": test_before, "sandbox_before": sandbox_before,
               "runs": [], "faults": {}, "checks": {}}
    target_dir = runtime / "TEST"
    for index in range(args.runs):
        code, report = run_once(runtime, target_dir)
        receipt["runs"].append({"run": index + 1, **summarize(report, code),
                                "ledger_after": ledger_counts(args.docker, container_project)})
    # Fault: duplicate receipt -- one receipt file repeated under a new name. Afterwards the
    # copy is moved out of receipts/ (kept in the sandbox) so the next faults start clean.
    dup_dir = f"{container_project}/mail-history/receipts"
    try:
        docker(["exec", "-u", "coder", CONTAINER, "sh", "-c",
                f"cd '{dup_dir}' && f=$(ls *.json | head -n 1) && cp -p \"$f\" \"zz-duplicate-$f\""], args.docker)
        injected = True
    except subprocess.CalledProcessError:
        injected = False
    code, report = run_once(runtime, target_dir)
    summary = {"injected": injected, **summarize(report, code),
               "ledger_after": ledger_counts(args.docker, container_project)}
    try:
        docker(["exec", "-u", "coder", CONTAINER, "sh", "-c",
                f"cd '{dup_dir}' && for f in zz-duplicate-*.json; do "
                f"mv \"$f\" \"{container_project}/.t12-removed-$f\"; done"], args.docker)
        summary["restored"] = True
    except subprocess.CalledProcessError:
        summary["restored"] = False
    receipt["faults"]["duplicate_receipt"] = summary
    # Fault: portal 404 -- the real fetch state machine with a no-network 404 transport, one
    # synthetic item on an allowlisted host; the real portal stage must report it.
    injection = inject(args.docker, base, "portal-404", "--approval-file", base["CONTAINER_APPROVAL_FILE"])
    code, report = run_once(runtime, target_dir)
    receipt["faults"]["portal_404"] = {"injection": injection, **summarize(report, code),
                                       "ledger_after": ledger_counts(args.docker, container_project)}
    # Fault: corrupt PDF -- a damaged PDF through the engine's intake; extract must code it.
    injection = inject(args.docker, base, "corrupt-pdf")
    code, report = run_once(runtime, target_dir)
    receipt["faults"]["corrupt_pdf"] = {"injection": injection, **summarize(report, code),
                                        "ledger_after": ledger_counts(args.docker, container_project)}
    # Fault: docker down -- the wrapper's docker is a shim that always fails.
    shim_dir = scratch / "docker-down"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "docker"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(0o700)
    down = scratch / "runtime-docker-down"
    render(engine, down, {**base, "DOCKER": str(shim), "RUNTIME_DIR": str(down),
                          "RUN_RECORDS_SH": str(down / "run-records.sh")})
    before = ledger_counts(args.docker, container_project)
    code, report = run_once(down, down / "TEST")
    receipt["faults"]["docker_down"] = {**summarize(report, code), "ledger_unchanged":
                                        ledger_counts(args.docker, container_project) == before}
    receipt["test_after"] = ledger_counts(args.docker, TEST_DIR)
    receipt["live_tree_unchanged"] = manifest(args.docker, LIVE_DIR) == before_live
    receipt["test_tree_unchanged"] = manifest(args.docker, TEST_DIR) == before_test_tree
    checks, failures = evaluate(receipt)
    receipt["checks"], receipt["failed_checks"] = checks, failures
    receipt["passed"] = not failures
    Path(args.receipt).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": receipt["passed"], "target_mode": TARGET_MODE, "failed_checks": failures,
                      "checks": checks}, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
