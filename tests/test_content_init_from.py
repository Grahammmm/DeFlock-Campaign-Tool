"""Synthetic second-locality branding copy; no records or network."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from campaign_tool import content_init

REPO = Path(__file__).resolve().parents[1]


def cli(*args):
    return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', *map(str, args)], cwd=REPO,
                          text=True, capture_output=True, timeout=120)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


def snapshot(root):
    result = {}
    for path in [root, *root.rglob('*')]:
        info = path.lstat()
        value = os.readlink(path) if path.is_symlink() else (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, value)
    return result


class CopyBranding(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='branding-test-')
        self.addCleanup(self.tmp.cleanup)
        self.old, self.new = (Path(self.tmp.name) / x for x in ('old', 'new'))
        for root, name, county, state in ((self.old, 'Cedar', 'Cedar County', 'CA'),
                                          (self.new, 'Pine Watch', 'Pine County', 'OR')):
            result = cli('init', '--directory', root, '--name', name, '--county', county, '--state', state)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.content = self.old / 'content'
        write(self.content / 'site.json', {'schema_version': 1, 'tagline': 'Cedar in Cedar County CA CAMERA',
            'about_md': 'CEDAR COUNTY and Cedar in CA', 'mission_md': 'Cedar County',
            'contact_email': 'old@example.org', 'signup': {'mode': 'brevo_hosted',
                'form_html_allowlisted_url': 'https://example.sibforms.com/old'}, 'base_url': 'https://old.example.org'})

    def run_copy(self, source=None):
        return cli('content', 'init', '--directory', self.new, '--from', source or self.old)

    def assert_refused(self, source=None):
        result = self.run_copy(source)
        self.assertEqual(result.returncode, 1, (result.stdout, result.stderr))
        self.assertIn('Stopped:', result.stderr)
        self.assertFalse(os.path.lexists(self.new / 'content'))
        self.assertEqual(list(self.new.glob('.content.new-*')), [])

    def test_happy_path_with_only_public_allowlist_and_build(self):
        write(self.content / 'labels.json', {'schema_version': 1, 'labels': {'nav.home': 'Cedar CA'}})
        for slug in ('one', 'two'):
            write(self.content / 'pages' / (slug + '.json'), {'title': 'Cedar County', 'body_md': 'Cedar CA CAMERA'})
        write(self.content / 'pages' / 'UPPER.json', {'title': 'do not copy'})
        for name in ('findings', 'meetings', 'map'):
            write(self.content / name / 'record.json', {'not': 'copied'})
        for name in ('sources.json', 'agencies.json'):
            write(self.content / name, [{'not': 'copied'}])
        write(self.old / 'kit' / 'private.json', {'not': 'copied'})
        before = snapshot(self.old)
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout[:result.stdout.rindex('}') + 1])
        self.assertEqual(report['copied'], ['content/site.json', 'content/labels.json',
                                            'content/pages/one.json', 'content/pages/two.json'])
        self.assertTrue({'private', 'kit', 'findings', 'meetings', 'map', 'sources.json',
                         'agencies.json', 'pages/UPPER.json'} <= set(report['skipped']))
        self.assertEqual(report['reset'], ['contact_email', 'signup', 'base_url'])
        self.assertIn('site.json.about_md', report['review'])
        self.assertEqual(report['replaced'], {'name': 5, 'county': 4, 'state': 5})
        dest = self.new / 'content'
        self.assertEqual(sorted(p.name for p in dest.iterdir()), ['labels.json', 'pages', 'site.json'])
        site = json.loads((dest / 'site.json').read_text())
        self.assertEqual(site['tagline'], 'Pine Watch in Pine County OR CAMERA')
        self.assertEqual(site['about_md'], 'CEDAR COUNTY and Pine Watch in OR')
        self.assertIsNone(site['contact_email'])
        self.assertEqual(site['signup'], {'mode': 'none'})
        self.assertNotIn('base_url', site)
        self.assertNotIn('old@example.org', json.dumps(site))
        self.assertIn('content/ written from old.', result.stdout)
        for path in [dest, *dest.rglob('*')]:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(snapshot(self.old), before)
        build = cli('build', '--directory', self.new, '--check')
        self.assertEqual(build.returncode, 0, build.stderr)

    def test_optional_labels_and_pages(self):
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in (self.new / 'content').iterdir()], ['site.json'])

    def test_reverse_overlap_and_state_token_boundaries(self):
        cfg = json.loads((self.old / 'campaign.json').read_text())
        cfg.update(name='Cedar County Watch', county='Cedar')
        write(self.old / 'campaign.json', cfg)
        write(self.content / 'site.json', {'schema_version': 1,
              'tagline': 'Cedar County Watch in Cedar CA CAMERA', 'about_md': 'Cedar County Watch'})
        result = self.run_copy()
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout[:result.stdout.rindex('}') + 1])
        self.assertEqual(report['replaced'], {'name': 2, 'county': 1, 'state': 1})
        site = json.loads((self.new / 'content' / 'site.json').read_text())
        self.assertEqual(site['tagline'], 'Pine Watch in Pine County OR CAMERA')

    def test_destination_inside_source_refused_without_modifying_it(self):
        nested = self.old / 'nested'
        nested.mkdir()
        shutil.copyfile(self.new / 'campaign.json', nested / 'campaign.json')
        before = snapshot(self.old)
        result = cli('content', 'init', '--directory', nested, '--from', self.old)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(snapshot(self.old), before)

    def test_refusals_and_cleanups(self):
        for name in ('same', 'missing_source', 'source_symlink', 'content_symlink', 'page_symlink',
                     'invalid_site', 'oversize_labels', 'invalid_campaign', 'campaign_symlink',
                     'site_symlink', 'bad_labels', 'bad_page', 'pages_symlink'):
            with self.subTest(name=name):
                backup = Path(self.tmp.name) / 'backup'
                shutil.copytree(self.old, backup, symlinks=True)
                source = self.old
                if name == 'same':
                    source = self.new
                elif name == 'missing_source':
                    source = self.old / 'absent'
                elif name == 'source_symlink':
                    source = Path(self.tmp.name) / 'link'
                    source.symlink_to(self.old, target_is_directory=True)
                elif name == 'content_symlink':
                    self.content.rename(self.old / 'other')
                    self.content.symlink_to(self.old / 'other', target_is_directory=True)
                elif name == 'page_symlink':
                    (self.content / 'pages').mkdir()
                    (self.content / 'pages' / 'x.json').symlink_to(self.content / 'site.json')
                elif name == 'pages_symlink':
                    (self.content / 'pages').symlink_to(self.content, target_is_directory=True)
                elif name == 'invalid_site':
                    write(self.content / 'site.json', {'schema_version': 1})
                elif name == 'oversize_labels':
                    (self.content / 'labels.json').write_bytes(b' ' * (256 * 1024 + 1))
                elif name == 'invalid_campaign':
                    write(self.old / 'campaign.json', {'schema_version': 1})
                elif name == 'campaign_symlink':
                    (self.old / 'campaign.json').unlink()
                    (self.old / 'campaign.json').symlink_to(self.new / 'campaign.json')
                elif name == 'site_symlink':
                    (self.content / 'site.json').unlink()
                    (self.content / 'site.json').symlink_to(self.new / 'campaign.json')
                elif name == 'bad_labels':
                    write(self.content / 'labels.json', {'schema_version': 1, 'labels': {'bad': 'value'}})
                elif name == 'bad_page':
                    write(self.content / 'pages' / 'ok.json', {'title': 'missing body'})
                self.assert_refused(source)
                if name == 'source_symlink':
                    source.unlink()
                shutil.rmtree(self.old)
                backup.rename(self.old)

    def test_existing_content_is_never_changed(self):
        write(self.new / 'content' / 'keep.json', {'keep': True})
        before = snapshot(self.new)
        result = self.run_copy()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(snapshot(self.new), before)

    def test_rename_race_and_failure_remove_temporary_tree(self):
        with mock.patch.object(content_init, '_rename_noreplace', side_effect=OSError('injected')):
            with self.assertRaisesRegex(OSError, 'injected'):
                content_init.init_from(self.new, self.old)
        self.assertFalse((self.new / 'content').exists())
        self.assertEqual(list(self.new.glob('.content.new-*')), [])
        def race(source, target):
            target.mkdir()
            raise FileExistsError()
        with mock.patch.object(content_init, '_rename_noreplace', side_effect=race):
            with self.assertRaisesRegex(ValueError, 'already exists'):
                content_init.init_from(self.new, self.old)
        self.assertEqual(list((self.new / 'content').iterdir()), [])
        self.assertEqual(list(self.new.glob('.content.new-*')), [])


if __name__ == '__main__':
    unittest.main()
