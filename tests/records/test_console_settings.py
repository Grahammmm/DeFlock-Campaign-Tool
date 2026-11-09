"""Synthetic Settings acceptance: no live files, transport or decisions."""
import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from unittest import mock

from campaign_tool.records import console_settings as cs, public_updates as pu, public_packets as pp
from tests.records.test_admin_panel import Base, Http, RUN, ap
from tests.approval_signing import Signer


class SettingsTests(Base):
    def snapshot(self):
        return {str(p.relative_to(self.project)): p.read_bytes()
                for p in self.project.rglob('*') if p.is_file()}

    def render(self, config=None, drafts=None):
        before = self.snapshot()
        html = cs.render(self.project, config, drafts)
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn('<form', html)
        return html

    def config(self, **changes):
        secret = self.write(self.project / 'password-canary', 'SYNTHETIC-CANARY-CONTENT')
        data = dict(enabled=False, live=False, daily_cap=5, test_recipient='owner@example.invalid',
                    smtp_host='smtp.example.invalid', smtp_user='fixture', smtp_password_file=str(secret),
                    imap_config=str(self.project / 'not-read'), outbox_path=str(self.project / 'outbox.sqlite'))
        data.update(changes)
        return self.write(self.project / 'approvals' / 'send.json', data)

    def test_missing_and_invalid_send_config(self):
        html = self.render()
        for text in ('not given to the console', '<dd>no</dd>', 'Switch: OFF',
                     'records public-updates off', 'records public-packets enable', 'followup-settings.json'):
            self.assertIn(text, html)
        html = self.render(self.project / 'missing.json')
        self.assertIn('sending unavailable', html)
        self.assertIn('unknown (config unavailable)', html)
        self.assertNotIn(str(self.project), html)

    def test_sending_stops_modes_and_no_password_disclosure(self):
        custom = self.write(self.project / '<stop>', '')
        config = self.config(kill_file=str(custom))
        self.write(self.project / 'approvals' / 'SEND-STOP', '')
        self.write(self.project / 'modes' / 'RECORDS-SEND-STOP', '')
        html = self.render(config, self.project / 'not-read-mailbox')
        for text in ('<dt>Enabled</dt><dd>no</dd>', 'approvals/SEND-STOP', 'modes/RECORDS-SEND-STOP',
                     '&lt;stop&gt;', '<dt>Mode</dt><dd>test</dd>', 'owner@example.invalid',
                     'SMTP password file</dt><dd>configured', '<dt>Configured</dt><dd>yes</dd>'):
            self.assertIn(text, html)
        for text in ('SYNTHETIC-CANARY-CONTENT', 'password-canary', str(self.project), '<stop>'):
            self.assertNotIn(text, html)
        config = self.config(enabled=True, live=True)
        self.assertIn('<dt>Mode</dt><dd>live</dd>', self.render(config))
        self.assertIn('<dt>Enabled</dt><dd>yes</dd>', self.render(config))

    def test_cap_today_count_and_unavailable_outbox(self):
        config = self.config(enabled=True, live=True, daily_cap=1)
        with closing(sqlite3.connect(self.project / 'outbox.sqlite')) as con:
            con.execute('CREATE TABLE outbox(kind,state,draft_json,sent_at,sending_at)')
            con.executemany('INSERT INTO outbox VALUES(?,?,?,?,?)', [
                ('send_followup', 'sent', '{"intent_id":"recq-a"}', '2026-11-06T12:00:00Z', None),
                ('send_followup', 'sent', '{"intent_id":"recq-b"}', '2026-11-05T12:00:00Z', None)])
            con.commit()
        with mock.patch.object(cs.mail_send, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime(2026, 11, 6, 20, tzinfo=timezone.utc)
            html = self.render(config)
        self.assertIn('<dt>Daily cap</dt><dd>1</dd>', html)
        self.assertIn('includes sending)</dt><dd>1</dd>', html)
        self.assertIn('recipient gates)</dt><dd>no</dd>', html)
        self.write(self.project / 'outbox.sqlite', 'invalid')
        self.assertIn('includes sending)</dt><dd>unknown</dd>', self.render(config))

    def seed_outbox(self, *stamps):
        with closing(sqlite3.connect(self.project / 'outbox.sqlite')) as con:
            con.execute('CREATE TABLE outbox(kind,state,draft_json,sent_at,sending_at)')
            con.executemany('INSERT INTO outbox VALUES(?,?,?,?,?)', [
                ('send_followup', 'sent', '{"intent_id":"recq-%d"}' % i, stamp, None) for i, stamp in enumerate(stamps)])
            con.commit()

    def test_settings_budget_zone_matches_count(self):
        config = self.config(enabled=True, live=True, daily_cap=5)
        self.seed_outbox('2026-11-04T23:00:00Z')  # Nov 4 15:00 Pacific, Nov 4 23:00 UTC
        counts = {}
        for zone, label in ((None, 'America/Los_Angeles'), ('UTC', 'UTC')):
            if zone:
                self.write(self.project / 'campaign.json', dict(schema_version=1, timezone=zone))
            with mock.patch.object(cs.mail_send, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = datetime(2026, 11, 5, 1, tzinfo=timezone.utc)
                html = self.render(config)
            self.assertIn('Sent today (%s; includes sending)</dt><dd>' % label, html)
            counts[label] = html.split('includes sending)</dt><dd>')[1].split('<', 1)[0]
        self.assertEqual(counts, {'America/Los_Angeles': '1', 'UTC': '0'})
        self.write(self.project / 'campaign.json', dict(schema_version=1, timezone='No/Such_Zone'))
        html = self.render(config)
        self.assertIn('Sent today (unknown zone; includes sending)</dt><dd>unknown</dd>', html)
        self.assertIn('<dt>Can send now (subject to approval and recipient gates)</dt><dd>no</dd>', html)
        self.assertIn('<dt>Daily cap</dt><dd>unknown</dd>', html)
        for text in ('America/Los_Angeles', 'No/Such_Zone', 'SYNTHETIC-CANARY-CONTENT', str(self.project)):
            self.assertNotIn(text, html)

    def test_reminder_overrides_and_invalid_duplicate_settings(self):
        path = self.project / 'followup-settings.json'
        self.assertIn('<td>off</td><td>0</td><td>10</td>', self.render())
        self.write(path, dict(schema=cs.followup_clock.SETTINGS_SCHEMA,
                             default=dict(notice_after_days=12),
                             agencies={'alpha-pd': dict(autonomy='draft', silence_after_days=3)}))
        self.assertIn('<td>alpha-pd</td><td>draft</td><td>3</td><td>12</td>', self.render())
        for raw in ('invalid', '{"schema":"records-followup-settings-v1","default":{},"default":{}}'):
            self.write(path, raw)
            self.assertIn('settings file invalid: all reminders off', self.render())

    def test_public_switches_signed_on_stale_and_killed_are_read_only(self):
        signer = Signer()
        self.addCleanup(signer.close)
        with signer.trust():
            for module in (pu, pp):
                module.init(self.project)
                module.enable(self.project, signer.sign(module.switch_payload(self.project)))
            self.assertEqual(self.render().count('Switch: ON'), 2)
            for module in (pu, pp):
                self.write(self.project / module.KILL, '')
            self.assertEqual(self.render().count('Switch: KILLED'), 2)
            for module in (pu, pp):
                (self.project / module.KILL).unlink()
                self.write(self.project / module.JOURNAL, 'invalid\n')
            self.assertEqual(self.render().count('not confirmed ON or OFF'), 2)
        for module in (pu, pp):
            (self.project / module.LOCK).unlink()
        self.assertEqual(self.render().count('not confirmed ON or OFF'), 2)

    def test_http_get_only_query_rejected_no_mutation(self):
        server = ap.build_server(self.project, RUN, actor='operator-1', system=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            before = self.snapshot()
            client = Http(server.server_address[1])
            status, _, html = client.send('GET', '/settings')
            self.assertEqual(status, 200)
            self.assertIn('aria-current="page">Settings', html)
            self.assertNotIn('<form', html)
            self.assertEqual(client.send('GET', '/settings?x=1')[0], 404)
            self.assertEqual(client.send('POST', '/settings')[0], 404)
            self.assertEqual(self.snapshot(), before)
        finally:
            server.shutdown()
            thread.join(5)
            server.server_close()
