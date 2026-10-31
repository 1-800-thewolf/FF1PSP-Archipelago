"""Stage A, part 2: the canal's inland sea (basin) and the harbor channel.

Nitro's canal is always on in random worlds (user 2026-10-14), its direction
rolled per seed:
  'second'  the ship starts on the main ocean; the canal opens an inland sea
            (with an island) somewhere on a big landmass
  'inner'   vanilla-style: the harbor's water IS the inland sea; the canal
            opens the main ocean (a big gate, like vanilla's)

The canal is the vanilla window (tables.CANAL, 5x4): the ship sails E-W
along it, walkers cross N-S down its middle column, open sea in its outer
columns. Its cells become a gate (closed tile -> open tile), so the skeleton
draws the CLOSED state: the basin is a separate sea until Nerrick blows it.
"""
import math

from . import grid as G
from . import tables as T

N, NN, NB4 = G.N, G.NN, G.NB4
C = T.CANAL
SRC = C["src"]
CW, CH = SRC[2], SRC[3]
RIM = C["rim"]
CHANNEL_MAX = C["channel_max"]
OPEN_SEA = 0xF00F


def closed_tile(t):
    return C["closed"].get(t, t)


def window_cells(origin):
    """[(i, open tile, closed tile)] of the canal window at origin."""
    cx, cy = origin
    out = []
    for j in range(CH):
        for i in range(CW):
            t = C["tiles"][j][i]
            out.append((G.idx(cx + i, cy + j), t, closed_tile(t)))
    return out


def noisy_radius(rng, r0, amp=0.55):
    """r(theta): a lobed blob, not a circle (five random harmonics, the low
    ones strongest)."""
    terms = [(rng.uniform(-amp, amp) / k ** 0.8, k, rng.uniform(0, 6.283)) for k in (2, 3, 4, 5, 7)]

    def r(th):
        return r0 * (1 + sum(a * math.sin(k * th + p) for a, k, p in terms))
    return r


def biggest_sea(land, exclude=()):
    sea = bytearray(0 if land[i] else 1 for i in range(NN))
    for i in exclude:
        sea[i] = 0
    lab, sizes = G.label(sea)
    if not sizes:
        return set(), lab, sizes
    k = max(range(len(sizes)), key=sizes.__getitem__)
    return {i for i in range(NN) if lab[i] == k}, lab, sizes


class Basin:
    def __init__(self, center, inner, island, canal_origin, canal_side, cells):
        self.center = center
        self.inner = inner            # basin water cells
        self.island = island          # island cells (land)
        self.canal_origin = canal_origin
        self.canal_side = canal_side  # +1: basin west of the canal, far sea east
        self.cells = cells            # [(x, y, open, closed)] gate cells


def carve(land, pinned, rng, center, rb, keep_sea=(), island_r=None):
    """Basin around center: a lobed inland lake, an island, and a RIM of land
    sealing it from every other sea. The lake keeps RIM+1 tiles off any
    existing sea, so the rim is the land that was there (no ring drawn
    through the ocean). Returns (inner, island) cell sets."""
    r = noisy_radius(rng, rb)
    ri = noisy_radius(rng, island_r, amp=0.35) if island_r else None
    inner, island, rim = set(), set(), set()
    span = int(rb * 1.8) + RIM + 2
    cx, cy = center
    box = [G.idx(cx + dx, cy + dy) for dy in range(-span - RIM - 2, span + RIM + 3)
           for dx in range(-span - RIM - 2, span + RIM + 3)]
    keep = {G.idx(*q) for q in keep_sea}
    sea_near = [i for i in box if not land[i] and i not in keep]
    dsea = _ring_dist(sea_near, RIM + 2)
    for dy in range(-span, span + 1):
        for dx in range(-span, span + 1):
            d = math.hypot(dx, dy)
            th = math.atan2(dy, dx)
            i = G.idx(cx + dx, cy + dy)
            rr = r(th)
            if d < rr:
                if ri is not None and d < ri(th):
                    island.add(i)
                elif dsea.get(i, 99) > RIM + 1 or i in keep:
                    inner.add(i)
            elif d < rr + RIM:
                rim.add(i)
    # one lake: its biggest 4-connected body (lobes cut off by the
    # keep-off-the-sea rule would be separate seas)
    if inner:
        best, seen = set(), set()
        for s0 in inner:
            if s0 in seen:
                continue
            comp = {s0}
            stack = [s0]
            while stack:
                i = stack.pop()
                for j in NB4[i]:
                    if j in inner and j not in comp:
                        comp.add(j)
                        stack.append(j)
            seen |= comp
            if len(comp) > len(best) or (keep & comp and not keep & best):
                best = comp
        rim |= inner - best
        inner = best
    for i in rim:
        if not pinned[i]:
            land[i] = 1
    for i in inner:
        if not pinned[i] or i in keep:
            land[i] = 0
    for i in island:
        if not pinned[i]:
            land[i] = 1
    for i in keep:
        land[i] = 0
    return inner, island


def _ring_dist(seeds, cap):
    """{cell: 4-step distance} from seeds, up to cap (a small local BFS)."""
    dist = {i: 0 for i in seeds}
    front = list(seeds)
    for d in range(1, cap + 1):
        nxt = []
        for i in front:
            for j in NB4[i]:
                if j not in dist:
                    dist[j] = d
                    nxt.append(j)
        front = nxt
    return dist


def _true_island(land, island, min_size=40):
    """The basin's island is its own landmass (water all round it)."""
    if not island:
        return False
    s0 = next(iter(island))
    if not land[s0]:
        return False
    seen = {s0}
    stack = [s0]
    while stack:
        i = stack.pop()
        for j in NB4[i]:
            if land[j] and j not in seen:
                if j not in island:
                    return False          # joined to the shore
                seen.add(j)
                stack.append(j)
    return len(seen) >= min_size


def place_canal(land, pinned, rng, center, inner, ocean):
    """Canal window on the basin's east or west rim at the centre row, with
    a 3-row channel from its far column to `ocean`. Returns (origin, side,
    channel cells) or None."""
    cx, cy0 = center
    row = [G.xy(i)[0] for i in inner if G.xy(i)[1] == cy0 % N]
    if not row:
        return None
    rel = sorted(((x - cx + N // 2) % N - N // 2) for x in row)
    east, west = rel[-1], rel[0]
    sides = [1, -1]
    rng.shuffle(sides)
    for side in sides:
        oy = (cy0 - C["row"]) % N
        ox = (cx + east) % N if side > 0 else (cx + west - (CW - 1)) % N
        far = ox + (CW - 1 if side > 0 else 0)
        chan = []
        ok = False
        for k in range(1, CHANNEL_MAX + 1):
            x = far + side * k
            col = [G.idx(x, oy + j) for j in (1, 2, 3)]
            if any(i in ocean for i in col):
                ok = True
                break
            chan += col
        if not ok:
            continue
        win = window_cells((ox, oy))
        if any(pinned[i] for i, _o, _c in win) or any(pinned[i] for i in chan):
            continue
        return (ox, oy), side, chan
    return None


def build(land, pinned, rng, canal_dir, harbor_water, avoid):
    """Carve this seed's basin + canal into the skeleton. `avoid`: [(x, y,
    radius)] the 'second' basin keeps clear of. Returns (Basin, direction
    actually built) or (None, None)."""
    tries = (canal_dir, "second") if canal_dir == "inner" else ("second",)
    for d in tries:
        for _attempt in range(40):
            saved = bytearray(land)
            rb = rng.randint(13, 18)
            if d == "inner":
                hx = 202 + rng.randint(-3, 3)
                center = (hx % N, (146 + rb - 1) % N)
                keep = list(harbor_water) + [(x, y) for y in range(147, 146 + rb) for x in (201, 202)]
                island_r = rng.choice((5, 6, 7))    # its south shore takes the ship's dock
            else:
                center = (rng.randrange(N), rng.randrange(N))
                if not land[G.idx(*center)]:
                    continue
                if any(G.tdist(center, (ax, ay)) < ar_ + rb for ax, ay, ar_ in avoid):
                    continue
                disk = G.disk(center[0], center[1], rb * 1.2 + RIM)
                if sum(land[i] for i in disk) < 0.6 * len(disk):
                    continue
                keep = ()
                island_r = rng.choice((4, 5, 6))
            inner, island = carve(land, pinned, rng, center, rb, keep, island_r)
            if len(inner) < 120 or not _true_island(land, island):
                land[:] = saved
                continue
            # the ocean as it is NOW: the rim may have reshaped the coast
            ocean, _lab, _sz = biggest_sea(land, exclude=inner)
            got = place_canal(land, pinned, rng, center, inner, ocean)
            if got is None:
                land[:] = saved
                continue
            origin, side, chan = got
            for i in chan:
                land[i] = 0
            win = window_cells(origin)
            cells = []
            for i, o, c in win:
                land[i] = 0 if T.ATT[c] == OPEN_SEA else 1
                pinned[i] = 1
                x, y = G.xy(i)
                if o != c:
                    cells.append((x, y, o, c))
            # walkers carry on north and south of the window
            ox, oy = origin
            for j in (-3, -2, -1, CH, CH + 1, CH + 2):
                for i in range(-1, CW + 1):
                    k = G.idx(ox + i, oy + j)
                    land[k] = 1
                    pinned[k] = 1
            for i in chan:
                pinned[i] = 1
            for i in inner:
                pinned[i] = 1
            for i in island:
                pinned[i] = 1
            # the basin must be ITS OWN sea while the canal is shut, and the
            # far side the main ocean
            _o, lab, sz = biggest_sea(land)
            bi = next((i for i in inner if not land[i]), None)
            far = G.idx(origin[0] + (CW - 1 if side > 0 else 0), origin[1] + 2)
            near = G.idx(origin[0] + (0 if side > 0 else CW - 1), origin[1] + 2)
            big = max(range(len(sz)), key=sz.__getitem__) if sz else -1
            # the near side must BE the basin, the far side the main ocean
            if bi is None or lab[near] != lab[bi] or lab[bi] == lab[far] or lab[far] != big:
                land[:] = saved
                continue
            return Basin(center, inner, island, origin, side, cells), d
    return None, None


def harbor_channel(land, pinned, harbor_water, mouth=(202, 148), rng=None):
    """'second': a 3-wide sea channel from the pier to the main ocean when the
    pier's water is cut off: the cheapest path through unpinned land under a
    noisy step cost, so it meanders like an inlet instead of a straight cut."""
    import heapq
    import random as _r
    rng = rng or _r.Random(1)
    ocean, lab, _sz = biggest_sea(land)
    hw = [G.idx(*q) for q in harbor_water]
    if any(i in ocean for i in hw):
        return []
    start = G.idx(*mouth)
    jitter = {}
    prev = {start: None}
    best = {start: 0.0}
    heap = [(0.0, start)]
    end = None
    while heap:
        d, i = heapq.heappop(heap)
        if d > best.get(i, 1e18):
            continue
        if i in ocean:
            end = i
            break
        for j in NB4[i]:
            if pinned[j] and land[j]:
                continue
            if j not in jitter:
                jitter[j] = 1.0 + 3.0 * rng.random() ** 2
            nd = d + jitter[j]
            if nd < best.get(j, 1e18):
                best[j] = nd
                prev[j] = i
                heapq.heappush(heap, (nd, j))
    if end is None:
        return []
    path = []
    while end is not None:
        path.append(end)
        end = prev[end]
    out = []
    for i in path:
        for j in (i,) + G.NB8[i]:
            if not pinned[j]:
                land[j] = 0
                out.append(j)
    for i in hw:
        land[i] = 0
    return out
