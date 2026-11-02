"""kit-copy writes only the sendable part of a request draft to kit/outgoing/ (.txt or an unsent .eml); nothing is sent."""
import contextlib
import email
import email.policy
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_check, kit_copy  # noqa: E402

EXCLUDED = ("Statutory context", "Operator checklist", "DRAFT ONLY", "To:", "Agency:", "NO REVIEWED LAW PACKAGE")


def run(*arguments):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class KitCopyTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        smtp = mock.patch("smtplib.SMTP", side_effect=AssertionError("SMTP used"))
        smtp.start()
        self.addCleanup(smtp.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-copy-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        code, _, err = run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                           "--state", "CA")
        self.assertEqual(code, 0, err)
        code, _, err = run("kit", "--directory", self.root)
        self.assertEqual(code, 0, err)
        self.kit = self.root / "kit"
        self.out = self.kit / "outgoing"
        self.a = sorted(p.stem for p in (self.kit / "requests").glob("*.md"))[0]
        self.draft = self.kit / "requests" / f"{self.a}.md"

    def fill(self, verify=True, **contact):
        self.draft.write_text(kit_check.PLACEHOLDER.sub("synthetic value", self.draft.read_text(encoding="utf-8")),
                              encoding="utf-8")
        if verify:
            kit.verify_agency(self.root, self.a, source_url="https://example.org/records", checked_on="2026-01-02",
                              **(contact or {"records_email": "records@example.org"}))

    def copy(self, target, expect=0):
        code, out, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", target, "--json")
        self.assertEqual(code, expect, err)
        return json.loads(out) if expect == 0 else err

    def snapshot(self):
        return {str(p.relative_to(self.kit)): p.read_bytes() for p in self.kit.rglob("*")
                if p.is_file() and "outgoing" not in p.parts}

    def listing(self):
        return {p.name: (p.lstat().st_mode, p.lstat().st_mtime_ns) for p in self.out.iterdir()} if self.out.is_dir() \
            and not self.out.is_symlink() else None

    def assert_refused(self, target, reason):
        before = self.listing()
        err = self.copy(target, 3)
        self.assertIn("Stopped: ", err)
        self.assertIn(reason, err)
        self.assertEqual(self.listing(), before, "kit/outgoing changed on a refusal")
        return err

    def test_sendable_splits_subject_and_body_and_drops_the_rest(self):
        text = ("# Draft\n\nDRAFT ONLY. banner\n\nTo: x@example.org\nAgency: A\nSubject: Records of A\n\n"
                "Please provide.\n\n- item\n\nName\n\n## Statutory context (for the requester, not for sending)\n\n"
                "- note\n\nOperator checklist: record.\n")
        self.assertEqual(kit_copy.sendable(text), {"subject": "Records of A", "body": "Please provide.\n\n- item\n\nName"})
        self.assertEqual(kit_copy.sendable("Subject: S\nbody\nOperator checklist: x\nmore")["body"], "body")
        self.assertEqual(kit_copy.sendable("Subject: S\n\nbody runs to the end\n\n")["body"], "body runs to the end")
        with self.assertRaisesRegex(ValueError, "draft has no Subject line"):
            kit_copy.sendable("To: x\nRe: S\nbody\n")

    def test_combined_draft_portal_copy_holds_only_subject_and_body(self):
        self.fill()
        before = self.snapshot()
        result = self.copy("portal")
        written = (self.out / f"{self.a}.txt").read_text(encoding="utf-8")
        subject, body = written.split("\n\n", 1)
        body = body[:-1]
        self.assertTrue(written.endswith("\n") and not written.endswith("\n\n"))
        self.assertEqual(result, {"written": f"kit/outgoing/{self.a}.txt", "characters": len(subject) + len(body),
                                  "subject": subject})
        self.assertTrue(subject.startswith("Records concerning any automated license plate reader"), subject)
        self.assertIn("The date range for this request is synthetic value.", body)
        self.assertTrue(body.endswith("synthetic value"), body[-80:])
        for excluded in EXCLUDED:
            self.assertNotIn(excluded, written)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.out / f"{self.a}.txt").stat().st_mode), 0o600)
        self.assertEqual(self.snapshot(), before, "kit/requests, agencies.json or sent.json changed")
        self.assertFalse((self.kit / "sent.json").exists())
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [f"{self.a}.txt"], "temp file left behind")
        code, out, err = run("kit-copy", "--directory", self.root, "--agency", self.a, "--for", "portal")
        self.assertEqual(code, 0, err)
        self.assertIn(f"characters: {len(subject) + len(body)}", out.splitlines())
        self.assertIn("Nothing was sent.", err)

    def test_template_fallback_body_ends_after_the_requester_block(self):
        with tempfile.TemporaryDirectory(prefix="synthetic-") as empty, contextlib.redirect_stdout(io.StringIO()):
            kit.build_kit(self.root, law_base=Path(empty))
        self.assertNotIn("Statutory context", self.draft.read_text(encoding="utf-8"))
        self.fill(verify=False)
        self.copy("portal")
        written = (self.out / f"{self.a}.txt").read_text(encoding="utf-8")
        self.assertTrue(written.startswith("Records concerning ALPR use by "), written[:80])
        self.assertTrue(written.endswith("Rolling production is welcome.\n\nsynthetic value\n"), written[-120:])
        for excluded in EXCLUDED:
            self.assertNotIn(excluded, written)

    def test_placeholder_in_the_body_refuses_but_one_only_in_excluded_sections_does_not(self):
        text = self.draft.read_text(encoding="utf-8")
        self.fill()
        filled = self.draft.read_text(encoding="utf-8")
        self.draft.write_text(filled.replace("Rolling production", "[explicit approved cap, e.g. $50] Rolling"),
                              encoding="utf-8")
        err = self.assert_refused("portal", "1 unfilled placeholder(s) in the sendable part: "
                                  "[explicit approved cap, e.g. $50]")
        self.assertFalse(self.out.exists())
        self.draft.write_text(text, encoding="utf-8")  # a fresh draft: several, first three named
        err = self.assert_refused("email", "unfilled placeholder(s) in the sendable part: [")
        self.assertEqual(err.count("["), 3, err)
        statutory = "## Statutory context (for the requester, not for sending)"
        self.draft.write_text(filled.replace(statutory, statutory + "\n\n[counsel to confirm]")
                              .replace("Operator checklist:", "Operator checklist: [who approved]"), encoding="utf-8")
        result = self.copy("portal")
        self.assertNotIn("[", (self.out / f"{self.a}.txt").read_text(encoding="utf-8"))
        self.assertEqual(result["written"], f"kit/outgoing/{self.a}.txt")

    def test_email_needs_a_verified_records_email(self):
        self.fill(verify=False)
        self.assert_refused("email", f"{self.a} has no verified records_email")
        self.fill(records_url="https://example.org/portal")
        self.assert_refused("email", f"{self.a} has no verified records_email")
        self.fill()
        agencies = self.kit / "agencies.json"
        document = json.loads(agencies.read_text(encoding="utf-8"))
        for entry in document:
            if entry["agency_id"] == self.a:
                entry["verified"] = False
        agencies.write_text(json.dumps(document), encoding="utf-8")
        self.assert_refused("email", f"{self.a} has no verified records_email")
        for entry in document:
            if entry["agency_id"] == self.a:
                entry["verified"] = True
        agencies.write_text(json.dumps(document), encoding="utf-8")
        before = self.snapshot()
        result = self.copy("email")
        path = self.out / f"{self.a}.eml"
        message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
        self.assertEqual((message["To"], message["X-Unsent"], message["Subject"]),
                         ("records@example.org", "1", result["subject"]))
        for header in ("From", "Date", "Message-ID", "Bcc", "Cc"):
            self.assertIsNone(message[header], header)
        self.assertEqual(message.get_content_charset(), "utf-8")
        body = message.get_content()
        self.assertEqual(result["characters"], len(result["subject"]) + len(body) - 1)
        for excluded in EXCLUDED:
            self.assertNotIn(excluded, body)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(self.snapshot(), before)

    def test_no_subject_unknown_agency_and_symlinked_draft_stop(self):
        self.fill()
        filled = self.draft.read_text(encoding="utf-8")
        self.draft.write_text(filled.replace("Subject: ", "Re: "), encoding="utf-8")
        self.assert_refused("portal", "Stopped: draft has no Subject line")
        code, _, err = run("kit-copy", "--directory", self.root, "--agency", "ca-not-listed", "--for", "portal")
        self.assertEqual(code, 3)
        self.assertIn("Stopped: agency_id 'ca-not-listed' is not in kit/agencies.json", err)
        outside = self.tmp / "outside.md"
        outside.write_text(filled, encoding="utf-8")
        self.draft.unlink()
        self.draft.symlink_to(outside)
        self.assert_refused("portal", f"kit/requests/{self.a}.md must be a regular file")

    def test_symlinked_outgoing_or_target_is_refused_and_nothing_written_outside(self):
        self.fill()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        self.out.symlink_to(elsewhere, target_is_directory=True)
        self.assert_refused("portal", "kit/outgoing must be a directory (not a symlink)")
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.out.unlink()
        self.out.mkdir(mode=0o755)
        victim = self.tmp / "victim.txt"
        victim.write_text("untouched", encoding="utf-8")
        (self.out / f"{self.a}.txt").symlink_to(victim)
        self.assert_refused("portal", f"kit/outgoing/{self.a}.txt must be a regular file")
        self.assertEqual(victim.read_text(encoding="utf-8"), "untouched")
        (self.out / f"{self.a}.eml").mkdir()
        self.assert_refused("email", f"kit/outgoing/{self.a}.eml must be a regular file")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), [f"{self.a}.eml", f"{self.a}.txt"])

    def test_rerun_replaces_the_copy_from_the_current_draft(self):
        self.fill()
        self.copy("portal")
        self.draft.write_text(self.draft.read_text(encoding="utf-8").replace("Rolling production is welcome.",
                                                                             "Edited line."), encoding="utf-8")
        self.copy("portal")
        written = (self.out / f"{self.a}.txt").read_text(encoding="utf-8")
        self.assertIn("Edited line.", written)
        self.assertNotIn("Rolling production", written)

    def test_subject_header_injection_is_refused(self):
        self.fill()
        text = self.draft.read_text(encoding="utf-8")
        start = text.index("Subject: ")
        end = text.index("\n", start)
        self.draft.write_bytes((text[:start] + "Subject: x\r\nBcc: a@b.example" + text[end:]).encode("utf-8"))
        for target in ("email", "portal"):
            self.assert_refused(target, "subject must be one line")
        self.draft.write_bytes((text[:start] + "Subject: x\u2028Bcc: a@b.example" + text[end:]).encode("utf-8"))
        self.assert_refused("email", "subject must be one line")
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
