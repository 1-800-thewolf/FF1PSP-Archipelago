"""Repaint the menu world map (MINIMAP0/1/2.PCK) for a generated overworld.

The menu "Select a location" map is a static hand-painted picture, not a view
of MAP_00 (memory: world-map-screen-minimap). Each MINIMAPn.PCK holds
MINI512.GIM (512x512 idx8, swizzled 16x8) + MINI256.GIM (256x256 idx8, own
palette), and the town/cave/dock markers are painted INTO it. Grid cell
(gx, gy) <-> MINI512 pixels x = 2*((gx+9) % 256), y = 2*gy (2x2 per tile);
MINI256 is 1 px per tile. On a random overworld the vanilla picture misleads
the player (the cursor points already move -- randworld_bake), so the bake
paints a new one, in one of three STYLES:

  flat      (DEFAULT, user call 2026-10-23 after mockups A-E) the client Map
            tab's terrain colours (ow_minimap_data.PALETTE, two dithered shades
            per class) at 2x2 px per tile, a dark coast line on every land side
            touching open water (ow_map.coast_mask -- the Map tab draws the same
            line), the game's own painted markers moved as below. Both GIM
            palettes are rewritten: MINI512 keeps only the marker and fog
            indices, MINI256 is an exact 2x2 box filter of MINI512 in its 200
            unused palette slots. ~30 KB packed, ~2 s.
  painted   the v336 texture synthesis described next. Reads poorly (brown
            contour bands over plains), kept for a future yaml option.
  flat_sea  flat land (no coast line) on the painted sea. Kept for a future
            yaml option.
No player-facing switch picks the style today.

The painted style is the production port of the prototype
attic/ow_rando_v0/mapscreen.py (LIVE-VERIFIED
2026-10-14 on archipelago_1). Same method, texture-by-numbers: every pixel of
the vanilla painting (variant 2 = bridge + canal, matching the static AMD) and
of the target gets a feature code -- terrain class + distance bin + direction
to the nearest boundary -- and the target is filled in raster order by copying
vanilla pixels whose neighbourhood features match, preferring CONTINUATIONS of
the source pixels its filled neighbours copied (Ashikhmin 2001), so long runs
of real brushwork carry over. Open sea is a plate of horizontal runs of
vanilla deep sea, re-shaded by distance to land in the NEW world. Markers are
masked out of the source and moved with their stamp (src window -> dst).

PURE PYTHON, and NO SHIPPED PIXELS: the frozen Archipelago client has no
numpy/PIL, and the painting is Square Enix art, so everything is read from
the player's own ISO at bake time (painting, vanilla grid) plus the package's
tile attributes and landmark list. Painted: 12-18 s on a laptop i7 (2026-10-17).

Differences from the prototype (output is close, not bit-identical):
  - speed: uniform-window fast path in the class field, padded distance
    transforms, a pruned nearest-colour search cached across FIT_LEVELS, a
    per-pixel neighbour table in the synthesis (~2.5x faster);
  - a non-dock marker moves with ONE owner (randworld_bake.owner, the same
    rule the entrance records use), so overlapping stamp windows (Ice /
    Whisperwind) can no longer stamp a ghost copy, and it moves to the cell
    its landmark lands on, so a stamp wrapped past the grid edge keeps it;
  - dpk I/O touches only the three records, never the whole archive.
"""
import math
import random
import struct
import time
from operator import add, sub

W = H = 512                       # MINI512
GW = 255                          # overworld grid
DX = 9                            # painting x-roll vs grid (fit 2026-10-13)
MINIMAP_PCKS = ("MINIMAP0.PCK", "MINIMAP1.PCK", "MINIMAP2.PCK")   # vanilla / +bridge / +canal
SOURCE_PCK = "MINIMAP2.PCK"       # the painting the static AMD matches
# In-place decode lead cap (wp16.inplace_lead): vanilla MINIMAPn lead
# 24,522..24,568 B and loads; the game left a 42,096 B gap for it (live hang
# 2026-10-24). Below vanilla, so ours never asks more of that buffer.
INPLACE_MAX_LEAD = 24_000

STYLE_FLAT, STYLE_PAINTED, STYLE_FLAT_SEA = "flat", "painted", "flat_sea"
STYLES = (STYLE_FLAT, STYLE_PAINTED, STYLE_FLAT_SEA)
DEFAULT_STYLE = STYLE_FLAT

# ---- terrain classes (merged for painting) -----------------------------------
C_SEA, C_SHALLOW, C_RIVER, C_MARSH, C_PLAIN, C_FOREST, C_DESERT, C_GRASS2, \
    C_MOUNTAIN, C_BRIDGE = range(10)
N_CLS = 10
_LAND_CLS = (C_MARSH, C_PLAIN, C_FOREST, C_DESERT, C_GRASS2, C_MOUNTAIN)


def classify(attr):
    lo = attr & 0x0FFF
    if attr == 0xF00F:
        return C_SEA
    if attr == 0xF002:
        return C_SHALLOW
    if attr == 0xF009:
        return C_RIVER
    if attr == 0x0010:
        return C_MARSH
    if attr == 0x0006:
        return C_FOREST
    if lo == 0x000E:
        return C_DESERT
    if attr == 0x000C:
        return C_GRASS2
    if attr in (0xF012, 0xF011):
        return C_MOUNTAIN
    if attr == 0xF001:
        return C_BRIDGE
    return C_PLAIN                # plain/dead earth/towns/caves/docks


# ---- GIM (idx8, swizzled 16x8) -------------------------------------------------
def _gim_blocks(b):
    out = []

    def walk(p, end):
        while p < end:
            bid, _u, size, _nxt, dat = struct.unpack_from("<HHIII", b, p)
            out.append((bid, p, size, dat))
            if bid in (2, 3):
                walk(p + dat, p + size)
            if not size:
                break
            p += size
    walk(16, len(b))
    return out


def _gim_image(b, p, dat):
    h = p + dat
    fmt, order, w, hh = struct.unpack_from("<HHHH", b, h + 4)
    start = struct.unpack_from("<I", b, h + 0x1C)[0]
    return h + start, fmt, order, w, hh


def gim_decode(b):
    """-> (w, h, pixels bytearray row-major, palette list of (r,g,b,a), pix_off)"""
    img = pal = None
    for bid, p, _size, dat in _gim_blocks(b):
        if bid == 4:
            img = _gim_image(b, p, dat)
        elif bid == 5:
            pal = _gim_image(b, p, dat)
    if img is None or pal is None:
        raise ValueError("GIM: image or palette block missing")
    off, fmt, order, w, h = img
    if (fmt, order) != (5, 1) or w % 16 or h % 8:
        raise ValueError(f"GIM: expected swizzled idx8, got fmt {fmt} order {order} {w}x{h}")
    raw = b[off:off + w * h]
    pix = bytearray(w * h)
    bw = w // 16
    for by in range(h // 8):
        for bx in range(bw):
            src = (by * bw + bx) * 128
            for r in range(8):
                d = (by * 8 + r) * w + bx * 16
                pix[d:d + 16] = raw[src + r * 16:src + r * 16 + 16]
    poff, _f, _o, pw, _ph = pal
    P = [tuple(b[poff + 4 * i:poff + 4 * i + 4]) for i in range(pw)]
    return w, h, pix, P, off


def gim_palette(b):
    """-> (offset of the RGBA8888 palette data inside GIM bytes `b`, entries)."""
    for bid, p, _size, dat in _gim_blocks(b):
        if bid == 5:
            off, fmt, _order, w, _h = _gim_image(b, p, dat)
            if fmt != 3:
                raise ValueError(f"GIM: palette format {fmt}, expected RGBA8888 (3)")
            return off, w
    raise ValueError("GIM: palette block missing")


def gim_swizzle(pix, w, h):
    out = bytearray(w * h)
    bw = w // 16
    for by in range(h // 8):
        for bx in range(bw):
            dst = (by * bw + bx) * 128
            for r in range(8):
                s = (by * 8 + r) * w + bx * 16
                out[dst + r * 16:dst + r * 16 + 16] = pix[s:s + 16]
    return out


def pck_entries(dec):
    cnt = struct.unpack_from("<I", dec, 0)[0]
    out = {}
    for i in range(cnt):
        e = dec[0x10 + i * 0x24:0x10 + (i + 1) * 0x24]
        nm = e[:16].split(b"\0")[0].decode("latin1")
        _a, _b, off, size, _s2 = struct.unpack_from("<IIIII", e, 16)
        out[nm] = (off, size)
    return out


# ---- per-pixel class field (smooth) ------------------------------------------------
# Each pixel samples the class weights of the grid cells around it with a
# Gaussian (SIGMA tiles). That rounds the 2x2 tile stairs into curves the way
# the painter drew coasts, and drops tile-scale noise (a lone forest tile in a
# plain) the way the painter abstracted it. Thin features survive on purpose:
# a pixel keeps its own cell's land/water group while that group holds
# KEEP_OWN of the weight (1-tile straits and isthmuses are real geography),
# and rivers/bridges paint wherever they hold LINE_T (lower than half, so a
# 1-tile river becomes a smooth ~3 px band instead of vanishing).
SIGMA = 0.75
KEEP_OWN = 0.40
SIGMA_LINE = 1.05                 # rivers/bridges: wider kernel rounds their tile stairs
LINE_T = 0.17
_KS = (-2, -1, 0, 1, 2)           # both kernels keep all five taps (w > 0.01)


def _phase_w(sigma, ph):
    u = (ph - 0.5) / 2.0            # pixel centre vs its cell centre, tiles
    out = []
    for k in _KS:
        d = k - u
        w = math.exp(-d * d / (2 * sigma * sigma))
        out.append(w if w > 0.01 else 0.0)
    return out


def _phase_tables():
    """[py][px] -> (taps [(row 0..4, col offset, w, line w)], sum w, sum line w)."""
    tabs = []
    for py in (0, 1):
        row = []
        for px in (0, 1):
            wy, wx = _phase_w(SIGMA, py), _phase_w(SIGMA, px)
            ly, lx = _phase_w(SIGMA_LINE, py), _phase_w(SIGMA_LINE, px)
            taps = [(a, _KS[b], wy[a] * wx[b], ly[a] * lx[b])
                    for a in range(5) for b in range(5)]
            row.append((taps, sum(t[2] for t in taps), sum(t[3] for t in taps)))
        tabs.append(row)
    return tabs


def class_px(grid, att):
    """grid: 255 rows of 255 tile ids -> 512*512 bytearray of (smoothed) classes.
    Every pixel reads the 5x5 cells around its own; where those all share one
    class the answer is that class (each branch below returns `own` then), so
    only boundary pixels pay for the weighting."""
    tc = bytes(classify(a) for a in att)
    colmap = [min((bx - DX) % 256, GW - 1) for bx in range(256)]
    P = [bytes(tc[row[c]] for c in colmap) for row in grid]   # GW rows x 256 painting columns
    rows5 = [[P[min(max(gy + k, 0), GW - 1)] for k in _KS] for gy in range(GW)]
    # uniform 5x5 windows -> class, else 255
    H5 = []
    for r in P:
        e = r[-2:] + r + r[:2]
        H5.append(bytes(v if e[i] == v and e[i + 1] == v and e[i + 3] == v and e[i + 4] == v
                        else 255 for i, v in enumerate(e[2:258])))
    U = []
    for gy in range(GW):
        hs = [H5[min(max(gy + k, 0), GW - 1)] for k in _KS]
        U.append(bytes(v if v != 255 and a == v and b == v and d == v and e == v else 255
                       for a, b, v, d, e in zip(*hs)))
    tabs = _phase_tables()
    out = bytearray(W * H)
    for y in range(H):
        gy = min(y >> 1, GW - 1)
        urow = U[gy]
        rws = rows5[gy]
        own_row = rws[2]
        line = bytearray(W)
        line[0::2] = urow
        line[1::2] = urow
        ptab = tabs[y & 1]
        bx = urow.find(255)
        while bx >= 0:
            own = own_row[bx]
            for px in (0, 1):
                taps, s, ls = ptab[px]
                tot = [0.0] * N_CLS
                lr = lb = 0.0
                for a, k, w, lw in taps:
                    t = rws[a][(bx + k) & 255]
                    tot[t] += w
                    if t == C_RIVER:
                        lr += lw
                    elif t == C_BRIDGE:
                        lb += lw
                if lb >= LINE_T * ls:
                    c = C_BRIDGE
                elif lr >= LINE_T * ls:
                    c = C_RIVER
                else:
                    water = tot[C_SEA] + tot[C_SHALLOW]
                    land = s - water - tot[C_RIVER] - tot[C_BRIDGE]
                    if own in (C_SEA, C_SHALLOW):
                        wet = water >= KEEP_OWN * s or water >= land
                    elif own in (C_RIVER, C_BRIDGE):
                        wet = water > land
                    else:
                        wet = not (land >= KEEP_OWN * s or land >= water)
                    if wet:
                        c = C_SEA if tot[C_SEA] >= tot[C_SHALLOW] else C_SHALLOW
                    else:
                        c = C_PLAIN
                        best = -1.0
                        for k2 in _LAND_CLS:
                            if tot[k2] > best:
                                best, c = tot[k2], k2
                line[2 * bx + px] = c
            bx = urow.find(255, bx + 1)
        out[y * W:(y + 1) * W] = line
    return out


def _nearest_other(vals):
    """Vector propagation DT: for every pixel, offset (dx,dy) to the nearest
    pixel whose value differs. Two raster passes, 8-neighbour (approx).
    Runs on a 1-px padded copy (border = never reached) so the passes need no
    bounds checks."""
    INF = 1 << 30
    PW = W + 2
    n = PW * (H + 2)
    vx = [0] * n
    vy = [0] * n
    d2 = [INF] * n
    for y in range(H):
        b = y * W
        pb = (y + 1) * PW + 1
        up = vals[b - W:b] if y else None
        dn = vals[b + W:b + 2 * W] if y < H - 1 else None
        row = vals[b:b + W]
        for x in range(W):
            a = row[x]
            if x < W - 1 and row[x + 1] != a:
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
    # forward: (-1,-1) (0,-1) (1,-1) (-1,0); backward: mirrored (s = -1)
    for s in (1, -1):
        nb = [(ox * s, oy * s, (oy * PW + ox) * s)
              for ox, oy in ((-1, -1), (0, -1), (1, -1), (-1, 0))]
        (ax, ay, ao), (bx_, by_, bo), (cx_, cy_, co), (ex, ey, eo) = nb
        first, last = PW + 1, PW * H + 1           # row starts, y = 0 and y = H - 1
        for pb in (range(first, last + 1, PW) if s > 0 else range(last, first - 1, -PW)):
            for i in (range(pb, pb + W) if s > 0 else range(pb + W - 1, pb - 1, -1)):
                best = d2[i]
                # four unrolled neighbour tests (padding holds INF)
                j = i + ao
                if d2[j] < INF:
                    cx = vx[j] + ax
                    cy = vy[j] + ay
                    dd = cx * cx + cy * cy
                    if dd < best:
                        best = d2[i] = dd
                        vx[i] = cx
                        vy[i] = cy
                j = i + bo
                if d2[j] < INF:
                    cx = vx[j] + bx_
                    cy = vy[j] + by_
                    dd = cx * cx + cy * cy
                    if dd < best:
                        best = d2[i] = dd
                        vx[i] = cx
                        vy[i] = cy
                j = i + co
                if d2[j] < INF:
                    cx = vx[j] + cx_
                    cy = vy[j] + cy_
                    dd = cx * cx + cy * cy
                    if dd < best:
                        best = d2[i] = dd
                        vx[i] = cx
                        vy[i] = cy
                j = i + eo
                if d2[j] < INF:
                    cx = vx[j] + ex
                    cy = vy[j] + ey
                    dd = cx * cx + cy * cy
                    if dd < best:
                        d2[i] = dd
                        vx[i] = cx
                        vy[i] = cy
    ox, oy, od = [], [], []
    for y in range(H):
        pb = (y + 1) * PW + 1
        ox += vx[pb:pb + W]
        oy += vy[pb:pb + W]
        od += d2[pb:pb + W]
    return ox, oy, od


# ---- features ----------------------------------------------------------------------
# code = ((((cls*6 + kind)*10 + dbin)*5 + dir)*4 + coast)
#   kind  = group of the class across the NEAREST boundary of any kind
#           (water / river / mountain / green / desert / bridge) -- the painter
#           draws a different edge for each: dark coast line, cream river bank,
#           cream mountain rim ...
#   dbin  = distance to that boundary, 10 bins
#   dir   = quadrant toward it (4 = far, ignored)
#   coast = land pixels only: distance to the sea, 4 bins (a forest strip on a
#           coast still needs the coast outline even when the plain is nearer)
GROUP = (0, 0, 1, 3, 3, 3, 4, 3, 2, 5)        # indexed by class
N_KIND = 6
_DB2 = (1, 4, 9, 16, 36, 81, 196, 400, 625)   # d^2 -> bins: 1,2,3,4,6,9,14,20,25,>25 px
N_DBIN = 10
CODE_PER_CLS = N_KIND * N_DBIN * 5 * 4
ECHO = 12                                     # land interiors: repeat rim distances every ECHO px


def _bin(dd):
    b = 0
    while b < N_DBIN - 1 and dd > _DB2[b]:
        b += 1
    return b


def features(cls):
    vx, vy, d2 = _nearest_other(cls)
    land = bytearray(0 if c in (C_SEA, C_SHALLOW) else 1 for c in cls)
    cx, cy, c2 = _nearest_other(land)
    out = [0] * (W * H)
    land_cls = set(_LAND_CLS)
    for i in range(W * H):
        c = cls[i]
        dd = d2[i]
        if ECHO and dd > 196 and c in land_cls:
            e = int(math.sqrt(dd)) % ECHO + 1           # contour echo: rims recur inward
            dd = e * e
        b = _bin(dd)
        if b >= 6:
            dr, kind = 4, 0
        else:
            x, y = vx[i], vy[i]
            dr = (0 if x >= 0 else 1) + (0 if y >= 0 else 2)
            kind = GROUP[cls[i + y * W + x]]
        if land[i]:
            cd = c2[i]
            coast = 0 if cd <= 1 else 1 if cd <= 4 else 2 if cd <= 9 else 3
        else:
            coast = 0
        out[i] = (((c * N_KIND + kind) * N_DBIN + b) * 5 + dr) * 4 + coast
    return out, (cx, cy, c2)


def _decode(code):
    code, coast = divmod(code, 4)
    code, dr = divmod(code, 5)
    code, b = divmod(code, N_DBIN)
    c, kind = divmod(code, N_KIND)
    return c, kind, b, dr, coast


def _cost_row(a, s_dec):
    """Feature distance from target code `a` to every source code (<= 255)."""
    ca, ka, ba, da, sa = a
    wa = ca in (C_SEA, C_SHALLOW)
    ga = GROUP[ca]
    out = bytearray(len(s_dec))
    for j, (cb, kb, bb, db, sb, wb) in enumerate(s_dec):
        v = 0
        if ca != cb:
            v = 90 if wa != wb else (30 if ga == GROUP[cb] else 50)
        v += 5 * abs(ba - bb) + 4 * abs(sa - sb)
        if ka != kb and ba < 6 and bb < 6:
            v += 14 if min(ba, bb) <= 2 else 6
        if da != db and da != 4 and db != 4:
            v += 4
        out[j] = v if v < 255 else 255
    return out


def is_far_sea(code):
    return code // CODE_PER_CLS == C_SEA


def components(cls):
    """4-connected same-class regions -> (comp id per pixel, [(cls, area)])."""
    comp = [-1] * (W * H)
    info = []
    for s in range(W * H):
        if comp[s] >= 0:
            continue
        k = len(info)
        c = cls[s]
        comp[s] = k
        st = [s]
        n = 0
        while st:
            i = st.pop()
            n += 1
            x = i % W
            for j in ((i - 1) if x else -1, (i + 1) if x < W - 1 else -1, i - W, i + W):
                if 0 <= j < W * H and comp[j] < 0 and cls[j] == c:
                    comp[j] = k
                    st.append(j)
        info.append((c, n))
    return comp, info


def region_stats(pix, pal, comp, n_regions, valid):
    """Per source region: texture (mean |luma step| right/below -- flat
    regions like the pale plateau round the NE desert score low) and mean RGB
    (to reject class outliers like Gulg's dark volcano slopes)."""
    luma = [(c[0] * 3 + c[1] * 6 + c[2]) // 10 for c in pal]
    tot = [0] * n_regions
    cnt = [0] * n_regions
    rgb = [[0, 0, 0, 0] for _ in range(n_regions)]
    for i in range(W * H):
        if not valid[i]:
            continue
        r = comp[i]
        c = pal[pix[i]]
        a = rgb[r]
        a[0] += c[0]
        a[1] += c[1]
        a[2] += c[2]
        a[3] += 1
        if i < W * (H - 1) and (i % W) < W - 1 and comp[i + 1] == r and comp[i + W] == r:
            lu = luma[pix[i]]
            tot[r] += abs(lu - luma[pix[i + 1]]) + abs(lu - luma[pix[i + W]])
            cnt[r] += 1
    tex = [tot[k] / cnt[k] if cnt[k] else 0.0 for k in range(n_regions)]
    mean = [(a[0] / a[3], a[1] / a[3], a[2] / a[3]) if a[3] else (0, 0, 0) for a in rgb]
    return tex, mean


def assign_regions(src_info, src_valid_area, src_tex, src_mean, dst_info, seed):
    """Each target region copies one vanilla region of the same class -- so a
    mountain range is painted in ONE consistent style, like the painter's,
    instead of a mottle of every range. Score: close in (log) area, textured,
    and close to the class's typical colour; pick among the best 3."""
    rng = random.Random(seed * 7 + 3)
    by_cls = {}
    for k, (c, _n) in enumerate(src_info):
        if src_valid_area[k] >= 24:
            by_cls.setdefault(c, []).append(k)
    typical = {}
    for c, ks in by_cls.items():
        tw = sum(src_valid_area[k] for k in ks)
        typical[c] = tuple(sum(src_mean[k][j] * src_valid_area[k] for k in ks) / tw for j in range(3))

    def score(k, c, n):
        m, t = src_mean[k], typical[c]
        cd = math.sqrt(sum((m[j] - t[j]) ** 2 for j in range(3)))
        return abs(math.log(src_valid_area[k] / max(n, 1))) - 0.15 * src_tex[k] + 0.03 * cd

    out = []
    for c, n in dst_info:
        cand = by_cls.get(c)
        if not cand or c in (C_SEA, C_SHALLOW):
            out.append(-1)
            continue
        cand = sorted(cand, key=lambda k: (score(k, c, n), k))[:3]
        out.append(cand[rng.randrange(len(cand))])
    return out


REGION_PEN = 24


# ---- sea plate -------------------------------------------------------------------
DEEP = 25                         # px: vanilla sea tone is flat beyond this


def sea_plate(pix, cls, coast_d2, valid, seed=1):
    """Full-size open-sea TEXTURE (tone_sea removes its low frequencies and
    re-shades it by distance to land). The painted sea is horizontal streaks
    with little vertical structure, so every output row is built from random
    horizontal runs (48-160 px) of vanilla DEEP sea (>= DEEP px from land),
    taken from rows near the same height. Cuts land at different x on every
    row, so no seam lines up -- 2D chunk copies traced the outlines of the
    holes they filled (scallops)."""
    import bisect
    rng = random.Random(seed * 31 + 5)
    n2 = DEEP * DEEP
    runs = []                                       # (y, x0, x1) deep-sea runs >= 32 px
    for y in range(H):
        b0 = y * W
        x = 0
        while x < W:
            i = b0 + x
            if valid[i] and cls[i] == C_SEA and coast_d2[i] >= n2:
                x0 = x
                while x < W and valid[b0 + x] and cls[b0 + x] == C_SEA and coast_d2[b0 + x] >= n2:
                    x += 1
                if x - x0 >= 32:
                    runs.append((y, x0, x))
            else:
                x += 1
    if not runs:
        raise ValueError("MINIMAP: no open-sea runs in the source painting")
    by_y = sorted(runs)
    ys = [r[0] for r in by_y]
    plate = bytearray(W * H)
    for y in range(H):
        lo = bisect.bisect_left(ys, y - 48)
        hi = bisect.bisect_right(ys, y + 48)
        pool = by_y[lo:hi] or by_y
        x = 0
        while x < W:
            ry, x0, x1 = pool[rng.randrange(len(pool))]
            L = min(rng.randrange(48, 161), x1 - x0, W - x)
            st = x0 + rng.randrange(0, x1 - x0 - L + 1)
            plate[y * W + x:y * W + x + L] = pix[ry * W + st:ry * W + st + L]
            x += L
    return plate


# ---- synthesis -------------------------------------------------------------------
_R = 2
_CAUSAL = [(dx, dy) for dy in range(-_R, 1) for dx in range(-_R, _R + 1) if dy < 0 or dx < 0]
_AHEAD = [(dx, dy) for dy in range(0, _R + 1) for dx in range(-_R, _R + 1) if dy > 0 or dx > 0]


def synthesize(srcpix, srccode, srcvalid, srccomp, dstcode, dstcomp, assign,
               seed=1, plate=None):
    rng = random.Random(seed)
    # dense code spaces: source codes as columns, target codes as rows
    s_ids = {}
    sfeat = [0] * (W * H)
    for i, c in enumerate(srccode):
        k = s_ids.get(c)
        if k is None:
            k = s_ids[c] = len(s_ids)
        sfeat[i] = k
    s_dec = [None] * len(s_ids)
    for c, k in s_ids.items():
        d = _decode(c)
        s_dec[k] = d + (d[0] in (C_SEA, C_SHALLOW),)
    t_ids = {}
    tfeat = [0] * (W * H)
    for i, c in enumerate(dstcode):
        k = t_ids.get(c)
        if k is None:
            k = t_ids[c] = len(t_ids)
        tfeat[i] = k
    D = [None] * len(t_ids)
    for c, k in t_ids.items():
        D[k] = _cost_row(_decode(c), s_dec)
    t_same = [-1] * len(t_ids)                     # target code -> identical source code
    for c, k in t_ids.items():
        t_same[k] = s_ids.get(c, -1)

    # buckets: (source region, code) and code-only
    rb = {}
    cb = {}
    region_codes = {}
    for i in range(W * H):
        if srcvalid[i]:
            k = sfeat[i]
            r = srccomp[i]
            rb.setdefault((r, k), []).append(i)
            cb.setdefault(k, []).append(i)
            region_codes.setdefault(r, {})[k] = None
    cache = {}

    def bucket(tk, region):
        key = (tk, region)
        b = cache.get(key)
        if b is None:
            row = D[tk]
            codes = region_codes.get(region)
            if codes:
                best = min(codes, key=row.__getitem__)
                if row[best] <= 20:
                    b = rb[(region, best)]
            if b is None:
                best = min(cb, key=row.__getitem__)
                b = cb[best]
            cache[key] = b
        return b

    co = [(dx, dy, dy * W + dx) for dx, dy in _CAUSAL]
    ah = [(dx, dy, dy * W + dx) for dx, dy in _AHEAD]
    out = bytearray(W * H)
    S = [-1] * (W * H)
    far = bytes(W * H) if plate is None else bytes(is_far_sea(c) for c in dstcode)
    for y in range(H):
        for x in range(W):
            p = y * W + x
            if far[p]:
                out[p] = plate[p]                   # open sea: plate in place
                S[p] = p if srcvalid[p] and srcpix[p] == plate[p] else -1
                continue
            tk = tfeat[p]
            region = assign[dstcomp[p]]
            if x and y:
                sl = S[p - 1]
                su = S[p - W]
                c = sl + 1
                if sl >= 0 and su >= 0 and c == su + W and srcvalid[c] \
                        and sfeat[c] == t_same[tk] and (region < 0 or srccomp[c] == region):
                    S[p] = c
                    out[p] = srcpix[c]
                    continue
            # this pixel's neighbourhood, looked up once for every candidate
            nco = [(o, D[tfeat[p + o]], out[p + o]) for dx, dy, o in co
                   if 0 <= x + dx < W and 0 <= y + dy]
            nah = [(o, D[tfeat[p + o]]) for dx, dy, o in ah
                   if 0 <= x + dx < W and y + dy < H]
            cands = {}
            for o, _row, _v in nco:
                s = S[p + o]
                if s >= 0:
                    c = s - o
                    if 0 <= c < W * H and srcvalid[c]:
                        cands[c] = None
            b = bucket(tk, region)
            cands[b[rng.randrange(len(b))]] = None
            cands[b[rng.randrange(len(b))]] = None
            Dp = D[tk]
            best = None
            bs = 1 << 30
            for c in cands:
                sc = Dp[sfeat[c]] * 4
                if region >= 0 and srccomp[c] != region:
                    sc += REGION_PEN
                if sc >= bs:
                    continue
                for o, row, v in nco:
                    q = c + o
                    sc += row[sfeat[q]]
                    if v != srcpix[q]:
                        sc += 10
                if sc >= bs:
                    continue
                for o, row in nah:
                    sc += row[sfeat[c + o]]
                if sc < bs:
                    bs, best = sc, c
            S[p] = best
            out[p] = srcpix[best]
    return out


# ---- sea tone by distance ---------------------------------------------------------
# The painted sea is darkest at the shore and lightens with distance (vanilla:
# luma 98 at 1 px, 143 at 6, 165 at 14, flat ~173 from ~25 px). The synthesis
# covers the first 6 px by feature match; everything beyond comes from the deep
# sea plate and is re-shaded here by its distance in the NEW world.
N_SECT = 8


def _sector(vx, vy):
    """Direction from a sea pixel toward its nearest land, 8 sectors (0 = east)."""
    return int(round(math.atan2(vy, vx) / (math.pi / 4))) % N_SECT


def _fill(seq):
    if seq[0] is None:
        seq[0] = next((v for v in seq if v is not None), None)
    for d in range(1, len(seq)):
        if seq[d] is None:
            seq[d] = seq[d - 1]


def sea_curve(pix, pal, cls, coast, margin=4):
    """Mean sea colour by distance to land, plus the luma spread by distance
    (the painter's near-shore sea is full of dark horizontal strokes, far
    more contrast than the calm deep sea). The shadow is directional in
    vanilla (sea EAST of land stays dark longest), but per-direction curves
    seamed where the nearest land flips, so the 8 sector curves are averaged
    into ONE isotropic curve."""
    cx, cy, c2 = coast
    acc = [[[0, 0, 0, 0] for _ in range(DEEP + 1)] for _ in range(N_SECT)]
    sd = [[0.0, 0.0, 0] for _ in range(DEEP + 1)]
    for y in range(margin, H - margin):
        for x in range(margin, W - margin):
            i = y * W + x
            k = cls[i]
            if k not in (C_SEA, C_SHALLOW):
                continue
            d = min(int(math.sqrt(c2[i])), DEEP)
            rgb = pal[pix[i]]
            a = acc[_sector(cx[i], cy[i])][d]
            a[0] += rgb[0]
            a[1] += rgb[1]
            a[2] += rgb[2]
            a[3] += 1
            if k == C_SEA:
                lu = (rgb[0] * 3 + rgb[1] * 6 + rgb[2]) / 10.0
                s = sd[d]
                s[0] += lu
                s[1] += lu * lu
                s[2] += 1
    curves = []
    for sec in range(N_SECT):
        cur = []
        for d in range(DEEP + 1):
            # pool +-1 px and the neighbouring sectors for a smooth estimate
            t = [0, 0, 0, 0]
            for ss in (sec - 1, sec, sec + 1):
                for dd in (d - 1, d, d + 1):
                    if 0 <= dd <= DEEP:
                        a = acc[ss % N_SECT][dd]
                        w = 2 if (ss == sec and dd == d) else 1
                        for k in range(4):
                            t[k] += a[k] * w
            cur.append((t[0] / t[3], t[1] / t[3], t[2] / t[3]) if t[3] else None)
        _fill(cur)
        if cur[0] is None:
            raise ValueError("MINIMAP: the source painting has no sea")
        curves.append(cur)
    curve = [tuple(sum(curves[s][d][j] for s in range(N_SECT)) / N_SECT for j in range(3))
             for d in range(DEEP + 1)]
    spread = []
    for a in sd:
        m = a[0] / a[2] if a[2] else 0
        spread.append(math.sqrt(max(a[1] / a[2] - m * m, 0)) if a[2] else None)
    _fill(spread)
    if spread[0] is None:
        raise ValueError("MINIMAP: the source painting has no open sea")
    return curve, spread


def plate_spread(plate, pal, margin=8):
    s1 = s2 = n = 0
    for y in range(margin, H - margin):
        for x in range(margin, W - margin):
            c = pal[plate[y * W + x]]
            lu = (c[0] * 3 + c[1] * 6 + c[2]) / 10.0
            s1 += lu
            s2 += lu * lu
            n += 1
    m = s1 / n
    return math.sqrt(max(s2 / n - m * m, 1e-6))


def local_mean(plate, pal, r=12):
    """Box-blurred plate colour (integral images): the plate's large-scale tone,
    which tone_sea subtracts so only the brush texture survives."""
    from itertools import accumulate
    xs_hi = [min(W, x + r + 1) for x in range(W)]
    xs_lo = [max(0, x - r) for x in range(W)]
    wdt = [a - b for a, b in zip(xs_hi, xs_lo)]
    out = []
    for ch in range(3):
        lut = bytes(c[ch] for c in pal).ljust(256, bytes(1))
        # column-prefix of the per-row horizontal window sums
        colpre = [[0] * W]
        for y in range(H):
            pre = [0]
            pre += accumulate(plate[y * W:(y + 1) * W].translate(lut))
            box = list(map(sub, map(pre.__getitem__, xs_hi), map(pre.__getitem__, xs_lo)))
            colpre.append(list(map(add, colpre[-1], box)))
        m = []
        for y in range(H):
            y0, y1 = max(0, y - r), min(H, y + r + 1)
            hgt = y1 - y0
            m += [s / (hgt * w) for s, w in zip(map(sub, colpre[y1], colpre[y0]), wdt)]
        out.append(m)
    return out


def sea_palette(pix, cls):
    """Palette indices the painter used on open sea (>= 20 uses): the only
    colours tone_sea may snap to."""
    cnt = [0] * 256
    for i in range(W * H):
        if cls[i] == C_SEA:
            cnt[pix[i]] += 1
    return sorted((k for k in range(256) if cnt[k] >= 20), key=lambda k: (-cnt[k], k))


# Compression fit: the painting barely compresses (vanilla 330,400 -> 306,684 B,
# the dpk slot), so the repaint must not be noisier than the original. The sea
# is most of the picture; each level coarsens only its texture:
#   (residual shift, sea colours kept, distance step in 1/4 px, texture gain)
FIT_LEVELS = (
    (1, 256, 1, 1.0),
    (2, 256, 1, 1.0),
    (2, 64, 4, 1.0),
    (3, 48, 4, 0.85),
    (3, 32, 4, 0.7),
    (4, 24, 4, 0.5),
)


class _Snap:
    """Nearest palette entry among the first `n` sea colours (ties: the more
    used colour), cached on the rounded RGB and shared by every FIT_LEVELS
    pass. Exact search, pruned: colours sorted by red, scanned outward from
    the query's red until the red gap alone exceeds the best distance."""

    def __init__(self, pal, sea_idx):
        self.pal = pal
        self.sea_idx = list(sea_idx)
        self.by_n = {}
        self.cache = {}

    def _table(self, n):
        t = self.by_n.get(n)
        if t is None:
            cols = sorted((self.pal[k][0], rank, self.pal[k][1], self.pal[k][2], k)
                          for rank, k in enumerate(self.sea_idx[:n]))
            t = self.by_n[n] = ([c[0] for c in cols], cols)
        return t

    def __call__(self, n, r, g, b):
        key = (n, int(r + 0.5), int(g + 0.5), int(b + 0.5))
        v = self.cache.get(key)
        if v is not None:
            return v
        import bisect
        _n, r, g, b = key
        reds, cols = self._table(n)
        ncol = len(cols)
        hi = bisect.bisect_left(reds, r)
        lo = hi - 1
        bd = brank = 1 << 30
        while lo >= 0 or hi < ncol:
            # each side stops once its red gap alone exceeds the best distance
            # (the gap only grows outward, the best only shrinks)
            if lo >= 0:
                pr, rank, pg, pb, k = cols[lo]
                if (r - pr) * (r - pr) > bd:
                    lo = -1
                else:
                    d = (pr - r) * (pr - r) + (pg - g) * (pg - g) + (pb - b) * (pb - b)
                    if d < bd or (d == bd and rank < brank):
                        bd, brank, v = d, rank, k
                    lo -= 1
            if hi < ncol:
                pr, rank, pg, pb, k = cols[hi]
                if (pr - r) * (pr - r) > bd:
                    hi = ncol
                else:
                    d = (pr - r) * (pr - r) + (pg - g) * (pg - g) + (pb - b) * (pb - b)
                    if d < bd or (d == bd and rank < brank):
                        bd, brank, v = d, rank, k
                    hi += 1
        self.cache[key] = v
        return v


def tone_sea(out, sea_px, coast_d2, pal, curve, snap, spread, pspread, lmean, level=0):
    """Shade every open-sea pixel by its distance to land in the NEW world:
        colour = curve(d) + (plate - local plate mean) * gain(d)
    The plate contributes only its brush texture (local mean removed, so the
    chunks it was copied from leave no tone seams); gain = vanilla's luma
    spread at d / the plate's, so near-shore strokes get the painter's
    contrast. d is fractional (quarter px) so the shading has no ring steps.
    Snaps to the colours the painter used on sea."""
    qshift, npal, qstep, gscale = FIT_LEVELS[level]
    mr, mg, mb = lmean
    cache = {}
    n2 = DEEP * DEEP
    u = 1 << qshift
    for p in sea_px:
        dd = coast_d2[p]
        q = 4 * DEEP if dd >= n2 else int(4 * math.sqrt(dd)) // qstep * qstep
        c = pal[out[p]]
        key = (int(c[0] - mr[p]) >> qshift, int(c[1] - mg[p]) >> qshift,
               int(c[2] - mb[p]) >> qshift, q)
        v = cache.get(key)
        if v is None:
            d0, fr = divmod(q, 4)
            d1 = min(d0 + 1, DEEP)
            fr /= 4.0
            ta, tb = curve[d0], curve[d1]
            g = (spread[d0] * (1 - fr) + spread[d1] * fr) / pspread * gscale
            v = snap(npal,
                     ta[0] * (1 - fr) + tb[0] * fr + u * key[0] * g,
                     ta[1] * (1 - fr) + tb[1] * fr + u * key[1] * g,
                     ta[2] * (1 - fr) + tb[2] * fr + u * key[2] * g)
            cache[key] = v
        out[p] = v


# ---- markers ---------------------------------------------------------------------
SEED_IDX = {0xFD, 0x33, 0x34, 0xFE, 0xFF}
FRAME_IDX = {0x53, 0xBC, 0x6C}
DOCK_IDX = {0x6C, 0xE8, 0x53, 0xBC}
MARK_FILL = {0xFD, 0x33, 0x34}    # red town/castle body, yellow cave body
MARK_CREAM = 0x6C                 # frame / dock inner ring -- ALSO the painting's shore highlight
MARK_OUTLINE = (SEED_IDX | FRAME_IDX | DOCK_IDX) - MARK_FILL - {MARK_CREAM}
MARK_RIM = 2                      # px: outline thickness around the interior
DOCK_MIN = 60                     # px: a real dock L is 77+; less is a stray stroke


def gpx(gx, gy):
    return 2 * ((gx + DX) % 256), 2 * gy


def marker_components(pix, landmarks, drawn=False):
    """Cut the vanilla marker sprites out at their landmark positions.
    -> list of (kind, gx, gy, sorted pixel indices).
    Landmarks of DRAWN_SITES and the Cardia dots are skipped unless `drawn`
    (they get a drawn sprite instead; the (88,44) cut, Bahamut's, also
    swallowed a Cardia dragon cave).
    Interior first: flood the fill (+ cream ring) of a site, the cream of a
    dock, from interior-coloured seeds; outline colours join only within
    MARK_RIM px of that interior. The painting draws its shore highlights in
    the frames' cream and its ink in their browns, so a flood through the
    outline carried every stroke touching it to the marker's new home (white
    curls beside W. Keep / Waterfall, user 2026-10-24). A dock under DOCK_MIN
    px has no painted L (Elfheim's) and is dropped."""
    comps = []
    taken = set()
    for gx, gy, kind, *_r in landmarks:
        if not drawn and _r and _r[-1] in UNCUT_SITES:
            continue
        px, py = gpx(gx, gy)
        dock = kind == "dock"
        inner = {MARK_CREAM} if dock else MARK_FILL | {MARK_CREAM}
        seed_c = {MARK_CREAM} if dock else MARK_FILL
        R = 5 if dock else 10
        st = [y * W + x for y in range(py - 8, py + 10) for x in range(px - 8, px + 10)
              if 0 <= x < W and 0 <= y < H and pix[y * W + x] in seed_c and
              (not dock or (abs(x - px - 1) <= 2 and abs(y - py - 1) <= 2))]
        core = set()
        while st:
            i = st.pop()
            if i in core:
                continue
            x, y = i % W, i // W
            if abs(x - px) > R + 4 or abs(y - py) > R + 6 or pix[i] not in inner:
                continue
            core.add(i)
            for o in (1, -1, W, -W):
                j = i + o
                if 0 <= j < W * H and j not in core:
                    st.append(j)
        comp = set(core)
        for i in core:
            x, y = i % W, i // W
            for yy in range(max(y - MARK_RIM, 0), min(y + MARK_RIM + 1, H)):
                for xx in range(max(x - MARK_RIM, 0), min(x + MARK_RIM + 1, W)):
                    if pix[yy * W + xx] in MARK_OUTLINE:
                        comp.add(yy * W + xx)
        comp -= taken
        if dock and len(comp) < DOCK_MIN:
            continue
        taken |= comp
        if comp:
            comps.append((kind, gx, gy, sorted(comp)))
    return comps


def _in_window(gx, gy, src):
    sx0, sy0, w, h = src
    return (gx - sx0) % GW < w and (gy - sy0) % GW < h


HARBOR_DOCK_SEED = (418, 290)      # MINI512 px inside Pravoka's dock L (fill)
HARBOR_DOCK_ROWS = (289, 297)      # the L's rows: below Pravoka's frame


def _harbor_dock_px(pix):
    """Pixel indices of the vanilla painting's Pravoka dock L, or []."""
    y0, y1 = HARBOR_DOCK_ROWS
    st = [HARBOR_DOCK_SEED[1] * W + HARBOR_DOCK_SEED[0]]
    got = set()
    while st:
        i = st.pop()
        if i in got or not (y0 <= i // W < y1) or pix[i] not in DOCK_IDX:
            continue
        got.add(i)
        st += [i + 1, i - 1, i + W, i - W]
    return sorted(got)


def stamp_markers(out, pix, comps, placed):
    """Move each vanilla marker with its stamp (src window -> dst). A dock
    marker follows every dock stamp whose window holds it (one vanilla dock
    may be copied several times); a site marker follows its ONE owner (the
    entrance records' rule), else it is dropped with its parked site.
    The sprite moves rigidly to the painted cell its landmark lands on, so a
    stamp that wraps the grid edge keeps its marker (the prototype shifted by
    2*(dst-src) px and lost markers wrapped past the bottom row)."""
    from .randworld_bake import owner, _shift
    n = 0
    for kind, gx, gy, comp in comps:
        if kind == "dock":
            sites = [p for p in placed if p["kind"] == "dock" and _in_window(gx, gy, p["src"])]
        else:
            p = owner(gx, gy, placed, grows=(0,))
            sites = [p] if p is not None else []
        ox, oy = gpx(gx, gy)
        for site in sites:
            nx, ny = gpx(*_shift(site, gx, gy))
            sx, sy = nx - ox, ny - oy
            for i in comp:
                y = i // W + sy
                if 0 <= y < H:
                    out[y * W + (i % W + sx) % W] = pix[i]
            n += 1
    # The ship's starting pier (the "harbor" stamp, Pravoka's dock) is painted
    # as a white L hanging under Pravoka's frame, but it is not a landmark, so
    # it was never masked/moved and the synthesis painted it away (user
    # 2026-10-18). Copy that L (vanilla rows 289..296 under the frame) with
    # the harbor stamp.
    L = _harbor_dock_px(pix)
    for p in placed:
        if p["kind"] != "harbor" or not L:
            continue
        ox, oy = gpx(*p["src"][:2])
        nx, ny = gpx(*p["dst"][:2])
        sx, sy = nx - ox, ny - oy
        for i in L:
            y = i // W + sy
            if 0 <= y < H:
                out[y * W + (i % W + sx) % W] = pix[i]
        n += 1
    # DRAWN_SITES get the cave sprite centred on their placed entrance: the
    # painting never marked Lifespring (open sea, impossible to find on a
    # random world), Caravan, Earthgift, Whisperwind (user 2026-10-24) or
    # Hellfire Chasm (87,53), and Bahamut's painted square (88,44) comes cut
    # together with a Cardia dragon cave. The Dragon Caves are three sites
    # placed apart on a random map, but the painted dots moved as vanilla's
    # cluster (the empty caves rode along with whichever stamp's window held
    # them; playtest 2026-10-30), so each placed cave gets one drawn sprite.
    for p in placed:
        if p["key"] not in DRAWN_SITES or not p.get("ents"):
            continue
        px, py = gpx(*p["ents"][0])
        draw_cave_marker(out, px, py)
        n += 1
    return n


# The painting's cave sprite: 10x10 yellow body (0x33, darker 0x34 corners)
# in a black rim with a black 2x2 dot, top-left at the landmark's px -4,-4.
CAVE_SPRITE = (
    "ffffffffffffffffffff",
    "ff3433333333333334ff",
    "ff3333333333333333ff",
    "ff3333333333333333ff",
    "ff333333ffff333333ff",
    "ff333333ffff333333ff",
    "ff3333333333333333ff",
    "ff3333333333333333ff",
    "ff3433333333333334ff",
    "ffffffffffffffffffff",
)
DRAWN_SITES = {"lifespring", "caravan", "earthgift", "whisperwind", "hellfire", "bahamut",
               "cardia_plains", "cardia_marsh", "cardia_forest"}
# landmark pins whose painted marker is never cut ("cardia": the empty caves)
UNCUT_SITES = DRAWN_SITES | {"cardia"}


def draw_cave_marker(out, px, py):
    for r, row in enumerate(CAVE_SPRITE):
        y = py - 4 + r
        if 0 <= y < H:
            for c in range(10):
                out[y * W + (px - 4 + c) % W] = int(row[2 * c:2 * c + 2], 16)


# ---- MINI256 from MINI512 --------------------------------------------------------
def downsample(pix512, pal512, pal256, used256):
    """2x2 average snapped to the MINI256 palette -- only to the entries the
    vanilla MINI256 actually uses (56 of 256; the rest are unused junk colours
    that showed as pink speckle)."""
    cand = [(j, pal256[j][0] * 4, pal256[j][1] * 4, pal256[j][2] * 4)
            for j in used256 if pal256[j][3]]
    rl = [c[0] for c in pal512]
    gl = [c[1] for c in pal512]
    bl = [c[2] for c in pal512]
    cache = {}
    out = bytearray(256 * 256)
    for y in range(256):
        r0 = 2 * y * W
        for x in range(256):
            i = r0 + 2 * x
            a, b, c, d = pix512[i], pix512[i + 1], pix512[i + W], pix512[i + W + 1]
            r = rl[a] + rl[b] + rl[c] + rl[d]
            g = gl[a] + gl[b] + gl[c] + gl[d]
            bb = bl[a] + bl[b] + bl[c] + bl[d]
            key = (r >> 4, g >> 4, bb >> 4)
            v = cache.get(key)
            if v is None:
                best = 1 << 30
                for j, pr, pg, pb in cand:
                    dd = (pr - r) ** 2 + (pg - g) ** 2 + (pb - bb) ** 2
                    if dd < best:
                        best, v = dd, j
                cache[key] = v
            out[y * 256 + x] = v
    return out


# ---- flat style ----------------------------------------------------------------------
MARK_IDX = SEED_IDX | FRAME_IDX | DOCK_IDX     # every palette entry a marker pixel uses


def render_flat(grid, placed, dec, landmarks, base=None, coast=True):
    """Flat-style pictures for `grid` (GW*GW tile ids, row-major).
      base:  None = flat sea (STYLE_FLAT); a painted MINI512 = keep its
             pixels on water cells (STYLE_FLAT_SEA)
      coast: draw ow_map's coast line on land
    -> (pix512, pal512, pix256, pal256), palettes as 256 (r, g, b, a).
    MINI512 entries keep their vanilla colour where a marker, the fog texture
    (mapfog_bake.fog_textures indexes the VANILLA palette) or a kept base
    pixel uses them; the Map tab colours and the coast colour take the lowest
    of the rest. Each cell is a 2x2 block; the painting is 256 tiles square
    for a 255 grid, so the spare unit (DX - 1) and row 255 repeat their
    neighbour's fill without a coast line."""
    from .. import ow_map as OM
    from .. import ow_minimap_data as OWD
    from . import mapfog_bake as MF
    ent = pck_entries(dec)
    o5, s5 = ent["MINI512.GIM"]
    o2, s2 = ent["MINI256.GIM"]
    _w, _h, vpix, pal, _o = gim_decode(dec[o5:o5 + s5])
    _w, _h, vpix2, pal2, _o = gim_decode(dec[o2:o2 + s2])
    cls = OM.grid_classes(grid)
    cm = OM.coast_mask(cls) if coast else bytes(GW * GW)
    wet = [c in OM.COAST_WATER for c in range(256)]

    def blocks():
        """(MINI512 offset of the block's top-left pixel, cell, cell x, cell y, spare)"""
        for row in range(256):
            for u in range(256):
                gx = (u - DX) % 256
                sx, sy = min(gx, GW - 1), min(row, GW - 1)
                yield 2 * row * W + 2 * u, sy * GW + sx, sx, sy, gx >= GW or row >= GW

    reserved = set(MARK_IDX) | set(MF.fog_textures(dec)[0])
    if base is not None:
        for p, i, _x, _y, _s in blocks():
            if wet[cls[i]]:
                reserved.update((base[p], base[p + 1], base[p + W], base[p + W + 1]))
    free = [j for j in range(256) if j not in reserved]
    if len(free) < len(OWD.PALETTE) + 1:
        raise ValueError(f"MINIMAP flat: {len(free)} free MINI512 palette entries "
                         f"< {len(OWD.PALETTE) + 1}")
    idx = free[:len(OWD.PALETTE)]
    cidx = free[len(OWD.PALETTE)]
    pal512 = list(pal)
    for j, rgb in zip(idx, OWD.PALETTE):
        pal512[j] = tuple(rgb) + (255,)
    pal512[cidx] = tuple(OM.COAST_RGB) + (255,)

    out = bytearray(base) if base is not None else bytearray(W * H)
    for p, i, sx, sy, spare in blocks():
        c = cls[i]
        if base is not None and wet[c]:
            continue
        v = idx[2 * c + ((sx + sy) & 1)]           # the Map tab's two-shade dither
        tl, tr, bl, br = (False,) * 4 if spare else OM.coast_quads(cm[i])
        out[p] = cidx if tl else v
        out[p + 1] = cidx if tr else v
        out[p + W] = cidx if bl else v
        out[p + W + 1] = cidx if br else v
    stamp_markers(out, vpix, marker_components(vpix, landmarks), placed)
    pix256, pal256 = box256(out, pal512, pal2, set(vpix2))
    return out, pal512, pix256, pal256


def box256(pix512, pal512, pal256, keep):
    """MINI256 = exact 2x2 box filter of MINI512. Palette entries in `keep`
    (the ones vanilla MINI256 uses, the fog texture's among them) stay; the
    rest (unused junk in vanilla) take the averages, most frequent first, and
    averages left over once they run out snap to the nearest colour already
    in the palette. -> (pixels, palette)."""
    rl = [c[0] for c in pal512]
    gl = [c[1] for c in pal512]
    bl = [c[2] for c in pal512]
    keys = [0] * (256 * 256)
    count = {}
    for y in range(256):
        r0 = 2 * y * W
        for x in range(256):
            i = r0 + 2 * x
            a, b, c, d = pix512[i], pix512[i + 1], pix512[i + W], pix512[i + W + 1]
            k = (((rl[a] + rl[b] + rl[c] + rl[d] + 2) >> 2) << 16
                 | ((gl[a] + gl[b] + gl[c] + gl[d] + 2) >> 2) << 8
                 | ((bl[a] + bl[b] + bl[c] + bl[d] + 2) >> 2))
            keys[y * 256 + x] = k
            count[k] = count.get(k, 0) + 1
    pal = list(pal256)
    free = [j for j in range(256) if j not in keep]
    order = sorted(count, key=lambda k: (-count[k], k))
    slot = {}
    for k, j in zip(order, free):
        slot[k] = j
        pal[j] = (k >> 16, (k >> 8) & 255, k & 255, 255)
    cand = sorted(keep) + sorted(slot.values())
    for k in order[len(free):]:
        r, g, b = k >> 16, (k >> 8) & 255, k & 255
        slot[k] = min(cand, key=lambda j: (pal[j][0] - r) ** 2 + (pal[j][1] - g) ** 2
                      + (pal[j][2] - b) ** 2)
    return bytes(slot[k] for k in keys), pal


# ---- render ------------------------------------------------------------------------
def render(tgt, placed, dec, van, att, landmarks, seed=1, log=None, fit=None):
    """Paint `tgt` (255 rows of tile ids) from the vanilla painting.
      placed:    world["placed"] (markers move with their stamps)
      dec:       decompressed vanilla MINIMAP2.PCK
      van:       the vanilla grid (255 rows) the painting depicts
      att:       512 tile attributes
      landmarks: ow_minimap_data.LANDMARKS (vanilla marker sites)
      fit(pix512, pix256) -> (ok, msg): tried per FIT_LEVELS level, first ok wins
    -> (pix512, pix256, level)."""
    say = log or (lambda _m: None)
    t = time.time()
    ent = pck_entries(dec)
    o5, s5 = ent["MINI512.GIM"]
    o2, s2 = ent["MINI256.GIM"]
    w5, h5, pix, pal, _o = gim_decode(dec[o5:o5 + s5])
    w2, h2, pix2v, pal2, _o2 = gim_decode(dec[o2:o2 + s2])
    if (w5, h5, w2, h2) != (W, H, 256, 256):
        raise ValueError(f"MINIMAP: unexpected GIM sizes {w5}x{h5} / {w2}x{h2}")
    used256 = sorted(set(pix2v))

    comps = marker_components(pix, landmarks)
    valid = bytearray([1]) * (W * H)
    for _k, _x, _y, comp in marker_components(pix, landmarks, drawn=True):
        for i in comp:
            x, y = i % W, i // W
            for yy in range(max(y - 3, 0), min(y + 4, H)):
                lo, hi = yy * W + max(x - 3, 0), yy * W + min(x + 4, W)
                valid[lo:hi] = bytes(hi - lo)
    plate_valid = bytearray(valid)                  # border rows/cols are fine for the plate
    for y in range(H):
        if y < 2 or y >= H - 2:
            valid[y * W:(y + 1) * W] = bytes(W)
        else:
            valid[y * W:y * W + 2] = b"\0\0"
            valid[y * W + W - 2:(y + 1) * W] = b"\0\0"

    scls = class_px(van, att)
    dcls = class_px(tgt, att)
    sf, s_c2 = features(scls)
    df, d_c2 = features(dcls)
    scomp, sinfo = components(scls)
    dcomp, dinfo = components(dcls)
    sval = [0] * len(sinfo)
    for i in range(W * H):
        if valid[i]:
            sval[scomp[i]] += 1
    stex, smean = region_stats(pix, pal, scomp, len(sinfo), valid)
    assign = assign_regions(sinfo, sval, stex, smean, dinfo, seed=seed)
    plate = sea_plate(pix, scls, s_c2[2], plate_valid, seed=seed)
    curve, spread = sea_curve(pix, pal, scls, s_c2)
    say(f"menu map: analysis {time.time() - t:.1f}s ({len(sinfo)} -> {len(dinfo)} regions)")
    t = time.time()
    base = synthesize(pix, sf, valid, scomp, df, dcomp, assign, seed=seed, plate=plate)
    say(f"menu map: synthesis {time.time() - t:.1f}s")
    t = time.time()
    snap = _Snap(pal, sea_palette(pix, scls))
    pspread = plate_spread(plate, pal)
    lmean = local_mean(plate, pal)
    sea_px = [p for p in range(W * H) if is_far_sea(df[p])]
    for level in range(len(FIT_LEVELS)):
        out = bytearray(base)
        tone_sea(out, sea_px, d_c2[2], pal, curve, snap, spread, pspread, lmean, level=level)
        n = stamp_markers(out, pix, comps, placed)
        small = downsample(out, pal, pal2, used256)
        if fit is None:
            break
        ok, msg = fit(out, small)
        say(f"menu map: fit level {level}: {msg}")
        if ok:
            break
    else:
        raise ValueError("MINIMAP repaint does not fit its dpk slot at any FIT_LEVELS level")
    say(f"menu map: toned {time.time() - t:.1f}s, {n} markers stamped (level {level})")
    return out, small, level


def build_pck(dec, pix512, pix256, pal512=None, pal256=None):
    """New decompressed MINIMAP PCK: the vanilla one with both GIMs' pixel
    data replaced, and their palettes when given as 256 (r, g, b, a)
    (headers and sizes untouched)."""
    new = bytearray(dec)
    ent = pck_entries(dec)
    for nm, pix, pal in (("MINI512.GIM", pix512, pal512), ("MINI256.GIM", pix256, pal256)):
        o, sz = ent[nm]
        w, h, _p, _pal, poff = gim_decode(dec[o:o + sz])
        if len(pix) != w * h:
            raise ValueError(f"{nm}: {len(pix)} px for a {w}x{h} image")
        new[o + poff:o + poff + w * h] = gim_swizzle(pix, w, h)
        if pal is not None:
            qoff, n = gim_palette(dec[o:o + sz])
            if len(pal) != n:
                raise ValueError(f"{nm}: {len(pal)} palette entries for a {n}-entry palette")
            new[o + qoff:o + qoff + 4 * n] = b"".join(bytes(c[:4]) for c in pal)
    return bytes(new)


# ---- ISO I/O (only the records involved, never the whole dpk) -------------------
def _dpk_index(f):
    from .extern_bake import _find_dpk, _dpk_records
    dpk_off, _size = _find_dpk(f)
    f.seek(dpk_off)
    cnt = struct.unpack("<I", f.read(4))[0]
    f.seek(dpk_off)
    return dpk_off, _dpk_records(f.read(16 + 36 * cnt))


def _read_record(f, dpk_off, recs, name):
    from . import wp16
    _rec, off, size = recs[name]
    f.seek(dpk_off + off)
    return wp16.decompress(f.read(size))


def read_vanilla(iso):
    """(decompressed MINIMAP2.PCK, vanilla grid as 255 rows) from a vanilla ISO."""
    from . import map_bake as MB
    from . import openworld_data as OW
    with open(iso, "rb") as f:
        dpk_off, recs = _dpk_index(f)
        dec = _read_record(f, dpk_off, recs, SOURCE_PCK)
        blob = _read_record(f, dpk_off, recs, MB._PCK00)
    doff, dsz = MB._find_subfile(blob, MB._AMD00)
    _flags, w, h = struct.unpack_from("<IHH", blob, doff)
    g0 = doff + MB._AMD00_HDR
    if (w, h) != (GW, GW) or dsz != MB._AMD00_HDR + w * h * 2:
        raise ValueError(f"MINIMAP: unexpected overworld grid {w}x{h}")
    if bytes(blob[g0 + OW.ANCHOR_OFF:][:len(OW.ANCHOR)]) != OW.ANCHOR:
        raise ValueError("MINIMAP: the source ISO's overworld is not vanilla")
    flat = struct.unpack_from("<%dH" % (GW * GW), blob, g0)
    return dec, [flat[y * GW:(y + 1) * GW] for y in range(GW)]


def slot_size(iso, names=MINIMAP_PCKS):
    with open(iso, "rb") as f:
        _off, recs = _dpk_index(f)
    return min(recs[nm][2] for nm in names)


def write_minimaps(iso, comp, dec_len, names=MINIMAP_PCKS):
    """Write one compressed PCK into every MINIMAPn.PCK slot of `iso` in place
    and read each back. The three vanilla variants (start / +bridge / +canal)
    differ only in pixels and GimConv source names, so one PCK serves all
    three (live 2026-10-14).

    +28 (compressed size) MUST be len(comp): the game loads the file at the
    END of a buffer sized from +32 and decodes in place, so a stale slot-sized
    +28 parks a short stream far from the buffer end and the decoder overruns
    its own input -- the 2026-10-24 map-open freeze (flat map, 35 KB stream
    under +28 = 306,684; wp16.inplace_lead). Fixing +28 alone leans on how
    the game sizes that buffer, which is not RE'd; so the stream is also
    reshaped (wp16.compress_inplace) to lead no more than vanilla's own does,
    which makes +28 land within a KB of vanilla's and the load vanilla-shaped."""
    from . import wp16
    if wp16.inplace_lead(comp) > INPLACE_MAX_LEAD:
        comp = wp16.compress_inplace(wp16.decompress(comp), INPLACE_MAX_LEAD)
    with open(iso, "r+b") as f:
        dpk_off, recs = _dpk_index(f)
        for nm in names:
            rec_off, data_off, data_size = recs[nm]
            if len(comp) > data_size:
                raise ValueError(f"{nm}: repainted PCK {len(comp)} B > slot {data_size} B")
        for nm in names:
            rec_off, data_off, data_size = recs[nm]
            f.seek(dpk_off + data_off)
            f.write(comp + b"\x00" * (data_size - len(comp)))
            f.seek(dpk_off + rec_off + 28)
            f.write(struct.pack("<II", len(comp), dec_len))   # compressed, DECOMPRESSED
        f.flush()
        want = wp16.decompress(comp)
        for nm in names:
            if _read_record(f, dpk_off, recs, nm) != want:
                raise ValueError(f"{nm}: read-back mismatch")


def paint_world(world, dec, van, log=None, slot=None, style=DEFAULT_STYLE):
    """Render the menu map for slot_data["randworld"]["world"] in `style`.
    -> (new decompressed PCK, compressed PCK, level). slot=None skips the
    fit; the flat style has no fit levels (level 0) and raises over `slot`."""
    from . import wp16
    from . import randworld_bake as RWB
    from ..randworld import landmarks as OWD       # the random map's rows (Bahamut, Hellfire)
    from ..randworld import tables as T
    if style not in STYLES:
        raise ValueError(f"MINIMAP: unknown style {style!r}")
    g = RWB.world_grid(world, resolve="closed")     # gate ids -> the shut gate's art
    if style == STYLE_FLAT:
        p5, q5, p2, q2 = render_flat(g, world["placed"], dec, OWD.LANDMARKS)
        new = build_pck(dec, p5, p2, q5, q2)
        comp = wp16.compress(new)
        if slot and len(comp) > slot:
            raise ValueError(f"MINIMAP flat: {len(comp)} B > slot {slot} B")
        return new, comp, 0
    tgt = [g[y * GW:(y + 1) * GW] for y in range(GW)]
    seed = int.from_bytes(RWB.world_digest(world)[:4], "little")
    got = {}

    def pack(pix512, pix256):
        if style == STYLE_FLAT_SEA:
            p5, q5, p2, q2 = render_flat(g, world["placed"], dec, OWD.LANDMARKS,
                                         base=pix512, coast=False)
            new = build_pck(dec, p5, p2, q5, q2)
        else:
            new = build_pck(dec, pix512, pix256)
        got["pck"] = (new, wp16.compress(new))
        return len(got["pck"][1])

    def fit(pix512, pix256):
        n = pack(pix512, pix256)
        return n <= slot, f"{n}/{slot} B"
    out, small, level = render(tgt, world["placed"], dec, van, T.ATT, OWD.LANDMARKS,
                               seed=seed, log=log, fit=fit if slot else None)
    if not slot:
        pack(out, small)
    return got["pck"] + (level,)


def bake_minimap(iso_out, world, src_iso, log=print, style=DEFAULT_STYLE):
    """Repaint MINIMAP0/1/2.PCK of `iso_out` for the generated world; the
    painting and the vanilla grid it depicts come from `src_iso` (the
    player's untouched ISO). COSMETIC: callers run it under a soft guard --
    on any failure the vanilla picture stays (the cursor points still move)."""
    from . import wp16
    t0 = time.time()
    dec, van = read_vanilla(src_iso)
    slot = slot_size(iso_out)
    new, comp, level = paint_world(world, dec, van, log=log, slot=slot, style=style)
    if wp16.decompress(comp) != new:
        raise ValueError("MINIMAP: wp16 round-trip mismatch")
    write_minimaps(iso_out, comp, len(new))
    log(f"menu map repainted ({style}): {len(comp)}/{slot} B, fit level {level}, "
        f"{time.time() - t0:.1f}s")
    return level
