#!/usr/bin/env python3
"""Render the records runtime templates into a private output directory.

    python3 deploy/records-runtime/render.py --values VALUES.json --out DIR \
        --target TEST=deploy/records-runtime/targets/stub.json.in [--target ...]

VALUES.json is a flat object mapping each placeholder name (without @) to a
string. The renderer fails closed. It refuses a missing, unknown or
shell-unsafe value, a leftover placeholder, an invalid stage plan, a timer
that is not the three Pacific slots (07:00, 13:00, 20:30
America/Los_Angeles), a budget that does not fit the unit's 47 minute
TimeoutStartSec, and a MIN_FREE_BYTES free-space threshold that is not a
canonical byte count between 64 MiB and 1 PiB (or a wrapper template that
lost the placeholder). It also refuses an output directory that already has files
or sits inside a Git checkout, because rendered files hold private host
values.

Output: run-records.sh (0700), records-run.service, records-run.timer and
targets/NAME.json (0600). Installing them into the private runtime and the
systemd user directory is a separate, owner-approved cutover step (plan T17).
Nothing here installs, enables or starts anything.
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.records import run_orchestrator  # noqa: E402

TEMPLATES = (("run-records.sh.in", "run-records.sh", 0o700),
             ("records-run.service.in", "records-run.service", 0o600),
             ("records-run.timer.in", "records-run.timer", 0o600))
PLACEHOLDER = re.compile(r"@([A-Z][A-Z0-9_]*)@")
SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:+=,-]+$")
ABSOLUTE = {"ENGINE_DIR", "DOCKER", "TIMEOUT", "FLOCK", "RUNTIME_DIR", "HOST_PYTHON",
            "NOTIFY_PY", "NOTIFY_ENV_FILE", "RUN_RECORDS_SH", "STUB_FIXTURE",
            # Container stage plans (targets/test-container.json.in,
            # targets/live-container.json.in and the Phase 2 OCR plans
            # targets/live-ocr-container.json.in and
            # targets/live-ocr-vision-container.json.in).
            # CONTAINER_OCR_VISION_CONFIG is the owner-only local-vision-config-v1
            # inside the container; only the live-ocr-vision plan uses (and so
            # requires) it.
            "CONTAINER_OCR_VISION_CONFIG",
            "CONTAINER_ENGINE_DIR", "CONTAINER_PROJECT_DIR", "CONTAINER_APPROVAL_FILE",
            "CONTAINER_PYTHON", "CONTAINER_SYNC_SCRIPT", "ADOPTION_CONFIG"}
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
RESERVED = {"TARGET_NAME"}
# Safety placeholders a template must keep: a wrapper template that dropped
# the free-space preflight would otherwise render cleanly without it.
REQUIRED_PLACEHOLDERS = {"run-records.sh.in": {"MIN_FREE_BYTES"}}
UNIT_TIMEOUT_SECONDS = 47 * 60
KILL_GRACE = 2
BACKSTOP_GRACE = 15
SAFETY_MARGIN = 30
TIMER_SLOTS = ["*-*-* 07:00:00 America/Los_Angeles",
               "*-*-* 13:00:00 America/Los_Angeles",
               "*-*-* 20:30:00 America/Los_Angeles"]


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
    if "RELEASE_COMMIT" in values and not re.fullmatch(r"[0-9a-f]{40}", values["RELEASE_COMMIT"]):
        raise RenderError("RELEASE_COMMIT: expected 40 lower-case hex characters")
    if "EXPECTED_IMAGE" in values and not re.fullmatch(r"sha256:[0-9a-f]{64}",
                                                       values["EXPECTED_IMAGE"]):
        raise RenderError("EXPECTED_IMAGE: expected sha256:<64 hex>")
    if "MIN_FREE_BYTES" in values:
        check_min_free_bytes(values["MIN_FREE_BYTES"])


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


def check_timer(text):
    slots = [line.split("=", 1)[1].strip() for line in text.splitlines()
             if line.startswith("OnCalendar=")]
    if slots != TIMER_SLOTS:
        raise RenderError(f"timer: expected OnCalendar slots {TIMER_SLOTS}, got {slots}")


def inside_git_checkout(path):
    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return True
    return False


def render(values, targets, out):
    check_values(values)
    out = Path(out).resolve()
    if inside_git_checkout(out):
        raise RenderError("out: refusing to write rendered private files inside a Git checkout")
    if out.exists() and any(out.iterdir()):
        raise RenderError("out: directory exists and is not empty")
    names = [name for name, _ in targets]
    if len(set(names)) != len(names):
        raise RenderError("targets: duplicate target name")
    for key in TARGET_NAMES:
        if key in values and values[key] not in names:
            raise RenderError(f"{key}: {values[key]} has no rendered stage plan (--target)")

    used = set()
    files = []
    for template, output, mode in TEMPLATES:
        text = (HERE / template).read_text(encoding="utf-8")
        found = set(PLACEHOLDER.findall(text))
        missing_required = sorted(REQUIRED_PLACEHOLDERS.get(template, set()) - found)
        if missing_required:
            raise RenderError(f"{template}: template lost required placeholder(s) "
                              f"{missing_required}")
        used.update(found)
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
    unknown = sorted(set(values) - used)
    if unknown:
        raise RenderError(f"values: unknown placeholder(s) {unknown}")
    check_budget(values)
    check_timer(next(text for output, text, _ in files if output == "records-run.timer"))

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
