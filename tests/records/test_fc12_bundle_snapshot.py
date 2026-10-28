"""FC12 receipt-normalized evidence bundle snapshot experiment; imports are discovery-inert.

Synthetic only: the fixture describes fictional receipts. No real records, receipt
reader, storage path, model, network or ledger is used. Each case builds a valid
format-extraction-v1 receipt (or unit/bundle inputs) with exactly derived hashes,
calls the real API, then mutates either a caller input or the returned value and
requires the other side to keep identical canonical bytes. The deciding assertion
for every case is the snapshot immutability check that fails with KILL_MESSAGE.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc12-bundle-snapshot/cases.json"
MODULE = "tests.records.test_fc12_bundle_snapshot"
KILL_MESSAGE = "snapshot_aliased_to_caller:"
SEAM = "detach"
# Deciding case tests plus unchanged-behavior controls; run nested for controls/baselines.
CASE_TESTS = ("test_unit_cases", "test_bundle_cases", "test_receipt_cases",
              "test_repeated_equal_inputs_and_derived_hashes", "test_tamper_rejected_without_reseal",
              "test_count_conservation")


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expect(data["schema"] == "fc12-bundle-snapshot/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until the dedicated runner/child has redirected tempfile.tempdir.
    from campaign_tool.records.understanding import evidence
    return evidence


# Independent derivation (test-owned, not the product helpers).
def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else encode(value)).hexdigest()


class World:
    """Independently constructed fictional inputs with exactly derived hashes."""

    def __init__(self, data):
        base = data["base"]
        self.data = data
        self.original = hashlib.sha256(base["original_text"].encode("utf-8")).hexdigest()
        self.other = hashlib.sha256(base["other_original_text"].encode("utf-8")).hexdigest()
        receipt = copy.deepcopy(base["receipt"])
        receipt["original_sha256"] = self.original
        self.receipt_template = receipt
        self.receipt_sha = digest(receipt)

    def receipt(self):
        return copy.deepcopy(self.receipt_template)

    def extra_context(self):
        return copy.deepcopy(self.data["base"]["extra_context"])

    def raw_units(self):
        return [copy.deepcopy(u) for u in self.receipt_template["units"] if set(u) == {"kind", "locator", "text", "data"}]

    def resolve(self, value, evidence):
        if value == "<original>":
            return self.original
        if value == "<other>":
            return self.other
        if value == "<receipt_sha>":
            return self.receipt_sha
        if value == "<extra_raw_unit>":
            return copy.deepcopy(self.data["base"]["extra_raw_unit"])
        if value == "<extra_unit>":
            return evidence.unit(copy.deepcopy(self.data["base"]["extra_raw_unit"]), self.original,
                                 self.receipt_sha, 3)
        if isinstance(value, dict):
            return {k: self.resolve(v, evidence) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v, evidence) for v in value]
        return value

    def bundle_inputs(self, evidence):
        units = [evidence.unit(raw, self.original, self.receipt_sha, n) for n, raw in enumerate(self.raw_units(), 1)]
        context = {self.original: {"format": "pdf", "pages": copy.deepcopy(self.receipt_template["pages"]),
                                   "notes": {"labels": ["fictional"]}}}
        receipts = [{"original_sha256": self.original, "receipt_sha256": self.receipt_sha, "attempt_kind": "native",
                     "declared_unit_count": len(units), "normalized_unit_count": len(units)}]
        return {"units": units, "source_context": context, "source_receipts": receipts}

    def expected_bundle(self, receipt, extra):
        """Whole expected normalize_receipt output derived without product helpers."""
        imp = digest(["receipt-bound-derivative-v1", self.original, self.receipt_sha, "native"])
        units = []
        for ordinal, raw in enumerate(receipt["units"], 1):
            if set(raw) != {"kind", "locator", "text", "data"}:
                continue
            art = digest(raw)
            units.append({"unit_id": digest([imp, ordinal, art]), "original_sha256": self.original, "import_id": imp,
                          "artifact_sha256": art, "text_sha256": digest(raw["text"].encode("utf-8")),
                          **copy.deepcopy(raw), "ordinal": ordinal, "receipt_sha256": self.receipt_sha,
                          "attempt_kind": "native", "authority_basis": "receipt_bound_derivative_not_canonical"})
        counts = self.data["base"]["expected_counts"]
        context = {"format": receipt["route"]["format"], "native_status": receipt["status"],
                   "pages": copy.deepcopy(receipt["pages"]), "declared_unit_count": counts["declared_unit_count"],
                   "normalized_unit_count": counts["normalized_unit_count"],
                   "normalization_gaps": copy.deepcopy(counts["normalization_gaps"]), "parser": receipt["parser"],
                   "parser_version": receipt["parser_version"], "native_receipt_sha256": self.receipt_sha,
                   "review_acceptance": False, "semantic_acceptance": False, **copy.deepcopy(extra)}
        body = {"schema": "records-understanding-input-v1",
                "units": sorted(units, key=lambda u: (u["original_sha256"], u["receipt_sha256"], u["ordinal"])),
                "source_context": {self.original: context},
                "source_receipts": [{"original_sha256": self.original, "receipt_sha256": self.receipt_sha,
                                     "attempt_kind": "native", "declared_unit_count": counts["declared_unit_count"],
                                     "normalized_unit_count": counts["normalized_unit_count"]}]}
        return {**body, "snapshot_sha256": digest(body)}


def walk(root, path, world, evidence):
    keys = [world.resolve(p, evidence) for p in path]
    node = root
    for key in keys[:-1]:
        node = node[key]
    return node, keys[-1]


def apply(root, case, world, evidence):
    parent, key = walk(root, case["path"], world, evidence)
    op = case["op"]
    if op == "set":
        parent[key] = world.resolve(case["value"], evidence)
        return
    target = parent[key]
    expect(isinstance(target, list), "list_operation_target:" + case["id"])
    before = list(target)
    if op == "append":
        target.append(world.resolve(case["value"], evidence))
    elif op == "pop":
        target.pop()
    else:
        expect(op == "reverse", "known_op:" + op)
        target.reverse()
    expect(target != before, "operation_must_change_value:" + case["id"])


class BundleSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.coverage = []
        self.data = fixture()
        self.evidence = product()
        self.world = World(self.data)

    def cases(self, target):
        return [c for c in self.data["cases"] if c["target"] == target]

    def call(self, target):
        """Return (caller input root, returned value) for one fresh real-API call."""
        ev, w = self.evidence, self.world
        if target == "unit":
            root = w.raw_units()[0]
            return root, ev.unit(root, w.original, w.receipt_sha, 1)
        if target == "bundle":
            root = w.bundle_inputs(ev)
            return root, ev.bundle(root["units"], root["source_context"], root["source_receipts"])
        root = {"receipt": w.receipt(), "extra_context": w.extra_context()}
        return root, ev.normalize_receipt(root["receipt"], w.original, w.receipt_sha,
                                          extra_context=root["extra_context"])

    def check_case(self, case):
        ev = self.evidence
        root, returned = self.call(case["target"])
        if case["target"] != "unit":
            self.assertIs(ev.validate_bundle(returned), returned)
        inputs_before, output_before = encode(root), encode(returned)
        if case["direction"] == "input":
            apply(root, case, self.world, ev)
            self.assertNotEqual(encode(root), inputs_before, case["id"] + ": mutation must change the input")
            # Deciding snapshot immutability assertion.
            if encode(returned) != output_before:
                self.fail(KILL_MESSAGE + case["id"])
            if case["target"] == "unit":
                raw = {k: returned[k] for k in ("kind", "locator", "text", "data")}
                self.assertEqual(ev.unit(raw, self.world.original, self.world.receipt_sha, 1), returned)
            else:
                self.assertIs(ev.validate_bundle(returned), returned)
        else:
            apply(returned, case, self.world, ev)
            self.assertNotEqual(encode(returned), output_before, case["id"] + ": mutation must change the output")
            if encode(root) != inputs_before:
                self.fail(KILL_MESSAGE + case["id"])
            if case["target"] != "unit":
                with self.assertRaises(ValueError) as caught:
                    ev.validate_bundle(returned)
                self.assertEqual(str(caught.exception), "bundle_snapshot_mismatch")
        self.coverage.append("case:" + case["id"])
        self.coverage.extend(case["dimensions"])

    def run_cases(self, target):
        cases = self.cases(target)
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case["id"]):
                self.check_case(case)

    def test_unit_cases(self):
        self.run_cases("unit")

    def test_bundle_cases(self):
        self.run_cases("bundle")

    def test_receipt_cases(self):
        self.run_cases("receipt")

    def test_repeated_equal_inputs_and_derived_hashes(self):
        ev, w = self.evidence, self.world
        first = ev.normalize_receipt(w.receipt(), w.original, w.receipt_sha, extra_context=w.extra_context())
        second = ev.normalize_receipt(World(fixture()).receipt(), w.original, w.receipt_sha,
                                      extra_context=w.extra_context())
        self.assertIsNot(first, second)
        self.assertEqual(encode(first), encode(second))
        self.assertEqual(first["snapshot_sha256"], second["snapshot_sha256"])
        self.assertEqual([u["unit_id"] for u in first["units"]], [u["unit_id"] for u in second["units"]])
        self.coverage.append("repeated_equal_hashes")
        expected = w.expected_bundle(w.receipt(), w.extra_context())
        self.assertEqual(encode(first), encode(expected))
        self.assertEqual(first["snapshot_sha256"], expected["snapshot_sha256"])
        # Direct unit and bundle constructors over the same detached values give the same identities.
        inputs = w.bundle_inputs(ev)
        self.assertEqual([u["unit_id"] for u in inputs["units"]], [u["unit_id"] for u in expected["units"]])
        built = ev.bundle(inputs["units"], inputs["source_context"], inputs["source_receipts"])
        again = ev.bundle(*(w.bundle_inputs(ev)[k] for k in ("units", "source_context", "source_receipts")))
        self.assertEqual(encode(built), encode(again))
        body = {k: v for k, v in built.items() if k != "snapshot_sha256"}
        self.assertEqual(built["snapshot_sha256"], digest(body))
        self.coverage.append("derived_hashes_exact")

    def test_tamper_rejected_without_reseal(self):
        ev, w = self.evidence, self.world
        value = ev.normalize_receipt(w.receipt(), w.original, w.receipt_sha, extra_context=w.extra_context())
        self.assertIs(ev.validate_bundle(value), value)
        tampered = copy.deepcopy(value)
        tampered["units"][0]["text"] = "altered fictional text"
        before = encode(tampered)
        with self.assertRaises(ValueError) as caught:
            ev.validate_bundle(tampered)
        self.assertEqual(str(caught.exception), "bundle_snapshot_mismatch")
        self.assertEqual(encode(tampered), before)  # the validator never copies or reseals
        resealed = copy.deepcopy(tampered)
        resealed["snapshot_sha256"] = digest({k: v for k, v in resealed.items() if k != "snapshot_sha256"})
        with self.assertRaises(ValueError) as caught:
            ev.validate_bundle(resealed)
        self.assertEqual(str(caught.exception), "unit_binding_mismatch")
        self.coverage.extend(["tamper_rejected", "validator_not_resealing"])

    def test_count_conservation(self):
        ev, w = self.evidence, self.world
        receipt = w.receipt()
        value = ev.normalize_receipt(receipt, w.original, w.receipt_sha)
        counts = self.data["base"]["expected_counts"]
        context = value["source_context"][w.original]
        self.assertEqual(context["declared_unit_count"], counts["declared_unit_count"])
        self.assertEqual(context["normalized_unit_count"], counts["normalized_unit_count"])
        self.assertEqual(context["normalization_gaps"], counts["normalization_gaps"])
        self.assertEqual(len(value["units"]), counts["normalized_unit_count"])
        [source] = value["source_receipts"]
        self.assertEqual((source["declared_unit_count"], source["normalized_unit_count"]),
                         (counts["declared_unit_count"], counts["normalized_unit_count"]))
        broken = copy.deepcopy(value)
        broken["source_receipts"][0]["normalized_unit_count"] = 1
        broken["snapshot_sha256"] = digest({k: v for k, v in broken.items() if k != "snapshot_sha256"})
        with self.assertRaises(ValueError) as caught:
            ev.validate_bundle(broken)
        self.assertEqual(str(caught.exception), "receipt_count_mismatch")
        self.coverage.append("count_conservation")

    def test_detachment_negative_control_and_restore(self):
        ev = self.evidence
        declared = getattr(ev, SEAM)
        with mock.patch.object(ev, SEAM, lambda value: value):  # identity at ONLY the detachment seam
            mutant = nested_result()
        self.assertIs(getattr(ev, SEAM), declared)
        killed = sorted(killed_ids(mutant))
        expected = expected_kills(self.data)
        self.assertEqual(mutant.errors, [])
        self.assertEqual(mutant.testsRun, len(CASE_TESTS))
        self.assertEqual(killed, expected)
        self.assertEqual(len(mutant.failures), len(expected))
        self.coverage.append("negative_control_killed")
        restored = nested_result()
        save(Path(tempfile.mkdtemp(prefix="fc12-control-")) / "control-observation.json", {
            "label": "INJECTED_CONTROL_INFORMATIONAL", "seam": SEAM, "replacement": "identity",
            "mutant": result_summary(mutant), "restored": result_summary(restored)})
        # Restored: canonical equality, derived hashes and original tamper rejection all pass again.
        self.assertEqual((restored.failures, restored.errors, restored.skipped), ([], [], []))
        self.assertEqual(restored.testsRun, len(CASE_TESTS))
        self.coverage.append("negative_control_restored")


def expected_kills(data):
    return sorted(c["id"] for c in data["cases"] if c["new_gate"])


def nested_result():
    suite = unittest.TestSuite(BundleSnapshotTests(name) for name in CASE_TESTS)
    result = unittest.TestResult()
    suite.run(result)
    return result


def killed_ids(result):
    killed = []
    for _test, text in result.failures:
        line = [part for part in text.splitlines() if KILL_MESSAGE in part]
        expect(len(line) == 1, "unexpected_control_failure:" + text[-300:])
        killed.append(line[0].split(KILL_MESSAGE, 1)[1].strip())
    return killed


def result_summary(result):
    return {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "killed_case_ids": sorted(killed_ids(result)) if not result.errors else None,
            "failure_messages": [text.strip().splitlines()[-1] for _t, text in result.failures],
            "error_messages": [text.strip().splitlines()[-1] for _t, text in result.errors]}


def run_against(evidence_module):
    """Run the deciding case tests against an alternative evidence module (pinned-original baseline)."""
    with mock.patch.object(sys.modules[__name__], "product", lambda: evidence_module):
        return nested_result()


class CoverageResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dimensions = set()
        self.case_ids = set()

    def addSuccess(self, test):
        for entry in getattr(test, "coverage", []):
            (self.case_ids if entry.startswith("case:") else self.dimensions).add(entry.split("case:", 1)[-1])
        super().addSuccess(test)


def run_suite(report_path, discovery=False):
    loader = unittest.TestLoader()
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc12_bundle_snapshot.py",
                             top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".BundleSnapshotTests"))
    with (Path(report_path).parent / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CoverageResult).run(suite)
    data = fixture()
    required = set(data["required_dimensions"])
    expected_ids = {c["id"] for c in data["cases"]}
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "dimensions": sorted(result.dimensions),
               "required_dimensions": sorted(required), "missing_dimensions": sorted(required - result.dimensions),
               "case_ids_passed": sorted(result.case_ids), "case_ids_expected": len(expected_ids),
               "missing_case_ids": sorted(expected_ids - result.case_ids),
               "expected_negative_control_kills": expected_kills(data)}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"])
               or bool(summary["missing_case_ids"]))


def sentinel_discovery(report_path):
    # Snapshot BEFORE ordinary discovery, then run an unrelated peer before/after.
    old_env, old_temp, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    import socket
    import subprocess
    old_hooks = (socket.socket.connect, socket.create_connection, socket.getaddrinfo, subprocess.Popen, os.system)
    sentinel_file = Path(tempfile.gettempdir()) / "sentinel-fixture.txt"
    sentinel_file.write_text("unrelated sentinel")

    class Sentinel(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), old_env)
            self.assertEqual(tempfile.tempdir, old_temp)
            self.assertEqual(sys.path, old_path)
            self.assertEqual((socket.socket.connect, socket.create_connection, socket.getaddrinfo,
                              subprocess.Popen, os.system), old_hooks)
            self.assertEqual(sentinel_file.read_text(), "unrelated sentinel")
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Path(directory).parent, Path(old_temp))
    before = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    code = run_suite(report_path, discovery=True)
    after = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    expect(before.wasSuccessful() and after.wasSuccessful(), "discovery_peer_contamination")
    return code


class OfflineGuardRefusal(RuntimeError):
    pass


class OfflineGuard:
    """Script-child-only scoped refusal of network, listener and child-process calls.

    Installed by main() before the product import; never at module import, so ordinary
    unittest discovery stays inert. Probes refuse before any socket, DNS or process syscall.
    """
    PROBE_ADDRESS = ("192.0.2.1", 9)  # RFC 5737 documentation range; never contacted

    def __init__(self):
        import http.client
        import socket
        import subprocess
        import urllib.request
        self.modules = (socket, urllib.request, http.client, subprocess)
        self.targets = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "bind"),
                        (socket.socket, "listen"), (socket.socket, "sendto"), (socket, "create_connection"),
                        (socket, "create_server"), (socket, "getaddrinfo"), (urllib.request, "urlopen"),
                        (http.client.HTTPConnection, "connect"), (subprocess, "Popen"), (os, "system")]
        self.saved, self.blocked, self.probes, self.report = [], [], [], {}

    def _wrap(self, label):
        def guarded(*_args, **_kwargs):
            self.blocked.append(label)
            raise OfflineGuardRefusal("offline_guard_refused:" + label)
        return guarded

    def __enter__(self):
        preloaded = sorted(name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests"))
        expect(not preloaded, "product_or_test_imported_before_guard:" + ",".join(preloaded))
        for owner, name in self.targets:
            self.saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, self._wrap(getattr(owner, "__name__", "?") + "." + name))
        sock, request, client, subprocess = self.modules
        for label, call in (("socket.create_connection", lambda: sock.create_connection(self.PROBE_ADDRESS, 0.01)),
                            ("socket.getaddrinfo", lambda: sock.getaddrinfo("fc12.invalid", 9)),
                            ("socket.create_server", lambda: sock.create_server(("127.0.0.1", 0))),
                            ("urllib.request.urlopen", lambda: request.urlopen("http://192.0.2.1:9/", timeout=0.01)),
                            ("http.client.HTTPConnection.connect",
                             lambda: client.HTTPConnection(*self.PROBE_ADDRESS, timeout=0.01).connect()),
                            ("subprocess.Popen", lambda: subprocess.Popen(["true"])),
                            ("os.system", lambda: os.system("true"))):
            try:
                call()
            except OfflineGuardRefusal as error:
                self.probes.append({"probe": label, "refused": str(error)})
            else:
                raise AssertionError("offline_guard_probe_not_refused:" + label)
        expect(len(self.blocked) == len(self.probes) == 7, "offline_guard_probe_accounting")
        self.blocked = []
        self.report = {"installed_before_imports": True, "preloaded_guarded_modules": preloaded,
                       "patched": [getattr(o, "__name__", "?") + "." + n for o, n in self.targets],
                       "self_probes": list(self.probes), "permitted_children": 0}
        return self

    def __exit__(self, *exc):
        for owner, name, original in reversed(self.saved):
            setattr(owner, name, original)
        self.report.update(restored=all(getattr(o, n) is orig for o, n, orig in self.saved),
                           unexpected_blocked=list(self.blocked),
                           guarded_modules_imported_under_guard=sorted(
                               name for name in sys.modules if name.split(".")[0] in ("campaign_tool", "tests")))
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("suite", "discovery"), default="suite")
    parser.add_argument("--temp-root", required=True)
    args = parser.parse_args()
    temp = Path(args.temp_root)
    expect(temp.is_absolute() and ".." not in temp.parts and temp.is_dir(), "literal_existing_temp_root")
    tempfile.tempdir = str(temp)  # fresh isolated script process; precedes product import
    guard = OfflineGuard()
    with guard:  # offline boundary precedes the product/test import graph
        sys.path.insert(0, str(REPO))
        report = temp / "suite-summary.json"
        code = sentinel_discovery(report) if args.mode == "discovery" else run_suite(report)
    save(temp / "offline-guard.json", guard.report)
    if not guard.report["restored"] or guard.report["unexpected_blocked"]:
        return 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
