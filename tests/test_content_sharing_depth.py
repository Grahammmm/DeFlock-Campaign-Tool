"""RF1005-8: ``content agency-sharing`` refuses an agencies.json nested beyond the JSON decoder's recursion limit.

Synthetic raw files only (a fictional campaign copied from the repository example into a temporary directory); no
network, build or publication. The decoder's RecursionError becomes one private SharingImportError before anything is
staged or written; every other failure keeps its existing behaviour.
"""
import errno
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_sharing
from campaign_tool.content import MAX_JSON_BYTES
from campaign_tool.content_sharing import SharingImportError, import_sharing

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
TODAY = date(2026, 11, 1)
AGENCY = "ca-cedar-police"
URL = "https://transparency.flocksafety.com/cedar-police"
CHECKED = "2026-10-31"
CSV = "name,state,kind\nCedar Hills PD,CA,local\nReno Test PD,NV,local\nFictional Federal Office,US,federal\n"
MESSAGE = "content/agencies.json: JSON nesting is too deep; nothing was written"
CANARY = "synthetic-depth-canary"
DEPTH = 20000  # far below MAX_JSON_BYTES in bytes, far above the decoder's recursion limit

DEEP = {
    "array": ("[" * DEPTH + f'"{CANARY}"' + "]" * DEPTH).encode(),
    "object": ('{"k":' * DEPTH + f'"{CANARY}"' + "}" * DEPTH).encode(),
    "nested value in a nominal envelope": (f'[{{"agency_id":"{AGENCY}","name":"x","extra":' + "[" * DEPTH + "]" * DEPTH + "}]").encode(),
}


def snapshot(directory):
    """Every entry of ``directory`` with bytes, mode, inode and mtime."""
    found = {}
    for path in sorted(Path(directory).iterdir()):
        info = path.lstat()
        found[path.name] = (info.st_ino, info.st_mode, info.st_mtime_ns, path.read_bytes() if path.is_file() else None)
    return found


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-sharing-depth-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.content = self.root / "content"
        self.target = self.content / "agencies.json"
        self.original = self.target.read_bytes()
        self.csv = self.tmp / "sharing.csv"
        self.csv.write_text(CSV, encoding="utf-8", newline="")
        (self.content / ".agencies.json.sentinel.tmp").write_bytes(b"unrelated")

    def deep(self, label):
        self.target.write_bytes(DEEP[label])
        self.assertLess(len(DEEP[label]), MAX_JSON_BYTES)

    def run_import(self, **kwargs):
        arguments = dict(agency_id=AGENCY, csv_path=self.csv, checked_on=CHECKED, source_url=URL, today=TODAY)
        arguments.update(kwargs)
        return import_sharing(self.root, **arguments)

    def cli(self, *extra):
        arguments = [sys.executable, "-B", "-m", "campaign_tool", "content", "agency-sharing", "--directory", str(self.root),
                     "--agency", AGENCY, "--csv", str(self.csv), "--checked-on", CHECKED, "--source-url", URL, *extra]
        return subprocess.run(arguments, cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)


class RealDepthTests(Base):
    def test_real_deep_array_and_object_refuse_for_add_and_replace(self):
        for label in DEEP:
            for replace in (False, True):
                with self.subTest(label=label, replace=replace):
                    self.deep(label)
                    before = snapshot(self.content)
                    with self.assertRaises(SharingImportError) as caught:
                        self.run_import(replace=replace)
                    self.assertEqual(str(caught.exception), MESSAGE)
                    self.assertTrue(caught.exception.__suppress_context__)
                    self.assertEqual(before, snapshot(self.content))
                    self.assertEqual(self.target.read_bytes(), DEEP[label])

    def test_real_cli_depth_refusal_is_one_private_stopped_line(self):
        for label in DEEP:
            for extra in ((), ("--replace",)):
                with self.subTest(label=label, extra=extra):
                    self.deep(label)
                    before = snapshot(self.content)
                    done = self.cli(*extra)
                    self.assertEqual(done.returncode, 1, done.stderr)
                    self.assertEqual(done.stdout, "")
                    self.assertEqual(done.stderr, f"Stopped: {MESSAGE}\n")
                    for leaked in ("Traceback", "RecursionError", CANARY, URL, AGENCY, "Fix", "Recorded only", str(self.tmp)):
                        self.assertNotIn(leaked, done.stderr + done.stdout)
                    self.assertEqual(before, snapshot(self.content))


class SeamTests(Base):
    def test_depth_refusal_closes_only_owned_descriptors_before_staging(self):
        self.deep("array")
        real_open, opened = os.open, []

        def recording_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd
        sentinel = os.open(self.csv, os.O_RDONLY)
        self.addCleanup(lambda: os.close(sentinel) if self._fd_alive(sentinel) else None)
        trips = {name: mock.Mock(side_effect=AssertionError(name)) for name in ("token_hex", "_sharing", "load_agencies", "dumps", "fsync", "replace", "_before_install")}
        with mock.patch.object(content_sharing.os, "open", side_effect=recording_open), \
                mock.patch.object(secrets, "token_hex", trips["token_hex"]), \
                mock.patch.object(content_sharing, "_sharing", trips["_sharing"]), \
                mock.patch.object(content_sharing, "load_agencies", trips["load_agencies"]), \
                mock.patch.object(content_sharing.json, "dumps", trips["dumps"]), \
                mock.patch.object(content_sharing.os, "fsync", trips["fsync"]), \
                mock.patch.object(content_sharing.os, "replace", trips["replace"]), \
                mock.patch.object(content_sharing, "_before_install", trips["_before_install"]):
            for _ in range(3):  # repeated refusals do not accumulate live descriptors
                with self.assertRaises(SharingImportError) as caught:
                    self.run_import()
                self.assertEqual(str(caught.exception), MESSAGE)
        for trip in trips.values():
            trip.assert_not_called()
        owned = [fd for fd in opened if fd != sentinel]
        self.assertGreaterEqual(len(owned), 9)  # root, content, target each time
        for fd in owned:
            self.assertFalse(self._fd_alive(fd), f"descriptor {fd} left open")
        self.assertTrue(self._fd_alive(sentinel))

    @staticmethod
    def _fd_alive(fd):
        try:
            os.fstat(fd)
        except OSError as exc:
            assert exc.errno == errno.EBADF
            return False
        return True

    def test_parse_seam_translation_is_narrow_and_private(self):
        before = snapshot(self.content)
        with mock.patch.object(content_sharing, "_parse_target", side_effect=RecursionError(CANARY)):
            with self.assertRaises(SharingImportError) as caught:
                self.run_import()
        self.assertEqual(str(caught.exception), MESSAGE)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn(CANARY, str(caught.exception))
        self.assertEqual(before, snapshot(self.content))
        # a RecursionError from any other stage is not mislabelled as decoder depth
        for name, patch in {
            "read": mock.patch.object(content_sharing, "_read_target", side_effect=RecursionError(CANARY)),
            "sharing": mock.patch.object(content_sharing, "_sharing", side_effect=RecursionError(CANARY)),
            "load_agencies": mock.patch.object(content_sharing, "load_agencies", side_effect=RecursionError(CANARY)),
            "serializer": mock.patch.object(content_sharing.json, "dumps", side_effect=RecursionError(CANARY)),
            "install": mock.patch.object(content_sharing, "_before_install", side_effect=RecursionError(CANARY)),
        }.items():
            with self.subTest(name), patch, self.assertRaises(RecursionError) as raised:
                self.run_import()
            self.assertEqual(str(raised.exception), CANARY)
            self.assertEqual(before, snapshot(self.content))
        # the shared helper itself is unchanged: a direct call keeps the decoder's own exception
        with self.assertRaises(RecursionError):
            content_sharing._parse_target(DEEP["array"])


class PrecedenceTests(Base):
    def refused(self, fragment, **kwargs):
        before = snapshot(self.content)
        with mock.patch.object(content_sharing, "_parse_target", side_effect=AssertionError("parser reached")):
            with self.assertRaises(SharingImportError) as caught:
                self.run_import(**kwargs)
        self.assertIn(fragment, str(caught.exception))
        self.assertNotIn("nesting", str(caught.exception))
        self.assertEqual(before, snapshot(self.content))

    def test_earlier_csv_date_source_path_and_size_refusals_stay_authoritative(self):
        self.deep("array")
        self.refused("--checked-on", checked_on="2026-11-02")
        self.refused("--source-url", source_url="http://example.invalid/x")
        self.csv.write_text("wrong,header\n", encoding="utf-8")
        self.refused("csv: header")
        self.csv.write_text(CSV, encoding="utf-8", newline="")
        self.target.write_bytes(b"[" * (MAX_JSON_BYTES + 1))  # oversized deep prefix: refused by size, parser never called
        self.refused("file too large")
        self.target.unlink()
        self.refused("not found")
        self.target.mkdir()
        self.refused("regular file")
        self.target.rmdir()
        shutil.rmtree(self.content)
        self.content.symlink_to(self.tmp)
        with self.assertRaises(SharingImportError) as caught:
            self.run_import()
        self.assertIn("content/", str(caught.exception))
        self.assertNotIn("nesting", str(caught.exception))

    def test_ordinary_syntax_and_encoding_errors_keep_their_messages(self):
        for data in (b"[{", b"[\xff]"):
            with self.subTest(data=data):
                self.target.write_bytes(data)
                with self.assertRaises(SharingImportError) as caught:
                    self.run_import()
                self.assertIn("not valid UTF-8 JSON", str(caught.exception))
                self.assertNotIn("nesting", str(caught.exception))


class LegacyContractTests(Base):
    def expected(self, recipients):
        document = json.loads(self.original.decode("utf-8"))
        entry = next(a for a in document if a["agency_id"] == AGENCY)
        entry.setdefault("profile", {})["sharing"] = {"checked_on": CHECKED, "source_url": URL, "recipients": recipients}
        return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    def test_shallow_valid_add_replace_and_failed_install_keep_legacy_contract(self):
        self.csv.write_text("name,state,kind\nZürich Test PD,CA,local\nReno Test PD,NV,local\nCedar Hills PD,CA,local\n", encoding="utf-8", newline="")
        before_other = {a["agency_id"]: a for a in json.loads(self.original) if a["agency_id"] != AGENCY}
        summary = self.run_import()
        self.assertEqual(summary, {"agency_id": AGENCY, "recipients": 3, "checked_on": CHECKED, "source_url": URL})
        want = self.expected([{"name": "Cedar Hills PD", "state": "CA", "kind": "local"}, {"name": "Zürich Test PD", "state": "CA", "kind": "local"},
                              {"name": "Reno Test PD", "state": "NV", "kind": "local"}])
        self.assertEqual(self.target.read_bytes(), want)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)
        self.assertEqual({a["agency_id"]: a for a in json.loads(want) if a["agency_id"] != AGENCY}, before_other)
        installed = self.target.read_bytes()
        with self.assertRaises(SharingImportError) as caught:  # an existing list needs --replace
            self.run_import()
        self.assertIn("--replace", str(caught.exception))
        self.assertEqual(self.target.read_bytes(), installed)
        self.csv.write_text("name,state,kind\nReno Test PD,NV,local\n", encoding="utf-8", newline="")
        self.assertEqual(self.run_import(replace=True)["recipients"], 1)
        self.assertEqual(self.target.read_bytes(), self.expected([{"name": "Reno Test PD", "state": "NV", "kind": "local"}]))
        # a failed write still leaves the original file and only unrelated entries behind
        before = snapshot(self.content)
        with mock.patch.object(content_sharing.os, "fsync", side_effect=OSError(errno.EIO, "synthetic")):
            with self.assertRaises(OSError):
                self.run_import(replace=True)
        self.assertEqual(before, snapshot(self.content))


if __name__ == "__main__":
    unittest.main()
