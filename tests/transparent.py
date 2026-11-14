"""In-game check that impostors draw behind the transparent things in front of them
(SE-1541): a transparent LCD showing a script and the flames of two thrusters, with
three asteroids 60 km behind them that only impostors show. Offline, a headless client
with the Impostors client plugin, like tests/local.py.

    IMP_TASK=se1541 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/transparent.py
    IMP_TAG=before ...   # prefix of the screenshots in ~/.se-test/<task>-shots
    IMP_KEEP=1 ...       # leave the game running

It shoots the view without the impostors (the renderer's "real" mode), with them drawn
the old way in the LDR pass (the plugin's Impostors.ontop file) and with them drawn
before the transparent pass. Where the shot without them is very bright there is an LCD
sprite or a flame, and with the impostors behind them those pixels stay as they are.
Offline the game draws the real asteroids too, dimmer than that. Prints one PASS or FAIL
line per check and exits with the number of failures.
"""

from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import local  # noqa: E402
import rig  # noqa: E402
import world  # noqa: E402
from se_remote import SetOp  # noqa: E402

TAG = os.environ.get("IMP_TAG", "shot")
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"
HOST = rig.Client(1, "Alpha", plugin=True, probe=True)
WORLD = HOST.appdata / "Saves" / "Impostors transparent"
# With this file the client plugin leaves the impostors in the LDR pass
ON_TOP = HOST.plugin_storage / "Impostors.ontop"

# One behind the LCD and one behind each flame, as seen from HOME
ROCKS = tuple(
    world.Asteroid(
        f"Rock{i}", 130000000000000031 + i, (60000.0 - 256, y - 256, z - 256), file
    )
    for i, (y, z, file) in enumerate(
        (
            (0.0, 0.0, "Asteroid512-001.vx2"),
            (-470.0, 1470.0, "Asteroid512-013.vx2"),
            (-470.0, -1470.0, "Asteroid512-001.vx2"),
        )
    )
)
# The grid sits between HOME and the rocks, its row of blocks across the view
GRID_AT = (300.0, 0.0, 0.0)
BATTERY, LCD = (0, 0, 0), (0, 1, 0)
# Two cells long each, by their min cell, with the way the flame points
THRUSTERS = {(-2, 0, 0): "Left", (1, 0, 0): "Right"}
FOV = 2.0
# Brighter than this without the impostors: an LCD sprite or a flame
BRIGHT = 200
# A covered pixel differs by more than this
CHANGED = 60


def grid_xml() -> str:
    def block(kind, subtype, cell, forward="Forward", extra=""):
        x, y, z = cell
        return (
            f'<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_{kind}">'
            f"<SubtypeName>{subtype}</SubtypeName>"
            f'<Min x="{x}" y="{y}" z="{z}" />'
            f'<BlockOrientation Forward="{forward}" Up="Up" />'
            f"<Enabled>true</Enabled>{extra}</MyObjectBuilder_CubeBlock>"
        )

    blocks = (
        block(
            "BatteryBlock",
            "LargeBlockBatteryBlock",
            BATTERY,
            extra="<CurrentStoredPower>3</CurrentStoredPower>"
            "<MaxStoredPower>3</MaxStoredPower>",
        )
        + block("TextPanel", "TransparentLCDLarge", LCD)
        + "".join(
            block("Thrust", "LargeBlockSmallThrust", cell, flame)
            for cell, flame in THRUSTERS.items()
        )
    )
    return (
        '<?xml version="1.0"?>'
        '<Definitions xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><ShipBlueprints>'
        '<ShipBlueprint xsi:type="MyObjectBuilder_ShipBlueprintDefinition">'
        '<Id Type="MyObjectBuilder_ShipBlueprintDefinition" Subtype="SE1541" />'
        "<CubeGrids><CubeGrid><SubtypeName /><EntityId>0</EntityId>"
        "<PersistentFlags>CastShadows InScene</PersistentFlags>"
        '<PositionAndOrientation><Position x="0" y="0" z="0" />'
        '<Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
        f"<GridSizeEnum>Large</GridSizeEnum><CubeBlocks>{blocks}</CubeBlocks>"
        "<IsStatic>false</IsStatic><DisplayName>SE1541</DisplayName></CubeGrid></CubeGrids>"
        "</ShipBlueprint></ShipBlueprints></Definitions>"
    )


def shoot(api, name: str) -> np.ndarray:
    data = api.screenshot_bytes(ignore_sprites=True)
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / f"{TAG}-{name}.png").write_bytes(data)
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"), dtype=np.int16)


def set_mode(mode: str | None) -> None:
    for storage in (HOST.mod_storage, HOST.loaded_mod_storage):
        if mode:
            (storage / "mode").write_text(mode)
        else:
            (storage / "mode").unlink(missing_ok=True)


def main() -> int:
    world.prepare(WORLD, HOST.steam_id, HOST.name, grids=(), asteroids=ROCKS, mod=False)
    args = HOST.args
    HOST.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    ON_TOP.unlink(missing_ok=True)
    HOST.launch(0)
    try:
        api = HOST.wait_api()
        api.load(str(WORLD), online_mode="OFFLINE")
        local.wait(lambda: local.past_questions(api), 600, 2)
        rig.ensure_character(api)
        rig.wait_world(api)
        rig.focus_gameplay(api)
        api.character_teleport(*world.HOME)
        if api.get_view().get("thirdPerson"):
            api.key("V")

        grid = api.paste_blueprint(
            xml=grid_xml(), position=GRID_AT, forward=(1, 0, 0), up=(0, 1, 0)
        )[0]["entityId"]
        api.set_surface_script(grid, LCD, "TSS_ArtificialHorizon")
        # Both in one tick, or the first one pushes the grid away
        full = api.get_property(grid, next(iter(THRUSTERS)), "Override")["max"]
        api.batch(
            sets=[SetOp.property(grid, cell, "Override", full) for cell in THRUSTERS]
        )

        impostors = local.shown(HOST, (r.entity_id for r in ROCKS), timeout=600)
        for rock in ROCKS:
            local.check(
                f"{rock.name} shown", local.has_shape(impostors, rock.entity_id)
            )

        api.character_look_at(*GRID_AT, tolerance=0.2)
        HOST.set_fov(FOV)
        time.sleep(15)
        totals, _ = HOST.stats()
        behind = shoot(api, "behind")
        ON_TOP.write_text("")
        time.sleep(5)
        on_top = shoot(api, "ontop")
        ON_TOP.unlink()
        set_mode("real")
        time.sleep(15)
        without = shoot(api, "none")
        set_mode(None)

        bright = without.max(axis=2) > BRIGHT
        local.check(
            "the LCD and the flames show without impostors",
            bright.mean() > 0.002,
            f"{int(bright.sum())} bright pixels",
        )
        for name, shot, hidden in (
            ("LDR pass", on_top, True),
            ("early", behind, False),
        ):
            drawn = np.abs(shot - without).max(axis=2) > CHANGED
            covered = int((drawn & bright).sum())
            local.check(
                f"{name}: impostors show in the view",
                drawn.mean() > 0.005,
                f"{drawn.mean():.1%} of the screen, {totals.get('quads', '?')} quads",
            )
            local.check(
                f"{name}: impostors {'cover' if hidden else 'do not cover'} the LCD and "
                "the flames",
                # The rim of a flame is faint, a rock behind it shows through
                (
                    covered > 0.3 * bright.sum()
                    if hidden
                    else covered < 0.1 * bright.sum()
                ),
                f"{covered} of {int(bright.sum())} bright pixels changed",
            )
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            HOST.stop()
    print(
        f"{'PASSED' if local.failures == 0 else 'FAILED'}: {local.failures} failure(s)"
    )
    return local.failures


if __name__ == "__main__":
    sys.exit(main())
