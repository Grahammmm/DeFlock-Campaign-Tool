"""RF1345-5: assignment display and overdue reminders honour the campaign binding. Synthetic fixtures only.

Run from the repository root: python3 -B -m unittest tests.test_finding_assignment_binding -v
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
from campaign_tool import cli, findings  # noqa: E402
from tests.test_findings_review import draft  # noqa: E402

AS_OF = date(2026, 3, 15)
ROLES = ("factual", "legal", "privacy")


def run(*arguments):
    out, err, code = io.StringIO(), io.StringIO(), 0
    with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    rows = {}
    for path in sorted(Path(root).rglob("*")):
        info = path.lstat()
        rows[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns,
                                             path.read_bytes() if path.is_file() and not path.is_symlink() else None)
    return rows


class AssignmentBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-assignment-binding-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.draft = draft()
        self.root = self.campaign("local")
        self.path = self.root / "private" / "drafts" / "syn-finding-1.json"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.draft), encoding="utf-8")
        self.other = self.campaign("other")
        self.receipts = self.root / "private" / "reviews" / "syn-finding-1.json"

    def campaign(self, name):
        root = self.base / name
        code, out, err = run("init", "--directory", root, "--name", "Synthetic", "--county", "Synthetic County",
                             "--state", "ca")
        self.assertEqual(code, 0, err)
        return root

    def assign(self, *roles):
        for role in roles:
            code, out, err = run("finding", "assign", self.path, "--directory", self.root, "--role", role,
                                 "--reviewer", "Rev One", "--on", "2026-03-01")
            self.assertEqual(code, 0, err)

    def foreign(self, role="factual", decision="approve", reviewer="Rev One"):
        """A real receipt from the other campaign, copied beside this campaign's identical draft."""
        findings.add_receipt(self.other, self.draft, role, decision, reviewer, "Checked p. 1.")
        source = self.other / "private" / "reviews" / "syn-finding-1.json"
        rows = json.loads(self.receipts.read_text(encoding="utf-8")) if self.receipts.exists() else []
        self.receipts.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.receipts.write_text(json.dumps(rows + json.loads(source.read_text(encoding="utf-8"))), encoding="utf-8")
        source.unlink()

    def local(self, role="factual", decision="approve", reviewer="Rev One"):
        findings.add_receipt(self.root, self.draft, role, decision, reviewer, "Checked p. 1.")

    def rewrite(self, change):
        rows = json.loads(self.receipts.read_text(encoding="utf-8"))
        self.receipts.write_text(json.dumps(change(rows)), encoding="utf-8")

    def states(self):
        return findings.assignment_states(self.root, self.draft)

    def status(self):
        code, out, err = run("finding", "review-status", self.path, "--directory", self.root)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def today(self, *extra):
        code, out, err = run("today", "--directory", self.root, "--as-of", AS_OF.isoformat(), "--json", *extra)
        self.assertEqual(code, 0, err)
        return [i["what"] for i in json.loads(out) if i["kind"] == "review_waiting"]

    def waiting(self, *roles):
        return [f"Review ({role}) of syn-finding-1 assigned to Rev One since 2026-03-01" for role in roles]

    def test_foreign_current_receipt_keeps_each_role_assigned_and_due(self):
        self.local("factual", reviewer="Rev Two")  # gives the local campaign an id; other roles stay unreviewed
        self.assertEqual(self.states()["factual"], {"state": "receipt_recorded"})
        for role in ROLES:
            self.foreign(role)
        self.rewrite(lambda rows: [r for r in rows if r["campaign_id"] != self.local_id()])
        self.assign(*ROLES)
        assigned = {"state": "assigned", "reviewer": "Rev One", "since": "2026-03-01"}
        self.assertEqual(self.states(), {role: assigned for role in ROLES})
        self.assertEqual(self.status()["assignments"], {role: assigned for role in ROLES})
        self.assertIn("foreign_campaign_receipt", self.status()["review_blockers"])
        self.assertEqual(self.today(), self.waiting(*ROLES))
        code, out, err = run("today", "--directory", self.root, "--as-of", AS_OF.isoformat())
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("[review_waiting]"), 3)

    def local_id(self):
        return findings.campaign_id(self.root)

    def test_unbound_or_invalid_receipt_binding_cannot_complete_local_assignment(self):
        self.local("legal")  # local identity exists
        self.foreign("factual")
        original = [r for r in json.loads(self.receipts.read_text(encoding="utf-8")) if r["role"] == "factual"][0]
        for label, mutate in (("missing", lambda r: r.pop("campaign_id")), ("null", lambda r: r.update(campaign_id=None)),
                              ("empty", lambda r: r.update(campaign_id="")), ("int", lambda r: r.update(campaign_id=5)),
                              ("list", lambda r: r.update(campaign_id=[self.local_id()])),
                              ("foreign", lambda r: None)):
            row = dict(original)
            mutate(row)
            local_row = [r for r in json.loads(self.receipts.read_text(encoding="utf-8")) if r["role"] == "legal"]
            self.receipts.write_text(json.dumps(local_row + [row]), encoding="utf-8")
            before = self.receipts.read_bytes()
            with self.subTest(label, assigned=False):
                self.assertEqual(self.states()["factual"], {"state": "unassigned"})
            self.assertEqual(self.receipts.read_bytes(), before)
        self.assign("factual")
        with self.subTest("assigned fallback"):
            self.assertEqual(self.states()["factual"]["state"], "assigned")
            self.assertEqual(self.today(), self.waiting("factual"))

    def test_missing_local_identity_never_matches_an_unbound_receipt_or_creates_identity(self):
        self.foreign("factual")
        (self.root / "private" / "campaign-id.json").unlink()  # init writes one; this case has none
        self.assertIsNone(findings.campaign_id(self.root))
        unbound = [{k: v for k, v in r.items() if k != "campaign_id"}
                   for r in json.loads(self.receipts.read_text(encoding="utf-8"))]
        self.assign("factual")
        for rows in (json.loads(self.receipts.read_text(encoding="utf-8")), unbound,
                     [dict(unbound[0], campaign_id=None)]):
            self.receipts.write_text(json.dumps(rows), encoding="utf-8")
            before = snapshot(self.root)
            self.assertEqual(self.states()["factual"]["state"], "assigned")
            self.assertEqual(self.status()["assignments"]["factual"]["state"], "assigned")
            self.assertEqual(self.today(), self.waiting("factual"))
            self.assertEqual(snapshot(self.root), before)
            self.assertFalse((self.root / "private" / "campaign-id.json").exists())

    def test_bound_current_receipts_from_any_reviewer_preserve_recorded_semantics(self):
        for role, decision in zip(ROLES, ("approve", "reject", "changes_requested")):
            self.local(role, decision, "Rev Two")  # not the assignee
        self.assign(*ROLES)
        self.assertEqual(self.states(), {role: {"state": "receipt_recorded"} for role in ROLES})
        self.assertEqual(self.today(), [])
        blockers = self.status()["review_blockers"]
        self.assertIn("unresolved_challenge", blockers)
        self.assertNotIn("missing_factual_review", blockers)
        self.assertNotIn("foreign_campaign_receipt", blockers)

    def test_mixed_binding_hashes_and_roles_do_not_cross_complete(self):
        self.local("factual")
        self.local("legal", reviewer="Rev Two")
        self.foreign("privacy")
        rows = json.loads(self.receipts.read_text(encoding="utf-8"))
        stale = dict(rows[0], role="privacy", content_sha256="0" * 64, campaign_id=self.local_id())
        unknown = dict(rows[0], role="nonsense")
        listed = dict(rows[0], role=["privacy"])
        self.assign(*ROLES)
        for order in (rows + [stale, unknown, listed, "junk", 7], list(reversed(rows + [stale, unknown, listed, "junk", 7]))):
            self.receipts.write_text(json.dumps(order), encoding="utf-8")
            states = self.states()
            self.assertEqual(states["factual"], {"state": "receipt_recorded"})
            self.assertEqual(states["legal"], {"state": "receipt_recorded"})
            self.assertEqual(states["privacy"]["state"], "assigned")
            self.assertEqual(sorted(states), list(ROLES))
        self.assertEqual(self.today(), self.waiting("privacy"))
        self.assertIn("foreign_campaign_receipt", self.status()["review_blockers"])

    def test_explicit_legacy_binding_restores_completion_without_assignment_edits(self):
        self.local("legal")  # local identity exists
        self.local("factual")
        self.rewrite(lambda rows: [r for r in rows if r["role"] == "legal"]
                     + [{k: v for k, v in r.items() if k != "campaign_id"} for r in rows if r["role"] == "factual"])
        self.assign("factual")
        assignments = (self.root / "private" / "review-assignments" / "syn-finding-1.json")
        kept = assignments.read_bytes()
        self.assertEqual(self.states()["factual"]["state"], "assigned")
        self.assertEqual(self.today(), self.waiting("factual"))
        self.assertIn("unbound_legacy_receipt", self.status()["review_blockers"])
        self.assertEqual(self.states()["factual"]["state"], "assigned")  # reads never migrate
        findings.bind_receipts(self.root, confirm=True)
        self.assertEqual(self.states()["factual"], {"state": "receipt_recorded"})
        self.assertEqual(self.today(), [])
        self.assertEqual(assignments.read_bytes(), kept)

    def test_due_boundary_and_read_only_cli_views(self):
        self.foreign("factual")
        self.local("legal")
        self.assign("factual")
        before = snapshot(self.root)
        for _ in range(2):
            self.assertEqual(self.today("--review-days", "14"), self.waiting("factual"))  # exactly 14 days old
            self.assertEqual(self.today("--review-days", "15"), [])  # one day below the threshold
            self.assertEqual(self.today(), self.waiting("factual"))
            self.assertEqual(self.status()["assignments"]["factual"]["state"], "assigned")
            self.states()
        self.assertEqual(snapshot(self.root), before)

    def test_malformed_or_linked_identity_refuses_without_mutation(self):
        self.foreign("factual")
        self.assign("factual")
        identity = self.root / "private" / "campaign-id.json"
        elsewhere = self.base / "elsewhere.json"
        elsewhere.write_text(json.dumps({"schema_version": 1, "campaign_id": "cmp_" + "a" * 32,
                                         "created_at": "2026-03-01T00:00:00Z"}), encoding="utf-8")
        for label, make in (("malformed", lambda: identity.write_text("{not json", encoding="utf-8")),
                            ("wrong shape", lambda: identity.write_text('{"campaign_id": "x"}', encoding="utf-8")),
                            ("linked", lambda: os.symlink(elsewhere, identity))):
            if os.path.lexists(identity):
                identity.unlink()
            make()
            before = snapshot(self.root)
            with self.subTest(label):
                with self.assertRaises(ValueError):
                    self.states()
                code, out, err = run("finding", "review-status", self.path, "--directory", self.root)
                self.assertNotEqual(code, 0, out)
                self.assertTrue(err.startswith("Stopped: "), err)
                self.assertNotIn("Traceback", err)
                self.assertNotIn("Checked p. 1.", err + out)
                self.assertEqual(snapshot(self.root), before)
                self.assertEqual(os.path.islink(identity), label == "linked")


if __name__ == "__main__":
    unittest.main()
