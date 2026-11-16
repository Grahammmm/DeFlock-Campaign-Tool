"""FLK-CON-3: ``records public-packets revoked-written`` lists written packet revisions whose recorded approval no longer
verifies against the current anchor. Synthetic projects and throwaway test-only SSH keys; real ssh-keygen verification."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from campaign_tool import approval  # noqa: E402
from campaign_tool.records import public_packets as pp  # noqa: E402
from tests.approval_signing import Approvers  # noqa: E402

DAY = 86400
T0 = 1_794_600_000  # 2026-11-14 UTC
SHA = "a" * 64
HEADER = "Written packets with approvals that do not verify against current trust (exclude from site-PR review):"


def packet(source="doc-1", text="Example PD released its ALPR policy on 2026-11-03."):
    return {"kind": "document", "agency_key": "ex-pd", "request_id": "r1", "source": source, "title": "Policy released",
            "text": text, "documents": [{"sha256": SHA, "date_received": "2026-11-03", "redaction_ids": ["red-1"]}]}


def fingerprint(root):
    out = {}
    for base, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(base, name)
            st = os.lstat(path)
            out[os.path.relpath(path, root)] = (st.st_mode, os.readlink(path) if os.path.islink(path) else (
                hashlib.sha256(Path(path).read_bytes()).hexdigest() if os.path.isfile(path) else None))
    return out


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.keys = Approvers("old@example.invalid", "new@example.invalid")
        cls.keys.start()
        cls.both = cls.keys.file.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.keys.stop()

    def setUp(self):
        self.keys.file.write_text(self.both, encoding="utf-8")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = os.path.join(self.tmp.name, "p")
        pp.init(self.project)

    def only(self, who):
        self.keys.file.write_text(self.keys.signers[who].signers.read_text(encoding="utf-8"), encoding="utf-8")

    def sign(self, who, data):
        return self.keys.signers[who + "@example.invalid"].sign(data)

    def enable(self, who="new", now=T0):
        pp.enable(self.project, self.sign(who, pp.switch_payload(self.project)), now=now)

    def written(self, who="old", source="doc-1", approve_at=T0, write_at=T0 + DAY, text=None):
        got = pp.propose(self.project, packet(source, *([text] if text else [])), now=approve_at)
        ident = got["id"]
        pp.approve(self.project, ident, self.sign(who, pp.payload(self.project, ident)), now=approve_at)
        pp.write_change(self.project, ident, now=write_at)
        return ident, got["revision"]

    def cli(self, *args, project=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = pp.main(["revoked-written", "--project", project or self.project, *args])
        return code, out.getvalue(), err.getvalue()

    def row(self, ident, revision=0, approved=T0, written=T0 + DAY):
        return {"id": ident, "revision": revision, "target": f"content/updates/ex-pd/{pp._iso(approved)[:10]}-{ident}.json",
                "written_at": pp._iso(written)}


class ReportTests(Base):
    def test_real_anchor_rotation_lists_only_old_written(self):
        self.enable()
        old, _ = self.written("old", "doc-old", write_at=T0 + DAY)
        new, _ = self.written("new", "doc-new", write_at=T0 + DAY)
        self.assertEqual(pp.revoked_written(self.project), [])
        self.assertEqual(self.cli("--json"), (0, '{"packets": []}\n', ""))
        self.only("new@example.invalid")
        want = [self.row(old)]
        self.assertEqual(pp.revoked_written(self.project), want)
        code, out, err = self.cli("--json")
        self.assertEqual((code, err, json.loads(out)), (0, "", {"packets": want}))
        self.assertEqual(self.cli()[1], f"{HEADER}\n{old}\t0\t{want[0]['target']}\t{want[0]['written_at']}\n")
        self.keys.file.write_text(self.both, encoding="utf-8")
        self.assertEqual(self.cli(), (0, "No written packets with non-verifying approvals.\n", ""))
        self.assertNotEqual(old, new)

    def test_written_time_is_not_approval_time_and_old_rows_stay(self):
        self.enable(now=T0)
        ident, _ = self.written("old", approve_at=T0, write_at=T0 + DAY + 3600)
        self.only("new@example.invalid")
        (r,) = pp.revoked_written(self.project)
        self.assertEqual(r["written_at"], pp._iso(T0 + DAY + 3600))
        self.assertEqual(r["target"], f"content/updates/ex-pd/{pp._iso(T0)[:10]}-{ident}.json")
        self.assertNotEqual(pp._iso(T0)[:10], r["written_at"][:10])
        # far beyond the 14-day proposal window: the written row is still listed
        self.assertEqual(len(pp.revoked_written(self.project)), 1)

    def test_all_historical_written_revisions_survive_supersession(self):
        self.enable()
        r0, rev0 = self.written("old", "doc-1", T0, T0 + DAY)
        got = pp.propose(self.project, packet("doc-1", "Example PD revised its ALPR policy on 2026-11-05."), now=T0 + 2 * DAY)
        self.assertEqual((got["id"], got["revision"]), (r0, 1))
        # revision 1 stays unwritten and then is cancelled: never listed; revision 0 is hidden from the ordinary fold
        pp.decide(self.project, r0, "cancelled", "operator", now=T0 + 2 * DAY)
        self.assertNotIn("written", [p["status"] for p in pp.status(self.project, now=T0 + 2 * DAY)["packets"]])
        r1 = pp.propose(self.project, packet("doc-1", "Example PD revised its ALPR policy on 2026-11-06."), now=T0 + 3 * DAY)
        self.assertEqual(r1["revision"], 2)
        pp.approve(self.project, r0, self.sign("new", pp.payload(self.project, r0)), now=T0 + 3 * DAY)
        pp.write_change(self.project, r0, now=T0 + 4 * DAY)
        self.only("new@example.invalid")
        self.assertEqual(pp.revoked_written(self.project), [self.row(r0)])
        self.only("old@example.invalid")
        self.assertEqual([(r["id"], r["revision"]) for r in pp.revoked_written(self.project)], [(r0, 2)])

    def test_nonwritten_rows_never_enter_report_and_switch_does_not_hide(self):
        self.enable()
        ident, _ = self.written("old")
        for i, how in enumerate(("approve", "cancel", "reject", "plain")):
            got = pp.propose(self.project, packet(f"x-{i}"), now=T0)
            if how in ("approve", "cancel"):
                pp.approve(self.project, got["id"], self.sign("old", pp.payload(self.project, got["id"])), now=T0)
            if how in ("cancel", "reject"):
                pp.decide(self.project, got["id"], {"cancel": "cancelled", "reject": "rejected"}[how], "operator", now=T0)
        # a premature written event for an unapproved packet is ignored by the fold and must not be inspected
        early = pp.propose(self.project, packet("x-premature"), now=T0)
        with pp._Locked(self.project) as lock:
            lock.append("written", T0, id=early["id"], revision=0, file="site-changes/x.json", file_sha256="0" * 64)
        self.only("new@example.invalid")
        pp.turn_off(self.project, "operator", now=T0 + 5 * DAY)
        open(os.path.join(self.project, pp.KILL), "w").close()
        self.assertEqual(pp.status(self.project, now=T0 + 5 * DAY)["switch"], "KILLED")
        self.assertEqual([r["id"] for r in pp.revoked_written(self.project)], [ident])

    def test_exact_signature_and_recorded_principal_are_checked(self):
        self.enable()
        ident, _ = self.written("old", "doc-1")
        # a verifier that succeeds but names another principal, and one that always fails, both list the row
        with mock.patch.object(pp.approval, "verify", lambda data, sig: "someone@else"):
            self.assertEqual([r["id"] for r in pp.revoked_written(self.project)], [ident])
        with mock.patch.object(pp.approval, "verify", side_effect=approval.ApprovalError("raw secret text")):
            code, out, err = self.cli("--json")
        self.assertEqual((code, err), (0, ""))
        self.assertNotIn("secret", out)
        self.assertEqual(set(json.loads(out)["packets"][0]), {"id", "revision", "target", "written_at"})
        # every written revision is verified individually (no per-principal shortcut)
        self.written("old", "doc-2", write_at=T0 + DAY)
        seen = []
        real = approval.verify
        with mock.patch.object(pp.approval, "verify", lambda d, s: seen.append(s) or real(d, s)):
            self.assertEqual(pp.revoked_written(self.project), [])
        self.assertEqual(len(seen), 2)
        # a signature copied onto another revision's journal row does not verify that row's payload
        path = os.path.join(self.project, pp.JOURNAL)
        lines = Path(path).read_text().splitlines()
        sigs = [json.loads(line)["signature"] for line in lines if json.loads(line)["event"] == "approved"]
        Path(path).write_text("\n".join(l.replace(json.dumps(sigs[1])[1:-1], json.dumps(sigs[0])[1:-1]) if '"approved"' in l
                                        else l for l in lines) + "\n")
        self.assertEqual(len(pp.revoked_written(self.project)), 1)

    def test_malformed_written_history_refuses_without_content(self):
        self.enable()
        self.written("old")
        path = os.path.join(self.project, pp.JOURNAL)
        good = Path(path).read_text()
        for label, edit in (("digest", lambda t: t.replace('"digest":"', '"digest":"0')),
                            ("ts", lambda t: t.replace('"event":"written"', '"event":"written"').replace(
                                f'"ts":{T0 + DAY}', '"ts":"x"')),
                            ("signature", lambda t: t.replace('"signature":"', '"signature":7,"x":"'))):
            Path(path).write_text(edit(good))
            code, out, err = self.cli("--json")
            self.assertEqual((code, out, err), (1, "", "written_packet_invalid\n"), label)
        dup = next(e for e in map(json.loads, good.splitlines()) if e["event"] == "packet")
        Path(path).write_text(good + json.dumps(dup) + "\n")
        self.assertEqual(self.cli("--json")[0], 1)
        self.assertEqual(self.cli()[2], "written_packet_invalid\n")


class AnchorTests(Base):
    def assert_unavailable(self, project=None):
        for extra in ((), ("--json",)):
            self.assertEqual(self.cli(*extra, project=project), (1, "", "APPROVAL_ANCHOR_UNAVAILABLE\n"))
        with self.assertRaises(pp.PacketError) as ctx:
            pp.revoked_written(project or self.project)
        self.assertEqual(ctx.exception.code, "APPROVAL_ANCHOR_UNAVAILABLE")

    def test_unavailable_anchor_never_reports_empty(self):
        bare = os.path.join(self.tmp.name, "bare")
        os.mkdir(bare)
        self.enable()
        self.written("old")
        self.assertEqual(self.cli("--json")[0], 0)
        for label in ("missing", "no-namespace", "comment-only"):
            if label == "missing":
                self.keys.file.unlink()
            else:
                self.keys.file.write_text("# nothing\n" if label == "comment-only" else
                                          "x@y ssh-ed25519 AAAA\n", encoding="utf-8")
            self.assert_unavailable()  # a project with a written packet
            self.assert_unavailable(bare)  # a bare empty project
        self.keys.file.write_text(self.both, encoding="utf-8")
        empty = os.path.join(self.tmp.name, "initialized")
        pp.init(empty)
        self.assertEqual(self.cli("--json", project=empty), (0, '{"packets": []}\n', ""))
        self.keys.file.unlink()
        self.assert_unavailable(empty)

    def test_unavailable_verifier_after_a_failed_signature_is_still_unavailable(self):
        self.enable()
        self.written("old", "doc-1")
        self.written("new", "doc-2", write_at=T0 + DAY)
        calls = []

        def verify(data, sig):
            calls.append(1)
            if len(calls) == 1:
                raise approval.ApprovalError("untrusted")
            raise approval.TrustUnavailable("gone")
        with mock.patch.object(pp.approval, "verify", verify):
            self.assertEqual(self.cli("--json"), (1, "", "APPROVAL_ANCHOR_UNAVAILABLE\n"))


class ReadOnlyTests(Base):
    def test_report_is_read_only_on_all_outcomes(self):
        self.enable()
        self.written("old", "doc-1")
        self.written("new", "doc-2", write_at=T0 + DAY)
        sentinel = os.path.join(self.project, "sentinel.txt")
        Path(sentinel).write_text("keep")
        before = fingerprint(self.project)
        opened = []
        real_open = os.open

        def spy(path, flags, *a, **k):
            if str(path).startswith(self.project):
                opened.append(flags)
            return real_open(path, flags, *a, **k)
        denied = {name: mock.Mock(side_effect=AssertionError(name)) for name in (
            "propose", "approve", "decide", "write_change", "enable", "turn_off", "init")}
        os.chmod(sentinel, 0o400)
        for sub in (pp.OUT,):
            os.chmod(os.path.join(self.project, sub), 0o500)
        os.chmod(os.path.join(self.project, pp.JOURNAL), 0o400)
        os.chmod(self.project, 0o500)
        self.addCleanup(os.chmod, self.project, 0o700)
        self.addCleanup(os.chmod, os.path.join(self.project, pp.OUT), 0o700)
        sealed = fingerprint(self.project)
        with mock.patch.object(pp.os, "open", spy), mock.patch.multiple(pp, **denied):
            outcomes = [self.cli("--json")]
            self.only("new@example.invalid")
            outcomes += [self.cli("--json"), self.cli("--json")]
            self.keys.file.unlink()
            outcomes.append(self.cli("--json"))
        self.assertEqual([o[0] for o in outcomes], [0, 0, 0, 1])
        self.assertEqual(len(json.loads(outcomes[1][1])["packets"]), 1)
        self.assertEqual(outcomes[1], outcomes[2])
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
        self.assertTrue(opened)
        self.assertFalse([f for f in opened if f & write_flags], opened)
        self.assertEqual(fingerprint(self.project), sealed)
        os.chmod(self.project, 0o700)
        os.chmod(os.path.join(self.project, pp.OUT), 0o700)
        os.chmod(os.path.join(self.project, pp.JOURNAL), 0o600)
        os.chmod(sentinel, 0o600)
        self.assertEqual(fingerprint(self.project), before)  # no cancel, re-proposal or rewrite event or file

    def test_bare_missing_lock_and_corrupt_project_refuse_correctly(self):
        bare = os.path.join(self.tmp.name, "bare")
        os.mkdir(bare)
        self.assertEqual(self.cli("--json", project=bare), (0, '{"packets": []}\n', ""))
        self.assertFalse(os.path.lexists(os.path.join(bare, pp.LOCK)))
        Path(bare, pp.JOURNAL).write_text("")
        self.assertEqual(self.cli("--json", project=bare), (1, "", "packets_unavailable\n"))
        self.assertFalse(os.path.lexists(os.path.join(bare, pp.LOCK)))
        self.assertEqual(self.cli(project=os.path.join(self.tmp.name, "missing"))[2], "project_invalid\n")
        Path(self.project, pp.JOURNAL).write_text("{not json\n")
        self.assertEqual(self.cli("--json"), (1, "", "journal_invalid\n"))
        os.replace(os.path.join(self.project, pp.LOCK), os.path.join(self.tmp.name, "real.lock"))
        os.symlink(os.path.join(self.tmp.name, "real.lock"), os.path.join(self.project, pp.LOCK))
        self.assertEqual(self.cli("--json"), (1, "", "packets_unavailable\n"))
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.project, link)
        self.assertEqual(self.cli(project=link)[2], "project_invalid\n")

    def test_cli_dispatch_and_existing_actions_unchanged(self):
        from campaign_tool.records import cli
        self.enable()
        ident, _ = self.written("old")
        self.only("new@example.invalid")
        out, err = io.StringIO(), io.StringIO()
        argv = ["records", "public-packets", "revoked-written", "--project", self.project, "--json"]
        with mock.patch.object(sys, "argv", argv), redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(cli.main(), 0)
        self.assertEqual(len(json.loads(out.getvalue())["packets"]), 1)
        self.assertEqual(json.loads(out.getvalue())["packets"][0]["id"], ident)
        self.assertEqual(pp.status(self.project, now=T0 + 2 * DAY)["switch"], "ON")  # not turned off by the report
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(pp.main(["status", "--project", self.project]), 0)
        self.assertEqual(set(json.loads(out.getvalue())), {"switch", "packets"})
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            pp.main(["status", "--project", self.project, "--bogus"])


if __name__ == "__main__":
    unittest.main()
