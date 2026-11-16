"""DF2007-10 (CON-LINE-APPROVE-1): G's signed approval of one public status line while auto-post is OFF."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import public_updates as pu
from tests.approval_signing import Signer

H = 3600
T0 = 1_793_000_000  # fixed clock
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "public_updates" / "line_approval_legacy_on.json"


def ev(kind, date, src="received_item"):
    return {"type": kind, "date": date, "source": {"kind": src, "item_id": f"{kind}{date}"}}


def cands(agency_key="ex", agency="Example PD", n=1):
    """Up to four distinct synthetic lines (L1, L2, L4, L5) for one agency."""
    events = [ev("sent", "2026-11-01", "outbox_receipt"), ev("acknowledged", "2026-11-02"),
              ev("determination", "2026-11-03"), ev("production", "2026-11-04")][:n]
    timeline = {"schema": "records-timeline-v1", "requests": [
        {"agency_key": agency_key, "agency": agency, "request_id": "r1", "issues": [], "events": events}]}
    return pu.generate(timeline, today="2026-11-05")[0]


def legacy_on_scenario(project):
    """Fixed-clock ON-only controls (hold edges, veto, cap, kill). The switch reads ON through a patch, so no signature is
    involved and the journal/feed bytes depend only on the code under test."""
    out = {}
    with mock.patch.object(pu, "_switch_state", return_value="ON"):
        pu.init(project)
        lines = cands("ex", "Example PD", 4) + cands("other", "Other PD", 1)
        pu.propose(project, lines, T0)
        veto = lines[4]["id"]
        results = [pu.advance(project, T0)]
        pu.retract(project, veto, "g", now=T0 + 5)
        for dt in (12 * H - 1, 12 * H, 12 * H + 1):
            results.append(pu.advance(project, T0 + dt))
        out["results"] = results
    out["journal"] = Path(project, pu.JOURNAL).read_text()
    out["feed"] = Path(project, pu.FEED).read_text()
    return out


def legacy_on_kill_scenario(project):
    out = {}
    with mock.patch.object(pu, "_switch_state",
                           side_effect=lambda lock, events: "KILLED" if os.path.lexists(lock.path(pu.KILL)) else "ON"):
        pu.init(project)
        pu.propose(project, cands("ex", "Example PD", 2), T0)
        first = pu.advance(project, T0)
        Path(project, pu.KILL).write_text("")
        second = pu.advance(project, T0 + 13 * H)
    out["results"] = [first, second]
    out["journal"] = Path(project, pu.JOURNAL).read_text()
    return out


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = os.path.join(tmp.name, "proj")
        pu.init(self.project)
        self.signer = Signer()
        self.addCleanup(self.signer.close)
        trust = self.signer.trust()
        trust.__enter__()
        self.addCleanup(trust.__exit__, None, None, None)

    def path(self, name):
        return os.path.join(self.project, name)

    def events(self, kind=None):
        with pu._Locked(self.project) as lock:
            return [e for e in lock.events() if kind is None or e["event"] == kind]

    def row(self, lid):
        return pu.fold(self.events())[0][lid]

    def propose(self, *lines):
        pu.propose(self.project, list(lines), T0)
        return [x["id"] for x in lines]

    def sign(self, lid, signer=None):
        return (signer or self.signer).sign(pu.line_payload(self.project, lid))

    def approve(self, lid, now=T0 + 1):
        return pu.approve_line(self.project, lid, self.sign(lid), revision=pu.line_revision(self.row(lid)), now=now)

    def state(self, lid):
        return self.row(lid)["state"]

    def feed_ids(self):
        if not os.path.exists(self.path(pu.FEED)):
            return []
        with open(self.path(pu.FEED)) as f:
            return [x["id"] for x in json.load(f)["lines"]]


class SignedLineTests(Base):
    def test_signed_line_posts_next_advance_while_off(self):
        a, b = self.propose(*cands(n=2))
        self.assertEqual(self.approve(a), "approved")
        self.assertEqual((self.state(a), self.feed_ids()), ("proposed", []))  # recording never posts
        self.assertFalse(os.path.exists(self.path(pu.FEED)))
        result = pu.advance(self.project, T0 + 2)  # no 12 h wait, no hold event
        self.assertEqual((result["state"], result["posted"], result["held"]), ("OFF", [a], []))
        self.assertEqual((self.state(a), self.state(b), self.feed_ids()), ("posted", "proposed", [a]))
        with open(self.path(pu.SWITCH)) as f:
            self.assertEqual(json.load(f), {"enabled": False})
        self.assertEqual(pu.switch_state(self.project), "OFF")
        self.assertEqual(self.events("hold") + self.events("switch"), [])
        again = pu.advance(self.project, T0 + 3)  # repeat advance: no second post
        self.assertEqual(again["posted"], [])
        self.assertEqual(len(self.events("post")), 1)
        with self.assertRaises(pu.PublicUpdatesError) as raised:  # a posted line cannot be approved again
            self.approve(a)
        self.assertEqual(raised.exception.code, "invalid_transition")
        self.assertEqual(len(self.events("line_approval")), 1)

    def test_a_holding_line_posts_without_waiting_and_a_repeat_is_a_noop(self):
        (a,) = self.propose(*cands())
        with mock.patch.object(pu, "_switch_state", return_value="ON"):
            pu.advance(self.project, T0)
        self.assertEqual(self.state(a), "holding")
        self.assertEqual(self.approve(a), "approved")
        sig, rev = self.sign(a), pu.line_revision(self.row(a))
        self.assertEqual(pu.approve_line(self.project, a, sig, revision=rev, now=T0 + 4), "already")
        self.assertEqual(len(self.events("line_approval")), 1)  # an identical repeat is not journaled twice
        self.assertEqual(pu.advance(self.project, T0 + 60)["posted"], [a])  # hold would run to T0 + 12 h
        self.assertEqual(self.feed_ids(), [a])

    def test_manual_authorisation_is_public_to_the_row_without_the_signature(self):
        (a,) = self.propose(*cands())
        self.approve(a)
        row = self.row(a)
        self.assertEqual((row["signed_revision"], row["signed_by"]), (pu.line_revision(row), self.signer.principal))
        self.assertNotIn("signature", json.dumps(row))

    def test_kill_file_blocks_an_approved_line_and_kill_after_the_file_is_removed_stays(self):
        a, b = self.propose(*cands(n=2))
        self.approve(a)
        self.approve(b)
        Path(self.path(pu.KILL)).write_text("")
        result = pu.advance(self.project, T0 + 5)
        self.assertEqual((result["state"], result["posted"], sorted(result["retracted"])), ("KILLED", [], sorted([a, b])))
        self.assertEqual((self.state(a), self.feed_ids()), ("retracted", []))
        os.unlink(self.path(pu.KILL))
        self.assertEqual(pu.advance(self.project, T0 + 6)["posted"], [])  # deleting the kill file resurrects nothing
        self.assertEqual(pu.switch_state(self.project), "OFF")

    def test_kill_arriving_between_two_candidates_wins(self):
        a, b = self.propose(*cands(n=2))
        self.approve(a)
        self.approve(b)
        real = pu._authorized

        def arrive(project, row, approvals):
            ok = real(project, row, approvals)
            Path(self.path(pu.KILL)).write_text("")  # a kill appears after the first candidate was checked
            return ok
        with mock.patch.object(pu, "_authorized", arrive):
            result = pu.advance(self.project, T0 + 5)
        self.assertEqual((len(result["posted"]), result["state"], len(result["retracted"])), (1, "KILLED", 1))

    def test_the_kill_api_refuses_a_new_approval(self):
        (a,) = self.propose(*cands())
        pu.kill(self.project, "g", now=T0 + 1)
        with self.assertRaises(pu.PublicUpdatesError) as raised:
            self.approve(a)
        self.assertEqual(raised.exception.code, "invalid_transition")  # the kill retracted the waiting line

    def test_veto_after_approval_blocks_and_undo_is_terminal(self):
        a, b = self.propose(*cands(n=2))
        self.approve(a)
        self.approve(b)
        pu.retract(self.project, a, "g", now=T0 + 2)  # veto
        self.assertEqual(pu.advance(self.project, T0 + 3)["posted"], [b])
        pu.retract(self.project, b, "g", now=T0 + 4)  # undo of the posted line
        self.assertEqual((self.state(a), self.state(b), self.feed_ids()), ("retracted", "retracted", []))
        self.assertEqual(pu.advance(self.project, T0 + 5)["posted"], [])
        self.assertEqual(len(self.events("post")), 1)
        with self.assertRaises(pu.PublicUpdatesError):
            pu.approve_line(self.project, a, self.sign(a), revision=pu.line_revision(self.row(a)))

    def test_changed_text_voids_the_approval_and_needs_a_new_one(self):
        (a,) = self.propose(*cands())
        old_rev = pu.line_revision(self.row(a))
        old_sig = self.sign(a)
        self.approve(a)
        raw = Path(self.path(pu.JOURNAL)).read_text()
        edited = raw.replace("Example PD", "Changed PD")  # a valid alternate wording, as if the line had been replaced
        self.assertNotEqual(raw, edited)
        Path(self.path(pu.JOURNAL)).write_text(edited)
        self.assertNotEqual(pu.line_revision(self.row(a)), old_rev)
        self.assertEqual(pu.advance(self.project, T0 + 5)["posted"], [])
        self.assertEqual(self.state(a), "proposed")
        with self.assertRaises(pu.PublicUpdatesError) as raised:  # the old form's revision is stale
            pu.approve_line(self.project, a, old_sig, revision=old_rev)
        self.assertEqual(raised.exception.code, "stale_revision")
        with self.assertRaises(pu.PublicUpdatesError) as raised:  # the old signature does not cover the new text
            pu.approve_line(self.project, a, old_sig, revision=pu.line_revision(self.row(a)))
        self.assertEqual(raised.exception.code, "approval_required")
        self.assertEqual(self.approve(a, now=T0 + 6), "approved")  # a new signature over the new payload works
        self.assertEqual(pu.advance(self.project, T0 + 7)["posted"], [a])

    def test_text_that_no_longer_matches_its_wording_is_refused_even_with_a_matching_revision(self):
        (a,) = self.propose(*cands())
        raw = Path(self.path(pu.JOURNAL)).read_text()
        Path(self.path(pu.JOURNAL)).write_text(raw.replace("We sent a public records request to Example PD.",
                                                           "Free text they are lying."))
        with self.assertRaises(pu.PublicUpdatesError) as raised:
            self.approve(a)
        self.assertEqual(raised.exception.code, "text_mismatch")
        self.assertEqual(self.events("line_approval"), [])

    def test_forged_and_wrong_scope_signatures_are_refused_and_nothing_is_journaled(self):
        a, b = self.propose(*cands(n=2))
        rev = pu.line_revision(self.row(a))
        stranger = Signer("stranger@example.invalid", trusted=False)
        self.addCleanup(stranger.close)
        for label, signature in (("typed name", "I approve"), ("garbage", "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"),
                                 ("other line", self.sign(b)), ("switch scope", self.signer.sign(pu.switch_payload(self.project))),
                                 ("untrusted key", self.sign(a, stranger)), ("blank", "  "), ("oversize", "x" * 9000)):
            with self.assertRaises(pu.PublicUpdatesError, msg=label) as raised:
                pu.approve_line(self.project, a, signature, revision=rev)
            self.assertEqual(raised.exception.code, "approval_required", label)
        with self.assertRaises(pu.PublicUpdatesError):
            pu.approve_line(self.project, a, None, revision=rev)
        self.assertEqual(self.events("line_approval"), [])
        self.assertEqual(pu.advance(self.project, T0 + 2)["posted"], [])

    def test_a_forged_event_in_the_journal_does_not_post(self):
        (a,) = self.propose(*cands())
        rev = pu.line_revision(self.row(a))
        with pu._Locked(self.project) as lock:
            lock.append("line_approval", T0 + 1, id=a, revision=rev, principal="G", signature="SIG:forged")
        self.assertEqual(pu.advance(self.project, T0 + 2)["posted"], [])
        self.assertEqual(self.state(a), "proposed")

    def test_trust_unavailable_is_deferred_never_final_and_a_retry_posts_once(self):
        (a,) = self.propose(*cands())
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("anchor")):
            with self.assertRaises(pu.PublicUpdatesError) as raised:
                pu.approve_line(self.project, a, self.sign(a), revision=pu.line_revision(self.row(a)))
        self.assertEqual(raised.exception.code, "approval_unavailable")
        self.assertEqual(self.events("line_approval"), [])
        self.approve(a)
        with mock.patch.object(approval, "verify", side_effect=approval.TrustUnavailable("verifier")):
            result = pu.advance(self.project, T0 + 5)
        self.assertEqual((result["posted"], result["refused"], result["deferred"]), ([], [], [a]))
        self.assertEqual(self.state(a), "proposed")  # neither posted, refused nor consumed
        self.assertEqual(pu.advance(self.project, T0 + 6)["posted"], [a])
        self.assertEqual(len(self.events("post")), 1)

    def test_caps_still_cover_manual_lines_and_a_refusal_is_final_with_an_alert(self):
        ids = self.propose(*cands(n=4))
        for lid in ids:
            self.approve(lid)
        result = pu.advance(self.project, T0 + 5)
        self.assertEqual(len(result["posted"]), pu.CAP_PER_AGENCY_DAY)
        self.assertEqual([code for _, code in result["refused"]], ["cap_agency_day"])
        refused = result["refused"][0][0]
        self.assertEqual((self.state(refused), len(self.events("alert"))), ("refused", 1))
        self.assertEqual(pu.advance(self.project, T0 + 6), {"state": "OFF", "held": [], "posted": [], "refused": [], "retracted": []})
        pu.retract(self.project, result["posted"][0], "g", now=T0 + 7)  # undo still consumes the historical cap
        (extra,) = self.propose(*cands("other", "Other PD", 1))
        self.assertEqual(self.approve(extra), "approved")
        self.assertEqual(len(pu.advance(self.project, T0 + 8)["posted"]), 1)

    def test_total_day_cap_counts_manual_and_automatic_posts(self):
        auto = [cands(f"a{i}", f"Agency {i}", 1)[0] for i in range(pu.CAP_PER_DAY)]
        self.propose(*auto)
        with mock.patch.object(pu, "_switch_state", return_value="ON"):
            pu.advance(self.project, T0)
            self.assertEqual(len(pu.advance(self.project, T0 + 12 * H)["posted"]), pu.CAP_PER_DAY)
        (extra,) = self.propose(*cands("extra", "Extra PD", 1))
        self.approve(extra, now=T0 + 12 * H + 1)
        result = pu.advance(self.project, T0 + 12 * H + 2)
        self.assertEqual((result["posted"], result["refused"]), ([], [[extra, "cap_day"]]))

    def test_with_the_switch_on_an_approved_line_skips_the_wait_and_others_keep_the_hold(self):
        a, b = self.propose(*cands(n=2))
        self.approve(a)
        with mock.patch.object(pu, "_switch_state", return_value="ON"):
            first = pu.advance(self.project, T0 + 2)
            self.assertEqual((first["posted"], first["held"]), ([a], [a, b]))  # a is held as usual, then posts at once
            self.assertEqual(pu.advance(self.project, T0 + 12 * H + 1)["held"], [])
            self.assertEqual(self.state(b), "holding")
            self.assertEqual(pu.advance(self.project, T0 + 2 + 12 * H)["posted"], [b])


class LegacyOnTests(unittest.TestCase):
    def test_on_without_manual_approval_is_byte_identical(self):
        expected = json.loads(FIXTURE.read_text())  # captured from the code before this unit, same fixed clock
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(legacy_on_scenario(os.path.join(tmp, "p")), expected["normal"])
            self.assertEqual(legacy_on_kill_scenario(os.path.join(tmp, "k")), expected["kill"])


class CommandLineTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pu.main(["--project", self.project, *args])
        return code, out.getvalue(), err.getvalue()

    def test_line_payload_and_approve_line_over_the_cli(self):
        (a,) = self.propose(*cands())
        row = self.row(a)
        pid = pu.project_id(self.project)
        content = f"project: {pid}\nline: {a}\nrevision: {pu.line_revision(row)}\ntext:\n{row['text']}"
        want = approval.payload("public-update-line-v1", pid, content)
        self.assertEqual(pu.line_payload(self.project, a), want)
        self.assertEqual(pu.line_payload_for_row(self.project, row), want)
        sigfile = Path(self.project).parent / "sig"
        sigfile.write_text(self.signer.sign(want))
        code, out, err = self.run_cli("approve-line", "--line", a, "--revision", pu.line_revision(row), "--signature-file", str(sigfile))
        self.assertEqual((code, json.loads(out)["result"], err), (0, "approved", ""))
        self.assertEqual(len(self.events("line_approval")), 1)

    def test_missing_or_mixed_operands_refuse_before_any_mutation(self):
        (a,) = self.propose(*cands())
        before = Path(self.path(pu.JOURNAL)).read_bytes()
        for args in (("approve-line",), ("approve-line", "--line", a), ("approve-line", "--line", a, "--revision", "0" * 64),
                     ("line-payload",), ("line-payload", "--line", a, "--revision", "0" * 64),
                     ("approve-line", "--line", a, "--revision", "0" * 64, "--signature-file", "/nonexistent")):
            code, out, err = self.run_cli(*args)
            self.assertEqual((code, out), (1, ""), args)
        self.assertEqual(Path(self.path(pu.JOURNAL)).read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
