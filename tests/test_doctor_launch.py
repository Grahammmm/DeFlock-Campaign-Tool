"""Synthetic acceptance for offline launch readiness; never use campaign records."""
from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from campaign_tool import law
from campaign_tool.cli import doctor_report
from campaign_tool.content import load_content
from campaign_tool.launch_check import INIT, _local_rules_bytes, launch_checks
from campaign_tool.site import build_site
from tests import test_local_law as local

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


def state(root):
    """Every path under ``root`` with its mode and bytes (link target for a symlink); links are never followed."""
    seen = {}
    for folder, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(folder, name)
            info = os.lstat(path)
            body = os.readlink(path) if stat.S_ISLNK(info.st_mode) else \
                Path(path).read_bytes() if stat.S_ISREG(info.st_mode) else None
            seen[os.path.relpath(path, root)] = (info.st_mode, body)
    return seen


class LocalRuleRehearsalTests(local.LocalLawCase):
    """The rehearsal builds from the local rules the campaign's own loader admitted (RF0015-1); synthetic data only."""
    AGENCY_PAGE = 'public/agencies/cedar.html'

    def setUp(self):
        super().setUp()
        self.created, self.staged = [], {}
        real_mkdtemp, real_build = tempfile.mkdtemp, build_site

        def mkdtemp(*args, **kwargs):
            path = Path(real_mkdtemp(*args, **kwargs))
            if kwargs.get('prefix') == 'campaign-launch-':  # the rehearsal's own scratch tree
                self.assertNotIn(self.root, path.parents)
                self.created.append(path)
            return str(path)

        def observe(scratch):
            overlay = scratch / 'law/local-rules.json'
            self.staged = dict(law=sorted(p.name for p in (scratch / 'law').glob('*')) if (scratch / 'law').exists()
                               else None, data=overlay.read_bytes() if overlay.exists() else None, args=[scratch])
            result = real_build(scratch)
            self.staged['page'] = (scratch / self.AGENCY_PAGE).read_text(encoding='utf-8')
            return result

        self.build = patch('campaign_tool.launch_check.build_site', side_effect=observe)
        self.mkdtemp = patch('campaign_tool.launch_check.tempfile.mkdtemp', side_effect=mkdtemp)
        self.build_mock = self.build.start()
        self.mkdtemp.start()
        self.addCleanup(self.build.stop)
        self.addCleanup(self.mkdtemp.stop)

    def rows(self):
        report = launch_checks(self.root)
        self.assertEqual([r['check'] for r in report['checks']], ORDER)
        self.assertEqual(report['total'], 9)
        self.assertTrue(all(not p.exists() for p in self.created))
        return {r['check']: r for r in report['checks']}

    def local_section(self, html):
        return html[html.index('<section class="local-rules">'):html.index('</section>', html.index('<section class="local-rules">'))]

    def cite_by_source(self):
        path = self.root / 'content/findings' / f'{local.SLUG}.json'
        document = json.loads(path.read_text(encoding='utf-8'))
        document['rule_ids'] = []
        document['sources'][0]['rule_id'] = local.LOCAL
        path.write_text(json.dumps(document), encoding='utf-8')

    def test_local_rule_citation_rehearses_the_admitted_campaign(self):
        for form in ('rule_ids', 'sources'):
            with self.subTest(form=form):
                self.overlay([local.rule(), local.rule(rule_id=local.OTHER, effective_to=None)])
                self.finding(rule_ids=[local.LOCAL] if form == 'rule_ids' else [])
                if form == 'sources':
                    self.cite_by_source()
                model = load_content(self.root)  # the campaign's own loader admits it, so a direct build succeeds
                self.assertEqual(len(model['local_rules']), 2)
                rows = self.rows()
                self.assertEqual((rows['build_check']['state'], rows['links']['state']), ('ok', 'ok'))
                staged = json.loads(self.staged['data'])
                self.assertEqual(staged, dict(schema_version=1, jurisdiction='us-ca', rules=model['local_rules']))

    def test_uncited_and_versioned_rules_reach_the_rehearsal_renderer(self):
        rules = [local.rule(), local.rule(citation='Cedar Municipal Code 5.1 (2022 text)', effective_from='2021-07-01',
                                          effective_to='2030-12-31'),
                 local.rule(citation='Cedar Municipal Code 5.1 (2031 text)', effective_from='2031-01-01', effective_to=None),
                 local.rule(rule_id=local.OTHER, citation='Cedar Sign Code 9', agency_ids=[local.SHERIFF, local.COUNCIL],
                            effective_to=None)]
        self.overlay(rules)  # no finding cites any of them
        self.assertEqual(self.rows()['build_check']['state'], 'ok')
        page = self.local_section(self.staged['page'])
        for text in ('(2020 text)', '(2022 text)', '(2031 text)', 'Delete plate reads after 30 days.', local.SOURCE,
                     'Past', 'Not yet in force', 'In force on build date', local.LABEL,
                     "not a determination about the agency's conduct"):
            self.assertIn(text, page)
        self.assertNotIn('Cedar Sign Code 9', page)  # scoped to other agencies
        self.assertTrue(build_site(self.root))  # a direct build of the same campaign renders the same section
        direct = (self.root / self.AGENCY_PAGE).read_text(encoding='utf-8')
        self.assertEqual(self.local_section(direct), page)

    def test_absent_and_valid_empty_overlay_keep_legacy_checks(self):
        self.finding()
        absent = self.rows()
        self.assertEqual(self.staged['law'], None)
        self.assertEqual(self.build_mock.call_args.args, (self.staged['args'][0],))
        self.assertEqual(self.build_mock.call_args.kwargs, {})
        self.overlay([])
        self.assertEqual(self.rows(), absent)  # same rows, order and detail; no law file in the scratch tree
        self.assertEqual(self.staged['law'], None)

    def test_refused_overlay_cannot_become_scratch_build_success(self):
        outside = self.tmp / 'outside.json'
        outside.write_text('{"marker": "never-read"}', encoding='utf-8')
        valid = json.dumps({'schema_version': 1, 'jurisdiction': 'us-ca', 'rules': [local.rule()]})
        cases = {
            'malformed': lambda: self.put('{not json'),
            'duplicate key': lambda: self.put(valid.replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')),
            'unknown field': lambda: self.put(valid.replace('"rules"', '"extra": 1, "rules"')),
            'oversized': lambda: self.put(' ' * (law.LOCAL_RULES_MAX_BYTES + 1)),
            'symlinked file': lambda: (self.put(None), (self.root / 'law/local-rules.json').symlink_to(outside)),
            'dangling file link': lambda: (self.put(None), (self.root / 'law/local-rules.json').symlink_to(self.tmp / 'gone')),
            'symlinked law dir': lambda: (shutil.rmtree(self.root / 'law'), (self.root / 'law').symlink_to(self.tmp)),
            'law is a file': lambda: (shutil.rmtree(self.root / 'law'), (self.root / 'law').write_text('x')),
        }
        for name, arrange in cases.items():
            with self.subTest(name):
                law_path = self.root / 'law'
                if law_path.is_symlink() or law_path.is_file():
                    law_path.unlink()
                elif law_path.exists():
                    shutil.rmtree(law_path)
                self.put(None)
                arrange()
                self.assertIsNotNone(self.load_error())
                self.build_mock.reset_mock()
                rows = self.rows()
                self.assertEqual((rows['build_check']['state'], rows['links']['state']), ('fail', 'fail'))
                self.assertIn('law/', rows['build_check']['detail'])
                self.build_mock.assert_not_called()
        self.assertEqual(outside.read_text(encoding='utf-8'), '{"marker": "never-read"}')

    def put(self, text):
        (self.root / 'law').mkdir(exist_ok=True)
        if text is not None:
            (self.root / 'law/local-rules.json').write_text(text, encoding='utf-8')

    def test_accepted_overlay_is_not_reopened_or_copied_recursively(self):
        self.overlay()
        (self.root / 'law/private-sentinel.bin').write_bytes(b'private\x00')
        real_read, real_tree = law._local_read, shutil.copytree
        staging = []

        def read(root):
            self.assertFalse(staging and Path(root) == self.root, 'campaign overlay reopened after staging began')
            return real_read(root)

        def tree(source, *args, **kwargs):
            if isinstance(source, (str, Path)) and Path(source).parent == self.root:  # the rehearsal's own copies
                staging.append(Path(source))
                self.assertNotEqual(Path(source), self.root / 'law')
            return real_tree(source, *args, **kwargs)

        with patch('campaign_tool.law._local_read', side_effect=read), \
                patch('campaign_tool.launch_check.shutil.copytree', side_effect=tree):
            self.assertEqual(self.rows()['build_check']['state'], 'ok')
        self.assertEqual(staging, [self.root / 'content'])
        self.assertEqual(self.staged['law'], ['local-rules.json'])

    def test_projection_preserves_unicode_and_reader_byte_limit(self):
        citation, duty = 'Cédar Code § 5.1 — 条例', 'Löschen nach 30 Tagen — «sofort».'
        self.overlay([local.rule(citation=citation, duty=duty)])
        self.assertEqual(self.rows()['build_check']['state'], 'ok')
        data = self.staged['data']
        self.assertLessEqual(len(data), law.LOCAL_RULES_MAX_BYTES)
        self.assertIn(citation.encode('utf-8'), data)  # not \u-escaped
        self.assertNotIn(b': ', data)  # compact separators
        rule = json.loads(data)['rules'][0]
        self.assertEqual((rule['citation'], rule['duty']), (citation, duty))
        self.assertEqual(len(_local_rules_bytes([], 'us-ca', limit=len(_local_rules_bytes([], 'us-ca')))),
                         len(_local_rules_bytes([], 'us-ca')))
        with self.assertRaises(ValueError):
            _local_rules_bytes([], 'us-ca', limit=10)

    def test_oversized_projection_fails_before_any_law_file_exists(self):
        self.overlay()
        real_load = load_content

        def inflated(root, today=None):
            model = real_load(root, today=today)
            model['local_rules'][0]['duty'] = 'x' * law.LOCAL_RULES_MAX_BYTES
            return model

        with patch('campaign_tool.launch_check.load_content', side_effect=inflated):
            rows = self.rows()
        self.assertEqual((rows['build_check']['state'], rows['links']['state']), ('fail', 'fail'))
        self.assertIn('larger than', rows['build_check']['detail'])
        self.build_mock.assert_not_called()
        self.assertEqual(len(self.created), 1)  # scratch was made, never given a law folder, and is gone

    def test_overlay_rehearsal_is_read_only_and_cleanup_survives_failure(self):
        self.overlay()
        self.finding(rule_ids=[local.LOCAL])
        (self.root / 'public').mkdir()
        (self.root / 'public/sentinel.bin').write_bytes(b'synthetic\x00\xff')
        before = state(self.root)
        self.assertEqual(self.rows()['build_check']['state'], 'ok')
        self.assertEqual(state(self.root), before)
        failures = {'build error': patch('campaign_tool.launch_check.build_site', side_effect=OSError('synthetic build')),
                    'scan error': patch('campaign_tool.cli.check_public_tree',
                                        side_effect=OSError('synthetic scan'))}
        for name, failing in failures.items():
            with self.subTest(name):
                with failing:
                    rows = self.rows()
                self.assertEqual(rows['build_check']['state'], 'fail')
                self.assertEqual(state(self.root), before)
        self.put('{not json')
        refused = state(self.root)
        self.assertEqual(self.rows()['build_check']['state'], 'fail')
        self.assertEqual(state(self.root), refused)


if __name__ == '__main__':
    unittest.main()
