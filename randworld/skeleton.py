"""Stage A: the land / sea skeleton (RANDWORLD_V2_DESIGN.md section 3).

No shape is forced around the start. The noise field is TRANSLATED on the
torus until the three fixed anchors fall where they need to be: Cornelia deep
inside a big landmass, the ship's pier at a coast, the airship desert on land.
Then only the anchor cells themselves are pinned.

Coast rules (user 2026-10-17): 1-tile-wide land fingers into the sea are
removed; one may survive now and then.
"""
from . import basin as BS
from . import grid as G
from . import noise
from . import tables as T

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8

COAST_BLUR = 2                             # box-blur radius on the height field (0 = off)
# A closed edge keeps a band of open ocean, as in vanilla (rows 0/254 all sea;
# the x edge is sea except Lufenia's rows 25-42). It once guarded every edge:
# grid column 254 seemed skipped in game (column 253 meeting column 0). It is
# not -- the engine's map starts one cell before ours, so its column 0 is our
# column 254 one row down, and the bake now files that column one row up
# (client/randworld_bake.disc_cells): the game's map is this 255-torus.
SEAM_BAND = 2
# Open edges (user 2026-10-26: "sea around all borders is good, but land
# wrapping is also fine", every shape): each world rolls which axes stay
# closed. An open axis has no sink, no band and no translation penalty, so
# the land runs across that edge wherever the noise does -- land wrapping
# round a central sea, or land everywhere round one big lake.
OPEN_EDGES = ((0.5, ""), (0.15, "x"), (0.15, "y"), (0.2, "xy"))
# Shores near the map's edge (user 2026-10-25: "too many straight lines
# especially at edges"). The band used to be the only thing keeping land off
# the edge, so a landmass that crossed it was cut flat. Now the height field
# sinks past a margin line whose distance from the edge follows its own noise,
# with a short ramp: high land (a plateau) ends close to that line, which bends
# in and out in big bays and small capes, instead of tracing one long shore
# parallel to the edge. The band cut stays as the last guard.
EDGE_MARGIN = (9, 30)          # tiles from the edge to the margin line (low end >= SEAM_BAND + EDGE_RAMP)
EDGE_NOISE = (7, 3, 0.6)       # its fbm (lattice, octaves, gain): ~36-tile bays down to ~9-tile capes
EDGE_RAMP = 7                  # tiles from the margin line to the full sink
EDGE_SINK = 1.1                # full sink, in units of the field's span above sea level
EDGE_AVOID = 2.5               # translation score: weight against land inside the margin
OFFSET_TRIES = 12              # best translations re-checked after the sink
# a mild domain warp everywhere (same request): bays and peninsulas at the
# 10-30 tile scale; COAST_BLUR still removes the cell-scale wiggle
WARP_BASE = 7                  # lattice of the warp field (~36-tile bends)
WARP_AMP = 9.0                 # tiles (typical push ~3-5)
SHAPES = {
    #               lattice  land   octaves  min start landmass
    "vanilla":     dict(base=5, land=0.40, octaves=5, start_mass=1600),
    "archipelago": dict(base=8, land=0.31, octaves=5, start_mass=700),
    "pangaea":     dict(base=3, land=0.58, octaves=5, start_mass=3000),
}

LAND_MAX = 0.75               # the most land a scaled shape may hold (a huge one-continent world)

CORNELIA = T.SITES["cornelia"]["parts"][0]
_cs = CORNELIA["src"]
CORNELIA_AT = (_cs[0] + CORNELIA["ents"][0][0], _cs[1] + CORNELIA["ents"][0][1])
A = T.ANCHORS
HARBOR_LAND = tuple(A["harbor_land"])
HARBOR_WATER = [tuple(q) for q in A["harbor_water"]]
HARBOR_DOCK = [(x, y) for x, y, _t in A["harbor_dock"]]
DESERT = [(x, y) for x, y, _t in A["airship_desert"]]
AIRSHIP_SEED = tuple(A["airship_seed"])
FINGER_KEEP_P = 0.2        # chance that one finger survives (user: "occasionally")
# cells each anchor's area must own (areas.build)
_cw = CORNELIA["src"]
CORNELIA_CELLS = [G.idx(_cw[0] + i, _cw[1] + j) for j in range(_cw[3]) for i in range(_cw[2])]
HARBOR_CELLS = [G.idx(x, y) for x in range(HARBOR_LAND[0] - 3, HARBOR_LAND[0] + 5)
                for y in range(HARBOR_LAND[1] - 2, HARBOR_LAND[1] + 3)]
DESERT_CELLS = sorted({i for q in DESERT for i in G.disk(q[0], q[1], 3.5)})


class Skeleton:
    """land[i] = 1 for land. pinned: cells nothing later may flip.
    basin: the canal's inland sea (basin.Basin) and canal_dir what was built."""

    def __init__(self, land, pinned, shape, offset, fingers_kept, basin=None, canal_dir=None,
                 open_edges=""):
        self.land = land
        self.pinned = pinned
        self.shape = shape
        self.offset = offset
        self.fingers_kept = fingers_kept
        self.basin = basin
        self.canal_dir = canal_dir
        self.open_edges = open_edges           # "", "x", "y" or "xy": axes land may cross


def edge_dist(x, y, open_edges=""):
    """Cells from (x, y) to the nearest CLOSED edge (N when none is)."""
    d = N
    if "x" not in open_edges:
        d = min(d, x, N - 1 - x)
    if "y" not in open_edges:
        d = min(d, y, N - 1 - y)
    return d


def roll_open_edges(rng):
    r = rng.random()
    for p, axes in OPEN_EDGES:
        if r < p:
            return axes
        r -= p
    return OPEN_EDGES[-1][1]


def _majority(land, keep):
    """One 8-neighbour majority pass (rounds the coast's stairs)."""
    out = bytearray(land)
    for i in range(NN):
        if keep[i]:
            continue
        s = sum(land[j] for j in NB8[i])
        if s >= 5:
            out[i] = 1
        elif s <= 3:
            out[i] = 0
    return out


def fingers(land):
    """Land cells that are 1 tile wide: sea on both sides E/W or N/S."""
    out = []
    for i in range(NN):
        if land[i]:
            e, w, s, n = NB4[i]
            if (not land[e] and not land[w]) or (not land[s] and not land[n]):
                out.append(i)
    return out


def _diagonal_pinches(land):
    """2x2 windows holding land on one diagonal only (land joined at a
    corner): a shape no tile can draw."""
    out = []
    for i in range(NN):
        e, _w, s, _n = NB4[i]
        se = NB8[i][4]
        a, b, c, d = land[i], land[e], land[s], land[se]
        if a and d and not b and not c:
            out.append((i, e, s, se))
        elif b and c and not a and not d:
            out.append((i, e, s, se))
    return out


def tidy(land, pinned, rng, finger_p=FINGER_KEEP_P):
    """Remove fingers, corner pinches, specks and pinholes until stable.
    Returns the number of fingers deliberately kept (0 or 1)."""
    kept = 0
    keep_cells = set()
    if rng.random() < finger_p:
        fs = fingers(land)
        if fs:
            start = rng.choice(fs)
            # the whole finger: 1-wide cells 4-connected to it
            fset = set(fs)
            stack, comp = [start], set()
            while stack:
                i = stack.pop()
                if i in comp or i not in fset:
                    continue
                comp.add(i)
                stack.extend(NB4[i])
            if 2 <= len(comp) <= 6:
                keep_cells = comp
                kept = 1
    for _ in range(12):
        changed = 0
        for i in fingers(land):
            if not pinned[i] and i not in keep_cells:
                land[i] = 0
                changed += 1
        for quad in _diagonal_pinches(land):
            # join the corner: turn one of the sea cells to land
            for j in quad:
                if not land[j] and not pinned[j]:
                    land[j] = 1
                    changed += 1
                    break
        if not changed:
            break
    # specks of land and pinholes of sea
    lab, sizes = G.label(land)
    for i in range(NN):
        if land[i] and sizes[lab[i]] < 12 and not pinned[i]:
            land[i] = 0
    sea = bytearray(1 - v for v in land)
    lab, sizes = G.label(sea)
    for i in range(NN):
        if sea[i] and sizes[lab[i]] < 8 and not pinned[i]:
            land[i] = 1
    return kept


def _score_offset(lab, sizes, landf, ox, oy, shape, canal_dir="second", start_mass=None):
    """How well the anchors sit for a translation (ox, oy): None = unusable.
    start_mass: the start landmass's floor (None: the shape's own)."""
    def at(p):
        return G.idx(p[0] - ox, p[1] - oy)
    c = at(CORNELIA_AT)
    if not landf[c]:
        return None
    mass = sizes[lab[c]]
    if mass < (SHAPES[shape]["start_mass"] if start_mass is None else start_mass):
        return None
    # Cornelia not on the coast: land within 6 tiles all round
    cx, cy = CORNELIA_AT
    ring = sum(landf[G.idx(cx - ox + dx, cy - oy + dy)]
               for dx in range(-6, 7, 3) for dy in range(-6, 7, 3))
    if ring < 20:
        return None
    # soft: the pier near a coast (its water is pinned anyway; a far coast
    # means a long dug channel), the landing on land, the desert on land
    hx, hy = HARBOR_LAND
    sea_d = 12
    for d in range(1, 12):
        if any(not landf[G.idx(hx - ox + dx, hy - oy + dy)]
               for dx in range(-d, d + 1) for dy in (-d, d)) or any(
               not landf[G.idx(hx - ox + dx, hy - oy + dy)]
               for dy in range(-d, d + 1) for dx in (-d, d)):
            sea_d = d
            break
    dl = sum(landf[at(q)] for q in DESERT) / len(DESERT)
    if lab[at(HARBOR_LAND)] != lab[c]:
        return None                    # the harbor shares Cornelia's landmass
    if canal_dir == "inner":
        # the harbor's water becomes an inland sea: land all round the
        # basin south of the pier, and the ocean within a channel's reach
        bx, by = HARBOR_LAND[0], HARBOR_LAND[1] + 17
        disk = [(dx, dy) for dx in range(-22, 23, 4) for dy in range(-22, 23, 4)
                if dx * dx + dy * dy <= 22 * 22]
        frac = sum(landf[G.idx(bx - ox + dx, by - oy + dy)] for dx, dy in disk) / len(disk)
        reach = any(not landf[G.idx(bx - ox + side * k, by - oy)]
                    for side in (1, -1) for k in range(24, 44))
        if frac < 0.65 or not reach:
            return None
        return 4.0 * frac + 3.0 * dl
    return 3.0 * (1 - sea_d / 12) + 1.5 * landf[at(HARBOR_LAND)] + 3.0 * dl


_EDGE_SAMPLES = {
    "x": [q for k in range(0, N, 5) for d in (3, 8, 13) for q in ((d, k), (N - 1 - d, k))],
    "y": [q for k in range(0, N, 5) for d in (3, 8, 13) for q in ((k, d), (k, N - 1 - d))],
}


def _edge_land(landf, ox, oy, open_edges=""):
    """Share of the closed edges' margin that would hold land under
    translation (ox, oy)."""
    pts = [q for a in "xy" if a not in open_edges for q in _EDGE_SAMPLES[a]]
    if not pts:
        return 0.0
    return sum(landf[G.idx(x - ox, y - oy)] for x, y in pts) / len(pts)


def edge_sink(rng, open_edges=""):
    """Per cell (world coordinates) 0..1: how far past the margin line it
    sits (EDGE_RAMP tiles to 1), to the power 1.5. The line's distance from
    the edge spans EDGE_MARGIN (its noise ranked over that range), and the
    nearest line point is at least one ramp off the band, so the band always
    sinks fully. Open edges do not sink."""
    lo, hi = EDGE_MARGIN
    u = noise.fbm(rng, *EDGE_NOISE)
    # equalised to ranks: the line spends equal time at every depth (a
    # min/max stretch leaves it near the middle, a clipped one draws flats)
    rank = [0.0] * NN
    for k, i in enumerate(sorted(range(NN), key=u.__getitem__)):
        rank[i] = k / (NN - 1)
    out = [0.0] * NN
    for y in range(N):
        for x in range(N):
            d = edge_dist(x, y, open_edges)
            if d >= hi:
                continue
            i = y * N + x
            m = lo + (hi - lo) * rank[i]
            if d < m:
                out[i] = min(1.0, (m - d) / EDGE_RAMP) ** 1.5
    return out


def _land_at(h, ox, oy, sink, sea_level, land_share):
    """The height field translated by (ox, oy), sunk along the edges and
    thresholded back to the shape's land share."""
    ht = [0.0] * NN
    for y in range(N):
        sy = ((y - oy) % N) * N
        row = y * N
        for x in range(N):
            ht[row + x] = h[sy + (x - ox) % N]
    k = EDGE_SINK * (max(ht) - sea_level)
    for i in range(NN):
        if sink[i]:
            ht[i] -= k * sink[i]
    lvl = noise.quantile(ht, 1 - land_share)
    return bytearray(1 if v > lvl else 0 for v in ht)


def build(rng, shape="vanilla", canal_dir="second", open_edges=None, land_scale=1.0):
    """open_edges: "", "x", "y" or "xy" (None: rolled here, OPEN_EDGES).
    land_scale: the shape's land share (and its start landmass floor) times
    this (Map Size, randworld/sizing.py); 1.0 = the shape as it is."""
    S = dict(SHAPES[shape])
    if land_scale != 1.0:
        S["land"] = min(LAND_MAX, S["land"] * land_scale)
        S["start_mass"] = int(S["start_mass"] * land_scale)
    h = noise.fbm(rng, S["base"], S["octaves"])
    fine = noise.fbm(rng, 40, 2)
    for i in range(NN):
        h[i] += 0.05 * (fine[i] - 0.5)
    if WARP_AMP:
        h = noise.warp(h, rng, WARP_BASE, WARP_AMP)
    if COAST_BLUR:
        # vanilla's coasts are smooth curves drawn with REGULAR stair steps;
        # the top octaves left 1-3 cell wiggles that tile as a bumpy, jagged
        # shore (playtest 2026-10-17). Blur before the threshold: the shape
        # stays, the cell-scale wiggle goes.
        h = G.box_blur(h, COAST_BLUR)
    sea_level = noise.quantile(h, 1 - S["land"])
    landf = bytearray(1 if v > sea_level else 0 for v in h)
    lab, sizes = G.label(landf)
    if open_edges is None:
        open_edges = roll_open_edges(rng)
    sink = edge_sink(rng, open_edges)
    land = None
    for d in ((canal_dir, "second") if canal_dir == "inner" else ("second",)):
        cands = []
        for _ in range(900):
            ox, oy = rng.randrange(N), rng.randrange(N)
            s = _score_offset(lab, sizes, landf, ox, oy, shape, d, S["start_mass"])
            if s is not None:
                cands.append((s - EDGE_AVOID * _edge_land(landf, ox, oy, open_edges), ox, oy))
        cands.sort(reverse=True)
        # the sink reshapes the margin (and the threshold moves a little):
        # the anchors must still fit the final land
        for _s, ox, oy in cands[:OFFSET_TRIES]:
            cand = _land_at(h, ox, oy, sink, sea_level, S["land"])
            lab2, sizes2 = G.label(cand)
            if _score_offset(lab2, sizes2, cand, 0, 0, shape, d, S["start_mass"]) is not None:
                land, canal_dir, off = cand, d, (ox, oy)
                break
        if land is not None:
            break
    if land is None:
        return None
    for y in range(N):
        for x in range(N):
            if edge_dist(x, y, open_edges) < SEAM_BAND:
                land[y * N + x] = 0
    pinned = bytearray(NN)
    for q in DESERT:                       # desert + a land halo (never beside water)
        for i in G.disk(q[0], q[1], 2.5):
            land[i] = 1
        pinned[G.idx(*q)] = 1
    for q in HARBOR_DOCK:
        i = G.idx(*q)
        land[i] = 1
        pinned[i] = 1
    for q in HARBOR_WATER:
        i = G.idx(*q)
        land[i] = 0
        pinned[i] = 1
    for x in range(HARBOR_LAND[0] - 2, HARBOR_LAND[0] + 4):     # landing row
        i = G.idx(x, HARBOR_LAND[1])
        land[i] = 1
        pinned[i] = 1
    for i in G.disk(CORNELIA_AT[0], CORNELIA_AT[1] - 3, 7):     # Cornelia's stamp sits on land
        land[i] = 1
        pinned[i] = 1
    land = _majority(land, pinned)
    avoid = [(CORNELIA_AT[0], CORNELIA_AT[1], 40), (HARBOR_LAND[0], HARBOR_LAND[1], 28),
             (DESERT[0][0], DESERT[0][1], 24)]
    basin, built = BS.build(land, pinned, rng, canal_dir, HARBOR_WATER, avoid)
    if basin is None:
        return None
    if built == "second":
        for i in BS.harbor_channel(land, pinned, HARBOR_WATER, rng=rng):
            pinned[i] = 1
    kept = tidy(land, pinned, rng)
    return Skeleton(land, pinned, shape, off, kept, basin, built, open_edges)
