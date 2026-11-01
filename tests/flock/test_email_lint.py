import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "flock_email.py"
FIXTURES = ROOT / "tests" / "fixtures" / "flock"
SLUG = "sample-city-police-department"
DAY = "2026-10-31"
TAIL = "draft only; G approval required before any send"
OLD, NEW = "records@city.example", "cpra@city.example"


def run(*args):
    done = subprocess.run([sys.executable, "-B", str(TOOL), *map(str, args)], capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


class EmailLintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agency = Path(self.tmp.name) / "agencies" / SLUG
        self.rundir, self.drafts = self.agency / "runs" / DAY, Path(self.tmp.name) / "drafts"
        self.rundir.mkdir(parents=True)
        shutil.copy(FIXTURES / "followup-draft-sample.md", self.rundir / "FOLLOWUP-DRAFT.md")
        shutil.copy(FIXTURES / "contact-sample.csv", self.agency / "CONTACT.csv")
        self.assertEqual(run("draft", self.rundir, "--drafts", self.drafts)[0], 0)
        self.first, self.twin = self.rundir / "EMAIL-DRAFT.md", self.drafts / f"{SLUG}-{DAY}-EMAIL-DRAFT.md"

    def lint(self):
        return run("lint", self.rundir, "--drafts", self.drafts)

    def edit(self, old, new, paths=None):
        for path in paths or (self.first, self.twin):
            path.write_text(path.read_text(encoding="utf-8").replace(old, new, 1), encoding="utf-8")

    def rules(self, out):
        return [ln.split(" | ")[3] for ln in out.splitlines() if ln.startswith(f"lint | {SLUG} | {DAY} | ")]

    def test_fresh_pair_is_clean_and_files_untouched(self):
        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.first, self.twin)]
        code, out = self.lint()
        self.assertEqual((code, out), (0, f"lint | rules=0 | email drafted | {TAIL}\n"))
        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.first, self.twin)], before)

    def test_one_copy_edited_names_first_differing_line(self):
        lines = self.first.read_text(encoding="utf-8").splitlines(keepends=True)
        n = lines.index("---\n") + 2
        lines[n - 1] = "Edited sentence.\n"
        self.first.write_text("".join(lines), encoding="utf-8")
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, [f"copies differ (first differing line {n})"]))
        self.assertTrue(out.endswith(f"lint | rules=1 | email drafted | {TAIL}\n"))
        self.assertNotIn("Edited", out)

    def test_twin_appended_line_is_a_difference(self):
        with self.twin.open("a", encoding="utf-8") as fh:
            fh.write("extra\n")
        n = len(self.first.read_text(encoding="utf-8").splitlines()) + 1
        self.assertEqual(self.rules(self.lint()[1]), [f"copies differ (first differing line {n})"])

    def test_twin_missing_or_linked(self):
        target = Path(self.tmp.name) / "elsewhere.md"
        target.write_bytes(self.twin.read_bytes())
        os.unlink(self.twin)
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, ["twin missing"]))
        os.symlink(target, self.twin)
        os.chmod(target, 0)  # a read of the target would fail; the link is refused before any read
        self.addCleanup(os.chmod, target, 0o600)
        self.assertEqual(self.lint(), (1, "lint: link refused\n"))

    def test_run_copy_linked_or_missing(self):
        moved = Path(self.tmp.name) / "moved.md"
        os.rename(self.first, moved)
        self.assertEqual(self.lint(), (1, "lint: EMAIL-DRAFT.md missing\n"))
        os.symlink(moved, self.first)
        self.assertEqual(self.lint(), (1, "lint: link refused\n"))

    def test_records_address_changed_after_drafting(self):
        self.edit(OLD, NEW, [self.agency / "CONTACT.csv"])
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, ["to: not the current records address"]))
        self.assertNotIn("@", out)

    def test_edited_to_line_is_not_current(self):
        self.edit(f"To: {OLD}", "To: someone@other.example")
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, ["to: not the current records address"]))
        self.assertNotIn("@", out)

    def test_contact_missing(self):
        os.unlink(self.agency / "CONTACT.csv")
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, ["to: CONTACT.csv contact: CONTACT.csv missing or unreadable"]))

    def test_placeholders_one_line_per_draft(self):
        self.edit("Please provide", "TODO {{agency}} ____ Please provide")
        self.assertEqual(self.rules(self.lint()[1]), ["placeholder"])

    def test_each_placeholder_form_and_case(self):
        for word in ("todo", "XxX", "tbd", "[Insert name]", "<insert date>", "{{", "}}", "____"):
            with self.subTest(word=word):
                self.setUp()
                self.edit("Please provide", f"{word} Please provide")
                self.assertEqual(self.rules(self.lint()[1]), ["placeholder"])

    def test_placeholder_outside_body_is_not_body(self):
        # G APPROVED: ____ sits after the second ---; it is not a placeholder finding.
        self.assertIn("G APPROVED: ____", self.first.read_text(encoding="utf-8"))
        self.assertEqual(self.lint()[0], 0)

    def test_private_patterns_and_own_address(self):
        self.edit("Please provide", "Call (805) 555-0100 about plate 7ABC123. Please provide")
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, ["private: phone", "private: plate"]))
        self.assertNotIn("555", out)
        self.assertNotIn("7ABC", out)

    def test_own_records_address_in_body_allowed(self):
        self.edit("Please provide", f"Reply to {OLD}. Please provide")
        self.assertEqual(self.lint(), (0, f"lint | rules=0 | email drafted | {TAIL}\n"))

    def test_other_email_in_body_flagged(self):
        for other in ("someone@else.example", f"evil.{OLD}", f"{OLD}.evil.example", f"x{OLD}"):
            with self.subTest(other=other):
                self.setUp()
                self.edit("Please provide", f"Reply to {other}. Please provide")
                code, out = self.lint()
                self.assertEqual((code, self.rules(out)), (1, ["private: email"]))
                self.assertNotIn(other, out)

    def test_approval_recorded_in_both_copies_still_linted(self):
        self.edit("G APPROVED: ____", "G APPROVED: G 2026-10-31")
        self.assertEqual(self.lint(), (0, f"lint | rules=0 | approval recorded | {TAIL}\n"))
        self.edit("Please provide", "TODO Please provide")
        code, out = self.lint()
        self.assertEqual((code, self.rules(out)), (1, ["placeholder"]))
        self.assertIn("| approval recorded |", out)

    def test_approval_in_one_copy_only(self):
        self.edit("G APPROVED: ____", "G APPROVED: G 2026-10-31", [self.twin])
        code, out = self.lint()
        self.assertEqual(code, 1)
        self.assertEqual(len(self.rules(out)), 1)
        self.assertTrue(self.rules(out)[0].startswith("copies differ (first differing line "))
        self.assertIn("| email drafted |", out)

    def test_twin_only_edit_is_also_linted(self):
        self.edit("Please provide", "Call (805) 555-0100. Please provide", [self.twin])
        rules = self.rules(self.lint()[1])
        self.assertEqual(rules[1:], ["private: phone"])

    def test_length(self):
        lines = self.first.read_text(encoding="utf-8").splitlines()
        self.edit(lines[2], "Subject: " + "s" * 151)
        self.assertEqual(self.rules(self.lint()[1]), ["length"])
        self.setUp()
        self.edit(lines[2], "Subject: " + "s" * 150)
        self.assertEqual(self.lint()[0], 0)
        self.edit("Please provide", "word " * 1700 + "Please provide")
        self.assertEqual(self.rules(self.lint()[1]), ["length"])

    def test_malformed(self):
        for old, new in (("DRAFT - not sent", "Draft"), ("To: ", "Cc: "), ("Subject: ", "Re: "),
                         ("Sent by: ____", "Sent by: ____\nSent by: ____"), ("G APPROVED: ____", "")):
            with self.subTest(old=old):
                self.setUp()
                self.edit(old, new)
                code, out = self.lint()
                self.assertEqual(code, 1)
                self.assertIn("malformed", self.rules(out))

    def test_refused_layouts_and_usage(self):
        code, out = run("lint", self.agency, "--drafts", self.drafts)
        self.assertEqual((code, out), (1, "lint: RUNDIR must be <store>/agencies/<slug>/runs/<date>\n"))
        inside = ROOT / "agencies" / SLUG / "runs" / DAY
        self.assertEqual(run("lint", inside, "--drafts", self.drafts), (1, "lint: RUNDIR is inside the repository\n"))
        self.assertFalse((ROOT / "agencies").exists())
        for argv in (["lint", self.rundir], ["lint", self.rundir, "--draft", self.drafts], ["lint", self.rundir, "--drafts"]):
            code, out = run(*argv)
            self.assertEqual(code, 2)
            self.assertIn("lint RUNDIR --drafts DIR", out)


if __name__ == "__main__":
    unittest.main()
