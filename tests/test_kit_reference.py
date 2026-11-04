"""The agency's own reference number on a hand-sent request (DF2037-10). Offline, synthetic."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli  # noqa: E402
from tests.test_kit_check import ready_drafts  # noqa: E402

BAD = ("", "R" * 41, "R 1", "R/1", "R-1\n", "R<1", "Ré1")
FORMAT = "--reference must be 1-40 letters, digits, - _ # or ."


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


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


class KitReferenceTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-reference-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        for command in (("init", "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA"), ("kit",)):
            code, _, err = run(command[0], "--directory", self.root, *command[1:])
            self.assertEqual(code, 0, err)
        self.a, self.b = sorted(p.stem for p in (self.root / "kit" / "requests").glob("*.md"))[:2]
        ready_drafts(self.root)
        self.sent = self.root / "kit" / "sent.json"
        self.folder = self.root / "kit" / "followups"

    def cli(self, command, agency, *extra, code=0):
        got, out, err = run(command, "--directory", self.root, "--agency", agency, *extra)
        self.assertEqual(got, code, out + err)
        return out, err

    def send(self, agency, *extra, code=0):
        return self.cli("kit-sent", agency, "--date", "2026-03-02", "--channel", "email", *extra, code=code)

    def step(self, agency, kind, *extra, on="2026-03-05", code=0):
        return self.cli("kit-response", agency, "--date", on, "--kind", kind, *extra, code=code)

    def entry(self, agency):
        return json.loads(self.sent.read_text(encoding="utf-8"))["requests"][agency]

    def refused(self, words, command, *args, code=1):
        before = snapshot(self.root)
        _, err = command(*args, code=code)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertIn(words, err)
        self.assertEqual(snapshot(self.root), before)

    def report(self, command, *extra, code=0):
        got, out, err = run(command, "--directory", self.root, *extra)
        self.assertEqual(got, code, err)
        return json.loads(out)

    def test_sent_reference_stored_and_shown_by_status_and_due(self):
        self.send(self.a, "--reference", "R-1")
        self.send(self.b)
        self.assertEqual(self.entry(self.a)["reference"], "R-1")
        self.assertNotIn("reference", self.entry(self.b))
        rows = {r["agency_id"]: r["reference"] for r in self.report("kit-status")["requests"]}
        self.assertEqual(rows, {self.a: "R-1", self.b: None})
        rows = {r["agency_id"]: r["reference"] for r in self.report("kit-due", "--as-of", "2026-03-05")["open"]}
        self.assertEqual(rows, {self.a: "R-1", self.b: None})
        self.step(self.a, "determination")  # answered and stated-date rows carry it too
        [answered] = self.report("kit-due", "--as-of", "2026-03-05")["answered"]
        self.assertEqual(answered["reference"], "R-1")

    def test_reference_first_given_on_a_response_is_stored_on_the_entry_not_the_step(self):
        self.send(self.a)
        self.step(self.a, "acknowledged", "--reference", "R0123-110125")
        entry = self.entry(self.a)
        self.assertEqual(entry["reference"], "R0123-110125")
        self.assertEqual([sorted(s) for s in entry["steps"]], [["kind", "on", "recorded_at"]])

    def test_same_value_is_a_no_op_different_value_exit_3_and_replace_reference_replaces(self):
        self.send(self.a, "--reference", "R-1")
        before = self.entry(self.a)
        self.step(self.a, "acknowledged", "--reference", "R-1")
        after = self.entry(self.a)
        self.assertEqual({**after, "steps": after["steps"][:-1]}, before)  # only the new step was added
        self.assertEqual(len(after["steps"]), 1)
        conflict = "reference already recorded as R-1; pass --replace-reference to change it"
        self.refused(conflict, self.step, self.a, "extension", "--reference", "R-2", code=3)
        self.step(self.a, "extension", "--reference", "R-2", "--replace-reference")
        replaced = self.entry(self.a)
        self.assertEqual(replaced["reference"], "R-2")
        self.assertEqual({**replaced, "reference": "R-1", "steps": replaced["steps"][:-1]}, after)
        self.assertEqual([sorted(s) for s in replaced["steps"]], [["kind", "on", "recorded_at"]] * 2)

    def test_kit_sent_replace_keeps_the_reference_and_a_different_one_needs_replace_reference(self):
        self.send(self.a, "--reference", "R-1")
        self.refused("reference already recorded as R-1", self.send, self.a, "--replace", "--reference", "R-2",
                     code=3)
        self.send(self.a, "--replace")
        self.assertEqual(self.entry(self.a)["reference"], "R-1")
        self.send(self.a, "--replace", "--reference", "R-2", "--replace-reference")
        self.assertEqual(self.entry(self.a)["reference"], "R-2")

    def test_bad_values_refused_on_both_commands_and_allowed_characters_accepted(self):
        for value in BAD:
            with self.subTest(value=value):
                self.refused(FORMAT, self.send, self.a, "--reference", value)
        self.send(self.a)
        for value in BAD:
            with self.subTest(value=value):
                self.refused(FORMAT, self.step, self.a, "acknowledged", "--reference", value)
        self.step(self.a, "acknowledged", "--reference", "#12.3_a-b")
        self.assertEqual(self.entry(self.a)["reference"], "#12.3_a-b")
        self.send(self.b, "--reference", "x" * 40)
        self.assertEqual(self.entry(self.b)["reference"], "x" * 40)

    def test_replace_reference_without_reference_refused(self):
        self.refused("--replace-reference needs --reference", self.send, self.a, "--replace-reference")
        self.send(self.a, "--reference", "R-1")
        self.refused("--replace-reference needs --reference", self.step, self.a, "acknowledged",
                     "--replace-reference")

    def test_closed_request_refuses_a_reference_change(self):
        self.send(self.a, "--reference", "R-1")
        self.step(self.a, "acknowledged")
        self.step(self.a, "closed")
        self.refused("is closed; no step can follow closed", self.step, self.a, "produced", "--reference", "R-2",
                     "--replace-reference")

    def test_hand_edited_bad_reference_is_unreadable_naming_the_agency(self):
        self.send(self.a)
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        for value in (12, "a b", None, ""):
            with self.subTest(value=value):
                document["requests"][self.a]["reference"] = value
                self.sent.write_text(json.dumps(document), encoding="utf-8")
                for command in ("kit-status", "kit-due"):
                    got, _, err = run(command, "--directory", self.root)
                    self.assertEqual(got, 1)
                    self.assertIn(f"kit/sent.json unreadable: malformed entry {self.a!r}", err)

    def quoted(self, path, reference):
        """Store ``reference`` by hand on the entry; return the draft's bytes from before."""
        without = path.read_bytes()
        document = json.loads(self.sent.read_text(encoding="utf-8"))
        document["requests"][self.a]["reference"] = reference
        self.sent.write_text(json.dumps(document), encoding="utf-8")
        return without

    def check_quoted(self, without, text, title):
        lines = text.split("\n")
        self.assertTrue(lines[0].startswith(title) and lines[0].endswith(" (Your reference R-1)"), lines[0])
        body = lines.index(next(line for line in lines if line.startswith("To: "))) + 2
        self.assertEqual(lines[body:body + 2], ["Regarding your reference R-1,", ""])
        self.assertTrue(lines[body + 2].startswith("On 2026-03-02 we sent a records request by email"))
        golden = "\n".join([lines[0].removesuffix(" (Your reference R-1)"), *lines[1:body], *lines[body + 2:]])
        self.assertEqual(golden.encode("utf-8"), without)  # without a reference: the draft as before

    def test_followup_draft_quotes_reference_in_subject_and_first_line(self):
        self.send(self.a)
        self.cli("kit-followup", self.a, "--as-of", "2026-04-01")
        path = self.folder / f"{self.a}-2026-04-01.md"
        self.assertNotIn("reference", path.read_text(encoding="utf-8"))
        without = self.quoted(path, "R-1")
        self.cli("kit-followup", self.a, "--as-of", "2026-04-01", "--replace")
        self.check_quoted(without, path.read_text(encoding="utf-8"), "# Follow-up draft: ")

    def test_denial_reply_draft_quotes_reference_in_subject_and_first_line(self):
        self.send(self.a)
        self.step(self.a, "denied", on="2026-03-20")
        self.cli("kit-denial-reply", self.a)
        path = self.folder / f"{self.a}-denial-2026-03-20.md"
        self.assertNotIn("reference", path.read_text(encoding="utf-8"))
        without = self.quoted(path, "R-1")
        self.cli("kit-denial-reply", self.a, "--replace")
        self.check_quoted(without, path.read_text(encoding="utf-8"), "# Denial reply draft: ")


if __name__ == "__main__":
    unittest.main()
