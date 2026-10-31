"""Backward shortcuts (user 2026-10-24): quality of life, never logic.

A later vehicle opens a quicker way between places the party could already
reach before it: walk a long way to a site, then the canoe, the ship or the
airship gives a quick way back (or on to another far site). The user aims
them at the ACTUAL item spots: the apworld runs this pass after fill, once it
knows where this slot's Canoe and Ship landed (aims, below), then the tiles
are solved again (build.add_shortcuts). Without aims (tools) every pair of
places counts the same.

  river   a canoe river from near one place to near another, 1-2 wide, cut
          through rock (vanilla: 600 of its 1122 river cells touch a
          mountain) or open ground where the cell cuts nothing off; short
          where it can be (every cell costs extra in the search)
  mouth   a river from the ship's (or the canal's) sea into the land: the
          ship moors there and the canoe goes in, "the same purpose as docks"
          (user 2026-10-24) -- counted as a river, not a dock
  dock    a dock beside an earlier place, cutting a pass through a coastal
          mountain wall when it must (user: "a longer pass is okay too")
  landing airship-landable ground beside every earlier site (a forest block
          near the door turns to grass)

PURE (the user's hard rule): a shortcut never changes what any set of items
reaches. Candidates are chosen on reach SIGNATURES (per graph node: every
token state that reaches it), so they only join places every holder of the
vehicle could already reach; the world is contracted again after each one
and a changed signature rolls it back. At the end the apworld's own logic
export (site and land rules, and the water rules the seed's Bestiary reads:
see logic_view) must equal the one before the pass.
Counts (user): 2-8 rivers + docks per world, at most 3 docks, best effort,
"they don't need to be huge"; landings are not counted.
"""
import heapq
import os

from .. import randworld_territories as RWT
from .. import worldgraph as WG
from . import decorate as DC
from . import gates as GT
from . import grid as G
from . import paint as PT
from . import progression as PR
from . import tables as T
from . import verify as VF
from .worldmap import RIVER, WALK, WALK_CLS

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8
INF = float("inf")

# ON (user 2026-10-24: playtesting it in real AP seeds); the environment
# switch FF1PSP_RANDWORLD_SHORTCUTS=0 turns it off
ENABLED = os.environ.get("FF1PSP_RANDWORLD_SHORTCUTS", "1") != "0"
COUNT = (2, 8)            # rivers + docks per world (user 2026-10-24)
MAX_DOCKS = 3
MIN_SAVE = 30             # tiles saved ("they don't need to be huge", user 2026-10-24)
SAVE_CAP = 150            # beyond this a saving scores no higher
NO_WAY_SAVE = 60          # worth of a shortcut to a place with no earlier way of its kind
START_R = 25              # a shortcut starts within this travel of its place
RIVER_MIN = 5             # a shorter cut reads as a puddle in a ridge (2026-10-24 gallery)
MOUTH_MIN = 4             # a mouth shorter than this is a notch: a dock serves there
RIVER_MAX = 40            # river cells
RIVER_COST = 1.5          # per river cell in the search: travel 1 + "generally short"
OPEN, THIN = 1.5, 3.0     # extra: river over open ground / one leaving a 1-thick rock wall
THIN_ROUNDS = 4           # widening rounds against thin rock beside a cut
THIN_GROW = 40            # cells a cut may grow by
PASS_MAX = 14             # rock cells a dock's pass may cut
PASS_COST = 1.5
DOCK_SPACING = 20         # from every other dock
DOCK_POOL = 10            # coast cells nearest the pass a dock piece may sit on
LAND_NEAR = 5             # landable ground within this walk of an earlier site's door
GROUND_RULES_R = 2        # the decoration grammar re-runs this close to a changed cell
WIDE_P = 0.6              # 2-wide rivers (vanilla mixes 1- and 2-wide runs)
MOUTH_NARROW = 3          # a mouth's last cells stay 1 wide (all 16 vanilla mouths are)
SPACE = 4                 # cells between two shortcut rivers
WATER_SLACK = 3           # river cells the tile repair may still add (a 7-cell river
                          # in an empty band became 9 tiles: the Bestiary's rr rule moved)
PER_SITE = 2              # shortcuts at one place
AIM_WEIGHT = 4.0          # back from the vehicle's own item spot
EARLY_WEIGHT = 2.0        # between places reached before the vehicle
SPREAD = 15               # tiles between two shortcuts' centres (no clusters)
STATS = {}                # funnel counters (tools read them)


def _count(k, n=1):
    STATS[k] = STATS.get(k, 0) + n


# ------------------------------------------------------------- purity ---
def signatures(wg):
    """Per graph node, an int whose bit k is set when token state k reaches
    it: every mask, read with the airship as an event (the apworld's rules)
    and with air_gate (verify's ladder)."""
    M = 1 << len(wg.tokens)
    sig = [0] * wg.n_nodes
    for v, ag in enumerate((False, True)):
        for m in range(M):
            r = wg.reach(m, air_gate=ag)
            bit = 1 << (v * M + m)
            for n in range(wg.n_nodes):
                if r[n]:
                    sig[n] |= bit
    return sig


def states_with(wg, token):
    """Signature bits of the states whose mask holds `token` (0 when the
    world has no such token)."""
    M = 1 << len(wg.tokens)
    b = wg.bit.get(token)
    if b is None:
        return 0
    out = 0
    for m in range(M):
        if m & b:
            out |= (1 << m) | (1 << (M + m))
    return out


def changes(wa, sa, wb, sb):
    """Nodes whose reach differs between two graphs of one world, matched
    through the cells they share (land, river, sea, gate pieces), plus the
    airship. Empty = the edit between them was pure."""
    if wa.tokens != wb.tokens:
        return [("tokens", wa.tokens, wb.tokens)]
    out = []
    if sa[wa.sky] != sb[wb.sky]:
        out.append(("sky",))
    pairs = set()
    for ca, cb, ba, bb in ((wa.comp, wb.comp, 0, 0), (wa.rcomp, wb.rcomp, wa.r_base, wb.r_base),
                           (wa.scomp, wb.scomp, wa.s_base, wb.s_base)):
        for x, y in zip(ca, cb):
            if x >= 0 and y >= 0:
                pairs.add((x + ba, y + bb))
    for i, p in wa.piece.items():
        q = wb.piece.get(i)
        if q is not None:
            pairs.add((p, q))
    out += [("node", a, b) for a, b in sorted(pairs) if sa[a] != sb[b]]
    return out


def logic_export(wm, grid=None, water=None):
    """What the apworld's logic reads from this world: the site, water and
    land rules of randworld_territories.export (placement fields left out;
    land keyed by (kind, hub) so only RULES compare, not tile counts)."""
    terr = RWT.Territories(grid if grid is not None else wm.tile_grid(), T.ATT,
                           RWT.sites_from_plan(wm.placed), gates=wm.gates,
                           merge=getattr(wm, "hub_merge", None))
    return logic_view(terr.export(), water)


def logic_view(ex, water=None):
    """The rule parts of a Territories export (see logic_export). water: the
    water regions the seed's logic reads (logic.randworld_water_read; None =
    every region). Only the Bestiary's hunts read them, so without
    bestiarysanity none does, and a region nothing reads may move (user
    2026-10-26)."""
    land = {(k, hub): rule for k, hub, _n, rule in ex["land"]}
    wet = ex["water"] if water is None else {r: v for r, v in ex["water"].items() if r in water}
    return ex["sites"], wet, land


def _river_band(i):
    x, y = G.xy(i)
    return "rr" if RWT.zone_of(x, y) in RWT.RR_NORTH_ZONES else "river_south"


def _bits(s):
    while s:
        low = s & -s
        yield low.bit_length() - 1
        s ^= low


def _water_counts(wg, sig):
    """{water region: {state bit: river cells reached}} for 'river' and its
    two compass bands -- the counts Territories' water rules (the Bestiary's
    river hunts) are cut from at MIN_TILES."""
    per = {}
    for i in range(NN):
        r = wg.rcomp[i]
        if r >= 0:
            d = per.setdefault(wg.r_base + r, {})
            b = _river_band(i)
            d[b] = d.get(b, 0) + 1
    out = {"river": {}, "rr": {}, "river_south": {}}
    for n, d in per.items():
        tot = sum(d.values())
        for k in _bits(sig[n]):
            out["river"][k] = out["river"].get(k, 0) + tot
            for b, c in d.items():
                out[b][k] = out[b].get(k, 0) + c
    return out


class _State:
    """The current graph, its signatures, site rules and river counts."""

    def __init__(self, wm, water=None):
        self.cap = (getattr(wm, "dice", None) or {}).get("start_cap")
        self.read = water                      # guarded water regions (None = all)
        self.set(VF.graph_of(wm))

    def set(self, wg, sig=None):
        self.wg = wg
        self.sig = sig if sig is not None else signatures(wg)
        self.rules = wg.site_rules()
        self.water = _water_counts(wg, self.sig)
        self.CB = states_with(wg, WG.CANOE)

    def water_ok(self, cells, key):
        """Would a new river over `cells`, reached in the states of `key`,
        lift some state's count of a water region the seed's logic reads
        past MIN_TILES?"""
        add = {"river": len(cells), "rr": 0, "river_south": 0}
        for i in cells:
            add[_river_band(i)] += 1
        for reg, k in add.items():
            if not k or (self.read is not None and reg not in self.read):
                continue
            cnt = self.water[reg]
            for s in _bits(key):
                c = cnt.get(s, 0)
                if c < RWT.MIN_TILES <= c + k + WATER_SLACK:
                    return False
        return True

    def pure_after(self, wm):
        """Contract wm again; keep the new graph when nothing moved (and the
        start stays inside its tile cap)."""
        wg = VF.graph_of(wm)
        sig = signatures(wg)
        if changes(self.wg, self.sig, wg, sig) or wg.site_rules() != self.rules:
            return False
        if self.cap:
            r = wg.reach(0)
            if sum(wg.comp_size[n] for n in range(wg.n_land) if r[n]) > self.cap:
                return False
        self.set(wg, sig)
        return True


def _snap(wm):
    return PT.snapshot(wm), dict(wm.prefer), list(wm.shortcuts)


def _restore(wm, snap):
    PT.restore(wm, snap[0])
    wm.prefer.clear()
    wm.prefer.update(snap[1])
    wm.shortcuts[:] = snap[2]


# -------------------------------------------------------------- masks ---
def _walk_mask(wm):
    m = bytearray(1 if wm.walk(i) else 0 for i in range(NN))
    for i in wm.ent_cells:
        m[i] = 0
    return m


def _travel_mask(wm):
    """Where the party moves with the canoe: ground and rivers (no sea)."""
    m = bytearray(1 if wm.move(i) in (WALK, RIVER) else 0 for i in range(NN))
    for i in wm.ent_cells:
        m[i] = 0
    return m


def _rock_depth(wm, cap=3):
    """Steps from each rock cell to the nearest non-rock cell (1 = beside
    ground or water), capped."""
    rock = bytearray(1 if wm.cls[i] == "M" and i not in wm.fixed else 0 for i in range(NN))
    seeds = [i for i in range(NN) if not rock[i] and any(rock[j] for j in NB4[i])]
    return G.bfs_dist(rock, seeds, cap=cap)


class _Dist:
    """Travel BFS per door, recomputed when the map changes."""

    def __init__(self, wm):
        self.refresh(wm)

    def refresh(self, wm):
        self.tm = _travel_mask(wm)
        self.wk = _walk_mask(wm)
        self._t, self._w = {}, {}

    def travel(self, d):
        if d not in self._t:
            self._t[d] = G.bfs_dist(self.tm, [d])
        return self._t[d]

    def walk(self, d):
        if d not in self._w:
            self._w[d] = G.bfs_dist(self.wk, [d])
        return self._w[d]


def _places(wg):
    """[(site key, door cell)] of the placed sites with a door on land (a
    river-entered site's door is its river cell: nothing to land beside)."""
    return [(s["key"], d) for s, d in zip(wg.sites, wg.doors)
            if d is not None and wg.comp[d] >= 0 and s["key"] not in PR.RIVER_SITES]


def _sea_node(wg, lab, sid):
    """The graph node of skeleton sea `sid` in THIS graph. Sea nodes are
    numbered after the land and river nodes (s_base = n_land + n_river), so
    every river a shortcut adds moves them: a candidate keeps the sea id and
    asks again when it is applied (a stored node id killed every mouth
    queued behind the first river, bundle 2026-10-26)."""
    return next((wg.s_base + wg.scomp[i] for i in range(NN)
                 if lab[i] == sid and wg.scomp[i] >= 0), None)


def _seas(wm, wg, g_sea):
    """[(skeleton sea id, graph sea node, token)] for the ship's sea and the
    canal's."""
    out = []
    for sid, tok in ((g_sea.get("ship"), WG.SHIP), (g_sea.get("canal"), WG.CANAL_OPENED)):
        if sid is None:
            continue
        node = _sea_node(wg, g_sea["lab"], sid)
        if node is not None:
            out.append((sid, node, tok))
    return out


# ---------------------------------------------------------------- aims ---
def aim_view(wg, aims):
    """{token: (item site key or None, sites reached before it)} from the
    apworld's aims {"Canoe": {"site": key, "before": [item names]}, ...}."""
    out = {}
    nodes = wg.site_nodes()
    for tok, a in (aims or {}).items():
        r = wg.reach(wg.mask_of(a.get("before") or ()))
        out[tok] = (a.get("site"), {k for k, ns in nodes.items() if any(r[n] for n in ns)})
    return out


def _weight(av, gate, p, q=None):
    """How much the user's aim favours a shortcut gated by `gate` between
    places p and q (q None: p and the sea)."""
    w = 1.0
    for tok in gate:
        a = av.get(tok)
        if not a:
            continue
        site, early = a
        other = q if site == p else p
        if site in (p, q) and (other is None or other in early):
            w = max(w, AIM_WEIGHT)               # back from the item spot
        elif p in early and (q is None or q in early):
            w = max(w, EARLY_WEIGHT)
    return w


# -------------------------------------------------------------- rivers ---
def _river_cells(wm, st, allowed, near, sea_node=None):
    """(ok, mouth): ok(cell) -- may this cell become shortcut river? Rock or
    open ground of a land area, nothing fixed, protected or an entrance
    beside it, no water within one cell (no new mouth, no joining a river),
    every walkable 4-neighbour on a landmass in `allowed`. mouth(cell) -- the
    same, but it touches sea_node (and no other water): a river's last cell."""
    wg = st.wg
    comp, caps, scomp, sb = wg.comp, wg.caps, wg.scomp, wg.s_base
    own, cls, fixed, prot = wm.ar.owner, wm.cls, wm.fixed, wm.protect
    ents, nowater = wm.ent_cells, wm.no_water
    memo = {}

    def base(i):
        c = cls[i]
        if (own[i] < 0 or prot[i] or i in fixed or nowater[i] or i in ents or i in near
                or not (c == "M" or c in WALK_CLS)
                or (caps[i] & WG.WALK and comp[i] not in allowed)):
            return False
        for j in NB4[i]:
            if j in ents or (caps[j] & WG.WALK and comp[j] not in allowed):
                return False
        return True

    def ok(i):
        v = memo.get(i)
        if v is None:
            v = base(i) and not any(cls[j] in ("o", "r") or j in fixed for j in NB8[i])
            memo[i] = v
        return v

    def mouth(i):
        if sea_node is None or not base(i):
            return False
        touch = False
        for j in NB8[i]:
            if cls[j] == "r" or j in fixed:
                return False
            if cls[j] == "o":
                if scomp[j] < 0 or sb + scomp[j] != sea_node:
                    return False
                touch = touch or j in NB4[i]
        return touch

    def grow(i, own):
        """ok(), but beside this river's own cells: widening it."""
        if not base(i):
            return False
        for j in NB8[i]:
            if j in fixed or (cls[j] == "r" and j not in own):
                return False
            if cls[j] == "o" and (sea_node is None or scomp[j] < 0 or sb + scomp[j] != sea_node):
                return False
        return True
    ok.grow = grow
    return ok, mouth


def _flood(wm, depth, ok, starts, mouth=None):
    """Cheapest river cells from beside `starts` ({walk cell: travel cost}),
    at most RIVER_MAX long. A mouth cell is reached but not expanded (the
    river ends at the sea). Returns (g {cell: cost}, par {cell: previous
    river cell, or the bank cell for a first cell}, ln {cell: river cells so
    far})."""
    g, par, ln = {}, {}, {}
    heap = []

    def cost(c):
        x = RIVER_COST
        if wm.cls[c] in WALK_CLS:
            x += OPEN
        elif depth[c] == 2:
            x += THIN
        return x
    for w, b in starts.items():
        for c in NB4[w]:
            if c in starts or not (ok(c) or (mouth and mouth(c))):
                continue
            x = b + cost(c)
            if x < g.get(c, INF):
                g[c], par[c], ln[c] = x, w, 1
                heapq.heappush(heap, (x, c))
    while heap:
        x, c = heapq.heappop(heap)
        if x > g[c] or ln[c] >= RIVER_MAX or not ok(c):
            continue
        for v in NB4[c]:
            if v in starts or not (ok(v) or (mouth and mouth(v))):
                continue
            y = x + cost(v)
            if y < g.get(v, INF):
                g[v], par[v], ln[v] = y, c, ln[c] + 1
                heapq.heappush(heap, (y, v))
    return g, par, ln


def _chain(g, par, c):
    """River cells from the bank to c, in order."""
    out = []
    while c in g:
        out.append(c)
        c = par[c]
    out.reverse()
    return out


def _start_zone(D, door, comp, allowed):
    """{walk cell: travel from the door} within START_R (rivers ridden, but
    a shortcut starts from ground)."""
    out = {door: 0}
    front = [door]
    for s in range(1, START_R + 1):
        nxt = []
        for i in front:
            for j in NB4[i]:
                if D.tm[j] and j not in out:
                    out[j] = s
                    nxt.append(j)
        front = nxt
    return {i: s for i, s in out.items() if D.wk[i] and comp[i] in allowed}


def _land_rivers(wm, st, D, depth, near, places, av):
    """River candidates between two places on the same landmass class."""
    wg, sig, CB = st.wg, st.sig, st.CB
    AIR = states_with(wg, WG.AIRSHIP)
    out = []
    for ka, da in places:
        key = sig[wg.comp[da]] & CB
        if not key & ~AIR:
            continue                           # airship-only ground: the airship is the shortcut
        targets = [(kb, db) for kb, db in places
                   if kb != ka and sig[wg.comp[db]] & CB == key]
        if not targets:
            continue
        allowed = {n for n in range(wg.n_land) if sig[n] & CB == key}
        ok, _m = _river_cells(wm, st, allowed, near)
        dP = D.travel(da)
        starts = _start_zone(D, da, wg.comp, allowed)
        g, par, ln = _flood(wm, depth, ok, starts)
        land = {}
        for c, x in g.items():
            if ln[c] < RIVER_MIN:
                continue
            for v in NB4[c]:
                if D.wk[v] and wg.comp[v] in allowed and v not in starts:
                    if (dP[v] < 0 or x + 1 + MIN_SAVE <= dP[v]) and x + 1 < land.get(v, (INF,))[0]:
                        land[v] = (x + 1, c)
        if not land:
            continue
        for kb, db in targets:
            dQ = D.travel(db)
            best = None
            for v, (x, c) in land.items():
                if dQ[v] >= 0 and (best is None or x + dQ[v] < best[0]):
                    best = (x + dQ[v], c)
            if best is None:
                continue
            old = dP[db] if dP[db] >= 0 else INF
            save = old - best[0]
            if save < MIN_SAVE:
                continue
            cells = _chain(g, par, best[1])
            out.append(dict(kind="river", token=WG.CANOE, sites=[ka, kb], key=key, cells=cells,
                            doors=(da, db), new=best[0], save=save,
                            w=_weight(av, (WG.CANOE,), ka, kb)))
    return out


def _moorings(wg, node, D):
    """Cells a ship (with the canoe) lands from sea node: docks, and river
    cells at a mouth."""
    sb = wg.s_base
    out = []
    for i in range(NN):
        if wg.caps[i] & (WG.DOCK | WG.RIVER) and any(
                wg.scomp[j] >= 0 and sb + wg.scomp[j] == node for j in NB4[i]):
            out.append(i)
    return out


def _mouth_rivers(wm, st, D, depth, near, places, seas, av):
    """River candidates from a sea into the land near a place."""
    wg, sig, CB = st.wg, st.sig, st.CB
    out = []
    for sid, node, tok in seas:
        moor = _moorings(wg, node, D)
        for k, d in places:
            L = wg.comp[d]
            key = sig[L] & CB
            if not sig[node] & CB or (sig[node] & CB) & ~key:
                continue                       # the sea would be a new way in
            allowed = {n for n in range(wg.n_land) if sig[n] & CB == key}
            ok, mouth = _river_cells(wm, st, allowed, near, node)
            dX = D.travel(d)
            starts = _start_zone(D, d, wg.comp, allowed)
            g, par, ln = _flood(wm, depth, ok, starts, mouth)
            ends = [(x, c) for c, x in g.items() if ln[c] >= MOUTH_MIN and mouth(c)]
            if not ends:
                continue
            new, c = min(ends)
            old = min((dX[m] for m in moor if dX[m] >= 0), default=INF)
            save = old - new
            if save < MIN_SAVE:
                continue
            out.append(dict(kind="mouth", token=tok, sea=sid, node=node, sites=[k], key=key,
                            cells=_chain(g, par, c), doors=(d,), new=new, save=save,
                            w=_weight(av, (WG.CANOE, tok), k)))
    return out


def _pier_fits(wm, sid, c, memo):
    """Does some vanilla dock piece fit with its anchor on coast cell c? The
    pieces only face south or west, so a straight east- or north-facing shore
    takes none: a search that stopped at the nearest coast there had nothing
    to place (Cornelia's and Elfheim's bay, bundle 2026-10-26)."""
    v = memo.get((sid, c))
    if v is None:
        cx, cy = G.xy(c)
        area = wm.ar.owner[c]
        v = any(PT.off_edge([G.idx(cx - d["at"][0] + i, cy - d["at"][1] + j) for i, j, _t in d["cells"]])
                and PT.dock_fits(wm, area, wm.sea_lab, sid, c, d) is not None for d in T.DOCKS)
        memo[(sid, c)] = v
    return v


def _dock_cands(wm, st, D, places, seas, av):
    """Dock candidates: walk (and cut a pass through rock) from a place to
    the nearest coast of a sea it may moor on where a dock piece fits, no
    farther than START_R."""
    wg, sig = st.wg, st.sig
    own, cls, fixed, prot, ents = wm.ar.owner, wm.cls, wm.fixed, wm.protect, wm.ent_cells
    sb = wg.s_base
    fits = {}
    out = []
    for sid, node, tok in seas:
        lab = wm.sea_lab
        moor = [i for i in range(NN) if wg.caps[i] & WG.DOCK
                and any(wg.scomp[j] >= 0 and sb + wg.scomp[j] == node for j in NB4[i])]
        for k, d in places:
            L = wg.comp[d]
            if not sig[node] or sig[node] & ~sig[L]:
                continue                       # the dock would be a new way in
            same = {n for n in range(wg.n_land) if sig[n] == sig[L]}

            def rock_ok(i):
                if own[i] < 0 or prot[i] or i in fixed or i in ents or cls[i] not in ("M", "c"):
                    return False
                if any(j in fixed for j in NB8[i]):
                    return False
                for j in NB4[i]:
                    if j in ents or cls[j] == "r" or (wg.caps[j] & WG.WALK and wg.comp[j] not in same):
                        return False
                return True

            def coast(i):
                return any(lab[j] == sid and cls[j] == "o" for j in NB4[i])
            dW = D.walk(d)
            old = min((dW[m] for m in moor if dW[m] >= 0), default=INF)
            # Dijkstra: walk cells cost 1, rock (a pass) PASS_COST, <= PASS_MAX rock in a row
            g = {d: 0.0}
            par = {d: None}
            run = {d: 0}
            heap = [(0.0, d)]
            goal = None
            while heap:
                x, c = heapq.heappop(heap)
                if x > g[c]:
                    continue
                if x + MIN_SAVE > old or x > START_R:
                    break
                if coast(c) and own[c] >= 0 and _pier_fits(wm, sid, c, fits):
                    goal = (x, c)
                    break
                for v in NB4[c]:
                    if D.wk[v] and wg.comp[v] in same:
                        y, r = x + 1, 0
                    elif rock_ok(v) and run[c] < PASS_MAX:
                        y, r = x + PASS_COST, run[c] + 1
                    else:
                        continue
                    if y < g.get(v, INF):
                        g[v], par[v], run[v] = y, c, r
                        heapq.heappush(heap, (y, v))
            if goal is None:
                continue
            x, c = goal
            save = old - x
            if save < MIN_SAVE:
                continue
            path = []
            while c is not None:
                path.append(c)
                c = par[c]
            path.reverse()
            out.append(dict(kind="dock", token=tok, sea=sid, node=node, sites=[k],
                            coast=path[-1], cut=[i for i in path if not D.wk[i]],
                            doors=(d,), new=x, save=save, w=_weight(av, (tok,), k)))
    return out


def _score(c):
    n = len(c.get("cells") or ()) or (len(c.get("cut") or ()) + 5)
    save = NO_WAY_SAVE if c["save"] == INF else min(c["save"], SAVE_CAP)
    return c["w"] * save / (n + 10)


def _centre(c):
    cells = list(c.get("cells") or ()) + list(c.get("cut") or ()) + ([c["coast"]] if c.get("coast") else [])
    xs = [G.xy(i) for i in cells]
    return (sum(p[0] for p in xs) / len(xs), sum(p[1] for p in xs) / len(xs))


def _dedupe(cands):
    """One candidate per drawn shape (many site pairs share one river)."""
    best = {}
    for c in cands:
        k = (c["kind"], frozenset(c.get("cells") or ()), frozenset(c.get("cut") or ()), c.get("coast"))
        if k not in best or _score(c) > _score(best[k]):
            best[k] = c
    return list(best.values())


def _still_saves(wm, c, st, D):
    """Shortcuts accepted since the candidate was found may already have
    made its trip short: measure the old way again."""
    if c["kind"] == "river":
        da, db = c["doors"]
        t = D.travel(da)[db]
        old = t if t >= 0 else INF
    elif c["kind"] == "mouth":
        dX = D.travel(c["doors"][0])
        node = _sea_node(st.wg, wm.sea_lab, c["sea"])
        old = min((dX[m] for m in _moorings(st.wg, node, D) if dX[m] >= 0), default=INF)
    else:
        wg, sb = st.wg, st.wg.s_base
        node = _sea_node(wg, wm.sea_lab, c["sea"])
        dW = D.walk(c["doors"][0])
        old = min((dW[i] for i in range(NN) if dW[i] >= 0 and wg.caps[i] & WG.DOCK and any(
            wg.scomp[j] >= 0 and sb + wg.scomp[j] == node for j in NB4[i])), default=INF)
    return old - c["new"] >= MIN_SAVE


# --------------------------------------------------------------- apply ---
def _banks(wm, cells):
    """Ground beside a new river reads as bank (decorate.ground_classes)."""
    for i in cells:
        for j in NB4[i]:
            if wm.cls[j] in WALK_CLS and j not in wm.fixed and not wm.protect[j] \
                    and j not in wm.prefer:
                wm.cls[j] = "b"


def _widen(wm, cells, ok, mouth_end):
    """A second column along the river (right-hand side of travel), kept 1
    wide for its last MOUTH_NARROW cells when it ends at the sea."""
    out = list(cells)
    have = set(cells)
    stop = len(cells) - MOUTH_NARROW if mouth_end else len(cells)
    for k in range(min(stop, len(cells) - 1)):
        a, b = G.xy(cells[k]), G.xy(cells[k + 1])
        dx, dy = (b[0] - a[0] + 1) % N - 1, (b[1] - a[1] + 1) % N - 1
        side = G.idx(a[0] - dy, a[1] + dx)
        if side not in have and ok(side):
            have.add(side)
            out.append(side)
    return out


def _apply_river(wm, st, c, rng, near):
    wg = st.wg
    allowed = {n for n in range(wg.n_land) if st.sig[n] & st.CB == c["key"]}
    node = _sea_node(wg, wm.sea_lab, c["sea"]) if c["kind"] == "mouth" else None
    ok, mouth = _river_cells(wm, st, allowed, near, node)
    cells = c["cells"]
    end_mouth = c["kind"] == "mouth"
    if not all(ok(i) for i in cells[:-1]) or not (mouth(cells[-1]) if end_mouth else ok(cells[-1])):
        _count("stale path")
        return None
    if rng.random() < WIDE_P:
        cells = _widen(wm, cells, ok, end_mouth)
    for i in cells:
        if wm.walk(i) and not PT.simple_to_block(wm, i):
            _count("would cut ground")
            return None
        wm.set(i, "r")
    cells = _fix_thin(wm, st, cells, lambda j, own: ok.grow(j, own), allowed)
    if cells is None:
        _count("would leave thin rock")
        return None
    if not st.water_ok(cells, c["key"]):
        _count("moves a water rule")
        return None
    _banks(wm, cells)
    return cells


def _thin_cells(wm, cells):
    """Rock cells beside `cells` that sit in no 2x2 rock block. Vanilla has
    no thinner mountain art (decorate.drawable_rock): left alone, the tiles
    reshape them into ground and a river reads as a pond."""
    out, seen = [], set()
    for i in cells:
        for j in NB8[i]:
            if j in seen or wm.cls[j] != "M":
                continue
            seen.add(j)
            x, y = G.xy(j)
            if not any(all(wm.cls[G.idx(x - ox + a, y - oy + b)] in ("M", "c")
                           for a in (0, 1) for b in (0, 1))
                       for ox in (0, 1) for oy in (0, 1)):
                out.append(j)
    return sorted(out)


def _fix_thin(wm, st, cells, grow, allowed, wet=True):
    """Make the cut bigger until no thin rock is left beside it (user
    2026-10-24: "let shortcuts be bigger if it means tiles look better"): a
    sliver becomes more river (wet) where a river cell may go, else bank
    ground where that joins only ground of one reach. Returns the cut's
    cells (grown), or None when a sliver cannot go."""
    wg, sig = st.wg, st.sig
    cut = list(cells)
    have = set(cut)
    for _round in range(THIN_ROUNDS):
        thin = _thin_cells(wm, cut)
        if not thin:
            return cut
        if len(cut) + len(thin) > len(cells) + THIN_GROW:
            return None
        for j in thin:
            if wm.cls[j] != "M":
                continue
            if wet and grow(j, have):
                wm.set(j, "r")
                cut.append(j)
                have.add(j)
                continue
            ns = {wg.comp[q] for q in NB4[j] if wg.caps[q] & WG.WALK and wm.walk(q)}
            if (ns and ns <= allowed and len({sig[n] for n in ns}) == 1
                    and not wm.protect[j] and j not in wm.fixed and j not in wm.ent_cells):
                wm.set(j, "b" if wet else "g")
                continue
            return None
    return cut if not _thin_cells(wm, cut) else None


def _apply_dock(wm, st, c, rng):
    for i in c["cut"]:
        wm.set(i, "g")
    if c["cut"]:
        wg, sig = st.wg, st.sig
        L = wg.comp[c["doors"][0]]
        same = {n for n in range(wg.n_land) if sig[n] == sig[L]}
        if _fix_thin(wm, st, c["cut"], None, same, wet=False) is None:
            _count("pass would leave thin rock")
            return None
    rec = PT.place_dock(wm, wm.ar.owner[c["coast"]], wm.sea_lab, c["sea"], rng, near=G.xy(c["coast"]),
                        limit=DOCK_POOL)
    if rec is None:
        _count("dock does not fit")
        return None
    if G.tdist(rec["ents"][0], G.xy(c["coast"])) > 6:
        _count("dock fit too far from its pass")
        return None
    others = [p["ents"][0] for p in wm.placed if p.get("kind") == "dock" and p is not rec]
    if any(G.tdist(rec["ents"][0], o) < DOCK_SPACING for o in others):
        _count("dock too close to another")
        return None
    rec["shortcut"] = True
    return rec


def add(wm, weights, g_sea, rng, aims=None, water=None):
    """Backward shortcuts into a FINISHED world (decorated class map; the
    caller solves the tiles again). aims: the apworld's vehicle spots (see
    aim_view). water: the water regions the seed's logic reads (see
    logic_view; None = all). Records them in wm.shortcuts /
    wm.report["shortcuts"]; leaves wm.report as verify(wm) makes it. Returns
    True when the map changed."""
    if not ENABLED:
        return False
    wm.shortcuts = list(getattr(wm, "shortcuts", ()))
    total = rng.randint(*COUNT)
    before = logic_export(wm, water=water)
    st = _State(wm, water)
    av = aim_view(st.wg, aims)
    start = _snap(wm)
    D = _Dist(wm)
    depth = _rock_depth(wm)
    places = _places(st.wg)
    seas = _seas(wm, st.wg, g_sea)
    cands = (_land_rivers(wm, st, D, depth, set(), places, av)
             + _mouth_rivers(wm, st, D, depth, set(), places, seas, av)
             + _dock_cands(wm, st, D, places, seas, av))
    cands = _dedupe(cands)
    _count("candidates", len(cands))
    for c in cands:
        c["score"] = _score(c) * rng.uniform(0.8, 1.25)
    cands.sort(key=lambda c: -c["score"])
    undo = []
    used = {}
    docks = rivers = 0
    mine = set()
    centres = []
    for c in cands:
        if docks + rivers >= total:
            break
        if c["kind"] == "dock" and docks >= MAX_DOCKS:
            continue
        if any(used.get(s, 0) >= PER_SITE for s in c["sites"]):
            continue
        if any(G.tdist(_centre(c), q) < SPREAD for q in centres):
            _count("too close to another shortcut")
            continue
        if undo and not _still_saves(wm, c, st, D):
            _count("saving gone")
            continue
        snap = _snap(wm)
        near = G.dilate(mine, SPACE) if mine else set()
        if c["kind"] == "dock":
            rec = _apply_dock(wm, st, c, rng)
            ok = rec is not None
        else:
            if c["kind"] == "river" and any(i in near for i in c["cells"]):
                continue
            cells = _apply_river(wm, st, c, rng, near)
            ok = cells is not None
        if not ok or not st.pure_after(wm):
            if ok:
                _count(f"{c['kind']} impure")
            _restore(wm, snap)
            continue
        _count(f"{c['kind']} made")
        undo.append(snap)
        centres.append(_centre(c))
        for s in c["sites"]:
            used[s] = used.get(s, 0) + 1
        if c["kind"] == "dock":
            docks += 1
            wm.shortcuts.append(dict(kind="dock", token=c["token"], sites=c["sites"], at=rec["ents"][0],
                                     cut=[G.xy(i) for i in c["cut"]], save=int(min(c["save"], 999))))
        else:
            rivers += 1
            mine.update(cells)
            wm.shortcuts.append(dict(kind=c["kind"], token=c["token"], sites=c["sites"],
                                     cells=[G.xy(i) for i in cells], save=int(min(c["save"], 999))))
        D.refresh(wm)
        depth = _rock_depth(wm)
    landings(wm, st)
    if not wm.shortcuts:
        _restore(wm, start)
        wm.report["shortcuts"] = dict(want=total, note="nothing fit")
        return False
    # decoration's grammar again where a pass or bank landed (a pass from the
    # desert to the coast: coasts are grass, desert is wrapped in forest).
    # ONLY there: on a finished world the grammar is not a fixed point (later
    # stages leave cells it would still flip), and a world-wide pass re-tiled
    # forest 50+ tiles from any shortcut (seed 21, 2026-10-28)
    cls0, fixed0 = start[0][0], start[0][1]
    near = G.dilate([i for i in range(NN) if wm.cls[i] != cls0[i]
                     or wm.fixed.get(i) != fixed0.get(i)], GROUND_RULES_R)
    pre = list(wm.cls)
    DC.ground_rules(wm)
    for i in range(NN):
        if wm.cls[i] != pre[i] and i not in near:
            wm.cls[i] = pre[i]
    GT.unlandable(wm)
    note = None
    # the apworld's own logic view (sites, water, land rules) must not move:
    # the signatures cover reach, this also covers land headings' tile counts
    dropped = 0
    while undo and logic_export(wm, water=water) != before:
        _restore(wm, undo.pop())
        dropped += 1
    if dropped:
        note = f"logic export moved: {dropped} shortcut(s) rolled back"
    VF.verify(wm, weights)
    if wm.report["problems"] and wm.shortcuts:
        _restore(wm, start)
        VF.verify(wm, weights)
        note = "verify failed after shortcuts: rolled back"
    wm.report["shortcuts"] = dict(
        want=total, note=note,
        **{k: sum(1 for s in wm.shortcuts if s["kind"] == k) for k in ("river", "mouth", "dock", "landing")})
    return bool(wm.shortcuts)


def _landable(wm, i):
    t = wm.fixed.get(i)
    if t is not None:
        return T.ATT[t] in WG.AIR_LAND
    return wm.cls[i] in "gdbw"


def landings(wm, st=None):
    """Every earlier site keeps airship-landable ground within LAND_NEAR
    steps of its door: the nearest forest 2x2 block there turns to grass.
    Only where the airship already reaches that landmass (pure), never on a
    rolled-unlandable cell (wm.noland)."""
    wg = st.wg if st is not None else VF.graph_of(wm)
    sig = st.sig if st is not None else signatures(wg)
    sky = sig[wg.sky]
    walk = _walk_mask(wm)
    snap = _snap(wm)
    made = []
    for key, d in _places(wg):
        if sky & ~sig[wg.comp[d]]:
            continue
        zone = {d: 0}
        front = [d]
        for s in range(1, LAND_NEAR + 1):
            nxt = []
            for i in front:
                for j in NB4[i]:
                    if walk[j] and j not in zone:
                        zone[j] = s
                        nxt.append(j)
            front = nxt
        if any(_landable(wm, i) for i in zone):
            continue
        for z in sorted(zone, key=lambda c: (zone[c], c)):
            x, y = G.xy(z)
            blk = None
            for ox, oy in ((0, 0), (-1, 0), (0, -1), (-1, -1)):
                q = [G.idx(x + ox + a, y + oy + b) for a in (0, 1) for b in (0, 1)]
                if all(wm.cls[c] == "f" and c not in wm.fixed and not wm.protect[c]
                       and c not in wm.noland for c in q):
                    blk = q
                    break
            if blk:
                for c in blk:
                    wm.cls[c] = "g"
                made.append(dict(kind="landing", token=WG.AIRSHIP, sites=[key],
                                 cells=[G.xy(c) for c in blk]))
                break
    if not made:
        return
    wg2 = VF.graph_of(wm)
    if changes(wg, sig, wg2, signatures(wg2)):
        _restore(wm, snap)
        return
    wm.shortcuts.extend(made)
