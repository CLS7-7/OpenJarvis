"""Minimal QR code encoder (byte mode) rendering to an inline SVG string.

Used to show the TOTP provisioning URI during setup without calling any
external QR service and without adding a dependency. Follows ISO/IEC 18004:
byte-mode segments, Reed-Solomon error correction, all 40 versions, and the
standard mask-penalty selection.
"""

from __future__ import annotations

# Index order for error-correction levels: L, M, Q, H.
_ECL_INDEX = {"L": 0, "M": 1, "Q": 2, "H": 3}
_ECL_FORMAT_BITS = {"L": 1, "M": 0, "Q": 3, "H": 2}

_ECC_CODEWORDS_PER_BLOCK = (
    (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
     28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
     26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28),
    (-1, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24, 28, 26, 24, 20, 30, 24, 28, 28, 26, 30,
     28, 30, 30, 30, 30, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
    (-1, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28, 24, 28, 22, 24, 24, 30, 28, 28, 26, 28,
     30, 24, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30),
)

_NUM_ERROR_CORRECTION_BLOCKS = (
    (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
     8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25),
    (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
     17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49),
    (-1, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8, 8, 10, 12, 16, 12, 17, 16, 18, 21, 20,
     23, 23, 25, 27, 29, 34, 34, 35, 38, 40, 43, 45, 48, 51, 53, 56, 59, 62, 65, 68),
    (-1, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8, 11, 11, 16, 16, 18, 16, 19, 21, 25, 25,
     25, 34, 30, 32, 35, 37, 40, 42, 45, 48, 51, 54, 57, 60, 63, 66, 70, 74, 77, 81),
)


def _bit(value: int, index: int) -> bool:
    return (value >> index) & 1 != 0


def _num_raw_data_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        numalign = ver // 7 + 2
        result -= (25 * numalign - 10) * numalign - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver: int, ecl: int) -> int:
    return (_num_raw_data_modules(ver) // 8
            - _ECC_CODEWORDS_PER_BLOCK[ecl][ver] * _NUM_ERROR_CORRECTION_BLOCKS[ecl][ver])


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    numalign = ver // 7 + 2
    step = 26 if ver == 32 else (ver * 4 + numalign * 2 + 1) // (numalign * 2 - 2) * 2
    result = [ver * 4 + 10 - i * step for i in range(numalign - 1)] + [6]
    return list(reversed(result))


class QrCode:
    """A QR code symbol: ``modules[y][x]`` is True for a dark module."""

    def __init__(self, data: bytes, ecl: str = "M", mask: int | None = None) -> None:
        if ecl not in _ECL_INDEX:
            raise ValueError("unknown error correction level")
        self.ecl = ecl
        e = _ECL_INDEX[ecl]
        for ver in range(1, 41):
            cc_bits = 8 if ver <= 9 else 16
            if len(data) < (1 << cc_bits) and 4 + cc_bits + len(data) * 8 <= _num_data_codewords(ver, e) * 8:
                break
        else:
            raise ValueError("data too long for a QR code")
        self.version = ver
        self.size = ver * 4 + 17
        codewords = self._encode_data(data, cc_bits, e)
        allcodewords = self._add_ecc_and_interleave(codewords, e)

        size = self.size
        self.modules = [[False] * size for _ in range(size)]
        self._isfunction = [[False] * size for _ in range(size)]
        self._draw_function_patterns()
        self._draw_codewords(allcodewords)

        if mask is None:
            best_penalty = None
            for m in range(8):
                self._apply_mask(m)
                self._draw_format_bits(m)
                penalty = self._penalty_score()
                if best_penalty is None or penalty < best_penalty:
                    mask, best_penalty = m, penalty
                self._apply_mask(m)  # XOR again to undo
        self.mask = mask
        self._apply_mask(mask)
        self._draw_format_bits(mask)
        del self._isfunction

    # --- data encoding -------------------------------------------------
    def _encode_data(self, data: bytes, cc_bits: int, e: int) -> list[int]:
        bits: list[int] = []

        def append(val: int, n: int) -> None:
            bits.extend((val >> i) & 1 for i in reversed(range(n)))

        append(0x4, 4)
        append(len(data), cc_bits)
        for b in data:
            append(b, 8)
        capacity = _num_data_codewords(self.version, e) * 8
        append(0, min(4, capacity - len(bits)))
        append(0, -len(bits) % 8)
        pad = 0xEC
        while len(bits) < capacity:
            append(pad, 8)
            pad ^= 0xEC ^ 0x11
        return [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    def _add_ecc_and_interleave(self, data: list[int], e: int) -> list[int]:
        ver = self.version
        numblocks = _NUM_ERROR_CORRECTION_BLOCKS[e][ver]
        blockecclen = _ECC_CODEWORDS_PER_BLOCK[e][ver]
        rawcodewords = _num_raw_data_modules(ver) // 8
        numshortblocks = numblocks - rawcodewords % numblocks
        shortblocklen = rawcodewords // numblocks

        blocks = []
        divisor = _rs_divisor(blockecclen)
        k = 0
        for i in range(numblocks):
            dat = data[k:k + shortblocklen - blockecclen + (0 if i < numshortblocks else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, divisor)
            if i < numshortblocks:
                dat = dat + [0]
            blocks.append(dat + ecc)

        result = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != shortblocklen - blockecclen or j >= numshortblocks:
                    result.append(blk[i])
        return result

    # --- module placement ----------------------------------------------
    def _set_function(self, x: int, y: int, dark: bool) -> None:
        self.modules[y][x] = dark
        self._isfunction[y][x] = True

    def _draw_function_patterns(self) -> None:
        size = self.size
        for i in range(size):
            self._set_function(6, i, i % 2 == 0)
            self._set_function(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    xx, yy = cx + dx, cy + dy
                    if 0 <= xx < size and 0 <= yy < size:
                        self._set_function(xx, yy, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(self.version)
        n = len(positions)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set_function(positions[i] + dx, positions[j] + dy,
                                           max(abs(dx), abs(dy)) != 1)
        self._draw_format_bits(0)  # reserve the area; real bits drawn later
        self._draw_version()

    def _draw_format_bits(self, mask: int) -> None:
        data = _ECL_FORMAT_BITS[self.ecl] << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        size = self.size
        for i in range(0, 6):
            self._set_function(8, i, _bit(bits, i))
        self._set_function(8, 7, _bit(bits, 6))
        self._set_function(8, 8, _bit(bits, 7))
        self._set_function(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set_function(14 - i, 8, _bit(bits, i))
        for i in range(0, 8):
            self._set_function(size - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set_function(8, size - 15 + i, _bit(bits, i))
        self._set_function(8, size - 8, True)

    def _draw_version(self) -> None:
        if self.version < 7:
            return
        rem = self.version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = self.version << 12 | rem
        for i in range(18):
            dark = _bit(bits, i)
            a = self.size - 11 + i % 3
            b = i // 3
            self._set_function(a, b, dark)
            self._set_function(b, a, dark)

    def _draw_codewords(self, data: list[int]) -> None:
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = (size - 1 - vert) if upward else vert
                    if not self._isfunction[y][x] and i < len(data) * 8:
                        self.modules[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    def _apply_mask(self, mask: int) -> None:
        masks = (
            lambda x, y: (x + y) % 2,
            lambda x, y: y % 2,
            lambda x, y: x % 3,
            lambda x, y: (x + y) % 3,
            lambda x, y: (x // 3 + y // 2) % 2,
            lambda x, y: x * y % 2 + x * y % 3,
            lambda x, y: (x * y % 2 + x * y % 3) % 2,
            lambda x, y: ((x + y) % 2 + x * y % 3) % 2,
        )
        fn = masks[mask]
        for y in range(self.size):
            row, func = self.modules[y], self._isfunction[y]
            for x in range(self.size):
                if not func[x] and fn(x, y) == 0:
                    row[x] = not row[x]

    def _penalty_score(self) -> int:
        size = self.size
        mods = self.modules
        result = 0
        lines = [mods[y] for y in range(size)] + [[mods[y][x] for y in range(size)] for x in range(size)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = list(reversed(finder_a))
        for line in lines:
            run = 1
            for i in range(1, size + 1):
                if i < size and line[i] == line[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        result += 3 + (run - 5)
                    run = 1
            padded = [False] * 4 + list(line) + [False] * 4
            for i in range(len(padded) - 10):
                window = padded[i:i + 11]
                if window == finder_a or window == finder_b:
                    result += 40
        for y in range(size - 1):
            for x in range(size - 1):
                c = mods[y][x]
                if c == mods[y][x + 1] == mods[y + 1][x] == mods[y + 1][x + 1]:
                    result += 3
        dark = sum(sum(1 for v in row if v) for row in mods)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        result += k * 10
        return result


def svg(text: str, border: int = 4, ecl: str = "M") -> str:
    """Return an inline SVG (string) of a QR code encoding ``text``."""
    qr = QrCode(text.encode("utf-8"), ecl=ecl)
    dim = qr.size + border * 2
    parts = []
    for y, row in enumerate(qr.modules):
        for x, dark in enumerate(row):
            if dark:
                parts.append(f"M{x + border},{y + border}h1v1h-1z")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'class="qr" role="img" aria-label="QR code for authenticator enrollment" '
        f'shape-rendering="crispEdges">'
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>'
        f'<path d="{"".join(parts)}" fill="#000000"/></svg>'
    )
