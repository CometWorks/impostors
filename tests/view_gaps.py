"""In-game check of gaps in a moving view (SE-671): a ring of saved asteroids,
a near band 15 to 40 km away that is always shown and a far band 250 to 400 km
away that only a zoomed view shows. The client's test probe flies the
spectator camera (camera.txt): zoomed pans across both bands, then a fast
flight over the near band. Every second it compares the impostors the mod
draws (stats.txt) with the asteroids that should be on screen at that moment
and counts the missing ones, by reason. Same rig as scenario.py. Prints the
numbers, one PASS or FAIL line per check, and exits with the number of
failures.

    uv run --no-project --with httpx --with pillow python tests/view_gaps.py
    IMP_SURVIVAL=1 ...   # survival with spectating allowed: the server has the
                           camera from the View messages only
    IMP_KEEP=1 ...       # leave the game running
    IMP_MAX_GAP=0.05 ... # the share of missing asteroids a phase may have
"""

from __future__ import annotations

import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402

SURVIVAL = os.environ.get("IMP_SURVIVAL") == "1"
MAX_GAP = float(os.environ.get("IMP_MAX_GAP", "0.05"))
SERVER = rig.Server()
CLIENT = rig.Client(1, "Alpha", probe=True)
MIN_PIXELS = 3.0
MAX_DISTANCE = 500000
FOV_ZOOMED = 5.0
FOV_WIDE = 70.0

failures = 0


def check(name: str, ok: bool, detail="") -> bool:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip(), flush=True)
    failures += not ok
    return ok


@dataclass
class Rock:
    asteroid: world.Asteroid
    size: float

    @property
    def center(self):
        return tuple(c + self.size / 2 for c in self.asteroid.position)

    @property
    def radius(self):
        # The server sizes impostors by the bounding sphere of their AABB
        return self.size / 2 * math.sqrt(3)


def ring() -> list[Rock]:
    """Asteroids ahead of HOME (along -Z), 60 degrees to either side, below the eye"""
    rng = random.Random(671)
    files = sorted(p.name for p in world.VOXEL_MAPS.glob("Asteroid512-*.vx2"))
    small = sorted(p.name for p in world.VOXEL_MAPS.glob("Asteroid256-*.vx2"))
    rocks = []
    for i, (lo, hi, count) in enumerate(((15000, 40000, 120), (250000, 400000, 120))):
        for k in range(count):
            distance = rng.uniform(lo, hi)
            yaw = math.radians(rng.uniform(-60, 60))
            # The far band within the zoomed pans' view, which turn at pitch -7
            pitch = math.radians(rng.uniform(-12, -3) if i == 0 else rng.uniform(-8.5, -5.5))
            large = i == 1 or k % 2 == 0
            size = 512.0 if large else 256.0
            file = rng.choice(files if large else small)
            center = (
                distance * math.cos(pitch) * math.sin(yaw),
                distance * math.sin(pitch),
                -distance * math.cos(pitch) * math.cos(yaw),
            )
            rocks.append(
                Rock(
                    world.Asteroid(
                        f"Ring{i}_{k:03d}",
                        131000000000000000 + i * 1000 + k,
                        tuple(c - size / 2 for c in center),
                        file,
                    ),
                    size,
                )
            )
    return rocks


ROCKS = ring()


def vec(text: str):
    return tuple(float(v) for v in text.split(","))


def expected(totals: dict) -> set[int]:
    """The asteroids the camera of these stats should show: in the view cone around the
    diagonal, beyond the replication of saved voxels, at least MIN_PIXELS across"""
    camera, forward = vec(totals["camera"]), vec(totals["forward"])
    fov = math.radians(float(totals["fov"]))
    width, height = 1280, 720
    half = math.atan(math.tan(fov / 2) * math.hypot(1, width / height))
    pixels_per_radian = height / 2 / math.tan(fov / 2)
    replicated = world.VIEW_M / math.sqrt(3)
    ids = set()
    for rock in ROCKS:
        d = [c - e for c, e in zip(rock.center, camera)]
        distance = math.sqrt(sum(v * v for v in d))
        if all(abs(v) < replicated + rock.size / 2 for v in d):
            continue
        if distance - rock.radius > MAX_DISTANCE:
            continue
        cos = sum(a * b for a, b in zip(d, forward)) / distance
        angle = math.acos(max(-1, min(1, cos))) - math.asin(min(1, rock.radius / distance))
        # A margin inside the edge: the stats' camera is up to a frame off the drawn one
        if angle > half * 0.9:
            continue
        # Clearly above the cut-off, in case the estimates differ a little
        if 2 * rock.radius / distance * pixels_per_radian < 2 * MIN_PIXELS:
            continue
        ids.add(rock.asteroid.entity_id)
    return ids


def fly(path: str | None) -> None:
    file = CLIENT.appdata / "camera.txt"
    if path is None:
        file.unlink(missing_ok=True)
    else:
        file.write_text(path)


def sample(seconds: float, label: str) -> dict:
    """Missing asteroids per stats.txt second over this many seconds, by reason: never
    sent or removed (absent), no shape yet, sent but not drawn (budget or culling)"""
    result = dict(samples=0, expected=0, absent=0, noshape=0, undrawn=0, waiting=0)
    worst = 0.0
    last_tick = None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.25)
        totals, impostors = CLIENT.stats()
        if not totals or totals.get("tick") == last_tick or "camera" not in totals:
            continue
        last_tick = totals["tick"]
        want = expected(totals)
        if not want:
            continue
        missing = 0
        for id in want:
            i = impostors.get(id)
            if i is None:
                result["absent"] += 1
            elif i["shape"] == "none":
                result["noshape"] += 1
            elif i["drawn"] != "1":
                result["undrawn"] += 1
            else:
                if i["shape"].split("/")[1] != i["lod"]:
                    result["waiting"] += 1
                continue
            missing += 1
        result["samples"] += 1
        result["expected"] += len(want)
        worst = max(worst, missing / len(want))
    gaps = result["absent"] + result["noshape"] + result["undrawn"]
    result["gap"] = gaps / max(1, result["expected"])
    result["worst"] = worst
    totals, _ = CLIENT.stats()
    for key in ("removed", "restored", "stored", "storedkb", "culled", "overbudget"):
        result[key] = totals.get(key)
    print(
        f"{label}: {result['samples']} samples, {result['expected']} expected, "
        f"gap {result['gap']:.3f} (worst second {worst:.3f}): absent {result['absent']}, "
        f"no shape {result['noshape']}, not drawn {result['undrawn']}, "
        f"drawn at a coarser level waiting for credit {result['waiting']}; client totals "
        + " ".join(f"{k}={result[k]}" for k in ("removed", "restored", "stored", "storedkb"))
        + f" culled={result['culled']} overbudget={result['overbudget']}",
        flush=True,
    )
    return result


def server_minutes() -> list[str]:
    log = SERVER.game_log()
    if log is None:
        return []
    return re.findall(
        r"Impostors of player \d+ in the last minute: .*", log.read_text(errors="replace")
    )


def builds() -> int:
    log = SERVER.game_log()
    return log.read_text(errors="replace").count("Impostor build:") if log else 0


def start():
    if SURVIVAL:
        world.SETTINGS["GameMode"] = "Survival"
        world.SETTINGS["EnableSpectator"] = "true"
    world.prepare(
        SERVER.world,
        CLIENT.steam_id,
        CLIENT.name,
        grids=(),
        asteroids=tuple(r.asteroid for r in ROCKS),
    )
    SERVER.prepare([] if SURVIVAL else [CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(MaxDistance=MAX_DISTANCE, AsteroidOcclusion=False)
    )
    SERVER.start()
    CLIENT.launch(SERVER.port)
    api = CLIENT.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    return api


def pan(yaw: float, rate: float, pitch: float = -7.0, x=0.0, y=0.0, z=0.0) -> str:
    return f"{x} {y} {z} {yaw} {pitch} {rate} 0 0 0"


def warm_up() -> None:
    """Sweeps both bands until every asteroid has its shapes, so the measured runs see
    gaps from the view, not from first builds"""
    for fov in (FOV_WIDE, FOV_ZOOMED):
        CLIENT.set_fov(fov)
        deadline = time.monotonic() + 900
        last = -1
        while time.monotonic() < deadline:
            fly(pan(-70, 10 if fov == FOV_WIDE else 4))
            time.sleep(140 / (10 if fov == FOV_WIDE else 4))
            count = builds()
            print(f"warm-up fov {fov}: {count} builds", flush=True)
            if count == last:
                break
            last = count
    fly(None)
    CLIENT.set_fov(None)


def run() -> None:
    warm_up()
    results = {}

    # Zoomed pans back and forth across the ring, at a slow and a fast rate. The keep time
    # and the cache are what the second pass over the same rocks tests.
    CLIENT.set_fov(FOV_ZOOMED)
    for rate in (5, 20):
        for direction in (1, -1):
            fly(pan(-60 if direction > 0 else 60, rate * direction))
            time.sleep(2)
            results[f"zoomed pan {rate} deg/s {'+' if direction > 0 else '-'}"] = sample(
                120 / rate - 3, f"zoomed pan {rate} deg/s {'right' if direction > 0 else 'left'}"
            )
    fly(None)
    CLIENT.set_fov(None)
    time.sleep(5)

    # Fast flight over the near band, wide view, looking down at it ahead
    fly("0 3000 30000 0 -10 0 0 0 -2000")
    time.sleep(2)
    results["spectator flight 2 km/s"] = sample(20, "spectator flight 2 km/s")
    fly("0 3000 60000 0 -5 0 0 0 -8000")
    time.sleep(1)
    results["spectator flight 8 km/s"] = sample(6, "spectator flight 8 km/s")
    fly(None)

    for name, r in results.items():
        check(f"{name} gap under {MAX_GAP}", r["gap"] <= MAX_GAP, f"{r['gap']:.3f}")
    time.sleep(61)
    for line in server_minutes()[-4:]:
        print("server:", line)


def main() -> int:
    try:
        start()
        run()
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            fly(None)
            CLIENT.stop()
            SERVER.stop()
    return failures


if __name__ == "__main__":
    sys.exit(main())
