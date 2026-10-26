"""How fast a server builds the impostors of an asteroid ring seen for the first time (SE-678).
A client joins the "Octahedron - Seed 2" world with empty shape caches and no asteroid pack
and looks along the asteroid ring from above, like the measure run of tests/asteroid_pack.py.
Needs the save (se1/worlds/octahedron/build_world.py --seed 2) and its Workshop mods
downloaded, like tests/ring.py.

    IMP_TASK=imp678 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/build_throughput.py
    IMP_HEIGHT=10000 IMP_SETTLE=180 ...   # meters above the ring, seconds to watch
    IMP_TAG=before ...                    # names the run in the output
    ... python tests/build_throughput.py logs <server logs>   # the summary of saved logs
    IMP_MIN_RATE=30 ...                   # PASS limit, builds per second at the busiest
    IMP_BUILD_MS=0.1 ...                  # PASS limit, game thread ms per build

Prints the impostors the client has every 5 seconds, then a summary from the server log: the
asteroid builds, the bursts they came in (the client's spawn and its teleport each bring new
asteroids into view; a burst ends at a 3 second gap), each with its builds and duration, the
most builds in 5 seconds, and the game thread time of starting the builds from the server's
once a minute lines (per build and the most in one tick). The PASS/FAIL line checks that the
server built at least IMP_MIN_RATE asteroids a second in its busiest 5 seconds and that
starting them cost the game thread at most IMP_BUILD_MS per build.
"""

from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asteroid_pack  # noqa: E402
import rig  # noqa: E402
import ring  # noqa: E402

HEIGHT = float(os.environ.get("IMP_HEIGHT", "10000"))
SETTLE = float(os.environ.get("IMP_SETTLE", "180"))
TAG = os.environ.get("IMP_TAG", "run")
MIN_RATE = float(os.environ.get("IMP_MIN_RATE", "30"))
BUILD_MS = float(os.environ.get("IMP_BUILD_MS", "0.1"))

SERVER = ring.SERVER
CLIENT = ring.CLIENT

STAMP = r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)"


def stamp(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def summary(text: str) -> dict:
    """Asteroid builds over time and the game thread time, from the server log"""
    times = sorted(
        stamp(m.group(1))
        for m in re.finditer(STAMP + r".*Impostor build: .*kind=Voxel", text, re.M)
    )
    result = {"builds": len(times), "bursts": [], "peak": 0.0}
    for t in times:
        if result["bursts"] and t - result["bursts"][-1][1] <= 3:
            result["bursts"][-1][1] = t
            result["bursts"][-1][2] += 1
        else:
            result["bursts"].append([t, t, 1])
    for i, t in enumerate(times):
        j = i
        while j < len(times) and times[j] - t < 5:
            j += 1
        result["peak"] = max(result["peak"], (j - i) / 5)
    minutes = [
        (float(total), float(most))
        for total, most in re.findall(
            r"starting the builds took ([0-9.]+) ms on the game thread, ([0-9.]+) ms the most in a tick",
            text,
        )
    ]
    result["game_ms"] = sum(t for t, _ in minutes)
    result["tick_ms"] = max((m for _, m in minutes), default=float("nan"))
    result["minutes"] = len(minutes)
    snapshots = [
        float(s)
        for s in re.findall(r"Impostor build: .*kind=Voxel .* snapshot=([0-9.]+)", text)
    ]
    result["snapshot_ms"] = sum(snapshots)
    return result


def main() -> int:
    ids = asteroid_pack.setup_world(False)
    SERVER.write_plugin_config(
        rig.plugin_config(
            MaxDistance=500000, MaxImpostorsPerPlayer=4000, MaxQuadsPerFrame=16000
        )
    )
    center, normal, side, _ = ring.ring_geometry(SERVER.world)
    SERVER.start(timeout=3600)
    for cache in CLIENT.mod_storage.glob("cache-*"):
        cache.unlink()
    CLIENT.launch(SERVER.port)
    asteroid_pack.stage(CLIENT.appdata / "Mods", ids)
    counts = []
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        base = center + side * 120000
        tangent = np.cross(normal, side)
        api.character_teleport(*(base + normal * (HEIGHT + 300)))
        time.sleep(5)
        api.character_look_at(*(base + tangent * (HEIGHT + 5000)), tolerance=2)
        start = time.monotonic()
        while time.monotonic() - start < SETTLE:
            time.sleep(5)
            totals, _ = CLIENT.stats()
            t = time.monotonic() - start
            counts.append((t, totals.get("impostors")))
            print(f"{TAG} t={t:.0f}s impostors={totals.get('impostors')}", flush=True)
        # The once a minute line of the last minute
        time.sleep(65)
        text = asteroid_pack.log_text()
    finally:
        CLIENT.stop()
        SERVER.stop()

    r = summary(text)
    at = {
        s: next((n for t, n in counts if t >= s - 2.5), None) for s in (10, 20, 30, 60)
    }
    per_build = r["game_ms"] / max(r["builds"], 1)
    bursts = ", ".join(f"{n} in {end - begin:.1f} s" for begin, end, n in r["bursts"])
    print(
        f"{TAG}: {r['builds']} asteroid builds in bursts of {bursts}; {r['peak']:.1f} builds/s "
        f"in the busiest 5 s; client impostors at 10/20/30/60 s after look_at: "
        f"{at[10]}/{at[20]}/{at[30]}/{at[60]}; game thread {r['game_ms']:.1f} ms in "
        f"{r['minutes']} minutes, {per_build:.3f} ms per build, {r['tick_ms']:.2f} ms the most "
        f"in a tick, voxel snapshots {r['snapshot_ms']:.1f} ms; load average "
        f"{os.getloadavg()[0]:.1f}",
        flush=True,
    )
    ok = r["builds"] > 0 and r["peak"] >= MIN_RATE and per_build <= BUILD_MS
    print(
        f"{'PASS' if ok else 'FAIL'} {TAG}: at least {MIN_RATE:.0f} builds/s "
        f"({r['peak']:.1f}), at most {BUILD_MS} ms of game thread per build ({per_build:.3f})",
        flush=True,
    )
    return 0 if ok else 1


def offline(paths: list[str]) -> int:
    """The summary of saved server logs, without a game"""
    for path in paths:
        r = summary(Path(path).read_text(errors="replace"))
        bursts = ", ".join(
            f"{n} in {end - begin:.1f} s" for begin, end, n in r["bursts"]
        )
        print(
            f"{path}: {r['builds']} builds in bursts of {bursts}; {r['peak']:.1f} builds/s at "
            f"the busiest; game thread {r['game_ms'] / max(r['builds'], 1):.3f} ms per build, "
            f"{r['tick_ms']:.2f} ms the most in a tick"
        )
    return 0


if __name__ == "__main__":
    sys.exit(offline(sys.argv[2:]) if sys.argv[1:2] == ["logs"] else main())
