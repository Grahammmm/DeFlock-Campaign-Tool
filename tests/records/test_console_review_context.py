"""Synthetic prepared review context, sealed-export isolation and stable navigation."""
import json
import re
from html import unescape
from unittest import mock

from campaign_tool.records import approval_queue as aq, console_export as ce, console_review
from campaign_tool.records import console_snapshot as cs, draft_import, mail_preview, mail_drafts
from tests.records.test_console_snapshot import Panel
from tests.records.test_console_export import tree_state
from tests.records.test_console_model import RUN
from tests.records.test_draft_import import document


def section(page):
    return re.search(r'<section class="review-context">.*?</section>', page, re.S)[0]


class ReviewContextTests(Panel):
    def imported(self, *, target=False):
        doc = document()
        doc['source_run_id'] = RUN
        doc['body'] = 'Long body\n' + 'x' * 65000 + '\nTAIL-Ω <script>body</script>'
        doc['citations'].append({'source_type': 'tracker', 'source_id': 'tracker-row-1',
                                 'locator': '<script>locator</script>', 'as_of': '2026-11-15',
                                 'verification': 'unverified', 'limits': 'Original not obtained'})
        if target:
            doc['kind'] = 'draft-agency-summary'
            doc.pop('envelope')
            doc['citations'] = [{k: c[k] for k in ('source_type', 'source_id', 'locator', 'as_of', 'verification')}
                                for c in doc['citations']]
            doc['target'] = {'website': 'https://example.test', 'page': 'agency', 'section': 'ALPR'}
        return aq.add_draft(self.live, **draft_import.validate(doc))

    def test_live_sealed_parity_no_mail_or_config_reads_and_full_tail(self):
        row = self.imported()
        live = cs.LiveSource(self.live).review_context(row['id'])
        name = self.make_export()
        pin = cs.Pinner(str(self.root)).pin()
        source = cs.SnapshotSource(pin)
        self.assertEqual(source.review_context(row['id']), live)
        doc = json.loads((self.root / 'exports' / name / 'console-model/queue.json').read_text())
        self.assertEqual(doc['rows'], aq.load_queue(self.live))
        self.assertEqual(set(doc['review_context']), {r['id'] for r in doc['rows']})
        for row0 in doc['rows']:
            self.assertEqual(doc['history'][row0['id']], aq.history(self.live, row0['id']))
        mail = self.live / 'mail'
        mail.mkdir(exist_ok=True)
        mail.chmod(0)
        self.addCleanup(lambda: mail.chmod(0o700) if mail.exists() else None)
        before = tree_state(self.live)
        self.up()
        with mock.patch.object(mail_preview, 'preview', side_effect=AssertionError('live preview forbidden')), \
                mock.patch.object(mail_drafts, 'load_config', side_effect=AssertionError('config forbidden')), \
                mock.patch.object(aq, '_project_id', side_effect=AssertionError('host project identity forbidden')):
            page = self.get('/review?id=' + row['id'])
            home = self.get('/')
            dashboard = self.get('/dashboard')
        self.assertEqual(section(page), mail_preview.context_html(live))
        for rendered in (page, home, dashboard):
            self.assertIn('custodian@agency.example', rendered)
            self.assertIn('source_type: original', rendered)
            self.assertIn('source_type: tracker', rendered)
            self.assertIn('Original not obtained', rendered)
            self.assertIn('&lt;script&gt;locator&lt;/script&gt;', rendered)
            self.assertNotIn('<script>locator', rendered)
        citation_panel = section(page)
        self.assertNotIn('approval-payload', citation_panel)
        self.assertIn('TAIL-Ω &lt;script&gt;body&lt;/script&gt;', page)
        shown = unescape(re.search(r'<textarea id="body"[^>]*>\n(.*?)</textarea>', page, re.S)[1])
        self.assertEqual(shown, row['body'])
        self.assertEqual(tree_state(self.live), before)
        mail.chmod(0o700)

    def test_target_and_provenance_visible_without_mail_recipient(self):
        row = self.imported(target=True)
        context = cs.LiveSource(self.live).review_context(row['id'])
        self.assertFalse(context['send_ready'])
        self.make_export()
        self.up()
        for path in ('/review?id=' + row['id'], '/', '/dashboard'):
            page = self.get(path)
            self.assertIn('Target website', page)
            self.assertIn('https://example.test', page)
            self.assertIn('source_type: tracker', page)
        self.assertIsNone(context['recipient'])

    def test_unknown_recipient_does_not_guess_from_body_or_tracker(self):
        row = next(r for r in aq.load_queue(self.live) if r['id'] == self.pending_id)
        context = cs.LiveSource(self.live).review_context(row['id'])
        self.assertIsNone(context['recipient'])
        self.assertFalse(context['send_ready'])
        self.assertIsNone(context['citations'])
        self.make_export()
        self.up()
        page = self.get('/review?id=' + row['id'])
        self.assertIn('Not send-ready', page)
        self.assertIn('Sources unknown', page)

    def test_old_missing_and_stale_context_ask_refresh(self):
        row = self.imported()
        context = cs.LiveSource(self.live).review_context(row['id'])
        original_root = self.root
        for mode in ('old', 'missing', 'project', 'revision', 'evidence', 'queue'):
            self.root = original_root / mode
            self.root.mkdir(mode=0o700)
            def mutate(dest):
                path = dest / 'console-model/queue.json'
                doc = json.loads(path.read_text())
                if mode == 'old':
                    doc.pop('review_context')
                elif mode == 'missing':
                    doc['review_context'].pop(row['id'])
                else:
                    key = {'project': 'source_project_id', 'revision': 'revision',
                           'evidence': 'evidence_sha256', 'queue': 'queue_id'}[mode]
                    doc['review_context'][row['id']][key] = 'stale'
                path.write_text(json.dumps(doc))
            with self.subTest(mode=mode):
                self.make_export(mutate=mutate)
                source = cs.SnapshotSource(cs.Pinner(str(self.root)).pin())
                self.assertIsNone(source.review_context(row['id']))
                self.assertIn(mail_preview.REFRESH, mail_preview.context_html(source.review_context(row['id'])))
        self.assertEqual(context['queue_id'], row['id'])

    def test_navigation_leave_and_reject_preserve_other_rows(self):
        from campaign_tool.records import console_review
        self.imported()
        rows = sorted((r for r in aq.load_queue(self.live) if r['status'] == 'pending'), key=lambda r: r['id'])
        # Add a third distinct pending identity, then leave the middle item without an event.
        third = aq.add_draft(self.live, item_id='f' * 64, kind='reply-extension', agency_id='caltrans',
                             request_id='req-3', body='Third', evidence={'why': 'Separate'}, source_run_id=RUN)
        rows = sorted([*rows, third], key=lambda r: r['id'])
        first, middle, last = rows
        before = (self.live / 'approvals/queue.jsonl').read_bytes()
        links = console_review.navigation(middle, rows)
        self.assertIn('Leave pending', links)
        self.assertIn('/review?id=' + last['id'], links)
        self.assertEqual((self.live / 'approvals/queue.jsonl').read_bytes(), before)
        aq.reject(self.live, middle['id'], 'operator', 'Deferred by human')
        after = {r['id']: r for r in aq.load_queue(self.live)}
        self.assertEqual(after[first['id']], first)
        self.assertEqual(after[last['id']], last)
        self.assertIn('/review?id=' + last['id'], console_review.navigation(after[middle['id']], after.values()))
        self.assertIn('/review?id=' + first['id'], console_review.navigation(last, after.values()))
        self.make_export()
        self.up()
        home = self.get('/')
        self.assertIn('/review?id=' + first['id'], home)
        self.assertIn('/review?id=' + last['id'], home)
        self.assertNotIn('/review?id=' + middle['id'], home)
        page = self.get('/review?id=' + middle['id'])
        self.assertIn('>Next</a>', page)
        self.assertNotIn('>Leave pending</a>', page)
