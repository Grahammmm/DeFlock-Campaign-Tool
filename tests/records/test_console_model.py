"""console-model/ in each console export: live-verified read model computed in the container (DFGL-16R)."""
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import console_cited_pages as cp, console_export, console_snapshot, mail_drafts, mail_send, public_packets as pp
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

    def test_cited_pages_writer_leaves_every_existing_model_file_byte_identical(self):
        # Contract (DF2307-07): the new file changes no other model file's bytes or format. Bug caught: a shared writer change
        # (encoding, indent, key order) leaking into the existing files the console already reads.
        old = {k: v for k, v in console_export.MODEL.items() if k != console_export.CITED}
        out = []
        for models in (old, None):
            dest = self.tmp / ("bytes%d" % len(out))
            dest.mkdir(mode=0o700)
            console_export.write_model(str(self.live), str(dest), T0, models, run_id=RUN)
            out.append(dest / "console-model")
        self.assertEqual(sorted(p.name for p in out[1].iterdir()), sorted([*old, console_export.CITED]))
        self.assertEqual(sorted(p.name for p in out[0].iterdir()), sorted(old))
        for name in old:
            self.assertEqual((out[0] / name).read_bytes(), (out[1] / name).read_bytes(), name)
            text = (out[0] / name).read_text()
            self.assertEqual(text, json.dumps(json.loads(text), indent=1, sort_keys=True, allow_nan=False) + "\n", name)
        cited = json.loads((out[1] / console_export.CITED).read_text())
        self.assertEqual((cited["schema"], cited["state"], cited["code"]), (cp.SCHEMA, "unavailable", "cited_pages_input_missing"))

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


class CitedPagesReader(ModelBase):
    """SnapshotSource.cited_pages: the pinned file only, with the loader's no-follow, strict JSON and project/run bindings."""
    PID = "0123456789abcdef"

    def page(self):
        return {"original_sha256": SHA, "page": 1, "agency": "maple", "received_at": None, "route": "native",
                "confidence_band": "not_applicable", "review_state": "not_applicable", "text": "é text",
                "text_sha256": hashlib.sha256("é text".encode()).hexdigest(),
                "citations": [{"kind": "fact", "id": "maple/cost/1", "label": "cost"}]}

    def doc(self, **changes):
        return {"schema": cp.SCHEMA, "source_project_id": self.PID, "state": "ok", "run_id": RUN, "pages": [self.page()],
                "not_exported": [{"original_sha256": "b" * 64, "page": 2, "reason": "page_bytes"}], **changes}

    def read(self, body, run=RUN):
        """cited_pages() over a pinned export holding `body` (bytes, a dict, or None for an absent file)."""
        export = self.tmp / ("exp%d" % len(list(self.tmp.glob("exp*"))))
        (export / "console-model").mkdir(parents=True, mode=0o700)
        path = export / "console-model" / "cited-pages.json"
        if body == "symlink":
            (export / "elsewhere.json").write_text(json.dumps(self.doc()))
            path.symlink_to(export / "elsewhere.json")
        elif body is not None:
            path.write_bytes(body if isinstance(body, bytes) else json.dumps(body).encode())
        return console_snapshot.SnapshotSource(console_snapshot.Pin(str(export), RUN, self.PID, "n", "t")).cited_pages(run)

    def test_valid_empty_and_full_models_are_returned_unchanged(self):
        for doc in (self.doc(), self.doc(pages=[], not_exported=[])):
            self.assertEqual(self.read(doc), doc)

    def test_missing_foreign_malformed_and_unlinked_files_are_unavailable_never_empty(self):
        # Contract: every refusal is {state: unavailable, code}; bug caught: a bad or foreign file read as no cited pages.
        bad = lambda code: {"state": "unavailable", "code": code}
        cases = {"absent": (None, "console_model_missing"), "symlink": ("symlink", "console_model_invalid"),
                 "not json": (b"{", "console_model_invalid"),
                 "duplicate key": (b'{"schema":"x","schema":"y"}', "console_model_invalid"),
                 "other schema": (self.doc(schema=console_export.MODEL_SCHEMA), "console_model_invalid"),
                 "other project": (self.doc(source_project_id="f" * 16), "console_model_mismatch"),
                 "reported unavailable": ({"schema": cp.SCHEMA, "source_project_id": self.PID, "state": "unavailable",
                                           "code": "artifact_hash_mismatch"}, "artifact_hash_mismatch")}
        for label, (body, code) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.read(body), bad(code))
        self.assertEqual(self.read(self.doc(), run="other-run"), bad("console_model_mismatch"))
        self.assertEqual(self.read(self.doc(run_id="other-run")), bad("console_model_invalid"))

    def test_malformed_page_rows_are_refused(self):
        def edit(change):
            doc = self.doc()
            change(doc)
            return doc
        twin = lambda d: d["pages"].append(self.page())
        cases = {"duplicate page": twin, "page also not exported": lambda d: d["not_exported"].append({"original_sha256": SHA, "page": 1, "reason": "page_count"}),
                 "bool page": lambda d: d["pages"][0].update(page=True), "upper-case hash": lambda d: d["pages"][0].update(original_sha256="A" * 64),
                 "text hash": lambda d: d["pages"][0].update(text_sha256="0" * 64),
                 "oversize text": lambda d: d["pages"][0].update(text="x" * (cp.MAX_PAGE_BYTES + 1), text_sha256=hashlib.sha256(b"x" * (cp.MAX_PAGE_BYTES + 1)).hexdigest()),
                 "native with a review": lambda d: d["pages"][0].update(review_state="confirmed"),
                 "extra key": lambda d: d["pages"][0].update(path="/x"), "unknown reason": lambda d: d["not_exported"][0].update(reason="other"),
                 "no citation": lambda d: d["pages"][0].update(citations=[])}
        for label, change in cases.items():
            with self.subTest(label):
                self.assertEqual(self.read(edit(change)), {"state": "unavailable", "code": "console_model_invalid"})


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
