"""RF1239-2: flock_sweep matrix --prev DIR writes COMPARE-CHANGES.csv (one row per changed cell) and one counts line.
A bad --prev refuses before anything is created or replaced. Synthetic fixtures only (the RF1058-6 two-agency store)."""
import contextlib, io, os, unittest
from tests.flock import test_sweep as base
from tests.flock import test_sweep_matrix_edges as edges

AGENCY, SECOND, MATRIX, sweep = edges.AGENCY, edges.SECOND, edges.MATRIX, edges.sweep
MLINE = "matrix rows=7 agencies=2\n"
COUNTS = "changes rows=%d up=%d down=%d added=%d removed=%d agencies_added=%d agencies_removed=%d\n"
HEAD = "topic,claim_class,agency,old,new,change\n"


class SweepMatrixPrev(unittest.TestCase):
    setUp, finish, sweep, build = (base.FlockSweepTest.setUp, base.FlockSweepTest.finish, base.FlockSweepTest.sweep,
                                   edges.SweepMatrixEdges.build)

    def mx(self, out, *extra, command="matrix"):
        buf = io.StringIO()
        args = [command, "--watchlist", base.WATCH, "--roster", base.ROSTER, "--store", str(self.store)]
        with contextlib.redirect_stdout(buf):
            code = sweep.main(args + (["--out", str(out)] if out else []) + list(extra))
        return code, buf.getvalue()

    def prev(self, text, name="a"):
        folder = self.tmp / name
        folder.mkdir()
        (folder / "COMPARE-MATRIX.csv").write_text(text)
        return folder

    def changed(self, text, counts, rows):
        code, out = self.mx(self.tmp / "b", "--prev", str(self.prev(text)))
        self.assertEqual((code, out), (0, MLINE + COUNTS % counts))
        self.assertEqual((self.tmp / "b" / "COMPARE-CHANGES.csv").read_text(), HEAD + "".join(r + "\n" for r in rows))
        self.assertEqual((self.tmp / "b" / "COMPARE-MATRIX.csv").read_text(), MATRIX)
        for leak in (AGENCY, SECOND, "record-gap", "audits", "withheld-training", "zz-topic", "gone-agency", str(self.tmp)):
            self.assertNotIn(leak, out)  # counts only on stdout

    def test_no_change_header_only(self):
        self.build()
        self.assertEqual(self.mx(self.tmp / "a"), (0, MLINE))
        self.assertEqual(self.mx(self.tmp / "b", "--prev", str(self.tmp / "a")), (0, MLINE + COUNTS % ((0,) * 7)))
        changes = self.tmp / "b" / "COMPARE-CHANGES.csv"
        self.assertEqual((changes.read_text(), changes.stat().st_mode & 0o777), (HEAD, 0o600))
        for name in ("COMPARE-MATRIX.csv", "COMPARE.md"):
            self.assertEqual((self.tmp / "b" / name).read_bytes(), (self.tmp / "a" / name).read_bytes())

    def test_up_and_down_by_order(self):
        self.build()
        text = MATRIX.replace("audits,record-gap,strong,strong", "audits,record-gap,weak,strong") \
            .replace("retention-changes,record-gap,medium,medium", "retention-changes,record-gap,medium,strong") \
            .replace("outside-agency-access,duty-concern,rejected,rejected", "outside-agency-access,duty-concern,weak,rejected")
        self.changed(text, (3, 1, 2, 0, 0, 0, 0), [
            "audits,record-gap,%s,weak,strong,up" % AGENCY,
            "outside-agency-access,duty-concern,%s,weak,rejected,down" % AGENCY,
            "retention-changes,record-gap,%s,strong,medium,down" % SECOND])

    def test_added_and_removed_keys(self):
        self.build()  # a key gone from the old matrix is added; an old-only key is removed; '-' to '-' is no change
        text = MATRIX.replace("withheld-training,record-gap,strong,strong\n", "") + "zz-topic,record-gap,weak,-\n"
        self.changed(text, (3, 0, 0, 2, 1, 0, 0), [
            "withheld-training,record-gap,%s,-,strong,added" % AGENCY,
            "withheld-training,record-gap,%s,-,strong,added" % SECOND,
            "zz-topic,record-gap,%s,weak,-,removed" % AGENCY])

    def test_agency_removed_and_added(self):
        self.build()
        lines = MATRIX.splitlines()
        gone = [lines[0] + ",gone-agency", lines[1] + ",medium"] + [l + ",-" for l in lines[2:]]
        self.changed("\n".join(gone) + "\n", (1, 0, 0, 0, 1, 0, 1), ["audits,record-gap,gone-agency,medium,-,removed"])
        os.rename(self.tmp / "a", self.tmp / "a-old")
        os.rename(self.tmp / "b", self.tmp / "b-old")
        one = [l.rsplit(",", 1)[0] for l in lines]  # an old matrix without SECOND: each of its cells is added
        self.changed("\n".join(one) + "\n", (7, 0, 0, 7, 0, 1, 0),
                     ["%s,%s,%s,-,%s,added" % (t, c, SECOND, r) for t, c, r in edges.CELLS])

    def test_bad_prev_refused_nothing_written(self):
        self.build()
        good = self.prev(MATRIX, "good")
        (self.tmp / "nodir").mkdir()
        (self.tmp / "afile").write_text(MATRIX)
        (self.tmp / "linkfile").mkdir()
        (self.tmp / "linkfile" / "COMPARE-MATRIX.csv").symlink_to(good / "COMPARE-MATRIX.csv")
        (self.tmp / "linkdir").symlink_to(good)
        missing, link, header = "prev: COMPARE-MATRIX.csv is missing", "prev: COMPARE-MATRIX.csv is a link", "prev: header is invalid"
        rows = MATRIX.splitlines(True)
        cases = [(self.tmp / "absent", missing), (self.tmp / "nodir", missing), (self.tmp / "afile", missing),
                 (self.tmp / "linkfile", link), (self.tmp / "linkdir", link)] + [
            (self.prev(text, "p%d" % n), msg) for n, (text, msg) in enumerate((
                ("", header), (MATRIX.replace("topic,claim_class", "topic,claim", 1), header),
                (MATRIX.replace(SECOND, AGENCY, 1), header), (MATRIX.replace("," + SECOND, ",", 1), header),
                (MATRIX.replace(",strong,", ",excellent,", 1), "prev: row 2 is invalid"),
                ("".join(rows[:3] + rows[2:]), "prev: row 4 is invalid"),
                ("".join(rows[:4]) + "audits,record-gap\n", "prev: row 5 is invalid"),
                (MATRIX.replace("audits,", ",", 1), "prev: row 2 is invalid"),
                (MATRIX + "zz,x,strong,strong,weak\n", "prev: row 9 is invalid")))]
        keep = self.tmp / "keep"
        keep.mkdir()
        (keep / "COMPARE-MATRIX.csv").write_text("old matrix\n")
        for prev, message in cases:
            for out in (self.tmp / "new", keep):
                with self.subTest(prev=prev.name, out=out.name):
                    self.assertEqual(self.mx(out, "--prev", str(prev)), (1, message + "\n"))
                    self.assertFalse((self.tmp / "new").exists())
                    self.assertEqual([p.name for p in keep.iterdir()], ["COMPARE-MATRIX.csv"])
                    self.assertEqual((keep / "COMPARE-MATRIX.csv").read_text(), "old matrix\n")

    def test_prev_same_folder_as_out(self):
        self.build()
        self.assertEqual(self.mx(self.tmp / "a"), (0, MLINE))
        old = self.tmp / "a" / "COMPARE-MATRIX.csv"
        old.write_text(MATRIX.replace("audits,record-gap,strong,strong", "audits,record-gap,strong,medium"))
        self.assertEqual(self.mx(self.tmp / "a", "--prev", str(self.tmp / "a")), (0, MLINE + COUNTS % (1, 1, 0, 0, 0, 0, 0)))
        self.assertEqual((old.read_text(), (self.tmp / "a" / "COMPARE-CHANGES.csv").read_text()),
                         (MATRIX, HEAD + "audits,record-gap,%s,medium,strong,up\n" % SECOND))

    def test_prev_only_with_matrix(self):
        self.build()
        a = self.prev(MATRIX)
        for command, out in (("status", None), ("plan", None), ("status", self.tmp / "s.csv"), ("matrix", None)):
            err = io.StringIO()
            with self.subTest(command=command, out=out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as ctx:
                self.mx(out, "--prev", str(a), command=command)
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("[--date D] [--prev DIR]", err.getvalue())
        self.assertFalse((self.tmp / "s.csv").exists())

    def test_without_prev_unchanged(self):
        self.build()
        self.assertEqual(self.sweep("matrix", "--out", str(self.tmp / "c")), (0, MLINE))
        self.assertEqual(sorted(p.name for p in (self.tmp / "c").iterdir()), ["COMPARE-MATRIX.csv", "COMPARE.md"])
        self.assertEqual((self.tmp / "c" / "COMPARE-MATRIX.csv").read_bytes(), MATRIX.encode())


if __name__ == "__main__":
    unittest.main()
