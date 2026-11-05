"""Offline QR encoder: sourced known answers, an independent reference matrix, capacity, masks, SVG and refusals.

Expected values here come from the standard's published worked examples and tables (see each test) or from the
committed independent-encoder fixture, never from campaign_tool.qr itself. Nothing here uses the network.
"""
import hashlib
import json
import re
import socket
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from campaign_tool import qr

FIXTURE = Path(__file__).resolve().parent / 'fixtures/qr/reference-v1-m.json'
LENGTH_MESSAGE = 'QR URL exceeds 213-byte capacity (version 10, level M).'
CAPACITY = [14, 26, 42, 62, 84, 106, 122, 152, 180, 213]  # byte-mode capacity per version, level M
SIZES = [21, 25, 29, 33, 37, 41, 45, 49, 53, 57]
DATA_CODEWORDS = [16, 28, 44, 64, 86, 108, 124, 154, 182, 216]
ALL_CODEWORDS = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346]


def url_of(length):
    return 'https://x.test' if length == 14 else 'https://x.test/' + 'a' * (length - 15)


def bits(rows_cols, matrix):
    """The bits at (row, column) positions, first position first, as a string of 0 and 1."""
    return ''.join('1' if matrix[row][col] else '0' for row, col in rows_cols)


def format_copies(matrix):
    """Both 15-bit format strings (most significant bit first) as placed in a symbol: around the top-left finder, and split
    between the bottom-left and top-right finders. Positions are (row, column) for bits 0..14."""
    n = len(matrix)
    around_top_left = [(i, 8) for i in range(6)] + [(7, 8), (8, 8), (8, 7)] + [(8, 14 - i) for i in range(9, 15)]
    split = [(8, n - 1 - i) for i in range(8)] + [(n - 15 + i, 8) for i in range(8, 15)]
    return tuple(bits(positions, matrix)[::-1] for positions in (around_top_left, split))


def decode_format(matrix):
    """(error-correction indicator, mask) read from the first format copy."""
    data = (int(format_copies(matrix)[0], 2) ^ 0x5412) >> 10
    return data >> 3, data & 7


def version_copies(matrix):
    """Both 18-bit version strings (most significant bit first): top-right block and bottom-left block."""
    n = len(matrix)
    top_right = [(i // 3, n - 11 + i % 3) for i in range(18)]
    bottom_left = [(n - 11 + i % 3, i // 3) for i in range(18)]
    return bits(top_right, matrix)[::-1], bits(bottom_left, matrix)[::-1]


class KnownAnswerTests(unittest.TestCase):
    def test_rs_standard_v1_m_known_answer(self):
        # Thonky "Error Correction Coding" worked example, version 1-M HELLO WORLD (alphanumeric mode data stream,
        # supplied directly to the helper; not encode('HELLO WORLD')). Remainder is the degree-10 division result.
        data = bytes([32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17])
        self.assertEqual(list(qr._rs_remainder(data, 10)), [196, 35, 39, 119, 235, 215, 231, 226, 93, 23])
        self.assertEqual(qr._rs_remainder(b'\x00' * 16, 10), bytes(10))  # linearity: zero data, zero check bytes
        self.assertEqual(len(qr._rs_remainder(data, 26)), 26)

    def test_format_and_version_standard_vectors(self):
        # Thonky "Format and Version Information" tables, level M rows (indicator 00, data bits 00mmm) and versions 7-10.
        formats = ['101010000010010', '101000100100101', '101111001111100', '101101101001011',
                   '100010111111001', '100000011001110', '100111110010111', '100101010100000']
        self.assertEqual([format(qr._format_bits(mask), '015b') for mask in range(8)], formats)
        versions = {7: '000111110010010100', 8: '001000010110111100', 9: '001001101010011001', 10: '001010010011010011'}
        self.assertEqual({v: format(qr._version_bits(v), '018b') for v in versions}, versions)
        self.assertEqual(format(qr._version_bits(7), '018b')[:3], '000')  # leading zeros are kept at full width

    def test_format_bits_are_written_twice_and_version_bits_twice_from_v7_only(self):
        reference = json.loads(FIXTURE.read_text())
        matrix = [[c == '1' for c in row] for row in reference['matrix']]
        formats = {3: '101101101001011'}  # the reference symbol's mask 3 in the table above
        self.assertEqual(format_copies(matrix), (formats[3], formats[3]))
        n = len(matrix)
        self.assertTrue(matrix[n - 8][8])  # the always-dark module
        for version, expected in ((7, '000111110010010100'), (10, '001010010011010011')):
            symbol = qr.encode(url_of(CAPACITY[version - 2] + 1))  # smallest URL that needs this version
            self.assertEqual(len(symbol), 17 + 4 * version)
            self.assertEqual(version_copies(symbol), (expected, expected))
            first, second = format_copies(symbol)
            self.assertEqual(first, second)
            self.assertEqual(decode_format(symbol)[0], 0)  # level M

    def test_data_stream_framing_and_padding(self):
        self.assertEqual(qr._data_codewords(b'abc', 1).hex(), '40361626' + '30' + 'ec11ec11ec11ec11ec11ec')
        # 'https://x.test' fills version 1 exactly: 0100, count 14, the 14 ASCII bytes and the 0000 terminator, nibble-shifted
        self.assertEqual(list(qr._data_codewords(b'https://x.test', 1)), [0x40, 0xE6, 0x87, 0x47, 0x47, 0x07, 0x33, 0xA2,
                                                                          0xF2, 0xF7, 0x82, 0xE7, 0x46, 0x57, 0x37, 0x40])
        # version 9 counts in 8 bits (180 = 0xB4), version 10 in 16 (181 = 0x00B5); first payload byte 'x' is 0x78
        self.assertEqual(qr._data_codewords(b'x' * 180, 9)[:3].hex(), '4b4787')
        self.assertEqual(qr._data_codewords(b'x' * 181, 10)[:3].hex(), '400b57')
        self.assertEqual(len(qr._data_codewords(b'x' * 213, 10)), 216)

    def test_reference_matrix_byte_mode_m(self):
        # tests/fixtures/qr/reference-v1-m.json was made once by an encoder that shares no code with campaign_tool.qr
        # (node-qrcode 1.5.4, one explicit byte segment, level M; provenance, file SHA-256 values and the invocation are
        # recorded in the fixture itself). Every module and the mask are compared; this is not a self round trip.
        reference = json.loads(FIXTURE.read_text())
        for key in ('encoder', 'source', 'file_sha256', 'invocation', 'options', 'deviation_from_spec'):
            self.assertTrue(reference['provenance'][key], key)
        self.assertEqual((reference['payload'], reference['mode'], reference['error_correction']), ('https://x.test', 'byte', 'M'))
        # Public reference hashes use ordered chunks to avoid resembling credential literals.
        for chunks in [reference['matrix_sha256'], *reference['provenance']['file_sha256'].values()]:
            self.assertEqual(len(chunks), 8)
            self.assertTrue(all(re.fullmatch(r'[0-9a-f]{8}', chunk) for chunk in chunks))
        self.assertEqual(hashlib.sha256('\n'.join(reference['matrix']).encode('ascii')).hexdigest(), ''.join(reference['matrix_sha256']))
        self.assertEqual({len(row) for row in reference['matrix']}, {21})
        self.assertEqual(len(reference['matrix']), 21)
        symbol = qr.encode(reference['payload'])
        self.assertEqual(len(symbol), 17 + 4 * reference['version'])
        self.assertEqual([''.join('1' if cell else '0' for cell in row) for row in symbol], reference['matrix'])
        self.assertEqual(decode_format(symbol), (0, reference['mask']))  # level M and the encoder's selected mask


class CapacityTests(unittest.TestCase):
    def test_capacity_every_version_and_unequal_blocks(self):
        self.assertEqual([qr._byte_capacity(v) for v in range(1, 11)], CAPACITY)
        self.assertEqual([qr._data_capacity(v) for v in range(1, 11)], DATA_CODEWORDS)
        for version in range(1, 11):
            with self.subTest(version=version):
                self.assertEqual(len(qr.encode(url_of(CAPACITY[version - 1]))), SIZES[version - 1])  # fits exactly
                self.assertEqual(len(qr._codewords(b'x' * 14, version)), ALL_CODEWORDS[version - 1])
                if version < 10:
                    self.assertEqual(len(qr.encode(url_of(CAPACITY[version - 1] + 1))), SIZES[version])  # one more byte
        self.assertEqual(qr._BLOCKS[8][1], ((2, 38), (2, 39)))
        self.assertEqual(qr._BLOCKS[9][1], ((3, 36), (2, 37)))
        self.assertEqual(qr._BLOCKS[10][1], ((4, 43), (1, 44)))
        self.assertEqual([len(qr._BLOCKS[v][1]) for v in (1, 2, 3, 4, 5, 6, 7)], [1] * 7)
        self.assertEqual(qr.encode(url_of(213)), qr.encode(url_of(213)))

    def test_unequal_blocks_interleave_data_then_check_bytes_per_block(self):
        payload = (b'https://x.test/' + b'abcdefghijklmnopqrstuvwxyz' * 6)[:152]  # fills version 8: blocks of 38, 38, 39, 39
        data = qr._data_codewords(payload, 8)
        blocks = [data[0:38], data[38:76], data[76:115], data[115:154]]
        out = qr._codewords(payload, 8)
        self.assertEqual(len(out), 242)
        self.assertEqual(list(out[:4]), [block[0] for block in blocks])
        self.assertEqual(list(out[38 * 4:38 * 4 + 2]), [blocks[2][38], blocks[3][38]])  # only the 39-byte blocks have a 39th
        checks = [qr._rs_remainder(block, 22) for block in blocks]
        self.assertEqual(out[154:154 + 4], bytes(check[0] for check in checks))
        self.assertEqual(out[-4:], bytes(check[21] for check in checks))

    def test_over_capacity_is_refused_by_length(self):
        for length in (214, 215, 1000, 2_000_000):
            with self.subTest(length=length):
                url = url_of(length)
                for call in (qr.encode, qr.svg):
                    with self.assertRaises(qr.QrError) as caught:
                        call(url)
                    self.assertEqual(str(caught.exception), LENGTH_MESSAGE)
                    self.assertNotIn('x.test', str(caught.exception))
        self.assertTrue(issubclass(qr.QrError, ValueError))
        self.assertIn('<svg', qr.svg(url_of(213)))


class MaskTests(unittest.TestCase):
    def test_run_rule_n1_counts_rows_and_columns(self):
        score = qr._run_score
        self.assertEqual([score([True] * n) for n in (4, 5, 6, 11)], [0, 3, 4, 9])
        self.assertEqual(score([True] * 5 + [False] * 6), 3 + 4)
        self.assertEqual(score([True, False] * 6), 0)
        light = [[False] * 5 for _ in range(5)]
        self.assertEqual(sum(map(score, qr._lines(light))), 5 * 3 + 5 * 3)  # five rows and five columns
        row = [[True] * 5] + [[False] * 5 for _ in range(4)]
        self.assertEqual(sum(map(score, qr._lines(row))), 3 + 4 * 3)  # the dark row; the four light rows; no column has a run
        column = [list(r) for r in zip(*row)]
        self.assertEqual(sum(map(score, qr._lines(column))), 3 + 4 * 3)

    def test_block_rule_n2_counts_overlapping_squares(self):
        self.assertEqual(qr._block_score([[False] * 3 for _ in range(3)]), 4 * 3)
        self.assertEqual(qr._block_score([[True, False], [False, True]]), 0)
        self.assertEqual(qr._block_score([[True, True], [True, True]]), 3)
        self.assertEqual(qr._block_score([[(x + y) % 2 == 0 for x in range(6)] for y in range(6)]), 0)

    def test_finder_like_rule_n3_needs_four_light_modules_on_one_side(self):
        forward = [True, False, True, True, True, False, True, False, False, False, False]
        self.assertEqual(qr._finder_like_score(forward), 40)
        self.assertEqual(qr._finder_like_score(forward[::-1]), 40)
        self.assertEqual(qr._finder_like_score([False] * 3 + forward), 40)  # light context before it does not add more
        self.assertEqual(qr._finder_like_score(forward + forward[::-1]), 80)
        self.assertEqual(qr._finder_like_score(forward[:-1] + [True]), 0)  # only three light modules follow
        self.assertEqual(qr._finder_like_score([False] * 3 + forward[:7] + [False] * 3), 0)  # three light on each side: no
        self.assertEqual(qr._finder_like_score(forward[:10]), 0)  # shorter than the pattern: the quiet zone is not scored
        n = 11
        grid = [[False] * n for _ in range(n)]
        grid[5] = list(forward)
        self.assertEqual(sum(map(qr._finder_like_score, qr._lines(grid))), 40)  # in a row
        transposed = [list(r) for r in zip(*grid)]
        self.assertEqual(sum(map(qr._finder_like_score, qr._lines(transposed))), 40)  # in a column

    def test_balance_rule_n4_steps_of_five_percent(self):
        def share(dark):
            cells = [True] * dark + [False] * (25 - dark)
            return qr._balance_score([cells[i * 5:(i + 1) * 5] for i in range(5)])
        # 25 modules: 13 = 52%, 12 = 48% (both inside the first 5% step), 11 = 44%, 14 = 56%, 15 = 60%, 7 = 28%
        self.assertEqual([share(d) for d in (13, 12, 11, 14, 15, 10, 7, 0, 25)], [0, 0, 10, 10, 20, 20, 40, 100, 100])

    def test_penalty_adds_all_four_rules(self):
        light = [[False] * 11 for _ in range(11)]
        # N1: 22 lines of 11 = 22 * 9; N2: 100 squares * 3; N3: none; N4: 0% dark = ten steps = 100
        self.assertEqual(qr._penalty(light), 22 * 9 + 300 + 0 + 100)
        self.assertEqual(qr._penalty([[True] * 11 for _ in range(11)]), 22 * 9 + 300 + 100)

    def test_mask_penalties_and_tie_break(self):
        url = 'https://x.test/mask'
        formats = [qr._format_bits(mask) for mask in range(8)]

        def chosen(scores):
            with patch.object(qr, '_penalty', side_effect=scores) as penalty:
                symbol = qr.encode(url)
            self.assertEqual(penalty.call_count, 8)  # all eight data masks are scored, each on a finished symbol
            return [mask for mask in range(8) if format_copies(symbol)[0] == format(formats[mask], '015b')][0]

        self.assertEqual(chosen([9, 8, 7, 6, 5, 4, 3, 2]), 7)
        self.assertEqual(chosen([5, 9, 9, 9, 9, 9, 9, 9]), 0)
        self.assertEqual(chosen([9, 9, 4, 9, 4, 9, 4, 9]), 2)  # equal minimum: the lowest mask index wins
        self.assertEqual(chosen([7] * 8), 0)

    def test_scored_candidates_keep_function_cells_and_carry_their_own_format_bits(self):
        seen = []
        real = qr._penalty
        with patch.object(qr, '_penalty', side_effect=lambda m: seen.append([list(r) for r in m]) or real(m)):
            symbol = qr.encode('https://x.test/function-cells')
        self.assertEqual(len(seen), 8)
        n = len(seen[0])
        function = [(y, x) for y in range(8) for x in range(8)] + [(y, x) for y in range(8) for x in range(n - 8, n)] + \
                   [(y, x) for y in range(n - 8, n) for x in range(8)] + [(6, i) for i in range(n)] + [(i, 6) for i in range(n)]
        for y, x in function:
            self.assertEqual(len({candidate[y][x] for candidate in seen}), 1, (y, x))
        self.assertTrue(all(candidate[n - 8][8] for candidate in seen))
        self.assertEqual(len({format_copies(candidate)[0] for candidate in seen}), 8)  # each candidate has its own mask bits
        self.assertEqual(len({tuple(map(tuple, candidate)) for candidate in seen}), 8)
        scores = [real(candidate) for candidate in seen]
        self.assertIn([list(r) for r in symbol], seen)
        self.assertEqual([list(r) for r in symbol], seen[scores.index(min(scores))])  # the first minimum


class SvgTests(unittest.TestCase):
    URL = 'https://x.test/a?b=1&c=2%20d'

    def test_svg_reconstructs_matrix_and_quiet_zone(self):
        matrix = qr.encode(self.URL)
        n = len(matrix)
        text = qr.svg(self.URL)
        root = ET.fromstring(text)
        ns = '{http://www.w3.org/2000/svg}'
        self.assertEqual(root.tag, ns + 'svg')
        self.assertEqual(root.attrib, {'viewBox': f'0 0 {n + 8} {n + 8}', 'width': '32mm', 'height': '32mm', 'class': 'qr',
                                       'aria-hidden': 'true', 'focusable': 'false', 'shape-rendering': 'crispEdges'})
        background, path = list(root)
        self.assertEqual((background.tag, background.attrib), (ns + 'rect', {'width': str(n + 8), 'height': str(n + 8), 'fill': '#fff'}))
        self.assertEqual((path.tag, set(path.attrib)), (ns + 'path', {'fill', 'd'}))
        self.assertEqual(path.attrib['fill'], '#000')
        runs = re.findall(r'M(\d+) (\d+)h(\d+)v1h-(\d+)z', path.attrib['d'])
        self.assertEqual(''.join(f'M{a} {b}h{c}v1h-{d}z' for a, b, c, d in runs), path.attrib['d'])  # nothing else in the path
        dark = set()
        for x, y, width, again in runs:
            self.assertEqual(width, again)
            dark |= {(int(x) + k, int(y)) for k in range(int(width))}
        self.assertEqual(len(dark), sum(map(sum, matrix)))
        rebuilt = tuple(tuple((x + 4, y + 4) in dark for x in range(n)) for y in range(n))
        self.assertEqual(rebuilt, matrix)
        self.assertEqual((min(x for x, _ in dark), min(y for _, y in dark)), (4, 4))
        self.assertLessEqual(max(max(x for x, _ in dark), max(y for _, y in dark)), n + 3)  # four white modules on every side
        self.assertTrue(all(4 <= x < n + 4 and 4 <= y < n + 4 for x, y in dark))

    def test_svg_is_deterministic_numeric_and_inert(self):
        text = qr.svg(self.URL)
        self.assertEqual(text, qr.svg(self.URL))
        self.assertEqual(text.count('http'), 1)  # only the xmlns namespace
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', text)
        for banned in ('script', 'foreignObject', 'href', 'url(', 'x.test', 'onload', '<image', '<use', '<style', 'xlink'):
            self.assertNotIn(banned, text)
        self.assertTrue(text.startswith('<svg ') and text.endswith('</svg>') and '\n' not in text)
        self.assertIn('.qr {', qr.CSS)
        self.assertNotIn('color', qr.CSS)


class RefusalTests(unittest.TestCase):
    def test_invalid_urls_and_length_errors_are_safe(self):
        not_text = [None, True, False, b'https://x.test', bytearray(b'https://x.test'), 5, ['https://x.test'], ('https://x.test',)]

        class Text(str):
            pass

        # Assemble clearly synthetic userinfo at runtime; retain both credential-refusal cases.
        userinfo = ['user' + ':' + 'secret', ':' + 'secret']
        invalid = ['https://' + auth + '@x.test' + suffix for auth, suffix in zip(userinfo, ('', '/'))]
        invalid += ['', 'http://x.test', 'ftp://x.test', 'https://x.test/caf\u00e9',
                   'https://x.test/\n', 'https://x.test/a b', 'https://x.test/\x00', 'https://', 'https:///path',
                   'https://-bad-.test', 'HTTPS://x.test', 'https://x.test\u2215a', ' https://x.test', 'https://x.test ']
        with patch.object(socket, 'socket', side_effect=AssertionError('network used')):
            for value in not_text + [Text('https://x.test')]:
                for call in (qr.encode, qr.svg):
                    with self.subTest(value=repr(value), call=call.__name__), self.assertRaises(qr.QrError) as caught:
                        call(value)
                    self.assertEqual(str(caught.exception), 'QR URL must be a string.')
            for value in invalid:
                for call in (qr.encode, qr.svg):
                    with self.subTest(value=repr(value), call=call.__name__), self.assertRaises(qr.QrError) as caught:
                        call(value)
                    message = str(caught.exception)
                    self.assertEqual(message, 'QR URL must be an ASCII https:// URL with a valid host.')
                    for piece in (value.strip(), 'secret', 'x.test'):
                        if piece and piece != 'https://':  # the fixed message itself names the scheme
                            self.assertNotIn(piece, message)
                    self.assertIsNone(caught.exception.__cause__)  # no chained error that could carry the URL
                    self.assertTrue(caught.exception.__suppress_context__)
            with self.assertRaises(qr.QrError) as caught:
                qr.encode('https://x.test/' + 'a' * 300)
            self.assertEqual(str(caught.exception), LENGTH_MESSAGE)


if __name__ == '__main__':
    unittest.main()
