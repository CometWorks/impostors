"""Side-by-side screenshots of a grid or an asteroid and its impostor at a range of
distances, for judging how close the impostors look to the real thing.

The world has the grid of a blueprint, or an asteroid of the game's voxel maps, and the
largest sync and view distances the game allows (20 and 50 km), so the client has the real
one up to there; farther out the real column is empty, as in the game without impostors.
The server sends the impostor anyway (ReplicationMargin covers the whole sync distance). At
each distance the player looks at it from the same direction; one screenshot shows the real
one, one the impostor:

- A grid is hidden by the mod's debug compare mode, which draws the impostor in its place.
- An asteroid can't be hidden on the client (voxel clipmaps ignore Visible), so the mod
  draws the impostor moved sideways by three radii, and the player moves along with it.
  The server picks the level of detail from the player's distance to the real asteroid.

The result is one image about 3840 pixels wide: a row per distance and a column pair per
view direction, the real one on the left and the impostor on the right, both cropped around
it and scaled with nearest neighbor sampling. Every impostor cell names its level of detail.

Environment: IMP_DIRECTIONS (corners, faces or "x,y,z;..."), IMP_SUN ("x,y,z"), IMP_TAG (a
suffix for the output names), IMP_OCCLUSION=1 (asteroid occlusion on), IMP_KEEP=1 (leave the
game running), and the rig's IMP_RESOLUTION and IMP_DISPLAY (see rig.py).

    uv run --no-project --with httpx --with pillow python tests/compare.py \\
        ["~/.config/SpaceEngineers/Blueprints/local/Visitor Ion" | asteroid:Asteroid512-008.vx2] \\
        [distances...]

The image lands in ~/.se-test/<task>-shots/compare-<name>.png.

IMP_ZOOMS=1,2,4,8,16 makes a zoom sheet instead, from the first distance D only: a row per
zoom factor z, the real one seen from D/z with the player's 70 degree view next to the
impostor seen from D through a view narrowed z times (the client probe sets the field of
view), so both are the same size on screen. Run it at 1920x1080 (IMP_RESOLUTION), the
screen the server's estimate is for.
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

# Zoom factors of a zoom sheet, see the docstring
ZOOMS = [float(z) for z in os.environ.get("IMP_ZOOMS", "").split(",") if z]

# Rows of the sheet: distance of the real shot, distance of the impostor shot, zoom
ROWS = (
    [(DISTANCES[0] / z, DISTANCES[0], z) for z in ZOOMS]
    if ZOOMS
    else [(d, d, 1.0) for d in DISTANCES]
)

SERVER = rig.Server()
# Medium: on the high preset the headless client (integrated GPU) draws asteroids without
# sunlight. Low and minimal may break other rendering.
CLIENT = rig.Client(1, "Alpha", quality="medium", probe=bool(ZOOMS))
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

# Side of one image cell, pixels: the sheet is about 3840 pixels wide (label column,
# then per direction two cells and the gaps, see compose)
CELL = int(3840 / (230 / 360 + len(DIRECTIONS) * (2 + 34 / 360)))


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
    # Kept inside the screenshot: a target near the edge is off center rather than cut
    cx = min(max(int(x * sx), half), image.size[0] - half)
    cy = min(max(int(y * sy), half), image.size[1] - half)
    box = (cx - half, cy - half, cx + half, cy + half)
    return image.crop(box).resize((CELL, CELL), Image.NEAREST), radius


def main() -> None:
    # Saved asteroids replicate to ViewDistance/sqrt(3) per axis at least. The game clamps
    # the view distance to 50 km and the sync distance to 20 km: farther out the client has
    # no real one, only the impostor, which MaxDistance lets through.
    world.SETTINGS.update(SyncDistance="20000", ViewDistance="50000")
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
        rig.plugin_config(
            ReplicationMargin=100000,
            MinPixels=0,
            MaxDistance=max(50000, max(DISTANCES) * 1.2),
            MinBlocks=1,
            # IMP_OCCLUSION=1 compares asteroids with ambient occlusion baked in
            AsteroidOcclusion=os.environ.get("IMP_OCCLUSION") == "1",
        )
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
    )  # (direction index, row index) -> (real row, real shot, impostor row, impostor shot)
    sun = None
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        # A windowed client starts in third person, with the character in front of the target
        if api.get_view().get("thirdPerson"):
            api.key("V")

        for index, view in enumerate(DIRECTIONS):
            direction = normalize(view)
            # Sideways: across the view direction, in the horizontal plane if possible
            side = [-direction[2], 0.0, direction[0]]
            if sum(v * v for v in side) < 0.01:
                side = [1.0, 0.0, 0.0]
            shift = [v * shift_length for v in normalize(side)]
            # The levels of the last zoom stay 30 seconds (ZoomKeepSeconds)
            if ZOOMS and index > 0:
                time.sleep(35)
            for row_index, (distance, far, zoom) in enumerate(ROWS):
                eye = [p + d * distance for p, d in zip(GRID_POSITION, direction)]
                write_mode("real")
                real = look(api, main_id, eye, GRID_POSITION, distance)
                real_shot = shoot(api, f"compare-{NAME}-{index}-{int(distance)}-real")

                # A zoom sheet takes the impostor from the far distance, through the zoom
                moved = far != distance
                if moved:
                    eye = [p + d * far for p, d in zip(GRID_POSITION, direction)]
                    CLIENT.set_fov(
                        2 * math.degrees(math.atan(math.tan(math.radians(35)) / zoom))
                    )

                if ASTEROID:
                    write_mode("shift " + " ".join(f"{v:.0f}" for v in shift))
                    impostor = look(
                        api,
                        main_id,
                        [e + v for e, v in zip(eye, shift)],
                        [p + v for p, v in zip(GRID_POSITION, shift)],
                        far,
                        drawn=True,
                        aim=moved,
                    )
                else:
                    write_mode("impostor")
                    impostor = look(
                        api,
                        main_id,
                        eye if moved else None,
                        GRID_POSITION,
                        far,
                        drawn=True,
                        aim=moved,
                    )
                if moved:
                    impostor = settle(main_id) or impostor
                impostor_shot = shoot(
                    api, f"compare-{NAME}-{index}-{int(distance)}-x{zoom:g}-impostor"
                )
                CLIENT.set_fov(None)
                print(
                    f"{view} {far:.0f} m zoom {zoom:g}: {impostor.get('lod')}",
                    flush=True,
                )
                shots[index, row_index] = (real, real_shot, impostor, impostor_shot)
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
                    {
                        "direction": i,
                        "distance": ROWS[k][0],
                        "impostor_distance": ROWS[k][1],
                        "zoom": ROWS[k][2],
                        "real": r,
                        "impostor": m,
                    }
                    for (i, k), (r, _, m, _) in shots.items()
                ],
            },
            indent=1,
        )
    )


def normalize(v):
    length = math.sqrt(sum(x * x for x in v))
    return [x / length for x in v]


LAST_EYE: list = []


def look(api, entity_id, eye, target, distance, drawn=False, aim=True) -> dict:
    """Moves the player (unless eye is None), aims at the target and waits for stats of
    this view: the target near the screen center at the right distance. The impostor shot
    doesn't aim again: aiming in zero gravity can end in another roll of the camera, and
    the two shots wouldn't line up."""
    if eye is not None:
        # After a long jump the game takes a while to load the near models of the target
        far = math.dist(eye, LAST_EYE[0]) > 5000 if LAST_EYE else True
        LAST_EYE[:] = [eye]
        api.character_teleport(*eye)
        time.sleep(12 if far else 2)
    if aim:
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


def settle(entity_id: int, timeout: float = 20) -> dict:
    """The stats once the impostor's level held for 3 seconds. A finer shape larger than
    the shape credit left can wait a second or two behind the ones sent before it."""
    start = time.monotonic()
    last, changed = None, start
    while time.monotonic() - start < timeout:
        lod = stats_row(entity_id).get("lod")
        if lod != last:
            last, changed = lod, time.monotonic()
        elif time.monotonic() - changed > 3:
            break
        time.sleep(0.5)
    print(f"level {last} after {changed - start:.1f} s", flush=True)
    return stats_row(entity_id)


def compose(shots, sun, target: Path) -> None:
    """A row per distance; per direction a real and an impostor cell"""
    from PIL import Image, ImageDraw, ImageFont

    k = CELL / 360  # Layout scale
    font = ImageFont.load_default(size=round(22 * k))
    label, gap, pair_gap, header = (round(v * k) for v in (230, 6, 28, 80))
    pair = 2 * CELL + gap
    width = label + len(DIRECTIONS) * (pair + pair_gap)
    height = header + len(ROWS) * (CELL + gap)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    sun = normalize(sun)
    for index, view in enumerate(DIRECTIONS):
        left = label + index * (pair + pair_gap)
        angle = math.degrees(
            math.acos(max(-1, min(1, sum(a * b for a, b in zip(normalize(view), sun)))))
        )
        name = "".join(f"{'+' if v > 0 else '-'}{a}" for v, a in zip(view, "XYZ") if v)
        draw.text(
            (left, round(8 * k)),
            f"from {name}, {angle:.0f} deg from the sun",
            fill="black",
            font=font,
        )
        draw.text((left + 8, round(44 * k)), "real", fill="black", font=font)
        draw.text(
            (left + CELL + gap + 8, round(44 * k)), "impostor", fill="black", font=font
        )
    for row_index, (distance, far, zoom) in enumerate(ROWS):
        top = header + row_index * (CELL + gap)
        radius, viewport = 0.0, 720
        for index in range(len(DIRECTIONS)):
            left = label + index * (pair + pair_gap)
            real, real_shot, impostor, impostor_shot = shots[index, row_index]
            for column, (row, shot) in enumerate(
                ((real, real_shot), (impostor, impostor_shot))
            ):
                try:
                    cell, r = crop(shot, row)
                except (KeyError, ValueError):
                    cell, r = Image.new("RGB", (CELL, CELL), "gray"), 0.0
                x = left + column * (CELL + gap)
                sheet.paste(cell, (x, top))
                if column == 0:
                    radius = max(radius, r)
                else:
                    # The level of each impostor: one that kept a coarse level shows here
                    text = f"level {impostor.get('lod', '?')}"
                    box = draw.textbbox((x + 6, top + 6), text, font=font)
                    draw.rectangle(
                        (box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill="black"
                    )
                    draw.text((x + 6, top + 6), text, fill="white", font=font)
            viewport = int(real.get("viewport", "1280x720").split("x")[1])
        # The server's default MinPixels drops impostors under 3 pixels across at 1080p,
        # this sheet shows them anyway
        cut = "\ncut off by default" if 2 * radius * 1080 / viewport < 3 else ""
        text = f"{distance:.0f} m\nradius {radius:.1f} px{cut}"
        if ZOOMS:
            fov = 2 * math.degrees(math.atan(math.tan(math.radians(35)) / zoom))
            text = (
                f"zoom {zoom:g}x\nreal {distance:.0f} m\nimpostor {far:.0f} m,"
                f"\n  {fov:.1f} deg view\nradius {radius:.1f} px"
            )
        draw.multiline_text(
            (12, top + round(12 * k)),
            text,
            fill="black",
            spacing=8,
            font=font,
        )
    sheet.save(target)
    print("comparison", target)


if __name__ == "__main__":
    main()
