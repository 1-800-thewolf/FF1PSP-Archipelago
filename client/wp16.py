"""
Wp16 codec (Square Enix PSP LZSS, 16-bit word based). FULLY WORKING both ways.

Decompress verified on MAP_19_01.PCK: exact size (0x104ff0) + 8 clean sub-files.
Compress verified on MAP_00.PCK: round-trip identical, 0x539ce <= 0x54100 slot.

Format (per FireFly's wp16.c / aluigi wp16.bms, ZenHAX, + empirical fixups):
  header: "Wp16"(4) + decompressed_size_bytes u32 LE. Compressed stream @ offset 8.
  stream = repeating blocks: u32 flags (LE) then up to 32 16-bit words.
    flag bits LSB-first; bit==1 -> literal word (emit 2 bytes verbatim).
    bit==0 -> back-ref control word w (LE): d = w >> 5 (distance in WORDS),
              c = (w & 0x1f) + 2 (word count); emit c words copied from output
              history starting d words back. Back-refs that point before the
              start of output emit zero words (output is effectively zero-padded).
  Stop at decompressed_size.

The decompressed blob begins with an internal index:
  u32 ver(8) + u32 total_size + u16 count + entries.
  entry = [id byte on entries>0] name\0, u16, u16, u32 offset, u32 size, u16.
  offset/size locate each sub-file within the decompressed blob.
"""
import struct
from collections import defaultdict


def compress(data: bytes, pad_to: int | None = None) -> bytes:
    """Wp16 compressor (greedy longest-match). Round-trips with decompress().

    Word-granular LZSS: back-ref distance 1..2047 words, length 2..33 words,
    overlapping copies allowed (decoder copies word-at-a-time). Stream ends
    with a 0-distance terminator word; pad_to zero-pads the file to a fixed
    size for in-place ISO replacement (decoder stops at the terminator).

    DO NOT "improve" this parse (measured 2026-07-27, both rejected):
      * deflate-style LAZY matching makes output BIGGER (12330 vs 12314 on
        FM_CAMPUS). Every token here costs the same 2 bytes + 1 flag bit
        whether it is a literal (1 word) or a 33-word match, so the objective
        is fewest TOKENS; deferring a length-L match to gain one word spends an
        extra token, and (L+2)/2 < L for every L > 2.
      * a full optimal parse (backward DP over that uniform cost) emits the
        byte-identical 12314 -- greedy is already optimal on this data -- while
        needing a match search at EVERY position: MAP_00.PCK went 0.4s -> 326s.
    """
    out_size = len(data)
    if out_size & 1:
        data = data + b"\0"                      # decoder stops at out_size
    nw = len(data) // 2
    MAX_DIST, MAX_LEN, MAX_CAND = 0x7FF, 33, 1024

    tokens = []                                  # (is_literal, 2-byte word)
    heads = defaultdict(list)                    # 4-byte key -> word positions

    def best_at(i):
        """Longest match for position i among positions ALREADY indexed
        (strictly < the current index frontier). Returns (length, distance)."""
        if i + 2 > nw:
            return 0, 0
        best_len = best_dist = 0
        cands = heads.get(data[i * 2:i * 2 + 4])
        if not cands:
            return 0, 0
        lo = i - MAX_DIST
        limit = min(MAX_LEN, nw - i)
        for j in reversed(cands[-MAX_CAND:]):
            if j < lo:
                break
            length = 2
            while length < limit and \
                    data[(j + length) * 2:(j + length) * 2 + 2] == \
                    data[(i + length) * 2:(i + length) * 2 + 2]:
                length += 1
            if length > best_len:
                best_len, best_dist = length, i - j
                if length == limit:
                    break
        return best_len, best_dist

    i = 0
    while i < nw:
        best_len, best_dist = best_at(i)
        if best_len >= 2:
            tokens.append((False, struct.pack(
                "<H", (best_dist << 5) | (best_len - 2))))
            end = i + best_len
        else:
            tokens.append((True, data[i * 2:i * 2 + 2]))
            end = i + 1
        while i < end:                           # index every consumed word
            if i + 2 <= nw:
                heads[data[i * 2:i * 2 + 4]].append(i)
            i += 1

    tokens.append((False, b"\0\0"))              # 0-distance terminator
    out = bytearray(b"Wp16" + struct.pack("<I", out_size))
    for k in range(0, len(tokens), 32):
        chunk = tokens[k:k + 32]
        flags = 0
        for bit, (lit, _) in enumerate(chunk):
            if lit:
                flags |= 1 << bit
        out += struct.pack("<I", flags)
        for _, w in chunk:
            out += w
    if pad_to is not None:
        assert len(out) <= pad_to, f"compressed {len(out):#x} > pad_to {pad_to:#x}"
        out += b"\0" * (pad_to - len(out))
    return bytes(out)


def decompress(data: bytes) -> bytes:
    assert data[:4] == b"Wp16", "not a Wp16 stream"
    out_size = struct.unpack_from("<I", data, 4)[0]
    pos, n = 8, len(data)
    out = bytearray()
    while len(out) < out_size and pos + 4 <= n:
        flags = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        for i in range(32):
            if len(out) >= out_size or pos + 2 > n:
                break
            w = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if (flags >> i) & 1:                 # literal word
                out += data[pos - 2:pos]
            else:                                # back-reference
                d = w >> 5
                if d == 0:
                    return bytes(out[:out_size])  # terminator
                c = (w & 0x1f) + 2
                start = len(out) - d * 2
                for _ in range(c):
                    out += out[start:start + 2] if 0 <= start < len(out) else b"\0\0"
                    start += 2
    return bytes(out[:out_size])


def list_subfiles(blob: bytes):
    """Parse the internal index -> list of (name, offset, size).

    Index header = u32 count @0, u32 total_size @4. Entries follow from ~0x10,
    each = (variable padding) name\\0 (variable padding) then a u32 offset + u32
    size pointing into the blob. Padding widths vary, so locate offset/size
    tolerantly: the first valid (offset, size) u32 pair after each name."""
    n = len(blob)
    count = struct.unpack_from("<I", blob, 0)[0]
    subs, p = [], 0x10
    for _ in range(count):
        while p < n and not (0x20 <= blob[p] <= 0x7e):
            p += 1                                # skip id byte / padding
        if p >= n:
            break
        e = blob.index(b"\0", p)
        name = blob[p:e].decode("latin1")
        q = e + 1
        off = size = None
        for k in range(q, min(q + 0x18, n - 8), 2):
            o, s = struct.unpack_from("<II", blob, k)
            if 0 < o < n and 0 < s <= n and o + s <= n:
                off, size, q = o, s, k + 8
                break
        if off is None:
            break
        subs.append((name, off, size))
        p = q
    return subs


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "re_only/MAP_19_01.pck"
    raw = open(path, "rb").read()
    blob = decompress(raw)
    size = struct.unpack_from("<I", raw, 4)[0]
    print(f"{path}: {len(raw):#x} -> {len(blob):#x} (target {size:#x}) "
          f"{'OK' if len(blob) == size else 'SIZE MISMATCH'}")
    for name, off, sz in list_subfiles(blob):
        print(f"  {name:22s} off={off:#09x} size={sz:#08x} magic={blob[off:off+8]}")
