"""Level changes of the ring asteroids in a still view (SE-1097), from the asteroid pack's
index alone, no game: replays EarlyAsteroids' pick once a second for a fixed camera, the
level each impostor shows fed back into the next pick, and counts the level changes after
the first pick. In multiplayer the pick's budget is MaxQuadsPerFrame less the quads of the
server's impostors in view (grids, the station's neighbors), so it moves while ships come
and go; `--used` gives the range those quads take, drawn anew each second.

    uv run --no-project --with numpy python tests/ring_still.py <pack.dat> [old|new] [--used MAX] [--picks N]

`old` is the pick of main before SE-1097, `new` the one since. Prints per spot and zoom the
asteroids in view, the changes and the asteroids that changed; exits with 1 when `new`
changed a level in a view.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np

from ring_budget import (
    BUDGET,
    CELLS_PER_PIXEL,
    MIN_PIXELS,
    PIXELS_PER_RADIAN,
    RANGE,
    REAL,
    UP,
    ZOOMS,
    fov_for,
    in_view,
    read_index,
    spots,
    weight,
)

HEIGHT = 1080
KEEP = 1.125
HOLD = 30


def quads(lv: dict, lod: int) -> int:
    return lv[lod] // 2


def level(lv: dict, pixels: float, current: int) -> int:
    """EarlyAsteroids.Level, with its hysteresis around the current level"""
    wanted = 2 * pixels * CELLS_PER_PIXEL
    fit = [l for l in lv if l >= wanted]
    best = min(fit) if fit else max(lv)
    if current in lv and best < current and wanted > current * 0.4:
        return current
    return best


class Picker:
    """EarlyAsteroids.Pick and FitLevels for one still view; `shown` is what the impostors
    show, the level of the last pick"""

    def __init__(self, mode, eye, forward, fov, pos, rad, levels):
        self.mode = mode
        self.levels = levels
        dist = np.linalg.norm(pos - eye, axis=1)
        pixels = rad / np.maximum(dist, 1) * PIXELS_PER_RADIAN
        real = np.all(np.abs(pos - eye) - rad[:, None] <= REAL, axis=1)
        ok = (dist - rad <= RANGE) & (2 * pixels >= MIN_PIXELS) & ~real
        zoom = min(max(HEIGHT / 2 / np.tan(fov / 2) / PIXELS_PER_RADIAN, 1), 32)
        view = in_view(eye, forward, UP, fov, pos, rad, fov / 8)
        self.ids = np.nonzero(ok)[0]
        self.view = view
        self.size = np.where(view, pixels * zoom, pixels)
        self.w = weight(eye, forward, UP, fov, pos, rad, fov / 8)
        self.shown: dict[int, int] = {}
        self.picks = 0
        self.held = [None, None]

    def hold(self, budget: int) -> int:
        """EarlyAsteroids.Hold: the lowest budget of this window of HOLD picks and the last"""
        if self.held[1] is None:
            self.held = [budget, budget]
        elif self.picks % HOLD == 0:
            self.held = [self.held[1], budget]
        else:
            self.held[1] = min(self.held[1], budget)
        self.picks += 1
        return min(self.held)

    def pick(self, budget: int) -> dict[int, int]:
        if self.mode == "new":
            budget = self.hold(budget)
        lv, shown = self.levels, self.shown
        picked = {}
        total = 0
        for i in self.ids:
            if self.view[i]:
                picked[i] = level(lv[i], self.size[i], shown.get(i, 0))
                total += quads(lv[i], picked[i])
            else:
                picked[i] = min(lv[i])
        if total > budget:
            self.fill(picked, budget)
        if self.mode == "new" and self.keep(picked, budget):
            return self.shown
        self.shown = picked
        return picked

    def error(self, levels: dict) -> float:
        """EarlyAsteroids.Error: the on-screen error of those in view, a level finer than the
        one their size wants counting as that one"""
        lv = self.levels
        total = 0.0
        for i in self.ids:
            if self.view[i]:
                wanted = 2 * self.size[i] * CELLS_PER_PIXEL
                target = level(lv[i], self.size[i], 0)
                total += self.w[i] * wanted**3 / min(levels[i], target)
        return total

    def keep(self, picked: dict, budget: int) -> bool:
        """The levels shown stay, the least useful steps of them taken back while they don't
        fit, unless the new pick takes 1/8 more off the error"""
        lv, shown = self.levels, dict(self.shown)
        total = 0
        for i in self.ids:
            if self.view[i]:
                if shown.get(i) not in lv[i]:
                    return False
                total += quads(lv[i], shown[i])
            else:
                shown[i] = picked[i]
        while total > budget:
            best, low = None, None
            for i in self.ids:
                if not self.view[i] or shown[i] == min(lv[i]):
                    continue
                lods = sorted(lv[i])
                a = lods[lods.index(shown[i]) - 1]
                wanted = 2 * self.size[i] * CELLS_PER_PIXEL
                added = max(1, quads(lv[i], shown[i]) - quads(lv[i], a))
                gain = self.w[i] * wanted**3 / a / added
                if low is None or gain < low:
                    best, low = i, gain
            if best is None:
                return False
            lods = sorted(lv[best])
            a = lods[lods.index(shown[best]) - 1]
            total -= quads(lv[best], shown[best]) - quads(lv[best], a)
            shown[best] = a
        if self.error(shown) > self.error(picked) * KEEP:
            return False
        self.shown = shown
        return True

    def fill(self, picked, budget):
        lv = self.levels
        total = 0
        steps = []
        for i in self.ids:
            if not self.view[i]:
                continue
            wanted = 2 * self.size[i] * CELLS_PER_PIXEL
            target = level(lv[i], self.size[i], 0)
            current = self.shown.get(i, 0)
            picked[i] = min(lv[i])
            total += quads(lv[i], picked[i])
            lods = sorted(lv[i])
            for a, b in zip(lods, lods[1:]):
                if a >= target:
                    break
                added = max(1, quads(lv[i], b) - quads(lv[i], a))
                gain = self.w[i] * wanted**3 / a / added
                if self.mode == "old" and b <= current:
                    gain *= 2
                steps.append((-gain, i, a, b))
        steps.sort()
        full = set()
        for _, i, a, b in steps:
            if i in full or picked[i] != a:
                continue
            added = quads(lv[i], b) - quads(lv[i], a)
            if total + added > budget:
                full.add(i)
            else:
                total += added
                picked[i] = b


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pack", type=Path)
    parser.add_argument("mode", nargs="?", default="old", choices=("old", "new"))
    parser.add_argument("--used", type=int, default=0)
    parser.add_argument("--picks", type=int, default=120)
    args = parser.parse_args()
    pos, rad, levels = read_index(args.pack)
    rng = random.Random(1097)
    print(f"mode {args.mode}, used 0..{args.used} quads, {args.picks} picks\n")
    print("| spot | zoom | in view | changes | changed | after 10 s |")
    print("|---|---|---|---|---|---|")
    failed = False
    for name, eye, forward in spots(pos):
        for zoom in ZOOMS:
            p = Picker(args.mode, eye, forward, fov_for(zoom), pos, rad, levels)
            p.pick(BUDGET - rng.randint(0, args.used))
            changes, changed, late = 0, set(), 0
            for n in range(args.picks):
                before = dict(p.shown)
                after = p.pick(BUDGET - rng.randint(0, args.used))
                for i, l in after.items():
                    if before.get(i) != l:
                        changes += 1
                        changed.add(i)
                        late += n >= 10
            failed |= args.mode == "new" and changes > 0
            print(
                f"| {name} | {zoom}x | {int((p.view[p.ids]).sum())} | {changes} | {len(changed)} | {late} |"
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
