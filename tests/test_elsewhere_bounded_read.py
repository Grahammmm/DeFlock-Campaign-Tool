"""RF0115-5: content/elsewhere.json is read through a bounded binary read (cap + 1 bytes) before it is refused or parsed.

Synthetic only: temp copies of examples/fictional-campaign, reserved .test hosts, no network. A thin forwarding wrapper
around the real binary handle records the size each read requested, how many bytes came back and whether the stream closed.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool import content_elsewhere
from campaign_tool.content import MAX_JSON_BYTES, ContentError
from campaign_tool.content_check import content_problems, run_cli
from campaign_tool.content_elsewhere import read_elsewhere
from campaign_tool.meeting_stage import stage
from campaign_tool.site import build_site

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "examples" / "fictional-campaign"
DAY = date(2026, 10, 1)
MID = "legistar-examplecity-3001"
TOO_LARGE = r"^elsewhere\.json: file too large$"
SENTINEL = "ZQ-PRIVATE-READ-SENTINEL"


def row(number=1, place=None):
    return {"place": place or f"Springfield {number}", "state": "OR", "body": "Springfield City Council",
            "decided_on": "2026-03-10", "kind": "paused", "as_stated": "The council continued the contract.",
            "source_url": f"https://records.example.test/minutes/{number}", "checked_on": "2026-09-15"}


def padded(prefix, size):
    """``prefix`` followed by legal JSON whitespace so the whole payload is exactly ``size`` bytes."""
    assert len(prefix) <= size
    return prefix + b" " * (size - len(prefix))


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-elsewhere-bound-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "campaign"
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns("public"))
        self.path = self.root / "content" / "elsewhere.json"
        self.public = self.root / "public"
        self.reads, self.closed, self.before_read, self.read_error = [], [], None, None
        for target in ("socket.socket", "urllib.request.urlopen"):
            guard = mock.patch(target, side_effect=AssertionError("network access attempted"))
            guard.start()
            self.addCleanup(guard.stop)
        real_fdopen, case = os.fdopen, self

        class Stream:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                case.closed.append(True)
                return self.handle.__exit__(*args)

            def close(self):
                case.closed.append(True)
                return self.handle.close()

            def read(self, size=-1):
                if case.before_read:
                    case.before_read()
                if case.read_error:
                    raise case.read_error
                data = self.handle.read(size)
                case.reads.append((size, len(data)))
                return data

            def __getattr__(self, key):
                return getattr(self.handle, key)

        def opened(descriptor, *args, **kwargs):
            # scoped to the target: only the descriptor that is the elsewhere.json file is wrapped
            try:
                target = os.path.samestat(os.fstat(descriptor), os.stat(case.path))
            except OSError:
                target = False
            handle = real_fdopen(descriptor, *args, **kwargs)
            return Stream(handle) if target else handle

        patcher = mock.patch.object(os, "fdopen", opened)
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, data):
        with open(self.path, "wb") as writer:  # builtin open: the fixture's own I/O is never recorded as production reads
            writer.write(data if isinstance(data, bytes) else data.encode("utf-8"))

    def raw(self, path):
        with open(path, "rb") as reader:
            return reader.read()

    def valid(self, *rows, ensure_ascii=True):
        return json.dumps(list(rows) or [row()], ensure_ascii=ensure_ascii).encode("utf-8")

    def assertBounded(self):
        self.assertTrue(self.reads, "the payload was never read")
        for requested, returned in self.reads:
            self.assertEqual(requested, MAX_JSON_BYTES + 1)
            self.assertLessEqual(returned, MAX_JSON_BYTES + 1)

    def snapshot(self, base):
        return {p.relative_to(base).as_posix(): (None if p.is_dir() else self.raw(p), p.lstat().st_mode & 0o7777)
                for p in sorted(base.rglob("*"))}

    def oversize(self, prefix=None):
        self.put(padded(prefix or self.valid(), MAX_JSON_BYTES * 8 + 1))


class ReaderBudgetTests(Case):
    def test_static_oversize_reads_no_more_than_cap_plus_one_before_refusal(self):
        self.oversize()
        with mock.patch.object(content_elsewhere.json, "loads", side_effect=AssertionError("parsed")), \
                mock.patch.object(content_elsewhere, "load_elsewhere", side_effect=AssertionError("loaded")):
            with self.assertRaisesRegex(ContentError, TOO_LARGE):
                read_elsewhere(self.root, DAY)
        self.assertEqual(self.reads, [(MAX_JSON_BYTES + 1, MAX_JSON_BYTES + 1)])
        self.assertEqual(len(self.closed), 1)

    def test_exact_utf8_byte_boundaries(self):
        text = self.valid(row(place="Z\u00fcrich \u2013 caf\u00e9"), ensure_ascii=False)
        self.assertGreater(len(text), len(text.decode("utf-8")))  # bytes differ from characters
        for size, refused in ((MAX_JSON_BYTES - 1, False), (MAX_JSON_BYTES, False), (MAX_JSON_BYTES + 1, True)):
            with self.subTest(size=size):
                self.put(padded(text, size))
                if refused:
                    with self.assertRaisesRegex(ContentError, TOO_LARGE):
                        read_elsewhere(self.root, DAY)
                else:
                    self.assertEqual(read_elsewhere(self.root, DAY)[0]["place"], "Z\u00fcrich \u2013 caf\u00e9")
        # a multi-byte character split by the overflow boundary is a size refusal, never a decode error
        self.put(b'["' + b"a" * (MAX_JSON_BYTES - 2) + "\u00e9".encode("utf-8"))  # the read ends inside the character
        with self.assertRaisesRegex(ContentError, TOO_LARGE):
            read_elsewhere(self.root, DAY)

    def test_growth_before_delegated_read_cannot_escape_budget(self):
        self.put(self.valid())

        def grow():
            self.before_read = None
            with open(self.path, "ab") as writer:
                writer.write(b" " * (MAX_JSON_BYTES * 2))
        self.before_read = grow
        with self.assertRaisesRegex(ContentError, TOO_LARGE):
            read_elsewhere(self.root, DAY)
        self.assertEqual(self.reads, [(MAX_JSON_BYTES + 1, MAX_JSON_BYTES + 1)])
        self.assertEqual(self.path.stat().st_size, len(self.valid()) + MAX_JSON_BYTES * 2)

    def test_binary_handle_closes_and_read_errors_keep_existing_category(self):
        cases = {"accepted": self.valid(), "overflow": padded(self.valid(), MAX_JSON_BYTES + 1),
                 "invalid utf-8": b"[\xff]", "malformed json": b"[{", "duplicate member": b'[{"a": 1, "a": 2}]'}
        for label, data in cases.items():
            with self.subTest(case=label):
                self.closed.clear()
                self.put(data)
                try:
                    read_elsewhere(self.root, DAY)
                except ContentError:
                    pass
                self.assertEqual(len(self.closed), 1)
        self.put(self.valid())
        self.read_error = OSError(SENTINEL)
        for collecting in (False, True):
            with self.subTest(collecting=collecting):
                self.closed.clear()
                problems = [] if collecting else None
                if collecting:
                    self.assertIsNone(read_elsewhere(self.root, DAY, problems))
                    self.assertEqual(problems, ["elsewhere.json: could not be read"])
                else:
                    with self.assertRaises(ContentError) as caught:
                        read_elsewhere(self.root, DAY)
                    self.assertEqual(str(caught.exception), "elsewhere.json: could not be read")
                    self.assertNotIn(SENTINEL, repr(caught.exception.__cause__) + str(caught.exception))
                self.assertEqual(len(self.closed), 1)

    def test_oversize_precedes_parse_and_collects_one_problem(self):
        for label, prefix in (("duplicate member", b'[{"a": 1, "a": 2}]'), ("malformed", b"[{"),
                              ("invalid utf-8", b"[\xff]")):
            with self.subTest(case=label):
                self.put(padded(prefix, MAX_JSON_BYTES + 1))
                with self.assertRaisesRegex(ContentError, TOO_LARGE):
                    read_elsewhere(self.root, DAY)
                problems = []
                self.assertIsNone(read_elsewhere(self.root, DAY, problems))
                self.assertEqual(problems, ["elsewhere.json: file too large"])
        for label, data, message in (("duplicate member", b'[{"a": 1, "a": 2}]', "a JSON object repeats a key"),
                                     ("invalid utf-8", b"[\xff]", "not valid UTF-8 JSON"),
                                     ("malformed", b"[{", "not valid UTF-8 JSON")):
            with self.subTest(under_cap=label):
                self.put(data)
                with self.assertRaisesRegex(ContentError, message):
                    read_elsewhere(self.root, DAY)


class ConsumerTests(Case):
    def test_real_checker_modes_keep_bounded_reads_and_existing_failure_shape(self):
        self.oversize()
        before = self.snapshot(self.root)
        self.assertEqual([p for p in content_problems(self.root) if p.startswith("elsewhere.json")],
                         ["elsewhere.json: file too large"])
        self.assertBounded()
        for as_json in (False, True):
            out, code = io.StringIO(), 0
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    run_cli(self.root, as_json=as_json)
                except SystemExit as exc:
                    code = exc.code
            self.assertEqual(code, 3)
            if as_json:
                found = [r for r in json.loads(out.getvalue())["problems"] if r["file"].startswith("elsewhere.json")]
                self.assertEqual(len(found), 1)
            else:
                self.assertIn("elsewhere.json: file too large", out.getvalue())
                self.assertNotIn("content/: 0 problem", out.getvalue())
        self.assertBounded()
        self.assertEqual(self.snapshot(self.root), before)

    def test_build_refusal_preserves_public_tree_and_source(self):
        self.put(self.valid())
        build_site(self.root, today=DAY)
        public_before = self.snapshot(self.public)
        self.oversize()
        source = (self.raw(self.path), self.path.stat().st_mode, self.path.stat().st_mtime_ns)
        self.reads.clear()
        with self.assertRaisesRegex(ContentError, TOO_LARGE):
            build_site(self.root, today=DAY)
        self.assertBounded()
        self.assertEqual(self.snapshot(self.public), public_before)
        self.assertEqual((self.raw(self.path), self.path.stat().st_mode, self.path.stat().st_mtime_ns), source)
        self.assertEqual(list(self.root.glob(".public.*")), [])
        shutil.rmtree(self.public)
        with self.assertRaisesRegex(ContentError, TOO_LARGE):
            build_site(self.root, today=DAY)
        self.assertFalse(self.public.exists())
        self.assertEqual(list(self.root.glob(".public.*")), [])

    def test_stage_first_and_replace_refuse_with_bounded_reads_and_no_mutation(self):
        (self.root / "kit").mkdir(exist_ok=True)
        (self.root / "kit" / "meetings.json").write_text(json.dumps({"schema_version": 1, "meetings": [
            {"id": MID, "body": "Example City Council", "starts_at": "2026-11-18T18:00:00-08:00"}]}), encoding="utf-8")
        target = self.root / "content" / "meetings" / f"{MID}.json"
        self.oversize()
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(ContentError, TOO_LARGE):
            stage(self.root, MID, elsewhere_indices=[1])
        self.assertBounded()
        self.assertFalse(target.exists())
        self.assertEqual(self.snapshot(self.root), before)
        self.put(self.valid())
        stage(self.root, MID, elsewhere_indices=[1])
        staged = (target.read_bytes(), target.stat().st_mode)
        self.oversize()
        self.reads.clear()
        with self.assertRaisesRegex(ContentError, TOO_LARGE):
            stage(self.root, MID, replace=True, elsewhere_indices=[1])
        self.assertBounded()
        self.assertEqual((target.read_bytes(), target.stat().st_mode), staged)

    def test_valid_order_duplicate_hook_and_absent_feature_are_compatible(self):
        self.put(self.valid(row(1, place="Z\u00fcrich"), row(2), ensure_ascii=False))
        entries = read_elsewhere(self.root, DAY)
        self.assertEqual([e["place"] for e in entries], ["Z\u00fcrich", "Springfield 2"])
        self.assertBounded()
        self.put(padded(self.valid(), MAX_JSON_BYTES))
        self.assertEqual(len(read_elsewhere(self.root, DAY)), 1)
        self.put(b'[{"a": 1, "a": 1}]')
        with self.assertRaisesRegex(ContentError, r"^elsewhere\.json: a JSON object repeats a key$"):
            read_elsewhere(self.root, DAY)
        # absent: no payload open, no output
        self.path.unlink()
        self.reads.clear()
        self.assertIsNone(read_elsewhere(self.root, DAY))
        self.assertEqual(self.reads, [])
        # static refusals never open the payload
        self.path.symlink_to(self.root / "nowhere.json")
        with self.assertRaisesRegex(ContentError, "symlinks are not read"):
            read_elsewhere(self.root, DAY)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaisesRegex(ContentError, "must be a regular file"):
            read_elsewhere(self.root, DAY)
        self.path.rmdir()
        os.mkfifo(self.path)
        with self.assertRaisesRegex(ContentError, "must be a regular file"):
            read_elsewhere(self.root, DAY)
        self.assertEqual(self.reads, [])


if __name__ == "__main__":
    unittest.main()
