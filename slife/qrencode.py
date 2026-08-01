"""QR code encoder — pure Python, zero dependencies.

Encodes arbitrary text (UTF-8, byte mode) into a QR matrix, auto-selecting
the smallest version that fits (1–10, M-level ECC).  Renders compact
terminal-scannable ASCII art via ``encode_qr_ascii()``.

Usage::

    from slife.qrencode import encode_qr_ascii
    print(encode_qr_ascii("https://example.com/login?token=abc123"))
"""

from __future__ import annotations

# ═══════════════════════════════════════════════════════════════════════════════
# Galois field GF(256) — primitive polynomial x⁸ + x⁴ + x³ + x² + 1 (0x11D)
# ═══════════════════════════════════════════════════════════════════════════════

_EXP: list[int] = [0] * 512
_LOG: list[int] = [0] * 256


def _init_gf() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _EXP[i + 255] = x  # double-size for convenience
        _LOG[x] = i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D


_init_gf()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


# ═══════════════════════════════════════════════════════════════════════════════
# Reed-Solomon error correction
# ═══════════════════════════════════════════════════════════════════════════════


def _rs_generator(nsym: int) -> list[int]:
    """Return generator polynomial coefficients (highest degree first)."""
    g = [1]
    for i in range(nsym):
        root = _EXP[i]
        # multiply g by (x + root)
        ng = [0] * (len(g) + 1)
        for j, c in enumerate(g):
            ng[j] ^= _gf_mul(c, root)
            ng[j + 1] ^= c
        g = ng
    return g


def _rs_encode(data: list[int], nsym: int) -> list[int]:
    """Return ECC codewords for the given data codewords."""
    gen = _rs_generator(nsym)
    res = [0] * nsym
    for b in data:
        feedback = b ^ res.pop(0)
        res.append(0)
        for i in range(nsym):
            res[i] ^= _gf_mul(gen[i], feedback)
    return res


# ═══════════════════════════════════════════════════════════════════════════════
# QR version tables (versions 1–10, M-level ECC)
# ═══════════════════════════════════════════════════════════════════════════════

# fmt: off
# (total_codewords, ecc_codewords, group1_blocks, group1_data, group2_blocks, group2_data)
_VERSION_ECC: dict[int, tuple[int, int, int, int, int, int]] = {
    1:  (26,  10,  1,  16,  0, 0),
    2:  (44,  16,  1,  28,  0, 0),
    3:  (70,  26,  1,  44,  0, 0),
    4:  (100, 36,  2,  32,  0, 0),
    5:  (134, 48,  2,  43,  0, 0),
    6:  (172, 64,  4,  27,  0, 0),
    7:  (196, 72,  2,  31,  2, 32),
    8:  (242, 88,  2,  38,  2, 39),
    9:  (292, 110, 3,  36,  2, 37),
    10: (346, 130, 4,  43,  1, 44),
}
# fmt: on

# Max byte-mode capacity per version (M-level)
_BYTE_CAP: dict[int, int] = {
    1: 14, 2: 26, 3: 42, 4: 62, 5: 84,
    6: 106, 7: 122, 8: 152, 9: 180, 10: 213,
}

# Alignment pattern locations per version
_ALIGN_POS: dict[int, tuple[int, ...]] = {
    1: (), 2: (18,), 3: (22,), 4: (26,), 5: (30,),
    6: (34,), 7: (6, 22, 38), 8: (6, 24, 42), 9: (6, 26, 46), 10: (6, 28, 50),
}


def _select_version(text: str) -> int:
    """Return the smallest QR version that fits the text (byte-encoded)."""
    length = len(text.encode("utf-8"))
    for v in range(1, 11):
        if length <= _BYTE_CAP[v]:
            return v
    raise ValueError(f"Text too long ({length} bytes) for QR version ≤10")


def _size(version: int) -> int:
    """Module count per side."""
    return 17 + version * 4


# ═══════════════════════════════════════════════════════════════════════════════
# Data encoding — byte mode (handles arbitrary UTF-8 bytes)
# ═══════════════════════════════════════════════════════════════════════════════


def _encode_bytes(text: str) -> list[int]:
    """Encode text as byte-mode bit stream (list of 0/1 ints)."""
    raw = text.encode("utf-8")
    bits: list[int] = []
    for byte in raw:
        for b in range(7, -1, -1):
            bits.append((byte >> b) & 1)
    return bits


# ═══════════════════════════════════════════════════════════════════════════════
# Bit-stream construction
# ═══════════════════════════════════════════════════════════════════════════════


def _build_codewords(text: str, version: int) -> tuple[list[int], int]:
    """Return (data_codewords, total_data_codewords_needed)."""
    total, ecc_cw, *_ = _VERSION_ECC[version]

    # Encode data bits (byte mode)
    data_bits = _encode_bytes(text)

    # Mode indicator: 0100 (byte mode)
    mode = [0, 1, 0, 0]

    # Character count indicator: 8 bits for version 1-9, 16 for 10+
    count_bits = 8 if version <= 9 else 16
    count = len(text.encode("utf-8"))
    count_field = [(count >> b) & 1 for b in range(count_bits - 1, -1, -1)]

    # Required data bits
    data_cw_count = total - ecc_cw
    required_bits = data_cw_count * 8

    # Assemble bit stream
    bit_stream = mode + count_field + data_bits

    # Terminator (up to 4 zero bits)
    term = min(4, required_bits - len(bit_stream))
    bit_stream += [0] * term

    # Pad to byte boundary
    while len(bit_stream) % 8 != 0:
        bit_stream.append(0)

    # Pad bytes (alternating 0xEC, 0x11)
    pad_bytes = [0xEC, 0x11]
    pi = 0
    while len(bit_stream) < required_bits:
        for b in range(7, -1, -1):
            bit_stream.append((pad_bytes[pi] >> b) & 1)
        pi ^= 1

    # Convert to bytes
    codewords = []
    for i in range(0, len(bit_stream), 8):
        byte = 0
        for b in range(8):
            byte = (byte << 1) | bit_stream[i + b]
        codewords.append(byte)

    return codewords, data_cw_count


# ═══════════════════════════════════════════════════════════════════════════════
# Matrix construction
# ═══════════════════════════════════════════════════════════════════════════════


def _init_matrix(size: int) -> list[list[bool | None]]:
    """Create an empty matrix (None = unset)."""
    return [[None] * size for _ in range(size)]


def _place_finder(matrix: list[list[bool | None]], row: int, col: int) -> None:
    """Place a 7×7 finder pattern at (row, col)."""
    for r in range(7):
        for c in range(7):
            if (r == 0 or r == 6 or c == 0 or c == 6 or (2 <= r <= 4 and 2 <= c <= 4)):
                matrix[row + r][col + c] = True
            else:
                matrix[row + r][col + c] = False


def _place_separators(matrix: list[list[bool | None]], size: int) -> None:
    """Place separator rings around finder patterns (False = white)."""
    # Top-left separator
    for r in range(8):
        if r < 8 and 7 < size:
            matrix[r][7] = False
    for c in range(8):
        if 7 < size and c < 8:
            matrix[7][c] = False

    # Top-right separator
    if size - 8 >= 0:
        for r in range(8):
            if size - 8 < size:
                matrix[r][size - 8] = False
        for c in range(size - 7, size):
            if 7 < size:
                matrix[7][c] = False

    # Bottom-left separator
    if size - 8 >= 0:
        for r in range(size - 7, size):
            if size - 8 < size:
                matrix[size - 8][r] = False
        for c in range(8):
            if r < size and c < 8:
                matrix[size - 8][c] = False


def _place_timing(matrix: list[list[bool | None]], size: int) -> None:
    """Place horizontal and vertical timing patterns."""
    for i in range(8, size - 8):
        matrix[6][i] = (i % 2 == 0)
        matrix[i][6] = (i % 2 == 0)


def _place_dark_module(matrix: list[list[bool | None]], size: int) -> None:
    """Always-dark module at (4*version+9, 8)."""
    matrix[size - 8][8] = True


def _place_alignment(matrix: list[list[bool | None]], size: int) -> None:
    """Place alignment patterns."""
    positions = _ALIGN_POS.get(int((size - 17) / 4), ())
    for r in positions:
        for c in positions:
            # Skip if overlaps finder pattern
            if (r < 9 and c < 9) or (r < 9 and c > size - 10) or (r > size - 10 and c < 9):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    if abs(dr) == 2 or abs(dc) == 2 or dr == 0 or dc == 0:
                        if 0 <= r + dr < size and 0 <= c + dc < size:
                            matrix[r + dr][c + dc] = True
                    else:
                        if 0 <= r + dr < size and 0 <= c + dc < size:
                            matrix[r + dr][c + dc] = False


# Format info — 15 bits: 5 data bits + 10 BCH bits, XOR mask 0x5412
_FORMAT_INFO = [
    0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0,
    0x77C4, 0x72F3, 0x7DAA, 0x789D, 0x662F, 0x6318, 0x6C41, 0x6976,
    0x1689, 0x13BE, 0x1CE7, 0x19D0, 0x0762, 0x0255, 0x0D0C, 0x083B,
    0x355F, 0x3068, 0x3F31, 0x3A06, 0x24B4, 0x2183, 0x2EDA, 0x2BED,
]


def _place_format(matrix: list[list[bool | None]], size: int, mask: int) -> None:
    """Place format info around finder patterns and separators."""
    ecc_level = 0  # M = 00
    fi = _FORMAT_INFO[(ecc_level << 3) | mask]
    coords = (
        [(8, i) for i in range(6)] + [(8, i) for i in range(7, 9)]
        + [(i, 8) for i in range(7, -1, -1) if i != 6]
    )
    coords_b = [(size - 1 - i, 8) for i in range(8)] + [(8, size - 1 - i) for i in range(7, -1, -1)]

    for i, (r, c) in enumerate(coords):
        if 0 <= r < size and 0 <= c < size:
            matrix[r][c] = bool((fi >> i) & 1)
    for i, (r, c) in enumerate(coords_b):
        if 0 <= r < size and 0 <= c < size and i < 8:
            matrix[r][c] = bool((fi >> i) & 1)


# ═══════════════════════════════════════════════════════════════════════════════
# Mask patterns
# ═══════════════════════════════════════════════════════════════════════════════

_MASK_FUNCS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: ((r // 2) + (c // 3)) % 2 == 0,
    lambda r, c: ((r * c) % 2) + ((r * c) % 3) == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def _apply_mask(matrix: list[list[bool | None]], size: int, mask_idx: int) -> list[list[bool]]:
    """Return a masked matrix (all bool, no None)."""
    fn = _MASK_FUNCS[mask_idx]
    result: list[list[bool]] = []
    for r in range(size):
        row: list[bool] = []
        for c in range(size):
            v = matrix[r][c] or False  # None → light
            if fn(r, c):
                v = not v
            row.append(v)
        result.append(row)
    return result


def _mask_score(mat: list[list[bool]], size: int) -> int:
    """Evaluate mask pattern penalty score (lower is better)."""
    score = 0

    # Condition 1: 5+ consecutive same-color modules in a row
    for r in range(size):
        run = 0
        last = False
        for c in range(size):
            if mat[r][c] == last:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
                last = mat[r][c]
        if run >= 5:
            score += run - 2

    # Same for columns
    for c in range(size):
        run = 0
        last = False
        for r in range(size):
            if mat[r][c] == last:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run = 1
                last = mat[r][c]
        if run >= 5:
            score += run - 2

    # Condition 2: 2x2 same-color blocks
    for r in range(size - 1):
        for c in range(size - 1):
            if mat[r][c] == mat[r + 1][c] == mat[r][c + 1] == mat[r + 1][c + 1]:
                score += 3

    # Condition 3: 1:1:3:1:1 ratio pattern and its reverse
    for r in range(size):
        row_data = [mat[r][c] for c in range(size)]
        score += _eval_ratio(row_data) * 40
    for c in range(size):
        col_data = [mat[r][c] for r in range(size)]
        score += _eval_ratio(col_data) * 40

    # Condition 4: dark module ratio penalty
    dark = sum(1 for r in range(size) for c in range(size) if mat[r][c])
    total = size * size
    pct = dark * 100 // total
    deviation = abs(pct - 50) // 5
    score += deviation * 10

    return score


def _eval_ratio(line: list[bool]) -> int:
    """Count occurrences of 1:1:3:1:1 ratio (dark/light)."""
    count = 0
    n = len(line)
    # pattern: dark(1) light(1) dark(3) light(1) dark(1) — or inverse
    for i in range(n - 6):
        # Normalize to True=True
        p = [line[i + j] for j in range(7)]
        # look for 1,1,3,1,1 pattern (ignoring color — just check run lengths)
        runs = []
        cur = p[0]
        rlen = 1
        for v in p[1:]:
            if v == cur:
                rlen += 1
            else:
                runs.append(rlen)
                cur = v
                rlen = 1
        runs.append(rlen)
        if runs == [1, 1, 3, 1, 1]:
            count += 1
        elif len(runs) == 5:
            # Also check for the wider pattern with leading/trailing opposite color
            # Check: the first run is preceded by 4+ same-color
            pre_run = 0
            for j in range(i - 1, -1, -1):
                if line[j] == line[i]:
                    pre_run += 1
                else:
                    break
            post_run = 0
            for j in range(i + 7, n):
                if line[j] == line[i + 6]:
                    post_run += 1
                else:
                    break
            if runs == [1, 1, 3, 1, 1]:
                count += 1
    return count


# ═══════════════════════════════════════════════════════════════════════════════
# Module placement (the tricky zigzag pattern)
# ═══════════════════════════════════════════════════════════════════════════════


def _place_data(matrix: list[list[bool | None]], size: int, all_codewords: list[int]) -> None:
    """Fill the QR matrix with data + ECC bits (zigzag pattern, upward)."""
    # Build bit stream
    stream: list[int] = []
    for cw in all_codewords:
        for b in range(7, -1, -1):
            stream.append((cw >> b) & 1)

    # Zigzag: starts at bottom-right, moves upward in columns of 2
    col = size - 1
    bit_idx = 0
    upward = True

    while col > 0:
        if col == 6:  # Skip vertical timing pattern column
            col -= 1
            continue

        c = col
        rows = range(size - 1, -1, -1) if upward else range(size)
        for r in rows:
            for dc in (0, -1):
                cc = c + dc
                if 0 <= cc < size and 0 <= r < size:
                    if matrix[r][cc] is None and bit_idx < len(stream):
                        matrix[r][cc] = bool(stream[bit_idx])
                        bit_idx += 1

        upward = not upward
        col -= 2


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════


def encode_qr(text: str) -> list[list[bool]]:
    """Return a QR code matrix (True=dark, False=light) for the given text.

    Auto-selects version 1–10 with M-level error correction.
    Raises ValueError if text is too long for version 10.
    """
    version = _select_version(text)
    size = _size(version)
    total_cw, ecc_cw, g1b, g1d, g2b, g2d = _VERSION_ECC[version]

    # Encode data
    data_cw, _ = _build_codewords(text, version)

    # Interleave data across blocks
    blocks = []
    offset = 0
    for blk_count, blk_size in [(g1b, g1d), (g2b, g2d)]:
        for _ in range(blk_count):
            blocks.append(data_cw[offset : offset + blk_size])
            offset += blk_size

    # Compute ECC for each block
    ecc_blocks = [_rs_encode(blk, ecc_cw) for blk in blocks]

    # Interleave: all data byte 0, then all data byte 1, ..., then all ECC byte 0, ...
    all_cw: list[int] = []
    max_data = max(len(b) for b in blocks)
    for i in range(max_data):
        for b in blocks:
            if i < len(b):
                all_cw.append(b[i])

    for i in range(ecc_cw):
        for eb in ecc_blocks:
            all_cw.append(eb[i])

    # Pad with remainder codewords to fill capacity
    pad = [0xEC, 0x11]
    pi = 0
    while len(all_cw) < total_cw:
        all_cw.append(pad[pi])
        pi ^= 1

    # Reserve function patterns
    matrix = _init_matrix(size)

    # Place finder patterns (top-left, top-right, bottom-left)
    _place_finder(matrix, 0, 0)
    _place_finder(matrix, 0, size - 7)
    _place_finder(matrix, size - 7, 0)

    # Separators around finders
    _place_separators(matrix, size)

    # Timing patterns
    _place_timing(matrix, size)

    # Dark module
    _place_dark_module(matrix, size)

    # Alignment patterns
    _place_alignment(matrix, size)

    # Place data + ECC bits
    _place_data(matrix, size, all_cw)

    # Try all 8 masks, pick best
    best_score = float("inf")
    best_matrix: list[list[bool]] | None = None

    for mask in range(8):
        # Place format info for this mask
        m2 = [r[:] for r in matrix]  # shallow copy rows
        _place_format(m2, size, mask)

        masked = _apply_mask(m2, size, mask)
        score = _mask_score(masked, size)
        if score < best_score:
            best_score = score
            best_matrix = masked

    assert best_matrix is not None
    return best_matrix


def encode_qr_ascii(text: str) -> str:
    """Return a compact ASCII-art QR code using Unicode half-block chars.

    Produces output suitable for terminal display (~31-49 chars wide).
    Uses █ (full block), ▀ (upper half), ▄ (lower half), and space.
    """
    matrix = encode_qr(text)
    size = len(matrix)

    lines: list[str] = []
    for y in range(0, size, 2):
        row_chars: list[str] = []
        for x in range(size):
            top = matrix[y][x]
            bot = matrix[y + 1][x] if y + 1 < size else False
            if top and bot:
                row_chars.append("█")
            elif top:
                row_chars.append("▀")
            elif bot:
                row_chars.append("▄")
            else:
                row_chars.append(" ")
        lines.append("".join(row_chars))
    return "\n".join(lines)
