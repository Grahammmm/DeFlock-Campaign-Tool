"""Synthetic per-file batch approval acceptance; transports must never run."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch

from campaign_tool import approval, outbox as ob, outbox_batch as batch
from tests import outbox_entry_driver
from tests.approval_signing import Signer

REPO = Path(__file__).resolve().parents[1]


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.box = ob.Outbox(self.root / "private" / "outbox.sqlite")
        self.addCleanup(self.box.close)
        self.out = self.root / "letters"
        for target in ("send", "send_email", "file_muckrock"):
            owner = ob.Outbox if target == "send" else ob
            guard = patch.object(owner, target, side_effect=AssertionError("batch must never send"))
            guard.start()
            self.addCleanup(guard.stop)

    def propose(self, request="one", agency="Agency A", fee=0, body=None):
        draft = ob.RequestDraft(request, agency, 1, "email", "Synthetic letter",
                                body or f"Dear {agency}, records please.", "records@example.invalid",
                                fee_cap_cents=fee)
        return self.box.propose(draft)["idempotency_key"]

    def sign(self, key, text=None):
        data = (self.out / (key + ".txt")).read_bytes()
        (self.out / (key + ".txt.sig")).write_text(text or "-----BEGIN SSH SIGNATURE-----\n" + hashlib.sha256(data).hexdigest())

    @staticmethod
    def verify(data, signature):
        if "-----BEGIN SSH SIGNATURE-----" not in signature:
            raise approval.ApprovalError("approval requires an SSH signature, not a name")
        if signature.splitlines()[-1] != hashlib.sha256(data).hexdigest():
            raise approval.ApprovalError("signature does not match the approved text")
        return "owner@example.invalid"

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = ob.main(["--journal", str(self.box.path), *args])
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    @contextmanager
    def fault(self, number=1, stage="write", hook=None, error=None):
        """Fail a real opened stream, after writing bytes or closing it."""
        original = os.fdopen
        streams = []
        failure = error or OSError(28, "synthetic disk full")

        class Stream:
            def __init__(inner, real, fail):
                inner.real, inner.fail = real, fail

            def __enter__(inner):
                return inner

            def fileno(inner):
                return inner.real.fileno()

            def write(inner, data):
                if inner.fail and stage == "write":
                    inner.real.write(data[:7])
                    inner.real.flush()
                    if hook:
                        hook()
                    raise failure
                return inner.real.write(data)

            def __exit__(inner, *args):
                try:
                    if inner.fail and stage == "flush":
                        inner.real.flush()
                        raise failure
                finally:
                    inner.real.close()
                if inner.fail and stage == "close":
                    if hook:
                        hook()
                    raise failure

        def opened(*args, **kwargs):
            real = original(*args, **kwargs)
            streams.append(real)
            return Stream(real, len(streams) == number)

        before = self.snapshot()
        with patch.object(batch.os, "fdopen", side_effect=opened), patch.object(
                ob.Outbox, "approve", side_effect=AssertionError("must not approve")):
            yield
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(streams)
        self.assertTrue(all(stream.closed for stream in streams))

    def snapshot(self):
        return [dict(row) for row in self.box.rows()]

    def retry_exact(self):
        reference = self.root / "reference"
        batch.export(self.box, reference)
        batch.export(self.box, self.out)
        self.assertEqual({p.name: p.read_bytes() for p in self.out.iterdir()},
                         {p.name: p.read_bytes() for p in reference.iterdir()})
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o777 == 0o600 for p in self.out.iterdir()))

    def test_second_payload_partial_write_rolls_back_and_retry_succeeds(self):
        self.propose()
        self.propose("two")
        before = self.snapshot()
        with self.fault(2), self.assertRaises(ob.OutboxError):
            batch.export(self.box, self.out)
        self.assertFalse(self.out.exists())
        self.assertEqual(self.snapshot(), before)
        self.retry_exact()

    def test_partial_index_failure_removes_payloads_and_index(self):
        self.propose()
        self.propose("two")
        before = self.snapshot()
        for stage in ("write", "flush", "close"):
            with self.subTest(stage=stage):
                with self.fault(3, stage), self.assertRaises(ob.OutboxError):
                    batch.export(self.box, self.out)
                self.assertFalse(self.out.exists())
                self.assertEqual(self.snapshot(), before)
        self.retry_exact()

    def test_first_payload_open_write_and_close_failures_leave_no_partial_file(self):
        self.propose()
        before = self.snapshot()
        original = os.open
        def refused(name, *args, **kwargs):
            if str(name).endswith(".txt"):
                raise OSError(28, "synthetic open failure")
            return original(name, *args, **kwargs)
        with patch.object(batch.os, "open", side_effect=refused), self.assertRaises(ob.OutboxError):
            batch.export(self.box, self.out)
        self.assertFalse(self.out.exists())
        for stage in ("write", "flush", "close"):
            with self.subTest(stage=stage):
                with self.fault(stage=stage), self.assertRaises(ob.OutboxError):
                    batch.export(self.box, self.out)
                self.assertFalse(self.out.exists())
        self.assertEqual(self.snapshot(), before)
        self.retry_exact()

    def test_fdopen_failure_closes_raw_descriptor_and_removes_output(self):
        self.propose()
        descriptors = []
        def refused(fd, *args, **kwargs):
            descriptors.append(fd)
            raise OSError(28, "synthetic fdopen failure")
        with patch.object(batch.os, "fdopen", side_effect=refused), self.assertRaises(ob.OutboxError):
            batch.export(self.box, self.out)
        self.assertEqual(len(descriptors), 1)
        with self.assertRaises(OSError):
            os.fstat(descriptors[0])
        self.assertFalse(self.out.exists())
        self.retry_exact()

    def test_empty_selection_preserves_existing_directory_mode(self):
        self.out.mkdir()
        self.out.chmod(0o750)
        self.assertEqual(batch.export(self.box, self.out), {"written": 0})
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o750)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_existing_empty_directory_survives_failed_export(self):
        self.propose()
        self.out.mkdir(mode=0o750)
        self.out.chmod(0o750)
        identity = self.out.stat().st_ino
        with self.fault(), self.assertRaises(ob.OutboxError):
            batch.export(self.box, self.out)
        self.assertEqual(self.out.stat().st_ino, identity)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o750)
        self.assertEqual(list(self.out.iterdir()), [])
        self.retry_exact()

    def test_foreign_file_arriving_during_export_survives_rollback(self):
        self.propose()
        sentinel = self.out / "foreign"
        def arrive():
            sentinel.write_bytes(b"foreign")
            sentinel.chmod(0o640)
        with self.fault(hook=arrive), self.assertRaisesRegex(ob.OutboxError, "partial local output remains"):
            batch.export(self.box, self.out)
        self.assertEqual(sentinel.read_bytes(), b"foreign")
        self.assertEqual(sentinel.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(self.out.iterdir()), [sentinel])
        with self.assertRaisesRegex(ob.OutboxError, "must be empty"):
            batch.export(self.box, self.out)

    def test_replaced_payload_is_not_unlinked_by_rollback(self):
        keys = sorted([self.propose(), self.propose("two")])
        target = self.out / (keys[0] + ".txt")
        saved = self.root / "saved"
        foreign = self.root / "foreign"
        foreign.write_bytes(b"foreign")
        for kind in ("regular", "symlink", "directory"):
            with self.subTest(kind=kind):
                def replace():
                    target.rename(saved)  # Keep the original inode alive (no inode reuse).
                    if kind == "regular":
                        target.write_bytes(b"replacement")
                    elif kind == "symlink":
                        target.symlink_to(foreign)
                    else:
                        target.mkdir()
                with self.fault(2, hook=replace), self.assertRaisesRegex(ob.OutboxError, "partial local output remains"):
                    batch.export(self.box, self.out)
                self.assertEqual(list(self.out.iterdir()), [target])
                if kind == "directory":
                    self.assertTrue(target.is_dir())
                    target.rmdir()
                else:
                    self.assertEqual(target.read_bytes(), b"foreign" if kind == "symlink" else b"replacement")
                    self.assertEqual(target.is_symlink(), kind == "symlink")
                    target.unlink()
                saved.unlink()
                self.out.rmdir()
        self.assertEqual(foreign.read_bytes(), b"foreign")

    def test_partial_payload_replacement_is_preserved(self):
        key = self.propose()
        target = self.out / (key + ".txt")
        def replace():
            target.rename(self.root / "saved")
            target.symlink_to(self.root / "absent")
        with self.fault(hook=replace), self.assertRaisesRegex(ob.OutboxError, "partial local output remains"):
            batch.export(self.box, self.out)
        self.assertTrue(target.is_symlink())

    def test_cleanup_failure_is_reported_without_abandoning_other_cleanup(self):
        keys = sorted([self.propose(), self.propose("two")])
        original = os.unlink
        attempted = []
        def unlink(name, *args, **kwargs):
            attempted.append(name)
            if name == keys[0] + ".txt":
                raise PermissionError("synthetic cleanup failure")
            return original(name, *args, **kwargs)
        with self.fault(3), patch.object(batch.os, "unlink", side_effect=unlink), self.assertRaisesRegex(
                ob.OutboxError, "partial local output remains"):
            batch.export(self.box, self.out)
        self.assertEqual(set(attempted), {k + ".txt" for k in keys} | {"INDEX.md"})
        self.assertEqual([p.name for p in self.out.iterdir()], [keys[0] + ".txt"])
        with self.assertRaisesRegex(ob.OutboxError, "must be empty"):
            batch.export(self.box, self.out)

    def test_replaced_output_directory_is_not_removed(self):
        self.propose()
        moved = self.root / "moved"
        def replace():
            self.out.rename(moved)
            self.out.mkdir()
            (self.out / "sentinel").write_bytes(b"foreign")
        with self.fault(hook=replace), self.assertRaisesRegex(ob.OutboxError, "partial local output remains"):
            batch.export(self.box, self.out)
        self.assertEqual(list(moved.iterdir()), [])
        self.assertEqual((self.out / "sentinel").read_bytes(), b"foreign")

    def test_export_faults_never_send_approve_or_update_journal(self):
        self.propose()
        before = self.snapshot()
        with patch.object(ob.Outbox, "approve", side_effect=AssertionError("must not approve")), self.fault():
            code, lines = self.cli("payload-batch", "--out", str(self.out))
        self.assertEqual(code, 2)
        self.assertEqual(lines[0]["state"], "refused")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.out.exists())

    def test_programming_exception_is_rolled_back_and_preserved(self):
        self.propose()
        self.propose("two")
        with self.fault(2, error=ValueError("synthetic bug")), self.assertRaisesRegex(ValueError, "synthetic bug"):
            batch.export(self.box, self.out)
        self.assertFalse(self.out.exists())

    def test_export_exact_proposed_payloads_modes_and_order(self):
        keys = [self.propose("one", "Agency B"), self.propose("two"), self.propose("three")]
        for request, state in (("four", "approved"), ("five", "sent")):
            self.box._set(self.propose(request), state=state)
        result = batch.export(self.box, self.out)
        self.assertEqual(result, {"written": 3, "dir": str(self.out)})
        self.assertEqual(len(list(self.out.iterdir())), 4)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        for key in keys:
            path = self.out / (key + ".txt")
            self.assertEqual(path.read_bytes(), self.box.approval_payload(key))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        index = (self.out / "INDEX.md").read_text()
        self.assertEqual((self.out / "INDEX.md").stat().st_mode & 0o777, 0o600)
        order = sorted(keys, key=lambda k: (self.box.row(k)["agency_id"], k))
        positions = [index.index(k + ".txt") for k in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(hashlib.sha256(self.box.approval_payload(keys[0])).hexdigest()[:12], index)
        self.assertTrue(index.endswith("ssh-keygen -Y sign -n deflock-approval -f <your key> *.txt\n"))

    def test_agency_filter_repeat_unknown_and_empty(self):
        a = self.propose()
        b = self.propose("two", "Agency B")
        code, lines = self.cli("payload-batch", "--out", str(self.out), "--agency", "Agency A")
        self.assertEqual((code, lines[0]["written"]), (0, 1))
        self.assertTrue((self.out / (a + ".txt")).exists())
        self.assertFalse((self.out / (b + ".txt")).exists())
        both = self.root / "both"
        code, lines = self.cli("payload-batch", "--out", str(both), "--agency", "Agency A", "--agency", "Agency B")
        self.assertEqual((code, lines[0]["written"]), (0, 2))
        absent = self.root / "absent"
        self.assertEqual(self.cli("payload-batch", "--out", str(absent), "--agency", "unknown"), (0, [{"written": 0}]))
        self.assertFalse(absent.exists())
        self.assertEqual(batch.approve_all(self.box, self.root / "both"),
                         [{"idempotency_key": k, "state": "no_signature"} for k in sorted([a, b])])

    def test_unsafe_output_paths_refused_without_writes(self):
        self.propose()
        full = self.root / "full"
        full.mkdir()
        (full / "keep").write_text("untouched")
        file = self.root / "file"
        file.write_text("untouched")
        link = self.root / "link"
        link.symlink_to(full, target_is_directory=True)
        broken = self.root / "broken"
        broken.symlink_to(self.root / "missing")
        for path in (full, file, link, broken):
            with self.subTest(path=path):
                code, lines = self.cli("payload-batch", "--out", str(path))
                self.assertEqual(code, 2)
                self.assertEqual(lines[0]["state"], "refused")
        self.assertEqual([p.name for p in full.iterdir()], ["keep"])
        self.assertEqual(file.read_text(), "untouched")

    def test_comparison_masks_only_agency_and_bounds_diff(self):
        # Same request in synthetic previews isolates the agency-only difference.
        a = self.box.row(self.propose())
        b = dict(a, agency_id="Agency B", draft_json=a["draft_json"].replace("Agency A", "Agency B"))
        self.assertEqual(batch._masked(a), batch._masked(b))
        with patch.object(self.box, "rows", return_value=[a, b]), patch.object(
                self.box, "approval_payload", return_value=self.box.approval_payload(a["idempotency_key"])):
            # Distinct filenames while keeping otherwise identical preview text.
            b["idempotency_key"] = "f" * 64
            batch.export(self.box, self.out)
        self.assertIn("no differences", (self.out / "INDEX.md").read_text())
        other = self.root / "fee"
        self.propose("fee", fee=500)
        batch.export(self.box, other)
        self.assertIn("+Fee cap (cents): 500", (other / "INDEX.md").read_text())
        long = self.root / "long"
        self.propose("long", body="\n".join(f"line {n}" for n in range(300)))
        batch.export(self.box, long)
        index = (long / "INDEX.md").read_text()
        self.assertIn("more lines; read the file", index)
        for section in index.split("```diff\n")[1:]:
            self.assertLessEqual(len(section.split("\n```")[0].splitlines()), 200)

    def test_valid_batch_and_idempotent_second_run(self):
        keys = [self.propose(str(n), "Agency A" if n < 2 else "Agency B") for n in range(3)]
        batch.export(self.box, self.out)
        for key in keys:
            self.sign(key)
        with patch.object(approval, "verify", side_effect=self.verify):
            code, lines = self.cli("approve-batch", "--dir", str(self.out))
            self.assertEqual(code, 0)
            self.assertEqual(lines[-1], {"approved": 3, "skipped": 0})
            self.assertEqual([r["idempotency_key"] for r in lines[:-1]], sorted(keys))
            for key in keys:
                row = self.box.row(key)
                self.assertEqual((row["state"], row["approved_by"]), ("approved", "owner@example.invalid"))
            code, lines = self.cli("approve-batch", "--dir", str(self.out))
            self.assertEqual(code, 2)
            self.assertEqual([r["state"] for r in lines[:-1]], ["not_proposed"] * 3)

    def test_mixed_failures_continue_and_do_not_approve_changed_rows(self):
        keys = [self.propose(str(n)) for n in range(5)]
        batch.export(self.box, self.out)
        for key in keys[1:]:
            self.sign(key)
        draft = json.loads(self.box.row(keys[1])["draft_json"])
        draft["body"] = "Changed after export"
        self.box._set(keys[1], draft_json=json.dumps(draft))
        self.sign(keys[2], "-----BEGIN SSH SIGNATURE-----\ninvalid")
        self.sign(keys[3], "Owner Name")
        with patch.object(approval, "verify", side_effect=self.verify):
            code, lines = self.cli("approve-batch", "--dir", str(self.out))
        self.assertEqual(code, 2)
        states = {r["idempotency_key"]: r for r in lines[:-1]}
        self.assertEqual([states[k]["state"] for k in keys], ["no_signature", "changed", "refused", "refused", "approved"])
        self.assertIn("approval requires an SSH signature", states[keys[3]]["reason"])
        self.assertEqual(lines[-1], {"approved": 1, "skipped": 4})
        for key in keys[:-1]:
            self.assertEqual(self.box.row(key)["state"], "proposed")

    def test_symlinks_oversized_signatures_unknown_and_nonregular_files(self):
        keys = [self.propose(str(n)) for n in range(4)]
        batch.export(self.box, self.out)
        payload = self.out / (keys[0] + ".txt")
        payload.unlink()
        payload.symlink_to(self.out / (keys[1] + ".txt"))
        (self.out / (keys[1] + ".txt.sig")).write_bytes(b"x" * (approval.MAX_SIGNATURE + 1))
        self.sign(keys[2])
        signature = self.out / (keys[2] + ".txt.sig")
        signature.unlink()
        signature.symlink_to(self.out / (keys[1] + ".txt.sig"))
        (self.out / "foo.txt").write_text("stray")
        os.mkfifo(self.out / "fifo.txt")
        (self.out / "directory.txt").mkdir()
        self.sign(keys[3])
        with patch.object(approval, "verify", side_effect=self.verify):
            results = batch.approve_all(self.box, self.out)
        states = {r["idempotency_key"]: r["state"] for r in results}
        self.assertEqual([states[k] for k in keys], ["refused", "refused", "refused", "approved"])
        self.assertEqual(states["foo"], "unknown_key")
        self.assertEqual(states["fifo"], "refused")
        self.assertEqual(states["directory"], "refused")
        link = self.root / "link"
        link.symlink_to(self.out)
        self.assertEqual(self.cli("approve-batch", "--dir", str(link))[0], 2)

    def test_moved_round_and_edited_payload_are_changed(self):
        a, b = self.propose("a"), self.propose("b")
        batch.export(self.box, self.out)
        self.sign(a)
        self.sign(b)
        (self.out / (a + ".txt")).write_bytes(b"edited")
        with patch.object(self.box, "_round", return_value=1):
            results = batch.approve_all(self.box, self.out)
        self.assertEqual([r["state"] for r in results], ["changed", "changed"])
        self.assertFalse(self.box.db.in_transaction)


class EntryPointTests(unittest.TestCase):
    """The documented `python3 -m campaign_tool.outbox` entry point, run in a real subprocess."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.journal = self.root / "private" / "outbox.sqlite"
        self.out = self.root / "letters"
        self.signer = Signer()
        self.addCleanup(self.signer.close)

    def run_cli(self, *args, trusted=False):
        entry = [outbox_entry_driver.__file__, str(self.signer.signers)] if trusted \
            else ["-m", "campaign_tool.outbox"]
        result = subprocess.run([sys.executable, "-B", *entry, "--journal", str(self.journal), *args],
                                cwd=REPO, capture_output=True, text=True, timeout=120, check=False)
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, [json.loads(line) for line in result.stdout.splitlines()]

    def test_invalid_signature_then_valid_letter_and_path_errors(self):
        box = ob.Outbox(self.journal)
        try:
            keys = sorted(box.propose(ob.RequestDraft(r, "Agency A", 1, "email", "Synthetic letter",
                                                      "Dear Agency A, records please.",
                                                      "records@example.invalid"))["idempotency_key"]
                          for r in ("one", "two"))
        finally:
            box.close()
        self.assertEqual(self.run_cli("payload-batch", "--out", str(self.out)), (0, [{"written": 2, "dir": str(self.out)}]))
        bad, good = (self.out / (k + ".txt") for k in keys)
        Path(str(bad) + ".sig").write_text("-----BEGIN SSH SIGNATURE-----\ninvalid\n-----END SSH SIGNATURE-----\n")
        Path(str(good) + ".sig").write_text(self.signer.sign(good.read_bytes()))
        code, lines = self.run_cli("approve-batch", "--dir", str(self.out), trusted=True)
        self.assertEqual(code, 2)
        self.assertEqual([(r["idempotency_key"], r["state"]) for r in lines[:-1]],
                         [(keys[0], "refused"), (keys[1], "approved")])
        self.assertIn("approval refused", lines[0]["reason"])
        self.assertEqual(lines[1]["approved_by"], "owner@example.invalid")
        self.assertEqual(lines[-1], {"approved": 1, "skipped": 1})
        box = ob.Outbox(self.journal)
        try:
            self.assertEqual([box.row(k)["state"] for k in keys], ["proposed", "approved"])
        finally:
            box.close()
        self.assertEqual(self.run_cli("payload-batch", "--out", str(self.out)),
                         (2, [{"state": "refused", "reason": "output directory must be empty"}]))
        code, lines = self.run_cli("approve-batch", "--dir", str(self.root / "missing"))
        self.assertEqual((code, lines[0]["state"]), (2, "refused"))


if __name__ == "__main__":
    unittest.main()
