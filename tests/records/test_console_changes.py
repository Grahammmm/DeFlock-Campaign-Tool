"""GET /changes (DF1117L-14): the selected run against its predecessor, through DF1117L-13's diff. Synthetic trees only."""
import hashlib
import html
import json
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from campaign_tool.records import console_export as ce, console_page_changes as page, console_pages, console_snapshot as snap
from campaign_tool.records.outcomes import analysis_ready
from tests.records.test_outcome_changes import NEW_ID, OLD_ID, write_files

PID = "0123456789abcdef"
OLDER_ID = "run_20290101T000000Z_dddddddd"
OLD_NAME, NEW_NAME = "20300103T000000Z-" + OLD_ID, "20300104T000000Z-" + NEW_ID


class Server:
    def __init__(self, pinner):
        self.pinner = pinner


class Handler:
    def __init__(self, path="/changes", pin=None, pinner=None, project=None, run_id=None):
        self.path, self.pin, self.server, self.sent = path, pin, Server(pinner), []
        self.options = {"project": project, "run_id": run_id}

    def _send(self, status, body, *rest):
        self.sent.append((status, body))


class Hrefs(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.found.append(dict(attrs)["href"])


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        (self.root / "exports").mkdir(parents=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def export(self, name, new, exported_at, mutate=None):
        top = self.root / "exports" / name
        top.mkdir(mode=0o700)
        write_files(top / "run-outputs" / name[17:], new)
        if mutate:
            mutate(top / "run-outputs" / name[17:])
        files = {str(p.relative_to(top)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(top.rglob("*")) if p.is_file()}
        (top / ce.MANIFEST).write_text(json.dumps({"schema": ce.SCHEMA, "run_id": name[17:], "source_project_id": PID,
                                                   "files": files, "exported_at": exported_at}))
        return top

    def get(self, current=NEW_NAME):
        link = self.root / "current"
        if link.is_symlink():
            link.unlink()
        link.symlink_to("exports/" + current)
        pinner = snap.Pinner(str(self.root))
        handler = Handler(pin=pinner.pin(), pinner=pinner)
        self.assertTrue(page.serve(handler))
        self.assertEqual(handler.sent[0][0], 200)
        return html.unescape(handler.sent[0][1])


class Hosted(Base):
    def test_two_installed_exports_show_each_change(self):
        self.export(OLD_NAME, False, "2030-01-09T20:30:00Z")  # exported later than the newer one: install order wins
        self.export(NEW_NAME, True, "2030-01-02T08:05:00Z")
        self.export("20300105T000000Z-" + OLD_ID, False, "2030-01-10T00:00:00Z")  # installed after the pinned one: never chosen
        self.export("20300102T000000Z-" + NEW_ID, True, "2030-01-02T00:00:00Z")  # a re-export of the same run: not older
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        text = self.get()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        for line in ("New statements: 2", "Statements no longer present: 1", "Comparison rows changed: 4", "Requests changed: 3",
                     "New received items: 2", "Gap proposals added: 1", "Gap proposals removed: 2"):
            self.assertIn("<li>" + line + "</li>", text)
        self.assertIn("(exported 00:05 PT, 2030-01-02)", text)  # 08:05Z in January is PST
        self.assertIn("(exported 12:30 PT, 2030-01-09)", text)
        self.assertLess(text.index(NEW_ID), text.index(OLD_ID))  # newest first
        self.assertIn("New %s: vendor, document aaaaaaaaaaaa page 4" % '<a href="/facts?agency=a1">a1</a>', text)
        self.assertIn("Previous run only a1: vendor, document aaaaaaaaaaaa page 2", text)  # a removed source is not linked
        self.assertIn('<a href="/comparison?agency=a1">a1</a>: retention: unknown (missing_or_unqualified_evidence) now gap', text)
        self.assertIn('<a href="/agency?agency=a1">a1</a>: request R1: state: running due: 2030-01-10 determination_at: - now state: past_due_reminder', text)
        self.assertIn("unassigned: request T1", text)
        self.assertNotIn('agency=unassigned', text)
        self.assertIn("a2: item ", text)
        self.assertIn("a1: request R1 proposal ", text)
        for tag in ("<form", "<button", "SECRET", "QUOTE"):
            self.assertNotIn(tag, text)

    def test_first_export_has_no_older_run(self):
        self.export(NEW_NAME, True, "2030-01-02T08:05:00Z")
        (self.root / "exports" / ".staging-x").mkdir()
        text = self.get()
        self.assertIn("first export: nothing to compare", text)
        self.assertNotIn("<li>", text)
        self.assertNotIn("New statements", text)

    def test_broken_older_export_is_unavailable_and_never_skipped(self):
        self.export("20300101T000000Z-" + OLDER_ID, False, "2030-01-01T00:00:00Z")  # a valid, earlier export
        top = self.export(OLD_NAME, False, "2030-01-03T00:00:00Z")
        self.export(NEW_NAME, True, "2030-01-02T08:05:00Z")
        (top / "run-outputs" / OLD_ID / "comparison.json").write_text("{}")  # corrupted after install
        text = self.get()
        self.assertIn("Changes: unavailable: previous_export_invalid", text)
        self.assertNotIn("New statements", text)
        self.assertNotIn("first export", text)

    def test_missing_artifact_makes_only_that_section_unavailable(self):
        self.export(OLD_NAME, False, "2030-01-03T00:00:00Z", mutate=lambda run: (run / "comparison.json").unlink())
        self.export(NEW_NAME, True, "2030-01-02T08:05:00Z")
        text = self.get()
        self.assertIn("<li>Comparison rows changed: unavailable: previous_missing</li>", text)
        self.assertIn("<li>New statements: 2</li>", text)

    def test_unlistable_or_foreign_history_is_unavailable(self):
        self.export(NEW_NAME, True, "2030-01-02T08:05:00Z")
        for n in range(page.SCAN_CAP + 1):
            (self.root / "exports" / ("x%d" % n)).touch()
        self.assertIn("unavailable: history_unavailable", self.get())


class Live(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name)
        self.old, self.new = (self.project / "run-outputs" / r for r in (OLD_ID, NEW_ID))
        write_files(self.old, False)
        write_files(self.new, True)
        self.marker = self.new / "outcomes" / "analysis-ready.json"
        self.marker.write_text(json.dumps({"schema": analysis_ready.SCHEMA, "run_id": NEW_ID}))

    def get(self, run_id=NEW_ID):
        handler = Handler(project=str(self.project), run_id=run_id)
        self.assertTrue(page.serve(handler))
        self.assertEqual(handler.sent[0][0], 200)
        return html.unescape(handler.sent[0][1])

    def test_live_compares_selected_run_to_previous_settled_directory(self):
        os.utime(self.old, (9e9, 9e9))  # a misleading mtime does not change chronology
        before = {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()}
        text = self.get()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.project.rglob("*") if p.is_file()})
        self.assertIn("<li>New statements: 2</li>", text)
        self.assertIn("(started 16:00 PT, 2029-12-31; live run, not exported)", text)
        self.assertIn("(started 16:00 PT, 2030-01-01; live run, not exported)", text)
        self.assertNotIn("exported 0", text)

    def test_unsettled_selected_run_is_unavailable_and_junk_does_not_settle_it(self):
        self.marker.unlink()
        self.assertIn("unavailable: run_unsettled", self.get())
        (self.project / "run-outputs" / "staging-x").mkdir()
        (self.project / "run-outputs" / "run_20300109T000000Z_cccccccc").symlink_to(self.new)
        self.assertIn("unavailable: run_unsettled", self.get())
        self.marker.write_text(json.dumps({"schema": analysis_ready.SCHEMA, "run_id": OLD_ID}))
        self.assertIn("unavailable: run_unsettled", self.get())

    def test_older_selected_run_needs_no_marker_and_first_run_says_so(self):
        self.marker.unlink()
        (self.project / "run-outputs" / "run_20300109T000000Z_cccccccc").mkdir()
        self.assertIn("first run: nothing to compare", self.get(OLD_ID))
        self.assertIn("unavailable: run_unavailable", self.get("run_20300103T000000Z_eeeeeeee"))


class Links(unittest.TestCase):
    def test_change_links_quote_query_values_and_escape_markup(self):
        agency = 'a&b"<i>?x=1#y'
        facts = {"added": [{"agency_id": agency, "field": "<b>f</b>", "document_prefix": "a" * 12, "page": 1}], "removed": []}
        comparison = {"changed": [{"agency_id": agency, "field": "f", "request_id": None,
                                   "before": {"status": "gap", "reason": "r"}, "after": {"status": "meets", "reason": "r"}}]}
        register = {"changed": [{"agency_id": agency, "request_id": "R", "before": {"state": "a", "due": None, "determination_at": None},
                                 "after": {"state": "b", "due": None, "determination_at": None}},
                                {"agency_id": None, "request_id": "T", "before": {"state": "a", "due": None, "determination_at": None},
                                 "after": {"state": "b", "due": None, "determination_at": None}}]}
        result = {"facts": facts, "comparison": comparison, "register": register, "received": {"added": []},
                  "proposals": {"added": [], "removed": []}}
        sides = (("/x/o", OLD_ID, "old"), ("/x/n", NEW_ID, "new"))
        body = page.render_pair(result, sides)
        parser = Hrefs()
        parser.feed(body)
        quoted = "a%26b%22%3Ci%3E%3Fx%3D1%23y"
        self.assertEqual(parser.found, ["/facts?agency=" + quoted, "/comparison?agency=" + quoted, "/agency?agency=" + quoted])
        self.assertNotIn("<i>", body)
        self.assertNotIn("<b>f", body)
        self.assertEqual(len(re.findall(r"<a ", body)), 3)  # the unbound tracker row has no link


class Registered(unittest.TestCase):
    def test_route_is_a_registered_get_page_with_a_nav_entry(self):
        self.assertIn(("Changes", "/changes"), console_pages.nav_links())
        self.assertFalse(page.serve(Handler(path="/changes?x=1")))


if __name__ == "__main__":
    unittest.main()
