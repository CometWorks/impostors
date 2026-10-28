"""Planet occlusion (SE-738): an impostor whose center is behind a planet's sea level sphere,
seen from the camera, isn't drawn. The world is planet_shadow.py's: the EarthLike planet (60 km
sea level, hills to 67.2 km) at its PLANET. The camera is 160 km from the planet's center,
looking at it, with grids around it:

- behind, behind-off: behind the planet, the line of sight passes its center at 0 and 20 km
- limb: the line of sight passes at 63 km, above sea level but below the hilltops; terrain
  doesn't count, so it's drawn
- side: past the limb at 87 km, drawn
- front: between the camera and the planet, drawn

PASS per grid when its stats row says occluded and not drawn, or the other way round. It also
prints what the test costs, microseconds per impostor, and saves a screenshot.

The billboards are drawn far to near (ImpostorRenderer.Draw sorts its parts by distance) since
they test depth but never write it, so that order is checked in the code, not here.

    IMP_TASK=se738 uv run --no-project --with httpx --with pillow python tests/planet_occlusion.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import planet_shadow as ps  # noqa: E402
import rig  # noqa: E402
import world  # noqa: E402

EYE = ps.add(ps.PLANET, (0.0, 0.0, 160000.0))

# Name, position relative to the planet's center, occluded
CASES = [
    ("behind", (0.0, 0.0, -150000.0), True),
    ("behind-off", (40000.0, 0.0, -150000.0), True),
    ("limb", (132800.0, 0.0, -150000.0), False),
    ("side", (200000.0, 0.0, -150000.0), False),
    ("front", (30000.0, 0.0, 100000.0), False),
]

GRIDS = [
    world.Grid(
        name,
        120000000000000200 + i,
        ps.add(ps.PLANET, offset),
        ps.GRID_SIZE,
        True,
        world.YELLOW,
    )
    for i, (name, offset, _) in enumerate(CASES)
]


def main() -> int:
    world.SETTINGS.update(SyncDistance="20000", ViewDistance="50000")
    ps.GRIDS = GRIDS
    ps.prepare_world()
    ps.SERVER.prepare([ps.CLIENT.steam_id])
    ps.SERVER.write_plugin_config(
        rig.plugin_config(
            ReplicationMargin=100000, MinPixels=0, MaxDistance=400000, MinBlocks=1
        )
    )
    ps.SHOTS.mkdir(parents=True, exist_ok=True)
    ok = True
    try:
        ps.SERVER.start()
        ps.CLIENT.launch(ps.SERVER.port)
        api = ps.CLIENT.wait_api()
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        api.character_teleport(*EYE)
        time.sleep(5)
        character = api.get_character()
        if not character.get("jetpack"):
            api.key("X")
        if not character.get("dampeners"):
            api.character_set_dampeners(True)

        def shaped():
            api.character_look_at(*ps.PLANET, tolerance=0.5)
            rows = ps.CLIENT.stats()[1]
            return (
                all(
                    rows.get(g.entity_id, {}).get("shape", "none") != "none"
                    for g in GRIDS
                )
                and rows
            )

        ps.wait_for(shaped, 180, "the impostors of every grid")
        time.sleep(3)
        totals, rows = ps.CLIENT.stats()
        shot = ps.SHOTS / "planet-occlusion.png"
        shot.write_bytes(api.screenshot_bytes(ignore_sprites=True))
        for grid, (name, _, expected) in zip(GRIDS, CASES):
            row = rows.get(grid.entity_id, {})
            occluded, drawn = row.get("occluded") == "1", row.get("drawn") == "1"
            good = occluded == expected and drawn != expected
            ok &= good
            print(
                f"{'PASS' if good else 'FAIL'} {name}: occluded {int(occluded)}, drawn {int(drawn)}, "
                f"expected {'occluded' if expected else 'drawn'}",
                flush=True,
            )
        us, count = int(totals.get("occlusionus", 0)), int(totals.get("occlusions", 0))
        print(
            f"occlusion: {totals.get('occluded')} occluded this frame, {count} tests in {us} us "
            f"over the last second, {us / max(count, 1):.3f} us per impostor; screenshot {shot}",
            flush=True,
        )
    finally:
        ps.CLIENT.stop()
        ps.SERVER.stop()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
