import contextlib, importlib.util, io, os, socket, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch

from tests.flock.test_review import ROOT, TOOL, make_review, tree


def load(name, path):  # the tools are loaded by file path, as test_review does
    spec = importlib.util.spec_from_file_location(name, path)
    spec.loader.exec_module(module := importlib.util.module_from_spec(spec))
    return module


review, batch = load("_rf1058_review", TOOL), load("_rf1058_batch", ROOT / "tools" / "flock_review_batch.py")
HOST, STAMP = socket.gethostname(), "20261031T000000Z"


def dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class ReviewLockTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.config, self.paths = make_review(self.tmp)
        self.dir = self.paths["out"] / ".review"
        self.dir.mkdir(mode=0o700, parents=True)
        self.lock = self.dir / "LOCK"

    def plant(self, text, age=0):
        self.lock.write_text(text)
        if age:
            os.utime(self.lock, (time.time() - age, time.time() - age))
        return self.lock.read_bytes(), os.lstat(self.lock).st_ino

    def main(self, *words):  # in-process, so patches of os.kill / os.open / now apply
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            code = review.main(list(words) or ["run", str(self.config), "--until", "inventory"])
        return code, shown.getvalue()

    def asides(self):
        return sorted(self.dir.glob("stale-LOCK-*"))

    def assertRefused(self, planted, code, out):
        self.assertEqual((code, out), (1, "locked: %s\n" % self.lock))
        self.assertEqual((self.lock.read_bytes(), os.lstat(self.lock).st_ino), planted)
        self.assertEqual(self.asides(), [])

    def assertMoved(self, planted, why, code, out):
        self.assertEqual(code, 0, out)
        self.assertTrue(out.startswith("lock: stale (%s), moved aside\nstage inventory done rc=0" % why), out)
        self.assertEqual([p.read_bytes() for p in self.asides()], [planted[0]])
        self.assertFalse(os.path.lexists(self.lock))

    def test_free_lock_holds_pid_and_host_then_goes(self):
        seen, real = [], review.execute
        def spy(c, i, state):
            seen.append(self.lock.read_text())
            return real(c, i, state)
        with patch.object(review, "execute", spy):
            code, out = self.main()
        self.assertEqual((code, seen), (0, ["%d %s\n" % (os.getpid(), HOST)]), out)
        self.assertEqual((os.path.lexists(self.lock), self.asides()), (False, []))

    def test_dead_same_host_pid_is_moved_aside(self):
        pid = dead_pid()
        self.assertMoved(self.plant("%d %s\n" % (pid, HOST)), "pid %d not running" % pid, *self.main())

    def test_legacy_one_field_dead_pid_is_this_host(self):
        pid = dead_pid()
        self.assertMoved(self.plant("%d\n" % pid), "pid %d not running" % pid, *self.main())

    def test_alive_pid_refused(self):
        self.assertRefused(self.plant("%d %s\n" % (os.getpid(), HOST)), *self.main())

    def test_foreign_host_refused(self):
        self.assertRefused(self.plant("%d other-host\n" % dead_pid()), *self.main())

    def test_kill_errors_other_than_no_such_process_refused(self):
        planted = self.plant("%d %s\n" % (dead_pid(), HOST))
        for exc in (PermissionError(1, "denied"), OSError(22, "invalid"), OverflowError("too big")):
            with self.subTest(exc=type(exc).__name__), patch.object(review.os, "kill", side_effect=exc):
                self.assertRefused(planted, *self.main())

    def test_unparseable_lock_moved_only_when_old(self):
        for text in ("", "0\n", "-1 %s\n" % HOST, "abc\n", "%d %s extra\n" % (dead_pid(), HOST)):
            with self.subTest(text=text):
                self.assertRefused(self.plant(text), *self.main())
        self.assertRefused(self.plant("", age=590), *self.main())
        self.assertMoved(self.plant("", age=601), "unparseable, older than 600 s", *self.main())

    def test_symlink_lock_refused_and_untouched(self):
        target, text = self.tmp / "elsewhere", "%d %s\n" % (dead_pid(), HOST)
        target.write_text(text)
        self.lock.symlink_to(target)
        code, out = self.main()
        self.assertEqual((code, out), (1, "locked: %s\n" % self.lock))
        self.assertEqual((os.readlink(self.lock), target.read_text(), self.asides()), (str(target), text, []))
        self.assertEqual(review.lock_state(review.context(self.config)), ("held", "not a regular file"))

    def test_new_lock_before_retry_refused_once_moved(self):
        pid, real, opened = dead_pid(), os.open, []
        planted = self.plant("%d %s\n" % (pid, HOST))
        def racy(path, flags, *rest):
            if not flags & os.O_EXCL:
                return real(path, flags, *rest)
            opened.append(path)
            if len(opened) == 2:  # another run took the free LOCK between the move and the retry
                Path(path).write_text("%d %s\n" % (os.getpid(), HOST))
            raise FileExistsError(path)
        with patch.object(review.os, "open", racy):
            code, out = self.main()
        self.assertEqual((code, out, len(opened)), (1, "lock: stale (pid %d not running), moved aside\nlocked: %s\n" % (pid, self.lock), 2))
        self.assertEqual([p.read_bytes() for p in self.asides()], [planted[0]])
        self.assertEqual(self.lock.read_text(), "%d %s\n" % (os.getpid(), HOST))

    def test_aside_name_collision_gets_pid_suffix_never_overwrites(self):
        older = self.dir / ("stale-LOCK-" + STAMP)
        older.write_bytes(b"older\n")
        planted = self.plant("%d %s\n" % (dead_pid(), HOST))
        with patch.object(review, "now", return_value=STAMP):
            code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertEqual([(p.name, p.read_bytes()) for p in self.asides()],
                         [(older.name, b"older\n"), ("%s-%d" % (older.name, os.getpid()), planted[0])])
        planted, before = self.plant("%d %s\n" % (dead_pid(), HOST)), [p.read_bytes() for p in self.asides()]
        with patch.object(review, "now", return_value=STAMP):
            code, out = self.main()
        self.assertEqual((code, out), (1, "locked: %s\n" % self.lock))  # both names taken: refused, nothing renamed over
        self.assertEqual(((self.lock.read_bytes(), os.lstat(self.lock).st_ino), [p.read_bytes() for p in self.asides()]), (planted, before))

    def test_status_reports_stale_and_held_read_only(self):
        for text, word in (("%d %s\n" % (dead_pid(), HOST), "stale"), ("%d %s\n" % (os.getpid(), HOST), "yes")):
            with self.subTest(word=word):
                self.plant(text)
                listing = tree(self.paths["out"])
                code, out = self.main("status", str(self.config))
                self.assertEqual(code, 0, out)
                self.assertTrue(out.endswith(" locked=%s\n" % word), out)
                self.assertEqual((tree(self.paths["out"]), self.asides()), (listing, []))

    def test_batch_runs_stale_agency_and_holds_held_one(self):
        c = batch.fr.context(self.config)
        for text, word in (("%d %s\n" % (dead_pid(), HOST), "ready"), ("%d %s\n" % (os.getpid(), HOST), "locked"),
                           ("%d other-host\n" % dead_pid(), "locked")):
            with self.subTest(word=word, text=text):
                planted = self.plant(text)
                self.assertEqual(batch.agency(c, None)["state"], word)
                self.assertEqual(((self.lock.read_bytes(), os.lstat(self.lock).st_ino), self.asides()), (planted, []))


if __name__ == "__main__":
    unittest.main()
