"""Mixed-radix IPv4 aliases must not pass suspected-source admission. Synthetic only."""
import contextlib
import io
import json
import subprocess
import sys
import unittest

from campaign_tool.content import ContentError, load_content
from campaign_tool.content_check import content_problems
from campaign_tool.content_suspected import SOURCES_UNKNOWN, _public_source, read_suspected
from campaign_tool.site import build_site
from tests import test_content_suspected as base

ALIASES = ['127.0.0x1', '0177.0.0x1', '192.168.0x1', '169.254.0x1', '0.0.0x0', '224.0.0x1', '8.8.0x808',
           '127.0.0X1', '127.0.0x1.', '8.8.0x808.', '1.0x2', '0x7f.1', '1.2.3.0xFF', '1.0x1.2.3']
GOOD = ['https://records.example.test/permit', 'https://records.0x1.example.test/permit',
        'https://1password.example.test/x', 'https://8.8.8.8/x', 'https://records.example.test/0x1/127.0.0x1?h=127.0.0x1']


class HostTests(base.Case):
    def test_mixed_radix_literal_hosts_refuse(self):
        for host in ['127.0.0.1'] + ALIASES:
            for url in (f'https://{host}/permit', f'https://{host}:8443/permit'):
                with self.subTest(url=url):
                    self.assertFalse(_public_source(url, []))
                    self.assertFalse(_public_source(url, SOURCES_UNKNOWN))
        self.put([base.entry(source='https://127.0.0x1/permit')])
        with self.assertRaisesRegex(ContentError, r'\.source'):
            load_content(self.root, base.DAY)

    def test_numeric_alias_shapes_and_boundaries(self):
        for host in ['0x', '0x0', '1.0xffffffffff', '99999999999999999999.0x1', '.0x1', '0x1..1', 'a.123', 'a.0x1',
                     '1.2.3.4.0x5']:
            with self.subTest(host=host):
                self.assertFalse(_public_source(f'https://{host}/x', []))
        for url in ('https://', 'https:///x', 'https://127.0.0x1:99999/x', 'https://127.0.0x1:bad/x'):
            with self.subTest(url=url):
                self.assertFalse(_public_source(url, []))

    def test_dns_public_ipv4_and_nonhost_numeric_text_still_pass(self):
        for url in GOOD:
            with self.subTest(url=url):
                self.assertTrue(_public_source(url, []))
        self.put([base.entry(source=url) for url in ()] + [base.entry(source=GOOD[4])])
        self.assertEqual(load_content(self.root, base.DAY)['suspected_locations'][0]['source'], GOOD[4])
        self.assertTrue(_public_source('abcdefabcdef', SOURCES_UNKNOWN))
        self.assertFalse(_public_source('', []))
        self.assertFalse(_public_source(None, []))

    def test_strict_diagnostic_and_unknown_context_agree(self):
        secret = 'canary-path-xyz'
        self.put([base.entry(id='a', source=f'https://127.0.0x1/{secret}'),
                  base.entry(id='b', source=f'https://192.168.0x1/{secret}')])
        problems = []
        self.assertIsNone(read_suspected(self.root, None, SOURCES_UNKNOWN, base.DAY, problems))
        text = '\n'.join(problems)
        self.assertIn('.entries[0].source', text)
        self.assertIn('.entries[1].source', text)
        self.assertNotIn(secret, text)
        with self.assertRaises(ContentError) as caught:
            read_suspected(self.root, None, SOURCES_UNKNOWN, base.DAY)
        self.assertNotIn(secret, str(caught.exception))

    def test_real_content_check_and_build_refuse_without_public_changes(self):
        secret = 'canary-path-xyz'
        self.put([base.entry()])
        build_site(self.root, today=base.DAY)
        before = self.tree()
        self.assertIn('suspected.html', before)
        self.put([base.entry(source=f'https://127.0.0x1/{secret}?q=1')])
        with self.assertRaises(ContentError):
            load_content(self.root, base.DAY)
        self.assertTrue(any('.source' in p for p in content_problems(self.root, today=base.DAY)))
        for args in (['content-check'], ['content-check', '--json'], ['build']):
            result = subprocess.run([sys.executable, '-B', '-m', 'campaign_tool', *args, '--directory', str(self.root)],
                                    cwd=base.REPO, capture_output=True, text=True, timeout=90)
            out = result.stdout + result.stderr
            with self.subTest(args=args):
                self.assertNotEqual(result.returncode, 0, out)
                self.assertIn('.source', out)
                self.assertNotIn(secret, out)
                if '--json' in args:
                    self.assertEqual(json.loads(result.stdout)['schema'], 'content-check-v1')
        self.assertEqual(before, self.tree())

    def test_valid_retry_absence_empty_and_no_network(self):
        self.put([base.entry(source='https://127.0.0x1/p')])
        with self.assertRaises(ContentError):
            build_site(self.root, today=base.DAY)
        self.put([base.entry()])
        build_site(self.root, today=base.DAY)
        self.assertIn(b'https://records.example.test/permit', self.tree()['suspected.html'])
        self.put([])
        self.assertEqual(load_content(self.root, base.DAY)['suspected_locations'], [])
        self.path.unlink()
        self.assertIsNone(read_suspected(self.root, [], [], base.DAY))


if __name__ == '__main__':
    unittest.main()
