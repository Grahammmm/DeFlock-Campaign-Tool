"""FC11 cumulative model-source character budgets and current-view citation issuance.

Synthetic only: the fixture describes fictional units. No real records, model,
provider, network, ledger or publication is used. ``ModelSources.source_payload``
truncates at whole-unit boundaries, counting each serialized record as its JSON
reference, TAB, redacted text and one trailing newline, in characters. Only the
units in the latest view are citable; a detector hit from an omitted unit issues
exactly its own line, and a newer shorter view clears every earlier issuance.

Imports are discovery-inert: no ambient mutation or file write at import time.
Deciding failures share one message prefix so the declared negative controls and
the pinned-original baseline are counted by case ID, never by an arbitrary error.
"""
import argparse
import copy
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc11-model-payload-budget/cases.json"
MODULE = "tests.records.test_fc11_model_payload_budget"
KILL_MESSAGE = "budget_breach:"
DOCUMENT = {"scope": "document"}
CASE_TESTS = ("test_record_costs_frozen", "test_matrix_fresh", "test_matrix_shared_descending",
              "test_cycle_same_instance", "test_hit_line_only", "test_hit_cleared_by_shorter_view",
              "test_inputs_unchanged")
CONTROLS = ("union_issued", "uncounted_newline")
CONTROL_OBSERVATIONS = []  # filled only by the negative-control test; no file writes on ordinary runs
REFUSED = object()


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expect(data["schema"] == "fc11-model-payload-budget/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until the dedicated runner/child has redirected tempfile.tempdir and installed guards.
    from campaign_tool.digest import model_input
    return model_input


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")


def output(reference):
    """A model-shaped digest output citing ``reference`` in every unit-citing field."""
    return {"scope": "synthetic", "actors": [],
            "dates": [{"date": "2026-01-01", "locator": copy.deepcopy(reference)}],
            "statements": [{"text": "synthetic", "locator": copy.deepcopy(reference)}],
            "omissions": [{"text": "synthetic", "locator": dict(DOCUMENT)}],
            "counterevidence": [{"text": "synthetic", "locator": copy.deepcopy(reference)}],
            "conclusions": [{"text": "synthetic", "confidence": "needs_attorney_review",
                             "sources": [{"locator": copy.deepcopy(reference), "rule_id": None}]}]}


def cumulative(costs):
    totals, running = [], 0
    for cost in costs:
        running += cost
        totals.append(running)
    return totals


def admitted(budget, costs, slack=0):
    """Independent model: record k is admitted iff its cumulative cost minus ``slack`` fits."""
    if budget is None:
        return len(costs)
    return sum(1 for total in cumulative(costs) if total - slack <= budget)


def view_ids(prefix, n, real, units=3):
    """Deciding-check IDs a view with ``real`` emitted units would differ on if ``n`` were admitted."""
    ids = []
    if n != real:
        ids.append(prefix + ":prefix")
    if (n < units) != (real < units):
        ids.append(prefix + ":truncated")
    for index in range(min(n, real), max(n, real)):
        kind = "refuse" if index >= real else "cite"
        ids += [prefix + ":" + kind + ":" + str(index), prefix + ":" + kind + "_line:" + str(index)]
    return ids


def expected_kills(data, control):
    """Deciding case IDs each declared control must fail, derived from the fixture alone."""
    costs, kills = data["record_costs"], []
    cases = data["budget_cases"]
    if control == "uncounted_newline":
        for label, order in (("fresh", cases), ("shared", list(reversed(cases)))):
            for case in order:
                real = admitted(case["budget"], costs)
                kills += view_ids(label + ":" + case["id"], admitted(case["budget"], costs, slack=1), real)
        for step in data["cycle"]:
            real = admitted(step["budget"], costs)
            kills += view_ids("cycle:" + step["id"], admitted(step["budget"], costs, slack=1), real)
        # The hit/clear budget is an exact first-record budget, so the control admits the same view there.
        expect(admitted(data["hit"]["budget"], costs, slack=1) == admitted(data["hit"]["budget"], costs),
               "hit_budget_must_not_decide_newline_control")
    elif control == "union_issued":
        for label, order in (("shared", list(reversed(cases))), ("cycle", data["cycle"])):
            widest = 0
            for case in order:
                real = admitted(case["budget"], costs)
                widest = max(widest, real)  # a union keeps every earlier view's units issued
                for index in range(real, widest):
                    kills += [label + ":" + case["id"] + ":refuse:" + str(index),
                              label + ":" + case["id"] + ":refuse_line:" + str(index)]
        kills.append("clear:refuse_exact")  # the earlier hit line survives a union
    else:
        raise AssertionError("unknown_control")
    return sorted(kills)


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.coverage = []
        self.data = fixture()
        self.mi = product()
        self.records = self.data["serialized_records"]

    def check(self, identifier, ok):
        with self.subTest(case=identifier):
            if not ok:
                self.fail(KILL_MESSAGE + identifier)
            self.coverage.append("case:" + identifier)

    def sources(self, units=None):
        return self.mi.ModelSources(copy.deepcopy(self.data["units"]) if units is None else units)

    def restored(self, sources, reference):
        document = output(reference)
        before = encoded(document)
        try:
            result = sources.restore_output(document)
        except self.mi.ModelInputError:
            result = REFUSED
        self.assertEqual(encoded(document), before, "restore_output mutated its input")
        return result

    def citable(self, sources, reference, locator):
        result = self.restored(sources, reference)
        if result is REFUSED:
            return False
        cited = [result["dates"][0]["locator"], result["statements"][0]["locator"],
                 result["counterevidence"][0]["locator"], result["conclusions"][0]["sources"][0]["locator"]]
        return all(item == locator for item in cited) and result["omissions"][0]["locator"] == DOCUMENT

    def refused(self, sources, reference):
        return self.restored(sources, reference) is REFUSED

    def locator(self, index, line=None):
        locator = copy.deepcopy(self.data["units"][index]["locator"])
        if line is not None:
            locator["line"] = line
        return locator

    def view_checks(self, prefix, sources, payload, n, truncated):
        self.assertEqual(set(payload), {"document_locator", "record_text", "truncated"})
        self.check(prefix + ":prefix", payload["record_text"] == self.records[:n])
        self.check(prefix + ":truncated", payload["truncated"] is truncated)
        self.check(prefix + ":document", payload["document_locator"] == DOCUMENT
                   and self.citable(sources, DOCUMENT, DOCUMENT))
        for index in range(len(self.records)):
            if index < n:
                self.check(prefix + ":cite:" + str(index),
                           self.citable(sources, {"unit": index}, self.locator(index)))
                self.check(prefix + ":cite_line:" + str(index),
                           self.citable(sources, {"unit": index, "line": 1}, self.locator(index, 1)))
            else:
                self.check(prefix + ":refuse:" + str(index), self.refused(sources, {"unit": index}))
                self.check(prefix + ":refuse_line:" + str(index),
                           self.refused(sources, {"unit": index, "line": 1}))

    def test_record_costs_frozen(self):
        sources = self.sources()
        payload = sources.source_payload(max_chars=None)
        self.assertEqual(payload["record_text"], self.records)
        costs = self.data["record_costs"]
        self.assertEqual([len(record) + 1 for record in self.records], costs)
        self.assertEqual([len(record.encode("utf-8")) for record in self.records], self.data["record_utf8_bytes"])
        self.assertTrue(any(b > len(r) for b, r in zip(self.data["record_utf8_bytes"], self.records)),
                        "a multibyte unit must make bytes and characters differ")
        totals = cumulative(costs)
        derived = {"zero": 0, "first_minus_one": totals[0] - 1, "first_equal": totals[0],
                   "two_minus_one": totals[1] - 1, "two_equal": totals[1],
                   "total_minus_one": totals[2] - 1, "total_equal": totals[2], "unbounded": None}
        self.assertEqual({case["id"]: case["budget"] for case in self.data["budget_cases"]}, derived)
        for case in self.data["budget_cases"]:
            self.assertEqual(case["prefix_count"], admitted(case["budget"], costs), case["id"])
            self.assertEqual(case["truncated"], case["prefix_count"] < len(costs), case["id"])
        # A byte-based budget would admit fewer records at the two-record equal budget.
        self.assertLess(admitted(totals[1], [b + 1 for b in self.data["record_utf8_bytes"]]), 2)
        self.assertEqual(len({json.dumps(u["locator"], sort_keys=True) for u in self.data["units"]}), 3)
        self.coverage.append("record_costs_frozen")

    def test_matrix_fresh(self):
        for case in self.data["budget_cases"]:
            units = copy.deepcopy(self.data["units"])
            before = encoded(units)
            sources = self.sources(units)
            payload = sources.source_payload(max_chars=case["budget"])
            self.view_checks("fresh:" + case["id"], sources, payload, case["prefix_count"], case["truncated"])
            self.assertEqual(encoded(units), before, case["id"] + ": units mutated")
        self.coverage.append("matrix_fresh")

    def test_matrix_shared_descending(self):
        units = copy.deepcopy(self.data["units"])
        before = encoded(units)
        sources = self.sources(units)
        for case in reversed(self.data["budget_cases"]):
            payload = sources.source_payload(max_chars=case["budget"])
            self.view_checks("shared:" + case["id"], sources, payload, case["prefix_count"], case["truncated"])
        self.assertEqual(encoded(units), before)
        self.coverage.append("matrix_shared_descending")

    def test_cycle_same_instance(self):
        units = copy.deepcopy(self.data["units"])
        before = encoded(units)
        sources = self.sources(units)
        self.assertEqual([step["id"] for step in self.data["cycle"]], ["full_first", "partial", "empty", "full_again"])
        for step in self.data["cycle"]:
            payload = sources.source_payload(max_chars=step["budget"])
            n = step["prefix_count"]
            self.view_checks("cycle:" + step["id"], sources, payload, n, n < len(self.records))
        self.assertEqual(encoded(units), before)
        self.coverage.append("cycle_same_instance")

    def hit_payload(self, sources):
        spec = self.data["hit"]
        hits = [copy.deepcopy(spec["detector_hit"])]
        hits_before = encoded(hits)
        payload = sources.primary_payload(hits, [], max_chars=spec["budget"])
        self.assertEqual(encoded(hits), hits_before, "hits mutated")
        return payload

    def test_hit_line_only(self):
        spec = self.data["hit"]
        units = copy.deepcopy(self.data["units"])
        before = encoded(units)
        sources = self.sources(units)
        payload = self.hit_payload(sources)
        n = spec["prefix_count"]
        self.check("hit:prefix", payload["record_text"] == self.records[:n])
        self.check("hit:truncated", payload["truncated"] is (n < len(self.records)))
        hits = payload["detector_hits"]
        self.check("hit:emitted", len(hits) == 1 and hits[0]["locator"] == spec["emitted_reference"])
        self.check("hit:excerpt", len(hits) == 1 and hits[0]["excerpt"] == spec["detector_hit"]["excerpt"])
        self.check("hit:cite_exact", self.citable(sources, spec["emitted_reference"], spec["restored_locator"]))
        for name, reference in sorted(spec["refused_references"].items()):
            self.check("hit:refuse_" + name, self.refused(sources, reference))
        self.check("hit:cite:0", self.citable(sources, {"unit": 0}, self.locator(0)))
        self.assertEqual(encoded(units), before)
        self.coverage.append("hit_line_only")

    def test_hit_cleared_by_shorter_view(self):
        spec = self.data["hit"]
        units = copy.deepcopy(self.data["units"])
        before = encoded(units)
        sources = self.sources(units)
        self.hit_payload(sources)
        # Precondition, not a deciding check: the hit line was issued by the earlier request.
        self.assertTrue(self.citable(sources, spec["emitted_reference"], spec["restored_locator"]))
        payload = sources.source_payload(max_chars=spec["budget"])
        n = spec["prefix_count"]
        self.check("clear:prefix", payload["record_text"] == self.records[:n])
        self.check("clear:truncated", payload["truncated"] is (n < len(self.records)))
        self.check("clear:refuse_exact", self.refused(sources, spec["emitted_reference"]))
        self.check("clear:refuse_whole:2", self.refused(sources, {"unit": 2}))
        self.check("clear:cite:0", self.citable(sources, {"unit": 0}, self.locator(0)))
        self.assertEqual(encoded(units), before)
        self.coverage.append("hit_cleared_by_shorter_view")

    def test_inputs_unchanged(self):
        # Pass the fixture's own objects (no defensive copy) and prove every call leaves them byte-equal.
        data = fixture()
        before = encoded(data)
        sources = self.mi.ModelSources(data["units"])
        for case in data["budget_cases"]:
            sources.source_payload(max_chars=case["budget"])
        sources.primary_payload([data["hit"]["detector_hit"]], [], max_chars=data["hit"]["budget"])
        sources.restore_output(output(data["hit"]["emitted_reference"]))
        self.assertEqual(encoded(data), before)
        self.coverage.append("inputs_unchanged")

    def test_negative_controls_and_restore(self):
        declared = self.mi.ModelSources.source_payload
        observation = {"label": "INJECTED_CONTROLS_INFORMATIONAL", "controls": {}}
        for name, build in (("union_issued", union_control), ("uncounted_newline", newline_control)):
            control = build(self.mi)
            with mock.patch.object(self.mi.ModelSources, "source_payload", control):
                mutant = nested_result()
            self.assertIs(self.mi.ModelSources.source_payload, declared)
            expected = expected_kills(self.data, name)
            self.assertTrue(expected, name)
            self.assertEqual(mutant.errors, [], name)
            self.assertEqual(mutant.testsRun, len(CASE_TESTS), name)
            self.assertEqual(sorted(killed_ids(mutant)), expected, name)
            self.assertEqual(len(mutant.failures), len(expected), name)
            observation["controls"][name] = {"seam": self.data["controls"][name], "expected_kills": expected,
                                             "mutant": result_summary(mutant)}
            self.coverage.append("negative_control_" + ("union" if name == "union_issued" else "newline")
                                 + "_killed")
        restored = nested_result()
        observation["restored"] = result_summary(restored)
        # Kept in memory only; run_suite() writes it into the runner's row-local report directory.
        CONTROL_OBSERVATIONS.append(observation)
        self.assertEqual((restored.failures, restored.errors, restored.skipped), ([], [], []))
        self.assertEqual(restored.testsRun, len(CASE_TESTS))
        self.coverage.append("negative_controls_restored")


def union_control(model_input):
    """Declared control 1: a scoped wrapper that unions the previously issued set back after each view."""
    original = model_input.ModelSources.source_payload

    def source_payload(self, max_chars=60000):
        old = set(self._issued)
        result = original(self, max_chars)
        self._issued |= old
        return result
    return source_payload


def newline_control(model_input):
    """Declared control 2: an isolated copy mechanically derived from the staged source_payload.

    ONLY the budget-admission seam changes: the counted trailing newline is dropped from the cost.
    """
    seams = fixture()["controls"]["uncounted_newline"]
    source = textwrap.dedent(inspect.getsource(model_input.ModelSources.source_payload))
    expect(source.count(seams["seam_candidate"]) == 1 and seams["seam_control"] not in source,
           "newline_seam_expected_once")
    control_source = source.replace(seams["seam_candidate"], seams["seam_control"])
    expect(control_source.replace(seams["seam_control"], seams["seam_candidate"]) == source,
           "control_changes_more_than_seam")
    namespace = dict(model_input.__dict__)  # a copy: the module globals stay untouched
    exec(compile(control_source, "<fc11-uncounted-newline-control>", "exec"), namespace)
    return namespace["source_payload"]


def nested_result():
    suite = unittest.TestSuite(BudgetTests(name) for name in CASE_TESTS)
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
            "skipped": len(result.skipped),
            "killed_case_ids": sorted(killed_ids(result)) if not result.errors else None,
            "error_messages": [text.strip().splitlines()[-1] for _t, text in result.errors]}


def run_against(model_input):
    """Run the deciding case tests against an alternative module (the pinned-original baseline)."""
    with mock.patch.object(sys.modules[__name__], "product", lambda: model_input):
        return nested_result()


def deciding_case_ids(data):
    """Every deciding ID the passing suite must cover, derived from the fixture."""
    ids = []
    for label, order in (("fresh", data["budget_cases"]), ("shared", data["budget_cases"])):
        for case in order:
            ids += view_case_ids(label + ":" + case["id"], case["prefix_count"])
    for step in data["cycle"]:
        ids += view_case_ids("cycle:" + step["id"], step["prefix_count"])
    ids += ["hit:prefix", "hit:truncated", "hit:emitted", "hit:excerpt", "hit:cite_exact", "hit:cite:0"]
    ids += ["hit:refuse_" + name for name in data["hit"]["refused_references"]]
    ids += ["clear:prefix", "clear:truncated", "clear:refuse_exact", "clear:refuse_whole:2", "clear:cite:0"]
    return sorted(ids)


def view_case_ids(prefix, n, units=3):
    ids = [prefix + ":prefix", prefix + ":truncated", prefix + ":document"]
    for index in range(units):
        kind = "cite" if index < n else "refuse"
        ids += [prefix + ":" + kind + ":" + str(index), prefix + ":" + kind + "_line:" + str(index)]
    return ids


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
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc11_model_payload_budget.py",
                             top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".BudgetTests"))
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
               "unexpected_case_ids": sorted(result.case_ids - expected_ids),
               "expected_negative_control_kills": {name: expected_kills(data, name) for name in CONTROLS},
               "control_observations": len(observations)}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"])
               or bool(summary["missing_case_ids"]) or bool(summary["unexpected_case_ids"])
               or len(observations) != 1)


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
                            ("socket.getaddrinfo", lambda: sock.getaddrinfo("fc11.invalid", 9)),
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
