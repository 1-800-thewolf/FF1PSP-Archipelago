"""Stage F: tiles for the class map, MOVEMENT-LOCKED (RANDWORLD_V2 section 3).

A bitmask WFC (v0 proto.wfc, made pure Python and movement-safe):
  domain[i] = tiles of the cell's class (fixed cells: their tile); cells deep
              inside a mass of one class take only vanilla's interior tiles
  rule      = the learned pixel-seam pairs (tables.TILES["compat"])
  collapse  = lowest entropy first, weighted by vanilla frequency with a
              per-class share budget on shores (v0's spiky-shore lesson)
  conflict  = wipe a window around the dead cell and re-solve it, growing
              the window; after repeated failure the window's cells are
              SOFTENED to every tile of the same movement + landing kind
              (never a different movement: plan == tiles)
"""
import heapq

from . import grid as G
from . import tables as T

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8
TB = T.TILES
ALPHA = TB["alpha"]
K = len(ALPHA)
KI = {t: k for k, t in enumerate(ALPHA)}
FREQ = TB["freq"]
VSHARE = TB["vshare"]
# E W S N SE SW NE NW (grid.NB8 order): edge-type pairs AND the corner-quad
# projections (gen_data.tile_tables), so corners propagate like edges
COMPAT = [[int(h, 16) for h in TB["compat"][d]] for d in range(8)]
NDIR = 8
CLS_MASK = {c: int(h, 16) for c, h in TB["cls_mask"].items()}
INTERIOR = {c: int(h, 16) for c, h in TB["interior"].items()}
TCLS_K = [T.TCLS[t] for t in ALPHA]
ATT = T.ATT
OPP = (1, 0, 3, 2)


def _kind(t):
    """Movement + landing kind of a tile: what logic can tell apart."""
    a = ATT[t]
    if a == 0xF00F:
        return "sea"
    if a == 0xF009:
        return "river"
    if a >= 0xF000:
        return "block"
    if a in (0x0003, 0x0002, 0x000D, 0x000C, 0x000E):
        return "land"            # walkable, airship can land
    if a == 0x0000:
        return "dock"
    return "walk"                # walkable, no landing (forest, marsh banks)


_free = (1 << K) - 1
for _t in TB["trigger_art"]:
    if _t in KI:
        _free &= ~(1 << KI[_t])
_free &= ~CLS_MASK["x"]
# one-off art (vanilla shows it <= 3 times: canal water 0x173, the canal
# crossing 0x174, ...) is never a free choice -- only as a stamp's own tile
# (playtest 2026-10-17: a whole coast column of 0x173 beside Matoya)
for _k, _f in enumerate(FREQ):
    if _f <= 3:
        _free &= ~(1 << _k)
KIND_MASK = {}
for _k, _t in enumerate(ALPHA):
    if _free >> _k & 1:
        KIND_MASK.setdefault(_kind(_t), 0)
        KIND_MASK[_kind(_t)] |= 1 << _k
# PAIRN[d][q] = {a: n}: vanilla put tile a at direction d (E W S N) of q n times
PAIRN = [[{} for _ in range(K)] for _ in range(4)]
for _a, _b, _n in TB["pairs"]["0"]:
    PAIRN[0][_a][_b] = _n
    PAIRN[1][_b][_a] = _n
for _a, _b, _n in TB["pairs"]["2"]:
    PAIRN[2][_a][_b] = _n
    PAIRN[3][_b][_a] = _n


_TOT = float(sum(FREQ))
_SMOOTH = 2.0                           # pseudo-counts, spread by background frequency


def pair_p(q, d, a):
    """P(tile a at direction d | q), smoothed TOWARD a's own frequency: a rare
    q with few observations falls back to the background, instead of a flat
    pseudo-count that made every rare tile look like a perfect fit (the 0x173
    coast column, 2026-10-17)."""
    return (PAIRN[d][q].get(a, 0) + _SMOOTH * FREQ[a] / _TOT) / (FREQ[q] + _SMOOTH)


def neighbour_weight(q, nbrs):
    """P(neighbours | q): how vanilla arranges q with the tiles already
    placed around it. nbrs = [(direction, alpha idx)]."""
    w = 1.0
    for d, a in nbrs:
        w *= pair_p(q, d, a)
    return w


OCEANY = int(TB["oceany"], 16)          # non-sea tiles with ocean art: only beside sea
RIVER_OPEN = int(TB["river_open"], 16)  # river tiles that LOOK like open river


def near_sea(cls, i):
    return cls[i] == "o" or any(cls[j] == "o" for j in NB8[i])


def restrict(cls, i, m, open_water=()):
    """Per-cell art rules on top of the class: no ocean art away from the
    sea, open river on a river site's doorstep. Never empties a domain."""
    r = m
    if not near_sea(cls, i):
        r &= ~OCEANY
    if i in open_water:
        r &= RIVER_OPEN
    return r or m


CLASS_KIND = {"o": "sea", "r": "river", "M": "block", "c": "block", "g": "land", "d": "land",
              "f": "walk", "m": "walk"}


def _soft_mask(c):
    if c in CLASS_KIND:
        return KIND_MASK[CLASS_KIND[c]]
    if c in ("b", "w"):
        return KIND_MASK["land"] | KIND_MASK["walk"]
    return CLS_MASK.get(c, _free)


def _bits(m):
    while m:
        low = m & -m
        yield low.bit_length() - 1
        m ^= low


# MAP_00 holds the grid wp16-compressed inside a fixed slot: measured
# 2026-10-17, the PCK's compressed size = compressed(grid alone) + 310,820 B
# exactly, and the slot is 344,320 B (bake re-simplifies above slot - 512).
GRID_BUDGET = 344320 - 310820 - 900
FREQ_POWERS = (2.5, 3.5, 5.0)          # stronger bias -> fewer variants -> smaller


def _chunk_tables():
    """_CHUNK[d][k][b] = OR of COMPAT[d][8k + i] over the bits i of byte b."""
    out = []
    for d in range(NDIR):
        comp = COMPAT[d]
        per = []
        for k in range((K + 7) // 8):
            t = [0] * 256
            for b in range(1, 256):
                low = b & -b
                i = 8 * k + low.bit_length() - 1
                t[b] = t[b ^ low] | (comp[i] if i < K else 0)
            per.append(t)
        out.append(per)
    return out


_CHUNK = _chunk_tables()


class _LiveGrid:
    """gk-like view for _solve_window inside the solver: window cells read
    their trial value, collapsed cells their tile, open cells None."""

    def __init__(self, solver, inw):
        self.s = solver
        self.inw = inw
        self.vals = {c: None for c in inw}

    def __getitem__(self, i):
        if i in self.inw:
            return self.vals[i]
        if self.s.done[i] and self.s.dom[i]:
            return self.s.dom[i].bit_length() - 1
        return None

    def __setitem__(self, i, v):
        self.vals[i] = v


class Solver:
    def __init__(self, cls, fixed, rng, power=1.0, prefer=None, open_water=()):
        self.rng = rng
        self.open_water = set(open_water)
        self.prefer = {i: KI[t] for i, t in (prefer or {}).items() if t in KI}
        self.freq = [f ** power for f in FREQ]
        self.memo = [dict() for _ in range(NDIR)]
        self.fixed = {i: KI[t] for i, t in fixed.items() if t in KI}
        self.extra_fixed = {i: t for i, t in fixed.items() if t not in KI}
        dom0 = [0] * NN
        for i in range(NN):
            c = cls[i]
            if i in self.fixed:
                dom0[i] = 1 << self.fixed[i]
                continue
            m = CLS_MASK.get(c, _free) & _free
            if c == "M" and any(cls[j] == "o" for j in NB4[i]):
                m |= CLS_MASK["c"] & _free         # blocked coast: mountain or cliff
            if c in INTERIOR and all(cls[j] == c for j in NB8[i]):
                mi = m & INTERIOR[c]
                if mi:
                    m = mi
            m = m or _soft_mask(c)
            pk = self.prefer.get(i)
            if pk is not None and m >> pk & 1 == 0 and _soft_mask(c) >> pk & 1                     and not (OCEANY >> pk & 1 and not near_sea(cls, i)):
                m |= 1 << pk                       # vanilla's ring tile, same movement
            dom0[i] = restrict(cls, i, m, self.open_water)
        self.cls = cls
        self.dom0 = dom0
        self.dom = list(dom0)
        self.done = bytearray(NN)
        for i in self.fixed:
            self.done[i] = 1
        for i in self.extra_fixed:
            self.done[i] = 1
        self.frozen = set(self.fixed) | set(self.extra_fixed)
        self.softened = set()
        self.forced = set()
        self.used = [0] * K
        self.used_cls = {}
        self.resets = 0

    def support(self, d, dom):
        memo = self.memo[d]
        r = memo.get(dom)
        if r is None:
            r = 0
            if dom.bit_count() <= 6:
                c = COMPAT[d]
                for k in _bits(dom):
                    r |= c[k]
            else:                          # byte-chunk tables: ~K/8 lookups
                tab = _CHUNK[d]
                k = 0
                m = dom
                while m:
                    b = m & 255
                    if b:
                        r |= tab[k][b]
                    m >>= 8
                    k += 1
            if len(memo) < 300000:
                memo[dom] = r
        return r

    def ac(self, queue):
        """Arc consistency from queue; returns the first cell emptied (its
        domain left as it was) or None."""
        dom = self.dom
        frozen = self.frozen
        inq = set(queue)
        while queue:
            i = queue.pop()
            inq.discard(i)
            di = dom[i]
            nb = NB8[i]
            for d in range(NDIR):
                j = nb[d]
                if j in frozen:
                    continue
                nd = dom[j] & self.support(d, di)
                if nd != dom[j]:
                    if nd == 0:
                        return j
                    dom[j] = nd
                    if j not in inq:
                        inq.add(j)
                        queue.append(j)
        return None

    def _quads_ok_live(self, i, k):
        dom, done = self.dom, self.done
        for v, pos in CV[i]:
            q = []
            for u in VC[v]:
                if u == i:
                    q.append(k)
                elif done[u] and dom[u]:
                    q.append(dom[u].bit_length() - 1)
                else:
                    break
            else:
                if not _quad_ok(*q):
                    return False
        return True

    def backtrack(self, bad, sizes=(1, 2), budget=300):
        """A contradiction at `bad`: re-solve the window around it exactly
        (forward-checking DFS against everything outside the window) before
        the wipe-and-soften fallback. Returns None when resolved."""
        dom, done = self.dom, self.done
        bx, by = G.xy(bad)
        for r in sizes:
            win = [G.idx(bx + dx, by + dy) for dy in range(-r, r + 1) for dx in range(-r, r + 1)]
            cells = [c for c in win if c not in self.frozen]
            if not cells:
                return bad
            inw = set(cells)
            saved = [(dom[c], done[c]) for c in cells]
            gk = _LiveGrid(self, inw)
            masks = []
            for c in cells:
                m = self.dom0[c]
                for d, j in enumerate(NB8[c]):
                    if j not in inw:
                        m &= self.support(_OPP8[d], dom[j])
                masks.append(m)
            if all(masks) and _solve_window(gk, cells, masks, self.rng, budget, self.freq, self.prefer):
                for c in cells:
                    dom[c] = 1 << gk.vals[c]
                    done[c] = 1
                edge = [j for c in cells for j in NB8[c] if j not in inw]
                if self.ac(edge + cells) is None:
                    return None
            for c, (dv, dn) in zip(cells, saved):
                dom[c], done[c] = dv, dn
        return bad

    def budget(self, k):
        c = TCLS_K[k]
        if c not in ("o", "c", "g"):
            return 1.0
        tot = self.used_cls.get(c, 0)
        if tot < 50:
            return 1.0
        share = self.used[k] / tot
        want = VSHARE[k] or 1e-3
        return 1.0 if share <= want else (want / share) ** 3

    def settle(self):
        """Initial propagation; cells the rules cannot satisfy get softened."""
        for _round in range(20):
            bad = self.ac(list(range(NN)))
            if bad is None:
                return
            self.soften_at(bad, 1)
            self.dom[:] = [self.dom0[i] if not self.done[i] else self.dom[i] for i in range(NN)]

    def soften_at(self, bad, r):
        bx, by = G.xy(bad)
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                j = G.idx(bx + dx, by + dy)
                if j in self.frozen:
                    continue
                self.dom0[j] = restrict(self.cls, j, _soft_mask(self.cls[j]), self.open_water)
                self.softened.add(j)

    def run(self, max_resets=4000):
        rng = self.rng
        dom = self.dom
        done = self.done
        self.settle()
        heap = [(dom[i].bit_count(), rng.random(), i) for i in range(NN) if not done[i]]
        heapq.heapify(heap)
        fails = {}
        n = 0
        while heap:
            cnt, _r, i = heapq.heappop(heap)
            if done[i]:
                continue
            bc = dom[i].bit_count()
            if bc != cnt:
                heapq.heappush(heap, (bc, rng.random(), i))
                continue
            opts = list(_bits(dom[i]))
            # exact corner quads with the neighbours already placed
            good = [q for q in opts if self._quads_ok_live(i, q)]
            opts = good or opts
            pc = self.cls[i]
            pk = self.prefer.get(i)
            if pk is not None and pk in opts:
                k = pk                     # vanilla's own tile beside a stamp: exact joins
            elif len(opts) == 1:
                k = opts[0]
            else:
                nb4 = NB4[i]
                nbrs = [(d, dom[nb4[d]].bit_length() - 1) for d in range(4)
                        if done[nb4[d]] and dom[nb4[d]]]
                w = [self.freq[q] * (60 if TCLS_K[q] == pc else 1) * self.budget(q)
                     * neighbour_weight(q, nbrs) + 1e-12 for q in opts]
                k = rng.choices(opts, w)[0]
            self.used[k] += 1
            self.used_cls[TCLS_K[k]] = self.used_cls.get(TCLS_K[k], 0) + 1
            dom[i] = 1 << k
            done[i] = 1
            n += 1
            bad = self.ac([i])
            if bad is not None:
                bad = self.backtrack(bad)
            R = 2
            while bad is not None:
                self.resets += 1
                if self.resets > max_resets:
                    break
                blk = (G.xy(bad)[1] // 6) * 64 + G.xy(bad)[0] // 6
                fails[blk] = fails.get(blk, 0) + 1
                R = min(R + 1, 10)
                if fails[blk] >= 12 or bad in self.softened and fails[blk] >= 6:
                    # unsatisfiable here: force a tile of the cell's own
                    # movement kind, freeze it and accept the seam
                    m = restrict(self.cls, bad, _soft_mask(self.cls[bad]), self.open_water)
                    opts = list(_bits(m))
                    k2 = max(opts, key=lambda q: FREQ[q] * (3 if TCLS_K[q] == self.cls[bad] else 1))
                    dom[bad] = 1 << k2
                    done[bad] = 1
                    self.frozen.add(bad)
                    self.forced.add(bad)
                    fails[blk] = 0
                    bad = self.ac([bad] + [j for j in NB8[bad] if not done[j]])
                    continue
                if fails[blk] >= 3:
                    self.soften_at(bad, 2 if fails[blk] < 8 else R)
                bx, by = G.xy(bad)
                wipe = []
                for dy in range(-R, R + 1):
                    for dx in range(-R, R + 1):
                        j = G.idx(bx + dx, by + dy)
                        if j in self.frozen:
                            continue
                        done[j] = 0
                        dom[j] = self.dom0[j]
                        wipe.append(j)
                edge = []
                for dy in range(-R - 1, R + 2):
                    for dx in range(-R - 1, R + 2):
                        if max(abs(dx), abs(dy)) == R + 1:
                            edge.append(G.idx(bx + dx, by + dy))
                bad = self.ac(edge + wipe)
                for j in wipe:
                    heapq.heappush(heap, (dom[j].bit_count(), rng.random(), j))
            if self.resets > max_resets:
                break
            if len(heap) > 3 * NN:
                heap = [(dom[j].bit_count(), rng.random(), j) for j in range(NN) if not done[j]]
                heapq.heapify(heap)
        grid = [0] * NN
        for i in range(NN):
            if i in self.extra_fixed:
                grid[i] = self.extra_fixed[i]
                continue
            d = dom[i]
            if d:
                grid[i] = ALPHA[d.bit_length() - 1]
            else:
                # last resort: any tile of the cell's own kind
                grid[i] = ALPHA[_soft_mask(self.cls[i]).bit_length() - 1]
        return grid


def compressed_size(grid_on_disc):
    """wp16 size of the grid alone (see GRID_BUDGET)."""
    import struct
    from ..client import wp16
    return len(wp16.compress(struct.pack("<%dH" % NN, *grid_on_disc)))


def open_water_cells(wm):
    """River sites' doorsteps (approach cells reached by canoe)."""
    out = set()
    for p in wm.placed:
        for rec in p.get("appr") or ():
            if len(rec) >= 5 and rec[4] == "r":
                out.add(G.idx(rec[0], rec[1]))
    return out


def solve(wm, rng, disc=None, same_graph=None):
    """Solve, repair and polish; compress the finished grid and, only if it
    overflows the MAP_00 slot, do it all again with a stronger frequency
    bias. disc(grid) -> the grid as written to disc. same_graph(grid) ->
    False if repair moved an edge of the access graph: repair again without
    its shape flips, then without its forest <-> grass swaps."""
    from .reshape import Reshaper
    # land packs bigger than sea: a mostly-land world starts at the strongest
    # bias (2026-10-17, with the water rule: pangaea fit at 3.5 on 2 of 5
    # seeds, at 2.5 on none -- each failed try costs a whole solve)
    land = sum(1 for c in wm.cls if c not in ("o",)) / NN
    k = len(FREQ_POWERS) - 1 if land > 0.5 else 0
    ow = open_water_cells(wm)
    nl = getattr(wm, "noland", None) or frozenset()
    while True:
        power = FREQ_POWERS[k]
        S = Solver(wm.cls, wm.fixed, rng, power, prefer=wm.prefer, open_water=ow)
        raw = S.run()
        rs = Reshaper(wm) if same_graph is not None else None
        grid, rep = repair(raw, wm.cls, wm.fixed, rng, prefer=wm.prefer, reshaper=rs, open_water=ow,
                           noland=nl)
        if same_graph is not None and not same_graph(grid):
            # a flip or a forest<->grass swap moved an edge of the access graph
            # (an airship landing, most likely): drop the flips, then the swaps
            rs.undo()                      # the class map back as the plan drew it
            grid, rep = repair(raw, wm.cls, wm.fixed, rng, prefer=wm.prefer, open_water=ow, noland=nl)
            rep["shape_undone"] = True
            if not same_graph(grid):
                grid, rep = repair(raw, wm.cls, wm.fixed, rng, prefer=wm.prefer,
                                   tiers=("class", "move"), open_water=ow, noland=nl)
                rep["foot_undone"] = True
        grid, polished = polish_runs(grid, wm.cls, wm.fixed, prefer=wm.prefer, open_water=ow)
        size = compressed_size(disc(grid) if disc else grid)
        if size <= GRID_BUDGET or k == len(FREQ_POWERS) - 1:
            break
        if rs is not None:
            rs.undo()
        # far over: skip the middle power (each round costs a whole solve)
        k = min(k + (2 if size > GRID_BUDGET * 1.12 else 1), len(FREQ_POWERS) - 1)
    wm.report["tiles"] = dict(resets=S.resets, softened=len(S.softened), forced=len(S.forced),
                              polished=polished,
                              power=power, packed=size, budget=GRID_BUDGET,
                              bad_before=rep["before"], bad_after=rep["after"],
                              foot_undone=rep.get("foot_undone", False),
                              shape_undone=rep.get("shape_undone", False) or rep.get("foot_undone", False),
                              flips=rep.get("flips", 0))
    return grid


def seam_count(grid):
    """Pairs of neighbours the learned rules do not allow (E and S)."""
    bad = 0
    for i in range(NN):
        a = KI.get(grid[i])
        if a is None:
            continue
        e, _w, s, _n = NB4[i]
        b = KI.get(grid[e])
        if b is not None and not COMPAT[0][a] >> b & 1:
            bad += 1
        b = KI.get(grid[s])
        if b is not None and not COMPAT[2][a] >> b & 1:
            bad += 1
    return bad


# ------------------------------------------------------------ corner repair --
# v0's awfc.repair, pure Python: the 8-neighbour compat above enforces each
# corner quad only pairwise, so a few quads (and seams next to forced cells)
# stay illegal. Each is re-solved in a small window, class-locked (so the
# movement of every cell is untouched), with the exact quad rule.
CORNERS = TB["corners"]                       # per alpha index: TL TR BL BR type
_OPP8 = (1, 0, 3, 2, 7, 6, 5, 4)              # E<->W S<->N SE<->NW SW<->NE
QUADS = {tuple(q) for q in TB["quads"]}
SPECIAL_K = {KI[t] for t in TB["special"] if t in KI}
# quads with a structure / one-off tile that vanilla itself shows exactly
EXACT_Q = {tuple(KI[t] for t in q) for q in TB["exact_quads"] if all(t in KI for t in q)}


_CTL = [c[3] for c in CORNERS]                # the corner each tile puts at a vertex
_CTR = [c[2] for c in CORNERS]                # when it sits TL / TR / BL / BR of it
_CBL = [c[1] for c in CORNERS]
_CBR = [c[0] for c in CORNERS]


def _quad_ok(a, b, c, d):
    """Tiles (alpha idx) around one vertex: a TL, b TR, c BL, d BR. Legal if
    the four corner types form a quad vanilla shows, or (around stamps) the
    exact tile quad occurs in vanilla."""
    if (_CTL[a], _CTR[b], _CBL[c], _CBR[d]) in QUADS:
        return True
    return (a, b, c, d) in EXACT_Q


# vertex v is named by its TL cell: VC[v] = its cells TL TR BL BR; CV[c] =
# the 4 vertices of cell c with c's position in each
VC = [(v, NB8[v][0], NB8[v][2], NB8[v][4]) for v in range(NN)]
CV = [((NB8[c][7], 3), (NB8[c][3], 2), (NB8[c][1], 1), (c, 0)) for c in range(NN)]


def _pair_ok(a, b, d):
    return COMPAT[d][a] >> b & 1


def bad_spots(gk):
    """Cells to repair: the TL cell of every illegal vertex, and both cells of
    every illegal E/S edge. gk = grid as alpha indices (None for others)."""
    out = set()
    for i in range(NN):
        a = gk[i]
        if a is None:
            continue
        e, _w, s, _n, se = NB8[i][0], NB8[i][1], NB8[i][2], NB8[i][3], NB8[i][4]
        b, c, d = gk[e], gk[s], gk[se]
        if b is not None and not _pair_ok(a, b, 0):
            out.add(i)
            out.add(e)
        if c is not None and not _pair_ok(a, c, 2):
            out.add(i)
            out.add(s)
        if b is not None and c is not None and d is not None and not _quad_ok(a, b, c, d):
            out.add(i)
    return out


def _quads_ok_at(gk, c, t):
    """Every vertex of cell c whose other three tiles are known is legal."""
    for v, pos in CV[c]:
        a, b, cc, d = VC[v]
        q = [gk[a], gk[b], gk[cc], gk[d]]
        q[pos] = t
        if None not in q and not _quad_ok(*q):
            return False
    return True


def _solve_window(gk, cells, base_masks, rng, budget, weight=None, pref=None, home=None):
    """Forward-checking DFS over `cells` (gk None there). base_masks: what the
    cells OUTSIDE the window allow. Assigning a cell narrows its unassigned
    window neighbours by bitmask; quads are checked exactly. Assigns gk on
    success, leaves the cells None on failure."""
    idx = {c: k for k, c in enumerate(cells)}
    nbrs = [[(d, idx[j]) for d, j in enumerate(NB8[c]) if j in idx] for c in cells]
    weight = weight or FREQ
    nodes = [0]

    def dfs(masks, left):
        if not left:
            return True
        nodes[0] += 1
        if nodes[0] > budget:
            return False
        k = min(left, key=lambda q: masks[q].bit_count())
        c = cells[k]
        if home is not None:               # the cell's own movement first: fewest flips
            hm = home[k]
            opts = sorted(_bits(masks[k]), key=lambda q: (not hm >> q & 1, -weight[q] * (0.6 + 0.8 * rng.random())))
        else:
            opts = sorted(_bits(masks[k]), key=lambda q: -weight[q] * (0.6 + 0.8 * rng.random()))
        pk = pref.get(c) if pref else None
        if pk is not None and masks[k] >> pk & 1:
            opts.remove(pk)
            opts.insert(0, pk)              # vanilla's own tile beside a stamp first
        rest = [q for q in left if q != k]
        for t in opts:
            if not _quads_ok_at(gk, c, t):
                continue
            nm = list(masks)
            dead = False
            for d, j in nbrs[k]:
                if gk[cells[j]] is None:
                    nm[j] &= COMPAT[d][t]
                    if not nm[j]:
                        dead = True
                        break
            if dead:
                continue
            gk[c] = t
            if dfs(nm, rest):
                return True
            gk[c] = None
        return False
    return dfs(list(base_masks), list(range(len(cells))))


_FOOT = KIND_MASK["land"] | KIND_MASK["walk"]


def _legal_at(gk, c, q):
    nb = NB8[c]
    for d in range(8):
        j = gk[nb[d]]
        if j is not None and not COMPAT[d][q] >> j & 1:
            return False
    return _quads_ok_at(gk, c, q)


def polish_coast(grid, cls, fixed, rng, prefer=None, open_water=(), sweeps=3):
    """Shores the way vanilla draws them (playtest 2026-10-17: "shores are
    largely jagged" -- the coast OUTLINE turns as often as vanilla's, 59 vs
    60 per 100 edges; it was the shore variants, picked by frequency alone).
    Iterated conditional modes over the coast band: each cell takes the tile
    vanilla most expects next to its 4 neighbours, among tiles legal with all
    8 neighbours and every vertex, of the cell's own movement + landing kind
    and art rules. A cell that is legal now is only replaced by a legal one,
    so no seam is added and no movement changes."""
    import math
    gk = [KI.get(t) for t in grid]
    frozen = set(fixed) | set(prefer or ())
    band = [i for i in range(NN) if i not in frozen and gk[i] is not None
            and any((cls[j] == "o") != (cls[i] == "o") for j in NB4[i])]
    logf = [math.log(f + 1.0) for f in FREQ]
    changed = 0
    for _s in range(sweeps):
        rng.shuffle(band)
        moved = 0
        for c in band:
            t0 = gk[c]
            nbrs = [(d, gk[NB4[c][d]]) for d in range(4) if gk[NB4[c][d]] is not None]
            cand = restrict(cls, c, KIND_MASK.get(_kind(ALPHA[t0]), 0) | (1 << t0), open_water)
            ok0 = _legal_at(gk, c, t0)
            best, bs = t0, None
            for q in _bits(cand):
                if q != t0 and not _legal_at(gk, c, q):
                    continue
                if q == t0 and not ok0:
                    continue
                sc = logf[q]
                for d, a in nbrs:
                    sc += math.log(pair_p(q, d, a))
                if bs is None or sc > bs + 1e-9:
                    best, bs = q, sc
            if best != t0:
                gk[c] = best
                moved += 1
        changed += moved
        if not moved:
            break
    out = [ALPHA[k] if k is not None else grid[i] for i, k in enumerate(gk)]
    return out, changed


def _chains(band, horizontal):
    """Maximal straight runs of band cells (rows or columns), in order."""
    out = []
    for a in range(N):
        run = []
        for b in range(N):
            c = G.idx(b, a) if horizontal else G.idx(a, b)
            if c in band:
                run.append(c)
            else:
                if len(run) >= 2:
                    out.append(run)
                run = []
        if len(run) >= 2:
            out.append(run)
    return out


def polish_runs(grid, cls, fixed, prefer=None, open_water=(), sweeps=2):
    """Shore SEQUENCES (playtest 2026-10-17, "jagged shores"): vanilla draws a
    straight coast as an ordered run (0x27 0x28 0x29 0x2A ...); the solver
    alternated two variants into a sawtooth. Each straight run of coast-band
    cells is re-chosen as a whole by Viterbi: the most vanilla-likely tile
    sequence (vanilla's neighbour counts) among tiles of each cell's own
    movement + landing kind that are legal with every cell around the run and
    at every vertex. Whatever it picks is legal, so no seam is added."""
    import math
    gk = [KI.get(t) for t in grid]
    frozen = set(fixed) | set(prefer or ())
    band = {i for i in range(NN) if i not in frozen and gk[i] is not None
            and any((cls[j] == "o") != (cls[i] == "o") for j in NB4[i])}
    logf = [math.log(f + 1.0) for f in FREQ]
    changed = 0

    def lp(q, d, a):
        return math.log(pair_p(q, d, a))
    for _s in range(sweeps):
        for horizontal in (True, False):
            df, db = (0, 1) if horizontal else (2, 3)
            for run in _chains(band, horizontal):
                n = len(run)
                inrun = set(run)
                cands, unary = [], []
                for k, c in enumerate(run):
                    t0 = gk[c]
                    m = restrict(cls, c, KIND_MASK.get(_kind(ALPHA[t0]), 0) | (1 << t0), open_water)
                    nb = NB8[c]
                    cs, us = [], []
                    for q in _bits(m):
                        ok = True
                        sc = 0.0
                        for d in range(8):
                            j = nb[d]
                            if j in inrun and (d == df or d == db):
                                continue
                            a = gk[j]
                            if a is None:
                                continue
                            if not COMPAT[d][q] >> a & 1:
                                ok = False
                                break
                            if d < 4:
                                sc += lp(q, d, a)
                        if not ok:
                            continue
                        # vertices shared with no run neighbour: check now
                        for v, pos in CV[c]:
                            vc = VC[v]
                            if any(u != c and u in inrun for u in vc):
                                continue
                            qd = [gk[u] for u in vc]
                            qd[pos] = q
                            if None not in qd and not _quad_ok(*qd):
                                ok = False
                                break
                        if ok:
                            cs.append(q)
                            us.append(sc + (logf[q] if k == 0 else 0.0))
                    if not cs:
                        break
                    cands.append(cs)
                    unary.append(us)
                if len(cands) < n:
                    continue
                # Viterbi; pair term = run-neighbour compat + the 2 shared vertices
                best = [list(unary[0])]
                back = [[-1] * len(cands[0])]
                for k in range(1, n):
                    c0, c1 = run[k - 1], run[k]
                    shared = [(v, pos) for v, pos in CV[c1] if c0 in VC[v]]
                    row, brow = [], []
                    for qi, q in enumerate(cands[k]):
                        bs, bp = None, -1
                        for pi, pq in enumerate(cands[k - 1]):
                            if best[k - 1][pi] is None or not COMPAT[df][pq] >> q & 1:
                                continue
                            ok = True
                            for v, pos in shared:
                                vc = VC[v]
                                qd = [gk[u] for u in vc]
                                qd[pos] = q
                                qd[vc.index(c0)] = pq
                                if None not in qd and not _quad_ok(*qd):
                                    ok = False
                                    break
                            if not ok:
                                continue
                            sc = best[k - 1][pi] + lp(pq, df, q)
                            if bs is None or sc > bs:
                                bs, bp = sc, pi
                        row.append(None if bs is None else bs + unary[k][qi])
                        brow.append(bp)
                    best.append(row)
                    back.append(brow)
                last = [(v, i) for i, v in enumerate(best[-1]) if v is not None]
                if not last:
                    continue
                _v, qi = max(last)
                seq = [0] * n
                for k in range(n - 1, -1, -1):
                    seq[k] = cands[k][qi]
                    qi = back[k][qi]
                for c, q in zip(run, seq):
                    if gk[c] != q:
                        gk[c] = q
                        changed += 1
    out = [ALPHA[k] if k is not None else grid[i] for i, k in enumerate(gk)]
    return out, changed


def bad_cells(gk):
    """Every cell in an illegal pair or quad (bad_spots names one per quad)."""
    out = set()
    for i in bad_spots(gk):
        out.add(i)
        e, s, se = NB8[i][0], NB8[i][2], NB8[i][4]
        a, b, c, d = gk[i], gk[e], gk[s], gk[se]
        if None not in (a, b, c, d) and not _quad_ok(a, b, c, d):
            out.update((e, s, se))
    return out


_KMASK = {"sea": None, "river": None, "block": None, "ground": None}


def _kmask(kind):
    if _KMASK[kind] is None:
        _KMASK[kind] = _FOOT if kind == "ground" else KIND_MASK[kind]
    return _KMASK[kind]


def repair(grid, cls, fixed, rng, rounds=4, budget=600, prefer=None,
           sizes=((2, 600), (3, 600), (4, 600), (5, 1500)), tiers=("class", "move", "foot"),
           reshaper=None, open_water=(), noland=()):
    """v0 awfc.repair, pure Python: re-solve windows (2x2 .. 5x5) around
    every bad spot -- CLASS-locked first, then MOVEMENT-locked (a forest
    tile may become grass, never water or rock), then FOOT (forest <->
    grass), then -- only with a reshaper (reshape.py) -- SHAPE: a cell may
    change movement where that is topologically inert, the class map
    following the tile. Candidates are prefiltered
    by bitmask against the cells around the window, except still-broken
    cells there (v0's exempt set: their own window fixes them later, else
    a cluster of seams can never be untangled). A spot that failed is
    retried in a later round only if something within 3 cells changed
    since (2026-10-17: ~3/4 of repair time went to re-failing 5x5 windows).
    noland: cells that must never take an airship-landable tile (a gate's
    unlandable far side, gates.unlandable). Returns (grid, stats)."""
    gk = [KI.get(t) for t in grid]
    frozen = set(fixed)
    pref = {i: KI[t] for i, t in (prefer or {}).items() if t in KI}
    if reshaper is not None and "shape" not in tiers:
        tiers = tuple(tiers) + ("shape",)
    from . import reshape as RS
    stats = dict(before=len(bad_spots(gk)), flips=0)
    stamp = [0] * NN                       # repair step at which a cell last changed
    step = 1
    failed = {}                            # spot -> step of its last failed attempt
    for _rnd in range(rounds):
        spots = sorted(bad_spots(gk) - frozen)
        if not spots:
            break
        broken = bad_cells(gk) - frozen
        rng.shuffle(spots)
        done = set()
        for s0 in spots:
            if s0 in done:
                continue
            x0, y0 = G.xy(s0)
            f = failed.get(s0)
            if f is not None and max(stamp[G.idx(x0 + a, y0 + b)]
                                     for b in range(-3, 4) for a in range(-3, 4)) <= f:
                continue
            fixed_ok = False
            for tier in tiers:
                for w, bud in (sizes[:2] if tier == "shape" else sizes):
                    win = [G.idx(x0 + a - (w - 1) // 2, y0 + b - (w - 1) // 2)
                           for b in range(w) for a in range(w)]
                    cells = [c for c in win if c not in frozen and gk[c] is not None]
                    if not cells:
                        continue
                    inwin = set(cells)
                    ex = {j for c in cells for j in NB8[c] if j not in inwin and j in broken}
                    exv = {j: gk[j] for j in ex}
                    for j in ex:
                        gk[j] = None
                    saved = [gk[c] for c in cells]
                    masks = []
                    home = [] if tier == "shape" else None
                    for c, t0 in zip(cells, saved):
                        k0 = _kind(ALPHA[t0])
                        if tier == "shape":
                            hk = RS.kind_of_tile(k0)
                            m = _kmask(hk) if k0 != "dock" else 1 << t0
                            home.append(m | (1 << t0))
                            if k0 != "dock":
                                for nk in ("sea", "river", "ground", "block"):
                                    if nk != hk and reshaper.ok(c, nk):
                                        m |= _kmask(nk)
                        elif tier == "class":
                            m = CLS_MASK.get(TCLS_K[t0], 0) & _free
                        elif tier == "foot" and k0 in ("land", "walk"):
                            m = _FOOT                  # forest <-> grass: airship landing moves
                        else:
                            m = KIND_MASK.get(k0, 0)
                        m = (m or (1 << t0)) | (1 << t0)
                        if c in noland:
                            m &= ~KIND_MASK["land"] | (1 << t0)
                        pk = pref.get(c)
                        if pk is not None and KIND_MASK.get(_kind(ALPHA[t0]), 0) >> pk & 1:
                            m |= 1 << pk
                        m = restrict(cls, c, m, open_water)
                        if home is not None:
                            home[-1] &= m | (1 << t0)
                        nb = NB8[c]
                        for d in range(8):
                            j = nb[d]
                            if j not in inwin and gk[j] is not None:
                                m &= COMPAT[_OPP8[d]][gk[j]]
                        masks.append(m)
                    ok = all(masks)
                    if ok:
                        for c in cells:
                            gk[c] = None
                        ok = _solve_window(gk, cells, masks, rng, bud, pref=pref, home=home)
                        if ok and tier == "shape":
                            ch = []
                            for c, t0 in zip(cells, saved):
                                k1 = RS.kind_of_tile(_kind(ALPHA[gk[c]]))
                                if k1 != RS.kind_of_tile(_kind(ALPHA[t0])):
                                    ch.append((c, ALPHA[gk[c]], k1))
                            ok = reshaper.apply(ch)
                            if ok:
                                stats["flips"] += len(ch)
                        if not ok:
                            for c, t in zip(cells, saved):
                                gk[c] = t
                    for j, t in exv.items():
                        gk[j] = t
                    if ok:
                        done.update(win)
                        broken.difference_update(cells)
                        step += 1
                        for c, t in zip(cells, saved):
                            if gk[c] != t:
                                stamp[c] = step
                        fixed_ok = True
                        break
                if fixed_ok:
                    break
            if not fixed_ok:
                failed[s0] = step
    stats["after"] = len(bad_spots(gk))
    out = [ALPHA[k] if k is not None else grid[i] for i, k in enumerate(gk)]
    return out, stats
