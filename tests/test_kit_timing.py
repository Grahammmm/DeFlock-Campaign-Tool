"""kit-timing: private calendar-day response-time report from kit/sent.json. Offline, synthetic, injected dates."""
import contextlib
import csv
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from campaign_tool import kit_timing  # noqa: E402
from tests.test_kit_status_csv import run  # noqa: E402

TODAY = date(2026, 4, 30)
NOTE = "calendar days; provisional; not a legal deadline"
CSV_HEADER = ["row_type", "agency_id", "request", "name", "metric", "start_on", "end_on", "days", "open", "count",
              "median", "note"]
A, B = "ca-alpha-police", "ca-beta-sheriff"
MALFORMED = "kit/sent.json unreadable: malformed timing steps"


def entry(agency, sent_on, steps=(), request="main", **extra):
    stem = agency if request == "main" else f"{agency}--{request}"
    return {"sent_on": sent_on, "channel": "email", "draft": f"requests/{stem}.md", "draft_sha256": "0" * 64,
            "recorded_at": f"{sent_on}T12:00:00+00:00", "steps": [dict(s) if isinstance(s, dict) else s for s in steps], **extra}


def key(agency, request="main"):
    return agency if request == "main" else f"{agency}#{request}"


def step(on, kind, **extra):
    return {"on": on, "kind": kind, "recorded_at": f"{on}T12:00:00+00:00", **extra}


def tree(root):
    return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
            for p in sorted(Path(root).rglob("*"))}


class Base(unittest.TestCase):
    def setUp(self):
        for target in ("urllib.request.urlopen", "socket.socket"):
            patcher = mock.patch(target, side_effect=AssertionError("network used"))
            patcher.start()
            self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-kit-timing-")
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.root = self.tmp / "campaign"
        (self.root / "kit").mkdir(parents=True)
        self.private = self.tmp / "private"
        self.private.mkdir()

    def sent(self, *items):  # items: (agency, request, entry)
        requests = {key(agency, request): value for agency, request, value in items}
        (self.root / "kit" / "sent.json").write_text(json.dumps({"schema_version": 1, "requests": requests}))

    def names(self, **names):
        (self.root / "kit" / "agencies.json").write_text(json.dumps(
            [{"agency_id": agency, "name": name, "selected": True, "kind": "police"} for agency, name in names.items()]))

    def report(self, as_of=None, today=TODAY):
        return kit_timing.report(self.root, as_of, today=today)

    def rows(self, **kwargs):
        return {(r["agency_id"], r["request"]): r for r in self.report(**kwargs)["requests"]}

    def metrics(self, agency, **kwargs):
        return next(a for a in self.report(**kwargs)["agencies"] if a["agency_id"] == agency)["metrics"]


class KitTimingTests(Base):
    def test_endpoint_days_and_missing_zero(self):
        self.sent(
            (A, "main", entry(A, "2026-03-02", [step("2026-03-02", "acknowledged"), step("2026-03-12", "determination"),
                                                step("2026-03-20", "produced", partial=True),
                                                step("2026-03-30", "produced"), step("2026-04-10", "produced")])),
            (A, "determ", entry(A, "2026-03-02", [step("2026-03-12", "determination")], "determ")),
            (A, "partial", entry(A, "2026-03-02", [step("2026-03-20", "produced", partial=True)], "partial")),
            (A, "denied", entry(A, "2026-03-02", [step("2026-03-05", "denied")], "denied")),
            (A, "closed", entry(A, "2026-03-02", [step("2026-03-05", "acknowledged"), step("2026-03-06", "closed")],
                                "closed")),
            (A, "none", entry(A, "2026-03-02", [], "none")))
        rows = self.rows()

        def days(request):
            return [rows[(A, request)][m] for m in kit_timing.SCALARS]
        self.assertEqual(days("main"), [0, 10, 18, 28, None])  # zero is a day count; the later production does not move final
        self.assertEqual(days("determ"), [10, 10, None, None, 59])  # determination alone and ...
        self.assertEqual(days("partial"), [18, None, 18, None, 59])  # ... partial alone stay open
        self.assertEqual(days("denied"), [3, None, None, None, None])  # denial is not a determination; it stops the age
        self.assertEqual(days("closed"), [3, None, None, None, None])
        self.assertEqual(days("none"), [None, None, None, None, 59])
        self.assertEqual(rows[(A, "main")]["sent_on"], "2026-03-02")

    def test_organizer_steps_do_not_count_as_first_agency_response(self):
        noisy = {"note": "SECRET-NOTE", "reference": "REF-1", "contact": "someone@example.invalid"}
        estimate = lambda on, cents, **extra: step(on, "fee_estimate", amount_cents=cents, **extra)  # noqa: E731

        def history(cents, paid_note):
            return [(A, "main", entry(A, "2026-03-01", [estimate("2026-03-03", cents), step("2026-03-05", "fee_paid",
                                                                                          amount_cents=cents, note=paid_note)],
                                      **noisy)),
                    (A, "waive", entry(A, "2026-03-01", [estimate("2026-03-04", cents), step("2026-03-06", "fee_waived")],
                                       "waive")),
                    (A, "ask", entry(A, "2026-03-01", [step("2026-03-04", "clarification_requested"),
                                                       step("2026-03-06", "clarified"), step("2026-03-08", "determination")],
                                     "ask"))]
        self.sent(*history(51234, "PAID-NOTE"))
        rows = self.rows()
        # the agency's estimate (not the organizer's payment or waiver) and its clarification request are the first steps
        self.assertEqual([rows[(A, r)]["first_step_days"] for r in ("main", "waive", "ask")], [2, 3, 3])
        self.assertEqual(rows[(A, "ask")]["determination_days"], 7)
        report = self.report()
        out = self.private / "t.csv"
        kit_timing.write_csv(self.root, out, report)
        exported = out.read_text() + kit_timing.format_report(report) + json.dumps(report)
        for secret in ("SECRET-NOTE", "REF-1", "someone@example.invalid", "PAID-NOTE", "recorded_at", "0" * 64, "51234"):
            self.assertNotIn(secret, exported)
        self.sent(*[(a, r, {k: v for k, v in e.items() if k not in noisy}) for a, r, e in history(700, "OTHER-NOTE")])
        self.assertEqual(self.report(), report)  # notes, reference, contact, amount and recorded_at do not set any span
        # organizer-only tuples never become the first agency step (the calculator, independent of replay admission)
        organizer = [(kind, date(2026, 3, 3 + i), False) for i, kind in enumerate(kit_timing.ORGANIZER)]
        row = kit_timing._row(A, "main", A, entry(A, "2026-03-01"), organizer, TODAY)
        self.assertIsNone(row["first_step_days"])

    def test_waits_separate_never_subtracted(self):
        self.sent(
            (A, "main", entry(A, "2026-03-01", [
                step("2026-03-02", "acknowledged"), step("2026-03-05", "clarification_requested"),
                step("2026-03-10", "clarified"), step("2026-03-12", "clarification_requested"),
                step("2026-03-13", "extension"), step("2026-03-20", "determination", expected_on="2026-04-15")])),
            (A, "same", entry(A, "2026-03-01", [
                step("2026-03-05", "clarification_requested"), step("2026-03-05", "clarified"),
                step("2026-03-06", "extension"), step("2026-03-06", "produced")], "same")),
            (A, "denied", entry(A, "2026-03-01", [step("2026-03-02", "extension"), step("2026-03-09", "denied")],
                                "denied")),
            (A, "closed", entry(A, "2026-03-01", [step("2026-03-02", "clarification_requested"),
                                                  step("2026-03-04", "closed")], "closed")),
            (A, "expiry", entry(A, "2026-03-01", [
                step("2026-03-02", "clarification_requested"),
                step("2026-03-03", "produced", partial=True, expected_on="2026-03-08",
                     download_expires_on="2026-03-05")], "expiry")))
        rows = self.rows()
        span = lambda start, end, days, open_: {"start_on": start, "end_on": end, "days": days, "open": open_}  # noqa: E731
        main = rows[(A, "main")]
        self.assertEqual(main["extension_spans"], [span("2026-03-13", "2026-03-20", 7, False)])
        self.assertEqual(main["clarification_spans"], [span("2026-03-05", "2026-03-10", 5, False),
                                                      span("2026-03-12", "2026-04-30", 49, True)])  # the open cycle runs to as-of
        self.assertEqual((main["determination_days"], main["open_age_days"]), (19, 60))  # waits are never subtracted
        same = rows[(A, "same")]
        self.assertEqual((same["clarification_spans"], same["extension_spans"]),
                         ([span("2026-03-05", "2026-03-05", 0, False)], [span("2026-03-06", "2026-03-06", 0, False)]))
        self.assertEqual(rows[(A, "denied")]["extension_spans"], [span("2026-03-02", "2026-03-09", 7, False)])
        self.assertEqual(rows[(A, "closed")]["clarification_spans"], [span("2026-03-02", "2026-03-04", 2, False)])
        self.assertEqual(rows[(A, "expiry")]["clarification_spans"], [span("2026-03-02", "2026-04-30", 59, True)])
        self.assertEqual(rows[(A, "expiry")]["first_production_days"], 2)  # a stated date or expiry ends no wait

    def test_counts_medians_even_odd_empty(self):
        finals = {"r1": "2026-03-01", "r2": "2026-03-03", "r3": "2026-03-04", "r4": "2026-03-11"}  # 0, 2, 3, 10 days
        items = [(A, r, entry(A, "2026-03-01", [step(on, "produced")], r)) for r, on in finals.items()]
        items.append((A, "r5", entry(A, "2026-03-01", [step("2026-03-02", "extension"), step("2026-03-04", "determination")],
                                     "r5")))
        items.append((A, "r6", entry(A, "2026-03-01", [step("2026-03-02", "clarification_requested")], "r6")))
        items.append((B, "main", entry(B, "2026-03-01", [])))
        self.sent(*items)
        a, b = self.metrics(A), self.metrics(B)
        self.assertEqual(a["final_production_days"], {"count": 4, "median": 2.5})  # even sample, .5 median
        self.assertEqual(a["first_production_days"], {"count": 4, "median": 2.5})
        self.assertEqual(a["determination_days"], {"count": 1, "median": 3})  # nulls excluded; an integer median is an int
        self.assertEqual(a["first_step_days"], {"count": 6, "median": 1.5})  # r1..r4: 0,2,3,10; r5: 1; r6: 1
        self.assertEqual(a["open_age_days"], {"count": 2, "median": 60})  # r5 and r6 only, 60 days each
        self.assertEqual(a["extension_days"], {"count": 1, "median": 2, "open": 0})  # one observation per wait
        self.assertEqual(a["clarification_days"], {"count": 1, "median": 59, "open": 1})  # censored open wait
        self.assertEqual(b["final_production_days"], {"count": 0, "median": None})
        self.assertEqual(b["extension_days"], {"count": 0, "median": None, "open": 0})
        self.assertEqual(b["open_age_days"], {"count": 1, "median": 60})
        zero = [(A, r, entry(A, "2026-03-01", [step("2026-03-01", "produced")], r)) for r in ("z1", "z2", "z3")]
        self.sent(*zero)
        self.assertEqual(self.metrics(A)["final_production_days"], {"count": 3, "median": 0})  # zero is counted
        agencies = self.report()["agencies"]
        self.assertEqual([x["request_count"] for x in agencies], [3])

    def test_as_of_named_and_download_pending(self):
        self.names(**{A: "Alpha Police Department"})
        self.sent(
            (A, "main", entry(A, "2026-03-01", [step("2026-03-03", "acknowledged"), step("2026-03-10", "produced"),
                                                step("2026-03-20", "produced")])),
            (A, "other", entry(A, "2026-03-01", [step("2026-03-12", "denied")], "other")),
            (A, "later", entry(A, "2026-03-15", [step("2026-03-16", "acknowledged")], "later")),
            (B, "main", entry(B, "2026-03-01", [step("2026-03-04", "produced", download_expires_on="2026-03-30")])))
        early = self.rows(as_of="2026-03-09")
        self.assertEqual(sorted(early), [(A, "main"), (A, "other"), (B, "main")])  # sent after the cutoff is omitted
        self.assertEqual([early[(A, "main")][m] for m in kit_timing.SCALARS], [2, None, None, None, 8])
        self.assertEqual(early[(A, "other")]["open_age_days"], 8)  # the denial is after the cutoff: still open then
        on_cutoff = self.rows(as_of="2026-03-10")[(A, "main")]
        self.assertEqual((on_cutoff["first_production_days"], on_cutoff["final_production_days"]), (9, 9))  # equal date kept
        full = self.rows()
        self.assertEqual(full[(A, "main")]["final_production_days"], 9)  # the first final production, not the later one
        self.assertEqual(full[(A, "main")]["name"], "Alpha Police Department")
        self.assertEqual(full[(B, "main")]["name"], B)  # no name recorded: the agency id
        self.assertEqual((full[(A, "other")]["first_step_days"], full[(A, "later")]["first_step_days"]), (11, 1))
        # a link-only final production counts as production even while the download is pending
        pending = full[(B, "main")]
        self.assertEqual((pending["final_production_days"], pending["open_age_days"]), (3, None))
        self.assertEqual(self.report(as_of="2026-03-09")["as_of"], "2026-03-09")
        self.assertIn("separate reminder", (REPO_ROOT / "docs" / "AGENCY-TIMELINE.md").read_text())

    def test_bad_input_no_partial_report(self):
        bad_histories = {
            "nonobject": ["x"], "unknown kind": [step("2026-03-02", "waved")], "missing date": [{"kind": "produced"}],
            "bad date": [step("2026-02-30", "acknowledged")], "before sent": [step("2026-02-28", "acknowledged")],
            "descending": [step("2026-03-05", "acknowledged"), step("2026-03-04", "determination")],
            "future": [step("2026-05-01", "acknowledged")],
            "partial false": [step("2026-03-02", "produced", partial=False)],
            "partial string": [step("2026-03-02", "produced", partial="yes")],
            "partial on ack": [step("2026-03-02", "acknowledged", partial=True)],
            "clarified alone": [step("2026-03-02", "clarified")],
            "clarified twice": [step("2026-03-02", "clarification_requested"), step("2026-03-03", "clarified"),
                                step("2026-03-04", "clarified")],
            "second open request": [step("2026-03-02", "clarification_requested"),
                                    step("2026-03-03", "clarification_requested")],
            "second extension": [step("2026-03-02", "extension"), step("2026-03-03", "extension")],
            "extension after answer": [step("2026-03-02", "determination"), step("2026-03-03", "extension")],
            "after closed": [step("2026-03-02", "acknowledged"), step("2026-03-03", "closed"),
                             step("2026-03-04", "produced")]}
        for name, steps in bad_histories.items():
            for as_of in (None, "2026-03-01"):  # bad data beyond the cutoff still refuses the whole report
                with self.subTest(name, as_of=as_of):
                    self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged")])),
                              (B, "main", entry(B, "2026-03-01", steps)))
                    with self.assertRaisesRegex(ValueError, "^" + MALFORMED + "$"):
                        self.report(as_of=as_of)
        for name, text in {"future sent": entry(A, "2026-05-01"), "not an object": "x"}.items():
            with self.subTest(name):
                self.sent((A, "main", text))
                with self.assertRaisesRegex(ValueError, "kit/sent.json unreadable: "):
                    self.report()
        (self.root / "kit" / "sent.json").write_text(json.dumps({"schema_version": 2, "requests": {}}))
        with self.assertRaisesRegex(ValueError, "kit/sent.json unreadable"):
            self.report()
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "waved")])))
        before = tree(self.root)
        code, out, err = run("kit-timing", "--directory", self.root, "--csv", self.private / "t.csv", "--as-of", "2026-04-30")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("Stopped: " + MALFORMED, err)
        self.assertEqual(list(self.private.iterdir()), [])  # no partial CSV
        self.assertEqual(tree(self.root), before)

    def refuses(self, steps, as_of=None, sent_on="2026-03-01", **kwargs):
        """A valid first agency plus one request carrying ``steps``: the whole report must be refused, generically."""
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged")])),
                  (B, "main", entry(B, sent_on, steps, **kwargs)))
        with self.assertRaisesRegex(ValueError, "^" + MALFORMED + "$"):
            self.report(as_of=as_of)

    def test_duplicate_ack_and_determination_refuse_whole_report(self):
        bad = {"ack same day": [step("2026-03-02", "acknowledged"), step("2026-03-02", "acknowledged")],
               "ack later": [step("2026-03-02", "acknowledged"), step("2026-03-04", "acknowledged")],
               "determination same day": [step("2026-03-05", "determination"), step("2026-03-05", "determination")],
               "determination later": [step("2026-03-05", "determination"), step("2026-03-09", "determination")]}
        for name, steps in bad.items():
            for as_of in (None, "2026-03-01"):
                with self.subTest(name, as_of=as_of):
                    self.refuses(steps, as_of)
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged"), step("2026-03-05", "determination")])))
        row = self.rows()[(A, "main")]
        self.assertEqual((row["first_step_days"], row["determination_days"]), (1, 4))

    def test_settlement_without_estimate_and_first_step_closure_refuse(self):
        estimate = step("2026-03-02", "fee_estimate", amount_cents=500)
        bad = {"paid without estimate": [step("2026-03-02", "fee_paid")],
               "waived without estimate": [step("2026-03-02", "fee_waived")],
               "settled twice": [estimate, step("2026-03-03", "fee_paid"), step("2026-03-04", "fee_waived")],
               "closed first": [step("2026-03-02", "closed")],
               "withdrawn without note": [step("2026-03-02", "acknowledged"), step("2026-03-03", "withdrawn")]}
        for name, steps in bad.items():
            for as_of in (None, "2026-03-01"):
                with self.subTest(name, as_of=as_of):
                    self.refuses(steps, as_of)
        self.sent((A, "closed", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged"), step("2026-03-04", "closed")],
                                      "closed")),
                  (A, "paid", entry(A, "2026-03-01", [estimate, step("2026-03-03", "fee_paid", amount_cents=500)], "paid")),
                  (A, "waived", entry(A, "2026-03-01", [estimate, step("2026-03-03", "fee_waived")], "waived")),
                  (A, "withdrawn", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged"),
                                                           step("2026-03-03", "withdrawn", note="we changed course")],
                                         "withdrawn")))
        rows = self.rows()
        self.assertEqual([rows[(A, r)]["first_step_days"] for r in ("closed", "paid", "waived", "withdrawn")], [1, 1, 1, 1])
        self.assertEqual([rows[(A, r)]["open_age_days"] for r in ("closed", "paid", "waived", "withdrawn")],
                         [None, 60, 60, None])

    def test_malformed_ignored_fields_refuse(self):
        bad = {"negative amount": [step("2026-03-02", "fee_estimate", amount_cents=-1)],
               "bool amount": [step("2026-03-02", "fee_estimate", amount_cents=True)],
               "string amount": [step("2026-03-02", "fee_estimate", amount_cents="5")],
               "amount on acknowledgment": [step("2026-03-02", "acknowledged", amount_cents=5)],
               "malformed expiry": [step("2026-03-02", "produced", download_expires_on="2026-13-01")],
               "expiry before step": [step("2026-03-03", "produced", download_expires_on="2026-03-02")],
               "expiry on acknowledgment": [step("2026-03-02", "acknowledged", download_expires_on="2026-03-09")],
               "expected date before determination": [step("2026-03-02", "acknowledged", expected_on="2026-03-09")],
               "bad recorded_at": [step("2026-03-02", "acknowledged", recorded_at="yesterday")],
               "unknown stored key": [step("2026-03-02", "acknowledged", mystery="x")]}
        for name, steps in bad.items():
            for as_of in (None, "2026-03-01"):
                with self.subTest(name, as_of=as_of):
                    self.refuses(steps, as_of)

    def test_full_history_validated_before_as_of_filter(self):
        later = [step("2026-03-02", "acknowledged"), step("2026-03-20", "determination"), step("2026-03-25", "determination")]
        for named in (False, True):
            if named:
                self.names(**{A: "Alpha", B: "Beta"})
            with self.subTest("bad steps after the cutoff", named=named):
                self.refuses(later, "2026-03-10")
            with self.subTest("bad entry sent after the cutoff", named=named):
                self.refuses([step("2026-03-22", "acknowledged"), step("2026-03-22", "acknowledged")], "2026-03-10",
                             sent_on="2026-03-20")
            with self.subTest("valid first request, bad second", named=named):
                self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged")])),
                          (A, "named", entry(A, "2026-03-01", [step("2026-03-02", "closed")], "named")))
                with self.assertRaisesRegex(ValueError, "^" + MALFORMED + "$"):
                    self.report()
                with self.assertRaisesRegex(ValueError, "^" + MALFORMED + "$"):
                    kit_timing.format_report(self.report(as_of="2026-03-02"))
                self.assertFalse(list(self.private.iterdir()))

    def test_cli_replay_refusal_creates_no_export_and_leaks_no_payload(self):
        sentinel = "PRIVATE-MARKER-77"
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged", note=sentinel),
                                                      step("2026-03-04", "acknowledged", note=sentinel)],
                                    reference=sentinel, contact=sentinel)))
        before = tree(self.tmp)
        out_csv = self.private / "t.csv"
        with mock.patch.object(kit_timing, "write_csv", side_effect=AssertionError("write_csv used")) as write, \
                mock.patch.object(kit_timing, "format_report", side_effect=AssertionError("table built")), \
                mock.patch("campaign_tool.kit_export.write_rows", side_effect=AssertionError("write_rows used")):
            for extra in ((), ("--csv", out_csv), ("--as-of", "2026-03-01", "--csv", out_csv)):
                with self.subTest(extra=[str(e) for e in extra]):
                    code, out, err = run("kit-timing", "--directory", self.root, *extra)
                    self.assertEqual((code, out), (1, ""))
                    self.assertEqual(err, "Stopped: " + MALFORMED + "\n")
                    self.assertNotIn(sentinel, err)
        write.assert_not_called()
        self.assertFalse(out_csv.exists())
        self.assertEqual(tree(self.tmp), before)  # bytes, modes' mtimes and the file set are unchanged

    def test_valid_replay_projection_and_exports_preserve_contract(self):
        estimate = step("2026-03-07", "fee_estimate", amount_cents=2500)
        self.names(**{A: "Alpha Police"})
        self.sent(
            (A, "main", entry(A, "2026-03-01", [
                step("2026-03-02", "acknowledged"), step("2026-03-03", "clarification_requested"),
                step("2026-03-06", "clarified"), step("2026-03-07", "extension"), estimate,
                step("2026-03-08", "fee_paid", amount_cents=2500), step("2026-03-12", "determination",
                                                                         expected_on="2026-04-01"),
                step("2026-03-20", "produced", partial=True), step("2026-03-25", "produced"),
                step("2026-04-02", "produced")], received_on="2026-03-02", receipt_evidence="portal")),
            (A, "zero", entry(A, "2026-03-01", [step("2026-03-01", "produced")], "zero")),
            (B, "main", entry(B, "2026-03-01", [step("2026-03-02", "acknowledged"),
                                                step("2026-03-05", "withdrawn", note="withdrawn by the campaign")])))
        report = self.report()
        main = report["requests"][0]
        self.assertEqual([main[m] for m in kit_timing.SCALARS], [1, 11, 19, 24, None])
        self.assertEqual(main["extension_spans"], [{"start_on": "2026-03-07", "end_on": "2026-03-12", "days": 5, "open": False}])
        self.assertEqual(main["clarification_spans"], [{"start_on": "2026-03-03", "end_on": "2026-03-06", "days": 3,
                                                       "open": False}])
        rows = self.rows()
        self.assertEqual(rows[(A, "zero")]["final_production_days"], 0)
        self.assertEqual((rows[(B, "main")]["first_step_days"], rows[(B, "main")]["open_age_days"]), (1, None))
        early = self.report(as_of="2026-03-10")
        self.assertEqual([r["determination_days"] for r in early["requests"]], [None, None, None])
        self.assertEqual(self.report(), report)  # deterministic repeated reads
        out = self.private / "valid.csv"
        summary = kit_timing.write_csv(self.root, out, report)
        with open(out, encoding="utf-8", newline="") as handle:
            written = list(csv.reader(handle))
        self.assertEqual(written[0], CSV_HEADER)
        self.assertEqual(summary["rows"], len(written) - 1)
        self.assertIn("Alpha Police", kit_timing.format_report(report))
        code, text, err = run("kit-timing", "--directory", self.root, "--as-of", "2026-04-30")
        self.assertEqual((code, err), (0, "Read-only. Nothing was sent.\n"))
        self.assertEqual(text, kit_timing.format_report(report) + "\n")

    def test_empty_and_future_sent_guard_survive(self):
        self.sent((A, "main", entry(A, "2026-03-01", [])))
        self.assertEqual(self.rows()[(A, "main")]["open_age_days"], 60)  # an empty history is valid
        self.sent((A, "main", entry(A, "2026-05-01", [])))  # sent after today, no steps at all
        with self.assertRaisesRegex(ValueError, "^kit/sent.json unreadable: a request was sent after today$"):
            self.report()
        with self.assertRaisesRegex(ValueError, "after today"):
            self.report(as_of="2026-03-01")
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged")])))
        real = kit_timing.kit_track.validate_steps
        with mock.patch.object(kit_timing.kit_track, "validate_steps", wraps=real) as spy:
            self.report(as_of="2026-03-03")
        spy.assert_called_once()
        self.assertEqual(spy.call_args.kwargs, {"today": TODAY})  # the effective day, never the as-of cutoff

    def test_csv_private_exclusive_formula_safe(self):
        weird = ["=cmd", "+x", "-x", "@x", "\tx", "\rx", 'a"b,c']
        agencies = [f"ca-w{i}-police" for i in range(len(weird))]
        self.names(**dict(zip(agencies, weird)))
        items = [(a, "main", entry(a, "2026-03-01", [step("2026-03-03", "acknowledged")])) for a in agencies]
        self.sent(*items)
        report = self.report()
        out = self.private / "t.csv"
        previous = os.umask(0)
        try:
            summary = kit_timing.write_csv(self.root, out, report)
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        data = out.read_bytes()
        self.assertEqual(data.count(b"\r\n"), data.count(b"\n"))
        with open(out, encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertEqual(summary, {"written": str(out.resolve()), "rows": len(rows) - 1, "note": NOTE})
        names = [r[3] for r in rows[1:] if r[0] == "request_metric" and r[4] == "first_step_days"]
        self.assertEqual(names, ["'" + w if w[0] in "=+-@\t\r" else w for w in weird])
        self.assertTrue(all(r[11] == NOTE for r in rows[1:]))

    def test_csv_fixed_row_order(self):  # five scalars, extension then clarification spans, then agency metrics
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "clarification_requested"),
                                                      step("2026-03-04", "extension"), step("2026-03-06", "clarified"),
                                                      step("2026-03-08", "determination")])),
                  (A, "second", entry(A, "2026-03-01", [], "second")))
        self.names(**{A: "Alpha"})
        out = self.private / "order.csv"
        kit_timing.write_csv(self.root, out, self.report(as_of="2026-03-10"))
        with open(out, encoding="utf-8", newline="") as handle:
            rows = [r[:11] for r in csv.reader(handle)][1:]
        out.unlink()
        self.assertEqual(rows[:8], [
            ["request_metric", A, "main", "Alpha", "first_step_days", "", "", "1", "", "", ""],
            ["request_metric", A, "main", "Alpha", "determination_days", "", "", "7", "", "", ""],
            ["request_metric", A, "main", "Alpha", "first_production_days", "", "", "", "", "", ""],
            ["request_metric", A, "main", "Alpha", "final_production_days", "", "", "", "", "", ""],
            ["request_metric", A, "main", "Alpha", "open_age_days", "", "", "9", "", "", ""],
            ["request_span", A, "main", "Alpha", "extension_days", "2026-03-04", "2026-03-08", "4", "no", "", ""],
            ["request_span", A, "main", "Alpha", "clarification_days", "2026-03-02", "2026-03-06", "4", "no", "", ""],
            ["request_metric", A, "second", "Alpha", "first_step_days", "", "", "", "", "", ""]])
        self.assertEqual([r[4] for r in rows if r[0] == "agency_metric"],
                         list(kit_timing.SCALARS) + list(kit_timing.WAITS))
        self.assertEqual(rows[-2], ["agency_metric", A, "", "Alpha", "extension_days", "", "", "", "0", "1", "4"])

    def test_csv_target_refusals_and_failures_leave_nothing_behind(self):
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged")])))
        report = self.report()
        for folder in ("public", "content"):
            (self.root / folder).mkdir()
            (self.private / f"{folder}-alias").symlink_to(self.root / folder)
            for out in (self.root / folder / "t.csv", self.private / f"{folder}-alias" / "t.csv"):
                with self.subTest(out=str(out)), self.assertRaisesRegex(ValueError, f"{folder}/"):
                    kit_timing.write_csv(self.root, out, report)
            self.assertEqual(list((self.root / folder).iterdir()), [])
        existing = self.private / "existing.csv"
        existing.write_text("keep")
        (self.private / "dangling.csv").symlink_to(self.private / "nowhere.csv")
        (self.private / "link.csv").symlink_to(existing)
        for out, words in ((existing, "exists"), (self.private / "dangling.csv", "exists"), (self.private / "link.csv", "exists"),
                           (self.private / "missing" / "t.csv", "does not exist"), (self.private / "t.txt", ".csv")):
            with self.subTest(out=str(out)), self.assertRaisesRegex(ValueError, words):
                kit_timing.write_csv(self.root, out, report)
        self.assertEqual(existing.read_text(), "keep")
        self.assertFalse((self.private / "nowhere.csv").exists())
        self.assertFalse((self.private / "t.txt").exists())
        for patched, error in (("os.fsync", OSError("fsync")), ("csv.writer", OSError("write")),
                               ("os.fchmod", OSError("chmod"))):
            out = self.private / "new.csv"
            with self.subTest(patched), mock.patch(patched, side_effect=error), self.assertRaises(OSError):
                kit_timing.write_csv(self.root, out, report)
            self.assertFalse(out.exists())  # this call's partial file is removed
        with mock.patch("os.open", side_effect=PermissionError("open")), self.assertRaises(PermissionError):
            kit_timing.write_csv(self.root, self.private / "new.csv", report)
        self.assertEqual(sorted(p.name for p in self.private.iterdir()),
                         ["content-alias", "dangling.csv", "existing.csv", "link.csv", "public-alias"])
        self.assertEqual(existing.read_text(), "keep")

    def test_read_only_deterministic_empty(self):
        shutil_root = self.tmp / "bare"
        shutil_root.mkdir()
        empty = {"as_of": TODAY.isoformat(), "note": NOTE, "requests": [], "agencies": []}
        self.assertEqual(kit_timing.report(shutil_root, today=TODAY), empty)  # no kit/ at all
        self.assertFalse((shutil_root / "kit").exists())  # reading never creates it
        self.assertEqual(self.report(), empty)  # kit/ without sent.json
        out = self.private / "empty.csv"
        self.assertEqual(kit_timing.write_csv(self.root, out, empty)["rows"], 0)
        self.assertEqual(list(csv.reader(out.read_text().splitlines())), [CSV_HEADER])  # header only
        self.assertIn("No recorded requests.", kit_timing.format_report(empty))
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged")])))
        before = tree(self.root)
        first, second = self.report(), self.report()
        self.assertEqual(first, second)
        self.assertEqual(kit_timing.format_report(first), kit_timing.format_report(second))
        self.assertEqual(first["requests"][0]["name"], A)  # agencies.json absent: the id
        self.assertEqual(tree(self.root), before)
        for text in ("not json", '{"a": 1}', '[{"agency_id": "x"}]', "[]\xff"):
            (self.root / "kit" / "agencies.json").write_text(text, encoding="utf-8")
            with self.subTest(text), self.assertRaisesRegex(ValueError, "kit/agencies.json unreadable"):
                self.report()
        (self.root / "kit" / "agencies.json").unlink()
        (self.root / "kit" / "real.json").write_text("[]")
        (self.root / "kit" / "agencies.json").symlink_to(self.root / "kit" / "real.json")
        with self.assertRaisesRegex(ValueError, "kit/agencies.json unreadable: not a regular file"):
            self.report()
        (self.root / "kit" / "agencies.json").unlink()
        moved = self.tmp / "realkit"
        (self.root / "kit").rename(moved)
        (self.root / "kit").symlink_to(moved)
        with self.assertRaisesRegex(ValueError, "kit/ must not be a symlink"):
            self.report()

    def test_cli_help_refusals_and_projection_label(self):
        code, out, _ = run("kit-timing", "--help")
        self.assertEqual(code, 0)
        for word in ("--directory", "--as-of", "--csv", "not a legal deadline"):
            self.assertIn(word, out)
        self.assertEqual(run("kit-timing")[0], 2)
        self.assertEqual(run("kit-timing", "--directory", self.root, "--bogus")[0], 2)
        self.assertEqual(run("kit-timing", "--directory", self.root, "--csv")[0], 2)
        self.sent((A, "main", entry(A, "2026-03-01", [step("2026-03-02", "acknowledged"), step("2026-03-05", "produced")])))
        for as_of, words in (("2026-02-30", "--as-of must be a real YYYY-MM-DD date"), ("soon", "--as-of must be a real"),
                             ("2999-01-01", "--as-of must not be in the future")):
            code, out, err = run("kit-timing", "--directory", self.root, "--as-of", as_of)
            self.assertEqual((code, out), (1, ""), as_of)
            self.assertIn("Stopped: " + words, err)
        code, out, err = run("kit-timing", "--directory", self.root, "--as-of", "2026-04-30")
        self.assertEqual((code, err), (0, "Read-only. Nothing was sent.\n"))
        for text in ("Response times (" + NOTE + ")", "As of 2026-04-30", "as-of projection", "not what was known",
                     "never subtracted", A, "first step", "determination", "first production", "final production",
                     "open age", "Per agency", "censored"):
            self.assertIn(text, out)
        csv_out = self.private / "cli.csv"
        code, out, err = run("kit-timing", "--directory", self.root, "--as-of", "2026-04-30", "--csv", csv_out)
        self.assertEqual((code, err), (0, "Read-only. Nothing was sent.\n"))
        table, _, summary = out.partition("\n{")
        self.assertIn("Response times", table)  # the table prints even with --csv, then the new-file summary
        self.assertEqual(json.loads("{" + summary)["written"], str(csv_out.resolve()))
        self.assertEqual(stat.S_IMODE(csv_out.stat().st_mode), 0o600)
        (self.root / "public").mkdir()
        code, out, err = run("kit-timing", "--directory", self.root, "--csv", self.root / "public" / "t.csv")
        self.assertEqual((code, out), (1, ""))  # a refused path prints no table
        self.assertIn("must not be inside the campaign's public/ folder", err)
        code, out, err = run("kit-timing", "--directory", self.root, "--csv", csv_out)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("exists", err)


if __name__ == "__main__":
    unittest.main()
