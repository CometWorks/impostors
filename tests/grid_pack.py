"""The grid pack of the "Octahedron - Seed 2" world (SE-774): writes it and checks it. Needs the
save (se1/worlds/octahedron/build_world.py --seed 2) and its Workshop mods downloaded, like
tests/ring.py.

    IMP_TASK=se774 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/grid_pack.py write    # the pack, into ~/.se-test/<task>-gridpack
    ... python tests/grid_pack.py again    # spawn everything again with the pack: hit rate
    ... python tests/grid_pack.py measure  # join without the pack, with it, then the fallback
    ... python tests/grid_pack.py offline  # offline with the client plugin
    ... python tests/grid_pack.py lobby    # a lobby host with the client plugin and a joiner

`write` runs the server alone with WriteGridPack and prints the pack's size, plain and gzipped.
`again` does the same with the pack in the world and new entity ids: every grid group that
hashes differently from the first time is one a player would get over the network.

`measure` starts server and client with empty caches, 10 km over the ring, and compares shapes
sent. With the pack it then spawns a sample of prefabs (they should come from the pack, their shapes not
sent), powered as in play, and removes a layer of one's blocks (built, its shapes sent).

`offline` and `lobby` check that the local service takes the grids from the pack, and the
joiner reads them from it. Every check prints a PASS or FAIL line, the exit code counts FAILs.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import ring  # noqa: E402
import asteroid_pack as asteroids  # noqa: E402
import local  # noqa: E402

PACK_ID = 900000204
PACK = rig.TEST_ROOT / f"{rig.TASK}-gridpack"
PACK_FILE = "ImpostorsGridPack.dat"
HEIGHT = float(os.environ.get("IMP_HEIGHT", "10000"))
SETTLE = float(os.environ.get("IMP_SETTLE", "90"))
# Which prefabs `measure` spawns, by size: every step-th of the largest ones
PREFAB = int(os.environ.get("IMP_PREFAB", "5"))
PREFAB_STEP = int(os.environ.get("IMP_PREFAB_STEP", "40"))
PREFABS = int(os.environ.get("IMP_PREFABS", "1190"))
# What the prefabs spawned in play may miss, percent
MAX_LIVE_MISSES = float(os.environ.get("IMP_MAX_LIVE_MISSES", "30"))
# What `again` may miss, percent of the shapes
MAX_MISSES = float(os.environ.get("IMP_MAX_MISSES", "5"))

SERVER = ring.SERVER
CLIENT = ring.CLIENT
failures = 0

GRID_BUILD = r"Impostor build: .*kind=Grid"
GRID_PACKED = r"Impostor from the grid pack: .*kind=Grid"
CONFIG = dict(
    Asteroids=False,
    SafeZones=False,
    MaxDistance=500000,
    MaxImpostorsPerPlayer=4000,
    MinPixels=0.1,
    RebuildSeconds=5,
)


def check(name: str, ok: bool, detail="") -> None:
    global failures
    failures += not ok
    print(
        f"{'PASS' if ok else 'FAIL'} {name}{f': {detail}' if detail != '' else ''}",
        flush=True,
    )


def add_pack(world: Path) -> None:
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world / name
        f.write_text(
            f.read_text(encoding="utf-8").replace(
                "</Mods>",
                '<ModItem FriendlyName="Impostors Grid Pack">'
                f"<Name>{PACK_ID}.sbm</Name><PublishedFileId>{PACK_ID}</PublishedFileId>"
                "<PublishedServiceName>Steam</PublishedServiceName></ModItem>\n    </Mods>",
                1,
            ),
            encoding="utf-8",
        )


def stage(mods: Path, ids) -> None:
    ring.stage_mods(mods, [i for i in ids if i != PACK_ID])
    if PACK_ID in ids:
        link = mods / f"{PACK_ID}.sbm"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(PACK)


def setup_server(with_pack: bool) -> list[int]:
    ids = ring.copy_world(SERVER.world)
    if with_pack:
        add_pack(SERVER.world)
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
    for cache in ("ImpostorsCache", "Storage/Impostors/Cache"):
        shutil.rmtree(SERVER.data / cache, ignore_errors=True)
    return ids


def run_writer(with_pack: bool) -> tuple[str, float]:
    """The server until it has written the pack: its log and the seconds it took"""
    setup_server(with_pack)
    SERVER.write_plugin_config(
        rig.plugin_config(
            Asteroids=False, SafeZones=False, CacheMegabytes=0, WriteGridPack=True
        )
    )
    SERVER.start(timeout=3600)
    start = time.monotonic()
    try:
        while time.monotonic() - start < 7200:
            text = asteroids.log_text()
            if "Grid pack written:" in text or SERVER.pid() is None:
                break
            time.sleep(5)
    finally:
        SERVER.stop()
    return asteroids.log_text(), time.monotonic() - start


def written_folder(text: str) -> Path | None:
    m = re.search(r"Grid pack folder: (\S.*)$", text, re.M)
    return Path(m.group(1).strip()) if m else None


def write() -> None:
    text, seconds = run_writer(False)
    line = re.search(r"Grid pack written: .*$", text, re.M)
    if not line:
        check("the pack is written", False, "see the server log")
        return
    folder = written_folder(text)
    if PACK.exists():
        shutil.rmtree(PACK)
    shutil.copytree(folder, PACK)
    data = (PACK / PACK_FILE).read_bytes()
    print(
        f"{line.group(0)}\n{seconds:.0f} s after the server was ready, "
        f"{len(data) / 1048576:.1f} MB, {len(gzip.compress(data, 6)) / 1048576:.1f} MB gzipped, "
        f"{len(re.findall(GRID_BUILD, text))} builds; copied to {PACK}",
        flush=True,
    )
    check("the pack is written", True)


def again() -> None:
    text, seconds = run_writer(True)
    built = len(re.findall(GRID_BUILD, text))
    packed = len(re.findall(GRID_PACKED, text))
    misses = 100 * built / max(1, built + packed)
    print(f"{packed} from the pack, {built} built in {seconds:.0f} s", flush=True)
    check(
        "spawned again the groups hit the pack",
        misses <= MAX_MISSES,
        f"{misses:.1f}% missed",
    )


def counts(pattern: str) -> int:
    return len(re.findall(pattern, asteroids.log_text()))


def join(with_pack: bool) -> dict:
    ids = setup_server(with_pack)
    SERVER.write_plugin_config(rig.plugin_config(**CONFIG))
    center, normal, side, _ = ring.ring_geometry(SERVER.world)
    SERVER.start(timeout=3600)
    for cache in CLIENT.mod_storage.glob("cache-*"):
        cache.unlink()
    CLIENT.launch(SERVER.port)
    stage(CLIENT.appdata / "Mods", ids)
    tag = "pack" if with_pack else "nopack"
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        eye = center + side * 120000 + normal * HEIGHT
        api.character_teleport(*eye)
        time.sleep(SETTLE)
        totals, _ = CLIENT.stats()
        result = {
            "impostors": int(totals.get("impostors", 0)),
            "shapes": int(totals.get("shapes", 0)),
            "shapekb": int(totals.get("shapekb", 0)),
            "packed": int(totals.get("packed", 0)),
            "builds": counts(GRID_BUILD),
            "frompack": counts(GRID_PACKED),
        }
        print(f"{tag}: {result}", flush=True)
        if with_pack:
            check("impostors shown", result["impostors"] > 0, result["impostors"])
            check("no grid shape sent", result["shapes"] == 0, result["shapes"])
            check("read from the pack", result["packed"] > 0, result["packed"])
            check("no grid built", result["builds"] == 0, result["builds"])
            fallback(api, eye + normal * 30000, side, result)
        return result
    finally:
        CLIENT.stop()
        SERVER.stop()


def grid_blocks(api) -> dict[int, int]:
    """The server's grids and their block counts"""
    return {
        int(i): int(n)
        for i, n in re.findall(
            r"(-?\d+):(\d+)", asteroids.command(api, "!iprobe grids")
        )
    }


def fallback(api, spot, side, before: dict) -> None:
    known = grid_blocks(api)
    for k, n in enumerate(range(PREFAB, PREFABS, PREFAB_STEP)):
        at = spot + side * (k * 20000.0)
        print(
            asteroids.command(
                api, f"!iprobe prefab {n} {at[0]:.0f} {at[1]:.0f} {at[2]:.0f}"
            ),
            flush=True,
        )
        time.sleep(2)
    time.sleep(45)
    totals, _ = CLIENT.stats()
    new = {i: n for i, n in grid_blocks(api).items() if i not in known}
    check("the prefabs spawned", bool(new), len(new))
    built = counts(GRID_BUILD) - before["builds"]
    packed = counts(GRID_PACKED) - before["frompack"]
    print(
        f"spawned while powered: {packed} from the pack, {built} built, "
        f"{totals.get('shapes')} shapes sent, {totals.get('packed')} read from the pack",
        flush=True,
    )
    for line in re.findall(GRID_BUILD + r".*?blocks=\d+", asteroids.log_text()):
        print(" ", line[-60:], flush=True)
    check(
        "most prefabs come from the pack",
        100 * built / max(1, built + packed) <= MAX_LIVE_MISSES,
        f"{built} of {built + packed} missed",
    )
    check(
        "the client reads them from the pack",
        int(totals.get("packed", 0)) > before["packed"],
        totals.get("packed"),
    )
    if not new:
        return
    grid = max(new, key=new.get)
    shapes = int(totals.get("shapes", 0))
    builds = counts(GRID_BUILD)
    edge = re.search(r"(-?\d+)\s*$", asteroids.command(api, f"!iprobe maxx {grid}"))
    print(
        asteroids.command(api, f"!iprobe remove {grid} x {edge.group(1)}"), flush=True
    )
    time.sleep(30)
    totals, _ = CLIENT.stats()
    check(
        "changed, it is built", counts(GRID_BUILD) > builds, counts(GRID_BUILD) - builds
    )
    check(
        "changed, its shapes are sent",
        int(totals.get("shapes", 0)) > shapes,
        int(totals.get("shapes", 0)) - shapes,
    )


def measure() -> None:
    without = join(False)
    with_pack = join(True)
    print(f"without {without}\nwith {with_pack}", flush=True)
    check(
        "the same impostors with the pack",
        with_pack["impostors"] >= without["impostors"] * 0.95,
        f"{without['impostors']} -> {with_pack['impostors']}",
    )


def local_run(lobby: bool) -> None:
    host = rig.Client(2, "Viktor", plugin=True)
    world_dir = host.appdata / "Saves" / ring.WORLD_NAME
    ring.PLUGIN = not lobby
    ids = ring.copy_world(world_dir)
    online = "FRIENDS" if lobby else "OFFLINE"
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world_dir / name
        f.write_text(
            f.read_text(encoding="utf-8").replace(
                "<OnlineMode>PUBLIC</OnlineMode>", f"<OnlineMode>{online}</OnlineMode>"
            ),
            encoding="utf-8",
        )
    add_pack(world_dir)
    ids.append(PACK_ID)
    center, normal, side, _ = ring.ring_geometry(world_dir)
    host.appdata.mkdir(parents=True, exist_ok=True)
    (host.appdata / "Impostors.cfg").write_text(
        rig.plugin_config(**CONFIG), encoding="utf-8"
    )
    for cache in ("ImpostorsCache", "Storage/Impostors/Cache"):
        shutil.rmtree(host.appdata / cache, ignore_errors=True)
    args = host.args
    port = rig.SLOT["DS_SERVER_PORT_BASE"]
    host.args = lambda _port: [
        *[a for a in args(0) if a not in ("--connect", "127.0.0.1:0")],
        *(["--host-lobby", port] if lobby else []),
    ]
    rig.CLIENT_GIB = 10.0
    host.launch(0)
    stage(host.appdata / "Mods", ids)
    joiner = None
    log = lambda: host.log.read_text(errors="replace") if host.log.exists() else ""
    try:
        api = host.wait_api()
        api.load(str(world_dir), online_mode=online)
        local.wait(lambda: local.past_questions(api), 900, 2)
        rig.ensure_character(api)
        rig.wait_world(api, timeout=1200)
        rig.focus_gameplay(api)
        eye = center + side * 120000 + normal * HEIGHT
        api.character_teleport(*eye)
        time.sleep(SETTLE)
        text = log()
        built = len(re.findall(GRID_BUILD, text))
        packed = len(re.findall(GRID_PACKED, text))
        print(f"host: {packed} from the pack, {built} built", flush=True)
        check(
            "the host's grids come from the pack",
            packed > 0 and built == 0,
            f"{built} built",
        )
        if not lobby:
            return
        joiner = rig.Client(1, "Joiner", probe=False)
        jargs = joiner.args
        joiner.args = lambda _port: [
            *[a for a in jargs(0) if a not in ("--connect", "127.0.0.1:0")],
            "--join-lobby",
            f"127.0.0.1:{port}",
        ]
        for cache in joiner.mod_storage.glob("cache-*"):
            cache.unlink()
        joiner.launch(0)
        stage(joiner.appdata / "Mods", ids)
        japi = joiner.wait_api()
        rig.wait_world(japi, timeout=1200)
        rig.ensure_character(japi)
        rig.focus_gameplay(japi)
        japi.character_teleport(*eye)
        time.sleep(SETTLE)
        totals, _ = joiner.stats()
        print(f"joiner: {totals}", flush=True)
        check("the joiner shows impostors", int(totals.get("impostors", 0)) > 0)
        check(
            "the joiner gets no shape",
            int(totals.get("shapes", 0)) == 0,
            totals.get("shapes"),
        )
        check(
            "the joiner reads the pack",
            int(totals.get("packed", 0)) > 0,
            totals.get("packed"),
        )
    finally:
        if joiner:
            joiner.stop()
        host.stop()


def main() -> int:
    step = sys.argv[1] if len(sys.argv) > 1 else "measure"
    if step == "lobby" or step == "offline":
        local_run(step == "lobby")
    else:
        {"write": write, "again": again, "measure": measure}[step]()
    print(f"{failures} failures", flush=True)
    return failures


if __name__ == "__main__":
    sys.exit(main())
