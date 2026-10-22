"""T4B slice A, group A: the approved mail-resolution policy (synthetic bytes only)."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from campaign_tool.records.intake import mail_resolution_policy as module
from campaign_tool.records.intake.mail_resolution_policy import ApprovedMailResolutionPolicy
from tests.records.mail_resolution_fixtures import build_policy, policy_bytes, sha

O1, O2, O3 = sha("original one"), sha("original two"), sha("original three")
R1, R2, R3 = sha("receipt one"), sha("receipt two"), sha("receipt three")
OCC = sha("occurrence one")


def entry(**changes):
    value = {"bytes": 12, "role": "original", "scope": "in_scope", "verification_receipt_sha256": R1,
             "binding_occurrence_id": OCC, "dependent_receipts": [R2], "legacy_binding_sha256": sha("legacy")}
    value.update(changes)
    return value


def load(raw, expected=None):
    return ApprovedMailResolutionPolicy(raw, expected_sha256=expected or sha(raw))


class PolicyTests(unittest.TestCase):
    def refuses(self, raw, code="resolution_policy_invalid"):
        with self.assertRaisesRegex(ValueError, code):
            load(raw)

    def test_valid_policy_loads_and_hash_is_checked(self):  # 1
        policy = build_policy({O1: entry()}, [O2])
        self.assertEqual(policy.sha256, sha(policy.raw))
        self.assertEqual(policy.originals(), [O1])
        self.assertEqual(policy.held(), [O2])
        self.assertTrue(policy.is_held(O2) and not policy.is_held(O1))
        self.assertEqual(policy.entry(O1), entry())
        self.assertIsNone(policy.entry(O2))

    def test_entry_is_a_copy(self):
        policy = build_policy({O1: entry()})
        policy.entry(O1)["dependent_receipts"].append("x")
        self.assertEqual(policy.entry(O1)["dependent_receipts"], [R2])

    def test_hash_mismatch_fails_closed(self):  # 2
        raw = policy_bytes({O1: entry()})
        with self.assertRaisesRegex(ValueError, "resolution_policy_hash_mismatch"):
            load(raw, sha("another policy"))
        with self.assertRaisesRegex(ValueError, "invalid_capture_hash"):
            load(raw, "ABC")

    def test_rejected_inputs(self):  # 3
        good = json.loads(policy_bytes({O1: entry()}, [O2]))

        def raw(**changes):
            value = dict(good, **changes)
            return json.dumps(value, sort_keys=True).encode()
        cases = {
            "extra top key": raw(extra=1),
            "wrong schema": raw(schema="approved-mail-resolution-policy-v2"),
            "bad inventory": raw(inventory_sha256="A" * 64),
            "missing entry key": raw(originals={O1: {k: v for k, v in entry().items() if k != "bytes"}}),
            "extra entry key": raw(originals={O1: entry(extra=1)}),
            "role": raw(originals={O1: entry(role="reference")}),
            "scope": raw(originals={O1: entry(scope="out_of_scope")}),
            "bytes bool": raw(originals={O1: entry(bytes=True)}),
            "bytes negative": raw(originals={O1: entry(bytes=-1)}),
            "upper-case sha": raw(originals={O1.upper(): entry()}),
            "upper-case receipt": raw(originals={O1: entry(verification_receipt_sha256=R1.upper())}),
            "malformed occurrence": raw(originals={O1: entry(binding_occurrence_id="occ-1")}),
            "short occurrence": raw(originals={O1: entry(binding_occurrence_id=OCC[:63])}),
            "unsorted dependents": raw(originals={O1: entry(dependent_receipts=sorted([R2, R3], reverse=True))}),
            "repeated dependents": raw(originals={O1: entry(dependent_receipts=[R2, R2])}),
            "binding in own dependents": raw(originals={O1: entry(dependent_receipts=sorted([R1, R2]))}),
            "dependents not a list": raw(originals={O1: entry(dependent_receipts=R2)}),
            "unsorted held": raw(held=sorted([O2, O3], reverse=True)),
            "repeated held": raw(held=[O2, O2]),
            "original also held": raw(held=[O1]),
            "originals not a dict": raw(originals=[O1]),
            "empty policy": raw(originals={}, held=[]),
            "not json": b"not json",
            "not an object": b"[]",
        }
        for name, value in cases.items():
            with self.subTest(name):
                self.refuses(value)

    def test_duplicate_keys_and_non_finite_numbers(self):  # 3
        text = policy_bytes({O1: entry()}).decode()
        self.refuses(text.replace('"role":"original"', '"role":"original","role":"original"').encode())
        self.refuses(text.replace('"bytes":12', '"bytes":NaN').encode())
        self.refuses(text.replace('"bytes":12', '"bytes":Infinity').encode())

    def test_size_and_entry_bounds(self):  # 3
        raw = policy_bytes({O1: entry()})
        self.refuses(raw + b" " * (module.MAX_POLICY_BYTES - len(raw) + 1))
        load(raw + b" " * (module.MAX_POLICY_BYTES - len(raw)))
        # 10,000 full entries would exceed 2 MiB, so the entry bound is exercised with `held`.
        limit = module.MAX_POLICY_ENTRIES
        digests = [format(number + 100, "064x") for number in range(limit + 1)]
        load(policy_bytes({}, digests[:limit]))
        self.refuses(policy_bytes({}, digests))
        load(policy_bytes({O1: entry()}, digests[:limit - 1]))
        self.refuses(policy_bytes({O1: entry()}, digests[:limit]))
        many = [format(number + 1, "064x") for number in range(module.MAX_DEPENDENT_RECEIPTS + 1)]
        self.refuses(policy_bytes({O1: entry(dependent_receipts=many)}))
        load(policy_bytes({O1: entry(dependent_receipts=many[:-1])}))


class PolicyFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.root.chmod(0o700)
        self.directory = self.root / "resolution-policies"
        self.directory.mkdir(mode=0o700)
        self.raw = policy_bytes({O1: entry()})
        self.digest = sha(self.raw)
        self.path = self.directory / self.digest

    def write(self, mode=0o400):
        self.path.write_bytes(self.raw)
        self.path.chmod(mode)

    def test_owner_only_regular_file_loads(self):  # 4
        self.write()
        self.assertEqual(module.load(self.root, self.digest).sha256, self.digest)

    def test_unsafe_files_are_refused(self):  # 4
        for name, prepare in {
            "group readable": lambda: self.write(0o640),
            "world readable": lambda: self.write(0o604),
            "missing": lambda: None,
            "symlink": lambda: self.path.symlink_to(self.root / "elsewhere"),
            "directory": lambda: self.path.mkdir(),
            "hardlink": lambda: (self.write(), os.link(self.path, self.root / "second")),
        }.items():
            with self.subTest(name):
                if self.path.is_symlink() or self.path.exists():
                    if self.path.is_dir() and not self.path.is_symlink():
                        self.path.rmdir()
                    else:
                        self.path.unlink()
                (self.root / "second").unlink(missing_ok=True)
                prepare()
                if name == "symlink":
                    (self.root / "elsewhere").write_bytes(self.raw)
                with self.assertRaisesRegex(ValueError, "resolution_policy_unsafe_file"):
                    module.load(self.root, self.digest)

    def test_symlinked_directory_component_is_refused(self):  # 4
        self.write()
        link = self.root / "linked"
        link.symlink_to(self.root)
        with self.assertRaisesRegex(ValueError, "resolution_policy_unsafe_file"):
            module.load(link, self.digest)

    def test_changed_bytes_are_refused(self):
        self.write(0o600)
        self.path.write_bytes(self.raw + b" ")
        self.path.chmod(0o400)
        with self.assertRaisesRegex(ValueError, "resolution_policy_hash_mismatch"):
            module.load(self.root, self.digest)


if __name__ == "__main__":
    unittest.main()
