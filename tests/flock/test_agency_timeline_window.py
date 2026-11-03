"""RF0550-10: flock_agency.py timeline --since D and an explicit --as-of D window the events; before/after are counted."""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from tests.flock.approval_field import trusted
from tests.flock.test_agency_timeline import BANNED, FEES, HEADER, MOD, SHA, SLUG, STEPS, row

ALL = [["2026-09-01", "sent", "R-1", "-"], ["2026-10-05", "sent", "R-2", "-"], ["2026-10-20", "receipt", "R-1", "-"],
       ["2026-11-10", "extension-notice", "R-1", "-"], ["undated", "closed", "R-2", "-"]]


class TimelineWindowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = self.tmp / "store"
        self.folder = self.store / "agencies" / SLUG
        self.folder.mkdir(parents=True)
        # events on 09-01, 10-05, 10-20, 11-10 and one undated closed (R-2 has no dated closing step)
        self.put("REQUESTS.csv", HEADER + row("R-1", sent="2026-09-01", ev="synthetic ack", rdate="2026-10-20", sha=SHA)
                 + row("R-2", "closed", sent="2026-10-05"))
        self.put("REQUEST-STEPS.csv", STEPS + f"R-1,extension,2026-11-10,x,{SHA},2026-11-10\n")

    def put(self, name, text):
        (self.folder / name).write_text(text, encoding="utf-8")

    def run_tool(self, *extra, today=date(2026, 10, 31)):
        out, err = io.StringIO(), io.StringIO()
        fake = mock.Mock(today=mock.Mock(return_value=today))
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), trusted(), mock.patch.object(MOD, "date", fake):
            code = MOD.main(["timeline", str(self.store), "--agency", SLUG, *extra])
        return code, out.getvalue(), err.getvalue()

    def events(self, out):
        return [ln.split(" | ") for ln in out.splitlines()[1:] if ln[:1] in "0123456789u" and " | " in ln]

    def opened(self, out):
        return [ln for ln in out.splitlines() if ln.startswith("still open")]

    def test_october_window_keeps_october_and_undated_and_counts_the_rest(self):
        code, out, err = self.run_tool("--since", "2026-10-01", "--as-of", "2026-10-31")
        self.assertEqual((code, err), (0, ""), out)
        self.assertEqual(out.splitlines()[0].split(" | ")[:2], [f"agency {SLUG}", "as of 2026-10-31"])
        self.assertEqual(self.events(out), [ALL[1], ALL[2], ALL[4]])
        self.assertEqual(out.splitlines()[-1], "totals | events=3 open=1 errors=0 failing_runs=0 before=1 after=1")
        code, raw, _ = self.run_tool("--since", "2026-10-01", "--as-of", "2026-10-31", "--json")
        data = json.loads(raw)
        self.assertEqual((code, data["since"], data["as_of"], data["before"], data["after"]), (0, "2026-10-01", "2026-10-31", 1, 1))
        self.assertEqual([[e["date"], e["kind"], e["request"], e["detail"]] for e in data["events"]], self.events(out))
        self.assertFalse(BANNED.search(out + raw), out + raw)  # the window counts carry no banned wording

    def test_past_as_of_alone_hides_later_events_and_keeps_the_open_block(self):
        code, out, _ = self.run_tool("--as-of", "2026-09-30")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.events(out), [ALL[0], ALL[4]])
        self.assertTrue(out.splitlines()[-1].endswith(" before=0 after=3"), out)
        _, plain, _ = self.run_tool(today=date(2026, 9, 30))  # same as-of from the default, so no window
        self.assertEqual(self.events(plain), ALL)
        self.assertEqual(self.opened(out), self.opened(plain))
        self.assertEqual(len(self.opened(out)), 1)
        data = json.loads(self.run_tool("--as-of", "2026-09-30", "--json")[1])
        self.assertEqual((data["since"], data["before"], data["after"]), (None, 0, 3))
        self.assertEqual(data["open"], json.loads(self.run_tool("--json", today=date(2026, 9, 30))[1])["open"])

    def test_since_alone_windows_up_to_the_default_as_of(self):
        code, out, _ = self.run_tool("--since", "2026-10-06", today=date(2026, 11, 1))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.events(out), [ALL[2], ALL[4]])
        self.assertTrue(out.splitlines()[-1].endswith(" before=2 after=1"), out)

    def test_no_flags_output_is_unchanged(self):
        for extra in ((), ("--json",)):
            with self.subTest(extra=extra):
                code, out, err = self.run_tool(*extra)
                golden = io.StringIO()
                with contextlib.redirect_stdout(golden), trusted():  # the unwindowed render path, called directly
                    want = MOD.render(SLUG, date(2026, 10, 31), *MOD.timeline(str(self.store), SLUG, date(2026, 10, 31), None),
                                      bool(extra))
                self.assertEqual((code, out, err), (want, golden.getvalue(), ""))
        self.assertEqual(self.events(out := self.run_tool()[1]), ALL)  # 11-10 is after the default as-of and still shown
        self.assertEqual(out.splitlines()[-1], "totals | events=5 open=1 errors=0 failing_runs=0")
        self.assertNotIn("before", out)
        data = json.loads(self.run_tool("--json")[1])
        self.assertEqual(set(data) & {"since", "before", "after"}, set())

    def test_bounds_are_inclusive(self):
        code, out, _ = self.run_tool("--since", "2026-10-05", "--as-of", "2026-10-20")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.events(out), [ALL[1], ALL[2], ALL[4]])
        self.assertTrue(out.splitlines()[-1].endswith(" before=1 after=1"), out)
        code, out, _ = self.run_tool("--since", "2026-10-20", "--as-of", "2026-10-20")
        self.assertEqual((code, self.events(out)), (0, [ALL[2], ALL[4]]))

    def test_bad_window_is_usage_and_prints_nothing(self):
        for extra, why in (
                (("--since", "2026-02-30"), "timeline: --since must be a real YYYY-MM-DD date\n"),
                (("--since", "10/01/2026"), "timeline: --since must be a real YYYY-MM-DD date\n"),
                (("--since", ""), "timeline: --since must be a real YYYY-MM-DD date\n"),
                (("--since", "2026-11-01", "--as-of", "2026-10-01"), "timeline: --since is after --as-of\n"),
                (("--since", "2026-11-01"), "timeline: --since is after --as-of\n"),  # the default as-of is 2026-10-31
                (("--since", "2026-10-01", "--since", "2026-10-02"), None),
                (("--since",), None), (("--since", "2026-10-01", "--json", "--json"), None)):
            with self.subTest(extra=extra):
                code, out, err = self.run_tool(*extra)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(err, why) if why else self.assertTrue(err.startswith("usage: "), err)

    def test_window_with_no_dated_event_left_and_a_source_error(self):
        code, out, _ = self.run_tool("--since", "2026-12-01", "--as-of", "2026-12-31")
        self.assertEqual((code, self.events(out)), (0, [ALL[4]]))
        self.assertEqual(out.splitlines()[-1], "totals | events=1 open=1 errors=0 failing_runs=0 before=4 after=0")
        self.put("FEES.csv", "not,a,fee,file\n")
        code, out, err = self.run_tool("--since", "2026-12-01", "--as-of", "2026-12-31")
        self.assertEqual((code, err, self.events(out)), (1, "", [ALL[4]]))
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("source-error | FEES.csv | ")]), 1, out)
        self.assertEqual(out.splitlines()[-1], "totals | events=1 open=1 errors=1 failing_runs=0 before=4 after=0")
        data = json.loads(self.run_tool("--since", "2026-12-01", "--as-of", "2026-12-31", "--json")[1])
        self.assertEqual((len(data["errors"]), data["errors"][0]["file"], data["before"]), (1, "FEES.csv", 4))

    def test_windowed_json_keeps_schema_review_flag_and_event_detail(self):
        self.put("FEES.csv", FEES + f"R-1,invoice,1250,2026-10-11,inv,{SHA},2026-10-12\nR-1,invoice,1250,2026-10-11,inv,{SHA},2026-10-12\n")
        full = json.loads(self.run_tool("--json")[1])
        code, raw, _ = self.run_tool("--since", "2026-10-01", "--as-of", "2026-10-31", "--json")
        data = json.loads(raw)
        self.assertEqual((code, data["schema"], data["review_required"], data["agency"]), (0, "flock-agency-timeline-v1", True, SLUG))
        self.assertIn({"date": "2026-10-11", "kind": "fee-invoice", "request": "R-1", "detail": "USD 12.50 (recorded twice)"},
                      data["events"])
        self.assertEqual(data["events"], [e for e in full["events"] if e["date"] == "undated" or "2026-10-01" <= e["date"] <= "2026-10-31"])
        self.assertEqual({k: v for k, v in data.items() if k not in ("events", "since", "before", "after")},
                         {k: v for k, v in full.items() if k != "events"})
        self.assertEqual(MOD.window(full["events"], None, date(2026, 10, 31)), (full["events"][:-2] + full["events"][-1:], 0, 1))


if __name__ == "__main__":
    unittest.main()
