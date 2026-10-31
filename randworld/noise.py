"""Seamless value noise on the 255 x 255 torus (pure Python).

value(x, y) for one octave with `cells` lattice points per side is a
smoothstep-bilinear blend of the four lattice values around (x, y); the
lattice wraps, so the field tiles. fbm() sums octaves with halving amplitude;
warp() bends a field's coordinates.
"""
from .grid import N, NN


def _axis(cells):
    """Per coordinate: (i0, i1, smoothstep weight)."""
    out = []
    for u in range(N):
        t = u * cells / N
        i0 = int(t)
        f = t - i0
        f = f * f * (3 - 2 * f)
        out.append((i0 % cells, (i0 + 1) % cells, f))
    return out


def octave(rng, cells):
    """One octave as a flat list in [0, 1]."""
    lat = [[rng.random() for _ in range(cells)] for _ in range(cells)]
    ax = _axis(cells)
    out = [0.0] * NN
    k = 0
    for y in range(N):
        j0, j1, fy = ax[y]
        r0, r1 = lat[j0], lat[j1]
        gy = 1 - fy
        for x in range(N):
            i0, i1, fx = ax[x]
            a = r0[i0] + (r0[i1] - r0[i0]) * fx
            b = r1[i0] + (r1[i1] - r1[i0]) * fx
            out[k] = a * gy + b * fy
            k += 1
    return out


def fbm(rng, base, octaves, gain=0.5):
    """Fractal sum of `octaves` octaves starting at `base` lattice cells per
    side, normalised to [0, 1] by the amplitude sum."""
    acc = [0.0] * NN
    amp, tot = 1.0, 0.0
    cells = base
    for _ in range(octaves):
        o = octave(rng, min(cells, N))
        for i in range(NN):
            acc[i] += amp * o[i]
        tot += amp
        amp *= gain
        cells *= 2
    inv = 1.0 / tot
    return [v * inv for v in acc]


def warp(h, rng, base, amp):
    """Domain warp: h resampled at points pushed by two low-frequency fields
    (up to ~amp tiles). Bends the lattice-aligned blobs of value noise into
    bays and peninsulas; the field stays seamless on the torus."""
    wx = fbm(rng, base, 2)
    wy = fbm(rng, base, 2)
    out = [0.0] * NN
    k = 0
    for y in range(N):
        for x in range(N):
            fx = x + (wx[k] - 0.5) * 2 * amp
            fy = y + (wy[k] - 0.5) * 2 * amp
            x0, y0 = int(fx // 1), int(fy // 1)
            tx, ty = fx - x0, fy - y0
            x0 %= N
            y0 %= N
            x1, y1 = (x0 + 1) % N, (y0 + 1) % N
            r0, r1 = y0 * N, y1 * N
            a = h[r0 + x0] + (h[r0 + x1] - h[r0 + x0]) * tx
            b = h[r1 + x0] + (h[r1 + x1] - h[r1 + x0]) * tx
            out[k] = a + (b - a) * ty
            k += 1
    return out


def quantile(values, q):
    """The q-quantile of a list (q in [0, 1])."""
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(q * (len(s) - 1))))
    return s[k]
