"""Map Size (user 2026-10-27): which dungeons a random world keeps, and how
much land it gets.

  tiny   ~1/3 of the seed's checks    small  ~2/3 of the checks
  normal every dungeon                huge   every dungeon, ~1.5x the land

The budget is TOTAL CHECKS, not a dungeon count: a tiny world may be the Sky
Castle and Whisperwind Cove, or Mount Gulg and the Sunken Shrine. Each seed
rolls its own target inside its size's band and walks the dungeons in a
random order, so no size settles on one fixed set; the bands never overlap,
so a tiny world is always smaller than a small one.

Never skipped: towns (shops sell the spells) -- except Lufenia (user
2026-10-28: its magic shop's two spells and the Rosetta Stone go with it) --
Cornelia, the Chaos Shrine, Bahamut (promotion, no checks) and Mount Duergar
(Nerrick opens the canal -- user: force it until a world without him is
proven). At least one Fiend's
dungeon always stays; a skipped Fiend takes its bonus dungeon with it, and a
bonus dungeon the yaml turned off is left off the map. Sarda needs the
Vampire, so he goes with the Cavern of Earth.

Pure module: logic tables only (no BaseClasses), so tests and the bench can
roll worlds directly.
"""
from .. import data as DATA
from .. import logic as L
from .. import randworld_territories as RWT
from .._locinfo import LOC_INFO

TINY, SMALL, NORMAL, HUGE = 0, 1, 2, 3
NAMES = {TINY: "tiny", SMALL: "small", NORMAL: "normal", HUGE: "huge"}

# share of the seed's site checks a world keeps: rolled per seed inside the
# band (user: "we can stray from the targets to add variance")
CHECK_BAND = {TINY: (0.24, 0.42), SMALL: (0.54, 0.80)}
OVERSHOOT = 0.03                    # of the total: how far one dungeon may push past the target
TRIES = 60
SHOP_SHARE = {TINY: 1 / 3, SMALL: 2 / 3}            # AP offers, extra stock, hints per shop
# the generator's knobs per size: land share, area spacing (sites per land),
# the start's walkable-tile cap
# (bench 2026-10-27, tools/randworld/bench_map_size.py: tiny ~40% and small
# ~60% of normal's walkable tiles, so sites sit closer than on normal)
LAND = {TINY: 0.42, SMALL: 0.62, NORMAL: 1.0, HUGE: 1.5}
SPACING = {TINY: 0.85, SMALL: 0.92, NORMAL: 1.0, HUGE: 1.22}
START_CAP = {TINY: 0.7, SMALL: 0.85, NORMAL: 1.0, HUGE: 1.3}

SUNKEN_REGIONS = tuple(r for r, (site, _e) in L.REGION_SITE.items()
                       if site == "onrac" and r.startswith("sunken_shrine"))

# unit -> what leaving it out removes. sites: overworld sites the map drops;
# regions: location regions beyond those sites' own (the Sunken Shrine is
# dived into from Onrac, which stays); fiend: the crystal its boss grants;
# bonus: bonus dungeon index; needs: units it cannot exist without.
UNITS = {
    "marsh":       dict(sites=("marsh",)),
    "ice":         dict(sites=("ice",)),
    "wkeep":       dict(sites=("wkeep",)),
    "matoya":      dict(sites=("matoya",)),
    "titan":       dict(sites=("titan",)),
    "trials":      dict(sites=("trials",)),
    "waterfall":   dict(sites=("waterfall",)),
    # each Dragon Caves chest cave is its own site and unit (user 2026-10-28)
    "cardia_plains": dict(sites=("cardia_plains",)),
    "cardia_marsh":  dict(sites=("cardia_marsh",)),
    "cardia_forest": dict(sites=("cardia_forest",)),
    "lufenia":     dict(sites=("lufenia",)),
    "earth":       dict(sites=("earth",), fiend=L.EARTH_CRYSTAL),
    "sarda":       dict(sites=("sarda",), needs=("earth",)),
    "gulg":        dict(sites=("gulg",), fiend=L.FIRE_CRYSTAL),
    "sunken":      dict(regions=SUNKEN_REGIONS, fiend=L.WATER_CRYSTAL),
    "mirage":      dict(sites=("mirage",), fiend=L.AIR_CRYSTAL),
    "earthgift":   dict(sites=("earthgift",), bonus=0, needs=("earth",)),
    "hellfire":    dict(sites=("hellfire",), bonus=1, needs=("gulg",)),
    "lifespring":  dict(sites=("lifespring",), bonus=2, needs=("sunken",)),
    "whisperwind": dict(sites=("whisperwind",), bonus=3, needs=("mirage",)),
}
BONUS_UNIT = {u["bonus"]: k for k, u in UNITS.items() if "bonus" in u}

# the bestiary's random-map dungeon keys (logic.RANDWORLD_DUNGEON_SITE) per unit
BESTIARY_DUNGEONS = {"marsh": ("marsh",), "earth": ("earth",), "titan": ("titan",),
                     "gulg": ("gurgu",),
                     "ice": ("ice",), "trials": ("trials",), "waterfall": ("waterfall",),
                     "mirage": ("mirage", "fortress"), "sunken": ("sunken",)}

# key items whose every use sits in these units: left out of the pool when
# ALL of them are skipped (the Crown opens Astos and the Citadel)
DEAD_ITEMS = ((L.EARTH_ROD, ("earth",)), (L.STAR_RUBY, ("titan",)),
              (L.CHIME, ("mirage",)), (L.WARP_CUBE, ("mirage",)),
              (L.OXYALE, ("sunken",)), (L.CROWN, ("wkeep", "trials")),
              (L.CRYSTAL_EYE, ("matoya",)),
              # Dr Unne's translation only opens the Lufenia Elder
              (L.ROSETTA, ("lufenia",)))


def _region_unit():
    out = {}
    for u, spec in UNITS.items():
        for r, (site, _e) in L.REGION_SITE.items():
            if site in spec.get("sites", ()):
                out[r] = u
        for r in spec.get("regions", ()):
            out[r] = u
    return out


REGION_UNIT = _region_unit()


def unit_checks(caps, removed_idx=frozenset()):
    """({unit: AP locations it holds}, total site checks) under the yaml's
    bonus caps ({dungeon index: cap}) and the chests the seed drops anyway.
    Shops and the Bestiary are not counted (shops shrink on their own)."""
    w = {u: 0 for u in UNITS}
    total = 0
    # a static boss chest is a check only while its dungeon holds dynamic
    # ones (logic.removed_static_dlc_idx)
    removed_idx = set(removed_idx) | L.removed_static_dlc_idx(caps)
    for _lid, _nm, idx in DATA.LOCATIONS:
        if idx in removed_idx:
            continue
        total += 1
        dg = L.DLC_STATIC_IDX_DUNGEON.get(idx)
        if dg is not None:
            w[BONUS_UNIT[dg]] += 1
            continue
        u = REGION_UNIT.get((LOC_INFO.get(idx) or (None, None))[0])
        if u:
            w[u] += 1
    for _nm, ordn, region, _x in L.NPC_LOCATIONS:
        total += 1
        u = REGION_UNIT.get(L.npc_region(ordn, region, skip_lute=True))
        if u:
            w[u] += 1
    for dg, n in caps.items():
        total += int(n)
        w[BONUS_UNIT[dg]] += int(n)
    return w, total


def pick(rng, size, checks, total):
    """frozenset of the units a world of `size` leaves out (empty for normal
    and huge). checks / total: unit_checks()."""
    band = CHECK_BAND.get(size)
    if band is None:
        return frozenset()
    lo, hi = band[0] * total, band[1] * total
    # a bonus dungeon the yaml turned off holds nothing: off the map
    off = {u for u, spec in UNITS.items() if "bonus" in spec and checks[u] <= 0}
    fixed = total - sum(checks.values())
    fiends = [u for u, spec in UNITS.items() if "fiend" in spec]
    best = None
    for _k in range(TRIES):
        target = rng.uniform(lo, hi)
        cap = target + OVERSHOOT * total
        order = sorted(fiends)
        rng.shuffle(order)
        first = next((u for u in order if fixed + checks[u] <= cap), min(order, key=checks.get))
        kept = {first}
        have = fixed + checks[first]
        rest = sorted(u for u in UNITS if u not in kept and u not in off)
        rng.shuffle(rest)
        grew = True
        while grew and have < target:
            grew = False
            for u in rest:
                if u in kept or not all(n in kept for n in UNITS[u].get("needs", ())):
                    continue
                if have + checks[u] > cap:
                    continue
                kept.add(u)
                have += checks[u]
                grew = True
                if have >= target:
                    break
        miss = 0 if lo <= have <= hi else min(abs(have - lo), abs(have - hi))
        if best is None or miss < best[0]:
            best = (miss, kept)
        if miss == 0:
            break
    return frozenset(u for u in UNITS if u not in best[1])


def shop_count(v, size):
    """A per-shop yaml count (AP offers / extra stock / hints) for a world
    of `size`: a third (tiny) or two thirds (small), rounded, never below 1
    when the yaml asks for any; normal and huge keep the yaml's."""
    share = SHOP_SHARE.get(size)
    if share is None or v <= 0:
        return v
    return max(1, round(v * share))


def kept_checks(checks, total, skipped):
    return total - sum(checks[u] for u in skipped)


def skipped_sites(skipped):
    return frozenset(s for u in skipped for s in UNITS[u].get("sites", ()))


def skipped_regions(skipped):
    return frozenset(r for r, u in REGION_UNIT.items() if u in skipped)


def skipped_bonus(skipped):
    """Bonus dungeon indices left out."""
    return frozenset(UNITS[u]["bonus"] for u in skipped if "bonus" in UNITS[u])


def skipped_dungeons(skipped):
    """logic.RANDWORLD_DUNGEON_SITE keys whose monsters no longer spawn."""
    return frozenset(d for u in skipped for d in BESTIARY_DUNGEONS.get(u, ()))


def crystals(skipped, bonus_crystals=False):
    """The crystals a world with these units left out can still earn
    (bonus_dungeon_crystals: the crystal sits in the Fiend's bonus dungeon)."""
    out = set()
    for u, spec in UNITS.items():
        c = spec.get("fiend")
        if c is None or u in skipped:
            continue
        if bonus_crystals:
            b = next(k for k, s in UNITS.items() if s.get("needs") == (u,) and "bonus" in s)
            if b in skipped:
                continue
        out.add(c)
    return out


def dead_items(skipped):
    return frozenset(item for item, units in DEAD_ITEMS if all(u in skipped for u in units))


# ---------------------------------------------------------- hunting grounds
# A tiny / small world folds its hubs together (user 2026-10-27): fewer
# encounter rows, less monster variance. Each fold joins a hub to its nearest
# neighbour, any tier; the EARLIER hub's row wins (its land takes the later
# one's), so Cornelia always stays Cornelia. A fold never removes the last
# hub of a difficulty band, so a world keeps early AND late fights.
HUB_KEEP = {TINY: (0.45, 0.55), SMALL: (0.70, 0.80)}      # share of the live hubs kept
# tiny + regional oceans: every outer quadrant rolls the SE pool, the central
# sea keeps its vanilla roll (iso_patcher._OC_POOL_ROWS 4; small keeps all)
TINY_OCEAN_POOL = 4
HUB_BANDS = ((0, 2), (3, 5), (6, 8))                       # early / mid / late tiers
ACROSS_WATER = 40                                          # extra distance between landmasses
NEAREST_PICK = (3, 2, 1)                                   # weights over the 3 nearest folds


def _band(tier):
    return next(k for k, (lo, hi) in enumerate(HUB_BANDS) if lo <= tier <= hi)


def _hub_rank(key):
    return (RWT.HUB_TIER[key], RWT.HUB_INDEX[key])


def plan_hub_merge(rng, size, hubs):
    """{hub key: hub key it folds into} for a world of `size` ({} for normal
    and huge). hubs: [(hub key, door (x, y), landmass id)] of the hubs this
    world placed (Territories.site_hub + comp)."""
    keep = HUB_KEEP.get(size)
    live = sorted({k for k, _xy, _c in hubs}, key=_hub_rank)
    if keep is None or len(live) < 2:
        return {}
    want = max(1, round(len(live) * rng.uniform(*keep)))
    doors = {}
    for k, xy, comp in hubs:
        doors.setdefault(k, []).append((xy, comp))
    group = {k: k for k in live}                   # hub -> the hub whose row it rolls

    def members(rep):
        return [k for k in live if group[k] == rep]

    def dist(a, b):
        best = None
        for m in members(a):
            for n in members(b):
                for (p, ca) in doors[m]:
                    for (q, cb) in doors[n]:
                        dx = abs(p[0] - q[0])
                        dy = abs(p[1] - q[1])
                        dx, dy = min(dx, RWT.N - dx), min(dy, RWT.N - dy)
                        d = (dx * dx + dy * dy) ** 0.5 + (0 if ca == cb else ACROSS_WATER)
                        best = d if best is None else min(best, d)
        return best

    while len(set(group.values())) > want:
        reps = sorted(set(group.values()), key=_hub_rank)
        bands = {}
        for r in reps:
            bands.setdefault(_band(RWT.HUB_TIER[r]), []).append(r)
        folds = []
        for i, a in enumerate(reps):
            for b in reps[i + 1:]:
                lose = max(a, b, key=_hub_rank)          # the later hub's row goes
                if len(bands[_band(RWT.HUB_TIER[lose])]) < 2:
                    continue                             # its band's last hub
                folds.append((dist(a, b), RWT.HUB_INDEX[a], RWT.HUB_INDEX[b], a, b))
        if not folds:
            break
        folds.sort()
        top = folds[:len(NEAREST_PICK)]
        _d, _ia, _ib, a, b = rng.choices(top, weights=NEAREST_PICK[:len(top)])[0]
        win, lose = sorted((a, b), key=_hub_rank)
        for k in live:
            if group[k] == lose:
                group[k] = win
    return {k: r for k, r in group.items() if k != r}


def hub_doors(terr):
    """plan_hub_merge's input from an (unmerged) Territories."""
    return [(k, xy, terr.comp[xy[1] * RWT.N + xy[0]])
            for k, xy, _h in terr.site_hub if k in RWT.HUB_INDEX and xy is not None]
