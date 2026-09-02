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
# Canal CROSSING -- one walkable cell in the blown canal, on-disc in MAP_00.PCK.
# Live-designed against a running game + user-approved 2026-07-26. The ART half
# lives in the CLIENT, not here; baking it was tried and measured inert 2026-09-01.
#
# The Nerrick canal, once blown open, is a 3x2 block of globally-unique tile ids:
#   y156 (banks)   (93,156)=0x163  (94,156)=0x164  (95,156)=0x165
#   y157 (channel) (93,157)=0x173  (94,157)=0x174  (95,157)=0x175
# The ship sails the y157 channel. (94,156)=0x164 is ALREADY walkable in vanilla
# (attr 0x0002), so making the single cell (94,157)=0x174 walk+sail turns column
# x=94 into a clean 1-tile crossing: forest(94,155) -> 0x164 -> 0x174 ->
# forest(94,158). Its two neighbours 0x173/0x175 keep vanilla 0xF00F = ship-only,
# no foot, so the crossing is ONE tile wide rather than three (user request).
#
# ONE edit, on tile id 0x174 alone: ATT -> 0x000F (walk + sail). Every byte of the
# canal's ART is left BIT-FOR-BIT VANILLA, because on-disc art edits DO NOT WORK
# here -- see below.
#
# THE ART CANNOT BE BAKED AS ASC. Measured live 2026-09-01 (v308): the bridge's own
# ASC entries were copied onto the canal ids 0x164<-0x161 and 0x174<-0x171, donors
# chosen to share their target's ANM.ATI record so the overlay would match. The
# patch reached the ISO and reached RAM -- the baked bridge signature was found at
# both 0x161's and 0x164's live ASC entries -- and the canal still rendered
# unchanged. So for a tile id registered in MAP_00_ANM.ATI, ASC is not the art: the
# animation system supplies it, and moving the id to another record (what the
# RETIRED water reskin did, donor 0xA3 + a move into the river family) changes the
# look only by making it that record's animation -- which is the river the
# 2026-09-01 report photographed. Drawing a BRIDGE here on disc would need
# ANM.GIM/ATI frame semantics that are not cracked; until they are, the bridge is
# the client's grid write (openworld_data.CANAL_BRIDGE_GRID_EDITS), which writes
# tile ids the engine already draws as a bridge.
#
# The AMD grid holds the canal ids 0x163-0x165/0x173-0x175 at those cells in BOTH
# states (verified on disc), so the closed canal is not a different grid -- which is
# why the client's bridge paint keeps its canal-open gate.
#
# Id 0x174 > 255, so the ATT edit lands OUTSIDE the client's 512-byte ATT_PREFIX
# signature: the openworld loop's ATT sig-scan is unaffected, and its CANAL_ATT
# poke (the fallback for seeds whose ISO predates this bake) writes the same
# 0x000F to the same id. Non-load-bearing: a failure here is swallowed by the
# caller and never aborts the bake.
_PCK00 = "MAP_00.PCK"
_ASC00 = b"MAP_00_ASC.BIN"
_ATT00 = b"MAP_00_ATT.BIN"
_ATI00 = b"MAP_00_ANM.ATI"
_ASC_HDR = 12                # ASC.BIN: 12-byte header, then 512 * 8 u16 entries
_ASC_STRIDE = 16             # 8 u16 = 16 bytes per tile id

CANAL_CROSS_ID = 0x174       # (94,157) -- the single cell that becomes walkable
CANAL_CROSS_ATTR = 0x000F    # walk + sail
# Canal ids deliberately left vanilla (asserted by the test_patch regression)
CANAL_UNTOUCHED_IDS = (0x163, 0x164, 0x165, 0x173, 0x175)


def build_canal_crossing(blob):
    """Return the MAP_00 blob with the canal's centre channel cell walk+sail.
    Idempotent, and the ONLY bytes it touches are that tile id's ATT entry."""
    blob = bytearray(blob)
    toff, _tsz = _find_subfile(blob, _ATT00)
    struct.pack_into("<H", blob, toff + CANAL_CROSS_ID * 2, CANAL_CROSS_ATTR)
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


def bake_canal_crossing(iso_path, log=print):
    """Bake the canal's walkable centre cell into `iso_path` (in place, same dpk
    record slot). Non-load-bearing -- the caller swallows failures."""
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        rec_off, data_off, data_size = _dpk_records(dpk)[_PCK00]
        blob = wp16.decompress(bytes(dpk[data_off:data_off + data_size]))
        new_blob = build_canal_crossing(blob)
        if new_blob == blob:
            log("[map_bake] canal crossing: already baked")
            return
        comp = wp16.compress(new_blob)
        if wp16.decompress(comp) != new_blob:
            raise ValueError(f"{_PCK00}: wp16 round-trip mismatch")
        if len(comp) > data_size:
            raise ValueError(f"{_PCK00} canal PCK {len(comp)} > slot {data_size}")
        dpk[data_off:data_off + data_size] = comp + b"\x00" * (data_size - len(comp))
        struct.pack_into("<I", dpk, rec_off + 32, len(new_blob))   # DECOMPRESSED size
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[map_bake] canal crossing (94,157): tile {CANAL_CROSS_ID:#05x} -> attr "
        f"{CANAL_CROSS_ATTR:#06x}, art left vanilla "
        f"(compressed {len(comp)}/{data_size})")
