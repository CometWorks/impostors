"""SE-727: observe shadowed, partly shaded, and sunlit asteroids in one fixed view.

IMP_TASK=se727off IMP_WORKSPACE=/home/viktor/ws/se uv run --no-project \
    --with httpx --with pillow python tests/shadow_flicker.py

Starts offline by default. IMP_MODE=server uses an isolated Magnetar with the same
scene and both the client plugin and mod. Records every drawn frame through the
test probe, in addition to screenshots and the ordinary once-per-second stats.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageStat

import local
import planet_shadow as shadow
import rig
import world

MODE = os.environ.get("IMP_MODE", "offline")
CLIENT = rig.Client(1, "Shadow observer", plugin=True, probe=True, quality="medium")
SERVER = rig.Server()
WORLD = (
    CLIENT.appdata / "Saves" / "ShadowFlicker" if MODE == "offline" else SERVER.world
)
OUT = rig.TEST_ROOT / f"{rig.TASK}-shots"
EYE = (-150000.0, 68500.0, -300000.0)
ROCKS = [
    world.Asteroid(
        f"{name}-{i}",
        130000000000001000 + group * 10 + i,
        (-175256.0, passing - 256, -300256.0 + (i - 1.5) * 1200),
        "Asteroid512-001.vx2",
    )
    for group, (name, passing) in enumerate(
        (("shadow", 56000.0), ("band", 69000.0), ("sunlit", 81000.0))
    )
    for i in range(4)
]
CONFIG = rig.plugin_config(MaxDistance=400000, MinPixels=0, MaxQuadsPerFrame=16000)


def wait(predicate, seconds: float, message: str):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(2)
    raise TimeoutError(message)


def prepare() -> None:
    world.SETTINGS.update(ViewDistance="15000", SyncDistance="3000")
    world.prepare(WORLD, CLIENT.steam_id, CLIENT.name, grids=(), asteroids=ROCKS)
    entry, voxel = shadow.planet_xml()
    sector = WORLD / "SANDBOX_0_0_0_.sbs"
    sector.write_text(
        sector.read_text().replace("<SectorObjects>", "<SectorObjects>" + entry, 1)
    )
    shutil.copy(voxel, WORLD / voxel.name)
    checkpoint = WORLD / "Sandbox.sbc"
    checkpoint.write_text(
        re.sub(
            r"<(BaseSunDirection|SunDirectionNormalized) [^/]*/>",
            r'<\1 x="1" y="0" z="0" />',
            checkpoint.read_text(),
        )
    )
    CLIENT.appdata.mkdir(parents=True, exist_ok=True)
    (CLIENT.appdata / "observe-shadow").write_text("startup")
    (CLIENT.appdata / "Impostors.cfg").write_text(CONFIG)


def verify_sources() -> None:
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=rig.REPO, text=True
    ).strip()
    latest = subprocess.check_output(
        ["git", "rev-parse", "origin/main"], cwd=rig.REPO, text=True
    ).strip()
    changed = subprocess.check_output(
        ["git", "diff", "HEAD", "--", "ClientPlugin", "Shared", "ImpostorsMod"],
        cwd=rig.REPO,
        text=True,
    )
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", latest, head], cwd=rig.REPO, check=True
    )
    assert not changed, "Commit production changes before testing their deployment"
    staged = CLIENT.appdata / "Mods" / f"{rig.MOD_ID}.sbm"
    assert staged.resolve() == rig.MOD_FOLDER.resolve(), staged
    if MODE != "offline":
        server_mod = SERVER.data / "Mods" / f"{rig.MOD_ID}.sbm"
        assert server_mod.resolve() == rig.MOD_FOLDER.resolve(), server_mod
        assert str(rig.REPO) in (SERVER.config / "Sources" / "sources.xml").read_text()
    hashes = {
        str(p.relative_to(rig.REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ("ClientPlugin", "Shared", "ImpostorsMod/Mod")
        for p in sorted((rig.REPO / folder).rglob("*"))
        if p.is_file() and (p.suffix == ".cs" or folder.endswith("/Mod"))
    }
    (OUT / "sources.json").write_text(
        json.dumps(
            {
                "commit": head,
                "main": latest,
                "staged_mod": str(staged.resolve()),
                "hashes": hashes,
            },
            indent=2,
        )
    )
    print(
        f"PASS source verification: {head}, staged mod {staged.resolve()}", flush=True
    )


def observation() -> list[dict[str, str]]:
    try:
        return [
            dict(field.split("=", 1) for field in line.split())
            for line in (CLIENT.appdata / "shadow-observation.txt")
            .read_text()
            .splitlines()
        ]
    except FileNotFoundError:
        return []


def check_results(rows: list[dict[str, str]]) -> None:
    errors = []
    for rock in ROCKS:
        views = [r for r in rows if r["source"].endswith(":" + str(rock.entity_id))]
        if len(views) != 1:
            errors.append(f"{rock.name}: {len(views)} renderers, expected one")
        expected = shadow.expected_sunlight(rock.center, shadow.PLANET, 60000, 78000)
        for row in views:
            if int(row["frames"]) < 100:
                errors.append(f"{rock.name}: too few observed frames")
            if any(abs(float(row[k]) - expected) > 0.002 for k in ("min", "max")):
                errors.append(
                    f"{rock.name}: sunlight {row['min']}..{row['max']}, expected {expected:.4f}"
                )
    images = [Image.open(p).convert("RGB") for p in sorted(OUT.glob("frame-*.png"))]
    assert len(images) >= 2, "Missing rendered frames"
    width, height = images[0].size
    differences = {}
    for name, passing in (("shadow", 56000), ("band", 69000), ("sunlit", 81000)):
        y = (1 - (passing - EYE[1]) / 25000 / math.tan(math.radians(35))) * height / 2
        box = (
            int(width / 2 - width * 48 / 1280),
            int(y - height * 12 / 720),
            int(width / 2 + width * 48 / 1280),
            int(y + height * 12 / 720),
        )
        differences[name] = max(
            sum(ImageStat.Stat(ImageChops.difference(a.crop(box), b.crop(box))).mean)
            / 3
            for a, b in zip(images, images[1:])
        )
        if differences[name] > 2:
            errors.append(
                f"{name}: rendered brightness changed by {differences[name]:.3f}"
            )
    (OUT / "pixel-differences.json").write_text(json.dumps(differences, indent=2))
    print(f"Pixel differences (0..255): {differences}", flush=True)
    for error in errors:
        print("FAIL " + error, flush=True)
    assert not errors, f"{len(errors)} shadow flicker checks failed"
    print(
        "PASS: one renderer per asteroid, steady correct sunlight and rendered pixels",
        flush=True,
    )


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    prepare()
    try:
        if MODE == "offline":
            args = CLIENT.args
            CLIENT.args = lambda _port: [
                a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
            ]
            CLIENT.launch(0)
            verify_sources()
            api = CLIENT.wait_api()
            api.load(str(WORLD), online_mode="OFFLINE")
            wait(lambda: local.past_questions(api), 180, "offline world load")
        else:
            SERVER.prepare([CLIENT.steam_id])
            SERVER.write_plugin_config(CONFIG)
            SERVER.start()
            CLIENT.launch(SERVER.port)
            verify_sources()
            api = CLIENT.wait_api()
        rig.ensure_character(api)
        rig.wait_world(api)
        rig.focus_gameplay(api)
        api.character_teleport(EYE[0] + 100, EYE[1], EYE[2])
        # The test probe fixes the camera every update, removing character sway.
        (CLIENT.appdata / "camera.txt").write_text(
            "-150000 68500 -300000 -90 0 0 0 0 0"
        )
        wait(
            lambda: len(observation()) >= len(ROCKS),
            180,
            "per-frame asteroid observations",
        )
        time.sleep(10)
        startup = observation()
        (OUT / "startup.json").write_text(json.dumps(startup, indent=2))
        (CLIENT.appdata / "observe-shadow").write_text("settled")
        time.sleep(3)
        print(
            f"Recording {MODE}: shadow/band/sunlit controls, 12 asteroids", flush=True
        )
        for i in range(30):
            if i % 3 == 0:
                (OUT / f"frame-{i:02}.png").write_bytes(
                    api.screenshot_bytes(ignore_sprites=True)
                )
            time.sleep(1)
        rows = observation()
        (OUT / "settled.json").write_text(json.dumps(rows, indent=2))
        for row in rows:
            id_ = int(row["source"].rsplit(":", 1)[1])
            rock = next((r for r in ROCKS if r.entity_id == id_), None)
            if rock is None:
                continue
            print(
                f"{rock.name} source={row['source']} frames={row['frames']} "
                f"sunlight={row['min']}..{row['max']} levels={row['levels']}",
                flush=True,
            )
        print(f"Evidence: {OUT}", flush=True)
        check_results(rows)
    finally:
        CLIENT.stop()
        if MODE != "offline":
            SERVER.stop()


if __name__ == "__main__":
    main()
