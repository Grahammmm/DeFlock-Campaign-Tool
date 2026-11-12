"""RF1741-4: refuse control-bearing meeting identifiers before doctor restaging advice is emitted."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from campaign_tool.cli import doctor_report
from campaign_tool.doctor_checklist import checklist, render

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "examples" / "fictional-campaign"
MESSAGE = "checklist stale meeting identifier contains a control character; repair content/meetings/"
CONTROLS = [chr(c) for c in (*range(0, 32), *range(127, 160), 0x2028, 0x2029)]
FORGED = "synthetic\n[ok] FORGED"
SLUG = "cedar-policy-posted"
KIT = f"Cite /findings/{SLUG}.html in two minutes."


def stale(*meetings):
    return {"stale_comment_kits": [{"meeting": m, "finding": "f", "latest_correction": "2026-01-02"}
                                   for m in meetings]}


def snapshot(root):
    return {str(p.relative_to(root)): (p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class DirectTests(unittest.TestCase):
    def setUp(self):
        self.git = self.enterContext(mock.patch("campaign_tool.git_exposure.git_exposure_row", return_value=None))

    def refused(self, call):
        with self.assertRaises(ValueError) as caught:
            call()
        self.assertEqual(str(caught.exception), MESSAGE)

    def test_all_meeting_slots_refuse_existing_control_policy(self):
        for control in CONTROLS:
            for slot in range(3):
                meetings = ["m-a", "m-b", "m-c"]
                meetings[slot] = "x" + control + "y"
                meetings += [meetings[slot]]  # a duplicate bad value cannot hide it from validation
                report = stale(*meetings)
                before = copy.deepcopy(report)
                with self.subTest(code=ord(control), slot=slot):
                    self.refused(lambda: checklist(report, "D"))
                    self.refused(lambda: render(report, "D"))
                    self.refused(lambda: render(report, "D", as_json=True))
                    self.assertEqual(report, before)
        self.assertTrue(self.git.called)

    def test_late_unique_bad_meeting_is_still_refused(self):
        self.refused(lambda: checklist(stale(*(f"m{i:02d}" for i in range(30)), "zz-late\x1b[2J"), "D"))

    def test_ordinary_commands_and_empty_rows_are_byte_identical(self):
        report = stale("m2", "m1", "m1", "two words", "o'neil", "é-ü")
        row = {r["key"]: r for r in checklist(report, "D")}["stale_comment_kits"]
        base = "`python3 -m campaign_tool meetings-stage --directory D --id "
        self.assertEqual(row["next"], "Re-stage with corrected findings: " + "; ".join(
            base + quoted + " --replace`" for quoted in ("m1", "m2", "'o'\"'\"'neil'", "'two words'", "'é-ü'")))
        self.assertEqual((row["state"], row["owner"]), ("todo", "organizer"))
        lead = {r["key"]: r for r in checklist(stale("m1"), "-lead")}["stale_comment_kits"]
        self.assertEqual(lead["next"], "Re-stage with corrected findings: `python3 -m campaign_tool meetings-stage "
                                       "--directory ./-lead --id m1 --replace`")
        for report in ({"stale_comment_kits": []}, {}):
            rows = {r["key"]: r for r in checklist(report, "D")}
            self.assertEqual(rows.get("stale_comment_kits", {"state": "ok"})["state"], "ok")
            self.assertEqual(rows.get("stale_comment_kits", {}).get("next"), None)

    def test_directory_error_precedence_and_contact_controls_remain(self):
        with self.assertRaises(ValueError) as caught:
            checklist(stale(FORGED), "bad\ndir")
        self.assertIn("checklist directory contains", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            checklist({"stale_contacts": [{"agency_id": FORGED}], **stale("m1")}, "D")
        self.assertIn("checklist stale contact agency_id", str(caught.exception))
        rows = {r["key"]: r for r in checklist({**stale("m1"), "extra_flag": False}, "D")}
        self.assertEqual(rows["extra_flag"]["state"], "info")


class RealChainTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-controls-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        (self.base / "canary.txt").write_text("synthetic canary", encoding="utf-8")

    def cli(self, *args):
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.base))
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=120)

    def meeting(self, stem, **extra):
        document = {"body": "Cedar City Council (fictional)", "starts_at": "2026-10-14T18:00:00-07:00",
                    "comment_kit_md": KIT, **extra}
        (self.meetings / f"{stem}.json").write_text(json.dumps(document), encoding="utf-8")

    def clear(self):
        for path in self.meetings.glob("*.json"):
            if path.name != "2026-10-cedar-council.json":
                path.unlink()

    def doctor(self, *extra):
        return self.cli("doctor", "--directory", str(self.root), *extra)

    def test_real_no_id_filename_reaches_guard(self):
        for stem in (FORGED, "synthetic\x1b[2J", "synthetic\rcr", "synthetic\ttab", "synthetic\x85nel",
                     "synthetic\u2028[ok] FORGED", "synthetic\u2029para"):
            with self.subTest(stem=ascii(stem)):
                self.clear()
                self.meeting(stem)
                report = doctor_report(self.root)
                self.assertEqual([e["meeting"] for e in report["stale_comment_kits"]], [stem])
                with self.assertRaises(ValueError) as caught:
                    render(report, self.root)
                self.assertEqual(str(caught.exception), MESSAGE)
                plain = self.doctor()
                self.assertEqual(plain.returncode, 0, plain.stderr)
                self.assertEqual(json.loads(plain.stdout)["stale_comment_kits"][0]["meeting"], stem)

    def test_real_cli_text_and_json_refuse_without_partial_report(self):
        self.meeting("a-safe")
        self.meeting("b-safe")
        self.meeting("z-late\nFORGED")
        before = snapshot(self.base)
        for extra in (("--checklist",), ("--checklist", "--json")):
            with self.subTest(extra=extra):
                done = self.doctor(*extra)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "Stopped: " + MESSAGE + "\n"))
        self.assertEqual(snapshot(self.base), before)

    def test_safe_explicit_id_keeps_control_filename_outside_display_scope(self):
        self.meeting("odd\nname", id="safe-id")
        plain = json.loads(self.doctor().stdout)
        self.assertEqual([e["meeting"] for e in plain["stale_comment_kits"]], ["safe-id"])
        for extra in (("--checklist",), ("--checklist", "--json")):
            done = self.doctor(*extra)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("--id safe-id --replace", done.stdout)
            self.assertNotIn("odd", done.stdout)

    def test_cli_success_is_unchanged_nonsticky_and_read_only(self):
        self.meeting("m-two")
        self.meeting("m-one")
        before = snapshot(self.base)
        good = self.doctor("--checklist")
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertIn("`python3 -m campaign_tool meetings-stage --directory " + str(self.root)
                      + " --id m-one --replace`; `python3 -m campaign_tool meetings-stage --directory "
                      + str(self.root) + " --id m-two --replace`", good.stdout)
        self.assertEqual(snapshot(self.base), before)
        self.meeting("m\nbad")
        self.assertEqual(self.doctor("--checklist").returncode, 1)
        (self.meetings / "m\nbad.json").unlink()
        again = self.doctor("--checklist")
        self.assertEqual((again.returncode, again.stdout), (0, good.stdout))


if __name__ == "__main__":
    unittest.main()
