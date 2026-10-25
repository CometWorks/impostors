"""The asteroid pack of the "Octahedron - Seed 2" world (SE-701): writes it, then measures
what a client joining above the asteroid ring costs the server with and without it. Needs the
save (se1/worlds/octahedron/build_world.py --seed 2) and its Workshop mods downloaded, like
tests/ring.py.

    IMP_TASK=imp701 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/asteroid_pack.py write      # the pack, into ~/.se-test/<task>-pack
    ... python tests/asteroid_pack.py measure    # a join without the pack, then with it
    IMP_PACK_LEVEL=32 ...                        # write: finest level in the pack (64)
    IMP_HEIGHT=10000 IMP_SETTLE=240 ...          # measure: meters above the ring, seconds
    IMP_RUNS=pack ...                            # measure: only the run with the pack

`write` runs the server alone with WriteAsteroidPack, every asteroid is built, and prints the
pack's size and how long it took. `measure` starts the server and the client with empty shape
caches each time, so everything in view is built or taken from the pack. It prints a line every
10 seconds (impostors the client has, shapes it got and their KB, shapes from the pack) and a
summary per run: asteroid builds on the server and their CPU time, shape KB sent, shapes the
client took from the pack, and how long the client took to read the pack's index. The final
PASS/FAIL line checks that with the pack the server built no asteroid and sent no asteroid
shape.
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
import ring  # noqa: E402

PACK_ID = 900000203
PACK = rig.TEST_ROOT / f"{rig.TASK}-pack"
PACK_LEVEL = int(os.environ.get("IMP_PACK_LEVEL", "64"))
HEIGHT = float(os.environ.get("IMP_HEIGHT", "10000"))
SETTLE = float(os.environ.get("IMP_SETTLE", "240"))

SERVER = ring.SERVER
CLIENT = ring.CLIENT


def setup_world(with_pack: bool) -> list[int]:
    """The save with its mods staged on the server, plus the pack mod when asked"""
    ids = ring.copy_world(SERVER.world)
    if with_pack:
        for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
            f = SERVER.world / name
            text = f.read_text(encoding="utf-8").replace(
                "</Mods>",
                '<ModItem FriendlyName="Impostors Asteroid Pack">'
                f"<Name>{PACK_ID}.sbm</Name><PublishedFileId>{PACK_ID}</PublishedFileId>"
                "<PublishedServiceName>Steam</PublishedServiceName></ModItem>\n    </Mods>",
                1,
            )
            f.write_text(text, encoding="utf-8")
        ids.append(PACK_ID)
    SERVER.prepare([CLIENT.steam_id])
    cfg = SERVER.data / "SpaceEngineers-Dedicated.cfg"
    cfg.write_text(
        cfg.read_text(encoding="utf-8").replace(
            "<WorldName>ImpostorsTest</WorldName>",
            f"<WorldName>{ring.WORLD_NAME}</WorldName>",
        ),
        encoding="utf-8",
    )
    stage(SERVER.data / "Mods", ids)
    shutil.rmtree(SERVER.data / "ImpostorsCache", ignore_errors=True)
    return ids


def stage(mods: Path, ids) -> None:
    ring.stage_mods(mods, [i for i in ids if i != PACK_ID])
    if PACK_ID in ids:
        link = mods / f"{PACK_ID}.sbm"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(PACK)


def log_text() -> str:
    log = SERVER.game_log()
    return log.read_text(errors="replace") if log else ""


def write() -> int:
    setup_world(False)
    SERVER.write_plugin_config(
        rig.plugin_config(
            Grids=False,
            SafeZones=False,
            CacheMegabytes=0,
            WriteAsteroidPack=True,
            AsteroidPackMaxLevel=PACK_LEVEL,
        )
    )
    SERVER.start(timeout=3600)
    start = time.monotonic()
    try:
        deadline = start + 3600
        while True:
            text = log_text()
            m = re.search(r"Asteroid pack written: .* in (\S.*)$", text, re.M)
            if m or "Failed to write the asteroid pack" in text:
                break
            if time.monotonic() > deadline or SERVER.pid() is None:
                print("FAIL the pack was not written", flush=True)
                return 1
            time.sleep(5)
    finally:
        SERVER.stop()
    if not m:
        print("FAIL writing the pack failed, see the server log", flush=True)
        return 1
    folder = Path(m.group(1).strip())
    if PACK.exists():
        shutil.rmtree(PACK)
    shutil.copytree(folder, PACK)
    builds = re.findall(r"Impostor build: .*kind=Voxel .* cpu=([0-9.]+)", text)
    line = re.search(r"Asteroid pack written: .*$", text, re.M).group(0)
    print(
        f"PASS {line}\n  {len(builds)} asteroid builds, {sum(map(float, builds)) / 1000:.1f} s CPU, "
        f"{time.monotonic() - start:.0f} s after the server was ready; copied to {PACK}",
        flush=True,
    )
    return 0


def run(with_pack: bool) -> dict:
    tag = "pack" if with_pack else "nopack"
    ids = setup_world(with_pack)
    SERVER.write_plugin_config(
        rig.plugin_config(
            MaxDistance=500000, MaxImpostorsPerPlayer=4000, MaxQuadsPerFrame=16000
        )
    )
    center, normal, side, _ = ring.ring_geometry(SERVER.world)
    SERVER.start(timeout=3600)
    for cache in CLIENT.mod_storage.glob("cache-*"):
        cache.unlink()
    CLIENT.launch(SERVER.port)
    stage(CLIENT.appdata / "Mods", ids)
    result = {"tag": tag}
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        base = center + side * 120000
        tangent = np.cross(normal, side)
        eye = base + normal * (HEIGHT + 300)
        api.character_teleport(*eye)
        time.sleep(5)
        api.character_look_at(*(base + tangent * (HEIGHT + 5000)), tolerance=2)
        builds_before = len(re.findall(r"Impostor build: .*kind=Voxel", log_text()))
        start = time.monotonic()
        while time.monotonic() - start < SETTLE:
            time.sleep(10)
            totals, _ = CLIENT.stats()
            print(
                f"{tag} t={time.monotonic() - start:.0f}s impostors={totals.get('impostors')} "
                f"shapes={totals.get('shapes')} shapekb={totals.get('shapekb')} "
                f"cached={totals.get('cached')} packed={totals.get('packed')}",
                flush=True,
            )
        totals, _ = CLIENT.stats()
        text = log_text()
        cpus = [
            float(c)
            for c in re.findall(r"Impostor build: .*kind=Voxel .* cpu=([0-9.]+)", text)
        ]
        client_log = (
            CLIENT.log.read_text(errors="replace") if CLIENT.log.exists() else ""
        )
        index = re.search(r"asteroid pack of mod .* index read in (\d+) ms", client_log)
        reused = re.findall(r"the reused ones took (\d+) ms CPU", text)
        result.update(
            impostors=int(totals.get("impostors", 0)),
            shapes=int(totals.get("shapes", 0)),
            shapekb=int(totals.get("shapekb", 0)),
            packed=int(totals.get("packed", 0)),
            builds=len(cpus),
            builds_during=len(cpus) - builds_before,
            build_cpu_s=sum(cpus) / 1000,
            reused_cpu_s=sum(map(int, reused)) / 1000,
            index_ms=int(index.group(1)) if index else None,
        )
    finally:
        CLIENT.stop()
        SERVER.stop()
    print(
        f"{tag}: {result.get('impostors')} impostors, {result.get('builds')} asteroid builds "
        f"({result.get('build_cpu_s', 0):.1f} s CPU, reused ones {result.get('reused_cpu_s', 0):.1f} s "
        f"in whole minutes), {result.get('shapes')} shapes sent "
        f"({result.get('shapekb')} KB), {result.get('packed')} from the pack, "
        f"pack index read in {result.get('index_ms')} ms",
        flush=True,
    )
    return result


def measure() -> int:
    if not (PACK / "ImpostorsAsteroidPack.bin").exists():
        raise SystemExit(f"No pack in {PACK}, run write first")
    runs = os.environ.get("IMP_RUNS", "nopack,pack").split(",")
    without = run(False) if "nopack" in runs else {}
    with_pack = run(True)
    ok = with_pack.get("builds") == 0 and with_pack.get("packed", 0) > 0
    print(
        f"{'PASS' if ok else 'FAIL'} with the pack: {with_pack.get('builds')} asteroid builds, "
        f"{with_pack.get('packed')} shapes from the pack, {with_pack.get('shapekb')} KB of shapes "
        f"against {without.get('shapekb')} KB without",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode not in ("write", "measure"):
        raise SystemExit(__doc__)
    sys.exit(write() if mode == "write" else measure())
