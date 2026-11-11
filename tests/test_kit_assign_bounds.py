"""RF2315-4: real-file byte limits and read-only consumer refusals, synthetic only."""
import contextlib
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import kit_assign
from tests import test_kit_assign as existing

LIMIT = kit_assign.MAX_BYTES
SIZE_ERROR = "kit/assignments.json unreadable: larger than 1 MiB"
REAL_OPEN = Path.open
REAL_OS_OPEN, REAL_FSTAT, REAL_FDOPEN = os.open, os.fstat, os.fdopen


class ReadProbe:
    """Forward to real handles, optionally growing the store at the open seam."""

    def __init__(self, path, grow_to=None, after_growth=None, enforce=True):
        self.path = path
        self.grow_to = grow_to
        self.after_growth = after_growth
        self.enforce = enforce
        self.handles = []
        self.reads = []
        self.fds = []
        self.grown = False

    def is_store(self, path):
        return isinstance(path, (str, os.PathLike)) and os.fspath(path) == str(self.path)

    @contextlib.contextmanager
    def active(self):
        probe = self

        def opened(path, flags, *args, **kwargs):
            fd = REAL_OS_OPEN(path, flags, *args, **kwargs)
            if probe.is_store(path):
                probe.fds.append(fd)
            return fd

        def statted(fd, *args, **kwargs):
            result = REAL_FSTAT(fd, *args, **kwargs)  # the genuine metadata snapshot
            if fd in probe.fds and probe.grow_to is not None and not probe.grown:
                # Grow the same admitted inode right after the snapshot, then return the stale snapshot, so only
                # the finite read and actual-length guard can notice.
                with REAL_OPEN(probe.path, "ab") as handle:
                    handle.write(b" " * (probe.grow_to - result.st_size))
                probe.grown = True
                if probe.after_growth:
                    probe.after_growth()
            return result

        def fdopened(fd, *args, **kwargs):
            handle = REAL_FDOPEN(fd, *args, **kwargs)
            if fd not in probe.fds:
                return handle
            probe.handles.append(handle)

            class Forward:
                def __enter__(inner):
                    return inner

                def __exit__(inner, *exc):
                    return handle.__exit__(*exc)

                def read(inner, size=-1):
                    if probe.enforce and size != LIMIT + 1:
                        raise AssertionError(f"unbounded/unexpected read: {size}")
                    raw = handle.read(size)
                    probe.reads.append((size, len(raw)))
                    return raw

            return Forward()

        with mock.patch.object(os, "open", opened), mock.patch.object(os, "fstat", statted), \
                mock.patch.object(os, "fdopen", fdopened):
            yield self


def snapshot(root):
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            with REAL_OPEN(path, "rb") as handle:
                result[str(path.relative_to(root))] = (handle.read(), path.stat().st_mode, path.stat().st_mtime_ns)
    return result


class KitAssignBoundsTests(unittest.TestCase):
    setUp = existing.KitAssignTests.setUp
    ok = existing.KitAssignTests.ok
    put_sent = existing.KitAssignTests.put_sent
    write_store = existing.KitAssignTests.write_store

    def payload(self):
        return json.dumps({"schema_version": 1, "assignments": {
            self.a: "Zoë", f"{self.a}#press": "Team", "ca-gone-pd": "Orphan"}},
            ensure_ascii=False).encode("utf-8")

    def assert_probe(self, probe):
        self.assertTrue(probe.handles)
        self.assertTrue(all(h.closed for h in probe.handles))
        self.assertTrue(probe.reads)
        self.assertTrue(all(size == LIMIT + 1 and length <= LIMIT + 1 for size, length in probe.reads))

    def test_real_growth_after_precheck_is_refused_before_decode(self):
        for size in (LIMIT + 1, 3 * LIMIT):
            with self.subTest(size=size):
                self.store.write_bytes(self.payload())
                probe = ReadProbe(self.store, size, enforce=False)
                with probe.active(), mock.patch.object(kit_assign.json, "loads", wraps=json.loads) as loads:
                    with self.assertRaisesRegex(ValueError, "^" + SIZE_ERROR + "$"):
                        kit_assign.read_assignments(self.root)
                    loads.assert_not_called()
                self.assert_probe(probe)
                self.assertEqual(self.store.stat().st_size, size)
        # Invalid UTF-8 in an oversized read must still be called oversized.
        self.store.write_bytes(b"\xff")
        with ReadProbe(self.store, LIMIT + 1).active():
            with self.assertRaisesRegex(ValueError, "^" + SIZE_ERROR + "$"):
                kit_assign.read_assignments(self.root)

    def test_binary_reader_is_bounded_and_closed(self):
        for raw, error in ((self.payload(), None), (b"\xff", "not JSON"),
                           (b"{", "not JSON"), (b"", "not JSON")):
            with self.subTest(raw=raw[:10]):
                self.store.write_bytes(raw)
                probe = ReadProbe(self.store)
                with probe.active():
                    if error:
                        with self.assertRaisesRegex(ValueError, error):
                            kit_assign.read_assignments(self.root)
                    else:
                        self.assertEqual(kit_assign.read_assignments(self.root)[self.a], "Zoë")
                self.assert_probe(probe)
        self.store.write_bytes(self.payload())
        probe = ReadProbe(self.store, LIMIT + 1)
        with probe.active(), self.assertRaisesRegex(ValueError, "larger than 1 MiB"):
            kit_assign.read_assignments(self.root)
        self.assert_probe(probe)

    def test_exact_byte_boundaries_and_multibyte_labels(self):
        raw = self.payload()
        expected = json.loads(raw)["assignments"]
        for size in (LIMIT - 1, LIMIT):
            with self.subTest(size=size):
                self.store.write_bytes(raw + b" " * (size - len(raw)))
                probe = ReadProbe(self.store)
                with probe.active():
                    self.assertEqual(kit_assign.read_assignments(self.root), expected)
                self.assertEqual(probe.reads, [(LIMIT + 1, size)])
                self.assert_probe(probe)
        self.store.write_bytes(raw + b" " * (LIMIT + 1 - len(raw)))
        probe = ReadProbe(self.store)
        with probe.active(), self.assertRaisesRegex(ValueError, "^" + SIZE_ERROR + "$"):
            kit_assign.read_assignments(self.root)
        self.assertEqual(probe.handles, [])  # static oversize still refuses early

    def grown_command(self, arguments, on_growth=None):
        self.store.write_bytes(self.payload())
        after_growth = []

        def remember():
            if on_growth:
                on_growth()
            after_growth.append(snapshot(self.root))

        probe = ReadProbe(self.store, 2 * LIMIT, after_growth=remember)
        with probe.active(), mock.patch("os.replace", side_effect=AssertionError("replacement attempted")), \
                mock.patch("tempfile.mkstemp", side_effect=AssertionError("temporary file attempted")):
            result = existing.run(arguments[0], "--directory", self.root, *arguments[1:])
        self.assert_probe(probe)
        self.assertTrue(probe.grown)
        self.assertEqual(snapshot(self.root), after_growth[0])
        return result

    def test_grown_store_stops_set_clear_and_noop_without_writes(self):
        self.put_sent({self.a: existing.imported("2026-03-02"), self.b: existing.imported("2026-03-02")})
        (self.root / "sentinel").write_bytes(b"unrelated synthetic bytes")
        for agency, action in ((self.a, ("--to", "Replacement")), (self.a, ("--clear",)),
                               (self.a, ("--to", "Zoë")), (self.b, ("--clear",))):
            with self.subTest(agency=agency, action=action):
                code, out, err = self.grown_command(("kit-assign", "--agency", agency, *action))
                self.assertEqual((code, out), (1, ""))
                self.assertIn(SIZE_ERROR, err)
                self.assertNotIn("Recorded", out + err)

    def test_grown_store_refuses_status_due_coverage_and_csv(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        csv_path = self.tmp / "report.csv"
        for arguments in (("kit-status",), ("kit-due", *existing.ASOF), ("kit-coverage",),
                          ("kit-coverage", "--csv")):
            with self.subTest(arguments=arguments):
                code, out, err = self.grown_command(arguments)
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn(SIZE_ERROR, err)
        for present in (False, True):
            with self.subTest(csv_exists=present):
                # CSV rejects an already-existing destination before loading owners.
                # Seed it after that check, at growth, to test no truncation on refusal.
                seed = (lambda: csv_path.write_bytes(b"existing csv sentinel\n")) if present else None
                code, out, err = self.grown_command(("kit-status", "--csv", csv_path), seed)
                self.assertEqual((code, out), (1, ""), err)
                self.assertIn(SIZE_ERROR, err)
                if present:
                    self.assertEqual(csv_path.read_bytes(), b"existing csv sentinel\n")
                else:
                    self.assertFalse(csv_path.exists())

    def test_today_grown_store_reports_one_unreadable_source_without_request_fallback(self):
        self.put_sent({self.a: existing.imported("2026-03-02")})
        agencies_path = self.kit / "agencies.json"
        agencies = json.loads(agencies_path.read_bytes())
        for agency in agencies:
            if agency["agency_id"] == self.d:
                agency.update(selected=True, verified=True, records_email="records@agency.synthetic.example",
                               verification={"source_url": "https://agency.synthetic.example/records",
                                             "fields": ["records_email"], "checked_on": "2025-01-01"})
        agencies_path.write_text(json.dumps(agencies), encoding="utf-8")
        for extra in ((), ("--owner", "Zoë")):
            with self.subTest(extra=extra):
                code, out, err = self.grown_command(("today", *existing.ASOF, "--json", *extra))
                self.assertEqual((code, err), (0, ""))
                items = json.loads(out)
                self.assertFalse(any(i["kind"].startswith("request_") for i in items))
                bad = [i for i in items if i["kind"] == "unreadable"]
                self.assertEqual(len(bad), 1)
                self.assertIn(SIZE_ERROR, bad[0]["what"])
                self.assertIn("stale_contact", {i["kind"] for i in items})

    def test_missing_normal_and_malformed_contracts_unchanged(self):
        before = snapshot(self.root)
        self.assertEqual(kit_assign.read_assignments(self.root), {})
        self.assertEqual(snapshot(self.root), before)
        self.store.write_bytes(self.payload())
        self.assertEqual(kit_assign.read_assignments(self.root), json.loads(self.payload())["assignments"])
        for raw, diagnostic in ((b"\xff", "not JSON, or a key is repeated"),
                                (b"{", "not JSON, or a key is repeated"),
                                (b'{"schema_version":1,"schema_version":1,"assignments":{}}',
                                 "not JSON, or a key is repeated"),
                                (b'{"schema_version":true,"assignments":{}}', "expected schema_version"),
                                (b'{"schema_version":1,"assignments":{"BAD KEY":"A"}}', "malformed"),
                                (b'{"schema_version":1,"assignments":{"ca-gone-pd":"A  B"}}', "malformed")):
            with self.subTest(raw=raw):
                self.store.write_bytes(raw)
                with self.assertRaisesRegex(ValueError, diagnostic):
                    kit_assign.read_assignments(self.root)
                self.assertEqual(self.store.read_bytes(), raw)
        self.store.unlink()
        self.store.mkdir()
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            kit_assign.read_assignments(self.root)
        self.store.rmdir()
        outside = self.tmp / "outside.json"
        outside.write_bytes(self.payload())
        self.store.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            kit_assign.read_assignments(self.root)
        self.assertEqual(outside.read_bytes(), self.payload())


if __name__ == "__main__":
    unittest.main()
