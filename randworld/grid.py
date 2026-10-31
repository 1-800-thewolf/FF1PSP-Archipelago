"""Torus grid helpers for the random overworld (pure Python, no numpy).

The overworld is 255 x 255 cells and wraps both ways. Cells are addressed by a
flat index i = y * N + x. Every helper here works on flat lists / bytearrays.
"""
from collections import deque

N = 255
NN = N * N


def idx(x, y):
    return (y % N) * N + (x % N)


def xy(i):
    return i % N, i // N


def _build_nb():
    nb4, nb8 = [], []
    for y in range(N):
        yu, yd = ((y - 1) % N) * N, ((y + 1) % N) * N
        row = y * N
        for x in range(N):
            xl, xr = (x - 1) % N, (x + 1) % N
            nb4.append((row + xr, row + xl, yd + x, yu + x))
            nb8.append((row + xr, row + xl, yd + x, yu + x,
                        yd + xr, yd + xl, yu + xr, yu + xl))
    return nb4, nb8


NB4, NB8 = _build_nb()
# NB4 order: E W S N.  NB8 adds SE SW NE NW.
DIRS4 = ((1, 0), (-1, 0), (0, 1), (0, -1))


def tdist2(a, b):
    """Squared torus distance between two (x, y) points."""
    dx = abs(a[0] - b[0]) % N
    dy = abs(a[1] - b[1]) % N
    dx = min(dx, N - dx)
    dy = min(dy, N - dy)
    return dx * dx + dy * dy


def tdist(a, b):
    return tdist2(a, b) ** 0.5


def label(mask, nb=None):
    """4-connected components of the truthy cells of `mask`.
    Returns (lab, sizes): lab[i] = component id or -1."""
    nb = nb or NB4
    lab = [-1] * NN
    sizes = []
    for s in range(NN):
        if not mask[s] or lab[s] >= 0:
            continue
        c = len(sizes)
        lab[s] = c
        stack, n = [s], 1
        while stack:
            i = stack.pop()
            for j in nb[i]:
                if mask[j] and lab[j] < 0:
                    lab[j] = c
                    n += 1
                    stack.append(j)
        sizes.append(n)
    return lab, sizes


def flood(mask, seeds, nb=None):
    """bytearray of the cells 4-connected to `seeds` through truthy mask cells
    (seed cells count even when the mask is false there)."""
    nb = nb or NB4
    out = bytearray(NN)
    q = deque()
    for s in seeds:
        if not out[s]:
            out[s] = 1
            q.append(s)
    while q:
        i = q.popleft()
        for j in nb[i]:
            if mask[j] and not out[j]:
                out[j] = 1
                q.append(j)
    return out


def bfs_dist(mask, seeds, cap=None, nb=None):
    """Multi-source BFS distance through truthy mask cells (-1 = unreached).
    Seeds are distance 0 whether or not the mask holds there."""
    nb = nb or NB4
    dist = [-1] * NN
    q = deque()
    for s in seeds:
        if dist[s] < 0:
            dist[s] = 0
            q.append(s)
    while q:
        i = q.popleft()
        d = dist[i] + 1
        if cap is not None and d > cap:
            continue
        for j in nb[i]:
            if mask[j] and dist[j] < 0:
                dist[j] = d
                q.append(j)
    return dist


def voronoi(mask, seeds, cost, maxc=8):
    """Geodesic Voronoi over the truthy mask cells: each cell goes to the seed
    with the cheapest 4-connected path, where entering cell i costs cost[i]
    (an int 1..maxc). Dial's bucket queue, so it stays linear.
    Returns (owner, dist): owner[i] = index into seeds or -1."""
    owner = [-1] * NN
    dist = [1 << 30] * NN
    buckets = [[] for _ in range(maxc + 1)]
    for k, s in enumerate(seeds):
        owner[s] = k
        dist[s] = 0
        buckets[0].append(s)
    cur, pending, ring = 0, len(seeds), maxc + 1
    nb = NB4
    while pending:
        b = buckets[cur % ring]
        while b:
            i = b.pop()
            pending -= 1
            d = dist[i]
            if d != cur:
                continue
            o = owner[i]
            for j in nb[i]:
                if not mask[j]:
                    continue
                nd = d + cost[j]
                if nd < dist[j]:
                    dist[j] = nd
                    owner[j] = o
                    buckets[nd % ring].append(j)
                    pending += 1
        cur += 1
    return owner, dist


def dilate(cells, r, mask=None):
    """Set of cells within 4-connected BFS distance r of `cells` (optionally
    only through truthy mask cells)."""
    out = set(cells)
    front = list(out)
    for _ in range(r):
        nxt = []
        for i in front:
            for j in NB4[i]:
                if j not in out and (mask is None or mask[j]):
                    out.add(j)
                    nxt.append(j)
        front = nxt
    return out


def disk(cx, cy, r):
    """Flat indices of the cells within Euclidean radius r of (cx, cy)."""
    out = []
    ri = int(r) + 1
    rr = r * r
    for dy in range(-ri, ri + 1):
        for dx in range(-ri, ri + 1):
            if dx * dx + dy * dy <= rr:
                out.append(idx(cx + dx, cy + dy))
    return out


def line(a, b):
    """4-connected cell path from point a to point b (shortest torus way)."""
    ax, ay = a
    bx, by = b
    dx = (bx - ax + N // 2) % N - N // 2
    dy = (by - ay + N // 2) % N - N // 2
    n = max(abs(dx), abs(dy), 1)
    out = []
    px, py = ax, ay
    out.append(idx(px, py))
    for k in range(1, n + 1):
        x = ax + round(dx * k / n)
        y = ay + round(dy * k / n)
        if x != px and y != py:          # keep it 4-connected
            out.append(idx(x, py))
        out.append(idx(x, y))
        px, py = x, y
    return out


def box_blur(values, r=1, passes=2):
    """Separable box blur on the torus (two passes ~ a Gaussian of sigma
    ~ r * 0.8): smooths a height field before it is thresholded."""
    v = list(values)
    w = 2 * r + 1
    for _ in range(passes):
        out = [0.0] * NN
        for y in range(N):                      # horizontal
            row = y * N
            for x in range(N):
                acc = 0.0
                for d in range(-r, r + 1):
                    acc += v[row + (x + d) % N]
                out[row + x] = acc / w
        v2 = [0.0] * NN
        for y in range(N):                      # vertical
            for x in range(N):
                acc = 0.0
                for d in range(-r, r + 1):
                    acc += out[((y + d) % N) * N + x]
                v2[y * N + x] = acc / w
        v = v2
    return v
