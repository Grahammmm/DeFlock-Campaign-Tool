"""RF0600-2: the QR encoder refuses hostname labels longer than 63 octets before any matrix work or private output."""
import traceback
import unittest
from unittest import mock

from campaign_tool import flyer, meeting_pack, qr
from tests import test_qr_ports as ports

MESSAGE = 'QR URL must be an ASCII https:// URL with a valid host.'


def label(n):
    return 'q' * n


class LabelAdmissionTests(unittest.TestCase):
    def test_dns_label_63_accepts_64_refuses_both_encoders(self):
        for n in (62, 63):
            url = f'https://{label(n)}.invalid/a'
            self.assertEqual(qr._check(url), url.encode('ascii'))
            self.assertTrue(qr.encode(url))
            self.assertIn('<svg', qr.svg(url))
        for url in (f'https://{label(64)}.invalid/a', f'https://{label(65)}.invalid/a',
                    f'https://ok.{label(64)}.invalid/a', f'https://ok.invalid.{label(64)}/a',
                    f'https://{label(64)}/a'):
            for function in (qr.encode, qr.svg):
                with self.subTest(url=url, function=function.__name__):
                    with self.assertRaises(qr.QrError) as caught:
                        function(url)
                    self.assertEqual(str(caught.exception), MESSAGE)

    def test_overlong_host_stops_before_matrix_work_without_leaking(self):
        url = f'https://{label(64)}.invalid/pathcanary'
        with mock.patch.object(qr, '_version_for') as version, mock.patch.object(qr, '_codewords') as words, \
                mock.patch.object(qr, '_Grid') as grid, mock.patch.object(qr, '_penalty') as penalty:
            with self.assertRaises(qr.QrError) as caught:
                qr.encode(url)
        for seam in (version, words, grid, penalty):
            seam.assert_not_called()
        shown = ''.join(traceback.format_exception(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)
        for marker in (label(10), 'pathcanary', 'invalid'):
            self.assertNotIn(marker, shown)

    def test_only_hostname_labels_are_bounded(self):
        long = label(64)
        for url in (f'https://x.test/{long}', f'https://x.test/a?q={long}', f'https://x.test/a#{long}',
                    f'https://{label(63)}.test:8443/a?q={long}', 'https://Ab-c.Ex-ample.test/A'):
            with self.subTest(url=url):
                self.assertEqual(qr._check(url), url.encode('ascii'))
                self.assertTrue(qr.encode(url))

    def test_type_capacity_and_port_precedence_remain_identical(self):
        with self.assertRaises(qr.QrError) as caught:
            qr.encode(None)
        self.assertEqual(str(caught.exception), 'QR URL must be a string.')
        with mock.patch.object(qr, 'urlsplit') as parse:
            with self.assertRaises(qr.QrError) as caught:
                qr.encode(f'https://{label(64)}.test/' + 'a' * 300)
        self.assertEqual(str(caught.exception), qr._LENGTH_MESSAGE)
        parse.assert_not_called()
        with self.assertRaises(qr.QrError) as caught:
            qr.encode(f'https://{label(64)}.test:abc/a')
        self.assertEqual(str(caught.exception), MESSAGE)
        for url in ('https://x.test:0/a', 'https://x.test:65535/a'):
            self.assertEqual(qr._check(url), url.encode('ascii'))

    def test_valid_host_boundary_keeps_payload_and_determinism(self):
        url = f'https://{label(63)}.test:443/a'
        seen = []
        real = qr._codewords
        with mock.patch.object(qr, '_codewords', side_effect=lambda payload, version: seen.append(payload) or real(payload, version)):
            first = qr.encode(url)
        self.assertEqual(seen, [url.encode('ascii')])
        self.assertEqual(first, qr.encode(url))
        self.assertEqual(qr.svg(url), qr.svg(url))


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.fx = ports.PrivateConsumerTests('test_real_flyer_and_single_pack_cli_stop_without_traceback')
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.bad_url = f'https://{label(64)}.invalid'

    def test_first_flyer_and_pack_overlong_host_write_nothing(self):
        fx = self.fx
        fx.base(self.bad_url)
        before = fx.tree()
        with self.assertRaises(qr.QrError) as caught:
            flyer.build(fx.root)
        self.assertEqual(str(caught.exception), MESSAGE)
        with self.assertRaises(qr.QrError):
            meeting_pack.build(fx.root, 'm', today=ports.NOW)
        self.assertEqual(before, fx.tree())
        for command, extra in (('flyer', ()), ('meeting-pack', ('--id', 'm'))):
            result = fx.cli(command, *extra)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertNotIn('Traceback', result.stderr)
            self.assertIn('QR URL must be an ASCII https:// URL', result.stderr)
            self.assertEqual(before, fx.tree())
        self.assertFalse((fx.root / 'kit/flyer').exists())
        self.assertFalse((fx.root / 'kit/meeting-packs').exists())

    def test_replacement_refusal_preserves_operator_outputs(self):
        fx = self.fx
        fx.base(f'https://{label(63)}.invalid')  # valid 63-character control succeeds
        flyer.build(fx.root)
        meeting_pack.build(fx.root, 'm', today=ports.NOW)
        (fx.root / 'kit/flyer/flyer.txt').write_text('operator edit')
        (fx.root / 'kit/meeting-packs/m/comment.md').write_text('operator comment')
        fx.base(self.bad_url)
        before = fx.tree()
        with self.assertRaises(qr.QrError):
            flyer.build(fx.root, replace=True)
        with self.assertRaises(qr.QrError):
            meeting_pack.build(fx.root, 'm', today=ports.NOW, replace=True)
        self.assertEqual(before, fx.tree())
        for command, extra in (('flyer', ()), ('meeting-pack', ('--id', 'm'))):
            result = fx.cli(command, '--replace', *extra)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(result.stdout, '')
            self.assertEqual(before, fx.tree())


if __name__ == '__main__':
    unittest.main()
