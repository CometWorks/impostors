"""Planet shadow on impostors (SE-713): grids around a planet, on its day side, its night
side and in the band around the terminator where the sunlight fades, each shot real and next
to it as its impostor, shaded and unshaded (the lighting before SE-713), from two distances.

The world is the game's Empty World with the EarthLike planet of its Home System world
(60 km sea level, hills to 67.2 km, so the shade fades from 60 to 78 km) at PLANET, the sun
along +X and static boxes of armor at the CASES. Setting Visible doesn't hide these grids on
the client, so the mod's "shift" debug mode draws the impostor beside the real grid, five
radii to the camera's right (see ImpostorRenderer.ReadMode); the "real" shot, without
impostors, is the background there.

Checks, each printed with PASS or FAIL:
- the renderer found the planet, with the band it should have
- each impostor's sunlight in the stats matches this script's own ray test
- the impostors in the shadow lost most of their light against the unshaded ones, the day
  side ones none of it

It also prints, per case and distance, how bright the real grid and its impostor are: the
mean of the brightest quarter of the pixels inside the grid's circle on screen, over the
median of a ring around it. The real grids against their impostors are the hand-over check.
At the end it prints what the shade costs: microseconds per impostor drawn.

    IMP_TASK=se713 uv run --no-project --with httpx --with pillow python tests/planet_shadow.py

The sheet lands in ~/.se-test/<task>-shots/planet-shadow.png, the numbers next to it in
planet-shadow.json. IMP_CASES=night,day runs only those cases, IMP_KEEP=1 leaves the game
running. With the argument "analyze" the script only redoes the checks and the sheet from
the last run's planet-shadow-raw.json.
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

SERVER = rig.Server()
# Medium like compare.py: on high the headless client draws asteroids without sunlight
CLIENT = rig.Client(1, "Alpha", quality="medium")
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"

# The planet's center, far from where the player spawns. Its storage is 2^18 m on a side and
# the save holds its min corner.
PLANET = (0.0, 0.0, -300000.0)
PLANET_HALF = 131072.0
PLANET_SOURCE = rig.DS64.parent / "Content" / "CustomWorlds" / "Home System"
SUN = (1.0, 0.0, 0.0)


# Name, position relative to the planet's center. The sun is along +X, so on the night side
# (x < 0) the ray towards the sun passes the center at the distance in the YZ plane. All are
# above the hills (67.2 km). The band cases sit 80 km from the center, their rays pass it at
# 58 to 75 km.
def band(passing: float) -> tuple[str, tuple[float, float, float]]:
    return f"band-{passing / 1000:g}", (
        -math.sqrt(80000.0**2 - passing**2),
        0.0,
        passing,
    )


CASES = [
    ("day", (75000.0, 0.0, 0.0)),
    ("dawn", (3000.0, 0.0, 68500.0)),
    *(band(p) for p in (75000.0, 72000.0, 69000.0, 66000.0, 63000.0, 60500.0, 58000.0)),
    ("night", (-75000.0, 0.0, 0.0)),
    ("far-night", (-150000.0, 0.0, 30000.0)),
]
# IMP_CASES=night,day runs only those
if os.environ.get("IMP_CASES"):
    CASES = [c for c in CASES if c[0] in os.environ["IMP_CASES"].split(",")]
# Within the shadow cascades of the real grids (about 1 km) and beyond them
DISTANCES = [600.0, 3000.0]
# Columns of the sheet: the real grid, its impostor, its impostor lit as before SE-713
KINDS = ["real", "impostor", "unshaded"]
GRID_SIZE = (24, 12, 24)


def add(a, b, k=1.0):
    return tuple(x + k * y for x, y in zip(a, b))


def normalize(v):
    length = math.sqrt(sum(x * x for x in v))
    return tuple(x / length for x in v)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


GRIDS = [
    world.Grid(
        name,
        120000000000000100 + i,
        add(PLANET, offset),
        GRID_SIZE,
        True,
        world.YELLOW,
    )
    for i, (name, offset) in enumerate(CASES)
]


def planet_xml() -> tuple[str, Path]:
    """The EarthLike entry of the Home System world, moved to PLANET"""
    text = (PLANET_SOURCE / "SANDBOX_0_0_0_.sbs").read_text(encoding="utf-8-sig")
    entry = next(
        m.group(0)
        for m in re.finditer(
            r'<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_Planet">.*?'
            r"</MyObjectBuilder_EntityBase>",
            text,
            re.S,
        )
        if "<PlanetGenerator>EarthLike<" in m.group(0)
    )
    corner = [c - PLANET_HALF for c in PLANET]
    entry = re.sub(
        r'<Position x="[^"]*" y="[^"]*" z="[^"]*" />',
        f'<Position x="{corner[0]}" y="{corner[1]}" z="{corner[2]}" />',
        entry,
        count=1,
    )
    storage = re.search(r"<StorageName>([^<]*)</StorageName>", entry).group(1)
    return entry, PLANET_SOURCE / f"{storage}.vx2"


def expected_sunlight(point, center, ground, top) -> float:
    """The renderer's Sunlight for one planet"""
    to_center = [c - p for c, p in zip(center, point)]
    along = dot(to_center, SUN)
    squared = dot(to_center, to_center)
    passing = (
        math.sqrt(max(0.0, squared - along * along))
        if along > 0
        else math.sqrt(squared) - along
    )
    if passing >= top:
        return 1.0
    t = (passing - ground) / (top - ground)
    return 0.0 if t <= 0 else t * t * (3 - 2 * t)


def prepare_world() -> None:
    world.prepare(SERVER.world, CLIENT.steam_id, CLIENT.name, grids=GRIDS, asteroids=())
    entry, vx2 = planet_xml()
    sector = SERVER.world / "SANDBOX_0_0_0_.sbs"
    sector.write_text(
        sector.read_text(encoding="utf-8").replace(
            "<SectorObjects>", "<SectorObjects>" + entry, 1
        ),
        encoding="utf-8",
    )
    (SERVER.world / vx2.name).write_bytes(vx2.read_bytes())
    sandbox = SERVER.world / "Sandbox.sbc"
    vector = 'x="{}" y="{}" z="{}"'.format(*normalize(SUN))
    sandbox.write_text(
        re.sub(
            r"<(BaseSunDirection|SunDirectionNormalized) [^/]*/>",
            rf"<\1 {vector} />",
            sandbox.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def write_mode(mode: str) -> None:
    """Switches the mod's debug mode and waits until it has read it: it reads the mode
    file right before it writes the stats, once a second of game time, and the headless
    client may run slower than real time"""
    before = int(CLIENT.stats()[0].get("tick", 0))
    (CLIENT.mod_storage / "mode").write_text(mode)
    wait_for(
        lambda: int(CLIENT.stats()[0].get("tick", 0)) >= before + 120,
        60,
        f"mode {mode} not read",
    )
    time.sleep(0.5)


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(1)
    raise TimeoutError(what)


def eye_for(grid: world.Grid, distance: float):
    """A camera on the sun's side of the grid and above it, so the sunlit faces show; the
    sun stays well behind the camera"""
    direction = normalize(add(normalize(SUN), (0.0, 1.0, 0.4), 1.0))
    return add(grid_center(grid), direction, distance)


def look(api, grid: world.Grid, distance: float, far: bool) -> dict:
    """Moves there and waits until the stats show the real grid centered at that distance"""
    eye = eye_for(grid, distance)
    api.character_teleport(*eye)
    # After a long jump the game takes a while to stream the grid in
    time.sleep(12 if far else 3)
    # In the planet's gravity the character hovers on its jetpack
    character = api.get_character()
    if not character.get("jetpack"):
        api.key("X")
    if not character.get("dampeners"):
        api.character_set_dampeners(True)
    write_mode("real")
    last = {}

    def ready():
        api.character_look_at(*grid_center(grid), tolerance=0.1)
        row = CLIENT.stats()[1].get(grid.entity_id, {})
        last.update(row)
        if row.get("real") != "1" or "screen" not in row or row["screen"] == "behind":
            return None
        x, y = (float(v) for v in row["screen"].split(","))
        w, h = (float(v) for v in row["viewport"].split("x"))
        near = abs(float(row["distance"]) - distance) < distance * 0.1
        return row if abs(x - w / 2) < 60 and abs(y - h / 2) < 60 and near else None

    try:
        return wait_for(ready, 90, f"{grid.name} at {distance:.0f} m not in view")
    except TimeoutError as error:
        raise TimeoutError(f"{error}, last stats: {last}") from None


def brightness(shot: Path, row: dict) -> float:
    """The brightest quarter of the pixels inside the circle of the stats row, over the
    median of the ring from 1.5 to 2.5 radii, luminance 0-255"""
    from PIL import Image

    image = Image.open(shot).convert("L")
    vw, vh = (int(v) for v in row["viewport"].split("x"))
    scale = image.size[0] / vw
    x, y = (float(v) * scale for v in row["screen"].split(","))
    r = max(2.0, float(row["radius"]) * 0.8 * scale)
    inside, ring = [], []
    for j in range(int(y - 2.5 * r), int(y + 2.5 * r) + 1):
        for i in range(int(x - 2.5 * r), int(x + 2.5 * r) + 1):
            if not (0 <= i < image.size[0] and 0 <= j < image.size[1]):
                continue
            d = math.hypot(i + 0.5 - x, j + 0.5 - y)
            if d <= r:
                inside.append(image.getpixel((i, j)))
            elif 1.5 * r <= d <= 2.5 * r:
                ring.append(image.getpixel((i, j)))
    inside.sort(reverse=True)
    top = inside[: max(1, len(inside) // 4)]
    ring.sort()
    background = ring[len(ring) // 2] if ring else 0
    return sum(top) / len(top) - background


def main() -> None:
    world.SETTINGS.update(SyncDistance="20000", ViewDistance="50000")
    prepare_world()
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(
            ReplicationMargin=100000, MinPixels=0, MaxDistance=400000, MinBlocks=1
        )
    )
    SHOTS.mkdir(parents=True, exist_ok=True)

    results, costs = [], []
    planets = []
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        planets = api.get_planets()

        for grid, (name, offset) in zip(GRIDS, CASES):
            for index, distance in enumerate(DISTANCES):
                try:
                    real_row = look(api, grid, distance, far=index == 0)
                except TimeoutError as error:
                    print(f"FAIL {name} {distance:.0f} m: {error}", flush=True)
                    continue
                # The real grid fades in over a second
                time.sleep(3)
                character = api.get_character()
                right = normalize(cross(character["forward"], character["up"]))
                side = " ".join(f"{v * 5 * float(grid_radius()):.1f}" for v in right)
                shots, rows = {}, {"real": real_row}
                for kind, mode in (
                    ("real", "real"),
                    ("impostor", f"shift {side}"),
                    ("unshaded", f"shift {side} unshaded"),
                ):
                    write_mode(mode)
                    path = SHOTS / f"planet-shadow-{name}-{int(distance)}-{kind}.png"
                    path.write_bytes(api.screenshot_bytes(ignore_sprites=True))
                    shots[kind] = path
                    stats = CLIENT.stats()
                    if kind != "real":
                        rows[kind] = stats[1].get(grid.entity_id, {})
                    if kind == "impostor":
                        costs.append(
                            (
                                int(stats[0].get("shadeus", 0)),
                                int(stats[0].get("shades", 0)),
                            )
                        )
                values = {}
                for kind in KINDS:
                    try:
                        values[kind] = round(brightness(shots[kind], rows[kind]), 2)
                    except (KeyError, ValueError):
                        values[kind] = float("nan")
                result = {
                    "case": name,
                    "distance": distance,
                    "sunlight": float(rows["impostor"].get("sunlight", "nan")),
                    "lod": rows["impostor"].get("lod"),
                    "header_planets": CLIENT.stats()[0].get("planets", ""),
                    **values,
                    "shift": [v * 5 * grid_radius() for v in right],
                    "rows": rows,
                    "shots": {k: str(p) for k, p in shots.items()},
                }
                results.append(result)
                print(
                    f"{name:10} {distance:5.0f} m sunlight {result['sunlight']:.3f}  "
                    f"real {values['real']:6.1f}  impostor {values['impostor']:6.1f}  "
                    f"unshaded {values['unshaded']:6.1f}",
                    flush=True,
                )
        write_mode("real")
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()

    RAW.write_text(
        json.dumps({"planets": planets, "results": results, "costs": costs}, indent=1)
    )
    analyze()


def grid_center(grid: world.Grid):
    """The middle of its box: blocks are 2.5 m, the first one centered on its position"""
    return add(grid.position, [(n - 1) * 1.25 for n in GRID_SIZE])


def grid_radius() -> float:
    """Half the diagonal of the grids' boxes, meters"""
    return 1.25 * math.sqrt(sum(v * v for v in GRID_SIZE))


RAW = SHOTS / "planet-shadow-raw.json"


def analyze() -> None:
    """The checks, the cost and the sheet, from the numbers main() saved"""
    raw = json.loads(RAW.read_text())
    results, costs, checks = raw["results"], raw["costs"], []
    # The planet the renderer found, against the game's own numbers
    planet = raw["planets"][0] if raw["planets"] else {}
    center = tuple(planet.get("center", (0.0, 0.0, 0.0)))
    ground = planet.get("averageRadius", 0.0)
    top = ground + (planet.get("maximumRadius", 0.0) - ground) * 2 * 1.25
    found = results[0]["header_planets"] if results else ""
    checks.append(
        (
            "planet band",
            found == f"{ground:.0f}:{top:.0f}",
            f"renderer {found}, expected {ground:.0f}:{top:.0f}, center {center}",
        )
    )
    for result in results:
        # The renderer shades the impostor where it draws it, moved by the shift
        position = [float(v) for v in result["rows"]["impostor"]["pos"].split(",")]
        want = expected_sunlight(add(position, result["shift"]), center, ground, top)
        checks.append(
            (
                f"sunlight {result['case']} {result['distance']:.0f} m",
                abs(result["sunlight"] - want) < 0.02,
                f"{result['sunlight']:.3f}, expected {want:.3f}",
            )
        )
        ratio = result["impostor"] / max(0.01, result["unshaded"])
        result["shaded_ratio"] = round(ratio, 3)
        if want < 0.05:
            checks.append(
                (
                    f"dark {result['case']} {result['distance']:.0f} m",
                    ratio < 0.5,
                    f"impostor/unshaded {ratio:.2f}",
                )
            )
        elif want > 0.99:
            checks.append(
                (
                    f"lit {result['case']} {result['distance']:.0f} m",
                    0.9 < ratio < 1.1,
                    f"impostor/unshaded {ratio:.2f}",
                )
            )

    us = sum(c[0] for c in costs)
    calls = sum(c[1] for c in costs)
    print(
        f"shade cost: {us} us over {calls} impostors drawn,"
        f" {us / max(1, calls):.3f} us per impostor"
    )
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
    compose(results, SHOTS / "planet-shadow.png")
    (SHOTS / "planet-shadow.json").write_text(
        json.dumps(
            {
                "planet": planet,
                "results": results,
                "cost_us": us,
                "cost_calls": calls,
                "checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in checks],
            },
            indent=1,
        )
    )
    print("PASS" if checks and all(ok for _, ok, _ in checks) else "FAIL")


def compose(results, target: Path) -> None:
    """A row per case; per distance the real grid, its impostor and the unshaded impostor,
    cropped around each"""
    from PIL import Image, ImageDraw, ImageFont

    cell, gap, label, header = 300, 6, 260, 70
    columns = [(d, k) for d in DISTANCES for k in KINDS]
    names = [c[0] for c in CASES]
    sheet = Image.new(
        "RGB",
        (label + len(columns) * (cell + gap), header + len(names) * (cell + gap)),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    titles = {"real": "real", "impostor": "impostor", "unshaded": "before (unshaded)"}
    for k, (distance, kind) in enumerate(columns):
        draw.text(
            (label + k * (cell + gap), 10),
            f"{distance:.0f} m\n{titles[kind]}",
            fill="black",
            font=font,
        )
    for i, name in enumerate(names):
        top = header + i * (cell + gap)
        sunlight = next((r["sunlight"] for r in results if r["case"] == name), None)
        draw.multiline_text(
            (10, top + 10),
            f"{name}\nsunlight {sunlight:.2f}" if sunlight is not None else name,
            fill="black",
            font=font,
        )
        for k, (distance, kind) in enumerate(columns):
            result = next(
                (r for r in results if r["case"] == name and r["distance"] == distance),
                None,
            )
            row = result and result["rows"].get(kind)
            if not row or "screen" not in row:
                continue
            image = Image.open(result["shots"][kind]).convert("RGB")
            vw = int(row["viewport"].split("x")[0])
            scale = image.size[0] / vw
            x, y = (float(v) * scale for v in row["screen"].split(","))
            half = max(24, int(float(row["radius"]) * scale * 2.2))
            crop = image.crop((x - half, y - half, x + half, y + half))
            sheet.paste(
                crop.resize((cell, cell), Image.NEAREST),
                (label + k * (cell + gap), top),
            )
            draw.text(
                (label + k * (cell + gap) + 6, top + 6),
                f"{result[kind]:.1f}",
                fill="white",
                font=font,
            )
    sheet.save(target)
    print("sheet", target)


if __name__ == "__main__":
    analyze() if sys.argv[1:] == ["analyze"] else main()
