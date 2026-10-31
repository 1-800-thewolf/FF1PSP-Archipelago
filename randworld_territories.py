"""Bestiary territories for RANDOM overworlds (user spec 2026-10-14).

On the vanilla map a Bestiary heading ("Forests (Elfheim)") is a 32x32 engine
zone's difficulty band plus the landmass it stands on, and the borders are the
zone grid. A generated world moves every site, so the headings have to be
rebuilt from where the sites actually landed. The user's rules:

  * HUBS are the 14 site-named vanilla headings (HUBS below), each keeping its
    vanilla tier and Bestiary tab. Every other site (Marsh Cave, Matoya, Earth
    Cave, ...) JOINS a hub: one on its own landmass first (nearest on land),
    else the nearest hub across the water. A member site then pulls the land
    around it into its hub ("Marsh Cave next to Lufenia -> Lufenia").
  * A LANDMASS is a foot-walkable patch: sea, rivers and impassable mountains
    all split landmasses. Site entrance cells are not pass-through (stepping on
    one warps you), exactly as in ow_rando/world.py's flood.
  * A landmass with seeds is split by NEAREST SEED MEASURED ON LAND (multi-
    source BFS), seeds = hub doors + member-site doors.
  * A landmass with no site joins the hub of the nearest seed, whole.
  * Tiers follow the hub strictly; geography-only vanilla names (Ryukhan
    Desert, Northwest continent, ...) do not exist here.
  * A territory never spills across a closed world-changing gate (user
    2026-10-23: "monsters near Lufenia" need the Lufenian Bridge): a site or
    an empty landmass that reaches somewhere a hub does not, where only a gate
    item would bring the hub in, joins the nearest hub that does not hide
    behind a gate instead.
  * A terrain heading counts in logic only with >= MIN_TILES REACHABLE tiles
    of that terrain in the territory, per item set.

Access comes from the world's contracted access graph (ff1psp/worldgraph.py,
RANDWORLD_LOGIC_DESIGN.md): the ship boards only at its fixed spawn once the
pier is reached and sails that one body of water, moors at dock tiles (or a
river mouth with the canoe); the canoe walks rivers; the airship lands on
AIR_LAND ground (its trip to the desert lives in the Airship EVENT's rule);
Titan's tunnel joins its two mouths with the Star Ruby; the plan's gates (the
canal = "Canal Opened", the Lufenian Bridge, the Nitro doors, the Gulg seal)
are edges needing their own tokens. Every rule here is a minimal token set
over ALL of those, enumerated on the graph. Vehicles never move
(no-vehicle-coordinate-manipulation), so SHIP_SPAWN / AIRSHIP_SEED are
constants.

Pure Python, no numpy, no Archipelago imports: the apworld runs it at
generation time and the tests run it bare. Inputs are the generated grid
(tile ids), the tile attribute table, the placed sites and the plan's gates.
"""
from collections import deque

from . import worldgraph as WG
from .worldgraph import (A_DOCK, A_OCEAN, A_RIVER, AIR_LAND, AIRSHIP,
                         AIRSHIP_SEED, CANOE, SHIP, SHIP_SPAWN, STAR_RUBY,
                         START_KEY, TITAN_KEY)

N = WG.N
NN = WG.NN

# the four vehicle tokens (logic.py names; test_randworld_territories pins the
# parity). A world's full token list is Territories.tokens.
TOKENS = WG.BASE_TOKENS

# (plan key, Bestiary name, vanilla tier, Bestiary tab). Order = tie-break
# order when two seeds are equally near a cell.
HUBS = (
    ("cornelia", "Cornelia", 0, "cornelia"),
    ("pravoka", "Pravoka", 1, "cornelia"),
    ("duergar", "Mount Duergar", 1, "cornelia"),
    ("elfheim", "Elfheim", 2, "elfheim"),
    ("wkeep", "Western Keep", 3, "elfheim"),
    ("melmond", "Melmond", 4, "earth"),
    ("sarda", "Sarda's Cave", 4, "earth"),
    ("crescent", "Crescent Lake", 5, "fire"),
    ("gulg", "Mount Gulg", 5, "fire"),
    ("onrac", "Onrac", 6, "water"),
    ("trials", "Citadel of Trials", 7, "lategame"),
    ("mirage", "Mirage Tower", 8, "lategame"),
    ("lufenia", "Lufenia", 8, "lategame"),
    ("gaia", "Gaia", 8, "lategame"),
)
HUB_KEYS = tuple(h[0] for h in HUBS)
HUB_INDEX = {k: i for i, k in enumerate(HUB_KEYS)}
HUB_NAME = {h[0]: h[1] for h in HUBS}
HUB_TIER = {h[0]: h[2] for h in HUBS}
HUB_SECTION = {h[0]: h[3] for h in HUBS}

MIN_TILES = 8                  # user 2026-10-14: a heading needs >= 8 reachable tiles

# placed-entry kinds that are not locations (= ow_rando/world.py NOT_SITES; the
# canal window's translation rides export "stamps", see stamp_shifts)
NOT_SITES = frozenset(("dock", "bridge", "airship", "harbor", "canal"))

# Bestiary heading kinds and their words (ow_map.KIND_WORD)
KIND_WORD = {"ow": "Overworld", "forest": "Forests", "desert": "Desert",
             "t4": "Grass"}
LAND_KINDS = frozenset(KIND_WORD)

# Water hunts keep their COMPASS regions (user 2026-10-14). Mirrors of
# ow_map / iso_patcher (test_randworld_territories pins parity). Region names:
#   ocean        the whole sea (regional_ocean OFF: one pool)
#   ocean_start  sea outside the four quadrants (regional_ocean ON)
#   oc1..oc4     the regional_ocean quadrants
#   river        every river (northern_river OFF)
#   river_south  rivers outside the northern band (northern_river ON)
#   rr           the northern band
ZONE_BIAS = 7
RR_NORTH_ZONES = frozenset(range(0, 24)) | {31}
OC_POOL_ZONES = {
    1: (0, 1, 2, 3, 8, 9, 10, 11, 16, 17, 18, 19, 24, 25, 26, 27),
    2: (4, 5, 6, 7, 12, 13, 14, 15, 20, 21, 22, 23, 28, 29, 30, 31),
    3: (32, 33, 34, 40, 41, 42, 48, 49, 50, 56, 57, 58, 59),
    4: (39, 47, 55, 60, 61, 62, 63),
}
WATER_REGIONS = ("ocean", "ocean_start", "oc1", "oc2", "oc3", "oc4",
                 "river", "river_south", "rr")


def zone_of(x, y):
    """The engine's 32x32 zone index, both grid indices clamped to 7
    (ow_map.zone_of): the compass water regions are keyed by it."""
    return (min((x + ZONE_BIAS) >> 5, 7)
            + 8 * min((y + ZONE_BIAS) >> 5, 7))


def walkable(attr):
    return attr < 0xF000


def terrain_of(attr):
    """Encounter terrain of a tile attribute: 'plain' | 'marsh' | 'forest' |
    'desert' | 't4' | None (towns, docks, bridges, water, mountains).
    Mirrors gen_ow_minimap.classify for the classes that roll land tables."""
    if attr == 0x0010:
        return "marsh"
    if attr in (0x0003, 0x0002, 0x000D):
        return "plain"
    if attr == 0x0006:
        return "forest"
    if attr < 0xF000 and (attr & 0x0FFF) == 0x000E:
        return "desert"
    if attr == 0x000C:
        return "t4"
    return None


def heading_kind(terrain, forests_on=True):
    """Bestiary heading kind for a terrain (ow_map.kind_terrains): marsh rolls
    the land table, and with dangerous_forests OFF so do forests. Brush ("t4")
    rolls it too (iso_patcher._apply_rw_brush_is_land, user call 2026-10-26:
    it reads as green ground in game), so a random map has no Grass heading."""
    if terrain in ("plain", "marsh", "t4"):
        return "ow"
    if terrain == "forest":
        return "forest" if forests_on else "ow"
    if terrain == "desert":
        return terrain
    return None


def heading_label(kind, hub):
    return f"{KIND_WORD[kind]} ({HUB_NAME[hub]})"


def sites_from_plan(placed):
    """The location sites of an ow_rando plan's `placed` list, as
    [{"key", "ents": [(x, y)], "appr": [(x, y), ...], "shift": (dx, dy)}]
    (docks, the bridge, the airship desert and the harbor are not locations).
    "shift" = the stamp's translation, dst - src origin mod N (world.py put:
    every entrance lands at vanilla + shift), or None when the entry carries
    no src/dst."""
    out = []
    for p in placed:
        if p.get("kind") in NOT_SITES:
            continue
        src, dst = p.get("src"), p.get("dst")
        shift = (((dst[0] - src[0]) % N, (dst[1] - src[1]) % N)
                 if src and dst else None)
        out.append({"key": p["key"],
                    "ents": [tuple(e[:2]) for e in p.get("ents") or ()],
                    "appr": [tuple(a[:2]) for a in p.get("appr") or ()],
                    "shift": shift})
    return out


def stamp_shifts(placed):
    """{key: [[dx, dy], ...]} for the plan's NON-site stamps that carry
    src/dst (the canal window, the Cornelia bridge, the harbor, the airship
    desert), in plan order. Docks are left out: they are scattered copies of
    one dock stamp and nothing reads their shift. The client paints the
    user's hand-picked vanilla canal bridge tiles at the moved canal with the
    "canal" entry (_openworld_loop)."""
    out = {}
    for p in placed:
        kind = p.get("kind")
        src, dst = p.get("src"), p.get("dst")
        if kind not in NOT_SITES or kind == "dock" or not (src and dst):
            continue
        out.setdefault(p["key"], []).append(
            [(dst[0] - src[0]) % N, (dst[1] - src[1]) % N])
    return out


_nb4 = WG.nb4
_idx = WG.idx
_xy = WG.xy


def _torus_d2(a, b):
    dx = abs(a[0] - b[0]); dy = abs(a[1] - b[1])
    dx = min(dx, N - dx); dy = min(dy, N - dy)
    return dx * dx + dy * dy


class Territories:
    """Territories, access and Bestiary headings of one generated world.

    grid   -- N*N tile ids, row-major (y * N + x), or N rows of N
    att    -- tile id -> attribute (512 entries)
    sites  -- [{"key", "ents", "appr"}] (sites_from_plan)
    stamps -- stamp_shifts(placed) or None; passed through to export()
    gates  -- the plan JSON's "gates" (gates.to_json), or None
    start  -- the party's new-game overworld cell, or None (Cornelia's door)
    merge  -- {hub key: hub key} a tiny / small world folds together
              (randworld/sizing.plan_hub_merge): the first hub's site joins the
              second as a member, so its land rolls the second's row

    After construction:
      graph         the world's WorldGraph (worldgraph.py)
      tokens        its access tokens: the four vehicles, "Canal Opened" when
                    the plan has a canal, then each gate item
      comp[i]       foot landmass id of cell i, or -1 (not walkable)
      territory[i]  HUB_INDEX of the hub owning cell i, or -1
      site_hub      [(site key, (x, y) door or None, hub key or None)]
      comp_access   {landmass: [[token, ...], ...]} minimal item sets
    """

    def __init__(self, grid, att, sites, stamps=None, gates=None, start=None, merge=None):
        self.stamps = {k: [list(s) for s in v] for k, v in (stamps or {}).items()}
        self.merge = dict(merge or {})
        G = self.graph = WG.WorldGraph(grid, att, sites, gates or (), start)
        self.tokens = G.tokens
        # the BASE state: gate cells closed, entrance cells blocked
        A = self.attr = G.attr
        caps = G.caps
        self.walk = bytearray(1 if c & WG.WALK else 0 for c in caps)
        self.river = bytearray(1 if c & WG.RIVER else 0 for c in caps)
        self.sea = bytearray(1 if c & WG.SEA else 0 for c in caps)
        self.terrain = [terrain_of(a) if self.walk[i] else None
                        for i, a in enumerate(A)]
        self.sites = sites
        self.comp, self.comp_size = G.comp, G.comp_size
        # where you stand outside each site: the graph's door rule (approach
        # cell on the BIGGEST landmass; world.py's first-cell rule could pick a
        # pocket inside a walled town, archipelago_12's Lufenia)
        self.doors = list(G.doors)
        self._masks = list(G.masks())
        self._reached = {m: G.combo(m) for m in self._masks}
        # the world-changing gate items (not the vehicles, not Nerrick's canal)
        self._gate_bits = 0
        for t in self.tokens:
            if t not in WG.BASE_TOKENS and t != WG.CANAL_OPENED:
                self._gate_bits |= G.bit[t]
        self._gated_comps = None             # landmasses behind gates (_behind_gate)
        self._assign_sites()
        self._split_territories()
        self._compute_access()

    # ------------------------------------------------------- site -> hub
    def _bfs_owner(self, seeds):
        """Multi-source BFS over walkable land. seeds = [(cell, owner)] in
        priority order; returns owner per cell (-1 = unreached)."""
        nb = _nb4()
        own = [-1] * NN
        q = deque()
        for i, o in seeds:
            if i is not None and own[i] < 0:
                own[i] = o
                q.append(i)
        while q:
            i = q.popleft()
            o = own[i]
            for j in nb[i]:
                if self.walk[j] and own[j] < 0:
                    own[j] = o
                    q.append(j)
        return own

    def _behind_gate(self, c, h):
        """Would landmass c take a roster from behind a closed gate? Hub h
        stands behind one when, the airship aside, its landmass needs a
        world-changing item (the Lufenian Bridge, a Nitro door); then only a
        landmass that is never reached before h's own may take h."""
        hc = self._hub_comp.get(h)
        if not self._gate_bits or c is None or c < 0 or hc is None or hc == c:
            return False
        R = self._reached
        if self._gated_comps is None:
            no_air = (1 << len(self.tokens)) - 1 & ~self.graph.bit[AIRSHIP]
            top, low = R[no_air], R[no_air & ~self._gate_bits]
            self._gated_comps = {k for k in range(len(self.comp_size)) if top[k] and not low[k]}
        if hc not in self._gated_comps:
            return False
        return any(R[m][c] and not R[m][hc] for m in self._masks)

    def _assign_sites(self):
        hub_seeds = []
        hub_xy = []                          # (entrance xy, hub index) for across-water
        self._hub_comp = {}                  # hub index -> its door's landmass
        for k in HUB_KEYS:
            if k in self.merge:
                continue                     # folded into another hub (map_size)
            for s, d in zip(self.sites, self.doors):
                if s["key"] == k:
                    if d is not None:
                        hub_seeds.append((d, HUB_INDEX[k]))
                        self._hub_comp.setdefault(HUB_INDEX[k], self.comp[d])
                    if s["ents"]:
                        hub_xy.append((tuple(s["ents"][0]), HUB_INDEX[k]))
        near_hub = self._bfs_owner(hub_seeds)
        site_hub = []
        for s, d in zip(self.sites, self.doors):
            k = s["key"]
            if k in self.merge:
                h = HUB_INDEX[self.merge[k]]
            elif k in HUB_INDEX:
                h = HUB_INDEX[k]
            elif d is not None and near_hub[d] >= 0:
                h = near_hub[d]                  # a hub on its own landmass
            elif hub_xy and s["ents"]:
                e = tuple(s["ents"][0])          # nearest hub across the water
                c = self.comp[d] if d is not None else None
                ok = [t for t in hub_xy if not self._behind_gate(c, t[1])] or hub_xy
                h = min(ok, key=lambda t: (_torus_d2(e, t[0]), t[1]))[1]
            else:
                h = -1
            site_hub.append(h)
        self._site_hub = site_hub
        self.site_hub = [(s["key"], _xy(d) if d is not None else None,
                          HUB_KEYS[h] if h >= 0 else None)
                         for s, d, h in zip(self.sites, self.doors, site_hub)]

    # ------------------------------------------------------- territories
    def _split_territories(self):
        # hubs first, then member sites, each in HUB order -> stable tie-breaks
        seeds = []
        for want_hub in (True, False):
            for hi in range(len(HUB_KEYS)):
                for s, d, h in zip(self.sites, self.doors, self._site_hub):
                    is_hub = s["key"] in HUB_INDEX and s["key"] not in self.merge
                    if h == hi and d is not None and is_hub == want_hub:
                        seeds.append((d, h))
        terr = self._bfs_owner(seeds)
        # empty landmasses: the hub of the nearest seed, measured straight
        # across water and mountains (8-neighbour BFS over every cell)
        empty = [c for c in range(len(self.comp_size))]
        owned = set(self.comp[i] for i in range(NN) if terr[i] >= 0)
        empty = [c for c in empty if c not in owned]
        if empty and seeds:
            dist = [-1] * NN
            near = [-1] * NN
            q = deque()
            for i, h in seeds:
                if dist[i] < 0:
                    dist[i], near[i] = 0, h
                    q.append(i)
            while q:
                i = q.popleft()
                x, y = i % N, i // N
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1),
                               (1, 1), (-1, 1), (1, -1), (-1, -1)):
                    j = ((y + dy) % N) * N + (x + dx) % N
                    if dist[j] < 0:
                        dist[j], near[j] = dist[i] + 1, near[i]
                        q.append(j)
            best = {}
            for i in range(NN):
                c = self.comp[i]
                if c >= 0 and c not in owned:
                    b = best.get(c)
                    if b is None or dist[i] < b[0]:
                        best[c] = (dist[i], near[i], i)
            empty_hub = {}
            for c, (_d, h, i) in best.items():
                if self._behind_gate(c, h):
                    # the nearest hub hides behind a gate: the nearest seed of
                    # a hub that does not, measured straight
                    ok = [(s, hh) for s, hh in seeds if not self._behind_gate(c, hh)]
                    if ok:
                        xy = _xy(i)
                        h = min(ok, key=lambda t: (_torus_d2(xy, _xy(t[0])), t[1]))[1]
                empty_hub[c] = h
            for i in range(NN):
                c = self.comp[i]
                if c >= 0 and terr[i] < 0 and c in empty_hub:
                    terr[i] = empty_hub[c]
            self.empty_landmass_hub = {c: HUB_KEYS[h] for c, h in empty_hub.items()}
        else:
            self.empty_landmass_hub = {}
        self.territory = terr

    # ------------------------------------------------------------- access
    def reach(self, items):
        """Cells reachable (bytearray) with this item set (token names or a
        graph mask): the landmasses reached, plus the river cells the canoe
        walked and the sea the ship sailed."""
        G = self.graph
        r = G.reach(items)
        out = bytearray(NN)
        for i in range(NN):
            if any(r[n] for n in G.nodes_of_cell(i)):
                out[i] = 1
        return out

    def _compute_access(self):
        """Per token combination (the graph's masks): which landmasses, sites
        and water cells it reaches. Landmass ids are the graph's L node ids."""
        G = self.graph
        masks = list(G.masks())
        self._masks = masks
        self._reached = {m: G.combo(m) for m in masks}
        self.comp_access = {
            c: G.minimal([m for m in masks if self._reached[m][c]])
            for c in range(len(self.comp_size))}
        # site key -> OR over its entries (Titan's two mouths, Cardia's caves),
        # plus the airship desert pseudo-site (the Airship event)
        self.site_rules = G.site_rules()
        # water tallies per graph node, then per combination
        per_node = {}
        for i, regs in self._water_regions():
            n = (G.s_base + G.scomp[i]) if self.sea[i] else (G.r_base + G.rcomp[i])
            d = per_node.setdefault(n, {})
            for r in regs:
                d[r] = d.get(r, 0) + 1
        water_n = {}
        for m in masks:
            hit = self._reached[m]
            cnt = dict.fromkeys(WATER_REGIONS, 0)
            for n, d in per_node.items():
                if hit[n]:
                    for r, k in d.items():
                        cnt[r] += k
            water_n[m] = cnt
        self.water_rules = {}
        for r in WATER_REGIONS:
            ok = [m for m in masks if water_n[m][r] >= MIN_TILES]
            self.water_rules[r] = G.minimal(ok) if ok else None
        self.water_tiles = {r: max(water_n[m][r] for m in masks)
                            for r in WATER_REGIONS}

    def _water_regions(self):
        """[(cell, (region, ...))] for every sea and river cell. The water
        hunts keep their compass regions on a random map (user 2026-10-14):
        the regional_ocean quadrants and the northern_river band are the
        patcher's own zone sets (OC_POOL_ZONES / RR_NORTH_ZONES, which
        test_randworld_territories pins to ow_map's mirrors)."""
        quad = {z: q for q, zs in OC_POOL_ZONES.items() for z in zs}
        out = []
        for i in range(NN):
            x, y = i % N, i // N
            z = zone_of(x, y)
            if self.sea[i]:
                q = quad.get(z)
                out.append((i, ("ocean", f"oc{q}" if q else "ocean_start")))
            elif self.river[i]:
                out.append((i, ("river", "rr" if z in RR_NORTH_ZONES else "river_south")))
        return out

    def export(self, forests_on=True):
        """Plain-JSON snapshot for logic and slot_data:
        {"land": [[kind, hub, tiles, rule], ...], "water": {region: rule},
         "sites": {site key: rule}, "tokens": [token, ...],
         "regions": encode_regions(region_map()),
         "doors": site_doors(), "stamps": stamp_shifts(placed)}.
        rule = OR-of-AND token lists, or None when it never opens (logic then
        drops the place); "sites" carries every placed site plus "airship"
        (the desert spot: the Airship event's place), and logic builds EVERY
        location rule of the seed from it (logic.randworld_site_rules).
        "tokens" = the access tokens the rules may name (the four vehicles,
        "Canal Opened", the plan's gate items). "regions" is what the client
        bakes for the randworld_regions encounter patch -- the same map logic
        counted. "doors" is where the client's overworld-coordinate features
        (NPC doorstep prearms, the Gulg hint) find each moved site; "stamps"
        the same for non-site stamps (the canal bridge paint)."""
        H = self.headings(forests_on)
        return {
            "land": [[k, hub, v["tiles"], v["rule"]]
                     for (k, hub), v in sorted(H.items(), key=lambda kv: (
                         HUB_INDEX[kv[0][1]], kv[0][0]))],
            "water": dict(self.water_rules),
            "sites": dict(sorted(self.site_rules.items())),
            "tokens": list(self.tokens),
            "regions": encode_regions(self.region_map()),
            "doors": self.site_doors(),
            "stamps": {k: [list(s) for s in v] for k, v in self.stamps.items()},
        }

    def site_doors(self):
        """{site key: [{"ent": [x, y], "door": [x, y] or None,
        "shift": [dx, dy] or None}, ...]} -- one record per placed entry, in
        plan order (Titan's two mouths are two records). GRID cells (field x
        = grid x + 1). ent = the first entrance cell, door = the stand cell
        (_door), shift = the stamp's translation from vanilla
        (sites_from_plan). The client's doorstep windows translate by shift,
        which keeps each window's hand-tuned offset from the mouth (the
        Waterfall's sits 5 tiles down its river approach)."""
        out = {}
        for s, d in zip(self.sites, self.doors):
            sh = s.get("shift")
            out.setdefault(s["key"], []).append({
                "ent": list(s["ents"][0]) if s["ents"] else None,
                "door": list(_xy(d)) if d is not None else None,
                "shift": list(sh) if sh is not None else None,
            })
        return out

    # ---------------------------------------------------------- headings
    def headings(self, forests_on=True, min_tiles=MIN_TILES):
        """{(kind, hub key): {"label", "section", "tier", "tiles",
        "by_comp": {comp: tiles}, "rule": [[token, ...]] or None}}.

        rule = the minimal item sets under which the territory has at least
        `min_tiles` REACHABLE tiles of that terrain (None: never -- the
        heading still exists and still rolls, it just carries no logic)."""
        by = {}
        for i in range(NN):
            h = self.territory[i]
            if h < 0:
                continue
            k = heading_kind(self.terrain[i], forests_on)
            if k is None:
                continue
            d = by.setdefault((k, HUB_KEYS[h]), {})
            c = self.comp[i]
            d[c] = d.get(c, 0) + 1
        out = {}
        for (k, hub), per in by.items():
            items = sorted(per.items())
            ok = [m for m in self._masks
                  if sum(n for c, n in items if self._reached[m][c]) >= min_tiles]
            out[(k, hub)] = {
                "label": heading_label(k, hub),
                "section": HUB_SECTION[hub],
                "tier": HUB_TIER[hub],
                "tiles": sum(per.values()),
                "by_comp": per,
                "rule": self.graph.minimal(ok) if ok else None,
            }
        return out

    def region_map(self):
        """bytes(N*N): hub index per cell, 0xFF where no hub (water, mountains,
        towns). The fine region map the encounter patch will read."""
        return bytes(t if t >= 0 else 0xFF for t in self.territory)


def encode_regions(region_map):
    """region_map bytes -> compact text for slot_data (zlib + base64; a
    territory map is a few big blobs, so it packs to a few KB)."""
    import base64
    import zlib
    return base64.b64encode(zlib.compress(bytes(region_map), 9)).decode("ascii")


def decode_regions(text):
    """encode_regions inverse; raises on a corrupt or mis-sized blob."""
    import base64
    import zlib
    out = zlib.decompress(base64.b64decode(text))
    if len(out) != NN:
        raise ValueError(f"region map is {len(out)} B, want {NN}")
    return out
