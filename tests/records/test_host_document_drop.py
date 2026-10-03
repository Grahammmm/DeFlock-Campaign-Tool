"""Fixed host document selector and genuine inner CLI command contracts."""
import json
import os
from pathlib import Path
from unittest.mock import patch
from campaign_tool.records import container_host as host
import unittest
from tests.records import test_document_drop as fixtures


class HostDocumentDropTests(unittest.TestCase):
    setUp = fixtures.DocumentDropTests.setUp
    add = fixtures.DocumentDropTests.add
    # Reuse only fixture setup/helpers; the document tests remain in their own class.
    def test_fixed_arguments_no_record_callback(self):
        value = {"input_mode": "documents", "documents": "/synthetic/documents",
                 "document_manifest_sha256": "a" * 64}
        self.assertEqual(host.input_arguments(value), ["--input-mode", "documents", "--documents",
                                                      "/synthetic/documents", "--document-manifest-sha256", "a" * 64])
        value["mail_config"] = "/synthetic/private.json"
        with self.assertRaises(host.Rejected):
            host.input_selection(value)

    def test_all_ambiguous_selectors_fail(self):
        for extra in ("mail_config", "inbox"):
            with self.subTest(extra=extra):
                with self.assertRaises(host.Rejected):
                    host.input_selection({"input_mode": "documents", "documents": "/drop",
                                          "document_manifest_sha256": "a" * 64, extra: "/other"})
        with self.assertRaises(host.Rejected):
            host.input_selection({"input_mode": "documents", "documents": "/drop"})
        with self.assertRaises(host.Rejected):
            host.input_selection({"input_mode": "documents", "document_manifest_sha256": "a" * 64})

    def test_legacy_mailbox_unchanged(self):
        self.assertEqual(host.input_arguments({"mail_config": "/private/mail.json"}),
                         ["--mail-config", "/private/mail.json"])

    def test_document_manifest_command_redacts_paths(self):
        self.add()
        from contextlib import redirect_stdout
        import io
        output = io.StringIO()
        with redirect_stdout(output):
            code = host.main(["document-manifest", "--documents", str(self.input)])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(set(result), {"schema", "status", "sha256", "documents", "bytes"})
        self.assertNotIn(str(self.input), output.getvalue())

    def test_exact_ro_mount_not_ancestor(self):
        value = {"input_mode": "documents", "documents": "/drop", "document_manifest_sha256": "a" * 64,
                 "mounts": [{"kind": "bind", "source": "/synthetic/source", "target": "/", "readonly": True}]}
        with self.assertRaises(host.Rejected):
            host.inbox_mount(value)

    def test_actual_mount_and_no_alias(self):
        selected = {"kind": "bind", "source": "/synthetic/input", "target": "/drop", "readonly": True}
        value = {"input_mode": "documents", "documents": "/drop",
                 "document_manifest_sha256": "a" * 64, "mounts": [selected]}
        runtime = {"mounts": [{"Type": "bind", "Source": selected["source"], "Destination": "/drop", "RW": False}],
                   "configured_mounts": [{"Type": "bind", "Source": selected["source"], "Target": "/drop", "ReadOnly": True}]}
        host.check_inbox_mounts(value, runtime)
        runtime["mounts"][0]["RW"] = True
        with self.assertRaises(host.Rejected):
            host.check_inbox_mounts(value, runtime)

    def test_entry_forwards_genuine_documents_and_manifest(self):
        identity = {"schema": 1, "job_id": "a" * 32, "role": "worker"}
        paths = {"role": "worker", "job_id": identity["job_id"], "wall_seconds": 60,
                 "worker_state_parent": "/synthetic/gates", "input_mode": "documents",
                 "documents": "/synthetic/documents", "document_manifest_sha256": "b" * 64,
                 "python": "/usr/bin/python3", "root": "/synthetic/records"}
        class Gate:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def child(self, *args, **kwargs): return self
            def write(self, *args, **kwargs): pass
            def read(self, *args, **kwargs): return host.encoded(identity), None
        with patch.object(host.os, "getuid", return_value=1000), patch.object(host.os, "geteuid", return_value=1000), \
                patch.object(host, "PrivateDir", Gate), patch.object(host, "offline_snapshot") as snapshot, \
                patch.object(host.os, "execve", side_effect=RuntimeError("synthetic exec capture")) as execute:
            with self.assertRaises(RuntimeError):
                host.entry(paths)
        argv = execute.call_args.args[1]
        child = argv[argv.index("--") + 1:]
        self.assertEqual(child[:5], ["/usr/bin/python3", "-B", "-m", "campaign_tool.records", "run"])
        self.assertIn("--documents", child)
        self.assertEqual(child[child.index("--documents") + 1], paths["documents"])
        self.assertEqual(child[child.index("--document-manifest-sha256") + 1], "b" * 64)
        self.assertNotIn("--mail-config", child)
        self.assertNotIn("--inbox", child)
        snapshot.assert_called_once_with("documents", paths["documents"], expected="b" * 64, readonly=True)
