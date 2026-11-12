"""RF1721-4: the offline launch rehearsal keeps validated supporter consent (synthetic only; no network)."""
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import launch_check, supporters
from campaign_tool.content import load_content
from campaign_tool.launch_check import CONSENT_STAGE, _supporter_consent_bytes, launch_checks
from campaign_tool.meeting_stage import stage
from campaign_tool.site import build_site as real_build_site
from campaign_tool.supporters import add_supporter, set_cosign_consent
from tests import test_meeting_cosign as cosign

REPO = Path(__file__).resolve().parents[1]
TODAY = cosign.TODAY
NAME = "Zed Alpha Alliance"
SECRET = "Private How Note"


def fixture(test):
    temp = tempfile.TemporaryDirectory(prefix="synthetic-launch-consent-")
    test.addCleanup(temp.cleanup)
    test.tmp = Path(temp.name)
    test.root = test.tmp / "campaign"
    shutil.copytree(cosign.EXAMPLE, test.root, ignore=shutil.ignore_patterns("public"))
    (test.root / "kit").mkdir(exist_ok=True)
    (test.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
        {"id": mid, "body": "Example City Council", "starts_at": starts, "agenda_item": "Item 7: ALPR",
         "agenda_url": "https://example.invalid/agenda"} for mid, starts in cosign.STARTS.items()]}), encoding="utf-8")
    test.private_file = test.root / "kit" / "supporters-consent.json"
    patcher = mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network used"))
    patcher.start()
    test.addCleanup(patcher.stop)


def add(test, name=NAME, **fields):
    values = dict(name=name, kind="community", organization=True, confirmed_on=cosign.LISTING_DATE, how="email",
                  today=TODAY)
    values.update(fields)
    return add_supporter(test.root, **values)["id"]


def rows_of(report):
    return {row["check"]: row for row in report["checks"]}


class Capture:
    """Delegating build_site seam: records the staged consent file before running the real builder."""

    def __init__(self):
        self.calls, self.consent, self.mode, self.kit_mode = 0, None, None, None
        self.scratch_dirs = []

    def __call__(self, scratch):
        self.calls += 1
        self.scratch_dirs.append(scratch)
        path = Path(scratch) / "kit" / "supporters-consent.json"
        if path.exists():
            self.consent = json.loads(path.read_text(encoding="utf-8"))
            self.mode = stat.S_IMODE(path.stat().st_mode)
            self.kit_mode = stat.S_IMODE(path.parent.stat().st_mode)
        return real_build_site(scratch)


class ConsentRehearsalTests(unittest.TestCase):
    def setUp(self):
        fixture(self)

    def rehearse(self, hook=None):
        capture = Capture()
        with mock.patch.object(launch_check, "build_site", capture):
            if hook:
                hook(capture)
            report = launch_checks(self.root, today=TODAY)
        return report, capture

    def test_consented_supporter_direct_build_and_rehearsal_agree(self):
        sid = add(self, how="letter")
        load_content(self.root, today=TODAY)
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        report, capture = self.rehearse()
        rows = rows_of(report)
        self.assertEqual((rows["build_check"]["state"], rows["links"]["state"]), ("ok", "ok"))
        self.assertEqual(capture.mode, 0o600)
        self.assertEqual(capture.kit_mode, 0o700)
        record = json.loads(self.private_file.read_text(encoding="utf-8"))["supporters"][sid]
        self.assertEqual(capture.consent, {"schema_version": 1, "supporters": {sid: record}})
        self.assertEqual(record["how"], "letter")
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, before)
        text = json.dumps(report)
        self.assertNotIn(record["public_sha256"], text)
        self.assertFalse(Path(capture.scratch_dirs[0]).exists())

    def test_cosigned_meeting_rehearsal_keeps_exact_explicit_grant(self):
        a, b = add(self, "Zed Alpha Alliance"), add(self, "Zed Beta Alliance")
        set_cosign_consent(self.root, a, cosign=[cosign.MID], confirmed_on=cosign.GRANT_DATE, how="letter", today=TODAY)
        set_cosign_consent(self.root, b, cosign=["any"], confirmed_on=cosign.GRANT_DATE, how="form", today=TODAY)
        part = self.tmp / "part.json"
        document = json.loads(json.dumps(cosign.PART))
        part.write_text(json.dumps(document), encoding="utf-8")
        stage(self.root, cosign.MID, participation=str(part), today=TODAY, cosign_ids=[a, b])
        load_content(self.root, today=TODAY)
        report, capture = self.rehearse()
        self.assertEqual(rows_of(report)["build_check"]["state"], "ok")
        original = json.loads(self.private_file.read_text(encoding="utf-8"))["supporters"]
        self.assertEqual(capture.consent["supporters"], original)
        self.assertEqual(capture.consent["supporters"][a]["cosign"], [cosign.MID])
        self.assertEqual(capture.consent["supporters"][b]["cosign"], "any")
        # A narrowed grant is preserved and refused by the real builder, never reinstated from public content.
        set_cosign_consent(self.root, a, cosign=[cosign.MID2], confirmed_on=cosign.GRANT_DATE, how="letter", today=TODAY)
        report, capture = self.rehearse()
        self.assertEqual(rows_of(report)["build_check"]["state"], "fail")
        self.assertEqual(capture.calls, 0)  # initial admission refuses first
        # Listing alone never becomes co-sign permission.
        set_cosign_consent(self.root, a, clear=True, today=TODAY)
        report, _ = self.rehearse()
        self.assertEqual(rows_of(report)["build_check"]["state"], "fail")

    def test_absent_and_empty_supporters_skip_secondary_consent_read(self):
        with mock.patch.object(supporters, "read_consent", side_effect=AssertionError("read")) as spy:
            self.assertIsNone(_supporter_consent_bytes(self.root, None))
            self.assertIsNone(_supporter_consent_bytes(self.root, []))
            report, capture = self.rehearse()
        self.assertEqual(rows_of(report)["build_check"]["state"], "ok")
        self.assertIsNone(capture.consent)
        spy.assert_not_called()

    def test_secondary_missing_or_changed_consent_refuses_without_fabrication(self):
        sid = add(self)
        good = self.private_file.read_text(encoding="utf-8")

        def mutate(change):
            def hook(capture):
                original = launch_check._supporter_consent_bytes

                def after_admission(root, rows, today=None, limit=None):
                    document = json.loads(good)
                    change(document["supporters"], sid)
                    self.private_file.write_text(json.dumps(document), encoding="utf-8")
                    os.chmod(self.private_file, 0o600)
                    return original(root, rows, today, limit)
                mock.patch.object(launch_check, "_supporter_consent_bytes", after_admission).start()
            return hook

        self.addCleanup(mock.patch.stopall)
        changes = {"missing": lambda s, i: s.pop(i),
                   "hash": lambda s, i: s[i].update(public_sha256="0" * 64),
                   "date": lambda s, i: s[i].update(confirmed_on="2026-09-02")}
        for label, change in changes.items():
            with self.subTest(label):
                mock.patch.stopall()
                self.private_file.write_text(good, encoding="utf-8")
                os.chmod(self.private_file, 0o600)
                report, capture = self.rehearse(mutate(change))
                self.assertEqual(rows_of(report)["build_check"]["detail"], CONSENT_STAGE)
                self.assertEqual(rows_of(report)["links"]["state"], "fail")
                self.assertEqual(capture.calls, 0)
        mock.patch.stopall()
        self.private_file.unlink()
        report, capture = self.rehearse()  # missing store at initial admission: existing behaviour
        self.assertEqual(rows_of(report)["build_check"]["state"], "fail")
        self.assertEqual(capture.calls, 0)

    def test_secondary_unsafe_or_invalid_consent_never_follows_or_leaks(self):
        add(self)
        good = self.private_file.read_bytes()
        outside = self.tmp / "outside-sentinel.json"
        outside.write_bytes(good)

        def break_store(prepare):
            def hook(capture):
                original = launch_check._supporter_consent_bytes

                def wrapper(root, rows, today=None, limit=None):
                    prepare()
                    return original(root, rows, today, limit)
                mock.patch.object(launch_check, "_supporter_consent_bytes", wrapper).start()
            return hook

        def link():
            self.private_file.unlink()
            self.private_file.symlink_to(outside)

        def loose():
            os.chmod(self.private_file, 0o640)

        def malformed():
            self.private_file.write_bytes(b"{not json")

        def oversized():
            self.private_file.write_bytes(b" " * (supporters.MAX_BYTES + 1))

        self.addCleanup(mock.patch.stopall)
        for label, prepare in (("link", link), ("mode", loose), ("malformed", malformed), ("oversized", oversized)):
            with self.subTest(label):
                mock.patch.stopall()
                if self.private_file.is_symlink():
                    self.private_file.unlink()
                self.private_file.write_bytes(good)
                os.chmod(self.private_file, 0o600)
                report, capture = self.rehearse(break_store(prepare))
                self.assertEqual(rows_of(report)["build_check"]["detail"], CONSENT_STAGE)
                self.assertEqual(capture.calls, 0)
        mock.patch.stopall()
        self.private_file.unlink() if self.private_file.is_symlink() else None
        self.private_file.write_bytes(good)
        os.chmod(self.private_file, 0o600)
        # OSError at the secondary read reaches the fixed BUILD_FS handler.
        with mock.patch.object(supporters, "read_consent", side_effect=OSError("/secret/path boom")):
            report, capture = self.rehearse()
        self.assertEqual(rows_of(report)["build_check"]["detail"], launch_check.BUILD_FS)
        self.assertNotIn("secret", json.dumps(report))
        self.assertEqual(capture.calls, 0)

    def test_projection_limit_unicode_and_selection_are_exact(self):
        sid = add(self, "Zed Ünï Alliance")
        records = supporters.read_consent(self.root, today=TODAY)
        rows = load_content(self.root, today=TODAY)["supporters"]
        data = _supporter_consent_bytes(self.root, rows, TODAY)
        self.assertTrue(data.endswith(b"}\n"))
        self.assertEqual(json.loads(data), {"schema_version": 1, "supporters": {sid: records[sid]}})
        self.assertEqual(data, (json.dumps({"schema_version": 1, "supporters": {sid: records[sid]}},
                                           ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        self.assertEqual(_supporter_consent_bytes(self.root, rows, TODAY, limit=len(data)), data)
        with self.assertRaisesRegex(ValueError, "cannot stage validated consent"):
            _supporter_consent_bytes(self.root, rows, TODAY, limit=len(data) - 1)

    def test_rehearsal_copies_no_unrelated_private_material(self):
        kept = add(self)
        (self.root / "kit" / "sibling-sentinel.txt").write_text("private sibling", encoding="utf-8")
        (self.root / "private").mkdir(exist_ok=True)
        (self.root / "private" / "sentinel.txt").write_text("private tree", encoding="utf-8")
        orphan = add(self, "Zed Orphan Alliance")
        public = self.root / "content" / "supporters.json"
        document = json.loads(public.read_text(encoding="utf-8"))
        rows = document["supporters"] if isinstance(document, dict) else document
        document_kept = [r for r in rows if r["id"] != orphan]
        public.write_text(json.dumps(dict(document, supporters=document_kept) if isinstance(document, dict)
                                     else document_kept), encoding="utf-8")  # orphan stays only in the private store
        seen, staged = [], {}

        def spy(scratch):
            seen.extend(p.relative_to(scratch).as_posix() for p in Path(scratch).rglob("*"))
            staged.update(json.loads((Path(scratch) / "kit" / "supporters-consent.json").read_text())["supporters"])
            return real_build_site(scratch)
        with mock.patch.object(launch_check, "build_site", spy):
            launch_checks(self.root, today=TODAY)
        self.assertIn("kit/supporters-consent.json", seen)
        self.assertIn(orphan, json.loads(self.private_file.read_text(encoding="utf-8"))["supporters"])
        self.assertEqual(list(staged), [kept])
        self.assertFalse([p for p in seen if "sentinel" in p or p.startswith("private")])

    def test_actual_doctor_cli_and_cleanup_contracts_remain(self):
        sid = add(self, how="letter")
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "doctor", "--launch", "--directory",
                                 str(self.root)], cwd=REPO, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        launch = report["launch"]
        self.assertEqual(launch["total"], 9)
        rows = rows_of(launch)
        self.assertEqual((rows["build_check"]["state"], rows["links"]["state"]), ("ok", "ok"))
        for key in ("production_ready", "public_deployment_checked", "safe_to_send_automatically"):
            self.assertIs(report[key], False)
        record = json.loads(self.private_file.read_text(encoding="utf-8"))["supporters"][sid]
        self.assertNotIn(record["public_sha256"], result.stdout)
        # A genuine cleanup failure keeps the existing fixed refusal.
        with mock.patch.object(shutil, "rmtree", side_effect=OSError("/secret/path")):
            with self.assertRaisesRegex(ValueError, "could not be removed") as caught:
                launch_checks(self.root, today=TODAY)
        self.assertNotIn("secret", str(caught.exception))
        # RuntimeError is not converted into a consent refusal.
        with mock.patch.object(supporters, "read_consent", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                launch_checks(self.root, today=TODAY)


if __name__ == "__main__":
    unittest.main()
