"""The asteroid levels a client picks over the Octahedron ring, from the asteroid pack's index
alone (SE-971), no game: replays EarlyAsteroids' pick (the levels of the pack's asteroids in
range, fitted into the quad budget) and the renderer's draw (the ones in view, nearest first,
within the budget) for fixed camera spots, with and without zoom. Per spot: the ring
asteroids in view, their levels and the quads.

    uv run --no-project --with numpy python tests/ring_budget.py <pack.dat> [old|new]

`old` is the pick before SE-971: every asteroid in range takes from the budget, all levels
are picked for one smaller screen, and the size ignores the zoom. `new` is the pick since:
only those in view take from it, sized for the zoomed view, one level finer at a time where
that takes the most off the on-screen error per quad, while the budget lasts, the error
counting less the farther off the view's center the asteroid is. Asteroids within
the voxel replication range are left out, the game draws them. Exits with 0; the numbers are
for reading.
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

import numpy as np

PIXELS_PER_RADIAN = 540 / 0.7002075382097097
CELLS_PER_PIXEL = 1 / 2.5
HEADER, QUAD = 19, 10
BUDGET = 16000
MIN_PIXELS = 3.0
RANGE = 400000.0
# The game draws the asteroids within its voxel replication range itself: ViewDistance/sqrt(3)
# per axis less the ReplicationMargin, 15 km in the Octahedron
REAL = 15000 / math.sqrt(3) - 500
FOV = math.radians(70)
WIDTH, HEIGHT = 1920, 1080
LODS = (2, 4, 8, 16, 32, 64)


def read_index(path: Path):
    """Positions, radii and per asteroid {level: quads} of the pack's entries"""
    with path.open("rb") as f:
        magic, fmt = struct.unpack("<iB", f.read(5))
        assert magic == 0x50414D49 and fmt == 3, (magic, fmt)
        n = shift = 0
        while True:
            b = f.read(1)[0]
            n |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                break
        f.read(n + 8)
        (count,) = struct.unpack("<i", f.read(4))
        pos, rad, levels = [], [], []
        for _ in range(count):
            f.read(8 + 8 + 4 + 1)
            r, x, y, z = struct.unpack("<fddd", f.read(28))
            f.read(16)
            lv = {}
            for _ in range(f.read(1)[0]):
                lod, length = struct.unpack("<Bi", f.read(5))
                lv[lod] = (length - HEADER) // QUAD
            pos.append((x, y, z))
            rad.append(r)
            levels.append(lv)
    return np.array(pos), np.array(rad), levels


def level(lv: dict, pixels: float) -> int:
    """EarlyAsteroids.Level without the hysteresis"""
    wanted = 2 * pixels * CELLS_PER_PIXEL
    fit = [l for l in lv if l >= wanted]
    return min(fit) if fit else max(lv)


def unit(v):
    return v / np.linalg.norm(v)


def in_view(eye, forward, up, fov, pos, rad, margin=0.0):
    """ImpostorRenderer.InView, the field of view widened by margin radians"""
    right = unit(np.cross(forward, up))
    up = np.cross(right, forward)
    d = pos - eye
    depth = d @ forward
    x, y = d @ right, d @ up
    tan_y = math.tan(fov / 2 + margin)
    tan_x = math.tan(math.atan(math.tan(fov / 2) * WIDTH / HEIGHT) + margin)
    return (np.abs(x) - depth * tan_x <= rad * math.sqrt(1 + tan_x**2)) & (
        np.abs(y) - depth * tan_y <= rad * math.sqrt(1 + tan_y**2)
    )


def weight(eye, forward, up, fov, pos, rad, margin):
    """EarlyAsteroids.Weight: 1 in the view's center, 3/4 at the screen's edge and 1/2 at
    the margin's outer edge, by the asteroid's point nearest to the center"""
    right = unit(np.cross(forward, up))
    up = np.cross(right, forward)
    d = pos - eye
    depth = d @ forward
    size = np.arcsin(np.minimum(1, rad / np.maximum(np.linalg.norm(d, axis=1), rad)))
    half_y = fov / 2
    half_x = math.atan(math.tan(half_y) * WIDTH / HEIGHT)

    def off(angle, half):
        return np.where(
            angle <= half,
            np.maximum(0, angle) / half / 4,
            np.minimum(0.25 + (angle - half) / margin / 4, 0.5),
        )

    x = np.arctan2(np.abs(d @ right), depth) - size
    y = np.arctan2(np.abs(d @ up), depth) - size
    return 1 - np.maximum(off(x, half_x), off(y, half_y))


def pick(mode, eye, forward, up, fov, pos, rad, levels):
    """Level per asteroid in range, as the client picks them, and the scale it took"""
    dist = np.linalg.norm(pos - eye, axis=1)
    pixels = rad / np.maximum(dist, 1) * PIXELS_PER_RADIAN
    real = np.all(np.abs(pos - eye) - rad[:, None] <= REAL, axis=1)
    ok = (dist - rad <= RANGE) & (2 * pixels >= MIN_PIXELS) & ~real
    zoom = HEIGHT / 2 / math.tan(fov / 2) / PIXELS_PER_RADIAN
    view = in_view(eye, forward, up, fov, pos, rad, fov / 8)
    if mode == "old":
        size = pixels
        counted = ok
    else:
        size = np.where(view, pixels * max(zoom, 1), pixels)
        counted = ok & view
    idx = np.nonzero(ok)[0]
    if mode == "new":
        w = weight(eye, forward, up, fov, pos, rad, fov / 8)
        return greedy(idx, counted, size, levels, w), ok, 0.0, size

    def levels_at(scale):
        out, quads = {}, 0
        for i in idx:
            l = level(levels[i], size[i] * scale)
            out[i] = l
            if counted[i]:
                quads += levels[i][l] // 2
        return out, quads

    picked, quads = levels_at(1)
    scale = 1.0
    if quads > BUDGET:
        low, high = 0.0, 1.0
        for _ in range(12):
            s = (low + high) / 2
            if levels_at(s)[1] <= BUDGET:
                low = s
            else:
                high = s
        picked, quads = levels_at(low)
        scale = low
    return picked, ok, scale, size


def greedy(idx, counted, size, levels, w):
    """EarlyAsteroids.Fill: all at their coarsest level, then one level finer at a time, the
    step that takes the most off the on-screen error per quad first, the error weighted by
    how far off the view's center the asteroid is, while the budget lasts.
    Those not counted stay at the coarsest. The levels they want when those fit."""
    if (
        sum(levels[i][level(levels[i], size[i])] // 2 for i in idx if counted[i])
        <= BUDGET
    ):
        return {
            i: level(levels[i], size[i]) if counted[i] else min(levels[i]) for i in idx
        }
    picked = {i: min(levels[i]) for i in idx}
    quads = sum(levels[i][picked[i]] // 2 for i in idx if counted[i])
    steps = []
    for i in idx:
        if not counted[i]:
            continue
        lods = sorted(levels[i])
        want = level(levels[i], size[i])
        wanted = 2 * size[i] * CELLS_PER_PIXEL
        for a, b in zip(lods, lods[1:]):
            if a >= want:
                break
            gain = w[i] * wanted**3 / a / max(1, levels[i][b] // 2 - levels[i][a] // 2)
            steps.append((-gain, i, a, b))
    steps.sort()
    stuck = set()
    for _, i, a, b in steps:
        if i in stuck or picked[i] != a:
            continue
        cost = levels[i][b] // 2 - levels[i][a] // 2
        if quads + cost > BUDGET:
            stuck.add(i)
            continue
        quads += cost
        picked[i] = b
    return picked


def draw(eye, forward, up, fov, pos, rad, levels, picked):
    """The renderer: in view, nearest first, half of each shape's quads within the budget"""
    view = in_view(eye, forward, up, fov, pos, rad)
    ids = sorted(
        (i for i in picked if view[i]), key=lambda i: np.sum((pos[i] - eye) ** 2)
    )
    quads, drawn, dropped = 0, [], 0
    for i in ids:
        cost = levels[i][picked[i]] // 2
        if quads + cost > BUDGET:
            dropped += 1
            continue
        quads += cost
        drawn.append(i)
    return drawn, dropped, quads


def spots(pos):
    """(name, eye, forward) of each camera spot, from the ring's asteroids: its center, the
    normal of its plane and the median radius. The camera's up is the world's (the client
    probe's camera.txt has no roll)."""
    center = pos.mean(0)
    normal = np.linalg.svd(pos - center)[2][2]
    if normal[1] > 0:
        normal = -normal
    side = unit(np.cross(normal, [1.0, 0, 0]))
    tangent = np.cross(normal, side)
    base = center + side * float(np.median(np.linalg.norm(pos - center, axis=1)))
    return [
        ("along", base + normal * 2000, tangent),
        ("along-back", base + normal * 2000, -tangent),
        ("above-30", base + normal * 10000, unit(tangent - 0.58 * normal)),
        ("above-down", base + normal * 10000, -normal),
        ("inside", base - side * 20000, side),
        ("outside", base + side * 60000, -side),
    ]


ZOOMS = (1, 2, 8)
UP = np.array([0.0, 1.0, 0.0])


def fov_for(zoom: float) -> float:
    """Vertical field of view of a zoom, in radians"""
    return 2 * math.atan(math.tan(FOV / 2) / zoom)


def main() -> None:
    pack = Path(sys.argv[1])
    mode = sys.argv[2] if len(sys.argv) > 2 else "old"
    assert mode in ("old", "new"), mode
    pos, rad, levels = read_index(pack)
    print(f"pack: {len(pos)} asteroids, radius median {np.median(rad):.0f} m")
    for lod in LODS:
        q = [lv[lod] for lv in levels if lod in lv]
        print(
            f"  level {lod:2}: quads median {np.median(q):.0f}, mean {np.mean(q):.0f}"
        )
    print(f"budget {BUDGET}; mode {mode}\n")
    print(
        "| spot | zoom | in range | in view | drawn | dropped | scale | quads |"
        + "".join(f" L{l} |" for l in LODS)
        + " cubes that want 8+ |"
    )
    print("|---" * (9 + len(LODS)) + "|")
    up = UP
    for name, eye, forward in spots(pos):
        for zoom in ZOOMS:
            fov = fov_for(zoom)
            picked, ok, scale, _ = pick(mode, eye, forward, up, fov, pos, rad, levels)
            drawn, dropped, quads = draw(
                eye, forward, up, fov, pos, rad, levels, picked
            )
            hist = dict.fromkeys(LODS, 0)
            starved = 0
            for i in drawn:
                hist[picked[i]] += 1
                own = rad[i] / np.linalg.norm(pos[i] - eye) * PIXELS_PER_RADIAN * zoom
                if picked[i] == 2 and level(levels[i], own) >= 8:
                    starved += 1
            view = in_view(eye, forward, up, fov, pos, rad)
            print(
                f"| {name} | {zoom}x | {int(ok.sum())} | {int((view & ok).sum())} | "
                f"{len(drawn)} | {dropped} | {scale:.3f} | {quads} | "
                + " | ".join(str(hist[l]) for l in LODS)
                + f" | {starved} |"
            )


if __name__ == "__main__":
    main()
