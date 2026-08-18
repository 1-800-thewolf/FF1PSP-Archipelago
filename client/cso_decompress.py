"""Compressed PSP disc images -> a plain .iso the patcher can bake.

Why this exists: everything in this project reads the disc by absolute byte
offset (eboot_patch walks ISO9660, extern_bake seeks FF1PSP.DPK). A compressed
image has no such offsets, so a .cso fails at the first seek with a confusing
"BOOT.BIN not a plaintext ELF" (user report 2026-08-08 -- the player's first
guess was in fact the CSO, and they were half right: the real cause was a
different ISO revision, but a CSO would have failed too, one step later).

CISO v1 is the only compressed container we can expand with the stdlib alone:
its blocks are raw deflate. ZSO/DAX/JSO need LZ4/LZO and CHD needs a full
hunk-map + CD-framing reader, so those are detected and refused by name with
instructions instead of failing obscurely.
"""
import os
import struct
import zlib

CISO_MAGIC = b"CISO"
ZISO_MAGIC = b"ZISO"
DAX_MAGIC = b"DAX\0"
CHD_MAGIC = b"MComprHD"


class CompressedImage(Exception):
    """The image is compressed in a format we cannot expand here.

    Carries the /checkiso pointer on __str__ for the same reason
    eboot_patch.UnsupportedIsoRevision does -- see CHECKISO_HINT there."""

    def __str__(self):
        from .eboot_patch import CHECKISO_HINT
        return super().__str__() + CHECKISO_HINT


def image_kind(path):
    """Return 'iso', 'ciso', 'ziso', 'dax', 'chd' or 'unknown' for `path`."""
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return "unknown"
    if head[:4] == CISO_MAGIC:
        return "ciso"
    if head[:4] == ZISO_MAGIC:
        return "ziso"
    if head[:4] == DAX_MAGIC:
        return "dax"
    if head[:8] == CHD_MAGIC:
        return "chd"
    return "iso"        # a raw ISO9660 image has no magic this early


def _ciso_decompress(src, dst, progress=None):
    """Expand a CISO v1 image. Index entry = (offset >> align), MSB set means
    the block is stored RAW (incompressible); otherwise it is raw deflate."""
    with open(src, "rb") as f:
        hdr = f.read(0x18)
        if len(hdr) < 0x18 or hdr[:4] != CISO_MAGIC:
            raise CompressedImage("not a CISO image")
        (_, hdr_size, total_bytes, block_size,
         ver, align, _res) = struct.unpack("<4sIQIBBH", hdr)
        if ver > 1:
            raise CompressedImage(
                f"this is a CISO version {ver} image; only version 1 can be "
                "expanded here. Please decompress it yourself (PPSSPP: "
                "Game settings > Tools, or maxcso) and pick the .iso.")
        n_blocks = total_bytes // block_size
        f.seek(hdr_size if hdr_size else 0x18)
        index = struct.unpack(f"<{n_blocks + 1}I", f.read(4 * (n_blocks + 1)))

        with open(dst, "wb") as out:
            for i in range(n_blocks):
                cur, nxt = index[i], index[i + 1]
                raw = bool(cur & 0x80000000)
                off = (cur & 0x7FFFFFFF) << align
                end = (nxt & 0x7FFFFFFF) << align
                f.seek(off)
                blob = f.read(max(end - off, 0) or block_size)
                if raw:
                    block = blob[:block_size]
                else:
                    # wbits=-15: raw deflate, no zlib header. The stored length
                    # is padded up to the alignment, so trailing garbage is
                    # normal -- decompressobj stops at the stream end.
                    block = zlib.decompressobj(-zlib.MAX_WBITS).decompress(
                        blob, block_size)
                if len(block) != block_size:
                    block = block.ljust(block_size, b"\0")[:block_size]
                out.write(block)
                if progress and (i & 0x3FF) == 0:
                    progress(i * block_size, total_bytes)
        if progress:
            progress(total_bytes, total_bytes)
    return dst


def ensure_plain_iso(path, notify=None, cache_dir=None):
    """Return a path to a PLAIN .iso for `path`, expanding a CISO if needed.

    A plain image is returned untouched. An expanded copy is cached next to the
    source (or in cache_dir) and reused, so this costs its ~1.5GB write once.
    Raises CompressedImage for containers we cannot expand."""
    say = notify or (lambda *_: None)
    kind = image_kind(path)
    if kind in ("iso", "unknown"):
        return path
    if kind != "ciso":
        pretty = {"ziso": "ZSO/ZISO (LZ4)", "dax": "DAX", "chd": "CHD"}[kind]
        raise CompressedImage(
            f"{os.path.basename(path)} is a {pretty} image. This client can "
            "only expand CSO automatically. Please convert it to a plain .iso "
            "(PPSSPP or maxcso) and select that file instead.")

    base = os.path.splitext(os.path.basename(path))[0] + ".iso"
    dst = os.path.join(cache_dir or os.path.dirname(path), base)
    done = dst + ".expanded"
    if os.path.isfile(dst) and os.path.isfile(done):
        say(f"Using the already-expanded copy of your CSO ({base}).")
        return dst
    if os.path.abspath(dst) == os.path.abspath(path):
        raise CompressedImage(f"{path} is a CSO but already named .iso; "
                              "rename it to .cso so the expanded copy has "
                              "somewhere to go.")
    say(f"Your game is a compressed CSO. Expanding it to a plain ISO once "
        f"({base}) -- the patcher needs real disc offsets. Your .cso is left "
        f"untouched.")
    last = [0.0]

    def prog(done_b, total_b):
        import time
        if time.monotonic() - last[0] < 3.0 and done_b < total_b:
            return
        last[0] = time.monotonic()
        say(f"  [cso] {done_b >> 20} / {total_b >> 20} MB")

    try:
        _ciso_decompress(path, dst, prog)
    except Exception:
        try:
            os.unlink(dst)          # never leave a half-written image behind
        except OSError:
            pass
        raise
    with open(done, "w", encoding="utf-8") as f:
        f.write(os.path.basename(path))
    say("CSO expanded.")
    return dst
