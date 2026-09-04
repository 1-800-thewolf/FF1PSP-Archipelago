"""
Shared AP id scheme for FF1 PSP. Imported by BOTH the apworld (generation) and
the runtime client (ff1psp/client/ApClient.py). Pure functions, no ISO / no AP
deps so either side can import it cheaply.

Encoding (all ids are AP integer ids, offset from a private BASE):

  LOCATION id  = BASE + treasure_index            (0..255; 256..267 are dropped
                 by gen_apdata.LABYRINTH_DROP. == the value the give-item hook
                 reads -> reversible: idx = loc_id - BASE)

  ITEM id (non-gil) = BASE + ITEM_OFF + cat*256 + game_id
                 cat: 0=key 1=item 2=weapon 3=armor ; game_id 1..~75
                 unique per item TYPE; the pool may hold duplicates (same id).

  ITEM id (gil)     = BASE + GIL_OFF + amount      (one item TYPE per distinct
                 gil amount; reversible: amount = item_id - BASE - GIL_OFF)

  Victory event item = BASE + VICTORY            (goal = Chaos defeated)
"""

BASE     = 0x00FF1000       # 16715776; private to this world
ITEM_OFF = 0x00010000
GIL_OFF  = 0x00020000
VICTORY  = 0x00000FFF

# --- locations ---
def loc_id(treasure_index):      return BASE + treasure_index
def loc_index(location_id):      return location_id - BASE

# --- items ---
def item_id(cat, game_id):       return BASE + ITEM_OFF + cat * 256 + game_id
def item_cat_gid(iid):
    code = iid - BASE - ITEM_OFF
    return code // 256, code % 256          # (cat, game_id)

def gil_id(amount):              return BASE + GIL_OFF + amount
def gil_amount(iid):             return iid - BASE - GIL_OFF

# --- NPC (story-event) locations: real AP item checks that are NOT chests, so they
# live outside the 0..267 treasure-index space. Detected by the client via story
# flags, not the chest hook. n is a small stable ordinal (0=King, 1=Princess). ---
NPC_OFF  = 0x00050000
def npc_loc_id(n):               return BASE + NPC_OFF + n

# --- job-advancement items: an AP pool item that promotes a base job to its
# promoted form (e.g. Thief -> Ninja). Encoded by the base ("from") job id (0..5);
# the client maps from_job -> promoted job and writes the party class byte. This is
# its own id space so the client can tell it apart from real game items. ---
JOB_OFF  = 0x00060000
def job_item_id(from_job):       return BASE + JOB_OFF + from_job
def is_job_item(iid):            return BASE + JOB_OFF <= iid < BASE + JOB_OFF + 0x100

# --- shop AP-stock locations: one-time AP purchases injected into town stores
# (see rando.SHOP_AP_SLOTS). Each shop sells up to SHOP_STRIDE offers, one at a
# time (shop_ap_offers). shop = stable index into rando.SHOP_AP_SLOTS,
# k = offer number within that shop (0-based, purchase order). Detected by the
# client via placeholder-item purchase, not the chest hook. ---
SHOP_OFF = 0x00070000
SHOP_STRIDE = 8                  # id slots reserved per shop (>= max offers, 6)
def shop_loc_id(shop, k=0):      return BASE + SHOP_OFF + shop * SHOP_STRIDE + k
def is_shop_loc(lid):            return BASE + SHOP_OFF <= lid < BASE + SHOP_OFF + 0x100
def shop_loc_shop_k(lid):
    code = lid - BASE - SHOP_OFF
    return code // SHOP_STRIDE, code % SHOP_STRIDE      # (shop, k)

# --- vehicle items: AP pool items that grant an overworld vehicle by setting its
# story flag rather than an inventory record (e.g. Ship -> story-flag id5 =
# 0x08D1151C bit5, spawns the ship at Provoka). Own id space so the client applies
# them via a flag write, not the inventory. v = vehicle ordinal (0 = Ship). See the
# ship-bikke-flag memory. ---
VEHICLE_OFF = 0x00080000
SHIP_VEHICLE = 0
def vehicle_item_id(v):          return BASE + VEHICLE_OFF + v
def is_vehicle(iid):             return BASE + VEHICLE_OFF <= iid < BASE + VEHICLE_OFF + 0x100

# --- dynamic bonus-dungeon chest locations: the Soul-of-Chaos dungeons regenerate
# their layout every entry, so procedural chests have NO static treasure index. We
# model "the first X chests opened in dungeon D" as counter locations: dungeon =
# stable index 0..3 (see logic.BONUS_DUNGEONS), ord = 0-based chest ordinal within
# that dungeon (0..cap-1). Own id space so the client applies them via the map-gated
# chest bp + a per-dungeon open counter (persistence = AP sent_locations), NOT the
# 268-bit static bitfield. See the bonus-dungeon-chests memory. ---
DYNCHEST_OFF    = 0x000A0000
# id slots per dungeon (>= the 40 REGISTERED Whisperwind ordinals, i.e. its floor
# count). WARNING: NOT >= every yaml ceiling -- the Whisperwind option's range_end
# is 100, and any ordinal >= 64 would ALIAS into the NEXT dungeon's id block
# (dyn_chest_dungeon_ord would decode (3, 99) as (4, 35)) -- so caps above 64
# must never be honored without widening this stride.
DYNCHEST_STRIDE = 64
def dyn_chest_loc_id(dungeon, ordinal):
    return BASE + DYNCHEST_OFF + dungeon * DYNCHEST_STRIDE + ordinal
def is_dyn_chest(lid):
    return BASE + DYNCHEST_OFF <= lid < BASE + DYNCHEST_OFF + 0x1000
def dyn_chest_dungeon_ord(lid):
    code = lid - BASE - DYNCHEST_OFF
    return code // DYNCHEST_STRIDE, code % DYNCHEST_STRIDE   # (dungeon, ordinal)

def dyn_chest_pending(dungeon, cap, checked):
    """(next ordinal to send, ordinals still unchecked) for one bonus dungeon.

    `checked` is any container of location ids -- the client passes its
    sent_locations. The next ordinal is the LOWEST unchecked one under the cap,
    NOT the count of checked ones: those agree only while the checked set is a
    contiguous prefix, and it is not the client's alone to fill. sent_locations
    also absorbs the server's `checked_locations`, so a !collect by a finished
    player marks every location holding one of THEIR items as checked --
    dynamic-chest ordinals this player never opened included. Counting then
    lands on an ordinal that is already checked, _bonus_dyn_loop's
    `lid not in sent_locations` guard eats the mailbox ring entry without
    sending anything, and the counter can never move again: from that chest on
    the dungeon shows the same box name forever and sends no check while the
    on-disc cave keeps stripping the vanilla grant (live 2026-08-30, Yine: a
    collect punched Hellfire ordinal 7 and froze it at chest 15, and nine
    collected Whisperwind ordinals froze that one at chest 10).

    The cap is clamped to DYNCHEST_STRIDE: ordinals past the stride alias into
    the next dungeon's id block (see the ceiling note in options.py), so they
    are never offered and the count reaching 0 lets the cave fall back to
    vanilla loot instead of stripping grants nothing replaces.
    """
    cap = min(cap, DYNCHEST_STRIDE)
    unsent = [o for o in range(cap)
              if dyn_chest_loc_id(dungeon, o) not in checked]
    return (unsent[0] if unsent else cap), len(unsent)

# --- EXP-bag items: synthetic filler that grants a FIXED EXP amount to ALL four
# party members on receipt. There is NO native game item; the client applies it by
# adding to each member's P_EXP save field (grant_exp). The EXP amount is encoded in
# the id like gil (amount = iid - BASE - EXP_OFF), so one id-per-amount. Filler-rolled
# only (see FILLER_ITEM_NAMES); never guaranteed to appear in a seed. ---
EXP_OFF = 0x00090000
def exp_item_id(amount):         return BASE + EXP_OFF + amount
def is_exp(iid):                 return BASE + EXP_OFF <= iid < BASE + EXP_OFF + 0x10000
def exp_amount(iid):             return iid - BASE - EXP_OFF

# --- Lute Tablet: synthetic progression item (lute_tablets yaml). N identical
# copies enter the pool; the Lute itself leaves the pool and is instead placed at
# a locked "Lute Assembled" event gated on holding lute_tablets_required tablets.
# NOT a game item -- the client counts received copies and sets the Lute
# possession bit (0x08D1153B b7) once the threshold is met. One id for all copies.
TABLET_OFF = 0x000B0000
def tablet_item_id():            return BASE + TABLET_OFF
def is_tablet(iid):              return iid == BASE + TABLET_OFF

# --- Equipment Rune: synthetic item (equipment_runes yaml). N identical copies
# enter the pool; holding equipment_runes_required of them assembles the
# "Equipment Rune Key" (never itself an item) -- the client then sets story flag
# 62, which the on-disc battle-usability gate reads to allow activating
# spell-on-use equipment as a battle item. Counter-only for the client (never
# granted in-game). One id for all copies.
RUNE_OFF = 0x000C0000
def rune_item_id():              return BASE + RUNE_OFF
def is_rune(iid):                return iid == BASE + RUNE_OFF

# --- Levistone Shard: synthetic progression item (levistone_shards yaml). N
# identical copies enter the pool; the Levistone itself leaves the pool and is
# instead placed at a locked "Levistone Assembled" event gated on holding
# levistone_shards_required shards. NOT a game item -- the client counts received
# copies and, at the threshold, grants the real Levistone (possession bit +
# obtained/airship function bits = airship raised). One id for all copies.
SHARD_OFF = 0x000D0000
def shard_item_id():             return BASE + SHARD_OFF
def is_shard(iid):               return iid == BASE + SHARD_OFF

# --- trap items: synthetic filler-replacing items (trap_percentage yaml) whose
# whole effect is client-side mischief (kill/status/sap the party, boost the next
# battle's monsters). No native game item; the
# client enqueues the trap at grant time (it rides the received-counter pipeline,
# so a death/reload rollback re-fires it exactly like a re-granted EXP bag) and
# fires it at a random upcoming battle. kind = small stable ordinal, one AP item
# id per kind. Ordinals are FROZEN and retired ones (5, Monster Magnet) leave a
# permanent gap -- see TRAP_ITEMS / RETIRED_TRAP_KINDS in ff1psp.__init__. ---
TRAP_OFF = 0x000E0000
def trap_item_id(kind):          return BASE + TRAP_OFF + kind
def is_trap(iid):                return BASE + TRAP_OFF <= iid < BASE + TRAP_OFF + 0x100
def trap_kind(iid):              return iid - BASE - TRAP_OFF

# --- party-member items: synthetic progression items (starting_party_members
# yaml) that restore a MISSING starting-party member. Encoded by BASE job id
# 0..5 like the job scrolls (its own id space so the client can tell them
# apart); a party with duplicate jobs simply has duplicate copies of one id.
# NOT a game item -- the client treats them counter-only and matches received
# copies against the slot_data "missing_party" plan (bottom rows first) to
# revive the member's save record. See the reduced-party memory. ---
PARTY_OFF = 0x000F0000
def party_member_item_id(job):   return BASE + PARTY_OFF + job
def is_party_member(iid):        return BASE + PARTY_OFF <= iid < BASE + PARTY_OFF + 0x100
def party_member_job(iid):       return iid - BASE - PARTY_OFF

# --- bestiary (bestiarysanity) locations: one per sampled monster id. The key
# is the MONSTER id (0x00-0xCA, the kill-count index at save+0x1198), NOT the
# bestiary display order. Chronodia (0xC3-0xCA) has no formation records and is
# never registered. ---
BESTIARY_OFF = 0x00100000
def bestiary_loc_id(mon):        return BASE + BESTIARY_OFF + mon
def is_bestiary_loc(lid):        return BASE + BESTIARY_OFF <= lid < BASE + BESTIARY_OFF + 0x100
def bestiary_loc_mon(lid):       return lid - BASE - BESTIARY_OFF

# --- world-changing items: REAL pool items (found anywhere in the multiworld)
# that have no in-game existence at all -- no key-item bit, no inventory slot,
# no function bit. What they change is the OVERWORLD MAP: the client paints a
# gated set of grid cells once one is held (openworld_data's
# LUFENIAN_BRIDGE_BUILT / *_BOULDER_OPEN / GULG_SEAL tables). So the client
# treats them counter-only, exactly like an event token, but unlike an event
# token the world PLACES them in the pool rather than precollecting them.
# n = a small stable ordinal (see ff1psp.__init__.WORLD_ITEM_NAMES).
WORLD_OFF = 0x00110000
# Ordinals are FROZEN -- they are the datapackage ids. Appending is safe;
# reordering renumbers live seeds. Single-sourced here because BOTH the world
# (ff1psp.__init__, by name) and the client (ApClient._openworld_loop, by id)
# have to agree on which item is which.
WORLD_LUFENIAN_BRIDGE = 0
WORLD_LUFENIAN_NITRO  = 1
WORLD_GAIAN_NITRO     = 2
WORLD_MOUNT_GULG_KEY  = 3
def world_item_id(n):            return BASE + WORLD_OFF + n
def is_world_item(iid):          return BASE + WORLD_OFF <= iid < BASE + WORLD_OFF + 0x100

# --- event (gate) items: synthetic progression tokens (Bridge, Canal, ...) the
# world precollects / places at event locations. They are NOT game items; the
# client treats them as counter-only. Range must match ff1psp.__init__._EVENT_BASE.
EVENT_OFF = 0x00030000
def is_event(iid):               return BASE + EVENT_OFF <= iid < BASE + EVENT_OFF + 0x10000

# --- classification helpers (used by the client to invert an AP item id) ---
def is_gil(iid):                 return iid >= BASE + GIL_OFF and iid < BASE + GIL_OFF + 0x10000
def is_victory(iid):             return iid == BASE + VICTORY
def is_item(iid):
    return BASE + ITEM_OFF <= iid < BASE + GIL_OFF
