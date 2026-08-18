"""On-disc BATTLEICON edits (dpk side of the per-target popup-colour feature).

Battle popups (damage/heal numbers, the hit counter, MISS!!) are drawn from
BATTLEICON.PCK in ff1psp.dpk: a wp16 bundle holding BATTLEICON.AOB (sprite
records), BATTLEICON.OTI (per-def source rects) and BATTLEICON_ENG/JPN.GIM
(the 512x32 index-8 atlas). The engine picks a sprite by KIND, which indexes
both the AOB record list and the OTI def list.

This module adds extra colours to that atlas so iso_patcher's popup-colour
caves have something to point at:
  * the GIM grows 512x32 -> 512x64 and row 3 receives recoloured copies of the
    ten white digits in red (x160..319) and teal (x0..159), plus the MISS!! text
    in red (x320) and yellow (x352). Only the white RAMP indices are remapped --
    the black outline index is left alone, or the glyphs become unreadable.
  * the AOB/OTI def tables GROW 24 -> 30 and the custom defs live at 25..29:
    25 = red digits, 26 = teal digits, 27 = yellow heal-arm digits (clones of
    the white digit def 23), 28/29 = red/yellow MISS!! (clones of def 11).
    Def 24 is an inert W=0 dummy: the engine natively spawns kind 0x18 = 24
    (site 0x88737d8), so that index must draw nothing.
    ALL 24 vanilla defs stay untouched. Defs 13-22 are the ENEMY-side status
    balloons (dx=-32 variants of party balloons 0-9, AOB p2=4592) -- the old
    in-place strategy overwrote defs 18-22 and made any enemy with status 5..9
    blink our glyphs (Blind = a mirrored red MISS!! flashing forever, live
    2026-07-24, see HANDOFF_stuck_red_miss_popup.md).
    Growing works only when BOTH tables grow together: the 2026-07-22 "grow to
    25 renders nothing" attempt grew only the OTI (the AOB record gates the
    lookup). Grown-table rendering was live-validated 2026-07-24 (kind-25 poke
    drew red digits).

UV rule (playtest-derived): pixel_x = def.U + (cell - def.id) * 16, pixel_y =
def.V. Digits are spawned WITH a cell (the colour bank + digit), so the red
digit def's U is the white U shifted by (red x - white x); MISS!! is spawned
with no cell, so its def's U/V are the glyph position verbatim.

BATTLEICON_JPN.GIM's pixels are zeroed (this is an English-only randomiser) to
buy back the compressed space the grown atlas needs.

Same in-place strategy as map_bake/campus_bake: decompress, edit, recompress,
write back into the SAME dpk record slot zero-padded, so neither the dpk nor the
ISO changes size. OWNERSHIP (see [[dpk-dead-space-ownership]]): this module is
the only writer of BATTLEICON.PCK -- ms2_bake's donor pool is FM_DBG_*/FM_*J1/J2,
extern_bake reserves FM_EXTERN*J1 and map_bake owns MAP_*.PCK, so nothing here
can collide. RAISES on any mismatch: the caves reference the new defs, so a
half-applied atlas would draw garbage rather than merely look wrong.
"""
import struct

try:
    from . import wp16
except ImportError:  # pragma: no cover - direct-script use
    import wp16

REC_OFF, REC_SIZE = 0x1938250, 0x1c54          # BATTLEICON.PCK inside ff1psp.dpk
DPK_BASE_IN_ISO = 0x2bb0000
ISO_OFF = DPK_BASE_IN_ISO + REC_OFF

GIM_OFF, OTI_OFF, OTI_SIZE = 0x5c0, 0x2a0, 0x312
AOB_OFF, AOB_SIZE, JPN_OFF = 0xa0, 0x1fc, 0x4aa0
W, H_OLD, H_NEW = 512, 32, 64
# HEIGHT IS FROZEN AT 64 (live 2026-07-23, two failed grows): 96 is not a
# power of two -> the GE garbled EVERY glyph; 128 is a power of two but still
# broke at runtime (teal row dark, "Back" label discoloured) -- something in
# the engine's upload path tops out at 512x64. Fit new art into y48..63.

DIGIT_CELL0, NDIGITS = 20, 10                  # white digits are cells 20..29
ROW3_Y = 32
TEAL_ROW_Y = 48
# y=32 band: red digits x160..319 | red MISS!! x320 | yellow MISS!! x352.
# y=48 band: teal digits x160..319 | yellow(heal-arm) digits x320..479. Both
# ride the GREEN heal bank (cells 40..49) and the renderer's cell-base
# subtractor is a CONSTANT 30 (live: def id fields ignored; popup samples
# U + (cell-30)*16). pixel_x = def.U + (10+digit)*16, so U=0 -> x160 (teal),
# U=160 -> x320 (yellow). The heal-arm yellow def lets the Grand Master's
# "attack gained" number float UP in yellow (like teal), not down like damage.
RED_DIGIT_X, WHITE_DIGIT_X, TEAL_DIGIT_X, YELLOWD_DIGIT_X = 160, 320, 160, 320
MISS_SRC = (320, 16, 32, 16)                   # x, y, w, h of the white MISS!!
MISS_RED_X, MISS_YELLOW_X = 320, 352

# Grown-table def indices (24 = inert dummy for the native kind-0x18 spawn).
DEF_COUNT_OLD, DEF_COUNT_NEW, DEF_DUMMY = 24, 30, 24
DEF_WHITE_DIGITS, DEF_RED_DIGITS, DEF_TEAL_DIGITS = 23, 25, 26
DEF_YELLOWD_DIGITS = 27                         # heal-arm yellow digit def (Master)
DEF_WHITE_MISS, DEF_RED_MISS, DEF_YELLOW_MISS = 11, 28, 29

# Ramps replacing the white glyph shades. Index 1 (black outline) is NOT in the
# map: recolouring it makes the glyphs unreadable (playtested).
RED_RAMP = {20: (255, 48, 48, 255), 22: (214, 34, 34, 255), 23: (182, 26, 26, 255),
            24: (148, 18, 18, 255), 25: (112, 12, 12, 255)}
YELLOW_RAMP = {20: (255, 213, 0, 255), 22: (252, 182, 0, 255), 23: (202, 124, 0, 255),
               24: (172, 90, 0, 255), 25: (98, 49, 0, 255)}
# Teal (Crimson Wizard mana-restore numbers). Same shade structure as the others.
TEAL_RAMP = {20: (0, 208, 208, 255), 22: (0, 176, 176, 255), 23: (0, 148, 148, 255),
             24: (0, 120, 120, 255), 25: (0, 92, 92, 255)}

# --- thief-steal loot-cue icons (see steal-sprite-cue memory) ------------------
# A rarity-coded icon pops over the thief at battle start: bag = common,
# coin = rare, gem = super-rare (Stealth Ninja Scroll tier).
#
# PIXELS ONLY -- no def/AOB edits. The first ship statically cloned defs
# 13/16/17 and LIVE-BROKE enemy status balloons (defs 13-22 ARE the enemy
# status balloons; the gem flashed over sleeping goblins, 2026-07-23). The
# icon def is borrowed at RUNTIME instead: the client (_arm_steal_icon) pokes
# the resident OTI's red-MISS def (STEAL_BORROW_DEF -- rarest spawn) to point
# at the chosen rarity's cell, arms the SPRB mailbox with that kind, and
# restores the def at battle end. While borrowed, a real red-tinted MISS!!
# would draw as the icon (rare, cosmetic).
# Row strings are 1-based indices into `colors` (0 = transparent); black/white
# reuse the atlas's idx 1 / 20.
STEAL_ICON_Y, STEAL_ICON_X0, STEAL_DX, STEAL_DY = ROW3_Y, 0, 48, -32
STEAL_PLACEMENT = ("coin", "bag", "gem")           # atlas cell order @ x = 0,16,32
STEAL_BORROW_DEF = DEF_RED_MISS                    # runtime-borrowed for the icon (def 28)
STEAL_ICONS = {
    "coin": (
        [(0, 0, 0, 255), (172, 90, 0, 255), (252, 182, 0, 255), (255, 255, 255, 255), (255, 213, 0, 255)],
        ['0000000000000000', '0000111111110000', '0001122222211000', '0012233333322100', '0112344553332110', '0123344555533210', '0123355225533210', '0123555225553210', '0123555225553210', '0123355225533210', '0123355555533210', '0112333553332110', '0012233333322100', '0001122222211000', '0000111111110000', '0000000000000000'],
    ),
    "bag": (
        [(0, 0, 0, 255), (172, 90, 0, 255), (202, 124, 0, 255), (252, 182, 0, 255), (255, 213, 0, 255)],
        ['0000000000000000', '0000000000000000', '0000001110000000', '0000012221000000', '0000012221000000', '0000122222110000', '0001322222431000', '0013444444443100', '0134444444444310', '0134444544444310', '0134444544444310', '0134444544444310', '0134444444444310', '0013444444443100', '0001344444431000', '0000133333310000'],
    ),
    "gem": (
        [(0, 0, 0, 255), (34, 92, 190, 255), (255, 255, 255, 255), (70, 150, 240, 255), (150, 210, 255, 255)],
        ['0000000000000000', '0000000110000000', '0000001221000000', '0000012222100000', '0000123332210000', '0001223334221000', '0012244554422100', '0122445555442210', '0122445555442210', '0012244554422100', '0001224444221000', '0000122442210000', '0000012222100000', '0000001221000000', '0000000110000000', '0000000000000000'],
    ),
}
_STEAL_SHARED = {(0, 0, 0, 255): 1, (255, 255, 255, 255): 20}   # black/white = atlas idx


def _pu16(b, o, v): b[o:o + 2] = struct.pack("<H", v)
def _pu32(b, o, v): b[o:o + 4] = struct.pack("<I", v)


# Images are flat row-major bytearrays of length w*h (index = y*w + x). PSP
# textures are stored swizzled in 16-wide x 8-tall blocks; these move pixels
# between that block order and plain row-major (the numpy transpose this file
# used to do, rewritten with slice copies so the frozen client needs no numpy).
def _unswizzle(raw, w, h):
    img = bytearray(w * h)
    i = 0
    for by in range(h // 8):
        for bx in range(w // 16):
            for iy in range(8):
                row = (by * 8 + iy) * w + bx * 16
                img[row:row + 16] = raw[i:i + 16]
                i += 16
    return img


def _swizzle(img, w, h):
    out = bytearray(w * h)
    i = 0
    for by in range(h // 8):
        for bx in range(w // 16):
            for iy in range(8):
                row = (by * 8 + iy) * w + bx * 16
                out[i:i + 16] = img[row:row + 16]
                i += 16
    return bytes(out)


def _get_rect(img, w, x, y, rw, rh):
    out = bytearray(rw * rh)
    for r in range(rh):
        s = (y + r) * w + x
        out[r * rw:(r + 1) * rw] = img[s:s + rw]
    return out


def _put_rect(img, w, x, y, rw, rh, data):
    for r in range(rh):
        d = (y + r) * w + x
        img[d:d + rw] = data[r * rw:(r + 1) * rw]


def _build_gim(dec):
    img = _unswizzle(dec[GIM_OFF + 0x80: GIM_OFF + 0x80 + W * H_OLD], W, H_OLD)
    pal = bytearray(dec[GIM_OFF + 0x40d0: GIM_OFF + 0x40d0 + 0x400])
    used = set(img)
    free = [i for i in range(256) if i not in used]
    _icon_colors = {rgba for _n, (cs, _r) in STEAL_ICONS.items() for rgba in cs
                    if rgba[3] and rgba not in _STEAL_SHARED}
    if len(free) < (len(RED_RAMP) + len(YELLOW_RAMP) + len(TEAL_RAMP)
                    + len(_icon_colors)):
        raise ValueError("BATTLEICON palette has no room for the recolours")

    def alloc(ramp):
        out = {}
        for white_idx, rgba in ramp.items():
            idx = free.pop(0)
            out[white_idx] = idx
            pal[idx * 4: idx * 4 + 4] = bytes(rgba)
        return out

    red, yellow, teal = alloc(RED_RAMP), alloc(YELLOW_RAMP), alloc(TEAL_RAMP)

    def recolour(src, remap):
        out = bytearray(src)
        for i, v in enumerate(out):
            nv = remap.get(v)
            if nv is not None:
                out[i] = nv
        return out

    big = bytearray(W * H_NEW)
    big[:W * H_OLD] = img
    for k in range(NDIGITS):
        cy, cx = divmod(DIGIT_CELL0 + k, 32)
        cell = _get_rect(img, W, cx * 16, cy * 16, 16, 16)
        _put_rect(big, W, RED_DIGIT_X + k * 16, ROW3_Y, 16, 16, recolour(cell, red))
        _put_rect(big, W, TEAL_DIGIT_X + k * 16, TEAL_ROW_Y, 16, 16, recolour(cell, teal))
        _put_rect(big, W, YELLOWD_DIGIT_X + k * 16, TEAL_ROW_Y, 16, 16, recolour(cell, yellow))
    mx, my, mw, mh = MISS_SRC
    miss = _get_rect(img, W, mx, my, mw, mh)
    for dst_x, remap in ((MISS_RED_X, red), (MISS_YELLOW_X, yellow)):
        _put_rect(big, W, dst_x, ROW3_Y, mw, mh, recolour(miss, remap))

    # thief-steal loot icons into the y=32 free band (x0..159). Allocate a
    # palette index per distinct icon colour (black/white reuse the atlas idx).
    icon_pal = dict(_STEAL_SHARED)
    for _name, (colors, _rows) in STEAL_ICONS.items():
        for rgba in colors:
            if rgba[3] and rgba not in icon_pal:
                idx = free.pop(0)
                icon_pal[rgba] = idx
                pal[idx * 4: idx * 4 + 4] = bytes(rgba)
    for ci, name in enumerate(STEAL_PLACEMENT):
        colors, rows = STEAL_ICONS[name]
        cell = bytearray(16 * 16)
        for y, row in enumerate(rows):
            for x, ch in enumerate(row):
                c = int(ch, 16)
                cell[y * 16 + x] = 0 if c == 0 else icon_pal[colors[c - 1]]
        _put_rect(big, W, STEAL_ICON_X0 + ci * 16, STEAL_ICON_Y, 16, 16, cell)

    nbytes = W * H_NEW
    # GIM header surgery, field map: image block sizes at chunk+0x04/+0x08
    # (block size incl. its 0x50 sub-header), height u16 at +0x1A, image
    # data size at +0x30 (0x40-biased). File-level sizes at gim+0x14
    # (len-0x10) and gim+0x24 (len-0x20). The palette block follows the
    # image data: two 0x50 headers bracket the pixel payload, hence pal_off.
    gim = bytearray(dec[GIM_OFF: GIM_OFF + 0x30])
    chunk = bytearray(dec[GIM_OFF + 0x30: GIM_OFF + 0x80])
    _pu32(chunk, 0x04, 0x50 + nbytes)
    _pu32(chunk, 0x08, 0x50 + nbytes)
    _pu16(chunk, 0x1a, H_NEW)
    _pu32(chunk, 0x30, 0x40 + nbytes)      # image DATA SIZE -- must grow with the pixels
    gim += chunk
    gim += _swizzle(big, W, H_NEW)
    gim += dec[GIM_OFF + 0x4080: GIM_OFF + 0x44e0]
    _pu32(gim, 0x14, len(gim) - 0x10)
    _pu32(gim, 0x24, len(gim) - 0x20)
    pal_off = 0x30 + 0x50 + nbytes + 0x50
    gim[pal_off: pal_off + 0x400] = bytes(pal)
    return bytes(gim)


def _build_oti(dec):
    """Grown OTI: all 24 vanilla defs verbatim + appended defs 24..29."""
    old = dec[OTI_OFF: OTI_OFF + OTI_SIZE]
    cnt = struct.unpack_from("<I", old, 0)[0]
    if cnt != DEF_COUNT_OLD:
        raise ValueError(f"BATTLEICON.OTI def count {cnt} != {DEF_COUNT_OLD}")
    offs = [struct.unpack_from("<I", old, 4 + 4 * i)[0] for i in range(cnt)]
    ends = offs[1:] + [OTI_SIZE]
    defs = [old[offs[i]:ends[i]] for i in range(cnt)]

    # 14-byte OTI def record: s16 dx, s16 dy, u16 atlas u (+4), u16 atlas v
    # (+6), u16 w (+8), u16 h (+10), u16 id (+12); a def entry holds one
    # record per sprite piece. Same record ApClient._arm_steal_icon_now
    # pokes in the resident copy at runtime.
    def variant(src, u, v, shift=False):
        var = bytearray(src)
        for k in range(len(var) // 14):
            cur_u = struct.unpack_from("<H", var, k * 14 + 4)[0]
            _pu16(var, k * 14 + 4, cur_u + u if shift else u)
            _pu16(var, k * 14 + 6, v)
        return bytes(var)

    def inert(src):
        var = bytearray(src)
        for k in range(len(var) // 14):
            _pu16(var, k * 14 + 8, 0)              # W = 0 -> draws nothing
            _pu16(var, k * 14 + 10, 0)             # H = 0
        return bytes(var)

    wd, wm = defs[DEF_WHITE_DIGITS], defs[DEF_WHITE_MISS]
    # digits are cell-addressed -> shift U; MISS!! is not -> absolute U.
    # Green-bank cells 40..49, constant-30 subtractor (renderer ignores the def
    # id, live): pixel_x = def.U + (cell-30)*16 = def.U + 160 + digit*16. U=0 ->
    # x160 (teal), U=160 -> x320 (yellow); both at V=48.
    appended = {
        DEF_DUMMY:           inert(wm),
        DEF_RED_DIGITS:      variant(wd, RED_DIGIT_X - WHITE_DIGIT_X, ROW3_Y, shift=True),
        DEF_TEAL_DIGITS:     variant(wd, 0, TEAL_ROW_Y),
        DEF_YELLOWD_DIGITS:  variant(wd, YELLOWD_DIGIT_X - 160, TEAL_ROW_Y),
        DEF_RED_MISS:        variant(wm, MISS_RED_X, ROW3_Y),
        DEF_YELLOW_MISS:     variant(wm, MISS_YELLOW_X, ROW3_Y),
    }
    new_defs = defs + [appended[i] for i in range(DEF_COUNT_OLD, DEF_COUNT_NEW)]
    hdr = 4 + 4 * DEF_COUNT_NEW
    out = bytearray(hdr)
    _pu32(out, 0, DEF_COUNT_NEW)
    pos = hdr
    for i, d in enumerate(new_defs):
        _pu32(out, 4 + 4 * i, pos)
        pos += len(d)
    for d in new_defs:
        out += d
    # NO steal-icon defs: the icon def is still borrowed at runtime by the
    # client (see STEAL_BORROW_DEF above) -- the borrow reads offsets by index
    # from the resident OTI, so it follows the grown layout automatically.
    return bytes(out)


def _build_aob(dec):
    """Grown AOB: all 24 vanilla records verbatim + appended records 24..29,
    PLUS the anim-script tail relocated intact.

    AOB layout (decoded live 2026-07-24 from the resident vanilla copy):
      header 0xc: u32 ver | u16 rec count | u16 ANIM count (13) |
                  u16 rec-offset-table word offset (6 = 0xc) |
                  u16 ANIM-offset-table word offset
      rec offsets (count u16, word offsets) | records |
      anim offsets (13 u16, word offsets)   | anim scripts
    Each anim script = {u16 0x40, u16 0x40, u16 nframes, nframes x (u16 rec,
    u16 duration)} -- these drive the status BALLOONS (script 9 plays recs
    15/16 = sleep Zz, script 11 plays recs 19/20 = blind sunglasses). The
    first grown build treated the anim-offset-table pointer as an
    "end of records" marker and DROPPED the whole anim tail -> every enemy
    status balloon rendered the cell-0 "..." bubble (live 2026-07-24)."""
    old = dec[AOB_OFF: AOB_OFF + AOB_SIZE]
    cnt = struct.unpack_from("<H", old, 4)[0]
    if cnt != DEF_COUNT_OLD:
        raise ValueError(f"BATTLEICON.AOB record count {cnt} != {DEF_COUNT_OLD}")
    anim_cnt = struct.unpack_from("<H", old, 6)[0]
    anim_tbl = struct.unpack_from("<H", old, 0x0a)[0]     # word offset
    woffs = [struct.unpack_from("<H", old, 0x0c + 2 * i)[0] for i in range(cnt)]
    recs = []
    for i in range(cnt):
        start = woffs[i] * 2
        end = (woffs[i + 1] if i + 1 < cnt else anim_tbl) * 2
        recs.append(old[start:end])
    anim_offs = [struct.unpack_from("<H", old, anim_tbl * 2 + 2 * i)[0]
                 for i in range(anim_cnt)]
    scripts_start = anim_tbl * 2 + 2 * anim_cnt
    if anim_offs[0] * 2 != scripts_start:
        raise ValueError("AOB anim table not contiguous with its scripts")
    anim_blob = old[scripts_start:]                        # scripts, verbatim

    rd, rm = recs[DEF_WHITE_DIGITS], recs[DEF_WHITE_MISS]
    appended = {DEF_DUMMY: rm, DEF_RED_DIGITS: rd, DEF_TEAL_DIGITS: rd,
                DEF_YELLOWD_DIGITS: rd, DEF_RED_MISS: rm, DEF_YELLOW_MISS: rm}
    new_recs = recs + [appended[i] for i in range(DEF_COUNT_OLD, DEF_COUNT_NEW)]

    # header is u16-aligned (hence the % 2 round-up); every table in the AOB
    # stores WORD offsets, so positions below are written as pos // 2.
    hdr_len = 0x0c + 2 * DEF_COUNT_NEW
    hdr_len += hdr_len % 2
    out = bytearray(hdr_len)
    out[:0x0c] = old[:0x0c]
    _pu16(out, 4, DEF_COUNT_NEW)
    pos = hdr_len
    for i, r in enumerate(new_recs):
        if pos % 2:
            raise ValueError("AOB record misaligned")
        _pu16(out, 0x0c + 2 * i, pos // 2)
        pos += len(r)
    for r in new_recs:
        out += r
    if len(out) % 2:
        raise ValueError("AOB anim table misaligned")
    _pu16(out, 0x0a, len(out) // 2)                 # anim-offset-table pointer
    # anim-script entries are WORD offsets too, so the rebase delta is the
    # byte displacement of the script blob divided by 2 (u16 units).
    delta = (len(out) + 2 * anim_cnt - scripts_start) // 2
    for o in anim_offs:
        out += struct.pack("<H", o + delta)         # rebase into the new tail
    out += anim_blob
    return bytes(out)


def build_bundle(vanilla_blob: bytes) -> bytes:
    """Return the recoloured BATTLEICON bundle, decompressed. The AOB and OTI
    grow, so every section is laid out fresh and the index entries (offset +
    both size fields) are rewritten for all four members."""
    dec = wp16.decompress(vanilla_blob)
    gim, oti, aob = _build_gim(dec), _build_oti(dec), _build_aob(dec)

    def pad(buf):
        buf += b"\0" * ((-len(buf)) % 0x20)

    out = bytearray(dec[:AOB_OFF])                 # bundle index header
    aob_off = len(out)
    out += aob
    pad(out)
    oti_off = len(out)
    out += oti
    pad(out)
    gim_off = len(out)
    out += gim
    pad(out)
    jpn_off = len(out)
    jpn = bytearray(dec[JPN_OFF:])
    jpn[0x80:] = b"\0" * (len(jpn) - 0x80)         # English-only: drop JPN pixels
    out += jpn

    def entry(name, off, size):
        p = out.find(name.encode())
        if p < 0:
            raise ValueError(f"{name} missing from BATTLEICON index")
        base = p + 22
        _pu32(out, base + 2, off)
        _pu32(out, base + 6, size)
        _pu32(out, base + 10, size)

    entry("BATTLEICON.AOB", aob_off, len(aob))
    entry("BATTLEICON.OTI", oti_off, len(oti))
    entry("BATTLEICON_ENG.GIM", gim_off, len(gim))
    entry("BATTLEICON_JPN.GIM", jpn_off, len(jpn))
    _pu32(out, 4, len(out))
    return bytes(out)


def bake_popup_colours(iso_path: str):
    """Rewrite BATTLEICON.PCK in `iso_path` with the recoloured atlas + defs."""
    with open(iso_path, "r+b") as f:
        f.seek(ISO_OFF)
        cur = f.read(REC_SIZE)
        if cur[:4] != b"Wp16":
            raise ValueError(f"{ISO_OFF:#x} is not a Wp16 blob (wrong disc/layout)")
        dec_head = wp16.decompress(cur)[:AOB_OFF]
        if b"BATTLEICON" not in dec_head:
            raise ValueError(f"{ISO_OFF:#x} is not the BATTLEICON bundle")
        blob = wp16.compress(build_bundle(cur), pad_to=REC_SIZE)
        if len(blob) != REC_SIZE:
            raise ValueError(f"recompressed bundle {len(blob):#x} != slot {REC_SIZE:#x}")
        f.seek(ISO_OFF)
        f.write(blob)
    return ISO_OFF
