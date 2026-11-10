"""DF1109B-02: `today` lists the meeting steps it used to miss: outcome_missing, participation_unverified and
meeting_not_staged. Read-only; synthetic content only, nothing fetched.

Run from the repository root: python3 -B -m unittest tests.test_today_meeting_work -v
"""
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import today

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
NEW = ("outcome_missing", "participation_unverified", "meeting_not_staged")
PART = {"in_person": False, "remote": None, "written_comment": None, "source_url": "https://example.invalid/agenda",
        "verified_by": "Synthetic Organizer", "verified_at": "2026-10-01"}
OUTCOME = {"result": "approved", "on": "2026-10-10", "source_url": "https://example.invalid/m",
           "verified_by": "Synthetic Organizer", "verified_at": "2026-10-10"}


class MeetingWork(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-work-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "synthetic campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.folder = self.root / "content" / "meetings"
        for path in self.folder.glob("*.json"):
            path.unlink()

    def stage(self, ident, starts, **extra):
        doc = {"id": ident, "body": "Cedar Board", "starts_at": starts, **extra}
        (self.folder / f"{ident}.json").write_text(json.dumps(doc), encoding="utf-8")

    def scan(self, rows, raw=None):
        (self.root / "kit").mkdir(exist_ok=True)
        text = raw if raw is not None else json.dumps({"schema_version": 1, "meetings": rows})
        (self.root / "kit" / "meetings.json").write_text(text, encoding="utf-8")

    def new(self, day, kinds=NEW, **kw):
        return [i for i in today.todo(self.root, date.fromisoformat(day), **kw) if i["kind"] in kinds]

    def test_outcome_missing_starts_next_typed_day_and_never_expires(self):
        self.stage("a-late", "2026-10-10T23:30:00-08:00", participation=PART)
        self.stage("b-early", "2026-10-10T00:30:00+09:00", participation=PART)
        self.assertEqual(self.new("2026-10-10"), [])  # the day of the meeting: not yet
        items = self.new("2026-10-11")
        self.assertEqual([i["what"] for i in items],
                         ["Cedar Board met on 2026-10-10 (a-late): record what happened",
                          "Cedar Board met on 2026-10-10 (b-early): record what happened"])
        self.assertEqual(items[0]["command"], "meetings-outcome --directory '%s' --id a-late --result RESULT "
                         "--on YYYY-MM-DD --source OFFICIAL_URL --verified-by NAME" % self.root)
        self.assertEqual(len(self.new("2027-06-01")), 2)  # no expiry
        self.stage("a-late", "2026-10-10T23:30:00-08:00", participation=PART, outcome=OUTCOME)
        self.stage("b-early", "2026-10-10T00:30:00+09:00", status="cancelled")
        self.assertEqual(self.new("2026-10-11"), [])

    def test_participation_window_inclusive_and_verified_none_routes(self):
        for offset, day in enumerate(("2026-10-09", "2026-10-10", "2026-10-24", "2026-10-25")):
            self.stage(f"m{offset}", f"{day}T18:00:00-07:00")
        self.stage("verified", "2026-10-12T18:00:00-07:00", participation=PART)
        self.stage("off", "2026-10-12T18:00:00-07:00", status="cancelled")
        ids = [i["what"].rsplit("(", 1)[1] for i in self.new("2026-10-10", ("participation_unverified",))]
        self.assertEqual(ids, ["m1)", "m2)"])  # day 0 and day 14 in; day -1 and day 15 out
        item = self.new("2026-10-10", ("participation_unverified",))[0]
        self.assertEqual(item["what"], "Verify how the public may take part in Cedar Board on 2026-10-10 (m1)")
        self.assertIn("--id m1 --participation FILE --replace", item["command"])

    def test_scan_match_staged_presence_not_false_absence(self):
        rows = [{"id": n, "body": "Cedar Board", "starts_at": "2026-10-12T18:00:00-07:00", "provider": "x"}
                for n in ("fresh", "staged", "named-file", "corrupt")]
        rows.append({"id": "far", "body": "B", "starts_at": "2026-11-12T18:00:00-07:00"})
        rows.append({"id": "off", "body": "B", "starts_at": "2026-10-12T18:00:00-07:00", "status": "cancelled"})
        self.scan(rows)
        self.stage("staged", "2026-10-12T18:00:00-07:00", participation=PART)
        (self.folder / "named-file.json").write_text(json.dumps({"id": "other-id", "body": "B",
                         "starts_at": "2026-10-12T18:00:00-07:00", "participation": PART}), encoding="utf-8")
        (self.folder / "corrupt.json").write_text("{", encoding="utf-8")
        items = self.new("2026-10-10", ("meeting_not_staged",))
        self.assertEqual([i["command"] for i in items], [f"meetings-stage --directory '{self.root}' --id fresh"])
        self.assertEqual(items[0]["what"], "Agenda match for Cedar Board on 2026-10-12 (fresh): verify the posted "
                         "agenda and stage the meeting")
        self.assertEqual(sum(i["kind"] == "unreadable" and "corrupt.json" in i["what"]
                             for i in today.todo(self.root, date(2026, 10, 10))), 1)

    def test_scan_missing_empty_broken_and_duplicate(self):
        self.stage("keep", "2026-10-12T18:00:00-07:00")
        self.assertEqual(self.new("2026-10-10", ("meeting_not_staged",)), [])  # missing file
        self.scan([])
        self.assertEqual(self.new("2026-10-10", ("meeting_not_staged",)), [])
        good = {"id": "ok", "body": "B", "starts_at": "2026-10-12T18:00:00-07:00"}
        bad_texts = ["{", json.dumps({"schema_version": 2, "meetings": []}), json.dumps({"schema_version": 1, "meetings": {}}),
                     json.dumps({"schema_version": 1, "meetings": [good, "x"]}),
                     json.dumps({"schema_version": 1, "meetings": [good, {**good, "id": "../x"}]}),
                     json.dumps({"schema_version": 1, "meetings": [good, {**good, "starts_at": "2026-10-12"}]}),
                     json.dumps({"schema_version": 1, "meetings": [good, good]}), "x" * (4 * 1024 * 1024 + 1)]
        for text in bad_texts:
            self.scan(None, raw=text)
            items = today.todo(self.root, date(2026, 10, 10))
            self.assertEqual([i["what"].startswith("fix kit/meetings.json") for i in items if i["kind"] == "unreadable"], [True])
            self.assertFalse([i for i in items if i["kind"] == "meeting_not_staged"])  # no partial rows
            self.assertTrue([i for i in items if i["kind"] == "participation_unverified"])  # others survive
        path = self.root / "kit" / "meetings.json"
        path.unlink()
        path.mkdir()
        self.assertEqual(len([i for i in today.todo(self.root, date(2026, 10, 10)) if i["kind"] == "unreadable"]), 1)
        path.rmdir()
        target = self.root / "elsewhere.json"
        target.write_text(json.dumps({"schema_version": 1, "meetings": [good]}), encoding="utf-8")
        path.symlink_to(target)
        items = today.todo(self.root, date(2026, 10, 10))
        self.assertEqual(len([i for i in items if i["kind"] == "unreadable"]), 1)
        self.assertFalse([i for i in items if i["kind"] == "meeting_not_staged"])

    def test_fixed_kind_rank_due_key_and_json_shape(self):
        order = list(today.KINDS)
        self.assertEqual(order[order.index("contract_ending") + 1:order.index("contract_ending") + 5],
                         [*NEW, "minutes_pending"])
        self.scan([{"id": "zz", "body": "B", "starts_at": "2026-10-11T18:00:00-07:00"},
                   {"id": "aa", "body": "B", "starts_at": "2026-10-11T18:00:00-07:00"}])
        self.stage("zz-old", "2026-10-01T18:00:00-07:00", participation=PART)
        self.stage("sp", "2026-10-11T18:00:00-07:00")
        items = [i for i in today.todo(self.root, date(2026, 10, 10)) if i["kind"] in NEW]
        self.assertEqual([(i["kind"], i["due"]) for i in items],
                         [("outcome_missing", "2026-10-01"), ("participation_unverified", "2026-10-11"),
                          ("meeting_not_staged", "2026-10-11"), ("meeting_not_staged", "2026-10-11")])
        self.assertEqual([i["command"].rsplit(" --id ", 1)[1][:2] for i in items[2:]], ["aa", "zz"])
        self.assertTrue(all(set(i) == {"rank", "kind", "what", "due", "command"} for i in items))

    def test_default_legacy_bytes_and_no_side_effects(self):
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(self.new("2026-10-10"), [])
        text = today.run_cli(self.root, "2026-10-10")
        json.loads(today.run_cli(self.root, "2026-10-10", as_json=True))
        self.assertNotIn("record what happened", text)
        self.scan(None, raw="{")
        today.run_cli(self.root, "2026-10-10")
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file() and p.name != "meetings.json"},
                         {p: b for p, b in before.items() if p.name != "meetings.json"})

    def test_named_selector_in_every_hint(self):
        self.scan([{"id": "fresh", "body": "B", "starts_at": "2026-10-12T18:00:00-07:00"}])
        self.stage("old", "2026-10-01T18:00:00-07:00")
        self.stage("soon", "2026-10-12T18:00:00-07:00")
        items = [i for i in today.todo(self.root, date(2026, 10, 10), campaign_name="demo") if i["kind"] in NEW]
        self.assertEqual(len(items), 3)
        self.assertTrue(all("--campaign demo" in i["command"] and "--directory" not in i["command"] for i in items))


if __name__ == "__main__":
    unittest.main()
