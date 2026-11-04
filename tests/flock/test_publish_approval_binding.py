"""DF-APPROVAL-TIME-BIND: a PUBLISH.json approval binds the content as it stood when the person approved it.

The approval records `review.content_hash` (sha256 of the entry minus `review`) at approval time; the publication gate
compares that recorded hash with the entry the run re-derives now. A synchronized edit of the approved entry and of the
run-derived text therefore no longer carries the old approval forward. Legacy approvals (no recorded hash) approve
nothing until a person re-binds them. Synthetic fixtures only. Run from the repository root:
python3 -B -m unittest tests.flock.test_publish_approval_binding -v
"""
import csv, json, os, subprocess, sys, unittest
from campaign_tool import publication_gate as gate
from tests.flock import test_publish as base
from tests.flock import test_publish_brief as brief_tests
from tests.flock import test_publish_stage as stage_tests

LEGACY = {"status": "approved", "approved_by": "G", "approved_on": "2026-10-30", "approval_ref": "G-1"}
EDIT = " Synthetic wording added after approval."


def digest(entry):
    return gate.content_digest({k: v for k, v in entry.items() if k != "review"})


def bound(entry):  # what the person records when approving: the hash of the entry as it stands at that moment
    return dict(LEGACY, content_hash=digest(entry))


def edit_run_and_entry(run_dir, entry):
    """The synchronized edit: the run's FINDINGS.csv claim and the PUBLISH.json text change together; review is kept."""
    path = run_dir / "FINDINGS.csv"
    with open(path, newline="") as handle:
        table = list(csv.reader(handle))
    row = next(r for r in table if r[0] == entry["finding_id"])
    row[table[0].index("claim")] += EDIT
    with open(path, "w", newline="") as handle:
        csv.writer(handle, lineterminator="\n").writerows(table)
    fresh = stage_tests.load_stage().publish.derive(run_dir, "sample")[2]
    entry["text"] = next(f for f in fresh["findings"] if f["finding_id"] == entry["finding_id"])["text"]
    assert EDIT.strip() in entry["text"]


class Harness(unittest.TestCase):
    def inner(self, cls, method):
        case = cls(method)
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case


class StageBinding(Harness):
    def setUp(self):
        self.case = self.inner(stage_tests.PublishStageTest, "test_one_approved_entry_staged_pending_skipped")
        self.n1 = self.case.doc["findings"][0]

    def save(self):
        self.case.out.write_text(json.dumps(self.case.doc))

    def test_positive_control_synchronized_edit_of_a_bound_approval_is_refused(self):
        self.n1["review"] = bound(self.n1)
        edit_run_and_entry(self.case.run_dir, self.n1)
        self.save()
        self.case.assert_refused(self.case.stage(), "publication refused: artefact N1: content_changed_after_approval")

    def test_positive_control_synchronized_edit_of_a_legacy_approval_is_refused(self):
        self.n1["review"] = dict(LEGACY)
        edit_run_and_entry(self.case.run_dir, self.n1)
        self.save()
        self.case.assert_refused(self.case.stage(), "publication refused: artefact N1: approval_unbound")

    def test_negative_control_unchanged_bound_approval_is_staged(self):
        self.n1["review"] = bound(self.n1)
        self.save()
        self.assertEqual(self.case.stage(), (0, "staged N1 -> content/findings/search-purpose.json\n" + stage_tests.FINAL % (1, 1, 3)))

    def test_legacy_policy_unbound_approval_refused_until_a_person_rebinds_it(self):
        self.n1["review"] = dict(LEGACY)
        self.save()
        self.case.assert_refused(self.case.stage(), "publication refused: artefact N1: approval_unbound")
        self.n1["review"]["content_hash"] = ""  # the export's empty slot is not a binding either
        self.save()
        self.case.assert_refused(self.case.stage(), "publication refused: artefact N1: approval_unbound")
        self.n1["review"]["content_hash"] = digest(self.n1)  # the person re-reads this exact text and records its hash
        self.save()
        self.assertEqual(self.case.stage()[0], 0)

    def test_a_hash_bound_to_other_content_is_refused(self):
        other = self.case.doc["findings"][1]
        self.n1["review"] = dict(LEGACY, content_hash=digest(other))
        self.save()
        self.case.assert_refused(self.case.stage(), "publication refused: artefact N1: content_changed_after_approval")

    def test_check_refuses_a_malformed_recorded_hash(self):
        for bad in ("A" * 64, "a" * 63, "not a hash", 7):
            with self.subTest(bad=bad):
                self.n1["review"] = dict(LEGACY, content_hash=bad)
                self.save()
                code, out = base.run("flock_publish.py", "--check", str(self.case.out))
                self.assertEqual(code, 1, out)
                self.assertIn("finding N1: review keys or values differ from the schema\n", out)


class BriefBinding(Harness):
    def setUp(self):
        self.case = self.inner(brief_tests.PublishBriefTest, "test_two_runs_three_approved_one_pending")
        self.n1 = self.case.entry(brief_tests.A, "N1")

    def brief(self):
        done = subprocess.run([sys.executable, "-B", str(base.TOOLS / "flock_publish.py"), "brief", str(self.case.store),
                               "--agency", base.AGENCY, "--out", str(self.case.brief_md)], capture_output=True, text=True, check=False)
        self.case.hygiene(done.stdout + done.stderr)
        return done

    def test_positive_control_synchronized_edit_of_a_bound_approval_is_refused(self):
        self.n1["review"] = bound(self.n1)
        edit_run_and_entry(self.case.run_dir, self.n1)
        self.case.save(brief_tests.A)
        done = self.brief()
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("publication refused: artefact N1: content_changed_after_approval", done.stderr)
        self.assertFalse(os.path.lexists(self.case.brief_md))

    def test_positive_control_synchronized_edit_of_a_legacy_approval_is_refused(self):
        self.n1["review"] = dict(LEGACY)
        edit_run_and_entry(self.case.run_dir, self.n1)
        self.case.save(brief_tests.A)
        done = self.brief()
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertIn("publication refused: artefact N1: approval_unbound", done.stderr)
        self.assertFalse(os.path.lexists(self.case.brief_md))

    def test_negative_control_unchanged_bound_approval_is_briefed(self):
        self.n1["review"] = bound(self.n1)
        self.case.save(brief_tests.A)
        done = self.brief()
        self.assertEqual((done.returncode, done.stdout), (0, brief_tests.summary(2, 1, 0)), done.stderr)
        self.assertIn(self.n1["text"], self.case.brief_md.read_text(encoding="utf-8"))


class QueueBinding(Harness):
    """The approval-recording surface: the queue shows the hash to record, and lists unbound or stale approvals again."""

    def setUp(self):
        from tests.flock import test_review_queue as rq
        self.rq, self.case = rq, self.inner(rq.ReviewQueueTest, "test_pending_order_stdout_and_immutable_inputs")

    def test_legacy_and_stale_approvals_are_listed_for_rebinding_bound_ones_are_counted(self):
        n1, o1, a1 = (next(f for f in self.case.doc["findings"] if f["finding_id"] == i) for i in ("N1", "O1", "A1"))
        n1["review"], o1["review"] = dict(LEGACY), bound(o1)
        a1["review"] = dict(LEGACY, content_hash=digest(o1))  # recorded for other content
        self.case.save()
        code, out = self.case.show()
        self.assertEqual(code, 1, out)  # a warning fails the queue run, as before
        text = self.case.report()
        self.assertIn("queue | %s | 2026-10-30 | N1 | approved | verdict=confirm | warnings=1" % base.AGENCY, out)
        self.assertIn("queue | %s | 2026-10-30 | A1 | approved | verdict=confirm | warnings=1" % base.AGENCY, out)
        self.assertNotIn("| O1 |", out)
        self.assertIn("WARNING: approval has no recorded content_hash (legacy): it approves nothing until re-bound", text)
        self.assertIn("WARNING: approval was recorded for different content: it approves nothing", text)
        self.assertIn("content_hash: " + digest(n1), text)
        self.assertIn("Approved entries (counted only): 1", text)


if __name__ == "__main__":
    unittest.main()
