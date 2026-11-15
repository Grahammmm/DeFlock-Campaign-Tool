"""Document predictions are optional reviewed content, never observed cameras. Synthetic only."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems, run_cli
from campaign_tool.site import build_site

REPO = Path(__file__).resolve().parents[1]
DAY = date(2026, 11, 15)
FILE = 'suspected-locations.json'


def entry(**extra):
    row = dict(id='pole-a', place_label='Library & Main', latitude=35.12345, longitude=-120.12345,
               source_kind='permit', source='https://records.example.test/permit', as_of='2026-11-15')
    row.update(extra)
    return {key: value for key, value in row.items() if value is not ...}


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='suspected-')
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root, ignore=shutil.ignore_patterns('public'))
        self.content = self.root / 'content'
        self.path = self.content / FILE
        patcher = mock.patch('socket.socket', side_effect=AssertionError('network attempted'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, rows):
        self.path.write_text(json.dumps({'schema_version': 1, 'entries': rows}), encoding='utf-8')

    def tree(self):
        return {p.relative_to(self.root / 'public').as_posix(): p.read_bytes()
                for p in (self.root / 'public').rglob('*') if p.is_file()}

    def refused(self, pattern):
        with self.assertRaisesRegex(ContentError, pattern) as caught:
            load_content(self.root, DAY)
        self.assertIn(str(caught.exception), content_problems(self.root, today=DAY))
        before = self.tree()
        with self.assertRaises(ContentError):
            build_site(self.root, today=DAY)
        self.assertEqual(before, self.tree())
        return str(caught.exception)


class ValidationTests(Case):
    def test_valid_is_not_ignored(self):
        self.put([entry()])
        model = load_content(self.root, DAY)
        self.assertIn('suspected_locations', model)
        self.assertEqual(model['suspected_locations'][0]['latitude'], 35.1235)

    def test_source_and_day_refusals_name_fields(self):
        cases = [('source', v) for v in (..., None, '', 'http://example.test/', 'javascript:alert(1)',
                 'https://localhost/x', 'https://127.0.0.1/x', 'https://10.0.0.1/x',
                 'https://192.0.2.1/x', 'https://host.test:bad/x', 'https://host.test:99999/x',
                 'https://224.0.0.1/x', 'https://127.1/x', 'https://0x7f000001/x',
                 'https://user:pass@host.test/x', 'a' * 64, 'abcdefabcdef')]
        cases += [('as_of', v) for v in (..., None, '2026-11-16', '2026-02-29', '2026-1-01', '２０２６-01-01')]
        build_site(self.root, today=DAY)
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.put([entry(**{key: value})])
                self.refused(r'entries\[0\]\.' + key)
        self.put([entry(source=..., as_of='2026-11-16')])
        problems = content_problems(self.root, today=DAY)
        self.assertTrue(any('.source:' in p for p in problems))
        self.assertTrue(any('.as_of:' in p for p in problems))
        for mode in (False, True):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
                run_cli(self.root, as_json=mode)
            self.assertIn('.source', out.getvalue())

    def test_bounds_shape_rounding_and_unique_ids(self):
        from campaign_tool.content_suspected import read_suspected
        for key, values in {'id': ['', 'A', 'a' * 65, None],
                            'place_label': ['', ' ', 'x' * 81, '<b>x</b>', 'a\nb', 'a\u200db', '\ud800'],
                            'latitude': [True, None, '35', 90.00001, float('nan'), float('inf')],
                            'longitude': [-180.00001, 1e300], 'source_kind': ['camera', None],
                            'agency_id': [None, 'unknown']}.items():
            for value in values:
                with self.subTest(key=key, value=repr(value)):
                    self.put([entry(**{key: value})]); self.refused(r'\.' + key)
        self.put([entry(), entry()]); self.refused(r'\.id')
        self.put([entry(private='canary-secret')]); self.assertNotIn('canary-secret', self.refused('unknown'))
        for document in (None, [], {}, {'schema_version': True, 'entries': []},
                         {'schema_version': 1, 'entries': [], 'private': 'canary'}):
            self.path.write_text(json.dumps(document)); self.refused('content/suspected-locations.json')
        rows = [entry(id='pole-' + str(i)) for i in range(200)]
        self.put(rows); self.assertEqual(len(read_suspected(self.root, [], [], DAY)), 200)
        self.put(rows + [entry(id='extra')]); self.refused('entries')
        self.put([entry(latitude=-0.00005, longitude=-0.00001)])
        row = read_suspected(self.root, [], [], DAY)[0]
        self.assertEqual((row['latitude'], row['longitude']), (-0.0001, 0))
        self.assertNotIn('-0.0,', json.dumps(row))

    def test_prefix_unknown_context_and_privacy(self):
        from campaign_tool.content import SOURCES_UNKNOWN
        from campaign_tool.content_suspected import read_suspected
        prefix = 'abcdefabcdef'
        self.put([entry(source=prefix, agency_id='known')])
        source = {'sha256': prefix + '0' * 52}
        self.assertEqual(len(read_suspected(self.root, [{'agency_id': 'known'}], [source], DAY)), 1)
        problems = []
        self.assertIsNone(read_suspected(self.root, None, [source, source], DAY, problems))
        self.assertIn('.source:', problems[0])
        self.assertEqual(len(read_suspected(self.root, None, SOURCES_UNKNOWN, DAY, [])), 1)
        self.put([entry(source='https://localhost', agency_id=None, as_of='2099-01-01')])
        problems = []
        read_suspected(self.root, None, SOURCES_UNKNOWN, DAY, problems)
        self.assertTrue(all(any('.' + key + ':' in p for p in problems) for key in ('source', 'agency_id', 'as_of')))
        for canary in ('secret@example.test', '805-555-1234', '7ABC123', 'a' * 64):
            self.put([entry(place_label=canary)])
            self.assertNotIn(canary, self.refused('place_label'))

    def test_unsafe_or_missing_optional_file(self):
        self.assertNotIn('suspected_locations', load_content(self.root, DAY))
        build_site(self.root, today=DAY)
        for raw in (b'null', b'{', b'\xff', b'{"schema_version":1,"schema_version":1,"entries":[]}', b' ' * (512 * 1024 + 1)):
            self.path.write_bytes(raw); self.refused('content/suspected-locations.json')
        self.path.unlink()
        for kind in ('symlink', 'dangling', 'directory', 'fifo'):
            with self.subTest(kind=kind):
                if kind in ('symlink', 'dangling'): self.path.symlink_to(self.content / ('site.json' if kind == 'symlink' else 'missing'))
                elif kind == 'directory': self.path.mkdir()
                else: os.mkfifo(self.path)
                self.refused('content/suspected-locations.json')
                self.path.rmdir() if kind == 'directory' else self.path.unlink()

    def test_cli_build_and_content_check(self):
        # Read descriptor replacement, read failures and cap-plus-one growth must fail closed.
        from campaign_tool.content_suspected import read_suspected, MAX_BYTES
        self.put([entry()])
        original_open = os.open
        def replaced(path, flags):
            other = self.content / 'replacement.json'
            other.write_bytes(self.path.read_bytes())
            os.replace(other, self.path)
            return original_open(path, flags)
        with mock.patch('campaign_tool.content_suspected.os.open', side_effect=replaced):
            with self.assertRaises(ContentError): read_suspected(self.root, [], [], DAY)
        with mock.patch('campaign_tool.content_suspected.os.open', side_effect=PermissionError):
            with self.assertRaises(ContentError): read_suspected(self.root, [], [], DAY)
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.fileno.return_value = 123
        fake.read.return_value = b' ' * (MAX_BYTES + 1)
        with mock.patch('campaign_tool.content_suspected.os.open', return_value=123), \
                mock.patch('campaign_tool.content_suspected.os.fdopen', return_value=fake), \
                mock.patch('campaign_tool.content_suspected.os.fstat', return_value=self.path.stat()):
            with self.assertRaises(ContentError): read_suspected(self.root, [], [], DAY)
        fake.read.assert_called_once_with(MAX_BYTES + 1)
        self.put([entry(as_of='2026-01-01')])
        for command in ('content-check', 'build'):
            result = subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', command, '--directory', str(self.root)],
                                    cwd=REPO, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.root / 'public/suspected.html').is_file())
        self.put([entry(source=...)])
        for command in ('content-check', 'build'):
            result = subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', command, '--directory', str(self.root)],
                                    cwd=REPO, capture_output=True, text=True, timeout=90)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('.source', result.stdout + result.stderr)
