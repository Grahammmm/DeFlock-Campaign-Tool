"""Synthetic IMAP transport policy and loopback TLS credential-disclosure controls."""
import json
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from campaign_tool.records.intake import imap_intake as imap


def settings(**changes):
    return {"host": "imap.example.invalid", "port": 993, "timeout": 3,
            "username": "synthetic-user", "password": "synthetic",  # pragma: allowlist secret - synthetic fixture
            "account_id": "synthetic-account", **changes}


class TransportPolicyTests(unittest.TestCase):
    def test_fresh_process_import_and_default_are_offline(self):
        probe = Path(__file__).with_name("intake_import_probe.py")
        result = subprocess.run([sys.executable, "-B", str(probe)], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("zero network attempts", result.stdout)

    def load(self, **changes):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.json"
            path.write_text(json.dumps(settings(**changes)))
            path.chmod(0o600)
            return imap.load_config(path)

    def test_tls_default_verifies_certificates_and_hostname(self):
        with mock.patch.object(imap.imaplib, "IMAP4_SSL") as tls, \
                mock.patch.object(imap.imaplib, "IMAP4") as plain:
            imap.default_client_factory(settings())
        context = tls.call_args.kwargs["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        plain.assert_not_called()
        tls.return_value.login.assert_called_once_with("synthetic-user", "synthetic")

    def test_plaintext_requires_literal_explicit_override_before_connection(self):
        for override in (None, False, "true", 1, [], {}):
            with self.subTest(override=override), \
                    mock.patch.object(imap.imaplib, "IMAP4") as plain, \
                    mock.patch.object(imap.imaplib, "IMAP4_SSL") as tls:
                config = settings(ssl=False)
                if override is not None:
                    config["allow_insecure_plaintext"] = override
                with self.assertRaises(imap.IntakeError):
                    imap.default_client_factory(config)
                with self.assertRaises(imap.IntakeError):
                    self.load(**config)
                plain.assert_not_called()
                tls.assert_not_called()

    def test_plaintext_override_is_preserved_and_observable(self):
        config = self.load(ssl=False, allow_insecure_plaintext=True)
        self.assertIs(config["allow_insecure_plaintext"], True)
        self.assertIs(imap.redacted(config)["allow_insecure_plaintext"], True)
        with mock.patch.object(imap.imaplib, "IMAP4") as plain, \
                mock.patch.object(imap.imaplib, "IMAP4_SSL") as tls:
            imap.default_client_factory(config)
        plain.assert_called_once_with(config["host"], config["port"], timeout=config["timeout"])
        plain.return_value.login.assert_called_once()
        tls.assert_not_called()

    def test_tls_override_never_disables_verification(self):
        with mock.patch.object(imap.imaplib, "IMAP4_SSL") as tls:
            imap.default_client_factory(settings(ssl=True, allow_insecure_plaintext=True))
        self.assertEqual(tls.call_args.kwargs["ssl_context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(tls.call_args.kwargs["ssl_context"].check_hostname)

    def test_malformed_tls_flags_are_rejected_at_both_entry_points(self):
        for value in (None, 0, 1, "false", "true", [], {}):
            with self.subTest(value=value), \
                    mock.patch.object(imap.imaplib, "IMAP4_SSL") as tls, \
                    mock.patch.object(imap.imaplib, "IMAP4") as plain:
                with self.assertRaisesRegex(imap.IntakeError, "mail_config_ssl_invalid"):
                    self.load(ssl=value)
                with self.assertRaisesRegex(imap.IntakeError, "mail_config_ssl_invalid"):
                    imap.default_client_factory(settings(ssl=value))
                tls.assert_not_called()
                plain.assert_not_called()

    def test_context_failure_never_falls_back_or_logs_in(self):
        with mock.patch.object(imap.ssl, "create_default_context", side_effect=ssl.SSLError("synthetic")), \
                mock.patch.object(imap.imaplib, "IMAP4_SSL") as tls, \
                mock.patch.object(imap.imaplib, "IMAP4") as plain:
            with self.assertRaises(ssl.SSLError):
                imap.default_client_factory(settings(ssl=True))
        tls.assert_not_called()
        plain.assert_not_called()

    def test_invalid_transport_rejected_before_password_source_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.json"
            config = settings(ssl=False)
            config.pop("password")
            config["password_source"] = {"type": "json", "path": "absent.json", "key": "TEST_VALUE"}
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            with mock.patch.object(imap, "_json_password") as password_source:
                with self.assertRaisesRegex(imap.IntakeError, "mail_config_plaintext_refused"):
                    imap.load_config(path)
            password_source.assert_not_called()


class LoopbackTLSVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise RuntimeError("openssl is required for the TLS regression controls")
        cls.temp = tempfile.TemporaryDirectory(prefix="synthetic-imap-cert-")
        cls.base = Path(cls.temp.name).resolve()
        cls.cert, cls.key = cls.base / "cert.pem", cls.base / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(cls.key), "-out", str(cls.cert), "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def connect(self, context=None, host="127.0.0.1"):
        received, errors = [], []
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.cert, self.key)
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                raw, _ = listener.accept()
                with raw:
                    raw.settimeout(5)
                    with server_context.wrap_socket(raw, server_side=True) as conn:
                        conn.sendall(b"* OK synthetic loopback IMAP\r\n")
                        with conn.makefile("rb") as source:
                            for line in source:
                                tag, command, *_ = line.split()
                                if command.upper() == b"CAPABILITY":
                                    conn.sendall(b"* CAPABILITY IMAP4rev1\r\n" + tag + b" OK done\r\n")
                                elif command.upper() == b"LOGIN":
                                    received.append(line)
                                    conn.sendall(tag + b" OK login\r\n")
                                else:
                                    raise AssertionError("unexpected loopback command")
            except ssl.SSLError:
                pass  # A verifying client rejecting this certificate is expected.
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        error = None
        try:
            config = settings(host=host, port=listener.getsockname()[1], ssl=True)
            if context is None:
                client = imap.default_client_factory(config)
            else:
                with mock.patch.object(imap.ssl, "create_default_context", return_value=context):
                    client = imap.default_client_factory(config)
            client.shutdown()
        except ssl.SSLCertVerificationError as caught:
            error = caught
        finally:
            thread.join(6)
        self.assertFalse(thread.is_alive(), "loopback TLS server did not terminate")
        self.assertEqual(errors, [])
        return error, received

    def test_untrusted_certificate_rejected_before_login(self):
        error, received = self.connect()
        self.assertIsInstance(error, ssl.SSLCertVerificationError)
        self.assertEqual(received, [])

    def test_trusted_certificate_wrong_hostname_rejected_before_login(self):
        context = ssl.create_default_context(cafile=str(self.cert))
        error, received = self.connect(context)
        self.assertIsInstance(error, ssl.SSLCertVerificationError)
        self.assertIn("mismatch", str(error).lower())
        self.assertEqual(received, [])

    def test_trusted_matching_certificate_positive_control(self):
        context = ssl.create_default_context(cafile=str(self.cert))
        error, received = self.connect(context, host="localhost")
        self.assertIsNone(error)
        self.assertEqual(len(received), 1)
        self.assertIn(b"synthetic-user", received[0])

    def test_broken_unverified_context_negative_control_exposes_login(self):
        error, received = self.connect(ssl._create_unverified_context())
        self.assertIsNone(error)
        self.assertEqual(len(received), 1)
        self.assertTrue(received[0].endswith(b' "synthetic"\r\n'))
