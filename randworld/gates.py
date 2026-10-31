"""Stages C/D for the world-changing AP items on a random map (M3).

User spec: memory random-world-gate-items-spec (2026-10-14/16/23). Opt-in
through the vanilla yaml toggles:

  unlockable_northern_foot_trails  per seed 0-1 BRIDGES and 1-2 NITRO DOORS
      (user 2026-10-23; at least one gate -- what the engine's world_gates
      rows hold: one span art, two door arts). Each walls in a site picked at
      random from every gate-able site (names.GATE_SITES: a Lufenia bridge no
      likelier than a Pravoka or Cavern of Ice one) and is named after it:
      "Pravoka Bridge", "Cavern of Ice Nitro", "Onrac Nitro (West)" when one
      pocket has both doors.
        bridge  a N-S span over a strait (deck exists only while held, stub
                piers on both banks); no strait -> the site's island is raised
        door    the user's boulder window cut into a ridge, 3-wide trails
  mount_gulg_key   the hand-painted seal over Mount Gulg's mouth, ALWAYS Mount
      Gulg: a HARD lock, the airship cannot get past it either.

(Nerrick's canal, always on, is build._pin_anchors' gate.)

How a gate sits in v2's graph: a region of AIRSHIP rung is already sealed --
ridges all round, no dock, no river (progression.design) -- so a gate is one
more way in, from a region of an earlier rung. Its far side stays reachable by
airship where it has landable ground (a SHORTCUT, like vanilla). On a roll of
UNLANDABLE_P a bridge / Gaian-art door far side loses every landable tile
(forest, rock coasts), at most UNLANDABLE_MAX per world: then only the item
opens it, before or after the airship. The Lufenian door art holds grass on
both sides, so it never rolls.

Rolled on top: the two doors share one pocket (vanilla's Gaia layout, around
any site) or wall one site each; the harbor way 'nitro' (a Gaian-art door is
the start's way to the ship's pier); a gated airship desert. Each gate walls
in exactly its site (plus the pier / the desert when it holds them).

Every gated cell is one of the engine rows (tables.GATES["rows"]): the bake
writes the row's gate id there and the world_gates cave (iso_patcher) shows
the open or closed tile from the save byte the client keeps -- the very rows
vanilla-layout seeds bake, only the cells moved. The gate NAMES lufenian_bridge
/ lufenian_nitro / gaian_nitro below are those art + save-bit slots, not places.
"""
import math

from . import grid as G
from . import paint as PT
from . import progression as PR
from . import tables as T
from .names import COMPASS4, GATE_ITEM_VARIANTS, GATE_SITES, site_name
from .worldmap import WALK, WALK_CLS, move_of_tile

N, NN, NB4, NB8 = G.N, G.NN, G.NB4, G.NB8

LBRIDGE, LNITRO, GNITRO, GULG = "lufenian_bridge", "lufenian_nitro", "gaian_nitro", "mount_gulg_key"
GULG_SITE = "gulg"
DOOR = "door"                          # border kind beside paint.RIDGE / RIVER_B / OPEN
NITRO_RUNG = "nitro"                   # the harbor area behind the start's Gaian door

ART = T.GATES
ROW_GID = {(o, c, m): gid for gid, o, c, m in ART["rows"]}
MASK = {LBRIDGE: ART["span"]["mask"], LNITRO: ART["doors"][LNITRO]["mask"],
        GNITRO: ART["doors"][GNITRO]["mask"], GULG: ART["seal_mask"]}

SPAN = (3, 8)                          # deck rows (vanilla 5; the gap needs >= 3)
TOPOLOGY = (("shared", 0.5), ("independent", 0.5))
BRIDGE_COUNTS = (0, 1)                 # user 2026-10-23: 0-1 bridges ...
DOOR_COUNTS = (1, 2)                   # ... and 1-2 Nitro doors (>= 1 gate)
UNLANDABLE_P = 0.25                    # user 2026-10-23: 1 in 4 ...
UNLANDABLE_MAX = 2                     # ... at most 2 per world
DESERT_GATE_P = 0.25                   # user 2026-10-23: "sometimes"
HARBOR_NITRO_W = 0.25                  # weight of the harbor way 'nitro' (trails on)
DOOR_MIN_BORDER = 8                    # border cell pairs a door needs
MIN_FAR = 160                          # cells a walled-in area holds at least
TRAIL_MAX = 26
EARLY = ("foot", "canoe", "ship", "ship+canoe", "canal", "titan", NITRO_RUNG)
STATS = {}                             # why attempts were thrown away (tools / tests read it)

# ----------------------------------------------------------- item names ---
# Bridge and Nitro door names are pre-declared in names.py (static
# datapackage): each is named after the site it walls in. The key keeps its
# fixed name -- it always opens Mount Gulg.
FIXED_ITEM = {GULG: "Mount Gulg Key"}


def _sgn(d):
    return ((d + N // 2) % N) - N // 2


# ---------------------------------------------------------------- state ---
class Plan:
    """What this world's gates are (wm.gate_plan)."""

    def __init__(self, opts):
        self.trails = bool(opts.get("trails"))
        self.gulg = bool(opts.get("gulg"))
        self.topology = None
        self.counts = (0, 0)        # (bridges, doors) rolled
        self.desert = False
        self.bridge = None          # dict(near, far, head, span, south)
        self.doors = []             # dict(name, near, far, far_end, site)
        self.pins = {}              # site -> area
        self.unland = set()         # far-side areas with no landable tile
        self.notes = []

    @property
    def on(self):
        return self.trails or self.gulg

    def report(self):
        return dict(trails=self.trails, gulg=self.gulg, topology=self.topology, counts=self.counts,
                    desert=self.desert, unland=sorted(self.unland),
                    doors=[(d["name"], d["near"], d["far"], d.get("site")) for d in self.doors],
                    bridge=dict(self.bridge) if self.bridge else None, notes=list(self.notes))


def forced_rungs(opts):
    """{site: rung} assign_sites must honour. Nothing since 2026-10-23: the
    sites behind gates are picked after the design (user: a Lufenia bridge
    no likelier than a Pravoka or Cavern of Ice one)."""
    return {}


def harbor_ways(opts):
    """progression.HARBOR_WAYS plus the Gaian door (trails on, user 2026-10-16)."""
    if not opts.get("trails"):
        return PR.HARBOR_WAYS
    return PR.HARBOR_WAYS + (("nitro", HARBOR_NITRO_W),)


def gated_areas(D):
    return getattr(D, "gated", None) or {}


# ------------------------------------------------------------- planning ---
def _excluded(wm, D, desert_ok=False):
    """Areas no gate may wall in: the start, the harbor, Titan's tunnel ends,
    anything beside a fixed anchor (the canal crossing joins its two shores
    on foot), and the desert unless the desert gate wants it."""
    own = wm.ar.owner
    out = set(D.start)
    out.add(D.harbor_area)
    for a, r in D.rung.items():
        if r in ("titan", NITRO_RUNG):
            out.add(a)
    d0 = wm.ar.of(*_desert_mid())
    for i in G.dilate(list(wm.fixed), 4):
        a = own[i]
        if a >= 0 and not (desert_ok and a == d0):
            out.add(a)
    return out


def _desert_mid():
    from . import skeleton as SK
    return SK.DESERT[len(SK.DESERT) // 2]


def _far_ok(D, b, excl, site=None):
    """May airship area b be walled in (for `site`)? It needs room for the
    site's stamp and some ground around it (a 61-cell islet could not take
    Lufenia, 2026-10-23)."""
    if D.rung.get(b) != "airship" or b in gated_areas(D) or b in excl:
        return False
    need = max(MIN_FAR, PR.site_size(site) * 4) if site else MIN_FAR
    return len(D.graph.wm.ar.areas[b].cells) >= need


def _near_ok(D, a, allow_air=False):
    if a in gated_areas(D) and D.rung.get(a) != NITRO_RUNG:
        return False
    r = D.rung.get(a)
    return r in EARLY or (allow_air and r == "airship")


def _isolate(D, far, keep=()):
    """Every border of the far set becomes a ridge (keep: its door borders)."""
    g = D.graph
    for k in g.bcells:
        if (k[0] in far) != (k[1] in far) and k not in keep:
            D.border[k] = PT.RIDGE


def _gate(D, areas, name):
    for a in areas:
        D.gated[a] = name


_AIR_LAND = (0x0003, 0x0002, 0x000D, 0x000C, 0x000E)     # worldgraph.AIR_LAND


def _stamp_lands(site, gulg):
    """Does the site's own stamp hold an airship-landable tile? Then its far
    side can never be unlandable (Mount Gulg's seal opens onto grass)."""
    tiles = [t for _i, _j, t in T.SITES[site]["parts"][0]["fixed"]]
    if site == GULG_SITE and gulg:
        tiles += [o for _i, _j, o, _c in ART["seal"]]
    return any(T.ATT[t] in _AIR_LAND for t in tiles)


def roll_counts(rng):
    """(bridges, doors) this world carries (user 2026-10-23: 0-1 bridges,
    1-2 Nitro doors -- what the engine's world_gates rows hold today: one
    span and two door arts; a richer mix needs per-seed rows, a later job)."""
    return rng.choice(BRIDGE_COUNTS), rng.choice(DOOR_COUNTS)


def plan(wm, D, cats, rng, g_sea, weights):
    """Choose every gate on the area graph (after progression.design): how
    many bridges and doors, the bridge (fixed into the map right away: it
    needs the raw coasts), the doors' borders, the site each one walls in
    (any site, user 2026-10-23), the unlandable rolls. Raises
    progression.Redo when a rolled gate cannot be laid out."""
    P = wm.gate_plan
    D.gated = getattr(D, "gated", {})
    if not P.trails:
        return
    d0 = wm.ar.of(*_desert_mid())
    unland_left = UNLANDABLE_MAX
    n_bridge, n_door = roll_counts(rng)
    arts = [GNITRO, LNITRO][:n_door]          # the Gaian art first: it may roll unlandable
    harbor_door = getattr(D, "harbor_door", None)
    if harbor_door:
        # the start's way to the pier is a Gaian door (design rolled it)
        s, h0 = harbor_door
        site = _gate_site(cats, weights, rng, P, wm.ar.size(h0))
        if site is None:
            raise PR.Redo("no site for the harbor door")
        _gate(D, (h0,), GNITRO)
        P.pins[site] = h0
        cats[site] = "airship"
        P.doors.append(dict(name=GNITRO, near=s, far=h0, far_end=None, site=site))
        P.notes.append(f"harbor behind the Gaian door ({site})")
        arts = [a for a in arts if a != GNITRO]
        if n_door == 1:
            arts = []
    P.desert = bool(getattr(D, "desert_gate", False))
    desert_by = None
    if P.desert:
        # the cheapest wall: a door from an early neighbour of the desert's area
        excl = _excluded(wm, D, desert_ok=True)
        if arts and any(_near_ok(D, q) and n >= DOOR_MIN_BORDER for q, n in D.graph.nbr[d0].items()) \
                and _far_ok(D, d0, excl - {d0}):
            desert_by = arts[0]
        else:
            desert_by = LBRIDGE
            n_bridge = 1
    P.counts = (n_bridge, n_door)
    # ---- the bridge: a natural strait, else MAKE ROOM (user 2026-10-14)
    if n_bridge:
        site = _gate_site(cats, weights, rng, P)
        if site is None:
            raise PR.Redo("no site for the bridge")
        roll_b = unland_left > 0 and desert_by != LBRIDGE and rng.random() < UNLANDABLE_P             and not _stamp_lands(site, P.gulg)
        want = d0 if desert_by == LBRIDGE else None
        br = _plan_bridge(wm, D, rng, g_sea, site, south_only=roll_b, want_far=want)
        if br is None and want is None:
            br = _isle_bridge(wm, D, rng, g_sea, site)
        if br is None:
            raise PR.Redo("no strait for the bridge")
        P.bridge = br
        if roll_b:
            P.unland.add(br["far"])
            unland_left -= 1
        P.pins[site] = br["far"]
        cats[site] = "airship"
    # ---- doors: one shared pocket (two doors, one site) or one site each
    if len(arts) == 2 and desert_by not in arts and PR._pick(rng, TOPOLOGY) == "shared":
        site = _gate_site(cats, weights, rng, P)
        if site is not None and _plan_shared(wm, D, rng, site):
            cats[site] = "airship"
            P.topology = "shared"
            arts = []
        else:
            P.notes.append("shared rolled; no pocket with two early neighbours")
    for art in arts:
        site = _gate_site(cats, weights, rng, P)
        if site is None:
            raise PR.Redo("no site for a door")
        # only the Gaian window's east end holds no grass: only it can wall
        # an unlandable far side
        roll = art == GNITRO and unland_left > 0 and desert_by != art and rng.random() < UNLANDABLE_P             and not _stamp_lands(site, P.gulg)
        want = d0 if desert_by == art else None
        if not _plan_door(wm, D, rng, art, site, want_far=want, unland=roll):
            raise PR.Redo("no border for a door")
        if roll:
            unland_left -= 1
        cats[site] = "airship"
    if P.topology is None and P.doors:
        P.topology = "independent"
    wm.regions = PR.regions_of(D, D.graph.n)
    for a in P.unland:
        wm.unland.update(wm.region(a))


def _gate_site(cats, weights, rng, P, room=None):
    """The site a gate walls in: any gate-able location site not yet behind
    a gate, airship-rung ones first (so the foot sphere keeps its count),
    else one lent by a later rung. room: the far area's size when known."""
    taken = set(P.pins)
    ok = [s for s in GATE_SITES if s in cats and s not in taken and weights.get(s, 0) > 0
          and (room is None or PR.site_size(s) * 4 <= room)]
    for pool in ([s for s in ok if cats[s] == "airship"],
                 [s for s in ok if cats[s] in ("canal", "ship+canoe", "ship", "canoe")]):
        if pool:
            return rng.choice(sorted(pool))
    return None


def _plan_door(wm, D, rng, name, site, want_far=None, unland=False):
    """A door on a border (near, far): near of an earlier rung, far an
    airship area walled in for `site`."""
    P = wm.gate_plan
    g = D.graph
    excl = _excluded(wm, D, desert_ok=want_far is not None)
    fars = [want_far] if want_far is not None else \
        [b for b in range(g.n) if _far_ok(D, b, excl, site)]
    cand = []
    for b in fars:
        if not (D.rung.get(b) == "airship" and b not in gated_areas(D)):
            continue
        for a, n in g.nbr[b].items():
            if n < DOOR_MIN_BORDER or not _near_ok(D, a, allow_air=unland):
                continue
            pref = 1 if D.rung.get(a) == "airship" else 0
            cand.append((pref, rng.random(), a, b))
    if not cand:
        return False
    cand.sort()
    _p, _r, a, b = cand[0]
    k = (min(a, b), max(a, b))
    _isolate(D, {b}, keep={kk for kk, v in D.border.items() if v == DOOR})
    D.border[k] = DOOR
    _gate(D, (b,), name)
    P.pins[site] = b
    P.doors.append(dict(name=name, near=a, far=b, far_end="e" if unland else None, site=site))
    if unland:
        P.unland.add(b)
    return True


def _plan_shared(wm, D, rng, site):
    """Vanilla's Gaia pocket, around any site: one airship area beside two
    different early regions, a door to each. Never rolls unlandable: the
    Lufenian window holds grass on its pocket side."""
    P = wm.gate_plan
    g = D.graph
    excl = _excluded(wm, D)
    cand = []
    for b in range(g.n):
        if not _far_ok(D, b, excl, site):
            continue
        qs = sorted(q for q, m in g.nbr[b].items() if m >= DOOR_MIN_BORDER and _near_ok(D, q))
        pairs = [(p, q) for p in qs for q in qs if p < q and wm.region(p) != wm.region(q)]
        if pairs:
            cand.append((rng.random(), b, rng.choice(pairs)))
    if not cand:
        return False
    cand.sort()
    _r, b, (p, q) = cand[0]
    keep = {kk for kk, v in D.border.items() if v == DOOR}
    _isolate(D, {b}, keep=keep)
    D.border[(min(p, b), max(p, b))] = DOOR
    D.border[(min(q, b), max(q, b))] = DOOR
    _gate(D, (b,), GNITRO)
    P.pins[site] = b
    P.doors.append(dict(name=LNITRO, near=p, far=b, far_end=None, site=site))
    P.doors.append(dict(name=GNITRO, near=q, far=b, far_end=None, site=site))
    return True


def _plan_bridge(wm, D, rng, g_sea, site, south_only=False, want_far=None):
    """Pick a N-S strait of the ship's sea between an early area and an
    airship area, fix the span into the map (closed deck = gate cells) and
    wall the far area in. Returns the plan dict or None."""
    from . import build as BD
    own = wm.ar.owner
    lab = g_sea["lab"]
    seas = _bridge_seas(g_sea)
    excl = _excluded(wm, D, desert_ok=want_far is not None)

    def land(i, a):
        return own[i] == a and wm.walk(i) and not wm.protect[i] and i not in wm.fixed

    cands = []
    for i in range(NN):
        a = own[i]
        if a < 0 or not land(i, a):
            continue
        x, yn = G.xy(i)
        if not all(land(G.idx(x + dx, yn + dy), a) for dx, dy in ((-1, -1), (0, -1), (-1, 0))):
            continue
        sea_id = lab[G.idx(x, yn + 1)]
        if sea_id not in seas:
            continue
        span = 0
        while span <= SPAN[1]:
            c = G.idx(x, yn + span + 1)
            if lab[c] == sea_id and wm.cls[c] == "o" and not wm.protect[c]:
                span += 1
            else:
                break
        if not SPAN[0] <= span <= SPAN[1]:
            continue
        j = G.idx(x, yn + span + 1)
        b = own[j]
        if b < 0 or b == a or not land(j, b):
            continue
        if not all(land(G.idx(x + dx, yn + span + 1), b) for dx in (-1, 0, 1)) \
                or not land(G.idx(x, yn + span + 2), b):
            continue
        if not all(lab[G.idx(x + dx, yn + k)] == sea_id and not wm.protect[G.idx(x + dx, yn + k)]
                   for k in range(1, span + 1) for dx in (-2, -1, 1, 2)):
            continue
        for near, far, south in ((a, b, True), (b, a, False)):
            if south_only and not south:
                continue
            if want_far is not None:
                if far != want_far or D.rung.get(far) != "airship" or far in gated_areas(D):
                    continue
            elif not _far_ok(D, far, excl, site):
                continue
            if not _near_ok(D, near, allow_air=south_only):
                continue
            pref = (0 if wm.ar.areas[a].mass != wm.ar.areas[b].mass else 1) \
                + (2 if D.rung.get(near) == "airship" else 0)
            cands.append((pref, span + 3 * rng.random(), x, yn, span, near, far, south, sea_id))
    cands.sort()
    for _pref, _s, x, yn, span, near, far, south, sea_id in cands[:30]:
        deck = {G.idx(x, yn + k) for k in range(1, span + 1)}
        sea = bytearray(1 if (lab[c] == sea_id and c not in deck and wm.cls[c] == "o") else 0
                        for c in range(NN))
        _l, sz = G.label(sea)
        if sum(1 for n in sz if n >= 20) > 1:
            continue                       # the sea must stay one body
        _commit_bridge(wm, D, BD.bridge_tiles(x, yn, span), x, yn, span, near, far, south, site)
        return dict(near=near, far=far, head=(x, yn), span=span, south=south)
    return None


def _bridge_seas(g_sea):
    """Sea bodies a Lufenian span may cross: the ship's sea and the sea
    beyond Nerrick's canal (with the canal 'inner', vanilla-style, the ship's
    sea is the small inner basin and the main ocean lies beyond the canal --
    the prototype bridged that big ocean). Never a lake."""
    return {s for s in (g_sea["ship"], g_sea.get("canal")) if s is not None}


ISLE_R = (6, 9)                        # Lufenia's raised island: radius ...
ISLE_RIM = 3                           # ... and open sea all round it


def _isle_bridge(wm, D, rng, g_sea, site):
    """MAKE ROOM (user 2026-10-14): no strait fits, so Lufenia's land rises as
    an island in the ship's sea a short strait SOUTH of an early area's coast
    (the prototype's _isle_bridge). The island is a new area of airship
    rung; the span is committed onto it. Returns the plan dict or None."""
    from . import areas as AR
    from . import build as BD
    ar = wm.ar
    own = ar.owner
    lab = g_sea["lab"]
    seas = _bridge_seas(g_sea)

    def free_land(q, a):
        return own[q] == a and wm.walk(q) and not wm.protect[q] and q not in wm.fixed

    def open_sea(q, sea_id):
        return lab[q] == sea_id and wm.cls[q] == "o" and not wm.protect[q] and q not in wm.fixed \
            and own[q] < 0

    heads = []
    for i in range(NN):
        a = own[i]
        if a < 0 or not _near_ok(D, a) or not free_land(i, a):
            continue
        x, yn = G.xy(i)
        s0 = lab[G.idx(x, yn + 1)]
        if s0 in seas and open_sea(G.idx(x, yn + 1), s0) and \
                all(free_land(G.idx(x + dx, yn + dy), a) for dx, dy in ((-1, -1), (0, -1), (-1, 0))):
            heads.append(i)
    rng.shuffle(heads)
    for i in heads[:800]:
        a = own[i]
        x, yn = G.xy(i)
        sea_id = lab[G.idx(x, yn + 1)]
        span = rng.randint(SPAN[0], 6)
        r = rng.randint(*ISLE_R)
        cx, cy = x + rng.randint(-3, 3), yn + span + 1 + r
        deck = {G.idx(x, yn + k) for k in range(1, span + 1)}
        sides = {G.idx(x + dx, yn + k) for k in range(1, span + 1) for dx in (-2, -1, 1, 2)}
        if not all(open_sea(q, sea_id) for q in set(G.disk(cx, cy, r + ISLE_RIM)) | deck | sides):
            continue
        ph, amp = rng.uniform(0, 6.28), rng.uniform(0.08, 0.16)
        isle = set()
        for q in G.disk(cx, cy, r * 1.2):
            qx, qy = G.xy(q)
            dx, dy = _sgn(qx - cx), _sgn(qy - cy)
            if math.hypot(dx, dy) <= r * (1 + amp * math.sin(3 * math.atan2(dy, dx) + ph)):
                isle.add(q)
        landing = [G.idx(x + dx, yn + span + 1) for dx in (-1, 0, 1)] + [G.idx(x, yn + span + 2)]
        isle.update(landing)
        if isle & (deck | sides):
            continue
        # one 4-connected piece holding the landing
        comp, stack = {landing[1]}, [landing[1]]
        while stack:
            q = stack.pop()
            for n in NB4[q]:
                if n in isle and n not in comp:
                    comp.add(n)
                    stack.append(n)
        isle = comp
        sea = bytearray(1 if (lab[q] == sea_id and wm.cls[q] == "o" and q not in isle and q not in deck)
                        else 0 for q in range(NN))
        _l, sz = G.label(sea)
        if sum(1 for n in sz if n >= 20) > 1:
            continue                       # the sea must stay one body
        nid = len(ar.areas)
        A = AR.Area(nid, G.idx(cx, cy))
        A.cells = sorted(isle)
        A.mass = len(ar.mass_sizes)
        ar.mass_sizes.append(len(isle))
        ar.areas.append(A)
        for q in isle:
            own[q] = nid
            ar.mass[q] = A.mass
            wm.cls[q] = "g"
            lab[q] = -1
            wm.sk.land[q] = 1
        A.coast = sum(1 for q in isle for n in NB4[q] if n not in isle)
        g = D.graph
        g.n += 1
        g.nbr.append({})
        g.coast.append({sea_id: A.coast})
        D.rung[nid] = "airship"
        _commit_bridge(wm, D, BD.bridge_tiles(x, yn, span), x, yn, span, a, nid, True, site)
        wm.gate_plan.notes.append(f"{site}'s island raised (r {r}, span {span})")
        return dict(near=a, far=nid, head=(x, yn), span=span, south=True, isle=True)
    return None


def _commit_bridge(wm, D, tiles, x, yn, span, near, far, south, site):
    sp = ART["span"]
    gate = dict(name=LBRIDGE, kind="bridge", item=f"{site_name(site)} Bridge", mask=sp["mask"],
                cells=[], head=[x, yn], span=span, site=site, far_area=far)
    gated = {}
    for k in range(1, span):
        closed = sp["first"] if k == 1 else (sp["last"] if k == span - 1 else sp["mid"])
        gated[G.idx(x, yn + k)] = (sp["open"], closed)
    for c, t in tiles.items():
        if c in gated:
            o, cl = gated[c]
            wm.fix(c, o)                    # the solver sees the built deck
            cx, cy = G.xy(c)
            gate["cells"].append([cx, cy, o, cl])
        else:
            wm.fix(c, t)
    for k in range(1, span + 1):
        for dx in (-2, -1, 1, 2):
            wm.protect[G.idx(x + dx, yn + k)] = 1
    north, bank = G.idx(x, yn), G.idx(x, yn + span + 1)
    for c, a in ((north, wm.ar.owner[north]), (bank, wm.ar.owner[bank])):
        wm.must_reach.append((c,))
        cx, cy = G.xy(c)
        dy0 = (-2, -1) if c == north else (1, 2)
        for dy in dy0:
            for dx in (-1, 0, 1):
                q = G.idx(cx + dx, cy + dy)
                if wm.ar.owner[q] == a and q not in wm.fixed and wm.cls[q] in WALK_CLS \
                        and all(wm.ar.owner[r] in (a, -1) for r in NB4[q]):
                    wm.protect[q] = 1
    gate["cells"].sort(key=lambda c: (c[1], c[0]))
    wm.gates.append(gate)
    _isolate(D, {far}, keep={kk for kk, v in D.border.items() if v == DOOR})
    _gate(D, (far,), LBRIDGE)
    near_cell = north if south else bank
    wm.gate_targets.append((G.xy(near_cell), near))


# -------------------------------------------------------------- carving ---
_END_DIR = {"w": (-1, 0), "e": (1, 0), "n": (0, -1), "s": (0, 1)}


def _window_groups(spec):
    """Walkable window cells (boulders shut) split into the two sides:
    {end name: set of (i, j)}."""
    tiles = spec["tiles"]
    h, w = len(tiles), len(tiles[0])
    walk = {(i, j) for j in range(h) for i in range(w) if move_of_tile(tiles[j][i]) == WALK}
    out = {}
    for e, (ei, ej) in spec["ends"].items():
        comp = {(ei, ej)}
        stack = [(ei, ej)]
        while stack:
            i, j = stack.pop()
            for di, dj in G.DIRS4:
                q = (i + di, j + dj)
                if q in walk and q not in comp:
                    comp.add(q)
                    stack.append(q)
        out[e] = comp
    a, b = list(out.values())
    assert not (a & b), "door window sides touch with the boulders shut"
    return out


def carve_doors(wm, D, rng):
    """Cut every planned door into its ridge (after build._paint_borders).
    Returns the names of doors that did not fit."""
    bad = []
    for d in wm.gate_plan.doors:
        if not _carve_door(wm, D, rng, d):
            bad.append(d["name"])
    return bad


def _carve_door(wm, D, rng, d):
    spec = ART["doors"][d["name"]]
    tiles = spec["tiles"]
    h, w = len(tiles), len(tiles[0])
    groups = _window_groups(spec)
    gated = {(i, j): (o, c) for i, j, o, c in spec["gated"]}
    a, b = d["near"], d["far"]
    own = wm.ar.owner
    regA, regB = wm.region(a), wm.region(b)
    key = (min(a, b), max(a, b))
    bc = sorted(D.graph.bcells.get(key, ()))
    if not bc:
        return False
    rng.shuffle(bc)
    ends = list(spec["ends"])
    orders = [(ends[0], ends[1]), (ends[1], ends[0])]
    if d.get("far_end"):
        orders = [o for o in orders if o[1] == d["far_end"]]
    def search(c):
        # trails must reach each region's MAIN walkable body (a stray pocket
        # of the region would leave the door opening onto nothing)
        bodies = (PT.area_walk_component(wm, a), PT.area_walk_component(wm, b))
        found = None
        cx, cy = G.xy(c)
        for ox in range(cx - w + 1, cx + 1):
            for oy in range(cy - h + 1, cy + 1):
                for eA, eB in orders:
                    got = _try_window(wm, spec, groups, gated, ox, oy, eA, eB, (a, b), regA, regB,
                                      bodies)
                    if got is not None:
                        score = len(got[0]) + len(got[1]) + rng.random()
                        if found is None or score < found[0]:
                            found = (score, ox, oy, eA, eB, got, bodies)
        return found

    # the ridge as painted first; then grow it around one border cell at a
    # time (rolled back when no window fits there)
    best = None
    for c in bc[:80]:
        got = search(c)
        if got is not None and (best is None or got[0] < best[0]):
            best = got
            if best[0] < 6:
                break
    if best is None:
        for c in bc[:40]:
            snap = PT.snapshot(wm)
            _thicken_at(wm, c, (a, b))
            best = search(c)
            if best is not None:
                break
            PT.restore(wm, snap)
    if best is None:
        return False
    _s, ox, oy, eA, eB, (pathA, pathB), bodies = best
    unland_far = b in wm.gate_plan.unland
    gate = dict(name=d["name"], kind="door", item=None, mask=spec["mask"], cells=[],
                origin=[ox % N, oy % N], site=d.get("site"), far_area=b)
    for j in range(h):
        for i in range(w):
            q = G.idx(ox + i, oy + j)
            if (i, j) in gated:
                o, cl = gated[(i, j)]
                # the solver sees the CLOSED boulder (legal under its pixel
                # rules: the ridge runs through it); shown the open pass it
                # drew a lone half boulder in a gap (prototype, 2026-10-15)
                wm.fix(q, cl)
                gate["cells"].append([(ox + i) % N, (oy + j) % N, o, cl])
            else:
                wm.fix(q, tiles[j][i])
    both = regA | regB
    win = {G.idx(ox + i, oy + j) for j in range(h) for i in range(w)}
    sides = {eA: {G.idx(ox + i, oy + j) for i, j in groups[eA]},
             eB: {G.idx(ox + i, oy + j) for i, j in groups[eB]}}
    carved = {}
    for path, reg, far_side, e_other in ((pathA, regA, False, eB), (pathB, regB, True, eA)):
        cls = "f" if (far_side and unland_far) else "g"
        # the other side's trail and window cells, with a cell to spare
        off = set(carved) | sides[e_other] | {n for q in carved for n in NB4[q]}
        off |= {n for q in sides[e_other] for n in NB4[q]}
        for q in _widen(wm, path, reg, both, keep_off=off - set(path)):
            carved[q] = cls
    for q, cls in carved.items():
        wm.set(q, cls, force=True)
        wm.protect[q] = 1
    # bury the window and the trails: the rock around them keeps what it is,
    # so no later stage opens a way round the boulders (whichever area's half
    # of the ridge a cell lies in) or seals a trail mouth
    for q in carved:
        for n in NB8[q]:
            if not wm.walk(n):
                wm.protect[n] = 1
    for j in range(-2, h + 2):
        for i in range(-2, w + 2):
            wm.protect[G.idx(ox + i, oy + j)] = 1
    # each side's mouth: the body cell its trail lands on (owned by that
    # region, whichever half of the ridge the window and trail lie in)
    # stays joined to the region's main ground through every later stage
    mouths = []
    for e, path, body in ((eA, pathA, bodies[0]), (eB, pathB, bodies[1])):
        ei, ej = spec["ends"][e]
        dx, dy = _END_DIR[e]
        last = path[-1] if path else G.idx(ox + ei + dx, oy + ej + dy)
        m = last if last in body else next(n for n in NB4[last] if n in body)
        wm.must_reach.append((m,))
        mouths.append(m)
    wm.gate_targets.append((G.xy(mouths[0]), a))
    wm.gates.append(gate)
    return True


def _thicken_at(wm, c, pair, r=4.5):
    """Grow the ridge around border cell c to fit a 4x3 window: walkable
    cells of the two areas turn to rock where that cuts nothing (simple
    points, unprotected, clear of stamps and must-reach cells)."""
    own = wm.ar.owner
    cx, cy = G.xy(c)
    clear = set()
    for grp in wm.must_reach:
        for i in grp:
            clear.update(G.dilate((i,), 3))
    for i in sorted(G.disk(cx, cy, r), key=lambda i: G.tdist2(G.xy(i), (cx, cy))):
        if own[i] in pair and wm.walk(i) and not wm.protect[i] and i not in wm.fixed \
                and i not in clear and i not in wm.ent_cells \
                and not any(wm.cls[j] in ("o", "r") for j in NB4[i]) and PT.simple_to_block(wm, i):
            wm.set(i, "M")


def _try_window(wm, spec, groups, gated, ox, oy, eA, eB, pair, regA, regB, bodies):
    own = wm.ar.owner
    tiles = spec["tiles"]
    h, w = len(tiles), len(tiles[0])
    win = {}
    for j in range(h):
        for i in range(w):
            q = G.idx(ox + i, oy + j)
            if own[q] not in pair or wm.protect[q] or q in wm.fixed or wm.cls[q] not in ("M", "c"):
                return None
            win[q] = (i, j)
    # a side's window cells may lie on either area's half of the ridge: the
    # protected rings (_carve_door) keep any later stage from opening a way
    # round the boulders, so only walkable contacts matter
    side = {}
    for e, reg in ((eA, regA), (eB, regB)):
        for (i, j) in groups[e]:
            side[G.idx(ox + i, oy + j)] = reg
    # nothing outside may touch a side's window cells but its own region
    for q, reg in side.items():
        for n in NB4[q]:
            if n in win:
                continue
            if wm.walk(n) and own[n] not in reg:
                return None
    paths = []
    other_cells = {eA: {q for q, r in side.items() if r is regB},
                   eB: {q for q, r in side.items() if r is regA}}
    taken = set()
    both = regA | regB
    for e, reg, body in ((eA, regA, bodies[0]), (eB, regB, bodies[1])):
        ei, ej = spec["ends"][e]
        dx, dy = _END_DIR[e]
        s0 = G.idx(ox + ei + dx, oy + ej + dy)
        path = _trail(wm, s0, reg, both, win, other_cells[e] | taken, body)
        if path is None:
            return None
        taken |= set(path) | {n for p in path for n in NB4[p]}
        paths.append(path)
    return paths[0], paths[1]


def _trail_ok(wm, q, reg, both, win, avoid):
    """May a trail of region `reg` run over rock cell q? Inside the two
    regions' rock (`both`), touching no other region's ground."""
    own = wm.ar.owner
    if q in win or q in avoid or q in wm.fixed or wm.protect[q] or own[q] not in both:
        return False
    if wm.cls[q] not in ("M", "c"):
        return False
    for n in NB4[q]:
        if n in avoid:
            return False
        o = own[n]
        if wm.walk(n) and o >= 0 and o not in reg:
            return False
        if wm.cls[n] in ("o", "r"):
            return False                  # off coasts and rivers: the solver eats them
    return True


def _trail(wm, s0, reg, both, win, avoid, body):
    """Cells from s0 (just outside the window end) through the ridge's rock
    to `body` (region `reg`'s main walkable piece), [] when s0 already
    stands on it, None when no trail fits."""
    if wm.walk(s0):
        return [] if s0 in body and s0 not in avoid else None
    if not _trail_ok(wm, s0, reg, both, win, avoid):
        return None
    prev = {s0: None}
    q = [s0]
    k = 0
    while k < len(q):
        i = q[k]
        k += 1
        depth = 0
        p = i
        while prev[p] is not None:
            p = prev[p]
            depth += 1
        if depth > TRAIL_MAX:
            continue
        for j in NB4[i]:
            if j in prev:
                continue
            if j in body and j not in avoid and j not in win:
                out = []
                while i is not None:
                    out.append(i)
                    i = prev[i]
                return out[::-1]
            if _trail_ok(wm, j, reg, both, win, avoid):
                prev[j] = i
                q.append(j)
    return None


def _widen(wm, path, reg, both, keep_off=()):
    """A trail 3 wide where its side cells are plain rock of the ridge that
    touches no other region's ground (the user's look: 3-wide trails, v0)."""
    out = list(path)
    own = wm.ar.owner
    for k, i in enumerate(path):
        if len(path) > 1:
            j = path[k + 1] if k + 1 < len(path) else path[k - 1]
            (x0, y0), (x1, y1) = G.xy(i), G.xy(j)
            dx, dy = _sgn(x1 - x0), _sgn(y1 - y0)
        else:
            dx, dy = 1, 0
        x, y = G.xy(i)
        for sx, sy in ((x - dy, y + dx), (x + dy, y - dx)):
            q = G.idx(sx, sy)
            if q in out or q in keep_off or own[q] not in both or q in wm.fixed or wm.protect[q] \
                    or wm.cls[q] not in ("M", "c"):
                continue
            if any((wm.walk(n) and own[n] >= 0 and own[n] not in reg) or n in keep_off
                   or wm.cls[n] in ("o", "r") for n in NB4[q]):
                continue
            out.append(q)
    return out


# ------------------------------------------------------------------ seal ---
def site_part(wm, key):
    """The stamp part `key` places: Mount Gulg carries its seal (open tiles,
    the solver's view) when the key is on."""
    part = T.SITES[key]["parts"][0]
    if key != GULG_SITE or not wm.gate_plan.gulg:
        return part
    seal = {(i, j): o for i, j, o, _c in ART["seal"]}
    out = dict(part)
    out["fixed"] = [(i, j, t) for i, j, t in part["fixed"] if (i, j) not in seal] \
        + [(i, j, o) for (i, j), o in sorted(seal.items())]
    out["hints"] = [(i, j, c) for i, j, c in part["hints"] if (i, j) not in seal]
    return out


def add_seal(wm):
    """The Gulg seal gate, from wherever Mount Gulg was stamped."""
    if not wm.gate_plan.gulg:
        return True
    p = next((p for p in wm.placed if p["key"] == GULG_SITE), None)
    if p is None:
        return False
    dx, dy = p["dst"]
    wm.gates.append(dict(name=GULG, kind="seal", item=FIXED_ITEM[GULG], mask=MASK[GULG],
                         cells=[[(dx + i) % N, (dy + j) % N, o, c] for i, j, o, c in ART["seal"]]))
    return True


# ---------------------------------------------------------------- names ---
def name_items(wm):
    """Give every Nitro door its AP item name (names.GATE_ITEM_VARIANTS): the
    site it walls in, plus a compass side (East/South/West/North) when one
    pocket has two doors. Bridges are named when committed."""
    doors = [g for g in wm.gates if g.get("kind") == "door"]
    by_site = {}
    for g in doors:
        by_site.setdefault(g.get("site"), []).append(g)
    for site, gs in by_site.items():
        base = f"{site_name(site)} Nitro"
        if len(gs) == 1:
            gs[0]["item"] = base
            continue
        cells = wm.ar.areas[gs[0]["far_area"]].cells
        cx = sum(G.xy(i)[0] for i in cells) / len(cells)
        cy = sum(G.xy(i)[1] for i in cells) / len(cells)
        # each door takes the nearest compass side the other has not taken
        taken = set()
        for g in gs:
            dx, dy = _sgn(g["origin"][0] - cx), _sgn(g["origin"][1] - cy)
            ang = math.degrees(math.atan2(dy, dx)) % 360
            order = sorted(range(4), key=lambda k: min(abs(ang - 90 * k), 360 - abs(ang - 90 * k)))
            k = next(k for k in order if k not in taken)
            taken.add(k)
            g["item"] = f"{base} ({COMPASS4[k]})"
        assert all(g["item"] in GATE_ITEM_VARIANTS for g in gs), [g["item"] for g in gs]
    harbor = getattr(wm.design, "harbor_door", None)
    wm.harbor_token = None
    if harbor:
        g = next((g for g in doors if g["far_area"] == harbor[1]), None)
        wm.harbor_token = g["item"] if g else None
    # the airship rung of verify's ladder holds the item that walls the desert
    wm.desert_token = None
    if wm.gate_plan.desert:
        d0 = wm.ar.of(*_desert_mid())
        for g in wm.gates:
            far = g.get("far_area") if g.get("kind") in ("door", "bridge") else None
            if far is not None and d0 in wm.region(far):
                wm.desert_token = g["item"]


def gate_items(wm):
    """{gate name: AP item name} for this world (canal excluded)."""
    return {g["name"]: g["item"] for g in wm.gates if g["name"] != "canal"}


# ------------------------------------------------------------ unlandable --
def unlandable(wm):
    """Rolled far sides keep no landable tile: ground turns to forest and
    coasts to rock where that cuts nothing (the airship lands on grass and
    desert only). Run at the end of decoration."""
    if not wm.unland:
        return
    own = wm.ar.owner
    for _r in range(3):
        for i in range(NN):
            if own[i] not in wm.unland or i in wm.fixed or wm.protect[i]:
                continue
            if wm.cls[i] in WALK_CLS and any(wm.cls[j] == "o" for j in NB8[i]) \
                    and PT.simple_to_block(wm, i):
                wm.cls[i] = "M"
    # the whole far-side ground, not only the area's own cells: a trail may
    # run over the near area's half of the ridge (and decoration's forest
    # thinning turned its 1-wide stretches to grass, 2026-10-23)
    shut = {G.idx(x, y) for g in wm.gates for x, y, _o, _c in g["cells"]}
    seen = set()
    stack = [i for i in range(NN) if own[i] in wm.unland and wm.walk(i) and i not in shut]
    seen.update(stack)
    while stack:
        i = stack.pop()
        if i not in wm.fixed and wm.cls[i] in WALK_CLS:
            wm.cls[i] = "f"
        for j in NB4[i]:
            if j not in seen and j not in shut and wm.walk(j):
                seen.add(j)
                stack.append(j)
    # the tile repair (tiles.repair noland) never draws a landable tile there,
    # nor on the rock around it (a reshaper flip could open one onto it)
    wm.noland = frozenset(seen | {j for i in seen for j in NB8[i]}
                          | {i for i in range(NN) if own[i] in wm.unland})


# ----------------------------------------------------------------- disc ---
def disc_ids(gates):
    """{cell: gate tile id} the bake writes for every non-canal gate cell."""
    out = {}
    for g in gates:
        if g["name"] == "canal":
            continue
        for x, y, o, c in g["cells"]:
            out[G.idx(x, y)] = ROW_GID[(o, c, g["mask"])]
    return out
