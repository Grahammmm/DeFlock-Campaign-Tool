"""One phone-sized markdown response sheet per agency (DFAGB-08).

Read-only projection of an existing run's response-items.json through item_tiers.
Prints only identifiers, tier, raw rating token, display state and diagnostic flags;
never ask text, response text, evidence or exemption wording. A machine reading,
review required; not a legal finding. Nothing is sent, published or approved.
"""
import argparse
import contextlib
import os
import re
import sys
from pathlib import Path

from . import item_tiers, playbook
from .outcomes import response_items

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
FLAG = re.compile(r"[a-z][a-z0-9_]*(:[a-z0-9_+]+)?")
NO_DATA = "unknown: no run data"
FOOTER = (
    "exemption cited means a citation appears in the text read; it is not a withholding and not a validity finding",
    "not addressed means not mentioned in the text read; it is not a duty finding",
    "unclear and text_unreviewed asks need a person to read the response",
)


def _flags(values):
    values = values if isinstance(values, list) else []
    return ", ".join(v if isinstance(v, str) and FLAG.fullmatch(v) else "other_flag" for v in values) or "none"


def _raw(ask):
    if "rating" not in ask:
        return "absent"
    value = ask["rating"]
    if value is None:
        return "null"
    return value if isinstance(value, str) else "invalid:" + type(value).__name__


def _state(ask):
    rating = ask.get("rating")
    return item_tiers.state_of(rating) if isinstance(rating, str) else item_tiers.NOT_RATED


def build_sheet(report, agency, book, run_id):
    lines = ["# Response sheet: %s" % agency, "machine reading, review required; not a legal finding",
             "run: %s" % (run_id or "unknown")]
    requests = []
    if isinstance(report, dict) and report.get("schema") == item_tiers.SCHEMA_IN:
        requests = [r for r in report.get("requests") or [] if isinstance(r, dict) and r.get("agency_key") == agency]
    if not requests:
        return "\n".join(lines + ["", NO_DATA]) + "\n"
    for req in requests:
        lines += ["", "## %s" % (req.get("request_id") if isinstance(req.get("request_id"), (str, int)) else "unknown")]
        if req.get("flags"):
            lines.append("request flags: " + _flags(req["flags"]))
        rows = []
        for pos, ask in enumerate(a for a in req.get("asks") or [] if isinstance(a, dict)):
            text = ask.get("text") if isinstance(ask.get("text"), str) else ""
            tier = playbook.tier_for(book, text)
            number = ask.get("ask_id") if isinstance(ask.get("ask_id"), (str, int)) else pos + 1
            rows.append(((tier if tier is not None else 99, pos), "- ask %s | tier %s | raw %s | %s | flags: %s" % (
                number, tier if tier is not None else "-", _raw(ask), _state(ask), _flags(ask.get("flags")))))
        lines += [row for _, row in sorted(rows, key=lambda r: r[0])]
    return "\n".join(lines + [""] + list(FOOTER)) + "\n"


def _pick_run(project, run_id):
    base = project / "run-outputs"
    if base.is_symlink():
        raise ValueError("unsafe run directory")
    if run_id:
        if not NAME.fullmatch(run_id):
            return None
        if (base / run_id).is_symlink() or (base / run_id / "outcomes").is_symlink():
            raise ValueError("unsafe run directory")
        return run_id if (base / run_id / "outcomes" / "response-items.json").is_file() else None
    found = []
    if base.is_dir() and not base.is_symlink():
        for entry in base.iterdir():
            if NAME.fullmatch(entry.name) and entry.is_dir() and not entry.is_symlink() \
                    and (entry / "outcomes" / "response-items.json").is_file():
                found.append((entry.stat().st_mtime, entry.name))
    return max(found)[1] if found else None


def _refuse(code):
    print("records agency-response-sheet: refused " + code, file=sys.stderr)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="records agency-response-sheet", description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--agency", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--run-id")
    args = parser.parse_args(argv)
    project, out = Path(args.project).resolve(), Path(args.out).absolute()
    if out.parent.resolve() == project or project in out.parent.resolve().parents:
        return _refuse("response_sheet_out_in_project")
    if out.exists() or out.is_symlink():
        return _refuse("response_sheet_out_exists")
    report = None
    try:
        run_id = _pick_run(project, args.run_id)
        if run_id:
            with contextlib.ExitStack() as stack:
                directory = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, directory)
                for part in ("run-outputs", run_id, "outcomes"):
                    directory = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    stack.callback(os.close, directory)
                report = response_items._read_json("response-items.json", dir_fd=directory)
            if not isinstance(report, dict) or report.get("schema") != item_tiers.SCHEMA_IN:
                return _refuse("response_sheet_input")
            requests = report.get("requests")
            if not isinstance(requests, list) or any(
                not isinstance(r, dict) or not isinstance(r.get("asks"), list) for r in requests
            ):
                return _refuse("response_sheet_input")
        sheet = build_sheet(report, args.agency, playbook.load(), run_id)
    except (OSError, ValueError, playbook.PlaybookError):
        return _refuse("response_sheet_input")
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        return _refuse("response_sheet_out_exists")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(sheet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
