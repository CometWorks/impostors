"""No-render check (SE-1407): a headless client started with --no-render has the
game's null renderer and no billboard pools, where the game's
AddBillboardOriented threw a NullReferenceException in the mod's Draw and
crashed the client. A Magnetar server with the plugin, one --no-render client
with the mod. The client must get the far grids' shapes, which the mod would
draw, keep running for a while after that, log that it draws nothing, and have
no NullReferenceException in its log. Prints one PASS or FAIL line per check
and exits with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/no_render.py
    IMP_KEEP=1 ...                             # leave the game running
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from scenario import check, wait  # noqa: E402

import scenario  # noqa: E402

SERVER = rig.Server()
CLIENT = rig.Client(1, "Alpha", render=False)
scenario.CLIENT = CLIENT
# Draw frames to survive once the mod has shapes to draw
HOLD_SECONDS = 30


def client_log() -> str:
    return "\n".join(
        p.read_text(errors="replace")
        for p in (CLIENT.launch_log, CLIENT.log)
        if p.exists()
    )


def run():
    CLIENT.launch(SERVER.port)
    api = CLIENT.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    api.character_teleport(*world.HOME)

    log = lambda: SERVER.game_log().read_text(errors="replace")
    check(
        "hello",
        wait(lambda: f"Player {CLIENT.steam_id} draws impostors" in log(), 90),
    )
    far = (world.SHIP, world.STATION, world.FAR)
    has_shape = lambda i: all(
        i.get(g.entity_id, {}).get("shape", "none") != "none" for g in far
    )
    totals, impostors = scenario.wait_stats(has_shape, 120)
    check("far shapes", has_shape(impostors), totals)
    time.sleep(HOLD_SECONDS)
    api.ping()


def main():
    world.prepare(SERVER.world, CLIENT.steam_id, CLIENT.name, mod=True)
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(rig.plugin_config(RebuildSeconds=10))
    try:
        SERVER.start()
        try:
            run()
            check("run", True)
        except Exception as e:  # noqa: BLE001 -- a crashed client fails any step
            check("run", False, repr(e))
        text = client_log()
        check("null renderer", "using MyNullRender" in text)
        check("client alive", CLIENT.pid() is not None)
        check(
            "drawing off logged",
            "Impostors: the game has no billboard pools" in text,
        )
        check("no NullReferenceException", "NullReferenceException" not in text)
        totals, _ = CLIENT.stats()
        check("no quads drawn", int(totals.get("quads", 0)) == 0, totals)
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    print(f"client log {CLIENT.log}")
    print(
        f"{'PASSED' if scenario.failures == 0 else 'FAILED'}: {scenario.failures} failure(s)"
    )


if __name__ == "__main__":
    main()
    sys.exit(scenario.failures)
