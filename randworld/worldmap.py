"""The v2 world under construction: one class per cell plus fixed tiles.

Classes (one char per cell, as in the v0 prototype's plan map):
  o sea   r river   M mountain   c cliff/shallows (blocked)
  g grass f forest  d desert     m marsh/bank tile   b river bank   w any walkable
  x a FIXED tile (stamp, dock, pier, gate): see .fixed[i]

MOVEMENT is what logic reads: SEA / RIVER / WALK / ROCK. Every stage after
progression may change a cell's class only within its movement (RANDWORLD_V2
section 2), which is what keeps the planned graph and the final tiles equal.
"""
from . import grid as G
from . import tables as T

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8

SEA, RIVER, WALK, ROCK = 0, 1, 2, 3
CLS_MOVE = {"o": SEA, "r": RIVER, "M": ROCK, "c": ROCK,
            "g": WALK, "f": WALK, "d": WALK, "m": WALK, "b": WALK, "w": WALK}
WALK_CLS = "gfdmbw"
# a representative tile per class (most frequent in vanilla) -- only for
# contracting a class map with worldgraph before the real tiles exist
REP_TILE = {"o": 0x000, "g": 0x0E1, "c": 0x010, "M": 0x054, "f": 0x051,
            "r": 0x1F0, "d": 0x1AF, "m": 0x1C6, "b": 0x0E1, "w": 0x0E1}
ATT = T.ATT
TCLS = T.TCLS


def move_of_tile(t):
    a = ATT[t]
    if a == 0xF00F:
        return SEA
    if a == 0xF009:
        return RIVER
    if a >= 0xF000:
        return ROCK
    return WALK


class WorldMap:
    def __init__(self, sk, ar):
        self.sk = sk
        self.ar = ar
        self.cls = ["g" if v else "o" for v in sk.land]
        self.fixed = {}                     # cell -> tile id
        self.protect = bytearray(NN)        # cells later stages must not touch
        self.no_water = bytearray(NN)       # cells that must never become sea / river
        self.must_reach = []                # cell groups: one cell of each must stay joined in its region
        self.ent_cells = set()              # site entrances: logic treats them as walls
        self.prefer = {}                    # cell -> vanilla tile next to a stamp (solver bias)
        self.placed = []                    # bake.py contract entries
        self.gates = []
        self.gate_plan = None               # gates.Plan (build._attempt)
        self.gate_targets = []              # [((x, y), area)] near-side gate mouths the start paths reach
        self.unland = set()                 # areas that must keep no airship-landable tile
        self.noland = frozenset()           # cells the tile repair keeps non-landable (gates.unlandable)
        self.harbor_token = None            # the Gaian door's item when it walls the pier
        self.desert_token = None            # the item of the gate walling the airship desert
        self.harbor = {}
        self.links = []
        self.shortcuts = []                 # shortcuts.py records (river / dock / landing)
        self.report = {}
        self.regions = None                 # area -> frozenset of areas joined by open borders

    def region(self, a):
        """The areas walkable together with area a (open borders)."""
        if self.regions is None:
            return frozenset((a,))
        return self.regions.get(a, frozenset((a,)))

    def region_cells(self, a):
        out = []
        for b in self.region(a):
            out.extend(self.ar.areas[b].cells)
        return out

    # ------------------------------------------------------------ cells --
    def move(self, i):
        t = self.fixed.get(i)
        if t is not None:
            return move_of_tile(t)
        return CLS_MOVE[self.cls[i]]

    def walk(self, i):
        return self.move(i) == WALK

    def set(self, i, c, force=False):
        if (self.protect[i] or i in self.fixed) and not force:
            return False
        self.cls[i] = c
        return True

    def fix(self, i, t):
        self.fixed[i] = t
        self.cls[i] = "x" if TCLS[t] == "x" else TCLS[t]
        self.protect[i] = 1

    def walk_mask(self, block_entrances=True):
        m = bytearray(1 if self.walk(i) else 0 for i in range(NN))
        if block_entrances:
            for p in self.placed:
                if p["kind"] in ("dock", "harbor", "airship", "bridge"):
                    continue
                if p.get("appr"):
                    for e in p["ents"]:
                        m[G.idx(*e)] = 0
        return m

    # ----------------------------------------------------------- stamps --
    def stamp_cells(self, part, dst):
        """(fixed [(i, tile)], hints [(i, cls)], ents [(x, y)], appr [...])
        of one stamp part placed with its window's top-left at dst."""
        x0, y0 = dst
        fixed = [(G.idx(x0 + i, y0 + j), t) for i, j, t in part["fixed"]]
        hints = [(G.idx(x0 + i, y0 + j), c) for i, j, c in part["hints"]]
        ents = [((x0 + ex) % N, (y0 + ey) % N) for ex, ey in part["ents"]]
        appr = [((x0 + ax) % N, (y0 + ay) % N, dx, dy, k) for ax, ay, dx, dy, k in part["appr"]]
        return fixed, hints, ents, appr

    def put(self, key, kind, part, dst, extra=None, river_site=False):
        """Write a stamp part: fixed tiles, ring hints (the classes vanilla had
        around the structure, water included -- paint.spot_score has already
        checked that a water hint joins no two seas and touches no river),
        and the bake.py placed record."""
        fixed, hints, ents, appr = self.stamp_cells(part, dst)
        water = {i for i, c in hints if c == "o"}
        x0, y0 = dst
        ring = {G.idx(x0 + i, y0 + j): t for i, j, t in part.get("ring", ())}
        for i, c in hints:
            if self.protect[i] or i in self.fixed:
                continue
            if c == "c" and not any(self.cls[j] == "o" or j in water for j in NB4[i]):
                c = "M"                        # a cliff needs its water: plain rock here
            if c == "w":
                vc = T.TCLS[ring[i]] if i in ring else None
                if vc in ("g", "f", "d"):
                    self.cls[i] = vc           # vanilla's own ground: its joins with the stamp exist
                elif self.cls[i] not in WALK_CLS:
                    self.cls[i] = "g"
            else:
                self.cls[i] = c
            self.protect[i] = 1
        for i, t in fixed:
            self.fix(i, t)
        self.prefer.update(ring)
        for ax, ay, _dx, _dy, k in appr:
            i = G.idx(ax, ay)
            if i in self.fixed:
                continue
            if k == "g" and self.cls[i] not in WALK_CLS:
                self.cls[i] = "g"
            elif k == "r":
                self.cls[i] = "r"
            elif k == "o":
                self.cls[i] = "o"
            self.protect[i] = 1
        rec = dict(key=key, kind=kind, src=tuple(part["src"]), dst=tuple(dst), ents=ents,
                   cells=[(i, j) for i, j, _t in part["fixed"]], appr=appr)
        if appr:
            self.ent_cells.update(G.idx(ex, ey) for ex, ey in ents)
        grp = tuple(G.idx(ax, ay) for ax, ay, _dx, _dy, k in appr if k == "g")
        if grp and not river_site:          # river sites are reached by canoe
            self.must_reach.append(grp)
        if extra:
            rec.update(extra)
        self.placed.append(rec)
        return rec

    # ------------------------------------------------------------ graph --
    def tile_grid(self):
        """Provisional tile ids: fixed tiles, else the class's representative
        tile (enough for worldgraph: only attributes matter)."""
        return [self.fixed.get(i, REP_TILE.get(self.cls[i], 0x0E1)) for i in range(NN)]
