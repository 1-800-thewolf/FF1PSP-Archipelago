"""Stage C: decide progression on the AREA GRAPH, then have paint.py draw it.

The ladder of states a player passes through (v0's spheres):
  foot -> canoe -> ship -> ship+canoe -> canal -> airship
Each site is given a rung FIRST (steered toward per-seed location-share
targets whose median is vanilla's, user 2026-10-17), then areas are chosen
to realise those rungs by construction:
  foot        the start region (Cornelia's area + merged neighbours)
  canoe       an area beside the start region, joined by a river border
  ship        an area on the ship's sea with a dock
  ship+canoe  two areas sharing a river border that mouths on the ship's sea
  canal       an area on the canal's sea (beyond Nerrick's canal) with a dock
  airship     everything else (ridges all round, no dock, no river)
Borders between areas of different rungs are ridges; that is what makes the
rungs hold. verify.py contracts the result and re-derives every rung.
"""
import math

from . import grid as G
from . import paint as PT
from . import skeleton as SK
from . import tables as T

N, NN, NB4 = G.N, G.NN, G.NB4

LADDER = ("foot", "canoe", "ship", "ship+canoe", "canal", "airship")
RUNG = {c: k for k, c in enumerate(LADDER)}

FOOT_SITES = {0: (4, 6), 1: (4, 8), 2: (4, 10)}      # user 2026-10-16 (v0 links.FOOT_SITES)
START_CAP = 2600                                      # walkable start tiles (v0 world.START_CAP)
START_CAP_STEP = 800
CELLS_PER_SITE = 380
STYLES = (("paths", 0.5), ("two_basins", 0.25), ("valley", 0.25))
HARBOR_WAYS = (("foot", 0.4), ("canoe", 0.3), ("titan", 0.3))
SHARE_SIGMA = 0.35                                    # wiggle room around vanilla's shares
P_OPEN = 0.6                                         # same-rung neighbours merged
MIN_BORDER = 5                                        # shared cell pairs that make two areas neighbours

# sites that only work on certain rungs
RIVER_SITES = ("whisperwind", "waterfall")
SEA_SITE = "lifespring"


def allowed(site):
    if site == "cornelia":
        return ("foot",)
    if site == "duergar":                 # Nerrick blows the canal: never behind it
        return ("foot", "canoe", "ship", "ship+canoe")
    if site in RIVER_SITES:
        return ("canoe", "ship+canoe")
    if site == SEA_SITE:
        return ("ship", "canal")
    return LADDER


# ------------------------------------------------------------------ dice --
def _pick(rng, table):
    r = rng.random() * sum(w for _k, w in table)
    for k, w in table:
        r -= w
        if r <= 0:
            return k
    return table[-1][0]


def sample_targets(rng, base, sigma=SHARE_SIGMA):
    """Per-seed share targets: vanilla's shares times lognormal noise
    (median = vanilla), renormalised."""
    t = {c: max(base.get(c, 0.0), 0.01) * math.exp(rng.gauss(0, sigma)) for c in LADDER}
    s = sum(t.values())
    return {c: v / s for c, v in t.items()}


def roll(rng, opts, base_shares, ways=HARBOR_WAYS):
    """ways: the harbor-way table (gates.harbor_ways adds the Gaian door)."""
    op = int(opts.get("open_progression", 0))
    lo, hi = FOOT_SITES[op]
    return dict(
        foot_n=rng.randint(lo, min(hi, lo + 2 + op * 2)),
        style=_pick(rng, STYLES),
        harbor_way=_pick(rng, ways),
        targets=sample_targets(rng, base_shares),
        start_cap=START_CAP + START_CAP_STEP * op,
    )


# --------------------------------------------------------------- sites ----
def assign_sites(rng, weights, targets, foot_n, force=None):
    """{site: rung}. weights = AP locations per site (0 for none). force =
    {site: rung} decided elsewhere (gates.forced_rungs): kept as given."""
    force = {s: c for s, c in (force or {}).items() if s in weights}
    total = sum(weights.values()) or 1.0
    want = {c: targets[c] * total for c in LADDER}
    have = {c: 0.0 for c in LADDER}
    count = {c: 0 for c in LADDER}
    cat = {"cornelia": "foot"}
    have["foot"] += weights.get("cornelia", 0)
    count["foot"] = 1
    for s, c in force.items():
        cat[s] = c
        have[c] += weights[s]
        count[c] += 1
    order = [s for s in weights if s != "cornelia" and s not in force]
    rng.shuffle(order)
    order.sort(key=lambda s: -weights[s] * (0.7 + 0.6 * rng.random()))
    for s in order:
        best = None
        for c in allowed(s):
            if c == "foot" and count["foot"] >= foot_n:
                continue
            gap = (want[c] - have[c]) / total
            sc = gap - 0.6 * max(0.0, have[c] + weights[s] - want[c]) / total + 0.04 * rng.random()
            if best is None or sc > best[0]:
                best = (sc, c)
        c = best[1]
        cat[s] = c
        have[c] += weights[s]
        count[c] += 1
    # foot holds exactly foot_n sites: top up with the lightest movable sites
    while count["foot"] < foot_n:
        cand = [s for s, c in cat.items() if c != "foot" and "foot" in allowed(s) and s not in force]
        if not cand:
            break
        s = min(cand, key=lambda k: weights[k] + rng.random())
        have[cat[s]] -= weights[s]
        count[cat[s]] -= 1
        cat[s] = "foot"
        count["foot"] += 1
    # ship, canoe and airship each open >= 1 LOCATION (user 2026-10-13)
    for c in ("canoe", "ship", "airship"):
        # (a forced site stands behind a gate: it may never be the airship's)
        if any(cc == c and weights[s] > 0 and s not in force for s, cc in cat.items()):
            continue
        donors = [s for s, cc in cat.items() if cc not in ("foot", c) and c in allowed(s)
                  and weights[s] > 0 and sum(1 for k in cat.values() if k == cc) > 1
                  and s not in force]
        if donors:
            s = min(donors, key=lambda k: weights[k] + rng.random())
            cat[s] = c
    return cat


# --------------------------------------------------------------- areas ----
class Redo(Exception):
    """This skeleton cannot carry the design: roll a new one."""


class Design:
    """The area-level plan the painter realises."""

    def __init__(self):
        self.rung = {}            # area -> rung
        self.border = {}          # (a, b) -> RIDGE / RIVER_B / OPEN
        self.docks = []           # (area, sea id)
        self.site_area = {}       # site -> area (titan: first mouth)
        self.titan = None         # (area A, area B)
        self.start = set()
        self.harbor_area = None
        self.notes = []


def _cap(ar, a):
    return max(1, min(3, ar.size(a) // CELLS_PER_SITE))


class Graph:
    """Area adjacency + coasts by sea body."""

    def __init__(self, wm):
        self.wm = wm
        ar = wm.ar
        self.n = len(ar.areas)
        # a corner touch is no border: design only joins areas sharing >= 5
        # cell pairs (a 1-cell join is closed by any ridge beside it)
        self.nbr = [{b: n for b, n in A.nbr.items() if n >= MIN_BORDER} for A in ar.areas]
        sea = bytearray(0 if wm.sk.land[i] else 1 for i in range(NN))
        self.sea_lab, self.sea_size = G.label(sea)
        self.coast = [dict() for _ in range(self.n)]      # area -> {sea id: cells}
        for i in range(NN):
            a = ar.owner[i]
            if a < 0:
                continue
            for j in NB4[i]:
                s = self.sea_lab[j]
                if s >= 0:
                    self.coast[a][s] = self.coast[a].get(s, 0) + 1
        self.bcells = PT.borders(ar)
        allb = set()
        for cells in self.bcells.values():
            allb |= cells
        self.near_border = G.dilate(allb, 4)       # docks keep this far from any border
        self._dock = {}

    def dockable(self, a, sea_id):
        """Cells of area a where a dock piece fits facing sea_id (cached)."""
        k = (a, sea_id)
        if k not in self._dock:
            self._dock[k] = PT.dockable(self.wm, a, self.sea_lab, sea_id, self.near_border)                 if self.coast[a].get(sea_id, 0) else []
        return self._dock[k]

    def border_touches_sea(self, key, sea_id):
        return any(self.sea_lab[j] == sea_id for i in self.bcells.get(key, ()) for j in NB4[i])


def design(wm, dice, cats, rng, ship_sea, canal_sea):
    """Choose every area's rung and every border's kind for the site rungs
    in `cats`. Returns a Design (with notes for whatever had to give)."""
    ar = wm.ar
    g = Graph(wm)
    D = Design()
    a0 = ar.of(*SK.CORNELIA_AT)
    h0 = ar.of(*SK.HARBOR_LAND)
    d0 = ar.of(*SK.DESERT[len(SK.DESERT) // 2])
    D.harbor_area = h0
    need = {c: [s for s, cc in cats.items() if cc == c] for c in LADDER}

    def same_mass(a, b):
        return ar.areas[a].mass == ar.areas[b].mass

    # ---- foot: grow the start region from Cornelia's area
    S = [a0]
    way = dice["harbor_way"]
    walk_ratio = {"paths": 0.5}.get(dice["style"], 0.78)   # walkable share after carving
    cap_cells = dice["start_cap"] / walk_ratio
    if way == "foot":
        path = _area_path(g, a0, h0)
        if path is None or sum(ar.size(a) for a in path) > cap_cells:
            way = "canoe"
            D.notes.append("harbor foot path too long -> canoe")
        else:
            S = list(dict.fromkeys(path))
    target = CELLS_PER_SITE * len(need["foot"]) + 300
    cells = sum(ar.size(a) for a in S)
    while cells < target:
        cand = [b for a in S for b in g.nbr[a] if b not in S and b not in (h0, d0)
                and same_mass(a, b)]
        if not cand:
            break
        b = min(set(cand), key=lambda k: ar.size(k) * (0.6 + 0.8 * rng.random()))
        if cells + ar.size(b) > cap_cells and cells >= target * 0.6:
            break
        S.append(b)
        cells += ar.size(b)
    for a in S:
        D.rung[a] = "foot"
    D.start = set(S)

    # ---- the harbor's way when it is not simply part of the start
    if h0 not in D.start:
        if way == "foot":
            way = "canoe"
        adj = [a for a in S if h0 in g.nbr[a]]
        if not adj:
            # one more start area to touch the harbor area
            mid = [b for a in S for b in g.nbr[a] if b not in D.start and h0 in g.nbr[b]]
            if mid:
                b = mid[0]
                S.append(b)
                D.start.add(b)
                D.rung[b] = "foot"
                adj = [b]
        if not adj:
            path = _area_path(g, a0, h0)
            if path is None or sum(ar.size(a) for a in path) > cap_cells * 1.2:
                raise Redo("harbor area too far from the start")
            D.notes.append("harbor area not beside the start: harbor = foot via merge")
            for b in path:
                D.start.add(b)
                D.rung[b] = "foot"
            way = "foot"
        else:
            s = max(adj, key=lambda a: g.nbr[a][h0])
            key = (min(s, h0), max(s, h0))
            if way == "canoe" and g.nbr[s][h0] < 10:
                way = "titan"                  # too short a border for a real river
                D.notes.append("harbor border too short for a river -> titan")
            if way == "nitro" and g.nbr[s][h0] < 8:
                way = "titan"                  # no room for a boulder door
                D.notes.append("harbor border too short for a door -> titan")
            if way == "canoe":
                D.border[key] = PT.RIVER_B
                D.rung[h0] = "canoe"
            elif way == "nitro":
                # the Gaian door (gates.py): Gaia and the pier behind it
                D.border[key] = "door"
                D.rung[h0] = "nitro"
                D.harbor_door = (s, h0)
            else:
                D.titan = (s, h0)
                D.rung[h0] = "titan"            # reached through the tunnel
    elif way == "nitro":
        way = "foot"                           # the harbor ended up in the start
    D.harbor_way = way

    def capacity(c):
        return sum(_cap(ar, a) for a, r in D.rung.items() if r == c)

    def free(a):
        return a not in D.rung

    # ---- canoe: rivers out of the start region
    cand = [b for a in D.start for b in g.nbr[a] if free(b)]
    rng.shuffle(cand)
    for b in dict.fromkeys(cand):
        if capacity("canoe") >= len(need["canoe"]) and capacity("canoe") > 0:
            break
        s = max((a for a in D.start if b in g.nbr[a]), key=lambda a: g.nbr[a][b])
        if g.nbr[s][b] < (14 if not any(PR_r == "canoe" for PR_r in D.rung.values()) and
                          any(cats.get(x) == "canoe" for x in RIVER_SITES) else 6):
            continue
        D.border[(min(s, b), max(s, b))] = PT.RIVER_B
        D.rung[b] = "canoe"

    # ---- ship: docked areas on the ship's sea
    cand = [a for a in range(g.n) if free(a) and g.coast[a].get(ship_sea, 0) >= 4
            and g.dockable(a, ship_sea)]
    rng.shuffle(cand)
    for a in cand:
        if capacity("ship") >= len(need["ship"]):
            break
        D.rung[a] = "ship"
        D.docks.append((a, ship_sea))

    if not any(r == "ship" for r in D.rung.values()):
        raise Redo("no dockable area on the ship's sea")

    # ---- ship+canoe: a river border that mouths on the ship's sea
    pairs = [k for k in g.bcells if free(k[0]) and free(k[1])
             and g.border_touches_sea(k, ship_sea) and len(g.bcells[k]) >= 10]
    rng.shuffle(pairs)
    for k in pairs:
        if capacity("ship+canoe") >= len(need["ship+canoe"]):
            break
        if not (free(k[0]) and free(k[1])):
            continue
        D.border[k] = PT.RIVER_B
        D.rung[k[0]] = D.rung[k[1]] = "ship+canoe"

    # ---- canal: docks on the sea beyond the canal
    if canal_sea is not None:
        cand = [a for a in range(g.n) if free(a) and g.coast[a].get(canal_sea, 0) >= 4
                and g.dockable(a, canal_sea)]
        rng.shuffle(cand)
        for a in cand:
            if capacity("canal") >= max(1, len(need["canal"])):
                break
            D.rung[a] = "canal"
            D.docks.append((a, canal_sea))

    # ---- the airship desert: reachable before the airship (vanilla:
    #      Ship + Canoe). A chain of free areas from the desert to the
    #      nearest one with an early way in: its own dock, a river from the
    #      start, or a river link from an area already reached early.
    if free(d0) and dice.get("desert_gate"):
        D.desert_gate = True                   # gates.py walls it in (a door or the bridge)
        D.notes.append("desert behind a gate")
    elif free(d0):
        got = _desert_chain(g, D, d0, ship_sea, canal_sea, rng)
        if got is None:
            D.notes.append("desert area has no early way in")

    # ---- airship: the rest
    for a in range(g.n):
        if free(a):
            D.rung[a] = "airship"

    # ---- borders: rivers as designed, same rung may merge, else ridge
    for k in g.bcells:
        if k in D.border:
            continue
        a, b = k
        ra, rb = D.rung[a], D.rung[b]
        if a in D.start and b in D.start:
            D.border[k] = PT.OPEN
        elif ra == rb and ra not in ("titan", "nitro") and rng.random() < P_OPEN:
            D.border[k] = PT.OPEN
        else:
            D.border[k] = PT.RIDGE
    if dice["style"] == "two_basins" and len(D.start) >= 2:
        D.notes.append("two basins")
    D.graph = g
    wm.regions = regions_of(D, g.n)
    return D


def regions_of(D, n):
    """Union-find over OPEN borders: {area: frozenset of its region}."""
    parent = list(range(n))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for (a, b), kind in D.border.items():
        if kind == PT.OPEN:
            parent[find(a)] = find(b)
    groups = {}
    for a in range(n):
        groups.setdefault(find(a), set()).add(a)
    return {a: frozenset(groups[find(a)]) for a in range(n)}


_LINK_RUNG = {"foot": "canoe", "canoe": "canoe", "ship": "ship+canoe", "ship+canoe": "ship+canoe"}


def _desert_chain(g, D, d0, ship_sea, canal_sea, rng):
    """BFS from the desert's area over free areas to the nearest area with
    an early way in; open the chain, give it that way. Returns the rung."""
    prev = {d0: None}
    q = [d0]
    k = 0
    while k < len(q):
        x = q[k]
        k += 1
        way = None
        if g.dockable(x, ship_sea):
            way = ("dock", ship_sea, "ship")
        else:
            nb = [b for b in g.nbr[x] if D.rung.get(b) in _LINK_RUNG and g.nbr[x][b] >= 6]
            if nb:
                b = min(nb, key=lambda c: RUNG.get(D.rung[c], 9))
                way = ("river", b, _LINK_RUNG[D.rung[b]])
            elif canal_sea is not None and g.dockable(x, canal_sea):
                way = ("dock", canal_sea, "canal")
        if way is not None:
            chain = []
            u = x
            while u is not None:
                chain.append(u)
                u = prev[u]
            rung = way[2]
            for u in chain:
                D.rung[u] = rung
            for u, v in zip(chain, chain[1:]):
                D.border[(min(u, v), max(u, v))] = PT.OPEN
            if way[0] == "dock":
                D.docks.append((x, way[1]))
            else:
                b = way[1]
                D.border[(min(x, b), max(x, b))] = PT.RIVER_B
            return rung
        for y in g.nbr[x]:
            if y not in prev and y not in D.rung:
                prev[y] = x
                q.append(y)
    return None


def _area_path(g, a, b):
    """Shortest chain of adjacent areas from a to b (same landmass)."""
    prev = {a: None}
    q = [a]
    k = 0
    while k < len(q):
        u = q[k]
        k += 1
        if u == b:
            out = []
            while u is not None:
                out.append(u)
                u = prev[u]
            return out[::-1]
        for v in g.nbr[u]:
            if v not in prev:
                prev[v] = u
                q.append(v)
    return None


def rebalance(D, cats, ar, rng, weights=None, fixed=()):
    """A rung whose areas cannot hold its sites (capacity = sum of _cap,
    with 50% slack) hands the overflow to the nearest rungs with room,
    LATER rungs first for the canal (its sea may be one small island),
    earlier first otherwise. Respects allowed(); updates cats in place.
    fixed: sites pinned behind gates (gates.Plan.pins), never moved."""
    have = {c: [a for a, r in D.rung.items() if r == c] for c in LADDER}
    have["foot"] = sorted(D.start)
    cap = {c: int(sum(_cap(ar, a) for a in have[c]) * 1.5) + (1 if have[c] else 0) for c in LADDER}
    load = {c: sum(1 for x in cats.values() if x == c) for c in LADDER}
    must_pay = ("canoe", "ship", "canal", "airship")
    wts = weights or {}
    for c in LADDER:
        over = load[c] - cap[c]
        if over <= 0 or c == "foot":
            continue
        k = RUNG[c]
        sites = [s for s, x in cats.items() if x == c and s != "cornelia" and s not in fixed]
        # big stamps leave first; the smallest LOCATION site always stays
        sites.sort(key=lambda s: -site_size(s) - rng.random())
        if c in must_pay:
            locs = [s for s in sites if wts.get(s, 1) > 0]
            if locs:
                keep = min(locs, key=site_size)
                sites.remove(keep)
        for s in sites[:over]:
            order = sorted(LADDER, key=lambda x: (abs(RUNG[x] - k)
                           + (0.5 if (RUNG[x] < k) == (c == "canal") else 0)))
            for c2 in order:
                if c2 != c and c2 != "foot" and c2 in allowed(s) and load[c2] < cap[c2]:
                    cats[s] = c2
                    load[c] -= 1
                    load[c2] += 1
                    D.notes.append(f"{c} full: {s} -> {c2}")
                    break


def site_size(s):
    """Window cells a site's stamp needs (fixed tiles' bounding box + ring)."""
    from . import tables as T
    if s in T.SEA_SITES:
        return 1
    best = 0
    for p in T.SITES[s]["parts"]:
        xs = [i for i, _j, _t in p["fixed"]] + [i for i, _j, _c in p["hints"]]
        ys = [j for _i, j, _t in p["fixed"]] + [j for _i, j, _c in p["hints"]]
        best = max(best, (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1))
    return best


def site_areas(D, cats, ar, rng, pins=None):
    """{site: area} spreading each rung's sites over that rung's areas. A
    rung with no area hands its sites to the nearest rung that has one,
    EARLIER rungs first (Nerrick must stay ahead of his canal), within what
    the site allows. cats is updated in place. pins = {site: area} of the
    sites walled in behind gates (gates.Plan.pins); a gated area takes no
    other site."""
    pins = dict(pins or {})
    gated = getattr(D, "gated", None) or {}
    out = dict(pins)
    load = {a: 1 for a in pins.values()}
    have = {c: [a for a, r in D.rung.items() if r == c and a not in gated] for c in LADDER}
    have["foot"] = sorted(D.start)
    for s, c in list(cats.items()):
        if have.get(c) or s in pins:
            continue
        k = RUNG[c]
        order = sorted(LADDER, key=lambda x: (abs(RUNG[x] - k) + (0.5 if RUNG[x] > k else 0)))
        for c2 in order:
            if have.get(c2) and c2 in allowed(s):
                cats[s] = c2
                D.notes.append(f"no {c} area: {s} -> {c2}")
                break
    # every rung that must open a location gets one site that FITS its areas
    for c in ("canoe", "ship", "canal", "airship"):
        areas = have.get(c)
        if not areas:
            continue
        biggest = max(ar.size(a) for a in areas)
        mine = [s for s, cc in cats.items() if cc == c and site_size(s) * 2.5 <= biggest
                and s not in pins]
        if mine:
            continue
        donors = [s for s, cc in cats.items() if cc not in ("foot", c) and c in allowed(s)
                  and s not in RIVER_SITES and s != SEA_SITE and site_size(s) * 2.5 <= biggest
                  and sum(1 for x in cats.values() if x == cc) > 1 and s not in pins]
        if donors:
            s = min(donors, key=lambda k: site_size(k) + rng.random())
            D.notes.append(f"{c} needs a site that fits: {s} {cats[s]} -> {c}")
            cats[s] = c
    room = {}
    for c in LADDER:
        sites = [s for s, cc in cats.items() if cc == c and s not in pins]
        areas = have.get(c) or sorted(D.start)
        # biggest stamps first, each to the area with the most room left
        sites.sort(key=lambda s: -site_size(s) - rng.random())
        for s in sites:
            if s == "cornelia":
                out[s] = ar.of(*SK.CORNELIA_AT)
                load[out[s]] = load.get(out[s], 0) + 1
                continue
            need = site_size(s) * 2.5
            fit = [a for a in areas if room.get(a, ar.size(a)) >= need] or areas
            a = min(fit, key=lambda k: (load.get(k, 0) + 0.5) / _cap(ar, k) + 0.01 * rng.random())
            out[s] = a
            load[a] = load.get(a, 0) + 1
            room[a] = room.get(a, ar.size(a)) - need
    return out
