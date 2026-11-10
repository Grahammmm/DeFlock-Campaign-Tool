"""DF1952-13: private never-sent reply drafts from a logged correction request's recorded state (synthetic, offline).

Run from the repository root: python3 -B -m unittest tests.test_correction_request_reply -v
"""
import json
import os
import stat
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import correction_requests as cr, findings  # noqa: E402
from tests import test_correction_requests as base  # noqa: E402  (module import: not re-collected here)

FID, TODAY, BANNER = base.FID, base.TODAY, "DRAFT — NOT SENT. Review and edit before sending it yourself."
UNIQUE = "SUMMARY-SENTINEL-77"


class ReplyTests(base.CorrectionRequestTests):
    # Reuse the synthetic campaign fixture; the inherited tests are dropped below so they do not run twice.
    def setUp(self):
        super().setUp()
        self.replies = self.root / "kit" / "corrections" / "replies"

    def correct_on(self, day):
        findings.correct(self.root, FID, day, "Fixed the quote.", now=datetime(2026, 3, 19, tzinfo=timezone.utc),
                         approved_by="Pat Organizer")

    def set_base(self, url):
        site = self.root / "content" / "site.json"
        document = json.loads(site.read_text())
        document["base_url"] = url
        site.write_text(json.dumps(document))

    def reply(self, n=1):
        return cr.reply(self.root, n)

    def text(self, n=1):
        return (self.replies / f"{n}.md").read_text(encoding="utf-8")

    def refuses(self, needle, n=1):
        before = self.file.read_bytes() if self.file.exists() else None
        with self.assertRaises(ValueError) as caught:
            self.reply(n)
        self.assertIn(needle, str(caught.exception))
        self.assertNotIn(str(self.root), str(caught.exception))
        self.assertEqual(self.file.read_bytes() if self.file.exists() else None, before)
        return str(caught.exception)

    def test_open_acknowledges_recorded_receipt(self):
        self.add(received="2026-03-06", summary=UNIQUE)
        before = self.file.read_bytes()
        self.set_base(None)
        self.assertFalse((self.root / "kit").exists())
        self.assertEqual(self.reply(), {"written": "kit/corrections/replies/1.md", "id": 1, "state": "open"})
        self.assertEqual(self.text(), "\n".join([
            "# Draft reply to correction request 1", "", BANNER, "", "[EDIT ME: add greeting outside this repository]", "",
            f"Subject: finding {FID}", "",
            "We received your correction request about this finding on 2026-03-06. It is being reviewed, and we will "
            "write again when a decision has been recorded.", "", "Finding: [EDIT ME: add the finding URL]", "",
            "[EDIT ME: sign-off]", ""]))
        self.assertEqual(self.file.read_bytes(), before)
        for part in ("kit", "kit/corrections", "kit/corrections/replies"):
            self.assertEqual(stat.S_IMODE((self.root / part).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.replies / "1.md").stat().st_mode), 0o600)
        self.assertNotIn(UNIQUE, self.text())

    def test_accepted_correction_not_decision_date(self):
        self.add(received="2026-03-06")
        self.correct_on("2026-03-08")
        self.decide(decision="accepted", on="2026-03-10", note="Quote fixed.")
        for url in ("https://example.org", "https://example.org/"):
            self.set_base(url)
            if (self.replies / "1.md").exists():
                (self.replies / "1.md").unlink()
            self.reply()
            text = self.text()
            self.assertIn("Decision date: 2026-03-10\nCorrection date: 2026-03-08\n", text)
            self.assertIn("Recorded decision note:\n> Quote fixed.\n", text)
            self.assertIn("[EDIT ME: confirm the correction addressed this request]", text)
            self.assertIn("Finding: https://example.org/findings/synthetic-title.html\n", text)  # slug, not the id
            self.assertNotIn(FID + ".html", text)
        # several qualifying corrections: no convenient pick
        self.correct_on("2026-03-09")
        (self.replies / "1.md").unlink()
        self.reply()
        self.assertIn("Correction date: [EDIT ME: confirm the correction date]", self.text())
        self.assertNotIn("Correction date: 2026-03-0", self.text())
        # a correction outside received..decided does not count: none qualifies
        self.add(received="2026-03-12", today=date(2026, 3, 20))
        self.correct_on("2026-03-13")
        self.decide(request_id=2, decision="accepted", on="2026-03-12", note="Later fix.")
        self.reply(2)
        self.assertIn("[EDIT ME: confirm the correction date]", self.text(2))
        self.assertIn("Decision date: 2026-03-12", self.text(2))

    def test_declined_quotes_recorded_reason(self):
        self.add(summary=UNIQUE)
        self.decide(note="Records support the text.\nSecond line {{x}} # not a heading")
        self.reply()
        text = self.text()
        self.assertIn("did not accept it", text)
        self.assertIn("Reason recorded:\n> Records support the text. Second line {{x}} # not a heading\n", text)
        self.assertNotIn(UNIQUE, text)
        self.assertIn("[EDIT ME: add greeting outside this repository]", text)
        self.assertIn("[EDIT ME: sign-off]", text)
        self.assertEqual(cr.list_requests(self.root, TODAY)[0]["state"], "declined")
        # a note with real line breaks (hand-edited log) is quoted line by line
        document = json.loads(self.file.read_text())
        document["requests"][0]["note"] = "one\ntwo"
        self.file.write_text(json.dumps(document))
        (self.replies / "1.md").unlink()
        self.reply()
        self.assertIn("Reason recorded:\n> one\n> two\n", self.text())

    def test_historical_finding_and_absent_base(self):
        self.add()
        self.decide()
        findings.withdraw(self.root, FID, "Synthetic withdrawal.")
        self.set_base("https://example.org")
        self.reply()
        self.assertIn("Finding: [EDIT ME: add the finding URL]", self.text())
        # accepted + finding gone: both markers
        document = json.loads(self.file.read_text())
        document["requests"][0].update(state="accepted", decided_on="2026-03-08", note="Fixed.")
        self.file.write_text(json.dumps(document))
        (self.replies / "1.md").unlink()
        self.reply()
        text = self.text()
        self.assertIn("[EDIT ME: confirm the correction date]", text)
        self.assertIn("[EDIT ME: add the finding URL]", text)
        # present but malformed content refuses and writes nothing
        (self.replies / "1.md").unlink()
        (self.content / "site.json").write_text("{not json")
        self.refuses("public content is unreadable")
        self.assertFalse((self.replies / "1.md").exists())

    def test_unknown_id_missing_log_and_corruption_write_nothing(self):
        self.refuses("missing, unreadable or invalid")  # no log at all
        self.assertFalse(self.file.exists())
        self.add()
        self.file.write_text('{"schema_version": 1, "requests": []}')
        self.refuses("no correction request 1")
        for bad in (0, -1, True, "1", 1.0):
            with self.assertRaises(ValueError):
                cr.reply(self.root, bad)
        self.file.unlink()
        self.add()
        self.add()
        good = json.loads(self.file.read_text())
        broken = json.loads(self.file.read_text())
        broken["requests"][1]["state"] = "accepted"  # unrelated row lacks decided_on/note
        for payload in (b"", b"\xff\xfe", b"[]", b"{", json.dumps(broken).encode(), b" " * (4 * 1024 * 1024 + 1)):
            self.file.write_bytes(payload)
            self.refuses("missing, unreadable or invalid")
        self.file.write_text(json.dumps(good))
        with mock.patch.object(cr, "_read_json", side_effect=OSError("boom")):
            self.refuses("missing, unreadable or invalid")
        self.assertFalse((self.root / "kit").exists())
        result = self.cli("correction-request", "reply", "--directory", self.root, "--id", "9", returncode=3)
        self.assertIn("no correction request 9", result.stderr)
        from subprocess import run
        for argv in (("--directory", str(self.root)), ("--id", "x", "--directory", str(self.root))):
            self.assertEqual(run([sys.executable, "-B", "-m", "campaign_tool", "correction-request", "reply", *argv],
                                 cwd=REPO_ROOT, capture_output=True, text=True).returncode, 2)

    def test_symlink_and_no_clobber(self):
        self.add()
        outside = self.base / "outside"
        outside.mkdir()
        # symlinked kit, dangling kit, symlinked deeper parents
        (self.root / "kit").symlink_to(outside)
        self.refuses("nothing was written")
        (self.root / "kit").unlink()
        (self.root / "kit").symlink_to(self.base / "nowhere")
        self.refuses("nothing was written")
        (self.root / "kit").unlink()
        (self.root / "kit" / "corrections").mkdir(parents=True)
        (self.root / "kit" / "corrections" / "replies").symlink_to(outside)
        self.refuses("nothing was written")
        (self.root / "kit" / "corrections" / "replies").unlink()
        (self.replies).mkdir()
        (self.replies / "1.md").symlink_to(outside / "target.md")  # dangling target link
        self.refuses("nothing was written")
        (self.replies / "1.md").unlink()
        (self.replies / "1.md").symlink_to(self.file)
        self.refuses("nothing was written")
        (self.replies / "1.md").unlink()
        os.mkfifo(self.replies / "1.md")
        self.refuses("already exists")
        (self.replies / "1.md").unlink()
        (self.replies / "1.md").mkdir()
        self.refuses("already exists")
        (self.replies / "1.md").rmdir()
        self.assertEqual(list(outside.iterdir()), [])
        # existing regular draft: identical or edited bytes are never replaced, mode kept
        self.reply()
        kept = self.text()
        self.assertEqual(self.refuses("edit it by hand"), "kit/corrections/replies/1.md already exists; "
                         "edit it by hand (an existing draft is never replaced)")
        (self.replies / "1.md").write_text("my edit\n")
        os.chmod(self.replies / "1.md", 0o640)
        self.refuses("edit it by hand")
        self.assertEqual(self.text(), "my edit\n")
        self.assertEqual(stat.S_IMODE((self.replies / "1.md").stat().st_mode), 0o640)
        self.assertNotEqual(kept, "my edit\n")
        # a competing creator wins the race: ours loses safely and keeps their bytes
        (self.replies / "1.md").unlink()
        real = os.open

        def racing(path, flags, *rest, **kwargs):
            if str(path).endswith("1.md"):
                real(path, os.O_WRONLY | os.O_CREAT, 0o600)
            return real(path, flags, *rest, **kwargs)

        with mock.patch.object(os, "open", racing):
            self.refuses("already exists")
        self.assertEqual(self.text(), "")

    def test_symlinked_parent_file_and_nondirectory(self):
        self.add()
        (self.root / "kit").write_text("a file")
        self.refuses("nothing was written")
        self.assertEqual((self.root / "kit").read_text(), "a file")
        (self.root / "kit").unlink()
        (self.private / "x").write_text("")
        link = self.root / "private_backup"
        link.symlink_to(self.private)
        self.reply()  # the log itself is untouched by a sibling link

    def test_private_write_failure_and_output(self):
        self.add()
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        for target, name in ((os, "fsync"), (os, "fchmod")):
            with mock.patch.object(target, name, side_effect=OSError("disk")):
                self.refuses("could not be written safely")
            self.assertFalse((self.replies / "1.md").exists())
        os.umask(0)
        before = self.file.read_bytes()
        result = self.cli("correction-request", "reply", "--directory", self.root, "--id", "1")
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads("\n".join(lines[:-1])), {"written": "kit/corrections/replies/1.md", "id": 1, "state": "open"})
        self.assertEqual(lines[-1], "Private draft only; nothing was sent or published.")
        self.assertNotIn("Page 2", result.stdout)
        self.assertNotIn("EDIT ME", result.stdout)
        self.assertEqual(stat.S_IMODE((self.replies / "1.md").stat().st_mode), 0o600)
        self.assertEqual(self.file.read_bytes(), before)
        help_text = self.cli("correction-request", "reply", "--help").stdout
        for flag in ("--to", "--name", "--email", "--replace"):
            self.assertNotIn(flag, help_text)
        self.assertEqual(self.cli("correction-request", "reply", "--directory", self.root, "--id", "1",
                                  returncode=3).stdout, "")

    def test_legacy_unused_and_counts_unchanged(self):
        self.add()
        self.add(received="2026-03-07")
        self.decide(request_id=2)
        log = self.file.read_bytes()
        listed = cr.list_requests(self.root, TODAY)
        counts = cr.quarterly_counts(self.root, TODAY)
        public = {str(p.relative_to(self.root)): p.read_bytes() for p in sorted((self.root / "content").rglob("*"))
                  if p.is_file()}
        with mock.patch("socket.socket", side_effect=AssertionError("network")):
            self.reply(1)
            self.reply(2)
        self.assertEqual(self.file.read_bytes(), log)
        self.assertEqual(cr.list_requests(self.root, TODAY), listed)
        self.assertEqual(cr.quarterly_counts(self.root, TODAY), counts)
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in sorted((self.root / "content").rglob("*"))
                          if p.is_file()}, public)
        self.assertEqual(sorted(p.name for p in self.replies.iterdir()), ["1.md", "2.md"])

    def test_present_nondirectory_content_refuses(self):
        self.add(received="2026-03-06")
        content = self.root / "content"
        for make in ("file", "dangling"):
            if content.is_symlink() or content.is_file():
                content.unlink()
            else:
                import shutil
                shutil.move(str(content), str(self.base / "content-moved"))
            if make == "file":
                content.write_text("not a directory")
            else:
                content.symlink_to(self.base / "nowhere")
            self.refuses("public content is unreadable")
            self.assertFalse((self.root / "kit").exists())

    def test_preflight_permission_error_is_domain_refusal(self):
        self.add(received="2026-03-06")
        real = os.lstat

        def deny(path, *a, **k):
            if str(path).endswith(os.path.join("kit", "corrections")):
                raise PermissionError(13, "denied", str(path))
            return real(path, *a, **k)

        (self.root / "kit" / "corrections").mkdir(parents=True)
        with mock.patch("campaign_tool.correction_requests.os.lstat", side_effect=deny):
            message = self.refuses("could not be checked safely")
        self.assertNotIn("denied", message)
        self.assertFalse(self.replies.exists())

    def test_duplicate_log_creates_no_output(self):
        self.add(received="2026-03-06")
        self.reply()  # a valid log first writes a draft (control)
        (self.replies / "1.md").write_text("hand edited", encoding="utf-8")
        self.file.write_text('{"schema_version": 1, "requests": [{"id": 1, "finding_id": "%s", "received_on": "2026-03-06", '
                             '"summary": "%s", "state": "declined", "state": "open"}]}' % (FID, UNIQUE), encoding="utf-8")
        before = {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}
        message = self.refuses("is missing, unreadable or invalid; no draft was written")
        self.assertNotIn("duplicate", message)
        self.assertNotIn(UNIQUE, message)
        self.assertEqual({str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()},
                         before)
        (self.replies / "1.md").unlink()
        (self.replies).rmdir()
        (self.root / "kit" / "corrections").rmdir()
        (self.root / "kit").rmdir()
        self.refuses("no draft was written")  # no kit tree is created on refusal
        self.assertFalse((self.root / "kit").exists())
        result = self.cli("correction-request", "reply", "--directory", self.root, "--id", "1", returncode=3)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)

    def test_reply_preflight_still_accepts_a_duplicate_free_log_and_refuses_non_finite(self):
        self.add(received="2026-03-06")
        self.assertEqual(self.reply()["state"], "open")
        (self.replies / "1.md").unlink()
        self.file.write_text(self.file.read_text().replace('"id": 1', '"id": NaN', 1), encoding="utf-8")
        self.refuses("no draft was written")


for _name in [n for n in dir(base.CorrectionRequestTests) if n.startswith("test_")]:
    setattr(ReplyTests, _name, None)  # inherited tests belong to tests.test_correction_requests

if __name__ == "__main__":
    unittest.main()
