"""DFGL-07a: evidence fingerprint, status-blind projection hash and the directory flock import and review take."""
import contextlib, csv, hashlib, importlib.util, io, json, os, shutil, tempfile, unittest
from pathlib import Path
from unittest import mock

from tests.flock.test_facts import FIXTURE, TOOL
from tests.flock.test_review import SLUG, make_review, mark_all, tool

_spec = importlib.util.spec_from_file_location("flock_facts_writer_under_test", TOOL)
facts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(facts)


def run(*args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = facts.main(list(args))
    return code, out.getvalue()


def listing(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mode) for p in Path(root).rglob("*"))


class WriterInterfaceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.store = self.dir / "work" / "store"
        self.src = self.dir / "src.csv"
        shutil.copy(FIXTURE, self.src)
        self.imp = ["import", str(self.src), "--store", str(self.store), "--added-by", "ana", "--status", "unreviewed", "--date", "2026-01-02"]

    def agency(self):
        self.assertEqual(run(*self.imp)[0], 0)
        path = next(self.store.glob("agencies/*/FACTS.csv"))
        return path, facts.load(path)[1][0][1]["fact_id"]

    def review(self, slug, fid):
        return run("review", "--store", str(self.store), "--agency", slug, "--ids", fid, "--set", "checked", "--reviewer", "ben")

    def test_projection_equal_across_a_real_review(self):
        path, fid = self.agency()
        self.assertEqual(facts.projection_sha256(path), hashlib.sha256(path.read_bytes()).hexdigest())
        before = path.read_bytes()
        self.assertEqual(self.review(path.parent.name, fid)[0], 0)
        self.assertNotEqual(path.read_bytes(), before)
        self.assertEqual(facts.projection_sha256(path), hashlib.sha256(before).hexdigest())
        self.assertEqual((facts.projection_sha256(self.dir / "none.csv"), facts.projection_sha256(self.src)), ("missing", "invalid"))

    def test_evidence_fp_fields(self):
        row = dict(zip(facts.V1, ["A", "R", "t", "v", "a" * 64, "1", "q"]), fact_id="F-0", kind="record", effective_from="1",
                   effective_to="2", added_by="x", added_at="3", status="unreviewed", source_path="/p")
        self.assertEqual(len(facts.EVIDENCE_FIELDS), 14)
        for name in facts.EVIDENCE_FIELDS:
            self.assertNotEqual(facts.evidence_fp(dict(row, **{name: "changed"})), facts.evidence_fp(row), name)
        self.assertEqual(facts.evidence_fp(dict(row, status="checked")), facts.evidence_fp(row))
        v2, v3 = dict(row, source_path=""), dict(row, source_path="")
        del v2["source_path"]
        self.assertEqual(facts.evidence_fp(v2), facts.evidence_fp(v3))

    def test_held_lock_refuses_review_and_import(self):
        path, fid = self.agency()
        for exclusive in (True, False):
            with mock.patch.object(facts, "LOCK_WAIT", 0.2), facts.store_lock(self.store, exclusive):
                before = (path.read_bytes(), listing(self.dir))
                self.assertEqual(self.review(path.parent.name, fid), (1, "locked: fact store busy\n"))
                self.assertEqual(run(*self.imp), (1, "locked: fact store busy\n"))
                self.assertEqual((path.read_bytes(), listing(self.dir)), before)
        self.assertEqual(self.review(path.parent.name, fid)[0], 0)

    def test_no_lock_file_and_refused_import_creates_nothing(self):
        self.assertEqual(run(*self.imp)[0], 0)
        path = next(self.store.glob("agencies/*/FACTS.csv"))
        fid = facts.load(path)[1][0][1]["fact_id"]
        before = listing(self.dir)
        self.assertEqual(run(*self.imp)[0], 0)  # all duplicates: still no new entry
        self.assertEqual(listing(self.dir), before)
        self.assertEqual(self.review(path.parent.name, fid)[0], 0)
        self.assertEqual({p for p, _ in listing(self.dir)} - {p for p, _ in before}, {"work/store/agencies/%s/FACT-REVIEWS.csv" % path.parent.name})
        fresh = self.dir / "fresh" / "store"
        self.assertEqual(run("import", str(self.dir / "absent.csv"), "--store", str(fresh), "--added-by", "ana")[0], 1)
        self.assertFalse(fresh.exists())

    def test_dag_facts_import_records_only_the_facts_file(self):
        config, p = make_review(self.dir / "dag")
        self.assertEqual(tool("run", config)[0], 3)  # waits at fact-drafts; mark it and run on into facts-import
        (p["FO"] / "drafts").mkdir(parents=True)
        (p["FO"] / "drafts" / "a.yaml").write_text("x\n")
        self.assertEqual(mark_all(config, {"fact-drafts": "anthropic"}), [0])
        self.assertEqual(tool("run", config)[0], 3)
        outputs = json.loads((p["out"] / "REVIEW-STATE.json").read_text())["stages"]["facts-import"]["outputs"]
        self.assertEqual(sorted(outputs), ["store/agencies/%s/FACTS.csv" % SLUG])


if __name__ == "__main__":
    unittest.main()
