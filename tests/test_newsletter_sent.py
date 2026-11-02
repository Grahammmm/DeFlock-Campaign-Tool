"""newsletter --record-sent: a hand-sent issue is recorded privately and starts the next window. Synthetic; nothing is sent."""
import contextlib
import hashlib
import io
import json
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool.newsletter.__main__ import main  # noqa: E402
from campaign_tool.newsletter.sent import last_sent, record_sent  # noqa: E402

SITE = {"schema_version": 1, "tagline": "Synthetic records.", "about_md": "Synthetic campaign.",
        "base_url": "https://synthetic.example.invalid"}
UNREADABLE = "kit/newsletter-sent.json unreadable: "


def finding(slug, published_at):
    return {"id": "fnd_" + slug, "title": "Synthetic finding " + slug, "slug": slug,
            "summary": "A synthetic record about " + slug + ".", "classification": "documented_fact",
            "confidence": "verified", "event_date": "2026-01-15",
            "sources": [{"sha256": "ab" * 32, "locator": "page:1", "title": "Synthetic record"}],
            "published_at": published_at, "corrections": [], "state": "published",
            "author": "synthetic-author", "limitations": ["Synthetic fixture."], "counterevidence": []}


class NewsletterSentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-newsletter-sent-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "init", "--directory", str(self.root), "--name",
                        "Synthetic", "--county", "Synthetic", "--state", "CA"], cwd=REPO_ROOT, check=True,
                       capture_output=True, timeout=30)
        content = self.root / "content"
        (content / "findings").mkdir(parents=True)
        (content / "site.json").write_text(json.dumps(SITE))
        for slug, when in (("early", "2026-03-01T18:00:00+00:00"), ("late", "2026-03-10T18:00:00+00:00")):
            (content / "findings" / (slug + ".json")).write_text(json.dumps(finding(slug, when)))
        self.kit = self.root / "kit"
        self.sent, self.draft = self.kit / "newsletter-sent.json", self.kit / "newsletter-draft.json"
        out = self.ok("--from-content")
        self.assertEqual((out["since_from"], out["counts"]["findings"]), (None, 2))

    def run_main(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network call")) as net, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                code = main(["--directory", str(self.root), *extra])
            except SystemExit as exc:
                code = exc.code
        self.assertFalse(net.called, "no network call")
        return code, stdout.getvalue(), stderr.getvalue()

    def ok(self, *extra):
        code, out, err = self.run_main(*extra)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def refused(self, message, *extra):
        code, out, err = self.run_main(*extra)
        self.assertEqual((code, out), (1, ""), err)
        self.assertIn("Stopped: " + message, err)

    def snapshot(self):
        return self.sent.read_bytes() if self.sent.is_file() and not self.sent.is_symlink() else None

    def test_record_then_from_content_starts_the_day_after_and_is_private(self):
        digest = hashlib.sha256(self.draft.read_bytes()).hexdigest()
        subject = json.loads(self.draft.read_text())["subject"]
        out = self.ok("--record-sent", "2026-03-05")
        self.assertEqual((set(out), out["sent"]), ({"recorded", "sent"}, False))
        self.assertEqual({k: out["recorded"][k] for k in ("sent_on", "draft_sha256", "subject")},
                         {"sent_on": "2026-03-05", "draft_sha256": digest, "subject": subject})
        document = json.loads(self.sent.read_text())
        self.assertEqual((document["schema_version"], document["issues"]), (1, [out["recorded"]]))
        self.assertEqual(stat.S_IMODE(self.sent.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.kit.stat().st_mode), 0o700)
        self.assertEqual(sorted(p.name for p in self.kit.glob(".*.tmp")), [])
        self.assertEqual(last_sent(self.root), "2026-03-05")
        out = self.ok("--from-content")
        self.assertEqual((out["since_from"], out["counts"]["findings"]), ("record", 1))
        self.assertEqual(out["excluded"]["findings_before_since"], 1)
        text = (self.kit / "newsletter-draft.txt").read_text()
        self.assertIn("since 2026-03-06", text)
        self.assertNotIn("finding early", text)

    def test_explicit_since_wins_over_the_record(self):
        self.ok("--record-sent", "2026-03-05")
        out = self.ok("--from-content", "--since", "2026-02-01")
        self.assertEqual((out["since_from"], out["counts"]["findings"]), ("flag", 2))
        self.assertIn("since 2026-02-01", (self.kit / "newsletter-draft.txt").read_text())
        self.assertIsNone(self.ok()["since_from"], "template output names no window")

    def test_future_impossible_and_too_early_dates_are_refused_and_change_nothing(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        cases = ((tomorrow, f"sent date {tomorrow} is after today"),
                 ("2026-02-30", "sent date '2026-02-30' is not a real YYYY-MM-DD day"),
                 ("2026-3-05", "sent date '2026-3-05' is not a real YYYY-MM-DD day"),
                 ("1999-12-31", "sent date 1999-12-31 is before 2000-01-01"))
        for value, message in cases:
            self.refused(message, "--record-sent", value)
            self.assertFalse(self.sent.exists(), value)
        self.ok("--record-sent", "2026-03-05")
        before = self.snapshot()
        for value, message in cases:
            self.refused(message, "--record-sent", value)
            self.assertEqual(self.snapshot(), before, value)
        with self.assertRaisesRegex(ValueError, "sent date 2026-03-06 is after today \\(2026-03-05\\)"):
            record_sent(self.root, "2026-03-06", today=date(2026, 3, 5))
        self.assertEqual(self.snapshot(), before)

    def test_missing_linked_or_oversized_draft_is_refused(self):
        real = self.draft.read_bytes()
        self.draft.unlink()
        self.refused("no draft to record: kit/newsletter-draft.json is missing", "--record-sent", "2026-03-05")
        outside = self.tmp / "outside-draft.json"
        outside.write_bytes(real)
        self.draft.symlink_to(outside)
        self.refused("kit/newsletter-draft.json must not be a symlink", "--record-sent", "2026-03-05")
        self.draft.unlink()
        self.draft.write_bytes(b" " * (4 * 1024 * 1024 + 1))
        self.refused("kit/newsletter-draft.json is larger than 4194304 bytes", "--record-sent", "2026-03-05")
        self.draft.write_text("[]\n")
        self.refused("kit/newsletter-draft.json is not a newsletter draft", "--record-sent", "2026-03-05")
        self.assertFalse(self.sent.exists())
        self.assertEqual(outside.read_bytes(), real)

    def test_duplicate_draft_and_out_of_order_date_are_refused(self):
        self.ok("--record-sent", "2026-03-05")
        before = self.snapshot()
        self.refused("this draft was already recorded as sent on 2026-03-05", "--record-sent", "2026-03-09")
        self.assertEqual(self.snapshot(), before)
        self.ok("--from-content")  # a new draft: only the late finding
        self.refused("sent date 2026-03-04 is before the last recorded send (2026-03-05)", "--record-sent", "2026-03-04")
        self.assertEqual(self.snapshot(), before)
        self.ok("--record-sent", "2026-03-05")  # a second issue the same day keeps date order
        issues = json.loads(self.sent.read_text())["issues"]
        self.assertEqual([i["sent_on"] for i in issues], ["2026-03-05", "2026-03-05"])
        self.assertNotEqual(issues[0]["draft_sha256"], issues[1]["draft_sha256"])
        self.refused("this draft was already recorded as sent on 2026-03-05", "--record-sent", "2026-03-11")

    def test_linked_folder_or_malformed_record_file_is_refused_never_rewritten(self):
        target = self.tmp / "outside.json"
        target.write_text('{"schema_version": 1, "issues": []}\n')
        valid = {"sent_on": "2026-03-05", "draft_sha256": "ab" * 32, "subject": "S", "recorded_at": "x"}
        bad_files = {"not json": b"{not json\n",
                     "wrong schema": b'{"schema_version": 2, "issues": []}\n',
                     "bad issue": json.dumps({"schema_version": 1, "issues": [dict(valid, draft_sha256="x")]}).encode(),
                     "out of order": json.dumps({"schema_version": 1, "issues": [
                         valid, dict(valid, sent_on="2026-03-01", draft_sha256="cd" * 32)]}).encode(),
                     "too large": b" " * (1024 * 1024 + 1)}
        cases = [("symlink", lambda: self.sent.symlink_to(target), "the file must not be a symlink"),
                 ("folder", lambda: self.sent.mkdir(), "the file is not a regular file")]
        cases += [(name, lambda data=data: self.sent.write_bytes(data), "") for name, data in bad_files.items()]
        for name, plant, message in cases:
            with self.subTest(name):
                plant()
                before = self.snapshot()
                for args in (("--record-sent", "2026-03-06"), ("--from-content",)):
                    self.refused(UNREADABLE + message, *args)
                    self.assertEqual(self.snapshot(), before)
                with self.assertRaisesRegex(ValueError, "^" + UNREADABLE):
                    last_sent(self.root)
                self.assertEqual((self.sent.is_symlink(), self.sent.is_dir()), (name == "symlink", name == "folder"))
                self.sent.rmdir() if name == "folder" else self.sent.unlink()
        self.assertEqual(target.read_text(), '{"schema_version": 1, "issues": []}\n')

    def test_append_that_would_exceed_the_size_limit_is_refused_and_keeps_the_archive(self):
        # A valid archive just under 1 MiB in UTF-8 bytes (2-byte characters, so bytes, not characters, count).
        issue = {"sent_on": "2026-03-01", "draft_sha256": "ab" * 32, "subject": "", "recorded_at": "x"}
        empty = len((json.dumps({"schema_version": 1, "issues": [issue]}, indent=2, ensure_ascii=False) + "\n").encode())
        issue["subject"] = "\u00e9" * ((1024 * 1024 - 64 - empty) // 2)
        self.sent.write_text(json.dumps({"schema_version": 1, "issues": [issue]}, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        before = self.snapshot()
        self.assertLessEqual(self.sent.stat().st_size, 1024 * 1024)
        self.assertEqual(last_sent(self.root), "2026-03-01")
        self.refused("kit/newsletter-sent.json would exceed 1048576 bytes; nothing recorded", "--record-sent", "2026-03-05")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(p.name for p in self.kit.glob(".*.tmp")), [])
        self.assertEqual(last_sent(self.root), "2026-03-01", "the archive still loads")

    def test_record_sent_cannot_be_combined_with_drafting(self):
        for extra in (("--manifest", str(self.tmp / "m.json")), ("--from-content",), ("--since", "2026-03-01")):
            with self.subTest(extra):
                code, out, err = self.run_main("--record-sent", "2026-03-05", *extra)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("usage:", err)
        self.assertFalse(self.sent.exists())


if __name__ == "__main__":
    unittest.main()
