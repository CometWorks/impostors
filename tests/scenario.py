"""In-game check of grid impostors on a vanilla client: a Magnetar server with
the Impostors plugin, one headless client without it, the Impostors mod from
the world's mod list. Prints one PASS or FAIL line per check and exits with
the number of failures. Screenshots land in ~/.se-test/<task>-shots.

    uv run --no-project --with httpx --with pillow python tests/scenario.py
    IMP_KEEP=1 ...                             # leave the game running
    IMP_PLUGIN=1 ...                           # the client runs the client plugin
                                                 and the world has no mod
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402

PLUGIN = os.environ.get("IMP_PLUGIN") == "1"
SERVER = rig.Server()
CLIENT = rig.Client(1, "Alpha", plugin=PLUGIN)
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"

failures = 0


def check(name: str, ok: bool, detail="") -> bool:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip(), flush=True)
    failures += not ok
    return ok


def wait(predicate, timeout: float, interval: float = 2.0):
    """The predicate's last value, once truthy or at the timeout"""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


def wait_stats(predicate, timeout: float, interval: float = 2.0):
    """The client's stats once predicate(impostors) holds, or the last ones at the timeout"""
    deadline = time.monotonic() + timeout
    while True:
        totals, impostors = CLIENT.stats()
        if predicate(impostors) or time.monotonic() > deadline:
            return totals, impostors
        time.sleep(interval)


def command(api, text: str, timeout: float = 20.0) -> str:
    """A chat command of the probe and its reply"""
    seen = {
        (m.get("text"), m.get("timestamp"))
        for m in api.get_chat_messages().get("hud") or []
    }
    api.send_chat(text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for m in api.get_chat_messages().get("hud") or []:
            if (m.get("text"), m.get("timestamp")) not in seen and m.get("sender") in (
                "iprobe",
                "ImpostorsProbe",
            ):
                return m.get("text")
        time.sleep(0.5)
    raise TimeoutError(f"No reply to {text!r}")


def shot(api, name: str, target) -> Path:
    api.character_look_at(*target, tolerance=1)
    time.sleep(2)
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    path.write_bytes(api.screenshot_bytes(ignore_sprites=True))
    return path


def shades(path: Path) -> int:
    """Colors covering at least 5 pixels at the screen center, where the impostor the camera
    looks at is: single bright pixels are stars or antialiased edges"""
    from collections import Counter

    from PIL import Image

    image = Image.open(path).convert("RGB")
    w, h = image.size
    center = image.crop((w // 2 - 30, h // 2 - 30, w // 2 + 30, h // 2 + 30))
    counts = Counter(c for c in center.getdata() if sum(c) > 90)
    return sum(1 for n in counts.values() if n >= 5)


def drawn(impostors: dict, grid: world.Grid) -> bool:
    i = impostors.get(grid.entity_id)
    return bool(i) and i["drawn"] == "1" and i["shape"] != "none"


def start():
    world.prepare(SERVER.world, CLIENT.steam_id, CLIENT.name, mod=not PLUGIN)
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(RebuildSeconds=10, RefreshSeconds=20, MaxQuadsPerFrame=20000)
    )
    SERVER.start()
    CLIENT.launch(SERVER.port)
    api = CLIENT.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    return api


def run(api):
    api.character_teleport(*world.HOME)
    time.sleep(3)

    # The server learns that the client draws impostors from its hello
    log = lambda: SERVER.game_log().read_text(errors="replace")
    check(
        "hello", wait(lambda: f"Player {CLIENT.steam_id} draws impostors" in log(), 90)
    )

    far = (world.SHIP, world.STATION, world.FAR)
    totals, impostors = wait_stats(lambda i: all(drawn(i, g) for g in far), 120)
    for g in far:
        check(
            f"impostor {g.name}",
            drawn(impostors, g),
            impostors.get(g.entity_id, "missing"),
        )
    check(
        "no impostor Near",
        world.NEAR.entity_id not in impostors,
        impostors.get(world.NEAR.entity_id, ""),
    )

    # Asteroids: the far one is beyond the range vanilla replicates saved asteroids to
    rock = world.ROCK_FAR
    totals, impostors = wait_stats(lambda i: drawn(i, rock), 90)
    check(
        "impostor RockFar",
        drawn(impostors, rock) and impostors[rock.entity_id]["kind"] == "Voxel",
        impostors.get(rock.entity_id, "missing"),
    )
    check(
        "no impostor RockNear",
        world.ROCK_NEAR.entity_id not in impostors,
        impostors.get(world.ROCK_NEAR.entity_id, ""),
    )
    check("quads drawn", int(totals.get("quads", 0)) > 0, totals)
    check("quad budget from the server", totals.get("maxquads") == "20000", totals)
    print("server:", command(api, "!iprobe stats"))

    for g in far + (world.NEAR,):
        print("screenshot", shot(api, f"home-{g.name}", g.position))
    for a in world.ASTEROIDS:
        print("screenshot", shot(api, f"home-{a.name}", a.center))

    # Seen along an axis, the far asteroid's impostor must not be one flat color
    tones = shades(SHOTS / f"home-{world.ROCK_FAR.name}.png")
    check("asteroid impostor shaded", tones >= 4, f"{tones} shades")

    # Closer, still beyond the sync distance: the impostor is larger on screen. Grids start
    # at level 16, which the station has at 8 km.
    station = world.STATION
    api.character_teleport(station.position[0] - 3000, 0, 0)
    totals, impostors = wait_stats(
        lambda i: drawn(i, station) and int(i[station.entity_id]["lod"]) > 16, 30
    )
    check(
        "station finer level at 3 km",
        int(impostors.get(station.entity_id, {}).get("lod", 0)) > 16,
        impostors.get(station.entity_id, "missing"),
    )
    print("screenshot", shot(api, "3km-Station", station.position))

    # Hand-off: next to the station its real grid replicates. The server stops sending the
    # impostor; while the client still has it, it must not draw it.
    api.character_teleport(station.position[0] - 1000, 0, 0)
    totals, impostors = wait_stats(
        lambda i: i.get(station.entity_id, {}).get("drawn") != "1", 60
    )
    i = impostors.get(station.entity_id, {})
    check("station impostor hidden near", i.get("drawn") != "1", i or "removed")
    print("screenshot", shot(api, "near-Station", station.position))

    api.character_teleport(*world.HOME)
    totals, impostors = wait_stats(
        lambda i: drawn(i, station) and i[station.entity_id]["real"] == "0", 90
    )
    check(
        "station impostor again",
        drawn(impostors, station),
        impostors.get(station.entity_id, "missing"),
    )

    # Motion: the ship moves, the client gets its new position
    ship = world.SHIP
    target = (ship.position[0] + 800, ship.position[1], ship.position[2])
    assert (
        command(
            api, f"!iprobe place {ship.entity_id} {target[0]} {target[1]} {target[2]}"
        )
        == "Probe place: done"
    )
    moved = (
        lambda i: abs(
            float(i.get(ship.entity_id, {}).get("pos", "0,0,0").split(",")[0])
            - target[0]
        )
        < 5
    )
    totals, impostors = wait_stats(moved, 30)
    check("ship moved", moved(impostors), impostors.get(ship.entity_id, "missing"))

    # Content: repainting the station rebuilds it, a new version reaches the client
    version = impostors.get(station.entity_id, {}).get("version")
    assert (
        command(api, f"!iprobe paint {station.entity_id} {world.YELLOW}")
        == "Probe paint: done"
    )
    changed = lambda i: i.get(station.entity_id, {}).get("version") not in (
        None,
        version,
    )
    totals, impostors = wait_stats(changed, 150, 5)
    i = impostors.get(station.entity_id, {})
    check(
        "station repainted",
        changed(impostors)
        and i.get("shape", "").startswith(i.get("version", "?") + "/"),
        i,
    )
    print("screenshot", shot(api, "home-Station-repainted", station.position))


def run_plugin(api):
    """The client plugin's copy of the renderer, on a world without the mod. The copy
    writes no stats: its hello shows it runs, the server's sent list and the screenshots
    show the rest."""
    api.character_teleport(*world.HOME)
    time.sleep(3)
    log = lambda: SERVER.game_log().read_text(errors="replace")
    check(
        "hello", wait(lambda: f"Player {CLIENT.steam_id} draws impostors" in log(), 90)
    )

    far = (world.SHIP, world.STATION, world.FAR)
    sent = lambda: command(api, "!iprobe stats").split("viewers=")[-1]
    reply = wait(lambda: all(str(g.entity_id) in sent() for g in far) and sent(), 90)
    check("server sent the far grids", bool(reply), reply)
    for g in far:
        print("screenshot", shot(api, f"plugin-home-{g.name}", g.position))


def main():
    try:
        api = start()
        (run_plugin if PLUGIN else run)(api)
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


if __name__ == "__main__":
    main()
