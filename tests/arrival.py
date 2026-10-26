"""Arriving next to a station of the "Octahedron - Seed 2" world (SE-709), the way a Trade
Lane drops a ship out of its tunnel: the player waits 20 km out until the impostors of the
station's grids and safe zone show, then jumps to 600 m from the zone's center, deep inside
the replication cube. Until the real grids draw, their impostors stand in, and they must show
the full level for their distance, not the small one from afar. Needs the save and its
Workshop mods, like tests/ring.py.

    IMP_TASK=imp709 uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/arrival.py
    IMP_STATIONS="Alpha Station,North Station" ...   # the stations to arrive at (three)
    IMP_ZONE_LOWER=1 ...      # SafeZoneLowerLevel on: from 600 m, outside the zone, the
                                grids inside it are one level lower; a second arrival
                                inside the zone must show them at their full level

Each grid's line has its level from afar, the levels it stood in at after the jump, the
full level the service picks for that distance (from the server's entry, rig.full_level) and
when its real grid drew. PASS or FAIL lines per grid: kept and drawn until the real grid
draws, never coarser than from afar, at the expected level from PICK_DELAY seconds after the
jump. Exits with the number of failures. Screenshots of the arrival go to
~/.se-test/<task>-shots: from afar, and once the stand-ins have their new level.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import ring  # noqa: E402
from asteroid_pack import command, setup_world  # noqa: E402

rig.CLIENT_GIB = 9.0
SERVER = ring.SERVER
CLIENT = ring.CLIENT
SHOTS = ring.SHOTS
ZONE_LOWER = os.environ.get("IMP_ZONE_LOWER") == "1"
STATIONS = os.environ.get(
    "IMP_STATIONS", "Alpha Station,North Station,Gamma Station"
).split(",")
# Seconds after the jump by which the stand-ins show their new level: the server picks once
# a second, the mod writes its stats once a second
PICK_DELAY = 2.5
failures = 0


def check(name: str, ok: bool, detail="") -> bool:
    global failures
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}".rstrip(), flush=True)
    failures += not ok
    return ok


def zones(world: Path) -> dict[str, tuple[tuple[float, float, float], float]]:
    """The stations' safe zones by name: center and radius"""
    text = (world / "SANDBOX_0_0_0_.sbs").read_text(encoding="utf-8")
    found = {}
    for m in re.finditer(
        r'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_SafeZone">(.*?)</MyObjectBuilder_EntityBase>',
        text,
        re.S,
    ):
        body = m.group(1)
        name = re.search(r"<DisplayName>([^<]*)</DisplayName>", body)
        position = re.search(r'<Position x="([^"]+)" y="([^"]+)" z="([^"]+)"', body)
        radius = re.search(r"<Radius>([^<]+)</Radius>", body)
        if name and position and radius:
            found[name.group(1)] = (
                tuple(float(v) for v in position.groups()),
                float(radius.group(1)),
            )
    return found


def along(center, direction, distance):
    return tuple(c + d * distance for c, d in zip(center, direction))


def shape_lod(i: dict | None) -> int | None:
    if not i or i.get("shape", "none") == "none":
        return None
    return int(i["shape"].split("/")[1])


def ready_log(totals: dict) -> dict[int, tuple[int, int]]:
    log = {}
    for item in totals.get("readylog", "").split(","):
        if item:
            id_, drawable, ready, _ = item.split(":")
            log[int(id_)] = (int(drawable), int(ready))
    return log


def shot(api, name: str) -> Path:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    path.write_bytes(api.screenshot_bytes(ignore_sprites=True))
    return path


def arrive(api, station: str, center, radius: float, distance: float) -> None:
    """Waits 20 km out, jumps to this distance from the zone's center and samples the
    client's stats until the station's stand-ins are gone"""
    outward = [c / math.dist(center, (0, 0, 0)) for c in center]
    tag = f"{station.replace(' ', '-')}-{distance:.0f}m"
    api.character_teleport(*along(center, outward, 20000))
    api.character_look_at(*center, tolerance=1)
    entries = {}
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        reply = command(api, f"!iprobe near {center[0]} {center[1]} {center[2]} 1500")
        entries = {
            i: e for i, e in rig.parse_near(reply).items() if e["kind"] == "Grid"
        }
        _, impostors = CLIENT.stats()
        if entries and all(impostors.get(i, {}).get("drawn") == "1" for i in entries):
            break
        time.sleep(3)
    time.sleep(8)  # the finer levels for 20 km
    _, impostors = CLIENT.stats()
    afar = {i: shape_lod(impostors.get(i)) for i in entries}
    if not check(
        f"{tag} grid impostors from afar",
        bool(entries) and all(afar.values()),
        afar,
    ):
        return
    print("screenshot", shot(api, f"arrival-{tag}-afar"), flush=True)

    eye = along(center, outward, distance)
    inside = distance <= radius
    if inside:
        # Of the 26 directions to the cube's faces, edges and corners, the one that keeps
        # farthest from the grids' bounding spheres
        directions = [
            (x / n, y / n, z / n)
            for x in (-1, 0, 1)
            for y in (-1, 0, 1)
            for z in (-1, 0, 1)
            if (n := math.sqrt(x * x + y * y + z * z))
        ]
        eye = max(
            (along(center, d, distance) for d in directions),
            key=lambda p: min(
                math.dist(p, e["center"]) - e["radius"] for e in entries.values()
            ),
        )
    expected = {}
    for i, e in entries.items():
        full = rig.full_level(
            "Grid", e["radius"], math.dist(eye, e["center"]), e["built"]
        )
        # Seen through the zone's surface with the rule on: one level lower, unless that
        # level isn't built
        lower = ZONE_LOWER and not inside and math.dist(e["center"], center) <= radius
        expected[i] = rig.LODS[max(rig.LODS.index(full) - 1, 0)] if lower else full

    api.character_teleport(*eye)
    start = time.monotonic()
    api.character_look_at(*center, tolerance=1)
    samples = []
    shot_path = None
    while time.monotonic() - start < 90:
        totals, impostors = CLIENT.stats()
        now = time.monotonic() - start
        samples.append(
            (
                now,
                int(totals.get("tick", 0)),
                {i: impostors.get(i) for i in entries},
                totals,
            )
        )
        if all(impostors.get(i) is None for i in entries):
            break
        if shot_path is None and now >= PICK_DELAY:
            shot_path = shot(api, f"arrival-{tag}-standin")
        time.sleep(0.5)
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / f"arrival-{tag}.json").write_text(
        json.dumps(samples, indent=1, default=str)
    )
    print("screenshot", shot_path, flush=True)

    log = ready_log(samples[-1][3])
    jump = samples[0][1]
    seconds = lambda t: None if t is None else round((t - jump) / 60, 2)
    for i in sorted(entries):
        drawable, ready = log.get(i, (None, None))
        before = [s for s in samples if ready is None or s[1] < ready]
        levels = [(round(s[0], 1), shape_lod(s[2][i])) for s in before]
        late = [lv for t, lv in levels if t >= PICK_DELAY]
        print(
            f"{tag} grid {i} ({entries[i]['radius']:.0f} m): level from afar {afar[i]}, "
            f"expected {expected[i]}, stood in at {sorted({lv for _, lv in levels if lv})}, "
            f"drawable at {seconds(drawable)} s, drawing at {seconds(ready)} s",
            flush=True,
        )
        check(
            f"{tag} grid {i} kept and drawn until its real grid draws",
            ready is not None
            and all(
                s[2][i] is not None and s[2][i].get("drawn") == "1" for s in before
            ),
            [None if s[2][i] is None else s[2][i].get("drawn") for s in before][:8],
        )
        check(
            f"{tag} grid {i} never coarser than from afar",
            all(lv is None or lv >= afar[i] for _, lv in levels),
            levels[:8],
        )
        check(
            f"{tag} grid {i} at level {expected[i]} for its distance",
            bool(late) and all(lv == expected[i] for lv in late),
            levels[:8] if late else f"real one drew within {PICK_DELAY} s: {levels}",
        )


def main() -> int:
    ids = setup_world(with_pack=False)
    found = zones(SERVER.world)
    SERVER.write_plugin_config(
        rig.plugin_config(MaxDistance=500000, SafeZoneLowerLevel=ZONE_LOWER)
    )
    SERVER.start(timeout=3600)
    CLIENT.launch(SERVER.port)
    ring.stage_mods(CLIENT.appdata / "Mods", ids)
    try:
        api = CLIENT.wait_api(timeout=3600)
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        for station in STATIONS:
            center, radius = found[station]
            arrive(api, station, center, radius, 600)
            if ZONE_LOWER:
                # Inside the zone, but clear of the station's grids
                arrive(api, station, center, radius, radius - 20)
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    return failures


if __name__ == "__main__":
    sys.exit(main())
