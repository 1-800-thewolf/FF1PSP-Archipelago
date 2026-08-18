"""
Boot-time DATA patching for the FF1 PSP bridge.

WHY THIS EXISTS (the hybrid split)
----------------------------------
PPSSPP runs PSP code through a JIT. Once a code block is compiled, rewriting the
underlying instruction bytes in RAM does NOT change what executes -- the debugger
`memory.write` does not invalidate the JIT block, and the WS debugger API exposes
no jit-clear event (see ppsspp_ws.py). This is the same wall the auto-dash 2.0
speed constant hit (ff1_data.DASH_FLAG_ADDR note) and the chest exec-breakpoint
unreliability (chest-handler-re). => CODE patches are NOT viable via this bridge.

DATA the game re-reads each frame / each level-up, however, patches cleanly: the
game reads the new value, no recompile involved. The encounter_rate table and the
level-up XP-requirement table are pure data. So instead of the old per-tick
re-poke loops (24 MB signature scan every minute + blind rewrites), we apply the
data once and only rewrite when it actually drifts back to vanilla.

REVERT MODEL
------------
A save-state load (and reload) restores RAM to the saved image, reverting our data
patch to vanilla. We detect that cheaply: keep the located addresses, and each tick
read just those small windows and compare to the patched bytes. Match => still
patched, do nothing (no write, no scan). Drift => rewrite in place. A full
signature scan runs only when we have no address yet (or a re-pin looks relocated,
e.g. after a full reboot). Steady state cost = one small read per table per tick.

This module is pure-data and side-effect-free except through the injected `psp`
object (anything with async read/write/read_chunked, i.e. ppsspp_ws.PPSSPP). The
scaling helpers are pure functions, unit-tested offline without PPSSPP.
"""

import struct

from .ppsspp_ws import USER_RAM_BASE, USER_RAM_SIZE
from . import eboot_patch as E
from . import ff1psp_debug as DBG
from . import ff1_data as D
from .. import rando as RANDO
from .. import rando_data as RD


# ---------------------------------------------------------- fixed table homes
# ABSOLUTE ISO offsets of every data table we patch. The shuffle tables come
# from rando_data.META; encounter/xp were located by signature once and are
# pinned here (verified byte-for-byte against the ISO + the live RAM addresses
# the old scanning path logged). BOOT.BIN's single PT_LOAD is a linear map, so
# each table also has a FIXED RAM address -- no signature scanning needed.
# rando_data tables that are NOT data patches. zones_overworld_hi is the overworld
# u16 companion: its META offset 0x2b21afc is the DESERT encounter table, and
# writing the companion there is what made every desert tile roll Goblins through
# v229. It now ships as bake context (feats["_ow_hi"]) into a cave-segment table --
# see iso_patcher.apply_overworld_u16 and rando.ow_hi_from_slot_data. It must stay
# out of TABLE_ISO_OFFSETS or the reconcile loop would re-inflict the damage every
# tick on a correctly baked ISO.
_NOT_DATA_PATCHED = frozenset({"zones_overworld_hi"})

TABLE_ISO_OFFSETS = {
    "encounter_rate": 0x2b216a8,       # RAM 0x8945654
    "xp_requirements": 0x2b2f438,      # RAM 0x89533e4
    "monster_rewards": 0x2b28480,      # monster_stats block (+0 XP, +2 Gil per rec)
    **{name: meta[0] for name, meta in RD.META.items()
       if name not in _NOT_DATA_PATCHED},
}


def table_ram_addr(name):
    """Fixed RAM address of a known table (None if not a fixed-home table)."""
    iso_off = TABLE_ISO_OFFSETS.get(name)
    if iso_off is None:
        return None
    return E.VADDR + (iso_off - E.BOOT_ISO_OFF - E.SEG_FILE_OFF)


# ---------------------------------------------------------------- pure builders
def scale_encounter_block(mult):
    """Vanilla encounter_rate table (96 u16) scaled by `mult`, clamped to u16.
    mult<1 => fewer encounters, mult==0 => off. Returns packed bytes."""
    scaled = [min(max(int(round(v * mult)), 0), 0xFFFF) for v in DBG.ENCOUNTER_RATES]
    return struct.pack("<96H", *scaled)


# Echidna / Cerberus / Ahriman / Two-Headed Dragon ship with xp = gil = 0: their
# bonus dungeons script the payout, so the stat record never needed one. The Chaos
# Shrine basement cameos (iso_patcher._CF_POOLS) field them as RANDOM encounters,
# where nothing scripts anything -- a 5000 HP Ahriman for zero reward. Rewards are
# set to Kraken parity (monster 0x7b: 4245 xp / 5000 gil), ranked by HP, BEFORE the
# boost multipliers so xp_boost scales them like every other monster.
#
# Applied here rather than in ff1_data because MONSTER_STATS_BLOCK is the DataPatch
# VANILLA SIGNATURE and must stay a byte-for-byte ISO mirror. iso_patcher's
# always-on dlc_boss_rewards writes the same four records on disc, so the patch's
# `cur != van and cur != new` check passes either way. Keeping this in the patched
# side also means the table is now non-vanilla at mult 1.0, so the DataPatch is no
# longer a no-op and the rewards are always written.
DLC_BOSS_REWARDS = {
    0x81: (4000, 4700),   # Cerberus           HP 4000
    0x83: (4300, 5000),   # Two-Headed Dragon  HP 4500
    0x80: (4400, 5200),   # Echidna            HP 4800
    0x82: (4500, 5400),   # Ahriman            HP 5000
}


def scale_monster_rewards(xp_mult, gil_mult):
    """Vanilla monster_stats block with the two STRIDED reward fields scaled:
    +0 XP reward (u16) x xp_mult, +2 Gil reward (u16) x gil_mult, each clamped to
    u16 (so >~204% clamps the 32000-XP/gil monsters -- an accepted ceiling). The
    party levels faster because each kill grants more XP (the game's own level-up
    check reads this table); gil piles up the same way.

    The four zero-reward DLC bosses (DLC_BOSS_REWARDS) are given a base payout
    first, so this is never a no-op even at mult 1.0.

    NB: thief-steal tiers are NOT affected -- _battle_xp() sums the vanilla
    D.MONSTER_XP constant, never the live/scaled field (see ff1_data / [[thief-
    steal-ability]]), so a low xp_boost still yields vanilla-XP-tier loot."""
    blk = bytearray(D.MONSTER_STATS_BLOCK)
    stride = D.MONSTER_STATS_STRIDE
    for mid, (xp, gil) in DLC_BOSS_REWARDS.items():
        struct.pack_into("<HH", blk, mid * stride, xp, gil)
    for i in range(D.MONSTER_STATS_COUNT):
        base = i * stride
        xp = struct.unpack_from("<H", blk, base)[0]
        gil = struct.unpack_from("<H", blk, base + 2)[0]
        struct.pack_into("<H", blk, base,
                         min(max(int(round(xp * xp_mult)), 0), 0xFFFF))
        struct.pack_into("<H", blk, base + 2,
                         min(max(int(round(gil * gil_mult)), 0), 0xFFFF))
    return bytes(blk)


# Boss ids whose combat stats boss_difficulty scales: SCROLL_BOSS_IDS, the
# canonical wiki boss set (user-curated).
#
# WarMech 0x76 is deliberately NOT here (user call 2026-08-03). It carries
# boss-tier stats but it is not a boss -- it has no boss room and no scripted
# fight, it is a rare random encounter, so a "boss difficulty" slider has no
# business touching it. It used to be added on, which had two bad effects:
# boss_difficulty scaled it, and (because cameo softening only reaches ids this
# set contains) every WarMech in the game was ALSO halved as a cameo, since a
# monster with no home dungeon is a cameo everywhere. It is a normal monster in
# all cases now -- see rando._NEVER_SOFT.
def _boss_stat_ids():
    from . import iso_patcher as IP
    return IP.SCROLL_BOSS_IDS


# Every monster_stats record that Monster Power scales: all of them EXCEPT the
# bosses (which Boss Difficulty owns). The two id sets are disjoint and cover the
# whole table between them, so no record is scaled by both knobs. WarMech 0x76 is
# a plain random encounter -- not in _boss_stat_ids() -- so it lands here and
# Monster Power scales it like any other mob (user call 2026-08-04).
def _nonboss_stat_ids():
    boss = set(_boss_stat_ids())
    return frozenset(i for i in range(D.MONSTER_STATS_COUNT) if i not in boss)


# monster_stats intra-record combat fields (byte offsets), live-verified against
# the DoS/PSP wiki bestiary 2026-07-14: +4 HP u16, +8 evasion, +9 defense,
# +10 hits/turn, +11 accuracy, +12 attack, +13 agility, +14 intelligence,
# +15 crit rate, +20 magic defense. (+0 XP / +2 Gil handled by rewards above.)
_BS_HP, _BS_EVA, _BS_DEF, _BS_HITS = 4, 8, 9, 10
_BS_ACC, _BS_ATK, _BS_AGI, _BS_INT, _BS_MDEF = 11, 12, 13, 14, 20
_BS_DAMP = 0.585        # defense-family damping: Kraken 60 def -> ~200 at 500%
_BS_HITS_CAP = 16   # live-verified 2026-07-14: 10 and 16 hits/turn run clean
                    # in-battle; 16 = worst overflow case (Omega/Shinryu @500%)


def _scale_stat_records(blk, mult, ids):
    """Scale the combat stats of the given monster ids in a monster_stats block
    (in place). Linear x mult, clamped to the byte: attack, accuracy, agility,
    intelligence. Damped (x 1+(mult-1)*0.585 above vanilla, linear below) so a
    monster never becomes unhittable/immune: defense, magic defense, evasion. HP
    scales linearly into its u16; overflow past 65535 converts into extra
    hits/turn (round, capped at 16) so lost HP comes back as offense. Crit rate
    untouched (scaled crits one-shot the party). Returns blk."""
    if mult == 1.0:
        return blk
    damped = mult if mult < 1.0 else 1.0 + (mult - 1.0) * _BS_DAMP

    def b(base, off, m):
        blk[base + off] = min(255, max(0, int(round(blk[base + off] * m))))

    for i in ids:
        base = i * D.MONSTER_STATS_STRIDE
        hp = struct.unpack_from("<H", blk, base + _BS_HP)[0]
        wanted = hp * mult
        struct.pack_into("<H", blk, base + _BS_HP,
                         min(65535, max(1, int(round(wanted)))))
        if wanted > 65535:
            hits = blk[base + _BS_HITS]
            blk[base + _BS_HITS] = min(_BS_HITS_CAP,
                                       max(hits, int(round(hits * wanted / 65535))))
        for off in (_BS_ATK, _BS_ACC, _BS_AGI, _BS_INT):
            b(base, off, mult)
        for off in (_BS_DEF, _BS_MDEF, _BS_EVA):
            b(base, off, damped)
    return blk


def scale_boss_stats(blk, mult):
    """Scale every boss's combat stats by boss_difficulty (1.0 vanilla), in place.
    See _scale_stat_records for the exact math. Returns blk."""
    if mult == 1.0:
        return blk          # skip the id-set build on the default multiplier
    return _scale_stat_records(blk, mult, _boss_stat_ids())


def scale_monster_stats(blk, mult):
    """Scale every NON-boss monster's combat stats by monster_power (1.0 vanilla),
    in place -- the same math as scale_boss_stats on the complementary id set, so
    the two knobs never touch the same record. Returns blk."""
    if mult == 1.0:
        return blk          # skip the 203-entry id-set build on the default
    return _scale_stat_records(blk, mult, _nonboss_stat_ids())


# A boss met away from its own boss room -- stamped into a rare random-encounter
# slot of a dungeon 3 tiers past its home (rando._DUNGEON_BOSS_SLOTS), or riding
# along as another boss's MINION (boss_minions) -- fights at this fraction of
# power. It ambushes an under-levelled party with no save point and no warning,
# or it is a second boss in a fight already balanced around one, so it gets a
# discount the scripted fight does not. See rando.cameo_soft_map() for the
# no-overlap property that makes this safe.
CAMEO_BOSS_SCALE = 0.50
# The soft record is built at CAMEO_BOSS_SCALE x min(boss_mult, 1.0), i.e. the
# LOWER of "half of base power" and "half of the seed's boss_difficulty" (user
# spec 2026-07-22). boss_difficulty only ever pulls the cameo DOWN: at 300% the
# guest still fights at 50%, while at 50% it fights at 25%. Multiplying straight
# through (the pre-2026-07-22 behaviour) let a 200% seed field a 150% "softened"
# minion -- live-reported as a Two-Headed Dragon one-shotting the party in the
# Shinryu fight.
def _cameo_mult(boss_mult):
    return min(boss_mult, 1.0) * CAMEO_BOSS_SCALE


def monster_stats_block(xp_mult=1.0, gil_mult=1.0, boss_mult=1.0, soft_ids=(),
                        monster_mult=1.0):
    """The monster_stats block as it should look in RAM right now.

    Rewards scaled (xp/gil), then NON-boss monsters scaled by monster_mult and
    bosses by boss_mult -- except the records in `soft_ids`, which are spliced in
    from a second block built at _cameo_mult(boss_mult). Non-boss scaling is
    folded into the shared `base` first: it and boss scaling touch disjoint id
    sets, so the splice below (boss records only) carries the monster-scaled
    non-boss records through untouched. Built from vanilla every time (never
    rescaled from already-scaled bytes), so toggling a map's cameo softening on
    and off repeatedly can't drift."""
    base = bytearray(scale_monster_rewards(xp_mult, gil_mult))
    scale_monster_stats(base, monster_mult)
    full = bytes(scale_boss_stats(bytearray(base), boss_mult))
    if not soft_ids:
        return full
    soft = bytes(scale_boss_stats(bytearray(base), _cameo_mult(boss_mult)))
    out = bytearray(full)
    stride = D.MONSTER_STATS_STRIDE
    for i in soft_ids:
        out[i * stride:(i + 1) * stride] = soft[i * stride:(i + 1) * stride]
    return bytes(out)


# --- magic_power_scaling mailbox tables (iso_patcher v228) -------------------
# The on-disc caves read three parallel u16[256] tables keyed by MONSTER ID.
# Building them here keeps the domain logic (which multiplier owns which id,
# including cameo softening) in the ONE module that already owns it -- the
# client must never re-derive it, or the engine and the odds log drift apart.
MP_TABLE_IDS = 256                  # cave indexes a full byte of monster id
MP_SHRINK_BASE = 0.5                # shrink = BASE ** (mult - 1)
MP_SENTINEL = 0                     # shrink256 == 0 => that monster is VANILLA


def _mp_shrink256(mult):
    """round(256 * 0.5**(mult-1)), or the VANILLA sentinel at exactly 1.0.

    Above 1.0 this decays landing chance multiplicatively (x0.50 at 200%,
    x0.25 at 300%, x0.065 at 500%). BELOW 1.0 it exceeds 256 -- an EXTRA chance
    to hit at reduced Monster Power, which is deliberate (user spec 2026-08-05);
    the engine's own clamp to 201 caps the result at 100%."""
    if round(mult * 1000) == 1000:
        return MP_SENTINEL
    return max(1, min(0xFFFF, int(round(256 * MP_SHRINK_BASE ** (mult - 1.0)))))


def magic_power_tables(boss_mult=1.0, soft_ids=(), monster_mult=1.0):
    """(mdef_eff, mdef_van, shrink256) as packed u16[256] little-endian blocks.

    mdef_van  the VANILLA magic-defence byte. The to-hit cave rebuilds the score
              from this, which is the whole point: the engine's own linear
              `acc + 148 - mdef` collapses once mdef is scaled.
    mdef_eff  magic defence as actually scaled into the live monster_stats
              record -- INCLUDING cameo softening, and deliberately NOT clamped
              to the byte the record has to store. _scale_stat_records pins at
              255, which is why vanilla magic damage stops responding to power
              past ~200%; the damage curve reads this uncapped value instead.
    shrink256 keyed on the monster's DOMAIN multiplier (boss_difficulty for
              bosses, monster_power otherwise) -- NOT on the cameo multiplier.
              That is what preserves the hard requirement that 100% power is
              exactly vanilla: at boss_mult 1.0 a cameo-softened boss gets the
              sentinel and every leg bails, even though its stat record is built
              at half power. The softening is already expressed in mdef_eff; it
              is a weaker record, not a different game rule.

    A monster whose domain multiplier is exactly 1.0 gets the sentinel, so
    Monster Power and Boss Difficulty resolve independently per id."""
    bosses = _boss_stat_ids()
    soft = set(soft_ids or ())
    cameo = _cameo_mult(boss_mult)
    n_rec = len(D.MONSTER_STATS_BLOCK) // D.MONSTER_STATS_STRIDE
    eff = bytearray(2 * MP_TABLE_IDS)
    van = bytearray(2 * MP_TABLE_IDS)
    shr = bytearray(2 * MP_TABLE_IDS)
    for i in range(min(MP_TABLE_IDS, n_rec)):
        mdef = D.MONSTER_STATS_BLOCK[i * D.MONSTER_STATS_STRIDE + _BS_MDEF]
        domain = boss_mult if i in bosses else monster_mult
        actual = cameo if i in soft else domain
        # damping formula must stay in LOCKSTEP with _scale_stat_records'
        # identical line: these tables describe the exact mdef the scaled
        # stat block carries -- diverging makes the caves read a value the
        # records never hold.
        damped = actual if actual < 1.0 else 1.0 + (actual - 1.0) * _BS_DAMP
        struct.pack_into("<H", eff, i * 2, min(0xFFFF, int(round(mdef * damped))))
        struct.pack_into("<H", van, i * 2, mdef)
        struct.pack_into("<H", shr, i * 2, _mp_shrink256(domain))
    return bytes(eff), bytes(van), bytes(shr)


# ----------------------------------------------------------------- patch engine
class DataPatch:
    """One relocatable data table to keep patched.

    vanilla_sig : the table's vanilla bytes -- doubles as the RAM search signature
                  AND as the "has it reverted?" marker.
    patched     : the bytes we want present instead.
    Both must be the same length (an in-place overwrite of the table).
    """

    def __init__(self, name, vanilla_sig, patched, fixed_addr=None):
        assert len(vanilla_sig) == len(patched), "sig/patched length mismatch"
        self.name = name
        self.vanilla_sig = bytes(vanilla_sig)
        self.patched = bytes(patched)
        # fixed_addr: the table's known, non-relocating RAM home (ELF .data).
        # Pre-locates the patch (no RAM scan) and disables relocation handling.
        self.fixed = fixed_addr is not None
        self.addrs = [fixed_addr] if self.fixed else []
        self.is_noop = self.vanilla_sig == self.patched
        # Earlier `patched` versions, appended by anyone who MUTATES patched at
        # runtime (the shop AP-slot delist). A save-state image can hold one of
        # these; reconcile() must treat it like "reverted", not "relocated".
        self.stale = []
        self._warned_foreign = False

    def _find(self, blob, base=USER_RAM_BASE):
        """Locate every copy of the vanilla signature inside an already-read blob."""
        hits, start = [], 0
        while True:
            i = blob.find(self.vanilla_sig, start)
            if i < 0:
                break
            hits.append(base + i)
            start = i + 1
        return hits

    async def _scan(self, psp):
        """Full signature scan of user RAM for vanilla copies of the table."""
        blob = await psp.read_chunked(USER_RAM_BASE, USER_RAM_SIZE)
        return self._find(blob)

    async def locate_in(self, psp, blob, base=USER_RAM_BASE):
        """Locate + write using a SHARED pre-read RAM blob (so N patches cost one
        read, not N). `base` = the RAM address blob[0] was read from. Returns the
        NEWLY found addresses (empty if nothing new).

        MERGES with already-located copies instead of replacing: a heap bank can
        gain a resident copy after boot (the weapons/armor shop NAME banks load
        once at the title screen and AGAIN with the save -- live 2026-08-05: the
        shop UI reads the second copy, so a boot-only locate left shelf names
        vanilla). An already-patched copy no longer matches the vanilla
        signature, so a plain replace would drop it. Cached addrs whose content
        matches no known state (freed/relocated heap) are pruned, so a stale
        address is never blind-written over a future allocation."""
        if self.is_noop:
            return []
        found = self._find(blob, base)
        keep = []
        for a in self.addrs:
            try:
                cur = await psp.read(a, len(self.patched))
            except Exception:
                continue
            if cur == self.patched or cur == self.vanilla_sig or cur in self.stale:
                keep.append(a)
        self.addrs = sorted(set(keep) | set(found))
        await self._write_all(psp)
        return [a for a in found if a not in keep]

    async def _write_all(self, psp):
        for a in self.addrs:
            await psp.write(a, self.patched)

    async def apply(self, psp):
        """Locate the vanilla table(s) and overwrite with the patched bytes.
        Returns the list of addresses patched (empty if not found)."""
        if self.is_noop:
            return []
        self.addrs = await self._scan(psp)
        await self._write_all(psp)
        return list(self.addrs)

    async def reconcile(self, psp):
        """Cheap per-tick upkeep. If a cached address still holds our patched bytes,
        do nothing. If it drifted (reverted to vanilla by a load), rewrite in place.
        Returns True if a full RAM re-scan is needed (not located yet, or the table
        relocated out from under us) so the caller can do ONE shared scan for all
        patches instead of each patch re-reading 24 MB. One small read per addr in the
        common case."""
        if self.is_noop:
            return False
        if not self.addrs:
            return True                       # never located -> caller rescans
        relocated = False
        for a in list(self.addrs):
            cur = await psp.read(a, len(self.patched))
            if cur == self.patched:
                continue                      # still good
            if cur == self.vanilla_sig or cur in self.stale:
                await psp.write(a, self.patched)   # reverted/stale -> re-patch in place
            elif self.fixed:
                # A fixed-home ELF table can't relocate. Foreign bytes = a save
                # state from another seed/bake or a mid-boot transient: do NOT
                # blind-write over unknown data; say so once and retry next tick
                # (a transient becomes vanilla/patched and self-heals).
                if not self._warned_foreign:
                    self._warned_foreign = True
                    print(f"  [boot_patch] {self.name}: unexpected bytes at fixed "
                          f"addr {a:#x} (stale save state from another seed?)")
            else:
                relocated = True              # neither -> table moved; caller rescans
        return relocated


class ShopBankPatch(DataPatch):
    """Shop NAME/DESC bank patch. Authors every located copy, exactly like the
    base class -- live probing 2026-08-05 showed all UIs (inventory menu, shop
    list) render from the LOWEST resident copy; the higher copy is the unused
    second-language region, and the shop UI merely SNAPSHOTS the bank when its
    dialog opens. The inventory name-bleed is prevented in ApClient instead:
    banks are authored only while the player stands inside a shop building
    (fine FIELD_MAP_ID in D.SHOP_INTERIOR_FIELD_MAP_IDS). What this class
    adds is resilient LOCATION."""

    # Locate by PREFIX + similarity, not exact match: a single stray byte in a
    # resident copy (live 2026-08-05: one glyph byte differed in the post-save
    # weapons copy) must not blind the scan forever. The 64-byte prefix (bank
    # header + offset-table start) is unique; a candidate then counts as this
    # bank if it is >=98% byte-identical to the vanilla OR authored image --
    # the follow-up write normalizes it either way.
    LOCATE_PREFIX = 64
    SIM_MIN = 0.98

    def _find(self, blob, base=USER_RAM_BASE):
        pre = self.vanilla_sig[:self.LOCATE_PREFIX]
        n = len(self.vanilla_sig)
        hits, start = [], 0
        while True:
            i = blob.find(pre, start)
            if i < 0:
                break
            cand = blob[i:i + n]
            if len(cand) == n:
                best = max(sum(a == b for a, b in zip(cand, self.vanilla_sig)),
                           sum(a == b for a, b in zip(cand, self.patched)))
                if best >= n * self.SIM_MIN:
                    hits.append(base + i)
            start = i + 1
        return hits

def build_patches(enc_mult=1.0, xp_mult=1.0, gil_mult=1.0, slot_data=None,
                  dabble_baked=False, boss_mult=1.0, monster_mult=1.0):
    """Construct the DataPatch set from the scaling options plus any Tier-A
    data-table shuffles carried in slot_data (see rando.SLOT_KEY). No-op patches
    (mult==1) are still returned but skip all RAM I/O via DataPatch.is_noop.

    Every one of these tables has a FIXED RAM home (TABLE_ISO_OFFSETS), so the
    patches are pre-located -- reconcile costs one small read per table per
    tick and NEVER scans RAM.

    dabble_baked: the running ISO was baked with monk_thief_dabble_in_magic,
    which ORs its learn bits on top of magic_learn ON DISC. The expected
    (patched) bytes must carry those bits too, or reconcile would see a
    'foreign' table forever -- this exact mismatch (vanilla-signature miss) is
    what used to make the old scanning path re-read 24 MB every 5 s for the
    whole session."""
    from . import iso_patcher as IP
    patches = [
        DataPatch("encounter_rate", DBG.ENCOUNTER_SIG, scale_encounter_block(enc_mult),
                  fixed_addr=table_ram_addr("encounter_rate")),
        # xp_boost + gil_boost both live in the monster_stats reward fields now
        # (percentage multipliers). Replaces the old xp_requirements-division
        # scheme; one block patch scales XP (+0) and Gil (+2) per record.
        DataPatch("monster_rewards",
                  D.MONSTER_STATS_BLOCK,
                  monster_stats_block(xp_mult, gil_mult, boss_mult,
                                      monster_mult=monster_mult),
                  fixed_addr=table_ram_addr("monster_rewards")),
    ]
    shuffles = list(RANDO.patches_from_slot_data(slot_data))
    # dabble's learn bits follow the magic-shop shuffle (slot remap), so the
    # overlay needs the seed's shuffled shops to match the baked table
    shops = next((p for n, _v, p in shuffles if n == "shops"), None)
    for name, vanilla, patched in shuffles:
        if name == "magic_learn" and dabble_baked:
            patched = bytes(IP.apply_dabble_learn_overlay(bytearray(patched),
                                                          shops=shops))
        patches.append(DataPatch(f"shuffle:{name}", vanilla, patched,
                                 fixed_addr=table_ram_addr(name)))
    return patches


def bake_data_patches(enc_mult=1.0, xp_mult=1.0, gil_mult=1.0, slot_data=None,
                      boss_mult=1.0, monster_mult=1.0):
    """The launcher-side twin of build_patches: the same tables as ISO data
    patches (iso_patcher.apply_data_patches format) for baking into the
    patched ISO. Code features (e.g. dabble's learn bits) are layered on top
    by patch_iso AFTER these, so pass the plain shuffle bytes here."""
    out = []
    for p in build_patches(enc_mult=enc_mult, xp_mult=xp_mult, gil_mult=gil_mult,
                           slot_data=slot_data, boss_mult=boss_mult,
                           monster_mult=monster_mult):
        if p.is_noop:
            continue
        name = p.name.split(":", 1)[-1]
        iso_off = TABLE_ISO_OFFSETS.get(name)
        if iso_off is None:
            continue                          # runtime-only patch (none today)
        # NB: build_patches was called WITHOUT dabble_baked, so magic_learn
        # here is the plain shuffle output -- exactly what the bake wants.
        out.append({"name": p.name, "iso_off": iso_off,
                    "vanilla": p.vanilla_sig, "patched": p.patched})
    return out
