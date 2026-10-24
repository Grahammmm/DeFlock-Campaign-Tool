"""FC-CLI-REPLAY: a saved understanding run is reused only when it is safe.

Every fixture is synthetic and lives in a fresh TemporaryDirectory. Hashes are
derived from labelled fixture bytes; nothing describes a real record, agency,
person or source. Owner and file-type failures are simulated with mocks; no
real ownership change is made.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from campaign_tool.records import grounded_evidence
from campaign_tool.records.understanding import cli, evidence

REPO = Path(__file__).resolve().parents[2]
AGENCY = "example-agency"
REQUEST = "R-0001"
PAGE_ONE = (
    "Public records request R-0001\n"
    "Agency: Example Agency\n"
    "Date: 2030-01-15\n"
    "Policy 100.1 requires retention of audit log entries for 30 days."
)
PAGE_TWO = '{"retention_days": 30, "system": "synthetic"}'
STABLE_CODE = re.compile(r"[a-z_]+\Z")


def digest(data):
    return hashlib.sha256(data).hexdigest()


ORIGINAL = digest(b"fc-cli-replay synthetic original document")
EXPECTED_NAMES = {
    "policy": ["receipt.json", "result.json"],
    "document": ["receipt.json", "result.json"],
    "requests": ["due-actions.json", "receipt.json", "result.json"],
    "reconcile": ["findings.md", "receipt.json", "result.json"],
}


def native_receipt():
    return {
        "schema": "format-extraction-v1", "original_sha256": ORIGINAL,
        "original_changed": False, "status": "complete", "parser": "synthetic",
        "parser_version": "0", "route": {"format": "pdf"},
        "pages": [{"page_no": 1, "status": "ok"}, {"page_no": 2, "status": "ok"}],
        "units": [
            {"kind": "text", "locator": {"page": 1}, "text": PAGE_ONE, "data": {}},
            {"kind": "text", "locator": {"page": 2}, "text": PAGE_TWO, "data": {}},
        ],
    }


def make_bundle():
    raw = evidence.canonical(native_receipt())
    return evidence.normalize_receipt(native_receipt(), ORIGINAL, digest(raw))


def policy_versions(requirements=None):
    return [{
        "version_id": "v1", "family_id": "retention", "jurisdiction": "example-state",
        "agency_id": "*", "regulated_roles": ["operator"], "effective_from": "2029-01-01",
        "issued_at": "2028-12-01", "retrieved_at": "2031-01-01",
        "supersession_status": "not_superseded_as_of_retrieval",
        "source": {"source_id": "synthetic-policy", "url": "https://example.invalid/policy",
                   "locator": "section 100.1", "verification_status": "verified",
                   "content_sha256": ORIGINAL},
        "requirements": requirements if requirements is not None else [],
    }]


def command_input(command):
    if command == "policy":
        return {"versions": policy_versions()}
    if command == "document":
        return {"bundle": make_bundle()}
    if command == "requests":
        return {"as_of": "2030-02-01T00:00:00Z", "events": [
            {"agency_id": AGENCY, "request_number": REQUEST, "event_type": "request",
             "occurred_at": "2030-01-01T10:00:00Z", "items_requested": ["audit logs"]},
            {"agency_id": AGENCY, "request_number": REQUEST,
             "text": "We received your request.", "occurred_at": "2030-01-02T10:00:00Z"},
        ]}
    bundle = make_bundle()
    req_text = "audit log entries for 30 days"
    rule = evidence.cite_text(bundle["units"][0], req_text)
    versions = policy_versions(requirements=[{
        "requirement_id": "r1", "text": req_text, "citations": [rule],
        "test": {"field": "retention_days", "operator": "at_most", "expected": 30}}])
    fact = grounded_evidence.pointer_support(bundle["units"][1], "/retention_days")
    return {"bundle": bundle, "versions": versions,
            "facts": [{"fact_id": "f1", "field": "retention_days", "value": 30, "citations": [fact]}],
            "scope": {"agency_id": AGENCY, "jurisdiction": "example-state",
                      "regulated_role": "operator", "event_at": "2030-01-15"}}


def snapshot(root):
    """Type, mode, identity and bytes of every entry, never following links."""
    out = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = os.path.join(base, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                body = ("link", os.readlink(path))
            elif stat.S_ISREG(st.st_mode):
                with open(path, "rb") as f:
                    body = ("file", f.read())
            else:
                body = ("other", None)
            out[os.path.relpath(path, root)] = (stat.S_IFMT(st.st_mode), stat.S_IMODE(st.st_mode), st.st_ino, body)
    return out


def fake_stat(real, **changes):
    fields = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size",
              "st_mtime_ns", "st_ctime_ns", "st_atime_ns")
    values = {k: getattr(real, k) for k in fields}
    values.update(changes)
    return types.SimpleNamespace(**values)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.root.chmod(0o700)
        self.output = self.root / "out"
        self.outside = self.root / "outside"
        self.outside.mkdir(mode=0o700)

    def run_command(self, command):
        value = command_input(command)
        return cli.run(command, value, digest(evidence.canonical(value)), str(self.output))

    def saved(self, command="policy"):
        first = self.run_command(command)
        self.assertFalse(first["reused"])
        return first, Path(first["output"])

    def assert_refused(self, command, codes, *patches):
        """Replay must raise a stable non-payload code and leave every byte as it was."""
        before = snapshot(self.root)
        with self.assertRaises(ValueError) as caught:
            with _stack(patches):
                self.run_command(command)
        code = str(caught.exception)
        self.assertRegex(code, STABLE_CODE)
        self.assertIn(code, codes)
        self.assertEqual(snapshot(self.root), before)
        return code

    def replace_with_symlink(self, run_dir, name):
        target = self.outside / ("copy-" + name)
        target.write_bytes((run_dir / name).read_bytes())
        target.chmod(0o400)
        (run_dir / name).unlink()
        os.symlink(target, run_dir / name)

    def replace_with_bytes(self, run_dir, name, raw, mode=0o400):
        (run_dir / name).unlink()
        (run_dir / name).write_bytes(raw)
        (run_dir / name).chmod(mode)

    # Fresh runs and legitimate replay.
    def test_fresh_and_exact_replay_for_every_command(self):
        for command, names in EXPECTED_NAMES.items():
            with self.subTest(command=command):
                first, run_dir = self.saved(command)
                self.assertEqual(sorted(p.name for p in run_dir.iterdir()), names)
                self.assertEqual(stat.S_IMODE(os.lstat(run_dir).st_mode), 0o700)
                for name in names:
                    st = os.lstat(run_dir / name)
                    self.assertTrue(stat.S_ISREG(st.st_mode))
                    self.assertEqual(stat.S_IMODE(st.st_mode), 0o400)
                receipt = json.loads((run_dir / "receipt.json").read_bytes())
                result_raw = (run_dir / "result.json").read_bytes()
                self.assertEqual(receipt["run_id"], first["run_id"])
                self.assertEqual(receipt["bindings"]["result_sha256"], digest(result_raw))
                before = snapshot(self.root)
                second = self.run_command(command)
                self.assertTrue(second["reused"])
                self.assertEqual(second["run_id"], first["run_id"])
                self.assertEqual(second["output"], first["output"])
                self.assertEqual(snapshot(self.root), before)

    def test_legitimate_private_replay_reused(self):
        first, _ = self.saved("policy")
        second = self.run_command("policy")
        self.assertTrue(second["reused"])
        self.assertEqual(second["run_id"], first["run_id"])

    def test_byte_equal_private_regular_replacement_is_still_valid(self):
        _, run_dir = self.saved("requests")
        self.replace_with_bytes(run_dir, "due-actions.json", (run_dir / "due-actions.json").read_bytes())
        self.assertTrue(self.run_command("requests")["reused"])

    # Root, intermediate and leaf symlinks.
    def test_root_symlink_refused(self):
        self.saved("policy")
        real = self.root / "real-out"
        os.rename(self.output, real)
        os.symlink(real, self.output)
        self.assert_refused("policy", {"output_symlink_or_git"})

    def test_intermediate_run_directory_symlink_refused(self):
        _, run_dir = self.saved("policy")
        moved = self.outside / "moved-run"
        os.rename(run_dir, moved)
        os.symlink(moved, run_dir)
        self.assert_refused("policy", {"existing_run_symlink"})

    def test_dangling_run_directory_symlink_refused(self):
        first = self.run_command("policy")
        run_dir = Path(first["output"])
        os.rename(run_dir, self.outside / "moved-run")
        os.symlink(self.outside / "absent-run", run_dir)
        self.assert_refused("policy", {"existing_run_symlink"})

    def test_leaf_symlink_byte_equal_result_refused(self):
        _, run_dir = self.saved("policy")
        self.replace_with_symlink(run_dir, "result.json")
        self.assert_refused("policy", {"symlink_artifact"})

    def test_leaf_symlink_each_artifact_refused(self):
        for command, names in EXPECTED_NAMES.items():
            for name in names:
                with self.subTest(command=command, name=name):
                    self.setUp()
                    _, run_dir = self.saved(command)
                    self.replace_with_symlink(run_dir, name)
                    self.assert_refused(command, {"symlink_artifact"})

    # Independently replaced artifacts.
    def test_result_receipt_and_optional_artifacts_replaced_independently(self):
        cases = [("policy", "result.json"), ("policy", "receipt.json"),
                 ("requests", "due-actions.json"), ("reconcile", "findings.md")]
        for command, name in cases:
            with self.subTest(command=command, name=name):
                self.setUp()
                _, run_dir = self.saved(command)
                old = (run_dir / name).read_bytes()
                altered = old[:-2] + (b"X" if old[-2:-1] != b"X" else b"Y") + old[-1:]
                self.assertEqual(len(altered), len(old))
                self.replace_with_bytes(run_dir, name, altered)
                self.assert_refused(command, {"existing_run_conflict"})

    # Unsafe artifact or directory properties.
    def test_nonprivate_artifact_refused(self):
        _, run_dir = self.saved("policy")
        (run_dir / "receipt.json").chmod(0o440)
        self.assert_refused("policy", {"private_artifact_bound"})

    def test_nonprivate_run_directory_refused(self):
        _, run_dir = self.saved("policy")
        run_dir.chmod(0o750)
        self.assert_refused("policy", {"existing_run_unsafe"})

    def test_real_fifo_artifact_refused_without_blocking(self):
        _, run_dir = self.saved("policy")
        (run_dir / "receipt.json").unlink()
        os.mkfifo(run_dir / "receipt.json", 0o600)

        def blocked(signum, frame):
            raise AssertionError("fifo_read_blocked")
        previous = signal.signal(signal.SIGALRM, blocked)
        signal.alarm(10)
        try:
            self.assert_refused("policy", {"private_artifact_bound"})
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)

    def test_mocked_nonregular_artifact_refused(self):
        self.saved("policy")
        real = os.fstat

        def nonregular(fd):
            st = real(fd)
            return fake_stat(st, st_mode=stat.S_IFIFO | 0o400) if stat.S_ISREG(st.st_mode) else st
        self.assert_refused("policy", {"private_artifact_bound"}, mock.patch("os.fstat", nonregular))

    def test_mocked_wrong_owner_artifact_refused(self):
        self.saved("policy")
        real = os.fstat

        def foreign(fd):
            st = real(fd)
            return fake_stat(st, st_uid=os.geteuid() + 1) if stat.S_ISREG(st.st_mode) else st
        self.assert_refused("policy", {"private_artifact_bound"}, mock.patch("os.fstat", foreign))

    def test_mocked_wrong_owner_run_directory_refused(self):
        _, run_dir = self.saved("policy")
        real = os.lstat

        def foreign(path, *args, **kwargs):
            st = real(path, *args, **kwargs)
            return fake_stat(st, st_uid=os.geteuid() + 1) if os.fspath(path) == str(run_dir) else st
        self.assert_refused("policy", {"existing_run_unsafe"}, mock.patch("os.lstat", foreign))

    def test_oversized_artifact_refused(self):
        _, run_dir = self.saved("policy")
        self.replace_with_bytes(run_dir, "result.json", (run_dir / "result.json").read_bytes() + b" " * 64)
        self.assert_refused("policy", {"private_artifact_bound"})

    def test_missing_artifact_refused(self):
        for command, name in (("policy", "receipt.json"), ("requests", "due-actions.json"),
                              ("reconcile", "findings.md")):
            with self.subTest(command=command, name=name):
                self.setUp()
                _, run_dir = self.saved(command)
                (run_dir / name).unlink()
                self.assert_refused(command, {"existing_run_artifact_set"})

    def test_extra_artifact_refused(self):
        for extra in ("extra.json", ".hidden"):
            with self.subTest(extra=extra):
                self.setUp()
                _, run_dir = self.saved("policy")
                (run_dir / extra).write_bytes(b"synthetic extra")
                (run_dir / extra).chmod(0o400)
                self.assert_refused("policy", {"existing_run_artifact_set"})

    def test_extra_due_actions_on_policy_run_refused(self):
        _, run_dir = self.saved("policy")
        (run_dir / "due-actions.json").write_bytes(b"{}\n")
        (run_dir / "due-actions.json").chmod(0o400)
        self.assert_refused("policy", {"existing_run_artifact_set"})

    def test_metadata_change_during_read_refused(self):
        self.saved("policy")
        real = os.fstat
        calls = {}

        def drifting(fd):
            st = real(fd)
            if not stat.S_ISREG(st.st_mode):
                return st
            calls[fd] = calls.get(fd, 0) + 1
            return fake_stat(st, st_mtime_ns=st.st_mtime_ns + calls[fd]) if calls[fd] > 1 else st
        self.assert_refused("policy", {"artifact_changed"}, mock.patch("os.fstat", drifting))

    def test_cli_refusal_is_stable_and_non_payload(self):
        value = command_input("policy")
        path = self.root / "input.json"
        path.write_bytes(evidence.canonical(value))
        path.chmod(0o600)
        argv = [sys.executable, "-B", "-m", "campaign_tool.records.understanding", "policy",
                "--input", str(path), "--output", str(self.output)]
        first = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual(first.returncode, 0, first.stderr)
        run_dir = Path(json.loads(first.stdout)["output"])
        self.replace_with_symlink(run_dir, "result.json")
        before = snapshot(self.root)
        bad = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(bad.stdout, "")
        self.assertEqual(bad.stderr.strip(), "understanding_input_or_evidence_validation_failed")
        self.assertEqual(snapshot(self.root), before)


class _stack:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        return False


if __name__ == "__main__":
    unittest.main()
