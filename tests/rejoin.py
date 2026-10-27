"""A client that joins again in the same process must get its impostors again
(SE-728): the client joins the scenario world, the server restarts, the client
drops to the main menu and DirectTransport joins it again without restarting
the game. Each session must take the server's Welcome, which the client logs
with its shape cache line, and receive shapes. The world has the mod and the
client runs the client plugin too, as Pulsar players on a modded server do.
Prints one PASS or FAIL line per check and exits with the number of failures.

    IMP_TASK=se728 uv run --no-project --with httpx --with pillow python tests/rejoin.py
    IMP_REJOINS=2 ...    # how many times the server restarts
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from scenario import SERVER, check, wait  # noqa: E402

CLIENT = rig.Client(1, "Alpha", plugin=True)
REJOINS = int(os.environ.get("IMP_REJOINS", "2"))
WELCOMED = " KB in the shape cache"


def client_log() -> str:
    return CLIENT.log.read_text(errors="replace") if CLIENT.log.exists() else ""


def server_log() -> str:
    log = SERVER.game_log()
    return log.read_text(errors="replace") if log else ""


def session(api, index: int, welcomes_before: int) -> int:
    """Checks one session of the client; returns the Welcome lines so far"""
    rig.wait_world(api)
    rig.ensure_character(api)
    api.character_teleport(*world.HOME)
    hello = f"Player {CLIENT.steam_id} draws impostors"
    check(
        f"session {index}: the server hears the client",
        bool(wait(lambda: hello in server_log(), 120)),
    )
    welcomes = wait(
        lambda: client_log().count(WELCOMED) > welcomes_before
        and client_log().count(WELCOMED),
        120,
    )
    check(
        f"session {index}: the client takes the Welcome",
        bool(welcomes),
        f"{client_log().count(WELCOMED) - welcomes_before} shape cache line(s)",
    )

    # The stats are of the renderer that draws: shapes it received or took from
    # its caches in this session
    def served():
        totals, impostors = CLIENT.stats()
        got = sum(int(totals.get(k, 0)) for k in ("shapes", "cached", "packed"))
        return got > 0 and impostors and (totals, impostors)

    stats = wait(served, 120)
    if stats:
        totals, impostors = stats
        detail = " ".join(
            f"{k}={totals.get(k)}" for k in ("shapes", "cached", "restored")
        )
        detail += f" impostors={len(impostors)}"
    else:
        detail = str(CLIENT.stats()[0])
    check(f"session {index}: the client has impostor shapes", bool(stats), detail)
    return client_log().count(WELCOMED)


def main():
    world.prepare(SERVER.world, CLIENT.steam_id, CLIENT.name, mod=True)
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(rig.plugin_config(RebuildSeconds=10, RefreshSeconds=20))
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        welcomes = session(api, 1, 0)
        for i in range(2, REJOINS + 2):
            pid = CLIENT.pid()
            SERVER.stop()
            # Rewritten by every session, so a stale one can't pass
            for stats in CLIENT.stats_files():
                stats.unlink(missing_ok=True)
            SERVER.start()
            back = wait(
                lambda: f"Player {CLIENT.steam_id} draws impostors" in server_log(),
                600,
                5,
            )
            check(
                f"session {i}: the client joined again in the same process",
                bool(back) and CLIENT.pid() == pid,
            )
            welcomes = session(CLIENT.wait_api(), i, welcomes)
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    import scenario

    print(
        f"{'PASSED' if scenario.failures == 0 else 'FAILED'}: {scenario.failures} failure(s)"
    )
    sys.exit(scenario.failures)


if __name__ == "__main__":
    main()
