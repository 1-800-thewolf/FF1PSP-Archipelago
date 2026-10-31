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
    return _emit(len(data), _tokens(data), pad_to)


def _tokens(data):
    """Greedy parse -> [(is_literal, 2-byte word)], terminator included.

    For each word: among the newest MAX_CAND earlier positions that start with
    the same two words (and lie within MAX_DIST), take the longest match; on a
    tie the newest wins. The output is pinned byte-for-byte to the original
    byte-slice parse (test_wp16_parse.py keeps that parse as its oracle) --
    compress_inplace and every baked slot depend on it.

    8.8x faster than that parse (2026-10-30; MAP_00.PCK 13.2s -> 1.2s): the
    words are ints with int keys instead of byte slices, the candidates are
    walked by index instead of copying cands[-MAX_CAND:] at every word, and a
    candidate that misses at word best_len is skipped -- it cannot beat
    best_len, so skipping it never changes the pick."""
    if len(data) & 1:
        data = data + b"\0"                      # decoder stops at out_size
    nw = len(data) // 2
    MAX_DIST, MAX_LEN, MAX_CAND = 0x7FF, 33, 1024
    w = struct.unpack(f"<{nw}H", data)           # the words, as ints
    pack = struct.Struct("<H").pack

    tokens = []                                  # (is_literal, 2-byte word)
    heads = {}                                   # w[i] | w[i+1] << 16 -> positions

    i = 0
    while i < nw:
        best_len = best_dist = 0
        if i + 2 <= nw:
            cands = heads.get(w[i] | (w[i + 1] << 16))
            if cands:
                lo = i - MAX_DIST
                limit = min(MAX_LEN, nw - i)
                k = len(cands) - 1
                stop = max(k - MAX_CAND, -1)     # newest MAX_CAND only
                while k > stop:
                    j = cands[k]
                    k -= 1
                    if j < lo:
                        break
                    if best_len and w[j + best_len] != w[i + best_len]:
                        continue
                    length = 2
                    while length < limit and w[j + length] == w[i + length]:
                        length += 1
                    if length > best_len:
                        best_len, best_dist = length, i - j
                        if length == limit:
                            break
        if best_len >= 2:
            tokens.append((False, pack((best_dist << 5) | (best_len - 2))))
            end = i + best_len
        else:
            tokens.append((True, data[i * 2:i * 2 + 2]))
            end = i + 1
        while i < end:                           # index every consumed word
            if i + 2 <= nw:
                key = w[i] | (w[i + 1] << 16)
                lst = heads.get(key)
                if lst is None:
                    heads[key] = [i]
                else:
                    lst.append(i)
            i += 1

    tokens.append((False, b"\0\0"))              # 0-distance terminator
    return tokens


def _emit(out_size, tokens, pad_to=None):
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


def compress_inplace(data: bytes, max_lead: int) -> bytes:
    """compress() reshaped so inplace_lead(result) <= max_lead: the greedy
    parse with its first tokens spelled out as literals. The literal prefix
    reads ahead of the write pointer, the tail keeps its matches (they copy
    from the same output history), so this is vanilla's shape -- a stream
    that fills its slot instead of a short one that outruns its own input.
    The result is about len(data) - max_lead bytes; the caller checks it fits."""
    out_size, tokens = len(data), _tokens(data)
    span = [1 if lit else (struct.unpack("<H", w)[0] & 0x1f) + 2 for lit, w in tokens]
    even = data + b"\0" if len(data) & 1 else data

    def build(k):                       # tokens[:k] -> literals
        nwords = sum(span[:k])
        lits = [(True, even[i * 2:i * 2 + 2]) for i in range(nwords)]
        return _emit(out_size, lits + tokens[k:])

    lo, hi = 0, len(tokens) - 1         # the terminator stays a terminator
    if inplace_lead(build(lo)) <= max_lead:
        return build(lo)
    if inplace_lead(build(hi)) > max_lead:
        raise ValueError(f"wp16: no literal prefix brings the lead under {max_lead} B")
    while hi - lo > 1:                  # smallest prefix that is safe
        mid = (lo + hi) // 2
        if inplace_lead(build(mid)) <= max_lead:
            hi = mid
        else:
            lo = mid
    return build(hi)


def inplace_lead(data: bytes) -> int:
    """Largest (bytes written - bytes read) over the stream, read counted from
    the file start (header included), measured where the game's decoder checks
    it: after every token. The game loads a dpk file at the END of a buffer
    sized from the record's DECOMPRESSED field and decodes in place; when the
    file sits at dst + gap, the decoder needs written < gap + read after each
    token or it parks in its overrun trap (`b .` at 0x089143CC) -- a freeze.
    Safe for ANY buffer >= the decompressed size iff lead < out_size - len(data)
    (the gap is then at least that), provided the record's compressed field
    (+28) is len(data): a stale, larger +28 shrinks the gap by the difference
    (2026-10-24: 35 KB flat menu map under a 306,684 B +28 hung on map open)."""
    assert data[:4] == b"Wp16", "not a Wp16 stream"
    out_size = struct.unpack_from("<I", data, 4)[0]
    pos, n, w, lead = 8, len(data), 0, -(1 << 62)
    while w < out_size and pos + 4 <= n:
        flags = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        for i in range(32):
            if w >= out_size or pos + 2 > n:
                break
            word = struct.unpack_from("<H", data, pos)[0]
            pos += 2
            if (flags >> i) & 1:
                w += 2
            else:
                if word >> 5 == 0:
                    return lead
                w += ((word & 0x1f) + 2) * 2
            lead = max(lead, w - pos)
    return lead


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
