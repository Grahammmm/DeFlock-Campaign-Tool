"""RF2310-8: flock_state.py diff OLD.json NEW.json names what is new, gone or moved between two `state --json` snapshots."""
import contextlib
import io
import json
import os
import re
import shutil
import unittest

from tests.flock import test_state as base

MOD, row, SLUG, ADDR = base.MOD, base.row, base.SLUG, base.ADDR
NOTE = " | recorded fields only; not a legal status"


class DiffTests(unittest.TestCase):
    setUp, requests, email, state = base.StateTests.setUp, base.StateTests.requests, base.StateTests.email, base.StateTests.state

    def snapshot(self, name):  # what the organizer saves: the exact stdout of `state STORE --json`
        code, out, err = self.state("--json")
        self.assertEqual(err, "")
        (self.tmp / name).write_text(out, encoding="utf-8")
        return self.tmp / name

    def diff(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = MOD.main(["diff", *map(str, argv)])
        return code, out.getvalue(), err.getvalue()

    def both(self, old, new):  # text and --json agree on the change count and the exit code
        code, out, err = self.diff(old, new)
        jcode, raw, jerr = self.diff(old, new, "--json")
        data = json.loads(raw)
        self.assertEqual((err, jerr, jcode, data["schema"], data["review_required"]), ("", "", code, "flock-state-diff-v1", True))
        self.assertEqual(len(data["changes"]), len(out.splitlines()) - 1)
        self.assertEqual(data["unmatched"], int(re.search(r" unmatched=([0-9]+) ", out)[1]))
        return code, out.splitlines(), data

    def test_same_store_twice_is_no_change(self):
        self.requests(row("R-1"), row("R-2", "closed"))
        self.email("2026-10-02", field=False)
        old, new = self.snapshot("old.json"), self.snapshot("new.json")
        code, lines, data = self.both(old, new)
        self.assertEqual((code, lines), (0, ["diff | changed=0 new=0 gone=0 unmatched=0 | totals: none" + NOTE]))
        self.assertEqual((data["changes"], data["totals_delta"]), ([], {}))

    def test_request_and_run_move_one_line_each_and_totals_list_only_those_states(self):
        self.requests(row("R-1"), row("R-2", "closed"))
        self.email("2026-10-02", field=False)
        old = self.snapshot("old.json")
        self.requests(row("R-1", ev="synthetic ack", rdate="2026-10-02", sha=base.SHA), row("R-2", "closed"))
        self.email("2026-10-02")  # now carries a signed approval record
        new = self.snapshot("new.json")
        code, lines, data = self.both(old, new)
        self.assertEqual(code, 3)
        self.assertEqual(lines, [f"diff | request | {SLUG} | R-1 | open-receipt-unknown -> open-receipt-recorded",
                                 f"diff | run | {SLUG} | 2026-10-02 | email-drafted -> approved-not-sent",
                                 "diff | changed=2 new=0 gone=0 unmatched=0 | totals: open-receipt-unknown=1->0 open-receipt-recorded=0->1 "
                                 "email-drafted=1->0 approved-not-sent=0->1" + NOTE])
        self.assertEqual(data["totals_delta"], {"open-receipt-unknown": {"old": 1, "new": 0}, "open-receipt-recorded": {"old": 0, "new": 1},
                                                "email-drafted": {"old": 1, "new": 0}, "approved-not-sent": {"old": 0, "new": 1}})
        self.assertEqual(data["changes"][1], {"kind": "run", "agency": SLUG, "id": "2026-10-02", "old": "email-drafted", "new": "approved-not-sent"})
        code, lines, _ = self.both(new, old)  # snapshots given out of order: the arrows reverse, nothing else
        self.assertEqual((code, lines[0]), (3, f"diff | request | {SLUG} | R-1 | open-receipt-recorded -> open-receipt-unknown"))

    def test_removed_row_is_gone_and_added_agency_is_new(self):
        self.requests(row("R-1"), row("R-2", "withdrawn"))
        old = self.snapshot("old.json")
        self.requests(row("R-1"))
        self.requests(row("R-9", agency="Other Town"), slug="other-town")
        new = self.snapshot("new.json")
        code, lines, data = self.both(old, new)
        self.assertEqual(code, 3)
        self.assertEqual(lines, ["diff | request | other-town | R-9 | new: open-receipt-unknown",
                                 f"diff | request | {SLUG} | R-2 | gone (was withdrawn)",
                                 "diff | changed=0 new=1 gone=1 unmatched=0 | totals: open-receipt-unknown=1->2 withdrawn=1->0" + NOTE])
        self.assertEqual([(c["old"], c["new"]) for c in data["changes"]], [(None, "open-receipt-unknown"), ("withdrawn", None)])

    def test_basis_change_with_the_same_state_is_not_a_change(self):
        self.requests(row("R-1", "weird"))
        old = self.snapshot("old.json")
        self.requests(row("R-1", sent="2026-10-05", ev="synthetic ack", rdate="2026-10-02", sha=base.SHA))
        new = self.snapshot("new.json")
        bases = [json.loads(p.read_text(encoding="utf-8"))["requests"][0]["basis"] for p in (old, new)]
        self.assertEqual(bases, [["status"], ["receipt_date"]])  # both record-error, for different fields
        code, lines, _ = self.both(old, new)
        self.assertEqual((code, lines), (0, ["diff | changed=0 new=0 gone=0 unmatched=0 | totals: none" + NOTE]))

    def test_masked_labels_are_counted_never_paired(self):
        self.requests(row("R-1"))
        old = self.snapshot("old.json")
        self.requests(row("R-1"), row("overdue"), row("R@" + ADDR, "closed"))  # two (unsafe-label) requests in one agency
        new = self.snapshot("new.json")
        code, lines, _ = self.both(old, new)
        self.assertEqual((code, lines), (3, ["diff | changed=0 new=0 gone=0 unmatched=2 | totals: open-receipt-unknown=1->2 closed=0->1" + NOTE]))
        self.requests(row("R-1"), row("overdue", "closed"), row("R@" + ADDR))  # their states swap: nothing pairs them
        code, lines, _ = self.both(new, self.snapshot("swap.json"))
        self.assertEqual((code, lines), (0, ["diff | changed=0 new=0 gone=0 unmatched=4 | totals: none" + NOTE]))
        self.requests(row("R-1"), row("", "open"), row("", "closed"), "R-G,short\n")  # no id (-) and `row N`: counted, not paired
        code, lines, _ = self.both(self.snapshot("rows.json"), self.snapshot("rows2.json"))
        self.assertEqual((code, lines), (0, ["diff | changed=0 new=0 gone=0 unmatched=6 | totals: none" + NOTE]))

    def test_planted_address_and_banned_word_in_old_labels_never_print(self):
        self.requests(row("R-1"), row("R@" + ADDR), row("overdue-1"), slug=SLUG)
        self.requests(row("R-2", agency="Illegal Town"), slug="illegal-town")
        old = self.snapshot("old.json")
        shutil.rmtree(self.store / "agencies" / "illegal-town")
        new = self.snapshot("new.json")
        for extra in ((), ("--json",)):
            code, out, err = self.diff(old, new, *extra)
            self.assertEqual(code, 3)
            self.assertFalse(base.BANNED.search(out + err) or "@" in out + err or "planted" in out + err, out + err)

    def valid(self):
        self.requests(row("R-1"), row("R-2"))
        self.email("2026-10-02", field=False)
        return json.loads(self.snapshot("valid.json").read_text(encoding="utf-8"))

    def write(self, name, data):
        (self.tmp / name).write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
        return self.tmp / name

    def test_unusable_snapshots_exit_one_naming_side_and_field_only(self):
        good = self.valid()
        text = json.dumps(good)
        reqs = good["requests"]
        cases = {"missing": (None, "snapshot unreadable"), "truncated": (text[: len(text) // 2], "snapshot unreadable"),
                 "not-utf8": ("\udcff", "snapshot unreadable"), "list": ([good], "snapshot keys are not flock-state-v1"),
                 "big": (text + " " * (MOD.SNAP_MAX + 1 - len(text)), "snapshot is over 5 MiB"),
                 "v2": ({**good, "schema": "flock-state-v2"}, "schema is not flock-state-v1"),
                 "review": ({**good, "review_required": False}, "review_required is not true"),
                 "review-1": ({**good, "review_required": 1}, "review_required is not true"),
                 "top-key": ({**good, "extra": 1}, "snapshot keys are not flock-state-v1"),
                 "state": ({**good, "requests": [reqs[0], {**reqs[1], "state": "pending"}]}, "requests[1].state is not in the vocabulary"),
                 "run-state": ({**good, "runs": [{**good["runs"][0], "state": "open-receipt-unknown"}]}, "runs[0].state is not in the vocabulary"),
                 "extra-key": ({**good, "requests": [{**reqs[0], "note": "x"}]}, "requests[0] keys are not v1"),
                 "basis": ({**good, "requests": [{**reqs[0], "basis": "status"}]}, "requests[0].basis is not a list of field names"),
                 "dup": ({**good, "requests": [reqs[0], reqs[0]]}, "requests[1] repeats an (agency, request_id) pair"),
                 "address": ({**good, "requests": [{**reqs[0], "agency": ADDR}]}, "requests[0].agency is not a plain label"),
                 "banned": ({**good, "requests": [{**reqs[0], "request_id": "overdue-R1"}]}, "requests[0].request_id is not a plain label"),
                 "run-id": ({**good, "runs": [{**good["runs"][0], "run": 7}]}, "runs[0].run is not a plain label")}
        for name, (data, why) in cases.items():
            with self.subTest(name):
                bad = self.tmp / "missing.json" if data is None else self.tmp / (name + ".json")
                if name == "not-utf8":
                    bad.write_bytes(b"\xff\xfe{}")
                elif data is not None:
                    self.write(bad.name, data)
                for argv, side in (((bad, self.tmp / "valid.json"), "OLD"), ((self.tmp / "valid.json", bad), "NEW")):
                    code, out, err = self.diff(*argv)
                    self.assertEqual((code, out, err), (1, "", f"diff: {why} ({side})\n"))
                    self.assertFalse(base.BANNED.search(err) or "@" in err or str(self.tmp) in err, err)
        (self.tmp / "link.json").symlink_to(self.tmp / "valid.json")
        self.assertEqual(self.diff(self.tmp / "link.json", self.tmp / "valid.json"), (1, "", "diff: snapshot must not be a link (OLD)\n"))
        self.assertEqual(self.diff(self.tmp / "valid.json", self.tmp), (1, "", "diff: snapshot unreadable (NEW)\n"))  # a folder
        os.mkfifo(self.tmp / "pipe.json")  # never blocks waiting for a writer
        self.assertEqual(self.diff(self.tmp / "valid.json", self.tmp / "pipe.json"), (1, "", "diff: snapshot is not a file (NEW)\n"))
        exact = self.write("exact.json", text + " " * (MOD.SNAP_MAX - len(text)))  # exactly 5 MiB is still read
        self.assertEqual(self.diff(exact, self.tmp / "valid.json")[0], 0)

    def test_usage_errors_exit_two(self):
        path = str(self.write("a.json", self.valid()))
        for argv in ([], [path], [path, path, path], [path, path, "--text"], ["--json", path], [path, "--json"], [path, path, "--json", "x"]):
            with self.subTest(argv=argv):
                code, out, err = self.diff(*argv)
                self.assertEqual((code, out, err.startswith("usage: ")), (2, "", True))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(MOD.main(["--help"]), 0)
        self.assertIn("diff OLD.json NEW.json [--json]", out.getvalue())


if __name__ == "__main__":
    unittest.main()
