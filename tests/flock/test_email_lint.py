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
SLUG, DATE = "sample-city-police-department", "2026-10-31"
SUMMARY = " | draft only; G approval required before any send"
OLD, NEW = "records@city.example", "cpra@city.example"


class EmailLintTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name)
        self.agency = self.store / "agencies" / SLUG
        self.run_dir, self.drafts = self.agency / "runs" / DATE, self.store / "drafts"
        self.run_dir.mkdir(parents=True)
        shutil.copy(FIXTURES / "followup-draft-sample.md", self.run_dir / "FOLLOWUP-DRAFT.md")
        shutil.copy(FIXTURES / "contact-sample.csv", self.agency / "CONTACT.csv")
        made = self.tool("draft", str(self.run_dir), "--drafts", str(self.drafts))
        self.assertEqual(made.returncode, 0, made.stderr)
        self.copy, self.twin = self.run_dir / "EMAIL-DRAFT.md", self.drafts / f"{SLUG}-{DATE}-EMAIL-DRAFT.md"

    def tool(self, *args):
        return subprocess.run([sys.executable, "-B", str(TOOL), *args], capture_output=True, text=True)

    def lint(self, rundir=None, *, drafts=True):
        return self.tool("lint", str(rundir or self.run_dir), *(["--drafts", str(self.drafts)] if drafts else []))

    def rules(self, done):
        lines = done.stdout.splitlines()
        self.assertTrue(lines and lines[-1].endswith(SUMMARY), done.stdout + done.stderr)
        prefix = f"lint | {SLUG} | {DATE} | "
        self.assertTrue(all(ln.startswith(prefix) for ln in lines[:-1]), done.stdout)
        self.assertEqual(done.returncode, 1 if lines[:-1] else 0)
        self.assertTrue(lines[-1].startswith(f"lint | rules={len(lines) - 1} | "))
        return [ln[len(prefix):] for ln in lines[:-1]]

    def edit(self, old, new, both=True):
        for path in (self.copy, self.twin) if both else (self.copy,):
            text = path.read_text(encoding="utf-8")
            self.assertIn(old, text)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_fresh_pair_is_clean_and_read_only(self):
        before = [(p.read_bytes(), os.stat(p).st_mtime_ns) for p in (self.copy, self.twin)]
        done = self.lint()
        self.assertEqual(self.rules(done), [])
        self.assertEqual(done.stdout.splitlines()[-1], "lint | rules=0 | email drafted" + SUMMARY)
        self.assertEqual([(p.read_bytes(), os.stat(p).st_mtime_ns) for p in (self.copy, self.twin)], before)
        self.assertEqual(sorted(p.name for p in self.drafts.iterdir()), [self.twin.name])
        self.assertEqual(sorted(p.name for p in self.run_dir.iterdir()), ["EMAIL-DRAFT.md", "FOLLOWUP-DRAFT.md"])

    def test_run_copy_edited_names_first_differing_line(self):
        self.edit("Please reference", "Kindly reference", both=False)
        line = next(n for n, ln in enumerate(self.copy.read_text(encoding="utf-8").splitlines(), 1) if ln.startswith("Kindly"))
        self.assertEqual(self.rules(self.lint()), [f"copies differ (first differing line {line})"])
        self.copy.write_bytes(self.twin.read_bytes() + b"extra\n")  # one copy is a prefix of the other
        n = len(self.twin.read_bytes().split(b"\n"))
        self.assertEqual(self.rules(self.lint()), [f"copies differ (first differing line {n})"])

    def test_twin_missing_and_twin_link_refused_unread(self):
        self.twin.unlink()
        self.assertEqual(self.rules(self.lint()), ["twin missing"])
        target = self.store / "elsewhere.md"
        target.write_text("(805) 555-0100 TODO\n", encoding="utf-8")
        self.twin.symlink_to(target)
        done = self.lint()
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "lint: link refused\n"))
        self.twin.unlink()
        shutil.copy(self.copy, self.twin)
        self.copy.unlink()
        self.copy.symlink_to(self.twin)  # a linked run copy is refused too
        done = self.lint()
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "lint: link refused\n"))

    def test_run_copy_missing(self):
        self.copy.unlink()
        done = self.lint()
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "lint: EMAIL-DRAFT.md missing\n"))

    def test_contact_changed_or_deleted(self):
        csv_path = self.agency / "CONTACT.csv"
        csv_path.write_text(csv_path.read_text(encoding="utf-8").replace(OLD, NEW), encoding="utf-8")
        done = self.lint()
        self.assertEqual(self.rules(done), ["to: not the current records address"])
        self.assertNotIn("records@", done.stdout + done.stderr)
        self.assertNotIn("cpra@", done.stdout + done.stderr)
        csv_path.write_text("agency,role\n", encoding="utf-8")
        self.assertEqual(self.rules(self.lint()), ["to: CONTACT.csv row 1: header does not match CONTACT.csv v1"])
        csv_path.unlink()
        self.assertEqual(self.rules(self.lint()), ["to: CONTACT.csv contact: CONTACT.csv missing or unreadable"])

    def test_to_line_edited_in_both_copies(self):
        self.edit(f"To: {OLD}", "To: someone@else.example")
        self.assertEqual(self.rules(self.lint()), ["to: not the current records address"])

    def test_placeholders_give_one_line_per_draft(self):
        self.edit("Please reference", "TODO {{agency}} ____ Please reference")
        self.assertEqual(self.rules(self.lint()), ["placeholder"])
        for word in ("tbd", "Xxx", "[Insert date]", "<insert name>", "}}", "todo:"):
            with self.subTest(word=word):
                self.setUp()
                self.edit("Please reference", f"{word} Please reference")
                self.assertEqual(self.rules(self.lint()), ["placeholder"])

    def test_placeholder_in_subject_counts(self):
        self.edit("Subject: CPRA request", "Subject: TBD CPRA request")
        self.assertEqual(self.rules(self.lint()), ["placeholder"])

    def test_private_patterns_named_never_printed(self):
        self.edit("Please reference", "Call (805) 555-0100 about plate 7ABC123. Please reference")
        done = self.lint()
        self.assertEqual(self.rules(done), ["private: phone", "private: plate"])
        self.assertNotIn("555", done.stdout + done.stderr)
        self.assertNotIn("7ABC", done.stdout + done.stderr)

    def test_records_address_allowed_other_address_private(self):
        self.edit("Please reference", f"Reply to {OLD} or <{OLD.upper()}>. Please reference")
        self.assertEqual(self.rules(self.lint()), [])
        for other in ("me@home.example", f"x.{OLD}", f"{OLD}.evil.example"):
            with self.subTest(other=other):
                self.setUp()
                self.edit("Please reference", f"Reply to {OLD} or {other}. Please reference")
                done = self.lint()
                self.assertEqual(self.rules(done), ["private: email"])
                self.assertNotIn(other, done.stdout + done.stderr)

    def test_approval_filled_in_both_still_linted(self):
        self.edit("G APPROVED: ____", "G APPROVED: G 2026-10-31")
        done = self.lint()
        self.assertEqual(self.rules(done), [])
        self.assertEqual(done.stdout.splitlines()[-1], "lint | rules=0 | approval recorded" + SUMMARY)
        self.edit("Please reference", "TODO Please reference")
        done = self.lint()
        self.assertEqual(self.rules(done), ["placeholder"])
        self.assertIn("| approval recorded |", done.stdout.splitlines()[-1])

    def test_approval_filled_in_one_copy_only(self):
        self.edit("G APPROVED: ____", "G APPROVED: G 2026-10-31", both=False)
        rules = self.rules(self.lint())
        self.assertEqual(len(rules), 1)
        self.assertTrue(rules[0].startswith("copies differ (first differing line "))
        self.assertNotIn("G 2026", rules[0])

    def test_length_limits(self):
        subject = next(ln for ln in self.copy.read_text(encoding="utf-8").splitlines() if ln.startswith("Subject: "))
        self.edit(subject, "Subject: " + "s" * 150)
        self.assertEqual(self.rules(self.lint()), [])
        self.edit("Subject: " + "s" * 150, "Subject: " + "s" * 151)
        self.assertEqual(self.rules(self.lint()), ["length"])
        self.setUp()
        self.edit("Please reference", "word " * 1700 + "Please reference")
        self.assertEqual(self.rules(self.lint()), ["length"])

    def test_malformed_shapes(self):
        cases = [("DRAFT - not sent - G approval required\n", "", False), (f"To: {OLD}\n", "", False),
                 ("Sent by: ____", "Sent by: ____\nSent by: ____", True), ("G APPROVED: ____", "", True)]
        for old, new, to_ok in cases:
            with self.subTest(old=old):
                self.setUp()
                self.edit(old, new)
                rules = self.rules(self.lint())
                self.assertIn("malformed", rules)
                self.assertEqual("to: not the current records address" in rules, not to_ok)
        self.setUp()
        text = self.copy.read_text(encoding="utf-8").replace("---\n", "", 2) + "Call (805) 555-0100\n"
        self.copy.write_text(text, encoding="utf-8")
        shutil.copy(self.copy, self.twin)
        self.assertEqual(self.rules(self.lint()), ["malformed", "placeholder", "private: phone"])  # no --- pair: rest scanned

    def test_rundir_layout_and_usage(self):
        done = self.lint(self.agency)
        self.assertEqual((done.returncode, done.stdout), (1, ""))
        self.assertEqual(done.stderr, "lint: RUNDIR must be <store>/agencies/<slug>/runs/<date>\n")
        inside = ROOT / "agencies" / SLUG / "runs" / DATE
        done = self.lint(inside)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, "", "lint: RUNDIR is inside the repository\n"))
        self.assertFalse((ROOT / "agencies").exists())
        done = self.lint(drafts=False)
        self.assertEqual((done.returncode, done.stdout), (2, ""))
        self.assertTrue(done.stderr.startswith("usage: flock_email.py"))
        self.assertIn("lint RUNDIR --drafts DIR", done.stderr)
        self.assertIn("before asking G to approve", self.tool("--help").stdout)
        self.assertEqual(self.tool("lint", str(self.run_dir), "--drafts", str(self.drafts), "extra").returncode, 2)


if __name__ == "__main__":
    unittest.main()
