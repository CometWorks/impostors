"""The asteroid ring of the Octahedron world seen from above North Station (SE-670), 135 to
231 km away: in an offline game with the client plugin, which loads the mod itself, or on a
Magnetar server with the mod. Both should give the same impostors. Needs what tests/ring.py
needs.

    IMP_TASK=imp670 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/station.py
    IMP_MODE=server ...       # a Magnetar server and a client with the mod only
    IMP_STATION=South ...     # above South Station instead
    IMP_SETTLE=900 ...        # seconds before the first view, while the rocks are built

Prints one line per view, looking at the ring in four directions: the impostors the client
has, those in the view and drawn, their quads and the levels of the drawn ones. Ends with
PASS when every view drew impostors. Screenshots and stats go to ~/.se-test/<task>-shots.
"""

from __future__ import annotations

import collections
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

MODE = os.environ.get("IMP_MODE", "offline")
# ring.py runs the client plugin, and leaves the mod out of the world, with IMP_PLUGIN=1
os.environ["IMP_PLUGIN"] = "1" if MODE == "offline" else "0"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import local  # noqa: E402
import rig  # noqa: E402
import ring  # noqa: E402

STATION = os.environ.get("IMP_STATION", "North") + " Station"
SETTLE = float(os.environ.get("IMP_SETTLE", "900"))
HEIGHT = 1500.0
CONFIG = rig.plugin_config(
    MaxDistance=500000, MaxImpostorsPerPlayer=4000, MaxQuadsPerFrame=16000
)
CLIENT = ring.CLIENT
SERVER = ring.SERVER
SHOTS = ring.SHOTS


def station_position(world: Path) -> np.ndarray:
    text = (world / "SANDBOX_0_0_0_.sbs").read_text(encoding="utf-8")
    m = re.search(f"<DisplayName>{STATION}</DisplayName>", text)
    start = text.rfind("<MyObjectBuilder_EntityBase", 0, m.start())
    p = re.search(
        r'<Position x="([^"]+)" y="([^"]+)" z="([^"]+)"', text[start : m.start()]
    )
    return np.array([float(v) for v in p.groups()])


def start_offline() -> tuple[rig.RemoteAPI, Path]:
    world = CLIENT.appdata / "Saves" / ring.WORLD_NAME
    ids = ring.copy_world(world)
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world / name
        f.write_text(
            f.read_text(encoding="utf-8").replace(
                "<OnlineMode>PUBLIC</OnlineMode>", "<OnlineMode>OFFLINE</OnlineMode>"
            ),
            encoding="utf-8",
        )
    (CLIENT.appdata / "Impostors.cfg").parent.mkdir(parents=True, exist_ok=True)
    (CLIENT.appdata / "Impostors.cfg").write_text(CONFIG, encoding="utf-8")
    args = CLIENT.args
    CLIENT.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    CLIENT.launch(0)
    ring.stage_mods(CLIENT.appdata / "Mods", ids)
    api = CLIENT.wait_api()
    api.load(str(world), online_mode="OFFLINE")
    local.wait(lambda: local.past_questions(api), 900, 2)
    return api, world


def start_server() -> tuple[rig.RemoteAPI, Path]:
    ids = ring.copy_world(SERVER.world)
    SERVER.prepare([CLIENT.steam_id])
    cfg = SERVER.data / "SpaceEngineers-Dedicated.cfg"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "<WorldName>ImpostorsTest</WorldName>",
            f"<WorldName>{ring.WORLD_NAME}</WorldName>",
        ),
        encoding="utf-8",
    )
    ring.stage_mods(SERVER.data / "Mods", ids)
    SERVER.write_plugin_config(CONFIG)
    SERVER.start(timeout=3600)
    CLIENT.launch(SERVER.port)
    ring.stage_mods(CLIENT.appdata / "Mods", ids)
    return CLIENT.wait_api(), SERVER.world


def view(api, name: str) -> bool:
    totals, impostors = CLIENT.stats()
    levels = collections.Counter(
        int(i["shape"]) for i in impostors.values() if i.get("drawn") == "1"
    )
    drawn = int(totals.get("drawn", 0))
    print(
        f"{MODE} {name} impostors={totals.get('impostors')} inview={totals.get('inview')} "
        f"drawn={drawn} quads={totals.get('quads')} budget={totals.get('budget')} "
        f"levels={dict(sorted(levels.items()))}",
        flush=True,
    )
    tag = f"{MODE}-{name}"
    (SHOTS / f"{tag}.png").write_bytes(api.screenshot_bytes(ignore_sprites=True))
    stats = next(p for p in CLIENT.stats_files() if p.exists())
    shutil.copy(stats, SHOTS / f"{tag}-stats.txt")
    return drawn > 0


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        api, world = start_offline() if MODE == "offline" else start_server()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        center, normal, side, points = ring.ring_geometry(world)
        radius = float(np.linalg.norm(points - center, axis=1).mean())
        station = station_position(world)
        up = (station - center) / np.linalg.norm(station - center)
        eye = station + up * HEIGHT
        distances = np.linalg.norm(points - eye, axis=1)
        print(
            f"{STATION} {station.round(0)}, eye {HEIGHT:.0f} m above, ring {len(points)} "
            f"asteroids {distances.min() / 1000:.0f} to {distances.max() / 1000:.0f} km away",
            flush=True,
        )
        api.character_teleport(*eye)
        tangent = np.cross(normal, side)
        directions = {
            "side": side,
            "tangent": tangent,
            "-side": -side,
            "-tangent": -tangent,
        }
        ok = True
        for i, (name, direction) in enumerate(directions.items()):
            api.character_teleport(*eye)
            api.character_look_at(*(center + direction * radius), tolerance=1)
            time.sleep(SETTLE if i == 0 else 60)
            ok &= view(api, name)
    finally:
        CLIENT.stop()
        SERVER.stop()
    print("PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
