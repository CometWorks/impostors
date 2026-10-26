"""In-game check of impostors standing in while a grid, safe zone or asteroid streams
in (SE-672): the player jumps from far away to next to it, deep inside the
replication cube, like a ship arriving at 10 km/s. The impostor must stay and be
drawn until the real entity draws itself, never coarser than from afar and, once
the next pick sent it, at the full level for its new distance (SE-709), then
hide and go. Last the player jumps into the safe zone (SE-703), with
SafeZoneLowerLevel on: the grid inside it is tinted and lower from afar, but from
inside the same zone it stands in untinted at its full level. Same rig as
scenario.py. Prints one PASS or FAIL line per check and the timings, and exits
with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/streaming.py
    IMP_WITH_PLUGIN=1 ...      # the client also runs the client plugin, which tells
                                 the mod when an asteroid's meshes show
    IMP_ONLY=inside ...        # only the arrival inside the safe zone
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

PLUGIN = os.environ.get("IMP_WITH_PLUGIN") == "1"
# The zone checks need the safe zone rule on, the other targets are outside of zones
scenario.ZONE_LOWER = True
if PLUGIN:
    # The world keeps the mod: the plugin's copy of the renderer stands down
    CLIENT = scenario.CLIENT = rig.Client(1, "Alpha", plugin=True)

# Each target: where the player waits far away (beyond the sync distance, outside the
# replication cube), and where it arrives (inside the cube, next to the target)
STATION = world.STATION
ZONE = world.ZONE
ROCK = world.ROCK_FAR
SHIELDED = world.SHIELDED

# Seconds after the jump by which the held impostor shows its full level: the server picks
# once a second and the mod writes its stats once a second
PICK_DELAY = 2.5
TARGETS = (
    (
        STATION,
        (STATION.position[0] - 5000, 0.0, 0.0),
        (STATION.position[0] - 400, 0.0, 0.0),
    ),
    (
        ZONE,
        (ZONE.position[0], ZONE.position[1] + 5500, ZONE.position[2]),
        # Outside its bounding sphere, 866 m: from inside the impostor isn't drawn
        (ZONE.position[0], ZONE.position[1] + 950, ZONE.position[2]),
    ),
    # Saved asteroids replicate to 8.66 km per axis: from HOME it is 11 km away
    (ROCK, world.HOME, (ROCK.center[0] + 1000, ROCK.center[1], ROCK.center[2])),
)


def shape_lod(i: dict | None) -> int | None:
    """The level of the shape the client draws"""
    if not i or i.get("shape", "none") == "none":
        return None
    return int(i["shape"].split("/")[1])


def expected_level(api, target, eye) -> int | None:
    """The full level for this distance from the server's entry, None for a safe zone,
    whose shape has no levels"""
    point = target.center if isinstance(target, world.Asteroid) else target.position
    entries = rig.parse_near(
        command(api, f"!iprobe near {point[0]} {point[1]} {point[2]} 600")
    )
    e = entries.get(target.entity_id)
    if e is None or e["kind"] == "SafeZone":
        return None
    distance = sum((a - b) ** 2 for a, b in zip(eye, e["center"])) ** 0.5
    return rig.full_level(e["kind"], e["radius"], distance, e["built"])


def arrive(api, target, far, near, timeout=75.0, also=None):
    """Waits far away for the impostor, jumps next to the target and samples the client's
    stats until the impostor is gone. Returns the level from afar and the samples: seconds
    since the jump, the impostor's stats (None once removed), the totals and the stats of
    the impostor of `also`, whose stats from afar are in samples[0][3]["afar"]."""
    # Only impostors in view are drawn: the character looks at the target
    point = target.center if isinstance(target, world.Asteroid) else target.position
    api.character_teleport(*far)
    api.character_look_at(*point, tolerance=1)
    totals, impostors = wait_stats(lambda i: drawn(i, target), 120)
    if not check(f"{target.name} impostor from afar", drawn(impostors, target)):
        return None, []
    time.sleep(5)  # finer levels for the new distance
    impostors = CLIENT.stats()[1]
    lod = shape_lod(impostors.get(target.entity_id))
    afar = impostors.get(also.entity_id) if also else None

    api.character_teleport(*near)
    start = time.monotonic()
    api.character_look_at(*point, tolerance=1)
    samples = []
    while time.monotonic() - start < timeout:
        totals, impostors = CLIENT.stats()
        i = impostors.get(target.entity_id)
        other = {"now": impostors.get(also.entity_id) if also else None}
        if not samples:
            other["afar"] = afar
        samples.append((time.monotonic() - start, i, totals, other))
        if i is None:
            break
        time.sleep(0.5)
    return lod, samples


def ready_log(totals: dict, entity_id: int, after: int = 0):
    """The client's ticks when the target's real entity became drawable and when it counted
    as drawing, and whether the client plugin reported an asteroid's meshes; the last time
    after this tick, an entity that streamed in before has older entries"""
    found = None
    for entry in totals.get("readylog", "").split(","):
        if entry.startswith(f"{entity_id}:"):
            _, drawable, ready, loaded = entry.split(":")
            if int(ready) >= after:
                found = int(drawable), int(ready), loaded == "1"
    return found


def run(api):
    # IMP_ONLY=inside: only the arrival inside the zone
    for target, far, near in TARGETS if os.environ.get("IMP_ONLY") != "inside" else ():
        lod, samples = arrive(api, target, far, near)
        if not samples:
            continue
        scenario.SHOTS.mkdir(parents=True, exist_ok=True)
        (scenario.SHOTS / f"streaming-{target.name}.json").write_text(
            json.dumps(samples, indent=1)
        )
        # Client ticks, from the first stats after the jump (up to a second late)
        jump = int(samples[0][2].get("tick", 0))
        log = ready_log(samples[-1][2], target.entity_id, after=jump)
        drawable, ready, loaded = log or (None, None, False)
        tick = lambda s: int(s[2].get("tick", 0))
        gone = next((tick(s) for s in samples if s[1] is None), None)
        seconds = lambda t: None if t is None else round((t - jump) / 60, 2)
        full = expected_level(api, target, near)
        print(
            f"{target.name}: level from afar {lod}, full level {full}, drawable at {seconds(drawable)} s, "
            f"drawing at {seconds(ready)} s, impostor removed by {seconds(gone)} s, "
            f"meshes reported {loaded}, "
            f"readyus {max(int(s[2].get('readyus', 0)) for s in samples)}"
        )
        before = [s[1] for s in samples if ready is None or tick(s) < ready]
        after = [s[1] for s in samples if ready is not None and tick(s) >= ready + 10]
        check(
            f"{target.name} impostor kept until the real one draws",
            ready is not None and all(i is not None for i in before),
            f"ready at tick {ready}, removed by {gone}",
        )
        check(
            f"{target.name} impostor drawn meanwhile",
            all(i and i.get("drawn") == "1" for i in before),
            [i.get("drawn") if i else "removed" for i in before],
        )
        check_levels(target.name, lod, full, samples, ready)
        check(
            f"{target.name} impostor hidden, then removed",
            samples[-1][1] is None
            and all(i is None or i.get("drawn") == "0" for i in after),
            after[-3:],
        )
        if target is ROCK and PLUGIN:
            check("RockFar meshes reported by the client plugin", loaded, log)
    inside_zone(api)
    print("server:", command(api, "!iprobe stats"))


def check_levels(name, lod, full, samples, ready, stats=lambda s: s[1]):
    """Held, the impostor is never coarser than from afar, and has its full level from
    PICK_DELAY seconds after the jump until the real one draws"""
    tick = lambda s: int(s[2].get("tick", 0))
    before = [s for s in samples if ready is None or tick(s) < ready]
    levels = [(round(s[0], 1), shape_lod(stats(s))) for s in before]
    check(
        f"{name} never coarser than from afar",
        lod is not None and all(lv is None or lv >= lod for _, lv in levels),
        [lod] + levels,
    )
    if full is None:
        return
    late = [lv for t, lv in levels if t >= PICK_DELAY]
    check(
        f"{name} at the full level for its distance",
        bool(late) and all(lv == full for lv in late),
        (
            f"full {full}, {levels}"
            if late
            else f"real one drew within {PICK_DELAY} s: {levels}"
        ),
    )


def inside_zone(api):
    """SE-703: the player arrives inside a spherical zone while its impostor stands in. With
    the camera inside its bounding sphere the impostor isn't drawn, and the client keeps
    running. SE-709: Shielded, inside the same zone, is tinted from afar and stands in
    untinted, at its full level, from inside."""
    far = (ZONE.position[0], ZONE.position[1] + 5500, ZONE.position[2])
    inside = (ZONE.position[0], ZONE.position[1] + 200, ZONE.position[2])
    lod, samples = arrive(api, ZONE, far, inside, also=SHIELDED)
    if not samples:
        return
    scenario.SHOTS.mkdir(parents=True, exist_ok=True)
    (scenario.SHOTS / "streaming-inside.json").write_text(json.dumps(samples, indent=1))
    afar = samples[0][3]["afar"] or {}
    check("Shielded tinted from afar", "tint" in afar, afar)
    full = expected_level(api, SHIELDED, inside)
    log = ready_log(
        samples[-1][2], SHIELDED.entity_id, after=int(samples[0][2].get("tick", 0))
    )
    ready = log[1] if log else None
    print(
        f"Shielded: readylog {log}, sampled ticks {int(samples[0][2].get('tick', 0))} to {int(samples[-1][2].get('tick', 0))}"
    )
    shielded = lambda s: s[3].get("now")
    tick = lambda s: int(s[2].get("tick", 0))
    late = [
        shielded(s)
        for s in samples
        if (ready is None or tick(s) < ready) and s[0] >= PICK_DELAY and shielded(s)
    ]
    check(
        "Shielded untinted from inside the same zone",
        bool(late) and all("tint" not in i for i in late),
        [i.get("tint", "-") for i in late] or f"real one drew within {PICK_DELAY} s",
    )
    check_levels("Shielded", shape_lod(afar), full, samples, ready, stats=shielded)
    # Only stats written after the jump: a client that crashed leaves its last ones
    fresh = [s[1] for s in samples if tick(s) > tick(samples[0])]
    check(
        "Zone impostor not drawn with the camera inside",
        bool(fresh) and all(i is None or i.get("drawn") != "1" for i in fresh),
        [i.get("drawn") if i else "removed" for i in fresh],
    )
    now = lambda: int(CLIENT.stats()[0].get("tick", 0))
    before = now()
    time.sleep(3)
    check("client still drawing after the zone streamed in", now() > before)


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
