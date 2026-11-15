"""DF2007-05: a model's PUBLIC-OK is not a human decision; the packet step holds each finding until a signed, bound approve exists.

Everything is synthetic: the real analysis fixture (export, sync, DAG to DONE, real fact review), a throwaway SSH key trusted
in-process (tests.approval_signing) and, for the CLI, a test-only sitecustomize that trusts the same throwaway key in the subprocess.
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import analysis_bridge as bridge, cli, finding_review as fr, public_packets as pp
from tests.approval_signing import KEYGEN, Signer
from tests.records import test_analysis_packet_outbox as to, test_analysis_tick as tt

REPO = tt.REPO
UNAVAILABLE = Path("/nonexistent/deflock-trust/approvers.allowed_signers")
FORBIDDEN = ("approve", "decide", "write_change", "enable", "turn_off", "init", "propose")


class FindingReviewTests(unittest.TestCase):
    def setUp(self):
        self.o = to.PacketOutboxTests("test_empty_received_at_holds_the_finding_as_date_unavailable")
        self.o.start()
        self.addCleanup(self.o.doCleanups)
        self.root, self.slug, self.signer = self.o.root, tt.SLUG, self.o.signer
        os.chmod(self.root / ".analysis", 0o700)
        self.config = json.loads(self.o.t.config().read_text())
        self.journal = self.root / ".analysis" / "finding-reviews.jsonl"
        self.findings = Path(self.config["compare"]["f3"]["store"]) / "agencies" / self.slug / "runs" / self.config["review"]["run_date"] / "FINDINGS.csv"
        self.shim = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.shim, True)
        self.env = self.make_env(self.signer.signers)

    def make_env(self, signers):
        """A subprocess environment whose approval module trusts `signers` (test-only sitecustomize; production has no such path)."""
        shim = Path(tempfile.mkdtemp(dir=self.shim))
        (shim / "sitecustomize.py").write_text(
            "from pathlib import Path\nfrom campaign_tool import approval\n"
            "approval.TRUSTED_SIGNERS = Path(%r)\napproval.SSH_KEYGEN = Path(%r)\napproval._require_root = lambda path: None\n"
            % (str(signers), KEYGEN))
        return dict(os.environ, PYTHONPATH=os.pathsep.join([str(shim), str(REPO)]))

    # ---- helpers
    def packets(self):
        return bridge._packets(self.root, self.slug, self.config)

    def held(self):
        return {h["finding_id"]: h for h in self.packets_held()}

    def packets_held(self):
        reviews = fr.Reviews(self.root)
        return bridge._evaluate(self.root, self.slug, self.config, lambda fid, found, dup: reviews.gate(self.slug, found, dup))["held"]

    def row(self, finding):
        (row,) = [r for r in fr.list_findings(self.root, agency=self.slug) if r["finding_id"] == finding]
        return row

    def sign(self, finding, signer=None, namespace="deflock-approval"):
        return (signer or self.signer).sign(self.row(finding)["payload"].encode("utf-8"), namespace)

    def edit(self, finding, **change):
        with open(self.findings, newline="") as handle:
            reader = csv.DictReader(handle)
            names, rows = list(reader.fieldnames or []), list(reader)
        with open(self.findings, "w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names, lineterminator="\n")
            writer.writeheader()
            writer.writerows(dict(r, **change) if r["finding_id"] == finding else r for r in rows)

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(fr.FindingReviewError) as caught:
            call(self.root, self.slug, *args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def cli(self, *args, env=None):
        done = subprocess.run([sys.executable, "-B", "-m", "campaign_tool.records", "finding-review", *args], cwd=REPO, env=env or self.env,
                              capture_output=True, text=True, check=False)
        return done.returncode, done.stdout

    def tree(self):
        return sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if "packet-outbox" not in p.parts)

    # ---- pure binding
    def test_hash_binds_exactly_topic_claim_fact_order_and_rule_order_and_nothing_else(self):
        base = {"finding_id": "X", "topic": "t", "claim": "c", "fact_ids": "F-1;F-2", "rule_ids": "r1;r2", "rating": "strong"}
        digest = fr.finding_hash(base)
        self.assertRegex(digest, "^[0-9a-f]{64}$")
        self.assertEqual(digest, fr.finding_hash(dict(base, finding_id="Y", rating="weak", next_step="x")))
        for change in ({"topic": "t2"}, {"claim": "c "}, {"fact_ids": "F-2;F-1"}, {"fact_ids": "F-1;F-2;F-2"}, {"rule_ids": "r2;r1"}, {"rule_ids": ""}):
            self.assertNotEqual(fr.finding_hash(dict(base, **change)), digest, change)
        for bad in ({"fact_ids": ""}, {"fact_ids": "F-1;;F-2"}, {"fact_ids": "F-1; "}, {"fact_ids": " F-1"}, {"rule_ids": "r1;"}, {"claim": None}, {"fact_ids": ["F-1"]}):
            with self.assertRaises(fr.FindingReviewError) as caught:
                fr.finding_hash(dict(base, **bad))
            self.assertEqual(caught.exception.code, "finding_malformed")
        payload = fr.finding_payload("agency-a", base)
        self.assertIn("scope: finding-review-v1\nsubject: agency-a:X\nround: 0\n", payload)
        self.assertIn('"claim":"c"', payload)
        self.assertIn(digest, payload)
        self.assertFalse(payload.endswith("\n"))

    # ---- acceptance behaviours
    def test_public_ok_without_human_finding_approval_is_held(self):
        self.o.review("N1", approve=False)  # the facts are checked; no human reviewed the finding
        self.o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 FINDING_UNREVIEWED=1")
        self.assertEqual(self.o.outbox(), [])
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.held()["N1"], {"finding_id": "N1", "code": "FINDING_UNREVIEWED", "ids": [self.o.cited["N1"]]})
        row = self.row("N1")
        self.assertEqual((row["review_state"], row["hold_code"], row["f4_verdict"]), ("unreviewed", "FINDING_UNREVIEWED", "confirm"))
        self.assertEqual(row["facts"], [{"fact_id": self.o.cited["N1"], "source_sha256": self.o.digest, "page": "2", "quote": "The search log lists date, user and plate only."}])

    def test_the_packet_step_without_a_review_gate_holds_everything(self):
        self.o.review("N1", approve=False)
        ev = bridge._evaluate(self.root, self.slug, self.config)
        self.assertEqual((ev["packets"], [h["code"] for h in ev["held"] if h["finding_id"] == "N1"]), ([], ["FINDING_UNREVIEWED"]))
        ev = bridge._evaluate(self.root, self.slug, self.config, lambda fid, found, dup: None)  # the gate is the only thing holding it
        self.assertEqual([e["provenance"]["finding_id"] for e in ev["packets"]], ["N1"])

    def test_signed_approve_stages_once_and_packet_approval_is_separate(self):
        self.o.review("N1", approve=False)
        payload, signature = self.o.sign_finding("N1")
        self.assertIn("The sample search log has no search-purpose field.", payload)
        with mock.patch.multiple(pp, **{name: mock.Mock(side_effect=AssertionError(name)) for name in FORBIDDEN}):
            done = fr.approve(self.root, self.slug, "N1", signature)
            self.assertEqual((done["decision"], done["principal"], done["idempotent"]), ("approve", self.signer.principal, False))
            self.assertEqual(done["finding_sha256"], self.row("N1")["finding_sha256"])
            self.assertEqual(oct(self.journal.stat().st_mode & 0o777), "0o600")
            self.o.tick("PACKET_STAGED")
            (path,) = self.o.outbox()
            self.o.tick("PACKET_UNCHANGED")
        self.assertEqual(self.o.outbox(), [path])
        body = json.loads(path.read_text())
        (entry,) = body["packets"]
        self.assertEqual(entry["packet"]["finding_state"], "approved for public")
        self.assertNotIn("signature", json.dumps(body))  # the packet itself still needs its own signed approval by G
        self.assertNotIn(signature, (self.root / ".analysis" / "analysis-ledger.jsonl").read_text())
        self.assertEqual(self.row("N1")["review_state"], "approved")
        again = fr.approve(self.root, self.slug, "N1", signature)  # the same signature again is a no-op
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.journal.read_text().splitlines()), 1)

    def test_forged_or_other_scope_signature_cannot_append(self):
        self.o.review("N1", approve=False)
        stranger = Signer("stranger@example.invalid")
        self.addCleanup(stranger.close)
        wrong_text = self.signer.sign(self.row("A1")["payload"].encode("utf-8"))
        edited = self.signer.sign(self.row("N1")["payload"].replace("search-purpose", "search-reason").encode("utf-8"))
        before = self.tree()
        for signature in (self.sign("N1", stranger), self.sign("N1", namespace="other-scope"), wrong_text, edited, "owner@example.invalid", ""):
            self.refused("finding_signature_refused", fr.approve, "N1", signature)
        self.refused("finding_signature_refused", fr.approve, "O1", self.sign("N1"))  # a signature of another finding
        self.refused("finding_unknown", fr.approve, "ZZ", self.sign("N1"))
        self.refused("finding_unknown", fr.approve, "T9", self.sign("N1"))
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.journal.exists())
        self.o.tick("PACKET_ALL_HELD n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 FINDING_UNREVIEWED=1")

    def test_claim_topic_fact_list_or_rule_change_requires_new_review(self):
        self.o.review("N1")
        self.o.tick("PACKET_STAGED")
        key = self.packets()[1]
        journal = self.journal.read_bytes()
        with open(self.findings, newline="") as handle:
            original = {r["finding_id"]: r for r in csv.DictReader(handle)}["N1"]
        fact = original["fact_ids"]
        for change in ({"claim": "The sample search log lacks a purpose field."}, {"topic": "search-purposes-2"}, {"rule_ids": ""},
                       {"fact_ids": fact + ";" + fact}):
            self.edit("N1", **change)
            self.assertEqual(self.held()["N1"]["code"], "FINDING_UNREVIEWED", change)
            self.assertEqual(self.row("N1")["review_state"], "unreviewed")
            self.assertEqual(self.packets()[0], "PACKET_ALL_HELD")
            self.assertEqual(self.journal.read_bytes(), journal)  # the old decision stays, bound to its old text
            self.edit("N1", **{k: original[k] for k in change})  # restoring the exact finding restores only its still-valid decision
            self.assertEqual(self.packets()[:2], ("PACKET_UNCHANGED", key))
        self.edit("N1", claim="The sample search log lacks a purpose field.")
        stale = self.row("N1")
        self.refused("finding_stale", fr.approve, "N1", self.sign("N1"), expected_hash=self.o.cited["N1"])
        self.refused("finding_stale", fr.reject, "N1", "no", expected_hash=fr.finding_hash(dict(original, finding_id="N1")))
        done = fr.approve(self.root, self.slug, "N1", self.sign("N1"), expected_hash=stale["finding_sha256"])  # a new review of the new text
        self.assertEqual(done["finding_sha256"], stale["finding_sha256"])
        self.assertEqual(self.packets()[0], "PACKET_STAGED")
        self.assertEqual(len(self.journal.read_text().splitlines()), 2)

    def test_rejection_holds_with_private_reason_and_latest_decision_wins(self):
        self.o.review("N1")
        self.assertEqual(self.packets()[0], "PACKET_STAGED")
        reason = "Secret reviewer note: wording overreaches."
        with mock.patch.object(approval, "TRUSTED_SIGNERS", UNAVAILABLE):  # a rejection needs a reason, not a signature
            done = fr.reject(self.root, self.slug, "N1", reason, entry_id="console-1")
            self.assertEqual((done["decision"], done["reason"], done["entry_id"]), ("reject", reason, "console-1"))
            self.assertTrue(fr.reject(self.root, self.slug, "N1", reason)["idempotent"])
            self.assertEqual(self.held()["N1"], {"finding_id": "N1", "code": "FINDING_REJECTED", "ids": [self.o.cited["N1"]], "reason": reason})
        outcome, _, _, summary, write = self.packets()
        self.assertEqual((outcome, summary, write), ("PACKET_ALL_HELD", "n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 FINDING_REJECTED=1", None))
        line = self.o.tick("PACKET_ALL_HELD")
        shown = line + "".join(p.read_text() for p in self.o.outbox()) + (self.root / ".analysis" / "analysis-ledger.jsonl").read_text()
        self.assertNotIn("Secret reviewer", shown)
        self.assertEqual((self.row("N1")["review_state"], self.row("N1")["reason"]), ("rejected", reason))
        for bad in ("", "  ", "x" * 501, "two\nlines"):
            self.refused("finding_reason_invalid", fr.reject, "N1", bad)
        done = fr.approve(self.root, self.slug, "N1", self.sign("N1"))  # a later valid signed approval supersedes the rejection
        self.assertFalse(done["idempotent"])
        self.assertEqual(self.row("N1")["review_state"], "approved")
        self.assertNotIn("N1", self.held())
        self.assertEqual([json.loads(x)["decision"] for x in self.journal.read_text().splitlines()], ["approve", "reject", "approve"])

    def test_trust_unavailable_is_deferred_not_final_refusal(self):
        self.o.review("N1", approve=False)
        signature = self.sign("N1")
        for patch in (mock.patch.object(approval, "TRUSTED_SIGNERS", UNAVAILABLE), mock.patch.object(approval, "SSH_KEYGEN", Path("/nonexistent/ssh-keygen"))):
            with patch:
                self.refused(fr.DEFERRED, fr.approve, "N1", signature)
            self.assertFalse(self.journal.exists())  # nothing consumed or appended
        sig = self.root.parent / (self.root.name + "-N1.sig")
        self.addCleanup(lambda: sig.exists() and sig.unlink())
        sig.write_text(signature)
        args = ["approve", "--root", str(self.root), "--agency", self.slug, "--finding-id", "N1", "--signature-file", str(sig)]
        self.assertEqual(self.cli(*args, env=self.make_env(UNAVAILABLE)), (1, "FINDING_REVIEW_DEFERRED\n"))  # exit 1 with its own code
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.cli(*args)[0], 0)  # retry once trust is back; the signature was not consumed
        self.o.tick("PACKET_STAGED")
        journal = self.journal.read_bytes()
        for patch in (mock.patch.object(approval, "TRUSTED_SIGNERS", UNAVAILABLE), mock.patch.object(approval, "SSH_KEYGEN", Path("/nonexistent/ssh-keygen"))):
            with patch:
                self.assertEqual(self.packets()[:4:3], ("PACKET_ALL_HELD", "n=0 held=4 EVIDENCE_FACT_UNREVIEWED=3 FINDING_REVIEW_DEFERRED=1"))
                self.assertEqual((self.row("N1")["review_state"], self.row("N1")["hold_code"]), ("deferred", fr.DEFERRED))
            self.assertEqual(self.journal.read_bytes(), journal)
        self.o.tick("PACKET_UNCHANGED")
        # a stored approval that no longer verifies is not eligible (and not deferred): a different key now holds the trust anchor
        other = Signer("other@example.invalid")
        self.addCleanup(other.close)
        with mock.patch.object(approval, "TRUSTED_SIGNERS", other.signers):
            self.assertEqual((self.row("N1")["review_state"], self.held()["N1"]["code"]), ("unreviewed", "FINDING_UNREVIEWED"))

    def test_first_hold_wins_in_parent_order(self):
        self.o.review("N1")  # approved and checked
        sources = self.o.out / "inputs" / "SOURCES.csv"
        with open(sources, newline="") as handle:
            rows = list(csv.DictReader(handle))

        def pages(count):
            with open(sources, "w", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
                writer.writeheader()
                writer.writerows(dict(r, pages=count) for r in rows)
        pages("1")  # N1 cites page 2 of a one-page record: the page hold comes before any review state
        self.assertEqual(self.held()["N1"]["code"], "EVIDENCE_PAGE_OUT_OF_RANGE")
        self.edit("N1", claim="Changed after approval.")  # now also unreviewed: still the page hold, exactly one row
        self.assertEqual([h["code"] for h in self.packets_held() if h["finding_id"] == "N1"], ["EVIDENCE_PAGE_OUT_OF_RANGE"])
        pages("6")
        self.assertEqual(self.held()["N1"]["code"], "FINDING_UNREVIEWED")
        self.assertEqual(self.held()["O1"]["code"], "EVIDENCE_FACT_UNREVIEWED")  # an unchecked fact is held before the finding review
        self.assertEqual(self.row("O1")["review_state"], "unreviewed")

    def test_missing_or_broken_journal_and_stale_write_are_safe(self):
        before = self.tree()
        self.assertEqual(fr.list_findings(self.root)[0]["review_state"], "unreviewed")
        self.assertEqual(fr.Reviews(self.root).error, None)
        self.o.t.tick()
        self.assertFalse(self.journal.exists())  # reading an absent journal creates nothing
        self.assertEqual([p for p in self.tree() if p not in before and "finding" in p], [])
        self.o.review("N1")
        fr.reject(self.root, self.slug, "A1", "not yet")
        good = self.journal.read_bytes()
        lines = good.splitlines(keepends=True)
        self.assertEqual(len(lines), 2)
        self.o.tick("PACKET_STAGED")
        key = self.packets()[1]

        def broken(label, make):
            with self.subTest(label):
                make()
                self.assertEqual(fr.Reviews(self.root).error, "finding_review_unavailable")
                self.assertEqual(self.held()["N1"]["code"], fr.DEFERRED)
                stored = self.journal.read_bytes() if self.journal.is_file() and not self.journal.is_symlink() else None
                self.refused("finding_review_unavailable", fr.reject, "T1", "x")
                self.refused("finding_review_unavailable", fr.approve, "N1", self.sign("N1"))
                self.assertEqual(self.journal.read_bytes() if stored is not None else None, stored)  # never repaired or rewritten

        def restore():
            if self.journal.is_symlink() or self.journal.exists():
                self.journal.unlink()
            self.journal.write_bytes(good)
            os.chmod(self.journal, 0o600)
            os.chmod(self.journal.parent, 0o700)
            self.assertEqual(self.packets()[:2], ("PACKET_UNCHANGED", key))
        broken("truncated tail", lambda: self.journal.write_bytes(good[:-5]))
        restore()
        broken("edited middle", lambda: self.journal.write_bytes(lines[0].replace(b"approve", b"reject") + lines[1]))
        restore()
        broken("dropped first line", lambda: self.journal.write_bytes(lines[1]))
        restore()
        broken("not json", lambda: self.journal.write_bytes(good + b"{\n"))
        restore()
        broken("group readable", lambda: os.chmod(self.journal, 0o644))
        restore()
        broken("open parent", lambda: os.chmod(self.journal.parent, 0o755))
        restore()
        elsewhere = self.root.parent / (self.root.name + "-elsewhere")
        self.addCleanup(lambda: elsewhere.exists() and elsewhere.unlink())

        def link():
            elsewhere.unlink(missing_ok=True)
            os.link(self.journal, elsewhere)

        def symlink():
            elsewhere.unlink(missing_ok=True)
            elsewhere.write_bytes(good)
            self.journal.unlink()
            self.journal.symlink_to(elsewhere)
        broken("symlink leaf", symlink)
        restore()
        broken("second hard link", link)
        elsewhere.unlink()
        restore()
        broken("not a regular file", lambda: (self.journal.unlink(), os.mkfifo(self.journal)))
        restore()
        with mock.patch.object(fr, "MAX", len(good) - 1):
            self.assertEqual(fr.Reviews(self.root).error, "finding_review_unavailable")
        with mock.patch.object(fr, "MAX", len(good) + 10):
            self.refused("finding_review_unavailable", fr.reject, "T1", "x" * 20)
        self.assertEqual(self.journal.read_bytes(), good)
        # a stale write is refused, and a rejection of an unrelated finding leaves the staged packet key alone
        self.refused("finding_stale", fr.reject, "T1", "x", expected_hash="0" * 64)
        self.assertEqual(self.journal.read_bytes(), good)
        fr.reject(self.root, self.slug, "T1", "unrelated")
        self.assertEqual(self.packets()[:2], ("PACKET_UNCHANGED", key))
        self.assertEqual(self.packets()[:2], ("PACKET_UNCHANGED", key))

    def test_concurrent_duplicate_writes_leave_one_effective_event(self):
        self.o.review("N1", approve=False)
        signature = self.sign("N1")
        results, errors = [], []

        def worker():
            try:
                results.append(fr.approve(self.root, self.slug, "N1", signature)["idempotent"])
            except Exception as exc:  # noqa: BLE001 - reported below
                errors.append(exc)
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual((errors, sorted(results)), ([], [False, True, True, True]))
        self.assertEqual(len(self.journal.read_text().splitlines()), 1)

    # ---- command line
    def test_command_is_registered_and_the_cli_lists_approves_and_rejects_with_exit_codes(self):
        self.assertEqual(cli.COMMANDS["finding-review"], ("finding_review", "main"))
        self.o.review("N1", approve=False)
        base = ["--root", str(self.root)]
        code, out = self.cli("list", *base, "--json")
        self.assertEqual(code, 0, out)
        rows = {r["finding_id"]: r for r in json.loads(out)}
        self.assertEqual(sorted(rows), ["A1", "N1", "O1", "T1"])
        self.assertEqual(rows["N1"]["payload"], self.row("N1")["payload"])
        code, text = self.cli("list", *base, "--agency", self.slug)
        self.assertEqual((code, "N1 %s unreviewed hold=FINDING_UNREVIEWED" % rows["N1"]["finding_sha256"][:12] in text), (0, True))
        code, payload = self.cli("list", *base, "--agency", self.slug, "--finding-id", "N1", "--payload")
        self.assertEqual((code, payload), (0, rows["N1"]["payload"]))
        sig = self.root.parent / (self.root.name + "-N1.sig")
        self.addCleanup(lambda: sig.exists() and sig.unlink())
        sig.write_text(self.signer.sign(payload.encode("utf-8")))
        who = ["--agency", self.slug, "--finding-id", "N1"]
        code, out = self.cli("approve", *base, *who, "--signature-file", str(sig), "--expected-hash", "0" * 64)
        self.assertEqual((code, out.strip()), (1, "finding_stale"))
        self.assertFalse(self.journal.exists())
        code, out = self.cli("approve", *base, *who, "--signature-file", str(sig), "--expected-hash", rows["N1"]["finding_sha256"])
        self.assertEqual((code, out.split()[:2]), (0, ["FINDING_APPROVED", self.slug]))
        self.assertEqual(oct(self.journal.stat().st_mode & 0o777), "0o600")
        self.assertIn("FINDING_REVIEW_UNCHANGED", self.cli("approve", *base, *who, "--signature-file", str(sig))[1])
        code, out = self.cli("reject", *base, "--agency", self.slug, "--finding-id", "O1", "--reason", "needs a second look")
        self.assertEqual((code, out.split()[0]), (0, "FINDING_REJECTED"))
        states = {r["finding_id"]: r["review_state"] for r in json.loads(self.cli("list", *base, "--json")[1])}
        self.assertEqual(states, {"A1": "unreviewed", "N1": "approved", "O1": "rejected", "T1": "unreviewed"})
        self.assertEqual(self.cli("approve", *base, *who)[0], 2)  # usage: no --signature-file
        self.assertEqual(self.cli("reject", *base, *who)[0], 2)
        self.assertEqual(self.cli("list")[0], 2)
        sig.write_text(self.signer.sign(payload.replace("search", "seek").encode("utf-8")))
        self.assertEqual(self.cli("approve", *base, "--agency", self.slug, "--finding-id", "A1", "--signature-file", str(sig))[1].strip(), "finding_signature_refused")
        sig.write_text("x" * 9000)
        self.assertEqual(self.cli("approve", *base, *who, "--signature-file", str(sig))[1].strip(), "finding_signature_unreadable")
        self.assertEqual(self.cli("list", "--root", str(REPO))[1].strip(), "finding_root_refused")


if __name__ == "__main__":
    unittest.main()
