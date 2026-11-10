"""Offline synthetic regressions for value-free historical-import dependency refusals."""
import contextlib
import json
import os
import stat
import subprocess
import sys
import unittest
from unittest import mock

from campaign_tool import kit, kit_import_sent, kit_track
from tests import test_kit_sent_import as base

FIXED = "kit/agencies.json unreadable: invalid agency data"
PRIVATE_ID = "ca-private-agency-canary"
PRIVATE_NAME = "PRIVATE-NAME-CANARY"


def snapshot(root):
    """Include directory metadata and external CSV, but not access times changed by reads."""
    return {str(p.relative_to(root)): (p.lstat().st_mode, p.lstat().st_mtime_ns,
                                      p.read_bytes() if p.is_file() and not p.is_symlink() else None,
                                      os.readlink(p) if p.is_symlink() else None)
            for p in [root, *sorted(root.rglob("*"))]}


class AgencyPrivacyTests(unittest.TestCase):
    setUp = base.KitSentImportTests.setUp
    csv = base.KitSentImportTests.csv
    do = base.KitSentImportTests.do
    api = base.KitSentImportTests.api
    ordinary_send = base.KitSentImportTests.ordinary_send
    stored = base.KitSentImportTests.stored

    def dependency(self, value):
        (self.kit / "agencies.json").write_bytes(value if isinstance(value, bytes)
                                                else json.dumps(value).encode("utf-8"))

    def check_refusal(self, path, message=FIXED):
        before = snapshot(self.tmp)
        for apply in (False, True):
            with self.subTest(apply=apply):
                with self.assertRaises(kit_import_sent.SentImportError) as caught:
                    self.api(path, apply=apply)
                exc = caught.exception
                self.assertEqual(str(exc), message)
                self.assertEqual(exc.exit_code, 2)
                self.assertIsNone(exc.__cause__)
                if message == FIXED:
                    self.assertTrue(exc.__suppress_context__)
                self.assertEqual(self.do(path, *(("--apply",) if apply else ())),
                                 (2, "", f"Stopped: {message}\n"))
                self.assertEqual(snapshot(self.tmp), before)

    def test_real_agency_shape_and_name_errors_never_echo_dependency_values(self):
        path = self.csv(f"{self.a},,2026-03-02,email,PRIVATE-CSV-REF,",
                        name="PRIVATE-ROOT-CANARY.csv")
        cases = {
            "object": {"PRIVATE-KEY-CANARY": PRIVATE_NAME},
            "scalar": PRIVATE_NAME, "null": None, "non-object": [PRIVATE_NAME],
            "missing-id": [{"name": PRIVATE_NAME}],
            "invalid-id": [{"agency_id": "PRIVATE-ID!", "name": PRIVATE_NAME}],
            "missing-name": [{"agency_id": PRIVATE_ID}],
            "wrong-name": [{"agency_id": PRIVATE_ID, "name": {"PRIVATE-KEY-CANARY": PRIVATE_NAME}}],
            "syntax": b'{"PRIVATE-KEY-CANARY":', "utf8": b'["\xff"]',
        }
        for label, value in cases.items():
            with self.subTest(label=label):
                self.dependency(value)
                self.check_refusal(path)

    def test_real_subprocess_preview_and_apply_refuse_without_disclosure(self):
        self.dependency([{"agency_id": PRIVATE_ID}])
        path = self.csv(f"{self.a},,2026-03-02,email,PRIVATE-CSV-REF,")
        before = snapshot(self.tmp)
        for apply in (False, True):
            args = [sys.executable, "-B", "-m", "campaign_tool", "kit-sent-import",
                    "--directory", str(self.root), "--csv", str(path)]
            if apply:
                args.append("--apply")
            result = subprocess.run(args, cwd=base.REPO_ROOT, capture_output=True, text=True, timeout=30)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (2, "", f"Stopped: {FIXED}\n"))
            self.assertEqual(snapshot(self.tmp), before)

    def test_injected_loader_value_error_is_fixed_and_suppresses_context(self):
        class LoaderError(ValueError):
            pass

        path = self.csv(f"{self.a},,2026-03-02,email,,")
        detail = f"{self.root}/PRIVATE-PATH {PRIVATE_ID} {PRIVATE_NAME}\nFix: INJECTED\r\x1b[31m"
        for kind in (ValueError, LoaderError):
            with self.subTest(kind=kind), mock.patch.object(kit, "_load_existing", side_effect=kind(detail)):
                self.check_refusal(path)
        for kind in (RuntimeError, AssertionError, RecursionError):
            for apply in (False, True):
                error = kind(detail)
                with self.subTest(kind=kind, apply=apply), \
                        mock.patch.object(kit, "_load_existing", side_effect=error):
                    with self.assertRaises(kind) as caught:
                        self.api(path, apply=apply)
                    self.assertIs(caught.exception, error)

    def test_agency_refusal_stops_before_all_downstream_work(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,{'a' * 16}")
        original = (self.kit / "agencies.json").read_bytes()
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    self.dependency(original)
                    self.assertEqual(self.ordinary_send(self.c)[0], 0)
                    self.api(self.csv(f"{self.b},,2026-03-02,portal,,", name="seed.csv"), apply=True)
                    self.assertEqual(len(kit_track.read_sent(self.root)), 2)
                self.dependency([{"agency_id": PRIVATE_ID}])
                with contextlib.ExitStack() as stack:
                    for module, name in ((kit_track, "read_sent"), (kit_import_sent, "_check_row"),
                                         (kit_import_sent, "_letter"), (kit_import_sent, "datetime"),
                                         (kit_import_sent.json, "dumps"), (kit, "_write_json")):
                        stack.enter_context(mock.patch.object(module, name, side_effect=AssertionError(name)))
                    self.check_refusal(path)
                self.assertEqual(self.sent.exists(), existing)

    def test_existing_file_and_io_refusals_keep_precedence(self):
        path = self.csv(f"{self.a},,2026-03-02,email,,")
        with mock.patch.object(kit, "_load_existing", side_effect=OSError(f"{self.root}/{PRIVATE_NAME}")):
            self.check_refusal(path, "kit/agencies.json cannot be read")
        agencies = self.kit / "agencies.json"
        agencies.unlink()
        self.check_refusal(path, "run kit first: kit/agencies.json is missing")
        target = self.tmp / "target.json"
        target.write_text("[]")
        agencies.symlink_to(target)
        self.check_refusal(path, "run kit first: kit/agencies.json is missing")
        agencies.unlink()
        self.dependency(None)
        moved = self.tmp / "moved-kit"
        self.kit.rename(moved)
        self.check_refusal(path, "run kit first: kit/ is missing or not a real folder")
        self.kit.symlink_to(moved)
        self.check_refusal(path, "run kit first: kit/ is missing or not a real folder")
        self.kit.unlink()
        moved.rename(self.kit)
        bad_csv = self.csv('"unclosed', name="malformed.csv")
        with mock.patch.object(kit, "_load_existing", side_effect=AssertionError("agency loader reached")):
            self.check_refusal(bad_csv, "csv line 2: malformed CSV (quotes or field size)")
        self.sent.write_text("{unreadable-store")
        self.check_refusal(path)

    def test_valid_agencies_and_row_failures_keep_exact_legacy_behavior(self):
        self.assertEqual(self.ordinary_send(self.c)[0], 0)
        old = kit_track.read_sent(self.root)
        self.dependency([{"agency_id": self.a, "name": "Synthétique", "selected": False},
                         {"agency_id": self.b, "name": "合成", "selected": True},
                         {"agency_id": self.c, "name": "Synthetic"}])
        source = (self.kit / "agencies.json").read_bytes()
        path = self.csv(f"{self.a},,2026-03-02,email,R-1,",
                        f"{self.a},named,2026-03-03,portal,,", f"{self.b},main,2026-03-04,mail,,")
        before = snapshot(self.tmp)
        self.assertEqual(self.do(path),
                         (0, json.dumps({"checked": 3, "imported": 0, "written": False}, indent=2) + "\n",
                          "Check only. Nothing was written or sent.\n"))
        self.assertEqual(snapshot(self.tmp), before)
        with mock.patch.object(kit, "_write_json", wraps=kit._write_json) as writer:
            code, out, err = self.do(path, "--apply")
        self.assertEqual((code, json.loads(out), err),
                         (0, {"checked": 3, "imported": 3, "written": True},
                          "Recorded only. Nothing was sent.\n"))
        writer.assert_called_once()
        self.assertEqual(stat.S_IMODE(self.sent.stat().st_mode), 0o600)
        requests = kit_track.read_sent(self.root)
        self.assertEqual(requests[self.c], old[self.c])
        for key, channel, day in ((self.a, "email", "2026-03-02"),
                                  (f"{self.a}#named", "portal", "2026-03-03"), (self.b, "mail", "2026-03-04")):
            expected = {"imported": True, "sent_on": day, "channel": channel, "steps": []}
            if key == self.a:
                expected["reference"] = "R-1"
            self.assertEqual({k: v for k, v in requests[key].items() if k != "recorded_at"}, expected)
        self.assertEqual((self.kit / "agencies.json").read_bytes(), source)
        # Structurally valid duplicate identities remain row errors, not loader corruption.
        self.dependency([{"agency_id": self.a, "name": "One"}, {"agency_id": self.a, "name": "Two"},
                         {"agency_id": self.b, "name": "Other"},
                         {"agency_id": f"{self.b}--named", "name": "Stem"}])
        cases = ((f"{self.a},fresh,2026-03-02,email,,", "agency_id: appears more than once in kit/agencies.json"),
                 ("ca-unknown-police,,2026-03-02,email,,", "agency_id: not in kit/agencies.json"),
                 (f"{self.b},named,2026-03-02,email,,", "request: its draft name is another agency's id; choose another request name"),
                 (f"{self.b},,2026-03-02,email,,", "request: already recorded for this agency"))
        for row, reason in cases:
            with self.subTest(reason=reason):
                self.check_refusal(self.csv(row),
                                   f"1 problem(s); kit/sent.json was not changed:\nline 2: {reason}")


if __name__ == "__main__":
    unittest.main()
