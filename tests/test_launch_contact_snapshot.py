"""RF1045-4: the launch ``verified_contacts`` row derives both predicates from one parsed snapshot (synthetic data only)."""
import contextlib
from datetime import date, timedelta
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool import cli, kit, launch_check
from campaign_tool.launch_check import _contact_snapshot, launch_checks
from tests import test_doctor_launch as base

TODAY = base.TODAY
STALE_DETAIL = "1 stale; verified selected contact present"
ABSENT_DETAIL = "0 stale; verified selected contact absent"
OK_DETAIL = "0 stale; verified selected contact present"
ACTION = "Re-check selected agency contacts on official sites (kit-verify-agency)"
HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def agency(checked_on, selected=True, verified=True, agency_id="ca-synthetic-sheriff"):
    return dict(agency_id=agency_id, selected=selected, verified=verified, verification=dict(checked_on=checked_on))


def days_ago(n, today=TODAY):
    return (today - timedelta(days=n)).isoformat()


class LaunchContactSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fx = base.DoctorLaunchTests("test_complete_campaign_and_no_public_tree")
        self.fx.setUp()
        self.addCleanup(self.fx.temp.cleanup)
        self.root = self.fx.root
        self.target = self.root / "kit/agencies.json"
        self.trees = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            path = real_mkdtemp(*args, **kwargs)
            if kwargs.get("prefix") == "campaign-launch-":
                self.trees.append(Path(path))
            return path

        patcher = patch.object(launch_check.tempfile, "mkdtemp", mkdtemp)
        patcher.start()
        self.addCleanup(self.sweep)
        self.addCleanup(patcher.stop)

    def sweep(self):  # test-owned residue only
        for tree in self.trees:
            shutil.rmtree(tree, ignore_errors=True)

    def put(self, document):
        self.target.write_text(json.dumps(document))

    def row(self, **kwargs):
        return self.fx.checks(**kwargs)["verified_contacts"]

    def replacing_after_first_read(self, replacement, arm=None):
        """Patch Path.read_text so the first real read of the source file returns the old bytes and then swaps the file.

        ``arm`` (optional) is a list that must be non-empty before a read counts, so only reads inside the launch
        stage are observed. Returns (patch, reads); ``reads`` records every counted read of the source file.
        """
        real, reads = Path.read_text, []

        def read_text(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if path == self.target and (arm is None or arm):
                reads.append(result)
                if len(reads) == 1:
                    self.target.write_text(json.dumps(replacement))
            return result

        return patch.object(Path, "read_text", read_text), reads

    # --- the demonstrated defect -------------------------------------------------------------------------------
    def test_two_nonready_documents_cannot_combine_into_ok(self):
        first = [agency(TODAY.isoformat(), selected=False)]  # fresh but unselected: independently todo
        second = [agency(days_ago(366))]  # selected and verified but stale: independently todo
        for document, detail in ((first, ABSENT_DETAIL), (second, STALE_DETAIL)):
            self.put(document)
            control = self.row()
            self.assertEqual((control["state"], control["detail"], control["next_action"]), ("todo", detail, ACTION))
            self.assertEqual(len(kit.stale_contacts(self.root, 365, TODAY)), 1 if document is second else 0)
        self.put(first)
        hook, reads = self.replacing_after_first_read(second)
        with hook:
            row = self.row()
        self.assertEqual(json.loads(self.target.read_text()), second)  # the swap really happened
        self.assertEqual(len(reads), 1)  # one acquisition for the row
        self.assertEqual((row["state"], row["detail"], row["next_action"]), ("todo", ABSENT_DETAIL, ACTION))

    def test_presence_and_stale_count_use_one_snapshot_in_both_directions(self):
        fresh, stale = agency(TODAY.isoformat()), agency(days_ago(366), agency_id="ca-synthetic-other")
        cases = (
            ("stale-first-unselected-second", [stale], [agency(TODAY.isoformat(), selected=False)], "todo", STALE_DETAIL),
            ("fresh-first-stale-second", [fresh], [stale], "ok", OK_DETAIL),  # coherent older snapshot is allowed
            ("row-inserted", [fresh], [fresh, stale], "ok", OK_DETAIL),
            ("row-removed", [fresh, stale], [fresh], "todo", STALE_DETAIL),
            ("selection-lost", [fresh], [agency(TODAY.isoformat(), selected=False)], "ok", OK_DETAIL),
            ("selection-gained", [agency(TODAY.isoformat(), selected=False)], [fresh], "todo", ABSENT_DETAIL),
        )
        for label, first, second, state, detail in cases:
            with self.subTest(label):
                self.put(first)
                hook, reads = self.replacing_after_first_read(second)
                with hook:
                    row = self.row()
                self.assertEqual(len(reads), 1)
                self.assertEqual((row["state"], row["detail"]), (state, detail))
                self.assertEqual(bool(row["next_action"]), state != "ok")

    # --- stable-input semantics --------------------------------------------------------------------------------
    def test_stable_predicate_parity_at_age_and_shape_edges(self):
        limit = 365
        bare = dict(agency_id="a", selected=True, verified=True)
        cases = [
            ("threshold-1", [agency(days_ago(limit - 1))], 0, True),
            ("threshold", [agency(days_ago(limit))], 0, True),
            ("threshold+1", [agency(days_ago(limit + 1))], 1, True),
            ("future", [agency((TODAY + timedelta(days=30)).isoformat())], 0, True),
            ("invalid-calendar-date", [agency("2026-02-30")], 1, True),
            ("unpadded", [agency("2026-1-1")], 1, True),
            ("trailing-newline", [agency(TODAY.isoformat() + "\n")], 1, True),
            ("blank", [agency("")], 1, True),
            ("integer-date", [agency(20261101)], 1, True),
            ("null-date", [agency(None)], 1, True),
            ("missing-checked_on", [dict(bare, verification={})], 1, True),
            ("missing-verification", [dict(bare)], 1, True),
            ("string-verification", [dict(bare, verification="2026-11-01")], 1, True),
            ("list-verification", [dict(bare, verification=[TODAY.isoformat()])], 1, True),
            ("non-dict-rows-ignored", [None, 5, "x", [1], agency(TODAY.isoformat())], 0, True),
            ("only-non-dict-rows", [None, 5, "x"], 0, False),
            ("strict-selected", [agency(days_ago(999), selected="true"), agency(days_ago(999), selected=1)], 0, False),
            ("strict-verified", [agency(days_ago(999), verified="true"), agency(days_ago(999), verified=1)], 0, False),
            ("unverified-stale-ignored", [agency(days_ago(999), verified=False)], 0, False),
            ("duplicates-counted-separately", [agency(days_ago(999)), agency(days_ago(999)), agency(TODAY.isoformat())], 2, True),
        ]
        for label, document, stale, present in cases:
            with self.subTest(label):
                self.assertEqual(launch_check._contact_status(document, limit, TODAY), (stale, present))
                self.put(document)
                self.assertEqual(_contact_snapshot(self.root, limit, TODAY), (stale, present))
                self.assertEqual(len(kit.stale_contacts(self.root, limit, TODAY)), stale)  # same rule as the doctor
                row = self.row()
                ready = not stale and present
                self.assertEqual((row["state"], row["detail"]),
                                 ("ok" if ready else "todo",
                                  f"{stale} stale; verified selected contact {'present' if present else 'absent'}"))
                self.assertEqual(row["next_action"], None if ready else ACTION)
        with self.subTest("leap-day"):
            self.put([agency("2024-02-29")])
            for today, expected in ((date(2025, 2, 28), (0, True)), (date(2025, 3, 1), (1, True))):
                self.assertEqual(_contact_snapshot(self.root, 365, today), expected)
                self.assertEqual(len(kit.stale_contacts(self.root, 365, today)), expected[0])

    def test_missing_empty_and_nonlist_keep_existing_todo(self):
        self.target.unlink()
        for label, document in (("absent", None), ("empty-list", []), ("object", {"selected": True, "verified": True}),
                                ("string", "x"), ("number", 5), ("null", None)):
            with self.subTest(label):
                if label != "absent":
                    self.put(document)
                report = self.fx.checks()
                self.assertEqual(report["verified_contacts"]["state"], "todo")
                self.assertEqual(report["verified_contacts"]["detail"], ABSENT_DETAIL)
                self.assertEqual(report["verified_contacts"]["next_action"], ACTION)
                self.assertEqual((_contact_snapshot(self.root, 365, TODAY)), (0, False))
        for label, data in (("malformed", b"{"), ("bad-utf8", b"\xff\xfe[1]")):
            with self.subTest(label):
                self.target.write_bytes(data)
                rows = self.fx.checks()
                self.assertEqual((rows["verified_contacts"]["state"], rows["verified_contacts"]["detail"]),
                                 ("fail", "kit/agencies.json is unreadable"))
                self.assertEqual(rows["verified_contacts"]["next_action"],
                                 "Fix kit/agencies.json and verify selected contacts")
                self.assertEqual(rows["law_package"]["check"], "law_package")
        self.target.unlink()
        self.target.mkdir()  # a real read refusal
        rows = self.fx.checks()
        self.assertEqual((rows["verified_contacts"]["state"], rows["verified_contacts"]["detail"]),
                         ("fail", self.fx.CONTACT_FS))
        self.fx.assert_private(rows)
        for exc in self.fx.payloads():
            with self.subTest(exc=type(exc).__name__):
                self.target.rmdir() if self.target.is_dir() else None
                self.put([agency(TODAY.isoformat())])
                real = Path.read_text

                def read_text(path, *args, **kwargs):
                    if path == self.target:
                        raise exc
                    return real(path, *args, **kwargs)

                with patch.object(Path, "read_text", read_text):
                    rows = self.fx.checks()
                self.assertEqual(rows["verified_contacts"]["detail"], self.fx.CONTACT_FS)
                self.fx.assert_private(rows)

    def test_effective_date_is_captured_once_and_custom_limit_is_honored(self):
        self.put([agency(days_ago(100)), agency(days_ago(200)), agency(days_ago(300))])
        self.assertEqual(self.row(contact_max_age_days=99)["detail"], "3 stale; verified selected contact present")
        self.assertEqual(self.row(contact_max_age_days=100)["detail"], "2 stale; verified selected contact present")
        self.assertEqual(self.row(contact_max_age_days=300)["detail"], OK_DETAIL)
        self.assertEqual(self.row()["state"], "ok")  # default limit 365
        calls = []

        class FakeDate(date):
            @classmethod
            def today(cls):
                calls.append("today")
                return cls(TODAY.year, TODAY.month, TODAY.day)

        with patch.object(launch_check, "date", FakeDate):
            self.assertEqual(_contact_snapshot(self.root, 150), (2, True))  # default date is read once for all rows
            self.assertEqual(calls, ["today"])
            self.assertEqual(_contact_snapshot(self.root, 150, TODAY + timedelta(days=100)), (3, True))
            self.assertEqual(calls, ["today"])  # an explicit date is used, the clock is not consulted
            self.assertEqual(_contact_snapshot(self.root, 150, today=TODAY - timedelta(days=100)), (1, True))

    # --- ordinary CLI -----------------------------------------------------------------------------------------
    def assert_launch_contract(self, result):
        launch = result["launch"]
        self.assertEqual([r["check"] for r in launch["checks"]], base.ORDER)
        self.assertEqual(launch["total"], 9)
        self.assertEqual(launch["passed"], sum(r["state"] == "ok" for r in launch["checks"]))
        self.assertEqual(launch["note"], launch_check.NOTE)
        for key in ("production_ready", "public_deployment_checked", "safe_to_send_automatically"):
            self.assertIs(result[key], False)
        self.assertNotRegex(json.dumps(result).lower(), r"launched|ready to launch")
        self.assertIsNone(HEX64.search(json.dumps(launch)))
        return {r["check"]: r for r in launch["checks"]}

    def test_cli_launch_preserves_json_contract_and_offline_flags(self):
        real_today = date.today()
        self.put([agency(days_ago(400, real_today))])
        result = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root),
                                 "--launch"], cwd=base.REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        rows = self.assert_launch_contract(json.loads(result.stdout))
        self.assertEqual((rows["verified_contacts"]["state"], rows["verified_contacts"]["detail"],
                          rows["verified_contacts"]["next_action"]), ("todo", STALE_DETAIL, ACTION))
        self.put([agency(real_today.isoformat())])
        fresh = subprocess.run([sys.executable, "-B", "-m", "campaign_tool", "doctor", "--directory", str(self.root),
                                "--launch"], cwd=base.REPO, capture_output=True, text=True, timeout=60)
        self.assertEqual((fresh.returncode, fresh.stderr), (0, ""))
        fresh_rows = self.assert_launch_contract(json.loads(fresh.stdout))
        self.assertEqual((fresh_rows["verified_contacts"]["state"], fresh_rows["verified_contacts"]["detail"]),
                         ("ok", OK_DETAIL))
        # Hooked in-process dispatch through the real argument parser: a swap during the launch stage only.
        real_today = date.today()
        first = [agency(real_today.isoformat(), selected=False)]
        second = [agency(days_ago(400, real_today))]
        self.put(first)
        armed = []
        real_launch = launch_check.launch_checks

        def arming(*args, **kwargs):
            armed.append(True)
            return real_launch(*args, **kwargs)

        hook, reads = self.replacing_after_first_read(second, arm=armed)
        out = io.StringIO()
        argv = ["campaign_tool", "doctor", "--directory", str(self.root), "--launch"]
        with patch.object(sys, "argv", argv), patch.object(launch_check, "launch_checks", arming), hook, \
                contextlib.redirect_stdout(out):
            cli.main()
        self.assertEqual(len(reads), 1)
        rows = self.assert_launch_contract(json.loads(out.getvalue()))
        self.assertEqual((rows["verified_contacts"]["state"], rows["verified_contacts"]["detail"],
                          rows["verified_contacts"]["next_action"]), ("todo", ABSENT_DETAIL, ACTION))

    # --- read-only preservation -------------------------------------------------------------------------------
    def state(self):
        out = {}
        for p in sorted(self.root.rglob("*")):
            st = p.lstat()
            out[str(p.relative_to(self.root))] = (
                p.read_bytes() if p.is_file() and not p.is_symlink() else None, st.st_mode, st.st_mtime_ns,
                os.readlink(p) if p.is_symlink() else None)
        return out

    def test_refusal_and_replacement_leave_campaign_outputs_untouched(self):
        (self.root / "public").mkdir()
        (self.root / "public/sentinel.bin").write_bytes(b"synthetic\x00\xff")
        before = self.state()
        self.fx.checks()
        self.assertEqual(self.state(), before)
        self.target.write_bytes(b"{")  # intentional test edit; the refusal itself must write nothing
        before = self.state()
        self.assertEqual(self.fx.checks()["verified_contacts"]["state"], "fail")
        self.assertEqual(self.state(), before)
        self.put([agency(TODAY.isoformat(), selected=False)])
        before = self.state()
        replacement = [agency(days_ago(366))]
        hook, reads = self.replacing_after_first_read(replacement)
        with hook:
            self.fx.checks()
        after = self.state()
        self.assertEqual(json.loads(after.pop("kit/agencies.json")[0]), replacement)  # only the test's own swap
        before.pop("kit/agencies.json")
        self.assertEqual(after, before)
        self.assertTrue(self.trees)
        self.assertTrue(all(not tree.exists() for tree in self.trees))  # the rehearsal removed its own scratch
        self.assertTrue((self.root / "public/sentinel.bin").is_file())


if __name__ == "__main__":
    unittest.main()
