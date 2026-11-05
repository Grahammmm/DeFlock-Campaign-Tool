"""Builds one synthetic campaign and collects every receipt-sensitive output (status, due, follow-up, calendar, today,
timing). Uses only commands and functions that existed before DF1407-01, so it runs unchanged on the pre-change main
that produced legacy-output.json:  python3 tests/fixtures/kit_received/legacy_capture.py OUT.json

The fixture stores what that run printed; tests compare receipt-less output with it byte for byte.
"""
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_calendar, kit_timing, kit_track, law, today  # noqa: E402
from datetime import date, datetime, timezone  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

AS_OF = "2026-03-25"
NOW = datetime(2026, 3, 25, 12, 0, tzinfo=timezone.utc)
STAMP = "2026-03-01T00:00:00+00:00"


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def must(*arguments):
    code, _, err = run(*arguments)
    assert code == 0, (arguments, err)


def build(root):
    """Init + kit + ready drafts; three main requests and three imported named requests with varied steps."""
    must("init", "--directory", root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
    must("kit", "--directory", root)
    ready_drafts(root)
    a, b, c = (p.stem for p in sorted((Path(root) / "kit" / "requests").glob("*.md"))[:3])
    for agency, sent in ((a, "2026-03-10"), (b, "2026-03-05"), (c, "2026-03-02")):
        must("kit-sent", "--directory", root, "--agency", agency, "--date", sent, "--channel", "email")
    path = Path(root) / "kit" / "sent.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for agency, name, sent in ((a, "ans", "2026-03-01"), (b, "roll", "2026-03-02"), (c, "dl", "2026-03-03")):
        document["requests"][kit_track._key(agency, name)] = {
            "sent_on": sent, "channel": "email", "imported": True, "recorded_at": STAMP, "steps": []}
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")

    def step(agency, request, on, kind, *extra):
        must("kit-response", "--directory", root, "--agency", agency, "--request", request, "--date", on,
             "--kind", kind, *extra)
    step(b, "main", "2026-03-12", "extension")
    step(c, "main", "2026-03-04", "acknowledged")
    step(c, "main", "2026-03-08", "clarification_requested")
    step(a, "ans", "2026-03-05", "determination", "--expected-on", "2026-03-20")
    step(a, "ans", "2026-03-06", "fee_estimate", "--amount", "50.00")
    step(b, "roll", "2026-03-08", "produced", "--partial", "--expected-on", "2026-03-20",
         "--download-expires-on", "2026-03-30")
    step(c, "dl", "2026-03-10", "produced", "--download-expires-on", "2026-03-22")
    return a, b, c


def collect(root, agencies):
    a, b, c = agencies
    root = Path(root)
    empty = root.parent / "no-packages"
    empty.mkdir(exist_ok=True)
    day = date.fromisoformat(AS_OF)
    out = {"status": kit_track.status(root, today=day), "status_no_law": kit_track.status(root, today=day, law_base=empty),
           "due": kit_track.due(root, AS_OF, today=day), "due_no_law": kit_track.due(root, AS_OF, today=day, law_base=empty)}
    out["cli_status_ans"] = run("kit-status", "--directory", root, "--request", "ans")
    out["cli_due"] = run("kit-due", "--directory", root, "--as-of", AS_OF)
    out["followup_result"] = kit_track.followup(root, a, AS_OF, today=day)
    out["followup_draft"] = (root / out["followup_result"]["written"]).read_text(encoding="utf-8")
    ics = root.parent / "dates.ics"
    kit_calendar.calendar(root, ics, AS_OF, now=NOW, today=day)
    out["calendar"] = ics.read_bytes().decode("utf-8")
    empty_ics = root.parent / "dates-no-law.ics"
    kit_calendar.calendar(root, empty_ics, AS_OF, now=NOW, today=day, law_base=empty)
    out["calendar_no_law"] = empty_ics.read_bytes().decode("utf-8")
    out["today_text"] = today.run_cli(root, AS_OF)
    out["today_json"] = today.run_cli(root, AS_OF, as_json=True)
    out["timing"] = kit_timing.format_report(kit_timing.report(root, AS_OF))
    text = json.dumps(out, indent=2, sort_keys=True)
    return json.loads(text.replace(json.dumps(str(root))[1:-1], "<ROOT>"))


def main():
    with tempfile.TemporaryDirectory(prefix="synthetic-legacy-") as tmp:
        root = Path(tmp) / "campaign"
        agencies = build(root)
        Path(sys.argv[1]).write_text(json.dumps(collect(root, agencies), indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")


if __name__ == "__main__":
    main()
