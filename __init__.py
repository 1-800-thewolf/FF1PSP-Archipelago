"""
Final Fantasy 1 PSP (ULUS10251, 20th Anniversary Edition) — Archipelago world.

Item randomizer with full access logic (set_rules builds the region/rule graph
from logic.py): 254 static chest locations, 15 NPC/story checks, optional shop
AP offers and yaml-capped dynamic Soul-of-Chaos bonus-dungeon chests. Goal =
Chaos defeated, reported by the runtime client (ff1psp/client/ApClient.py) via
StatusUpdate.
"""
from BaseClasses import Item, ItemClassification, Location, Region
from worlds.AutoWorld import World, WebWorld
from worlds.LauncherComponents import Component, Type, components, launch as launch_subprocess
import worlds.LauncherComponents as components_module

try:
    from Options import OptionGroup
except ImportError:
    OptionGroup = None

try:
    from Options import OptionError
except ImportError:      # older / stubbed Options predate OptionError
    OptionError = Exception

from . import data as DATA
from . import hints as HINTS
from . import ids as ID
from . import logic as LOGIC
from . import rando as RANDO
from . import rando_data as RD
from ._locinfo import LOC_INFO
from .client import class_names as CN      # pure data (no client deps at gen time)
from .client import ff1_data as FF1DATA     # pure data: WEAPONS/ARMOR gid->name maps
from .options import (
    FF1PSPOptions, OpenProgression, resolve_party_jobs,
)

GAME = "Final Fantasy 1 PSP"
VICTORY_ITEM = "Victory"
GOAL_LOCATION = "Chaos"


# ---- name <-> id maps (module level so AutoWorld can read them) ----
ITEM_NAME_TO_ID = {VICTORY_ITEM: ID.BASE + ID.VICTORY}
ITEM_NAME_TO_CLASS = {VICTORY_ITEM: ItemClassification.progression}
for _iid, (_name, _kind, _cat, _gid, _qty) in DATA.ITEM_TABLE.items():
    ITEM_NAME_TO_ID[_name] = _iid
    ITEM_NAME_TO_CLASS[_name] = (
        ItemClassification.progression if _kind == "key"
        else ItemClassification.filler
    )

LOCATION_NAME_TO_ID = {name: lid for (lid, name, _idx) in DATA.LOCATIONS}
# NPC story-event locations (King / Princess) — real AP checks outside chest space.
for _nm, _ord, _rgn, _extra in LOGIC.NPC_LOCATIONS:
    LOCATION_NAME_TO_ID[_nm] = ID.npc_loc_id(_ord)
# Shop AP-stock locations — one-time AP purchases in town stores (count set by
# shop_ap_offers > 0; ALL possible offer ids registered so the datapackage is stable).
for _nm, _shop, _reqs in LOGIC.SHOP_LOCATIONS:
    for _k in range(LOGIC.SHOP_MAX_OFFERS):
        LOCATION_NAME_TO_ID[LOGIC.shop_location_name(_nm, _k)] = \
            ID.shop_loc_id(_shop, _k)
# Dynamic bonus-dungeon chest locations (Soul-of-Chaos procedural chests). ALL
# possible ordinals per dungeon (up to its floor count) are registered so the
# datapackage stays stable regardless of the per-dungeon count yaml; only the
# resolved cap is actually created (create_regions). See logic.BONUS_DUNGEONS.
for _dg, _dname, _floors, _defcap, _tok, _attr in LOGIC.BONUS_DUNGEONS:
    for _o in range(_floors):
        LOCATION_NAME_TO_ID[LOGIC.dyn_chest_location_name(_dname, _o)] = \
            ID.dyn_chest_loc_id(_dg, _o)

# The Lute is a REAL game key item (cat 0, game id 1) that the client can grant via
# its key-item bitfield. It is no longer an event token -- it goes in the pool and is
# found anywhere in the multiworld; the endgame gates on having the "Lute" item.
LUTE_ITEM_ID = ID.item_id(0, 1)
ITEM_NAME_TO_ID[LOGIC.LUTE] = LUTE_ITEM_ID
ITEM_NAME_TO_CLASS[LOGIC.LUTE] = ItemClassification.progression

# Lute Tablet (lute_tablets yaml): synthetic progression pieces of the Lute. When
# the option is on, the Lute leaves the pool and lives at a locked "Lute Assembled"
# event gated on holding lute_tablets_required tablets -- so every LUTE-gated rule
# (Black Orb, endgame) is untouched. The client counts received copies and sets the
# Lute possession bit at the threshold. Registered unconditionally (stable
# datapackage); all copies share one id.
LUTE_TABLET_ITEM_ID = ID.tablet_item_id()
ITEM_NAME_TO_ID[LOGIC.LUTE_TABLET] = LUTE_TABLET_ITEM_ID
ITEM_NAME_TO_CLASS[LOGIC.LUTE_TABLET] = ItemClassification.progression

# Equipment Rune (equipment_runes yaml): pieces of the Equipment Rune Key. Hold
# equipment_runes_required of them and the client sets story flag 62; until then
# the on-disc battle-usability gate greys out ALL activatable (spell-on-use)
# equipment in the battle item menu. USEFUL, not progression: nothing in logic
# gates on activating equipment, so fill owes it no reachability guarantees.
# Registered unconditionally (stable datapackage); all copies share one id.
EQUIPMENT_RUNE_ITEM_ID = ID.rune_item_id()
ITEM_NAME_TO_ID[LOGIC.EQUIPMENT_RUNE] = EQUIPMENT_RUNE_ITEM_ID
ITEM_NAME_TO_CLASS[LOGIC.EQUIPMENT_RUNE] = ItemClassification.useful

# Levistone Shard (levistone_shards yaml): synthetic progression pieces of the
# Levistone. When the option is on, the Levistone leaves the pool and lives at a
# locked "Levistone Assembled" event gated on holding levistone_shards_required
# shards -- so the LEVISTONE-gated rule (the Ryukhan Desert Airship event) is
# untouched. The client counts received copies and grants the real Levistone
# (possession + obtained/airship bits) at the threshold. Registered
# unconditionally (stable datapackage); all copies share one id.
LEVISTONE_SHARD_ITEM_ID = ID.shard_item_id()
ITEM_NAME_TO_ID[LOGIC.LEVISTONE_SHARD] = LEVISTONE_SHARD_ITEM_ID
ITEM_NAME_TO_CLASS[LOGIC.LEVISTONE_SHARD] = ItemClassification.progression

# The Ship is a REAL pool item (progression) found anywhere in the multiworld. It is
# NOT a game inventory item -- the client applies it by setting story-flag id5
# (0x08D1151C bit5), which spawns the vanilla ship at Provoka. Its own vehicle id
# space (ids.is_vehicle) tells the client to grant it via that flag write, not the
# inventory. Bikke's defeat is a separate NPC location; see ship-bikke-flag memory.
SHIP_ITEM_ID = ID.vehicle_item_id(ID.SHIP_VEHICLE)
ITEM_NAME_TO_ID[LOGIC.SHIP] = SHIP_ITEM_ID
ITEM_NAME_TO_CLASS[LOGIC.SHIP] = ItemClassification.progression

# The Canoe is a REAL game key item (cat 0, game id 17) found anywhere in the
# multiworld. Like the Lute it is a normal inventory key item -- the client delivers
# it by setting its key-item bit (0x08D11539 & 0x80). It is no longer an event token;
# the Crescent Lake sage (which used to hand it over) is now a randomized NPC
# location, and the region rules gate on having the "Canoe" item. See logic.py.
CANOE_ITEM_ID = ID.item_id(0, 17)
ITEM_NAME_TO_ID[LOGIC.CANOE] = CANOE_ITEM_ID
ITEM_NAME_TO_CLASS[LOGIC.CANOE] = ItemClassification.progression

# Promoted NPC key items (2026-07-06 batches; registration RESTORED 2026-07-20 --
# it went missing at some point, so create_items KeyError'd on 'Earth Rod' and
# EVERY gen failed). Real game key items (cat 0) delivered via the client's
# key-item bitfield + function bits; each balances its NET-NEW NPC location
# appended in create_items. gids match client ff1_data.KEY_ITEMS / logic.py.
for _kname, _kgid, _kcls in (
        (LOGIC.CRYSTAL_EYE,     3,  ItemClassification.progression),
        (LOGIC.JOLT_TONIC,      4,  ItemClassification.progression),
        (LOGIC.MYSTIC_KEY,      5,  ItemClassification.progression),
        (LOGIC.ADAMANTITE,      7,  ItemClassification.progression),  # gates the Smith location
        (LOGIC.EARTH_ROD,      10,  ItemClassification.progression),
        (LOGIC.CHIME,          12,  ItemClassification.progression),
        (LOGIC.WARP_CUBE,      14,  ItemClassification.progression),
        (LOGIC.BOTTLED_FAERIE, 15,  ItemClassification.progression),
        (LOGIC.OXYALE,         16,  ItemClassification.progression)):
    ITEM_NAME_TO_ID[_kname] = ID.item_id(0, _kgid)
    ITEM_NAME_TO_CLASS[_kname] = _kcls
# Excalibur: WEAPON pool item (cat 2, game id 39), the Smith turn-in's reward.
ITEM_NAME_TO_ID[LOGIC.EXCALIBUR] = ID.item_id(2, 39)
ITEM_NAME_TO_CLASS[LOGIC.EXCALIBUR] = ItemClassification.useful

# Event (gate) items are not real game items: synthetic ids (precollected ones
# DO reach the client as starting inventory, which treats them counter-only via
# ids.is_event). They exist so the AP fill graph is connected. id space must
# match ids.EVENT_OFF.
_EVENT_BASE = ID.BASE + ID.EVENT_OFF
for _i, _gname in enumerate(LOGIC.GATE_ITEMS):
    ITEM_NAME_TO_ID[_gname] = _EVENT_BASE + _i
    ITEM_NAME_TO_CLASS[_gname] = ItemClassification.progression

# Job-advancement scroll items were REMOVED 2026-07-13: client-side promotion is
# impossible (the game's promote routine only works inside its native Bahamut
# event's lineup-scene context -- see job-advancement-items memory). Promotion is
# now the game's NATIVE Bahamut turn-in: the Rat's Tail is an AP item that, when
# received, functionally promotes the whole party at Bahamut (client sets the
# turn-in function bit; see ff1_data.KEY_ITEM_FUNCTION_BITS[13]). Bahamut is also
# an AP location (logic.NPC_LOCATIONS, gated on AIRSHIP + Rat's Tail).

# Job Scroll boosts (opt-out via JobScrollBoosts, default on; 2026-07-13): the
# scroll id space (ids.job_item_id, keyed by BASE job 0..5) is reused for six
# permanent per-class BOOST items (NOT promotions -- promotion stays native
# Bahamut). The client never grants these in-game (the is_job_item guard treats
# them as counter-only); it tracks ownership via _ever_won and arms the boosts:
# Ninja/RedWizard/Master = client loops, WhiteWizard/BlackWizard = the on-disc
# job_scroll_boosts caves via the SCRL mailbox. Knight (from_job 0) = LIFESTEAL
# (2026-07-14): the on-disc Knight leg heals a Knight-class member 10% of the
# physical damage each of their attacks deals, and a second leg shaves 10% off the
# target's DEF per hit (also gates the class rename).
# Item name = the job's PROMOTED rename + " Scroll" (e.g. "Blood Knight Scroll"),
# single-sourced off class_names.CLASS_RENAME so the AP item name and the in-game
# class name can never drift apart. Renaming a class therefore renames its scroll
# -- a BREAKING change for existing seeds/yamls, which is intended (2026-07-14).
JOB_SCROLL_ITEM_NAMES = {}
for _fj, (_base, _promo) in CN.CLASS_RENAME.items():
    _sn = f"{_promo} Scroll"
    ITEM_NAME_TO_ID[_sn] = ID.job_item_id(_fj)
    # progression: a job scroll permanently boosts a class, so fill must keep
    # them reachable (and they may not be dumped into unreachable filler slots).
    ITEM_NAME_TO_CLASS[_sn] = ItemClassification.progression
    JOB_SCROLL_ITEM_NAMES[_fj] = _sn

# Spell Tomes (opt-out via SpellTomes, default on): one usable teach-item per
# spell, swapped in for filler. Game item = consumable cat 1, game id 44 +
# spell index (the relocated 108-row item table's tome rows -- see the
# spell-tome-items-re memory / iso_patcher.apply_spell_tomes), so the normal
# grant path and even own-item chests handle them natively. Spell index =
# vanilla spell identity (magic-shop shuffle permutes SHOPS, not spell slots),
# so the static AP name always matches what the tome teaches in-game.
from .spell_data import SPELL_NAMES     # single source; also used by the magic-shop tab
SPELL_TOME_FIRST_GID = 44
SPELL_TOME_ITEM_NAMES = []
for _s, _sp in enumerate(SPELL_NAMES):
    _tn = f"Spell Tome: {_sp}"
    ITEM_NAME_TO_ID[_tn] = ID.item_id(1, SPELL_TOME_FIRST_GID + _s)
    # NOT in FILLER_ITEM_NAMES regardless of class: tomes enter the pool only via
    # create_items' swap_in (at most ONE copy of each per world);
    # get_filler_item_name must never roll extra copies.
    # Spell level = the vanilla magic-store tier: 4 spells per level per color,
    # levels 1..8 (see rando._color_slot_tiers). Levels 6-8 are USEFUL, the rest
    # filler.
    ITEM_NAME_TO_CLASS[_tn] = (
        ItemClassification.useful if (_s % 32) // 4 >= 5
        else ItemClassification.filler
    )
    SPELL_TOME_ITEM_NAMES.append(_tn)

# Filler consumables: stat-boost / battle-utility items eligible as AP filler
# (shop AP-stock padding + get_filler_item_name). Registered here because most
# never appear in a vanilla chest, so ITEM_TABLE doesn't know them.
#
# The six shop AP-placeholder consumables (Eye Drops 13, Echo Grass 14, Spider's
# Silk 19, Red Curtain 24, Lunar Curtain 27, Cockatrice Claw 30) used to be held
# OUT of this table: a granted copy would land in the inventory and the old
# gil-drop watcher would read it as a shop purchase. Since v202 (BUYB purchase
# mailbox) purchases are attributed by STORE ID, so a granted copy is just an
# item and they are ordinary filler again. Residual, accepted: while that shop's
# offer is unsold, a granted copy reads under the AP offer's name inside that
# town (the name bank is per-item-id; it is held vanilla everywhere else). An AP
# offer that IS its own store's placeholder simply strips and re-grants the same
# item -- correct, if odd. See [[shop-buy-mailbox]].
FILLER_CONSUMABLES = {
    20: "White Fang",     21: "Red Fang",      22: "Blue Fang",
    29: "Vampire Fang",
    23: "Light Curtain",  25: "White Curtain", 26: "Blue Curtain",
    24: "Red Curtain",    27: "Lunar Curtain",
    36: "Golden Apple",   37: "Silver Apple",  38: "Soma Drop",
    39: "Power Plus",     40: "Stamina Plus",  41: "Mind Plus",
    42: "Speed Plus",     43: "Luck Plus",
    10: "Remedy",         15: "Emergency Exit",
    13: "Eye Drops",      14: "Echo Grass",
    19: "Spider's Silk",  30: "Cockatrice Claw",
    31: "Giant's Tonic",  32: "Faerie Tonic",  33: "Strength Tonic",
    34: "Protect Drink",  35: "Speed Drink",
}
for _gid, _name in FILLER_CONSUMABLES.items():
    ITEM_NAME_TO_ID.setdefault(_name, ID.item_id(1, _gid))
    ITEM_NAME_TO_CLASS.setdefault(_name, ItemClassification.filler)
FILLER_ITEM_NAMES = sorted(FILLER_CONSUMABLES.values())
# Filler whose whole effect is the MP pool -- dropped from the draw when
# slot_magic is on (see World._filler_names / rando._MANA_ITEM_BLOCK).
_MANA_FILLER_NAMES = frozenset({"Faerie Tonic"})

# EXP-bag filler: SYNTHETIC items (no native game id) that grant a fixed EXP amount
# to every party member on receipt (client grant_exp -> P_EXP write). The amount is
# encoded in the id (ID.exp_item_id). Added to FILLER_ITEM_NAMES so get_filler_item_name
# can roll them; NOT explicitly placed, so a seed may contain zero or several of any size.
EXP_BAG_AMOUNTS = {500: "Small", 2000: "Medium", 5000: "Large", 20000: "Colossal"}
EXP_BAG_ITEM_NAMES = []
for _amt, _sz in EXP_BAG_AMOUNTS.items():
    _bn = f"Experience Bag ({_sz})"
    ITEM_NAME_TO_ID.setdefault(_bn, ID.exp_item_id(_amt))
    ITEM_NAME_TO_CLASS.setdefault(_bn, ItemClassification.filler)
    EXP_BAG_ITEM_NAMES.append(_bn)
FILLER_ITEM_NAMES = FILLER_ITEM_NAMES + EXP_BAG_ITEM_NAMES


# (RETIRED 2026-07-27: ACTIVATABLE_ITEM_CODES, which existed only to let
# late_activatable_equipment forbid spell-on-use gear at pre-airship locations.
# That gate constrained OUR locations only, so multiworld fill still handed the
# gear out early through other games. equipment_runes replaces it by gating
# ACTIVATION in-game -- no item-placement rules needed, and no leak. The
# underlying id sets still live in rando._ACTIVATABLE_WEAPON_IDS / _ARMOR_IDS,
# which shop tiering and the power-price multipliers continue to use.)


# --- exotic/priceless loot pool: candidate gear (see options.*LootAmount) ---
# Canonical unit is (ap_cat, gid): ap_cat 2 = weapon (FF1DATA.WEAPONS), 3 = armor
# (FF1DATA.ARMOR). At generation a subset sized by the two 0-10 loot-amount options
# is drawn into the multiworld pool; the UNSELECTED remainder supplies the per-seed
# AP-shop placeholders (create_items / _gear_pool). When the candidate pool is fully
# drawn (both amounts high) placeholders fall back to LOW_*_GIDS.
#
# ONE hand-maintained membership list per game category (2026-07-31). Everything
# else about a candidate is DERIVED, so the loot pools can never drift out of step
# with the shop gradient:
#   exotic vs priceless      -> rando._shop_id_min_tier (options.ShopItemPool):
#                               SHOP_TIER_ALL -> priceless loot, EXOTIC or
#                               ACTIVATABLE -> exotic loot.
#   activatable subset       -> the record's use-cast field (+7), same source as
#                               rando._ACTIVATABLE_*_IDS.
#   guaranteed chest token   -> the name appears in DATA.ITEM_POOL.
#   promoted-only equip lock -> rando._PRICELESS_*_IDS (the same tier data).
# A candidate the gradient rates SHOP_TIER_OVERWORLD (ordinary town stock, e.g.
# Cat Claws gid 35) belongs in NEITHER pool and is rejected at import by the
# len() assert below.
_LOOT_WEAPON_GIDS = (
    29,                                             # Light Axe (spell-on-use)
    24, 30, 31, 32, 33,                             # Sun Blade..Wizard's Staff
    36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51,
    52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67,
)                                                   # 37
_LOOT_ARMOR_GIDS = (
    10, 14, 15, 16,                                 # Dragon Mail..Black Robe
    17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
    40, 41, 42, 43, 44, 45,                         # Genji/Crystal/Hero's shields
    51, 52,                                         # Healing Helm / Ribbon
    53, 54, 55, 56, 57, 58, 59, 60, 61, 62,
    67, 68,                                         # Gauntlets / Giant's Gloves
    71, 72, 73, 74, 75,
)                                                   # 43


def _gear_name(cat, gid):
    return FF1DATA.WEAPONS[gid] if cat == 2 else FF1DATA.ARMOR[gid]


def _act_band(draw, n_act, n_nonact, lo_pct, hi_pct):
    """(low, high) count of ACTIVATABLE candidates to take from a `draw`-sized
    draw, as a percentage band of the draw. Both ends round UP: a draw of 1+
    always yields >= 1 activatable, and a rounded-down floor can never land under
    the stated percentage. The floor is raised further when the non-activatable
    pool is too small to fill the draw; both ends are clamped to what exists."""
    if draw <= 0:
        return 0, 0
    forced = max(0, draw - n_nonact)               # non-act pool can't fill it
    lo = min(max(forced, -(-draw * lo_pct // 100)), n_act, draw)
    hi = min(max(lo, -(-draw * hi_pct // 100)), n_act, draw)
    return lo, hi


def _is_activatable(cat, gid):
    """True if this weapon/armor casts a free spell when USED as a battle item
    (record field +7 nonzero). Same derivation rando uses for _ACTIVATABLE_*_IDS,
    so the split can't drift from the table data."""
    rec = RD.VANILLA["weapons"] if cat == 2 else RD.VANILLA["armor"]
    return rec[(gid - 1) * 28 + 7] != 0


_VANILLA_SHOP_IDS = RANDO._vanilla_shop_ids(RD.VANILLA["shops"])


def _shop_tier_of(cat, gid):
    """The ShopItemPool tier at which this gear becomes buyable (rando gradient).
    ap_cat 2/3 -> rando cat 0/1."""
    return RANDO._shop_id_min_tier(cat - 2, gid, _VANILLA_SHOP_IDS)


# Candidates that enter the draw pool MORE THAN ONCE. Each copy is an independent
# draw slot, so a seed can hand out up to N of them and even a small draw has a
# fair chance at one -- used for gear the vanilla game shipped in quantity, which
# would otherwise become a one-per-seed rarity once it joins a loot pool.
# A drawn copy and an undrawn copy can coexist; _gear_pool filters the undrawn
# ones out of the placeholder candidates (see there).
_LOOT_COPIES = {
    (3, 52): 4,      # Ribbon (priceless) -- 4 vanilla chests
    (3, 44): 3,      # Elven Cloak (exotic) -- capped at 3 (2026-08-01)
}

LOOT_GEAR = [t for t in ([(2, g) for g in _LOOT_WEAPON_GIDS]
                         + [(3, g) for g in _LOOT_ARMOR_GIDS])
             for _c in range(_LOOT_COPIES.get(t, 1))]                    # 85
PRICELESS_GEAR = [t for t in LOOT_GEAR
                  if _shop_tier_of(*t) == RANDO.SHOP_TIER_ALL]           # 23
EXOTIC_GEAR = [t for t in LOOT_GEAR
               if _shop_tier_of(*t) in (RANDO.SHOP_TIER_EXOTIC,
                                        RANDO.SHOP_TIER_ACTIVATABLE)]    # 62
assert len(EXOTIC_GEAR) + len(PRICELESS_GEAR) == len(LOOT_GEAR), (
    "loot candidate rated SHOP_TIER_OVERWORLD (or never-sold): "
    + str([_gear_name(*t) for t in LOOT_GEAR
           if t not in EXOTIC_GEAR and t not in PRICELESS_GEAR]))

# Activatable (spell-on-use) subsets. _gear_pool draws them under their own rarity
# caps: exotic activatables scale with the draw (5%..25% of it), priceless
# activatables are held to <=1 unless the non-activatable priceless pool runs out.
EXOTIC_ACT_GEAR = [t for t in EXOTIC_GEAR if _is_activatable(*t)]           # 15
EXOTIC_NONACT_GEAR = [t for t in EXOTIC_GEAR if not _is_activatable(*t)]    # 44
PRICELESS_ACT_GEAR = [t for t in PRICELESS_GEAR if _is_activatable(*t)]     # 7
PRICELESS_NONACT_GEAR = [t for t in PRICELESS_GEAR if not _is_activatable(*t)]  # 13

# Candidates that ALSO hold vanilla chest tokens in DATA.ITEM_POOL. Those tokens
# are demoted to filler UNCONDITIONALLY (2026-07-31 decision): NO loot candidate is
# ever guaranteed. A candidate exists in a seed only if its pool drew it, so some
# seeds simply have no Excalibur / Masamune / Ribbon at all, and at loot amount 0
# the item is absent entirely. The demoted chest stays an ordinary randomized AP
# location -- only the item this world contributes to the shared pool changes (to a
# filler); multiworld fill still decides what actually sits in that chest.
# This also keeps the placeholder invariant simple: an UNSELECTED candidate is
# never in the item pool, so it is always safe as an AP-shop placeholder.
_CHEST_ITEM_NAMES = frozenset(nm for (_iid, nm, _cls) in DATA.ITEM_POOL)
_LOOT_CHEST_NAMES = frozenset(
    n for n in map(lambda t: _gear_name(*t), LOOT_GEAR) if n in _CHEST_ITEM_NAMES)

# 0-10 loot-amount option -> number of candidates drawn. index 0..10, LINEAR in
# tenths of the pool, so the ladders re-scale themselves when a candidate (or a
# _LOOT_COPIES copy) is added or removed -- 0 -> none, 10 -> the whole pool.
# Today: exotic 62 (default 7 -> 43), priceless 23 (default 2 -> 5).
_EXOTIC_COUNT = tuple(round(i * len(EXOTIC_GEAR) / 10) for i in range(11))
_PRICELESS_COUNT = tuple(round(i * len(PRICELESS_GEAR) / 10) for i in range(11))

# Low-tier fallback placeholders (buy-only vanilla stock, NOT grantable, NOT
# stealable) -- used only when the candidate pool is too drawn to supply the 6
# weapon + 6 armor AP-shop slots. Deliberately EXCLUDES grantable low gear
# (Dagger/Flame Shield/Leather Cap) so no pool collision, no demotion needed.
LOW_WEAPON_GEAR = [(2, g) for g in (1, 4, 5, 7, 8, 9)]     # Nunchaku..Iron Nunchaku
LOW_ARMOR_GEAR = [(3, g) for g in (2, 3, 5, 32, 47, 63)]   # Leather Armor..Leather Gloves
# (No _GUARANTEED_GEAR any more: nothing in LOOT_GEAR keeps a guaranteed copy, so
# every unselected candidate -- Excalibur included -- is a legal placeholder.)

# Every gear id the game HAS, in id order. The placeholder candidate pool is
# drawn from this minus the seed's item pool (see FF1PSPWorld._spare_gear):
# 67 weapons + 75 armor, of which the vanilla chest pool only ever holds 29 + 34,
# so ~38 + ~41 ids are safe placeholders in ANY seed. The old candidate list was
# "unselected loot + the six low-tier fallbacks above" -- 19-25 ids on a default
# seed and SIX at loot amount 10, which silently cost both AP offer rows and hint
# rows (measured 2026-08-11: 29/37 offers, 4/24 hints at loot 10/10).
ALL_WEAPON_GEAR = [(2, g) for g in sorted(FF1DATA.WEAPONS)]
ALL_ARMOR_GEAR = [(3, g) for g in sorted(FF1DATA.ARMOR)]


# Register every candidate as a grantable AP item name (id setdefault: Excalibur/
# Masamune/Ultima Weapon already live in DATA.ITEM_TABLE and keep their id).
# Classification is set OUTRIGHT (not setdefault) so it can't be shadowed by the
# blanket filler default the ITEM_TABLE loop gave those chest tokens:
#   priceless (promoted-only, SHOP_TIER_ALL) -> always USEFUL
#   activatable (spell-on-use) -> always USEFUL, exotic or priceless
#   exotic non-activatable -> filler
_PRICELESS_SET = frozenset(PRICELESS_GEAR)
for _cat, _gid in LOOT_GEAR:
    _nm = _gear_name(_cat, _gid)
    ITEM_NAME_TO_ID.setdefault(_nm, ID.item_id(_cat, _gid))
    ITEM_NAME_TO_CLASS[_nm] = (
        ItemClassification.useful
        if ((_cat, _gid) in _PRICELESS_SET or _is_activatable(_cat, _gid))
        else ItemClassification.filler
    )


# ---- name groups (datapackage) -------------------------------------------
# What they buy the player: `!hint Weapons`, `!hint Marsh Cave`, and yaml
# exclude_locations / priority_locations / plando by group name instead of by
# individual location name.
#
# DERIVED, never hand-listed: item groups come off the id space (ids.is_* /
# item_cat_gid) and location groups off the id space + the location naming
# scheme, so anything registered above joins its group automatically. The one
# hand-maintained table is _AREA_PREFIXES -- a location whose name matches no
# prefix silently lands in no area group, which test_name_groups.py fails on.
#
# Group membership IS part of the datapackage (AutoWorld.get_data_package_data
# folds item_name_groups / location_name_groups into the checksum), so editing
# this block means bumping archipelago.json "version".

def _group_add(groups, key, name):
    groups.setdefault(key, set()).add(name)


_SPELL_TOME_SET = frozenset(SPELL_TOME_ITEM_NAMES)


def _build_item_name_groups():
    g = {}
    for name, iid in ITEM_NAME_TO_ID.items():
        # Event/gate tokens and the Victory token are synthetic: they never enter
        # anyone's pool, so a hint or a plando naming them would be a dead end.
        if ID.is_event(iid) or ID.is_victory(iid):
            continue
        if ID.is_gil(iid):
            _group_add(g, "Gil", name)
        elif ID.is_exp(iid):
            _group_add(g, "Experience Bags", name)
        elif ID.is_job_item(iid):
            _group_add(g, "Job Scrolls", name)
        elif ID.is_tablet(iid) or ID.is_shard(iid) or ID.is_rune(iid):
            # Lute Tablet / Levistone Shard / Equipment Rune: N copies that
            # assemble one thing at a yaml-set threshold.
            _group_add(g, "Assembly Pieces", name)
            if ID.is_shard(iid):
                _group_add(g, "Vehicles", name)
        elif ID.is_vehicle(iid):
            # Ship: granted by a story-flag write rather than an inventory record,
            # but the player reasons about it exactly like a key item.
            _group_add(g, "Key Items", name)
            _group_add(g, "Vehicles", name)
        elif ID.is_item(iid):
            cat, _gid = ID.item_cat_gid(iid)
            if cat == 0:
                _group_add(g, "Key Items", name)
                if name in (LOGIC.CANOE, LOGIC.LEVISTONE):
                    _group_add(g, "Vehicles", name)
            elif cat == 1:
                if name not in _SPELL_TOME_SET:
                    _group_add(g, "Consumables", name)
            elif cat == 2:
                _group_add(g, "Weapons", name)
                _group_add(g, "Equipment", name)
            elif cat == 3:
                _group_add(g, "Armor", name)
                _group_add(g, "Equipment", name)
    # Spell tomes carry the school split the magic shops use: spell_data indexes
    # 0..31 are white, 32..63 black (SPELL_TOME_ITEM_NAMES is in that order).
    for _i, _tn in enumerate(SPELL_TOME_ITEM_NAMES):
        _group_add(g, "Spell Tomes", _tn)
        _group_add(g, "White Magic Tomes" if _i < 32 else "Black Magic Tomes", _tn)
    return {k: frozenset(v) for k, v in g.items()}


# Location-name prefix -> area group. Longest-first at lookup: "Castle Cornelia"
# must beat "Cornelia", and the per-floor/per-wing names ("Mount Gulg B4",
# "Marsh Cave B3", "Sunken Shrine Depths") deliberately collapse into the one
# dungeon a player would name in a hint. Ice Cave == Cavern of Ice (the Levistone
# pair uses the short name). Every registered location must match one prefix.
_AREA_PREFIXES = (
    ("Castle Cornelia", "Castle Cornelia"),
    ("Cavern of Earth", "Cavern of Earth"),
    ("Cavern of Ice", "Cavern of Ice"),
    ("Ice Cave", "Cavern of Ice"),
    ("Chaos Shrine", "Chaos Shrine"),
    ("Citadel of Trials", "Citadel of Trials"),
    ("Cornelia", "Cornelia"),
    ("Crescent Lake", "Crescent Lake"),
    ("Dragon Caves", "Dragon Caves"),
    ("Earthgift Shrine", "Earthgift Shrine"),
    ("Elfheim", "Elfheim"),
    ("Elven Castle", "Elven Castle"),
    ("Flying Fortress", "Flying Fortress"),
    ("Gaia", "Gaia"),
    ("Giant's Cavern", "Giant's Cavern"),
    ("Hellfire Chasm", "Hellfire Chasm"),
    ("Lefein", "Lefein"),
    ("Lifespring Grotto", "Lifespring Grotto"),
    ("Marsh Cave", "Marsh Cave"),
    ("Matoya's Cave", "Matoya's Cave"),
    ("Melmond", "Melmond"),
    ("Mirage Tower", "Mirage Tower"),
    ("Mount Duergar", "Mount Duergar"),
    ("Mount Gulg", "Mount Gulg"),
    ("Onrac", "Onrac"),
    ("Pravoka", "Pravoka"),
    ("Sage's Cave", "Sage's Cave"),
    ("Sunken Shrine", "Sunken Shrine"),
    ("Waterfall Cavern", "Waterfall Cavern"),
    ("Western Keep", "Western Keep"),
    ("Whisperwind Cove", "Whisperwind Cove"),
)
_AREA_PREFIXES_BY_LEN = tuple(sorted(_AREA_PREFIXES, key=lambda p: -len(p[0])))

# The four Soul of Chaos dungeons, by the display name their locations carry.
_BONUS_DUNGEON_NAMES = tuple(_d[1] for _d in LOGIC.BONUS_DUNGEONS)


def _build_location_name_groups():
    g = {}
    for name, lid in LOCATION_NAME_TO_ID.items():
        # Kind, straight off the id space -- the same split the client uses to
        # decide how a check is detected (purchase / dynamic counter / story
        # flag / static chest bit).
        if ID.is_shop_loc(lid):
            _group_add(g, "Shops", name)
        elif ID.is_dyn_chest(lid):
            _group_add(g, "Chests", name)
            _group_add(g, "Bonus Dungeon Chests", name)
        elif ID.BASE + ID.NPC_OFF <= lid < ID.BASE + ID.NPC_OFF + 0x100:
            _group_add(g, "NPCs", name)
        else:
            _group_add(g, "Chests", name)
        for prefix, area in _AREA_PREFIXES_BY_LEN:
            if name.startswith(prefix):
                _group_add(g, area, name)
                break
        if name.startswith(_BONUS_DUNGEON_NAMES):
            _group_add(g, "Bonus Dungeons", name)
    return {k: frozenset(v) for k, v in g.items()}


ITEM_NAME_GROUPS = _build_item_name_groups()
LOCATION_NAME_GROUPS = _build_location_name_groups()


class FF1PSPItem(Item):
    game = GAME


class FF1PSPLocation(Location):
    game = GAME


class FF1PSPWeb(WebWorld):
    icon = "worlds/ff1psp/ff1psp_icon.png"
    tutorials = []
    if OptionGroup is not None:
        # SINGLE source of truth: options.ff1psp_option_groups. Do NOT re-declare
        # groups here -- a second hardcoded list silently wins over that one, and
        # every option missing from it drops into AP's default "Game Options"
        # bucket (which is what happened before 2026-07-29).
        from .options import ff1psp_option_groups
        option_groups = ff1psp_option_groups


class FF1PSPWorld(World):
    """Final Fantasy 1 PSP (20th Anniversary) item randomizer with access logic:
    chest, NPC, shop and bonus-dungeon checks, played live through PPSSPP via the
    bundled client (which also bakes the on-disc feature patches)."""
    game = GAME
    web = FF1PSPWeb()
    options_dataclass = FF1PSPOptions
    options: FF1PSPOptions

    item_name_to_id = ITEM_NAME_TO_ID
    location_name_to_id = LOCATION_NAME_TO_ID
    item_name_groups = ITEM_NAME_GROUPS
    location_name_groups = LOCATION_NAME_GROUPS

    def create_item(self, name: str) -> FF1PSPItem:
        return FF1PSPItem(name, ITEM_NAME_TO_CLASS[name], ITEM_NAME_TO_ID[name], self.player)

    def _dyn_caps(self):
        """Resolved per-dungeon dynamic-chest AP-location counts: {dungeon_idx: cap}.
        Each cap is the dungeon's yaml Range value verbatim (the option's own range_end
        is the ceiling), or 0 when exclude_bonus_dungeons is set. The player picks exactly
        how many dynamic AP locations that dungeon gets -- N chosen => N locations, no
        padding. Single source for create_regions / create_items / fill_slot_data so all
        three agree. Any N is reachable: bonus dungeons re-enter without limit and the
        client counts the first N procedural chests opened CUMULATIVELY across descents
        (next_ordinal rides AP sent_locations), so a cap larger than one clear's chest
        count just means more runs -- never a stranded/uncompletable check."""
        if self.options.exclude_bonus_dungeons.value:
            return {dg: 0 for dg, *_ in LOGIC.BONUS_DUNGEONS}
        return {dg: getattr(self.options, attr).value
                for dg, _name, _floors, _def, _tok, attr in LOGIC.BONUS_DUNGEONS}

    def _removed_chest_idx(self):
        """Treasure indices that are NOT AP chest locations this seed. ALWAYS the
        phantom set (event pickups with no chest bit); PLUS each static DLC boss
        chest (idx 252-255) whose dungeon contributes zero dynamic AP chests -- that
        dungeon's *_ap_locations is 0 (individually, or all four under
        exclude_bonus_dungeons). Those chests only physically exist inside the DLC
        bonus dungeons, so if the player is told to skip a dungeon they can never
        open its chest and the location would strand the check count below 100%.
        Single source of truth shared by create_regions / create_items /
        fill_slot_data / the client scout so all four agree (test_scout_parity
        enforces the last one). See logic.removed_static_dlc_idx."""
        removed = set(LOGIC.PHANTOM_TREASURE_INDICES)
        removed |= LOGIC.removed_static_dlc_idx(
            self._dyn_caps(), bool(self.options.exclude_bonus_dungeons.value))
        # loot_in_normally_empty_chests off: the ten alias-duplicate chests keep
        # the vanilla treasure index they SHARE with a neighbour (iso_patcher
        # skips their dedup records), so they can never open on their own bit --
        # drop the locations rather than strand ten checks the player cannot make.
        removed |= LOGIC.removed_normally_empty_idx(
            self.options.loot_in_normally_empty_chests.value)
        return removed

    def get_filler_item_name(self) -> str:
        return self.random.choice(self._filler_names())

    def _filler_names(self):
        """FILLER_ITEM_NAMES minus anything whose only effect is the mana pool.
        slot_magic replaces MP with per-level spell slots, so a Faerie Tonic
        would be a filler item that does literally nothing on use (user
        2026-07-31). The Soma Drop stays: slot_magic gives it a new job, raising
        the level-1 slot count and spilling upward when that level is full."""
        if not self.options.slot_magic.value:
            return FILLER_ITEM_NAMES
        cache = getattr(self, "_filler_names_cache", None)
        if cache is None:
            cache = [n for n in FILLER_ITEM_NAMES if n not in _MANA_FILLER_NAMES]
            self._filler_names_cache = cache
        return cache

    @staticmethod
    def _resolve_piece_gate(required, percentage, extra):
        """A Link to the Past's Triforce-Pieces convention: REQUIRED is the anchor,
        and the number of pieces placed in the multiworld (the "available" pool) is
        DERIVED from it and clamped UP so it can never fall below required -- an
        unwinnable "need N, only M<N exist" config is unrepresentable.

        pool = round(required x percentage/100) + extra, so the two spare-piece
        knobs stack: percentage gives proportional slack, extra a flat amount on
        top (percentage 100 + extra 0 = exactly required). required<=0 turns the
        feature off. Returns (available count, required)."""
        required = int(required)
        if required <= 0:
            return 0, 0
        avail = round(required * int(percentage) / 100) + int(extra)
        return max(avail, required), required

    def _lute_tablets(self):
        """Resolved (tablet count, tablets required). (0, 0) = option off (the Lute
        is a normal pool item). Single source for create_regions / create_items /
        set_rules / fill_slot_data."""
        o = self.options
        return self._resolve_piece_gate(
            o.lute_tablets_required.value, o.lute_tablets_percentage.value,
            o.lute_tablets_extra.value)

    def _equipment_runes(self):
        """Resolved (rune count, runes required). (0, 0) = gate off (equipment
        activates vanilla-style). Single source for create_items / fill_slot_data."""
        o = self.options
        return self._resolve_piece_gate(
            o.equipment_runes_required.value, o.equipment_runes_percentage.value,
            o.equipment_runes_extra.value)

    def _levistone_shards(self):
        """Resolved (shard count, shards required). (0, 0) = option off (the
        Levistone is a normal pool item). Single source for create_regions /
        create_items / set_rules / fill_slot_data."""
        o = self.options
        return self._resolve_piece_gate(
            o.levistone_shards_required.value, o.levistone_shards_percentage.value,
            o.levistone_shards_extra.value)

    def _resolved_party_jobs(self):
        """The 4 starting-party jobs resolved ONCE (job id 0..5, or None =
        choose at game start -- the client leaves that slot alone).
        `random_job` picks roll here via self.random, so this consumes RNG -- we cache
        it so create_items and fill_slot_data see the SAME result and RNG is rolled
        exactly once (create_items runs first and fills the cache)."""
        if getattr(self, "_party_jobs_cache", None) is None:
            self._party_jobs_cache = resolve_party_jobs([
                self.options.starting_job_1.value,
                self.options.starting_job_2.value,
                self.options.starting_job_3.value,
                self.options.starting_job_4.value,
            ], self.random,
                diversity=bool(self.options.party_diversity.value),
                magics=bool(self.options.white_and_black_magics.value),
                dabble=bool(self.options.monk_thief_dabble_in_magic.value))
        return self._party_jobs_cache

    def _gear_pool(self):
        """Exotic/priceless loot draw (cached). Returns (selected_names,
        placeholder_pool_w, placeholder_pool_a): selected_names = gear item names
        create_items swaps into the pool (empty at loot amounts 0/0); the two pools
        are preference-ordered (cat, gid) candidate lists for the weapon/armor
        AP-shop placeholder rows -- UNSELECTED candidates (shuffled) first, low-tier
        vanilla gear last. The actual placeholder PICK happens post-shuffle in
        rando.pick_seed_placeholders (build_shuffle_tables), which takes the first
        candidates the shuffled shops stock nowhere. An unselected candidate is
        never a pool item, so any of them is grant-safe. Counts come from the two
        0-10 loot-amount options via _EXOTIC_COUNT / _PRICELESS_COUNT. Consumes
        self.random unconditionally (amounts 0/0 draw nothing but still shuffle the
        candidate order); MUST run before the rest of _seed_shuffle so the RNG
        order is stable across callers."""
        cache = getattr(self, "_gear_pool_cache", None)
        if cache is not None:
            return cache
        r = self.random
        n_ex = _EXOTIC_COUNT[self.options.exotic_loot_amount.value]
        n_pr = _PRICELESS_COUNT[self.options.priceless_loot_amount.value]
        # Activatable (spell-on-use) gear is drawn under a percentage band of the
        # draw size, per pool. BOTH bounds round UP, so a small draw still clears
        # its own floor (floor-rounding gave 4.4% at the 5% floor once) and a draw
        # of 1+ always contains at least one activatable. The floor is forced
        # higher only when the non-activatable pool can't fill the draw.
        #   exotic:    5%..25% of the draw (natural share of the pool is 25%)
        #   priceless: 10%..30% of the draw (natural share is 30%)
        ex_lo_act, ex_hi_act = _act_band(n_ex, len(EXOTIC_ACT_GEAR),
                                         len(EXOTIC_NONACT_GEAR), 5, 25)
        ex_non = list(EXOTIC_NONACT_GEAR); ex_act = list(EXOTIC_ACT_GEAR)
        r.shuffle(ex_non); r.shuffle(ex_act)
        n_ex_act = r.randint(ex_lo_act, ex_hi_act) if n_ex else 0
        sel = ex_act[:n_ex_act] + ex_non[:n_ex - n_ex_act]
        unsel = ex_act[n_ex_act:] + ex_non[n_ex - n_ex_act:]
        lo_act, hi_act = _act_band(n_pr, len(PRICELESS_ACT_GEAR),
                                   len(PRICELESS_NONACT_GEAR), 10, 30)
        nonact = list(PRICELESS_NONACT_GEAR); act = list(PRICELESS_ACT_GEAR)
        r.shuffle(nonact); r.shuffle(act)
        n_act = r.randint(lo_act, hi_act) if n_pr else 0
        sel += act[:n_act] + nonact[:n_pr - n_act]
        unsel += act[n_act:] + nonact[n_pr - n_act:]
        # Placeholder candidate pools: UNSELECTED candidates first (nothing in
        # LOOT_GEAR is guaranteed any more, so any of them is legal), low-tier
        # vanilla gear as the fully-drawn fallback. MULTI-COPY candidates
        # (_LOOT_COPIES, e.g. 4x Ribbon) can be selected AND unselected in the
        # same seed: filter the unselected list against the SELECTED SET and
        # dedupe, or a placeholder could name an item that IS in the pool -- the
        # collision the placeholder invariant forbids.
        _drawn = set(sel)
        _seen = set()
        unsel = [t for t in unsel
                 if t not in _drawn and not (t in _seen or _seen.add(t))]
        unsel_w = [t for t in unsel if t[0] == 2]
        unsel_a = [t for t in unsel if t[0] == 3]
        r.shuffle(unsel_w); r.shuffle(unsel_a)
        names = [_gear_name(c, g) for (c, g) in sel]
        # ...then EVERY other gear id this seed's item pool cannot hand out. The
        # unselected-loot list alone runs to six ids at loot amount 10, and a dry
        # pool costs real rows (offers AND hints). Spares are drawn last, in a
        # shuffled order, so a seed that never needed them lays out as before
        # apart from the fallback tail.
        spare_w, spare_a = self._spare_gear(set(sel))
        _already = set(unsel_w) | set(unsel_a)
        spare_w = [t for t in spare_w if t not in _already]
        spare_a = [t for t in spare_a if t not in _already]
        r.shuffle(spare_w); r.shuffle(spare_a)
        cache = (names,
                 unsel_w + spare_w + LOW_WEAPON_GEAR,
                 unsel_a + spare_a + LOW_ARMOR_GEAR)
        self._gear_pool_cache = cache
        return cache

    def _spare_gear(self, selected):
        """([(2, gid), ...], [(3, gid), ...]) of gear ids this seed's ITEM POOL
        can never hand out -- the safe placeholder supply.

        The placeholder invariant is exactly "not an item somebody can receive"
        ([[placeholder-name-collision]]): while a row is unsold its authored name
        and price follow that item id everywhere, so a copy in the pool would
        arrive wearing the shop row's identity. The pool is knowable here without
        running the fill: it is DATA.ITEM_POOL minus the chests create_regions
        drops, minus the loot-gear tokens create_items demotes to filler
        UNCONDITIONALLY, plus the loot draw's selected gear.

        Already excludes the six LOW_*_GEAR fallbacks when they are pool items;
        those stay pinned at the end of the candidate list as a last resort.

        Also drops RANDO.NEVER_PLACEHOLDER (starting equipment + the delist
        fillers). Those ids are not in the item pool -- the player is handed them
        at new game instead -- so the pool test alone read them as free supply and
        the seed sold the party's own Knife identity to a hint row. The picker
        bans them too; this keeps the SUPPLY COUNT honest for the capacity math."""
        drop = self._removed_chest_idx() - set(LOGIC.PHANTOM_TREASURE_INDICES)
        pool_names = {name for (_lid, _nm, idx), (_iid, name, _cls)
                      in zip(DATA.LOCATIONS, DATA.ITEM_POOL) if idx not in drop}
        pool_names -= set(_LOOT_CHEST_NAMES)          # demoted to filler
        pool_names |= {_gear_name(c, g) for (c, g) in selected}
        out = ([], [])
        for i, allgear in enumerate((ALL_WEAPON_GEAR, ALL_ARMOR_GEAR)):
            for cat, gid in allgear:
                if ((cat, gid) not in RANDO.NEVER_PLACEHOLDER
                        and _gear_name(cat, gid) not in pool_names):
                    out[i].append((cat, gid))
        return out

    def _seed_shuffle(self):
        """Roll the shop AP-offer prices + Tier-A data-table shuffles ONCE and
        cache the result, so self.random is consumed a single time however many
        callers need it. create_items (the spell-tome learnability filter) and
        fill_slot_data (the shipped patch bytes) both read this; create_items runs
        first and fills the cache -- exactly like _resolved_party_jobs. Returns
        (shop_offer_prices, shop_ap_prices, shuffle_tables, placeholders) --
        placeholders = the post-shuffle per-seed {ordinal: (cat, gid)} AP-shop
        placeholder map from rando.pick_seed_placeholders (None when
        shop_ap_offers is 0).

        Shop AP stock: every offer rolls its OWN base, log-uniform over 300..5000
        gil (item quality is still irrelevant -- an AP item has no vanilla price);
        if Randomize Prices is on that base is then scaled through the same item
        price range as every other price. Both rolls are log-uniform and
        seed-reproducible, and they STACK, so AP prices span a far wider band than
        ordinary stock (clamped to [5, 99999] like anything else). Only offer 0's
        price is BAKED (shop_ap_prices); the client rewrites the placeholder
        between sales."""
        if getattr(self, "_seed_shuffle_cache", None) is None:
            # Roll the exotic/priceless loot pool FIRST (fixed RNG order): it
            # consumes self.random before the price/table shuffles below. Only
            # the selected NAMES matter since v2 -- the candidate pools are no
            # longer placeholder supply (constants are), but the draw still
            # runs in full so the RNG stream is stable.
            self._gear_pool()
            counts = self._shop_offer_counts()
            hint_products = self._hint_products()
            hint_caps = self._hint_shop_caps() if hint_products else {}
            shop_ap_prices = None       # feature switch for build_shuffle_tables
            shop_offer_prices = {}
            if counts:
                bounds = RANDO._norm_range(
                    (self.options.item_price_range_low.value / 100.0,
                     self.options.item_price_range_high.value / 100.0))
                shop_ap_prices = {}
                for nm, shop, _reqs in LOGIC.SHOP_LOCATIONS:
                    prices = []
                    for k in range(counts.get(shop, 0)):
                        # Per-offer base, log-uniform 300..5000 (see
                        # RANDO.rand_ap_base_price). Always rolled -- item
                        # quality still doesn't matter, but no two offers
                        # share a base any more.
                        gil = RANDO.rand_ap_base_price(self.random)
                        if self.options.randomize_prices.value:
                            gil = RANDO._rand_price(self.random, gil,
                                                    RANDO._PRICE_CAP_ITEM, bounds)
                        prices.append(gil)
                    shop_offer_prices[shop] = prices
                    # EVERY row's price is baked -- a price lives on the item
                    # record, not the shop row, so parallel rows need parallel
                    # ids and the client never reprices anything at runtime.
                    shop_ap_prices[shop] = prices
            shuffle_tables = RANDO.build_shuffle_tables(
                self.random,
                shop_tier=int(self.options.shop_item_pool.value),
                # Extra NORMAL stock rows per store (yaml shop_max_extra_items).
                # A per-store roll of 0..this, on top of the AP/hint reserve.
                shop_extra_slots=int(self.options.shop_max_extra_items.value),
                magic_shops=bool(self.options.shuffle_magic_shops.value),
                item_prices=bool(self.options.randomize_prices.value),
                # slot_magic replaces the MP pool with per-level spell slots --
                # a shuffled MP cost has nothing to act on (every cast costs 1
                # charge of its level), so the mana-cost shuffle is DEFERRED
                # (silently off) while slot_magic is on. Other MP-adjacent
                # features (dabble MP scaling, CW mana refund) are evaluated
                # case-by-case; see options.SlotMagic.
                spell_mp=bool(self.options.randomize_spell_mana_costs.value
                              and not self.options.slot_magic.value),
                equip_perms=bool(self.options.shuffle_who_equips_what.value),
                overworld_harder=bool(self.options.harder_overworld_encounters.value),
                dungeon_harder=bool(self.options.harder_dungeon_encounters.value),
                item_price_range=(self.options.item_price_range_low.value / 100.0,
                                  self.options.item_price_range_high.value / 100.0),
                spell_price_range=(self.options.spell_gil_price_range_low.value / 100.0,
                                   self.options.spell_gil_price_range_high.value / 100.0),
                spell_mp_range=(self.options.spell_mana_cost_range_low.value / 100.0,
                                self.options.spell_mana_cost_range_high.value / 100.0),
                # Spell-name list (empty = feature off). Drives the MP-cost floor
                # in MP mode and the spell-LEVEL push under slot_magic.
                costly_best_spells=list(self.options.costly_best_spells.value),
                slot_magic=bool(self.options.slot_magic.value),
                shop_ap_prices=shop_ap_prices,
                shop_ap_offers=counts or None,
                # Hint rows: gear-shop tail rows that reveal a tracker tile,
                # priced by how many locations the tile holds. A QUEUE plus a
                # per-store cap, not a per-store list -- the allocator seats
                # each row in a store whose category still has placeholder ids,
                # so a dry armor pool moves rows to the weapon shops instead of
                # dropping them (see rando.pick_seed_placeholders).
                shop_hint_prices=[p for _lbl, p, _lids in hint_products] or None,
                shop_hint_caps=hint_caps or None,
                # Shared placeholder constants (v2): rows in different stores
                # share gids and the client authors identity per town, so the
                # supply is structural and no per-seed pick runs. On for a
                # hints-only seed too -- hint rows ride the same constants.
                shared_tails=(shop_ap_prices is not None or bool(hint_products)),
                # Promoted-only equip lock on the priceless pool. Applies wherever
                # priceless gear can show up -- as AP loot OR as shop stock at
                # SHOP_TIER_ALL -- and is skipped entirely when the option is off
                # (gear then keeps vanilla or equip-shuffled perms).
                restrict_priceless_equip=bool(
                    self.options.only_advanced_jobs_equip_priceless_gear.value
                    and (self.options.priceless_loot_amount.value
                         or int(self.options.shop_item_pool.value)
                         >= RANDO.SHOP_TIER_ALL)),
                # Faerie Tonic restores the MP pool slot_magic makes inert --
                # keep it out of every shop/caravan draw for such a seed.
                block_mana_items=bool(self.options.slot_magic.value),
            )
            placeholders = shuffle_tables.pop("shop_ap_placeholders", None)
            # Each store's NORMAL stock width, i.e. everything before its AP
            # tail. The client re-renders the tail from it after every sale, and
            # only the shuffle knows it (the reserve can be trimmed by a dry
            # pool or the 15-row ceiling).
            base_widths = shuffle_tables.pop("shop_ap_base_widths", None)
            # The hint half of each tail: {ordinal: [(cat, gid), ...]}, in the
            # same shelf order as hint_plan's rows. Paired with the plan below so
            # fill_slot_data can ship gid + price + product together.
            hint_ph = shuffle_tables.pop("shop_hint_placeholders", None)
            self._seed_shuffle_cache = (shop_offer_prices, shop_ap_prices,
                                        shuffle_tables, placeholders,
                                        base_widths, hint_ph, hint_products)
        return self._seed_shuffle_cache

    def _shop_offer_counts(self) -> dict:
        """{shop index: how many AP offers that store lists in parallel}, or {}
        when the feature is off. Memoised.

        The per-shop variety comes from a Random seeded off this slot's own seed
        (same trick as the boss-minion roll below), so it is reproducible from
        the seed yet invisible to the main stream.

        Supply is STRUCTURAL (rando.RESERVED_SHOP_PLACEHOLDERS, shared across
        stores since v2): gear stores always seat the full request, item stores
        clamp to rando.ITEM_SHOP_MAX_OFFERS. Every count registered here is a
        row the shelf will actually carry -- the per-seed supply arithmetic
        that could strand a check ("Gaia Armor Shop: AP Stock 2", 2026-08-15)
        is gone with the per-seed placeholder draw."""
        if getattr(self, "_shop_counts_cache", None) is None:
            import random as _random
            # ShopApOffers IS the feature switch now (0 = no AP shop stock at
            # all, so no shop is a check); the old add_ap_item_to_shops toggle
            # is gone. shop_offer_counts itself returns {} at 0, but the branch
            # stays explicit because create_regions reads this to decide whether
            # to register shop locations.
            if not self.options.shop_ap_offers.value:
                self._shop_counts_cache = {}
            else:
                self._shop_counts_cache = RANDO.shop_offer_counts(
                    self.options.shop_ap_offers.value,
                    int(self.options.shop_item_pool.value),
                    block_mana_items=bool(self.options.slot_magic.value),
                    # getattr: the parity harnesses drive create_regions with a
                    # stub multiworld that has no seed. No seed -> no variance,
                    # every shop takes the full count, which is exactly what a
                    # parity check wants to compare against.
                    rng=(_random.Random(
                        f"{getattr(self.multiworld, 'seed', None)}"
                        f"-{self.player}-shopoffers")
                        if getattr(self.multiworld, "seed", None) is not None
                        else None))
        return self._shop_counts_cache

    def _reserved_gear_used(self) -> dict:
        """{store row: reserved-constant entries this seed can touch} -- the
        rando.reserved_used_prefix of the rolled counts + hint caps. Drives the
        two chest-pool demotions (Leather Cap / Bronze Gloves sit LAST in the
        armor constants so only a 6-7-row store reaches them) and ships to the
        client for its mask/name sets. Deterministic from options + the count
        roll; no RNG."""
        return RANDO.reserved_used_prefix(self._shop_offer_counts(),
                                          self._hint_shop_caps())

    def _hint_shop_caps(self) -> dict:
        """{gear shop index: most hint rows that store may carry}, or {} when
        Max Hints Per Gear Shop is 0. This is the shelf-width RESERVE; how many rows
        a store actually gets is settled later against the placeholder ids its
        category still has (rando.hint_shop_caps / pick_seed_placeholders).
        Options + vanilla bytes only -- never self.random (see
        _shop_offer_counts)."""
        if getattr(self, "_hint_caps_cache", None) is None:
            self._hint_caps_cache = RANDO.hint_shop_caps(
                int(self.options.shop_max_hints.value),
                int(self.options.shop_item_pool.value),
                ap_counts=self._shop_offer_counts())
        return self._hint_caps_cache

    def _hint_products(self) -> list:
        """[(product label, price, [location ids]), ...] -- the seed's hint
        products in shelf order, a world-level budget rather than a per-shop
        one (the yaml only says yes or no). Memoised.

        The draw is uniform over the tiles this seed's pool actually fills and
        takes each tile AT MOST ONCE, so no two rows ever reveal the same place
        (hints.plan_hint_products). Runs off a Random seeded from the slot seed,
        NOT self.random: this is rolled inside _seed_shuffle, which create_items
        and fill_slot_data share, and a draw there would reorder every other
        shuffle.

        Asks for a few more products than HINT_TARGET_ROWS: the allocator can
        seat more rows than the target in a lucky seed only up to the target, so
        the surplus is really a cushion for products that turn out to be empty.
        Needs the seed's locations, so it can only run once create_regions has
        built them; the parity harnesses drive a stub multiworld with no
        location list and get no hints, which is what a parity check wants."""
        if getattr(self, "_hint_products_cache", None) is None:
            import random as _random
            products = []
            if self._hint_shop_caps():
                try:
                    lids = [loc.address for loc
                            in self.multiworld.get_locations(self.player)
                            if loc.address is not None]
                except Exception:
                    lids = []
                if lids:
                    rng = _random.Random(
                        f"{getattr(self.multiworld, 'seed', None)}"
                        f"-{self.player}-hintshops")
                    products = HINTS.plan_hint_products(
                        rng, lids, RANDO.hint_target_rows(
                            self.options.shop_max_hints.value))
                    # Randomize Prices covers hint rows too: each price is
                    # scaled through the SAME log-uniform item price range as
                    # every other shelf price, so a seed that asks for cheap (or
                    # brutal) shops gets cheap (or brutal) hints. Rolled off the
                    # hint Random, never self.random -- _seed_shuffle's draw
                    # order must not move (see _shop_offer_counts).
                    if self.options.randomize_prices.value:
                        bounds = RANDO._norm_range(
                            (self.options.item_price_range_low.value / 100.0,
                             self.options.item_price_range_high.value / 100.0))
                        products = [
                            (label,
                             HINTS.round_price(RANDO._rand_price(
                                 rng, price, RANDO._PRICE_CAP_ITEM, bounds)),
                             plids)
                            for label, price, plids in products]
            self._hint_products_cache = products
        return self._hint_products_cache

    def _final_magic_learn(self) -> bytes:
        """The magic_learn bitfield EXACTLY as the client bakes it into the ISO:
        vanilla, then the magic-shop rebuild (only if shuffle_magic_shops -- see
        rando.build_shuffle_tables), then the Monk/Thief/Master dabble learn-overlay
        (only if enabled -- iso_patcher.apply_monkthief_magic ORs it on top of the
        shuffled table). 64 spells x u16. Read a class's access with the game's own
        can_learn bit = (job % 6) + (job // 6) * 8 (see _learnable_tome_names)."""
        _, _, tables, _, _, _, _ = self._seed_shuffle()
        learn = bytearray(tables.get("magic_learn", RD.VANILLA["magic_learn"]))
        if self.options.monk_thief_dabble_in_magic.value:
            from .client import iso_patcher as IP
            shops = tables.get("shops", RD.VANILLA["shops"])
            IP.apply_dabble_learn_overlay(learn, shops)
        return bytes(learn)

    def _learnable_tome_names(self):
        """Spell-Tome item names to add to the pool: only tomes whose spell at
        least one EVENTUAL party class can learn. The game gates tome use on
        can_learn(job, spell) AND magiclv >= the spell's (shuffle-aligned) level
        (iso_patcher._tome_validity_handler). The magiclv gate is provably
        redundant with the learn bit here: the shop shuffle realigns every spell to
        its store's tier and rebuilds per-store learn COUNTS, so a set learn bit
        always implies the class's magiclv reaches that spell's final level (true
        for the rebuilt casters + Ninja, for Knight's vanilla white 1-3, and for the
        low-level Monk/Thief/Master dabble sets). So a single learn-bit test on the
        FINAL magic_learn is exact.

        Eventual class = base OR its promoted form (base + 6): promotion is always
        reachable (the Rat's Tail is an AP item, in the pool under Full
        accessibility, and Bahamut promotes the whole party natively). A promoted
        class's learn set is a superset of its base's, so including both never
        under-counts. A `vanilla` (char-creation) party slot is unknown at gen ->
        be permissive, include every tome (some class could still learn any spell)."""
        base_jobs = self._resolved_party_jobs()   # 4x (job id 0..5 | None=vanilla)
        if any(j is None for j in base_jobs):
            return list(SPELL_TOME_ITEM_NAMES)
        classes = {j for j in base_jobs} | {j + 6 for j in base_jobs}
        learnable = RANDO.learnable_spells(self._final_magic_learn(), classes)
        return [SPELL_TOME_ITEM_NAMES[s] for s in sorted(learnable)]

    def _party_scroll_names(self):
        """Job-Scroll item names to add to the pool: only scrolls for base jobs the
        starting party will actually have. Each scroll (keyed by BASE job 0..5,
        JOB_SCROLL_ITEM_NAMES) boosts a specific class, so a scroll for a job no
        party member holds is dead weight. If any member is `vanilla` (job chosen at
        char-creation, unknown at gen) we can't know -> add all 6. Mirrors the tome
        learnability filter (_learnable_tome_names)."""
        base_jobs = self._resolved_party_jobs()   # 4x (job id 0..5 | None=vanilla)
        if any(j is None for j in base_jobs):
            return list(JOB_SCROLL_ITEM_NAMES.values())
        return [JOB_SCROLL_ITEM_NAMES[j] for j in sorted(set(base_jobs))]

    def fill_slot_data(self) -> dict:
        # Baked multipliers the runtime client applies (enc/xp/gil_mult in
        # ff1psp/client/ApClient.py -> boot_patch data tables, re-applied live by
        # set_encounter_rate / set_monster_scaling).
        # Random starting party: 4 entries (job id 0..5, or None = choose at
        # game start, i.e. leave the slot as character creation made it).
        # Resolved+cached (shared with create_items). The client writes these at new
        # game (class byte + level-1 stat block). See [[class-byte]].
        party_jobs = self._resolved_party_jobs()
        # Shop AP-offer prices + the Tier-A data-table shuffles are rolled ONCE in
        # _seed_shuffle (shared with create_items' spell-tome learnability filter, so
        # self.random is consumed a single time and both see the same tables). The
        # patched table bytes ship base64 for the client to boot-patch. See rando.py.
        (shop_offer_prices, shop_ap_prices, shuffle_tables,
         seed_placeholders, shop_base_widths,
         hint_placeholders, hint_products) = self._seed_shuffle()
        # On-disc (Route-2 code-patch) features: the client's launcher bakes these
        # into a patched copy of the player's ISO before boot (see iso_patcher).
        # ON_DISC_ALWAYS features (shop_spell_level) are unconditionally on.
        from .options import ON_DISC_OPTIONS, ON_DISC_ALWAYS, ON_DISC_SLOT_KEY
        # Resolve through the option class's internal_name: a feature key may
        # ride an option named differently (super_dash rode auto_dash until
        # v268, when it became ON_DISC_ALWAYS).
        on_disc = {name: bool(getattr(self.options,
                                      ON_DISC_OPTIONS[name].internal_name).value)
                   for name in ON_DISC_OPTIONS}
        on_disc.update({name: True for name in ON_DISC_ALWAYS})
        # overworld_u16 has no yaml toggle: it is the code half of the harder
        # overworld bands (u16 DLC formations in zones_overworld). It MUST be
        # baked exactly when rando ships the zones_overworld_hi companion table --
        # a baked cave without the companion reads vanilla terrain-3 bytes as high
        # bytes and yields garbage formation ids. Both key off this one flag.
        on_disc["overworld_u16"] = bool(self.options.harder_overworld_encounters.value)
        # Desert (terrain 3) + marsh/river (terrain 1) per-tier pools. Neither
        # terrain reads zones_overworld, so the harder bands never touched them;
        # both are authored cave rows keyed off the same flag. No yaml toggle.
        on_disc["terrain_pools"] = bool(self.options.harder_overworld_encounters.value)
        # Chaos Shrine basement: per-floor u16 pools + boss cameos. No yaml toggle
        # of its own -- it is the code half of harder DUNGEON encounters, which
        # cannot otherwise touch the terminal dungeon (its _CAVE_HARDER_DUNGEON
        # chain self-maps). Exactly parallel to overworld_u16 above.
        #
        # MUST live in on_disc: _build_bake() takes its feature dict from
        # slot_data["on_disc"] alone, so a code feature published as a top-level
        # slot_data key is silently never baked (live lesson 2026-08-04 -- both
        # chaos keys shipped top-level and no seed ever got them).
        on_disc["chaos_floor_pools"] = bool(
            self.options.harder_dungeon_encounters.value)
        # chaos_floor_encounters is INTENTIONALLY emitted twice: here (the copy
        # the bake actually consumes) and top-level (pre-fix seed rescue) -- see
        # the note at the top-level copy in the return dict below.
        on_disc["chaos_floor_encounters"] = bool(
            self.options.chaos_floor_encounters.value)
        # Shop AP stock for the client, in two shapes.
        #
        # shop_ap_rows (authoritative): [shop, category, base_width,
        # [[placeholder id, price], ...]] -- one entry per PARALLEL offer row.
        # Each row owns its own item id because price, name and description all
        # hang off the item record rather than the shop row; base_width is the
        # store's normal stock count, which the client needs to re-render the
        # tail after a sale.
        #
        # shop_ap (legacy): the pre-parallel [shop, cat, gid, [prices]] shape,
        # row 0 only. Kept so a client from before this feature still boots an
        # in-flight seed instead of crashing on an unknown shape.
        shop_ap = None
        shop_ap_rows = None
        if shop_ap_prices is not None:
            # The post-shuffle per-seed placeholder map (static fallback should
            # never trigger; guards a cache built without it). The client reads
            # (cat, gid) from here.
            ph = seed_placeholders or RANDO.default_placeholders()
            widths = shop_base_widths or {}
            shop_ap_rows = []
            shop_ap = []
            # Every registered location must own a shelf row. Structurally
            # guaranteed since v2 (the reserved constants cover every legal
            # count), so a shortfall here is memory corruption or a regression
            # in reserved_placeholder_map -- assert, never a degrade path.
            # (Before v2 this was a real failure mode: the per-seed placeholder
            # draw ran dry at high loot amounts and shipped "Gaia Armor Shop:
            # AP Stock 2" as an unbuyable check, 2026-08-15.)
            starved = {s: (n, len(RANDO.ph_list(ph, s)) if s in ph else 0)
                       for s, n in self._shop_offer_counts().items()
                       if (len(RANDO.ph_list(ph, s)) if s in ph else 0) < n}
            assert not starved, \
                f"reserved shop placeholders came up short: {starved}"
            for s in sorted(shop_offer_prices):
                if s not in ph:
                    continue            # store carries no offer row this seed
                rows = RANDO.ph_list(ph, s)
                prices = shop_offer_prices[s]
                if not rows or not prices:
                    continue
                cat = rows[0][0]
                shop_ap_rows.append(
                    [s, cat, int(widths.get(s, 0)),
                     [[g, p] for (_c, g), p in zip(rows, prices)]])
                shop_ap.append([s, cat, rows[0][1], prices[:1]])
        # Hint rows for the client, in the SAME tail as the AP offers and after
        # them: [shop, category, base_width, [[placeholder id, price, product
        # label, [location ids]], ...]]. A hint row is not a location -- it
        # carries the ids it will scout with create_as_hint instead. base_width
        # is the store's normal stock count, identical to the offers' copy,
        # because both kinds of row share one contiguous tail.
        #
        # PAIRING CONTRACT: the allocator seated the price queue by walking the
        # stores in ascending ordinal and taking rows off the front, so walking
        # them the same way here matches each row back to the product whose
        # price was baked into it. Nothing else ties the two together -- rando
        # never sees a label.
        hint_shop_rows = None
        if hint_products and hint_placeholders:
            widths = shop_base_widths or {}
            queue = list(hint_products)
            hint_shop_rows = []
            for s in sorted(hint_placeholders):
                ph_rows = RANDO.ph_list(hint_placeholders, s)
                if not ph_rows:
                    continue
                take, queue = queue[:len(ph_rows)], queue[len(ph_rows):]
                cat = ph_rows[0][0]
                hint_shop_rows.append(
                    [s, cat, int(widths.get(s, 0)),
                     [[g, price, label, lids]
                      for (_c, g), (label, price, lids) in zip(ph_rows, take)]])
        # Boss minions: curated adds rolled ONCE here (seed-static -- retrying a
        # boss refights the same set). Own RNG stream (seed+player derived) so it
        # never perturbs self.random consumption of existing options. The client
        # injects the plan into the bake feats (see ApClient) and the patcher
        # edits formation records + rebuilds MS2_<fid> sprite packs.
        minion_plan = None
        if self.options.boss_minions.value:
            import random as _random
            from . import boss_minions as BM
            _rng = _random.Random(f"{self.multiworld.seed}-{self.player}-bossminions")
            minion_plan = BM.roll_plan(self.options.boss_minions.value, _rng)
        return {
            "boss_minions_plan": minion_plan,
            "encounter_rate": self.options.encounter_rate.value / 100.0,
            "xp_boost": self.options.xp_boost_percentage.value / 100.0,
            "gil_boost": self.options.gil_boost_percentage.value / 100.0,
            # Written ONCE at new game by the client (ApClient._starting_gil_loop),
            # same freshness gate as the party/job write. 500 = vanilla.
            "starting_gil": int(self.options.starting_gil.value),
            "boss_difficulty": self.options.boss_difficulty_percentage.value / 100.0,
            "monster_power": self.options.monster_power_percentage.value / 100.0,
            "party_jobs": party_jobs,
            "naked_monks": bool(self.options.naked_monks.value),
            # Quality of life: in-game Config menu defaults the client writes
            # ONCE at new game and then leaves to the player (see
            # ApClient._movement_loop). spell_chance_colors is on-disc, so it
            # rides the ON_DISC_OPTIONS dict instead of a key here.
            "auto_dash": bool(self.options.auto_dash.value),
            "message_speed": int(self.options.message_speed.value),
            "cursor_mode": int(self.options.cursor_mode.value),
            "thief_steal": bool(self.options.thief_steal.value),
            # Live client option: the client sells AP-delivered gear that no
            # party job (current or promoted) can ever equip, paying the
            # effective sell value in gil instead. Judged against party_jobs +
            # the seed's effective weapon/armor tables (rando.gear_auto_sell_
            # value), so it needs nothing beyond what slot_data already carries.
            "auto_sell_unusable_items": bool(
                self.options.auto_sell_unusable_items.value),
            "dangerous_forests": bool(self.options.dangerous_forests.value),
            # DangerousForests reads this at bake time to pick the harder tier list
            # (so the two options STACK: harder forests get tougher signature mobs).
            # Forests are an overworld feature -> follow the overworld toggle.
            "harder_encounters": bool(self.options.harder_overworld_encounters.value),
            # The dungeon twin. Ships separately because the client needs BOTH
            # toggles to know which maps carry boss cameos (rando.cameo_soft_map).
            "harder_dungeon_encounters": bool(
                self.options.harder_dungeon_encounters.value),
            # NB: chaos_floor_pools / chaos_floor_encounters are CODE features and
            # live in on_disc (above), not here -- see the note there. This key is
            # kept top-level too so the client can rescue seeds generated before
            # the on_disc fix.
            "chaos_floor_encounters": bool(
                self.options.chaos_floor_encounters.value),
            # single yaml gradient; extended is a superset of early. Client still
            # consumes two independent bools (EARLY/EXTENDED grid-edit unions).
            "early_open_progression":
                self.options.open_progression.value >= OpenProgression.option_early,
            "extended_open_progression":
                self.options.open_progression.value >= OpenProgression.option_extended,
            "northern_docks": bool(self.options.northern_docks.value),
            # Per-dungeon dynamic-chest caps (dungeon index -> count of the FIRST opens
            # that become AP checks). The client arms its map-gated chest bp and, for the
            # first `cap` opens in each bonus dungeon, strips the native loot + delivers
            # the AP item (open cap+1.. = vanilla). 0 (incl. exclude_bonus_dungeons) = the
            # dungeon is never hooked. See logic.BONUS_DUNGEONS / client.
            "bonus_dyn_caps": {str(dg): cap for dg, cap in self._dyn_caps().items()},
            # exclude_bonus_dungeons: the client scout must skip the 16 static DLC
            # chests (idx 252-267) exactly as create_regions does -- else it scouts
            # ids the seed doesn't own (the runtime known-filter drops them, but we
            # keep the unfiltered derivation in lockstep; test_scout_parity enforces).
            "exclude_bonus_dungeons": bool(self.options.exclude_bonus_dungeons.value),
            # lute_tablets: tablets needed before the client sets the Lute
            # possession bit (0 = option off; the Lute is a normal AP item).
            "lute_tablets_required": self._lute_tablets()[1],
            # equipment_runes: runes needed before the client sets story flag 62
            # (Equipment Rune Key assembled -> activatable equipment usable in
            # battle). 0 = gate off (patcher installs no usability detour).
            "equipment_runes_required": self._equipment_runes()[1],
            # levistone_shards: shards needed before the client grants the real
            # Levistone (possession + obtained/airship bits -> airship raised).
            # 0 = option off (the Levistone is a normal AP item).
            "levistone_shards_required": self._levistone_shards()[1],
            # crystals_needed: 4 = vanilla; <4 rides into the on-disc bake as
            # wrapper-cave context (client _build_bake) + the tracker's N-of-4.
            "crystals_needed": int(self.options.crystals_needed.value),
            # bonus_dungeon_crystals: crystals activate by beating a Soul-of-Chaos
            # superboss instead of the Fiend. Bake context (client _build_bake ->
            # wrapper counts shadow bits) + tracker/logic decouple. Off = vanilla.
            "bonus_dungeon_crystals": bool(self.options.bonus_dungeon_crystals.value),
            # death_link: client enables the DeathLink tag + wipe-detect loop.
            # severity = how many LIVING party members a received death kills.
            "death_link": bool(self.options.death_link.value),
            "death_link_severity": int(self.options.death_link_severity.value),
            "shop_ap": shop_ap,
            "shop_ap_rows": shop_ap_rows,
            # v2 marker: tail gids are the RESERVED_SHOP_PLACEHOLDERS constants
            # SHARED across stores; the client must author names/prices per
            # town (street map id / store-id edge) instead of one global bank
            # pass. Absent on pre-v2 seeds, whose gids are globally unique.
            "shop_ap_shared": bool(shop_ap_rows or hint_shop_rows),
            "hint_shop_rows": hint_shop_rows,
            RANDO.SLOT_KEY: RANDO.slot_data_from_tables(shuffle_tables),
            ON_DISC_SLOT_KEY: on_disc,
        }

    def generate_early(self) -> None:
        # Hard-fail incompatible option combos BEFORE any region/item work. Reads
        # options only -- MUST NOT touch self.random (that would reorder the RNG
        # stream create_items/_seed_shuffle consume and break seed reproducibility).
        if (self.options.bonus_dungeon_crystals.value
                and self.options.exclude_bonus_dungeons.value):
            raise OptionError(
                "Final Fantasy 1 PSP: 'bonus_dungeon_crystals' moves Crystal "
                "activation onto the Soul-of-Chaos superbosses, but "
                "'exclude_bonus_dungeons' removes those dungeons entirely (no "
                "superbosses to beat, so the seed is unwinnable). Enable at most "
                "one of them.")

    def create_regions(self) -> None:
        menu = Region("Menu", self.player, self.multiworld)
        self.multiworld.regions.append(menu)

        # all real chests -> locations in Menu (access enforced by set_rules).
        # EXCEPTION: the Levistone treasure_index is a phantom chest -- the Ice Cave
        # Levistone is an EVENT PICKUP that sets no chest-open bit, so a poll-based
        # chest check there can never fire. Drop it here; the Levistone becomes a real
        # NPC-style location (LOGIC.NPC_LOCATIONS "Ice Cave - Levistone") detected on
        # its obtained-event flag instead. The Levistone item stays in the pool (found
        # anywhere) and that NPC location re-balances the one dropped chest, so the
        # itempool/location counts still match. See logic.py LEVISTONE_TREASURE_IDX.
        self._chest_locs = []
        removed = self._removed_chest_idx()
        for lid, name, idx in DATA.LOCATIONS:
            if idx in removed:                          # phantom chests + (when
                continue                                # exclude_bonus_dungeons) the
                                                        # 16 static DLC chests; the
                                                        # client scout + create_items
                                                        # skip the SAME set.
            loc = FF1PSPLocation(self.player, name, lid, menu)
            menu.locations.append(loc)
            self._chest_locs.append((loc, idx))

        # bonus_dungeon_crystals: the Fiend events grant a "<Fiend> Defeated" token
        # (dungeon access) instead of the crystal itself; the crystal is granted by a
        # new "<Dungeon> - Cleared" event (below) gated on clearing that bonus dungeon.
        bonus_cry = bool(self.options.bonus_dungeon_crystals.value)

        # event locations: each holds a locked GATE token (not in the pool, no id)
        self._event_locs = []
        for ev_name, region, token, extra in LOGIC.EVENTS:
            if bonus_cry and token in LOGIC.CRYSTALS:
                token = LOGIC.FIEND_TOKEN[token]   # Fiend grants "defeated", not crystal
            loc = FF1PSPLocation(self.player, ev_name, None, menu)
            loc.place_locked_item(self.create_item(token))
            menu.locations.append(loc)
            self._event_locs.append((loc, region, extra))
            # crystals_needed: set_rules swaps these events' 4-crystal AND-gate
            # for an N-of-4 count rule (mirrors the on-disc wrapper cave). The Black
            # Orb (crystals + Lute) opens the endgame; Crystals Placed (crystals only)
            # opens the Chaos Shrine 3F plaza chests.
            if token == LOGIC.BLACK_ORB_DESTROYED:
                self._black_orb_loc = loc
            elif token == LOGIC.CRYSTALS_PLACED:
                self._crystals_placed_loc = loc

        # bonus_dungeon_crystals: one locked "<Dungeon> - Cleared" event per bonus
        # dungeon holds that dungeon's element crystal, gated (set_rules) on the
        # dungeon being accessible+clearable -- i.e. the same rule as the dungeon's
        # own chests but with the crystal replaced by the Fiend-defeated token, so no
        # circular gate. Off -> _clear_locs stays empty (set_rules iterates it either
        # way) and no crystal source moves, so default seeds are byte-identical.
        self._clear_locs = []          # (loc, dg, crystal)
        if bonus_cry:
            for dg, dname, floors, defcap, crystal, attr in LOGIC.BONUS_DUNGEONS:
                loc = FF1PSPLocation(self.player, f"{dname} - Cleared", None, menu)
                loc.place_locked_item(self.create_item(crystal))
                menu.locations.append(loc)
                self._clear_locs.append((loc, dg, crystal))

        # NPC story-event locations: REAL randomized checks (King / Princess) that
        # take a pool item (not locked). Detected at runtime via story flags.
        self._npc_locs = []
        self._npc_ord = {}      # loc -> NPC ordinal (for LOGIC.npc_rule_alts)
        for nm, ordn, region, extra in LOGIC.NPC_LOCATIONS:
            loc = FF1PSPLocation(self.player, nm, ID.npc_loc_id(ordn), menu)
            menu.locations.append(loc)
            self._npc_locs.append((loc, region, extra))
            self._npc_ord[loc] = ordn

        # Shop AP-stock locations: each store lists its offers in PARALLEL, one
        # row per offer, so the player sees the whole shelf and buys in any
        # order. Detected at runtime via the placeholder item landing in the
        # inventory (each row has its own placeholder id).
        self._shop_locs = []
        counts = self._shop_offer_counts()
        for nm, shop, reqs in LOGIC.SHOP_LOCATIONS:
            for k in range(counts.get(shop, 0)):
                loc = FF1PSPLocation(self.player, LOGIC.shop_location_name(nm, k),
                                     ID.shop_loc_id(shop, k), menu)
                menu.locations.append(loc)
                self._shop_locs.append((loc, nm, reqs))

        # Dynamic bonus-dungeon chest locations: the first `cap` procedural chests in
        # each Soul-of-Chaos dungeon (cap = the dungeon's yaml Range, 0 if excluded).
        # Detected/delivered live by the client's map-gated chest bp (per-dungeon
        # counter). Each is gated on the dungeon's element CRYSTAL (its Fiend seal).
        # See logic.BONUS_DUNGEONS / bonus-dungeon-chests memory.
        self._dyn_locs = []           # (loc, dg, gate_token)
        caps = self._dyn_caps()
        for dg, dname, floors, defcap, gate_tok, attr in LOGIC.BONUS_DUNGEONS:
            for o in range(caps[dg]):
                nm = LOGIC.dyn_chest_location_name(dname, o)
                loc = FF1PSPLocation(self.player, nm, ID.dyn_chest_loc_id(dg, o), menu)
                menu.locations.append(loc)
                self._dyn_locs.append((loc, dg, gate_tok))

        # lute_tablets: locked "Lute Assembled" event holds the Lute itself, gated
        # (set_rules) on holding lute_tablets_required tablets. Every existing
        # LUTE-gated rule (Black Orb) then works unchanged -- the Lute is simply
        # derived instead of found. Off (count 0) -> no event; Lute stays in the pool.
        self._lute_evt = None
        n_tab, _need = self._lute_tablets()
        if n_tab:
            self._lute_evt = FF1PSPLocation(
                self.player, LOGIC.LUTE_ASSEMBLED_LOCATION, None, menu)
            self._lute_evt.place_locked_item(self.create_item(LOGIC.LUTE))
            menu.locations.append(self._lute_evt)

        # levistone_shards: locked "Levistone Assembled" event holds the Levistone
        # itself, gated (set_rules) on holding levistone_shards_required shards.
        # The LEVISTONE-gated rule (the Ryukhan Desert Airship event) then works
        # unchanged -- the Levistone is derived instead of found. Off (count 0)
        # -> no event; the Levistone stays in the pool.
        self._levi_evt = None
        n_shard, _sneed = self._levistone_shards()
        if n_shard:
            self._levi_evt = FF1PSPLocation(
                self.player, LOGIC.LEVISTONE_ASSEMBLED_LOCATION, None, menu)
            self._levi_evt.place_locked_item(self.create_item(LOGIC.LEVISTONE))
            menu.locations.append(self._levi_evt)

        # goal: Chaos defeated -> Victory (placed, locked); gated by the Black Orb
        self._chaos = FF1PSPLocation(self.player, GOAL_LOCATION, None, menu)
        self._chaos.place_locked_item(self.create_item(VICTORY_ITEM))
        menu.locations.append(self._chaos)

    def create_items(self) -> None:
        # one item token per real chest = the vanilla contents multiset (a shuffle).
        # DATA.LOCATIONS and DATA.ITEM_POOL are parallel (gen_apdata emits them in
        # lockstep), so drop the SAME idx create_regions drops -- EXCEPT the phantom
        # Levistone (idx 198), whose pool token stays and is rebalanced by the "Ice
        # Cave - Levistone" NPC location below. exclude_bonus_dungeons drops the 16
        # static DLC chests here too, keeping len(pool) == chest-location count.
        pool_drop = self._removed_chest_idx() - set(LOGIC.PHANTOM_TREASURE_INDICES)
        kept = [(name, cls) for (_lid, _nm, idx), (_iid, name, cls)
                in zip(DATA.LOCATIONS, DATA.ITEM_POOL) if idx not in pool_drop]
        pool = [name for (name, _cls) in kept]

        # Job-advancement items: swap one filler out for each job item so the pool
        # length (== chest count) stays balanced. Option A = one scroll upgrades the
        # WHOLE matching job, so we add at most ONE scroll per base job. Which jobs:
        # only the ones the starting party will actually have. If any member is
        # `vanilla` (job decided at char-creation, unknown at gen), we can't know, so
        # add all 6 scrolls. Fixed/random picks contribute their concrete job.
        swap_in = []
        # Lute Tablets FIRST (progression -- must never be starved out of the
        # filler slots by tomes/scrolls). The tablet count is n_tab: one copy
        # balances the Princess NPC location below (seat of the removed Lute),
        # the other n_tab-1 swap in for filler here.
        n_tab, _need = self._lute_tablets()
        if n_tab:
            swap_in += [LOGIC.LUTE_TABLET] * (n_tab - 1)
        # Levistone Shards (progression, same priority tier as tablets). Unlike
        # the Lute, the Levistone IS a base-pool token (the converted idx-198
        # chest, see the NPC balance note below), so the first shard takes the
        # Levistone's own pool seat in place and only the remaining n_shard-1
        # copies swap in for filler.
        n_shard, _sneed = self._levistone_shards()
        if n_shard:
            pool[pool.index(LOGIC.LEVISTONE)] = LOGIC.LEVISTONE_SHARD
            swap_in += [LOGIC.LEVISTONE_SHARD] * (n_shard - 1)
        # Exotic/priceless loot pool: selected gear swapped into filler slots
        # (after tablets, before runes/tomes -- never starves them out of the ~252
        # filler slots). NO candidate is guaranteed: every vanilla chest token a
        # candidate holds (_LOOT_CHEST_NAMES) is demoted to filler unconditionally,
        # so the gear exists only if the draw picked it -- some seeds have no
        # Excalibur/Masamune/Ribbon at all, and at loot amount 0 none of it exists.
        # The chest itself stays a normal randomized AP location either way.
        gear_names = self._gear_pool()[0]
        _demote = set(_LOOT_CHEST_NAMES)
        # Reserved shop placeholders that double as CHEST POOL items (only the
        # last two armor constants -- Leather Cap, Bronze Gloves -- qualify;
        # they were ordered last for exactly this). A reserved gid must not be
        # receivable (its equip masks are zeroed and its authored identity
        # follows the id), so the seed demotes its chest token to filler --
        # but ONLY when the rolled counts actually reach that constant, so a
        # default 5-row seed keeps both items in the pool untouched.
        used_a = self._reserved_gear_used()[1]
        for depth, (cat, gid) in enumerate(RANDO.RESERVED_SHOP_PLACEHOLDERS[1],
                                           start=1):
            if depth <= used_a:
                nm = FF1DATA.ARMOR.get(gid)
                if nm in {n for (_i, n, _c) in DATA.ITEM_POOL}:
                    _demote.add(nm)
        if gear_names:
            swap_in += gear_names
        for _i, _nm in enumerate(pool):
            if _nm in _demote:
                pool[_i] = self.get_filler_item_name()
        # Equipment Runes next (useful; before the ~64 tomes so a big tome pool
        # can't starve them out of filler slots). All n copies are filler swaps
        # (no net-new location to balance -- the gate is client/on-disc state).
        n_rune, _rneed = self._equipment_runes()
        if n_rune:
            swap_in += [LOGIC.EQUIPMENT_RUNE] * n_rune
        # Spell Tomes: swapped in for filler (the pool has ~252 filler entries).
        # Only tomes the EVENTUAL party can learn are added -- a tome for a spell no
        # party class can ever learn is dead weight (the in-game teach gate rejects
        # it), and shuffle_magic_shops moves spells between store tiers, changing who
        # can learn what. See _learnable_tome_names. Excluded tomes simply leave their
        # filler in place (pool length == chest count is unaffected).
        # Job Scrolls first (only 6 -- never starved by the ~64 tomes): six
        # per-class boost items swapped in for filler, same balance mechanism
        # as tomes (pool length == chest count unchanged).
        if self.options.job_scroll_boosts.value:
            swap_in += self._party_scroll_names()
        if self.options.spell_tomes.value:
            swap_in += self._learnable_tome_names()
        fillers = [i for i, (_n, cls) in enumerate(kept) if cls == 'filler']
        for k, ji in enumerate(swap_in):
            if k < len(fillers):
                pool[fillers[k]] = ji

        for name in pool:
            self.multiworld.itempool.append(self.create_item(name))

        # NPC locations need one pool item each: Princess is balanced by the Lute,
        # Bikke by the Ship, the Crescent Lake sage by the Canoe (all progression,
        # found anywhere now). King was removed (bridge always built).
        # The 4th NPC location ("Ice Cave - Levistone") is NOT balanced here: unlike
        # Lute/Ship/Canoe (which were events that added net-new fillable locations),
        # the Levistone is a CONVERTED chest -- its item already lives in the base
        # `pool` above (idx 198's vanilla content). Dropping the phantom idx-198 chest
        # (create_regions) removed one chest location; adding this NPC location adds
        # one back, so the base pool re-balances it. Appending a second Levistone here
        # would over-fill the pool.
        # lute_tablets: the Lute is NOT in the pool (it sits locked at the "Lute
        # Assembled" event); its Princess balance slot holds the first tablet copy.
        self.multiworld.itempool.append(self.create_item(
            LOGIC.LUTE_TABLET if n_tab else LOGIC.LUTE))
        self.multiworld.itempool.append(self.create_item(LOGIC.SHIP))
        self.multiworld.itempool.append(self.create_item(LOGIC.CANOE))

        # Five more NPC locations (Sarda/Earth Rod, Lefein/Chime, Robot/Warp Cube,
        # Caravan/Bottled Faerie, Fairy/Oxyale) promoted from EVENT tokens to real
        # randomized locations (2026-07-06). Each adds one NET-NEW fillable location
        # (they were locked event locations before, not chests), so each needs one
        # matching pool item for balance -- exactly like Lute/Ship/Canoe above. The
        # items are registered as progression key items (data.ITEM_TABLE) and are found
        # anywhere in the multiworld; the region/endgame rules gate downstream progress
        # on their NAMES. Star Ruby is NOT here -- it stays a chest (already in `pool`).
        for _promoted in (LOGIC.EARTH_ROD, LOGIC.CHIME, LOGIC.WARP_CUBE,
                          LOGIC.BOTTLED_FAERIE, LOGIC.OXYALE):
            self.multiworld.itempool.append(self.create_item(_promoted))

        # Classic-7 Mystic-Key trade chain (2026-07-06): Crystal Eye / Jolt Tonic /
        # Mystic Key promoted from EVENT tokens to real pool items. Their grantor NPCs
        # (Astos / Matoya / Elf Prince) became three NET-NEW randomized NPC locations
        # (logic.NPC_LOCATIONS ordinals 10-12), so each needs one matching pool item for
        # balance -- exactly like Earth Rod/Chime above. Registered as progression key
        # items (data.ITEM_TABLE, game ids 3/4/5); found anywhere; the trade-chain +
        # Mystic-Key-door rules gate downstream progress on their NAMES. Crown / Nitro /
        # Rat's Tail (the classic-7 CHEST items) are NOT here -- they already live in the
        # base `pool` at their vanilla treasure indices (139 / 8 / 28). Adamantite is not
        # modeled (no PSP chest for it; downstream is a non-goal reward -- see logic.py).
        for _promoted in (LOGIC.CRYSTAL_EYE, LOGIC.JOLT_TONIC, LOGIC.MYSTIC_KEY):
            self.multiworld.itempool.append(self.create_item(_promoted))

        # Adamantite + Excalibur (2026-07-06): live RE says Adamantite is a Flying
        # Fortress EVENT PICKUP (not a chest) and the Dwarf Smith turn-in forges
        # Excalibur -- both became NET-NEW randomized NPC locations (logic.NPC_LOCATIONS
        # ordinals 13/14), so each needs one matching pool item for balance (like the
        # Earth Rod/Chime batch). Adamantite (progression, gates the Smith location) and
        # Excalibur (filler weapon, gates nothing) are registered in data.ITEM_TABLE and
        # found anywhere in the multiworld. Neither is in the base `pool` (no vanilla
        # chest holds them).
        self.multiworld.itempool.append(self.create_item(LOGIC.ADAMANTITE))
        # Excalibur balances the Dwarf-Smith NPC location. Excalibur is a priceless
        # LOOT candidate and no candidate is guaranteed, so this balance slot is
        # always a filler -- an Excalibur exists in the seed only if the priceless
        # draw picked it (swapped in above), exactly like Masamune/Ultima/Ribbon.
        self.multiworld.itempool.append(self.create_item(self.get_filler_item_name()))

        # Bahamut class-change turn-in (2026-07-08): NET-NEW randomized NPC location
        # (logic.NPC_LOCATIONS ordinal 15), gated on AIRSHIP + Rat's Tail. Rat's Tail
        # is already in the base `pool` (vanilla treasure idx 28) and Bahamut's native
        # reward is the promotion itself (no item), so this location needs exactly ONE
        # balancing filler item -- like a shop AP slot. The multiworld fill decides what
        # actually lands at Bahamut.
        self.multiworld.itempool.append(self.create_item(self.get_filler_item_name()))

        # Shop AP stock adds one location per offer row -> as many more filler
        # items for balance (drawn from the filler consumable pool: Apples, Plus
        # tonics, Soma Drops, Fangs, Curtains). Derived from the locations that
        # were actually created, so a shop that was granted fewer rows than the
        # option asked for can never leave the pool unbalanced.
        for _ in range(len(self._shop_locs)):
            self.multiworld.itempool.append(
                self.create_item(self.get_filler_item_name()))

        # Dynamic bonus-dungeon chest locations: each created dyn location is a NET-NEW
        # fillable location (not a chest / not an event), so each needs one matching pool
        # item for balance -- one filler apiece (like a shop AP slot). The multiworld fill
        # decides what actually lands in each.
        for _ in range(len(self._dyn_locs)):
            self.multiworld.itempool.append(
                self.create_item(self.get_filler_item_name()))

        # The bridge is always built (the runtime client forces the overworld bridge
        # bit every game), so there is no BRIDGE token at all any more -- Matoya's
        # Cave, the Pravoka shops and the Bikke check are simply free. The canal is
        # NOT pre-opened; it is blown normally (Nitro -> Nerrick).

    def set_rules(self) -> None:
        from worlds.generic.Rules import set_rule
        player = self.player

        def as_alts(rule):
            """Normalize a rule to a list of AND-alternatives (list of token lists).
            A flat token list is one alternative; a list of lists is OR-of-ANDs; an
            empty rule is a single always-true alternative."""
            if not rule:
                return [[]]
            if isinstance(rule[0], (list, tuple)):
                return [list(a) for a in rule]
            return [list(rule)]

        def make_rule(region_rule, extra=()):
            """Access = (ANY OR-alternative of the region rule is satisfied) AND all
            of `extra` (per-chest / per-event tokens) are held. state.has_all([]) is
            True, so an empty alternative means always-reachable."""
            alts = as_alts(region_rule)
            ex = list(extra)
            def rule(state):
                if ex and not state.has_all(ex, player):
                    return False
                return any(state.has_all(a, player) for a in alts)
            return rule

        # effective region + shop rules for the open-world toggles. One yaml
        # gradient (open_progression): extended is a strict superset of early.
        op = self.options.open_progression.value
        early = op >= OpenProgression.option_early
        extended = op >= OpenProgression.option_extended
        docks = bool(self.options.northern_docks.value)
        region_rules = LOGIC.region_rules_for(early, extended, docks)
        shop_overrides = LOGIC.shop_rules_for(early, extended, docks)

        # bonus_dungeon_crystals: a bonus dungeon's chests must gate on FIEND access
        # (not the crystal), because the crystal now comes from clearing that dungeon
        # -- gating a chest on the crystal it helps produce would be circular. This
        # helper maps a dungeon's element crystal to its Fiend-defeated token in bonus
        # mode, and is the identity otherwise (default seeds unchanged).
        bonus_cry = bool(self.options.bonus_dungeon_crystals.value)

        def dungeon_gate(crystal):
            return LOGIC.FIEND_TOKEN[crystal] if bonus_cry else crystal

        # chests: region rule (+ unmapped fallback) AND per-chest token
        dlc_gate = {d: tok for (d, _n, _f, _c, tok, _o) in LOGIC.BONUS_DUNGEONS}
        for loc, idx in self._chest_locs:
            # live-swept static bonus chest: gated like its dungeon's dynamic
            # chests (element crystal + entrance tokens), NOT the [AIRSHIP]
            # unmapped fallback (which under-gates -- broken-seed risk).
            sdg = LOGIC.DLC_STATIC_IDX_DUNGEON.get(idx)
            if sdg is not None:
                extra = ([dungeon_gate(dlc_gate[sdg])]
                         + LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(sdg, []))
                if sdg == 3:
                    alts = [[]] if early else [LOGIC.WHISPERWIND_SHIP_CANAL_ALT]
                else:
                    alts = []
                set_rule(loc, make_rule(alts, extra))
                continue
            region, chest_token = LOC_INFO.get(idx, (None, None))
            region_rule = (LOGIC.UNMAPPED_RULE if region is None
                           else region_rules.get(region, []))
            extra = []
            if chest_token == "MysticKey":
                extra.append(LOGIC.MYSTIC_KEY)
            elif chest_token == "TitanFed":
                extra.append(LOGIC.TITAN_FED)
            set_rule(loc, make_rule(region_rule, extra))

        # events / NPC locations: region rule AND event-specific extra requirements
        # crystals_needed < 4: the Black Orb event drops the crystal tokens from
        # its AND-extra and instead needs any N of the four (the on-disc wrapper
        # cave makes the game agree). Tracker mirrors in resolve_tokens.
        crystals_needed = int(self.options.crystals_needed.value)
        count_locs = (getattr(self, "_black_orb_loc", None),
                      getattr(self, "_crystals_placed_loc", None))
        for loc, region, extra in self._event_locs:
            if loc in count_locs and crystals_needed < 4:
                non_cry = [t for t in extra if t not in LOGIC.CRYSTALS]
                base = make_rule(region_rules.get(region, []), non_cry)

                def orb_rule(state, _base=base, _n=crystals_needed):
                    if not _base(state):
                        return False
                    return sum(1 for c in LOGIC.CRYSTALS
                               if state.has(c, player)) >= _n
                set_rule(loc, orb_rule)
                continue
            set_rule(loc, make_rule(region_rules.get(region, []), extra))
        # NPCs: region rule AND the NPC's own tokens, OR any NPC_ALT_RULES bypass
        # (Sarda is also reachable by a plain airship landing) -- npc_rule_alts folds
        # both into one OR-of-ANDs so extra is already baked in.
        for loc, region, extra in self._npc_locs:
            alts = LOGIC.npc_rule_alts(self._npc_ord[loc],
                                       region_rules.get(region, []), extra)
            set_rule(loc, make_rule(alts))

        # shop AP stock: town-access tokens only (gil is grindable, not modeled);
        # overrides key on the SHOP name, applying to every offer of that shop
        for loc, shop_name, reqs in self._shop_locs:
            set_rule(loc, make_rule(shop_overrides.get(shop_name, reqs)))

        # dynamic bonus-dungeon chests: gated on the dungeon's element CRYSTAL (its
        # Fiend seal) plus that dungeon's vehicle/key-item entrance requirement (see
        # logic.DLC_DUNGEON_EXTRA_TOKENS). Whisperwind Cove additionally needs Ice
        # Cave access: early_open_progression (foot/canoe river) OR Ship+Nitro Powder.
        for loc, dg, gate_tok in self._dyn_locs:
            extra = [dungeon_gate(gate_tok)] + LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(dg, [])
            if dg == 3:
                ice_cave_alts = [[]] if early else [LOGIC.WHISPERWIND_SHIP_CANAL_ALT]
                set_rule(loc, make_rule(ice_cave_alts, extra))
            else:
                set_rule(loc, make_rule([], extra))

        # bonus_dungeon_crystals: each "<Dungeon> - Cleared" event grants that
        # dungeon's crystal, gated on the SAME rule as the dungeon's own chests
        # (fiend-defeated token + entrance tokens + Whisperwind ice-cave alt) -- with
        # the crystal NOT in the rule, so reaching+clearing the dungeon derives the
        # crystal and there is no circular gate. Empty (identity-inert) when off.
        for loc, dg, crystal in self._clear_locs:
            extra = ([LOGIC.FIEND_TOKEN[crystal]]
                     + LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(dg, []))
            if dg == 3:
                alts = [[]] if early else [LOGIC.WHISPERWIND_SHIP_CANAL_ALT]
            else:
                alts = []
            set_rule(loc, make_rule(alts, extra))

        # (RETIRED 2026-07-27: the late_activatable_equipment placement gate stood
        # here -- an add_item_rule forbidding spell-on-use gear at our pre-airship
        # locations. add_item_rule binds only OUR Location objects, so in a
        # multiworld the fill kept seating that gear in other games' early checks
        # (sim: 6-8 of 8 activatables landed foreign across three 3-player seeds).
        # equipment_runes supersedes it by gating activation in-game, which holds
        # regardless of which world the item comes from.)

        # Colossal Experience Bag: the biggest EXP filler (20000) must only land where
        # the player already owns a vehicle (Ship, Canoe, or Airship). It enters the
        # pool solely via get_filler_item_name (Bahamut / shop-AP / dyn-loc balancing
        # slots), so we forbid its code at every location reachable BEFORE any vehicle.
        # A location is "pre-vehicle" if any OR-alternative of its full access
        # requirement (region alt + per-loc extra tokens) contains no vehicle token.
        # Dynamic bonus-dungeon chests are crystal-gated (post-airship) -> never forbid.
        from worlds.generic.Rules import add_item_rule
        VEHICLES = frozenset((LOGIC.SHIP, LOGIC.CANOE, LOGIC.AIRSHIP))
        colossal_code = ITEM_NAME_TO_ID["Experience Bag (Colossal)"]

        def _pre_vehicle(region_rule, extra=()):
            ex = list(extra)
            for alt in as_alts(region_rule):
                if not (VEHICLES & set(alt) or VEHICLES & set(ex)):
                    return True
            return False

        forbid_locs = []
        for loc, idx in self._chest_locs:
            region, chest_token = LOC_INFO.get(idx, (None, None))
            region_rule = (LOGIC.UNMAPPED_RULE if region is None
                           else region_rules.get(region, []))
            extra = []
            if chest_token == "MysticKey":
                extra.append(LOGIC.MYSTIC_KEY)
            elif chest_token == "TitanFed":
                extra.append(LOGIC.TITAN_FED)
            if _pre_vehicle(region_rule, extra):
                forbid_locs.append(loc)
        for loc, region, extra in (self._event_locs + self._npc_locs):
            if _pre_vehicle(region_rules.get(region, []), extra):
                forbid_locs.append(loc)
        for loc, shop_name, reqs in self._shop_locs:
            if _pre_vehicle(shop_overrides.get(shop_name, reqs)):
                forbid_locs.append(loc)

        def _forbid_colossal(item):
            return not (item.player == player and item.code == colossal_code)
        for loc in forbid_locs:
            add_item_rule(loc, _forbid_colossal)

        # Same-map turn-in exclusions. The game picks an NPC's dialog/event branch at
        # MAP LOAD (see the Rosetta and Oxyale notes in client/ff1_data.py), so a key
        # item received in the SAME map as the NPC who consumes it is not acknowledged
        # until the player leaves and re-enters -- the turn-in silently "refuses"
        # (live report: Nitro Powder from a Mount Duergar chest, Nerrick would not take
        # it). Keep those two out of their consumer's map entirely. This is a PLACEMENT
        # fix only: an item sent by another world while the player already stands in the
        # room still needs a room re-entry.
        # NOT applicable to the Mystic Key -- locked doors read the function bit at
        # interaction time, not map load, so same-map use is vanilla-correct there.
        # Prefixes lost their " - " in the 2026-08-07 uniform location rename, so they
        # are now "<Place> " and match both "<Place> Chest #N" and "<Place> NPC: Who".
        # One deliberate change of coverage: the chest formerly named "Mount Duergar -
        # Chest 11" (idx 127) is physically on map 90 = Marsh Cave, so it is now
        # "Marsh Cave Chest #1" and correctly drops OUT of Nerrick's same-map rule.
        SAME_MAP_TURNIN = {
            "Crown":        "Western Keep ",    # Astos
            "Nitro Powder": "Mount Duergar ",   # Nerrick
            "Adamantite":   "Mount Duergar ",   # Dwarf Smith (same map as Nerrick)
        }
        for item_name, loc_prefix in SAME_MAP_TURNIN.items():
            code = ITEM_NAME_TO_ID[item_name]

            def _forbid(item, _code=code):
                return not (item.player == player and item.code == _code)
            for loc in ([l for l, _i in self._chest_locs]
                        + [l for l, _r, _e in (self._event_locs + self._npc_locs)]
                        + [l for l, _s, _r in self._shop_locs]):
                if loc.name.startswith(loc_prefix):
                    add_item_rule(loc, _forbid)

        # lute_tablets: a tablet must never land behind the Lute itself. Any location
        # whose access needs the LUTE (Chaos Shrine basement -- b2/b4 and the Black Orb
        # event) would otherwise be able to hold one of the pieces that derives it,
        # which the count rule cannot see through (the Lute is an EVENT item at the
        # locked "Lute Assembled" location, so fill's own progression logic still lets
        # a tablet sit behind a LUTE-gated door). The 3F plaza is only CRYSTALS_PLACED
        # gated -- tablets there are fine and stay allowed.
        if self._lute_evt is not None:
            def _requires_lute(region_rule, extra=()):
                if LOGIC.LUTE in set(extra):
                    return True
                return all(LOGIC.LUTE in set(alt) for alt in as_alts(region_rule))

            tablet_code = ITEM_NAME_TO_ID[LOGIC.LUTE_TABLET]

            def _forbid_tablet(item):
                return not (item.player == player and item.code == tablet_code)

            lute_locs = []
            for loc, idx in self._chest_locs:
                region, chest_token = LOC_INFO.get(idx, (None, None))
                region_rule = (LOGIC.UNMAPPED_RULE if region is None
                               else region_rules.get(region, []))
                extra = []
                if chest_token == "MysticKey":
                    extra.append(LOGIC.MYSTIC_KEY)
                elif chest_token == "TitanFed":
                    extra.append(LOGIC.TITAN_FED)
                if _requires_lute(region_rule, extra):
                    lute_locs.append(loc)
            for loc, region, extra in (self._event_locs + self._npc_locs):
                if _requires_lute(region_rules.get(region, []), extra):
                    lute_locs.append(loc)
            for loc, shop_name, reqs in self._shop_locs:
                if _requires_lute(shop_overrides.get(shop_name, reqs)):
                    lute_locs.append(loc)
            for loc in lute_locs:
                add_item_rule(loc, _forbid_tablet)

        # lute_tablets: the Lute derives from holding N tablets (count rule --
        # make_rule is boolean-only).
        if self._lute_evt is not None:
            _n_tab, need = self._lute_tablets()
            set_rule(self._lute_evt,
                     lambda state: state.has(LOGIC.LUTE_TABLET, player, need))

        # levistone_shards: a shard must never land behind the airship itself --
        # same reasoning as the tablet forbid above (the Levistone is an EVENT item
        # at the locked "Levistone Assembled" location, so fill's count rule cannot
        # see through it). Unlike the Lute, the Levistone's blast radius is the whole
        # airship-only half of the map, and it is TRANSITIVE through event tokens
        # (AIR_CRYSTAL comes from the airship-only Flying Fortress, so anything gated
        # on it is levistone-gated too). Closure: a granted event token "needs
        # levistone" when EVERY OR-alternative of its access rule (region alts, each
        # ANDed with its extra reqs) contains LEVISTONE, AIRSHIP, or a token already
        # in the set. Deliberately conservative: the crystals_needed<4 count swap may
        # make the Black Orb reachable without the airship crystals, but keeping it
        # in the closure only narrows shard placement, never breaks it. Pool-item
        # cycles (e.g. a shard behind a Mystic Key door whose Key sits post-airship)
        # are accepted the same way the tablet forbid accepts them.
        if self._levi_evt is not None:
            needs_levi = {LOGIC.LEVISTONE, LOGIC.AIRSHIP}

            def _rule_needs_levi(region_rule, extra=()):
                if set(extra) & needs_levi:
                    return True
                return all(set(alt) & needs_levi for alt in as_alts(region_rule))

            # fixpoint over the event graph + the bonus "Cleared" crystal events
            # (token remaps mirror create_regions / the set_rule blocks above)
            _clear_specs = []
            for _loc, dg, crystal in self._clear_locs:
                c_extra = ([LOGIC.FIEND_TOKEN[crystal]]
                           + LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(dg, []))
                c_alts = (([[]] if early else [LOGIC.WHISPERWIND_SHIP_CANAL_ALT])
                          if dg == 3 else [])
                _clear_specs.append((crystal, c_alts, c_extra))
            changed = True
            while changed:
                changed = False
                for _nm, region, token, extra in LOGIC.EVENTS:
                    if bonus_cry and token in LOGIC.CRYSTALS:
                        token = LOGIC.FIEND_TOKEN[token]
                    if token not in needs_levi and _rule_needs_levi(
                            region_rules.get(region, []), extra):
                        needs_levi.add(token)
                        changed = True
                for token, c_alts, c_extra in _clear_specs:
                    if token not in needs_levi and _rule_needs_levi(c_alts, c_extra):
                        needs_levi.add(token)
                        changed = True

            shard_code = ITEM_NAME_TO_ID[LOGIC.LEVISTONE_SHARD]

            def _forbid_shard(item):
                return not (item.player == player and item.code == shard_code)

            levi_locs = []
            for loc, idx in self._chest_locs:
                sdg = LOGIC.DLC_STATIC_IDX_DUNGEON.get(idx)
                if sdg is not None:
                    # static bonus chest: mirror its real rule (crystal/fiend gate
                    # + entrance tokens), not the [AIRSHIP] unmapped fallback
                    extra = ([dungeon_gate(dlc_gate[sdg])]
                             + LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(sdg, []))
                    alts = (([[]] if early else [LOGIC.WHISPERWIND_SHIP_CANAL_ALT])
                            if sdg == 3 else [])
                    if _rule_needs_levi(alts, extra):
                        levi_locs.append(loc)
                    continue
                region, chest_token = LOC_INFO.get(idx, (None, None))
                region_rule = (LOGIC.UNMAPPED_RULE if region is None
                               else region_rules.get(region, []))
                extra = []
                if chest_token == "MysticKey":
                    extra.append(LOGIC.MYSTIC_KEY)
                elif chest_token == "TitanFed":
                    extra.append(LOGIC.TITAN_FED)
                if _rule_needs_levi(region_rule, extra):
                    levi_locs.append(loc)
            for loc, region, extra in (self._event_locs + self._npc_locs):
                if _rule_needs_levi(region_rules.get(region, []), extra):
                    levi_locs.append(loc)
            for loc, shop_name, reqs in self._shop_locs:
                if _rule_needs_levi(shop_overrides.get(shop_name, reqs)):
                    levi_locs.append(loc)
            for loc, dg, gate_tok in self._dyn_locs:
                extra = ([dungeon_gate(gate_tok)]
                         + LOGIC.DLC_DUNGEON_EXTRA_TOKENS.get(dg, []))
                alts = (([[]] if early else [LOGIC.WHISPERWIND_SHIP_CANAL_ALT])
                        if dg == 3 else [])
                if _rule_needs_levi(alts, extra):
                    levi_locs.append(loc)
            for loc in levi_locs:
                add_item_rule(loc, _forbid_shard)

            # the Levistone derives from holding N shards (count rule)
            _n_shard, sneed = self._levistone_shards()
            set_rule(self._levi_evt,
                     lambda state: state.has(LOGIC.LEVISTONE_SHARD, player, sneed))

        set_rule(self._chaos, make_rule([LOGIC.BLACK_ORB_DESTROYED]))
        self.multiworld.completion_condition[player] = \
            lambda state: state.has(VICTORY_ITEM, player)


def run_client(*args) -> None:
    """Archipelago Launcher entry point — invoked when the user clicks
    'Final Fantasy 1 PSP Client' in the AP Launcher. CLI args pass through:
    `ArchipelagoLauncher.exe "Final Fantasy 1 PSP Client" -- <server> <slot>`
    pre-fills the connection so scripted/automated test runs can drive the
    full client path (connect -> bake -> launch) unattended."""
    import functools
    from .client.ApClient import main
    url = args[0] if len(args) > 0 else None
    slot = args[1] if len(args) > 1 else None
    if url or slot:
        launch_subprocess(functools.partial(main, url, None, slot),
                          name="ff1pspClient")
    else:
        launch_subprocess(main, name="ff1pspClient")


components.append(Component(
    "Final Fantasy 1 PSP Client",
    func=run_client,
    component_type=Type.CLIENT,
    icon="ff1psp",
))
components_module.icon_paths["ff1psp"] = f"ap:{__name__}/ff1psp_icon.png"
