"""In-game check of impostors standing in while a grid, safe zone or asteroid streams
in (SE-672): the player jumps from far away to next to it, deep inside the
replication cube, like a ship arriving at 10 km/s. The impostor must stay and be
drawn until the real entity draws itself, at the level it had from afar, then
hide and go. Same rig as scenario.py. Prints one PASS or FAIL line per check and
the timings, and exits with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/streaming.py
    IMP_WITH_PLUGIN=1 ...      # the client also runs the client plugin, which tells
                                 the mod when an asteroid's meshes show
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import world  # noqa: E402
from scenario import CLIENT, SERVER, check, command, drawn, wait_stats  # noqa: E402

import rig  # noqa: E402
import scenario  # noqa: E402

if os.environ.get("IMP_WITH_PLUGIN") == "1":
    # The world keeps the mod: the plugin's copy of the renderer stands down
    CLIENT = scenario.CLIENT = rig.Client(1, "Alpha", plugin=True)

# Each target: where the player waits far away (beyond the sync distance, outside the
# replication cube), and where it arrives (inside the cube, next to the target)
STATION = world.STATION
ZONE = world.ZONE
ROCK = world.ROCK_FAR
TARGETS = (
    (
        STATION,
        (STATION.position[0] - 5000, 0.0, 0.0),
        (STATION.position[0] - 400, 0.0, 0.0),
    ),
    (
        ZONE,
        (ZONE.position[0], ZONE.position[1] + 5500, ZONE.position[2]),
        (ZONE.position[0], ZONE.position[1] + 700, ZONE.position[2]),
    ),
    # Saved asteroids replicate to 8.66 km per axis: from HOME it is 11 km away
    (ROCK, world.HOME, (ROCK.center[0] + 1000, ROCK.center[1], ROCK.center[2])),
)


def arrive(api, target, far, near, timeout=75.0):
    """Waits far away for the impostor, jumps next to the target and samples the client's
    stats until the impostor is gone. Returns the level from afar and the samples: seconds
    since the jump and the impostor's stats (None once removed)."""
    api.character_teleport(*far)
    totals, impostors = wait_stats(lambda i: drawn(i, target), 120)
    if not check(f"{target.name} impostor from afar", drawn(impostors, target)):
        return None, [], 0
    time.sleep(5)  # finer levels for the new distance
    lod = CLIENT.stats()[1].get(target.entity_id, {}).get("lod")

    reported = int(CLIENT.stats()[0].get("readysent", 0))
    api.character_teleport(*near)
    start = time.monotonic()
    samples = []
    while time.monotonic() - start < timeout:
        totals, impostors = CLIENT.stats()
        i = impostors.get(target.entity_id)
        samples.append((time.monotonic() - start, i, totals))
        if i is None:
            break
        time.sleep(0.5)
    return lod, samples, reported


def first(samples, predicate):
    return next((t for t, i, _ in samples if predicate(i)), None)


def run(api):
    for target, far, near in TARGETS:
        lod, samples, reported = arrive(api, target, far, near)
        if not samples:
            continue
        scenario.SHOTS.mkdir(parents=True, exist_ok=True)
        (scenario.SHOTS / f"streaming-{target.name}.json").write_text(
            json.dumps([(t, i, totals) for t, i, totals in samples], indent=1)
        )
        real = first(samples, lambda i: i and i.get("real") == "1")
        gone = first(samples, lambda i: i is None)
        ready = first(samples, lambda i: i and i.get("ready") == "1")
        if ready is None and gone is not None:
            # Ready and removed between two stats: the Ready id went out before the removal
            if real is not None and int(samples[-1][2].get("readysent", 0)) > reported:
                ready = gone
        held = [i for t, i, _ in samples if ready is None or t < ready]
        print(
            f"{target.name}: level from afar {lod}, real entity at {real} s, "
            f"drawing at {ready} s, impostor removed at {gone} s, "
            f"stand-ins seen {max(int(s[2].get('standins', 0)) for s in samples)}, "
            f"readyus {max(int(s[2].get('readyus', 0)) for s in samples)}"
        )
        check(
            f"{target.name} impostor kept until the real one draws",
            ready is not None and all(i is not None for i in held),
            f"removed at {gone} s, ready at {ready} s",
        )
        check(
            f"{target.name} impostor drawn meanwhile",
            all(i and i.get("drawn") == "1" for i in held),
            [i.get("drawn") if i else "removed" for i in held],
        )
        check(
            f"{target.name} level from afar kept",
            all(i and i.get("lod") == lod for i in held),
            [i.get("lod") if i else "removed" for i in held],
        )
        after = [i for t, i, _ in samples if ready is not None and t >= ready]
        check(
            f"{target.name} impostor hidden, then removed",
            bool(after)
            and all(i is None or i.get("drawn") == "0" for i in after)
            and after[-1] is None,
            after[-3:],
        )
    print("server:", command(api, "!iprobe stats"))


def main():
    try:
        api = scenario.start()
        run(api)
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    print(
        f"{'PASSED' if scenario.failures == 0 else 'FAILED'}: {scenario.failures} failure(s)"
    )
    sys.exit(scenario.failures)


if __name__ == "__main__":
    main()
