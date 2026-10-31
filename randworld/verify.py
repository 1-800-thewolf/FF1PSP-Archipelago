"""Stage G: contract the world with worldgraph (the apworld's own reach code)
and re-derive every site's ladder rung and every user rule.

Before tiles exist, the class map stands in (worldmap.tile_grid: one
representative tile per class, same attributes). After tiles.py the real
grid is checked the same way and must give the same answers.
"""
from .. import worldgraph as WG
from ..randworld_territories import sites_from_plan
from . import grid as G
from . import tables as T

LADDER = ("foot", "canoe", "ship", "ship+canoe", "canal", "airship")
AIRPROOF_CAP = 15
FOOT_SITES = {0: (4, 6), 1: (4, 8), 2: (4, 10)}
DOCK_SEA_MIN = 100                 # sea cells a dock's water face must join (else a pond)


def ladder_states(harbor_way, harbor_token=None, desert_token=None):
    """The ladder's token sets. harbor_token: the Gaian door's item when the
    harbor way is 'nitro'; desert_token: the item of the gate that walls the
    airship desert in (gates.py) -- the airship rung holds it."""
    h = {"canoe": {"Canoe"}, "titan": {"Star Ruby"}}.get(harbor_way, set())
    if harbor_way == "nitro" and harbor_token:
        h = {harbor_token}
    d = {desert_token} if desert_token else set()
    base = [set(), {"Canoe"}, {"Ship"} | h, {"Ship", "Canoe"} | h,
            {"Ship", "Canoe", "Canal Opened", "Star Ruby"} | h,
            {"Ship", "Canoe", "Canal Opened", "Star Ruby", "Airship"} | h | d]
    return base


def dock_problems(wm, wg):
    """Every placed dock as the graph sees it (paint.dock_fits, user
    2026-10-30): land on its ring where vanilla had land, its water face on
    open sea (no pond) with DOCK_CHANNEL sea cells straight out."""
    from . import paint as PT
    pieces = {tuple(d["src"]): d for d in T.DOCKS}
    caps, scomp = wg.caps, wg.scomp
    sizes = {}
    out = []
    for rec in wm.placed:
        d = pieces.get(tuple(rec["src"])) if rec.get("kind") == "dock" else None
        if d is None:
            continue
        x0, y0 = rec["dst"]

        def at(i, j):
            return G.idx(x0 + i, y0 + j)
        bad = []
        if any(caps[at(i, j)] & (WG.SEA | WG.RIVER) for i, j, c in PT.dock_ring(d) if c):
            bad.append("water beside the pier")
        face = [at(i, j) for i, j in PT.dock_water(d)]
        if not all(caps[q] & WG.SEA for q in face):
            bad.append("water face filled in")
        else:
            for k in {scomp[q] for q in face}:
                if k not in sizes:
                    sizes[k] = sum(1 for i in range(G.NN) if scomp[i] == k)
                if sizes[k] < DOCK_SEA_MIN:
                    bad.append("water face is a pond")
                    break
        if not any(all(caps[at(i, j)] & WG.SEA for i, j in run) for _f, run in PT.dock_channels(d)):
            bad.append("no open channel")
        if bad:
            out.append(f"dock at {tuple(rec['ents'][0])}: {', '.join(bad)}")
    return out


def graph_of(wm, grid=None):
    grid = grid if grid is not None else wm.tile_grid()
    return WG.WorldGraph(grid, T.ATT, sites_from_plan(wm.placed), wm.gates)


def gate_tokens(wg):
    """The world-changing items this world's graph knows (canal excluded)."""
    return tuple(t for t in wg.tokens if t not in WG.BASE_TOKENS and t != WG.CANAL_OPENED)


def world_ladder(wm):
    """ladder_states for this world (its harbor way and gate tokens)."""
    return ladder_states(wm.design.harbor_way, getattr(wm, "harbor_token", None),
                         getattr(wm, "desert_token", None))


def rungs(wg, harbor_way, extra=(), states=None):
    """{site key: first rung index reached, -1 = never}. extra: tokens held
    on every rung (all gate items: the rung a site has once its gate is
    open). states: the ladder (default ladder_states(harbor_way))."""
    nodes = wg.site_nodes()
    out = {k: -1 for k in nodes}
    for k, st in enumerate(states or ladder_states(harbor_way)):
        r = wg.reach(wg.mask_of(set(st) | set(extra)), air_gate=True)
        for key, ns in nodes.items():
            if out[key] < 0 and any(r[n] for n in ns):
                out[key] = k
    return out


def access_sig(wm):
    """What decoration and tiles must never change: the ladder rungs without
    and with the gate items, and which gates the airship can bypass."""
    r = wm.report
    return (dict(r.get("rungs") or {}), dict(r.get("rungs_g") or {}),
            dict(r.get("gate_air") or {}))


def _subsets(items):
    items = list(items)
    for m in range(1 << len(items)):
        yield {items[k] for k in range(len(items)) if m >> k & 1}


def _gate_checks(wm, wg, locs, states, problems, rep):
    """Every gate opens >= 1 location in SOME state -- a ladder rung and any
    set of the other gate items (shared topology: the two doors chain round
    the bridge, as in vanilla) (user 2026-10-14); a rolled unlandable far
    side really has no landing; the Gulg seal is never bypassed."""
    toks = gate_tokens(wg)
    nodes = wg.site_nodes()
    pays, air = {}, {}
    for t in toks:
        rest = set(toks) - {t}
        got = False
        for st in states:
            for sub in _subsets(rest):
                on = wg.reach(wg.mask_of(set(st) | sub | {t}), air_gate=True)
                off = wg.reach(wg.mask_of(set(st) | sub), air_gate=True)
                if any(any(on[n] for n in ns) and not any(off[n] for n in ns)
                       for k, ns in nodes.items() if k in locs):
                    got = True
                    break
            if got:
                break
        pays[t] = got
        if not got:
            problems.append(f"gate {t!r} opens no location")
        # everything but this gate, airship included: does its far side open?
        full = wg.reach(wg.mask_of(set(states[-1]) | rest), air_gate=True)
        top = wg.reach(wg.mask_of(set(states[-1]) | set(toks)), air_gate=True)
        behind = [k for k, ns in nodes.items()
                  if any(top[n] for n in ns) and not any(full[n] for n in ns)]
        air[t] = not behind                  # True: the airship bypasses it
    items = {g["name"]: g.get("item") for g in wm.gates if g["name"] != "canal"}
    P = wm.gate_plan
    for g in wm.gates:
        if g["name"] == "canal":
            continue
        t = g.get("item")
        must_hold = g["name"] == "mount_gulg_key" or (
            P is not None and g.get("kind") in ("bridge", "door")
            and _far_area(wm, g) in P.unland)
        if must_hold and air.get(t, True):
            problems.append(f"gate {t!r} is bypassed by the airship")
    rep["gate_pays"] = pays
    rep["gate_items"] = items
    return air


def _far_area(wm, g):
    if g.get("kind") == "door":
        return g.get("far_area")
    b = (wm.gate_plan.bridge or {}) if wm.gate_plan else {}
    return b.get("far")


def verify(wm, weights, grid=None):
    wg = graph_of(wm, grid)
    D = wm.design
    states = world_ladder(wm)
    rg = rungs(wg, D.harbor_way, states=states)
    # with every world-changing item held: where a gated site sits once its
    # gate is open (unreachable / desert / Nerrick are judged here)
    gtoks = gate_tokens(wg)
    rgg = rungs(wg, D.harbor_way, extra=gtoks, states=states) if gtoks else rg
    problems = []
    rep = wm.report
    rep["rungs"] = {k: (LADDER[v] if v >= 0 else None) for k, v in rg.items()}
    rep["rungs_g"] = {k: (LADDER[v] if v >= 0 else None) for k, v in rgg.items()} if gtoks else {}
    locs = {k for k, n in weights.items() if n > 0}
    by = {c: [k for k, v in rg.items() if v == LADDER.index(c)] for c in LADDER}
    never = sorted(k for k, v in rgg.items() if v < 0 and k != WG.AIRSHIP_SITE)
    if never:
        problems.append(f"unreachable: {', '.join(never)}")
    # every site holding a location must exist (logic.randworld_site_rules
    # refuses a world without one: the harbor-Titan fallback once dropped
    # Titan's Tunnel and generation died in the apworld, 2026-10-23)
    missing = sorted(k for k in weights if k not in rg)
    if missing:
        problems.append(f"missing sites: {', '.join(missing)}")
    # a tiny / small world holds these loosely (user 2026-10-27): the
    # vehicles are all still there, a miss is reported in soft, not fatal
    soft = []
    miss = soft if getattr(wm, "relax_vehicles", False) else problems
    for c in ("canoe", "ship", "airship"):
        if not any(k in locs for k in by[c]) and not (c == "ship" and any(k in locs for k in by["ship+canoe"])):
            miss.append(f"{c} unlocks nothing")
    if not any(k in locs for k in by["canal"]):
        miss.append("canal opens nothing")
    rep["soft"] = soft
    if rgg.get(WG.AIRSHIP_SITE, -1) < 0 or rgg.get(WG.AIRSHIP_SITE) >= LADDER.index("airship"):
        problems.append("airship desert only reachable by airship")
    if rgg.get("duergar", -1) >= LADDER.index("canal"):
        problems.append("Nerrick (Duergar) only behind the canal he opens")
    rep["gate_air"] = _gate_checks(wm, wg, locs, states, problems, rep) if gtoks else {}
    P = getattr(wm, "gate_plan", None)
    if P is not None and P.on:
        # the rolled gates (gates.plan: 0-1 bridges, 1-2 doors) all built
        want = {d["name"] for d in P.doors}
        if P.bridge:
            want.add("lufenian_bridge")
        if P.gulg:
            want.add("mount_gulg_key")
        have = {g["name"] for g in wm.gates}
        if want - have:
            problems.append(f"gates missing: {', '.join(sorted(want - have))}")
        if P.trails and not (P.doors or P.bridge):
            problems.append("no bridge or door rolled")
        items = [g.get("item") for g in wm.gates if g["name"] != "canal"]
        if len(items) != len(set(items)) or None in items:
            problems.append(f"gate item names clash: {items}")
        rep["gates"] = P.report()
    op = int(wm.dice.get("open_progression", 0)) if hasattr(wm, "dice") else 0
    nfoot = len([k for k in by["foot"] if k != WG.AIRSHIP_SITE])
    lo, hi = FOOT_SITES.get(op, (4, 6))
    if not lo <= nfoot <= hi + 1:
        problems.append(f"foot sites {nfoot} (want {lo}-{hi})")
    if rep.get("unplaced"):
        problems.append(f"not placed: {', '.join(rep['unplaced'])}")
    # airship-proof: not reachable with only the airship (every gate item held)
    air_only = wg.reach(wg.mask_of({"Airship"} | {t for t in wg.tokens if t not in WG.BASE_TOKENS}),
                        air_gate=False)
    nodes = wg.site_nodes()
    ap = sorted(k for k, ns in nodes.items() if k != WG.AIRSHIP_SITE and not any(air_only[n] for n in ns))
    rep["airship_proof"] = ap
    if len(ap) > AIRPROOF_CAP:
        problems.append(f"{len(ap)} airship-proof sites (cap {AIRPROOF_CAP})")
    # start size (walkable tiles on foot)
    st0 = wg.reach(0)
    start_tiles = sum(wg.comp_size[n] for n in range(wg.n_land) if st0[n])
    cap = wm.dice.get("start_cap") if hasattr(wm, "dice") else None
    if cap and start_tiles > cap:
        problems.append(f"start area too open ({start_tiles} tiles, cap {cap})")
    # Titan: each mouth's foot area holds another location (the pier counts
    # on the harbor way's side)
    mouths = [s for s in wg.sites if s["key"] == "titan"]
    if len(mouths) == 2:
        for m in mouths:
            nodes = {n for n in wg.entry_nodes(m) if n < wg.n_land}
            others = [s for s in wg.sites if s["key"] not in ("titan",) and s["key"] in locs
                      and nodes & {n for n in wg.entry_nodes(s) if n < wg.n_land}]
            harbor = wg.comp[G.idx(*T.ANCHORS["harbor_land"])] in nodes
            if not others and not harbor:
                problems.append("a Titan mouth leads to no other location")
    # shares by location
    tot = {c: 0.0 for c in LADDER}
    for k, v in rg.items():
        if v >= 0:
            tot[LADDER[v]] += weights.get(k, 0)
    s = sum(tot.values()) or 1.0
    rep["shares"] = {c: round(v / s, 3) for c, v in tot.items()}
    # start size (walkable tiles on foot)
    st = wg.reach(0)
    rep["start_tiles"] = sum(wg.comp_size[n] for n in range(wg.n_land) if st[n])
    rep["intended"] = dict(wm.cats)
    rep["mismatch"] = sorted(k for k, c in wm.cats.items()
                             if rep["rungs"].get(k) not in (c, None) and k in rep["rungs"])
    rep["lost"] = list(wg.lost)
    problems += dock_problems(wm, wg)
    rep["problems"] = problems
    wm.graph = wg
    return problems


def area_rungs(wm, wg=None):
    """{area: (designed rung, rung the graph gives its biggest walk piece)}:
    the first area whose two differ is where a cascade starts."""
    wg = wg or wm.graph
    D = wm.design
    sizes = {}
    for i in range(G.NN):
        a = wm.ar.owner[i]
        c = wg.comp[i]
        if a >= 0 and c >= 0:
            sizes.setdefault(a, {}).setdefault(c, 0)
            sizes[a][c] += 1
    reach = [wg.reach(wg.mask_of(st), air_gate=True) for st in world_ladder(wm)]
    out = {}
    for a, comps in sizes.items():
        c = max(comps, key=comps.get)
        got = next((LADDER[k] for k, r in enumerate(reach) if r[c]), None)
        out[a] = (D.rung.get(a), got)
    return out
