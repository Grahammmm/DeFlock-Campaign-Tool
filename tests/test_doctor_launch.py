"""Synthetic acceptance for offline launch readiness; never use campaign records."""
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool.cli import doctor_report
from campaign_tool.launch_check import INIT, launch_checks
from campaign_tool.site import build_site

REPO = Path(__file__).resolve().parents[1]
ORDER = ['base_url', 'contact_email', 'signup', 'published_findings', 'evidence',
         'build_check', 'links', 'verified_contacts', 'law_package']
TODAY = date(2026, 11, 1)


class DoctorLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='synthetic-launch-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'campaign'
        self.cli('init', '--name', 'Synthetic Campaign', '--county', 'Synthetic County', '--state', 'CA')
        source = self.base / 'synthetic.txt'
        source.write_text('Synthetic record only.\n')
        self.digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.cli('ingest', '--file', str(source), '--source-id', 'synthetic-delivery')
        self.site = dict(schema_version=1, tagline='Synthetic campaign', about_md='Synthetic information.',
                         base_url='https://campaign.example.org', contact_email='organizer@example.org')
        self.write('content/site.json', self.site)
        self.write('content/agencies.json', [])
        self.finding = dict(id='synthetic-1', title='Synthetic finding', slug='synthetic-finding',
                            summary='A synthetic record was produced.', classification='documented_fact',
                            confidence='verified', event_date='2025-01-01',
                            sources=[dict(sha256=self.digest, locator='page:1')],
                            published_at='2025-01-02T00:00:00Z', corrections=[], state='published',
                            author='synthetic-author', limitations=['Synthetic.'], counterevidence=[])
        self.write('content/findings/synthetic.json', self.finding)
        self.write('content/sources.json', [dict(sha256=self.digest, title='Synthetic record',
                   agency_id='ca-synthetic-sheriff', received_at='2025-01-01T00:00:00Z',
                   public_url=None, fragment='synthetic-record')])
        self.agencies = [dict(agency_id='ca-synthetic-sheriff', selected=True, verified=True,
                             verification=dict(checked_on=TODAY.isoformat()))]
        self.write('kit/agencies.json', self.agencies)

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def cli(self, command, *args):
        result = subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', command,
                                 '--directory', str(self.root), *args], cwd=REPO,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout) if command != 'init' else result.stdout

    def checks(self, **kwargs):
        report = launch_checks(self.root, today=TODAY, **kwargs)
        self.assertEqual([r['check'] for r in report['checks']], ORDER)
        self.assertEqual(report['total'], 9)
        self.assertEqual(report['passed'], sum(r['state'] == 'ok' for r in report['checks']))
        for row in report['checks']:
            self.assertIn(row['state'], ('ok', 'todo', 'fail'))
            self.assertEqual(bool(row['next_action']), row['state'] != 'ok')
        text = json.dumps(report)
        self.assertNotRegex(text, r'[0-9a-fA-F]{64}')
        self.assertNotRegex(text.lower(), r'launched|ready to launch')
        return {r['check']: r for r in report['checks']}

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes() if p.is_file() else None
                for p in self.root.rglob('*')}

    def test_complete_campaign_and_no_public_tree(self):
        before = self.snapshot()
        rows = self.checks()
        self.assertEqual([r['check'] for r in rows.values() if r['state'] == 'todo'], ['signup', 'law_package'])
        self.assertEqual(sum(r['state'] == 'ok' for r in rows.values()), 7)
        self.assertEqual(rows['published_findings']['detail'], '1')
        self.assertFalse((self.root / 'public').exists())
        self.assertEqual(self.snapshot(), before)
        result = self.cli('doctor', '--launch')
        self.assertIn('launch', result)
        for key in ('production_ready', 'public_deployment_checked', 'safe_to_send_automatically'):
            self.assertIs(result[key], False)
        self.assertNotRegex(json.dumps(result).lower(), r'[0-9a-f]{64}|launched|ready to launch')

    def test_absent_content_does_not_write_anything(self):
        shutil.rmtree(self.root / 'content')
        before = self.snapshot()
        rows = self.checks()
        for name in ('base_url', 'contact_email', 'signup', 'published_findings', 'build_check', 'links'):
            self.assertEqual((rows[name]['state'], rows[name]['next_action']), ('todo', INIT))
        self.assertEqual(self.snapshot(), before)

    def test_evidence_failure_paths(self):
        sources = (self.root / 'content/sources.json').read_bytes()
        ledger = self.root / 'private/ledger.sqlite'
        ledger_bytes = ledger.read_bytes()
        for kind in ('unregistered', 'not_ingested', 'object_missing'):
            with self.subTest(kind=kind):
                (self.root / 'content/sources.json').write_bytes(sources)
                ledger.write_bytes(ledger_bytes)
                if kind == 'unregistered':
                    self.write('content/sources.json', [])
                elif kind == 'not_ingested':
                    (self.root / 'private/ledger.sqlite').unlink()
                else:
                    (self.root / 'private/objects' / self.digest).unlink()
                self.assertEqual(self.checks()['evidence']['state'], 'fail')

    def test_duplicate_findings_fail_without_touching_campaign(self):
        self.write('content/findings/duplicate.json', self.finding)
        before = self.snapshot()
        self.assertEqual(self.checks()['build_check']['state'], 'fail')
        self.assertEqual(self.snapshot(), before)

    def test_broken_links_and_scan_hits(self):
        # Main intentionally drops non-https Markdown links; inject a synthetic
        # broken generated link after the real build to exercise the actual scan.
        self.site['about_md'] = '[missing](/missing.html)'
        self.write('content/site.json', self.site)
        self.assertEqual(self.checks()['links']['state'], 'ok')

        def broken_build(root):
            build_site(root)
            with (root / 'public/about.html').open('a') as out:
                out.write('<a href="/missing.html">Synthetic</a>')

        with patch('campaign_tool.launch_check.build_site', side_effect=broken_build):
            row = self.checks()['links']
            self.assertEqual(row['state'], 'fail')
            self.assertGreater(int(row['detail']), 0)
        self.site['about_md'] = '/' + 'workspace/' + 'flockbot/synthetic'
        self.write('content/site.json', self.site)
        row = self.checks()['build_check']
        self.assertEqual(row['state'], 'fail')
        self.assertGreater(int(row['detail']), 0)

    def test_bad_content_doctor_still_exits_zero(self):
        self.site['bad_key'] = True
        self.write('content/site.json', self.site)
        rows = self.checks()
        self.assertEqual(rows['build_check']['state'], 'fail')
        self.assertIn('site.json', rows['build_check']['detail'])
        self.assertIn('bad_key', rows['build_check']['detail'])
        self.assertEqual(self.cli('doctor', '--launch')['launch']['total'], 9)

    def test_existing_public_untouched_and_scratch_removed_even_on_error(self):
        public = self.root / 'public'
        public.mkdir()
        (public / 'sentinel.bin').write_bytes(b'synthetic\x00\xff')
        original = tempfile.mkdtemp
        created = []

        def scratch(*args, **kwargs):
            path = Path(original(*args, **kwargs))
            self.assertNotIn(self.root, path.parents)
            created.append(path)
            return str(path)

        before = self.snapshot()
        with patch('campaign_tool.launch_check.tempfile.mkdtemp', side_effect=scratch):
            self.checks()
            self.site['bad_key'] = True
            self.write('content/site.json', self.site)
            invalid = self.snapshot()
            self.checks()
            self.assertEqual(self.snapshot(), invalid)
        self.assertGreaterEqual(len(created), 2)
        self.assertTrue(all(not p.exists() for p in created))
        self.assertEqual((public / 'sentinel.bin').read_bytes(), before['public/sentinel.bin'])

    def test_contact_failure_stale_boundary_and_no_selected_verified(self):
        path = self.root / 'kit/agencies.json'
        path.write_text('{')
        self.assertEqual(self.checks()['verified_contacts']['state'], 'fail')
        self.agencies[0]['verification']['checked_on'] = (TODAY - timedelta(days=366)).isoformat()
        self.write('kit/agencies.json', self.agencies)
        row = self.checks()['verified_contacts']
        self.assertEqual(row['state'], 'todo')
        self.assertIn('1 stale', row['detail'])
        self.assertEqual(self.checks(contact_max_age_days=366)['verified_contacts']['state'], 'ok')
        self.agencies[0]['selected'] = False
        self.write('kit/agencies.json', self.agencies)
        self.assertEqual(self.checks()['verified_contacts']['state'], 'todo')

    def test_missing_site_fields_signup_and_zero_findings(self):
        self.site.pop('base_url')
        self.site.pop('contact_email')
        self.write('content/site.json', self.site)
        (self.root / 'content/findings/synthetic.json').unlink()
        rows = self.checks()
        for name in ('base_url', 'contact_email', 'signup', 'published_findings'):
            self.assertEqual(rows[name]['state'], 'todo')
        self.site['signup'] = dict(mode='brevo_hosted', form_html_allowlisted_url='https://synthetic.sibforms.com/serve/synthetic')
        self.write('content/site.json', self.site)
        self.assertEqual(self.checks()['signup']['state'], 'ok')

    def test_law_statuses_and_no_flag_compatibility(self):
        self.assertEqual(self.cli('doctor'), doctor_report(self.root))
        self.assertNotIn('launch', self.cli('doctor'))
        for status, state in (('reviewed', 'ok'), ('draft', 'todo'), ('missing', 'fail')):
            with self.subTest(status=status), patch('campaign_tool.cli.law_package_report',
                                                  return_value={'law_package_status': status}):
                self.assertEqual(self.checks()['law_package']['state'], state)


if __name__ == '__main__':
    unittest.main()
