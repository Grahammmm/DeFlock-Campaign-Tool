"""flock_report region --check: the region draft on disk vs a fresh build, read-only (RF1710-9)."""
import os, shutil, stat, unittest
from pathlib import Path

from tests.flock import test_report as base
from tests.flock import test_report_region as reg

A, B, NAMES = reg.A, reg.B, reg.NAMES
TAIL = " | draft vs a fresh build, not a review verdict"
SENTINEL = "Sentinel QZXW7 kept."
NEVER = (reg.A_NAME, reg.B_NAME, "North records index", "Sample retention schedule", "Sample State Code", base.BASIS, "QZXW7")


def expect(region, sources, a="current", b="current"):
    states = (region, sources)
    current, excluded = (a, b).count("current"), 2 - (a, b).count("current")
    return "".join(["region-check: %s %s\n" % (n, s) for n, s in zip(NAMES, states)] +
                   ["region-check: agency %s %s\n" % (A, a), "region-check: agency %s %s\n" % (B, b),
                    "region-check: current=%d stale=%d missing=%d link=%d agencies_current=%d excluded=%d%s\n" % (
                        states.count("current"), states.count("stale"), states.count("missing"), states.count("link"),
                        current, excluded, TAIL)])


class FlockReportRegionCheckTest(unittest.TestCase):
    setUp, stage = base.FlockReportTest.setUp, base.FlockReportTest.stage
    agency_b, two, run_region = reg.FlockReportRegionTest.agency_b, reg.FlockReportRegionTest.two, reg.FlockReportRegionTest.run_region

    def snapshot(self):  # every path under the test root: lstat mode, size, mtime_ns, and the bytes of regular files
        seen = {}
        for root, dirs, files in os.walk(self.tmp):
            for name in dirs + files:
                path = os.path.join(root, name)
                st = os.lstat(path)
                seen[path] = (st.st_mode, st.st_size, st.st_mtime_ns,
                              Path(path).read_bytes() if stat.S_ISREG(st.st_mode) else os.path.islink(path))
        return seen

    def check(self, ag, matrix, out, *extra, slugs=(A, B)):  # runs --check; asserts read-only and no draft/label text in output
        before = self.snapshot()
        result = self.run_region(ag, matrix, out, "--check", *extra, slugs=slugs)
        self.assertTrue(before == self.snapshot(), "region --check changed a file, mtime or directory")
        for text in NEVER:
            self.assertNotIn(text, result[1] + result[2])
        return result

    def built(self, **kwargs):
        ag, matrix, out = self.two(**kwargs)
        self.assertEqual(self.run_region(ag, matrix, out)[0], 0)
        return ag, matrix, out

    def test_current_after_region(self):
        ag, matrix, out = self.built()
        self.assertEqual(self.check(ag, matrix, out), (0, expect("current", "current"), ""))
        self.assertEqual(self.run_region(ag, matrix, out, "--replace")[0], 0)  # a rerun of region stays current
        self.assertEqual(self.check(ag, matrix, out), (0, expect("current", "current"), ""))

    def test_agency_stale_or_missing_after_region(self):
        ag, matrix, out = self.built()
        draft = ag[A][1]["CO"] / "report" / "REPORT-DRAFT.md"
        draft.write_text(draft.read_text() + "- **Hand-added line** [1 p.9]\n")  # A is now excluded from a fresh build
        self.assertEqual(self.check(ag, matrix, out), (1, expect("stale", "stale", a="stale"), ""))
        os.unlink(ag[B][1]["CO"] / "report" / "SOURCES.json")
        self.assertEqual(self.check(ag, matrix, out), (1, expect("stale", "stale", a="stale", b="missing"), ""))
        self.assertEqual(self.run_region(ag, matrix, out, "--replace")[0], 0)  # rebuilt with both excluded: current again
        self.assertEqual(self.check(ag, matrix, out), (0, expect("current", "current", a="stale", b="missing"), ""))

    def test_matrix_cell_changed(self):
        ag, matrix, out = self.built()
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX.replace("audit,duty,medium,strong", "audit,duty,medium,weak"))
        code, stdout, stderr = self.check(ag, matrix, out)
        self.assertEqual((code, stderr), (1, ""))
        self.assertEqual(stdout.splitlines()[0], "region-check: REGION-REPORT-DRAFT.md stale")
        self.assertIn("region-check: agency %s current\nregion-check: agency %s current\n" % (A, B), stdout)
        self.assertTrue(stdout.endswith("agencies_current=2 excluded=0%s\n" % TAIL), stdout)
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX)  # back to the built matrix: current
        self.assertEqual(self.check(ag, matrix, out)[0], 0)

    def test_missing_region_files_never_create_out(self):
        ag, matrix, out = self.two()
        self.assertEqual(self.check(ag, matrix, out), (1, expect("missing", "missing"), ""))
        self.assertFalse(out.exists())
        out.mkdir()
        self.assertEqual(self.check(ag, matrix, out), (1, expect("missing", "missing"), ""))
        self.assertEqual(os.listdir(out), [])
        self.assertEqual(self.run_region(ag, matrix, out)[0], 0)
        os.unlink(out / NAMES[1])  # one file gone: that one is named
        self.assertEqual(self.check(ag, matrix, out), (1, expect("current", "missing"), ""))

    def test_link_named_target_never_read(self):
        ag, matrix, out = self.built()
        copy = self.tmp / "copy.md"  # the link points at identical bytes: following it would say current
        shutil.copyfile(out / NAMES[0], copy)
        os.unlink(out / NAMES[0])
        (out / NAMES[0]).symlink_to(copy)
        self.assertEqual(self.check(ag, matrix, out), (1, expect("link", "current"), ""))
        self.assertTrue((out / NAMES[0]).is_symlink())
        os.unlink(out / NAMES[1])
        (out / NAMES[1]).mkdir()  # a directory in place of a file is never current
        self.assertEqual(self.check(ag, matrix, out), (1, expect("link", "stale"), ""))

    def test_invalid_build(self):
        ag, matrix, out = self.built()
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX.replace("topic,claim_class", "claim_class,topic", 1))
        self.assertEqual(self.check(ag, matrix, out), (1, "matrix: COMPARE-MATRIX.csv header is not topic, claim_class, then "
                                                       "each agency slug once\nregion-check: invalid\n", ""))
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX.replace("strong,rejected", "strongest,rejected"))
        self.assertEqual(self.check(ag, matrix, out), (1, "matrix: COMPARE-MATRIX.csv has no rows or a cell that is not "
                                                       "strong|medium|weak|rejected|-\nregion-check: invalid\n", ""))
        (matrix / "COMPARE-MATRIX.csv").write_text(reg.MATRIX)
        swapped = {A: ag[B], B: ag[A]}
        self.assertEqual(self.check(swapped, matrix, out)[:2], (1, "agency %s: config: review.slug is not the matrix column\n"
                                                                  "agency %s: config: review.slug is not the matrix column\n"
                                                                  "region-check: invalid\n" % (A, B)))
        inside = base.ROOT / "tests" / "fixtures" / "flock" / "report" / "region-out"
        self.assertEqual(self.check(ag, matrix, inside)[:2], (1, "out: is inside the repository\nregion-check: invalid\n"))
        self.assertFalse(inside.exists())

    def test_check_with_replace_is_usage(self):
        ag, matrix, out = self.two()
        code, stdout, stderr = self.check(ag, matrix, out, "--replace")
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("--check and --replace cannot be combined", stderr)
        self.assertFalse(out.exists())
        code, stdout, stderr = self.check(ag, matrix, out, slugs=(A,))  # column mapping stays a usage error
        self.assertEqual((code, stdout), (2, ""))
        self.assertIn("matrix column %s has no --agency" % B, stderr)

    def test_finding_text_never_printed(self):
        ag, matrix, out = self.built(edit=lambda text, _: text.replace(base.BASIS, base.BASIS + " " + SENTINEL))
        self.assertIn(SENTINEL, (ag[A][1]["CO"] / "report" / "REPORT-DRAFT.md").read_text())  # planted in a shown finding
        self.assertEqual(self.check(ag, matrix, out), (0, expect("current", "current"), ""))
        (out / NAMES[1]).write_text((out / NAMES[1]).read_text() + "organizer note " + SENTINEL + "\n")
        self.assertEqual(self.check(ag, matrix, out), (1, expect("current", "stale"), ""))


if __name__ == "__main__":
    unittest.main()
