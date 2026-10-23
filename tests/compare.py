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

import json
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
# Tells runs with other settings apart, for example another sun
if os.environ.get("IMP_TAG"):
    NAME += "-" + os.environ["IMP_TAG"]
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
# Medium: on the high preset the headless client (integrated GPU) draws asteroids without
# sunlight. Low and minimal may break other rendering.
CLIENT = rig.Client(1, "Alpha", quality="medium")
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"

GRID_POSITION = (2000.0, 0.0, 0.0)

# The directions the player looks at the target from, at every distance: the 8 cube corners
# by default, which put the sun in front, behind and to the sides; "faces" for the 6 cube
# faces, or explicit "x,y,z;x,y,z"
CORNERS = [(x, y, z) for x in (1, -1) for y in (1, -1) for z in (1, -1)]
FACES = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
_directions = os.environ.get("IMP_DIRECTIONS", "corners")
DIRECTIONS = (
    CORNERS
    if _directions == "corners"
    else (
        FACES
        if _directions == "faces"
        else [tuple(float(v) for v in d.split(",")) for d in _directions.split(";")]
    )
)

# Direction towards the sun, by default the Empty World's
SUN = (
    tuple(float(v) for v in os.environ["IMP_SUN"].split(","))
    if os.environ.get("IMP_SUN")
    else None
)

CELL = 180  # Side of one image cell, pixels


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

    if SUN:
        sandbox = SERVER.world / "Sandbox.sbc"
        x, y, z = normalize(SUN)
        vector = f'x="{x}" y="{y}" z="{z}"'
        text = re.sub(
            r"<(BaseSunDirection|SunDirectionNormalized) [^/]*/>",
            rf"<\1 {vector} />",
            sandbox.read_text(encoding="utf-8"),
        )
        sandbox.write_text(text, encoding="utf-8")

    shots = (
        {}
    )  # (direction index, distance) -> (real row, real shot, impostor row, impostor shot)
    sun = None
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)

        for index, view in enumerate(DIRECTIONS):
            direction = normalize(view)
            # Sideways: across the view direction, in the horizontal plane if possible
            side = [-direction[2], 0.0, direction[0]]
            if sum(v * v for v in side) < 0.01:
                side = [1.0, 0.0, 0.0]
            shift = [v * shift_length for v in normalize(side)]
            for distance in DISTANCES:
                eye = [p + d * distance for p, d in zip(GRID_POSITION, direction)]
                write_mode("real")
                real = look(api, main_id, eye, GRID_POSITION, distance)
                real_shot = shoot(api, f"compare-{NAME}-{index}-{int(distance)}-real")

                if ASTEROID:
                    write_mode("shift " + " ".join(f"{v:.0f}" for v in shift))
                    impostor = look(
                        api,
                        main_id,
                        [e + v for e, v in zip(eye, shift)],
                        [p + v for p, v in zip(GRID_POSITION, shift)],
                        distance,
                        drawn=True,
                    )
                else:
                    write_mode("impostor")
                    impostor = look(
                        api, main_id, None, GRID_POSITION, distance, drawn=True
                    )
                impostor_shot = shoot(
                    api, f"compare-{NAME}-{index}-{int(distance)}-impostor"
                )
                print(f"{view} {distance:.0f} m: {impostor.get('lod')}", flush=True)
                shots[index, distance] = (real, real_shot, impostor, impostor_shot)
        write_mode("real")
        sun = SUN or tuple(
            float(v) for v in CLIENT.stats()[0].get("sun", "0,1,0").split(",")
        )
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()

    compose(shots, sun, SHOTS / f"compare-{NAME}.png")
    # The stats of every shot, for measuring them later
    (SHOTS / f"compare-{NAME}.json").write_text(
        json.dumps(
            {
                "sun": sun,
                "directions": DIRECTIONS,
                "shots": [
                    {"direction": i, "distance": d, "real": r, "impostor": m}
                    for (i, d), (r, _, m, _) in shots.items()
                ],
            },
            indent=1,
        )
    )


def normalize(v):
    length = math.sqrt(sum(x * x for x in v))
    return [x / length for x in v]


def look(api, entity_id, eye, target, distance, drawn=False) -> dict:
    """Moves the player (unless eye is None), aims at the target and waits for stats of
    this view: the target near the screen center at the right distance"""
    if eye is not None:
        api.character_teleport(*eye)
        time.sleep(2)
    api.character_look_at(*target, tolerance=0.2)

    def ready(row):
        if "screen" not in row or row["screen"] == "behind":
            return False
        x, y = (float(v) for v in row["screen"].split(","))
        w, h = (float(v) for v in row["viewport"].split("x"))
        centered = abs(x - w / 2) < 60 and abs(y - h / 2) < 60
        near = abs(float(row["distance"]) - distance) < max(30, distance * 0.05)
        return centered and near and (not drawn or row.get("drawn") == "1")

    row = wait_row(entity_id, ready)
    time.sleep(1.5)
    return stats_row(entity_id) or row


def compose(shots, sun, target: Path) -> None:
    """A row per distance; per direction a real and an impostor cell"""
    from PIL import Image, ImageDraw

    label, gap, pair_gap, header = 150, 4, 16, 44
    pair = 2 * CELL + gap
    width = label + len(DIRECTIONS) * (pair + pair_gap)
    height = header + len(DISTANCES) * (CELL + gap)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    sun = normalize(sun)
    for index, view in enumerate(DIRECTIONS):
        left = label + index * (pair + pair_gap)
        angle = math.degrees(
            math.acos(max(-1, min(1, sum(a * b for a, b in zip(normalize(view), sun)))))
        )
        name = "".join(f"{'+' if v > 0 else '-'}{a}" for v, a in zip(view, "XYZ") if v)
        draw.text((left, 4), f"from {name}, {angle:.0f} deg from the sun", fill="black")
        draw.text((left + CELL // 2 - 12, 24), "real", fill="black")
        draw.text((left + CELL + gap + CELL // 2 - 24, 24), "impostor", fill="black")
    for row_index, distance in enumerate(DISTANCES):
        top = header + row_index * (CELL + gap)
        radius, level = 0.0, "?"
        for index in range(len(DIRECTIONS)):
            left = label + index * (pair + pair_gap)
            real, real_shot, impostor, impostor_shot = shots[index, distance]
            for column, (row, shot) in enumerate(
                ((real, real_shot), (impostor, impostor_shot))
            ):
                try:
                    cell, r = crop(shot, row)
                except (KeyError, ValueError):
                    cell, r = Image.new("RGB", (CELL, CELL), "gray"), 0.0
                sheet.paste(cell, (left + column * (CELL + gap), top))
                if column == 0:
                    radius = max(radius, r)
            level = impostor.get("lod", level)
        draw.multiline_text(
            (8, top + 8),
            f"{distance:.0f} m\nradius {radius:.1f} px\nlevel {level}",
            fill="black",
            spacing=6,
        )
    sheet.save(target)
    print("comparison", target)


if __name__ == "__main__":
    main()
