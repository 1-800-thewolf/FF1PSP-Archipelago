"""Stage E: decoration that can never change the access graph.

1. Mountains: ranges grow out of the ridges and massif cores rise inside big
   areas, one cell at a time, and a walkable cell only turns to rock when it
   is a simple point (paint.simple_to_block): its walkable neighbours stay
   joined. So no area, path or approach is ever cut off.
2. Ground classes inside WALK: forest / desert / grass by noise, grass rims
   on coasts, bank hints beside rivers, patches thickened to 2x2 (vanilla
   art has no single-cell forest or desert).
Movement never changes here, which is what keeps plan == tiles.
"""
import heapq

from . import grid as G
from . import noise
from . import paint as PT
from . import tables as T
from .worldmap import WALK_CLS

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8

ROCK_SHARE = {"paths": 0.0, "valley": 0.22, "two_basins": 0.22}     # start region
ROCK_RANGE = (0.18, 0.36)                                            # other regions


def _keep_clear(wm):
    """Cells mountains must not take: near stamps, docks, approaches."""
    near = set()
    for i in wm.fixed:
        near.update(G.dilate((i,), 1))
    for grp in wm.must_reach:
        for i in grp:
            near.update(G.dilate((i,), 2))
    return near


def _one_wide_after(wm, i):
    """Would blocking i leave a walkable neighbour one tile wide?"""
    for j in NB4[i]:
        if not wm.walk(j):
            continue
        e, w, s, n = NB4[j]
        we = (e == i or not wm.walk(e)) and (w == i or not wm.walk(w))
        ns = (s == i or not wm.walk(s)) and (n == i or not wm.walk(n))
        if we or ns:
            return True
    return False


def _region_pieces(wm, reg_cells):
    """Sizes of the walkable pieces among reg_cells, biggest first."""
    cs = {i for i in reg_cells if wm.walk(i)}
    seen = set()
    out = []
    for s0 in cs:
        if s0 in seen:
            continue
        n = 0
        stack = [s0]
        seen.add(s0)
        while stack:
            i = stack.pop()
            n += 1
            for j in NB4[i]:
                if j in cs and j not in seen:
                    seen.add(j)
                    stack.append(j)
        out.append(n)
    return sorted(out, reverse=True)


def grow_mountains(wm, rng):
    """Massifs = cohesive blobs of a smooth noise field inside each region's
    interior. A blob is kept only if the region does not split (no new
    walkable piece of 20+ cells) and every must-reach group stays joined."""
    D = wm.design
    h = noise.fbm(rng, 9, 3)
    clear = _keep_clear(wm)
    own = wm.ar.owner
    regions = {}
    for a in range(len(wm.ar.areas)):
        regions.setdefault(min(wm.region(a)), wm.region(a))
    for key, reg in regions.items():
        if reg & D.start and wm.dice["style"] == "paths" and                 sum(1 for b in reg for i in wm.ar.areas[b].cells if wm.walk(i)) <= wm.dice["start_cap"]:
            continue
        cells = [i for b in reg for i in wm.ar.areas[b].cells]
        cset = set(cells)
        walk = [i for i in cells if wm.walk(i)]
        if reg & D.start:
            # the start also stays inside its walkable-tile cap
            share = max(ROCK_SHARE.get(wm.dice["style"], 0.2),
                        1 - 0.92 * wm.dice["start_cap"] / max(1, len(cells)))
        else:
            share = rng.uniform(*ROCK_RANGE)
        want = int(share * len(cells)) - (len(cells) - len(walk))
        if want < 20:
            continue
        # interior: 2+ steps from anything that is not this region's ground
        edge = [i for i in walk if any(j not in cset or not wm.walk(j) for j in NB4[i])]
        dist = G.bfs_dist(bytearray(1 if (i in cset and wm.walk(i)) else 0 for i in range(NN)), edge, cap=4)
        cand = [i for i in walk if dist[i] >= 2 and i not in clear and not wm.protect[i]]
        if not cand:
            continue
        cand.sort(key=lambda i: -h[i])
        blob_cells = set(cand[:want])
        # connected blobs, biggest first
        blobs, seen = [], set()
        for s0 in blob_cells:
            if s0 in seen:
                continue
            comp = [s0]
            seen.add(s0)
            k = 0
            while k < len(comp):
                i = comp[k]
                k += 1
                for j in NB4[i]:
                    if j in blob_cells and j not in seen:
                        seen.add(j)
                        comp.append(j)
            blobs.append(comp)
        blobs.sort(key=len, reverse=True)
        base = _region_pieces(wm, cells)
        big0 = sum(1 for n in base if n >= 20)
        for blob in blobs[:12]:
            if len(blob) < 8:
                continue
            for i in blob:
                wm.cls[i] = "M"
            after = _region_pieces(wm, cells)
            big = sum(1 for n in after if n >= 20)
            if big > big0 or not PT.anchors_joined(wm, min(reg)):
                for i in blob:
                    wm.cls[i] = "g"
        PT.fill_pockets(wm, min(reg))
    _smooth_rock(wm)


def _smooth_rock(wm, rounds=2):
    """Walkable notches with 3+ rock neighbours fill in (simple points
    only); lone rock specks inside walkable ground open up."""
    own = wm.ar.owner
    for _r in range(rounds):
        for i in range(NN):
            if own[i] < 0 or i in wm.fixed or wm.protect[i]:
                continue
            c = wm.cls[i]
            if c in WALK_CLS:
                rock = sum(1 for j in NB4[i] if wm.cls[j] == "M")
                if rock >= 3 and PT.simple_to_block(wm, i) and not any(
                        wm.cls[j] in ("o", "r") for j in NB4[i]):
                    wm.cls[i] = "M"
            elif c == "M":
                rock = sum(1 for j in NB8[i] if wm.cls[j] == "M")
                if rock == 0 and PT.can_open(wm, i):
                    wm.cls[i] = "g"


def ground_classes(wm, rng):
    wet = noise.fbm(rng, 6, 3)
    dry = noise.fbm(rng, 5, 3)
    wq = noise.quantile(wet, 0.68)
    dq = noise.quantile(dry, 0.90)
    for i in range(NN):
        if i in wm.fixed or wm.cls[i] not in WALK_CLS or wm.protect[i]:
            continue
        coast = any(wm.cls[j] == "o" for j in NB4[i])
        river = any(wm.cls[j] == "r" for j in NB4[i])
        pv = wm.prefer.get(i)
        pc = T.TCLS[pv] if pv is not None else None
        if pc in ("g", "f", "d") and not river:
            wm.cls[i] = pc            # beside a stamp: vanilla's own ground, so its joins exist
        elif river:
            wm.cls[i] = "b"
        elif dry[i] > dq and not coast and not wm.no_water[i]:
            wm.cls[i] = "d"
        elif wet[i] > wq and not (coast and rng.random() < 0.7):
            wm.cls[i] = "f"
        else:
            wm.cls[i] = "g"
    _thicken(wm, ("f", "d"))
    _ring_ground(wm)
    _landable(wm)


def _ring_ground(wm):
    """Stamp rings keep vanilla's forest / desert (worldmap.put) so the
    structure's own joins exist -- but a ring is one cell thick, and a 1-wide
    forest has no art. Grow each ring patch to a 2x2 block over plain open
    ground beside it; where it cannot grow, it goes back to grass."""
    def free(j, c):
        if j in wm.fixed or wm.cls[j] == c:
            return wm.cls[j] == c
        if wm.cls[j] != "g" or (wm.protect[j] and j not in wm.prefer):
            return False
        return not any(wm.cls[r] in ("r", "o") for r in NB4[j])
    for i in sorted(wm.prefer):
        c = wm.cls[i]
        if c not in ("f", "d") or i in wm.fixed:
            continue
        x, y = G.xy(i)
        blocks = [[G.idx(x + dx + a, y + dy + b) for a in (0, 1) for b in (0, 1)]
                  for dx, dy in ((0, 0), (-1, 0), (0, -1), (-1, -1))]
        if any(all(wm.cls[j] == c for j in q) for q in blocks):
            continue
        for q in blocks:
            if all(j == i or free(j, c) for j in q):
                for j in q:
                    wm.cls[j] = c
                break
        else:
            wm.cls[i] = "g"


def _thicken(wm, classes):
    """A forest / desert cell in no 2x2 block of its class becomes grass."""
    for _round in range(3):
        changed = 0
        for i in range(NN):
            c = wm.cls[i]
            if c not in classes or i in wm.fixed or i in wm.prefer:
                continue
            ok = False
            x, y = G.xy(i)
            for dx, dy in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
                q = [G.idx(x + dx + a, y + dy + b) for a in (0, 1) for b in (0, 1)]
                if all(wm.cls[j] == c for j in q):
                    ok = True
                    break
            if not ok:
                wm.cls[i] = "g"
                changed += 1
        if not changed:
            break


def _landable(wm, need=12):
    """Every region keeps some grass for the airship to land on."""
    done = set()
    for a in range(len(wm.ar.areas)):
        key = min(wm.region(a))
        if key in done:
            continue
        done.add(key)
        cells = [i for i in wm.region_cells(a) if wm.cls[i] in WALK_CLS and i not in wm.fixed]
        grass = sum(1 for i in cells if wm.cls[i] in ("g", "d"))
        if grass >= need:
            continue
        for i in cells:
            if wm.cls[i] == "f":
                wm.cls[i] = "g"
                grass += 1
                if grass >= need:
                    break


def thicken_rock(wm, k=3, rounds=3):
    """Vanilla draws mountains as masses: 89% of its mountain cells sit in a
    3x3 block. A rock cell in no k x k rock block gets one by turning the
    fewest walkable cells of a covering window to rock -- each a simple
    point, unprotected, off coasts and banks -- or is left to the solver."""
    clear = _keep_clear(wm)

    def window_cells(x0, y0):
        return [G.idx(x0 + a, y0 + b) for a in range(k) for b in range(k)]

    for _r in range(rounds):
        changed = 0
        for i in range(NN):
            if wm.cls[i] != "M" or wm.ar.owner[i] < 0:
                continue
            x, y = G.xy(i)
            best = None
            for ox in range(k):
                for oy in range(k):
                    cells = window_cells(x - ox, y - oy)
                    need = [j for j in cells if wm.cls[j] != "M"]
                    if not need:
                        best = ()
                        break
                    if best is not None and len(need) >= len(best):
                        continue
                    if all(wm.walk(j) and not wm.protect[j] and j not in wm.fixed and j not in clear
                           and not any(wm.cls[q] in ("o", "r") for q in NB4[j]) for j in need):
                        best = need
                if best == ():
                    break
            if not best:
                continue
            flipped = []
            for j in best:
                if PT.simple_to_block(wm, j):
                    wm.cls[j] = "M"
                    flipped.append(j)
                else:
                    break
            if len(flipped) != len(best):
                for j in flipped:              # all or nothing
                    wm.cls[j] = "g"
            else:
                changed += 1
        if not changed:
            break


def drawable_rock(wm, rounds=4):
    """Every rock cell sits in a 2x2 rock block (vanilla's mountain art has no
    thinner shapes; thin rock was the solver's main dead end). A thin cell is
    grown into a block (simple points only) or, when it separates nothing --
    no neighbour of another region, so opening it joins nothing new -- turned
    to ground. Movement only changes where access cannot."""
    clear = _keep_clear(wm)
    own = wm.ar.owner

    def in_block(i):
        x, y = G.xy(i)
        for ox in (0, 1):
            for oy in (0, 1):
                if all(wm.cls[G.idx(x - ox + a, y - oy + b)] in ("M", "c")
                       for a in (0, 1) for b in (0, 1)):
                    return True
        return False

    for _r in range(rounds):
        changed = 0
        for i in range(NN):
            if wm.cls[i] != "M" or own[i] < 0 or wm.protect[i] or i in wm.fixed or in_block(i):
                continue
            x, y = G.xy(i)
            best = None
            for ox in (0, 1):
                for oy in (0, 1):
                    cells = [G.idx(x - ox + a, y - oy + b) for a in (0, 1) for b in (0, 1)]
                    need = [j for j in cells if wm.cls[j] not in ("M", "c")]
                    if all(wm.walk(j) and not wm.protect[j] and j not in wm.fixed and j not in clear
                           for j in need) and (best is None or len(need) < len(best)):
                        best = need
            grown = False
            if best:
                done = []
                for j in best:
                    if PT.simple_to_block(wm, j):
                        wm.cls[j] = "M"
                        done.append(j)
                    else:
                        break
                if len(done) == len(best):
                    grown = True
                else:
                    for j in done:
                        wm.cls[j] = "g"
            if not grown and PT.can_open(wm, i) and not any(wm.cls[j] in ("o", "r") for j in NB4[i]):
                wm.cls[i] = "g"          # separates nothing: open it
                grown = True
            changed += grown
        if not changed:
            break


def ground_rules(wm):
    """v0 grammar, movement-safe: coasts are grass; desert never meets the
    sea and is wrapped in forest."""
    for _r in range(2):
        for i in range(NN):
            if i in wm.fixed or wm.protect[i]:
                continue
            c = wm.cls[i]
            if c in ("d", "m", "f") and any(wm.cls[j] == "o" for j in NB4[i]):
                wm.cls[i] = "g"
        for i in range(NN):
            if i in wm.fixed or wm.protect[i]:
                continue
            if wm.cls[i] in ("g", "b") and any(wm.cls[j] == "d" for j in NB4[i])                     and not any(wm.cls[j] in ("o", "r") for j in NB4[i]):
                wm.cls[i] = "f"
    _thicken(wm, ("f", "d"))


def fill_dead_pockets(wm, max_size=40):
    """Small walkable pockets enclosed by rock with nothing in them (no
    fixed tile, no protected cell, no water beside them) are unreachable
    decoration: rock. FULL components are labelled first -- a capped flood
    that marked its cells 'seen' once cut real corridors in two (2026-10-17)."""
    mask = bytearray(1 if (wm.walk(i) and wm.ar.owner[i] >= 0) else 0 for i in range(NN))
    lab, sizes = G.label(mask)
    comps = {}
    for i in range(NN):
        k = lab[i]
        if k >= 0 and sizes[k] <= max_size:
            comps.setdefault(k, []).append(i)
    for comp in comps.values():
        if any(wm.protect[i] or i in wm.fixed for i in comp):
            continue
        if any(wm.cls[j] in ("o", "r") or j in wm.fixed for i in comp for j in NB4[i]):
            continue
        for i in comp:
            wm.cls[i] = "M"


_OPP4 = (1, 0, 3, 2)


def narrow_mouths(wm, length=3):
    """Vanilla rivers meet the sea ONE cell wide (all 16 mouths; there is no
    art for a 2-wide one). The last `length` cells of one side of a 2-wide
    mouth become ground (rock against rock). Only the river's own edge
    column changes and only toward its own area, so no side joins another;
    the other column keeps the river whole."""
    own = wm.ar.owner

    def frozen(c):
        return wm.protect[c] or c in wm.fixed or c in wm.ent_cells
    for i in range(NN):
        if wm.cls[i] != "r" or frozen(i):
            continue
        for d in range(4):
            if wm.cls[NB4[i][d]] != "o":
                continue
            for p in ((2, 3) if d < 2 else (0, 1)):
                j = NB4[i][p]
                if wm.cls[j] != "r" or wm.cls[NB4[j][d]] != "o":
                    continue
                run = []
                c = i
                while len(run) < length and wm.cls[c] == "r" and not frozen(c)                         and wm.cls[NB4[c][p]] == "r" and wm.cls[NB4[c][_OPP4[p]]] != "r"                         and own[c] == own[i]:
                    run.append(c)
                    c = NB4[c][_OPP4[d]]
                if not run:
                    continue
                for c in run:
                    out = NB4[c][_OPP4[p]]
                    walk = wm.walk(out) and own[out] in (own[c], -1)
                    wm.set(c, "g" if walk else "M")
                break
            if wm.cls[i] != "r":
                break


def decorate(wm, rng):
    grow_mountains(wm, rng)
    thicken_rock(wm)
    narrow_mouths(wm)
    drawable_rock(wm)
    ground_classes(wm, rng)
    ground_rules(wm)
    _landable(wm)
