#!/usr/bin/env python3
"""Run the T12 harness scenarios against a sandbox copy of TEST (T11i).

Host-side driver. It never touches the live records project or TEST itself:

1. Copies TEST (mail-history, processing-v1, portal-queue-v1 and the TEST
   adoption config) into a fresh sandbox directory, inside the Flock container
   as the ledger owner.
2. Renders the runtime templates with ``targets/test-sandbox.json.in``. Each
   stage runs in a throwaway container from the pinned Flock image with
   ``--network none``; only the sandbox copy (mounted at the live project path
   so stored absolute paths resolve into the copy), the engine tree and the
   read-only portal allowlist are mounted.
3. Runs ``run-records.sh --target TEST`` N times in a row (default 3), then
   the docker-down and duplicate-receipt fault runs. Notify always points at a
   closed loopback port with a fake token (telegram-down on every run), so no
   message can leave the host.
4. Writes a numbers-and-codes-only receipt (no paths, URLs or record text).

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

CONTAINER = "agentorange-flock"
CONTAINER_WORK = "/workspace/flockbot/work"
TEST_DIR = CONTAINER_WORK + "/records-project-TEST"
COPY_ITEMS = ("mail-history", "processing-v1", "portal-queue-v1", ".t4-adoption-config-v1.json")
WORK_STAGES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs")
LEDGER_COUNTS = """
import json, sqlite3
p = "%s"
db = sqlite3.connect("file:" + p + "/processing-v1/ledger.sqlite?mode=ro", uri=True)
q = sqlite3.connect("file:" + p + "/portal-queue-v1/portal.sqlite?mode=ro", uri=True)
t = {r[0] for r in q.execute("select name from sqlite_master where type='table'")}
print(json.dumps({"mail_messages": db.execute("select count(*) from mail_messages").fetchone()[0],
  "originals": db.execute("select count(*) from originals").fetchone()[0],
  "runs": db.execute("select count(*) from runs").fetchone()[0],
  "portal_items": q.execute("select count(*) from portal_items").fetchone()[0],
  "portal_refusals": q.execute("select count(*) from portal_refusals").fetchone()[0] if "portal_refusals" in t else 0}))
"""


def sh(argv, **kw):
    return subprocess.run(argv, check=True, capture_output=True, text=True, **kw).stdout


def docker(args, docker_bin):
    return sh([docker_bin, *args], timeout=600)


def ledger_counts(docker_bin, project):
    out = subprocess.run([docker_bin, "exec", "-i", "-u", "coder", CONTAINER, "python3", "-B", "-"],
                         input=LEDGER_COUNTS % project, capture_output=True, text=True,
                         check=True, timeout=120).stdout
    return json.loads(out)


def summarize(report, exit_code):
    stages = {s["stage"]: {"status": s["status"], "error_code": s.get("error_code"),
                           "counts": s.get("counts")} for s in report.get("stages", [])}
    return {"exit": exit_code, "overall_status": report.get("overall_status"),
            "notify_failed": report.get("notify_failed"), "stages": stages,
            "new_data": report.get("new_data"), "review_required": report.get("review_required"),
            "needs_g": report.get("needs_g")}


def render(engine, runtime, values):
    values_file = runtime.parent / (runtime.name + "-values.json")
    values_file.write_text(json.dumps(values), encoding="utf-8")
    sh([sys.executable, "-B", str(engine / "deploy/records-runtime/render.py"), "--values", str(values_file),
        "--out", str(runtime), "--target",
        "TEST=" + str(engine / "deploy/records-runtime/targets/test-sandbox.json.in")])


def run_once(runtime, target_dir):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/"),
           "RECORDS_NOTIFY_API_BASE": "http://127.0.0.1:9", "XDG_RUNTIME_DIR": os.environ.get("XDG_RUNTIME_DIR", ""),
           "DOCKER_HOST": os.environ.get("DOCKER_HOST", "")}
    env = {k: v for k, v in env.items() if v}
    proc = subprocess.run([str(runtime / "run-records.sh"), "--target", "TEST"], env=env,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3000)
    report_path = target_dir / "run-report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    return proc.returncode, report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--engine", required=True, help="host path of the pinned engine tree (git archive)")
    p.add_argument("--release-commit", required=True)
    p.add_argument("--host-work", default=str(Path.home() / "deflockslo-workspace/work"))
    p.add_argument("--scratch", required=True, help="host scratch dir for runtime + receipt (outside Git)")
    p.add_argument("--docker", default=shutil.which("docker") or "/usr/bin/docker")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--receipt", required=True)
    args = p.parse_args(argv)
    engine, scratch = Path(args.engine).resolve(), Path(args.scratch).resolve()
    if not (engine / "campaign_tool/records/run_stages.py").is_file():
        print(json.dumps({"status": "refused", "code": "engine_tree_missing"}))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sandbox_rel = f".t11i-sandbox-{stamp}"
    container_project = f"{CONTAINER_WORK}/{sandbox_rel}/records-project"
    host_project = f"{args.host_work}/{sandbox_rel}/records-project"
    # 1. Sandbox copy of TEST, made inside the container as the ledger owner.
    copy = " ".join(f"'{TEST_DIR}/{item}'" for item in COPY_ITEMS)
    docker(["exec", "-u", "coder", CONTAINER, "sh", "-c",
            f"umask 077 && mkdir -p '{container_project}' && cp -a {copy} '{container_project}/'"], args.docker)
    before_test = ledger_counts(args.docker, TEST_DIR)
    image = docker(["inspect", "--format", "{{.Image}}", CONTAINER], args.docker).strip()
    scratch.mkdir(parents=True, exist_ok=True)
    env_file = scratch / "fake-telegram.env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=000000:SYNTHETIC_NOT_A_TOKEN\nTELEGRAM_HOME_CHANNEL=-100\n")
    env_file.chmod(0o600)
    approval = f"{args.host_work}/.flock-portal-intake/approval.json"
    base = {
        "RELEASE_COMMIT": args.release_commit, "ENGINE_DIR": str(engine), "DOCKER": args.docker,
        "TIMEOUT": shutil.which("timeout"), "FLOCK": shutil.which("flock"), "CONTAINER": CONTAINER,
        "EXPECTED_IMAGE": image, "RUN_UID": str(os.getuid()), "HOST_PYTHON": sys.executable,
        "NOTIFY_PY": str(engine / "deploy/records-runtime/notify.py"), "NOTIFY_ENV_FILE": str(env_file),
        "NOTIFY_LABEL": "T11i-TEST", "DOCKER_WAIT_ATTEMPTS": "3", "DOCKER_WAIT_DELAY": "1",
        "DOCKER_WAIT_MAX_DELAY": "2", "DOCKER_CMD_TIMEOUT": "20", "DOCKER_WAIT_DEADLINE": "60",
        "NOTIFY_TIMEOUT": "20", "RUN_BUDGET_SECONDS": "2640", "DEFAULT_TARGET": "TEST",
        "SCHEDULED_TARGET": "TEST", "SANDBOX_USER": "1000:1000", "SANDBOX_PROJECT_DIR": host_project,
        "SANDBOX_ENGINE_DIR": str(engine), "PORTAL_APPROVAL_FILE": approval,
    }
    runtime = scratch / "runtime"
    render(engine, runtime, {**base, "RUNTIME_DIR": str(runtime), "RUN_RECORDS_SH": str(runtime / "run-records.sh")})
    receipt = {"receipt_version": 1, "engine_commit": args.release_commit, "sandbox": sandbox_rel,
               "test_before": before_test, "runs": [], "faults": {}, "checks": {}}
    target_dir = runtime / "TEST"
    for index in range(args.runs):
        code, report = run_once(runtime, target_dir)
        receipt["runs"].append({"run": index + 1, **summarize(report, code),
                                "ledger_after": ledger_counts(args.docker, container_project)})
    # Fault: duplicate receipt -- one receipt file repeated under a new name.
    docker(["exec", "-u", "coder", CONTAINER, "sh", "-c",
            f"cd '{container_project}/mail-history/receipts' && f=$(ls *.json | head -n 1) && "
            f"cp -p \"$f\" \"zz-duplicate-$f\""], args.docker)
    code, report = run_once(runtime, target_dir)
    receipt["faults"]["duplicate_receipt"] = {**summarize(report, code),
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
    # Assertions (numbers only).
    runs = receipt["runs"]
    checks = receipt["checks"]
    checks["test_untouched"] = receipt["test_after"] == before_test
    checks["every_run_reported_all_stages"] = all(set(WORK_STAGES) <= set(r["stages"]) for r in runs)
    checks["notify_failed_never_changes_exit"] = all(r["notify_failed"] is True for r in runs)
    checks["consecutive_runs_same_exit"] = len({r["exit"] for r in runs}) == 1
    checks["rerun_adopts_zero"] = all((r["stages"].get("adoption", {}).get("counts") or {}).get("output", 0) == 0
                                      for r in runs[1:])
    checks["ledger_stable_after_first_run"] = all(r["ledger_after"]["mail_messages"] == runs[0]["ledger_after"]["mail_messages"]
                                                 for r in runs[1:])
    dup = receipt["faults"]["duplicate_receipt"]
    checks["duplicate_receipt_adopts_zero"] = (
        (dup["stages"].get("adoption", {}).get("counts") or {}).get("output", 0) == 0 and
        dup["ledger_after"]["mail_messages"] == runs[-1]["ledger_after"]["mail_messages"])
    dd = receipt["faults"]["docker_down"]
    checks["docker_down_exit_75_no_writes"] = dd["exit"] == 75 and dd["ledger_unchanged"]
    checks["awaiting_ocr_counted"] = all(any(item.get("code") in ("AWAITING_OCR", "EXTRACT_NATIVE_PARSER_MISSING")
                                             for item in (r.get("needs_g") or [])) or
                                         r["stages"].get("extract", {}).get("status") == "ok" for r in runs)
    receipt["passed"] = all(checks.values())
    Path(args.receipt).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": receipt["passed"], "checks": checks}, sort_keys=True))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
