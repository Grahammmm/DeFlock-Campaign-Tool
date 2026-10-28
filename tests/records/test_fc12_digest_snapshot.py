"""FC12 redacted-unit snapshot experiment; imports are discovery-inert.

Synthetic only: the fixture describes fictional units. No real records, model,
provider, opener, network, ledger or publication is used. ``redact_units`` must
return detached JSON-compatible unit metadata: neither the caller's units nor
the returned units may change when the other is mutated, while redacted text,
the opaque ``model_locator`` and aggregate counts stay byte-equal to the frozen
expectations. Deciding failures use one message prefix so negative controls and
the pinned-original baseline are counted by case ID, not by an arbitrary error.
"""
import argparse
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc12-digest-snapshot/cases.json"
MODULE = "tests.records.test_fc12_digest_snapshot"
KILL_MESSAGE = "detachment_breach:"
CASE_TESTS = ("test_empty_units", "test_ordinary_outputs_byte_equal", "test_caller_mutation_after_return",
              "test_returned_mutation_does_not_reach_caller", "test_repeated_calls", "test_shared_subobject",
              "test_digest_ordinary_values", "test_digest_nested_snapshot", "test_inputs_unchanged")
# The single named seam: the new deep detachment versus the prior shallow unit copy.
DEEP_SEAM = "{**copy.deepcopy(unit), "
SHALLOW_SEAM = "{**unit, "
CONTROL_OBSERVATIONS = []  # filled only by the negative-control test; no file writes on ordinary runs


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expect(data["schema"] == "fc12-digest-snapshot/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until the dedicated runner/child has redirected tempfile.tempdir and installed guards.
    from campaign_tool.digest import build
    return build


def package():
    from campaign_tool import law
    return law.load_package("us-ca")


def validator():
    from campaign_tool.digest.schema import validate_digest
    return validate_digest


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")


def set_path(tree, path, value):
    for part in path[:-1]:
        tree = tree[part]
    tree[path[-1]] = value


def get_path(tree, path):
    for part in path:
        tree = tree[part]
    return tree


def case_units(data, case_id):
    return copy.deepcopy(next(c for c in data["unit_cases"] if c["id"] == case_id)["units"])


def deciding_case_ids(data):
    ids = ["caller:" + m["id"] for m in data["mutations"]] + ["returned:" + m["id"] for m in data["mutations"]]
    return sorted(ids + ["shared:sibling", "shared:caller", "repeat:nested", "digest:caller", "digest:returned"])


def expected_kills(data):
    kills = ["caller:" + m["id"] for m in data["mutations"] if m["new_gate"]]
    kills += ["returned:" + m["id"] for m in data["mutations"] if m["new_gate"]]
    return sorted(kills + ["shared:sibling", "shared:caller", "repeat:nested", "digest:caller", "digest:returned"])


def digest_units(data):
    spec = data["digest"]
    return [{"locator": {"line": n, "source": copy.deepcopy(spec["source"])}, "text": line}
            for n, line in enumerate(spec["lines"], 1)]


class DigestSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.coverage = []
        self.data = fixture()
        self.build = product()

    def redact(self, units):
        return self.build.redact_units(units)

    def digest(self, units):
        spec = self.data["digest"]
        sha256 = hashlib.sha256("\n".join(spec["lines"]).encode("utf-8")).hexdigest()
        return self.build.build_digest(sha256, units, package(), spec["jurisdiction"], spec["law_package_version"],
                                       spec["privacy_tier"])

    def breach(self, identifier, ok):
        if not ok:
            self.fail(KILL_MESSAGE + identifier)
        self.coverage.append("case:" + identifier)

    def assert_expected(self, case_id, result):
        redacted, counts = result
        expected = self.data["expected"][case_id]
        self.assertEqual(encoded(redacted), encoded(expected["units"]), case_id + ": redacted units")
        self.assertEqual(encoded(counts), encoded(expected["counts"]), case_id + ": counts")

    def test_empty_units(self):
        units = []
        result = self.redact(units)
        self.assertEqual(result, ([], {}))
        self.assertIsNot(result[0], units)
        self.assertEqual(units, [])
        self.coverage.append("empty_units")

    def test_ordinary_outputs_byte_equal(self):
        for case in self.data["unit_cases"]:
            with self.subTest(case=case["id"]):
                units = case_units(self.data, case["id"])
                before = encoded(units)
                self.assert_expected(case["id"], self.redact(units))
                self.assertEqual(encoded(units), before, case["id"] + ": input mutated")
                self.coverage.append(case["dimension"])
        text = "\n".join(u["text"] for u in self.data["expected"]["name_email"]["units"])
        self.assertEqual((text.count("[NAME-1]"), text.count("[EMAIL-1]")), (2, 2))
        self.assertNotIn("Robin", text)
        self.assertNotIn("example.invalid", text)

    def test_caller_mutation_after_return(self):
        for mutation in self.data["mutations"]:
            identifier = "caller:" + mutation["id"]
            with self.subTest(case=identifier):
                units = case_units(self.data, self.data["mutation_case"])
                result = self.redact(units)
                self.assert_expected(self.data["mutation_case"], result)
                snapshot = encoded(result)
                set_path(units, mutation["path"], copy.deepcopy(mutation["value"]))
                self.assertEqual(get_path(units, mutation["path"]), mutation["value"])
                self.breach(identifier, encoded(result) == snapshot)
        self.coverage.append("caller_mutation")

    def test_returned_mutation_does_not_reach_caller(self):
        for mutation in self.data["mutations"]:
            identifier = "returned:" + mutation["id"]
            with self.subTest(case=identifier):
                units = case_units(self.data, self.data["mutation_case"])
                before = encoded(units)
                result = self.redact(units)
                self.assert_expected(self.data["mutation_case"], result)
                set_path(result[0], mutation["path"], copy.deepcopy(mutation["value"]))
                self.breach(identifier, encoded(units) == before)
        self.coverage.append("returned_mutation")

    def test_repeated_calls(self):
        spec = self.data["repeat"]
        units = case_units(self.data, spec["case"])
        before = encoded(units)
        first, second = self.redact(units), self.redact(units)
        self.assertEqual(encoded(first), encoded(second))
        self.assert_expected(spec["case"], first)
        self.assertEqual(encoded(units), before)
        set_path(first[0], spec["mutation"]["path"], spec["mutation"]["value"])
        with self.subTest(case="repeat:nested"):
            self.breach("repeat:nested", encoded(second[0]) == encoded(self.data["expected"][spec["case"]]["units"]))
        self.coverage.append("repeated_calls")

    def test_shared_subobject(self):
        spec = self.data["shared"]
        shared = copy.deepcopy(spec["source"])
        units = [{"locator": {"line": n, "source": shared}, "text": text} for n, text in enumerate(spec["texts"], 1)]
        before = encoded(units)
        result = self.redact(units)
        self.assert_expected("shared", result)
        self.assertEqual(encoded(units), before)
        set_path(result[0], spec["mutation"]["path"], spec["mutation"]["value"])
        expected_second = self.data["expected"]["shared"]["units"][1]
        with self.subTest(case="shared:sibling"):
            self.breach("shared:sibling", encoded(result[0][1]) == encoded(expected_second))
        with self.subTest(case="shared:caller"):
            self.breach("shared:caller", encoded(units) == before)
        self.coverage.append("shared_subobject")

    def check_digest_values(self, digest):
        expected = self.data["expected"]["digest"]
        validator()(copy.deepcopy(digest))
        self.assertEqual(digest["redaction_counts"], expected["redaction_counts"])
        self.assertEqual(digest["redaction_count"], expected["redaction_count"])
        self.assertEqual(digest["model_id"], expected["model_id"])
        self.assertEqual(len(digest["hits"]), expected["hit_count"])
        self.assertEqual(len(digest["statements"]), expected["statement_count"])
        self.assertEqual([d["rule_id"] for d in digest["duties"]], expected["duty_rule_ids"])
        self.assertEqual(sorted({c["confidence"] for c in digest["conclusions"]}), expected["confidences"])
        retention = [h for h in digest["hits"] if h["detector"] == "retention_period"]
        self.assertEqual(len(retention), 1)
        self.assertEqual(encoded(retention[0]["locator"]), encoded(expected["retention_locator"]))
        dumped = json.dumps(digest)
        self.assertNotIn("Robin", dumped)
        self.assertNotIn("example.invalid", dumped)
        return retention[0]

    def test_digest_ordinary_values(self):
        units = digest_units(self.data)
        pristine = copy.deepcopy(units)
        before = encoded(units)
        digest = self.digest(units)
        self.check_digest_values(digest)
        self.assertEqual(encoded(units), before)
        self.assertEqual(encoded(self.digest(pristine)), encoded(digest))
        self.coverage.append("digest_ordinary")

    def test_digest_nested_snapshot(self):
        spec = self.data["digest"]
        units = digest_units(self.data)
        digest = self.digest(units)
        self.check_digest_values(digest)
        snapshot = encoded(digest)
        set_path(units[spec["caller_mutation"]["unit"]], spec["caller_mutation"]["path"],
                 spec["caller_mutation"]["value"])
        with self.subTest(case="digest:caller"):
            self.breach("digest:caller", encoded(digest) == snapshot)
        units = digest_units(self.data)
        before = encoded(units)
        retention = self.check_digest_values(self.digest(units))
        set_path(retention, spec["returned_mutation"]["path"], spec["returned_mutation"]["value"])
        with self.subTest(case="digest:returned"):
            self.breach("digest:returned", encoded(units) == before)
        self.coverage.append("digest_nested_snapshot")

    def test_inputs_unchanged(self):
        # Pass the fixture's own objects (no defensive copy) and prove every call leaves them byte-equal.
        data = fixture()
        before = encoded(data)
        for case in data["unit_cases"]:
            self.redact(case["units"])
        units = digest_units(data)
        units_before = encoded(units)
        self.digest(units)
        self.assertEqual(encoded(units), units_before)
        self.assertEqual(encoded(data), before)
        self.assertEqual(len(deciding_case_ids(data)), 2 * len(data["mutations"]) + 5)
        self.coverage.append("inputs_unchanged")

    def test_shallow_negative_control_and_restore(self):
        declared = self.build.redact_units
        control = shallow_control(self.build)
        with mock.patch.object(self.build, "redact_units", control):
            mutant = nested_result()
        self.assertIs(self.build.redact_units, declared)
        expected = expected_kills(self.data)
        self.assertEqual(mutant.errors, [])
        self.assertEqual(mutant.testsRun, len(CASE_TESTS))
        self.assertEqual(sorted(killed_ids(mutant)), expected)
        self.assertEqual(len(mutant.failures), len(expected))
        self.coverage.append("negative_control_killed")
        restored = nested_result()
        # Kept in memory only; run_suite() writes it into the runner's row-local report directory.
        CONTROL_OBSERVATIONS.append({
            "label": "INJECTED_CONTROL_INFORMATIONAL", "seam": {"candidate": DEEP_SEAM, "control": SHALLOW_SEAM},
            "mutant": result_summary(mutant), "restored": result_summary(restored)})
        self.assertEqual((restored.failures, restored.errors, restored.skipped), ([], [], []))
        self.assertEqual(restored.testsRun, len(CASE_TESTS))
        self.coverage.append("negative_control_restored")


def shallow_control(build):
    """Isolated function copy mechanically derived from the staged source; ONLY the named seam changes."""
    source = inspect.getsource(build.redact_units)
    expect(source.count(DEEP_SEAM) == 1 and SHALLOW_SEAM not in source, "deep_seam_expected_once")
    control_source = source.replace(DEEP_SEAM, SHALLOW_SEAM)
    expect(control_source.replace(SHALLOW_SEAM, DEEP_SEAM) == source, "control_changes_more_than_seam")
    namespace = dict(build.__dict__)  # a copy: the module globals stay untouched
    exec(compile(control_source, "<fc12-shallow-control>", "exec"), namespace)
    return namespace["redact_units"]


def nested_result():
    suite = unittest.TestSuite(DigestSnapshotTests(name) for name in CASE_TESTS)
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


def run_against(build_module):
    """Run the deciding case tests against an alternative build module (pinned-original baseline)."""
    with mock.patch.object(sys.modules[__name__], "product", lambda: build_module):
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
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc12_digest_snapshot.py",
                             top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".DigestSnapshotTests"))
    with (Path(report_path).parent / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2, resultclass=CoverageResult).run(suite)
    # Loaded tests always live in the importable module (also when this file runs as __main__).
    observations = list(getattr(sys.modules.get(MODULE), "CONTROL_OBSERVATIONS", []))
    save(Path(report_path).parent / "control-observation.json", {"observations": observations})
    data = fixture()
    required = set(data["required_dimensions"])
    expected_ids = set(deciding_case_ids(data))
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skipped": len(result.skipped), "dimensions": sorted(result.dimensions),
               "required_dimensions": sorted(required), "missing_dimensions": sorted(required - result.dimensions),
               "case_ids_passed": sorted(result.case_ids), "case_ids_expected": sorted(expected_ids),
               "missing_case_ids": sorted(expected_ids - result.case_ids),
               "expected_negative_control_kills": expected_kills(data),
               "control_observations": len(observations)}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"])
               or bool(summary["missing_case_ids"]) or len(observations) != 1)


def sentinel_discovery(report_path):
    # Snapshot BEFORE ordinary discovery, then run an unrelated peer before and after it.
    import socket
    import subprocess
    old_env, old_temp, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
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

    Installed by main() before the product import, never at module import, so ordinary
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
    tempfile.tempdir = str(temp)  # fresh isolated script process; precedes the product import
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
