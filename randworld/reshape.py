"""Stage F feedback: let tile repair change a cell's MOVEMENT where the class
map drew a shape no vanilla art can (measured 2026-10-17: ~70% of the seams
left after repair sit in windows that are unsolvable with movement locked and
solvable without it -- coast and river kinks mostly).

A flip is offered only where it is topologically inert (RANDWORLD_V2 section
3: plan == tiles stays true because the class map is updated with the tile):
  - the cell is a SIMPLE point of the set it leaves: its neighbours in that
    set form one 4-connected run around it, so nothing splits;
  - its 4-neighbours in the set it joins form one run too, so nothing merges;
  - joining the walkable set: every walkable 4-neighbour is in ONE region
    (rivers and ridges between regions never open);
  - every land <-> river and sea -> river contact (canoe, river mouth: the
    access graph's edges, worldgraph) between components stays, none is new;
  - never on or beside a fixed tile, a protected cell or an entrance.
generate_tiles re-verifies the whole graph on the tiles afterwards and undoes
every flip (and the foot tier) if any rung moved.
"""
from . import grid as G
from . import tables as T

NN, NB4, NB8 = G.NN, G.NB4, G.NB8
TCLS = T.TCLS
# NB8 is E W S N SE SW NE NW; the ring clockwise from N:
_RING = (3, 6, 0, 4, 2, 5, 1, 7)
_EDGE = (True, False, True, False, True, False, True, False)


def kind_of_cls(c):
    if c == "o":
        return "sea"
    if c == "r":
        return "river"
    if c in ("M", "c", "x"):
        return "block"
    return "ground"


def kind_of_tile(tk):
    """tiles._kind name -> class-level kind."""
    return {"sea": "sea", "river": "river", "block": "block"}.get(tk, "ground")


def cls_for_tile(t, kind):
    if kind == "sea":
        return "o"
    if kind == "river":
        return "r"
    c = TCLS[t]
    if kind == "block":
        return "c" if c == "c" else "M"
    if c in ("g", "f", "d"):
        return c
    return "b" if c == "m" else "g"


_CROSS = {("ground", "river"), ("river", "ground"), ("sea", "river"), ("river", "sea")}


class Reshaper:
    def __init__(self, wm):
        self.wm = wm
        self.flipped = {}                  # cell -> its class before the first flip
        near = set()
        for i in wm.fixed:
            near.update(NB8[i])
            near.add(i)
        for i in wm.ent_cells:
            near.update(NB8[i])
            near.add(i)
        for grp in wm.must_reach:
            near.update(grp)
        from . import skeleton as SK
        open_edges = getattr(wm.sk, "open_edges", "")
        for i in range(NN):                   # closed edges stay sea (skeleton.SEAM_BAND)
            x, y = G.xy(i)
            if SK.edge_dist(x, y, open_edges) < SK.SEAM_BAND + 1:
                near.add(i)
        from .. import worldgraph as WG
        sp = G.idx(*WG.SHIP_SPAWN)            # the ship boards from the land beside it
        near.update(G.dilate((sp,), 2))
        self.frozen = near
        # component labels per movement set (ids unique across sets) and the
        # access-graph contacts between them: land <-> river (canoe), sea ->
        # river (a mouth). A flip must leave the SET of touching pairs as is.
        self.lab = [-1] * NN
        base = 0
        for kind in ("ground", "river", "sea"):
            m = bytearray(1 if self._kind(i) == kind else 0 for i in range(NN))
            lab, sizes = G.label(m)
            for i in range(NN):
                if lab[i] >= 0:
                    self.lab[i] = base + lab[i]
            base += len(sizes)
        self.next_lab = base
        self.contacts = {}                 # (piece, river) -> number of cell contacts
        for i in range(NN):
            for j in (NB4[i][0], NB4[i][2]):   # E and S: each contact once
                key = self._pair(i, j)
                if key is not None:
                    self.contacts[key] = self.contacts.get(key, 0) + 1

    def _kind(self, i):
        return kind_of_cls(self.wm.cls[i]) if i not in self.wm.fixed else "block"

    def _pair(self, i, j, ki=None, li=None):
        a = ki or self._kind(i)
        b = self._kind(j)
        if (a, b) not in _CROSS:
            return None
        la = self.lab[i] if li is None else li
        return (la, self.lab[j]) if a != "river" else (self.lab[j], la)

    def _runs(self, i, kind):
        """4-connected runs of `kind` around cell i that touch it (contain an
        edge neighbour)."""
        nb = NB8[i]
        inset = [self._kind(nb[d]) == kind for d in _RING]
        if all(inset):
            return 1
        start = inset.index(False)
        runs = 0
        cur_edge = False
        in_run = False
        for k in range(1, 9):
            p = (start + k) % 8
            if inset[p]:
                in_run = True
                cur_edge = cur_edge or _EDGE[p]
            else:
                if in_run and cur_edge:
                    runs += 1
                in_run = False
                cur_edge = False
        return runs

    def region_key(self, i):
        a = self.wm.ar.owner[i]
        return None if a < 0 else min(self.wm.region(a))

    def _new_label(self, i, new):
        for j in NB4[i]:
            if self._kind(j) == new:
                return self.lab[j]
        return None

    def _delta(self, i, new):
        """Contact count changes if cell i became `new`."""
        d = {}
        for j in NB4[i]:
            key = self._pair(i, j)
            if key is not None:
                d[key] = d.get(key, 0) - 1
        if new in ("ground", "river", "sea"):
            li = self._new_label(i, new)
            for j in NB4[i]:
                key = self._pair(i, j, new, li)
                if key is not None:
                    if li is None:
                        return None        # a brand-new piece touching a river
                    d[key] = d.get(key, 0) + 1
        return d

    def ok(self, i, new):
        """May cell i change to movement kind `new` (sea/river/ground/block)?"""
        wm = self.wm
        if i in self.frozen or wm.protect[i] or i in wm.fixed:
            return False
        old = self._kind(i)
        if old == new:
            return True
        if new in ("sea", "river") and wm.no_water[i]:
            return False
        if old != "block" and self._runs(i, old) > 1:
            return False                   # leaving would split its set
        if new != "block" and self._runs(i, new) > 1:
            return False                   # joining would merge two pieces
        if new == "ground":
            keys = {self.region_key(j) for j in NB4[i] if self._kind(j) == "ground"}
            if len(keys) > 1:              # (a cell outside every area counts as its own)
                return False
        d = self._delta(i, new)
        if d is None:
            return False
        for key, v in d.items():           # every land/sea-river contact kept, none new
            if (self.contacts.get(key, 0) > 0) != (self.contacts.get(key, 0) + v > 0):
                return False
        return True

    def _flip(self, i, t, kind):
        d = self._delta(i, kind)
        for key, v in d.items():
            self.contacts[key] = self.contacts.get(key, 0) + v
        undo = (i, self.wm.cls[i], self.lab[i], d)
        li = self._new_label(i, kind) if kind != "block" else -1
        if li is None:
            li = self.next_lab
            self.next_lab += 1
        self.lab[i] = li
        self.wm.cls[i] = cls_for_tile(t, kind)
        return undo

    def _unflip(self, undo):
        i, c, li, d = undo
        for key, v in d.items():
            self.contacts[key] -= v
        self.lab[i] = li
        self.wm.cls[i] = c

    def apply(self, changes):
        """changes: [(cell, tile, kind)]. All or nothing, re-checked one by
        one against the map as it changes. Returns True if applied."""
        done = []
        for i, t, kind in changes:
            if not self.ok(i, kind):
                for u in reversed(done):
                    self._unflip(u)
                return False
            done.append(self._flip(i, t, kind))
        for u in done:
            self.flipped.setdefault(u[0], u[1])
        return True

    def undo(self):
        for j, c in self.flipped.items():
            self.wm.cls[j] = c
        self.flipped = {}
