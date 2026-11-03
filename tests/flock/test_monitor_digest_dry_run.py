"""RF1100-11: digest --dry-run prints the would-be digest and leads; no file, lock, folder or LEADS.csv change."""
import contextlib
import csv
import fcntl
import importlib.util
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


monitor = load("_test_digest_dry_run_monitor", ROOT / "tools" / "flock_monitor.py")
builders = load("_test_digest_dry_run_leads", ROOT / "tests" / "flock" / "test_monitor_leads.py")  # store builders reused
item, DAY1, D1, A = builders.item, builders.DAY1, builders.D1, builders.A
TRAILER = "dry run | would write digest/%s.md | would add %d leads | nothing written"


class DigestDryRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp, self.store = Path(tmp.name), Path(tmp.name) / "store"
        self.store.mkdir(mode=0o700)

    def write_csv(self, name, header, rows, store=None):
        with open((store or self.store) / name, "w", encoding="utf-8", newline="") as out:
            writer = csv.DictWriter(out, header, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = monitor.main(list(args))
        return code, out.getvalue()

    def dry(self, store=None, *extra):
        return self.run_main("digest", "--store", str(store or self.store), "--date", D1, "--dry-run", *extra)

    def snapshot(self, root=None):  # every path under root (and root itself): kind, bytes, mode and mtime
        root, seen = Path(root or self.tmp), {}
        for path in [root, *sorted(root.rglob("*"))]:
            st = os.lstat(path)
            body = os.readlink(path) if path.is_symlink() else path.read_bytes() if path.is_file() else None
            seen[str(path.relative_to(root))] = (st.st_mode, st.st_mtime_ns, st.st_size, body)
        return seen

    def real_on_copy(self):  # the real digest on a copy of the store: (digest text, copy)
        copy = self.tmp / "copy"
        shutil.copytree(self.store, copy, symlinks=True)
        code, out = self.run_main("digest", "--store", str(copy), "--date", D1)
        self.assertEqual(code, 0, out)
        return (copy / "digest" / (D1 + ".md")).read_text(encoding="utf-8"), copy

    def test_dry_run_equals_real_digest_and_store_untouched(self):
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        log = [dict(scan_date=d, agency="Zed Town", url="https://www.zed.example/a", outcome="refused", reason="http 404")
               for d in ("2026-10-28", "2026-10-29", D1)]
        self.write_csv("SCAN-LOG.csv", monitor.LOG_HEADER, log)
        (self.store / "digest").mkdir()
        before = self.snapshot()
        code, out = self.dry()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.snapshot(), before)
        text, copy = self.real_on_copy()
        self.assertEqual(out, text + TRAILER % (D1, 2) + "\n")
        self.assertIn("Leads (2 new)", out)
        self.assertIn("Unreachable (1 pages, last 3 scans refused)", out)
        with open(copy / "LEADS.csv", encoding="utf-8", newline="") as handle:
            self.assertEqual(len(list(csv.reader(handle))), 3)  # the real run added the 2 leads the dry run announced

    def test_one_relevant_already_a_lead_would_add_one(self):
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        self.write_csv("LEADS.csv", monitor.LEADS_HEADER, [dict(item_id=A, agency="Example City", first_seen=D1,
                                                                state="requested", state_at=D1, state_by="Organizer A")])
        leads = (self.store / "LEADS.csv").read_bytes()
        before = self.snapshot()
        code, out = self.dry()
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], TRAILER % (D1, 1))
        self.assertEqual((self.store / "LEADS.csv").read_bytes(), leads)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(out, self.real_on_copy()[0] + TRAILER % (D1, 1) + "\n")

    def test_no_digest_folder_no_leads_no_lock_created(self):
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        code, out = self.dry()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), ["MONITOR.csv"])
        self.assertEqual(out.splitlines()[0], "Flock monitor 2026-10-30: 5 new items (public, unreviewed), 2 relevant")
        self.assertEqual(out.splitlines()[-1], TRAILER % (D1, 2))

    def test_missing_store_prints_and_creates_nothing(self):
        before = self.snapshot()
        code, out = self.dry(self.tmp / "absent")
        self.assertEqual((code, out), (0, "Flock monitor 2026-10-30: no new items\n" + TRAILER % (D1, 0) + "\n"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.tmp / "absent").exists())

    def test_held_lock_does_not_block_a_dry_run(self):  # no lock taken, so a running scan never makes it fail
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        fd = os.open(self.store / ".monitor.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(self.dry()[0], 0)
        self.assertEqual(self.run_main("digest", "--store", str(self.store), "--date", D1),
                         (1, "store busy: another monitor command is running\n"))

    def refused(self, expect):
        before = self.snapshot()
        code, out = self.dry()
        self.assertEqual((code, out), (1, expect + "\n"))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.store / ".monitor.lock").exists())
        real = self.run_main("digest", "--store", str(self.store), "--date", D1)
        self.assertEqual(real, (1, expect + "\n"))  # the real digest refuses with the same text

    def test_symlinked_leads_refused_target_untouched(self):
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        target = self.tmp / "elsewhere.csv"
        target.write_text("x\n", encoding="utf-8")
        (self.store / "LEADS.csv").symlink_to(target)
        self.refused("LEADS.csv is a symlink")
        self.assertEqual(target.read_text(encoding="utf-8"), "x\n")

    def test_symlinked_digest_folder_refused(self):
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        (self.tmp / "out").mkdir()
        (self.store / "digest").symlink_to(self.tmp / "out")
        self.refused("digest folder is a symlink")
        self.assertEqual(list((self.tmp / "out").iterdir()), [])

    def test_malformed_scan_log_refused(self):
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, DAY1)
        (self.store / "SCAN-LOG.csv").write_text("bad,header\n", encoding="utf-8")
        self.refused("SCAN-LOG.csv row 1: header is not the accepted header")

    def test_store_inside_repository_refused(self):
        code, out = self.dry(ROOT / "tests" / "flock" / "no-such-store")
        self.assertEqual((code, out), (1, "store inside repository\n"))
        self.assertFalse((ROOT / "tests" / "flock" / "no-such-store").exists())

    def test_more_than_39_items_same_cap_as_real(self):
        rows = [item(0x100 + n, "Agenda item %d" % n) for n in range(40)]
        rows += [item(0x200 + n, "Flock ALPR item %d" % n, agency="Zed Town") for n in range(5)]
        self.write_csv("MONITOR.csv", monitor.MONITOR_HEADER, rows)
        before = self.snapshot()
        code, out = self.dry()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.snapshot(), before)
        lines = out.splitlines()
        self.assertIn("- … and 7 more in MONITOR.csv (0 relevant)", lines)
        self.assertEqual(lines[-1], TRAILER % (D1, 5))
        self.assertEqual(out, self.real_on_copy()[0] + TRAILER % (D1, 5) + "\n")

    def test_dry_run_only_on_digest(self):
        for args in (["scan", "w.csv", "--store", str(self.store), "--dry-run"],
                     ["lead", "--store", str(self.store), "--item", A, "--state", "ignored", "--by", "x", "--dry-run"],
                     ["leads", "--store", str(self.store), "--dry-run"], ["check", "w.csv", "--dry-run"]):
            with self.subTest(args=args[0]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    monitor.main(args)
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(sorted(p.name for p in self.store.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
