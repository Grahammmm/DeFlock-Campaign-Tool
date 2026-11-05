"""DF1507-08: meetings for several governing bodies from campaign.json meeting_sources. Recorded synthetic JSON, no network."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.meetings import load_sources  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legistar-events.json"


def recorded(client, days):
    """Synthetic Legistar responses for one client: one ALPR event per (event id, ISO day)."""
    out = {f"/v1/{client}/events": [{"EventId": n, "EventBodyName": f"{client} board", "EventDate": f"{day}T00:00:00",
                                     "EventTime": "6:00 PM"} for n, day in days]}
    for n, _ in days:
        out[f"/v1/{client}/events/{n}/eventitems"] = [{"EventItemTitle": "Renew the automated license plate reader agreement"}]
    return out


A = recorded("alphacity", [(11, "2026-10-20"), (12, "2026-11-05")])
B = recorded("betacity", [(21, "2026-10-14")])
C = recorded("gammacity", [(31, "2026-10-30")])
SOURCES = [{"platform": "legistar", "client": c, "label": f"{c} council"} for c in ("alphacity", "betacity", "gammacity")]


class MultiSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-meetings-sources-")
        self.dir = Path(self.tmp.name)
        self.root = self.dir / "campaign"
        self.cli("init", "--directory", str(self.root), "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.target = self.root / "kit" / "meetings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=30)

    def sources(self, entries):
        path = self.root / "campaign.json"
        cfg = json.loads(path.read_text())
        cfg["meeting_sources"] = entries
        path.write_text(json.dumps(cfg))

    def run_multi(self, *mappings, extra=()):
        merged = {}
        for mapping in mappings:
            merged.update(mapping)
        src = self.dir / "recorded.json"
        src.write_text(json.dumps(merged))
        return self.cli("meetings", "--directory", str(self.root), "--from-json", str(src), "--since", "2026-09-30", *extra)

    def doc(self):
        return json.loads(self.target.read_text())

    def test_three_sources_combined_sorted_with_changes_by_client(self):
        self.sources(SOURCES)
        first = self.run_multi(A, B, C)
        self.assertEqual(first.returncode, 0, first.stderr)
        doc = self.doc()
        self.assertEqual((doc["schema_version"], doc["client"], doc["clients"]), (1, "multi", ["alphacity", "betacity", "gammacity"]))
        self.assertEqual([m["id"] for m in doc["meetings"]],
                         ["legistar-betacity-21", "legistar-alphacity-11", "legistar-gammacity-31", "legistar-alphacity-12"])
        self.assertEqual({m["client"] for m in doc["meetings"]}, {"alphacity", "betacity", "gammacity"})
        self.assertEqual(doc["meetings"][0]["source_label"], "betacity council")
        self.assertEqual([s["status"] for s in doc["sources"]], ["ok"] * 3)
        self.assertEqual({c: v["previous"] for c, v in doc["changes"].items()}, {c: "none" for c in ("alphacity", "betacity", "gammacity")})
        second = self.run_multi(A, B, C)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["changes"]["alphacity"],
                         {"added": 0, "removed": 0, "rescheduled": 0, "agenda_changed": 0, "past": 0})
        self.assertEqual(set(self.doc()["changes"]), {"alphacity", "betacity", "gammacity"})

    def test_failing_source_is_named_keeps_previous_and_others_update(self):
        self.sources(SOURCES)
        self.assertEqual(self.run_multi(A, B, C).returncode, 0)
        newer_a = recorded("alphacity", [(11, "2026-10-20"), (12, "2026-11-05"), (13, "2026-11-12")])
        result = self.run_multi(newer_a, C)  # betacity has no recorded response
        self.assertEqual(result.returncode, 3, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["failed_sources"], ["betacity"])
        doc = self.doc()
        failed = [s for s in doc["sources"] if s["status"] == "failed"]
        self.assertEqual([s["client"] for s in failed], ["betacity"])
        self.assertTrue(failed[0]["error"].startswith("MeetingsError"))
        by_id = {m["id"]: m for m in doc["meetings"]}
        self.assertTrue(by_id["legistar-betacity-21"]["stale"])
        self.assertNotIn("stale", by_id["legistar-alphacity-13"])
        self.assertIn("legistar-gammacity-31", by_id)
        self.assertEqual(doc["changes"]["alphacity"]["added"], ["legistar-alphacity-13"])
        self.assertNotIn("betacity", doc["changes"])
        third = self.run_multi(A, B, C)  # recovered: no stale left
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertFalse(any("stale" in m for m in self.doc()["meetings"]))

    def test_first_run_with_a_failing_source(self):
        self.sources(SOURCES)
        result = self.run_multi(A, C)
        self.assertEqual(result.returncode, 3, result.stderr)
        doc = self.doc()
        self.assertEqual({m["client"] for m in doc["meetings"]}, {"alphacity", "gammacity"})
        self.assertEqual([(s["client"], s["status"]) for s in doc["sources"]],
                         [("alphacity", "ok"), ("betacity", "failed"), ("gammacity", "ok")])

    def test_previous_single_client_file_is_compared_not_other_client(self):
        self.sources([{"platform": "legistar", "client": "examplecity", "label": "Example City"}, SOURCES[1]])
        single = self.cli("meetings", "--directory", str(self.root), "--client", "examplecity", "--from-json", str(FIXTURE),
                          "--since", "2026-09-30")
        self.assertEqual(single.returncode, 0, single.stderr)
        merged = json.loads(FIXTURE.read_text())
        merged.update(B)
        result = self.run_multi(merged)
        self.assertEqual(result.returncode, 0, result.stderr)
        changes = self.doc()["changes"]
        self.assertEqual(changes["examplecity"]["previous"], "compared")
        self.assertEqual(changes["examplecity"]["added"], [])
        self.assertEqual(changes["betacity"]["previous"], "none")

    def test_client_flag_keeps_single_client_document(self):
        self.sources(SOURCES)
        plain_root = self.dir / "plain"
        self.cli("init", "--directory", str(plain_root), "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        args = ("meetings", "--client", "examplecity", "--from-json", str(FIXTURE), "--since", "2026-09-30")
        self.assertEqual(self.cli(*args, "--directory", str(self.root)).returncode, 0)
        self.assertEqual(self.cli(*args, "--directory", str(plain_root)).returncode, 0)
        docs = [json.loads((r / "kit" / "meetings.json").read_text()) for r in (self.root, plain_root)]
        for d in docs:
            d.pop("generated")
        self.assertEqual(docs[0], docs[1])
        self.assertEqual(docs[0]["client"], "examplecity")
        self.assertNotIn("sources", docs[0])

    def test_bad_meeting_sources_stop_naming_the_index(self):
        ok = SOURCES[0]
        cases = {
            "meeting_sources[1]": [ok, dict(ok)],
            "meeting_sources[1] ": [ok, dict(ok, client="betacity", platform="swagit")],
            "meeting_sources[0]": [dict(ok, label="<b>council</b>")],
            "meeting_sources[0] ": [dict(ok, extra="x")],
            "meeting_sources[0]  ": [dict(ok, client="Bad_Slug")],
            "meeting_sources: give 1 to 12": [dict(ok, client=f"city{n:02d}") for n in range(13)],
        }
        for expect, entries in cases.items():
            with self.subTest(expect=expect):
                self.sources(entries)
                result = self.run_multi(A)
                self.assertEqual(result.returncode, 1)
                self.assertIn("Stopped: " + expect.strip(), result.stderr)
                self.assertFalse(self.target.exists())

    def test_no_client_and_no_sources_stops(self):
        result = self.run_multi(A)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Stopped: add meeting_sources to campaign.json or pass --client", result.stderr)
        self.sources([])
        self.assertIn("Stopped: meeting_sources: give 1 to 12", self.run_multi(A).stderr)

    def test_load_sources_defaults_label_to_client(self):
        self.assertEqual(load_sources({}), [])
        self.assertEqual(load_sources({"meeting_sources": [{"platform": "legistar", "client": "alphacity"}]})[0]["label"], "alphacity")

    def test_meetings_stage_works_on_the_combined_file(self):
        shutil.copytree(REPO_ROOT / "examples" / "fictional-campaign", self.dir / "example")
        self.root = self.dir / "example"
        self.target = self.root / "kit" / "meetings.json"
        self.sources(SOURCES)
        self.assertEqual(self.run_multi(A, B, C).returncode, 0)
        result = self.cli("meetings-stage", "--directory", str(self.root), "--id", "legistar-betacity-21")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "content" / "meetings" / "legistar-betacity-21.json").is_file())


if __name__ == "__main__":
    unittest.main()
