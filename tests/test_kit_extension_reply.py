"""DF1407-03: kit-extension-reply, a private never-sent reply to a recorded extension notice. Offline, synthetic."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

SENT, EXT = "2026-03-02", "2026-03-04"
CITATION = next(r["citation"] for r in law.load_package("us-ca")["rules"]
                if r["rule_id"] == "ca-gov-7922.535-determination-and-extension-2026")
REASON = "Staff “shortage”; records are \"voluminous\" — see §6253(c)"


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


class ExtensionReplyTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-extension-reply-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root)[0], 0)
        ready_drafts(self.root)
        self.a, self.b = (p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:2])
        self.sent = self.root / "kit" / "sent.json"
        self.followups = self.root / "kit" / "followups"
        self.send(self.a)

    def send(self, agency, *extra):
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", agency, "--date", SENT, "--channel",
                           "email", *extra)
        self.assertEqual(code, 0, err)

    def step(self, kind, *extra, on=EXT, agency=None):
        code, _, err = run("kit-response", "--directory", self.root, "--agency", agency or self.a, "--date", on,
                           "--kind", kind, *extra)
        self.assertEqual(code, 0, err)

    def reply(self, *extra, agency=None):
        return run("kit-extension-reply", "--directory", self.root, "--agency", agency or self.a, *extra)

    def draft(self, agency=None, on=EXT):
        return self.followups / f"{agency or self.a}-extension-{on}.md"

    def refused(self, words, *extra, code=1, agency=None):
        before = self.sent.read_bytes()
        existed = self.followups.exists()
        got, out, err = self.reply(*extra, agency=agency)
        self.assertEqual(got, code, out + err)
        self.assertIn(words, err)
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(self.followups.exists(), existed)

    def test_both_fields_render_and_nothing_is_recorded(self):
        self.step("extension", "--expected-on", "2026-03-20", "--reason", REASON)
        before = self.sent.read_bytes()
        code, out, err = self.reply()
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith("Draft only. Nothing was sent.\n"))
        report = json.loads(out.split("\nDraft only")[0])
        self.assertEqual(report, {"written": f"kit/followups/{self.a}-extension-{EXT}.md", "agency_id": self.a,
                                  "request": "main", "extension_on": EXT, "law_package_status": "draft"})
        text = self.draft().read_text(encoding="utf-8")
        self.assertIn("DRAFT — NOT SENT. Review before sending it yourself.", text)
        self.assertIn(f"On {EXT} we recorded the agency's extension notice.", text)
        self.assertIn(f"The agency stated this reason, as written:\n> {REASON}\n", text)
        self.assertIn("1. Tell us which circumstance the agency relies on", text)
        self.assertIn(CITATION, text)
        self.assertIn("law package: draft (not independently reviewed)", text)
        self.assertIn("needs_attorney_review", text)
        self.assertIn("2. Your notice states 2026-03-20 for dispatch of the determination; please confirm.", text)
        self.assertIn("3. Send any records that are ready now.", text)
        lowered = text.lower()
        for word in ("violat", "illegal", "unlawful"):
            self.assertNotIn(word, lowered)
        self.assertEqual(self.sent.read_bytes(), before)
        self.assertEqual(oct(self.followups.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct(self.draft().stat().st_mode & 0o777), "0o600")

    def test_legacy_extension_asks_for_the_date_and_omits_reason(self):
        self.step("extension")
        self.assertEqual(self.reply()[0], 0)
        text = self.draft().read_text(encoding="utf-8")
        self.assertIn("2. Tell us the date on which the determination will be dispatched.", text)
        self.assertNotIn("as written", text)
        self.assertNotIn("for dispatch of the determination", text)

    def test_date_only_and_reason_only(self):
        self.step("extension", "--expected-on", "2026-03-18")
        self.reply()
        text = self.draft().read_text(encoding="utf-8")
        self.assertIn("states 2026-03-18 for dispatch", text)
        self.assertNotIn("as written", text)
        self.send(self.b)
        self.step("extension", "--reason", "x", agency=self.b)
        self.assertEqual(self.reply(agency=self.b)[0], 0)
        other = self.draft(self.b).read_text(encoding="utf-8")
        self.assertIn("> x\n", other)
        self.assertIn("Tell us the date on which", other)

    def test_reference_and_portal_lines_once(self):
        self.send(self.b, "--reference", "R0123-110125")
        self.step("extension", agency=self.b)
        self.assertEqual(self.reply(agency=self.b)[0], 0)
        text = self.draft(self.b).read_text(encoding="utf-8")
        self.assertIn("R0123-110125", text)
        self.step("extension")
        self.reply()
        self.assertNotIn("Regarding your reference", self.draft().read_text(encoding="utf-8"))

    def test_acknowledgment_and_resolved_clarification_do_not_block(self):
        self.step("acknowledged", on="2026-03-03")
        self.step("clarification_requested", on="2026-03-03")
        self.step("clarified", on="2026-03-03")
        self.step("extension")
        self.assertEqual(self.reply()[0], 0)

    def test_refusals(self):
        self.refused("no recorded extension")
        self.step("acknowledged", on="2026-03-03")
        self.refused("no recorded extension")
        self.step("extension")
        self.step("determination", on="2026-03-10")
        self.refused("was answered (determination on 2026-03-10)")
        self.send(self.b)
        self.step("extension", agency=self.b)
        self.step("produced", "--partial", "--note", "synthetic part", on="2026-03-10", agency=self.b)
        self.refused("was answered (produced on 2026-03-10)", agency=self.b)
        self.refused("no hand-sent request recorded", "--request", "missing")
        self.refused("--request must be", "--request", "Bad Name")
        self.refused("no hand-sent request recorded", agency="no-such")

    def test_closed_and_withdrawn(self):
        self.step("extension")
        self.step("closed", on="2026-03-10")
        self.refused("is closed")
        self.send(self.b)
        self.step("extension", agency=self.b)
        self.step("withdrawn", "--note", "synthetic withdrawal", on="2026-03-10", agency=self.b)
        self.refused("was withdrawn by us on 2026-03-10", agency=self.b)

    def test_waiting_on_clarification_exits_3_even_after_extension(self):
        self.step("clarification_requested", on="2026-03-03")
        self.step("extension", on=EXT)
        self.refused("waiting on you since 2026-03-03", code=3)

    def test_named_request_is_isolated(self):
        (self.root / "kit" / "requests" / f"{self.a}--audit.md").write_text("Second request.\n", encoding="utf-8")
        code, _, err = run("kit-sent", "--directory", self.root, "--agency", self.a, "--request", "audit",
                           "--date", SENT, "--channel", "email")
        self.assertEqual(code, 0, err)
        self.step("extension")
        self.refused("no recorded extension", "--request", "audit")
        code, _, err = run("kit-response", "--directory", self.root, "--agency", self.a, "--request", "audit",
                           "--date", "2026-03-05", "--kind", "extension")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.reply("--request", "audit")[0], 0)
        self.assertTrue((self.followups / f"{self.a}--audit-extension-2026-03-05.md").is_file())
        self.assertFalse(self.draft().exists())

    def test_malformed_history_refused_before_output(self):
        self.step("extension")
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        document["requests"][self.a]["steps"][0]["extension_reason"] = "bad\nreason"
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        self.refused("unreadable", "--replace")

    def test_package_rule_missing_or_not_in_force(self):
        self.step("extension")
        with mock.patch("campaign_tool.law.rules_in_force", return_value=[]):
            self.refused("no determination-and-extension rule in force")
        with mock.patch("campaign_tool.law.load_package", side_effect=ValueError("none")):
            self.refused("no law package")

    def test_existing_draft_kept_unless_replace_and_symlinks_refused(self):
        self.step("extension", "--reason", "first")
        self.assertEqual(self.reply()[0], 0)
        self.draft().write_text("organizer edit\n", encoding="utf-8")
        got, _, err = self.reply()
        self.assertEqual(got, 1)
        self.assertIn("already exists; use --replace", err)
        self.assertEqual(self.draft().read_text(encoding="utf-8"), "organizer edit\n")
        self.assertEqual(self.reply("--replace")[0], 0)
        self.assertIn("> first", self.draft().read_text(encoding="utf-8"))
        outside = Path(self.root).parent / "outside.txt"
        outside.write_text("sentinel\n", encoding="utf-8")
        self.draft().unlink()
        os.symlink(outside, self.draft())
        got, _, err = self.reply("--replace")
        self.assertEqual(got, 1)
        self.assertIn("must not be a symlink", err)
        self.assertEqual(outside.read_text(encoding="utf-8"), "sentinel\n")
        self.assertEqual([p.name for p in self.followups.glob(".*")], [])


if __name__ == "__main__":
    unittest.main()
