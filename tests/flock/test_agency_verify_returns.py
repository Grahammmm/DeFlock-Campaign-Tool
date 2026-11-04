"""Regression coverage for the five agency verification RETURN findings; no sockets."""
import io
import json
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest import mock

from tests.flock.test_agency_verify import Base, MOD, agency


class Returns(Base):
    def test_robots_on_run_path_and_plain_text_cached(self):
        self.write_seed([agency('ca-one', 'One', 'https://own.example/private/x'),
                         agency('ca-two', 'Two', 'https://own.example/private/y')])
        calls = []
        def open_request(req, timeout):
            calls.append(req.full_url)
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.geturl.return_value = req.full_url
            response.status = 200
            response.headers = {'Content-Type': 'text/plain'}
            response.read.return_value = b'User-agent: *\nDisallow: /private/\nAllow: /public/\n'
            return response
        with mock.patch.object(urllib.request.OpenerDirector, 'open', side_effect=open_request), mock.patch.object(MOD.time, 'sleep'):
            doc = MOD.run_seed(str(self.seed), str(self.out), [], 25)
        self.assertEqual(calls, ['https://own.example/robots.txt'])
        self.assertTrue(all(r['unreachable'][0]['reason'] == 'robots-disallowed' for r in doc['rows']))

    def test_redirect_refused_before_second_transport_request(self):
        class Transport(urllib.request.HTTPSHandler):
            def https_open(inner, req):
                calls.append(req.full_url)
                headers = Message()
                headers['Location'] = target
                return urllib.error.HTTPError(req.full_url, 302, 'Found', headers, io.BytesIO())
        for target, reason in [('https://evil.example/error', 'redirect off host'),
                               ('http://own.example/plain', 'http scheme')]:
            calls = []
            opener = urllib.request.build_opener(MOD.RedirectGuard('own.example'), Transport())
            with mock.patch.object(MOD.urllib.request, 'build_opener', return_value=opener):
                result = MOD.fetch('https://own.example/start')
            self.assertEqual(result[3], reason)
            self.assertEqual(calls, ['https://own.example/start'])

    def test_unknown_official_domain_is_not_inferred_from_link(self):
        a = agency('ca-third', 'Third', 'https://news.example/story')
        row = MOD.evidence_row(a, MOD.privacy().PATTERNS, 'now',
                               lambda u: (u, 200, b'Flock Safety cameras', None))
        self.assertEqual(MOD.decide([row])[0]['verdict'], 'likely')
        schema = json.loads((MOD.REPO / 'schemas/agency-seed.schema.json').read_text())
        props = schema['properties']['counties']['items']['properties']['agencies']['items']['properties']
        self.assertIn('official_domain', props)

    def test_private_windows_rejected_later_clean_window_kept(self):
        for secret in ['123 Synthetic Lane', 'Officer Jane Example']:
            body = (secret + ' manages Flock Safety cameras. ' + '.' * 300 +
                    'The city uses ALPR cameras.').encode()
            row = MOD.evidence_row(agency('ca-test', 'Test', 'https://own.example/x'),
                                   MOD.privacy().PATTERNS, 'now', lambda u: (u, 200, body, None))
            self.assertEqual(len(row['citations']), 1)
            self.assertTrue(row['rejected_quotes'])
            self.assertNotIn(secret, json.dumps(row))
            self.assertIn('ALPR', row['citations'][0]['quote'])

    def test_quote_offset_is_exact_original_utf8_span(self):
        body = 'é\n\t <p>The city\n uses Flock Safety\t cameras.</p>'.encode()
        row = MOD.evidence_row(agency('ca-test', 'Test', 'https://own.example/x'),
                               MOD.privacy().PATTERNS, 'now', lambda u: (u, 200, body, None))
        citation = row['citations'][0]
        quote = citation['quote'].encode()
        self.assertEqual(body[citation['quote_offset']:citation['quote_offset'] + len(quote)], quote)


if __name__ == '__main__':
    unittest.main()
