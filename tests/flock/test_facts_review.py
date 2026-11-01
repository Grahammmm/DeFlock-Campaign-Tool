"""flock_facts review: a second person sets checked or disputed on listed fact ids; logged, all or nothing (DF1537-01)."""
import contextlib
import csv
import datetime
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from tests.flock.test_facts import ROOT, TOOL, snapshot

_spec = importlib.util.spec_from_file_location("flock_facts_review_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)

DOC = hashlib.sha256(b"synthetic review doc").hexdigest()
TODAY = datetime.date.today()
ADDED = (TODAY - datetime.timedelta(days=30)).isoformat()
DAY = (TODAY - datetime.timedelta(days=10)).isoformat()
NOTE = "Brindlemoss note text"
SECRETS = ["Quillwort", "REQ-SECRET-7", DOC, NOTE, "Brindlemoss"]
HEAD = "fact_id,from_status,to_status,reviewer,reviewed_at,note"


def fact(n, added_by="ana", status="unreviewed"):
    row = dict(zip(facts.V1, ["Alpha", "REQ-SECRET-7", "vendor", 'Quillwort, "value" %d' % n, DOC, str(n),
                              'Quillwort quote, "said" %d' % n]))
    row.update(kind="record", status=status, effective_from="", effective_to="", added_by=added_by, added_at=ADDED)
    row["fact_id"] = facts.fact_id(row)
    return row


class FactsReviewTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "store"
        self.rows = [fact(1), fact(2), fact(3, added_by="cara")]
        self.a, self.b, self.c = (r["fact_id"] for r in self.rows)
        self.folder = self.store / "agencies" / "alpha"
        self.facts, self.log = self.folder / "FACTS.csv", self.folder / "FACT-REVIEWS.csv"
        facts.write_atomic(self.facts, self.rows)

    def run_tool(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = facts.main(list(args))
            except SystemExit as exc:
                code = exc.code
        for secret in SECRETS:
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        return code, out.getvalue().splitlines(), err.getvalue().splitlines()

    def review(self, ids, to="checked", reviewer="ben", *extra, store=None):
        return self.run_tool("review", "--store", str(store or self.store), "--agency", "Alpha", "--ids", ",".join(ids),
                             "--set", to, "--reviewer", reviewer, *extra)

    def refused(self, *args, **kw):
        """A refusal: exit 1, named stderr lines, nothing on stdout, the store byte-identical."""
        before = snapshot(self.dir)
        code, out, err = self.review(*args, **kw)
        self.assertEqual((code, out), (1, []))
        self.assertEqual(snapshot(self.dir), before, "a refused review changed the store")
        return err

    def log_lines(self):
        return self.log.read_text(encoding="utf-8").splitlines()

    def check(self):
        code, out, _ = self.run_tool("--check", str(self.store))
        return code, [line for line in out if line]

    def test_two_facts_checked_by_second_reviewer_rest_byte_identical(self):
        before = self.facts.read_bytes().decode().split("\n")
        code, out, err = self.review([self.a, self.b], "checked", "ben", "--date", DAY, "--note", NOTE)
        self.assertEqual((code, err), (0, []))
        self.assertEqual(out, ["review | alpha | %s | unreviewed -> checked" % self.a,
                               "review | alpha | %s | unreviewed -> checked" % self.b, "reviewed=2 changed=2 unchanged=0"])
        self.assertEqual(self.log_lines(), [HEAD] + ["%s,unreviewed,checked,ben,%s,%s" % (i, DAY, NOTE) for i in (self.a, self.b)])
        self.assertEqual(stat.S_IMODE(self.log.stat().st_mode), 0o600)
        after = self.facts.read_bytes().decode().split("\n")
        self.assertEqual(len(after), len(before))
        for old, new in zip(before, after):  # header, quoting, column order and fact c unchanged; only a and b's status
            changed = old.startswith('Alpha,') and (self.a in old or self.b in old)
            self.assertEqual(new, old[:-len("unreviewed")] + "checked" if changed else old)
        self.assertIn('"Quillwort, ""value"" 1"', after[1])
        self.assertEqual(self.check(), (0, []))
        self.assertEqual([p.name for p in self.folder.iterdir() if p.name.startswith(".")], [])

    def test_reviewer_equal_to_added_by_refused_for_whole_call(self):
        for who in ("Ana ", " ANA", "ana"):
            with self.subTest(who=who):
                err = self.refused([self.c, self.a, self.b], "checked", who)
                self.assertEqual(err, ["%s: reviewer is the person who added it" % i for i in (self.a, self.b)])
        self.assertFalse(self.log.exists())
        self.assertEqual(self.refused([self.c], "checked", " Cara"), ["%s: reviewer is the person who added it" % self.c])
        self.assertEqual(self.refused([self.c], "checked", "  "), ["--reviewer: is empty"])

    def test_unknown_duplicate_and_too_many_ids_refused(self):
        unknown = "F-0123456789ab"
        self.assertEqual(self.refused([self.a, unknown, self.c]), ["--ids: unknown fact id %s" % unknown])
        self.assertEqual(self.refused([self.a, "Quillwort value", ""]),  # free text is never echoed back
                         ["--ids: unknown fact id at position 2", "--ids: unknown fact id at position 3"])
        self.assertEqual(self.refused([self.a, self.b, self.a]), ["--ids: duplicate id %s" % self.a])
        self.assertEqual(self.refused([self.a] + ["F-%012x" % n for n in range(200)])[0], "--ids: more than 200 ids")

    def test_already_at_target_is_unchanged_and_not_logged(self):
        self.assertEqual(self.review([self.a])[0], 0)
        before = snapshot(self.dir)
        self.assertEqual(self.review([self.a], "checked", "dan"),
                         (0, ["review | alpha | %s | unchanged" % self.a, "reviewed=1 changed=0 unchanged=1"], []))
        self.assertEqual(snapshot(self.dir), before, "an all-unchanged review wrote")
        code, out, _ = self.review([self.a, self.b], "checked", "dan")
        self.assertEqual((code, out[0], out[2]), (0, "review | alpha | %s | unchanged" % self.a, "reviewed=2 changed=1 unchanged=1"))
        self.assertEqual(len(self.log_lines()), 3)

    def test_checked_to_disputed_logged_then_hand_edit_named_by_check(self):
        self.assertEqual(self.review([self.a], "checked", "ben", "--date", DAY)[0], 0)
        code, out, _ = self.review([self.a], "disputed", "cara")
        self.assertEqual((code, out[0]), (0, "review | alpha | %s | checked -> disputed" % self.a))
        self.assertEqual(self.log_lines()[2], "%s,checked,disputed,cara,%s," % (self.a, TODAY.isoformat()))
        self.assertEqual(self.check(), (0, []))
        text = self.facts.read_text(encoding="utf-8")
        self.facts.write_text(text.replace(self.a + ",record,,,ana,%s,disputed" % ADDED, self.a + ",record,,,ana,%s,checked" % ADDED),
                              encoding="utf-8")
        mismatch = "file 1: reviews: %s: FACTS status checked does not match the review log (disputed)" % self.a
        self.assertEqual(self.check(), (1, [mismatch]))
        self.assertEqual(self.refused([self.b]), [mismatch])

    def test_check_names_bad_log_rows(self):
        self.log.write_text("\n".join([HEAD, "F-0123456789ab,unreviewed,checked,ben,%s," % DAY,
                                       "%s,unreviewed,checked,ANA,%s," % (self.a, DAY), "%s,unreviewed,disputed,ben,bad," % self.a,
                                       "%s,checked,checked,ben,%s," % (self.b, DAY), "%s,unreviewed,checked" % self.c, ""]),
                            encoding="utf-8")
        mismatch = "file 1: reviews: %s: FACTS status unreviewed does not match the review log (%s)"
        self.assertEqual(self.check(), (1, [
            "file 1: reviews row 2: fact_id is not in FACTS.csv", "file 1: reviews row 3: reviewer is invalid",
            "file 1: reviews row 4: from_status is invalid", "file 1: reviews row 4: reviewed_at is invalid",
            "file 1: reviews row 5: to_status is invalid", "file 1: reviews row 6: columns have the wrong count",
            mismatch % (self.a, "disputed"), mismatch % (self.b, "checked")]))
        self.log.write_text("fact_id,to_status\n", encoding="utf-8")
        self.assertEqual(self.check(), (1, ["file 1: reviews row 1: header is not FACT-REVIEWS.csv v1"]))

    def test_bad_dates_refused_and_unreviewed_is_usage(self):
        future = (TODAY + datetime.timedelta(days=1)).isoformat()
        before_added = (TODAY - datetime.timedelta(days=31)).isoformat()
        self.assertEqual(self.refused([self.a], "checked", "ben", "--date", future), ["--date: is after today"])
        self.assertEqual(self.refused([self.a, self.c], "checked", "ben", "--date", before_added),
                         ["%s: --date is before added_at" % i for i in (self.a, self.c)])
        for bad in ("2026-1-05", "yesterday", "2026-02-30", ""):
            self.assertEqual(self.refused([self.a], "checked", "ben", "--date", bad), ["--date: is not YYYY-MM-DD"])
        self.assertEqual(self.refused([self.a], "checked", "ben", "--note", "x" * 121),
                         ["--note: longer than 120 characters or has a line break"])
        self.assertEqual(self.refused([self.a], "checked", "ben", "--note", NOTE + "\nmore")[0][:7], "--note:")
        before = snapshot(self.dir)
        for args in (["--set", "unreviewed"], ["--set", "checked"], ["--set", "checked", "--reviewer", "ben", "--added-by", "x"]):
            code, out, _ = self.run_tool("review", "--store", str(self.store), "--agency", "alpha", "--ids", self.a, *args)
            self.assertEqual((code, out), (2, []))
        code, _, _ = self.run_tool("status", "--store", str(self.store), "--reviewer", "ben")
        self.assertEqual(code, 2)
        self.assertEqual(snapshot(self.dir), before)

    def test_symlinks_and_store_inside_repo_refused(self):
        self.assertEqual(self.refused([self.a], store=ROOT / "no-such-review-store"), ["store inside repository"])
        self.assertFalse((ROOT / "no-such-review-store").exists())
        real = self.dir / "real.csv"
        real.write_text(HEAD + "\n", encoding="utf-8")
        self.log.symlink_to(real)
        self.assertEqual(self.refused([self.a]), ["file 1: FACT-REVIEWS.csv: is a symlink or not a file; refusing"])
        self.log.unlink()
        other = self.dir / "other"
        self.folder.rename(other)
        self.folder.symlink_to(other, target_is_directory=True)
        self.assertEqual(self.refused([self.a]), ["agencies/alpha: is a symlink; refusing"])
        self.folder.unlink()
        other.rename(self.folder)
        moved = self.dir / "FACTS-real.csv"
        self.facts.rename(moved)
        self.facts.symlink_to(moved)
        self.assertEqual(self.refused([self.a]), ["agencies/alpha/FACTS.csv: is a symlink; refusing"])

    def test_crash_mid_write_leaves_both_files_as_before(self):
        self.assertEqual(self.review([self.a])[0], 0)
        for fail_on, log_present in ((1, True), (2, True), (2, False)):
            with self.subTest(fail_on=fail_on, log_present=log_present):
                if not log_present:
                    self.log.unlink()
                    facts.write_atomic(self.facts, self.rows)  # all unreviewed again, no log
                before, calls, real = snapshot(self.dir), [], os.replace

                def flaky(src, dst):
                    calls.append(dst)
                    if len(calls) == fail_on:
                        raise OSError("disk full")
                    return real(src, dst)
                with mock.patch.object(facts.os, "replace", flaky):
                    code, out, _ = self.review([self.b], "disputed")
                self.assertEqual((code, out), (1, ["io: OSError"]))
                self.assertEqual(snapshot(self.dir), before, "a failed write changed the store")
                self.assertEqual(self.log.exists(), log_present)
                self.assertEqual([p.name for p in self.folder.iterdir() if p.name.startswith(".")], [])


if __name__ == "__main__":
    unittest.main()
