import csv
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from campaign_tool.records import tracker_proposal as tp
from campaign_tool.records import tracker_update_proposals as up
from campaign_tool.records.cli import COMMANDS
from campaign_tool.records.outcomes.register import read_tracker

PROPOSED = Path(__file__).resolve().parents[2] / "data" / "slo-pilot" / "MASTER-REQUEST-TRACKER.proposed.csv"


class TrackerProposalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="trk-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def rewrite(self, edit):
        with open(PROPOSED, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        names = list(rows[0])
        edit(rows)
        out = self.tmp / "t.csv"
        with open(out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, names, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        return out

    def test_shipped_proposal_is_clean_with_seventeen_rows(self):
        rows, problems = tp.check_proposal(PROPOSED)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 17)

    def test_existing_register_reader_accepts_it(self):
        rows, gaps = read_tracker(PROPOSED)
        self.assertEqual(gaps, [])
        self.assertEqual(len(rows), 17)
        self.assertTrue(all(r["submitted_at"] and r["received_at"] for r in rows))

    def test_every_row_has_receipt_basis_and_source(self):
        rows, _ = tp.check_proposal(PROPOSED)
        for r in rows:
            self.assertIn(r["receipt_basis"], tp.BASES)
            self.assertTrue(r["source_message_ids"] and r["asked_items"])

    def test_missing_column_is_reported(self):
        out = self.tmp / "t.csv"
        shutil.copy(PROPOSED, out)
        out.write_text(out.read_text(encoding="utf-8").replace("receipt_basis", "basis", 1), encoding="utf-8")
        self.assertEqual(tp.check_proposal(out)[1], ["header_missing:receipt_basis"])

    def test_bad_value_is_reported(self):
        rid = tp.check_proposal(PROPOSED)[0][0]["request_id"]
        cases = [
            ("receipt_basis", "guessed", "receipt_basis_invalid"),
            ("submitted_at", "08/24/2026", "submitted_at_not_iso_date"),
            ("status", "overdue", "status_invalid"),
            ("asked_items", "1 [T1] a || 3 [T2] b", "asked_item_number:3!=2"),
            ("source_message_ids", "Sent:9..3", "source_id_malformed:Sent:9..3"),
            ("source_message_ids", "", "source_message_ids_blank"),
        ]
        for col, value, code in cases:
            with self.subTest(code=code):
                out = self.rewrite(lambda rows: rows[0].update({col: value}))
                self.assertIn(rid + ":" + code, tp.check_proposal(out)[1])

    def test_duplicate_request_id_is_reported(self):
        out = self.rewrite(lambda rows: rows[1].update({"request_id": rows[0]["request_id"].upper()}))
        self.assertTrue(any(p.endswith(":request_id_duplicate") for p in tp.check_proposal(out)[1]))

    def test_unresolved_source_id_is_reported_against_index(self):
        index = self.tmp / "index.tsv"
        index.write_text("folder\tuid\nSent\t1\n", encoding="utf-8")
        problems = tp.check_proposal(PROPOSED, tp.load_mail_index(index))[1]
        self.assertTrue(any("source_id_not_in_mail_index:Sent:33" in p for p in problems))

    def test_no_mail_marker_needs_another_source(self):
        out = self.rewrite(lambda rows: rows[0].update({"source_message_ids": tp.NO_MAIL, "non_mail_sources": ""}))
        self.assertTrue(any(p.endswith(":no_mail_source_without_other_source") for p in tp.check_proposal(out)[1]))

    def test_cli_exit_codes(self):
        self.assertEqual(tp.main([str(PROPOSED), "--expect-rows", "17"]), 0)
        self.assertEqual(tp.main([str(PROPOSED), "--expect-rows", "16"]), 1)


COLS = ["request_id", "agency", "scope", "submitted_at", "agency received date", "receipt_basis", "extension",
        "promised_dates", "status", "asked_items", "owner", "next_action", "source_message_ids", "non_mail_sources",
        "determination_at", "production_at"]
ROWS = {  # number -> (extension, determination_at)
    2: ("no", ""), 3: ("no", ""), 4: ("", ""), 5: ("no", ""), 6: ("no", "2026-11-01"), 7: ("no", ""), 8: ("no", ""),
    9: ("no", ""), 10: ("yes", "")}


def item(n, kind, request="L-%d", received="2026-11-10T20:00:00Z", quote="x", deadlines=(), oid=None, agency="ag1"):
    return {"item_id": "i%s-%s" % (n, kind), "agency_id": agency, "request_id": request % n if "%" in (request or "") else request,
            "occurrence_ids": [oid or "o%d" % n], "kind": kind, "received_at": received,
            "kind_basis": {"page": 2, "quote": quote}, "deadlines": list(deadlines)}


class TrackerProposalProducerTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="trkp-"))
        self.addCleanup(shutil.rmtree, base, True)
        self.project, self.out = base / "project", base / "out"
        (self.project / "run-outputs" / "R" / "outcomes").mkdir(parents=True)
        self.out.mkdir()
        self.dest = self.out / "bundle"

    def run_it(self, items, state="arrived_unrecorded", start="2026-11-01", run="R", blank4=True):
        reqs = [{"agency": "Agency", "agency_key": "ag1", "request_id": "26-%d" % n, "ledger_request_id": "L-%d" % n,
                 "request_date": start if n != 8 else "2026-11-11", "sources": [{"file": "t.csv", "row": n}],
                 "answer_arrived": {"state": state, "occurrence_ids": ["o%d" % n]}} for n in ROWS]
        run_dir = self.project / "run-outputs" / "R"
        (run_dir / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(
            {"schema": "records-request-register-v1", "as_of": "2026-11-20", "requests": reqs}), encoding="utf-8")
        (run_dir / "outcomes" / "received-items.json").write_text(json.dumps(
            {"schema": "records-received-item-v1", "run_id": run, "items": items}), encoding="utf-8")
        with open(self.project / "MASTER-REQUEST-TRACKER.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(COLS)
            for n, (ext, det) in {**ROWS, **({} if blank4 else {4: ("no", "")})}.items():
                w.writerow(["26-%d" % n, "Agency", "scope", "2026-11-01", "2026-11-01", "recorded_receipt", ext, "none",
                            "waiting_on_agency", "1 [T1] a", "owner", "next", "INBOX:%d" % n, "", det, ""])
        return up.main(["--project", str(self.project), "--run-id", "R", "--out", str(self.dest)])

    def bundle(self):
        doc = json.loads((self.dest / "proposals.json").read_text(encoding="utf-8"))
        with open(self.dest / "MASTER-REQUEST-TRACKER.proposed.csv", newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return doc, rows, {(p["request_id"], p["column"]): p for p in doc["proposals"]}

    def test_one_fixture_per_kind_and_checker_passes(self):
        long = "y" * 300
        stated = {"date": "2026-12-04", "date_text": "x", "page": 3, "quote": "by December 4", "confidence": "rule_match", "year_inferred": False}
        self.assertEqual(self.run_it([item(2, "denial"), item(3, "records", quote=long), item(4, "extension"),
                                      item(5, "fee", deadlines=[stated]), item(9, "response"), item(8, "other")]), 0)
        doc, rows, cells = self.bundle()
        by = {r["request_id"]: r for r in rows}
        self.assertEqual(by["26-2"]["determination_at"], "2026-11-10")
        self.assertEqual((by["26-3"]["determination_at"], by["26-3"]["production_at"]), ("2026-11-10", "2026-11-10"))
        self.assertEqual(by["26-4"]["extension"], "yes")
        self.assertEqual((by["26-5"]["promised_at"], by["26-5"]["determination_at"]), ("2026-12-04", ""))
        self.assertEqual((by["26-9"]["determination_at"], by["26-9"]["extension"]), ("", "no"))
        self.assertEqual(by["26-8"]["determination_at"], "")
        self.assertEqual(cells[("26-3", "production_at")]["sources"][0], {"item_id": "i3-records", "kind": "records", "page": 2, "quote": "y" * 200})
        self.assertEqual(cells[("26-5", "promised_at")]["sources"][0]["page"], 3)
        self.assertEqual(tp.check_proposal(self.dest / "MASTER-REQUEST-TRACKER.proposed.csv")[1], [])
        self.assertEqual(doc["conflicts"], [])
        self.assertEqual(oct(os.stat(self.dest).st_mode & 0o777), "0o700")

    def test_filled_cell_kept_and_conflicts_never_applied(self):
        other = item(7, "records", received="2026-11-12T20:00:00Z", oid="o7")
        self.assertEqual(self.run_it([
            item(6, "denial"), item(7, "denial"), other, item(8, "denial"), item(2, "denial", request=None, oid="o2"),
            item(3, "denial", request="L-99", oid="o3", agency="ag1"), item(4, "extension")]), 0)
        doc, rows, cells = self.bundle()
        by = {r["request_id"]: r for r in rows}
        codes = {(c["code"], c.get("request_id")) for c in doc["conflicts"]}
        self.assertEqual(by["26-6"]["determination_at"], "2026-11-01")
        self.assertIn(("filled_cell_differs", "26-6"), codes)
        self.assertEqual(by["26-7"]["determination_at"], "")
        self.assertIn(("replies_differ", "26-7"), codes)
        self.assertEqual(by["26-7"]["production_at"], "2026-11-12")  # an independent cell is still filled
        self.assertIn(("date_before_request_start", "26-8"), codes)
        self.assertEqual(by["26-8"]["determination_at"], "")
        self.assertIn(("reply_without_request_id", None), codes)
        self.assertIn(("reply_unmatched", None), codes)
        self.assertEqual(by["26-4"]["extension"], "yes")

    def test_only_unrecorded_arrivals_are_eligible_and_failures_leave_no_bundle(self):
        self.assertEqual(self.run_it([item(2, "denial")], state="none", blank4=False), 0)
        self.assertEqual(self.bundle()[0]["proposals"], [])
        self.dest = self.out / "second"  # extension left blank and unproven: the unchanged checker fails closed
        self.assertEqual(self.run_it([item(2, "denial")]), 1)
        self.assertFalse(os.path.lexists(self.dest))
        self.assertEqual(os.listdir(self.out), ["bundle"])

    def test_cli_refuses_existing_inside_project_and_wrong_run(self):
        self.assertIn("tracker-proposals", COMMANDS)
        live = (self.project / "MASTER-REQUEST-TRACKER.csv")
        self.run_it([item(2, "denial"), item(4, "extension")])
        before = live.read_bytes()
        self.assertEqual(up.main(["--project", str(self.project), "--run-id", "R", "--out", str(self.dest)]), 1)
        self.assertEqual(up.main(["--project", str(self.project), "--run-id", "R", "--out", str(self.project / "new")]), 1)
        self.assertEqual(up.main(["--project", str(self.project), "--run-id", "Q", "--out", str(self.out / "q")]), 1)
        self.assertEqual(up.main(["--project", str(self.project), "--run-id", "R", "--out", str(self.out / "nope" / "x")]), 1)
        self.assertEqual(live.read_bytes(), before)
        self.assertFalse((self.project / "new").exists())


if __name__ == "__main__":
    unittest.main()
