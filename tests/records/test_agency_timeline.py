"""Synthetic-only tests for the read-only per-agency timeline report (N3 / M-14).

The ledger is built through the canonical store API from the committed spec in
tests/fixtures/n3_agency_timeline (nothing is ever written there). No mailbox,
network, model, credential, subprocess or live path is used.
"""
import ast
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.records import correspondence
from campaign_tool.records.ledger.store import canonical, initialize, ledger
from campaign_tool.records.understanding import agency_timeline

REPO = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO / "tests" / "fixtures" / "n3_agency_timeline"
MODULE_PATH = REPO / "campaign_tool" / "records" / "understanding" / "agency_timeline.py"
TOOL_PATH = REPO / "tools" / "agency_timeline.py"
AS_OF = "2026-10-20T00:00:00Z"
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def load_spec():
    return json.loads((FIXTURE_DIR / "ledger_spec.json").read_text(encoding="utf-8"))


def sentinels():
    return load_spec()["sentinels"]


def digest_for(label):
    return hashlib.sha256(("FICTIONAL N3 ORIGINAL " + label).encode()).hexdigest()


def private_dir(prefix):
    """Owner-only, link-free temporary directory (resolve macOS-style aliases)."""
    holder = tempfile.TemporaryDirectory(prefix=prefix)
    base = Path(holder.name).resolve()
    base.chmod(0o700)
    return holder, base


def build_ledger(path, spec=None):
    """Create a synthetic canonical ledger at PATH through the store API only."""
    spec = load_spec() if spec is None else spec
    initialize(path)
    digests = {item["label"]: digest_for(item["label"]) for item in spec["originals"]}
    with ledger(path) as con:
        for item in spec["agencies"]:
            con.execute("INSERT INTO agencies(id,name,jurisdiction) VALUES(?,?,?)",
                        (item["id"], item["name"], item["jurisdiction"]))
        for item in spec["requests"]:
            con.execute("INSERT INTO requests(id,agency_id,external_ref,submitted_at,determination_due_at,"
                        "determination_at,production_at,status) VALUES(?,?,?,?,?,?,?,?)",
                        (item["id"], item["agency"], item["external_ref"], item["submitted_at"],
                         item["determination_due_at"], item["determination_at"], item["production_at"],
                         item["status"]))
        for item in spec["originals"]:
            raw = ("FICTIONAL N3 ORIGINAL " + item["label"]).encode()
            con.execute("INSERT INTO originals(sha256,bytes,mime_detected,role,scope,preservation_status,"
                        "provenance_json) VALUES(?,?,?,?,?,?,?)",
                        (digests[item["label"]], len(raw), item["mime"], "synthetic",
                         item.get("scope", "in_scope"), "verified", "{}"))
        for item in spec["occurrences"]:
            con.execute("INSERT INTO occurrences(id,original_sha256,kind,source_ref,parent_occurrence_id,"
                        "acquired_at,acquisition_method,evidence) VALUES(?,?,?,?,?,?,?,?)",
                        (item["id"], digests[item["original"]], item["kind"],
                         "synthetic:N3SENTINELFILE-" + item["id"] + ".pdf", item["parent"], AS_OF,
                         "synthetic_fixture", canonical({"locator": "mime:0/N3SENTINELLOCATOR"})))
        for item in spec["mail"]:
            digest = digests[item["original"]]
            message_id = "<" + item["id"] + ".n3-sentinel-addr@synthetic.invalid>"
            projection = {"schema": "verified-eml-headers-v1", "source_original_sha256": digest,
                          "parser_defects": [],
                          "fields": [["Message-ID", message_id],
                                     ["From", "Fictional Sender <n3-sentinel-addr@synthetic.invalid>"],
                                     ["To", "Fictional Office <n3-sentinel-addr-to@synthetic.invalid>"],
                                     ["Subject", "N3SENTINELSUBJECT fictional " + item["id"]],
                                     ["X-Synthetic-Body", "N3SENTINELBODY fictional text"]]}
            con.execute("INSERT INTO mail_messages(id,account,folder,uidvalidity,uid,message_id,eml_sha256,"
                        "headers,received_at,account_provenance_status) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (item["id"], "synthetic-account", item["folder"], 1, item["uid"], message_id,
                         digest, canonical(projection), item["received_at"], "configured_namespace_unattested"))
        for item in spec["joins"]:
            digest = digests[item["original"]]
            con.execute("INSERT INTO joins(id,original_sha256,agency_id,request_id,join_type,evidence,status,"
                        "blocked_reason) VALUES(?,?,?,?,?,?,?,?)",
                        (item["id"], digest, item["agency"], item["request"], "declared_request_reference",
                         canonical({"source_sha256": digest, "locator": "mime:0/header:N3SENTINELLOCATOR"}),
                         item["status"], "synthetic N3SENTINELBODY" if item["status"] == "blocked" else None))
        for item in spec["portal_items"]:
            con.execute("INSERT INTO portal_items(id,portal_host,request_id,item_id,agency_id,title,"
                        "notice_occurrence_id,retrieval_status,original_sha256) VALUES(?,?,?,?,?,?,?,?,?)",
                        (item["id"], "portal.n3-sentinel.invalid", item["request_ref"], item["item_id"],
                         item["agency"], "N3SENTINELTITLE fictional", None, item["retrieval_status"],
                         digests[item["original"]] if item["original"] else None))
        con.commit()
    return path


def database_state(path):
    with sqlite3.connect(path) as con:
        logical = "\n".join(con.iterdump())
    return hashlib.sha256(Path(path).read_bytes()).hexdigest(), logical


def load_tool():
    spec = importlib.util.spec_from_file_location("n3_agency_timeline_tool", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(result, agency_id):
    return next(item for item in result["agencies"] if item["agency_id"] == agency_id)


class LedgerCase(unittest.TestCase):
    def setUp(self):
        holder, self.base = private_dir("synthetic-n3-timeline-")
        self.addCleanup(holder.cleanup)
        self.path = build_ledger(self.base / "ledger.sqlite")

    def execute(self, sql, values=()):
        with ledger(self.path) as con:
            con.execute(sql, values)
            con.commit()


class FocusedTimelineTests(LedgerCase):
    def test_table_is_exact(self):
        result = agency_timeline.report(self.path)
        expected = (FIXTURE_DIR / "expected_table.txt").read_text(encoding="utf-8")
        self.assertEqual(agency_timeline.render_table(result),
                         expected.replace("<snapshot_sha256>", result["snapshot_sha256"]))

    def test_rows_cover_three_agencies_and_one_unknown_bucket_in_order(self):
        result = agency_timeline.report(self.path)
        self.assertEqual([(r["agency_id"], r["bucket"]) for r in result["agencies"]],
                         [("agency-alpha", "agency"), ("agency-bravo", "agency"),
                          ("agency-charlie", "agency"), (None, "unknown")])
        self.assertEqual(result["schema"], "agency-timeline-report-v1")
        self.assertIs(result["review_required"], True)
        self.assertEqual(result["semantic_assessment"], "NOT_ASSESSED")

    def test_months_are_exact_in_utc_including_offset_boundary(self):
        result = agency_timeline.report(self.path)
        self.assertEqual(row(result, "agency-alpha")["months"],
                         [{"month": "2026-07", "sent": 1, "received": 1},
                          {"month": "2026-08", "sent": 0, "received": 1}])
        # 2026-09-01T01:30+02:00 is 2026-08-31T23:30Z: counted in August.
        self.assertEqual(row(result, "agency-bravo")["months"], [{"month": "2026-08", "sent": 1, "received": 1}])
        self.assertEqual(row(result, "agency-bravo")["last_contact_at"], "2026-08-31T23:30:00Z")
        self.assertEqual(row(result, "agency-charlie")["months"], [{"month": "2026-10", "sent": 0, "received": 1}])
        self.assertEqual(row(result, None)["months"], [{"month": "2026-09", "sent": 0, "received": 1}])
        self.assertEqual(result["totals"]["months"],
                         [{"month": "2026-07", "sent": 1, "received": 1},
                          {"month": "2026-08", "sent": 1, "received": 2},
                          {"month": "2026-09", "sent": 0, "received": 1},
                          {"month": "2026-10", "sent": 0, "received": 1}])

    def test_unresolved_join_and_unknown_domain_counts_are_exact(self):
        result = agency_timeline.report(self.path)
        self.assertEqual({r["agency_id"]: r["unresolved_joins"] for r in result["agencies"]},
                         {"agency-alpha": 0, "agency-bravo": 1, "agency-charlie": 1, None: 1})
        self.assertEqual({r["agency_id"]: r["unknown_domain_messages"] for r in result["agencies"]},
                         {"agency-alpha": 0, "agency-bravo": 0, "agency-charlie": 0, None: 1})
        self.assertEqual(result["totals"]["unresolved_joins"], 3)
        self.assertEqual(result["gaps"]["scope_excluded_joins"], 1)

    def test_totals_equal_the_sum_of_rows(self):
        result = agency_timeline.report(self.path)
        for name in agency_timeline.ROW_COUNTS:
            self.assertEqual(result["totals"][name], sum(r[name] for r in result["agencies"]), name)
        merged = {}
        for item in result["agencies"]:
            for month in item["months"]:
                cell = merged.setdefault(month["month"], {"sent": 0, "received": 0})
                cell["sent"] += month["sent"]
                cell["received"] += month["received"]
        self.assertEqual(result["totals"]["months"], [{"month": m, **merged[m]} for m in sorted(merged)])
        dated = sum(m["sent"] + m["received"] for m in result["totals"]["months"])
        totals = result["totals"]
        self.assertEqual(dated, totals["messages_sent"] + totals["messages_received"] - totals["undated_messages"])
        self.assertEqual(totals["messages_sent"] + totals["messages_received"], result["gaps"]["visible_messages"])
        self.assertEqual(result["gaps"]["visible_messages"] + result["gaps"]["scope_excluded_messages"],
                         result["gaps"]["mail_messages_total"])
        self.assertEqual(totals["request_count"], sum(len(r["requests"]) for r in result["agencies"]))
        self.assertEqual((totals["first_contact_at"], totals["last_contact_at"]),
                         ("2026-07-03T16:05:00Z", "2026-10-01T12:00:00Z"))

    def test_snapshot_sha256_is_present_stable_and_equals_projection_binding(self):
        first = agency_timeline.report(self.path)
        second = agency_timeline.report(self.path)
        self.assertRegex(first["snapshot_sha256"], HEX64)
        self.assertEqual(canonical(first), canonical(second))
        self.assertEqual(first["snapshot_sha256"],
                         correspondence.report(self.path, as_of=AS_OF)["snapshot_sha256"])
        self.execute("UPDATE requests SET status='closed' WHERE id='req-bravo-1'")
        changed = agency_timeline.report(self.path)
        self.assertNotEqual(changed["snapshot_sha256"], first["snapshot_sha256"])
        self.assertEqual(row(changed, "agency-bravo")["requests"][0]["status"], "closed")

    def test_request_rows_reproduce_recorded_status_and_dates(self):
        result = agency_timeline.report(self.path)
        self.assertEqual(row(result, "agency-alpha")["requests"], [{
            "request_id": "req-alpha-1", "status": "awaiting_production",
            "submitted_at": "2026-07-03T16:00:00Z", "determination_due_at": "2026-07-20T00:00:00Z",
            "determination_at": "2026-07-18T17:30:00Z", "production_at": None,
            "linked_messages": 2, "last_linked_message_at": "2026-07-05T17:00:00Z"}])
        self.assertEqual(row(result, "agency-bravo")["requests"], [{
            "request_id": "req-bravo-1", "status": "open", "submitted_at": "2026-08-05",
            "determination_due_at": None, "determination_at": None, "production_at": None,
            "linked_messages": 1, "last_linked_message_at": "2026-08-05T15:00:00Z"}])
        self.assertEqual(row(result, "agency-charlie")["requests"], [])

    def test_shared_attachments_count_every_delivery_occurrence(self):
        result = agency_timeline.report(self.path)
        self.assertEqual({r["agency_id"]: r["attachment_occurrences"] for r in result["agencies"]},
                         {"agency-alpha": 2, "agency-bravo": 1, "agency-charlie": 0, None: 1})
        self.assertEqual(result["gaps"]["distinct_attachment_originals"], 3)
        self.assertEqual(result["gaps"]["scope_excluded_occurrences"], 2)

    def test_portal_items_and_contact_times_per_agency(self):
        result = agency_timeline.report(self.path)
        self.assertEqual({r["agency_id"]: (r["portal_items"], r["portal_items_unretrieved"])
                          for r in result["agencies"]},
                         {"agency-alpha": (1, 1), "agency-bravo": (1, 0), "agency-charlie": (0, 0), None: (1, 1)})
        alpha = row(result, "agency-alpha")
        self.assertEqual((alpha["first_contact_at"], alpha["last_contact_at"]),
                         ("2026-07-03T16:05:00Z", "2026-08-18T09:00:00Z"))
        charlie = row(result, "agency-charlie")
        self.assertEqual((charlie["messages_received"], charlie["undated_messages"]), (2, 1))
        self.assertEqual(charlie["first_contact_at"], charlie["last_contact_at"])

    def test_no_address_subject_filename_name_or_body_text_in_any_output(self):
        holder, outputs = private_dir("synthetic-n3-out-")
        self.addCleanup(holder.cleanup)
        tool = load_tool()
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = tool.main(["--ledger", str(self.path), "--output-dir", str(outputs / "report")])
        self.assertEqual(code, 0)
        texts = [stream.getvalue()] + [p.read_text(encoding="utf-8") for p in sorted((outputs / "report").iterdir())]
        self.assertEqual(len(texts), 3)
        for text in texts:
            self.assertNotIn("@", text)
            for needle in sentinels():
                self.assertNotIn(needle.casefold(), text.casefold())
            self.assertNotIn(str(self.base), text)


class ReadOnlyTests(LedgerCase):
    @contextmanager
    def write_probe(self):
        """Test double around the reader's ledger open: records the requested mode
        and attempts one rolled-back write on the connection the reader receives."""
        calls, probes = [], []
        current = correspondence.ledger

        @contextmanager
        def spy(path, **kwargs):
            calls.append(kwargs.get("readonly"))
            with current(path, **kwargs) as con:
                outcome = "write_capability_present"
                con.execute("SAVEPOINT n3_probe")
                try:
                    con.execute("INSERT INTO ledger_meta(key,value) VALUES('n3-probe','synthetic')")
                except sqlite3.OperationalError as error:
                    outcome = "write_refused_readonly" if "readonly" in str(error) else "write_refused_other"
                finally:
                    con.execute("ROLLBACK TO n3_probe")
                    con.execute("RELEASE n3_probe")
                probes.append(outcome)
                yield con
        with patch.object(correspondence, "ledger", spy):
            yield calls, probes

    def test_ledger_open_has_no_write_capability(self):
        before = database_state(self.path)
        with self.write_probe() as (calls, probes):
            result = agency_timeline.report(self.path)
        self.assertEqual(calls, [True])
        self.assertEqual(probes, ["write_refused_readonly"], "write_capability_present")
        self.assertEqual(result["totals"]["agency_rows"], 4)
        self.assertEqual(database_state(self.path), before)

    def test_report_and_cli_leave_the_ledger_unchanged(self):
        before = database_state(self.path)
        inode = self.path.stat().st_ino
        names = {p.name for p in self.base.iterdir()}
        holder, outputs = private_dir("synthetic-n3-out-")
        self.addCleanup(holder.cleanup)
        agency_timeline.report(self.path)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(load_tool().main(["--ledger", str(self.path), "--output-dir",
                                               str(outputs / "one")]), 0)
        self.assertEqual(database_state(self.path), before)
        self.assertEqual(self.path.stat().st_ino, inode)
        # The pinned read-only open of a quiet WAL ledger may add SQLite's own
        # shared-memory sidecar and an EMPTY -wal file; nothing else may appear.
        added = {p.name for p in self.base.iterdir()} - names
        self.assertLessEqual(added, {"ledger.sqlite-shm", "ledger.sqlite-wal"})
        wal = self.base / "ledger.sqlite-wal"
        if wal.exists():
            self.assertEqual(wal.stat().st_size, 0)

    def test_static_sources_import_no_writable_constructor_or_side_channel(self):
        forbidden_modules = {"sqlite3", "socket", "subprocess", "urllib", "http", "requests", "smtplib",
                             "imaplib", "ftplib", "shutil", "tempfile"}
        forbidden_names = {"initialize", "import_legacy", "capture_database", "private_parent", "connect"}
        for path in (MODULE_PATH, TOOL_PATH):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""] + [alias.name for alias in node.names]
                else:
                    names = []
                for name in names:
                    self.assertNotIn(name.split(".")[0], forbidden_modules, (path.name, name))
                    self.assertNotIn(name, forbidden_names, (path.name, name))
                if isinstance(node, ast.Attribute):
                    self.assertNotIn(node.attr, forbidden_names, (path.name, node.attr))
                if isinstance(node, ast.keyword) and node.arg == "readonly":
                    self.fail("explicit readonly keyword in " + path.name)
            self.assertNotIn("mode=rw", source)
        module = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("correspondence.snapshot(path)", module)
        self.assertNotIn("open(", module)


class CliTests(LedgerCase):
    def run_tool(self, argv):
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = load_tool().main(argv)
        return code, json.loads(stream.getvalue())

    def test_cli_writes_json_and_table_once_into_a_new_directory(self):
        holder, outputs = private_dir("synthetic-n3-out-")
        self.addCleanup(holder.cleanup)
        target = outputs / "report"
        code, status = self.run_tool(["--ledger", str(self.path), "--output-dir", str(target)])
        self.assertEqual(code, 0)
        self.assertEqual(status["status"], "review_required")
        self.assertEqual(sorted(p.name for p in target.iterdir()), ["agency-timeline.json", "agency-timeline.txt"])
        self.assertEqual(target.stat().st_mode & 0o777, 0o700)
        document = json.loads((target / "agency-timeline.json").read_text(encoding="utf-8"))
        self.assertEqual(document, json.loads(json.dumps(agency_timeline.report(self.path))))
        self.assertEqual(status["snapshot_sha256"], document["snapshot_sha256"])
        self.assertEqual((target / "agency-timeline.txt").read_text(encoding="utf-8"),
                         agency_timeline.render_table(document))
        before = {p.name: p.read_bytes() for p in target.iterdir()}
        code, status = self.run_tool(["--ledger", str(self.path), "--output-dir", str(target)])
        self.assertEqual((code, status["error"]), (2, "output_dir_exists"))
        self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, before)

    def test_cli_argv_is_exact_and_refusals_create_nothing(self):
        holder, outputs = private_dir("synthetic-n3-out-")
        self.addCleanup(holder.cleanup)
        target = outputs / "never"
        cases = [
            (["--ledger", str(self.path)], "usage_invalid"),
            (["--ledger", str(self.path), "--output-dir", str(target), "--extra"], "usage_invalid"),
            (["--ledger", "ledger.sqlite", "--output-dir", str(target)], "absolute_paths_required"),
            (["--ledger", str(self.path), "--output-dir", "relative-out"], "absolute_paths_required"),
            (["--ledger", str(self.path), "--output-dir", str(outputs / "missing" / "child")],
             "output_parent_unavailable"),
            (["--ledger", str(self.base / "absent.sqlite"), "--output-dir", str(target)],
             "canonical_ledger_unavailable_or_unsupported"),
        ]
        for argv, error in cases:
            code, status = self.run_tool(argv)
            self.assertEqual((code, status["status"], status["error"]), (2, "refused", error), argv)
            self.assertNotIn(str(self.base), json.dumps(status))
        self.assertEqual(list(outputs.iterdir()), [])
        self.assertFalse((self.base / "absent.sqlite").exists())
        self.assertFalse(os.path.lexists(Path.cwd() / "relative-out"))


class EdgeTests(unittest.TestCase):
    def setUp(self):
        holder, self.base = private_dir("synthetic-n3-edge-")
        self.addCleanup(holder.cleanup)

    def minimal_spec(self):
        return {"agencies": [], "requests": [], "originals": [], "occurrences": [], "mail": [], "joins": [],
                "portal_items": []}

    def test_empty_ledger_reports_only_a_zero_unknown_bucket(self):
        path = build_ledger(self.base / "ledger.sqlite", self.minimal_spec())
        result = agency_timeline.report(path)
        self.assertEqual(len(result["agencies"]), 1)
        unknown = result["agencies"][0]
        self.assertEqual((unknown["agency_id"], unknown["bucket"], unknown["first_contact_at"]), (None, "unknown", None))
        for name in agency_timeline.ROW_COUNTS:
            self.assertEqual(result["totals"][name], 0, name)
        self.assertEqual(result["totals"]["months"], [])

    def test_conflicting_and_cross_agency_joins_never_attribute_a_message(self):
        spec = self.minimal_spec()
        spec["agencies"] = [{"id": "agency-a", "name": "Fictional A", "jurisdiction": "synthetic"},
                            {"id": "agency-b", "name": "Fictional B", "jurisdiction": "synthetic"}]
        spec["requests"] = [{"id": "req-b", "agency": "agency-b", "external_ref": "x", "submitted_at": None,
                             "determination_due_at": None, "determination_at": None, "production_at": None,
                             "status": "open"}]
        spec["originals"] = [{"label": "mail-c", "mime": "message/rfc822"},
                             {"label": "mail-x", "mime": "message/rfc822"}]
        spec["occurrences"] = [{"id": "mc", "original": "mail-c", "kind": "mail", "parent": None},
                               {"id": "mx", "original": "mail-x", "kind": "mail", "parent": None}]
        spec["mail"] = [{"id": "mc", "original": "mail-c", "folder": "INBOX", "uid": 1,
                         "received_at": "2026-07-01T00:00:00Z"},
                        {"id": "mx", "original": "mail-x", "folder": "Archive.Sent Items", "uid": 2,
                         "received_at": "2026-07-02T00:00:00"}]
        spec["joins"] = [{"id": "c1", "original": "mail-c", "agency": "agency-a", "request": None, "status": "typed"},
                         {"id": "c2", "original": "mail-c", "agency": "agency-b", "request": None, "status": "typed"},
                         {"id": "x1", "original": "mail-x", "agency": "agency-a", "request": "req-b",
                          "status": "typed"}]
        path = build_ledger(self.base / "ledger.sqlite", spec)
        result = agency_timeline.report(path)
        unknown = row(result, None)
        self.assertEqual((unknown["agency_conflict_messages"], unknown["unknown_domain_messages"]), (1, 1))
        self.assertEqual((unknown["messages_received"], unknown["messages_sent"]), (1, 1))
        # Naive timestamp: counted, but never dated or placed in a month.
        self.assertEqual(unknown["undated_messages"], 1)
        self.assertEqual(row(result, "agency-a")["unresolved_joins"], 1)
        self.assertEqual(row(result, "agency-b")["requests"][0]["linked_messages"], 0)

    def test_unsafe_identifiers_status_text_and_bad_dates_are_withheld(self):
        spec = self.minimal_spec()
        spec["agencies"] = [{"id": "Fictional N3SENTINELNAME Office", "name": "n", "jurisdiction": "synthetic"}]
        spec["requests"] = [{"id": "req-1", "agency": "Fictional N3SENTINELNAME Office", "external_ref": "x",
                             "submitted_at": "N3SENTINELBODY yesterday", "determination_due_at": "2026-02-30",
                             "determination_at": None, "production_at": None,
                             "status": "Records attached N3SENTINELBODY"}]
        path = build_ledger(self.base / "ledger.sqlite", spec)
        result = agency_timeline.report(path)
        text = json.dumps(result) + agency_timeline.render_table(result)
        self.assertNotIn("N3SENTINEL", text)
        request = result["agencies"][0]["requests"][0]
        self.assertTrue(result["agencies"][0]["agency_id"].startswith("withheld-id-"))
        self.assertEqual((request["status"], request["submitted_at"], request["determination_due_at"]),
                         ("status_not_token", None, None))
        self.assertEqual((result["gaps"]["withheld_values"], result["gaps"]["unparseable_dates"]), (2, 2))

    def test_folder_direction_uses_only_the_last_folder_segment(self):
        self.assertEqual([agency_timeline.direction(f) for f in
                          ("Sent", "[Synthetic]/Sent Mail", "INBOX.Sent Items", "sent messages",
                           "INBOX", "Sent/Archive", "Unsent", None)],
                         ["sent", "sent", "sent", "sent", "received", "received", "received", "received"])


if __name__ == "__main__":
    unittest.main()
