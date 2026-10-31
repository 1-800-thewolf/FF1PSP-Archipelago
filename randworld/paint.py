"""Stage D primitives: turn area-graph decisions into cells.

Every function here edits a WorldMap's classes / fixed tiles. Borders come
from the stage-B area partition; what a border becomes was decided on the
graph (progression.py).
"""
import math

from . import grid as G
from . import noise
from . import tables as T
from .worldmap import WALK_CLS, SEA, RIVER, WALK, ROCK, move_of_tile

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8

RIDGE, RIVER_B, OPEN = "ridge", "river", "open"
MIN_GAP = 4               # tiles between two stamps' fixed tiles
# Soft spacing between locations (user 2026-10-25: "3+ locations within a
# single screen" was normal; aim ~2 per 15x9 screen). A spot pays SPREAD_W *
# (1 - d / SPREAD_R)^2 per location entrance d < SPREAD_R tiles away. Soft on
# purpose: a crowded area still takes its site rather than failing the world.
SPREAD_R = 26
SPREAD_W = 8.0
NOT_LOCATIONS = ("harbor", "airship", "canal")      # placed records that are no location
# A stamp never wraps a map edge, open or closed (skeleton.OPEN_EDGES): its
# entrance records, exit returns and menu-map points carry plain coordinates.
STAMP_EDGE = 3


def off_edge(cells):
    """Every cell (flat index) at least STAMP_EDGE from each map edge."""
    for i in cells:
        x, y = i % N, i // N
        if min(x, N - 1 - x, y, N - 1 - y) < STAMP_EDGE:
            return False
    return True


def location_points(wm, skip=None):
    """Entrance of every placed location (docks and the fixed anchors are
    not locations; Titan's two mouths are two). skip: a key left out (a
    tunnel's second mouth keeps its own 8-28 gap to the first)."""
    return [tuple(p["ents"][0]) for p in wm.placed
            if p.get("ents") and p["kind"] != "dock" and p["key"] not in NOT_LOCATIONS
            and p["key"] != skip]


def crowding(p, pts):
    """Spacing penalty of a location entrance at p (see SPREAD_R)."""
    s = 0.0
    for q in pts:
        d = G.tdist(p, q)
        if d < SPREAD_R:
            s += (1 - d / SPREAD_R) ** 2
    return SPREAD_W * s


# NB8 ring order around a cell, as (dx, dy), and 4-adjacency inside the ring
_RING = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))


def simple_to_block(wm, i):
    """Can walkable cell i become rock without splitting the walkable ground
    around it? True when its walkable 4-neighbours stay 4-connected through
    the other walkable cells of its 3x3 ring (a local, sufficient test)."""
    x, y = G.xy(i)
    ring = [G.idx(x + dx, y + dy) for dx, dy in _RING]
    ents = wm.ent_cells                 # logic blocks entrances: never a bypass
    w = [wm.walk(j) and j not in ents for j in ring]
    orth = [k for k in (0, 2, 4, 6) if w[k]]
    if len(orth) <= 1:
        return True
    # walk around the ring: orthogonal walkable cells joined through
    # walkable ring cells (a diagonal cell links its two orthogonal ones)
    seen = {orth[0]}
    stack = [orth[0]]
    while stack:
        k = stack.pop()
        for d in (-1, 1):
            n = (k + d) % 8
            if w[n] and n not in seen:
                # ring neighbours k and n are 4-adjacent unless both diagonal
                seen.add(n)
                stack.append(n)
    return all(k in seen for k in orth)


def can_open(wm, i):
    """May rock at cell i become walkable? Only when no 4-neighbour belongs to
    a DIFFERENT region (any class): the outermost cells of two regions then
    stay rock forever and no later stage can join them by accident."""
    own = wm.ar.owner
    a = own[i]
    if a < 0:
        return False
    reg = wm.region(a)
    return all(own[j] < 0 or own[j] in reg for j in NB4[i])


def borders(ar):
    """{(a, b) with a < b: [cells of a beside b] + [cells of b beside a]}."""
    own = ar.owner
    out = {}
    for i in range(NN):
        a = own[i]
        if a < 0:
            continue
        for j in NB4[i]:
            b = own[j]
            if b >= 0 and b != a:
                key = (a, b) if a < b else (b, a)
                out.setdefault(key, set()).add(i)
    return out


def paint_ridges(wm, ridge_pairs, bcells, rng, wmax=3):
    """Rock along every RIDGE border: border cells plus up to w more layers
    on each side, w (0..wmax) wandering with noise -> 2..8 tiles thick, so a
    border reads as a range that swells and narrows, not a uniform line."""
    own = wm.ar.owner
    wn = noise.fbm(rng, 16, 3)
    for key in ridge_pairs:
        a, b = key
        front = list(bcells[key])
        dist = {i: 0 for i in front}
        for d in range(1, wmax + 1):
            nxt = []
            for i in front:
                for j in NB4[i]:
                    if j not in dist and own[j] in (a, b):
                        dist[j] = d
                        nxt.append(j)
            front = nxt
        for d0 in range(wmax + 1):
            for i, d in dist.items():
                if d != d0:
                    continue
                # layer 1 always (where it is a simple point): two border
                # cells alone make a 2-wide ridge, and vanilla mountain art
                # needs ~3x3 masses (v0 thicken) -- 2-wide ridges were the
                # main source of softened, fragmented tiles
                w = max(1, int(wn[i] ** 1.3 * (wmax + 1.8)))
                if d <= w and (d == 0 or not wm.walk(i) or simple_to_block(wm, i)):
                    wm.set(i, "M")


def paint_river(wm, key, bcells):
    """A 2-wide river down a border (both sides' border cells). Returns its
    cells."""
    cells = [i for i in sorted(bcells[key]) if not wm.no_water[i]]
    for i in cells:
        wm.set(i, "r")
    return cells


def open_banks(wm, river_cells, allowed, depth=3):
    """Rock within `depth` of a river, inside its own two areas, turns to
    ground -- so a river painted over a ridge gets real banks. Never next to
    a third area's cells."""
    own = wm.ar.owner
    front = list(river_cells)
    seen = set(front)
    for _d in range(depth):
        nxt = []
        for i in front:
            for j in NB4[i]:
                if j in seen:
                    continue
                seen.add(j)
                if own[j] in allowed and wm.cls[j] == "M" and not wm.protect[j]                         and not any(own[r] >= 0 and own[r] not in allowed for r in NB4[j])                         and (can_open(wm, j) or any(wm.cls[r] == "r" for r in NB4[j])):
                    wm.set(j, "g")
                    nxt.append(j)
        front = nxt


def river_banks(wm, river_cells, allowed):
    """Cells beside a river that belong to an area NOT in `allowed` turn to
    rock, so a river only gives canoe access where it was planned."""
    own = wm.ar.owner
    for i in river_cells:
        if wm.cls[i] != "r":
            continue
        for j in NB4[i]:
            o = own[j]
            if o >= 0 and o not in allowed and wm.walk(j):
                wm.set(j, "M")


def area_walk_component(wm, a, start=None, extra_block=()):
    """Walk cells of area a's REGION (areas joined by open borders) reachable
    from `start` (default: the region's biggest walk component)."""
    block = set(extra_block)
    cells = [i for i in wm.region_cells(a) if wm.walk(i) and i not in block]
    cs = set(cells)
    if not cells:
        return set()
    seen = set()
    best = set()
    seeds = [start] if start is not None else cells
    for s in seeds:
        if s in seen or s not in cs:
            continue
        comp = {s}
        stack = [s]
        while stack:
            i = stack.pop()
            for j in NB4[i]:
                if j in cs and j not in comp:
                    comp.add(j)
                    stack.append(j)
        seen |= comp
        if len(comp) > len(best):
            best = comp
        if start is not None:
            break
    return best


def area_whole(wm, a, min_share=0.85):
    """Area a's region's walk cells mostly in one piece (a stamp did not cut it)."""
    total = sum(1 for i in wm.region_cells(a) if wm.walk(i))
    if total == 0:
        return True
    return len(area_walk_component(wm, a)) >= min_share * total


def river_links_ok(wm, river_cells, areas):
    """The river still touches the main walkable body of each area."""
    rc = set(river_cells)
    for a in areas:
        body = area_walk_component(wm, a)
        if not any(j in body for i in rc if wm.cls[i] == "r" for j in NB4[i]):
            return False
    return True


def reconnect_area(wm, a, min_piece=25):
    """Join every walkable piece of area a's REGION (>= min_piece cells) to
    its main body through the region's own rock, never next to another
    region's cells (so no ridge between regions is breached). Smaller
    unprotected pieces become rock; a piece that cannot be joined is left
    as it is. Returns True when the region ends in one piece."""
    own = wm.ar.owner
    reg = wm.region(a)

    def carvable(j):
        return own[j] in reg and not wm.protect[j] and wm.cls[j] in ("M", "c") and can_open(wm, j)

    failed = set()
    for _round in range(40):
        cells = [i for i in wm.region_cells(a) if wm.walk(i)]
        cs = set(cells)
        comps, seen = [], set()
        for s0 in cells:
            if s0 in seen:
                continue
            comp = {s0}
            stack = [s0]
            while stack:
                i = stack.pop()
                for j in NB4[i]:
                    if j in cs and j not in comp:
                        comp.add(j)
                        stack.append(j)
            seen |= comp
            comps.append(comp)
        if len(comps) <= 1:
            return True
        comps.sort(key=len, reverse=True)
        main = comps[0]
        todo = [c for c in comps[1:] if min(c) not in failed]
        if not todo:
            return False
        piece = todo[0]
        if len(piece) < min_piece and not any(wm.protect[i] for i in piece):
            for i in piece:
                wm.set(i, "M")
            continue
        prev = {i: None for i in piece}
        q = list(piece)
        k = 0
        end = None
        while k < len(q) and end is None:
            i = q[k]
            k += 1
            for j in NB4[i]:
                if j in prev:
                    continue
                if j in main:
                    prev[j] = i
                    end = j
                    break
                if carvable(j):
                    prev[j] = i
                    q.append(j)
        if end is None:
            failed.add(min(piece))
            continue
        i = prev[end]
        while i is not None and i not in piece:
            wm.set(i, "g")
            for j in NB4[i]:              # a 2-3 wide pass where it is safe
                if carvable(j):
                    wm.set(j, "g")
            i = prev[i]
    return False


def fill_pockets(wm, a, keep_min=30):
    """Walk pockets of area a cut off from its main body: tiny ones become
    rock (they would only be unreachable decoration)."""
    cells = [i for i in wm.region_cells(a) if wm.walk(i)]
    cs = set(cells)
    seen = set()
    comps = []
    for s in cells:
        if s in seen:
            continue
        comp = {s}
        stack = [s]
        while stack:
            i = stack.pop()
            for j in NB4[i]:
                if j in cs and j not in comp:
                    comp.add(j)
                    stack.append(j)
        seen |= comp
        comps.append(comp)
    if len(comps) <= 1:
        return 0
    comps.sort(key=len, reverse=True)
    n = 0
    for comp in comps[1:]:
        if len(comp) < keep_min and not any(wm.protect[i] for i in comp):
            for i in comp:
                wm.set(i, "M")
                n += 1
    return n


# ------------------------------------------------------------- stamps ---
def spot_score(wm, part, dst, area, busy):
    """None if the stamp cannot sit with its window at dst inside `area`,
    else a score (higher = better fit)."""
    own = wm.ar.owner
    fixed, hints, ents, appr = wm.stamp_cells(part, dst)
    if not off_edge([i for i, _t in fixed] + [i for i, _c in hints]
                    + [G.idx(a[0], a[1]) for a in appr]):
        return None
    for i, _t in fixed:
        if own[i] != area or wm.protect[i] or i in wm.fixed:
            return None
        if wm.cls[i] in ("o", "r"):
            return None
    for ax, ay, _dx, _dy, k in appr:
        i = G.idx(ax, ay)
        if k == "g" and (own[i] != area or wm.protect[i] and not wm.walk(i)):
            return None
        if k == "g" and not wm.walk(i) and not can_open(wm, i):
            return None
    s = 0.0
    hint_at = {i: c for i, c in hints}
    water_new = [i for i, c in hints if c in ("o", "r") and wm.cls[i] != c]
    seas = set()
    lab = getattr(wm, "sea_lab", None)
    for i in water_new:
        c = hint_at[i]
        for j in NB4[i]:
            if j in hint_at and hint_at[j] in ("o", "r"):
                continue
            if wm.cls[j] == "r" or (c == "r" and wm.cls[j] == "o"):
                return None          # new water never joins a river (or a river a sea)
            if wm.cls[j] == "o" and lab is not None:
                seas.add(lab[j])
    if len(seas) > 1:
        return None                  # would join two seas (the canal's job)
    for i, c in hints:
        if wm.cls[i] == "r" and c not in ("r", "o", "c"):
            return None              # a stamp never cuts a river
        o = own[i]
        if o >= 0 and o != area:
            if c != "M" or wm.walk(i):
                return None          # never paint into another area's ground
        if c in ("o", "r") and wm.cls[i] != c and (o != area or i in wm.fixed):
            return None
        if c in ("o", "r", "c") and wm.cls[i] not in (c, "o") and not can_open(wm, i) and wm.walk(i) is False                 and wm.cls[i] != "M":
            return None
        if wm.protect[i]:
            cur = wm.cls[i]
            if not (c == "w" and cur in WALK_CLS) and cur != c and not (c == "c" and cur == "M"):
                return None
        if c == "M":
            s += 1.0 if wm.cls[i] == "M" else 0.0
        elif c == "w":
            if wm.cls[i] not in WALK_CLS and not can_open(wm, i):
                return None
            s += 1.0 if wm.cls[i] in WALK_CLS else -0.5
        elif c == "o":
            s += 1.0 if wm.cls[i] == "o" else -0.3
    # keep clear of other stamps
    for i, _t in fixed:
        if i in busy:
            return None
    return s


def find_spot(wm, part, area, rng, tries=400, cells=None, key=None):
    """Best window origin for `part` in `area` (entrance on an area cell),
    kept apart from the other locations (crowding)."""
    pool = cells if cells is not None else [i for i in wm.ar.areas[area].cells if wm.walk(i)]
    if not pool:
        return None
    ex, ey = part["ents"][0]
    busy = G.dilate(wm.fixed.keys(), MIN_GAP)
    pts = location_points(wm, skip=key)
    best = None
    for _ in range(tries):
        cx, cy = G.xy(rng.choice(pool))
        dst = ((cx - ex) % N, (cy - ey) % N)
        s = spot_score(wm, part, dst, area, busy)
        if s is not None:
            s += rng.random() * 0.5 - crowding((cx, cy), pts)
            if best is None or s > best[0]:
                best = (s, dst)
    return None if best is None else best[1]


def anchors_joined(wm, area, extra_block=()):
    """One walkable piece of area's region holds at least one cell of every
    must-reach group there (site approaches, docks, the harbor landing)."""
    reg = wm.region(area)
    own = wm.ar.owner
    groups = [g for g in wm.must_reach if own[g[0]] in reg]
    if len(groups) <= 1:
        return True
    block = set(extra_block)
    allc = {i for i in wm.region_cells(area) if wm.walk(i) and i not in block}
    comp = {}
    cid = 0
    for g in groups:
        for s0 in g:
            if s0 in allc and s0 not in comp:
                comp[s0] = cid
                stack = [s0]
                while stack:
                    i = stack.pop()
                    for j in NB4[i]:
                        if j in allc and j not in comp:
                            comp[j] = cid
                            stack.append(j)
                cid += 1
    if cid == 0:
        return False
    sets = [{comp[c] for c in g if c in comp} for g in groups]
    if any(not s_ for s_ in sets):
        return False
    common = set.intersection(*sets)
    return bool(common)


def check_area_after(wm, area, rec, min_share=0.85):
    """After a stamp: its approach cells reach the area's main walk body,
    that body keeps most of the area, and nothing placed before in the
    region was cut off. Small cut-off pockets become rock."""
    blocks = [G.idx(*e) for p in wm.placed if p.get("appr") for e in p["ents"]]
    if not anchors_joined(wm, area, blocks):
        return False
    blocks = [G.idx(*e) for e in rec["ents"]]
    body = area_walk_component(wm, area, extra_block=blocks)
    total = sum(1 for i in wm.region_cells(area) if wm.walk(i)) - len(blocks)
    if total <= 0 or len(body) < min_share * total:
        return False
    ok = any(G.idx(ax, ay) in body for ax, ay, _dx, _dy, k in rec["appr"] if k == "g") \
        or not any(k == "g" for *_r, k in rec["appr"])
    return ok


def snapshot(wm):
    return (list(wm.cls), dict(wm.fixed), bytearray(wm.protect), len(wm.placed),
            len(wm.must_reach), set(wm.ent_cells), bytearray(wm.no_water))


def restore(wm, snap):
    wm.cls[:] = snap[0]
    wm.fixed.clear()
    wm.fixed.update(snap[1])
    wm.protect[:] = snap[2]
    del wm.placed[snap[3]:]
    del wm.must_reach[snap[4]:]
    wm.ent_cells.clear()
    wm.ent_cells.update(snap[5])
    wm.no_water[:] = snap[6]


def place_site(wm, key, kind, part, area, rng, cells=None, tries=400, extra=None):
    """Find a spot, stamp it, verify the area; roll back on failure."""
    for _attempt in range(6):
        dst = find_spot(wm, part, area, rng, tries=tries, cells=cells, key=key)
        if dst is None:
            return None
        snap = snapshot(wm)
        rec = wm.put(key, kind, part, dst, extra)
        if check_area_after(wm, area, rec):
            fill_pockets(wm, area)
            return rec
        restore(wm, snap)
    return None


# -------------------------------------------------------------- docks ---
# Sea meets a dock only on its WATER FACE (user 2026-10-30, bundle: sea
# against a post took a land-seamed coast tile -- "Dock | Land | Sea", the
# ship sat on the fake shore -- and the pool lost its one link to the sea
# when the post went down). The pier's RING (cells beside its tiles, outside
# the ctx window) stays land: any land class will do, a sea cell is FILLED.
# A ring cell may be sea only where vanilla shows that same pier tile with
# water on that side (below a post's foot, off Pravoka's tip), so the art
# always joins. The water opens DOCK_CHANNEL cells straight out to the sea.
DOCK_CHANNEL = 2
_D4 = ((1, 0), (-1, 0), (0, 1), (0, -1))
_RING_ORDER = (3, 6, 0, 4, 2, 5, 1, 7)      # NB8 (E W S N SE SW NE NW) clockwise from N
_RING_EDGE = (True, False, True, False, True, False, True, False)
_DOCK_RING = {}
_WET_SIDES = None


def _beside_other(own, q, area):
    return any(own[r] >= 0 and own[r] != area for r in NB4[q])


def _piece_sides(d):
    """(cell (i, j), tile, step (dx, dy), outside cell) for every side of
    piece d's tiles that faces out of its ctx window."""
    win = {(i, j) for i, j, _k in d["ctx"]}
    for i, j, t in d["cells"]:
        for dx, dy in _D4:
            q = (i + dx, j + dy)
            if q not in win:
                yield (i, j), t, (dx, dy), q


def _wet_sides():
    """{(tile, step)}: a pier tile vanilla shows with water on that side."""
    global _WET_SIDES
    if _WET_SIDES is None:
        _WET_SIDES = set()
        for d in T.DOCKS:
            pref = {(i, j): t for i, j, t in d.get("prefer", ())}
            for _c, t, step, q in _piece_sides(d):
                if q in pref and move_of_tile(pref[q]) in (SEA, RIVER):
                    _WET_SIDES.add((t, step))
    return _WET_SIDES


def dock_ring(d):
    """[(i, j, cls)] beside dock piece d's tiles, outside its ctx window:
    cls = the land class a sea cell there is filled with (rock where vanilla
    had rock), None where the sea may stay (every pier tile it touches has
    water on that side somewhere in vanilla)."""
    k = tuple(d["src"])
    ring = _DOCK_RING.get(k)
    if ring is None:
        pref = {(i, j): t for i, j, t in d.get("prefer", ())}
        wet = _wet_sides()
        dry, seen = set(), []
        for _c, t, step, q in _piece_sides(d):
            if q not in seen:
                seen.append(q)
            if (t, step) not in wet:
                dry.add(q)
        ring = []
        for q in seen:
            t = pref.get(q)
            mv = move_of_tile(t) if t is not None else WALK
            # a filled cell is grass (a lone forest / desert cell has no art)
            ring.append((q[0], q[1], None if q not in dry else "M" if mv == ROCK else "g"))
        _DOCK_RING[k] = ring
    return ring


def dock_water(d):
    """The piece's water face: its ctx 'o' cells."""
    return [(i, j) for i, j, k in d["ctx"] if k == "o"]


def dock_channels(d):
    """Every straight run of DOCK_CHANNEL cells out of the water face that
    leaves the ctx window and misses the land ring: [(face cell, run)]."""
    win = {(i, j) for i, j, _k in d["ctx"]}
    land = {(i, j) for i, j, c in dock_ring(d) if c}
    out = []
    for i, j in dock_water(d):
        for dx, dy in _D4:
            run = [(i + dx * k, j + dy * k) for k in range(1, DOCK_CHANNEL + 1)]
            if not any(q in win or q in land for q in run):
                out.append(((i, j), run))
    return out


def _runs(i, inset):
    """Edge-touching 4-connected runs of `inset` cells around cell i: a cell
    with one run is a SIMPLE point -- adding or removing it neither merges
    nor splits that set (reshape.Reshaper._runs)."""
    nb = NB8[i]
    on = [inset(nb[k]) for k in _RING_ORDER]
    if all(on):
        return 1
    start = on.index(False)
    runs, run, edge = 0, False, False
    for k in range(1, 9):
        p = (start + k) % 8
        if on[p]:
            run = True
            edge = edge or _RING_EDGE[p]
        else:
            if run and edge:
                runs += 1
            run = edge = False
    return runs


def _dock_topology_ok(wm, sea_lab, sea_id, dig, gone, walk, box):
    """Dug cells join the ship's sea (never another water) and cut no
    walkable ground in two inside `box`; every sea cell the dock covers or
    fills is a simple point of the sea, so no pond is cut off and the water
    face keeps its sea. walk: cells the dock makes walkable."""
    added, lost = set(dig), set()

    def ship(r):
        return sea_lab[r] == sea_id and wm.cls[r] == "o"

    def sea(q):
        return q in added or (q not in lost and wm.move(q) == SEA)
    joined = {q for q in dig if any(ship(r) for r in NB4[q])}
    grow = True
    while grow:
        grow = False
        for q in dig:
            if q not in joined and any(r in joined for r in NB4[q]):
                joined.add(q)
                grow = True
    if joined != added:
        return False
    if any(r not in added and wm.move(r) in (SEA, RIVER) and not ship(r) for q in dig for r in NB4[q]):
        return False
    for q in gone:
        if sea(q):
            if _runs(q, sea) > 1:
                return False
            lost.add(q)
    if dig:
        def ground(q):
            return q in walk or (q not in added and wm.walk(q))
        seeds = {r for q in dig for r in NB4[q] if ground(r)}
        if seeds:
            s0 = next(iter(seeds))
            seen, stack = {s0}, [s0]
            while stack:
                q = stack.pop()
                for r in NB4[q]:
                    if r in box and r not in seen and ground(r):
                        seen.add(r)
                        stack.append(r)
            if not seeds <= seen:
                return False
    return True


def dock_fits(wm, area, sea_lab, sea_id, c, d, away=()):
    """(score, carve, keep) if dock variant d fits with its anchor on coast
    cell c, else None. Water context may be DUG out of the area's own land
    (an inlet) as long as it joins that sea -- the pieces only face south or
    west, so a north- or east-facing coast takes its dock in an inlet. The
    land ring may be FILLED out of that sea. keep: {"water": cells that stay
    open sea, "land": cells that stay land}."""
    own = wm.ar.owner
    cx, cy = G.xy(c)
    ax, ay = d["at"]
    x0, y0 = cx - ax, cy - ay
    s = 0.0
    carve = []
    dig = []

    def ship_sea(q):
        return sea_lab[q] == sea_id and wm.cls[q] == "o"

    def diggable(q):
        return own[q] == area and not _beside_other(own, q, area) and not wm.no_water[q]
    # a pier is walkable: a pier cell beside another area's ground (outside
    # the window, where ctx checks nothing) would join that area to this one
    win = {G.idx(x0 + i, y0 + j) for i, j, _k in d["ctx"]}
    for i, j, _t in d["cells"]:
        for q in NB4[G.idx(x0 + i, y0 + j)]:
            if q not in win and wm.walk(q) and own[q] != area:
                return None
    for i, j, k in d["ctx"]:
        q = G.idx(x0 + i, y0 + j)
        if wm.protect[q] or q in wm.fixed or q in away:
            return None
        if k == "o":
            if ship_sea(q):
                s += 1
            elif diggable(q):
                dig.append(q)
            else:
                return None
        elif k == "x":
            if not (own[q] == area or ship_sea(q)):
                return None
            if own[q] == area and _beside_other(own, q, area):
                return None
        else:
            if own[q] != area:
                return None
            if wm.walk(q):
                s += 1
            elif _beside_other(own, q, area):
                return None
            else:
                carve.append((q, "g"))
    # the ring: land where vanilla had land (filled from the sea if need be)
    land, fill = [], []
    for i, j, rc in dock_ring(d):
        q = G.idx(x0 + i, y0 + j)
        mv = wm.move(q)
        if mv not in (SEA, RIVER):
            if rc:
                land.append(q)
            continue
        if not ship_sea(q):
            return None                    # a river or another water body against the pier
        if rc is None:
            continue
        if wm.protect[q] or q in wm.fixed or q in away:
            return None
        if rc != "M" and (area in wm.unland or any(wm.cls[r] == "r" for r in NB4[q])):
            rc = "M"                       # no new landing ground, no new canoe contact
        if rc != "M" and any(wm.walk(r) and own[r] >= 0 and own[r] != area for r in NB4[q]):
            return None
        fill.append((q, rc))
        land.append(q)
    # the channel: the cheapest straight run out of the water to the sea
    best = None
    for _f, run in dock_channels(d):
        qs = [G.idx(x0 + i, y0 + j) for i, j in run]
        if any(wm.protect[q] and not ship_sea(q) or q in wm.fixed or q in away for q in qs):
            continue
        if not ship_sea(qs[-1]):
            continue
        need = [q for q in qs[:-1] if not ship_sea(q)]
        if any(not diggable(q) for q in need):
            continue
        if best is None or len(need) < len(best[1]):
            best = (qs, need)
    if best is None:
        return None
    chan, cdig = best
    dig += [q for q in cdig if q not in dig]
    piece = [G.idx(x0 + i, y0 + j) for i, j, _t in d["cells"]]
    walk = set(piece) | {q for q, _c in carve} | {q for q, c in fill if c != "M"}
    m = DOCK_CHANNEL + 2
    xs = [i for i, _j, _k in d["ctx"]]
    ys = [j for _i, j, _k in d["ctx"]]
    box = {G.idx(x0 + i, y0 + j) for i in range(min(xs) - m, max(xs) + m + 1)
           for j in range(min(ys) - m, max(ys) + m + 1)}
    if not _dock_topology_ok(wm, sea_lab, sea_id, dig, piece + [q for q, _c in fill], walk, box):
        return None
    carve += [(q, "o") for q in dig] + fill
    water = [G.idx(x0 + i, y0 + j) for i, j in dock_water(d)] + chan
    return s, carve, {"water": water, "land": land}


def dockable(wm, area, sea_lab, sea_id, away=()):
    """Coast cells of `area` where some vanilla dock piece fits (the pieces
    only face south or west: a north- or east-facing coast takes none)."""
    out = []
    for c in wm.ar.areas[area].cells:
        if not any(sea_lab[j] == sea_id for j in NB4[c]):
            continue
        if any(dock_fits(wm, area, sea_lab, sea_id, c, d, away) for d in T.DOCKS):
            out.append(c)
    return out


def place_dock(wm, area, sea_lab, sea_id, rng, near=None, away=(), limit=80):
    """A vanilla dock piece on area's coast facing sea body sea_id. Every
    coast cell x dock variant is scored; context cells may be carved: land
    context -> the area's ground, water context -> that same sea (only next
    to it). near: only the `limit` coast cells closest to it. Returns the
    placed record or None."""
    coast = [i for i in wm.ar.areas[area].cells
             if wm.walk(i) and not wm.protect[i] and i not in away
             and any(sea_lab[j] == sea_id and wm.cls[j] == "o" for j in NB4[i])]
    if near is not None:
        coast.sort(key=lambda i: G.tdist2(G.xy(i), near))
        coast = coast[:limit]
    best = None
    for c in coast:
        cx, cy = G.xy(c)
        for d in T.DOCKS:
            ax, ay = d["at"]
            if not off_edge([G.idx(cx - ax + i, cy - ay + j) for i, j, _t in d["cells"]]):
                continue
            f = dock_fits(wm, area, sea_lab, sea_id, c, d, away)
            if f is None:
                continue
            s, carve, keep = f
            ax, ay = d["at"]
            water = sum(1 for dy in range(-3, 4) for dx in range(-3, 4)
                        if sea_lab[G.idx(cx + dx, cy + dy)] == sea_id)
            s += water * 0.25 - 0.8 * len(carve) + rng.random()
            if best is None or s > best[0]:
                best = (s, ((cx - ax) % N, (cy - ay) % N), d, carve, keep)
    if best is None:
        return None
    _s, (x0, y0), d, carve, keep = best
    for q, k in carve:
        wm.set(q, k)
    for i, j, t in d["cells"]:
        wm.fix(G.idx(x0 + i, y0 + j), t)
    for i, j, k in d["ctx"]:               # the land the dock opens onto stays
        q = G.idx(x0 + i, y0 + j)
        if k not in ("o", "x"):
            wm.protect[q] = 1
    for q in keep["water"]:                # the water face and its channel stay open sea
        wm.protect[q] = 1
    for q in keep["land"]:                 # the ring stays land (any class)
        wm.no_water[q] = 1
    for i, j, t in d.get("prefer", ()):
        q = G.idx(x0 + i, y0 + j)
        if q not in wm.fixed:
            wm.prefer[q] = t                # vanilla's own tiles around the pier
    ax, ay = d["at"]
    rec = dict(key="dock", kind="dock", src=tuple(d["src"]), dst=(x0, y0),
               ents=[((x0 + ax) % N, (y0 + ay) % N)],
               cells=[(i, j) for i, j, _t in d["cells"]])
    wm.placed.append(rec)
    wm.must_reach.append((G.idx(x0 + ax, y0 + ay),))
    return rec
