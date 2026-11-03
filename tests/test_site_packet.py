"""Printable findings packet: synthetic content only."""
import json
import re
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from campaign_tool.content import ContentError, load_content
from campaign_tool.site import Site, build_site
from campaign_tool.cli import read_config

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'fictional-campaign'
PACKET = 'findings/packet-all.html'
BASE = 'https://cedar-records.example.invalid'
NS = '{http://www.sitemaps.org/schemas/sitemap/0.9}'


class PacketTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='packet-test-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'campaign'
        shutil.copytree(EXAMPLE, self.root, ignore=shutil.ignore_patterns('public'))
        self.findings = self.root / 'content/findings'
        self.first = self.findings / 'cedar-policy-posted.json'

    def edit(self, path, **kwargs):
        obj = json.loads(path.read_text())
        obj.update(kwargs)
        path.write_text(json.dumps(obj))

    def second(self, slug='second-finding', **kwargs):
        path = self.findings / (slug + '.json')
        shutil.copyfile(self.first, path)
        self.edit(path, id='fnd_0000000000000002', slug=slug, title='Second synthetic finding', **kwargs)
        return path

    def build(self):
        build_site(self.root, today=date(2026, 10, 20))

    def text(self, path=PACKET):
        return (self.root / 'public' / path).read_text()

    def test_packet_order_fields_sitemap_and_print(self):
        self.edit(self.first, body_md='Secret body marker', corrections=[
            {'date': '2026-02-20', 'note': 'later correction'},
            {'date': '2026-02-10', 'note': 'earlier correction'}])
        self.second(corrections=[], updated_at='2026-03-01T00:00:00+00:00')
        model = load_content(self.root)
        self.build()
        page = self.text()
        self.assertIn('As of 2026-10-20 · 2 published findings · print or save as PDF', page)
        self.assertIn('This is not legal advice', page)
        slugs = [f['slug'] for f in model['findings']]
        self.assertEqual(re.findall(r'<article class="packet-finding" id="f-([^"]+)">', page), slugs)
        self.assertEqual(re.findall(r'<li><a href="#f-([^"]+)">', page), slugs)
        for finding in model['findings']:
            self.assertIn('Finding ' + finding['id'], page)
            self.assertIn(BASE + '/findings/' + finding['slug'] + '.html', page)
        for value in ('Documented fact', 'Verified against the record', '2026-01-15', 'Published 2026-02-01',
                      'Updated 2026-03-01', 'Synthetic Cedar Police ALPR policy', 'sha256 cafecafecafe',
                      'page:1-2', 'href="/sources.html#cedar-police-records"', 'href="/agencies/cedar.html"',
                      'Synthetic fixture; the underlying document does not exist.', 'No corrections have been issued.'):
            self.assertIn(value, page)
        self.assertLess(page.index('earlier correction'), page.index('later correction'))
        self.assertNotIn('Secret body marker', page)
        self.assertIn('href="/findings/packet-all.html"', self.text('findings/index.html'))
        urls = ET.fromstring(self.text('sitemap.xml')).findall(NS + 'url')
        packet = [u for u in urls if u.findtext(NS + 'loc') == BASE + '/' + PACKET]
        self.assertEqual(len(packet), 1)
        self.assertEqual(packet[0].findtext(NS + 'lastmod'), '2026-03-01')
        css = self.text('style.css')
        self.assertIn('.packet-finding{break-inside:avoid-page}', css)
        self.assertIn('.packet-finding+.packet-finding{break-before:auto}', css)

    def test_empty_and_relative(self):
        self.first.unlink()
        self.build()
        page = self.text()
        self.assertIn('No findings have been published yet.', page)
        self.assertNotIn('<ol>', page)
        self.assertIn(BASE + '/' + PACKET, self.text('sitemap.xml'))
        shutil.copyfile(EXAMPLE / 'content/findings/cedar-policy-posted.json', self.first)
        self.edit(self.root / 'content/site.json', base_url=None)
        self.build()
        self.assertIn('href="/findings/cedar-policy-posted.html"', self.text())
        self.assertFalse((self.root / 'public/sitemap.xml').exists())

    def test_escaping_and_unknown_source(self):
        # Exercise renderer with synthetic model data because content validation rejects unsafe markup upstream.
        model = load_content(self.root)
        f = model['findings'][0]
        f['title'] = 'Title <script> "x"'
        f['summary'] = 'Summary <script> "x"'
        f['sources'][0].update(title='Source <script> "x"', locator='page <script> "x"', sha256='a' * 64, rule_id='rule-1')
        site = Site(self.root, read_config(self.root), model, today=date(2026, 10, 20))
        page = site.packet_html()['html']
        main = page.split('<main id="top">', 1)[1].split('</main>', 1)[0]
        self.assertNotIn('<script', main)
        for value in ('Title &lt;script&gt; &quot;x&quot;', 'Summary &lt;script&gt; &quot;x&quot;',
                      'page &lt;script&gt; &quot;x&quot;', 'sha256 aaaaaaaaaaaa', 'rule rule-1'):
            self.assertIn(value, main)
        self.assertIn('Source &lt;script&gt; &quot;x&quot; · sha256', main)
        self.assertNotIn('/sources.html#', main)

    def test_unpublished_finding_is_not_in_packet(self):
        self.second(state='draft', body_md='Unpublished body marker')
        with self.assertRaises(ContentError):
            self.build()
        self.assertFalse((self.root / 'public').exists())

    def test_existing_index_page_collision_refused(self):
        # Simulate a future generated path despite today's slug validation reserving index.
        model = load_content(self.root)
        model['findings'][0]['slug'] = 'index'
        with patch('campaign_tool.site.load_content', return_value=model):
            with self.assertRaises(ContentError) as err:
                self.build()
        self.assertIn('findings/index.html', str(err.exception))
        self.assertIn('generated page', str(err.exception))
        self.assertFalse((self.root / 'public').exists())

    def test_collision_refused_atomically_and_old_packet_slug_still_works(self):
        self.edit(self.first, slug='packet')
        self.build()
        self.assertIn('synthetic fixture', self.text('findings/packet.html'))
        before = {p.relative_to(self.root / 'public'): p.read_bytes() for p in (self.root / 'public').rglob('*') if p.is_file()}
        self.edit(self.first, slug='packet-all')
        with self.assertRaises(ContentError) as err:
            self.build()
        self.assertIn('fnd_0000000000000001', str(err.exception))
        self.assertIn('findings/packet-all.html', str(err.exception))
        self.assertIn('generated page', str(err.exception))
        after = {p.relative_to(self.root / 'public'): p.read_bytes() for p in (self.root / 'public').rglob('*') if p.is_file()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
