"""FC9 synthetic process-lifetime experiment; imports are discovery-inert.

The fixture models the pinned WP8 producer schema. No real records, authentication,
production authority, ledger, transport, promotion or publication is used.
"""
import argparse
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import uuid

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests/fixtures/fc9-review-restart-lifecycle/cases.json"
MODULE = "tests.records.test_fc9_review_restart_lifecycle"


def expect(condition, reason):
    if not condition:
        raise AssertionError(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")


def fixture():
    data = json.loads(FIXTURE.read_text())
    expect(data["schema"] == "fc9-review-restart-lifecycle/v1" and data["synthetic_only"] is True,
           "synthetic_fixture_required")
    return data


def product():
    # Delayed until the dedicated runner/child has redirected tempfile.tempdir.
    from campaign_tool.records import review_bundle
    return review_bundle


def startup(data):
    rb = product()
    contexts = {name: object() for name in data["principals"]}
    sessions = {context: name for name, context in contexts.items()}
    authority = rb.Authority(profile_id=data["profile_id"], principals=data["principals"],
        authenticate=lambda context: sessions[context],
        signing_key=hashlib.sha256(data["fictional_key_material"].encode()).digest(), test_only=True)
    return rb, authority, contexts


def inputs(root, data, rb, create=False):
    bindings = {kind: [] for kind in rb.KINDS}
    for kind, text in data["artifacts"].items():
        path = root / "artifacts" / (kind + ".txt")
        raw = text.encode()
        if create and not path.exists():
            with path.open("xb") as stream:
                stream.write(raw)
            path.chmod(0o600)
        expect(path.read_bytes() == raw, "fictional_artifact_bytes")
        bindings[kind] = [{"id": kind, "path": str(path), "sha256": digest(raw), "bytes": len(raw)}]
    original, unit = bindings["originals"][0], bindings["units"][0]
    proposal = {"proposal_id": "fc9-fictional-proposal", "revision": 1, "tier": "A",
        "agency": "Fictional agency", "classification": "CONFIRMED_DOCUMENTARY_FACT",
        "next_action": "Await independent review; offline synthetic only", "event_date": None,
        "primary_evidence": [{"path": original["path"], "sha256": original["sha256"],
            "locator": "line:1", "observation": data["public"]["claim"], "coverage": "selected"}],
        "rules": [], "counterevidence": [], "missing_evidence": [], "exceptions_checked": False,
        "bindings": bindings, "coverage": [{"original_sha256": original["sha256"],
            "unit_sha256": unit["sha256"], "locator": "line:1", "scope": "selected"}], "supersedes": None}
    return proposal, rb.encoded(data["public"])


def review_payload(rb, root, identity, authority, step):
    return dict(rb.review_target(root / "bundles", identity, authority=authority),
        role="privacy" if step == "privacy" else "factual",
        verdict="challenge" if step == "challenge" else "pass",
        rationale="Fictional challenge" if step == "challenge" else "Fictional source comparison",
        reviewed_at="2026-01-01T00:00:00Z", reviewed_primary=True)


def owner_payload(report):
    return {**{key: report[key] for key in ("bundle_id", "finding_digest", "public_content_sha256",
        "review_set_sha256")}, "decision": "approve", "rationale": "Fictional owner decision",
        "decided_at": "2026-01-02T00:00:00Z"}


def inventory(root):
    return {str(path.relative_to(root)): digest(path.read_bytes())
            for path in sorted(root.rglob("*")) if path.is_file() and path.name != "review-bundle.lock"}


def observe(root, data, state, prefix, rb, authority, mutate, output):
    spec = data["prefixes"][prefix]
    if prefix == "empty":
        expect(not list((root / "bundles").iterdir()), "empty_bundle_root")
        return {"inventory": inventory(root), "prefix": prefix}
    identity = state["bundle_id"]
    directory = root / "bundles" / identity
    # Validate the physical receipt set and signed contents independently of _entries.
    expected_paths = {}
    for step in spec["reviews"] + (["owner"] if spec["owner"] else []):
        entry = state["receipts"][step]
        kind = "owners" if step == "owner" else "reviews"
        expected_paths[kind + "/" + entry["receipt_id"] + ".json"] = entry
    actual_paths = {str(path.relative_to(directory)) for kind in ("reviews", "owners")
                    for path in (directory / kind).iterdir()}
    expect(actual_paths == set(expected_paths), "exact_prefix_receipt_paths")
    for relative, entry in expected_paths.items():
        raw = (directory / relative).read_bytes()
        expect(digest(raw) == entry["receipt_id"], "receipt_content_address")
        body = authority.verify(rb.decode(raw))
        expect(body == entry["body"], "exact_authenticated_receipt_body")
    original_entries = rb._entries
    def hide_challenge(path, auth):
        # Validate EVERY file/signature first; hide only this one known committed receipt.
        entries = original_entries(path, auth)
        return [(key, body) for key, body in entries
                if not (path.name == "reviews" and key == state["receipts"]["challenge"]["receipt_id"])]
    with mock.patch.object(rb, "_entries", hide_challenge) if mutate else nullcontext():
        report = rb.assess_bundle(root / "bundles", identity, authority=authority)
    observed = {"prefix": prefix, "report": report, "inventory": inventory(root),
                "label": "MUTANT_INFORMATIONAL_NOT_REVIEW_EVIDENCE" if mutate else "REAL_INVENTORY"}
    save(output / "observed.json", observed)
    # The negative must die at this actual readiness gate, not a setup/count error.
    if prefix == "challenge":
        expect(report["review_complete"] is False, "challenge_inventory_gate")
    expect(report["review_complete"] is spec["complete"], "prefix_review_complete")
    expect(report["blockers"] == spec["blockers"], "exact_prefix_blockers_and_missing_roles")
    expect(report["owner_approval"] == spec["owner_approval"], "prefix_owner_binding")
    review_ids = sorted(state["receipts"][step]["receipt_id"] for step in spec["reviews"])
    encoded_ids = (json.dumps(review_ids, sort_keys=True, separators=(",", ":")) + "\n").encode()
    expect(report["review_set_sha256"] == digest(encoded_ids), "exact_review_set_binding")
    expect(report["review_receipts"] == len(review_ids), "exact_review_count")
    if spec["owner"]:
        old = state["receipts"]["owner"]["body"]["payload"]["review_set_sha256"]
        expect((old == report["review_set_sha256"]) is (prefix == "owner"), "owner_review_set_lifetime")
    for key in ("production_review_complete", "handoff_ready", "publication_ready"):
        expect(report[key] is False, "synthetic_never_ready:" + key)
    expect(report["stage_promotions"] == 0 and report["test_only"] is True, "zero_promotions_test_only")
    expect((directory / "public_content.json").stat().st_mode & 0o777 == 0o600, "private_public_bytes")
    expect(directory.stat().st_mode & 0o777 == 0o700, "private_bundle_directory")
    return observed


def child(args):
    data = fixture()
    rb, authority, contexts = startup(data)
    root, output = Path(args.case_root), Path(args.temp_root)
    state = json.loads(Path(args.state).read_text())
    identity = state.get("bundle_id")
    if args.action == "observe":
        return observe(root, data, state, args.prefix, rb, authority, args.mutant, output)
    if args.action == "create":
        proposal, public = inputs(root, data, rb, create=True)
        return rb.create_bundle(root / "bundles", public, proposal, authority=authority,
                                auth_context=contexts["author"])
    if args.action in ("factual", "privacy", "challenge", "malformed"):
        payload = review_payload(rb, root, identity, authority, args.action)
        role = "privacy" if args.action == "privacy" else "factual"
        if args.action == "malformed":
            case = next(c for c in data["malformed"] if c["id"] == args.case_id)
            payload.update(case["changes"])
            try:
                rb.record_review(root / "bundles", identity, payload, authority=authority,
                                 auth_context=contexts[role])
            except rb.BundleError as error:
                expect(str(error) == case["error"], "precise_malformed_reason")
                return {"error": str(error)}
            raise AssertionError("malformed_review_accepted")
        result = rb.record_review(root / "bundles", identity, payload, authority=authority,
                                  auth_context=contexts[role])
        body = {"kind": "review", "bundle_id": identity, "policy_sha256": authority.policy_sha256,
                "actor": data["principals"][role], "payload": payload}
    else:
        report = rb.assess_bundle(root / "bundles", identity, authority=authority)
        payload = (state["receipts"]["owner"]["body"]["payload"] if args.action == "stale-owner"
                   else owner_payload(report))
        if args.action in ("early-owner", "stale-owner"):
            reason = "stale_owner_binding" if args.action == "stale-owner" else "reviews_not_complete"
            try:
                rb.record_owner_decision(root / "bundles", identity, payload,
                                         authority=authority, auth_context=contexts["owner"])
            except rb.BundleError as error:
                expect(str(error) == reason, "precise_owner_refusal")
                return {"error": str(error)}
            raise AssertionError("invalid_owner_accepted")
        result = rb.record_owner_decision(root / "bundles", identity, payload,
                                          authority=authority, auth_context=contexts["owner"])
        body = {"kind": "owner", "bundle_id": identity, "policy_sha256": authority.policy_sha256,
                "actor": data["principals"]["owner"], "payload": payload}
    expect(result["receipt_id"] == digest(rb.encoded(authority.seal(body))), "exact_receipt_identity")
    return {**result, "body": body}


class RestartLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.coverage = []
        self.data = fixture()
        # No cleanup: raw process receipts and private state are retained for review.
        self.output = Path(tempfile.mkdtemp(prefix="fc9-case-"))
        self.root = self.output / "private-state"
        self.root.mkdir(mode=0o700)
        (self.root / "bundles").mkdir(mode=0o700)
        (self.root / "artifacts").mkdir(mode=0o700)
        self.state = {"receipts": {}}
        self.counter = 0
        self.identities = set()
        self.last_inventory = {}

    def invoke(self, action, prefix="empty", mutant=False, case_id=None):
        self.counter += 1
        out = self.output / ("%03d-%s%s" % (self.counter, action, "-mutant" if mutant else ""))
        out.mkdir(mode=0o700)
        state_path = out / "expected-state.json"
        save(state_path, self.state)
        argv = [sys.executable, str(Path(__file__).resolve()), "--mode", "child", "--temp-root", str(out),
                "--case-root", str(self.root), "--state", str(state_path), "--action", action,
                "--prefix", prefix]
        if mutant:
            argv.append("--mutant")
        if case_id:
            argv.extend(["--case-id", case_id])
        run = subprocess.run(argv, cwd=out, capture_output=True, shell=False)
        (out / "stdout.txt").write_bytes(run.stdout)
        (out / "stderr.txt").write_bytes(run.stderr)
        save(out / "command.json", {"argv": argv, "cwd": str(out), "interpreter": sys.executable,
            "exit": run.returncode, "stdout_sha256": digest(run.stdout), "stderr_sha256": digest(run.stderr)})
        result = json.loads(run.stdout)
        self.assertNotIn(result["process_instance"], self.identities)
        self.identities.add(result["process_instance"])
        if mutant:
            self.assertEqual(run.returncode, 7)
            self.assertEqual(result.get("assertion"), "challenge_inventory_gate")
            observed = json.loads((out / "observed.json").read_text())
            self.assertTrue(observed["report"]["review_complete"])
            self.assertEqual(observed["report"]["owner_approval"], "approved")
            self.assertEqual(observed["report"]["review_receipts"], 2)
            self.assertEqual(observed["inventory"], self.last_inventory)
        else:
            self.assertEqual(run.returncode, 0, run.stderr.decode() + run.stdout.decode())
        return result["result"] if not mutant else result

    def check(self, prefix):
        observed = self.invoke("observe", prefix)
        current = observed["inventory"]
        for path, old_hash in self.last_inventory.items():
            self.assertEqual(current.get(path), old_hash, "prior bytes changed: " + path)
        if prefix != "empty":
            allowed = {"artifacts/" + kind + ".txt" for kind in self.data["artifacts"]}
            base = "bundles/" + self.state["bundle_id"] + "/"
            allowed.update({base + "manifest.json", base + "public_content.json"})
            spec = self.data["prefixes"][prefix]
            for step in spec["reviews"] + (["owner"] if spec["owner"] else []):
                kind = "owners" if step == "owner" else "reviews"
                allowed.add(base + kind + "/" + self.state["receipts"][step]["receipt_id"] + ".json")
            self.assertEqual(set(current), allowed)
        self.last_inventory = current
        return observed

    def advance(self, step, previous):
        self.check(previous)  # dedicated fresh interpreter BEFORE the operation
        result = self.invoke(step)
        self.assertFalse(result["reused"])
        if step == "create":
            self.state["bundle_id"] = result["bundle_id"]
        else:
            self.state["receipts"][step] = result
        self.check(step)  # another fresh interpreter AFTER the operation
        before = self.last_inventory.copy()
        replay = self.invoke(step)  # new process; no cached authority or inventory
        self.assertTrue(replay["reused"])
        key = "bundle_id" if step == "create" else "receipt_id"
        self.assertEqual(replay[key], result[key])
        self.check(step)
        self.assertEqual(self.last_inventory, before)
        self.coverage.append("replay:" + step)

    def sequence(self, root_name):
        previous = "empty"
        for step in self.data["steps"]:
            self.advance(step, previous)
            self.coverage.append(root_name + ":" + step)
            previous = step

    def test_lifecycle_first_fresh_root(self):
        self.sequence("root-one")

    def test_lifecycle_second_fresh_root(self):
        # Independently reconstructed path-bound IDs; never compare IDs across roots.
        self.sequence("root-two")

    def test_challenge_inventory_negative_control_and_restore(self):
        self.sequence("control-root")
        before = self.last_inventory.copy()
        self.invoke("observe", "challenge", mutant=True)
        self.coverage.append("negative:challenge-hidden")
        self.check("challenge")
        self.assertEqual(self.last_inventory, before)
        self.coverage.append("negative:real-inventory-restored")
        result = self.invoke("stale-owner")
        self.assertEqual(result["error"], "stale_owner_binding")
        self.check("challenge")
        self.assertEqual(self.last_inventory, before)
        self.coverage.append("negative:stale-owner")

    def test_owner_approval_refused_at_incomplete_prefixes(self):
        self.advance("create", "empty")
        for prefix in ("create", "factual"):
            if prefix == "factual":
                self.advance("factual", "create")
            before = self.last_inventory.copy()
            self.assertEqual(self.invoke("early-owner")["error"], "reviews_not_complete")
            self.check(prefix)
            self.assertEqual(self.last_inventory, before)
            self.coverage.append("boundary:owner-at-" + prefix)

    def test_malformed_reviews_preserve_persisted_prefix(self):
        self.advance("create", "empty")
        for case in self.data["malformed"]:
            with self.subTest(case=case["id"]):
                before = self.last_inventory.copy()
                self.assertEqual(self.invoke("malformed", case_id=case["id"])["error"], case["error"])
                self.check("create")
                self.assertEqual(self.last_inventory, before)
                self.coverage.append("malformed:" + case["id"])


class CoverageResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dimensions = set()

    def addSuccess(self, test):
        self.dimensions.update(getattr(test, "coverage", []))
        super().addSuccess(test)


def run_suite(report_path, discovery=False):
    loader = unittest.TestLoader()
    suite = (loader.discover(str(REPO / "tests/records"), pattern="test_fc9_review_restart_lifecycle.py",
                            top_level_dir=str(REPO)) if discovery
             else loader.loadTestsFromName(MODULE + ".RestartLifecycleTests"))
    result = unittest.TextTestRunner(verbosity=2, resultclass=CoverageResult).run(suite)
    required = set(fixture()["required_dimensions"])
    summary = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
        "skipped": len(result.skipped), "dimensions": sorted(result.dimensions),
        "required_dimensions": sorted(required), "missing_dimensions": sorted(required - result.dimensions)}
    save(report_path, summary)
    return int(not result.wasSuccessful() or bool(result.skipped) or bool(summary["missing_dimensions"]))


def sentinel_discovery(report_path):
    # Snapshot BEFORE ordinary discovery, then run an unrelated peer before/after.
    old_env, old_temp, old_path = dict(os.environ), tempfile.tempdir, list(sys.path)
    sentinel_file = Path(tempfile.gettempdir()) / "sentinel-fixture.txt"
    sentinel_file.write_text("unrelated sentinel")
    class Sentinel(unittest.TestCase):
        def runTest(self):
            self.assertEqual(dict(os.environ), old_env)
            self.assertEqual(tempfile.tempdir, old_temp)
            self.assertEqual(sys.path, old_path)
            self.assertEqual(sentinel_file.read_text(), "unrelated sentinel")
            with tempfile.TemporaryDirectory() as directory:
                self.assertEqual(Path(directory).parent, Path(old_temp))
    before = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    code = run_suite(report_path, discovery=True)
    after = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([Sentinel()]))
    expect(before.wasSuccessful() and after.wasSuccessful(), "discovery_peer_contamination")
    return code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("suite", "discovery", "child"), default="suite")
    parser.add_argument("--temp-root", required=True)
    parser.add_argument("--case-root")
    parser.add_argument("--state")
    parser.add_argument("--action", choices=("observe", "create", "factual", "privacy", "owner", "challenge",
                                           "stale-owner", "early-owner", "malformed"))
    parser.add_argument("--prefix", default="empty")
    parser.add_argument("--mutant", action="store_true")
    parser.add_argument("--case-id")
    args = parser.parse_args()
    temp = Path(args.temp_root)
    expect(temp.is_absolute() and ".." not in temp.parts and temp.is_dir(), "literal_existing_temp_root")
    tempfile.tempdir = str(temp)
    sys.path.insert(0, str(REPO))
    if args.mode == "child":
        identity = {"pid": os.getpid(), "process_instance": str(uuid.uuid4()), "interpreter": sys.executable}
        try:
            result = child(args)
        except AssertionError as error:
            print(json.dumps({**identity, "assertion": str(error)}))
            return 7
        print(json.dumps({**identity, "result": result}, sort_keys=True))
        return 0
    report = temp / "suite-summary.json"
    return sentinel_discovery(report) if args.mode == "discovery" else run_suite(report)


if __name__ == "__main__":
    raise SystemExit(main())
