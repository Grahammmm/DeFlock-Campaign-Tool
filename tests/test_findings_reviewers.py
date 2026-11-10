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
from datetime import date, datetime, timedelta, timezone
from unittest import mock

from campaign_tool import findings, today
from tests.test_finding_assign import run as assign_run
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


D = date(2026, 3, 4)  # DF1109B-14: the retirement day in every test below
DAY = timedelta(days=1)
OTHER_CAMPAIGN = "cmp_" + "0" * 32


def at(day, hour=12, minute=0, second=0):
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=timezone.utc)


def snapshot(root):
    """Every file and directory under ``root``: bytes, mode and mtime (a refusal must change none of them)."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None, p.lstat().st_mode, p.lstat().st_mtime_ns)
            for p in sorted(root.rglob("*"))}


class RetireReviewerTests(unittest.TestCase):
    """DF1109B-14: `finding reviewers retire`, its effect on assign/review/gates and explicit reinstatement."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-retire-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        self.assertEqual(assign_run("init", "--directory", self.root, "--name", "Synthetic", "--county",
                                    "Synthetic County", "--state", "ca")[0], 0)
        self.draft = draft()
        drafts = self.root / "private" / "drafts"
        drafts.mkdir(mode=0o700, parents=True, exist_ok=True)
        (drafts / "syn-finding-1.json").write_text(json.dumps(self.draft), encoding="utf-8")
        findings.add_reviewer(self.root, "Reviewer A", ["factual"], today=date(2026, 1, 1))
        findings.add_reviewer(self.root, "Reviewer B", ["legal", "privacy"], today=date(2026, 1, 1))
        self.roster = self.root / "private" / "reviewers.json"
        self.receipts = self.root / "private" / "reviews" / "syn-finding-1.json"
        self.assignments = self.root / "private" / "review-assignments" / "syn-finding-1.json"

    def retire(self, name, on=D, role=None, now=None):
        return findings.retire_reviewer(self.root, name, str(on), role, now=now or at(D + DAY))

    def approve(self, role, who, when, decision="approve", item=None):
        return findings.add_receipt(self.root, item or self.draft, role, decision, who, "Checked p. 1.", now=when)

    def approve_all(self, when):
        for role, who in (("factual", "Reviewer A"), ("legal", "Reviewer B"), ("privacy", "Reviewer B")):
            self.approve(role, who, when)

    def stored(self, role, who, reviewed_at, decision="approve", campaign=None):
        """Append one receipt as a hand-made fixture (exact reviewed_at; never through the refusing writer)."""
        receipts = json.loads(self.receipts.read_text(encoding="utf-8")) if self.receipts.exists() else []
        self.receipts.parent.mkdir(mode=0o700, exist_ok=True)
        receipts.append({"content_sha256": findings.reviewed_hash(self.draft), "role": role, "decision": decision,
                         "reviewer": who, "rationale": "Checked p. 1.", "reviewed_at": reviewed_at,
                         "campaign_id": campaign or findings.campaign_id(self.root)})
        self.receipts.write_text(json.dumps(receipts), encoding="utf-8")

    def blockers(self, item=None):
        return findings.review_status(self.root, item or self.draft)["review_blockers"]

    def entry(self, name):
        return next(e for e in json.loads(self.roster.read_text(encoding="utf-8"))["reviewers"] if e["name"] == name)

    def test_retire_one_role_all_roles_and_legacy_listing(self):
        legacy = findings.list_reviewers(self.root)
        self.assertNotIn("retired_on", json.dumps(legacy))  # a roster without retired_on reads as before
        out = self.retire("Reviewer B", role="legal")
        self.assertEqual((out["retired"], out["unchanged"], out["roles"], out["retired_on"]),
                         (["legal"], False, ["legal", "privacy"], {"legal": "2026-03-04"}))
        findings.assign(self.root, self.draft, "privacy", "Reviewer B", "2026-03-05", today=D + DAY, now=at(D + DAY))
        with self.assertRaisesRegex(ValueError, "legal role was retired on 2026-03-04"):
            findings.assign(self.root, self.draft, "legal", "Reviewer B", "2026-03-05", today=D + DAY, now=at(D + DAY))
        out = self.retire(" reviewer  b ")  # the same identity rules as add; every held role
        self.assertEqual((out["name"], out["retired"], out["unchanged"], out["retired_on"]),
                         ("Reviewer B", ["legal", "privacy"], False, {"legal": "2026-03-04", "privacy": "2026-03-04"}))
        before = snapshot(self.root)
        self.assertTrue(self.retire("Reviewer B")["unchanged"])  # the same day again: nothing written
        self.assertEqual(snapshot(self.root), before)
        listed = {e["name"]: e for e in findings.list_reviewers(self.root)["reviewers"]}
        self.assertEqual(listed["Reviewer B"], {"name": "Reviewer B", "roles": ["legal", "privacy"],
                                                "added_on": "2026-01-01",
                                                "retired_on": {"legal": "2026-03-04", "privacy": "2026-03-04"}})
        self.assertEqual(listed["Reviewer A"], legacy["reviewers"][0])
        findings.assign(self.root, self.draft, "factual", "Reviewer A", "2026-03-05", today=D + DAY, now=at(D + DAY))
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "reviewers", "retire",
                                 "--directory", str(self.root), "--name", "Reviewer A", "--on", "2026-03-04"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.rstrip().endswith("Private roster updated; nothing was published."))
        self.assertEqual(self.entry("Reviewer A")["retired_on"], {"factual": "2026-03-04"})
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "finding", "reviewers", "retire",
                                 "--directory", str(self.root), "--name", "Reviewer A"],
                                cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 2)  # --on is required
        self.assertIn("--on", result.stderr)

    def test_retirement_writer_refuses_bad_input_without_writes(self):
        empty = self.base / "empty"
        empty.mkdir()
        with self.assertRaisesRegex(ValueError, "no reviewer roster"):
            findings.retire_reviewer(empty, "Reviewer B", "2026-03-04", now=at(D))
        self.assertEqual(os.listdir(empty), [])
        self.retire("Reviewer B", role="legal")
        before = snapshot(self.root)
        for name, on, role, now, message in (
                ("Nobody", D, None, None, "not on the reviewer roster"),
                ("Reviewer A", D, "legal", None, "does not hold the legal role"),
                ("Reviewer A", D, "editor", None, "--role must be one of"),
                ("Reviewer A", "2026-3-04", None, None, "real YYYY-MM-DD"),
                ("Reviewer A", "2026-02-30", None, None, "real YYYY-MM-DD"),
                ("Reviewer A", D + DAY * 2, None, at(D + DAY), "after today"),
                ("Reviewer A", "2025-12-31", None, None, "before Reviewer A's added_on"),
                ("Reviewer B", D + DAY, None, at(D + DAY * 2), "legal role is already retired on 2026-03-04"),
                ("Reviewer B", D - DAY, "legal", None, "already retired on 2026-03-04"),
                ("", D, None, None, "--name must not be empty")):
            with self.subTest(name=name, on=on, role=role):
                with self.assertRaisesRegex(ValueError, message):
                    self.retire(name, on, role, now)
                self.assertEqual(snapshot(self.root), before)  # a mixed all-role request changed no role either
        good = self.roster.read_bytes()
        bad_rosters = {"symlink": None, "fifo": None, "malformed": b"{not json", "oversize": b" " * (256 * 1024 + 1)}
        for label, content in bad_rosters.items():
            with self.subTest(roster=label):
                self.roster.unlink()
                if label == "symlink":
                    (self.base / "elsewhere.json").write_bytes(good)
                    self.roster.symlink_to(self.base / "elsewhere.json")
                elif label == "fifo":
                    os.mkfifo(self.roster)
                else:
                    self.roster.write_bytes(content)
                state = snapshot(self.root)
                with self.assertRaises(ValueError):
                    self.retire("Reviewer A")
                self.assertEqual(snapshot(self.root), state)
        self.roster.unlink()
        self.roster.write_bytes(good)

    def test_stored_retired_on_is_validated_not_coerced(self):
        good = json.loads(self.roster.read_text(encoding="utf-8"))
        for value in (None, [], {"factual": "2026-03-04"}, {"legal": 20260304}, {"legal": "2026-3-04"},
                      {"legal": "2026-02-30"}, {"legal": "2025-12-31"}, {"legal": "٢٠٢٦-٠٣-٠٤"}):
            with self.subTest(value=value):
                document = json.loads(json.dumps(good))
                document["reviewers"][1]["retired_on"] = value
                self.roster.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "retired_on"):
                    findings.list_reviewers(self.root)
        document = json.loads(json.dumps(good))
        document["reviewers"][1]["retired_on"] = {}  # an empty map: nothing retired
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        self.approve("legal", "Reviewer B", at(D + DAY * 30))

    def test_retirement_serialized_roster_cap_and_idempotence(self):
        original = self.roster.read_bytes()
        with mock.patch.object(findings, "MAX_ROSTER_BYTES", len(original) + 200):
            self.retire("Reviewer B", role="legal")
        final = len(self.roster.read_bytes())
        self.assertLessEqual(final, len(original) + 200)
        self.roster.write_bytes(original)
        with mock.patch.object(findings, "MAX_ROSTER_BYTES", final - 1):
            before = snapshot(self.root)
            with self.assertRaisesRegex(ValueError, "larger than"):
                self.retire("Reviewer B", role="legal")
            self.assertEqual(snapshot(self.root), before)
        with mock.patch.object(findings, "MAX_ROSTER_BYTES", final):
            self.assertFalse(self.retire("Reviewer B", role="legal")["unchanged"])
            self.assertEqual(len(self.roster.read_bytes()), final)  # newline and indentation included
            self.assertEqual(findings._load_roster(self.root)["reviewers"][1]["retired_on"], {"legal": "2026-03-04"})
            self.assertTrue(self.retire("Reviewer B", role="legal")["unchanged"])
        with self.assertRaisesRegex(ValueError, "already retired"):
            self.retire("Reviewer B", D + DAY, "legal", now=at(D + DAY * 2))
        self.assertEqual(self.entry("Reviewer A"), {"name": "Reviewer A", "roles": ["factual"], "added_on": "2026-01-01"})

    def test_reviewed_before_retirement_stays_reviewed_everywhere(self):
        self.approve_all(at(D - DAY, 23, 59, 59))
        self.assertEqual(self.blockers(), [])
        receipts, signers = self.receipts.read_bytes(), (self.root / "private" / "reviewers.allowed_signers").read_bytes()
        self.retire("Reviewer A")
        self.retire("Reviewer B")
        for as_of in (D, D + DAY):
            self.assertEqual(self.blockers(), [])
            self.assertEqual({s["state"] for s in findings.assignment_states(self.root, self.draft, as_of).values()},
                             {"receipt_recorded"})
        status = findings.review_status(self.root, self.draft)
        self.assertEqual((status["unrostered_receipts"], status["self_asserted"]), ([], 3))
        self.assertEqual(self.receipts.read_bytes(), receipts)
        self.assertEqual((self.root / "private" / "reviewers.allowed_signers").read_bytes(), signers)
        edited = draft(summary="Synthetic summary, revised.")
        self.assertTrue({"missing_factual_review", "missing_legal_review", "missing_privacy_review"}
                        <= set(self.blockers(edited)))
        with self.assertRaisesRegex(ValueError, "retired"):
            self.approve("factual", "Reviewer A", at(D), item=edited)

    def test_retired_new_assignment_and_all_review_decisions_refused(self):
        findings.assign(self.root, self.draft, "legal", "Reviewer B", "2026-03-03", today=D - DAY, now=at(D - DAY))
        self.retire("Reviewer B", role="legal", now=at(D))
        before = snapshot(self.root)
        for kwargs in ({"on": "2026-03-01"}, {"on": "2026-03-03"}, {"on": "2026-03-04", "replace": True}):
            with self.subTest(**kwargs), self.assertRaisesRegex(ValueError, "legal role was retired on 2026-03-04"):
                findings.assign(self.root, self.draft, "legal", "Reviewer B", today=D, now=at(D, 0), **kwargs)
        for decision in findings.DECISIONS:
            with self.subTest(decision=decision), self.assertRaisesRegex(ValueError, "retired"):
                self.approve("legal", "Reviewer B", at(D, 0), decision)
            with self.subTest(payload=decision), self.assertRaisesRegex(ValueError, "retired"):
                findings.review_payload(self.root, self.draft, "legal", decision, "Reviewer B", "Checked.", now=at(D))
        with self.assertRaisesRegex(ValueError, "retired"):  # refused before the signature is read
            findings.add_receipt(self.root, self.draft, "legal", "approve", "Reviewer B", "Checked.", now=at(D),
                                 signature=self.base / "absent.sig")
        self.assertEqual(snapshot(self.root), before)
        self.approve("legal", "Reviewer B", at(D - DAY, 23, 59, 59))  # the day before it was still active
        self.approve("privacy", "Reviewer B", at(D + DAY))  # another role is untouched

    def test_retire_interleaved_with_assignment_or_receipt_write(self):
        real_receipts, real_assignments = findings._receipts_path, findings._assignments_path

        def receipts_after_retirement(root, item):  # runs after the early check, before the locked write
            findings.retire_reviewer(root, "Reviewer B", "2026-03-04", "legal", now=at(D))
            return real_receipts(root, item)

        def assignments_after_retirement(root, item):
            findings.retire_reviewer(root, "Reviewer B", "2026-03-04", "privacy", now=at(D))
            return real_assignments(root, item)

        with mock.patch.object(findings, "_receipts_path", receipts_after_retirement), \
                self.assertRaisesRegex(ValueError, "legal role was retired"):
            self.approve("legal", "Reviewer B", at(D))
        self.assertFalse(self.receipts.exists())
        with mock.patch.object(findings, "_assignments_path", assignments_after_retirement), \
                self.assertRaisesRegex(ValueError, "privacy role was retired"):
            findings.assign(self.root, self.draft, "privacy", "Reviewer B", "2026-03-04", today=D, now=at(D))
        self.assertFalse(self.assignments.exists())

    def test_retirement_does_not_erase_challenges_or_binding_interest_guards(self):
        self.draft = draft(agency_id="ex-agency")
        self.approve("legal", "Reviewer B", at(D - DAY), "reject")
        self.approve("factual", "Reviewer A", at(D - DAY))
        self.approve("privacy", "Reviewer B", at(D - DAY))
        self.stored("legal", "Reviewer B", "2026-03-03T10:00:00Z", campaign=OTHER_CAMPAIGN)
        self.retire("Reviewer B")
        self.stored("legal", "Reviewer B", "2026-03-05T10:00:00Z", decision="changes_requested")  # after the cutoff
        found = self.blockers()
        self.assertTrue({"unresolved_challenge", "foreign_campaign_receipt", "missing_legal_review"} <= set(found))
        self.assertNotIn("missing_privacy_review", found)
        document = json.loads(self.roster.read_text(encoding="utf-8"))
        document["reviewers"][1].update(interest_agencies=["ex-agency"], interest_note="Synthetic private note.")
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        self.assertIn("missing_privacy_review", self.blockers())  # a current declared interest still excludes
        out = findings.add_reviewer(self.root, "Reviewer B", ["legal", "privacy"])
        self.assertEqual(out["reinstated"], ["legal", "privacy"])
        self.assertNotIn("Synthetic private note.", json.dumps(out))
        self.assertEqual((self.entry("Reviewer B")["interest_agencies"], self.entry("Reviewer B")["interest_note"]),
                         (["ex-agency"], "Synthetic private note."))
        self.assertTrue({"missing_privacy_review", "unresolved_challenge"} <= set(self.blockers()))

    def test_open_retired_assignment_needs_reassignment_not_completed(self):
        findings.assign(self.root, self.draft, "legal", "Reviewer B", "2026-03-03", today=D - DAY, now=at(D - DAY))
        self.retire("Reviewer B", role="legal", now=at(D))
        before = snapshot(self.root)
        self.assertEqual(findings.assignment_states(self.root, self.draft, D - DAY)["legal"],
                         {"state": "assigned", "reviewer": "Reviewer B", "since": "2026-03-03"})
        reassign = {"state": "needs_reassignment", "reviewer": "Reviewer B", "since": "2026-03-03",
                    "retired_on": "2026-03-04"}
        self.assertEqual(findings.assignment_states(self.root, self.draft, D)["legal"], reassign)
        self.assertEqual(snapshot(self.root), before)  # status reads write nothing
        item = today._review_waiting(self.root, D + DAY * 7, "DIR", 7)
        self.assertEqual(len(item), 1)
        self.assertEqual((item[0]["kind"], item[0]["due"], item[0]["_key"]),
                         ("review_waiting", "2026-03-03", "syn-finding-1 legal"))
        self.assertIn("needs reassignment: their legal role was retired on 2026-03-04", item[0]["what"])
        self.assertIn("--role legal --reviewer 'ACTIVE REVIEWER' --on 2026-03-11 --replace", item[0]["command"])
        self.assertEqual(today._review_waiting(self.root, D + DAY, "DIR", 7), [])  # same age threshold as before
        self.stored("legal", "Reviewer B", "2026-03-04T00:00:00Z")  # on the cutoff
        self.stored("legal", "Reviewer A", "2026-03-01T00:00:00Z", campaign=OTHER_CAMPAIGN)
        self.stored("legal", "Reviewer B", "2026-03-03 23:00:00")  # naive
        self.assertEqual(findings.assignment_states(self.root, self.draft, D)["legal"], reassign)
        findings.add_reviewer(self.root, "Reviewer C", ["legal"], today=D)
        with self.assertRaisesRegex(ValueError, "use --replace"):
            findings.assign(self.root, self.draft, "legal", "Reviewer C", "2026-03-05", today=D + DAY, now=at(D + DAY))
        findings.assign(self.root, self.draft, "legal", "Reviewer C", "2026-03-05", today=D + DAY, now=at(D + DAY),
                        replace=True)
        self.assertEqual(findings.assignment_states(self.root, self.draft, D + DAY)["legal"],
                         {"state": "assigned", "reviewer": "Reviewer C", "since": "2026-03-05"})
        self.stored("legal", "Reviewer B", "2026-03-03T23:59:59+00:00")  # a valid receipt from before the cutoff
        self.assertEqual(findings.assignment_states(self.root, self.draft, D + DAY)["legal"],
                         {"state": "receipt_recorded"})

    def test_add_reinstates_only_explicit_roles_and_preserves_identity(self):
        document = json.loads(self.roster.read_text(encoding="utf-8"))
        document["reviewers"][1].update(ssh_public_key="ssh-ed25519 " + "A" * 68, interest_agencies=["ex-agency"],
                                        interest_note="Synthetic private note.")
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        self.approve("legal", "Reviewer B", at(D - DAY))
        self.retire("Reviewer B")
        kept = {k: v for k, v in self.entry("Reviewer B").items() if k not in ("roles", "retired_on")}
        out = findings.add_reviewer(self.root, "reviewer b", ["legal"])
        self.assertEqual((out["reinstated"], out["retired_on"]), (["legal"], {"privacy": "2026-03-04"}))
        self.assertEqual({k: v for k, v in self.entry("Reviewer B").items() if k not in ("roles", "retired_on")}, kept)
        findings.assign(self.root, self.draft, "legal", "Reviewer B", "2026-03-05", today=D + DAY, now=at(D + DAY))
        with self.assertRaisesRegex(ValueError, "privacy role was retired"):
            findings.assign(self.root, self.draft, "privacy", "Reviewer B", "2026-03-05", today=D + DAY, now=at(D + DAY))
        self.assertIn("missing_privacy_review", self.blockers())  # reinstating grants no review
        out = findings.add_reviewer(self.root, "Reviewer B", ["factual", "privacy"])
        self.assertEqual((out["reinstated"], out["roles"]), (["privacy"], ["factual", "legal", "privacy"]))
        self.assertNotIn("retired_on", self.entry("Reviewer B"))  # the legacy shape again
        self.assertNotIn("reinstated", findings.add_reviewer(self.root, "Reviewer B", ["legal"]))
        receipts = self.receipts.read_bytes()
        self.retire("Reviewer B", D + DAY, "legal", now=at(D + DAY * 2))  # the new current cutoff
        self.assertEqual(self.entry("Reviewer B")["retired_on"], {"legal": "2026-03-05"})
        self.assertEqual(self.receipts.read_bytes(), receipts)
        self.assertNotIn("missing_legal_review", self.blockers())  # reviewed on D-1, before the new cutoff


if __name__ == "__main__":
    unittest.main()
