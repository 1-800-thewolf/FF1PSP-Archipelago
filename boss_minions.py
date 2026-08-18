"""Boss minions: per-boss (M, S) pool variants, rolled once at generation.

Every boss row has 4 curated pools; gen rolls ONE pool per boss (seed-static:
dying to a boss and retrying always refights the same adds), then the
boss_minions intensity decides the adds spawned from that pool:

    off       boss only
    light     boss + 1 minion
    difficult boss + 3 minions
    absurd    boss + 3 minions + 1 super-hard   (S = the pool's super-hard pick)

The plan ships in slot data (each entry = [fid, groups, layout]); the client's
ISO patcher (iso_patcher.apply_boss_minions + ms2_bake) edits the formation
records and provisions the per-formation sprite packs (MS2_<fid>.PCK).

Mechanics (see boss-adds-ms2-pack-cracked + dlc-boss-minion-feasibility
memories, all live-proven 2026-07-14/15):
  * Formation record byte0 = layout; layout 2 (BIG_GRID_LAYOUT) = 4 big-grid
    positions (boss slot 0 + up to 3 adds), layout 0/5 = the SAME 9 packed small
    positions in a different ORDER (0 = mid row first, 5 = top row first).
    absurd needs 5 monsters so it uses the 9-slot grid for EVERY boss
    (live-verified 2026-07-15: even a size-2 Fiend sprite renders acceptably on
    the small grid -- some overlap, no crash), and single big bosses take the
    TOP-first ordering (layout 5) there so the boss sprite does not sit on the
    middle row with its damage numbers under the battle menu (live 2026-08-03).
    Lighter intensities keep the overlap-free big grid for single bosses. Swarm
    bosses stay on layout 0 -- their slot-0 sprite is small, so no popup problem,
    and the mid-first order packs the swarm the way vanilla does. Swarm bosses
    (SMALL_GRID_FIDS -- Piscodemon, slot-0 count 2-7) ALWAYS use the small grid
    and emit the super-hard S first so the swarm can't clamp it out. The chosen
    layout travels in the plan (roll_plan) because it depends on intensity.
  * Every NORMAL add monster (id < 0x80 or 0x91+) needs its sprite pair
    (MS_<gim>.GIM/_S) inside MS2_<fid>.PCK (gid-sorted).
  * BOSS-id monsters (0x80-0x90) load per-MONSTER from their dedicated
    BOSS_<mon-0x80>.PCK packs, pack-free, in ANY slot -- live-proven
    (Echidna + Two-Headed Dragon as add, no MS2 support, rendered clean).
    So DLC bosses can be adds with zero sprite work.
  * DLC bosses (fids 0x100-0x110) ARE formation-based but ship with NO MS2
    pack -- ms2_bake creates one when normal adds need it (donor dpk record
    steal).
  * VRAM: 3 distinct species incl. two boss-size sprites verified clean
    (Tiamat + Weretiger x2 + Lich pixel-perfect).
  * Chronodia (0xC3-0xCA) has no formation record -- out of scope.

Fid map (formation-table scan): vanilla bosses sit at 0x1C/0x9C (Piscodemon),
0x7A-0x7F + 0x7B (Chaos), alt fiend forms 0x73-0x76; DLC bosses at
0x100 + (mon - 0x80). Scarmiglione owns two records (0x104/0x105); fid pairs
share their boss's rolled pool.
"""

# (boss_name, (fids...), [4 pools of ((M_id, M_name), (S_id, S_name))]).
# Names kept for readability; test_boss_minions cross-checks them against the
# bestiary. Pool picks curated by the user (boss_minion_pools.xlsx 2026-07-14).
BOSS_POOL_SETS = [
    ("Garland", (0x7F,), [
        ((0x15, "Skeleton"),        (0x6F, "Black Knight")),
        ((0x2B, "Zombie"),          (0x2C, "Ghoul")),
        ((0x01, "Goblin Guard"),    (0x69, "Garland")),
        ((0x02, "Wolf"),            (0x04, "Werewolf"))]),
    ("Piscodemon", (0x1C, 0x9C), [
        ((0x3E, "Gargoyle"),        (0x3F, "Horned Devil")),
        ((0x51, "Cockatrice"),      (0xA2, "Poison Eagle")),
        ((0x4A, "Tarantula"),       (0xA6, "Gloom Widow")),
        ((0x0C, "Sahagin"),         (0x0E, "Sahagin Prince"))]),
    ("Vampire", (0x7C,), [
        ((0x04, "Werewolf"),        (0x70, "Death Knight")),
        ((0x16, "Bloodbones"),      (0x3C, "Vampire")),
        ((0x15, "Skeleton"),        (0xAA, "Bonesnatch")),
        ((0xBF, "Skuldier"),        (0x50, "King Mummy"))]),
    ("Astos", (0x7D,), [
        ((0x1D, "Ogre Mage"),       (0x69, "Garland")),
        ((0x1F, "Anaconda"),        (0x72, "Dark Wizard")),
        ((0x06, "Lizard"),          (0x71, "Astos")),
        ((0x29, "Specter"),         (0x4B, "Manticore"))]),
    ("Lich", (0x7A, 0x73), [
        ((0x2A, "Ghost"),           (0x44, "Dragon Zombie")),
        ((0x4F, "Mummy"),           (0x50, "King Mummy")),
        ((0x21, "Scorpion"),        (0x71, "Astos")),
        ((0x40, "Earth Elemental"), (0x35, "Earth Medusa"))]),
    ("Marilith", (0x79, 0x74), [
        ((0x07, "Fire Lizard"),     (0x43, "Red Dragon")),
        ((0x31, "Lava Worm"),       (0x0B, "Fire Gigas")),
        ((0x27, "Shadow"),          (0xBD, "Reaper")),
        ((0x6C, "Clay Golem"),      (0x6D, "Stone Golem"))]),
    ("Kraken", (0x78, 0x75), [
        ((0x58, "Red Piranha"),     (0x63, "Water Naga")),
        ((0x0E, "Sahagin Prince"),  (0x14, "Deepeyes")),
        ((0x61, "Water Elemental"), (0x9C, "Killer Shark")),
        ((0x67, "Piscodemon"),      (0xA8, "Squidraken"))]),
    ("Tiamat", (0x77, 0x76), [
        ((0x54, "Wyrm"),            (0x6B, "Blue Dragon")),
        ((0x65, "Chimera"),         (0x98, "Mage Chimera")),
        ((0x62, "Air Elemental"),   (0x4D, "Baretta")),
        ((0x36, "Weretiger"),       (0x77, "Lich"))]),
    ("Chaos", (0x7B,), [
        ((0x68, "Mindflayer"),      (0x79, "Marilith")),
        ((0x44, "Dragon Zombie"),   (0xAC, "Black Dragon")),
        ((0x3D, "Vampire Lord"),    (0x7D, "Tiamat")),
        ((0xA7, "Duel Knight"),     (0x7B, "Kraken"))]),
    # ---------------- DLC / bonus-dungeon bosses (fid = 0x100 + mon - 0x80) --
    ("Echidna", (0x100,), [
        ((0x67, "Piscodemon"),      (0x69, "Garland")),
        ((0x20, "Sea Snake"),       (0x5C, "Neochu")),
        ((0x4B, "Manticore"),       (0x66, "Rhyos")),
        ((0x38, "Ankheg"),          (0x2F, "Purple Worm"))]),
    ("Cerberus", (0x101,), [
        ((0xB3, "Devil Hound"),     (0xB2, "Dark Elemental")),
        ((0x05, "Winter Wolf"),     (0xBA, "Dark Wolf")),
        ((0x19, "Hyenadon"),        (0x9E, "Blood Tiger")),
        ((0x3B, "Sabertooth"),      (0xB4, "Sekhret"))]),
    ("Ahriman", (0x102,), [
        ((0x32, "Evil Eye"),        (0x33, "Death Eye")),
        ((0x14, "Deepeyes"),        (0xA0, "Bloody Eye")),
        ((0x29, "Specter"),         (0x9F, "Dark Eye")),
        ((0x13, "Bigeyes"),         (0x37, "Rakshasa"))]),
    ("Two-Headed Dragon", (0x103,), [
        ((0x53, "Wyvern"),          (0x42, "White Dragon")),
        ((0x54, "Wyrm"),            (0x6B, "Blue Dragon")),
        ((0x52, "Pyrolisk"),        (0x43, "Red Dragon")),
        ((0x5D, "Hydra"),           (0x5E, "Fire Hydra"))]),
    ("Scarmiglione", (0x104, 0x105), [
        ((0xAA, "Bonesnatch"),      (0xBD, "Reaper")),
        ((0xC2, "Revenant"),        (0x2D, "Ghast")),
        ((0x24, "Minotaur Zombie"), (0xBF, "Skuldier")),
        ((0x16, "Bloodbones"),      (0x44, "Dragon Zombie"))]),
    ("Cagnazzo", (0x106,), [
        ((0xBC, "Sahagin Queen"),   (0xA8, "Squidraken")),
        ((0x5A, "White Croc"),      (0xAE, "Earth Troll")),
        ((0x12, "White Shark"),     (0x9C, "Killer Shark")),
        ((0x26, "Sea Troll"),       (0xAF, "Poison Naga"))]),
    ("Barbariccia", (0x107,), [
        ((0x6D, "Stone Golem"),     (0x64, "Spirit Naga")),
        ((0xA2, "Poison Eagle"),    (0xB5, "Catoblepas")),
        ((0x48, "Black Flan"),      (0xBB, "Rock Gargoyle")),
        ((0x62, "Air Elemental"),   (0x65, "Chimera"))]),
    ("Rubicante", (0x108,), [
        ((0xC0, "Red Flan"),        (0x0B, "Fire Gigas")),
        ((0x31, "Lava Worm"),       (0x94, "Flare Gigas")),
        ((0x41, "Fire Elemental"),  (0x79, "Marilith")),
        ((0x29, "Specter"),         (0x43, "Red Dragon"))]),
    ("Gilgamesh", (0x109,), [
        ((0x5F, "Guardian"),        (0x60, "Soldier")),
        ((0x70, "Death Knight"),    (0x6E, "Iron Golem")),
        ((0x6F, "Black Knight"),    (0xA7, "Duel Knight")),
        ((0x69, "Garland"),         (0x71, "Astos"))]),
    ("Omega", (0x10A,), [
        ((0x6D, "Stone Golem"),     (0x6E, "Iron Golem")),
        ((0x6C, "Clay Golem"),      (0x9B, "Mythril Golem")),
        ((0xC1, "Prototype"),       (0x7B, "Kraken")),
        ((0x4E, "Desert Baretta"),  (0x80, "Echidna"))]),
    ("Shinryu", (0x10B,), [
        ((0x42, "White Dragon"),    (0x9A, "Holy Dragon")),
        ((0x6A, "Green Dragon"),    (0x83, "Two-Headed Dragon")),
        ((0x99, "Yellow Dragon"),   (0xAC, "Black Dragon")),
        ((0x55, "Allosaurus"),      (0x56, "Tyrannosaur"))]),
    ("Atomos", (0x10C,), [
        ((0x2F, "Purple Worm"),     (0x30, "Sand Worm")),
        ((0x17, "Gigas Worm"),      (0x92, "Abyss Worm")),
        ((0xB6, "Hundlegs"),        (0xB7, "Undergrounder")),
        ((0x39, "Remorazz"),        (0x68, "Mindflayer"))]),
    ("Typhon", (0x10D,), [
        ((0x5D, "Hydra"),           (0xB1, "Yamatano Orochi")),
        ((0x93, "Elm Gigas"),       (0xA1, "Flood Gigas")),
        ((0x96, "Yellow Ogre"),     (0x97, "Mad Ogre")),
        ((0x66, "Rhyos"),           (0xB4, "Sekhret"))]),
    ("Orthros", (0x10E,), [
        ((0x58, "Red Piranha"),     (0x63, "Water Naga")),
        ((0x5A, "White Croc"),      (0x50, "King Mummy")),
        ((0x2A, "Ghost"),           (0x5C, "Neochu")),
        ((0x08, "Basilisk"),        (0x7B, "Kraken"))]),
    ("Phantom Train", (0x10F,), [
        ((0xBD, "Reaper"),          (0x2A, "Ghost")),
        ((0x27, "Shadow"),          (0x77, "Lich")),
        ((0xB8, "Death Elemental"), (0xA9, "Pharaoh")),
        ((0x24, "Minotaur Zombie"), (0xB0, "Earth Plant"))]),
    ("Death Gaze", (0x110,), [
        ((0x32, "Evil Eye"),        (0xA0, "Bloody Eye")),
        ((0x30, "Sand Worm"),       (0x92, "Abyss Worm")),
        ((0x3F, "Horned Devil"),    (0x77, "Lich")),
        ((0xB5, "Catoblepas"),      (0xBD, "Reaper"))]),
]

# fid -> boss slot-0 monster id (for reference/tests). Vanilla per formation
# scan; DLC = 0x80 + (fid - 0x100).
BOSS_MON = {
    0x1C: 0x67, 0x9C: 0x67, 0x7C: 0x3C, 0x7F: 0x69, 0x7D: 0x71,
    0x7A: 0x77, 0x73: 0x78, 0x79: 0x79, 0x74: 0x7A, 0x78: 0x7B,
    0x75: 0x7C, 0x77: 0x7D, 0x76: 0x7E, 0x7B: 0x7F,
    **{0x100 + i: 0x80 + i for i in range(0x11)},
}

# Formation position layouts (formation record byte0 -> static position array).
# Dispatch @0x08879254, jump table @0x0894BEC4, arrays @0x08948CAC+; an entry is
# 3 bytes (x, y, scale) and formation slot 0 (the boss) always takes ENTRY 0.
SMALL_GRID_LAYOUT = 0   # 9 packed small positions, column-major MID row first:
                        # entry0 = (0x10, 0x4c) -> a big boss sprite hangs down
                        # into the battle menu and eats its own damage popups.
TOP_GRID_LAYOUT = 5     # the SAME 9 coords, row-major TOP row first:
                        # entry0 = (0x10, 0x0c) -> boss sits high, popups clear.
                        # Vanilla users: fids 0x4A/0x96 only (array untouched).
BIG_GRID_LAYOUT = 2     # 4 spread big positions; entry0 = (0x10, 0x0c), already high

# intensity (option value) -> ordered add groups (role, min, max) drawn from the
# rolled (M, S) pool: M = the pool's minion, S = its super-hard pick. Counts
# collapse "3 minions" into ONE group so the 4-slot formation record (boss slot 0
# + at most 3 add groups) always fits.
#   light     = 1 minion
#   difficult = 3 minions
#   absurd    = 3 minions + 1 super-hard  (5 monsters -> needs the 9-slot grid)
_INTENSITY_ADDS = {
    1: [("M", 1, 1)],
    2: [("M", 3, 3)],
    3: [("M", 3, 3), ("S", 1, 1)],
}

# Bosses whose VANILLA formation is the 9-position SMALL grid (layout 0) and
# whose slot-0 boss is a SWARM (Piscodemon 2-7). They ALWAYS use the small grid,
# and because the swarm can fill positions before a late add group is reached,
# their adds are emitted super-hard-FIRST (so the S never clamps out) with a
# bumped minion count to help fill the 9 positions behind the swarm.
# Big bosses (single sprite, slot-0 count 1) use the small grid only at absurd,
# where 5 monsters need more than the 4 big-grid positions; lighter intensities
# keep the cleaner, overlap-free big grid.
SMALL_GRID_FIDS = frozenset({0x1C, 0x9C})   # Piscodemon (both formations)

# Per-intensity minion (min, max) for swarm bosses -- higher than the single-boss
# counts so the grid fills behind the 2-7 boss swarm (overflow just clamps at 9).
_SWARM_M_COUNT = {1: (2, 3), 2: (4, 6), 3: (4, 6)}


def all_fids():
    return tuple(sorted(f for _n, fids, _p in BOSS_POOL_SETS for f in fids))


def add_monster_ids():
    """Every monster id that can appear as an add (for GIM-availability tests)."""
    return sorted({mid for _n, _f, pools in BOSS_POOL_SETS
                   for (m, _), (s, _) in pools for mid in (m, s)})


def _swarm_groups(intensity, m, s):
    """Add groups for a swarm boss: super-hard S first (clamp guard), then the
    bumped minion count. Returns [[mon, min, max], ...]."""
    mlo, mhi = _SWARM_M_COUNT[intensity]
    ordered = sorted(_INTENSITY_ADDS[intensity], key=lambda a: a[0] != "S")
    out = []
    for role, lo, hi in ordered:
        if role == "S":
            out.append([s, lo, hi])
        else:
            out.append([m, mlo, mhi])
    return out


def roll_plan(intensity: int, rng) -> list:
    """Return the seed-static minion plan: [[fid, [[mon, min, max], ...], layout],
    ...] (JSON-safe for slot data). intensity = option value 1..3; rng = a seeded
    random.Random. One pool is rolled per BOSS (a boss's fid pair shares it)."""
    intensity = int(intensity)
    plan = {}
    for _name, fids, pools in BOSS_POOL_SETS:
        # Roll the pool FIRST (one rng draw per boss) so seed-static behavior is
        # identical regardless of the branch below.
        (m, _mn), (s, _sn) = pools[rng.randrange(len(pools))]
        if all(f in SMALL_GRID_FIDS for f in fids):          # swarm boss
            groups = _swarm_groups(intensity, m, s)
            layout = SMALL_GRID_LAYOUT
        else:                                                # single big boss
            groups = [[m if role == "M" else s, lo, hi]
                      for role, lo, hi in _INTENSITY_ADDS[intensity]]
            # absurd: 5 monsters need the 9-position grid, and a big boss must
            # take its TOP-first ordering (layout 5) so its damage numbers stay
            # above the battle menu (live-verified 2026-08-03 vs Tiamat).
            layout = (TOP_GRID_LAYOUT if intensity == 3
                      else BIG_GRID_LAYOUT)
        for fid in fids:
            plan[fid] = (groups, layout)
    return [[fid, plan[fid][0], plan[fid][1]] for fid in sorted(plan)]
