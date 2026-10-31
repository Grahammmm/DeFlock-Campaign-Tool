"""meetings-stage: one kit/meetings.json entry into content/meetings/<id>.json (synthetic only)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import cli
from campaign_tool.content import ContentError, load_content
from campaign_tool.meeting_stage import stage

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "legistar-events.json"
MID = "legistar-examplecity-1001"


def run(*args):
    return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True, timeout=60)


def snapshot(folder):
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir())} if folder.is_dir() else None


class MeetingStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-meeting-stage-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.meetings = self.root / "content" / "meetings"
        self.target = self.meetings / f"{MID}.json"

    def kit_from_fixture(self):
        result = run("meetings", "--directory", str(self.root), "--client", "examplecity",
                     "--from-json", str(FIXTURE), "--since", "2026-09-30")
        self.assertEqual(result.returncode, 0, result.stderr)

    def write_kit(self, meetings, raw=None):
        (self.root / "kit").mkdir(exist_ok=True)
        path = self.root / "kit" / "meetings.json"
        if raw is not None:
            path.write_bytes(raw)
        else:
            path.write_text(json.dumps({"schema_version": 1, "meetings": meetings}), encoding="utf-8")

    def stage_cli(self, *extra, root=None):
        return run("meetings-stage", "--directory", str(root or self.root), "--id", MID, *extra)

    def assert_stopped(self, result, *needles):
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("Stopped:", result.stderr)
        for needle in needles:
            self.assertIn(needle, result.stderr)

    def test_happy_path_writes_validated_meeting_and_builds(self):
        self.kit_from_fixture()
        result = self.stage_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["findings_cited"], 1)
        self.assertEqual(report["written"], f"content/meetings/{MID}.json")
        self.assertFalse(report["replaced"])
        self.assertIn("verify the posted agenda", report["note"])
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertLessEqual(set(document), {"id", "body", "starts_at", "agenda_url", "agenda_item", "comment_kit_md"})
        model = load_content(self.root)
        staged = [m for m in model["meetings"] if m["id"] == MID]
        self.assertEqual(len(staged), 1)
        finding = model["findings"][0]
        self.assertIn(finding["summary"], staged[0]["comment_kit_md"])
        self.assertIn(f"/findings/{finding['slug']}.html", staged[0]["comment_kit_md"])
        build = run("build", "--directory", str(self.root))
        self.assertEqual(build.returncode, 0, build.stderr)
        page = (self.root / "public" / "meetings.html").read_text(encoding="utf-8")
        self.assertIn(f"meeting-{MID}", page)

    def test_agency_filter_without_findings_says_none_published(self):
        self.kit_from_fixture()
        result = self.stage_cli("--agency-id", "ca-nowhere-police", "--location", "Synthetic Hall")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["findings_cited"], 0)
        document = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertIn("No published findings yet", document["comment_kit_md"])
        self.assertEqual(document["agency_id"], "ca-nowhere-police")
        self.assertEqual(document["location"], "Synthetic Hall")

    def test_missing_symlinked_or_malformed_kit_refused(self):
        before = snapshot(self.meetings)
        self.assert_stopped(self.stage_cli(), "kit/meetings.json not found", "run `meetings` first")
        (self.root / "kit").mkdir(exist_ok=True)
        real = self.tmp / "elsewhere.json"
        real.write_text(json.dumps({"schema_version": 1, "meetings": [{"id": MID}]}), encoding="utf-8")
        os.symlink(real, self.root / "kit" / "meetings.json")
        self.assert_stopped(self.stage_cli(), "kit/meetings.json must not be a symlink")
        (self.root / "kit" / "meetings.json").unlink()
        for raw, needle in ((b'{"schema_version": 2}', "schema_version"), (b"\xff\xfe not json", "not UTF-8 JSON"),
                            (b'{"schema_version": 1, "meetings": {}}', "meetings must be a list")):
            self.write_kit(None, raw=raw)
            self.assert_stopped(self.stage_cli(), "kit/meetings.json:", needle)
        self.write_kit(None, raw=b" " * (4 * 1024 * 1024 + 1))
        self.assert_stopped(self.stage_cli(), "kit/meetings.json:", "4 MiB")
        self.assertEqual(snapshot(self.meetings), before)

    def test_unknown_id_lists_known_ids(self):
        self.kit_from_fixture()
        result = run("meetings-stage", "--directory", str(self.root), "--id", "legistar-examplecity-9999")
        self.assert_stopped(result, "no meeting legistar-examplecity-9999", MID, "legistar-examplecity-1003")
        self.assertFalse(any(self.meetings.glob("legistar-*")))

    def test_campaign_without_content_refused(self):
        fresh = self.tmp / "fresh"
        init = run("init", "--directory", str(fresh), "--name", "Synthetic", "--county", "Synthetic", "--state", "CA")
        self.assertEqual(init.returncode, 0, init.stderr)
        (fresh / "kit").mkdir(exist_ok=True)
        (fresh / "kit" / "meetings.json").write_text(json.dumps(
            {"schema_version": 1, "meetings": [{"id": MID, "body": "B", "starts_at": "2026-10-14T18:00:00"}]}))
        self.assert_stopped(self.stage_cli(root=fresh), "content/ is required")
        self.assertFalse((fresh / "content").exists())

    def test_existing_target_needs_replace_and_symlink_target_always_refused(self):
        self.kit_from_fixture()
        self.meetings.mkdir(exist_ok=True)
        self.target.write_text('{"body": "Old", "starts_at": "2026-10-01T18:00:00"}\n', encoding="utf-8")
        old = self.target.read_bytes()
        self.assert_stopped(self.stage_cli(), "exists; pass --replace")
        self.assertEqual(self.target.read_bytes(), old)
        result = self.stage_cli("--replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["replaced"])
        self.assertNotEqual(self.target.read_bytes(), old)
        self.target.unlink()
        outside = self.tmp / "outside.json"
        outside.write_text('{"body": "Linked", "starts_at": "2026-10-01T18:00:00"}\n', encoding="utf-8")
        linked = outside.read_bytes()
        os.symlink(outside, self.target)
        self.assert_stopped(self.stage_cli("--replace"), f"{MID}.json", "symlink")
        self.assertEqual(outside.read_bytes(), linked)
        self.assertTrue(self.target.is_symlink())

    def test_duplicate_id_in_other_file_refused(self):
        self.kit_from_fixture()
        other = self.meetings / "other.json"
        other.write_text(json.dumps({"id": MID, "body": "B", "starts_at": "2026-10-14T18:00:00"}), encoding="utf-8")
        self.assert_stopped(self.stage_cli("--replace"), f"id {MID} is already used by meetings/other.json")
        self.assertFalse(self.target.exists())

    def test_symlinked_meetings_folder_refused(self):
        self.kit_from_fixture()
        outside = self.tmp / "outside-meetings"
        shutil.move(str(self.meetings), outside)
        os.symlink(outside, self.meetings)
        before = snapshot(outside)
        self.assert_stopped(self.stage_cli(), "meetings/ must not be a symlink")
        self.assertEqual(snapshot(outside), before)

    def test_ids_invalid_for_content_refused_before_write(self):
        before = snapshot(self.meetings)
        for bad in ("legistar-" + "x" * 61, "legistar-Examplecity-1001"):
            self.write_kit([{"id": bad, "body": "B", "starts_at": "2026-10-14T18:00:00"}])
            with self.assertRaises(ContentError) as caught:
                stage(self.root, bad)
            self.assertIn(".id", str(caught.exception))
            self.assertEqual(snapshot(self.meetings), before)
        self.write_kit([{"id": MID, "body": "B", "starts_at": "2026-10-14T18:00:00"}])
        with self.assertRaises(ContentError):
            stage(self.root, MID, agency_id="Not An Agency")
        self.assertEqual(snapshot(self.meetings), before)

    def test_angle_brackets_stripped_and_accepted(self):
        self.write_kit([{"id": MID, "body": "<b>Council</b>", "starts_at": "2026-10-14T18:00:00",
                         "agenda_item": "Item <script>7</script>: ALPR", "agenda_url": "https://example.invalid/a"}])
        report = stage(self.root, MID)
        self.assertEqual(report["findings_cited"], 1)
        text = self.target.read_text(encoding="utf-8")
        self.assertNotIn("<", text)
        self.assertNotIn(">", text)
        staged = [m for m in load_content(self.root)["meetings"] if m["id"] == MID][0]
        self.assertEqual(staged["body"], "bCouncil/b")
        self.assertEqual(staged["agenda_url"], "https://example.invalid/a")

    def test_write_failure_leaves_no_target_or_temp(self):
        self.kit_from_fixture()
        before = snapshot(self.meetings)
        stderr = io.StringIO()
        argv = ["campaign_tool", "meetings-stage", "--directory", str(self.root), "--id", MID]
        with mock.patch.object(sys, "argv", argv), mock.patch("campaign_tool.kit.os.replace",
                                                               side_effect=OSError("synthetic disk full")), \
                contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                cli.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Stopped: synthetic disk full", stderr.getvalue())
        self.assertFalse(self.target.exists())
        self.assertEqual(snapshot(self.meetings), before)
        self.assertFalse([p for p in self.meetings.iterdir() if p.name.endswith(".tmp")])


if __name__ == "__main__":
    unittest.main()
