"""Five separately invoked FC11-DIGEST-IDENTITIES offline acceptance rows; raw evidence is never reused."""
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
AUTHORED = ("campaign_tool/digest/schema.py",
            "tests/records/test_fc11_digest_identities.py",
            "tests/fixtures/fc11-digest-identities/cases.json",
            "tools/fc11_digest_identities_acceptance.py")
TEST_MODULE = AUTHORED[1]
CHECKERS = ("tools/check_public_tree.py", "campaign_tool/records/public_scan.py",
            "tools/check_secret_report.py")
SOURCE = ("campaign_tool/law.py", "campaign_tool/digest/build.py", "campaign_tool/digest/__init__.py",
          "tests/runner/test_digest.py", "tests/test_law.py")
SELECTORS = ("tests.runner.test_digest.BuildDigestTests.test_no_model_is_detector_only_and_needs_attorney_review",
             "tests.test_law.CaliforniaPackageTests",
             "tests.test_law.LoadPackageValidationTests")
BASE_DECLARATIONS = ['HEX64 = "^[0-9a-f]{64}$"',
                     '"jurisdiction": {"type": "string", "pattern": "^[a-z]{2}-[a-z]{2}$"},']
FINAL_DECLARATIONS = ['HEX64 = r"^[0-9a-f]{64}\\Z"',
                      '"jurisdiction": {"type": "string", "pattern": r"^[a-z]{2}-[a-z]{2}\\Z"},']
PINNED_ORIGINAL_NAME = "campaign_tool.digest._fc11_pinned_original"
GUARDED_PREFIXES = ("campaign_tool", "tests")
ROW_INTERPRETER = {"credentials": "<credential_scan_python3>"}


def _row_argv(phase):
    return [ROW_INTERPRETER.get(phase, "<write_root>/repo/.venv/bin/python3"),
            "<write_root>/repo/tools/fc11_digest_identities_acceptance.py", "--phase", phase,
            "--source-manifest", "<write_root>/SOURCE-MANIFEST.json", "--out-base", "<write_root>/out"]


# Sanitized, durable representation of the card's frozen acceptance contract.
# Host paths are templated and source pins are bound through SOURCE-MANIFEST.json and git
# blobs, never embedded hex. Every row writes these canonical bytes and records their SHA256.
FROZEN_SPEC = {
    "schema": "fc11-frozen-acceptance-spec/v1",
    "card": "t_1846b109",
    "title": "FC11-DIGEST-IDENTITIES Reject trailing characters in digest hashes and jurisdiction tokens "
             "without changing schema versions.",
    "source_pin": {"repository": "Grahammmm/DeFlock-Campaign-Tool", "branch": "main",
                   "binding": "operator SOURCE-MANIFEST.json head/tree; initial HEAD equals manifest head; "
                              "candidate descends from it"},
    "frozen_behavior": "Make the digest-local hash and jurisdiction patterns whole-string exact: root and "
                       "conclusion-source hashes exactly 64 lowercase ASCII hex, jurisdiction exactly two "
                       "lowercase ASCII letters, hyphen, two letters. No strip, case-fold or normalization; no "
                       "general law validator, law path, schema_version or locator change; source-to-original "
                       "equality gate and ordinary return shape preserved.",
    "frozen_test_experiment": ["valid_exact", "lf", "cr", "crlf", "space", "tab", "prefix_junk", "suffix_junk",
                               "uppercase", "wrong_length", "non_ascii_lookalike", "mismatch_control",
                               "ordinary_matching_source", "confidence_unchanged", "inputs_unchanged"],
    "negative_control": "In a test-local deep copy of DIGEST_SCHEMA (and its nested SOURCE) restore only the "
                        "permissive $ end anchors; root:lf, pair:lf, source:lf and jurisdiction:lf must fail "
                        "while valid-token controls pass; restore and rerun the real API.",
    "pinned_original_baseline": "The pinned base schema.py blob is executed in memory as an unregistered module "
                                "and the same deciding tests must fail exactly the four LF cases.",
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
            require(head == baseline, "unchanged_checker_or_source_modified")
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


def product_delta(e, candidate):
    """The product delta is exactly the two end-anchor declaration lines."""
    product = AUTHORED[0]
    base_raw = e.run("base-product-bytes", ["git", "show", candidate["base"] + ":" + product])
    base_bytes = base_raw.decode()
    diff = e.git("product-diff", "diff", "--unified=0", candidate["base"], "HEAD", "--", product)
    removed = [line[1:].strip() for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")]
    added = [line[1:].strip() for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")]
    require(removed == BASE_DECLARATIONS and added == FINAL_DECLARATIONS, "product_delta_not_anchor_only")
    require(all(base_bytes.count(line) == 1 for line in BASE_DECLARATIONS), "base_declarations_observed")
    return {"base_declarations": BASE_DECLARATIONS, "final_declarations": FINAL_DECLARATIONS,
            "base_product_sha256": sha(base_raw), "removed": removed, "added": added}, base_raw


def load_test_module():
    sys.path.insert(0, str(REPO))
    return importlib.import_module("tests.records.test_fc11_digest_identities")


def pinned_original(base_raw):
    """Execute the pinned base schema.py blob as an UNREGISTERED in-memory module (not in sys.modules)."""
    module = types.ModuleType(PINNED_ORIGINAL_NAME)
    module.__package__ = "campaign_tool.digest"  # resolves ``from ..law import validate`` to the real law module
    module.__file__ = "<pinned-base>/" + AUTHORED[0]
    exec(compile(base_raw, module.__file__, "exec"), module.__dict__)
    require(PINNED_ORIGINAL_NAME not in sys.modules, "pinned_original_must_not_be_registered")
    return module


def suite(e, candidate):
    delta, base_raw = product_delta(e, candidate)
    module = load_test_module()
    code = module.run_suite(e.output / "suite-summary.json")
    summary = json.loads((e.output / "suite-summary.json").read_text())
    require(code == 0, "suite_requirements_failed")
    require(summary["skipped"] == 0 and not summary["missing_dimensions"] and not summary["missing_case_ids"],
            "suite_required_coverage")
    expected = module.expected_kills(module.fixture())
    require(expected == ["jurisdiction:lf", "pair:lf", "root:lf", "source:lf"], "frozen_expected_kills")
    controls = sorted((e.output / "temporary").glob("fc11-control-*/control-observation.json"))
    require(len(controls) == 1, "single_control_observation")
    control = json.loads(controls[0].read_text())
    require(control["mutant"]["killed_case_ids"] == expected and control["mutant"]["errors"] == 0
            and control["restored"]["failures"] == 0 and control["restored"]["errors"] == 0,
            "control_observation_shape")
    # Original baseline: the same deciding tests against the pinned, unmodified product source.
    base_module = pinned_original(base_raw)
    require(base_module.HEX64 == "^[0-9a-f]{64}$", "pinned_original_is_permissive_base")
    baseline = module.result_summary(module.run_against(base_module))
    save(e.output / "baseline-original.json", {"label": "PINNED_ORIGINAL_BASELINE", "base": candidate["base"],
                                                "base_product_sha256": delta["base_product_sha256"],
                                                "result": baseline})
    require(baseline["errors"] == 0 and baseline["killed_case_ids"] == expected
            and baseline["failures"] == len(expected), "baseline_must_fail_exactly_the_lf_cases")
    # Final real API rerun after the baseline, recorded separately.
    final = module.result_summary(module.nested_result())
    save(e.output / "final-real-api.json", {"label": "CANDIDATE_REAL_API", "result": final})
    require(final["failures"] == 0 and final["errors"] == 0 and final["skipped"] == 0, "final_real_api_failed")
    return {"summary": summary, "product_delta": delta, "control_observation": {
        "path": str(controls[0]), "sha256": sha(controls[0].read_bytes()), "content": control},
        "baseline_original": baseline, "final_real_api": final}


def compatibility(e):
    import unittest
    sys.path.insert(0, str(REPO))
    selected = unittest.TestLoader().loadTestsFromNames(list(SELECTORS))
    # Raw unittest output is a real file, not a synthesized count.
    with (e.output / "unittest.txt").open("x") as stream:
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(selected)
    summary = {"selectors": list(SELECTORS), "tests_run": result.testsRun, "failures": len(result.failures),
               "errors": len(result.errors), "skipped": len(result.skipped)}
    save(e.output / "compatibility-summary.json", summary)
    require(result.wasSuccessful() and result.testsRun > 0 and not result.skipped, "compatibility_failed")
    return summary


def portable(e):
    forbidden = re.compile(r"/(?:home|Users|workspace|tmp)/|[A-Z]:\\\\|(?:PYTHONPATH|LD_LIBRARY_PATH)\s*=")
    for relative in AUTHORED:
        text = (REPO / relative).read_text()
        require(not forbidden.search(text), "host_specific_authored_literal:" + relative)
    save(e.output / "static-portability.json", {"checked": list(AUTHORED), "findings": []})
    summaries = {}
    for mode in ("suite", "discovery"):
        fresh = e.output / ("foreign-cwd-" + mode)
        fresh.mkdir(mode=0o700)
        e.run(mode, [sys.executable, str(REPO / TEST_MODULE), "--mode", mode, "--temp-root", str(fresh)], cwd=fresh)
        summaries[mode] = json.loads((fresh / "suite-summary.json").read_text())
        require(not summaries[mode]["missing_dimensions"] and not summaries[mode]["skipped"]
                and not summaries[mode]["missing_case_ids"] and summaries[mode]["failures"] == 0
                and summaries[mode]["errors"] == 0, "portable_required_coverage")
        guard = json.loads((fresh / "offline-guard.json").read_text())
        require(guard["installed_before_imports"] and not guard["preloaded_guarded_modules"]
                and len(guard["self_probes"]) == 7 and guard["restored"] and not guard["unexpected_blocked"]
                and guard["permitted_children"] == 0
                and "campaign_tool.digest.model" in guard["guarded_modules_imported_under_guard"],
                "portable_child_offline_guard")
        summaries[mode]["offline_guard"] = guard
    require(summaries["suite"]["dimensions"] == summaries["discovery"]["dimensions"]
            and summaries["suite"]["case_ids_passed"] == summaries["discovery"]["case_ids_passed"],
            "script_discovery_coverage_mismatch")
    return summaries


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
    # Preserve unchanged staged invocation; never call an informational result a pass.
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
                               "origin": "tools/fc11_digest_identities_acceptance.py FROZEN_SPEC at candidate HEAD"},
               "status": "FAILED", "semantic_acceptance": False, "remote_ci": "OWED",
               "full_discovery": "operator-run"}
    code = 1
    try:
        with guard:  # offline boundary precedes bind() and every product/test import
            receipt["offline_guard"] = guard.report
            require(Path.cwd() == REPO, "acceptance_cwd")
            m, candidate = bind(e, Path(args.source_manifest), args.phase)
            receipt["candidate"] = candidate
            template = {"<write_root>": str(REPO.parent),
                        "<credential_scan_python3>": m["runtime"]["credential_scan_python3"]}
            expected_argv = [template.get(part, part) for part in FROZEN_SPEC["rows"][args.phase]]
            expected_argv = [part.replace("<write_root>", str(REPO.parent)) for part in expected_argv]
            require(receipt["invocation"]["argv"] == expected_argv, "invocation_differs_from_frozen_spec_row")
            require(spec_path.read_bytes() == spec_raw and json.loads(spec_raw) == FROZEN_SPEC, "frozen_spec_bytes")
            receipt["frozen_spec"].update(row_argv_template=FROZEN_SPEC["rows"][args.phase], invocation_matches=True,
                                          runner_sha256=candidate["source_hashes"][AUTHORED[3]],
                                          fixture_sha256=candidate["source_hashes"][AUTHORED[2]])
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
            require("campaign_tool.digest.model" in report["guarded_modules_imported_under_guard"],
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
    # Do not traverse the clone's .git or symlinks while sealing task evidence.
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
