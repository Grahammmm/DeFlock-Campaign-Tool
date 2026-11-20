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

from campaign_tool.records import admin_panel as ap, console_codes, console_page_run as page, mail_adoption, mail_resolution_plan, run_stages
from campaign_tool.records.intake import mail_delta
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
    if kind in GAPS:
        gapped(data, GAPS[kind])
    if kind == "oldgaps":  # an older engine: the aggregate needs-G items exist but no breakdown was recorded
        data["stages"][2].update(status="warn", error_code="ADOPTION_CODED_GAPS")
        data["needs_g"] = [{"code": "ADOPTION_CODED_GAPS", "stage": "adoption", "severity": "action", "count": 3}]
        data.update(overall_status="warn", review_required_count=1)
    return data


def gapped(data, partitions):
    """Record each gap stage's outcomes partition and the matching needs-G totals; adoption warns when it has gaps."""
    for index, name in ((1, "delta"), (2, "adoption")):
        rows = partitions[name]
        data["stages"][index]["outcomes"] = [{"code": code, "count": count} for code, count in rows]
        data["stages"][index]["counts"] = dict(input=sum(count for _, count in rows), output=0, failed=0, skipped=0)
        coded = sum(count for code, count in rows if code[:3] in ("GA_", "GB_"))
        blocking = sum(count for code, count in rows if code[:3] == "GB_")
        for code, count in (("ADOPTION_CODED_GAPS", coded), ("ADOPTION_UNACCEPTED_GAP", blocking)):
            if count:
                data["needs_g"].append({"code": code, "stage": name, "severity": "action", "count": count})
        if name == "adoption" and coded:
            data["stages"][index].update(status="warn", error_code="ADOPTION_CODED_GAPS")
            data.update(overall_status="warn", review_required_count=1)


def lines(html):
    """One text line per block of the page body (heading and intro, each stage, new data, links); parts are joined with ' | '.

    A gap table reads as 'Adoption gaps | Gap | Messages | Disposition ; <row cells joined by |> ; ...' (rows end with ' ;').
    """
    body = html[html.index("<h1>"):html.index("</main>")]
    body = re.sub(r"</(?:h1|h2|p|li|caption)>", " | ", body.replace("\n", " "))
    body = re.sub(r"</t[hd]><t[hd]>", " | ", body).replace("</tr>", " ; ")
    body = re.sub(r"</?section>|<nav[^>]*>", "\n", body)
    return [re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", row))).strip(" |;") for row in body.split("\n") if row.strip()]


NAMES = ("sync", "delta", "adoption", "portal", "extract", "digest_gates", "outputs", "notify")
ZERO = "Counts: input 0, output 0, failed 0, skipped 0."
NAV_BOTH = "Previous (older) run Next (newer) run All runs"
MISSING = "breakdown not recorded by this engine"
NO_GAPS = "No coded adoption gaps."
ITEM = "%s (%s): needs action, %d"
CODED = "Some messages could not be adopted and were recorded as known gaps."
UNACCEPTED = "A gap in the mail has not been accepted by a person yet."
TABLE = "Adoption gaps | Gap | Messages | Disposition ; "
# Recorded gap partitions per stage: GA_ accepted, GB_ blocking, GAP_NONE the receipts without a coded gap.
GAPS = {
    "nogaps": {"delta": [], "adoption": [("GAP_NONE", 4)]},
    "mixed": {"delta": [("GB_SOURCE_HASH_MISMATCH", 2), ("GA_UNSUPPORTED_RFC822_PART", 5), ("GB_ORIGINAL_BYTES_CONFLICT", 2),
                        ("GAP_NONE", 4)],
              "adoption": [("GB_OTHER", 1), ("GA_OTHER", 1), ("GB_RESOLUTION_NOT_IN_POLICY", 1),
                           ("GA_ORIGINAL_SCOPE_UNRESOLVED", 2), ("GAP_NONE", 2)]},
    "gapunmapped": {"delta": [], "adoption": [("GB_ZZZ_NEW_GAP_CODE", 2)]},
}


def golden(i, overall="ok", review=0):
    """The expected page of run i when every stage is clean; a test then replaces the lines that differ."""
    head = ("Run details | Read-only. A finished run is machine-valid evidence, not semantic acceptance. | Run %s. Started 2026-03-01 %02d:59:00 PST, "
            "finished 2026-03-01 %02d:00:00 PST (Pacific time). Trigger: manual. Overall: %s. Items to review: %d." % (
                IDS[i], 3 + i, 4 + i, overall, review))
    stages = ["%d. %s: ok | No problem reported. | %s" % (n, name, ZERO) for n, name in enumerate(NAMES, 1)]
    stages[1] += " | " + MISSING  # delta and adoption: the default fixture records no gap breakdown (an older engine)
    stages[2] += " | " + MISSING
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
        fail[2] += " | " + MISSING  # delta and adoption: skipped, nothing recorded
        fail[3] += " | " + MISSING
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

    def gap_page(self, kind, overall="ok", review=0):
        """The page lines of the only run, after proving everything outside the delta and adoption stages is the clean golden."""
        self.put(kind)
        page_lines, base = self.lines(0), golden(0, overall, review)
        base[-1] = "Previous (older) run: none Next (newer) run: none All runs"
        for n in (0, 1, 4, 5, 6, 7, 8, 9, 10):
            self.assertEqual(page_lines[n], base[n])
        return page_lines

    def stage(self, n, status, problem, count, *tail):
        return " | ".join(["%d. %s: %s" % (n, NAMES[n - 1], status), problem,
                           "Counts: input %d, output 0, failed 0, skipped 0." % count] + list(tail))

    # Owner: the gap table with no gaps. Fails if a recorded empty or remainder-only partition shows a table row, shows the
    # legacy missing message, or a GAP_NONE remainder is shown as a gap.
    def test_run_detail_gap_breakdown_no_gaps(self):
        got = self.gap_page("nogaps")
        self.assertEqual(got[2], self.stage(2, "ok", "No problem reported.", 0, NO_GAPS))  # recorded zero input
        self.assertEqual(got[3], self.stage(3, "ok", "No problem reported.", 4, NO_GAPS))  # four receipts, none coded
        self.assertNotIn("GAP_NONE", self.get("?id=" + IDS[0])[1])
        self.assertNotIn("<table", self.get("?id=" + IDS[0])[1])

    # Owner: the mixed table. Fails if rows are not sorted by count then code (accepted before blocking on a tie), a disposition
    # is taken from the stage status or current policy, the Other bucket or a remainder is mis-shown, or wire codes leak.
    def test_run_detail_gap_breakdown_mixed_sorted(self):
        got = self.gap_page("mixed", "warn", 1)
        needs = lambda coded, blocking: (ITEM % (CODED, "ADOPTION_CODED_GAPS", coded), ITEM % (UNACCEPTED, "ADOPTION_UNACCEPTED_GAP", blocking))
        row = lambda text, code, count, disposition: "%s (%s) | %d | %s" % (text, code, count, disposition)
        delta = (row("This nested email part could not be adopted; this gap does not block the run.", "UNSUPPORTED_RFC822_PART", 5, "accepted"),
                 row("The ledger already holds this original with a different size.", "ORIGINAL_BYTES_CONFLICT", 2, "blocking"),
                 row("The message's source file does not match the checksum its receipt declares.", "SOURCE_HASH_MISMATCH", 2, "blocking"))
        adoption = (row("The original's scope is not yet resolved; this gap does not block the run.", "ORIGINAL_SCOPE_UNRESOLVED", 2, "accepted"),
                    row("Additional adoption gaps are grouped here.", "OTHER", 1, "accepted"),
                    row("Additional adoption gaps are grouped here.", "OTHER", 1, "blocking"),
                    row("The resolution policy does not cover this message.", "RESOLUTION_NOT_IN_POLICY", 1, "blocking"))
        self.assertEqual(got[2], self.stage(2, "ok", "No problem reported.", 13, "Needs attention:", *needs(9, 4), TABLE + " ; ".join(delta)))
        self.assertEqual(got[3], self.stage(3, "warn", CODED + " (ADOPTION_CODED_GAPS)", 7, "Needs attention:", *needs(5, 2),
                                            TABLE + " ; ".join(adoption)))  # delta stays ok: the status never sets a disposition
        html = self.get("?id=" + IDS[0])[1]
        self.assertEqual(html.count("<table"), 2)
        for wire in ("GAP_NONE", "GA_", "GB_"):
            self.assertNotIn(wire, html)

    # Owner: unmapped gap codes. Fails if a safe code missing from the table is dropped or shown without its count/disposition.
    def test_run_detail_gap_breakdown_unmapped(self):
        got = self.gap_page("gapunmapped", "warn", 1)
        self.assertEqual(got[3], self.stage(3, "warn", CODED + " (ADOPTION_CODED_GAPS)", 2, "Needs attention:",
                                            ITEM % (CODED, "ADOPTION_CODED_GAPS", 2), ITEM % (UNACCEPTED, "ADOPTION_UNACCEPTED_GAP", 2),
                                            TABLE + "ZZZ_NEW_GAP_CODE: no description yet | 2 | blocking"))

    # Owner: reports without the field. Fails if an old report (even one with the aggregate needs-G item) shows a table or the
    # no-gaps wording instead of the exact missing wording under both stages.
    def test_run_detail_gap_breakdown_old_report(self):
        got = self.gap_page("oldgaps", "warn", 1)
        self.assertEqual(got[2], self.stage(2, "ok", "No problem reported.", 0, MISSING))
        self.assertEqual(got[3], self.stage(3, "warn", CODED + " (ADOPTION_CODED_GAPS)", 0, "Needs attention:",
                                            ITEM % (CODED, "ADOPTION_CODED_GAPS", 3), MISSING))
        self.assertNotIn("<table", self.get("?id=" + IDS[0])[1])

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


# mail_adoption.py gap-code literals with no sentence on purpose, code -> reason (none: every code it emits has one).
GAP_ALLOWED = {}
# Codes mail_adoption relays from mail_delta (source path, MIME) and mail_resolution_plan (planner); each needs a sentence.
RELAYED_DELTA = """INVALID_SOURCE_PATH SOURCE_PATH_ESCAPE SOURCE_IS_OUTPUT INVALID_MIME_CONTENT_TYPE INVALID_RELATED_CONTAINER
AMBIGUOUS_RELATED_CONTENT_ID INVALID_RELATED_CONTENT_ID INVALID_RELATED_START MISSING_RELATED_ROOT RELATED_ROOT_TYPE_MISMATCH INVALID_EML
MIME_PART_LIMIT INVALID_MULTIPART_DISPOSITION UNSUPPORTED_ATTACHED_MULTIPART_PART INVALID_MIME_CONTAINER INVALID_LEAF_DISPOSITION
MIME_PAYLOAD_SIZE_LIMIT AMBIGUOUS_INLINE_TEXT_PART AMBIGUOUS_INLINE_BODY_PART"""
RELAYED_PLAN = """RESOLUTION_EXISTING_BINDING_MISMATCH RESOLUTION_IMPORT_BINDING_MISMATCH RESOLUTION_REQUIRES_UNACCEPTED_ORIGINAL
RESOLUTION_POLICY_RECEIPT_SET_MISMATCH RESOLUTION_NOT_IN_POLICY RESOLUTION_HELD_ORIGINAL_RESOLVED RESOLUTION_DEPENDENCY_CYCLE
RESOLUTION_BINDING_RECEIPT_BLOCKED RESOLUTION_POLICY_RECEIPT_MISMATCH RESOLUTION_DEPENDENCY_BLOCKED"""


def options(node):
    """The string values an expression can take: literals, either branch of a conditional, and '+' compositions of those."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return options(node.body) | options(node.orelse)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return {a + b for a in options(node.left) for b in options(node.right)}
    return set()  # str(error) and other relayed values: covered by the RELAYED_* lists


def gap_literals(source):
    """Upper-cased gap codes in mail_adoption.py: values returned by binding_conflict/mime_precheck and ACCEPTED_GAP_CODES members."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in ("binding_conflict", "mime_precheck"):
            for ret in (n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value is not None):
                found |= options(ret.value)
        elif isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "ACCEPTED_GAP_CODES" for t in node.targets):
            found |= {n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return {code.upper() for code in found}


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

    # Owner: gap code coverage drift. Fails if mail_adoption.py gains a coded-gap literal (a returned code in binding_conflict or
    # mime_precheck, a composed branch, or an ACCEPTED_GAP_CODES member) with no sentence, or a relayed MIME/source/planner code
    # the adopter can pass through loses its sentence or its producer literal.
    def test_every_mail_adoption_gap_code_is_explained_or_allowed(self):
        source = Path(mail_adoption.__file__).read_text(encoding="utf-8")
        found = gap_literals(source)
        self.assertLessEqual({"ORIGINAL_SCOPE_UNRESOLVED", "ORIGINAL_SCOPE_NOT_IN_SCOPE", "SOURCE_UNREADABLE", "UNSUPPORTED_RFC822_PART"}, found)
        self.assertEqual(found - set(console_codes.CODES) - set(GAP_ALLOWED), set())
        self.assertEqual(set(GAP_ALLOWED) & set(console_codes.CODES), set())
        for old, new, expected in (('"source_unreadable"', '"source_brand_new"', "SOURCE_BRAND_NEW"),  # a new returned literal
                                   ('"not_in_scope"', '"brand_new_scope"', "ORIGINAL_SCOPE_BRAND_NEW_SCOPE")):  # a new composed branch
            self.assertEqual(gap_literals(source.replace(old, new)) - found, {expected})
        for module, codes in ((mail_delta, RELAYED_DELTA), (mail_resolution_plan, RELAYED_PLAN)):
            text = Path(module.__file__).read_text(encoding="utf-8")
            for code in codes.split():
                self.assertIn(code, console_codes.CODES)
                self.assertIn('"%s"' % code.lower(), text, code)


if __name__ == "__main__":
    unittest.main()
