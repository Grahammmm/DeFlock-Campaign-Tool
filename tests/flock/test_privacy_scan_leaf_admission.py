"""RF0511-2: flock_privacy_scan admits a leaf on its nonblocking descriptor before reading; substituted leaves are unreadable."""
import contextlib, importlib.util, io, json, os, stat, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parents[2] / "tools" / "flock_privacy_scan.py"
spec = importlib.util.spec_from_file_location("_leaf_admission_scan", TOOL)
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)
EMAIL = "alice@example.invalid"

CHILD = '''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("s", sys.argv[1]); s = importlib.util.module_from_spec(spec); spec.loader.exec_module(s)
leaf, marker = sys.argv[2], sys.argv[3]
real = os.open
def hook(path, flags, *a, **k):
    if os.fspath(path) == leaf:
        os.unlink(leaf); os.mkfifo(leaf)
        open(marker, "w").write("reached")
    return real(path, flags, *a, **k)
os.open = hook
sys.exit(s.main(sys.argv[4:]))
'''


def run_main(args, swap=None):
    """Run scan.main in-process; swap[path]() runs immediately before the production open of that leaf."""
    real_open, out, calls = os.open, io.StringIO(), {"hook": 0}

    def hook(path, flags, *a, **k):
        if swap and os.fspath(path) in swap:
            calls["hook"] += 1
            swap[os.fspath(path)]()
        return real_open(path, flags, *a, **k)

    with mock.patch.object(os, "open", hook), contextlib.redirect_stdout(out):
        code = scan.main(args)
    return code, out.getvalue(), calls["hook"]


class Spy:
    """Wraps a real stream and records read sizes."""
    reads = []

    def __init__(self, fh):
        self.fh = fh

    def read(self, n):
        Spy.reads.append(n)
        return self.fh.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fh.close()


class LeafAdmission(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.scanned = self.root / "scanned"
        self.scanned.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()
        Spy.reads = []

    def leaf(self, name, text="clean\n"):
        p = self.scanned / name
        if p.is_symlink():
            p.unlink()
        p.write_text(text)
        return str(p)

    def test_regular_to_fifo_swap_returns_without_writer(self):
        leaf = self.leaf("a.txt")
        helper, marker = self.root / "child.py", self.root / "marker"
        helper.write_text(CHILD)
        proc = subprocess.run([sys.executable, "-B", str(helper), str(TOOL), leaf, str(marker), leaf],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(marker.read_text(), "reached")
        self.assertEqual(proc.returncode, 1)
        self.assertIn(f"{leaf}: unreadable", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertIn("files=1 scanned=0 skipped=0 findings=0", proc.stdout)

    def test_regular_to_regular_and_symlink_swaps_are_refused(self):
        secret = self.root / "secret.txt"
        secret.write_text(f"{EMAIL} PRIVATE-CANARY\n")

        def replace_regular(p):
            other = self.root / "repl"
            other.write_text(f"{EMAIL}\n")
            os.replace(other, p)

        def replace_symlink(p):
            os.unlink(p)
            os.symlink(secret, p)

        for swap in (replace_regular, replace_symlink):
            for folder_mode in (False, True):
                with self.subTest(swap=swap.__name__, folder=folder_mode):
                    leaf = self.leaf("b.txt", "clean\n")
                    code, out, hooks = run_main([str(self.scanned) if folder_mode else leaf], {leaf: lambda: swap(leaf)})
                    self.assertEqual(hooks, 1)
                    self.assertEqual(code, 1)
                    self.assertIn(f"{leaf}: unreadable", out)
                    self.assertIn("scanned=0 skipped=0 findings=0", out)
                    self.assertNotIn("PRIVATE-CANARY", out)
                    self.assertNotIn(": email", out)

    def test_post_open_type_and_size_admission_precedes_read(self):
        leaf = self.leaf("c.txt", "abcd")
        real_fstat, real_fdopen = os.fstat, os.fdopen
        spy_fdopen = lambda fd, m: Spy(real_fdopen(fd, m))

        def run(fstat):
            Spy.reads = []
            with mock.patch.object(os, "fstat", fstat), mock.patch.object(os, "fdopen", spy_fdopen):
                return run_main([leaf, "--max-bytes", "4"])

        code, out, _ = run(real_fstat)  # exactly at the cap: accepted, one cap+1 read
        self.assertEqual((code, Spy.reads), (0, [5]))
        self.assertIn("scanned=1", out)

        def grown(fd):  # same inode, size beyond the cap seen on the descriptor
            r = real_fstat(fd)
            return os.stat_result((r.st_mode, r.st_ino, r.st_dev, r.st_nlink, r.st_uid, r.st_gid, 99, 0, 0, 0))
        code, out, _ = run(grown)
        self.assertEqual(Spy.reads, [])
        self.assertIn(f"{leaf}: skipped (size)", out)
        self.assertIn("skipped=1", out)

        def nonreg(fd):
            return os.stat_result((stat.S_IFIFO | 0o600,) + tuple(real_fstat(fd))[1:])
        code, out, _ = run(nonreg)
        self.assertEqual((code, Spy.reads), (1, []))
        self.assertIn(f"{leaf}: unreadable", out)

        # growth after admission is still bounded by the cap+1 read
        Spy.reads = []
        def short_fstat(fd):
            r = real_fstat(fd)
            return os.stat_result((r.st_mode, r.st_ino, r.st_dev, r.st_nlink, r.st_uid, r.st_gid, 1, 0, 0, 0))
        Path(leaf).write_text("abcde")
        with mock.patch.object(os, "fstat", short_fstat):
            code, out, _ = run_main([leaf, "--max-bytes", "4"])
        self.assertIn(f"{leaf}: skipped (size)", out)

    def test_unreadable_leaf_does_not_hide_later_findings_or_receipt(self):
        first = self.leaf("a.txt")
        self.leaf("b.txt", "nothing\n")
        self.leaf("c.txt", f"x\n{EMAIL}\n")
        secret = self.root / "s.txt"
        secret.write_text("PRIVATE-CANARY\n")

        def swap():
            os.unlink(first)
            os.symlink(secret, first)
        receipt = self.out / "r.json"
        code, text, _ = run_main([str(self.scanned), "--json-out", str(receipt)], {first: swap})
        self.assertEqual(code, 1)
        self.assertIn(f"{first}: unreadable", text)
        self.assertIn(f"{self.scanned}/c.txt:2: email", text)
        self.assertIn("files=3 scanned=2 skipped=0 findings=1", text)
        self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)
        raw = receipt.read_text()
        self.assertNotIn("PRIVATE-CANARY", raw)
        self.assertNotIn(EMAIL, raw)
        doc = json.loads(raw)
        self.assertTrue(doc["totals"]["failed"])
        recs = {Path(r["path"]).name: r for r in doc["files"]}
        self.assertEqual((recs["a.txt"]["outcome"], recs["a.txt"]["sha256"], recs["a.txt"]["findings"]), ("unreadable", None, []))
        self.assertEqual(recs["c.txt"]["findings"], [{"line": 2, "category": "email"}])
        self.assertEqual(doc["totals"]["scanned"], 2)

    def test_acquisition_and_stream_failures_release_their_owner(self):
        leaf = self.leaf("d.txt")
        real_open, real_close, real_fdopen = os.open, os.close, os.fdopen
        opened, closed = [], []

        def spy_open(p, f, *a, **k):
            fd = real_open(p, f, *a, **k)
            if os.fspath(p) == leaf:
                opened.append(fd)
            return fd

        def spy_close(fd):
            closed.append(fd)
            return real_close(fd)

        def alive(fd):
            try:
                os.fstat(fd)
                return True
            except OSError:
                return False

        for name, fault in (("fstat", mock.patch.object(os, "fstat", side_effect=OSError("x"))),
                            ("fdopen", mock.patch.object(os, "fdopen", side_effect=OSError("x")))):
            with self.subTest(name), mock.patch.object(os, "open", spy_open), fault:
                opened.clear()
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = scan.main([leaf])
                self.assertEqual(code, 1)
                self.assertEqual(out.getvalue().splitlines()[0], f"{leaf}: unreadable")
                self.assertEqual(len(opened), 1)
                self.assertFalse(alive(opened[0]))
        with self.subTest("unexpected"), mock.patch.object(os, "open", spy_open), \
                mock.patch.object(os, "fdopen", side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            opened.clear()
            with self.assertRaises(KeyboardInterrupt):
                scan.main([leaf])
            self.assertFalse(alive(opened[0]))

        class Bad(Spy):
            def read(self, n):
                raise OSError("x")
        with self.subTest("read"), mock.patch.object(os, "open", spy_open), mock.patch.object(os, "close", spy_close), \
                mock.patch.object(os, "fdopen", lambda fd, m: Bad(real_fdopen(fd, m))), contextlib.redirect_stdout(io.StringIO()):
            opened.clear()
            closed.clear()
            code = scan.main([leaf])
            self.assertEqual(code, 1)
            self.assertEqual(closed.count(opened[0]), 0)  # the stream owns it; no raw second close
            self.assertFalse(alive(opened[0]))

    def test_allow_size_binary_category_and_read_only_compatibility(self):
        allow = self.root / "allow.txt"
        allow.write_text(f"{EMAIL}\n")
        self.leaf("a.txt", f"{EMAIL.upper()}\n")
        self.leaf("z.txt", "bob@example.invalid\n")
        (self.scanned / "bin.dat").write_bytes(b"ab\0cd")
        (self.scanned / "big.txt").write_text("x" * 50)
        (self.scanned / ".hidden").mkdir()
        (self.scanned / ".hidden" / "h.txt").write_text(f"{EMAIL}\n")
        os.link(allow, self.scanned / "alias.txt")
        snap = lambda: sorted((str(p), p.stat().st_mtime_ns, p.stat().st_mode) for p in self.root.rglob("*") if self.out not in (p, *p.parents))
        before = snap()
        receipt = self.out / "r.json"
        code, out, _ = run_main([str(self.scanned), "--allow", str(allow), "--max-bytes", "40", "--json-out", str(receipt)])
        self.assertEqual(code, 3)
        lines = out.splitlines()
        self.assertEqual(lines, [f"{self.scanned}/alias.txt: skipped (allow)", f"{self.scanned}/big.txt: skipped (size)",
                                 f"{self.scanned}/bin.dat: skipped (binary)", f"{self.scanned}/z.txt:1: email",
                                 "files=5 scanned=2 skipped=3 findings=1 allowed=1"])
        paths = [r["path"] for r in json.loads(receipt.read_text())["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(before, snap())


if __name__ == "__main__":
    unittest.main()
