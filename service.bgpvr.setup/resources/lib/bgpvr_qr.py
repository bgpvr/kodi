# -*- coding: utf-8 -*-
# A QR code (ISO/IEC 18004, byte mode) and a greyscale PNG with the standard library only (Kodi add-ons take no pip).
# shortcut: versions 1-6 only (up to 106 bytes at level M, 134 at L; a sign-in address is ~50): longer text -> None,
# the screen then shows the code and the address without a picture. Versions 7+ need the version-info blocks.
import struct
import zlib

# version -> level -> (EC codewords per block, data codewords per block)
_BLOCKS = {
    1: {"M": (10, [16]), "L": (7, [19])},
    2: {"M": (16, [28]), "L": (10, [34])},
    3: {"M": (26, [44]), "L": (15, [55])},
    4: {"M": (18, [32, 32]), "L": (20, [80])},
    5: {"M": (24, [43, 43]), "L": (26, [108])},
    6: {"M": (16, [27, 27, 27, 27]), "L": (18, [68, 68])},
}
_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_LEVEL_BITS = {"L": 1, "M": 0}

_EXP, _LOG = [0] * 512, [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i], _LOG[_x] = _x, _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _ec(data, n):
    """Reed-Solomon remainder: n EC codewords for one block."""
    gen = [1]
    for i in range(n):
        gen = [(gen[j] if j < len(gen) else 0) ^ (_mul(gen[j - 1], _EXP[i]) if j else 0) for j in range(len(gen) + 1)]
    rem = [0] * n
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        for j in range(n):
            rem[j] ^= _mul(gen[j + 1], f)
    return rem


def _codewords(data, version, level):
    ec_n, blocks = _BLOCKS[version][level]
    cap = sum(blocks)
    bits = [0, 1, 0, 0] + [(len(data) >> i) & 1 for i in range(7, -1, -1)]
    for b in data:
        bits += [(b >> i) & 1 for i in range(7, -1, -1)]
    bits += [0] * min(4, cap * 8 - len(bits))
    bits += [0] * (-len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    words += [(0xEC, 0x11)[i % 2] for i in range(cap - len(words))]
    split, at = [], 0
    for n in blocks:
        split.append(words[at:at + n])
        at += n
    ecs = [_ec(b, ec_n) for b in split]
    out = [b[i] for i in range(max(blocks)) for b in split if i < len(b)]
    return out + [e[i] for i in range(ec_n) for e in ecs]


def _mask_bit(mask, r, c):
    return (((r + c) % 2 == 0), (r % 2 == 0), (c % 3 == 0), ((r + c) % 3 == 0), ((r // 2 + c // 3) % 2 == 0),
            ((r * c) % 2 + (r * c) % 3 == 0), (((r * c) % 2 + (r * c) % 3) % 2 == 0),
            (((r + c) % 2 + (r * c) % 3) % 2 == 0))[mask]


def _build(words, version, level, mask):
    size = 17 + 4 * version
    m = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def put(r, c, dark):
        m[r][c], fn[r][c] = dark, True

    for r0, c0 in ((0, 0), (0, size - 7), (size - 7, 0)):          # finders + their white separators
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    put(r, c, 0 <= dr <= 6 and 0 <= dc <= 6 and (dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4)))
    for i in range(8, size - 8):                                     # timing
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    pos = _ALIGN[version]
    for r0 in pos:
        for c0 in pos:
            if (r0, c0) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r0 + dr, c0 + dc, max(abs(dr), abs(dc)) != 1)
    data = (_LEVEL_BITS[level] << 3) | mask                         # format bits (BCH 15,5), both copies
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    fmt = ((data << 10) | rem) ^ 0x5412
    bit = lambda i: (fmt >> i) & 1 == 1  # noqa: E731
    for i in range(6):
        put(i, 8, bit(i))
    put(7, 8, bit(6))
    put(8, 8, bit(7))
    put(8, 7, bit(8))
    for i in range(9, 15):
        put(8, 14 - i, bit(i))
    for i in range(8):
        put(8, size - 1 - i, bit(i))
    for i in range(8, 15):
        put(size - 15 + i, 8, bit(i))
    put(size - 8, 8, True)                                           # the dark module
    i, n, col, up = 0, len(words) * 8, size - 1, True               # data, zig-zag from the bottom right
    while col > 0:
        if col == 6:
            col -= 1
        for k in range(size):
            r = size - 1 - k if up else k
            for c in (col, col - 1):
                if not fn[r][c]:
                    dark = i < n and (words[i // 8] >> (7 - i % 8)) & 1 == 1
                    m[r][c] = dark != _mask_bit(mask, r, c)
                    i += 1
        up = not up
        col -= 2
    return m


def _penalty(m):
    size, score = len(m), 0
    lines = m + [list(col) for col in zip(*m)]
    finder = ([1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0], [0, 0, 0, 0, 1, 0, 1, 1, 1, 0, 1])
    for line in lines:
        run = 1
        for a, b in zip(line, line[1:] + [None]):
            if a == b:
                run += 1
                continue
            score += run - 2 if run >= 5 else 0
            run = 1
        ints = [int(v) for v in line]
        score += 40 * sum(ints[i:i + 11] in finder for i in range(size - 10))
    score += 3 * sum(m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]
                     for r in range(size - 1) for c in range(size - 1))
    dark = sum(map(sum, m))
    return score + 10 * int(abs(dark * 100 / (size * size) - 50) // 5)


def encode(text, mask=None):
    """The QR matrix (rows of booleans, True = dark) for text, the smallest version 1-6 at level M (else L), or None
    when it does not fit. mask None = the lowest-penalty mask (the standard's choice)."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    for level in ("M", "L"):
        for version in range(1, 7):
            if len(data) <= sum(_BLOCKS[version][level][1]) - 2:
                words = _codewords(data, version, level)
                masks = range(8) if mask is None else [mask]
                return min((_build(words, version, level, k) for k in masks), key=_penalty)
    return None


def png(matrix, scale=10, border=4):
    """8-bit greyscale PNG bytes of the matrix, `border` light modules round it, each module scale x scale pixels."""
    n = len(matrix)
    side = (n + 2 * border) * scale
    raw = []
    for r in range(-border, n + border):
        cells = [0 if 0 <= r < n and 0 <= c < n and matrix[r][c] else 255 for c in range(-border, n + border)]
        line = b"\x00" + bytes(v for v in cells for _ in range(scale))
        raw += [line] * scale

    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(raw), 9)) + chunk(b"IEND", b""))
