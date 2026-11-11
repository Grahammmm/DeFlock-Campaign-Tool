"""RF0421-4: a descriptor metadata or close OSError on one recognized personal-data store is an unreadable FILE.

Offline and synthetic: real files in a temporary tree; only ``personal_data.os`` is replaced by a forwarding proxy
that injects the fault on the descriptor actually opened for one named file.
Run from the repository root: python3 -m unittest tests.test_personal_data_io
"""
import errno
import json
import os
import unittest
from unittest import mock

from tests import test_personal_data as base  # module alias: no historical TestCase is re-exported here
from campaign_tool import personal_data

PRIVATE = "SECRET-IO-MESSAGE-Q9"
ROW_DEFAULT = {"record_count": None, "oldest_record_date": None, "newest_record_date": None}


class Proxy:
    """Forwards to the real os; injects ``fault`` on the descriptor opened as ``target`` (a basename)."""

    def __init__(self, target, fault, then_close_fault=False):
        self.target, self.fault, self.then_close_fault = target, fault, then_close_fault
        self.fd = None
        self.opens = self.reads = self.closes = self.fstats = 0
        self.reused = None

    def __getattr__(self, name):
        return getattr(os, name)

    def open(self, name, *args, **kwargs):
        if name == self.target and self.fault == "open":
            raise PermissionError(errno.EACCES, PRIVATE)
        fd = os.open(name, *args, **kwargs)
        if name == self.target:
            self.fd, self.opens = fd, self.opens + 1
        return fd

    def fstat(self, fd):
        if fd == self.fd:
            self.fstats += 1
            if self.fault == "fstat":
                raise OSError(errno.EIO, PRIVATE)
        return os.fstat(fd)

    def read(self, fd, size):
        if fd != self.fd:
            return os.read(fd, size)
        self.reads += 1
        if self.fault == "read":
            raise OSError(errno.EIO, PRIVATE)
        if self.fault == "late_read":  # real short chunks first, then the failure
            if self.reads > 2:
                raise OSError(errno.EIO, PRIVATE)
            return os.read(fd, 5)
        if self.fault == "runtime":
            raise RuntimeError("programming error")
        if self.fault == "short":
            return os.read(fd, 7)
        return os.read(fd, size)

    def close(self, fd):
        if fd != self.fd:
            return os.close(fd)
        self.closes += 1
        self.fd = None  # the acquisition ends here; the number may now belong to someone else
        os.close(fd)  # the descriptor is really released first, then the error is reported
        if self.fault == "close" or self.then_close_fault:
            raise OSError(errno.EIO, PRIVATE)


class ReuseProxy(Proxy):
    def close(self, fd):
        if fd != self.fd:
            return os.close(fd)
        self.closes += 1
        self.fd = None
        os.close(fd)
        self.reused = os.open(os.devnull, os.O_RDONLY)  # normally takes the number just released
        raise OSError(errno.EIO, PRIVATE)


class Case(base.Base):
    def clean(self, *extra):
        """Fault-free JSON reports for register and find, as the control."""
        return (json.loads(base.run("personal-data", "--directory", self.root, "--json")[1]),
                json.loads(base.run("personal-data", "--directory", self.root, "--find", "qqq", "--json", *extra)[1]))

    def assert_file_unreadable(self, kind, proxy_factory):
        """Both APIs and all four CLI modes with the fault: complete report, exit 1, empty stderr."""
        base.valid_stores(self.root, text="qqq")
        control_reg, control_find = self.clean()
        before = base.tree(self.root)
        self.assertEqual(self.row(kind, control_reg)["status"], "present")
        path = base.KINDS[kind][0]
        for mode in ("register-json", "register-text", "find-json", "find-text"):
            with self.subTest(mode=mode):
                proxy = proxy_factory()
                args = ["personal-data", "--directory", self.root]
                args += ["--find", "qqq"] if mode.startswith("find") else []
                args += ["--json"] if mode.endswith("json") else []
                with mock.patch.object(personal_data, "os", proxy):
                    code, out, err = base.run(*args)
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(proxy.opens, 1)
                self.assertEqual(proxy.closes, 1, "exactly one close attempt per acquired descriptor")
                self.assertNotIn(PRIVATE, out)
                self.assertNotIn(str(self.root), out)
                if mode.endswith("json"):
                    report = json.loads(out)
                    expected = control_find if mode.startswith("find") else control_reg
                    got = {f["kind"]: f for f in report["files"]}
                    want = {f["kind"]: f for f in expected["files"]}
                    for other in want:
                        if other == kind:
                            continue
                        self.assertEqual(got[other], want[other], other)  # other files keep their own results
                    self.assertEqual(got[kind]["status"], "unreadable")
                    self.assertEqual(got[kind]["path"], path)
                    if mode.startswith("find"):
                        self.assertEqual(got[kind]["searched_fields"], [])
                        self.assertEqual(report["matches"], [m for m in expected["matches"] if m["kind"] != kind])
                        self.assertEqual(report["notice"], expected["notice"])
                    else:
                        for key, value in ROW_DEFAULT.items():
                            self.assertIsNone(got[kind][key])
                        self.assertEqual(report["notice"], expected["notice"])
                else:
                    self.assertIn(base.NOTICE if not mode.startswith("find") else personal_data_notice(), out)
                    self.assertIn("unreadable", out)
                self.assertEqual(base.tree(self.root), before)


def personal_data_notice():
    return base.FIND_NOTICE


class IoFaultTests(Case):
    def test_descriptor_metadata_fault_is_an_unreadable_store_in_both_apis_and_all_cli_modes(self):
        self.assert_file_unreadable("requester", lambda: Proxy("request-settings.json", "fstat"))
        proxy = Proxy("request-settings.json", "fstat")
        with mock.patch.object(personal_data, "os", proxy):
            report = personal_data.personal_data_register(self.root)
        self.assertEqual(proxy.reads, 0, "a failed metadata check reads and parses nothing")
        self.assertEqual(self.row("requester", report)["status"], "unreadable")

    def test_review_assignment_pattern_position_is_kept(self):
        base.valid_stores(self.root, text="qqq")
        base.put(self.root, "private/review-assignments/b.json", {"schema_version": 1, "assignments": {
            "legal": {"reviewer": "qqq", "on": "2026-02-11", "recorded_at": base.STAMP}}})
        control = personal_data.personal_data_find(self.root, "qqq")
        with mock.patch.object(personal_data, "os", Proxy("a.json", "fstat")):
            faulted = personal_data.personal_data_find(self.root, "qqq")
        files = [(f["path"], f["file_position"], f["status"]) for f in faulted["files"] if f["kind"] == "review_assignments"]
        want = [(f["path"], f["file_position"], f["status"]) for f in control["files"] if f["kind"] == "review_assignments"]
        self.assertEqual(len(files), 2)
        self.assertEqual([f[1] for f in files], [f[1] for f in want])
        self.assertEqual([f[2] for f in files], ["unreadable", want[1][2]])
        self.assertEqual([m for m in faulted["matches"] if m["kind"] == "review_assignments"],
                         [m for m in control["matches"] if m["kind"] == "review_assignments" and m["file_position"] == 2])

    def test_close_fault_after_success_discards_file_and_keeps_other_results(self):
        self.assert_file_unreadable("requester", lambda: Proxy("request-settings.json", "close"))
        self.assert_file_unreadable("volunteer_assignments", lambda: Proxy("assignments.json", "close"))

    def test_close_fault_on_no_identity_store_is_unreadable_not_no_identity_fields(self):
        base.put(self.root, "kit/supporters-consent.json", {"schema_version": 1, "supporters": {}})
        with mock.patch.object(personal_data, "os", Proxy("supporters-consent.json", "close")):
            report = personal_data.personal_data_find(self.root, "qqq")
        self.assertEqual(self.row("supporters_consent", report)["status"], "unreadable")

    def test_open_and_read_fault_controls_use_the_same_file_boundary(self):
        base.valid_stores(self.root, text="qqq")
        control = personal_data.personal_data_find(self.root, "qqq")
        for fault, closes in (("open", 0), ("read", 1), ("late_read", 1)):
            with self.subTest(fault=fault):
                proxy = Proxy("request-settings.json", fault)
                with mock.patch.object(personal_data, "os", proxy):
                    report = personal_data.personal_data_find(self.root, "qqq")
                    register = personal_data.personal_data_register(self.root)
                self.assertEqual(proxy.closes, closes * 2)
                self.assertEqual(self.row("requester", report)["status"], "unreadable")
                self.assertEqual(self.row("requester", register)["record_count"], None)
                self.assertEqual(report["matches"], [m for m in control["matches"] if m["kind"] != "requester"])
                self.assertNotIn(PRIVATE, json.dumps(report) + json.dumps(register))
        proxy = Proxy("request-settings.json", "short")
        with mock.patch.object(personal_data, "os", proxy):
            same = personal_data.personal_data_find(self.root, "qqq")
        self.assertEqual(same, control, "short reads to EOF give the unchanged result")
        self.assertGreater(proxy.reads, 2)

    def test_primary_refusal_and_secondary_close_failure_stay_private(self):
        base.valid_stores(self.root, text="qqq")
        for fault in ("fstat", "read"):
            with self.subTest(fault=fault):
                proxy = Proxy("request-settings.json", fault, then_close_fault=True)
                with mock.patch.object(personal_data, "os", proxy):
                    report = personal_data.personal_data_register(self.root)
                self.assertEqual(proxy.closes, 1)
                self.assertEqual(self.row("requester", report)["status"], "unreadable")
                self.assertNotIn(PRIVATE, json.dumps(report))
        base.put(self.root, "kit/request-settings.json", b"not json")  # malformed: adapter runs after a clean close
        proxy = Proxy("request-settings.json", "close")
        with mock.patch.object(personal_data, "os", proxy):
            report = personal_data.personal_data_register(self.root)
        self.assertEqual((self.row("requester", report)["status"], proxy.closes), ("unreadable", 1))

    def test_close_is_not_retried_when_descriptor_number_is_reused(self):
        base.valid_stores(self.root, text="qqq")
        proxy = ReuseProxy("request-settings.json", "reuse")
        try:
            with mock.patch.object(personal_data, "os", proxy):
                report = personal_data.personal_data_register(self.root)
            self.assertEqual(proxy.closes, 1)
            self.assertEqual(self.row("requester", report)["status"], "unreadable")
            self.assertIsNotNone(proxy.reused)
            os.fstat(proxy.reused)  # the unrelated descriptor is still open and usable
        finally:
            if proxy.reused is not None:
                os.close(proxy.reused)

    def test_unexpected_primary_exception_is_not_reclassified_or_masked(self):
        base.valid_stores(self.root, text="qqq")
        proxy = Proxy("request-settings.json", "runtime", then_close_fault=True)
        with mock.patch.object(personal_data, "os", proxy):
            with self.assertRaises(RuntimeError):
                personal_data.personal_data_register(self.root)
        self.assertEqual(proxy.closes, 1)
        with mock.patch.object(personal_data, "_parse", side_effect=RuntimeError("adapter bug")):
            with self.assertRaises(RuntimeError):
                personal_data.personal_data_register(self.root)

    def test_all_catalog_kinds_fault_per_file_without_inventory_policy_changes(self):
        names = {"requester": "request-settings.json", "reviewer_roster": "reviewers.json",
                 "review_assignments": "a.json", "comments_sent": "comments-sent.json",
                 "correction_requests": "correction-requests.json", "contact_log": "sent.json",
                 "volunteer_assignments": "assignments.json", "supporters_consent": "supporters-consent.json",
                 "speaker_plans": "m1.json"}
        base.valid_stores(self.root, text="qqq")
        control = personal_data.personal_data_register(self.root)
        control_find = personal_data.personal_data_find(self.root, "qqq")
        for kind, name in names.items():
            for fault in ("fstat", "close"):
                with self.subTest(kind=kind, fault=fault):
                    with mock.patch.object(personal_data, "os", Proxy(name, fault)):
                        register = personal_data.personal_data_register(self.root)
                        found = personal_data.personal_data_find(self.root, "qqq")
                    self.assertEqual(len(register["files"]), len(control["files"]))
                    for got, want in zip(register["files"], control["files"]):
                        if got["kind"] == kind:
                            self.assertEqual(got, {**want, "status": "unreadable", **ROW_DEFAULT})
                        else:
                            self.assertEqual(got, want)
                    self.assertEqual([m for m in found["matches"]],
                                     [m for m in control_find["matches"] if m["kind"] != kind])
                    self.assertEqual(self.row(kind, found)["status"], "unreadable")
                    self.assertEqual(self.row(kind, found)["searched_fields"], [])
                    self.assertEqual(personal_data.exit_code(register), 1)

    def test_fault_results_are_value_free_read_only_and_legacy_compatible(self):
        base.valid_stores(self.root, text="SENTINEL-VALUE-Q9")
        before = base.tree(self.root)
        with mock.patch.object(personal_data, "os", Proxy("request-settings.json", "close")):
            for extra in ((), ("--json",)):
                code, out, err = base.run("personal-data", "--directory", self.root, "--find", "SENTINEL-VALUE-Q9", *extra)
                self.assertEqual((code, err), (1, ""))
                for text in ("SENTINEL-VALUE-Q9", PRIVATE, str(self.root)):
                    self.assertNotIn(text, out)
        self.assertEqual(base.tree(self.root), before)
        code, out, err = base.run("personal-data", "--directory", self.root)  # no fault: unchanged success
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("unreadable", out)


if __name__ == "__main__":
    unittest.main()
