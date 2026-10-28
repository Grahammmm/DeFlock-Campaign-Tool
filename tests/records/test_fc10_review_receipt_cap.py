"""Retained synthetic receipt-cap experiments; inert during unittest discovery."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


OBSERVATIONS = []
SPEC = Path(__file__).resolve().parents[1] / "fixtures" / "fc10-review-receipt-cap" / "cases.json"


class ReceiptCapTests(unittest.TestCase):
    def setUp(self):
        from campaign_tool.records import review_bundle
        self.b = review_bundle
        self.spec = json.loads(SPEC.read_text())
        self.assertEqual(self.b.MAX_ITEMS, self.spec["limit"])
        self.root = Path(tempfile.mkdtemp(prefix="fc10-synthetic-")).resolve()
        self.root.chmod(0o700)
        self.store = self.root / "bundles"
        self.store.mkdir(mode=0o700)
        self.contexts = {name: object() for name in ("author", "factual", "owner")}
        sessions = {value: name for name, value in self.contexts.items()}
        principals = {name: {"identity": "synthetic-" + name, "provider": "synthetic",
                            "model": "synthetic", "roles": [name]} for name in self.contexts}
        self.authority = self.b.Authority(profile_id="fc10-synthetic", principals=principals,
            authenticate=lambda context: sessions[context], signing_key=b"synthetic-fixture-only-" * 3,
            test_only=True)
        bindings = {kind: [] for kind in self.b.KINDS}
        for kind in ("originals", "units", "digests", "sources"):
            raw = ("Synthetic " + kind).encode()
            path = self.root / (kind + ".txt")
            path.write_bytes(raw)
            path.chmod(0o600)
            bindings[kind] = [{"id": kind, "path": str(path), "sha256": self.b.sha(raw), "bytes": len(raw)}]
        original, unit = bindings["originals"][0], bindings["units"][0]
        proposal = {"proposal_id": "fc10-synthetic", "revision": 1, "tier": "A",
            "agency": "Synthetic agency", "classification": "CONFIRMED_DOCUMENTARY_FACT",
            "next_action": "Await independent review", "event_date": None,
            "primary_evidence": [{"path": original["path"], "sha256": original["sha256"],
                "locator": "line:1", "observation": "Synthetic observation", "coverage": "selected"}],
            "rules": [], "counterevidence": [], "missing_evidence": [], "exceptions_checked": False,
            "bindings": bindings, "coverage": [{"original_sha256": original["sha256"],
                "unit_sha256": unit["sha256"], "locator": "line:1", "scope": "selected"}], "supersedes": None}
        public = self.b.encoded({"title": "Synthetic", "claim": "Synthetic observation", "limitations": []})
        self.identity = self.b.create_bundle(self.store, public, proposal, authority=self.authority,
            auth_context=self.contexts["author"])["bundle_id"]
        self.directory = self.store / self.identity
        self.review_payload = dict(self.b.review_target(self.store, self.identity, authority=self.authority),
            role="factual", verdict="pass", rationale="Synthetic review", reviewed_at="2026-01-01T00:00:00Z",
            reviewed_primary=True)
        assessment = self.assess()
        self.owner_payload = {key: assessment[key] for key in
            ("bundle_id", "finding_digest", "public_content_sha256", "review_set_sha256")}
        self.owner_payload.update(decision="reject", rationale="Synthetic reject", decided_at="2026-01-01T00:00:00Z")

    def assess(self):
        return self.b.assess_bundle(self.store, self.identity, authority=self.authority)

    def write(self, kind, index):
        if kind == "reviews":
            return self.b.record_review(self.store, self.identity,
                dict(self.review_payload, rationale="Synthetic review " + str(index)),
                authority=self.authority, auth_context=self.contexts["factual"])
        # Rebind to a genuine current assessment when reviews have changed.
        assessment = self.assess()
        payload = dict(self.owner_payload, rationale="Synthetic reject " + str(index))
        payload.update({key: assessment[key] for key in
            ("bundle_id", "finding_digest", "public_content_sha256", "review_set_sha256")})
        return self.b.record_owner_decision(self.store, self.identity, payload,
            authority=self.authority, auth_context=self.contexts["owner"])

    def fill(self, kind, count):
        for index in range(count):
            self.assertFalse(self.write(kind, index)["reused"])
        self.assertEqual(len(list((self.directory / kind).glob("*.json"))), count)

    def snapshot(self, kind):
        return {p.name: ("directory" if p.is_dir() else self.b.sha(p.read_bytes()))
                for p in sorted((self.directory / kind).iterdir())}

    def note(self, case, **details):
        OBSERVATIONS.append(dict(case=case, test_id=self.id(), root=str(self.root), **details))

    def no_new_receipt(self, kind, before):
        self.assertEqual(self.snapshot(kind), before, "no-new-receipt assertion")

    def boundary(self, kind):
        self.fill(kind, 255)
        self.assess()
        self.assertFalse(self.write(kind, 255)["reused"])
        self.assess()
        before, assessment = self.snapshot(kind), self.assess()
        with self.assertRaisesRegex(self.b.BundleError, "^receipt_inventory$"):
            self.write(kind, 256)
        self.no_new_receipt(kind, before)
        self.assertEqual(self.assess(), assessment)
        self.note(kind + "_at_255_256_257", accepted=[255, 256], refused=257,
                  committed=len(before), preserved_assessment=True)

    def test_reviews_at_255_256_257(self):
        self.boundary("reviews")

    def test_owners_at_255_256_257(self):
        self.boundary("owners")

    def test_exact_replay_at_capacity(self):
        for kind in self.spec["directories"]:
            with self.subTest(kind=kind):
                self.fill(kind, 256)
                before = self.snapshot(kind)
                first = self.write(kind, 255)
                self.assertTrue(first["reused"])
                self.assertIn(first["receipt_id"] + ".json", before)
                self.no_new_receipt(kind, before)
                self.assess()
                self.note("exact_replay_at_capacity", kind=kind, reused=True)

    def test_unique_refusal_preserves_names_hashes_and_assessment(self):
        for kind in self.spec["directories"]:
            with self.subTest(kind=kind):
                self.fill(kind, 256)
                before, report = self.snapshot(kind), self.assess()
                with mock.patch.object(self.b.tempfile, "TemporaryDirectory",
                                       side_effect=AssertionError("staging must not start")):
                    with self.assertRaisesRegex(self.b.BundleError, "^receipt_inventory$"):
                        self.write(kind, 256)
                self.no_new_receipt(kind, before)
                self.assertEqual(report, self.assess())
                self.note("unique_refusal_preserves_names_hashes_and_assessment", kind=kind,
                          hashes=before, assessment=report)

    def test_separate_directories_not_combined(self):
        self.fill("reviews", 256)
        self.fill("owners", 256)
        report = self.assess()
        self.assertEqual(report["review_receipts"], 256)
        self.assertFalse(report["publication_ready"])
        self.assertFalse(report["handoff_ready"])
        self.note("separate_directories_not_combined", reviews=256, owners=256)

    def test_abandoned_staging_not_counted(self):
        for kind in self.spec["directories"]:
            self.setUp()
            with self.subTest(kind=kind):
                abandoned = self.directory / kind / ".receipt-abandoned"
                abandoned.mkdir(mode=0o700)
                (abandoned / "receipt.json").write_bytes(b"synthetic incomplete staging")
                self.fill(kind, 256)
                self.assess()
                before = self.snapshot(kind)
                with self.assertRaisesRegex(self.b.BundleError, "^receipt_inventory$"):
                    self.write(kind, 256)
                self.no_new_receipt(kind, before)
                self.assertTrue(abandoned.is_dir())
                self.note("abandoned_staging_not_counted", kind=kind, committed=256)

    def test_invalid_inventory_refused(self):
        for kind in self.spec["directories"]:
            for name, is_directory, reason in (("not-a-receipt.json", False, "receipt_inventory"),
                    (".receipt-file", False, "invalid_receipt_staging"),
                    ("unexpected", True, "receipt_inventory")):
                # Each invalid inventory gets its own fresh retained root.
                self.setUp()
                with self.subTest(kind=kind, name=name):
                    path = self.directory / kind / name
                    if is_directory:
                        path.mkdir(mode=0o700)
                    else:
                        path.write_bytes(b"synthetic invalid inventory")
                        path.chmod(0o600)
                    before = self.snapshot(kind)
                    with self.assertRaisesRegex(self.b.BundleError, "^" + reason + "$"):
                        self.write(kind, 0)
                    self.no_new_receipt(kind, before)
                    with self.assertRaisesRegex(self.b.BundleError, "^" + reason + "$"):
                        self.assess()
                    self.note("invalid_inventory_refused", kind=kind, name=name, reason=reason)

    def inject_overflow(self, kind):
        # Sole mutation seam: skip the append-only capacity preflight, not the reader.
        with mock.patch.object(self.b, "_append_capacity", return_value=None, create=True):
            self.assertFalse(self.write(kind, 256)["reused"])

    def test_existing_overfull_stays_refused(self):
        for kind in self.spec["directories"]:
            self.setUp()
            with self.subTest(kind=kind):
                self.fill(kind, 256)
                self.inject_overflow(kind)
                before = self.snapshot(kind)
                self.assertEqual(len(before), 257)
                with self.assertRaisesRegex(self.b.BundleError, "^receipt_inventory$"):
                    self.assess()
                with self.assertRaisesRegex(self.b.BundleError, "^receipt_inventory$"):
                    self.write(kind, 257)
                self.no_new_receipt(kind, before)
                self.note("existing_overfull_stays_refused", kind=kind, committed=257)

    def test_declared_negative_control_and_restored_replay(self):
        for kind in self.spec["directories"]:
            self.setUp()
            with self.subTest(kind=kind):
                self.fill(kind, 256)
                before = self.snapshot(kind)
                self.assess()  # All unrelated prerequisites are valid before mutation.
                self.inject_overflow(kind)
                with self.assertRaisesRegex(AssertionError, "no-new-receipt assertion") as killed:
                    self.no_new_receipt(kind, before)
                self.assertEqual(len(self.snapshot(kind)), 257)
                with self.assertRaisesRegex(self.b.BundleError, "^receipt_inventory$"):
                    self.assess()
                self.note("negative_control", kind=kind, deciding_assertion=str(killed.exception),
                          mutated_count=257)
                # Do not repair/delete the overfull control. Restore on a fresh root.
                self.setUp()
                self.fill(kind, 256)
                before = self.snapshot(kind)
                self.assertTrue(self.write(kind, 255)["reused"])
                self.no_new_receipt(kind, before)
                self.assess()
                self.note("restored_positive", kind=kind, reused=True, committed=256)


def run_suite(report_path, discovery=False):
    import os
    import socket
    import subprocess
    import sys
    # An unrelated retained peer fixture and ambient state bracket discovery AND
    # execution, so a passing suite cannot conceal contamination of its peers.
    ambient = (dict(os.environ), tempfile.tempdir, list(sys.path), Path.cwd(), socket.socket, subprocess.Popen)
    sentinel = Path(tempfile.gettempdir()) / "unrelated-discovery-sentinel.txt"
    if discovery:
        sentinel.write_text("unrelated synthetic peer\n")
    # Scoped process-local offline guards precede all product imports.
    def denied(*args, **kwargs):
        raise RuntimeError("fc10 offline boundary: external call forbidden")
    with mock.patch.object(socket, "socket", denied), mock.patch.object(socket, "create_connection", denied), \
            mock.patch.object(subprocess, "Popen", denied):
        if discovery:
            before = (dict(os.environ), tempfile.tempdir, list(sys.path), Path.cwd(), socket.socket)
            suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent),
                pattern=Path(__file__).name, top_level_dir=str(Path(__file__).resolve().parents[2]))
            after = (dict(os.environ), tempfile.tempdir, list(sys.path), Path.cwd(), socket.socket)
            if before != after:
                raise RuntimeError("discovery contaminated ambient state")
        else:
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(ReceiptCapTests)
        class Result(unittest.TextTestResult):
            def startTest(self, test):
                ids.append(test.id())
                super().startTest(test)
        ids = []
        result = unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    if discovery:
        after = (dict(os.environ), tempfile.tempdir, list(sys.path), Path.cwd(), socket.socket, subprocess.Popen)
        if ambient != after or sentinel.read_text() != "unrelated synthetic peer\n":
            raise RuntimeError("suite contaminated unrelated peer state")
    report = {"tests": result.testsRun, "test_ids": ids,
              "failures": [(str(t), text) for t, text in result.failures],
              "errors": [(str(t), text) for t, text in result.errors],
              "skips": [(str(t), text) for t, text in result.skipped],
              "observations": OBSERVATIONS, "discovery_sentinel_unchanged": discovery}
    Path(report_path).write_text(json.dumps(report, indent=2) + "\n")
    return int(not result.wasSuccessful() or bool(result.skipped))


if __name__ == "__main__":
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--temp-root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    args.temp_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    tempfile.tempdir = str(args.temp_root.resolve())
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    raise SystemExit(run_suite(args.report))
