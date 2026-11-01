import contextlib, csv, io, json, os, socket, unittest
from unittest import mock

from tests.flock import test_review_batch as base
from tests.flock.test_review import ROOT, SLUG
from tests.flock.test_review_lock import load

batch = load("_rf1700_4_batch", ROOT / "tools" / "flock_review_batch.py")
SECOND, CAMPUS = base.SLUGS[1:]
SENTINEL = "RF1700-4-SENTINEL-free-text"
OLD = base.HEADER[:-1]  # the BATCH-STATUS.csv header before `why`


class ReviewBatchWhyTest(unittest.TestCase):
    setUp = base.FlockReviewBatchTest.setUp

    def world(self, stubs=None):  # the three base agencies; a free-text sentinel in a config value of the first
        base.FlockReviewBatchTest.world(self, stubs=stubs)
        self.edit(self.configs[0], added_by=SENTINEL)

    def edit(self, config, **review):  # set review.<key> fields of a config
        cfg = json.loads(config.read_text())
        cfg["review"].update(review)
        config.write_text(json.dumps(cfg))

    def listing(self, *configs):
        self.list.write_text("".join("%s\n" % c for c in configs))

    def main(self, *words, roster=base.ROSTER, prev=None):  # -> (exit code, printed lines); nothing ever shows the sentinel
        args = ["status", str(self.list), "--out", str(self.out)] + (["--roster", str(roster)] if roster else []) + (
            ["--prev", str(prev)] if prev else [])
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown), contextlib.redirect_stderr(io.StringIO()):
            code = batch.main(args + [str(w) for w in words])
        out = shown.getvalue()
        self.assertNotIn(SENTINEL, out)
        if (self.out / "BATCH-STATUS.csv").exists():
            self.assertNotIn(SENTINEL, (self.out / "BATCH-STATUS.csv").read_text())
        return code, out.splitlines()

    def rows(self):
        fields, rows = base.FlockReviewBatchTest.rows(self)
        self.assertEqual(fields, base.HEADER)
        return {r["slug"] or r["config"]: (r["state"], r["why"]) for r in rows}

    def one(self, config, roster=None):  # status of one config -> (its status line, its (state, why) row)
        self.listing(config)
        code, lines = self.main(roster=roster)
        self.assertEqual(code, 0, lines)
        return lines[0], next(iter(self.rows().values()))

    def lock(self, n=0):
        path = self.outs[n] / ".review" / "LOCK"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def test_invalid_configs_say_which_field(self):
        self.world()
        bad = self.tmp / "bad.json"
        bad.write_text("{not json " + SENTINEL)
        self.assertEqual(self.one(bad), ("- invalid / next=- why=config-unreadable", ("invalid", "config-unreadable")))
        self.edit(self.configs[0], run_date="31/10/2026 " + SENTINEL)
        line, row = self.one(self.configs[0])
        self.assertEqual((line, row), ("%s invalid / next=- why=config:review.run_date" % SLUG, ("invalid", "config:review.run_date")))
        self.edit(self.configs[0], added_by=" ", verify_sample=0)  # two more problems, after run_date in check order
        self.assertEqual(self.one(self.configs[0])[1], ("invalid", "config:review.run_date+2"))

    def test_problem_text_never_printed(self):
        self.world()
        cases = {("config: review.out\n%s is x" % SENTINEL,): "config:other", ("config: %s/odd! is x" % SENTINEL,): "config:other",
                 (SENTINEL,): "config:other", ("config:review.slug x",): "config:other", ("config: " + SENTINEL,): "config:other",
                 ("config: review.slug is not a lowercase slug " + SENTINEL, SENTINEL): "config:review.slug+1",
                 (None, "config: review.out x"): "config:other+1"}
        for problems, why in cases.items():
            with self.subTest(problems=problems), mock.patch.object(batch.fr, "check", return_value=list(problems)):
                self.assertEqual(self.one(self.configs[1]), ("%s invalid / next=- why=%s" % (SECOND, why), ("invalid", why)))

    def test_unreadable_state_names_the_exception(self):
        self.world()
        with mock.patch.object(batch.fr, "load_state", side_effect=ValueError(SENTINEL)):
            self.assertEqual(self.one(self.configs[0]), ("%s invalid / next=- why=state-unreadable:ValueError" % SLUG,
                                                         ("invalid", "state-unreadable:ValueError")))
        state = self.outs[0] / "REVIEW-STATE.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text('{"schema": 1, "slug": "%s"' % SENTINEL)
        self.assertEqual(self.one(self.configs[0])[1], ("invalid", "state-unreadable:Fail"))

    def test_held_says_which_roster_rule(self):
        self.world()
        head, *rows = base.ROSTER.read_text().splitlines()
        roster = self.tmp / "roster.csv"
        cases = {"roster:no-row": rows[1:], "roster:not-in-scope": [rows[0].replace(",yes,", ",no,", 1)] + rows[1:],
                 "roster:applicability-differs": [rows[0].replace("local ALPR policy applies", SENTINEL, 1)] + rows[1:]}
        for why, body in cases.items():
            with self.subTest(why=why):
                roster.write_text("\n".join([head] + body) + "\n")
                self.listing(*self.configs)
                code, lines = self.main(roster=roster)
                self.assertEqual(code, 0, lines)
                self.assertEqual(lines[:3], ["%s held 2/28 next=inventory why=%s" % (SLUG, why), "%s ready 2/28 next=inventory" % SECOND,
                                             "%s held 2/28 next=inventory why=roster:not-in-scope" % CAMPUS])
                self.assertEqual(self.rows()[SLUG], ("held", why))
                self.assertEqual(self.rows()[SECOND], ("ready", ""))
        self.listing(*self.configs)
        self.assertEqual(self.main(roster=None)[1][:3], ["%s ready 2/28 next=inventory" % s for s in base.SLUGS])  # no roster: never held

    def test_locked_says_which_lock_word(self):
        self.world()
        lock, host = self.lock(), socket.gethostname()
        (self.tmp / "elsewhere").write_text("1 %s\n" % host)
        cases = (("not-regular-file", lambda: lock.symlink_to(self.tmp / "elsewhere")),
                 ("live-writer", lambda: lock.write_text("%d %s\n" % (os.getpid(), host))),
                 ("live-writer", lambda: lock.write_text("%d\n" % os.getpid())),
                 ("other-host", lambda: lock.write_text("4242 %s-%s\n" % (host, SENTINEL))),
                 ("unparseable", lambda: lock.write_text("%s and more words\n" % SENTINEL)))
        for why, make in cases:
            with self.subTest(why=why):
                make()
                line, row = self.one(self.configs[0], roster=base.ROSTER)
                self.assertEqual((line, row), ("%s locked 2/28 next=inventory why=lock:%s" % (SLUG, why), ("locked", "lock:" + why)))
                self.assertNotIn(str(os.getpid()), line)
                self.assertNotIn("4242", line)
                lock.unlink()
        with mock.patch.object(batch.fr, "lock_state", return_value=("held", "a new detail " + SENTINEL)):
            self.assertEqual(self.one(self.configs[0])[1], ("locked", "lock:held"))
        lock.write_text("99999999 %s\n" % host)  # a provably dead same-host writer (above pid_max): stale, not locked
        self.assertEqual(self.one(self.configs[0]), ("%s ready 2/28 next=inventory" % SLUG, ("ready", "")))
        self.assertEqual(self.one(self.configs[2], roster=base.ROSTER)[1], ("held", "roster:not-in-scope"))  # held before locked

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads a mode-000 file")
    def test_unreadable_lock(self):
        self.world()
        lock = self.lock()
        lock.write_text("1\n")
        lock.chmod(0)
        self.addCleanup(lock.chmod, 0o600)
        self.assertEqual(self.one(self.configs[0]), ("%s locked 2/28 next=inventory why=lock:unreadable" % SLUG, ("locked", "lock:unreadable")))

    def test_other_states_unchanged(self):
        self.world(stubs={"inventory": "sys.exit(1)"})
        code, lines = self.main("--run", *self.gate)
        self.assertEqual(code, 0, lines)
        self.assertIn("run %s rc=1 state=failed" % SLUG, lines)
        self.assertIn("run %s rc=3 state=waiting" % SECOND, lines)
        code, lines = self.main()
        self.assertEqual(lines[:3], ["%s failed 2/28 next=inventory" % SLUG, "%s waiting 5/28 next=fact-drafts" % SECOND,
                                     "%s held 2/28 next=inventory why=roster:not-in-scope" % CAMPUS])
        draft = self.outs[1] / "facts" / "drafts" / "a.yaml"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("draft\n")
        self.assertEqual(self.main()[1][1], "%s in-flight 5/28 next=fact-drafts" % SECOND)
        self.assertEqual(list(self.rows().values()), [("failed", ""), ("in-flight", ""), ("held", "roster:not-in-scope")])
        with mock.patch.object(batch.fr, "evaluate", return_value=("done", "")):  # every stage done (a full stub run is not needed)
            self.assertEqual(self.one(self.configs[1]), ("%s done 28/28 next=-" % SECOND, ("done", "")))

    def test_failed_run_clears_why(self):  # a run that fails is `failed` with no why, whatever the re-read said
        self.world()
        self.listing(self.configs[1])
        fake = mock.Mock(returncode=1, stdout=b"")
        with mock.patch.object(batch.subprocess, "run", return_value=fake), \
                mock.patch.object(batch.fr, "lock_state", side_effect=[("none", ""), ("held", "pid 7")]):
            code, lines = self.main("--run", *self.gate)
        self.assertEqual(code, 0, lines)
        self.assertIn("%s failed 2/28 next=inventory" % SECOND, lines)
        self.assertEqual(self.rows()[SECOND], ("failed", ""))

    def test_header_and_prev(self):
        self.world()
        self.assertEqual(self.main()[0], 0)
        written = self.out / "BATCH-STATUS.csv"
        self.assertTrue(written.read_text().splitlines()[0].endswith(",run_date,why"))
        new = self.tmp / "new.csv"
        new.write_text(written.read_text())
        with written.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        old = self.tmp / "old.csv"
        with old.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=OLD, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        for prev in (old, new):
            with self.subTest(prev=prev.name):
                code, lines = self.main(prev=prev)
                self.assertEqual(code, 0, lines)
                self.assertIn("changes advanced=0 regressed=0 state=0 new=0 removed=0 unchanged=3", lines)
        text = written.read_text()
        bad = {"extra": text.replace(",why\n", ",why,extra\n", 1), "moved": text.replace("run_date,why\n", "why,run_date\n", 1),
               "short": text.replace(",run_date,why\n", "\n", 1), "renamed": text.replace(",why\n", ",reason\n", 1)}
        for name, body in bad.items():
            with self.subTest(header=name):
                prev = self.tmp / (name + ".csv")
                prev.write_text(body)
                self.assertEqual(self.main(prev=prev), (1, ["prev: header differs"]))


if __name__ == "__main__":
    unittest.main()
