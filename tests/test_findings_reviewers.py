"""DF1507-03: `finding reviewers add|list` private roster and its effect on `finding review`.

Synthetic names only. Run from the repository root: python3 -B -m unittest tests.test_findings_reviewers -v
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.test_findings_review import draft

REPO_ROOT = Path(__file__).resolve().parents[1]


class FindingReviewerRosterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-roster-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.cli("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County",
                 "--state", "ca")
        self.draft = self.base / "draft.json"
        self.draft.write_text(json.dumps(draft()), encoding="utf-8")
        self.roster = self.root / "private" / "reviewers.json"
        self.receipts = self.root / "private" / "reviews" / "syn-finding-1.json"

    def cli(self, *arguments, returncode=0, umask=None):
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, arguments)],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=20,
                                preexec_fn=None if umask is None else (lambda: os.umask(umask)))
        self.assertEqual(result.returncode, returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if returncode == 1:
            self.assertTrue(result.stderr.startswith("Stopped: "), result.stderr)
        return result

    def add(self, name, *roles, returncode=0, umask=None):
        arguments = ["finding", "reviewers", "add", "--directory", self.root, "--name", name]
        for role in roles:
            arguments += ["--role", role]
        result = self.cli(*arguments, returncode=returncode, umask=umask)
        if returncode:
            return result.stderr
        body, _, footer = result.stdout.rpartition("}")
        self.assertEqual(footer.strip(), "Private roster updated; nothing was published.")
        return json.loads(body + "}")

    def listing(self, returncode=0):
        result = self.cli("finding", "reviewers", "list", "--directory", self.root, returncode=returncode)
        return json.loads(result.stdout) if returncode == 0 else result.stderr

    def review(self, reviewer, role="factual", decision="approve", returncode=0):
        result = self.cli("finding", "review", self.draft, "--directory", self.root, "--role", role, "--decision",
                          decision, "--reviewer", reviewer, "--rationale", "Checked p. 1.", returncode=returncode)
        return json.loads(result.stdout) if returncode == 0 else result.stderr

    def status(self, returncode=0):
        result = self.cli("finding", "review-status", self.draft, "--directory", self.root, returncode=returncode)
        return json.loads(result.stdout) if returncode == 0 else result.stderr

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns) if p.is_file() else None
                for p in sorted(self.root.rglob("*"))}

    def write_roster(self, reviewers, **extra):
        self.roster.write_text(json.dumps({"schema_version": 1, "reviewers": reviewers, **extra}), encoding="utf-8")

    def test_no_roster_review_and_status_unchanged(self):
        self.assertEqual(self.listing(), {"roster": "absent", "reviewers": []})
        self.assertEqual(self.review("Anyone Else")["reviewer"], "Anyone Else")
        report = self.status()
        self.assertNotIn("unrostered_receipts", report)
        self.assertEqual(report["receipts_for_this_content"], 1)
        self.assertFalse(os.path.lexists(self.roster))
        self.assertIn("the author cannot review", self.review("ada lovelace", returncode=1))

    def test_roster_refuses_unlisted_name_and_unassigned_role_before_any_write(self):
        self.add("Alice Smith", "factual")
        self.assertIn("Bob Jones is not on the reviewer roster", self.review("Bob Jones", returncode=1))
        self.assertIn("Alice Smith is not assigned the legal role", self.review("Alice Smith", "legal", returncode=1))
        self.assertFalse(os.path.lexists(self.receipts.parent))
        self.review("Alice Smith", "factual")
        before = self.tree()
        self.assertIn("is not on the reviewer roster", self.review("Bob Jones", "privacy", returncode=1))
        self.assertIn("is not assigned the legal role", self.review("Alice Smith", "legal", "reject", returncode=1))
        self.assertEqual(self.tree(), before)
        # The author refusal still comes first, even for an author who is not rostered.
        self.assertIn("the author cannot review", self.review("Ada Lovelace", returncode=1))
        self.assertEqual(self.status()["unrostered_receipts"], [])

    def test_names_match_ignoring_case_and_spacing(self):
        self.add("Alice Smith", "factual")
        self.assertEqual(self.review(" alice  SMITH")["role"], "factual")
        self.add("ALICE   smith", "legal")
        listing = self.listing()
        self.assertEqual(len(listing["reviewers"]), 1)
        self.assertEqual(listing["reviewers"][0]["name"], "Alice Smith")
        self.assertEqual(self.status()["unrostered_receipts"], [])

    def test_add_twice_merges_roles_and_keeps_added_on(self):
        first = self.add("Alice Smith", "legal", "factual", "legal")
        self.assertEqual(first["roles"], ["factual", "legal"])
        document = json.loads(self.roster.read_text(encoding="utf-8"))
        document["reviewers"][0]["added_on"] = "2020-01-02"
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        second = self.add("Alice Smith", "privacy")
        self.assertEqual((second["roles"], second["added_on"]), (["factual", "legal", "privacy"], "2020-01-02"))
        self.add("Bob Jones", "privacy")
        listing = self.listing()
        self.assertEqual(listing["roster"], "present")
        self.assertEqual([(r["name"], r["roles"]) for r in listing["reviewers"]],
                         [("Alice Smith", ["factual", "legal", "privacy"]), ("Bob Jones", ["privacy"])])
        self.assertEqual(json.loads(self.roster.read_text(encoding="utf-8"))["schema_version"], 1)

    def test_bad_add_arguments_stop_and_write_nothing(self):
        before = self.tree()
        self.assertIn("--role must be one of", self.add("Alice Smith", "editor", returncode=1))
        self.assertIn("--role must be one of", self.add("Alice Smith", "factual", "Factual", returncode=1))
        self.assertIn("--name must not be empty", self.add("   ", "factual", returncode=1))
        self.assertIn("--name contains control characters", self.add("Alice\nSmith", "factual", returncode=1))
        self.cli("finding", "reviewers", "add", "--directory", self.root, "--name", "Alice", returncode=2)
        self.assertEqual(self.tree(), before)

    def test_bad_rosters_stop_every_command_and_write_nothing(self):
        good = {"name": "Alice Smith", "roles": ["factual"], "added_on": "2026-01-02"}
        target = self.base / "elsewhere.json"
        target.write_text(json.dumps({"schema_version": 1, "reviewers": [good]}), encoding="utf-8")
        cases = {
            "symlink": (lambda: self.roster.symlink_to(target), "is a symlink"),
            "directory": (lambda: self.roster.mkdir(), "is not a regular file"),
            "invalid JSON": (lambda: self.roster.write_text("{not json", encoding="utf-8"), "is not valid JSON"),
            "not an object": (lambda: self.roster.write_text("[]", encoding="utf-8"), "must be a JSON object"),
            "unknown top key": (lambda: self.write_roster([good], note="x"), "unknown key 'note'"),
            "schema version": (lambda: self.roster.write_text(json.dumps({"schema_version": True, "reviewers": []}),
                                                              encoding="utf-8"), "'schema_version' must be 1"),
            "reviewers not a list": (lambda: self.roster.write_text(json.dumps({"schema_version": 1, "reviewers": {}}),
                                                                    encoding="utf-8"), "'reviewers' must be a list"),
            "duplicate names": (lambda: self.write_roster([good, dict(good, name=" alice SMITH ")]),
                                "reviewers[1].name is empty or duplicates"),
            "unknown role": (lambda: self.write_roster([dict(good, roles=["factual", "editor"])]), "reviewers[0].roles"),
            "empty roles": (lambda: self.write_roster([dict(good, roles=[])]), "reviewers[0].roles"),
            "unknown entry key": (lambda: self.write_roster([dict(good, email="x")]), "unknown key 'email'"),
            "missing entry key": (lambda: self.write_roster([{"name": "Alice Smith", "roles": ["factual"]}]),
                                  "missing key 'added_on'"),
            "bad added_on": (lambda: self.write_roster([dict(good, added_on="2026-13-40")]), "reviewers[0].added_on"),
            "too many": (lambda: self.write_roster([dict(good, name=f"Rev {i}") for i in range(201)]),
                         "at most 200 entries"),
        }
        for label, (make, message) in cases.items():
            with self.subTest(label):
                if self.roster.is_symlink() or self.roster.is_file():
                    self.roster.unlink()
                elif self.roster.is_dir():
                    self.roster.rmdir()
                make()
                before = self.tree()
                self.assertIn(message, self.add("Alice Smith", "legal", returncode=1))
                self.assertIn(message, self.listing(returncode=1))
                self.assertIn(message, self.review("Alice Smith", returncode=1))
                self.assertIn(message, self.status(returncode=1))
                self.assertEqual(self.tree(), before)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["reviewers"], [good])

    def test_receipts_before_the_roster_are_named_current_and_stale_alike(self):
        self.review("Old Reviewer", "factual")
        self.review("Old Reviewer", "factual", "reject")
        self.draft.write_text(json.dumps(draft(summary="Edited synthetic summary.")), encoding="utf-8")
        self.review("Rev Two", "legal")
        self.review("Rev Two", "privacy")
        self.review("Rev Three", "factual")
        self.assertNotIn("unrostered_receipts", self.status())
        self.add("Rev Two", "legal")
        self.add("rev three", "factual")
        report = self.status()
        self.assertEqual(report["stale_receipts"], 2)
        self.assertEqual(report["unrostered_receipts"], [{"reviewer": "Old Reviewer", "role": "factual"},
                                                         {"reviewer": "Rev Two", "role": "privacy"}])

    def test_modes_under_open_umask_and_list_and_status_write_nothing(self):
        os.chmod(self.root / "private", 0o755)
        self.add("Alice Smith", "factual", umask=0)
        self.assertEqual((self.root / "private").stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.roster.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "private" / ".lock").stat().st_mode & 0o777, 0o600)
        self.review("Alice Smith")
        before = self.tree()
        self.assertEqual(self.listing()["reviewers"][0]["name"], "Alice Smith")
        self.status()
        self.assertEqual(self.tree(), before)
        self.assertFalse([p for p in self.root.rglob(".reviewers-*")])


if __name__ == "__main__":
    unittest.main()
