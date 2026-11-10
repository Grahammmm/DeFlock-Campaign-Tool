"""DF1109-01: `today` lists request work waiting on the organizer: an open clarification request, an unpaid latest fee
estimate and a passed agency-stated production date. Read-only; nothing is sent. Synthetic only.

Run from the repository root: python3 -B -m unittest tests.test_today_request_work -v
"""
import contextlib
import json
import os
import shlex
import stat
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_track, today  # noqa: E402
from tests.test_findings_status import snapshot  # noqa: E402
from tests.test_today import AS_OF, ITEM_KEYS, TodayTests, run  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "today"
NEW = ("request_waiting_on_you", "request_fee_unpaid", "request_stated_date_passed")
C_NAME = "Grover Beach Police Department (ca-grover-beach-police)"
STATED = "the agency's own date; provisional, not a legal deadline; follow up"


def est(on, cents):
    return {"kind": "fee_estimate", "on": on, "amount_cents": cents}


def step(kind, on, **extra):
    return {"kind": kind, "on": on, **extra}


class RequestWorkTests(unittest.TestCase):
    setUp, ok, build_tree = TodayTests.setUp, TodayTests.ok, TodayTests.build_tree

    def sent(self, change):
        path = self.root / "kit" / "sent.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        change(document["requests"])
        path.write_text(json.dumps(document), encoding="utf-8")

    def steps(self, agency, steps, request="main"):
        def change(requests):
            key = kit_track._key(agency, request)
            if key not in requests:  # a further request to the same agency, copied from its main entry
                requests[key] = {**requests[agency], "draft": kit_track._draft_path(agency, request)}
            requests[key]["steps"] = steps
        self.sent(change)

    def work(self, **options):
        return [item for item in today.todo(self.root, AS_OF, **options) if item["kind"].startswith("request_")]

    def test_waiting_clarification_command_and_clearance(self):
        self.root = self.tmp / "o'brien campaign"  # the printed commands must survive a space and an apostrophe
        self.ok("init", "--directory", self.root, "--name", "Synthetic", "--county", "San Luis Obispo", "--state", "CA")
        self.build_tree()
        c, directory = self.c, shlex.quote(str(self.root))
        self.steps(c, [step("clarification_requested", "2026-03-04")])
        self.steps(c, [est("2026-03-03", 125_00), step("clarification_requested", "2026-03-05")], "second")
        items = self.work()
        self.assertEqual([i["kind"] for i in items], ["request_waiting_on_you", "request_waiting_on_you",
                                                      "request_fee_unpaid"], items)
        main, named, fee = items
        self.assertEqual((main["what"], main["due"]),
                         (f"{C_NAME}: the agency asked for clarification on 2026-03-04; reply", "2026-03-04"))
        self.assertEqual(main["command"], f"kit-clarify-reply --directory {directory} --agency {c}")
        self.assertEqual(named["what"], f"Grover Beach Police Department ({c}/second): the agency asked for "
                                        "clarification on 2026-03-05; reply")
        self.assertEqual(shlex.split(named["command"]), ["kit-clarify-reply", "--directory", str(self.root),
                                                         "--agency", c, "--request", "second"])
        self.assertEqual(shlex.split(fee["command"]), ["kit", "fee-reply", "--directory", str(self.root),
                                                       "--agency", c, "--request", "second"])
        self.assertNotIn("pause", json.dumps(items))
        self.steps(c, [step("clarification_requested", "2026-03-04"), step("clarified", "2026-03-06")])
        self.assertEqual([i["kind"] for i in self.work()], ["request_past_due", "request_waiting_on_you",
                                                            "request_fee_unpaid"])

    def test_latest_unpaid_estimate_date_amount_and_recorded_order(self):
        self.build_tree()
        a = self.a
        cases = (([], None), ([est("2026-03-11", 0)], ("$0.00", "2026-03-11")),
                 ([est("2026-03-11", 10_000_000)], ("$100,000.00", "2026-03-11")),
                 ([est("2026-03-11", 500), est("2026-03-12", 700)], ("$7.00", "2026-03-12")),
                 ([est("2026-03-12", 700), est("2026-03-11", 500)], ("$7.00", "2026-03-12")),
                 ([est("2026-03-12", 500), est("2026-03-12", 900)], ("$9.00", "2026-03-12")),
                 ([est("2026-03-11", 500), step("fee_paid", "2026-03-12", amount_cents=500)], None),
                 ([est("2026-03-11", 500), step("fee_waived", "2026-03-12")], None),
                 ([step("fee_waived", "2026-03-11"), est("2026-03-12", 500)], ("$5.00", "2026-03-12")),
                 ([est("2026-03-11", 500), step("fee_paid", "2026-03-12"), est("2026-03-13", 800)],
                  ("$8.00", "2026-03-13")))
        for steps, expected in cases:
            with self.subTest(steps=steps):
                self.steps(a, steps)
                fees = [i for i in self.work() if i["kind"] == "request_fee_unpaid"]
                if expected is None:
                    self.assertEqual(fees, [])
                    continue
                self.assertEqual(len(fees), 1, fees)  # only the selected latest estimate
                shown = next(r for r in kit_track.status(self.root)["requests"] if r["agency_id"] == a)
                self.assertEqual(shown["fee_estimate"], expected[0])  # the amount exactly as kit-status shows it
                self.assertEqual(fees[0], {"rank": fees[0]["rank"], "kind": "request_fee_unpaid", "due": expected[1],
                                           "what": f"Arroyo Grande Police Department ({a}): unpaid fee estimate "
                                                   f"{expected[0]} recorded {expected[1]}; review and reply",
                                           "command": f"kit fee-reply --directory {self.root} --agency {a}"})

    def test_closed_withdrawn_and_owned_requests(self):
        self.build_tree()
        c = self.c
        for end in ("closed", "withdrawn"):
            with self.subTest(end=end):
                self.steps(c, [est("2026-03-03", 100), step("clarification_requested", "2026-03-04"),
                               step(end, "2026-03-05")])
                self.assertEqual(self.work(), [])
        self.steps(c, [est("2026-03-03", 100)])
        self.steps(c, [step("clarification_requested", "2026-03-04")], "second")
        self.ok("kit-assign", "--directory", self.root, "--agency", c, "--to", "pat")
        everything = self.work()
        self.assertEqual([(i["kind"], i["what"].endswith(" [owner: pat]")) for i in everything],
                         [("request_past_due", True), ("request_waiting_on_you", False), ("request_fee_unpaid", True)])
        mine = self.work(owner="pat")  # the named request does not inherit main's owner
        self.assertEqual([i["kind"] for i in mine], ["request_past_due", "request_fee_unpaid"])
        self.assertEqual(mine[0]["command"], f"kit-status --directory {self.root} --owner pat")
        self.assertEqual(self.work(owner="sam"), [])
        (self.root / "kit" / "assignments.json").write_text("{broken", encoding="utf-8")
        items = today.todo(self.root, AS_OF, owner="pat")
        self.assertEqual([i["kind"] for i in items if i["kind"].startswith("request_")], [])
        self.assertEqual([i["what"].split(":")[0] for i in items if i["kind"] == "unreadable"],
                         ["fix kit/assignments.json"])
        self.assertIn("comment_deadline", [i["kind"] for i in items])

    def test_stated_date_strict_boundary_and_production_clearance(self):
        self.build_tree()
        c = self.c
        for stated, listed in (("2026-03-14", True), ("2026-03-15", False), ("2026-03-16", False)):
            with self.subTest(stated=stated):
                self.steps(c, [step("determination", "2026-03-11", expected_on=stated)])
                items = [i for i in self.work() if i["kind"] == "request_stated_date_passed"]
                self.assertEqual(items, [{"rank": items[0]["rank"], "kind": "request_stated_date_passed",
                                          "what": f"{C_NAME}: stated date passed {stated} — {STATED}", "due": stated,
                                          "command": f"kit-followup --directory {self.root} --agency {c}"}]
                                 if listed else [])
        cleared = [step("determination", "2026-03-11", expected_on="2026-03-13"), step("produced", "2026-03-14")]
        partial = [step("determination", "2026-03-11", expected_on="2026-03-12"),
                   step("produced", "2026-03-12", partial=True, expected_on="2026-03-13")]
        extension = [step("extension", "2026-03-05", extension_expected_on="2026-03-13")]
        for steps, kinds in ((cleared, []), (partial, ["request_installment_passed"]),
                             (extension, ["request_stated_extension_date_passed"])):
            with self.subTest(steps=steps):
                self.steps(c, steps)
                self.assertEqual([i["kind"] for i in self.work()], kinds)
        self.steps(c, [step("determination", "2026-03-11", expected_on="2026-03-14")])
        self.steps(self.a, [step("clarification_requested", "2026-03-12")])
        no_law = self.work(law_base=self.tmp / "no-law-packages")  # no provisional dates; organizer work remains
        self.assertEqual([(i["kind"], i["due"]) for i in no_law],
                         [("request_waiting_on_you", "2026-03-12"), ("request_stated_date_passed", "2026-03-14")])

    def test_receipt_provenance_and_fixed_ranking(self):
        self.assertEqual(today.KINDS[:10], ("comment_deadline", "request_past_due", "request_due_today", *NEW,
                                            "request_installment_passed", "request_installment_due",
                                            "request_stated_extension_date_passed", "request_download_pending"))
        self.assertEqual(today.KINDS[-5:], ("review_waiting", "stale_body_schedule", "expected_regular_meeting",
                                            "backup_due", "unreadable"))
        self.build_tree()
        a, c = self.a, self.c
        self.steps(c, [step("clarification_requested", "2026-03-04"), est("2026-03-12", 300)])
        self.steps(c, [est("2026-03-12", 200)], "second")  # past due, with a same-day estimate: tie broken by key
        self.steps(a, [step("determination", "2026-03-11", expected_on="2026-03-14"), est("2026-03-12", 250)])
        self.ok("kit-received", "--directory", self.root, "--agency", a, "--on", "2026-03-11", "--evidence", "portal")
        items = self.work()
        self.assertEqual([(i["kind"], i["what"].split(" (")[1].split(")")[0]) for i in items],
                         [("request_past_due", f"{c}/second"), ("request_waiting_on_you", c),
                          ("request_fee_unpaid", a), ("request_fee_unpaid", c), ("request_fee_unpaid", f"{c}/second"),
                          ("request_stated_date_passed", a)])
        note = " (counted from receipt 2026-03-11 (portal))"
        for item in items:
            received = f"({a})" in item["what"]
            self.assertEqual(item["what"].endswith(note), received, item)
            self.assertEqual(set(item), ITEM_KEYS | ({"received_on", "receipt_evidence"} if received else set()))
            if received:
                self.assertEqual((item["received_on"], item["receipt_evidence"]), ("2026-03-11", "portal"))
        code, out, err = run("today", "--directory", self.root, "--as-of", "2026-03-15", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual([i for i in json.loads(out) if i["kind"].startswith("request_")], items)
        text = self.ok("today", "--directory", self.root, "--as-of", "2026-03-15").splitlines()
        self.assertEqual([line for line in text if "] " in line and line.split("] ")[0].split("[")[1] in NEW],
                         [f"{i['rank']}. [{i['kind']}] {i['what']} — {i['command']}" for i in items
                          if i["kind"] in NEW])

    def test_bad_input_is_one_unreadable_and_other_sources_survive(self):
        self.build_tree()
        self.steps(self.c, [est("2026-03-03", 100), step("clarification_requested", "2026-03-04")])
        settings = self.root / "kit" / "request-settings.json"
        sent = self.root / "kit" / "sent.json"
        good = sent.read_bytes()
        bad = (lambda: settings.write_text("{nope", encoding="utf-8"),
               lambda: (sent.rename(self.tmp / "sent.json"), sent.symlink_to(self.tmp / "sent.json")),
               lambda: self.steps(self.c, [{"kind": "fee_estimate", "on": "2026-03-03"}]))
        for index, breaks in enumerate(bad):
            with self.subTest(case=index):
                breaks()
                kinds = [i["kind"] for i in today.todo(self.root, AS_OF)]
                self.assertEqual(kinds, ["comment_deadline", "finding_ready", "finding_blocked", "stale_comment_kit",
                                         "stale_contact", "unreadable"])
                self.assertEqual(today.todo(self.root, AS_OF)[-1]["command"], "open kit/sent.json and fix it")
                for path in (settings, sent):
                    if path.is_symlink() or path.exists():
                        path.unlink()
                sent.write_bytes(good)
        self.assertEqual([i["kind"] for i in self.work()], ["request_waiting_on_you", "request_fee_unpaid"])

    def test_no_new_states_matches_prechange_bytes_and_is_read_only(self):
        def state(root):
            return snapshot(root), {p: stat.S_IMODE(os.lstat(p).st_mode) for p in root.rglob("*")}
        writes = [mock.patch.object(Path, name, side_effect=AssertionError(f"Path.{name} called"))
                  for name in ("write_text", "write_bytes", "mkdir", "touch", "chmod")]
        writes += [mock.patch.object(os, name, side_effect=AssertionError(f"os.{name} called"))
                   for name in ("replace", "rename", "chmod", "mkdir", "unlink")]
        writes.append(mock.patch("fcntl.flock", side_effect=AssertionError("lock taken")))
        writes.append(mock.patch("subprocess.run", side_effect=AssertionError("command run")))
        empty = state(self.root)
        with NoWrites(writes):
            self.assertEqual(today.run_cli(self.root, "2026-03-15"), "Nothing due on 2026-03-15.")
            self.assertEqual(today.run_cli(self.root, "2026-03-15", as_json=True), "[]")
        self.assertEqual(state(self.root), empty)
        self.build_tree()
        before = state(self.root)
        expected = {mode: (FIXTURES / f"legacy01.{name}.txt").read_bytes().decode("utf-8")
                    for mode, name in ((False, "text"), (True, "json"))}
        with NoWrites(writes):
            for as_json, fixture in expected.items():
                self.assertEqual(today.run_cli(self.root, "2026-03-15", as_json=as_json)
                                 .replace(str(self.root), "<CAMPAIGN>"), fixture)
        for as_json, fixture in expected.items():  # the real CLI prints the same bytes plus one newline
            code, out, err = run("today", "--directory", self.root, "--as-of", "2026-03-15",
                                 *(["--json"] if as_json else []))
            self.assertEqual((code, out.replace(str(self.root), "<CAMPAIGN>"), err), (0, fixture + "\n", ""))
        self.assertEqual(state(self.root), before)  # bytes, modes and the path set
        self.steps(self.c, [est("2026-03-03", 100), step("clarification_requested", "2026-03-04")])
        self.steps(self.a, [step("determination", "2026-03-11", expected_on="2026-03-14")])
        before = state(self.root)
        with NoWrites(writes):
            self.assertEqual([i["kind"] for i in self.work()], list(NEW))
        self.assertEqual(state(self.root), before)


class NoWrites(contextlib.ExitStack):  # every write, lock and command patch at once, all undone on the way out
    def __init__(self, patches):
        super().__init__()
        self.patches = patches

    def __enter__(self):
        super().__enter__()
        for patch in self.patches:
            self.enter_context(patch)
        return self


if __name__ == "__main__":
    unittest.main()
