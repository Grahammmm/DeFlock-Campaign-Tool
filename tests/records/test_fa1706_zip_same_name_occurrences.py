"""Offline, parser-local ZIP entry identity qualification (not ledger acceptance).

All archives and payloads are fictional. No operator manifest is needed to load
or run these tests using ordinary unittest discovery.
"""
import builtins
from contextlib import ExitStack, contextmanager
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import resource
import socket
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
import warnings
import zipfile


REPO = Path(__file__).resolve().parents[2]
SOURCE = REPO / "campaign_tool/records/intake/folder.py"
A = b"Fictional archive record alpha\n"
B = b"Fictional archive record beta\n"
C = b"Fictional archive record gamma\n"
SAME = "record.txt"
ENTRIES = ((SAME, A), (SAME, B), (SAME, A), ("empty/", b""), ("other.txt", C))


def denied(*args, **kwargs):
    raise RuntimeError("FA1706 offline sentinel: unexpected external operation")


@contextmanager
def offline_guards(root, *, allow_children=False):
    """Scoped accident sentinels, not a hostile-code sandbox.

    Config has no host data constants or bound data-path defaults. The direct
    worker always receives explicit config; CLI compatibility supplies synthetic
    roots/output. Library/source reads are allowed; writes stay fixture-owned.
    """
    root = Path(root).resolve()
    original_import = builtins.__import__
    original_open, original_io_open, original_os_open = builtins.open, io.open, os.open
    libraries = [Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve(), REPO]
    libraries.extend(Path(p).resolve() for p in sys.path if p and Path(p).is_dir()
                     and ("site-packages" in p or "dist-packages" in p))

    def checked_path(value, writing):
        if isinstance(value, int):
            return
        p = Path(os.path.abspath(value))
        if p.resolve() != p:
            raise RuntimeError("FA1706 symlink data path refused")
        if p.is_relative_to(root):
            return
        if not writing and any(p.is_relative_to(lib) for lib in libraries):
            return
        raise RuntimeError("FA1706 non-fixture data path refused: " + str(p))

    def guarded_open(file, mode="r", *args, **kwargs):
        checked_path(file, any(c in mode for c in "wax+"))
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(file, mode="r", *args, **kwargs):
        checked_path(file, any(c in mode for c in "wax+"))
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(file, flags, mode=0o777, *, dir_fd=None):
        # secure_open walks directory descriptors from /; only its final read is
        # relative. The caller's source is checked separately before worker use.
        if dir_fd is None and not flags & os.O_DIRECTORY:
            checked_path(file, bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)))
        return original_os_open(file, flags, mode, dir_fd=dir_fd)

    forbidden = ("imaplib", "smtplib", "requests", "httpx", "ollama", "openai",
                 "torch", "pytesseract", "campaign_tool.records.ocr",
                 "campaign_tool.records.queue", "campaign_tool.records.intake.eml_export")

    def guarded_import(name, *args, **kwargs):
        if any(name == p or name.startswith(p + ".") for p in forbidden):
            denied()
        return original_import(name, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch("builtins.__import__", guarded_import))
        stack.enter_context(mock.patch("builtins.open", guarded_open))
        stack.enter_context(mock.patch("io.open", guarded_io_open))
        stack.enter_context(mock.patch("os.open", guarded_os_open))
        stack.enter_context(mock.patch.object(socket.socket, "connect", denied))
        stack.enter_context(mock.patch.object(socket.socket, "connect_ex", denied))
        stack.enter_context(mock.patch.object(socket, "create_connection", denied))
        stack.enter_context(mock.patch.object(resource, "setrlimit", denied))
        if not allow_children:
            stack.enter_context(mock.patch.object(subprocess, "Popen", denied))
        yield


def load_folder():
    spec = importlib.util.spec_from_file_location("campaign_tool.records.intake.fa1706_folder_under_test", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError("FA1706 source loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_archive(path, entries):
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="Duplicate name:.*", category=UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, data in entries:
                info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_STORED
                archive.writestr(info, data)


def assert_ordered_payloads(case, observed, entries):
    case.assertEqual(observed, [data for name, data in entries if not name.endswith("/")],
                     "FA1706 ordered payloads must match each source entry")


class ZipCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fa1706-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.sequence = 0

    def parse(self, entries, *, faulty=False):
        self.sequence += 1
        root = self.base / ("parse-" + str(self.sequence))
        root.mkdir()
        src, out, dest = root / "source.zip", root / "out", root / "derived"
        out.mkdir()
        write_archive(src, entries)
        raw = src.read_bytes()
        source_hash = hashlib.sha256(raw).hexdigest()
        real_zip = zipfile.ZipFile

        class FilenameReader(real_zip):
            def open(self, name, mode="r", pwd=None, *, force_zip64=False):
                if isinstance(name, zipfile.ZipInfo):
                    name = name.filename
                return super().open(name, mode, pwd, force_zip64=force_zip64)

        with offline_guards(root):
            folder = load_folder()
            config = {"roots": [str(root)], "output": str(out),
                      "agency_patterns": {}, "excluded_path_fragments": []}
            folder.validate_config(config, "run")
            with ExitStack() as stack:
                limit = stack.enter_context(mock.patch.object(
                    folder, "apply_resource_limit", side_effect=lambda kind, requested: requested))
                stack.enter_context(mock.patch.object(folder, "load_config", side_effect=denied))
                if faulty:
                    stack.enter_context(mock.patch.object(folder.zipfile, "ZipFile", FilenameReader))
                limits = dict(folder.LIMITS)
                folder.worker(src, source_hash, "zip", dest, out, limits, 0, config=config)
                limit.assert_called_once_with(resource.RLIMIT_AS, limits["memory_bytes"])
            result = json.loads((dest / "digest.json").read_text())
            units = [json.loads(line) for line in (dest / "units.jsonl").read_text().splitlines()]
            payloads = [(out / "blobs" / child["sha"]).read_bytes() for child in result["children"]]
            blobs = sorted(p.name for p in (out / "blobs").iterdir())
            self.assertEqual(src.read_bytes(), raw)
        return result, units, payloads, blobs

    def qualify(self, entries):
        result, units, payloads, blobs = self.parse(entries)
        self.assertEqual(result["stage"], "complete")
        self.assertEqual(result["review_status"], "not_reviewed")
        self.assertTrue(result["children_inventory_complete"])
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["counts"]["zip_member"], len(entries))
        self.assertEqual(len(units), len(entries))
        expected_locs = [{"member_index": n, "member": name}
                         for n, (name, _) in enumerate(entries, 1)]
        self.assertEqual([u["kind"] for u in units], ["zip_member"] * len(entries))
        self.assertEqual([u["locator"] for u in units], expected_locs)
        self.assertEqual([u["data"]["bytes"] for u in units], [len(data) for _, data in entries])
        self.assertEqual([u["data"]["compressed_bytes"] for u in units], [len(data) for _, data in entries])
        non_dirs = [(loc, name, data) for loc, (name, data) in zip(expected_locs, entries)
                    if not name.endswith("/")]
        self.assertEqual(len(result["children"]), len(non_dirs))
        self.assertEqual([c["locator"] for c in result["children"]], [v[0] for v in non_dirs])
        self.assertEqual([c["name"] for c in result["children"]], [v[1] for v in non_dirs])
        self.assertEqual([c["relation"] for c in result["children"]], ["zip_member"] * len(non_dirs))
        self.assertEqual([c["bytes"] for c in result["children"]], [len(v[2]) for v in non_dirs])
        assert_ordered_payloads(self, payloads, entries)
        hashes = [hashlib.sha256(v[2]).hexdigest() for v in non_dirs]
        self.assertEqual([c["sha"] for c in result["children"]], hashes)
        self.assertEqual(blobs, sorted(set(hashes)))
        self.assertEqual(result["counts"]["expanded_bytes"], sum(len(v[2]) for v in non_dirs))
        return result

class ZipSameNameTests(ZipCase):
    def test_three_same_names_preserve_aba_and_directory(self):
        result = self.qualify(ENTRIES)
        self.assertEqual(len(result["children"]), 4)
        self.assertEqual(len({c["sha"] for c in result["children"]}), 3)
        self.assertEqual(result["children"][0]["sha"], result["children"][2]["sha"])
        self.assertNotEqual(result["children"][0]["sha"], result["children"][1]["sha"])

    def test_reordered_entries_keep_position_not_filename_identity(self):
        first = self.qualify(ENTRIES)
        order = (4, 3, 1, 0, 2)
        reordered = tuple(ENTRIES[i] for i in order)
        second = self.qualify(reordered)
        self.assertEqual([c["sha"] for c in second["children"]],
                         [hashlib.sha256(data).hexdigest() for name, data in reordered
                          if not name.endswith("/")])
        self.assertNotEqual([c["locator"] for c in first["children"]],
                            [c["locator"] for c in second["children"]])

    def test_equal_name_equal_bytes_is_two_occurrences_one_blob(self):
        result = self.qualify(((SAME, A), (SAME, A)))
        self.assertEqual(len(result["children"]), 2)
        self.assertEqual(len({c["sha"] for c in result["children"]}), 1)

    def test_equal_name_unequal_bytes_is_two_occurrences_two_blobs(self):
        result = self.qualify(((SAME, A), (SAME, B)))
        self.assertEqual(len(result["children"]), 2)
        self.assertEqual(len({c["sha"] for c in result["children"]}), 2)


class FilenameLookupControl(ZipCase):
    def test_filename_lookup_control(self):
        self.qualify(ENTRIES)
        result, units, payloads, blobs = self.parse(ENTRIES, faulty=True)
        self.assertEqual(result["stage"], "complete")
        self.assertEqual(result["issues"], [])
        self.assertEqual(len(units), 5)
        self.assertEqual(len(result["children"]), 4)
        # Only this precise oracle failure qualifies; unrelated exceptions fail.
        try:
            assert_ordered_payloads(self, payloads, ENTRIES)
        except AssertionError as error:
            self.assertIn("FA1706 ordered payloads", str(error))
            evidence = traceback.format_exc()
        else:
            self.fail("filename lookup unexpectedly passed the ordered payload oracle")
        output = os.environ.get("FA1706_CONTROL_EVIDENCE")
        if output:
            Path(output).write_text(evidence, encoding="utf-8")
        self.qualify(ENTRIES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
