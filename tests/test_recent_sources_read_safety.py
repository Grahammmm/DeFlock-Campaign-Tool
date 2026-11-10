"""RF1005-10: `recent` reads content/sources.json through its bounded regular-file reader.

Synthetic campaign folders only; no network. Run from the repository root:
python3 -B -m unittest tests.test_recent_sources_read_safety -v
"""
import contextlib
import hashlib
import io
import json
import os
import shlex
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import cli, content, recent  # noqa: E402

TODAY = date(2026, 11, 4)
CAP = content.MAX_JSON_BYTES
PROBLEM = "content/sources.json unreadable"
CANARY_TITLE, CANARY_URL, CANARY_HASH = "Canary private title", "https://canary.example/secret", "d" * 64


def row(sha, received="2026-11-02T08:00:00Z", fragment="f", title="T"):
    return {"sha256": sha, "title": title, "agency_id": "ca-one", "received_at": received, "public_url": None,
            "fragment": fragment}


def hashes(count):
    return [f"{index:02x}" * 32 for index in range(count)]


class SourcesReadSafetyTests(unittest.TestCase):
    def setUp(self):
        offline = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network used"))
        offline.start()
        self.addCleanup(offline.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-recent-sources-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.root = self.tmp / "campaign root"
        self.root.mkdir()
        self.put("campaign.json", {"schema_version": 1, "name": "Synthetic", "county": "San Luis Obispo", "state": "CA"})
        self.put("content/meetings/good.json", {"id": "good", "body": "Synthetic Council",
                                                "starts_at": "2026-11-03T18:00:00-08:00"})
        self.sources = self.root / "content" / "sources.json"
        self.healthy = "Meeting on 2026-11-03: Synthetic Council (good)"

    def put(self, relative, document):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(document, bytes):
            path.write_bytes(document)
        else:
            path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")
        return path

    def report(self, since="2026-11-01", until=None, **kwargs):
        out, err = io.StringIO(), io.StringIO()
        code = recent.run_cli(self.root, since, until, today=TODAY, out=out, err=err, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def cli(self, *arguments):
        out, err, code = io.StringIO(), io.StringIO(), 0
        with mock.patch.object(sys, "argv", ["campaign_tool", "recent", "--directory", str(self.root), *arguments]), \
                mock.patch.object(recent, "_today", return_value=TODAY), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main()
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def assert_refused(self, code, out, err=""):
        self.assertEqual(code, 3, err)
        self.assertEqual(out.count(PROBLEM), 1)
        self.assertNotIn("records added to the library", out)
        self.assertNotIn("1 record added", out)
        self.assertIn(self.healthy, out)
        self.assertNotIn("Traceback", err)

    @staticmethod
    def no_read_all():
        """Path.read_bytes (the shared read-all) must never be used for the source leaf; other files read normally."""
        real = Path.read_bytes

        def guarded(path):
            if str(path).endswith("sources.json"):
                raise AssertionError("read-all of the source leaf")
            return real(path)

        return mock.patch.object(Path, "read_bytes", autospec=True, side_effect=lambda path: guarded(path))

    @staticmethod
    def no_big_decode(wrapped=None):
        """json.loads of a source-sized document fails the test; the small meeting file still decodes."""
        real = json.loads

        def guarded(text, *args, **kwargs):
            if len(text) > 1000:
                if wrapped is not None:
                    wrapped.append(len(text))
                else:
                    raise AssertionError("source decoded")
            return real(text, *args, **kwargs)

        return mock.patch("json.loads", side_effect=guarded)

    @contextlib.contextmanager
    def tripwire(self):
        """Any open of the source leaf (os.open, builtins/io open) fails the test; yields nothing."""
        real_os_open, leaf = os.open, "sources.json"

        def guarded_os_open(path, *args, **kwargs):
            if str(path).endswith(leaf):
                raise AssertionError("source leaf opened")
            return real_os_open(path, *args, **kwargs)

        def guarded_open(path, *args, **kwargs):
            if str(path).endswith(leaf):
                raise AssertionError("source leaf opened")
            return real_open(path, *args, **kwargs)

        real_open = open
        with mock.patch("os.open", side_effect=guarded_os_open), mock.patch("builtins.open", side_effect=guarded_open), \
                mock.patch("io.open", side_effect=guarded_open):
            yield

    def test_real_fifo_sources_are_unreadable_not_accepted(self):
        os.mkfifo(self.sources)
        before = os.lstat(self.sources)
        with self.tripwire():  # a no-writer FIFO is refused statically: opening it would block
            code, out, err = self.report()
            self.assert_refused(code, out, err)
            code, out, err = self.cli("--since", "2026-11-01")
            self.assert_refused(code, out, err)
        after = os.lstat(self.sources)
        self.assertTrue(stat.S_ISFIFO(after.st_mode))
        self.assertEqual((before.st_ino, before.st_mode), (after.st_ino, after.st_mode))

    def test_sources_static_file_kind_and_link_matrix(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps([row("a" * 64, fragment=CANARY_TITLE)]))
        internal = self.root / "content" / "copy.json"
        internal.write_text(json.dumps([row("a" * 64)]))
        # controls: absent is silent, [] is accepted, a valid library lists its record, null is a present invalid store
        code, out, _ = self.report()
        self.assertEqual(code, 0)
        self.assertNotIn(PROBLEM, out)
        self.put("content/sources.json", "[]")
        code, out, _ = self.report()
        self.assertEqual(code, 0)
        self.assertNotIn(PROBLEM, out)
        self.sources.write_text(json.dumps([row("a" * 64)]))
        code, out, _ = self.report()
        self.assertEqual(code, 0)
        self.assertIn("1 record added to the library: " + "a" * 12, out)
        self.sources.write_text("null")
        self.assert_refused(*self.report())
        for case in ("external link", "internal link", "dangling link", "directory", "fifo"):
            with self.subTest(case=case):
                if self.sources.is_dir() and not self.sources.is_symlink():
                    self.sources.rmdir()
                else:
                    self.sources.unlink()
                if case.endswith("link"):
                    self.sources.symlink_to({"external link": outside, "internal link": internal,
                                             "dangling link": self.tmp / "gone.json"}[case])
                elif case == "directory":
                    self.sources.mkdir()
                else:
                    os.mkfifo(self.sources)
                with self.tripwire():
                    code, out, err = self.report()
                self.assert_refused(code, out, err)
                self.assertNotIn(str(outside), out + err)
                self.assertNotIn(CANARY_TITLE, out + err)
        self.assertEqual(outside.read_text(), json.dumps([row("a" * 64, fragment=CANARY_TITLE)]))

    def test_sources_oversize_is_refused_before_body_read(self):
        self.sources.write_bytes(b"[]" + b" " * (CAP - 1))  # CAP + 1 bytes of valid JSON
        with self.tripwire(), self.no_read_all(), self.no_big_decode(), \
                mock.patch.object(recent, "load_sources", side_effect=AssertionError("validated")):
            self.assert_refused(*self.report())
        with open(self.sources, "wb") as handle:  # a much larger sparse file is refused unread too
            handle.truncate(CAP * 8)
        with self.tripwire(), self.no_read_all():
            self.assert_refused(*self.report())

    def spy_reads(self):
        """Record (requested, returned) for every read on a source handle opened via os.fdopen."""
        reads, real_fdopen = [], os.fdopen

        class Handle:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *exc):
                return self.handle.__exit__(*exc)

            def fileno(self):
                return self.handle.fileno()

            def read(self, size=-1):
                data = self.handle.read(size)
                reads.append((size, len(data)))
                return data

        return reads, mock.patch("os.fdopen", side_effect=lambda *a, **k: Handle(real_fdopen(*a, **k)))

    def test_sources_exact_cap_and_post_admission_growth(self):
        body = b"[]" + b" " * (CAP - 2)
        self.sources.write_bytes(body)
        reads, spy = self.spy_reads()
        with spy:
            code, out, err = self.report()
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn(PROBLEM, out)
        self.assertEqual(reads, [(CAP + 1, CAP)])
        # multibyte: CAP bytes but far fewer characters is read; CAP + 1 bytes is refused before decoding
        multibyte = b'["' + "é".encode() * ((CAP - 4) // 2) + b'"]'
        self.assertEqual(len(multibyte), CAP)
        self.sources.write_bytes(multibyte)
        decoded = []
        with self.no_big_decode(decoded):
            self.assert_refused(*self.report())
        self.assertEqual(decoded, [CAP // 2 + 2])  # admitted and decoded; the invalid row is refused by the validator
        self.sources.write_bytes(multibyte + b" ")
        with self.no_big_decode():
            self.assert_refused(*self.report())
        # grows after the size inspection: at most CAP + 1 bytes are returned and nothing is decoded or validated
        self.sources.write_bytes(body)
        real_os_open, grown = os.open, []

        def growing_open(path, *args, **kwargs):
            if str(path).endswith("sources.json") and not grown:
                grown.append(True)
                with open(path, "ab") as handle:
                    handle.write(b" " * 4096)
            return real_os_open(path, *args, **kwargs)

        reads, spy = self.spy_reads()
        with spy, mock.patch("os.open", side_effect=growing_open), \
                mock.patch.object(recent, "load_sources", side_effect=AssertionError("validated")), \
                self.no_big_decode():
            self.assert_refused(*self.report())
        self.assertEqual(grown, [True])
        self.assertEqual(reads, [(CAP + 1, CAP + 1)])

    def test_opened_nonregular_sources_never_reach_read_or_decode(self):
        self.sources.write_text("[]")  # a regular file at inspection time ...
        fifo = self.tmp / "swapped.fifo"
        os.mkfifo(fifo)
        real_os_open, opened = os.open, []

        def swapped_open(path, flags, *args, **kwargs):
            if str(path).endswith("sources.json"):
                descriptor = real_os_open(fifo, os.O_RDWR | os.O_NONBLOCK)  # ... a FIFO descriptor when opened
                opened.append(descriptor)
                return descriptor
            return real_os_open(path, flags, *args, **kwargs)

        reads, spy = self.spy_reads()
        with spy, mock.patch("os.open", side_effect=swapped_open), \
                mock.patch.object(recent, "load_sources", side_effect=AssertionError("validated")), \
                self.no_big_decode():
            self.assert_refused(*self.report())
        self.assertEqual(len(opened), 1)
        self.assertEqual(reads, [])
        with self.assertRaises(OSError):  # the refused descriptor was closed
            os.fstat(opened[0])

    def test_source_validation_failure_is_atomic_and_private(self):
        valid, invalid = row("a" * 64, title=CANARY_TITLE), {"sha256": CANARY_HASH, "title": CANARY_TITLE,
                                                             "public_url": CANARY_URL}
        cases = {"truncated json": b"[", "bad utf-8": b"[\xff]", "wrong shape": {"sha256": "a" * 64},
                 "null": "null", "invalid row": [invalid], "valid then invalid row": [valid, invalid],
                 "deep": "[" * 4001}
        md = self.tmp / "report.md"
        for name, document in cases.items():
            with self.subTest(case=name):
                self.put("content/sources.json", document)
                if md.exists():
                    md.unlink()
                code, out, err = self.report(md=str(md))
                self.assert_refused(code, out, err)
                code_json, as_json, err_json = self.report(as_json=True)
                self.assertEqual(code_json, 3)
                self.assertEqual([event["area"] for event in json.loads(as_json)], ["Meetings"])
                self.assertEqual(err_json.count(PROBLEM), 1)
                for text in (out, as_json, err, err_json, md.read_text()):
                    for canary in (CANARY_TITLE, CANARY_URL, CANARY_HASH, "a" * 64, str(self.root), "Errno"):
                        self.assertNotIn(canary, text)
                self.assertIn(PROBLEM, md.read_text())

    def test_source_problem_preserves_cli_and_markdown_contract(self):
        self.put("content/sources.json", "[")
        before = {p: os.lstat(p).st_mtime_ns for p in self.root.rglob("*")}
        code, out, err = self.cli("--since", "2026-11-01")
        self.assert_refused(code, out, err)
        self.assertEqual(err, "")
        code, out, err = self.cli("--since", "2026-11-01", "--json")
        self.assertEqual(code, 3)
        self.assertEqual([event["area"] for event in json.loads(out)], ["Meetings"])
        self.assertIn(PROBLEM, err)
        self.assertNotIn("Traceback", err)
        target = self.tmp / "report.md"
        code, out, err = self.cli("--since", "2026-11-01", "--md", str(target))
        self.assertEqual(code, 3, err)
        self.assertEqual(stat.S_IMODE(os.lstat(target).st_mode), 0o600)
        self.assertIn(PROBLEM, target.read_text())
        self.assertIn("Meeting on 2026-11-03: Synthetic Council", target.read_text())
        code, _, err = self.cli("--since", "2026-11-01", "--md", str(target))  # new-only: an existing target is refused
        self.assertNotEqual(code, 3)
        self.assertEqual(before, {p: os.lstat(p).st_mtime_ns for p in before})
        code, _, err = self.cli("--since", "not-a-day")  # a bad flag still wins, before any source read
        self.assertEqual(code, 2)
        self.assertNotIn(PROBLEM, err)
        moved = self.tmp / "moved-content"
        (self.root / "content").rename(moved)
        (self.root / "content").symlink_to(moved, target_is_directory=True)
        refused = self.tmp / "never.md"
        code, out, err = self.cli("--since", "2026-11-01", "--md", str(refused))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertFalse(refused.exists())

    def snapshot(self):
        result = {}
        for path in sorted(self.tmp.rglob("*")):
            info = os.lstat(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
            result[str(path)] = (info.st_mode, info.st_size, info.st_mtime_ns, digest)
        return result

    def test_valid_sources_keep_legacy_bytes_and_campaign_unchanged(self):
        nine = hashes(9)
        rows = [row(nine[0], "2026-11-01T00:00:00Z", "f1"), row(nine[0], "2026-11-01", "f2"),  # same hash, one day
                row(nine[1], "2026-11-01", "f3"), row(nine[2], "2026-11-04T23:59:59Z", "f4"),
                row("e" * 64, "2026-10-31", "outside-window"), row("f" * 64, "20261102", "undated")]
        rows += [row(h, "2026-11-03", f"g{index}") for index, h in enumerate(nine)]  # nine hashes: prefix truncation
        self.put("content/sources.json", rows)
        before = self.snapshot()
        code, out, err = self.report("2026-11-01", "2026-11-04")
        self.assertEqual((code, err), (0, ""), out)
        shown = ", ".join(h[:12] for h in nine[:8])
        directory = shlex.quote(str(self.root))
        expected = "\n".join([
            "Recent changes, 2026-11-01 to 2026-11-04 (private; nothing was sent or published)", "",
            "Requests", "nothing recorded", "",
            "Records",
            f"2026-11-04  1 record added to the library: {nine[2][:12]}  → sources list --directory {directory}",
            f"2026-11-03  9 records added to the library: {shown} (+1 more)  → sources list --directory {directory}",
            f"2026-11-01  2 records added to the library: {nine[0][:12]}, {nine[1][:12]}  → sources list --directory "
            f"{directory}", "",
            "Findings", "nothing recorded", "",
            "Meetings",
            f"2026-11-03  {self.healthy}  → open content/meetings/good.json", ""])
        self.assertEqual(out.splitlines()[:len(expected.splitlines()) - 1], expected.splitlines()[:-1])
        self.assertIn("1 undated", out)
        self.assertEqual(self.snapshot(), before)
        for document in ("[]", None):  # an empty library and a missing one add no Records event and no problem
            if document is None:
                self.sources.unlink()
            else:
                self.sources.write_text(document)
            code, out, err = self.report("2026-11-01", "2026-11-04")
            self.assertEqual((code, err), (0, ""))
            self.assertNotIn(PROBLEM, out)
            self.assertNotIn("added to the library", out)


if __name__ == "__main__":
    unittest.main()
