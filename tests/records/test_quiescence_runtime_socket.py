"""Audit checks live runtime via real host sockets, preserving path-mode evidence."""
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock
from campaign_tool.records import quiescence_audit as qa
from campaign_tool.records.runtime_fds import RuntimeSocketError
from tests.records import test_runtime_fds as transport
from tests.records.test_quiescence_audit import units_text


class SocketAudit(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.project = self.root / "project"
        self.project.mkdir()
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.units = self.root / "units"
        self.units.write_text(f"Captured={int(time.time())}\n\n" + units_text())
        self.out = self.root / "audit.json"
        patch = mock.patch.object(qa, "PROC_ROOT", str(self.proc))
        patch.start()
        self.addCleanup(patch.stop)

    def command(self, path):
        out = io.StringIO()
        with redirect_stdout(out):
            status = qa.main(["--project", str(self.project), "--runtime-socket", str(path),
                              "--units-file", str(self.units), "--out", str(self.out)])
        return status, out.getvalue()

    def test_audit_observes_live_lock_and_pending(self):
        lock = self.runtime / "run-records.lock"
        pending = self.runtime / "arrival.pending"
        clock = datetime(2026, 11, 15, tzinfo=timezone.utc)
        for state in ("free", "held", "absent"):
            for arrival in ("absent", "file", "symlink"):
                with self.subTest(state=state, arrival=arrival), transport.session(self.root) as (path, host):
                    # Mutations occur AFTER helper starts; no handoff-time snapshot can pass this.
                    lock.touch()
                    fd = os.open(lock, os.O_RDONLY)
                    try:
                        if state == "held":
                            fcntl.flock(fd, fcntl.LOCK_EX)
                        if state == "absent":
                            lock.unlink()
                        if arrival == "file":
                            pending.touch()
                        elif arrival == "symlink":
                            pending.symlink_to(self.root / "absent-target")
                        with mock.patch.object(qa, "datetime") as date:
                            date.now.return_value = clock
                            legacy = qa.audit(str(self.project), str(self.runtime), str(self.units))
                            status, text = self.command(path)
                        socket_doc = json.loads(self.out.read_bytes())
                        self.assertEqual(socket_doc.pop("runtime_source"), "socket")
                        self.assertEqual(socket_doc, legacy)
                        self.assertEqual(socket_doc["checks"]["run_lock"], state)
                        self.assertEqual(socket_doc["checks"]["arrival_pending"], arrival != "absent")
                        self.assertEqual(status, 1 if state == "held" or arrival != "absent" else 0)
                        self.assertIn("evidence_sha256=" + hashlib.sha256(self.out.read_bytes()).hexdigest(), text)
                    finally:
                        os.close(fd)
                        pending.unlink(missing_ok=True)

    def test_transport_refusal_preserves_out(self):
        self.out.write_bytes(b"unchanged existing result")
        before = self.out.read_bytes()
        self.assertEqual(self.command(self.root / "missing.sock"), (2, "RUNTIME_SOCKET_UNAVAILABLE\n"))
        with transport.session(self.root) as (path, host):
            os.chmod(path.parent, 0o750)
            self.assertEqual(self.command(path), (2, "RUNTIME_SOCKET_UNSAFE\n"))
        self.assertEqual(self.out.read_bytes(), before)

    def test_invalid_api_and_cli_sources(self):
        for runtime, source in ((None, None), (str(self.runtime), "socket")):
            with self.assertRaises(RuntimeSocketError) as caught:
                qa.audit(str(self.project), runtime, str(self.units), runtime_socket=source)
            self.assertEqual(caught.exception.code, "RUNTIME_SOCKET_UNSAFE")
        for sources in ([], ["--runtime", str(self.runtime), "--runtime-socket", "socket"]):
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    qa.main(["--project", str(self.project), "--units-file", str(self.units),
                             "--out", str(self.out), *sources])
            self.assertEqual(caught.exception.code, 2)
        self.assertFalse(self.out.exists())

    def test_pending_access_failure_never_means_absent(self):
        original = qa.os.open
        def denied(path, *args, **kwargs):
            if path == "arrival.pending":
                raise PermissionError("synthetic access failure")
            return original(path, *args, **kwargs)
        self.out.write_bytes(b"unchanged")
        with transport.session(self.root) as (path, host), mock.patch.object(qa.os, "open", denied):
            self.assertEqual(self.command(path), (2, "AUDIT_UNAVAILABLE\n"))
        self.assertEqual(self.out.read_bytes(), b"unchanged")

    def test_attended_documentation(self):
        text = (transport.REPO / "docs/RECORDS-HOLDS.md").read_text()
        for phrase in ("records_runtime_fds.py", "--runtime-socket {socket}", "quiescence-audit",
                       "RUNTIME_SOCKET_UNSAFE", "RUNTIME_SOCKET_UNAVAILABLE", "runtime_source", "new helper session"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
