"""Five separately invoked FC11-MODEL-PAYLOAD-BUDGET offline acceptance rows; raw evidence is never reused.

Test-only scope: all product code is read-only. The pinned-original baseline therefore
executes the unchanged base model_input.py blob and must PASS the same deciding tests
(a test-only qualification, not a regression red); the declared negative controls supply
the deciding failures.
"""
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import traceback
import types
import uuid

REPO = Path(__file__).resolve().parents[1]
AUTHORED = ("tests/records/test_fc11_model_payload_budget.py",
            "tests/fixtures/fc11-model-payload-budget/cases.json",
            "tools/fc11_model_payload_budget_acceptance.py")
TEST_MODULE = AUTHORED[0]
PRODUCT = "campaign_tool/digest/model_input.py"
CHECKERS = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
            "tools/check_secret_report.py")
SOURCE = (PRODUCT, "campaign_tool/digest/__init__.py", "campaign_tool/digest/detectors.py",
          "campaign_tool/digest/redact.py", "campaign_tool/digest/schema.py", "tests/test_model_input.py")
SELECTORS = ("tests.test_model_input.ModelInputTests.test_truncated_sources_cannot_be_cited_without_an_issued_hit",
             "tests.test_model_input.ModelInputTests.test_later_payload_does_not_reuse_earlier_issued_references",
             "tests.test_model_input.ModelInputTests.test_invalid_opaque_references_fail_closed_in_all_citation_fields")
PINNED_ORIGINAL_NAME = "campaign_tool.digest._fc11_pinned_original"
PRODUCT_MODULE = "campaign_tool.digest.model_input"
GUARDED_PREFIXES = ("campaign_tool", "tests", "runner")
ROW_INTERPRETER = {"credentials": "<credential_scan_python3>"}


def _row_argv(phase):
    return [ROW_INTERPRETER.get(phase, "<write_root>/repo/.venv/bin/python3"),
            "<write_root>/repo/tools/fc11_model_payload_budget_acceptance.py", "--phase", phase,
            "--source-manifest", "<write_root>/SOURCE-MANIFEST.json", "--out-base", "<write_root>/out"]


# Sanitized, durable representation of the card's frozen acceptance contract. Host paths are
# templated; source pins are bound through SOURCE-MANIFEST.json and git blobs, never embedded hex.
FROZEN_SPEC = {
    "schema": "fc11-frozen-acceptance-spec/v1",
    "card": "t_c18c8395",
    "title": "FC11-MODEL-PAYLOAD-BUDGET Prove multi-unit digest payload budgets issue only citations "
             "present in the current view.",
    "type": "test-only",
    "source_pin": {"repository": "Grahammmm/DeFlock-Campaign-Tool", "branch": "main",
                   "binding": "operator SOURCE-MANIFEST.json head/tree; initial HEAD equals manifest head; "
                              "candidate descends from it; every product file byte-equal to the base blob"},
    "frozen_behavior": "existing max_chars contract measured in characters (not UTF-8 bytes, not an overall "
                       "primary-request cap); source text truncates at whole-unit boundaries; a separately "
                       "emitted detector-hit excerpt issues only its exact line citation; no model call, "
                       "transport, budget or redaction change.",
    "frozen_test_experiment": ["three distinct synthetic locators, ASCII and multibyte text",
                               "frozen serialized record costs incl. JSON reference, TAB, counted newline",
                               "zero, first-1/first, two-1/two, total-1/total, None on fresh and shared instances",
                               "exact prefix, truncated flag, current unit/line citation eligibility, inputs unchanged",
                               "same instance full -> partial -> empty -> full",
                               "omitted-unit detector hit issues only its exact line",
                               "a newer shorter source view clears the prior hit issuance"],
    "negative_controls": {
        "union_issued": "scoped wrapper unions the previously issued set back after each shorter view; the "
                        "omitted-unit restoration refusals must fail by case ID",
        "uncounted_newline": "isolated source_payload copy mechanically derived from the staged source with ONLY "
                             "the admission seam dropping the counted newline; the one-character-under prefix "
                             "assertions must fail by case ID"},
    "pinned_original_baseline": "the base model_input.py blob runs in memory as an unregistered module; test-only "
                                "scope means it must PASS the same deciding tests, byte-equal to the candidate file",
    "exclusive_write_set": list(AUTHORED),
    "compatibility_selectors": list(SELECTORS),
    "acceptance_cwd": "<write_root>/repo",
    "rows": {phase: _row_argv(phase) for phase in ("suite", "compatibility", "portable", "credentials",
                                                    "public-tree")},
    "offline_guards": "scoped in-process refusal of network, listener and child-process calls installed "
                      "before any product or test import; only the runner's literal git/scanner/test-script "
                      "children are permitted; restored on exit",
    "expected_result": "All five separate rows exit zero with genuine hashed receipts. In-place staging "
                       "diagnostics are informational only. REVIEW REQUIRED, not semantic/legal/human/live "
                       "acceptance.",
    "limits": ["full discovery is operator-run", "remote push, PR and exact-head CI are OWED to the operator",
               "no push, merge, publication, outbound communication or hold change"],
}


def frozen_spec_bytes():
    return json.dumps(FROZEN_SPEC, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


class OfflineGuardRefusal(RuntimeError):
    pass


class OfflineGuard:
    """Scoped, restorable refusal of in-process network, listener and child-process calls.

    Installed by main() before any product or test import; the runner's own literal git,
    scanner and test-script children pass only inside permit_children(). Probes touch no
    target: the patched callables refuse before any socket, DNS or process syscall.
    """
    PROBE_ADDRESS = ("192.0.2.1", 9)  # RFC 5737 documentation range; never contacted

    def __init__(self):
        import http.client
        import socket
        import urllib.request
        self.socket, self.urllib_request, self.http_client = socket, urllib.request, http.client
        self.targets = [(socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "bind"),
                        (socket.socket, "listen"), (socket.socket, "sendto"), (socket, "create_connection"),
                        (socket, "create_server"), (socket, "getaddrinfo"), (urllib.request, "urlopen"),
                        (http.client.HTTPConnection, "connect"), (subprocess, "Popen"), (os, "system")]
        self.saved, self.blocked, self.permitted, self.probes = [], [], [], []
        self.children_allowed = False
        self.report = {}

    def _wrap(self, label, original, child):
        def guarded(*args, **kwargs):
            if child and self.children_allowed:
                self.permitted.append(label)
                return original(*args, **kwargs)
            self.blocked.append(label)
            raise OfflineGuardRefusal("offline_guard_refused:" + label)
        return guarded

    def __enter__(self):
        preloaded = sorted(name for name in sys.modules if name.split(".")[0] in GUARDED_PREFIXES)
        require(not preloaded, "product_or_test_imported_before_guard:" + ",".join(preloaded))
        for owner, name in self.targets:
            original = getattr(owner, name)
            label = getattr(owner, "__name__", "?") + "." + name
            self.saved.append((owner, name, original))
            setattr(owner, name, self._wrap(label, original, name in ("Popen", "system")))
        sock, request, client = self.socket, self.urllib_request, self.http_client
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
                raise RuntimeError("offline_guard_probe_not_refused:" + label)
        require(len(self.blocked) == len(self.probes) == 7, "offline_guard_probe_accounting")
        self.blocked = []
        self.report = {"installed_before_imports": True, "preloaded_guarded_modules": preloaded,
                       "patched": [getattr(o, "__name__", "?") + "." + n for o, n in self.targets],
                       "self_probes": list(self.probes)}
        return self

    def permit_children(self):
        guard = self

        class Permit:
            def __enter__(self):
                guard.children_allowed = True

            def __exit__(self, *exc):
                guard.children_allowed = False
        return Permit()

    def __exit__(self, *exc):
        for owner, name, original in reversed(self.saved):
            setattr(owner, name, original)
        restored = all(getattr(owner, name) is original for owner, name, original in self.saved)
        self.report.update(restored=restored, unexpected_blocked=list(self.blocked),
                           permitted_children=len(self.permitted),
                           guarded_modules_imported_under_guard=sorted(
                               name for name in sys.modules if name.split(".")[0] in GUARDED_PREFIXES))
        return False


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, data):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2, sort_keys=True)
        stream.write("\n")


def canonical(path):
    p = Path(path)
    require(p.is_absolute() and ".." not in p.parts, "literal_absolute_path_required")
    require(not any(part.is_symlink() for part in (p, *p.parents)), "output_or_manifest_symlink")
    return p


class Evidence:
    def __init__(self, output, guard):
        self.output = output
        self.guard = guard
        self.commands = []

    def run(self, label, argv, cwd=REPO, allowed=(0,)):
        require(isinstance(argv, list) and all(isinstance(a, str) for a in argv), "literal_argv_required")
        out = self.output / ("%03d-" % len(self.commands) + label)
        out.mkdir(mode=0o700)
        with self.guard.permit_children():  # only the runner's own literal, shell-disabled children
            run = subprocess.run(argv, cwd=cwd, capture_output=True, shell=False)
        (out / "stdout.txt").write_bytes(run.stdout)
        (out / "stderr.txt").write_bytes(run.stderr)
        receipt = {"label": label, "argv": argv, "cwd": str(cwd), "exit": run.returncode,
                   "stdout_sha256": sha(run.stdout), "stderr_sha256": sha(run.stderr), "directory": str(out)}
        self.commands.append(receipt)
        save(out / "command.json", receipt)
        require(run.returncode in allowed, "command_failed:" + label + ":" + str(run.returncode))
        return run.stdout

    def git(self, label, *args, cwd=REPO):
        return self.run(label, ["git", *args], cwd).decode().strip()


def identity(e, label, repo=REPO):
    return {"head": e.git(label + "-head", "rev-parse", "HEAD", cwd=repo),
            "tree": e.git(label + "-tree", "rev-parse", "HEAD^{tree}", cwd=repo),
            "status": e.git(label + "-status", "status", "--porcelain=v1", "--untracked-files=all", cwd=repo)}


def bind(e, manifest_path, phase):
    raw = canonical(manifest_path).read_bytes()
    m = json.loads(raw)
    require(m["schema"] == "agentorange.source-manifest/v1", "manifest_schema")
    require(m["repo"] == str(REPO) and Path(m["write_root"]) == REPO.parent, "manifest_repo_identity")
    require(re.fullmatch(r"[a-f0-9]{40}", m["head"]) and re.fullmatch(r"[a-f0-9]{40}", m["tree"]), "manifest_base_pin")
    base = m["head"]  # The operator's frozen manifest is the source identity authority.
    require(e.git("base-tree", "rev-parse", base + "^{tree}") == m["tree"], "base_tree_changed")
    e.run("descended-from-base", ["git", "merge-base", "--is-ancestor", base, "HEAD"])
    measured = identity(e, "candidate")
    require(measured["head"] != base, "candidate_must_be_committed_above_base")
    require(not measured["status"], "candidate_must_be_clean")
    changed = e.git("changed-paths", "diff", "--name-only", base, "HEAD").splitlines()
    require(set(changed) == set(AUTHORED), "exclusive_authored_write_set")
    require(e.git("index-diff", "diff", "--cached", "--name-only", "HEAD") == "", "index_changed")
    hashes = {}
    for relative in AUTHORED + CHECKERS + SOURCE:
        path = REPO / relative
        require(path.is_file() and not path.is_symlink(), "regular_source_required")
        head = e.run("head-bytes-" + path.name, ["git", "show", measured["head"] + ":" + relative])
        require(head == path.read_bytes(), "working_bytes_differ_from_head:" + relative)
        if relative not in AUTHORED:
            baseline = e.run("baseline-bytes-" + path.name, ["git", "show", base + ":" + relative])
            require(head == baseline, "unchanged_checker_or_source_modified:" + relative)
        hashes[relative] = sha(head)
    link = REPO / ".venv"
    require(link.is_symlink() and link.resolve() == Path(m["runtime"]["repo_venv"]).resolve(),
            "manifest_runtime_link")
    expected = (Path(m["runtime"]["credential_scan_python3"]) if phase == "credentials"
                else REPO / ".venv/bin/python3")
    require(Path(sys.executable).absolute() == expected, "literal_interpreter_identity")
    require(Path(sys.prefix).resolve() == Path(m["runtime"]["repo_venv"]).resolve(), "runtime_prefix")
    require(Path(sys.executable).resolve() == Path(m["runtime"]["system_python3"]).resolve(),
            "runtime_system_interpreter")
    return m, {**measured, "base": base, "source_hashes": hashes, "manifest_sha256": sha(raw),
               "manifest_path": str(manifest_path), "interpreter": sys.executable,
               "prefix": sys.prefix, "repo_venv_resolved": str(link.resolve())}


def test_only_delta(e, candidate):
    """Test-only: no product path changed; the candidate product file is byte-equal to the base blob."""
    changed = e.git("all-changed-paths", "diff", "--name-status", candidate["base"], "HEAD").splitlines()
    require(sorted(changed) == sorted("A\t" + relative for relative in AUTHORED), "authored_files_added_only")
    base_raw = e.run("base-product-bytes", ["git", "show", candidate["base"] + ":" + PRODUCT])
    require(base_raw == (REPO / PRODUCT).read_bytes(), "product_must_be_unchanged")
    seam = b"total + len(chunk) + 1 > max_chars"
    require(base_raw.count(seam) == 1, "base_admission_seam_observed_once")
    return {"changed": changed, "product": PRODUCT, "product_sha256": sha(base_raw),
            "product_byte_equal_to_base": True}, base_raw


def load_test_module():
    sys.path.insert(0, str(REPO))
    return importlib.import_module("tests.records.test_fc11_model_payload_budget")


def pinned_original(base_raw):
    """Execute the pinned base model_input.py blob as an UNREGISTERED in-memory module."""
    module = types.ModuleType(PINNED_ORIGINAL_NAME)
    module.__package__ = "campaign_tool.digest"  # resolves the relative imports to the real sibling modules
    module.__file__ = "<pinned-base>/" + PRODUCT
    exec(compile(base_raw, module.__file__, "exec"), module.__dict__)
    require(PINNED_ORIGINAL_NAME not in sys.modules, "pinned_original_must_not_be_registered")
    return module


def suite(e, candidate):
    delta, base_raw = test_only_delta(e, candidate)
    module = load_test_module()
    code = module.run_suite(e.output / "suite-summary.json")
    summary = json.loads((e.output / "suite-summary.json").read_text())
    require(code == 0, "suite_requirements_failed")
    require(summary["skipped"] == 0 and not summary["missing_dimensions"] and not summary["missing_case_ids"]
            and not summary["unexpected_case_ids"] and summary["tests_run"] == len(module.CASE_TESTS) + 1,
            "suite_required_coverage")
    data = module.fixture()
    expected = {name: module.expected_kills(data, name) for name in module.CONTROLS}
    require(expected["union_issued"] and all(":refuse" in k for k in expected["union_issued"]),
            "union_control_decides_refusals_only")
    require(all(case + ":prefix" in expected["uncounted_newline"]
                for case in ("fresh:first_minus_one", "fresh:two_minus_one", "fresh:total_minus_one")),
            "newline_control_decides_one_under_prefix")
    observations = json.loads((e.output / "control-observation.json").read_text())["observations"]
    require(len(observations) == 1, "single_control_observation")
    observation = observations[0]
    for name in module.CONTROLS:
        mutant = observation["controls"][name]["mutant"]
        require(mutant["killed_case_ids"] == expected[name] and mutant["errors"] == 0
                and mutant["failures"] == len(expected[name]), "control_observation_shape:" + name)
    require(observation["restored"]["failures"] == 0 and observation["restored"]["errors"] == 0,
            "controls_restored")
    save(e.output / "controls.json", {"label": "DECLARED_NEGATIVE_CONTROLS", "expected_kills": expected,
                                      "observation": observation})
    # Original baseline: the same deciding tests against the pinned, unmodified product blob.
    base_module = pinned_original(base_raw)
    baseline = module.result_summary(module.run_against(base_module))
    save(e.output / "baseline-original.json", {"label": "PINNED_ORIGINAL_BASELINE_TEST_ONLY",
                                                "base": candidate["base"],
                                                "product_sha256": delta["product_sha256"], "result": baseline,
                                                "expectation": "test-only: the unchanged product passes"})
    require(baseline["errors"] == 0 and baseline["failures"] == 0 and baseline["skipped"] == 0
            and baseline["tests_run"] == len(module.CASE_TESTS), "pinned_original_baseline_must_pass")
    # Final real API rerun after the baseline and controls, recorded separately.
    final = module.result_summary(module.nested_result())
    save(e.output / "final-real-api.json", {"label": "CANDIDATE_REAL_API", "result": final})
    require(final["failures"] == 0 and final["errors"] == 0 and final["skipped"] == 0
            and final["tests_run"] == len(module.CASE_TESTS), "final_real_api_failed")
    return {"summary_counts": {k: summary[k] for k in ("tests_run", "failures", "errors", "skipped")},
            "case_ids_passed": len(summary["case_ids_passed"]), "dimensions": summary["dimensions"],
            "test_only_delta": delta, "expected_kills": {k: len(v) for k, v in expected.items()},
            "baseline_original": baseline, "final_real_api": final}


def compatibility(e):
    import unittest
    sys.path.insert(0, str(REPO))
    selected = unittest.TestLoader().loadTestsFromNames(list(SELECTORS))
    ids = sorted(test.id() for test in iterate(selected))  # before running: TestSuite drops tests after a run
    require(ids == sorted(SELECTORS), "compatibility_selector_ids")
    # Raw unittest output is a real file, not a synthesized count.
    with (e.output / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(selected)
    summary = {"selectors": list(SELECTORS), "test_ids": ids, "tests_run": result.testsRun,
               "failures": len(result.failures), "errors": len(result.errors), "skipped": len(result.skipped)}
    save(e.output / "compatibility-summary.json", summary)
    require(result.wasSuccessful() and result.testsRun == len(ids) == 3 and not result.skipped,
            "compatibility_failed")
    return summary


def iterate(suite):
    for item in suite:
        if hasattr(item, "__iter__"):
            yield from iterate(item)
        else:
            yield item


def portable(e):
    forbidden = re.compile(r"/(?:home|Users|workspace|tmp)/|[A-Z]:\\\\|(?:PYTHONPATH|LD_LIBRARY_PATH)\s*=")
    for relative in AUTHORED:
        text = (REPO / relative).read_text(encoding="utf-8")
        require(not forbidden.search(text), "host_specific_authored_literal:" + relative)
    save(e.output / "static-portability.json", {"checked": list(AUTHORED), "findings": []})
    summaries = {}
    for mode in ("suite", "discovery"):
        fresh = e.output / ("foreign-cwd-" + mode)
        fresh.mkdir(mode=0o700)
        e.run(mode, [sys.executable, str(REPO / TEST_MODULE), "--mode", mode, "--temp-root", str(fresh)], cwd=fresh)
        summaries[mode] = json.loads((fresh / "suite-summary.json").read_text())
        require(not summaries[mode]["missing_dimensions"] and not summaries[mode]["skipped"]
                and not summaries[mode]["missing_case_ids"] and not summaries[mode]["unexpected_case_ids"]
                and summaries[mode]["failures"] == 0 and summaries[mode]["errors"] == 0
                and summaries[mode]["control_observations"] == 1, "portable_required_coverage")
        guard = json.loads((fresh / "offline-guard.json").read_text())
        require(guard["installed_before_imports"] and not guard["preloaded_guarded_modules"]
                and len(guard["self_probes"]) == 7 and guard["restored"] and not guard["unexpected_blocked"]
                and guard["permitted_children"] == 0
                and PRODUCT_MODULE in guard["guarded_modules_imported_under_guard"],
                "portable_child_offline_guard")
        summaries[mode]["offline_guard"] = guard
    require(summaries["suite"]["dimensions"] == summaries["discovery"]["dimensions"]
            and summaries["suite"]["case_ids_passed"] == summaries["discovery"]["case_ids_passed"]
            and summaries["suite"]["tests_run"] == summaries["discovery"]["tests_run"],
            "script_discovery_coverage_mismatch")
    return {mode: {"tests_run": s["tests_run"], "case_ids_passed": len(s["case_ids_passed"]),
                   "dimensions": s["dimensions"], "offline_guard_restored": s["offline_guard"]["restored"]}
            for mode, s in summaries.items()}


def credentials(e, m):
    scanner = Path(m["runtime"]["detect_secrets"])
    require(scanner.is_absolute() and scanner.is_file() and os.access(scanner, os.X_OK), "scanner_missing")
    require(scanner.parent == Path(m["runtime"]["credential_scan_python3"]).parent, "scanner_runtime_binding")
    first_line = scanner.read_text().splitlines()[0]
    require(first_line == "#!" + m["runtime"]["credential_scan_python3"], "scanner_shebang_binding")
    raw = e.run("detect-secrets", [str(scanner), "scan", "--all-files", "--exclude-files", "^.git/", "--no-verify"])
    parsed = json.loads(raw)
    require(isinstance(parsed.get("results"), dict), "genuine_scanner_json_required")
    report = e.output / "detect-secrets.json"
    with report.open("xb") as stream:
        stream.write(raw)
    e.run("secret-report", [sys.executable, str(REPO / "tools/check_secret_report.py"), str(report)])
    return {"scanner_report_sha256": sha(raw), "report": str(report),
            "scanner_sha256": sha(scanner.read_bytes()), "potential_hit_files": len(parsed["results"])}


def public_tree(e, m, candidate):
    # Inventory includes the ignored standard link without traversing it.
    e.run("literal-status-including-ignored", ["git", "status", "--short", "--ignored", "--untracked-files=all"])
    raw_inventory = e.run("scan-visible-path-inventory", ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    visible = set(raw_inventory.decode().split("\0")) - {""}
    tracked = set(e.run("tracked-inventory", ["git", "ls-files", "-z"]).decode().split("\0")) - {""}
    require(visible - tracked <= {".venv"}, "untracked_scan_visible_files")
    require(set(AUTHORED) <= tracked, "authored_files_not_tracked")
    scanner_hashes = {relative: sha((REPO / relative).read_bytes()) for relative in CHECKERS}
    clone = e.output / "committed-tree"
    e.run("local-clone", ["git", "clone", "--local", "--no-hardlinks", "--no-checkout",
                          "--no-recurse-submodules", str(REPO), str(clone)])
    e.run("exact-checkout", ["git", "checkout", "--detach", candidate["head"]], cwd=clone)
    cloned = identity(e, "cloned", clone)
    require(cloned["head"] == candidate["head"] and cloned["tree"] == candidate["tree"]
            and not cloned["status"], "clone_identity")
    require(not (clone / ".venv").exists() and not (clone / ".venv").is_symlink(), "clone_runtime_link_forbidden")
    for relative, expected in candidate["source_hashes"].items():
        require(sha((clone / relative).read_bytes()) == expected, "clone_hash_mismatch:" + relative)
    public = json.loads(e.run("committed-tree-public-scan", [sys.executable, str(clone / "tools/check_public_tree.py")], cwd=clone))
    require(public.get("findings") == [] and public.get("scanned_files", 0) == len(tracked), "incomplete_public_scan")
    # Preserve the unchanged staged invocation; never call an informational result a pass.
    raw = e.run("in_place_informational-INC-174", [sys.executable, str(REPO / "tools/check_public_tree.py")], allowed=(0, 1))
    informational = json.loads(raw)
    findings = informational.get("findings")
    code = e.commands[-1]["exit"]
    require((code == 0 and findings == []) or
            (code == 1 and findings == [[".venv", 0, "symlink_not_scanned"]]), "unexpected_staged_finding")
    require(informational.get("scanned_files", 0) == len(tracked), "incomplete_in_place_scan")
    require(identity(e, "source-recheck") == {key: candidate[key] for key in ("head", "tree", "status")},
            "source_changed_during_scan")
    return {"committed_tree": public, "clone": str(clone), "clone_identity": cloned,
            "scanner_hashes": scanner_hashes, "tracked_files": len(tracked),
            "in_place_informational": {"issue": "INC-174", "label": "in_place_informational",
                                       "exit": code, "result": informational}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("suite", "compatibility", "portable", "credentials", "public-tree"))
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--out-base", required=True)
    args = parser.parse_args()
    base = canonical(args.out_base)
    require(base.parent == REPO.parent and base.name == "out", "admitted_out_base_required")
    base.mkdir(mode=0o700, exist_ok=True)
    output = base / (args.phase + "-" + str(uuid.uuid4()))
    output.mkdir(mode=0o700)
    temp = output / "temporary"
    temp.mkdir(mode=0o700)
    tempfile.tempdir = str(temp)  # precedes every product or test import
    guard = OfflineGuard()
    e = Evidence(output, guard)
    spec_raw = frozen_spec_bytes()
    spec_path = output / "frozen-spec.json"
    with spec_path.open("xb") as stream:
        stream.write(spec_raw)
    receipt = {"schema": "fc11-acceptance-row/v1", "phase": args.phase, "output": str(output),
               "invocation": {"argv": [sys.executable, *sys.argv], "cwd": str(Path.cwd()), "interpreter": sys.executable},
               "frozen_spec": {"path": str(spec_path), "sha256": sha(spec_raw), "bytes": len(spec_raw),
                               "origin": "tools/fc11_model_payload_budget_acceptance.py FROZEN_SPEC at candidate HEAD"},
               "status": "FAILED", "semantic_acceptance": False, "remote_ci": "OWED",
               "full_discovery": "operator-run"}
    code = 1
    try:
        with guard:  # offline boundary precedes bind() and every product/test import
            receipt["offline_guard"] = guard.report
            require(Path.cwd() == REPO, "acceptance_cwd")
            m, candidate = bind(e, Path(args.source_manifest), args.phase)
            receipt["candidate"] = candidate
            template = {"<credential_scan_python3>": m["runtime"]["credential_scan_python3"]}
            expected_argv = [template.get(part, part).replace("<write_root>", str(REPO.parent))
                             for part in FROZEN_SPEC["rows"][args.phase]]
            require(receipt["invocation"]["argv"] == expected_argv, "invocation_differs_from_frozen_spec_row")
            require(spec_path.read_bytes() == spec_raw and json.loads(spec_raw) == FROZEN_SPEC, "frozen_spec_bytes")
            receipt["frozen_spec"].update(row_argv_template=FROZEN_SPEC["rows"][args.phase], invocation_matches=True,
                                          runner_sha256=candidate["source_hashes"][AUTHORED[2]],
                                          fixture_sha256=candidate["source_hashes"][AUTHORED[1]],
                                          test_module_sha256=candidate["source_hashes"][AUTHORED[0]])
            if args.phase == "suite":
                result = suite(e, candidate)
            elif args.phase == "compatibility":
                result = compatibility(e)
            elif args.phase == "portable":
                result = portable(e)
            elif args.phase == "credentials":
                result = credentials(e, m)
            else:
                result = public_tree(e, m, candidate)
            require(identity(e, "final") == {key: candidate[key] for key in ("head", "tree", "status")},
                    "final_identity_changed")
            require(sha(Path(args.source_manifest).read_bytes()) == candidate["manifest_sha256"], "manifest_changed")
        report = guard.report
        require(report["restored"] and not report["unexpected_blocked"], "offline_guard_breached_or_not_restored")
        if args.phase in ("suite", "compatibility"):
            require(PRODUCT_MODULE in report["guarded_modules_imported_under_guard"],
                    "admitted_call_graph_not_imported_under_guard")
        else:
            require(not report["guarded_modules_imported_under_guard"], "unexpected_in_process_product_import")
        receipt.update(status="PASS_REVIEW_REQUIRED", result=result)
        code = 0
    except Exception as error:
        receipt["error"] = {"type": type(error).__name__, "message": str(error)}
        with (output / "exception.txt").open("x") as stream:
            traceback.print_exc(file=stream)
    receipt["commands"] = e.commands
    receipt["exit"] = code
    # Sealed only after every child and output writer finished; the clone's .git is not traversed.
    receipt["artifact_hashes"] = {str(p.relative_to(output)): sha(p.read_bytes())
                                  for p in sorted(output.rglob("*")) if p.is_file() and not p.is_symlink()
                                  and "committed-tree" not in p.relative_to(output).parts}
    save(output / "row.json", receipt)
    print(json.dumps({"phase": args.phase, "exit": code, "status": receipt["status"],
                      "row": str(output / "row.json"), "row_sha256": sha((output / "row.json").read_bytes()),
                      "error": receipt.get("error"), "result": receipt.get("result")}, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
