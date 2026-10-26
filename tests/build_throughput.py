"""How fast a server builds the impostors of an asteroid ring seen for the first time (SE-678).
A client joins the "Octahedron - Seed 2" world with empty shape caches and no asteroid pack
and looks along the asteroid ring from above, like the measure run of tests/asteroid_pack.py.
Needs the save (se1/worlds/octahedron/build_world.py --seed 2) and its Workshop mods
downloaded, like tests/ring.py.

    IMP_TASK=imp678 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/build_throughput.py
    IMP_HEIGHT=10000 IMP_SETTLE=180 ...   # meters above the ring, seconds to watch
    IMP_TAG=before ...                    # names the run in the output
    IMP_FILL_SECONDS=30 ...               # PASS limit for 90% of the builds
    IMP_TICK_MS=4 ...                     # PASS limit for the game thread in one tick

Prints the impostors the client has every 5 seconds, then a summary from the server log: the
asteroid builds, how long after the first one 50%, 90% and all of them were done, builds per
second, and the game thread time of starting the builds from the server's once a minute
lines (all of it and the most in one tick). The PASS/FAIL line checks that 90% of the builds
were done within IMP_FILL_SECONDS and that no tick spent more than IMP_TICK_MS on them.
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
FILL_SECONDS = float(os.environ.get("IMP_FILL_SECONDS", "30"))
TICK_MS = float(os.environ.get("IMP_TICK_MS", "4"))

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
    result = {"builds": len(times)}
    if times:
        first = times[0]
        at = lambda share: times[max(0, int(np.ceil(share * len(times))) - 1)] - first
        result.update(
            t50=at(0.5),
            t90=at(0.9),
            t100=times[-1] - first,
            rate=len(times) / max(times[-1] - first, 1e-3),
        )
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
    print(
        f"{TAG}: {r['builds']} asteroid builds, 50% in {r.get('t50', 0):.1f} s, 90% in "
        f"{r.get('t90', 0):.1f} s, all in {r.get('t100', 0):.1f} s after the first, "
        f"{r.get('rate', 0):.1f} builds/s; client impostors at 10/20/30/60 s after look_at: "
        f"{at[10]}/{at[20]}/{at[30]}/{at[60]}; game thread {r['game_ms']:.1f} ms in "
        f"{r['minutes']} minutes, {r['tick_ms']:.2f} ms the most in a tick, voxel snapshots "
        f"{r['snapshot_ms']:.1f} ms; load average {os.getloadavg()[0]:.1f}",
        flush=True,
    )
    ok = r["builds"] > 0 and r.get("t90", 1e9) <= FILL_SECONDS and r["tick_ms"] <= TICK_MS
    print(
        f"{'PASS' if ok else 'FAIL'} {TAG}: 90% of the builds within {FILL_SECONDS:.0f} s "
        f"({r.get('t90', 0):.1f} s), at most {TICK_MS} ms in a tick ({r['tick_ms']:.2f} ms)",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
