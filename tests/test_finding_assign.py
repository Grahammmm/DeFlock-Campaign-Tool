"""DF1207-10: `finding assign`, private review assignments beside the receipts. Synthetic fixtures only.

Run from the repository root: python3 -B -m unittest tests.test_finding_assign -v
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, findings, today  # noqa: E402
from tests import test_finding_packet as packet_tests  # noqa: E402
from tests.test_findings_review import draft  # noqa: E402

AS_OF = date(2026, 3, 15)
NOTE = "Recorded only. Nothing was sent. An assignment is not a review."


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class FindingAssignTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-finding-assign-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "campaign"
        self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "Synthetic County", "--state", "ca")
        self.draft = draft()
        self.path = self.root / "private" / "drafts" / "syn-finding-1.json"
        self.path.parent.mkdir(mode=0o700, parents=True)
        self.path.write_text(json.dumps(self.draft), encoding="utf-8")
        self.file = self.root / "private" / "review-assignments" / "syn-finding-1.json"

    def ok(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 0, err)
        return out

    def refused(self, *arguments):
        code, out, err = run(*arguments)
        self.assertNotEqual(code, 0, out)
        self.assertTrue(err.startswith("Stopped: "), err)
        self.assertNotIn("Traceback", err)
        return err

    def assign(self, role="factual", reviewer="Rev One", on="2026-03-01", *extra, refused=None):
        arguments = ("finding", "assign", self.path, "--directory", self.root, "--role", role, "--reviewer", reviewer,
                     "--on", on, *extra)
        return self.ok(*arguments) if refused is None else self.refused(*arguments)

    def printed(self, out):
        self.assertTrue(out.endswith("\n" + NOTE + "\n"), out)
        return json.loads(out[:-len(NOTE) - 1])

    def status(self):
        return json.loads(self.ok("finding", "review-status", self.path, "--directory", self.root))

    def today_items(self, *extra):
        out = self.ok("today", "--directory", self.root, "--as-of", AS_OF.isoformat(), "--json", *extra)
        return [i for i in json.loads(out) if i["kind"] == "review_waiting"]

    def receipt(self, role="factual", reviewer="Rev One"):
        findings.add_receipt(self.root, self.draft, role, "approve", reviewer, "Checked p. 1.")

    def roster(self, name, *roles):
        findings.add_reviewer(self.root, name, list(roles))

    # 1
    def test_assigned_then_receipt_recorded_and_others_unassigned(self):
        out = self.assign("factual", "Rev One", "2026-03-01")
        self.assertTrue(out.endswith(NOTE + "\n"), out)
        self.assertEqual(self.printed(out)["reviewer"], "Rev One")
        self.assertEqual(self.status()["assignments"], {
            "factual": {"state": "assigned", "reviewer": "Rev One", "since": "2026-03-01"},
            "legal": {"state": "unassigned"}, "privacy": {"state": "unassigned"}})
        self.receipt("factual", "Rev Two")  # any reviewer's current receipt for the role counts as recorded
        self.assertEqual(self.status()["assignments"], {
            "factual": {"state": "receipt_recorded"}, "legal": {"state": "unassigned"},
            "privacy": {"state": "unassigned"}})
        self.assertEqual(self.file.stat().st_mode & 0o777, 0o600)
        stored = json.loads(self.file.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(set(stored["assignments"]["factual"]), {"reviewer", "on", "recorded_at"})

    # 2
    def test_author_refused_in_any_spelling(self):
        for name in ("Ada  Lovelace", "ada lovelace", "  ADA   LOVELACE "):
            self.assertIn("author cannot review", self.assign("legal", name, refused=True))
        self.assertFalse(self.file.exists())

    # 3
    def test_roster_refuses_unlisted_name_or_missing_role_and_no_roster_accepts_any(self):
        self.assign("factual", "Anyone At All")  # no roster: any non-author name
        self.roster("Rev One", "factual")
        self.assertIn("is not on the reviewer roster", self.assign("legal", "Stranger", refused=True))
        self.assertIn("is not assigned the legal role", self.assign("legal", "rev  one", refused=True))
        self.assign("factual", "REV ONE", "2026-03-02", "--replace")
        self.assertEqual(self.status()["assignments"]["factual"]["reviewer"], "REV ONE")
        self.assertNotIn("legal", json.loads(self.file.read_text(encoding="utf-8"))["assignments"])

    # 4
    def test_second_reviewer_needs_replace_and_same_reviewer_is_a_no_op(self):
        self.assign("legal", "Rev One", "2026-03-01")
        before = self.file.read_bytes()
        err = self.assign("legal", "Rev Two", "2026-03-02", refused=True)
        self.assertIn("already assigned to Rev One since 2026-03-01", err)
        self.assertEqual(self.file.read_bytes(), before)
        again = self.printed(self.assign("legal", " rev one ", "2026-03-05"))
        self.assertTrue(again["unchanged"])
        self.assertEqual(self.file.read_bytes(), before)  # the original date and time are kept
        self.assign("legal", "Rev Two", "2026-03-06", "--replace")
        self.assertEqual(self.status()["assignments"]["legal"],
                         {"state": "assigned", "reviewer": "Rev Two", "since": "2026-03-06"})

    # 5
    def test_assignments_change_no_blocker_and_publish_is_still_refused(self):
        before = self.status()
        for role in ("factual", "legal", "privacy"):
            self.assign(role, "Rev One")
        after = self.status()
        self.assertEqual(len(after["assignments"]), 3)
        self.assertTrue(all(v["state"] == "assigned" for v in after["assignments"].values()))
        self.assertEqual({k: v for k, v in after.items() if k != "assignments"},
                         {k: v for k, v in before.items() if k != "assignments"})
        self.assertEqual(after["review_blockers"], before["review_blockers"])
        self.assertTrue({"missing_factual_review", "missing_legal_review", "missing_privacy_review"}
                        <= set(after["review_blockers"]))
        self.assertEqual(findings.review_status(self.root, self.draft), {k: v for k, v in before.items() if k != "assignments"})
        self.refused("finding", "publish", self.path, "--directory", self.root, "--approved-by", "Pat Organizer")
        self.assertFalse((self.root / "content" / "findings" / "synthetic-title.json").exists())

    # 6
    def test_today_lists_old_unreviewed_assignments_only(self):
        self.assign("factual", "Rev One", "2026-03-08")  # exactly 7 days
        self.assign("legal", "Rev Two", "2026-03-09")  # 6 days
        self.assign("privacy", "Rev Three", "2026-03-01")  # 14 days, but reviewed below
        self.receipt("privacy", "Rev Four")
        items = self.today_items()
        self.assertEqual([i["what"] for i in items], ["Review (factual) of syn-finding-1 assigned to Rev One since 2026-03-08"])
        self.assertEqual(items[0]["command"], f"finding review-status {self.path} --directory {self.root}")
        self.assertEqual(set(items[0]), {"rank", "kind", "what", "due", "command"})
        self.assertEqual([i["what"].split()[1] for i in self.today_items("--review-days", "6")],
                         ["(factual)", "(legal)"])
        self.assertEqual(self.today_items("--review-days", "15"), [])
        self.assertIn("[review_waiting]", self.ok("today", "--directory", self.root, "--as-of", "2026-03-15"))
        self.assertEqual(today.todo(self.root, AS_OF, review_days=7)[0]["kind"], "review_waiting")
        for bad in ("0", "91", "-1", "x", "1.5"):
            self.assertIn("--review-days", self.refused_argument("today", "--directory", self.root, "--review-days", bad))

    def refused_argument(self, *arguments):
        code, out, err = run(*arguments)
        self.assertEqual(code, 2, out)
        return err

    # 7
    def test_stale_receipt_reads_assigned_again_and_today_lists_it(self):
        self.assign("factual", "Rev One", "2026-03-01")
        self.receipt("factual", "Rev One")
        self.assertEqual(self.today_items(), [])
        changed = dict(self.draft, summary="Synthetic summary, edited after review.")
        self.path.write_text(json.dumps(changed), encoding="utf-8")
        self.assertEqual(self.status()["assignments"]["factual"],
                         {"state": "assigned", "reviewer": "Rev One", "since": "2026-03-01"})
        self.assertEqual(len(self.today_items()), 1)

    # 8
    def test_symlinked_folder_refused_and_missing_file_changes_nothing(self):
        plain = self.status()
        self.assertEqual(plain["assignments"], {r: {"state": "unassigned"} for r in ("factual", "legal", "privacy")})
        self.assertEqual({k: v for k, v in plain.items() if k != "assignments"}, findings.review_status(self.root, self.draft))
        self.assertEqual(self.today_items(), [])
        self.assertFalse((self.root / "private" / "review-assignments").exists())  # reading never creates it
        elsewhere = self.base / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.root / "private" / "review-assignments")
        self.assertIn("must be a real directory", self.assign("factual", "Rev One", refused=True))
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.refused("finding", "review-status", self.path, "--directory", self.root)
        unreadable = [i for i in json.loads(self.ok("today", "--directory", self.root, "--as-of", "2026-03-15", "--json"))
                      if i["kind"] == "unreadable"]
        self.assertEqual([i["what"].split(":")[0] for i in unreadable], ["fix private/review-assignments/syn-finding-1.json"])

    def test_damaged_assignment_file_is_refused_not_trusted(self):
        self.file.parent.mkdir(mode=0o700)
        for text in ("[]", '{"schema_version": 1, "assignments": {"factual": {"reviewer": "R", "on": "2026-02-30", '
                     '"recorded_at": "x"}}}', '{"schema_version": 1, "assignments": {"boss": {}}}', "{"):
            self.file.write_text(text, encoding="utf-8")
            self.refused("finding", "review-status", self.path, "--directory", self.root)
            self.assign("legal", "Rev One", refused=True)

    def test_date_and_input_refusals_write_nothing(self):
        self.assertIn("--on must not be after today", self.assign("factual", "Rev One", "2999-01-01", refused=True))
        for bad in ("2026-02-30", "03/01/2026", "", "2026-3-1"):
            self.assertIn("--on must be a real YYYY-MM-DD date", self.assign("factual", "Rev One", bad, refused=True))
        self.assertIn("--role must be one of", self.assign("boss", "Rev One", refused=True))
        self.assertIn("--reviewer must not be empty", self.assign("factual", "  ", refused=True))
        self.assertFalse(self.file.exists())
        self.assertEqual(findings.assign(self.root, self.draft, "factual", "Rev One", "2026-03-15", today=AS_OF)["on"],
                         "2026-03-15")  # the day itself is allowed


class AssignmentPacketTests(unittest.TestCase):
    """Edge 9: the packet handed to an outside reviewer must not carry any assignee name."""
    setUp, cli, packet = (packet_tests.FindingPacketTests.setUp, packet_tests.FindingPacketTests.cli,
                          packet_tests.FindingPacketTests.packet)

    def test_packet_holds_no_assignee_name_or_assignment_key(self):
        for role, name in (("factual", "Zed Assignee"), ("legal", "Yan Assignee")):
            findings.assign(self.root, packet_tests.draft(), role, name, "2026-03-01", today=AS_OF)
        self.assertTrue((self.root / "private" / "review-assignments" / "fnd_00000000000000p1.json").exists())
        self.packet()
        text = b"".join(p.read_bytes() for p in sorted(self.out.rglob("*")) if p.is_file())
        self.assertNotIn(b"Assignee", text)
        self.assertNotIn(b"review-assignments", text)
        status = json.loads((self.out / "review-status.json").read_text(encoding="utf-8"))
        self.assertEqual(status, findings.review_status(self.root, packet_tests.draft()))
        self.assertNotIn("assignments", status)


if __name__ == "__main__":
    unittest.main()
