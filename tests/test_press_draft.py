"""Offline press drafts, using fictional content only."""
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign_tool.press import build_press

REPO = Path(__file__).resolve().parents[1]
FIRST = 'fnd_0000000000000001'
SECOND = 'fnd_0000000000000002'
SLUG = 'cedar-policy-posted'
DAY = '2026-03-01'


class PressDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fictional-press-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root,
                        ignore=shutil.ignore_patterns('public'))
        self.findings = self.root / 'content/findings'
        self.first = json.loads((self.findings / (SLUG + '.json')).read_text())
        self.second = dict(self.first, id=SECOND, slug='cedar-second', title='Second fictional finding',
                           summary='Second summary.', limitations=[], corrections=[])
        self.save(self.second)

    def save(self, finding):
        (self.findings / (finding['slug'] + '.json')).write_text(json.dumps(finding), encoding='utf-8')

    def site(self, **kw):
        path = self.root / 'content/site.json'
        data = json.loads(path.read_text())
        data.update(kw)
        if kw.get('base_url', '') is None:
            data.pop('base_url')
        path.write_text(json.dumps(data))

    def cli(self, *args):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'press', 'draft',
                               '--directory', str(self.root), *args], cwd=REPO,
                              capture_output=True, text=True, timeout=60)

    def test_single_cli_and_content(self):
        result = self.cli('--finding', FIRST, '--date', DAY)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout[:result.stdout.rindex('}') + 1])
        self.assertEqual(summary, {'path': f'kit/press/{DAY}-{SLUG}.md', 'findings': [FIRST],
                                   'kept_edited': False, 'sent': False})
        self.assertTrue(result.stdout.rstrip().endswith('Draft written to kit/press. Nothing was sent.'))
        text = (self.root / summary['path']).read_text()
        for item in (FIRST, self.first['title'], self.first['summary'], 'Documented fact',
                     'Verified against the record', '2026-01-15', '2026-02-03',
                     self.first['corrections'][0]['note'], self.first['limitations'][0],
                     f'https://cedar-records.example.invalid/findings/{SLUG}.html',
                     'DRAFT: not sent', 'This draft cites published findings only. It is not legal advice.'):
            self.assertIn(item, text)
        self.assertNotIn(self.first['body_md'], text)
        self.assertNotIn(self.first['sources'][0]['sha256'], text)
        self.assertNotIn(self.first['sources'][0]['title'], text)

    def test_order_dedup_and_contact(self):
        self.site(contact_email=None)
        result = build_press(self.root, [SECOND, FIRST, SECOND], DAY)
        self.assertEqual(result['findings'], [SECOND, FIRST])
        text = (self.root / result['path']).read_text()
        self.assertTrue(result['path'].endswith('cedar-second.md'))
        self.assertLess(text.index('## Second fictional finding'), text.index('## ' + self.first['title']))
        self.assertEqual(text.count('## Second fictional finding'), 1)
        self.assertIn('Contact: [add a contact before use]', text)
        self.assertIn('None stated.', text)
        self.assertIn('No corrections issued.', text)

    def test_unknown_and_unpublished_are_aggregated(self):
        # Even an invalid draft in content/ is refused without losing the other bad ID.
        draft = dict(self.first, id='draft-id', slug='draft-finding', state='draft')
        self.save(draft)
        result = self.cli('--finding', 'missing-id', '--finding', 'draft-id', '--date', DAY)
        self.assertEqual(result.returncode, 1)
        self.assertIn('finding draft-id is not a published finding', result.stderr)
        self.assertIn('finding missing-id is not a published finding', result.stderr)
        self.assertLess(result.stderr.index('draft-id'), result.stderr.index('missing-id'))
        self.assertFalse((self.root / 'kit/press').exists())

    def test_bad_arguments_and_missing_inputs_write_nothing(self):
        for args, message in [([], '--finding'), (['--finding', FIRST, '--date', '2026-02-30'], '--date'),
                              (sum((['--finding', str(i)] for i in range(13)), []), '12')]:
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.root / 'kit/press').exists())
        self.site(base_url=None)
        result = self.cli('--finding', FIRST)
        self.assertIn('social drafts need content/site.json base_url', result.stderr)
        self.assertEqual(result.returncode, 1)
        shutil.rmtree(self.root / 'content')
        result = self.cli('--finding', FIRST)
        self.assertIn('press drafts need a content/ directory', result.stderr)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.root / 'kit/press').exists())

    def test_edited_and_symlink_targets_preserved(self):
        result = build_press(self.root, [FIRST], DAY)
        target = self.root / result['path']
        original = target.read_bytes()
        self.assertFalse(build_press(self.root, [FIRST], DAY)['kept_edited'])
        self.assertEqual(target.read_bytes(), original)
        target.write_text('Organizer edit')
        for _ in range(2):
            result = build_press(self.root, [FIRST], DAY)
            self.assertTrue(result['kept_edited'])
            self.assertEqual(target.read_text(), 'Organizer edit')
            self.assertEqual((self.root / result['path']).read_bytes(), original)
        suggestion = (self.root / result['path']).read_bytes()
        self.assertEqual(json.loads((self.root / 'kit/press.suggested/.written.json').read_text()),
                         {target.name: hashlib.sha256(suggestion).hexdigest()})
        self.assertEqual(json.loads((self.root / 'kit/press/.written.json').read_text()),
                         {target.name: hashlib.sha256(original).hexdigest()})
        target.unlink()
        outside = Path(self.tmp.name) / 'outside'
        outside.write_text('untouched')
        os.symlink(outside, target)
        result = build_press(self.root, [FIRST], DAY)
        self.assertTrue(result['kept_edited'])
        self.assertTrue(target.is_symlink())
        self.assertEqual(outside.read_text(), 'untouched')

    def test_cap_refuses_before_writing(self):
        self.first['summary'] = 'x' * 6100
        self.save(self.first)
        with self.assertRaisesRegex(ValueError, '6000'):
            build_press(self.root, [FIRST], DAY)
        self.assertFalse((self.root / 'kit/press').exists())

    def test_no_network_imports(self):
        tree = ast.parse((REPO / 'campaign_tool/press.py').read_text())
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name.split('.')[0] for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split('.')[0])
        self.assertFalse(set(names) & {'smtplib', 'socket', 'http', 'urllib', 'requests', 'subprocess'})


if __name__ == '__main__':
    unittest.main()
