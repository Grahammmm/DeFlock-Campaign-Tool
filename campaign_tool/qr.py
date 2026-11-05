"""Offline QR Code (Model 2) encoder, standard library only: byte mode, error correction level M, versions 1-10.

``encode(url)`` returns the module matrix (True = dark, no quiet zone); ``svg(url)`` renders it as inline SVG with a
four-module quiet zone. The URL must satisfy the campaign's ASCII ``https://`` contract and is encoded byte for byte:
nothing is rewritten, shortened, fetched or segment-optimized. Nothing here reads or writes a file.
"""
from .content import ContentError, _https

MAX_BYTES = 213  # version 10, level M, byte mode
_QUIET = 4
# Print layout for the inline SVG: beside the readable link, never split across a page break.
CSS = ' .qr { display: inline-block; vertical-align: middle; margin-left: .6em; break-inside: avoid }'
_M_FORMAT_INDICATOR = 0  # level M is 00
_LENGTH_MESSAGE = f"QR URL exceeds {MAX_BYTES}-byte capacity (version 10, level M)."

# version -> (EC codewords per block, ((block count, data codewords per block), ...)); ISO/IEC 18004 level M.
_BLOCKS = {
    1: (10, ((1, 16),)),
    2: (16, ((1, 28),)),
    3: (26, ((1, 44),)),
    4: (18, ((2, 32),)),
    5: (24, ((2, 43),)),
    6: (16, ((4, 27),)),
    7: (18, ((4, 31),)),
    8: (22, ((2, 38), (2, 39))),
    9: (22, ((3, 36), (2, 37))),
    10: (26, ((4, 43), (1, 44))),
}
_ALIGNMENT = {1: (), 2: (6, 18), 3: (6, 22), 4: (6, 26), 5: (6, 30), 6: (6, 34),
              7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50)}
_REMAINDER_BITS = {1: 0, 2: 7, 3: 7, 4: 7, 5: 7, 6: 7, 7: 0, 8: 0, 9: 0, 10: 0}


class QrError(ValueError):
    """The URL cannot be encoded; the message never repeats the URL."""


def _data_capacity(version):
    return sum(count * size for count, size in _BLOCKS[version][1])


def _byte_capacity(version):
    """Payload bytes that fit: data bits minus the 4-bit mode and the 8- or 16-bit count."""
    return (_data_capacity(version) * 8 - 4 - (8 if version < 10 else 16)) // 8


def _version_for(length):
    for version in range(1, 11):
        if length <= _byte_capacity(version):
            return version
    raise QrError(_LENGTH_MESSAGE)


def _data_codewords(payload, version):
    """Mode 0100, count, payload, terminator, byte alignment and 0xEC/0x11 pads, filling the version's data codewords."""
    capacity_bits = _data_capacity(version) * 8
    count_bits = 8 if version < 10 else 16
    if len(payload) >= 1 << count_bits or 4 + count_bits + 8 * len(payload) > capacity_bits:
        raise QrError(_LENGTH_MESSAGE)
    bits = [0, 1, 0, 0]
    bits += [(len(payload) >> shift) & 1 for shift in range(count_bits - 1, -1, -1)]
    for byte in payload:
        bits += [(byte >> shift) & 1 for shift in range(7, -1, -1)]
    bits += [0] * min(4, capacity_bits - len(bits))
    bits += [0] * (-len(bits) % 8)
    out = bytearray(sum(bit << (7 - n % 8) for n, bit in enumerate(bits[i:i + 8])) for i in range(0, len(bits), 8))
    pad = (0xEC, 0x11)
    while len(out) < capacity_bits // 8:
        out.append(pad[(len(out) - len(bits) // 8) % 2])
    return bytes(out)


def _gf_tables():
    exp, log, value = [0] * 512, [0] * 256, 1
    for n in range(255):
        exp[n], log[value] = value, n
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for n in range(255, 512):
        exp[n] = exp[n - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _rs_remainder(data, degree):
    """Reed-Solomon remainder of ``data`` over GF(256) (polynomial 0x11D) with generator roots alpha^0..alpha^(degree-1)."""
    generator = [1]
    for root in range(degree):
        grown = generator + [0]
        for n, coefficient in enumerate(generator):
            if coefficient:
                grown[n + 1] ^= _EXP[_LOG[coefficient] + root]
        generator = grown
    remainder = [0] * degree
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        if factor:
            for n in range(degree):
                if generator[n + 1]:
                    remainder[n] ^= _EXP[_LOG[generator[n + 1]] + _LOG[factor]]
    return bytes(remainder)


def _codewords(payload, version):
    """Data and EC codewords split per block, then interleaved column by column (data first, then EC)."""
    data = _data_codewords(payload, version)
    ec_length, groups = _BLOCKS[version]
    blocks, start = [], 0
    for count, size in groups:
        for _ in range(count):
            blocks.append(data[start:start + size])
            start += size
    checks = [_rs_remainder(block, ec_length) for block in blocks]
    out = bytearray()
    for column in range(max(len(block) for block in blocks)):
        out += bytes(block[column] for block in blocks if column < len(block))
    for column in range(ec_length):
        out += bytes(check[column] for check in checks)
    return bytes(out)


def _bch(value, generator, shift):
    """``value`` followed by its BCH remainder: ``shift`` check bits for ``generator``."""
    remainder = value
    for _ in range(shift):
        remainder = (remainder << 1) ^ ((remainder >> (generator.bit_length() - 2)) * generator)
    return (value << shift) | remainder


def _format_bits(mask):
    """15 format bits for level M and ``mask`` (BCH generator 0x537, then XOR 0x5412)."""
    return _bch((_M_FORMAT_INDICATOR << 3) | mask, 0x537, 10) ^ 0x5412


def _version_bits(version):
    """18 version-information bits (BCH generator 0x1F25); the standard defines them for versions 7 and up."""
    return _bch(version, 0x1F25, 12)


def _bit(value, index):
    return (value >> index) & 1 == 1


class _Grid:
    def __init__(self, version):
        self.size = 17 + 4 * version
        self.version = version
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.reserved = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()

    def put(self, x, y, dark):
        self.dark[y][x] = dark
        self.reserved[y][x] = True

    def _function_patterns(self):
        n = self.size
        for n_ in range(n):  # timing patterns, overwritten by the finder and alignment drawings that follow
            self.put(6, n_, n_ % 2 == 0)
            self.put(n_, 6, n_ % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):  # finders with their light separators
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.put(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        centres = _ALIGNMENT[self.version]
        for i, cx in enumerate(centres):
            for j, cy in enumerate(centres):
                if (i == 0 and j == 0) or (i == 0 and j == len(centres) - 1) or (i == len(centres) - 1 and j == 0):
                    continue  # these three positions are the finders
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.put(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format(0)  # reserve the format cells; the real bits are written per mask
        if self.version >= 7:
            bits = _version_bits(self.version)
            for i in range(18):
                a, b = n - 11 + i % 3, i // 3
                self.put(a, b, _bit(bits, i))
                self.put(b, a, _bit(bits, i))

    def format(self, mask):
        n, bits = self.size, _format_bits(mask)
        for i in range(6):
            self.put(8, i, _bit(bits, i))
        self.put(8, 7, _bit(bits, 6))
        self.put(8, 8, _bit(bits, 7))
        self.put(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self.put(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self.put(n - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self.put(8, n - 15 + i, _bit(bits, i))
        self.put(8, n - 8, True)  # the always-dark module

    def place(self, codewords):
        """Zigzag placement of data bits and remainder zeros into every unreserved cell."""
        bits = [(byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)]
        bits += [0] * _REMAINDER_BITS[self.version]
        index, n, right = 0, self.size, self.size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for step in range(n):
                for offset in range(2):
                    x = right - offset
                    y = n - 1 - step if ((right + 1) & 2) == 0 else step
                    if not self.reserved[y][x]:
                        self.dark[y][x] = bool(bits[index]) if index < len(bits) else False
                        index += 1
            right -= 2
        if index != len(bits):
            raise AssertionError("codeword placement did not fill the symbol")


_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _lines(matrix):
    n = len(matrix)
    return [list(row) for row in matrix] + [[matrix[y][x] for y in range(n)] for x in range(n)]


def _run_score(line):
    """N1 for one row or column: each run of five or more same-colour modules scores 3 plus one per extra module."""
    score, run = 0, 1
    for i in range(1, len(line) + 1):
        if i < len(line) and line[i] == line[i - 1]:
            run += 1
            continue
        if run >= 5:
            score += 3 + run - 5
        run = 1
    return score


_FINDER_LIKE = ([True, False, True, True, True, False, True, False, False, False, False],
                [False, False, False, False, True, False, True, True, True, False, True])


def _finder_like_score(line):
    """N3 for one row or column: 40 for each 1:1:3:1:1 dark-light-dark pattern with four light modules on one side."""
    return 40 * sum(line[i:i + 11] in _FINDER_LIKE for i in range(len(line) - 10))


def _block_score(matrix):
    """N2: 3 for every 2x2 block of one colour; overlapping blocks are counted separately."""
    n = len(matrix)
    return 3 * sum(matrix[y][x] == matrix[y][x + 1] == matrix[y + 1][x] == matrix[y + 1][x + 1]
                   for y in range(n - 1) for x in range(n - 1))


def _balance_score(matrix):
    """N4: 10 for each full 5% step the dark share is away from 50% (the nearer 5% boundary decides)."""
    dark, total = sum(map(sum, matrix)), len(matrix) ** 2
    low, high = (dark * 20) // total, -(-(dark * 20) // total)
    return 10 * min(abs(low - 10), abs(high - 10))


def _penalty(matrix):
    """ISO/IEC 18004 mask penalty N1+N2+N3+N4 of a complete symbol (quiet zone not included)."""
    lines = _lines(matrix)
    return (sum(map(_run_score, lines)) + _block_score(matrix) + sum(map(_finder_like_score, lines))
            + _balance_score(matrix))


def _check(url):
    if type(url) is not str:
        raise QrError("QR URL must be a string.")
    if len(url) > MAX_BYTES:  # bounded before any validation work or bit buffer
        raise QrError(_LENGTH_MESSAGE)
    try:
        _https(url, "QR URL")
    except ContentError:
        raise QrError("QR URL must be an ASCII https:// URL with a valid host.") from None
    return url.encode("ascii")


def encode(url):
    """Square row-major matrix (True = dark) for ``url``, smallest version 1-10, level M, lowest-penalty mask."""
    payload = _check(url)
    version = _version_for(len(payload))
    codewords = _codewords(payload, version)
    best = (float("inf"), [])
    for mask, condition in enumerate(_MASKS):
        grid = _Grid(version)
        grid.place(codewords)
        n = grid.size
        for y in range(n):
            for x in range(n):
                if not grid.reserved[y][x] and condition(x, y):
                    grid.dark[y][x] = not grid.dark[y][x]
        grid.format(mask)
        score = _penalty(grid.dark)
        if score < best[0]:
            best = (score, grid.dark)
    return tuple(tuple(row) for row in best[1])


def svg(url):
    """Inline SVG of the symbol: white background over the whole viewBox, black module runs, four-module quiet zone."""
    matrix = encode(url)
    n = len(matrix)
    side = n + 2 * _QUIET
    runs = []
    for y, row in enumerate(matrix):
        x = 0
        while x < n:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < n and row[x]:
                x += 1
            runs.append(f"M{start + _QUIET} {y + _QUIET}h{x - start}v1h-{x - start}z")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {side} {side}" width="32mm" height="32mm" '
            f'class="qr" aria-hidden="true" focusable="false" shape-rendering="crispEdges">'
            f'<rect width="{side}" height="{side}" fill="#fff"/><path fill="#000" d="{"".join(runs)}"/></svg>')
