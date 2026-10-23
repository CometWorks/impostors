"""Side-by-side screenshots of a grid or an asteroid and its impostor at a range of
distances, for judging how close the impostors look to the real thing.

The world has the grid of a blueprint, or an asteroid of the game's voxel maps, and a sync
distance of 16 km, so the client has the real one at every distance. The server sends its
impostor anyway (ReplicationMargin covers the whole sync distance). At each distance the
player looks at it from the same direction; one screenshot shows the real one, one the
impostor:

- A grid is hidden by the mod's debug compare mode, which draws the impostor in its place.
- An asteroid can't be hidden on the client (voxel clipmaps ignore Visible), so the mod
  draws the impostor moved sideways by three radii, and the player moves along with it.
  The server picks the level of detail from the player's distance to the real asteroid,
  which is larger then: the label shows both levels where they differ.

The result is one image, a row per distance: the real one on the left, the impostor on the
right, both cropped around it and scaled up with nearest neighbor sampling.

    uv run --no-project --with httpx --with pillow python tests/compare.py \\
        ["~/.config/SpaceEngineers/Blueprints/local/Visitor Ion" | asteroid:Asteroid512-008.vx2] \\
        [distances...]

The image lands in ~/.se-test/<task>-shots/compare-<name>.png.
"""

from __future__ import annotations

import math
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402

TARGET = (
    sys.argv[1]
    if len(sys.argv) > 1
    else str(Path.home() / ".config/SpaceEngineers/Blueprints/local/Visitor Ion")
)
ASTEROID = TARGET.removeprefix("asteroid:") if TARGET.startswith("asteroid:") else None
NAME = (ASTEROID.removesuffix(".vx2") if ASTEROID else Path(TARGET).name).replace(
    " ", "-"
)
DISTANCES = [float(d) for d in sys.argv[2:]] or [
    100,
    200,
    400,
    800,
    1600,
    3200,
    6400,
    9600,
    12800,
]

SERVER = rig.Server()
CLIENT = rig.Client(1, "Alpha", quality="high")
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"

# The grid's position, and the direction the player looks at it from: a three quarter
# view, so no face is seen edge on
GRID_POSITION = (2000.0, 0.0, 0.0)
VIEW_FROM = (-0.6, 0.35, 0.72)

CELL = 400  # Side of one image cell, pixels


def blueprint_xml(path: Path, position) -> tuple[str, list[int]]:
    """The blueprint's grids as sector objects, moved so its first grid is at position"""
    text = (path / "bp.sbc").read_text(encoding="utf-8")
    grids = re.findall(r"<CubeGrid>(.*?)</CubeGrid>", text, re.S)
    first = re.search(r'<Position x="([^"]+)" y="([^"]+)" z="([^"]+)"', grids[0])
    offset = [p - float(f) for p, f in zip(position, first.groups())]
    objects, ids = [], []
    for body in grids:
        ids.append(int(re.search(r"<EntityId>(\d+)</EntityId>", body).group(1)))

        def move(m):
            x, y, z = (float(v) + o for v, o in zip(m.groups(), offset))
            return f'<Position x="{x}" y="{y}" z="{z}"'

        body = re.sub(
            r'<Position x="([^"]+)" y="([^"]+)" z="([^"]+)"', move, body, count=1
        )
        objects.append(
            '<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_CubeGrid">'
            f"{body}</MyObjectBuilder_EntityBase>"
        )
    return "".join(objects), ids


def write_mode(mode: str) -> None:
    (CLIENT.mod_storage / "mode").write_text(mode)


def stats_row(entity_id: int) -> dict:
    return CLIENT.stats()[1].get(entity_id, {})


def wait_row(entity_id: int, predicate, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        row = stats_row(entity_id)
        if (row and predicate(row)) or time.monotonic() > deadline:
            return row
        time.sleep(1)


def shoot(api, name: str) -> Path:
    path = SHOTS / f"{name}.png"
    path.write_bytes(api.screenshot_bytes(ignore_sprites=True))
    return path


def crop(path: Path, row: dict):
    """The grid's surroundings, scaled to one cell"""
    from PIL import Image

    image = Image.open(path).convert("RGB")
    vw, vh = (int(v) for v in row["viewport"].split("x"))
    sx, sy = image.size[0] / vw, image.size[1] / vh
    x, y = (float(v) for v in row["screen"].split(","))
    radius = float(row["radius"])
    half = max(16, min(image.size[1] // 2, int(radius * 1.6 * sy)))
    box = (
        int(x * sx) - half,
        int(y * sy) - half,
        int(x * sx) + half,
        int(y * sy) + half,
    )
    return image.crop(box).resize((CELL, CELL), Image.NEAREST), radius


def main() -> None:
    # Saved asteroids replicate to ViewDistance/sqrt(3) per axis at least
    world.SETTINGS.update(SyncDistance="16000", ViewDistance="30000")
    if ASTEROID:
        size = int(re.search(r"Asteroid(\d+)", ASTEROID).group(1))
        corner = tuple(p - size / 2 for p in GRID_POSITION)
        asteroid = world.Asteroid("Compare", 130000000000000099, corner, ASTEROID)
        main_id = asteroid.entity_id
        shift_length = 3 * size / 2 * math.sqrt(3)
        world.prepare(
            SERVER.world, CLIENT.steam_id, CLIENT.name, grids=(), asteroids=(asteroid,)
        )
    else:
        grids_xml, ids = blueprint_xml(Path(TARGET).expanduser(), GRID_POSITION)
        main_id = ids[0]
        shift_length = 0
        world.prepare(
            SERVER.world, CLIENT.steam_id, CLIENT.name, grids=(), asteroids=()
        )
        sector = SERVER.world / "SANDBOX_0_0_0_.sbs"
        sector.write_text(
            sector.read_text(encoding="utf-8").replace(
                "<SectorObjects>", "<SectorObjects>" + grids_xml, 1
            ),
            encoding="utf-8",
        )
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(ReplicationMargin=100000, MinPixels=0, MinBlocks=1)
    )
    SHOTS.mkdir(parents=True, exist_ok=True)

    rows = []
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)

        norm = math.sqrt(sum(v * v for v in VIEW_FROM))
        direction = [v / norm for v in VIEW_FROM]
        # Sideways: across the view direction, in the horizontal plane
        side = [-direction[2], 0.0, direction[0]]
        side_norm = math.sqrt(sum(v * v for v in side))
        shift = [v / side_norm * shift_length for v in side]
        for distance in DISTANCES:
            eye = [p + d * distance for p, d in zip(GRID_POSITION, direction)]
            write_mode("real")
            api.character_teleport(*eye)
            time.sleep(3)
            api.character_look_at(*GRID_POSITION, tolerance=0.2)
            real = wait_row(main_id, lambda r: r.get("real") == "1" and "screen" in r)
            time.sleep(2)
            real = stats_row(main_id) or real
            real_shot = shoot(api, f"compare-{NAME}-{int(distance)}-real")

            if ASTEROID:
                write_mode("shift " + " ".join(f"{v:.0f}" for v in shift))
                api.character_teleport(*[e + v for e, v in zip(eye, shift)])
                time.sleep(3)
                api.character_look_at(
                    *[p + v for p, v in zip(GRID_POSITION, shift)], tolerance=0.2
                )
            else:
                write_mode("impostor")
            impostor = wait_row(main_id, lambda r: r.get("drawn") == "1")
            time.sleep(2)
            impostor = stats_row(main_id) or impostor
            impostor_shot = shoot(api, f"compare-{NAME}-{int(distance)}-impostor")
            print(f"{distance:.0f} m: real {real}\n  impostor {impostor}", flush=True)
            rows.append((distance, real, real_shot, impostor, impostor_shot))
        write_mode("real")
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()

    compose(rows, SHOTS / f"compare-{NAME}.png")


def compose(rows, target: Path) -> None:
    from PIL import Image, ImageDraw

    label = 200
    sheet = Image.new(
        "RGB", (label + 2 * CELL + 10, len(rows) * (CELL + 10) + 30), "white"
    )
    draw = ImageDraw.Draw(sheet)
    draw.text((label + CELL // 2 - 20, 8), "real", fill="black")
    draw.text((label + CELL + 10 + CELL // 2 - 30, 8), "impostor", fill="black")
    for i, (distance, real, real_shot, impostor, impostor_shot) in enumerate(rows):
        top = 30 + i * (CELL + 10)
        cells = []
        for row, shot in ((real, real_shot), (impostor, impostor_shot)):
            try:
                cells.append(crop(shot, row))
            except (KeyError, ValueError):
                cells.append((Image.new("RGB", (CELL, CELL), "gray"), 0.0))
        (real_cell, radius), (impostor_cell, _) = cells
        sheet.paste(real_cell, (label, top))
        sheet.paste(impostor_cell, (label + CELL + 10, top))
        shape = impostor.get("shape", "none").split("/")
        quads = shape[2] if len(shape) == 3 else "?"
        level = impostor.get("lod", "?")
        if real.get("lod") not in (None, level):
            level += f" ({real['lod']} at this distance)"
        draw.multiline_text(
            (10, top + 10),
            f"{distance:.0f} m\nradius {radius:.1f} px\nlevel {level}\n{quads} quads",
            fill="black",
            spacing=6,
        )
    sheet.save(target)
    print("comparison", target)


if __name__ == "__main__":
    main()
