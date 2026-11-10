"""DF1109B-04: meetings-status, the private read-only inventory of staged meetings and unstaged leads.

Synthetic data only (tempfile campaign folders and the fictional example finding); nothing is fetched, sent or written.
Run from the repository root: python3 -B -m unittest tests.test_meetings_status -v
"""
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meetings_status
from campaign_tool.meetings_status import inventory, render_text

REPO_ROOT = Path(__file__).resolve().parents[1]
FINDING = REPO_ROOT / "examples" / "fictional-campaign" / "content" / "findings" / "cedar-policy-posted.json"
TODAY = date(2026, 11, 9)
AS_OF = "2026-11-09"
LINK = "https://campaign.example.invalid/findings/cedar-policy-posted.html"
CURRENT_KIT = f"## Sources\n- **Cedar** {LINK} (corrected 2026-02-03; corrections: {LINK}#corrections)\n"
STALE_KIT = f"## Sources\n- **Cedar** {LINK}\n"
SECRETS = ("SECRET-HOW-REMOTE", "secret-clerk@example.invalid", "SECRET-VERIFIER-P", "SECRET-VERIFIER-O",
           "SECRET-VOTE-PROSE", "Secret Member", "Secret Speaker", "SECRET-KIT-PROSE")
SRC = "https://example.invalid/agenda"


def participation(written=True, verified_at="2026-11-05"):
    return {"in_person": True, "remote": {"available": True, "how": "SECRET-HOW-REMOTE"},
            "written_comment": {"how": "Email secret-clerk@example.invalid", "deadline": None} if written else None,
            "source_url": SRC, "verified_by": "SECRET-VERIFIER-P", "verified_at": verified_at}


def outcome(result="approved", on="2026-10-02", minutes=True, votes=True):
    value = {"result": result, "on": on, "source_url": SRC, "verified_by": "SECRET-VERIFIER-O", "verified_at": on}
    if minutes:
        value.update(minutes_url="https://example.invalid/minutes", minutes_approved_on=on)
    if votes == "members":
        value["votes"] = [{"name": "Secret Member", "vote": "yes"}]
    elif votes:
        value["vote"] = "SECRET-VOTE-PROSE 4-1"
    return value


def meeting(mid, starts, kit=CURRENT_KIT + "SECRET-KIT-PROSE\n", **extra):
    return {"id": mid, "body": f"Body {mid}", "starts_at": starts, "agenda_item": "Item 1", "comment_kit_md": kit,
            **extra}


STAGED = {
    "a-future": meeting("a-future", "2026-11-20T18:00:00-08:00"),
    "b-today": meeting("b-today", "2026-11-09T19:00:00-08:00", participation=participation()),
    "c-stale": meeting("c-stale", "2026-11-12T09:00:00+09:00", kit=STALE_KIT, participation=participation(False)),
    "d-cancelled": meeting("d-cancelled", "2026-11-15T18:00:00-08:00", status="cancelled"),
    "e-continued": meeting("e-continued", "2026-10-20T18:00:00-07:00",
                           outcome=outcome("continued", "2026-10-20", minutes=False, votes=False)),
    "f-past": meeting("f-past", "2026-10-01T18:00:00-07:00"),
    "g-done": meeting("g-done", "2026-10-02T18:00:00-07:00", outcome=outcome()),
    "h-novotes": meeting("h-novotes", "2026-10-02T18:00:00.500-07:00", outcome=outcome(votes=False)),
    "i-planned": meeting("i-planned", "2026-11-21T18:00:00-08:00", participation=participation(False)),
    "j-nokit": meeting("j-nokit", "2026-11-22T18:00:00-08:00", kit="", participation=participation(False)),
    "k-sent": meeting("k-sent", "2026-11-23T18:00:00-08:00", participation=participation()),
    "l-members": meeting("l-members", "2026-10-02T18:00:00-07:00", outcome=outcome(votes="members")),
}
SCAN = [{"id": "a-future", "body": "Body a", "starts_at": "2026-11-20T18:00:00-08:00", "status": "scheduled"},
        {"id": "scan-new", "body": "Scan <b>body</b>", "starts_at": "2026-11-25T18:00:00-08:00", "status": "scheduled",
         "agency_contact": "secret-clerk@example.invalid", "event_id": 5},
        {"id": "scan-cancel", "body": "Scan cancelled", "starts_at": "2026-11-24T18:00:00-08:00", "status": "cancelled"},
        {"id": "scan-past", "body": "Scan past", "starts_at": "2026-10-05T18:00:00-07:00"},
        "not an object"]
MANUAL = {"id": "manual-board", "body": "Manual board", "agency_id": "ca-cedar-police",
          "starts_at": "2026-11-30T10:00:00-08:00", "agenda_url": SRC, "agenda_item": "Item", "source_url": SRC,
          "checked_on": "2026-11-01", "source": "manual", "time_known": True, "status": "scheduled",
          "history": [{"action": "replace", "on": "2026-11-02", "reason": "SECRET-KIT-PROSE reason", "before": {
              "id": "manual-board", "body": "Manual board", "agency_id": "ca-cedar-police",
              "starts_at": "2026-11-29T10:00:00-08:00", "agenda_url": SRC, "agenda_item": "Item", "source_url": SRC,
              "checked_on": "2026-11-01", "source": "manual", "time_known": True, "status": "scheduled"}}]}

EXPECTED_ORDER = ["b-today", "c-stale", "d-cancelled", "a-future", "i-planned", "j-nokit", "k-sent", "e-continued",
                  "h-novotes", "g-done", "l-members", "f-past", "scan-cancel", "scan-new", "manual-board", "scan-past"]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")


def build(root):
    for mid, doc in STAGED.items():
        write(root / "content" / "meetings" / f"{mid}.json", doc)
    (root / "content" / "findings").mkdir()
    shutil.copy(FINDING, root / "content" / "findings" / FINDING.name)
    write(root / "kit" / "meetings.json", {"schema_version": 1, "meetings": SCAN})
    write(root / "kit" / "meetings-manual.json", {"schema_version": 1, "meetings": [MANUAL]})
    write(root / "kit" / "comments-sent.json", {"schema_version": 1, "comments": {
        "k-sent": {"on": "2026-11-08", "channel": "email", "recorded_at": "2026-11-08T10:00:00+00:00"},
        "b-other": {"on": "2026-11-08", "channel": "mail", "recorded_at": "2026-11-08T10:00:00+00:00"}}})
    for mid in ("i-planned", "g-done"):
        write(root / "kit" / "meetings" / "speakers" / f"{mid}.json", {
            "schema_version": 1, "meeting_id": mid, "kit_sha256": "ab" * 32,
            "assignments": [{"label": "Secret Speaker", "point": "ask:1"}]})
    write(root / "kit" / "meetings" / "speakers" / "k-sent.json", {
        "schema_version": 1, "meeting_id": "k-sent", "kit_sha256": "cd" * 32, "assignments": []})


def snapshot(root):
    found = {}
    for here, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(here, name)
            info = os.lstat(path)
            data = Path(path).read_bytes() if os.path.isfile(path) and not os.path.islink(path) else None
            found[os.path.relpath(path, root)] = (info.st_mode, data)
    return found


def cli(*argv, cwd=REPO_ROOT):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *argv], cwd=cwd, capture_output=True,
                          text=True, timeout=120)


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    return {k for v in value for k in keys(v)} if isinstance(value, list) else set()


class MeetingsStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-meetings-status-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "campaign"
        self.root.mkdir()
        build(self.root)

    def report(self, as_of=AS_OF):
        return inventory(self.root, as_of, directory="D", today=TODAY)

    def rows(self, report=None):
        return {row.get("id", row["path"]): row for row in (report or self.report())["rows"]}

    def test_every_staged_row_and_local_calendar_order(self):
        report = self.report()
        self.assertEqual([row.get("id", row["path"]) for row in report["rows"]], EXPECTED_ORDER + ["kit/meetings.json[4]"])
        self.assertEqual(report["schema_version"], 1)
        self.assertTrue(report["private"])
        rows = self.rows(report)
        self.assertEqual(rows["c-stale"]["starts_at"], "2026-11-12T09:00:00+09:00")  # offset kept, never converted
        self.assertEqual(rows["b-today"]["period"], "upcoming")  # today counts as upcoming
        self.assertEqual(rows["d-cancelled"]["status"], "cancelled")
        self.assertEqual(rows["e-continued"]["status"], "continued")
        self.assertEqual(rows["e-continued"]["outcome"], "continued")
        self.assertEqual(rows["scan-new"]["body"], "Scan bbody/b")  # meetings-stage's own _clean, as it would stage it
        self.assertEqual(render_text(report), render_text(self.report()))  # repeated byte-identical

    def test_state_projection_is_allowlisted(self):
        rows = self.rows()
        expect = {
            "b-today": ("verified", "current", "not_recorded", "absent"),
            "c-stale": ("verified", "stale", "not_recorded", "absent"),
            "a-future": ("not_verified", "current", "not_recorded", "absent"),
            "i-planned": ("verified", "current", "not_recorded", "present"),
            "j-nokit": ("verified", "missing", "not_recorded", "absent"),
            "k-sent": ("verified", "current", "recorded", "present"),  # a valid empty plan is present
            "g-done": ("not_verified", "current", "not_recorded", "present")}
        for mid, (part, kit, sent, speakers) in expect.items():
            row = rows[mid]
            self.assertEqual((row["participation"], row["comment_kit"], row["comment_sent"], row["speakers_plan"]),
                             (part, kit, sent, speakers), mid)
        self.assertEqual([rows[m][k] for m in ("g-done", "l-members", "h-novotes") for k in ("outcome", "minutes", "votes")],
                         ["approved", "recorded", "recorded"] * 2 + ["approved", "recorded", "not_recorded"])
        report = self.report()
        text, blob = render_text(report), json.dumps(report, ensure_ascii=False)
        for secret in SECRETS:
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, blob)
        self.assertEqual(keys(report) - {"schema_version", "as_of", "private", "rows"},
                         {"kind", "path", "id", "body", "starts_at", "period", "status", "participation", "comment_kit",
                          "comment_sent", "speakers_plan", "outcome", "minutes", "votes", "next_step", "command", "error"})
        self.assertEqual(text.splitlines()[0],
                         "staged | b-today | Body b-today | 2026-11-09T19:00:00-08:00 | scheduled | participation=verified"
                         " | kit=current | sent=not_recorded | speakers=absent | outcome=not_recorded | minutes=not_recorded"
                         " | votes=not_recorded | next=python3 -m campaign_tool meetings-comment-sent --directory D --id "
                         "b-today --on YYYY-MM-DD --channel CHANNEL | private; review-required")

    def test_missing_step_priority_and_safe_templates(self):
        rows = self.rows()
        steps = {mid: rows[mid]["next_step"] for mid in EXPECTED_ORDER}
        self.assertEqual(steps, {
            "b-today": "record_comment_sent", "c-stale": "restage_kit", "d-cancelled": None,
            "a-future": "verify_participation", "i-planned": None, "j-nokit": "restage_kit", "k-sent": None,
            "e-continued": "record_minutes", "h-novotes": "review_optional_votes", "g-done": None, "l-members": None,
            "f-past": "record_outcome", "scan-cancel": None, "scan-new": "stage_meeting", "manual-board": "stage_meeting",
            "scan-past": "stage_meeting"})
        self.assertIsNone(rows["d-cancelled"]["command"])
        self.assertEqual(rows["h-novotes"]["command"], "python3 -m campaign_tool meetings-outcome --help")
        self.assertNotIn("--replace", rows["e-continued"]["command"])  # minutes confirm, never replace an outcome
        # a past meeting never asks for a retroactive comment or speakers
        self.assertEqual(rows["f-past"]["command"], "python3 -m campaign_tool meetings-outcome --directory D --id f-past "
                         "--result RESULT --on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME")
        quoted = inventory(self.root, AS_OF, directory="/tmp/it's a dir", today=TODAY)
        command = self.rows(quoted)["scan-new"]["command"]
        self.assertEqual(shlex.split(command)[-4:], ["--directory", "/tmp/it's a dir", "--id", "scan-new"])
        from tests.test_findings_guide_doc import parse_only
        fill = {"CHANNEL": "email", "FILE": "p.json"}
        for row in self.report()["rows"]:
            if row["command"] and "--help" not in row["command"]:
                parse_only("campaign_tool", [fill.get(t, t) for t in shlex.split(row["command"])[3:]])

    def test_continued_meeting_follows_outcome_ladder_in_any_period(self):
        meetings = self.root / "content" / "meetings"
        write(meetings / "m-today.json", meeting("m-today", "2026-11-09T18:00:00-08:00",
                                                 outcome=outcome("continued", "2026-11-09", minutes=False, votes=False),
                                                 participation=participation()))
        rows = self.rows()
        self.assertEqual((rows["m-today"]["period"], rows["m-today"]["status"]), ("upcoming", "continued"))
        self.assertEqual(rows["m-today"]["next_step"], "record_minutes")
        self.assertNotIn("--participation", rows["m-today"]["command"])
        self.assertNotIn("--replace", rows["m-today"]["command"])
        early = self.rows(self.report("2026-09-01"))  # an earlier as-of regroups the past continued meeting
        self.assertEqual((early["e-continued"]["period"], early["e-continued"]["status"]), ("upcoming", "continued"))
        self.assertEqual(early["e-continued"]["next_step"], "record_minutes")
        self.assertNotIn("--participation", early["e-continued"]["command"])
        self.assertEqual(early["a-future"]["next_step"], "verify_participation")  # scheduled upcoming rows unchanged

    def test_duplicate_effective_ids_error_every_affected_file(self):
        meetings = self.root / "content" / "meetings"
        write(meetings / "alpha.json", meeting("alpha", "2026-11-26T10:00:00-08:00"))
        write(meetings / "beta.json", meeting("alpha", "2026-11-27T10:00:00-08:00"))
        write(meetings / "gamma.json", meeting("gamma", "2026-11-28T10:00:00-08:00"))
        noid = meeting("x", "2026-11-29T10:00:00-08:00")
        del noid["id"]
        write(meetings / "delta.json", noid)  # no id: the file name delta is its effective id
        write(meetings / "epsilon.json", meeting("delta", "2026-11-30T10:00:00-08:00"))
        report = self.report()
        rows = self.rows(report)
        bad = [r["path"] for r in report["rows"] if r["kind"] == "unreadable"]
        for name in ("alpha", "beta", "delta", "epsilon"):
            self.assertIn(f"content/meetings/{name}.json", bad)
        self.assertEqual(len(bad), len(set(bad)))
        staged = [r for r in report["rows"] if r["kind"] == "staged"]
        self.assertFalse({"alpha", "beta", "delta", "epsilon"} & {r["id"] for r in staged})
        for r in report["rows"]:
            if r["kind"] != "unreadable":
                self.assertFalse(r.get("command") and ("alpha" in r["command"] or "delta" in r["command"]))
        self.assertEqual(rows["gamma"]["kind"], "staged")  # a unique neighbor stays
        self.assertIsNotNone(rows["gamma"]["command"])
        self.assertNotIn("alpha", rows)

    def test_broken_staged_file_never_hides_neighbors(self):
        meetings = self.root / "content" / "meetings"
        (meetings / "0-bad-utf8.json").write_bytes(b"\xff\xfe{")
        write(meetings / "m-bad-json.json", "{")
        write(meetings / "m-bad-schema.json", {"id": "m-bad-schema", "body": "x"})
        write(meetings / "m-mismatch.json", meeting("scan-new", "2026-11-26T10:00:00-08:00"))
        write(meetings / "z-empty.json", "")
        (meetings / "y-big.json").write_bytes(b" " * (2 * 1024 * 1024 + 1))
        doc = meeting("x", "2026-11-27T10:00:00-08:00")
        del doc["id"]
        write(meetings / "n-noid.json", doc)  # no id: the file name is the id
        report = self.report()
        rows = report["rows"]
        bad = [row["path"] for row in rows if row["kind"] == "unreadable"]
        self.assertEqual(bad, ["content/meetings/0-bad-utf8.json", "content/meetings/m-bad-json.json",
                               "content/meetings/m-bad-schema.json", "content/meetings/m-mismatch.json",
                               "content/meetings/y-big.json", "content/meetings/z-empty.json",
                               "kit/meetings.json[4]"])
        ids = {row["id"] for row in rows if row["kind"] != "unreadable"}
        self.assertEqual(ids, set(EXPECTED_ORDER) - {"scan-new"} | {"n-noid"})  # scan-new's id is used by a staged file
        self.assertEqual([row["kind"] for row in rows[13:19]], ["unreadable"] * 6)  # right after the 13 staged rows
        for row in rows:
            if row["kind"] == "unreadable":
                self.assertEqual(set(row), {"kind", "path", "error", "next_step", "command"})
        self.assertIn("unreadable | content/meetings/m-bad-json.json | review input locally | private; review-required",
                      render_text(report))

    def test_auxiliary_faults_are_unknown_not_absent(self):
        write(self.root / "kit" / "comments-sent.json", "{")
        write(self.root / "kit" / "meetings" / "speakers" / "i-planned.json", "[]")
        write(self.root / "content" / "findings" / "broken.json", {"id": "x"})
        rows = self.rows()
        self.assertEqual({rows[m]["comment_sent"] for m in STAGED}, {"unknown"})
        self.assertEqual(rows["i-planned"]["speakers_plan"], "unknown")
        self.assertEqual(rows["g-done"]["speakers_plan"], "present")
        self.assertEqual(rows["c-stale"]["comment_kit"], "stale")  # a proven stale hit stays stale
        self.assertEqual(rows["a-future"]["comment_kit"], "unknown")  # never falsely current
        self.assertEqual(rows["j-nokit"]["comment_kit"], "missing")
        self.assertEqual(rows["b-today"]["next_step"], "repair_input")
        self.assertIsNone(rows["b-today"]["command"])
        errors = [r["path"] for r in self.report()["rows"] if r["kind"] == "unreadable"]
        self.assertEqual(errors, ["content/findings/broken.json", "kit/comments-sent.json", "kit/meetings.json[4]",
                                  "kit/meetings/speakers/i-planned.json"])
        with mock.patch.object(meetings_status, "read_plan", side_effect=PermissionError("SECRET-KIT-PROSE")):
            report = self.report()
        self.assertEqual({r["speakers_plan"] for r in report["rows"] if r["kind"] == "staged"}, {"unknown"})
        self.assertNotIn("SECRET-KIT-PROSE", json.dumps(report) + render_text(report))

    def test_scan_and_manual_isolation(self):
        rows = self.rows()
        self.assertEqual(rows["kit/meetings.json[4]"]["kind"], "unreadable")
        self.assertNotIn("a-future", [r["id"] for r in self.report()["rows"] if r["kind"] == "unstaged"])
        self.assertEqual(rows["scan-new"]["path"], "kit/meetings.json")
        self.assertEqual(rows["manual-board"]["path"], "kit/meetings-manual.json")
        write(self.root / "kit" / "meetings.json", {"schema_version": 2, "meetings": []})
        rows = self.rows()
        self.assertIn("kit/meetings.json", rows)
        self.assertIn("manual-board", rows)  # a broken scan never hides manual entries
        write(self.root / "kit" / "meetings.json", {"schema_version": 1, "meetings": SCAN[1:3] + [SCAN[1]]})
        write(self.root / "kit" / "meetings-manual.json", {"schema_version": 1, "meetings": [MANUAL, {"id": "bad"}]})
        rows = self.rows()
        self.assertIn("scan-cancel", rows)
        self.assertNotIn("scan-new", rows)  # listed twice: errors, never an arbitrary pick
        self.assertIn("kit/meetings.json[0]", rows)
        self.assertIn("kit/meetings.json[2]", rows)
        self.assertIn("kit/meetings-manual.json[1]", rows)
        self.assertIn("manual-board", rows)
        for name in ("meetings.json", "meetings-manual.json"):
            (self.root / "kit" / name).unlink()
        self.assertNotIn("unstaged", {r["kind"] for r in self.report()["rows"]})

    def test_symlink_and_wrong_type_inputs_not_followed(self):
        foreign = Path(self.tmp.name) / "foreign"
        foreign.mkdir()
        os.mkfifo(foreign / "sentinel.json")  # opening it would hang the test
        os.symlink(foreign / "sentinel.json", self.root / "content" / "meetings" / "zz-link.json")
        os.mkfifo(self.root / "content" / "meetings" / "zz-fifo.json")
        (self.root / "kit" / "comments-sent.json").unlink()
        os.mkfifo(self.root / "kit" / "comments-sent.json")
        (self.root / "kit" / "meetings.json").unlink()
        os.symlink(foreign / "sentinel.json", self.root / "kit" / "meetings.json")
        rows = self.rows()
        for path in ("content/meetings/zz-link.json", "content/meetings/zz-fifo.json", "kit/comments-sent.json",
                     "kit/meetings.json"):
            self.assertEqual(rows[path]["kind"], "unreadable", path)
        self.assertEqual(rows["a-future"]["comment_kit"], "unknown")  # the helper is skipped, never trusted
        self.assertIn("manual-board", rows)
        shutil.rmtree(self.root / "kit")
        os.symlink(foreign, self.root / "kit")
        rows = self.rows()
        self.assertEqual(rows["kit"]["kind"], "unreadable")
        self.assertEqual(rows["b-today"]["speakers_plan"], "unknown")
        shutil.rmtree(self.root / "content")
        os.symlink(foreign, self.root / "content")
        rows = self.rows()
        self.assertEqual(rows["content"]["kind"], "unreadable")  # a refused folder is a row, never an empty success
        link = Path(self.tmp.name) / "root-link"
        os.symlink(self.root, link)
        with self.assertRaises(meetings_status.MeetingsStatusError):
            inventory(link, AS_OF, today=TODAY)

    def test_as_of_is_not_a_history_reconstruction(self):
        early = self.rows(self.report("2026-09-01"))
        self.assertEqual(early["g-done"]["outcome"], "approved")  # recorded later, still shown
        self.assertEqual(early["k-sent"]["comment_sent"], "recorded")
        self.assertEqual(early["f-past"]["period"], "upcoming")
        boundary = self.rows(self.report("2026-11-10"))
        self.assertEqual(boundary["b-today"]["period"], "past")
        self.assertEqual(boundary["b-today"]["next_step"], "record_outcome")
        for bad in ("2026-02-30", "2026-1-05", "20261105"):
            result = cli("meetings-status", "--directory", "/nonexistent-synthetic", "--as-of", bad)
            self.assertEqual((result.returncode, result.stdout), (1, ""))
            self.assertEqual(result.stderr, "Stopped: --as-of must be a real YYYY-MM-DD date\n")
        missing = cli("meetings-status", "--directory", str(Path(self.tmp.name) / "absent"))
        self.assertEqual((missing.returncode, missing.stdout), (1, ""))
        self.assertIn("campaign directory does not exist", missing.stderr)
        self.assertEqual(cli("meetings-status", "--directory", str(self.root), "--typo").returncode, 2)
        self.assertEqual(cli("meetings-status").returncode, 2)
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        result = cli("meetings-status", "--directory", str(empty))
        self.assertEqual((result.returncode, result.stdout),
                         (0, "No staged meetings or unstaged matches. | private; review-required\n"))
        result = cli("meetings-status", "--directory", str(empty), "--json", "--as-of", AS_OF)
        self.assertEqual(json.loads(result.stdout), {"schema_version": 1, "as_of": AS_OF, "private": True, "rows": []})

    def test_read_only_and_legacy_default_bytes(self):
        before = snapshot(self.tmp.name)
        text = cli("meetings-status", "--directory", str(self.root), "--as-of", AS_OF)
        self.assertEqual(text.returncode, 1, text.stderr)  # kit/meetings.json[4] is unreadable
        self.assertEqual(text.stdout, render_text(inventory(self.root, AS_OF, directory=str(self.root))))
        as_json = cli("meetings-status", "--directory", str(self.root), "--as-of", AS_OF, "--json")
        self.assertEqual(as_json.stdout, json.dumps(inventory(self.root, AS_OF, directory=str(self.root)), indent=2,
                                                    ensure_ascii=False) + "\n")
        self.assertEqual(as_json.stderr, "")
        self.assertEqual(snapshot(self.tmp.name), before)
        help_text = cli("meetings-status", "--help").stdout
        for flag in ("--directory", "--as-of", "--json"):
            self.assertIn(flag, help_text)
        self.assertNotIn("meetings-status", cli("meetings-stage", "--help").stdout)
        source = (REPO_ROOT / "campaign_tool" / "meetings_status.py").read_text(encoding="utf-8")
        for writer in ("write_text", "write_bytes", "mkdir", "chmod", "_write_json", "O_CREAT", "unlink", "rename"):
            self.assertNotIn(writer, source)

    def test_inventory_docs_and_command_examples(self):
        handover = (REPO_ROOT / "docs" / "HANDOVER.md").read_text(encoding="utf-8")
        guide = (REPO_ROOT / "docs" / "MEETINGS-GUIDE.md").read_text(encoding="utf-8")
        self.assertIn("python3 -m campaign_tool meetings-status --directory NEW_EMPTY_DIR\n", handover)
        self.assertIn("python3 -m campaign_tool meetings-status --directory NEW_EMPTY_DIR --json\n", handover)
        self.assertNotIn("not a full meeting inventory", handover)
        self.assertIn("MEETINGS-GUIDE.md#meeting-inventory-meetings-status", handover)
        self.assertIn("## Meeting inventory (meetings-status)", guide)
        for heading in ("## 7. Submit by hand, then record it", "## 8. Record the outcome, votes and recording"):
            self.assertIn(heading, guide)
        for text in (handover, guide):
            self.assertIn("not proof", text)


if __name__ == "__main__":
    unittest.main()
