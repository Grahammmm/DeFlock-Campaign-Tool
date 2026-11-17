"""RF1751-4: next-step input files with repeated JSON member names are refused whole (synthetic data only)."""
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import next_steps as ns
from tests.records.test_next_steps import AGENCIES, RUN, doc, proposal

SKIP = {"queued": 0, "skipped": "proposals_invalid"}
GROUPS = (("ca-caltrans", "req-1", [proposal(1), proposal(2, type="misuse_lead", field="sharing", confidence="medium"),
                                    proposal(3, type="misuse_lead", confidence="low")]),)
RECEIVED = {"items": [{"agency_id": "ca-caltrans", "kind": "records", "received_at": "2026-11-02T18:59:00Z"}]}


def dump(value):
    return json.dumps(value)


class DuplicateMemberTests(unittest.TestCase):
    def setUp(self):
        os.umask(0o077)
        tmp = tempfile.TemporaryDirectory(prefix="nsdup-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.project, self.campaign = root / "project", root / "campaign"
        self.outcomes = self.project / "run-outputs" / RUN / "outcomes"
        self.outcomes.mkdir(parents=True, mode=0o700)
        (self.campaign / "content").mkdir(parents=True)
        self.agencies = self.campaign / "content" / "agencies.json"
        self.agencies.write_text(json.dumps(AGENCIES, indent=2) + "\n", encoding="utf-8")
        self.put(dump(doc(*GROUPS)), dump(RECEIVED))

    def put(self, proposals, received):
        (self.outcomes / "gap-proposals.json").write_text(proposals, encoding="utf-8")
        (self.outcomes / "received-items.json").write_text(received, encoding="utf-8")

    def run_ns(self, **kw):
        return ns.queue_run_next_steps(self.project, RUN, campaign_root=self.campaign, **kw)

    def cli(self, *extra):
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ns.main(["--project", str(self.project), "--run-id", RUN, "--campaign-root", str(self.campaign), *extra])
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        files = [self.project / "approvals" / "queue.jsonl", self.agencies, *sorted(self.outcomes.iterdir())]
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in files if p.exists()}

    def bad_proposals(self):
        good = dump(doc(*GROUPS))
        yield good.replace('"confidence": "high"', '"confidence": "low", "confidence": "high"', 1)
        yield good.replace('"confidence": "high"', '"confidence": "high", "confidence": "low"', 1)
        yield good.replace('"confidence": "high"', '"confidence": "high", "confidence": "high"', 1)
        yield good.replace('"schema": ', '"schema": "wrong", "schema": ', 1)
        yield good.replace('"run_id": ', '"run_id": "x", "run_id": ', 1)
        yield good.replace('"proposal_id": ', '"proposal_id": "a", "proposal_id": ', 1)
        yield good.replace('"confidence": "high"', '"confidence": "low", "confid\\u0065nce": "high"', 1)
        yield good.replace('"rule_ref": {', '"rule_ref": {"x": 1, "x": 2, ', 1)

    def bad_received(self):
        good = dump(RECEIVED)
        yield good.replace('"kind": "records"', '"kind": "denial", "kind": "records"', 1)
        yield good.replace('"kind": "records"', '"kind": "records", "kind": "denial"', 1)
        yield good.replace('"received_at": "2026-11-02T18:59:00Z"',
                           '"received_at": "2026-11-02T18:59:00Z", "received_at": "2026-11-02T18:59:00Z"', 1)
        yield '{"items": [], "items": []}'

    def test_proposals_duplicate_members_refused_before_queue_access(self):
        for raw in self.bad_proposals():
            with self.subTest(raw=raw[:60]):
                self.put(raw, dump(RECEIVED))
                self.assertEqual(self.run_ns(), SKIP)
                self.assertFalse((self.project / "approvals").exists())

    def test_received_duplicate_kind_refuses_whole_input(self):
        for raw in self.bad_received():
            with self.subTest(raw=raw[:60]):
                self.put(dump(doc(*GROUPS)), raw)
                self.assertEqual(self.run_ns(), SKIP)
                self.assertFalse((self.project / "approvals").exists())

    def test_escaped_and_unknown_nested_duplicate_names_refused(self):
        raw = dump(doc(*GROUPS)).replace('"rule_ref": {', '"rule_ref": {"zz_secret": 1, "zz\\u005fsecret": 2, ', 1)
        self.put(raw, dump(RECEIVED))
        self.assertEqual(self.run_ns(), SKIP)
        code, out, err = self.cli()
        self.assertNotIn("zz_secret", out + err)
        self.assertNotIn("confidence", out + err)

    def test_invalid_rerun_preserves_existing_state(self):
        self.assertEqual(self.run_ns()["queued"], 3)
        before = self.snapshot()
        cases = [(raw, dump(RECEIVED)) for raw in self.bad_proposals()] + [(dump(doc(*GROUPS)), r) for r in self.bad_received()]
        for proposals, received in cases:
            self.put(proposals, received)
            before_in = self.snapshot()
            for kw in ({}, {"dry_run": True}):
                with self.subTest(kw=kw, raw=(proposals + received)[:40]):
                    self.assertEqual(self.run_ns(**kw), SKIP)
                    self.assertEqual(self.snapshot(), before_in)
        self.assertEqual(self.snapshot()["queue.jsonl"], before["queue.jsonl"])

    def test_cli_duplicate_input_reports_skip_without_traceback(self):
        self.put(next(self.bad_proposals()), dump(RECEIVED))
        for extra in ((), ("--dry-run",)):
            with self.subTest(extra=extra):
                code, out, err = self.cli(*extra)
                self.assertEqual((code, json.loads(out), err), (0, SKIP, ""))
        self.assertEqual(aq.load_queue(self.project) if (self.project / "approvals").exists() else [], [])

    def test_site_diff_duplicate_members_use_existing_diagnostic_boundary(self):
        raw = json.dumps(AGENCIES, indent=2).replace('"name": "Caltrans"', '"name": "Caltrans", "name": "Other"', 1) + "\n"
        self.agencies.write_text(raw, encoding="utf-8")
        before = self.agencies.read_bytes()
        with self.assertRaises(ns.NextStepsError) as caught:
            ns._site_diff(self.campaign, "ca-caltrans", "2026-11-02")
        self.assertEqual(caught.exception.code, "site_update_invalid")
        result = ns.recommend(doc(*GROUPS), AGENCIES, run_id=RUN, root=self.campaign, received=RECEIVED)
        self.assertIn("site_update_invalid", [d["code"] for d in result["diagnostics"]])
        self.assertNotIn(ns.SITE, [d["kind"] for d in result["drafts"]])
        self.assertTrue({ns.SOCIAL, ns.PROBE} <= {d["kind"] for d in result["drafts"]})
        self.assertEqual(self.agencies.read_bytes(), before)

    def test_unique_json_control_keeps_ids_and_idempotence(self):
        expected = ns.recommend(doc(*GROUPS), AGENCIES, run_id=RUN, root=self.campaign, received=RECEIVED)["drafts"]
        self.assertEqual(self.run_ns()["queued"], 3)
        rows = {r["kind"]: r for r in aq.load_queue(self.project)}
        self.assertEqual(set(rows), set(ns.KINDS))
        for draft in expected:
            self.assertEqual(rows[draft["kind"]]["id"], aq.draft_id(draft["item_id"], draft["kind"]))
        before = (self.project / "approvals" / "queue.jsonl").read_bytes()
        self.assertEqual(self.run_ns()["queued"], 0)
        self.assertEqual((self.project / "approvals" / "queue.jsonl").read_bytes(), before)

    def test_same_member_name_in_distinct_objects_is_valid(self):
        d = doc(*GROUPS)
        for p in d["agencies"][0]["requests"][0]["proposals"]:
            p["extra"] = {"confidence": "low", "schema": "x"}
            p["caf\u00e9"] = 1
        self.put(dump(d), dump(RECEIVED))
        report = self.run_ns(dry_run=True)
        self.assertEqual((len(report["drafts"]), report["skipped_low"]), (3, 1))
        (self.outcomes / "received-items.json").unlink()
        self.assertEqual(self.run_ns(dry_run=True)["skipped_low"], 1)
        self.put("{not json", dump(RECEIVED))
        self.assertEqual(self.run_ns(), SKIP)


if __name__ == "__main__":
    unittest.main()
