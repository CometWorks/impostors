"""Level changes of the ring asteroids in a still view, in the game (SE-1097): the "Octahedron -
Seed 2" world with the asteroid pack, offline with the client plugin as tests/ring_levels.py
runs it, or with IMP_MODE=server on an isolated Magnetar that a client with the plugin joins.
The character stands at some of tests/ring_budget.py's spots, the camera still, and once a
second the rig reads each drawn asteroid's level from the stats. After a warm-up it counts the
level changes over IMP_SECONDS (150 by default); with a still view there should be none.

On the server the client must show the asteroids from the pack: none of the drawn ones is the
server's and the server announces none (the stats' notices). Then the server forgets the
player, as a cluster node does when the player leaves it for less than the client's Hello
interval (the probe's `forget`): within a minute the client must send its inventory again and
keep its asteroids, without the server sending them.

    IMP_TASK=se1097 IMP_RESOLUTION=1920x1080 IMP_SAVE=<"Octahedron - Seed 2" save> \\
        uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/still_levels.py <tag>
    IMP_MODE=server ...

IMP_VIEWS picks the views as spot-zoom, comma separated (along-back-1,outside-1,along-2 by
default). Files in ~/.se-test/<task>-still/<tag>: <view>.json with the levels per second, and
table.md. Prints PASS when no drawn asteroid changed its level and, on the server, the pack's
asteroids stayed the client's; FAIL otherwise.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asteroid_pack as ap  # noqa: E402
import rig  # noqa: E402
import ring  # noqa: E402
import ring_budget as rb  # noqa: E402
import ring_levels as rl  # noqa: E402

MODE = os.environ.get("IMP_MODE", "offline")
OUT = rig.TEST_ROOT / f"{rig.TASK}-still"
VIEWS = os.environ.get("IMP_VIEWS", "along-back-1,outside-1,along-2").split(",")
SECONDS = float(os.environ.get("IMP_SECONDS", "150"))
WARMUP = 20.0
rig.CLIENT_GIB = 10.0


def levels(client) -> tuple[dict, dict[int, str]]:
    totals, impostors = client.stats()
    return totals, {i: r["shape"] for i, r in rl.drawn(impostors).items()}


def served(client) -> int:
    """Drawn asteroids the server sent instead of the client picking them from the pack"""
    _, impostors = client.stats()
    return sum(r.get("early") == "0" for r in rl.drawn(impostors).values())


def watch(client, out: Path, view: str) -> str:
    """Levels per second for SECONDS after the warm-up, and the table row"""
    time.sleep(WARMUP)
    samples, last_tick = [], None
    start = time.monotonic()
    while time.monotonic() - start < SECONDS:
        time.sleep(1)
        totals, shown = levels(client)
        if totals.get("tick") == last_tick:
            continue
        last_tick = totals.get("tick")
        samples.append(
            {"t": time.monotonic() - start, "totals": totals, "shown": shown}
        )
    changes, changed, coarser = 0, set(), 0
    for a, b in zip(samples, samples[1:]):
        for i, shape in b["shown"].items():
            if i in a["shown"] and a["shown"][i] != shape:
                changes += 1
                changed.add(i)
        coarser = max(coarser, int(b["totals"].get("coarser", 0) or 0))
    (out / f"{view}.json").write_text(json.dumps(samples))
    drawn = len(samples[-1]["shown"]) if samples else 0
    return (
        f"| {view} | {len(samples)} | {drawn} | {changes} | {len(changed)} | "
        f"{samples[-1]['totals'].get('quads') if samples else ''} | {coarser} |"
    )


def forget(api, client, out: Path) -> list[str]:
    """The server forgets the player; the client must send its inventory again within a
    minute and keep showing its asteroids, none of them sent by the server. Problems found.
    """
    before, shown = levels(client)
    log = ap.SERVER.game_log()
    inventories = log.read_text(errors="replace").count(
        f"Player {client.steam_id} has "
    )
    print("server:", ap.command(api, f"!iprobe forget {client.steam_id}"), flush=True)
    low, samples = len(shown), []
    start = time.monotonic()
    while time.monotonic() - start < 60:
        time.sleep(1)
        totals, now = levels(client)
        low = min(low, len(now))
        samples.append(
            {"t": time.monotonic() - start, "totals": totals, "drawn": len(now)}
        )
    (out / "forget.json").write_text(json.dumps(samples))
    after, _ = levels(client)
    text = log.read_text(errors="replace")
    problems = []
    if text.count(f"Player {client.steam_id} has ") <= inventories:
        problems.append("no inventory after the server forgot the player")
    notices = int(after.get("notices", 0)) - int(before.get("notices", 0))
    if notices:
        problems.append(f"{notices} asteroids announced by the server")
    if low < 0.95 * len(shown):
        problems.append(f"drawn asteroids fell from {len(shown)} to {low}")
    count = served(client)
    if count:
        problems.append(f"{count} drawn asteroids sent by the server")
    print(
        f"forget: drawn {len(shown)}, lowest {low}, notices {notices}, served {count}, "
        f"{'; '.join(problems) or 'ok'}",
        flush=True,
    )
    return problems


def start_offline(client):
    import local

    world = rl.launch(client)
    api = client.wait_api()
    api.load(str(world), online_mode="OFFLINE")
    local.wait(lambda: local.past_questions(api), 900, 2)
    return api


def start_server(client):
    """The world with the pack on the slot's Magnetar, the client joining it with an empty
    shape cache"""
    ring.PLUGIN = False
    ap.CLIENT = client  # the admin on the server, for the probe's commands
    ids = ap.setup_world(True)
    ap.SERVER.write_plugin_config(
        rig.plugin_config(
            MaxDistance=500000, MaxImpostorsPerPlayer=4000, MaxQuadsPerFrame=16384
        )
    )
    ap.SERVER.start(timeout=3600)
    for cache in client.mod_storage.glob("cache-*"):
        cache.unlink()
    for name in ("camera.txt", "look.txt"):
        (client.appdata / name).unlink(missing_ok=True)
    client.set_fov(None)
    client.launch(ap.SERVER.port)
    ap.stage(client.appdata / "Mods", ids)
    api = client.wait_api()
    rig.wait_world(api, timeout=2400)
    return api


def run(out: Path) -> tuple[list[str], list[str]]:
    pos, _, _ = rb.read_index(ap.PACK / ap.PACK_FILE)
    spots = {name: (eye, forward) for name, eye, forward in rb.spots(pos)}
    client = rig.Client(2, "Viktor", plugin=True, quality="medium", probe=True)
    rows, problems = [], []
    try:
        api = start_server(client) if MODE == "server" else start_offline(client)
        rig.ensure_character(api)
        rig.wait_world(api, timeout=1200)
        rig.focus_gameplay(api)
        for view in VIEWS:
            spot, zoom = view.rsplit("-", 1)
            eye, forward = spots[spot]
            if MODE == "server":
                api.character_teleport(*eye)
                time.sleep(5)
                api.character_look_at(*(eye + forward * 5000), tolerance=1)
            else:
                rl.look(client, eye, forward)
            client.set_fov(math.degrees(rb.fov_for(int(zoom))))
            rows.append(watch(client, out, view))
            print(rows[-1], flush=True)
            if MODE == "server":
                count = served(client)
                if count:
                    problems.append(
                        f"{view}: {count} drawn asteroids sent by the server"
                    )
        if MODE == "server":
            problems += forget(api, client, out)
    finally:
        client.stop()
        if MODE == "server":
            ap.SERVER.stop()
    return rows, problems


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    out = OUT / sys.argv[1]
    out.mkdir(parents=True, exist_ok=True)
    rows, problems = run(out)
    table = (
        "| view | samples | drawn | level changes | asteroids changed | quads | coarser |\n"
        "|---|---|---|---|---|---|---|\n" + "\n".join(rows) + "\n"
    )
    (out / "table.md").write_text(table)
    print(table, flush=True)
    failed = any(int(r.split("|")[4]) for r in rows) or problems
    for problem in problems:
        print(problem, flush=True)
    print("FAIL" if failed else "PASS", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
