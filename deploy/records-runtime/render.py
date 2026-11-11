#!/usr/bin/env python3
"""Render the records runtime templates into a private output directory.

    python3 deploy/records-runtime/render.py --values VALUES.json --out DIR \
        --target TEST=deploy/records-runtime/targets/stub.json.in [--target ...]

VALUES.json is a flat object mapping each placeholder name (without @) to a
string. The renderer fails closed. It refuses a missing, unknown or
shell-unsafe value, a leftover placeholder, an invalid stage plan, a timer
whose OnCalendar lines are not exactly the validated schedule, a timer without
Persistent=true, a budget that does not fit the unit's 47 minute
TimeoutStartSec, and a MIN_FREE_BYTES free-space threshold that is not a
canonical byte count between 64 MiB and 1 PiB (or a wrapper template that
lost the placeholder). It also refuses an output directory that already has files
or sits inside a Git checkout, because rendered files hold private host
values.

The timer calendar is data. VALUES.json may set TIMER_DAYS (default Mon..Fri; a
list such as Mon,Wed,Fri, or daily), TIMER_TIMES (default 07:00,15:00; ascending
HH:MM, at least 60 minutes apart) and TIMER_TIMEZONE (default America/Los_Angeles;
any IANA zone). Each OnCalendar line is built from those after strict validation
(campaign_tool.records.schedule, the same rules `records health` uses) and, where
`systemd-analyze` exists, checked by `systemd-analyze calendar`. Persistent=true
stays, so a slot missed during downtime catches up.

Output: run-records.sh (0700), records-run.service, records-run.timer,
records-arrival.path, records-arrival.service and targets/NAME.json (0600). Installing them into the private runtime and the
systemd user directory is a separate, owner-approved cutover step (plan T17).
Nothing here installs, enables or starts anything.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.records import run_orchestrator, schedule as records_schedule  # noqa: E402

TEMPLATES = (("run-records.sh.in", "run-records.sh", 0o700),
             ("records-run.service.in", "records-run.service", 0o600),
             ("records-run.timer.in", "records-run.timer", 0o600),
             ("records-arrival.path.in", "records-arrival.path", 0o600),
             ("records-arrival.service.in", "records-arrival.service", 0o600))
# Rendered only when BACKUP_DIR (the backup directory as mounted in the container) is given.
BACKUP_CHECK_TEMPLATES = (("records-backup-check.service.in", "records-backup-check.service", 0o600),
                          ("records-backup-check.timer.in", "records-backup-check.timer", 0o600))
PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")
ABSOLUTE = {"BACKUP_DIR", "ENGINE_DIR", "DOCKER", "TIMEOUT", "FLOCK", "RUNTIME_DIR", "HOST_PYTHON",
            "NOTIFY_PY", "NOTIFY_ENV_FILE", "RUN_RECORDS_SH", "STUB_FIXTURE", "RECORDS_DROP_DIR",
            # Container stage plans (targets/test-container.json.in,
            # targets/live-container.json.in and the Phase 2 OCR plans
            # targets/live-ocr-container.json.in and
            # targets/live-ocr-vision-container.json.in).
            # CONTAINER_OCR_VISION_CONFIG is the owner-only local-vision-config-v1
            # inside the container; only the live-ocr-vision plan uses (and so
            # requires) it.
            "CONTAINER_OCR_VISION_CONFIG",
            "CONTAINER_ENGINE_DIR", "CONTAINER_PROJECT_DIR", "CONTAINER_APPROVAL_FILE",
            "CONTAINER_PYTHON", "CONTAINER_SYNC_SCRIPT", "ADOPTION_CONFIG",
            # AUD-DF1: the TEST plan's own sandbox project, adoption config and approval file.
            "CONTAINER_PROJECT_DIR_TEST", "ADOPTION_CONFIG_TEST", "CONTAINER_APPROVAL_FILE_TEST"}
INTEGERS = {"RUN_UID", "DOCKER_WAIT_ATTEMPTS", "DOCKER_WAIT_DELAY", "DOCKER_WAIT_MAX_DELAY",
            "DOCKER_CMD_TIMEOUT", "DOCKER_WAIT_DEADLINE", "NOTIFY_TIMEOUT", "RUN_BUDGET_SECONDS",
            "MIN_FREE_BYTES"}
# Free-space preflight (Fable audit F4(a)): bytes that must be available on
# the RUNTIME_DIR filesystem before a run starts. The floor refuses values
# that would make the check meaningless (0, or a unit slip such as "2" for
# 2 GiB); the ceiling (1 PiB) refuses typos and keeps shell arithmetic safe.
MIN_FREE_BYTES_FLOOR = 64 * 1024 ** 2
MIN_FREE_BYTES_CEILING = 1024 ** 5
# Worst case of the wrapper's df probe (DF_TIMEOUT + kill grace), which runs
# before the Docker wait and so counts against RUN_BUDGET_SECONDS.
DF_PROBE_SECONDS = 10 + 2
TARGET_NAMES = {"DEFAULT_TARGET", "SCHEDULED_TARGET"}
# Supplied per target by the renderer, never by VALUES.json.
RESERVED = {"TARGET_NAME", "TIMER_ONCALENDAR"}
# Optional timer calendar inputs; absent means the default weekday schedule.
TIMER_KEYS = ("TIMER_DAYS", "TIMER_TIMES", "TIMER_TIMEZONE")
# Safety placeholders a template must keep: a wrapper template that dropped
# the free-space preflight would otherwise render cleanly without it.
REQUIRED_PLACEHOLDERS = {"run-records.sh.in": {"MIN_FREE_BYTES", "RECORDS_DROP_DIR", "SCHEDULED_TARGET"},
                         "records-arrival.path.in": {"RECORDS_DROP_DIR", "RUNTIME_DIR"},
                         "records-arrival.service.in": {"RUN_RECORDS_SH"}}
UNIT_TIMEOUT_SECONDS = 47 * 60
KILL_GRACE = 2
BACKSTOP_GRACE = 15
SAFETY_MARGIN = 30
ONCALENDAR_LINE = re.compile(r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:\.\.(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))?"
                             r"(?:,(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:\.\.(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))?)* )?"
                             r"\*-\*-\* (?:[01][0-9]|2[0-3]):[0-5][0-9]:00 [A-Za-z][A-Za-z0-9_+-]*"
                             r"(?:/[A-Za-z0-9_+-]+){0,2}")


class RenderError(ValueError):
    pass


def check_values(values):
    if not isinstance(values, dict):
        raise RenderError("values: expected a JSON object")
    for key, value in values.items():
        if key in RESERVED:
            raise RenderError(f"{key}: reserved; set per target by the renderer")
        if not isinstance(value, str) or not SAFE_VALUE.match(value):
            raise RenderError(f"{key}: value must be a non-empty shell-safe word")
        if key in ABSOLUTE and not value.startswith("/"):
            raise RenderError(f"{key}: expected an absolute path")
        if key in INTEGERS and not value.isdigit():
            raise RenderError(f"{key}: expected a non-negative integer")
        if key in TARGET_NAMES and not run_orchestrator.TARGET_RE.match(value):
            raise RenderError(f"{key}: expected an upper-case target name")
    timer_schedule(values)
    if "RELEASE_COMMIT" in values and not re.fullmatch(r"[0-9a-f]{40}", values["RELEASE_COMMIT"]):
        raise RenderError("RELEASE_COMMIT: expected 40 lower-case hex characters")
    if "EXPECTED_IMAGE" in values and not re.fullmatch(r"sha256:[0-9a-f]{64}",
                                                       values["EXPECTED_IMAGE"]):
        raise RenderError("EXPECTED_IMAGE: expected sha256:<64 hex>")
    if "MIN_FREE_BYTES" in values:
        check_min_free_bytes(values["MIN_FREE_BYTES"])
    if "RECORDS_DROP_DIR" in values:
        check_drop_dir(values["RECORDS_DROP_DIR"], values.get("RUNTIME_DIR"))


def timer_schedule(values):
    """The validated timer calendar from the optional TIMER_* values (default: weekdays twice a day)."""
    default = records_schedule.DEFAULT_SCHEDULE
    try:
        return records_schedule.Schedule(values.get("TIMER_DAYS", default.days_spec),
                                         values.get("TIMER_TIMES", default.times_spec),
                                         values.get("TIMER_TIMEZONE", default.timezone))
    except records_schedule.ScheduleError as exc:
        raise RenderError(f"timer: {exc}") from None


def check_drop_dir(drop, runtime):
    """The drop folder must be a normalised absolute path apart from the runtime tree.

    The runtime tree holds the arrival stamps, done/, reports and receipts, so a
    drop folder equal to, inside or around it would let the pipeline's own output
    re-trigger the path unit.
    """
    path = PurePosixPath(drop)
    if os.path.normpath(drop) != drop or drop == "/":
        raise RenderError("RECORDS_DROP_DIR: expected a normalised absolute path")
    if runtime is not None:
        other = PurePosixPath(os.path.normpath(runtime))
        if path == other or path in other.parents or other in path.parents:
            raise RenderError("RECORDS_DROP_DIR: must be separate from RUNTIME_DIR "
                              "(arrival, done and report trees)")


def check_min_free_bytes(value):
    """Validate the free-space preflight threshold (canonical decimal bytes)."""
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise RenderError("MIN_FREE_BYTES: expected a positive decimal byte count "
                          "without leading zeros")
    number = int(value)
    if number < MIN_FREE_BYTES_FLOOR:
        raise RenderError(f"MIN_FREE_BYTES: {number} is below the {MIN_FREE_BYTES_FLOOR} byte "
                          f"floor (64 MiB); the value is in bytes, not GiB")
    if number > MIN_FREE_BYTES_CEILING:
        raise RenderError(f"MIN_FREE_BYTES: {number} exceeds the {MIN_FREE_BYTES_CEILING} byte "
                          f"ceiling (1 PiB)")


def check_budget(values):
    number = {key: int(values[key]) for key in INTEGERS}
    total = (number["RUN_BUDGET_SECONDS"] + BACKSTOP_GRACE + KILL_GRACE
             + number["NOTIFY_TIMEOUT"] + KILL_GRACE + SAFETY_MARGIN)
    if total > UNIT_TIMEOUT_SECONDS:
        raise RenderError(f"budget: RUN_BUDGET_SECONDS + notify + grace = {total}s exceeds the "
                          f"{UNIT_TIMEOUT_SECONDS}s unit timeout")
    readiness = (DF_PROBE_SECONDS + number["DOCKER_WAIT_DEADLINE"]
                 + 3 * (number["DOCKER_CMD_TIMEOUT"] + KILL_GRACE))
    if readiness >= number["RUN_BUDGET_SECONDS"]:
        raise RenderError(f"budget: worst-case Docker readiness {readiness}s leaves no time "
                          f"inside RUN_BUDGET_SECONDS")
    if number["DOCKER_WAIT_ATTEMPTS"] < 1 or number["DOCKER_CMD_TIMEOUT"] < 1 \
            or number["NOTIFY_TIMEOUT"] < 1:
        raise RenderError("budget: attempts and timeouts must be at least 1")


def substitute(text, values, where):
    missing = sorted({name for name in PLACEHOLDER.findall(text) if name not in values})
    if missing:
        raise RenderError(f"{where}: no value for {missing}")
    rendered = PLACEHOLDER.sub(lambda match: values[match.group(1)], text)
    if "@" in rendered and PLACEHOLDER.search(rendered):
        raise RenderError(f"{where}: placeholder left after rendering")
    return rendered


def check_timer(text, expected=None):
    """Refuse a timer whose OnCalendar lines are not exactly the validated schedule's, or that lost Persistent=true."""
    expected = (expected or records_schedule.DEFAULT_SCHEDULE).on_calendar()
    slots = [line.split("=", 1)[1].strip() for line in text.splitlines()
             if line.startswith("OnCalendar=")]
    if slots != expected:
        raise RenderError(f"timer: expected OnCalendar slots {expected}, got {slots}")
    for slot in slots:
        if not ONCALENDAR_LINE.fullmatch(slot):
            raise RenderError(f"timer: unsafe OnCalendar value {slot!r}")
    if "Persistent=true" not in text.splitlines():
        raise RenderError("timer: Persistent=true is required so a missed slot catches up after downtime")
    analyzer = shutil.which("systemd-analyze")
    if analyzer:  # the strict pattern above is the fallback where systemd is absent
        for slot in slots:
            try:
                result = subprocess.run([analyzer, "calendar", slot], capture_output=True, text=True,
                                        timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode != 0:
                raise RenderError(f"timer: systemd-analyze rejected OnCalendar {slot!r}")


def inside_git_checkout(path):
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return True
    return False


def plan_projects(config):
    """Normalised ``--project`` values of a stage plan's enabled stages."""
    return {PurePosixPath(os.path.normpath(value)) for stage in config["stages"]
            if stage.get("enabled", True)
            for flag, value in zip(stage["argv"], stage["argv"][1:]) if flag == "--project"}


def check_overlap(plans):
    """Refuse a TEST plan whose project equals, holds or lies inside a LIVE plan's (AUD-DF1)."""
    for test_name, test in plans:
        for live_name, live in plans:
            if test["mode"] == "test" and live["mode"] == "live" and any(
                    a == b or a in b.parents or b in a.parents
                    for a in plan_projects(test) for b in plan_projects(live)):
                raise RenderError(f"target {test_name}: test plan project overlaps live target "
                                  f"{live_name}'s project")


def render(values, targets, out):
    check_values(values)
    out = Path(out).resolve()
    if inside_git_checkout(out):
        raise RenderError("out: refusing to write rendered private files inside a Git checkout")
    if out.exists() and any(out.iterdir()):
        raise RenderError("out: directory exists and is not empty")
    timer = timer_schedule(values)
    names = [name for name, _ in targets]
    if len(set(names)) != len(names):
        raise RenderError("targets: duplicate target name")
    for key in TARGET_NAMES:
        if key in values and values[key] not in names:
            raise RenderError(f"{key}: {values[key]} has no rendered stage plan (--target)")

    used = set()
    files = []
    plans = []
    for template, output, mode in TEMPLATES:
        text = (HERE / template).read_text(encoding="utf-8")
        found = set(PLACEHOLDER.findall(text))
        missing_required = sorted(REQUIRED_PLACEHOLDERS.get(template, set()) - found)
        if missing_required:
            raise RenderError(f"{template}: template lost required placeholder(s) "
                              f"{missing_required}")
        used.update(found)
        extra = {"TIMER_ONCALENDAR": "\n".join("OnCalendar=" + line for line in timer.on_calendar())}
        files.append((output, substitute(text, {**values, **extra}, template), mode))
    for template, output, mode in BACKUP_CHECK_TEMPLATES if "BACKUP_DIR" in values else ():
        text = (HERE / template).read_text(encoding="utf-8")
        used.update(PLACEHOLDER.findall(text))
        files.append((output, substitute(text, values, template), mode))
    for name, template in targets:
        if not run_orchestrator.TARGET_RE.match(name):
            raise RenderError(f"target {name!r}: expected an upper-case target name")
        text = Path(template).read_text(encoding="utf-8")
        used.update(set(PLACEHOLDER.findall(text)) - RESERVED)
        rendered = substitute(text, {**values, "TARGET_NAME": name}, f"target {name}")
        try:
            config = run_orchestrator.validate_target_config(json.loads(rendered))
        except ValueError as exc:
            raise RenderError(f"target {name}: {exc}") from None
        if config["target"] != name:
            raise RenderError(f"target {name}: stage plan names target {config['target']}")
        files.append((f"targets/{name}.json", rendered, 0o600))
        plans.append((name, config))
    check_overlap(plans)
    unknown = sorted(set(values) - used - set(TIMER_KEYS))
    if unknown:
        raise RenderError(f"values: unknown placeholder(s) {unknown}")
    check_budget(values)
    check_timer(next(text for output, text, _ in files if output == "records-run.timer"), timer)

    old_umask = os.umask(0o077)
    try:
        (out / "targets").mkdir(parents=True, exist_ok=True)
        for output, text, mode in files:
            path = out / output
            path.write_text(text, encoding="utf-8")
            path.chmod(mode)
    finally:
        os.umask(old_umask)
    return [output for output, _, _ in files]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Render the records runtime templates.")
    parser.add_argument("--values", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--target", action="append", default=[], metavar="NAME=TEMPLATE",
                        required=True)
    args = parser.parse_args(argv)
    try:
        values = json.loads(Path(args.values).read_text(encoding="utf-8"))
        targets = []
        for spec in args.target:
            name, sep, template = spec.partition("=")
            if not sep or not template:
                raise RenderError(f"--target {spec!r}: expected NAME=TEMPLATE")
            targets.append((name, template))
        written = render(values, targets, args.out)
    except (OSError, ValueError) as exc:
        print(f"render: refused: {exc}", file=sys.stderr)
        return 1
    for output in written:
        print(f"rendered {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
