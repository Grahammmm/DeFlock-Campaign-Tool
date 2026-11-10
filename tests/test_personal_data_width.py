"""RF1005-11: bounded enumeration, using only synthetic private trees."""
import hashlib
import json
import os
import unittest
from unittest import mock

from campaign_tool import personal_data as pd
from tests import test_personal_data as fixtures

CALLS = (pd.personal_data_register, lambda root: pd.personal_data_find(root, "findme"))
MODES = ((), ("--json",), ("--find", "findme"), ("--find", "findme", "--json"))
REFUSAL = "Stopped: personal-data inventory incomplete; review private folders locally\n"


class PoisonEntry:
    @property
    def name(self):
        raise AssertionError("overflow entry name accessed")

    def stat(self, **kwargs):
        raise AssertionError("overflow entry classified")


class ScannerOs:
    """Forward real descriptor-relative operations, tracking scanner-owned resources only."""
    def __init__(self, guard=None, fail=None, poison=False, open_error=None):
        self.guard, self.fail, self.poison, self.open_error = guard, fail, poison, open_error
        self.yielded = 0
        self.live, self.opened, self.closed = set(), [], []
        self.scans, self.exits = [], []

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, name, flags, *args, **kwargs):
        fd = os.open(name, flags, *args, **kwargs)
        if fd in self.live:
            raise AssertionError("reused live descriptor")
        self.live.add(fd)
        self.opened.append(fd)
        return fd

    def close(self, fd):
        if fd not in self.live:
            raise AssertionError("closed unowned descriptor")
        self.live.remove(fd)
        self.closed.append(fd)
        os.close(fd)

    def scandir(self, fd):
        inode = os.fstat(fd).st_ino
        if inode == self.open_error:
            raise OSError("SYNTHETIC-PRIVATE-OPEN-DETAIL")
        real = os.scandir(fd)
        owner = self

        class Scan:
            def __enter__(self):
                real.__enter__()
                owner.scans.append(inode)
                self.count = 0
                return self

            def __exit__(self, *args):
                owner.exits.append(inode)
                return real.__exit__(*args)

            def __iter__(self):
                return self

            def __next__(self):
                if owner.guard is not None and owner.yielded >= owner.guard:
                    raise AssertionError("requested entry after overflow detector")
                if owner.fail == (inode, self.count):
                    raise OSError("SYNTHETIC-PRIVATE-NEXT-DETAIL")
                item = next(real)
                self.count += 1
                owner.yielded += 1
                if owner.poison and owner.yielded == owner.guard:
                    return PoisonEntry()
                return item

        return Scan()


def admitted_fixture(root):
    # Deliberately unsorted creation and prefix-vs-path ordering; all names synthetic.
    fixtures.put(root, "kit/assignments.json", {"schema_version": 1, "assignments": {"a": "Findme Synthetic"}})
    for name in ("z", "drafts/note", "drafts.txt", "é", "control\nname", "raw-\udcff", ".dot"):
        fixtures.put(root, "private/" + name, b"unknown synthetic bytes")
    (root / "private" / "link").symlink_to("z")
    os.mkfifo(root / "private" / "fifo")
    (root / "private" / "reviewers.json").mkdir()
    fixtures.put(root, "private/reviewers.json/ignored", b"not descended")


def report_digests(root):
    register = pd.personal_data_register(root)
    find = pd.personal_data_find(root, "findme")
    payloads = (json.dumps(register, sort_keys=True), pd.render_text(register),
                json.dumps(find, sort_keys=True), pd.render_find_text(find))
    return [hashlib.sha256(p.encode("utf-8")).hexdigest() for p in payloads]


class WidthTests(fixtures.Base):
    def assert_closed(self, scanner):
        self.assertEqual(scanner.live, set())
        self.assertEqual(len(scanner.opened), len(scanner.closed))
        self.assertEqual(scanner.scans, scanner.exits)

    def refused(self, call, root):
        with self.assertRaises(pd.PersonalDataError) as caught:
            call(root)
        self.assertEqual(str(caught.exception), pd.INCOMPLETE)

    def test_default_budget_stops_real_wide_directory_on_first_excess_entry(self):
        self.assertEqual(pd.MAX_ENTRIES, 10000)
        (self.root / "kit").mkdir()
        for index in range(10250):
            fixtures.put(self.root, f"private/f{index}", b"")
        before = fixtures.tree(self.root)
        for call in CALLS:
            scanner = ScannerOs()
            with mock.patch.object(pd, "os", scanner):
                self.refused(call, self.root)
            self.assertEqual(scanner.yielded, 10001)
            self.assert_closed(scanner)
        self.assertEqual(fixtures.tree(self.root), before)

    def test_guarded_iterator_never_touches_detector_or_requests_another_entry(self):
        for index in range(8):
            fixtures.put(self.root, f"kit/f{index}", b"x")
        for call in CALLS:
            scanner = ScannerOs(guard=4, poison=True)
            with mock.patch.object(pd, "MAX_ENTRIES", 3), mock.patch.object(pd, "os", scanner):
                self.refused(call, self.root)
            self.assertEqual(scanner.yielded, 4)
            self.assert_closed(scanner)

    def test_shared_roots_and_reserved_parent_siblings(self):
        for nested in (False, True):
            root = self.tmp / str(nested)
            for index in range(3):
                fixtures.put(root, f"kit/f{index}", b"x")
            # In nested case all three parent siblings reserve units before descent.
            prefix = "private/a/" if nested else "private/"
            for index in range(20):
                fixtures.put(root, prefix + f"f{index}", b"x")
            if nested:
                fixtures.put(root, "private/z", b"x")
                fixtures.put(root, "private/y", b"x")
            for call in CALLS:
                scanner = ScannerOs(guard=7 if nested else 6, poison=True)
                budget = 6 if nested else 5
                with mock.patch.object(pd, "MAX_ENTRIES", budget), mock.patch.object(pd, "os", scanner):
                    self.refused(call, root)
                self.assertEqual(scanner.yielded, budget + 1)
                self.assert_closed(scanner)

    def test_zero_one_exact_over_and_exhausted_empty_directories(self):
        cases = ((0, 0, 0, True), (0, 0, 1, False), (1, 1, 0, True),
                 (1, 1, 1, False), (5, 3, 2, True), (5, 3, 3, False))
        for index, (budget, kit_count, private_count, accepted) in enumerate(cases):
            root = self.tmp / f"boundary-{index}"
            for top, count in (("kit", kit_count), ("private", private_count)):
                (root / top).mkdir(parents=True)
                for num in range(count):
                    fixtures.put(root, f"{top}/f{num}", b"x")
            for call in CALLS:
                scanner = ScannerOs()
                with self.subTest(case=index, call=call), mock.patch.object(pd, "MAX_ENTRIES", budget), \
                        mock.patch.object(pd, "os", scanner):
                    if accepted:
                        call(root)
                    else:
                        self.refused(call, root)
                self.assertEqual(scanner.yielded, kit_count + private_count)
                self.assert_closed(scanner)
        root = self.tmp / "last-unit-empty-child"
        (root / "kit" / "empty").mkdir(parents=True)
        (root / "private").mkdir()
        for call in CALLS:
            with mock.patch.object(pd, "MAX_ENTRIES", 1):
                call(root)

    def test_cli_overflow_withholds_earlier_matches_and_private_details(self):
        fixtures.put(self.root, "kit/assignments.json", {
            "schema_version": 1, "assignments": {"synthetic-agency": "Findme VALUE-SENTINEL"}})
        for index in range(20):
            fixtures.put(self.root, f"private/BASENAME-SENTINEL-{index}", b"UNKNOWN-CONTENT")
        before = fixtures.tree(self.root)
        real_read = pd._read_bytes
        reads = []

        def read(fd, name, st, cap):
            self.assertEqual(name, "assignments.json", "unknown content opened")
            reads.append(name)
            return real_read(fd, name, st, cap)

        for extra in MODES:
            scanner = ScannerOs(guard=4)
            with mock.patch.object(pd, "MAX_ENTRIES", 3), mock.patch.object(pd, "os", scanner), \
                    mock.patch.object(pd, "_read_bytes", side_effect=read):
                self.assertEqual(fixtures.run("personal-data", "--directory", self.root, *extra), (1, "", REFUSAL))
            self.assertEqual(scanner.yielded, 4)
            self.assert_closed(scanner)
        self.assertEqual(reads, ["assignments.json"] * 4)
        self.assertEqual(fixtures.tree(self.root), before)

    def test_iterator_open_next_errors_and_overflow_close_resources_without_mutation(self):
        fixtures.put(self.root, "kit/first", b"earlier")
        for index in range(5):
            fixtures.put(self.root, f"private/a/f{index}", b"synthetic")
        before = fixtures.tree(self.root)
        for target in (self.root / "private", self.root / "private" / "a"):
            inode = target.stat().st_ino
            for exit_kind in ("success", "overflow", "open", 0, 2):
                for call in CALLS:
                    scanner = ScannerOs(fail=(inode, exit_kind) if isinstance(exit_kind, int) else None,
                                        open_error=inode if exit_kind == "open" else None)
                    budget = 3 if exit_kind == "overflow" else 20
                    unrelated = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        with self.subTest(target=target.name, exit_kind=exit_kind, call=call), \
                                mock.patch.object(pd, "os", scanner), mock.patch.object(pd, "MAX_ENTRIES", budget):
                            # The second root has one child, so next failure at 2 only applies in the child.
                            if exit_kind == "success" or (exit_kind == 2 and target.name == "private"):
                                call(self.root)
                            else:
                                self.refused(call, self.root)
                        os.fstat(unrelated)
                        self.assert_closed(scanner)
                    finally:
                        os.close(unrelated)
                    self.assertEqual(fixtures.tree(self.root), before)

    def test_admitted_bytes_order_and_entry_kinds_are_unchanged(self):
        admitted_fixture(self.root)
        before = fixtures.tree(self.root)
        # Captured on unmodified main 0d5b1c3b, not refreshed after the repair.
        expected = ["34a238090872c096483789061857579bebd83b39e982728cae2ef3ca4b6cb6b8",  # pragma: allowlist secret -- SHA-256 of synthetic report output, not a credential
                    "2b40205824b5af732146f0f0615ed84f7764b0d377cb9c822c349d61c5feed51",  # pragma: allowlist secret -- SHA-256 of synthetic report output, not a credential
                    "9ba3fdd3f6c7868bd0e5fd6eb66b2e1ce56b0d16685d9ed80fd337df0caecc76",  # pragma: allowlist secret -- SHA-256 of synthetic report output, not a credential
                    "a70d600f7f5673e7efca0fd583367a6241940332e7efe299b911cfd2071aefab"]  # pragma: allowlist secret -- SHA-256 of synthetic report output, not a credential
        with mock.patch.object(pd, "MAX_ENTRIES", 12):
            self.assertEqual(report_digests(self.root), expected)
            register = pd.personal_data_register(self.root)
            self.assertEqual([r["path"] for r in register["files"] if r["kind"] == "unknown"],
                             ["private/" + p for p in (".dot", "control\nname", "drafts.txt", "drafts/note",
                                                      "fifo", "link", "raw-\udcff", "z", "é")])
            find = pd.personal_data_find(self.root, "findme")
            self.assertEqual([(r["file_position"], r["record_position"]) for r in find["matches"]], [(1, [1])])
            for extra in MODES:
                code, _out, err = fixtures.run("personal-data", "--directory", self.root, *extra)
                self.assertEqual((code, err), (1, ""))  # catalog-shaped directory remains unreadable
        with mock.patch.object(pd, "MAX_ENTRIES", 11):
            for call in CALLS:
                self.refused(call, self.root)
        self.assertEqual(fixtures.tree(self.root), before)


if __name__ == "__main__":
    unittest.main()
