"""DF1407-11: private printable event flyer (offline, synthetic copy of examples/fictional-campaign)."""
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
from datetime import date
from pathlib import Path

from campaign_tool.flyer import build, build_event

REPO = Path(__file__).resolve().parents[1]
BASE = 'https://synthetic.invalid'
TODAY = date(2026, 12, 1)
FIXTURE = {label: {name: ''.join(chunks) for name, chunks in files.items()}
           for label, files in json.loads(
               (REPO / 'tests/fixtures/flyer/campaign-main.json').read_text())['sha256'].items()}
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'


def event(number, **changes):
    document = {'id': f'ev{number}', 'title': f'Forum {number} & "friends"', 'starts_at': '2026-12-05T18:30:00-08:00',
                'ends_at': '2026-12-05T20:00:00-08:00', 'description_md': 'Line **one** & "more"\nLine two \u00e9',
                'venue': 'Cedar Library, Room 2'}
    document.update(changes)
    return document


class EventFlyerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-event-flyer-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root)
        self.content = self.root / 'content'
        shutil.rmtree(self.content / 'findings')
        (self.content / 'findings').mkdir()
        self.site({'base_url': BASE})
        self.events([event(1)])
        self.folder = self.root / 'kit/flyer'

    def site(self, changes):
        path = self.content / 'site.json'
        document = json.loads(path.read_text())
        document.update(changes)
        path.write_text(json.dumps(document), encoding='utf-8')

    def events(self, value):
        (self.content / 'events.json').write_text(json.dumps(value), encoding='utf-8')

    def logo(self, data=SVG, name='mark.svg'):
        assets = self.content / 'public-assets'
        assets.mkdir(exist_ok=True)
        (assets / name).write_bytes(data)
        self.site({'brand': {'logo': name, 'logo_alt': 'Cedar "Watch" & co'}})

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes() if p.is_file() else None, p.lstat().st_mode, p.lstat().st_mtime_ns)
                for p in sorted(self.root.rglob('*'))}

    def cli(self, *extra):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', 'flyer', '--directory', str(self.root), *extra],
                              cwd=REPO, capture_output=True, text=True, timeout=30)

    def make(self, number=1, **kw):
        return build_event(self.root, f'ev{number}', TODAY, **kw)

    def test_fields_in_both_twins(self):
        self.events([event(1, accessibility_note='Step-free entrance & quiet room')])
        report = self.make()
        self.assertEqual(report, {'written': ['kit/flyer/event-ev1.html', 'kit/flyer/event-ev1.txt'], 'event': 'ev1'})
        page = (self.folder / 'event-ev1.html').read_text()
        text = (self.folder / 'event-ev1.txt').read_text()
        anchor = BASE + '/events.html#event-ev1'
        for twin in (page, text):
            self.assertIn('2026-12-05 18:30:00 (UTC-08:00)', twin)
            self.assertIn('2026-12-05 20:00:00 (UTC-08:00)', twin)
            self.assertIn('Cedar Library, Room 2', twin)
            self.assertIn('Accessibility', twin)
            self.assertIn(anchor, twin)
            self.assertNotIn('What we found', twin)
        self.assertIn('Forum 1 &amp; &quot;friends&quot;', page)
        self.assertIn('Line **one** &amp; &quot;more&quot;\nLine two \u00e9', page)
        self.assertIn('Line **one** & "more"\nLine two \u00e9', text)
        self.assertIn(f'<a href="{anchor}">{anchor}</a>', page)
        for mode, path in ((0o700, self.folder), (0o600, self.folder / 'event-ev1.html'), (0o600, self.folder / 'event-ev1.txt')):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

    def test_online_note_absent_and_z_zone_and_relative_anchor(self):
        path = self.content / 'site.json'
        document = json.loads(path.read_text())
        del document['base_url']
        path.write_text(json.dumps(document), encoding='utf-8')
        document = event(1, online=True, starts_at='2026-12-05T18:30:00Z', ends_at='2026-12-05T19:00:00Z')
        del document['venue']
        self.events([document])
        self.make()
        text = (self.folder / 'event-ev1.txt').read_text()
        self.assertIn('Online', text)
        self.assertIn('2026-12-05 18:30:00 UTC to 2026-12-05 19:00:00 UTC', text)
        self.assertNotIn('Accessibility', text)
        self.assertIn('/events.html#event-ev1', text)
        self.assertNotIn('http', text)

    def test_base_url_path_prefix_and_trailing_slash(self):
        self.site({'base_url': BASE + '/prefix/'})
        self.make()
        self.assertIn(BASE + '/prefix/events.html#event-ev1', (self.folder / 'event-ev1.txt').read_text())

    def test_no_scripts_or_external_loads(self):
        self.logo()
        self.make()
        page = (self.folder / 'event-ev1.html').read_text()
        self.assertIn('<img src="event-ev1-logo.svg"', page)
        self.assertNotRegex(page, r'(?i)<script|<iframe|@import|url\(|<link')
        self.assertEqual(re.findall(r'src="([^"]*)"', page), ['event-ev1-logo.svg'])

    def test_cutoff_is_start_based_utc_day(self):
        self.events([event(1, starts_at='2026-12-01T00:00:00Z', ends_at='2026-12-01T01:00:00Z'),
                     event(2, starts_at='2026-11-30T23:59:59Z', ends_at='2026-12-02T01:00:00Z'),
                     event(3, starts_at='2026-12-01T00:00:00+14:00', ends_at='2026-12-01T01:00:00+14:00'),
                     event(4, starts_at='2026-11-30T16:00:00-08:00', ends_at='2026-11-30T17:00:00-08:00')])
        self.make(1)  # exactly midnight UTC
        with self.assertRaisesRegex(ValueError, 'ev2 is past'):  # one second before, even though it is still underway
            self.make(2)
        with self.assertRaisesRegex(ValueError, 'ev3 is past'):  # local Dec 1 but Nov 30 in UTC
            self.make(3)
        self.make(4)  # local Nov 30 but Dec 1 00:00 UTC

    def test_refusals_write_nothing(self):
        for value, pattern in (('nope', 'unknown event nope'), ('ev9', 'unknown event ev9'), ('../x', '--event'), ('', '--event'),
                               ('A', '--event'), ('a\x1b[31m', '--event'), ('a' * 65, '--event'), (5, '--event')):
            with self.assertRaisesRegex(ValueError, pattern if isinstance(value, str) and value else '--event'):
                build_event(self.root, value, TODAY)
        with self.assertRaisesRegex(ValueError, 'must be a date'):
            build_event(self.root, 'ev1', 'today')
        self.events([])
        with self.assertRaisesRegex(ValueError, 'no events'):
            self.make()
        (self.content / 'events.json').unlink()
        with self.assertRaisesRegex(ValueError, 'no events'):
            self.make()
        self.assertFalse((self.root / 'kit').exists())
        self.events([event(1), event(2, title='Bad <title>')])  # a bad unrelated event still refuses
        with self.assertRaisesRegex(ValueError, r'events\.json\[1\]\.title'):
            self.make()
        self.events([event(1, description_md='mail jane.doe@gmail.com now')])
        with self.assertRaises(ValueError) as caught:
            self.make()
        self.assertNotIn('jane.doe', str(caught.exception))
        self.assertFalse((self.root / 'kit').exists())

    def test_cli_mode_exclusive_and_report(self):
        for extra in (('--event', 'ev1', '--max-findings', '3'), ('--event',), ('--event', '')):
            result = self.cli(*extra)
            self.assertNotEqual(result.returncode, 0, extra)
            self.assertNotIn('Private file', result.stdout)
        self.assertFalse((self.root / 'kit').exists())
        result = self.cli('--event', 'nope')
        self.assertEqual((result.returncode, result.stdout), (1, ''))
        self.assertIn('nope', result.stderr)
        result = self.cli('--event', 'ev1')
        if result.returncode == 1:  # the fixed event date is in the past once real time passes it
            self.assertIn('is past', result.stderr)
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(json.loads(lines[0]), {'written': ['kit/flyer/event-ev1.html', 'kit/flyer/event-ev1.txt'], 'event': 'ev1'})
        self.assertEqual(lines[1], 'Private file; nothing was printed, sent or published.')

    def test_cli_event_dispatch_with_far_future_event(self):
        self.events([event(1, starts_at='2999-01-01T10:00:00Z', ends_at='2999-01-01T11:00:00Z')])
        result = self.cli('--event', 'ev1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[0])['event'], 'ev1')
        self.assertIn('Private file', result.stdout)

    def test_existing_refused_without_replace_including_partial_and_orphan_logo(self):
        self.make()
        for victim in ('event-ev1.html', 'event-ev1.txt'):
            before = self.tree()
            with self.assertRaisesRegex(ValueError, 'use --replace'):
                self.make()
            self.assertEqual(self.tree(), before)
            (self.folder / victim).unlink()
            with self.assertRaisesRegex(ValueError, 'use --replace'):
                self.make()
            self.make(replace=True)
        for name in ('event-ev1.html', 'event-ev1.txt'):
            (self.folder / name).unlink()
        (self.folder / 'event-ev1-logo.png').write_bytes(b'x')
        with self.assertRaisesRegex(ValueError, 'event-ev1-logo.png'):
            self.make()

    def test_coexistence_and_replacement_touch_only_selected_event(self):
        self.events([event(1), event(2)])
        self.logo()
        build(self.root)
        campaign = {n: (self.folder / n).read_bytes() for n in ('flyer.html', 'flyer.txt', 'logo.svg')}
        self.make(1)
        self.make(2)
        other = {n: (self.folder / n).read_bytes() for n in ('event-ev2.html', 'event-ev2.txt', 'event-ev2-logo.svg')}
        self.logo(b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"/>', 'new.svg')
        self.make(1, replace=True)
        build(self.root, replace=True)  # campaign replace must not remove event logos
        self.assertEqual({n: (self.folder / n).read_bytes() for n in other}, other)
        self.assertEqual((self.folder / 'event-ev1-logo.svg').read_bytes(), (self.content / 'public-assets/new.svg').read_bytes())
        self.assertEqual((self.folder / 'logo.svg').read_bytes(), (self.content / 'public-assets/new.svg').read_bytes())
        self.assertNotEqual((self.folder / 'logo.svg').read_bytes(), campaign['logo.svg'])
        # removing the brand logo and replacing event 1 removes only its own logo
        self.site({'brand': {}})
        self.make(1, replace=True)
        self.assertFalse((self.folder / 'event-ev1-logo.svg').exists())
        self.assertTrue((self.folder / 'event-ev2-logo.svg').exists())
        self.assertNotIn('<img', (self.folder / 'event-ev1.html').read_text())

    def test_event_id_that_looks_like_another_events_logo_is_not_cleaned(self):
        self.events([event(1), {**event(2), 'id': 'ev1-logo'}])
        self.make(1)
        build_event(self.root, 'ev1-logo', TODAY)
        self.make(1, replace=True)
        self.assertTrue((self.folder / 'event-ev1-logo.html').exists())

    def test_campaign_bytes_unchanged(self):
        for label, base in (('base_on', BASE), ('base_off', None)):
            shutil.rmtree(self.root / 'kit', ignore_errors=True)
            self.site({'base_url': base})
            build(self.root)
            for name, digest in FIXTURE[label].items():
                self.assertEqual(hashlib.sha256((self.folder / name).read_bytes()).hexdigest(), digest, (label, name))
            self.make()
            self.make(replace=True)
            for name, digest in FIXTURE[label].items():
                self.assertEqual(hashlib.sha256((self.folder / name).read_bytes()).hexdigest(), digest, (label, name))

    def test_predictable_temporary_logo_symlink_is_not_followed(self):
        self.logo()
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'keep')
        os.chmod(outside, 0o644)
        self.folder.mkdir(parents=True)
        os.symlink(outside, self.folder / '.event-ev1-logo.tmp')
        report = self.make()
        self.assertIn('kit/flyer/event-ev1-logo.svg', report['written'])
        self.assertEqual(outside.read_bytes(), b'keep')
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        self.assertEqual((self.folder / 'event-ev1-logo.svg').read_bytes(), SVG)
        self.assertEqual(stat.S_IMODE((self.folder / 'event-ev1-logo.svg').stat().st_mode), 0o600)

    def test_bad_logo_symlinks_and_special_targets_refused_untouched(self):
        self.make()
        self.logo(b'not an svg')
        before = self.tree()
        with self.assertRaises(ValueError):
            self.make(replace=True)
        self.assertEqual(self.tree(), before)
        self.site({'brand': {}})
        (self.folder / 'event-ev1.txt').unlink()
        os.symlink('/nonexistent-target', self.folder / 'event-ev1.txt')  # dangling link destination
        before = self.tree()
        with self.assertRaises(ValueError):
            self.make(replace=True)
        self.assertEqual(self.tree(), before)
        (self.folder / 'event-ev1.txt').unlink()
        (self.folder / 'event-ev1.txt').mkdir()
        before = self.tree()
        with self.assertRaises(ValueError):
            self.make(replace=True)
        self.assertEqual(self.tree(), before)
        (self.folder / 'event-ev1.txt').rmdir()
        elsewhere = self.root / 'elsewhere'
        shutil.move(str(self.folder), str(elsewhere))
        os.symlink(elsewhere, self.folder)
        before = self.tree()
        with self.assertRaises(ValueError):
            self.make(replace=True)
        self.assertEqual(self.tree(), before)

    def test_no_network_and_nothing_outside_kit_flyer(self):
        before = {k: v for k, v in self.tree().items() if k.split('/')[0] in ('content', 'public')}
        import socket
        original = socket.socket.connect
        socket.socket.connect = lambda *a, **k: self.fail('network used')
        self.addCleanup(setattr, socket.socket, 'connect', original)
        self.make()
        after = self.tree()
        self.assertEqual({k: v for k, v in after.items() if k.split('/')[0] in ('content', 'public')}, before)
        self.assertEqual(sorted(k for k in after if k.startswith('kit')), ['kit', 'kit/flyer', 'kit/flyer/event-ev1.html', 'kit/flyer/event-ev1.txt'])


if __name__ == '__main__':
    unittest.main()
