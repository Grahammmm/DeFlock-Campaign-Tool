"""RF1210-12: a saved register name holding a non-printable character refuses the whole register with one fixed
value-free ValueError, before selection, listing or mutation. Synthetic real-file tests only."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import registry

REPO = Path(__file__).resolve().parents[1]
FIXED = "the register contains a non-printable campaign name"
STOPPED = f"Stopped: {FIXED}\n"
STAMP = "2026-01-01T00:00:00Z"
BAD = ("Alpha\nFORGED", "Alpha\rX", "Alpha\tX", "Alpha\x00X", "Alpha\x1b[2J", "Alpha\x7fX", "Alpha\x85X",
       "Alpha\u202eBeta", "Alpha\u200bX", "Alpha\u2028X", "Alpha\u2029X", "Alpha\ud800X")


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-nametext-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(os.path.realpath(temporary.name))
        self.config = self.base / "config"
        self.config.mkdir(mode=0o700)
        self.register = self.config / "campaigns.json"
        self.a, self.b, self.c = (self.campaign(n) for n in ("DirA", "DirB", "DirC"))
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)

    def campaign(self, name):
        root = self.base / name
        root.mkdir()
        (root / "campaign.json").write_text(
            json.dumps({"schema_version": 1, "name": name, "county": "Synthetic", "state": "CA"}), encoding="utf-8")
        return root

    def seed(self, *rows, raw=None):
        data = raw if raw is not None else json.dumps(
            [{"name": n, "directory": str(d), "added_at": STAMP} for n, d in rows]).encode("utf-8")
        fd = os.open(self.register, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)

    def snapshot(self):
        info = os.stat(self.register)
        files = sorted((p.relative_to(self.base).as_posix(), p.read_bytes(), p.stat().st_mode)
                       for p in self.base.rglob("*") if p.is_file() and p != self.register)
        return (self.register.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns,
                sorted(os.listdir(self.config)), files)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO, text=True,
                              capture_output=True, timeout=60, env=dict(os.environ))


class Names(Base):
    def test_saved_names_refuse_before_any_consumer(self):
        for bad in BAD:
            for label, rows in (("first", [(bad, self.a), ("Ok", self.b), ("Fine", self.c)]),
                                ("middle", [("Ok", self.b), (bad, self.a), ("Fine", self.c)]),
                                ("last", [("Ok", self.b), ("Fine", self.c), (bad, self.a)])):
                with self.subTest(repr(bad), label=label):
                    self.seed(*rows)
                    with self.assertRaises(ValueError) as caught:
                        registry._load(self.register)
                    self.assertEqual(str(caught.exception), FIXED)
        # decoded JSON escape spelling counts identically
        self.seed(raw=('[{"name": "A\\u000aB", "directory": %s, "added_at": "x"}]' % json.dumps(str(self.a))).encode())
        with self.assertRaises(ValueError) as caught:
            registry._load(self.register)
        self.assertEqual(str(caught.exception), FIXED)
        self.assertNotIn(str(self.a), str(caught.exception))

    def test_printable_saved_names_remain_compatible(self):
        names = ["Alpha", "alpha", "with space", "\u00e9", "e\u0301", "a.b_c-d", "x (y), z!", "", "\u65e5\u672c"]
        self.seed(*[(n, self.a) for n in names])
        self.assertEqual([e["name"] for e in registry._load(self.register)], names)

    def test_existing_refusals_keep_precedence(self):
        self.seed(raw=b'[{"name": "S\\n", "name": "S\\n", "directory": "d", "added_at": "x"}]')
        with self.assertRaisesRegex(ValueError, "repeated object member"):
            registry._load(self.register)
        self.seed(raw=b'[{"name": "S\\n"}]')
        with self.assertRaisesRegex(ValueError, "must hold only a list"):
            registry._load(self.register)
        self.seed(("S\n", self.a), ("S\n", self.b))
        with self.assertRaisesRegex(ValueError, "repeated campaign name"):
            registry._load(self.register)


class Routes(Base):
    def test_cli_and_named_selector_refuse_without_partial_rows(self):
        self.seed(("Healthy", self.b), ("Alpha\nFORGED-ROW", self.a))
        before = self.snapshot()
        for argv in (("list",), ("status",), ("today", "--as-of", "2026-11-11"),
                     ("today", "--as-of", "2026-11-11", "--json"), ("remove", "Healthy"),
                     ("add", "Third", "--directory", self.c)):
            with self.subTest(argv):
                result = self.cli("campaigns", *argv)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", STOPPED))
        named = self.cli("status", "--campaign", "Healthy")
        self.assertEqual((named.returncode, named.stdout), (2, ""))
        self.assertIn(FIXED, named.stderr)
        self.assertNotIn("Traceback", named.stderr)
        self.assertNotIn("FORGED", named.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_no_campaign_read_and_lock_release(self):
        self.seed(("Healthy", self.b), ("Bad\x1b[2J", self.a))
        before = self.snapshot()
        with mock.patch("campaign_tool.cli.read_config", side_effect=AssertionError("campaign read")):
            with self.assertRaises(ValueError):
                registry.resolve("Healthy")
        self.assertEqual(self.snapshot(), before)
        self.seed(("Healthy", self.b), ("Other", self.a))  # restore valid bytes
        self.assertEqual(self.cli("campaigns", "remove", "Other").returncode, 0)
        self.assertEqual(self.cli("campaigns", "add", "Third", "--directory", self.c).returncode, 0)
        self.assertEqual([e["name"] for e in registry._load(self.register)], ["Healthy", "Third"])


if __name__ == "__main__":
    unittest.main()
