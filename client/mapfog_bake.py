"""Fog of war for the menu world map (random_map + fog_of_war): two on-disc
hooks + a client mailbox. Production port of attic/ow_rando_v0/mapfog.py
(LIVE-VERIFIED 2026-10-14); same cave, same mailbox (ff1_data.MAPFOG_*). See memory world-map-screen-minimap.

Map PCK lifecycle (live 2026-10-14): the game unpacks MINIMAPn.PCK ONCE per
boot (already during save load) into a heap buffer and keeps it cached for the
session; the GPU textures straight from that buffer and re-reads it on every
map OPEN (not while the map is shown). So pixels written into the cache show
on the next open.

GAME-SIDE, NO CLIENT LATENCY (user 2026-10-14: "I don't want a long delay"):
  * UNPACK hook -- full-function wrapper on the Wp16 decompressor 0x0891413C.
    When dst is the map PCK: copy both pictures to a PRISTINE buffer (zero-init
    bss tail after the cave), fog every chunk the mask does not mark explored,
    set applied = mask, publish bufptr.
  * STEP hook -- entry detour on the field scene's per-frame update 0x08864FE0
    (vtable 0x089C7A70 slot 3; a0 = field object). On the overworld (the FINE
    map id, save+0x2008 through the save-struct pointer, == 0xFF) with a sane
    position (field+0x68F0 x = grid+1, +0x68F2 y), and that position STABLE for STABLE_FRAMES frames (every frame moved
    at most JUMP tiles), when the party's tile or vehicle changes it sets the
    mask bits of every chunk whose centre is within the vehicle's reveal radius
    (REVEAL_*: foot/canoe 9 tiles, ship 3x, airship 6x; v362).
    v342 (player report 2026-10-24: in and out of Cornelia revealed the NW
    corner): the gate used to read 0x08D13121, which is a scroll/region coord,
    not a map id (ff1_data TRAP note) -- and un-relocated, so at a nonzero save
    delta it read dead RAM and passed in towns, marking the town's small x/y as
    world chunks (live 2026-10-24: lastkey moved inside Cornelia, fine 0x31,
    and its shop, 0x19). The stability count is belt and braces for the map
    change itself: sampled live the fine id flips first, with on-field 0
    between, but ff1_data's hold note saw coords lead the id once.
    The on-field byte (+0x68B4) is NOT a gate any more (player report
    2026-10-24: sailing the ship revealed nothing). It was only ever
    characterized as walking 1 / battle 0; the coords it guarded against are
    the party's own spot at a battle edge, and a map change is the fine id's
    and the stability count's job. EVERY frame it syncs: chunks in mask but not in applied get
    their pristine pixels copied into the cache (32 word compares when idle).
  * The CLIENT only persists the mask (AP data storage) and writes it back
    after connect; the per-frame sync draws whatever it restores.
Every cache write first re-validates the buffer (PCK count 2, 'MINI512.' name,
both GIM 'MIG.' headers) so a freed/reused buffer is never written.

Chunks: 8x8 tiles = 16x16 px of MINI512 (two contiguous 128-byte swizzle
blocks: +0 and +0x1000) = 8x8 px of MINI256 (8 rows x 8 bytes); 32x32 chunks,
c = cy*32 + cx in PIXEL space: cx = ((gx+9)&255)>>3, cy = gy>>3.

Mailbox (cave base B):
  +0x000 'FFOG'  +0x004 bufptr  +0x008 flags (bit0 fog on)  +0x00C version
  +0x010 mask[128]     bit c = explored (game + client write)
  +0x090 applied[128]  bit c = true pixels are in the cache (game only)
  +0x110 lastkey (mode<<16 | y<<8 | x of the last reveal)   +0x114 pristine_ok
  +0x118 fx_applied  +0x11C fx count  +0x120 fx table  +0x124 fx_dirty
  +0x128 stable (frames the overworld position has held without a jump)
  +0x12C lastpos (u16 x, u16 y of the previous gated frame)
  +0x130..+0x13F reserved

WORLD EFFECTS (mapfx_bake, 2026-10-24): the same two hooks carry the menu
map's world changes, with or without fog (flags bit0 = fog). Every field
frame, BEFORE the fog step, `fx` reads each effect's save byte (through the
save-struct pointer 0x089D7AD8) into a want mask; effects whose bit differs
from fx_applied, or all of them while fx_dirty (set by every unpack: the cache
holds the disc picture again), get their OPEN or CLOSED word runs written --
into the cache, or with fog into the pristine copy, clearing the effect's
chunks from `applied` so the fog sync redraws the explored ones this frame.
"""
import struct

DECOMP = 0x0891413C          # Wp16 decompress(a0=src, a1=dst); leaf, one jr ra @0x089143EC
DECOMP_ORIG = (0x27BDFFF0, 0xAFB3000C)   # addiu sp,sp,-0x10 ; sw s3,0xc(sp)
FIELD_UPDATE = 0x08864FE0    # field scene per-frame update (vtable 0x089C7A70 slot 3)
FIELD_UPDATE_ORIG = (0x27BDFFF0, 0xAFBF000C)   # addiu sp,sp,-0x10 ; sw ra,0xc(sp)
FINE_MAP_OFF = 0x2008        # save struct + off = u32 FINE map id (ff1_data.FIELD_MAP_ID_SA)
OVERWORLD_FINE = 0xFF        # ff1_data.OVERWORLD_FIELD_MAP_ID
STABLE_FRAMES = 30           # frames without a jump before a position reveals
JUMP = 8                     # tiles per frame beyond which a move is a jump, not travel
POS_OFF = 0x68F0             # field object: u16 x (= grid x + 1), u16 y (= grid y)
ONFIELD_OFF = 0x68B4         # u8 1 = walking the field (ff1_data); NOT a fog gate since v342
MODE_OFF = 0x2A8             # save+0x2A8 u32 vehicle mode (ff1_data.SD_VEHICLE_MODE_SA)
MODE_SHIP, MODE_AIRSHIP = 2, 3   # 0 foot, 1 canoe
# Reveal radius in tiles: a chunk is explored when its centre lies within R of
# the party's tile centre (square). R=12 is exactly the old 3x3 chunks; user
# 2026-10-29: base 9 tiles, ship 3x, airship 6x (foot and canoe = base).
REVEAL_FOOT = 9
REVEAL_SHIP = REVEAL_FOOT * 3
REVEAL_AIRSHIP = REVEAL_FOOT * 6
R16_FOOT, R16_SHIP, R16_AIRSHIP = (int(round(r * 16)) for r in (REVEAL_FOOT, REVEAL_SHIP, REVEAL_AIRSHIP))
PIX512_OFF = 0x10A50         # MINI512 pixels inside the decompressed PCK
PIX256_OFF = 0x530           # MINI256 pixels
GIM256_OFF = 0x60            # 'MIG.' headers inside the PCK
GIM512_OFF = 0x10580
PRISTINE_SIZE = 0x50000      # 512 pixels (0x40000) + 256 pixels (0x10000)
MAGIC = b"FFOG"
O_BUF, O_FLAGS, O_MASK, O_APPLIED, O_LASTKEY, O_PRIS = 0x4, 0x8, 0x10, 0x90, 0x110, 0x114
O_FXAPP, O_FXN, O_FXTAB, O_FXDIRTY = 0x118, 0x11C, 0x120, 0x124
O_STABLE, O_LASTPOS = 0x128, 0x12C
SAVE_PTR = 0x089D7AD8        # u32 -> live save struct (ff1_data.SAVE_BLOCK_PTR)
HDR = 0x140
N_CHUNK = 1024
FOG_WINDOW = (152, 152)      # 64x64 deep-sea source window in the VANILLA painting
                             # (pick_fog_window(): all pixels >= 25 px from land)
W_MINI, W_512, W_MIG = 0x494E494D, 0x2E323135, 0x2E47494D   # 'MINI' '512.' 'MIG.'


def chunk_of(gx, gy):
    """Chunk index for grid cell (gx, gy)."""
    return (gy >> 3) * 32 + (((gx + 9) & 255) >> 3)


def _swizzle(pix, w, h):
    out = bytearray(w * h)
    bw = w // 16
    for by in range(h // 8):
        for bx in range(bw):
            d = (by * bw + bx) * 128
            for r in range(8):
                s = (by * 8 + r) * w + bx * 16
                out[d + r * 16:d + r * 16 + 16] = pix[s:s + 16]
    return bytes(out)


def fog_textures(dec):
    """(fog512 swizzled 64x64, fog256 swizzled 32x32) from the VANILLA PCK's
    deep sea, darkened to an 'uncharted' tone below any sea tone (shore 98+)
    so it reads as unknown, not as open water."""
    from . import minimap_bake as M
    ent = M.pck_entries(dec)
    o5, s5 = ent["MINI512.GIM"]
    o2, s2 = ent["MINI256.GIM"]
    _w, _h, pix, pal, _o = M.gim_decode(dec[o5:o5 + s5])
    _w2, _h2, pix2, pal2, _o2 = M.gim_decode(dec[o2:o2 + s2])
    used = sorted(set(pix))
    used2 = sorted(set(pix2))
    luma = lambda c: (c[0] * 3 + c[1] * 6 + c[2]) / 10.0
    x0, y0 = FOG_WINDOW
    win = [pix[(y0 + y) * 512 + x0 + x] for y in range(64) for x in range(64)]
    mean = sum(luma(pal[k]) for k in win) / len(win)
    s = 92.0 / max(mean, 1.0)
    cache = {}

    def near(P, cand, r, g, b):
        return min(cand, key=lambda j: (P[j][0] - r) ** 2 + (P[j][1] - g) ** 2 + (P[j][2] - b) ** 2)

    f512 = bytearray()
    for k in win:
        v = cache.get(k)
        if v is None:
            c = pal[k]
            v = cache[k] = near(pal, used, c[0] * s, c[1] * s * 0.97, c[2] * s * 0.95)
        f512.append(v)
    f256 = bytearray(32 * 32)
    for y in range(32):
        for x in range(32):
            q = [win[(2 * y + dy) * 64 + 2 * x + dx] for dy in (0, 1) for dx in (0, 1)]
            f256[y * 32 + x] = near(pal2, used2, sum(pal[k][0] for k in q) / 4 * s,
                                    sum(pal[k][1] for k in q) / 4 * s * 0.97,
                                    sum(pal[k][2] for k in q) / 4 * s * 0.95)
    return _swizzle(bytes(f512), 64, 64), _swizzle(bytes(f256), 32, 32)


def _A():
    from . import mips_asm as A
    return A


def _nor(A, rd, rs, rt):
    return A.word((A._r(rs) << 21) | (A._r(rt) << 16) | (A._r(rd) << 11) | 0x27)


def _sllv(A, rd, rt, rs):
    return A.word((A._r(rs) << 21) | (A._r(rt) << 16) | (A._r(rd) << 11) | 0x04)


def _xor(A, rd, rs, rt):
    return A.word((A._r(rs) << 21) | (A._r(rt) << 16) | (A._r(rd) << 11) | 0x26)


def fx_count(fx):
    """Records in a mapfx_bake.pack table (each carries its own length)."""
    n = o = 0
    while o < len(fx):
        ln = struct.unpack_from("<I", fx, o + 4)[0]
        if ln < 140 or ln % 4 or o + ln > len(fx):
            raise ValueError("mapfog: malformed effect table")
        o += ln
        n += 1
    return n


def _fx_code(A, base, P512, P256):
    """fx(): the world-effects pass (see the module docstring). A leaf;
    clobbers a0-a3, v0, v1, t0-t9."""
    L = lambda n: ("label", n)
    return [
        A.li("t9", base),
        A.lw("a3", O_FXN, "t9"),
        ("beq", "a3", "zero", "fret"), A.nop(),
        A.lui("t0", SAVE_PTR >> 16), A.lw("t0", SAVE_PTR & 0xFFFF, "t0"),
        A.lui("t1", 0x0880), A.sltu("t1", "t0", "t1"),
        ("bne", "t1", "zero", "fret"), A.nop(),
        A.lui("t1", 0x0A00), A.sltu("t1", "t0", "t1"),
        ("beq", "t1", "zero", "fret"), A.nop(),
        # v0 = want: bit i = effect i's save byte & mask
        A.lw("t2", O_FXTAB, "t9"),
        A.addiu("v0", "zero", 0), A.addiu("v1", "zero", 0),
        L("fw"),
        A.lhu("t3", 0, "t2"), A.addu("t3", "t3", "t0"), A.lbu("t3", 0, "t3"),
        A.lbu("t4", 2, "t2"), A.and_("t3", "t3", "t4"),
        ("beq", "t3", "zero", "fwn"), A.addiu("t4", "zero", 1),
        _sllv(A, "t4", "t4", "v1"), A.or_("v0", "v0", "t4"),
        L("fwn"),
        A.lw("t4", 4, "t2"), A.addu("t2", "t2", "t4"),
        A.addiu("v1", "v1", 1),
        ("bne", "v1", "a3", "fw"), A.nop(),
        # t6 = effects to write: changed since last pass, or all after an unpack
        A.lw("t5", O_FXAPP, "t9"), _xor(A, "t6", "v0", "t5"),
        A.lw("t5", O_FXDIRTY, "t9"), A.or_("t6", "t6", "t5"),
        ("beq", "t6", "zero", "fret"), A.nop(),
        A.lw("a2", O_BUF, "t9"),
        ("beq", "a2", "zero", "fret"), A.nop(),
    ] + _valid_buf(A, "a2", "fret") + [
        # targets: the pristine copy when the fog draws from it, else the cache
        A.addiu("a1", "zero", 0),
        A.lw("t0", O_FLAGS, "t9"), A.andi("t0", "t0", 1),
        ("beq", "t0", "zero", "fcache"), A.nop(),
        A.lw("t0", O_PRIS, "t9"),
        ("beq", "t0", "zero", "fcache"), A.nop(),
        A.addiu("a1", "zero", 1),
        A.li("t7", P512), A.li("t8", P256),
        ("beq", "zero", "zero", "fgo"), A.nop(),
        L("fcache"),
        A.li("t7", PIX512_OFF), A.addu("t7", "t7", "a2"),
        A.addiu("t8", "a2", PIX256_OFF),
        L("fgo"),
        A.lw("t2", O_FXTAB, "t9"),
        A.addiu("v1", "zero", 0),
        L("fe"),
        A.srlv("t3", "t6", "v1"), A.andi("t3", "t3", 1),
        ("beq", "t3", "zero", "fen"), A.nop(),
        A.srlv("t4", "v0", "v1"), A.andi("t4", "t4", 1),      # 1 = paint OPEN
        A.lw("t3", 8, "t2"),                                  # runs left
        A.addiu("a0", "t2", 140),                             # first run
        L("fr"),
        ("beq", "t3", "zero", "frd"), A.nop(),
        A.lw("t0", 0, "a0"), A.lw("t1", 4, "a0"),             # dst, nwords
        A.sll("t5", "t0", 1), A.srl("t5", "t5", 1),           # byte offset
        A.srl("t0", "t0", 31),                                # 1 = MINI256
        ("beq", "t0", "zero", "f512"), A.addu("a2", "t5", "t7"),
        A.addu("a2", "t5", "t8"),
        L("f512"),
        A.addiu("t5", "a0", 8),                               # open words
        A.sll("t0", "t1", 2),
        ("bne", "t4", "zero", "fcp"), A.nop(),
        A.addu("t5", "t5", "t0"),                             # closed words
        L("fcp"),
        A.addu("a0", "a0", "t0"), A.addu("a0", "a0", "t0"), A.addiu("a0", "a0", 8),
        L("fcl"),
        A.lw("t0", 0, "t5"), A.sw("t0", 0, "a2"),
        A.addiu("t5", "t5", 4), A.addiu("a2", "a2", 4), A.addiu("t1", "t1", -1),
        ("bne", "t1", "zero", "fcl"), A.nop(),
        ("beq", "zero", "zero", "fr"), A.addiu("t3", "t3", -1),
        L("frd"),
        # fog: drop the touched chunks from applied -> the sync redraws them
        ("beq", "a1", "zero", "fen"), A.nop(),
        A.addiu("t3", "zero", 0),
        L("ft"),
        A.addu("t0", "t2", "t3"), A.lw("t0", 12, "t0"),
        _nor(A, "t0", "t0", "zero"),
        A.addu("t1", "t9", "t3"), A.lw("t5", O_APPLIED, "t1"),
        A.and_("t5", "t5", "t0"), A.sw("t5", O_APPLIED, "t1"),
        A.addiu("t3", "t3", 4), A.sltiu("t0", "t3", 128),
        ("bne", "t0", "zero", "ft"), A.nop(),
        L("fen"),
        A.lw("t0", 4, "t2"), A.addu("t2", "t2", "t0"),
        A.addiu("v1", "v1", 1),
        ("bne", "v1", "a3", "fe"), A.nop(),
        A.sw("v0", O_FXAPP, "t9"), A.sw("zero", O_FXDIRTY, "t9"),
        L("fret"),
        A.jr("ra"), A.nop(),
    ]


def _copy_chunk(A, tag, c, buf512, buf256, src512, src256, mod4=False):
    """Copy chunk `c` (register) into the cache. src* = registers holding the
    source picture bases (pristine) or, with mod4, fog textures indexed by
    chunk mod 4. Clobbers t1 t5 t7 t8 v1 a1."""
    L = lambda n: ("label", n + tag)
    out = [
        A.andi("t1", c, 31),                         # cx
        A.srl("t5", c, 5),                           # cy
    ]
    if not mod4:
        out += [A.sll("a1", "t5", 13), A.sll("t7", "t1", 7), A.addu("a1", "a1", "t7"),
                A.addu("v1", "a1", src512), A.addu("a1", "a1", buf512)]
    else:
        out += [A.sll("a1", "t5", 13), A.sll("t7", "t1", 7), A.addu("a1", "a1", "t7"),
                A.addu("a1", "a1", buf512),
                A.andi("v1", "t5", 3), A.sll("v1", "v1", 10),
                A.andi("t7", "t1", 3), A.sll("t7", "t7", 7), A.addu("v1", "v1", "t7"),
                A.addu("v1", "v1", src512)]
    two = 0x1000 if not mod4 else 0x200              # 2nd 128-B block of the chunk in the source
    out += [
        A.addiu("t8", "zero", 32),
        L("c1"),
        A.lw("t7", 0, "v1"), A.sw("t7", 0, "a1"),
        A.lw("t7", two, "v1"), A.sw("t7", 0x1000, "a1"),
        A.addiu("v1", "v1", 4), A.addiu("a1", "a1", 4), A.addiu("t8", "t8", -1),
        ("bne", "t8", "zero", "c1" + tag), A.nop(),
        # 256: (cy<<11) + ((cx>>1)<<7) + ((cx&1)<<3)
        A.sll("a1", "t5", 11),
        A.srl("t7", "t1", 1), A.sll("t7", "t7", 7), A.addu("a1", "a1", "t7"),
        A.andi("t7", "t1", 1), A.sll("t7", "t7", 3), A.addu("a1", "a1", "t7"),
    ]
    if not mod4:
        out += [A.addu("v1", "a1", src256), A.addu("a1", "a1", buf256)]
    else:
        out += [A.addu("a1", "a1", buf256),
                A.andi("v1", "t5", 3), A.sll("v1", "v1", 8),
                A.srl("t7", "t1", 1), A.andi("t7", "t7", 1), A.sll("t7", "t7", 7), A.addu("v1", "v1", "t7"),
                A.andi("t7", "t1", 1), A.sll("t7", "t7", 3), A.addu("v1", "v1", "t7"),
                A.addu("v1", "v1", src256)]
    out += [
        A.addiu("t8", "zero", 8),
        L("c2"),
        A.lw("t7", 0, "v1"), A.sw("t7", 0, "a1"),
        A.lw("t7", 4, "v1"), A.sw("t7", 4, "a1"),
        A.addiu("v1", "v1", 16), A.addiu("a1", "a1", 16), A.addiu("t8", "t8", -1),
        ("bne", "t8", "zero", "c2" + tag), A.nop(),
    ]
    return out


def _valid_buf(A, reg, fail):
    """Branch to `fail` unless `reg` points at the cached map PCK. Clobbers t0 t1."""
    return [
        A.andi("t0", reg, 3), ("bne", "t0", "zero", fail), A.nop(),
        A.lw("t0", 0, reg), A.addiu("t1", "zero", 2), ("bne", "t0", "t1", fail), A.nop(),
        A.lw("t0", 0x10, reg), A.li("t1", W_MINI), ("bne", "t0", "t1", fail), A.nop(),
        A.lw("t0", 0x14, reg), A.li("t1", W_512), ("bne", "t0", "t1", fail), A.nop(),
        A.lw("t0", GIM256_OFF, reg), A.li("t1", W_MIG), ("bne", "t0", "t1", fail), A.nop(),
        A.li("t1", GIM512_OFF), A.addu("t1", "t1", reg), A.lw("t0", 0, "t1"),
        A.li("t1", W_MIG), ("bne", "t0", "t1", fail), A.nop(),
    ]


def build_cave(base, pristine, fog512, fog256, flags=1, fx=b""):
    """Assemble the whole cave for RAM address `base`; `pristine` = bss buffer
    (unused without fog); `fx` = a mapfx_bake.pack effect table."""
    A = _A()
    L = lambda n: ("label", n)
    P512, P256 = pristine, pristine + 0x40000

    def assemble(at):
        fog512_at, fog256_at = at["fog512"], at["fog256"]
        # --- unpack wrapper (full-function wrapper on the leaf decompressor) ---
        wrap = [
            A.addiu("sp", "sp", -16), A.sw("ra", 12, "sp"), A.sw("a1", 8, "sp"),
            A.jal(at["tramp"]), A.nop(),
            A.sw("v0", 4, "sp"), A.lw("a0", 8, "sp"),
            A.jal(at["post"]), A.nop(),
            A.lw("v0", 4, "sp"), A.lw("ra", 12, "sp"),
            A.jr("ra"), A.addiu("sp", "sp", 16),
        ]
        tramp = [A.word(DECOMP_ORIG[0]), A.word(DECOMP_ORIG[1]), A.j(DECOMP + 8), A.nop()]
        # --- post(a0 = dst) ---
        post = _valid_buf(A, "a0", "pret") + [
            A.li("t9", base),
            A.sw("a0", O_BUF, "t9"),
            # the cache holds the disc picture again: fx rewrites every effect
            A.sw("zero", O_FXAPP, "t9"),
            A.addiu("t0", "zero", -1), A.sw("t0", O_FXDIRTY, "t9"),
            A.lw("t0", O_FLAGS, "t9"), A.andi("t0", "t0", 1),
            ("beq", "t0", "zero", "pret"), A.nop(),
            # pristine copy: 512 then 256 pixels
            A.li("t3", PIX512_OFF), A.addu("t3", "t3", "a0"),
            A.addiu("t4", "a0", PIX256_OFF),
            A.li("v1", P512), A.li("t8", 0x10000),
            L("pc1"),
            A.lw("t7", 0, "t3"), A.sw("t7", 0, "v1"),
            A.addiu("t3", "t3", 4), A.addiu("v1", "v1", 4), A.addiu("t8", "t8", -1),
            ("bne", "t8", "zero", "pc1"), A.nop(),
            A.addiu("t4", "a0", PIX256_OFF),
            A.li("v1", P256), A.li("t8", 0x4000),
            L("pc2"),
            A.lw("t7", 0, "t4"), A.sw("t7", 0, "v1"),
            A.addiu("t4", "t4", 4), A.addiu("v1", "v1", 4), A.addiu("t8", "t8", -1),
            ("bne", "t8", "zero", "pc2"), A.nop(),
            A.addiu("t0", "zero", 1), A.sw("t0", O_PRIS, "t9"),
            # fog every chunk not in mask; applied = mask
            A.li("t3", PIX512_OFF), A.addu("t3", "t3", "a0"),
            A.addiu("t4", "a0", PIX256_OFF),
            A.li("t6", fog512_at), A.li("a2", fog256_at),
            A.addiu("t2", "zero", 0),
            L("ploop"),
            A.srl("t0", "t2", 3), A.addu("t0", "t0", "t9"), A.lbu("t0", O_MASK, "t0"),
            A.andi("t1", "t2", 7), A.srlv("t0", "t0", "t1"), A.andi("t0", "t0", 1),
            ("bne", "t0", "zero", "pnext"), A.nop(),
        ] + _copy_chunk(A, "_f", "t2", "t3", "t4", "t6", "a2", mod4=True) + [
            L("pnext"),
            A.addiu("t2", "t2", 1), A.sltiu("t0", "t2", N_CHUNK),
            ("bne", "t0", "zero", "ploop"), A.nop(),
            A.addiu("t2", "zero", 0),
            L("pap"),
            A.addu("t0", "t2", "t9"),
            A.lw("t1", O_MASK, "t0"), A.sw("t1", O_APPLIED, "t0"),
            A.addiu("t2", "t2", 4), A.sltiu("t0", "t2", 128),
            ("bne", "t0", "zero", "pap"), A.nop(),
            L("pret"),
            A.jr("ra"), A.nop(),
        ]
        # --- per-frame field update detour ---
        stephook = [
            A.addiu("sp", "sp", -16), A.sw("ra", 12, "sp"), A.sw("a0", 8, "sp"),
            A.jal(at["fx"]), A.nop(),
            A.lw("a0", 8, "sp"),
            A.jal(at["step"]), A.nop(),
            A.lw("a0", 8, "sp"), A.lw("ra", 12, "sp"), A.addiu("sp", "sp", 16),
            A.word(FIELD_UPDATE_ORIG[0]), A.word(FIELD_UPDATE_ORIG[1]),
            A.j(FIELD_UPDATE + 8), A.nop(),
        ]
        # --- step(a0 = field object) ---
        step = [
            A.li("t9", base),
            A.lw("t0", O_FLAGS, "t9"), A.andi("t0", "t0", 1),
            ("beq", "t0", "zero", "sret"), A.nop(),
            # overworld = FINE map id (relocates with the save struct) == 0xFF
            A.lui("t0", SAVE_PTR >> 16), A.lw("t0", SAVE_PTR & 0xFFFF, "t0"),
            A.lui("t1", 0x0880), A.sltu("t1", "t0", "t1"),
            ("bne", "t1", "zero", "nogo"), A.nop(),
            A.lui("t1", 0x0A00), A.sltu("t1", "t0", "t1"),
            ("beq", "t1", "zero", "nogo"), A.nop(),
            A.lw("t0", FINE_MAP_OFF, "t0"), A.addiu("t1", "zero", OVERWORLD_FINE),
            ("bne", "t0", "t1", "nogo"), A.nop(),
            A.lhu("t0", POS_OFF, "a0"), A.lhu("t1", POS_OFF + 2, "a0"),
            A.sltiu("t2", "t0", 256), ("beq", "t2", "zero", "nogo"), A.nop(),
            A.sltiu("t2", "t1", 256), ("beq", "t2", "zero", "nogo"), A.nop(),
            # stable: a jump (map change, teleport) restarts the count
            A.lhu("t3", O_LASTPOS, "t9"), A.lhu("t4", O_LASTPOS + 2, "t9"),
            A.sh("t0", O_LASTPOS, "t9"), A.sh("t1", O_LASTPOS + 2, "t9"),
            A.subu("t3", "t0", "t3"), A.addiu("t3", "t3", JUMP), A.andi("t3", "t3", 255),
            A.sltiu("t3", "t3", 2 * JUMP + 1), ("beq", "t3", "zero", "nogo"), A.nop(),  # |dx| <= JUMP (wraps)
            A.subu("t4", "t1", "t4"), A.addiu("t4", "t4", JUMP), A.andi("t4", "t4", 255),
            A.sltiu("t4", "t4", 2 * JUMP + 1), ("beq", "t4", "zero", "nogo"), A.nop(),  # |dy| <= JUMP
            A.lw("t3", O_STABLE, "t9"), A.sltiu("t4", "t3", STABLE_FRAMES),
            ("beq", "t4", "zero", "held"), A.nop(),
            A.addiu("t3", "t3", 1), A.sw("t3", O_STABLE, "t9"),
            ("beq", "zero", "zero", "sync"), A.nop(),
            L("held"),
            # reveal radius (1/16 tiles) by vehicle mode; the save ptr passed the range check above
            A.lui("t2", SAVE_PTR >> 16), A.lw("t2", SAVE_PTR & 0xFFFF, "t2"),
            A.lw("t2", MODE_OFF, "t2"),
            A.addiu("a3", "zero", R16_FOOT),
            A.addiu("t3", "zero", MODE_SHIP), ("bne", "t2", "t3", "nship"), A.nop(),
            A.addiu("a3", "zero", R16_SHIP),
            L("nship"),
            A.addiu("t3", "zero", MODE_AIRSHIP), ("bne", "t2", "t3", "nair"), A.nop(),
            A.addiu("a3", "zero", R16_AIRSHIP),
            L("nair"),
            # key = mode<<16 | y<<8 | x: a new tile or vehicle re-reveals
            A.andi("t2", "t2", 255), A.sll("t2", "t2", 16),
            A.sll("t3", "t1", 8), A.or_("t2", "t2", "t3"), A.or_("t2", "t2", "t0"),
            A.lw("t3", O_LASTKEY, "t9"),
            ("beq", "t2", "t3", "sync"), A.nop(),
            A.sw("t2", O_LASTKEY, "t9"),
            # party tile centre, 1/16 tiles, pixel-chunk space
            A.addiu("t0", "t0", 8), A.andi("t0", "t0", 255),                         # (x-1)+9 & 255
            A.sll("t0", "t0", 4), A.addiu("t0", "t0", 8),
            A.sll("t1", "t1", 4), A.addiu("t1", "t1", 8),
            A.sll("a2", "a3", 1), A.addiu("a2", "a2", 1),                            # 2R+1
            # chunk c is revealed when its centre is within R of the party (square)
            A.addiu("t4", "zero", 0),                                                # cy
            L("yl"),
            A.sll("t5", "t4", 7), A.addiu("t5", "t5", 64),
            A.subu("t5", "t5", "t1"), A.addu("t5", "t5", "a3"),                      # Cy-Py+R
            A.sltu("t5", "t5", "a2"), ("beq", "t5", "zero", "yn"), A.nop(),
            A.addiu("t7", "zero", 0),                                                # cx
            L("xl"),
            A.sll("t5", "t7", 7), A.addiu("t5", "t5", 64),
            A.subu("t5", "t5", "t0"), A.addu("t5", "t5", "a3"),
            A.andi("t5", "t5", 0xFFF),                                               # x wraps (256 tiles)
            A.sltu("t5", "t5", "a2"), ("beq", "t5", "zero", "xn"), A.nop(),
            A.sll("t3", "t4", 5), A.addu("t3", "t3", "t7"),                          # c
            A.srl("t6", "t3", 3), A.addu("t6", "t6", "t9"), A.lbu("v0", O_MASK, "t6"),
            A.andi("v1", "t3", 7), A.addiu("a1", "zero", 1), _sllv(A, "a1", "a1", "v1"),
            A.or_("v0", "v0", "a1"), A.sb("v0", O_MASK, "t6"),
            L("xn"),
            A.addiu("t7", "t7", 1), A.sltiu("t5", "t7", 32), ("bne", "t5", "zero", "xl"), A.nop(),
            L("yn"),
            A.addiu("t4", "t4", 1), A.sltiu("t5", "t4", 32), ("bne", "t5", "zero", "yl"), A.nop(),
            ("beq", "zero", "zero", "sync"), A.nop(),
            L("nogo"),
            A.sw("zero", O_STABLE, "t9"),
            L("sync"),
            A.lw("t0", O_PRIS, "t9"), ("beq", "t0", "zero", "sret"), A.nop(),
            A.lw("a2", O_BUF, "t9"), ("beq", "a2", "zero", "sret"), A.nop(),
        ] + _valid_buf(A, "a2", "sret") + [
            A.li("t3", PIX512_OFF), A.addu("t3", "t3", "a2"),
            A.addiu("t4", "a2", PIX256_OFF),
            A.li("t6", P512), A.li("a0", P256),         # a0 is free here (saved by stephook)
            A.addiu("a3", "zero", 0),                   # word index w
            L("wl"),
            A.sll("t0", "a3", 2), A.addu("t0", "t0", "t9"),
            A.lw("t1", O_MASK, "t0"), A.lw("t5", O_APPLIED, "t0"),
            _nor(A, "t2", "t5", "zero"), A.and_("t2", "t1", "t2"),    # pend = mask & ~applied
            ("beq", "t2", "zero", "wn"), A.nop(),
            A.or_("t5", "t5", "t2"), A.sw("t5", O_APPLIED, "t0"),
            A.addu("a2", "t2", "zero"),                 # a2 = pend (bufptr no longer needed)
            A.addiu("v0", "zero", 0),                   # bit b
            L("bl"),
            A.srlv("t7", "a2", "v0"), A.andi("t7", "t7", 1),
            ("beq", "t7", "zero", "bn"), A.nop(),
            A.sll("t2", "a3", 5), A.addu("t2", "t2", "v0"),           # c
        ] + _copy_chunk(A, "_s", "t2", "t3", "t4", "t6", "a0") + [
            L("bn"),
            A.addiu("v0", "v0", 1), A.sltiu("t7", "v0", 32),
            ("bne", "t7", "zero", "bl"), A.nop(),
            L("wn"),
            A.addiu("a3", "a3", 1), A.sltiu("t0", "a3", 32),
            ("bne", "t0", "zero", "wl"), A.nop(),
            L("sret"),
            A.jr("ra"), A.nop(),
        ]
        parts = [("wrap", b"".join(wrap)), ("tramp", b"".join(tramp)), ("post", A.asm_labels(post)),
                 ("stephook", b"".join(stephook)), ("step", A.asm_labels(step)),
                 ("fx", A.asm_labels(_fx_code(A, base, P512, P256)))]
        return parts

    # layout pass (sizes are address-independent)
    at = {"fog512": 0, "fog256": 0, "tramp": 0, "post": 0, "step": 0, "fx": 0}
    pos = base + HDR
    layout = {}
    for name, blob in assemble(at):
        layout[name] = pos
        pos += len(blob)
    data_at = base + ((pos - base + 15) & ~15)   # base-relative: size is address-independent
    layout["fog512"] = data_at
    layout["fog256"] = data_at + len(fog512)
    layout["fxtab"] = data_at + len(fog512) + len(fog256)
    parts = assemble(layout)
    hdr = MAGIC + struct.pack("<III", 0, flags, 4) + bytes(128) + bytes(128) + struct.pack("<II", 0xFFFFFFFF, 0)
    hdr += struct.pack("<IIII", 0, fx_count(fx), layout["fxtab"] if fx else 0, 0xFFFFFFFF)
    hdr += struct.pack("<II", 0, 0xFFFFFFFF)                    # stable, lastpos
    hdr = hdr.ljust(HDR, b"\0")
    blob = hdr + b"".join(b for _n, b in parts)
    blob = blob.ljust(data_at - base, b"\0") + fog512 + fog256 + bytes(fx)
    return bytes(blob), layout


def _cave_size(fx=b""):
    return len(build_cave(0x08B30E00, 0x08B40000, bytes(0x1000), bytes(0x400), fx=fx)[0])


def reserve(elf, fx=b""):
    """Phase 1 (BEFORE any cave_bss_tail): check both hook sites are vanilla
    and add a placeholder cave; returns its RAM address. The cave's size is
    address-independent, so finish() can assemble it in place later; it
    grows with the effect table `fx`, which finish() must get unchanged."""
    from . import eboot_patch as EP
    for addr, orig in ((DECOMP, DECOMP_ORIG), (FIELD_UPDATE, FIELD_UPDATE_ORIG)):
        got = struct.unpack_from("<II", elf, EP.ram2file(addr))
        if got != orig:
            raise ValueError("%#x prologue %r != expected %r (already hooked?)" % (addr, got, orig))
    return EP.add_segment_cave(elf, bytearray(_cave_size(fx)))


def finish(elf, base, dec, log=print, flags=1, fx=b""):
    """Phase 2 (after every add_segment_cave): reserve the pristine bss
    buffer (fog only), assemble the cave at `base` (from reserve) with the fog
    textures cut from `dec` (the VANILLA decompressed MINIMAP2.PCK) and the
    effect table `fx`, and detour the Wp16 decompressor and the field
    per-frame update into it."""
    from . import eboot_patch as EP
    f512, f256 = fog_textures(dec)
    pristine = EP.cave_bss_tail(elf, PRISTINE_SIZE, align=0x40) if flags & 1 else 0
    blob, where = build_cave(base, pristine, f512, f256, flags, fx=fx)
    if len(blob) != _cave_size(fx):
        raise ValueError("mapfog: cave size changed between reserve and finish")
    EP.cave_write(elf, base, blob)
    EP.install_detour(elf, DECOMP, where["wrap"])
    EP.install_detour(elf, FIELD_UPDATE, where["stephook"])
    log("[mapfog] cave @%#010x (%d B, fog %s, %d world effect(s)) + pristine bss @%#010x;"
        " detoured Wp16 decompressor -> %#x, field update -> %#x"
        % (base, len(blob), "on" if flags & 1 else "off", fx_count(fx), pristine,
           where["wrap"], where["stephook"]))
    return where


def apply_reference(dec, mask, fog512, fog256):
    """Python twin of the unpack fog pass: same addresses, same copies."""
    buf = bytearray(dec)
    for c in range(N_CHUNK):
        if mask[c >> 3] >> (c & 7) & 1:
            continue
        cx, cy = c & 31, c >> 5
        d = PIX512_OFF + (cy << 13) + (cx << 7)
        s_ = ((cy & 3) << 10) + ((cx & 3) << 7)
        buf[d:d + 128] = fog512[s_:s_ + 128]
        buf[d + 0x1000:d + 0x1080] = fog512[s_ + 0x200:s_ + 0x280]
        d = PIX256_OFF + (cy << 11) + ((cx >> 1) << 7) + ((cx & 1) << 3)
        s_ = ((cy & 3) << 8) + (((cx >> 1) & 1) << 7) + ((cx & 1) << 3)
        for r in range(8):
            buf[d + 16 * r:d + 16 * r + 8] = fog256[s_ + 16 * r:s_ + 16 * r + 8]
    return bytes(buf)
