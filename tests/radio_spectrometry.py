"""The "[QoL] Radio Spectrometry" mod of the Octahedron world on a ring asteroid, once loaded
and once drawn only as an impostor (SE-984). Runs the "Octahedron - Seed 2" world like
tests/ring.py, pastes a station with an ore detector, a battery and an LCD showing the mod's
"Radio Spectrometry Reader" script, its detector facing the asteroid, and reads what the LCD
shows.

    IMP_TASK=se984 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/radio_spectrometry.py
    IMP_RS_LOADED=1200 IMP_RS_FAR=14000   # station height over the asteroid, in metres
    IMP_RS_PACK=<folder>     # an asteroid pack mod instead of the world's
    IMP_RS_NOEARLY=1         # the mod shows no early asteroids
    IMP_RS_PATCH=<file>      # Radio Spectrometry with this patch (Docs/RadioSpectrometry.patch)
    IMP_PLUGIN=1, IMP_SAVE, IMP_TAG and the rest as in tests/ring.py

Prints one PASS or FAIL line per case: the LCD shows "Scanning" and an asteroid, and a last
line comparing the two spectra. The LCD's sprites and screenshots go to
~/.se-test/<task>-shots. Exits with the number of failures.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import ring  # noqa: E402
from se_remote import CallOp  # noqa: E402

LOADED = float(os.environ.get("IMP_RS_LOADED", "1200"))
FAR = float(os.environ.get("IMP_RS_FAR", "14000"))
SCRIPT = "RadioSpectrometer"
RS_ID = 2687324923
PACK = os.environ.get("IMP_RS_PACK")
PATCH = os.environ.get("IMP_RS_PATCH")
NOEARLY = os.environ.get("IMP_RS_NOEARLY") == "1"
SERVER, CLIENT, SHOTS = ring.SERVER, ring.CLIENT, ring.SHOTS
# Grid cells: the detector looks along the grid's forward, -Z. Two LCDs beside it facing
# opposite ways, one of them faces the player behind the station.
DETECTOR, BATTERY = (0, 0, 0), (1, 0, 0)
LCDS = {(-1, 0, 0): "Forward", (-2, 0, 0): "Backward"}


def station_xml(name: str) -> str:
    def block(kind, subtype, cell, forward="Forward", extra=""):
        x, y, z = cell
        return (
            f'<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_{kind}">'
            f"<SubtypeName>{subtype}</SubtypeName>"
            f'<Min x="{x}" y="{y}" z="{z}" />'
            f'<BlockOrientation Forward="{forward}" Up="Up" />'
            f"{extra}</MyObjectBuilder_CubeBlock>"
        )

    blocks = (
        block(
            "OreDetector", "LargeOreDetector", DETECTOR, extra="<Enabled>true</Enabled>"
        )
        + block(
            "BatteryBlock",
            "LargeBlockBatteryBlock",
            BATTERY,
            extra="<Enabled>true</Enabled><CurrentStoredPower>3</CurrentStoredPower>"
            "<MaxStoredPower>3</MaxStoredPower>",
        )
        + "".join(
            block(
                "TextPanel", "LargeTextPanel", cell, facing, "<Enabled>true</Enabled>"
            )
            for cell, facing in LCDS.items()
        )
    )
    return (
        '<?xml version="1.0"?>'
        '<Definitions xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><ShipBlueprints>'
        '<ShipBlueprint xsi:type="MyObjectBuilder_ShipBlueprintDefinition">'
        f'<Id Type="MyObjectBuilder_ShipBlueprintDefinition" Subtype="{name}" />'
        "<CubeGrids><CubeGrid><SubtypeName /><EntityId>0</EntityId>"
        "<PersistentFlags>CastShadows InScene</PersistentFlags>"
        '<PositionAndOrientation><Position x="0" y="0" z="0" />'
        '<Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
        f"<GridSizeEnum>Large</GridSizeEnum><CubeBlocks>{blocks}</CubeBlocks>"
        f"<IsStatic>true</IsStatic><DisplayName>{name}</DisplayName></CubeGrid></CubeGrids>"
        "</ShipBlueprint></ShipBlueprints></Definitions>"
    )


def patch_rs() -> Path:
    """Radio Spectrometry with the patch, built anew"""
    patched = rig.TEST_ROOT / f"{rig.TASK}-rs"
    shutil.rmtree(patched, ignore_errors=True)
    shutil.copytree(ring.WORKSHOP / str(RS_ID), patched)
    for f in patched.rglob("*.cs"):
        f.write_bytes(f.read_bytes().replace(b"\r\n", b"\n"))
    subprocess.run(
        ["patch", "-p1", "-i", str(Path(PATCH).resolve())], cwd=patched, check=True
    )
    return patched


def stage_overrides(mods: Path, ids, patched: Path | None) -> None:
    """The pack and the patched Radio Spectrometry in place of the world's"""
    if PACK:
        for i in ids:
            if (ring.WORKSHOP / str(i) / "ImpostorsAsteroidPack.dat").exists():
                (mods / f"{i}.sbm").unlink()
                (mods / f"{i}.sbm").symlink_to(Path(PACK).resolve())
    if patched:
        (mods / f"{RS_ID}.sbm").unlink()
        (mods / f"{RS_ID}.sbm").symlink_to(patched)


def grid_to_world(api, grid: int, cell) -> np.ndarray:
    world = api.batch(calls=[CallOp.grid_to_world(grid, cell)]).call(0)["world"]
    return np.array(world, dtype=float)


def texts(surface: dict) -> list[str]:
    return [
        s.get("data") or ""
        for s in surface.get("sprites") or []
        if s.get("type") == "TEXT"
    ]


def spectrum(surface: dict) -> list[float]:
    """The plotted spectrum from the LCD's line sprites: 1 at the top, 0 at the bottom"""
    lines = [
        s
        for s in surface.get("sprites") or []
        if s.get("data") == "SquareSimple" and s["size"][1] == 1
    ]
    return [round((215 - l["position"][1]) / 210, 3) for l in lines]


def asteroid(api, around: np.ndarray, name: str | None = None) -> dict | None:
    maps = [
        m
        for m in api.get_voxel_maps()
        if m.get("name", "").startswith("Octahedron-Asteroid")
    ]
    if name:
        return next((m for m in maps if m["name"] == name), None)
    return min(
        maps, key=lambda m: np.linalg.norm(np.array(m["center"]) - around), default=None
    )


def impostor_of(entity_id: int) -> dict | None:
    return CLIENT.stats()[1].get(entity_id)


def voxel_ray(detector, ahead) -> str:
    """What the mod's own lookup finds along the detector's ray, through the client probe"""
    result = CLIENT.appdata / "voxelray-result.txt"
    result.unlink(missing_ok=True)
    ends = np.concatenate([detector + ahead * 5, detector + ahead * 25000])
    (CLIENT.appdata / "voxelray.txt").write_text(" ".join(f"{x:.3f}" for x in ends))
    deadline = time.monotonic() + 10
    while not result.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    return result.read_text().strip().replace("\n", "; ") if result.exists() else "none"


def dump_stacks(name: str) -> None:
    """Three samples of the client's managed stacks, when it stopped answering"""
    SHOTS.mkdir(parents=True, exist_ok=True)
    for sample in range(3):
        subprocess.run(
            f"dotnet-stack report -p $(pgrep -f '^{CLIENT.launcher} ' | tail -1) "
            f"> '{SHOTS / f'{name}-stacks{sample}.txt'}' 2>&1",
            shell=True,
            env={
                **os.environ,
                "PATH": f"{Path.home()}/.dotnet/tools:{os.environ['PATH']}",
            },
        )
        time.sleep(5)


def paste(api, case: str, spot, normal, side) -> list:
    return api.paste_blueprint(
        xml=station_xml(f"RS984 {case}"),
        position=tuple(spot),
        forward=tuple(-normal),
        up=tuple(side),
    )


def check(
    api, case: str, target: dict, normal, side, height: float
) -> tuple[bool, list]:
    """A station `height` metres over the asteroid, its detector facing it"""
    center = np.array(target["center"])
    spot = center + normal * height
    api.character_teleport(*(spot + normal * 30 + side * 20))
    time.sleep(3)
    # The asteroid must be loaded or gone, as the case wants, before the detector looks
    want_real = case == "loaded"
    deadline = time.monotonic() + 120
    while (asteroid(api, center, target["name"]) is not None) != want_real:
        if time.monotonic() > deadline:
            print(
                f"FAIL {case}: the client still {'lacks' if want_real else 'has'} "
                f"{target['name']}",
                flush=True,
            )
            return False, []
        time.sleep(2)
    try:
        grids = paste(api, case, spot, normal, side)
    except Exception:
        dump_stacks(f"{case}-paste")
        raise
    grid = grids[0]["entityId"]
    detector = grid_to_world(api, grid, DETECTOR)
    ahead = grid_to_world(api, grid, (0, 0, -1)) - detector
    facing = float(np.dot(ahead / np.linalg.norm(ahead), -normal))
    lcd = np.mean([grid_to_world(api, grid, cell) for cell in LCDS], axis=0)
    ray = voxel_ray(detector, ahead / np.linalg.norm(ahead))
    api.character_teleport(*(lcd + normal * 8))
    time.sleep(2)
    api.character_look_at(*lcd, tolerance=1)

    def read() -> dict:
        """The LCD showing the most, selecting the script where it isn't yet"""
        surfaces = []
        for cell in LCDS:
            surface = api.get_surface(grid, cell)
            if surface.get("contentType") != "SCRIPT":
                api.set_surface_script(grid, cell, SCRIPT)
            surfaces.append(surface)
        return max(surfaces, key=lambda x: len(x.get("sprites") or []))

    surface, deadline = {}, time.monotonic() + 90
    while time.monotonic() < deadline:
        surface = read()
        shown = texts(surface)
        if "Scanning" in shown or (
            "Undetected" in shown and time.monotonic() > deadline - 45
        ):
            break
        time.sleep(2)
    time.sleep(3)
    surface = read()
    shown = texts(surface)
    curve = spectrum(surface)
    real = asteroid(api, center, target["name"]) is not None
    imp = impostor_of(int(target["entityId"]))
    ok = "Scanning" in shown and "No Asteroid Detected" not in shown
    print(
        f"{'PASS' if ok else 'FAIL'} {case}: {height/1000:.1f} km over {target['name']} "
        f"(r={target['radius']:.0f} m), real={int(real)} "
        f"impostor={'none' if imp is None else 'drawn=' + imp.get('drawn', '?') + ' lod=' + imp.get('lod', '?')} "
        f"detector->asteroid {np.linalg.norm(detector - center):.0f} m facing {facing:.2f}, "
        f"lcd={[t for t in shown if t and not t.startswith('RS984')][:6]} "
        f"spectrum peak={max(curve, default=0):.2f} points={len(curve)} | voxel ray: {ray}",
        flush=True,
    )
    (SHOTS / f"{case}-lcd.json").write_text(json.dumps(surface, indent=1))
    (SHOTS / f"{case}-lcd.png").write_bytes(api.screenshot_bytes())
    # The asteroid as the player sees it from the station, zoomed
    api.character_teleport(*(spot + normal * 30 + side * 20))
    time.sleep(2)
    api.character_look_at(*center, tolerance=0.5)
    CLIENT.set_fov(max(2.0, np.degrees(4 * target["radius"] / height)))
    time.sleep(8)
    (SHOTS / f"{case}-asteroid.png").write_bytes(
        api.screenshot_bytes(ignore_sprites=True)
    )
    CLIENT.set_fov(None)
    api.close_grid(grid)
    return ok, curve


def main() -> int:
    ids = ring.copy_world(SERVER.world)
    patched = patch_rs() if PATCH else None
    center, normal, side, points = ring.ring_geometry(SERVER.world)
    SERVER.prepare([CLIENT.steam_id])
    cfg = SERVER.data / "SpaceEngineers-Dedicated.cfg"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "<WorldName>ImpostorsTest</WorldName>",
            f"<WorldName>{ring.WORLD_NAME}</WorldName>",
        ),
        encoding="utf-8",
    )
    ring.stage_mods(SERVER.data / "Mods", ids)
    stage_overrides(SERVER.data / "Mods", ids, patched)
    SERVER.write_plugin_config(rig.plugin_config(MaxDistance=500000))
    SERVER.start(timeout=3600)
    CLIENT.mod_storage.mkdir(parents=True, exist_ok=True)
    (CLIENT.mod_storage / "noearly").unlink(missing_ok=True)
    (CLIENT.appdata / "voxelray.txt").unlink(missing_ok=True)
    if NOEARLY:
        (CLIENT.mod_storage / "noearly").write_text("")
    CLIENT.launch(SERVER.port)
    ring.stage_mods(CLIENT.appdata / "Mods", ids)
    stage_overrides(CLIENT.appdata / "Mods", ids, patched)
    failures = 0
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        # The world's copy and paste is off: a server takes a paste only from an admin with
        # creative tools on
        api.set_admin_flag("creativeTools", True)
        api.set_admin_flag("ignorePcu", True)
        if api.get_view().get("thirdPerson"):
            api.key("V")
        SHOTS.mkdir(parents=True, exist_ok=True)
        # The saved asteroid nearest to a spot on the ring, found from close by
        base = points[
            np.argmin(np.linalg.norm(points - (center + side * 120000), axis=1))
        ]
        api.character_teleport(*(base + normal * 1500))
        time.sleep(10)
        target = None
        deadline = time.monotonic() + 120
        while target is None and time.monotonic() < deadline:
            target = asteroid(api, base)
            time.sleep(2)
        if target is None:
            print("FAIL no ring asteroid loaded near", base.round(0), flush=True)
            return 1
        print(
            f"asteroid {target['name']} id={target['entityId']} r={target['radius']:.0f}",
            flush=True,
        )
        curves = []
        for case, height in (("loaded", LOADED), ("impostor", FAR)):
            ok, curve = check(api, case, target, normal, side, height)
            failures += not ok
            curves.append(curve)
        a, b = curves
        diff = (
            max((abs(x - y) for x, y in zip(a, b)), default=1.0)
            if len(a) == len(b)
            else 1.0
        )
        same = bool(a) and diff < 0.01
        failures += not same
        print(
            f"{'PASS' if same else 'FAIL'} same spectrum both ways: largest difference {diff:.3f}",
            flush=True,
        )
    finally:
        CLIENT.stop()
        SERVER.stop()
    return failures


if __name__ == "__main__":
    sys.exit(main())
