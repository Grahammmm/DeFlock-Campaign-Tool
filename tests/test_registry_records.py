"""FLK-CMP-5: the optional private `records` binding in the campaign register (synthetic paths only)."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import registry

A, B = "a" * 16, "b" * 16


class RegistryRecordsTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(os.path.realpath(tempfile.mkdtemp(prefix="synthetic-reg-")))
        self.register = self.base / "campaigns.json"
        patch = mock.patch.dict(os.environ, {registry.ENV: str(self.register)})
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(registry.release)

    def entry(self, name, records="absent", directory=None):
        row = {"name": name, "directory": str(directory or self.base / name), "added_at": "2026-11-01T00:00:00Z"}
        if records != "absent":
            row["records"] = records
        return row

    def bind(self, root, pid=A):
        return {"export_root": str(root), "source_project_id": pid}

    def load(self, entries):
        self.register.write_text(json.dumps(entries))
        return registry._load(self.register)

    def refused(self, entries):
        self.register.write_text(json.dumps(entries))
        with self.assertRaises(ValueError) as caught:
            registry._load(self.register)
        for secret in ("SENTINEL", str(self.base)):
            self.assertNotIn(secret, str(caught.exception))  # a supplied value is never echoed

    def test_legacy_and_exact_binding_accepted(self):
        entries = [self.entry("one"), self.entry("two", self.bind(self.base / "exp-two"))]
        self.assertEqual(self.load(entries), entries)
        self.assertEqual(self.load([]), [])

    def test_bad_shapes_are_refused_without_echo(self):
        root = self.base / "exp"
        for records in ({"export_root": str(root)}, {**self.bind(root), "extra": "x"}, self.bind(root, "ABCDEF0123456789"),
                        self.bind(root, "abc"), self.bind("relative/SENTINEL"), self.bind(str(root) + "/../x"),
                        self.bind(str(root) + "/"), self.bind("/"), {"export_root": 5, "source_project_id": A}, None, [],
                        self.bind(registry.REPO / "SENTINEL"), self.bind(registry.REPO.parent)):
            with self.subTest(records=records):
                self.refused([self.entry("one", records)])
        self.register.write_text('[{"name": "x", "directory": "/d", "added_at": "t", "records": {"export_root": "/e", '
                                 '"export_root": "/f", "source_project_id": "' + A + '"}}]')
        with self.assertRaises(ValueError):
            registry._load(self.register)

    def test_symlink_ancestry_is_refused_and_missing_roots_are_not(self):
        real = self.base / "real"
        real.mkdir()
        os.symlink(real, self.base / "link")
        self.refused([self.entry("one", self.bind(self.base / "link" / "exp"))])
        self.load([self.entry("one", self.bind(self.base / "missing" / "exp"))])  # not created, not probed into existence
        self.assertFalse((self.base / "missing").exists())

    def test_isolation_between_campaigns(self):
        one, two = self.base / "one", self.base / "two"
        self.load([self.entry("one", self.bind(one / "records-export")), self.entry("two")])  # inside its OWN campaign: allowed
        for entries in ([self.entry("one", self.bind(two / "exp")), self.entry("two")],  # inside another campaign
                        [self.entry("one", self.bind(self.base)), self.entry("two")],  # around another campaign
                        [self.entry("one", self.bind(self.base / "x" / "e", A)),
                         self.entry("two", self.bind(self.base / "x" / "e" / "f", B))],  # overlapping export roots
                        [self.entry("one", self.bind(self.base / "x1", A)), self.entry("two", self.bind(self.base / "x2", A))]):
            with self.subTest(entries=entries):
                self.refused(entries)

    def test_add_and_remove_preserve_other_bindings(self):
        one, two = self.base / "one", self.base / "two"
        for folder in (one, two):
            folder.mkdir()
            (folder / "campaign.json").write_text(json.dumps({"schema_version": 1, "name": folder.name, "county": "X", "state": "CA"}))
        registry.add("one", one)
        entries = json.loads(self.register.read_text())
        entries[0]["records"] = self.bind(self.base / "exp-one")
        self.register.write_text(json.dumps(entries))
        registry.add("two", two)
        self.assertEqual(json.loads(self.register.read_text())[0]["records"], self.bind(self.base / "exp-one"))
        registry.remove("two")
        self.assertEqual(json.loads(self.register.read_text())[0]["records"], self.bind(self.base / "exp-one"))
        inside = self.base / "exp-one" / "inner"  # a new campaign may not sit inside another's records export root
        inside.mkdir(parents=True)
        (inside / "campaign.json").write_text(json.dumps({"schema_version": 1, "name": "x", "county": "X", "state": "CA"}))
        with self.assertRaises(ValueError):
            registry.add("three", inside)

    def test_size_cap_counts_the_binding(self):
        row = self.entry("one", self.bind(self.base / "exp"))
        pad = registry.MAX_BYTES - len(json.dumps([row], indent=2)) - 1
        row["directory"] = str(self.base / ("d" * 10))
        for extra, ok in ((0, True), (1, False)):
            text = json.dumps([row], indent=2)
            text += " " * (registry.MAX_BYTES - len(text.encode()) + extra)
            self.register.write_text(text)
            if ok:
                self.assertEqual(len(registry._load(self.register)), 1)
            else:
                with self.assertRaises(ValueError):
                    registry._load(self.register)
        self.assertGreater(pad, 0)


if __name__ == "__main__":
    unittest.main()
