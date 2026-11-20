"""RF1221-6: the retained review-config.json read is bounded, no-follow and regular-file only."""
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool.records import analysis_config
from tests.flock import test_review

ROOT = Path(__file__).resolve().parents[2]
SLUG = test_review.SLUG
PROFILE = ROOT / "tests" / "fixtures" / "records" / "analysis-profile.json"
SHA = "a" * 64
INVALID = "CONFIG_INVALID existing_config"


class RetainedConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        method = self.tmp / "method"
        for name in " ".join(test_review.SCRIPTS.values()).split():
            test_review.write_stub(method, name)
        self.root = self.tmp / "root"
        (self.root / "profiles").mkdir(parents=True, mode=0o700)
        profile = PROFILE.read_text().replace("@ROOT@", str(self.root)).replace("@METHOD@", str(method)).replace("@PY@", sys.executable)
        (self.root / "profiles" / (SLUG + ".json")).write_text(profile)
        text = self.root / "agencies" / SLUG / "review" / "inputs" / "records" / (SHA + ".txt")
        text.parent.mkdir(parents=True)
        text.write_text("[[PAGE 1]]\nfictional record text\n")
        self.target = self.root / "agencies" / SLUG / "review-config.json"

    def write(self, **kw):
        return analysis_config.write_config(self.root, SLUG, created_date=kw.pop("created_date", "2026-11-11"), **kw)

    def refused_untouched(self, make):
        """Both modes refuse with the fixed outcome and never build, check or publish."""
        make()
        before = sorted(os.listdir(self.target.parent))
        with mock.patch.object(analysis_config, "build_config") as build:
            for check_only in (False, True):
                self.assertEqual(self.write(check_only=check_only), INVALID)
        build.assert_not_called()
        self.assertEqual(sorted(os.listdir(self.target.parent)), before)

    def test_healthy_bytes_and_run_date_unchanged(self):
        self.assertEqual(self.write(), "CONFIG_WRITTEN")
        first = self.target.read_bytes()
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.write(created_date="2026-12-24"), "CONFIG_UNCHANGED")
        self.assertEqual(self.target.read_bytes(), first)
        self.assertEqual(self.write(created_date="2026-12-24", check_only=True), "CONFIG_UNCHANGED")

    def test_links_are_refused_without_follow_or_replace(self):
        self.write()
        good = self.target.read_bytes()
        foreign = self.tmp / "foreign.json"
        foreign.write_bytes(good)
        mtime = foreign.stat().st_mtime_ns
        self.target.unlink()
        self.refused_untouched(lambda: self.target.symlink_to(foreign))
        self.assertTrue(self.target.is_symlink())
        self.assertEqual(os.readlink(self.target), str(foreign))
        self.assertEqual((foreign.read_bytes(), foreign.stat().st_mtime_ns), (good, mtime))
        self.target.unlink()
        self.refused_untouched(lambda: self.target.symlink_to(self.tmp / "missing.json"))
        self.assertTrue(self.target.is_symlink())

    def test_cli_refuses_link_with_one_line(self):
        self.write()
        self.target.unlink()
        self.target.symlink_to(self.tmp / "missing.json")
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-config", "--root", str(self.root), "--slug", SLUG],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout, done.stderr), (1, INVALID + "\n", ""))

    def test_nonregular_entries_refuse_promptly(self):
        self.write()
        self.target.unlink()
        self.refused_untouched(self.target.mkdir)
        self.target.rmdir()
        self.refused_untouched(lambda: os.mkfifo(self.target))
        self.target.unlink()
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        try:
            sock.bind(str(self.target))
        except OSError:
            self.skipTest("unix sockets unavailable or path too long")
        self.refused_untouched(lambda: None)

    @contextlib.contextmanager
    def watch(self, open_exc=None, read=None):
        """Instrument only the retained-CONFIG descriptor; returns a log of reads, opens and closes."""
        log = {"opens": 0, "reads": [], "open_fds": set(), "closed": []}
        real_open, real_read, real_close = os.open, os.read, os.close
        target = str(self.target)

        def fake_open(path, *a, **kw):
            if str(path) != target:
                return real_open(path, *a, **kw)
            log["opens"] += 1
            if open_exc:
                raise open_exc
            fd = real_open(path, *a, **kw)
            log["open_fds"].add(fd)
            return fd

        def fake_read(fd, n):
            if fd not in log["open_fds"]:
                return real_read(fd, n)
            data = read(fd, n) if read else real_read(fd, n)
            log["reads"].append(len(data))
            return data

        def fake_close(fd):
            if fd in log["open_fds"]:
                log["closed"].append(fd)
                log["open_fds"].discard(fd)
            real_close(fd)
        with mock.patch.object(os, "open", fake_open), mock.patch.object(os, "read", fake_read), mock.patch.object(os, "close", fake_close):
            yield log

    def test_cap_boundaries(self):
        self.write()
        body = self.target.read_bytes()
        cap = analysis_config.MAX_BYTES
        for size, outcome in ((cap - 1, "CONFIG_PENDING"), (cap, "CONFIG_PENDING"), (cap + 1, INVALID), (cap + 4096, INVALID)):
            self.target.write_bytes(b" " * (size - len(body)) + body)
            with self.watch() as log:
                self.assertEqual(self.write(check_only=True), outcome)
            self.assertLessEqual(sum(log["reads"]), cap + 1)
            self.assertEqual(log["open_fds"], set())

    def test_growth_after_fstat_and_short_reads_are_bounded(self):
        self.write()
        chunks = [b" " * 600000, b" " * 600000, b" " * 600000]
        with self.watch(read=lambda fd, n: chunks.pop(0)[:n]) as log:
            self.assertEqual(self.write(check_only=True), INVALID)
        self.assertLessEqual(sum(log["reads"]), analysis_config.MAX_BYTES + 1)
        self.assertEqual(len(log["closed"]), 1)

    def test_read_faults_are_fixed_and_nonwriting(self):
        self.write()
        before = self.target.read_bytes()
        faults = {"open": dict(open_exc=PermissionError(13, "SECRET-canary")),
                  "read": dict(read=mock.Mock(side_effect=OSError(5, "SECRET-canary")))}
        for name, kw in faults.items():
            with self.watch(**kw) as log, mock.patch.object(analysis_config, "build_config") as build:
                for check_only in (False, True):
                    self.assertEqual(self.write(check_only=check_only), INVALID, name)
            build.assert_not_called()
            self.assertEqual(log["opens"], 2)
            self.assertEqual(len(log["closed"]), 0 if name == "open" else 2)
            self.assertEqual(self.target.read_bytes(), before)
        self.assertEqual(self.write(), "CONFIG_UNCHANGED")

    def test_absent_entry_and_absent_parent_are_not_errors(self):
        self.assertEqual(self.write(check_only=True), "CONFIG_PENDING")
        self.assertFalse(self.target.exists())
        self.target.parent.rename(self.target.parent.with_name("moved"))
        self.assertIsNone(analysis_config._read_retained(self.target))

    def test_regular_json_recovery_compatibility(self):
        self.write()
        for junk in (b"{not json", b"\xff\xfe", b"[" * 3000, b"[]", b'{"review": []}', b'{"review": {"run_date": "nope"}}', b""):
            self.target.write_bytes(junk)
            self.assertEqual(self.write(check_only=True), "CONFIG_PENDING", junk[:12])
            self.assertEqual(self.target.read_bytes(), junk)
            self.assertEqual(self.write(created_date="2026-12-24"), "CONFIG_WRITTEN")
            self.assertEqual(json.loads(self.target.read_bytes())["review"]["run_date"], "2026-12-24")

    def test_precedence_and_error_propagation(self):
        profile = self.root / "profiles" / (SLUG + ".json")
        profile.write_text("{")
        with mock.patch.object(analysis_config, "_read_retained") as read:
            self.assertEqual(self.write(), "PROFILE_INVALID")
        read.assert_not_called()
        for exc in (RuntimeError("x"), KeyboardInterrupt()):
            with mock.patch.object(analysis_config, "_read_retained", side_effect=exc):
                self.write_profile_ok()
                with self.assertRaises(type(exc)):
                    self.write()

    def write_profile_ok(self):
        method = self.tmp / "method"
        text = PROFILE.read_text().replace("@ROOT@", str(self.root)).replace("@METHOD@", str(method)).replace("@PY@", sys.executable)
        (self.root / "profiles" / (SLUG + ".json")).write_text(text)

    def test_publication_failure_keeps_existing_boundary(self):
        with mock.patch("os.replace", side_effect=OSError("publish")):
            with self.assertRaises(OSError):
                self.write()


if __name__ == "__main__":
    unittest.main()
