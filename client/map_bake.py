"""On-disc field-map GRID edits (dpk side of the giant_cave_gate feature).

A dungeon's walkable grid lives in MAP_<n>_<m>_<k>_AMD.BIN inside the map's
wp16-compressed MAP_<n>_<m>.PCK, which in turn lives in ff1psp.dpk inside the
ISO. Layout: an 8-byte AMD header (u32 flags, u16 W, u16 H) then W*H u16 tile
ids, row-major -- so cell (x,y) is at 8 + y*W*2 + x*2.

Giant's Cave (map index 40 = MAP_06_00.PCK, FIELD_MAP_ID 0x22, grid 34x35) gets
ONE tile changed: (11,13) 0x006B -> 0x0055, a solid lone-boulder tile. 0x0055 is
already used elsewhere in this same map ((13,9) and (11,15)) and its ATT entry is
already 0xF000 (impassable), so no ATT edit is needed and no new tile art is
referenced. Together with the Giant moved onto (12,13) by
iso_patcher.apply_giant_cave_gate, this seals the four Giant's Cave chests behind
Titan; the rock is permanent (the Giant despawning when fed reopens (12,13)).

Same in-place strategy as campus_bake/extern_bake: decompress, edit, recompress,
write back into the SAME dpk record slot zero-padded, so neither the dpk nor the
ISO changes size (no LBA-shift repacker). OWNERSHIP (see [[dpk-dead-space-
ownership]]): this module is the only writer of MAP_*.PCK -- ms2_bake's donor
pool is FM_DBG_*/FM_*J1/J2 packs and extern_bake reserves FM_EXTERN*J1, so
nothing here can be stolen or collide. It also never grows a record, so it
cannot take space from anyone else. The edit does not change the
decompressed length, so the record's +32 DECOMPRESSED-size field is unchanged --
it is rewritten anyway (from the real blob length) so this stays correct if the
edit ever grows. RAISES on any mismatch: a half-applied gate is worse than none.
"""
import struct

from . import wp16
from .extern_bake import _find_dpk, _dpk_records

_PCK = "MAP_06_00.PCK"
_AMD = b"MAP_06_00_00_AMD.BIN"
_REC_STRIDE = 36              # PCK inner index from 0x10: name[24] + u32 off + u32 size

GIANT_CAVE_W, GIANT_CAVE_H = 34, 35
ROCK_XY = (11, 13)
ROCK_VANILLA_TILE = 0x006B
ROCK_TILE = 0x0055            # solid lone boulder; ATT already 0xF000


def _find_subfile(blob, name):
    """(dec offset, size) of the PCK inner-directory entry `name`."""
    cnt = struct.unpack_from("<I", blob, 0)[0]
    for k in range(cnt):
        p = 0x10 + k * _REC_STRIDE
        if blob[p:p + 24].split(b"\0")[0] == name:
            return struct.unpack_from("<II", blob, p + 24)
    raise KeyError(f"{name!r} not in PCK directory")


def build_gated_map06(blob):
    """Return the MAP_06_00 blob with the Giant's Cave boulder placed.
    Idempotent: a blob that already carries the rock is returned unchanged."""
    blob = bytearray(blob)
    doff, dsz = _find_subfile(blob, _AMD)
    _flags, w, h = struct.unpack_from("<IHH", blob, doff)
    if (w, h) != (GIANT_CAVE_W, GIANT_CAVE_H) or dsz != 8 + w * h * 2:
        raise ValueError(f"{_AMD.decode()}: unexpected grid {w}x{h} size {dsz}")
    x, y = ROCK_XY
    cell = doff + 8 + y * (w * 2) + x * 2
    cur = struct.unpack_from("<H", blob, cell)[0]
    if cur == ROCK_TILE:
        return bytes(blob)                          # already gated
    if cur != ROCK_VANILLA_TILE:
        raise ValueError(f"Giant's Cave ({x},{y}) is {cur:#06x}, expected "
                         f"vanilla {ROCK_VANILLA_TILE:#06x}")
    struct.pack_into("<H", blob, cell, ROCK_TILE)
    return bytes(blob)


def place_giant_rock(iso_path, log=print):
    """Bake the Giant's Cave choke-point boulder into `iso_path` (in place, same
    dpk record slot). Raises loudly rather than shipping a half-applied gate."""
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        rec_off, data_off, data_size = _dpk_records(dpk)[_PCK]
        blob = wp16.decompress(bytes(dpk[data_off:data_off + data_size]))
        new_blob = build_gated_map06(blob)
        comp = wp16.compress(new_blob)
        if wp16.decompress(comp) != new_blob:
            raise ValueError(f"{_PCK}: wp16 round-trip mismatch")
        if len(comp) > data_size:
            raise ValueError(f"{_PCK} gated PCK {len(comp)} > slot {data_size}")
        dpk[data_off:data_off + data_size] = comp + b"\x00" * (data_size - len(comp))
        struct.pack_into("<I", dpk, rec_off + 32, len(new_blob))   # DECOMPRESSED size
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[map_bake] Giant's Cave boulder at {ROCK_XY} "
        f"(compressed {len(comp)}/{data_size})")


# --------------------------------------------------------------------------
# Canal FORD -- single-tile walkable crossing with distinct water art, on-disc in
# MAP_00.PCK. Live-designed against a running game + user-approved 2026-07-26.
#
# The Nerrick canal, once blown open, is a 3x2 block of globally-unique tile ids:
#   y156 (banks)   (93,156)=0x163  (94,156)=0x164  (95,156)=0x165
#   y157 (channel) (93,157)=0x173  (94,157)=0x174  (95,157)=0x175
# The ship sails the y157 channel. (94,156)=0x164 is ALREADY walkable in vanilla
# (attr 0x0002), so making the single cell (94,157)=0x174 walk+sail turns column
# x=94 into a clean 1-tile FORD: forest(94,155) -> 0x164 -> 0x174 -> forest(94,158).
# Its two neighbours 0x173/0x175 keep vanilla 0xF00F = ship-only, no foot, so the
# crossing is ONE tile wide rather than three (user request). Every other canal id
# is left BIT-FOR-BIT VANILLA.
#
# Three coordinated edits, all on tile id 0x174 alone:
#   1. ATT -> 0x000F (walk + sail).
#   2. ASC -> a copy of donor 0xA3's subtile assembly (the light-cyan shallow art
#      the Duergar pass uses). Copying a working ASC entry verbatim needs no
#      understanding of the subtile-word bit format.
#   3. ANM.ATI -> move 0x174 out of the deep-ocean animation family and into the
#      RIVER family. THIS is the edit that actually changes what you see: the
#      overworld composites a per-frame ANIMATION OVERLAY over every tile id
#      registered in MAP_00_ANM.ATI, and the ocean overlay was painting over the
#      base art -- which is why an ASC-only reskin was provably INERT in game (see
#      [[canal-shallows-plan]]). Registered in the river family the cell renders as
#      animated river water, clearly distinct from the surrounding ocean.
#
# ATI FORMAT (cracked 2026-07-26, validated against all 50 records): u32 count,
# u32 offsets[count] (ATI-relative), then the records. Each record is a 0x1C-byte
# header [cnt, type, -1, flag, 0, param, 0] followed by `cnt` member tile ids at
# STRIDE 8 (u32 id + u32 pad) EXCEPT the last member, which carries no trailing
# pad -- so record length == 0x1C + (cnt-1)*8 + 4.
#
# NET-ZERO SIZE: removing 0x174 from the ocean record (-8 bytes) and appending it
# to the river record (+8 bytes) leaves the ATI subfile length UNCHANGED, so the
# enclosing PCK's inner-directory offsets never move. This matters: MAP_00_ASC.BIN
# begins only 4 bytes after the ATI ends, so a grown ATI would have collided with
# it and required rebuilding the whole PCK directory.
#
# SAFE by construction: the canal ids exist in the grid ONLY while the canal is
# OPEN -- the closed state uses different land ids that we never touch, so before
# the player blows the canal this area stays an ordinary walkable foot path (user
# requirement). Id 0x174 > 255, so the ATT edit lands OUTSIDE the client's
# 512-byte ATT_PREFIX signature: the openworld loop's ATT sig-scan is unaffected,
# and its CANAL_ATT poke (kept as a belt-and-suspenders fallback this round)
# writes the same 0x000F to the same id. COSMETIC and non-load-bearing: a failure
# here is swallowed by the caller and never aborts the bake.
_PCK00 = "MAP_00.PCK"
_ASC00 = b"MAP_00_ASC.BIN"
_ATT00 = b"MAP_00_ATT.BIN"
_ATI00 = b"MAP_00_ANM.ATI"
_ASC_HDR = 12                # ASC.BIN: 12-byte header, then 512 * 8 u16 entries
_ASC_STRIDE = 16             # 8 u16 = 16 bytes per tile id
_ATI_HDR = 0x1C              # per-record header size
_ATI_STRIDE = 8              # member entry stride (last member carries no pad)

CANAL_FORD_ID = 0x174        # (94,157) -- the single cell that becomes the ford
CANAL_FORD_ATTR = 0x000F     # walk + sail
CANAL_ART_DONOR = 0x0A3      # light-cyan shallow tile; its ASC is copied verbatim
CANAL_ANIM_ANCHOR = 0x1F0    # dominant river tile -> identifies the river ATI record
# Canal ids deliberately left vanilla (asserted by the test_patch regression)
CANAL_UNTOUCHED_IDS = (0x163, 0x164, 0x165, 0x173, 0x175)


def _ati_records(ati):
    """[(index, start, end, member_count)] for an ATI blob, validating the format."""
    n_recs = struct.unpack_from("<I", ati, 0)[0]
    offs = list(struct.unpack_from(f"<{n_recs}I", ati, 4))
    out = []
    for ri, (a, b) in enumerate(zip(offs, offs[1:] + [len(ati)])):
        cnt = struct.unpack_from("<I", ati, a)[0]
        if _ATI_HDR + (cnt - 1) * _ATI_STRIDE + 4 != b - a:
            raise ValueError(f"ATI record {ri}: cnt {cnt} inconsistent with "
                             f"length {b - a:#x}")
        out.append((ri, a, b, cnt))
    return out


def _ati_members(ati, start, cnt):
    """The `cnt` member tile ids of the record beginning at `start`."""
    return [struct.unpack_from("<I", ati, start + _ATI_HDR + k * _ATI_STRIDE)[0]
            for k in range(cnt)]


def _ati_owner(ati, tile_id):
    """(start, cnt, member_index) of the record registering `tile_id`, or None."""
    for _ri, a, _b, cnt in _ati_records(ati):
        members = _ati_members(ati, a, cnt)
        if tile_id in members:
            return a, cnt, members.index(tile_id)
    return None


def _ati_move_member(ati, tile_id, anchor_id):
    """Move `tile_id` out of whichever animation record owns it into the record that
    contains `anchor_id`. Returns a new blob of exactly the SAME length."""
    ati = bytes(ati)
    recs = _ati_records(ati)
    src = dst = None
    for ri, a, b, cnt in recs:
        members = _ati_members(ati, a, cnt)
        if tile_id in members:
            src = (ri, a, b, cnt, members.index(tile_id))
        if anchor_id in members:
            dst = (ri, a, b, cnt)
    if src is None:
        raise ValueError(f"ATI: tile {tile_id:#x} is not registered in any record")
    if dst is None:
        raise ValueError(f"ATI: anchor {anchor_id:#x} is not registered in any record")
    if src[0] == dst[0]:
        raise ValueError(f"ATI: {tile_id:#x} already shares {anchor_id:#x}'s record")

    def rebuild(start, members):
        """Serialise one record with a new member list, preserving its header."""
        body = bytearray(ati[start:start + _ATI_HDR])
        struct.pack_into("<I", body, 0, len(members))
        for k, mid in enumerate(members):
            body += struct.pack("<I", mid)
            if k != len(members) - 1:
                body += b"\x00\x00\x00\x00"       # last member carries no pad
        return bytes(body)

    src_members = _ati_members(ati, src[1], src[3])
    src_members.pop(src[4])                                       # de-register: -8 bytes
    dst_members = _ati_members(ati, dst[1], dst[3]) + [tile_id]   # register:    +8 bytes
    pieces = {src[0]: rebuild(src[1], src_members),
              dst[0]: rebuild(dst[1], dst_members)}

    out = bytearray(ati[:recs[0][1]])            # count + offset table, patched below
    new_offs = []
    for ri, a, b, _cnt in recs:
        new_offs.append(len(out))
        out += pieces.get(ri, ati[a:b])
    for ri, off in enumerate(new_offs):
        struct.pack_into("<I", out, 4 + ri * 4, off)
    if len(out) != len(ati):
        raise ValueError(f"ATI size changed {len(ati):#x} -> {len(out):#x} "
                         f"(the move must be net-zero)")
    _ati_records(bytes(out))                     # re-validate every record
    return bytes(out)


def build_canal_ford(blob):
    """Return the MAP_00 blob carrying the single-tile canal ford (attr + art +
    river animation). Idempotent: an already-forded blob is returned unchanged."""
    blob = bytearray(blob)
    aoff, _asz = _find_subfile(blob, _ASC00)
    toff, _tsz = _find_subfile(blob, _ATT00)
    ioff, isz = _find_subfile(blob, _ATI00)
    ford = aoff + _ASC_HDR + CANAL_FORD_ID * _ASC_STRIDE
    donor = bytes(blob[aoff + _ASC_HDR + CANAL_ART_DONOR * _ASC_STRIDE:][:_ASC_STRIDE])
    ati = bytes(blob[ioff:ioff + isz])

    owner = _ati_owner(ati, CANAL_FORD_ID)
    anchor = _ati_owner(ati, CANAL_ANIM_ANCHOR)
    if anchor is None:
        raise ValueError(f"ATI: river anchor {CANAL_ANIM_ANCHOR:#x} not registered")
    if (bytes(blob[ford:ford + _ASC_STRIDE]) == donor
            and struct.unpack_from("<H", blob, toff + CANAL_FORD_ID * 2)[0] == CANAL_FORD_ATTR
            and owner is not None and owner[0] == anchor[0]):
        return bytes(blob)                                        # already baked

    blob[ford:ford + _ASC_STRIDE] = donor                                     # 1. art
    struct.pack_into("<H", blob, toff + CANAL_FORD_ID * 2, CANAL_FORD_ATTR)   # 2. attr
    blob[ioff:ioff + isz] = _ati_move_member(                                 # 3. anim
        ati, CANAL_FORD_ID, CANAL_ANIM_ANCHOR)
    return bytes(blob)


# --------------------------------------------------------------------------
# OPEN-PROGRESSION grid edits ON DISC -- foot trails, canoe rivers and the
# northern docks, baked into MAP_00_AMD.BIN instead of being maintained by the
# client's _openworld_loop.
#
# WHY: the loop pokes the DECOMPRESSED heap arena, which the game re-decompresses
# to a fresh address on (some) overworld loads. Every failure mode the feature has
# ever had is downstream of that -- a freed copy still holding our edits makes the
# canary read clean while the live arena renders vanilla, and a write that lands
# after a chunk is drawn does not repaint until that chunk scrolls out and back.
# The 2026-08-08 report (northern docks absent until a game restart) is exactly
# that. Baked here the cells are correct BEFORE the arena is ever decompressed, on
# every load, with no loop, no relocation hazard and no repaint hazard.
#
# The client loop is deliberately KEPT as a repair path: it writes the same values
# to the same cells, so on a baked ISO it is a no-op, and it still covers an
# in-progress seed whose ISO predates this bake.
#
# GRID LAYOUT: MAP_00_AMD.BIN is 0x1fc0c bytes = a 10-byte header then 255*255 u16
# tile ids, row-major (stride 510). Note the 10 -- the dungeon AMDs in this same
# PCK use the documented 8-byte header (u32 flags, u16 W, u16 H) and their size is
# 8 + W*H*2 exactly, but MAP_00's is 10 + W*H*2 exactly. Rather than trust either
# number we VERIFY: openworld_data.ANCHOR is 20 bytes of vanilla row 132 that the
# client already uses to locate the arena in RAM, and its offset is relative to the
# grid DATA start -- so if the anchor reads correctly at header+ANCHOR_OFF the
# header size and stride are both confirmed before any write.
#
# SIZE: the PCK slot is 0x54100 with ~1.8 KB of slack over an identity recompress,
# and these are ~50 cells out of 65025, so the delta is tiny -- but it is checked,
# and an overflow raises rather than corrupting the dpk.
_AMD00 = b"MAP_00_AMD.BIN"
_AMD00_HDR = 10               # verified against openworld_data.ANCHOR, see above


def _ow_edits(early, extended, docks):
    """The {(x, y): tile_id} union the three open-progression toggles select --
    the same union, in the same precedence order, as ApClient._openworld_loop."""
    from . import openworld_data as OW
    edits = {}
    if early:
        edits.update(OW.EARLY_GRID_EDITS)
    if extended:
        edits.update(OW.EXTENDED_GRID_EDITS)
    if docks:
        edits.update(OW.NORTHERN_DOCKS_GRID_EDITS)
    return edits


def build_openworld_grid(blob, edits):
    """Return the MAP_00 blob with `edits` ({(x, y): tile_id}) written into the
    overworld grid. Idempotent: an already-baked blob is returned unchanged."""
    from . import openworld_data as OW
    blob = bytearray(blob)
    doff, dsz = _find_subfile(blob, _AMD00)
    _flags, w, h = struct.unpack_from("<IHH", blob, doff)
    grid = doff + _AMD00_HDR
    if (w, h) != (OW.GRID_W, OW.GRID_W) or dsz != _AMD00_HDR + w * h * 2:
        raise ValueError(f"{_AMD00.decode()}: unexpected grid {w}x{h} size {dsz:#x}")
    if bytes(blob[grid + OW.ANCHOR_OFF:][:len(OW.ANCHOR)]) != OW.ANCHOR:
        raise ValueError(f"{_AMD00.decode()}: vanilla anchor not at "
                         f"header+{OW.ANCHOR_OFF:#x} -- header/stride assumption "
                         f"is wrong, refusing to write")
    for (x, y), tile in edits.items():
        if not (0 <= x < w and 0 <= y < h):
            raise ValueError(f"open-progression cell ({x},{y}) outside {w}x{h}")
        struct.pack_into("<H", blob, grid + y * OW.GRID_STRIDE + x * 2, tile)
    return bytes(blob)


def bake_openworld_grid(iso_path, early, extended, docks, log=print):
    """Bake the open-progression grid edits (foot trails, canoe rivers, northern
    docks) into MAP_00_AMD.BIN on disc -- see build_openworld_grid."""
    edits = _ow_edits(early, extended, docks)
    if not edits:
        return
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        rec_off, data_off, data_size = _dpk_records(dpk)[_PCK00]
        blob = wp16.decompress(bytes(dpk[data_off:data_off + data_size]))
        new_blob = build_openworld_grid(blob, edits)
        if new_blob == blob:
            log("[map_bake] overworld grid: already baked")
            return
        comp = wp16.compress(new_blob)
        if wp16.decompress(comp) != new_blob:
            raise ValueError(f"{_PCK00}: wp16 round-trip mismatch")
        if len(comp) > data_size:
            raise ValueError(f"{_PCK00} open-progression PCK {len(comp)} > "
                             f"slot {data_size}")
        dpk[data_off:data_off + data_size] = comp + b"\x00" * (data_size - len(comp))
        struct.pack_into("<I", dpk, rec_off + 32, len(new_blob))   # DECOMPRESSED size
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[map_bake] overworld grid: {len(edits)} cells baked (early={early} "
        f"extended={extended} docks={docks}; compressed {len(comp)}/{data_size})")


def bake_canal_ford(iso_path, log=print):
    """Bake the single-tile canal ford into `iso_path` (in place, same dpk record
    slot). COSMETIC/non-load-bearing -- the caller swallows failures."""
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        rec_off, data_off, data_size = _dpk_records(dpk)[_PCK00]
        blob = wp16.decompress(bytes(dpk[data_off:data_off + data_size]))
        new_blob = build_canal_ford(blob)
        if new_blob == blob:
            log("[map_bake] canal ford: already baked")
            return
        comp = wp16.compress(new_blob)
        if wp16.decompress(comp) != new_blob:
            raise ValueError(f"{_PCK00}: wp16 round-trip mismatch")
        if len(comp) > data_size:
            raise ValueError(f"{_PCK00} ford PCK {len(comp)} > slot {data_size}")
        dpk[data_off:data_off + data_size] = comp + b"\x00" * (data_size - len(comp))
        struct.pack_into("<I", dpk, rec_off + 32, len(new_blob))   # DECOMPRESSED size
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[map_bake] canal ford (94,157): tile {CANAL_FORD_ID:#05x} -> attr "
        f"{CANAL_FORD_ATTR:#06x} + art {CANAL_ART_DONOR:#05x} + river anim "
        f"(compressed {len(comp)}/{data_size})")
