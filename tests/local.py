"""In-game check of the client plugin's service for the local player (SE-668): a
headless client with the Impostors client plugin loads the test world without the
mod, offline or as the host of a lobby game. The plugin loads the mod itself, and
the mod draws for the local player (SE-670). Prints one PASS or FAIL line per check
and exits with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/local.py
    IMP_MODE=lobby ...   # host a lobby (DirectTransport) and let a second client join
    IMP_WORLD_MOD=1 ...  # the world lists the mod, the plugin loads no second copy
    IMP_KEEP=1 ...       # leave the game running

Offline the game draws everything within the view distance (15 km), so only the
grid and the asteroid beyond it get impostors. A lobby host draws only within the
sync distance (3 km, MySector.LoadData), so it gets the far grids and asteroids
too, and the joiner gets them over mod networking as before.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402

LOBBY = os.environ.get("IMP_MODE") == "lobby"
WORLD_MOD = os.environ.get("IMP_WORLD_MOD") == "1"
HOST = rig.Client(1, "Alpha", plugin=True)
JOINER = rig.Client(2, "Beta", plugin=True)
LOBBY_PORT = rig.SLOT["DS_SERVER_PORT_BASE"]
WORLD = HOST.appdata / "Saves" / "Impostors local"

# Beyond the view distance: offline only impostors can show these
BEYOND = world.Grid(
    "Beyond", 120000000000000021, (0.0, 0.0, 25000.0), (20, 12, 40), True, world.GREEN
)
ROCK_BEYOND = world.Asteroid(
    "RockBeyond", 130000000000000021, (19744.0, -256.0, -256.0), "Asteroid512-001.vx2"
)
NEAR_GRIDS = (world.SHIP, world.STATION, world.FAR)

failures = 0


def check(name: str, ok: bool, detail="") -> bool:
    global failures
    failures += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'} {name}{': ' + str(detail) if detail else ''}")
    return ok


def wait(predicate, timeout: float, interval: float = 3.0):
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if result or time.monotonic() > deadline:
            return result
        time.sleep(interval)


def has_shape(impostors: dict, entity_id: int) -> bool:
    """The renderer has the impostor with a shape. Whether it draws it also depends on
    where the camera looks, as it culls what is outside the view."""
    return impostors.get(entity_id, {}).get("shape", "none") != "none"


def shown(client: rig.Client, ids, timeout: float = 180) -> dict:
    """Waits until the client's renderer has a shape for every id; its stats per id"""
    ids = set(ids)

    def drawn():
        _, impostors = client.stats()
        ok = all(has_shape(impostors, i) for i in ids)
        return impostors if ok else None

    return wait(drawn, timeout) or client.stats()[1]


def past_questions(api) -> bool:
    """Answers the game's question about a world saved without its binary files;
    True once the respawn screen or the world is up"""
    try:
        screens = api.list_screens()
        for screen in screens:
            if screen.get("type") == "MyGuiScreenMessageBox":
                for button in ("Yes", "OK"):
                    try:
                        api.control_click(text=button, screen=screen["index"])
                        break
                    except Exception:  # noqa: BLE001 -- not this button
                        continue
        types = [screen.get("type", "") for screen in screens]
        return any("Medical" in t for t in types) or api.get_state().get("ready", False)
    except Exception:  # noqa: BLE001 -- the API times out while a world loads
        return False


def start_host() -> rig.RemoteAPI:
    world.prepare(
        WORLD,
        HOST.steam_id,
        HOST.name,
        grids=world.GRIDS + (BEYOND,),
        asteroids=world.ASTEROIDS + (ROCK_BEYOND,),
        mod=WORLD_MOD,
    )
    # No --connect: the host loads the world through Remote
    args = HOST.args
    HOST.args = lambda _port: [
        *[a for a in args(0) if a not in ("--connect", "127.0.0.1:0")],
        *(["--host-lobby", LOBBY_PORT] if LOBBY else []),
    ]
    HOST.launch(0)
    api = HOST.wait_api()
    api.load(str(WORLD), online_mode="FRIENDS" if LOBBY else "OFFLINE")
    # Offline the game asks whether to load a world saved without its binary files, and
    # the session is not ready before the player spawns
    wait(lambda: past_questions(api), 600, 2)
    rig.ensure_character(api)
    rig.wait_world(api)
    rig.focus_gameplay(api)
    api.character_teleport(*world.HOME)
    return api


def check_mod_draws() -> None:
    """The mod's renderer draws for the local player, the plugin's copy stands down"""
    log = HOST.log.read_text(errors="replace")
    check(
        (
            "the plugin loads the mod"
            if not WORLD_MOD
            else "the plugin loads no second mod"
        ),
        ("Loading the Impostors mod from" in log) != WORLD_MOD,
    )
    check("the mod draws", "Impostors: the mod draws for the local player" in log)
    check(
        "the plugin's renderer stands down",
        "Impostors: the client plugin draws for the local player" not in log,
    )
    stats = HOST.mod_storage if WORLD_MOD else HOST.loaded_mod_storage
    check("the mod writes the stats", (stats / "stats.txt").exists(), stats)
    check(
        "the plugin's renderer writes none",
        not (HOST.plugin_storage / "Impostors.stats.txt").exists(),
    )


def run_offline() -> None:
    log = lambda: HOST.log.read_text(errors="replace")
    check(
        "service serves the local player",
        bool(wait(lambda: "The local player draws impostors" in log(), 120)),
    )
    impostors = shown(HOST, (BEYOND.entity_id, ROCK_BEYOND.entity_id))
    for thing in (BEYOND, ROCK_BEYOND):
        check(
            f"{thing.name} shown",
            has_shape(impostors, thing.entity_id),
        )
    within = [g.name for g in world.GRIDS + world.ASTEROIDS if g.entity_id in impostors]
    check("nothing within the view distance", not within, within)
    check_mod_draws()


def run_lobby(host_api) -> None:
    log = lambda: HOST.log.read_text(errors="replace")
    check(
        "service serves the host's own player",
        bool(wait(lambda: "The local player draws impostors" in log(), 120)),
    )
    far = NEAR_GRIDS + world.ASTEROIDS + (BEYOND, ROCK_BEYOND)
    impostors = shown(HOST, (t.entity_id for t in far))
    for thing in far:
        check(
            f"host shows {thing.name}",
            has_shape(impostors, thing.entity_id),
        )
    check("host has no impostor of Near", world.NEAR.entity_id not in impostors)
    check_mod_draws()

    JOINER.args = lambda _port, args=JOINER.args: [
        *[a for a in args(0) if a not in ("--connect", "127.0.0.1:0")],
        "--join-lobby",
        f"127.0.0.1:{LOBBY_PORT}",
    ]
    JOINER.launch(0)
    api = JOINER.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    api.character_teleport(*world.HOME)
    check(
        "service serves the joiner",
        bool(wait(lambda: f"Player {JOINER.steam_id} draws impostors" in log(), 120)),
    )
    impostors = shown(JOINER, (g.entity_id for g in NEAR_GRIDS))
    for g in NEAR_GRIDS:
        check(f"joiner shows {g.name}", has_shape(impostors, g.entity_id))


def main():
    try:
        api = start_host()
        run_lobby(api) if LOBBY else run_offline()
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            JOINER.stop()
            HOST.stop()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


if __name__ == "__main__":
    main()
