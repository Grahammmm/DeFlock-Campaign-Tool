"""DF1103-09: meetings-add keeps organizer-entered meetings in kit/meetings-manual.json (synthetic, offline)."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import meetings_manual as mm
from campaign_tool.content import load_content
from campaign_tool.meeting_stage import MeetingStageError, stage
from campaign_tool.meetings_manual import ManualMeetingError, cancel_manual, read_manual, read_meetings, record_manual

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legistar-events.json"
TODAY = date(2026, 11, 10)
MID = "manual-smallville-water"
BASE = dict(body="Smallville Water Board", agency_id="ca-cedar-police", starts_at="2026-11-20T18:30:00-08:00",
            agenda_url="https://example.invalid/agenda", agenda_item="Item 5: ALPR renewal",
            source_url="https://example.invalid/page", checked_on="2026-11-09")
REASON = "Secret organizer reason"


def cli(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=60)


class ManualBase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meetings-manual-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.manual = self.root / "kit" / "meetings-manual.json"
        self.scan = self.root / "kit" / "meetings.json"

    def add(self, meeting_id=MID, **over):
        return record_manual(self.root, meeting_id, **{**BASE, **over}, today=TODAY)

    def replace(self, on="2026-11-10", reason=REASON, **over):
        return record_manual(self.root, MID, **{**BASE, **over}, replace=True, on=on, reason=reason, today=TODAY)

    def refused(self, call, needle=None):
        before = self.manual.read_bytes() if self.manual.exists() else None
        with self.assertRaises(ManualMeetingError) as caught:
            call()
        if needle:
            self.assertIn(needle, str(caught.exception))
        self.assertEqual(self.manual.read_bytes() if self.manual.exists() else None, before)
        return str(caught.exception)

    def write_scan(self, meetings, raw=None):
        self.scan.parent.mkdir(exist_ok=True)
        self.scan.write_bytes(raw if raw is not None else json.dumps({"schema_version": 1, "meetings": meetings}).encode())

    def doc(self):
        return json.loads(self.manual.read_text(encoding="utf-8"))


class ManualMeetingTests(ManualBase):
    def test_manual_document_matches_read_manual(self):
        self.add(starts_at="2026-11-20T18:30:15.5Z")
        self.replace()
        cancel_manual(self.root, MID, on="2026-11-10", reason=REASON, today=TODAY)
        document = self.doc()
        before = self.manual.read_bytes()
        self.assertEqual(mm._manual_document(document), read_manual(self.root))
        self.assertEqual(self.manual.read_bytes(), before)
        entry = document["meetings"][0]
        invalid = [None, [], {**document, "schema_version": True},
                   {**document, "schema_version": 2}, {**document, "extra": 1},
                   {**document, "meetings": {}}, {**document, "meetings": [entry, entry]}]
        for changes in ({"status": "gone"}, {"history": "x"}, {"extra": 1},
                        {"history": [{"action": "cancel", "on": "bad", "reason": REASON}]},
                        {"body": "<invalid>"}, {"time_known": False}):
            invalid.append({**document, "meetings": [{**entry, **changes}]})
        for bad in invalid:
            with self.subTest(document=bad):
                self.manual.write_text(json.dumps(bad))
                with self.assertRaises(ManualMeetingError) as pure:
                    mm._manual_document(bad)
                with self.assertRaises(ManualMeetingError) as reader:
                    read_manual(self.root)
                self.assertEqual(str(pure.exception), str(reader.exception))

    def test_add_manual_only_stage_and_marker(self):
        result = self.add()
        self.assertEqual(result, {"id": MID, "status": "scheduled", "path": "kit/meetings-manual.json"})
        self.assertFalse(self.scan.exists())
        entry = self.doc()["meetings"][0]
        self.assertEqual((entry["source"], entry["time_known"], entry["status"], entry["history"]), ("manual", True, "scheduled", []))
        report = stage(self.root, MID)
        self.assertEqual(report["note"], "Organizer-entered meeting; official agenda checked 2026-11-09.")
        self.assertEqual(report["findings_cited"], 1)
        staged = json.loads((self.root / "content" / "meetings" / f"{MID}.json").read_text(encoding="utf-8"))
        self.assertEqual(staged["agency_id"], "ca-cedar-police")  # defaulted from the saved entry
        self.assertIn("Organizer-entered meeting; official agenda checked 2026-11-09.", staged["comment_kit_md"])
        text = json.dumps(staged)
        for private in ("source_url", "checked_on", "history", "time_known", "example.invalid/page"):
            self.assertNotIn(private, text)
        self.assertEqual(staged["cited_findings"], report["findings"])
        self.assertTrue(any(m["id"] == MID for m in load_content(self.root)["meetings"]))
        # a conflicting agency refuses; --finding order and an unmarked scan kit are unchanged
        self.assertRaises(MeetingStageError, stage, self.root, MID, agency_id="ca-other-police", replace=True)
        stage(self.root, MID, agency_id="ca-cedar-police", replace=True, finding_ids=["fnd_0000000000000001"])

    def test_scanned_kit_bytes_and_note_unchanged(self):
        self.write_scan([{"id": "legistar-x-1", "body": "B", "starts_at": "2026-10-14T18:00:00-07:00", "agenda_item": "A"}])
        report = stage(self.root, "legistar-x-1")
        self.assertIn("keyword lead from Legistar", report["note"])
        kit = json.loads((self.root / "content" / "meetings" / "legistar-x-1.json").read_text())["comment_kit_md"]
        self.assertNotIn("Organizer-entered", kit)

    def test_rescan_never_removes_manual(self):
        self.add()
        first = self.manual.read_bytes()
        for n in range(2):  # two recorded rescans of another body; the second compares against the first
            run = cli("meetings", "--directory", str(self.root), "--client", "examplecity", "--from-json", str(FIXTURE),
                      "--since", "2026-09-30")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(self.manual.read_bytes(), first)
        changes = json.loads(self.scan.read_text())["changes"]
        self.assertNotIn(MID, json.dumps(changes))
        self.assertNotIn(MID, [m["id"] for m in json.loads(self.scan.read_text())["meetings"]])
        self.assertEqual([e["id"] for e in read_manual(self.root)], [MID])
        stage(self.root, MID)  # still stages after the scan file was replaced
        stage(self.root, "legistar-examplecity-1001")

    def test_rescan_multi_body_with_failing_source_keeps_manual(self):
        self.add()
        first = self.manual.read_bytes()
        cfg = json.loads((self.root / "campaign.json").read_text())
        cfg["meeting_sources"] = [{"platform": "legistar", "client": c, "label": c} for c in ("alphacity", "betacity")]
        (self.root / "campaign.json").write_text(json.dumps(cfg))
        recorded = {"/v1/alphacity/events": [{"EventId": 11, "EventBodyName": "Alpha", "EventDate": "2026-11-05T00:00:00",
                                              "EventTime": "6:00 PM"}],
                    "/v1/alphacity/events/11/eventitems": [{"EventItemTitle": "Renew the automated license plate reader agreement"}]}
        source = self.tmp / "recorded.json"
        source.write_text(json.dumps(recorded))  # betacity has no recorded response: that source fails
        run = cli("meetings", "--directory", str(self.root), "--from-json", str(source), "--since", "2026-09-30")
        self.assertEqual(run.returncode, 3, run.stderr)
        self.assertEqual(self.manual.read_bytes(), first)
        self.assertNotIn(MID, self.scan.read_text())
        stage(self.root, MID)

    def test_replace_cancel_dated_history(self):
        self.add()
        self.replace(on="2026-11-10", body="Smallville Water Board 2", starts_at="2026-11-21T18:30:00-08:00")
        entry = self.doc()["meetings"][0]
        self.assertEqual(entry["body"], "Smallville Water Board 2")
        history = entry["history"]
        self.assertEqual([(h["action"], h["on"], h["reason"]) for h in history], [("replace", "2026-11-10", REASON)])
        self.assertEqual(history[0]["before"]["body"], "Smallville Water Board")
        self.assertNotIn("history", history[0]["before"])
        self.assertEqual(cancel_manual(self.root, MID, on="2026-11-10", reason="Notice posted", today=TODAY)["status"], "cancelled")
        entry = self.doc()["meetings"][0]
        self.assertEqual([h["action"] for h in entry["history"]], ["replace", "cancel"])  # equal dates keep call order
        self.assertEqual(entry["history"][0], history[0])
        self.assertEqual(entry["body"], "Smallville Water Board 2")
        self.refused(lambda: cancel_manual(self.root, MID, on="2026-11-10", reason="again", today=TODAY), "already cancelled")
        self.assertRaises(MeetingStageError, stage, self.root, MID)  # a cancelled entry stages nothing
        self.replace(on="2026-11-10", reason="Back on")  # an explicit replacement restores it
        entry = self.doc()["meetings"][0]
        self.assertEqual((entry["status"], len(entry["history"])), ("scheduled", 3))
        self.assertEqual(entry["history"][2]["before"]["status"], "cancelled")
        for kwargs, needle in (({"on": "2026-11-08"}, "--on"), ({"on": "2026-11-11"}, "--on"), ({"on": "2026-13-01"}, "--on"),
                               ({"on": None}, "--on"), ({"reason": ""}, "--reason"), ({"reason": "a<b"}, "--reason"),
                               ({"reason": None}, "--reason"), ({"reason": "x" * 201}, "--reason")):
            self.refused(lambda k=kwargs: self.replace(**{"on": "2026-11-10", "reason": REASON, **k}), needle)
        self.refused(lambda: record_manual(self.root, "manual-absent", **BASE, replace=True, on="2026-11-10", reason="r",
                                           today=TODAY), "no manual meeting")
        self.refused(lambda: cancel_manual(self.root, "manual-absent", on="2026-11-10", reason="r", today=TODAY), "no manual")
        self.refused(lambda: self.add(), "already exists")  # an existing id without --replace
        self.refused(lambda: record_manual(self.root, "manual-second", **BASE, on="2026-11-10", reason="r", today=TODAY))

    def test_collision_and_corruption_write_nothing(self):
        self.add()
        self.write_scan([{"id": MID, "body": "B", "starts_at": "2026-10-14T18:00:00-07:00"}])
        self.refused(lambda: self.add("manual-other"), "more than once")  # across the two files
        self.assertRaises(MeetingStageError, stage, self.root, MID)
        self.write_scan([{"id": "legistar-x-1"}, {"id": "legistar-x-1"}])
        self.refused(lambda: self.add("manual-other"), "more than once")  # within the scan file
        self.scan.unlink()
        good = self.doc()
        entry = good["meetings"][0]
        twin = {**entry}
        bad_documents = [
            {**good, "schema_version": True}, {**good, "schema_version": 2}, {**good, "extra": 1}, {"schema_version": 1, "meetings": {}},
            {**good, "meetings": [entry, twin]}, {**good, "meetings": [{**entry, "extra": 1}]},
            {**good, "meetings": [{**entry, "status": "gone"}]}, {**good, "meetings": [{**entry, "history": "x"}]},
            {**good, "meetings": [{**entry, "history": [{"action": "replace", "on": "2026-11-10", "reason": "r"}]}]},
            {**good, "meetings": [{**entry, "history": [{"action": "replace", "on": "2026-11-10", "reason": "r",
                                                          "before": {**entry, "id": "manual-else"}}]}]},
            {**good, "meetings": [{**entry, "source_url": "http://example.invalid/x"}]},
            {**good, "meetings": [{**entry, "starts_at": "2026-11-20T18:30:00"}]},
            {**good, "meetings": [{**entry, "time_known": False}]},
        ]
        for document in bad_documents:
            self.manual.write_text(json.dumps(document), encoding="utf-8")
            raw = self.manual.read_bytes()
            self.refused(lambda: self.add("manual-other"))
            self.assertRaises(MeetingStageError, stage, self.root, MID)
            self.assertEqual(self.manual.read_bytes(), raw)
        for raw in (b"\xff\xfe nope", b"{not json", b" " * (4 * 1024 * 1024 + 1)):
            self.manual.write_bytes(raw)
            self.refused(lambda: self.add("manual-other"))
            self.assertRaises(MeetingStageError, stage, self.root, MID)
        self.manual.unlink()
        self.assertEqual(read_manual(self.root), [])
        self.assertRaises(MeetingStageError, stage, self.root, MID)  # neither file: the scan hint
        self.write_scan(None, raw=b"\xff not json")  # a present corrupt scan file refuses, never omitted
        self.refused(lambda: self.add(), "kit/meetings.json: not UTF-8 JSON")
        self.assertFalse(self.manual.exists())
        self.scan.unlink()
        with mock.patch.object(mm, "MAX_BYTES", 300):  # the serialized output is capped too
            self.refused(lambda: self.add(), "larger than 4 MiB")
        self.assertFalse(self.manual.exists())

    def test_private_modes_symlinks_and_write_failure(self):
        self.add()
        self.assertEqual(stat.S_IMODE(self.manual.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "kit").stat().st_mode), 0o700)
        os.chmod(self.root / "kit", 0o755)
        self.add("manual-second", agenda_item="Item 6")
        self.assertEqual(stat.S_IMODE((self.root / "kit").stat().st_mode), 0o700)
        before = self.manual.read_bytes()
        with mock.patch("campaign_tool.kit.os.replace", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.add("manual-third")
        self.assertEqual(self.manual.read_bytes(), before)
        self.assertFalse([p for p in (self.root / "kit").iterdir() if p.name.endswith(".tmp")])
        outside = self.tmp / "outside.json"
        outside.write_text("{}")
        for make in ("link", "dangling", "dir", "kitlink"):
            if self.manual.is_symlink() or self.manual.is_file():
                self.manual.unlink()
            elif self.manual.is_dir():
                self.manual.rmdir()
            if make == "link":
                os.symlink(outside, self.manual)
            elif make == "dangling":
                os.symlink(self.tmp / "missing.json", self.manual)
            elif make == "dir":
                self.manual.mkdir()
            else:
                shutil.move(str(self.root / "kit"), self.tmp / "realkit")
                os.symlink(self.tmp / "realkit", self.root / "kit")
            with self.assertRaises(ManualMeetingError):
                self.add("manual-fourth")
            with self.assertRaises(MeetingStageError):
                stage(self.root, MID)
            self.assertEqual(outside.read_text(), "{}")
        os.unlink(self.root / "kit")
        shutil.move(str(self.tmp / "realkit"), self.root / "kit")
        self.write_scan([])
        self.scan.unlink()
        os.symlink(outside, self.scan)
        self.refused(lambda: self.add("manual-fifth"), "must not be a symlink")

    def test_dates_urls_and_text(self):
        self.add(starts_at="2026-11-10T00:30:00-08:00", checked_on="2026-11-10")  # today is accepted
        self.assertEqual(self.doc()["meetings"][0]["starts_at"], "2026-11-10T00:30:00-08:00")
        self.manual.unlink()
        self.add(starts_at="2026-11-20T18:30:15.5Z")  # seconds normalized, offset kept
        self.assertEqual(self.doc()["meetings"][0]["starts_at"], "2026-11-20T18:30:15+00:00")
        self.manual.unlink()
        bad = {
            "past start": dict(starts_at="2026-11-09T18:30:00-08:00"), "future checked": dict(checked_on="2026-11-11"),
            "checked after start": dict(starts_at="2026-11-10T09:00:00-08:00", checked_on="2026-11-11"),
            "naive": dict(starts_at="2026-11-20T18:30:00"), "impossible": dict(starts_at="2026-02-30T18:30:00-08:00"),
            "date only": dict(starts_at="2026-11-20"), "bad checked": dict(checked_on="2026-1-9"),
            "impossible checked": dict(checked_on="2026-02-30"),
            "markup body": dict(body="<b>Board</b>"), "control body": dict(body="Board\x00"), "tab": dict(agenda_item="a\tb"),
            "format char": dict(body="Board\u202e"), "empty body": dict(body="   "), "long body": dict(body="b" * 201),
            "empty item": dict(agenda_item=""), "long item": dict(agenda_item="i" * 501), "bad agency": dict(agency_id="Not An Agency"),
            "http": dict(agenda_url="http://example.invalid/a"), "creds": dict(source_url="https://u:p@example.invalid/a"),
            "no host": dict(agenda_url="https:///agenda"), "space": dict(agenda_url="https://example.invalid/a b"),
            "long url": dict(source_url="https://example.invalid/" + "a" * 500), "not str": dict(body=5),
        }
        for name, over in bad.items():
            with self.subTest(name):
                self.refused(lambda o=over: self.add(**o))
        for bad_id in ("manual-", "legistar-x-1", "manual-Upper", "manual-a_b", "manual-" + "x" * 80, "manual-é", ""):
            self.refused(lambda i=bad_id: self.add(i))
        self.assertEqual(self.add(body="  Spaced   Board ")["status"], "scheduled")
        self.assertEqual(self.doc()["meetings"][0]["body"], "Spaced Board")

    def test_stage_guards_and_private_cancel(self):
        self.add()
        stage(self.root, MID)
        target = self.root / "content" / "meetings" / f"{MID}.json"
        receipt = self.root / "kit" / "comments-sent.json"
        receipt.write_text(json.dumps({"schema_version": 1, "comments": {MID: {"on": "2026-11-09", "channel": "email",
                                                                              "recorded_at": "2026-11-09T00:00:00+00:00"}}}))
        frozen = (target.read_bytes(), receipt.read_bytes())
        cancel_manual(self.root, MID, on="2026-11-10", reason="Notice posted", today=TODAY)
        self.assertEqual((target.read_bytes(), receipt.read_bytes()), frozen)  # a private cancel never retracts a page
        self.assertRaises(MeetingStageError, stage, self.root, MID, replace=True)
        self.assertEqual(target.read_bytes(), frozen[0])
        self.replace(reason="Rescheduled")
        stage(self.root, MID, replace=True)  # an explicit re-stage after the restore

    def test_cli_and_help(self):
        helped = cli("meetings-add", "--help")
        self.assertEqual(helped.returncode, 0, helped.stderr)
        for flag in ("--body", "--item-title", "--cancel", "--replace", "--checked-on"):
            self.assertIn(flag, helped.stdout)
        d = str(self.root)
        entry = ["--body", BASE["body"], "--agency-id", BASE["agency_id"], "--starts-at", "2099-01-20T18:30:00-08:00",
                 "--agenda-url", BASE["agenda_url"], "--item-title", BASE["agenda_item"], "--source-url", BASE["source_url"],
                 "--checked-on", "2026-01-01"]
        ok = cli("meetings-add", "--directory", d, "--id", MID, *entry)
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn("Recorded only. Nothing was sent or published.", ok.stdout)
        self.assertEqual(json.loads(ok.stdout.split("\n}")[0] + "\n}"), {"id": MID, "status": "scheduled", "path": "kit/meetings-manual.json"})
        raw = self.manual.read_bytes()
        for args in (["--replace"], ["--cancel"], ["--replace", "--cancel", "--on", "2026-02-01", "--reason", "r"],
                     ["--on", "2026-02-01", "--reason", "r"], ["--cancel", "--on", "2026-02-01", "--reason", "r", "--body", "B"]):
            self.assertEqual(cli("meetings-add", "--directory", d, "--id", MID, *entry, *args).returncode, 2, args)
        self.assertEqual(cli("meetings-add", "--directory", d, "--id", MID, "--body", "B").returncode, 2)
        self.assertEqual(cli("meetings-add", "--directory", d, "--id", MID, "--cancel").returncode, 2)
        self.assertEqual(self.manual.read_bytes(), raw)
        past = cli("meetings-add", "--directory", d, "--id", "manual-old", *entry[:4], "--starts-at", "2000-01-01T10:00:00Z", *entry[6:])
        self.assertEqual((past.returncode, past.stdout), (1, ""))
        self.assertIn("Stopped:", past.stderr)
        self.assertNotIn("Traceback", past.stderr)
        again = cli("meetings-add", "--directory", d, "--id", MID, *entry)
        self.assertEqual(again.returncode, 1)
        self.assertIn("already exists", again.stderr)
        cancelled = cli("meetings-add", "--directory", d, "--id", MID, "--cancel", "--on", "2026-02-01", "--reason", REASON)
        self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
        for out in (ok.stdout + cancelled.stdout, cancelled.stderr, again.stderr, past.stderr):
            self.assertNotIn(REASON, out)
            self.assertNotIn("history", out)
        self.assertEqual(cli("meetings-add", "--directory", str(self.tmp / "nope"), "--id", MID, *entry).returncode, 1)
        self.assertFalse((self.root / "public").exists())
        self.assertFalse(list((self.root / "content" / "meetings").glob("manual-*")))


if __name__ == "__main__":
    unittest.main()
