"""Synthetic engine -> signed queue -> fake mailbox acceptance. No external IO."""
import contextlib
import csv
import io
import json
from datetime import datetime, timezone
from unittest import mock

from campaign_tool.records import approval_queue as aq, case_builder, console_home
from campaign_tool.records import draft_templates, followup_clock as fc, followup_engine as fe
from campaign_tool.records import mail_drafts as md
from campaign_tool.records.outcomes import timeline
from tests.records.test_case_builder import DAY, ask, items, request
from tests.records.test_mail_drafts import Base, DOC, EML, received_items


class EngineTests(Base):
    def fixture(self, run="r1", closed=False, data=None, received=None):
        out = self.project / "run-outputs" / run / "outcomes"
        out.mkdir(parents=True, exist_ok=True)
        req = request(closed)
        data = data or items(ask(1, "ALPR search logs", "missing"), ask(2, "Vendor agreement", "answered"))
        docs = {"timeline.json": {"schema": timeline.SCHEMA, "run_id": run, "as_of": DAY, "requests": [req]},
                "response-items.json": {"schema": "records-response-items-v1", "run_id": run, "requests": [data]},
                "received-items.json": {"schema": received_items.SCHEMA, "run_id": run,
                                        "items": [self.source()] if received is None else received}}
        for name, doc in docs.items():
            (out / name).write_text(json.dumps(doc))
        (out.parent / "CURRENT-REQUEST-REGISTER.json").write_text(json.dumps(
            {"schema": "records-request-register-v1", "as_of": DAY, "requests": [req]}))
        return out

    def source(self, **kwargs):
        return dict({"agency_id": "alpha-pd", "request_id": "OLD-1", "received_at": DAY + "T12:00:00Z",
                     "item_id": received_items.item_id(DOC)}, **kwargs)

    def settings(self, autonomy="draft"):
        path = self.project / fc.SETTINGS_FILE
        path.write_text(json.dumps({"schema": fc.SETTINGS_SCHEMA, "default": {"autonomy": autonomy}}))
        path.chmod(0o600)

    def snapshot(self):
        # SQLite's existing mode=ro thread reader can create transient WAL sidecars.
        return {str(p): p.read_bytes() for p in self.project.rglob("*")
                if p.is_file() and not p.name.endswith(("-wal", "-shm"))}

    def queued(self):
        return aq.load_queue(self.project)

    def clean_approve(self, row):
        aq.edit(self.project, row["id"], "operator", "Please supply the outstanding ALPR search logs.\n")
        return self.approve(row["id"])

    def test_dry_apply_evidence_home_age_and_residue_gate(self):
        self.build_ledger()
        self.settings()
        self.fixture()
        # Existing queue bytes, not only an absent queue, must be preserved by dry run.
        aq.add_draft(self.project, item_id="unrelated", kind="first-request", agency_id="beta-pd",
                     request_id="B-1", body="Draft", evidence={}, source_run_id="r1")
        before = self.snapshot()
        self.assertEqual(fe.run(self.project, "r1")["would_queue"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)
        row = next(r for r in self.queued() if r["kind"] == fe.KIND)
        self.assertEqual(row["status"], "pending")
        ev = row["evidence"]
        self.assertEqual(set(ev), {"source", "pattern", "extra_patterns", "asks_for", "covered", "state", "flags",
                                   "vehicle", "thread_item_id", "thread_basis", "planned_send_date", "date_basis",
                                   "draft_status", "lint_hits", "unfilled"})
        self.assertEqual((ev["source"], ev["pattern"], ev["vehicle"], ev["thread_item_id"]),
                         ("followup_engine", "partial_production", "reply_in_thread", received_items.item_id(DOC)))
        self.assertEqual(ev["planned_send_date"], DAY)
        self.assertGreater(ev["unfilled"], 0)
        model = {"now": datetime(2027, 1, 1, tzinfo=timezone.utc), "queue": {"rows": [row]},
                 "sends": {"rows": [], "configured": False}, "public": {"lines": []}, "packets": {"rows": []},
                 "timeline": {"doc": {"requests": []}}}
        home = console_home.decision_rows(model, model["now"].date())
        self.assertEqual(home[0]["kind_group"], "follow-up")
        self.assertIsInstance(home[0]["age_hours"], int)
        self.approve(row["id"])
        self.assertEqual(self.place()["failed"], 1)
        self.assertEqual(md.placements(self.project)[0]["code"], "body_has_draft_residue")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(draft_templates.FOLLOWUP_KINDS,
                         ("reminder-no-response", "reminder-promised-date", "formal-notice-no-response"))
        self.assertIn(fe.KIND, timeline.FOLLOW_UP_KINDS)

    def test_reply_threads_to_latest_source_end_to_end(self):
        self.build_ledger(eml_headers="From: clerk@agency.example.test\r\nReply-To: records@agency.example.test\r\n"
                          "Message-ID: <src-1@agency.example.test>\r\nSubject: Records\r\n"
                          "References: <old@agency.example.test>\r\n\r\nbody\r\n")
        self.settings()
        self.fixture(received=[self.source(received_at="2026-11-01T12:00:00Z", item_id="a" * 64), self.source()])
        fe.run(self.project, "r1", apply=True)
        row, = self.queued()
        self.assertEqual(row["evidence"]["thread_item_id"], received_items.item_id(DOC))
        self.clean_approve(row)
        self.assertEqual(self.place()["placed"], 1)
        msg = self.parsed()
        self.assertEqual(msg["To"], "records@agency.example.test")
        self.assertEqual(msg["In-Reply-To"], "<src-1@agency.example.test>")
        self.assertTrue(str(msg["References"]).endswith("<src-1@agency.example.test>"))
        self.assertEqual(msg["Subject"], "Re: Records")
        self.assertTrue(md.placements(self.project)[0]["threaded"])

    def two_messages(self):
        self.build_ledger(eml_headers="From: old@agency.example.test\r\n"
                          "Message-ID: <old@agency.example.test>\r\n\r\nold\r\n")
        digest = "a" * 64
        blob = self.root / "blobs" / digest
        blob.write_text("From: new@agency.example.test\r\nReply-To: records@agency.example.test\r\n"
                        "Message-ID: <new@agency.example.test>\r\nSubject: New records\r\n"
                        "References: <ancestor@agency.example.test>\r\n\r\nnew\r\n")
        with md.store.ledger(self.ledger) as con:
            con.execute("INSERT INTO originals(sha256,bytes,mime_detected,first_seen_at,role,scope,"
                        "preservation_status,provenance_json,storage_path) VALUES(?,?,?,?,?,?,?,?,?)",
                        (digest, blob.stat().st_size, "message/rfc822", DAY, "agency_original", "in_scope",
                         "verified", "{}", str(blob)))
            con.execute("INSERT INTO occurrences VALUES('o3',?,'mail','new',NULL,NULL,'t','{}')", (digest,))
            con.execute("INSERT INTO mail_messages VALUES('msg-2','acct','INBOX',1,2,?,?,?,?,'x')",
                        ("<new@agency.example.test>", digest, "{}", DAY))
            con.commit()
        return [self.source(received_at=DAY + "T12:00:00Z"),
                self.source(received_at=DAY + "T12:00:00.500000Z", item_id=received_items.item_id(digest))]

    def test_latest_source_fractional_second_end_to_end(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                self.tearDown_project()
                sources = self.two_messages()
                self.settings()
                self.fixture(received=sources[::-1] if reverse else sources)
                before = self.snapshot()
                self.assertEqual(fe.run(self.project, "r1")["would_queue"], 1)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)
                row, = self.queued()
                self.assertEqual(row["evidence"]["thread_item_id"], sources[1]["item_id"])
                self.assertTrue(row["evidence"]["thread_basis"].endswith(sources[1]["received_at"]))
                self.clean_approve(row)
                self.assertEqual(self.place()["placed"], 1)
                msg = self.parsed()
                self.assertEqual(msg["To"], "records@agency.example.test")
                self.assertEqual(msg["In-Reply-To"], "<new@agency.example.test>")
                self.assertEqual(msg["References"], "<ancestor@agency.example.test> <new@agency.example.test>")

    def test_equal_instants_are_ambiguous_across_spellings(self):
        self.build_ledger()
        self.settings()
        for spelling in (DAY + "T12:00:00.000000Z", DAY + "T13:00:00+01:00"):
            sources = [self.source(), self.source(received_at=spelling, item_id="a" * 64)]
            for rows in (sources, sources[::-1], [sources[0], sources[0]]):
                with self.subTest(spelling=spelling, rows=rows):
                    self.fixture(received=rows)
                    before = self.snapshot()
                    result = fe.run(self.project, "r1", apply=True)
                    self.assertEqual(result["manual_reasons"], {"ambiguous_source_message": 1})
                    self.assertEqual(result["queued"], 0)
                    self.assertEqual(before, self.snapshot())
        earlier = self.source(received_at=DAY + "T11:00:00Z", item_id="a" * 64)
        self.fixture(received=[earlier, earlier, self.source()])
        self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)

    def test_invalid_matching_receipt_is_manual_not_older_fallback(self):
        self.build_ledger()
        self.settings()
        aq.add_draft(self.project, item_id="unrelated", kind="first-request", agency_id="beta-pd",
                     request_id="B-1", body="Draft", evidence={}, source_run_id="r1")
        invalid = ["", 123, True, [], {}, DAY, DAY + "T12:00:00", "bad", "x" * 65,
                   "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"]
        for value in invalid:
            for reverse in (False, True):
                with self.subTest(value=value, reverse=reverse):
                    rows = [self.source(), self.source(received_at=value, item_id="a" * 64)]
                    self.fixture(received=rows[::-1] if reverse else rows)
                    before = self.snapshot()
                    result = fe.run(self.project, "r1", apply=True)
                    self.assertEqual(result["manual_reasons"], {"source_message_date_invalid": 1})
                    self.assertEqual(result["queued"], 0)
                    self.assertEqual(before, self.snapshot())
        self.fixture(received=[self.source(), self.source(agency_id="beta-pd", received_at="bad"),
                               self.source(request_id="OTHER", received_at=123)])
        self.assertEqual(fe.run(self.project, "r1")["would_queue"], 1)
        for source in ({k: v for k, v in self.source().items() if k != "received_at"},
                       self.source(received_at=None)):
            self.fixture(received=[source])
            before = self.snapshot()
            self.assertEqual(fe.run(self.project, "r1", apply=True)["manual_reasons"], {"no_source_message": 1})
            self.assertEqual(before, self.snapshot())

    def test_instant_selection_is_campaign_local_and_replay_safe(self):
        sources_a = self.two_messages()
        self.settings()
        # Keep an already signed older draft: selection must not rewrite its binding.
        self.fixture(received=sources_a[:1])
        fe.run(self.project, "r1", apply=True)
        old, = self.queued()
        self.clean_approve(old)
        old = self.queued()[0]
        self.fixture(received=sources_a)
        self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)
        self.assertEqual(self.queued()[0], old)
        project_a, ledger_a = self.project, self.ledger
        before_a = self.snapshot()
        self.tearDown_project()
        sources_b = self.two_messages()
        sources_b[0]["received_at"] = DAY + "T13:00:00+00:00"
        self.settings()
        self.fixture(received=sources_b)
        self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)
        row_b, = self.queued()
        self.assertEqual(row_b["evidence"]["thread_item_id"], sources_b[0]["item_id"])
        self.clean_approve(row_b)
        before_b = self.snapshot()
        self.assertEqual(fe.run(self.project, "r1", apply=True)["duplicate"], 1)
        self.assertEqual(before_b, self.snapshot())
        self.project, self.ledger = project_a, ledger_a
        self.assertEqual(fe.run(self.project, "r1", apply=True)["duplicate"], 1)
        self.assertEqual(before_a, self.snapshot())
        self.assertEqual(self.queued()[0], old)
        self.assertEqual(self.queued()[1]["evidence"]["thread_item_id"], sources_a[1]["item_id"])

    def test_new_request_does_not_parse_receipts(self):
        self.settings()
        self.fixture(closed=True, received=[self.source(received_at="bad")])
        with mock.patch.object(fe, "_receipt_instant", side_effect=AssertionError("must not parse")), \
                mock.patch.object(md, "find_thread", side_effect=AssertionError("must not lookup")):
            self.assertEqual(fe.run(self.project, "r1", apply=True)["queued"], 1)
        row, = self.queued()
        self.assertIsNone(row["evidence"]["thread_item_id"])

    def test_new_request_never_looks_up_thread(self):
        self.settings()
        self.fixture(closed=True)
        with mock.patch.object(md, "find_thread", side_effect=AssertionError("must not lookup")):
            fe.run(self.project, "r1", apply=True)
            row, = self.queued()
            self.assertIsNone(row["evidence"]["thread_item_id"])
            self.assertEqual(row["evidence"]["thread_basis"], "new request: no thread; recipient filled by G")
            self.clean_approve(row)
            self.assertEqual(self.place()["placed"], 1)
        msg = self.parsed()
        for header in ("To", "In-Reply-To", "References"):
            self.assertIsNone(msg[header])
        self.assertEqual(msg["Subject"], "Records request OLD-1 follow-up")
        row, = md.placements(self.project)
        self.assertEqual((row["threaded"], row["thread_reason"]), (False, "new_request"))

    def test_unresolved_provenance_never_queues(self):
        for name in ("no_source_message", "ambiguous_source_message", "eml_missing", "message_id_missing", "thread_not_found"):
            with self.subTest(name=name):
                self.tearDown_project()
                self.settings()
                self.build_ledger(store_file=name != "eml_missing", link=name != "thread_not_found",
                                  eml_headers="From: a@b.example.test\r\n\r\n" if name == "message_id_missing" else None)
                sources = [] if name == "no_source_message" else [self.source()]
                if name == "ambiguous_source_message":
                    sources.append(self.source(item_id="a" * 64))
                self.fixture(received=sources)
                out = fe.run(self.project, "r1", apply=True)
                self.assertEqual(out["provenance_unresolved"], 1)
                self.assertEqual(out["manual_reasons"], {name: 1})
                self.assertEqual(out["manual"][0]["reason"], name)
                self.assertEqual(self.queued(), [])

    def test_idempotent_across_runs_rejections_and_changed_state_or_source(self):
        self.build_ledger()
        self.settings()
        self.fixture()
        fe.run(self.project, "r1", apply=True)
        out = self.fixture("r2")
        doc = json.loads((out / "timeline.json").read_text())
        doc["requests"][0]["events"].append({"type": "draft", "date": DAY,
                                              "source": {"draft_id": self.queued()[0]["id"]}})
        (out / "timeline.json").write_text(json.dumps(doc))
        with mock.patch.object(aq, "add_draft", side_effect=AssertionError("duplicate add")):
            self.assertEqual(fe.run(self.project, "r2", apply=True)["duplicate"], 1)
        row, = self.queued()
        self.assertEqual(row["source_run_id"], "r1")
        aq.reject(self.project, row["id"], "operator", "stale letter")
        self.assertEqual(fe.run(self.project, "r2", apply=True)["duplicate"], 1)
        self.fixture("r3", closed=True)
        self.assertEqual(fe.run(self.project, "r3", apply=True)["queued"], 1)
        self.fixture("r4", data=items(ask(1, "ALPR search logs", "missing"), ask(2, "Vendor agreement", "answered"),
                                     ask(3, "Audits", "missing")))
        self.assertEqual(fe.run(self.project, "r4", apply=True)["queued"], 1)
        self.fixture("r5", received=[self.source(item_id=received_items.item_id(EML))])
        self.assertEqual(fe.run(self.project, "r5", apply=True)["queued"], 1)
        self.assertEqual(len(self.queued()), 4)

    def test_autonomy_off_and_other_handlers(self):
        self.fixture(closed=True)
        self.assertEqual(fe.run(self.project, "r1", apply=True)["autonomy_off"], 1)
        self.settings("off")
        self.assertEqual(fe.run(self.project, "r1", apply=True)["autonomy_off"], 1)
        self.settings()
        base = {"agency_key": "alpha-pd", "request_id": "OLD-1", "pattern": "silence", "unreconciled": []}
        routes = [dict(base, handled_by=h, reason=reason, unreconciled=ids) for h, reason, ids in
                  [("followup_clock", "silence", []), ("received_items", "covered", []),
                   ("manual", "coverage_unreconciled", ["q1"]), ("manual", "coverage_table_invalid", [])]]
        with mock.patch.object(fe.followup_route, "route_run", return_value=routes):
            out = fe.run(self.project, "r1", apply=True)
        self.assertEqual(out["by_handled_by"], {"followup_clock": 1, "received_items": 1, "manual": 2})
        self.assertEqual(out["manual_reasons"], {"coverage_unreconciled": 1, "coverage_table_invalid": 1})
        self.assertEqual(out["manual"][0]["unreconciled"], ["q1"])
        self.assertEqual(self.queued(), [])

    def test_prior_pending_or_produced_asks_not_queued(self):
        """Prior-ask suppression must not create an empty request."""
        self.settings()
        self.fixture(closed=True)
        path = self.project / "agency-asks" / "alpha-pd.tsv"
        path.parent.mkdir()
        for state in ("pending", "produced"):
            with self.subTest(state=state), path.open("w", newline="") as stream:
                writer = csv.DictWriter(stream, case_builder.ASK_COLUMNS, delimiter="\t")
                writer.writeheader()
                writer.writerow({"item_text": "ALPR search logs", "state": state})
            self.assertEqual(fe.run(self.project, "r1", apply=True)["no_open_asks"], 1)
        self.assertEqual(self.queued(), [])

    def test_cancelled_or_expired_coverers_do_not_hide_residual_asks(self):
        self.build_ledger()
        self.settings()
        self.fixture(data=items(dict(ask(1, "ALPR search logs", "withheld"), evidence=[{"item_id": "i1"}])))
        for status in ("cancelled", "expired"):
            row = {"id": "old", "agency_id": "alpha-pd", "request_id": "OLD-1", "kind": "appeal-denial",
                   "status": status, "evidence": {"item_id": "i1"}}
            with self.subTest(status=status), mock.patch.object(aq, "load_queue", return_value=[row]):
                self.assertEqual(fe.run(self.project, "r1")["would_queue"], 1)

    def test_stale_artifacts_and_unknown_vehicle_fail_without_writes(self):
        self.settings()
        out = self.fixture(closed=True)
        before = self.snapshot()
        with self.assertRaisesRegex(ValueError, "as_of_stale"):
            fe.run(self.project, "r1", apply=True, as_of="2026-11-07")
        self.assertEqual(before, self.snapshot())
        for filename in ("timeline.json", "response-items.json", "received-items.json"):
            original = (out / filename).read_text()
            doc = json.loads(original)
            doc["run_id"] = "stale"
            (out / filename).write_text(json.dumps(doc))
            with self.subTest(file=filename), self.assertRaises(ValueError):
                fe.run(self.project, "r1", apply=True)
            self.assertEqual(self.queued(), [])
            (out / filename).write_text(original)
        with self.assertRaisesRegex(ValueError, "vehicle_unknown"):
            fe._thread(self.project, {"vehicle": "unknown"}, [])

    def test_cli_summary_no_bodies_default_read_only(self):
        self.settings()
        self.fixture(closed=True)
        before = self.snapshot()
        output = io.StringIO()
        with mock.patch("sys.argv", ["followup-engine", "--project", str(self.project), "--run-id", "r1"]), contextlib.redirect_stdout(output):
            self.assertEqual(fe.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["would_queue"], 1)
        self.assertNotIn("ALPR search logs", output.getvalue())
        self.assertEqual(before, self.snapshot())
