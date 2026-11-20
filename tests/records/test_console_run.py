"""GET /run?id=: one run explained in plain words, and the code table that explains it (DFCON17-03). Synthetic reports only."""
import ast
import json
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from html import unescape
from pathlib import Path

from campaign_tool.records import admin_panel as ap, console_codes, console_page_run as page, run_stages
from tests.records.test_console_runs import fetch
from tests.records.test_pipeline_health import report_for

BASE = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
# Literals in run_stages.py that match the code grammar but are not stage, needs-G or outcome codes.
ALLOWED = {"RECORDS_RUN_TRIGGER": "name of an environment variable the stage reads"}
IDS = [report_for(BASE + timedelta(hours=i), seq=i)["run_id"] for i in range(3)]


def make(i, kind):
    data = report_for(BASE + timedelta(hours=i), "fail" if kind == "fail" else "ok", seq=i)
    data["mode"] = "manual"
    if kind in ("warn", "unmapped"):
        code = "ZZZ_NEW_STAGE_CODE" if kind == "unmapped" else "EXTRACT_BACKLOG_DEFERRED"
        data["stages"][4].update(status="warn", error_code=code, counts={"input": 3, "output": 2, "failed": 0, "skipped": 1})
        data["needs_g"] = [{"code": "EXTRACT_PRESERVE_PENDING", "stage": "extract", "severity": "info", "count": 1},
                           {"code": "ZZZ_NEW_GAP_CODE" if kind == "unmapped" else "AWAITING_OCR", "stage": "extract",
                            "severity": "action", "count": 2}]
        data["new_data"].update(documents=2, pages=7)
        data.update(overall_status="warn", review_required_count=1)
    if kind == "pending":
        data["stages"][7].update(status="skipped", error_code="NOTIFY_PENDING", duration_seconds=None)
    if kind == "unwritable":
        data["stages"][6].update(status="warn", error_code="OUTPUTS_APPROVAL_QUEUE_UNWRITABLE")
        data["needs_g"] = [{"code": "OUTPUTS_APPROVAL_QUEUE_UNWRITABLE", "stage": "outputs", "severity": "action", "count": 1}]
        data.update(overall_status="warn", review_required_count=1)
    return data


def lines(html):
    """One text line per block of the page body (heading and intro, each stage, new data, links); parts are joined with ' | '."""
    body = html[html.index("<h1>"):html.index("</main>")]
    body = re.sub(r"</(?:h1|h2|p|li)>", " | ", body.replace("\n", " "))
    body = re.sub(r"</?section>|<nav[^>]*>", "\n", body)
    return [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", row))).strip(" |") for row in body.split("\n") if row.strip()]


NAMES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs", "notify")
ZERO = "Counts: input 0, output 0, failed 0, skipped 0."
NAV_BOTH = "Previous (older) run Next (newer) run All runs"


def golden(i, overall="ok", review=0):
    """The expected page of run i when every stage is clean; a test then replaces the lines that differ."""
    head = ("Run details | Read-only. A finished run is machine-valid evidence, not semantic acceptance. | Run %s. Started 2026-03-01 %02d:59:00 PST, "
            "finished 2026-03-01 %02d:00:00 PST (Pacific time). Trigger: manual. Overall: %s. Items to review: %d." % (
                IDS[i], 3 + i, 4 + i, overall, review))
    stages = ["%d. %s: ok | No problem reported. | %s" % (n, name, ZERO) for n, name in enumerate(NAMES, 1)]
    stages[7] += " | The summary notification was sent."
    return [head] + stages + ["New data this run: documents 0, pages 0, extracted units 0, portal releases 0, request status changes 0.", NAV_BOTH]


WARN_EXTRACT = ("5. extract: warn | %s | Counts: input 3, output 2, failed 0, skipped 1. | Needs attention: | A document is extracted but its "
                "original is not preserved yet. (EXTRACT_PRESERVE_PENDING): for information, 1 | %s: needs action, 2")


class RunDetail(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.target = Path(tmp.name) / "t"
        (self.target / "reports").mkdir(parents=True)
        self.path = str(self.target / "run-report.json")

    def put(self, *kinds):
        for i, kind in enumerate(kinds):
            data = make(i, kind)
            (self.target / "reports" / (data["run_id"] + ".json")).write_text(json.dumps(data))

    def get(self, query):
        class Handler:
            sent = None
            options = {"run_report_path": self.path}
            path = "/run" + query

            def _send(self, status, body):
                self.sent = (status, body)

            def _fail(self, status, *_):
                self.sent = (status, "")
        handler = Handler()
        self.assertTrue(page.serve(handler))
        return handler.sent

    def lines(self, i):
        status, html = self.get("?id=" + IDS[i])
        self.assertEqual(status, 200)
        return lines(html)

    # Owner: the stage-by-stage contract. Fails if a stage is omitted or reordered, a code shows without its
    # sentence, counts/needs-G/new-data/notify are lost, an unmapped code is hidden, or adjacent links are swapped.
    def test_run_detail_projects_clean_warn_fail_and_unknown(self):
        self.put("clean", "warn", "fail")
        clean = golden(0)
        clean[-1] = "Previous (older) run: none Next (newer) run All runs"
        self.assertEqual(self.lines(0), clean)
        warn = golden(1, "warn", 1)  # extract warned with two needs-G items; new data counted
        warn[5] = WARN_EXTRACT % ("Some documents were put off to a later run. (EXTRACT_BACKLOG_DEFERRED)",
                                  "A scanned document is waiting for text recognition (OCR). (AWAITING_OCR)")
        warn[9] = "New data this run: documents 2, pages 7, extracted units 0, portal releases 0, request status changes 0."
        self.assertEqual(self.lines(1), warn)
        fail = golden(2, "fail")
        fail[1] = "1. sync: fail | Syncing the mail export failed. (SYNC_FAILED) | " + ZERO
        for n in range(2, 9):
            fail[n] = "%d. %s: skipped | An earlier stage failed, so this stage was skipped. (UPSTREAM_FAILED) | %s" % (n, NAMES[n - 1], ZERO)
        fail[8] += " | Not sent: An earlier stage failed, so this stage was skipped. (UPSTREAM_FAILED)"
        fail[-1] = "Previous (older) run Next (newer) run: none All runs"
        self.assertEqual(self.lines(2), fail)
        self.assertNotIn("was sent", fail[8])
        html = self.get("?id=" + IDS[1])[1]  # adjacent links: older is run 0, newer is run 2
        self.assertEqual(re.findall(r'<a href="/run\?id=(run_\w+)">(Previous|Next)', html), [(IDS[0], "Previous"), (IDS[2], "Next")])

    # Owner: unmapped codes. Fails if a code missing from the table is dropped, echoed as a bare code, or an unsafe token is echoed.
    def test_unmapped_code_is_shown_with_no_description_yet(self):
        self.put("clean", "unmapped")
        got = self.lines(1)
        self.assertIn("5. extract: warn | ZZZ_NEW_STAGE_CODE: no description yet | Counts: input 3, output 2, failed 0, skipped 1. | "
                      "Needs attention: | A document is extracted but its original is not preserved yet. (EXTRACT_PRESERVE_PENDING): "
                      "for information, 1 | ZZZ_NEW_GAP_CODE: no description yet: needs action, 2", got)
        self.assertEqual(console_codes.explain("<script>x</script>"), "Unrecognised code: no description yet")
        self.assertEqual(console_codes.explain(None), "Unrecognised code: no description yet")

    # Owner: the outputs queue code. Fails if the new stage code is hidden or shown without its operator sentence.
    def test_unwritable_queue_code_shows_the_operator_sentence(self):
        self.put("unwritable")
        sentence = ("The records runner cannot write the approval queue folder; an operator must create it with the "
                    "runner as owner.")
        got = self.lines(0)[7]
        self.assertIn("7. outputs: warn | %s (OUTPUTS_APPROVAL_QUEUE_UNWRITABLE)" % sentence, got)
        self.assertIn("%s (OUTPUTS_APPROVAL_QUEUE_UNWRITABLE): needs action, 1" % sentence, got)

    # Owner: notify wording. Fails if a pending or skipped notification is described as sent.
    def test_pending_notification_is_not_called_sent(self):
        self.put("pending")
        notify = self.lines(0)[8]
        self.assertIn("Not sent yet: The summary notification had not been sent when this report was written. (NOTIFY_PENDING)", notify)
        self.assertNotIn("was sent.", notify)

    # Owner: the route contract. Fails if a missing, repeated, extra, malformed or path-like id is accepted as a lookup or a
    # file location, an unknown valid id is not 404, or an unreadable history is shown as an unknown run.
    def test_run_id_requires_exact_query_and_lookup(self):
        self.put("clean")
        bad = ("", "?", "?id=", "?id=" + IDS[0] + "&id=" + IDS[0], "?id=" + IDS[0] + "&x=1", "?x=" + IDS[0], "?id=" + IDS[0] + "%0A",
               "?id=" + IDS[0][:-1], "?id=" + IDS[0].upper(), "?id=../reports/" + IDS[0], "?id=%2e%2e%2f" + IDS[0], "?ID=" + IDS[0])
        for query in bad:
            with self.subTest(query):
                self.assertEqual(self.get(query), (400, ""))
        self.assertEqual(self.get("?id=run_20260101T000000Z_deadbeef"), (404, ""))
        self.assertEqual(self.get("?id=" + IDS[0])[0], 200)  # positive control
        (self.target / "reports" / "run_20260101T000000Z_deadbeef.json").write_text("{not json")  # invalid file: still unknown
        self.assertEqual(self.get("?id=run_20260101T000000Z_deadbeef"), (404, ""))
        (self.target / "reports").rename(self.target / "moved")
        status, html = self.get("?id=" + IDS[0])
        self.assertEqual(status, 200)
        self.assertIn("unavailable (reports_missing)", html)
        self.assertNotIn("Previous (older)", html)
        self.path = str(self.target / "other.json")
        self.assertIn("not configured on this console", self.get("?id=" + IDS[0])[1])

    # Owner: registration. Fails if the page is not served by the real panel, or answers an id the engine would not mint.
    def test_page_is_served_by_the_panel(self):
        self.put("clean", "warn")
        live = ap.build_server(str(self.target), "run_1", actor="operator", system=False, run_report_path=self.path)
        thread = threading.Thread(target=live.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (live.shutdown(), live.server_close(), thread.join()))
        self.assertEqual(fetch(live, "/run")[0], 400)
        self.assertEqual(fetch(live, "/run?id=run_20260101T000000Z_deadbeef")[0], 404)
        status, html = fetch(live, "/run?id=" + IDS[1])
        self.assertEqual(status, 200)
        self.assertIn("EXTRACT_BACKLOG_DEFERRED", html)


def unexplained(source):
    """Code-shaped string literals anywhere in the module (producer positions: result(), needs() and tables) with no table entry."""
    found = {n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and console_codes.CODE_RE.fullmatch(n.value)}
    return found - set(console_codes.CODES) - set(ALLOWED)


class CodeTable(unittest.TestCase):
    # Owner: code coverage drift. Fails if the engine emits a code in run_stages.py that the table cannot explain, or the
    # allow-list hides a code that has a sentence.
    def test_every_run_stages_code_is_explained_or_allowed(self):
        source = Path(run_stages.__file__).read_text(encoding="utf-8")
        self.assertEqual(unexplained(source), set())
        self.assertEqual(set(ALLOWED) & set(console_codes.CODES), set())
        added = source.replace('"OUTPUTS_TIMELINE_UNAVAILABLE"', '"OUTPUTS_BRAND_NEW_UNAVAILABLE"')
        self.assertEqual(unexplained(added), {"OUTPUTS_BRAND_NEW_UNAVAILABLE"})  # the walk sees a new producer literal
        for code, text in console_codes.CODES.items():
            self.assertTrue(console_codes.CODE_RE.fullmatch(code) and text.endswith(".") and code not in text, code)


if __name__ == "__main__":
    unittest.main()
