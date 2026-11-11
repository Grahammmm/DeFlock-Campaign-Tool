"""kit-sent-import: an unreadable existing kit/sent.json is one fixed, value-free refusal.
Offline, synthetic: invented agencies and request keys; no socket may be opened and nothing is sent."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit, kit_import_sent, kit_track  # noqa: E402
from tests.test_kit_sent_import import HEADER, TODAY, run, snapshot  # noqa: E402

FIXED = "kit/sent.json unreadable: invalid request data"
CANARY = "PRIVATE-STORED-KEY-CANARY"
STAMP = "2026-03-02T00:00:00+00:00"


def imported(follows=None):
    entry = {"sent_on": "2026-03-02", "channel": "email", "imported": True, "recorded_at": STAMP, "steps": []}
    if follows:
        entry["follows"] = follows
    return entry


class StorePrivacyTests(unittest.TestCase):
    def setUp(self):
        for target in ("socket.socket", "urllib.request.urlopen", "smtplib.SMTP"):
            patcher = mock.patch(target, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sent-store-privacy-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        for command in (("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo",
                         "--state", "CA"), ("kit", "--directory", self.root)):
            self.assertEqual(run(*command)[0], 0)
        self.sent = self.root / "kit" / "sent.json"
        self.a, self.b = [p.stem for p in sorted((self.root / "kit" / "requests").glob("*.md"))[:2]]
        self.csv = self.tmp / "sent.csv"
        self.csv.write_text(f"{HEADER}\n{self.a},,2026-03-02,email,,\n", encoding="utf-8")

    def store(self, requests):
        self.sent.write_text(json.dumps({"schema_version": 1, "requests": requests}), encoding="utf-8")

    def bad_stores(self):
        yield {f"{self.a}#{CANARY}\n\x07é": {"sent_on": "2026-03-02"}}
        yield {CANARY: imported()}
        yield {f"{self.b}#{CANARY}": imported("missing")}  # missing parent
        yield {f"{self.b}#{CANARY}-a": imported(f"{CANARY}-b".lower()),
               f"{self.b}#{CANARY}-b".lower(): imported(f"{CANARY}-a".lower())}  # cycle
        yield {f"{self.b}#self": imported("self")}

    def assert_refused_everywhere(self, secrets=(CANARY,)):
        before = snapshot(self.root)
        with self.assertRaises(ValueError):  # the shared reader itself refuses (and keeps its own text)
            kit_track.read_sent(self.root)
        for apply in (False, True):
            with self.assertRaises(kit_import_sent.SentImportError) as raised:
                kit_import_sent.import_sent(self.root, self.csv, apply=apply, today=TODAY)
            self.assertEqual(str(raised.exception), FIXED)
            self.assertEqual(raised.exception.exit_code, 2)
            self.assertTrue(raised.exception.__suppress_context__)
            extra = ("--apply",) if apply else ()
            code, out, err = run("kit-sent-import", "--directory", self.root, "--csv", self.csv, *extra)
            self.assertEqual((code, out, err), (2, "", "Stopped: " + FIXED + "\n"))
            for secret in secrets:
                self.assertNotIn(secret.lower(), (err + str(raised.exception)).lower())
        self.assertEqual(snapshot(self.root), before)

    def test_real_malformed_entry_and_chain_keys_are_private_in_preview_and_apply(self):
        for requests in self.bad_stores():
            with self.subTest(keys=len(requests)):
                self.store(requests)
                self.assert_refused_everywhere()

    def test_real_subprocess_store_refusal_is_exit_two_and_value_free(self):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        for requests in self.bad_stores():
            self.store(requests)
            before = snapshot(self.root)
            for extra in ((), ("--apply",)):
                done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "kit-sent-import", "--directory",
                                       str(self.root), "--csv", str(self.csv), *extra], cwd=REPO_ROOT, env=env,
                                      capture_output=True, text=True, timeout=120)
                self.assertEqual((done.returncode, done.stdout, done.stderr), (2, "", "Stopped: " + FIXED + "\n"))
            self.assertEqual(snapshot(self.root), before)

    def test_every_reader_value_error_uses_one_message(self):
        for text in ("Traceback /secret/path 'value'\nline2", ""):
            for error in (ValueError(text), UnicodeError(text), json.JSONDecodeError(text or "x", "d", 0)):
                with self.subTest(error=type(error).__name__), \
                        mock.patch.object(kit_track, "read_sent", side_effect=error):
                    with self.assertRaises(kit_import_sent.SentImportError) as raised:
                        kit_import_sent.import_sent(self.root, self.csv, apply=True, today=TODAY)
                    self.assertEqual(str(raised.exception), FIXED)
                    self.assertTrue(raised.exception.__suppress_context__)
        for content in (b"\xff\xfe", b"{not json", b'{"schema_version": 2, "requests": {}}'):
            self.sent.write_bytes(content)
            self.assert_refused_everywhere(secrets=())

    def test_locality_and_precedence(self):
        with mock.patch.object(kit_track, "read_sent", side_effect=OSError("/secret/path detail")):
            with self.assertRaises(kit_import_sent.SentImportError) as raised:
                kit_import_sent.import_sent(self.root, self.csv, today=TODAY)
        self.assertEqual(str(raised.exception), "kit/sent.json cannot be read")
        for error in (RuntimeError("x"), RecursionError("x"), AssertionError("x")):
            with mock.patch.object(kit_track, "read_sent", side_effect=error), self.assertRaises(type(error)):
                kit_import_sent.import_sent(self.root, self.csv, today=TODAY)
        # a bad row never outranks the store; downstream work never starts
        self.csv.write_text(f"{HEADER}\n{self.a},,not-a-date,email,,\n", encoding="utf-8")
        self.store({CANARY: imported()})
        with mock.patch.object(kit_import_sent, "_check_row", side_effect=AssertionError("row checked")), \
                mock.patch.object(kit, "_write_json", side_effect=AssertionError("written")):
            for apply in (False, True):
                with self.assertRaises(kit_import_sent.SentImportError) as raised:
                    kit_import_sent.import_sent(self.root, self.csv, apply=apply, today=TODAY)
                self.assertEqual(str(raised.exception), FIXED)

    def test_valid_and_absent_store_still_import(self):
        self.assertEqual(kit_import_sent.import_sent(self.root, self.csv, today=TODAY),
                         {"checked": 1, "imported": 0, "written": False})
        self.store({f"{self.b}#named": imported(), f"{self.b}#second": imported("named")})
        self.assertEqual(kit_import_sent.import_sent(self.root, self.csv, apply=True, today=TODAY),
                         {"checked": 1, "imported": 1, "written": True})
        stored = kit_track.read_sent(self.root)
        self.assertEqual(set(stored), {self.a, f"{self.b}#named", f"{self.b}#second"})
        self.assertEqual(self.sent.stat().st_mode & 0o777, 0o600)

    def test_prospective_cap_message_is_unchanged(self):
        with mock.patch.object(kit_track, "MAX_BYTES", 100):
            with self.assertRaises(kit_import_sent.SentImportError) as raised:
                kit_import_sent.import_sent(self.root, self.csv, apply=True, today=TODAY)
        self.assertIn("larger than 1 MiB", str(raised.exception))
        self.assertFalse(self.sent.exists())


if __name__ == "__main__":
    unittest.main()
