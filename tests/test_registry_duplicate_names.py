"""RF1900-3: two saved register rows with one decoded, case-sensitive campaign name refuse the whole register with one
fixed value-free ValueError, before selection, aggregate output or mutation. Synthetic real-file tests only."""
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
FIXED = "the register contains a repeated campaign name"
STOPPED = f"Stopped: {FIXED}\n"
STAMP = "2026-01-01T00:00:00Z"


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-dupnames-")
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
        return (self.register.read_bytes(), info.st_ino, info.st_mode, info.st_mtime_ns, sorted(os.listdir(self.config)))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "campaign_tool", *map(str, args)], cwd=REPO, text=True,
                              capture_output=True, timeout=60, env=dict(os.environ))


class Names(Base):
    def test_equal_names_across_distinct_rows_refuse_whole(self):
        for label, rows in (("adjacent", [("S", self.a), ("S", self.b)]),
                            ("nonadjacent", [("S", self.a), ("T", self.c), ("S", self.b)]),
                            ("identical", [("S", self.a), ("S", self.a)]),
                            ("late", [("T", self.c), ("S", self.a), ("S", self.b)])):
            with self.subTest(label):
                self.seed(*rows)
                with self.assertRaises(ValueError) as caught:
                    registry._load(self.register)
                self.assertEqual(str(caught.exception), FIXED)
                self.assertNotIn(str(self.a), str(caught.exception))
                for call in (lambda: registry.resolve("T" if label != "nonadjacent" else "T"), lambda: registry.resolve("S")):
                    with self.assertRaises(ValueError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), FIXED)

    def test_decoded_name_identity_is_exact_and_case_sensitive(self):
        raw = ('[{"name": "na\\u006de", "directory": %s, "added_at": "x"}, {"name": "name", "directory": %s, '
               '"added_at": "x"}]' % (json.dumps(str(self.a)), json.dumps(str(self.b)))).encode()
        self.seed(raw=raw)
        with self.assertRaisesRegex(ValueError, FIXED):
            registry._load(self.register)
        self.seed(("Alpha", self.a), ("alpha", self.b), ("\u00e9", self.c), ("e\u0301", self.c))
        self.assertEqual([e["name"] for e in registry._load(self.register)], ["Alpha", "alpha", "\u00e9", "e\u0301"])

    def test_earlier_errors_keep_precedence_and_empty_stays_empty(self):
        self.seed(raw=b'[{"name": "S", "name": "S", "directory": "d", "added_at": "x"}]')
        with self.assertRaisesRegex(ValueError, "repeated object member"):
            registry._load(self.register)
        self.seed(raw=b'[{"name": "S"}, {"name": "S"}]')
        with self.assertRaisesRegex(ValueError, "must hold only a list"):
            registry._load(self.register)
        self.seed(raw=b"")
        self.assertEqual(registry._load(self.register), [])


class Routes(Base):
    def test_all_verbs_refuse_without_mutation_and_named_selector_exits_2(self):
        self.seed(("S", self.a), ("S", self.b))
        before = self.snapshot()
        for argv in (("list",), ("status",), ("today", "--as-of", "2026-11-11"),
                     ("today", "--as-of", "2026-11-11", "--json"), ("remove", "S"),
                     ("add", "Third", "--directory", self.c)):
            with self.subTest(argv):
                result = self.cli("campaigns", *argv)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (1, "", STOPPED))
        named = self.cli("status", "--campaign", "S")
        self.assertEqual(named.returncode, 2)
        self.assertIn(FIXED, named.stderr)
        self.assertNotIn("Traceback", named.stderr)
        self.assertEqual(self.snapshot(), before)  # remove did not drop both rows; add did not rewrite

    def test_unique_selection_refused_when_other_rows_collide(self):
        self.seed(("U", self.c), ("S", self.a), ("S", self.b))
        result = self.cli("status", "--campaign", "U")
        self.assertEqual(result.returncode, 2)
        self.assertIn(FIXED, result.stderr)

    def test_valid_replacement_recovers_and_unique_names_work(self):
        self.seed(("S", self.a), ("S", self.b))
        self.assertEqual(self.cli("campaigns", "list").returncode, 1)
        self.seed(("Alpha", self.a), ("alpha", self.b))
        listed = self.cli("campaigns", "list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual([line.split(" | ")[1] for line in listed.stdout.splitlines()], ["Alpha", "alpha"])
        self.assertEqual(self.cli("campaigns", "add", "Third", "--directory", self.c).returncode, 0)
        self.assertEqual(self.cli("campaigns", "remove", "alpha").returncode, 0)
        self.assertEqual([e["name"] for e in registry._load(self.register)], ["Alpha", "Third"])
        self.assertEqual(sorted(os.listdir(self.config)), ["campaigns.json"])


if __name__ == "__main__":
    unittest.main()
