"""Owner-approved failure observability repair; synthetic messages only."""
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from campaign_tool.records.intake import imap_intake as imap, mail_delta
from campaign_tool.records.ledger import store
from tests.records.test_imap_config_sources import TrackingIMAP


REVIEWED_MIME_FAILURE_CODES = (
    "invalid_mime_content_type",
    "invalid_related_container",
    "ambiguous_related_content_id",
    "invalid_related_content_id",
    "invalid_related_start",
    "missing_related_root",
    "related_root_type_mismatch",
    "invalid_multipart_disposition",
    "unsupported_attached_multipart_part",
    "invalid_mime_container",
    "invalid_leaf_disposition",
)


class FailureCodeTests(unittest.TestCase):
    def test_reviewed_mime_rejection_codes_are_preserved(self):
        # Literal producer contract, never derived from the consumer allowlist.
        self.assertEqual(len(REVIEWED_MIME_FAILURE_CODES), 11)
        for code in REVIEWED_MIME_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertEqual(imap._failure_code(mail_delta.Rejected(code)), code)

    def test_all_fixed_project_codes_are_preserved(self):
        for cls, codes in ((imap.IntakeError, imap._INTAKE_FAILURE_CODES),
                           (mail_delta.Rejected, imap._MAIL_DELTA_FAILURE_CODES)):
            for code in codes:
                with self.subTest(cls=cls.__name__, code=code):
                    self.assertLessEqual(len(code), 64)
                    self.assertEqual(imap._failure_code(cls(code)), code)

    def test_matching_text_from_untrusted_exception_types_is_not_reported(self):
        for cls in (ValueError, RuntimeError, OSError):
            for code in ("ambiguous_inline_body_part", "unsupported_rfc822_part", "export_scope_mismatch"):
                with self.subTest(cls=cls.__name__, code=code):
                    self.assertEqual(imap._failure_code(cls(code)), "fetch_or_preserve_failed")

    def test_unknown_long_multiple_and_nonstring_args_are_not_reported(self):
        for cls in (imap.IntakeError, mail_delta.Rejected):
            for args in ((), ("synthetic-private-marker",), ("x" * 100000,),
                         ("export_scope_mismatch", "synthetic-private-marker"), (None,), (["export_scope_mismatch"],)):
                with self.subTest(cls=cls.__name__, arg_count=len(args)):
                    self.assertEqual(imap._failure_code(cls(*args)), "fetch_or_preserve_failed")

    def test_subclasses_and_custom_str_are_never_invoked(self):
        class UntrustedRejected(mail_delta.Rejected):
            def __str__(self):
                raise AssertionError("exception formatter must never execute")
        class UntrustedIntake(imap.IntakeError):
            def __str__(self):
                raise AssertionError("exception formatter must never execute")
        class UntrustedArgument:
            def __str__(self):
                raise AssertionError("argument formatter must never execute")
        for error in (UntrustedRejected("export_scope_mismatch"),
                      UntrustedIntake("imap_fetch_failed"), mail_delta.Rejected(UntrustedArgument())):
            self.assertEqual(imap._failure_code(error), "fetch_or_preserve_failed")


class ExporterObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-imap-failure-codes-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.base.chmod(0o700)
        self.con = sqlite3.connect(":memory:")
        self.addCleanup(self.con.close)
        self.marker = "synthetic-private-marker"

    def run_message(self, raw, *, backend_error=None, fetch_error=None, expected_success=False):
        folders = {"INBOX": {"uidvalidity": 1, "messages": {1: raw}},
                   "Deferred": {"uidvalidity": 1, "messages": {2: raw}}}
        server = TrackingIMAP(folders)
        if fetch_error is not None:
            original_uid = server.uid
            def uid(command, *args):
                if command == "FETCH":
                    server.fetches.append((server.selected, int(args[0])))
                    raise fetch_error
                return original_uid(command, *args)
            server.uid = uid
        backend = SimpleNamespace(preserve=Mock(side_effect=backend_error))
        config = {"host": "imap.example.invalid", "port": 993, "ssl": True,
                  "username": "synthetic-user", "account_id": "synthetic-account",
                  "password": self.marker,  # pragma: allowlist secret - synthetic non-disclosure probe
                  "folders": ["INBOX", "Deferred"], "timeout": 1, "max_messages_per_run": 1}
        alerts = []
        @contextmanager
        def ledger(path):
            yield self.con
        intake = imap.IMAPIntake(config, ledger=self.base / "unused.sqlite", mail_root=self.base / "mail",
                                 backend=backend, alert=lambda key, **kw: alerts.append(key),
                                 client_factory=lambda cfg: server)
        with patch.object(store, "ledger", ledger):
            report = intake.run()
        self.assertTrue(server.logged_out)
        self.assertEqual(server.fetches, [("INBOX", 1)])
        self.assertEqual(report["attempted"], 1)
        self.assertEqual(report["deferred"], 1)
        self.assertTrue(report["limit_reached"])
        self.assertEqual(report["failures"], 0 if expected_success else 1)
        checkpoints = self.con.execute("SELECT folder,highest_uid FROM mail_checkpoints ORDER BY folder").fetchall()
        self.assertEqual(checkpoints, [("Deferred", 0), ("INBOX", 1 if expected_success else 0)])
        self.assertEqual(alerts, [] if expected_success else ["mail:preserve_failed:INBOX:1"])
        self.assertNotIn(self.marker, repr(report))
        if expected_success:
            backend.preserve.assert_called_once()
            self.assertEqual(report["preserved"], 1)
            self.assertIsNone(report["folders"][0]["failed"])
            return report
        return report["folders"][0]["failed"]

    def test_actual_exporter_ambiguous_inline_body_code_survives(self):
        message = EmailMessage()
        message.set_content("<p>synthetic body</p>", subtype="html")
        message.add_related(b"synthetic-image", maintype="image", subtype="png",
                            cid="<synthetic-inline>", filename="inline.png", disposition="inline")
        # A related branch outside the first mixed body slot is genuinely ambiguous.
        # Its HTML/image are 1.2.1/1.2.2, not the allowed first-slot body.
        outer = EmailMessage()
        outer.set_content("synthetic outer body")
        outer.make_mixed()
        outer.attach(message)
        self.assertEqual(self.run_message(outer.as_bytes()), "uid 1: ambiguous_inline_body_part")

    def test_actual_exporter_valid_related_body_succeeds_with_exact_locators(self):
        import hashlib

        message = EmailMessage()
        message.set_content("<p>synthetic body</p>", subtype="html")
        message.add_related(b"synthetic-image", maintype="image", subtype="png",
                            cid="<synthetic-inline>", filename="inline.png", disposition="inline")
        raw = message.as_bytes()
        self.run_message(raw, expected_success=True)
        wire = self.base / "synthetic-related.eml"
        wire.write_bytes(raw)
        wire.chmod(0o600)
        candidates = mail_delta.mime_candidates(wire)
        self.assertEqual([(part["locator"], part["content_type"], part["attachable"])
                          for part in candidates],
                         [("1.1", "text/html", False), ("1.2", "image/png", True)])
        self.assertEqual(candidates[1]["name"], "inline.png")
        self.assertEqual(candidates[1]["size"], len(b"synthetic-image"))
        self.assertEqual(candidates[1]["sha"], hashlib.sha256(b"synthetic-image").hexdigest())

    def test_backend_reviewed_mime_codes_survive_without_advancing_checkpoint(self):
        for code in REVIEWED_MIME_FAILURE_CODES:
            with self.subTest(code=code):
                self.assertEqual(self.run_message(b"Subject: synthetic\r\n\r\nbody",
                                                 backend_error=mail_delta.Rejected(code)),
                                 "uid 1: " + code)

    def test_actual_exporter_duplicate_leaf_disposition_code_survives(self):
        raw = (b'Content-Type: multipart/related; boundary="synthetic-related"\r\n'
               b'\r\n--synthetic-related\r\nContent-Type: text/html\r\n'
               b'Content-Disposition: inline\r\n'
               b'Content-Disposition: attachment; filename="synthetic-record.html"\r\n'
               b'\r\n<p>synthetic body</p>\r\n--synthetic-related--\r\n')
        self.assertEqual(self.run_message(raw), "uid 1: invalid_leaf_disposition")

    def test_actual_exporter_invalid_encapsulated_headers_code_survives(self):
        message = EmailMessage()
        message.set_content("synthetic outer")
        nested = EmailMessage()
        nested.set_content("synthetic nested")
        message.add_attachment(nested)
        self.assertEqual(self.run_message(message.as_bytes()), "uid 1: rfc822_wire_rejected")

    def test_backend_scope_rejection_code_survives(self):
        self.assertEqual(self.run_message(b"Subject: synthetic\r\n\r\nbody",
                                         backend_error=mail_delta.Rejected("export_scope_mismatch")),
                         "uid 1: export_scope_mismatch")

    def test_backend_resource_rejection_code_survives(self):
        self.assertEqual(self.run_message(b"Subject: synthetic\r\n\r\nbody",
                                         backend_error=mail_delta.Rejected("receipt_size_limit")),
                         "uid 1: receipt_size_limit")

    def test_unknown_project_code_is_redacted(self):
        self.assertEqual(self.run_message(b"Subject: synthetic\r\n\r\nbody",
                                         backend_error=mail_delta.Rejected(self.marker)),
                         "uid 1: fetch_or_preserve_failed")

    def test_untrusted_fetch_exception_is_redacted(self):
        self.assertEqual(self.run_message(b"Subject: synthetic\r\n\r\nbody",
                                         fetch_error=RuntimeError(self.marker)),
                         "uid 1: fetch_or_preserve_failed")

    def test_lookalike_backend_exception_does_not_gain_trust(self):
        self.assertEqual(self.run_message(b"Subject: synthetic\r\n\r\nbody",
                                         backend_error=ValueError("export_scope_mismatch")),
                         "uid 1: fetch_or_preserve_failed")


if __name__ == "__main__":
    unittest.main()
