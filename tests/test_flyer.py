"""Synthetic offline flyer acceptance and refusal paths (copy of examples/fictional-campaign)."""
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

from campaign_tool.flyer import build
from campaign_tool.site import CLASSIFICATION_LABELS, CONFIDENCE_LABELS

REPO = Path(__file__).resolve().parents[1]
BASE = 'https://synthetic.invalid'


def chunk(kind, body):
    return len(body).to_bytes(4, 'big') + kind + body + zlib.crc32(kind + body).to_bytes(4, 'big')


def png(width, height):
    ihdr = width.to_bytes(4, 'big') + height.to_bytes(4, 'big') + b'\x08\x02\x00\x00\x00'
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', ihdr) + chunk(b'IDAT', b'\x78\x9c\x01\x02') + chunk(b'IEND', b'')


class FlyerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-flyer-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root)
        self.content = self.root / 'content'
        self.template = json.loads((self.content / 'findings/cedar-policy-posted.json').read_text())
        shutil.rmtree(self.content / 'findings')
        (self.content / 'findings').mkdir()
        self.site({'base_url': BASE})
        self.folder = self.root / 'kit/flyer'

    def site(self, changes):
        path = self.content / 'site.json'
        document = {**json.loads(path.read_text()), **changes}
        path.write_text(json.dumps(document), encoding='utf-8')

    def finding(self, number, state='published', published='2026-02-01T18:00:00+00:00', **extra):
        document = {**self.template, 'id': f'f{number}', 'slug': f'finding-{number}', 'title': f'Finding {number}',
                    'corrections': [], 'state': state, 'published_at': published, **extra}
        (self.content / f'findings/f{number}.json').write_text(json.dumps(document), encoding='utf-8')

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for base in ('content', 'public') for p in (self.root / base).rglob('*') if p.is_file()}

    def html(self):
        return (self.folder / 'flyer.html').read_text()

    def text(self):
        return (self.folder / 'flyer.txt').read_text()

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'flyer', '--directory', str(self.root), *extra],
                              cwd=REPO, capture_output=True, text=True, timeout=30)

    # 1. the example campaign: its published finding with labels and URL; draft/withdrawn/challenged never appear
    def test_example_campaign_and_unpublished_findings_absent(self):
        shutil.copy(REPO / 'examples/fictional-campaign/content/findings/cedar-policy-posted.json',
                    self.content / 'findings/cedar-policy-posted.json')
        self.finding(2, state='draft', title='Draft title')
        self.finding(3, state='withdrawn', title='Withdrawn title')
        self.finding(4, state='challenged', title='Challenged title')
        before = self.snapshot()
        report = build(self.root)
        self.assertEqual(report, {'written': ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt'], 'findings': 1, 'truncated': 0})
        page, text = self.html(), self.text()
        label = f'{CLASSIFICATION_LABELS["documented_fact"]}; {CONFIDENCE_LABELS["verified"]}'
        url = BASE + '/findings/cedar-policy-posted.html'
        for body in (page, text):
            for wanted in (self.template['title'], label, url, 'This is not legal advice'):
                self.assertIn(wanted, body)
            for hidden in ('Draft title', 'Withdrawn title', 'Challenged title', 'nothing is published yet'):
                self.assertNotIn(hidden, body)
        self.assertIn('Cedar County (fictional), CA', text)
        self.assertIn('Local records. Public understanding.', text)
        self.assertIn('@page { size: letter; margin: 0.5in }', page)
        self.assertNotIn('<', text)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        for name in ('flyer.html', 'flyer.txt'):
            self.assertEqual(stat.S_IMODE((self.folder / name).stat().st_mode), 0o600)

    # 2. max findings: truncation count, newest published_at first (zone-aware), bounds
    def test_max_findings_order_and_bounds(self):
        self.finding(1, published='2026-01-01T00:00:00+00:00')
        self.finding(2, published='2026-03-01T00:30:00+05:00')  # 2026-02-28 19:30 UTC: older than finding 3
        self.finding(3, published='2026-03-01T00:00:00+00:00')
        report = build(self.root, 1)
        self.assertEqual((report['findings'], report['truncated']), (1, 2))
        self.assertIn('Finding 3', self.text())
        self.assertNotIn('Finding 2', self.text())
        self.assertIn(f'2 more on the site: {BASE}', self.text())
        build(self.root, 6, replace=True)
        titles = re.findall(r'^Finding \d$', self.text(), re.M)
        self.assertEqual(titles, ['Finding 3', 'Finding 2', 'Finding 1'])
        self.assertNotIn('more on the site', self.text())
        for bad in (0, 7, True, '3', None):
            with self.assertRaises(ValueError):
                build(self.root, bad, replace=True)
        for bad in ('0', '7'):
            result = self.cli('--max-findings', bad)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('invalid choice', result.stderr)

    # 3. nothing published: the plain line, still written
    def test_nothing_published(self):
        self.finding(1, state='draft')
        report = build(self.root)
        self.assertEqual(report['findings'], 0)
        for body in (self.html(), self.text()):
            self.assertIn('Records are requested; nothing is published yet.', body)
            self.assertNotIn('Finding 1', body)

    # 4. no base_url: no links, no URL text
    def test_no_base_url(self):
        self.site({'base_url': None})
        for number in (1, 2):
            self.finding(number)
        build(self.root, 1)
        self.assertNotIn('<a ', self.html())
        self.assertNotIn('href', self.html())
        for body in (self.html(), self.text()):
            self.assertNotIn('http', body)
            self.assertNotIn('Get involved', body)
        self.assertIn('1 more on the site', self.text())
        self.assertNotIn('1 more on the site:', self.text())

    # 5. logo: copied with a relative src, missing refused by path, none -> no img
    def test_logo(self):
        self.finding(1)
        build(self.root)
        self.assertNotIn('<img', self.html())
        assets = self.content / 'public-assets'
        assets.mkdir()
        self.site({'brand': {'logo': 'mark.png', 'logo_alt': 'Cedar "Watch" & co'}})
        with self.assertRaises(ValueError) as caught:
            build(self.root, replace=True)
        self.assertIn('content/public-assets/mark.png', str(caught.exception))
        self.assertNotIn('<img', self.html())  # the refusal left the earlier flyer untouched
        (assets / 'mark.png').write_bytes(png(120, 40))
        build(self.root, replace=True)
        self.assertEqual((self.folder / 'logo.png').read_bytes(), png(120, 40))
        self.assertIn('<img src="logo.png" alt="Cedar &quot;Watch&quot; &amp; co" width="120" height="40">', self.html())
        self.assertEqual(stat.S_IMODE((self.folder / 'logo.png').stat().st_mode), 0o600)
        self.assertNotIn('src="http', self.html())
        # a symlinked logo is refused and writes nothing new
        (assets / 'link.png').symlink_to(assets / 'mark.png')
        self.site({'brand': {'logo': 'link.png', 'logo_alt': 'Linked'}})
        with self.assertRaises(ValueError) as caught:
            build(self.root, replace=True)
        self.assertIn('symlink', str(caught.exception))
        # dropping the logo and replacing removes the old copy and the img
        self.site({'brand': {}})
        build(self.root, replace=True)
        self.assertFalse((self.folder / 'logo.png').exists())
        self.assertNotIn('<img', self.html())

    def test_logo_missing_on_first_run_writes_nothing(self):
        self.site({'brand': {'logo': 'mark.png', 'logo_alt': 'Mark'}})
        (self.content / 'public-assets').mkdir()
        with self.assertRaises(ValueError):
            build(self.root)
        self.assertFalse((self.root / 'kit').exists())

    # 6. second run refused with the files unchanged; --replace rewrites
    def test_second_run_needs_replace(self):
        self.finding(1)
        build(self.root)
        before = (self.html(), self.text())
        self.finding(2, published='2026-03-01T00:00:00+00:00')
        with self.assertRaises(ValueError) as caught:
            build(self.root)
        self.assertIn('kit/flyer/flyer.html', str(caught.exception))
        self.assertIn('--replace', str(caught.exception))
        self.assertEqual(before, (self.html(), self.text()))
        build(self.root, replace=True)
        self.assertIn('Finding 2', self.html())
        self.assertNotEqual(before[0], self.html())
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertIn('--replace', result.stderr)

    def test_partial_leftover_refused_before_anything_is_written(self):
        self.finding(1)
        self.folder.mkdir(parents=True)
        (self.folder / 'flyer.txt').write_text('mine')
        with self.assertRaises(ValueError) as caught:
            build(self.root)
        self.assertIn('kit/flyer/flyer.txt', str(caught.exception))
        self.assertEqual((self.folder / 'flyer.txt').read_text(), 'mine')
        self.assertFalse((self.folder / 'flyer.html').exists())

    # 7. hostile title is escaped; no script, no external src or href other than base_url
    def test_markup_is_escaped_and_nothing_external_loads(self):
        self.finding(1, title='<script>alert("x")</script> & "q"')
        build(self.root)
        page = self.html()
        self.assertNotIn('<script', page.lower())
        self.assertIn('&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; &quot;q&quot;', page)
        self.assertIn('<script>alert("x")</script> & "q"', self.text())
        self.assertEqual(re.findall(r'\bsrc=', page), [])
        for link in re.findall(r'href="([^"]*)"', page):
            self.assertTrue(link == BASE or link.startswith(BASE + '/'), link)
        self.assertNotRegex(page, r'url\(|@import|<link|<iframe')

    # 8. symlinks refused; nothing under public/ or content/ changes
    def test_symlinks_refused_and_public_tree_untouched(self):
        self.finding(1)
        before = self.snapshot()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (self.root / 'kit').mkdir()
        os.symlink(elsewhere, self.root / 'kit/flyer')
        with self.assertRaises(ValueError):
            build(self.root)
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(self.root / 'kit/flyer')
        self.folder.mkdir()
        os.symlink(elsewhere / 'target.html', self.folder / 'flyer.html')
        with self.assertRaises(ValueError):
            build(self.root, replace=True)
        self.assertEqual(os.listdir(elsewhere), [])
        os.unlink(self.folder / 'flyer.html')
        build(self.root)
        self.assertEqual(before, self.snapshot())

    def test_cli_prints_paths_and_private_notice(self):
        self.finding(1)
        result = self.cli('--max-findings', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = result.stdout.splitlines()
        self.assertEqual(json.loads(first)['written'], ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt'])
        self.assertEqual(second, 'Private file; nothing was printed, sent or published.')


if __name__ == '__main__':
    unittest.main()
