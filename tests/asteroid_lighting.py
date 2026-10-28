"""Asteroid impostor lighting against the real voxel asteroid (SE-745): an asteroid on the
day side of a planet, in the band where its shadow fades and in the shadow, each shot real
and then as its impostor, drawn beside the real one, from the sunlit side and from the side away
from the sun, at the distances where the impostor takes over.

The world is planet_shadow.py's: the game's Empty World with the EarthLike planet of its
Home System world at PLANET and the sun along +X. Every case has a 512 m asteroid of the
game's own VoxelMaps. The views keep the planet out of the frame, except day-planet, which
has its sunlit side behind the asteroid: the game's eye adaptation then darkens the real
asteroid, while the impostors, drawn after tone mapping, stay as they are.

It prints, per case, view and distance, the light of the real asteroid and its impostor:
the mean luminance and color of the pixels inside 0.15 of its radius on screen, which the
asteroid covers from every side, and the impostor's over the real one's. PASS when every
impostor is within MARGIN of its real asteroid, except the KNOWN cases.

    IMP_TASK=se745 uv run --no-project --with httpx --with pillow \\
        python tests/asteroid_lighting.py

The sheet lands in ~/.se-test/<task>-shots/asteroid-lighting.png, the numbers next to it in
asteroid-lighting.json. IMP_CASES=night,day runs only those cases, IMP_KEEP=1 leaves the
game running, IMP_TAG adds a suffix to the output names. With the argument "analyze" it
only redoes the checks and the sheet from the last run.
"""

from __future__ import annotations

import json
import re
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import planet_shadow as ps  # noqa: E402
import rig  # noqa: E402
import world  # noqa: E402

from planet_shadow import CLIENT, PLANET, SERVER, SUN, add, normalize  # noqa: E402

FILE = os.environ.get("IMP_ASTEROID", "Asteroid512-008.vx2")
SIZE = 512
TAG = os.environ.get("IMP_TAG", "")
SHOTS = ps.SHOTS
RAW = SHOTS / f"asteroid-lighting{TAG}-raw.json"
# Impostor over real, of the luminance
MARGIN = 0.2
# Shown, not checked: with the sunlit planet behind it the game's eye adaptation darkens the
# real asteroid, which impostors drawn after tone mapping can't follow, and seen from behind
# it sits next to the sun's glare. In the band and far behind the planet the game doesn't
# shade the real asteroid at all, while the impostor follows the planet's shadow (SE-713).
KNOWN = {
    ("day-planet", "front"): "eye adaptation",
    ("day-planet", "back"): "next to the sun's glare",
    ("band-66", "front"): "real one not shaded",
    ("far-night", "front"): "real one not shaded",
}

CASES = [
    ("day", (0.0, 300000.0, 0.0)),
    ("day-planet", (90000.0, 0.0, 0.0)),
    ("band-66", (-55000.0, 0.0, 66000.0)),
    ("night", (-90000.0, 0.0, 0.0)),
    ("far-night", (-150000.0, 30000.0, 0.0)),
]
if os.environ.get("IMP_CASES"):
    CASES = [c for c in CASES if c[0] in os.environ["IMP_CASES"].split(",")]
# Near where the real asteroid gives way to its impostor
DISTANCES = [float(d) for d in os.environ.get("IMP_DISTANCES", "3000,8000").split(",")]
# From the sun's side, and from the side away from it, where only ambient light reaches
VIEWS = {"front": (1.0, 0.3, 0.3), "back": (-1.0, 0.3, 0.3)}
KINDS = ["real", "impostor"]

ASTEROIDS = [
    world.Asteroid(
        f"Light{i}",
        130000000000000200 + i,
        tuple(c - SIZE / 2 for c in add(PLANET, offset)),
        FILE,
    )
    for i, (name, offset) in enumerate(CASES)
]


def prepare_world() -> None:
    world.prepare(
        SERVER.world, CLIENT.steam_id, CLIENT.name, grids=(), asteroids=ASTEROIDS
    )
    entry, vx2 = ps.planet_xml()
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


def look(api, asteroid, view, distance: float, far: bool) -> dict:
    eye = add(asteroid.center, normalize(VIEWS[view]), distance)
    api.character_teleport(*eye)
    time.sleep(15 if far else 4)
    character = api.get_character()
    if not character.get("jetpack"):
        api.key("X")
    if not character.get("dampeners"):
        api.character_set_dampeners(True)
    ps.write_mode("real")
    last = {}

    def ready():
        api.character_look_at(*asteroid.center, tolerance=0.1)
        row = CLIENT.stats()[1].get(asteroid.entity_id, {})
        last.update(row)
        if row.get("real") != "1" or "screen" not in row or row["screen"] == "behind":
            return None
        x, y = (float(v) for v in row["screen"].split(","))
        w, h = (float(v) for v in row["viewport"].split("x"))
        return row if abs(x - w / 2) < 60 and abs(y - h / 2) < 60 else None

    try:
        return ps.wait_for(
            ready, 120, f"{asteroid.name} at {distance:.0f} m not in view"
        )
    except TimeoutError as error:
        raise TimeoutError(f"{error}, last stats: {last}") from None


def light(shot: Path, row: dict) -> dict:
    """Mean luminance and RGB inside 0.15 of the radius on screen"""
    from PIL import Image

    image = Image.open(shot).convert("RGB")
    vw = int(row["viewport"].split("x")[0])
    scale = image.size[0] / vw
    x, y = (float(v) * scale for v in row["screen"].split(","))
    r = max(2.0, float(row["radius"]) * 0.15 * scale)
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


def main() -> None:
    world.SETTINGS.update(SyncDistance="20000", ViewDistance="50000")
    prepare_world()
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(ReplicationMargin=100000, MinPixels=0, MaxDistance=400000)
    )
    SHOTS.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        SERVER.start()
        CLIENT.launch(SERVER.port)
        api = CLIENT.wait_api()
        rig.wait_world(api)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        for asteroid, (name, offset) in zip(ASTEROIDS, CASES):
            first = True
            for view in VIEWS:
                for distance in DISTANCES:
                    try:
                        row = look(api, asteroid, view, distance, far=first)
                    except TimeoutError as error:
                        print(
                            f"FAIL {name} {view} {distance:.0f} m: {error}", flush=True
                        )
                        continue
                    first = False
                    # Voxel LODs and eye adaptation settle
                    time.sleep(5)
                    shots, rows = {}, {"real": row}
                    # The impostor beside the real one: hidden, the real asteroid still
                    # draws some of its cells, in front of the impostor's
                    character = api.get_character()
                    right = normalize(ps.cross(character["forward"], character["up"]))
                    side = " ".join(f"{v * 1.5 * SIZE:.1f}" for v in right)
                    for kind, mode in (("real", "real"), ("impostor", f"shift {side}")):
                        ps.write_mode(mode)
                        time.sleep(3)
                        path = (
                            SHOTS
                            / f"asteroid-lighting{TAG}-{name}-{view}-{int(distance)}-{kind}.png"
                        )
                        path.write_bytes(api.screenshot_bytes(ignore_sprites=True))
                        shots[kind] = str(path)
                        rows[kind] = CLIENT.stats()[1].get(asteroid.entity_id, row)
                    result = {
                        "case": name,
                        "view": view,
                        "distance": distance,
                        "sunlight": float(rows["impostor"].get("sunlight", "nan")),
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


def measure(result: dict) -> None:
    for kind in KINDS:
        result[kind] = light(Path(result["shots"][kind]), result["rows"][kind])
    result["ratio"] = round(
        result["impostor"]["luma"] / max(0.5, result["real"]["luma"]), 3
    )


def print_row(r: dict) -> None:
    print(
        f"{r['case']:10} {r['view']:5} {r['distance']:6.0f} m sun {r['sunlight']:.2f} "
        f"ambient {r.get('ambient', '')}  "
        f"real {r['real']['luma']:6.1f} {r['real']['rgb']}  "
        f"impostor {r['impostor']['luma']:6.1f} {r['impostor']['rgb']}  "
        f"ratio {r['ratio']:.2f}",
        flush=True,
    )


def analyze() -> None:
    results = json.loads(RAW.read_text())
    checks = []
    for r in results:
        measure(r)
        print_row(r)
        # Near black both: a few levels of difference are no jump
        ok = (
            abs(r["ratio"] - 1) <= MARGIN
            or abs(r["impostor"]["luma"] - r["real"]["luma"]) <= 4
        )
        name = f"{r['case']} {r['view']} {r['distance']:.0f} m"
        known = KNOWN.get((r["case"], r["view"]))
        if known:
            print(f"INFO {name}: impostor/real {r['ratio']:.2f} ({known})")
        else:
            checks.append((name, ok, r["ratio"]))
    for name, ok, ratio in checks:
        print(f"{'PASS' if ok else 'FAIL'} {name}: impostor/real {ratio:.2f}")
    compose(results, SHOTS / f"asteroid-lighting{TAG}.png")
    (SHOTS / f"asteroid-lighting{TAG}.json").write_text(json.dumps(results, indent=1))
    print("PASS" if checks and all(ok for _, ok, _ in checks) else "FAIL")


def compose(results, target: Path) -> None:
    """A row per case and view, per distance the real asteroid and its impostor"""
    from PIL import Image, ImageDraw, ImageFont

    cell, gap, label, header = 260, 6, 260, 60
    columns = [(d, k) for d in DISTANCES for k in KINDS]
    rows = [(c[0], v) for c in CASES for v in VIEWS]
    sheet = Image.new(
        "RGB",
        (label + len(columns) * (cell + gap), header + len(rows) * (cell + gap)),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default(size=20)
    for k, (distance, kind) in enumerate(columns):
        draw.text(
            (label + k * (cell + gap), 10), f"{distance:.0f} m\n{kind}", "black", font
        )
    for i, (name, view) in enumerate(rows):
        top = header + i * (cell + gap)
        sun = next((r["sunlight"] for r in results if r["case"] == name), float("nan"))
        draw.multiline_text(
            (10, top + 10), f"{name} {view}\nsunlight {sun:.2f}", "black", font
        )
        for k, (distance, kind) in enumerate(columns):
            r = next(
                (
                    r
                    for r in results
                    if r["case"] == name
                    and r["view"] == view
                    and r["distance"] == distance
                ),
                None,
            )
            if not r:
                continue
            row = r["rows"][kind]
            image = Image.open(r["shots"][kind]).convert("RGB")
            scale = image.size[0] / int(row["viewport"].split("x")[0])
            x, y = (float(v) * scale for v in row["screen"].split(","))
            half = max(24, int(float(row["radius"]) * scale * 1.6))
            crop = image.crop((x - half, y - half, x + half, y + half))
            sheet.paste(crop.resize((cell, cell)), (label + k * (cell + gap), top))
            draw.text(
                (label + k * (cell + gap) + 6, top + 6),
                f"{r[kind]['luma']:.1f}",
                "white",
                font,
            )
    sheet.save(target)
    print("sheet", target)


if __name__ == "__main__":
    analyze() if sys.argv[1:] == ["analyze"] else main()
