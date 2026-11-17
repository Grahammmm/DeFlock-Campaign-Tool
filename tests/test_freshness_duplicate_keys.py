"""A repeated JSON member name in public freshness content makes that file one unknown (offline, synthetic)."""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import freshness  # noqa: E402

TODAY = date(2026, 11, 16)
OLD, NEW = '"checked_on":"2025-01-01"', '"checked_on":"2026-11-16"'
FILES = {
    "agencies.json": ('[{{"agency_id":"ex-city","profile":{{"contract":{{{}}}}}}}]', "content/agencies.json",
                      "recheck the source and update checked_on", "content/agencies.json:ex-city.contract"),
    "officials.json": ('{{"schema_version":1,"bodies":[{{"officials":[{{{}}}]}}]}}', "content/officials.json",
                       "re-check the official's page and update checked_on", "content/officials.json:bodies[0].officials[0]"),
    "body-schedules.json": ('{{"schema_version":1,"schedules":[{{{}}}]}}', "content/body-schedules.json",
                            "re-check the body's calendar page and update checked_on",
                            "content/body-schedules.json:schedules[0]"),
    "sources.json": ('[{{"public_copy":{{{}}}}}]', "content/sources.json",
                     "re-download the public copy and run sources verify-copy", "content/sources.json:[0].public_copy"),
}


class FreshnessDuplicateKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-freshness-dup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "content").mkdir()

    def write(self, name, text):
        (self.root / "content" / name).write_text(text, encoding="utf-8")

    def entries(self, prefix="content/"):
        found = freshness.stale_inputs(self.root, {"state": "CA"}, 90, today=TODAY)
        return [e for e in found if e["input"].startswith(prefix)]

    def test_duplicate_checked_on_is_file_unknown_for_each_public_family(self):
        for name, (template, label, refresh, row) in FILES.items():
            for body in (OLD + "," + NEW, NEW + "," + OLD, OLD + "," + OLD):
                with self.subTest(name=name, body=body):
                    self.write(name, template.format(body))
                    self.assertEqual(self.entries(label), [
                        {"input": label, "status": "unknown", "date": None, "refresh": refresh}])
            self.write(name, template.format(OLD))  # control: stale without a duplicate
            self.assertEqual([e["input"] for e in self.entries(label)], [row])

    def test_duplicate_containers_cannot_hide_facts(self):
        stale = '{"checked_on":"2025-01-01"}'
        cases = {
            "officials.json": ['{"schema_version":1,"bodies":[{"officials":[%s]}],"bodies":[]}' % stale,
                               '{"schema_version":1,"schema_version":1,"bodies":[]}'],
            "body-schedules.json": ['{"schema_version":1,"schedules":[%s],"schedules":[]}' % stale],
            "agencies.json": ['[{"agency_id":"ex-city","profile":{"contract":%s},"profile":{}}]' % stale,
                              '[{"agency_id":"ex-city","profile":{"x":{"a":1,"a":1}}}]'],
            "sources.json": ['[{"public_copy":%s,"public_copy":{"matches":true}}]' % stale],
        }
        for name, texts in cases.items():
            for text in texts:
                with self.subTest(name=name, text=text):
                    self.write(name, text)
                    self.assertEqual([e["status"] for e in self.entries("content/" + name)], ["unknown"])

    def test_escaped_names_share_decoded_identity_and_nothing_leaks(self):
        self.write("body-schedules.json",
                   '{"schema_version":1,"schedules":[{"checked_on":"2025-01-01","checked\\u005fon":"2026-11-16"}]}')
        self.assertEqual([e["status"] for e in self.entries("content/body-schedules")], ["unknown"])
        self.write("sources.json", '[{"meta":{"zz-canary-key":"zz-canary-val","zz-canary-key":"x"}}]')
        shown = json.dumps(self.entries("content/sources"))
        for canary in ("zz-canary", str(self.root)):
            self.assertNotIn(canary, shown)

    def test_uniqueness_is_per_object_and_valid_controls_match(self):
        self.write("body-schedules.json", '{"schema_version":1,"schedules":[{%s},{%s},{"x":1}]}' % (OLD, NEW))
        self.assertEqual([(e["input"], e["status"]) for e in self.entries("content/body")], [
            ("content/body-schedules.json:schedules[0]", "stale"),
            ("content/body-schedules.json:schedules[2]", "unknown")])
        self.write("officials.json", '{"schema_version":1,"bodies":[{"officials":[{"a":1}]},{"officials":[{"a":2}]}]}')
        self.write("sources.json", "[]")
        self.assertEqual([e["input"] for e in self.entries("content/officials")],
                         ["content/officials.json:bodies[0].officials[0]", "content/officials.json:bodies[1].officials[0]"])
        self.assertEqual(self.entries("content/sources"), [])
        self.write("agencies.json", '[{"agency_id":"ex-city","profile":{"contract":{%s}}}]' % NEW)
        self.assertEqual(self.entries("content/agencies"), [])

    def test_bad_file_does_not_suppress_other_file_diagnostics(self):
        self.write("agencies.json", '[{"agency_id":"ex-city","profile":{"contract":{%s,%s}}}]' % (OLD, NEW))
        self.write("body-schedules.json", '{"schema_version":1,"schedules":[{%s}]}' % OLD)
        self.write("sources.json", '[{"public_copy":{"checked_on":"2027-01-01"}}]')
        got = [(e["input"], e["status"]) for e in self.entries("content/") if "/map/" not in e["input"]]
        self.assertEqual(got, [("content/agencies.json", "unknown"),
                               ("content/body-schedules.json:schedules[0]", "stale"),
                               ("content/sources.json:[0].public_copy", "future")])

    def test_doctor_cli_reports_unknown_read_only_and_recovers(self):
        command = [sys.executable, "-B", "-m", "campaign_tool"]
        init = subprocess.run(command + ["init", "--directory", str(self.root / "c"), "--name", "Synthetic",
                                         "--county", "San Luis Obispo", "--state", "CA"],
                              cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(init.returncode, 0, init.stderr)
        target = self.root / "c" / "content" / "body-schedules.json"
        target.parent.mkdir(exist_ok=True)
        text = '{"schema_version":1,"schedules":[{%s,%s}]}' % (OLD, NEW)
        target.write_text(text, encoding="utf-8")

        def doctor(*extra):
            done = subprocess.run(command + ["doctor", "--directory", str(self.root / "c")] + list(extra),
                                  cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertNotIn("Traceback", done.stderr)
            return json.loads(done.stdout)

        rows = [e for e in doctor("--stale-days", "90")["stale_inputs"] if e["input"].endswith("body-schedules.json")]
        self.assertEqual([e["status"] for e in rows], ["unknown"])
        self.assertEqual(target.read_text(encoding="utf-8"), text)
        self.assertFalse(any("stale_inputs" in key for key in doctor()))  # no flag: not scanned
        target.write_text('{"schema_version":1,"schedules":[{%s}]}' % NEW, encoding="utf-8")
        again = doctor("--stale-days", "90")["stale_inputs"]
        self.assertEqual([e for e in again if e["input"].endswith("body-schedules.json")], [])


if __name__ == "__main__":
    unittest.main()
