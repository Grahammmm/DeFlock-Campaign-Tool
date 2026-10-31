"""meetings: changes since the previous kit/meetings.json. Synthetic fixture only, no network."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.meetings import compare_meetings  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legistar-events.json"
ID1, ID3 = "legistar-examplecity-1001", "legistar-examplecity-1003"
COUNTS0 = {"added": 0, "removed": 0, "rescheduled": 0, "agenda_changed": 0, "past": 0}
SINCE = date(2026, 9, 30)


def meeting(mid, starts_at, item="Item 7: ALPR", url="https://agenda.example.invalid/1"):
    return {"id": mid, "starts_at": starts_at, "agenda_item": item, "agenda_url": url}


def doc(meetings, client="examplecity", **extra):
    return dict({"schema_version": 1, "client": client, "generated": "2026-10-01T00:00:00+00:00",
                 "meetings": meetings}, **extra)


class CompareMeetingsTests(unittest.TestCase):
    def test_none_and_stable_order(self):
        self.assertEqual(compare_meetings(None, [], "x", date(2026, 10, 1)), {"previous": "none"})
        old = [meeting("c", "2026-10-30T00:00:00"), meeting("a", "2026-10-30T00:00:00"),
               meeting("b", "2026-10-02T00:00:00"), meeting("z", "2026-09-01T00:00:00"),
               meeting("y", "2026-09-02T00:00:00")]
        new = [meeting("e", "2026-11-01T00:00:00"), meeting("b", "2026-10-03T00:00:00", item="other"),
               meeting("d", "2026-11-01T00:00:00")]
        first = compare_meetings(doc(old), new, "examplecity", SINCE)
        second = compare_meetings(doc(list(reversed(old))), list(reversed(new)), "examplecity", SINCE)
        self.assertEqual(first, second)
        self.assertEqual(first["added"], ["d", "e"])
        self.assertEqual(first["removed"], ["a", "c"])
        self.assertEqual(first["past"], ["y", "z"])
        self.assertEqual(first["rescheduled"], [{"id": "b", "from": "2026-10-02T00:00:00", "to": "2026-10-03T00:00:00"}])
        self.assertEqual(first["agenda_changed"], ["b"])
        self.assertEqual(first["previous_generated"], "2026-10-01T00:00:00+00:00")

    def test_since_boundary_is_removed_not_past(self):
        out = compare_meetings(doc([meeting("a", "2026-09-30T23:00:00"), meeting("b", "2026-09-29T23:59:59")]),
                               [], "examplecity", SINCE)
        self.assertEqual((out["removed"], out["past"]), (["a"], ["b"]))

    def test_agenda_url_alone_and_unchanged(self):
        old = [meeting("a", "2026-10-30T00:00:00"), meeting("b", "2026-10-30T00:00:00")]
        new = [meeting("a", "2026-10-30T00:00:00", url=None), meeting("b", "2026-10-30T00:00:00")]
        out = compare_meetings(doc(old), new, "examplecity", SINCE)
        self.assertEqual(out["agenda_changed"], ["a"])
        self.assertEqual(out["rescheduled"], [])
        self.assertIsNone(compare_meetings(doc([], generated=5), [], "examplecity", SINCE)["previous_generated"])

    def test_unreadable_shapes(self):
        for bad in ("unreadable", [], {"schema_version": 2}, doc(None), doc({}), doc([meeting("a", None)]),
                    doc([{"starts_at": "2026-10-01T00:00:00"}]), doc([meeting(7, "2026-10-01T00:00:00")]),
                    doc(["not a dict"]), dict(doc([]), schema_version="1")):
            self.assertEqual(compare_meetings(bad, [], "examplecity", SINCE), {"previous": "unreadable"}, bad)

    def test_other_client_is_truncated(self):
        self.assertEqual(compare_meetings(doc([], client="othercity"), [], "examplecity", SINCE),
                         {"previous": "other_client", "previous_client": "othercity"})
        out = compare_meetings(doc([], client="x" * 100), [], "examplecity", SINCE)
        self.assertEqual(out["previous_client"], "x" * 64)
        self.assertEqual(compare_meetings(doc([], client=None), [], "examplecity", SINCE)["previous"], "other_client")


class MeetingsChangesCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-meetings-changes-")
        self.root = Path(self.tmp.name) / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=20)
        self.target = self.root / "kit" / "meetings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_meetings(self, source=FIXTURE, client="examplecity"):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "meetings", "--directory", str(self.root),
                               "--client", client, "--from-json", str(source), "--since", "2026-09-30"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)

    def run_ok(self):
        result = self.run_meetings()
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout), json.loads(self.target.read_text())

    def write_previous(self, mutate):
        out, current = self.run_ok()
        previous = copy.deepcopy(current)
        mutate(previous)
        self.target.write_text(json.dumps(previous))
        return current

    def test_first_then_identical_run(self):
        out, document = self.run_ok()
        self.assertEqual(out["previous"], "none")
        self.assertNotIn("changes", out)
        self.assertNotIn("changes", document)
        out, document = self.run_ok()
        self.assertEqual(out["previous"], "compared")
        self.assertEqual(out["changes"], COUNTS0)
        for key in COUNTS0:
            self.assertEqual(document["changes"][key], [], key)
        self.assertEqual(document["changes"]["previous"], "compared")
        self.assertIsInstance(document["changes"]["previous_generated"], str)

    def test_added_removed_past_rescheduled_agenda(self):
        def mutate(previous):
            first = previous["meetings"][0]
            first["starts_at"] = "2026-10-07T18:00:00"
            first["agenda_item"] = "Item 9: different text"
            previous["meetings"] = [first, dict(first, id="legistar-examplecity-1099", starts_at="2026-10-30T18:00:00"),
                                    dict(first, id="legistar-examplecity-1098", starts_at="2026-09-01T18:00:00")]
        self.write_previous(mutate)
        out, document = self.run_ok()
        changes = document["changes"]
        self.assertEqual(out["changes"], {"added": 1, "removed": 1, "rescheduled": 1, "agenda_changed": 1, "past": 1})
        self.assertEqual(changes["added"], [ID3])
        self.assertEqual(changes["removed"], ["legistar-examplecity-1099"])
        self.assertEqual(changes["past"], ["legistar-examplecity-1098"])
        self.assertEqual(changes["rescheduled"], [{"id": ID1, "from": "2026-10-07T18:00:00", "to": "2026-10-14T18:00:00"}])
        self.assertEqual(changes["agenda_changed"], [ID1])
        self.assertEqual([m["id"] for m in document["meetings"]], [ID1, ID3])

    def test_agenda_url_alone(self):
        self.write_previous(lambda p: p["meetings"][0].update(agenda_url="https://agenda.example.invalid/old"))
        out, document = self.run_ok()
        self.assertEqual(document["changes"]["agenda_changed"], [ID1])
        self.assertEqual(out["changes"], dict(COUNTS0, agenda_changed=1))

    def assert_unreadable_then_replaced(self):
        out, document = self.run_ok()
        self.assertEqual(out["previous"], "unreadable")
        self.assertNotIn("changes", out)
        self.assertEqual(document["changes"], {"previous": "unreadable"})
        self.assertEqual([m["id"] for m in document["meetings"]], [ID1, ID3])

    def test_unreadable_previous_files(self):
        self.run_ok()
        payloads = [b"{not json", b"\xff\xfe{}", b"null", json.dumps({"schema_version": 2}).encode(),
                    json.dumps({"schema_version": 1, "client": "examplecity",
                                "meetings": [{"starts_at": "2026-10-14T18:00:00"}]}).encode(),
                    b" " * (4 * 1024 * 1024) + b"{}"]
        for payload in payloads:
            with self.subTest(payload=payload[:20]):
                self.target.write_bytes(payload)
                self.assert_unreadable_then_replaced()

    def test_symlinked_previous_is_replaced_target_untouched(self):
        _, current = self.run_ok()
        outside = Path(self.tmp.name) / "outside.json"
        outside.write_text(json.dumps(current))
        before = outside.read_bytes()
        self.target.unlink()
        self.target.symlink_to(outside)
        self.assert_unreadable_then_replaced()
        self.assertFalse(self.target.is_symlink())
        self.assertEqual(outside.read_bytes(), before)

    def test_dangling_symlink_is_unreadable(self):
        self.run_ok()
        self.target.unlink()
        self.target.symlink_to(Path(self.tmp.name) / "missing.json")
        self.assert_unreadable_then_replaced()
        self.assertFalse(self.target.is_symlink())

    def test_other_client(self):
        self.write_previous(lambda p: p.update(client="othercity"))
        out, document = self.run_ok()
        self.assertEqual(out["previous"], "other_client")
        self.assertNotIn("changes", out)
        self.assertEqual(document["changes"], {"previous": "other_client", "previous_client": "othercity"})

    def test_fetch_failure_keeps_old_file(self):
        self.run_ok()
        before = self.target.read_bytes()
        broken = Path(self.tmp.name) / "no-events.json"
        broken.write_text("{}")
        result = self.run_meetings(source=broken)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.target.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
