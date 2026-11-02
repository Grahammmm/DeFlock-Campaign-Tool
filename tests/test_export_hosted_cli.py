"""Runner-host backup command, entirely synthetic and without network calls."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool import backup
from runner.__main__ import main
from runner.client import Workspace


class Response(io.BytesIO):
    status = 200
    headers = {}


class HostedCommandTests(unittest.TestCase):
    def server(self, corrupt=False):
        objects = [b"synthetic document\n", b"From: sender@example.invalid\n\nsynthetic mail\n"]
        rows = [{"sha256": hashlib.sha256(data).hexdigest(), "byte_count": len(data),
                 "r2_key": ("mail/" if i else "originals/") + hashlib.sha256(data).hexdigest()}
                for i, data in enumerate(objects)]
        doc = {"schema_version": 1, "campaign_id": "synthetic", "tables": ["campaign", "original", "receipt_occurrence"],
               "rows": {"campaign": [{"campaign_id": "synthetic", "name": "Synthetic", "jurisdiction": "us-ca"}],
                        "original": rows, "receipt_occurrence": [{"sha256": row["sha256"]} for row in rows]}}
        calls = []

        def opener(request, timeout):
            calls.append((request.full_url, request.get_header("Authorization")))
            if request.full_url.endswith("/export.json"):
                data = json.dumps(doc).encode()
            else:
                index = next(i for i, row in enumerate(rows) if request.full_url.endswith(row["sha256"]))
                data = b"corrupt" if corrupt else objects[index]
            return Response(data)
        return opener, calls, rows

    def run_export(self, out, corrupt=False):
        opener, calls, rows = self.server(corrupt)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"WORKSPACE_URL": "https://workspace.example.invalid", "RUNNER_TOKEN": "synthetic-runner-token"}, clear=True), \
                patch("urllib.request.urlopen", opener), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(["export-hosted", "--out", str(out)])
        self.assertNotIn("synthetic-runner-token", stdout.getvalue() + stderr.getvalue())
        return result, stdout.getvalue(), stderr.getvalue(), calls, rows

    def test_command_writes_verified_archive_including_mail_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "hosted.tar"
            result, stdout, stderr, calls, rows = self.run_export(out)
            self.assertEqual(result, 0, stderr)
            summary = json.loads(stdout)
            self.assertEqual(summary["objects"], 2)
            report = backup.verify(out, expected_manifest_sha256=summary["manifest_sha256"])
            self.assertEqual(report["counts"]["objects"], 2)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            self.assertEqual([url for url, _ in calls], ["https://workspace.example.invalid/api/runner/export.json"] +
                             ["https://workspace.example.invalid/api/runner/originals/" + r["sha256"] for r in rows])
            self.assertTrue(all(auth == "Bearer synthetic-runner-token" for _, auth in calls))

    def test_hash_mismatch_stops_and_removes_partial_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "bad.tar"
            result, stdout, stderr, _, _ = self.run_export(out, corrupt=True)
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            self.assertIn("Stopped:", stderr)
            self.assertFalse(out.exists())

    def test_get_refuses_paths_outside_backup_api(self):
        client = Workspace("https://workspace.example.invalid", "synthetic")
        for path in ["https://elsewhere.example.invalid", "/jobs", "/api/runner/../export.json", "/api/runner/export.json?token=x"]:
            with self.assertRaises(ValueError):
                client.get(path)

    def test_missing_environment_stops_before_network(self):
        with patch.dict("os.environ", {}, clear=True), patch("urllib.request.urlopen") as opener, \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            main(["export-hosted", "--out", "unused.tar"])
        self.assertEqual(exc.exception.code, 2)
        opener.assert_not_called()

    def test_token_argument_is_not_accepted(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            main(["export-hosted", "--out", "unused.tar", "--token", "synthetic"])
        self.assertEqual(exc.exception.code, 2)

    def test_remote_exception_cannot_echo_token(self):
        from runner.client import WorkspaceError
        stderr = io.StringIO()
        with patch.dict("os.environ", {"WORKSPACE_URL": "https://workspace.example.invalid", "RUNNER_TOKEN": "synthetic-private-token"}, clear=True), \
                patch.object(Workspace, "get", side_effect=WorkspaceError(403, "synthetic-private-token")), \
                contextlib.redirect_stderr(stderr):
            result = main(["export-hosted", "--out", "unused.tar"])
        self.assertEqual(result, 1)
        self.assertNotIn("synthetic-private-token", stderr.getvalue())

    def test_existing_archive_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "existing.tar"
            out.write_bytes(b"existing backup")
            result, _, _, calls, _ = self.run_export(out)
            self.assertEqual(result, 1)
            self.assertEqual(out.read_bytes(), b"existing backup")
            self.assertEqual(calls, [])
