import contextlib
import inspect
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from campaign_tool import approval
from campaign_tool.records import public_updates as pu

H = 3600
T0 = 1_793_000_000  # fixed clock


def fake_verify(data, signature):
    # stand-in verifier: "SIG:<principal>:<hex of exact payload>" proves a signature over those bytes
    if not signature.startswith("SIG:") or signature.split(":")[2] != data.hex():
        raise approval.ApprovalError("bad")
    return signature.split(":")[1]


def ev(kind, date="2026-11-03", src="received_item", **extra):
    return {"type": kind, "date": date, "source": {"kind": src, "item_id": f"{kind}{date}"}, **extra}


def tl(*events, agency="Example PD", key="ex", issues=()):
    return {"schema": "records-timeline-v1", "requests": [
        {"agency_key": key, "agency": agency, "request_id": "r1", "issues": list(issues), "events": list(events)}]}


def cands(*events, **kw):
    return pu.generate(tl(*events, **kw), today="2026-11-05")


class GeneratorTests(unittest.TestCase):
    def test_templates_render_fixed_wording(self):
        lines, _ = cands(ev("sent", src="outbox_receipt"), ev("acknowledged"),
                         ev("extension notice", new_dates=["2026-11-20"]), ev("determination", "2026-11-04"),
                         ev("production"), ev("our follow-up sent", src="outbox_receipt"))
        self.assertEqual(sorted(x["template"] for x in lines), ["L1", "L2", "L3", "L4", "L5", "L6"])
        by = {x["template"]: x["text"] for x in lines}
        self.assertEqual(by["L3"], "2026-11-03: Example PD said it needs more time and expects to respond by 2026-11-20.")
        self.assertEqual(by["L5"], "2026-11-03: Example PD released records. We review records before publishing any of them.")
        self.assertEqual(by["L6"], "2026-11-03: We sent Example PD a follow-up on our request.")

    def test_reference_shown_and_free_text_refused(self):
        t = tl(ev("sent", src="outbox_receipt"))
        t["requests"][0]["agency_reference"] = "PRA 26-348"
        self.assertTrue(pu.generate(t, today="2026-11-05")[0][0]["text"].endswith("to Example PD (reference PRA 26-348)."))
        with self.assertRaises(pu.PublicUpdatesError):
            pu.render("L4", {"date": "2026-11-03", "agency": "X\nFree text: they are lying"})
        with self.assertRaises(pu.PublicUpdatesError):
            pu.render("L4", {"date": "2026-11-03", "agency": "X", "extra": "no"})

    def test_never_auto_and_unsourced_events_make_no_line(self):
        lines, skipped = cands(
            ev("denial"), ev("no records"), ev("fee estimate"), ev("closed"),
            ev("determination"), ev("production", "2026-11-03"),  # same-day denial blocks L4/L5
            ev("acknowledged", "2026-11-04", src="draft"), ev("sent", "2026-11-04", src="manual"),
            ev("extension notice", "2026-11-04", new_dates=[]),
            ev("acknowledged", "2026-12-01"),  # future
            ev("acknowledged", "2026-11-02", corrects="x"), ev("acknowledged", "2026-11-01", superseded=True))
        self.assertEqual(lines, [])
        self.assertTrue({s["reason"] for s in skipped} >= {
            "never_auto", "never_auto_same_day", "no_source", "no_agency_date", "bad_date", "correction"})

    def test_issue_flag_blocks_every_line(self):
        self.assertEqual(cands(ev("acknowledged"), issues=["unverified citation"])[0], [])

    def test_no_network_or_send_surface(self):
        src = inspect.getsource(pu)
        for word in ("import socket", "smtplib", "urllib", "import requests", "subprocess", "http.client"):
            self.assertNotIn(word, src)


class ProjectTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.project = os.path.join(tmp.name, "proj")
        pu.init(self.project)
        patcher = mock.patch.object(pu.approval, "verify", fake_verify)
        patcher.start()
        self.addCleanup(patcher.stop)

    def path(self, name):
        return os.path.join(self.project, name)

    def sign(self, who="G"):
        return f"SIG:{who}:{pu.switch_payload(self.project).hex()}"

    def on(self):
        pu.enable(self.project, self.sign(), T0)

    def journal_events(self):
        with pu._Locked(self.project) as lock:
            return lock.events()


class SwitchTests(ProjectTestCase):
    def test_ships_off_and_hand_edited_file_stays_off(self):
        self.assertEqual(pu.switch_state(self.project), "OFF")
        with open(self.path(pu.SWITCH)) as f:
            self.assertEqual(json.load(f), {"enabled": False})
        with open(self.path(pu.SWITCH), "w") as f:
            f.write('{"enabled": true}')
        self.assertEqual(pu.switch_state(self.project), "OFF")
        pu.propose(self.project, cands(ev("acknowledged"))[0], T0)
        self.assertEqual(pu.advance(self.project, T0 + 20 * H)["posted"], [])

    def test_enable_needs_g_signature_and_old_signature_is_stale(self):
        with self.assertRaises(pu.PublicUpdatesError) as e:
            pu.enable(self.project, "SIG:G:00", T0)
        self.assertEqual(e.exception.code, "approval_required")
        with self.assertRaises(pu.PublicUpdatesError):
            pu.enable(self.project, "I approve", T0)
        old = self.sign()
        self.assertEqual(pu.enable(self.project, old, T0), "ON")
        pu.turn_off(self.project, "console", T0 + 1)
        self.assertEqual(pu.switch_state(self.project), "OFF")
        with self.assertRaises(pu.PublicUpdatesError):  # replaying the earlier signature fails: the round moved on
            pu.enable(self.project, old, T0 + 2)
        self.assertEqual(pu.enable(self.project, self.sign(), T0 + 3), "ON")
        self.assertEqual(pu.switch_state(self.project), "ON")

    def test_kill_switch_stops_posting_and_retracts_holding(self):
        self.on()
        (a,) = cands(ev("acknowledged"))[0]
        pu.propose(self.project, [a], T0)
        pu.advance(self.project, T0)
        self.assertEqual(pu.kill(self.project, "console", T0 + H), [a["id"]])
        self.assertEqual(pu.switch_state(self.project), "KILLED")
        self.assertTrue(os.path.exists(self.path(pu.KILL)))
        self.assertEqual(pu.advance(self.project, T0 + 20 * H)["posted"], [])
        with self.assertRaises(pu.PublicUpdatesError) as e:
            pu.enable(self.project, self.sign(), T0 + 21 * H)
        self.assertEqual(e.exception.code, "killed")
        os.unlink(self.path(pu.KILL))  # removal by hand does not switch back on
        self.assertEqual(pu.switch_state(self.project), "OFF")

    def test_kill_file_created_by_hand_is_checked_before_post(self):
        self.on()
        (a,) = cands(ev("acknowledged"))[0]
        pu.propose(self.project, [a], T0)
        pu.advance(self.project, T0)
        open(self.path(pu.KILL), "w").close()
        out = pu.advance(self.project, T0 + 13 * H)
        self.assertEqual(out["posted"], [])
        self.assertEqual(out["retracted"], [a["id"]])
        # the per-post detection persisted OFF: removing the file by hand must not restore ON
        os.unlink(self.path(pu.KILL))
        self.assertEqual(pu.switch_state(self.project), "OFF")
        with open(self.path(pu.SWITCH)) as f:
            self.assertEqual(json.load(f), {"enabled": False})
        self.assertEqual(self.journal_events()[-2]["state"], "killed")
        self.assertEqual(pu.advance(self.project, T0 + 40 * H)["posted"], [])
        # only a fresh signed ON turns it back on
        self.assertEqual(pu.enable(self.project, self.sign(), T0 + 41 * H), "ON")

    def test_kill_file_created_by_hand_is_persisted_on_plain_advance(self):
        self.on()
        open(self.path(pu.KILL), "w").close()
        self.assertEqual(pu.advance(self.project, T0 + H)["state"], "KILLED")
        self.assertEqual(self.journal_events()[-1]["state"], "killed")
        n = len(self.journal_events())
        pu.advance(self.project, T0 + 2 * H)  # observing again does not pile up switch events
        self.assertEqual(len(self.journal_events()), n)
        os.unlink(self.path(pu.KILL))
        self.assertEqual(pu.switch_state(self.project), "OFF")
        with open(self.path(pu.SWITCH)) as f:
            self.assertEqual(json.load(f), {"enabled": False})


class LineTests(ProjectTestCase):
    def test_hold_then_post_then_duplicate_and_feed(self):
        self.on()
        lines = cands(ev("acknowledged"))[0]
        self.assertEqual(pu.propose(self.project, lines, T0), [lines[0]["id"]])
        self.assertEqual(pu.propose(self.project, lines, T0), [])  # duplicate event: one line only
        self.assertEqual(pu.advance(self.project, T0)["held"], [lines[0]["id"]])
        self.assertEqual(pu.advance(self.project, T0 + 12 * H - 1)["posted"], [])  # still holding
        self.assertEqual(pu.advance(self.project, T0 + 12 * H)["posted"], [lines[0]["id"]])
        self.assertEqual(pu.advance(self.project, T0 + 13 * H)["posted"], [])  # never posted twice
        with open(self.path(pu.FEED)) as f:
            feed = json.load(f)
        self.assertEqual([x["text"] for x in feed["lines"]], [lines[0]["text"]])

    def test_veto_during_hold_and_undo_after_post(self):
        self.on()
        a, b = cands(ev("acknowledged"), ev("production"))[0]
        pu.propose(self.project, [a, b], T0)
        pu.advance(self.project, T0)
        pu.retract(self.project, a["id"], "g", "veto", T0 + H)
        self.assertEqual(pu.advance(self.project, T0 + 13 * H)["posted"], [b["id"]])
        pu.retract(self.project, b["id"], "g", "undo", T0 + 14 * H)
        with open(self.path(pu.FEED)) as f:
            self.assertEqual(json.load(f)["lines"], [])
        with self.assertRaises(pu.PublicUpdatesError) as e:
            pu.retract(self.project, b["id"], "g", "undo", T0 + 15 * H)
        self.assertEqual(e.exception.code, "invalid_transition")
        self.assertEqual([x["event"] for x in self.journal_events()].count("retract"), 2)  # history kept
        self.assertEqual(pu.advance(self.project, T0 + 40 * H)["posted"], [])  # a retracted event is never re-posted
        self.assertEqual(pu.propose(self.project, [b], T0 + 41 * H), [])

    def test_caps_refuse_and_alert_never_queue(self):
        self.on()
        pu.propose(self.project, [], T0)
        many = [ev("acknowledged", f"2026-11-0{d}") for d in range(1, 5)]
        lines, _ = cands(*many)
        self.assertEqual(len(lines), 4)
        pu.propose(self.project, lines, T0)
        pu.advance(self.project, T0)
        out = pu.advance(self.project, T0 + 12 * H)
        self.assertEqual(len(out["posted"]), 3)
        self.assertEqual([c for _, c in out["refused"]], ["cap_agency_day"])
        self.assertEqual(pu.status(self.project)["lines"]["refused"], 1)
        self.assertEqual(pu.status(self.project)["alerts"], 1)
        self.assertEqual(pu.advance(self.project, T0 + 30 * H)["posted"], [])  # refused is final, not queued

    def test_daily_cap_across_agencies(self):
        self.on()
        allc = []
        for i in range(11):
            allc += cands(ev("acknowledged"), agency=f"Agency {i}", key=f"a{i}")[0]
        pu.propose(self.project, allc, T0)
        pu.advance(self.project, T0)
        out = pu.advance(self.project, T0 + 12 * H)
        self.assertEqual(len(out["posted"]), 10)
        self.assertEqual([c for _, c in out["refused"]], ["cap_day"])

    def test_tampered_journal_text_is_refused(self):
        self.on()
        (a,) = cands(ev("acknowledged"))[0]
        with self.assertRaises(pu.PublicUpdatesError) as e:
            pu.propose(self.project, [dict(a, text=a["text"] + " They lied.")], T0)
        self.assertEqual(e.exception.code, "text_mismatch")

    def test_cli_status_and_seam(self):
        from campaign_tool.records import cli
        self.assertEqual(cli.COMMANDS["public-updates"], ("public_updates", "main"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(pu.main(["status", "--project", self.project]), 0)
        self.assertEqual(json.loads(out.getvalue())["switch"], "OFF")


if __name__ == "__main__":
    unittest.main()
