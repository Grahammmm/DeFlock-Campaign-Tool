"""Synthetic offline flyer acceptance and refusal paths (copy of examples/fictional-campaign)."""
import contextlib
import hashlib
import html
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
from unittest import mock

from campaign_tool.flyer import build
from campaign_tool.qr import svg
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

    def tree_state(self):
        """Every path under the campaign with bytes, mtime and mode (dirs: mode only), so a refusal can prove no write."""
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode) if p.is_file()
                else (None, None, p.stat().st_mode) for p in sorted(self.root.rglob('*'))}

    def svg_logo(self, name, body, attrs=''):
        assets = self.content / 'public-assets'
        assets.mkdir(exist_ok=True)
        data = (f"<svg xmlns='http://www.w3.org/2000/svg' xmlns:xlink='http://www.w3.org/1999/xlink' "
                f"width='240' height='80'{attrs}>{body}</svg>").encode()
        (assets / name).write_bytes(data)
        return data

    # RF0220-2: an XML Base logo is refused on the first run and on --replace; the earlier flyer survives untouched
    def test_svg_xml_base_refusal_preserves_existing_flyer(self):
        self.finding(1)
        url = 'https://synthetic.invalid/private-base/sprite.svg'
        self.svg_logo('based.svg', "<use href='#a'/>", f" xml:base='{url}'")
        self.svg_logo('nested.svg', f"<g xml:base='{url}'><use href='#a'/></g>")
        self.svg_logo('empty.svg', "<rect width='4' height='4'/>", " xml:base=''")
        message = 'site.json.brand.logo: SVG (external reference)'
        for name in ('based.svg', 'nested.svg', 'empty.svg'):
            self.site({'brand': {'logo': name, 'logo_alt': 'Mark'}})
            with self.subTest(name=name, first_run=True):
                before = self.tree_state()
                result = self.cli()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(message, result.stderr)
                self.assertEqual(result.stdout, '')  # no success JSON, no private notice
                self.assertNotIn('synthetic.invalid', result.stdout + result.stderr)
                self.assertFalse((self.root / 'kit').exists())
                self.assertEqual(self.tree_state(), before)
        for prior in ('png', 'svg'):  # a previous flyer with a safe PNG or SVG logo
            shutil.rmtree(self.root / 'kit', ignore_errors=True)
            if prior == 'png':
                (self.content / 'public-assets' / 'safe.png').write_bytes(png(120, 40))
                self.site({'brand': {'logo': 'safe.png', 'logo_alt': 'Safe'}})
            else:
                self.svg_logo('safe.svg', "<defs><linearGradient id='g'/></defs><rect fill='url(#g)'/><use href='#g'/>")
                self.site({'brand': {'logo': 'safe.svg', 'logo_alt': 'Safe'}})
            self.assertEqual(self.cli().returncode, 0)
            for name in ('based.svg', 'nested.svg', 'empty.svg'):
                self.site({'brand': {'logo': name, 'logo_alt': 'Mark'}})
                with self.subTest(prior=prior, name=name, first_run=False):
                    # site.json changed above, so snapshot after it; kit/ is what a refusal must preserve
                    before = self.tree_state()
                    result = self.cli('--replace')
                    self.assertEqual(result.returncode, 1, result.stderr)
                    self.assertIn(message, result.stderr)
                    self.assertEqual(result.stdout, '')
                    self.assertNotIn('synthetic.invalid', result.stdout + result.stderr)
                    self.assertEqual(self.tree_state(), before)
                    self.assertTrue((self.folder / f'logo.{prior}').is_file())
                    self.assertEqual(stat.S_IMODE((self.folder / f'logo.{prior}').stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)

    # RF0220-2: a self-contained gradient/use logo still builds privately and byte for byte
    def test_clean_svg_fragment_logo_is_private_and_byte_identical(self):
        self.finding(1)
        data = self.svg_logo('mark.svg', "<defs><linearGradient id='g'/></defs><rect fill='url(#g)' width='4' height='4'/>"
                                         "<use href='#g'/><use xlink:href='#g'/><style>rect{fill:url(#g)}</style>")
        self.site({'brand': {'logo': 'mark.svg', 'logo_alt': 'Mark'}})
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])['written'],
                         ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt', 'kit/flyer/logo.svg'])
        self.assertEqual((self.folder / 'logo.svg').read_bytes(), data)
        self.assertIn('<img src="logo.svg" alt="Mark" width="240" height="80">', self.html())
        self.assertNotIn('src="http', self.html())
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700)
        for name in ('flyer.html', 'flyer.txt', 'logo.svg'):
            self.assertEqual(stat.S_IMODE((self.folder / name).stat().st_mode), 0o600, name)
        # an ordinary "base" attribute is not XML Base; replacement with it still works
        data = self.svg_logo('plain.svg', "<use href='#a'/>", " base='https://synthetic.invalid/'")
        self.site({'brand': {'logo': 'plain.svg', 'logo_alt': 'Mark'}})
        self.assertEqual(self.cli('--replace').returncode, 0)
        self.assertEqual((self.folder / 'logo.svg').read_bytes(), data)

    # DF0237-14: QR codes ride on base_url only. The digests below are sha256 of the bytes written by origin/main
    # 42d6a576 (before any QR code) for the same fixtures, so "no base_url" is proven byte-identical to main.
    NO_QR_DIGESTS = {  # public synthetic fixture digests, not credentials
        ('null', 'truncated', 'html'): '8f17d64b992ac23cfde3124a4ee175e2ab7472a0ca156541fe822aaf2cb55d8f',  # pragma: allowlist secret
        ('null', 'truncated', 'txt'): 'ba7c6e6e9f02c4b42bde8d0ea15811962e4dbe5529bb2bb70f94c227e2b63d75',  # pragma: allowlist secret
        ('null', 'two', 'html'): '4f8c339d25d32d2acd6840b4d10a8891ab213e75710b6b1b2c5427d3ed4c0970',  # pragma: allowlist secret
        ('null', 'two', 'txt'): 'c271dc3377b4ceac38847fc9c60affe9188bb7137957e7f5d93859d0eabbf04e',  # pragma: allowlist secret
        ('null', 'empty', 'html'): 'a7b1277852e92450c5fb3ce2c5c238608d8ae739731f2c078f8f947528fc0d3b',  # pragma: allowlist secret
        ('null', 'empty', 'txt'): '188cf8a29e9b137f7952750f79cd6eaa0220d25dbe5829f7851da424ff4e3f52',  # pragma: allowlist secret
        ('null', 'logo', 'html'): 'afdd11615dc5b23c664225a2133edc12ceafa49ee9678fa5bdfde2a356a56ca3',  # pragma: allowlist secret
        ('null', 'logo', 'txt'): 'd5d750dd9ced00ba38a049d16fa20ac2fb8850aa2ac1cb5fbed429e946cdcaa7',  # pragma: allowlist secret
        ('base', 'truncated', 'txt'): '7e49eccc1e79120cf7249d3f7126dcac11bdbe9adee6af926cf9ef1085dedc59',  # pragma: allowlist secret
        ('base', 'two', 'txt'): '5727d6cdeb6370564f1a5471c2a23cae563a3aea10594343e6ade47e6e48fb4d',  # pragma: allowlist secret
        ('base', 'empty', 'txt'): '734aab57de9bea1f06e32921e72a86454022b79bd5a97d0a09369a5db4e01ded',  # pragma: allowlist secret
        ('base', 'logo', 'txt'): '18dfa42140fd9f6b44ee7cc41a52baea79ef7d0d77277da1ab523eb34de51d89',  # pragma: allowlist secret
    }

    def flyer_cases(self):
        """flyer.html / flyer.txt sha256 for four fixtures: cut off, two findings, none, and a PNG logo."""
        sha = lambda name: hashlib.sha256((self.folder / name).read_bytes()).hexdigest()
        out = {}

        def run(case, *args):
            build(self.root, *args, replace=True) if self.folder.exists() else build(self.root, *args)
            out[case] = {'html': sha('flyer.html'), 'txt': sha('flyer.txt')}

        self.finding(1, published='2026-01-01T00:00:00+00:00')
        self.finding(2, published='2026-03-01T00:00:00+00:00')
        run('truncated', 1)
        run('two', 6)
        for number in (1, 2):
            (self.content / f'findings/f{number}.json').unlink()
        run('empty', 3)
        self.finding(1)
        (self.content / 'public-assets').mkdir()
        (self.content / 'public-assets/mark.png').write_bytes(png(120, 40))
        self.site({'brand': {'logo': 'mark.png', 'logo_alt': 'Cedar "Watch" & co'}})
        run('logo', 3)
        return out

    def test_no_base_outputs_are_byte_identical(self):
        for mode in ('null', 'missing'):
            with self.subTest(mode=mode):
                shutil.rmtree(self.root / 'kit', ignore_errors=True)
                document = json.loads((self.content / 'site.json').read_text())
                document.pop('base_url', None)
                document['base_url'] = None
                if mode == 'missing':
                    del document['base_url']
                (self.content / 'site.json').write_text(json.dumps(document), encoding='utf-8')
                cases = self.flyer_cases()
                for case, digests in cases.items():
                    for kind, digest in digests.items():
                        self.assertEqual(digest, self.NO_QR_DIGESTS[('null', case, kind)], (mode, case, kind))
                self.assertIn('<img src="logo.png"', (self.folder / 'flyer.html').read_text())  # the last case has a logo
                for name in ('flyer.html', 'flyer.txt'):
                    data = (self.folder / name).read_bytes()
                    self.assertNotIn(b'<svg', data)
                    self.assertNotIn(b'xmlns', data)
                    self.assertNotIn(b'.qr', data)
                first = (self.folder / 'flyer.html').read_bytes()
                build(self.root, 3, replace=True)
                self.assertEqual(first, (self.folder / 'flyer.html').read_bytes())  # deterministic repeated build
                shutil.rmtree(self.content / 'findings')
                (self.content / 'findings').mkdir()
                shutil.rmtree(self.content / 'public-assets', ignore_errors=True)
                self.site({'brand': {}})

    def test_flyer_text_is_unchanged_by_qr_codes(self):
        cases = self.flyer_cases()
        for case, digests in cases.items():
            self.assertEqual(digests['txt'], self.NO_QR_DIGESTS[('base', case, 'txt')], case)
        self.assertNotIn('<svg', self.text())

    def test_one_qr_beside_each_finding_url_and_the_site_url(self):
        for number in (1, 2, 3):
            self.finding(number, published=f'2026-0{number}-01T00:00:00+00:00')
        build(self.root, 2)
        page = self.html()
        urls = [f'{BASE}/findings/finding-3.html', f'{BASE}/findings/finding-2.html']
        for url in urls + [BASE]:
            self.assertIn(f'<p><a href="{url}">{url}</a>{svg(url)}</p>', page)
        self.assertEqual(page.count('<svg'), 3)  # not the truncation line, not finding 1
        self.assertIn('<p>1 more on the site: ' + BASE + '</p>', page)
        self.assertIn('.qr { display: inline-block', page)
        self.assertEqual(page.count('class="qr"'), 3)
        self.assertEqual(page.count('<article>'), 2)
        for block in re.findall(r'<article>.*?</article>', page, re.S):
            self.assertEqual(block.count('<svg'), 1)  # each QR stays inside its unbreakable article

    def test_qr_encodes_the_raw_url_and_html_escapes_it_once(self):
        base = 'https://synthetic.invalid/a?x=1&y=100%25_b'
        self.site({'base_url': base + '/'})
        self.finding(1)
        build(self.root)
        page = self.html()
        url = base + '/findings/finding-1.html'
        escaped = html.escape(url, quote=True)
        self.assertIn('&amp;', escaped)
        self.assertIn(f'<p><a href="{escaped}">{escaped}</a>{svg(url)}</p>', page)
        self.assertIn(f'<p><a href="{html.escape(base)}">{html.escape(base)}</a>{svg(base)}</p>', page)  # trailing slash dropped
        self.assertNotIn('&amp;amp;', page)
        self.assertNotIn(base + '/<', page)
        self.assertIn(url + '\n', self.text())  # flyer.txt carries the raw URL, never an escaped one

    def test_no_findings_still_gets_one_site_qr(self):
        build(self.root)
        self.assertEqual(self.html().count('<svg'), 1)
        self.assertIn(f'<p><a href="{BASE}">{BASE}</a>{svg(BASE)}</p>', self.html())

    def test_qr_svg_adds_no_link_script_or_external_load(self):
        self.finding(1)
        build(self.root)
        page = self.html()
        for link in re.findall(r'href="([^"]*)"', page):
            self.assertTrue(link == BASE or link.startswith(BASE + '/'), link)
        self.assertNotRegex(page, r'<script|<image|foreignObject|<use|xlink|\bsrc=|url\(|@import|<link|<iframe')
        for fragment in re.findall(r'<svg.*?</svg>', page, re.S):
            self.assertNotIn('synthetic', fragment)  # numeric geometry only: no URL, title or id inside the graphic

    def test_oversized_finding_refuses_before_writes(self):
        self.finding(1)
        self.site({'base_url': 'https://synthetic.invalid/' + 'z' * 180})  # the base fits, base + finding path does not
        before = self.tree_state()
        with self.assertRaises(ValueError) as caught:
            build(self.root)
        self.assertEqual(str(caught.exception), 'QR URL exceeds 213-byte capacity (version 10, level M).')
        self.assertNotIn('synthetic', str(caught.exception))
        self.assertEqual(before, self.tree_state())
        self.assertFalse((self.root / 'kit').exists())
        # --replace keeps every earlier file, its bytes and its mode
        self.site({'base_url': BASE, 'brand': {'logo': 'mark.png', 'logo_alt': 'Mark'}})
        (self.content / 'public-assets').mkdir()
        (self.content / 'public-assets/mark.png').write_bytes(png(120, 40))
        build(self.root)
        self.site({'base_url': 'https://synthetic.invalid/' + 'z' * 180})
        before = self.tree_state()
        with self.assertRaises(ValueError):
            build(self.root, replace=True)
        self.assertEqual(before, self.tree_state())
        result = self.cli('--replace')
        self.assertEqual(result.returncode, 1)
        self.assertIn('QR URL exceeds 213-byte capacity', result.stderr)
        self.assertEqual(before, self.tree_state())

    def test_cli_prints_paths_and_private_notice(self):
        self.finding(1)
        result = self.cli('--max-findings', '2')
        self.assertEqual(result.returncode, 0, result.stderr)
        first, second = result.stdout.splitlines()
        self.assertEqual(json.loads(first)['written'], ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt'])
        self.assertEqual(second, 'Private file; nothing was printed, sent or published.')

    # RF2045-1: logo staging never follows or touches a pre-existing .logo.tmp entry
    def branded(self, data=None):
        self.finding(1)
        assets = self.content / 'public-assets'
        assets.mkdir(exist_ok=True)
        (assets / 'mark.png').write_bytes(data or png(120, 40))
        self.site({'brand': {'logo': 'mark.png', 'logo_alt': 'Mark'}})
        self.folder.mkdir(parents=True, exist_ok=True)

    def sentinel(self):
        path = self.root.parent / 'unrelated-sentinel.bin'
        path.write_bytes(b'unrelated synthetic bytes')
        os.chmod(path, 0o644)
        return path

    @staticmethod
    def meta(path):
        info = os.lstat(path)
        return (info.st_ino, info.st_mode, info.st_mtime_ns, info.st_nlink, info.st_size)

    def assert_private_regular_logo(self, expected):
        logo = self.folder / 'logo.png'
        info = os.lstat(logo)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(logo.read_bytes(), expected)
        return info

    def test_campaign_logo_ignores_legacy_symlink_temp(self):
        self.branded()
        sentinel = self.sentinel()
        legacy = self.folder / '.logo.tmp'
        legacy.symlink_to(sentinel)
        before = (sentinel.read_bytes(), self.meta(sentinel), os.readlink(legacy))
        for replace in (False, True):
            build(self.root, replace=replace)
            self.assertEqual(before, (sentinel.read_bytes(), self.meta(sentinel), os.readlink(legacy)))
            info = self.assert_private_regular_logo(png(120, 40))
            self.assertNotEqual(info.st_ino, os.stat(sentinel).st_ino)
            self.assertEqual(info.st_nlink, 1)

    def test_campaign_logo_ignores_legacy_hardlink_temp(self):
        self.branded()
        sentinel = self.sentinel()
        legacy = self.folder / '.logo.tmp'
        os.link(sentinel, legacy)
        before = (sentinel.read_bytes(), self.meta(sentinel), self.meta(legacy))
        self.assertEqual(before[1][3], 2)
        for replace in (False, True):
            build(self.root, replace=replace)
            self.assertEqual(before, (sentinel.read_bytes(), self.meta(sentinel), self.meta(legacy)))
            info = self.assert_private_regular_logo(png(120, 40))
            self.assertNotEqual(info.st_ino, os.stat(sentinel).st_ino)

    def test_campaign_logo_preserves_other_legacy_temp_entries(self):
        sentinel = self.sentinel()
        kinds = {
            'file': lambda p: p.write_bytes(b'ordinary legacy file'),
            'dangling': lambda p: p.symlink_to(self.root.parent / 'absent-target'),
            'directory': lambda p: p.mkdir(),
        }
        for kind, make in kinds.items():
            for branded in (True, False):
                with self.subTest(kind=kind, branded=branded):
                    shutil.rmtree(self.folder, ignore_errors=True)
                    self.branded()
                    if not branded:
                        self.site({'brand': {}})
                    legacy = self.folder / '.logo.tmp'
                    make(legacy)
                    before = self.meta(legacy)
                    build(self.root)
                    self.assertEqual(before, self.meta(legacy))
                    if kind == 'file':
                        self.assertEqual(legacy.read_bytes(), b'ordinary legacy file')
                    if kind == 'directory':
                        self.assertEqual(list(legacy.iterdir()), [])
                    self.assertEqual((self.folder / 'logo.png').exists(), branded)
                    if branded:
                        self.assert_private_regular_logo(png(120, 40))
                    self.assertEqual([p.name for p in self.folder.glob('.logo.*.tmp')], [])
        self.assertEqual(sentinel.read_bytes(), b'unrelated synthetic bytes')

    def test_campaign_logo_stage_is_private_and_unique(self):
        self.branded()
        legacy = self.folder / '.logo.tmp'
        legacy.write_bytes(b'legacy')
        seen = []
        real_mkstemp, real_replace = tempfile.mkstemp, os.replace

        def mkstemp(*args, **kwargs):
            descriptor, name = real_mkstemp(*args, **kwargs)
            seen.append(Path(name))
            return descriptor, name

        def replace(source, target, *args, **kwargs):
            if Path(target).name == 'logo.png':
                info = os.lstat(source)
                seen.append((Path(source), stat.S_ISREG(info.st_mode), stat.S_IMODE(info.st_mode), info.st_nlink,
                             Path(source).read_bytes()))
            return real_replace(source, target, *args, **kwargs)

        old_umask = os.umask(0o000)
        try:
            with mock.patch('tempfile.mkstemp', mkstemp), mock.patch('os.replace', replace):
                build(self.root)
        finally:
            os.umask(old_umask)
        staged = [item for item in seen if isinstance(item, Path) and item.name.startswith('.logo.')]
        self.assertEqual(len(staged), 1)
        checked = [item for item in seen if isinstance(item, tuple)]
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0][0], staged[-1])
        self.assertEqual(checked[0][1:], (True, 0o600, 1, png(120, 40)))
        self.assertEqual(staged[-1].parent, self.folder)
        self.assertNotEqual(staged[-1].name, '.logo.tmp')
        self.assertTrue(staged[-1].name.startswith('.logo.') and staged[-1].name.endswith('.tmp'))
        self.assertFalse(staged[-1].exists())
        self.assertEqual(legacy.read_bytes(), b'legacy')
        self.assert_private_regular_logo(png(120, 40))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['.logo.tmp', 'flyer.html', 'flyer.txt', 'logo.png'])

    def test_campaign_logo_stage_failures_preserve_previous_logo(self):
        self.branded()
        build(self.root)
        old_logo = png(120, 40)
        new_logo = png(60, 20)
        (self.content / 'public-assets/mark.png').write_bytes(new_logo)
        unrelated = self.folder / '.other.tmp'
        unrelated.write_bytes(b'not ours')
        real_mkstemp, real_fdopen, real_read = tempfile.mkstemp, os.fdopen, Path.read_bytes
        fds = []

        class Proxy:
            def __init__(self, handle, mode):
                self.handle, self.mode = handle, mode

            def fileno(self):
                return self.handle.fileno()

            def write(self, data):
                if self.mode == 'write':
                    self.handle.write(data[:8])
                    self.handle.flush()  # a real partial write reaches the staging file
                    raise OSError('injected write failure')
                return self.handle.write(data)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()
                if self.mode == 'close':
                    raise OSError('injected close failure')
                return False

        def mkstemp(*args, **kwargs):
            descriptor, name = real_mkstemp(*args, **kwargs)
            fds.append(descriptor)
            return descriptor, name

        def reading(path):
            if path.name == 'mark.png' and list(self.folder.glob('.logo.*.tmp')):  # only the copy into staging fails
                raise OSError('injected read failure')
            return real_read(path)

        def fdopen_failing(*args, **kwargs):
            raise OSError('injected fdopen failure')

        def replace_failing(source, target, *args, **kwargs):
            if Path(target).name == 'logo.png':
                raise OSError('injected replace failure')
            return os.rename(source, target)

        cases = {
            'read': [mock.patch.object(Path, 'read_bytes', reading)],
            'fdopen': [mock.patch('os.fdopen', fdopen_failing)],
            'write': [mock.patch('os.fdopen', lambda *a, **k: Proxy(real_fdopen(*a, **k), 'write'))],
            'close': [mock.patch('os.fdopen', lambda *a, **k: Proxy(real_fdopen(*a, **k), 'close'))],
            'replace': [mock.patch('os.replace', replace_failing)],
        }
        for label, patches in cases.items():
            with self.subTest(label):
                del fds[:]
                with mock.patch('tempfile.mkstemp', mkstemp), contextlib.ExitStack() as stack:
                    for item in patches:
                        stack.enter_context(item)
                    with self.assertRaises(OSError) as caught:
                        build(self.root, replace=True)
                self.assertIn(f'injected {label} failure', str(caught.exception))
                self.assertEqual(len(fds), 1)
                with self.assertRaises(OSError):
                    os.fstat(fds[0])  # the raw descriptor was closed
                self.assertEqual((self.folder / 'logo.png').read_bytes(), old_logo)
                self.assertEqual([p.name for p in self.folder.glob('.logo.*.tmp')], [])
                self.assertEqual(unrelated.read_bytes(), b'not ours')
        build(self.root, replace=True)  # retry after the failures succeeds
        self.assert_private_regular_logo(new_logo)
        self.assertEqual([p.name for p in self.folder.glob('.logo.*.tmp')], [])
        self.assertEqual(unrelated.read_bytes(), b'not ours')

    def test_campaign_logo_cli_link_isolation_and_legacy_output_bytes(self):
        self.branded()
        sentinel = self.sentinel()
        before = (sentinel.read_bytes(), self.meta(sentinel))
        (self.folder / '.logo.tmp').symlink_to(sentinel)
        first = self.cli()
        self.assertEqual(first.returncode, 0, first.stderr)
        report, notice = first.stdout.splitlines()
        self.assertEqual(json.loads(report)['written'], ['kit/flyer/flyer.html', 'kit/flyer/flyer.txt', 'kit/flyer/logo.png'])
        self.assertEqual(notice, 'Private file; nothing was printed, sent or published.')
        self.assertEqual(before, (sentinel.read_bytes(), self.meta(sentinel)))
        trapped = (self.html(), self.text(), self.assert_private_regular_logo(png(120, 40)).st_size)
        again = self.cli('--replace')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(before, (sentinel.read_bytes(), self.meta(sentinel)))
        self.assertEqual(trapped, (self.html(), self.text(), (self.folder / 'logo.png').stat().st_size))
        shutil.rmtree(self.root / 'kit')  # the same build with no trap yields identical bytes
        clean = self.cli()
        self.assertEqual(clean.returncode, 0, clean.stderr)
        self.assertEqual(first.stdout, clean.stdout)
        self.assertEqual(trapped[:2], (self.html(), self.text()))
        self.assert_private_regular_logo(png(120, 40))
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ['flyer.html', 'flyer.txt', 'logo.png'])
        # the existing refusals are unchanged: a symlinked final logo is refused and the sentinel stays intact
        os.unlink(self.folder / 'logo.png')
        (self.folder / 'logo.png').symlink_to(sentinel)
        refused = self.cli('--replace')
        self.assertEqual(refused.returncode, 1)
        self.assertIn('symlink', refused.stderr)
        self.assertEqual(before, (sentinel.read_bytes(), self.meta(sentinel)))


if __name__ == '__main__':
    unittest.main()
