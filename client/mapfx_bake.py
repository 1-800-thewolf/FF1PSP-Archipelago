"""The menu world map follows the world (user 2026-10-24): the canal, the
King's bridge, the Lufenian Bridge, both trail Nitros and the Mount Gulg seal
repaint the in-game "Select a location" map when they change.

The map picture is static art the game unpacks into a heap cache and textures
from on every map open (mapfog_bake). A world change is therefore a pixel
EFFECT the game applies itself: one save-byte condition + word runs holding
the OPEN and the CLOSED pixels of both GIMs (MINI512 + MINI256). The mapfog
cave (mapfog_bake.build_cave, `fx`) runs every field frame, and writes the
runs of each effect whose condition differs from what it last drew -- into
the cache, or with fog on into the fog's pristine copy, clearing the
effect's chunks from `applied` so the fog sync redraws the explored ones.
A fresh unpack is the disc picture, so the cave re-writes every effect once
after it (dirty word). Terrain only: no markers for gates, open or shut
(user call 2026-10-24).

Two painters, one effect format:
  random map (flat style, minimap_bake.render_flat): the disc picture is the
      world with every gate CLOSED (the canal too: its closed ids are on the
      disc grid). Each gate's OPEN picture is a full flat render with only
      that gate open -- the canal with its foot bridge -- diffed against it.
  vanilla map (user call: "patch the painting"): the painting never showed
      the AP trails or the Gulg seal, so their always-on BASE paint (stub
      pier, corridor, both boulders shut; the seal) is INPAINTED into all
      three MINIMAPn.PCK -- the painted synthesis (minimap_bake) with every
      pixel outside the changed cells held fixed -- and each item's opening
      is inpainted on top of that base. The King's bridge and the canal come
      from the painting's own variants (MINIMAP0 -> 1 -> 2; the engine picks
      one at unpack, the effects keep it right after a mid-session change),
      plus the canal's inpainted foot bridge.

PURE PYTHON, no shipped pixels: everything is read from the player's ISO at
bake time, like minimap_bake.
"""
import random
import struct
import time

from . import minimap_bake as M

W, H, GW = M.W, M.H, M.GW
# Story flags live at save+0x41C, flag n = bit (n & 7) of byte n >> 3
# (ff1_data.CANAL_OPEN_ADDR_SA = 0x08D1151D bit 3 is flag 11).
STORY_FLAGS_OFF = 0x41C
KINGS_BRIDGE = (STORY_FLAGS_OFF + 0, 0x08)      # story flag 3
CANAL = (STORY_FLAGS_OFF + 1, 0x08)             # story flag 11
WORLD_GATE_OFF = 0x834                          # save byte, bits 4-7 (openworld_data)
IMG256 = 0x80000000                             # run dst bit: MINI256 (else MINI512)
MAX_FX = 32                                     # one bit each in the cave's words
REC_HDR = 12 + 128                              # off/mask/len/nruns + chunk mask
MASK_PAD = 2                                    # px grown around a painted change
# inpaint tuning (painted patches)
N_RANDOM = 1            # random bucket candidates per pixel (beside continuations)
PIX_MISMATCH = 30       # cost of a neighbour pixel that does not continue
LOCAL_SLACK = 5         # local-style bucket kept within this feature cost of the best
REFINE = 2              # extra passes with every neighbour known (calms speckle)


class Effect:
    """One world change. runs = [(dst, open bytes, closed bytes)], dst =
    IMG256 | byte offset into that GIM's SWIZZLED pixels (word aligned)."""
    __slots__ = ("name", "off", "mask", "runs")

    def __init__(self, name, off, mask, runs):
        self.name, self.off, self.mask, self.runs = name, off, mask, runs

    def size(self):
        return REC_HDR + sum(8 + 2 * len(o) for _d, o, _c in self.runs)

    def __repr__(self):
        return (f"Effect({self.name!r}, save+{self.off:#x}&{self.mask:#04x}, "
                f"{len(self.runs)} runs, {self.size()} B)")


# ---- runs ----------------------------------------------------------------------
def runs_between(closed, open_, flag=0, gap=1):
    """Word runs where two equal-length swizzled pixel buffers differ.
    Changed words at most `gap` unchanged words apart share a run (a run
    header costs 8 bytes, the same as one merged word)."""
    if len(closed) != len(open_):
        raise ValueError("runs_between: buffers differ in size")
    words = []
    for b in range(0, len(closed), 128):            # one swizzle block at a time
        if closed[b:b + 128] == open_[b:b + 128]:
            continue
        words += [i for i in range(b, b + 128, 4) if closed[i:i + 4] != open_[i:i + 4]]
    out = []
    i = 0
    while i < len(words):
        j = i
        while j + 1 < len(words) and words[j + 1] - words[j] <= 4 * (gap + 1):
            j += 1
        a, b = words[i], words[j] + 4
        out.append((flag | a, bytes(open_[a:b]), bytes(closed[a:b])))
        i = j + 1
    return out


def effect(name, cond, c512, o512, c256, o256):
    """Effect from linear (unswizzled) closed/open pictures; None if equal."""
    runs = (runs_between(M.gim_swizzle(c512, W, H), M.gim_swizzle(o512, W, H))
            + runs_between(M.gim_swizzle(c256, 256, 256),
                           M.gim_swizzle(o256, 256, 256), IMG256))
    return Effect(name, cond[0], cond[1], runs) if runs else None


def chunk_of_byte(dst):
    """Fog chunk (mapfog_bake.chunk_of space) of a swizzled byte offset."""
    off = dst & 0xFFFFFF
    blk, inner = off >> 7, off & 127
    if dst & IMG256:
        by, bx = blk >> 4, blk & 15
        return by * 32 + bx * 2 + ((inner & 15) >= 8)
    by, bx = blk >> 5, blk & 31
    return (by >> 1) * 32 + bx


def touch(e):
    """u8[128] bit c = the effect writes into fog chunk c."""
    m = bytearray(128)
    for dst, op, _cl in e.runs:
        for k in range(0, len(op), 4):
            c = chunk_of_byte(dst + k)
            m[c >> 3] |= 1 << (c & 7)
    return bytes(m)


def pack(effects):
    """The cave's effect table: records back to back, each
      +0 u16 save offset  +2 u8 mask  +3 u8 0  +4 u32 record bytes
      +8 u32 nruns  +12 u8[128] fog chunks touched
      +140 runs: u32 dst, u32 nwords, u32 open[nwords], u32 closed[nwords]"""
    if len(effects) > MAX_FX:
        raise ValueError(f"mapfx: {len(effects)} effects > {MAX_FX}")
    out = bytearray()
    for e in effects:
        body = b"".join(struct.pack("<II", d, len(o) // 4) + o + c for d, o, c in e.runs)
        out += struct.pack("<HBBII", e.off, e.mask, 0, REC_HDR + len(body), len(e.runs))
        out += touch(e) + body
    return bytes(out)


def apply_reference(dec, effects, want):
    """Python twin of a fresh unpack followed by the cave's first pass: the
    decompressed PCK with every effect in `want` (bit i = effects[i] open)
    painted OPEN and the rest CLOSED."""
    from .mapfog_bake import PIX512_OFF, PIX256_OFF
    buf = bytearray(dec)
    for i, e in enumerate(effects):
        opened = want >> i & 1
        for dst, op, cl in e.runs:
            o = (PIX256_OFF if dst & IMG256 else PIX512_OFF) + (dst & 0xFFFFFF)
            v = op if opened else cl
            buf[o:o + len(v)] = v
    return bytes(buf)


def check_disjoint(effects):
    """Two effects writing one word would fight over it (the later one wins
    in every state). Returns the offending (a, b) name pairs."""
    seen, bad = {}, set()
    for e in effects:
        for dst, op, _cl in e.runs:
            for k in range(0, len(op), 4):
                prev = seen.setdefault(dst + k, e.name)
                if prev != e.name:
                    bad.add((prev, e.name))
    return sorted(bad)


# ---- pictures --------------------------------------------------------------------
def pictures(dec):
    """(pix512, pal512, pix256, pal256) of a decompressed MINIMAP PCK (linear)."""
    ent = M.pck_entries(dec)
    o5, s5 = ent["MINI512.GIM"]
    o2, s2 = ent["MINI256.GIM"]
    _w, _h, p5, q5, _o = M.gim_decode(dec[o5:o5 + s5])
    _w, _h, p2, q2, _o = M.gim_decode(dec[o2:o2 + s2])
    return bytearray(p5), q5, bytearray(p2), q2


def _rows(flat):
    return [flat[y * GW:(y + 1) * GW] for y in range(GW)]


def _cell_px(gx, gy):
    """The 4 MINI512 pixels of grid cell (gx, gy)."""
    px, py = M.gpx(gx, gy)
    return (py * W + px, py * W + px + 1, (py + 1) * W + px, (py + 1) * W + px + 1)


def _blocks256(pix):
    """MINI256 pixels covering a set of MINI512 pixels."""
    return {(p // W >> 1) * 256 + ((p % W) >> 1) for p in pix}


def _block_px(b):
    y, x = divmod(b, 256)
    i = 2 * y * W + 2 * x
    return i, i + 1, i + W, i + W + 1


# ---- random map (flat) -----------------------------------------------------------------
def _avg_key(pal512, pix, b):
    q = [pal512[pix[i]] for i in _block_px(b)]
    return tuple((sum(c[k] for c in q) + 2) >> 2 for k in range(3))


def _snap256(known, base256, pal512, pal256, open512, blocks):
    """MINI256 of `open512` over `blocks`, in the BASE picture's MINI256
    palette: the flat style's box256 hands out palette slots per picture, so
    an open state's own MINI256 would index another palette. A 2x2 average
    the base already shows reuses its entry (`known`: average -> entry, grown
    in place); any other snaps to the nearest colour the base uses."""
    cand = sorted(set(base256))
    out = bytearray(base256)
    for b in blocks:
        k = _avg_key(pal512, open512, b)
        v = known.get(k)
        if v is None:
            v = known[k] = min(cand, key=lambda j: (pal256[j][0] - k[0]) ** 2
                               + (pal256[j][1] - k[1]) ** 2 + (pal256[j][2] - k[2]) ** 2)
        out[b] = v
    return out


def random_effects(world, dec, log=None):
    """Effects for a random map painted in the flat style (the minimap_bake
    default): one per gate in world["gates"], the canal included. `dec` = the
    VANILLA decompressed MINIMAP2.PCK (render_flat takes its markers)."""
    from . import openworld_data as OW
    from . import randworld_bake as RWB
    from ..randworld import landmarks as OWD       # the random map's rows (Bahamut, Hellfire)
    say = log or (lambda _m: None)
    t = time.time()
    placed = world["placed"]
    g0 = RWB.world_grid(world, resolve="closed")
    b5, q5, b2, q2 = M.render_flat(g0, placed, dec, OWD.LANDMARKS)
    canal_stamp = next((p for p in placed if p.get("key") == "canal"), None)
    known = {}
    for b in range(256 * 256):
        known.setdefault(_avg_key(q5, b5, b), b2[b])
    out = []
    for g in world.get("gates") or ():
        cells = g.get("cells") or ()
        if not cells:
            continue
        grid = list(g0)
        for x, y, o, _c in cells:
            grid[(y % GW) * GW + x % GW] = o
        if g.get("name") == "canal":
            cond = CANAL
            if canal_stamp is not None:          # the client's foot bridge, moved
                for (x, y), tid in OW.CANAL_BRIDGE_GRID_EDITS.items():
                    mx, my = RWB._shift(canal_stamp, x, y)
                    grid[my * GW + mx] = tid
        else:
            cond = (WORLD_GATE_OFF, int(g.get("mask") or 0) & 0xFF)
            if not cond[1]:
                continue
        o5, _q, _p2, _q2 = M.render_flat(grid, placed, dec, OWD.LANDMARKS)
        changed = [i for i in range(W * H) if o5[i] != b5[i]]
        o2 = _snap256(known, b2, q5, q2, o5, _blocks256(changed))
        e = effect(g.get("item") or g.get("name") or "gate", cond, b5, o5, b2, o2)
        if e is not None:
            out.append(e)
    say(f"menu map effects (flat): {len(out)} gate(s), "
        f"{sum(e.size() for e in out)} B, {time.time() - t:.1f}s")
    return out, (bytes(b5), bytes(b2))


# ---- vanilla map (painted) ----------------------------------------------------------------
def source_valid(pix, landmarks):
    """Painting pixels the synthesis may copy: not a marker (+-3 px), not
    within 2 px of an edge (candidates read a 5x5 neighbourhood)."""
    valid = bytearray([1]) * (W * H)
    for _k, _x, _y, comp in M.marker_components(pix, landmarks, drawn=True):
        for i in comp:
            x, y = i % W, i // W
            for yy in range(max(y - 3, 0), min(y + 4, H)):
                lo, hi = yy * W + max(x - 3, 0), yy * W + min(x + 4, W)
                valid[lo:hi] = bytes(hi - lo)
    for y in range(H):
        if y < 2 or y >= H - 2:
            valid[y * W:(y + 1) * W] = bytes(W)
        else:
            valid[y * W:y * W + 2] = b"\0\0"
            valid[y * W + W - 2:(y + 1) * W] = b"\0\0"
    return valid


def marker_pixels(pix, landmarks):
    """Every pixel of the painting's own markers: never repainted."""
    out = set()
    for _k, _x, _y, comp in M.marker_components(pix, landmarks, drawn=True):
        out.update(comp)
    return out


def change_mask(cells, cls_a, cls_b, keep=(), pad=MASK_PAD):
    """Pixels to repaint: the changed cells' 2x2 blocks plus every pixel whose
    smoothed class changed, grown by `pad`, minus `keep` (markers)."""
    core = set()
    for gx, gy in cells:
        core.update(_cell_px(gx, gy))
    if core:
        ys = [p // W for p in core]
        xs = [p % W for p in core]
        y0, y1 = max(min(ys) - 8, 0), min(max(ys) + 8, H - 1)
        x0, x1 = max(min(xs) - 8, 0), min(max(xs) + 8, W - 1)
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                p = y * W + x
                if cls_a[p] != cls_b[p]:
                    core.add(p)
    out = set()
    for p in core:
        x, y = p % W, p // W
        for yy in range(max(y - pad, 0), min(y + pad + 1, H)):
            for xx in range(max(x - pad, 0), min(x + pad + 1, W)):
                out.add(yy * W + xx)
    return out - set(keep)


def inpaint(srcpix, scodes, srcvalid, cur, dcodes, mask, S0=None, seed=1, local=96, w=W):
    """Ashikhmin synthesis (minimap_bake.synthesize) over `mask` only: every
    other pixel stays as `cur` and steers the fill -- its source position
    seeds coherent continuations and its pixel value is matched on BOTH
    sides of the patch, so the strokes run on through the patch edges.
    Candidates prefer source pixels within `local` px of the patch (the
    painting's local style). `w` = the picture's side: MINI512 (default) or
    MINI256 (painted from the hand-drawn MINI256 itself, never averaged).
    -> (pixels, S = source position per pixel)."""
    W = H = w
    rng = random.Random(seed)
    s_ids = {}
    sfeat = [0] * (W * H)
    for i, c in enumerate(scodes):
        k = s_ids.get(c)
        if k is None:
            k = s_ids[c] = len(s_ids)
        sfeat[i] = k
    s_dec = [None] * len(s_ids)
    for c, k in s_ids.items():
        d = M._decode(c)
        s_dec[k] = d + (d[0] in (M.C_SEA, M.C_SHALLOW),)
    rows = {}

    def row(code):
        r = rows.get(code)
        if r is None:
            r = rows[code] = M._cost_row(M._decode(code), s_dec)
        return r

    pts = sorted(mask)
    out = bytearray(cur)
    if S0 is not None:
        S = list(S0)
    else:
        S = [p if srcvalid[p] and cur[p] == srcpix[p] else -1 for p in range(W * H)]
    if not pts:
        return out, S
    xs = [p % W for p in pts]
    ys = [p // W for p in pts]
    lx0, lx1 = min(xs) - local, max(xs) + local
    ly0, ly1 = min(ys) - local, max(ys) + local
    cb, lb = {}, {}
    for i in range(W * H):
        if srcvalid[i]:
            k = sfeat[i]
            cb.setdefault(k, []).append(i)
            if lx0 <= i % W <= lx1 and ly0 <= i // W <= ly1:
                lb.setdefault(k, []).append(i)
    cache = {}

    def bucket(code):
        # the local style wins only when it holds (nearly) as good a feature
        # match as the whole painting: a bridge must borrow the King's
        # bridge's strokes, not the nearest coast's
        b = cache.get(code)
        if b is None:
            r = row(code)
            g = min(cb, key=r.__getitem__)
            b = cb[g]
            if lb:
                k = min(lb, key=r.__getitem__)
                if r[k] <= r[g] + LOCAL_SLACK:
                    b = lb[k]
            cache[code] = b
        return b

    inmask = bytearray(W * H)
    for p in pts:
        inmask[p] = 1
    nb = [(dx, dy, dy * W + dx) for dy in range(-2, 3) for dx in range(-2, 3)
          if dx or dy]

    def fill(p, causal):
        """Pick the source pixel for `p`. causal: the raster pass (masked
        pixels ahead are unknown); else a refinement pass (every neighbour
        is known, `p`'s own pick competes again)."""
        x, y = p % W, p // W
        Dp = row(dcodes[p])
        neigh = []
        cands = {} if causal or S[p] < 0 else {S[p]: None}
        for dx, dy, o in nb:
            if not (0 <= x + dx < W and 0 <= y + dy < H):
                continue
            q = p + o
            known = not causal or not inmask[q] or dy < 0 or (dy == 0 and dx < 0)
            neigh.append((o, row(dcodes[q]), out[q] if known else -1))
            if known and S[q] >= 0:
                c = S[q] - o
                if 0 <= c < W * H and srcvalid[c]:
                    cands[c] = None
        b = bucket(dcodes[p])
        for _ in range(N_RANDOM):
            cands[b[rng.randrange(len(b))]] = None
        best, bs = None, 1 << 30
        for c in cands:
            sc = Dp[sfeat[c]] * 4
            if sc >= bs:
                continue
            for o, r, v in neigh:
                q = c + o
                sc += r[sfeat[q]]
                if v >= 0 and v != srcpix[q]:
                    sc += PIX_MISMATCH
                if sc >= bs:
                    break
            if sc < bs:
                bs, best = sc, c
        S[p] = best
        out[p] = srcpix[best]

    for p in pts:
        fill(p, True)
    for _ in range(REFINE):
        for p in pts:
            fill(p, False)
    return out, S


def small_classes(g, att):
    """MINI256's class image: ONE pixel per tile, the tile's own class
    (minimap_bake.classify), unsmoothed -- the hand-drawn small map keeps a
    3-tile trail visible where the big map's smoothed classes thin it out.
    Column u shows grid x = (u - DX) % 256 (the spare unit repeats x 254)."""
    tc = bytes(M.classify(a) for a in att)
    cols = [min((u - M.DX) % 256, GW - 1) for u in range(256)]
    return bytes(tc[g[min(y, GW - 1) * GW + x]] for y in range(256) for x in cols)


def grow256(pix, r):
    out = set()
    for p in pix:
        x, y = p % 256, p // 256
        for yy in range(max(y - r, 0), min(y + r + 1, 256)):
            for xx in range(max(x - r, 0), min(x + r + 1, 256)):
                out.add(yy * 256 + xx)
    return out


def valid256(keep2):
    """MINI256 pixels the synthesis may copy: not a marker (+-2 px), not
    within 2 px of an edge."""
    valid = bytearray([1]) * (256 * 256)
    for p in grow256(keep2, 2):
        valid[p] = 0
    for y in range(256):
        for x in range(256):
            if x < 2 or y < 2 or x >= 254 or y >= 254:
                valid[y * 256 + x] = 0
    return valid


def downsample_blocks(pix512, pal512, pal256, used256, base256, blocks):
    """minimap_bake.downsample's 2x2 average + nearest used entry, over
    `blocks` only; every other MINI256 pixel stays `base256` -- the vanilla
    MINI256 is hand-made, not a downsample, and must not move."""
    cand = [(j, pal256[j][0] * 4, pal256[j][1] * 4, pal256[j][2] * 4)
            for j in used256 if pal256[j][3]]
    rl = [c[0] for c in pal512]
    gl = [c[1] for c in pal512]
    bl = [c[2] for c in pal512]
    out = bytearray(base256)
    for blk in blocks:
        a, b, c, d = (pix512[i] for i in _block_px(blk))
        r = rl[a] + rl[b] + rl[c] + rl[d]
        g = gl[a] + gl[b] + gl[c] + gl[d]
        bb = bl[a] + bl[b] + bl[c] + bl[d]
        out[blk] = min(cand, key=lambda t: (t[1] - r) ** 2 + (t[2] - g) ** 2
                       + (t[3] - bb) ** 2)[0]
    return out


def _partition(pix, groups):
    """Each pixel -> the name of the nearest group of cells (Chebyshev, in
    MINI512 pixels), decided per 8x2-px UNIT: one unit is exactly one MINI256
    word and two MINI512 words per row, so no word of either picture ever
    belongs to two effects (the cave writes whole words). groups = [(name,
    cells)]."""
    centres = [(name, [(2 * ((gx + M.DX) % 256) + 1, 2 * gy + 1) for gx, gy in cells])
               for name, cells in groups if cells]
    units = {}
    for p in pix:
        units.setdefault((p // W >> 1, (p % W) >> 3), []).append(p)
    out = {}
    for (uy, ux), ps in units.items():
        x, y = ux * 8 + 4, uy * 2 + 1
        name = min(centres, key=lambda nc: min(max(abs(x - cx), abs(y - cy))
                                               for cx, cy in nc[1]))[0]
        for p in ps:
            out[p] = name
    return out


# ---- windowed features ----------------------------------------------------------------------
FEAT_MARGIN = 64        # px of context around a patch for its distance transform


def _nearest_other_wh(vals, w, h):
    """minimap_bake._nearest_other for a w x h image (same passes, same
    answers inside it)."""
    INF = 1 << 30
    PW = w + 2
    n = PW * (h + 2)
    vx = [0] * n
    vy = [0] * n
    d2 = [INF] * n
    for y in range(h):
        b = y * w
        pb = (y + 1) * PW + 1
        up = vals[b - w:b] if y else None
        dn = vals[b + w:b + 2 * w] if y < h - 1 else None
        row = vals[b:b + w]
        for x in range(w):
            a = row[x]
            if x < w - 1 and row[x + 1] != a:
                vx[pb + x] = 1
            elif x and row[x - 1] != a:
                vx[pb + x] = -1
            elif dn is not None and dn[x] != a:
                vy[pb + x] = 1
            elif up is not None and up[x] != a:
                vy[pb + x] = -1
            else:
                continue
            d2[pb + x] = 1
    for s in (1, -1):
        nb = [(ox * s, oy * s, (oy * PW + ox) * s)
              for ox, oy in ((-1, -1), (0, -1), (1, -1), (-1, 0))]
        first, last = PW + 1, PW * h + 1
        for pb in (range(first, last + 1, PW) if s > 0 else range(last, first - 1, -PW)):
            for i in (range(pb, pb + w) if s > 0 else range(pb + w - 1, pb - 1, -1)):
                best = d2[i]
                for ax, ay, ao in nb:
                    j = i + ao
                    if d2[j] < INF:
                        cx = vx[j] + ax
                        cy = vy[j] + ay
                        dd = cx * cx + cy * cy
                        if dd < best:
                            best = d2[i] = dd
                            vx[i] = cx
                            vy[i] = cy
    ox, oy, od = [], [], []
    for y in range(h):
        pb = (y + 1) * PW + 1
        ox += vx[pb:pb + w]
        oy += vy[pb:pb + w]
        od += d2[pb:pb + w]
    return ox, oy, od


def features_window(cls, pix, base_codes, w=W):
    """minimap_bake.features for the pixels around `pix` only: the class
    image is cut to their bounding box + FEAT_MARGIN and transformed there
    (the patches sit beside the boundaries they change, so their nearest
    boundary is inside the window). Codes outside the window are
    `base_codes`'s. `w` = the picture's side. -> full-size code list."""
    import math
    W = H = w
    xs = [p % W for p in pix]
    ys = [p // W for p in pix]
    x0, x1 = max(min(xs) - FEAT_MARGIN, 0), min(max(xs) + FEAT_MARGIN, W - 1)
    y0, y1 = max(min(ys) - FEAT_MARGIN, 0), min(max(ys) + FEAT_MARGIN, H - 1)
    w, h = x1 - x0 + 1, y1 - y0 + 1          # the window from here on
    sub = bytearray(w * h)
    for y in range(h):
        sub[y * w:(y + 1) * w] = cls[(y0 + y) * W + x0:(y0 + y) * W + x1 + 1]
    vx, vy, d2 = _nearest_other_wh(sub, w, h)
    land = bytearray(0 if c in (M.C_SEA, M.C_SHALLOW) else 1 for c in sub)
    _cx, _cy, c2 = _nearest_other_wh(land, w, h)
    out = list(base_codes)
    land_cls = set(M._LAND_CLS)
    for j in range(w * h):
        c = sub[j]
        dd = d2[j]
        if M.ECHO and dd > 196 and c in land_cls:
            e = int(math.sqrt(dd)) % M.ECHO + 1
            dd = e * e
        b = M._bin(dd)
        if b >= 6:
            dr, kind = 4, 0
        else:
            x, y = vx[j], vy[j]
            dr = (0 if x >= 0 else 1) + (0 if y >= 0 else 2)
            kind = M.GROUP[sub[j + y * w + x]]
        if land[j]:
            cd = c2[j]
            coast = 0 if cd <= 1 else 1 if cd <= 4 else 2 if cd <= 9 else 3
        else:
            coast = 0
        out[(y0 + j // w) * W + x0 + j % w] = \
            (((c * M.N_KIND + kind) * M.N_DBIN + b) * 5 + dr) * 4 + coast
    return out


def vanilla_plan(trails, gulg):
    """(closed grid edits, [(name, cond, open edits)]) for the vanilla map.
    The closed world = what the client paints with no world item held."""
    from . import openworld_data as OW
    closed = {}
    items = []
    if trails:
        closed.update(OW.TRAILS_BASE_GRID_EDITS)
        items += [
            ("Lufenian Bridge", (WORLD_GATE_OFF, OW.WORLD_GATE_BIT_LUFENIAN_BRIDGE),
             dict(OW.LUFENIAN_BRIDGE_BUILT_GRID_EDITS)),
            ("Lufenian Nitro Powder", (WORLD_GATE_OFF, OW.WORLD_GATE_BIT_LUFENIAN_NITRO),
             dict(OW.LUFENIAN_BOULDER_OPEN_GRID_EDITS)),
            ("Gaian Nitro Powder", (WORLD_GATE_OFF, OW.WORLD_GATE_BIT_GAIAN_NITRO),
             dict(OW.GAIAN_BOULDER_OPEN_GRID_EDITS)),
        ]
    if gulg:
        closed.update(OW.GULG_SEAL_GRID_EDITS)
        items.append(("Mount Gulg Key", (WORLD_GATE_OFF, OW.WORLD_GATE_BIT_MOUNT_GULG_KEY),
                      dict(OW.GULG_SEAL_CLEARED_GRID_EDITS)))
    return closed, items


BRIDGE_IDS = (353, 369, 385)    # the King's bridge deck column (0x161/0x171/0x181), which
                                # the client paints for every other bridge; attr
                                # 0x13/0x11, so the painter's classes call it land
KB_SPRITE = (307, 288, 5, 7)    # the King's bridge in MINIMAP1: x, y, w, h (px)


def bridge_runs(g, under):
    """Vertical runs of bridge-art cells that `g` adds over `under` (the
    vanilla grid already holds these ids: they are the King's bridge deck,
    which its own painting shows): [(x, y_top, y_bottom)]."""
    cells = {(i % GW, i // GW) for i, t in enumerate(g)
             if t in BRIDGE_IDS and under[i] != t}
    out = []
    for x, y in sorted(cells):
        if (x, y - 1) in cells:
            continue
        yb = y
        while (x, yb + 1) in cells:
            yb += 1
        out.append((x, y, yb))
    return out


def bridge_sprite(var0, var1):
    """The painting's King's bridge (rows of MINIMAP1 pixels): top cap,
    five deck rows, bottom cap."""
    x0, y0, w, h = KB_SPRITE
    if all(var0[(y0 + r) * W + x0 + c] == var1[(y0 + r) * W + x0 + c]
           for r in range(h) for c in range(w)):
        raise ValueError("mapfx: no King's bridge at KB_SPRITE in MINIMAP1")
    return [bytes(var1[(y0 + r) * W + x0:(y0 + r) * W + x0 + w]) for r in range(h)]


def stamp_bridges(pix, runs, sprite, keep=()):
    """The King's bridge stretched over each run: caps on the run's first
    row and one row past its last (as the painting draws it over its own
    3-cell deck), the deck rows cycled between. The sprite's left edge sits
    1 px left of the column, which puts the deck over the canal's painted
    channel. -> the pixels written."""
    top, mid, bot = sprite[0], sprite[1:-1], sprite[-1]
    done = set()
    for x, ya, yb in runs:
        px0 = 2 * ((x + M.DX) % 256) - 1
        m = 2 * (yb - ya) + 1
        if m == 1:
            lines = [mid[len(mid) // 2]]
        else:
            lines = [mid[0]] + [mid[1 + (k - 1) % (len(mid) - 2)] for k in range(1, m - 1)] \
                + [mid[-1]]
        for r, line in enumerate([top] + lines + [bot]):
            y = 2 * ya + r
            if not 0 <= y < H:
                continue
            for c, v in enumerate(line):
                p = y * W + (px0 + c) % W
                if p not in keep:
                    pix[p] = v
                    done.add(p)
    return done


def vanilla_effects(decs, van, trails, gulg, log=None, seed=1):
    """Effects + base pictures for the vanilla map.
      decs: the three VANILLA decompressed MINIMAP0/1/2.PCK
      van:  the vanilla grid (255 rows) -- the painting depicts MINIMAP2's
    -> (effects, bases): bases = [(pix512, pix256)] per variant, or None when
    nothing static changes (no trails / Gulg options)."""
    from . import openworld_data as OW
    from .. import ow_minimap_data as OWD
    from ..randworld import tables as T
    say = log or (lambda _m: None)
    t = time.time()
    v = [pictures(d) for d in decs]
    pal512, pal256 = v[2][1], v[2][3]
    if any(x[1] != pal512 or x[3] != pal256 for x in v):
        raise ValueError("mapfx: the MINIMAP variants' palettes differ")
    src, src256 = v[2][0], v[2][2]
    used256 = sorted(set(src256))
    diff01 = {i for i in range(W * H) if v[0][0][i] != v[1][0][i]}
    diff12 = {i for i in range(W * H) if v[1][0][i] != v[2][0][i]}
    keep = marker_pixels(src, OWD.LANDMARKS)
    valid = source_valid(src, OWD.LANDMARKS)
    sprite = bridge_sprite(v[0][0], v[1][0])
    vflat = [tid for row in van for tid in row]
    scls = M.class_px(van, T.ATT)
    sf, _ = M.features(scls)
    # MINI256 is hand-drawn (crisp outlines, flat fills; only 12% of it is
    # the 2x2 average of MINI512), so its patches are painted from IT, at its
    # own scale -- averaging the MINI512 patch read as mud (user 2026-10-24).
    keep2 = grow256(_blocks256(keep), 1)
    valid2 = valid256(keep2)
    scls2 = small_classes(vflat, T.ATT)
    sf2 = features_window(scls2, range(256 * 256), [0] * (256 * 256), w=256)

    def grid_with(edits, base):
        g = list(base)
        for (x, y), tid in edits.items():
            g[y * GW + x] = tid
        return g

    def under(g):
        # a bridge is drawn OVER what lies beneath it: paint that, stamp after
        return [vflat[i] if tid in BRIDGE_IDS and vflat[i] != tid else tid
                for i, tid in enumerate(g)]

    def classes(g):
        return M.class_px(_rows(under(g)), T.ATT)

    def down(pix, base, blocks):
        # bridges only: the stamped sprite, averaged (MINI256 draws none)
        return downsample_blocks(pix, pal512, pal256, used256, base, blocks - keep2)

    def paint256(cur2, g, base_codes, mask512, S0, sd):
        m2 = _blocks256(mask512) - keep2
        if not m2:
            return bytearray(cur2), S0, base_codes
        codes = features_window(small_classes(under(g), T.ATT), m2, base_codes, w=256)
        out, S2 = inpaint(src256, sf2, valid2, cur2, codes, m2, S0=S0, seed=sd,
                          local=48, w=256)
        return out, S2, codes

    closed_edits, items = vanilla_plan(trails, gulg)
    gc = grid_with(closed_edits, vflat)
    ccls, cf, cf2 = scls, sf, sf2
    base512, S = bytearray(src), None
    base256, S2 = bytearray(src256), None
    base_px = set()
    if closed_edits:
        ccls = classes(gc)
        cells = [xy for xy, tid in closed_edits.items() if vflat[xy[1] * GW + xy[0]] != tid]
        mask = change_mask(cells, scls, ccls, keep)
        if mask:
            cf = features_window(ccls, mask, sf)
            base512, S = inpaint(src, sf, valid, src, cf, mask, seed=seed)
            base256, S2, cf2 = paint256(src256, gc, sf2, mask, None, seed)
        stamped = stamp_bridges(base512, bridge_runs(gc, vflat), sprite, keep)
        base256 = down(base512, base256, _blocks256(stamped))
        base_px = mask | stamped
        if base_px & (diff01 | diff12):
            raise ValueError("mapfx: the trails/Gulg base paint touches the "
                             "King's bridge or the canal")
    bases = None
    if base_px:
        bases = []
        for p5, _q5, p2, _q2 in v:
            b5, b2 = bytearray(p5), bytearray(p2)
            for p in base_px:
                b5[p] = base512[p]
            for b in _blocks256(base_px):
                b2[b] = base256[b]
            bases.append((bytes(b5), bytes(b2)))
    # every item open at once, plus the canal's foot bridge (the client
    # paints it while flag 11 is set); the items sit far apart, so each
    # effect takes its own share of the one synthesis
    open_edits = {}
    groups = []
    for name, _cond, edits in items:
        open_edits.update(edits)
        groups.append((name, [xy for xy, tid in edits.items()
                              if gc[xy[1] * GW + xy[0]] != tid]))
    bridge = dict(OW.CANAL_BRIDGE_GRID_EDITS)
    open_edits.update(bridge)
    groups.append(("canal", list(bridge)))
    go = grid_with(open_edits, gc)
    ocls = classes(go)
    cells = [xy for xy, tid in open_edits.items() if gc[xy[1] * GW + xy[0]] != tid]
    mask_o = change_mask(cells, ccls, ocls, keep)
    open512, open256 = bytearray(base512), bytearray(base256)
    if mask_o:
        of = features_window(ocls, mask_o, cf)
        open512, _S = inpaint(src, sf, valid, base512, of, mask_o, S0=S, seed=seed + 1)
        open256, _S2, _c = paint256(base256, go, cf2, mask_o, S2, seed + 1)
    stamped = stamp_bridges(open512, bridge_runs(go, vflat), sprite, keep)
    open256 = down(open512, open256, _blocks256(stamped))
    open_px = mask_o | stamped
    owner = _partition(open_px, groups)
    effects = []
    for name, cond, _edits in items:
        part = {p for p, n in owner.items() if n == name}
        if not part:
            continue
        o5 = bytearray(base512)
        if name == "Mount Gulg Key":
            # open = the painting itself: the cleared seal IS vanilla Gulg
            o2 = bytearray(base256)
            for p in part:
                o5[p] = src[p]
            for b in _blocks256(part):
                o2[b] = src256[b]
        else:
            o2 = bytearray(base256)
            for p in part:
                o5[p] = open512[p]
            for b in _blocks256(part):
                o2[b] = open256[b]
        e = effect(name, cond, base512, o5, base256, o2)
        if e is not None:
            effects.append(e)
    # the canal: MINIMAP1's pixels shut, MINIMAP2's + the foot bridge open
    part = {p for p, n in owner.items() if n == "canal"}
    region = diff12 | part
    c5, o5 = bytearray(base512), bytearray(base512)
    c2, o2 = bytearray(base256), bytearray(base256)
    for p in region:
        c5[p] = v[1][0][p]
        o5[p] = open512[p] if p in part else v[2][0][p]
    for b in _blocks256(region):
        c2[b] = v[1][2][b]
        o2[b] = open256[b] if b in _blocks256(part) else v[2][2][b]
    e = effect("canal", CANAL, c5, o5, c2, o2)
    if e is not None:
        effects.append(e)
    # the King's bridge: MINIMAP0 -> MINIMAP1
    e = effect("King's bridge", KINGS_BRIDGE, v[0][0], v[1][0], v[0][2], v[1][2])
    if e is not None:
        effects.append(e)
    bad = check_disjoint(effects)
    if bad:
        raise ValueError(f"mapfx: overlapping effects {bad}")
    say(f"menu map effects (painted): {len(effects)} effect(s), "
        f"{sum(e.size() for e in effects)} B, base {'patched' if bases else 'vanilla'}, "
        f"{time.time() - t:.1f}s")
    return effects, bases


# ---- bake ---------------------------------------------------------------------------------
class Plan:
    """What patch_iso needs: the cave's table (+ count) for mapfog_bake, and
    for a vanilla map the base pictures to write into MINIMAP0/1/2
    ({name: (compressed, decompressed length)}; empty = disc stays as is)."""
    def __init__(self, effects, pcks=None):
        self.effects = effects
        self.table = pack(effects)
        self.pcks = pcks or {}


def read_variants(iso):
    """The three vanilla decompressed MINIMAPn.PCK."""
    with open(iso, "rb") as f:
        dpk_off, recs = M._dpk_index(f)
        return [M._read_record(f, dpk_off, recs, n) for n in M.MINIMAP_PCKS]


def build(feats, iso_in, log=print):
    """Effects for this bake, or None when the menu map has nothing to follow.
    Random map: only in the flat style (its disc picture is render_flat's).
    Vanilla map: the canal / King's bridge always, the trails / Gulg seal
    with their options (feats["_world_gates"])."""
    from . import wp16
    rw = feats.get("_rw_world")
    if rw:
        if M.DEFAULT_STYLE != M.STYLE_FLAT:
            return None
        dec, _van = M.read_vanilla(iso_in)
        effects, _closed = random_effects(rw, dec, log=log)
        for a, b in check_disjoint(effects):
            log(f"[mapfx] menu-map effects {a!r} and {b!r} share pixels "
                f"(gates side by side); each state draws the later one there")
        return Plan(effects) if effects else None
    wg = feats.get("_world_gates") or {}
    decs = read_variants(iso_in)
    _dec, van = M.read_vanilla(iso_in)
    try:
        effects, bases = vanilla_effects(decs, van, bool(wg.get("trails")),
                                         bool(wg.get("gulg")), log=log)
    except ValueError as e:
        # the base paint failed (or does not fit): keep the painting as is
        # and follow only what the variants already hold
        log(f"[mapfx] trails/Gulg menu-map paint skipped ({e}); canal and "
            f"King's bridge only")
        effects, bases = vanilla_effects(decs, van, False, False, log=log)
    pcks = {}
    if bases:
        with open(iso_in, "rb") as f:
            _off, recs = M._dpk_index(f)
        for name, dec, (b5, b2) in zip(M.MINIMAP_PCKS, decs, bases):
            new = M.build_pck(dec, b5, b2)
            comp = wp16.compress(new)
            if len(comp) > recs[name][2]:
                log(f"[mapfx] {name}: patched painting {len(comp)} B > slot "
                    f"{recs[name][2]} B; canal and King's bridge only")
                effects, _b = vanilla_effects(decs, van, False, False, log=log)
                pcks = {}
                break
            pcks[name] = (comp, len(new))
    return Plan(effects, pcks) if effects else None


def write_bases(iso_out, plan, log=print):
    """Dpk phase: the vanilla map's patched painting into each MINIMAPn.PCK
    (each variant keeps its own King's bridge / canal pixels)."""
    for name, (comp, n) in plan.pcks.items():
        M.write_minimaps(iso_out, comp, n, names=(name,))
    if plan.pcks:
        log(f"menu map: trails/Gulg painted into {len(plan.pcks)} MINIMAP variant(s)")
