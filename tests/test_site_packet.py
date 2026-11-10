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
# DF1109-08: the only packet addition a legacy fixture (no counterevidence, reply or Updates) gains, once per finding.
LEGACY_COUNTER = b'<h4>Counterevidence considered</h4><ul><li>None identified at publication.</li></ul><h4>Corrections</h4>'


def strip_packet_counterevidence(test, name, data):
    """For the legacy whole-tree digests: assert the packet holds exactly one default counterevidence block per finding
    (and no reply or Updates section), then remove exactly those blocks; every other file is returned untouched."""
    if name != PACKET:
        return data
    test.assertEqual(data.count(LEGACY_COUNTER), data.count(b'<article class="packet-finding"'), name)
    test.assertNotIn(b'id="packet-response-', data, name)
    test.assertNotIn(b'id="packet-updates-', data, name)
    return data.replace(LEGACY_COUNTER, b'<h4>Corrections</h4>')


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


SHA = 'cafe' * 16
RECEIVED = {'status': 'received', 'prepared_on': '2026-03-01', 'asked_on': '2026-03-02', 'reply_by': '2026-03-16',
            'on': '2026-03-09', 'text': 'Line one <i>"q"</i> & more\nLine two'}
UPDATES = [{'date': '2026-03-04', 'note': 'first stored "q"'}, {'date': '2026-03-10', 'note': 'newest & last'},
           {'date': '2026-03-04', 'note': 'second stored, same day'}]


def tree(public):
    return {p.relative_to(public).as_posix(): p.read_bytes() for p in sorted(public.rglob('*')) if p.is_file()}


class PacketSectionTests(unittest.TestCase):
    """DF1109-08: each packet article states the finding page's counterevidence, agency reply and dated Updates."""
    setUp, edit, second, build, text = PacketTests.setUp, PacketTests.edit, PacketTests.second, PacketTests.build, PacketTests.text

    def article(self, slug, page=None):
        page = page if page is not None else self.text()
        return re.search(rf'<article class="packet-finding" id="f-{re.escape(slug)}">.*?</article>', page, re.S).group(0)

    def finding(self, slug):
        return self.text(f'findings/{slug}.html')

    def reply_parity(self, slug):
        """The packet's reply section equals the finding page's, apart from its h4 and per-finding id."""
        page = re.search(r'<section class="agency-response" id="agency-response">.*?</section>', self.finding(slug), re.S)
        packet = re.search(r'<section class="agency-response" id="[^"]*">.*?</section>', self.article(slug), re.S)
        if page is None:
            self.assertIsNone(packet, slug)
            self.assertNotIn('Agency response', self.article(slug))
            return None
        expected = page.group(0).replace('id="agency-response"><h3>', f'id="packet-response-{slug}"><h4>', 1).replace('</h3>', '</h4>', 1)
        self.assertEqual(packet.group(0), expected)
        return packet.group(0)

    def test_packet_counterevidence_matches_finding(self):
        self.edit(self.first, counterevidence=['Minutes say "none" & more', 'A second item'])
        self.second(counterevidence=[])
        self.build()
        for slug, heading in (('cedar-policy-posted', 'Counterevidence considered'), ('second-finding', 'Counterevidence considered')):
            rows = re.search(rf'<h3>{heading}</h3><ul>(.*?)</ul>', self.finding(slug)).group(1)
            article = self.article(slug)
            self.assertEqual(article.count(f'<h4>{heading}</h4><ul>{rows}</ul><h4>Corrections</h4>'), 1, slug)
            self.assertLess(article.index('<h4>Limitations</h4>'), article.index(f'<h4>{heading}</h4>'))
        self.assertIn('<li>Minutes say &quot;none&quot; &amp; more</li><li>A second item</li>',
                      self.article('cedar-policy-posted'))
        self.assertIn('<h4>Counterevidence considered</h4><ul><li>None identified at publication.</li></ul>',
                      self.article('second-finding'))
        model = load_content(self.root)
        site = Site(self.root, read_config(self.root), model, today=date(2026, 10, 20))
        for f in model['findings']:  # one helper renders both
            rows = site.counterevidence_items_html(f)
            self.assertIn(f'<h3>Counterevidence considered</h3><ul>{rows}</ul>', self.finding(f['slug']))
            self.assertIn(f'<h4>Counterevidence considered</h4><ul>{rows}</ul>', self.article(f['slug']))
        model['findings'][0]['counterevidence'] = ['<b>raw</b>']  # the loader refuses markup; the renderer escapes it anyway
        self.assertIn('<li>&lt;b&gt;raw&lt;/b&gt;</li>', site.packet_html()['html'])
        (self.root / 'content/labels.json').write_text(json.dumps({'schema_version': 1, 'labels': {
            'finding.counterevidence': 'Contrary evidence', 'finding.none_identified': 'Nothing contrary found.'}}))
        self.build()
        self.assertIn('<h4>Contrary evidence</h4><ul><li>Nothing contrary found.</li></ul>', self.article('second-finding'))
        self.assertIn('<h3>Contrary evidence</h3><ul><li>Nothing contrary found.</li></ul>', self.finding('second-finding'))

    def test_packet_agency_response_status_matrix(self):
        no_response = {'status': 'no_response', 'prepared_on': '2026-03-01', 'asked_on': '2026-03-02',
                       'reply_by': '2026-03-16', 'on': '2026-03-16'}  # on == reply_by: the earliest allowed day
        declined = {k: v for k, v in RECEIVED.items() if k != 'text'} | {'status': 'declined'}
        cases = {'absent': None, 'pending': {'status': 'pending', 'prepared_on': '2026-03-01', 'reply_by': '2026-03-16'},
                 'received': RECEIVED, 'declined': declined, 'declined_text': declined | {'text': 'No comment <at> this time.'},
                 'no_response': no_response}
        for name, reply in cases.items():
            with self.subTest(name):
                obj = json.loads(self.first.read_text())
                obj.pop('agency_response', None)
                if reply is not None:
                    obj['agency_response'] = reply
                self.first.write_text(json.dumps(obj))
                self.build()
                section = self.reply_parity('cedar-policy-posted')
                if name in ('absent', 'pending'):
                    self.assertNotIn('packet-response-', self.text())
                    continue
                self.assertNotIn('2026-03-01', section)  # prepared_on never shown
                article = self.article('cedar-policy-posted')
                self.assertLess(article.index('<h4>Counterevidence considered</h4>'), article.index(section))
                self.assertLess(article.index(section), article.index('<h4>Corrections</h4>'))
        self.assertIn('asked on 2026-03-02; no response by 2026-03-16', section)
        self.build_with(agency_response=RECEIVED)
        section = self.reply_parity('cedar-policy-posted')
        self.assertIn('<p>The agency replied on 2026-03-09. We asked on 2026-03-02.</p>', section)
        self.assertIn('<p class="reply-text">Line one &lt;i&gt;&quot;q&quot;&lt;/i&gt; &amp; more<br>Line two</p>', section)

    def build_with(self, **kwargs):
        self.edit(self.first, **kwargs)
        self.build()

    def test_packet_reply_record_links_and_short_hash(self):
        self.build_with(agency_response=RECEIVED | {'record_sha256': SHA})
        section = self.reply_parity('cedar-policy-posted')
        self.assertIn('<code>cafecafecafe</code>', section)
        self.assertNotIn(SHA, section)
        self.assertNotIn(SHA, self.text())
        model = load_content(self.root)
        site = Site(self.root, read_config(self.root), model, today=date(2026, 10, 20))
        f = model['findings'][0]
        for pages in ({}, {SHA: '/records/synthetic.html'}):
            with self.subTest(pages=pages), patch.object(Site, 'record_pages', return_value=pages):
                page = site.agency_response_html(f)
                packet = site.agency_response_html(f, heading_level=4, section_id='packet-response-cedar-policy-posted')
                self.assertEqual(packet, page.replace('id="agency-response"><h3>', 'id="packet-response-cedar-policy-posted"><h4>', 1)
                                 .replace('</h3>', '</h4>', 1))
                self.assertEqual('href="/records/synthetic.html"' in packet, bool(pages))
                self.assertIn('<code>cafecafecafe</code>', packet)
                self.assertNotIn(SHA, packet)

    def test_packet_updates_after_corrections_newest_first(self):
        self.edit(self.first, corrections=[{'date': '2026-02-20', 'note': 'later correction'},
                                           {'date': '2026-02-10', 'note': 'earlier correction'}])
        self.build_with(updates=UPDATES)
        self.second(updates=[])
        self.build()
        page = re.search(r'<h3 id="updates">.*?</ul>', self.finding('cedar-policy-posted')).group(0)
        article = self.article('cedar-policy-posted')
        expected = page.replace('<h3 id="updates">', '<h4 id="packet-updates-cedar-policy-posted">', 1).replace('</h3>', '</h4>', 1)
        self.assertEqual(article.count(expected), 1)
        self.assertEqual(re.findall(r'Update · <time datetime="([^"]+)">[^<]*</time>: ([^<]*)</li>', expected),
                         [('2026-03-10', 'newest &amp; last'), ('2026-03-04', 'second stored, same day'),
                          ('2026-03-04', 'first stored &quot;q&quot;')])
        self.assertLess(article.index('earlier correction'), article.index('later correction'))
        self.assertLess(article.index('later correction'), article.index(expected))
        self.assertLess(article.index(expected), article.index('<p>Finding fnd_0000000000000001'))
        self.assertNotIn('Updates', self.article('second-finding'))
        (self.root / 'content/labels.json').write_text(json.dumps({'schema_version': 1, 'labels': {
            'finding.updates': 'Later notes', 'finding.update': 'Note'}}))
        self.build()
        self.assertIn('<h4 id="packet-updates-cedar-policy-posted">Later notes</h4><ul><li>Note · ', self.text())

    def test_packet_multiple_findings_unique_section_ids(self):
        self.edit(self.first, updates=UPDATES[:1], agency_response=RECEIVED)
        self.second(updates=UPDATES[1:], agency_response=RECEIVED)
        self.build()
        page = self.text()
        ids = re.findall(r' id="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))
        for slug in ('cedar-policy-posted', 'second-finding'):
            self.assertIn(f'id="packet-response-{slug}"><h4>Agency response</h4>', self.article(slug))
            self.assertIn(f'<h4 id="packet-updates-{slug}">Updates</h4>', self.article(slug))
            self.assertEqual(page.count(f'<li><a href="#f-{slug}">'), 1)
            self.assertTrue(self.article(slug).startswith(f'<article class="packet-finding" id="f-{slug}"><h3>'))
        self.assertNotIn('id="updates"', page)
        self.assertNotIn('id="agency-response"', page)
        main = page.split('<main id="top">', 1)[1]
        self.assertNotRegex(main, r'<h3[^>]*>(Updates|Agency response|Counterevidence considered)</h3>')

    def rich(self):
        self.edit(self.first, body_md='Body-only marker', counterevidence=['Contrary entry'], agency_response=RECEIVED,
                  updates=UPDATES)

    def test_packet_is_still_summary_only_and_other_files_unchanged(self):
        self.rich()
        self.build()
        self.assertIn('Body-only marker', self.finding('cedar-policy-posted'))
        self.assertNotIn('Body-only marker', self.text())
        for marker in ('Contrary entry', 'The agency replied on 2026-03-09', 'newest &amp; last'):
            self.assertIn(marker, self.article('cedar-policy-posted'))
        public = self.root / 'public'
        # The finding page keeps its pre-DF1109-08 markup for all three sections (h3, ids #agency-response / #updates).
        page = self.finding('cedar-policy-posted')
        for old in ('<h3>Counterevidence considered</h3><ul><li>Contrary entry</li></ul>',
                    '<section class="agency-response" id="agency-response"><h3>Agency response</h3><p>The agency replied on '
                    '2026-03-09. We asked on 2026-03-02.</p><p class="reply-text">Line one &lt;i&gt;&quot;q&quot;&lt;/i&gt; '
                    '&amp; more<br>Line two</p></section>',
                    '<h3 id="updates">Updates</h3><ul><li>Update · <time datetime="2026-03-10">2026-03-10</time>: newest '
                    '&amp; last</li><li>Update · <time datetime="2026-03-04">2026-03-04</time>: second stored, same day</li>'
                    '<li>Update · <time datetime="2026-03-04">2026-03-04</time>: first stored &quot;q&quot;</li></ul>'):
            self.assertEqual(page.count(old), 1, old)
        # Only the packet shows the additions: rendering the packet alone from the plain finding changes no other file.
        rich = tree(public)
        real = Site.packet_html

        def plain_packet(site):
            model = site.model
            site.model = dict(model, findings=[{k: v for k, v in f.items() if k not in ('agency_response', 'updates')}
                                               | {'counterevidence': []} for f in model['findings']])
            try:
                return real(site)
            finally:
                site.model = model
        with patch.object(Site, 'packet_html', plain_packet):
            self.build()
        plain = tree(public)
        self.assertEqual(set(rich), set(plain))
        self.assertEqual(sorted(n for n in rich if rich[n] != plain[n]), [PACKET])
        self.assertEqual(plain[PACKET].count(LEGACY_COUNTER), 1)
        self.edit(self.root / 'content/site.json', base_url=None)
        self.build()
        self.assertNotIn('Body-only marker', self.text())
        self.first.unlink()  # the empty packet gains nothing
        self.build()
        self.assertIn('No findings have been published yet.', self.text())
        self.assertNotIn('<h4>', self.text())

    def test_packet_refuses_bad_update_or_reply_without_overwrite(self):
        self.rich()
        self.build()
        before = tree(self.root / 'public')
        good = self.first.read_text()
        bad = {'malformed JSON': good[:-5],
               'future update': json.dumps(dict(json.loads(good), updates=[{'date': '2999-01-01', 'note': 'Private sentinel'}])),
               'invalid update date': json.dumps(dict(json.loads(good), updates=[{'date': '2026-02-30', 'note': 'Private sentinel'}])),
               'invalid reply shape': json.dumps(dict(json.loads(good), agency_response=dict(RECEIVED, status='maybe')))}
        for name, text in bad.items():
            with self.subTest(name):
                self.first.write_text(text)
                with self.assertRaises(ContentError) as caught:
                    self.build()
                self.assertNotIn('Private sentinel', str(caught.exception))
                self.assertEqual(tree(self.root / 'public'), before)


if __name__ == '__main__':
    unittest.main()
