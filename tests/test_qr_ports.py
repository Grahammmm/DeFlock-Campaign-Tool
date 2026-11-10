"""RF0925-4: the QR encoder refuses unparsable HTTPS authority ports before any matrix work or private output.

Accepted-port digests below were captured from the encoder before the repair, so they are compatibility
evidence for unchanged bytes, not values produced by the modified check.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from campaign_tool import flyer, meeting_pack, qr
from tests.test_flyer import png

REPO = Path(__file__).resolve().parents[1]
GOOD = 'https://synthetic.invalid'
CANARY = 'canary-host.invalid'
MESSAGE = 'QR URL must be an ASCII https:// URL with a valid host.'
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
BAD_PORTS = ('abc', '12x', '65536', '999999', '-1', '+443', '1:2')
SUFFIXES = ('', '/path', '/path?q=1', '/path#frag')
# url -> (matrix size, sha256 prefix of the row-major 0/1 matrix, sha256 prefix of the svg), pre-repair
BASELINE = {
    'https://x.test/a': (25, 'e59709f23906ed42', '1171d7e3edb9d0e0'),
    'https://x.test:/a': (25, '24d15c04ae56a10b', 'c9350dd96d6cfc1b'),
    'https://x.test:0/a': (25, '30f01704971cb1c8', '1861cc772eb3af66'),
    'https://x.test:1/a': (25, 'ec576cb563944ac5', '7f1c4d5aefaba00c'),
    'https://x.test:443/a': (25, '51c9e8e249cf08e2', '5cc85d1ce74b84dd'),
    'https://x.test:65535/a': (25, '27733336066ea1c4', '6c6131fe49d5149e'),
    'https://x.test:00443/a': (25, '2450b99365c58989', '9ab5445c0af27c9d'),
    'https://x.test/a?time=12:99#f:1': (29, '1f329ff116390d9a', '5ba1dc7065459677'),
    'https://x.test:8443/a?time=12:99%3A00': (29, '516ddfaddc33886c', '5a16105717444705'),
}


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def at(days):
    return (NOW + timedelta(days=days)).isoformat()


class PortAdmissionTests(unittest.TestCase):
    def test_invalid_authority_ports_refuse_both_public_encoders(self):
        for port in BAD_PORTS:
            for suffix in SUFFIXES:
                url = f'https://x.test:{port}{suffix}'
                for function in (qr.encode, qr.svg):
                    with self.subTest(url=url, function=function.__name__):
                        with mock.patch.object(qr, '_version_for') as version, mock.patch.object(qr, '_codewords') as words, \
                                mock.patch.object(qr, '_Grid') as grid, mock.patch.object(qr, '_penalty') as penalty:
                            with self.assertRaises(qr.QrError) as caught:
                                function(url)
                        self.assertEqual(str(caught.exception), MESSAGE)
                        for seam in (version, words, grid, penalty):
                            seam.assert_not_called()

    def test_port_errors_are_fixed_value_free_and_unchained(self):
        url = f'https://{CANARY}:portcanary/pathcanary'
        with self.assertRaises(qr.QrError) as caught:
            qr.encode(url)
        shown = ''.join(traceback.format_exception(caught.exception))
        self.assertEqual(str(caught.exception), MESSAGE)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        for marker in ('portcanary', 'pathcanary', CANARY, 'Port could not'):
            self.assertNotIn(marker, shown)
        # a ValueError from the parser seam gets the same fixed message and no marker; other errors are not relabelled
        with mock.patch.object(qr, 'urlsplit', side_effect=ValueError('seam-marker')):
            with self.assertRaises(qr.QrError) as caught:
                qr.encode(f'https://{CANARY}/a')
        self.assertEqual(str(caught.exception), MESSAGE)
        self.assertNotIn('seam-marker', ''.join(traceback.format_exception(caught.exception)))
        with mock.patch.object(qr, '_codewords', side_effect=RuntimeError('programming')):
            with self.assertRaises(RuntimeError):
                qr.encode('https://x.test/a')

    def test_valid_ports_keep_original_bytes_matrix_and_svg(self):
        for url, (size, matrix_digest, svg_digest) in BASELINE.items():
            with self.subTest(url=url):
                self.assertEqual(qr._check(url), url.encode('ascii'))
                matrix = qr.encode(url)
                flat = ''.join('1' if cell else '0' for row in matrix for cell in row)
                self.assertEqual((len(matrix), digest(flat), digest(qr.svg(url))), (size, matrix_digest, svg_digest))
        seen = []
        real = qr._codewords
        with mock.patch.object(qr, '_codewords', side_effect=lambda payload, version: seen.append(payload) or real(payload, version)):
            qr.encode('https://x.test:443/a')
            qr.encode('https://x.test:00443/a')
        self.assertEqual(seen, [b'https://x.test:443/a', b'https://x.test:00443/a'])

    def test_colons_outside_authority_are_not_ports(self):
        for url in ('https://x.test/a?time=12:99', 'https://x.test/a#frag:99', 'https://x.test/a%3Ab',
                    'https://x.test:8443/a?time=12:99#f:abc'):
            with self.subTest(url=url):
                self.assertEqual(qr._check(url), url.encode('ascii'))
        for url in ('http://x.test/a', 'HTTPS://x.test/a', 'https://u:p@x.test/a', 'https://x.test\u00e9/a', 'https://x.test/a\n'):
            with self.subTest(url=url):
                with self.assertRaises(qr.QrError) as caught:
                    qr.encode(url)
                self.assertEqual(str(caught.exception), MESSAGE)

    def test_type_and_capacity_precedence_and_work_bound_stay_intact(self):
        class Sub(str):
            pass
        for value in (Sub('https://x.test/a'), None, b'https://x.test/a'):
            with self.assertRaises(qr.QrError) as caught:
                qr.encode(value)
            self.assertEqual(str(caught.exception), 'QR URL must be a string.')
        long_bad = 'https://x.test:abc/' + 'a' * 300
        with mock.patch.object(qr, 'urlsplit') as parse, mock.patch.object(qr, '_https') as admit:
            with self.assertRaises(qr.QrError) as caught:
                qr.encode(long_bad)
        self.assertEqual(str(caught.exception), qr._LENGTH_MESSAGE)
        parse.assert_not_called()
        admit.assert_not_called()
        prefix = 'https://x.test:443/'
        self.assertEqual(len(qr.encode(prefix + 'a' * (qr.MAX_BYTES - len(prefix)))), 57)
        with self.assertRaises(qr.QrError) as caught:
            qr.encode(prefix + 'a' * (qr.MAX_BYTES - len(prefix) + 1))
        self.assertEqual(str(caught.exception), qr._LENGTH_MESSAGE)


class PrivateConsumerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='synthetic-qr-ports-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'campaign'
        shutil.copytree(REPO / 'examples/fictional-campaign', self.root)
        self.content = self.root / 'content'
        for folder in ('findings', 'meetings'):
            shutil.rmtree(self.content / folder)
            (self.content / folder).mkdir()
        self.site = json.loads((self.content / 'site.json').read_text())
        self.template = json.loads((REPO / 'examples/fictional-campaign/content/findings/cedar-policy-posted.json').read_text())
        self.meeting = {'body': 'Synthetic Council', 'starts_at': '2099-11-02T18:00:00-08:00', 'agenda_item': 'Item 7',
                        'location': 'Synthetic Hall', 'agency_id': 'ca-cedar-police', 'agenda_url': 'https://synthetic.invalid/agenda',
                        'comment_kit_md': 'Hello.\nThank you.\n'}
        self.library = []
        self.base(GOOD)
        self.finding(1)
        self.finding(2)
        self.stage('m')
        (self.root / 'kit/unrelated').mkdir(parents=True)
        (self.root / 'kit/unrelated/sentinel.txt').write_text('sentinel')

    def write(self, relative, value):
        (self.content / relative).write_text(json.dumps(value), encoding='utf-8')

    def base(self, url, **extra):
        self.site = {**self.site, 'base_url': url, **extra}
        self.write('site.json', self.site)

    def stage(self, meeting_id, starts_at='2099-11-02T18:00:00-08:00', **extra):
        self.write(f'meetings/{meeting_id}.json', {**self.meeting, 'id': meeting_id, 'starts_at': starts_at, **extra})

    def finding(self, number):
        sha = hashlib.sha256(f'synthetic {number}'.encode()).hexdigest()
        self.write(f'findings/f{number}.json', {**self.template, 'id': f'f{number}', 'slug': f'finding-{number}',
                                                'title': f'Finding {number}', 'event_date': f'2026-01-{number:02d}', 'corrections': [],
                                                'sources': [{'sha256': sha, 'title': f'Source {number}', 'locator': f'page:{number}'}]})
        self.library.append({'sha256': sha, 'title': f'Source {number}', 'agency_id': 'ca-cedar-police',
                             'received_at': f'2026-02-{number:02d}T12:00:00Z', 'public_url': None, 'fragment': f'source-{number}'})
        self.write('sources.json', self.library)

    def tree(self):
        state = {}
        for path in sorted(self.root.rglob('*')):
            info = path.lstat()
            state[str(path.relative_to(self.root))] = (info.st_mode, info.st_mtime_ns,
                                                       path.read_bytes() if path.is_file() else None)
        return state

    def cli(self, command, *extra):
        return subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', command, '--directory', str(self.root), *extra],
                              cwd=REPO, capture_output=True, text=True, timeout=60)

    def assert_safe_stop(self, result, code=1):
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('nothing was printed', result.stdout)
        for marker in (CANARY, ':abc', 'abc'):
            self.assertNotIn(marker, result.stderr + result.stdout)

    def bad(self):
        self.base(f'https://{CANARY}:abc')

    def test_first_flyer_and_pack_bad_port_write_nothing(self):
        for findings in (True, False):
            if not findings:
                shutil.rmtree(self.content / 'findings')
                (self.content / 'findings').mkdir()
                self.stage('m', agency_id='ca-other-police')
            self.bad()
            before = self.tree()
            with mock.patch('os.chmod') as chmod, mock.patch('shutil.copyfile') as copy:
                with self.assertRaises(qr.QrError) as caught:
                    flyer.build(self.root)
                with self.assertRaises(qr.QrError):
                    meeting_pack.build(self.root, 'm', today=NOW)
            chmod.assert_not_called()
            copy.assert_not_called()
            self.assertEqual(str(caught.exception), MESSAGE)
            self.assertEqual(before, self.tree(), f'findings={findings}')
            self.assertFalse((self.root / 'kit/flyer').exists())
            self.assertFalse((self.root / 'kit/meeting-packs').exists())

    def test_bad_port_replacement_preserves_complete_private_outputs(self):
        self.base(GOOD, brand={'logo': 'mark.png', 'logo_alt': 'Mark'})
        (self.content / 'public-assets').mkdir()
        (self.content / 'public-assets/mark.png').write_bytes(png(120, 40))
        flyer.build(self.root)
        meeting_pack.build(self.root, 'm', today=NOW)
        (self.root / 'kit/flyer/flyer.txt').write_text('operator edit')
        (self.root / 'kit/meeting-packs/m/comment.md').write_text('operator comment')
        self.bad()
        before = self.tree()
        with self.assertRaises(qr.QrError):
            flyer.build(self.root, replace=True)
        with self.assertRaises(qr.QrError):
            meeting_pack.build(self.root, 'm', today=NOW, replace=True)
        self.assertEqual(before, self.tree())
        self.assertTrue(any(name.startswith('kit/flyer/') and 'logo' in name for name in before))
        for command, extra in (('flyer', ()), ('meeting-pack', ('--id', 'm'))):
            result = self.cli(command, '--replace', *extra)
            self.assert_safe_stop(result)
            self.assertEqual(result.stdout, '')
            self.assertIn('Stopped', result.stderr)
            self.assertIn('QR URL must be an ASCII https:// URL', result.stderr)
            self.assertEqual(before, self.tree())

    def test_real_flyer_and_single_pack_cli_stop_without_traceback(self):
        self.bad()
        before = self.tree()
        for command, extra in (('flyer', ()), ('meeting-pack', ('--id', 'm'))):
            result = self.cli(command, *extra)
            self.assert_safe_stop(result)
            self.assertEqual(result.stdout, '')
            self.assertEqual(before, self.tree())
        self.base(GOOD + ':8443')  # valid-port control: normal private reports, notice and modes
        result = self.cli('flyer')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Private file; nothing was printed, sent or published.', result.stdout)
        self.assertEqual(os.stat(self.root / 'kit/flyer/flyer.html').st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.root / 'kit/flyer').st_mode & 0o777, 0o700)
        result = self.cli('meeting-pack', '--id', 'm')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(':8443/', (self.root / 'kit/meeting-packs/m/handout.html').read_text())

    def test_upcoming_bad_base_skips_each_eligible_pack_without_mutation(self):
        for name, days, extra in (('soon', 2, {}), ('quiet', 3, {'agency_id': 'ca-other-police'}),
                                  ('gone', 4, {'status': 'cancelled'}), ('far', 60, {})):
            self.stage(name, at(days), **extra)
        self.bad()
        before = self.tree()
        report = meeting_pack.build_upcoming(self.root, 14, today=NOW)
        self.assertEqual(report['built'], [])
        self.assertEqual(report['skipped'], [{'id': 'soon', 'reason': MESSAGE}, {'id': 'quiet', 'reason': MESSAGE},
                                             {'id': 'gone', 'reason': 'cancelled'}])
        self.assertEqual(before, self.tree())
        result = self.cli('meeting-pack', '--upcoming', '--within-days', '90')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['built'], [])
        self.assertEqual(before, self.tree())
        # existing packs survive --replace under the bad base
        self.base(GOOD)
        built = meeting_pack.build_upcoming(self.root, 14, today=NOW)
        self.assertEqual([item['id'] for item in built['built']], ['soon', 'quiet'])
        self.bad()
        before = self.tree()
        report = meeting_pack.build_upcoming(self.root, 14, today=NOW, replace=True)
        self.assertEqual(report['built'], [])
        self.assertEqual([item['id'] for item in report['skipped']], ['soon', 'quiet', 'gone'])
        self.assertEqual(before, self.tree())


if __name__ == '__main__':
    unittest.main()
