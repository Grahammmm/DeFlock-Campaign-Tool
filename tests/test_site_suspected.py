"""Enabled page/export/counts and independently captured current-main absence tree. Synthetic only."""
import copy
import hashlib
import json
import sys
from decimal import Decimal
from datetime import date
from pathlib import Path

from campaign_tool import law
from campaign_tool.content import ContentError
from campaign_tool.content_suspected import NOTICE
from campaign_tool.site import _plan, _plan_site, build_site
from campaign_tool.site_check import check_accessibility, check_links
from campaign_tool.site_data_catalogue import verify_data_package
from tests.test_content_suspected import Case, DAY, REPO, entry

# Whole planned trees, captured independently from main 67d04d8d, not candidate output.
BASELINE = {
    'map': 'a792d40f476344a1ea91f067bef0ff2420e0e8ee9e578fd6066356044b49d9f2',
    'no-map': '48ab1fe2c4ae0bf266b638757069cf80403604eef4cc97f0bec699fce77a6dc9',
    'no-base': '75d7868936cd71c4bc98b92a57f1b0e06f71d1446399832893048af08c8c5389',
    'labels': '21cbc8ec5034b5b4dac2624ea8bfab0f03abbd31359be186e8753098aa3ac79e',
}
FOOTER = b'<p><a href="/suspected.html">Suspected locations</a></p>'


def validate_export(doc, schema):
    """law's subset plus schema-driven keywords it does not implement; no runtime dependency."""
    law.validate(doc, schema)
    def extra(value, rule):
        if isinstance(value, str):
            if len(value) > rule.get('maxLength', len(value)):
                raise ValueError('maxLength')
            if rule.get('format') == 'date':
                date.fromisoformat(value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > rule.get('maximum', value):
                raise ValueError('maximum')
            if 'multipleOf' in rule and Decimal(str(value)) % Decimal(str(rule['multipleOf'])):
                raise ValueError('multipleOf')
        elif isinstance(value, list):
            if len(value) > rule.get('maxItems', len(value)):
                raise ValueError('maxItems')
            for row in value:
                extra(row, rule.get('items', {}))
        elif isinstance(value, dict):
            for key, item in value.items():
                extra(item, rule.get('properties', {}).get(key, {}))
    extra(doc, schema)


class SiteTests(Case):
    def test_valid_url_and_prefix_render_grouped_page_and_export(self):
        self.put([entry(id='z', agency_id='ca-cedar-county-sheriff', source='cafecafecafe'),
                  entry(id='a', agency_id='ca-cedar-police', source_kind='contract'), entry(id='u')])
        build_site(self.root, today=DAY)
        tree = self.tree(); page = tree['suspected.html'].decode()
        self.assertIn(NOTICE, page)
        self.assertIn('Library &amp; Main', page)
        self.assertIn('/sources/cafecafecafe.html', page)
        self.assertIn('rel="noopener noreferrer"', page)
        self.assertLess(page.index('location-z'), page.index('location-a'))
        self.assertLess(page.index('location-a'), page.index('Agency not specified'))
        self.assertNotIn('href="/agencies/cedar-county-sheriff', page)
        for slug in ('a','u','z'):
            self.assertIn('id="location-' + slug + '"', page)
        doc = json.loads(tree['data/suspected-locations.json'])
        self.assertEqual(set(doc), {'schema_version','generated_from','notice','entries'})
        self.assertEqual([r['id'] for r in doc['entries']], ['a','u','z'])
        self.assertEqual(doc['entries'][0]['latitude'], 35.1235)
        self.assertNotIn(b'35.12345', b''.join(tree.values()))
        self.assertIn(b'/suspected.html', tree['sitemap.xml'])
        self.assertIn('initMap();initSuspectedLayer();', tree['app.js'].decode())
        self.assertIn('id="suspected-toggle" type="checkbox" checked', tree['index.html'].decode())
        self.assertEqual(verify_data_package(self.root / 'public'), [])
        self.assertEqual(check_links(self.root / 'public'), [])
        self.assertEqual(check_accessibility(self.root / 'public'), [])

    def test_suspected_never_changes_camera_counts(self):
        baseline, _, old = _plan(self.root, DAY)
        self.put([entry(latitude=1, longitude=1, agency_id='ca-cedar-police')])
        enabled, _, new = _plan(self.root, DAY)
        for name in ('data/cameras.geojson','data/cameras.csv','data/agencies.json','data/agencies.csv','site-data.js'):
            self.assertEqual(baseline[name], enabled[name], name)
        for name in ('cameras.html', 'agencies.html', *[p[1:] for p in old.agency_pages().values()]):
            self.assertEqual(baseline[name], enabled[name].replace(FOOTER.decode(), ''), name)
        self.assertEqual(old.mapped_counts(), new.mapped_counts())
        self.assertEqual(old.agency_pages(), new.agency_pages())
        self.assertEqual(old.cards_config(), new.cards_config())

    def test_absent_bytes_rerun_removal_and_refusal_preservation(self):
        site = self.content / 'site.json'; original = site.read_bytes()
        for variant, expected in BASELINE.items():
            with self.subTest(variant=variant):
                if variant == 'no-map': (self.content / 'map/map-config.json').rename(self.root / 'saved')
                if variant == 'no-base':
                    doc = json.loads(original); doc.pop('base_url', None); site.write_text(json.dumps(doc))
                if variant == 'labels':
                    (self.content / 'labels.json').write_text(json.dumps({'schema_version':1,'labels':{'footer.data':'Data files'}}))
                files, copies = _plan_site(self.root, DAY)
                tree = {p: (v if isinstance(v, bytes) else v.encode()) for p,v in files.items()}
                tree.update({str(p): src.read_bytes() for src,p,_ in copies})
                digest = hashlib.sha256(json.dumps({p:hashlib.sha256(v).hexdigest() for p,v in sorted(tree.items())}, sort_keys=True).encode()).hexdigest()
                self.assertEqual(digest, expected)
                site.write_bytes(original)
                if variant == 'no-map': (self.root / 'saved').rename(self.content / 'map/map-config.json')
                if variant == 'labels': (self.content / 'labels.json').unlink()
        build_site(self.root, today=DAY); before = self.tree()
        self.put([entry()]); build_site(self.root, today=DAY); present = self.tree()
        build_site(self.root, today=DAY); self.assertEqual(present, self.tree())
        self.put([entry(as_of='2099-01-01')]); self.refused('as_of')
        self.path.unlink(); build_site(self.root, today=DAY); self.assertEqual(before, self.tree())

    def test_empty_no_map_and_no_vendor_fallbacks(self):
        self.put([]); build_site(self.root, today=DAY)
        tree = self.tree()
        self.assertIn(b'No suspected locations are listed.', tree['suspected.html'])
        self.assertNotIn(b'initSuspectedLayer', tree['app.js'])
        self.assertNotIn(b'suspected-toggle', tree['index.html'])
        self.put([entry()]); build_site(self.root, today=DAY)
        self.assertIn(b'/suspected.html', self.tree()['index.html'])  # vendor is absent in the example
        (self.content / 'map/map-config.json').unlink(); build_site(self.root, today=DAY)
        self.assertIn('suspected.html', self.tree())
        self.assertNotIn(b'initSuspectedLayer', self.tree()['app.js'])
        self.assertNotIn(b'suspected-toggle', self.tree()['index.html'])

    def test_collisions_are_conditional_and_preserve_public(self):
        pages = self.content / 'pages'; pages.mkdir(exist_ok=True)
        path = pages / 'suspected.json'
        path.write_text(json.dumps({'title':'Custom predictions','body_md':'Reviewed text.'}))
        build_site(self.root, today=DAY)
        self.put([]); self.refused('collides')
        path.unlink(); build_site(self.root, today=DAY)
        assets = self.content / 'public-assets/suspected.html'; assets.mkdir(parents=True)
        (assets / 'note.ico').write_text('Synthetic')
        with self.assertRaisesRegex(ContentError, 'collides'):
            build_site(self.root, today=DAY)
        (assets / 'note.ico').unlink(); assets.rmdir()
        redirects = self.content / 'redirects.json'
        redirects.write_text(json.dumps([{'from':'/suspected.html','to':'/about.html'}]))
        with self.assertRaises(ContentError):
            build_site(self.root, today=DAY)

    def test_schema_validates_built_bytes_and_rejects_bad_exports(self):
        self.put([entry()]); build_site(self.root, today=DAY)
        doc = json.loads(self.tree()['data/suspected-locations.json'])
        schema = json.loads((REPO / 'schemas/suspected-locations-open-data.schema.json').read_text())
        validate_export(doc, schema)
        for key, value in [('private','secret'), ('latitude',90.0001), ('longitude',-180.0001),
                           ('latitude',0.12345), ('place_label','x'*81), ('source_kind','camera'),
                           ('as_of','2026-02-29'), ('source','/private/original')]:
            with self.subTest(key=key, value=value):
                bad = copy.deepcopy(doc); bad['entries'][0][key] = value
                with self.assertRaises(ValueError): validate_export(bad, schema)
        bad = copy.deepcopy(doc); bad['entries'] *= 201
        with self.assertRaises(ValueError): validate_export(bad, schema)


# VM tests consume an actual feature-enabled integrated app, not a copied controller fixture.
if __name__ == '__main__' and '--fixture-js' in sys.argv:
    import tempfile, shutil
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', root, ignore=shutil.ignore_patterns('public'))
        (root / 'content/suspected-locations.json').write_text(json.dumps({'schema_version':1,'entries':[entry()]}))
        print(_plan_site(root, DAY)[0]['app.js'], end='')
