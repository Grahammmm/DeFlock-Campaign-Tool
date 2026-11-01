import contextlib, csv, io, json, os, socket, subprocess, unittest
from unittest.mock import patch

from tests.flock import test_review_batch as base
from tests.flock.test_review import ROOT, SLUG
from tests.flock.test_review_lock import load

batch = load("_rf1700_4_batch", ROOT / "tools" / "flock_review_batch.py")
SECOND, CAMPUS = base.SLUGS[1:]
SENTINEL = "RF1700-4-SENTINEL"
PLAIN = ("done", "ready", "waiting", "in-flight", "failed")


class ReviewBatchWhyTest(unittest.TestCase):
    setUp = base.FlockReviewBatchTest.setUp

    def world(self, stubs=None):  # the three base agencies; a free-text sentinel in a config value that is never shown
        base.FlockReviewBatchTest.world(self, stubs=stubs)
        self.review(0, added_by=SENTINEL)

    def review(self, n, **fields):  # set review.<field> in agency n's config
        cfg = json.loads(self.configs[n].read_text())
        cfg["review"].update(fields)
        self.configs[n].write_text(json.dumps(cfg))

    def main(self, *configs, roster=base.ROSTER, words=(), out=None):  # -> exit code, stdout, BATCH-STATUS.csv rows
        out = out or self.out
        self.list.write_text("".join("%s\n" % c for c in configs or self.configs))
        args = ["status", str(self.list), "--out", str(out)] + (["--roster", str(roster)] if roster else []) + [str(w) for w in words]
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
            code = batch.main(args)
        status = out / "BATCH-STATUS.csv"
        text = status.read_text() if status.exists() else ""
        for written in (shown.getvalue(), text):
            self.assertNotIn(SENTINEL, written)
        return code, shown.getvalue(), list(csv.DictReader(io.StringIO(text)))

    def why(self, *configs, **kw):  # -> [(line, csv why)] per agency; every line with a why ends ` why=<the csv why>`
        code, out, rows = self.main(*configs, **kw)
        self.assertEqual(code, 0, out)
        lines = out.splitlines()[-len(rows) - 1:-1]
        for line, row in zip(lines, rows):
            self.assertEqual(line, "%s %s %s/%s next=%s" % (row["slug"] or "-", row["state"], row["stages_done"], row["stages_total"],
                                                           row["next"] or "-") + (" why=" + row["why"] if row["why"] else ""))
        return [(row["state"], row["why"]) for row in rows], out

    def lock(self, n=0):
        path = self.outs[n] / ".review" / "LOCK"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    def test_unreadable_config(self):
        self.world()
        bad, odd = self.tmp / "bad.json", self.tmp / "odd.json"
        bad.write_text("{not json " + SENTINEL)
        odd.write_text(json.dumps([SENTINEL]))  # JSON, but not a config object
        found, out = self.why(self.configs[0], bad, odd)
        self.assertEqual(found, [("ready", ""), ("invalid", "config-unreadable"), ("invalid", "config-unreadable")])
        self.assertIn("\n- invalid / next=- why=config-unreadable\n", out)

    def test_first_config_field_and_more_problems(self):
        self.world()
        self.review(0, run_date=SENTINEL)
        self.assertEqual(self.why(self.configs[0])[0], [("invalid", "config:review.run_date")])
        self.review(0, added_by=" ", method_python=str(self.tmp / SENTINEL))
        found, out = self.why(self.configs[0])
        self.assertEqual(found, [("invalid", "config:review.run_date+2")])
        self.assertIn("%s invalid / next=- why=config:review.run_date+2\n" % SLUG, out)

    def test_odd_problem_text_is_never_shown(self):
        self.world()
        cases = {"config:other": ["config: %s/x is bad" % SENTINEL], "config:other+1": ["free text " + SENTINEL, "config: review.slug x"],
                 "config:review.slug+1": ["config: review.slug " + SENTINEL, "config: %s y" % SENTINEL],
                 "config:other+2": ["config: review.slug\x1b[2J%s x" % SENTINEL, "a", "b"], "config:CONFIG": ["config: CONFIG is inside"]}
        for code, problems in cases.items():
            with self.subTest(code=code), patch.object(batch.fr, "check", return_value=problems):
                self.assertEqual(self.why(self.configs[0])[0], [("invalid", code)])

    def test_state_unreadable_names_the_exception_class_only(self):
        self.world()
        for exc, name in ((ValueError(SENTINEL), "ValueError"), (PermissionError(13, SENTINEL), "PermissionError"),
                          (KeyError(SENTINEL), "KeyError"), (batch.fr.Fail(SENTINEL), "Fail")):
            with self.subTest(name=name), patch.object(batch.fr, "load_state", side_effect=exc):
                self.assertEqual(self.why(self.configs[0])[0], [("invalid", "state-unreadable:" + name)])
        (self.outs[0] / "REVIEW-STATE.json").write_text("{" + SENTINEL)  # the real reader refuses it
        self.assertEqual(self.why(self.configs[0])[0], [("invalid", "state-unreadable:Fail")])

    def test_roster_codes(self):
        self.world()
        roster, lines = self.tmp / "roster.csv", base.ROSTER.read_text(encoding="utf-8").splitlines()
        self.assertEqual(self.why()[0], [("ready", ""), ("ready", ""), ("held", "roster:not-in-scope")])  # the fixture: campus is `hold`
        rows = [lines[0], lines[1].replace("local ALPR policy applies", "local ALPR %s applies" % SENTINEL),  # applicability differs
                lines[3].replace(",hold,", ",no,")]  # second-city-police has no row; campus in_scope=no
        roster.write_text("".join(r + "\n" for r in rows), encoding="utf-8")
        self.assertEqual(self.why(roster=roster)[0], [("held", "roster:applicability-differs"), ("held", "roster:no-row"),
                                                     ("held", "roster:not-in-scope")])
        roster.write_text("".join(r + "\n" for r in rows[:2] + [lines[3].replace("local ALPR", SENTINEL)]), encoding="utf-8")
        self.assertEqual(self.why(roster=roster)[0][2], ("held", "roster:not-in-scope"))  # out of scope wins over applicability
        self.assertEqual(self.why(roster=None)[0], [("ready", ""), ("ready", ""), ("ready", "")])  # no roster: nothing is held

    def test_lock_codes(self):
        self.world()
        lock, host = self.lock(), socket.gethostname()
        target = self.tmp / "target"
        target.write_text("%d %s\n" % (os.getpid(), host))
        lock.symlink_to(target)
        self.assertEqual(self.why(self.configs[0], roster=None)[0], [("locked", "lock:not-regular-file")])
        lock.unlink()
        for text, code in (("%d %s\n" % (os.getpid(), host), "lock:live-writer"), ("%d\n" % os.getpid(), "lock:live-writer"),
                           ("4242 %s\n" % SENTINEL, "lock:other-host"), ("%s words\n" % SENTINEL, "lock:unparseable")):
            with self.subTest(code=code, text=text):
                lock.write_text(text)
                found, out = self.why(self.configs[0], roster=None)
                self.assertEqual(found, [("locked", code)])
                self.assertNotIn(" pid ", out)
        lock.write_text("%d %s\n" % (os.getpid(), host))
        lock.chmod(0)
        self.addCleanup(lock.chmod, 0o600)
        with contextlib.ExitStack() as stack:
            if os.access(lock, os.R_OK):  # root reads anything: the reader's own detail stands in
                stack.enter_context(patch.object(batch.fr, "lock_state", return_value=("held", "unreadable")))
            self.assertEqual(self.why(self.configs[0], roster=None)[0], [("locked", "lock:unreadable")])
        for detail in ("pid 12 on host " + SENTINEL, SENTINEL, "", "pid x"):
            with self.subTest(detail=detail), patch.object(batch.fr, "lock_state", return_value=("held", detail)):
                self.assertEqual(self.why(self.configs[0], roster=None)[0], [("locked", "lock:held")])
        with patch.object(batch.fr, "lock_state", return_value=("stale", "pid 12 not running")):
            self.assertEqual(self.why(self.configs[0], roster=None)[0], [("ready", "")])

    def test_held_wins_over_locked(self):
        self.world()
        self.lock(2).symlink_to(self.tmp / "nowhere")
        self.assertEqual(self.why()[0][2], ("held", "roster:not-in-scope"))

    def test_other_states_have_no_why(self):
        self.world(stubs={"inventory": "sys.exit(1)"})
        seen = set()
        found, _ = self.why(words=["--run", *self.gate])
        self.assertEqual(found, [("failed", ""), ("waiting", ""), ("held", "roster:not-in-scope")])
        draft = self.outs[1] / "facts" / "drafts" / "a.yaml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("draft\n")
        seen.update(found + self.why()[0])
        with patch.object(batch.fr, "evaluate", return_value=("done", None)):
            seen.update(self.why(roster=None)[0])
        seen.update(self.why(self.configs[2], roster=None)[0])
        self.assertEqual({w for s, w in seen if s in PLAIN}, {""})
        self.assertEqual({s for s, w in seen} & set(PLAIN), set(PLAIN))

    def test_failed_run_clears_a_why(self):  # a run that fails and leaves the agency locked is `failed`, with no why
        self.world()
        lock = self.lock()

        def child(argv, **kw):
            lock.symlink_to(self.tmp / "nowhere")
            return subprocess.CompletedProcess(argv, 1, b"", b"")
        with patch.object(batch.subprocess, "run", side_effect=child):
            found, out = self.why(self.configs[0], roster=None, words=["--run", *self.gate])
        self.assertEqual(found, [("failed", "")])
        self.assertIn("run %s rc=1 state=failed\n" % SLUG, out)
        self.assertEqual(self.why(self.configs[0], roster=None)[0], [("locked", "lock:not-regular-file")])

    def test_header_and_prev(self):
        self.world()
        self.assertEqual(self.main(out=self.tmp / "new")[0], 0)
        new = self.tmp / "new" / "BATCH-STATUS.csv"
        self.assertEqual(new.read_text().splitlines()[0], ",".join(base.HEADER))
        self.assertTrue(new.read_text().splitlines()[0].endswith(",run_date,why"))
        old = self.tmp / "old.csv"
        with new.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        with old.open("w", newline="") as handle:  # a BATCH-STATUS.csv written before the why column
            writer = csv.DictWriter(handle, fieldnames=base.HEADER[:-1], extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        self.assertNotIn("why", old.read_text())
        for prev in (new, old):
            with self.subTest(prev=prev.name):
                code, out, _ = self.main(words=["--prev", prev], out=self.tmp / ("o" + prev.stem))
                self.assertEqual(code, 0, out)
                self.assertIn("changes advanced=0 regressed=0 state=0 new=0 removed=0 unchanged=3\n", out)
        body = "".join(new.read_text().splitlines(keepends=True)[1:])
        for header in (base.HEADER + ["extra"], ["why"] + base.HEADER[:-1], base.HEADER[:-2] + ["why"], base.HEADER[:-2],
                       base.HEADER[:-1] + ["WHY"]):
            with self.subTest(header=header):
                bad = self.tmp / "bad.csv"
                bad.write_text(",".join(header) + "\n" + body)
                code, out, _ = self.main(words=["--prev", bad], out=self.tmp / "refused")
                self.assertEqual((code, out), (1, "prev: header differs\n"))
                self.assertFalse((self.tmp / "refused").exists())


if __name__ == "__main__":
    unittest.main()
