"""DFGL-07g: the analysis tick turns each PUBLIC-OK finding of a DONE review into a finding packet in the host outbox."""
import csv
import datetime
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from campaign_tool import law
from campaign_tool.records import analysis_bridge as bridge, public_packets as pp
from tests.flock import test_review as tr
from tests.records import test_analysis_bridge as tb, test_analysis_tick as tt

REPO, FACTS_TOOL = tt.REPO, tt.REPO / "tools" / "flock_facts.py"
BANNED = "The sample department violated the access law."


def facts_module():
    spec = importlib.util.spec_from_file_location("_packet_test_facts", FACTS_TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PacketOutboxTests(unittest.TestCase):
    def start(self, claim=None, at="2026-11-02T18:00:00Z"):
        """A tick fixture whose facts cite the real received document (its sha) and whose fact ids are recomputed for it."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        fixtures = Path(scratch.name) / "flock"
        shutil.copytree(tr.FIX, fixtures)
        self.t = tt.AnalysisTickTests("test_command_is_registered_and_lane_off_writes_nothing")
        probe = tb.AnalysisBridgeTests("test_dry_run_writes_nothing_and_the_run_store_is_never_modified")
        probe.setUp()
        digest = probe.document(tb.PAGES, tb.AGY)[0]
        probe.doCleanups()
        module, old = facts_module(), "b" * 64
        with open(fixtures / "compare-facts.csv", newline="") as handle:
            rows = list(csv.DictReader(handle))
        ids = {module.fact_id(r): module.fact_id(dict(r, source_sha256=digest)) for r in rows}
        for path in fixtures.rglob("*"):
            if path.is_file() and path.suffix in (".csv", ".json", ".txt", ".md"):
                text = path.read_text().replace(old, digest)
                for was, now in ids.items():
                    text = text.replace(was, now)
                if claim and path.name == "findings-sample.csv":
                    text = text.replace("The sample search log has no search-purpose field.", claim)
                path.write_text(text)
        patcher = patch.object(tr, "FIX", fixtures)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t.setUp()
        rows = "request,doc_id,title,sha256,analysis_only\nREQ-101,1,doc.pdf,%s,no\n" % digest
        tr.write_stub(self.t.method, "inventory", "Path(a[1]).mkdir(parents=True, exist_ok=True); (Path(a[1]) / 'INVENTORY.csv').write_text(%r); sys.exit(0)" % rows)
        self.addCleanup(self.t.doCleanups)
        if at != "2026-11-02T18:00:00Z":
            first = self.t.sync_agency
            self.t.sync_agency = lambda *a, **k: first(*a, at=at, **k)
        self.digest, self.root = digest, self.t.root
        self.out = self.t.handoff()
        self.store = self.out / "facts" / "store"
        self.facts = self.store / "agencies" / tt.SLUG / "FACTS.csv"
        self.log = self.facts.with_name("FACT-REVIEWS.csv")
        run = self.out / "compare" / "store" / "agencies" / tt.SLUG / "runs" / json.loads(self.t.config().read_text())["review"]["run_date"]
        with open(run / "FINDINGS.csv", newline="") as handle:
            self.cited = {r["finding_id"]: r["fact_ids"] for r in csv.DictReader(handle)}

    def review(self, finding, status="checked"):
        done = subprocess.run([sys.executable, "-B", str(FACTS_TOOL), "review", "--store", str(self.store), "--agency", tt.SLUG, "--ids",
                               self.cited[finding], "--set", status, "--reviewer", "second-reviewer", "--note", "n",
                               "--date", datetime.date.today().isoformat()], capture_output=True, text=True, check=False)
        self.assertEqual(done.returncode, 0, done.stdout)

    def outbox(self):
        folder = self.root / ".analysis" / "packet-outbox"
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def tick(self, expect):
        code, out = self.t.tick()
        self.assertEqual(code, 0, out)
        line = next(x for x in out.splitlines() if x.startswith("%s PACKET_" % tt.SLUG))
        self.assertIn(" " + expect, line)
        return line

    def ledger(self):
        return [json.loads(x) for x in (self.root / ".analysis" / "analysis-ledger.jsonl").read_text().splitlines() if '"packet"' in x]

    def test_unreviewed_is_held_then_a_human_check_stages_one_clean_packet_that_survives_the_next_tick(self):
        self.start()
        self.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=4")
        self.assertEqual(self.outbox(), [])
        self.review("N1")
        checked, reviewed = self.facts.read_bytes(), self.log.read_bytes()
        line = self.tick("PACKET_STAGED")
        self.assertIn("n=1 held=3 EVIDENCE_FACT_UNREVIEWED=3", line)
        (path,) = self.outbox()
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        body = json.loads(path.read_text())
        self.assertEqual((body["schema"], body["slug"], body["agency_id"]), ("records-analysis-packet-v2", tt.SLUG, tb.AGY))
        self.assertEqual(path.name, "%s-%s.json" % (tt.SLUG, body["key"][:12]))
        (entry,) = body["packets"]
        packet, prov = entry["packet"], entry["provenance"]
        self.assertEqual(pp.clean(packet), packet)  # the real public_packets.clean accepts it unchanged
        self.assertEqual(packet["documents"], [{"sha256": self.digest, "date_received": "2026-11-02", "source_request": "rq1",
                                                "redaction_ids": [], "use_restriction": False}])
        self.assertEqual([c["ref"] for c in packet["citations"]], ["record %s page 2 (%s)" % (self.digest[:12], self.cited["N1"])])
        self.assertTrue(all(c["verified"] is True for c in packet["citations"]))
        self.assertEqual((packet["request_id"], packet["finding_state"], packet["kind"]), ("rq1", "approved for public", "finding"))
        self.assertTrue(packet["text"].endswith("Research draft - not legal advice. The us-ca law package is draft and unreviewed."))
        self.assertEqual((prov["rule_ids"], prov["withheld_rule_ids"]), ([], ["ca-civ-1798.90.52-access-records"]))
        self.assertEqual(prov["documents"][0]["received_at_source"], "mail_received_at")
        self.assertEqual({h["finding_id"] for h in body["held"]}, {"O1", "A1", "T1"})
        self.tick("PACKET_UNCHANGED")
        self.assertEqual(self.outbox(), [path])
        self.assertEqual((self.facts.read_bytes(), self.log.read_bytes()), (checked, reviewed))  # checked state survives the next tick
        with open(self.facts, newline="") as handle:
            self.assertEqual({r["fact_id"]: r["status"] for r in csv.DictReader(handle)}[self.cited["N1"]], "checked")
        blob = "\n".join(self.t.seen) + (self.root / ".analysis" / "analysis-ledger.jsonl").read_text() + json.dumps(body["held"])
        for private in ("search log", "The ALPR vendor", "Twelve outside agencies", packet["text"][:20]):
            self.assertNotIn(private, blob)

    def test_empty_received_at_holds_the_finding_as_date_unavailable(self):
        self.start(at="")
        self.review("N1")
        self.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_DATE_UNAVAILABLE=1 EVIDENCE_FACT_UNREVIEWED=3")
        self.assertEqual(self.outbox(), [])

    def test_banned_legal_wording_is_refused_without_rewriting(self):
        self.start(claim=BANNED)
        self.review("N1")
        self.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 EVIDENCE_REFUSED legal_claim_wording=1")
        self.assertEqual(self.outbox(), [])

    def test_a_raw_status_edit_is_not_a_review(self):
        self.start()
        self.facts.write_text(self.facts.read_text().replace("unreviewed", "checked"))
        self.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_REVIEW_STATE_INVALID=4")
        self.assertEqual(self.outbox(), [])

    def test_a_pending_journal_holds_every_finding(self):
        self.start()
        self.review("N1")
        self.facts.with_name(".FACTS-TXN.json").write_text("{}")
        config = json.loads(self.t.config().read_text())
        outcome, _, _, summary, write = bridge._packets(self.root, tt.SLUG, config)
        self.assertEqual((outcome, summary, write), ("PACKET_ALL_HELD", "n=0 held=4 EVIDENCE_REVIEW_TXN_PENDING=4", None))
        self.t.tick()
        self.assertEqual(self.outbox(), [])

    def test_the_view_and_its_input_hashes_are_one_shared_snapshot_a_writer_cannot_split(self):
        self.start()
        self.review("N1")
        facts_tool, seen = bridge._tool("flock_facts"), []
        _tool = bridge._tool
        real = facts_tool.read_reviewed

        def probe(store, slug):
            got = real(store, slug)
            try:  # still inside the caller's shared lock: a review writer must be refused here
                with facts_tool.store_lock(Path(store), exclusive=True):
                    seen.append("writer got in")
            except facts_tool.Locked:
                seen.append("writer excluded")
            return got

        with patch.object(facts_tool, "read_reviewed", probe), patch.object(facts_tool, "LOCK_WAIT", 0), \
                patch.object(bridge, "_tool", lambda name: facts_tool if name == "flock_facts" else _tool(name)):
            bridge._packets(self.root, tt.SLUG, json.loads(self.t.config().read_text()))
        self.assertEqual(seen, ["writer excluded"])

    def test_a_newer_run_without_analysis_ready_does_not_hide_the_older_retained_one(self):
        self.start()
        self.review("N1")
        newer = self.root / ".analysis" / "runs" / "run_20261231T000000Z_dddddddd"
        newer.mkdir()
        (newer / "EXPORT.json").write_text(json.dumps({"analysis_ready": "missing"}))
        outcome, _, _, summary, write = bridge._packets(self.root, tt.SLUG, json.loads(self.t.config().read_text()))
        self.assertEqual((outcome, summary), ("PACKET_STAGED", "n=1 held=3 EVIDENCE_FACT_UNREVIEWED=3"))
        (newer / "analysis-ready.json").write_text("{")  # a present but unreadable newer file is still unavailable
        self.assertEqual(bridge._packets(self.root, tt.SLUG, json.loads(self.t.config().read_text()))[0], "PACKET_ALL_HELD")

    def test_provenance_comes_from_the_one_earliest_received_row_of_a_document(self):
        self.start()
        self.review("N1")
        sources = self.out / "inputs" / "SOURCES.csv"
        with open(sources, newline="") as handle:
            rows = list(csv.DictReader(handle))
        later = dict(rows[0], run_id=tb.R2, received_at="2026-11-01T09:00:00Z", received_at_source="fallback_received_at", request_id="rq1")
        with open(sources, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows + [later])
        write = bridge._packets(self.root, tt.SLUG, json.loads(self.t.config().read_text()))[4]
        (entry,) = json.loads(write[2])["packets"]
        self.assertEqual(entry["provenance"]["documents"], [{"sha256": self.digest, "run_id": tb.R2, "received_at": "2026-11-01T09:00:00Z",
                                                             "received_at_source": "fallback_received_at"}])
        self.assertEqual(entry["packet"]["documents"][0]["date_received"], "2026-11-01")

    def test_a_dispute_after_staging_holds_the_finding_and_adds_no_file(self):
        self.start()
        self.review("N1")
        self.tick("PACKET_STAGED")
        self.review("N1", "disputed")
        self.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_DISPUTED=1 EVIDENCE_FACT_UNREVIEWED=3")
        self.assertEqual(len(self.outbox()), 1)

    def test_a_reviewed_package_cites_only_its_verified_rules(self):
        self.start()
        for finding in ("N1", "A1"):
            self.review(finding)
        base = Path(tempfile.mkdtemp(dir=self.t.b.tmp))
        for verdict, cited in (("verified", ["Civ. Code § 1798.90.52"]), ("likely", [])):
            package = json.loads((REPO / "jurisdictions" / "us-ca" / "package.json").read_text())
            package.update(status="reviewed", reviewed_by=["Synthetic Reviewer"], reviewed_at="2026-11-01")
            for rule in package["rules"]:
                rule["review"] = verdict if rule["rule_id"] == "ca-civ-1798.90.52-access-records" else "verified"
            (base / verdict / "us-ca").mkdir(parents=True)
            (base / verdict / "us-ca" / "package.json").write_text(json.dumps(package))
            ready = next((self.root / ".analysis" / "runs").glob("*/analysis-ready.json"))
            ready.write_text(ready.read_text().replace('"draft"', '"reviewed"'))
            config = json.loads(self.t.config().read_text())
            with patch.object(law, "JURISDICTIONS", base / verdict):
                outcome, _, _, _, write = bridge._packets(self.root, tt.SLUG, config)
            self.assertEqual(outcome, "PACKET_STAGED")
            body = json.loads(write[2])
            by_finding = {e["provenance"]["finding_id"]: e for e in body["packets"]}
            refs = [c["ref"] for c in by_finding["N1"]["packet"]["citations"] if not c["ref"].startswith("record ")]
            self.assertEqual(refs, cited)
            self.assertEqual(by_finding["N1"]["provenance"]["withheld_rule_ids"], [] if cited else ["ca-civ-1798.90.52-access-records"])
            self.assertFalse(by_finding["N1"]["packet"]["text"].endswith("unreviewed."))
            self.assertEqual(by_finding["A1"]["provenance"]["withheld_policy_refs"], ["P-3ee5198a9319#435.6(c)"])
        outcome = bridge._packets(self.root, tt.SLUG, config)[0]  # the package on disk is draft but the retained ready says reviewed
        self.assertEqual(outcome, "PACKET_ALL_HELD")


if __name__ == "__main__":
    unittest.main()
