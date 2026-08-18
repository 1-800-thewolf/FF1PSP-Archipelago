"""
FF1 PSP Tier-A data-table shufflers (pure, seed-reproducible).

Each function takes a `random.Random` (the AP world's `self.random`) plus the
vanilla table bytes (from rando_data.VANILLA) and returns a mutated bytearray of
the SAME length -- an in-place overwrite, exactly what boot_patch.DataPatch wants.

Transforms are ported from gameboy9's FFRPSP (re_only/FFRPSP_Form1.cs); we mirror
the byte layout / value ladders, NOT the C# System.Random sequence (we use AP's
seeded rng so results are reproducible from the AP seed).

Feature -> table(s):
  shop_items        -> shops (weapon/armor/item stores ONLY; per-slot reroll
                        within its own category/tier -- NOT a swap of one
                        shop's contents with another's)
  magic_shops       -> shops (magic stores: a PERMUTATION of the 32 spells per
                        color across that color's shop slots, so every spell is
                        still sold exactly once) + magic_learn (rebuilt so each
                        class learns the same NUMBER of spells from each shop
                        as vanilla -- e.g. red mage still learns 2 of the 4
                        spells Cornelia's white shop now sells) + magic_info(+9)
                        (spell level realigned to its new shop's tier)
  item_prices       -> item_buy_prices, weapons(+20..26), armor(+20..26), magic_info(+12/13)
                        (log-uniform x0.25..x4 of that item's own vanilla price)
  spell_mp          -> magic_info(+10) (log-uniform x0.25..x4 of that spell's
                        own vanilla MP cost -- independent of spell "level")
  equip_perms       -> weapons(+2/3), armor(+2/3) (column-permutation: each
                        job's total equippable-item COUNT is preserved,
                        membership is randomized)
  overworld_harder  -> per-tier precomputed THREAT pools (_OW_HARDER_POOL,
                        v192 -- the stepped index bands are gone; see the note
                        at the pool literal) with real trash floors, forced
                        hand-picks (_OW_HANDPICK), + curated boss cameos
                        hand-placed by region (Elfheim/Onrac/Trials/Lufenia).
                        OFF still rolls each zone's NAMED-tier band (the map
                        corrections apply in both modes). Boss formations are
                        always stripped from the random draws (_BOSS_POOL_EXCLUDE).
  dungeon_harder    -> zones_caves tags stepped one tier up + each boss dropped
                        as a rare single-slot cameo three dungeons past its home.
                        Rerolls ONLY the cave table. (With neither harder toggle
                        set, encounters stay vanilla -- there is no plain shuffle.)

(shuffle_spell_learn was removed: magic-learn randomization is now driven by
shuffle_magic_shops, which ties WHO learns a spell to WHERE it is sold.)

Class stat GROWTH (task #6) is intentionally absent: FFRPSP has no growth
randomizer and the growth table offset is not yet reverse-engineered. See report.
"""
import base64
import math

from . import rando_data as RD
from .spell_data import SPELL_NAMES


# ---------------------------------------------------------------- price helper
_PRICE_CAP_ITEM = 99999
_PRICE_CAP_MAGIC = 65500


# default multiplier bounds (fraction of vanilla). Overridable per-run via yaml.
_DEFAULT_PRICE_RANGE = (0.25, 4.0)
_MIN_PRICE = 5  # random prices never cost less than this many gil


def _norm_range(rng_bounds):
    """(low, high) fractions -> sanitized (low, high), low<=high, both >0."""
    if not rng_bounds:
        return _DEFAULT_PRICE_RANGE
    lo, hi = rng_bounds
    lo = max(1e-6, float(lo))
    hi = max(1e-6, float(hi))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _rand_price(rng, vanilla, cap, bounds=_DEFAULT_PRICE_RANGE):
    """Randomize a price around its vanilla value (log-uniform over `bounds`,
    a (low, high) fraction of vanilla), clamped to [_MIN_PRICE, cap].
    0-priced (unsellable) entries stay 0."""
    if vanilla <= 0:
        return 0
    lo, hi = bounds
    factor = math.exp(rng.uniform(math.log(lo), math.log(hi)))
    v = int(vanilla * factor)
    return max(_MIN_PRICE, min(cap, v))


# AP shop offers have no vanilla price to scale, so each offer rolls its OWN
# base (log-uniform over this range) and the item price range is then applied on
# top of that. Two stacked multiplicative spreads => AP prices vary far more than
# ordinary stock: a cheap base pushed low bottoms out at _MIN_PRICE, an expensive
# base pushed high runs into _PRICE_CAP_ITEM.
_AP_BASE_RANGE = (300, 5000)


def rand_ap_base_price(rng):
    """One AP shop offer's base price: log-uniform over _AP_BASE_RANGE.
    Rolled regardless of Randomize Prices -- this is the AP offer's stand-in for
    a vanilla price, not a randomization of one."""
    lo, hi = _AP_BASE_RANGE
    return int(math.exp(rng.uniform(math.log(lo), math.log(hi))))


# ---------------------------------------------------------------- #3 spell MP
_MP_CAP = 99  # single byte, but no spell should cost more than this


# Spells strong enough that a cheap MP roll warps the game (party-wide buffs and
# the top nukes/heals). With `costly_best` set they roll over the UPPER HALF of
# the range only. Names resolve to magic indexes via SPELL_NAMES, so a rename
# can't silently point the set at the wrong spell.
_COSTLY_BEST_SPELL_NAMES = ("Temper", "Haste", "Flare", "Heal", "Healara", "Healaga", "Full-Life")
COSTLY_BEST_SPELLS = frozenset(
    SPELL_NAMES.index(n) for n in _COSTLY_BEST_SPELL_NAMES
)


def resolve_costly_spells(costly):
    """Normalize the `costly_best_spells` yaml value to a list of magic indexes
    (0..63), duplicates dropped, yaml order preserved.

    Yaml order is cosmetic: the MP branch treats the list as a set, and
    force_costly_spell_levels re-sorts by vanilla spell level with a random
    tiebreak. Nothing gives an earlier yaml entry priority.

    Accepts False/None (empty), True (the legacy default set), or any iterable
    of spell names / magic indexes. Unknown names are dropped here; the option
    class (options.CostlyBestSpells, valid_keys) is what rejects a typo at
    generation time, so this stays lenient for direct/test callers."""
    if not costly:
        return []
    if costly is True:
        costly = _COSTLY_BEST_SPELL_NAMES
    out, seen = [], set()
    for entry in costly:
        if isinstance(entry, int):
            idx = entry
        else:
            try:
                idx = SPELL_NAMES.index(str(entry))
            except ValueError:
                continue
        if 0 <= idx < 64 and idx not in seen:
            seen.add(idx)
            out.append(idx)
    return out


def shuffle_spell_mana_costs(rng, magic_info, bounds=None, costly_best=False):
    """Randomize each spell's MP cost (+10 in the 14-byte record) by a
    log-uniform multiplier on its OWN vanilla MP cost -- independent per spell,
    no coupling to spell 'level' or the vanilla level ladder. So a level-7 spell
    can land on 5 MP and a level-1 spell can land on 50 MP.

    `bounds` = (low, high) fractions of vanilla (e.g. (0.5, 2.5)); defaults to
    x0.25..x4 when omitted. MP is clamped to 1.._MP_CAP.

    `costly_best` (see resolve_costly_spells -- a spell-name list, True for the
    legacy default set, or False) restricts those spells to the upper half of
    that range in LOG space -- i.e. [sqrt(lo*hi), hi]. Still random, still inside
    the player's stated bounds (so a sub-1.0 range stays sub-1.0), just never
    cheap relative to the rest of the roll."""
    lo, hi = _norm_range(bounds) if bounds else (0.25, 4.0)
    log_lo, log_hi = math.log(lo), math.log(hi)
    log_mid = (log_lo + log_hi) / 2.0   # geometric mean = midpoint in log space
    costly = set(resolve_costly_spells(costly_best))
    out = bytearray(magic_info)
    stride, count = 14, 64
    for i in range(count):
        b = i * stride + 10
        van_mp = out[b]
        f_lo = log_mid if i in costly else log_lo
        factor = math.exp(rng.uniform(f_lo, log_hi))
        new_mp = max(1, min(_MP_CAP, int(round(van_mp * factor))))
        out[b] = new_mp
    return out


# ---------------------------------------------------------------- #5 who learns
# magic_learn = 2 bytes/spell = class bitmask. Bit layout (decoded from vanilla,
# matches FFRPSP's randomizeMagic construction). Each class contributes fixed
# bits into byte0 / byte1 of the record. Spells 0..31 white, 32..63 black.
#   class -> (byte_index, bitmask)
# (byte,mask) here is the SAME physical bit the game's can_learn(job) reads via
# bit = (job%6)+(job//6)*8, so u16 bit8 = job6 = KNIGHT (the white-magic hybrid)
# and u16 bit9 = job7 = NINJA (the black-magic hybrid). Keys are just labels for
# the rebuild loop -- what matters is the (byte,mask). Don't change a mask.
_WHITE_CLASS_BITS = {
    "white_mage":   (0, 0x10),
    "red_mage":     (0, 0x08),
    "white_wizard": (1, 0x10),
    "red_wizard":   (1, 0x08),
    "knight":       (1, 0x01),   # u16 bit8 = job6 (Knight); Ninja learns no white
}
_BLACK_CLASS_BITS = {
    "black_mage":   (0, 0x20),
    "red_mage":     (0, 0x08),
    "black_wizard": (1, 0x20),
    "red_wizard":   (1, 0x08),
    "ninja":        (1, 0x02),   # u16 bit9 = job7 (Ninja); Knight learns no black
}
# base class -> promoted class whose learn set must stay a SUPERSET of the
# base's, so promoting never loses access to a spell you could already learn.
_PROMOTION_PAIRS = {
    "white_mage": "white_wizard",
    "red_mage":   "red_wizard",
    "black_mage": "black_wizard",
}


# ---------------------------------------------------------------- #4 equip perms
def shuffle_who_equips_what(rng, records, count):
    """Randomize WHICH items each job can equip, while keeping each job's total
    item count the same as vanilla (a monk who could equip N weapons still can
    equip N weapons -- just a different N).

    +2 (equip1) and +3 (equip2) are both 6-bit job masks; equip2 is a superset
    of equip1 in vanilla data (e.g. equip1 = 'can equip', the extra bits in
    equip2 = a secondary permission tier), and the two are disjoint by
    construction (extra = equip2 bits not already in equip1). For each of the
    6 job bits we take the vanilla equip1 population count `a` and the
    vanilla 'extra' population count `b`, then draw `a` fresh item slots for
    equip1 and `b` MORE fresh slots (from the remaining items, so it can never
    collide with the new equip1 slots) for extra. This reproduces both the
    per-job equip1 COUNT and the per-job equip2 COUNT exactly (since
    equip1 and extra stay disjoint, |equip2| = a + b just like vanilla),
    while randomizing which items hold each bit."""
    out = bytearray(records)
    stride = 28
    equip1 = [out[i * stride + 2] for i in range(count)]
    equip2 = [out[i * stride + 3] for i in range(count)]
    extra = [(e2 & ~e1) & 0x3F for e1, e2 in zip(equip1, equip2)]

    new_equip1 = [0] * count
    new_extra = [0] * count
    for bit in range(6):
        mask = 1 << bit
        a = sum(1 for i in range(count) if equip1[i] & mask)
        b = sum(1 for i in range(count) if extra[i] & mask)
        idxs = list(range(count))
        rng.shuffle(idxs)
        for i in idxs[:a]:
            new_equip1[i] |= mask
        for i in idxs[a:a + b]:
            new_extra[i] |= mask

    # Each bit is drawn independently, so an item can lose every job bit and end
    # up equippable by NOBODY (Giant's Glove, reported live). Repair by MOVING a
    # bit from an item that holds two or more, within the SAME layer (equip1 or
    # extra) and the SAME bit -- a move is count-neutral, so every per-job
    # equip1/equip2 total still matches vanilla. Only items that were equippable
    # in vanilla are rescued; a vanilla-unequippable record stays that way.
    def _bits(i):
        return new_equip1[i] | new_extra[i]

    donors = [i for i in range(count) if bin(_bits(i)).count("1") >= 2]
    for i in range(count):
        if _bits(i) or not equip2[i]:
            continue
        for d in donors:
            if bin(_bits(d)).count("1") < 2:
                continue
            layer = new_equip1 if new_equip1[d] else new_extra
            mask = layer[d] & -layer[d]      # lowest set bit
            layer[d] &= ~mask
            layer[i] |= mask
            break

    for i in range(count):
        e1 = new_equip1[i]
        e2 = (e1 | new_extra[i]) & 0x3F
        out[i * stride + 2] = e1
        out[i * stride + 3] = e2
    return out


# ---------------------------------------------------------------- #2 prices
def shuffle_item_buy_prices(rng, item_buy_prices, bounds=_DEFAULT_PRICE_RANGE):
    """Randomize the 43 consumable buy prices (u24 LE @+0) and keep the paired
    sell price (u24 LE @+4) at buy//2, so a randomized buy can never end up
    below its sell (infinite-gil exploit)."""
    out = bytearray(item_buy_prices)
    stride = 16
    for i in range(43):
        b = i * stride
        v = out[b] | (out[b + 1] << 8) | (out[b + 2] << 16)
        v = _rand_price(rng, v, _PRICE_CAP_ITEM, bounds)
        out[b] = v & 0xFF
        out[b + 1] = (v >> 8) & 0xFF
        out[b + 2] = (v >> 16) & 0xFF
        # Sell tracks buy at buy//2 -- but only for genuinely buyable items.
        # Vanilla 0-buy consumables (unbuyable drops/finds) carry an independent
        # nonzero sell value, so leave those alone (v stays 0 via _rand_price).
        if v:
            s = v // 2
            out[b + 4] = s & 0xFF
            out[b + 5] = (s >> 8) & 0xFF
            out[b + 6] = (s >> 16) & 0xFF
    return out


def fix_item_sell_prices(item_buy_prices):
    """Clamp any buyable consumable whose sell price (u24 @+4) exceeds buy//2
    (u24 @+0) down to buy//2, so you can never buy an item for less than it
    sells (infinite-gil exploit). Clamp-DOWN only: sell already at or below
    buy//2 is left as-is -- the power-price multiplier deliberately inflates
    buy while leaving sell low (priceless/activatable stock), and that is not
    exploitable. RNG-neutral + idempotent. Records with buy==0 (unbuyable finds
    keep their independent vanilla sell) are untouched."""
    out = bytearray(item_buy_prices)
    stride = 16
    for i in range(len(out) // stride):
        b = i * stride
        buy = _read_u24(out, b)
        if buy and _read_u24(out, b + 4) > buy // 2:
            _write_u24(out, b + 4, buy // 2)
    return out


def shuffle_equip_prices(rng, records, count, bounds=_DEFAULT_PRICE_RANGE):
    """Randomize buy (u24 @+20) + sell (=buy/2, u24 @+24) of 28-byte records."""
    out = bytearray(records)
    stride = 28
    for i in range(count):
        b = i * stride
        v = out[b + 20] | (out[b + 21] << 8) | (out[b + 22] << 16)
        v = _rand_price(rng, v, _PRICE_CAP_ITEM, bounds)
        out[b + 20] = v & 0xFF
        out[b + 21] = (v >> 8) & 0xFF
        out[b + 22] = (v >> 16) & 0xFF
        s = v // 2
        out[b + 24] = s & 0xFF
        out[b + 25] = (s >> 8) & 0xFF
        out[b + 26] = (s >> 16) & 0xFF
    return out


def shuffle_magic_prices(rng, magic_info, bounds=_DEFAULT_PRICE_RANGE):
    """Randomize spell gil price (u16 @+12) in the 14-byte magic record."""
    out = bytearray(magic_info)
    stride = 14
    for i in range(64):
        b = i * stride
        v = out[b + 12] | (out[b + 13] << 8)
        v = _rand_price(rng, v, _PRICE_CAP_MAGIC, bounds)
        out[b + 12] = v & 0xFF
        out[b + 13] = (v >> 8) & 0xFF
    return out


# ---------------------------------------------------------------- #7 encounters
# Battle-rank order (difficulty-sorted formation ids). Port of randomizeMonsterZonesV2.
_BATTLE_RANK = [
    0x00, 0x80, 0x82, 0x05, 0x01, 0x04, 0x06, 0x86, 0x07, 0x02, 0x81, 0x03, 0x09, 0x08, 0x83, 0x84,
    0x7f, 0x7e, 0x85, 0x87, 0x0a, 0x8a, 0x0b, 0x0c, 0x8b, 0x0d, 0x8e, 0x5b, 0xdb, 0xdc, 0x5c, 0xdd,
    0x5d, 0x5e, 0xde, 0xe6, 0x66, 0x14, 0x94, 0x12, 0x92, 0x0f, 0x88, 0x8d, 0x8c, 0x0e, 0x8f, 0x10,
    0x90, 0x91, 0x11, 0x13, 0x93, 0x15, 0x1e, 0x1c, 0x9c, 0x95, 0x2b, 0x89, 0x16, 0x96, 0x17, 0xab,
    0x9f, 0x97, 0x9e, 0x18, 0x19, 0x1a, 0x1b, 0x1d, 0x1f, 0x98, 0x99, 0x9a, 0x9b, 0x9d, 0x20, 0xa0,
    0x21, 0x22, 0xa2, 0x7d, 0x23, 0xa3, 0x24, 0xa4, 0x25, 0xa5, 0x2c, 0x26, 0x27, 0xa6, 0xa7, 0x28,
    0x29, 0x2a, 0xa8, 0xac, 0xaa, 0x2d, 0xad, 0x2e, 0x2f, 0xaf, 0xa9, 0xae, 0xa1, 0x30, 0x31, 0x32,
    0xb2, 0xb5, 0x33, 0x34, 0x35, 0xb4, 0x36, 0x37, 0xd5, 0xb7, 0xb6, 0xbe, 0x39, 0x3a, 0x3b, 0xba,
    0xb9, 0xbb, 0x3c, 0x3d, 0xbd, 0xbc, 0x3f, 0xb0, 0xb1, 0xb3, 0xbf, 0x40, 0xc0, 0x41, 0xc1, 0x42,
    0xc2, 0x43, 0xc3, 0xc4, 0x44, 0x45, 0xcb, 0x47, 0x48, 0x49, 0xca, 0xc9, 0xcc, 0x7c, 0x4b, 0x4c,
    0x4d, 0xcf, 0x4f, 0xc5, 0xc7, 0xc8, 0x4a, 0xcd, 0x50, 0xd0, 0x51, 0xd1, 0x52, 0xd2, 0x53, 0xd3,
    0x54, 0xd4, 0x58, 0xd8, 0x5a, 0xda, 0x5f, 0xdf, 0xd6, 0xe0, 0x60, 0xe1, 0x61, 0x62, 0xe2, 0x63,
    0xe3, 0x64, 0xe4, 0x65, 0xe5, 0x67, 0xe7, 0x68, 0x6a, 0xea, 0x6b, 0xeb, 0x6d, 0xed, 0xee, 0x6e,
    0x6f, 0xef, 0x70, 0xf0, 0x72, 0xf2, 0x4e, 0xce, 0xc6, 0xe8, 0x55, 0x57, 0xd7, 0x69, 0x6c, 0xec,
    0xe9, 0x38, 0xb8, 0x71, 0xf1, 0x59, 0xd9, 0x3e, 0x46, 0x56, 0x7a, 0x79, 0x78, 0x77, 0x73, 0x74, 0x75, 0x76,
]

# ---- SHARED overworld zone map (foot encounters + Dangerous Forests) --------
# One canonical 8x8 zone->tier map, user-curated 2026-07-15 (interactive-editor
# rounds 1+2). Tiers are the 9 route stops 0..8: Cornelia, Pravoka, Elfheim,
# W.Keep, Melmond, Crescent, Onrac, Trials, Lufenia. iso_patcher._DF_ZONE_TIER
# MUST hold these same 64 values (test_rando enforces parity) -- forests and
# grass on a landmass always agree on "where" they are. Zone index uses the
# engine's +7 bias: zone = ((x+7)>>5) + 8*((y+7)>>5) (see encounter_census.py).
ZONE_TIER = (
    6, 6, 6, 7, 7, 7, 8, 8,   # z0-2 Onrac; z3-5 Trials; NE = Lufenia
    6, 6, 6, 7, 7, 7, 8, 8,
    6, 6, 6, 7, 7, 7, 7, 8,
    6, 6, 6, 1, 1, 1, 8, 8,   # Matoya pocket (z27-29) Pravoka; z30/31 peninsula tip
    4, 4, 4, 1, 0, 1, 1, 1,   # z36 Cornelia; z39 Pravoka (peninsula south landmass)
    4, 4, 4, 3, 0, 0, 1, 1,
    4, 4, 3, 2, 2, 5, 5, 5,   # z50 W.Keep; z51/52 Elfheim; z53+ Crescent
    4, 4, 3, 3, 2, 5, 5, 5,
)
_TIER_NAMES = ("Cornelia", "Pravoka", "Elfheim", "W.Keep", "Melmond",
               "Crescent", "Onrac", "Trials", "Lufenia")
TIER_NONE = 9   # reserved: no foot encounters (slots -> formation 0); unused today

# tier -> _BATTLE_RANK band. Vanilla (harder off) = cumulative (0..high), the
# FFRPSP-ported difficulty ceilings, keyed by the zone's NAMED tier. Harder =
# the named tier's band stepped one stop up PLUS a floor lagging two stops
# behind (floor(k) = high(k-2)+1), so low-rank trash phases out as tiers climb
# (no Goblins at Melmond+). Lufenia harder = a curated SPECIAL POOL (None
# sentinel), not a band draw.
_TIER_HIGH = (3, 6, 12, 24, 38, 48, 66, 83, 237)
_TIER_BAND = tuple((0, h) for h in _TIER_HIGH)
# HARDER MODE, v192: the index bands are GONE. They were slices of _BATTLE_RANK,
# whose order does NOT track difficulty, so a "floor" at index 25 removed nothing
# -- harder Crescent still rolled 1-2 Cobra (threat 4.1) next to 1-6 Ankheg (29.0)
# and felt identical to normal (user report 2026-08-01). Harder now draws from
# per-tier THREAT bands with a real floor: floor(k) = ceiling(k-2), so trash phases
# out two stops behind. Tiers 0/1 have no earlier tier and instead use an explicit
# cutoff the user set by inspection (Cornelia drops <=5.4, Pravoka drops <=6.0).
#
# The threat metric needs the ISO (monster stats + formation records) and this file
# runs at GENERATION time where no ISO exists, so the per-tier candidate lists are
# PRECOMPUTED into the literal below by re_only/gen_ow_pools.py. That script is the
# single source of truth for the bands, the water/boss/status rules and the DLC
# cutoff; test_patch.ow_pool_checks() regenerates it from the ISO and fails on
# drift. To retune: edit FLOOR/CEIL there, rerun it, paste the output here.
#
# Lists are content-deduped (identical monster multisets collapse to one id) so a
# zone cannot draw the same fight twice, and are sorted ascending by threat.
# Normal mode is UNCHANGED -- it still uses the cumulative _TIER_BAND index slices.
_OW_HARDER_POOL = (
    # 0 Cornelia: threat 5.45-10.2, 18 candidates
    (0x012, 0x086, 0x080, 0x009, 0x083, 0x007, 0x08d, 0x00c, 0x002, 0x010, 0x00b, 0x094, 
     0x0e6, 0x08e, 0x08b, 0x064, 0x087, 0x06b, ),
    # 1 Pravoka: threat 6.05-10.2, 16 candidates
    (0x009, 0x083, 0x0dd, 0x007, 0x08d, 0x00c, 0x002, 0x010, 0x00b, 0x094, 0x0e6, 0x08e, 
     0x08b, 0x064, 0x087, 0x06b, ),
    # 2 Elfheim: threat 10.2-13.8, 21 candidates
    (0x150, 0x011, 0x116, 0x013, 0x02b, 0x063, 0x01a, 0x13a, 0x160, 0x019, 0x08c, 0x01b, 
     0x0e4, 0x066, 0x12b, 0x141, 0x036, 0x091, 0x151, 0x090, 0x13b, ),
    # 3 W.Keep: threat 10.2-15.1, 29 candidates
    (0x150, 0x011, 0x116, 0x013, 0x02b, 0x063, 0x01a, 0x13a, 0x160, 0x019, 0x08c, 0x01b, 
     0x0e4, 0x066, 0x12b, 0x141, 0x036, 0x091, 0x151, 0x090, 0x13b, 0x085, 0x117, 0x06d, 
     0x08f, 0x09b, 0x0e3, 0x123, 0x029, ),
    # 4 Melmond: threat 13.8-22.5, 59 candidates
    (0x085, 0x117, 0x06d, 0x08f, 0x09b, 0x0e3, 0x123, 0x029, 0x0b6, 0x01e, 0x015, 0x022, 
     0x093, 0x14f, 0x0eb, 0x06a, 0x142, 0x0ee, 0x018, 0x11a, 0x0d5, 0x134, 0x06f, 0x20a, 
     0x129, 0x02d, 0x0ad, 0x039, 0x14c, 0x032, 0x01c, 0x0ab, 0x092, 0x03c, 0x143, 0x070, 
     0x09a, 0x168, 0x01f, 0x0ed, 0x028, 0x099, 0x135, 0x11b, 0x0ea, 0x01d, 0x089, 0x09e, 
     0x0a2, 0x04f, 0x0f0, 0x12a, 0x095, 0x06e, 0x09f, 0x0bc, 0x021, 0x030, 0x136, ),
    # 5 Crescent: threat 15.1-27.1, 114 candidates
    (0x0b6, 0x01e, 0x0a5, 0x015, 0x034, 0x022, 0x093, 0x14f, 0x0eb, 0x06a, 0x142, 0x0df, 
     0x03a, 0x0ee, 0x131, 0x0ec, 0x018, 0x11a, 0x0d5, 0x134, 0x06f, 0x069, 0x20a, 0x129, 
     0x02d, 0x0ad, 0x11c, 0x039, 0x14c, 0x0a3, 0x0a4, 0x025, 0x032, 0x01c, 0x0ab, 0x092, 
     0x03c, 0x143, 0x070, 0x09a, 0x154, 0x168, 0x01f, 0x0ed, 0x028, 0x099, 0x033, 0x135, 
     0x11b, 0x0ea, 0x01d, 0x060, 0x089, 0x09e, 0x041, 0x11d, 0x0a2, 0x04f, 0x0f0, 0x0c0, 
     0x12a, 0x095, 0x06e, 0x09f, 0x0bc, 0x021, 0x0e5, 0x0ba, 0x030, 0x136, 0x0ef, 0x16b, 
     0x027, 0x02e, 0x0e1, 0x096, 0x072, 0x017, 0x144, 0x00e, 0x067, 0x02f, 0x14a, 0x03b, 
     0x0b2, 0x0b4, 0x118, 0x03d, 0x0a9, 0x0e9, 0x165, 0x065, 0x137, 0x209, 0x0e7, 0x031, 
     0x09c, 0x0c1, 0x042, 0x043, 0x037, 0x158, 0x162, 0x03f, 0x052, 0x02a, 0x119, 0x12c, 
     0x0bd, 0x0b9, 0x026, 0x0c2, 0x0c3, 0x058, ),
    # 6 Onrac: threat 22.5-34.8, 99 candidates
    (0x0ef, 0x16b, 0x027, 0x02e, 0x0e1, 0x096, 0x072, 0x017, 0x144, 0x00e, 0x067, 0x02f, 
     0x14a, 0x03b, 0x0b2, 0x0b4, 0x118, 0x03d, 0x0a9, 0x0e9, 0x165, 0x065, 0x137, 0x209, 
     0x0e7, 0x031, 0x09c, 0x0c1, 0x042, 0x043, 0x037, 0x158, 0x162, 0x03f, 0x052, 0x02a, 
     0x119, 0x12c, 0x0bd, 0x0b9, 0x026, 0x0c2, 0x0c3, 0x058, 0x0bb, 0x059, 0x156, 0x04b, 
     0x20b, 0x047, 0x061, 0x0fe, 0x157, 0x0ac, 0x111, 0x258, 0x054, 0x159, 0x166, 0x0f2, 
     0x0cf, 0x097, 0x0b1, 0x0a0, 0x201, 0x132, 0x0b0, 0x200, 0x038, 0x0a8, 0x140, 0x0a7, 
     0x0e2, 0x0b5, 0x0a6, 0x071, 0x0af, 0x13f, 0x15b, 0x04e, 0x062, 0x057, 0x0b3, 0x0c4, 
     0x098, 0x09d, 0x163, 0x224, 0x0d4, 0x0ae, 0x0ca, 0x113, 0x0d2, 0x145, 0x055, 0x04d, 
     0x0b8, 0x147, 0x115, ),
    # 7 Trials: threat 27.1-39.5, 75 candidates
    (0x0bb, 0x059, 0x156, 0x04b, 0x20b, 0x047, 0x061, 0x0fe, 0x157, 0x0ac, 0x111, 0x258, 
     0x054, 0x159, 0x166, 0x0f2, 0x0cf, 0x097, 0x0b1, 0x0a0, 0x201, 0x132, 0x0b0, 0x200, 
     0x038, 0x0a8, 0x140, 0x0a7, 0x0e2, 0x0b5, 0x0a6, 0x071, 0x0af, 0x13f, 0x15b, 0x04e, 
     0x062, 0x057, 0x0b3, 0x0c4, 0x098, 0x09d, 0x163, 0x224, 0x0d4, 0x0ae, 0x0ca, 0x113, 
     0x0d2, 0x145, 0x055, 0x04d, 0x0b8, 0x147, 0x115, 0x0a1, 0x25b, 0x049, 0x133, 0x13d, 
     0x051, 0x218, 0x02c, 0x252, 0x0d8, 0x170, 0x0cb, 0x046, 0x15c, 0x0f1, 0x21b, 0x138, 
     0x125, 0x0be, 0x211, ),
    # 8 Lufenia: threat 34.8-64.4, 66 candidates
    (0x0a1, 0x25b, 0x133, 0x13d, 0x051, 0x218, 0x02c, 0x252, 0x0d8, 0x170, 0x0cb, 0x046, 
     0x0f1, 0x21b, 0x138, 0x0be, 0x211, 0x152, 0x03e, 0x050, 0x0aa, 0x149, 0x210, 0x15a, 
     0x148, 0x053, 0x0d7, 0x0d9, 0x253, 0x164, 0x146, 0x0d1, 0x068, 0x0ce, 0x223, 0x22d, 
     0x0bf, 0x225, 0x139, 0x16e, 0x203, 0x20c, 0x14e, 0x153, 0x130, 0x235, 0x0d0, 0x0c6, 
     0x0cd, 0x112, 0x127, 0x0d3, 0x16d, 0x13e, 0x22c, 0x22b, 0x259, 0x23d, 0x14d, 0x0e8, 
     0x15f, 0x15e, 0x126, 0x228, 0x128, 0x04a, ),
)
# Hand-picked per-tier entries, forced into EVERY zone of that tier so the region
# gets a signature fight the neighbouring tiers do not share:
#   Pravoka  0x0dd  4-6 Sahagin              (aquatic -- an explicit exception to
#                                             _LAND_AQUATIC; Pravoka is a port)
#   Elfheim  0x01a  2-4 Scorpion + 0 Minotaur (a pure Scorpion fight; Scorpion was
#                                             removed from DF pool B t3 to keep it
#                                             unique to this tier)
# They take part in the normal threat sort, so they land wherever their threat puts
# them in the rarity curve -- deliberately NOT pinned to a common slot (user call).
_OW_HANDPICK = {1: 0x0dd, 2: 0x01a}

# ---- Dungeon-flavored cave encounters ---------------------------------------
# Caves reroll each map's 8 slots from its OWN dungeon's vanilla formation pool,
# NOT a terrain-agnostic power band. The old scheme keyed every cave map to a
# _BATTLE_RANK[low..high] slice; because that rank ordering interleaves overworld,
# ocean and cave formations by raw power, an early cave would roll ocean Sharks
# and open-field Wolf/Tarantula packs that merely shared its tier (playtest bug:
# Marsh Cave spawning Cornelia/Elfheim-grass + ocean fights). Drawing from the
# dungeon's own vanilla formations keeps the flavor intact and -- because the
# land dungeons below never reference sea formations -- keeps the ocean out.
#
# Each cave map id -> the dungeon it belongs to (grouped by vanilla content, see
# re_only/encounter_census.py). Maps not listed have no random encounters and are
# left untouched.
# Map-id groups from the project's dungeon RE (see the curated-encounters notes /
# re_only/encounter_census.py). Empty entrance maps (e.g. 0x4d) are omitted so a
# reroll never adds encounters where vanilla has none; the water dungeon (Sunken
# Shrine + its stray sea floor 0x25) is isolated so its sea formations never bleed
# into a land cave and no land cave ever draws them.
_CAVE_DUNGEONS = {
    "chaos_f1":       (0x1f,),
    "marsh":          (0x59, 0x5a, 0x5b),
    "earth":          (0x01, 0x02, 0x03, 0x04, 0x05),
    "gurgu":          tuple(range(0x29, 0x2f)),
    "ice":            tuple(range(0x42, 0x47)),
    "trials":         (0x4e, 0x4f),                        # 0x4d = empty entrance
    "waterfall":      (0x53,),
    "mirage":         (0x50, 0x51, 0x52),
    "fortress":       tuple(range(0x5c, 0x61)),
    "chaos_basement": (0x20, 0x21, 0x22, 0x23, 0x24, 0x26, 0x27),  # 0x25 -> sunken
    "onrac":          (0x28,),                             # Sabertooth cave (side)
    "sunken":         (0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x25),  # water
}
# harder-mode target: which dungeon's pool a map rerolls from instead of its own.
# Follows the story/difficulty progression so "one tier up" reads as the NEXT real
# dungeon (Marsh -> Cavern of Earth -> Gurgu -> ...), not a random power-neighbor.
# Water (sunken) stays sea; the side cave (onrac) and terminal dungeon step to a
# sensible land successor / to themselves -- never across the land/sea line.
_CAVE_HARDER_DUNGEON = {
    "chaos_f1": "marsh", "marsh": "earth", "earth": "gurgu", "gurgu": "ice",
    "ice": "trials", "trials": "waterfall", "waterfall": "mirage",
    "mirage": "fortress", "fortress": "chaos_basement",
    "chaos_basement": "chaos_basement",
    "onrac": "gurgu", "sunken": "sunken",
}
_CAVE_MAP_DUNGEON = {mid: name
                     for name, mids in _CAVE_DUNGEONS.items() for mid in mids}


# ---- Curated boss encounters -------------------------------------------------
# "Fully curated" bosses: every boss formation is stripped from the random draw
# pool (so a shuffle can never spawn one in the wrong place), then hand-placed
# into a SINGLE (record, slot) -- a rare 1-in-8 cameo on that one sub-map/zone.
# Pool stripping happens on every reroll; placement is gated by the two harder
# toggles (see build_shuffle_tables).
#
# Pure-boss formation ids that live in _BATTLE_RANK (a range draw could roll
# them). Piscodemon (0x1c) is a normal enemy and deliberately stays in the pool.
_BOSS_POOL_EXCLUDE = frozenset({
    0x7f,                    # Garland
    0x7d,                    # Astos
    0x7c,                    # Vampire
    0x7a,                    # Lich
    0x79,                    # Marilith
    0x78,                    # Kraken
    0x77,                    # Tiamat
    0x73, 0x74, 0x75, 0x76,  # Chaos-Shrine alt fiend forms
    0x56,                    # WarMech (1-1). 0xd6 is "0-0 WarMech + Dark Fighters"
                             # = no actual boss, so it stays a poolable encounter.
    # 2026-08-01: these were never in _BATTLE_RANK, so nothing needed to exclude
    # them while draws were rank-index based. The threat-band overworld draw can
    # reach ANY formation, so they must be named explicitly now.
    0x7b,                    # 1 Chaos (threat 275 -- would end a Lufenia zone)
    0x4c,                    # 2-5 Guardian + 0 Soldier (Cornelia castle NPCs)
})

# Dungeon cameos (harder_dungeon): each boss's home dungeon stepped +3 down the
# difficulty-ordered chain, into one slot of one floor of the target dungeon.
# (cave map_id, slot) -> formation id. Tiamat is dropped (would overflow the
# chain past Chaos Shrine Basement).
_DUNGEON_BOSS_SLOTS = {
    (0x29, 7): 0x7f,   # Garland    -> Mount Gulg
    (0x42, 7): 0x1c,   # Piscodemon -> Cavern of Ice
    (0x42, 6): 0x7d,   # Astos      -> Cavern of Ice
    (0x4e, 7): 0x7c,   # Vampire    -> Citadel of Trials  (0x4d is an empty entrance map)
    (0x4e, 6): 0x7a,   # Lich       -> Citadel of Trials
    (0x17, 7): 0x79,   # Marilith   -> Sunken Shrine
    (0x5c, 6): 0x78,   # Kraken     -> Flying Fortress (slot 6; 7 is WarMech's)
    # WarMech (0x56 = 1-1 WarMech) in EVERY Flying Fortress floor (0x5c-0x60),
    # one slot each -> its iconic Sky-Fortress cameo, reachable on any floor.
    # 2026-08-03: no longer a flat slot 5. The slot climbs the rarity curve as
    # the party climbs the tower -- F1 slot 7 (1.56%) through F5 slot 3 (18.75%)
    # -- so WarMech stalks you harder the closer you get to the top. These bytes
    # mirror iso_patcher._FF_POOLS, which is authoritative while the per-floor
    # pools are installed; keep the two in sync.
    (0x5c, 7): 0x56,
    (0x5d, 6): 0x56,
    (0x5e, 5): 0x56,
    (0x5f, 4): 0x56,
    (0x60, 3): 0x56,
}

# Overworld cameos (harder_overworld): hand-placed by region.
# (overworld zone, slot) -> formation id.
_OVERWORLD_BOSS_SLOTS = {
    (52, 7): 0x7f,   # Garland  -> Elfheim / Elven Castle
    (12, 7): 0x7c,   # Vampire  -> Citadel of Trials region
    (12, 6): 0x7a,   # Lich     -> Citadel of Trials region
    (13, 7): 0x79,   # Marilith -> Lufenia / Mirage Desert
}
# The Onrac overworld region draws encounters from cave-table map 0x28 (the
# Sabertooth zone), so Astos/Piscodemon at Onrac physically live in zones_caves
# but are gated by the OVERWORLD toggle. (cave map_id, slot) -> formation id.
_ONRAC_BOSS_SLOTS = {
    (0x28, 7): 0x7d,   # Astos      -> Onrac overworld
    (0x28, 6): 0x1c,   # Piscodemon -> Onrac overworld
}


# Water formations on land. _BATTLE_RANK interleaves these into the LAND
# difficulty bands, so the overworld shuffle can otherwise place them on foot
# ("ocean Sahagin on land", user report 2026-07-09; "lone Shark on the grass near
# Crescent Lake", 2026-08-01). Two tiers of rule, per the user 2026-08-01:
#
#   _LAND_SHARK_BAN   -- a Shark / White Shark / Killer Shark can actually SPAWN.
#                        Never allowed on land anywhere.
#   _LAND_AQUATIC     -- every other water fight (Sahagin and Bigeyes families,
#                        Pirates, Sea Troll/Scorpion/Snake, Water Elemental/Naga,
#                        and the whole river set: Piranha/Croc/Ochu/Hydra/
#                        Squidraken). Allowed ONLY in the Crescent Lake, Onrac
#                        and Citadel of Trials regions (_AQUA_TIERS).
#
# Both sets are derived by a FULL-GROUP scan using SPAWNABLE counts -- a group
# whose max is 0 never appears, and the sea monster need not be the lead group.
# The old lead-monster scan is what let 0xdc ("0 Buccaneer + 1 Shark") through.
# test_patch.land_sea_exclude_checks() regenerates both from the ISO and fails on
# drift, so the gap cannot reopen. NOT touched: Sunken Shrine + the water dungeon
# (their own cave pool), DLC dungeons, and Regional Ocean -- none draw from these.
_AQUA_TIERS = frozenset({5, 6, 7})     # Crescent Lake + Onrac + Trials regions
_LAND_SHARK_BAN = frozenset({
    0x045, 0x048, 0x05a, 0x05d, 0x05e, 0x0c5, 0x0c8, 0x0da, 0x0dc, 0x0de, 0x11e, 0x11f, 
    0x124, 
})
_LAND_AQUATIC = frozenset({
    0x020, 0x024, 0x025, 0x041, 0x042, 0x043, 0x044, 0x047, 0x049, 0x052, 0x05b, 0x05c, 
    0x05f, 0x060, 0x061, 0x062, 0x065, 0x072, 0x07e, 0x0a0, 0x0a4, 0x0a5, 0x0c1, 0x0c2, 
    0x0c3, 0x0c4, 0x0c7, 0x0c9, 0x0d2, 0x0db, 0x0dd, 0x0df, 0x0e0, 0x0e1, 0x0e2, 0x0e5, 
    0x0f2, 0x0fe, 0x125, 0x12d, 0x12e, 0x12f, 0x154, 0x15c, 0x15d, 0x230, 0x231, 0x236, 
    0x237, 0x23c, 0x25a, 
})
# One deliberate exception: Pravoka is a PORT town, so it hand-picks an aquatic
# fight (see _OW_HANDPICK) that the restriction would otherwise bar.
_LAND_SEA_EXCLUDE = _LAND_SHARK_BAN    # back-compat alias (old seeds / callers)


# ------------------------------------------------------------ boss stat softening
# A boss met OUTSIDE its own boss room -- as a random-encounter CAMEO
# (_DUNGEON_BOSS_SLOTS) or as another boss's MINION (boss_minions) -- is the same
# monster_stats record as the scripted fight, so it inherits
# boss_difficulty_percentage in full. A 500% Kraken ambushing you out of a 1-in-8
# Flying Fortress slot, or a 500% Lich riding along with Tiamat, is a different
# proposition from the 500% Kraken you walked into at the Sunken Shrine altar. The
# client softens those appearances to boot_patch._cameo_mult(boss_difficulty) --
# the LOWER of CAMEO_BOSS_SCALE and CAMEO_BOSS_SCALE x boss_difficulty, so a
# high boss_difficulty never raises a guest above the flat scale -- by rewriting
# the stat record while the party stands somewhere the soft version applies
# (ApClient._cameo_boss_loop).
#
# The discriminator is the MAP, because nothing at battle time can tell a cameo or
# a minion apart from the real fight (same formation record, same monster record).
# Two gating styles, because two situations:
#
# EXCLUSION-gated (_BOSS_HOME_MAPS). A boss whose primary record is used in exactly
# three places -- its own fight, its cameos, its minion appearances -- is softened
# EVERYWHERE EXCEPT its home dungeon. That single rule covers cameos and minions at
# once and needs no list of where the soft appearances are. It is exact because the
# fiend REMATCH forms carry DIFFERENT monster ids (formation scan 2026-07-20: alt
# fids 0x73/0x74/0x75/0x76 -> monsters 0x78/0x7a/0x7c/0x7e, vs primaries
# 0x77/0x79/0x7b/0x7d), so the Chaos Shrine basement rematches are never touched by
# softening the primaries there for Chaos's minions. Self-minions (a pool that picks
# the host's own species, e.g. Garland's pool 2) fall out correctly for free: the
# host's fight IS its home map, so it stays full strength.
#
# INCLUSION-gated (_CAMEO_MAP_ONLY). A boss with no identifiable home fight can't
# use the rule above -- "everywhere except nowhere" would soften its real fight too:
#   Piscodemon 0x67 -- has no scripted fight at all and IS an ordinary random
#                    encounter, so "everywhere" would nerf normal Piscodemon fights.
#                    Cameo-map softening only (user decision 2026-07-20); as a
#                    Kraken/Echidna minion it stays at full boss_difficulty.
#   WarMech 0x76  -- no scripted fight; vanilla-rare Fortress encounter. Cameo only.
# (Astos 0x71 WAS here until his Western Keep map id was captured 2026-07-20 --
# see the _BOSS_HOME_MAPS entry below. Same route remains open for the others.)
#
# Lead monster (monster_stats record id) of each cameo formation, read from the
# vanilla formation table (ISO 0x2b24d68, 15B stride, +3 = first type slot) 2026-07-19.
# Every id here is already in boot_patch._boss_stat_ids(), i.e. already boss-scaled.
_CAMEO_FID_MONSTER = {
    0x7f: 0x69,   # Garland
    0x7d: 0x71,   # Astos
    0x7c: 0x3c,   # Vampire
    0x7a: 0x77,   # Lich
    0x79: 0x79,   # Marilith
    0x78: 0x7b,   # Kraken
    0x56: 0x76,   # WarMech
    0x1c: 0x67,   # Piscodemon
    0x77: 0x7d,   # Tiamat            (Chaos Shrine basement cameos, below)
    0x100: 0x80,  # Echidna
    0x101: 0x81,  # Cerberus
    0x102: 0x82,  # Ahriman
    0x103: 0x83,  # Two-Headed Dragon
}
# Chaos Shrine basement cameos. Unlike the other cameo tables these are NOT
# stamped into zones_caves -- the pools live in a u16 detour cave
# (iso_patcher._CF_POOLS) because four of them are DLC formations (>= 0x100) that
# a u8 table cannot hold. Listed here purely so boss_soft_plan() knows a soft
# appearance exists; test_rando asserts the two stay in sync.
# (cave map id, slot) -> formation id.
_CHAOS_BOSS_SLOTS = {
    (0x20, 7): 0x056,   # WarMech            -> B1
    (0x21, 7): 0x101,   # Cerberus           -> B2
    (0x22, 7): 0x103,   # Two-Headed Dragon  -> B3
    (0x23, 6): 0x100,   # Echidna            -> Earth floor
    (0x23, 7): 0x077,   # Tiamat             -> Earth floor
    (0x24, 7): 0x07a,   # Lich               -> Fire floor
    (0x25, 7): 0x079,   # Marilith           -> Water floor
    (0x26, 6): 0x102,   # Ahriman            -> Air floor
    (0x26, 7): 0x078,   # Kraken             -> Air floor
}
# monster id -> the dungeon (key of _CAVE_DUNGEONS) whose maps are that boss's own
# fight. On those maps it keeps FULL boss_difficulty; everywhere else it is soft.
# Only primaries appear here -- rematch forms have their own ids and are never
# softened, so the Chaos Shrine basement refights stay at full strength.
_BOSS_HOME_DUNGEON = {
    0x69: "chaos_f1",   # Garland   -> Chaos Shrine F1
    0x3c: "earth",      # Vampire   -> Cavern of Earth
    0x77: "earth",      # Lich      -> Cavern of Earth
    0x79: "gurgu",      # Marilith  -> Mount Gulg
    0x7b: "sunken",     # Kraken    -> Sunken Shrine
    0x7d: "fortress",   # Tiamat    -> Flying Fortress
}
_BOSS_HOME_MAPS = {mon: frozenset(_CAVE_DUNGEONS[dn])
                   for mon, dn in _BOSS_HOME_DUNGEON.items()}
# NO boss's scripted fight is in the Chaos Shrine basement: the four fiends down
# there are REMATCH forms with their own monster ids (0x78/0x7a/0x7c/0x7e), which
# are never softened at all. So no basement map may count as a home -- otherwise a
# cameo standing on it would fight at FULL strength.
#
# This is not hypothetical. Map 0x25 is the basement's WATER floor, but it is
# filed under "sunken" in _CAVE_DUNGEONS (deliberately -- its sea formations
# belong in the Sunken Shrine's reroll pool, and moving it out would strip the
# five hardest water fights from that pool). That grouping made 0x25 read as
# KRAKEN'S HOME. Subtracting the basement here fixes the home set without
# touching the pool set -- the two concerns just needed separating.
_CHAOS_BASEMENT_MAPS = frozenset(range(0x20, 0x28))
_BOSS_HOME_MAPS = {mon: maps - _CHAOS_BASEMENT_MAPS
                   for mon, maps in _BOSS_HOME_MAPS.items()}
# Astos's home is the Western Keep, which has no random encounters and so no
# _CAVE_DUNGEONS entry -- but the canonical map id is still populated there:
# 0x58, captured live 2026-07-20 standing in the throne room (re_only/
# mapid_watch.py). With it, Astos graduates from _CAMEO_MAP_ONLY to the
# home-relative rule: full strength for the real fight, soft as a cameo AND as
# a minion (Lich's and Gilgamesh's pools pick him).
_BOSS_HOME_MAPS[0x71] = frozenset({0x58})

# DLC / bonus-dungeon bosses (monster 0x80-0x90). Their scripted fights live in
# the four bonus dungeons, whose floors draw mapids from fixed contiguous BANDS
# (client ff1_data.BONUS_MAPID_BANDS, live-dumped 2026-07-21) -- so a whole
# dungeon's band IS the home map set, and the home-relative rule works for them
# unchanged. Duplicated here as literals rather than imported: rando is
# apworld-side (gen) and ff1_data is client-side; test_boss_minions asserts the
# two stay in sync.
#   band 0  Earthgift Shrine   0x87-0x8b
#   band 1  Hellfire Chasm     0x8c-0x95
#   band 2  Lifespring Grotto  0x96-0xa9
#   band 3  Whisperwind Cove   0xaa-0xd1
# Rosters confirmed by the user 2026-07-22. Scarmiglione owns TWO monster
# records (0x84 undead / 0x85 true form) and BOTH are full bosses of Hellfire.
_BONUS_BANDS = ((0x87, 0x8b), (0x8c, 0x95), (0x96, 0xa9), (0xaa, 0xd1))
_DLC_BOSS_HOME_BAND = {
    0x80: 0, 0x81: 0, 0x82: 0, 0x83: 0,   # Echidna, Cerberus, Ahriman, 2-Head Dragon
    0x84: 1, 0x85: 1, 0x86: 1, 0x87: 1, 0x88: 1,  # Scarmiglione x2, Cagnazzo,
                                                  # Barbariccia, Rubicante
    0x89: 2, 0x8a: 2, 0x8b: 2, 0x8c: 2,   # Gilgamesh, Omega, Shinryu, Atomos
    0x8d: 3, 0x8e: 3, 0x8f: 3, 0x90: 3,   # Typhon, Orthros, Phantom Train, Death Gaze
}
for _mon, _bd in _DLC_BOSS_HOME_BAND.items():
    _lo, _hi = _BONUS_BANDS[_bd]
    _BOSS_HOME_MAPS[_mon] = frozenset(range(_lo, _hi + 1))
del _mon, _bd, _lo, _hi
# A Whisperwind GIMMICK floor reports a live mapid below 0x87 (town/field rows),
# so a DLC boss can read as "away from home" while standing on one. That is the
# safe direction: it softens a guest appearance that isn't there. It can never
# soften a real fight, because no boss room is a gimmick floor.

# Softened on cameo maps only -- no usable home fight (see the note above).
#
# WarMech 0x76 was here until 2026-08-03 and is NOT softened as a cameo any more
# (user call): it has boss-tier stats but it is not a boss -- no boss room, no
# scripted fight, just a rare random encounter. Because it has no home dungeon,
# "cameo softening" meant EVERY WarMech in the game fought at 50%, and there was
# no way to meet a full-strength one at all. It is also out of
# boot_patch._boss_stat_ids() now, so boss_difficulty never touches it either.
_CAMEO_MAP_ONLY = frozenset({0x67})
# Monsters that must NEVER be softened and never boss-scaled, however boss-like
# their stat record looks. WarMech is a plain random encounter: no boss room, no
# scripted fight, and it is in no minion pool either (boss_minions.BOSS_POOL_SETS
# -- verified 2026-08-03, 133 pool monsters, 0x76 not among them), so there is no
# appearance of it anywhere that is a "guest" rather than simply WarMech.
_NEVER_SOFT = frozenset({0x76})
# Key of the overworld entry in boss_soft_plan()'s map dict (no cave map id there).
CAMEO_OVERWORLD = "overworld"


def boss_soft_plan(dungeon_harder=False, overworld_harder=False, minion_plan=None):
    """Which bosses get the softened stat record, and where.

    Returns (everywhere_but_home, home_maps, map_soft):
      everywhere_but_home -- frozenset of monster ids soft on EVERY map except the
                             matching home_maps entry (covers cameos AND minions)
      home_maps           -- {monster id: frozenset(cave map ids)} for those ids
      map_soft            -- {cave map id or CAMEO_OVERWORLD: frozenset(ids)} for
                             the inclusion-gated leftovers (_CAMEO_MAP_ONLY)

    Mirrors the SAME inputs that create the soft appearances -- the two harder
    toggles place cameos, minion_plan (slot data boss_minions_plan) places minions --
    so with cameos off and minions off this is empty and the feature is a strict
    no-op. A boss is only listed if something actually puts it somewhere soft.

    The Onrac cameos are listed under BOTH cave map 0x28 and the overworld: they are
    stamped into cave row 0x28 but the party is physically on overworld tiles when
    they fire, and which id the game reports there hasn't been live-captured --
    covering both costs nothing (no scripted fight lives on either)."""
    everywhere = set()
    map_soft = {}

    def add(key, mon):
        if mon in _BOSS_HOME_MAPS:
            everywhere.add(mon)         # home-relative rule subsumes the map entry
        elif mon in _CAMEO_MAP_ONLY:
            map_soft.setdefault(key, set()).add(mon)

    if dungeon_harder:
        for (rec, _slot), fid in _DUNGEON_BOSS_SLOTS.items():
            add(rec, _CAMEO_FID_MONSTER[fid])
        # Chaos Shrine basement (u16 detour pools, same toggle).
        for (rec, _slot), fid in _CHAOS_BOSS_SLOTS.items():
            add(rec, _CAMEO_FID_MONSTER[fid])
    if overworld_harder:
        for (_zone, _slot), fid in _OVERWORLD_BOSS_SLOTS.items():
            add(CAMEO_OVERWORLD, _CAMEO_FID_MONSTER[fid])
        for (rec, _slot), fid in _ONRAC_BOSS_SLOTS.items():
            mon = _CAMEO_FID_MONSTER[fid]
            add(rec, mon)
            add(CAMEO_OVERWORLD, mon)
    # Minions: a rolled add that IS a boss. Skip a pool that picked the host's own
    # species (Garland/Vampire/Astos pools do) -- softening it would soften the boss
    # itself, and no map can separate a boss from its clone.
    from .boss_minions import BOSS_MON
    for entry in (minion_plan or []):
        fid, groups = entry[0], entry[1]
        host = BOSS_MON.get(fid)
        for grp in groups:
            mon = grp[0]
            if mon != host and mon in _BOSS_HOME_MAPS:
                everywhere.add(mon)
    return (frozenset(everywhere),
            {m: _BOSS_HOME_MAPS[m] for m in everywhere},
            {k: frozenset(v) for k, v in map_soft.items()})


def boss_soft_ids(plan, map_key):
    """The monster ids to soften while standing on `map_key` (a cave map id or
    CAMEO_OVERWORLD), given a boss_soft_plan() result. Pure -- the client calls this
    every time the map changes."""
    everywhere, home_maps, map_soft = plan
    out = {m for m in everywhere if map_key not in home_maps[m]}
    out |= set(map_soft.get(map_key, ()))
    return frozenset(out)


def _draw_formation(rng, low, high):
    """Draw a non-boss formation id from _BATTLE_RANK[low..high]. Bosses are
    curated (see _BOSS_POOL_EXCLUDE) so they never enter a random pool: reject
    and redraw, capped so a boss-dense range can't loop forever."""
    fid = _BATTLE_RANK[rng.randrange(high - low + 1) + low]
    for _ in range(16):
        if fid not in _BOSS_POOL_EXCLUDE:
            break
        fid = _BATTLE_RANK[rng.randrange(high - low + 1) + low]
    return fid


def strip_land_sea(block, vanilla, hi=None):
    """Revert any land-zone slot the shuffle filled with a water fight that does
    not belong there, back to that slot's VANILLA land formation.

    Two rules (see _LAND_SHARK_BAN / _LAND_AQUATIC): sharks are stripped from
    every zone, other aquatic fights only from zones outside the Crescent Lake
    and Onrac regions. RNG-NEUTRAL (a pure post-process, no draws), so it never
    shifts the rest of the shuffle stream or item placement. Applied BOTH at
    generation and client-side at bake (patches_from_slot_data), so seeds made
    before a rule change are corrected on the next client relaunch -- no
    regeneration needed. zones_overworld is entirely terrain-0 LAND, so stripping
    across the whole table can never remove a legitimate sea encounter."""
    out = bytearray(block)
    for i, fid in enumerate(out):
        # `hi` is the companion high-byte table. A slot with a non-zero high byte
        # holds a u16 DLC id whose LOW byte is meaningless here -- comparing it
        # against these u8 sets would strip innocent fights (0x15e Red Flan has
        # low byte 0x5e, a banned Shark id). Skip those; the harder-mode pools
        # already exclude water fights by construction.
        if hi is not None and hi[i]:
            continue
        tier = ZONE_TIER[i // 8]
        if fid in _LAND_SHARK_BAN:
            out[i] = vanilla[i]
        elif (fid in _LAND_AQUATIC and tier not in _AQUA_TIERS
                and _OW_HANDPICK.get(tier) != fid):
            # a tier's hand-pick is an explicit exception to the aquatic rule
            # (Pravoka's 4-6 Sahagin -- it is a port town). Without this the strip
            # deletes the very fight the shuffle was told to force in.
            out[i] = vanilla[i]
    return bytes(out)


def _stamp_boss_slots(block, slots):
    """Overwrite fixed (record, slot) bytes of a zone block with curated boss
    formation ids (rare single-slot cameos). Returns a new bytearray."""
    out = bytearray(block)
    for (rec, slot), fid in slots.items():
        out[rec * 8 + slot] = fid
    return out


def _rank_slots(picks):
    """Order 8 picks (given ascending by difficulty) onto the engine's slot
    rarity curve -- slots 0-3 are 18.75% each, 4-5 are 9.38%, slot 6 is 4.69% and
    slot 7 is 1.56% (scramble 0x08945850, which the overworld table DOES go
    through, unlike the forest/ocean caves). Rule, shared with dangerous_forests
    and regional_ocean: the HARDEST fight is the rarest (slot 7), the EASIEST is
    second-rarest (slot 6), and the middle six run easiest -> hardest across
    slots 0..5, i.e. most common -> rarest."""
    return list(picks[1:-1]) + [picks[0], picks[-1]]


def shuffle_zones_overworld(rng, block, harder=False):
    """Roll every zone's 8 slots and return (low, high) 512-byte tables.

    Normal mode is unchanged: a cumulative _TIER_BAND index slice of
    _BATTLE_RANK, in random slot order, u8 only (high table stays all zero).

    Harder mode (v192) draws from the zone tier's precomputed THREAT pool
    (_OW_HARDER_POOL, ascending by threat), forces in that tier's _OW_HANDPICK if
    it has one, and orders the eight onto the rarity curve via _rank_slots. Pools
    may contain u16 DLC formation ids, so the id is split across the low table and
    the companion high-byte table (see iso_patcher.apply_overworld_u16).

    Both modes finish with strip_land_sea (RNG-neutral) so water fights cannot
    stand on dry land."""
    out = bytearray(block)
    hi = bytearray(512)
    for zi, tier in enumerate(ZONE_TIER):
        if tier == TIER_NONE:
            out[zi * 8:zi * 8 + 8] = bytes(8)          # formation 0 = no encounter
            continue
        if not harder:
            low, high = _TIER_BAND[tier]
            for j in range(8):
                out[zi * 8 + j] = _draw_formation(rng, low, high)
            continue
        pool = _OW_HARDER_POOL[tier]
        forced = _OW_HANDPICK.get(tier)
        n = 8 - (1 if forced is not None else 0)
        picks = rng.sample(list(pool), min(n, len(pool)))
        if forced is not None and forced not in picks:
            picks.append(forced)
        # the pool literal is sorted ascending by threat, so pool-index order IS
        # difficulty order -- no threat values needed at generation time.
        picks.sort(key=pool.index)
        for j, fid in enumerate(_rank_slots(picks)):
            out[zi * 8 + j] = fid & 0xFF
            hi[zi * 8 + j] = fid >> 8
    return strip_land_sea(bytes(out), block, hi), bytes(hi)


def _cave_dungeon_pool(block, name):
    """Sorted distinct non-zero formation ids the dungeon uses in vanilla -- the
    flavor pool a shuffle rerolls its maps from."""
    pool = set()
    for mid in _CAVE_DUNGEONS[name]:
        pool.update(block[mid * 8: mid * 8 + 8])
    pool.discard(0)
    return sorted(pool)


def _draw_pool(rng, pool):
    """Pick a formation id from `pool`, skipping curated bosses (see
    _BOSS_POOL_EXCLUDE). Falls back to the raw pool if every entry is a boss, so
    the draw stays deterministic and non-empty."""
    ok = [f for f in pool if f not in _BOSS_POOL_EXCLUDE] or pool
    return ok[rng.randrange(len(ok))]


def shuffle_zones_caves(rng, block, harder=False):
    """Reroll each dungeon map's 8 slots from its own dungeon's vanilla formation
    pool (harder: from the next dungeon up the progression chain instead). Maps
    with no dungeon assignment keep their vanilla bytes. See _CAVE_DUNGEONS."""
    out = bytearray(block)
    pools = {name: _cave_dungeon_pool(block, name) for name in _CAVE_DUNGEONS}
    for mid in sorted(_CAVE_MAP_DUNGEON):
        name = _CAVE_MAP_DUNGEON[mid]
        src = _CAVE_HARDER_DUNGEON[name] if harder else name
        pool = pools[src] or pools[name]
        for j in range(8):
            out[mid * 8 + j] = _draw_pool(rng, pool)
    return out


# ---------------------------------------------------------------- #1 shop items
_SHOP_BASE = 0x2b1a1dc          # primary store region (== rando_data shops base)
_SHOP_BASE2 = 0x2b1a4c8         # secondary (2nd-language) store region
_CITY_STARTS = [0, 28, 60, 100, 124, 156, 172, 204, 216]
# rows: weapons, armor, items, white1, white2, black1, black2
# City index 8 = the Onrac Caravan (store type 4). Its item row is -1 here so the
# GENERIC weapon/armor/item shuffle and _vanilla_shop_ids both skip it: the caravan
# is filled by a dedicated pass (_fill_caravan) from a stepped-up rarity split, and its
# vanilla tonics must NOT be counted as OVERWORLD town-shop ids (they are EXOTIC).
# See caravan-shop-pool.
_STORES = [
    [1, 1, 1, 1, 1, -1, 1, -1, -1],
    [9, 9, 9, 9, 9, -1, 5, -1, -1],
    [13, 17, 17, -1, 17, 1, 9, -1, -1],
    [20, 24, 24, 16, 24, 8, 16, 0, -1],
    [-1, -1, 28, -1, -1, -1, 20, -1, -1],
    [24, 28, 32, 20, 28, 12, 24, 4, -1],
    [-1, -1, 36, -1, -1, -1, 28, -1, -1],
]
_STORE_SIZES = [
    [5, 4, 4, 4, 4, -1, 1, -1, -1],
    [3, 5, 5, 5, 5, -1, 2, -1, -1],
    [7, 7, 7, -1, 7, 7, 7, -1, -1],
    [4, 4, 4, 4, 4, 2, 2, 1, -1],
    [-1, -1, 4, -1, -1, -1, 3, -1, -1],
    [4, 4, 4, 4, 4, 2, 2, 1, -1],
    [-1, -1, 4, -1, -1, -1, 3, -1, -1],
]
# Onrac Caravan (city 8) block-relative id-list start (def[39], the revert/tonic
# store) = _CITY_STARTS[8]; the secondary-language region copy sits _SEC_DELTA later.
_CARAVAN_LIST_OFF = _CITY_STARTS[8]     # 0xD8
_CARAVAN_VANILLA_WIDTH = 5              # vanilla def[39] slot count
_MIN_NUM = [1, 1, 1, 1, 1, 0x21, 0x21]
_MAX_NUM = [0x43, 0x4b, 0x2b, 0x20, 0x20, 0x40, 0x40]
# Draw pool for the forced "cheap consumable" slots at the front of the early
# item stores. Widened 2026-08-07 from 10 ids to 14: the item stores now run a
# one-per-id GLOBAL cap (see _ITEM_ID_CAP), and 12 forced common slots against a
# 10-id pool made that cap unreachable by arithmetic alone. The 12 ids that are
# vanilla town item stock -- {1,2,4,9,10,11,12,13,14,16,17,18}, i.e. every id
# _vanilla_shop_ids finds in row 2 -- are all OVERWORLD-tier, so the cap fits
# exactly at the lowest shuffled tier; Turbo Ether (5) and Emergency Exit (15)
# are EXOTIC (not vanilla town stock) and only widen the pool further up.
_COMMON_ITEMS = [0x01, 0x02, 0x04, 0x05, 0x09, 0x0a, 0x0b, 0x0c,
                 0x0d, 0x0e, 0x0f, 0x10, 0x11, 0x12]
# Remedy (0x0a) moved out 2026-08-07: it is vanilla town stock, so it now lives
# in _COMMON_ITEMS. Being in BOTH pools gave it double draw weight and made it
# the single most-repeated consumable at the low tiers.
_RARE_ITEMS = [0x03, 0x06, 0x07, 0x08, 0x1f, 0x20, 0x21, 0x22, 0x23,
               0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b]

# --- priceless-item handling (SHOP_TIER_ALL; see options.ShopItemPool) -------
# Weapons with vanilla buy price 0 (drop/treasure-only ultimates), 1-based id.
# These need a SYNTHETIC price when priceless items are allowed (vanilla = 0).
# (Murasame 43 is 0-gil too but is NOT priceless -- it gates at ACTIVATABLE, so
# its synthetic price lives in _ALWAYS_PRICED_WEAPONS instead.)
_PRICELESS_WEAPON_ZERO_PRICE_IDS = frozenset({41, 42, 44, 45, 46})  # Ultima Weapon .. Judgment Staff
_PRICELESS_WEAPON_PRICE = 99999
# Full priceless-gated weapon set: the zero-price ultimates above PLUS named
# top-tier gear that already has a real vanilla price (60000+) -- these are
# blocked from shop shuffle unless allowed, but keep their own vanilla price
# (never forced to _PRICELESS_WEAPON_PRICE) since they aren't actually 0-gil.
_PRICELESS_WEAPON_IDS = _PRICELESS_WEAPON_ZERO_PRICE_IDS | frozenset({
    39,   # Excalibur
    40,   # Masamune
    50,   # Deathbringer
    54,   # Rune Axe
    64,   # Sage's Staff (vanilla buy=39900, top-tier caster staff)
    65,   # Barbarian's Sword
    66,   # Lust Dagger
    67,   # Golden Staff
})
# Armor gated the same way -- the 5 ultimates already sit at the 99900 display
# cap and keep their vanilla price; Ribbon carries a 2-gil placeholder price in
# vanilla (treasure-only), so it gets a synthetic base like the 0-gil weapons
# (see _PRICELESS_ARMOR_PRICES / apply_priceless_base_prices).
_PRICELESS_ARMOR_IDS = frozenset({
    28,   # Maximillian
    29,   # Survival Vest
    30,   # Lordly Robes
    42,   # Hero's Shield (vanilla buy=62000, treasure-exclusive endgame shield)
    45,   # Master Shield
    52,   # Ribbon (vanilla buy=2 placeholder -> synthetic 80000)
    62,   # Shadow Mask
})
# Priceless armor whose vanilla price is a placeholder, 1-based id -> real base.
_PRICELESS_ARMOR_PRICES = {
    52: 80000,                                 # Ribbon
}

# --- exotic/priceless loot pool: promoted-only equip restriction -------------
# Priceless AP-gear gids whose equip perms are locked to the six PROMOTED
# (class-changed) jobs -- Knight/Ninja/Master/Red-White-Black Wizard. In the
# 28-byte record's +2/+3 job masks, +2 (equip1) = "base job (and its promotion)
# may equip" and the +3 "extra" bits (equip2 & ~equip1) = "promoted form ONLY".
# So moving every equip1 bit into extra (new e1=0, e2 = e1|e2) makes the item
# unequippable pre-promotion. See shuffle_who_equips_what for mask semantics and
# apply_promoted_only_equip below. gids are 1-based cat-2 (weapon) / cat-3 (armor).
# NOT a separate hand-list: "priceless AP-gear" IS the priceless shop tier, so these
# alias the tier sets above -- one place to edit when an item changes category (the
# apworld's PRICELESS_GEAR loot pool is derived from the same tier data).
_PROMOTED_ONLY_WEAPON_GIDS = _PRICELESS_WEAPON_IDS
_PROMOTED_ONLY_ARMOR_GIDS = _PRICELESS_ARMOR_IDS


def apply_promoted_only_equip(weapons, armor):
    """Restrict every priceless AP-gear item to promoted-job equip only, in place.
    Idempotent (already-restricted records have e1=0, so e1|e2 is unchanged).
    Run AFTER shuffle_who_equips_what so it overrides a randomized equip roll."""
    changed = False
    for blk, gids in ((weapons, _PROMOTED_ONLY_WEAPON_GIDS),
                      (armor, _PROMOTED_ONLY_ARMOR_GIDS)):
        for gid in gids:
            rec = (gid - 1) * 28
            e1, e2 = blk[rec + 2], blk[rec + 3]
            new_e2 = (e1 | e2) & 0x3F
            if e1 != 0 or e2 != new_e2:
                blk[rec + 2], blk[rec + 3] = 0, new_e2
                changed = True
    return changed
# Consumables that get a real base price when priceless items are allowed in
# shops (1-based item id -> gil). Mostly 0-gil in vanilla, so the base is
# synthetic. 2026-08-05 (user): the two ethers swapped tiers -- Dry Ether
# restores the FULL mana pool, so it is the priceless one; Turbo Ether restores
# a lot but not all, and gates at ACTIVATABLE via _ALWAYS_PRICED_ITEMS.
_PRICELESS_ITEM_PRICES = {
    3: 30000,                                  # X-Potion
    6: 45000,                                  # Dry Ether (full MP restore)
    8: 70000,                                  # Megalixir
    36: 80000,                                 # Golden Apple
    37: 50000,                                 # Silver Apple
    38: 50000,                                 # Soma Drop
    39: 80000, 40: 80000, 41: 80000, 42: 80000, 43: 80000,   # *Plus tonics
}
# Fangs & co: unlike _PRICELESS_ITEM_PRICES, these are NOT priceless-gated --
# they always get a real price and are shop-eligible from the ACTIVATABLE tier
# up (battle-use / spell-effect items, so they gate alongside the spell-on-use
# gear rather than in the plain exotic pool; see _shop_id_min_tier).
# (Curtains need no such entry: they already carry a nonzero vanilla buy price,
# so their min-tier is EXOTIC via the normal path -- except Light Curtain, see
# _ACTIVATABLE_TIER_ITEMS).
_ALWAYS_PRICED_ITEMS = {
    20: 1000, 21: 1000, 22: 1000, 29: 1000,    # White/Red/Blue/Vampire Fang
    30: 4000,                                  # Cockatrice Claw (0-gil vanilla)
    5: 12000,                                  # Turbo Ether (big MP restore --
                                               # vanilla 500 gil is far too cheap
                                               # for activatable-tier stock)
}
# Weapons with a 0-gil vanilla price that are NOT priceless-gated: they become
# shop-eligible at their own tier (Murasame casts a spell on use -> ACTIVATABLE),
# so they need a real base price unconditionally, exactly like the Fangs above --
# apply_priceless_base_prices only runs at SHOP_TIER_ALL and would leave them free.
# 1-based weapon id -> gil (the activatable x1.5 multiplier applies on top).
_ALWAYS_PRICED_WEAPONS = {
    43: 30000,                                 # Murasame
}
# Consumables that already carry a real vanilla price (so they need no entry in
# _ALWAYS_PRICED_ITEMS) but should still gate at ACTIVATABLE rather than EXOTIC,
# because they are battle-use spell-effect items like the Fangs above.
_ACTIVATABLE_TIER_ITEMS = frozenset({
    23,   # Light Curtain
})
# 0-gil vanilla consumables priced unconditionally like _ALWAYS_PRICED_ITEMS but
# with NO tier override: non-vanilla-shop ids, so they gate at EXOTIC via the
# normal _shop_id_min_tier path.
_EXOTIC_PRICED_ITEMS = {
    15: 500,                                   # Emergency Exit
    19: 1500,                                  # Spider's Silk
}
# Consumables banned from shops even when priceless items are allowed -- no
# replacement price, so they'd otherwise sell for 0 gil; permanently excluded.
_ALWAYS_ITEM_BLOCK = frozenset({
    7,    # Elixir
})
# Consumables whose entire effect is the mana pool. slot_magic makes that pool
# inert, so they are pulled from every shop/caravan draw for such a seed (user
# 2026-07-31); the Soma Drop is NOT here -- slot_magic repurposes it into a
# spell-slot raise (see iso_patcher _SM_SOMA_*). Threaded as a parameter rather
# than a module flag: one generation process hosts many worlds, and only the
# slot_magic ones may block it.
_MANA_ITEM_BLOCK = frozenset({
    32,   # Faerie Tonic (full MP restore)
})
# --- shop item pool tiers (see options.ShopItemPool) ------------------------
# One gradient controls how deep the shop-shuffle draw pool goes. Each id has a
# minimum tier at which it becomes shop-eligible; a higher tier is a strict
# superset of every lower one. Priceless gear/consumables sit ABOVE activatable
# and exotic (a priceless-AND-activatable weapon like Ragnarok/Judgment Staff is
# gated to TIER_ALL, not TIER_ACTIVATABLE -- priceless always wins).
SHOP_TIER_UNSHUFFLED = 0     # shops not shuffled at all
SHOP_TIER_MUNDANE = 1        # vanilla town-shop stock + _MUNDANE_*_IDS treasure gear
SHOP_TIER_OVERWORLD = SHOP_TIER_MUNDANE   # pre-2026-07-31 name (option "overworld")
# MUNDANE overrides (2026-07-31): gear that is treasure-only in vanilla (so the
# _vanilla_shop_ids scan would rate it EXOTIC) but is ordinary elemental/mid-tier
# equipment nobody should have to unlock a shop tier for. Forced to OVERWORLD:
# buyable from tier 1, and -- since the loot pools derive from this gradient --
# never an exotic/priceless LOOT candidate either. All carry a real vanilla buy
# price (1600..60000), so no synthetic pricing is needed.
# (Light Axe 29 is deliberately NOT here: spell-on-use -> stays ACTIVATABLE.)
_MUNDANE_WEAPON_IDS = frozenset({
    14, 20, 21, 22,   # Great Axe / Flame Sword / Ice Brand / Wyrmkiller
    23, 25, 26, 27,   # Great Sword / Coral Sword / Werebuster / Rune Blade
    28, 34,           # Power Staff / Vorpal Sword
})
_MUNDANE_ARMOR_IDS = frozenset({
    7, 8, 9,          # Flame Mail / Ice Armor / Diamond Armor
    34, 35, 36, 37,   # Flame Shield / Ice Shield / Diamond Shield / Aegis Shield
    39, 50, 69,       # Protect Cloak / Diamond Helm / Diamond Gloves
})
SHOP_TIER_EXOTIC = 2         # + non-activatable exotic (DLC) weapons/armor/items
SHOP_TIER_ACTIVATABLE = 3    # + exotic weapons that cast a spell on use
SHOP_TIER_ALL = 4            # + priceless gear/consumables (ultimates, tonics, ...)

# Weapons that cast a free spell effect when USED as a battle item: weapon record
# field +7 (the use-cast spell id) is nonzero. Derived from the vanilla table so
# it stays correct if the data changes -- no hand-maintained id list. These are
# all in the exotic range (none appear in vanilla overworld shops).
_ACTIVATABLE_WEAPON_IDS = frozenset(
    i for i in range(1, 68) if RD.VANILLA["weapons"][(i - 1) * 28 + 7] != 0)

# Armor uses the SAME 28-byte record shape as weapons, and its +7 field is likewise
# a use-cast spell id: some helms/robes/gloves cast a free spell when USED as a battle
# item (White Robe, Black Robe, Healing Helm, Gauntlets, Giant's Gloves). Same late-game
# gate as activatable weapons -- kept in a separate set so callers can distinguish the
# game category (0 weapon / 1 armor) when indexing shop records.
_ACTIVATABLE_ARMOR_IDS = frozenset(
    i for i in range(1, 76) if RD.VANILLA["armor"][(i - 1) * 28 + 7] != 0)

# Post-randomization buy-price multipliers that push powerful stock toward the
# gil ceiling (applied AFTER every price shuffle, see apply_power_price_multipliers).
# Priceless wins over activatable, so each id is scaled exactly once.
_ACTIVATABLE_PRICE_MULT = 1.5
_PRICELESS_PRICE_MULT = 2.0

_ITEM_ID_MAX = 0x2b                    # consumable (cat 2) id range is 1..0x2b

# ---------------------------------------------------------------- shop widths
# Every weapon/armor/item store and the Caravan gets rng.randint(0, extra_max)
# EXTRA slots on top of its base width, clamped to _MAX_SHOP_SLOTS. `extra_max`
# is the yaml `shop_max_extra_items` (0..._EXTRA_SLOTS_MAX); it was hardcoded at
# 5 before 2026-08-14. Live-verified
# in Cornelia 2026-08-07 (re_only/shop_width_live.py) -- see shop-width-relocation:
#
#   * 15 is a HARD ceiling: the slot count is the LOW NIBBLE of the def code word.
#   * The shop UI viewport is 5 rows and scrolls; 15 rows drew names, prices and
#     descriptions correctly.
#   * The ids the game draws come from the PRIMARY def's list pointer, but the ROW
#     COUNT comes from the SECONDARY def table (primary=10 + secondary=4 drew FOUR
#     rows; both=10 drew ten). Possibly min() of the two -- untested, same rule
#     either way: WRITE THE SIZE NIBBLE TO BOTH DEF TABLES.
#   * The def list pointer is an absolute RAM address, so lists relocate freely.
#     They must: item stores have ZERO in-place headroom (the 4->7 widen ate it)
#     and weapons/armor have 0-3 bytes.
#
# Both def tables are pointed at ONE shared list, which also retires the old
# "independent second draw per region" (the two regions shipped DIFFERENT stock
# for years -- invisible, because region 2's ids are never read).
_ID_CAP = 1                            # max slots one id may occupy world-wide
_ID_CAP_STEPS = 8                      # how far draw() may escalate that cap
_MAX_SHOP_SLOTS = 15                   # def code low nibble
_EXTRA_SLOTS_MAX = 6                   # ceiling on the yaml value (ShopMaxExtraItems)
_EXTRA_SLOTS_DEFAULT = 2               # ShopMaxExtraItems.default, for direct callers
# A store this narrow in vanilla gets a GUARANTEED extra slot (user 2026-08-07):
# only Gaia's weapon shop qualifies, and rolling 0 there left it selling a single
# weapon -- the case that prompted the whole feature. The guarantee is itself
# clamped to extra_max, so shop_max_extra_items=0 is a true off switch.
_TINY_STORE_WIDTH = 1
_EXTRA_SLOTS_MIN_TINY = 1
# (RETIRED 2026-08-14: _ITEM_BASE_WIDTH = 7, the flat "town item stores always
# widen from vanilla 4/5 to 7" bump. Item stores now start at their VANILLA def
# width like every other store and take the same shop_max_extra_items roll, so
# one yaml option governs every shelf. _STORE_SIZES[2] still reads 7 -- it is the
# slot-enumeration extent used by the price/AP scans, not a width plan.)
_SEC_DELTA = _SHOP_BASE2 - _SHOP_BASE  # 0x2ec: primary -> secondary region
_BLOCK_LEN = 0x600                     # the shops block is 1536 bytes
# The two id-list areas (block-relative). Everything else in the block is def
# tables, a u16 id table and a trailing pointer table -- never allocate there.
_LIST_AREA_LEN = 294
_LIST_AREAS = ((0, _LIST_AREA_LEN),
               (_SEC_DELTA, _SEC_DELTA + _LIST_AREA_LEN))
_CARAVAN_DEF_IDX = 39                  # Onrac Caravan def record (type 4)


def _def_scan(block, def_base, limit=64):
    """Yield (idx, rec, code, ptr, typ, size) for each def record at `def_base`
    whose list pointer lands inside the block. Stops at the first record that
    does not (the table is followed by unrelated data)."""
    for idx in range(limit):
        rec = def_base + idx * 8
        if rec + 8 > len(block):
            return
        code = int.from_bytes(block[rec:rec + 4], "little")
        ptr = int.from_bytes(block[rec + 4:rec + 8], "little")
        if not (SHOPS_RAM_BASE <= ptr < SHOPS_RAM_BASE + len(block)):
            return
        yield idx, rec, code, ptr, (code >> 4) & 0xF, code & 0xF


def _def_extent(block, def_base, idx):
    """(start, end) of the block bytes def `idx` owns, marker byte included."""
    for i, _rec, _code, ptr, typ, size in _def_scan(block, def_base):
        if i != idx:
            continue
        start = ptr - SHOPS_RAM_BASE
        return start, start + size + (1 if typ in _LIST_MARKER_TYPES else 0)
    return None


def _free_list_extents(block, relocating):
    """Sorted free [start, end) extents inside the id-list areas, given that the
    defs in `relocating` are about to be re-homed (so their current bytes are
    free) and every other def keeps its list where it is.

    In practice `relocating` is every weapon/armor/item store plus the Caravan,
    which frees ~226 bytes across both regions; the magic stores (types 5/6, no
    marker byte) and the Caravan PRESALE row keep theirs. Byte-level ownership,
    so overlapping or oddly-ordered records can never yield a bogus extent."""
    owned = bytearray(len(block))
    for def_base in _DEF_TABLES:
        for idx, _rec, _code, ptr, typ, size in _def_scan(block, def_base):
            if idx in relocating:
                continue
            start = ptr - SHOPS_RAM_BASE
            end = start + size + (1 if typ in _LIST_MARKER_TYPES else 0)
            for k in range(max(0, start), min(len(block), end)):
                owned[k] = 1
    free = []
    for lo, hi in _LIST_AREAS:
        k = lo
        while k < hi:
            if owned[k]:
                k += 1
                continue
            j = k
            while j < hi and not owned[j]:
                j += 1
            free.append([k, j])
            k = j
    return free


def _alloc_list(free, need):
    """First-fit `need` bytes out of `free` (mutated). Returns the block offset,
    or None when no extent is large enough."""
    for ext in free:
        if ext[1] - ext[0] >= need:
            off = ext[0]
            ext[0] += need
            return off
    return None


def shop_list_overlaps(block):
    """[(keeper idx, victim idx)] for every store whose id list shares block
    bytes with another store's. Empty on a healthy block.

    Walk in list order, widest first on a tie: the first store to claim a byte
    keeps it and anything landing inside it is the victim -- which is exactly
    the store relayout_shop_lists failed to seat before the budget clamp
    (v261)."""
    ext = []
    for idx, _rec, _code, ptr, typ, size in _def_scan(block, _DEF_TABLES[0]):
        if typ not in _DEF_MARKER_TYPES:
            continue                        # magic stores are never re-homed
        start = ptr - SHOPS_RAM_BASE
        ext.append((start, -size, idx,
                    start + size + (1 if typ in _LIST_MARKER_TYPES else 0)))
    claimed = {}                            # byte -> the def that owns it
    bad = []
    for start, _negsize, idx, end in sorted(ext):
        hit = next((claimed[b] for b in range(start, end) if b in claimed), None)
        if hit is not None:
            bad.append((hit, idx))
            continue                        # a victim claims nothing
        for b in range(start, end):
            claimed[b] = idx
    return bad


def repair_shop_list_overlaps(block):
    """Re-home every store whose id list overlaps another's, in place on a COPY.

    Client-side rescue for seeds generated before the relayout budget clamp:
    their slot_data can hold a store parked inside a neighbour's list (Elfheim
    armor at [68,71) inside Pravoka's [60,72), live 2026-08-13), which costs the
    victim its marker byte, its width and its shelf -- it sells the neighbour's
    rows, and its AP offer shows up on the neighbour's shelf as well.

    The victim keeps its width and whatever ids it currently reads (its AP tail
    is re-stamped by render_shop_ap_tail right after, so only stock rows carry
    over); it moves to free bytes in the id-list areas and gets its marker back.
    RNG-free and idempotent -- a healthy block is returned unchanged, so this is
    safe on the patch path every seed takes."""
    bad = shop_list_overlaps(block)
    if not bad:
        return bytes(block)
    out = bytearray(block)
    victims = {v for _keeper, v in bad}
    free = _free_list_extents(out, victims)
    for idx in sorted(victims,
                      key=lambda i: (-(_shop_def(out, _DEF_TABLES[0], i)[1]), i)):
        rec = _DEF_TABLES[0] + idx * 8
        typ = (int.from_bytes(out[rec:rec + 4], "little") >> 4) & 0xF
        _r, size, lst = _shop_def(out, _DEF_TABLES[0], idx)
        marker = typ in _LIST_MARKER_TYPES
        ids = [out[lst + k] for k in range(size)]
        off = _alloc_list(free, size + (1 if marker else 0))
        while off is None and size > 1:
            size -= 1
            ids = ids[:size]
            off = _alloc_list(free, size + (1 if marker else 0))
        if off is None:
            continue                        # nowhere to go: leave it as it lies
        ids_off = off
        if marker:
            out[off] = _LIST_MARKERS[typ]
            ids_off = off + 1
        for k, gid in enumerate(ids):
            out[ids_off + k] = gid
        for def_base in _DEF_TABLES:
            drec = def_base + idx * 8
            out[drec:drec + 4] = ((typ << 4) | size).to_bytes(4, "little")
            out[drec + 4:drec + 8] = (SHOPS_RAM_BASE + off).to_bytes(4, "little")
    return bytes(out)


def _clamp_widths_to_budget(widths, typ_of, free):
    """`widths` trimmed so every relocating store fits in `free` at once.

    The id-list areas hold ~226 free bytes once the gear stores let go of their
    vanilla homes, and the rolls can ask for more than that (18 stores x up to
    15 rows + a marker byte each). Something has to give; the only question is
    what. Trimming the WIDEST roll one row at a time spreads the loss over the
    stores that rolled big, instead of dumping all of it on whichever store the
    first-fit pass happens to reach last -- which is what used to fail to
    allocate at all.

    Each extent can waste up to one byte on a store that needs two, so the
    budget is the free bytes MINUS one per extent: with that slack a store can
    always be seated, and nothing ever falls through to the vanilla home.
    Returns a new dict; never mutates the caller's."""
    out = dict(widths)
    seats = [idx for idx in out if typ_of.get(idx) in _DEF_MARKER_TYPES]
    if not seats:
        return out
    pad = {idx: (1 if typ_of.get(idx) in _LIST_MARKER_TYPES else 0)
           for idx in seats}
    budget = sum(hi - lo for lo, hi in free) - len(free)
    need = sum(out[idx] + pad[idx] for idx in seats)
    while need > budget:
        widest = max(seats, key=lambda i: (out[i], -i))
        if out[widest] <= 1:
            break                    # already one row each: nothing left to give
        out[widest] -= 1
        need -= 1
    return out


_VANILLA_WIDTHS = None                 # {(city, row): vanilla def slot count}


def _vanilla_store_widths():
    """{(city, row): slot count} read off the SHIPPED shops block. Item stores
    are 4 or 5 there; _STORE_SIZES[2] says 7 because that table is a scan extent
    (the old flat widen), not a vanilla width."""
    global _VANILLA_WIDTHS
    if _VANILLA_WIDTHS is None:
        van = bytearray(RD.VANILLA["shops"])
        _VANILLA_WIDTHS = {
            key: _shop_def(van, _DEF_TABLES[0], idx)[1]
            for key, idx in _DEF_IDX.items()}
    return _VANILLA_WIDTHS


def _base_width(city, row):
    """Vanilla slot count before the random widening, for every store alike --
    item stores included since 2026-08-14 (see the _ITEM_BASE_WIDTH retirement).
    The Caravan is keyed (None, None)."""
    if city is None:
        return _CARAVAN_VANILLA_WIDTH
    return _vanilla_store_widths()[(city, row)]


def plan_shop_widths(rng, block, extra_max=_EXTRA_SLOTS_DEFAULT, ap_reserve=None,
                     hint_reserve=None):
    """{def_idx: slot_count} for every relocatable store: base width plus
    rng.randint(0, extra_max), clamped to the 15-slot nibble ceiling. Drawn in a
    fixed (city, row) order so a seed's layout is reproducible.

    `extra_max` is the yaml `shop_max_extra_items` (clamped to 0.._EXTRA_SLOTS_MAX).
    At 0 nothing widens at all -- not even the tiny-store guarantee, so Gaia is
    back to its single weapon. One roll happens per store either way, so the
    RNG stream is the same length at every setting.

    `ap_reserve` ({def_idx: n}) widens a store by n rows to carry n parallel AP
    offers, so the offers ADD rows instead of eating stock. Item stores have
    their random widening damped by the same n: every extra item row burns one
    of the 43 consumable ids that the placeholder pool also draws from, and an
    item store that stacks reserve + roll can otherwise crowd the 15-slot clamp,
    which would silently swallow the AP rows instead of the stock.

    `hint_reserve` ({def_idx: n}) reserves HINT rows (hints.plan_hint_products)
    the same way. A gear store carrying both kinds is reserving up to 9 rows, so
    its random widening is capped by the SHELF headroom (15 minus base minus the
    reserve) rather than damped by the reserve itself -- damping it the way item
    stores are damped left those stores at exactly base+reserve, i.e. with no
    random stock variety at all. Item stores keep their own damping: an extra
    item row burns one of the 43 consumable ids the placeholder pool needs.
    A store with no hint rows keeps its old roll, so an AP-only seed lays out
    byte-identically."""
    reserve = ap_reserve or {}
    hints = hint_reserve or {}
    extra_max = max(0, min(int(extra_max), _EXTRA_SLOTS_MAX))

    def roll(base, n=0, damp=False, headroom=False):
        lo = _EXTRA_SLOTS_MIN_TINY if base <= _TINY_STORE_WIDTH else 0
        lo = min(lo, extra_max)         # extra_max 0 = off, guarantee included
        top = extra_max
        if damp:
            top = max(lo, extra_max - n)
        elif headroom:
            top = max(lo, min(extra_max, _MAX_SHOP_SLOTS - base - n))
        return min(_MAX_SHOP_SLOTS, base + n + rng.randint(lo, top))

    widths = {}
    for (city, row), idx in sorted(_DEF_IDX.items()):
        n_hint = hints.get(idx, 0)
        widths[idx] = roll(_base_width(city, row),
                           reserve.get(idx, 0) + n_hint,
                           damp=(row == 2), headroom=bool(n_hint))
    widths[_CARAVAN_DEF_IDX] = roll(_base_width(None, None))
    return widths


def relayout_shop_lists(block, widths):
    """Re-home every store in `widths` to a freshly allocated id list of its new
    width and point BOTH def tables at that ONE list (ids from the primary
    pointer, row count from the secondary size nibble -- both are now identical).
    Mutates `block`; returns {def_idx: (list_off, size)}.

    Allocation is first-fit DECREASING so the widest lists claim the largest
    extents; a store the plan cannot seat at its rolled width is NARROWED to
    what is left rather than parked on top of a neighbour.

    The id-list areas are a fixed budget, so the rolled widths are clamped to
    fit BEFORE anything is placed (_clamp_widths_to_budget) and a store that
    still comes up short shrinks a row at a time. The old fallback -- "no room:
    keep the vanilla home" -- was the bug: _free_list_extents has already handed
    that store's vanilla bytes to somebody else, so the loser's def ended up
    pointing INTO another store's list. Live seed 2026-08-13: Elfheim's armor
    def sat at [68,71) inside Pravoka's [60,72), which cost Elfheim its 0xfc
    marker (it read 0x1d, a stray id byte), showed the wrong two rows, and put
    Elfheim's AP offer on Pravoka's shelf as well."""
    relocating = set(widths)
    free = _free_list_extents(block, relocating)
    plan = {}
    typ_of = {idx: typ for idx, _r, _c, _p, typ, _s
              in _def_scan(block, _DEF_TABLES[0]) if idx in relocating}
    widths = _clamp_widths_to_budget(widths, typ_of, free)
    for idx in sorted(relocating, key=lambda i: (-widths[i], i)):
        typ = typ_of.get(idx)
        if typ not in _DEF_MARKER_TYPES:
            continue                            # not a marker-carrying store
        size = widths[idx]
        marker = typ in _LIST_MARKER_TYPES      # type 4 has no marker byte
        off = _alloc_list(free, size + (1 if marker else 0))
        while off is None and size > 1:         # fragmented: take a narrower home
            size -= 1
            off = _alloc_list(free, size + (1 if marker else 0))
        if off is None:
            # Unreachable with the budget clamp (every store is guaranteed one
            # row), and NOT survivable by keeping the vanilla home -- those bytes
            # are in the free pool. Leave the def exactly as it stands: vanilla
            # pointer, vanilla width, no widening, no overlap.
            _rec, cur, lst = _shop_def(block, _DEF_TABLES[0], idx)
            plan[idx] = (lst, cur)
            continue
        ids_off = off
        if marker:
            block[off] = _LIST_MARKERS[typ]
            ids_off = off + 1
        for k in range(size):
            block[ids_off + k] = 0
        for def_base in _DEF_TABLES:
            rec = def_base + idx * 8
            block[rec:rec + 4] = ((typ << 4) | size).to_bytes(4, "little")
            block[rec + 4:rec + 8] = (SHOPS_RAM_BASE + off).to_bytes(4, "little")
        plan[idx] = (ids_off, size)
    return plan


def set_shop_width(block, idx, size):
    """Shrink def `idx` to `size` slots in BOTH def tables (the row count is read
    from the secondary one). Used to trim trailing unfilled slots so no store
    ever shows an id-0 '0 gil' blank row."""
    for def_base in _DEF_TABLES:
        rec = def_base + idx * 8
        code = int.from_bytes(block[rec:rec + 4], "little")
        block[rec:rec + 4] = (((code >> 4) << 4) | (size & 0xF)).to_bytes(4, "little")


# id-list marker byte by store type. Types 1/2/3 (weapon/armor/item) carry one
# before the ids; TYPE 4 (Caravan + presale) DOES NOT -- its def pointer aims
# straight at the first id. Verified against the vanilla block 2026-08-07:
# def[39] ptr -> offset 216 reads 0x1f (a real id), not a 0xfe marker.
_LIST_MARKERS = {1: 0xFD, 2: 0xFC, 3: 0xFE}
_LIST_MARKER_TYPES = tuple(_LIST_MARKERS)       # types whose list has a marker


def _vanilla_shop_ids(block):
    """Set of weapon/armor/item ids that appear in the VANILLA town stores
    (store rows 0/1/2 across all cities), keyed by cat 0/1/2. This is exactly
    "what normally appears in overworld item shops" (SHOP_TIER_OVERWORLD) -- used
    to keep DLC / exotic gear (Thor's Hammer, Mage's Staff, Gauntlets, Fangs,
    Curtains, etc.) out of the shuffle at low tiers. Scanned from the incoming
    vanilla block, so it stays correct if the store bytes ever change. Excludes
    id 0 (empty slot padding)."""
    sets = {0: set(), 1: set(), 2: set()}
    for j in range(3):
        for i in range(9):
            if _STORES[j][i] == -1:
                continue
            off = _CITY_STARTS[i] + _STORES[j][i]
            for k in range(_STORE_SIZES[j][i]):
                v = block[off + k]
                if v:
                    sets[j].add(v)
    return sets


def _shop_id_min_tier(cat, v, vanilla_ids, block_mana_items=False):
    """Lowest SHOP_TIER at which id `v` of category `cat` (0 weapon / 1 armor /
    2 item) becomes shop-eligible, or None if it must NEVER appear. Escalating
    gate, priceless-first: priceless -> ALL, activatable weapon/armor -> ACTIVATABLE,
    other exotic (non-vanilla-shop) -> EXOTIC, vanilla-shop id or a _MUNDANE_*_IDS
    override -> OVERWORLD."""
    if cat == 2 and v in _ALWAYS_ITEM_BLOCK:
        return None                                    # never (would sell for 0 gil)
    if block_mana_items and cat == 2 and v in _MANA_ITEM_BLOCK:
        return None                                    # inert under slot_magic
    if cat == 2 and v in _ALWAYS_PRICED_ITEMS:
        return SHOP_TIER_ACTIVATABLE                   # Fangs/Claw: battle-use -> activatable tier
    if cat == 2 and v in _ACTIVATABLE_TIER_ITEMS:
        return SHOP_TIER_ACTIVATABLE                   # Light Curtain: battle-use, own vanilla price
    priceless = ((cat == 0 and v in _PRICELESS_WEAPON_IDS)
                 or (cat == 1 and v in _PRICELESS_ARMOR_IDS)
                 or (cat == 2 and v in _PRICELESS_ITEM_PRICES))
    if priceless:
        return SHOP_TIER_ALL
    if (cat == 0 and v in _ACTIVATABLE_WEAPON_IDS) or \
       (cat == 1 and v in _ACTIVATABLE_ARMOR_IDS):
        return SHOP_TIER_ACTIVATABLE
    if (cat == 0 and v in _MUNDANE_WEAPON_IDS) or \
       (cat == 1 and v in _MUNDANE_ARMOR_IDS):
        return SHOP_TIER_OVERWORLD                     # treasure-only but ordinary
    if v not in vanilla_ids[cat]:
        return SHOP_TIER_EXOTIC
    return SHOP_TIER_OVERWORLD


def consumable_placeholder_capacity(tier, block_mana_items=False):
    """How many consumable ids are GUARANTEED free to serve as AP shop
    placeholders at `tier`, i.e. ids the shuffle can never stock.

    Parallel AP shop rows each need their own placeholder gid (price, name and
    description all hang off the item id, not the shop row -- see _STOCK_PRICE),
    and the 6 item stores can only draw from the 43 consumable ids. An id that
    is INELIGIBLE for stock at this tier is free by construction, which makes
    the supply a function of the tier rather than of the seed's luck:

        tier 0 unshuffled   31    (everything outside the vanilla town lists)
        tier 1 mundane      30
        tier 2 exotic       18    <- the default: exactly 3 rows x 6 item stores
        tier 3 activatable  11
        tier 4 all           0    (falls back to eligible-but-unstocked ids)

    Elixir (_ALWAYS_ITEM_BLOCK) is excluded even though it is never stocked: it
    has no vanilla price, and leaving it out keeps a one-id safety margin.
    Weapons and armor have no never-stockable ids but ~67/75 ids against ~22
    stocked slots, so gear stores never bind -- this is the item-store limit.

    RNG-FREE and options-only, so create_regions can size the location list
    without touching self.random (see logic.shop_offer_counts)."""
    vanilla_ids = _vanilla_shop_ids(bytearray(RD.VANILLA["shops"]))
    free = 0
    for g in range(1, _ITEM_ID_MAX + 1):
        if g in _ALWAYS_ITEM_BLOCK:
            continue                                   # margin, see docstring
        if block_mana_items and g in _MANA_ITEM_BLOCK:
            continue                                   # inert under slot_magic
        if tier <= SHOP_TIER_UNSHUFFLED:
            # Nothing is rewritten at this tier, so "free" is exactly "not in
            # the vanilla town lists" -- _shop_id_min_tier would wrongly call
            # every vanilla id free here (it reports OVERWORLD == tier 1).
            if g not in vanilla_ids[2]:
                free += 1
            continue
        mt = _shop_id_min_tier(2, g, vanilla_ids, block_mana_items)
        if mt is None or mt > tier:
            free += 1
    return free


# Max AP offers one shop may carry. Bounded by the location id space:
# ids.SHOP_STRIDE reserves 8 ids per shop and logic.SHOP_MAX_OFFERS registers 6
# of them in the datapackage, so 6 is the ceiling that keeps ids stable.
MAX_SHOP_OFFERS = 6

# ---------------------------------------------------- shared placeholder gids
# STORE-KEYED VIRTUAL ROWS (v2, 2026-08-16). Rows in DIFFERENT stores share
# placeholder gids; the client authors each gid's identity (name, price, desc)
# per TOWN on town entry. What makes that sound, all live-proven 2026-08-16
# (re_only/v2_walkthrough.py):
#   * purchases attribute by (store, gid) -- the BUYB mailbox carries the store
#     id, so the same gid on two shelves names two different rows;
#   * the Buy list snapshots PRICES at dialog open but charges the LIVE record,
#     so prices must be authored before the list opens -- town entry (street
#     FIELD_MAP_ID) is seconds earlier; names re-read live every frame;
#   * a town has at most ONE store per category, so one town's rows never
#     contend for a gid -- the per-store ceilings below are the whole demand.
#
# Placeholder supply therefore stops being a per-seed draw against the loot
# pools (the "Gaia Armor Shop: AP Stock 2" unreachable check, 2026-08-15) and
# becomes these CONSTANTS. Chosen with the user (2026-08-16): low-tier gear
# nobody misses, Cat Claws/Rapier/Mythril Sword/Buckler/Knight's Armor kept as
# real items. Order matters -- row k of every store in a category uses entry k,
# and only the prefix a seed actually needs is reserved (scrubbed from stock,
# masks zeroed, pool copies demoted), so a default seed spends 5 gear ids, not
# 7. The two armor entries that live in the CHEST POOL (Leather Cap, Bronze
# Gloves) sit LAST so only 6-7-row seeds demote their chests to filler.
# Consumables stay ordinary grantable filler (the town-local name overlap is
# the same "residual, accepted" as v202); gear is exclusive because the dupe
# guard zeroes its equip masks.
RESERVED_SHOP_PLACEHOLDERS = {
    0: [(2, 1), (2, 9), (2, 5), (2, 7), (2, 8), (2, 11), (2, 13)],
    #   Nunchaku, Iron Nunchaku, Hammer, Battle Axe, Scimitar, Crosier,
    #   Longsword
    1: [(3, 2), (3, 3), (3, 32), (3, 47), (3, 63), (3, 46), (3, 64)],
    #   Leather Armor, Chain Mail, Iron Shield, Helm, Leather Gloves,
    #   Leather Cap (pool), Bronze Gloves (pool)
    2: [(1, 19), (1, 24), (1, 27)],
    #   Spider's Silk, Red Curtain, Lunar Curtain (all unstocked in vanilla)
}
# One gear store's whole tail: offers (<= MAX_SHOP_OFFERS) plus hint rows fill
# up to this. 7 == len(RESERVED_SHOP_PLACEHOLDERS[0]) -- the supply IS the cap.
GEAR_SHOP_MAX_ROWS = 7
# Item stores carry no hints and cap their offers here, bounding the reserved
# consumables at 3 (user 2026-08-16: the tiny consumable space must not fund
# six rows).
ITEM_SHOP_MAX_OFFERS = 3


def reserved_used_prefix(counts, hint_caps=None):
    """{store row -> entries of RESERVED_SHOP_PLACEHOLDERS[row] a seed can
    touch}: the deepest offers+hints tail any store of that row may seat.
    Deterministic from (counts, hint_caps) -- options + the count roll -- so
    create_items (pool demotion), the bake (mask zeroing, stock scrub) and the
    client (via slot_data) all agree without sharing state."""
    used = {0: 0, 1: 0, 2: 0}
    caps = hint_caps or {}
    for o, (_city, row, _ph) in enumerate(SHOP_AP_SLOTS):
        n = int((counts or {}).get(o, 0)) + int(caps.get(o, 0))
        used[row] = max(used[row], min(n, len(RESERVED_SHOP_PLACEHOLDERS[row])))
    return used


def shop_offer_counts(requested, tier, block_mana_items=False, rng=None):
    """{shop ordinal -> AP offers that shop actually gets}, for `requested`
    offers per shop at ShopItemPool `tier`.

    Supply is structural now (RESERVED_SHOP_PLACEHOLDERS): gear stores always
    seat the full request (<= MAX_SHOP_OFFERS 6 < GEAR_SHOP_MAX_ROWS 7), item
    stores clamp to ITEM_SHOP_MAX_OFFERS. No count registered here can ever
    miss its shelf row -- the per-seed supply arithmetic (gear_caps, the random
    short deal, consumable_placeholder_capacity coupling) is deleted with the
    per-seed placeholder draw itself.

    `rng` (a dedicated random.Random, NOT the world's) makes each shop roll its
    own count in 1..cap so no two seeds lay out the same. NEVER consumes the
    world's RNG: create_regions calls this to size the location list."""
    requested = max(0, min(int(requested), MAX_SHOP_OFFERS))
    if requested == 0:
        return {}
    # At SHOP_TIER_UNSHUFFLED nothing is widened or relocated (shuffle_shop_items
    # never runs), so the AP rows must fit inside each store's VANILLA width and
    # still leave it one real item. Gaia weapons is 1 row wide, so it keeps the
    # long-standing single-offer behaviour: the offer IS the store, and selling
    # it leaves a filler (see render_shop_ap_tail).
    fit = {}
    if tier <= SHOP_TIER_UNSHUFFLED:
        van = bytearray(RD.VANILLA["shops"])
        for o, (city, row, _ph) in enumerate(SHOP_AP_SLOTS):
            _rec, size, _lst = _shop_def(van, _DEF_TABLES[0], _DEF_IDX[(city, row)])
            fit[o] = max(1, size - 1)
    out = {}
    for o, (_city, row, _ph) in enumerate(SHOP_AP_SLOTS):
        cap = min(requested, ITEM_SHOP_MAX_OFFERS) if row == 2 else requested
        cap = max(1, min(cap, fit.get(o, cap)))
        out[o] = rng.randint(1, cap) if rng is not None else cap
    return out


def reserved_placeholder_map(counts, hint_caps=None, hint_target=0):
    """The shared-tail replacement for pick_seed_placeholders: {ordinal:
    [(cat, gid), ...]} sliced straight off RESERVED_SHOP_PLACEHOLDERS.

    Offers take entries [0:n) of the store's row constant; hint rows continue
    at [n:n+h). Supply covers every legal request by construction (counts <= 6
    or 3, offers+hints <= 7), so there is no drain path, no fallback and no
    RNG. Hint rows are seated round-robin over the capped gear stores until
    `hint_target` rows are placed -- every store gets its first row before any
    store gets a second, same fairness as the old picker, minus the dry-pool
    skip that could starve a category."""
    ph = {o: list(RESERVED_SHOP_PLACEHOLDERS[row][:int((counts or {}).get(o, 0))])
          for o, (_c, row, _p) in enumerate(SHOP_AP_SLOTS)}
    caps = dict(hint_caps or {})
    if caps and hint_target > 0:
        added = {o: 0 for o in caps}
        placed = 0
        while placed < hint_target:
            progress = False
            for o in sorted(caps):
                if placed >= hint_target:
                    break
                row = SHOP_AP_SLOTS[o][1]
                depth = len(ph.get(o, ())) + 1
                if (added[o] >= caps[o]
                        or depth > len(RESERVED_SHOP_PLACEHOLDERS[row])):
                    continue
                ph.setdefault(o, []).append(
                    RESERVED_SHOP_PLACEHOLDERS[row][depth - 1])
                added[o] += 1
                placed += 1
                progress = True
            if not progress:
                break                   # every store is at its cap
    return {o: rows for o, rows in ph.items() if rows}


# (RETIRED 2026-07-27: _AIRSHIP_SHOP_CITIES + the late_activatable shop gate that
# confined spell-on-use gear to the airship-only Gaia store. It paired with the
# placement-side late_activatable_equipment option, now superseded by
# equipment_runes -- which gates ACTIVATION, so buying the gear early is harmless:
# it simply cannot be activated until the Equipment Rune Key is assembled.)


def shuffle_shop_items(rng, block, tier=SHOP_TIER_OVERWORLD, ap_slots=False,
                       ap_blocked=None, block_mana_items=False,
                       ap_reserve=None, hint_reserve=None,
                       extra_slots=_EXTRA_SLOTS_DEFAULT):
    """Randomize weapon/armor/item store inventories (rows j=0..2). Port of the
    randomizeStores() fill for those categories. Magic stores (j=3..6) are NOT
    touched here -- see shuffle_magic_shops. `block` is the 0x600-byte region
    starting at 0x2b1a1dc; secondary region 0x2b1a4c8 falls inside it.

    `tier` (SHOP_TIER_*) sets how deep the draw pool goes: every id whose
    _shop_id_min_tier exceeds `tier` is rerolled out of every store, so e.g. at
    SHOP_TIER_OVERWORLD only vanilla town-shop ids can be stocked and at
    SHOP_TIER_ACTIVATABLE the spell-on-use weapons join but priceless gear still
    can't. With `ap_slots`, each AP placeholder id is rerolled out of ITS OWN
    store's normal slots only (`ap_blocked` = ap_blocked_by_store map) -- since
    the BUYB purchase mailbox (v202) attributes purchases by store id, the same
    id is ordinary stock anywhere else.

    `extra_slots` is the yaml `shop_max_extra_items` -- the most EXTRA normal
    stock rows a store may roll on top of its base width (see
    plan_shop_widths)."""
    out = bytearray(block)
    vanilla_ids = _vanilla_shop_ids(block)      # BEFORE the relayout moves lists
    blocked_ids = (ap_blocked if ap_blocked is not None
                   else ap_blocked_by_store(default_placeholders()))

    # Random extra width + list relocation. Must run before the fill loop: every
    # store's slot count and list home come from the def records afterwards, not
    # from _STORE_SIZES / _STORES.
    #
    # AP rows are RESERVED here rather than stamped over stock afterwards. The
    # old single-offer build overwrote the last filled slot, which cost the
    # store one real item; with several offers that would gut a store, and a
    # pool-dry trim (set_shop_width below) could leave fewer rows than there are
    # offers to place.
    reserve = {}
    for o, n in (ap_reserve or {}).items():
        city, row, _ph = SHOP_AP_SLOTS[o]
        reserve[_DEF_IDX[(city, row)]] = int(n)
    hint_res = {}
    for o, n in (hint_reserve or {}).items():
        city, row, _ph = SHOP_AP_SLOTS[o]
        hint_res[_DEF_IDX[(city, row)]] = int(n)
    widths = plan_shop_widths(rng, out, extra_max=extra_slots,
                              ap_reserve=reserve, hint_reserve=hint_res)
    plan = relayout_shop_lists(out, widths)

    # Global one-per-id throttle. Gear (cat 0 weapon, 1 armor) and consumables
    # (cat 2) all cap at ONE slot across every store; the item stores joined the
    # throttle 2026-08-07 (user: item shops should not all sell White Curtain).
    # The cap is a strong PREFERENCE, not an invariant -- with the widened stores
    # there are more consumable slots than eligible consumable ids, so draw()'s
    # relax pass hands out a second copy rather than leaving a blank shelf.
    used = {0: {}, 1: {}, 2: {}}

    def hard_blocked(city, cat, v):
        # Constraints that must NEVER be relaxed: an out-of-tier id, or the AP
        # placeholder of THIS store, must not leak even when the soft pool is
        # exhausted.
        if ap_slots and v in blocked_ids.get((city, cat), ()):
            return True                                # this store's AP slot id
        if cat not in (0, 1, 2):
            return False                               # magic handled elsewhere
        need = _shop_id_min_tier(cat, v, vanilla_ids, block_mana_items)
        return need is None or need > tier

    def blocked(city, cat, v, cap, avoid):
        if v in avoid:
            return True                                # already on THIS shelf
        if cat in used and used[cat].get(v, 0) >= cap:
            return True                                # already stocked elsewhere
        return hard_blocked(city, cat, v)

    def draw(city, cat, gen, avoid=()):
        """Reroll until allowed.

        When the eligible pool is smaller than the number of slots to fill -- the
        widened item stores ask for ~57 consumable slots against ~31 ids that
        clear the tier gate -- the per-id cap is raised ONE STEP AT A TIME rather
        than dropped. No id gets a third copy until every id has two, so the
        unavoidable duplicates stay as evenly spread as the pool allows instead
        of clustering on whatever the rng favoured (measured before this: one id
        at 4 copies while others sat at 1).

        The tier / AP-placeholder gate is never relaxed: a repeated in-tier id
        beats leaking a higher-tier one. Returns None when even that cannot be
        satisfied -- at the low tiers a store can be WIDER than the whole
        eligible pool (SHOP_TIER_MUNDANE leaves ~12 consumable ids against item
        stores of up to 12 slots), and the caller trims the store rather than
        shelving a duplicate or a blank row."""
        for cap in range(_ID_CAP, _ID_CAP + _ID_CAP_STEPS):
            v = gen()
            for _ in range(400):
                if not blocked(city, cat, v, cap, avoid):
                    return v
                v = gen()
        v = gen()
        for _ in range(1000):
            if not hard_blocked(city, cat, v) and v not in avoid:
                return v
            v = gen()
        return None

    for i in range(9):                     # city
        for j in range(3):                 # store type: weapons/armor/items only
            idx = _DEF_IDX.get((i, j))
            if idx is None or idx not in plan:
                continue
            list_off, size = plan[idx]
            # Leave the reserved AP tail unfilled -- render_shop_ap_tail owns
            # those rows. Never reserve the whole store: a store must keep at
            # least one normal item.
            res = min(reserve.get(idx, 0), max(0, size - 1))
            stock = size - res
            stack = []
            k = 0
            while k < stock:
                general = lambda: rng.randrange(_MAX_NUM[j] - _MIN_NUM[j] + 1) + _MIN_NUM[j]
                if j == 2 and (((i == 0 or i == 1) and k <= 4) or ((i == 2 or i == 3) and k <= 1)):
                    gen = lambda: _COMMON_ITEMS[rng.randrange(len(_COMMON_ITEMS))]
                elif j == 2 and ((i == 5 or i == 6) and k <= 1):
                    gen = lambda: _RARE_ITEMS[rng.randrange(len(_RARE_ITEMS))]
                else:
                    gen = general
                # `stack` is passed as the avoid-set, so a store can never stock
                # the same id twice and the draw cannot spin against itself.
                v = draw(i, j, gen, stack)
                if v is None and gen is not general:
                    # The flavour sub-pool is a PREFERENCE, not a constraint: at
                    # SHOP_TIER_MUNDANE every _RARE_ITEMS id is out of tier, which
                    # used to leak an out-of-tier id (draw's old fallback returned
                    # its last reroll unchecked). Fall back to the general in-tier
                    # draw instead.
                    v = draw(i, j, general, stack)
                if v is None:
                    break                    # pool dry: trim to k slots below
                # ONE list, both def tables pointing at it (relayout_shop_lists):
                # the old independent second-region draw is retired.
                out[list_off + k] = v
                used[j][v] = used[j].get(v, 0) + 1
                stack.append(v)
                k += 1
            if k < stock:                    # never leave an id-0 "0 gil" row
                # Trim the unfilled STOCK rows only; the reserved tail stays,
                # so the AP offers still have somewhere to live.
                set_shop_width(out, idx, k + res)
                plan[idx] = (list_off, k + res)

    # guarantee: a status-cure item is always buyable somewhere (Remedy/Gold
    # Needle must never be locked out of the whole world). Checks EVERY store,
    # not just the two starting towns, and overwrites a slot whose id is stocked
    # more than once so the guarantee can never be what creates a duplicate.
    if not (_stocked_item_ids(out, plan) & set(_CURE_IDS)):
        idx = _DEF_IDX[(0, 2)]                           # Cornelia item shop
        list_off, size = plan[idx]
        # Stock rows only: overwriting a reserved AP row would drop an offer.
        stock = size - min(reserve.get(idx, 0), max(0, size - 1))
        spot = next((list_off + k for k in range(stock)
                     if used[2].get(out[list_off + k], 0) > 1), list_off)
        used[2][out[spot]] = max(0, used[2].get(out[spot], 1) - 1)
        out[spot] = _CURE_IDS[1]                         # Gold Needle
        used[2][_CURE_IDS[1]] = used[2].get(_CURE_IDS[1], 0) + 1

    _fill_caravan(rng, out, tier, vanilla_ids, ap_slots, blocked_ids,
                  block_mana_items, used=used[2], plan=plan)
    return out


_CURE_IDS = (0x0a, 0x0c)                 # Remedy, Gold Needle


def _stocked_item_ids(block, plan):
    """Every consumable id stocked by a town item store in `plan`."""
    out = set()
    for (city, row), idx in _DEF_IDX.items():
        if row != 2 or idx not in plan:
            continue
        list_off, size = plan[idx]
        out |= {block[list_off + k] for k in range(size)}
    return out


# Caravan slot split: 2 ids drawn from the selected tier, 3 from ONE tier above it
# (see _caravan_pools). Sums to _CARAVAN_VANILLA_WIDTH.
_CARAVAN_AT_TIER = 2
_CARAVAN_ABOVE_TIER = 3


def _caravan_tier_ids(exact_tier, vanilla_ids, ap_slots=False, ap_blocked=None,
                      block_mana_items=False):
    """Consumable (cat 2) ids whose MINIMUM shop tier is exactly `exact_tier`.
    The caravan (city 8) has no AP slot, and since the BUYB purchase mailbox
    (v202) attributes purchases by store id, placeholder ids are ordinary goods
    here -- no reservation. `ap_slots`/`ap_blocked` kept for signature
    stability (unused)."""
    return [v for v in range(1, _ITEM_ID_MAX + 1)
            if _shop_id_min_tier(2, v, vanilla_ids,
                                 block_mana_items) == exact_tier]


def _caravan_pools(tier, vanilla_ids, ap_slots=False, ap_blocked=None,
                   block_mana_items=False):
    """(at_tier_ids, above_tier_ids) for the Onrac Caravan at shop `tier`. The
    caravan is a "stepped-up" vendor: _CARAVAN_AT_TIER of its slots come from the
    rarest tier the seed's shop pool actually reaches (`tier`) and
    _CARAVAN_ABOVE_TIER come from the tier just ABOVE it, i.e. goods the player
    cannot buy in any town at this setting. E.g. at EXOTIC the caravan mixes
    tonics/exotic consumables with activatable ones (Vampire Fang, Spider's Silk,
    Cockatrice Claw). At SHOP_TIER_ALL there is no tier above, so the above-pool is
    empty and _fill_caravan takes the whole width from `tier`. The vanilla caravan
    tonics count as EXOTIC because _vanilla_shop_ids skips city 8, so they are not
    treated as OVERWORLD town items. See caravan-shop-pool."""
    at = _caravan_tier_ids(tier, vanilla_ids, ap_slots, ap_blocked,
                           block_mana_items)
    above = (_caravan_tier_ids(tier + 1, vanilla_ids, ap_slots, ap_blocked,
                               block_mana_items)
             if tier < SHOP_TIER_ALL else [])
    return at, above


def _fill_caravan(rng, out, tier, vanilla_ids, ap_slots=False, ap_blocked=None,
                  block_mana_items=False, used=None, plan=None):
    """Rewrite the Onrac Caravan sale list (def[39], both language regions) from
    the stepped-up split (_caravan_pools): _CARAVAN_AT_TIER ids from `tier` plus
    _CARAVAN_ABOVE_TIER from one tier above. If either side is short (or the
    above-pool is empty at SHOP_TIER_ALL) the other side covers the remainder.
    Distinct ids, NO id-0 blank rows: the def slot count is set to the number of
    ids actually placed (<= the vanilla width of 5). `out` is indexed
    block-relative to _SHOP_BASE (like the `put` helper in shuffle_shop_items).
    The presale offer (def[38], the Bottled Faerie key item) is left untouched."""
    at_pool, above_pool = _caravan_pools(tier, vanilla_ids, ap_slots, ap_blocked,
                                         block_mana_items)
    # Diversify against the towns: drop ids the town stores already stock. Only
    # the at-tier side really collides (the above-tier side is a rarity the towns
    # cannot sell at this setting), and a filter that empties a pool is dropped
    # -- a short caravan is worse than one repeat.
    if used:
        at_kept = [v for v in at_pool if used.get(v, 0) < _ID_CAP]
        above_kept = [v for v in above_pool if used.get(v, 0) < _ID_CAP]
        at_pool = at_kept or at_pool
        above_pool = above_kept or above_pool
    if not at_pool and not above_pool:
        return out
    if plan is None or _CARAVAN_DEF_IDX not in plan:
        return out
    list_off, width = plan[_CARAVAN_DEF_IDX]
    # Slots beyond the vanilla 5 are split in the same 2:3 at-tier / above-tier
    # ratio, so a widened caravan stays a stepped-up vendor rather than drifting
    # toward whichever pool happens to be larger.
    want_at = max(1, round(width * _CARAVAN_AT_TIER / _CARAVAN_VANILLA_WIDTH))
    want_above = width - want_at
    n_at = min(want_at, len(at_pool))
    n_above = min(want_above, len(above_pool))
    # short side spills into the other so the caravan still fills its width
    n_at = min(len(at_pool), n_at + (want_above - n_above))
    n_above = min(len(above_pool), n_above + max(0, want_at - n_at))
    chosen = rng.sample(at_pool, n_at) + rng.sample(above_pool, n_above)
    rng.shuffle(chosen)
    n = min(width, len(chosen))
    # ONE list, both def tables pointing at it; size = ids actually placed, so
    # the caravan never carries an unfilled id-0 "0 gil" blank row.
    for k in range(n):
        out[list_off + k] = chosen[k] & 0xFF
        if used is not None:
            used[chosen[k]] = used.get(chosen[k], 0) + 1
    if n != width:
        set_shop_width(out, _CARAVAN_DEF_IDX, n)
    return out


# ------------------------------------------------------------ shop AP slots
# AP purchases sold in town stores (shop_ap_offers). Each shop's AP stock
# REPLACES the last slot of a weapon/armor/item store with a PLACEHOLDER game
# item; buying it hands the placeholder to the player, the client detects it in
# the inventory, removes it, and sends the shop's NEXT unsold offer's location
# check (offers sell one at a time; the client rewrites the placeholder's
# name/description/price between sales). After the LAST offer the client delists
# the slot from the (boot-patched) shop table so nothing more can be bought.
# See logic.SHOP_LOCATIONS (same order).
#
# The shops block itself tells us where everything lives: the shop-def table
# (8-byte records of (u32 code, u32 ptr)) sits at block+0x128 (primary region)
# and block+0x414 (secondary/2nd-language region). code low nibble = slot count,
# high nibble = store type (1 weapon / 2 armor / 3 item / 4 caravan / 5-6 magic);
# ptr is an ABSOLUTE RAM address into the block (block loads at 0x0893E188), so
# list_offset = ptr - 0x0893E188. Weapon/armor/item lists carry 1 marker byte
# (0xfd/0xfc/0xfe) before the ids; magic lists don't. Derived offline from the
# vanilla block + FFRPSP's randomizeStores port above.
SHOPS_RAM_BASE = 0x0893E188          # where the shops block lives in PSP RAM
_DEF_TABLES = (0x128, 0x414)         # shop-def tables: primary + secondary region
_DEF_MARKER_TYPES = (1, 2, 3, 4)     # store types whose list starts with a marker

# def-record index for each (city, store_row) cell, in shop-def table order
# (verified against the vanilla block: codes 15,23,34,54,64|14,25,35,54,64|...).
_DEF_IDX = {
    (0, 0): 0,  (0, 1): 1,  (0, 2): 2,          # Cornelia  w/a/i
    (1, 0): 5,  (1, 1): 6,  (1, 2): 7,          # Pravoka
    (2, 0): 10, (2, 1): 11, (2, 2): 12,         # Elfheim
    (3, 0): 17, (3, 1): 18,                     # Melmond (no item shop)
    (4, 0): 21, (4, 1): 22, (4, 2): 23,         # Crescent Lake
    (5, 2): 26,                                 # Onrac (item shop only)
    (6, 0): 29, (6, 1): 30, (6, 2): 31,         # Gaia
}

# Ids that may NEVER be a placeholder, whatever the pools offer (a HARD ban:
# a drained seed grants fewer hint rows rather than reaching for these).
# A placeholder's authored name, price and zeroed equip masks follow the ITEM ID
# everywhere while the row is unsold, so an id the player already OWNS wears the
# shop row's identity in their own menus. Two ways that happens, both observed:
#
#   * STARTING EQUIPMENT -- Knife/Staff/Clothes are handed out at new game and are
#     not in DATA.ITEM_POOL, so _spare_gear read them as free supply. Live
#     2026-08-12 (user): a Cornelia weapon shop showed the Thief's equipped Knife
#     as "HINT: MatoyaCav", sell 0 gil, equip masks zeroed by force_shop_ap_prices
#     (so Remove/Optimal could not put it back on). Staff/Clothes are chest-pool
#     items today and were already safe -- pinned here so a pool edit can't undo
#     that.
#   * _DELIST_FILLER -- a delisted stock row is rewritten to Knife/Clothes/Potion,
#     which would put a hint-priced, hint-named row in a store that sells no hints.
#
# Potion is banned for the placeholder draw ONLY: it must stay ordinary stock, so
# it does NOT go in _ALWAYS_ITEM_BLOCK.
NEVER_PLACEHOLDER = frozenset({
    (2, 2),    # Knife   -- starting weapon, weapon-row _DELIST_FILLER
    (2, 3),    # Staff   -- starting weapon
    (3, 1),    # Clothes -- starting armor, armor-row _DELIST_FILLER
    (1, 1),    # Potion  -- item-row _DELIST_FILLER
})


# Slot table. Order == logic.SHOP_LOCATIONS ordinals == slot_data order.
# (city, store_row, placeholder (cat, game_id)). The static placeholder column
# is only a last-ditch fallback: on the default placeholder_pools path,
# pick_seed_placeholders chooses every placeholder POST-SHUFFLE from ids the
# shuffled stores stock nowhere. Since the BUYB purchase mailbox (v202)
# attributes purchases by store id, a placeholder is an ordinary item away from
# its own AP shelf (see ap_blocked_by_store) -- the old "absent from the chest
# and thief-steal pools" guard is retired (the 2026-07-29 trade below had
# already broken it for steals).
#
# 2026-07-29 (user): the two consumable placeholders that cost the caravan its
# rarest stock -- Turbo Ether (5) and Hermes' Shoes (28) -- were traded for LOW-tier
# Eye Drops (13) and Echo Grass (14), so the reserved ids come off the top of the
# consumable pool instead of its ceiling. Eye Drops/Echo Grass ARE vanilla town
# stock, so scrub_placeholder_stock() must strip them from every store list at
# EVERY shop_item_pool tier (unshuffled included) or a real purchase would fire a
# bogus AP check. Turbo Ether/Hermes' Shoes are now ordinary stock again (they were
# already in the steal pools, which the old table technically violated).
# cat: 1 = consumable, 2 = weapon, 3 = armor (== ff1_data CAT_*).
SHOP_AP_SLOTS = [
    (0, 0, (2, 65)),   # 0  Cornelia weapons   <- Barbarian's Sword
    (0, 1, (3, 45)),   # 1  Cornelia armor     <- Master Shield
    (0, 2, (1, 30)),   # 2  Cornelia items     <- Cockatrice Claw
    (1, 0, (2, 60)),   # 3  Pravoka weapons    <- Assassin Dagger
    (1, 1, (3, 30)),   # 4  Pravoka armor      <- Lordly Robes
    (1, 2, (1, 19)),   # 5  Pravoka items      <- Spider's Silk
    (2, 0, (2, 56)),   # 6  Elfheim weapons    <- Kikuichimonji
    (2, 1, (3, 28)),   # 7  Elfheim armor      <- Maximillian
    (2, 2, (1, 13)),   # 8  Elfheim items      <- Eye Drops
    (3, 0, (2, 47)),   # 9  Melmond weapons    <- Dark Claymore
    (3, 1, (3, 27)),   # 10 Melmond armor      <- Genji Armor
    (4, 0, (2, 50)),   # 11 Crescent weapons   <- Deathbringer
    (4, 1, (3, 40)),   # 12 Crescent armor     <- Genji Shield
    (4, 2, (1, 24)),   # 13 Crescent items     <- Red Curtain
    (5, 2, (1, 27)),   # 14 Onrac items        <- Lunar Curtain
    (6, 0, (2, 44)),   # 15 Gaia weapons       <- Lightbringer
    (6, 1, (3, 62)),   # 16 Gaia armor         <- Shadow Mask
    (6, 2, (1, 14)),   # 17 Gaia items         <- Echo Grass
]


def default_placeholders():
    """The static placeholder map {ordinal: (cat, gid)} from SHOP_AP_SLOTS. Used
    when the exotic/priceless loot pool is OFF, so the whole placeholder pipeline
    behaves byte-identically to the pre-dynamic-placeholder build."""
    return {o: SHOP_AP_SLOTS[o][2] for o in range(len(SHOP_AP_SLOTS))}


# Hint rows are sold by WEAPON and ARMOR stores only. Item stores are excluded on
# purpose: every row needs a placeholder id the shops never stock, and the item
# stores' 43 consumables are already the binding constraint for AP offers (see
# consumable_placeholder_capacity -- 18 free ids at the default tier, all six
# item stores' AP rows drawing from them). Gear has ~67/75 ids against ~22
# stocked slots, so hint rows there cost nothing scarce. Their name banks also
# re-lay out, which is what lets "HINT: Sunken Mermaid" fit (see hints.py).
HINT_SHOP_ORDINALS = tuple(o for o, (_c, row, _ph) in enumerate(SHOP_AP_SLOTS)
                           if row in (0, 1))
# Rows one store may carry, and what the seed AIMS for in total. The target is a
# world-level figure, not a per-shop promise (the yaml only says yes/no): the
# allocator fills stores round-robin, so it lands on 2 apiece when everything
# fits and shifts rows to the stores that CAN take them when a category's
# placeholder ids run short (measured 2026-08-11: at default loot the armor ids
# come up ~1 short while weapons have ~10 spare, which cost two towns their
# hints outright before this existed).
MAX_HINT_OFFERS = 3
DEFAULT_MAX_HINT_OFFERS = 2
HINT_TARGET_ROWS = DEFAULT_MAX_HINT_OFFERS * len(HINT_SHOP_ORDINALS)


def hint_target_rows(max_offers=DEFAULT_MAX_HINT_OFFERS):
    """World-level row budget for a seed whose per-shop cap is `max_offers`
    (yaml `shop_max_hints`). Per-shop is a CAP, so this is what the allocator
    aims for, not a promise."""
    return max(0, min(int(max_offers), MAX_HINT_OFFERS)) * len(HINT_SHOP_ORDINALS)


def hint_shop_caps(max_offers, tier, ap_counts=None):
    """{gear shop ordinal -> most hint rows that store may carry}. This is the
    WIDTH RESERVE, not an allocation: pick_seed_placeholders hands out rows
    against it up to HINT_TARGET_ROWS, and build_shuffle_tables trims each store
    back to the rows it actually got.

    Reserving the cap rather than the fair share is what makes spill possible:
    a store cannot be given a row it has no shelf space for, and shelf space is
    decided before the shuffle while placeholder supply is only known after it.

    At SHOP_TIER_UNSHUFFLED nothing is widened or relocated, so a hint row can
    only take a slot the store already has: what is left of the vanilla width
    after its AP offers, keeping at least one real item on the shelf. Gaia's
    1-row weapon store therefore sells no hints, which is correct -- its single
    row IS the AP offer.

    RNG-FREE (options + vanilla bytes only), like shop_offer_counts: hint rows
    create no locations, but anything reachable from create_regions inherits the
    "never consume the world RNG" rule.

    `max_offers` is the yaml `shop_max_hints` value: the per-store CAP, 0 =
    feature off, clamped to MAX_HINT_OFFERS."""
    cap = max(0, min(int(max_offers), MAX_HINT_OFFERS))
    if not cap:
        return {}
    ap = ap_counts or {}
    fit = {}
    if tier <= SHOP_TIER_UNSHUFFLED:
        van = bytearray(RD.VANILLA["shops"])
        for o in HINT_SHOP_ORDINALS:
            city, row, _ph = SHOP_AP_SLOTS[o]
            _rec, size, _lst = _shop_def(van, _DEF_TABLES[0], _DEF_IDX[(city, row)])
            fit[o] = max(0, size - 1 - int(ap.get(o, 0)))
    out = {}
    for o in HINT_SHOP_ORDINALS:
        # Shared tails (v2): a gear store's whole tail -- offers plus hints --
        # is bounded by the reserved-constant supply, GEAR_SHOP_MAX_ROWS. At 6
        # offers a store carries at most 1 hint row; the world hint budget then
        # seats its remaining rows in stores with more headroom.
        room = GEAR_SHOP_MAX_ROWS - int(ap.get(o, 0))
        n = min(cap, room, fit.get(o, cap))
        if n > 0:
            out[o] = n
    return out


def ph_list(placeholders, o):
    """The (cat, gid) list for ordinal `o`, accepting either placeholder-map
    shape: the single-offer `{o: (cat, gid)}` (static table, legacy slot_data)
    or the parallel-rows `{o: [(cat, gid), ...]}`. One normaliser so every
    consumer below works with both.

    An EMPTY value means no rows, NOT one row: `tuple([])` used to hand back a
    phantom `()` that inject_shop_ap_slots then unpacked into
    "not enough values to unpack (expected 2, got 0)". A store reaches that state
    whenever it is asked for zero offers (a hints-only seed sets pick_counts to 0
    for every store) and its category has no id left for a hint row either."""
    v = placeholders[o]
    if not v:
        return []
    if isinstance(v[0], (tuple, list)):
        return [tuple(t) for t in v]
    return [tuple(v)]


def ap_blocked_by_row(placeholders):
    """{store_row -> set(gid)} of placeholder ids to keep OUT of the shuffle draws,
    for a per-seed placeholder map. Row-global rollup of ap_blocked_by_store (the
    v202 per-store map); used by the all-stores scrub path."""
    out = {0: set(), 1: set(), 2: set()}
    for o in placeholders:
        for _cat, gid in ph_list(placeholders, o):
            out[SHOP_AP_SLOTS[o][1]].add(gid)
    return out


def ap_blocked_by_store(placeholders):
    """{(city, store_row) -> set(gid)}: placeholder ids to keep out of THAT ONE
    store's normal shuffle draws. Since the BUYB purchase mailbox (v202) the
    client attributes purchases by store id, so a placeholder id is a perfectly
    ordinary item everywhere EXCEPT on its own AP store's shelf -- there a
    normal slot with the same id would be indistinguishable from the AP slot."""
    out = {}
    for o in placeholders:
        city, row, _ph = SHOP_AP_SLOTS[o]
        for _cat, gid in ph_list(placeholders, o):
            out.setdefault((city, row), set()).add(gid)
    return out


def _shop_def(block, def_base, idx):
    """(code_offset, size, id_list_offset) of def-record `idx` in `block`."""
    rec = def_base + idx * 8
    code = int.from_bytes(block[rec:rec + 4], "little")
    ptr = int.from_bytes(block[rec + 4:rec + 8], "little")
    size = code & 0xF
    lst = ptr - SHOPS_RAM_BASE
    if (code >> 4) & 0xF in _LIST_MARKER_TYPES:
        lst += 1                                   # skip the 0xfd/fc/fe marker
    return rec, size, lst


def shop_ap_slot_addrs(block, ordinal, n=1):
    """For AP slot `ordinal`, yield (code_offset, size, tail_offsets) for the
    primary AND secondary region, where `tail_offsets` is the store's LAST `n`
    id offsets in list order. Reads the CURRENT slot count from the block, so it
    is correct whether or not the stores were widened by the shuffle.

    The AP rows of a store are always its contiguous TAIL -- that invariant is
    what lets a row be sold out of order without disturbing normal stock."""
    city, row, _ph = SHOP_AP_SLOTS[ordinal]
    idx = _DEF_IDX[(city, row)]
    out = []
    for def_base in _DEF_TABLES:
        rec, size, lst = _shop_def(block, def_base, idx)
        first = lst + max(0, size - n)
        out.append((rec, size, list(range(first, lst + size))))
    return out


def inject_shop_ap_slots(block, ordinals, placeholders=None):
    """Write each AP slot's placeholder ids into the LAST slots of its store
    (primary + secondary region), one row per placeholder. Mutates + returns
    `block`. `placeholders` (ordinal -> (cat, gid) or [(cat, gid), ...])
    overrides the static SHOP_AP_SLOTS gid per seed."""
    ph = placeholders if placeholders is not None else default_placeholders()
    for o in ordinals:
        gids = [g for _cat, g in ph_list(ph, o)]
        for _rec, _size, tail in shop_ap_slot_addrs(block, o, len(gids)):
            for off, gid in zip(tail, gids):
                block[off] = gid
    return block


# Stand-ins used when a store legitimately stocks a placeholder id (see
# scrub_placeholder_stock). Per row: cheap, always-priced vanilla stock.
_SCRUB_SUBS = {
    0: (2, 10, 13),          # weapons: Knife / Dagger / Longsword
    1: (1, 2, 3),            # armor:   Clothes / Wooden Armor / Chain Mail
    2: (1, 11, 17, 4, 16),   # items:   Potion / Antidote / Tent / Ether / Sleeping Bag
}


def scrub_placeholder_stock(block, ordinals, placeholders=None,
                            all_stores=False):
    """Remove placeholder ids from normal (non-AP) store slots in `block`.

    Default (`all_stores=False`, the static-placeholder path): each AP store's
    OWN placeholder only. Since the BUYB purchase mailbox (v202) the client
    attributes purchases by store id, so a placeholder is ordinary stock in
    every other store; but on its own store's shelf a duplicate in a normal
    slot would be indistinguishable from the AP slot. Matters at
    shop_item_pool=unshuffled, where vanilla lists stand unrewritten and e.g.
    Gaia items natively stocks Echo Grass -- its own placeholder.

    `all_stores=True` (the pick_seed_placeholders path): EVERY placeholder id
    is stripped from EVERY store of its row, caravan included -- the authored
    AP name/price follows the gid globally, so a copy on any shelf would
    masquerade as the AP offer ([[placeholder-name-collision]]). With
    post-shuffle picking this is a no-op belt-and-suspenders pass.

    Each offending slot takes the first _SCRUB_SUBS id for its row that the
    store doesn't already stock (falling back to the first sub). Run AFTER
    inject_shop_ap_slots so the AP slots themselves are skipped. Mutates +
    returns `block`."""
    ph = placeholders if placeholders is not None else default_placeholders()
    ap_last = set()
    for o in ordinals:
        n = len(ph_list(ph, o))
        for _rec, _size, tail in shop_ap_slot_addrs(block, o, n):
            ap_last.update(tail)

    def scrub(def_base, idx, row, bad):
        if not bad:
            return
        _rec, size, list_off = _shop_def(block, def_base, idx)
        if size <= 0:
            return
        stock = [list_off + k for k in range(size)]
        for off in stock:
            if off in ap_last or block[off] not in bad:
                continue
            have = {block[p] for p in stock if p != off}
            subs = _SCRUB_SUBS[row]
            sub = next((s for s in subs if s not in have and s not in bad), None)
            if sub is None:
                # Every preferred stand-in is taken or is itself a placeholder.
                # Falling back to subs[0] blindly used to re-inject the very id
                # we are scrubbing (row 2's subs[0] IS Potion, a common pick),
                # which the widened stores hit routinely -- walk the whole id
                # space for this row instead.
                sub = next((s for s in range(1, _MAX_NUM[row] + 1)
                            if s not in have and s not in bad), subs[0])
            block[off] = sub

    if all_stores:
        rowbad = ap_blocked_by_row(ph)
        for def_base in _DEF_TABLES:
            for idx in range(_DEF_COUNT):
                rec = def_base + idx * 8
                typ = (int.from_bytes(block[rec:rec + 4], "little") >> 4) & 0xF
                if typ not in _DEF_MARKER_TYPES:
                    continue
                row = 2 if typ == 4 else typ - 1
                scrub(def_base, idx, row, rowbad[row])
    else:
        reserved = ap_blocked_by_store(ph)
        for (city, row), idx in _DEF_IDX.items():
            if row not in _SCRUB_SUBS:
                continue
            for def_base in _DEF_TABLES:
                scrub(def_base, idx, row, reserved.get((city, row), ()))
    return block


_DEF_COUNT = 40                      # def records per table (0..38 + caravan 39)


def stocked_shop_gids(block):
    """{store_row -> set(gid)} of every id stocked ANYWHERE in `block`: the
    weapon/armor/item stores of all cities plus the caravan + presale (type-4
    defs), both language regions. Sizes are read live from the def records, so
    widened item stores and the caravan's variable width scan fully. Type-4
    (caravan) rows count as consumable stock (row 2)."""
    rows = {0: set(), 1: set(), 2: set()}
    for def_base in _DEF_TABLES:
        for idx in range(_DEF_COUNT):
            rec = def_base + idx * 8
            typ = (int.from_bytes(block[rec:rec + 4], "little") >> 4) & 0xF
            if typ not in _DEF_MARKER_TYPES:
                continue                       # magic stores handled elsewhere
            _rec, size, lst = _shop_def(block, def_base, idx)
            row = 2 if typ == 4 else typ - 1
            for k in range(size):
                v = block[lst + k]
                if v:
                    rows[row].add(v)
    return rows


def pick_seed_placeholders(rng, block, gear_weapons, gear_armor,
                           counts=None, tier=None, block_mana_items=False,
                           hint_caps=None, hint_target=0):
    """Per-seed placeholder map {ordinal: [(cat, gid), ...]}, chosen AFTER the shop
    shuffle from ids the shuffled block stocks NOWHERE (caravan included, both
    regions) -- so an AP slot's authored name/desc/price can never surface on
    another store's shelf ([[placeholder-name-collision]]; the pre-pick
    per-store block let e.g. Crescent Lake's consumable placeholder shuffle
    into Cornelia's item shop wearing the AP offer's name and price).

    `gear_weapons` / `gear_armor` are preference-ordered (cat, gid) candidate
    lists (unselected loot candidates first, low-tier vanilla fallback last);
    the consumable row draws from the full consumable id space, rng-shuffled.
    If a row's every candidate is stocked (never seen in practice: stores fill
    ~22 gear slots against 60+ candidates), the first unused candidate is taken
    anyway and scrub_placeholder_stock(all_stores=True) strips the copies; the
    static SHOP_AP_SLOTS gid is the last-ditch fallback.

    `counts` ({ordinal: n}, default 1 each) asks for n parallel rows per shop --
    each row needs its OWN gid, since price/name/description all hang off the
    item id. `tier` orders the consumable pool INELIGIBLE-FIRST: an id the
    shuffle cannot stock at this tier is free by construction, which turns the
    item stores' supply from luck into arithmetic (see
    consumable_placeholder_capacity). Draw order within each group is untouched,
    so RNG consumption is identical to the single-row build.

    `hint_caps` ({ordinal: max rows}) + `hint_target` (rows wanted seed-wide) run
    a SECOND pass for hint rows, appended after each store's offers. It is
    round-robin over the stores rather than store-by-store, which is what makes
    the count a world-level figure: every store gets its first row before any
    store gets a second, and a store whose category has run out of ids is simply
    skipped, so its rows land in stores that still have supply. Nothing here
    touches `rng` -- the pools were shuffled for the offer pass, and hint rows
    take them in that same order."""
    stocked = stocked_shop_gids(block)
    items = [g for g in range(1, _ITEM_ID_MAX + 1)
             if g not in _ALWAYS_ITEM_BLOCK
             and not (block_mana_items and g in _MANA_ITEM_BLOCK)]
    rng.shuffle(items)
    if tier is not None:
        # Stable partition of the ALREADY-shuffled list: same draws, better
        # order. Ids that are out of tier can never be stocked, so they are the
        # safest placeholders and go first.
        van = _vanilla_shop_ids(bytearray(RD.VANILLA["shops"]))

        def _free(g):
            if tier <= SHOP_TIER_UNSHUFFLED:
                return g not in van[2]
            mt = _shop_id_min_tier(2, g, van, block_mana_items)
            return mt is None or mt > tier

        items = [g for g in items if _free(g)] + [g for g in items if not _free(g)]
    # NEVER_PLACEHOLDER is applied HERE, the one choke point every caller shares
    # (gen builds the gear lists, the consumable list is built above), and AFTER
    # every rng.shuffle so the ban does not shift the draw order of what remains.
    pools = {0: [t for t in gear_weapons if t not in NEVER_PLACEHOLDER],
             1: [t for t in gear_armor if t not in NEVER_PLACEHOLDER],
             2: [(1, g) for g in items if (1, g) not in NEVER_PLACEHOLDER]}
    ph = {}
    used = {0: set(), 1: set(), 2: set()}
    for o, (_city, row, static_ph) in enumerate(SHOP_AP_SLOTS):
        picks = []
        # A MISSING count still means one row (the historical default), but an
        # explicit 0 means none: a hints-only seed asks for zero offer rows, and
        # handing every store a placeholder anyway would spend ids no row uses.
        want = (counts or {}).get(o)
        for _ in range(1 if want is None else max(0, int(want))):
            cand = [t for t in pools[row] if t[1] not in used[row]]
            pick = next((t for t in cand if t[1] not in stocked[row]),
                        cand[0] if cand else static_ph)
            if pick in picks:
                # Pool fully drained -> the static fallback would repeat. Two
                # rows of ONE store sharing a gid is the one thing that breaks
                # purchase attribution outright, so grant fewer rows instead;
                # the caller reconciles off len(ph[o]).
                break
            used[row].add(pick[1])
            picks.append(pick)
        ph[o] = picks

    def draw(row):
        """Next free id for `row`, preferring one no store stocks."""
        cand = [t for t in pools[row] if t[1] not in used[row]]
        if not cand:
            return None
        pick = next((t for t in cand if t[1] not in stocked[row]), cand[0])
        used[row].add(pick[1])
        return pick

    caps = dict(hint_caps or {})
    if caps and hint_target > 0:
        added = {o: 0 for o in caps}
        placed = 0
        while placed < hint_target:
            progress = False
            for o in sorted(caps):
                if placed >= hint_target:
                    break
                if added[o] >= caps[o]:
                    continue
                pick = draw(SHOP_AP_SLOTS[o][1])
                if pick is None:
                    continue            # this category is out; others carry on
                ph.setdefault(o, []).append(pick)
                added[o] += 1
                placed += 1
                progress = True
            if not progress:
                break                   # every store is capped or out of ids
    return ph


def parse_shop_ap_slot_data(slot_data):
    """Read a seed's AP shop stock out of slot_data.

    Returns (rows, base_widths) where rows is {shop: [(cat, gid, price), ...]}
    -- one entry per PARALLEL offer row, in shelf order -- and base_widths is
    {shop: normal stock rows}.

    Two shapes are accepted. `shop_ap_rows` is the current one:
    [[shop, cat, base_width, [[gid, price], ...]], ...]. `shop_ap` is what seeds
    generated before parallel offers carry: [[shop, cat, gid, [prices]], ...],
    a single row whose base width is unknown (empty base_widths), so the client
    falls back to the old delist path for it.

    Pure, so the client's slot_data handling is testable without a live game."""
    rows, base = {}, {}
    for entry in (slot_data.get("shop_ap_rows") or []):
        s, cat, bw, offers = entry
        rows[int(s)] = [(int(cat), int(g), int(p)) for g, p in offers]
        base[int(s)] = int(bw)
    if rows:
        return rows, base
    for entry in (slot_data.get("shop_ap") or []):
        s, cat, gid, prices = entry
        if prices:
            rows[int(s)] = [(int(cat), int(gid), int(prices[0]))]
    return rows, base


def parse_hint_shop_slot_data(slot_data):
    """Read a seed's HINT shop rows out of slot_data.

    Returns (rows, base_widths): rows is {shop: [(cat, gid, price, label,
    [location ids]), ...]} in shelf order -- the rows that sit past the AP
    offers in the same tail -- and base_widths is {shop: normal stock rows}, the
    same figure parse_shop_ap_slot_data reports (one tail, one base width).

    slot_data shape: hint_shop_rows = [[shop, cat, base_width, [[gid, price,
    label, [lids]], ...]], ...]. A seed generated before hint shops simply has
    no key, which reads as no hint rows.

    Pure, so the client's slot_data handling is testable without a live game."""
    rows, base = {}, {}
    for entry in (slot_data.get("hint_shop_rows") or []):
        s, cat, bw, offers = entry
        rows[int(s)] = [(int(cat), int(g), int(p), str(lbl),
                         [int(x) for x in (lids or [])])
                        for g, p, lbl, lids in offers]
        base[int(s)] = int(bw)
    return rows, base


_DELIST_FILLER = {0: 2, 1: 1, 2: 1}      # Knife / Clothes / Potion


def render_shop_ap_tail(block, ordinal, gids, base_width, sold=()):
    """Render a store's AP tail as a pure function of (gids, sold). Mutates and
    returns `block` (the PATCHED shops bytes).

    `base_width` is the store's NORMAL stock row count -- everything before the
    AP tail. The store is sized to `base_width + unsold`, and the unsold
    placeholder ids are written into the rows just past the stock. So selling
    one row shrinks the store by exactly one row, whichever row it was: the
    survivors slide down and normal stock is never touched.

    TOTAL and IDEMPOTENT by construction. It never reads the current slot count
    to decide what to do, which is what makes it safe to re-run on reconnect --
    the old delist_shop_ap_slot shrank RELATIVE to the current width, so a
    second call would have eaten a real stock row.

    Both def tables get the size nibble: the game reads the row count from the
    SECONDARY one (see delist_shop_ap_slot's grouping note), while the ids come
    off the primary pointer. After relayout_shop_lists both point at one shared
    list, so the id writes coincide; at shop_item_pool=unshuffled each region
    still owns its own list and both are written."""
    city, row, _ph = SHOP_AP_SLOTS[ordinal]
    idx = _DEF_IDX[(city, row)]
    sold = set(sold)
    unsold = [g for k, g in enumerate(gids) if k not in sold]
    width = max(1, base_width + len(unsold))
    for def_base in _DEF_TABLES:
        rec = def_base + idx * 8
        code = int.from_bytes(block[rec:rec + 4], "little")
        _r, _size, lst = _shop_def(block, def_base, idx)
        block[rec:rec + 4] = ((code & ~0xF) | (width & 0xF)).to_bytes(4, "little")
        for k, gid in enumerate(unsold):
            off = lst + base_width + k
            if off < lst + width:
                block[off] = gid
        # Clear the rows the shrink just vacated. They are out of the store's
        # range so the game never reads them, but leaving a live placeholder id
        # behind would make stocked_shop_gids and every leak scan report it as
        # shelved stock.
        for off in range(lst + width, lst + base_width + len(gids)):
            block[off] = 0
        if not unsold and base_width == 0:
            # A store whose ONLY row was the AP row cannot shrink to empty
            # (Gaia weapons at width 1). Leave it selling a plain filler.
            block[lst] = _DELIST_FILLER[row]
    return block


def delist_shop_ap_slot(block, ordinal, placeholder=None):
    """Remove a purchased AP slot from its store in `block` (the PATCHED shops
    bytes): shrink the slot count by 1 in the def code word (both regions). A
    1-slot store (Gaia weapons) can't shrink to empty -- its placeholder id is
    swapped for a plain filler instead (Knife/Clothes/Potion by category).
    Idempotent: a slot already delisted is left alone. Client-side helper.
    `placeholder` ((cat, gid)) overrides the static gid for a per-seed map."""
    row = SHOP_AP_SLOTS[ordinal][1]
    gid = placeholder[1] if placeholder is not None else SHOP_AP_SLOTS[ordinal][2][1]
    filler = _DELIST_FILLER[row]                   # Knife / Clothes / Potion
    addrs = [(rec, size, tail[-1])
             for rec, size, tail in shop_ap_slot_addrs(block, ordinal)]
    # Since relayout_shop_lists both def tables usually point at ONE shared list,
    # so the two records report the SAME `last` byte. Group by it: the old
    # per-record loop cleared the id on the primary pass and then read `!= gid`
    # on the secondary pass, skipping it -- which left the SECONDARY size nibble
    # unshrunk, and that is the nibble the game reads for the row count (the
    # slot stayed on the shelf). Blocks that were never relayouted
    # (shop_item_pool=unshuffled) still have per-region lists and take the same
    # path safely, one group each.
    for last in dict.fromkeys(a[2] for a in addrs):
        group = [a for a in addrs if a[2] == last]
        if block[last] != gid:
            continue                               # already delisted / rewritten
        if all(size > 1 for _rec, size, _l in group):
            for rec, size, _l in group:
                code = int.from_bytes(block[rec:rec + 4], "little")
                block[rec:rec + 4] = ((code & ~0xF) | (size - 1)).to_bytes(4, "little")
            block[last] = 0
        else:
            block[last] = filler                   # 1-slot store can't shrink
    return block


def force_shop_ap_prices(prices, weapons, armor, item_buy, placeholders=None):
    """Overwrite each AP placeholder's buy price with its rolled slot price and
    zero its sell price (nothing to sell -- the client removes the item). Runs
    AFTER every price shuffle so nothing re-randomizes it. `prices` maps
    ordinal -> gil. `placeholders` (ordinal -> (cat, gid)) overrides the static
    SHOP_AP_SLOTS gid per seed."""
    ph = placeholders if placeholders is not None else default_placeholders()
    for o, gil in prices.items():
        # Parallel rows each carry their OWN price, because the game reads a
        # price off the ITEM record, not the shop row (see _STOCK_PRICE). Baking
        # every row's price here is what retires the client's between-sales
        # reprice: nothing is ever rewritten at runtime.
        gils = list(gil) if isinstance(gil, (list, tuple)) else [gil]
        for (cat, gid), g in zip(ph_list(ph, o), gils):
            if cat == 2:
                rec = (gid - 1) * 28
                _write_u24(weapons, rec + 20, g)
                _write_u24(weapons, rec + 24, 0)
                weapons[rec + 2] = weapons[rec + 3] = 0   # no class can equip
            elif cat == 3:
                rec = (gid - 1) * 28
                _write_u24(armor, rec + 20, g)
                _write_u24(armor, rec + 24, 0)
                armor[rec + 2] = armor[rec + 3] = 0       # no class can equip
            else:
                rec = (gid - 1) * 16
                _write_u24(item_buy, rec, g)
                _write_u24(item_buy, rec + 4, 0)


# weapon/armor AP placeholder (cat, gid) pairs -- the slots whose equip masks
# force_shop_ap_prices zeroes (consumable slots have no masks).
SHOP_AP_EQUIP_GIDS = frozenset(
    (cat, gid) for _c, _row, (cat, gid) in SHOP_AP_SLOTS if cat in (2, 3))


def set_shop_ap_masks(weapons, armor, restore=frozenset(), equip_gids=None):
    """Set each weapon/armor AP placeholder's equip masks (+2/+3): the VANILLA
    masks for (cat, gid) pairs in `restore`, zero for the rest. Zero = the
    while-buyable dupe guard (the client strips a purchased placeholder from
    the inventory, but an EQUIPPED copy lives in the party record and would
    survive the strip). Restore = the placeholder id doubles as a real vanilla
    item that bonus dungeons drop natively ([[placeholder-name-collision]]);
    those copies must be equippable. Mutates weapons/armor; returns True if
    anything changed. Client-side helper (_shop_sync_masks); the bake always
    zeroes via force_shop_ap_prices. `equip_gids` (a {(cat, gid)} set) overrides
    the static SHOP_AP_EQUIP_GIDS for a per-seed placeholder map."""
    V = RD.VANILLA
    changed = False
    for cat, gid in (equip_gids if equip_gids is not None else SHOP_AP_EQUIP_GIDS):
        blk, van = (weapons, V["weapons"]) if cat == 2 else (armor, V["armor"])
        rec = (gid - 1) * 28
        e1, e2 = ((van[rec + 2], van[rec + 3]) if (cat, gid) in restore
                  else (0, 0))
        if blk[rec + 2] != e1 or blk[rec + 3] != e2:
            blk[rec + 2], blk[rec + 3] = e1, e2
            changed = True
    return changed


# magic store cells (j, i) for white1/white2/black1/black2 that actually exist.
_MAGIC_STORE_CELLS = [(j, i) for j in (3, 4, 5, 6) for i in range(9)
                      if _STORES[j][i] != -1]
# (_SEC_DELTA, the secondary-region offset, is defined with the shop-width
# constants above -- the magic stores keep their per-region copies.)

# color -> (store rows, first 1-based shop spell id). White spells are magic
# indexes 0..31 (shop ids 1..0x20), black 32..63 (shop ids 0x21..0x40).
_MAGIC_COLORS = {"white": ((3, 4), 1), "black": ((5, 6), 0x21)}


# store-city index -> which magic rows are white vs black (rows 3,4 white; 5,6 black)
_WHITE_ROWS = (3, 4)
_BLACK_ROWS = (5, 6)


def magic_shops_for_city(shops, magic_info, city):
    """Enumerate the white + black spells a town's magic shops sell, for the Shops
    GUI tab. Pure: takes the effective `shops` and `magic_info` byte blocks (patched
    from slot_data, or RD.VANILLA when a table wasn't shuffled) and a store-city
    index (0 Cornelia .. 6 Gaia, 7 Lufenia).

    Returns {"white": [entry, ...], "black": [...]} where each entry is
    {"name","level","price","index"}. Level is the spell's magic_info+9 -- which
    align_shop_spell_levels has set to the tier of its store, so e.g. everything a
    town's tier-1 shop sells reads level 1. Price is the u16 at magic_info+12.
    Ordered by the store rows, then slot within a row.
    """
    from .spell_data import SPELL_NAMES

    def read_row(j):
        off = _STORES[j][city]
        if off == -1:
            return []
        base = _CITY_STARTS[city] + off
        out = []
        for k in range(_STORE_SIZES[j][city]):
            sid = shops[base + k]
            if sid == 0:
                continue
            idx = sid - 1                       # magic-index 0..63
            rec = idx * 14
            level = magic_info[rec + 9]
            price = magic_info[rec + 12] | (magic_info[rec + 13] << 8)
            name = SPELL_NAMES[idx] if 0 <= idx < len(SPELL_NAMES) else f"Spell {sid}"
            out.append({"name": name, "level": level, "price": price, "index": idx})
        return out

    white, black = [], []
    for j in _WHITE_ROWS:
        white += read_row(j)
    for j in _BLACK_ROWS:
        black += read_row(j)
    return {"white": white, "black": black}


# store_row -> (buy-price table, record stride, buy-price offset in the record).
# There is no per-shop price anywhere: a shop list byte is just a 1-based id in
# the store's own category, and the game reads the price off the item's record.
_STOCK_PRICE = {
    0: ("weapons", 28, 20),
    1: ("armor", 28, 20),
    2: ("item_buy_prices", 16, 0),
}


def shop_stock_for_city(shops, city, prices=None, ap_ordinals=(),
                        caravan=False):
    """Enumerate the NATIVE stock of a town's weapon/armor/item stores, for the
    Shops GUI tab. The magic-shop counterpart is magic_shops_for_city.

    Pure: takes the effective `shops` block, a store-city index (0 Cornelia ..
    6 Gaia; 7 Lufenia has no item stores) and `prices`, a dict of the effective
    {"weapons", "armor", "item_buy_prices"} blocks (omit for gid-only output).

    Returns {store_row: [{"gid", "price", "slot"}, ...]} for the rows this city
    has -- Melmond has no item shop, Onrac no weapon/armor shop. `caravan=True`
    adds the Onrac desert caravan (a type-4 store, priced as consumables) under
    key "caravan".

    `ap_ordinals` are the SHOP_AP_SLOTS ordinals this seed injected placeholders
    into; their shelves are the store's LAST rows and are dropped here, since the
    GUI renders those as scouted AP offers instead. Pass a {ordinal: rows}
    mapping when a shop carries several parallel offers -- a bare iterable means
    one row each. Count the rows STILL LISTED, not the rows the seed started
    with: a sold row is gone from the block, so over-counting here would hide a
    real stock item. Sizes come from the def records, so widened stores
    (relayout_shop_lists) scan fully -- never use the _CITY_STARTS/_STORES static
    offsets for rows 0/1/2.
    """
    prices = prices or {}
    ap_last = set()
    for o in ap_ordinals:
        n = ap_ordinals[o] if isinstance(ap_ordinals, dict) else 1
        for _rec, _size, tail in shop_ap_slot_addrs(shops, o, n):
            ap_last.update(tail)

    def read_store(idx, row):
        _rec, size, lst = _shop_def(shops, _DEF_TABLES[0], idx)
        tbl_name, stride, poff = _STOCK_PRICE[row]
        tbl = prices.get(tbl_name)
        out = []
        for k in range(size):
            off = lst + k
            gid = shops[off] if off < len(shops) else 0
            if not gid or off in ap_last:
                continue                       # padding, or the AP shelf
            price = None
            if tbl:
                prec = (gid - 1) * stride + poff
                if 0 <= prec + 2 < len(tbl):
                    price = _read_u24(tbl, prec)
            out.append({"gid": gid, "price": price, "slot": k})
        return out

    out = {}
    for row in (0, 1, 2):
        idx = _DEF_IDX.get((city, row))
        if idx is not None:
            out[row] = read_store(idx, row)
    if caravan:
        out["caravan"] = read_store(_CARAVAN_DEF_IDX, 2)
    return out


def _magic_slots(color):
    """Ordered list of primary-region slot offsets for every magic-shop slot of
    `color` (32 slots per color in vanilla -- one per spell)."""
    rows, _ = _MAGIC_COLORS[color]
    slots = []
    for (j, i) in _MAGIC_STORE_CELLS:
        if j not in rows:
            continue
        off = _CITY_STARTS[i] + _STORES[j][i]
        slots.extend(off + k for k in range(_STORE_SIZES[j][i]))
    return slots


def _early_magic_slots(color):
    """Slot offsets of this color's Cornelia+Pravoka magic stores -- the two
    lowest-tier stores (tier <= 2, i.e. the level-1 and level-2 shops), derived
    from ISO data via _magic_store_tiers (no hardcoded city ids)."""
    rows, _ = _MAGIC_COLORS[color]
    tiers = _magic_store_tiers()
    slots = []
    for (j, i) in _MAGIC_STORE_CELLS:
        if j not in rows or tiers.get((j, i), 99) > 2:
            continue
        off = _CITY_STARTS[i] + _STORES[j][i]
        slots.extend(off + k for k in range(_STORE_SIZES[j][i]))
    return slots


def _low_level_spell_ids(color):
    """1-based shop ids of this color's spells whose vanilla level (+9 in
    magic_info) is 1 or 2."""
    _rows, base_id = _MAGIC_COLORS[color]
    vmagic = RD.VANILLA["magic_info"]
    return {base_id + n for n in range(32)
            if vmagic[(base_id - 1 + n) * 14 + 9] in (1, 2)}


def shuffle_magic_shops(rng, block):
    """Randomize magic store inventories as a PERMUTATION: the 32 spells of each
    color are dealt across that color's 32 shop slots, so any shop can sell any
    spell of its color (a Cornelia white shop selling Cure/Holy/Lifaga/Exit) but
    every spell is still sold exactly once -- which keeps per-shop learn counts
    well-defined (see rebuild_magic_learn) and no spell unobtainable. The
    secondary (2nd-language) region mirrors the primary exactly.

    Constraint: each color keeps at least one level-1-or-2 spell somewhere in
    the Cornelia+Pravoka shops (the two earliest stores) so a fresh party can
    always buy a low-level spell of each color early."""
    out = bytearray(block)
    for color, (_rows, base_id) in _MAGIC_COLORS.items():
        slots = _magic_slots(color)
        ids = [base_id + n for n in range(32)]
        rng.shuffle(ids)
        early = set(_early_magic_slots(color))
        low = _low_level_spell_ids(color)
        if early and low:
            early_k = [k for k, off in enumerate(slots) if off in early]
            if not any(ids[k] in low for k in early_k):
                # no low-level spell landed in the early stores -- swap one in
                e = rng.choice(early_k)
                donors = [k for k in range(len(ids)) if ids[k] in low]
                f = rng.choice(donors)
                ids[e], ids[f] = ids[f], ids[e]
        for off, sid in zip(slots, ids):
            out[off] = sid
            out[off + _SEC_DELTA] = sid
    return out


# A costly spell draws its shop slot with probability proportional to
# (tier - 1), so level 1 is impossible by construction and level 8 is the mode.
# Uncrowded per-level odds, tiers 2..8: 3.6 / 7.1 / 10.7 / 14.3 / 17.9 / 21.4 /
# 25.0 % -- mean level 6.0, vs 12.5% flat for an unsteered spell.
def _costly_slot_weight(tier):
    return tier - 1


def _color_slot_tiers(color):
    """{slot offset -> store tier} for every magic-shop slot of `color`, in
    shop-cell order. A spell's LEVEL is the tier of the store it lands in (see
    align_shop_spell_levels), so this map is the level board the costly-spell
    pass plays on. Four slots per tier per color, tiers 1..8."""
    rows, _base_id = _MAGIC_COLORS[color]
    tiers = _magic_store_tiers()
    slot_tier = {}
    for (j, i) in _MAGIC_STORE_CELLS:
        if j not in rows:
            continue
        tier = tiers.get((j, i))
        if tier is None:
            continue
        off = _CITY_STARTS[i] + _STORES[j][i]
        for k in range(_STORE_SIZES[j][i]):
            slot_tier[off + k] = tier
    return slot_tier


def force_costly_spell_levels(rng, shops_block, costly):
    """Slot-magic pass: re-deal each color's magic shops so the player's `costly`
    spells (see resolve_costly_spells) land in HIGH spell levels and never in
    level 1.

    A spell's level is the tier of the store selling it, so this is an
    assignment, not a bump ladder: every costly spell draws a free slot with
    probability proportional to _costly_slot_weight(tier) over tiers 2..8, then
    the ordinary spells deal uniformly into whatever slots are left. Result is
    still a permutation (each spell sold exactly once) with every store's stock
    size intact, and the secondary-language region mirrors the primary.

    Placement order is vanilla spell level DESCENDING with a RANDOM tiebreak
    inside a level. Order only matters once the upper tiers get crowded -- with a
    handful of costly spells every one of them sees the clean weight table -- so
    the ordering is what keeps a big list roughly vanilla-shaped instead of
    first-come-first-served. The tiebreak must stay random: a class learns only a
    subset of each shop's stock, so a fixed order would make the same class learn
    the same spell every seed.

    Overflow (more costly spells in a color than the 28 tier-2+ slots) is drawn
    uniformly from the WHOLE costly list, not from the tail of the order -- so at
    full list size any costly spell can be the one that eats a level-1 slot.

    Only meaningful under slot_magic (level = which charge pool a cast draws
    from) and only when the magic shops were shuffled; build_shuffle_tables gates
    it on both. Runs BEFORE rebuild_magic_learn/align_shop_spell_levels so both
    see the final layout.

    Because this re-deals every slot, shuffle_magic_shops' "a vanilla level-1-or-2
    spell stays buyable in Cornelia/Pravoka" guarantee does not survive on its
    own and is re-applied at the end here, preferring a NON-costly donor so a
    costly spell is never forced down into an early store."""
    out = bytearray(shops_block)
    order_all = resolve_costly_spells(costly)
    if not order_all:
        return out
    vmagic = RD.VANILLA["magic_info"]
    for color, (_rows, base_id) in _MAGIC_COLORS.items():
        slot_tier = _color_slot_tiers(color)
        costly_here = sorted(idx + 1 for idx in order_all   # magic index -> shop id
                             if base_id <= idx + 1 < base_id + 32)
        if not costly_here:
            continue
        costly_ids = set(costly_here)
        upper = sorted(off for off, t in slot_tier.items() if t >= 2)
        lowest = sorted(off for off, t in slot_tier.items() if t < 2)

        n_sink = max(0, len(costly_here) - len(upper))
        sunk = set(rng.sample(costly_here, n_sink)) if n_sink else set()

        place = {}
        free_low = lowest[:]
        rng.shuffle(free_low)
        for sid in sorted(sunk):
            place[sid] = free_low.pop()

        order = [sid for sid in costly_here if sid not in sunk]
        rng.shuffle(order)                                  # tiebreak inside a level
        order.sort(key=lambda sid: -vmagic[(sid - 1) * 14 + 9])
        free_up = upper[:]
        for sid in order:
            weights = [_costly_slot_weight(slot_tier[off]) for off in free_up]
            pick = rng.choices(free_up, weights=weights)[0]
            free_up.remove(pick)
            place[sid] = pick

        rest = [base_id + n for n in range(32) if base_id + n not in costly_ids]
        leftovers = free_up + free_low
        rng.shuffle(rest)
        rng.shuffle(leftovers)
        for sid, off in zip(rest, leftovers):
            place[sid] = off

        for sid, off in place.items():
            out[off] = sid
            out[off + _SEC_DELTA] = sid

        early = [off for off in _early_magic_slots(color) if off in slot_tier]
        low = _low_level_spell_ids(color)
        if early and low and not any(out[off] in low for off in early):
            # nothing vanilla-low landed early: swap one in. Non-costly donors
            # first; only if EVERY vanilla-low spell is costly does a costly one
            # get pulled down.
            donors = sorted(low - costly_ids) or sorted(low)
            sid = rng.choice(donors)
            dest = rng.choice(early)
            src, other = place[sid], out[dest]
            out[src], out[dest] = other, sid
            out[src + _SEC_DELTA], out[dest + _SEC_DELTA] = other, sid
    return out


def rebuild_magic_learn(rng, shops_block):
    """Rebuild the magic_learn class bitmasks so each class learns the same
    NUMBER of spells from each magic shop as it did in vanilla, re-drawn from
    the spells that shop sells now. E.g. Cornelia's white shop: white mage
    still learns all 4 of whatever it sells, red mage learns 2 of them.

    Promoted classes are forced to be supersets of their base class per shop
    (red wizard's picks include red mage's picks plus vanilla-count extras), so
    class change never loses learn access. White/black wizards learn all 4 per
    shop in vanilla anyway, so the constraint only bites for red mage/wizard.

    The five per-color caster bits are the ONLY learn bits vanilla ever sets
    (white = white_mage/red_mage/white_wizard/red_wizard/knight; black =
    black_mage/red_mage/black_wizard/red_wizard/ninja -- Knight=bit8/job6 and
    Ninja=bit9/job7 are the hybrids), so rewriting exactly those rebuilds every
    class's per-shop learn count. Base non-casters (Warrior/Thief/Monk, bits
    0/1/2) learn nothing here; the dabble feature ORs its own bits on afterward
    (see client.iso_patcher.apply_dabble_learn_overlay)."""
    vshops = RD.VANILLA["shops"]
    vlearn = RD.VANILLA["magic_learn"]
    out = bytearray(vlearn)
    for color, class_bits in (("white", _WHITE_CLASS_BITS),
                              ("black", _BLACK_CLASS_BITS)):
        rows, base_id = _MAGIC_COLORS[color]
        # clear the known class bits on every spell of this color
        for idx in range(base_id - 1, base_id - 1 + 32):
            for (byte_i, mask) in class_bits.values():
                out[idx * 2 + byte_i] &= ~mask & 0xFF

        def learned(idx, cls):
            byte_i, mask = class_bits[cls]
            return bool(vlearn[idx * 2 + byte_i] & mask)

        def set_bit(idx, cls):
            byte_i, mask = class_bits[cls]
            out[idx * 2 + byte_i] |= mask

        for (j, i) in _MAGIC_STORE_CELLS:
            if j not in rows:
                continue
            off = _CITY_STARTS[i] + _STORES[j][i]
            size = _STORE_SIZES[j][i]
            van_idxs = [vshops[off + k] - 1 for k in range(size)]
            new_idxs = [shops_block[off + k] - 1 for k in range(size)]
            counts = {cls: sum(1 for idx in van_idxs if learned(idx, cls))
                      for cls in class_bits}
            done = set()
            for base_cls, promo_cls in _PROMOTION_PAIRS.items():
                if base_cls not in class_bits:
                    continue
                a, b = counts[base_cls], counts[promo_cls]
                base_pick = rng.sample(new_idxs, min(a, len(new_idxs)))
                rest = [idx for idx in new_idxs if idx not in base_pick]
                promo_pick = base_pick + rng.sample(rest, min(max(0, b - a), len(rest)))
                for idx in base_pick:
                    set_bit(idx, base_cls)
                for idx in promo_pick:
                    set_bit(idx, promo_cls)
                done.update((base_cls, promo_cls))
            for cls in class_bits:
                if cls in done:
                    continue
                for idx in rng.sample(new_idxs, min(counts[cls], len(new_idxs))):
                    set_bit(idx, cls)
    return out


def _magic_store_tiers():
    """For each magic store cell, its intended spell 'level' tier = the minimum
    vanilla spell level (+9 in magic_info) among the spells that store sold in
    vanilla. Cornelia -> 1, Pravoka -> 2, ... derived from ISO data, no magic
    numbers. Returns {(j, i): tier}."""
    vshops = RD.VANILLA["shops"]
    vmagic = RD.VANILLA["magic_info"]
    tiers = {}
    for (j, i) in _MAGIC_STORE_CELLS:
        off = _CITY_STARTS[i] + _STORES[j][i]
        size = _STORE_SIZES[j][i]
        lvls = []
        for k in range(size):
            sid = vshops[off + k]                 # shop spell id is 1-based
            idx = sid - 1
            if 0 <= idx < 64:
                lvls.append(vmagic[idx * 14 + 9])
        if lvls:
            tiers[(j, i)] = min(lvls)
    return tiers


def align_shop_spell_levels(shops_block, magic_info):
    """After a shop shuffle, rewrite each spell's displayed/functional level
    (+9 in its 14-byte magic_info record) so it matches the shop it now sells
    in: a spell takes the tier of the LOWEST-tier magic store that stocks it
    (so a Thundaga dropped into Cornelia's shop becomes Level 1, and shows
    Level 1 everywhere -- one spell can only have one stored level). Spells not
    sold in any magic store keep their vanilla level. Reads the shuffled ids
    from the primary shop region (`shops_block`)."""
    out = bytearray(magic_info)
    tiers = _magic_store_tiers()
    target = {}   # spell magic-index -> lowest tier it is sold at
    for (j, i), tier in tiers.items():
        off = _CITY_STARTS[i] + _STORES[j][i]
        size = _STORE_SIZES[j][i]
        for k in range(size):
            sid = shops_block[off + k]            # 1-based shop id
            idx = sid - 1
            if 0 <= idx < 64:
                target[idx] = min(target.get(idx, 99), tier)
    for idx, lvl in target.items():
        out[idx * 14 + 9] = lvl
    return out


# ---- spell-tome learnability (which spells the party can EVER learn) ----------
def can_learn_bit(job):
    """The magic_learn u16 bit index the game's can_learn(job, spell) leaf tests
    (see client.iso_patcher._tome_validity_handler): (job % 6) + (job // 6) * 8 --
    base jobs 0..5 use bits 0..5, their promoted forms (job + 6) use bits 8..13.
    Verified against the vanilla table (Warrior/Thief/Monk learn nothing;
    Knight=job6=bit8 white lv1-3; Ninja=job7=bit9 black lv1-4; RedWizard=bit11;
    White/BlackWizard=bit12/13 = all of their color) -- matches canonical FF1."""
    return (job % 6) + (job // 6) * 8


def learnable_spells(magic_learn, classes):
    """The set of spell indices (0..63) that at least one class byte in `classes`
    can learn, per the FINAL (shuffle- + dabble-resolved) magic_learn bitfield.

    The magiclv half of the game's teach gate (magiclv >= the spell's aligned
    level) is redundant with a set learn bit once the shop shuffle realigns each
    spell to its store's tier and rebuilds per-store learn COUNTS: a set bit always
    implies the class's magiclv reaches that spell's final level (holds for the
    rebuilt casters + Ninja, for Knight's vanilla white 1-3, and for the low-level
    Monk/Thief/Master dabble sets). So this single bit test is exact. See
    FF1PSPWorld._learnable_tome_names."""
    out = set()
    for s in range(len(magic_learn) // 2):
        rec = magic_learn[s * 2] | (magic_learn[s * 2 + 1] << 8)
        if any(rec & (1 << can_learn_bit(j)) for j in classes):
            out.add(s)
    return out


# Gear equip masks live at +2/+3 of the 28-byte weapon/armor record, as 6-bit
# job masks. BIT INDEX == BASE JOB ID (0 Warrior .. 5 BlackMage): a bit in
# equip1 means "this base job AND its promotion may equip", while the bits only
# in equip2 (equip2 & ~equip1) are PROMOTED-ONLY -- see shuffle_who_equips_what
# and apply_promoted_only_equip. Promoted job ids are base+6, so `job % 6` is
# always the mask bit.
_GEAR_STRIDE = 28
_EQUIP_MASK = 0x3F
_PROMO_MIN = 6              # ff1_data.PROMOTED_JOB_MIN, without the client import


def gear_is_activatable(block, gid):
    """True if this record casts a free spell when USED as a battle item (the
    spell-proc byte at +7 is nonzero). Reads the EFFECTIVE block rather than the
    vanilla table the module-level _ACTIVATABLE_*_IDS sets are built from; the
    two always agree, because no shuffle touches +7 (asserted in test_rando)."""
    rec = (gid - 1) * _GEAR_STRIDE
    if gid <= 0 or rec + 8 > len(block):
        return False
    return block[rec + 7] != 0


def gear_sell_value(block, gid):
    """What a shop pays for this piece of gear: the BUY price (u24 LE @+20)
    halved, exactly the rule shuffle_equip_prices writes into +24.

    Deliberately recomputed instead of read from +24: apply_power_price_
    multipliers bumps the buy price for rarity and leaves the stored sell price
    alone, so +24 goes stale on any seed with shop items on. Buy is capped at
    _PRICE_CAP_ITEM, so this can never exceed 49999."""
    rec = (gid - 1) * _GEAR_STRIDE
    if gid <= 0 or rec + 23 > len(block):
        return 0
    return (block[rec + 20] | (block[rec + 21] << 8)
            | (block[rec + 22] << 16)) // 2


def usability_jobs(jobs):
    """The job ids to judge "can anybody here use this?" against.

    A None slot is a choose-at-game-start member whose class nobody knows yet, so
    it could turn out to be ANY base job -- the permissive reading (every base
    job is in the party) is the only safe one when the answer decides whether an
    item is destroyed. One unknown slot therefore suppresses auto-selling for the
    whole seed, which is the intended degradation.

    Note this is the OPPOSITE of gear_equip_state's own None handling: shading a
    shop row wrongly costs nothing, selling an item wrongly costs the item."""
    jobs = list(jobs or [])
    if not jobs or any(j is None for j in jobs):
        return list(range(_PROMO_MIN))
    return jobs


def gear_auto_sell_value(block, gid, jobs):
    """Gil this piece of gear is worth auto-selling for, or 0 to KEEP it
    (auto_sell_unusable_items yaml). `jobs` must already be through
    usability_jobs.

    Sells only on a flat "never": gear no member can equip today and no member's
    promotion will ever unlock. Activatable gear is always kept -- a Braveheart
    nobody can wield still casts Confuse from the battle item menu.

    An ALL-ZERO equip mask is never a sale. Real gear always lists somebody;
    0/0 is the signature of set_shop_ap_masks' buy-dupe guard, which blanks an
    AP placeholder's masks (and rewrites its price) until the client restores
    them. The caller already excludes this seed's placeholder ids -- this is the
    backstop for any other id the bake blanks the same way."""
    if gear_is_activatable(block, gid):
        return 0
    rec = (gid - 1) * _GEAR_STRIDE
    if gid <= 0 or rec + 4 > len(block):
        return 0
    if not (block[rec + 2] & _EQUIP_MASK) and not (block[rec + 3] & _EQUIP_MASK):
        return 0
    if gear_equip_state(block, gid, jobs) != "never":
        return 0
    return gear_sell_value(block, gid)


def gear_equip_state(block, gid, jobs, rune_ok=True):
    """Can this party use that piece of gear? "now" / "later" / "never", for the
    Shops tab's usability shading.

    `block` is the effective weapons or armor table, `gid` the 1-based id, and
    `jobs` the party's CURRENT class ids (0..5 base, 6..11 promoted; None = a
    slot whose class is unknown, which is skipped rather than guessed).

    "later" means no current class can equip it but a base-class member's
    PROMOTION could -- the class-change gate, not a dead end. With `jobs` full of
    base ids (the offline slot_data fallback) the answer stays correct: a member
    who is already promoted can only ever widen the result to "now".

    ACTIVATABLE gear (spell-on-use, +7 nonzero) is never a dead end either: it is
    usable as a battle item by anyone, so it never returns "never". `rune_ok` is
    the equipment_runes gate -- False means the gate is on and the Rune Key is
    not assembled yet, so activation is greyed out for now => "later". The True
    default is "no gate", which is what every pre-rune caller means."""
    rec = (gid - 1) * _GEAR_STRIDE
    if gid <= 0 or rec + 4 > len(block):
        return "never"
    e1 = block[rec + 2] & _EQUIP_MASK
    e2 = block[rec + 3] & _EQUIP_MASK
    now = later = False
    for j in jobs:
        if j is None:
            continue
        bit = 1 << (j % _PROMO_MIN)
        if j >= _PROMO_MIN:
            if (e1 | e2) & bit:         # promoted: both halves apply
                now = True
        elif e1 & bit:
            now = True
        elif e2 & bit:
            later = True                # unlocks at the class change
    if not now and gear_is_activatable(block, gid):
        return "now" if rune_ok else "later"
    return "now" if now else ("later" if later else "never")


def spell_learn_state(magic_learn, magic_info, idx, jobs, magic_levels=None):
    """Can this party learn spell `idx` (0..63)? "now" / "later" / "never".

    Mirrors the game's own teach gate (iso_patcher._tome_validity_handler): the
    class's learn bit, then magiclv >= the spell's aligned level. `magic_levels`
    is the party's live P_MAGICLV per member; pass None (or None entries) when
    the game is not attached -- an unknown magic level is treated as high enough,
    so nothing is faded on a guess. A base-class member whose PROMOTION carries
    the learn bit reads "later"."""
    rec = magic_learn[idx * 2] | (magic_learn[idx * 2 + 1] << 8)
    level = magic_info[idx * 14 + 9]
    now = later = False
    for k, j in enumerate(jobs):
        if j is None:
            continue
        if rec & (1 << can_learn_bit(j)):
            lv = None if magic_levels is None else magic_levels[k]
            if lv is None or lv >= level:
                now = True
            else:
                later = True            # right class, magic level too low yet
        elif j < _PROMO_MIN and rec & (1 << can_learn_bit(j + _PROMO_MIN)):
            later = True                # unlocks at the class change
    return "now" if now else ("later" if later else "never")


def _write_u24(buf, off, val):
    buf[off] = val & 0xFF
    buf[off + 1] = (val >> 8) & 0xFF
    buf[off + 2] = (val >> 16) & 0xFF


def _read_u24(buf, off):
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16)


def apply_power_price_multipliers(weapons, armor, item_buy, tier):
    """Scale buy prices of the strongest stock UP after every price shuffle, to
    push them toward the gil ceiling: priceless gear/consumables x2, activatable
    (spell-on-use) weapons x1.5. Priceless wins, so each id is scaled once. Only
    scales ids that are actually purchasable at `tier` (activatable needs
    SHOP_TIER_ACTIVATABLE, priceless needs SHOP_TIER_ALL), so a low-tier seed
    leaves off-pool prices vanilla. The x2/x1.5 kickers stay tier-gated; the
    BASE prices (apply_priceless_base_prices) do not, so caravan step-up stock
    is never free. Buy price only (sell untouched); capped at
    _PRICE_CAP_ITEM (the 5-digit shop-display width -- a higher value wraps in
    the buy menu, e.g. 104032 rendered as "04032"). Mutates the bytearrays."""
    def scale(buf, off, factor):
        _write_u24(buf, off, min(int(_read_u24(buf, off) * factor), _PRICE_CAP_ITEM))

    priceless_on = tier >= SHOP_TIER_ALL
    activatable_on = tier >= SHOP_TIER_ACTIVATABLE
    for wid in range(1, 68):
        if wid in _PRICELESS_WEAPON_IDS:
            if priceless_on:
                scale(weapons, (wid - 1) * 28 + 20, _PRICELESS_PRICE_MULT)
        elif wid in _ACTIVATABLE_WEAPON_IDS and activatable_on:
            scale(weapons, (wid - 1) * 28 + 20, _ACTIVATABLE_PRICE_MULT)
    for aid in range(1, 76):
        if aid in _PRICELESS_ARMOR_IDS:
            if priceless_on:
                scale(armor, (aid - 1) * 28 + 20, _PRICELESS_PRICE_MULT)
        elif aid in _ACTIVATABLE_ARMOR_IDS and activatable_on:
            scale(armor, (aid - 1) * 28 + 20, _ACTIVATABLE_PRICE_MULT)
    if priceless_on:
        for iid in _PRICELESS_ITEM_PRICES:
            scale(item_buy, (iid - 1) * 16, _PRICELESS_PRICE_MULT)
    return weapons, armor, item_buy


def apply_priceless_base_prices(weapons, armor, item_buy_prices):
    """Give normally-priceless (0-gil / placeholder-priced) shop items a real
    base buy price (sell = buy//2). Mutates + returns the three bytearrays.
    Call at ANY shuffled shop tier (shop eligibility is gated elsewhere; a
    priced record the seed never stocks costs nothing), and BEFORE price
    randomization so a random-prices roll scales the new base instead of the
    0 it would otherwise keep. Only touches the genuinely 0-gil weapon
    ultimates (_PRICELESS_WEAPON_ZERO_PRICE_IDS) and placeholder-priced armor
    (_PRICELESS_ARMOR_PRICES, e.g. Ribbon's vanilla 2 gil) -- the other
    _PRICELESS_WEAPON_IDS / _PRICELESS_ARMOR_IDS entries already carry a real
    vanilla price and keep it untouched; only their shop-eligibility is gated."""
    for wid in _PRICELESS_WEAPON_ZERO_PRICE_IDS:         # 28-byte weapon records
        rec = (wid - 1) * 28
        _write_u24(weapons, rec + 20, _PRICELESS_WEAPON_PRICE)
        _write_u24(weapons, rec + 24, _PRICELESS_WEAPON_PRICE // 2)
    for aid, price in _PRICELESS_ARMOR_PRICES.items():   # 28-byte armor records
        rec = (aid - 1) * 28
        _write_u24(armor, rec + 20, price)
        _write_u24(armor, rec + 24, price // 2)
    for iid, price in _PRICELESS_ITEM_PRICES.items():    # 16-byte item recs, buy @+0
        _write_u24(item_buy_prices, (iid - 1) * 16, price)
        _write_u24(item_buy_prices, (iid - 1) * 16 + 4, price // 2)   # sell @+4
    return weapons, armor, item_buy_prices


def apply_always_priced_items(item_buy_prices, weapons=None):
    """Give the always-priced consumables (_ALWAYS_PRICED_ITEMS activatable-tier
    set + _EXOTIC_PRICED_ITEMS exotic-tier set) and the always-priced 0-gil
    weapons (_ALWAYS_PRICED_WEAPONS, e.g. Murasame) a real base buy price
    unconditionally -- unlike apply_priceless_base_prices, call this any time
    shop items are shuffled, at any tier. Mutates + returns item_buy_prices."""
    for iid, price in {**_ALWAYS_PRICED_ITEMS, **_EXOTIC_PRICED_ITEMS}.items():
        _write_u24(item_buy_prices, (iid - 1) * 16, price)
        _write_u24(item_buy_prices, (iid - 1) * 16 + 4, price // 2)   # sell @+4
    if weapons is not None:
        for wid, price in _ALWAYS_PRICED_WEAPONS.items():             # 28-byte recs
            _write_u24(weapons, (wid - 1) * 28 + 20, price)
            _write_u24(weapons, (wid - 1) * 28 + 24, price // 2)
    return item_buy_prices


# ---------------------------------------------------------------- assembler
# slot_data key holding the shuffle patches (table_name -> base64 patched bytes).
SLOT_KEY = "shuffle_tables"


def build_shuffle_tables(rng, *, shop_tier=SHOP_TIER_UNSHUFFLED, magic_shops=False,
                         item_prices=False, spell_mp=False,
                         equip_perms=False,
                         overworld_harder=False, dungeon_harder=False,
                         item_price_range=None, spell_price_range=None,
                         spell_mp_range=None, shop_ap_prices=None,
                         shop_ap_offers=None, shop_hint_prices=None,
                         shop_hint_caps=None,
                         costly_best_spells=False, slot_magic=False,
                         placeholders=None, placeholder_pools=None,
                         restrict_priceless_equip=False,
                         block_mana_items=False,
                         shop_extra_slots=_EXTRA_SLOTS_DEFAULT,
                         shared_tails=False):
    """Assemble the enabled Tier-A shuffles into per-physical-table patched bytes.
    Multiple features can touch the same table (e.g. equip_perms + item_prices
    both edit `weapons`); they are layered onto one bytearray so a single
    DataPatch covers each table. Returns {table_name: bytes} for tables that
    actually changed.

    The overworld table ALWAYS rolls from the shared ZONE_TIER map: off = each
    zone's named-tier band, `overworld_harder` = the tier's precomputed threat
    pool (_OW_HARDER_POOL, v192) + forced hand-picks + curated boss cameos.
    `dungeon_harder` rerolls each cave map from the NEXT dungeon up the
    progression chain instead of its own (see _CAVE_HARDER_DUNGEON) and stamps
    the cave boss cameos; with it unset the cave table stays vanilla-curated.
    Boss formations are stripped from every random draw (_BOSS_POOL_EXCLUDE /
    _draw_formation).

    `shop_tier` (SHOP_TIER_*, 0 = shops not shuffled) sets both whether the
    weapon/armor/item stores are shuffled and how deep the draw pool goes
    (overworld / +exotic / +activatable / +priceless).

    `shop_extra_slots` (yaml `shop_max_extra_items`) is how many EXTRA normal
    stock rows a store may roll on top of its base width. Only meaningful at a
    shuffled tier -- SHOP_TIER_UNSHUFFLED never relocates a list, so nothing
    widens there.

    `shop_ap_prices` (shop index -> gil, or a per-offer [gil, ...] list; None =
    feature off) injects the AP shop slots: placeholder ids into each store's
    tail rows and forced prices, applied AFTER the price shuffles. EVERY offer's
    price is baked, because a price belongs to the item id, not the shop row --
    so nothing is repriced at runtime.

    `shop_ap_offers` ({shop index: n}, from shop_offer_counts) asks each store
    for n PARALLEL offer rows: n distinct placeholder ids, each its own
    purchasable line. The rows are reserved as extra width before the stock
    fill. Returns `shop_ap_base_widths` (shop index -> normal stock rows)
    alongside `shop_ap_placeholders` so the client can re-render the tail.

    `shop_hint_prices` ([gil, ...], a QUEUE of priced hint products) plus
    `shop_hint_caps` ({gear shop index: max rows}, from hint_shop_caps) add HINT
    rows to the same store tails: each takes one more reserved row and one more
    placeholder id, and its price is baked exactly like an offer's. WHICH store
    carries which row is decided here, round-robin, once the placeholder supply
    is known -- so the queue is a seed-wide budget and a store whose category
    ran out of ids simply passes its turn. Stores are trimmed back to the rows
    they actually got, so an unfilled reserve never shows as a blank shelf line.
    A hint row is NOT a multiworld location, so nothing else in generation knows
    about it -- the split between the two kinds is reported back as
    `shop_ap_placeholders` (offers) and `shop_hint_placeholders` (hints), in
    shelf order, so the client can tell which tail row is which. Needs
    `placeholder_pools` (the static table holds one gid per store, which cannot
    price two rows apart).

    `placeholder_pools` ({"weapons": [(cat, gid), ...], "armor": [...]},
    preference-ordered) switches placeholder selection to POST-SHUFFLE:
    pick_seed_placeholders chooses every placeholder (consumable rows included)
    from ids the shuffled stores stock nowhere, and the chosen map is returned
    under result["shop_ap_placeholders"] (NOT a table -- callers must pop it).
    Without it, the legacy path applies: static/`placeholders` gids, blocked
    per-store from the shuffle draws.

    `shared_tails` (v2, 2026-08-16) replaces the per-seed pick with the
    RESERVED_SHOP_PLACEHOLDERS constants: rows in different stores SHARE gids
    and the client authors identity per town. Supply covers every legal count
    by construction, so the drained-pool trim below becomes dead code on this
    path. Placeholder prices still bake per gid; with sharing that is
    last-store-wins on the disc, which is fine because the client re-stamps the
    standing town's prices on town entry and the Buy list charges the live
    record (proven 2026-08-16). Mutually exclusive with `placeholder_pools`."""
    V = RD.VANILLA
    weapons = bytearray(V["weapons"])
    armor = bytearray(V["armor"])
    magic_info = bytearray(V["magic_info"])
    item_buy = bytearray(V["item_buy_prices"])
    item_bounds = _norm_range(item_price_range)
    spell_bounds = _norm_range(spell_price_range)
    shop_items = shop_tier >= SHOP_TIER_OVERWORLD

    def _plist(v):
        return list(v) if isinstance(v, (list, tuple)) else [v]

    # Hint rows ride the AP tail. They need per-row placeholder ids, so they are
    # only available on the post-shuffle picking path (placeholder_pools).
    # `shop_hint_prices` is a QUEUE, not a per-shop map: which store ends up
    # carrying which hint row is decided by the allocator once the placeholder
    # supply is known, so the caller hands over a priced product list and reads
    # the assignment back out of shop_hint_placeholders.
    assert not (shared_tails and placeholder_pools is not None), \
        "shared_tails replaces placeholder_pools; pass one or the other"
    hint_queue = ([int(p) for p in shop_hint_prices]
                  if (placeholder_pools is not None or shared_tails)
                  and shop_hint_prices else [])
    hint_caps = dict(shop_hint_caps or {}) if hint_queue else {}
    ap_prices = {int(o): _plist(v) for o, v in (shop_ap_prices or {}).items()}
    tail_on = shop_ap_prices is not None or bool(hint_caps)
    # Rows to reserve per store BEFORE the stock fill. AP asks for its requested
    # count (shop_ap_offers); with no offers dict, one row per shipped price.
    ap_req = {o: int((shop_ap_offers or {}).get(o, len(ap_prices[o])))
              for o in ap_prices}
    # The offer pass is untouched by hints -- except on a hints-only seed, where
    # it must ask for NOTHING rather than the historical one-row-per-store.
    pick_counts = shop_ap_offers
    if shop_ap_prices is None and hint_caps:
        pick_counts = {o: 0 for o in range(len(SHOP_AP_SLOTS))}

    if equip_perms:
        weapons = shuffle_who_equips_what(rng, weapons, 67)
        armor = shuffle_who_equips_what(rng, armor, 75)
    # Lock the priceless AP-gear pool to promoted-job equip only (after any
    # equip-perm shuffle, so it wins). exotic/priceless loot pool feature.
    if restrict_priceless_equip:
        apply_promoted_only_equip(weapons, armor)
    # Give priceless shop items a real base price BEFORE randomization, so a
    # random-prices roll scales the base instead of leaving them at 0 gil.
    # UNCONDITIONAL at any shuffled tier (user 2026-08-05): tier-gating this left
    # priceless goods free wherever they leaked into a store below SHOP_TIER_ALL
    # (the stepped-up caravan), and a real base price is harmless for stock the
    # seed never sells -- only shop-ELIGIBILITY is tier-gated.
    if shop_items:
        apply_priceless_base_prices(weapons, armor, item_buy)
    # Fangs (and 0-gil non-priceless weapons like Murasame) get a base price +
    # shop eligibility unconditionally (see _ALWAYS_PRICED_ITEMS /
    # _ALWAYS_PRICED_WEAPONS), independent of tier.
    if shop_items:
        apply_always_priced_items(item_buy, weapons)
    if item_prices:
        weapons = shuffle_equip_prices(rng, weapons, 67, item_bounds)
        armor = shuffle_equip_prices(rng, armor, 75, item_bounds)
    if spell_mp:
        magic_info = shuffle_spell_mana_costs(rng, magic_info, spell_mp_range,
                                              costly_best=costly_best_spells)
    if item_prices:
        magic_info = shuffle_magic_prices(rng, magic_info, spell_bounds)

    result = {}
    # shops first: the magic shuffle realigns each spell's level (+9) to the
    # tier of the shop it now sells in, so magic_info must be finalized after.
    shops = bytearray(V["shops"])
    if shop_items:
        # post-shuffle placeholder picking (placeholder_pools) needs NO draw
        # blocking: placeholders are chosen from unstocked ids afterwards.
        # ap_slots (the per-store static-gid draw block) is the LEGACY guard:
        # the pools path and the shared-constants path both scrub placeholder
        # ids from every store after the fill instead.
        shops = shuffle_shop_items(rng, shops, tier=shop_tier,
                                   ap_slots=(shop_ap_prices is not None
                                             and placeholder_pools is None
                                             and not shared_tails),
                                   ap_blocked=(ap_blocked_by_store(placeholders)
                                               if placeholders is not None else None),
                                   block_mana_items=block_mana_items,
                                   ap_reserve=(shop_ap_offers
                                               if shop_ap_prices is not None else None),
                                   hint_reserve=hint_caps or None,
                                   extra_slots=shop_extra_slots)
    if magic_shops:
        shops = shuffle_magic_shops(rng, shops)
        # Slot magic: a spell's LEVEL is its charge pool, so the costly list
        # buys distance instead of MP (there is no MP) -- out of level 1, with a
        # chance to climb one more. Needs the shuffled layout, and must land
        # before the learn rebuild/level alignment below.
        if slot_magic:
            shops = force_costly_spell_levels(rng, shops, costly_best_spells)
        result["magic_learn"] = bytes(rebuild_magic_learn(rng, shops))
        magic_info = align_shop_spell_levels(shops, magic_info)
    chosen_placeholders = None
    chosen_hint_placeholders = None
    shop_ap_base_widths = None
    if tail_on:
        if placeholder_pools is not None or shared_tails:
            if shared_tails:
                # v2: constants, sliced per store. No draw, no drain, no RNG --
                # the split below still runs but its short/trim legs are dead
                # (picks always cover the request).
                placeholders = reserved_placeholder_map(
                    pick_counts if pick_counts is not None else ap_req,
                    hint_caps=hint_caps, hint_target=len(hint_queue))
            else:
                # Post-shuffle pick: every placeholder (consumables included)
                # comes from ids the shuffled stores stock NOWHERE, so the
                # AP-authored name/price can never masquerade on another shelf.
                placeholders = pick_seed_placeholders(
                    rng, shops,
                    placeholder_pools.get("weapons", ()),
                    placeholder_pools.get("armor", ()),
                    counts=pick_counts, tier=shop_tier,
                    block_mana_items=block_mana_items,
                    hint_caps=hint_caps, hint_target=len(hint_queue))
            # SPLIT the tail: offers first, hints after. The picker can hand back
            # fewer ids than asked (a drained pool), and offers are real
            # multiworld locations while hints are not -- so offers keep what
            # there is and the hint rows are the ones that go short.
            chosen_placeholders = {}
            chosen_hint_placeholders = {}
            tail_ph = {}
            tail_prices = {}
            queue = list(hint_queue)
            for o in sorted(set(ap_prices) | set(hint_caps)):
                picks = ph_list(placeholders, o) if o in placeholders else []
                take = min(ap_req.get(o, 0), len(picks))
                ap_take = picks[:take]
                hint_take = picks[take:]
                # Prices are zipped against the ids in shelf order, so a short
                # pick must drop the SAME rows from the offer price list; hint
                # rows draw their price off the queue in this same shop order,
                # which is the contract fill_slot_data re-walks to pair each row
                # with the product it sells.
                hint_take = hint_take[:len(queue)]
                hint_gil, queue = queue[:len(hint_take)], queue[len(hint_take):]
                if ap_take:
                    chosen_placeholders[o] = ap_take
                if hint_take:
                    chosen_hint_placeholders[o] = hint_take
                if ap_take or hint_take:
                    tail_ph[o] = ap_take + hint_take
                    tail_prices[o] = ap_prices.get(o, [])[:take] + hint_gil
                # Give back the reserved rows this store did not fill, so the
                # shelf never shows a blank 0-gil line: the reserve is the CAP
                # (hint_shop_caps), not the share, precisely so rows can move to
                # a store whose category still has ids.
                reserved = ap_req.get(o, 0) + hint_caps.get(o, 0)
                short = reserved - len(ap_take) - len(hint_take)
                if short > 0:
                    city, row, _p = SHOP_AP_SLOTS[o]
                    idx = _DEF_IDX[(city, row)]
                    _rec, size, _lst = _shop_def(shops, _DEF_TABLES[0], idx)
                    set_shop_width(shops, idx, max(1, size - short))
            # Only stores that actually carry a tail row keep a placeholder: a
            # store with no offer and no hint (item shops on a hints-only seed)
            # must not have an id injected, scrubbed or priced for it.
            placeholders = tail_ph
        else:
            tail_prices = dict(ap_prices)
        inject_shop_ap_slots(shops, sorted(tail_prices), placeholders)
        # The store's NORMAL stock width, i.e. everything before the AP tail.
        # The client needs it to re-render the tail after each sale, and it is
        # only knowable here (the reserve may have been trimmed by a dry pool or
        # by the 15-row clamp). Counts the WHOLE tail: offers and hints sell out
        # of one contiguous block (render_shop_ap_tail depends on that).
        shop_ap_base_widths = {}
        ph_eff = (placeholders if placeholders is not None
                  else default_placeholders())
        for o in sorted(tail_prices):
            n = len(ph_list(ph_eff, o))
            _rec, size, _tail = shop_ap_slot_addrs(shops, o, n)[0]
            shop_ap_base_widths[o] = max(0, size - n)
        # Placeholder ids must be unbuyable outside their AP slot. Needed at EVERY
        # tier: at shop_tier 0 the vanilla lists stand unrewritten. all_stores in
        # pools mode (global invariant; normally a no-op after the unstocked pick).
        scrub_placeholder_stock(shops, sorted(tail_prices), placeholders,
                                all_stores=(placeholder_pools is not None
                                            or shared_tails))
    if shops != V["shops"]:
        result["shops"] = bytes(shops)
    if chosen_placeholders is not None:
        # NOT a table: the per-seed placeholder map for the caller to pop.
        result["shop_ap_placeholders"] = chosen_placeholders
    if chosen_hint_placeholders:
        # Same shape, for the hint half of each tail (also popped, not a table).
        result["shop_hint_placeholders"] = chosen_hint_placeholders
    if shop_ap_base_widths is not None:
        result["shop_ap_base_widths"] = shop_ap_base_widths

    if item_prices:
        item_buy = shuffle_item_buy_prices(rng, item_buy, item_bounds)
    # Balance pass: scale the strongest stock UP (priceless x2, activatable x1.5)
    # AFTER the price shuffles, so a random-price roll can't undercut them.
    if shop_items:
        apply_power_price_multipliers(weapons, armor, item_buy, shop_tier)
    if tail_on:
        # last word on placeholder prices: after priceless-base + price shuffles
        # + the power multipliers (an AP slot's own rolled price wins). Hint rows
        # are priced from the same list -- their gil is the tail entries past
        # the offers (see the split above).
        force_shop_ap_prices(tail_prices, weapons, armor, item_buy, placeholders)
    if weapons != V["weapons"]:
        result["weapons"] = bytes(weapons)
    if armor != V["armor"]:
        result["armor"] = bytes(armor)
    if magic_info != V["magic_info"]:
        result["magic_info"] = bytes(magic_info)
    if item_buy != V["item_buy_prices"]:
        result["item_buy_prices"] = bytes(item_buy)
    # Overworld foot encounters ALWAYS roll from the shared ZONE_TIER map (off =
    # each zone's named-tier band, on = the _OW_HARDER_POOL threat pools +
    # hand-picks + boss cameos, v192) -- the map corrections apply in both modes.
    # Caves stay vanilla unless dungeon_harder. The overworld branch also
    # touches the cave table, but solely to stamp the Onrac-region boss cameos
    # (Astos/Piscodemon), which physically live in cave map 0x28.
    ow, ow_hi = shuffle_zones_overworld(rng, V["zones_overworld"],
                                        harder=overworld_harder)
    if overworld_harder:
        ow = _stamp_boss_slots(ow, _OVERWORLD_BOSS_SLOTS)
        # a curated cameo is a u8 id, so clear any high byte left in that slot
        ow_hi = bytearray(ow_hi)
        for (rec, slot) in _OVERWORLD_BOSS_SLOTS:
            ow_hi[rec * 8 + slot] = 0
        ow_hi = bytes(ow_hi)
    result["zones_overworld"] = bytes(ow)
    if overworld_harder:
        # ALWAYS ship the companion when harder is on, even if it happens to be
        # all zero. The overworld_u16 cave is baked off the same flag, and a baked
        # cave with an unshipped companion would read the VANILLA terrain-3 bytes
        # as high bytes and produce garbage formation ids. The two must move
        # together; do not make this conditional on any(ow_hi).
        result["zones_overworld_hi"] = ow_hi
    if overworld_harder or dungeon_harder:
        cv = (shuffle_zones_caves(rng, V["zones_caves"], harder=True)
              if dungeon_harder else bytearray(V["zones_caves"]))
        if overworld_harder:
            cv = _stamp_boss_slots(cv, _ONRAC_BOSS_SLOTS)   # Onrac routes via cave map 0x28
        if dungeon_harder:
            cv = _stamp_boss_slots(cv, _DUNGEON_BOSS_SLOTS)
        result["zones_caves"] = bytes(cv)
    return result


def slot_data_from_tables(tables):
    """{name: bytes} -> {name: base64 str} for JSON slot_data transport."""
    return {k: base64.b64encode(v).decode() for k, v in tables.items()}


def ow_hi_from_slot_data(sd):
    """The overworld u16 COMPANION high-byte table (or None).

    v230: this is no longer a DataPatch. It used to be homed on the ISO table at
    0x08945aa8 -- which is the DESERT encounter table, not the "unused terrain-3"
    table it was documented as, so writing it there turned every desert tile in
    the game into Goblins and Skeletons. It now rides into the bake as
    feats["_ow_hi"] and lives inside the patcher's own cave segment
    (iso_patcher.apply_overworld_u16). rando_data still carries the vanilla blob
    under this name purely as the LENGTH reference."""
    enc = (sd or {}).get(SLOT_KEY) or {}
    if "zones_overworld_hi" not in enc:
        return None
    try:
        cand = base64.b64decode(enc["zones_overworld_hi"])
    except Exception:
        return None
    return cand if len(cand) == len(RD.VANILLA["zones_overworld_hi"]) else None


def patches_from_slot_data(sd):
    """Client side: read the shuffle slot_data key and yield
    (name, vanilla_bytes, patched_bytes) triples for boot_patch.DataPatch.
    Silently skips any table whose patched length != vanilla (corrupt)."""
    enc = (sd or {}).get(SLOT_KEY) or {}
    # The companion high-byte table must be decoded FIRST: strip_land_sea has to see
    # it, or it compares the low byte of a u16 id against the u8 ban sets and mangles
    # innocent fights (0x15e Red Flan -> low byte 0x5e, a banned Shark id).
    ow_hi = ow_hi_from_slot_data(sd)
    for name, b64 in enc.items():
        van = RD.VANILLA.get(name)
        if van is None:
            continue
        # NOT a data patch any more (v230) -- see ow_hi_from_slot_data. Writing it
        # to its old ISO home would overwrite the desert table again, and the
        # runtime reconcile loop would keep restoring that damage every tick.
        if name == "zones_overworld_hi":
            continue
        patched = base64.b64decode(b64)
        if len(patched) != len(van):
            continue
        # Fix seeds generated before the sea-on-land strip: scrub any sea forms the
        # old overworld shuffle baked into the LAND table (RNG-neutral, idempotent --
        # a no-op on tables already clean). See strip_land_sea / [[vehicle-encounter-table]].
        if name == "zones_overworld":
            patched = strip_land_sea(patched, van, ow_hi)
        # Fix seeds generated before the sell-price fix: any buyable consumable
        # whose sell != buy//2 let you buy low + sell high for infinite gil.
        # RNG-neutral + idempotent (recomputes sell from the already-rolled buy).
        if name == "item_buy_prices":
            patched = fix_item_sell_prices(patched)
        # Fix seeds generated before the relayout budget clamp: a store that
        # could not be seated kept its vanilla home, which by then belonged to
        # another store -- so two defs pointed into one list (see
        # repair_shop_list_overlaps). RNG-neutral + idempotent, and it runs
        # BEFORE the AP tail is rendered, so the victim's offers land on its own
        # shelf instead of the neighbour's.
        if name == "shops":
            patched = repair_shop_list_overlaps(patched)
        yield name, van, patched
