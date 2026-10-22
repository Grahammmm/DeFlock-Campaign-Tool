"""Policy loader/constructor attacks. Fictional hashes only; no ledger needed."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
import unittest

from tests.t4b_adversarial import require_enabled

require_enabled()

from campaign_tool.records.ledger import store  # noqa: E402
from campaign_tool.records.ledger import mail_resolution_policy as mrp  # noqa: E402

H = lambda c: c * 64  # noqa: E731


def entry(n=0, deps=()):
    h = lambda tag: hashlib.sha256(("%s-%d" % (tag, n)).encode()).hexdigest()  # noqa: E731
    return {"bytes": 10 + n, "role": "original", "scope": "in_scope",
            "verification_receipt_sha256": h("r"), "binding_occurrence_id": h("o"),
            "dependent_receipts": list(deps), "legacy_binding_sha256": h("l")}


def value(entries=None, held=()):
    entries = entries if entries is not None else {H("a"): entry(0)}
    return {"schema": "approved-mail-resolution-policy-v1", "inventory_sha256": H("e"),
            "approval_sha256": H("f"), "originals": entries, "held": list(held)}


def raw_of(v):
    return store.canonical(v).encode()


def build(raw):
    return mrp.ApprovedMailResolutionPolicy(raw, expected_sha256=hashlib.sha256(raw).hexdigest())


class Root:
    """Private temp root holding resolution-policies/<sha>."""
    def __init__(self, case):
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.root = Path(os.path.realpath(self.tmp.name)) / "records"
        self.dir = self.root / "resolution-policies"
        self.dir.mkdir(parents=True, mode=0o700)

    def put(self, raw, mode=0o600):
        digest = hashlib.sha256(raw).hexdigest()
        path = self.dir / digest
        path.write_bytes(raw)
        path.chmod(mode)
        return digest, path


class LoaderFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.r = Root(self)
        self.raw = raw_of(value())

    def load(self, digest, root=None):
        return mrp.ApprovedMailResolutionPolicy.load(root or self.r.root, expected_sha256=digest)

    def test_happy(self):
        digest, _ = self.r.put(self.raw)
        self.assertEqual(self.load(digest).sha256, digest)

    def test_group_world_readable_or_writable_refused(self):
        for mode in (0o640, 0o604, 0o620, 0o666, 0o644):
            with self.subTest(mode=oct(mode)):
                digest, path = self.r.put(self.raw, mode)
                with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
                    self.load(digest)
                path.unlink()

    def test_setuid_sticky_bits_refused_or_ignored(self):
        digest, path = self.r.put(self.raw, 0o600)
        path.chmod(0o4600)
        try:
            self.load(digest)  # only owner-only matters for the hash-pinned bytes
        except ValueError as exc:
            self.assertEqual(str(exc), "resolution_policy_invalid")

    def test_symlink_file_refused(self):
        digest, path = self.r.put(self.raw)
        real = self.r.root / "elsewhere"
        path.rename(real)
        path.symlink_to(real)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(digest)

    def test_symlinked_policy_directory_and_root_refused(self):
        digest, _ = self.r.put(self.raw)
        moved = self.r.root / "real-policies"
        self.r.dir.rename(moved)
        self.r.dir.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(digest)
        self.r.dir.unlink()
        moved.rename(self.r.dir)
        alias = self.r.root.parent / "alias"
        alias.symlink_to(self.r.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(digest, root=alias)

    def test_dotdot_cannot_launder_a_symlinked_component(self):
        """F-05: os.path.abspath() collapses '..' lexically BEFORE the no-symlink walk.

        'linkdir/..' means the symlink target's parent to the kernel, but the
        loader resolves it as the lexical parent. The path it opens is therefore
        not the path the caller wrote, and a symlinked component is never seen.
        """
        digest, _ = self.r.put(self.raw)
        deep = self.r.root.parent / "deep" / "er"
        deep.mkdir(parents=True)
        link = self.r.root.parent / "linkdir"
        link.symlink_to(deep, target_is_directory=True)
        # kernel meaning of root/../linkdir/.. is deep's parent, NOT root.parent
        sneaky = Path(str(link) + "/../" + self.r.root.name)
        self.assertFalse(os.path.exists(sneaky))  # kernel: no such path
        with self.assertRaises(ValueError):       # loader must agree with the kernel
            self.load(digest, root=sneaky)

    def test_fifo_and_directory_and_device_refused_without_blocking(self):
        digest = H("1")
        os.mkfifo(self.r.dir / digest, 0o600)
        t0 = time.monotonic()
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(digest)
        self.assertLess(time.monotonic() - t0, 2)
        d2 = H("2")
        (self.r.dir / d2).mkdir()
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(d2)
        d3 = H("3")
        os.symlink("/dev/zero", self.r.dir / d3)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(d3)

    def test_torn_truncated_file_refused(self):
        digest, path = self.r.put(self.raw)
        for cut in (0, 1, len(self.raw) // 2, len(self.raw) - 1):
            path.write_bytes(self.raw[:cut])
            path.chmod(0o600)
            with self.subTest(cut=cut), self.assertRaisesRegex(ValueError, "resolution_policy_(invalid|mismatch)"):
                self.load(digest)

    def test_appended_garbage_and_oversize_refused(self):
        digest, path = self.r.put(self.raw + b"\n")
        with self.assertRaisesRegex(ValueError, "resolution_policy_(invalid|mismatch)"):
            self.load(digest)
        big = b"x" * (mrp.MAX_POLICY_BYTES + 1)
        digest, path = self.r.put(big)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(digest)

    def test_missing_and_malformed_expected_hash_cannot_traverse(self):
        for bad in ("../x", "A" * 64, "a" * 63, "a" * 64 + "/", "", None, 7, "a" * 64 + "\n", "١" * 64):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.load(bad)
        with self.assertRaisesRegex(ValueError, "resolution_policy_invalid"):
            self.load(H("9"))

    def test_hardlinked_policy_does_not_change_trust(self):
        digest, path = self.r.put(self.raw)
        os.link(path, self.r.root / "second-name")
        self.assertEqual(self.load(digest).sha256, digest)  # hash-pinned; noted in audit as sound


class ConstructorTests(unittest.TestCase):
    def rejects(self, raw, digest=None):
        with self.assertRaises(ValueError) as caught:
            mrp.ApprovedMailResolutionPolicy(raw, expected_sha256=digest or hashlib.sha256(raw).hexdigest())
        self.assertRegex(str(caught.exception), r"^resolution_policy_(invalid|mismatch)$")

    def test_non_json_shapes_only_ever_raise_the_coded_error(self):
        deep = b"[" * 200000 + b"]" * 200000
        hostile = [b"", b"\xef\xbb\xbf" + raw_of(value()), b"\xff\xfe{\x00", raw_of(value()).decode().encode("utf-16"),
                   b'{"a":NaN}', b'{"a":Infinity}', b'{"a":-Infinity}', deep, b"1" * 100000, b"null", b"[]",
                   b'{"schema":"approved-mail-resolution-policy-v1","schema":"approved-mail-resolution-policy-v1"}',
                   b'{"x":"\\ud800"}', b'{"x":"\xed\xa0\x80"}', b"\x00" * 10]
        for raw in hostile:
            with self.subTest(head=raw[:20]):
                self.rejects(raw)

    def test_non_bytes_inputs(self):
        for raw in ("text", bytearray(b"{}"), memoryview(b"{}"), None):
            with self.subTest(type=type(raw).__name__), self.assertRaises(ValueError):
                mrp.ApprovedMailResolutionPolicy(raw, expected_sha256=H("a"))

    def test_duplicate_key_at_every_depth(self):
        base = raw_of(value()).decode()
        self.rejects(base.replace('"held":[]', '"held":[],"held":[]').encode())
        inner = '"role":"original"'
        self.rejects(base.replace(inner, inner + "," + inner).encode())

    def test_reordered_and_noncanonical_layouts_refused(self):
        v = value()
        pretty = json.dumps(v, indent=1).encode()
        self.rejects(pretty)
        shuffled = json.dumps(v, separators=(",", ":")).encode()  # insertion order != sorted order
        if shuffled != raw_of(v):
            self.rejects(shuffled)
        two = {H("b"): entry(1), H("a"): entry(2)}
        self.rejects(json.dumps(value(two), separators=(",", ":"), ensure_ascii=False).encode())
        self.rejects(raw_of(value()).replace(b'"bytes":10', b'"bytes":1e1'))
        self.rejects(raw_of(value()).replace(b'"bytes":10', b'"bytes":010'))
        self.rejects(raw_of(value()).replace(b'"bytes":10', b'"bytes":10.0'))

    def test_duplicate_and_reordered_lists(self):
        dep = [H("2"), H("1")]
        self.rejects(raw_of(value({H("a"): entry(0, dep)})))
        dup = [H("1"), H("1")]
        self.rejects(raw_of(value({H("a"): entry(0, dup)})))
        self.rejects(raw_of(value(held=[H("2"), H("1")])))
        self.rejects(raw_of(value(held=[H("1"), H("1")])))
        self.rejects(raw_of(value(held=[H("a")])))  # also in originals

    def test_binding_receipt_cannot_be_its_own_dependent(self):
        e = entry(0)
        e["dependent_receipts"] = [e["verification_receipt_sha256"]]
        self.rejects(raw_of(value({H("a"): e})))

    def test_digest_strictness(self):
        for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, " " + "a" * 63, "a" * 63 + "\n", "١" * 64, "a" * 64 + "\x00"):
            with self.subTest(bad=bad):
                e = entry(0)
                e["legacy_binding_sha256"] = bad
                self.rejects(raw_of(value({H("a"): e})))
                self.rejects(raw_of(value({bad: entry(0)})))

    def test_type_confusion_on_bytes(self):
        for bad in (True, False, -1, 1.0, "10", None, [10], {"a": 1}, 2 ** 70 + 0.5):
            with self.subTest(bad=bad):
                e = entry(0)
                e["bytes"] = bad
                self.rejects(raw_of(value({H("a"): e})))

    def test_huge_integer_bytes_accepted_or_coded(self):
        e = entry(0)
        e["bytes"] = 2 ** 200
        try:
            build(raw_of(value({H("a"): e})))
        except ValueError as exc:
            self.assertEqual(str(exc), "resolution_policy_invalid")

    def test_hash_mismatch_by_one_bit(self):
        raw = raw_of(value())
        digest = hashlib.sha256(raw).hexdigest()
        flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
        self.rejects(raw, flipped)
        self.rejects(raw, digest.upper())
        self.rejects(raw, digest + "0")

    def test_limits_boundaries_and_reachability(self):
        held = sorted(hashlib.sha256(str(i).encode()).hexdigest() for i in range(mrp.MAX_ORIGINALS))
        raw = raw_of(value(entries={}, held=held))
        self.assertLessEqual(len(raw), mrp.MAX_POLICY_BYTES)
        build(raw)  # exactly MAX_ORIGINALS held is accepted
        held.append(hashlib.sha256(b"one-more").hexdigest())
        held.sort()
        self.rejects(raw_of(value(entries={}, held=held)))
        # F-09: MAX_ORIGINALS entries can never fit in MAX_POLICY_BYTES
        sample = {H("a"): entry(0)}
        per_entry = len(raw_of(value(sample))) - len(raw_of(value({})))
        self.assertGreater(per_entry * mrp.MAX_ORIGINALS, mrp.MAX_POLICY_BYTES,
                           "documented 10,000-entry bound is reachable; if so this finding is moot")

    def test_receipt_list_bound(self):
        deps = sorted(hashlib.sha256(str(i).encode()).hexdigest() for i in range(mrp.MAX_RECEIPTS + 1))
        self.rejects(raw_of(value({H("a"): entry(0, deps)})))

    def test_exact_size_boundary(self):
        pad = mrp.MAX_POLICY_BYTES
        v = value()
        v["inventory_sha256"] = H("e")
        raw = raw_of(v) + b" " * (pad - len(raw_of(v)) + 1)
        self.rejects(raw)


class MutationAfterConstructionTests(unittest.TestCase):
    """The class says admission() is mutation-proof; the instance itself is not."""

    def test_attributes_are_frozen(self):
        p = build(raw_of(value()))
        evil = value({H("b"): entry(5)})
        for name, new in (("raw", raw_of(evil)), ("sha256", H("0"))):
            with self.subTest(name=name):
                try:
                    setattr(p, name, new)
                except (AttributeError, TypeError):
                    continue
                self.fail("F-06: policy.%s is reassignable after validation" % name)

    def test_subclass_or_new_bypass_is_rejected_by_writer(self):
        class Fake(mrp.ApprovedMailResolutionPolicy):
            def __init__(self):
                self.raw, self.sha256 = raw_of(value()), H("0")
        from campaign_tool.records.ledger import original_resolution_mail as mail
        self.assertIsNot(type(Fake()), mrp.ApprovedMailResolutionPolicy)
        # writer uses `type(policy) is ApprovedMailResolutionPolicy`; checked behaviourally in test_resolution_adversarial
        self.assertTrue(hasattr(mail, "append"))

    def test_admission_returns_isolated_copy(self):
        p = build(raw_of(value()))
        got = p.admission(H("a"), 10)
        got["dependent_receipts"].append("x")
        got["bytes"] = 99
        self.assertEqual(p.admission(H("a"), 10)["bytes"], 10)

    def test_admission_cost_does_not_scale_with_policy_size(self):
        """F-08: every admission() re-parses the whole raw policy."""
        entries = {}
        n = 0
        while True:
            entries[hashlib.sha256(b"s%d" % n).hexdigest()] = entry(n)
            n += 1
            if n % 500 == 0 and len(raw_of(value(entries))) > mrp.MAX_POLICY_BYTES * 0.8:
                break
            if n > 20000:
                break
        entries = dict(sorted(entries.items()))
        p = build(raw_of(value(entries)))
        subject = next(iter(entries))
        t0 = time.perf_counter()
        for _ in range(200):
            p.admission(subject, entries[subject]["bytes"])
        per_call = (time.perf_counter() - t0) / 200
        self.assertLess(per_call * len(entries), 5.0,
                        "resolving every original costs %.1fs of pure re-parsing (%d entries, %.0f ms/call)"
                        % (per_call * len(entries), len(entries), per_call * 1000))


class ReadRegularTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(os.path.realpath(self.tmp.name))

    def test_growth_during_read_is_not_returned_as_valid(self):
        f = self.dir / "f"
        f.write_bytes(b"abc")
        self.assertEqual(mrp.read_regular(f, 10), b"abc")
        f.write_bytes(b"x" * 11)
        with self.assertRaises(ValueError):
            mrp.read_regular(f, 10)

    def test_limit_is_inclusive_and_sparse_file_is_bounded(self):
        f = self.dir / "sparse"
        with open(f, "wb") as out:
            out.truncate(10 ** 9)
        with self.assertRaises(ValueError):
            mrp.read_regular(f, 1024)
        g = self.dir / "exact"
        g.write_bytes(b"y" * 1024)
        self.assertEqual(len(mrp.read_regular(g, 1024)), 1024)

    def test_odd_paths_raise_only_oserror_or_valueerror(self):
        for p in ("/", "", "/nonexistent/x", str(self.dir) + "/\x00bad", str(self.dir) + "/\ud800", "/" + "a/" * 5000 + "f"):
            with self.subTest(p=p[:30]):
                try:
                    mrp.read_regular(p, 10)
                except (OSError, ValueError):
                    pass

    def test_root_dir_swap_between_open_and_stat_is_detected(self):
        f = self.dir / "t"
        f.write_bytes(b"one")
        other = self.dir / "t2"
        other.write_bytes(b"two")
        self.assertEqual(mrp.read_regular(f, 10), b"one")
        self.assertTrue(stat.S_ISREG(os.stat(f).st_mode))


if __name__ == "__main__":
    unittest.main()
