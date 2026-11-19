"""DFCON17-10: GET /findings lists every finding in every review state; /findings/review and console_findings.load are unchanged.

Owner contracts: (1) the all-findings page shows approved/rejected/deferred/other-held findings that the review page drops, and
never decides, signs or leaks anything; (2) console_findings.load keeps its exact pre-refactor output; (3) an unreadable analysis
view is said, never shown as 'no findings'. Reader states are controlled by replacing finding_review.list_findings per agency
(its real row shape); one test runs the real reader, route and page over the DF2007-06 analysis fixture.
"""
import html
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from campaign_tool.records import console_findings as cf, console_page_findings as page, finding_review as fr
from tests.records.test_console_export import tree_state
from tests.records.test_console_findings import FindingConsole

HOSTILE = "<script>alert(1)</script> & \"quoted\" " + "x" * 300 + " /home/user/secret/path " + "ab" * 32
SHA = "c" * 64
REAL = fr.list_findings
PAYLOAD = "finding-review-v1\nPRIVATE-PAYLOAD-TEXT"


def row(agency, finding, state, hold, claim, **extra):
    base = {"agency": agency, "finding_id": finding, "review_state": state, "reason": None, "hold_code": hold, "f4_verdict": "confirm",
            "topic": "topic of " + finding, "claim": claim, "fact_ids": "F1", "rule_ids": "", "finding_sha256": SHA[:-1] + finding[-1],
            "payload": PAYLOAD, "facts": [{"fact_id": "F1", "source_sha256": "%02d" % int(finding[-1]) * 32, "page": 3, "quote": "SECRET-QUOTE"}]}
    base.update(extra)
    return base


ROWS = {"alpha-pd": [row("alpha-pd", "N1", "unreviewed", "FINDING_UNREVIEWED", "Waiting claim."),
                     row("alpha-pd", "N2", "approved", None, "Approved claim."),
                     row("alpha-pd", "N3", "rejected", "FINDING_REJECTED", "Rejected claim.", reason="PRIVATE-REASON"),
                     row("alpha-pd", "N4", "deferred", "FINDING_REVIEW_DEFERRED", "Deferred claim.")],
        "beta-sheriff": [  # an early-held row carries no topic, claim, facts, hash or payload; one more has malformed optional metadata
            {"agency": "beta-sheriff", "finding_id": "N5", "review_state": "unreviewed", "reason": None,
             "hold_code": "EVIDENCE_SOURCE_UNKNOWN", "f4_verdict": "unavailable"},
            row("beta-sheriff", "N6", "unreviewed", "FINDING_UNREVIEWED", HOSTILE, facts=[{"fact_id": "F9", "source_sha256": "nothex", "page": "x/y"}])]}


def fake_list(root, *, agency=None):
    if isinstance(ROWS.get(agency), Exception):
        raise ROWS[agency]
    return [dict(r) for r in ROWS[agency]]


def old_load(root, list_findings):
    """The pre-refactor console_findings.load, kept verbatim as the independent expectation."""
    try:
        names = sorted(p.parent.name for p in Path(root).glob("agencies/*/review-config.json"))
        rows, broken = [], 0
        for slug in names:
            try:
                rows += list_findings(root, agency=slug)
            except (fr.FindingReviewError, OSError, ValueError, KeyError, TypeError):
                broken += 1
    except (OSError, ValueError):
        return {"state": "unavailable", "code": "finding_inputs_unavailable"}
    if broken and broken == len(names):
        return {"state": "unavailable", "code": "finding_inputs_unavailable"}
    waiting = [r for r in rows if r.get("hold_code") == "FINDING_UNREVIEWED" and r.get("payload") and r.get("finding_sha256")]
    count = lambda state: sum(1 for r in rows if r["review_state"] == state)  # noqa: E731
    return {"state": "ok", "rows": waiting, "rejected": count("rejected"), "deferred": count("deferred"), "broken": broken,
            "other": len(rows) - len(waiting) - count("rejected") - count("deferred")}


class Handler:
    def __init__(self, path, root):
        self.path, self.server, self.pin, self.sent = path, SimpleNamespace(analysis_root=root, decisions_dir=None), None, None

    def _send(self, status, body, **_):
        self.sent = (status, body)

    def _fail(self, status, *_):
        self.sent = (status, "")

    def csrf_token(self):
        raise AssertionError("a read-only page asks for no token")


def get_findings(root, path="/findings"):
    handler = Handler(path, root)
    assert page.serve(handler) is True
    return handler.sent


def get_review(root):
    handler = Handler("/findings/review", root)
    assert cf.serve(handler) is True
    return handler.sent[1]


class AnalysisRoot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        for slug in ROWS:
            (Path(self.root) / "agencies" / slug).mkdir(parents=True)
            (Path(self.root) / "agencies" / slug / "review-config.json").write_text("{}")
        patcher = mock.patch.object(fr, "list_findings", fake_list)
        patcher.start()
        self.addCleanup(patcher.stop)


class AllFindings(AnalysisRoot):
    def test_all_findings_includes_every_state_without_changing_review(self):
        before = tree_state(self.root)
        status, body = get_findings(self.root)
        text = html.unescape(body)
        self.assertEqual(status, 200)
        for claim in ("Waiting claim.", "Approved claim.", "Rejected claim.", "Deferred claim.", "<script>alert(1)</script>"):
            self.assertIn(claim, text)
        for want in ("6 findings in every review state", "approved (human review complete): 1", "rejected by a human: 1",
                     "review deferred (journal or trust anchor unavailable): 1", "waiting for a human review: 3",
                     "held for another reason (EVIDENCE_SOURCE_UNKNOWN): 1", "no hold: 1", "N5", "alpha-pd", "topic of N2",
                     "record 020202020202, page 3", "record unknown, page unknown", "unknown"):
            self.assertIn(want, text)
        self.assertEqual(body.count("review required; not publishable"), 5)  # every row but the approved one
        self.assertEqual(body.count("review complete; publication is a separate approval"), 1)
        self.assertEqual(body.count('<a href="/findings/review">review it</a>'), 2)  # only N1 and N6 are what /findings/review lists
        for private in ("PRIVATE-REASON", "PRIVATE-PAYLOAD-TEXT", "SECRET-QUOTE"):
            self.assertNotIn(private, body)
        self.assertEqual(tree_state(self.root), before)  # nothing created, locked or written under the root
        self.assertFalse(os.path.lexists(os.path.join(self.root, ".analysis")))
        review = html.unescape(get_review(self.root))  # the review page still lists only what it did
        self.assertIn("2 findings waiting for a human review. 1 rejected, 1 deferred, 2 held for another reason.", review)
        self.assertIn("Waiting claim.", review)
        for absent in ("Approved claim.", "Rejected claim.", "Deferred claim."):
            self.assertNotIn(absent, review)

    def test_load_after_the_refactor_equals_its_pre_refactor_output(self):
        # Catches load being rewired to anything but the unfiltered loop plus its own filter and counts, or a changed key order.
        doc, old = cf.load(self.root), old_load(self.root, fake_list)
        self.assertEqual(list(doc), list(old))
        self.assertEqual(doc, old)
        self.assertEqual(cf.render_body(doc, None), cf.render_body(old, None))
        self.assertEqual((len(doc["rows"]), doc["rejected"], doc["deferred"], doc["other"], doc["broken"]), (2, 1, 1, 2, 0))
        self.assertEqual([r["finding_id"] for r in cf.load_all(self.root)["rows"]], ["N1", "N2", "N3", "N4", "N5", "N6"])
        with mock.patch.dict(ROWS, {"alpha-pd": fr.FindingReviewError("finding_inputs_unavailable")}):
            self.assertEqual(cf.load(self.root), old_load(self.root, fake_list))  # a broken agency is counted identically
            self.assertEqual(cf.load(self.root)["broken"], 1)

    def test_hostile_text_is_escaped_cut_and_never_leaks_a_hash_or_a_path(self):
        status, body = get_findings(self.root)
        self.assertNotIn("<script>", body)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", body)
        self.assertIn("x" * 163 + " …", body)  # the claim stops at its first 200 characters (37 of them are the markup text before the x run)
        self.assertNotIn("x" * 164, body)
        self.assertNotRegex(body, r"(?i)[0-9a-f]{64}")  # no full hash: not the finding's, not a cited record's, not one inside text
        self.assertNotIn("/home/user", body)
        self.assertNotIn(self.root, body)
        full = {"claim": "see /var/lib/x/y and C:\\Users\\a\\b and " + SHA + " end", "agency": "alpha-pd"}
        self.assertEqual(page._text(full["claim"], 200), "see [path withheld] and [path withheld] and [hash withheld] end")


class SourceStates(AnalysisRoot):
    def test_findings_source_states_are_not_empty_success(self):
        self.assertIn("findings: not configured on this console", get_findings(None)[1])
        self.assertNotIn("<table", get_findings(None)[1])
        removed = os.path.join(self.root, "gone")
        status, body = get_findings(removed)  # a configured root that cannot be read is not 'no findings'
        self.assertIn("finding_inputs_unavailable", body)
        self.assertIn("not an empty list", body)
        self.assertNotIn("holds no findings", body)
        self.assertEqual(cf.load_all(removed), {"state": "unavailable", "code": "finding_inputs_unavailable"})
        with mock.patch.object(Path, "glob", side_effect=OSError):  # glob failing is unavailable too
            self.assertEqual(cf.load_all(self.root)["state"], "unavailable")
        with mock.patch.dict(ROWS, {"alpha-pd": fr.FindingReviewError("finding_inputs_unavailable")}):
            partial = html.unescape(get_findings(self.root)[1])  # one broken agency: counted and said, the other still listed
            self.assertIn("1 agency review could not be read", partial)
            self.assertIn("2 findings in every review state", partial)
            with mock.patch.dict(ROWS, {"beta-sheriff": OSError("x")}):
                everything = get_findings(self.root)[1]  # every agency broken: unavailable, not a count of zero
            self.assertIn("finding_inputs_unavailable", everything)
            self.assertNotIn("<table", everything)

    def test_a_valid_root_with_no_agency_files_is_empty_not_unavailable(self):
        empty = tempfile.mkdtemp(dir=self.root)
        self.assertEqual(cf.load_all(empty), {"state": "ok", "rows": [], "broken": 0})
        self.assertIn("holds no findings", get_findings(empty)[1])

    def test_a_real_broken_agency_file_is_unavailable_through_the_real_reader(self):
        with mock.patch.object(fr, "list_findings", REAL), tempfile.TemporaryDirectory() as tmp:  # the genuine reader, an unparsable config
            root = os.path.realpath(tmp)
            config = Path(root) / "agencies" / "alpha-pd" / "review-config.json"
            config.parent.mkdir(parents=True)
            config.write_text("{broken")
            self.assertEqual(cf.load_all(root), {"state": "unavailable", "code": "finding_inputs_unavailable"})
            self.assertIn("finding_inputs_unavailable", get_findings(root)[1])

    def test_a_query_is_refused(self):
        self.assertEqual(get_findings(self.root, "/findings?state=approved")[0], 400)


class RealConsole(FindingConsole):
    def test_the_real_console_serves_the_waiting_finding_on_both_pages_and_links_it(self):
        self.up_findings()
        review = self.get("/findings/review")
        body = self.get("/findings")
        text = html.unescape(body)
        for want in (self.slug, "N1", self.frow()["claim"], "4 findings in every review state", "waiting for a human review: 4",
                     "record %s, page 2" % self.o.digest[:12], "review required; not publishable", 'href="/findings/review"'):
            self.assertIn(want, text)
        self.assertIn('href="/findings"', self.get("/"))  # the registered navigation link
        self.assertNotIn("<form", body)
        self.assertNotIn("<textarea", body)
        self.assertNotIn(self.frow()["finding_sha256"], body)  # neither the hash nor the signing text is shown here
        self.assertNotIn(html.escape(self.frow()["payload"]), body)
        self.assertNotRegex(body, r"(?i)[0-9a-f]{64}")
        self.assertNotIn(str(self.ar), body)
        self.assertIn("1 finding waiting", review)  # the review page is as before
        self.assertEqual(body.count('<a href="/findings/review">review it</a>'), 1)  # only N1 is on it; A1, O1, T1 are held earlier


if __name__ == "__main__":
    unittest.main()
