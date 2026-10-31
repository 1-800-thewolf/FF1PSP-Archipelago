"""One reachability graph per overworld (RANDWORLD_LOGIC_DESIGN.md, 2026-10-16).

The 255x255 grid is contracted ONCE into components, and every access
question becomes a breadth-first search over a few hundred nodes instead of a
grid flood per item set. That is what makes it affordable to enumerate every
combination of the access tokens a random world carries (vehicles, the canal,
the world-changing gate items): 2^9 searches take milliseconds, where 2^9
grid floods took ~20 s.

Nodes
  L  foot landmass   (walkable cells, 4-connected; entrance cells blocked)
  R  river body      (canoe water)
  S  sea body        (ship water)
  G  gate piece      the cells one gate OPENS (bridge deck, boulder, seal,
                     canal channel), 4-connected per gate
  SKY                the airship in the air

Edges (and the token they need)
  L <-> R            Canoe (the canoe walks rivers)
  S  -> R            Canoe (ship -> canoe at a river mouth)
  S  -> L            nothing (moor at a dock cell beside that sea)
  L  -> S            Ship, ONLY from the land beside SHIP_SPAWN (the ship waits
                     at its fixed pier and never leaves its body of water)
  start -> SKY       Airship; SKY -> every L with airship-landable ground. The
                     AIRSHIP token is an EVENT (logic: the desert spot +
                     Levistone), so the trip to the desert lives in that event's
                     rule, not here. reach(..., air_gate=True) re-imposes it
                     (world.py's semantics) for callers that want it.
  Titan mouths       Star Ruby, every approach landmass of one mouth to every
                     approach landmass of the other
  G <-> neighbours   the gate's token (the canal's is "Canal Opened")

Gates come from the plan JSON (gates.to_json): cells (x, y, open, closed).
The BASE state is every gate closed. A capability a gate LOSES when it opens
(the Lufenian deck turns sea into walk-only bridge) is removed in every state,
so the model can be pessimistic but never optimistic: Archipelago logic
assumes a received item never closes a route. Such cells are listed in
`lost` for the generator to look at.

Pure Python, no numpy, no Archipelago imports, like randworld_territories.
"""
from collections import deque

N = 255
NN = N * N

# token names (logic.py's; test_worldgraph pins the parity)
SHIP, CANOE, AIRSHIP, STAR_RUBY = "Ship", "Canoe", "Airship", "Star Ruby"
CANAL_OPENED = "Canal Opened"
BASE_TOKENS = (SHIP, CANOE, AIRSHIP, STAR_RUBY)

# Fixed vehicle spots (ow_rando/world.py; vehicles are never moved).
SHIP_SPAWN = (202, 146)        # Pravoka's vanilla pier water
AIRSHIP_SEED = (211, 230)      # the Lykion Desert spot the airship rises from

START_KEY = "cornelia"
TITAN_KEY = "titan"
AIRSHIP_SITE = "airship"       # pseudo-site: the desert spot (the Airship event)

# tile attributes (re_only/gen_ow_minimap.classify; ow_rando/proto.py)
A_OCEAN, A_RIVER, A_DOCK = 0xF00F, 0xF009, 0x0000
AIR_LAND = frozenset((0x0003, 0x0002, 0x000D, 0x000C, 0x000E))

CANAL_GATE = "canal"           # gates.py gate name of Nerrick's canal
CANAL_CROSS = 0x174            # baked walk + sail when open (gates.CANAL_CROSS)
# item names for plan JSON that predates gates[].item (gates.item_names)
GATE_ITEM_FALLBACK = {
    "lufenian_bridge": "Lufenian Bridge",
    "lufenian_nitro": "Lufenian Nitro Powder",
    "gaian_nitro": "Gaian Nitro Powder",
    "mount_gulg_key": "Mount Gulg Key",
}

# capability bits of one cell
WALK, RIVER, SEA, DOCK, LAND = 1, 2, 4, 8, 16
_MOVE = WALK | RIVER | SEA


def caps_of(attr):
    """Capability bits of a tile attribute."""
    c = 0
    if attr < 0xF000:
        c |= WALK
        if attr == A_DOCK:
            c |= DOCK
        if attr in AIR_LAND:
            c |= LAND
    elif attr == A_RIVER:
        c |= RIVER
    elif attr == A_OCEAN:
        c |= SEA
    return c


_NB4 = None


def nb4():
    """4-neighbour cell indices on the torus (the overworld wraps)."""
    global _NB4
    if _NB4 is None:
        nb = []
        for y in range(N):
            yu, yd = ((y - 1) % N) * N, ((y + 1) % N) * N
            row = y * N
            for x in range(N):
                nb.append((row + (x + 1) % N, row + (x - 1) % N, yd + x, yu + x))
        _NB4 = nb
    return _NB4


def idx(xy):
    return (xy[1] % N) * N + (xy[0] % N)


def xy(i):
    return i % N, i // N


def _label(caps, bit):
    """4-connected components of the cells holding `bit`: (labels, sizes)."""
    nb = nb4()
    lab = [-1] * NN
    sizes = []
    for s in range(NN):
        if not caps[s] & bit or lab[s] >= 0:
            continue
        c = len(sizes)
        lab[s] = c
        stack, n = [s], 1
        while stack:
            i = stack.pop()
            for j in nb[i]:
                if caps[j] & bit and lab[j] < 0:
                    lab[j] = c
                    n += 1
                    stack.append(j)
        sizes.append(n)
    return lab, sizes


def gate_token(gate):
    """The access token one plan gate needs: the canal is opened by Nerrick
    (an event), every other gate by its own AP item."""
    if gate["name"] == CANAL_GATE:
        return CANAL_OPENED
    return gate.get("item") or GATE_ITEM_FALLBACK.get(gate["name"], gate["name"])


class WorldGraph:
    """The contracted access graph of one overworld.

    grid   -- N*N tile ids, row-major (y * N + x), or N rows of N
    att    -- tile id -> attribute (512 entries)
    sites  -- [{"key", "ents": [(x, y)], "appr": [(x, y), ...]}]
              (randworld_territories.sites_from_plan; Titan = two records)
    gates  -- the plan JSON's gates list ({"name", "item", "cells": [[x, y,
              open, closed], ...]}); () on a world without gates
    start  -- the party's new-game overworld cell (Cornelia's exit return), or
              None: then Cornelia's door (door()) stands in for it

    After construction:
      attr[i]   attribute of the BASE tile (a gate cell's closed tile)
      caps[i]   capability bits in the base state (entrances blocked)
      comp[i]   landmass id = node id (L nodes come first), or -1
      tokens    the access tokens of this world, bit k = 1 << k
      lost      [(x, y, gate name, bits)] cells whose capability a gate
                REMOVES when it opens (masked out in every state)
    """

    def __init__(self, grid, att, sites, gates=(), start=None):
        flat = list(grid) if len(grid) == NN else [t for row in grid for t in row]
        if len(flat) != NN:
            raise ValueError(f"grid must hold {NN} cells, got {len(flat)}")
        self.sites = list(sites)
        self.gates = [g for g in (gates or ()) if g.get("cells")]
        attr = [att[int(t)] for t in flat]
        caps = [caps_of(a) for a in attr]

        # tokens: the four vehicles, then one per gate (in plan order)
        toks = list(BASE_TOKENS)
        for g in self.gates:
            t = gate_token(g)
            if t not in toks:
                toks.append(t)
        self.tokens = tuple(toks)
        self.bit = {t: 1 << k for k, t in enumerate(self.tokens)}

        # gate cells: base = closed AND open; the gate adds open AND NOT closed
        self.lost = []
        add = {}                                   # cell -> (gate bit, added caps)
        for g in self.gates:
            gb = self.bit[gate_token(g)]
            for x, y, o, c in g["cells"]:
                i = idx((x, y))
                cc = caps_of(att[int(c)])
                oc = caps_of(att[int(o)])
                if g["name"] == CANAL_GATE and int(o) == CANAL_CROSS:
                    oc = WALK | SEA                # baked walk + sail
                attr[i] = att[int(c)]
                caps[i] = cc & oc
                gone = cc & ~oc & _MOVE
                if gone:
                    self.lost.append((x % N, y % N, g["name"], gone))
                plus = oc & ~cc & _MOVE
                if plus:
                    pb, pa = add.get(i, (0, 0))
                    add[i] = (pb | gb, pa | plus)
        # entrance cells are not pass-through (stepping on one warps you into
        # the site): only for sites placed with an approach record, as in
        # ow_rando/world.py _block_ents
        for s in self.sites:
            if s.get("appr"):
                for e in s.get("ents") or ():
                    caps[idx(e)] &= ~(WALK | DOCK | LAND)
        self.attr, self.caps = attr, caps

        # components
        self.comp, self.comp_size = _label(caps, WALK)
        self.rcomp, rsize = _label(caps, RIVER)
        self.scomp, ssize = _label(caps, SEA)
        nl, nr, ns = len(self.comp_size), len(rsize), len(ssize)
        self.n_land, self.n_river, self.n_sea = nl, nr, ns
        self.r_base, self.s_base = nl, nl + nr
        # gate pieces: per gate bit, 4-connected among the cells it opens
        self.piece = {}                            # cell -> piece node
        self.piece_info = []                       # (gate bit, added caps)
        nb = nb4()
        base = nl + nr + ns
        for i in sorted(add):
            if i in self.piece:
                continue
            gb, _pa = add[i]
            node = base + len(self.piece_info)
            stack, acc = [i], 0
            self.piece[i] = node
            while stack:
                k = stack.pop()
                acc |= add[k][1]
                for j in nb[k]:
                    if j in add and j not in self.piece and add[j][0] == gb:
                        self.piece[j] = node
                        stack.append(j)
            self.piece_info.append((gb, acc))
        self.sky = base + len(self.piece_info)
        self.n_nodes = self.sky + 1
        self._add = add

        self.doors = [self.door(s) for s in self.sites]
        self.start = self._start_node(start)
        self.desert = self.comp[idx(AIRSHIP_SEED)]
        if self.desert < 0:
            self.desert = None
        self._build_edges()
        self._combos = {}

    # ----------------------------------------------------------- nodes ---
    def nodes_of_cell(self, i):
        """Every node cell i belongs to (a canal cell is land AND a gate)."""
        out = []
        if self.comp[i] >= 0:
            out.append(self.comp[i])
        if self.rcomp[i] >= 0:
            out.append(self.r_base + self.rcomp[i])
        if self.scomp[i] >= 0:
            out.append(self.s_base + self.scomp[i])
        p = self.piece.get(i)
        if p is not None:
            out.append(p)
        return out

    def door(self, site):
        """Where you stand outside a site: a walkable approach cell, else a
        walkable cell at or around an entrance -- whichever stands on the
        BIGGEST landmass (a walled town's approach list can lead with a pocket
        inside the walls: archipelago_12's Lufenia). None for a site entered
        from the water (Lifespring). Returns a cell index."""
        cand = [idx(a) for a in site.get("appr") or () if self.caps[idx(a)] & WALK]
        if not cand:
            # an approach a gate shuts (the Mount Gulg seal): stand just
            # outside it, on the ground the gate opens onto
            nb = nb4()
            cand = [j for a in site.get("appr") or () if idx(a) in self._add
                    for j in nb[idx(a)] if self.caps[j] & WALK and j not in self._add]
        if not cand:
            ents = site.get("ents") or ()
            if ents and all(self.attr[idx(e)] == A_OCEAN for e in ents):
                return None
            cand = [idx((x + dx, y + dy)) for x, y in ents
                    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1),
                                   (1, 1), (-1, 1), (1, -1), (-1, -1))
                    if self.caps[idx((x + dx, y + dy))] & WALK]
        if not cand:
            return None
        # max() keeps the first of equals, so list order still breaks ties
        return max(cand, key=lambda i: self.comp_size[self.comp[i]])

    def _start_node(self, start):
        if start is not None:
            c = self.comp[idx(start)]
            if c >= 0:
                return c
        for s, d in zip(self.sites, self.doors):
            if s["key"] == START_KEY and d is not None:
                return self.comp[d]
        return None

    def entry_nodes(self, site):
        """Nodes from which this ONE placed entry counts as reached: its
        approach cells when it has them (world.py _site_reached), else at or
        next to an entrance -- a water entrance (Lifespring) needs its own
        cell, sailed onto."""
        nb = nb4()
        out = set()
        appr = site.get("appr")
        if appr:
            for a in appr:
                out.update(self.nodes_of_cell(idx(a)))
            return out
        for e in site.get("ents") or ():
            i = idx(e)
            if self.attr[i] == A_OCEAN:
                out.update(n for n in self.nodes_of_cell(i)
                           if n >= self.s_base)     # sea body / sea gate only
                continue
            for j in (i,) + nb[i]:
                out.update(self.nodes_of_cell(j))
        return out

    def site_nodes(self):
        """{site key: node set} -- OR over a key's entries (Titan's two
        mouths), plus the airship pseudo-site."""
        out = {}
        for s in self.sites:
            out.setdefault(s["key"], set()).update(self.entry_nodes(s))
        if self.desert is not None:
            out[AIRSHIP_SITE] = {self.desert}
        return out

    # ----------------------------------------------------------- edges ---
    def _build_edges(self):
        nb = nb4()
        B = self.bit
        canoe = B[CANOE]
        adj = [set() for _ in range(self.n_nodes)]

        def edge(u, v, need, both=False):
            if u == v:
                return
            adj[u].add((v, need))
            if both:
                adj[v].add((u, need))

        comp, rcomp, scomp, caps = self.comp, self.rcomp, self.scomp, self.caps
        rb, sb = self.r_base, self.s_base
        piece, info = self.piece, self.piece_info
        for i in range(NN):
            li, ri, si = comp[i], rcomp[i], scomp[i]
            pi = piece.get(i)
            if li < 0 and si < 0 and pi is None:
                continue
            for j in nb[i]:
                lj, rj = comp[j], rcomp[j]
                if li >= 0 and rj >= 0:
                    edge(li, rb + rj, canoe, both=True)
                if si >= 0:
                    if rj >= 0:
                        edge(sb + si, rb + rj, canoe)            # river mouth
                    if lj >= 0 and caps[j] & DOCK:
                        edge(sb + si, lj, 0)                     # moor at a dock
                if pi is not None:
                    gb, acc = info[pi - sb - self.n_sea]
                    sj = scomp[j]
                    pj = piece.get(j)
                    if acc & WALK:
                        if lj >= 0:
                            edge(pi, lj, gb, both=True)
                        if rj >= 0:
                            edge(pi, rb + rj, gb | canoe, both=True)
                    if acc & SEA:
                        if sj >= 0:
                            edge(sb + sj, pi, gb)
                            edge(pi, sb + sj, gb)
                        if rj >= 0:
                            edge(pi, rb + rj, gb | canoe)
                        if lj >= 0 and caps[j] & DOCK:
                            edge(pi, lj, gb)
                    if acc & RIVER:
                        if lj >= 0:
                            edge(pi, lj, gb | canoe, both=True)
                        if rj >= 0:
                            edge(pi, rb + rj, gb | canoe, both=True)
                        if sj >= 0:
                            edge(sb + sj, pi, gb | canoe)
                    if pj is not None and pj != pi:
                        gj, accj = info[pj - sb - self.n_sea]
                        if acc & accj & _MOVE:
                            edge(pi, pj, gb | gj, both=True)
        # the ship: boarded only at its fixed spawn, from the land beside it
        sp = idx(SHIP_SPAWN)
        if scomp[sp] >= 0:
            for j in nb[sp]:
                if comp[j] >= 0:
                    edge(comp[j], sb + scomp[sp], B[SHIP])
        # the airship: lands on any landmass with landable ground. A world
        # with no desert spot on land has no airship to raise at all.
        if self.start is not None and self.desert is not None:
            edge(self.start, self.sky, B[AIRSHIP])
        landable = {comp[i] for i in range(NN) if caps[i] & LAND and comp[i] >= 0}
        for c in sorted(landable):
            edge(self.sky, c, 0)
        # Titan's tunnel: the Star Ruby joins the two mouths' landmasses
        mouths = [s for s in self.sites if s["key"] == TITAN_KEY]
        if len(mouths) == 2:
            a = {n for n in self.entry_nodes(mouths[0]) if n < self.n_land}
            b = {n for n in self.entry_nodes(mouths[1]) if n < self.n_land}
            for u in a:
                for v in b:
                    edge(u, v, B[STAR_RUBY], both=True)
        self.adj = [sorted(s) for s in adj]

    # ----------------------------------------------------------- queries --
    def mask_of(self, items):
        """Token names -> bit mask (names this world does not use are ignored)."""
        m = 0
        for t in items:
            m |= self.bit.get(t, 0)
        return m

    def _bfs(self, mask):
        seen = bytearray(self.n_nodes)
        if self.start is None:
            return seen
        seen[self.start] = 1
        q = deque([self.start])
        adj = self.adj
        while q:
            u = q.popleft()
            for v, need in adj[u]:
                if not seen[v] and need & mask == need:
                    seen[v] = 1
                    q.append(v)
        return seen

    def reach(self, mask, air_gate=False):
        """bytearray over nodes reached with this token mask (an int, or an
        iterable of token names). air_gate: the airship only flies once its
        desert spot is reached WITHOUT it (world.py reach(air_gate=True));
        the default treats Airship as an event token that already implies it."""
        if not isinstance(mask, int):
            mask = self.mask_of(mask)
        air = self.bit[AIRSHIP]
        if air_gate and mask & air:
            low = self.combo(mask & ~air)
            if self.desert is None or not low[self.desert]:
                return low
        return self.combo(mask)

    def combo(self, mask):
        r = self._combos.get(mask)
        if r is None:
            r = self._combos[mask] = self._bfs(mask)
        return r

    def masks(self):
        return range(1 << len(self.tokens))

    def minimal(self, masks):
        """Minimal token sets among `masks` (bit sets), as token-name lists in
        token order, fewest tokens first (randworld_territories' format)."""
        ms = set(masks)
        keep = [m for m in ms if not any(o != m and o & m == o for o in ms)]
        return [[t for t in self.tokens if m & self.bit[t]]
                for m in sorted(keep, key=lambda v: (bin(v).count("1"), v))]

    def rule_of(self, test, air_gate=False):
        """Minimal token sets under which test(reach bytearray) holds, or
        None when it never does."""
        ok = [m for m in self.masks() if test(self.reach(m, air_gate))]
        return self.minimal(ok) if ok else None

    def site_rules(self, air_gate=False):
        """{site key: minimal token sets, or None (never reached)} -- every
        placed site plus the airship pseudo-site (the Airship event)."""
        out = {}
        for key, nodes in self.site_nodes().items():
            nodes = sorted(nodes)
            out[key] = self.rule_of(lambda r, ns=nodes: any(r[n] for n in ns),
                                    air_gate)
        return out
