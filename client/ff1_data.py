"""
FF1 PSP (ULUS10251) reverse-engineered data tables and RAM map.
Built live via PPSSPP debugger + ppsspp_scan.py. Feeds the future apworld.
"""

# --- RAM map (CANONICAL save-block layout; base moves between sessions!) ---
# The 0x08D1xxxx save/game-state block is HEAP-ALLOCATED: observed base deltas
# 0, +0x1000, +0x4000 vs this canonical layout (save-block-address-shift
# memory). The game keeps a static pointer to the live struct; the client
# resolves the session delta from it once and offsets EVERY 0x08D1xxxx
# read/write (ApClient.sa()). Found 2026-07-02 by locating the live block
# (gil/party scan) and scanning the module image for pointers into it.
#
# NAMING CONTRACT: every constant or helper that yields an address in this
# relocatable arena carries a `_SA` / `_sa` suffix. Any such name used as the
# address argument to psp.read*/write* MUST be lexically wrapped in self.sa().
# test_client_policies.py (policy P1) enforces this against ApClient.py so a
# forgotten sa() fails CI instead of silently reading dead RAM at delta != 0
# (the 2026-07-03 thief-steal bug). New arena address -> give it an `_SA`
# suffix. One-off manual-delta sites suppress with a trailing `# sa-ok`.
SAVE_BLOCK_PTR = 0x089D7AD8        # u32 runtime global: -> live save struct
SAVE_BLOCK_PTR_CANON = 0x08D11100  # its value under the canonical layout
GIL_ADDR_SA = 0x08D12264          # u32
GIL_MAX = 999_999                 # game cap (6-digit display; over-cap shows the
                                  # LOW 6 digits, e.g. 1_003_399 -> "003399")
INVENTORY_BASE_SA = 0x08D12034    # start of packed consumable records

# --- Archipelago received-items counter (save-resident; "Opt A" item delivery) ---
# A u32 WE own, stored INSIDE the save struct so it rolls back together with the
# save on death/load. It holds the count of AP received-items already granted (the
# next absolute position to grant). Keeping this counter OUTSIDE the save (the old
# received_count JSON) was the item-loss bug: on death the item rolls back but an
# external counter does not, so the re-grant is skipped forever. In the save, item
# and counter roll back atomically, so _grant_loop re-grants exactly the lost items.
#
# REQUIREMENTS for this slot (ALL must hold, or you get silent item loss / save
# corruption): (a) 0 on a fresh new-game save, (b) never read/written by the game
# itself, (c) serialized into the .sav. VERIFIED LIVE 2026-06-27 via the save->load
# probe: 0x11560 sits deep in a 996-byte zero run (0x11558..0x1193b), is restored
# from the in-game save on load (serialized), stays put during play (game ignores
# it), and its u32 window + neighbors are zero (free). NOTE: the earlier candidate
# 0x1153C FAILED -- it is scratch RAM the game zeroes on load (NOT serialized),
# even though it is adjacent to the serialized key-item field 0x11537..0x1153B.
# RELOCATED 2026-07-31 (v193, was 0x08D11560 = save+0x460): the old home sat
# inside the native rolling map-record list (see the slot_magic block below --
# the vanilla endgame save holds a full record at save+0x460..0x46F, and the
# 2026-06-27 "VERIFIED free" was the same flawed zero-snapshot evidence).
# Client-only (no cave touches it), so the move is one constant. u32 LE.
RECEIVED_COUNTER_ADDR_SA = 0x08D11908   # save+0x808, just below the spent array

# --- slot_magic: per-spell-level SPENT-charge array (save-resident) ----------
# 4 chars x 8 spell levels of u8 "charges SPENT". Chosen as SPENT (not
# remaining) deliberately: the zero state = nothing spent = full charges,
# so a fresh new game needs NO init hook, level-ups need NO grant hook (max is
# derived from class+level at read time and spent is unchanged), and save/load
# rolls charges back atomically with everything else. current = max - spent
# (clamped >= 0). Caves reach it via *SAVE_BLOCK_PTR + SPELL_SLOTS_SPENT_OFF
# like the rune-gate flag read, so the moving save base is a non-issue.
#
# RELOCATED 2026-07-31 (v193): the original home 0x464..0x48F sat in the
# "verified 996-byte zero run" -- which turned out to be a NATIVE bounded
# rolling list of per-visited-map return records ({u32 flag, u16 x, u16 y,
# u32 dir, u32 map_id}, 16B stride, growing up from save+0x440; live-caught
# overwriting Arus's spent row with record coordinates). Observed high-water
# save+0x4E8 (mid-game) / 0x4B8 (endgame slot 20); the list REUSES entries,
# it does not grow monotonically. New home is flush against the TOP of the
# v188 preview-copy window (fill-fn copy #9 spans save+0x43C..0x840): a
# native word lives at save+0x83C and BONUS_FLOOR_TABLE_SA claims 0x840, so
# the block ends at 0x83B exactly. ~790 bytes (~49 records) of headroom
# above the rolling list's observed reach. Layout (save-relative):
#   0x808..0x80B  AP received-items counter u32 (RECEIVED_COUNTER_ADDR_SA)
#   0x80C..0x82B  spent u8[4][8]
#   0x82C..0x82F  CW damage-point pool u8[4]   (iso_patcher _SM_CW_POOL_OFF)
#   0x830..0x833  Soma Drop counts u8[4]       (iso_patcher _SM_SOMA_OFF)
#   0x834         bonus_dungeon_crystals shadow bits 0-3 (BONUS_CRYSTAL_SHADOW_ADDR);
#                 0x835..0x837 still reserved (was the v183 INT-accrual s16[4])
#   0x838         save-file marker 0x5A        (no longer overlaps acc[2]!)
#   0x839..0x83B  guard pad, must stay zero
# Zero-in-two-saves is necessary-not-sufficient (that exact mistake caused
# the collision) -- the client's slotbox loop canaries the guard band below
# the block (save+0x7EC..0x807, covering the two record starts 0x7F0/0x800)
# and the pad, and alarms loudly if native data ever intrudes.
SPELL_SLOTS_SPENT_BASE_SA = 0x08D1190C  # u8[4][8]; char*8 + (spell_level-1)
SPELL_SLOTS_PER_CHAR = 8                # spell levels per character = row stride
# slot_magic save-file MARKER: stamped 0x5A by the client while a slot_magic
# seed plays; rides into every save via the save->slot preview copy (fill fn
# 0x08825CE0 copy #9, save+0x43C..0x840 -> slot+0x71C), letting the save/load
# file-select show "Magic N" for slot files and vanilla "MP n" for mana files
# PER FILE. Cleared by _reset_slotmagic_state on NG+ (re-stamped seconds
# later by the slotbox loop).
# v194: per-character Soma Drop count. Under slot_magic a Soma Drop raises the
# level-1 spell slot count, spilling up to the next level below 9 once level 1
# is full; the whole distribution stays DERIVED from (natural table, this
# count), so this is the only byte the drop writes. Saturates at 72 (8x9).
SPELL_SLOTS_CWPOOL_SA = 0x08D1192C      # u8[4] Crimson Wizard damage pool
SPELL_SLOTS_SOMA_SA = 0x08D11930        # u8[4], one per party row
SPELL_SLOTS_SOMA_MAX = 72
# v220: ALL THREE of the arrays above are indexed by party ROW, but a Formation
# swap MOVES the character records between rows -- so charges/pool/soma stuck to
# the SLOT, not the caster (reported 2026-08-04: a Red Wizard's available charges
# changed when the party was reordered). Fixed ON-DISC by iso_patcher's
# _sm_formswap_cave, which swaps all three in the same breath as the records.
# NOT fixed from here: a client-side permute lands a tick late (the menu holds
# its rendered row until reopened) AND would re-apply the cave's swap, undoing
# it. Per-character identity byte, if ever needed again for RE:
# PARTY_BASE_SA - 4 + row*0x5C (same "-2 class array" family as CLASS_BASE_SA;
# unique across the four rows and travels with the character).
SPELL_SLOTS_MARKER_SA = 0x08D11938      # u8; 0x5A = slot-magic save
SPELL_SLOTS_MARKER_VALUE = 0x5A

# --- AP INIT MARKER: "this save block has already been through client init" ---
# The one byte that separates a NEW GAME from a LOADED SAVE. Claimed 2026-08-17
# out of the reserved save+0x835..0x837 run documented above (was the v183 INT
# accrual; nothing has used it since). Client-only, no cave reads it.
#
# WHY THIS WORKS -- the property was MEASURED, not assumed. Right after a New
# Game started from the title following a load of a save whose AP counter was 8,
# the whole AP region read zero:
#     received counter save+0x808 = 0
#     slot_magic block save+0x80C..0x83B = all zero
# So the game's new-game init ZEROES this region, while a load RESTORES it from
# the file. That is exactly the asymmetry every state heuristic lacked: a fresh
# party, an untouched purse and an unopened chest set are all equally true of a
# save file the player made before their first battle (the 2026-08-17 bug: the
# jobs one-shot re-stamped a reordered party, so "the White Mage knows Firaga").
#
# NG+ IS THE ONE EXCEPTION. A New Game from a BEATEN save copies the cleared save
# in for bestiary carryover and can bring our fields with it (counter read 348
# live 2026-07-23), so the marker may survive into a genuinely new game. That is
# handled in _decide_newgame by the _newgame_saw_ngplus latch rather than here:
# the client refuses to decide at all while the carried snapshot is on screen
# (Chaos bit set), so if it ever saw that window it knows to distrust the marker
# and fall back to the state evidence. A cold new game and a title-screen New
# Game both arrive with the region already zeroed, as measured.
#
# _reset_slotmagic_state zeroes 0x80C..0x83B and therefore wipes this byte; that
# is harmless because it only runs on the NG+ path (where the latch decides) and
# _save_delta_loop re-stamps within a tick.
#
# A save file written before this byte existed reads 0 and so looks "new" once.
# It then falls through to the gil/chests evidence -- which is what caught the
# reported bug anyway -- and self-heals on the player's next save.
AP_INIT_MARKER_SA = 0x08D11935          # u8 @ save+0x835
AP_INIT_MARKER_VALUE = 0xA7
SPELL_SLOTS_SPENT_OFF = SPELL_SLOTS_SPENT_BASE_SA - SAVE_BLOCK_PTR_CANON  # 0x80C
SPELL_SLOTS_GUARD_LO_SA = 0x08D118EC    # canary band save+0x7EC..0x807 (28B)
SPELL_SLOTS_GUARD_LO_LEN = 0x1C
SPELL_SLOTS_PAD_SA = 0x08D11939         # guard pad save+0x839..0x83B (3B)

# --- native rolling map-record list (save+0x440) -----------------------------
# The bounded per-visited-map return list RE'd 2026-07-31 while chasing the
# slot_magic block collision (documented in full above): {u32 flag, u16 x,
# u16 y, u32 dir, u32 map_id}, 16-byte stride, growing up from save+0x440,
# REUSING entries rather than growing monotonically.
#
# NEW GAME vs LOADED SAVE: usable, and measured on both sides 2026-08-17.
#   fresh new game, first field frame  ->  0 populated records
#   after a few minutes across maps    ->  2 records at save+0x440 / 0x450,
#                                          e.g. 01000000 1809 c809 00000000
#                                          3a000000 = flag 1, x, y, dir, map 0x3a
#   after LOADING that file            ->  the same 2 records, byte-identical
# So it populates with play and SURVIVES a load: a second, independent "this
# block came off disk" signal alongside the play-time clock.
#
# (An earlier read of a live loaded save showed this window all-zero and it was
# briefly written off as "zeroed on load". That was wrong: the list had simply
# never been observed POPULATED, so zero-on-loaded proved nothing. Do not repeat
# that shortcut -- a "field X is not preserved" claim needs a reading where X is
# known-nonzero beforehand.)
# NOT USED by the client. Kept because the RE cost real effort and the next
# person to wonder "could the map-record list tell a new game from a load?"
# deserves the answer (yes, but AP_INIT_MARKER_SA answers it directly, so this
# is redundant -- see the NEW GAME vs LOADED SAVE note in ApClient).
MAP_RECORD_LIST_SA = 0x08D11540          # save+0x440
MAP_RECORD_STRIDE = 0x10
MAP_RECORD_SLOTS = 11                    # spans save+0x440..0x4EF
MAP_RECORD_LIST_LEN = MAP_RECORD_STRIDE * MAP_RECORD_SLOTS

# --- play time ---------------------------------------------------------------
# save+0x1168 is TOTAL play time in SECONDS: serialized, restored from the file
# on load, and zero on a committed new game. All three legs measured 2026-08-17:
#   first field frame after committing a new game  ->  0
#   after ~2 minutes of play                       ->  129
#   at the moment of saving                        ->  144
#   first field frame after loading that file      ->  141
# The clock starts at the character-creation COMMIT, not at the title, so the
# minutes a player spends naming the party contribute nothing -- which is what
# makes "still ~0" a clean statement that this game has only just begun.
#
# NOT save+0x13a8. That neighbour looks like the same clock in frames and is
# not: it is a SESSION frame counter, reset on load (read 110 -- under two
# seconds -- immediately after the load that restored 0x1168 to 141, and it
# drifts against 0x1168 over a long session). Do not use it.
PLAY_TIME_SECONDS_SA = 0x08D12268        # u32 @ save+0x1168
# LEGACY leg of _save_is_initialised, for save files written before
# AP_INIT_MARKER_SA existed. A new game reads 0 and the gate latches within a
# tick or so of the save block resolving, so anything past a minute is a file.
# Generous both ways on purpose: a player cannot reach a save point in under a
# minute, and a new game would have to sit undecided for a full minute to trip it.
NEW_GAME_PLAY_TIME_MAX = 60
# _newgame_block_live (the carried-NG+-counter reset) reuses the SAME threshold.
# It cannot use the init marker: we stamp that the moment the one-shots arm, so
# by the time the grant loop runs it would always read "initialised" and a NG+
# seed's wedged grants would never be freed. Play time bounds it instead.
#
# A roomier window was tried and rejected. With 300 s, this sequence still
# duplicated items: player saves 2 minutes in with 3 items delivered, quits the
# CLIENT (so the session high-water resets to 0), restarts, loads. The save's
# counter of 3 then reads above a high-water of 0 and the reset fires on a
# perfectly good save. 60 s does not fix that in principle -- it needs a save
# made inside the first minute -- but it puts the residual squarely in the same
# "saved within seconds of starting" territory we deliberately do not chase, and
# the `c > _counter_hw` guard at the call site is the second layer.

# Inventory layout: ONE unified packed list of 3-byte records (consumables AND
# equipment together, in acquisition order; the game can re-sort the display).
#   record = [ category (+0), item_id (+1), qty (+2) ]
#   category:  1 = consumable item,  2 = weapon,  3 = armor
#   item_id is per-category (id 0 = empty slot). qty caps in normal play.
# To grant: write bytes([category, item_id, qty]) into a free record slot.
INV_RECORD_SIZE = 3
INV_CATEGORY_OFFSET = 0
INV_ID_OFFSET = 1
INV_QTY_OFFSET = 2
INV_QTY_MAX = 99                  # per-stack cap; a grant past this spills to a new slot
CAT_KEY, CAT_ITEM, CAT_WEAPON, CAT_ARMOR = 0, 1, 2, 3
# Sentinel "category" for EXP-bag grants (ID.is_exp items). NOT a real inventory cat;
# _ap_item_to_game returns it so _grant_pending routes to grant_exp instead of an
# inventory write. A string can never collide with the int cats 0..3.
CAT_EXP = "exp"

# --- Consumable item IDs (middle byte). Mapped by index-encoded flood, 2026-06.
# IDs 1..43 are all valid; 44+ fall back to Potion (invalid). Matches FFRPSP 0x2b.
CONSUMABLE_ITEMS = {
    1:  "Potion",          2:  "Hi-Potion",       3:  "X-Potion",
    4:  "Ether",           5:  "Turbo Ether",     6:  "Dry Ether",
    7:  "Elixir",          8:  "Megalixir",       9:  "Phoenix Down",
    10: "Remedy",          11: "Antidote",        12: "Gold Needle",
    13: "Eye Drops",       14: "Echo Grass",      15: "Emergency Exit",
    16: "Sleeping Bag",    17: "Tent",            18: "Cottage",
    19: "Spider's Silk",   20: "White Fang",      21: "Red Fang",
    22: "Blue Fang",       23: "Light Curtain",   24: "Red Curtain",
    25: "White Curtain",   26: "Blue Curtain",    27: "Lunar Curtain",
    28: "Hermes' Shoes",   29: "Vampire Fang",    30: "Cockatrice Claw",
    31: "Giant's Tonic",   32: "Faerie Tonic",    33: "Strength Tonic",
    34: "Protect Drink",   35: "Speed Drink",     36: "Golden Apple",
    37: "Silver Apple",    38: "Soma Drop",       39: "Power Plus",
    40: "Stamina Plus",    41: "Mind Plus",       42: "Speed Plus",
    43: "Luck Plus",
}
# Consumables whose whole effect is the mana pool. slot_magic makes that pool
# inert, so these get pulled from every pool we control (steal / shop / loot /
# AP filler) -- see _sm_soma / rando.strip_mana_items.
FAERIE_TONIC_ID = 32                    # full MP restore
SOMA_DROP_ID = 38                       # +5 maxMP -> +1 level-1 spell slot

# Weapon IDs (category 2). Mapped + self-verified (qty==id) 2026-06; all 67, no gaps.
WEAPONS = {
    1:  "Nunchaku",        2:  "Knife",            3:  "Staff",
    4:  "Rapier",          5:  "Hammer",           6:  "Broadsword",
    7:  "Battle Axe",      8:  "Scimitar",         9:  "Iron Nunchaku",
    10: "Dagger",          11: "Crosier",          12: "Saber",
    13: "Longsword",       14: "Great Axe",        15: "Falchion",
    16: "Mythril Knife",   17: "Mythril Sword",    18: "Mythril Hammer",
    19: "Mythril Axe",     20: "Flame Sword",      21: "Ice Brand",
    22: "Wyrmkiller",      23: "Great Sword",      24: "Sun Blade",
    25: "Coral Sword",     26: "Werebuster",       27: "Rune Blade",
    28: "Power Staff",     29: "Light Axe",        30: "Healing Staff",
    31: "Mage's Staff",    32: "Defender",         33: "Wizard's Staff",
    34: "Vorpal Sword",    35: "Cat Claws",        36: "Thor's Hammer",
    37: "Razer",           38: "Sasuke's Blade",   39: "Excalibur",
    40: "Masamune",        41: "Ultima Weapon",    42: "Ragnarok",
    43: "Murasame",        44: "Lightbringer",     45: "Rune Staff",
    46: "Judgment Staff",  47: "Dark Claymore",    48: "Duel Rapier",
    49: "Braveheart",      50: "Deathbringer",     51: "Enhancer",
    52: "Gigantaxe",       53: "Viking Axe",       54: "Rune Axe",
    55: "Ogrekiller",      56: "Kikuichimonji",    57: "Asura",
    58: "Kotetsu",         59: "War Hammer",       60: "Assassin Dagger",
    61: "Orichalcum",      62: "Mage Masher",      63: "Gladius",
    64: "Sage's Staff",    65: "Barbarian's Sword", 66: "Lust Dagger",
    67: "Golden Staff",
}

# Armor IDs (category 3). Mapped + self-verified (qty==id) 2026-06; all 75, no gaps.
# Includes body armor, shields, helms, gloves, and accessories (rings/capes).
ARMOR = {
    1:  "Clothes",          2:  "Leather Armor",    3:  "Chain Mail",
    4:  "Iron Armor",       5:  "Knight's Armor",   6:  "Mythril Mail",
    7:  "Flame Mail",       8:  "Ice Armor",        9:  "Diamond Armor",
    10: "Dragon Mail",      11: "Copper Armlet",    12: "Silver Armlet",
    13: "Ruby Armlet",      14: "Diamond Armlet",   15: "White Robe",
    16: "Black Robe",       17: "Crystal Mail",     18: "Thief's Armlet",
    19: "Black Garb",       20: "Kenpogi",          21: "Power Sash",
    22: "Red Jacket",       23: "Sage's Surplice",  24: "Light Robe",
    25: "Gaia Gear",        26: "Bard's Tunic",     27: "Genji Armor",
    28: "Maximillian",      29: "Survival Vest",    30: "Lordly Robes",
    31: "Leather Shield",   32: "Iron Shield",      33: "Mythril Shield",
    34: "Flame Shield",     35: "Ice Shield",       36: "Diamond Shield",
    37: "Aegis Shield",     38: "Buckler",          39: "Protect Cloak",
    40: "Genji Shield",     41: "Crystal Shield",   42: "Hero's Shield",
    43: "Zephyr Cape",      44: "Elven Cloak",      45: "Master Shield",
    46: "Leather Cap",      47: "Helm",             48: "Great Helm",
    49: "Mythril Helm",     50: "Diamond Helm",     51: "Healing Helm",
    52: "Ribbon",           53: "Genji Helm",       54: "Crystal Helm",
    55: "Black Cowl",       56: "Twist Headband",   57: "Tiger Mask",
    58: "Feathered Cap",    59: "Red Cap",          60: "Wizard's Hat",
    61: "Sage's Mitre",     62: "Shadow Mask",      63: "Leather Gloves",
    64: "Bronze Gloves",    65: "Steel Gloves",     66: "Mythril Gloves",
    67: "Gauntlets",        68: "Giant's Gloves",   69: "Diamond Gloves",
    70: "Protect Ring",     71: "Crystal Gloves",   72: "Thief's Gloves",
    73: "Crystal Ring",     74: "Angel's Ring",     75: "Genji Gloves",
}

# Key item IDs (category 0). Names mapped via the "bugged Items-tab" render of
# cat-0 records (2026-06); ids 1..36, id 37+ default to Lute. 36 total.
# Possession is NOT an inventory record -> it is a 5-byte BITFIELD (see below).
# id order here == in-menu display order == bit order in the bitfield.
KEY_ITEMS = {
    1:  "Lute",            2:  "Crown",           3:  "Crystal Eye",
    4:  "Jolt Tonic",      5:  "Mystic Key",      6:  "Nitro Powder",
    7:  "Adamantite",      8:  "Rosetta Stone",   9:  "Star Ruby",
    10: "Earth Rod",       11: "Levistone",       12: "Chime",
    13: "Rat's Tail",      14: "Warp Cube",       15: "Bottled Faerie",
    16: "Oxyale",          17: "Canoe",           18: "Carobo",
    19: "Ocarina",         20: "Cogwheel",        21: "Pickaxe",
    22: "Autograph",       23: "Witch's Brew",    24: "Smyth's Tools",
    25: "House Key",       26: "Cat's Whisker",   27: "Arm Parts",
    28: "Shoulder Parts",  29: "Torso Parts",     30: "Audio Circuit",
    31: "Leg Parts",       32: "Exoskeleton",     33: "A.I. Chip",
    34: "Head Parts",      35: "Battery Circuit", 36: "Energy Chip",
}

# --- Key-item POSSESSION bitfield (SOLVED + fully mapped 2026-06 via ki_multi.py
# multi-snapshot diff + live bit-probe; verified against the in-game Key Items menu).
# 5 bytes, MSB-first within each byte, running BACKWARD from the high byte:
#   0x08D1153B = ids 1..8   (bit7=id1 Lute .. bit0=id8 Rosetta Stone)
#   0x08D1153A = ids 9..16
#   0x08D11539 = ids 17..24
#   0x08D11538 = ids 25..32
#   0x08D11537 = ids 33..36 (bit7=id33 .. bit4=id36; bits3..0 unused)
# So for KEY_ITEMS id (1-based): byte = HIGH - (id-1)//8 ; bit = 7 - (id-1)%8.
# Grant: byte |= (1<<bit). Remove: byte &= ~(1<<bit). The id->name order in
# KEY_ITEMS above is exactly this bit order. Whole-region 0xFF over
# 0x08D11500-0x08D11540 grants ALL key items. NOTE: the Key Items menu caches its
# display list -> a fresh menu open (from the field) is needed to see changes.
KEY_ITEM_BITFIELD_HIGH = 0x08D1153B   # byte holding ids 1..8 (highest address)

def key_item_bit(item_id):
    """Return (address, bitmask) for a 1-based KEY_ITEMS id."""
    if not 1 <= item_id <= 36:
        raise ValueError(f"key item id out of range: {item_id}")
    addr = KEY_ITEM_BITFIELD_HIGH - (item_id - 1) // 8
    mask = 1 << (7 - (item_id - 1) % 8)
    return addr, mask

KEY_ITEM_IDS_BY_NAME = {name.lower(): i for i, name in KEY_ITEMS.items()}

def key_item_id(name):
    """Resolve a key-item name (case-insensitive) to its id, or None."""
    return KEY_ITEM_IDS_BY_NAME.get(name.lower())

SPELLS = {}  # category 7 (id 1 = Cure confirmed). FFRPSP: 64. TODO: flood [7,i,i].

# Inventory record CATEGORY byte values (byte0), discovered 2026-06:
#   0 = key item (display-bugged in this list; real possession stored elsewhere)
#   1 = consumable item   2 = weapon   3 = armor   7 = spell (own storage too)
# Categories 4,5,6,8+ render as invalid/fallback in the normal Items tab.

# --- Party baseline stats (Lv 1 start), captured 2026-06 for later RE/diffing ---
# Argus  (Warrior):    HP35 MP0  STR10 AGI8  INT1  STA15 LCK8  Atk10 Acc28 Def1 Eva59
# Sarisa (Thief):      HP30 MP0  STR5  AGI15 INT1  STA5  LCK15 Atk7  Acc40 Def1 Eva71
# Jenica (White Mage): HP33 MP10 STR5  AGI5  INT15 STA8  LCK5  Atk8  Acc10 Def1 Eva56  MagicLv1
# Gilles (Black Mage): HP25 MP10 STR3  AGI5  INT20 STA2  LCK10 Atk7  Acc23 Def1 Eva56  MagicLv1
# PARTY ARRAY (decoded 2026-06). Working copy at 0x08D11EE4; a backup copy lives
# near 0x08D29xxx. 4 characters, stride 0x5C. Order: Argus, Sarisa, Jenica, Gilles.
PARTY_BASE_SA = 0x08D11EE4
PARTY_STRIDE = 0x5C
PARTY_COUNT = 4
# Field offsets from each character's record start:
P_LEVEL   = 0x00   # u32
P_EXP     = 0x04   # u32 (current exp)
P_HP      = 0x08   # u16
P_MAXHP   = 0x0A   # u16
P_MP      = 0x0C   # u16
P_MAXMP   = 0x0E   # u16
P_MAGICLV = 0x10   # u8
P_STR     = 0x11   # u8 base stats (displayed values add equipment bonuses)
P_AGI     = 0x12
P_INT     = 0x13
P_STA     = 0x14
P_LCK     = 0x15
# +0x16.. derived stats (attack / accuracy / defense / evasion) as u16s, TBD exact.
# Slot id (1-based) at 0x58. CLASS/JOB byte at 0x5A (decoded 2026-06-29 via
# 4-Warrior vs 4-Thief save diff). Enum: 0 Warrior,1 Thief,2 Monk,3 RedMage,
# 4 WhiteMage,5 BlackMage. Writing 0x5A live updates sprite/name/equip-list and
# level-up growth, but does NOT retro-recompute already-baked stats/HP/MP/magic.
# So a starting-party setter must also write the job's level-1 base stat block.
P_SLOTID  = 0x58   # u8, name/identity id (NOT formation; do not rewrite blindly)
P_CLASS   = 0x5A   # u8, job id (within-record), but see CLASS addressing below.

# DISPLAY/RENDER ADDRESSING (decoded 2026-06-29 by per-record calibration on a
# real new game, falsifying the (r-1)%4 wrap). The status menu renders each row r:
#   HP/MP/stats  from record r                  (record order)
#   CLASS+SPRITE from PARTY_BASE_SA - 2 + r*0x5C    (a class array offset -2 vs stats)
# Row0's class lives in a separate "leader" byte at PARTY_BASE_SA-2 (NOT record 3);
# rows 1..3 read the previous record's 0x5A, which equals CLASS_BASE_SA + r*0x5C. So a
# single linear class array (base PARTY_BASE_SA-2, stride 0x5C) covers all four rows.
# Field KO-status byte (RE'd 2026-07-29 by natural-KO vs church-revive full-RAM
# diff): u8 at PARTY_BASE_SA - 1 + row*0x5C -- the byte BETWEEN the class array
# entry (-2) and the record start. 1 = KO (menu shows the fallen pose + church
# offers revival); church revive clears it to 0. A member with HP 0 but this
# byte 0 is only SOFT-dead (can't act, but no KO label), so a client kill must
# write BOTH HP=0 and this byte=1 (death_link).
STATUS_BASE_SA = PARTY_BASE_SA - 1           # 0x08D11EE3
P_STATUS_KO = 0x01
def status_addr_sa(row):                     # row 0..3 = menu order
    return STATUS_BASE_SA + row * PARTY_STRIDE

CLASS_BASE_SA   = PARTY_BASE_SA - 2          # 0x08D11EE2
CLASS_STRIDE = PARTY_STRIDE            # 0x5C
def class_addr_sa(row):                   # row 0..3 = top-to-bottom menu order
    return CLASS_BASE_SA + row * CLASS_STRIDE
# To set display row r to job j:
#   write JOB_L1_BLOCK[j] at party_addr_sa(r, L1_BLOCK_OFF)   # stats/HP/MP  -> record r
#   write bytes([j])       at class_addr_sa(r)                # class+sprite -> class array
JOB_NAMES = ["Warrior", "Thief", "Monk", "RedMage", "WhiteMage", "BlackMage"]

# Promoted job ids. promoted = base + 6 (all six live-verified 2026-07-01:
# 6=Knight 7=Ninja 8=Master 9=RedWizard 10=WhiteWizard 11=BlackWizard). Used for
# spell-tome learnability (a promoted class's learn set is a superset of its base)
# and job/name lookups. Promotion itself happens via the game's NATIVE Bahamut
# event (Rat's Tail turn-in; see KEY_ITEM_FUNCTION_BITS[13]) -- there is no
# client-side promotion. The v47 client mailbox promotion was abandoned: the
# game's promote routine can't run outside its event's lineup-scene context (see
# job-advancement-items memory).
KNIGHT, NINJA, MASTER, RED_WIZARD, WHITE_WIZARD, BLACK_WIZARD = 6, 7, 8, 9, 10, 11
PROMOTED_JOB_NAMES = {6: "Knight", 7: "Ninja", 8: "Master",
                      9: "RedWizard", 10: "WhiteWizard", 11: "BlackWizard"}
# base ("from") job id -> promoted ("to") job id.
PROMOTE = {0: KNIGHT, 1: NINJA, 2: MASTER, 3: RED_WIZARD,
           4: WHITE_WIZARD, 5: BLACK_WIZARD}

# Level-1 job-constant stat block, harvested 2026-06-29 from one fresh new game
# per job (4-of-a-kind parties, fields identical across all 4 chars). Covers
# offsets 0x08..0x1C inclusive (21 bytes): HP/maxHP, MP/maxMP, MagicLv, STR/AGI/
# INT/STA/LCK, and level-1 derived stats (0x16/0x18/0x1A/0x1C). Per-char tail
# (0x46+) is NOT job-derived and is left untouched. Write this block at 0x08 of a
# character's record together with the class byte (0x5A) to set a starting job.
JOB_L1_BLOCK = {
    0: bytes.fromhex('2300230000000000000a08010f080a0035000f0002'),  # Warrior
    1: bytes.fromhex('1e001e000000000000050f01050f0f003a000d0002'),  # Thief
    2: bytes.fromhex('2100210000000000000c05010a05080032000a0003'),  # Monk
    3: bytes.fromhex('1e001e000a000a0001050a0a05050c003500140002'),  # RedMage
    4: bytes.fromhex('210021000a000a000105050f080505003500170003'),  # WhiteMage
    5: bytes.fromhex('190019000a000a0001030514020a08003500170002'),  # BlackMage
}
L1_BLOCK_OFF = 0x08

# --- Equipped-gear slots (naked_monks) --------------------------------------------
# RE'd 2026-07-14 via re_only/naked_probe.py --wide (fresh party, before/after an
# in-game unequip): the equipped weapon/armor refs are IN the party record, a 4-byte
# block at +0x1c -> [weapon_id @+0x1c, 0, 0, armor_id @+0x1f]. Verified across all 4
# starting jobs (weapon 02/03, armor 01=Clothes) and confirmed by the wide diff:
# unequipping zeros exactly +0x1c and +0x1f (the two nonzero bytes). The gear is
# stored HERE, not as an inventory record -- an equipped item is NOT in the packed
# inventory list, so zeroing this block destroys the gear outright (nothing to add
# to / remove from the bag). +0x1c doubles as the record's weapon-derived attack
# stat; the game's own unequip clears it too, so zeroing matches native behavior.
# The 0x08D18E.. sixplex (stride 0xE4) that also holds these ids is a display cache
# rebuilt from the record on menu/field refresh -- do NOT write it.
EQUIP_OFF = 0x1C        # record offset of the 4-byte equipped-gear block
EQUIP_LEN = 4           # +0x1c weapon id .. +0x1f armor id (bytes between = 0)
NAKED_MONK_GIL = 7      # gil granted per Monk stripped at new game
VANILLA_START_GIL = 500  # gil a vanilla new game commits; the yaml starting_gil
                         # option is applied as a DELTA off this (see
                         # ApClient._starting_gil_loop) so an AP gil item that
                         # lands during character creation is never clobbered

PARTY_NAMES = ["Argus", "Sarisa", "Jenica", "Gilles"]
def party_addr_sa(char_index, field=0):  # char_index 0..3
    return PARTY_BASE_SA + char_index * PARTY_STRIDE + field

# --- Battle drop list (native "Obtained <item>." reward path) --------------------
# During a battle, *(BATTLE_ACTOR_OBJ_PTR_SA) = battle_base (e.g. 0x08d1f6a0). The
# victory drop-processing fn (0x887fb90) reads a DROP LIST at battle_base+0x6848:
# BATTLE_DROP_SLOTS entries of BATTLE_DROP_STRIDE bytes each = [category, id, ?].
# A nonzero category slot is granted + announced natively at victory. Writing our
# own entry into a free (cat==0) slot mid-battle makes the Thief "extra item" a
# real drop (see thief-steal-ability memory; verified live 2026-07-01).
BATTLE_ACTOR_OBJ_PTR_SA = 0x08D2C65C   # *(this) = battle_base; valid only in battle
# In-battle boolean (u8): 1 during an active battle, 0 on the field. Sits at
# battle_base - 0x4A0 (canonical battle_base = 0x08D1F6A0, so this = 0x08D1F200)
# and, unlike BATTLE_ACTOR_OBJ_PTR above, is CLEARED on battle exit. The pointer
# LATCHES -- once a battle sets it, it keeps the last battle_base forever (the
# game only overwrites it at the next battle, never zeroes it on exit), so
# "pointer in RAM range" reads as "in battle" permanently after the first fight.
# That silently broke every loop that gated on it (thief-steal rolled once then
# never again; chest-poll/openworld/table stopped ticking post-battle). Gate on
# THIS flag instead. Save-arena-relative -> read via sa() like the pointer.
# (RE'd 2026-07-06 by battle/field snapshot diff + live active-cycle validation.)
BATTLE_ACTIVE_FLAG_SA = 0x08D1F200
BATTLE_DROP_LIST_OFF = 0x6848       # battle_base + this = drop list
BATTLE_DROP_STRIDE   = 3            # bytes per entry: [cat, id, qty]
                                   # (disasm: victory-drop fn 0x887fb90 reads
                                   # +0=category blez->skip, +1=id. The 2026-07-08
                                   # [id,cat,qty] swap FROZE the loot phase; reverted.)
BATTLE_DROP_SLOTS    = 9            # max entries the drop fn scans (index < 9)
# SLOT N IS OWNED BY ENEMY ROW N -- it is not a free-for-all list. The victory
# reward loop (0x888646c) walks enemy rows 0..8 and calls set_drop(0x8886654)
# with slot = row, which stamps [cat, id, 1] from the monster drop table
# (0x0894c446 + species*36: +0 cat, +1 id, +2 percent chance) whenever that
# species has a drop and rand%100 clears it. So an entry the client parks in an
# OCCUPIED row's slot gets silently replaced at victory -- the client must pick
# a row whose BU_SPECIES reads 0xFF (see ApClient._steal_drop_slot).
# (Disasm 2026-08-06, after two live steal->wrong-item mismatches.)

# --- Per-actor battle-unit records (job-scroll boosts) ----------------------------
# Battle runs on per-battle COPIES of the party stats (battle-engine-re +
# blood-magic RE, live-verified): record = battle_base + BATTLE_UNIT_OFF +
# row*BATTLE_UNIT_STRIDE, rows 0-3 = party (menu order), 4+ = enemies. HP/MP
# write back to the party record at battle end; every other field resets next
# battle -- i.e. these ARE the "temporary battle stats" the tonics buff, so
# client writes here are tonic-style by construction.
BATTLE_UNIT_OFF    = 0xC714
BATTLE_UNIT_STRIDE = 0x6C
BU_STATUS = 0x00   # u16 status bits (bit0=KO, bit1=stone; &3 = out of action)
BU_HP     = 0x08   # u16 current HP
BU_MAXHP  = 0x0A   # u16 max HP
BU_MP     = 0x0C   # u16 current MP
BU_MAXMP  = 0x0E   # u16 max MP
BU_ATTACK = 0x18   # u16 attack power (the phys damage formula's base)
# EFFECTIVE (equipment-inclusive) stat block, u8 each. RE'd live 2026-07-31
# (re_only/probe_unit_agi.py): for every party member wearing no stat gear the
# unit byte equals the BASE party-record stat exactly, and a member wearing stat
# gear reads base + the gear bonus -- a Thief in Thief's Gloves showed AGI 23 ->
# 28 (+5) at +0x37 while the other three matched base at all four offsets. These
# are what the game itself fights with (gear + tonics + any temp buff), so any
# client roll that should respect equipment must read HERE, not P_AGI/P_LCK.
BU_STA_EFF = 0x35
BU_INT_EFF = 0x36
BU_AGI_EFF = 0x37
BU_LCK_EFF = 0x38
BU_SPECIES = 0x49  # u8 monster id of an ENEMY row (rows 4+); the actor's
                   # own monster_stats index. RE'd live 2026-07-25 (Chimera
                   # fight, probe_enemy_species.py): resolves the SoS/Break
                   # target directly by row, unlike the type-count walk which
                   # mis-maps centered single big-enemy formations (Chimera
                   # reserves 3 enemy rows, live one is the CENTER row, so the
                   # cave's unit_raw != the packed type-expansion ordinal).

# --- TEMP STAT BONUSES: the fields the in-battle tonics write ---------------------
# RE'd live 2026-07-14 by diffing a battle-unit record across a tonic use:
#   Giant's Tonic   -> +0x66 = 200, and BU_MAXHP  went 137 -> 337
#   Strength Tonic  -> +0x26 = 10,  and BU_ATTACK went  10 ->  20
# CRITICAL: the engine RE-DERIVES the visible stat as (party-record stat + bonus)
# every time damage resolves. Writing BU_MAXHP / BU_ATTACK directly is therefore
# silently reverted by the next hit -- the BONUS is the only input that persists.
# These are ALSO the player's tonic bonuses, so client writes must be ADDITIVE
# (read-modify-write); overwriting one would wipe a Giant's/Strength Tonic the
# player drank. Both reset per battle like the rest of the record.
BU_ATTACK_BONUS = 0x26   # u16 temp attack bonus  (Strength Tonic)
BU_MAXHP_BONUS  = 0x66   # u16 temp max-HP bonus  (Giant's Tonic)

# --- Battle enemy info (for XP-scaled thief-steal loot) ---------------------------
# RE'd 2026-07-02 (re_only/probe_battle_units.py, lizard-vs-5-goblin dump diff,
# live-verified): battle_base+0x68A6 holds the resolved encounter:
#   +0  u16 formation id (row of the 0x2b24d68 formations table)
#   +2  u16 (always 2 so far; battle type?)
#   +4  u8[4] monster ids, 0xFF = unused type slot
#   +8  u8[4] spawned count per type slot
# Battle XP payout = sum(MONSTER_XP[id] * count).
BATTLE_ENEMY_INFO_OFF = 0x68A6
BATTLE_ENEMY_TYPES    = 4

# Vanilla per-monster XP reward, indexed by monster id. u16 at +0 of each
# monster_stats record (ISO 0x2b28480, 0x24 stride, 203 rows -- ffrpsp_tables);
# monster stats aren't shuffled, so a static bake is safe.
MONSTER_XP = [
    6, 18, 24, 93, 135, 402, 153, 2472, 1977, 879, 1752, 1506, 30, 105,
    882, 40, 60, 267, 2361, 42, 3591, 9, 378, 63, 186, 288, 1182, 195,
    282, 723, 123, 165, 957, 225, 639, 489, 1050, 621, 852, 90, 231, 432,
    990, 24, 93, 117, 150, 4344, 2683, 1671, 3225, 1, 699, 1218, 780, 603,
    1194, 2244, 438, 843, 1200, 2385, 132, 387, 1536, 1620, 1701, 2904,
    2331, 84, 255, 252, 1101, 30, 141, 1317, 1160, 1428, 2610, 300, 984,
    186, 423, 1173, 1218, 3387, 7200, 240, 546, 816, 1890, 1224, 3189,
    915, 1215, 1224, 4000, 1962, 1614, 2355, 3489, 2064, 4584, 276, 822,
    130, 4068, 3274, 1257, 2385, 6717, 1263, 2700, 2250, 1095, 3420, 63,
    1272, 32000, 2200, 2000, 2475, 2000, 4245, 2000, 5496, 2000, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3800, 4000, 850, 2000,
    500, 300, 1000, 5000, 2400, 5505, 6000, 2500, 800, 300, 555, 2000,
    300, 500, 200, 500, 250, 140, 1200, 888, 1542, 500, 1800, 3000, 340,
    1200, 960, 4440, 4050, 1500, 300, 1300, 1500, 1000, 2500, 753, 240,
    300, 120, 500, 1000, 200, 133, 1110, 2000, 250, 1, 1, 1, 1, 1, 1, 1, 1,
]

# --- Full vanilla monster_stats block (FFRPSP; ISO 0x2b28480, 0x24 stride, 203
# rows). Bundled whole because the reward fields we scale are STRIDED: +0 = XP
# reward (u16), +2 = Gil reward (u16). boot_patch.scale_monster_rewards() copies
# this block and multiplies those two fields per record (xp_boost / gil_boost),
# leaving the other 0x20 stat bytes untouched. Read byte-for-byte from our ISO;
# the +0 field matches MONSTER_XP above (asserted in test_patch).
import base64 as _b64
MONSTER_STATS_STRIDE = 0x24
MONSTER_STATS_COUNT  = 203
MONSTER_STATS_BLOCK  = _b64.b64decode(
    "BgAGAAgAav8GBAECBAMBAQAAAAQQAAAAAAAAAAAAAAAAAAAAEgASABAAeP8JBgEECAUDAQAAAAQXAAAAAAABAQMAAAAAAAAAGAAGABQAaf8kAAEFCBIBAQAAAAAcAAAAAAAAAAAAAAD7////XQAWAEgAbP82AAESDhsDAQAAAAAuAAAAAAABCwQAAAD7////hwBDAEQAeP8qBgERDhUIAQIBBJEtAAAAAAAAAAAAAAD7////kgHIAFwAyAA2AAEXGRsMAQAAAAA3ABAAIAADIwIAAAD7////mQAyAFwAhv8YDAEXEgwDCgAAAAI3AAAAAAAAAAAAAAD7////qAmwBCgByAEkEgJKHxIIAQAAAAKPACAAEAAAAAAAAAD7////uQeSAsQAyAIYFAE2HgwMAQAAAAJbAAAAAAABDAcAAAD7////bwNvA/AAiP8wDAE8JhgFAQAAAAR4AAAAAAABEQMAAAD7////2AbYBlAByP8wEAFOPBgKAQAAAASWABAAIAAAAAAAAAD7////4gXiBSwByP8wFAFTSRgMAQAAAASHACAAEAAAAAAAAAD7////HgAeABwAbv9IBAEHCiQEAQAAACAcAEAAkAABAQUAAAD7////aQBpAEAAjv9OCAEQDycHAQAAACAuAEAAkAAAAAAAAAD7////cgNyA8wAyP9gFAEzLzAPAQAAACBlAEAAkAAAAAAAAAD2////KAAoABgA//8MAAECCgYDAQAAAAAjAAAAAAADHwIAAAD7////PAB4ADIAav8YBgENDgwGAQAAAAAlAAAAgAACDAMAAAD7////CwFCAHgAef9IAAEeFiQIAQAAACBGAEAAkAAAAAAAAADx////OQlYAlgByP9ICAFWMiQQAQAAACCqAEAAkAACFgMAAAD2////KgAKAAoAbgNUAAECBCoGAQAAAAAOAEAAkAAAAAAAAADx////Bw4HDjAByAQYEAJMHgwAAQAAACCcAEAAkAAAAAAAAAD7////CQADAAoAfP8MAAECCgYAAQAAAAgRABAAKz8BDgQAAAAAAAAAegF6AZAAnP8qDAEkGhUGAQAAAAhMABAAKz8AAAAAAAD7////PwAPADgAaP8YCAEOEQwJAQAAAAAoABAAAAAAAAAAAAD7////ugDIAFQAav8qCAgVARUFAQE+EAAzAAAAAAAAAAAAAAD7////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JQWKAqQAliFICAIpFiQIAQAAAABfAAAAgAABEQMAAAD2////iASIBOQAhP94DAM5FzwgAQAAAABzAAAAgAAAAAAAAADs////lAUsAQABkv84JgNAPBwQAQAAAACCAAAAAAAAAAAAAAD7////MgoBAGABkP8wMAFYYhgMAQAAAACcAAAAAAAAAAAAAAD2////LAEsAVAArP8YFAEUHgwQAQE+IAg8ABAAKz8AAAAAAAAAAAAA2APoA7wAlP8YGAEvKwwYAQIBIAhfABAAKz8AAAAAAAAAAAAAugDIADIAfP9IBAEKASQIAQIBAgAvAAAAgAABDAcAAAD2////pwH0ASwAfBBIBAELFCQPAQAAAAAtACAAkAABDAIAAAD7////lQQyANQAlv9gDAE1HjAQAQIBBAJzAAAAgAAAAAAAAAD2////wgT2AQQBlv88FgFBKB4YAQAAAAKDAAAAgAAAAAAAAAAAAAAAOw32AeABkP88CgGFQR4KAQAAAALIAAAAAAABIQEAAAD2////IBxYAlgClv88CgGQcx4YHgAAAALIAAAAAAABHwEAAAD2////8AAUAFwAiv9IAAEXFiQKAQAAAABEAEAAkAAAAAAAAAD2////IgIuAKwAjv9IFAErJSQNAQAAAABTAAAAAAAAAAAAAAD7////MAOEA7gAiv8wEAIuKhgJAQAAAABnAEAAkAABEgMAAAD2////YgfQByABjv8wFAJIOBgQAQAAAAKPAEAAkAABAgUAAAD7////yARmANAAsP8YGAM0FAwSAQIBBAB0AEAAkAAAAAAAAAAAAAAAdQz0AVgByP8YIANWIwwUAQIBBACqAAAAAAAAAAAAAAAAAAAAkwOWANQAiv8kDgM1HhIOAQAAAAJ0AAAAAAAAAAAAAAAAAAAAvwSQAbYAmBEkDgMuFBIQAQAAAAJnACAAEAABFQIAAAAAAAAAyASQAcgAyP9IKAIyGSQYAQE+EABuAEAACz8AAAAAAAD2////oA/QB5ABlv9gMAFaZjAcAQAAAACgAEAAuz8BCQQAAAD2////qgcgAywByP9IFAFERSQgAQAAAAGCACAAmz8AAAAAAAAAAAAATgYnA2YByP+QBAE+NUgoAQAAAAGCAAAAiz8BBAIAAADx////MwkzCWQByBJICAFHCSQZAQIBBGB0AEAAkAAAAAAAAAD2////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"
)
assert len(MONSTER_STATS_BLOCK) == MONSTER_STATS_STRIDE * MONSTER_STATS_COUNT

# --- Level-up XP requirement table (FFRPSP xp_requirements; ISO 0x2b2f438) -------
# 98 u32 LE = cumulative EXP needed to REACH each next level. The game reads this
# table during its own level-up check, so dividing every entry by N makes the party
# level N times faster WITH correct timing (no post-hoc EXP edit / level lag).
# Read byte-for-byte from our ISO. Loaded into RAM -> locate by signature scan
# (392-byte pattern), scale, re-apply (it reverts to vanilla on save/load).
XP_REQUIREMENTS = (
    14, 42, 98, 196, 350, 574, 882, 1288,
    1806, 2675, 3851, 5258, 6917, 8849, 11075, 13616,
    16493, 19727, 23339, 27350, 31781, 36653, 41987, 47804,
    54125, 60971, 68363, 76322, 84869, 94025, 103811, 114248,
    125357, 137159, 149675, 162926, 176933, 191717, 207299, 223700,
    240941, 259043, 278027, 297914, 318725, 340475, 362225, 383975,
    405723, 427473, 449223, 470973, 492722, 514472, 536222, 557972,
    579720, 601470, 623220, 644970, 666719, 688469, 710219, 731969,
    753717, 775467, 797217, 818967, 840716, 862466, 884216, 905966,
    927714, 949464, 971214, 992964, 1014713, 1036463, 1058213, 1079963,
    1101711, 1123461, 1145211, 1166961, 1188710, 1210460, 1232210, 1253960,
    1275708, 1297458, 1319208, 1340958, 1362707, 1384457, 1406207, 1427957,
    1449705, 1471455,
)
# Cumulative EXP to REACH the max level (99) = last XP_REQUIREMENTS entry. grant_exp
# clamps each member's P_EXP to this so an EXP bag can never overflow past max level.
EXP_CAP = XP_REQUIREMENTS[-1]     # 1_471_455

# --- Location / treasure flags --- (investigated 2026-06)
# Opening a chest also sets a bit in a per-map LIVE bitfield at 0x08D192B0
# (chest #N -> bit N-1), but that one does NOT survive save/reload -- do not
# build detection on it. The live detector is the SAVE-RESIDENT global chest
# bitfield CHEST_OPEN_BF_SA (see its note further down), polled by
# _chest_poll_loop; the old exec-breakpoint chest hook is gone.
# --- Auto-dash: persistent CONFIG dash setting (SOLVED 2026-06-28) ---
# bit0 of the config byte at 0x08D12270 (save block, 12 bytes after GIL_ADDR_SA) is
# the in-game Config "Dash" toggle: 0x40 = off, 0x41 = on. Found via a low-noise
# bracket-diff of the save block (0x08D11000+) while toggling the menu. This is a
# FIXED address (stable across boots, like gil/key-items) and DRIVES NATIVE
# BEHAVIOR: setting bit0 makes the Config menu show Dash=On AND the party auto-runs
# without holding the dash button (live-verified). Unlike the per-actor runtime
# flag (dynamic, corrupted RAM), this is the correct, safe target. See [[auto-dash-flag]].
# Applied ONE-SHOT at new game (with the party jobs), never re-asserted: Dash is a
# player preference, and the old 3s re-apply fought players who turned it off.
# CORRECTION 2026-07-21: the "0x40 = off / 0x41 = on" reading above was wrong about
# the high bits -- 0x40 was just the message-speed field's value at the time. Only
# bit0 is Dash; bits6-7 are Message Speed (below). Live byte read 0x43 and 0x83.
DASH_CONFIG_ADDR_SA = 0x08D12270
DASH_CONFIG_MASK = 0x01

# --- SUPER DASH: the whole movement-speed mechanism (RE'd 2026-08-14) --------
# Field movement is tile-quantized: a step record at ctx+0x68E0 (ctx =
# *(0x089D7ADC)) interpolates one tile over N frames. rec+0xC = frame counter
# (0..N-1), rec+0xE = N = FRAMES-PER-TILE = the speed. N comes from a u16 table
# at 0x08941870 indexed by the vehicle mode ([save+0x2A8]):
#   [16, 16, 8, 4, 8, 4, 1, ...] -- foot 16, ship 8, airship 4 (measured
#   tiles/s with re_only/speed_tour.py matched: walk 4.2, dash/ship ~7.5-8.6).
# The step-record init caller loads it at 0x08836D04 (`lhu a3,0(v0)`, v0 =
# table + mode*2), then the engine's own dash leg at 0x08836DB8 does
# `threshold >>= 1` (16 -> 8 = vanilla dash), gated by:
#   * dash trigger = Config Dash bit ([save+0x1170] bit0 -- this constant,
#     read via the live save base in s0, hence no delta) OR the dash button:
#     bit 0x2 of the u16 buttons mirror at SD_PAD_BUTTONS (static). So with
#     Config Dash on, holding the button changes nothing vanilla -- there is
#     no double halving.
#   * mode == 0 (foot only -- ship/airship never halve), plus map checks
#     ([ctx+0xBF4] vs 0xD4/0xE9/0xDF and a [save+0xB0] bit8 veto).
# Found via a write breakpoint on the step counter (re_only/bp_speed.py,
# FastMemoryAccess=False) + memory.disasm (the plain RAM view shows JIT
# emuhack words 0x68xxxxxx at hot loads -- disc bytes are clean).
# iso_patcher.apply_super_dash detours the two sites; overworld foot is told
# apart via the field map id ([save+0x2008] == 0xFF; town Cornelia read 0x4B).
SD_PAD_BUTTONS = 0x08B10D7E     # STATIC buttons mirror u16 -- never sa()
SD_PAD_DASH_MASK = 0x0002       # dash button bit in that word
SD_SPEED_TABLE = 0x08941870     # STATIC u16 frames-per-tile by vehicle mode
SD_VEHICLE_MODE_SA = 0x08D113A8     # save+0x2A8: 0 foot / 2 ship / 3 airship

# --- Message Speed: same config byte, bits6-7 (RE 2026-07-21, save-block diff) ---
# Captured with _cfgspeed_snap.py: changing ONLY Message Speed from medium-slow to
# medium-fast moved exactly TWO stable save-block bytes --
#   0x08D12270: 0x43 -> 0x83   (bits6-7: 1 -> 2, i.e. the speed index)
#   0x08D124A9: 0x02 -> 0x04   (1-of-N bitmask mirror, bit# == the same index)
# So the field is a 0..3 index (0=slowest .. 3=fastest) stored twice. Which copy the
# engine actually reads is un-RE'd, so we write BOTH, reproducing exactly the state a
# real menu change produces. 2nd-to-fastest == index 2 (the values observed above).
MSG_SPEED_SHIFT = 6                  # in DASH_CONFIG_ADDR_SA
MSG_SPEED_MASK = 0xC0
MSG_SPEED_MIRROR_ADDR_SA = 0x08D124A9   # 1-of-N bitmask: 1 << index
MSG_SPEED_DEFAULT = 2                # 2nd-to-fastest == yaml `fast`

# --- Cursor: SAME config byte, bit1 (RE 2026-07-29) ------------------------
# The Config menu's "Cursor" row (Default / Memory -- whether menus reopen with
# the cursor where you left it) is bit1 of DASH_CONFIG_ADDR_SA: 0 = Default,
# 1 = Memory. So one byte carries all three Config-menu settings we touch:
#   bit0    Dash        bit1  Cursor        bits6-7  Message Speed
#
# Method: a save-block diff found NOTHING (the narrow 0x08D11000..0x08D13000
# window showed zero movement), so this was found by diffing ALL of user RAM
# across two 3-snapshot passes (idle / idle / toggled) and keeping only the
# addresses that flipped BACK on the reverse toggle -- 25MB and ~5000 candidates
# down to one. See re_only/cursor_snap.py.
#
# The hit landed at 0x08D16270, which is 0x4000 above this constant: that gap is
# just the live save_delta the client's sa() already applies, NOT a second copy.
# Live-verified both ways 2026-07-29: poking 0x83->0x81 flipped the menu row to
# Default, 0x81->0x83 back to Memory, with Dash and Message Speed unmoved.
# Save-block resident, so it survives save/load and the one-shot-at-new-game
# write is enough (no re-assert loop).
CURSOR_CONFIG_ADDR_SA = DASH_CONFIG_ADDR_SA   # same byte
CURSOR_CONFIG_MASK = 0x02                     # bit1: 0 = Default, 1 = Memory
CURSOR_MODE_DEFAULT = 1              # yaml default: `memory`

# Cornelia bridge "built" gate: bit3 (0x08) of the overworld map-state byte at
# 0x08D1151C. Flag-gated tilemap generation -- set the bit + reload the overworld
# and the bridge is built & passable. FIXED save-block address. NOT a key-item
# flag. See [[bridge-map-state]].
BRIDGE_STATE_ADDR = 0x08D1151C
BRIDGE_STATE_MASK = 0x08

# One-shot intro cutscene "watched" bits (live pre/post diff per scene, 2026-07-01,
# all three verified skipped on a fresh game with only these bits set):
#   0x08D11520 b7 -> Matoya's cave bumping-into-things scene (first entry)
#   0x08D11521 b0 -> King's summon / show-the-crystals scene (forced before
#                    leaving Cornelia the first time)
#   0x08D11521 b1 -> bridge-crossing slash-art scene (first crossing)
# Same save-block map-state region as the bridge byte. Flooding them skips the
# scenes outright (no guard block, no scene trigger).
#
# ALWAYS_SET_FLAGS drives the client _flags_loop: every (addr, mask) is OR'd into
# RAM ~1x/s so the bits survive save/load/new-game. Keep addresses clustered --
# the loop reads one span from min(addr) to max(addr) per tick.
ALWAYS_SET_FLAGS = [
    (BRIDGE_STATE_ADDR, BRIDGE_STATE_MASK, "bridge built"),
    (0x08D11520, 0x80, "Matoya cutscene skipped"),
    (0x08D11521, 0x03, "King summon + bridge cutscenes skipped"),
]

# --- Town "revealed on world map" flags -> auto-hint that town's AP shop offers ---
# The first time the party steps into a town, the game adds it (by name) to the
# overworld map and sets a durable per-town bit in the save block. The client
# watches these bits and, on first observation, HINTS every AP shop offer sold in
# that town (see _shop_hint_loop) so the player learns what the town's shops hold
# without buying blind.
#
# Shop ordinal -> city index, in SHOP_AP_SLOTS / slot_data order (rando.SHOP_AP_SLOTS).
# city ids: 0 Cornelia, 1 Pravoka, 2 Elfheim, 3 Melmond, 4 Crescent Lake,
#           5 Onrac, 6 Gaia.  (Lefein has no AP shop.)
SHOP_CITY = [0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 4, 4, 4, 5, 6, 6, 6]
CITY_NAME = {0: "Cornelia", 1: "Pravoka", 2: "Elfheim", 3: "Melmond",
             4: "Crescent Lake", 5: "Onrac", 6: "Gaia"}

# Per-town "revealed on world map" bit: (city_index, addr, mask). FULLY MAPPED
# live 2026-07-08 via map-menu binary probe (capture_town_flags.py + 0xAA/0xCC/0xF0
# probes of each byte, verified against the Enlarged Map location list). Two
# adjacent "areas discovered" bytes in the durable save block hold every world-map
# marker; the shop towns are:
#   0x08D11528: b4 Elven Castle, b5 Elfheim, b6 Onrac, b7 Gaia
#   0x08D11529: b0 Caravan, b1 Crescent Lake, b2 Castle Cornelia, b3 Cornelia,
#               b4 Western Keep, b5 Pravoka, b6 Melmond, b7 Lufenia
#   (0x08D1152A = dungeon markers -- Cavern of Earth, Chaos Shrine, etc.)
# Addresses are CANONICAL; the client applies the per-session save-block shift via
# self.sa(). All 7 AP-shop towns mapped -> _shop_hint_loop is now fully live.
# City 7 (Lufenia) has NO AP item shops -- it is here only so the Shops GUI tab can
# unlock its magic-shop sub-tab on first visit; _shop_hint_loop finds no AP offers
# for it (D.SHOP_CITY has no 7) and simply marks it hinted, which is harmless.
TOWN_MAP_FLAGS = [
    (0, 0x08D11529, 0x08),   # Cornelia
    (1, 0x08D11529, 0x20),   # Pravoka
    (2, 0x08D11528, 0x20),   # Elfheim
    (3, 0x08D11529, 0x40),   # Melmond
    (4, 0x08D11529, 0x02),   # Crescent Lake
    (5, 0x08D11528, 0x40),   # Onrac
    (6, 0x08D11528, 0x80),   # Gaia
    (7, 0x08D11529, 0x80),   # Lufenia (magic shops only; no AP item shops)
]

# NPC story-event location detection (return-to-castle throne scene).
# GROUND TRUTH (live pre/post diff of accepting the Lute, 2026-06-30):
#   Accepting the Lute from the Princess set EXACTLY two durable bits:
#     - 0x08D1153B b7  -> the Lute KEY ITEM (shuffled; NOT a detector)
#     - 0x08D1151C b4 (0x10) -> the Princess event flag (unique, chest-safe:
#       a chest granting Lute sets the 0x1153B bit, NOT this one)
#   Bits 1,2 (0x06) of 0x1151C were already set BEFORE the Lute (King throne
#   scene), so Princess is cleanly isolated by bit4 alone.
# Pre-Garland 0x1151C==0x00 (prior 3-phase table); after the post-Garland throne
# it was 0x0E (bits1,2 King + bit3 bridge) with NO Lute/bit4; the Lute then added
# ONLY bit4. So King and Princess are cleanly separable:
# King was REMOVED as a location: the bridge is always built now, so there is no
# King-reward event to check.
# Princess now fires when the player NORMALLY receives the Lute -- detected by the
# Lute KEY-ITEM bit 0x08D1153B & 0x80, which sets exactly at the princess scene.
# (Previously used 0x1151C b4, which lagged to the next chest open.)
LUTE_KEYITEM_ADDR  = 0x08D1153B
LUTE_KEYITEM_MASK  = 0x80          # b7: Lute key item -> set at princess-gives-Lute
# ordinal must match LOGIC.NPC_LOCATIONS / ids.npc_loc_id (Princess kept at 1)
PRINCESS_NPC_ORDINAL = 1

# --- lute_tablets: "Lute Tablets N of M" line in the Key Items menu ----------
# The menu lists a key item iff its possession bit is set, and for the Lute that
# bit IS the endgame gate -- setting it early would hand the player the Lute at
# one tablet. So the progress line rides a SPARE bit instead.
#
# The key bitfield is 36 items over 5 bytes 0x08D11537..3B, backward/MSB-first
# (see key-item-flags memory), leaving ids 37..40 as the 4 low bits of 0x11537.
# LIVE-VERIFIED 2026-07-23: setting id 37 adds a clean, gate-inert menu entry
# (the name getter's cat-0 bound is slti 0x26 = 38, so 37 is in range) and that
# entry ALIASES the Lute's name string (key id 1 / bank entry 0). Since id 1 and
# id 37 are never both set, they safely SHARE that string: pre-assembly we set
# id 37 and write the ratio there; at assembly we clear id 37, restore "Lute",
# and the existing code sets the real id 1 bit.
LUTE_TABLET_SLOT_ADDR = 0x08D11537
LUTE_TABLET_SLOT_MASK = 0x08       # b3 = spare key id 37
# Glyph width the KEY_NAME entry-0 slot is padded to on disc (must equal
# extern_bake.KEY_NAME_GLYPHS). The menu draws off[i+1]-off[i] bytes and IGNORES
# the terminator, so every write must fill the whole slot with spaces.
LUTE_TABLET_SLOT_GLYPHS = 24

# Bikke (Provoka): story-flag id5 = byte 0x08D1151C bit5 (0x20). Vanilla doubly-binds
# it: Bikke's defeat sets it AND it spawns the ship at Provoka (RE'd live 2026-07-03,
# see ship-bikke-flag memory). The bikke_ship_split EBOOT feature (PATCHER v65,
# always-on) remaps story-flag id5 -> id63 for flag reads/writes while inside Pravoka
# (FIELD_MAP_ID 0x37), so:
#   id63 (0x08D11523 b7) = "Bikke defeated" -- set natively by his defeat event,
#       read natively by the pirates-presence gate. Persistent; pirates never
#       re-offer the fight. The client sends the Bikke check on ITS rising edge.
#   id5 = "ship available" only -- the client mirrors it from Ship AP-item
#       ownership (set when owned, stripped when not). Stripping no longer
#       respawns the pirates.
# Legacy self-heal: id5 rising WITHOUT the Ship item (old-scheme save or an
# unpatched window) still counts as defeat evidence -> check + set id63 + strip.
SHIP_FLAG_ADDR = 0x08D1151C
SHIP_FLAG_MASK = 0x20

# Equipment Rune Key (equipment_runes yaml): story flag 62 = "Key assembled".
# Unused vanilla (native story flags are ids 0-48; id 63 is Bikke's split bit in
# the SAME byte, b7). The client sets it once equipment_runes_required Equipment
# Rune copies are held; the on-disc equipment_rune_gate cave (iso_patcher) reads
# it via the live save-struct ptr to decide whether activatable equipment may be
# used as a battle item. Save-persistent; never stripped. NOT a key-item bit:
# key ids 38-40 are storage-spare but the menu name getter's cat-0 bound
# (slti 0x26 = 38) rejects them, so a set bit there would draw garbage.
RUNE_KEY_FLAG_ADDR = 0x08D11523        # flag byte 7 = 0x1151C + (62 >> 3)
RUNE_KEY_FLAG_MASK = 0x40              # bit 62 & 7 = b6

# bonus_dungeon_crystals yaml: 4 client-owned "crystal activated" shadow bits, one
# per Soul-of-Chaos dungeon (dg 0 Earthgift/Earth, 1 Hellfire/Fire, 2 Lifespring/
# Water, 3 Whisperwind/Air), stored in bits 0-3 of the dead slot-magic reserve byte
# save+0x834 (absolute 0x08D11934, the ex-v183 INT-accrual gap between Soma @0x830
# and the marker @0x838). This byte is the client's OWN -- nothing else reads or
# writes it (verified), so unlike the story-flag array (byte 0x11522 is shared with
# RUNE_BORROW_OWNED flag 55) there is NO read-modify-write race. It rides the save
# ->slot preview copy (fill span save+0x43C..0x840) so it persists across save/load
# + into slot files; it is OUTSIDE both slotbox canary bands (0x7EC..0x807 and the
# 0x839..0x83B pad), so client writes here never trip the canary. The client ORs
# bit (0x01 << dg) when it detects that dungeon's END boss die (a whitelisted
# end-boss enemy, D.BONUS_END_BOSS_IDS[dg], at HP 0 or KO/stone status); the on-disc
# crystals_needed wrapper counts THESE bits instead of the four Fiend flags when
# bonus mode is baked. Re-asserted each tick from a sticky latch (cleared on a fresh
# new game), never stripped. See bonus-dungeon-crystals memory + iso_patcher _CRY_SHADOW.
BONUS_CRYSTAL_SHADOW_ADDR = 0x08D11934   # save+0x834 (dead slot-magic reserve)
def bonus_crystal_shadow_mask(dg):       # dg 0..3 -> bit 0..3 of that byte
    return 0x01 << dg
# DLC bonus-dungeon superbosses: monster ids 0x80-0x90 are the Soul-of-Chaos
# "blue-flame" bosses, but only SOME of each dungeon's blue-flame bosses sit at the
# END (clearing the dungeon = lighting its crystal); the rest are MIDPOINT bosses
# that must NOT credit the crystal (user-confirmed 2026-08-06). So detection is a
# per-dungeon END-boss whitelist, NOT a range -- a midpoint kill (e.g. Cerberus in
# Hellfire) leaves the crystal dark, and only an end boss of the current dungeon
# lights it. dg -> element: 0 Earthgift/Earth, 1 Hellfire/Fire, 2 Lifespring/Water,
# 3 Whisperwind/Air. Rematch Fiend forms (0x73-0x7e) are excluded (below 0x80).
BONUS_END_BOSS_IDS = {
    0: frozenset({0x80, 0x81, 0x82, 0x83}),  # Earthgift : Echidna, Cerberus,
                                             #             Ahriman, Two-Headed Dragon
    1: frozenset({0x87, 0x88}),   # Hellfire   : Barbariccia, Rubicante
    2: frozenset({0x8A, 0x8B}),   # Lifespring : Omega, Shinryu
    3: frozenset({0x90}),         # Whisperwind: Death Gaze
}

# Equipment-Rune Key Items MENU line: a DEDICATED entry (user 2026-07-27: no
# sharing with the Lute Tabs line, which clipped in the right column). There is
# no second SPARE id -- 38 fails the name getter's cat-0 bound (slti 0x26
# @0x088d4750) AND its cat0-array slot 0x08953604 lies inside the cat-1
# consumable array -- so we BORROW a real key id for DISPLAY ONLY (the gate is
# story flag 62, above): id 35 "Battery Circuit", a Whisperwind Cove robot part.
# Safe because ids 18-36 are touched by NOTHING outside the bonus dungeons, and
# id 35 specifically can only be GRANTED inside Whisperwind Cove (its robot-part
# minigame). So the un-hijack (bit cleared + native name/desc restored) is scoped
# to that ONE dungeon -- RUNE_BORROW_RELEASE_BANDS below -- not to every bonus
# floor: releasing it in Earthgift / Hellfire / Lifespring blinded the counter
# across 35 floors that can never touch the bit (user report 2026-08-06, where
# runes are FOUND in those dungeons and the gate is what the player is watching).
# Ownership is no longer a heuristic: RUNE_BORROW_OWNED_* below records that the
# set bit is OURS, so a natively-earned part is never stolen and our own borrow
# is never abandoned. Its KEY_NAME entry is disc-padded via the same pad_key_ids
# bake as the Lute slot.
RUNE_MENU_SLOT_KEY_ID = 35             # "Battery Circuit"
RUNE_MENU_SLOT_ADDR = 0x08D11537       # = key_item_bit(35)
RUNE_MENU_SLOT_MASK = 0x20             # id 35 = bit5 (id37 spare = bit3, same byte)
# Provable ownership of the id-35 display borrow: story flag id 55.
#   0x1151C + (55 >> 3) = 0x11522, mask 1 << (55 & 7) = 0x80.
# Native story flags are ids 0-48, and byte 0x11522 is written by NOTHING else
# in this client -- deliberately NOT 0x11523, which _npc_loop read-modify-writes
# through a tick-stale _ByteSnapshot for flags 62 and 63; a lost update there
# would clobber Bikke's bit (BIKKE_DEFEATED_*, which does not self-heal).
# Lives in the save struct, so it rolls back WITH the key-item bit on any
# save/load -- the two can never desync.
RUNE_BORROW_OWNED_FLAG_ID = 55
RUNE_BORROW_OWNED_ADDR = 0x08D11522
RUNE_BORROW_OWNED_MASK = 0x80
# ids 18-36 minus 35: "does the player hold any OTHER bonus-dungeon key item?"
# Sole use is the one-shot legacy-save adoption test in _keyratio_loop (saves
# written before the shadow flag existed carry no ownership evidence).
RUNE_BORROW_PEER_KEY_IDS = tuple(i for i in range(18, 37)
                                 if i != RUNE_MENU_SLOT_KEY_ID)
# Post-assembly the slot stays shown as a permanent item instead of vanishing.
# The client rewrites both strings in place each tick; the LOCKED description is
# the one patch_iso bakes (it is the longest, so it sizes the slot every other
# string must fit). RUNE_SLOT_NATIVE_DESC restores the vanilla text while the
# player is on a bonus floor, where the borrowed item must read truthfully.
RUNE_KEY_NAME = "Rune Key"
RUNE_KEY_DESC = "Allows you to activate magical equipment in battle."

# Levistone-Shard Key Items MENU line (levistone_shards yaml): same DEDICATED-
# entry approach as the rune line above, borrowing id 36 "Energy Chip" -- the
# LAST real key id, so the line draws immediately beside the id-37 Lute Tabs
# line (user 2026-08-12: "next to Lute Tabs"; the menu draws in id order).
# Energy Chip is another Whisperwind Cove robot part, so the release zone and
# every safety argument are identical to the rune borrow (_rune_borrow_zone is
# shared). DISPLAY ONLY: the gate is the shard counter in ApClient (_key_won).
# At assembly the borrow is RELEASED for good -- the real Levistone entry
# (id 11, possession set by the map-reset row) takes over; no duplicate line.
# Unlike the rune borrow there is NO legacy-save adoption heuristic: this
# feature ships WITH its shadow flag, so a set bit whose shadow is clear is
# always the natively-earned part. KEY_NAME entry disc-padded via pad_key_ids.
SHARD_MENU_SLOT_KEY_ID = 36            # "Energy Chip"
SHARD_MENU_SLOT_ADDR = 0x08D11537      # = key_item_bit(36)
SHARD_MENU_SLOT_MASK = 0x10            # id 36 = bit4 (id35 = bit5, id37 = bit3)
# Provable ownership of the id-36 display borrow: story flag id 54 --
#   0x1151C + (54 >> 3) = 0x11522, mask 1 << (54 & 7) = 0x40.
# Same byte as RUNE_BORROW_OWNED (flag 55): written by NOTHING else, and both
# flags are only touched from the single _keyratio_loop, so there is no
# read-modify-write race between them. Rolls back WITH the key-item bit on
# save/load, so the two can never desync.
SHARD_BORROW_OWNED_FLAG_ID = 54
SHARD_BORROW_OWNED_ADDR = 0x08D11522
SHARD_BORROW_OWNED_MASK = 0x40
RUNE_SLOT_NATIVE_DESC = "Bridge for connecting battery and chip."
BIKKE_NPC_ORDINAL = 2
BIKKE_DEFEATED_FLAG_ID = 63            # story flag id chosen unused (ids 0-48 native)
BIKKE_DEFEATED_ADDR = 0x08D11523       # flag byte 7 = 0x1151C + (63 >> 3)
BIKKE_DEFEATED_MASK = 0x80             # bit 63 & 7

# Crescent Lake sage: natively gives the CANOE (key item id17 = 0x08D11539 & 0x80), but
# ONLY after the Earth Crystal / Lich (confirmed live 2026-07-04; first visit sets no
# durable flag -- 0x08D12268 that changes is a volatile step byte). In the rando the Canoe
# is a POOL item and the sage is a randomized NPC location. INTERIM detector = the canoe
# key-item bit itself (like the princess/Lute): when the bit is set while the Canoe AP item
# is NOT owned, send the Sage check + strip the free native canoe. The not-owned gate stops
# the client's own Canoe DELIVERY from false-firing the sage check.
#   KNOWN WART (clean fix deferred -- the sage's own durable event flag; RE is blocked on
#   needing the Earth Crystal to make the sage fire): if the player finds the AP Canoe
#   BEFORE reaching the sage, the sage's native grant reads as delivery and the Sage check
#   is MISSED -> potential unreachable location (release-blocker). See open-progression-rework.
CANOE_KEYITEM_ADDR = 0x08D11539
CANOE_KEYITEM_MASK = 0x80
# Canoe FUNCTIONAL gate (river/shallow sailing). PROVEN LIVE 2026-07-05 POC: the
# possession bit above is COSMETIC (Key Items menu only) -- clearing it still sails;
# clearing THIS bit stops sailing instantly (no overworld reload). The overworld
# movement code gates canoe traversal on this sage-event bit, which the sage sets
# alongside possession when handing the canoe over. Consequences for the rando:
#   - DELIVERING the Canoe AP item must set this bit too (else the menu shows a
#     canoe the player cannot actually use).
#   - STRIPPING the sage's native canoe must clear this bit too (else the player
#     keeps free sailing after the possession bit is stripped).
# See flag-collection-progress / ap-keyitem-grant-infra memories.
CANOE_FUNCTION_ADDR = 0x08D1151E
CANOE_FUNCTION_MASK = 0x04
SAGE_NPC_ORDINAL   = 3

# Ice Cave Levistone (NPC-style location; NOT a chest). The Ice Cave Levistone is an
# EVENT PICKUP -- grabbing it sets NO bit in the chest-open bitfield (0x114EC..), so
# the poll-based chest loop can never see it. Instead its "obtained" event bit is
# 0x08D1151E b4 (0x10), which the game sets exactly when the player picks up the
# Levistone (LIVE-CONFIRMED 2026-07-05; see flag-collection-progress). Detect the AP
# location on that bit's rising edge, then GATE on the randomized Levistone AP item:
# when the bit is set while the Levistone AP item is NOT owned, that = the native
# pickup (not our own delivery, which sets it via KEY_ITEM_FUNCTION_BITS), so send the
# check + strip the free native Levistone (possession + BOTH event bits) so it is
# obtainable only as its randomized AP item. Same not-owned gate + strip pattern as the
# Sage/Canoe. The Levistone possession bit (0x1153A b5) is derived via key_item_bit(11);
# the two event bits are 0x1151E b4 (obtained) and b5 (airship-raised). WART (same class
# as the Sage): finding the AP Levistone BEFORE reaching the Ice Cave spot reads as our
# delivery -> the location check is missed. Clean fix = the pickup's own durable flag /
# FIF edit (deferred). See ap-keyitem-grant-infra / flag-collection-progress memories.
LEVISTONE_EVENT_ADDR   = 0x08D1151E   # obtained-event register byte
LEVISTONE_EVENT_MASK   = 0x10         # b4: Levistone picked up (location detector)
LEVISTONE_AIRSHIP_MASK = 0x20         # b5: airship raised (function; same byte)
LEVISTONE_NPC_ORDINAL  = 4

# --- Six more promoted key items (2026-07-06) -------------------------------------
# Each was previously a logic-only EVENT token; now each is a REAL AP pool item whose
# ORIGINAL in-game source becomes a randomized location. Star Ruby uses the CHEST
# system (its Earth Cave chest sets a normal treasure bit 0x114F1 b1 -> handled by
# the poll-based chest loop, no NPC block); the other five are NPC-poll locations
# detected exactly like the Levistone above: read the detector event bit (sa-wrapped);
# if set AND the matching AP item is NOT owned, that = the game's native grant (not our
# own delivery), so send the check once + STRIP the free native key item (possession bit
# via key_item_bit + the detector event bit) so the item is obtainable ONLY as its
# randomized AP item. The not-owned gate stops the client's own delivery from
# false-firing. Detector bits captured LIVE 2026-07-05 (flag-collection-progress memory).
#
# GRANT POLICY: these are POSSESSION-ONLY grants. They are deliberately ABSENT from
# KEY_ITEM_FUNCTION_BITS below -- their possession-vs-function splits are UNVERIFIED,
# and prematurely setting an item's acquisition-event bit can suppress the very NPC
# meant to be a location. A live "have-everything -> strip -> receive-back" sweep will
# resolve each split (POC checklist in the wiring report); until then grant_key_item
# sets possession only.
#
# Same inherited WART as Sage/Levistone: receiving the AP item BEFORE reaching the
# native spot reads as our delivery, so the location's check is missed (deferred;
# accessibility:full mitigates in fill). Ordinals 5..9 (1=Princess .. 4=Levistone).
# Canal blown open (Nerrick took the Nitro Powder) -- the tilemap map-state bit,
# twin of the bridge bit 0x1151C b3. Gates the canal BRIDGE grid art in
# _openworld_loop; see [[canal-flag-bit]] / [[canal-shallows-plan]].
CANAL_OPEN_ADDR_SA     = 0x08D1151D   # bit 0x08 (openworld_data.CANAL_OPEN_BIT)
EARTH_ROD_EVENT_ADDR   = 0x08D1151D   # b7: Sarda handed over the Earth Rod
EARTH_ROD_EVENT_MASK   = 0x80
EARTH_ROD_NPC_ORDINAL  = 5

CHIME_EVENT_ADDR       = 0x08D1151F   # b7: Lefein elder gave the Chime
CHIME_EVENT_MASK       = 0x80
CHIME_NPC_ORDINAL      = 6

WARP_CUBE_EVENT_ADDR   = 0x08D11520   # b0: Waterfall Cavern robot gave the Warp Cube
WARP_CUBE_EVENT_MASK   = 0x01
WARP_CUBE_NPC_ORDINAL  = 7

# ---- NPC map-entry native-refresh (2026-07-13) ----------------------------
# Recurring bug class: an NPC that natively grants key item K is ALSO an AP
# *location*, and K is a SEPARATE randomized AP item placed elsewhere. If the
# player obtains the AP K BEFORE visiting the NPC, K's grant sets the SAME
# event/function bit the NPC reads as its "already gave it" gate -> the NPC
# refuses to fire -> the NPC's AP location is unreachable (softlock) and its
# obtain-box never shows. First hit live 2026-07-13: Warp Cube / Waterfall
# Cavern robot -- 0x11520 b0 is BOTH the warp function bit AND the robot's
# done-gate, so the shop-bought AP Warp Cube suppressed the robot (which owed
# the player Spell Tome: Flare). The legacy _npc_loop batches gate detection on
# "AP K not owned", which is exactly blind to this obtain-early ordering.
#
# ARCHITECTURE ("map-entry native-refresh"): make the NPC behave like vanilla.
# On ENTERING the NPC's map with its AP location still unchecked, EDGE-clear the
# gate bit once so the NPC is willing to fire again; then watch the bit RISE
# (the NPC firing on interaction) as the detector -> send the location check ->
# the AP location item is delivered by the normal received-items pipeline and
# the obtain-box shows the AP name via _mapmsg_loop. Strip the free native K
# ONLY if the player has NOT legitimately won the AP K (owned -> leave K + the
# gate bit, which doubles as the function bit, so the warp keeps working). On
# LEAVING the map before the NPC fired, restore the bit if K is owned so the
# function works elsewhere. Net experience: "talk to NPC -> box names the AP
# item -> receive that AP item", with no free duplicate key item, regardless of
# whether K was obtained before or after visiting the NPC.
#
# TWO NPC FLAVOURS, learned the hard way (live RE 2026-07-16, Sarda):
#
#   (a) TALK-TIME gate: the NPC re-reads the gate bit when you interact, so
#       clearing it on the map-entry tick is soon enough. (The Waterfall robot
#       was the example here until 2026-08-05 -- it is NOT one; see its row.)
#   (b) MAP-LOAD gate (Sarda): the NPC's "already gave it" state is fixed when
#       the MAP LOADS. Clearing the bit on the entry tick is ONE MAP LOAD TOO
#       LATE -- he still refuses, and only offers after you walk out and back in
#       (live-reproduced). So his bit must already be clear BEFORE his map loads.
#       That is what `prearm` does: while the player stands on the OVERWORLD
#       within `radius` tiles of the cave mouth, hold the gate clear, so entering
#       loads a willing NPC on the FIRST visit.
#
# The prearm window is deliberately a small radius, NOT "anywhere on the
# overworld": the gate bit doubles as the Earth Rod's FUNCTION bit (the Lich /
# Earth Crystal gate in the Cavern of Earth), and a blanket clear would risk
# breaking that the same map-load way, just mirrored. Clearing only at his cave
# mouth keeps the bit set everywhere the rod actually has to work.
#
# `hold_poss`: whether to also hold the POSSESSION bit clear while in-map. The
# robot needs it (its already-gave check reads possession too). Sarda does NOT
# -- live experiment 2026-07-16: gate CLEAR + possession SET -> he hands over.
# So the Earth Rod stays visible/usable in the Key Items menu throughout, which
# is what we want for a key item the player legitimately won from the AP pool.
#
# Rows: (name, map_id (FIELD_MAP_ID_SA u32, captured live), gate_addr, gate_mask,
#        key_id, npc_ordinal, hold_poss, prearm, consumed).
#        prearm = (ow_x, ow_y, radius)
#        on the overworld, or None for talk-time NPCs. Add a row per NPC once its
#        map id is captured; a migrated NPC MUST be removed from the legacy
#        not-owned-gated batch below.
# prearm sentinel: hold the gate clear in EVERY map (not just at an overworld
# doorstep tile) until the NPC's AP location is checked. For map-load-gated
# pickups with no overworld approach tile to scope to -- see the Adamantite row.
# Used as ("global", exempt_map_id, ...) -- in the exempt maps the hold is
# skipped and the won-key restore applies, so a function-bit consumer living in
# a DIFFERENT room (the Dwarf Smith reading the Adamantite bit) still works
# while the pickup's own location is unchecked.
PREARM_GLOBAL = "global"

# ("ow",): hold ONLY while the player is on the OVERWORLD (FIELD_MAP_ID 0xFF).
# For a map-load-gated NPC whose map is entered exclusively from the overworld
# but whose gate bit is a function bit CONSUMED IN OTHER INTERIORS: the ow hold
# makes the NPC's map always load un-gated (the ow IS its doorstep), while every
# other map falls through to the won-key restore, so the key stays in the menu
# and functional there. The interior consumer is safe from the load-binding race
# as long as it does not sit on the FIRST floor entered from the overworld: the
# entry floor binds with bits clear (held on the ow a tick earlier), the restore
# sets the bits within one tick inside, and each deeper floor transition is a
# fresh map load that binds with the bits SET. See the Warp Cube row.
PREARM_OVERWORLD = "ow"

# FLOOR on every radius doorstep (the (x, y, radius) prearm tuples below).
# Chebyshev, so 10 = a 21x21 square around the cave mouth. The per-row radii
# were authored at 8 one at a time and 8 is demonstrably too tight: a player
# approaching Sarda's cave along an angle the doorstep does not cover entered
# with the gate still SET, got the entry-tick clear instead of the prearm, and
# had to walk out and back in TWICE before the check fired (live 2026-08-08,
# Prime). Widening is close to free -- a prearm hold only does anything while
# the row's key is WON and its location UNCHECKED, and the moment the player
# steps off the overworld the won-key restore puts every bit back -- so the
# cost of a too-WIDE net is a few extra tiles of held function bit, while the
# cost of a too-NARROW one is a location the player cannot reach. Raise the
# floor here rather than editing each tuple: the tuples stay honest about
# where the actual doorstep is.
PREARM_MIN_RADIUS = 10

# Player-facing explanation for a prearm hold that is currently keeping a WON
# key's possession bit clear (prearm_poss rows only). Without this the player
# sees an AP item arrive, finds nothing in the Key Items menu, and reports it as
# a lost item -- live 2026-08-07: Warp Cube won from a Mount Gulg chest, robot
# location unchecked, cube invisible. Keyed by the row's display name.
PREARM_HOLD_HINT = {
    "Warp Cube": ("it works normally everywhere (the Mirage Tower warp "
                  "included); it only slips out of Key Items while you are "
                  "right at the Waterfall Cavern falls, until the robot "
                  "inside has given his check"),
}

# 9th field `consumed`: (addr, mask) of a durable "downstream turn-in done" bit,
# or None. Once that bit is set the key has served its purpose forever -- the
# won-key restore then holds the POSSESSION bit clear (instead of re-setting it
# every tick) so the spent key stops cluttering the key-item menu. The gate/
# function bit is still restored (native-consistent). Live 2026-07-20: the
# Smith consumed the Adamantite but the restore loop resurrected it in the menu.
#
# OPTIONAL 10th field `restore_mask`: bits the won-key restore sets when the
# player is out of the NPC's map / the location is checked. Defaults to
# gate_mask. Split them when the gate bit lives in a byte with OTHER function
# bits that must NEVER be held clear -- gate_mask is what we DETECT and HOLD
# CLEAR, restore_mask is what a won key must have SET. See the Levistone row
# (holding its airship bit clear made the airship vanish, live 2026-07-27).
#
# OPTIONAL 11th field `prearm_poss`: also hold the POSSESSION bit clear during
# the prearm window (default False -- prearm clears the gate only). Only for
# NPCs whose already-gave check reads possession AND binds at map load: the
# robot. Kept opt-in so a global prearm on another row cannot yank a key item
# out of the menu / out of its function everywhere.
NPC_MAP_RESET = [
    # PREARM ADDED 2026-08-05 (prearm was None; the "(a) talk-time" reading
    # below was wrong once the player actually OWNS an AP Warp Cube). Live
    # player report: cube won from Onrac Item Shop AP Stock, walked into
    # Waterfall Cavern -> "[warp cube] cleared gate+possession in-map (entry)"
    # in the log, robot still answered *buzz* *whirr* and refused. The robot's
    # script BINDS AT MAP LOAD like every other NPC: the out-of-map won-key
    # restore had b0 + possession SET when 0x4E loaded, so the already-gave
    # branch bound and the entry-tick clear was one map load too late. Leaving
    # and re-entering cannot help -- the restore re-sets the bits on the way
    # out. TENTH instance of the map-load-binding race.
    # RADIUS DOORSTEP (2026-08-08; 2026-08-07 was PREARM_OVERWORLD, before that
    # PREARM_GLOBAL with no exempts). The global hold made a WON AP cube fully
    # inert -- possession held clear everywhere (no Key Items entry, "my cube
    # never arrived" live report) and the Mirage Tower -> Flying Fortress warp
    # dead until the robot's check fired, silently adding "reach the robot
    # (Canoe)" as a fortress prerequisite the game and the logic do not have.
    # The ow-wide hold fixed function but still hid the cube on the whole world
    # map -- misleading. Doorstep tile finally captured live 2026-08-08 with the
    # party canoeing AT the falls mouth: (47,22); center pushed south to (47,27)
    # r8 so the window spans the mouth plus the river approach (X 39-55,
    # Y 19-35) -- the only direction the cavern can be entered from.
    # Guarantees, same as before:
    #   * Waterfall Cavern is entered ONLY through that river: the map always
    #     LOADS from inside the window, gate+possession clear -> robot offers.
    #   * Everywhere else (all interiors AND the rest of the overworld) the
    #     won-key restore keeps possession + b0 SET -> cube in the menu, Mirage
    #     warp works without the robot visit (fortress floors bind from
    #     interior loads with the bits already set).
    # Cost: cube hides from Key Items only within ~8 tiles of the falls.
    # PREARM_HOLD_HINT explains that once per session.
    # prearm_poss=True: hold_poss is True for a reason (the robot's check reads
    # possession too), so the prearm window must clear possession as well or
    # the map still loads gated.
    ("Warp Cube", 0x0000004E, WARP_CUBE_EVENT_ADDR, WARP_CUBE_EVENT_MASK,
     14, WARP_CUBE_NPC_ORDINAL, True, (47, 27, 8), None,
     WARP_CUBE_EVENT_MASK, True),
    # Sarda / Sage's Cave (2026-07-16): SAME collision as the robot -- 0x1151D b7
    # is BOTH the Earth Rod function bit (KEY_ITEM_FUNCTION_BITS[10], gates Lich)
    # and Sarda's "already handed the rod over" gate. An AP Earth Rod obtained
    # before visiting him (live: Mount Gulg chest) set b7 -> Sarda never fired ->
    # "Sage's Cave - Sarda" unreachable. Map id 0x2F + cave-mouth overworld tile
    # (23,184) captured live. He is flavour (b), hence the prearm.
    # Earth Rod is NOT consumed-strippable: its function bit gates Lich/Earth
    # Crystal for the rest of the game, and the rod stays in the vanilla menu too.
    ("Earth Rod", 0x0000002F, EARTH_ROD_EVENT_ADDR, EARTH_ROD_EVENT_MASK,
     10, EARTH_ROD_NPC_ORDINAL, False, (23, 184, 8), None),
    # Adamantite / Flying Fortress (2026-07-19): THIRD instance of the same
    # collision (robot, Sarda, now this). 0x11520 b1 is BOTH the Adamantite
    # function bit (KEY_ITEM_FUNCTION_BITS[7], gates the Smith turn-in) and the
    # fortress pickup's "already collected" gate. An AP Adamantite obtained
    # before visiting the fortress (live 2026-07-19: Mirage Tower Chest 11) set
    # b1 -> the pickup never appears -> "Flying Fortress - Adamantite"
    # unreachable, and the old not-owned-gated poll in _npc_loop was suppressed
    # by the very same ownership, so the check could never fire either.
    # Map id 0x3A captured live standing in the pickup room. hold_poss=False:
    # the pickup gates on the event bit only (same as Sarda), so the AP
    # Adamantite stays visible in the key-item menu the whole time.
    # PREARM_GLOBAL, not None: the pickup's presence is decided at MAP LOAD, and
    # the fortress is reached by airship + interior stairs, so there is no
    # overworld doorstep tile to scope a radius prearm to. With prearm=None the
    # out-of-map branch re-SETS b1 the instant you leave, so the walk-out/walk-in
    # meant to redraw the pickup reloads the room gated shut -- dead forever.
    # The global hold EXEMPTS Mount Duergar (0x2E) AND the overworld (0xFF):
    # in those maps the won-key restore re-SETS b1 so the Dwarf Smith accepts a
    # won Adamantite even while the fortress pickup is still unchecked/unreachable.
    # BOTH are required (0xFF added 2026-07-25): NPC dialog scripts BIND AT MAP
    # LOAD (proven via the sage RE, see [[sage-talk-detect]]) -- the Smith reads
    # b1 when his cave LOADS, not when you talk, so exempting 0x2E alone lost the
    # race (his cave loaded from the overworld where b1 was held clear -> refuse
    # script bound; the in-map restore set b1 a tick too late). Live bug 2026-
    # 07-25: player received the AP Adamantite before reaching the Flying Fortress
    # (needs the Warp Cube), owned it + stood at the Smith, and he perpetually
    # refused. Exempting the overworld makes his cave LOAD with b1 already set.
    # Safe for the fortress pickup: 0x3A is reached by the Warp Cube from Mirage
    # Tower's summit, never entered directly from the overworld, and Mirage Tower
    # is non-exempt (held clear), so the pickup room still loads with b1 clear.
    # (literals, not the ADAMANTITE_*/OVERWORLD names: those are defined BELOW
    # this table.)
    # consumed = Smith-forged bit 0x11520 b3 (SMITH_EVENT_*, defined below):
    # after the forge the ore is spent -- hold possession clear so it leaves
    # the key-item menu (vanilla consumes it too; live 2026-07-20 the restore
    # loop kept resurrecting it).
    ("Adamantite", 0x0000003A, 0x08D11520, 0x02, 7, 13, False,
     (PREARM_GLOBAL, 0x2E, 0xFF), (0x08D11520, 0x08)),
    # Astos / Western Keep (2026-07-20): FOURTH instance of the collision.
    # 0x1151C b7 is BOTH the Crystal Eye function bit (KEY_ITEM_FUNCTION_BITS[3],
    # Matoya's trade gate) and the Western Keep king's "Astos already dealt with"
    # despawn gate, and the despawn is decided at MAP LOAD (live 2026-07-20:
    # clearing b7 did nothing until a walk-out/walk-in; then the king was back).
    # An AP Crystal Eye received before the fight set b7 -> king gone forever ->
    # "Western Keep - Astos" unreachable; on top of that the legacy Classic-7
    # poll's `or won` gate swallowed the check even after a genuine kill (the
    # Smith lesson again). Migrated OUT of that poll to this table.
    # Map id 0x33 + doorstep tile (95,179) captured live 2026-07-20 (mapid_watch
    # / df_probe). hold_poss=True: whether the king's event ALSO reads
    # possession is unverified, so hold it clear in-map like the robot -- the
    # cost is the eye leaving the key-item menu only while inside the keep.
    # Radius prearm (not GLOBAL): the keep has a real overworld doorstep, and
    # b7 must stay SET elsewhere -- it is Matoya's trade gate.
    # consumed=None on purpose: the eye's natural sink is Matoya's trade, but
    # her "done" bit is the Jolt Tonic function bit (0x1151D b0), which an AP
    # Jolt Tonic grant ALSO sets -- using it would strip the eye's menu entry
    # on an unrelated AP delivery. A lingering spent eye is cosmetic; fine.
    # (literal ordinal 10 = CRYSTAL_EYE_NPC_ORDINAL, defined below this table.)
    ("Crystal Eye", 0x00000033, 0x08D1151C, 0x80, 3, 10,
     True, (95, 179, 8), None),
    # Mystic Key / Elf Prince: NO ROW. Formerly the FIFTH instance (0x1151D b1
    # = both the Mystic Key function bit and the quest's already-done gate),
    # and the row's in-map hold deadlocked the four Elven Castle Mystic Key
    # doors (live 2026-08-08: won key + no Jolt Tonic -> hold never ends).
    # Cured at the ROOT by iso_patcher.apply_prince_gate_split (v247,
    # ON_DISC_ALWAYS): the quest chain's set/dialogue operands are repointed on
    # disc to shadow flag 69 (NPC_GATE_SPLIT_FLAG_BASE + key id 5), so flag 9
    # belongs to the doors + AP grant alone and the quest runs on its own flag.
    # v250: doors no longer read flag 9 AT ALL -- mystic_door_gate drops every
    # locked-door record on POSSESSION, so the quest and the doors are not the
    # same mechanism and cannot fight. (v247 repointing this site had left every
    # Elfheim door shut for a won key, live 2026-08-10; v249's block surgery is
    # deleted.) Client detection = ELF_PRINCE_QUEST_* rise (ApClient
    # _npc_loop "prince-quest" stage); key id 5 sits in OWNED_FUNCTION_REASSERT
    # so a won key keeps the doors open across save reloads. Full RE
    # 2026-08-09: re_only/prince_gate_probe.py + [[npc-gate-holds-can-deadlock
    # -in-map]]. This is the TEMPLATE for retiring the other collision rows
    # (Levistone next): probe the NPC's check/set sites, add _NGS_SITES rows,
    # delete the table row here.
    # Levistone / Ice Cavern pickup (2026-07-20): SIXTH instance. 0x1151E b4
    # (obtained) is BOTH the Levistone's detector AND part of its function mask
    # (KEY_ITEM_FUNCTION_BITS[11] = 0x30, obtained b4 + airship-raised b5): an
    # AP Levistone received before reaching the Ice Cavern spot set b4 -> the
    # floor pickup never appears (live player report 2026-07-20, same class as
    # Adamantite) and the old not-owned-gated _npc_loop poll was suppressed by
    # the same ownership. Map id 0x24 captured live standing at the spot.
    # gate MASK = 0x10 (the detector/obtained bit ONLY) + restore_mask 0x30:
    # the won-key restore is the ONLY thing asserting the airship bits (11
    # removed from OWNED_FUNCTION_REASSERT -- it would fight the hold every
    # tick), so it must set the FULL function mask or a won Levistone never
    # raises the airship. But the HOLD must NOT touch b5: with gmask 0x30 the
    # global prearm cleared airship-raised in every non-overworld map, and the
    # overworld draws the airship AT MAP LOAD -- so stepping out of any cave
    # reloaded the overworld with b5 clear and THE AIRSHIP WAS GONE (live
    # 2026-07-27; restore set b5 a tick too late, hence "leave and re-enter a
    # few times and it comes back"). Same map-load-binding race as the Smith.
    # (PREARM_GLOBAL, 0xFF): floor pickup presumed MAP-LOAD gated
    # like the Adamantite (no doorstep tile captured; global is safe + simpler).
    # Exempt = the OVERWORLD (0xFF literal -- OVERWORLD_FIELD_MAP_ID is defined
    # below this table): the airship is drawn/boarded only on the overworld, so
    # the restore there keeps a won Levistone's airship flying while the Ice
    # Cavern location is still unchecked. hold_poss=True: whether the pickup's
    # already-got check reads possession is unverified -- conservative; costs
    # the menu entry only while standing in the pickup room. consumed=None:
    # the Levistone is never spent (it IS the airship key).
    ("Levistone", 0x00000024, LEVISTONE_EVENT_ADDR, LEVISTONE_EVENT_MASK,
     11, LEVISTONE_NPC_ORDINAL, True, (PREARM_GLOBAL, 0xFF), None,
     LEVISTONE_EVENT_MASK | LEVISTONE_AIRSHIP_MASK),
    # Oxyale / Gaia fairy (2026-07-21): SEVENTH instance. 0x1151F b1 (fairy
    # freed) is BOTH the Oxyale location detector AND one bit of its function
    # mask (KEY_ITEM_FUNCTION_BITS[16] = 0x0E, b1 freed + b2 mermaid gate +
    # b3 sub-ready): an AP Oxyale received before visiting the freed fairy ->
    # she gives her "has the oxyale helped you out?" already-gave line and the
    # old not-owned-gated batch poll was suppressed by the same ownership
    # (live player report 2026-07-21). Gaia FIELD_MAP_ID 0x4B captured live at
    # the spring. Gate MASK = 0x0C (b2+b3), NOT b1: b1 (fairy freed) is her
    # SPAWN/dialogue precondition -- holding it clear left her fluttering
    # silently with no talk event at all (live 2026-07-21, first attempt used
    # 0x0E). Her already-gave read must be in b2/b3; b1 stays owned by the
    # normal delivery path (KEY_ITEM_FUNCTION_BITS[16] still 0x0E). 16 removed
    # from OWNED_FUNCTION_REASSERT (fought the hold), so this row's won-key
    # restore asserts b2+b3 (the Onrac mermaid/sub gate) for a won Oxyale;
    # only b1 is left to the one-time grant. Holding b2/b3 clear costs nothing
    # inside Gaia (the sub is in Onrac). prearm=None: talk-time flavour
    # presumed (Elf Prince pattern); if live test shows map-load, add a Gaia
    # doorstep radius prearm. hold_poss=True conservative.
    # (Literals: OXYALE_* constants are defined below this table.)
    ("Oxyale", 0x0000004B, 0x08D1151F, 0x0C, 16, 9, True, None, None),
    # Chime / Lefein elder (2026-08-02): NINTH instance, and the last of the
    # legacy not-owned-gated batch that had this collision. 0x1151F b7 is BOTH
    # the Chime function bit (KEY_ITEM_FUNCTION_BITS[12], Mirage Tower / Sky
    # Castle ascend) AND the elder's "already handed the Chime over" gate. Live
    # player report 2026-08-02: player owned the AP Chime, learned Lufenian, and
    # the elder gave his post-gift line while the tracker showed "Lefein - Elder
    # 0/1" -- the old batch poll's `or won` gate swallowed the check on top of
    # the refusal, so the location was doubly unreachable.
    # Lefein FIELD_MAP_ID 0x50 + overworld doorstep (230,92) captured live
    # 2026-08-02. Radius prearm, NOT PREARM_GLOBAL: b7 must stay SET everywhere
    # else or a won Chime stops raising Mirage Tower / the Sky Castle -- and
    # Mirage Tower is entered from a far-away overworld tile, well outside this
    # radius, so its map-load read is unaffected.
    # hold_poss=True conservative (the elder's possession read is unverified;
    # costs the menu entry only while inside Lefein). consumed=None: the Chime
    # is never spent.
    ("Chime", 0x00000050, CHIME_EVENT_ADDR, CHIME_EVENT_MASK,
     12, CHIME_NPC_ORDINAL, True, (230, 92, 8), None),
    # Matoya / Jolt Tonic (2026-08-03): ELEVENTH instance and the LAST member of
    # the legacy Classic-7 poll -- the one this file has listed as "still latent"
    # since 2026-07-20. 0x1151D b0 is BOTH the Jolt Tonic function bit
    # (KEY_ITEM_FUNCTION_BITS[4], the Elf Prince's accept gate) AND Matoya's own
    # "already traded" gate. Live player report 2026-08-03: AP Jolt Tonic owned,
    # Crystal Eye in the bag, Matoya answered "You still here? I don't need you
    # anymore" with `Matoya's Cave - Matoya` unchecked -- refusal + the old
    # poll's `or won` gate swallowing the check, both halves dead exactly like
    # Astos and the Smith.
    # Matoya's Cave FIELD_MAP_ID 0x23 + overworld doorstep (161,110) captured
    # live 2026-08-03 (the tile where the id flipped). Radius prearm because her
    # dialog binds at MAP LOAD like every other NPC -- an in-map-only hold would
    # clear b0 a tick after she had already bound her already-traded line (the
    # Citadel/healer/Smith race). Radius, never global: b0 must stay SET
    # elsewhere or the Elf Prince stops accepting a won tonic.
    # hold_poss=True conservative (whether her event also reads tonic possession
    # is unverified; costs the menu entry only inside her cave).
    # consumed=None: her natural sink is the Crystal Eye trade, and the eye's
    # own row already documents why that bit cannot be used as a consumed
    # detector (it IS this bit).
    # (literal ordinal 11 = JOLT_TONIC_NPC_ORDINAL, defined below this table --
    # same reason the Astos/Elf Prince rows spell theirs out.)
    ("Jolt Tonic", 0x00000023, 0x08D1151D, 0x01,
     4, 11, True, (161, 110, 8), None),
    # Bottled Faerie / Onrac Caravan (2026-08-05): migrated out of the legacy
    # not-owned-gated batch because the Faerie GAINED a function bit this
    # session (KEY_ITEM_FUNCTION_BITS[15] = 0x11521 b2, see the note there), and
    # that bit is ALSO the Caravan's "already sold -> revert to the tonic shop"
    # state. Without a row, a won AP Faerie would revert the shop before the
    # player ever bought, killing "Onrac - Caravan"; and the old batch's
    # `or won` gate would have swallowed the check after a genuine purchase
    # (the Smith lesson, third time).
    # MAP TUPLE, not a single id: the Caravan is TWO nested maps -- the outer
    # camp 0x1F entered from overworld (18,44), and the tent interior 0x17
    # where the sale happens (user-described layout, ids captured live
    # 2026-08-05). Holding the gate in the tent alone would lose the
    # camp -> tent load race; holding both + the doorstep prearm makes every
    # layer load with the Faerie still on sale.
    # hold_poss=True conservative (whether the shop also reads possession is
    # unverified). consumed=None: the bottle is spent at the Gaia spring, but
    # its spring bit 0x1151F b1 sits inside the Oxyale function mask, so it is
    # unusable as a consumed detector -- see the KEY_ITEM_CONSUMED notes.
    # (literals: BOTTLE_* constants are defined below this table.)
    ("Bottled Faerie", (0x0000001F, 0x00000017), 0x08D11521, 0x04,
     15, 8, True, (18, 44, 8), None),
]

# Overworld value of FIELD_MAP_ID_SA (the FINE per-map id). Distinct from the
# COARSE LOADED_MAP_ID_SA, whose overworld value is 0. Live-captured 2026-07-16.
OVERWORLD_FIELD_MAP_ID = 0xFF

# Chaos defeated -> victory. Live before/after diff of killing Chaos (2026-07-07):
# the ONLY changed bit in the durable story/key-item block 0x11500-0x1153F was
# 0x11520 b6; verified to persist after the cutscene. Same event register as the
# key-item function bits above, so it survives save/load. Drives /goal auto-report.
CHAOS_DEFEATED_ADDR    = 0x08D11520
CHAOS_DEFEATED_MASK     = 0x40

BOTTLE_EVENT_ADDR      = 0x08D11521   # b2: Caravan sold the Bottled Faerie
BOTTLE_EVENT_MASK      = 0x04
BOTTLE_NPC_ORDINAL     = 8

# Oxyale fairy-release: native release sets both 0x1151F b1 AND b2. b1 is the
# stable LOCATION detector; b2 is what the Onrac sub MERMAID's presence gates on
# (grant must set b2 too -- see KEY_ITEM_FUNCTION_BITS[16], mask 0x0E, and the
# 2026-07-17 RE that finally caught this). Do NOT drop b2 from the grant mask.
OXYALE_EVENT_ADDR      = 0x08D1151F   # b1: fairy freed -> gave Oxyale (detector)
OXYALE_EVENT_MASK      = 0x02
OXYALE_NPC_ORDINAL     = 9

# --- Classic-7 Mystic-Key trade chain (2026-07-06) --------------------------------
# Crystal Eye (Astos) / Jolt Tonic (Matoya) / Mystic Key (Elf Prince) promoted from
# EVENT tokens to REAL AP pool items whose grantor NPC is a randomized location.
# UNLIKE the Earth Rod / Chime / etc. batch (detected on a separate obtained-EVENT bit
# 0x1151D/E/F), these three are detected on the granted item's OWN POSSESSION bit +
# a not-owned gate -- the SAME pattern as the Princess/Lute and Sage/Canoe. The native
# NPC sets the item's possession bit (0x1153B b5/b4/b3) exactly when it hands the item
# over, so that bit IS the location detector; no separate event-bit RE is needed. The
# possession bit/addr is derived at runtime via key_item_bit(id) in _npc_loop, so only
# the ordinal is stored here. POSSESSION-ONLY grant policy (deliberately ABSENT from
# KEY_ITEM_FUNCTION_BITS -- splits UNVERIFIED; a live strip/receive sweep resolves them).
# Same inherited WART (finding the AP item before the native NPC misses the check).
# Ordinals 10..12 (1=Princess .. 9=Oxyale; last used was 9). See wiring report.
CRYSTAL_EYE_NPC_ORDINAL = 10   # Astos (Western Keep); detect Crystal Eye poss 0x1153B b5
JOLT_TONIC_NPC_ORDINAL  = 11   # Matoya's Cave;         detect Jolt Tonic  poss 0x1153B b4
MYSTIC_KEY_NPC_ORDINAL  = 12   # Elf Prince (Elven); detect = shadow quest flag (below)

# --- Elf Prince shadow quest flag (2026-08-09, prince_gate_split v247) ------------
# apply_prince_gate_split repoints the Prince quest chain from story flag 9 to
# flag 69 = NPC_GATE_SPLIT_FLAG_BASE(64) + key id 5. Flags map linearly from
# 0x1151C (flag N -> byte 0x1151C + N//8, bit N%8; live-verified via pokeflag),
# so 69 -> 0x11524 b5. Ids 64..95 verified free on disc, in engine code and in
# this client (2026-08-09 audit); 49..63 skipped (client shadow space, e.g. the
# rune borrow's flag 55). The flag lives in the save block -> a completion
# survives save/reload, and the client re-asserts it from the server's checked
# list so the event can never re-run after a rollback.
ELF_PRINCE_QUEST_ADDR = 0x08D11524   # story flag 69
ELF_PRINCE_QUEST_MASK = 0x20

# Crescent Lake sage, same scheme (v255): shadow flag 64 + canoe key id 17 = 81
# -> 0x1151C + 81//8 = 0x11526, bit 81%8 = 1. Set by the one repointed
# `2d 04 12 00` at ISO 0x089AB14C (his handover cutscene, immediately before the
# canoe give). This REPLACES the dialog-latch talk detector entirely: that
# watched a pointer which is live only while his box is on screen, so a player
# who pressed through the box was never sampled and the check silently never
# sent (live 2026-08-10, the Blue Curtain report -- the third time this detector
# "died"). A story flag is durable, so no fast poll, no rising edge, no window
# scan and no DIALOG_STATE_ADDR are needed. See [[dialog-state-addr-moves-
# between-builds]].
SAGE_QUEST_ADDR = 0x08D11526         # story flag 81
SAGE_QUEST_MASK = 0x02

# --- Adamantite pickup + Dwarf Smith turn-in (2026-07-06) -------------------------
# Adamantite is an EVENT pickup (NOT a chest -- no PSP treasure-table entry), detected
# on the obtained-event bit 0x08D11520 b1 (0x02) which the game sets when the party
# grabs the ore. Now a randomized NPC-poll location; the Adamantite is a POOL item
# (key item id7). Detector-and-function bit are the SAME (0x11520 b1) -- see
# KEY_ITEM_FUNCTION_BITS[7]. Strip = possession (0x1153B b1 via key_item_bit) + b1.
ADAMANTITE_EVENT_ADDR  = 0x08D11520
ADAMANTITE_EVENT_MASK   = 0x02        # b1: Adamantite obtained (location detector)
ADAMANTITE_NPC_ORDINAL  = 13
# Dwarf Smith turn-in: hand the Smith the Adamantite -> he forges Excalibur. The
# turn-in sets durable event 0x08D11520 b3 (0x08) -- the detector for the Smith AP
# location. Excalibur is a WEAPON (cat 2, game id 39), added as a normal AP pool item;
# the native forge is stripped by removing the [2,39,qty] inventory record. Ordinal 14.
SMITH_EVENT_ADDR       = 0x08D11520
SMITH_EVENT_MASK        = 0x08        # b3: Smith forged Excalibur (location detector)
SMITH_NPC_ORDINAL       = 14
EXCALIBUR_WEAPON_ID     = 39          # cat 2 (weapon) game id -> WEAPONS[39] == "Excalibur"

# --- Bahamut = AP LOCATION ONLY, NO promotion (design decision, user 2026-07-08) --
# The player wants reaching Bahamut with the Rat's Tail to grant the location's AP
# item WITHOUT the party class change. The native turn-in IS the promotion, so we
# must NOT let Bahamut accept the tail: Rat's Tail is granted possession-only (its
# entry is REMOVED from KEY_ITEM_FUNCTION_BITS), so Bahamut refuses it (gives his
# "return with a token of your courage" dialog, promotes nobody) and the old
# class-change-done event 0x1151F b0 never sets. The location detector is therefore
# NOT that bit -- it is: standing in Bahamut's room (FIELD_MAP_ID == BAHAMUT_ROOM_ID)
# while owning the AP Rat's Tail (possession bit 0x1153A b3 via key_item_bit(13)).
# Logic (logic.py) still gates the location on AIRSHIP + RATS_TAIL, so it is only
# ever reachable with the tail in hand.
#   NB: LOADED_MAP_ID_SA (0x13118) is a COARSE bucket -- it reads 1 for EVERY dungeon
#   (Bahamut's room AND Chaos Shrine both == 1), so it CANNOT identify Bahamut. The
#   fine-grained per-map id is FIELD_MAP_ID_SA (0x13108, u32): live-verified distinct
#   (Bahamut=0x1D, Chaos Shrine=0x4E) via re_only/dump_mapstate.py, 2026-07-08.
FIELD_MAP_ID_SA         = 0x08D13108  # u32 fine per-map id (unlike coarse 0x13118)
# Shop-building interior fine map ids: town shops share THREE generic interior
# maps (full 7-town / 17-shop tour live-captured 2026-08-05):
#   0x19 -- most shops (all of Crescent Lake/Melmond/Onrac, Gaia items,
#           Elfheim/Pravoka weapon+item, Cornelia items)
#   0x1a -- Elfheim/Pravoka armor, Cornelia weapon+armor
#   0x21 -- Gaia weapon+armor
# (Streets for reference: Cornelia 0x31, Pravoka 0x37, Elfheim 0x41, Melmond
# 0x30, Crescent 0x43, Onrac 0x42, Gaia 0x4b.) Gates the shop name/desc bank
# authoring window: every shop UI and the inventory menu render from the SAME
# resident bank copy (the shop list snapshots it at dialog open), so AP names
# may exist only while the player stands inside a shop building.
SHOP_INTERIOR_FIELD_MAP_IDS = frozenset({0x19, 0x1a, 0x21})
# Town STREET fine map ids -> store-city index (0 Cornelia .. 6 Gaia). The
# street ids are unique per town (the interiors above are generic and shared),
# so crossing one is the "entered town X" edge. Captured in the 2026-08-05
# 17-shop tour (the "streets for reference" list above) and re-confirmed live
# 2026-08-16 (v2 peek: Gaia 75, Melmond 48, Pravoka 55, Onrac 66, Cornelia 49).
# Drives the shared-placeholder (v2) per-town shop identity authoring.
TOWN_STREET_MAP_IDS = {
    0x31: 0,   # Cornelia
    0x37: 1,   # Pravoka
    0x41: 2,   # Elfheim
    0x30: 3,   # Melmond
    0x43: 4,   # Crescent Lake
    0x42: 5,   # Onrac
    0x4b: 6,   # Gaia
}
# Live shop-UI struct fields, save-block relative (sa()). Set at shop UI init
# (the Buy/Sell/Exit prompt -- BEFORE the Buy list opens and snapshots prices)
# and STALE afterwards: they hold the last shop visited, so treat as an edge,
# never a level test. Located 2026-08-16 by value-scan intersection
# (re_only/v2_walkthrough.py --scan); the BUYB cave reads the same pair at
# s0+0x7064/+0x7068, so s0 = save+0xE140 (NOT the save base -- the first guess
# sa(0x08D18164) reads a constant 2 in every shop).
SHOP_STORE_ID_SA = 0x08D262A4     # u32 shop-def index (== rando._DEF_IDX values)
SHOP_STORE_TYPE_SA = 0x08D262A8   # u32 1=weapon 2=armor 3=item
BAHAMUT_ROOM_ID         = 0x1D        # FIELD_MAP_ID value in Bahamut's room
BAHAMUT_NPC_ORDINAL     = 15
RATS_TAIL_KEY_ID        = 13          # KEY_ITEMS id -> possession bit via key_item_bit()
# FIELD_MAP_ID for Crescent Lake (the sages' circle). Live-captured 0x43 on
# 2026-07-15 (re_only/state_probe.py, delta +0x6000, standing at the sages).
# Used as the map-presence proxy for the Sage-check item-order fix (the sage
# sets NO durable flag beyond canoe possession/function -- RE-confirmed 2026-
# 07-15, event 0x1b1 has op37 grant + op30 but no setStoryFlag -- so once the
# Canoe AP item is owned, possession is indistinguishable from our own grant
# and the not-owned detector can never fire; standing in Crescent Lake is the
# only save-visible signal that the player has REACHED the sage). See memory
# ship-bikke-flag / event-system-static-blob.
CRESCENT_LAKE_MAP_ID    = 0x43        # FIELD_MAP_ID in the Crescent Lake sages' map
# The giver sage's tile in map 0x43, captured live 2026-08-10 standing beside
# him: (x, y, radius). Radius 5 = the user-specified 10x10 box. The Sage
# location check sends when the party stands inside it (proximity detection,
# the Bahamut pattern -- see the ApClient sage stage for why his handover
# could not be used).
SAGE_TILE = (47, 22, 5)
# Pravoka town (Bikke / the pirates). Same role for the Bikke detectors: his
# "defeated" (id63) and "ship available" (id5) flags are SAVE-CARRIED, so seeing
# them set proves nothing about THIS session -- a save slot from another session
# carries them too. The battle can only happen on this map, so presence here is
# the corroborating signal (see the _npc_loop Bikke block, fixed 2026-07-31).
PRAVOKA_MAP_ID          = 0x37        # matches iso_patcher._BSS_PRAVOKA_ID

# Field-dialog state struct (STATIC BSS, no relocation; RE'd live 2026-07-24).
# The game PRE-SELECTS every NPC's dialog at MAP LOAD (that's when the event
# blob's story-flag checks run -- talking fires ZERO interpreter ops, which is
# why every talk-stream patch attempt failed); talking just renders the cached
# line and LATCHES it here: +0x0 bank base, +0x4 bank base (dup), +0x8 pointer
# to the shown entry's text, +0xC entry INDEX in the map's .MSG TEXT bank.
# The latch survives box close (cleared only by the next dialog), so a 2s poll
# cannot miss it. In Crescent Lake (MAP08.MSG) the GIVER sage's two boxes are
# entries 0x17 ("Four hundred years ago it was wind...") + 0x18 ("The four
# forces...") and NO other NPC in town uses either (live-swept 2026-07-24:
# other sages/townsfolk showed 5,6,8,0xa,0xc,0xe,0x10,0x12,0x14) -- so
# (map==0x43, entry in {0x17,0x18}) == "player talked to the giver sage".
# "STATIC BSS" is static only for a GIVEN EBOOT: the bake's code caves grew
# between 2026-07-25 and 2026-08-01 and the struct moved +0x3000
# (0x08C56610 -> 0x08C59610, re-located live via scratchpad dlg_scan.py:
# quad {bank, bank==bank, heap text ptr, small entry idx}, only candidate
# whose entry flipped to 0x17 with the sage box open; latch verified to
# survive box close). RE-SCAN AFTER ANY PATCHER CHANGE THAT GROWS THE EBOOT.
DIALOG_STATE_ADDR     = 0x08C59610
SAGE_DIALOG_ENTRIES   = (0x17, 0x18)
# Vanilla first-box text (lowercased, punctuation-stripped) -- the _mapmsg
# authoring hook keys on this to find/rewrite the sage's box in MAP08's bank.
SAGE_BOX_VANILLA      = "four hundred years ago it was wind"
SAGE_BOX2_VANILLA     = "the four forces that make up the world"

# Key-item FUNCTION bits. For progression key items the possession bitfield
# (0x11537-0x1153B) is only the inventory DISPLAY layer; the real functional gate
# that overworld movement / turn-in NPCs actually check is a separate EVENT-REGISTER
# bit (0x1151D/E/F). So delivering one as an AP item must set BOTH (possession +
# function) and native-strip must clear both -- otherwise the menu shows an item the
# player cannot use, or a stripped item keeps working. Map: KEY_ITEMS id -> (addr,mask).
# ONLY items whose split is CONFIRMED LIVE are listed (audit + POC 2026-07-05; see
# ap-keyitem-grant-infra / flag-collection-progress memories). Possession-only items
# are deliberately ABSENT -- do NOT add an item's acquisition-event bit here until the
# clear-possession-only POC proves the split, because prematurely setting an event bit
# (e.g. robot-gave-cube 0x11520 b0) can suppress the NPC that is meant to be a location.
KEY_ITEM_FUNCTION_BITS = {
    # Classic-7 Mystic-Key trade chain + Sarda/Lefein/robot batch: the live split
    # sweep (2026-07-06) confirmed each of these needs a FUNCTION event-register bit
    # in addition to the display-only possession bit (grantor NPCs / doors / warps /
    # movement gate on the event bit, not the possession bitfield). grant_key_item
    # sets BOTH (possession + this bit); every native-strip in _npc_loop clears BOTH.
    2:  (0x08D1151C, 0x40),                          # Crown       -> Astos trade gate
    3:  (0x08D1151C, 0x80),                          # Crystal Eye -> Matoya trade gate
    4:  (0x08D1151D, 0x01),                          # Jolt Tonic  -> Elf-Prince trade gate
    5:  (0x08D1151D, 0x02),                          # Mystic Key  -> locked doors
    6:  (0x08D1151D, 0x04),                          # Nitro Powder-> Nerrick canal turn-in
    7:  (0x08D11520, 0x02),                          # Adamantite  -> Smith turn-in
    # Star Ruby (9): RE-FIXED LIVE 2026-07-19. 0x1151D b5 (0x20) alone was WRONG
    # and was a live PROGRESSION BREAK: an AP Star Ruby never opened Titan's
    # Tunnel. Poll-diff of the durable block while handing the Ruby to the Giant
    # in Giant's Cave captured 0x1151D 0xa4 -> 0xe4 (b6 RISES) and possession
    # 0x1153A 0xe1 -> 0x61 (b7 consumed). So:
    #   b6 (0x40) = FED -- the Giant is removed / the tunnel is open. THIS is the
    #               function bit; setting it is what actually opens the way.
    #   b5 (0x20) = the Giant's ACCEPT precondition. Live: possession + b5 clear
    #               -> he refuses ("You shall not pass!"); possession + b5 set ->
    #               he accepts, consumes the Ruby and sets b6. It is NOT consumed
    #               by the turn-in (set before AND after in the capture).
    # Grant mask = 0x60 (both), for two reasons. (1) b5 alone is provably not
    # enough -- setting only b5 and fully reloading the map leaves the Giant
    # standing (verified twice live), so b6 is mandatory. (2) b5 is kept so the
    # granted state is the FULL native "ruby recognized" state: if the player is
    # standing in the cave when the item lands, the Giant's own accept path stays
    # coherent rather than half-armed. Symmetric strip is safe here even though
    # b5 may be set by an earlier native step: every strip path clears the same
    # mask, and the next legitimate AP grant re-sets b6, which opens the tunnel
    # regardless of b5 -- so a cleared b5 can never strand the player.
    # NB: unlike the other trade-chain turn-ins, this bit carries NO location
    # semantics for us: "Giant's Cave - Titan" is a logic-only EVENT token
    # (logic.EVENTS), not a client-detected AP location, and the Star Ruby's own
    # AP location is its Earth Cave CHEST (EVENT_KEY_CHESTS idx 41, detected on
    # the treasure bitfield). So nothing can be suppressed by setting b6.
    9:  (0x08D1151D, 0x60),                          # Star Ruby -> Titan accept b5 + FED b6
    10: (0x08D1151D, 0x80),                          # Earth Rod   -> Lich / Earth Crystal
    12: (0x08D1151F, 0x80),                          # Chime       -> Sky Castle ascend
    14: (0x08D11520, 0x01),                          # Warp Cube   -> deep Sky Castle warp
    17: (CANOE_FUNCTION_ADDR, CANOE_FUNCTION_MASK),  # Canoe -> sail rivers/shallows
    # Levistone delivery AUTO-RAISES the airship (design choice 2026-07-05): receiving
    # the AP item sets obtained b4 + airship-raised b5 (mask 0x30) -- the EXACT state
    # the live POC validated (possession + 0x1151E b4 + b5 -> airship rose on reload,
    # boarded + flew). The airship is then up at its desert spot (206,66) and the player
    # travels there to board it. No desert visit required to raise; overworld reload
    # needed to draw. b4 is also the Ice Cave location detector bit, but that poll is
    # gated on Levistone-not-owned, so our own delivery never false-fires it. See
    # ap-keyitem-grant-infra / flag-collection-progress.
    11: (0x08D1151E, 0x30),                          # Levistone -> obtained b4 + airship b5
    # Rosetta: TWO bits, not one (live-RE'd 2026-07-30 after a player report that the
    # Lufenians stayed unintelligible and the elder gave no Chime even though Dr Unne's
    # translate cutscene HAD played and said "learned Lufenian"). b4 = story flag 28 =
    # "Unne translated" (the state his event branches on); b6 = story flag 30 = "party
    # understands Lufenian" -- what the Lefein map's dialog selection actually reads
    # (getStoryFlag(30) at the EVM check handler ra=0x08843ecc, captured on Lefein map
    # load; poking b6 live flipped the town to English + elder handed the Chime).
    # Holding ONLY b4 was actively harmful: the reassert loop pins it set from the
    # moment the AP Rosetta is owned, so Unne's event always takes its already-
    # translated branch and the leg that sets b6 NEVER runs -- while his cutscene
    # still plays normally (NPC dialog is pre-selected at map load, independent of
    # the flag-set leg), which made the visit look successful. Net: language
    # unlearnable in-seed = HARD softlock on Chime -> Sky Castle -> Tiamat.
    # Both bits are in the mask now, and id 8 is in OWNED_FUNCTION_REASSERT, so a
    # save that already owns the Rosetta self-heals on the next tick (the
    # grant-counter anti-dup never re-runs the grant itself).
    # NARROWED BACK TO b4 2026-08-02 (player report: owning the AP Rosetta made the
    # Lufenians intelligible IMMEDIATELY -- Dr Unne became cosmetic and the logic edge
    # ROSETTA -> "Melmond - Dr Unne" -> ROSETTA_TRANSLATED a no-op).
    # The 2026-07-30 theory that added b6 here ("with b4 pinned, Unne's event always
    # takes the already-translated branch, so the leg that sets b6 never runs") is
    # REFUTED LIVE 2026-08-02: on a save that owned the AP Rosetta (b4 pinned by the
    # reassert loop since delivery) and had NEVER visited Unne, clearing b6 and then
    # talking to him for the first time flipped 0x1151F 0x90 -> 0xd0 -- his native
    # event sets b6 fine with b4 already up. The real 07-30 failure was almost
    # certainly an Unne visit made BEFORE the AP Rosetta arrived (no Rosetta = no
    # translate leg at all); the cure there is to revisit him, not to pre-grant b6.
    # So b6 (flag 30, "party understands Lufenian" -- what the Lefein map's dialog
    # selection reads at map load) is EARNED at Unne and must never be granted or
    # reasserted: doing so unlocks Lefein/Chime the moment the stone is owned.
    8:  (0x08D1151F, 0x10),                          # Rosetta -> Unne translate gate b4 (b6 = earned at Unne)
    # Rat's Tail (13): function bit 0x1151E b7 lets Bahamut ACCEPT the tail and run
    # the game's OWN promotion event (builds the party-lineup scene + promotes all 4
    # correctly -- live-verified clean 2026-07-13). This is the ONLY promotion path:
    # the client-side scroll/mailbox promotion was abandoned (the routine can't be
    # called outside its native event's lineup-scene context -- see
    # job-advancement-items memory). The Bahamut AP-location detector reads
    # room 0x1D + tail POSSESSION (not the class-change-done flag), so the native
    # promotion setting 0x1151F b0 does not false-fire it. See BAHAMUT_* above.
    # Oxyale (16): RE-FIXED LIVE 2026-07-17 after RECURRING failure. The Onrac
    # barrel-sub MERMAID is a map-load-conditional NPC: she is DRAWN unless the
    # fairy-freed state is set, and she blocks the sub while present. The native
    # fairy-release sets 0x1151F b1 AND b2; the mermaid's disappearance gates on
    # **b2 (0x04)**. Every prior session set only b1(+b3) = mask 0x0A and trusted
    # a 2026-07-09 note that said "she checks b1" and never tried b2 -- so the
    # bug came back each time. Live-proven this session: with b1+b3 set the
    # mermaid stayed and the sub refused; setting b2 (mask -> 0x0E = b1+b2+b3) and
    # reloading the room removed her and opened the sub. b1 = the Fairy-location
    # detector (safe: its poll skips when AP Oxyale is owned, `... or won:
    # continue`); b3 = "sub-ready" result the mermaid normally sets herself
    # (harmless to pre-set). So set all three = the full native fairy-freed state.
    # Presence is decided at MAP LOAD, so an overworld reload of her room is
    # needed to redraw her out (client-delivered grant + next room entry suffices).
    16: (0x08D1151F, 0x0E),                          # Oxyale -> fairy freed (b1+b2) + sub-ready (b3)
    13: (0x08D1151E, 0x80),                          # Rat's Tail -> Bahamut promotion turn-in
    # Bottled Faerie (2026-08-05): the Faerie had NO function bit, which made
    # the AP item INERT and broke the logic edge `Gaia - Fairy` requires
    # BOTTLED_FAERIE. Live-proven the same day: the Gaia spring releases the
    # fairy on **0x11521 b2** -- the Caravan's "bottle sold" state -- NOT on
    # possession. Player bought the Caravan's AP slot, and the spring fired at
    # Gaia with the possession bit correctly stripped and the real AP Faerie
    # still sitting unfound in a Sunken Shrine chest: the whole Fairy -> Oxyale
    # -> Sea Shrine chain opened on the purchase alone.
    # So b2 IS the "party carries a bottle" state the spring reads, and a won
    # AP Faerie must set it -- otherwise a player who receives the item but
    # never buys at the Caravan could never free the fairy (logic requires only
    # the item, so that would be a softlock).
    # The mirror trap this creates is handled, do NOT remove either half:
    #   * an AP Faerie owned before visiting the Caravan would revert the shop
    #     -> the Bottled Faerie NPC_MAP_RESET row (maps 0x1F+0x17, doorstep
    #     (18,44)) holds b2 clear across the whole Caravan until its check;
    #   * b2 left set from a mere purchase would still free the fairy early
    #     -> the MAP_SCOPED_FUNCTION_HOLD 'unwon' row holds it clear in Gaia
    #     (+ doorstep (214,22)) until the AP Faerie is actually won.
    15: (0x08D11521, 0x04),                          # Bottled Faerie -> "carrying a bottle" (Gaia spring)
}

# Function bits are applied by grant_key_item ONLY at delivery. The grant-counter
# anti-dup means an ALREADY-delivered item is never re-granted, so a function bit
# that (a) was delivered under an OLD/narrower mask, or (b) got dropped by a save
# reload, is NEVER re-asserted -> the item silently stops working on relaunch.
# This bit the Oxyale mermaid live 2026-07-17: the b2 mask fix above helps only
# NEW grants; an Oxyale delivered under the old 0x0A mask stayed broken across a
# client restart because grant never re-ran. FIX: _npc_loop reconciles the below
# ids every tick -- if the item is WON (legitimately owned as an AP item), force
# its full function mask SET (idempotent, writes only on change).
#
# ONLY "usage/movement enable" items belong here -- ones where owning the AP item
# should ALWAYS imply the enable bit. DELIBERATELY EXCLUDED:
#   * NPC_MAP_RESET items (7 Adamantite, 10 Earth Rod, 14 Warp Cube): gate HELD
#     CLEAR by the map-reset prearm/hold logic; force-setting it fights that.
#   * trade-chain turn-in items (Crown/Crystal Eye/Jolt/Mystic/Nitro/Chime/
#     Adamantite/Rat's Tail): their event bit carries "NPC accepted/done"
#     LOCATION semantics -- force-setting could suppress a location or a chain step.
# The Star Ruby (9) is the ONE turn-in that IS listed: its bits gate nothing we
# detect (see KEY_ITEM_FUNCTION_BITS[9] -- Titan is a logic-only EVENT and the
# Ruby's AP location is a chest), and without the reassert every Star Ruby
# delivered under the old broken 0x20 mask stays broken forever (grant-counter
# anti-dup never re-runs the grant) -- exactly the Oxyale failure mode, and now a
# HARD softlock because the Giant physically blocks the cave (giant_cave_gate).
# Each listed item's function bit is a pure gameplay gate whose location detector
# (if any) is gated on NOT-owned, so force-setting it for an owned item is safe.
OWNED_FUNCTION_REASSERT = (
    # 5 Mystic Key ADDED 2026-08-09 (prince_gate_split): flag 9 is now the
    # doors' bit alone -- nothing native ever sets it (the quest chain moved to
    # flag 69), so a won key's door access must survive save reloads via this
    # reassert. Safe from the old fight-the-hold problem: the Mystic Key row
    # left NPC_MAP_RESET entirely, so nothing holds b1 clear anywhere, ever.
    5,    # Mystic Key -> locked doors 0x1151D b1
    9,    # Star Ruby-> Titan accept 0x1151D b5 + FED b6 (opens Titan's Tunnel)
    # 16 Oxyale NARROWED 2026-07-21 (see OWNED_FUNCTION_REASSERT_MASK): b2/b3
    # migrated to the NPC_MAP_RESET row (Gaia fairy already-gave gate; full
    # reassert fought the hold); b1 (fairy freed = her SPAWN bit) stays
    # reasserted here or a save reload could despawn her forever (the grant
    # never re-runs).
    16,   # Oxyale   -> fairy-freed b1 ONLY (masked below; b2/b3 = map-reset row)
    # 17 Canoe RESTORED 2026-08-10 (v258): briefly removed for the v256/v257
    # sage holds, which held both canoe bits clear in Crescent Lake so his
    # handover could fire. The sage location is PROXIMITY-detected now (his
    # give latches Lich + sailing + possession at map load -- all three proven
    # blocking live -- so the handover path was abandoned); nothing holds any
    # canoe bit anywhere, and the reassert is safe and wanted again.
    17,   # Canoe    -> sail 0x1151E b2
    # 11 Levistone REMOVED 2026-07-20: migrated to NPC_MAP_RESET (its obtained
    # bit b4 doubles as the Ice Cavern pickup's gate; the reassert fought the
    # hold). The map-reset won-key restore now asserts the full 0x30 mask.
    8,    # Rosetta  -> Unne translate GATE b4 only (never b6 -- b6 = the Lufenian
          #             language, earned by actually talking to Unne; see the
          #             KEY_ITEM_FUNCTION_BITS[8] note)
)

# Per-key override of the reassert mask (default = the full
# KEY_ITEM_FUNCTION_BITS mask). Lets a key stay on the reassert list for the
# bits a NPC_MAP_RESET row does NOT own. Oxyale: the map-reset row holds/
# restores b2+b3 (the fairy's already-gave gate + mermaid/sub function);
# reasserting those here would fight the in-Gaia hold, but b1 (fairy freed =
# her spawn bit) belongs to no row and must survive save reloads.
OWNED_FUNCTION_REASSERT_MASK = {
    16: 0x02,   # Oxyale -> fairy-freed b1 only
}

# --- On-disc gate splits: shadow story flag owned by the AP grant (v260) -----
# For a key whose vanilla function bit is SHARED with an event that other
# content also fires (a Soul-of-Chaos floor re-running a vanilla script), the
# bake repoints the REAL gate's READ to a private shadow story flag (see
# iso_patcher NPC_GATE_SPLIT_FLAG_BASE / titan_gate_split). That flag is set ONLY
# by us: grant_key_item sets/clears it beside the function bit, and the func-
# reassert loop pins it while the key is owned (a save reload can drop it exactly
# like the Oxyale mask bug). Kept SEPARATE from KEY_ITEM_FUNCTION_BITS because
# the vanilla function bit MUST still be maintained too -- an old ISO without the
# split cave reads the vanilla bit, so both are written unconditionally; on a
# baked ISO the shadow flag drives the real gate and the vanilla bit is a
# harmless redundant write (nothing on the real map reads it any more).
#   item_id -> (canonical addr, mask)
GATE_SPLIT_SHADOW_BITS = {
    # Star Ruby (9) -> Titan's Tunnel. Shadow flag 73 = NPC_GATE_SPLIT_FLAG_BASE
    # (64) + key id 9 -> byte 0x11525 b1. Live-verified free of disc refs and
    # RAM-rehearsed end to end 2026-08-11 (see titan-gate-split-plan memory).
    9: (0x08D11525, 0x02),
}

# --- SPENT KEY ITEMS (2026-07-21) ------------------------------------------
# Vanilla CONSUMES several key items at their turn-in (the Nitro Powder goes to
# Nerrick, the Rat's Tail to Bahamut, ...) -- the item leaves the Key Items menu
# for good. Our AP delivery sets the possession bit once and nothing ever clears
# it, so every spent key lingers in the menu forever (live player report
# 2026-07-21: canal built + party promoted, powder and tail still listed).
#
# This table generalizes the `consumed` field of NPC_MAP_RESET (see that table's
# 9th-field note) to key items that have NO map-reset row: KEY_ITEMS id ->
# (addr, mask) of a DURABLE "downstream turn-in done" bit. Once it is up,
# _npc_loop holds the POSSESSION bit clear every tick. The FUNCTION bit is left
# alone (native-consistent, and it usually IS the turn-in record).
#
# HARD RULE for adding a row: the detector bit must NOT be the item's own
# KEY_ITEM_FUNCTION_BITS bit, nor any bit an AP grant sets -- grant_key_item
# sets function bits at delivery, so such a detector would strip the item the
# instant it arrives. That rules out (do NOT add, each checked 2026-07-21):
#   * Crystal Eye (3)  -- Matoya-done IS the Jolt Tonic function bit 0x1151D b0,
#                         which an unrelated AP Jolt Tonic grant also sets.
#                         Already documented on its NPC_MAP_RESET row.
#   * Bottled Faerie(15)-- freed-at-spring is 0x1151F b1, part of the Oxyale
#                         function mask 0x0E -> an AP Oxyale would strip it.
#                         Needs a live-captured distinct bit.
#   * Rosetta Stone (8) -- Unne-translated is its own function bit 0x1151F b4.
#   * Star Ruby (9)     -- FED bit 0x1151D b6 is inside its own function mask
#                         0x60, and see the star-ruby-titan-bit open bug.
#   * Crown (2)         -- Astos-dealt-with has no bit distinct from the Crown's
#                         own trade gate 0x1151C b6.
# Never-spent items (Lute, Mystic Key, Earth Rod, Levistone, Chime, Warp Cube,
# Oxyale, Canoe) belong here under NO circumstances -- they stay in the menu in
# vanilla too, and several are permanent movement/gate keys.
KEY_ITEM_CONSUMED = {
    # Nitro Powder -> Nerrick blows the canal. Detector = the canal-open
    # tilemap flag 0x1151D b3 (live-verified by 9-round binary search
    # 2026-07-01, see canal-flag-bit memory; twin of the bridge bit 0x1151C
    # b3). DISTINCT from the powder's own function bit 0x1151D b2 (the Nerrick
    # turn-in gate an AP grant sets), so the arrival-strip trap is avoided.
    6: (0x08D1151D, 0x08),
}

# Rat's Tail (13) is spent by Bahamut's promotion, but that turn-in sets NO
# durable bit we can safely read: its recorded "accepted" bit IS the tail's own
# function bit 0x1151E b7 (KEY_ITEM_FUNCTION_BITS[13]), which grant_key_item
# sets on delivery. The promotion's real, unambiguous fingerprint is the PARTY:
# promoted job ids are 6..11 (PROMOTE / PROMOTED_JOB_NAMES above) and nothing
# else in the client ever writes a promoted id (_party_loop's starting-job
# write is base-only -- JOB_L1_BLOCK covers 0..5). So: any party row reading
# >= PROMOTED_JOB_MIN means Bahamut fired -> the tail is spent.
KEY_ITEM_CONSUMED_ON_PROMOTION = 13
PROMOTED_JOB_MIN = KNIGHT          # 6; ids 6..11 are the promoted classes

# --- MAP-SCOPED FUNCTION HOLD (2026-08-03) ---------------------------------
# Tenth instance of the gate-bit collision class ([[npc-map-reset]]), but with
# NO AP location to hang an NPC_MAP_RESET row on -- so it gets its own, simpler
# mechanism: while the party is inside one of `maps`, hold a won key item's
# function bit CLEAR; anywhere else, keep it SET.
#
# Rat's Tail (13): 0x1151E b7 is the tail's function bit (Bahamut's accept gate,
# KEY_ITEM_FUNCTION_BITS[13]) AND story flag 23, the Citadel of Trials'
# "trial done" state -- the sibling flag noted in the citadel-crown-gate RE.
# Receiving the AP Rat's Tail therefore marks the trial complete before the
# player has set foot in the Citadel: the admitting elder DESPAWNS and all 10
# Citadel chests are locked out of the seed (live player report 2026-08-03:
# empty pillar hall, no elder, `Citadel of Trials Chests 0/10`; clearing b7 and
# re-entering the room brought him straight back).
# Citadel FIELD_MAP_ID 0x2F covers EVERY floor (one id for the whole dungeon,
# captured live 2026-08-03 walking entrance -> top -> out). Scoped to that map,
# never global: outside it the bit must stay SET or Bahamut refuses the tail
# (his room is a different map, so the restore has already run by the time the
# Dragon Caves load -- the map-load-binding rule from the Smith bug).
# The Citadel's own Rat's Tail is treasure idx 28, an ORDINARY chest handled by
# the chest poll, so holding flag 23 clear inside costs the dungeon nothing.
#
# DOORSTEP PREARM (added the same session, after the in-map hold alone FAILED
# live): the elder's despawn is decided at MAP LOAD, so clearing flag 23 on the
# first in-map tick is one load too late -- the watcher caught the Citadel
# loading at 0x1151E=0xfc and the hold dropping it to 0x7c a tick later, with
# the elder already gone. Hold it clear while the party stands on the Citadel's
# overworld doorstep (122,39) so the dungeon LOADS with the trial not-done.
# Radius-scoped like Astos/the Elf Prince: the bit must stay SET everywhere
# else for Bahamut.
#
# NOTE — 0x2F IS NOT UNIQUE: Sarda's Cave reports the same FIELD_MAP_ID (live
# 2026-08-03, entered from its own doorstep (23,184)). The fine map id is per
# AREA TYPE here, not per dungeon. Harmless for this row (Sarda reads the Earth
# Rod bit 0x1151D b7, a different byte), and harmless for the Earth Rod
# NPC_MAP_RESET row whose prearm is scoped to Sarda's own doorstep -- but any
# future row keying on 0x2F must not assume it means one specific dungeon.
#
# `mode` picks WHICH WAY the hold runs:
#   'won'   -- hold while the key item IS won (Rat's Tail: owning the tail must
#              not mark the Citadel's trial done). Out of scope, re-SET the bit.
#   'unwon' -- hold while the key item is NOT won (Bottled Faerie: the Caravan
#              purchase must not free the Gaia fairy before the real AP Faerie
#              is found). Out of scope the bit is re-SET only if `restore_ord`'s
#              location is already checked, i.e. the native sale really happened
#              -- force-setting it unconditionally would revert the Caravan shop
#              before the player ever bought, killing that location. Once the
#              item IS won the row goes inert and the normal function-bit path
#              owns the bit.
# SPENT = HOLD FOREVER (2026-08-15, player report: Crown in hand, story flag 22
# clear, Citadel entrance floor EMPTY -- no admitting elder, throne still saying
# "Only they who have been granted permission may undergo the trials"). The
# out-of-scope RESTORE is what re-arms the grenade: every time the party leaves
# the Citadel (or a tick lands in the map-transition window, where the coords are
# already the interior's but FIELD_MAP_ID has not flipped to 0x2F yet) the row
# re-SETs 0x1151E b7, and the very next Citadel load binds its NPC roster with
# flag 23 = "trial done" -> elder gone again. The bit's ONLY consumer is
# Bahamut's accept gate, so once the tail has been SPENT (party promoted -- same
# fingerprint KEY_ITEM_CONSUMED_ON_PROMOTION uses, and nothing else in the client
# ever writes a promoted job id) there is nobody left to restore it FOR, and the
# hold becomes permanent. `spent` names that condition:
#   None         -- no spend state; restore as before.
#   'promotion'  -- once any party row reads a promoted job id, never restore.
# Rows: (name, maps, key_id, addr, mask, prearm(ow_x, ow_y, radius) or None,
#        mode, restore_ord or None, spent or None).
MAP_SCOPED_FUNCTION_HOLD = [
    ("Rat's Tail", (0x0000002F,), 13, 0x08D1151E, 0x80, (122, 39, 6),
     "won", None, "promotion"),
    # Bottled Faerie / Gaia spring (2026-08-05). Gaia FIELD_MAP_ID 0x4B (same id
    # the Oxyale NPC_MAP_RESET row uses) + overworld doorstep (214,22) captured
    # live. The spring's release reads 0x11521 b2, so while the AP Faerie is
    # unwon that bit must be clear in Gaia -- with the doorstep prearm because
    # every other NPC/event in this game binds at MAP LOAD and there is no
    # reason to believe the spring differs (cheaper to prearm than to re-learn
    # the lesson a sixth time). restore_ord = 8 = "Onrac - Caravan": once that
    # check has fired the sale genuinely happened, so the bit goes back on
    # outside Gaia and the Caravan stays reverted to its tonic shop (the
    # 2026-07-16 fix's whole point -- do not regress it).
    ("Bottled Faerie", (0x0000004B,), 15, 0x08D11521, 0x04, (214, 22, 8),
     "unwon", 8, None),
]

# Consecutive out-of-scope ticks a MAP_SCOPED_FUNCTION_HOLD row must see before
# it restores the bit. The hold's scope test can read FALSE for one tick during a
# map transition (the party coords are already the destination's while
# FIELD_MAP_ID still reads the old map), and a single such tick was enough to
# re-SET flag 23 immediately before the Citadel bound its NPC roster. The bit is
# only ever needed at Bahamut, so paying ~2 extra ticks before restoring costs
# nothing and closes the window.
MAP_SCOPED_HOLD_RESTORE_TICKS = 3

# TRAP, do not resurrect: 0x08D13121 (once misnamed CURRENT_MAP_ID; backup
# copy 0x08BB1719) is NOT a map identity -- it is a scroll/REGION coordinate:
# it reads 6..14 as you walk the single overworld and small values in field
# maps (probe 2026-07-03). Do NOT gate anything on it.

# TRUE runtime map identity: constant across ALL positions within a map, distinct
# per map (snapshot-diff 2026-07-03, 4 far-apart overworld dumps + 2 per dungeon/
# town): overworld=0, Chaos Shrine=1, Cornelia Town=2 (Cornelia CASTLE, which has
# chests, is a different un-captured id). In the map-state struct, 9 bytes before
# the scroll coord. Read via save_delta (sa()). Used to disarm chest exec
# breakpoints on the OVERWORLD (map 0, the only chest-free map we gate on) without
# the JIT block-linking lag -- collision-free because every chest-bearing map has a
# nonzero id. See [[chest-bp-jit-slowdown]].
LOADED_MAP_ID_SA = 0x08D13118
OVERWORLD_LOADED_MAP_ID = 0

# --- Overworld party position + forest terrain (Dangerous Forests feature) --------
# LIVE party tile coords (RE'd 2026-07-06 via re_only/find_party_xy.py differential
# scan + encounter fn 0x8841e1c disasm: lh $x,0x68f0($field); zone=((x+7)>>5)+
# 8*((y+7)>>5)). The field/scene struct is a heap object reached through a FIXED
# code-segment pointer: *(FIELD_STRUCT_PTR) = field_base; party X = u16 at
# field_base+0x68f0, Y at +0x68f2 (plain tile coords 0..254). Because it derefs a
# live pointer it auto-follows save-block relocation -- NO sa() on the deref.
# (NB: the vehicle rec0 @0x08D11400 is a STALE save-copy, frozen mid-session; do
# NOT use it for the live walking position -- that was the dangerous-forests bug.)
FIELD_STRUCT_PTR = 0x089D7ADC      # fixed global -> current field/scene struct base
FIELD_PARTY_X_OFF = 0x68F0         # u16 party tile X within the field struct
FIELD_PARTY_Y_OFF = 0x68F2         # u16 party tile Y
# On-field flag inside the same struct: 1 while walking the field, 0 during a battle
# (RE'd 2026-07-06 from thief-steal debugging: live 0x08D1CDB4 = field_base+0x68B4,
# the exact complement of the in-battle flag 0x08D23200). The party X/Y at +0x68F0
# are only VALID/current when this is 1 -- at battle entry/exit and map transitions
# they read stale, which made Dangerous Forests mis-gauge forest state. Gate on it.
FIELD_ONFIELD_OFF = 0x68B4         # u8: 1 = on field, 0 = in battle
# CAVEAT (measured 2026-07-16, 150 samples / 15 s while WALKING the overworld):
# this reads 0 in ~2/3 of samples even though the party X/Y above track movement
# correctly the whole time (66 distinct tiles seen). So "1 = coords valid" holds
# for the battle case it was RE'd from, but it is NOT a coords-valid signal on
# the OVERWORLD -- do not gate overworld position checks on it (that made the
# Sarda prearm fire on only ~1/3 of ticks). Gate on the FINE map id instead.
# Forest-tile detection: the SHIPPED Dangerous Forests cave gates on ATT
# attribute 0x0006 (iso_patcher._DF_FOREST_ATTR, live-verified) -- trust that.
# (An older histogram pass, re_only/owclass.py, read 0x0003 as forest; the two
# were never reconciled and nothing consumes the 0x0003 value.)
# Overworld encounter-zone table (rando_data 'zones_overworld', ISO 0x2b218e4):
# 64 zones x 8 formation-id slots = 512 bytes, fixed RAM home (boot_patch.
# table_ram_addr). The game re-reads it at each encounter roll, so a live write
# changes which formation the NEXT overworld fight uses.
OW_ZONE_COUNT = 64
OW_ZONE_SLOTS = 8
OW_ZONE_TABLE_LEN = OW_ZONE_COUNT * OW_ZONE_SLOTS   # 512

# --- Static treasure (chest contents) TABLE in the ISO (2026-06) ---
# FFRPSP offsets (github.com/gameboy9/FFRPSP), verified byte-for-byte vs our ISO.
# ABSOLUTE file offset into the ISO image; 268 chests, 4-byte records, stride 4.
#   start 0x2b227d8 .. end 0x2b22c0a.
#   record [b0,b1,b2,b3]:
#     b3==0x80 -> item chest: b0=category(0 key/1 item/2 weapon/3 armor), b1=id, b2=qty
#                (b0==0 & b1==0 & b3==0x80 => empty/event-granted chest)
#     b3!=0x80 -> gil chest: whole 4 bytes = u32 LE gil amount
# Category/id values match the inventory tables above. Decoder: treasure_table.py
# (dumps treasure_table.csv: 106 gil, 62 item, 51 armor, 33 weapon, 6 key, 10 empty).
TREASURE_TABLE_START = 0x2b227d8
TREASURE_TABLE_END   = 0x2b22c0a
TREASURE_COUNT       = 268

# --- Give-item routine (found 2026-06 via memory-write breakpoint + disasm) ---
# PROVEN universal hook for item/location detection. The game's "add item to
# inventory" function entry is 0x088D4494, called as:
#     a0 = inventory base/struct
#     a1 = category   (0=key,1=item,2=weapon,3=armor)
#     a2 = item id     (within category)
#     a3 = quantity
# It stores a1->record[+0], a2->record[+1], qty->record[+2] (confirms our layout).
# Detection that WORKS with PPSSPP JIT: set a MEMORY-WRITE breakpoint on the
# inventory region (0x08D12034 + ~0x90); it halts inside the fn at ~0x088D454C
# where a1/a2/a3 STILL hold (category,id,qty). Read them + 'ra' (= the chest/event
# call site, which identifies the AP location). Tool: item_catch.py.
# NOTE: exec breakpoints (cpu.breakpoint.add) do NOT fire reliably under JIT here;
# use memory breakpoints. Requires ini [CPU] FastMemoryAccess=False.
GIVE_ITEM_FN = 0x088D4494

# --- Chest handler (FULLY RE'd 2026-06-27 via PPSSPP disasm) ---------------------
# z_un_08843bbc @ 0x08843BBC is the COMMON open-chest handler for ALL static chests
# (item AND gil). It is chest-specific: equip 'Optimal', shops, and battle drops do
# NOT pass through it (unlike GIVE_ITEM_FN, which they DO -> hooking the fn caused
# the Optimal-equip freeze). Inside the handler:
#   s1 = chest object base ; chest record = [s1+0x52C8] ; idx = [rec+0x1C] & 0x7FFF
#   a1 = RUNTIME_TREASURE_TABLE[idx]  (u32):
#        bit31 SET  -> item chest: category = a1&0xFF, id = (a1>>8)&0xFF, qty 1
#                      granted via GIVE_ITEM_FN at call site CHEST_ITEM_CALL
#        bit31 CLEAR -> gil chest: amount = a1, granted via GIL_GIVE_FN at CHEST_GIL_CALL
# The chest-open flag (bitfield at [s1+0x768]) is set BEFORE these calls, so skipping
# a call suppresses the loot WITHOUT un-flagging the chest (it stays opened).
CHEST_HANDLER     = 0x08843BBC
CHEST_ITEM_CALL   = 0x08843D74   # jal GIVE_ITEM_FN  (item chests)
CHEST_ITEM_SKIP   = 0x08843D7C   # resume here to suppress the item grant (past delay slot)
CHEST_GIL_CALL    = 0x08843DC0   # jal GIL_GIVE_FN   (gil chests)
CHEST_GIL_SKIP    = 0x08843DC8   # resume here to suppress the gil grant (past delay slot)
GIL_GIVE_FN       = 0x088D41C0
# Runtime (in-RAM) treasure table, indexed by idx*4. Decodes byte-for-byte to the
# ISO treasure table / treasure_table.csv (verified idx 0,1,2,5,100(gil 7600),etc).
RUNTIME_TREASURE_TABLE = 0x08946784
# Persistent opened-chest bitfield (canonical/delta-0; sa()-wrap in the client).
# bit# = treasure idx (LSB-first), 268 bits -> 34 bytes. Lives at save-block
# base(SAVE_BLOCK_PTR_CANON=0x08D11100) + 0x3EC, i.e. INVENTORY_BASE_SA - 0xB48;
# LIVE-VERIFIED 2026-07-03 by opening Chaos Shrine chests idx 0/1/2 -> field read
# 0x01/0x02/0x04 exactly (bit#=idx), and confirmed inside the SAVE_BLOCK_PTR-
# tracked block so sa() follows it. (The old 0x08D12C68 was base+0x1B68 = a dense
# chest-record array -> read as bits = garbage -> spurious checks. FIXED.)
CHEST_OPEN_BF_SA  = 0x08D114EC
CHEST_OPEN_BF_BYTES = 34
# Benign filler baked into remote chests' treasure byte0/1 (safe native grant --
# GIVE_ITEM_FN is a bound-186 packed-record append, no id bound); the client
# removes one per remote chest opened. cat=CAT_ITEM(1), id=1 = Potion.
CHEST_FILLER_CAT  = 1
CHEST_FILLER_ID   = 1

# --- Soul-of-Chaos bonus dungeons: DYNAMIC (procedural) chest detection ---------
# The 4 bonus dungeons regenerate every entry, so their procedural chests have NO
# static treasure index (they DON'T set CHEST_OPEN_BF bits -- live-verified) and are
# invisible to the poll. We detect them with the (otherwise retired) chest exec bps,
# armed ONLY inside a bonus dungeon so the JIT tax stays out of normal play.
#
# BONUS GATE (cheap 1-byte): the live CANONICAL encounter map-id at BONUS_MAPID_ADDR
# reads >= 0x87 (the DLC formation-table range) iff the party is in a bonus dungeon;
# normal caves are < 0x87. (RE 2026-07-10 via encounter_census DLC map-id set +
# rando._CAVE_DUNGEONS; Marsh=0x59, Earthgift F1=0x8a.)
BONUS_MAPID_ADDR  = 0x08D130F4   # u8, save-relative (sa()); >= 0x87 => bonus dungeon
BONUS_MAPID_MIN   = 0x87
# PER-DUNGEON MAPID BANDS (live floor-table dump 2026-07-21, Lifespring fresh
# over Whisperwind stale): each dungeon draws its per-floor mapids from a fixed
# CONTIGUOUS range whose width == its floor count (5/10/20/40, ranges abut:
# 0x87..0xD1). Confirms prior spot-captures (Earthgift F1=0x8A F2=0x8B,
# Hellfire F2=0x91, Lifespring floors all 0x96-0xA9, Whisperwind 0xAA-0xD1).
# 0xD2/0xD3 (dlc_mapD2D3_95 census table) sit OUTSIDE the bands -- special
# floors, band unknown -> resolver skips them. THE band is the dungeon
# identity; the old whole-table floor-count read breaks on stale tails.
BONUS_MAPID_BANDS = [
    (0x87, 0x8B, 0),   # Earthgift Shrine   (5 floors)
    (0x8C, 0x95, 1),   # Hellfire Chasm     (10)
    (0x96, 0xA9, 2),   # Lifespring Grotto  (20)
    (0xAA, 0xD1, 3),   # Whisperwind Cove   (40)
]
BONUS_BAND_VOTE = 5    # table records voted; every dungeon has >= 5 floors
# Bands where the id-35 display borrow (RUNE_MENU_SLOT_*) must be RELEASED:
# only Whisperwind Cove, the sole dungeon whose events can grant Battery
# Circuit. Everywhere else the borrow is held, so the "Runes N of M" line stays
# readable exactly where the runes are being found. A wrong guess here is a
# one-constant fix: add the band.
RUNE_BORROW_RELEASE_BANDS = frozenset({3})   # 3 = Whisperwind Cove, 0xAA..0xD1


def bonus_mapid_band(v):
    """Dungeon index for a bonus-floor mapid, or None (gimmick/special/out)."""
    for lo, hi, dg in BONUS_MAPID_BANDS:
        if lo <= v <= hi:
            return dg
    return None
# DUNGEON ID: the current dungeon's per-floor map-id table. stride-6 records, map-id
# byte at record+2; the COUNT of records with mapid >= 0x87 == the dungeon's FLOOR
# COUNT, which is unique per dungeon (5/10/20/40) -> the dungeon index. Stable across
# floors, save-persistent, what the pause menu ("Hellfire Chasm B2") reads. (RE
# 2026-07-10: Earthgift=5 entries, Hellfire=10, live-verified.)
BONUS_FLOOR_TABLE_SA   = 0x08D11940
BONUS_FLOOR_STRIDE     = 6
BONUS_FLOOR_MAPID_OFF  = 2
# (The old whole-table floor-count -> dungeon scheme is gone: the client now
# reads BONUS_BAND_VOTE records and maps their map-ids through
# BONUS_MAPID_BANDS above -- the band IS the dungeon identity.)
# Remote AP names bake into the extended item NAME bank; the remote-chest
# treasure u32 carries the absolute string id in bits16-30 and the on-disc
# detour writes it as the box {NAME} string id. The base (first remote entry's
# string id) is the number of NON-remote bank entries, which depends on whether
# the spell_tomes tome block is present:
#   spell_tomes ON  -> 43 vanilla + 64 tomes  -> base 107
#   spell_tomes OFF -> 43 vanilla (no tomes)  -> base 43
# The scout picks the base from the on-disc spell_tomes flag; extern_bake grows
# the same bank so remote-name display no longer requires spell_tomes.
CHEST_REMOTE_SID_BASE = 107          # spell_tomes ON (43 vanilla + 64 tomes)
CHEST_REMOTE_SID_BASE_NO_TOMES = 43  # spell_tomes OFF (43 vanilla only)
CHEST_REC_PTR_OFF = 0x52C8       # [s1+0x52C8] = chest record ; +0x1C = idx (u16)
GIVE_ITEM_INNER_PC = 0x088D454C   # where the mem-write bp halts; a1/a2/a3 valid

# --- Event-granted (vanilla key-item) chests (DIAGNOSED LIVE 2026-07-07) ----------
# The 6 vanilla key-item chests are cat-0 treasure-table entries. The chest handler
# 0x08843BBC does NOT grant these -- a map EVENT (FIF) grants the key item and prints
# a hardcoded "You found <key item>" box; the treasure table (and its runtime copy at
# RUNTIME_TREASURE_TABLE) is IGNORED for them. PROVEN live: runtime table idx 8 held
# the baked AP item (Mind Plus, cat1/id41) yet opening the chest still granted vanilla
# Nitro Powder + showed its name; no AP item reached the inventory (the client had
# classed it an OWN chest -> counter-only delivery relying on a native grant that gave
# the WRONG item). So these chests are handled specially by the client: the AP item is
# delivered through the grant loop (NOT counter-only) and the free native key item is
# stripped on the chest-open poll (possession bit via key_item_bit + any function bit
# in KEY_ITEM_FUNCTION_BITS). The box still shows the vanilla name until the event
# message itself is redirected (FIF-event RE, separate work). Map: treasure idx ->
# vanilla native KEY_ITEMS id (from treasure_table.py decode of the unpatched ISO).
# idx 198 (Levistone) is a phantom (LEVISTONE_TREASURE_IDX, dropped from the pool and
# handled by the NPC loop's event-bit path) so it is deliberately NOT here.
EVENT_KEY_CHESTS = {
    8:   6,    # Cornelia Castle - Chest 5    -> Nitro Powder
    28:  13,   # Citadel of Trials - Chest 9  -> Rat's Tail
    41:  9,    # Cavern of Earth - Chest 16   -> Star Ruby
    139: 2,    # Marsh Cave - Chest 12        -> Crown
    244: 8,    # Sunken Shrine mermaid treasure trove -> Rosetta Stone
               # (older notes mislabeled this "Giant's Cave - Chest 5")
}

# Key items granted natively by an NPC handover / event pickup -> the NPC
# location ordinal randomized at that spot. Together with EVENT_KEY_CHESTS
# this covers every native key-item grant, and every one is stripped by the
# client (_npc_loop / _strip_event_key_natives), so per seed each key id
# surfaces in the "You obtain the {key}." key-item-add box at EXACTLY ONE
# location -> renaming its KEY_NAME.MSG entry to that location's AP item
# name is a safe global rename (extern_bake key_names).
KEY_NPC_ORDINALS = {
    1:  PRINCESS_NPC_ORDINAL,     # Lute
    3:  CRYSTAL_EYE_NPC_ORDINAL,  # Crystal Eye (Astos)
    4:  JOLT_TONIC_NPC_ORDINAL,   # Jolt Tonic (Matoya)
    5:  MYSTIC_KEY_NPC_ORDINAL,   # Mystic Key (Elf Prince)
    7:  ADAMANTITE_NPC_ORDINAL,   # Adamantite pickup
    10: EARTH_ROD_NPC_ORDINAL,    # Earth Rod (Sarda)
    11: LEVISTONE_NPC_ORDINAL,    # Levistone (Ice Cave pickup)
    12: CHIME_NPC_ORDINAL,        # Chime (Lefein)
    14: WARP_CUBE_NPC_ORDINAL,    # Warp Cube (Waterfall robot)
    15: BOTTLE_NPC_ORDINAL,       # Bottled Faerie (Caravan)
    16: OXYALE_NPC_ORDINAL,       # Oxyale (Gaia fairy)
    17: SAGE_NPC_ORDINAL,         # Canoe (Crescent Lake sage)
}
# Each event chest carries a byte = 1 at static_def+2 in its STATIC map-object record
# (BOOT.BIN; static_def = *(field+0x52C8 rec)). Part B v1 ("type-byte flip") theory:
# flipping that byte 1->0 on-disc converts the chest to a normal AP chest. REFUTED
# LIVE 2026-07-09: all 5 bytes verified 0x00 in RAM (patched-ISO bake in force) yet
# opening idx8 AND idx41 still ran the vanilla FIF event (key-item box + free native
# key). The byte is opcode 0x06's ARG inside an event BYTECODE STREAM, not a routing
# field the chest handler consults. Record structure (per chest, vanilla):
#   05 08 <ev16> 00 ...            event header (idx8=0x128, idx41=0x142, idx139=0x10b)
#   ... 06 04 01 ff ... 2d 04 <obj16> | 37 04 00 <keyid-1> | 2e 04 <msg16> ...
#   op 0x37 = grant key item (arg = key id - 1, matches all 5); op 0x2e = show
#   message (idx8=0x13ad Nitro, idx41=0x13b7 StarRuby, idx139=0x1398 Crown).
# Message text is NOT in the cracked font_map glyph encoding anywhere in RAM/ISO
# (0 hits) -> per-bank atlas/indices; string rewrite needs fresh RE. Real Part B =
# rewrite the map FIF event (chest-location-format memory) or crack the event msg
# bank; until then the chests are handled at runtime (strip + grant loop).
# (BOOT.BIN RAM<->ISO mapping lives in eboot_patch: ram2file / BOOT_ISO_OFF.)

# Event-script record addresses (ALL 5 captured live 2026-07-09; grant-op key ids
# verified to match each chest). Kept for the Part B RE resumption.
EVENT_KEY_CHEST_TYPE_RAM = {41: 0x089AAA70, 139: 0x089A99FC,
                            8: 0x089A9F00, 28: 0x089ABEEC, 244: 0x089AD0C4}

# --- event-key chest box name: UNSOLVED (cosmetic); delivery works ---
# Opening one of the 5 vanilla key-item chests runs its FIF event ("path A": native key
# + "You obtain the crown" box), ignoring the treasure table, so the box never shows the
# AP item name. The path-A/B decision reads the event-obj DONE-STATE at interaction and
# remains UNFOUND (read-bps dead in PPSSPP 1.15.3). Every attempted lever was refuted
# live: v40 map-data type-byte flip (event reads a decompressed copy); the runtime
# completion-flag poke (obj+0x42 only correlated, the guardian-tile event resets it); and
# v46's on-disc detour on the 0x08843c00 beq -- that beq is opcode 0x06's ARG, not path
# A/B (see [[event-key-chests]] 9th-session note). DELIVERY is nonetheless correct: the
# scout classifies these remote-style (filler baked + grant-loop delivery) and
# _strip_event_key_natives removes the native key. Box name stays "crown" -- cosmetic.
# Gil is granted by a separate routine (gil chest = no inventory write); cover the
# gil u32 at 0x08D12264 with its own watch/breakpoint if needed.

# --- Give-item routine (caught via memory write breakpoint, 2026-06) ---
# Memory write-breakpoint on inventory (0x08D12034) HALTS the CPU inside the
# add-to-stack routine. Confirmed code (FastMemoryAccess MUST be off for mem bps):
GIVE_ITEM_STORE_PC = 0x088D454C   # 'sb v0,0x2(t3)' path; t3=item record ptr, a3=qty add
GIVE_ITEM_CALLER_RA = 0x08843D7C  # caller = chest/event handler -> ID's the AP location
# Routine increments an existing stack: lb v1,2(t3); v0=v1+(a3&0xff); cap at 99; sb v0,2(t3).
# a0 = inventory base arg. For the CONNECTOR: set an exec breakpoint (cpu.breakpoint.add)
# at the give-item FUNCTION ENTRY (walk back from here / from ra) where args hold the
# item id + qty; and/or at GIVE_ITEM_CALLER_RA to identify which chest/event granted it.
# This = the universal item/location detection hook (works for chests, shops, drops).

# --- blood_magic description leg (baked on-disc via extern_bake) -----------------
# Activatable equipment (record +7 = use-cast spell id, nonzero) gets a cost
# sentence appended to its WEAPON_EXP/ARMOR_EXP.MSG desc entry when blood_magic
# is on. Gids are STATIC across seeds: the Tier-A shuffles touch equip masks and
# prices only, never +7 (rando.shuffle_who_equips_what / shuffle_equip_prices).
# Derived from the vanilla tables (rando._ACTIVATABLE_WEAPON_IDS idiom);
# test_rando.py asserts parity with the rando-side sets so drift fails CI.
ACTIVATABLE_WEAPON_GIDS = frozenset(
    {29, 30, 31, 32, 33, 36, 37, 42, 43, 44, 45, 46, 49, 50, 54, 60, 62, 64})
ACTIVATABLE_ARMOR_GIDS = frozenset({15, 16, 51, 67, 68})

# No '%' glyph exists in the menu font (name_banks.MENU_FONT), so the cost is
# spelled out. Encoded via NB.menu_encode and appended before each entry's TERM.
BLOOD_DESC_SUFFIX = " Costs 10 percent HP."

# Length is a HARD constraint, not style. The item-menu desc box is ONE LINE and
# ~62 glyphs wide, and it IGNORES the 0xc2 0x8d line-break control (LIVE
# 2026-07-29: vanilla "Transports party out of dungeons." draws on one line even
# though the entry contains breaks). The longest activatable base desc is 38
# glyphs ("A sword etched with words of the gods."), so the suffix may not exceed
# 24 -- the original " Costs 10 percent of max HP." (28) put Braveheart at 65 and
# ran off the right edge of the box.
BLOOD_DESC_MAX_GLYPHS = 62
BLOOD_DESC_LONGEST_BASE = 38


def blood_desc_bank(key):
    """DESC_BANKS[key]-shaped dict with BLOOD_DESC_SUFFIX appended to every
    activatable entry (before its TERM glyph). The payload GROWS and the offset
    table is rebuilt (bank-relative, same base). Single source of the
    transformed bank for BOTH the on-disc bake (extern_bake) and the client's
    shop-desc DataPatch baseline -- they must be byte-identical or the client's
    RAM signature misses the baked bank."""
    from . import name_banks as NB
    gids = {"weapons": ACTIVATABLE_WEAPON_GIDS,
            "armor": ACTIVATABLE_ARMOR_GIDS}[key]
    bank = NB.DESC_BANKS[key]
    payload, count, offs = bank["payload"], bank["count"], bank["entry_offsets"]
    ends = list(offs[1:]) + [len(payload) + 0x10]
    ents = [bytearray(payload[o - 0x10:e - 0x10]) for o, e in zip(offs, ends)]
    suffix = NB.menu_encode(BLOOD_DESC_SUFFIX)
    g2e = NB.BANKS[key]["gameid_to_entry"]
    for gid in sorted(gids):
        ei = g2e[gid]
        ents[ei] = ents[ei][:-1] + suffix + ents[ei][-1:]
    new_offs, p = [], 0x10 + count * 4
    for e in ents:
        new_offs.append(p)
        p += len(e)
    new_payload = (b"".join(o.to_bytes(4, "little") for o in new_offs)
                   + b"".join(bytes(e) for e in ents))
    return {"count": count, "first": bank["first"], "payload": new_payload,
            "entry_offsets": new_offs}
