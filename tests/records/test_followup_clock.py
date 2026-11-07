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
from campaign_tool.records import run_stages, draft_residue, draft_templates
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
        self.assertIn("The date you gave, 2030-01-20, has passed", draft["body"])

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
        for status in ("pending", "rejected", "cancelled", "expired"):
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
            self.assertNotIn("DRAFT — NOT SENT. Review before sending it yourself.", text)
            self.assertNotIn("Provenance", text)
            for word in BANNED:
                self.assertNotIn(word, text.casefold())
        self.assertIn("req-1", statutory["body"])
        self.assertIn("sent on 2030-01-01", statutory["body"])
        for removed in ("Camera audit logs for 2029", register.LIMITS[1], register.LIMITS[2]):
            self.assertNotIn(removed, statutory["body"])
        self.assertIn("The date you gave, 2030-01-20, has passed", promised["body"])
        self.assertIn("[EDIT ME: date of our reminder]", notice_body)
        self.assertIn("in writing", notice_body)



def fc_render(case, kind):
    from campaign_tool.records import draft_templates
    return draft_templates.render_followup(
        kind, agency="Fable Bay Police", request_id="req-1", asked="Camera audit logs for 2029",
        request_date="2030-01-01", clock_kind="determination", clock_date="2030-01-12",
        deadline={"basis": "recorded_receipt", "extension_recorded": False}, limits=register.LIMITS, as_of=AS_OF)


class LetterText(Base):
    signature = "Synthetic Records Project\nrecords@example.test"

    def configure(self, signature):
        self.settings({"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": "draft", "signature": signature}})
        return fc.load_settings(self.root)

    def test_three_letters_signature_and_missing_signature(self):
        requests = [row(), row(request="req-2", state="determined",
                              promised={"state": "passed", "date": "2030-01-20"})]
        settings = self.configure(self.signature)
        prior = {"item_id": fc.item_id("ag1", "req-1", "determination", "2030-01-12"),
                 "kind": fc.REMINDERS[0], "status": "approved", "updated_at": "2030-01-20T10:00:00Z"}
        candidates = fc.plan(doc(requests), settings, [prior], as_of=AS_OF)
        self.assertEqual({c["kind"] for c in candidates}, set(draft_templates.FOLLOWUP_KINDS))
        for candidate in candidates:
            with self.subTest(kind=candidate["kind"]):
                body, evidence = fc._render(candidate, doc(requests), "run-1")
                self.assertEqual(draft_residue.codes(body), [])
                self.assertLessEqual(len(body), 1200)
                self.assertTrue(body.startswith("Hello,\n\nI am writing about public records request "))
                self.assertTrue(body.endswith("Thank you,\n\n" + self.signature + "\n"))
                for banned in ("DRAFT", "# Draft", "To:", "Subject:", "[EDIT ME", "Draft only", "Ladder:",
                               "Gov. Code", "§", "Camera audit logs", "Clock:"):
                    self.assertNotIn(banned, body)
                missing, _ = fc._render(dict(candidate, signature=None), doc(requests), "run-1")
                self.assertEqual(missing.count("[EDIT ME:"), 1)
                self.assertEqual(len(draft_residue.codes(missing)), 1)
                self.assertIn("[EDIT ME: signature]", missing)
                if candidate["kind"] == fc.NOTICE:
                    self.assertIn("This follows our reminder of 2030-01-20.", body)
        fallback = fc_render(self, fc.NOTICE)
        self.assertEqual(fallback.count("[EDIT ME: date of our reminder]"), 1)

    def test_signature_validation_all_off_and_boundary(self):
        self.put_register([row()])
        for bad in ("a\nb\nc\nd", "a\tb", "a\rb", "a\x00b", "a\x7fb", "a\x85b", "x" * 201,
                    "", "   ", None, 1, ["name"]):
            with self.subTest(signature=bad):
                self.assertTrue(self.configure(bad)["invalid"])
                self.assertEqual(fc.queue_followups(self.root, "run-1")["created"], 0)
                self.assertEqual(self.queued(), [])
        for good in ("x" * 200, "Project\nPublic contact\nrecords@example.test"):
            self.assertFalse(self.configure(good)["invalid"])
        self.enable(signature=self.signature)  # only the project default owns the signature
        self.assertTrue(fc.load_settings(self.root)["invalid"])

    def test_golden_evidence_old_body_duplicate_and_signature_change(self):
        settings = self.configure(self.signature)
        self.put_register([row()])
        candidate = fc.plan(doc([row()]), settings, [], as_of=AS_OF)[0]
        body, evidence = fc._render(candidate, doc([row()]), "run-1")
        golden = {"source": "register", "run_id": "run-1", "agency_key": "ag1", "request_id": "req-1",
                  "clock_kind": "determination", "clock_date": "2030-01-12",
                  "register_sources": [{"table": "requests", "id": "r1"}]}
        self.assertEqual(evidence, golden)
        old_body = '# Draft (reminder-no-response): Fable Bay Police / req-1\n\nDRAFT — NOT SENT. Review before sending it yourself.\n\nTarget agency: Fable Bay Police\nBelongs to request: req-1\nState: pending-approval\nTo: [EDIT ME: recipient and contact; not taken from the record]\nSubject: Reminder: no written determination recorded for public records request req-1\n\nHello,\n\nI am writing about public records request req-1, dated 2030-01-01 in our records. The request asked for: Camera audit logs for 2029\n\nClock: the determination reminder date in our register was 2030-01-12, counted from the recorded receipt date.\nGov. Code 7922.535: provisional determination reminder, not a production deadline.\n10 calendar days from recorded receipt (otherwise submission proxy); an explicitly recorded extension adds 14 days. Weekend last days roll forward to Monday (CCP 12a/12b). Receipt, holiday rules and lawful written extension require human verification.\n\nThis is a courtesy reminder. Our register shows no written determination recorded as of 2030-02-01.\n\nPlease send your written determination on this request, or tell us the date by which you expect to send it.\n\nThank you,\n\n[EDIT ME: name]\n\nNotes for the reviewer (remove before sending):\n  Ladder: 1 reminder, 2 formal notice (only after this reminder is approved and the wait has passed), 3 supervisor or records-office head, 4 any appeal or legal route the law package lists for this jurisdiction; check each by hand.\n\n---\nDraft only. Not legal advice. Review required. Dates come from the request register, which marks them provisional; check receipt dates and holidays by hand.\n'
        aq.add_draft(self.root, item_id=candidate["item_id"], kind=candidate["kind"], agency_id="ag1",
                     request_id="req-1", body=old_body, evidence=golden, source_run_id="run-1")
        before = self.queued()
        self.configure("Changed project signature")
        for run in ("run-1", "run-2"):
            self.put_register([row(), row()], run=run)  # duplicate source rows remain idempotent
            result = fc.queue_followups(self.root, run)
            self.assertEqual((result["created"], result["existing"]), (0, 2))
            self.assertEqual(self.queued(), before)
        self.assertNotEqual(body, old_body)


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


class DueClocks(Base):
    run_cli = Cli.run_cli
    snapshot = Cli.snapshot

    def test_silence_validation_and_override(self):
        self.assertEqual(fc.load_settings(self.root)["default"]["silence_after_days"], 0)
        for scope in ("default", "agencies"):
            for bad in (31, -1, "3", True):
                with self.subTest(scope=scope, bad=bad):
                    entry = {"autonomy": "draft", "silence_after_days": bad}
                    self.settings({"schema": fc.SETTINGS_SCHEMA,
                                   scope: entry if scope == "default" else {"ag1": entry}})
                    settings = fc.load_settings(self.root)
                    self.assertTrue(settings["invalid"])
                    self.assertEqual(fc.plan(doc([row()]), settings, [], as_of=AS_OF), [])
        self.settings({"schema": fc.SETTINGS_SCHEMA,
                       "default": {"autonomy": "draft", "silence_after_days": 3},
                       "agencies": {"ag1": {"silence_after_days": 30}, "ag2": {"silence_after_days": 0}}})
        settings = fc.load_settings(self.root)
        self.assertFalse(settings["invalid"])
        self.assertEqual([fc._setting(settings, a)["silence_after_days"] for a in ("ag1", "ag2", "ag3")], [30, 0, 3])

    def test_grace_boundary_and_notice_unchanged(self):
        from datetime import date, timedelta
        self.enable(silence_after_days=5)
        settings = fc.load_settings(self.root)
        for kind in ("determination", "promised"):
            r = row(state="determined" if kind == "promised" else "past_due_reminder",
                    due="2030-01-12", promised={"state": "passed", "date": "2030-01-12"} if kind == "promised" else None)
            expected = [(kind, "2030-01-12", "2030-01-18")]
            self.assertEqual(fc.followup_due(r, fc._setting(settings, "ag1")), expected)
            for offset in range(1, 6):
                day = (date(2030, 1, 12) + timedelta(days=offset)).isoformat()
                self.assertEqual(fc.plan(doc([r]), settings, [], as_of=day), [])
            found = fc.plan(doc([r]), settings, [], as_of="2030-01-18")
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["item_id"], fc.item_id("ag1", "req-1", kind, "2030-01-12"))
            approved = [{**found[0], "status": "approved", "updated_at": "2030-01-18T12:00:00Z"}]
            self.assertNotIn(fc.NOTICE, [c["kind"] for c in fc.plan(doc([r]), settings, approved, as_of="2030-01-27")])
            self.assertIn(fc.NOTICE, [c["kind"] for c in fc.plan(doc([r]), settings, approved, as_of="2030-01-28")])

    def test_first_plan_date_equivalence(self):
        from datetime import date, timedelta
        anchor = date(2030, 1, 12)
        for n in (0, 3):
            self.enable(silence_after_days=n)
            settings = fc.load_settings(self.root)
            for case in ("running", "due_today", "past_due_reminder", "upcoming", "passed", "determined", "unknown", "closed"):
                with self.subTest(n=n, case=case):
                    promised = case in ("upcoming", "passed")
                    base = anchor + timedelta(days={"running": 2, "past_due_reminder": -1, "upcoming": 2, "passed": -1}.get(case, 0))
                    r = row(state="determined" if promised else case if case != "closed" else "running",
                            due=None if case == "unknown" else base.isoformat(),
                            promised={"state": case, "date": base.isoformat()} if promised else None,
                            status="closed" if case == "closed" else "open")
                    expected = fc.followup_due(r, fc._setting(settings, "ag1"))
                    first = None
                    for offset in range(-2, 41):
                        day = anchor + timedelta(days=offset)
                        current = json.loads(json.dumps(r))
                        if promised:
                            current["promised"]["state"] = "passed" if day > base else "due_today" if day == base else "upcoming"
                        elif case not in ("determined", "unknown"):
                            current["deadline"]["state"] = "past_due_reminder" if day > base else "due_today" if day == base else "running"
                        if fc.plan(doc([current], day.isoformat()), settings, [], as_of=day.isoformat()):
                            first = day.isoformat()
                            break
                    self.assertEqual(first, expected[0][2] if expected else None)

    def test_clock_table_golden_and_identity(self):
        self.enable(silence_after_days=3)
        settings = fc.load_settings(self.root)
        rows = [row(request="overdue"), row(request="future", state="running", due="2030-02-08"),
                row(request="closed", status="closed"), row(request="unknown", state="unknown", due=None),
                row(request="promise", state="determined", promised={"state": "upcoming", "date": "2030-02-03"})]
        rows[0]["deadline"].update(base_due="2030-01-12", extended_due="2030-01-26")
        rows[3]["issues"] = ["missing_receipt"]
        timeline = {"requests": [{"agency_key": "ag1", "request_id": "overdue", "extension_chain": {"exceeds_14_days": True}},
                                 {"agency_key": "other", "request_id": "future", "extension_chain": {"exceeds_14_days": True}}]}
        approved = {"item_id": fc.item_id("ag1", "overdue", "determination", "2030-01-12"),
                    "kind": "reminder-no-response", "status": "approved", "updated_at": "2030-01-23T12:00:00Z"}
        inputs = json.dumps([rows, settings, timeline, approved], sort_keys=True)
        result = fc.clock_table(doc(rows), settings, [approved], timeline, as_of=AS_OF)
        self.assertEqual([r["request_id"] for r in result], ["overdue", "promise", "future", "closed", "unknown"])
        self.assertEqual(result[0], {"agency_key": "ag1", "request_id": "overdue", "basis": "recorded_receipt",
                         "base_due": "2030-01-12", "extended_due": "2030-01-26", "due": "2030-01-12",
                         "state": "past_due_reminder", "promised": {"state": "none", "date": None},
                         "followup_due": [{"clock_kind": "determination", "clock_date": "2030-01-12", "due_date": "2030-01-16"}],
                         "notice_due": "2030-02-02", "extension_limit_exceeded": True, "autonomy": "draft", "issues": [], "closed": False})
        self.assertTrue(result[3]["closed"])
        self.assertEqual(result[3]["followup_due"], [])
        self.assertEqual(result[4]["issues"], ["missing_receipt"])
        self.assertIsNone(result[2]["extension_limit_exceeded"])
        self.assertEqual(json.dumps([rows, settings, timeline, approved], sort_keys=True), inputs)
        for status in ("pending", "rejected", "cancelled", "expired"):
            queue = [{**approved, "status": status}]
            self.assertTrue(all(r["notice_due"] is None for r in fc.clock_table(doc(rows), settings, queue, as_of=AS_OF)))
        stale = {**approved, "item_id": fc.item_id("ag1", "overdue", "determination", "2030-01-01")}
        self.assertTrue(all(r["notice_due"] is None for r in fc.clock_table(doc(rows), settings, [stale], as_of=AS_OF)))
        self.assertEqual(fc.clock_table(doc(rows), settings, [approved, approved], timeline, as_of=AS_OF), result)

    def test_clocks_cli_optional_timeline_and_failures(self):
        from campaign_tool.records.outcomes.timeline import SCHEMA
        self.enable(silence_after_days=3)
        self.put_register([row()])
        argv = ("clocks", "--project", str(self.root), "--run-id", "run-1")
        before = self.snapshot()
        code, out, _ = self.run_cli(*argv)
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)[0]["extension_limit_exceeded"])
        self.assertEqual(self.snapshot(), before)
        path = self.root / "run-outputs/run-1/outcomes/timeline.json"
        path.parent.mkdir()
        timeline = {"schema": SCHEMA, "run_id": "run-1", "requests": [
            {"agency_key": "ag1", "request_id": "req-1", "extension_chain": {"exceeds_14_days": True}}]}
        path.write_text(json.dumps(timeline))
        before = self.snapshot()
        code, out, _ = self.run_cli(*argv)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)[0]["extension_limit_exceeded"])
        self.assertEqual(self.snapshot(), before)
        for bad in ("not json", json.dumps({**timeline, "run_id": "stale"}), json.dumps({**timeline, "requests": [None]})):
            path.write_text(bad)
            code, _, err = self.run_cli(*argv)
            self.assertEqual((code, err.strip()), (1, "timeline_unreadable"))
        path.unlink()
        path.symlink_to(self.root / "run-outputs/run-1" / fc.REGISTER_NAME)
        code, _, err = self.run_cli(*argv)
        self.assertEqual((code, err.strip()), (1, "timeline_unreadable"))
        self.assertEqual(self.run_cli("clocks", "--project", str(self.root), "--run-id", "missing")[0], 1)


class ReminderStatus(Base):
    def test_six_states_identity_and_purity(self):
        self.enable()
        settings = fc.load_settings(self.root)
        settings["agencies"].update({"off": {"autonomy": "off"},
                                      "wait": {"autonomy": "draft", "silence_after_days": 30}})
        rows = [row(request=s) for s in ("drafted", "approved", "rejected")]
        rows += [row("off", "off"), row("wait", "not_yet"), row(request="closed", status="closed")]
        queue = [{"id": s, "item_id": fc.item_id("ag1", s, "determination", "2030-01-12"),
                  "kind": fc.REMINDERS[0], "status": q, "updated_at": AS_OF}
                 for s, q in (("drafted", "pending"), ("approved", "approved"), ("rejected", "rejected"))]
        inputs = json.dumps([rows, settings, queue], sort_keys=True)
        with mock.patch("builtins.open", side_effect=AssertionError("IO")), mock.patch.object(os, "open", side_effect=AssertionError("IO")):
            result = fc.reminder_status(doc(rows), settings, queue, as_of=AS_OF)
            self.assertEqual(fc.reminder_status(doc(rows), settings, queue, as_of=AS_OF), result)
        self.assertEqual([r["state"] for r in result], ["drafted", "approved", "rejected", "off", "not_yet", "closed"])
        self.assertEqual(result[4]["due"], "2030-02-12")
        candidates = {c["item_id"] for c in fc.plan(doc(rows), settings, queue, as_of=AS_OF)}
        for r in result:
            ident = fc.item_id(*(r[k] for k in ("agency_key", "request_id", "clock_kind", "clock_date")))
            self.assertEqual(r["state"] in ("drafted", "approved", "rejected"),
                             ident in candidates and ident in {q["item_id"] for q in queue})
        self.assertEqual(json.dumps([rows, settings, queue], sort_keys=True), inputs)

    def test_no_false_ready_cancelled_expired_stale_duplicate_and_unqueued(self):
        self.enable()
        settings = fc.load_settings(self.root)
        request = row(state="determined", promised={"state": "passed", "date": "2030-01-20"})
        q = {"id": "draft", "item_id": fc.item_id("ag1", "req-1", "promised", "2030-01-20"),
             "kind": fc.REMINDERS[1], "status": "pending"}
        status = lambda rows, queue: fc.reminder_status(doc(rows), settings, queue, as_of=AS_OF)
        ready = status([request], [q])
        self.assertEqual(ready[0]["state"], "drafted")
        self.assertEqual(status([request, request], [q, q]), ready)
        for state in ("cancelled", "expired"):
            result = status([request], [q | {"status": state}])[0]
            self.assertIsNone(result["state"])
            self.assertIn(state, result["reason"])
        for stale in (q | {"item_id": fc.item_id("ag1", "req-1", "promised", "2030-01-19")},
                      q | {"kind": fc.NOTICE}):
            self.assertNotIn("queue_id", status([request], [stale])[0])
        self.assertEqual(status([request], [])[0]["reason"], "No reminder draft queued")
        self.assertIn("needs checking", status([request | {"issues": ["bad_date"]}], [q])[0]["reason"])
        self.assertEqual(status([row(state="running", due="2030-03-01")], []), [])
        with self.assertRaisesRegex(ValueError, "as_of_invalid"):
            fc.reminder_status(doc([request]), settings, [], as_of="bad")


if __name__ == "__main__":
    unittest.main()
