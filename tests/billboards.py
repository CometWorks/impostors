"""In-game check of the billboard limit (SE-664): the game draws at most 32768
billboards per frame and cuts off the rest, PostPP and late LDR ones first.
The client test probe (tests/clientprobe) adds PostPP and LDR billboards until
the frame is over the limit and says after every frame which billboards fall
beyond the drawn count.

    uv run --no-project --with httpx --with pillow python tests/billboards.py
    IMP_PLUGIN=1 ...    # the client runs the client plugin, the world has no mod

With the mod only, the impostors keep within the server's MaxQuadsPerFrame and
the game cuts other billboards, which the plugin is there to prevent. With the
client plugin, no other billboard is cut: in a steady frame the impostors take
only the room left (the adaptive budget), and when the stress billboards come
and go every half second, the plugin's patch drops impostor quads instead.
Prints one PASS or FAIL line per check and exits with the number of failures.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402

PLUGIN = os.environ.get("IMP_PLUGIN") == "1"
LIMIT = 32768
# BillboardPriority.Margin of the client plugin
MARGIN = 2048
# The mod's quad budget in its run is low enough to cut some impostors
MOD_QUADS = 1500

SERVER = rig.Server()
CLIENT = rig.Client(1, "Alpha", plugin=PLUGIN, probe=True)
STRESS = CLIENT.appdata / "billboards.txt"
STATS = CLIENT.appdata / "billboard-stats.txt"

failures = 0


def check(name: str, ok: bool, detail="") -> bool:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip(), flush=True)
    failures += not ok
    return ok


def probe_stats() -> dict:
    """The probe's numbers of the last half second"""
    try:
        text = STATS.read_text()
    except FileNotFoundError:
        return {}
    fields = dict(kv.split("=", 1) for kv in text.split())
    low, high = fields.pop("impostorsdrawn").split("-")
    values = {k: int(v) for k, v in fields.items()}
    values["impostors_min"] = int(low) if low != str(2**31 - 1) else 0
    values["impostors_max"] = int(high)
    return values


def samples(seconds: float) -> list[dict]:
    """One set of probe numbers per half second, for this long"""
    result, last = [], None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(0.5)
        s = probe_stats()
        if s and s != last:
            result.append(s)
            last = s
    return result


def stress(postpp: int = 0, ldr: int = 0, pulse: int = 0) -> None:
    STRESS.write_text(f"postpp {postpp} ldr {ldr} pulse {pulse}\n")


def summary(rows: list[dict]) -> str:
    keys = ("maxtotal", "maxdrawn", "cutothers", "cutpostpp", "cutimpostors")
    parts = [f"{k} {max(r[k] for r in rows)}" for k in keys]
    parts.append(
        f"impostors {min(r['impostors_min'] for r in rows)}"
        f"-{max(r['impostors_max'] for r in rows)}"
    )
    return f"{len(rows)} samples, max: " + ", ".join(parts)


def start():
    world.prepare(
        SERVER.world,
        CLIENT.steam_id,
        CLIENT.name,
        grids=world.GRIDS + world.ZONE_GRIDS,
        zones=world.ZONES,
        mod=not PLUGIN,
    )
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(
            RebuildSeconds=10,
            RefreshSeconds=20,
            MaxQuadsPerFrame=20000 if PLUGIN else MOD_QUADS,
        )
    )
    SERVER.start()
    STRESS.parent.mkdir(parents=True, exist_ok=True)
    stress()
    STATS.unlink(missing_ok=True)
    CLIENT.launch(SERVER.port)
    api = CLIENT.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    return api


def impostor_quads(timeout: float = 240.0) -> tuple[int, int]:
    """Impostor quads per frame once they stop growing, and the other billboards"""
    deadline = time.monotonic() + timeout
    best, since = 0, time.monotonic()
    while time.monotonic() < deadline:
        rows = samples(5)
        if not rows:
            if time.monotonic() - since > 60:
                break
            continue
        quads = max(r["impostors_max"] for r in rows)
        if quads > best:
            best, since = quads, time.monotonic()
        elif best and time.monotonic() - since > 30:
            break
    rows = samples(3)
    if not rows:
        raise RuntimeError(f"No probe stats in {STATS}")
    others = max(r["maxtotal"] - r["impostors_max"] for r in rows)
    return best, others


def run(api):
    api.character_teleport(*world.HOME)
    log = lambda: SERVER.game_log().read_text(errors="replace")
    deadline = time.monotonic() + 90
    while f"Player {CLIENT.steam_id} draws impostors" not in log():
        if time.monotonic() > deadline:
            break
        time.sleep(2)

    quads, vanilla = impostor_quads()
    check(
        "impostors drawn",
        quads > (1000 if PLUGIN else 100),
        f"{quads} quads, {vanilla} others",
    )
    if PLUGIN:
        run_plugin(quads, vanilla)
    else:
        run_mod(vanilla, quads)


def run_mod(vanilla: int, quads: int):
    # Over the limit by half of the impostor quads
    room = LIMIT - vanilla - quads // 2
    stress(postpp=room // 2, ldr=room - room // 2)
    time.sleep(3)
    rows = samples(10)
    print("steady stress:", summary(rows))
    totals, _ = CLIENT.stats()
    check(
        "the mod keeps within MaxQuadsPerFrame",
        int(totals.get("quads", 0)) <= int(totals.get("maxquads", 0)) == MOD_QUADS
        and max(r["impostors_max"] for r in rows) <= MOD_QUADS,
        totals,
    )
    check(
        "the budget cuts some impostors",
        int(totals.get("drawn", 0)) < int(totals.get("impostors", 0)),
        totals,
    )
    # Without the plugin the game cuts other billboards, PostPP first
    check(
        "the game cuts PostPP billboards (no plugin)",
        min(r["cutpostpp"] for r in rows) > 0,
        summary(rows),
    )


def run_plugin(quads: int, vanilla: int):
    # Room for half of the impostors after the margin
    room = LIMIT - vanilla - MARGIN - quads // 2
    stress(postpp=room // 2, ldr=room - room // 2)
    time.sleep(3)
    rows = samples(10)
    print("steady stress:", summary(rows))
    check(
        "steady: no other billboard cut",
        all(r["cutothers"] == 0 for r in rows),
        summary(rows),
    )
    drawn = max(r["impostors_max"] for r in rows)
    check(
        "steady: the adaptive budget makes room",
        0 < drawn < quads and all(r["maxtotal"] <= LIMIT for r in rows),
        f"{drawn} of {quads} impostor quads",
    )

    # Every 30 ticks the stress billboards come back, before the budget knows, and the
    # frame is over the limit by half of the impostor quads
    room = LIMIT - vanilla - quads // 2
    lines = lambda: re.findall(
        r"Billboards in the last minute: .*", CLIENT.log.read_text(errors="replace")
    )
    before = len(lines())
    stress(postpp=room // 2, ldr=room - room // 2, pulse=30)
    rows = samples(70)
    print("pulsed stress:", summary(rows))
    check(
        "pulsed: no other billboard cut",
        all(r["cutothers"] == 0 for r in rows),
        summary(rows),
    )
    # An empty frame between two bursts gives the whole budget back
    back = sum(r["impostors_max"] == quads for r in rows[4:])
    check(
        "pulsed: the impostors come back between bursts", back >= 10, f"{back} samples"
    )
    # The plugin logs a summary every minute, the second one after the pulses started
    # covers only pulsed frames
    deadline = time.monotonic() + 90
    while len(lines()) < before + 2 and time.monotonic() < deadline:
        time.sleep(2)
    new = lines()[before:]
    for line in new:
        print("client:", line)
    over = re.search(
        r"over \d+ in (\d+)(?: of \d+)? frames, (\d+) impostor quads dropped",
        new[-1] if new else "",
    )
    check(
        "pulsed: the patch dropped impostor quads, burst after burst",
        len(new) >= 2
        and over is not None
        and int(over.group(1)) >= 20
        and int(over.group(2)) > 0,
        new[-1:],
    )
    stress()


def main():
    try:
        run(start())
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


if __name__ == "__main__":
    main()
