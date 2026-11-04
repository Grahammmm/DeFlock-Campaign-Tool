"""status --check: private/objects re-hashed against the ledger (RF1230-4).

Synthetic bytes in a temporary campaign only. Run from the repository root:
python3 -m unittest tests.test_store_check -v
"""
import contextlib
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli as cli_module  # noqa: E402
from campaign_tool import store_check  # noqa: E402

FIRST = b"synthetic first object\n"
SECOND = b"synthetic second object, a bit longer\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class StoreCheckTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-store-check-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.umask, os.umask(0o077))  # planted fixtures stay owner-only (RF2010-5 loose_mode)
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.objects = self.root / "private" / "objects"
        self.cli("init", "--name", "Synthetic Campaign", "--county", "Synthetic", "--state", "CA")

    def cli(self, command, *arguments, code=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "campaign_tool", command, "--directory", str(self.root),
             *map(str, arguments)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def ingest(self, data, name, source="synthetic-source"):
        path = self.base / name
        path.write_bytes(data)
        self.cli("ingest", "--file", path, "--source-id", source)

    def ingest_two(self):
        self.ingest(FIRST, "first.txt")
        self.ingest(SECOND, "second.txt")

    def check(self, code):
        result = self.cli("status", "--check", code=code)
        decoder = json.JSONDecoder()
        counts, end = decoder.raw_decode(result.stdout)
        block, _ = decoder.raw_decode(result.stdout[end:].lstrip())
        return counts, block["check"], result

    def problems(self, code=3):
        return self.check(code)[1]["problems"]

    def snapshot(self):
        found = {}
        for path in sorted(self.root.rglob("*")):
            info = os.lstat(path)
            found[str(path.relative_to(self.root))] = (info.st_mode, info.st_size, info.st_mtime_ns)
        return found

    def test_clean_store_exit_zero_and_counts_unchanged(self):
        self.ingest_two()
        plain = self.cli("status").stdout
        counts, check, result = self.check(0)
        self.assertEqual(check, {"checked_objects": 2, "problems": []})
        self.assertTrue(result.stdout.startswith(plain))
        self.assertEqual(counts, json.loads(plain))
        self.assertEqual(counts["stored_objects"], 2)

    def test_missing_file(self):
        self.ingest_two()
        (self.objects / sha(FIRST)).unlink()
        self.assertEqual(self.problems(), [{"sha256": sha(FIRST), "problem": "missing_file"}])

    def test_edited_in_place_same_size_is_hash_mismatch(self):
        self.ingest_two()
        target = self.objects / sha(SECOND)
        data = bytearray(target.read_bytes())
        data[0] ^= 1
        target.write_bytes(bytes(data))
        self.assertEqual(self.problems(), [{"sha256": sha(SECOND), "problem": "hash_mismatch"}])

    def test_truncated_is_size_mismatch_without_hashing(self):
        self.ingest_two()
        (self.objects / sha(FIRST)).write_bytes(FIRST[:5])
        hashed = []
        real = store_check._sha256

        def counting(fd, byte_limit):
            hashed.append(os.fstat(fd).st_ino)
            return real(fd, byte_limit)
        with mock.patch.object(store_check, "_sha256", counting):
            result = store_check.check_store(self.root)
        self.assertEqual(result["problems"], [{"sha256": sha(FIRST), "problem": "size_mismatch",
                                               "expected": len(FIRST), "actual": 5}])
        self.assertEqual(hashed, [os.stat(self.objects / sha(SECOND)).st_ino])
        self.assertEqual(self.problems()[0]["problem"], "size_mismatch")

    def test_orphan_temp_and_unexpected_entries(self):
        self.ingest_two()
        orphan = b"synthetic orphan copied in by hand\n"
        (self.objects / sha(orphan)).write_bytes(orphan)
        (self.objects / ".intake-abc").write_bytes(FIRST)
        (self.objects / "notes.txt").write_text("synthetic", encoding="utf-8")
        (self.objects / "subdir").mkdir()
        (self.objects / sha(FIRST).upper()).write_bytes(FIRST)
        expected = sorted([
            {"name": sha(orphan), "problem": "no_ledger_row"},
            {"name": ".intake-abc", "problem": "leftover_temp"},
            {"name": "notes.txt", "problem": "unexpected_entry"},
            {"name": "subdir", "problem": "unexpected_entry"},
            {"name": sha(FIRST).upper(), "problem": "unexpected_entry"},
        ], key=lambda item: item["name"])
        self.assertEqual(self.problems(), expected)

    def test_symlinked_object_is_never_followed(self):
        self.ingest_two()
        outside = self.base / "outside-copy"
        outside.write_bytes(FIRST)
        target = self.objects / sha(FIRST)
        target.unlink()
        target.symlink_to(outside)
        real_open = open

        def guarded(path, *args, **kwargs):
            if Path(path).resolve() == outside.resolve():
                raise AssertionError("symlink target opened")
            return real_open(path, *args, **kwargs)
        with mock.patch("builtins.open", guarded):
            result = store_check.check_store(self.root)
        self.assertEqual(result["problems"], [{"sha256": sha(FIRST), "problem": "not_a_regular_file"}])
        self.assertEqual(self.problems(), result["problems"])

    def test_directory_in_place_of_object_is_not_a_regular_file(self):
        self.ingest_two()
        target = self.objects / sha(SECOND)
        target.unlink()
        target.mkdir()
        self.assertEqual(self.problems(), [{"sha256": sha(SECOND), "problem": "not_a_regular_file"}])

    def test_symlinked_objects_folder_stops(self):
        self.ingest_two()
        moved = self.base / "objects-elsewhere"
        os.rename(self.objects, moved)
        self.objects.symlink_to(moved, target_is_directory=True)
        result = self.cli("status", "--check", code=1)
        self.assertIn("Stopped: private/objects must not be a symlink", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_symlinked_private_folder_stops(self):
        self.ingest_two()
        moved = self.base / "private-elsewhere"
        os.rename(self.root / "private", moved)
        (self.root / "private").symlink_to(moved, target_is_directory=True)
        result = self.cli("status", "--check", code=1)
        self.assertIn("Stopped: private/objects must not be a symlink", result.stderr)

    def test_fresh_campaign_has_no_problems_and_stays_without_ledger(self):
        counts, check, _ = self.check(0)
        self.assertEqual(check, {"checked_objects": 0, "problems": []})
        self.assertEqual(counts["stored_objects"], 0)
        self.assertFalse((self.root / "private" / "ledger.sqlite").exists())
        self.assertFalse(self.objects.exists())

    def test_run_with_problems_changes_nothing(self):
        self.ingest_two()
        (self.objects / sha(FIRST)).write_bytes(b"x")
        (self.objects / ".intake-left").write_bytes(b"partial")
        ledger = self.root / "private" / "ledger.sqlite"
        before, ledger_bytes = self.snapshot(), ledger.read_bytes()
        self.assertEqual(len(self.problems()), 2)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ledger.read_bytes(), ledger_bytes)

    def test_no_source_id_or_original_name_in_output(self):
        self.ingest(FIRST, "synthetic-name.pdf", source="synthetic-source-xyz")
        (self.objects / ".intake-zz").write_bytes(b"partial")
        (self.objects / sha(FIRST)).write_bytes(FIRST[:3])
        _, _, result = self.check(3)
        for text in ("synthetic-source-xyz", "synthetic-name"):
            self.assertNotIn(text, result.stdout + result.stderr)

    def test_corrupt_ledger_stops(self):
        self.ingest_two()
        (self.root / "private" / "ledger.sqlite").write_bytes(b"synthetic corrupt ledger " * 200)
        result = self.cli("status", "--check", code=1)
        self.assertIn("Stopped: ledger unreadable: ", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_ledger_without_objects_table_is_unreadable(self):
        private = self.root / "private"
        sqlite3.connect(private / "ledger.sqlite").close()
        with self.assertRaisesRegex(ValueError, "^ledger unreadable: OperationalError$"):
            store_check.check_store(self.root)

    def test_invalid_ledger_row_builds_no_path(self):
        self.ingest_two()
        db = sqlite3.connect(self.root / "private" / "ledger.sqlite")
        with db:
            db.execute("INSERT INTO objects VALUES (?, ?, ?)", ("../../campaign.json", 1, "t"))
        db.close()
        with mock.patch.object(store_check.os, "lstat", wraps=os.lstat) as lstat:
            result = store_check.check_store(self.root)
        self.assertEqual(result["problems"],
                         [{"sha256": "../../campaign.json", "problem": "invalid_ledger_row"}])
        self.assertEqual(result["checked_objects"], 3)
        self.assertEqual(lstat.call_count, 4)  # two valid rows: lstat before open and again before accepting; none for the invalid row

    def run_main_in_process(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["campaign_tool", *map(str, arguments)]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli_module.main()
            except SystemExit as stop:
                return stop.code, out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def test_cli_changed_object_refuses_before_counts_and_preserves_tree(self):
        self.ingest_two()
        target = self.objects / sha(FIRST)
        outside = self.base / "outside-target"
        outside.write_bytes(FIRST)
        outside_before = (outside.read_bytes(), os.lstat(outside).st_mode, os.lstat(outside).st_mtime_ns)
        real_open = os.open

        def unreadable(path, *args, **kwargs):
            if Path(path).name == sha(FIRST):
                raise PermissionError(13, "synthetic secret")
            return real_open(path, *args, **kwargs)
        before = self.snapshot()
        with mock.patch.object(store_check.os, "open", unreadable):
            code, out, err = self.run_main_in_process("status", "--check", "--directory", self.root)
        self.assertEqual((code, out, err.strip()), (1, "", "Stopped: " + store_check.UNREADABLE))
        self.assertEqual(self.snapshot(), before)  # no byte, mode or mtime moved
        self.assertEqual(target.read_bytes(), FIRST)

        real_read, real_fstat, state = os.read, os.fstat, []
        target_inode = os.stat(target).st_ino

        def mutating_read(fd, count):
            if not state and real_fstat(fd).st_ino == target_inode:
                state.append(1)
                with open(target, "r+b") as handle:  # same size, different bytes, mtime moved forward
                    handle.write(b"X")
                os.utime(target, ns=(0, os.lstat(target).st_mtime_ns + 10 ** 9))
            return real_read(fd, count)
        with mock.patch.object(store_check.os, "read", mutating_read):
            code, out, err = self.run_main_in_process("status", "--check", "--directory", self.root)
        self.assertEqual((code, out, err.strip()), (1, "", "Stopped: " + store_check.CHANGED))
        self.assertNotIn("Traceback", err)
        self.assertEqual((outside.read_bytes(), os.lstat(outside).st_mode, os.lstat(outside).st_mtime_ns),
                         outside_before)

    def test_cli_stable_clean_is_zero_and_stable_bad_bytes_is_three(self):
        self.ingest_two()
        self.check(0)
        (self.objects / sha(SECOND)).write_bytes(b"x" * len(SECOND))
        self.assertEqual(self.problems(), [{"sha256": sha(SECOND), "problem": "hash_mismatch"}])


RACE_SCRIPT = """
import json, os, socket, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from campaign_tool import store_check
root, target, kind, sha, size = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4], sys.argv[5], int(sys.argv[6])
real, done, hashed = os.lstat, [], []
def swapping(path, *args, **kwargs):
    result = real(path, *args, **kwargs)
    if Path(path) == target and not done:
        done.append(1)
        os.unlink(target)
        if kind == "fifo":
            os.mkfifo(target)
        elif kind == "dir":
            os.mkdir(target)
        else:
            os.chdir(target.parent)
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(target.name)
    return result
os.lstat = swapping
real_sha = store_check._sha256
store_check._sha256 = lambda *args: hashed.append(1) or real_sha(*args)
print(json.dumps([store_check.check_store(root, rows=[(sha, size)])["problems"], len(hashed)]))
"""


class DescriptorBoundTests(unittest.TestCase):
    """Race, bound and error behaviour of the descriptor-bound object check (RF0220-4); no ledger, no CLI."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-store-race-")
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.umask, os.umask(0o077))
        self.base = Path(temporary.name)
        self.root = self.base / "campaign"
        self.objects = self.root / "private" / "objects"
        self.objects.mkdir(parents=True, mode=0o700)
        self.rows = []
        self.opened, self.closed, self.reads, self.opened_paths = [], [], [], []

    def put(self, data):
        path = self.objects / sha(data)
        self.force(path, data)
        self.rows.append((sha(data), len(data)))
        return path

    @staticmethod
    def force(path, data):
        """Make ``path`` a plain regular file holding ``data``, whatever it was."""
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
        path.write_bytes(data)

    def fresh(self):
        for path in self.objects.iterdir():
            path.unlink()
        self.rows.clear()
        for spy in (self.opened, self.closed, self.reads, self.opened_paths):
            spy.clear()

    def check(self):
        return store_check.check_store(self.root, rows=self.rows)["problems"]

    def outside(self, data):
        path = self.base / "outside-sentinel"
        path.write_bytes(data)
        return path

    @contextlib.contextmanager
    def watch(self, fail=None, on_read=None, before=None, after=None):
        """Spy on object opens, closes, reads and fstats. ``before``/``after``: {(function, call number): action}
        run around that call (lstat/open on object paths, read/fstat on object descriptors); ``fail``:
        {(function, call number): exception raised instead}; ``on_read``: runs before the first object read."""
        real = {name: getattr(os, name) for name in ("lstat", "open", "close", "read", "fstat")}
        calls = {}
        before, after, fail = before or {}, after or {}, fail or {}

        def step(name):
            calls[name] = calls.get(name, 0) + 1
            key = (name, calls[name])
            if key in fail:
                raise fail[key]
            if key in before:
                before[key]()
            return key

        def lstat(path, *args, **kwargs):
            if Path(path).parent != self.objects:
                return real["lstat"](path, *args, **kwargs)
            key = step("lstat")
            result = real["lstat"](path, *args, **kwargs)
            if key in after:
                after[key]()
            return result

        def open_(path, flags, *args, **kwargs):
            if Path(path).parent != self.objects:
                return real["open"](path, flags, *args, **kwargs)
            self.opened_paths.append(Path(path))
            key = step("open")
            fd = real["open"](path, flags, *args, **kwargs)
            self.opened.append(fd)
            if key in after:
                after[key]()
            return fd

        def close(fd):
            if fd in self.opened:
                self.closed.append(fd)
            return real["close"](fd)

        def read(fd, count):
            if fd not in self.opened:
                return real["read"](fd, count)
            self.reads.append(count)
            key = step("read")
            if on_read and key == ("read", 1):
                on_read()
            data = real["read"](fd, count)
            if key in after:
                after[key]()
            return data

        def fstat(fd):
            if fd in self.opened:
                step("fstat")
            return real["fstat"](fd)

        with contextlib.ExitStack() as stack:
            for name, function in (("lstat", lstat), ("open", open_), ("close", close), ("read", read),
                                   ("fstat", fstat)):
                stack.enter_context(mock.patch.object(store_check.os, name, function))
            yield

    def assert_fds_closed_once(self):
        self.assertEqual(sorted(self.closed), sorted(self.opened))

    def replace_with(self, target, data):
        replacement = target.with_name(".replacement")
        replacement.write_bytes(data)
        os.replace(replacement, target)

    def bump_mtime(self, target):
        os.utime(target, ns=(0, os.lstat(target).st_mtime_ns + 10 ** 9))

    # --- symlink and special-file swaps after the lstat ---

    def test_object_swap_to_symlink_after_lstat_never_reads_target(self):
        outside = self.outside(FIRST)  # carries the expected bytes on purpose: the old code reported a clean store
        target = self.put(FIRST)
        outside_inode, real_fstat, real_read, touched = os.stat(outside).st_ino, os.fstat, os.read, []

        def spying_read(fd, count):
            if real_fstat(fd).st_ino == outside_inode:
                touched.append(fd)
            return real_read(fd, count)

        def swap():
            target.unlink()
            target.symlink_to(outside)
        for label, hooks in (("after lstat", {"after": {("lstat", 1): swap}}),
                             ("just before open", {"before": {("open", 1): swap}})):
            with self.subTest(label):
                self.force(target, FIRST)
                with mock.patch.object(store_check.os, "read", spying_read), self.watch(**hooks):
                    problems = self.check()
                self.assertEqual(problems, [{"sha256": sha(FIRST), "problem": "not_a_regular_file"}])
                self.assertEqual(touched, [])
                self.assertEqual(outside.read_bytes(), FIRST)
        self.assert_fds_closed_once()

    def test_object_swap_to_dangling_symlink_is_not_a_regular_file(self):
        target = self.put(FIRST)

        def swap():
            target.unlink()
            target.symlink_to(self.base / "does-not-exist")
        with self.watch(after={("lstat", 1): swap}):
            self.assertEqual(self.check(), [{"sha256": sha(FIRST), "problem": "not_a_regular_file"}])

    def test_object_swap_to_fifo_or_directory_never_hashes_or_blocks(self):
        script = self.base / "race.py"
        script.write_text(RACE_SCRIPT, encoding="utf-8")
        kinds = ["fifo", "dir"] + (["socket"] if hasattr(os, "mkfifo") and sys.platform != "win32" else [])
        for kind in kinds:
            with self.subTest(kind):
                root = self.base / ("campaign-" + kind)
                objects = root / "private" / "objects"
                objects.mkdir(parents=True, mode=0o700)
                (objects / sha(FIRST)).write_bytes(FIRST)
                done = subprocess.run(
                    [sys.executable, "-B", str(script), str(REPO_ROOT), str(root), str(objects / sha(FIRST)),
                     kind, sha(FIRST), str(len(FIRST))],
                    capture_output=True, text=True, timeout=20, cwd=self.base)  # a blocked FIFO open times out
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(json.loads(done.stdout),
                                 [[{"sha256": sha(FIRST), "problem": "not_a_regular_file"}], 0])

    # --- disappearance ---

    def test_object_disappears_before_open_is_missing_and_scan_continues(self):
        target = self.put(FIRST)
        self.put(SECOND)
        with self.watch(before={("open", 1): target.unlink}):
            problems = self.check()
        self.assertEqual(problems, [{"sha256": sha(FIRST), "problem": "missing_file"}])
        self.assertEqual(len(self.opened), 1)  # the other object was still opened and verified
        self.assert_fds_closed_once()

    def test_object_disappears_after_hashing_is_missing_by_final_check(self):
        target = self.put(FIRST)
        with self.watch(before={("lstat", 2): target.unlink}):
            problems = self.check()
        self.assertEqual(problems, [{"sha256": sha(FIRST), "problem": "missing_file"}])
        self.assert_fds_closed_once()

    def test_final_path_replaced_by_symlink_or_directory_is_not_a_regular_file(self):
        target = self.put(FIRST)
        outside = self.outside(FIRST)
        for label, make in (("symlink", lambda: target.symlink_to(outside)), ("directory", target.mkdir)):
            with self.subTest(label):
                self.force(target, FIRST)

                def swap():
                    target.unlink()
                    make()
                with self.watch(before={("lstat", 2): swap}):
                    problems = self.check()
                self.assertEqual(problems, [{"sha256": sha(FIRST), "problem": "not_a_regular_file"}])
        self.assert_fds_closed_once()

    # --- descriptor size recheck ---

    def test_fd_size_recheck_skips_hashing_replacement(self):
        target = self.put(FIRST)
        other = self.put(SECOND)
        real, hashed = store_check._sha256, []

        def counting(fd, limit):
            hashed.append(os.fstat(fd).st_ino)
            return real(fd, limit)
        for label, data in (("shorter", FIRST[:4]), ("longer", FIRST + b"extra")):
            with self.subTest(label):
                self.force(target, FIRST)
                hashed.clear()
                with mock.patch.object(store_check, "_sha256", counting), \
                        self.watch(before={("open", 1): lambda data=data: self.replace_with(target, data)}):
                    problems = self.check()
                self.assertEqual(problems, [{"sha256": sha(FIRST), "problem": "size_mismatch",
                                             "expected": len(FIRST), "actual": len(data)}])
                self.assertEqual(hashed, [os.stat(other).st_ino])
        self.assert_fds_closed_once()

    def test_stable_replacement_with_same_size_and_bytes_before_open_is_accepted(self):
        target = self.put(FIRST)
        with self.watch(before={("open", 1): lambda: self.replace_with(target, FIRST)}):
            self.assertEqual(self.check(), [])

    # --- bounded, chunked reads ---

    def test_hash_reads_are_chunked_and_capped_after_growth(self):
        exact = b"0123456789abcdefghijklmn"  # 24 bytes: three whole chunks of 8

        def grow():
            with open(self.objects / sha(exact), "ab") as handle:
                handle.write(b"G" * 100)

        def shrink():
            os.truncate(self.objects / sha(exact), 5)
        with mock.patch.object(store_check, "CHUNK", 8):
            with self.subTest("empty object"):
                self.put(b"")
                with self.watch():
                    self.assertEqual(self.check(), [])
                self.assertEqual(self.reads, [1])  # only the EOF-detection byte
                self.assert_fds_closed_once()
            with self.subTest("exact multi-chunk object"):
                self.fresh()
                self.put(exact)
                with self.watch():
                    self.assertEqual(self.check(), [])
                self.assertEqual(self.reads, [8, 8, 8, 1])
                self.assert_fds_closed_once()
            for label, mutate in (("growth after fstat", grow), ("early EOF", shrink)):
                with self.subTest(label):
                    self.fresh()
                    self.put(exact)
                    with self.watch(on_read=mutate), self.assertRaises(ValueError) as caught:
                        self.check()
                    self.assertEqual(str(caught.exception), store_check.CHANGED)
                    self.assertLessEqual(sum(self.reads), len(exact) + 1)
                    self.assertLessEqual(max(self.reads), 8)
                    self.assert_fds_closed_once()

    # --- observed change refusals ---

    def test_observed_mutation_or_final_inode_swap_refuses_clean_result(self):
        target = self.put(FIRST)

        def same_size_edit():
            with open(target, "r+b") as handle:
                handle.write(b"X")
            self.bump_mtime(target)

        def append():
            with open(target, "ab") as handle:
                handle.write(b"more")
        cases = (("same-size edit while hashing", {"on_read": same_size_edit}),
                 ("append while hashing", {"on_read": append}),
                 ("truncate while hashing", {"on_read": lambda: os.truncate(target, 3)}),
                 ("matching-byte replacement before final lstat",
                  {"before": {("lstat", 2): lambda: self.replace_with(target, FIRST)}}),
                 ("metadata change before final lstat", {"before": {("lstat", 2): lambda: self.bump_mtime(target)}}))
        for label, hooks in cases:
            with self.subTest(label):
                self.force(target, FIRST)
                with self.watch(**hooks), self.assertRaises(ValueError) as caught:
                    self.check()
                self.assertEqual(str(caught.exception), store_check.CHANGED)
        self.assert_fds_closed_once()

    def test_stable_same_size_bad_bytes_stay_hash_mismatch(self):
        target = self.put(FIRST)
        target.write_bytes(b"X" + FIRST[1:])
        with self.watch():
            self.assertEqual(self.check(), [{"sha256": sha(FIRST), "problem": "hash_mismatch"}])
        self.assert_fds_closed_once()

    # --- unreadable objects ---

    def test_object_io_error_refusal_is_private_and_closes_fd(self):
        self.put(FIRST)
        secret = "/private/secret-name-%s synthetic-source-id" % sha(FIRST)
        cases = (("lstat", ("lstat", 1)), ("open", ("open", 1)), ("fstat", ("fstat", 1)),
                 ("read", ("read", 1)), ("fstat after hashing", ("fstat", 2)), ("final lstat", ("lstat", 2)))
        for label, key in cases:
            for error in (PermissionError(13, secret), OSError(5, secret)):
                with self.subTest(label, error=type(error).__name__):
                    self.opened.clear()
                    self.closed.clear()
                    with self.watch(fail={key: error}), self.assertRaises(ValueError) as caught:
                        self.check()
                    exc = caught.exception
                    self.assertEqual(str(exc), store_check.UNREADABLE)
                    self.assertTrue(exc.__suppress_context__)
                    self.assertIsNone(exc.__cause__)
                    shown = repr(exc.args) + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    for text in ("secret", "synthetic-source-id", sha(FIRST), str(self.base)):
                        self.assertNotIn(text, shown)
                    self.assert_fds_closed_once()

    def test_open_errors_enoent_eloop_and_enxio_map_to_findings(self):
        self.put(FIRST)
        for error, problem in ((FileNotFoundError(2, "gone"), "missing_file"),
                               (OSError(40, "too many levels"), "not_a_regular_file"),
                               (OSError(6, "no such device or address"), "not_a_regular_file")):
            with self.subTest(errno=error.errno), self.watch(fail={("open", 1): error}):
                self.assertEqual(self.check(), [{"sha256": sha(FIRST), "problem": problem}])

    # --- descriptor hygiene ---

    def test_check_error_and_short_read_do_not_leak_descriptors(self):
        first = self.put(FIRST)
        second = self.put(SECOND)
        rows = list(self.rows)
        cases = (("clean", lambda: None, {}),
                 ("hash mismatch", lambda: first.write_bytes(b"X" + FIRST[1:]), {}),
                 ("nonregular", lambda: (first.unlink(), first.mkdir()), {}),
                 ("early EOF", lambda: None, {"on_read": lambda: os.truncate(second, 2)}),
                 ("read error", lambda: None, {"fail": {("read", 1): OSError(5, "synthetic")}}))
        for label, prepare, hooks in cases:
            with self.subTest(label):
                self.force(first, FIRST)
                self.force(second, SECOND)
                prepare()
                self.opened.clear()
                self.closed.clear()
                try:
                    with self.watch(**hooks):
                        self.check()
                except ValueError:
                    pass
                self.assert_fds_closed_once()
                self.assertEqual(self.rows, rows)  # the caller's rows are never changed
                self.assertTrue(all(path.parent == self.objects for path in self.opened_paths))  # no receipts opened


if __name__ == "__main__":
    unittest.main()
