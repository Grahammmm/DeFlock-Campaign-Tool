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


def sha(data):
    return hashlib.sha256(data).hexdigest()


class PinGuardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "a.py").write_bytes(b"pinned\n")
        (self.root / "b.py").write_bytes(b"also pinned\n")
        (self.root / "m.json").write_bytes(b"token-1\n")
        expand = lambda raw: raw.replace(b"token-1", b"real-value")  # noqa: E731
        pins = [("a.py", sha(b"pinned\n"), None), ("b.py", sha(b"also pinned\n"), None),
                ("m.json", sha(b"token-1\n"), None), ("m.json", sha(b"real-value\n"), expand)]
        patcher = mock.patch.dict(guard.REGISTRIES, {"synthetic": (("pins.json",), lambda repo: pins)},
                                  clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.pins = pins

    def test_unrelated_or_unmodified_changes_pass(self):
        self.assertEqual(guard.violations(self.root, {"c.py"}), [])
        self.assertEqual(guard.violations(self.root, {"a.py", "m.json"}), [])

    def test_changed_pinned_file_without_repin_fails(self):
        (self.root / "a.py").write_bytes(b"edited\n")
        self.assertEqual(guard.violations(self.root, {"a.py"}),
                         [("synthetic", "a.py", "content differs from its pin")])
        # Not in the diff: left to the full suite, not blamed on this PR.
        self.assertEqual(guard.violations(self.root, {"c.py"}), [])

    def test_stale_materialized_pin_fails_even_with_raw_repin(self):
        (self.root / "m.json").write_bytes(b"token-1 edited\n")
        self.pins[2] = ("m.json", sha(b"token-1 edited\n"), None)  # raw pin updated, materialized not
        self.assertEqual(guard.violations(self.root, {"m.json"}),
                         [("synthetic", "m.json", "materialized content (hash anchors restored) differs from its pin")])

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
            for _path, digest, _transform in pins:
                self.assertRegex(digest, r"\A[0-9a-f]{64}\Z")

    def test_known_pinned_sources_are_covered(self):
        pinned = {}
        for _holders, loader in guard.REGISTRIES.values():
            for path, _digest, transform in loader(REPO):
                pinned.setdefault(path, set()).add(transform is not None)
        self.assertIn("campaign_tool/records/run_stages.py", pinned)
        self.assertIn(f"{guard.T4B_FROZEN}/campaign_tool/records/mail_adoption.py.txt", pinned)
        # T4B pins are historical: the live files may move on without a re-pin.
        self.assertNotIn("campaign_tool/records/mail_adoption.py", pinned)
        self.assertNotIn("campaign_tool/records/cli.py", pinned)
        self.assertEqual(pinned["tests/fixtures/comprehension/rules.json"], {False, True})


class T4BHistoricalPinsTest(unittest.TestCase):
    def test_live_drift_passes_frozen_copy_drift_fails(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "tools").mkdir()
        digest = sha(b"frozen\n")
        (root / "tools/t4b_test_apply_check.py").write_text(f"PINS = {{'a.py': '{digest}'}}\n", encoding="utf-8")
        copy_rel = f"{guard.T4B_FROZEN}/a.py.txt"
        copy = root / copy_rel
        copy.parent.mkdir(parents=True)
        copy.write_bytes(b"frozen\n")
        (root / "a.py").write_bytes(b"moved on\n")
        name = next(n for n in guard.REGISTRIES if n.startswith("T4B checker historical pins"))
        patcher = mock.patch.dict(guard.REGISTRIES, {name: guard.REGISTRIES[name]}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(guard.violations(root, None), [])
        self.assertEqual(guard.violations(root, {"a.py"}), [])
        copy.write_bytes(b"tampered\n")
        expected = [(name, copy_rel, "content differs from its pin")]
        self.assertEqual(guard.violations(root, None), expected)
        self.assertEqual(guard.violations(root, {copy_rel}), expected)
        # Holder changed (PINS edited without its copy) -> every T4B pin is checked.
        self.assertEqual(guard.violations(root, {"tools/t4b_test_apply_check.py"}), expected)
        copy.unlink()
        self.assertEqual(guard.violations(root, None), [(name, copy_rel, "pinned file missing")])


if __name__ == "__main__":
    unittest.main()
