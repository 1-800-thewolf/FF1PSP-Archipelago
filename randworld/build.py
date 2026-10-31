"""Generate one random overworld: stages A-E end to end (tiles: tiles.py).

  wm = build.generate(seed, opts, weights, base_shares)

opts: {"shape": "vanilla"|"archipelago"|"pangaea", "open_progression": 0-2,
       "northern_docks": bool, "trails": bool, "gulg": bool, "size": sizing.TINY..HUGE}
       (trails = unlockable_northern_foot_trails, gulg = mount_gulg_key: gates.py;
       size = map_size: land, spacing and start scale, sizing.py)
weights / base_shares: targets.site_weights() / targets.vanilla_shares().
The sites placed are weights' keys: a tiny / small world passes fewer.
"""
import math
import random

from . import areas as AR
from . import basin as BS
from . import decorate as DC
from . import gates as GT
from . import grid as G
from . import paint as PT
from . import progression as PR
from . import shortcuts as SC
from . import sizing as SZ
from . import skeleton as SK
from . import tables as T
from . import verify as VF
from .worldmap import WorldMap, WALK_CLS

N, NN, NB4 = G.N, G.NN, G.NB4
TITAN_GAP = (8, 28)                  # tunnel length between the two mouths (v0)
DOCK_COUNT = (6, 10)                 # vanilla has 8 (v0)
NORTHERN_DOCKS_FACTOR = 1.75
DOCK_SPACING = 35
CLEARING_PICKS = 40                  # start clearings: best of this many random spots
SOFT_TRIES = 3                       # attempts before a soft-miss world is taken (tiny / small)
LAND_GROW_AFTER, LAND_GROW = 5, 1.15  # skeleton misses in a row before a scaled-down world gets more land


def generate_tiles(seed, opts, weights, base_shares, max_tries=6):
    """generate() + stage F (tiles) + stage G on the REAL grid: the rungs
    must equal the class map's (a mismatch is a generator bug)."""
    from . import tiles as TL
    wm = generate(seed, opts, weights, base_shares, max_tries)
    if wm is None:
        return None, None
    from . import export as EX
    rng = random.Random(f"ff1psp-randworld-v2-tiles:{seed}")
    before = VF.access_sig(wm)

    def same_graph(g):
        VF.verify(wm, weights, grid=g)
        return VF.access_sig(wm) == before
    grid = TL.solve(wm, rng, disc=lambda g: EX.disc_grid(wm, g), same_graph=same_graph)
    VF.verify(wm, weights, grid=grid)                 # resets report["problems"]
    if VF.access_sig(wm) != before:
        wm.report["problems"].append("tiles changed the access graph")
    if wm.report["tiles"]["packed"] > TL.GRID_BUDGET:
        wm.report["problems"].append("grid too big for the MAP_00 slot")
    wm.report["seams"] = TL.seam_count(grid)
    return wm, grid


SHORTCUT_RETILE_R = 4                # tiles re-solved around a shortcut's cells


def add_shortcuts(wm, weights, seed, grid, aims=None, water=None):
    """Backward shortcuts (shortcuts.py) on a FINISHED world (wm, grid from
    generate_tiles), then the tiles re-solved AROUND them only: every tile
    farther than SHORTCUT_RETILE_R from a changed cell stays as it was, so
    the rest of the world (and the Bestiary's terrain counts) cannot drift.
    aims: where this slot's vehicles landed (the apworld, after fill).
    water: the water regions the seed's logic reads (shortcuts.logic_view;
    None = all). Returns the new grid, or None when nothing was added or the
    retiled world failed a check (the caller keeps its world; wm is spent)."""
    from . import export as EX
    from . import tiles as TL
    before = SC.logic_export(wm, EX.disc_grid(wm, grid), water)
    cls0, fixed0 = list(wm.cls), dict(wm.fixed)
    if not SC.add(wm, weights, _sea_ids(wm.sk), random.Random(f"ff1psp-randworld-v2-shortcuts:{seed}"),
                  aims, water):
        return None
    changed = [i for i in range(NN) if wm.cls[i] != cls0[i] or wm.fixed.get(i) != fixed0.get(i)]
    win = G.dilate(changed, SHORTCUT_RETILE_R)
    sig = VF.access_sig(wm)

    def same_graph(g):
        VF.verify(wm, weights, grid=g)
        return VF.access_sig(wm) == sig
    own_fixed = wm.fixed
    pinned = dict(own_fixed)
    for i in range(NN):
        if i not in win and i not in pinned:
            pinned[i] = grid[i]
    wm.fixed = pinned
    try:
        rng = random.Random(f"ff1psp-randworld-v2-tiles-shortcuts:{seed}")
        new = TL.solve(wm, rng, disc=lambda g: EX.disc_grid(wm, g), same_graph=same_graph)
    finally:
        wm.fixed = own_fixed
    VF.verify(wm, weights, grid=new)
    if VF.access_sig(wm) != sig:
        wm.report["problems"].append("tiles changed the access graph")
    if wm.report["tiles"]["packed"] > TL.GRID_BUDGET:
        wm.report["problems"].append("grid too big for the MAP_00 slot")
    if wm.report["problems"] or SC.logic_export(wm, EX.disc_grid(wm, new), water) != before:
        return None
    wm.report["seams"] = TL.seam_count(new)
    wm.report["shortcut_window"] = len(win)
    return new


def generate(seed, opts, weights, base_shares, max_tries=6):
    rng = random.Random(f"ff1psp-randworld-v2:{seed}")
    # the seed's open edges are rolled once: open worlds fail a first attempt
    # more often (measured 2026-10-26: 6/16 vs 1/32), so a per-attempt roll
    # let the retries drift back to closed borders (25% open, not 50%). A
    # rolled style that never builds cleanly falls back to closed borders
    # (seed 18 + open progression: land wrapping north-south joined too many
    # foot sites, 6 attempts in a row) -- the generator as it was before.
    styles = [opts.get("open_edges")]
    if styles[0] is None:
        styles = [SK.roll_open_edges(random.Random(f"ff1psp-randworld-v2-edges:{seed}"))]
        if styles[0]:
            styles.append("")
    last = None
    soft = None                    # a clean world with a soft miss (sizing: tiny / small)
    attempt = 0
    for style in styles:
        o = dict(opts, open_edges=style)
        tried = 0
        misses = 0
        for _k in range(max_tries * 4):
            if tried >= max_tries:
                break
            wm = _attempt(rng, o, weights, base_shares)
            if wm is None:             # the skeleton found no anchor fit: cheap, roll again
                misses += 1
                # a tiny world's land can be too little for the anchors (a tiny
                # archipelago is 13% land): grow it back toward normal
                land = o.get("land_scale") or SZ.LAND[o.get("size", SZ.NORMAL)]
                if misses % LAND_GROW_AFTER == 0 and land < 1.0:
                    o["land_scale"] = min(1.0, land * LAND_GROW)
                continue
            tried += 1
            attempt += 1
            wm.report["attempt"] = attempt - 1
            if not wm.report["problems"]:
                if not wm.report.get("soft"):
                    return wm
                # best effort (user 2026-10-27): a few more tries for a world
                # where every vehicle opens a location, else the fewest misses
                if soft is None or len(wm.report["soft"]) < len(soft.report["soft"]):
                    soft = wm
                if tried >= SOFT_TRIES:
                    return soft
                continue
            if last is None or len(wm.report["problems"]) < len(last.report["problems"]):
                last = wm
    return soft or last


def _attempt(rng, opts, weights, base_shares):
    shape = opts.get("shape", "vanilla")
    size = opts.get("size", SZ.NORMAL)
    # vanilla's canal opens the outer ocean ('inner'); a 'second' inland sea
    # can only pay off a fraction of that, so it is the rarer roll
    canal_dir = "inner" if rng.random() < 0.65 else "second"
    land = opts.get("land_scale") or SZ.LAND[size]
    sk = SK.build(rng, shape, canal_dir, opts.get("open_edges"), land_scale=land)
    if sk is None:
        return None
    ar = AR.build(sk, rng, spacing=round(AR.SPACING.get(shape, 27) * SZ.SPACING[size]))
    wm = WorldMap(sk, ar)
    wm.gate_plan = GT.Plan(opts)
    # tiny / small: "each vehicle opens a location" is best effort (verify)
    wm.relax_vehicles = size in SZ.CHECK_BAND
    ways = GT.harbor_ways(opts)
    if "titan" not in weights:                 # a world without Titan's Tunnel
        ways = tuple(w for w in ways if w[0] != "titan")
    dice = PR.roll(rng, opts, base_shares, ways=ways)
    dice["start_cap"] = int(dice["start_cap"] * SZ.START_CAP[size])
    if opts.get("trails"):
        dice["desert_gate"] = rng.random() < GT.DESERT_GATE_P
    if sk.canal_dir == "second":
        # an inland sea holds an island and a few shores, not vanilla's
        # outer ocean: cap the canal's share and give the rest back
        t = dict(dice["targets"])
        cap = rng.uniform(0.08, 0.18)
        if t["canal"] > cap:
            spill = t["canal"] - cap
            t["canal"] = cap
            rest = sum(v for k, v in t.items() if k != "canal")
            for k in t:
                if k != "canal":
                    t[k] += spill * t[k] / rest
        dice["targets"] = t
    cats = PR.assign_sites(rng, weights, dice["targets"], dice["foot_n"],
                           force=GT.forced_rungs(opts))
    g_sea = _sea_ids(sk)
    wm.sea_lab = g_sea["lab"]
    _pin_anchors(wm)
    try:
        D = PR.design(wm, dice, cats, rng, g_sea["ship"], g_sea["canal"])
        GT.plan(wm, D, cats, rng, g_sea, weights)
    except PR.Redo as e:
        GT.STATS[f"redo: {e}"] = GT.STATS.get(f"redo: {e}", 0) + 1
        return None
    wm.design, wm.dice, wm.cats = D, dice, cats
    wm.report.update(shape=shape, canal=sk.canal_dir, open_edges=sk.open_edges,
                     style=dice["style"], harbor=D.harbor_way, targets=dice["targets"],
                     notes=D.notes)
    _place_rung_docks(wm, D, rng, g_sea)       # before ridges: kept off every border
    _paint_borders(wm, D, rng)
    corn = T.SITES["cornelia"]
    wm.put("cornelia", corn["kind"], corn["parts"][0], tuple(corn["parts"][0]["src"][:2]))
    bad = GT.carve_doors(wm, D, rng)
    if bad:
        k = "door did not fit: " + ", ".join(bad)
        GT.STATS[k] = GT.STATS.get(k, 0) + 1
        return None
    pins = wm.gate_plan.pins
    PR.rebalance(D, cats, ar, rng, weights, fixed=pins)
    D.site_area = PR.site_areas(D, cats, ar, rng, pins=pins)
    early_unplaced = _place_river_sites(wm, D, cats, rng)
    clearings = _start_style(wm, D, dice, rng)
    _place_sites(wm, D, cats, rng, clearings, g_sea, early_unplaced)
    GT.add_seal(wm)
    GT.name_items(wm)
    _harbor_fallback(wm, D, rng)
    _repair_links(wm, D)
    _place_docks(wm, D, opts, rng, g_sea)
    _place_bridge(wm, D, rng, g_sea)
    for a in {min(wm.region(a)) for a in range(len(ar.areas))}:
        PT.fill_pockets(wm, a)
    GT.unlandable(wm)
    VF.verify(wm, weights)
    before = VF.access_sig(wm)
    DC.decorate(wm, rng)
    for a in {min(wm.region(a)) for a in range(len(ar.areas))}:
        PT.fill_pockets(wm, a)
    DC.fill_dead_pockets(wm)
    GT.unlandable(wm)
    VF.verify(wm, weights)
    after = VF.access_sig(wm)
    if after != before:                        # decoration must never change access
        diff = sorted(k for k in before[0] if before[0][k] != after[0].get(k))
        wm.report["problems"].append("decoration changed the access graph: " + ", ".join(
            f"{k} {before[0][k]}->{after[0].get(k)}" for k in diff) + (
            "" if before[1:] == after[1:] else " (gated rungs / airship landings)"))
    return wm


def _sea_ids(sk):
    """Sea labels of the ship's water and of the water beyond the canal.
    The canal window's outer columns are open sea: the basin's side and the
    far side (canal_side +1: basin west, far sea east)."""
    sea = bytearray(0 if sk.land[i] else 1 for i in range(NN))
    lab, _sz = G.label(sea)
    ship = lab[G.idx(*SK.HARBOR_WATER[0])]
    b = sk.basin
    ox, oy = b.canal_origin
    west = lab[G.idx(ox, oy + 2)]
    east = lab[G.idx(ox + BS.CW - 1, oy + 2)]
    basin_side, far_side = (west, east) if b.canal_side > 0 else (east, west)
    canal = far_side if sk.canal_dir == "inner" else basin_side
    return {"ship": ship, "canal": canal, "lab": lab}


# ------------------------------------------------------------- borders ---
def _paint_borders(wm, D, rng):
    g = D.graph
    # a door border is a ridge first; gates.carve_doors cuts the door into it
    ridges = [k for k, v in D.border.items() if v in (PT.RIDGE, GT.DOOR)]
    PT.paint_ridges(wm, ridges, g.bcells, rng)
    wm.rivers = {}
    for k, v in D.border.items():
        if v == PT.RIVER_B:
            wm.rivers[k] = PT.paint_river(wm, k, g.bcells)
    for k, cells in wm.rivers.items():
        PT.river_banks(wm, cells, set(k))
    for a in {min(wm.region(a)) for a in range(len(wm.ar.areas))}:
        PT.reconnect_area(wm, a)


def _pin_anchors(wm):
    """The fixed things: airship desert, the ship's pier, the canal window."""
    A = T.ANCHORS
    xs = [x for x, _y, _t in A["airship_desert"]]
    ys = [y for _x, y, _t in A["airship_desert"]]
    x0, y0 = min(xs), min(ys)
    for x, y, t in A["airship_desert"]:
        wm.fix(G.idx(x, y), t)
    for x, y, _t in A["airship_desert"]:
        for i in G.disk(x, y, 2.5):              # a land halo: desert art never meets water
            if i not in wm.fixed and wm.cls[i] in ("o", "r", "c"):
                wm.set(i, "g", force=True)
            wm.no_water[i] = 1
    wm.placed.append(dict(key="airship", kind="airship", src=(x0, y0, max(xs) - x0 + 1, max(ys) - y0 + 1),
                          dst=(x0, y0), ents=[tuple(A["airship_seed"])],
                          cells=[(x - x0, y - y0) for x, y, _t in A["airship_desert"]]))
    for x, y, t in A["harbor_dock"]:
        wm.fix(G.idx(x, y), t)
    for x, y in A["harbor_water"]:
        wm.set(G.idx(x, y), "o", force=True)
        wm.protect[G.idx(x, y)] = 1
    hx, hy = A["harbor_land"]
    for x in range(hx - 2, hx + 4):
        i = G.idx(x, hy)
        if i not in wm.fixed:
            wm.set(i, "g", force=True)
            wm.protect[i] = 1
    wm.must_reach.append((G.idx(hx, hy),))
    wm.placed.append(dict(key="harbor", kind="harbor", src=(200, 145, 4, 2), dst=(200, 145),
                          ents=[tuple(A["harbor_land"])],
                          cells=[(x - 200, y - 145) for x, y, _t in A["harbor_dock"]]))
    b = wm.sk.basin
    ox, oy = b.canal_origin
    gate = dict(name="canal", item="Nitro Powder", kind="canal", origin=[ox, oy], side=b.canal_side,
                cells=[])
    for i, o, c in BS.window_cells(b.canal_origin):
        if o != c:
            x, y = G.xy(i)
            gate["cells"].append([x, y, o, c])
            wm.fix(i, c)                 # the CLOSED tile; the gate cave swaps it
        else:
            wm.fix(i, o)
    wm.gates.append(gate)
    wm.placed.append(dict(key="canal", kind="canal", src=tuple(T.CANAL["src"]), dst=(ox, oy), ents=[],
                          cells=[(i, j) for j in range(BS.CH) for i in range(BS.CW)]))
    # walkers carry on north and south of the window
    for j in (-3, -2, -1, BS.CH, BS.CH + 1, BS.CH + 2):
        for i in range(-1, BS.CW + 1):
            q = G.idx(ox + i, oy + j)
            if q not in wm.fixed and wm.cls[q] not in WALK_CLS:
                wm.set(q, "g", force=True)
            wm.protect[q] = 1


# --------------------------------------------------------- start style ---
def _outside_gate(wm):
    """The first walkable cell outside Cornelia's fixed tiles, walking out
    from its entrance's approach cells (the castle's gate)."""
    p = next(p for p in wm.placed if p["key"] == "cornelia")
    starts = [G.idx(ax, ay) for ax, ay, *_r in p["appr"] if wm.walk(G.idx(ax, ay))]
    seen = set(starts)
    q = list(starts)
    k = 0
    while k < len(q):
        i = q[k]
        k += 1
        if i not in wm.fixed:
            return G.xy(i)
        for j in NB4[i]:
            if j not in seen and wm.walk(j):
                seen.add(j)
                q.append(j)
    e = p["ents"][0]
    return (e[0], e[1] + 5)


def _wobbly_walk(rng, a, b, steps=160):
    """Cells along a meandering line from a toward b."""
    x, y = float(a[0]), float(a[1])
    dx = (b[0] - a[0] + N // 2) % N - N // 2
    dy = (b[1] - a[1] + N // 2) % N - N // 2
    tx, ty = a[0] + dx, a[1] + dy
    head = math.atan2(ty - y, tx - x)
    wob = rng.uniform(0, 6.28)
    out = []
    for step in range(steps):
        want = math.atan2(ty - y, tx - x)
        d = (want - head + math.pi) % (2 * math.pi) - math.pi
        head += 0.35 * d + 0.22 * math.sin(wob + step * 0.35)
        x += math.cos(head)
        y += math.sin(head)
        out.append((int(round(x)) % N, int(round(y)) % N))
        if (x - tx) ** 2 + (y - ty) ** 2 < 2.5:
            break
    return out


def _start_style(wm, D, dice, rng):
    """Returns {site index: cells} clearings for foot sites (None = anywhere
    in the start region)."""
    style = dice["style"]
    own = wm.ar.owner
    S = D.start
    cells = [i for a in S for i in wm.ar.areas[a].cells]
    walk_now = sum(1 for i in cells if wm.walk(i))
    if style != "paths" and walk_now * 0.85 > dice["start_cap"]:
        style = dice["style"] = "paths"          # an open start this big would break the cap
        D.notes.append("start too big for an open style -> paths")
    if style == "two_basins" and len(S) >= 2:
        _two_basins(wm, D, rng)
        PT.reconnect_area(wm, min(S))
        return None
    if style != "paths":
        return None
    walk = [i for i in cells if wm.walk(i)]
    if len(walk) < 400:
        return None
    sset = set(walk)
    # distance from the start region's edge: clearings sit inside it
    edge = [i for i in walk if any(j not in sset for j in NB4[i])]
    dist = G.bfs_dist(bytearray(1 if i in sset else 0 for i in range(NN)), edge, cap=12)
    hub = _outside_gate(wm)
    n = dice["foot_n"] - 1
    inner = [G.xy(i) for i in walk if dist[i] >= 6 and G.tdist(G.xy(i), hub) >= 14]
    rng.shuffle(inner)
    # clearings 15+ apart, each the roomiest of CLEARING_PICKS random spots
    # (user 2026-10-25: spread the start's sites inside the same start)
    centres = []
    others = [hub] + PT.location_points(wm)
    for _k in range(n):
        best = None
        seen = 0
        for p in inner:
            if any(G.tdist(p, q) < 15 for q in centres):
                continue
            s = min(min(G.tdist(p, q) for q in centres + others), PT.SPREAD_R)
            if best is None or s > best[0]:
                best = (s, p)
            seen += 1
            if seen >= CLEARING_PICKS:
                break
        if best is None:
            break
        centres.append(best[1])
        inner.remove(best[1])
    targets = list(centres)
    # every way out of the start must stay on the path network
    for k, kind in D.border.items():
        a, b = k
        if kind == PT.RIVER_B and (a in S) != (b in S):
            rc = wm.rivers.get(k, ())
            if rc:
                mid = rc[len(rc) // 2]
                targets.append(G.xy(mid))
    # gate mouths on the start's side (a door's trail end, the bridge's landing)
    targets += [xy for xy, a in wm.gate_targets if a in S]
    if D.harbor_way == "foot":
        targets.append(SK.HARBOR_LAND)
    elif D.titan and D.titan[0] in S:
        s, h = D.titan
        bc = sorted(D.graph.bcells.get((min(s, h), max(s, h)), ()))
        if bc:
            targets.append(G.xy(bc[len(bc) // 2]))
    keep = set()
    for i in G.disk(hub[0], hub[1], 6):
        keep.add(i)
    net = [hub]
    path_r = rng.choice((3, 3, 4))
    for t in targets:
        start = min(net, key=lambda q: G.tdist2(q, t))
        for k, p in enumerate(_wobbly_walk(rng, start, t)):
            r = path_r + (1 if math.sin(k * 0.21 + len(net)) > -0.2 else 0)
            keep.update(G.disk(p[0], p[1], r))
            net.append(p)
    clear = {}
    for k, c in enumerate(centres):
        cl = G.disk(c[0], c[1], rng.randint(6, 8))
        keep.update(cl)
        clear[k] = [i for i in cl if i in sset]
    keep = {i for i in keep if own[i] in S}
    # every clearing and exit joined to the hub, whatever the wobble did
    goals = [G.idx(*t) for t in targets]
    for _round in range(len(goals) + 1):
        reach = G.flood(bytearray(1 if i in keep else 0 for i in range(NN)), [G.idx(*hub)])
        lost = [q for q in goals if not reach[q]]
        if not lost:
            break
        q = lost[0]
        path = _land_path(wm, [i for i in keep if reach[i]], q, S)
        if path is None:
            goals.remove(q)
            continue
        for c in path:
            for i in G.disk(G.xy(c)[0], G.xy(c)[1], 2.5):
                if own[i] in S:
                    keep.add(i)
    for i in cells:
        if i not in keep and wm.walk(i) and not wm.protect[i]:
            wm.set(i, "M")
    for i in keep:
        if wm.cls[i] == "M" and not wm.protect[i] and PT.can_open(wm, i):
            wm.set(i, "g")
    PT.reconnect_area(wm, min(S))
    wm.report["start_paths"] = len(targets)
    return clear


def _land_path(wm, sources, goal, areas):
    """Shortest 4-connected path over land cells of `areas` (any class but
    fixed) from any source to goal, or None."""
    own = wm.ar.owner
    prev = {i: None for i in sources}
    q = list(sources)
    k = 0
    while k < len(q):
        i = q[k]
        k += 1
        if i == goal:
            out = []
            while i is not None:
                out.append(i)
                i = prev[i]
            return out
        for j in NB4[i]:
            if j in prev:
                continue
            if j == goal or (own[j] in areas and j not in wm.fixed
                             and (wm.walk(j) or PT.can_open(wm, j))):
                prev[j] = i
                q.append(j)
    return None


def _two_basins(wm, D, rng):
    """One inner border of the start region (the longest) becomes a ridge
    with a single pass in its middle; the others stay open, so the start
    always stays one walkable piece."""
    g = D.graph
    inner = [k for k in g.bcells if k[0] in D.start and k[1] in D.start]
    if not inner:
        return
    k = max(inner, key=lambda q: len(g.bcells[q]))
    # painted as a ridge, but still one region (the pass keeps it whole and
    # reconnect_area may cut more passes through it)
    PT.paint_ridges(wm, [k], g.bcells, rng, wmax=2)
    bc = sorted(g.bcells[k], key=lambda i: G.xy(i))
    mid = G.xy(bc[len(bc) // 2])
    for i in G.disk(mid[0], mid[1], 4.5):
        if wm.ar.owner[i] in D.start and not wm.protect[i] and wm.cls[i] == "M"                 and PT.can_open(wm, i):
            wm.set(i, "g")


# --------------------------------------------------------------- sites ---
def _place_river_sites(wm, D, cats, rng):
    """Waterfall and Whisperwind first, while the rivers' ends are free."""
    unplaced = []
    for s in PR.RIVER_SITES:
        if s in cats and not _place_river_site(wm, D, s, T.SITES[s], cats[s], rng):
            unplaced.append(s)
    return unplaced


def _place_sites(wm, D, cats, rng, clearings, g_sea, early_unplaced=()):
    ar = wm.ar
    site_area = D.site_area
    unplaced = list(early_unplaced)
    foot_clear = list((clearings or {}).values())
    order = sorted((s for s in site_area if s != "cornelia" and s not in PR.RIVER_SITES),
                   key=lambda s: (s != "titan", rng.random()))
    titan_done = False
    for s in order:
        a = site_area[s]
        if s == PR.SEA_SITE:
            sea_id = g_sea["ship"] if cats[s] == "ship" else g_sea["canal"]
            if not _place_sea_site(wm, s, sea_id, g_sea["lab"], rng):
                unplaced.append(s)
            continue
        site = T.SITES[s]
        if s == "titan":
            # harbor way 'titan': the tunnel IS the way to the pier
            hb = D.harbor_way == "titan"
            done = _place_titan(wm, D, a, rng, harbor=hb)
            if not done and not hb:
                for b in sorted(D.rung, key=lambda k: rng.random())[:25]:
                    if b != a and D.rung[b] in PR.RUNG and b not in GT.gated_areas(D) \
                            and _place_titan(wm, D, b, rng):
                        done = True
                        break
            if not done:
                unplaced.append(s)
            titan_done = True
            continue
        part = GT.site_part(wm, s)
        cells = None
        if a in D.start and foot_clear:
            cells = foot_clear.pop(0)
        rec = PT.place_site(wm, s, site["kind"], part, a, rng, cells=cells)
        if rec is None and cells is not None:
            rec = PT.place_site(wm, s, site["kind"], part, a, rng)
        if rec is None and s in wm.gate_plan.pins:
            rec = PT.place_site(wm, s, site["kind"], part, a, rng, tries=1200)
            if rec is None:
                unplaced.append(s)             # a walled-in site stays behind its gate
                continue
        if rec is None:
            # anywhere in an area of the same rung, then the nearest rungs
            # the site allows (earlier first: the shares drift a little)
            k0 = PR.RUNG.get(D.rung.get(a), 0)
            alts = sorted((b for b in D.rung if b != a and D.rung[b] in PR.RUNG
                           and D.rung[b] in PR.allowed(s)),
                          key=lambda b: (abs(PR.RUNG[D.rung[b]] - k0)
                                         + (0.5 if PR.RUNG[D.rung[b]] > k0 else 0), rng.random()))
            for b in alts[:25]:
                if b in GT.gated_areas(D):
                    continue
                rec = PT.place_site(wm, s, site["kind"], part, b, rng, tries=200)
                if rec:
                    site_area[s] = b
                    cats[s] = D.rung[b] if b not in D.start else "foot"
                    break
        if rec is None:
            unplaced.append(s)
    if D.titan and not titan_done:
        _place_titan(wm, D, D.titan[0], rng, harbor=True)
    wm.report["unplaced"] = unplaced


def _place_titan(wm, D, a, rng, harbor=False):
    """Two mouths 8-28 tiles apart, one in area a and one in a neighbour
    (the harbor way 'titan' fixes the pair)."""
    parts = T.SITES["titan"]["parts"]
    g = D.graph
    sited = set()
    for s2, b2 in D.site_area.items():
        if s2 != "titan":
            sited |= wm.region(b2)
    gated = GT.gated_areas(D)
    if D.titan and (harbor or a == D.titan[0]):
        pairs = [D.titan]
    elif a in gated:
        pairs = []                         # a tunnel out of a gated area would bypass its gate
    else:
        nb = [b for b in g.nbr[a] if D.rung.get(b) not in (None,) and b != a and b in sited
              and b not in gated]
        rng.shuffle(nb)
        pairs = [(a, b) for b in nb] if a in sited else []
    for x, y in pairs:
        key = (min(x, y), max(x, y))
        bc = g.bcells.get(key)
        if not bc:
            continue
        near = set(G.dilate(bc, 9))
        cx = [i for i in wm.ar.areas[x].cells if i in near and wm.walk(i)]
        cy = [i for i in wm.ar.areas[y].cells if i in near and wm.walk(i)]
        if not cx or not cy:
            continue
        snap = PT.snapshot(wm)
        ra = PT.place_site(wm, "titan", "cave", parts[0], x, rng, cells=cx, tries=300)
        if ra is None:
            continue
        ea = ra["ents"][0]
        cy2 = [i for i in cy if TITAN_GAP[0] <= G.tdist(G.xy(i), ea) <= TITAN_GAP[1]]
        rb = PT.place_site(wm, "titan", "cave", parts[1], y, rng, cells=cy2 or None, tries=300) if cy2 else None
        if rb is None:
            PT.restore(wm, snap)
            continue
        D.titan = (x, y)
        return True
    if harbor or a not in sited:
        return False
    # user 2026-10-13: the mouths need not sit on separate areas -- both in
    # area a, 8-28 tiles apart
    ca = [i for i in wm.ar.areas[a].cells if wm.walk(i)]
    for _k in range(4):
        snap = PT.snapshot(wm)
        ra = PT.place_site(wm, "titan", "cave", parts[0], a, rng, cells=ca, tries=300)
        if ra is None:
            return False
        ea = ra["ents"][0]
        cb = [i for i in ca if TITAN_GAP[0] <= G.tdist(G.xy(i), ea) <= TITAN_GAP[1]]
        rb = PT.place_site(wm, "titan", "cave", parts[1], a, rng, cells=cb, tries=300) if cb else None
        if rb:
            D.titan = (a, a)
            return True
        PT.restore(wm, snap)
    return False


def _place_sea_site(wm, key, sea_id, lab, rng, clear=2, near=(3, 10)):
    spec = T.SEA_SITES[key]
    cand = [i for i in range(NN) if lab[i] == sea_id and wm.cls[i] == "o" and not wm.protect[i]]
    rng.shuffle(cand)
    cand = cand[:4000]
    pts = PT.location_points(wm)
    cand.sort(key=lambda i: PT.crowding(G.xy(i), pts) + rng.random())
    for i in cand:
        x, y = G.xy(i)
        box = [G.idx(x + dx, y + dy) for dy in range(-clear, clear + 2) for dx in range(-clear, clear + 1)]
        if any(wm.cls[q] != "o" or wm.protect[q] for q in box) or not PT.off_edge(box):
            continue
        land = min((max(abs(dx), abs(dy)) for dy in range(-near[1], near[1] + 1)
                    for dx in range(-near[1], near[1] + 1)
                    if wm.cls[G.idx(x + dx, y + dy)] != "o"), default=99)
        if not near[0] <= land <= near[1]:
            continue
        tx, ty = spec["trig"]
        dst = ((x - tx) % N, (y - ty) % N)
        for ii, jj, t in spec["fixed"]:
            wm.fix(G.idx(dst[0] + ii, dst[1] + jj), t)
        wm.protect[G.idx(x, y + 1)] = 1
        wm.placed.append(dict(key=key, kind=spec["kind"], src=tuple(spec["src"]), dst=dst,
                              ents=[(x, y)], cells=[(ii, jj) for ii, jj, _t in spec["fixed"]], appr=[]))
        return True
    return False


def _place_river_site(wm, D, key, site, rung, rng):
    """Waterfall / Whisperwind: the stamp's water cells join a river of the
    right rung; the rock it needs may cover that river's own banks as long
    as both areas stay whole."""
    part = site["parts"][0]
    mine = [(k, cells) for k, cells in wm.rivers.items()
            if D.rung.get(k[0]) == rung or D.rung.get(k[1]) == rung]
    rng.shuffle(mine)
    rest = [kc for kc in wm.rivers.items() if kc not in mine]
    rng.shuffle(rest)
    rivers = mine + rest                     # its rung's rivers first, then any
    water_off = [(i, j) for i, j, c in part["hints"] if c == "r"]
    water_off += [(i, j) for i, j, t in part["fixed"] if T.ATT[t] == 0xF009]
    water_off += [(ax, ay) for ax, ay, _dx, _dy, k in part["appr"] if k == "r"]
    # the cell vanilla puts above the falls (a sealed pool), never joined
    pool = [(ax, ay) for ax, ay, _dx, dy, k in part["appr"] if k == "r" and dy < 0]
    before = {kk: PT.river_links_ok(wm, cells, kk) for kk, cells in wm.rivers.items()}
    pts = PT.location_points(wm)
    for k, rc in rivers:
        rset = set(rc)
        allowed = set(k)
        cand = list(rc)
        rng.shuffle(cand)
        cand.sort(key=lambda r: PT.crowding(G.xy(r), pts) + rng.random())
        for r in cand[:250]:
            rx, ry = G.xy(r)
            for (wi, wj) in water_off:
                if (wi, wj) in pool:
                    continue
                for ddx, ddy in G.DIRS4:
                    dst = ((rx + ddx - wi) % N, (ry + ddy - wj) % N)
                    if not _river_site_ok(wm, part, dst, rset, pool, allowed):
                        continue
                    snap = PT.snapshot(wm)
                    rec = wm.put(key, site["kind"], part, dst, river_site=True)
                    for (pi, pj) in pool:          # seal the pool above the falls
                        pc = G.idx(dst[0] + pi, dst[1] + pj)
                        for q in NB4[pc]:
                            if q not in wm.fixed and wm.cls[q] != "r":
                                wm.set(q, "M", force=True)
                    if all(PT.area_whole(wm, a) for a in allowed) and                             all(PT.river_links_ok(wm, cells, kk) for kk, cells in wm.rivers.items()
                                if before[kk]):
                        for a in allowed:
                            PT.fill_pockets(wm, a)
                        return True
                    PT.restore(wm, snap)
    return False


def _river_site_ok(wm, part, dst, rset, pool, allowed):
    own = wm.ar.owner
    fixed, hints, ents, appr = wm.stamp_cells(part, dst)
    water = {i for i, c in hints if c == "r"}
    water |= {i for i, t in fixed if T.ATT[t] == 0xF009}
    water |= {G.idx(ax, ay) for ax, ay, _dx, _dy, k in appr if k == "r"}
    rock = {i for i, t in fixed if T.ATT[t] >= 0xF000 and T.ATT[t] != 0xF009}
    rock |= {i for i, c in hints if c == "M"}
    stamp = water | rock | {i for i, _t in fixed}
    if not PT.off_edge(stamp | {i for i, _c in hints}):
        return False
    pool_cells = {G.idx(dst[0] + pi, dst[1] + pj) for pi, pj in pool}
    for i in stamp:
        if wm.protect[i] and not (i in water and wm.cls[i] == "r"):
            return False
        if wm.walk(i) and own[i] not in allowed:
            return False
        if wm.cls[i] == "o":
            return False
        if wm.cls[i] == "r" and i not in water:
            return False                       # never cut a river with rock
    joins = False
    for i in water:
        for j in NB4[i]:
            if j in stamp:
                continue
            if j in rset:
                if i in pool_cells:
                    return False
                joins = True
            elif wm.cls[j] == "r":
                return False                   # never another river
            elif wm.walk(j) and own[j] not in allowed:
                return False                   # (the pool's own banks get sealed in rock)
    return joins


# -------------------------------------------------------------- bridge ---
BRIDGE_SRC = (143, 144, 3, 3)      # vanilla span window: the 0xfa6 first-crossing
BRIDGE_DECK0 = (144, 145)          # trigger and 90 exit returns ride along (bake.py)
BRIDGE_SPAN = (1, 8)


def bridge_tiles(x, yn, span):
    """{cell: tile} for a King's bridge whose north landing is (x, yn).
    span 1: Cornelia's 3x3 block; 2+: the user's lean Lufenian layout (head
    with its exact ring, repeated deck, posts on the last row, then bank)."""
    t = {}
    if span == 1:
        for k, d in enumerate((-1, 0, 1)):
            t[G.idx(x + d, yn)] = 0x160 + k
            t[G.idx(x + d, yn + 1)] = 0x170 + k
            t[G.idx(x + d, yn + 2)] = 0x180 + k
        return t
    for (dx, dy), v in {(-1, -1): 0x3F, (0, -1): 0x4F, (1, -1): 0x3B,
                        (-1, 0): 0x3E, (1, 0): 0x4D, (-1, 1): 0x4E}.items():
        t[G.idx(x + dx, yn + dy)] = v
    t[G.idx(x, yn)] = 0x161
    for k in range(1, span + 1):
        t[G.idx(x, yn + k)] = 0x171
    t[G.idx(x - 1, yn + span)] = 0x183
    t[G.idx(x + 1, yn + span)] = 0x185
    for k, d in enumerate((-1, 0, 1)):
        t[G.idx(x + d, yn + span + 1)] = 0x180 + k
    return t


def _place_bridge(wm, D, rng, g_sea):
    """ONE always-built King's bridge over a main-ocean strait, N-S, between
    two regions of the same rung (a shortcut that moves no site's rung),
    other landmasses preferred, never cutting the ship's sea in two, never
    bypassing Titan's tunnel (user 2026-10-13). No strait, no bridge."""
    own = wm.ar.owner
    lab = g_sea["lab"]
    ocean = g_sea["ship"]
    titan_pair = set(D.titan) if D.titan and D.titan[0] != D.titan[1] else None
    cands = []
    for i in range(NN):
        if not wm.walk(i) or wm.protect[i] or i in wm.fixed:
            continue
        x, yn = G.xy(i)
        a = own[i]
        if a < 0:
            continue
        for span in range(BRIDGE_SPAN[0], BRIDGE_SPAN[1] + 1):
            deck = [G.idx(x, yn + k) for k in range(1, span + 1)]
            if not all(lab[c] == ocean and wm.cls[c] == "o" and not wm.protect[c] for c in deck):
                if wm.cls[deck[-1]] != "o":
                    break
                continue
            j = G.idx(x, yn + span + 1)
            b = own[j]
            if b < 0 or not wm.walk(j) or wm.protect[j] or j in wm.fixed:
                break
            if D.rung.get(a) != D.rung.get(b):
                break                      # would pull one side to another rung
            if a in GT.gated_areas(D) or b in GT.gated_areas(D):
                break                      # would bypass a world-changing gate
            if titan_pair and {a, b} == titan_pair:
                break
            side = all(lab[G.idx(x + dx, yn + k)] == ocean
                       for k in range(1, span + 1) for dx in (-2, -1, 1, 2))
            if side:
                cells = bridge_tiles(x, yn, span)
                if PT.off_edge(cells) and \
                        not any(c in wm.fixed or wm.protect[c] for c in cells if c not in deck):
                    # joining other landmasses first, then other regions,
                    # then a shortcut inside one region (user: fine)
                    pref = 0 if wm.ar.areas[a].mass != wm.ar.areas[b].mass else                         (1 if wm.region(a) != wm.region(b) else 2)
                    cands.append((pref, span + rng.random() * 3, x, yn, span))
            break
    cands.sort()
    for _pref, _s, x, yn, span in cands[:40]:
        cells = bridge_tiles(x, yn, span)
        deck = {G.idx(x, yn + k) for k in range(1, span + 1)}
        # the ship's sea must stay one body with the deck in place
        sea = bytearray(1 if (lab[c] == ocean and c not in deck and wm.cls[c] == "o") else 0
                        for c in range(NN))
        _l, sz = G.label(sea)
        if sum(1 for n in sz if n >= 20) > 1:
            continue
        snap = PT.snapshot(wm)
        for c, t in cells.items():
            wm.fix(c, t)
        dx0 = (x - (BRIDGE_DECK0[0] - BRIDGE_SRC[0])) % N
        dy0 = (yn + 1 - (BRIDGE_DECK0[1] - BRIDGE_SRC[1])) % N
        wm.placed.append(dict(key="bridge", kind="bridge", src=BRIDGE_SRC, dst=(dx0, dy0),
                              ents=[], cells=[], span=span, head=(x, yn),
                              split=_pref < 2))
        wm.report["bridge"] = dict(head=(x, yn), span=span, split=_pref < 2,
                                   kind=("landmasses", "regions", "shortcut")[_pref])
        return True
    wm.report["bridge"] = None
    return False


# --------------------------------------------------------------- docks ---
def _repair_links(wm, D):
    """Every designed river must still touch the main ground of both its
    areas; if a ridge, stamp or start carving pinched it, give it banks."""
    for k, cells in wm.rivers.items():
        if not PT.river_links_ok(wm, cells, k):
            PT.open_banks(wm, cells, set(k), depth=4)
            for a in k:
                PT.reconnect_area(wm, a)
            if not PT.river_links_ok(wm, cells, k):
                D.notes.append(f"river {k} lost a bank")


def _place_rung_docks(wm, D, rng, g_sea):
    """The docks the design depends on (ship / canal / desert areas)."""
    wm.rung_docks = []
    for a, sea_id in D.docks:
        rec = PT.place_dock(wm, a, g_sea["lab"], sea_id, rng, away=D.graph.near_border)
        if rec is None:
            rec = PT.place_dock(wm, a, g_sea["lab"], sea_id, rng)
        if rec:
            wm.rung_docks.append(rec)
        else:
            D.notes.append(f"no dock fits area {a}")


def _harbor_fallback(wm, D, rng):
    """The harbor way 'titan' needs both mouths. When they did not fit, the
    start reaches the harbor area by a river (a long enough border), else by
    a pass carved through the ridge (foot)."""
    if D.harbor_way != "titan" or any(p["key"] == "titan" for p in wm.placed):
        return
    s, h = D.titan
    key = (min(s, h), max(s, h))
    bc = D.graph.bcells[key]
    if "titan" in wm.report.get("unplaced", []):
        wm.report["unplaced"].remove("titan")
        D.notes.append("Titan's Tunnel left out (its harbor tunnel did not fit)")
    D.titan = None
    if len(bc) >= 10:
        cells = PT.paint_river(wm, key, D.graph.bcells)
        PT.open_banks(wm, cells, set(key))
        PT.river_banks(wm, cells, set(key))
        wm.rivers[key] = cells
        if PT.river_links_ok(wm, cells, key):
            D.border[key] = PT.RIVER_B
            D.harbor_way = "canoe"
            D.rung[h] = "canoe"
            D.notes.append("titan harbor did not fit -> canoe river")
            return
        for i in cells:
            wm.set(i, "M")
        del wm.rivers[key]
    _open_pass(wm, D, key)
    D.harbor_way = "foot"
    D.rung[h] = "foot"
    D.start.add(h)
    D.notes.append("titan harbor did not fit -> pass through the ridge")


def _open_pass(wm, D, key, r=3.5):
    """Carve a walkable pass through the ridge on border `key`; the two
    areas become one region."""
    D.border[key] = PT.OPEN
    wm.regions = PR.regions_of(D, D.graph.n)
    bc = sorted(D.graph.bcells[key], key=lambda i: G.xy(i))
    mid = G.xy(bc[len(bc) // 2])
    for i in G.disk(mid[0], mid[1], r):
        if wm.ar.owner[i] in key and not wm.protect[i] and wm.cls[i] in ("M", "c")                 and PT.can_open(wm, i):
            wm.set(i, "g")
    PT.reconnect_area(wm, key[0])


def _place_docks(wm, D, opts, rng, g_sea):
    lab = g_sea["lab"]
    placed = list(wm.rung_docks)
    # top up toward the dock count on areas the ship may visit anyway
    lo, hi = DOCK_COUNT
    spacing = DOCK_SPACING
    if opts.get("northern_docks"):
        lo, hi = round(lo * NORTHERN_DOCKS_FACTOR), round(hi * NORTHERN_DOCKS_FACTOR)
        spacing = DOCK_SPACING / NORTHERN_DOCKS_FACTOR ** 0.5
    want = rng.randint(lo, hi)
    early = ("foot", "canoe", "ship")
    has_loc = {wm.design.site_area.get(s) for s in wm.cats}
    cand = [a for a, r in D.rung.items() if r in early and a in has_loc
            and D.graph.coast[a].get(g_sea["ship"], 0) >= 4]
    rng.shuffle(cand)
    for a in cand:
        if len(placed) >= want:
            break
        snap = PT.snapshot(wm)
        rec = PT.place_dock(wm, a, lab, g_sea["ship"], rng)
        if rec is None:
            continue
        if all(G.tdist(rec["ents"][0], p["ents"][0]) >= spacing for p in placed):
            placed.append(rec)
        else:
            PT.restore(wm, snap)
    wm.report["docks"] = len(placed)
