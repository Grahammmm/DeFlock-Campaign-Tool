"""GET /coverage: the newest run's coverage and its trend over synthetic archived reports (DF1117L-04).

Arithmetic stays with test_run_coverage and archive safety with test_console_runs; these tests own the page."""
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from campaign_tool.records import admin_panel as ap, console_page_coverage as page, console_runs_model as model
from tests.records.test_console_runs import fetch, make
from tests.records.test_console_snapshot import Panel
from tests.records.test_pipeline_health import NOW

REJECTED = "run_20261101T000000Z_deadbeef.json"  # filename and report id disagree: never valid


def cover(i, out=0, skipped=0, gap=0, ocr=0, portal=0, kind="ok", stub=False):
    """A valid report whose coverage inputs are the arguments; kind warn/fail marks the adoption stage."""
    data = make(i)
    adoption = next(s for s in data["stages"] if s["name"] == "adoption")
    adoption["counts"].update(output=out, skipped=skipped)
    if kind != "ok":
        adoption.update(status=kind, error_code="ADOPTION_CODED_GAPS" if kind == "warn" else "ADOPTION_FAILED")
        data["overall_status"] = kind
    wanted = [(gap, "adoption", "ADOPTION_CODED_GAPS"), (ocr, "extract", "AWAITING_OCR"),
              (portal, "portal", "PORTAL_FETCH_DRY_RUN_ONLY"), (int(stub), "extract", "STUB_STAGE_BACKEND")]
    data["needs_g"] = [{"code": c, "stage": s, "severity": "action", "count": n} for n, s, c in wanted if n]
    return data


def text(html):
    return re.sub(r"<[^>]+>", "", html)


def rows(html):
    """Table rows as cell texts (tags stripped), newest first."""
    body = re.findall(r"<tbody>(.*)</tbody>", html, re.S)[0]
    return [[text(c) for c in re.findall(r"<td>(.*?)</td>", row, re.S)] for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)]


def latest(html):
    return {text(k): text(v) for k, v in re.findall(r"<dt>(.*?)</dt><dd>(.*?)</dd>", html, re.S)}


class CoveragePage(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / "t"
        self.reports = self.target / "reports"
        self.reports.mkdir(parents=True)
        self.path = str(self.target / "run-report.json")

    def show(self, *reports, name=None):
        for data in reports:
            (self.reports / (name or data["run_id"] + ".json")).write_text(json.dumps(data))
        return page.render(model.load_history(self.path, NOW))

    # Fails if a count is taken from anywhere but the coverage projection, an actual zero reads as unknown, or a
    # metric or its sentence is dropped.
    def test_latest_warn_shows_projected_values_and_explanations(self):
        html = self.show(cover(1, out=12, skipped=5, gap=4, portal=7, kind="warn"))
        block = latest(html)
        self.assertEqual(sorted(k for k in block if not k.startswith("Stub")), sorted((
            "Adopted: 13", "Adopted this run: 12", "Coded gaps: 4", "Awaiting OCR: 0", "Portal held: 7", "Mode: scheduled")))
        self.assertTrue(all(len(v) > 20 for v in block.values()))  # one plain sentence each
        self.assertEqual(rows(html)[0][1:7], ["scheduled", "13", "12", "4", "0", "7"])
        self.assertNotIn("unknown", html)

    # Fails if a failed adoption stage's synthetic counts leak through, unknown shows as 0 or blank, or any change
    # mark appears on either side of an unknown.
    def test_failed_adoption_is_unknown_and_never_gets_arrows(self):
        html = self.show(cover(1, out=3, skipped=2, gap=1, ocr=1, portal=2), cover(2, out=9, skipped=9, gap=9, ocr=2, portal=7, kind="fail"),
                         cover(3, out=4, skipped=2, gap=1, ocr=3, portal=7))
        block = latest(html)
        self.assertEqual([k for k in block if k.endswith("unknown")], [])  # newest is a good run
        failed = rows(html)[1]
        self.assertEqual(failed[2:5], ["unknown", "unknown", "unknown"])
        self.assertEqual(failed[5:7], ["2 \u2191 up", "7 \u2191 up"])  # extract and portal still known, compared to the oldest
        self.assertEqual(rows(html)[0][2:5], ["5", "4", "1"])  # known beside a failed older run: no marks
        self.assertEqual(rows(html)[0][5:7], ["3 \u2191 up", "7 = same"])
        self.assertEqual(rows(html)[2][2:], ["4", "3", "1", "1", "2", "Details"])  # the oldest has no predecessor
        for f in self.reports.glob("run_*"):
            f.unlink()
        newest_failed = latest(self.show(cover(4, out=1, kind="fail", ocr=1)))  # the newest run itself failed
        for key in ("Adopted: unknown", "Adopted this run: unknown", "Coded gaps: unknown", "Awaiting OCR: 1"):
            self.assertIn(key, newest_failed)

    # Fails if a negative subtraction collapses the other adoption counts to unknown or shows adopted as a number.
    def test_negative_adopted_is_unknown_but_other_adoption_counts_remain(self):
        html = self.show(cover(1, out=2, skipped=4, gap=1), cover(2, out=1, skipped=0, gap=5))
        for key in ("Adopted: unknown", "Adopted this run: 1", "Coded gaps: 5"):
            self.assertIn(key, latest(html))
        self.assertEqual(rows(html)[0][2:5], ["unknown", "1 \u2193 down", "5 \u2191 up"])

    # Fails if a stub run is not labelled (latest block and row) or its copy implies real processing.
    def test_stub_run_is_labelled(self):
        html = self.show(cover(1, out=3, skipped=1, gap=1, stub=True))
        self.assertIn("A stub backend was used, so these counts are not evidence of real processing.", latest(html)["Stub run"])
        self.assertEqual(rows(html)[0][:3], ["2026-11-01 04:59 PST", "scheduled Stub run", "3"])
        for f in self.reports.glob("run_*"):
            f.unlink()
        self.assertNotIn("Stub run", self.show(cover(2, out=1)))  # no stub item, no label

    # Fails if the cap counts rejected files, the 31st run is displayed, its role as baseline is lost, a change mark
    # is wrong, or a lone run gets a mark.
    def test_newest_thirty_valid_rows_compare_against_immediately_older_run(self):
        runs = [cover(i, out=i, skipped=5, gap=3, portal=100 - i) for i in range(31)]
        self.show(make(40), name=REJECTED)  # a rejected newer file takes no slot
        html = self.show(*runs)
        ids = re.findall(r"/run\?id=(run_\w+)", html)
        self.assertEqual(ids, [runs[i]["run_id"] for i in range(30, 0, -1)])
        self.assertNotIn(runs[0]["run_id"], html)
        self.assertIn("Older valid runs not listed: 1.", html)
        self.assertIn("Reports not shown: 1 (invalid", html)
        newest, last = rows(html)[0], rows(html)[-1]
        self.assertEqual(newest[:7], ["2026-11-02 09:59 PST", "scheduled", "32 \u2191 up", "30 \u2191 up", "3 = same", "0 = same", "70 \u2193 down"])
        self.assertEqual(last[:7], ["2026-11-01 04:59 PST", "scheduled", "3 \u2191 up", "1 \u2191 up", "3 = same", "0 = same", "99 \u2193 down"])
        for f in self.reports.glob("run_*"):
            f.unlink()
        self.assertEqual(rows(self.show(runs[1]))[0][2:7], ["3", "1", "3", "0", "99"])  # one valid report: nothing to compare

    # Fails if the unconfigured, unavailable, empty and rejected-only states are confused, or any invents a summary.
    def test_not_configured_unavailable_and_no_valid_reports_are_distinct(self):
        none = page.render(model.load_history(None, NOW))
        self.assertIn("coverage: not configured on this console", none)
        self.reports.rmdir()
        missing = page.render(model.load_history(self.path, NOW))
        self.assertIn("Coverage: unavailable (reports_missing)", missing)
        self.reports.mkdir()
        empty = self.show()
        self.assertIn("Coverage: no completed runs yet.", empty)
        rejected = self.show(make(1), name=REJECTED)
        self.assertIn("Coverage: no valid reports to show.", rejected)
        self.assertIn("Reports not shown: 1", rejected)
        for html in (none, missing, empty, rejected):
            self.assertNotIn("<table", html)
            self.assertNotIn("Newest run", html)


class CoveragePages(Panel):
    # Fails if export and live panels read different archives or render differently, the page is not in More, a query
    # or POST reaches it, or its detail links do not resolve.
    def test_export_and_project_modes_show_same_coverage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        reports = Path(tmp.name) / "t" / "reports"
        reports.mkdir(parents=True)
        for i in range(3):
            data = cover(i, out=i + 1, skipped=1, gap=1, portal=5, stub=i == 1)
            (reports / (data["run_id"] + ".json")).write_text(json.dumps(data))
        report = str(reports.parent / "run-report.json")
        live = ap.build_server(str(self.live), "run_1", actor="operator", system=False, run_report_path=report)
        thread = threading.Thread(target=live.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (live.shutdown(), live.server_close(), thread.join()))
        self.make_export()
        self.up(run_report_path=report)
        pages = []
        for server in (live, self.server):
            status, html = fetch(server, "/coverage")
            self.assertEqual(status, 200)
            self.assertEqual(fetch(server, "/coverage?x=1")[0], 404)
            self.assertIn('<a href="/coverage" aria-current="page">Coverage</a>', html)
            pages.append(re.findall(r"<dl>.*?</dl>|<table.*?</table>", html, re.S))
        self.assertEqual(len(pages[0]), 2)
        self.assertEqual(pages[0], pages[1])
        self.assertIn("Stub run", pages[0][1])
        self.assertNotEqual(self.request("POST", "/coverage", {})[0], 200)
        for server in (live, self.server):
            link = re.findall(r'href="(/run\?id=run_\w+)"', fetch(server, "/coverage")[1])[0]
            self.assertEqual(fetch(server, link)[0], 200)
