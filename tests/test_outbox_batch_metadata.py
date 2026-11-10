"""Synthetic regressions: batch comparison refuses a non-object ``extra`` before any output effect."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from campaign_tool import approval, outbox as ob, outbox_batch as batch

REPO = Path(__file__).resolve().parents[1]
REASON = "draft extra must be an object for batch comparison"
SENTINEL = "PRIVATE-SENTINEL-4c1e"
# Every explicit non-object JSON value, including the falsey ones an ``or {}`` shortcut would hide.
MALFORMED = {"null": None, "empty list": [], "list": [SENTINEL], "empty string": "", "string": SENTINEL,
             "false": False, "true": True, "zero": 0, "integer": 7, "zero float": 0.0, "float": 1.5}

# Independently pinned from the unrepaired checked-main exporter (see PR body); never regenerated from the fix.
LEGACY = {
    "absent": (None, "b9d5d50c4bdb77710b0df17a96ddf1d07ee4df6e82d89fc5da752450d904a24c",
               "a740ff397529a79e1010916f45e5e36c1348e4e69dbb7c4dc234866c92c287cc"),
    "empty": ({}, "96a6387d3962bb15cf9260f6b94c0ae90a1e59bf4e456cd88556c56b3014ade6",
              "ab52b7b29c0f5a34515fef9713b53c2db65e3ccf8d078531f34e0473b6528bf4"),
    "named": ({"agency_name": "Alpha Records Bureau"},
              "d590bdcb657d5c02abb30282fe66883c4f6bb2625fba5db91d26bc5acffa80f6",
              "23a60822b5924840a841861dac97eb8919003e3317005f28f0f0481322c72423"),
    "mixed": ({"agency_name": "Zoë Café Ünit", "nested": {"agency_name": 7, "k": [1, 2]}, "n": None},
              "5a243044be02605e79f2e71fe61fd3284fcaccab4b6a782624d20125c7017427",
              "fa7c41804df36078ce5a04524f7c56c87bf0141b01590bceb2419d6d9aaa5102"),
    "nonstring": ({"agency_name": 5}, "8036892a52e1ed5f2f18473b2e9238637c1ccfaa724ca4697116cd3bc06774cd",
                  "a5ab4ec15bbebd856311e1690c523609e0312405ee5a2ecfc017e84f2555733e"),
}
_EMPTY = object()
LEGACY_INDEX = "40c918504f87d5d6160222a2263bdb96abb7f66a6d2024474c6e1c16d44353b2"


class MetadataBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.box = ob.Outbox(self.root / "private" / "outbox.sqlite")
        self.addCleanup(self.box.close)
        self.out = self.root / "letters"
        for owner, target in ((ob.Outbox, "send"), (ob, "send_email"), (ob, "file_muckrock"),
                              (ob.Outbox, "approve"), (approval, "verify")):
            guard = patch.object(owner, target, side_effect=AssertionError("export must never send, verify or approve"))
            guard.start()
            self.addCleanup(guard.stop)

    def propose(self, request="one", agency="Agency A", extra=_EMPTY):
        """Propose through the real API; ``extra`` is stored as given (default: an empty object)."""
        draft = ob.RequestDraft(request, agency, 1, "email", "Synthetic letter",
                                f"Dear {agency}, records please. {SENTINEL}", "records@example.invalid",
                                extra={} if extra is _EMPTY else extra)
        return self.box.propose(draft)["idempotency_key"]

    def strip_extra(self, key):
        data = json.loads(self.box.row(key)["draft_json"])
        del data["extra"]
        self.box.db.execute("UPDATE outbox SET draft_json=? WHERE idempotency_key=?",
                            (json.dumps(data, sort_keys=True), key))

    def journal(self):
        return ([dict(r) for r in self.box.db.execute("SELECT * FROM outbox ORDER BY idempotency_key")],
                [tuple(r) for r in self.box.db.execute("SELECT * FROM outbox_retry_approval")])

    def cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = ob.main(["--journal", str(self.box.path), *args])
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    @staticmethod
    def disk(path):
        """Identity, mode, bytes and mtime of an object without following a link."""
        path = Path(path)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        entry = (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns)
        if path.is_symlink():
            return entry + (os.readlink(path),)
        if path.is_dir():
            return entry + (sorted(p.name for p in path.iterdir()),)
        return entry + (path.read_bytes(),)

    def tripwires(self):
        """Spies that fail if the exporter touches the output path or generates a payload."""
        stack = []
        for owner, name in ((ob.Outbox, "approval_payload"), (batch, "_write"), (batch, "_directory"),
                            (batch.os, "open"), (batch.os, "fchmod"), (batch.os, "listdir"),
                            (Path, "mkdir"), (Path, "exists"), (Path, "is_symlink"), (Path, "lstat")):
            stack.append(patch.object(owner, name, side_effect=AssertionError("output effect before preflight: " + name)))
        return stack


class _Spies:
    def __init__(self, patches):
        self.patches = patches

    def __enter__(self):
        for p in self.patches:
            p.start()

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()


class NonObjectExtraTests(MetadataBase):
    def test_non_object_extra_refuses_without_traceback(self):
        for label, extra in MALFORMED.items():
            with self.subTest(extra=label):
                box = ob.Outbox(self.root / label.replace(" ", "-") / "outbox.sqlite")
                self.addCleanup(box.close)
                draft = ob.RequestDraft("r", "Agency A", 1, "email", "Synthetic letter", "body " + SENTINEL,
                                        "records@example.invalid", extra=extra)
                key = box.propose(draft)["idempotency_key"]
                self.assertTrue(box.approval_payload(key))  # the ordinary API accepts and renders it
                out = self.root / ("out-" + label.replace(" ", "-"))
                with self.assertRaises(ob.OutboxError) as caught:
                    batch.export(box, out)
                self.assertEqual(str(caught.exception), REASON)
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn(SENTINEL, str(caught.exception))
                self.assertFalse(out.exists())
                with self.assertRaises(ob.OutboxError) as direct:
                    batch._masked(box.row(key))
                self.assertEqual(str(direct.exception), REASON)

    def test_invalid_later_selected_row_preflights_before_any_output_effect(self):
        self.propose("one", "Agency A")
        self.propose("two", "Agency B", extra=None)  # explicit null, sorts after the valid row
        self.propose("three", "Agency C")
        existing = self.root / "existing"
        existing.mkdir(mode=0o750)
        os.chmod(existing, 0o750)
        for order in ("later", "first"):
            if order == "first":
                self.box.db.execute("UPDATE outbox SET agency_id='Agency 0' WHERE agency_id='Agency B'")
                self.box.db.execute(
                    "UPDATE outbox SET draft_json=replace(draft_json, 'Agency B', 'Agency 0') WHERE agency_id='Agency 0'")
            for target in (self.out, existing):
                with self.subTest(order=order, target=target.name):
                    before = self.disk(target)
                    journal = self.journal()
                    with _Spies(self.tripwires()):
                        with self.assertRaises(ob.OutboxError) as caught:
                            batch.export(self.box, target)
                    self.assertEqual(str(caught.exception), REASON)
                    self.assertEqual(self.disk(target), before)
                    self.assertEqual(self.journal(), journal)

    def test_refusal_preserves_output_objects_and_journal(self):
        self.propose("one", "Agency A")
        self.propose("two", "Agency B", extra=None)
        referent = self.root / "referent"
        referent.mkdir()
        (referent / "keep").write_text("referent bytes")
        empty = self.root / "empty"
        empty.mkdir()
        os.chmod(empty, 0o750)
        full = self.root / "full"
        full.mkdir()
        (full / "keep").write_text("sentinel bytes")
        plain = self.root / "plain"
        plain.write_text("file bytes")
        link = self.root / "link"
        link.symlink_to(referent, target_is_directory=True)
        dangling = self.root / "dangling"
        dangling.symlink_to(self.root / "missing")
        targets = [self.out, empty, full, plain, link, dangling]
        journal = self.journal()
        for target in targets:
            with self.subTest(target=target.name):
                before = (self.disk(target), self.disk(referent), self.disk(self.root / "missing"))
                with self.assertRaises(ob.OutboxError) as caught:
                    batch.export(self.box, target)
                self.assertEqual(str(caught.exception), REASON)
                self.assertEqual((self.disk(target), self.disk(referent), self.disk(self.root / "missing")), before)
                self.assertEqual(self.journal(), journal)
        self.assertEqual(oct(empty.stat().st_mode & 0o777), "0o750")
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         sorted(["private", "referent", "empty", "full", "plain", "link", "dangling"]))

    def test_real_module_cli_reports_one_private_refusal(self):
        self.propose("one", "Agency A")
        self.propose("two", "Agency B", extra=SENTINEL)
        journal = self.journal()
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        for label, value in (("null", None), ("array", [SENTINEL])):
            stored = json.loads(self.box.db.execute(
                "SELECT draft_json FROM outbox WHERE agency_id='Agency B'").fetchone()[0])
            self.box.db.execute("UPDATE outbox SET draft_json=? WHERE agency_id='Agency B'",
                                (json.dumps(dict(stored, extra=value), sort_keys=True),))
            journal = self.journal()
            out = self.root / ("module-" + label)
            with self.subTest(case=label):
                done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.outbox", "--journal",
                                       str(self.box.path), "payload-batch", "--out", str(out)],
                                      cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
                self.assertEqual(done.returncode, 2)
                self.assertEqual(done.stderr, "")
                self.assertNotIn("Traceback", done.stdout)
                self.assertEqual([json.loads(line) for line in done.stdout.splitlines()],
                                 [{"state": "refused", "reason": REASON}])
                for private in (SENTINEL, str(out), str(self.root)):
                    self.assertNotIn(private, done.stdout)
                self.assertFalse(out.exists())
                self.assertEqual(self.journal(), journal)
            with self.subTest(case=label + " in process"):
                code, lines = self.cli("payload-batch", "--out", str(out))
                self.assertEqual((code, lines), (2, [{"state": "refused", "reason": REASON}]))
                self.assertFalse(out.exists())

    def test_filter_and_state_selection_do_not_validate_excluded_rows(self):
        good = self.propose("one", "Agency A")
        bad = self.propose("two", "Agency B", extra=None)
        settled = self.propose("three", "Agency C", extra=[])
        self.box._set(settled, state="approved")
        journal = self.journal()
        # An unfiltered selection includes the malformed proposed row and is refused.
        with self.assertRaises(ob.OutboxError):
            batch.export(self.box, self.root / "unfiltered")
        # A filter that excludes it, and a non-proposed malformed row, do not block a valid export.
        result = batch.export(self.box, self.out, ["Agency A", "Agency C"])
        self.assertEqual(result, {"written": 1, "dir": str(self.out)})
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), sorted([good + ".txt", "INDEX.md"]))
        self.assertNotIn(bad, (self.out / "INDEX.md").read_text())
        self.assertEqual(self.journal(), journal)
        # All-excluded selection keeps the no-op and the existing destination validation.
        absent = self.root / "absent"
        self.assertEqual(batch.export(self.box, absent, ["unknown"]), {"written": 0})
        self.assertFalse(absent.exists())
        self.assertEqual(self.cli("payload-batch", "--out", str(absent), "--agency", "Agency C"), (0, [{"written": 0}]))
        full = self.root / "full"
        full.mkdir()
        (full / "keep").write_text("untouched")
        with self.assertRaisesRegex(ob.OutboxError, "must be empty"):
            batch.export(self.box, full, ["unknown"])
        self.assertEqual([p.name for p in full.iterdir()], ["keep"])


class AcceptedMetadataTests(MetadataBase):
    def build_legacy(self):
        keys = {}
        for label, (extra, _payload, _mask) in LEGACY.items():
            agency = "Zoë Café Ünit" if label == "mixed" else "Alpha Records Bureau"
            draft = ob.RequestDraft("req-" + label, "AG-" + label.upper(), 1, "email", "Synthetic letter",
                                    f"Dear {agency} (AG-{label.upper()}), records please.", "records@example.invalid",
                                    extra={} if extra is None else extra)
            keys[label] = self.box.propose(draft)["idempotency_key"]
            if extra is None:
                self.strip_extra(keys[label])
        return keys

    def test_missing_and_object_extra_keep_exact_legacy_bytes(self):
        keys = self.build_legacy()
        self.assertNotIn('"extra"', self.box.row(keys["absent"])["draft_json"])
        for label, (_extra, payload, mask) in LEGACY.items():
            self.assertEqual(hashlib.sha256("\n".join(batch._masked(self.box.row(keys[label]))).encode()).hexdigest(),
                             mask, label)
        self.assertEqual(batch.export(self.box, self.out), {"written": 5, "dir": str(self.out)})
        for label, (_extra, payload, _mask) in LEGACY.items():
            data = (self.out / (keys[label] + ".txt")).read_bytes()
            self.assertEqual(data, self.box.approval_payload(keys[label]))
            self.assertEqual(hashlib.sha256(data).hexdigest(), payload, label)
        self.assertEqual(hashlib.sha256((self.out / "INDEX.md").read_bytes()).hexdigest(), LEGACY_INDEX)
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)

    def test_late_io_failure_still_uses_existing_owned_rollback(self):
        self.propose("one", "Agency A", extra={"agency_name": "Alpha"})
        self.propose("two", "Agency B", extra={"x": 1})
        real = batch._write
        existing = self.root / "existing"
        existing.mkdir()
        os.chmod(existing, 0o750)
        for fail_at in (2, 3):  # second payload, then INDEX.md
            for target in (self.out, existing):
                calls = []

                def flaky(fd, name, data):
                    calls.append(name)
                    if len(calls) == fail_at:
                        raise OSError(28, "synthetic disk full")
                    return real(fd, name, data)

                with self.subTest(fail_at=fail_at, target=target.name):
                    with patch.object(batch, "_write", side_effect=flaky):
                        with self.assertRaises(ob.OutboxError) as caught:
                            batch.export(self.box, target)
                    self.assertNotEqual(str(caught.exception), REASON)
                    self.assertEqual(len(calls), fail_at)
                    if target is self.out:
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(list(target.iterdir()), [])
                        self.assertEqual(target.stat().st_mode & 0o777, 0o750)

    def test_validation_is_narrow_and_never_changes_authority(self):
        key = self.propose("one", "Agency A", extra={"agency_name": "Alpha"})
        self.propose("two", "Agency B", extra={})
        payload = self.box.approval_payload(key)
        journal = self.journal()
        with patch.object(batch, "preview", side_effect=AttributeError("synthetic bug")):
            with self.assertRaises(AttributeError):
                batch.export(self.box, self.out)
        self.assertFalse(self.out.exists())
        batch.export(self.box, self.out)
        self.assertEqual((self.out / (key + ".txt")).read_bytes(), payload)
        self.assertEqual(self.box.approval_payload(key), payload)
        self.assertEqual(self.journal(), journal)  # no approval, state change or draft rewrite


if __name__ == "__main__":
    unittest.main()
