"""Name a records-pipeline stage that has stopped producing, from run reports. Read-only and
stateless: no ledger, no clock (--now is required), no state file, no send. An alert exists exactly
while the last N scheduled reports satisfy the rule. Exit 3 alerts, 0 none, 1 untrusted, 2 usage."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from campaign_tool.records.run_report import load_schema, validate_run_report  # noqa: E402

STAGES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs")
MAX_SILENT_HOURS = 36  # frozen; no flag changes it
STUB_CODE, MAX_FILES, MAX_BYTES = "STUB_STAGE_BACKEND", 500, 1 << 20
STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
KIND_OF, TIE_ORDER = {"failed": "failing", "stuck": "stuck", "unknown": "skipped"}, ("failing", "stuck", "skipped")
ACTIONS = {
    "stuck": "stage had input and produced nothing; check its input queue and run logs",
    "failing": "stage failed in every considered run; see records status and tell the operator",
    "skipped": "stage did not really run (skipped or stub backend); check the stage backend",
    "no-recent-report": "no recent scheduled run report; check the run timer and records health",
    "latest-report-invalid": "newest run report fails validation; check the run report writer",
    "unparseable-report": "a run report file is not readable JSON; check the reports directory",
    "duplicate-run-id": "two different reports share one run id; both were excluded",
    "insufficient-history": "not enough scheduled runs to judge stages; this is not all clear",
    "invalid-report": "invalid reports were excluded from classification",
    "report-from-future": "reports dated after --now were excluded",
}


class Untrusted(Exception):  # the check itself cannot be trusted (exit 1)
    pass


def parse_stamp(text):
    if not isinstance(text, str) or not STAMP.fullmatch(text):
        raise ValueError("bad timestamp")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def read_reports(directory):  # [(bytes, parsed-or-None)]; a missing directory gives []
    directory = Path(directory)
    if directory.is_symlink():
        raise Untrusted("reports directory is a symlink")
    files = sorted(p for p in directory.iterdir() if p.name.endswith(".json")) if directory.is_dir() else []
    if len(files) > MAX_FILES:
        raise Untrusted("too many report files")
    loaded = []
    for path in files:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise Untrusted("report file is a symlink, not a regular file or too large")
        data = path.read_bytes()
        try:
            loaded.append((data, json.loads(data.decode("utf-8"))))
        except ValueError:  # includes UnicodeDecodeError
            loaded.append((data, None))
    return loaded


def classify(report, name):
    stage = next(s for s in report["stages"] if s["name"] == name)
    if stage["status"] == "fail":
        return "failed"
    if stage["status"] == "skipped" or any(i["code"] == STUB_CODE and i["stage"] == name for i in report["needs_g"]):
        return "unknown"
    if stage["counts"]["output"] > 0:
        return "produced"
    return "idle" if stage["counts"]["input"] == 0 else "stuck"


def order_key(report):  # (started_at, run_id); an undatable report sorts newest so it cannot hide
    try:
        started = parse_stamp(report.get("started_at"))
    except (AttributeError, ValueError):
        started = datetime.max.replace(tzinfo=timezone.utc)
    rid = report.get("run_id") if isinstance(report, dict) else None
    return started, rid if isinstance(rid, str) else ""


def check(loaded, now, runs):
    schema, groups = load_schema(), {}
    for data, parsed in loaded:
        if parsed is not None:
            rid = order_key(parsed)[1]
            groups.setdefault(rid or id(parsed), []).append((data, parsed))
    tally, scheduled, latest = Counter(unparseable=sum(1 for _, parsed in loaded if parsed is None)), [], None
    for members in groups.values():
        if len({data for data, _ in members}) > 1:
            tally["duplicate"] += 1
            continue
        report, key = members[0][1], order_key(members[0][1])
        try:
            ok = validate_run_report(report, schema) is not None
        except Exception:  # any validator failure makes the report invalid
            ok = False
        if ok and key[0] > now:
            tally["future"] += 1
            continue
        latest = max(latest or (key, ok), (key, ok), key=lambda item: item[0])
        if not ok:
            tally["invalid"] += 1
        elif report["mode"] in ("manual", "test"):
            tally[report["mode"]] += 1
        else:
            scheduled.append((key, report))
    scheduled, alerts, info = [report for _, report in sorted(scheduled, key=lambda item: item[0])], [], []

    def add(target, ident, stage="-", kind=None, last=None, **extra):
        target.append(dict(id=ident, source="stage-stale-check", stage=stage, kind=kind or ident,
                           runs_considered=runs, last_produced_run=last, **extra))

    if not scheduled or now - parse_stamp(scheduled[-1]["finished_at"]) > timedelta(hours=MAX_SILENT_HOURS):
        add(alerts, "no-recent-report")
    if latest is not None and not latest[1]:
        add(alerts, "latest-report-invalid")
    for name, ident, target in (("unparseable", "unparseable-report", alerts), ("duplicate", "duplicate-run-id", alerts),
                                ("invalid", "invalid-report", info), ("future", "report-from-future", info)):
        if tally[name]:
            add(target, ident, count=tally[name])
    if len(scheduled) < runs:
        add(info, "insufficient-history")
    else:
        window = scheduled[-runs:]
        for name in STAGES:
            kinds = [classify(r, name) for r in window]
            if any(k in ("produced", "idle") for k in kinds):
                continue
            counts = Counter(KIND_OF[k] for k in kinds)
            kind = max(TIE_ORDER, key=lambda k: (counts[k], -TIE_ORDER.index(k)))
            produced = [r["run_id"] for r in scheduled if classify(r, name) == "produced"]
            add(alerts, f"stale:{name}", stage=name, kind=kind, last=produced[-1] if produced else None,
                first_run=window[0]["run_id"], last_run=window[-1]["run_id"])
    return sorted(alerts, key=lambda a: a["id"]), sorted(info, key=lambda a: a["id"]), tally


def render(alerts, info, tally, runs):
    return [f"stale | {a['id']} | {a['stage']} | kind={a['kind']} | runs={runs} | "
            f"last_produced {a['last_produced_run'] or 'never'} | action: {ACTIONS[a['kind']]}"
            for a in alerts + info] + [
        f"stale | stages={len(STAGES)} alerts={len(alerts)} info={len(info)} runs_considered={runs} "
        f"excluded_manual={tally['manual']} excluded_test={tally['test']} invalid={tally['invalid']}"
        " | alerts are not a legal status; recorded counts only"]


def checked_out_path(text):
    path = Path(text)
    if not path.is_absolute() or os.path.lexists(path):
        raise Untrusted("--json-out must be a new absolute path")
    if any(part.is_symlink() for part in [path.parent, *path.parent.parents]) or not path.parent.is_dir():
        raise Untrusted("--json-out parent must exist and be link-free")
    if path.parent == REPO or REPO in path.parent.parents:
        raise Untrusted("--json-out must be outside the repository")
    return path


def write_json(path, payload):
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:  # never leave a partial file
        os.unlink(path)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="Alert when a records-pipeline stage stops producing.")
    cmd = parser.add_subparsers(dest="verb", required=True).add_parser("check", help="classify the last N scheduled reports")
    cmd.add_argument("--reports", required=True, help="directory of run report *.json files")
    cmd.add_argument("--now", required=True, help="UTC time YYYY-MM-DDTHH:MM:SSZ (the clock is never read)")
    cmd.add_argument("--runs", default="3", help="scheduled runs to consider, 2-20 (default 3)")
    cmd.add_argument("--json-out", help="new absolute file outside the repo for flock-alerts-v1 JSON")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[0-9]{1,2}", args.runs) or not 2 <= int(args.runs) <= 20:
        cmd.error("--runs must be an integer from 2 to 20")  # exit 2; one quiet run is never an alert
    try:
        now, runs = parse_stamp(args.now), int(args.runs)
    except ValueError:
        cmd.error("--now must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        out = checked_out_path(args.json_out) if args.json_out else None
        alerts, info, tally = check(read_reports(args.reports), now, runs)
        if out is not None:
            write_json(out, {"schema": "flock-alerts-v1", "review_required": True, "now": args.now,
                             "alerts": alerts, "info": info})
    except (Untrusted, OSError) as exc:
        print(f"stale | check not trusted: {exc if isinstance(exc, Untrusted) else 'file error'}", file=sys.stderr)
        return 1
    print("\n".join(render(alerts, info, tally, runs)))
    return 3 if alerts else 0


if __name__ == "__main__":
    sys.exit(main())
