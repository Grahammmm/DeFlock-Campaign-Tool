"""DOC-PUBLISH-1: ``records public-packets propose-document`` turns a received record into ONE pending document packet, only
with a verified, applied redaction record. Synthetic fixtures only; no live ledger, record, key or signature is used."""
from contextlib import closing, redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from campaign_tool import approval  # noqa: E402
from campaign_tool.records import public_packets as pp  # noqa: E402
from campaign_tool.records import redaction_record as rr  # noqa: E402
from campaign_tool.records.ledger.migrations import v001  # noqa: E402

RUN = "run_20261113T150000Z_0a1b2c3d"
STAMP = "2026-11-10T00:00:00Z"
SIGNATURE = "-----BEGIN SSH SIGNATURE-----\nsynthetic\n-----END SSH SIGNATURE-----\n"
ORIGINAL = b"Records release from Example PD\nContact synthetic@example.invalid or 202-555-0187\n"
EMAIL_SPAN = {"category": "email", "locator": {"line": 2, "offset": 8, "length": 25}}
PHONE_SPAN = {"category": "phone", "locator": {"line": 2, "offset": 37, "length": 12}}
T0 = 1_794_600_000
PACKET_FILES = frozenset({"public-packets.jsonl", "public-packets.lock"})


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tree(root, skip=frozenset()):
    """Hash of every path, mode and file byte under root (names in skip ignored)."""
    out = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.name in skip:
            continue
        info = path.lstat()
        out.update(f"{path.relative_to(root)}\0{info.st_mode}\0".encode())
        out.update(path.read_bytes() if path.is_file() else b"")
    return out.hexdigest()


class ProposeDocumentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.chmod(tmp.name, 0o700)
        self.project = Path(tmp.name) / "project"
        self.project.mkdir(mode=0o700)
        self.original = self.private_file(self.project / "originals" / "letter.txt", ORIGINAL)
        self.sha = digest(ORIGINAL)
        self.ledger()
        self.received({"received_at": "2026-11-03T17:20:00Z"})
        self.record = self.redaction([EMAIL_SPAN, PHONE_SPAN])
        patcher = mock.patch.object(approval, "verify", return_value="synthetic-reviewer")
        self.verify = patcher.start()
        self.addCleanup(patcher.stop)

    # ---- fixtures ----------------------------------------------------------------------------------------------------
    def private_file(self, path, data):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def ledger(self, preserve="done", extract="done", role="agency_original"):
        processing = self.project / "processing-v1"
        processing.mkdir(mode=0o700, exist_ok=True)
        with closing(sqlite3.connect(processing / "ledger.sqlite")) as db:
            db.executescript(v001.SQL)
            db.execute("INSERT INTO runs VALUES ('r1','records-runner','2026-11-01T00:00:00Z',NULL,'e',NULL,NULL,NULL,"
                       "'completed','{}')")
            db.execute("INSERT INTO originals(sha256,bytes,first_seen_at,role,scope,storage_path,preservation_status,"
                       "provenance_json) VALUES (?,?,?,?,'in_scope',?,'preserved','{}')",
                       (self.sha, len(ORIGINAL), "2026-11-03T17:20:00+00:00", role, str(self.original)))
            for stage, status in (("preserve", preserve), ("extract", extract)):
                db.execute("INSERT INTO stage_state VALUES (?,?,?,?,'runtime','t','r1',NULL)",
                           (self.sha, stage, status, "b" * 64 if status == "done" else None))
            db.commit()

    def received(self, fields, run=RUN):
        item = {"original_sha256": self.sha, "agency_id": "ex-pd", "request_id": "r1", "extraction_status": "current", **fields}
        target = self.project / "run-outputs" / run / "outcomes" / "received-items.json"
        self.private_file(target, json.dumps({"run_id": run, "items": [item]}).encode())

    def write_record(self, name, value):
        raw = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
        self.private_file(rr.record_dir(self.project, "ex-pd", self.sha) / name, raw)
        return raw

    def redaction(self, spans, source=None):
        """An applied record removing ``spans`` from the original (built the way tools/flock_redact.py apply builds one)."""
        source = source or self.sha
        lines = ORIGINAL.split(b"\n")
        for span in sorted(spans, key=lambda s: -s["locator"]["offset"]):
            loc, line = span["locator"], lines[span["locator"]["line"] - 1]
            marker = f"[{span['category']}:{loc['length']} bytes removed]".encode()
            lines[loc["line"] - 1] = line[:loc["offset"]] + marker + line[loc["offset"] + loc["length"]:]
        self.derivative = self.private_file(self.project / "redacted" / f"{self.sha}.redacted.txt", b"\n".join(lines))
        proposal = self.write_record("PROPOSAL.json", {
            "schema_version": 1, "document_id": source, "source_sha256": source, "redaction_required": True,
            "proposed": spans, "not_detected_note": "", "scan_receipt": "c" * 64, "created_at": STAMP})
        signed = {"document_id": source, "source_sha256": source, "proposal_sha256": digest(proposal),
                  "signed_by": "synthetic-reviewer", "signed_at": STAMP}
        self.write_record("SIGNOFF.json", dict(signed, schema_version=1, scope="flock-redaction", signature=SIGNATURE))
        return digest(self.write_record("ELIGIBILITY.json", dict(
            signed, schema_version=1, derivative_path=str(self.derivative),
            derivative_sha256=digest(self.derivative.read_bytes()), applied_at=STAMP)))

    def run_main(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pp.main(["propose-document", "--project", str(self.project), "--sha256", self.sha,
                            "--redaction-record", self.record, *extra])
        return code, out.getvalue(), err.getvalue()

    def refused(self, code, sha=None, record=None):
        before = tree(self.project)
        with self.assertRaises(pp.PacketError) as ctx:
            pp.propose_document(str(self.project), sha or self.sha, record or self.record, now=T0)
        self.assertEqual(ctx.exception.code, code)
        self.assertEqual(tree(self.project), before, "a refusal changes no file and creates no journal")
        self.assertFalse((self.project / "public-packets.jsonl").exists())
        return ctx.exception

    def packets(self):
        lines = (self.project / "public-packets.jsonl").read_text().splitlines()
        return [e for e in map(json.loads, lines) if e["event"] == "packet"]

    # ---- acceptance --------------------------------------------------------------------------------------------------
    def test_happy_path_one_pending_document_packet(self):
        before = tree(self.project, PACKET_FILES)
        code, out, err = self.run_main()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["result"], "proposed")
        [event] = self.packets()
        packet = event["packet"]
        self.assertEqual(pp.clean(packet), packet)
        self.assertEqual(packet["kind"], "document")
        self.assertEqual(packet["documents"], [{"sha256": self.sha, "date_received": "2026-11-03", "source_request": "r1",
                                                "redaction_ids": [self.record], "use_restriction": False}])
        self.assertEqual(packet["citations"], [])
        self.assertEqual([(p["kind"], p["status"]) for p in pp.pending(str(self.project), now=T0 + 60)],
                         [("document", "proposed")])
        self.assertIn(self.record, pp.payload(str(self.project), event["id"]).decode(), "G signs over the record id")
        self.assertEqual(tree(self.project, PACKET_FILES), before, "only the packet store changed")
        self.assertNotIn(self.derivative.read_bytes().decode(), (self.project / "public-packets.jsonl").read_text())

    def test_second_call_is_packet_exists_without_a_duplicate(self):
        self.assertEqual(self.run_main()[0], 0)
        journal = (self.project / "public-packets.jsonl").read_bytes()
        code, out, _ = self.run_main("--title", "A different title")
        self.assertEqual((code, json.loads(out)["result"]), (0, "PACKET_EXISTS"))
        self.assertEqual((self.project / "public-packets.jsonl").read_bytes(), journal)
        self.assertEqual(len(self.packets()), 1)

    def test_original_unknown(self):
        self.refused("ORIGINAL_UNKNOWN", sha="e" * 64)
        self.refused("ORIGINAL_UNKNOWN", sha="not-a-hash")

    def test_excluded_role_is_original_unknown(self):
        (self.project / "processing-v1" / "ledger.sqlite").unlink()
        self.ledger(role="excluded_unrelated_personal")
        self.refused("ORIGINAL_UNKNOWN")

    def test_not_preserved(self):
        (self.project / "processing-v1" / "ledger.sqlite").unlink()
        self.ledger(preserve="pending")
        self.refused("NOT_PRESERVED")

    def test_changed_original_bytes_are_not_preserved(self):
        self.original.write_bytes(ORIGINAL + b"x")
        self.refused("NOT_PRESERVED")

    def test_not_extracted(self):
        (self.project / "processing-v1" / "ledger.sqlite").unlink()
        self.ledger(extract="pending")
        self.refused("NOT_EXTRACTED")

    def test_date_unavailable(self):
        self.received({"received_at": None})
        self.refused("DATE_UNAVAILABLE")
        self.received({"received_at": "2026-02-30T00:00:00Z"})
        self.refused("DATE_UNAVAILABLE")

    def test_only_the_newest_run_counts(self):
        self.received({"received_at": None}, run="run_20261114T070000Z_0a1b2c3d")
        self.refused("DATE_UNAVAILABLE")

    def test_redaction_mismatch(self):
        self.assertEqual(self.refused("REDACTION_MISMATCH", record="f" * 64).detail, "record_id_mismatch")
        rr.record_dir(self.project, "ex-pd", self.sha).joinpath("ELIGIBILITY.json").unlink()
        self.assertEqual(self.refused("REDACTION_MISMATCH").detail, "no_record")

    def test_record_for_another_original_is_a_mismatch(self):
        self.record = self.redaction([EMAIL_SPAN, PHONE_SPAN], source="d" * 64)
        self.assertEqual(self.refused("REDACTION_MISMATCH").detail, "source_mismatch")

    def test_redaction_unverified(self):
        self.verify.side_effect = approval.ApprovalError("untrusted")
        self.assertEqual(self.refused("REDACTION_UNVERIFIED").detail, "signer_untrusted")
        self.verify.side_effect = None
        self.derivative.write_bytes(b"changed\n")
        self.assertEqual(self.refused("REDACTION_UNVERIFIED").detail, "derivative_mismatch")
        rr.record_dir(self.project, "ex-pd", self.sha).joinpath("SIGNOFF.json").unlink()
        self.assertEqual(self.refused("REDACTION_UNVERIFIED").detail, "unsigned")

    def test_loose_record_permissions_are_unverified(self):
        rr.record_dir(self.project, "ex-pd", self.sha).chmod(0o755)
        self.assertEqual(self.refused("REDACTION_UNVERIFIED").detail, "proposal_mismatch")

    def test_record_that_misses_an_email_is_privacy_lint_and_not_proposed(self):
        self.record = self.redaction([PHONE_SPAN])  # verified and applied, but the email stays in the derivative
        error = self.refused("PRIVACY_LINT")
        self.assertEqual(error.detail, "derivative: email")
        code, out, err = self.run_main()
        self.assertEqual((code, out, err.strip()), (1, "", "PRIVACY_LINT derivative: email"))
        self.assertNotIn("synthetic@", err)


if __name__ == "__main__":
    unittest.main()
