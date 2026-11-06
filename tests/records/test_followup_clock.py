"""No-response follow-ups (REC-AUTO-7): clocks to pending drafts. Synthetic data only; nothing is sent."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from campaign_tool.records import approval_queue as aq
from campaign_tool.records import followup_clock as fc
from campaign_tool.records import run_stages
from campaign_tool.records.ledger.migrations import v001
from campaign_tool.records.outcomes import register
from tests.approval_signing import Signer

AS_OF = "2030-02-01"
BANNED = ("violat", "unlawful", "penalt", "lawsuit", "sue")


def row(agency="ag1", request="req-1", state="past_due_reminder", due="2030-01-12", promised=None,
        status="open", issues=(), extension=False, name="Fable Bay Police"):
    return {"agency": name, "agency_key": agency, "request_id": request, "status": status,
            "asked": "Camera audit logs for 2029", "request_date": "2030-01-01",
            "deadline": {"state": state, "due": due, "basis": "recorded_receipt", "extension_recorded": extension},
            "promised": promised or {"state": "none", "date": None}, "issues": list(issues),
            "sources": [{"table": "requests", "id": "r1"}]}


def doc(rows, as_of=AS_OF):
    return {"schema": register.SCHEMA, "as_of": as_of, "requests": rows, "limits": register.LIMITS}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "project"
        self.root.mkdir(mode=0o700)

    def put_register(self, rows, run="run-1", as_of=AS_OF):
        directory = self.root / "run-outputs" / run
        directory.mkdir(parents=True, exist_ok=True)
        (directory / fc.REGISTER_NAME).write_text(json.dumps(doc(rows, as_of)), encoding="utf-8")

    def settings(self, body, mode=0o600):
        path = self.root / fc.SETTINGS_FILE
        path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
        os.chmod(path, mode)
        return path

    def enable(self, agency="ag1", **extra):
        self.settings({"schema": fc.SETTINGS_SCHEMA, "agencies": {agency: {"autonomy": "draft", **extra}}})

    def queued(self):
        return aq.load_queue(self.root)


class SettingsAndStates(Base):
    def test_off_by_default(self):
        self.put_register([row(), row("ag2", "req-2")])
        self.assertEqual(fc.queue_followups(self.root, "run-1")["skipped_off"], 2)
        self.assertEqual(self.queued(), [])
        self.assertFalse((self.root / "approvals").exists())
        self.enable("ag2")
        out = fc.queue_followups(self.root, "run-1")
        self.assertEqual((out["created"], out["skipped_off"]), (1, 1))
        self.assertEqual([r["agency_id"] for r in self.queued()], ["ag2"])

    def test_autonomy_send_refused(self):
        self.put_register([row()])
        for bad in ("send", "auto", "on", True, 1, None, ["draft"]):
            with self.subTest(value=bad):
                self.settings({"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": "off"},
                               "agencies": {"ag1": {"autonomy": bad}}})
                out = fc.queue_followups(self.root, "run-1")
                self.assertTrue(out["settings_invalid"])
                self.assertEqual(out["created"], 0)
        for text in ('{"schema":"records-followup-settings-v1","agencies":{"ag1":{"autonomy":"draft","send":true}}}',
                     '{"schema":"records-followup-settings-v1","agencies":{"ag1":{"autonomy":"draft","autonomy":"draft"}}}',
                     '{"schema":"records-followup-settings-v1","agencies":{"ag1":{"autonomy":"draft","notice_after_days":0}}}',
                     '{"schema":"records-followup-settings-v1","agencies":{"ag1":{"autonomy":"draft","notice_after_days":true}}}',
                     '{"schema":"other","agencies":{}}', "not json"):
            with self.subTest(text=text[:60]):
                self.settings(text)
                self.assertTrue(fc.queue_followups(self.root, "run-1")["settings_invalid"])
        self.assertEqual(self.queued(), [])

    def test_unsafe_settings_files_refused(self):
        self.put_register([row()])
        good = json.dumps({"schema": fc.SETTINGS_SCHEMA, "agencies": {"ag1": {"autonomy": "draft"}}})
        self.settings(good, mode=0o640)
        self.assertTrue(fc.queue_followups(self.root, "run-1")["settings_invalid"])
        self.settings(good + " " * fc.MAX_SETTINGS)
        self.assertTrue(fc.queue_followups(self.root, "run-1")["settings_invalid"])
        target = self.root / "real.json"
        target.write_text(good)
        os.chmod(target, 0o600)
        (self.root / fc.SETTINGS_FILE).unlink()
        (self.root / fc.SETTINGS_FILE).symlink_to(target)
        self.assertTrue(fc.queue_followups(self.root, "run-1")["settings_invalid"])
        self.assertEqual(self.queued(), [])

    def test_statutory_states(self):
        self.enable()
        self.put_register([row(request="past"), row(request="today", state="due_today"),
                           row(request="run", state="running"), row(request="ns", state="not_started"),
                           row(request="unk", state="unknown", due=None), row(request="det", state="determined"),
                           row(request="iss", issues=["invalid_date:submitted_at"]),
                           row(request="closed", status="Closed")])
        out = fc.queue_followups(self.root, "run-1")
        self.assertEqual((out["created"], out["skipped_issue"], out["not_due"]), (1, 1, 6))
        self.assertEqual([(r["request_id"], r["kind"], r["status"]) for r in self.queued()],
                         [("past", "reminder-no-response", "pending")])

    def test_promised_date(self):
        self.enable()
        past = {"state": "passed", "date": "2030-01-20"}
        self.put_register([
            row(request="p", state="running", promised=past),
            row(request="today", state="running", promised={"state": "due_today", "date": AS_OF}),
            row(request="soon", state="running", promised={"state": "upcoming", "date": "2030-03-01"}),
            row(request="bad", state="running", promised={"state": "invalid", "date": None},
                issues=["invalid_date:promised_at"]),
            row(request="done", state="running", promised={"state": "none", "date": "2030-01-20"})])
        out = fc.queue_followups(self.root, "run-1")
        self.assertEqual(out["created"], 1)
        draft = self.queued()[0]
        self.assertEqual((draft["request_id"], draft["kind"]), ("p", "reminder-promised-date"))
        self.assertIn("the date the agency gave: 2030-01-20", draft["body"])

    def test_missing_register_never_reads_older_run_or_queue(self):
        self.enable()
        self.put_register([row()], run="run-old")
        out = fc.queue_followups(self.root, "run-new")
        self.assertEqual((out["created"], out["existing"]), (0, 0))
        self.assertFalse((self.root / "approvals").exists())
        with self.assertRaises(ValueError):
            fc.queue_followups(self.root, "../run-old")


class Queueing(Base):
    def test_rerun_idempotent_and_new_date(self):
        self.enable()
        self.put_register([row()])
        self.assertEqual(fc.queue_followups(self.root, "run-1")["created"], 1)
        self.put_register([row()], run="run-2")  # a later run: run provenance is not evidence identity
        again = fc.queue_followups(self.root, "run-2")
        self.assertEqual((again["created"], again["existing"]), (0, 1))
        self.assertEqual(len(self.queued()), 1)
        self.put_register([row(due="2030-01-26", extension=True)], run="run-3")  # extension: new due, new item
        self.assertEqual(fc.queue_followups(self.root, "run-3")["created"], 1)
        self.assertEqual(len(self.queued()), 2)

    def test_edited_or_rejected_reminder_never_overwritten(self):
        self.enable()
        self.put_register([row(), row(request="req-2")])
        fc.queue_followups(self.root, "run-1")
        first, second = self.queued()
        aq.edit(self.root, first["id"], "G", "My own wording")
        aq.reject(self.root, second["id"], "G", "not wanted")
        again = fc.queue_followups(self.root, "run-1")
        self.assertEqual((again["created"], again["existing"]), (0, 2))
        rows = {r["id"]: r for r in self.queued()}
        self.assertEqual(rows[first["id"]]["body"], "My own wording")
        self.assertEqual(rows[second["id"]]["status"], "rejected")

    def test_formal_notice_ladder_pure(self):
        ident = fc.item_id("ag1", "req-1", "determination", "2030-01-12")
        settings = fc.load_settings(self.root)
        settings["agencies"] = {"ag1": {"autonomy": "draft", "notice_after_days": 10}}

        def reminder(status, updated="2030-01-22T10:00:00Z"):
            return [{"item_id": ident, "kind": "reminder-no-response", "status": status, "updated_at": updated}]
        kinds = lambda queue, as_of: [c["kind"] for c in fc.plan(doc([row()]), settings, queue, as_of=as_of)]
        self.assertEqual(kinds(reminder("approved"), "2030-01-31"), ["reminder-no-response"])
        self.assertEqual(kinds(reminder("approved"), "2030-02-01"), ["reminder-no-response", fc.NOTICE])
        for status in ("pending", "rejected"):
            self.assertEqual(kinds(reminder(status), "2030-03-01"), ["reminder-no-response"])
        # the clock moved on (extension recorded): the old reminder does not escalate the new clock
        moved = fc.plan(doc([row(due="2030-01-26", extension=True)]), settings, reminder("approved"), as_of="2030-03-01")
        self.assertEqual([c["kind"] for c in moved], ["reminder-no-response"])

    def test_formal_notice_after_signed_approval_once(self):
        self.enable()
        self.put_register([row()])
        fc.queue_followups(self.root, "run-1")
        ident = self.queued()[0]["id"]
        signer = Signer()
        self.addCleanup(signer.close)
        with signer.trust():
            aq.approve(self.root, ident, {"kind": "ssh", "signature": signer.sign(aq.approval_payload(self.root, ident))})
            self.put_register([row()], run="run-2", as_of="2031-01-01")
            out = fc.queue_followups(self.root, "run-2")
            self.assertEqual((out["created"], out["existing"]), (1, 1))
            twice = fc.queue_followups(self.root, "run-2")
            self.assertEqual((twice["created"], twice["existing"]), (0, 2))
            kinds = sorted(r["kind"] for r in self.queued())
        self.assertEqual(kinds, ["formal-notice-no-response", "reminder-no-response"])
        notice = next(r for r in self.queued() if r["kind"] == fc.NOTICE)
        self.assertEqual(notice["status"], "pending")

    def test_body_contents(self):
        self.enable()
        self.put_register([row(), row(request="req-2", state="running", promised={"state": "passed", "date": "2030-01-20"})])
        fc.queue_followups(self.root, "run-1")
        ident = fc.item_id("ag1", "req-1", "determination", "2030-01-12")
        notice_body = fc_render(self, "formal-notice-no-response")
        bodies = {r["kind"]: r for r in self.queued()}
        statutory, promised = bodies["reminder-no-response"], bodies["reminder-promised-date"]
        self.assertEqual(statutory["evidence"]["source"], "register")
        self.assertEqual(statutory["item_id"], ident)
        for text in (statutory["body"], promised["body"], notice_body):
            self.assertIn("DRAFT — NOT SENT. Review before sending it yourself.", text)
            self.assertNotIn("Provenance", text)
            for word in BANNED:
                self.assertNotIn(word, text.casefold())
        self.assertIn("req-1", statutory["body"])
        self.assertIn("2030-01-12", statutory["body"])
        self.assertIn("dated 2030-01-01", statutory["body"])
        self.assertIn("Camera audit logs for 2029", statutory["body"])
        self.assertIn(register.LIMITS[1], statutory["body"])
        self.assertIn(register.LIMITS[2], statutory["body"])
        self.assertIn("the date the agency gave: 2030-01-20", promised["body"])
        self.assertIn("[EDIT ME: date you sent the reminder]", notice_body)
        self.assertIn("in writing", notice_body)


def fc_render(case, kind):
    from campaign_tool.records import draft_templates
    return draft_templates.render_followup(
        kind, agency="Fable Bay Police", request_id="req-1", asked="Camera audit logs for 2029",
        request_date="2030-01-01", clock_kind="determination", clock_date="2030-01-12",
        deadline={"basis": "recorded_receipt", "extension_recorded": False}, limits=register.LIMITS, as_of=AS_OF)


class Seam(Base):
    def setUp(self):
        super().setUp()
        processing = self.root / "processing-v1"
        processing.mkdir()
        self.ledger = processing / "ledger.sqlite"
        with closing(sqlite3.connect(self.ledger)) as con, con:
            con.executescript(v001.SQL)
            con.execute("INSERT INTO agencies(id,name) VALUES('a1','Fable Bay Police')")
            con.execute("INSERT INTO requests(id,agency_id,external_ref,submitted_at,status) "
                        "VALUES('r1','a1','FB-01','2020-01-01','open')")
        (self.root / "portal-queue-v1").mkdir()
        with closing(sqlite3.connect(self.root / "portal-queue-v1" / "portal.sqlite")) as con, con:
            con.execute("CREATE TABLE portal_items(id TEXT)")
        (processing / "runs").mkdir(mode=0o700)
        self.run_no = 0

    def outputs(self, run=None, **patches):
        self.run_no += 1
        run = run or f"seam-{self.run_no}"
        (self.root / "processing-v1" / "runs" / f"extract-{run}.json").write_text(
            json.dumps({"schema": "records-extract-outcomes-v1", "run_id": run, "documents": []}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run_stages.main(["outputs", "--project", str(self.root), "--run-id", run])
        return code, json.loads(out.getvalue()), run

    def test_outputs_seam_queues_pending_and_sends_nothing(self):
        self.enable("a1")
        code, result, run = self.outputs()
        self.assertEqual((code, result["status"]), (0, "ok"))
        draft = self.queued()
        self.assertEqual([(r["kind"], r["status"], r["agency_id"], r["request_id"]) for r in draft],
                         [("reminder-no-response", "pending", "a1", "FB-01")])
        self.assertTrue((self.root / "run-outputs" / run / fc.REGISTER_NAME).is_file())
        for name in ("outbox.sqlite", "EMAIL-APPROVAL.json"):
            self.assertFalse(list(self.root.rglob(name)))
        self.assertFalse(list(self.root.rglob("*receipt*")))
        self.assertEqual(self.outputs()[0], 0)
        self.assertEqual(len(self.queued()), 1)

    def test_register_failure_means_no_followup_call(self):
        self.enable("a1")
        with mock.patch.object(register, "write_for_run", side_effect=OSError("Secret")), \
                mock.patch.object(fc, "queue_followups") as called:
            code, result, _ = self.outputs()
        called.assert_not_called()
        self.assertEqual(result["error_code"], "OUTPUTS_REGISTER_UNAVAILABLE")
        self.assertEqual(self.queued(), [])

    def test_followup_failure_is_isolated_and_timeout_propagates(self):
        self.enable("a1")
        with mock.patch.object(fc, "queue_followups", side_effect=RuntimeError("Secret")):
            code, result, run = self.outputs()
        self.assertEqual((code, result["status"], result["error_code"]), (0, "warn", "OUTPUTS_FOLLOWUP_CLOCK_UNAVAILABLE"))
        self.assertNotIn("Secret", json.dumps(result))
        self.assertTrue((self.root / "run-outputs" / f"{run}.json").is_file())
        self.assertTrue((self.root / "run-outputs" / run / fc.REGISTER_NAME).is_file())
        with mock.patch.object(fc, "queue_followups", side_effect=run_stages.StageTimeout):
            code, result, _ = self.outputs()
        self.assertEqual((code, result["error_code"]), (1, "STAGE_CONTAINER_TIMEOUT"))

    def test_invalid_settings_warn_and_queue_nothing(self):
        self.settings({"schema": fc.SETTINGS_SCHEMA, "agencies": {"a1": {"autonomy": "send"}}})
        code, result, _ = self.outputs()
        self.assertEqual((code, result["error_code"]), (0, "OUTPUTS_FOLLOWUP_SETTINGS_INVALID"))
        self.assertEqual(self.queued(), [])


class Cli(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("sys.argv", ["records followups", *argv]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = fc.main()
        return code, out.getvalue(), err.getvalue()

    def snapshot(self):
        return sorted((str(p.relative_to(self.root)), p.read_bytes() if p.is_file() else b"") for p in self.root.rglob("*"))

    def test_plan_and_rows_are_read_only(self):
        self.enable()
        self.put_register([row(), row(request="soon", state="running", due="2030-02-10"),
                           row(request="prom", state="running", promised={"state": "passed", "date": "2030-01-20"})])
        before = self.snapshot()
        code, out, _ = self.run_cli("plan", "--project", str(self.root), "--run-id", "run-1")
        plan = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual([(c["kind"], c["request_id"], c["queued"]) for c in plan["candidates"]],
                         [("reminder-no-response", "req-1", False), ("reminder-promised-date", "prom", False)])
        code, out, _ = self.run_cli("rows", "--project", str(self.root), "--run-id", "run-1")
        rows = json.loads(out)
        self.assertEqual([(r["request_id"], r["clock_kind"]) for r in rows["overdue"]],
                         [("req-1", "determination"), ("prom", "promised")])
        self.assertEqual([(r["request_id"], r["state"]) for r in rows["deadlines"]], [("prom", "running"), ("soon", "running")])
        self.assertEqual(self.snapshot(), before)
        code, _, err = self.run_cli("plan", "--project", str(self.root), "--run-id", "run-missing")
        self.assertEqual((code, err.strip()), (1, "register_missing"))


if __name__ == "__main__":
    unittest.main()
