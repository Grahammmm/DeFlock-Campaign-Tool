import contextlib, csv, io, json, os, re, unittest

from tests.flock import test_review_batch as base
from tests.flock.test_review import ROOT, SLUG, mark_all
from tests.flock.test_review_lock import load

batch = load("_rf1700_2_batch", ROOT / "tools" / "flock_review_batch.py")
SECOND, CAMPUS = base.SLUGS[1:]
CHANGES = "key,change,old_state,new_state,old_done,new_done\n"
SENTINEL = "RF1700-2-SENTINEL-free-text"
SUMMARY = re.compile(r"agencies=(\d+) done=(\d+) waiting=(\d+) in_flight=(\d+) failed=(\d+) invalid=(\d+) held=(\d+) locked=(\d+) ready=(\d+)")


class ReviewBatchPrevTest(unittest.TestCase):
    setUp, trees = base.FlockReviewBatchTest.setUp, base.FlockReviewBatchTest.trees

    def world(self, stubs=None, invalid=0):  # the three base agencies, a free-text sentinel and `invalid` unreadable configs
        base.FlockReviewBatchTest.world(self, stubs=stubs)
        cfg = json.loads(self.configs[0].read_text())
        cfg["review"]["added_by"] = SENTINEL
        self.configs[0].write_text(json.dumps(cfg))
        self.bad = [self.tmp / ("bad%d.json" % n) for n in range(invalid)]
        for path in self.bad:
            path.write_text("{not json " + SENTINEL)
        self.listing(*self.configs, *self.bad)

    def listing(self, *configs):
        self.list.write_text("".join("%s\n" % c for c in configs))

    def main(self, *words, out=None, prev=None):
        args = ["status", str(self.list), "--roster", str(base.ROSTER), "--out", str(out or self.out)] + (["--prev", str(prev)] if prev else [])
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
            code = batch.main(args + [str(w) for w in words])
        out = shown.getvalue()
        self.assertNotIn(SENTINEL, out)
        totals = [int(n) for n in SUMMARY.search(out).groups()] if code == 0 else None
        if totals:  # every state is counted once: done+waiting+in_flight+failed+invalid+held+locked+ready == agencies
            self.assertEqual(sum(totals[1:]), totals[0], out)
        return code, out, totals

    def snapshot(self, name="prev"):  # a status into its own folder; -> its BATCH-STATUS.csv
        self.assertEqual(self.main(out=self.tmp / name)[0], 0)
        return self.tmp / name / "BATCH-STATUS.csv"

    def edit(self, path, slug, **fields):  # rewrite one row of a prev CSV
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        [row.update(fields) for row in rows if row["slug"] == slug]
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=base.HEADER, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return path

    def changed(self, out):
        return [x for x in out.splitlines() if x.startswith("change ")]

    def written(self, folder=None):
        written = (folder or self.out) / "BATCH-CHANGES.csv"
        self.assertNotIn(SENTINEL, written.read_text())
        self.assertEqual(written.stat().st_mode & 0o777, 0o600)
        return written.read_text()

    def test_identical_runs_have_no_changes(self):
        self.world()
        code, out, totals = self.main(prev=self.snapshot())
        self.assertEqual((code, self.changed(out)), (0, []), out)
        self.assertIn("\nchanges advanced=0 regressed=0 state=0 new=0 removed=0 unchanged=3\nagencies=3 ", out)
        self.assertEqual(totals, [3, 0, 0, 0, 0, 0, 1, 0, 2])  # ready=2: the two in-scope agencies
        self.assertEqual(self.written(), CHANGES)
        code, out, _ = self.main()  # no --prev: no change lines, no totals line, no BATCH-CHANGES.csv
        self.assertEqual((code, "changes " in out, (self.out / "BATCH-CHANGES.csv").exists()), (0, False, True))
        self.assertEqual(self.main(out=self.tmp / "plain")[0], 0)
        self.assertFalse((self.tmp / "plain" / "BATCH-CHANGES.csv").exists())

    def test_marked_wait_input_advances(self):
        self.world()
        self.assertEqual(self.main("--run", *self.gate, out=self.tmp / "prev")[0], 0)  # both reach the fact-drafts wait
        draft = self.outs[0] / "facts" / "drafts" / "a.yaml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("draft\n")
        self.assertEqual(mark_all(self.configs[0], {"fact-drafts": "anthropic"}), [0])
        code, out, _ = self.main(prev=self.tmp / "prev" / "BATCH-STATUS.csv")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.changed(out), ["change %s advanced waiting->ready 5->6" % SLUG], out)
        self.assertIn("changes advanced=1 regressed=0 state=0 new=0 removed=0 unchanged=2\n", out)
        self.assertEqual(self.written(), CHANGES + "%s,advanced,waiting,ready,5,6\n" % SLUG)

    def test_failed_regresses_and_recovery_is_a_state_change(self):
        self.world(stubs={"inventory": "sys.exit(1)"}, invalid=1)
        prev = self.snapshot()
        code, out, totals = self.main("--run", *self.gate, prev=prev)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.changed(out), ["change %s regressed ready->failed 2->2" % SLUG,
                                             "change %s advanced ready->waiting 2->5" % SECOND], out)  # regressed sorts first
        self.assertIn("changes advanced=1 regressed=1 state=0 new=0 removed=0 unchanged=2\n", out)
        self.assertEqual(totals, [4, 0, 1, 0, 1, 1, 1, 0, 0])
        # More stages done but entering failed is still a regression; locked -> failed stays inside the bad set, so `state`.
        self.edit(prev, SLUG, stages_done="1")
        self.edit(prev, SECOND, state="locked", stages_done="5")
        code, out, _ = self.main(prev=prev, out=self.tmp / "again")
        self.assertEqual(self.changed(out), ["change %s regressed ready->failed 1->2" % SLUG, "change %s state locked->waiting 5->5" % SECOND], out)
        self.edit(prev, SECOND, state="waiting", stages_done="5")
        self.edit(prev, SLUG, state="locked", stages_done="2")
        code, out, _ = self.main(prev=prev, out=self.tmp / "again")
        self.assertEqual(self.changed(out), ["change %s state locked->failed 2->2" % SLUG], out)

    def test_leaving_failed_is_a_state_change(self):
        self.world()
        prev = self.edit(self.snapshot(), SLUG, state="failed")
        self.edit(prev, SECOND, state="failed", stages_done="1")
        code, out, _ = self.main(prev=prev)
        self.assertEqual(self.changed(out), ["change %s advanced failed->ready 1->2" % SECOND, "change %s state failed->ready 2->2" % SLUG], out)
        self.assertEqual(self.written(), CHANGES + "%s,advanced,failed,ready,1,2\n%s,state,failed,ready,2,2\n" % (SECOND, SLUG))

    def test_unknown_prev_done_compares_state_only(self):
        self.world()
        prev = self.edit(self.snapshot(), SLUG, stages_done="x")
        self.edit(prev, SECOND, stages_done="x", state="waiting")
        self.edit(prev, CAMPUS, stages_done="9")  # a number still counts: 9 -> 2 regressed
        code, out, _ = self.main(prev=prev)
        self.assertEqual(self.changed(out), ["change %s regressed held->held 9->2" % CAMPUS, "change %s state waiting->ready x->2" % SECOND], out)
        self.assertIn("changes advanced=0 regressed=1 state=1 new=0 removed=0 unchanged=1\n", out)

    def test_new_removed_and_invalid_keys(self):
        self.world(invalid=2)
        prev = self.snapshot()
        self.listing(self.configs[0], self.configs[2], *self.bad)
        code, out, totals = self.main(prev=prev)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.changed(out), ["change %s removed ready->- 2->-" % SECOND], out)
        self.assertIn("changes advanced=0 regressed=0 state=0 new=0 removed=1 unchanged=4\n", out)  # both invalid rows: unchanged
        self.listing(*self.configs, *self.bad)
        code, out, totals = self.main(prev=self.out / "BATCH-STATUS.csv")
        self.assertEqual(self.changed(out), ["change %s new -->ready -->2" % SECOND], out)
        self.assertEqual(self.written(), CHANGES + "%s,new,,ready,,2\n" % SECOND)
        self.assertEqual(totals, [5, 0, 0, 0, 0, 2, 1, 0, 2])
        keys = [row["slug"] or "config:" + row["config"] for row in csv.DictReader(io.StringIO(prev.read_text()))]
        self.assertEqual(keys[3:], ["config:%s" % p.resolve() for p in self.bad])  # keyed by path, no collision
        self.listing(*self.configs, self.bad[0])
        code, out, _ = self.main(prev=prev)
        self.assertEqual(self.changed(out), ["change config:%s removed invalid->- -->-" % self.bad[1].resolve()], out)

    def test_prev_may_be_the_status_file_it_replaces(self):
        self.world()
        current = self.snapshot("batch")
        code, out, _ = self.main("--run", *self.gate, prev=current)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.changed(out), ["change %s advanced ready->waiting 2->5" % s for s in (SLUG, SECOND)], out)
        self.assertEqual([r["state"] for r in csv.DictReader(io.StringIO(current.read_text()))], ["waiting", "waiting", "held"])
        self.assertEqual(self.written(), CHANGES + "".join("%s,advanced,ready,waiting,2,5\n" % s for s in (SLUG, SECOND)))

    def test_bad_prev_is_refused_before_anything_runs(self):
        self.world(invalid=1)
        good = self.snapshot()
        cases = {"prev: header differs": good.read_text().replace("run_date", "date", 1),
                 "prev row 3: duplicate": good.read_text().replace(SECOND + ",", SLUG + ",", 1),
                 "prev row 6: duplicate": good.read_text() + good.read_text().splitlines()[-1] + "\n",  # the invalid row, keyed by path
                 "prev: cannot be read": b"\xff\xfe" + good.read_bytes()}
        before = self.trees()
        for word, data in [*cases.items(), ("prev: is a link; refused", good), ("prev: cannot be read", None),
                           ("prev: cannot be read", self.tmp)]:
            with self.subTest(word=word, data=str(data)[:40]):
                path = self.tmp / "bad-prev.csv"
                if path.is_symlink() or path.exists():
                    path.unlink()
                if isinstance(data, (str, bytes)):
                    path.write_bytes(data.encode() if isinstance(data, str) else data)
                elif data is not None:
                    os.symlink(data, path)
                code, out, _ = self.main("--run", *self.gate, prev=self.tmp if data == self.tmp else path)  # a folder cannot be read
                self.assertEqual((code, out), (1, word + "\n"))  # no golden line, no run, no status line
                self.assertFalse(self.out.exists())
                self.assertEqual(self.trees(), before)

    def test_prev_is_for_status_only(self):
        self.world()
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            batch.main(["plan", str(self.list), "--prev", str(self.snapshot()), *map(str, self.gate)])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("--prev is for status", err.getvalue())


if __name__ == "__main__":
    unittest.main()
