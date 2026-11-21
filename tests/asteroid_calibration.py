"""Asteroid impostor lighting calibrated against the real asteroids (SE-1685): a few kinds of
asteroid, each seen real and then as its impostor drawn beside it, with the sun at several
angles to the line of sight, from a camera that stays put.

The world is the game's Empty World with the sun along SUN. Each SET is one asteroid kind
(size and surface) with a copy per PHASES angle: the angle at the asteroid between the sun
and the camera. Each copy has a camera position of its own, the same distance away and far
from the others: copies around one camera shade each other, the one at 180 lies on the sun
ray of the one at 0. 0 is the side facing the sun seen face on, 90 half lit with the
terminator across the middle, 180 the dark side with the sun right behind the asteroid. The
copies lie in a plane turned SPIN degrees further about the sun for each set, so each kind
shows other sides to the sun. Voxel maps can't rotate, so the sun's angle to the surface
changes by where the copy sits, not by turning it. The grid sets are static large grid
boxes: plain light armor, a mix of block kinds (armor, heavy armor, windows, conveyors,
batteries) and two-tone painted armor, placed the same way.

Per set, angle and region it prints the mean luminance and RGB of the real asteroid and of
its impostor, and the impostor's over the real one's. The regions are discs inside the
asteroid on screen, at the center and towards and away from the sun on screen. PASS when every
impostor region is within MARGIN of the real one (or within NEAR_BLACK levels of it, where
both are nearly black) and its tint within TINT_MARGIN, except the KNOWN cases.

    IMP_TASK=se1685 uv run --no-project --with httpx --with pillow \\
        python tests/asteroid_calibration.py

Under the Octahedron - Seed 2 environment (SunIntensity 150, EnvSkyboxBrightness 5), with
its environment mod staged from the Workshop folder:

    IMP_EXTRA_MODS=3415912276:<workshop>/3415912276 IMP_TAG=-seed2 ...

The sheet lands in ~/.se-test/<task>-shots/asteroid-calibration<tag>.png, the numbers next
to it in asteroid-calibration<tag>.json. IMP_CUBE="<six colors>" lights the impostors with
that ambient cube (the AsteroidAmbientCube setting, from tools/ambient_cube.py) instead of the
gray ambient light, as a pack with it would (SE-1743). IMP_SETS=stone512,ice256 runs only those sets,
IMP_PHASES=0,90 only those angles, IMP_KEEP=1 leaves the game running. With the argument
"analyze" it only redoes the checks and the sheet from the last run, "table" prints the
Markdown table of one or more result files (before and after, for a PR), with the RGB of each
region. Besides the luminance it prints the tint error: how far the impostor's color is from the
real one's, the mean over the regions of the channels' difference once both are scaled to the
same luminance, in parts of that luminance.

IMP_PLUGIN=1 runs the client with the client plugin, which draws the impostors before tone
mapping and so lights them with the mod's Hdr terms. To fit those, sweep a grid of sun and
ambient terms with IMP_SWEEP_GRID and fit them with "fitgrid" (SE-1798).
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

import planet_shadow as ps  # noqa: E402
import rig  # noqa: E402
import vx2_material  # noqa: E402
import world  # noqa: E402

from planet_shadow import CLIENT, SERVER, add, cross, dot, normalize  # noqa: E402

TAG = os.environ.get("IMP_TAG", "")
CUBE = os.environ.get("IMP_CUBE", "")
SHOTS = ps.SHOTS
RAW = SHOTS / f"asteroid-calibration{TAG}-raw.json"

# Not along an axis, so no side of the voxel cells faces it head on
SUN = normalize((1.0, 0.35, 0.2))

# Name, what it is, how far the camera is. Each is about 45 pixels in radius on a 720p
# screen there. An asteroid is a voxel map, its size and the material all its voxels get
# (None keeps the map's). A grid is a static large grid box (blocks on a side) of the GRID_STYLES.
SETS = [
    ("stone512", ("Asteroid512-008.vx2", 512, None), 3000.0),
    ("ice256", ("Asteroid256-005.vx2", 256, "Ice_01"), 1500.0),
    ("iron128", ("Asteroid128-003.vx2", 128, "Iron_02"), 750.0),
    ("rust128", ("Asteroid128-005.vx2", 128, "Iron_01"), 750.0),
    ("armor", ("grid", (20, 12, 28), "armor"), 600.0),
    ("mixed", ("grid", (20, 12, 28), "mixed"), 600.0),
    ("painted", ("grid", (20, 12, 28), "painted"), 600.0),
]
if os.environ.get("IMP_SETS"):
    SETS = [s for s in SETS if s[0] in os.environ["IMP_SETS"].split(",")]
PHASES = [float(p) for p in os.environ.get("IMP_PHASES", "0,45,90,135,180").split(",")]
# The cameras of the sets, this far apart along Z, and of their copies along Y
SET_SPACING = 80000.0
COPY_SPACING = 30000.0
# Degrees the plane of a set's copies turns about the sun from one set to the next
SPIN = 120.0

# IMP_SWEEP=1 also shoots the impostor with these lighting terms (the renderer's debug
# "light D W A": sun, wrap past the terminator, ambient scale), for fitting them: the
# ambient light alone, and the sun alone at two strengths and three wraps
SWEEP = (
    {
        "amb": "0 0 1",
        "d05": "0.5 0 0",
        "d1w0": "1 0 0",
        "d1w35": "1 0.35 0",
        "d1w70": "1 0.7 0",
    }
    if os.environ.get("IMP_SWEEP") == "1"
    else {}
)
# IMP_SWEEP_TERMS="key=D W A;...": these terms instead
if os.environ.get("IMP_SWEEP_TERMS"):
    SWEEP = dict(t.split("=", 1) for t in os.environ["IMP_SWEEP_TERMS"].split(";") if t)
# IMP_SWEEP_GRID="D,D,...;A,A,...;W": every D with every A at wrap W, for "fitgrid". Where
# the client plugin draws the impostors before tone mapping (IMP_PLUGIN=1), the screen isn't
# linear in the light, so the fit can't add up the ambient and sun shots as "fit" does.
if os.environ.get("IMP_SWEEP_GRID"):
    _d, _a, _w = os.environ["IMP_SWEEP_GRID"].split(";")
    SWEEP = {
        f"g_{d}_{_w}_{a}": f"{d} {_w} {a}" for d in _d.split(",") for a in _a.split(",")
    }

# Impostor over real, of the luminance
MARGIN = 0.2
NEAR_BLACK = 6.0
# And of its tint (see tint_error): at most 0.36 on good runs, over 1 on a broken frame that
# drew an impostor in nearly pure blue (SE-1798)
TINT_MARGIN = 0.5
# Shown, not checked: (set or None for every set, angle) -> why
GLARE = "the sun's disc and glare show around the real one, not behind the impostor"
KNOWN = {
    ("iron128", 180.0): GLARE,
    ("rust128", 180.0): GLARE,
    ("armor", 180.0): GLARE,
    ("mixed", 180.0): GLARE,
    ("painted", 180.0): GLARE,
}

# Regions, in parts of the asteroid's radius on screen: (name, offset towards the sun on
# screen, radius). Seen along the sun's line there's no sun side on screen and the side discs
# fall back to the center.
REGIONS = [("center", 0.0, 0.12), ("sun", 0.2, 0.08), ("shade", -0.2, 0.08)]


# Blocks of the grid styles, cycled over the shell, with their paint (ColorMaskHSV): light
# armor unpainted, a mix of block kinds unpainted, light armor in two tones of red
GRID_STYLES = {
    "armor": [("CubeBlock", "LargeBlockArmorBlock")],
    "mixed": [
        ("CubeBlock", "LargeBlockArmorBlock"),
        ("CubeBlock", "LargeHeavyBlockArmorBlock"),
        ("CubeBlock", "Window1x1Flat"),
        ("Conveyor", "LargeBlockConveyor"),
        ("BatteryBlock", "LargeBlockBatteryBlock"),
        ("CubeBlock", "LargeBlockArmorBlock"),
        ("CubeBlock", "LargeHeavyBlockArmorBlock"),
    ],
    "painted": [("CubeBlock", "LargeBlockArmorBlock")],
}
UNPAINTED = (0.0, -1.0, 0.0)
CELL = 2.5


def is_grid(what) -> bool:
    return what[0] == "grid"


def turn(v, axis, degrees):
    """v turned about the unit axis (Rodrigues)"""
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    k = cross(axis, v)
    d = dot(axis, v)
    return tuple(v[i] * c + k[i] * s + axis[i] * d * (1 - c) for i in range(3))


def camera_of(index: int, phase: float):
    return (0.0, COPY_SPACING * PHASES.index(phase), SET_SPACING * (index + 1))


def placement(index: int, phase: float, distance: float):
    """Center of the copy at this sun angle: the direction from it to the camera is the sun
    turned by the angle, in the set's plane"""
    side = normalize(cross(SUN, (0.0, 0.0, 1.0)))
    side = turn(side, SUN, SPIN * index)
    a = math.radians(phase)
    to_camera = tuple(math.cos(a) * s + math.sin(a) * p for s, p in zip(SUN, side))
    return add(camera_of(index, phase), to_camera, -distance)


# Per (set, angle): its entity id, center and how wide it is
TARGETS = {}
ASTEROIDS = []
GRIDS = []


def targets() -> None:
    for i, (name, what, distance) in enumerate(SETS):
        for k, phase in enumerate(PHASES):
            center = placement(i, phase, distance)
            entity = 130000000000001000 + 100 * i + k
            if is_grid(what):
                blocks = what[1]
                span = math.sqrt(sum((b * CELL) ** 2 for b in blocks))
                corner = tuple(c - (b - 1) / 2 * CELL for c, b in zip(center, blocks))
                GRIDS.append((f"Cal-{name}-{int(phase)}", entity, corner, what))
            else:
                file, size, material = what
                span = size
                ASTEROIDS.append(
                    world.Asteroid(
                        f"Cal-{name}-{int(phase)}",
                        entity,
                        tuple(c - size / 2 for c in center),
                        file,
                        size,
                    )
                )
            TARGETS[(name, phase)] = (entity, center, span)


targets()


def grid_xml(name, entity, position, what) -> str:
    """A hollow box of the style's blocks"""
    sx, sy, sz = what[1]
    style = GRID_STYLES[what[2]]
    blocks = []
    n = 0
    for x in range(sx):
        for y in range(sy):
            for z in range(sz):
                if 0 < x < sx - 1 and 0 < y < sy - 1 and 0 < z < sz - 1:
                    continue
                kind, subtype = style[n % len(style)]
                n += 1
                if what[2] == "painted":
                    # A darker band across the middle
                    paint = (world.RED, -0.3, 0.15 if abs(z - sz // 2) <= 2 else 0.45)
                else:
                    paint = UNPAINTED
                blocks.append(
                    f'<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_{kind}">'
                    f"<SubtypeName>{subtype}</SubtypeName>"
                    f'<Min x="{x}" y="{y}" z="{z}" />'
                    '<ColorMaskHSV x="{}" y="{}" z="{}" />'.format(*paint)
                    + "</MyObjectBuilder_CubeBlock>"
                )
    p = 'x="{}" y="{}" z="{}"'
    return (
        '<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_CubeGrid">'
        f"<SubtypeName /><EntityId>{entity}</EntityId>"
        "<PersistentFlags>CastShadows InScene</PersistentFlags>"
        "<PositionAndOrientation>"
        f"<Position {p.format(*position)} />"
        '<Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" />'
        "</PositionAndOrientation>"
        "<GridSizeEnum>Large</GridSizeEnum>"
        f"<CubeBlocks>{''.join(blocks)}</CubeBlocks>"
        "<IsStatic>true</IsStatic><IsUnsupportedStation>true</IsUnsupportedStation>"
        f"<DisplayName>{name}</DisplayName>"
        "<DestructibleBlocks>true</DestructibleBlocks>"
        "</MyObjectBuilder_EntityBase>"
    )


def prepare_world() -> None:
    # One material all over: copies of the maps, which world.prepare copies into the world
    staged = SHOTS / "maps"
    staged.mkdir(parents=True, exist_ok=True)
    files = {}
    for name, what, distance in SETS:
        if is_grid(what):
            continue
        file, size, material = what
        if material:
            target = staged / f"{name}.vx2"
            argv = sys.argv
            sys.argv = [argv[0], str(world.VOXEL_MAPS / file), str(target), material]
            vx2_material.main()
            sys.argv = argv
            files[name] = str(target)
    for a in ASTEROIDS:
        name = a.name.split("-")[1]
        if name in files:
            a.file = files[name]
    world.prepare(
        SERVER.world, CLIENT.steam_id, CLIENT.name, grids=(), asteroids=ASTEROIDS
    )
    sector = SERVER.world / "SANDBOX_0_0_0_.sbs"
    sector.write_text(
        sector.read_text(encoding="utf-8").replace(
            "<SectorObjects>",
            "<SectorObjects>" + "".join(grid_xml(*g) for g in GRIDS),
            1,
        ),
        encoding="utf-8",
    )
    sandbox = SERVER.world / "Sandbox.sbc"
    vector = 'x="{}" y="{}" z="{}"'.format(*SUN)
    sandbox.write_text(
        re.sub(
            r"<(BaseSunDirection|SunDirectionNormalized) [^/]*/>",
            rf"<\1 {vector} />",
            sandbox.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def aim(api, entity, center, label) -> dict:
    ps.write_mode("real")
    last = {}

    def ready():
        api.character_look_at(*center, tolerance=0.05)
        row = CLIENT.stats()[1].get(entity, {})
        last.update(row)
        if row.get("real") != "1" or "screen" not in row or row["screen"] == "behind":
            return None
        x, y = (float(v) for v in row["screen"].split(","))
        w, h = (float(v) for v in row["viewport"].split("x"))
        return row if abs(x - w / 2) < 40 and abs(y - h / 2) < 40 else None

    try:
        return ps.wait_for(ready, 120, f"{label} not in view")
    except TimeoutError as error:
        raise TimeoutError(f"{error}, last stats: {last}") from None


def main() -> None:
    world.SETTINGS.update(SyncDistance="20000", ViewDistance="50000")
    SHOTS.mkdir(parents=True, exist_ok=True)
    prepare_world()
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(ReplicationMargin=100000, MinPixels=0, MaxDistance=400000)
    )
    results = []
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        if CUBE:
            (CLIENT.mod_storage / "ambient-cube").write_text(CUBE)
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        for i, (name, what, distance) in enumerate(SETS):
            for phase in PHASES:
                api.character_teleport(*camera_of(i, phase))
                time.sleep(10)
                character = api.get_character()
                if not character.get("jetpack"):
                    api.key("X")
                if not character.get("dampeners"):
                    api.character_set_dampeners(True)
                entity, center, span = TARGETS[(name, phase)]
                try:
                    row = aim(api, entity, center, f"{name} {phase:.0f}")
                except TimeoutError as error:
                    print(f"FAIL {name} {phase:.0f}: {error}", flush=True)
                    continue
                # Voxel LODs and eye adaptation settle
                time.sleep(6)
                character = api.get_character()
                forward, up = character["forward"], character["up"]
                right = normalize(cross(forward, up))
                shots, rows = {}, {"real": row}
                side = " ".join(f"{v * 1.2 * span:.1f}" for v in right)
                modes = [("real", "real"), ("impostor", f"shift {side}")] + [
                    (f"sweep-{key}", f"shift {side} light {terms}")
                    for key, terms in SWEEP.items()
                ]
                for kind, mode in modes:
                    ps.write_mode(mode)
                    time.sleep(3)
                    path = (
                        SHOTS
                        / f"asteroid-calibration{TAG}-{name}-{int(phase)}-{kind}.png"
                    )
                    path.write_bytes(api.screenshot_bytes(ignore_sprites=True))
                    shots[kind] = str(path)
                    rows[kind] = CLIENT.stats()[1].get(entity, row)
                result = {
                    "set": name,
                    "phase": phase,
                    # The sun on screen, x right and y down, unit length or zero
                    "sun_screen": sun_on_screen(right, up),
                    "ambient": CLIENT.stats()[0].get("ambient", ""),
                    "rows": rows,
                    "shots": shots,
                }
                results.append(result)
                RAW.write_text(json.dumps(results, indent=1))
                measure(result)
                print_row(result)
        ps.write_mode("real")
    finally:
        if os.environ.get("IMP_KEEP") != "1":
            CLIENT.stop()
            SERVER.stop()
    RAW.write_text(json.dumps(results, indent=1))
    analyze()


def sun_on_screen(right, up) -> list:
    x, y = dot(SUN, right), -dot(SUN, up)
    length = math.hypot(x, y)
    return [x / length, y / length] if length > 0.2 else [0.0, 0.0]


def region_light(shot: Path, row: dict, sun: list, offset: float, size: float) -> dict:
    """Mean luminance and RGB of a disc, offset and size in parts of the radius on screen"""
    from PIL import Image

    image = Image.open(shot).convert("RGB")
    scale = image.size[0] / int(row["viewport"].split("x")[0])
    x, y = (float(v) * scale for v in row["screen"].split(","))
    radius = float(row["radius"]) * scale
    x += sun[0] * offset * radius
    y += sun[1] * offset * radius
    r = max(2.0, radius * size)
    pixels = [
        image.getpixel((i, j))
        for j in range(int(y - r), int(y + r) + 1)
        for i in range(int(x - r), int(x + r) + 1)
        if (i + 0.5 - x) ** 2 + (j + 0.5 - y) ** 2 <= r * r
        and 0 <= i < image.size[0]
        and 0 <= j < image.size[1]
    ]
    rgb = [sum(p[c] for p in pixels) / len(pixels) for c in range(3)]
    return {
        "luma": round(0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2], 2),
        "rgb": [round(c, 1) for c in rgb],
    }


def measure(result: dict) -> None:
    result["regions"] = {}
    for name, offset, size in REGIONS:
        light = {
            kind: region_light(
                Path(result["shots"][kind]),
                result["rows"][kind],
                result["sun_screen"],
                offset,
                size,
            )
            for kind in ("real", "impostor")
        }
        light["ratio"] = round(
            light["impostor"]["luma"] / max(0.5, light["real"]["luma"]), 3
        )
        result["regions"][name] = light
        for kind in result["shots"]:
            if kind.startswith("sweep-"):
                light[kind] = region_light(
                    Path(result["shots"][kind]),
                    result["rows"][kind],
                    result["sun_screen"],
                    offset,
                    size,
                )


def tint_error(light: dict) -> float:
    """How far the impostor's color is from the real one's, at the same luminance"""
    real, impostor = light["real"], light["impostor"]
    if real["luma"] < NEAR_BLACK or impostor["luma"] < NEAR_BLACK:
        return None
    return (
        sum(
            abs(i / impostor["luma"] - r / real["luma"])
            for i, r in zip(impostor["rgb"], real["rgb"])
        )
        / 3
    )


def mean_tint_error(results, regions=None) -> float:
    errors = [
        e
        for r in results
        if (r["set"], r["phase"]) not in KNOWN
        for name, light in r["regions"].items()
        if regions is None or name in regions
        if (e := tint_error(light)) is not None
    ]
    return sum(errors) / len(errors) if errors else float("nan")


def ok(light: dict) -> bool:
    tint = tint_error(light)
    return (tint is None or tint <= TINT_MARGIN) and (
        abs(light["ratio"] - 1) <= MARGIN
        or abs(light["impostor"]["luma"] - light["real"]["luma"]) <= NEAR_BLACK
    )


def print_row(r: dict) -> None:
    parts = [
        f"{n} {g['real']['luma']:5.1f}/{g['impostor']['luma']:5.1f} ({g['ratio']:.2f})"
        for n, g in r["regions"].items()
    ]
    c = r["regions"]["center"]
    print(
        f"{r['set']:9} {r['phase']:5.0f}  real/impostor  "
        + "  ".join(parts)
        + f"  rgb {c['real']['rgb']} / {c['impostor']['rgb']}",
        flush=True,
    )


def analyze() -> None:
    results = json.loads(RAW.read_text())
    checks = []
    for r in results:
        measure(r)
        print_row(r)
        known = KNOWN.get((r["set"], r["phase"])) or KNOWN.get((None, r["phase"]))
        for region, light in r["regions"].items():
            name = f"{r['set']} {r['phase']:.0f} {region}"
            if known:
                print(f"INFO {name}: impostor/real {light['ratio']:.2f} ({known})")
            else:
                checks.append((name, ok(light), light))
    for name, good, light in checks:
        print(
            f"{'PASS' if good else 'FAIL'} {name}: real {light['real']['luma']:.1f} "
            f"impostor {light['impostor']['luma']:.1f} ratio {light['ratio']:.2f}"
            + (f" tint error {t:.2f}" if (t := tint_error(light)) is not None else "")
        )
    compose(results, SHOTS / f"asteroid-calibration{TAG}.png")
    (SHOTS / f"asteroid-calibration{TAG}.json").write_text(
        json.dumps(results, indent=1)
    )
    passed = sum(good for _, good, _ in checks)
    print(f"{passed}/{len(checks)} regions within the margin")
    print(
        f"tint error {mean_tint_error(results):.3f}, "
        f"shade only {mean_tint_error(results, ['shade']):.3f}"
    )
    print("PASS" if checks and passed == len(checks) else "FAIL")


def table(files: list[str]) -> None:
    """Markdown table: per set, angle and region the real luminance and each file's
    impostor with its ratio, the files in the order given (before, after)"""
    runs = [json.loads(Path(f).read_text()) for f in files]
    names = [Path(f).stem for f in files]
    for n, run in zip(names, runs):
        print(
            f"- {n}: tint error {mean_tint_error(run):.3f}, "
            f"shade only {mean_tint_error(run, ['shade']):.3f}"
        )
    print()
    print(
        "| Asteroid | Sun angle | Region | Real | "
        + " | ".join(f"Impostor {n}" for n in names)
        + " |"
    )
    print("|" + "---|" * (4 + len(names)))
    cases = {}
    for run in runs:
        for x in run:
            cases.setdefault((x["set"], x["phase"]), x)
    for r in cases.values():
        for region in r["regions"]:
            cells = []
            for run in runs:
                m = next(
                    (
                        x
                        for x in run
                        if x["set"] == r["set"] and x["phase"] == r["phase"]
                    ),
                    None,
                )
                if m is None:
                    cells.append("")
                    continue
                g = m["regions"][region]
                mark = "" if ok(g) else " ✗"
                cells.append(
                    f"{g['impostor']['luma']:.0f} ({g['ratio']:.2f}){mark} "
                    + rgb(g["impostor"]["rgb"])
                )
            real = r["regions"][region]["real"]
            print(
                f"| {r['set']} | {r['phase']:.0f}° | {region} | "
                + f"{real['luma']:.0f} {rgb(real['rgb'])} | "
                + " | ".join(cells)
                + " |"
            )


def rgb(color) -> str:
    return "(" + ", ".join(f"{c:.0f}" for c in color) + ")"


def fit(files: list[str]) -> None:
    """Fits the lighting terms to sweep runs (IMP_SWEEP=1), separately for asteroids and
    grids: the sun's strength D, its wrap W past the terminator and the ambient scale A that
    bring the impostors' regions closest to the real ones'. An impostor's pixel is the
    ambient shot times A plus the sun shot of its wrap times D, the wraps between the swept
    ones interpolated, cut at white. Prints the sun shots' linearity first (d05 over d1w0:
    0.5 when the billboard colors reach the screen linearly)."""
    runs = [r for f in files for r in json.loads(Path(f).read_text())]
    wraps = {0.0: "d1w0", 0.35: "d1w35", 0.7: "d1w70"}
    samples = {"asteroid": [], "grid": []}
    halves = []
    for r in runs:
        if (r["set"], r["phase"]) in KNOWN:
            continue
        group = "grid" if r["set"] in GRID_STYLES else "asteroid"
        for g in r["regions"].values():
            if "sweep-amb" not in g:
                continue
            full = g["sweep-d1w0"]["luma"]
            if 40 < full < 200:
                halves.append(g["sweep-d05"]["luma"] / full)
            samples[group].append(
                (
                    g["real"]["luma"],
                    g["sweep-amb"]["luma"],
                    {w: g[f"sweep-{k}"]["luma"] for w, k in wraps.items()},
                )
            )
    if halves:
        print(f"d05/d1w0 median {sorted(halves)[len(halves) // 2]:.2f}")

    def sun(levels, w):
        keys = sorted(levels)
        for lo, hi in zip(keys, keys[1:]):
            if w <= hi:
                t = (w - lo) / (hi - lo)
                return levels[lo] * (1 - t) + levels[hi] * t
        return levels[keys[-1]]

    def error(group, d, w, a):
        out = []
        for real, amb, levels in samples[group]:
            predicted = min(255.0, amb * a + sun(levels, w) * d)
            out.append(math.log((predicted + 8) / (real + 8)))
        return sum(e * e for e in out) / len(out), out

    for group, rows in samples.items():
        if not rows:
            continue
        best = min(
            (error(group, d / 20, w / 20, a / 20)[0], d / 20, w / 20, a / 20)
            for d in range(10, 121, 2)
            for w in range(0, 15)
            for a in range(10, 81, 2)
        )
        _, d, w, a = best
        errs = error(group, d, w, a)[1]
        within = sum(abs(math.exp(e) - 1) <= MARGIN for e in errs)
        print(
            f"{group}: D {d:.2f} W {w:.2f} A {a:.2f}, "
            f"rms log error {math.sqrt(best[0]):.3f}, {within}/{len(errs)} regions within "
            f"{MARGIN:.0%} (sun shots at D 1, ambient at A 1)"
        )


def fit_grid(files: list[str]) -> None:
    """Fits the sun's strength D and the ambient scale A to grid sweep runs
    (IMP_SWEEP_GRID), separately for asteroids and grids: each region's luminance between the
    swept terms interpolated in log D and log A, the pair with the least error against the
    real ones' found on a fine grid between them. The wrap stays the swept one."""
    runs = [r for f in files for r in json.loads(Path(f).read_text())]
    samples = {"asteroid": [], "grid": []}
    wraps = set()
    for r in runs:
        if (r["set"], r["phase"]) in KNOWN:
            continue
        group = "grid" if r["set"] in GRID_STYLES else "asteroid"
        for g in r["regions"].values():
            levels = {}
            for kind, light in g.items():
                if kind.startswith("sweep-g_"):
                    d, w, a = (float(v) for v in kind[len("sweep-g_") :].split("_"))
                    wraps.add(w)
                    levels[(d, a)] = math.log(light["luma"] + 8)
            if levels:
                samples[group].append((math.log(g["real"]["luma"] + 8), levels))

    def at(levels, ds, as_, d, a):
        def bracket(values, v):
            i = max(0, min(len(values) - 2, sum(x <= v for x in values) - 1))
            lo, hi = values[i], values[i + 1]
            return lo, hi, (math.log(v) - math.log(lo)) / (math.log(hi) - math.log(lo))

        d0, d1, s = bracket(ds, d)
        a0, a1, t = bracket(as_, a)
        return (
            levels[(d0, a0)] * (1 - s) * (1 - t)
            + levels[(d1, a0)] * s * (1 - t)
            + levels[(d0, a1)] * (1 - s) * t
            + levels[(d1, a1)] * s * t
        )

    for group, rows in samples.items():
        if not rows:
            continue
        ds = sorted({d for d, _ in rows[0][1]})
        as_ = sorted({a for _, a in rows[0][1]})

        def errors(d, a):
            return [at(levels, ds, as_, d, a) - real for real, levels in rows]

        steps = 40
        best = min(
            (sum(e * e for e in errors(d, a)), d, a)
            for d in (ds[0] * (ds[-1] / ds[0]) ** (i / steps) for i in range(steps + 1))
            for a in (
                as_[0] * (as_[-1] / as_[0]) ** (j / steps) for j in range(steps + 1)
            )
        )
        _, d, a = best
        # IMP_FIT_BASE="asteroid=D,A;grid=D,A": also the error of these terms
        base = dict(
            t.split("=") for t in os.environ.get("IMP_FIT_BASE", "").split(";") if t
        )
        pairs = [("fit", d, a)]
        if group in base:
            pairs.append(("base", *(float(v) for v in base[group].split(","))))
        for label, d, a in pairs:
            errs = errors(d, a)
            within = sum(abs(math.exp(e) - 1) <= MARGIN for e in errs)
            print(
                f"{group} {label}: D {d:.2f} A {a:.2f} (W {sorted(wraps)}), rms log error "
                f"{math.sqrt(sum(e * e for e in errs) / len(errs)):.3f}, {within}/"
                f"{len(errs)} regions within {MARGIN:.0%}; swept D {ds}, A {as_}"
            )


def compose(results, target: Path) -> None:
    """A row per set, per sun angle the real asteroid and its impostor, the measured
    regions outlined"""
    from PIL import Image, ImageDraw, ImageFont

    cell, gap, label, header = 220, 6, 150, 60
    columns = [(p, k) for p in PHASES for k in ("real", "impostor")]
    sets = sorted({r["set"] for r in results}, key=[s[0] for s in SETS].index)
    sheet = Image.new(
        "RGB",
        (label + len(columns) * (cell + gap), header + len(sets) * (cell + gap)),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=18)
    for k, (phase, kind) in enumerate(columns):
        draw.text(
            (label + k * (cell + gap), 10), f"sun {phase:.0f}°\n{kind}", "black", font
        )
    for i, name in enumerate(sets):
        top = header + i * (cell + gap)
        draw.text((10, top + 10), name, "black", font)
        for k, (phase, kind) in enumerate(columns):
            r = next(
                (r for r in results if r["set"] == name and r["phase"] == phase), None
            )
            if not r:
                continue
            row = r["rows"][kind]
            image = Image.open(r["shots"][kind]).convert("RGB")
            scale = image.size[0] / int(row["viewport"].split("x")[0])
            x, y = (float(v) * scale for v in row["screen"].split(","))
            radius = float(row["radius"]) * scale
            half = max(24, int(radius * 0.9))
            crop = image.crop((x - half, y - half, x + half, y + half))
            outline = ImageDraw.Draw(crop)
            for _, offset, size in REGIONS:
                cx = half + r["sun_screen"][0] * offset * radius
                cy = half + r["sun_screen"][1] * offset * radius
                rr = max(2.0, radius * size)
                outline.ellipse(
                    (cx - rr, cy - rr, cx + rr, cy + rr), outline=(255, 0, 255)
                )
            sheet.paste(crop.resize((cell, cell)), (label + k * (cell + gap), top))
            draw.text(
                (label + k * (cell + gap) + 6, top + 6),
                f"{r['regions']['center'][kind]['luma']:.0f}",
                "yellow",
                font,
            )
    sheet.save(target)
    print("sheet", target)


if __name__ == "__main__":
    if sys.argv[1:] == ["analyze"]:
        analyze()
    elif sys.argv[1:2] == ["fit"]:
        fit(sys.argv[2:])
    elif sys.argv[1:2] == ["fitgrid"]:
        fit_grid(sys.argv[2:])
    elif sys.argv[1:2] == ["table"]:
        table(sys.argv[2:])
    else:
        main()
