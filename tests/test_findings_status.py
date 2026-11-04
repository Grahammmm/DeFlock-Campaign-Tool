"""DF2237-05: `finding status --directory D [--draft PATH ...] [--json]`. Synthetic campaign only.

Run from the repository root: python3 -B -m unittest tests.test_findings_status -v
"""
import hashlib
import json
import os
import stat
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import findings  # noqa: E402
from tests import test_findings_publish as publish_tests  # noqa: E402  (module import: not re-collected here)
from tests.test_findings_withdraw import LATER  # noqa: E402

NOW, draft = publish_tests.NOW, publish_tests.draft
NOTE = "Synthetic correction note: page two was misdated."
REASON = "Synthetic withdrawal reason: wrong agency cited."
RECEIPT_ONLY = ["missing_factual_review", "missing_legal_review", "missing_privacy_review",
                "need_two_independent_reviewers"]
EXPECTED = {  # id -> (state, blockers)
    "st-draft": ("draft", RECEIPT_ONLY),
    "st-early": ("draft", sorted(RECEIPT_ONLY + ["missing_summary"])),  # structural, but no receipt yet
    "st-review": ("in_review", RECEIPT_ONLY[1:]),
    "st-challenge": ("blocked", sorted(RECEIPT_ONLY[1:] + ["unresolved_challenge"])),
    "st-struct": ("blocked", sorted(RECEIPT_ONLY[1:] + ["missing_summary"])),
    "st-ready": ("ready", []),
    "st-stale": ("draft", RECEIPT_ONLY),
    "st-pub": ("published", []),
    "st-fix": ("corrected", []),
    "st-gone": ("withdrawn", []),
}
KEYS = {"id", "slug", "state", "receipts_current", "receipts_stale", "blockers", "published_at", "updated_at",
        "corrections"}


def snapshot(root):
    """(size, mtime_ns, sha256) of every path under ``root``, the root itself included; symlinks not followed."""
    result = {}
    for folder, dirs, files in os.walk(root):
        for path in [folder] + [os.path.join(folder, name) for name in dirs + files]:
            info = os.lstat(path)
            digest = hashlib.sha256(Path(path).read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            result[os.path.relpath(path, root)] = (info.st_size, info.st_mtime_ns, digest)
    return result


class FindingStatusTests(unittest.TestCase):
    _p = vars(publish_tests.FindingPublishTests)
    setUp_campaign, cli, register, write_draft, receipt, approve_all = (
        _p["setUp"], _p["cli"], _p["register"], _p["write_draft"], _p["receipt"], _p["approve_all"])

    def setUp(self):
        self.setUp_campaign()
        self.drafts = self.root / "private" / "drafts"
        self.drafts.mkdir(mode=0o700)
        self.put("st-draft")
        self.put("st-early", summary="")
        self.receipt("factual", "Rev One", document=self.put("st-review"))
        challenged = self.put("st-challenge")
        self.receipt("factual", "Rev One", document=challenged)
        self.receipt("legal", "Rev Two", "reject", document=challenged)
        self.receipt("factual", "Rev One", document=self.put("st-struct", summary=""))
        self.approve_all(self.put("st-ready"))
        self.receipt("factual", "Rev One", document=self.put("st-stale"))
        self.put("st-stale", summary="Edited synthetic summary.")  # the receipt is now stale
        for name in ("st-pub", "st-fix", "st-gone"):
            self.approve_all(self.put(name, folder=self.base))
            findings.publish(self.root, self.base / f"{name}.json", "Pat Organizer", now=NOW)
        findings.correct(self.root, "st-fix", "2026-03-05", NOTE, now=LATER, approved_by="Pat Organizer")
        self.gone_bytes = (self.content / "findings" / "st-gone.json").read_bytes()
        findings.withdraw(self.root, "st-gone", REASON, now=LATER)

    def put(self, name, folder=None, **changes):
        document = draft(id=name, slug=name, **changes)
        (folder or self.drafts).joinpath(f"{name}.json").write_text(json.dumps(document), encoding="utf-8")
        return document

    def status(self, *arguments):
        return self.cli("finding", "status", "--directory", self.root, *arguments).stdout

    def rows(self, *arguments):
        return json.loads(self.status("--json", *arguments))

    def test_one_row_per_state_with_blocker_names(self):
        rows = self.rows()
        self.assertEqual([r["id"] for r in rows], sorted(EXPECTED, key=lambda i: (
            findings.STATUS_ORDER.index(EXPECTED[i][0]), i)))  # state order, withdrawn last, then id
        self.assertEqual({r["id"]: (r["state"], r["blockers"]) for r in rows}, EXPECTED)
        self.assertTrue(all(set(r) == KEYS for r in rows), rows)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual((by_id["st-ready"]["receipts_current"], by_id["st-pub"]["receipts_current"]), (3, 3))
        self.assertEqual((by_id["st-draft"]["receipts_current"], by_id["st-draft"]["receipts_stale"]), (0, 0))
        self.assertEqual(by_id["st-fix"]["corrections"], 1)
        self.assertEqual(by_id["st-fix"]["updated_at"], "2026-03-10T12:00:00Z")
        self.assertEqual(by_id["st-pub"]["published_at"], "2026-03-04T05:06:07Z")
        self.assertEqual((by_id["st-pub"]["corrections"], by_id["st-pub"]["updated_at"]), (0, None))
        self.assertEqual(rows, findings.status_rows(self.root))  # the CLI prints exactly the library rows

    def test_table_columns_order_and_blockers(self):
        lines = self.status().splitlines()
        self.assertEqual(lines[0].split(), ["ID", "SLUG", "STATE", "RECEIPTS", "BLOCKERS"])
        self.assertEqual([line.split()[0] for line in lines[1:]], [r["id"] for r in self.rows()])
        cells = {line.split()[0]: line.split() for line in lines[1:]}
        self.assertEqual(cells["st-ready"][2:], ["ready", "3", "-"])
        self.assertEqual(cells["st-challenge"][2:], ["blocked", "2", ",".join(EXPECTED["st-challenge"][1])])
        self.assertEqual(cells["st-stale"][2:], ["draft", "0", "(+1", "stale)", ",".join(RECEIPT_ONLY)])
        self.assertEqual(lines[-1].split()[:3], ["st-gone", "st-gone", "withdrawn"])

    def test_stale_receipts_only_is_draft(self):
        row = next(r for r in self.rows() if r["id"] == "st-stale")
        self.assertEqual((row["state"], row["receipts_current"], row["receipts_stale"]), ("draft", 0, 1))

    def test_published_id_with_newer_draft_is_second_pending_correction_row(self):
        self.put("st-pub", summary="Newer synthetic summary.")
        self.receipt("factual", "Rev One", document=draft(id="st-pub", slug="st-pub", summary="Newer synthetic summary."))
        rows = [r for r in self.rows() if r["id"] == "st-pub"]
        self.assertEqual([r["state"] for r in rows], ["published", "pending correction"])
        self.assertEqual(rows[1]["blockers"], RECEIPT_ONLY[1:])
        self.assertEqual((rows[1]["receipts_current"], rows[1]["receipts_stale"]), (1, 3))
        self.assertEqual(rows[0]["receipts_current"], 3)  # published text keeps its own receipts

    def test_withdrawn_wins_over_leftover_content_file_and_a_draft(self):
        leftover = self.content / "findings" / "st-gone.json"
        leftover.write_bytes(self.gone_bytes)  # as if withdraw could not unlink it
        self.put("st-gone", summary="Rewritten synthetic summary.")
        rows = [r for r in self.rows() if r["id"] == "st-gone"]
        self.assertEqual([r["state"] for r in rows], ["withdrawn", "withdrawn"])
        self.assertEqual([r.get("leftover") for r in rows].count(["content/findings/st-gone.json"]), 1)
        self.assertIn("leftover content/findings/st-gone.json", self.status())

    def test_unreadable_files_are_rows_and_other_rows_unaffected(self):
        baseline = self.rows()
        (self.root / "private" / "reviews" / "st-ready.json").write_text("not json", encoding="utf-8")
        (self.drafts / "broken.json").write_text("{not json", encoding="utf-8")
        (self.drafts / "linked.json").symlink_to(self.base / "st-pub.json")
        (self.content / "findings" / "odd.json").write_text("[1]", encoding="utf-8")
        missing = self.base / "absent.json"
        rows = self.rows("--draft", missing)
        bad = {Path(r["path"]).name: r for r in rows if r["state"] == "unreadable"}
        self.assertEqual(set(bad), {"st-ready.json", "broken.json", "linked.json", "odd.json", "absent.json"})
        self.assertIn("st-ready.json: receipt file is not valid JSON", bad["st-ready.json"]["error"])
        self.assertIn("symlink", bad["linked.json"]["error"])
        self.assertTrue(all(r["id"] is None and r["blockers"] == [] for r in bad.values()))
        self.assertEqual([r for r in rows if r["state"] != "unreadable"],
                         [r for r in baseline if r["id"] != "st-ready"])
        self.assertIn("unreadable " + str(self.drafts / "broken.json"), self.status())  # exit 0 checked by cli()

    def test_unknown_key_text_is_withheld_not_quoted(self):
        note_text = "Private reviewer note: Rev Three doubts page four"
        document = dict(draft(id="st-leak", slug="st-leak"), **{note_text: "Unpublished rationale text."})
        (self.drafts / "st-leak.json").write_text(json.dumps(document), encoding="utf-8")
        baseline = self.rows()
        [bad] = [r for r in baseline if r["state"] == "unreadable"]
        path = str(self.drafts / "st-leak.json")
        self.assertEqual(bad["path"], path)
        self.assertEqual(bad["error"], path + ": draft is not valid (reason withheld because it can quote the file; "
                                              "check the file itself)")
        with self.assertRaisesRegex(ValueError, "unknown key"):
            findings.load_draft(self.drafts / "st-leak.json")  # the withheld reason is real
        for text in (self.status("--json"), self.status()):
            for hidden in (note_text, "Rev Three", "page four", "Unpublished rationale"):
                self.assertNotIn(hidden, text)
        self.assertEqual({r["id"]: (r["state"], r["blockers"]) for r in baseline if r["id"]}, EXPECTED)

    def test_symlinked_drafts_folder_is_one_unreadable_row(self):
        os.rename(self.drafts, self.base / "real-drafts")
        self.drafts.symlink_to(self.base / "real-drafts", target_is_directory=True)
        rows = self.rows()
        [bad] = [r for r in rows if r["state"] == "unreadable"]
        self.assertEqual(bad["path"], str(self.drafts))
        self.assertEqual({r["id"] for r in rows if r["state"] != "unreadable"}, {"st-pub", "st-fix", "st-gone"})

    def test_extra_draft_listed_once_even_when_repeated(self):
        outside = self.base / "outside.json"
        outside.write_text(json.dumps(draft(id="st-outside", slug="st-outside")), encoding="utf-8")
        inside = self.drafts / "st-draft.json"
        rows = self.rows("--draft", outside, "--draft", outside, "--draft", inside)
        self.assertEqual([r["id"] for r in rows].count("st-outside"), 1)
        self.assertEqual([r["id"] for r in rows].count("st-draft"), 1)
        self.assertEqual(next(r for r in rows if r["id"] == "st-outside")["state"], "draft")

    def test_read_only_tree_identical_and_no_private_file_created(self):
        self.put("st-pub", summary="Newer synthetic summary.")  # exercise the pending-correction path too
        (self.drafts / "broken.json").write_text("{not json", encoding="utf-8")
        before = snapshot(self.root)
        self.status()
        self.status("--json", "--draft", self.base / "st-pub.json")
        findings.status_rows(self.root, [self.base / "absent.json"])
        self.assertEqual(snapshot(self.root), before)

    def test_read_only_without_reviews_folder(self):
        os.rename(self.root / "private" / "reviews", self.base / "moved-reviews")
        before = snapshot(self.root)
        self.assertEqual({r["receipts_current"] for r in self.rows()}, {0})
        self.assertEqual(snapshot(self.root), before)  # no private/reviews/ or lock file created

    def test_no_reviewer_names_rationale_or_note_text_in_output(self):
        self.put("st-pub", summary="Newer synthetic summary.")
        for text in (self.status("--json"), self.status()):
            for hidden in ("Rev One", "Rev Two", "Checked synthetic pages.", NOTE, REASON, "Pat Organizer",
                           "Ada", "misdated", "wrong agency"):
                self.assertNotIn(hidden, text)


if __name__ == "__main__":
    unittest.main()
