"""DF0022-05: private reviewer declared interests (`finding reviewers add --interest-agency/--interest-note`).

A rostered reviewer whose declared agencies include a finding's agency_id or any also_agency_ids cannot review it,
and earlier approvals from such a reviewer count toward no role and no person. Synthetic names and agencies only.
Run from the repository root: python3 -B -m unittest tests.test_reviewer_interests -v
"""
import copy
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from campaign_tool.review import declared_interest_agencies, review_blockers  # noqa: E402
from campaign_tool.site import build_site  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)
from tests.approval_signing import Signer  # noqa: E402

draft, NOW = publish_tests.draft, publish_tests.NOW
POLICE, SHERIFF, COUNCIL = "ca-cedar-police", "ca-cedar-county-sheriff", "ca-cedar-city-council"
NOTE = "SENTINEL-NOTE-7731 works for the police records unit"
EXTRA = [f"ca-syn-agency-{i:02d}" for i in range(25)]
LATER = publish_tests.datetime(2026, 3, 10, 12, 0, 0, tzinfo=publish_tests.timezone.utc)


class ReviewerInterestTests(unittest.TestCase):
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, log_lines = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["log_lines"])

    def setUp(self):
        self.setUp_campaign()
        agencies = [{"agency_id": item, "name": "Synthetic " + item} for item in (POLICE, SHERIFF, COUNCIL, *EXTRA)]
        (self.content / "agencies.json").write_text(json.dumps(agencies), encoding="utf-8")
        self.roster = self.root / "private" / "reviewers.json"
        self.text = {"agency_id": POLICE, "also_agency_ids": [SHERIFF]}
        self.write_draft(draft(**self.text))
        self.doc = json.loads(self.draft.read_text(encoding="utf-8"))

    # helpers ---------------------------------------------------------------------------------------------------
    def add(self, name, roles=("factual", "legal", "privacy"), *interests, note=None, **kwargs):
        return findings.add_reviewer(self.root, name, list(roles), interest_agencies=list(interests) or None,
                                     interest_note=note, **kwargs)

    def rostered(self, name):
        return findings._rostered(findings._load_roster(self.root), name)

    def refuses(self, needle, *args, **kwargs):
        before = self.tree()
        with self.assertRaises(ValueError) as caught:
            self.add(*args, **kwargs)
        self.assertIn(needle, str(caught.exception))
        self.assertNotIn("SENTINEL", str(caught.exception))
        self.assertEqual(self.tree(), before)

    def tree(self):
        return {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                for p in sorted(self.root.rglob("*"))}

    def status(self):
        return findings.review_status(self.root, self.doc)

    def blockers(self):
        return self.status()["review_blockers"]

    def approve(self, role, name, document=None):
        self.receipt(role, name, document=document or self.doc)

    def approve_three_roles(self):
        self.approve("factual", "Alice Smith")
        self.approve("legal", "Bob Jones")
        self.approve("privacy", "Alice Smith")

    def refused_review(self, name, role="factual", decision="approve", needle=None):
        before = self.tree()
        with self.assertRaises(ValueError) as caught:
            findings.add_receipt(self.root, self.doc, role, decision, name, "Checked.")
        self.assertIn(needle, str(caught.exception))
        self.assertNotIn("SENTINEL", str(caught.exception))
        self.assertEqual(self.tree(), before)

    # roster -------------------------------------------------------------------------------------------------------
    def test_roster_legacy_replace_and_clear(self):
        legacy = self.add("Alice Smith", ["factual"])
        self.assertNotIn("interest_agencies", legacy)
        self.assertNotIn("interest_note", self.rostered("Alice Smith"))
        first = self.add("Alice Smith", ["legal"], POLICE, SHERIFF, note=NOTE)
        self.assertEqual(first["interest_agencies"], [POLICE, SHERIFF])
        self.assertNotIn("interest_note", first)  # the add result never shows the note
        row = self.rostered("Alice Smith")
        self.assertEqual((row["interest_agencies"], row["interest_note"], row["roles"], row["added_on"]),
                         ([POLICE, SHERIFF], NOTE, ["factual", "legal"], legacy["added_on"]))
        self.assertEqual(stat.S_IMODE(self.roster.stat().st_mode), 0o600)
        listing = findings.list_reviewers(self.root)
        self.assertEqual(listing["reviewers"][0]["interest_agencies"], [POLICE, SHERIFF])
        self.assertNotIn("interest_note", listing["reviewers"][0])
        self.add("Alice Smith", ["privacy"])  # no interest flags: the declaration is kept as it was
        self.assertEqual({k: v for k, v in self.rostered("Alice Smith").items() if k.startswith("interest")},
                         {"interest_agencies": [POLICE, SHERIFF], "interest_note": NOTE})
        self.add("Alice Smith", ["privacy"], COUNCIL)  # supplied agencies replace, an omitted old note stays
        row = self.rostered("Alice Smith")
        self.assertEqual((row["interest_agencies"], row["interest_note"]), ([COUNCIL], NOTE))
        self.add("Alice Smith", ["privacy"], note="x")  # 1 character; agencies kept
        self.add("Alice Smith", ["privacy"], note="é" * 200)
        self.assertEqual(self.rostered("Alice Smith")["interest_note"], "é" * 200)
        self.add("Bob Jones", ["legal"], *EXTRA[:20])  # 20 is the maximum
        self.assertEqual(len(self.rostered("Bob Jones")["interest_agencies"]), 20)
        self.add("Alice Smith", ["privacy"], clear_interests=True)
        row = self.rostered("Alice Smith")
        self.assertFalse({"interest_agencies", "interest_note"} & set(row))
        self.assertEqual(row["roles"], ["factual", "legal", "privacy"])

    def test_roster_refusals_write_nothing(self):
        self.add("Alice Smith", ["factual"], POLICE, note=NOTE)
        before = self.tree()
        with self.assertRaises(ValueError) as caught:  # an empty list is not a declaration: use --clear-interests
            findings.add_reviewer(self.root, "Alice Smith", ["factual"], interest_agencies=[])
        self.assertIn("must be a list of 1 to 20", str(caught.exception))
        self.assertEqual(self.tree(), before)
        self.refuses("must be a list of 1 to 20", "Alice Smith", ["factual"], *EXTRA[:21])
        self.refuses("repeats an earlier agency id", "Alice Smith", ["factual"], POLICE, POLICE)
        self.refuses("unknown agency 'ca-ghost'", "Alice Smith", ["factual"], "ca-ghost")
        self.refuses("not a valid agency id", "Alice Smith", ["factual"], "Bad Id")
        self.refuses("not a valid agency id", "Alice Smith", ["factual"], POLICE.upper())
        self.refuses("not a valid agency id", "Alice Smith", ["factual"], 5)
        self.refuses("not a valid agency id", "Alice Smith", ["factual"], None)
        for bad in ("", "   ", "x" * 201, "a\nb", "a\u200bb", "a\x00b", 5):
            self.refuses("--interest-note must be 1 to 200", "Alice Smith", ["factual"], note=bad)
        self.refuses("needs at least one --interest-agency", "Carol New", ["factual"], note="a note")
        self.refuses("cannot be combined", "Alice Smith", ["factual"], POLICE, clear_interests=True)
        self.refuses("cannot be combined", "Alice Smith", ["factual"], note="a note", clear_interests=True)
        self.refuses("--role must be one of", "Alice Smith", ["editor"], POLICE)
        self.add("Alice Smith", ["factual"], clear_interests=True)
        self.refuses("needs at least one --interest-agency", "Alice Smith", ["factual"], note="a note")

    def test_cli_flags_replace_clear_and_stay_private(self):
        def add(*flags, returncode=0):
            return self.cli("finding", "reviewers", "add", "--directory", self.root, "--name", "Alice Smith",
                            "--role", "factual", *flags, returncode=returncode)
        result = add("--interest-agency", POLICE, "--interest-agency", COUNCIL, "--interest-note", NOTE)
        self.assertIn(f'"{POLICE}"', result.stdout)
        self.assertNotIn("SENTINEL", result.stdout + result.stderr)
        listed = self.cli("finding", "reviewers", "list", "--directory", self.root)
        self.assertNotIn("SENTINEL", listed.stdout + listed.stderr)
        self.assertIn("SENTINEL-NOTE-7731", self.roster.read_text(encoding="utf-8"))
        self.assertIn("cannot be combined", add("--interest-note", "a", "--clear-interests", returncode=1).stderr)
        self.assertIn("cannot be combined", add("--interest-agency", POLICE, "--clear-interests", returncode=1).stderr)
        add("--clear-interests")
        self.assertNotIn("interest", self.roster.read_text(encoding="utf-8"))

    def test_roster_read_checks_format_not_existence(self):
        self.add("Alice Smith", ["factual"], POLICE, note=NOTE)
        document = json.loads(self.roster.read_text(encoding="utf-8"))
        document["reviewers"][0]["interest_agencies"] = ["ca-retired-agency"]  # an agency that no longer exists
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        self.assertEqual(findings.list_reviewers(self.root)["reviewers"][0]["interest_agencies"],
                         ["ca-retired-agency"])
        for value in (["ca-a-b", "ca-a-b"], EXTRA[:21], POLICE, None, ["Not An Id"], [3]):
            document["reviewers"][0]["interest_agencies"] = value
            self.roster.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(agencies=value), self.assertRaises(ValueError):
                findings.list_reviewers(self.root)
        document["reviewers"][0]["interest_agencies"] = [POLICE]
        for value in (7, "", "x" * 201, "a\nb SENTINEL", None):
            document["reviewers"][0]["interest_note"] = value
            self.roster.write_text(json.dumps(document), encoding="utf-8")
            with self.subTest(note=value), self.assertRaises(ValueError) as caught:
                findings.list_reviewers(self.root)
            self.assertNotIn("SENTINEL", str(caught.exception))

    # refusal ------------------------------------------------------------------------------------------------------
    def test_primary_extra_exact_matching_and_private_refusal(self):
        self.add("Alice Smith", ["factual"], SHERIFF, note=NOTE)  # an extra agency of the finding
        self.add("Bob Jones", ["factual"], COUNCIL, note=NOTE)  # unrelated, then a prefix-like id of POLICE
        document = json.loads(self.roster.read_text(encoding="utf-8"))
        document["reviewers"][1]["interest_agencies"].append("ca-cedar")
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        self.add("Carol Diaz", ["factual", "legal"], SHERIFF, POLICE)
        self.add("Dan Lee", ["factual"])
        before = self.tree()
        for name in ("Alice Smith", "  alice   SMITH ", "ALICE SMITH"):
            for decision in ("approve", "reject", "changes_requested"):
                self.refused_review(name, decision=decision,
                                    needle=f"declared interest in agency {SHERIFF}; receipt refused")
        self.refused_review("Carol Diaz", "legal", needle=f"declared interest in agency {POLICE}; receipt")
        with self.assertRaises(ValueError) as caught:  # the signing payload is refused the same way
            findings.review_payload(self.root, self.doc, "factual", "approve", "Alice Smith", "Checked.")
        self.assertIn("receipt refused", str(caught.exception))
        with self.assertRaises(ValueError) as caught:  # a signed receipt is refused before the file is even read
            findings.add_receipt(self.root, self.doc, "factual", "approve", "Alice Smith", "Checked.",
                                 signature=self.base / "never-read.sig")
        self.assertIn("declared interest", str(caught.exception))
        self.assertEqual(self.tree(), before)
        self.assertFalse(os.path.lexists(self.root / "private" / "reviews" / "syn-pub-1.json"))
        self.approve("factual", "Bob Jones")  # prefix-like and unrelated ids never match
        self.approve("factual", "dan LEE")
        result = self.cli("finding", "review", self.draft, "--directory", self.root, "--role", "factual",
                          "--decision", "approve", "--reviewer", "Alice Smith", "--rationale", "Checked.", returncode=1)
        self.assertIn(f"has declared interest in agency {SHERIFF}; receipt refused", result.stderr)
        self.assertNotIn("SENTINEL", result.stdout + result.stderr)

    def test_helper_is_exact_ordered_and_name_free(self):
        entry = {"name": "Alice Smith", "interest_agencies": [SHERIFF, POLICE, "ca-cedar"]}
        self.assertEqual(declared_interest_agencies(self.doc, entry), [POLICE, SHERIFF])  # finding order, no folding
        self.assertEqual(declared_interest_agencies(self.doc, {"interest_agencies": [POLICE.upper()]}), [])
        for entry in (None, {}, {"interest_agencies": []}, {"interest_agencies": "ca"}, "x"):
            self.assertEqual(declared_interest_agencies(self.doc, entry), [])
        self.assertEqual(declared_interest_agencies({"id": "x"}, {"interest_agencies": [POLICE]}), [])

    # earlier receipts -----------------------------------------------------------------------------------------------
    def test_earlier_receipts_reclassified_at_every_gate(self):
        for name, roles in (("Alice Smith", ("factual", "privacy")), ("Bob Jones", ("legal",)),
                            ("Carol Diaz", ("factual", "privacy"))):
            self.add(name, roles)
        self.approve_three_roles()
        self.assertEqual(self.blockers(), [])
        self.assertNotIn("declared_interest", self.status())
        self.assertEqual(findings.status_rows(self.root, [self.draft])[0]["state"], "ready")
        self.add("Alice Smith", ["factual"], POLICE, SHERIFF, note=NOTE)  # declared after the receipts exist
        report = self.status()
        self.assertEqual(report["review_blockers"], ["missing_factual_review", "missing_privacy_review",
                                                      "need_two_independent_reviewers"])
        self.assertEqual([(r["reviewer"], r["role"], r["decision"], r["agency_ids"]) for r in report["declared_interest"]],
                         [("Alice Smith", "factual", "approve", [POLICE, SHERIFF]),
                          ("Alice Smith", "privacy", "approve", [POLICE, SHERIFF])])
        self.assertEqual(set(report["declared_interest"][0]), {"reviewer", "role", "decision", "reviewed_at", "agency_ids"})
        self.assertNotIn("SENTINEL", json.dumps(report))
        self.assertEqual((report["receipts_for_this_content"], report["self_asserted"]), (3, 3))  # provenance only
        self.assertEqual(findings.status_rows(self.root, [self.draft])[0]["state"], "in_review")
        with self.assertRaises(ValueError) as caught:  # publish
            findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        self.assertIn("need_two_independent_reviewers", str(caught.exception))
        self.assertFalse((self.content / "findings" / "synthetic-title.json").exists())
        self.approve("factual", "Carol Diaz")  # a third independent reviewer restores eligibility
        self.approve("privacy", "Carol Diaz")
        self.assertEqual(self.blockers(), [])
        self.assertEqual(len(self.status()["declared_interest"]), 2)  # still listed: it is a classification
        self.add("Alice Smith", ["factual"], clear_interests=True)  # clearing reclassifies the other way too
        self.assertNotIn("declared_interest", self.status())
        self.add("Alice Smith", ["factual"], SHERIFF)
        findings.publish(self.root, self.draft, "Pat Organizer", now=NOW)
        # correct --from: a conflicted approval on the new content does not count either
        corrected = dict(self.doc, summary="Corrected summary.")
        path = self.base / "corrected.json"
        path.write_text(json.dumps(corrected), encoding="utf-8")
        self.add("Alice Smith", ["factual"], clear_interests=True)
        self.approve("factual", "Alice Smith", document=corrected)
        self.approve("privacy", "Alice Smith", document=corrected)
        self.approve("legal", "Bob Jones", document=corrected)
        self.add("Alice Smith", ["factual"], SHERIFF)  # declared after her approvals of the new content
        before = (self.content / "findings" / "synthetic-title.json").read_bytes()
        with self.assertRaises(ValueError) as caught:
            findings.correct(self.root, "syn-pub-1", "2026-03-06", "Fixed.", path, now=LATER, approved_by="Pat Organizer")
        self.assertIn("need_two_independent_reviewers", str(caught.exception))
        self.assertEqual((self.content / "findings" / "synthetic-title.json").read_bytes(), before)
        for role in ("factual", "privacy"):
            self.approve(role, "Carol Diaz", document=corrected)
        self.assertEqual(findings.correct(self.root, "syn-pub-1", "2026-03-06", "Fixed.", path, now=LATER,
                                          approved_by="Pat Organizer")["corrections"], 1)

    def test_challenges_binding_and_trust_are_not_laundered(self):
        for name in ("Alice Smith", "Bob Jones", "Carol Diaz"):
            self.add(name)
        self.approve("factual", "Alice Smith")
        self.receipt("legal", "Alice Smith", decision="reject")
        self.approve("privacy", "Bob Jones")
        self.add("Alice Smith", ["factual"], POLICE)
        report = self.status()
        self.assertIn("unresolved_challenge", report["review_blockers"])  # a conflicted challenge still blocks
        self.assertEqual([(r["role"], r["decision"]) for r in report["declared_interest"]],
                         [("factual", "approve"), ("legal", "reject")])
        # foreign / unbound approvals never count, whoever they are; the conflict only adds a classification
        receipts_path = self.root / "private" / "reviews" / "syn-pub-1.json"
        receipts = json.loads(receipts_path.read_text(encoding="utf-8"))
        receipts[2]["campaign_id"] = "cmp_" + "0" * 32
        del receipts[0]["campaign_id"]
        receipts_path.write_text(json.dumps(receipts), encoding="utf-8")
        report = self.status()
        self.assertTrue({"foreign_campaign_receipt", "unbound_legacy_receipt"} <= set(report["review_blockers"]))
        self.assertEqual(len(report["declared_interest"]), 2)
        # stale receipts (content edited) are still listed, and the receipt bytes are never rewritten
        stale = dict(self.doc, summary="Edited after review.")
        before = receipts_path.read_bytes()
        report = findings.review_status(self.root, stale)
        self.assertEqual((report["receipts_for_this_content"], len(report["declared_interest"])), (0, 2))
        self.assertEqual(receipts_path.read_bytes(), before)

    def test_pure_blockers_exclude_conflicted_approvals_not_challenges(self):
        finding = dict(self.doc, **{"sources": [{"locator": "p. 1", "sha256": "a" * 64}], "limitations": "x",
                                    "counterevidence": "y"})
        digest = findings.content_hash(finding)
        receipts = [{"content_sha256": digest, "role": role, "decision": "approve", "reviewer": who,
                     "rationale": "ok", "reviewed_at": "2026-01-01T00:00:00Z", "campaign_id": "cmp_x"}
                    for role, who in (("factual", "Alice Smith"), ("legal", "Bob Jones"), ("privacy", "Bob Jones"))]
        roster = {"schema_version": 1, "reviewers": [{"name": "alice smith", "roles": ["factual"],
                                                      "added_on": "2026-01-01", "interest_agencies": [SHERIFF]}]}
        self.assertEqual(review_blockers(finding, receipts, "cmp_x"), [])
        self.assertEqual(review_blockers(finding, receipts, "cmp_x", roster),
                         ["missing_factual_review", "need_two_independent_reviewers"])
        self.assertEqual(review_blockers(finding, receipts, "cmp_x", {"schema_version": 1, "reviewers": []}), [])
        challenge = dict(receipts[0], decision="changes_requested")
        self.assertIn("unresolved_challenge", review_blockers(finding, receipts + [challenge], "cmp_x", roster))

    @unittest.skipUnless(shutil.which("ssh-keygen"), "ssh-keygen (OpenSSH 8.2+) is required")
    def test_signed_conflicted_approval_has_trust_but_not_eligibility(self):
        signer = Signer("alice")
        self.addCleanup(signer.close)
        self.add("Alice Smith", ["factual"], ssh_key=signer.dir / "key.pub")
        self.add("Bob Jones", ["legal", "privacy"])
        payload = findings.review_payload(self.root, self.doc, "factual", "approve", "Alice Smith", "Checked.")
        signature = self.base / "review.sig"
        signature.write_text(signer.sign(payload, findings.SIGN_NAMESPACE), encoding="utf-8")
        findings.add_receipt(self.root, self.doc, "factual", "approve", "Alice Smith", "Checked.", signature=signature)
        self.approve("legal", "Bob Jones")
        self.approve("privacy", "Bob Jones")
        self.assertEqual(self.blockers(), [])
        self.add("Alice Smith", ["factual"], SHERIFF)
        report = self.status()
        self.assertEqual((report["signed"], report["bad_signatures"]), (1, []))
        self.assertEqual(report["review_blockers"], ["missing_factual_review", "need_two_independent_reviewers"])
        self.assertEqual(len(report["declared_interest"]), 1)

    # privacy ------------------------------------------------------------------------------------------------------
    def test_private_note_never_leaks(self):
        self.add("Sentinel Reviewer-55", ["factual"], POLICE, note=NOTE)
        self.add("Bob Jones", ["legal", "privacy"])
        self.approve("legal", "Bob Jones")
        outputs = [self.cli("finding", "review-status", self.draft, "--directory", self.root).stdout,
                   self.cli("finding", "reviewers", "list", "--directory", self.root).stdout,
                   self.cli("finding", "review", self.draft, "--directory", self.root, "--role", "factual",
                            "--decision", "approve", "--reviewer", "Sentinel Reviewer-55", "--rationale", "x",
                            returncode=1).stderr]
        self.assertTrue(all("SENTINEL-NOTE" not in text for text in outputs))
        # malformed rosters refuse without echoing the note
        document = json.loads(self.roster.read_text(encoding="utf-8"))
        document["reviewers"][0]["interest_note"] = NOTE + "\n" * 3
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        for command in (("finding", "reviewers", "list"), ("finding", "review-status", self.draft)):
            result = self.cli(*command, "--directory", self.root, returncode=1)
            self.assertNotIn("SENTINEL", result.stderr + result.stdout)
        document["reviewers"][0]["interest_note"] = NOTE
        # symlinked roster, oversize roster, failed replace
        self.roster.write_text(json.dumps(document), encoding="utf-8")
        good = self.roster.read_bytes()
        self.roster.write_bytes(good + b" " * findings.MAX_ROSTER_BYTES)
        with self.assertRaises(ValueError):
            self.add("Bob Jones", ["legal"], POLICE)
        self.roster.write_bytes(good)
        target = self.base / "elsewhere.json"
        target.write_bytes(good)
        self.roster.unlink()
        self.roster.symlink_to(target)
        with self.assertRaises(ValueError) as caught:
            self.add("Bob Jones", ["legal"], POLICE)
        self.assertIn("symlink", str(caught.exception))
        self.roster.unlink()
        self.roster.write_bytes(good)
        before = self.tree()
        with mock.patch("os.replace", side_effect=OSError("disk gone")), self.assertRaises(OSError):
            self.add("Bob Jones", ["legal"], POLICE, note="SENTINEL-OTHER")
        self.assertEqual(self.tree(), before)

    def test_nothing_about_interests_is_published(self):
        example = REPO_ROOT / "examples" / "fictional-campaign"

        def build(with_roster):
            root = self.base / ("with" if with_roster else "without")
            shutil.copytree(example, root, ignore=shutil.ignore_patterns("public"))
            if with_roster:
                private = root / "private"
                private.mkdir(exist_ok=True)
                (private / "reviewers.json").write_text(json.dumps({"schema_version": 1, "reviewers": [{
                    "name": "Sentinel Reviewer-55", "roles": ["factual"], "added_on": "2026-01-01",
                    "interest_agencies": [POLICE, SHERIFF], "interest_note": NOTE}]}), encoding="utf-8")
            with mock.patch("sys.stderr"):
                build_site(root)
            return root / "public"
        with_roster, without = build(True), build(False)
        files = sorted(p.relative_to(without) for p in without.rglob("*") if p.is_file())
        self.assertEqual(files, sorted(p.relative_to(with_roster) for p in with_roster.rglob("*") if p.is_file()))
        for relative in files:
            self.assertEqual((with_roster / relative).read_bytes(), (without / relative).read_bytes(), relative)
            self.assertNotIn(b"Sentinel Reviewer", (with_roster / relative).read_bytes())
        methods = (with_roster / "methods.html").read_text(encoding="utf-8")
        self.assertIn("Reviewers declare agency interests privately. A declared interest excludes their review from "
                      "findings about that agency.", methods)

    def test_legacy_status_shape_and_no_automatic_rebuild(self):
        self.add("Alice Smith")
        self.add("Bob Jones", ["legal", "privacy"])
        self.approve("factual", "Alice Smith")
        keys = set(self.status())
        self.assertNotIn("declared_interest", keys)
        self.assertEqual(keys, {"id", "content_sha256", "receipts_for_this_content", "stale_receipts", "campaign_id",
                                "unbound_receipts", "foreign_campaign_receipts", "signed", "self_asserted",
                                "bad_signatures", "review_blockers", "unrostered_receipts"})
        receipts_path = self.root / "private" / "reviews" / "syn-pub-1.json"
        before = (receipts_path.read_bytes(), sorted(p.name for p in self.content.iterdir()))
        self.add("Alice Smith", ["factual"], POLICE)  # declaring changes no receipt, hash or public content
        self.assertEqual((receipts_path.read_bytes(), sorted(p.name for p in self.content.iterdir())), before)
        self.assertEqual(findings.reviewed_hash(self.doc), self.status()["content_sha256"])


if __name__ == "__main__":
    unittest.main()
