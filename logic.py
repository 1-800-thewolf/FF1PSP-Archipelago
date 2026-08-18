"""
Access logic for FF1 PSP — region graph + entrance rules.

Modelled on the FF1 PR (PSP Pixel Remaster) randomizer's access graph
(re_only/pr_ref/Locations.cs) and the canonical FF1 / FiendsOfTheElements NES
randomizer progression. See the ff1prap-crossref memory.

Scope note:
  Randomized locations today: the 254 static chest locations (see gen_apdata.py) +
  15 NPC/story checks (NPC_LOCATIONS) + up to 18 shop AP offers (SHOP_LOCATIONS,
  when shop_ap_offers > 0) + a per-dungeon yaml-capped count of dynamic
  Soul-of-Chaos chest counters (BONUS_DUNGEONS). The 6 KEY ITEMS that live in chests
  (Crown, Nitro Powder, Rosetta Stone, Star Ruby, Levistone, Rat's Tail) are REAL
  AP progression items placed by fill — so they gate logic. NOTE: the Levistone is
  the one exception whose SOURCE is not a real chest — it is an Ice Cave event
  pickup that sets no chest bit, so its treasure_index (198) is dropped from the
  chest pool and the Levistone is found via a real NPC-style location instead (see
  NPC_LOCATIONS / LEVISTONE_TREASURE_IDX below). The item is still a normal pool
  progression item; only its detection differs.
  The remaining vanilla gates (Canal, Airship, the four Crystals, boss/story
  milestones ...) are NOT randomized; the player earns them through normal play.
  We model each as an EVENT: a locked location that hands the player a GATE
  token when its requirements are met. That keeps the reachability graph connected
  so the fill algorithm never buries a needed key item behind itself.

Reconstructed edges:
  re_only only gives us per-chest region + per-chest extra access (MysticKey /
  TitanFed). The world-map region->region connectivity (which vehicle reaches which
  dungeon) is NOT in the dump, so REGION_RULES below is reconstructed from canonical
  FF1. Edges marked  # RECON  are best-effort and worth play-verifying.
"""

# ---- GATE tokens (event item names; not real game items, never in the pool) ----
SHIP     = "Ship"          # REAL pool item (registered in __init__, found anywhere)
CANAL    = "Canal Opened"
CANOE    = "Canoe"         # REAL pool item now (game key item id 17); found anywhere
AIRSHIP  = "Airship"
# NOTE: there is no submarine in FF1 PSP. The Sunken Shrine is reached with the
# Airship + Canoe (to reach Onrac) plus Oxyale (to dive) -- see REGION_RULES.
# CRYSTAL_EYE / JOLT_TONIC / MYSTIC_KEY are the "classic 7" Mystic-Key trade chain,
# promoted to REAL AP pool items (2026-07-06, batch 3): each name matches its game
# key item (registered as progression in __init__ / data.ITEM_TABLE) and is found
# anywhere in the multiworld. Their former grantor NPCs (Astos / Matoya / Elf Prince)
# are now randomized NPC-poll locations (NPC_LOCATIONS ordinals 10-12), so they are
# NOT event tokens anymore. The chain Crown -> Astos -> Crystal Eye -> Matoya ->
# Jolt Tonic -> Elf Prince -> Mystic Key is encoded as per-location extra reqs (each
# NPC needs the previous chain item); Mystic-Key-locked chest regions gate on the
# MYSTIC_KEY item name via LOC_INFO's "MysticKey" token (already wired in set_rules).
MYSTIC_KEY = "Mystic Key"
CRYSTAL_EYE = "Crystal Eye"
JOLT_TONIC = "Jolt Tonic"
LUTE     = "Lute"
# lute_tablets yaml: N tablet pieces replace the Lute in the pool; the Lute is
# instead placed at a locked "Lute Assembled" event gated on holding
# lute_tablets_required tablets. Every LUTE-gated rule stays untouched.
LUTE_TABLET = "Lute Tablet"
EQUIPMENT_RUNE = "Equipment Rune"    # equipment_runes yaml; see ids.rune_item_id
LUTE_ASSEMBLED_LOCATION = "Lute Assembled"
# levistone_shards yaml: N shard pieces replace the Levistone in the pool; the
# Levistone is instead placed at a locked "Levistone Assembled" event gated on
# holding levistone_shards_required shards. Every LEVISTONE-gated rule (the
# Ryukhan Desert Airship event) stays untouched.
LEVISTONE_SHARD = "Levistone Shard"
LEVISTONE_ASSEMBLED_LOCATION = "Levistone Assembled"
ROSETTA_TRANSLATED = "Rosetta Stone (Translated)"

# Six more key items promoted to REAL AP pool items (2026-07-06). Their exact pool
# names match the game key-item names (registered as progression in __init__ via
# data.ITEM_TABLE). Each is found anywhere in the multiworld; its ORIGINAL in-game
# source became a randomized location (Star Ruby = its Earth Cave CHEST; the other
# five = NPC-poll locations in NPC_LOCATIONS). Region/endgame rules gate downstream
# progress on these item NAMES via state.has. See ap-keyitem-grant-infra memory.
STAR_RUBY = "Star Ruby"          # already a chest pool item (Earth Cave); gates Titan
EARTH_ROD = "Earth Rod"          # Sarda NPC -> now a pool item; gates Lich/Earth Crystal
CHIME     = "Chime"              # Lefein NPC -> pool item; gates Sky Castle (Mirage) path
WARP_CUBE = "Warp Cube"          # Waterfall robot -> pool item; gates deep Sky Castle/Tiamat
BOTTLED_FAERIE = "Bottled Faerie"  # Caravan -> pool item; gates the Fairy (Oxyale)
OXYALE   = "Oxyale"              # Fairy -> pool item; gates Sea Shrine (Kraken)

GARLAND_DEFEATED = "Garland Defeated"
VAMPIRE_DEFEATED = "Vampire Defeated"
TITAN_FED = "Titan Fed"
BLACK_ORB_DESTROYED = "Black Orb Destroyed"
# crystals_needed of the four elemental crystals placed on the Chaos Shrine altars
# opens the 3F plaza -- the Lute is NOT required for the plaza (only to descend past
# it into the basement). Derived by an N-of-4 count rule (set_rules /
# tracker.resolve_tokens), exactly like BLACK_ORB_DESTROYED.
CRYSTALS_PLACED = "Crystals Placed"

EARTH_CRYSTAL = "Earth Crystal"
FIRE_CRYSTAL  = "Fire Crystal"
WATER_CRYSTAL = "Water Crystal"
AIR_CRYSTAL   = "Air Crystal"
# crystals_needed yaml: the Black Orb (and the on-disc wrapper cave) needs only
# N of these four. Single source for __init__.set_rules + tracker.
CRYSTALS = (EARTH_CRYSTAL, FIRE_CRYSTAL, WATER_CRYSTAL, AIR_CRYSTAL)

# bonus_dungeon_crystals yaml: normally a Fiend's defeat IS the crystal (the Fiend
# event grants the crystal token directly). With the option ON the crystal instead
# comes from clearing that element's Soul-of-Chaos bonus dungeon, so the Fiend needs
# a SEPARATE "defeated" token to keep gating dungeon access (and the dungeon's own
# chests) without a circular gate (chest needs crystal <- crystal needs clearing the
# dungeon whose chests need the crystal). These tokens are used ONLY when the option
# is on; default seeds never see them. See __init__.create_regions / set_rules.
EARTH_FIEND = "Lich Defeated"
FIRE_FIEND  = "Kary Defeated"
WATER_FIEND = "Kraken Defeated"
AIR_FIEND   = "Tiamat Defeated"
FIEND_TOKEN = {EARTH_CRYSTAL: EARTH_FIEND, FIRE_CRYSTAL: FIRE_FIEND,
               WATER_CRYSTAL: WATER_FIEND, AIR_CRYSTAL: AIR_FIEND}

# chest key items (REAL AP items — exact pool names). STAR_RUBY moved up with the
# other promoted key items; the rest below still live in vanilla chests.
#   CROWN     -> Marsh-Cave-tier chest (PSP treasure idx 139); feeds Astos (Crystal Eye).
#   NITRO     -> Mystic-Key-locked chest (PSP treasure idx 8, LOC_INFO token MysticKey ->
#                requires MYSTIC_KEY); feeds Nerrick (Canal).
#   RATS_TAIL -> chest (PSP treasure idx 28); classically turned in to Bahamut.
#                UPDATE (2026-07-08): Bahamut is a REAL AP location ("Dragon Caves -
#                Bahamut", NPC ordinal 15) gated on AIRSHIP (region) + RATS_TAIL.
#                DESIGN (2026-07-13): Bahamut is BOTH an AP location AND the promotion
#                turn-in. The tail is granted with its function bit (ff1_data
#                KEY_ITEM_FUNCTION_BITS[13]), so Bahamut accepts it and runs the game's
#                NATIVE promotion (whole party); the location is detected by Bahamut-room
#                map id + tail owned (the detector reads possession, not the class-change
#                flag, so native promotion doesn't false-fire it). Client-side scroll
#                promotion was removed (impossible; see job-advancement-items memory).
#                Neither gates the goal. RATS_TAIL has one downstream edge: the Bahamut
#                location requires it.
# ADAMANTITE (classic-7 #6) has NO chest source: the PSP static treasure table holds no
# Adamantite chest (verified via re_only/treasure_table.csv -- only 6 key chests exist:
# Crown/Nitro/Rat's Tail/Star Ruby/Rosetta/Levistone). It IS modeled anyway: live RE
# found the event pickup, so its source is the randomized "Flying Fortress - Adamantite"
# NPC location (ordinal 13), the Adamantite is a REAL pool item, and the Dwarf Smith
# turn-in (ordinal 14) requires it. Nothing downstream gates on it (Smith -> Excalibur
# is a non-goal reward). See the 2026-07-06 note below.
CROWN    = "Crown"
NITRO    = "Nitro Powder"
ROSETTA  = "Rosetta Stone"
LEVISTONE = "Levistone"
RATS_TAIL = "Rat's Tail"

# Adamantite + Excalibur (2026-07-06): live RE says Adamantite is an EVENT PICKUP
# (Flying Fortress area in the PSP layout), NOT a chest -- so its source is a
# randomized NPC-poll location ("Flying Fortress - Adamantite") and the Adamantite is
# a REAL pool item (game key item id 7). The Dwarf Smith turn-in (Adamantite ->
# Excalibur) is ALSO a randomized location ("Mount Duergar - Smith", the Dwarf-cave
# region shared with Nerrick); Excalibur is a WEAPON pool item (cat 2, game id 39).
# Neither gates anything downstream (Smith->Excalibur is an optional non-goal reward),
# so ADAMANTITE has no region/endgame edge; only the Smith LOCATION requires it (you
# must hold the Adamantite to forge). Both are "useful"/filler-class items.
ADAMANTITE = "Adamantite"
EXCALIBUR  = "Excalibur"

# Every gate token that is granted by an event (used to register them as items).
# NOTE: LUTE, SHIP and CANOE are NOT here -- they are REAL pool items now, found
# anywhere in the multiworld and registered as normal AP items in __init__ (Lute =
# key item id 1; Canoe = key item id 17; Ship = a vehicle item, ids.vehicle_item_id).
# EARTH_ROD, CHIME, WARP_CUBE, BOTTLED_FAERIE and OXYALE were ALSO promoted to REAL
# pool items (2026-07-06) -- their former grantor NPCs are now randomized locations
# (NPC_LOCATIONS), so they are NOT event tokens anymore either. The region rules and
# endgame still gate on the item NAMES via state.has. Ship is delivered by the client
# (setStoryFlag id5 -> ship at Provoka); Canoe/the five new items by writing their
# key-item bits; each has its own randomized NPC location. See ap-keyitem-grant-infra.
# CRYSTAL_EYE / JOLT_TONIC / MYSTIC_KEY (classic-7 batch, 2026-07-06) were ALSO
# promoted to REAL pool items -- their grantor NPCs (Astos / Matoya / Elf Prince) are
# now randomized NPC-poll locations (NPC_LOCATIONS ordinals 10-12), so they are NOT
# event tokens anymore. They are registered as progression in __init__ (data.ITEM_TABLE)
# and appended to the itempool; the Mystic-Key-locked chest regions gate on the
# MYSTIC_KEY item NAME via LOC_INFO's "MysticKey" token, and the trade chain is encoded
# as the NPC locations' extra reqs.
# NOTE: BRIDGE was REMOVED 2026-07-17. The bridge is always built (the client forces
# the overworld bit every game), so the token gated nothing -- Matoya's Cave, the
# Pravoka shops and the Bikke check are simply FREE now. Removing it shifts the
# synthetic event-item ids below it, so seeds must be regenerated.
GATE_ITEMS = [
    CANAL, AIRSHIP, ROSETTA_TRANSLATED,
    GARLAND_DEFEATED, VAMPIRE_DEFEATED, TITAN_FED, BLACK_ORB_DESTROYED,
    EARTH_CRYSTAL, FIRE_CRYSTAL, WATER_CRYSTAL, AIR_CRYSTAL,
    # Appended (keeps the ids above stable). CRYSTALS_PLACED = plaza gate (N crystals,
    # no Lute); the Crystals Placed event holds it. See EVENTS / REGION_RULES.
    CRYSTALS_PLACED,
    # Appended AGAIN (id stability): the four Fiend-defeated tokens, used only when
    # bonus_dungeon_crystals is on. Off, they are never granted/required, so seeds
    # stay byte-identical; these datapackage item-names are just never placed.
    EARTH_FIEND, FIRE_FIEND, WATER_FIEND, AIR_FIEND,
]

# ---- NPC story-event locations: REAL randomized AP item checks (not chests) ------
# (loc_name, npc_ordinal, region, [extra requirements]). The client detects these
# via durable story flags (see king-princess-events memory):
#   Princess -> u8[0x08D1153B] & 0x80   (Lute key-item bit -- fires when the player
#               normally receives the Lute from the princess).
# King was REMOVED: the bridge is always built now, so there is no King-reward
# event to check. Princess keeps ordinal 1 so existing loc/item IDs are unchanged.
#   Bikke    -> u8[0x08D1151C] & 0x20   (story-flag id5, set when Bikke is defeated
#               in Provoka -- the same bit that natively spawns the ship. The client
#               sends this check on the bit's rising edge, then gates the actual ship
#               on the randomized "Ship" AP item; see ship-bikke-flag memory).
#   Sage     -> Crescent Lake sage. Randomized NPC location, TALK-GATED with NO
#               prereq (2026-07-25): the check fires when the player talks to the giver
#               sage, detected via the field-dialog latch (client _npc_loop; see
#               sage-talk-detect memory). No Earth Crystal / Lich requirement -- the
#               location is in logic as soon as Crescent Lake is reachable, same as the
#               CL shop locations. (The native Canoe grant is still stripped when the
#               Canoe AP item is unowned; the Canoe is a separate randomized pool item.)
#   Levistone-> Ice Cave Levistone pickup. It is an EVENT PICKUP, NOT a numbered
#               treasure chest: grabbing it sets NO bit in the chest bitfield
#               (0x114EC..) -- so the phantom "chest" treasure_index 198 can never
#               be detected by the poll-based chest loop and MUST NOT be an AP chest
#               location (an item placed there would be unreachable). Instead the
#               Levistone SOURCE is a real NPC-style location detected on the
#               obtained-event bit 0x08D1151E & 0x10 (b4). Chest idx 198 is dropped
#               from the chest pool in __init__.create_regions; the Levistone item
#               it used to hold stays in the pool (found anywhere) and this location
#               re-balances it. INTERIM detector = the obtained-event bit while the
#               Levistone AP item is NOT owned (client strips the free native
#               Levistone possession+function bits + sends the check). Same not-owned
#               gate + strip pattern as the Sage/Canoe. See flag-collection-progress /
#               ap-keyitem-grant-infra memories.
#   Earth Rod  -> Sarda's Cave. Sarda natively hands over the Earth Rod (key-item bit
#               0x1153A b6) after the Vampire + Titan; obtained-event 0x1151D b7. Now a
#               randomized NPC location; the Earth Rod is a POOL item that gates the Lich
#               (Earth Crystal). Detector = 0x1151D b7 while the Earth Rod AP item is NOT
#               owned (client strips native possession + event bit + sends the check).
#   Chime      -> Lefein elder. Gives the Chime (0x1153A b4); obtained-event 0x1151F b7.
#               Now a randomized NPC location; Chime is a POOL item gating the Sky Castle
#               (Mirage Tower / Flying Fortress). Region = lefein ([AIRSHIP]).
#   Warp Cube  -> Waterfall Cavern robot. Gives the Warp Cube (0x1153A b2); obtained-event
#               0x11520 b0. Randomized NPC location; Warp Cube is a POOL item gating the
#               deep Sky Castle / Tiamat. Region = waterfall.
#   Bottled Faerie -> Onrac Caravan. Sells the Bottled Faerie (0x1153A b1); bought-event
#               0x11521 b2. CARAVAN DECISION (2026-07-06): the Caravan is a SPECIAL single-
#               item vendor, NOT one of the 18 standard town stores that shop_ap_offers
#               / rando.SHOP_AP_SLOTS target -- it has no shop-record slot in that infra, so
#               a shop-AP-slot would need a bespoke caravan-vendor RE and is not gen-safe.
#               Fell back to the poll-on-bottle-bought NPC-location model (pattern-consistent
#               with the Sage/Levistone). Bottled Faerie is a POOL item gating the Fairy.
#   Oxyale     -> Gaia Fairy (freed with the Bottled Faerie). fairy-release event 0x1151F
#               b1 (b1 and b2 both fire; b1 is the stable detector). Randomized NPC location
#               (extra req [BOTTLED_FAERIE]); Oxyale is a POOL item gating the Sea Shrine
#               (Kraken / Water Crystal). SEQUENCE RISK encoded in logic: Oxyale is only
#               reachable after the Bottled Faerie, which comes from the Caravan -- so the
#               Sunken Shrine (which needs Oxyale) stays reachable in the correct order.
# All five are POSSESSION-ONLY client grants (splits UNVERIFIED, absent from
# KEY_ITEM_FUNCTION_BITS) and share the Sage/Levistone WART (finding the AP item before
# the native spot misses the check). See ap-keyitem-grant-infra / flag-collection-progress.
#
# Classic-7 Mystic-Key trade chain (2026-07-06 batch): three MORE NPC-poll locations
# (ordinals 10-12), promoted from EVENTS. Unlike the Earth Rod / Chime / etc. above (which
# are detected on a separate obtained-EVENT bit), these three are detected exactly like the
# Princess/Lute and Sage/Canoe -- on the granted item's OWN POSSESSION bit + a not-owned
# gate (no separate event-bit capture; possession bits known for all 36 via key_item_bit):
#   Astos    -> Western Keep. Disguised NW "king" -> fight -> gives the Crystal Eye
#               (possession 0x1153B b5). Requires [CROWN] (the trade token you hand Astos).
#               Detector = the Crystal Eye possession bit while the Crystal Eye AP item is
#               NOT owned; client sends the check + strips the native possession bit.
#   Matoya   -> Matoya's Cave. Trade Crystal Eye -> get the Jolt Tonic (possession 0x1153B
#               b4). Requires [CRYSTAL_EYE]. Detector = the Jolt Tonic possession bit.
#   ElfPrince-> Elven Castle. Trade Jolt Tonic -> wake the Elf Prince -> get the Mystic Key
#               (possession 0x1153B b3). Requires [JOLT_TONIC]. Detector = the Mystic Key
#               possession bit. Mystic-Key-locked chest regions (LOC_INFO "MysticKey" token)
#               and the Cornelia Nitro room gate on the MYSTIC_KEY item NAME via set_rules.
# The linear chain Crown -> Astos -> Crystal Eye -> Matoya -> Jolt Tonic -> Elf Prince ->
# Mystic Key is encoded by these three extra-req lists; accessibility:full fill keeps it
# self-lock-free (each NPC's item is placed reachable given the prior chain items). Same
# POSSESSION-ONLY grant policy + not-owned strip + inherited WART as the batch above.
LEVISTONE_TREASURE_IDX = 198   # phantom (event-pickup) chest -> dropped, see above

# SINGLE SOURCE OF TRUTH for treasure indices that are NOT real chest locations.
# A "phantom" chest has an entry in data.LOCATIONS but is never created as an AP
# location (e.g. the Levistone -- an event pickup that sets no chest-open bit, so a
# poll-based chest check there can never fire). BOTH sides must agree on this set:
#   - apworld create_regions() skips these when building chest locations
#   - the client's ApClient._scout_locations() skips these when scouting
# If they ever disagree, the client scouts an id the seed doesn't own and the AP
# server closes the socket on that LocationScouts (mystery disconnect loop). The
# offline test_scout_parity.py enforces the two stay in lockstep.
# NOTE (2026-07-22 chest dedup): 24/131/132/133/138/141/185/188/189/191 were
# phantoms (no chest record in any map's OBJ_LIST_TABLE) but are now REAL
# locations -- iso_patcher.apply_chest_dedup (ON_DISC_ALWAYS, PATCHER 107)
# re-points the 10 physically-duplicated chest records (aliased indices
# 19/127/129/134/176/180) onto them, one unique index per physical chest.
PHANTOM_TREASURE_INDICES = frozenset({LEVISTONE_TREASURE_IDX})

# Every NORMALLY EMPTY chest: the ten physical chest records whose VANILLA
# treasure index is already owned by another chest on the same floor. One open
# bit per index means opening the alias source opens the twin too, so in vanilla
# the twin dispenses nothing, forever. iso_patcher repoints each to a unique
# previously-unused index, which is what makes it its own AP check.
#   24            Citadel of Trials (alias src 19)
#   131/132/133   Marsh Cave B2 south (alias srcs 127 / 129 / 129)
#   138/141       Marsh Cave B3       (alias src 134)
#   185           Mount Gulg B4       (alias src 176)
#   188/189/191   Mount Gulg B5       (alias src 180)
# LootInNormallyEmptyChests OFF leaves all ten vanilla: iso_patcher skips the
# dedup records and world._removed_chest_idx drops these indices, so the
# locations, the itempool and the client scout all shrink together. ON (default)
# they are ordinary AP chests.
NORMALLY_EMPTY_LOOT_INDICES = frozenset(
    {24, 131, 132, 133, 138, 141, 185, 188, 189, 191})

# LEGACY (pre-2026-08-12 seeds): the option used to be LootInGulgB5Chests and
# covered only the Mount Gulg B5 third of that set -- the other seven records
# were ON_DISC_ALWAYS. An old seed's slot_data still speaks that dialect, so the
# client keeps reading it (see removed_normally_empty_idx).
GULG_B5_LOOT_INDICES = frozenset({188, 189, 191})


def removed_normally_empty_idx(loot_in_normally_empty_chests,
                               legacy_gulg_b5=None) -> frozenset:
    """The normally-empty indices this seed does NOT own. Shared by
    create_regions, create_items and the client scout so all three skip the same
    set.

    Two dialects, because a seed carries its own:
      * NEW seeds set loot_in_normally_empty_chests -- all ten indices ride it.
      * OLD seeds set only loot_in_gulg_b5_chests; their other seven records
        were always baked, so only the Gulg B5 three can be missing.
    A seed predating BOTH flags was always deduped, so all-None reads as ON."""
    if loot_in_normally_empty_chests is not None:
        return (frozenset() if bool(loot_in_normally_empty_chests)
                else NORMALLY_EMPTY_LOOT_INDICES)
    if legacy_gulg_b5 is not None:
        return frozenset() if bool(legacy_gulg_b5) else GULG_B5_LOOT_INDICES
    return frozenset()
NPC_LOCATIONS = [
    ("Cornelia NPC: Princess",       1, "castle_cornelia_1f",    [GARLAND_DEFEATED]),
    ("Pravoka NPC: Bikke",           2, "chaos_shrine",          []),                # bridge always built -> free
    ("Crescent Lake NPC: Sage",      3, "crescent_lake",         []),                # talk-gated, no prereq (2026-07-25) -- same access as CL shops
    ("Ice Cave NPC: Levistone",      4, "cavern_of_ice_b2_room", []),               # event pickup (no chest bit)
    ("Sage's Cave NPC: Sarda",       5, "sardas_cave",           [VAMPIRE_DEFEATED, TITAN_FED]),  # Earth Rod
    ("Lefein NPC: Elder",            6, "lefein",                []),               # random AP item (native Chime stripped); region gates Airship+Rosetta(Translated)
    ("Waterfall Cavern NPC: Robot",  7, "waterfall",             []),               # Warp Cube
    ("Onrac NPC: Caravan",           8, "onrac_hub",             []),               # Bottled Faerie
    ("Gaia NPC: Fairy",              9, "gaia",                  [BOTTLED_FAERIE]),  # Oxyale; Gaia = airship-only
    ("Western Keep NPC: Astos",     10, "western_keep",          [CROWN]),           # Crystal Eye (trade Crown)
    ("Matoya's Cave NPC: Matoya",   11, "matoyas_cave",          [CRYSTAL_EYE]),     # Jolt Tonic (trade Crystal Eye)
    ("Elven Castle NPC: Elf Prince",12, "elven_castle",          [JOLT_TONIC]),      # Mystic Key (trade Jolt Tonic)
    # Adamantite = Flying Fortress event pickup (region = flying_fortress_1f, the
    # first real Sky-Castle region key -- there is no bare "flying_fortress" region).
    # No extra req (it is just a pickup once you reach the fortress).
    ("Flying Fortress NPC: Adamantite",13, "flying_fortress_1f", []),               # Adamantite (event pickup)
    # Dwarf Smith turn-in in the Dwarf cave (Mount Duergar, same region as Nerrick).
    # Requires the ADAMANTITE pool item (you hand it over to forge Excalibur).
    ("Mount Duergar NPC: Smith",    14, "mount_duergar",         [ADAMANTITE]),      # Excalibur (forge)
    # Bahamut class-change turn-in: hand him the Rat's Tail and he promotes the party
    # (native real promotion, crash-free) and sets detector 0x1151F b0. Bahamut is in
    # the Cardia islands (airship-gated dragon_caves region); requires the RATS_TAIL
    # pool item to turn in. Native reward is the promotion (no item), so the location
    # just grants whatever AP item is placed there.
    ("Dragon Caves NPC: Bahamut",   15, "dragon_caves_plains",   [RATS_TAIL]),       # class change (turn in Rat's Tail)
]

# NPC ordinal -> an extra OR-alternative that bypasses BOTH the region rule and the
# NPC's own extra tokens. Sarda: the airship lands right at the Sage's Cave, so the
# Vampire/Titan overland chain (Star Ruby -> Titan's Tunnel -> Giant's Cavern) is
# only one of two ways in -- the airship alone also reaches him.
NPC_ALT_RULES = {
    5: [AIRSHIP],       # Sage's Cave - Sarda
}


def npc_rule_alts(ordinal, region_rule, extra):
    """The NPC location's full access rule as OR-of-ANDs: (region alternative AND
    the NPC's extra tokens) for each region alternative, plus any NPC_ALT_RULES
    bypass alternative. Shared by __init__.set_rules and tracker.classify so the
    generator and the in-game tracker can never disagree."""
    alts = _and_all(_rule_alts(region_rule), list(extra))
    bypass = NPC_ALT_RULES.get(ordinal)
    if bypass:
        alts = alts + [list(bypass)]
    return _simplify(alts)

# ---- shop AP-stock locations: REAL randomized AP checks sold in town stores ------
# (shop_name, shop_index, [required tokens]). Shop indexes index rando.SHOP_AP_SLOTS
# (same order -- enforced by test_rando). With shop_ap_offers > 0, a shop
# lists several offers IN PARALLEL (rando.shop_offer_counts sizes each store, up
# to SHOP_MAX_OFFERS); offer k's location name is f"{shop_name} {k+1}" (see
# shop_location_name). Access = reach the town. Gil cost is NOT modeled in logic
# (money is always grindable, like other FF1 randomizers treat shops). Onrac/Gaia
# mirror the airship-tier hub the EVENTS graph already uses.
SHOP_LOCATIONS = [
    ("Cornelia Weapon Shop: AP Stock",       0, []),
    ("Cornelia Armor Shop: AP Stock",        1, []),
    ("Cornelia Item Shop: AP Stock",         2, []),
    ("Pravoka Weapon Shop: AP Stock",        3, []),
    ("Pravoka Armor Shop: AP Stock",         4, []),
    ("Pravoka Item Shop: AP Stock",          5, []),
    ("Elfheim Weapon Shop: AP Stock",        6, [SHIP]),
    ("Elfheim Armor Shop: AP Stock",         7, [SHIP]),
    ("Elfheim Item Shop: AP Stock",          8, [SHIP]),
    ("Melmond Weapon Shop: AP Stock",        9, [SHIP, CANAL]),
    ("Melmond Armor Shop: AP Stock",        10, [SHIP, CANAL]),
    ("Crescent Lake Weapon Shop: AP Stock", 11, [SHIP, CANAL]),
    ("Crescent Lake Armor Shop: AP Stock",  12, [SHIP, CANAL]),
    ("Crescent Lake Item Shop: AP Stock",   13, [SHIP, CANAL]),
    ("Onrac Item Shop: AP Stock",           14, [AIRSHIP, CANOE]),
    ("Gaia Weapon Shop: AP Stock",          15, [AIRSHIP]),
    ("Gaia Armor Shop: AP Stock",           16, [AIRSHIP]),
    ("Gaia Item Shop: AP Stock",            17, [AIRSHIP]),
]

# Max offers per shop the id space reserves (ids.SHOP_STRIDE leaves room for 8).
# ALL of them are registered in the datapackage regardless of how many a seed
# actually grants, so location ids never shift when the option changes.
SHOP_MAX_OFFERS = 6


def shop_location_name(shop_name, k):
    """AP location name for offer k (0-based) of a shop."""
    return f"{shop_name} {k + 1}"

# ---- region access: region -> access rule (BASE = both open-progression toggles
# off). A rule is either an AND-list of tokens (["Ship","Canal"]) OR an OR-of-ANDs
# (a list of token LISTS: [["Ship","Canal"], ["Canoe"]] = "either alternative").
# region_rules_for(early, extended) below applies the toggle deltas. A chest is
# reachable iff its region rule passes AND its per-chest token (MysticKey/TitanFed).
REGION_RULES = {
    # tier 0 — start, reachable on foot from Cornelia
    "chaos_shrine":            [],
    "castle_cornelia_1f":      [],                       # treasury chests carry MysticKey per-chest
    "matoyas_cave":            [],                        # bridge always built -> free

    # tier 1 — Ship (coastal landmasses near Elfland / Pravoka)
    "elven_castle":            [SHIP],
    "western_keep":            [SHIP],                    # RECON: Astos's keep
    "marsh_cave_b2_top":       [SHIP],
    "marsh_cave_b2_bottom":    [SHIP],
    "marsh_cave_b3":           [SHIP],
    "mount_duergar":           [SHIP],

    # tier 2 — Ship + Canal (Nerrick opens the inner sea): Melmond peninsula,
    # Cavern of Earth, Titan's Cave. CANAL alone is NOT enough now that early
    # progression can blow the canal on foot without a ship -- these sit across
    # water, so they also require the SHIP to actually sail there.
    "giants_cavern":           [[AIRSHIP, STAR_RUBY], [SHIP, CANAL, STAR_RUBY]],  # +TitanFed per-chest
    # Sarda's Cave sits BEYOND Titan's Tunnel -> Giant's Cavern OR a plain airship landing.
    "sardas_cave":             [[AIRSHIP], [SHIP, CANAL, STAR_RUBY]],
    "cavern_of_earth_b1":      [SHIP, CANAL],            # UPPER Cavern of Earth chests
    "cavern_of_earth_b2":      [SHIP, CANAL],
    "cavern_of_earth_b3":      [SHIP, CANAL],            # Vampire event (pre-Rod)
    "cavern_of_earth_b4":      [[AIRSHIP], [SHIP, CANAL]],  # Lich event (self-gates EARTH_ROD)
    # LOWER Earth Cavern chests sit past the mud gate: Earth Rod IN ADDITION to Ship+Canal.
    # (Kept separate from b4 so Sarda -- who HANDS OVER the Rod -- never requires it: no self-lock.)
    "cavern_of_earth_lower":   [SHIP, CANAL, EARTH_ROD],
    "crescent_lake":           [[AIRSHIP], [SHIP, CANAL]],  # +[CANOE] with early; sage AP loc

    # tier 3 — Canoe (rivers/lakes) reached by Ship. Canoe is a REAL pool item now,
    # so "has canoe" no longer implies you sailed to a river -- these need the SHIP
    # to reach the river mouth PLUS the CANOE to navigate. The early/extended toggles
    # are what drop the SHIP (a foot trail + river carve makes the canoe component
    # foot-reachable pre-ship; see region_rules_for).
    "mount_gulg_b2":           [[AIRSHIP], [SHIP, CANAL, CANOE]],
    "mount_gulg_b4_agama":     [[AIRSHIP], [SHIP, CANAL, CANOE]],
    "mount_gulg_b5":           [[AIRSHIP], [SHIP, CANAL, CANOE]],
    "cavern_of_ice_b1_backdoor": [[AIRSHIP], [CANOE]],
    "cavern_of_ice_b2_room":   [[AIRSHIP], [CANOE]],
    "cavern_of_ice_b3_treasury": [[AIRSHIP], [CANOE]],
    "citadel_of_trials_2f":    [[CROWN, CANOE, SHIP, CANAL], [CROWN, CANOE, AIRSHIP]],  # Crown + Canoe + (Ship+Canal OR Airship)
    "waterfall":               [AIRSHIP, CANOE],            # Airship + Canoe (Warp Cube robot)

    # tier 4a — Lefein (airship-tier town, home of the Chime AP location). Gated on the
    # translated Rosetta: the Lefein are unintelligible until Dr Unne translates the
    # Rosetta, so the Chime they hand over requires ROSETTA_TRANSLATED. That keeps the
    # Rosetta -> Dr Unne chain meaningful even though Mirage/Flying Fortress now gate on
    # the Chime itself rather than the Rosetta directly. Reachable by airship, solvable.
    "lefein":                  [AIRSHIP, ROSETTA_TRANSLATED],

    # tier 4 — Airship: open-plains / island dungeons. The Sky Castle (Mirage Tower +
    # Flying Fortress) requires the CHIME to enter (a real pool item; its Lefein source
    # is Rosetta-gated). The Flying Fortress additionally requires the WARP_CUBE on EVERY
    # floor (the Warp Cube warps you into the fortress from Mirage Tower's top). Both
    # sources are reachable before the Sky Castle, so the gate is solvable.
    "mirage_tower_1f":         [AIRSHIP, CHIME],              # Airship + Chime
    "mirage_tower_2f":         [AIRSHIP, CHIME],
    # NO CANOE TERM HERE, deliberately (2026-08-07): the fortress is Chime + Warp
    # Cube on top of mirage_desert access (Airship OR Ship+Canal+northern_docks).
    # The Waterfall robot -- whose already-gave gate collides with the cube's
    # function bit -- is NOT a logic prerequisite; the CLIENT must make a won cube
    # work without visiting him (see the Warp Cube row in
    # client/ff1_data.NPC_MAP_RESET). Adding CANOE here would import an access
    # requirement the game does not have.
    "flying_fortress_1f":      [AIRSHIP, CHIME, WARP_CUBE],   # Airship + Chime + Warp Cube
    "flying_fortress_2f":      [AIRSHIP, CHIME, WARP_CUBE],
    "flying_fortress_3f":      [AIRSHIP, CHIME, WARP_CUBE],
    "dragon_caves_plains":     [AIRSHIP],                # RECON: Cardia islands
    "dragon_caves_forest":     [AIRSHIP],                # RECON
    "dragon_caves_marsh":      [AIRSHIP],                # RECON

    # Onrac hub + the Caravan (Bottled Faerie). Both are ACCESS-NODE driven -- see
    # region_rules_for / access_nodes; the values here are just the no-toggle base.
    "onrac_hub":               [AIRSHIP],
    "onrac_caravan":           [AIRSHIP],
    # Melmond town (Dr Unne). Node-driven; base = Airship OR Ship+Canal.
    "melmond":                 [[AIRSHIP], [SHIP, CANAL]],
    # Gaia town (Fairy -> Oxyale) sits on airship-reachable plains -> Airship ALONE.
    # (Getting Oxyale from the Fairy still needs the Bottled Faerie, whose Onrac source
    # is Airship+Canoe -- so Oxyale is effectively Airship+Canoe, per the Fairy's req.)
    "gaia":                    [AIRSHIP],

    # tier 5 — Sunken Shrine = Onrac access + Oxyale (node-driven; access_nodes).
    "sunken_shrine_2f_sharknado": [AIRSHIP, OXYALE],
    "sunken_shrine_3f_split":  [AIRSHIP, OXYALE],
    "sunken_shrine_3f_vertical": [AIRSHIP, OXYALE],
    "sunken_shrine_4f_tfc":    [AIRSHIP, OXYALE],
    "sunken_shrine_5f":        [AIRSHIP, OXYALE],

    # endgame — revisited Chaos Shrine. The 3F plaza opens once crystals_needed
    # crystals are placed on the altars (Lute NOT required); the basement additionally
    # needs the Lute. CRYSTALS_PLACED is the N-of-4 count gate (set_rules mirrors the
    # on-disc wrapper; tracker.resolve_tokens mirrors it client-side).
    "chaos_shrine_3f_plaza":   [CRYSTALS_PLACED],
    "chaos_shrine_b2":         [CRYSTALS_PLACED, LUTE],
    "chaos_shrine_b4":         [CRYSTALS_PLACED, LUTE],
}

# Fallback rule for chests whose LOC_INFO region is None. Only 5 indices lack a
# region today: 198 (phantom Levistone, always dropped from the chest pool) and
# 252-255 (static DLC boss chests, re-gated per-dungeon via DLC_STATIC_IDX_DUNGEON
# below) -- so no live location actually keeps this rule; it is a safety net for
# any future unmapped index.
UNMAPPED_RULE = [AIRSHIP]

# ---- Soul-of-Chaos bonus dungeons: DYNAMIC (procedural) chest locations ----
# The four elemental bonus dungeons regenerate their layout every entry, so their
# procedural chests have NO static treasure index. We add "the first X chests opened
# in dungeon D" as counter locations (ids.dyn_chest_loc_id): the client arms a
# map-gated chest bp, and for the first cap opens strips the native loot + delivers
# the AP item; open cap+1.. behaves vanilla. See the bonus-dungeon-chests memory.
#   (dungeon index, display name, floors, default cap, gate token, yaml option attr)
# floors = canonical Soul-of-Chaos size = how many ordinals __init__ registers in
# the datapackage (the yaml Range option's own range_end is a separate ceiling).
# Each dungeon is sealed by its element's Fiend statue, so access is gated on that
# element's CRYSTAL token (granted by the Fiend event) -- NOT the airship, so a
# northern-docks / no-airship route that still earns the crystal reaches the dungeon.
# The per-dungeon count of dynamic chests that become AP checks is set directly by the
# dungeon's yaml Range option (default cap); exclude_bonus_dungeons forces every cap to 0.
BONUS_DUNGEONS = [
    (0, "Earthgift Shrine",   5,  5,  EARTH_CRYSTAL, "earthgift_ap_locations"),
    (1, "Hellfire Chasm",     10, 10, FIRE_CRYSTAL,  "hellfire_ap_locations"),
    (2, "Lifespring Grotto",  20, 15, WATER_CRYSTAL, "lifespring_ap_locations"),
    (3, "Whisperwind Cove",   40, 25, AIR_CRYSTAL,   "whisperwind_ap_locations"),
]

# Extra AND-token requirements per dungeon (dg index), on top of the crystal gate
# above. Vehicles / key items each dungeon's entrance actually needs:
#   Hellfire Chasm   -> Airship
#   Lifespring Grotto-> Ship + Nitro Powder
#   Whisperwind Cove -> Canoe, PLUS Ice Cave access (see ICE_CAVE_ACCESS_ALTS below;
#                        that piece is an OR, so it is NOT folded into this AND list --
#                        __init__.create_regions() builds it via make_rule's region_rule
#                        (OR-of-ANDs) param, not this extra list).
DLC_DUNGEON_EXTRA_TOKENS = {
    0: [],                 # Earthgift: Earth Crystal only
    1: [AIRSHIP],          # Hellfire
    2: [SHIP, CANAL],      # Lifespring: Canal + Ship + Water Crystal
    3: [CANOE],            # Whisperwind: + the OR alt below
}

# Whisperwind Cove also requires reaching the Ice Cave. Ice Cave access is early
# (foot/canoe river, see region_rules_for) OR Ship + Nitro Powder. Expressed as
# OR-of-AND alternatives for make_rule's region_rule param; the early alt is only
# valid when the early_open_progression yaml toggle is on (computed by the caller).
WHISPERWIND_SHIP_CANAL_ALT = [SHIP, CANAL]

# The 4 STATIC DLC boss-chamber loot chests (one-time rewards behind Ahriman /
# Rubicante / Shinryu / Death Gaze) occupy treasure indices 252..255 -- the keys of
# DLC_STATIC_IDX_DUNGEON below. exclude_bonus_dungeons REMOVES these AP locations
# (see __init__._removed_chest_idx). Treasure indices 256..267 are NOT among them:
# those are the Labyrinth of Time PUZZLE chests, which gen_apdata.LABYRINTH_DROP
# strips from data.py entirely (never AP locations at all, regardless of the
# exclude option), so nothing downstream ever sees them.
#
# Live-swept static bonus chest -> its dungeon (dg index into BONUS_DUNGEONS).
# A mapped idx is gated EXACTLY like that dungeon's dynamic chests (element
# crystal + DLC_DUNGEON_EXTRA_TOKENS, + the Whisperwind Ice-Cave alt) instead of
# the [AIRSHIP] fallback — the fallback UNDER-gates (airship alone never grants
# the crystal), a broken-seed risk if progression lands there. Display name in
# re_only/chest_names.csv (keep the two in sync; regen data.py after editing).
#   252 = Earthgift Shrine's only static chest (live playtest 2026-07-19).
DLC_STATIC_IDX_DUNGEON = {
    252: 0,   # Earthgift Shrine - Chest behind Ahriman
    253: 1,   # Hellfire Chasm - Chest behind Rubicante (live 2026-07-20)
    254: 2,   # Lifespring Grotto - Chest behind Shinryu (live 2026-07-22)
    255: 3,   # Whisperwind Cove - Chest behind Death Gaze, floor 40 (live 2026-07-21)
}


def removed_static_dlc_idx(dyn_caps, exclude_dlc=False):
    """Static DLC boss chests (252-255) that are NOT AP locations this seed.

    Each static boss-chamber chest is tied to its dungeon's dynamic AP count: it is
    an AP location only if that dungeon contributes >= 1 dynamic AP chest. So a
    dungeon whose *_ap_locations is 0 drops its static chest too (it would otherwise
    strand a check the player is told to skip), and exclude_bonus_dungeons -- which
    zeroes every count -- drops all four. Evaluated PER DUNGEON: lifespring=3 keeps
    the Lifespring static while whisperwind=0 drops the Whisperwind static.

    `dyn_caps` is {dungeon_idx: cap} (from _dyn_caps / slot_data bonus_dyn_caps).
    Shared by __init__._removed_chest_idx and the client scout so both agree
    (test_scout_parity enforces)."""
    out = set()
    for idx, dg in DLC_STATIC_IDX_DUNGEON.items():
        if exclude_dlc or int(dyn_caps.get(dg, 0)) == 0:
            out.add(idx)
    return out


def dyn_chest_location_name(dungeon_name, ordinal0):
    """Display name for a dynamic bonus-dungeon chest location (1-based ordinal)."""
    return f"{dungeon_name} - Dynamic Chest {ordinal0 + 1}"

# ---- early / extended open-progression: toggle-dependent region + shop rules ----
# Two INDEPENDENT yaml toggles (options.py). Each carves overworld foot trails + one
# canoe river (client _openworld_loop / re_only/gen_openworld.py) and lowers the
# matching access rules. The canal is NO LONGER pre-opened -- it is blown normally,
# so CANAL is not precollected and the inner-sea regions keep [SHIP, CANAL].
#
#   early_open_progression (default on):
#     - Cornelia -> Mount Duergar foot trail  => mount_duergar reachable on foot.
#     - Gulg/Crescent-lake <-> Ice-Cavern canoe river => the Gulg + Ice canoe regions
#       and Crescent Lake are reachable with just the CANOE (pre-ship).
#   extended_open_progression (default off):
#     - Mount Duergar -> Western Keep FOOT trail (no canal needed). IFF early is also on,
#       Cornelia->Duergar (early pass) chains into it, so WK / Elven / Marsh (and Elfland)
#       are reachable at START on foot -- ship-free and canal-free.
#     - Duergar <-> Melmond canoe river => Melmond / Cavern of Earth / Titan reachable via
#       (reach-Duergar + CANOE) as an alternative to Ship + Canal. Reach-Duergar = foot
#       (early) or Ship, so the canoe alt is [CANOE] with early, [SHIP, CANOE] without.
# Independence note: extended alone (no early) still requires the SHIP to reach Duergar
# (the WK trail's and Melmond river's north end), so extended-solo needs Ship for its
# shortcuts -- it never opens start-access on its own.

# Regions the AIRSHIP can also reach: it lands on flat land near them, so once you
# have the airship you no longer need the Ship (or the Ship+Canal). Each value is
# the airship alternative's AND-tokens -- usually just [AIRSHIP], but Lower Cavern
# of Earth still needs the Earth Rod to pass the mud gate on the airship route too.
# Applied ON TOP of the toggle-adjusted rules (see region_rules_for) so it composes
# with early/extended/northern_docks. (2026-07-17, per player: airship should open
# these Ship-gated spots the way the NES randomizer allows.)
AIRSHIP_REACHES = {
    "elven_castle":          [AIRSHIP],
    "western_keep":          [AIRSHIP],
    "marsh_cave_b2_top":     [AIRSHIP],
    "marsh_cave_b2_bottom":  [AIRSHIP],
    "marsh_cave_b3":         [AIRSHIP],
    "cavern_of_earth_b1":    [AIRSHIP],
    "cavern_of_earth_b2":    [AIRSHIP],
    "cavern_of_earth_b3":    [AIRSHIP],
    "cavern_of_earth_b4":    [AIRSHIP],
    "cavern_of_earth_lower": [AIRSHIP, EARTH_ROD],
}


def _and_all(alts, extra):
    """AND `extra` onto every OR-alternative of `alts` (dedup within each)."""
    out = []
    for a in alts:
        merged = list(a) + [t for t in extra if t not in a]
        out.append(merged)
    return out


def _simplify(alts):
    """Drop OR-alternatives that can never be the cheapest path: any alternative
    that is a strict SUPERSET of another (needing everything it needs plus more),
    plus exact duplicates. Pure simplification -- the satisfied/not-satisfied
    verdict is identical, the rule just reads cleanly after nodes compose."""
    out, seen = [], []
    for a in alts:
        s = set(a)
        if any(set(b) < s for b in alts):
            continue                      # a strictly harder path exists
        if s in seen:
            continue
        seen.append(s)
        out.append(list(a))
    return out or [[]]


def access_nodes(early: bool, extended: bool, northern_docks: bool = False) -> dict:
    """The NAMED access nodes the region rules are built from.

    Each value is an OR-of-AND alternative list. Naming the shared prefixes means
    a downstream area states only what it ADDS ("Flying Fortress = Mirage Tower +
    Warp Cube") instead of restating the whole vehicle chain, so the two can never
    drift apart. The yaml toggles are generation-time options, so they select which
    alternatives EXIST rather than appearing as tokens.
    """
    # Mt. Duergar access = early_open_progression OR Ship
    duergar = [[]] if early else [[SHIP]]
    # The canal itself is an EVENT (Nerrick): Mt. Duergar access + Nitro Powder.
    inner_sea = [[SHIP, CANAL]]          # sailing the inner sea
    # Melmond = Airship OR (Ship+Canal) OR (extended + Duergar access + Canoe)
    melmond = [[AIRSHIP], [SHIP, CANAL]]
    if extended:
        melmond += _and_all(duergar, [CANOE])
    # northern_docks adds a Ship+Canal route to the northern continents.
    docked = [[SHIP, CANAL]] if northern_docks else []
    onrac = [[AIRSHIP]] + docked                      # Onrac access
    caravan = [[AIRSHIP]] + _and_all(docked, [CANOE])  # Caravan wants the canoe by sea
    mirage_desert = [[AIRSHIP]] + docked
    mirage_tower = _and_all(mirage_desert, [CHIME])
    flying_fortress = _and_all(mirage_tower, [WARP_CUBE])
    # Giant's Cavern (Titan's Tunnel) = Melmond access + Star Ruby -- you need the
    # Ruby to get past Titan at all. Sarda's Cave sits BEYOND the tunnel, so it is
    # Giant's Cavern OR a straight airship landing.
    giants = _and_all(melmond, [STAR_RUBY])
    sardas = _simplify(giants + [[AIRSHIP]])
    # Crescent Lake = Ship+Canal OR (early + Canoe) OR Airship
    crescent = [[AIRSHIP], [SHIP, CANAL]] + ([[CANOE]] if early else [])
    # Mount Gulg = Airship OR (Crescent Lake + Canoe); Ice Cave = Airship OR Canoe
    gulg = _simplify([[AIRSHIP]] + _and_all(crescent, [CANOE]))
    ice_cave = [[AIRSHIP], [CANOE]]
    return {
        "duergar": duergar, "inner_sea": inner_sea, "melmond": melmond,
        "onrac": onrac, "caravan": caravan, "mirage_desert": mirage_desert,
        "mirage_tower": mirage_tower, "flying_fortress": flying_fortress,
        "crescent": crescent, "gulg": gulg, "ice_cave": ice_cave,
        "giants": giants, "sardas": sardas,
    }


def region_rules_for(early: bool, extended: bool, northern_docks: bool = False) -> dict:
    """Effective region rules for the open-world toggles. Fresh dict;
    values are AND-lists or OR-of-AND-lists (see set_rules access())."""
    r = {k: list(v) for k, v in REGION_RULES.items()}
    n = access_nodes(early, extended, northern_docks)

    # --- node-driven regions (see access_nodes) ---
    r["mount_duergar"] = n["duergar"]
    r["melmond"] = n["melmond"]
    # Cavern of Earth Upper = Melmond access; Lower = Upper + Earth Rod.
    for k in ("cavern_of_earth_b1", "cavern_of_earth_b2", "cavern_of_earth_b3",
              "cavern_of_earth_b4"):
        r[k] = n["melmond"]
    r["cavern_of_earth_lower"] = _and_all(n["melmond"], [EARTH_ROD])
    r["giants_cavern"] = n["giants"]
    r["sardas_cave"] = n["sardas"]
    r["onrac_hub"] = n["onrac"]
    r["onrac_caravan"] = n["caravan"]
    # Sunken Shrine = Onrac access + Oxyale (you still need to breathe down there).
    # Waterfall = Onrac access + Canoe.
    for k in ("sunken_shrine_2f_sharknado", "sunken_shrine_3f_split",
              "sunken_shrine_3f_vertical", "sunken_shrine_4f_tfc",
              "sunken_shrine_5f"):
        r[k] = _and_all(n["onrac"], [OXYALE])
    r["waterfall"] = _and_all(n["onrac"], [CANOE])
    # Crescent Lake / Mount Gulg / Cavern of Ice (see access_nodes).
    r["crescent_lake"] = n["crescent"]
    for k in ("mount_gulg_b2", "mount_gulg_b4_agama", "mount_gulg_b5"):
        r[k] = n["gulg"]
    for k in ("cavern_of_ice_b1_backdoor", "cavern_of_ice_b2_room",
              "cavern_of_ice_b3_treasury"):
        r[k] = n["ice_cave"]
    for k in ("mirage_tower_1f", "mirage_tower_2f"):
        r[k] = n["mirage_tower"]
    for k in ("flying_fortress_1f", "flying_fortress_2f", "flying_fortress_3f"):
        r[k] = n["flying_fortress"]
    # Citadel = Crown + Canoe + (Airship OR Ship+Canal)
    r["citadel_of_trials_2f"] = _and_all([[AIRSHIP], [SHIP, CANAL]], [CROWN, CANOE])

    if early:
        # early carves the Cornelia->Duergar foot pass and the canal is walkable
        # regardless of Nitro, so the whole Elfland landmass (Western Keep, Elven
        # Castle, Marsh Cave) is reachable with just the CANOE -- ship-free. Added
        # as an OR alternative to the base [SHIP]; the airship alt is folded in
        # below, and extended (if also on) upgrades these to free foot access.
        for k in ("western_keep", "elven_castle", "marsh_cave_b2_top",
                  "marsh_cave_b2_bottom", "marsh_cave_b3"):
            r[k] = _rule_alts(r[k]) + [[CANOE]]

    if extended:
        if early:
            # Duergar->WK is a plain FOOT trail (no canal needed). With early also on,
            # Cornelia->Duergar (early pass) chains into Duergar->WK (extended trail), so
            # you walk there from the START, ship-free and canal-free -> WK / Elven / Marsh
            # are reachable at start. (Only meaningful WITH early, since reaching Duergar
            # on foot is what the early pass provides.)
            for k in ("western_keep", "elven_castle", "marsh_cave_b2_top",
                      "marsh_cave_b2_bottom", "marsh_cave_b3"):
                r[k] = []
    # Airship also reaches these areas -> OR in its alternative on top of whatever
    # the toggles produced. A region already reachable ([]) stays [] (the airship
    # alt is harmlessly redundant); everything else gains "or by airship".
    for region, air_alt in AIRSHIP_REACHES.items():
        if region not in r:
            continue
        alts = _rule_alts(r[region])
        if air_alt not in alts:
            r[region] = alts + [list(air_alt)]
    # Collapse alternatives that composing nodes made redundant (a path needing
    # everything another needs plus more). Semantics unchanged; rules read cleanly.
    return {k: _simplify(_rule_alts(v)) for k, v in r.items()}


# (RETIRED 2026-07-27: LATE_TOKENS / is_late_rule / late_regions, the "is this
# region airship-or-endgame-only" predicates that existed solely to drive the
# late_activatable_equipment placement gate. Superseded by equipment_runes, which
# gates activation in-game instead of placement. _rule_alts below stays -- it is
# also the normalizer region_rules_for uses.)


def _rule_alts(rule):
    """Normalize a region rule to a list of AND-alternatives (list of token lists).
    A flat token list is one alternative; a list of lists is OR-of-ANDs; an empty
    rule is a single always-true (start-reachable) alternative. Mirrors
    __init__.set_rules.as_alts."""
    if not rule:
        return [[]]
    if isinstance(rule[0], (list, tuple)):
        return [list(a) for a in rule]
    return [list(rule)]


# Every AP shop sells inside a town, so its access rule IS that town's access rule.
# shop name prefix -> the region whose rule it inherits ("" = free from the start).
SHOP_TOWN_REGION = {
    "Cornelia":      None,             # start town
    "Pravoka":       None,             # bridge always built -> free
    "Elfheim":       "elven_castle",
    "Melmond":       "melmond",
    "Crescent Lake": "crescent_lake",
    "Onrac":         "onrac_hub",
    "Gaia":          "gaia",
}


def shop_rules_for(early: bool, extended: bool, northern_docks: bool = False) -> dict:
    """Shop-name -> access rule. Every item shop simply inherits the rule of the
    town it stands in (SHOP_TOWN_REGION), so a shop can never disagree with its
    town's chests."""
    r = region_rules_for(early, extended, northern_docks)
    o = {}
    for nm, _shop, _reqs in SHOP_LOCATIONS:
        town = nm.split(" Weapon")[0].split(" Armor")[0].split(" Item")[0]
        region = SHOP_TOWN_REGION.get(town, "__missing__")
        if region is None:
            o[nm] = []
        elif region in r:
            o[nm] = r[region]
    return o

# ---- event graph: (event_name, region, granted_token, [extra requirements]) --
# Reachability of an event = its region rule passes AND extra reqs held.
# Order doesn't matter; AP resolves the fixpoint.
EVENTS = [
    # opening: Garland defeated still gates story progress. The bridge is ALWAYS
    # built (the runtime client forces the overworld bridge bit every game), so the
    # BRIDGE token was removed outright (2026-07-17) -- there is no "King's Bridge"
    # event and no King check (nothing left to detect). The Princess IS in the
    # graph: "Cornelia - Princess" is a real randomized NPC location (NPC_LOCATIONS
    # ordinal 1, detected via the Lute key-item bit), and the LUTE is a REAL pool
    # item (or assembled from Lute Tablets) that the endgame Black-Orb rule gates
    # on via state.has.
    ("Chaos Shrine - Garland",      "chaos_shrine",        GARLAND_DEFEATED, []),

    # Pravoka -> Bikke is now a REAL randomized NPC location (NPC_LOCATIONS), and the
    # SHIP is a REAL pool item -- neither is an event token anymore.

    # Mystic Key chain: Crown -> Astos -> Crystal Eye -> Matoya -> Jolt Tonic -> Elf
    # Prince -> Mystic Key. All three grantor NPCs (Astos / Matoya / Elf Prince) are now
    # REAL randomized NPC locations (NPC_LOCATIONS ordinals 10-12) that hand out a random
    # AP item, and CRYSTAL_EYE / JOLT_TONIC / MYSTIC_KEY are REAL pool items -- so there
    # are no Astos / Matoya / Elf-Prince EVENTS anymore. The Mystic-Key-locked chests
    # gate on the MYSTIC_KEY item NAME (LOC_INFO "MysticKey" token, set_rules); the trade
    # chain lives in those NPC locations' extra reqs (Astos:[CROWN], Matoya:[CRYSTAL_EYE],
    # ElfPrince:[JOLT_TONIC]).

    # Canal: Nitro Powder -> Nerrick
    ("Mount Duergar - Nerrick",     "mount_duergar",       CANAL,   [NITRO]),

    # Earth: Vampire -> Star Ruby feeds Titan -> Sarda's Rod -> Lich -> Earth Crystal.
    # Star Ruby is a chest pool item (gates Titan); the Earth Rod is now a REAL pool
    # item handed out by the "Sage's Cave - Sarda" NPC location (NPC_LOCATIONS), NOT an
    # event token -- so there is no Sarda EVENT anymore. The Lich still gates on the
    # EARTH_ROD item name via state.has.
    ("Cavern of Earth - Vampire",   "cavern_of_earth_b3",  VAMPIRE_DEFEATED, []),
    ("Giant's Cave - Titan",        "giants_cavern",       TITAN_FED, [STAR_RUBY]),
    ("Cavern of Earth - Lich",      "cavern_of_earth_b4",  EARTH_CRYSTAL, [EARTH_ROD]),

    # Canoe is a REAL pool item now (found anywhere), NOT granted by an event. The
    # Crescent Lake sage is a randomized NPC location (NPC_LOCATIONS) that hands over
    # a random AP item instead of the canoe.

    # Fire: Kary
    ("Mount Gulg - Kary",           "mount_gulg_b5",       FIRE_CRYSTAL, []),

    # Airship: Levistone raised in the desert
    ("Ryukhan Desert - Airship",    "cavern_of_ice_b2_room", AIRSHIP, [LEVISTONE]),  # RECON: desert reachable once canoe/ice tier

    # Water: Bottled Faerie -> Oxyale -> Kraken (NO submarine in FF1 PSP). The Onrac /
    # Gaia hub is Airship + Canoe (onrac_hub). The Bottled Faerie (Caravan) and Oxyale
    # (Fairy) are now REAL pool items handed out by the "Onrac - Caravan" and "Gaia -
    # Fairy" NPC locations (NPC_LOCATIONS), NOT event tokens -- so there are no Caravan
    # / Fairy EVENTS anymore. The Sea Shrine still gates on the OXYALE item name (region
    # rules); the Fairy NPC location still requires the BOTTLED_FAERIE item, preserving
    # the Caravan -> Bottled Faerie -> Fairy -> Oxyale -> Sea Shrine sequence.
    ("Sunken Shrine - Kraken",      "sunken_shrine_5f",    WATER_CRYSTAL, []),

    # Air: Rosetta Stone -> Dr Unne -> Lefein assist -> Tiamat
    ("Melmond - Dr Unne",           "melmond",             ROSETTA_TRANSLATED, [ROSETTA]),
    ("Flying Fortress - Tiamat",    "flying_fortress_3f",  AIR_CRYSTAL, []),

    # endgame — crystals_needed crystals placed opens the 3F plaza (no Lute). set_rules
    # swaps the 4-crystal AND for an N-of-4 count rule, exactly like the Black Orb below.
    ("Chaos Shrine - Crystals Placed", "chaos_shrine",     CRYSTALS_PLACED,
        [EARTH_CRYSTAL, FIRE_CRYSTAL, WATER_CRYSTAL, AIR_CRYSTAL]),
    ("Chaos Shrine - Black Orb",    "chaos_shrine",        BLACK_ORB_DESTROYED,
        [EARTH_CRYSTAL, FIRE_CRYSTAL, WATER_CRYSTAL, AIR_CRYSTAL, LUTE]),
]
