"""kit-clarify-reply: a private, never-sent reply to the open clarification request. Offline, synthetic."""
import contextlib
import hashlib
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit_track, law  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

PACKAGE = law.load_package("us-ca")
TITLES = {s["scope_id"]: s["title"] for s in PACKAGE["request_scopes"]}
A, B, C = list(TITLES)[:3]
NAME = "contract-2026"
SENT_ON, ASKED_ON = "2026-03-02", "2026-03-05"
TODAY = date.today().isoformat()
NO_OPEN = "no open clarification request to answer"
SENTENCE = f"On {ASKED_ON} you asked us to clarify or narrow this request"


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


class ClarifyReplyTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "socket.create_connection", "urllib.request.urlopen"):
            patch = mock.patch(target, side_effect=AssertionError("network used"))
            patch.start()
            self.addCleanup(patch.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-clarify-reply-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        self.kit, self.followups = self.root / "kit", self.root / "kit" / "followups"
        self.sent = self.kit / "sent.json"
        self.assertEqual(run("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                             "--state", "CA")[0], 0)
        self.assertEqual(run("kit", "--directory", self.root, "--scope", A, "--scope", B)[0], 0)
        drafts = sorted((self.kit / "requests").glob("*.md"))
        self.agency, self.other = drafts[0].stem, drafts[1].stem
        ready_drafts(self.root)
        (self.kit / "requests" / f"{self.agency}--{NAME}.md").write_text("Narrowed request.\n", encoding="utf-8")
        self.send(self.agency)

    def cli(self, command, *extra):
        return run(command, "--directory", self.root, *extra)

    def send(self, agency, *extra):
        code, _, err = self.cli("kit-sent", "--agency", agency, "--date", SENT_ON, "--channel", "email", *extra)
        self.assertEqual(code, 0, err)

    def step(self, kind, on=ASKED_ON, *extra, agency=None):
        code, _, err = self.cli("kit-response", "--agency", agency or self.agency, "--date", on, "--kind", kind, *extra)
        self.assertEqual(code, 0, err)

    def reply(self, *extra):
        return self.cli("kit-clarify-reply", "--agency", self.agency, *extra)

    def draft(self, *, request=None):
        stem = self.agency if request is None else f"{self.agency}--{request}"
        return self.followups / f"{stem}-clarify-{TODAY}.md"

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}

    def refused(self, words, *extra):
        before, folder = self.snapshot(), self.followups.exists()
        code, out, err = self.reply(*extra)
        self.assertEqual(code, 1, out)
        self.assertIn(words, err)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.followups.exists(), folder)
        self.assertNotIn("Traceback", err)

    def test_default_keeps_every_asked_scope_and_records_nothing(self):
        self.step("clarification_requested")
        before = self.snapshot()
        entry = json.loads(self.sent.read_text())["requests"][self.agency]
        code, out, err = self.reply()
        self.assertEqual(code, 0, err)
        text = self.draft().read_text(encoding="utf-8")
        self.assertIn(SENTENCE, text)
        self.assertIn("DRAFT — NOT SENT", text)
        self.assertIn("None — all originally asked scopes are retained.", text)
        for kept in (A, B):
            self.assertIn(f"## {TITLES[kept]}", text)
        self.assertNotIn(f"## {TITLES[C]}", text)  # today's other package scope is not added
        self.assertIn("Statutory context (for the requester, not for sending)", text)
        self.assertIn("Draft only. Nothing was sent or recorded.", out)
        after = self.snapshot()
        after.pop(f"kit/followups/{self.draft().name}")
        self.assertEqual(after, before)  # sent.json, drafts and settings untouched
        again = json.loads(self.sent.read_text())["requests"][self.agency]
        self.assertEqual((again["scopes"], again["draft_sha256"], again["steps"]),
                         (entry["scopes"], entry["draft_sha256"], entry["steps"]))
        self.assertEqual(self.draft().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.followups.stat().st_mode & 0o777, 0o700)
        self.assertIn('"waiting_on_you_since": "2026-03-05"', self.cli("kit-status")[1])  # nothing recorded

    def test_subset_lists_every_omitted_asked_scope_once(self):
        self.step("clarification_requested")
        code, _, err = self.reply("--scope", B)
        self.assertEqual(code, 0, err)
        text = self.draft().read_text(encoding="utf-8")
        self.assertIn(f"## {TITLES[B]}", text)
        self.assertNotIn(f"## {TITLES[A]}", text)
        self.assertEqual(text.count(f"- {TITLES[A]} ({A})"), 1)
        self.assertNotIn("None — all originally", text)
        self.assertNotIn(f"({B})", text.split("## Request as clarified")[0])

    def test_scope_group_after_one_flag_and_stored_order(self):
        self.step("clarification_requested")
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["scopes"] = [B, A]
        self.sent.write_text(json.dumps(document))
        self.assertEqual(self.reply("--scope", A)[0], 0)
        text = self.draft().read_text(encoding="utf-8")
        self.assertEqual(text.count(f"({B})"), 1)
        self.assertEqual(self.reply("--scope", B, A, "--replace")[0], 0)
        text = self.draft().read_text(encoding="utf-8")
        self.assertLess(text.index(f"## {TITLES[A]}"), text.index(f"## {TITLES[B]}"))  # package order
        self.assertEqual(self.reply("--scope", A, "--scope", B, "--replace")[0], 0)

    def test_bad_scopes_refused(self):
        self.step("clarification_requested")
        self.refused(f"--scope {C} was not asked for", "--scope", C)
        self.refused("is not in the law package", "--scope", "nonesuch")
        self.refused("must not repeat", "--scope", A, "--scope", A)
        self.refused("must not repeat", "--scope", A, B, A)
        with self.assertRaisesRegex(ValueError, "at least one scope"):
            kit_track.clarify_reply(self.root, self.agency, scopes=[])
        self.assertFalse(self.followups.exists())
        self.assertEqual(self.cli("kit-clarify-reply", "--agency", self.agency, "--scope")[0], 2)

    def test_scopes_not_recorded_or_gone_from_package(self):
        self.step("clarification_requested")
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["scopes"] = [A, "retired-scope"]
        self.sent.write_text(json.dumps(document))
        self.refused("no longer in the law package")
        del document["requests"][self.agency]["scopes"]
        self.sent.write_text(json.dumps(document))
        self.refused("scopes asked for are not recorded")

    def test_lifecycle(self):
        self.refused(NO_OPEN)  # no steps
        self.step("clarification_requested")
        self.step("extension", "2026-03-10")  # an unrelated later step does not hide it
        self.assertEqual(self.reply()[0], 0)
        self.step("clarified", "2026-03-12")
        self.refused(NO_OPEN, "--replace")
        self.step("clarification_requested", "2026-03-12")  # same day as the answer: a new cycle
        self.assertEqual(self.reply("--replace")[0], 0)
        self.assertIn("On 2026-03-12 you asked us", self.draft().read_text(encoding="utf-8"))
        self.step("closed", "2026-03-20")
        self.refused(NO_OPEN, "--replace")

    def test_withdrawn_and_malformed_history_refused(self):
        self.step("clarification_requested")
        self.step("withdrawn", "2026-03-06", "--note", "no longer needed")
        self.refused(NO_OPEN)
        document = json.loads(self.sent.read_text())
        document["requests"][self.agency]["steps"].append({"on": "2026-03-01", "kind": "closed"})
        self.sent.write_text(json.dumps(document))
        self.refused("is before the request was sent")

    def test_unknown_agency_request_and_names(self):
        self.step("clarification_requested")
        self.refused("no hand-sent request recorded", "--request", "other-one")
        self.refused("--request must be", "--request", "../x")
        code, _, err = self.cli("kit-clarify-reply", "--agency", "../evil")
        self.assertEqual(code, 1)
        self.assertIn("no hand-sent request recorded", err)
        self.assertEqual(self.cli("kit-clarify-reply")[0], 2)
        self.assertEqual(self.reply("--bogus")[0], 2)

    def test_date_range_rules(self):
        self.step("clarification_requested")
        cases = ("2026-01-01", "2026-01-01:", ":2026-01-01", "2026-01-01:2026-02-01:2026-03-01", " 2026-01-01:2026-02-01",
                 "2026-01-01 :2026-02-01", "2026-02-30:2026-03-01", "2026-02-02:2026-02-01", "2026-01-01:2999-01-01",
                 "2026-01-01–2026-02-01", "2025-02-29:2026-01-01")
        for value in cases:
            self.refused("--date-range", "--date-range", value)
        for value, text in (("2024-02-29:2026-01-01", "2024-02-29 through 2026-01-01"),
                            ("2026-01-05:2026-01-05", "2026-01-05 through 2026-01-05"),
                            (f"2026-01-01:{TODAY}", f"2026-01-01 through {TODAY}")):
            self.assertEqual(self.reply("--date-range", value, "--replace")[0], 0)
            body = self.draft().read_text(encoding="utf-8")
            self.assertIn(f"The date range for this request is {text}.", body)
            self.assertIn("the original date range is not stored", body)

    def test_settings_supply_requester_fee_cap_and_range(self):
        self.step("clarification_requested")
        self.assertEqual(self.reply()[0], 0)
        body = self.draft().read_text(encoding="utf-8")
        self.assertIn("[requester-approved contact details]", body)  # visible placeholder, nothing invented
        self.assertIn("The date range for this request is [date range", body)
        (self.kit / "request-settings.json").write_text(json.dumps({
            "schema_version": 1, "requester": ["Synthetic Requester", "synthetic@example.org"], "fee_cap_usd": 25,
            "date_from": "2025-01-01", "date_to": "2025-12-31"}), encoding="utf-8")
        self.assertEqual(self.reply("--replace")[0], 0)
        body = self.draft().read_text(encoding="utf-8")
        for expected in ("Synthetic Requester\nsynthetic@example.org", "beyond $25.00", "range for this request is "
                         "2025-01-01 through 2025-12-31"):
            self.assertIn(expected, body)
        self.assertEqual(self.reply("--date-range", "2025-02-01:2025-03-01", "--replace")[0], 0)  # flag wins
        self.assertIn("is 2025-02-01 through 2025-03-01.", self.draft().read_text(encoding="utf-8"))
        (self.kit / "request-settings.json").write_text("{not json", encoding="utf-8")
        self.refused("request-settings", "--replace")  # malformed settings: the existing draft stays as it was

    def test_reference_portal_and_command_for_a_named_request(self):
        self.send(self.agency, "--request", NAME, "--reference", "R-1")
        self.step("clarification_requested", ASKED_ON, "--request", NAME, "--reference", "R-1")
        self.assertEqual(self.reply()[0], 1)
        self.refused(NO_OPEN)  # main never had a clarification: eligibility is per request
        code, _, err = self.reply("--request", NAME)
        self.assertEqual(code, 0, err)
        text = self.draft(request=NAME).read_text(encoding="utf-8")
        self.assertIn("(Your reference R-1)", text)
        self.assertIn("Regarding your reference R-1,", text)
        command = [line for line in text.splitlines() if "kit-response" in line][0].strip()
        parts = shlex.split(command)
        self.assertEqual(parts[:5], ["python3", "-B", "-m", "campaign_tool", "kit-response"])
        self.assertEqual(parts[parts.index("--directory") + 1], str(self.root))
        self.assertEqual(parts[parts.index("--request") + 1], NAME)
        self.assertEqual(parts[-4:], ["--date", TODAY, "--kind", "clarified"])
        self.assertIn("Check any narrowing", text)
        # the recording command was not run: still waiting
        self.assertIn('"waiting_on_you_since": "2026-03-05"', self.cli("kit-status")[1])

    def test_command_quotes_unusual_directory(self):
        self.step("clarification_requested")
        spaced = Path(self.root.parent) / "my campaign $x"
        os.symlink(self.root, spaced)
        result = kit_track.clarify_reply(spaced, self.agency)
        parts = shlex.split(result["record_after_sending"])
        self.assertEqual(parts[parts.index("--directory") + 1], str(spaced))

    def test_existing_draft_kept_unless_replace_and_target_checks(self):
        self.step("clarification_requested")
        self.assertEqual(self.reply()[0], 0)
        self.draft().write_text("edited by organizer\n", encoding="utf-8")
        other = self.followups / "other-file.md"
        other.write_text("keep\n")
        self.refused("already exists; use --replace")
        self.assertEqual(self.draft().read_text(), "edited by organizer\n")
        self.assertEqual(self.reply("--replace")[0], 0)
        self.assertIn(SENTENCE, self.draft().read_text(encoding="utf-8"))
        self.assertEqual(other.read_text(), "keep\n")
        self.draft().unlink()
        link = self.followups / "elsewhere.md"
        link.write_text("not ours\n")
        self.draft().symlink_to(link)
        for extra in ((), ("--replace",)):
            self.refused("must not be a symlink", *extra)
        self.assertEqual(link.read_text(), "not ours\n")
        self.draft().unlink()
        self.draft().mkdir()
        for extra in ((), ("--replace",)):
            self.refused("kit/followups/", *extra)

    def test_followups_folder_symlink_refused(self):
        self.step("clarification_requested")
        real = self.root / "elsewhere"
        real.mkdir()
        self.followups.symlink_to(real)
        self.refused("must not be a symlink")
        self.assertEqual(list(real.iterdir()), [])

    def test_hash_of_sent_draft_unchanged(self):
        self.step("clarification_requested")
        draft = self.kit / "requests" / f"{self.agency}.md"
        digest = hashlib.sha256(draft.read_bytes()).hexdigest()
        self.assertEqual(self.reply("--scope", A, "--date-range", "2026-01-01:2026-02-01")[0], 0)
        self.assertEqual(hashlib.sha256(draft.read_bytes()).hexdigest(), digest)
        self.assertEqual(json.loads(self.sent.read_text())["requests"][self.agency]["draft_sha256"], digest)

    def test_help_lists_the_command(self):
        code, out, err = self.cli("kit-clarify-reply", "--help")
        self.assertEqual(code, 0, err)
        self.assertIn("--date-range", out)


if __name__ == "__main__":
    unittest.main()
