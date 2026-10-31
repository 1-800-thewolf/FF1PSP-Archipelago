"""Stage B: split every landmass into foot AREAS (RANDWORLD_V2_DESIGN.md section 3).

Areas are geodesic Voronoi cells of Poisson-disk seeds, with a noisy step
cost so the borders wander like ridge lines instead of straight bisectors.
The anchors (Cornelia, the harbor landing, the airship desert) seed their own
areas. Later stages decide what each BORDER becomes (ridge, river, open).
"""
from . import grid as G
from . import noise
from . import skeleton as SK

N, NN, NB4 = G.N, G.NN, G.NB4

SPACING = {"vanilla": 32, "archipelago": 27, "pangaea": 36}
MIN_AREA = 90             # smaller Voronoi cells merge into a neighbour


class Area:
    __slots__ = ("id", "cells", "mass", "seed", "nbr", "coast")

    def __init__(self, aid, seed):
        self.id = aid
        self.seed = seed           # flat index
        self.cells = []
        self.mass = -1             # landmass id
        self.nbr = {}              # area id -> shared border length (cell pairs)
        self.coast = 0             # land cells of this area beside sea


class Areas:
    """owner[i] = area id of land cell i (-1 at sea)."""

    def __init__(self, sk, owner, areas, mass, mass_sizes):
        self.sk = sk
        self.owner = owner
        self.areas = areas
        self.mass = mass
        self.mass_sizes = mass_sizes

    def of(self, x, y):
        return self.owner[G.idx(x, y)]

    def size(self, a):
        return len(self.areas[a].cells)


def _poisson(cells, r, rng, fixed=()):
    """Accept cells in random order when no accepted one lies within r."""
    cs = r
    buckets = {}
    out = []

    def ok(i):
        x, y = G.xy(i)
        bx, by = x // cs, y // cs
        nbk = (N + cs - 1) // cs
        for ddx in (-1, 0, 1):
            for ddy in (-1, 0, 1):
                for j in buckets.get(((bx + ddx) % nbk, (by + ddy) % nbk), ()):
                    if G.tdist2(G.xy(j), (x, y)) < r * r:
                        return False
        return True

    def add(i):
        x, y = G.xy(i)
        nbk = (N + cs - 1) // cs
        buckets.setdefault(((x // cs) % nbk, (y // cs) % nbk), []).append(i)
        out.append(i)

    for i in fixed:
        add(i)
    order = list(cells)
    rng.shuffle(order)
    for i in order:
        if ok(i):
            add(i)
    return out


def build(sk, rng, spacing=None):
    land = sk.land
    r = spacing or SPACING.get(sk.shape, 27)
    mass, mass_sizes = G.label(land)
    anchors = [G.idx(*SK.CORNELIA_AT), G.idx(*SK.HARBOR_LAND), G.idx(*SK.AIRSHIP_SEED)]
    cells = [i for i in range(NN) if land[i]]
    seeds = _poisson(cells, r, rng, fixed=anchors)
    # landmasses no seed landed on get one at a random cell
    seeded = {mass[s] for s in seeds}
    for i in cells:
        if mass[i] not in seeded:
            seeded.add(mass[i])
            seeds.append(i)
    # noisy step cost: borders follow the noise's high ground, like ridge
    # lines, instead of straight bisectors
    # strong multi-octave step cost: fronts race along the cheap valleys and
    # stall on the expensive ridges, so borders meander instead of running
    # as straight bisectors between seeds (the "cell web" look)
    warp = noise.fbm(rng, 6, 5, gain=0.6)
    lo, hi = min(warp), max(warp)
    cost = [1 + int(29.99 * ((w - lo) / (hi - lo)) ** 3) for w in warp]
    owner, _d = G.voronoi(land, seeds, cost, maxc=30)
    # each anchor's own cells belong to the anchor's area, whatever the
    # noisy Voronoi said (the desert with its halo, the pier + landing row,
    # Cornelia's castle window)
    for k, group in ((0, SK.CORNELIA_CELLS), (1, SK.HARBOR_CELLS), (2, SK.DESERT_CELLS)):
        for i in group:
            if land[i]:
                owner[i] = k
    # the canal's walker crossing (window + the land rows north and south of
    # it) is ONE area's ground: walkers cross there whether the canal is open
    # or not, so it must never join two areas by accident
    if sk.basin is not None:
        ox, oy = sk.basin.canal_origin
        from . import basin as BS
        cells = [G.idx(ox + i, oy + j) for j in range(-3, BS.CH + 3) for i in range(-1, BS.CW + 1)]
        north = next((owner[c] for c in cells if land[c] and owner[c] >= 0), -1)
        if north >= 0:
            for c in cells:
                if land[c]:
                    owner[c] = north
    owner = _merge_small(owner, len(seeds), anchors, seeds)
    # renumber densely
    remap = {}
    areas = []
    for i in range(NN):
        o = owner[i]
        if o < 0:
            continue
        if o not in remap:
            remap[o] = len(areas)
            areas.append(Area(remap[o], seeds[o]))
        a = remap[o]
        owner[i] = a
        areas[a].cells.append(i)
    for A in areas:
        A.mass = mass[A.cells[0]]
    for i in range(NN):
        a = owner[i]
        if a < 0:
            continue
        for j in NB4[i]:
            b = owner[j]
            if b < 0:
                areas[a].coast += 1
            elif b != a:
                areas[a].nbr[b] = areas[a].nbr.get(b, 0) + 1
    return Areas(sk, owner, areas, mass, mass_sizes)


def _merge_small(owner, n, anchors, seeds):
    """Fold Voronoi cells under MIN_AREA into the neighbour they share the
    longest border with (anchor cells are never folded away)."""
    keep = {seeds.index(a) for a in anchors if a in seeds}
    for _round in range(4):
        size = [0] * n
        for o in owner:
            if o >= 0:
                size[o] += 1
        border = {}
        for i in range(NN):
            a = owner[i]
            if a < 0 or size[a] >= MIN_AREA or a in keep:
                continue
            for j in NB4[i]:
                b = owner[j]
                if b >= 0 and b != a:
                    d = border.setdefault(a, {})
                    d[b] = d.get(b, 0) + 1
        if not border:
            break
        into = {a: max(d, key=d.get) for a, d in border.items()}
        # resolve chains a->b->c
        for a in list(into):
            seen = {a}
            b = into[a]
            while b in into and b not in seen:
                seen.add(b)
                b = into[b]
            into[a] = b
        for i in range(NN):
            o = owner[i]
            if o in into:
                owner[i] = into[o]
    return owner
