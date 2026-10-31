"""Bake a generated overworld (slot_data["randworld"]["world"]) into the ISO.

The production twin of the prototype's attic/ow_rando_v0/bake.py, pure Python
(the frozen client has no numpy). Two halves:

  apply_elf(elf, world)      ELF data the engine reads at fixed addresses:
                             map-0 entrance records, menu-map cursor targets,
                             overworld exit returns, the Mirage blocker's
                             script object, the terrain class map, and the
                             King's bridge remap (always built)
  bake_grid(iso_path, world) the tile grid itself, into MAP_00_AMD.BIN (dpk)

Every stamp the generator placed carries src (vanilla window) and dst (where
the window went), so anything that sat inside a site's vanilla window moves
with it by the same shift.
"""
import struct

from . import eboot_patch as EP

N = 255

OBJ_LIST_MAP0 = 0x0897A340
# Map 0's load script runs `30 04 04 i` (op 0x30 on field actor i, arg 4 =
# clear flag 0x20) on the six building sprites, actor i = the i-th type-2
# record. Op 0x30 traps (`b .` @0x088491EC) on an actor that does not exist,
# so a sprite record must never be removed (v355 froze every new game on a
# map that skipped one, 2026-10-29); arg 2 clears the DRAW bit instead, the
# way vanilla scripts hide NPCs (`30 04 02 i`).
MAP0_SPRITE_OPS = 0x089A5A8C
TERRAIN_MAP = 0x089B2884
MINIMAP_POINTS = 0x0893E7F8
SCRIPT_LO, SCRIPT_HI = 0x08990000, 0x089C0000
REMAP_TABLE_B = 0x08944E54
BRIDGE_TILE_IDS = (0x160, 0x161, 0x162, 0x170, 0x171, 0x172)
EXIT_TABLE_PTRS = 0x08944634
VANILLA_DECK = (144, 145)            # the King's bridge deck: the 0xfa6 first-
                                     # crossing trigger + 90 exit returns


def world_grid(world, resolve=None):
    """The disc grid. World-changing gate cells hold spare gate ids there
    (the world_gates cave remaps them); resolve="open" / "closed" puts the
    tile each shows in that state instead, for passes that read tile art or
    tile classes (terrain classes, the menu-map picture)."""
    from ..randworld import export as RWE
    grid = RWE.grid_from_slot(world)
    if resolve:
        grid = list(grid)
        for g in world.get("gates") or ():
            if g.get("name") == "canal":
                continue
            for x, y, o, c in g.get("cells") or ():
                grid[(y % N) * N + x % N] = o if resolve == "open" else c
    return grid


# ----------------------------------------------------------- ownership ---
def _in_window(gx, gy, src):
    sx0, sy0, w, h = src
    return (gx - sx0) % N < w and (gy - sy0) % N < h


def _grow(src, r):
    sx0, sy0, w, h = src
    return (sx0 - r, sy0 - r, w + 2 * r, h + 2 * r)


def _entrance_dist(p, gx, gy):
    sx0, sy0, _w, _h = p["src"]
    dx, dy = p["dst"]
    best = 99
    for ex, ey in p["ents"]:
        vx, vy = (sx0 + (ex - dx)) % N, (sy0 + (ey - dy)) % N
        ddx = min((vx - gx) % N, (gx - vx) % N)
        ddy = min((vy - gy) % N, (gy - vy) % N)
        best = min(best, max(ddx, ddy))
    return best


def owner(gx, gy, placed, grows=(0, 4)):
    """The placed stamp whose vanilla window holds (gx, gy): a record ON a
    site's own entrance belongs to that site, else the SMALLEST window
    (Duergar's window covers Lifespring's trigger, Ice's and Whisperwind's
    overlap -- both shipped the wrong record once in the prototype)."""
    for r in grows:
        hits = [p for p in placed if p["kind"] != "dock"
                and _in_window(gx, gy, _grow(p["src"], r) if r else p["src"])]
        if hits:
            own = [p for p in hits if _entrance_dist(p, gx, gy) <= 2]
            if own:
                return min(own, key=lambda p: _entrance_dist(p, gx, gy))
            return min(hits, key=lambda p: p["src"][2] * p["src"][3])
    return None


def belongs(p, gx, gy):
    """Does vanilla entrance record (gx, gy) belong to stamp p, beyond lying
    in its window? On or beside one of its entrances or kept tiles. A window
    also covers unrelated vanilla entrances (Hellfire's holds three Cardia
    dragon caves): shifted with it they land wherever the window offset says
    -- open sea, a mountain, bare grass (playtest 2026-10-17, a dragon cave
    in the middle of the water). The King's bridge carries its own trigger."""
    if p["kind"] == "bridge" or _entrance_dist(p, gx, gy) <= 2:
        return True
    sx0, sy0 = p["src"][0], p["src"][1]
    rx, ry = (gx - sx0) % N, (gy - sy0) % N
    return any(max(abs(rx - cx), abs(ry - cy)) <= 1 for cx, cy in p.get("cells") or ())


def _shift(p, gx, gy):
    sx0, sy0, _w, _h = p["src"]
    dx, dy = p["dst"]
    return (dx + (gx - sx0) % N) % N, (dy + (gy - sy0) % N) % N


def _has_bridge(placed):
    return any(p["kind"] == "bridge" for p in placed)


def _cornelia_door(placed):
    p = next(p for p in placed if p["key"] == "cornelia")
    for a in p.get("appr") or ():
        return a[0], a[1]
    return tuple(p["ents"][0])


def _park_cell(grid, att):
    """A cell inside solid mountain (5x5): a trigger there can never fire."""
    for y in range(2, N - 2):
        for x in range(2, N - 2):
            if all(att[grid[(y + b) * N + x + a]] == 0xF012
                   for a in range(-2, 3) for b in range(-2, 3)):
                return x, y
    raise ValueError("randworld: no solid mountain block to park records in")


# ----------------------------------------------------------------- ELF ---
def read_records(elf):
    o = EP.ram2file(OBJ_LIST_MAP0) + 10
    out = []
    while True:
        ty = struct.unpack_from("<H", elf, o)[0]
        if ty == 0xFFFF:
            break
        param, x, y = struct.unpack_from("<HHH", elf, o + 2)
        out.append((o, ty, param, x, y))
        o += 16 if ty == 2 else 8
    return out


def move_records(elf, placed, park, log):
    """Door triggers (type 1) of sites the map left out are parked in solid
    rock, where they never fire. Type-2 records are the six building SPRITES
    (Cornelia, Chaos, Elfheim, Citadel, Mirage Tower, W. Keep: sprite 0xbc-
    0xc1): parked, a tiny map's skipped W. Keep and Mirage Tower drew stacked
    on one mountain (playtest 2026-10-28). An unplaced sprite stays in the
    list (map 0's load script addresses the sprites by index, see
    MAP0_SPRITE_OPS), parked, and its load-script op hides it instead."""
    moved, parked, hidden = 0, [], []
    bridge = _has_bridge(placed)
    ops = EP.ram2file(MAP0_SPRITE_OPS)
    sprite = 0
    for o, ty, param, x, y in read_records(elf):
        gx, gy = x - 1, y
        hit = owner(gx, gy, placed) if bridge or (gx, gy) != VANILLA_DECK else None
        if hit is not None and not belongs(hit, gx, gy):
            hit = None
        if hit is None:
            parked.append((ty, param, x, y))
            struct.pack_into("<HH", elf, o + 4, park[0] + 1, park[1])
        else:
            ngx, ngy = _shift(hit, gx, gy)
            struct.pack_into("<HH", elf, o + 4, ngx + 1, ngy)
            moved += 1
        if ty == 2:
            op = ops + 4 * sprite
            if bytes(elf[op:op + 4]) != bytes((0x30, 0x04, 0x04, sprite)):
                raise ValueError("randworld: map 0 sprite op %d is %s" % (sprite, elf[op:op + 4].hex()))
            if hit is None:
                elf[op + 2] = 0x02                                  # clear DRAW
                hidden.append((gx, gy))
            sprite += 1
    log(f"[randworld] entrance records: {moved} moved, {len(parked)} parked at {park}"
        + (f", {len(hidden)} unplaced building sprite(s) hidden {hidden}" if hidden else ""))


def move_minimap_points(elf, placed, log):
    o = EP.ram2file(MINIMAP_POINTS)
    moved = 0
    while True:
        x, y = elf[o], elf[o + 1]
        if x == 0 and y == 0:
            break
        hit = owner(x - 1, y, placed)
        if hit is not None:
            ngx, ngy = _shift(hit, x - 1, y)
            elf[o], elf[o + 1] = ngx + 1, ngy
            moved += 1
        o += 2
    log(f"[randworld] menu-map cursor points: {moved} moved")


def move_script_objects(elf, placed, log):
    """Event-script op 0x2F `2f 08 ?? ?? ff X Y ??` places a runtime object at
    field (X, Y); the overworld startup script uses it for the Mirage Tower's
    blocker. Overworld-sized coordinates inside a stamp's window move with it
    (dungeon scripts use small coordinates and stay)."""
    lo, hi = EP.ram2file(SCRIPT_LO), EP.ram2file(SCRIPT_HI)
    data = bytes(elf)
    i = data.find(b"/", lo)
    moved = 0
    while i != -1 and i < hi:
        if elf[i + 4] == 0xFF:
            fx, fy = elf[i + 5], elf[i + 6]
            if fx >= 64 or fy >= 64:
                p = owner(fx - 1, fy, placed, grows=(0, 2))
                if p is not None:
                    ngx, ngy = _shift(p, fx - 1, fy)
                    elf[i + 5], elf[i + 6] = ngx + 1, ngy
                    moved += 1
        i = data.find(b"/", i + 1)
    log(f"[randworld] event-script objects: {moved} moved")


# Scene teleports `03 0c 01 MM xx 00 yy 00 zz 00 ff ff`: event op 0x03
# (handler 0x08842F18; the interpreter's op table is the 12-byte member-pointer
# array at 0x0898AF38, fn at +8) requests a map change to map MM with the party
# at field (x, y). The map-change loop copies that into the save's position
# (0x08867708) and the overworld loader puts the party, so the camera, there
# (0x0883D88C). Those that target the overworld (MM = 0) move with a stamp.
#
# Nerrick's canal scene (RE 2026-10-17; playtest: "the camera pans from random
# Duergar south a fixed distance. That spot isn't where random canal is"):
#   0x089AA62C  03 -> field (95,149)        camera start
#   0x089AA638  16 04 00 ff                 hide the party
#   0x089AA644  15 08 08 03 00 00 00 00     walk 8 tiles, dir 0 = south (op
#                                           0x15, 0x0884481C): THE PAN
#   ...         15 08 01 02 00|01 00 00 00  1-tile south/north pairs: net zero
#   0x089AA6B0  2d 04 0b 00                 story flag 11 set (canal open)
#   0x089AA6BC  03 -> field (95,157)        reload ON the crossing, canal open
# The pan is a RELATIVE walk: it ends 8 rows below wherever the scene starts.
# So both teleports move by the CANAL's shift, which keeps vanilla's start-to-
# crossing geometry and the scene's length. (95,149) lies inside Duergar's
# window, two tiles from its door, so owner()/belongs() would hand it to
# Duergar: the camera would start at the new Duergar and pan onto nothing.
# Hence the tie by address. (The playtested ISO moved neither teleport: its
# start was vanilla (95,149), beside the cave that seed placed there.)
#
# The King's bridge scene is the same shape: 0x089A8C0C teleports to field
# (145,153) and walks 7 tiles north onto the vanilla deck before flag 3. On a
# random world it follows the King's bridge stamp (a world without a bridge
# keeps vanilla coordinates: no deck exists to show).
# 0x089A8928 = iso_patcher._SLC_TELEPORT (skip_lute_cutscene: Garland's event
# ends by walking out of Chaos Shrine onto its own doorstep; 0x089A8920 before
# v361 put the return-stack clear ahead of it).
SCENE_FOLLOWS = {0x089AA62C: "canal", 0x089AA6BC: "canal", 0x089A8C0C: "bridge",
                 0x089A8928: "chaos"}


def move_scene_teleports(elf, placed, log):
    lo, hi = EP.ram2file(SCRIPT_LO), EP.ram2file(SCRIPT_HI)
    base = EP.ram2file(SCRIPT_LO)
    by_key = {p["key"]: p for p in placed}
    moved = []
    for o in range(lo, hi - 12, 2):
        if elf[o] != 0x03 or elf[o + 1] != 0x0C or elf[o + 2] != 0x01 or elf[o + 3] != 0x00                 or elf[o + 10] != 0xFF or elf[o + 11] != 0xFF:
            continue
        fx, fy = struct.unpack_from("<HH", elf, o + 4)
        if not (1 <= fx <= N and 0 <= fy < N):
            continue
        ram = SCRIPT_LO + (o - base)
        key = SCENE_FOLLOWS.get(ram)
        p = by_key.get(key) if key else owner(fx - 1, fy, placed)
        if p is None or (key is None and not belongs(p, fx - 1, fy)):
            continue
        ngx, ngy = _shift(p, fx - 1, fy)
        if (ngx, ngy) != (fx - 1, fy):
            struct.pack_into("<HH", elf, o + 4, ngx + 1, ngy)
            moved.append((hex(ram), p["key"], (fx - 1, fy), (ngx, ngy)))
    log(f"[randworld] overworld scene teleports: {len(moved)} moved {moved}")


def move_exit_returns(elf, placed, log):
    """Exit-table entries landing on the overworld (dest map 0) carry the
    arrival tile (field_x*16+8, field_y*16+8) at entry +8 (20-byte entries);
    each moves with the stamp whose window holds it. With no bridge, returns
    onto the vanilla deck go to Cornelia's door."""
    bases = sorted({struct.unpack_from("<I", elf, EP.ram2file(EXIT_TABLE_PTRS + 8 * m))[0]
                    for m in range(260)})
    moved = 0
    bridge = _has_bridge(placed)
    door = _cornelia_door(placed)
    for k, b in enumerate(bases):
        nxt = bases[k + 1] if k + 1 < len(bases) else EXIT_TABLE_PTRS
        for i in range((nxt - b) // 20):
            o = EP.ram2file(b + 20 * i)
            dm, _u, x, y = struct.unpack_from("<IIHH", elf, o)
            if dm != 0 or not (x or y):
                continue
            gx, gy = x // 16 - 1, y // 16
            if not bridge and (gx, gy) == VANILLA_DECK:
                struct.pack_into("<HH", elf, o + 8, (door[0] + 1) * 16 + 8, door[1] * 16 + 8)
                moved += 1
                continue
            hit = owner(gx, gy, placed)
            if hit is None:
                continue
            ngx, ngy = _shift(hit, gx, gy)
            if (ngx, ngy) != (gx, gy):
                struct.pack_into("<HH", elf, o + 8, (ngx + 1) * 16 + 8, ngy * 16 + 8)
                moved += 1
    log(f"[randworld] exit returns: {moved} moved")


def rewrite_terrain(elf, grid, placed, log):
    """The terrain CLASS map (one byte per cell, stored one column EAST of
    its tile): each cell takes its tile's vanilla mode (randworld tables),
    and stamp footprints keep their vanilla bytes."""
    from ..randworld import tables as T
    modes = T.TERRAIN["modes"]
    base = EP.ram2file(TERRAIN_MAP) + 4
    old = bytes(elf[base:base + N * N])
    new = bytearray(N * N)
    for y in range(N):
        row = y * N
        for x in range(N):
            new[row + (x + 1) % N] = modes[grid[row + x]]
    for p in placed:
        if p["kind"] == "bridge":
            continue
        sx0, sy0, w, h = p["src"]
        dx, dy = p["dst"]
        cells = p.get("cells") or [(i, j) for j in range(h) for i in range(w)]
        for i, j in cells:
            new[((dy + j) % N) * N + (dx + i + 1) % N] = old[((sy0 + j) % N) * N + (sx0 + i + 1) % N]
    elf[base:base + N * N] = new
    changed = sum(1 for a, b in zip(old, new) if a != b)
    log(f"[randworld] terrain class map: {changed * 100 // (N * N)}% of cells changed")


def bridge_always_built(elf, log):
    """Random worlds: the King's bridge is always built (user 2026-10-13).
    Remap table B (story flag 3 clear) turns the bridge ids into water; make
    it leave them alone. Must run AFTER the world_gates cave, which checks
    table B is vanilla."""
    base = EP.ram2file(REMAP_TABLE_B)
    for t in BRIDGE_TILE_IDS:
        struct.pack_into("<H", elf, base + 2 * t, t)
    log("[randworld] bridge remap: always built")


def apply_elf(elf, world, log=print):
    from ..randworld import tables as T
    grid = world_grid(world)
    placed = world["placed"]
    park = _park_cell(grid, T.ATT)
    move_records(elf, placed, park, log)
    move_minimap_points(elf, placed, log)
    move_exit_returns(elf, placed, log)
    move_script_objects(elf, placed, log)
    move_scene_teleports(elf, placed, log)
    rewrite_terrain(elf, world_grid(world, resolve="open"), placed, log)
    bridge_always_built(elf, log)


# ---------------------------------------------------------------- disc ---
def disc_cells(grid):
    """(cell 0, file cells) for MAP_00_AMD.BIN. The engine's grid starts at
    the AMD's byte 8 (the dungeon AMDs' header size; ff1_data.OW_GRID_PTR_SKEW),
    so engine cell 0 is the u16 our 10-byte header reading calls padding, and
    engine cell k+1 is file cell k. The engine wraps x and y modulo 255, so
    engine column 0 is the generator's column 254 -- the vanilla class map
    agrees, and the vanilla grid reads as the generator's 255-torus only that
    way. Writing the grid flat put generator column 254 one ROW low beside
    column 0 (harmless while every map kept a sea band there). Column 254 is
    filed one row up; the generator's (254, 0) goes to engine cell 0. The
    file's last cell lies past the engine's 255 x 255 and is never read."""
    cells = list(grid)
    for y in range(N - 1):
        cells[y * N + N - 1] = grid[(y + 1) * N + N - 1]
    cells[N * N - 1] = grid[N - 1]
    return grid[N - 1], cells


def bake_grid(iso_path, world, log=print):
    """Write the generated grid into MAP_00_AMD.BIN (dpk, same record slot).
    LOAD-BEARING: the ELF half above points every entrance at this grid."""
    from . import map_bake as MB
    from . import wp16
    from .extern_bake import _find_dpk, _dpk_records
    from . import openworld_data as OW
    grid = world_grid(world)
    with open(iso_path, "r+b") as f:
        dpk_off, dpk_size = _find_dpk(f)
        f.seek(dpk_off)
        dpk = bytearray(f.read(dpk_size))
        rec_off, data_off, data_size = _dpk_records(dpk)[MB._PCK00]
        blob = bytearray(wp16.decompress(bytes(dpk[data_off:data_off + data_size])))
        doff, dsz = MB._find_subfile(blob, MB._AMD00)
        _flags, w, h = struct.unpack_from("<IHH", blob, doff)
        g0 = doff + MB._AMD00_HDR
        if (w, h) != (N, N) or dsz != MB._AMD00_HDR + w * h * 2:
            raise ValueError(f"randworld: unexpected overworld grid {w}x{h}")
        if bytes(blob[g0 + OW.ANCHOR_OFF:][:len(OW.ANCHOR)]) != OW.ANCHOR:
            raise ValueError("randworld: the source overworld is not vanilla (anchor missing)")
        cell0, cells = disc_cells(grid)
        struct.pack_into("<H", blob, g0 - 2, cell0)
        blob[g0:g0 + N * N * 2] = struct.pack("<%dH" % (N * N), *cells)
        comp = wp16.compress(bytes(blob))
        if len(comp) > data_size:
            raise ValueError(f"randworld: MAP_00 {len(comp)} B > slot {data_size} B")
        if wp16.decompress(comp) != bytes(blob):
            raise ValueError("randworld: wp16 round-trip mismatch")
        dpk[data_off:data_off + data_size] = comp + b"\x00" * (data_size - len(comp))
        struct.pack_into("<I", dpk, rec_off + 32, len(blob))
        f.seek(dpk_off)
        f.write(dpk)
    log(f"[randworld] overworld grid baked (compressed {len(comp)}/{data_size})")


def world_digest(world):
    """A stable digest of everything the bake writes (folded into bake_hash32)."""
    import hashlib
    import json
    h = hashlib.sha1()
    h.update(world["grid"].encode("ascii"))
    h.update(json.dumps(world["placed"], sort_keys=True).encode())
    h.update(json.dumps(world.get("gates") or [], sort_keys=True).encode())
    if world.get("fog"):                 # fog_of_war: the mapfog hooks are baked
        h.update(b"fog")
    return h.digest()
