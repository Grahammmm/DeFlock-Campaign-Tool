"""Write the private per-agency timeline report (JSON and plain-text table).

Usage: agency_timeline.py --ledger ABSOLUTE_LEDGER --output-dir NEW_ABSOLUTE_DIR

The ledger is read once through the existing read-only correspondence reader.
The output directory must be absolute, must not exist and its parent must be
an existing, link-free directory; it is created owner-only only after the
report has been built, and files are created exclusively (never overwritten).
Stdout carries one status line without paths or source content. Exit 0 on
success, 2 on any refusal. Output is PRIVATE and REVIEW REQUIRED.
"""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from campaign_tool.records.understanding import agency_timeline  # noqa: E402

JSON_NAME = "agency-timeline.json"
TABLE_NAME = "agency-timeline.txt"


class Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused("usage_invalid")


def _linked(path):
    return any(candidate.is_symlink() for candidate in (path, *path.parents))


def checked_args(argv):
    parser = _Parser(add_help=False)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    ledger, output = Path(args.ledger), Path(args.output_dir)
    if not ledger.is_absolute() or not output.is_absolute():
        raise Refused("absolute_paths_required")
    if Path(os.path.normpath(output)) != output or Path(os.path.normpath(ledger)) != ledger:
        raise Refused("normalized_paths_required")
    if os.path.lexists(output):
        raise Refused("output_dir_exists")
    if _linked(output.parent) or not output.parent.is_dir():
        raise Refused("output_parent_unavailable")
    if output == ledger.parent or output in ledger.parents:
        raise Refused("output_dir_overlaps_ledger")
    return ledger, output


def write_outputs(output, result):
    body = json.dumps(result, sort_keys=True, indent=2, ensure_ascii=True) + "\n"
    table = agency_timeline.render_table(result)
    output.mkdir(mode=0o700)
    for name, text in ((JSON_NAME, body), (TABLE_NAME, table)):
        data = text.encode("utf-8")
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)


def main(argv=None):
    try:
        ledger, output = checked_args(sys.argv[1:] if argv is None else argv)
        result = agency_timeline.report(ledger)
        write_outputs(output, result)
    except Refused as error:
        print(json.dumps({"schema": agency_timeline.SCHEMA, "status": "refused", "error": str(error)}))
        return 2
    except agency_timeline.TimelineError as error:
        print(json.dumps({"schema": agency_timeline.SCHEMA, "status": "refused", "error": str(error)}))
        return 2
    except OSError:
        print(json.dumps({"schema": agency_timeline.SCHEMA, "status": "refused", "error": "output_write_failed"}))
        return 2
    print(json.dumps({"schema": agency_timeline.SCHEMA, "status": "review_required",
                      "snapshot_sha256": result["snapshot_sha256"],
                      "agency_rows": result["totals"]["agency_rows"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
