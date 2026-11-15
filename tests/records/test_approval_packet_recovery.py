"""Approved design packet recovery replay: 29 checks against candidate code, synthetic projects only."""
import os
import tempfile
import unittest
from unittest.mock import patch
from campaign_tool import approval
from campaign_tool.records import public_packets as pp, packet_changes, console_apply

OLD, NEW = "sig-old-key", "sig-new-key"
PRINCIPAL = {OLD: "g-old", NEW: "g-new"}
MODE = {"m": "ok"}
T0 = 1_794_600_000
FAILS = []
CHECKS = []


def verify(payload, sig):
    if MODE["m"] == "gone":
        raise approval.TrustUnavailable("trust file missing")
    if MODE["m"] == "rotated" and sig != NEW:
        raise approval.ApprovalError("no trusted principal")
    if sig not in PRINCIPAL:
        raise approval.ApprovalError("not a signature")
    return PRINCIPAL[sig]


def packet(title="Synthetic update"):
    return {"kind": "site_update", "agency_key": "synthetic-agency", "request_id": "", "source": "queue-synthetic",
            "title": title, "text": "Synthetic text, no record content."}


def try_(fn, *a, **k):
    try:
        return fn(*a, **k)
    except pp.PacketError as error:
        return "REFUSED " + error.code


def view(project, ident, now):
    rows = {r["id"]: r for r in pp.status(project, now=now)["packets"]}
    r = rows[ident]
    return f"{r['status']}@r{r['revision']} signed_by={r.get('signed_by')}"


def check(label, got, want):
    CHECKS.append(label)
    ok = got == want if not callable(want) else want(got)
    if not ok:
        FAILS.append(label)


def console_codes():
    return console_apply.CODES


def fresh(base, name):
    project = os.path.join(base, name)
    pp.init(project)
    return project


class PacketRotationReplay(unittest.TestCase):
    def test_29_check_recovery_replay(self):
        FAILS.clear()
        CHECKS.clear()
        with tempfile.TemporaryDirectory(prefix="f1b-packet-probe-") as base, \
                patch.object(approval, "verify", side_effect=verify):
            now = T0

            p = fresh(base, "r1")
            ident = pp.propose(p, packet(), now=now)["id"]
            MODE["m"] = "ok"
            pp.enable(p, OLD, now=now)
            pp.approve(p, ident, OLD, now=now, revision=0)
            MODE["m"] = "rotated"
            check("R1 status after rotation (fold/_view do not re-verify)", view(p, ident, now + 60),
                  "approved@r0 signed_by=g-old")
            check("R1 switch after rotation (switch re-verifies)", pp.status(p, now=now + 60)["switch"], "OFF")
            check("R2 page/CLI Approve of the approved packet with the NEW key", try_(pp.approve, p, ident, NEW, now=now + 60,
                  revision=0), "REFUSED packet_not_open")
            check("R2 console_apply receipt code for packet_not_open (CODES literal, read from source)",
                  console_codes()["packet_not_open"], "INVALID_TRANSITION")
            check("R3a write_change, switch still on the old key", try_(pp.write_change, p, ident, now=now + 60),
                  "REFUSED switch_off")
            pp.enable(p, NEW, now=now + 120)
            check("R3b switch after G re-enables it with the NEW key", pp.status(p, now=now + 120)["switch"], "ON")
            check("R3c write_change of the old-key approval (re-verifies before writing)",
                  try_(pp.write_change, p, ident, now=now + 120), "REFUSED approval_required")
            check("R3d status unchanged by the refusal", view(p, ident, now + 120), "approved@r0 signed_by=g-old")
            check("R3e no change file written", os.path.exists(os.path.join(p, pp.OUT, f"{ident}-r0.json")), False)

            check("R4a cancel (no signature; anchor-independent)", try_(pp.decide, p, ident, "cancelled", "console",
                  now=now + 180, revision=0), {"id": ident, "result": "cancelled"})
            got = pp.propose(p, packet(), now=now + 240)
            check("R4b propose identical content reopens a cancelled id", (got["result"], got["revision"]), ("proposed", 1))
            check("R4c new revision is open; expiry counts from the re-proposal",
                  [(r["status"], r["revision"], r["proposed_at"]) for r in pp.pending(p, now=now + 240) if r["id"] == ident],
                  [("proposed", 1, pp._iso(now + 240))])
            check("R4d approve with the NEW key at the new revision", pp.approve(p, ident, NEW, now=now + 300, revision=1),
                  {"id": ident, "signed_by": "g-new"})
            check("R4e write_change", try_(pp.write_change, p, ident, now=now + 360),
                  lambda g: isinstance(g, dict) and g.get("result") == "written" and g["file"].endswith(f"{ident}-r1.json"))
            check("R4f the old revision's approval never wrote anything",
                  os.path.exists(os.path.join(p, pp.OUT, f"{ident}-r0.json")), False)

            p5 = fresh(base, "r5")
            MODE["m"] = "ok"
            i5 = pp.propose(p5, packet(), now=now)["id"]
            pp.approve(p5, i5, OLD, now=now, revision=0)
            MODE["m"] = "rotated"
            pp.decide(p5, i5, "rejected", "console", now=now + 60, revision=0)
            got = pp.propose(p5, packet(), now=now + 120)
            check("R5a propose identical content after reject", (got["result"], got["revision"]), ("duplicate", 0))
            check("R5b approve NEW key after reject", try_(pp.approve, p5, i5, NEW, now=now + 120, revision=0),
                  "REFUSED packet_not_open")
            got = pp.propose(p5, packet(), now=now + 180, only_new=True)
            check("R5c only_new propose (propose-document path) reopens a rejected id", (got["result"], got["revision"]),
                  ("proposed", 1))

            p6 = fresh(base, "r6")
            MODE["m"] = "ok"
            i6 = pp.propose(p6, packet(), now=now)["id"]
            pp.enable(p6, OLD, now=now)
            pp.approve(p6, i6, OLD, now=now, revision=0)
            pp.write_change(p6, i6, now=now + 10)
            MODE["m"] = "rotated"
            check("R6a status after rotation", view(p6, i6, now + 60), "written@r0 signed_by=g-old")
            check("R6b write_change while the switch reads OFF (switch is checked first)", try_(pp.write_change, p6, i6, now=now + 60),
                  "REFUSED switch_off")
            pp.enable(p6, NEW, now=now + 70)
            check("R6c write_change with switch on (new key) returns existing", try_(pp.write_change, p6, i6, now=now + 80),
                  lambda g: isinstance(g, dict) and g.get("result") == "existing")
            check("R6d cancel/reject of a written packet", try_(pp.decide, p6, i6, "cancelled", "console", now=now + 90),
                  "REFUSED packet_not_open")
            out = os.path.join(base, "export-r6")
            got = packet_changes.export_changes(p6, out)
            check("R6e export-changes still exports the old-key written change (no signature check)", got["active"], 1)

            p7 = fresh(base, "r7")
            MODE["m"] = "ok"
            i7 = pp.propose(p7, packet(), now=now)["id"]
            pp.enable(p7, OLD, now=now)
            pp.approve(p7, i7, OLD, now=now, revision=0)
            MODE["m"] = "gone"
            check("R7a status while unavailable", view(p7, i7, now + 60), "approved@r0 signed_by=g-old")
            check("R7b write_change while unavailable", try_(pp.write_change, p7, i7, now=now + 60), "REFUSED switch_off")
            MODE["m"] = "ok"
            check("R7c write_change after the same anchor is back (no new event needed)",
                  try_(pp.write_change, p7, i7, now=now + 120), lambda g: isinstance(g, dict) and g.get("result") == "written")

            p8 = fresh(base, "r8")
            i8 = pp.propose(p8, packet(), now=now)["id"]
            pp.approve(p8, i8, OLD, now=now, revision=0)
            late = now + pp.EXPIRY_SECONDS + 1
            check("E1 view of an approved packet past expiry", view(p8, i8, late), "expired@r0 signed_by=g-old")
            check("E2 cancel is still allowed (row status is approved)", try_(pp.decide, p8, i8, "cancelled", "console",
                  now=late, revision=0), {"id": i8, "result": "cancelled"})
            check("E3 re-propose after cancel", pp.propose(p8, packet(), now=late)["revision"], 1)
        self.assertEqual(FAILS, [])
        self.assertEqual(len(CHECKS), 29)
