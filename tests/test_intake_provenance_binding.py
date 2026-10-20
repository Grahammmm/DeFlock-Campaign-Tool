"""The intake provenance manifest's scan exemptions after the #137 atomic secure-open change.

Synthetic cases only: the real manifest and engine bytes are read or copied into a temporary
root; no private data. Every replacement hash is computed here, so no new hex literal appears.
"""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.check_secret_report import APPROVED_MANIFESTS, HASH_LINE, verified_provenance_hit

ROOT = Path(__file__).resolve().parents[1]
NAME = "docs/records-intake-import.json"
HEX = "Hex High Entropy String"
ENGINES = ("campaign_tool/records/intake/folder.py", "campaign_tool/records/intake/repair_intake.py",
           "campaign_tool/records/intake/scope_quarantine.py", "tests/records/test_intake.py")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def hits(raw):
    """One scanner-shaped hit per exact provenance hash line."""
    found = []
    for number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        match = HASH_LINE.fullmatch(line)
        if match:
            found.append({"type": HEX, "line_number": number, "hashed_secret": hashlib.sha1(match[1].encode()).hexdigest()})
    return found


class RealManifestTests(unittest.TestCase):
    def setUp(self):
        self.raw = (ROOT / NAME).read_bytes()
        self.data = json.loads(self.raw)

    def test_nine_hash_fields_are_each_exempt_on_the_reviewed_bytes(self):
        found = hits(self.raw)
        self.assertEqual([h["line_number"] for h in found], [7, 9, 13, 15, 19, 21, 25, 27, 31])
        for hit in found:
            with self.subTest(line=hit["line_number"]):
                self.assertTrue(verified_provenance_hit(ROOT, NAME, hit))

    def test_whole_manifest_pin_binds_these_exact_bytes(self):
        self.assertEqual(sha256(self.raw), APPROVED_MANIFESTS[NAME])

    def test_four_engine_bindings_match_current_engine_bytes(self):
        self.assertEqual(tuple(e["engine_path"] for e in self.data["files"]), ENGINES)
        for entry in self.data["files"]:
            with self.subTest(path=entry["engine_path"]):
                self.assertEqual(sha256((ROOT / entry["engine_path"]).read_bytes()), entry["engine_sha256"])

    def test_atomic_open_change_is_recorded_after_every_earlier_adaptation(self):
        notes = self.data["port_adaptations"]
        self.assertEqual(len(notes), 16)
        self.assertTrue(notes[-1].startswith("#137: secure_open opens once with O_NOFOLLOW_ANY alone"))
        self.assertTrue(notes[-2].startswith("C20 acceptance reconciliation:"))
        self.assertEqual(self.data["files"][0]["source_relative_path"], "scripts/intake.py")


class CopiedManifestTests(unittest.TestCase):
    """A copy of the manifest and its four engine files, approved by its own copied bytes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for relative in (NAME, *ENGINES):
            (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, self.root / relative)
        self.raw = (self.root / NAME).read_bytes()
        self.approved = {NAME: sha256(self.raw)}
        self.hits = hits(self.raw)

    def allowed(self, hit, path=NAME, approved=None):
        return verified_provenance_hit(self.root, path, hit, self.approved if approved is None else approved)

    def rewrite(self, old, new, repin=False):
        raw = self.raw.replace(old.encode(), new.encode(), 1)
        self.assertNotEqual(raw, self.raw)
        (self.root / NAME).write_bytes(raw)
        if repin:
            self.approved = {NAME: sha256(raw)}
        return raw

    def test_copy_accepts_all_nine(self):
        self.assertEqual(len(self.hits), 9)
        self.assertTrue(all(self.allowed(hit) for hit in self.hits))

    def test_each_changed_engine_byte_refuses_every_hit(self):
        for relative in ENGINES:
            with self.subTest(path=relative):
                path = self.root / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assertFalse(any(self.allowed(hit) for hit in self.hits))
                path.write_bytes(original)

    def test_changed_source_engine_or_schema_hash_refuses_without_a_new_pin(self):
        data = json.loads(self.raw)
        for field, old in (("source_sha256", data["files"][0]["source_sha256"]), ("engine_sha256", data["files"][0]["engine_sha256"]),
                           ("schema_sha256", data["schema_sha256"])):
            with self.subTest(field=field):
                raw = self.rewrite(old, sha256(b"synthetic replacement " + old.encode()))
                self.assertFalse(any(self.allowed(hit) for hit in hits(raw)))
                (self.root / NAME).write_bytes(self.raw)

    def test_other_manifest_pin_refuses(self):
        approved = {NAME: sha256(b"synthetic other manifest")}
        self.assertFalse(any(self.allowed(hit, approved=approved) for hit in self.hits))

    def test_other_detector_refuses(self):
        for kind in ("Secret Keyword", "Base64 High Entropy String", "Private Key", None):
            with self.subTest(kind=kind):
                self.assertFalse(self.allowed({**self.hits[0], "type": kind}))

    def test_other_path_refuses(self):
        for path in ("./" + NAME, "docs/records-gates-import.json", "docs/other.json", NAME.upper()):
            with self.subTest(path=path):
                self.assertFalse(self.allowed(self.hits[0], path=path))
        # The gates manifest is approved, but an intake hash hit is not one of its exact lines.
        self.assertFalse(any(verified_provenance_hit(ROOT, "docs/records-gates-import.json", hit) for hit in self.hits))

    def test_wrong_line_or_scanned_value_refuses(self):
        hit = self.hits[1]
        lines = len(self.raw.decode().splitlines())
        for change in ({"line_number": hit["line_number"] + 1}, {"line_number": 3}, {"line_number": 0},
                       {"line_number": True}, {"line_number": str(hit["line_number"])}, {"line_number": lines + 1},
                       {"hashed_secret": hashlib.sha1(b"synthetic other value").hexdigest()}, {"hashed_secret": None}):
            with self.subTest(change=change):
                self.assertFalse(self.allowed({**hit, **change}))

    def test_added_entropy_or_credential_line_refuses_even_when_repinned(self):
        value = sha256(b"synthetic non-provenance value")
        raw = self.rewrite('  "scope":', f'  "extra_value": "{value}",\n  "scope":', repin=True)
        lines = raw.decode().splitlines()
        number = next(i for i, line in enumerate(lines, 1) if '"extra_value"' in line)
        for kind in (HEX, "Secret Keyword"):
            with self.subTest(kind=kind):
                self.assertFalse(self.allowed({"type": kind, "line_number": number,
                                               "hashed_secret": hashlib.sha1(value.encode()).hexdigest()}))

    def test_adaptation_note_line_is_not_a_hash_line(self):
        lines = self.raw.decode().splitlines()
        number = next(i for i, line in enumerate(lines, 1) if line.lstrip().startswith('"#137:'))
        self.assertFalse(self.allowed({"type": HEX, "line_number": number,
                                       "hashed_secret": hashlib.sha1(lines[number - 1].encode()).hexdigest()}))


if __name__ == "__main__":
    unittest.main()
