"""kit fee-reply: an OSError while loading campaign inputs is one fixed, path-free refusal. Offline, synthetic."""
import errno
import json
import os
import stat
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, kit, kit_fee_reply, kit_track, law  # noqa: E402
from tests.test_kit_fee_reply import A, TODAY, FeeReplyTests as Fixture, run, snapshot  # noqa: E402

FIXED = "kit fee-reply: unable to read campaign inputs"
PRIVATE = "SYNTHETIC-PRIVATE-MESSAGE"
PRIVATE_FILE = "/SYNTHETIC-PRIVATE-DIR/input.json"


def oserror(cls=PermissionError, code=errno.EACCES):
    error = cls(code, PRIVATE)
    error.filename = PRIVATE_FILE
    return error


class FeeReplyReadErrorTests(unittest.TestCase):
    def setUp(self):  # reuse the fixture without inheriting its test methods
        self.fx = Fixture("test_main_named_same_day_latest")
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.root = self.fx.root
        self.fx.estimate()

    def inputs_snapshot(self):
        return snapshot(self.root), {p: stat.S_IMODE(p.stat().st_mode) for p in self.root.rglob("*")}

    def faults(self):
        return {
            "agencies": (kit, "_load_existing"),
            "tracker": (kit_track, "read_sent"),
            "config": (cli, "read_config"),
            "law": (law, "load_package"),
        }

    def test_each_input_read_is_one_fixed_path_free_refusal(self):
        for label, (module, attr) in self.faults().items():
            for error in (oserror(), oserror(FileNotFoundError, errno.ENOENT), oserror(OSError, errno.EIO)):
                with self.subTest(label=label, error=type(error).__name__):
                    before = self.inputs_snapshot()
                    with mock.patch.object(module, attr, side_effect=error) as seam:
                        with self.assertRaises(ValueError) as caught:
                            self.fx.make()
                    self.assertEqual(seam.call_count, 1)  # reached, not retried
                    self.assertEqual(str(caught.exception), FIXED)
                    self.assertIsNone(caught.exception.__cause__)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.assertNotIn(PRIVATE, repr(caught.exception))
                    self.assertEqual(self.inputs_snapshot(), before)
                    self.assertFalse((self.root / "kit" / "followups").exists())

    def test_cli_refusals_are_path_free(self):
        with mock.patch.object(law, "JURISDICTIONS", self.fx.base):
            for label, (module, attr) in self.faults().items():
                with self.subTest(label=label), mock.patch.object(module, attr, side_effect=oserror()):
                    code, out, err = run("kit", "fee-reply", "--directory", self.root, "--agency", A)
                    self.assertEqual((code, out), (1, ""))
                    self.assertIn(f"Stopped: {FIXED}", err)
                    for leak in (PRIVATE, PRIVATE_FILE, "Traceback", "Errno", "Nothing was sent"):
                        self.assertNotIn(leak, err)

    def test_existing_draft_untouched_and_recovery(self):
        result = self.fx.make()
        draft = self.root / result["written"]
        draft.write_text(draft.read_text(encoding="utf-8") + "MY EDIT\n", encoding="utf-8")
        for replace in (False, True):
            before = self.inputs_snapshot()
            with mock.patch.object(kit_track, "read_sent", side_effect=oserror()):
                with self.assertRaisesRegex(ValueError, FIXED):
                    self.fx.make(replace=replace)
            self.assertEqual(self.inputs_snapshot(), before)
        self.assertEqual(self.fx.make(replace=True)["written"], result["written"])  # recovers with no change to inputs

    def test_validation_errors_keep_their_messages_and_identity(self):
        self.fx.step("fee_paid", "2026-03-12")
        with self.assertRaisesRegex(ValueError, "not a fee estimate"):
            self.fx.make()
        self.fx.sent[A]["steps"].pop()
        self.fx.save()
        with self.assertRaisesRegex(ValueError, "No unambiguous fee rule"):
            kit_fee_reply.fee_reply(self.root, A, today=TODAY)  # shipped package has no exact-citation rule
        with self.assertRaisesRegex(ValueError, "not a selected agency"):
            kit_fee_reply.fee_reply(self.root, "ca-zzz-police", today=TODAY, law_base=self.fx.base)
        for sentinel in (ValueError("sentinel"), RuntimeError("sentinel"), RecursionError("sentinel"),
                         KeyboardInterrupt()):
            with self.subTest(sentinel=type(sentinel).__name__):
                with mock.patch.object(kit_track, "read_sent", side_effect=sentinel):
                    with self.assertRaises(type(sentinel)) as caught:
                        self.fx.make()
                self.assertIs(caught.exception, sentinel)

    def test_writer_and_notes_failures_remain_oserrors(self):
        good = self.fx.make()
        path = self.root / good["written"]
        before = path.read_bytes()
        for patched in ("os.replace", "os.fsync"):
            with mock.patch(f"campaign_tool.kit_fee_reply.{patched}", side_effect=OSError("disk")):
                with self.assertRaises(OSError) as caught:
                    self.fx.make(replace=True)
            self.assertNotEqual(str(caught.exception), FIXED)
            self.assertEqual(path.read_bytes(), before)
        with mock.patch.object(kit_fee_reply, "_write", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.fx.make(replace=True)
        with mock.patch.object(kit_fee_reply, "legal_notes", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.fx.make(replace=True)

    def test_healthy_output_unchanged(self):
        result = self.fx.make(narrow="just 2026")
        self.assertEqual(result["amount_cents"], 24550)
        self.assertEqual(stat.S_IMODE(os.stat(self.root / result["written"]).st_mode), 0o600)
        text = self.fx.letter(result)
        self.assertIn("Would limiting the scope", text)
        self.assertIn("$245.50", text)
        self.assertNotIn(FIXED, text)


if __name__ == "__main__":
    unittest.main()
