"""The asteroid ring seen from its sunward side (SE-1798): the "Octahedron - Seed 2" world
offline with the client plugin and the world's asteroid pack, the camera 140 km from
EarthLike's center with the sun behind it, looking at the planet across the ring, as from
Gamma Station when the sun is behind the player. Every ring asteroid between the camera and
the planet is in full sunlight, so its impostor must be drawn with it.

    IMP_TASK=se1798 IMP_SAVE=<"Octahedron - Seed 2" save> \\
        uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/sunward_ring.py <tag>

Per view, a screenshot and the stats in ~/.se-test/<task>-sunward/<tag>, then one line per
view: the drawn asteroid impostors on the sun's side of the planet and how many of them the
renderer lit with less than full sunlight, next to this script's own ray test. A view passes
when the renderer's sunlight matches the ray test for every one of them. Exits with the
number of failed views.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asteroid_pack as ap  # noqa: E402
import rig  # noqa: E402
import ring_budget as rb  # noqa: E402
import ring_levels as rl  # noqa: E402

OUT = rig.TEST_ROOT / f"{rig.TASK}-sunward"
DISTANCE = 140000.0
rig.CLIENT_GIB = 10.0

# The world's planets: EarthLike's storage (2^18 m) has its min corner at -131072 on each
# axis, so it sits at the origin; the Moon's (2^15 m) at (0, 120000, -130000). In the order
# the stats list them, matched by size below.
CENTERS = {
    "EarthLike": np.zeros(3),
    "Moon": np.array([16384.0, 136384.0, -113616.0]),
}


def vec(text: str) -> np.ndarray:
    return np.array([float(v) for v in text.split(",")])


def unit(v) -> np.ndarray:
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def planets(totals: dict) -> list[tuple[float, float, np.ndarray]]:
    """(ground, top, center) per planet of the stats"""
    out = []
    for p in totals.get("planets", "").split(";"):
        if not p:
            continue
        ground, top = (float(x) for x in p.split(":"))
        center = CENTERS["EarthLike" if ground > 30000 else "Moon"]
        out.append((ground, top, center))
    return out


def ray_sunlight(point, sun, bands) -> float:
    """The renderer's Sunlight"""
    light = 1.0
    for ground, top, center in bands:
        to_center = center - point
        along = float(to_center @ sun)
        squared = float(to_center @ to_center)
        passing = (
            math.sqrt(max(0.0, squared - along * along))
            if along > 0
            else math.sqrt(squared) - along
        )
        if passing >= top:
            continue
        t = (passing - ground) / (top - ground)
        if t <= 0:
            return 0.0
        light *= t * t * (3 - 2 * t)
    return light


def ring_normal() -> np.ndarray:
    """The normal of the ring's plane, from the pack's asteroids"""
    pos, _, _ = rb.read_index(ap.PACK / ap.PACK_FILE)
    return np.linalg.svd(pos - pos.mean(0))[2][2]


def views(sun: np.ndarray, normal: np.ndarray):
    """(name, eye, forward): the camera on the sun's side in the ring's plane, edge on to it,
    straight at the planet and turned 20 degrees off it, so the ring shows against space too
    """
    flat = unit(sun - normal * float(sun @ normal))
    eye = flat * DISTANCE
    toward = -flat
    side = unit(np.cross(toward, normal))
    turned = unit(
        toward * math.cos(math.radians(20)) + side * math.sin(math.radians(20))
    )
    return [("planet", eye, toward), ("beside", eye, turned)]


def check(name: str, totals: dict, impostors: dict) -> bool:
    sun = vec(totals["sun"])
    bands = planets(totals)
    lit = dark = wrong = 0
    worst = []
    for r in rl.drawn(impostors).values():
        pos = vec(r["pos"])
        if float(pos @ sun) <= 0:
            continue
        got = float(r.get("sunlight", "nan"))
        want = ray_sunlight(pos, sun, bands)
        if got < 0.99:
            dark += 1
        else:
            lit += 1
        if not abs(got - want) <= 0.02:
            wrong += 1
            worst.append((abs(got - want), r["id"], got, want))
    worst.sort(reverse=True)
    ok = wrong == 0 and lit + dark > 0
    print(
        f"{'PASS' if ok else 'FAIL'} {name}: sun {totals['sun']}, planets "
        f"{totals.get('planets')}; {lit + dark} drawn on the sun's side, {dark} below full "
        f"sunlight, {wrong} differ from the ray test"
        + "".join(f"; id {i} {g:.3f} vs {w:.3f}" for _, i, g, w in worst[:3]),
        flush=True,
    )
    return ok


SHIFT = 3000.0


def top_luma(image, x: float, y: float, r: float) -> float:
    """The mean of the brightest quarter of the pixels in the circle"""
    h, w = image.shape
    x0, x1 = max(0, int(x - r)), min(w, int(x + r) + 1)
    y0, y1 = max(0, int(y - r)), min(h, int(y + r) + 1)
    if x0 >= x1 or y0 >= y1:
        return float("nan")
    yy, xx = np.mgrid[y0:y1, x0:x1]
    inside = image[y0:y1, x0:x1][(xx + 0.5 - x) ** 2 + (yy + 0.5 - y) ** 2 <= r * r]
    if inside.size == 0:
        return float("nan")
    inside = np.sort(inside)[::-1]
    return float(inside[: max(1, inside.size // 4)].mean())


def pairs(out: Path, name: str) -> dict:
    """Per asteroid with its real one drawn: the impostor's brightness over the real one's in
    the shift shot, where both are in the same frame, grouped by what is behind them: the
    planet or space. Prints and returns the medians."""
    from PIL import Image

    image = np.asarray(Image.open(out / f"{name}-shift.png").convert("L"), dtype=float)
    plain = np.asarray(Image.open(out / f"{name}-real.png").convert("L"), dtype=float)
    real = json.loads((out / f"{name}-real.json").read_text())["impostors"]
    moved = json.loads((out / f"{name}-shift.json").read_text())["impostors"]
    groups: dict[str, list[float]] = {}
    for i, r in real.items():
        m = moved.get(i)
        if r.get("kind") != "Voxel" or r.get("real") != "1" or not m:
            continue
        if "radius" not in r or "radius" not in m or r.get("screen") == "behind":
            continue
        radius = float(r["radius"])
        if not 4 < radius < 80:
            continue
        rx, ry = (float(v) for v in r["screen"].split(","))
        mx, my = (float(v) for v in m["screen"].split(","))
        a = top_luma(image, rx, ry, radius * 0.8)
        b = top_luma(image, mx, my, radius * 0.8)
        # What's behind: the real-only shot around the impostor's spot
        behind = top_luma(plain, mx, my, radius * 1.5)
        if not (a > 8 and b == b and behind == behind):
            continue
        groups.setdefault("planet" if behind > 60 else "space", []).append(b / a)
    result = {}
    for group, ratios in sorted(groups.items()):
        ratios.sort()
        result[group] = ratios[len(ratios) // 2]
        print(
            f"{name} {group}: {len(ratios)} asteroids, impostor/real median "
            f"{ratios[len(ratios) // 2]:.2f} (quartiles {ratios[len(ratios) // 4]:.2f}"
            f"-{ratios[3 * len(ratios) // 4]:.2f})",
            flush=True,
        )
    return result


def run(out: Path) -> int:
    import local

    client = rig.Client(2, "Viktor", plugin=True, quality="medium", probe=True)
    world = rl.launch(client)
    # IMP_ONTOP=1: the client plugin leaves the impostors in the LDR pass, after tone mapping
    ontop = client.plugin_storage / "Impostors.ontop"
    if os.environ.get("IMP_ONTOP") == "1":
        ontop.parent.mkdir(parents=True, exist_ok=True)
        ontop.write_text("")
    else:
        ontop.unlink(missing_ok=True)
    failures = 0
    try:
        api = client.wait_api()
        api.load(str(world), online_mode="OFFLINE")
        local.wait(lambda: local.past_questions(api), 900, 2)
        rig.ensure_character(api)
        rig.wait_world(api, timeout=1200)
        rig.focus_gameplay(api)
        totals = {}
        deadline = time.monotonic() + 120
        while "sun" not in totals and time.monotonic() < deadline:
            time.sleep(2)
            totals, _ = client.stats()
        for name, eye, forward in views(vec(totals["sun"]), ring_normal()):
            rl.look(client, eye, forward)
            totals, impostors, _ = rl.settle(client)
            (out / f"{name}.png").write_bytes(api.screenshot_bytes(ignore_sprites=True))
            (out / f"{name}.json").write_text(
                json.dumps({"totals": totals, "impostors": impostors, "eye": list(eye)})
            )
            if not check(name, totals, impostors):
                failures += 1
            # The same view with the real asteroids only, and with the impostors drawn
            # SHIFT m to the right of the real ones, in the same frame
            right = unit(np.cross(forward, [0.0, 1.0, 0.0]))
            side = " ".join(f"{v * SHIFT:.1f}" for v in right)
            for mode, mode_text in (("real", "real"), ("shift", f"shift {side}")):
                before = int(client.stats()[0].get("tick", 0))
                (client.loaded_mod_storage / "mode").write_text(mode_text)
                while int(client.stats()[0].get("tick", 0)) < before + 240:
                    time.sleep(1)
                time.sleep(5)
                (out / f"{name}-{mode}.png").write_bytes(
                    api.screenshot_bytes(ignore_sprites=True)
                )
                (out / f"{name}-{mode}.json").write_text(
                    json.dumps({"impostors": client.stats()[1]})
                )
            pairs(out, name)
            (client.loaded_mod_storage / "mode").unlink(missing_ok=True)
    finally:
        client.stop()
    return failures


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = OUT / sys.argv[1]
    out.mkdir(parents=True, exist_ok=True)
    return run(out)


if __name__ == "__main__":
    sys.exit(main())
