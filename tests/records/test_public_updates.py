import json
import os

import pytest

from campaign_tool import approval
from campaign_tool.records import public_updates as pu

H = 3600
T0 = 1_793_000_000  # fixed clock


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    pu.init(str(root))
    # stand-in verifier: "SIG:<principal>:<hex of exact payload>" proves a signature over those bytes
    def fake_verify(data, signature):
        if not signature.startswith("SIG:") or signature.split(":")[2] != data.hex():
            raise approval.ApprovalError("bad")
        return signature.split(":")[1]
    monkeypatch.setattr(pu.approval, "verify", fake_verify)
    return str(root)


def sign(project, who="G"):
    return f"SIG:{who}:{pu.switch_payload(project).hex()}"


def ev(kind, date="2026-11-03", src="received_item", **extra):
    return {"type": kind, "date": date, "source": {"kind": src, "item_id": f"{kind}{date}"}, **extra}


def tl(*events, agency="Example PD", key="ex", issues=()):
    return {"schema": "records-timeline-v1", "requests": [
        {"agency_key": key, "agency": agency, "request_id": "r1", "issues": list(issues), "events": list(events)}]}


def cands(*events, **kw):
    return pu.generate(tl(*events, **kw), today="2026-11-05")


def test_templates_render_fixed_wording():
    lines, _ = cands(ev("sent", src="outbox_receipt"), ev("acknowledged"), ev("extension notice", new_dates=["2026-11-20"]),
                     ev("determination", "2026-11-04"), ev("production"), ev("our follow-up sent", src="outbox_receipt"))
    assert sorted(l["template"] for l in lines) == ["L1", "L2", "L3", "L4", "L5", "L6"]
    by = {l["template"]: l["text"] for l in lines}
    assert by["L3"] == "2026-11-03: Example PD said it needs more time and expects to respond by 2026-11-20."
    assert by["L5"] == "2026-11-03: Example PD released records. We review records before publishing any of them."
    assert by["L6"] == "2026-11-03: We sent Example PD a follow-up on our request."


def test_reference_shown_and_free_text_refused():
    t = tl(ev("sent", src="outbox_receipt"))
    t["requests"][0]["agency_reference"] = "PRA 26-348"
    assert pu.generate(t, today="2026-11-05")[0][0]["text"].endswith("to Example PD (reference PRA 26-348).")
    with pytest.raises(pu.PublicUpdatesError):
        pu.render("L4", {"date": "2026-11-03", "agency": "X\nFree text: they are lying"})
    with pytest.raises(pu.PublicUpdatesError):
        pu.render("L4", {"date": "2026-11-03", "agency": "X", "extra": "no"})


def test_never_auto_and_unsourced_events_make_no_line():
    lines, skipped = cands(ev("denial"), ev("no records"), ev("fee estimate"), ev("closed"),
                           ev("determination"), ev("production", "2026-11-03"),  # same-day denial blocks L4/L5
                           ev("acknowledged", "2026-11-04", src="draft"), ev("sent", "2026-11-04", src="manual"),
                           ev("extension notice", "2026-11-04", new_dates=[]),
                           ev("acknowledged", "2026-12-01"),  # future
                           ev("acknowledged", "2026-11-02", corrects="x"), ev("acknowledged", "2026-11-01", superseded=True))
    assert lines == []
    assert {s["reason"] for s in skipped} >= {"never_auto", "never_auto_same_day", "no_source", "no_agency_date",
                                              "bad_date", "correction"}


def test_issue_flag_blocks_every_line():
    assert cands(ev("acknowledged"), issues=["unverified citation"])[0] == []


def test_ships_off_and_hand_edited_file_stays_off(project):
    assert pu.switch_state(project) == "OFF"
    assert json.load(open(os.path.join(project, pu.SWITCH))) == {"enabled": False}
    open(os.path.join(project, pu.SWITCH), "w").write('{"enabled": true}')
    assert pu.switch_state(project) == "OFF"
    pu.propose(project, cands(ev("acknowledged"))[0], T0)
    assert pu.advance(project, T0 + 20 * H)["posted"] == []


def test_enable_needs_g_signature_and_old_signature_is_stale(project):
    with pytest.raises(pu.PublicUpdatesError) as e:
        pu.enable(project, "SIG:G:00", T0)
    assert e.value.code == "approval_required"
    with pytest.raises(pu.PublicUpdatesError):
        pu.enable(project, "I approve", T0)
    old = sign(project)
    assert pu.enable(project, old, T0) == "ON"
    pu.turn_off(project, "console", T0 + 1)
    assert pu.switch_state(project) == "OFF"
    with pytest.raises(pu.PublicUpdatesError):  # replaying the earlier signature fails: the round moved on
        pu.enable(project, old, T0 + 2)
    assert pu.enable(project, sign(project), T0 + 3) == "ON"
    assert pu.switch_state(project) == "ON"


def on(project):
    pu.enable(project, sign(project), T0)


def test_hold_then_post_then_duplicate_and_feed(project):
    on(project)
    lines = cands(ev("acknowledged"))[0]
    assert pu.propose(project, lines, T0) == [lines[0]["id"]]
    assert pu.propose(project, lines, T0) == []  # duplicate event: one line only
    assert pu.advance(project, T0)["held"] == [lines[0]["id"]]
    assert pu.advance(project, T0 + 12 * H - 1)["posted"] == []  # still holding
    assert pu.advance(project, T0 + 12 * H)["posted"] == [lines[0]["id"]]
    assert pu.advance(project, T0 + 13 * H)["posted"] == []  # never posted twice
    feed = json.load(open(os.path.join(project, pu.FEED)))
    assert [l["text"] for l in feed["lines"]] == [lines[0]["text"]]


def test_veto_during_hold_and_undo_after_post(project):
    on(project)
    a, b = cands(ev("acknowledged"), ev("production"))[0]
    pu.propose(project, [a, b], T0)
    pu.advance(project, T0)
    pu.retract(project, a["id"], "g", "veto", T0 + H)
    posted = pu.advance(project, T0 + 13 * H)["posted"]
    assert posted == [b["id"]]
    pu.retract(project, b["id"], "g", "undo", T0 + 14 * H)
    assert json.load(open(os.path.join(project, pu.FEED)))["lines"] == []
    with pytest.raises(pu.PublicUpdatesError) as e:
        pu.retract(project, b["id"], "g", "undo", T0 + 15 * H)
    assert e.value.code == "invalid_transition"
    with pu._Locked(project) as lock:
        assert [x["event"] for x in lock.events()].count("retract") == 2  # history kept
    assert pu.advance(project, T0 + 40 * H)["posted"] == []  # a retracted event is never re-posted
    assert pu.propose(project, [b], T0 + 41 * H) == []


def test_kill_switch_stops_posting_and_retracts_holding(project):
    on(project)
    (a,) = cands(ev("acknowledged"))[0]
    pu.propose(project, [a], T0)
    pu.advance(project, T0)
    assert pu.kill(project, "console", T0 + H) == [a["id"]]
    assert pu.switch_state(project) == "KILLED"
    assert os.path.exists(os.path.join(project, pu.KILL))
    assert pu.advance(project, T0 + 20 * H)["posted"] == []
    with pytest.raises(pu.PublicUpdatesError) as e:
        pu.enable(project, sign(project), T0 + 21 * H)
    assert e.value.code == "killed"
    os.unlink(os.path.join(project, pu.KILL))  # removal by hand does not switch back on
    assert pu.switch_state(project) == "OFF"


def test_kill_file_created_by_hand_is_checked_before_post(project):
    on(project)
    (a,) = cands(ev("acknowledged"))[0]
    pu.propose(project, [a], T0)
    pu.advance(project, T0)
    open(os.path.join(project, pu.KILL), "w").close()
    out = pu.advance(project, T0 + 13 * H)
    assert out["posted"] == [] and out["retracted"] == [a["id"]]


def test_caps_refuse_and_alert_never_queue(project):
    on(project)
    pu.propose(project, [], T0)
    many = [ev("acknowledged", f"2026-11-0{d}") for d in range(1, 5)]
    lines, _ = cands(*many)
    assert len(lines) == 4
    pu.propose(project, lines, T0)
    pu.advance(project, T0)
    out = pu.advance(project, T0 + 12 * H)
    assert len(out["posted"]) == 3 and [c for _, c in out["refused"]] == ["cap_agency_day"]
    assert pu.status(project)["lines"]["refused"] == 1 and pu.status(project)["alerts"] == 1
    assert pu.advance(project, T0 + 30 * H)["posted"] == []  # refused is final, not queued


def test_daily_cap_across_agencies(project):
    on(project)
    allc = []
    for i in range(11):
        allc += cands(ev("acknowledged"), agency=f"Agency {i}", key=f"a{i}")[0]
    pu.propose(project, allc, T0)
    pu.advance(project, T0)
    out = pu.advance(project, T0 + 12 * H)
    assert len(out["posted"]) == 10 and [c for _, c in out["refused"]] == ["cap_day"]


def test_tampered_journal_text_is_refused(project):
    on(project)
    (a,) = cands(ev("acknowledged"))[0]
    with pytest.raises(pu.PublicUpdatesError) as e:
        pu.propose(project, [dict(a, text=a["text"] + " They lied.")], T0)
    assert e.value.code == "text_mismatch"


def test_no_network_or_send_surface():
    import inspect
    src = inspect.getsource(pu)
    for word in ("import socket", "smtplib", "urllib", "import requests", "subprocess", "http.client"):
        assert word not in src


def test_cli_status_and_seam(project, capsys):
    from campaign_tool.records import cli
    assert cli.COMMANDS["public-updates"] == ("public_updates", "main")
    assert pu.main(["status", "--project", project]) == 0
    assert json.loads(capsys.readouterr().out)["switch"] == "OFF"
