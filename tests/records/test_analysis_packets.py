"""DFGL-07h: records analysis-packets-import proposes verified outbox packets and never approves or publishes (synthetic roots)."""
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import types
import unittest
from unittest.mock import patch

from campaign_tool.records import analysis_packets as ap, cli, public_packets as pp, run_stages
from campaign_tool.records.ledger import store
from tests.records import test_analysis_bridge as tb, test_analysis_packet_outbox as to, test_outcome_received_items as tri

REPO = tb.REPO
FORBIDDEN = ("approve", "decide", "write_change", "enable", "turn_off", "init")


def rekey(body):
    body = {k: v for k, v in body.items() if k != "key"}
    key = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    return "%s-%s.json" % (body["slug"], key[:12]), (json.dumps(dict(body, key=key), indent=1, sort_keys=True) + "\n").encode()


def tar_of(*files, link=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(link)
            info.type, info.linkname = tarfile.SYMTYPE, "elsewhere"
            tar.addfile(info)
    return buf.getvalue()


class AnalysisPacketImportTests(unittest.TestCase):
    def setUp(self):
        outbox = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        outbox.start()
        self.addCleanup(outbox.doCleanups)
        outbox.review("N1")
        outbox.tick("PACKET_STAGED")
        (self.path,) = outbox.outbox()  # the real builder's file
        self.name, self.data = self.path.name, self.path.read_bytes()
        self.body = json.loads(self.data)
        self.run = self.body["packets"][0]["provenance"]["documents"][0]["run_id"]
        received = tri.ReceivedItemsTests("test_stage_artifact_modes_replay_conflict_and_no_coordinator_input")
        received.setUp()
        self.addCleanup(received.doCleanups)
        with store.ledger(received.database) as con:
            con.execute("INSERT INTO agencies VALUES(?,?,NULL,NULL)", (tb.AGY, tb.AGY))
            con.commit()
        digest = received.document(tb.PAGES, tb.AGY)[0]
        self.assertEqual(digest, outbox.digest)
        received.mail(digest, "2026-11-02T18:00:00Z")
        (received.processing / "runs").mkdir(mode=0o700)
        (received.processing / "runs" / ("extract-%s.json" % self.run)).write_text(json.dumps(
            {"schema": "records-extract-outcomes-v1", "run_id": self.run, "documents": []}))
        with contextlib.redirect_stdout(io.StringIO()):  # the real writer of received-items.json
            self.assertEqual(run_stages.main(["outputs", "--project", str(received.project), "--run-id", self.run]), 0)
        self.project, self.digest = received.project, digest
        self.journal = self.project / pp.JOURNAL

    def run_main(self, *files, link=None, forbidden=False):
        stdin = types.SimpleNamespace(buffer=io.BytesIO(tar_of(*files, link=link)))
        out = io.StringIO()
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(out), patch.object(sys, "stdin", stdin):
            for name in FORBIDDEN if forbidden else ():
                stack.enter_context(patch.object(pp, name, side_effect=AssertionError(name)))
            code = ap.main(["--project", str(self.project), "--from-tar", "-"])
        return code, out.getvalue()

    def edited(self, **change):
        body = json.loads(self.data)
        body.pop("key")
        body["packets"][0]["packet"].update(change)
        return rekey(body)

    def receipts(self):
        path = self.project / ap.RECEIPT
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def test_command_is_registered(self):
        self.assertEqual(cli.COMMANDS["analysis-packets-import"], ("analysis_packets", "main"))

    def test_real_outbox_file_through_the_cli_waits_as_one_pending_finding_and_a_resend_is_a_no_op(self):
        def cli_run():
            done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "analysis-packets-import", "--project", str(self.project),
                                   "--from-tar", "-"], cwd=REPO, input=tar_of((self.name, self.data)), capture_output=True, check=False)
            return done.returncode, done.stdout.decode()
        code, out = cli_run()
        self.assertEqual(code, 0, out)
        self.assertIn("proposed=1", out)
        (row,) = pp.pending(self.project)
        self.assertEqual((row["kind"], row["status"], row["signed_by"]), ("finding", "proposed", None))
        self.assertFalse((self.project / pp.OUT).exists())
        self.assertFalse((self.project / pp.SWITCH).exists())
        self.assertEqual([r["result"] for r in self.receipts()], ["proposed r0"])
        self.assertEqual(oct((self.project / ap.RECEIPT).stat().st_mode & 0o777), "0o600")
        journal = self.journal.read_bytes()
        code, out = cli_run()
        self.assertEqual(code, 0, out)
        self.assertIn("ALREADY=1", out)
        self.assertEqual((self.journal.read_bytes(), len(self.receipts())), (journal, 1))

    def test_from_directory_and_approval_side_functions_are_never_called(self):
        folder = self.project.parent / "outbox-dir"
        shutil.rmtree(folder, ignore_errors=True)
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        folder.mkdir()
        (folder / "sent").mkdir()
        shutil.copy(self.path, folder / self.name)
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(io.StringIO()):
            for name in FORBIDDEN:
                stack.enter_context(patch.object(pp, name, side_effect=AssertionError(name)))
            self.assertEqual(ap.main(["--project", str(self.project), "--from", str(folder)]), 0)
        self.assertEqual(len(pp.pending(self.project)), 1)
        code, out = self.run_main((self.name, self.data), forbidden=True)
        self.assertEqual((code, "ALREADY=1" in out), (0, True))

    def test_unknown_document_and_date_mismatch_and_other_agency_are_recorded_without_a_journal_event(self):
        other = "c" * 64
        for change, expect in ((dict(document={"sha256": other}), "IMPORT_DOCUMENT_UNKNOWN"),
                               (dict(document={"date_received": "2026-11-01"}), "IMPORT_DATE_MISMATCH"),
                               (dict(document={"date_received": "2026-11-03"}), "IMPORT_DATE_MISMATCH")):
            body = json.loads(self.data)
            body.pop("key")
            doc, prov = body["packets"][0]["packet"]["documents"][0], body["packets"][0]["provenance"]["documents"][0]
            doc.update(change["document"])
            prov["sha256"] = doc["sha256"]
            name, data = rekey(body)
            code, out = self.run_main((name, data))
            self.assertEqual(code, 0, out)
            self.assertIn(expect, out)
            self.assertEqual(self.receipts()[-1]["result"], expect)
        body = json.loads(self.data)
        body.pop("key")
        body["agency_id"] = body["packets"][0]["packet"]["agency_key"] = "agency-z"
        self.assertIn("IMPORT_DOCUMENT_UNKNOWN", self.run_main(rekey(body))[1])
        body["agency_id"] = "agency-y"
        self.assertIn("IMPORT_IDENTITY_MISMATCH", self.run_main(rekey(body))[1])
        self.assertFalse(self.journal.exists())
        self.assertEqual(pp.pending(self.project), [])

    def test_a_clean_refusal_is_a_recorded_outcome_with_exit_zero_and_no_event(self):
        name, data = self.edited(text="The sample department violated the access law.")
        code, out = self.run_main((name, data))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.receipts()[0]["result"], "REFUSED legal_claim_wording")
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.run_main((name, data))[1].count("ALREADY=1"), 1)  # a refused packet is not retried

    def test_tampered_key_or_content_or_non_regular_member_is_import_invalid_and_nothing_is_proposed(self):
        body = json.loads(self.data)
        body["key"] = "0" * 64
        tampered = (json.dumps(body, sort_keys=True) + "\n").encode()
        body = json.loads(self.data)
        body["packets"][0]["packet"]["title"] = "Edited after staging"
        edited = (json.dumps(body, sort_keys=True) + "\n").encode()
        for files, link in (([(self.name, tampered)], None), ([(self.name, edited)], None), ([("x-%s.json" % ("0" * 12), self.data)], None),
                            ([], self.name), ([(self.name, b"{")], None)):
            code, out = self.run_main(*files, link=link)
            self.assertEqual(code, 1, out)
            self.assertIn("IMPORT_INVALID", out)
        self.assertFalse(self.journal.exists())
        self.assertEqual((pp.pending(self.project), self.receipts()), ([], []))
        code, out = self.run_main((self.name, tampered), (self.name, self.data))  # the valid member still imports; the run still fails
        self.assertEqual((code, len(pp.pending(self.project))), (1, 1))

    def test_malformed_packet_collections_are_import_invalid_and_later_valid_members_still_import(self):
        for field, value in (("citations", 1), ("citations", True), ("documents", 1), ("documents", {"sha256": "a"}), ("documents", "x")):
            body = json.loads(self.data)
            body.pop("key")
            packet, prov = body["packets"][0]["packet"], body["packets"][0]["provenance"]
            packet[field] = value
            if field == "documents":
                prov["documents"] = value
            bad = rekey(body)
            code, out = self.run_main(bad, (self.name, self.data))
            self.assertEqual(code, 1, (field, value, out))
            self.assertIn("IMPORT_INVALID", out)
            self.assertIn("proposed=1", out)
            self.assertEqual((len(pp.pending(self.project)), len(self.receipts())), (1, 1))
            self.assertNotIn("Traceback", out)
            for path in (self.project / ap.RECEIPT, self.journal):  # reset so the next case starts clean
                path.unlink()

    def test_a_busy_journal_exits_75_and_leaves_no_receipt(self):
        with patch.object(pp, "propose", side_effect=pp.PacketError("journal_busy")):
            code, out = self.run_main((self.name, self.data))
        self.assertEqual((code, out.strip(), self.receipts()), (75, "journal_busy", []))


if __name__ == "__main__":
    unittest.main()
