"""FA03: a discovered receipt declaration stays bound to the bytes it is hashed from.

``mail_adoption.discover`` parses each receipt with the real intake parser
(``mail_delta.load_receipt``) and then reads the receipt again for its sha256.
These synthetic tests change the receipt between, and during, those reads and
require one fixed ``receipt_changed`` gap instead of an item whose declaration
and ``receipt_sha256`` describe different bytes.

Fixtures reuse the existing producers: ``legacy_export`` (legacy receipts) and
``eml_export.export_message`` (exact mail-wire receipts). No mailbox, network,
live path or private record is used; everything lives in a temporary directory.
Seams wrap the real parser and opener in memory only to change synthetic files
at a deterministic point; production code is never edited or replaced.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import FunctionType, SimpleNamespace
import unittest
from unittest import mock

from campaign_tool.records import mail_adoption
from campaign_tool.records.intake import eml_export, folder as intake_folder, mail_delta
from campaign_tool.records.run import Pipeline

from tests.records.test_export_bridge import legacy_export
from tests.records.test_mail_wire_intake import chain

REAL_LOAD = mail_delta.load_receipt
REAL_OPEN = intake_folder.secure_open


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_inplace(path, data, keep_mtime=None):
    """Overwrite the same inode (no truncate unless shorter data is written)."""
    fd = os.open(path, os.O_WRONLY)
    try:
        os.pwrite(fd, data, 0)
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)
    if keep_mtime is not None:
        os.utime(path, ns=keep_mtime)


def replace_file(path, data):
    """Replace the path with a new inode holding ``data``."""
    tmp = Path(str(path) + ".swap")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(data)
    os.replace(tmp, path)


def tree(base):
    result = {}
    for root, dirs, files in os.walk(base):
        for name in files:
            path = Path(root) / name
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                result[str(path)] = (info.st_ino, info.st_size, sha(path.read_bytes()))
            else:
                result[str(path)] = (info.st_ino, stat.S_IFMT(info.st_mode))
    return result


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = Pipeline(self.base / "processing").root.path
        self.export = self.base / "mail-history"
        self.export.mkdir(mode=0o700)
        self.loads, self.opens = [], []

    # -- fixtures -----------------------------------------------------------
    def legacy(self, uid):
        path, receipt = legacy_export(self.export, uid=uid)
        return path, receipt

    def wire(self, uid, name=None):
        raw, _, _, _ = chain()
        produced = eml_export.export_message(raw, mail_root=self.export, account="synthetic-fa03-account",
                                             mailbox="FA03", uidvalidity=900, uid=uid)
        target = self.export / "receipts" / (name or "wire-%d.json" % uid)
        target.parent.mkdir(mode=0o700, exist_ok=True)
        data = Path(produced).read_bytes()
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        return target, json.loads(data)

    def discover(self):
        return mail_adoption.discover(self.export, self.root / "intake")

    # -- seams --------------------------------------------------------------
    def after_parse(self, target, action):
        """Run ``action`` right after the real parser read ``target``."""
        def load(path):
            self.loads.append(str(path))
            result = REAL_LOAD(path)
            if str(path) == str(target):
                action()
            return result
        return mock.patch.object(mail_delta, "load_receipt", load)

    def around_parse(self, target, before, after):
        def load(path):
            self.loads.append(str(path))
            if str(path) == str(target):
                before()
            result = REAL_LOAD(path)
            if str(path) == str(target):
                after()
            return result
        return mock.patch.object(mail_delta, "load_receipt", load)

    def counting_open(self, target=None, during_read=None):
        """Count opens; optionally run ``during_read`` inside the second read of ``target``."""
        test = self

        class Stream:
            def __init__(self, inner):
                self.inner = inner

            def fileno(self):
                return self.inner.fileno()

            def read(self, size=-1):
                data = self.inner.read(size)
                during_read()
                return data

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.inner.close()
                return False

        def opener(path):
            test.opens.append(str(path))
            stream = REAL_OPEN(path)
            if (during_read is not None and str(path) == str(target)
                    and test.opens.count(str(path)) == 2):
                return Stream(stream)
            return stream
        return mock.patch.object(intake_folder, "secure_open", opener)

    def assert_bound(self, item, path):
        data = Path(path).read_bytes()
        self.assertEqual(item["receipt_sha256"], sha(data))
        self.assertEqual(item["declaration"], json.loads(data))


class Regression(_Base):
    # -- positive and restart/boundary contract ------------------------------
    def test_stable_legacy_and_wire_receipts_bind_declaration_to_hashed_bytes(self):
        legacy41, _ = self.legacy(41)
        legacy42, _ = self.legacy(42)
        wire, receipt = self.wire(11)
        self.assertEqual(receipt["schema"], "mail-wire-receipt-v2")
        before = tree(self.base)
        found, gaps = self.discover()
        self.assertEqual(dict(gaps), {})
        self.assertEqual(len(found), 3)
        by_path = {str(item["path"]): item for item in found}
        for path in (legacy41, legacy42, wire):
            self.assert_bound(by_path[str(path)], path)
        self.assertEqual(by_path[str(wire)]["declaration"]["schema"], "mail-wire-receipt-v2")
        self.assertEqual(by_path[str(wire)]["identity"], ("synthetic-fa03-account", "FA03", "900", "11"))
        # Restart: a second discovery over the same bytes is identical.
        again, gaps_again = self.discover()
        self.assertEqual((again, gaps_again), (found, gaps))
        self.assertEqual(tree(self.base), before)

    def test_stable_duplicate_identity_keeps_first_and_counts_duplicate(self):
        first, receipt = self.legacy(41)
        second = first.parent / ("zz-" + first.name)
        data = first.read_bytes()
        fd = os.open(second, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        found, gaps = self.discover()
        self.assertEqual(dict(gaps), {"duplicate_receipt_identity": 1})
        self.assertEqual([str(item["path"]) for item in found], [str(first)])
        self.assert_bound(found[0], first)

    def test_ordinary_gap_counters_are_unchanged(self):
        path, receipt = self.legacy(41)
        self.legacy(42)
        bad = dict(receipt, complete=False)
        replace_file(path, json.dumps(bad).encode())
        broken = path.parent / "broken.json"
        fd = os.open(broken, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(b"{not json")
        found, gaps = self.discover()
        self.assertEqual(dict(gaps), {"incomplete_receipt": 1, "invalid_receipt_file": 1})
        self.assertEqual(len(found), 1)

    def test_whitespace_only_replacement_with_same_typed_declaration_is_positive(self):
        path, receipt = self.legacy(41)
        relaid = (json.dumps(receipt, indent=3, sort_keys=False) + "\n\n").encode()
        self.assertNotEqual(relaid, path.read_bytes())
        with self.after_parse(path, lambda: replace_file(path, relaid)):
            found, gaps = self.discover()
        self.assertEqual(dict(gaps), {})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["receipt_sha256"], sha(relaid))
        self.assertEqual(found[0]["declaration"], receipt)

    # -- negative and preservation contract ----------------------------------
    def assert_changed(self, path, gaps, found, peers=()):
        self.assertEqual(gaps["receipt_changed"], 1, dict(gaps))
        self.assertNotIn(str(path), [str(item["path"]) for item in found])
        # No automatic second attempt: one parse and at most one hash read.
        self.assertEqual(self.loads.count(str(path)), 1)
        self.assertLessEqual(self.opens.count(str(path)), 2)
        found_paths = {str(item["path"]): item for item in found}
        for peer in peers:
            self.assert_bound(found_paths[str(peer)], peer)

    def test_same_length_different_declaration_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        peer, _ = self.legacy(42)
        data = path.read_bytes()
        changed = data.replace(b'"uid": 41', b'"uid": 49')
        self.assertEqual(len(changed), len(data))
        self.assertNotEqual(changed, data)
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, changed)):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found, peers=(peer,))
        self.assertEqual(dict(gaps), {"receipt_changed": 1})

    def test_equal_size_same_inode_write_with_restored_mtime_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        peer, _ = self.legacy(42)
        data = path.read_bytes()
        info = path.stat()
        inode = info.st_ino
        changed = data.replace(b'"Records"', b'"Recordz"')
        self.assertEqual(len(changed), len(data))

        def write():
            write_inplace(path, changed, keep_mtime=(info.st_atime_ns, info.st_mtime_ns))
        with self.counting_open(), self.after_parse(path, write):
            found, gaps = self.discover()
        self.assertEqual(path.stat().st_ino, inode)
        self.assertEqual((path.stat().st_size, path.stat().st_mtime_ns), (info.st_size, info.st_mtime_ns))
        self.assert_changed(path, gaps, found, peers=(peer,))

    def test_path_replacement_with_different_content_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        peer, _ = self.legacy(42)
        other = json.dumps(dict(receipt, internaldate="2026-02-03T04:05:06Z"), sort_keys=True).encode()
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, other)):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found, peers=(peer,))

    def test_type_only_change_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        data = path.read_bytes()
        changed = data.replace(b'"uidvalidity": 900', b'"uidvalidity": 900.0')
        self.assertNotEqual(changed, data)
        self.assertEqual(json.loads(changed), json.loads(data))  # equal under Python ==
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, changed)):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)

    def test_nested_json_type_and_array_order_changes_are_refused(self):
        path, receipt = self.legacy(41)
        # Extra JSON fields are accepted by the unchanged real legacy parser.
        receipt["synthetic_metadata"] = {"values": [1, True, None, {"n": 2}]}
        original = json.dumps(receipt).encode()
        for values in ([True, True, None, {"n": 2}],
                       [1.0, True, None, {"n": 2}],
                       [1, True, None, {"n": 2.0}],
                       [True, 1, None, {"n": 2}]):
            with self.subTest(values=values):
                replace_file(path, original)
                self.loads.clear()
                self.opens.clear()
                changed = dict(receipt, synthetic_metadata={"values": values})
                with self.counting_open(), self.after_parse(
                        path, lambda: replace_file(path, json.dumps(changed).encode())):
                    found, gaps = self.discover()
                self.assert_changed(path, gaps, found)
                self.assertEqual(dict(gaps), {"receipt_changed": 1})

    def test_invalid_json_in_second_read_is_receipt_changed(self):
        path, _ = self.legacy(41)
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, b"{invalid")):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)
        self.assertEqual(dict(gaps), {"receipt_changed": 1})

    def test_equal_size_same_inode_change_during_read_is_refused(self):
        path, _ = self.legacy(41)
        raw, info = path.read_bytes(), path.stat()
        changed = raw.replace(b'"Records"', b'"Recordz"')
        with self.counting_open(path, lambda: write_inplace(
                path, changed, keep_mtime=(info.st_atime_ns, info.st_mtime_ns))), \
                self.after_parse(path, lambda: None):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)
        self.assertEqual(path.stat().st_ino, info.st_ino)
        self.assertEqual(path.stat().st_size, info.st_size)
        self.assertEqual(path.stat().st_mtime_ns, info.st_mtime_ns)

    def test_path_replaced_during_read_even_with_same_bytes_is_refused(self):
        path, _ = self.legacy(41)
        raw = path.read_bytes()
        with self.counting_open(path, lambda: replace_file(path, raw)), \
                self.after_parse(path, lambda: None):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)
        self.assertEqual(dict(gaps), {"receipt_changed": 1})

    def test_a_to_b_to_a_parser_hash_mismatch_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        peer, _ = self.legacy(42)
        original = path.read_bytes()
        b_bytes = json.dumps(dict(receipt, folder="Elsewhere"), sort_keys=True).encode()
        with self.counting_open(), self.around_parse(path, lambda: replace_file(path, b_bytes),
                                                     lambda: replace_file(path, original)):
            found, gaps = self.discover()
        self.assertEqual(path.read_bytes(), original)
        self.assert_changed(path, gaps, found, peers=(peer,))

    def test_change_during_hash_read_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        peer, _ = self.legacy(42)
        data = path.read_bytes()

        def grow():
            with open(path, "ab") as out:
                out.write(b" ")
            info = path.stat()
            os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
        with self.counting_open(path, grow), self.after_parse(path, lambda: None):
            found, gaps = self.discover()
        self.assertEqual(self.opens.count(str(path)), 2)
        self.assert_changed(path, gaps, found, peers=(peer,))
        self.assertEqual(path.read_bytes(), data + b" ")

    def test_changed_receipt_does_not_consume_identity(self):
        path, receipt = self.legacy(41)
        twin = path.parent / ("zz-" + path.name)
        data = path.read_bytes()
        fd = os.open(twin, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
        other = json.dumps(dict(receipt, internaldate="2026-02-03T04:05:06Z"), sort_keys=True).encode()
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, other)):
            found, gaps = self.discover()
        self.assertEqual(dict(gaps), {"receipt_changed": 1})
        self.assertEqual([str(item["path"]) for item in found], [str(twin)])
        self.assert_bound(found[0], twin)

    def test_changed_wire_receipt_is_receipt_changed(self):
        legacy, _ = self.legacy(41)
        path, receipt = self.wire(11)
        other = json.dumps(dict(receipt, budget=dict(receipt["budget"], synthetic_drift=1)),
                           sort_keys=True).encode()
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, other)):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found, peers=(legacy,))

    def test_oversized_replacement_keeps_receipt_size_limit(self):
        path, receipt = self.legacy(41)
        peer, _ = self.legacy(42)
        big = json.dumps(dict(receipt, pad="x" * (mail_delta.MAX_RECEIPT + 10))).encode()
        with self.counting_open(), self.after_parse(path, lambda: replace_file(path, big)):
            found, gaps = self.discover()
        self.assertEqual(dict(gaps), {"receipt_size_limit": 1})
        self.assertEqual([str(item["path"]) for item in found], [str(peer)])

    def test_oversized_receipt_keeps_receipt_size_limit(self):
        path, receipt = self.legacy(41)
        replace_file(path, json.dumps(dict(receipt, pad="x" * (mail_delta.MAX_RECEIPT + 10))).encode())
        found, gaps = self.discover()
        self.assertEqual((found, dict(gaps)), ([], {"receipt_size_limit": 1}))

    def test_deleted_between_reads_is_receipt_changed(self):
        path, receipt = self.legacy(41)
        with self.counting_open(), self.after_parse(path, lambda: os.unlink(path)):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)
        self.assertEqual(self.opens.count(str(path)), 1)

    # -- symlink and FIFO controls (never read their targets, never block) ---
    def test_symlink_receipt_is_refused_without_reading_target(self):
        target, _ = self.legacy(41)
        moved = self.base / "outside.json"
        os.replace(target, moved)
        os.symlink(moved, target)
        with self.counting_open(), self.after_parse(None, lambda: None):
            found, gaps = self.discover()
        self.assertEqual((found, dict(gaps)), ([], {"receipt_not_regular_file": 1}))
        self.assertEqual((self.loads, self.opens), ([], []))

    def test_symlink_swapped_in_after_parse_is_refused_without_reading_target(self):
        path, receipt = self.legacy(41)
        decoy = self.base / "decoy.json"
        decoy.write_bytes(path.read_bytes())

        def swap():
            os.unlink(path)
            os.symlink(decoy, path)
        with self.counting_open(), self.after_parse(path, swap):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)
        self.assertEqual(self.opens.count(str(path)), 1)

    def test_fifo_receipt_is_refused_without_open(self):
        path, _ = self.legacy(41)
        os.unlink(path)
        os.mkfifo(path, 0o600)
        # Hold a non-blocking writer so even an erroneous open could never block.
        holder = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.addCleanup(os.close, holder)
        with self.counting_open(), self.after_parse(None, lambda: None):
            found, gaps = self.discover()
        self.assertEqual((found, dict(gaps)), ([], {"receipt_not_regular_file": 1}))
        self.assertEqual((self.loads, self.opens), ([], []))

    def test_fifo_swapped_in_after_parse_is_refused_without_open(self):
        path, _ = self.legacy(41)
        holders = []

        def swap():
            os.unlink(path)
            os.mkfifo(path, 0o600)
            holders.append(os.open(path, os.O_RDWR | os.O_NONBLOCK))
        self.addCleanup(lambda: [os.close(fd) for fd in holders])
        with self.counting_open(), self.after_parse(path, swap):
            found, gaps = self.discover()
        self.assert_changed(path, gaps, found)
        self.assertEqual(self.opens.count(str(path)), 1)


class GateControl(unittest.TestCase):
    """Synthetic control: without the declaration comparison the regression must fail."""

    TARGET = "test_path_replacement_with_different_content_is_receipt_changed"
    POSITIVE = "test_stable_legacy_and_wire_receipts_bind_declaration_to_hashed_bytes"

    def run_case(self, name, discover=None, label="restored"):
        case = Regression(name)
        if discover is not None:
            case.discover = lambda: discover(case.export, case.root / "intake")
        output = io.StringIO()
        result = unittest.TextTestRunner(stream=output, verbosity=2).run(case)
        print("CONTROL %s\n%s" % (label, output.getvalue()))
        return result

    def test_disabled_comparison_is_detected_and_restored_positive_passes(self):
        # A detached synthetic backend uses the actual function code, with its
        # own globals. Only canonical comparison is disabled. Never patch the
        # imported production gate (the regression's file-change seams remain
        # restricted to generated fixtures).
        helper = mail_adoption._bound_receipt_bytes
        helper_globals = dict(helper.__globals__)
        helper_globals["json"] = SimpleNamespace(loads=json.loads, dumps=lambda *a, **k: "disabled")
        broken_helper = FunctionType(helper.__code__, helper_globals, helper.__name__)
        discover_globals = dict(mail_adoption.discover.__globals__)
        discover_globals["_bound_receipt_bytes"] = broken_helper
        synthetic_discover = FunctionType(mail_adoption.discover.__code__, discover_globals, "synthetic_discover")
        broken = self.run_case(self.TARGET, synthetic_discover, "comparison-disabled")
        self.assertIs(mail_adoption._bound_receipt_bytes, helper)
        self.assertEqual((broken.testsRun, len(broken.errors), len(broken.skipped)), (1, 0, 0))
        self.assertEqual(len(broken.failures), 1, "disabled comparison was not detected")
        self.assertIn("receipt_changed", broken.failures[0][1])
        restored = self.run_case(self.TARGET)
        self.assertTrue(restored.wasSuccessful(), restored.failures + restored.errors)
        positive = self.run_case(self.POSITIVE)
        self.assertTrue(positive.wasSuccessful(), positive.failures + positive.errors)
        self.assertEqual((restored.testsRun, positive.testsRun), (1, 1))


if __name__ == "__main__":
    unittest.main()
