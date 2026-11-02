"""Synthetic offline meeting-pack acceptance and refusal paths."""
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from campaign_tool.meeting_pack import build

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)


class MeetingPackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-meeting-pack-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root)
        self.content = self.root / 'content'
        for folder in ('findings', 'meetings'):
            shutil.rmtree(self.content / folder)
            (self.content / folder).mkdir()
        self.site = json.loads((self.content / 'site.json').read_text())
        self.site['base_url'] = 'https://synthetic.invalid'
        self.write('site.json', self.site)
        self.template = json.loads((REPO / 'examples/fictional-campaign/content/findings/cedar-policy-posted.json').read_text())
        self.meeting = {'id': 'm', 'body': 'Synthetic Council', 'starts_at': '2099-11-02T18:00:00-08:00',
                        'agenda_item': 'Item 7', 'location': 'Synthetic Hall', 'agency_id': 'ca-cedar-police',
                        'agenda_url': 'https://synthetic.invalid/agenda', 'comment_kit_md': 'Hello.\nThank you.\n'}
        self.library = []
        self.finding(1)
        self.finding(2)
        self.stage()
        self.folder = self.root / 'kit/meeting-packs/m'

    def write(self, relative, value):
        (self.content / relative).write_text(json.dumps(value), encoding='utf-8')

    def stage(self):
        self.write('meetings/m.json', self.meeting)

    def finding(self, number, **extra):
        digest = hashlib.sha256(f'synthetic {number}'.encode()).hexdigest()
        finding = {**self.template, 'id': f'f{number}', 'slug': f'finding-{number}', 'title': f'Finding {number}',
                   'event_date': f'2026-01-{number:02d}', 'corrections': [],
                   'sources': [{'sha256': digest, 'title': f'Source {number}', 'locator': f'page:{number}'}], **extra}
        self.write(f'findings/f{number}.json', finding)
        self.library = [entry for entry in self.library if entry['sha256'] != digest]
        self.library.append({'sha256': digest, 'title': f'Source {number}', 'agency_id': 'ca-cedar-police',
                             'received_at': f'2026-02-{number:02d}T12:00:00Z', 'public_url': None, 'fragment': f'source-{number}'})
        self.write('sources.json', self.library)
        return finding

    def pack(self, **kwargs):
        return build(self.root, 'm', today=NOW, **kwargs)

    def html(self):
        return (self.folder / 'handout.html').read_text()

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for base in ('content', 'public') for p in (self.root / base).rglob('*') if p.is_file()}

    def test_three_files_sources_comment_permissions_and_no_public_mutation(self):
        before = self.snapshot()
        report = self.pack()
        self.assertEqual(report, {'written': [f'kit/meeting-packs/m/{name}' for name in
                                             ('handout.html', 'comment.md', 'sources.csv')], 'findings': 2, 'truncated': 0})
        page = self.html()
        for number in (1, 2):
            self.assertIn(f'Finding {number}', page)
            self.assertIn(f'received 2026-02-{number:02d}', page)
            self.assertIn(self.library[number-1]['sha256'][:12], page)
        self.assertIn('@page { size: letter; margin: 0.6in }', page)
        self.assertIn('does not by itself prove who wrote it', page)
        self.assertIn('Synthetic Council', page)
        self.assertIn('Item 7', page)
        self.assertIn('Synthetic Hall', page)
        self.assertIn('not legal advice', page.lower())
        self.assertEqual((self.folder / 'comment.md').read_bytes(), self.meeting['comment_kit_md'].encode())
        for path in self.folder.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotRegex(path.read_text(), r'[0-9a-fA-F]{40,}')
        for path in (self.root / 'kit', self.folder.parent, self.folder):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.snapshot(), before)

    def test_newest_first_truncation_and_optional_base(self):
        for number in range(3, 8):
            self.finding(number)
        for base in ('https://synthetic.invalid', None):
            self.site['base_url'] = base
            self.write('site.json', self.site)
            report = self.pack(replace=self.folder.exists())
            self.assertEqual((report['findings'], report['truncated']), (5, 2))
            page = self.html()
            self.assertIn('2 more on the site' + (': https://synthetic.invalid' if base else '</p>'), page)
            self.assertNotIn('Finding 1', page)
            self.assertLess(page.index('Finding 7'), page.index('Finding 3'))
            self.assertEqual('/findings/' in page, bool(base))

    def test_no_agency_includes_all_but_drafts_and_unpublished(self):
        self.meeting.pop('agency_id')
        self.stage()
        self.finding(3, agency_id='ca-other-police')
        self.finding(4, state='draft')
        self.finding(5, state='withdrawn')
        self.assertEqual(self.pack()['findings'], 3)
        self.assertNotIn('Finding 4', self.html())
        self.assertNotIn('Finding 5', self.html())

    def test_agency_filter_and_empty_state(self):
        self.meeting['agency_id'] = 'ca-other-police'
        self.stage()
        self.assertEqual(self.pack()['findings'], 0)
        self.assertIn('Records are requested; nothing is published yet.', self.html())
        self.assertNotIn('<ul', self.html())

    def test_missing_content_or_stage(self):
        (self.content / 'meetings/m.json').unlink()
        with self.assertRaisesRegex(ValueError, 'meetings-stage --id m'):
            self.pack()
        shutil.rmtree(self.content)
        with self.assertRaisesRegex(ValueError, 'run init/content first'):
            self.pack()
        self.assertFalse(self.folder.exists())

    def test_cancelled_cannot_be_bypassed_by_allow_past(self):
        self.meeting['status'] = 'cancelled'
        self.stage()
        with self.assertRaisesRegex(ValueError, 'cancelled'):
            self.pack(allow_past=True)
        self.assertFalse(self.folder.exists())

    def test_past_and_timezone_comparison(self):
        self.meeting['starts_at'] = '2026-10-31T20:00:00-07:00'
        self.stage()
        self.pack()  # 03:00 UTC on November 1, after NOW
        self.meeting['starts_at'] = '2026-10-31T16:00:00-07:00'
        self.stage()
        with self.assertRaisesRegex(ValueError, '--allow-past'):
            self.pack(replace=True)
        self.pack(allow_past=True, replace=True)

    def test_stale_correction_requires_restage_and_fresh_marks_correction(self):
        self.finding(1, corrections=[{'date': '2026-02-03', 'note': 'Synthetic correction'}])
        self.meeting['comment_kit_md'] = '/findings/finding-1.html'
        self.stage()
        with self.assertRaisesRegex(ValueError, 'meetings-stage --id m --replace'):
            self.pack(allow_past=True, replace=True)
        self.meeting['comment_kit_md'] += ' (corrected 2026-02-03;'
        self.stage()
        self.pack()
        self.assertIn('corrected 2026-02-03', self.html())

    def test_existing_pack_is_untouched_without_replace(self):
        self.pack()
        old = (self.folder / 'handout.html').read_bytes()
        self.finding(2, title='Replacement title')
        with self.assertRaisesRegex(ValueError, '--replace'):
            self.pack()
        self.assertEqual((self.folder / 'handout.html').read_bytes(), old)
        self.pack(replace=True)
        self.assertIn('Replacement title', self.html())

    def test_symlinks_at_every_output_level_refused(self):
        outside = self.root.parent / 'outside'
        outside.mkdir()
        for relative in ('kit', 'kit/meeting-packs', 'kit/meeting-packs/m', 'kit/meeting-packs/m/comment.md'):
            with self.subTest(relative=relative):
                target = self.root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    shutil.rmtree(target)
                target.symlink_to(outside, target_is_directory=True)
                with self.assertRaisesRegex(ValueError, 'symlink'):
                    self.pack(replace=True)
                target.unlink()
                self.assertEqual(list(outside.iterdir()), [])

    def test_invalid_ids_refused_before_reads_and_cli_max_bounds(self):
        with patch('campaign_tool.meeting_pack._read_json', side_effect=AssertionError('read')):
            for bad in ('../x', '', 'x' * 81, 'a/b', '.', '..'):
                with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, 'invalid meeting id'):
                    build(self.root, bad)
        for maximum in ('0', '11'):
            result = self.cli('--max-findings', maximum)
            self.assertEqual(result.returncode, 2)
        self.assertFalse(self.folder.exists())

    def test_html_escaping_and_only_intended_links(self):
        self.finding(2, title='<script>alert("x")</script> & title')
        self.pack()
        page = self.html()
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('&amp; title', page)
        self.assertNotIn('<script', page)
        urls = set(re.findall(r'https?://[^<"\s]+', page))
        self.assertEqual(urls, {'https://synthetic.invalid/agenda', 'https://synthetic.invalid/findings/finding-1.html',
                                'https://synthetic.invalid/findings/finding-2.html'})

    def test_csv_quoting_dedup_and_multiple_locators(self):
        one = self.finding(1)
        title = 'Source, "quoted"\nsecond line'
        source = {**one['sources'][0], 'title': title}
        self.finding(1, sources=[source])
        self.finding(2, sources=[{**source, 'locator': 'page:9'}])
        # Repeated library registration is not allowed; keep the canonical first entries.
        self.write('sources.json', self.library[:2])
        self.pack()
        rows = list(csv.reader(io.StringIO((self.folder / 'sources.csv').read_text())))
        self.assertEqual(rows[0], ['title', 'record_prefix', 'received', 'locator', 'public_copy', 'cited_by'])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][0], title)
        self.assertEqual(rows[1][4], 'held privately')
        self.assertIn('page:9', rows[1][3])
        self.assertIn('page:1', rows[1][3])
        self.assertIn('finding-1', rows[1][5])
        self.assertIn('finding-2', rows[1][5])

    def test_full_hash_in_verbatim_comment_refuses_without_writes(self):
        self.meeting['comment_kit_md'] = 'Do not expose ' + 'a' * 64
        self.stage()
        with self.assertRaisesRegex(ValueError, 'full hash'):
            self.pack()
        self.assertFalse(self.folder.exists())

    def test_atomic_write_failure_keeps_existing_file_and_cleans_temp(self):
        self.pack()
        before = {p.name: p.read_bytes() for p in self.folder.iterdir()}
        with patch('campaign_tool.meeting_pack.os.replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaisesRegex(OSError, 'synthetic disk failure'):
                self.pack(replace=True)
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir()}, before)

    def test_public_source_url_and_maximum_override(self):
        self.library[0]['public_url'] = 'https://synthetic.invalid/source'
        self.write('sources.json', self.library)
        report = self.pack(max_findings=1)
        self.assertEqual((report['findings'], report['truncated']), (1, 1))
        self.pack(max_findings=10, replace=True)
        self.assertIn('https://synthetic.invalid/source', (self.folder / 'sources.csv').read_text())

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'meeting-pack', '--directory',
                               str(self.root), '--id', 'm', *extra], cwd=REPO, capture_output=True, text=True, timeout=30)

    def test_cli_json_and_refusal(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['findings'], 2)
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn('--replace', result.stderr)
