"""console-model/ in each console export: live-verified read model computed in the container (DFGL-16R)."""
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_export, mail_drafts, mail_send, public_packets as pp
from tests.approval_signing import Signer
from tests.records.test_console_export import tree_state

RUN = "run_20261110T150000Z_ab12cd34"
T0 = int(time.time())  # the export reads packets at the current time
SHA = "a" * 64
MODEL = ("queue.json", "packets.json", "public-updates.json", "mail-drafts.json", "sends.json")
SEND_ROW = {"schema": mail_send.SCHEMA, "queue_id": "q-sent", "revision": 0, "state": "sent", "at": "2026-11-10T15:00:00Z"}


def packet(kind, source):
    out = {"kind": kind, "agency_key": "ex-pd", "request_id": "r1", "source": source, "title": "Policy released",
           "text": "Example PD released its ALPR policy on 2026-11-03.",
           "documents": [{"sha256": SHA, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}]}
    if kind == "finding":
        out.update(finding_state="approved for public", citations=[{"ref": "Policy 462.3, page 4", "verified": True}])
    return out


class ModelBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.live = self.tmp / "live"
        (self.live / "run-outputs" / RUN).mkdir(parents=True)
        (self.live / "run-outputs" / RUN / "text-state.json").write_text("{}")
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)

    def export(self, project=None):
        """Run the export in-process; return (dest, {name: parsed model file})."""
        dest = self.tmp / ("dest%d" % len(list(self.tmp.glob("dest*"))))
        dest.mkdir(mode=0o700)
        manifest = console_export.build(str(project or self.live), RUN, str(dest))
        model = {n: json.loads((dest / "console-model" / n).read_text()) for n in MODEL}
        return dest, manifest, model

    def send_journal(self):
        return self.live / "approvals" / "mail-send.jsonl"

    def write_sends(self, data, mode=0o600):
        path = self.send_journal()
        path.write_bytes(data)
        os.chmod(path, mode)

    def assert_others_ok(self, model, *skip):
        for name in MODEL:
            if name not in skip:
                self.assertEqual(model[name]["state"], "ok", name)


class LiveBase(ModelBase):
    def setUp(self):
        super().setUp()
        for n in (1, 2):
            aq.add_draft(self.live, item_id=f"{n:064x}", kind="reply-extension", agency_id="caltrans", request_id="req-1",
                         body=f"Please extend {n}.", evidence={"source": "test"}, source_run_id=RUN)
        self.approved_id, self.pending_id = (r["id"] for r in sorted(aq.load_queue(self.live), key=lambda r: r["item_id"]))
        aq.approve(self.live, self.approved_id,
                   {"kind": "ssh", "signature": self.signer.sign(aq.approval_payload(self.live, self.approved_id))})
        self.proposed = pp.propose(self.live, packet("finding", "f-proposed"), now=T0)["id"]
        self.approved_packet = pp.propose(self.live, packet("finding", "f-approved"), now=T0)["id"]
        pp.approve(self.live, self.approved_packet, self.signer.sign(pp.payload(self.live, self.approved_packet)), now=T0)
        self.write_sends(json.dumps(SEND_ROW).encode() + b"\n")
        (self.live / "approvals" / mail_drafts.JOURNAL).write_text(json.dumps(
            {"schema": mail_drafts.SCHEMA, "queue_id": self.approved_id, "revision": 1, "state": "placed",
             "at": "2026-11-10T15:00:00Z"}) + "\n")
        os.chmod(self.live / "approvals" / mail_drafts.JOURNAL, 0o600)


class LiveModel(LiveBase):
    def test_rows_and_payloads_are_live_signed_and_bound_to_the_live_project(self):
        pid = aq._project_id(self.live)
        before = tree_state(self.live)
        dest, manifest, model = self.export()
        self.assertEqual(tree_state(self.live), before)  # the live tree (and its lock files) is untouched
        queue = model["queue.json"]
        by_id = {r["id"]: r for r in queue["rows"]}
        self.assertEqual(by_id[self.approved_id]["status"], "approved")
        self.assertEqual(by_id[self.approved_id]["approval_evidence"]["project"], pid)
        self.assertEqual(by_id[self.pending_id]["status"], "pending")
        self.assertEqual(sorted(queue["payloads"]), [self.pending_id])
        self.assertIn("\nproject: " + pid + "\n", queue["payloads"][self.pending_id])
        self.assertEqual(queue["payloads"][self.pending_id].encode(), aq.approval_payload(self.live, self.pending_id))
        self.assertEqual(sorted(queue["history"]), sorted(by_id))
        packets = model["packets.json"]
        self.assertEqual(sorted(r["id"] for r in packets["pending"]), sorted([self.proposed, self.approved_packet]))
        self.assertEqual({r["id"]: r["status"] for r in packets["status"]["packets"]},
                         {self.proposed: "proposed", self.approved_packet: "approved"})
        self.assertEqual(packets["payloads"], {self.proposed: pp.payload(self.live, self.proposed).decode()})
        self.assertIn("project: " + pp.project_id(self.live), packets["payloads"][self.proposed])
        self.assertEqual(packets["switch"], "OFF")
        self.assertEqual(model["public-updates.json"]["lines"], [])
        self.assertEqual(model["mail-drafts.json"]["rows"], mail_drafts.placements(self.live))
        self.assertEqual(model["sends.json"]["rows"], [SEND_ROW])
        self.assert_others_ok(model)
        for name, body in model.items():
            self.assertEqual((body["schema"], body["source_project_id"]), (console_export.MODEL_SCHEMA, pid), name)
        self.assertEqual((manifest["model"]["schema"], manifest["model"]["source_project_id"]),
                         (console_export.MODEL_SCHEMA, pid))
        self.assertEqual(json.loads((dest / "EXPORT.json").read_text())["model"], manifest["model"])
        for name in MODEL:
            rel = "console-model/" + name
            self.assertIn(rel, manifest["files"])
            self.assertEqual(os.stat(dest / rel).st_mode & 0o777, 0o600)

    def test_no_raw_signature_anywhere_in_the_model(self):
        queue_events = [json.loads(line) for line in (self.live / "approvals" / "queue.jsonl").read_text().splitlines()]
        packet_events = [json.loads(line) for line in (self.live / pp.JOURNAL).read_text().splitlines()]
        signatures = [e["approval"]["signature"] for e in queue_events if "approval" in e]
        signatures += [e["signature"] for e in packet_events if "signature" in e]
        self.assertEqual(len(signatures), 2)
        dest, _, model = self.export()
        for path in (dest / "console-model").iterdir():
            text = path.read_text()
            self.assertNotIn("BEGIN SSH SIGNATURE", text, path.name)
            for signature in signatures:
                self.assertNotIn(signature.strip().splitlines()[1], text, path.name)
        approved = [r for r in model["queue.json"]["rows"] if r["status"] == "approved"]
        self.assertEqual(len(approved[0]["approval_evidence"]["signature_sha256"]), 64)

    def test_corrupt_queue_is_unavailable_and_the_rest_stays_ok(self):
        with open(self.live / "approvals" / "queue.jsonl", "ab") as journal:
            journal.write(b"not json\n")
        before = tree_state(self.live)
        _, _, model = self.export()
        self.assertEqual((model["queue.json"]["state"], model["queue.json"]["code"]), ("unavailable", "queue_invalid"))
        self.assertNotIn("rows", model["queue.json"])
        self.assert_others_ok(model, "queue.json")
        self.assertEqual(tree_state(self.live), before)

    def test_unreadable_packets_journal_is_unavailable_not_empty(self):
        (self.live / "public-packets.jsonl").write_text("not json\n")
        _, _, model = self.export()
        self.assertEqual((model["packets.json"]["state"], model["packets.json"]["code"]), ("unavailable", "journal_invalid"))
        for key in ("pending", "status", "payloads"):
            self.assertNotIn(key, model["packets.json"])
        self.assert_others_ok(model, "packets.json")


class SendResultsStrict(LiveBase):
    def check(self, code):
        before = tree_state(self.live)
        _, _, model = self.export()
        sends = model["sends.json"]
        self.assertEqual((sends["state"], sends["code"]), ("unavailable", code))
        self.assertNotIn("rows", sends)
        self.assert_others_ok(model, "sends.json")
        self.assertEqual(tree_state(self.live), before)

    def test_valid_row_then_non_json_line_is_unavailable_never_a_partial_list(self):
        self.write_sends(json.dumps(SEND_ROW).encode() + b"\nnot json\n")
        self.assertEqual(mail_send.results(str(self.live)), [SEND_ROW])  # the non-strict default would export a partial list
        self.check("send_results_invalid")

    def test_missing_final_newline_is_unavailable(self):
        self.write_sends(json.dumps(SEND_ROW).encode())
        self.assertEqual(mail_send.results(str(self.live)), [SEND_ROW])
        self.check("send_results_invalid")

    def test_row_without_schema_is_unavailable(self):
        self.write_sends(json.dumps({"queue_id": "x"}).encode() + b"\n")
        self.check("send_results_invalid")

    def test_symlinked_journal_is_a_send_io_refusal(self):
        copy = self.live / "approvals" / "copy.jsonl"
        copy.write_bytes(json.dumps(SEND_ROW).encode() + b"\n")
        os.chmod(copy, 0o600)
        self.send_journal().unlink()
        self.send_journal().symlink_to(copy)
        self.assertEqual(mail_send.results(str(self.live)), [])  # swallowed by the default: the export must not copy that
        self.check("send_io")

    def test_present_approvals_without_journal_is_an_empty_ok_list(self):
        self.send_journal().unlink()
        before = tree_state(self.live)
        _, _, model = self.export()
        self.assertEqual((model["sends.json"]["state"], model["sends.json"]["rows"]), ("ok", []))
        self.assertNotIn("journal", model["sends.json"])
        self.assert_others_ok(model)
        self.assertEqual(tree_state(self.live), before)


class BareProject(ModelBase):
    def test_project_that_never_had_a_journal_exports_empty_ok_and_creates_nothing(self):
        before = tree_state(self.live)
        _, _, model = self.export()
        self.assertEqual(tree_state(self.live), before)
        self.assertFalse((self.live / pp.LOCK).exists())
        self.assertFalse((self.live / "approvals").exists())
        self.assert_others_ok(model)
        self.assertEqual(model["queue.json"]["rows"], [])
        self.assertEqual(model["packets.json"]["status"], {"switch": "OFF", "packets": []})
        self.assertEqual((model["sends.json"]["rows"], model["sends.json"]["journal"]), ([], "absent"))

    def test_packet_evidence_without_a_lock_is_unavailable_not_empty(self):
        (self.live / pp.JOURNAL).write_text("")
        _, _, model = self.export()
        self.assertEqual((model["packets.json"]["state"], model["packets.json"]["code"]),
                         ("unavailable", "packets_unavailable"))
        self.assertFalse((self.live / pp.LOCK).exists())


if __name__ == "__main__":
    unittest.main()
