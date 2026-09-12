"""Overworld map logic for the client's Map tab (pure Python, no Kivy).

Answers two questions off the baked ow_minimap_data and bestiary_data:

  cells_for_source((kind, (tier, node)))  -> which map cells a Bestiary
      heading such as "Desert (Pravoka)" means: every 32x32 encounter zone
      whose ZONE_ACCESS is that (tier, node), clipped to the heading's own
      terrain (desert cells for a Desert heading, forest for Forests, plain
      land for Overworld, the terrain-4 grass for Grass).
  place_at(x, y)  -> the heading the party is standing in right now, from
      the cell's zone and terrain, so the tab can say "You are in: Forests
      (Pravoka)" and light the same cells.

Zone index = ((x + 7) >> 5) + 8 * ((y + 7) >> 5), the engine's own +7 bias
(iso_patcher._DF_ZONE_TIER), with both grid indices CLAMPED to 7. The bias makes
a ninth row and column (coords 249..254 give grid index 8) that the 64-entry
access table cannot name, and clamping hands that border to the zone beside it
-- water since PATCHER_VERSION 326, land since 327, matching the caves and both
generators. raw_zone_of keeps the unclamped formula for documentation.

Coordinates are AMD GRID cells. The field coordinates the client reads
(FIELD_PARTY_X_OFF) are grid x + 1 (ff1_data.GULG_TILE) -- callers convert
with field_to_grid().
"""
from . import bestiary_data as BD
from . import ow_minimap_data as D

W, H = D.W, D.H
TIER_NAMES = ("Cornelia", "Pravoka", "Elfheim", "Western Keep", "Melmond",
              "Crescent Lake", "Onrac", "Citadel of Trials", "Lufenia")

# Bestiary source kinds the map can draw, and the word their labels start with.
KIND_WORD = {
    "ow": "Overworld", "ow_harder": "Overworld",
    "forest": "Forests", "forest_harder": "Forests",
    "desert": "Desert", "desert_harder": "Desert",
    "t4": "Grass", "t4_harder": "Grass",
}
LAND_KINDS = frozenset(KIND_WORD)
# Water hunts (ship / canoe). Lit only from a MONSTER row click, pulsing in
# opposite phase to land (user call 2026-09-05).
WATER_KINDS = frozenset(("river", "rr", "ocean", "oc"))

# MIRRORS of iso_patcher (test_ow_map pins parity): the river option's
# northern-river zones and the regional-ocean quadrants. Unlisted sea zones
# are the Starting Ocean; with the option off the whole sea is one pool.
RR_NORTH_ZONES = frozenset(range(0, 24)) | {31}
OC_POOL_ZONES = {
    1: (0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19, 24, 25, 26, 27),
    2: (4, 5, 6, 7, 12, 13, 14, 15, 20, 21, 22, 23, 28, 29, 30, 31),
    3: (32, 33, 34, 40, 41, 42, 48, 49, 50, 56, 57, 58, 59),
    4: (39, 47, 55, 60, 61, 62, 63),   # 38 = Pravoka bay / starting lake -> vanilla
}

_terrain = None
_zone_cells = None


def terrain():
    global _terrain
    if _terrain is None:
        _terrain = D.terrain()
    return _terrain


def raw_zone_of(x, y):
    """The engine's unclamped formula, kept so the wrap stays documented and
    testable. Nothing indexes a table with this -- use zone_of."""
    return ((x + D.ZONE_BIAS) >> 5) + 8 * ((y + D.ZONE_BIAS) >> 5)


def zone_of(x, y):
    """Zone index with BOTH grid indices clamped to 7, so it is always 0..63.

    The +7 bias gives grid index 8 for coords 249..254 -- a ninth row and
    column the 64-entry tables cannot name -- and the two edges fail
    DIFFERENTLY. The row OVERFLOWS (zone 64..72), which a `< 64` bounds check
    catches. The column WRAPS: zone = 8 + 8*zy is column 0 of the NEXT row, so
    raw_zone_of(250, 100) and raw_zone_of(0, 132) are both 32 -- a perfectly
    legal index that no bounds check can flag. That wrap sent 1461 east-edge
    ocean cells to the far-west pool (v326: it drew a vertical band up the
    map's right side, hanging 32 tiles north of the real SW boundary) and
    folded 21 plain-land + 12 shallow cells into a far-west access class
    (v327). Clamping gives the border to the zone beside it.

    Mirrors the caves: apply_regional_ocean_encounters, apply_terrain_pools and
    apply_northern_river_encounters all clamp the same way, and both generators
    (gen_bestiary_areas, gen_ow_minimap) build their tables with it."""
    return (min((x + D.ZONE_BIAS) >> 5, 7)
            + 8 * min((y + D.ZONE_BIAS) >> 5, 7))


def field_to_grid(fx, fy):
    """Field-struct coordinates -> grid cell, or None when off the map."""
    gx, gy = fx - 1, fy
    if 0 <= gx < W and 0 <= gy < H:
        return gx, gy
    return None


def zone_cells():
    """zone index -> list of row-major cell indices, built once. Every index is
    0..63: zone_of clamps the ninth row/column onto the border zones."""
    global _zone_cells
    if _zone_cells is None:
        zc = {}
        for y in range(H):
            row = y * W
            for x in range(W):
                zc.setdefault(zone_of(x, y), []).append(row + x)
        _zone_cells = zc
    return _zone_cells


def kind_terrains(kind, forests_on=True):
    """Terrain classes a heading kind covers. With dangerous_forests OFF the
    forest tiles roll the plain land table, so an Overworld heading covers
    them too; with it ON they belong to the Forests heading alone."""
    base = kind.replace("_harder", "")
    if base == "ow":
        # marsh routes to the ordinary land table (iso_patcher: "marsh plays
        # mechanically like grass"), so it is Overworld, not a canoe pool
        return {D.T_PLAIN, D.T_MARSH} | (set() if forests_on else {D.T_FOREST})
    if base == "forest":
        return {D.T_FOREST}
    if base == "desert":
        return {D.T_DESERT}
    if base == "t4":
        return {D.T_GRASS2}
    return set()


# bestiary source kind -> the terrain name gen_bestiary_areas keys its
# per-zone access classes by
KIND_TERRAIN = {"ow": "land", "forest": "forest", "desert": "desert", "t4": "t4"}


def zones_for_access(acc, kind="ow"):
    """Zones whose (tier, access class) for this heading kind is `acc`.

    A heading's key is computed per (terrain, zone) by gen_bestiary_areas --
    a zone's desert can sit on a different landmass than its grass, so
    "Desert (Mirage Tower)" (8, 4) is a key no plain-land zone carries. The
    per-terrain table answers that; ZONE_ACCESS (plain land only) is the
    fallback for a bestiary_data built before it existed."""
    acc = tuple(int(v) for v in acc)
    # One rule for both directions (access_of is what place_at asks), so a
    # cell always lights under the heading the map names it with.
    return [z for z in range(64) if access_of(z, kind) == acc]


def water_cells(src, opts=None, zones=None):
    """Row-major cell indices of a WATER hunt: ('river', 0) every river cell
    (minus the northern zones when the river option splits them off),
    ('rr', 0) the northern rivers, ('ocean', 0) the whole sea (or the
    Starting Ocean when regional_ocean is on), ('oc', n) one quadrant.

    `zones` narrows the result to those encounter zones -- see
    cells_for_source."""
    opts = opts or {}
    kind, key = src[0], src[1]
    terr = terrain()
    zc = zone_cells()
    out = []
    if kind in ("river", "rr"):
        split = bool(opts.get("river"))
        for z, idxs in zc.items():
            if zones is not None and z not in zones:
                continue
            north = z in RR_NORTH_ZONES
            if kind == "rr" and not north:
                continue
            if kind == "river" and split and north:
                continue
            out.extend(i for i in idxs if terr[i] == D.T_RIVER)
    elif kind in ("ocean", "oc"):
        regional = bool(opts.get("ocean"))
        quads = {z for zs in OC_POOL_ZONES.values() for z in zs}
        # zone_cells() is clamped, so the map edge belongs to the zone beside
        # it exactly as the cave reads it -- no past-the-table bucket any more.
        for z, idxs in zc.items():
            if zones is not None and z not in zones:
                continue
            if kind == "oc":
                if z not in OC_POOL_ZONES.get(int(key), ()):
                    continue
            elif regional and z in quads:
                continue        # Starting Ocean = every sea zone outside the quadrants
            out.extend(i for i in idxs if terr[i] == D.T_OCEAN)
    return out


def cells_for_source(src, forests_on=True, opts=None, zones=None):
    """Row-major cell indices lit by a Bestiary source (kind, (tier, node)).
    Water kinds answer through water_cells; anything else is empty.

    `zones` (an int container, or None for "the whole heading") narrows the
    result to those encounter zones. A heading key is a reachability NODE, and
    13 of the 19 land nodes cover several 32x32 zones whose rolled rows have
    nothing in common -- "Overworld (Crescent Lake)" is zones 54, 55, 62 and
    63 -- so lighting the whole heading for ONE monster sends the party to
    zones that can never spawn it (live report 2026-09-07). A monster-row
    click passes logic.overworld_zone_sources()[mon]; a heading click passes
    None, because there the heading IS the question."""
    kind, key = src
    if kind in WATER_KINDS:
        return water_cells(src, opts, zones)
    if kind not in LAND_KINDS:
        return []
    try:
        acc = (int(key[0]), int(key[1]))
    except (TypeError, ValueError, IndexError):
        return []
    want = kind_terrains(kind, forests_on)
    if not want:
        return []
    terr = terrain()
    zc = zone_cells()
    out = []
    for z in zones_for_access(acc, kind):
        if zones is not None and z not in zones:
            continue
        for i in zc.get(z, ()):
            if terr[i] in want:
                out.append(i)
    return out


def cells_for_sources(srcs, forests_on=True, zones=None):
    """{src: [cells]} for several headings at once (a monster's places)."""
    return {tuple(s): cells_for_source(s, forests_on, zones=zones)
            for s in srcs}


def source_label(src, opts=None):
    """The Bestiary's own label for a heading, with a tier-name fallback for
    a zone the seed's tables never rolled (so the map can still name where
    the party stands). Water labels follow tracker._bestiary_source_label:
    one sea reads plain "Ocean" unless regional_ocean splits it."""
    kind, key = src
    if kind in WATER_KINDS:
        if kind == "ocean" and not (opts or {}).get("ocean"):
            return "Ocean"
        return BD.SOURCE_LABELS.get((kind, key), f"{kind}:{key}")
    label = BD.SOURCE_LABELS.get((kind, tuple(key)))
    if label:
        return label
    tier = int(key[0]) if key else 0
    name = TIER_NAMES[tier] if 0 <= tier < len(TIER_NAMES) else "?"
    return f"{KIND_WORD.get(kind, kind)} ({name})"


def place_at(gx, gy, forests_on=True, opts=None):
    """(label, src) for the cell the party stands on. `src` is a heading
    key the map can light -- a land (kind, (tier, node)) or a water
    (kind, key) -- or None for towns, mountains and other cells with no
    encounter. `opts` (the seed's bestiary opts) decides the water split
    and, when given, dangerous_forests."""
    if not (0 <= gx < W and 0 <= gy < H):
        return "", None
    if opts is not None:
        forests_on = bool(opts.get("forests", forests_on))
    t = terrain()[gy * W + gx]
    if t == D.T_OCEAN:
        z = zone_of(gx, gy)
        src = ("ocean", 0)
        if (opts or {}).get("ocean"):
            for n, zones in OC_POOL_ZONES.items():
                if z in zones:
                    src = ("oc", n)
                    break
        label = source_label(src, opts)
        if src[0] == "ocean":
            label += " (ship)"
        return label, src
    if t == D.T_SHALLOWS:
        return "Shallows (ship)", None
    if t == D.T_RIVER:
        src = ("rr", 0) if ((opts or {}).get("river")
                            and zone_of(gx, gy) in RR_NORTH_ZONES) else ("river", 0)
        return ("Northern rivers (canoe)" if src[0] == "rr" else "River (canoe)"), src
    if t == D.T_MOUNTAIN:
        return "Mountains", None
    if t == D.T_TOWN:
        return "Town / entrance", None
    if t == D.T_BRIDGE:
        return "Bridge", None
    kind = {D.T_PLAIN: "ow", D.T_MARSH: "ow",
            D.T_FOREST: "forest" if forests_on else "ow",
            D.T_DESERT: "desert", D.T_GRASS2: "t4"}.get(t)
    if kind is None:
        return "", None
    acc = access_of(zone_of(gx, gy), kind)
    if acc is None:
        return "No encounters here", None
    src = (kind, acc)
    return source_label(src), src


# Terrain classes whose encounter roll still comes from zones_overworld. The
# other land classes are detoured by a patcher cave before the table is read:
# dangerous_forests owns forest tiles (its own per-tier pool), terrain_pools
# owns desert and the class-9 grass, and water never reaches the land path.
# So a per-zone monster list -- and any narrowing of a highlight to the zones
# a monster really rolls in -- is only the truth on these.
ZONE_ROW_KINDS = frozenset(("ow", "ow_harder"))


def here_rows(zone_rows, gx, gy, forests_on=True, opts=None):
    """The monsters that ACTUALLY roll on the cell the party stands on:
    [{"mon", "name", "pct"}, ...] from logic.overworld_zone_rows(), commonest
    first, or [] when the cell's roll does not come from zones_overworld.

    This is the honest answer to "what can jump me here". place_at() names the
    heading, but a heading is a reachability NODE covering up to eight zones
    (Pravoka) with unrelated rows, so its roster is a union -- reading it as a
    local forecast is what made east-of-Crescent-Lake promise Rakshasa while
    the zone actually rolled Fire Hydra (2026-09-07).

    Forest / desert / grass and every water cell return []: a patcher cave
    detours those away from zones_overworld before it is indexed, so the zone
    row would be a confident lie about them."""
    if not zone_rows or not (0 <= gx < W and 0 <= gy < H):
        return []
    if opts is not None:
        forests_on = bool(opts.get("forests", forests_on))
    t = terrain()[gy * W + gx]
    kind = {D.T_PLAIN: "ow", D.T_MARSH: "ow",
            D.T_FOREST: "forest" if forests_on else "ow"}.get(t)
    if kind not in ZONE_ROW_KINDS:
        return []
    z = zone_of(gx, gy)
    return list(zone_rows.get(z) or zone_rows.get(str(z)) or ())


def access_of(zone, kind="ow"):
    """(tier, access class) of a zone for a heading kind, per the same
    per-terrain table zones_for_access reads (ZONE_ACCESS as fallback)."""
    terrain = KIND_TERRAIN.get(kind.replace("_harder", ""), "land")
    table = getattr(BD, "TERRAIN_ZONE_ACCESS", None)
    acc = None
    if table:
        acc = table.get((terrain, zone))
    if acc is None:
        acc = BD.ZONE_ACCESS.get(zone)
    if acc is None:
        return None
    return (int(acc[0]), int(acc[1]))


def label_matches(label, src):
    """True when a Bestiary group label names this land heading. Groups can
    merge several headings ("Desert / Grass (Cornelia)"), so match on the
    heading's own label being a whole part of the group's."""
    want = source_label(src)
    if label == want:
        return True
    head, _, tail = want.partition(" (")
    return head in label.split(" (")[0].split(" / ") and label.endswith("(" + tail)


def landmarks():
    return list(D.LANDMARKS)


# ---------------------------------------------------------------- squares --
# PopTracker-style squares (user call 2026-09-05): one per landmark pin, state
# from every AP check behind that pin -- Tracker areas (chests, NPCs, shops)
# plus the Bestiary monsters hunted inside it -- and one per Bestiary
# sub-tab for its OVERWORLD hunts only (encounters outside any location).
#   green  every remaining check here is in logic
#   red    none of the remaining checks is in logic
#   half   some in logic, some not
#   grey   all collected
#   None   nothing to check behind this pin in this seed -> no square
GREEN, RED, HALF, GREY = "green", "red", "half", "grey"

# Tracker area key -> pin, by prefix (longest match wins). Co-located areas
# fold into one pin: Sunken Shrine into Onrac, Flying Fortress into Mirage
# Tower, the Chaos Shrine basement into the temple.
PIN_AREA_PREFIX = (
    ("castle_cornelia", "cornelia"), ("mk_castle_cornelia", "cornelia"),
    ("shops_cornelia", "cornelia"),
    ("chaos_shrine", "chaos"), ("mk_chaos_shrine", "chaos"),
    ("shops_pravoka", "pravoka"),
    ("matoyas_cave", "matoya"),
    ("mount_duergar", "duergar"), ("mk_mount_duergar", "duergar"),
    ("elven_castle", "elfheim"), ("mk_elven_castle", "elfheim"),
    ("shops_elfheim", "elfheim"),
    ("western_keep", "wkeep"), ("mk_western_keep", "wkeep"),
    ("marsh_", "marsh"), ("mk_marsh", "marsh"),
    ("melmond", "melmond"), ("shops_melmond", "melmond"),
    ("cavern_of_earth", "earth"),
    ("giants_cavern", "titan"),
    ("sardas_cave", "sarda"),
    ("crescent_lake", "crescent"), ("shops_crescent", "crescent"),
    ("mount_gulg", "gulg"), ("gulg_", "gulg"),
    ("cavern_of_ice", "ice"),
    ("onrac_hub", "onrac"), ("shops_onrac", "onrac"),
    ("sunken_shrine", "onrac"), ("ss_", "onrac"),
    ("onrac_caravan", "caravan"),
    ("waterfall", "waterfall"),
    ("mirage_tower", "mirage"), ("flying_fortress", "mirage"),
    ("lefein", "lufenia"),
    ("gaia", "gaia"), ("shops_gaia", "gaia"),
    ("dragon_caves", "cardia"),
    ("citadel_of_trials", "trials"),
    ("bonus_0", "earthgift"), ("bonus_static_0", "earthgift"),
    ("bonus_1", "hellfire"), ("bonus_static_1", "hellfire"),
    ("bonus_2", "lifespring"), ("bonus_static_2", "lifespring"),
    ("bonus_3", "whisperwind"), ("bonus_static_3", "whisperwind"),
)
# bonus dungeon index (logic.BONUS_DUNGEONS order) -> pin
PIN_BONUS = ("earthgift", "hellfire", "lifespring", "whisperwind")
# NPC tiles are numbered, not named: tracker's npc_N -> pin.
PIN_NPC = {1: "cornelia", 2: "pravoka", 3: "crescent", 4: "ice", 5: "sarda",
           6: "lufenia", 7: "waterfall", 8: "onrac", 9: "gaia", 10: "wkeep",
           11: "matoya", 12: "elfheim", 13: "mirage", 14: "duergar"}
# bestiary dungeon key (bestiary_data.CAVE_MAP_DUNGEON values) -> pin
PIN_DUNGEON = {
    "chaos_f1": "chaos", "chaos_basement": "chaos", "marsh": "marsh",
    "earth": "earth", "gurgu": "gulg", "ice": "ice", "trials": "trials",
    "waterfall": "waterfall", "mirage": "mirage", "fortress": "mirage",
    "onrac": "onrac", "sunken": "onrac",
}
# monster-tile map id -> pin, for the maps without a dungeon row
PIN_TILE_PLACE = {0x58: "wkeep"}
# scripted boss fid -> pin (labels in bestiary_data.SOURCE_LABELS)
PIN_SCRIPTED = {
    127: "chaos", 126: "pravoka", 28: "marsh", 156: "marsh", 124: "earth",
    125: "wkeep", 122: "earth", 121: "gulg", 120: "onrac", 119: "mirage",
    86: "mirage", 76: "mirage", 123: "chaos", 115: "chaos", 116: "chaos",
    117: "chaos", 118: "chaos",
}
# Bestiary source kinds that are OVERWORLD hunts (outside any location)
OVERWORLD_KINDS = LAND_KINDS | frozenset(("ocean", "oc", "river", "rr"))


def pin_of_area(key):
    """Landmark pin for a Tracker area key, or None (unmapped / no pin)."""
    if key.startswith("npc_"):
        try:
            return PIN_NPC.get(int(key[4:]))
        except ValueError:
            return None
    best = None
    for prefix, pin in PIN_AREA_PREFIX:
        if key.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), pin)
    return best[1] if best else None


def pin_name(pin):
    """Landmark label for a pin id ("Marsh Cave"), or the id itself."""
    for _x, _y, _k, name, pid in D.LANDMARKS:
        if pid == pin and name:
            return name
    return pin


def dungeon_pins(places):
    """Ordered, de-duplicated landmark pins for the DUNGEON places of a
    Bestiary row / heading click (user call 2026-09-06: the dungeon square
    glows beside the land highlight). Overworld kinds contribute nothing."""
    out = []
    for p in places or ():
        if not p or p[0] in OVERWORLD_KINDS:
            continue
        pin = pin_of_source(tuple(p))
        if pin is not None and pin not in out:
            out.append(pin)
    return out


def pin_of_source(src):
    """Landmark pin for a Bestiary (kind, key) source, or None."""
    try:
        kind, key = src[0], src[1]
    except (TypeError, IndexError):
        return None
    if kind == "cave":
        return PIN_DUNGEON.get(BD.CAVE_MAP_DUNGEON.get(key))
    if kind == "tile":
        # monster tiles sit inside a dungeon, or in the Western Keep
        return PIN_DUNGEON.get(BD.CAVE_MAP_DUNGEON.get(key),
                               PIN_TILE_PLACE.get(key))
    if kind in ("hd", "ff", "cf"):
        return PIN_DUNGEON.get(key)
    if kind == "dlc":
        try:
            band = int(key)
        except (TypeError, ValueError):
            return None
        return PIN_BONUS[min(band // 10, 3)]
    if kind == "dlc_boss":
        try:
            fid = int(key)
        except (TypeError, ValueError):
            return None
        return ("earthgift" if 256 <= fid <= 259 else "hellfire" if fid <= 264
                else "lifespring" if fid <= 268 else "whisperwind" if fid <= 272
                else None)
    if kind == "scripted":
        try:
            return PIN_SCRIPTED.get(int(key))
        except (TypeError, ValueError):
            return None
    return None


def _state(n_in, n_out, n_total):
    if n_total <= 0:
        return None
    if n_in + n_out == 0:
        return GREY
    if n_out == 0:
        return GREEN
    if n_in == 0:
        return RED
    return HALF


def map_squares(areas, view):
    """Square states for the Map tab.

    areas -- {area key: AreaState-like} from the Tracker (needs .state,
             .total, .open_now, .gated; "empty" areas are skipped), or None
    view  -- tracker.bestiary_view payload (groups carry "places",
             "monsters", "open", "done"), or None

    Returns {"pins": {pin: state}, "hunts": {section: state}} with states
    GREEN / RED / HALF / GREY; a pin or section with nothing to check is
    absent. A monster that roams several floors of one dungeon is counted
    once for that pin, in logic if ANY of those groups is open."""
    pin_in, pin_out, pin_total = {}, {}, {}
    for key, a in (areas or {}).items():
        if getattr(a, "state", "empty") == "empty" or not getattr(a, "total", 0):
            continue
        pin = pin_of_area(key)
        if pin is None:
            continue
        pin_total[pin] = pin_total.get(pin, 0) + int(a.total)
        pin_in[pin] = pin_in.get(pin, 0) + int(getattr(a, "open_now", 0))
        pin_out[pin] = pin_out.get(pin, 0) + int(getattr(a, "gated", 0))

    # bestiary: per pin and per section, unique unchecked monsters
    pin_mons, sec_mons = {}, {}         # target -> {mon: open?}
    pin_done, sec_done = {}, {}         # target -> collected count seen
    for sec in (view or {}).get("sections") or []:
        skey = sec.get("key")
        for grp in sec.get("groups") or []:
            places = [tuple(p) for p in (grp.get("places") or ()) if p]
            pins = {pin_of_source(p) for p in places} - {None}
            hunt = any(p[0] in OVERWORLD_KINDS for p in places)
            is_open = grp.get("open", True) is not False
            targets = [("pin", p) for p in pins]
            if hunt and skey:
                targets.append(("sec", skey))
            for kind, t in targets:
                mons = pin_mons if kind == "pin" else sec_mons
                done = pin_done if kind == "pin" else sec_done
                d = mons.setdefault(t, {})
                for e in grp.get("monsters") or ():
                    m = e.get("mon")
                    d[m] = d.get(m, False) or is_open
                if grp.get("done"):
                    done[t] = done.get(t, 0) + int(grp["done"])
    pins = {}
    for pin in set(pin_total) | set(pin_mons) | set(pin_done):
        d = pin_mons.get(pin, {})
        n_in = pin_in.get(pin, 0) + sum(1 for v in d.values() if v)
        n_out = pin_out.get(pin, 0) + sum(1 for v in d.values() if not v)
        total = pin_total.get(pin, 0) + len(d) + pin_done.get(pin, 0)
        st = _state(n_in, n_out, total)
        if st:
            pins[pin] = st
    hunts = {}
    for skey in set(sec_mons) | set(sec_done):
        d = sec_mons.get(skey, {})
        st = _state(sum(1 for v in d.values() if v),
                    sum(1 for v in d.values() if not v),
                    len(d) + sec_done.get(skey, 0))
        if st:
            hunts[skey] = st
    return {"pins": pins, "hunts": hunts}


def section_land_places(view, skey):
    """Every overworld LAND (kind, key) under a Bestiary section, for the
    hunting-grounds square click."""
    out = []
    for sec in (view or {}).get("sections") or []:
        if sec.get("key") != skey:
            continue
        for grp in sec.get("groups") or []:
            for p in grp.get("places") or ():
                if p and p[0] in LAND_KINDS and tuple(p) not in out:
                    out.append(tuple(p))
    return out
