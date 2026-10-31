"""The v2 world in the shapes its consumers read.

  random_overworld(wm, grid)  -> __init__.random_overworld (grid, att, placed,
                                 gates, start): logic + territories
  plan_json(wm, seed, shape)  -> the plan JSON the ISO baker, the menu map and
                                 the client read (v0 field names kept)
  disc_grid(wm, grid)         -> the grid as it goes on disc: canal cells hold
                                 their OPEN ids (the engine shows the closed
                                 tile until Nerrick's flag 11); world-changing
                                 gate cells hold their engine row's GATE id
                                 (the world_gates cave picks open / closed)
"""
from . import gates as GT
from . import grid as G
from . import tables as T

LADDER = ("foot", "canoe", "ship", "ship+canoe", "canal", "airship")


def disc_grid(wm, grid):
    out = list(grid)
    for g in wm.gates:
        if g["name"] == "canal":
            for x, y, o, _c in g["cells"]:
                out[G.idx(x, y)] = o
    for i, gid in GT.disc_ids(wm.gates).items():
        out[i] = gid
    return out


def _jsonable(v):
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return v


def plan_json(wm, seed, shape):
    r = wm.report
    # with every world-changing item held (rungs_g): -1 means never reachable
    # at all, not "only behind an unlandable gate" (the test bake refuses -1)
    spheres = {k: (LADDER.index(v) if v in LADDER else -1)
               for k, v in (r.get("rungs_g") or r.get("rungs") or {}).items()
               if k != "airship"}          # v0 plans list placed sites only (no desert pseudo-site)
    D = wm.design
    return _jsonable(dict(
        version=2, seed=seed, shape=shape,
        placed=wm.placed, gates=wm.gates,
        opts=dict(canal=wm.sk.canal_dir, open_edges=wm.sk.open_edges,
                  **(dict(trails=wm.gate_plan.trails, gulg=wm.gate_plan.gulg,
                          topology=wm.gate_plan.topology)
                     if wm.gate_plan is not None and wm.gate_plan.on else {})),
        harbor=dict(way=D.harbor_way),
        spheres=spheres, final_problems=list(r.get("problems", [])),
        M=["".join(wm.cls[y * G.N:(y + 1) * G.N]) for y in range(G.N)],
        shortcuts=list(getattr(wm, "shortcuts", ())),
        report={k: r[k] for k in ("shares", "targets", "style", "bridge", "tiles", "seams", "docks",
                                  "airship_proof", "start_tiles", "gates", "gate_air", "shortcuts")
                if k in r},
    ))


def random_overworld(wm, grid):
    return dict(grid=disc_grid(wm, grid), att=list(T.ATT), placed=wm.placed,
                gates=wm.gates, start=None)


def slot_world(random_overworld, plan):
    """What the client needs to bake the world into the player's ISO
    (slot_data["randworld"]["world"]): the disc grid (zlib + base64 of
    little-endian u16), the stamps (bake moves their records), the gates,
    and the plan's small fields. ~50 KB."""
    import base64
    import struct
    import zlib
    g = random_overworld["grid"]
    blob = base64.b64encode(zlib.compress(struct.pack("<%dH" % len(g), *g), 9)).decode("ascii")
    return _jsonable(dict(v=2, grid=blob, placed=plan["placed"], gates=plan["gates"],
                          opts=plan.get("opts", {}), harbor=plan.get("harbor", {}),
                          shape=plan.get("shape"), seed=str(plan.get("seed")),
                          shortcuts=plan.get("shortcuts", [])))


def grid_from_slot(world):
    """Inverse of slot_world's grid field: a flat list of N*N tile ids."""
    import base64
    import struct
    import zlib
    raw = zlib.decompress(base64.b64decode(world["grid"]))
    return list(struct.unpack("<%dH" % (len(raw) // 2), raw))
