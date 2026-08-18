"""
Client-side reachability evaluator + area map for the GUI Tracker tab.

Pure module: NO imports beyond our own pure siblings (logic / ids / _locinfo), no
Archipelago BaseClasses, no kivy. That is deliberate -- the same rule data that
generation uses (logic.REGION_RULES / region_rules_for / EVENTS / ...) is replayed
here against a plain set of item NAMES, so the tracker can never drift from the
seed's real logic the way a hand-written mirror would. Testable headless.

Three things the client cannot get from the server and must compute here:

  1. EVENT TOKENS. Canal Opened / Airship / Titan Fed / the four
     Crystals / ... are granted by logic.EVENTS at locked event locations. They are
     never *received* as AP items, so `resolve_tokens` runs the same fixpoint AP
     resolves at generation time: walk EVENTS repeatedly, granting each event's
     token once its region rule + extra reqs are met, until nothing new appears.

  2. PER-LOCATION ACCESS. A chest is reachable iff its region rule passes AND its
     per-chest token (MysticKey / TitanFed) is held -- so a region can be MIXED:
     reachable, but with some chests inside still locked. `evaluate` therefore
     resolves every location individually and reports per-area splits.

  3. THE POOL. Which locations actually exist in this seed depends on options
     (bonus_dyn_caps, shop_ap_offers, exclude_bonus_dungeons, the phantom
     Levistone chest, ...). Rather than mirror all of that, the caller passes
     `pool_loc_ids` = ctx.missing_locations | ctx.checked_locations -- every id the
     server actually placed for this slot. Anything not in it simply doesn't count.
"""

from . import data as DATA
from . import ids as ID
from . import logic as LOGIC
from ._locinfo import LOC_INFO

# ---------------------------------------------------------------- sections ----
# Ordered sub-tabs of the Tracker tab. Grouped by GEOGRAPHY (the crystal-quest
# clusters a player actually thinks in) rather than by vehicle tier. Sections are
# STATIC across seeds so the tab strip never reshuffles -- only the tiles' colors
# move. rgb triples only -- no emoji anywhere in this file (PSP glyph constraint
# aside, the AP client's font has no color-emoji coverage).
#
# Mystic-Key-locked chests are pulled out of their geographic tile into a SEPARATE
# per-region tile that sits right after it, in the SAME section (e.g. "Castle
# Cornelia (Mystic Key)" under Cornelia/Start). A region with a mix (Marsh Cave:
# 10 open + 3 locked) therefore shows two tiles in one section.
SUMMARY = "summary"

SECTIONS = [
    # (key,        display,             rgb)
    (SUMMARY,      "Summary",           (0.82, 0.38, 0.58)),
    ("cornelia",   "Cornelia/Start",    (0.62, 0.78, 0.94)),
    ("elfheim",    "Elfheim",           (0.13, 0.45, 0.20)),
    ("earth",      "Earth",             (0.66, 0.48, 0.22)),
    ("fire",       "Fire",              (0.88, 0.34, 0.16)),
    ("water",      "Water",             (0.15, 0.38, 0.75)),
    ("wind",       "Wind",              (0.30, 0.72, 0.42)),
    ("lategame",   "Lategame",          (0.55, 0.45, 0.82)),
    ("chaos",      "Chaos Past",        (0.62, 0.20, 0.28)),
    ("bonus",      "Bonus",             (0.62, 0.35, 0.85)),
    ("shops",      "Shops",             (0.86, 0.68, 0.16)),
]

SECTION_KEYS = [k for k, _d, _c in SECTIONS]
SECTION_COLOR = {k: c for k, _d, c in SECTIONS}
SECTION_DISPLAY = {k: d for k, d, _c in SECTIONS}

# Sections whose tab background is bright enough that white text washes out.
BRIGHT_SECTIONS = frozenset({"shops", "cornelia"})

# ------------------------------------------------------------------- areas ----
# Area = one tile. Regions map 1:1 to a tile; shops are grouped per TOWN (a town's
# 2-3 stores share one tile, since they share an access rule and reading "Cornelia
# Shops 1/3" beats three near-identical tiles); each bonus dungeon gets one tile.
#
# UNMAPPED is the pseudo-area for region=None chests in LOC_INFO -- they fall back
# to LOGIC.UNMAPPED_RULE ([AIRSHIP]), exactly as __init__.set_rules does. Only 5
# indices lack a region today (198 = phantom Levistone, 252-255 = bonus-dungeon
# statics) and classify() routes all of them to other tiles, so this tile is
# legitimately EMPTY (test_tracker derives that rather than assuming occupants).
UNMAPPED = "_unmapped"

# region key -> (display name, section key). Ordered; drives tile order in a tab.
# A region's Mystic-Key-locked chests are NOT counted here -- classify() routes
# them to the region's mk_* tile in the Mystic Key section instead (see below), so
# these displays cover only the freely-accessible chests + any NPC/event location.
REGION_AREAS = [
    # --- Cornelia / Pravoka: the starting continent -----------------------
    ("chaos_shrine",              "Chaos Shrine F1",           "cornelia"),
    ("castle_cornelia_1f",        "Castle Cornelia",           "cornelia"),
    ("matoyas_cave",              "Matoya's Cave",             "cornelia"),
    # Mount Duergar = the Dwarf Cave (Nerrick opens the canal, Smith forges
    # Excalibur). Reachable from the starting continent on foot with the extended
    # open-progression trail; its 8 Mystic-Key chests split off to a Mystic Key
    # tile in this same section.
    ("mount_duergar",             "Mount Duergar",             "cornelia"),
    # --- Elfheim: Elfland landmass (Elf castle, Astos, Marsh Cave) ---------
    ("elven_castle",              "Elven Castle",              "elfheim"),
    ("western_keep",              "Western Keep",              "elfheim"),
    # marsh_cave_b2_top / marsh_cave_b2_bottom are logic regions that own ZERO
    # chests (every Marsh index is filed under marsh_cave_b3 in LOC_INFO), so
    # their "Marsh Cave Upper" / "Marsh Cave Lower" tiles could never be
    # anything but EMPTY -- dropped 2026-08-12. The regions stay in
    # logic.REGION_RULES; only the dead tiles are gone. The real floor split is
    # MARSH_SUBAREAS below.
    ("marsh_cave_b3",             "Marsh Cave",                "elfheim"),
    # --- Earth: Melmond, Cavern of Earth, Titan, Sarda -------------------
    ("melmond",                   "Melmond",                   "earth"),
    # B1-B4 are all the UPPER cavern (same access rule); the floor split is a logic
    # detail, not a place the player thinks of separately -- so they share one name.
    ("cavern_of_earth_b1",        "Cavern of Earth Upper",     "earth"),
    ("cavern_of_earth_b2",        "Cavern of Earth Upper",     "earth"),
    ("cavern_of_earth_b3",        "Cavern of Earth Upper",     "earth"),
    ("cavern_of_earth_b4",        "Cavern of Earth Upper",     "earth"),
    ("cavern_of_earth_lower",     "Cavern of Earth Lower",     "earth"),
    ("giants_cavern",             "Giant's Cavern",            "earth"),
    # Sarda's Cave is its OWN region: it sits beyond Titan's Tunnel, so it gates on
    # Giant's Cavern (or an airship landing) rather than on the Cavern of Earth.
    ("sardas_cave",               "Sarda's Cave",              "earth"),
    # --- Fire Crystal: Crescent Lake, Mount Gulg, Ice Cavern --------------
    ("crescent_lake",             "Crescent Lake",             "fire"),
    ("mount_gulg_b2",             "Mount Gulg",                "fire"),
    ("mount_gulg_b4_agama",       "Mount Gulg Agama",          "fire"),
    ("mount_gulg_b5",             "Mount Gulg",                "fire"),
    ("cavern_of_ice_b1_backdoor", "Cavern of Ice Backdoor",    "fire"),
    ("cavern_of_ice_b2_room",     "Cavern of Ice",             "fire"),
    ("cavern_of_ice_b3_treasury", "Cavern of Ice Treasury",    "fire"),
    # --- Water Crystal: Onrac, Caravan, Waterfall, Sunken Shrine ----------
    ("onrac_hub",                 "Onrac",                     "water"),
    ("onrac_caravan",             "Onrac Caravan",             "water"),
    ("waterfall",                 "Waterfall Cavern",          "water"),
    ("sunken_shrine_2f_sharknado", "Sunken Shrine",            "water"),
    ("sunken_shrine_3f_split",     "Sunken Shrine Split",      "water"),
    ("sunken_shrine_3f_vertical",  "Sunken Shrine Vertical",   "water"),
    ("sunken_shrine_4f_tfc",       "Sunken Shrine",            "water"),
    ("sunken_shrine_5f",           "Sunken Shrine",            "water"),
    # --- Wind Crystal: Mirage Tower, Flying Fortress, Lefein, Gaia --------
    ("mirage_tower_1f",           "Mirage Tower",              "wind"),
    ("mirage_tower_2f",           "Mirage Tower",              "wind"),
    ("flying_fortress_1f",        "Flying Fortress",           "wind"),
    ("flying_fortress_2f",        "Flying Fortress",           "wind"),
    ("flying_fortress_3f",        "Flying Fortress",           "wind"),
    # --- Overworld lategame: Dragon Caves, Citadel, Lefein, Gaia, misc ----
    # Lefein/Gaia are airship-tier NPC handovers (Chime / Oxyale), not Wind-crystal
    # dungeon progress -- they read as lategame errands, so they tile there.
    ("lefein",                    "Lefein",                    "lategame"),
    ("gaia",                      "Gaia",                      "lategame"),
    ("dragon_caves_plains",       "Dragon Caves",              "lategame"),
    ("dragon_caves_forest",       "Dragon Caves Forest",       "lategame"),
    ("dragon_caves_marsh",        "Dragon Caves Marsh",        "lategame"),
    ("citadel_of_trials_2f",      "Citadel of Trials",         "lategame"),
    (UNMAPPED,                    "Unmapped Chests",           "lategame"),
    # --- Chaos Shrine basement (endgame revisit) --------------------------
    # Plaza (3F) opens on crystals_needed crystals alone; the basement additionally
    # needs the Lute -- so the two tiles gate differently (see REGION_RULES).
    ("chaos_shrine_3f_plaza",     "Chaos Shrine Plaza",        "chaos"),
    ("chaos_shrine_b2",           "Chaos Shrine Basement",     "chaos"),
    ("chaos_shrine_b4",           "Chaos Shrine Basement",     "chaos"),
]

_REGION_DISPLAY = {k: d for k, d, _s in REGION_AREAS}

# town key -> (display, section, [shop indices into LOGIC.SHOP_LOCATIONS]).
# All shops live in their own "shops" section rather than scattering across the
# progression tiers: a town's stores are one errand, and mixing them into the
# dungeon tiers buried them. One tile per TOWN (its 2-3 stores share an access
# rule, so three near-identical tiles would say nothing extra).
SHOP_AREAS = [
    ("shops_cornelia",  "Cornelia Shops",      "shops", [0, 1, 2]),
    ("shops_pravoka",   "Pravoka Shops",       "shops", [3, 4, 5]),
    ("shops_elfheim",   "Elfheim Shops",       "shops", [6, 7, 8]),
    ("shops_melmond",   "Melmond Shops",       "shops", [9, 10]),
    ("shops_crescent",  "Crescent Lake Shops", "shops", [11, 12, 13]),
    ("shops_onrac",     "Onrac Shop",          "shops", [14]),
    ("shops_gaia",      "Gaia Shops",          "shops", [15, 16, 17]),
]

SHOP_AREA_OF_SHOP = {}
for _k, _d, _s, _idxs in SHOP_AREAS:
    for _i in _idxs:
        SHOP_AREA_OF_SHOP[_i] = _k

# ------------------------------------------------------------ shops tab -------
# Layout for the top-level Shops tab (a sibling of Tracker, NOT the Tracker's Shops
# sub-tab). One entry per town; `city` is the D.TOWN_MAP_FLAGS / D.SHOP_CITY index
# used to detect a first visit. Ordered by city id. Derived structure -- the shop
# indices come straight from SHOP_AREAS so the two can't disagree.
SHOP_TOWNS = [
    # (city id, town name, [shop indices into LOGIC.SHOP_LOCATIONS])
    (0, "Cornelia",      SHOP_AREAS[0][3]),
    (1, "Pravoka",       SHOP_AREAS[1][3]),
    (2, "Elfheim",       SHOP_AREAS[2][3]),
    (3, "Melmond",       SHOP_AREAS[3][3]),
    (4, "Crescent Lake", SHOP_AREAS[4][3]),
    (5, "Onrac",         SHOP_AREAS[5][3]),
    (6, "Gaia",          SHOP_AREAS[6][3]),
    # Lufenia has no AP item shops (no SHOP_LOCATIONS entry) -- only native magic
    # shops (store-city 7, the L8 Full-Life / Flare store), so its list is empty and
    # its sub-tab shows magic only.
    (7, "Lufenia",       []),
]

# Shops sub-tab color per town: each town borrows the tracker section color of the
# part of the game it belongs to, so the two tabs read as one palette. Cornelia
# takes the Summary pink; Pravoka the Cornelia/Start light blue; the elemental
# towns their fiend's section color; Lufenia the Lategame purple.
SHOP_TOWN_COLOR = {
    0: SECTION_COLOR[SUMMARY],       # Cornelia      -- pink
    1: SECTION_COLOR["cornelia"],    # Pravoka       -- light blue
    2: SECTION_COLOR["elfheim"],     # Elfheim       -- forest green
    3: SECTION_COLOR["earth"],       # Melmond       -- brown
    4: SECTION_COLOR["fire"],        # Crescent Lake -- orange
    5: SECTION_COLOR["water"],       # Onrac         -- blue
    6: SECTION_COLOR["wind"],        # Gaia          -- light green
    7: SECTION_COLOR["lategame"],    # Lufenia       -- light purple
}

# Same rule as BRIGHT_SECTIONS, for the shops sub-tab bar.
BRIGHT_SHOP_TOWNS = frozenset({1})

_SHOP_NAME = {s: nm for nm, s, _r in LOGIC.SHOP_LOCATIONS}


def shop_display(shop_index):
    """Human shop name, e.g. 'Cornelia Weapon Shop' (drops the ': AP Stock')."""
    nm = _SHOP_NAME.get(shop_index, f"Shop {shop_index}")
    return nm.replace(": AP Stock", "")


def shop_search(payload, query):
    """Every shelf matching `query` across the towns you have VISITED -- the data
    behind the Shops tab's Find view. Pure, so it can be tested without kivy
    (same reason the rest of the tracker's tab logic lives here).

    `payload` is ApClient._shops_payload(). Matching is case-insensitive
    substring, over AP offer item names, HINT row places, native shop stock, and
    magic-shop spell names. Unvisited towns are never searched: their stock is
    known client-side, but the tab treats a town you have not walked into as not
    yet revealed.

    Returns {"hits", "towns_visited", "towns_total", "groups": [...]} where each
    group is {"title", "kind", "offers", "hints", "stock", "spells"} -- one per
    shop (or per magic school) that had at least one match, in town then shop
    order.
    """
    q = (query or "").strip().lower()
    towns = (payload or {}).get("towns") or []
    visited = [t for t in towns if t.get("visited")]
    out = {"hits": 0, "towns_visited": len(visited), "towns_total": len(towns),
           "groups": []}
    if not q:
        return out

    def add(title, kind, offers=(), stock=(), spells=(), hints=()):
        n = len(offers) + len(stock) + len(spells) + len(hints)
        if not n:
            return
        out["hits"] += n
        out["groups"].append({"title": title, "kind": kind,
                              "offers": list(offers), "stock": list(stock),
                              "spells": list(spells), "hints": list(hints)})

    for town in visited:
        for shop in town.get("shops") or []:
            add(shop["name"], shop.get("kind"),
                offers=[o for o in shop.get("offers") or []
                        if q in (o.get("item") or "").lower()],
                stock=[it for it in shop.get("stock") or []
                       if q in (it.get("name") or "").lower()],
                # "hint" itself matches every hint row, which is the obvious
                # thing to type when you remember seeing one somewhere.
                hints=[h for h in shop.get("hints") or []
                       if q in (h.get("place") or "").lower()
                       or q in (h.get("short") or "").lower()
                       or q in ("hint", "hints")])
        magic = town.get("magic") or {}
        for school, key in (("Black Magic", "black"), ("White Magic", "white")):
            add(f"{town['name']} {school}", "magic",
                spells=[sp for sp in (magic.get(key) or [])
                        if q in (sp.get("name") or "").lower()])
    return out


def bonus_area_key(dg):
    """The RANDOM (procedural/dynamic) chest tile for a bonus dungeon."""
    return f"bonus_{dg}"


def bonus_static_key(dg):
    """The STATIC (fixed boss-chamber) chest tile for a bonus dungeon."""
    return f"bonus_static_{dg}"


# Static bonus-dungeon chests (treasure idx 252..267) assigned to their dungeon.
# SINGLE SOURCE = logic.DLC_STATIC_IDX_DUNGEON (the live-sweep mapping). A mapped
# idx shows in its dungeon's Static tile AND is gated exactly like that dungeon's
# dynamic chests (crystal + entrance tokens) -- matching __init__.set_rules. Any
# idx NOT mapped stays in the Unmapped Chests tile under UNMAPPED_RULE ([AIRSHIP]).
BONUS_STATIC_IDX = {
    dg: sorted(i for i, d in LOGIC.DLC_STATIC_IDX_DUNGEON.items() if d == dg)
    for dg in (0, 1, 2, 3)}
_STATIC_DG_OF_IDX = dict(LOGIC.DLC_STATIC_IDX_DUNGEON)


_REGION_SECTION = {k: s for k, _d, s in REGION_AREAS}


def npc_area_key(ordinal):
    """Every NPC/story check gets its OWN tile, split out of its region's chest
    tile -- so e.g. "Dragon Caves Chests" can read in-logic on the airship while
    "Dragon Caves - Bahamut" stays dim until you hold the Rat's Tail."""
    return f"npc_{ordinal}"


# region -> [(area key, display, section)] for the NPC checks inside it. Display is
# the AP location name, which already reads "Place - Person".
_NPC_BY_REGION = {}
for _nm, _o, _rgn, _extra in LOGIC.NPC_LOCATIONS:
    _NPC_BY_REGION.setdefault(_rgn, []).append(
        (npc_area_key(_o), _nm, _REGION_SECTION.get(_rgn, UNMAPPED)))


# ---- Sunken Shrine floor split ------------------------------------------------
# All 32 Sunken Shrine chests share one region (sunken_shrine_5f) in LOC_INFO, so
# the four sub-tiles are keyed on the treasure index, from the OBJ_LIST_TABLE map
# each chest record lives on (re_only/chest_map_derived.json): maps 25 = entrance,
# 26 + the 4F/5F block = the depths, 28 = the path down to the village, 30 = the
# village itself. Contiguous by construction -- the vanilla table is laid out map
# by map -- with idx 244 the one exception (a dedup repoint, see gen_apdata).
# This USED to key on the words "upper"/"lower"/"entrance" inside the AP location
# name; the 2026-08-07 uniform rename removed those words, which silently emptied
# two of the tiles. Index ranges can't drift out from under a rename.
# Display-only: every chest keeps the region's access rule, so generation is
# untouched, and the parent sunken_shrine_5f tile ends up EMPTY and hides itself.
SUNKEN_REGION = "sunken_shrine_5f"
SUNKEN_SUBAREAS = [
    ("ss_entrance", "Sunken Shrine Entrance", "water"),
    ("ss_upper",    "Sunken Shrine Path to Mermaids", "water"),
    ("ss_mermaid",  "Sunken Shrine Mermaid Village", "water"),
    ("ss_lower",    "Sunken Shrine Depths",          "water"),
]
_SS_IDX = {                      # sub-area key -> treasure indices (map-derived)
    "ss_entrance": frozenset({65, 66}),                          # map 25
    "ss_lower":    frozenset(range(55, 65)) | {67, 68},          # 4F/5F + map 26
    "ss_upper":    frozenset(range(69, 74)),                     # map 28
    "ss_mermaid":  frozenset(range(74, 86)) | {244},             # map 30
}
_SS_SUB = {}          # treasure idx -> sub-area key
for _key, _idxs in _SS_IDX.items():
    for _idx in _idxs:
        _r, _t = LOC_INFO.get(_idx, (None, None))
        if _r == SUNKEN_REGION:
            _SS_SUB[_idx] = _key

# ---- Mount Gulg floor split ---------------------------------------------------
# All 37 Mount Gulg chests share one region (mount_gulg_b2) in LOC_INFO. Unlike
# the Sunken Shrine their AP location names carry no floor, so the split is keyed
# on the treasure index instead -- the ranges below are the OBJ_LIST_TABLE map ids
# each chest record actually lives on (re_only/chest_map_derived.json, map 42/45/46
# = B2/B4/B5; B1 and B3 hold no chests at all). Contiguous by construction: the
# vanilla table is laid out floor by floor.
# Display-only -- every chest keeps the region's access rule, so generation is
# untouched. The parent "Mount Gulg Chests" tile ends up with zero locations of its
# own and hides itself (state EMPTY), leaving exactly the three floor tiles.
# B5 is 4 chests with loot_in_normally_empty_chests on and 1 with it off; the tile stays
# either way, because 188/189/191 simply never become locations when it is off.
GULG_REGION = "mount_gulg_b2"
GULG_SUBAREAS = [
    # Sub-tiles bypass _chest_display, so the "Chests" suffix is spelled out.
    ("gulg_b2", "Mount Gulg B2 Chests", "fire"),
    ("gulg_b4", "Mount Gulg B4 Chests", "fire"),
    ("gulg_b5", "Mount Gulg B5 Chests", "fire"),
]
_GULG_FLOOR_RANGES = (           # (first idx, last idx inclusive, sub-area key)
    (155, 172, "gulg_b2"),       # map 42 -- Chests 1-18
    (173, 187, "gulg_b4"),       # map 45 -- Chests 19-32 + 34
    (188, 191, "gulg_b5"),       # map 46 -- Chests 33, 35, 36, 37
)
_GULG_SUB = {}        # treasure idx -> sub-area key
for _idx, (_r, _t) in LOC_INFO.items():
    if _r != GULG_REGION:
        continue
    for _lo, _hi, _key in _GULG_FLOOR_RANGES:
        if _lo <= _idx <= _hi:
            _GULG_SUB[_idx] = _key
            break

# ---- Marsh Cave floor split ---------------------------------------------------
# All 18 Marsh Cave chests share one region (marsh_cave_b3) in LOC_INFO, so the
# split is keyed on the treasure index like Mount Gulg's. Ranges are the
# OBJ_LIST_TABLE map id + record coordinates each chest actually sits on
# (re_only/chest_map_derived.json + iso_patcher._CHEST_DEDUP_NORMALLY_EMPTY):
#   map 90 = B2, y 11/34 -> the north half; y 79/80 -> the south half
#   map 91 = B3
# B2 SOUTH is the three alias-duplicate records (idx 131/132/133, repointed off
# 127/129): normally empty in vanilla, so the whole tile disappears when
# loot_in_normally_empty_chests is off. B2 NORTH is four ordinary vanilla chests
# and is never affected by that option.
# Display-only -- every chest keeps marsh_cave_b3's access rule, so generation is
# untouched. The three Mystic Key chests (142-144) never reach a sub-tile:
# classify() routes a MysticKey chest to mk_marsh_cave_b3 before the floor split,
# which is why the B3 range can safely span them. The parent "Marsh Cave Chests"
# tile ends up with zero locations and hides itself (state EMPTY).
MARSH_REGION = "marsh_cave_b3"
MARSH_SUBAREAS = [
    # Sub-tiles bypass _chest_display, so the "Chests" suffix is spelled out.
    ("marsh_b2_north",   "Marsh Cave B2 North Chests",  "elfheim"),
    ("marsh_b2_south",   "Marsh Cave B2 South Chests",  "elfheim"),
    ("marsh_b3",         "Marsh Cave B3 Chests",        "elfheim"),
    # The Crown chest, carved out of B3 as a tile (and a hint product) of its
    # own -- it is the one chest in the cave a player goes there FOR, and its
    # singular name is why this tile's display ends in "Chest", not "Chests".
    ("marsh_piscodemon", "Marsh Cave Piscodemon Chest", "elfheim"),
]
_MARSH_FLOOR_RANGES = (          # (first idx, last idx inclusive, sub-area key)
    (127, 130, "marsh_b2_north"),   # map 90, y 11/34 -- vanilla chests
    (131, 133, "marsh_b2_south"),   # map 90, y 79/80 -- normally-empty twins
    (134, 144, "marsh_b3"),         # map 91 (142-144 split off to the MK tile)
)
# The Piscodemon-guarded Crown chest, map 91 (33,47) -- vanilla treasure idx 139
# (ff1_data.EVENT_KEY_CHESTS: "-> Crown"). Carved out of the B3 range AFTER it,
# so the range table stays a plain contiguous floor map.
MARSH_PISCODEMON_IDX = 139
_MARSH_SUB = {}       # treasure idx -> sub-area key
for _idx, (_r, _t) in LOC_INFO.items():
    if _r != MARSH_REGION:
        continue
    for _lo, _hi, _key in _MARSH_FLOOR_RANGES:
        if _lo <= _idx <= _hi:
            _MARSH_SUB[_idx] = _key
            break
if MARSH_PISCODEMON_IDX in _MARSH_SUB:
    _MARSH_SUB[MARSH_PISCODEMON_IDX] = "marsh_piscodemon"

# region -> extra display-only tiles that follow it (chests split out by floor).
_SUBAREAS_BY_REGION = {
    SUNKEN_REGION: SUNKEN_SUBAREAS,
    GULG_REGION: GULG_SUBAREAS,
    MARSH_REGION: MARSH_SUBAREAS,
}

# treasure idx -> display-only sub-tile, merged across every floor-split region.
_FLOOR_SUB = dict(_SS_SUB)
_FLOOR_SUB.update(_GULG_SUB)
_FLOOR_SUB.update(_MARSH_SUB)


def mk_area_key(region):
    """The Mystic-Key tile for a region's locked chests."""
    return f"mk_{region}"


# Mystic-Key tiles, DERIVED from LOC_INFO so the set can never drift from the data:
# one tile per region that owns at least one MysticKey-token chest, in treasure-index
# order. classify() sends those chests here instead of to the geographic tile. Each
# lives in its REGION's own section, immediately after the region tile -- so the
# locked chests read as a sibling of the open ones, not a separate errand.
_MK_BY_REGION = {}
for _idx in sorted(LOC_INFO):
    _rgn, _tok = LOC_INFO[_idx]
    if _tok == "MysticKey" and _rgn is not None and _rgn not in _MK_BY_REGION:
        _MK_BY_REGION[_rgn] = (
            mk_area_key(_rgn),
            f"{_REGION_DISPLAY.get(_rgn, _rgn)} (Mystic Key)",
            _REGION_SECTION.get(_rgn, UNMAPPED))
MYSTIC_KEY_AREAS = list(_MK_BY_REGION.values())

def _chest_display(display):
    """Region tiles hold CHESTS only now (NPCs split off), so label them so."""
    return display if display.endswith("Chests") else f"{display} Chests"


# Full ordered area list: (key, display, section). One entry per tile. Per region:
# its chest tile, then any floor-split sub-tiles, then its Mystic-Key tile, then one
# tile per NPC check inside it.
AREAS = []
for _k, _d, _s in REGION_AREAS:
    AREAS.append((_k, _chest_display(_d), _s))
    for _sub in _SUBAREAS_BY_REGION.get(_k, ()):
        AREAS.append(_sub)
    if _k in _MK_BY_REGION:
        AREAS.append(_MK_BY_REGION[_k])
    for _npc in _NPC_BY_REGION.get(_k, ()):
        AREAS.append(_npc)
# Bonus dungeons: two tiles each, RANDOM (procedural) above STATIC (boss-chamber).
for _dg, _name, _fl, _cap, _tok, _attr in LOGIC.BONUS_DUNGEONS:
    AREAS.append((bonus_area_key(_dg), f"{_name} (Random)", "bonus"))
    AREAS.append((bonus_static_key(_dg), f"{_name} (Static)", "bonus"))
AREAS.extend((k, d, s) for k, d, s, _i in SHOP_AREAS)

AREA_DISPLAY = {k: d for k, d, _s in AREAS}
AREA_SECTION = {k: s for k, _d, s in AREAS}
AREAS_BY_SECTION = {sk: [k for k, _d, s in AREAS if s == sk] for sk in SECTION_KEYS}

# --------------------------------------------------------------- key items ----
# The pinned "what do I hold" strip. Every real pool item you FIND, plus the two
# derived gates that are milestones in their own right -- the Airship and the Black
# Orb. The other derived tokens (Bridge, Canal, each crystal, the boss-defeat
# flags) are intentionally NOT shown: they're bookkeeping the fixpoint tracks, not
# things the player collects, and a strip full of them buries the items that matter.
POOL_GATE_ITEMS = [
    (LOGIC.LUTE, "LUTE"), (LOGIC.SHIP, "SHIP"), (LOGIC.CANOE, "CANOE"),
    (LOGIC.CROWN, "CROWN"), (LOGIC.CRYSTAL_EYE, "EYE"), (LOGIC.JOLT_TONIC, "TONIC"),
    (LOGIC.MYSTIC_KEY, "KEY"), (LOGIC.NITRO, "NITRO"), (LOGIC.STAR_RUBY, "RUBY"),
    (LOGIC.EARTH_ROD, "ROD"), (LOGIC.LEVISTONE, "LEVI"), (LOGIC.RATS_TAIL, "TAIL"),
    (LOGIC.ROSETTA, "ROSETTA"), (LOGIC.CHIME, "CHIME"), (LOGIC.WARP_CUBE, "CUBE"),
    (LOGIC.BOTTLED_FAERIE, "FAERIE"), (LOGIC.OXYALE, "OXYALE"),
    (LOGIC.ADAMANTITE, "ADAMANT"),
]

# The two derived gates worth surfacing alongside the pool items.
EARNED_GATE_ITEMS = [
    (LOGIC.AIRSHIP, "AIRSHIP"), (LOGIC.BLACK_ORB_DESTROYED, "CHAOS"),
]

# Everything shown in the strip, in order. Its names are also the universe over
# which the green "unspent" flag is computed -- only chips the player can see.
STRIP_ITEMS = POOL_GATE_ITEMS + EARNED_GATE_ITEMS

# Long token name -> the short label used on tiles' "needs:" line. Kept COMPLETE
# (every gate, including the ones the strip hides) so a dim tile's "needs: CANAL"
# still renders a short label rather than the full token string.
SHORT_TOKEN = {n: s for n, s in POOL_GATE_ITEMS}
SHORT_TOKEN.update({
    LOGIC.CANAL: "CANAL", LOGIC.AIRSHIP: "AIRSHIP",
    LOGIC.GARLAND_DEFEATED: "GARLAND", LOGIC.VAMPIRE_DEFEATED: "VAMPIRE",
    LOGIC.TITAN_FED: "TITAN", LOGIC.ROSETTA_TRANSLATED: "UNNE",
    LOGIC.EARTH_CRYSTAL: "EARTH", LOGIC.FIRE_CRYSTAL: "FIRE",
    LOGIC.WATER_CRYSTAL: "WATER", LOGIC.AIR_CRYSTAL: "AIR",
    LOGIC.BLACK_ORB_DESTROYED: "CHAOS", LOGIC.CRYSTALS_PLACED: "CRYSTALS",
})


def short_reqs(tokens):
    """Render an unmet-requirement list for a tile face."""
    return ", ".join(SHORT_TOKEN.get(t, t) for t in tokens)


# ------------------------------------------------------------ rule algebra ----
def as_alts(rule):
    """Normalize a rule to a list of AND-alternatives. Mirrors
    __init__.set_rules.as_alts / logic._rule_alts -- an empty rule is one
    always-true alternative, not zero alternatives."""
    if not rule:
        return [[]]
    if isinstance(rule[0], (list, tuple)):
        return [list(a) for a in rule]
    return [list(rule)]


def unmet(alts, extra, owned):
    """Cheapest set of tokens still missing, over every OR-alternative. Returns []
    when reachable. `extra` (per-chest / per-location AND tokens) applies to every
    alternative, matching make_rule."""
    best = None
    ex_missing = [t for t in extra if t not in owned]
    for alt in alts:
        miss = [t for t in alt if t not in owned] + ex_missing
        if best is None or len(miss) < len(best):
            best = miss
            if not best:
                break
    return best if best is not None else ex_missing


def _reachable(alts, extra, owned):
    return not unmet(alts, extra, owned)


def resolve_tokens(owned_names, region_rules, forbid=(), crystals_needed=4,
                   bonus_crystals=False, early=False):
    """Run the EVENTS fixpoint: grant each event's token once its region rule and
    extra reqs are satisfied, repeating until stable. Returns a NEW set of every
    item name plus every derived gate token.

    This is the client-side stand-in for what AP's CollectionState sweep does at
    generation time (logic.EVENTS' comment: "Order doesn't matter; AP resolves the
    fixpoint" -- here, we resolve it).

    `forbid` names are stripped from the seed set AND may never be granted by an
    event. That is what makes the counterfactual in `_unused_gates` honest: to ask
    "what does the Nitro Powder actually unlock", you have to deny the Canal it
    would otherwise derive, not merely drop the Nitro from the item list.
    """
    banned = set(forbid)
    tokens = set(owned_names) - banned
    changed = True
    while changed:
        changed = False
        for _name, region, granted, extra in LOGIC.EVENTS:
            # bonus_dungeon_crystals: the Fiend event grants a "<Fiend> Defeated"
            # token instead of the crystal (which is derived below from clearing the
            # bonus dungeon). Mirrors __init__.create_regions' fiend->fiend-token swap.
            if bonus_crystals and granted in LOGIC.CRYSTALS:
                granted = LOGIC.FIEND_TOKEN[granted]
            if granted in tokens or granted in banned:
                continue
            # crystals_needed < 4: the Black Orb (and the plaza's Crystals Placed
            # gate) need any N of the four crystals instead of all of them (mirrors
            # __init__.set_rules' count rule and the on-disc wrapper cave).
            if (granted in (LOGIC.BLACK_ORB_DESTROYED, LOGIC.CRYSTALS_PLACED)
                    and crystals_needed < 4):
                if sum(1 for c in LOGIC.CRYSTALS if c in tokens) < crystals_needed:
                    continue
                extra = [t for t in extra if t not in LOGIC.CRYSTALS]
            if _reachable(as_alts(region_rules.get(region, [])), extra, tokens):
                tokens.add(granted)
                changed = True
        # bonus_dungeon_crystals: derive each crystal from CLEARING its dungeon --
        # the same rule as that dungeon's chests (fiend token + entrance tokens, the
        # crystal itself NOT required, so no cycle). Mirrors the "<Dungeon> - Cleared"
        # events in set_rules. Off -> loop body never runs (default byte-identical).
        if bonus_crystals:
            for dg, _nm, _fl, _cap, crystal, _a in LOGIC.BONUS_DUNGEONS:
                if crystal in tokens or crystal in banned:
                    continue
                extra = ([LOGIC.FIEND_TOKEN[crystal]]
                         + list(LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(dg, [])))
                if dg == 3:
                    alts = [[]] if early else [list(LOGIC.WHISPERWIND_SHIP_CANAL_ALT)]
                else:
                    alts = [[]]
                if _reachable(alts, extra, tokens):
                    tokens.add(crystal)
                    changed = True
    return tokens


# ------------------------------------------------------- location -> rule -----
def _is_npc_loc(lid):
    return ID.BASE + ID.NPC_OFF <= lid < ID.BASE + ID.NPC_OFF + 0x100


# Static chest treasure indices are 0..267 (treasure-table-static-count memory).
# The bound must be tight: ids.VICTORY (BASE + 0xFFF) also lives just above BASE,
# so a loose upper bound would swallow it and invent a phantom chest.
N_STATIC_CHESTS = 268


def _is_chest_loc(lid):
    return ID.BASE <= lid < ID.BASE + N_STATIC_CHESTS


_NPC_BY_ORD = {o: (nm, rgn, extra) for nm, o, rgn, extra in LOGIC.NPC_LOCATIONS}
_SHOP_BY_IDX = {s: (nm, reqs) for nm, s, reqs in LOGIC.SHOP_LOCATIONS}
_BONUS_BY_DG = {dg: (nm, tok) for dg, nm, _fl, _cap, tok, _a
                in LOGIC.BONUS_DUNGEONS}


def classify(lid, region_rules, shop_overrides, early, bonus_crystals=False):
    """location id -> (area_key, alts, extra) or None if the id isn't ours.

    Mirrors the four rule-application loops in __init__.set_rules exactly: chests
    (region rule + MysticKey/TitanFed token), events/NPCs (region rule + extras),
    shops (town tokens, overridable per shop NAME), and dynamic bonus chests (the
    element crystal + the dungeon's entrance tokens, with Whisperwind's Ice Cave
    access as an OR)."""
    if ID.is_shop_loc(lid):
        shop, _k = ID.shop_loc_shop_k(lid)
        ent = _SHOP_BY_IDX.get(shop)
        if ent is None:
            return None
        nm, reqs = ent
        rule = shop_overrides.get(nm, reqs)
        return SHOP_AREA_OF_SHOP.get(shop), as_alts(rule), []

    if ID.is_dyn_chest(lid):
        dg, _o = ID.dyn_chest_dungeon_ord(lid)
        ent = _BONUS_BY_DG.get(dg)
        if ent is None:
            return None
        _nm, gate_tok = ent
        # bonus_dungeon_crystals: chests gate on the Fiend-defeated token, not the
        # crystal (which they help produce) -- mirrors set_rules' dungeon_gate.
        if bonus_crystals:
            gate_tok = LOGIC.FIEND_TOKEN[gate_tok]
        extra = [gate_tok] + list(LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(dg, []))
        if dg == 3:
            # Whisperwind Cove also needs Ice Cave access: the early-progression
            # canoe river (free) OR Ship + Nitro Powder.
            alts = [[]] if early else [list(LOGIC.WHISPERWIND_SHIP_CANAL_ALT)]
        else:
            alts = [[]]
        return bonus_area_key(dg), alts, extra

    if _is_npc_loc(lid):
        ordinal = lid - ID.BASE - ID.NPC_OFF
        ent = _NPC_BY_ORD.get(ordinal)
        if ent is None:
            return None
        _nm, region, extra = ent
        # Own tile, but the SAME rule as set_rules: region + the NPC's own reqs,
        # OR an NPC_ALT_RULES bypass (Sarda by airship). npc_rule_alts bakes the
        # extras into the alternatives, so extra is empty here.
        return (npc_area_key(ordinal),
                LOGIC.npc_rule_alts(ordinal, region_rules.get(region, []), extra),
                [])

    if _is_chest_loc(lid):
        idx = ID.loc_index(lid)
        # A static bonus chest live-swept to its dungeon: Static tile, and the
        # SAME rule as the dungeon's dynamic chests (crystal + entrance tokens,
        # + the Whisperwind Ice-Cave alt) -- matching __init__.set_rules.
        _dg = _STATIC_DG_OF_IDX.get(idx)
        if _dg is not None:
            _nm, gate_tok = _BONUS_BY_DG[_dg]
            if bonus_crystals:
                gate_tok = LOGIC.FIEND_TOKEN[gate_tok]
            extra = [gate_tok] + list(LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(_dg, []))
            if _dg == 3:
                alts = [[]] if early else [list(LOGIC.WHISPERWIND_SHIP_CANAL_ALT)]
            else:
                alts = [[]]
            return bonus_static_key(_dg), alts, extra
        region, chest_token = LOC_INFO.get(idx, (None, None))
        if region is None:
            rule = LOGIC.UNMAPPED_RULE
            geo_area = UNMAPPED
        else:
            rule = region_rules.get(region, [])
            # Sunken Shrine / Mount Gulg chests split by floor (display only --
            # same rule as the region they came from).
            geo_area = _FLOOR_SUB.get(idx, region)
        if chest_token == "MysticKey":
            # Pull the locked chest out of its geographic tile into the Mystic Key
            # section. Same access rule (region rule + the Key), different tile.
            area = mk_area_key(region) if region is not None else UNMAPPED
            extra = [LOGIC.MYSTIC_KEY]
        elif chest_token == "TitanFed":
            area, extra = geo_area, [LOGIC.TITAN_FED]
        else:
            area, extra = geo_area, []
        return area, as_alts(rule), extra

    return None


# ------------------------------------------------------------- evaluation -----
# Tile states, brightest-first. The GUI maps these to colors; the ordering here is
# also the Summary's sort key.
IN_LOGIC = "in_logic"      # reachable checks remain -> bright, full color
OUT_LOGIC = "out_logic"    # nothing reachable, checks remain -> dim + slashes
CLEARED = "cleared"        # every check found -> grayscale
EMPTY = "empty"            # no locations in this seed's pool -> tile hidden


class AreaState:
    """Per-tile rollup. `open_now` counts locations that are reachable AND unfound
    -- the number the player can actually go get right now. `gated` counts unfound
    locations inside the area that are NOT reachable, which is what makes a MIXED
    area (in logic, but e.g. its Mystic Key chests still locked)."""

    __slots__ = ("key", "display", "section", "found", "total",
                 "open_now", "gated", "needs", "state")

    def __init__(self, key, display, section):
        self.key = key
        self.display = display
        self.section = section
        self.found = 0
        self.total = 0
        self.open_now = 0
        self.gated = 0
        self.needs = []        # cheapest unmet token list, for the "needs:" line
        self.state = EMPTY

    @property
    def remaining(self):
        return self.total - self.found

    @property
    def mixed(self):
        return self.state == IN_LOGIC and self.gated > 0

    def __repr__(self):
        return (f"<AreaState {self.key} {self.state} {self.found}/{self.total} "
                f"open={self.open_now} gated={self.gated}>")


class TrackerState:
    __slots__ = ("areas", "tokens", "owned", "found_total", "pool_total",
                 "unused", "crystals_have", "crystals_need",
                 "tablets_have", "tablets_need",
                 "runes_have", "runes_need",
                 "shards_have", "shards_need")

    def __init__(self):
        self.areas = {}        # area key -> AreaState
        self.tokens = set()    # items + derived event tokens
        self.owned = set()     # items only (no derived tokens)
        self.unused = set()    # held gates whose unlocks are all still unchecked
        self.found_total = 0
        self.pool_total = 0
        # Endgame gate progress, surfaced on the strip's totals line.
        self.crystals_have = 0   # crystals earned (Fiend tokens held)
        self.crystals_need = 0   # crystals_needed (0 = no crystal gate)
        self.tablets_have = 0    # Lute Tablets held
        self.tablets_need = 0    # lute_tablets_required (0 = lute_tablets off)
        self.runes_have = 0      # Equipment Runes held (clamped to need)
        self.runes_need = 0      # equipment_runes_required (0 = the option is off)
        self.shards_have = 0     # Levistone Shards held
        self.shards_need = 0     # levistone_shards_required (0 = option off)

    def summary_areas(self):
        """Areas the player can make progress in RIGHT NOW: in logic with checks
        left. Sorted most-reachable-first so the biggest wins are on top."""
        out = [a for a in self.areas.values() if a.state == IN_LOGIC]
        out.sort(key=lambda a: (-a.open_now, a.display))
        return out

    def section_rollup(self, section):
        """(found, total) over a section's non-empty areas."""
        f = t = 0
        for k in AREAS_BY_SECTION.get(section, ()):
            a = self.areas.get(k)
            if a is not None and a.state != EMPTY:
                f += a.found
                t += a.total
        return f, t

    def section_in_logic(self, section):
        """True if any area in the section has reachable checks left -- drives the
        sub-tab's in-logic shine (e.g. Water shines while Sunken Shrine is open)."""
        return any(self.areas.get(k) is not None
                   and self.areas[k].state == IN_LOGIC
                   for k in AREAS_BY_SECTION.get(section, ()))

    def section_shine(self, section):
        """True iff the section is in logic AND none of its in-logic areas have
        any check found yet. Mirrors the tile shine (in logic, found == 0) at the
        tab level: the strip stops shining as soon as the player checks anything
        reachable in the section, even while other areas there stay untouched."""
        any_in_logic = False
        for k in AREAS_BY_SECTION.get(section, ()):
            a = self.areas.get(k)
            if a is not None and a.state == IN_LOGIC:
                any_in_logic = True
                if a.found > 0:
                    return False
        return any_in_logic


STRIP_GATES = [n for n, _s in STRIP_ITEMS]


def _unused_gates(st, locs, region_rules, crystals_needed=4,
                  bonus_crystals=False, early=False):
    """Which held gates the player has not cashed in yet.

    "Used" is defined by counterfactual rather than by a hand-written table of
    "Crown -> Astos, Crystal Eye -> Matoya, ...": a location DEPENDS on gate X iff
    it is reachable with everything you hold but NOT reachable once X (and
    everything X derives) is denied. X is UNUSED iff it has dependents and you have
    checked none of them.

    One rule, and it covers both flavours the player thinks of separately: trade
    items (the Crown's only dependent is the Astos location) and access items (the
    Mystic Key's dependents are the Mystic-Key chests; Oxyale's are the Sunken
    Shrine). It also cannot drift -- a new gate or a re-pointed requirement is
    picked up for free, where a table would quietly go stale.
    """
    unused = set()
    for name in STRIP_GATES:
        if name not in st.tokens:
            continue
        without = resolve_tokens(st.owned - {name}, region_rules, forbid=(name,),
                                 crystals_needed=crystals_needed,
                                 bonus_crystals=bonus_crystals, early=early)
        deps = found = 0
        for _lid, alts, extra, checked in locs:
            if unmet(alts, extra, st.tokens):
                continue                       # not reachable anyway -> not a dep
            if not unmet(alts, extra, without):
                continue                       # reachable without X -> not a dep
            deps += 1
            if checked:
                found += 1
                break                          # one is enough to call it used
        if deps and not found:
            unused.add(name)
    return unused


def evaluate(item_names, pool_loc_ids, checked_loc_ids, slot_data=None,
             tablet_count=0, rune_count=0, shard_count=0):
    """Compute the whole tracker.

    item_names      -- iterable of AP item NAMES the player has received. Feed this
                       from the STICKY set (_ever_won -> names), never straight from
                       items_received: that list is transiently emptied on a
                       disconnect and the tracker would blink every area to
                       out-of-logic mid-session.
    pool_loc_ids    -- every location id this slot owns: missing | checked.
    checked_loc_ids -- ids already found: checked | locally sent.
    slot_data       -- the seed's options; the three open-progression toggles
                       plus lute_tablets_required matter to logic.
    tablet_count    -- Lute Tablets held (lute_tablets seeds). item_names is a SET
                       (duplicate copies collapse to one name), so the count rides
                       separately; feed it from the client's sticky tablet counter.
    rune_count      -- Equipment Runes held (equipment_runes seeds). Same reason
                       as tablet_count. DISPLAY ONLY: the activatable-equipment
                       gate is story flag 62, not an AP logic item, so this never
                       moves reachability -- it exists so the count stays visible
                       where the in-game Key Items line cannot be shown.
    shard_count     -- Levistone Shards held (levistone_shards seeds). Same
                       sticky-counter reason as tablet_count; at the threshold
                       the LEVISTONE derives (mirrors set_rules' count rule on
                       the locked "Levistone Assembled" event).
    """
    sd = slot_data or {}
    early = bool(sd.get("early_open_progression"))
    extended = bool(sd.get("extended_open_progression"))
    docks = bool(sd.get("northern_docks"))

    region_rules = LOGIC.region_rules_for(early, extended, docks)
    shop_overrides = LOGIC.shop_rules_for(early, extended, docks)

    st = TrackerState()
    st.owned = set(item_names)
    # lute_tablets: the Lute is never a pool item -- it DERIVES from holding
    # lute_tablets_required tablets (mirrors __init__.set_rules' count rule on
    # the locked "Lute Assembled" event). Owned, not a fixpoint token, so the
    # LUTE strip chip lights up too.
    lute_need = int(sd.get("lute_tablets_required") or 0)
    if lute_need and tablet_count >= lute_need:
        st.owned.add(LOGIC.LUTE)
    # levistone_shards: same derivation for the Levistone (the airship then
    # follows from the normal AIRSHIP event token in resolve_tokens).
    levi_need = int(sd.get("levistone_shards_required") or 0)
    if levi_need and shard_count >= levi_need:
        st.owned.add(LOGIC.LEVISTONE)
    # crystals_needed: 4 (or absent, old seeds) = vanilla all-four gate.
    _cn = sd.get("crystals_needed")
    crystals_needed = 4 if _cn is None else int(_cn)
    # bonus_dungeon_crystals: crystals derive from clearing the bonus dungeons, not
    # from the Fiends (absent/False in old seeds -> vanilla path, byte-identical).
    bonus_crystals = bool(sd.get("bonus_dungeon_crystals"))
    st.tokens = resolve_tokens(st.owned, region_rules,
                               crystals_needed=crystals_needed,
                               bonus_crystals=bonus_crystals, early=early)
    # Endgame gate progress for the strip's totals line.
    st.crystals_need = crystals_needed
    st.crystals_have = sum(1 for c in LOGIC.CRYSTALS if c in st.tokens)
    st.tablets_need = lute_need
    st.tablets_have = int(tablet_count)
    # CLAMP: the client's counter is an uncapped high-water, so an
    # equipment_runes_extra seed would otherwise read "Runes 13/10" beside an
    # in-game "Rune Key".
    st.runes_need = int(sd.get("equipment_runes_required") or 0)
    st.runes_have = (min(int(rune_count), st.runes_need) if st.runes_need
                     else int(rune_count))
    # CLAMPED for the same reason as the runes above, and it bites harder here:
    # levistone_shards_percentage/extra can put ~10x Required shards in the pool
    # (9 required, up to 98 placed), so an unclamped strip would read
    # "Levi Shards 90/9" (user 2026-08-12). The in-game menu line never shows
    # this -- it clamps and then hands the slot back to the real "Levistone"
    # entry at assembly -- and the strip must not disagree with it.
    st.shards_need = levi_need
    st.shards_have = (min(int(shard_count), levi_need) if levi_need
                      else int(shard_count))

    for key, display, section in AREAS:
        st.areas[key] = AreaState(key, display, section)

    checked = set(checked_loc_ids)
    # Cheapest unmet requirement seen per area, over its UNFOUND locations only --
    # a found location's requirement is history and shouldn't drive the tile face.
    best_needs = {}
    locs = []      # (lid, alts, extra, checked) -- reused by _unused_gates

    for lid in pool_loc_ids:
        info = classify(lid, region_rules, shop_overrides, early, bonus_crystals)
        if info is None:
            continue
        area_key, alts, extra = info
        area = st.areas.get(area_key)
        if area is None:
            continue
        locs.append((lid, alts, extra, lid in checked))

        area.total += 1
        st.pool_total += 1
        if lid in checked:
            area.found += 1
            st.found_total += 1
            continue

        missing = unmet(alts, extra, st.tokens)
        if missing:
            area.gated += 1
            prev = best_needs.get(area_key)
            if prev is None or len(missing) < len(prev):
                best_needs[area_key] = missing
        else:
            area.open_now += 1

    for area in st.areas.values():
        if area.total == 0:
            area.state = EMPTY
        elif area.found >= area.total:
            area.state = CLEARED
        elif area.open_now > 0:
            # Reachable work exists here -> in logic, even if some checks inside are
            # still gated (the tile reports the split; see AreaState.mixed).
            area.state = IN_LOGIC
            area.needs = best_needs.get(area.key, [])
        else:
            area.state = OUT_LOGIC
            area.needs = best_needs.get(area.key, [])

    st.unused = _unused_gates(st, locs, region_rules,
                              crystals_needed=crystals_needed,
                              bonus_crystals=bonus_crystals, early=early)
    return st
