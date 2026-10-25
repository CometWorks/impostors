"""Above the asteroid ring of the Octahedron world (SE-666): what the client gets and draws
at several heights over the ring, with the world's mods. Needs the "Octahedron - Seed 2"
save (se1/worlds/octahedron/build_world.py --seed 2) and its Workshop mods downloaded.

    IMP_TASK=oct666 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/ring.py
    IMP_PLUGIN=1 ...          # the client runs the client plugin, the world has no mod
    IMP_QUALITY=extreme ...   # the client's graphics preset (minimal)
    IMP_MOD_FOLDER=<path>     # the mod to stage instead of this working copy's
    IMP_TAG=<name>            # names the run in the output and the screenshots

Prints one line per height with the replicated voxel maps the client has near and far, then
one PASS or FAIL line per view: looking down at the ring, along it, zoomed in on it and
while sweeping the zoomed view across it. A view passes when the client draws every impostor
in its view; the line also has the quads and the billboards the game drew or cut. Exits
with the number of failures. Screenshots
go to ~/.se-test/<task>-shots.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402

PLUGIN = os.environ.get("IMP_PLUGIN") == "1"
QUALITY = os.environ.get("IMP_QUALITY", "minimal")
HEIGHTS = [
    float(h)
    for h in os.environ.get("IMP_HEIGHTS", "0,1000,3000,6000,10000,20000,40000").split(
        ","
    )
]
SETTLE = float(os.environ.get("IMP_SETTLE", "60"))

HOME = Path.home()
SAVE = HOME / ".config/SpaceEngineers/Saves/76561198223054696/Octahedron - Seed 2"
ORE_MOD = HOME / ".config/SpaceEngineers/Mods/OctahedronOres-Seed2"
WORKSHOP = HOME / ".steam/steam/steamapps/workshop/content/244850"
ORE_MOD_ID = 900000202
WORLD_NAME = "Octahedron - Seed 2"
SHOTS = rig.TEST_ROOT / f"{rig.TASK}-shots"
STATS = None  # the client's billboard-stats.txt
rig.SERVER_GIB = 8.0
# Vertical field of view of the zoomed checks, in degrees
ZOOM_FOV = 10
failures = 0

SERVER = rig.Server()
SERVER.world = SERVER.data / "Saves" / WORLD_NAME
CLIENT = rig.Client(1, "Viktor", plugin=PLUGIN, quality=QUALITY, probe=True)
STATS = CLIENT.appdata / "billboard-stats.txt"


def copy_world(target: Path) -> list[int]:
    """The save with the ore mod and Impostors under their stand-in ids (no Impostors with
    the client plugin), online. Returns the world's mod ids."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SAVE, target, ignore=shutil.ignore_patterns("Backup"))
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = target / name
        text = f.read_text(encoding="utf-8")
        text = re.sub(
            r"<Name>OctahedronOres-Seed2</Name>\s*(<PublishedFileId>0</PublishedFileId>)?",
            f"<Name>{ORE_MOD_ID}.sbm</Name><PublishedFileId>{ORE_MOD_ID}</PublishedFileId>",
            text,
        )
        text = re.sub(
            r'\s*<ModItem FriendlyName="Impostors">.*?</ModItem>', "", text, flags=re.S
        )
        if not PLUGIN:
            text = text.replace(
                "</Mods>",
                '<ModItem FriendlyName="Impostors">'
                f"<Name>{rig.MOD_ID}.sbm</Name><PublishedFileId>{rig.MOD_ID}</PublishedFileId>"
                "<PublishedServiceName>Steam</PublishedServiceName></ModItem>\n    </Mods>",
                1,
            )
        text = text.replace(
            "<OnlineMode>OFFLINE</OnlineMode>", "<OnlineMode>PUBLIC</OnlineMode>"
        )
        f.write_text(text, encoding="utf-8")
    text = (target / "Sandbox_config.sbc").read_text(encoding="utf-8")
    return [
        int(i) for i in re.findall(r"<PublishedFileId>(\d+)</PublishedFileId>", text)
    ]


def stage_mods(mods: Path, ids) -> None:
    mods.mkdir(parents=True, exist_ok=True)
    for i in ids:
        target = {ORE_MOD_ID: ORE_MOD, rig.MOD_ID: rig.MOD_FOLDER}.get(
            i, WORKSHOP / str(i)
        )
        if not target.is_dir():
            raise SystemExit(f"Mod {i} has no folder at {target}")
        link = mods / f"{i}.sbm"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)


def ring_geometry(world: Path):
    """Center, normal and an in-plane direction of the ring, from its asteroids"""
    text = (world / "SANDBOX_0_0_0_.sbs").read_text(encoding="utf-8")
    points = []
    for m in re.finditer(r"<StorageName>Octahedron-Asteroid-\d+</StorageName>", text):
        start = text.rfind("<MyObjectBuilder_EntityBase", 0, m.start())
        p = re.search(
            r'<Position x="([^"]+)" y="([^"]+)" z="([^"]+)"', text[start : m.start()]
        )
        points.append([float(v) for v in p.groups()])
    points = np.array(points)
    center = points.mean(0)
    normal = np.linalg.svd(points - center)[2][2]
    if normal[1] > 0:
        normal = -normal
    side = np.cross(normal, [1.0, 0, 0])
    return center, normal, side / np.linalg.norm(side), points


def probe_stats() -> dict:
    try:
        return dict(kv.split("=", 1) for kv in STATS.read_text().split())
    except (FileNotFoundError, ValueError):
        return {}


def measure(api, name: str, shot: bool = True) -> None:
    """One line of the mod's and the probe's numbers, a screenshot and the stats"""
    global failures
    totals, impostors = CLIENT.stats()
    real = sum(1 for i in impostors.values() if i.get("real") == "1")
    b = probe_stats()
    inview, drawn = int(totals.get("inview", -1)), int(totals.get("drawn", -1))
    ok = drawn == inview
    failures += not ok
    print(
        f"{'PASS' if ok else 'FAIL'} {name} impostors={totals.get('impostors')} "
        f"inview={inview} drawn={drawn} realskip={real} "
        f"quads={totals.get('quads')} maxquads={totals.get('maxquads')} "
        f"bb_maxtotal={b.get('maxtotal')} cutothers={b.get('cutothers')} "
        f"cutimpostors={b.get('cutimpostors')}",
        flush=True,
    )
    if shot:
        (SHOTS / f"{name}.png").write_bytes(api.screenshot_bytes(ignore_sprites=True))
        shutil.copy(CLIENT.mod_storage / "stats.txt", SHOTS / f"{name}-stats.txt")


def main() -> int:
    ids = copy_world(SERVER.world)
    center, normal, side, points = ring_geometry(SERVER.world)
    SERVER.prepare([CLIENT.steam_id])
    cfg = SERVER.data / "SpaceEngineers-Dedicated.cfg"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "<WorldName>ImpostorsTest</WorldName>",
            f"<WorldName>{WORLD_NAME}</WorldName>",
        ),
        encoding="utf-8",
    )
    stage_mods(SERVER.data / "Mods", ids)
    SERVER.write_plugin_config(
        rig.plugin_config(
            MaxDistance=500000, MaxImpostorsPerPlayer=4000, MaxQuadsPerFrame=16000
        )
    )
    print(
        f"ring center {center.round(0)} normal {normal.round(3)}, {len(points)} asteroids",
        flush=True,
    )
    SERVER.start(timeout=3600)
    CLIENT.launch(SERVER.port)
    stage_mods(CLIENT.appdata / "Mods", ids)
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        SHOTS.mkdir(parents=True, exist_ok=True)
        base = center + side * 120000
        tangent = np.cross(normal, side)
        tag = (
            os.environ.get("IMP_TAG") or ("plugin" if PLUGIN else "mod") + "-" + QUALITY
        )
        for height in HEIGHTS:
            eye = base + normal * (height + 300)
            api.character_teleport(*eye)
            time.sleep(5)
            # Down at the ring and ahead along it, so the near and the far asteroids show
            target = base + tangent * (height + 5000)
            api.character_look_at(*target, tolerance=2)
            CLIENT.set_fov(None)
            time.sleep(SETTLE)
            # Replicated voxel maps the client has, and how far
            rel = points - eye
            box = int((np.abs(rel) < 15000 / np.sqrt(3)).all(axis=1).sum())
            maps = [
                m
                for m in api.get_voxel_maps()
                if m.get("name", "").startswith("Octahedron-Asteroid")
            ]
            near = sum(
                np.linalg.norm(np.array(m["center"]) - eye) < 15000 for m in maps
            )
            print(
                f"{tag} h={height/1000:.0f}km box={box} clientmaps={len(maps)} within15km={near}",
                flush=True,
            )
            name = f"{tag}-{height/1000:.0f}km"
            measure(api, name + "-down")
            # Along the ring, then zoomed in on it and swept across it
            api.character_look_at(
                *(eye + tangent * 20000 - normal * (height + 300)), tolerance=2
            )
            time.sleep(5)
            measure(api, name + "-along")
            # 30 km ahead on the ring, past the real asteroids, then across the band
            far = base + tangent * (height + 30000)
            api.character_look_at(*far, tolerance=1)
            CLIENT.set_fov(ZOOM_FOV)
            time.sleep(10)
            measure(api, name + "-zoom")
            for step in range(1, 7):
                api.character_look_at(*(far + side * (step - 3.5) * 3000), tolerance=1)
                time.sleep(1)
                measure(api, f"{name}-sweep{step}", shot=step == 6)
            CLIENT.set_fov(None)
            time.sleep(2)
    finally:
        CLIENT.stop()
        SERVER.stop()
    return failures


if __name__ == "__main__":
    sys.exit(main())
