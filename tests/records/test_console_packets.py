"""Synthetic packet decisions over the real loopback server and packet journal."""
import html
import json
import threading
import time
from pathlib import Path
from unittest import mock

from campaign_tool.records import admin_panel as ap, public_packets as pp
from tests.records import test_console_manual_events as http_fixture
from tests.records.test_public_packets import Base, doc


class Packets(Base):
    request = http_fixture.ManualEvents.request
    close_server = http_fixture.ManualEvents.close_server

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.ident = pp.propose(self.project, doc(agency_key='alpha-pd'), now=self.now)['id']
        self.journal = Path(self.project) / pp.JOURNAL
        self.server = ap.build_server(self.project, 'run_1', actor='Operator (one)', system=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.origin = 'http://127.0.0.1:%d' % self.server.server_port
        self.fields = {'csrf_token': self.server.csrf_token, 'id': self.ident, 'revision': '0',
                       'signature': self.sign(pp.payload(self.project, self.ident))}

    def test_page_approval_audit_and_duplicate_never_write_changes(self):
        status, page, _ = self.request('GET', '/packets')
        self.assertEqual(status, 200)
        for value in ('r1', 'doc-1', 'age_hours', pp.pending(self.project)[0]['expires_at'],
                      html.escape(pp.payload(self.project, self.ident).decode()), 'OFF'):
            self.assertIn(value, page)
        with mock.patch.object(pp, 'write_change', side_effect=AssertionError('console must not write')):
            status, _, headers = self.request('POST', '/packets/approve', self.fields)
        self.assertEqual((status, headers['Location']), (303, '/packets'))
        event = json.loads(self.journal.read_text().splitlines()[-1])
        self.assertEqual((event['event'], event['principal'], event['revision']), ('approved', 'g@x', 0))
        self.assertEqual(event['signature'], self.fields['signature'])
        raw = self.journal.read_bytes()
        self.assertEqual(self.request('POST', '/packets/approve', self.fields)[0], 409)
        self.assertEqual(self.journal.read_bytes(), raw)
        self.assertEqual(pp.status(self.project)['packets'][0]['status'], 'approved')
        self.assertFalse((Path(self.project) / pp.OUT).exists())

    def test_refused_signatures_stale_cancelled_expired_leave_journal_unchanged(self):
        raw = self.journal.read_bytes()
        self.assertEqual(self.request('POST', '/packets/approve', self.fields | {'signature': self.sign(b'other')})[0], 403)
        self.assertEqual(self.journal.read_bytes(), raw)
        pp.propose(self.project, doc(agency_key='alpha-pd', text='Changed policy released.'), now=self.now)
        raw = self.journal.read_bytes()
        self.assertEqual(self.request('POST', '/packets/approve', self.fields)[0], 409)
        self.assertEqual(self.request('POST', '/packets/reject', self.reject_fields())[0], 409)
        self.assertEqual(self.journal.read_bytes(), raw)
        current = self.fields | {'revision': '1', 'signature': self.sign(pp.payload(self.project, self.ident))}
        with mock.patch.object(pp.time, 'time', return_value=self.now + pp.EXPIRY_SECONDS + 1):
            self.assertEqual(self.request('POST', '/packets/approve', current)[0], 409)
        self.assertEqual(self.journal.read_bytes(), raw)
        pp.decide(self.project, self.ident, 'cancelled', 'operator')
        raw = self.journal.read_bytes()
        self.assertEqual(self.request('POST', '/packets/approve', current)[0], 409)
        self.assertEqual(self.journal.read_bytes(), raw)

    def reject_fields(self):
        return {k: v for k, v in self.fields.items() if k != 'signature'} | {'reason': 'Needs source review'}

    def test_reject_reason_and_duplicate(self):
        fields = self.reject_fields()
        self.assertEqual(self.request('POST', '/packets/reject', fields | {'reason': ''})[0], 400)
        self.assertEqual(self.request('POST', '/packets/reject', fields)[0], 303)
        event = json.loads(self.journal.read_text().splitlines()[-1])
        self.assertEqual((event['event'], event['actor'], event['reason']),
                         ('rejected', 'Operator -one-', 'Needs source review'))
        raw = self.journal.read_bytes()
        self.assertEqual(self.request('POST', '/packets/reject', fields)[0], 409)
        self.assertEqual(self.journal.read_bytes(), raw)

    def test_guards_and_cancel_navigation_do_not_append(self):
        raw = self.journal.read_bytes()
        for route, fields in (('/packets/approve', self.fields), ('/packets/reject', self.reject_fields())):
            for changes, headers, expected in (({'csrf_token': 'bad'}, {}, 403),
                                                ({}, {'Origin': 'http://evil.test'}, 403),
                                                ({}, {'Host': 'evil.test'}, 403),
                                                ({'revision': 'x'}, {}, 400), ({'extra': 'x'}, {}, 400)):
                self.assertEqual(self.request('POST', route, fields | changes, headers)[0], expected)
        self.assertEqual(self.request('GET', '/packets', headers={'Host': 'evil.test'})[0], 403)
        self.request('GET', '/')  # navigating away cancels without a decision
        self.assertEqual(self.journal.read_bytes(), raw)

    def test_screen_f_and_home_use_packets_source_and_link(self):
        for path in ('/public-updates', '/'):
            status, page, _ = self.request('GET', path)
            self.assertEqual(status, 200)
            self.assertIn(self.ident[:12] if path == '/public-updates' else 'document r1', page)
            self.assertIn('href="/packets"', page)

    def test_revision_checked_inside_decision_lock(self):
        # A new revision appearing after the form was rendered cannot be rejected or approved by that form.
        pp.propose(self.project, doc(agency_key='alpha-pd', text='Revised policy.'), now=self.now)
        for fn, args in ((pp.approve, (self.fields['signature'],)), (pp.decide, ('rejected', 'operator'))):
            self.assertEqual(self.code(fn, self.project, self.ident, *args, revision=0), 'packet_stale')
