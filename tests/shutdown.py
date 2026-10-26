"""Server shutdown check (SE-676): the Magnetar server starts the impostor
service, then gets a SIGTERM. Magnetar disposes the plugins off the game
thread, where the game refuses to change message handlers. The log must have
no "Dispose failed", the service must log that it stopped, and the test probe,
disposed after Impostors, must find the service's instance and session
cleared. No client. Prints one PASS or FAIL line per check and exits with the
number of failures.

    uv run --no-project --with httpx --with pillow python tests/shutdown.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from scenario import CLIENT, SERVER, check, wait  # noqa: E402

import scenario  # noqa: E402


def server_log() -> str:
    log = SERVER.game_log()
    return log.read_text(errors="replace") if log else ""


def main():
    world.prepare(SERVER.world, CLIENT.steam_id, CLIENT.name)
    SERVER.prepare([])
    SERVER.write_plugin_config(rig.plugin_config())
    try:
        SERVER.start()
        started = wait(lambda: "Impostors service started" in server_log(), 120)
        check("service started", started)
    finally:
        SERVER.stop()
    # The server writes the last log lines while it exits
    time.sleep(2)
    text = server_log()
    check("no 'Dispose failed'", "Dispose failed" not in text)
    check("no handler thread error", "message handlers from another thread" not in text)
    stopped = text.find("Impostors service stopped")
    check("service stopped", stopped >= 0)
    probe = text.find("ImpostorsProbe: disposed")
    line = text[probe:].splitlines()[0] if probe >= 0 else ""
    check("probe disposed after Impostors", probe > stopped >= 0, line)
    check(
        "instance and session cleared",
        "instance=null session=null" in line,
        line,
    )
    print(f"log {SERVER.game_log()}")
    print(
        f"{'PASSED' if scenario.failures == 0 else 'FAILED'}: {scenario.failures} failure(s)"
    )
    sys.exit(scenario.failures)


if __name__ == "__main__":
    main()
