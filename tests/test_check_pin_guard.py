import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_pin_guard", REPO / "tools/check_pin_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


class PinGuardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "a.py").write_bytes(b"pinned\n")
        (self.root / "b.py").write_bytes(b"also pinned\n")
        pins = {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                for name in ("a.py", "b.py")}
        patcher = mock.patch.dict(guard.REGISTRIES, {"synthetic": (("pins.json",), lambda repo: pins)},
                                  clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_unrelated_or_unmodified_changes_pass(self):
        self.assertEqual(guard.violations(self.root, {"c.py"}), [])
        self.assertEqual(guard.violations(self.root, {"a.py"}), [])

    def test_changed_pinned_file_without_repin_fails(self):
        (self.root / "a.py").write_bytes(b"edited\n")
        self.assertEqual(guard.violations(self.root, {"a.py"}),
                         [("synthetic", "a.py", "content differs from its pin")])
        # Not in the diff: left to the full suite, not blamed on this PR.
        self.assertEqual(guard.violations(self.root, {"c.py"}), [])

    def test_registry_change_or_all_checks_every_pin(self):
        (self.root / "b.py").unlink()
        expected = [("synthetic", "b.py", "pinned file missing")]
        self.assertEqual(guard.violations(self.root, {"pins.json"}), expected)
        self.assertEqual(guard.violations(self.root, None), expected)


class RealRegistriesTest(unittest.TestCase):
    def test_real_registries_parse(self):
        for name, (holders, loader) in guard.REGISTRIES.items():
            pins = loader(REPO)
            self.assertTrue(pins, name)
            for holder in holders:
                self.assertTrue((REPO / holder).is_file(), holder)
            for digest in pins.values():
                self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")

    def test_known_pinned_sources_are_covered(self):
        pinned = set()
        for _holders, loader in guard.REGISTRIES.values():
            pinned.update(loader(REPO))
        self.assertIn("campaign_tool/records/run_stages.py", pinned)
        self.assertIn("campaign_tool/records/mail_adoption.py", pinned)


if __name__ == "__main__":
    unittest.main()
