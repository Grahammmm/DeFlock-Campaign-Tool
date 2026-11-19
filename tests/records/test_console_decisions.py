"""The read-only decisions list joins synthetic journal entries to one admitted export."""
import io
import json
import re
from contextlib import redirect_stderr
from datetime import datetime
from unittest import mock

from campaign_tool.records import console_journal as journal, console_pages, console_snapshot
from tests.records.test_console_export import tree_state
from tests.records.test_console_journal import Journal, set_manifest
from tests.records.test_console_model import RUN
from tests.records.test_console_snapshot import RUN2, OpenSpy, Panel

WAITING = "Waiting for the next records run"
CUTOFF = "2026-07-14T06:06:00Z"
APPLIED = {"outcome": "APPLIED"}


def refusal(code):
    return {"outcome": code, "refusal": code}


def rows(page):
    return re.findall(r"<tr><td>(.*?)</tr>", page)


class DecisionHistory(Journal):
    def record(self, action, minute, target=None, *, pid=None, **fields):
        pin = console_snapshot.Pin("", RUN, pid or self.pid, "fixture-export", CUTOFF)
        at = f"2026-07-14T06:{minute:02d}:00Z"
        with mock.patch.object(journal, "datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.fromisoformat(at)
            return journal.append(str(self.j), pin, "actor-sentinel", "/route-sentinel", action,
                                  target if target is not None else self.pending_id, fields.pop("revision", 7),
                                  fields.pop("value", "body-signature-sentinel"), **fields)

    def test_decisions_keeps_recorded_and_applied_separate(self):
        approve = self.record("approve", 0)
        future = self.record("message_link_relink", 7, "d" * 64)
        reject = self.record("reject", 1)
        edit = self.record("edit", 2)
        self.record("packet_approve", 3)
        self.record("finding_reject", 4, "sample-pd:finding", kind=journal.FINDING_KIND, revision="a" * 64,
                    value='{"analysis_root_sha256":"' + "b" * 64 + '","reason":"finding-sentinel"}')
        self.record("off", 5, "switch", pid="0" * 16)
        switch = self.record("off", 6, "switch")  # exactly at the cutoff is not newer
        self.make_export(mutate=set_manifest(exported_at=CUTOFF, applied_journal_ids=[approve, future],
                         apply_outcomes={approve: APPLIED, future: APPLIED, reject: refusal("STALE_REVISION"),
                                         edit: refusal("DEFERRED"), switch: refusal("REFUSED switch_invalid")}))
        self.up()
        before = tree_state(self.tmp)
        spy = OpenSpy(self.tmp)
        with mock.patch.object(journal.os, "open", spy):
            page = self.get("/decisions")
        self.assertEqual(tree_state(self.tmp), before)
        self.assertEqual(spy.writes, [])
        self.assertIn('href="/decisions" aria-current="page"', page)
        self.assertNotIn("<form", page)
        for secret in ("sentinel", "d" * 64, "a" * 64, "b" * 64, *(e["entry_id"] for e in self.lines())):
            self.assertNotIn(secret, page)
        review = "/review?id=" + self.pending_id
        expected = [
            (7, "Relink message", "/board", "Message link", WAITING),
            (6, "Turn off public updates", "/public-updates", "Public-update switch",
             "Refused. The records run did not apply this decision."),
            (4, "Reject finding", "/findings/review", "Finding review", WAITING),
            (3, "Approve packet", "/packets", "Packet", WAITING),
            (2, "Edit draft", review, "Draft", "Deferred. The records run did not apply this decision yet."),
            (1, "Reject draft", review, "Draft", "Refused. The item changed after this decision was recorded."),
            (0, "Approve draft", review, "Draft", "Applied"),
        ]
        self.assertEqual(len(rows(page)), len(expected))
        for row, (minute, action, href, kind, outcome) in zip(rows(page), expected):
            with self.subTest(action=action):
                self.assertIn(f"2026-07-13 23:{minute:02d}:00 PDT", row)
                self.assertIn(f'<td>{action}</td><td><a href="{href}">{kind}</a></td><td>{outcome}</td>', row)

    def test_invalid_outcome_does_not_claim_applied_or_echo_values(self):
        ident = self.record("reject", 0, 'https://example.invalid/unsafe-target')
        future = self.record("unknown-sentinel", 7)
        name = self.make_export(mutate=set_manifest(exported_at=CUTOFF))
        self.up()
        path = self.root / "exports" / name / "EXPORT.json"
        manifest = json.loads(path.read_text())
        del manifest["applied_journal_ids"], manifest["apply_outcomes"]
        path.chmod(0o600)  # synthetic owner rewrite of manifest metadata, not a change to export admission
        cases = [("legacy export", None, None), ("listed but no receipt", [ident], {}),
                 ("receipt but not listed", [], {ident: APPLIED}),
                 ("duplicate applied ID", [ident, ident], {ident: APPLIED}),
                 ("mis-shaped list", ident, {ident: APPLIED}),
                 ("extra field", [ident], {ident: {**APPLIED, "refusal": "raw-sentinel"}}),
                 ("unknown status", [], {ident: refusal("UNKNOWN")}),
                 ("unsafe refusal", [], {ident: refusal("REFUSED <raw-sentinel>")}),
                 ("null receipt", [], {ident: None})]
        for case, applied, outcomes in cases:
            with self.subTest(case=case):
                claims = {} if outcomes is None else {
                    "applied_journal_ids": applied, "apply_outcomes": {**outcomes, future: APPLIED}}
                path.write_text(json.dumps({**manifest, **claims}))
                page = self.get("/decisions")
                newer, older = rows(page)
                self.assertIn(f"<td>Unknown action</td><td>Unknown item</td><td>{WAITING}</td>", newer)
                self.assertIn("<td>Draft</td><td>Outcomes unavailable</td>", older)
                self.assertNotIn("sentinel", page)
                self.assertNotIn("unsafe-target", page)

    def test_request_keeps_its_pin_when_current_swaps(self):
        ident = self.record("edit", 0)
        older = self.make_export(mutate=set_manifest(exported_at=CUTOFF))
        newer = self.make_export(RUN2, set_manifest(exported_at=CUTOFF, applied_journal_ids=[ident],
                                                  apply_outcomes={ident: APPLIED}))
        current = self.root / "current"

        def swap(name):
            scratch = self.root / ".next"
            scratch.symlink_to("exports/" + name)
            scratch.replace(current)

        swap(older)
        self.up()
        serve = console_pages.serve

        def after_pin(handler):
            swap(newer)
            return serve(handler)

        with mock.patch.object(console_pages, "serve", after_pin):
            self.assertIn(WAITING, rows(self.get("/decisions"))[0])
        self.assertIn("<td>Applied</td>", rows(self.get("/decisions"))[0])

    def test_unreadable_journal_is_not_empty_history(self):
        self.make_export()
        self.up()
        path = self.j / journal.FILE
        raw = b"broken entry sentinel\n"
        path.write_bytes(raw)
        with redirect_stderr(io.StringIO()):
            page = self.get("/decisions", 503)
        self.assertIn("journal_unavailable", page)
        self.assertNotIn("sentinel", page)
        self.assertEqual(path.read_bytes(), raw)


class UnconfiguredDecisions(Panel):
    def test_without_decisions_dir_the_page_names_the_missing_configuration(self):
        self.make_export()
        self.up()
        page = self.get("/decisions")
        self.assertIn("Decisions: not configured on this console", page)
        self.assertNotIn("<form", page)
