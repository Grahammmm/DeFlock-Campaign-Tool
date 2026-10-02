"""Synthetic owner-only JSON credential sources and global IMAP fetch budgets."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import traceback
import unittest
from unittest.mock import patch

from campaign_tool.records.intake import imap_intake as imap
from tests.records.test_imap_intake import FakeIMAP, IMAPIntakeTests
from tests.records.test_run_slice import synthetic_email


class CredentialSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="synthetic-imap-source-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.config_path = self.base / "mail.json"
        self.source = self.base / "exporter.json"
        self.marker = "synthetic-credential-marker"
        self.settings = {"host": "imap.example.invalid", "username": "synthetic-user",
                         "account_id": "synthetic-account",
                         "password_source": {"type": "json", "path": "exporter.json", "key": "MAIL_PASSWORD"}}
        self.write_source({"MAIL_PASSWORD": self.marker, "UNRELATED_VALUE": "synthetic-unused"})

    def write_source(self, value):
        self.source.write_text(json.dumps(value))
        self.source.chmod(0o600)

    def load(self):
        self.config_path.write_text(json.dumps(self.settings))
        self.config_path.chmod(0o600)
        return imap.load_config(self.config_path)

    def reject(self, code):
        with self.assertRaisesRegex(imap.IntakeError, code) as caught:
            self.load()
        rendered = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn(self.marker, rendered)
        return caught.exception

    def test_relative_and_absolute_source_and_exact_password_bytes(self):
        self.assertEqual(self.load()["password"], self.marker)
        self.settings["password_source"]["path"] = str(self.source)
        self.write_source({"MAIL_PASSWORD": "  " + self.marker + "  "})
        self.assertEqual(self.load()["password"], "  " + self.marker + "  ")

    def test_redaction_and_fingerprint_ignore_all_credential_fields(self):
        config = self.load()
        first = imap.fingerprint(config)
        config.update(password="synthetic-rotated", password_source={"password": self.marker},  # pragma: allowlist secret - owner-reviewed synthetic fixture
                      password_file=self.marker, password_env=self.marker, unexpected={"secret": self.marker})
        self.assertEqual(first, imap.fingerprint(config))
        self.assertNotIn(self.marker, json.dumps(imap.redacted(config)))
        self.assertNotIn("synthetic-rotated", json.dumps(imap.redacted(config)))
        self.assertNotIn("password_source", imap.redacted(config))
        self.assertEqual(len(first), 64)

    def test_owner_only_modes(self):
        for mode in (0o604, 0o640, 0o660, 0o644, 0o777):
            with self.subTest(mode=mode):
                self.source.chmod(mode)
                self.reject("owner_only")
        self.source.chmod(0o400)
        self.assertEqual(self.load()["password"], self.marker)

    def test_wrong_uid_rejected_before_read(self):
        real_fstat = os.fstat
        def foreign_owner(fd):
            info = real_fstat(fd)
            if info.st_ino == self.source.stat().st_ino:
                values = {k: getattr(info, k) for k in (
                    "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                    "st_size", "st_mtime_ns", "st_ctime_ns")}
                values["st_uid"] += 1
                return SimpleNamespace(**values)
            return info
        with patch.object(imap.os, "fstat", side_effect=foreign_owner):
            self.reject("owner_only")

    def test_symlink_directory_fifo_and_missing_source_rejected(self):
        link = self.base / "link.json"
        link.symlink_to(self.source)
        fifo = self.base / "fifo.json"
        os.mkfifo(fifo, 0o600)
        for path in (link, self.base, fifo, self.base / "missing.json"):
            with self.subTest(kind=path.name):
                self.settings["password_source"]["path"] = str(path)
                self.reject("mail_password_source_")

    def test_secure_open_flags_are_used(self):
        real_open = os.open
        flags_seen = []
        def observe(path, flags, *args, **kwargs):
            flags_seen.append(flags)
            return real_open(path, flags, *args, **kwargs)
        with patch.object(imap.os, "open", side_effect=observe):
            self.load()
        self.assertEqual(len(flags_seen), 2)
        for flags in flags_seen:
            self.assertTrue(flags & os.O_NOFOLLOW)
            self.assertTrue(flags & os.O_NONBLOCK)

    def test_changed_descriptor_metadata_rejected(self):
        real_fstat = os.fstat
        for field in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_uid", "st_ino"):
            calls = []
            def changed(fd):
                info = real_fstat(fd)
                if info.st_ino != self.source.stat().st_ino:
                    return info
                calls.append(fd)
                if len(calls) == 2:
                    values = {k: getattr(info, k) for k in (
                        "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                        "st_size", "st_mtime_ns", "st_ctime_ns")}
                    values[field] += 1
                    return SimpleNamespace(**values)
                return info
            with self.subTest(field=field), patch.object(imap.os, "fstat", side_effect=changed):
                self.reject("changed_during_read")

    def test_byte_limit_before_and_during_read(self):
        self.source.write_bytes(b"x" * (imap.MAX_CREDENTIAL_BYTES + 1))
        self.reject("size_limit")
        # A deceptive initial size must not bypass the bounded read.
        real_fstat = os.fstat
        calls = []
        def small_initial(fd):
            info = real_fstat(fd)
            if info.st_ino != self.source.stat().st_ino:
                return info
            calls.append(fd)
            values = {k: getattr(info, k) for k in (
                "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink",
                "st_size", "st_mtime_ns", "st_ctime_ns")}
            values["st_size"] = 1
            return SimpleNamespace(**values)
        with patch.object(imap.os, "fstat", side_effect=small_initial):
            self.reject("size_limit")

    def test_malformed_duplicate_and_non_object_json_do_not_leak(self):
        samples = [
            ('{"MAIL_PASSWORD":"' + self.marker + '",broken}').encode(),
            ('{"MAIL_PASSWORD":"' + self.marker + '","MAIL_PASSWORD":"other"}').encode(),  # pragma: allowlist secret - owner-reviewed synthetic fixture
            b'{"MAIL_PASSWORD":"x","other":{"duplicate":1,"duplicate":2}}',
            b'{"MAIL_PASSWORD":"x","other":NaN}', b"\xff", b"[1,2]", b"null",
            b"[" * 1500 + b"]" * 1500,
        ]
        for raw in samples:
            with self.subTest(kind=len(raw)):
                self.source.write_bytes(raw)
                self.reject("invalid_json|must_be_object")

    def test_literal_key_and_nonempty_string_required(self):
        self.write_source({"nested": {"password": self.marker}, "nested.password": self.marker})
        self.settings["password_source"]["key"] = "nested.password"
        self.assertEqual(self.load()["password"], self.marker)
        for value in (None, "", "  ", False, 123, {}, [], "bad\0value"):
            with self.subTest(value=value):
                self.write_source({"MAIL_PASSWORD": value})
                self.settings["password_source"]["key"] = "MAIL_PASSWORD"
                self.reject("key_invalid")
        self.write_source({"OTHER": self.marker})
        self.reject("key_invalid")

    def test_invalid_descriptors_and_ambiguity(self):
        for descriptor in (None, [], {}, {"type": "env", "path": "exporter.json", "key": "MAIL_PASSWORD"},
                           {"type": "json", "path": 1, "key": "MAIL_PASSWORD"},
                           {"type": "json", "path": "", "key": "MAIL_PASSWORD"},
                           {"type": "json", "path": "exporter.json", "key": ""},
                           {"type": "json", "path": "exporter.json", "key": "MAIL_PASSWORD", "extra": self.marker}):
            with self.subTest(descriptor=descriptor):
                self.settings["password_source"] = descriptor
                self.reject("source_invalid")
        self.settings["password_source"] = {"type": "json", "path": "exporter.json", "key": "MAIL_PASSWORD"}
        for key in ("password", "password_file", "password_env"):
            self.settings[key] = ""
            self.reject("source_ambiguous")
            del self.settings[key]

    def test_legacy_inline_file_env_and_precedence(self):
        del self.settings["password_source"]
        self.settings["password"] = self.marker
        self.settings["password_file"] = "missing"  # pragma: allowlist secret - owner-reviewed synthetic fixture
        self.settings["password_env"] = "SYNTHETIC_IMAP_PASSWORD"  # pragma: allowlist secret - owner-reviewed synthetic fixture
        self.assertEqual(self.load()["password"], self.marker)
        del self.settings["password"]
        plain = self.base / "plain.txt"
        plain.write_text("synthetic-file-value\n")
        plain.chmod(0o600)
        self.settings["password_file"] = "plain.txt"  # pragma: allowlist secret - owner-reviewed synthetic fixture
        with patch.dict(os.environ, {"SYNTHETIC_IMAP_PASSWORD": "synthetic-env-value"}):  # pragma: allowlist secret - owner-reviewed synthetic fixture
            self.assertEqual(self.load()["password"], "synthetic-file-value")
            del self.settings["password_file"]
            self.assertEqual(self.load()["password"], "synthetic-env-value")
        with patch.dict(os.environ, {}, clear=True):
            self.reject("password_unavailable")
        self.assertNotIn(self.marker, json.dumps(imap.redacted(self.settings)))

    def test_config_json_and_permissions_fail_safely(self):
        self.load()
        self.config_path.write_text('{"password":"' + self.marker + '",bad}')
        with self.assertRaisesRegex(imap.IntakeError, "mail_config_invalid_json") as caught:
            imap.load_config(self.config_path)
        self.assertNotIn(self.marker, "".join(traceback.format_exception(caught.exception)))
        self.load()
        self.config_path.chmod(0o644)
        with self.assertRaisesRegex(imap.IntakeError, "owner_only"):
            imap.load_config(self.config_path)

    def test_default_and_valid_message_limits(self):
        self.assertEqual(self.load()["max_messages_per_run"], 200)
        for limit in (1, 2, 200):
            self.settings["max_messages_per_run"] = limit
            self.assertEqual(self.load()["max_messages_per_run"], limit)

    def test_invalid_message_limits(self):
        for limit in (True, False, 0, -1, 201, 500, 1.0, "2", None, [], {}):
            with self.subTest(limit=limit):
                self.settings["max_messages_per_run"] = limit
                self.reject("max_messages_per_run_invalid")


class TrackingIMAP(FakeIMAP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fetches = []
        self.searches = []

    def uid(self, command, *args):
        if command == "FETCH":
            self.fetches.append((self.selected, int(args[0])))
            assert args[1] == "(BODY.PEEK[])"
        if command == "SEARCH":
            self.searches.append(self.selected)
        return super().uid(command, *args)


class GlobalBudgetTests(unittest.TestCase):
    # Reuse synthetic setup/helpers, not the original test methods.
    setUp = IMAPIntakeTests.setUp
    write_config = IMAPIntakeTests.write_config
    ledger = IMAPIntakeTests.ledger
    checkpoints = IMAPIntakeTests.checkpoints
    run_with = IMAPIntakeTests.run_with

    def configure(self, limit=2, folders=None, source=False):
        config = {"host": "imap.example.invalid", "username": "synthetic-user",
                  "account_id": "synthetic-account", "folders": folders or ["INBOX", "Agencies"],
                  "max_messages_per_run": limit}
        if source:
            path = self.base / "source.json"
            path.write_text(json.dumps({"MAIL_PASSWORD": "synthetic-source-marker"}))  # pragma: allowlist secret - owner-reviewed synthetic fixture
            path.chmod(0o600)
            config["password_source"] = {"type": "json", "path": "source.json", "key": "MAIL_PASSWORD"}
        else:
            config["password"] = "synthetic-only"  # pragma: allowlist secret - owner-reviewed synthetic fixture
        self.write_config(config)

    def messages(self, *uids):
        return {uid: synthetic_email(message_id=f"<synthetic-{uid}@example.invalid>") for uid in uids}

    def folder(self, *uids, validity=1):
        return {"uidvalidity": validity, "messages": self.messages(*uids)}

    def test_limit_across_folders_with_source_and_checkpoint_edge(self):
        self.configure(source=True)
        server = TrackingIMAP({"INBOX": self.folder(1), "Agencies": self.folder(5, 6)})
        report = self.run_with(server)["mailbox"]
        self.assertEqual(server.fetches, [("INBOX", 1), ("Agencies", 5)])
        self.assertEqual(report["attempted"], 2)
        self.assertEqual(report["deferred"], 1)
        self.assertTrue(report["limit_reached"])
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 1), "Agencies": (1, 5)})
        again = TrackingIMAP(server.folders)
        report = self.run_with(again)["mailbox"]
        self.assertEqual(again.fetches, [("Agencies", 6)])
        self.assertEqual(self.checkpoints()["Agencies"], (1, 6))
        self.assertFalse(report["limit_reached"])
        for path in (self.root / "runs").iterdir():
            self.assertNotIn("synthetic-source-marker", path.read_text())
        for row in self.ledger("SELECT summary FROM runs"):
            self.assertNotIn("synthetic-source-marker", row[0])

    def test_budget_does_not_hide_later_folders_or_advance_deferred_checkpoints(self):
        self.configure(folders=["INBOX", "Agencies", "Empty", "Missing"])
        server = TrackingIMAP({"INBOX": self.folder(1, 2, 3), "Agencies": self.folder(5, 6),
                               "Empty": self.folder(), "Unconfigured": self.folder(9)})
        report = self.run_with(server)["mailbox"]
        self.assertEqual(server.fetches, [("INBOX", 1), ("INBOX", 2)])
        self.assertEqual(server.searches, ["INBOX", "Agencies", "Empty"])
        self.assertEqual(report["deferred"], 3)
        self.assertEqual(report["unconfigured_folders"], ["Unconfigured"])
        entries = {f["folder"]: f for f in report["folders"]}
        self.assertEqual(entries["Agencies"]["attempted"], 0)
        self.assertEqual(entries["Agencies"]["deferred"], 2)
        self.assertTrue(entries["Agencies"]["limit_reached"])
        self.assertEqual(entries["Missing"]["failed"], "folder_missing")
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 2), "Agencies": (1, 0), "Empty": (1, 0)})

    def test_failure_uses_budget_and_retry_does_not_skip_failed_uid(self):
        self.configure()
        folders = {"INBOX": self.folder(1, 2, 3), "Agencies": self.folder(5)}
        first = TrackingIMAP(folders, fail_on={("INBOX", 2)})
        report = self.run_with(first)["mailbox"]
        self.assertEqual(first.fetches, [("INBOX", 1), ("INBOX", 2)])
        self.assertEqual(report["failures"], 1)
        self.assertEqual(report["attempted"], 2)
        self.assertEqual(report["deferred"], 2)
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 1), "Agencies": (1, 0)})
        retry = TrackingIMAP(folders)
        self.run_with(retry)
        self.assertEqual(retry.fetches, [("INBOX", 2), ("INBOX", 3)])
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 3), "Agencies": (1, 0)})

    def test_failure_in_first_folder_leaves_remaining_budget_for_other_folder(self):
        self.configure()
        server = TrackingIMAP({"INBOX": self.folder(1, 2), "Agencies": self.folder(5, 6)},
                              fail_on={("INBOX", 1)})
        report = self.run_with(server)["mailbox"]
        self.assertEqual(server.fetches, [("INBOX", 1), ("Agencies", 5)])
        self.assertEqual(report["attempted"], 2)
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 0), "Agencies": (1, 5)})

    def test_uidvalidity_replay_consumes_budget_and_leaves_deferred_uid(self):
        self.configure()
        folders = {"INBOX": self.folder(1, 2, 3), "Agencies": self.folder(5)}
        self.run_with(TrackingIMAP(folders))
        count = self.ledger("SELECT count(*) FROM originals")[0][0]
        folders["INBOX"]["uidvalidity"] = 2
        reset = TrackingIMAP(folders)
        report = self.run_with(reset)["mailbox"]
        self.assertEqual(reset.fetches, [("INBOX", 1), ("INBOX", 2)])
        self.assertEqual(report["attempted"], 2)
        self.assertTrue(report["folders"][0]["uidvalidity_reset"])
        self.assertEqual(self.checkpoints(), {"INBOX": (2, 2), "Agencies": (1, 0)})
        self.assertEqual(self.ledger("SELECT count(*) FROM originals")[0][0], count)

    def test_default_old_config_limits_two_large_folders_to_200(self):
        # Original setup config has no max_messages_per_run.
        server = TrackingIMAP({"INBOX": self.folder(*range(1, 202)), "Agencies": self.folder(301, 302)})
        report = self.run_with(server)["mailbox"]
        self.assertEqual(len(server.fetches), 200)
        self.assertEqual(report["max_messages_per_run"], 200)
        self.assertEqual(report["attempted"], 200)
        self.assertEqual(report["deferred"], 3)
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 200), "Agencies": (1, 0)})

    def test_fetch_errors_never_echo_credentials_into_reports(self):
        self.configure(limit=1, source=True)
        class LeakyServer(TrackingIMAP):
            def uid(self, command, *args):
                if command == "FETCH":
                    self.fetches.append((self.selected, int(args[0])))
                    raise RuntimeError("synthetic-source-marker")
                return super().uid(command, *args)
        server = LeakyServer({"INBOX": self.folder(1), "Agencies": self.folder(5)})
        report = self.run_with(server)
        self.assertNotIn("synthetic-source-marker", json.dumps(report))
        self.assertEqual(len(server.fetches), 1)
        self.assertEqual(report["mailbox"]["deferred"], 1)
        self.assertEqual(self.checkpoints(), {"INBOX": (1, 0), "Agencies": (1, 0)})

    def test_login_error_never_echoes_credentials(self):
        self.configure(source=True)
        config = imap.load_config(self.config_path)
        def fail(config):
            raise RuntimeError(config["password"])
        intake = imap.IMAPIntake(config, ledger=self.root / "ledger.sqlite", mail_root=self.root / "mail",
                                 backend=None, alert=lambda *a, **k: None, client_factory=fail)
        with self.assertRaisesRegex(imap.IntakeError, "connection_or_login_failed") as caught:
            intake.run()
        self.assertNotIn("synthetic-source-marker", "".join(traceback.format_exception(caught.exception)))


if __name__ == "__main__":
    unittest.main()
