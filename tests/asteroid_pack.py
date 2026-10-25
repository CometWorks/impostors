"""The asteroid pack of the "Octahedron - Seed 2" world (SE-701): writes it, then measures
what a client joining above the asteroid ring costs the server with and without it. Needs the
save (se1/worlds/octahedron/build_world.py --seed 2) and its Workshop mods downloaded, like
tests/ring.py.

    IMP_TASK=imp701 uv run --no-project --with httpx --with pillow --with numpy \
        python tests/asteroid_pack.py write      # the pack, into ~/.se-test/<task>-pack
    ... python tests/asteroid_pack.py measure    # a join without the pack, then with it
    ... python tests/asteroid_pack.py reset      # mine an asteroid, restart, reset it
    ... python tests/asteroid_pack.py offline    # offline with the client plugin, without
                                                 # the pack, with it, with AsteroidPackMod set
    IMP_PACK_LEVEL=32 ...                        # write: finest level in the pack (64)
    IMP_HEIGHT=10000 IMP_SETTLE=240 ...          # measure: meters above the ring, seconds
    IMP_RUNS=pack ...                            # measure: only the run with the pack

`reset` follows one asteroid the client sees: unedited it has the pack's version, cut it
has a new one, after a server restart (the known hashes saved with the world) it keeps that
without a build, and after a voxel reset it has the pack's version again without a build. A
PASS/FAIL line per step.

`offline` loads the world offline in a client with the client plugin, whose service serves
the local player, and compares the same numbers from the client's log and stats, without the
pack and with it (AsteroidPackMod 0, then the pack's id). The run with the pack saves the
world, and a load of that save must know the asteroids' hashes and read no voxels.

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
import struct
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


def add_pack(world: Path) -> None:
    """The pack mod in the world's mod list"""
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world / name
        text = f.read_text(encoding="utf-8").replace(
            "</Mods>",
            '<ModItem FriendlyName="Impostors Asteroid Pack">'
            f"<Name>{PACK_ID}.sbm</Name><PublishedFileId>{PACK_ID}</PublishedFileId>"
            "<PublishedServiceName>Steam</PublishedServiceName></ModItem>\n    </Mods>",
            1,
        )
        f.write_text(text, encoding="utf-8")


def setup_world(with_pack: bool) -> list[int]:
    """The save with its mods staged on the server, plus the pack mod when asked"""
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


def command(api, text: str, timeout: float = 20.0) -> str:
    """A chat command of the probe and its reply"""
    seen = {
        (m.get("text"), m.get("timestamp"))
        for m in api.get_chat_messages().get("hud") or []
    }
    api.send_chat(text)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for m in api.get_chat_messages().get("hud") or []:
            if (m.get("text"), m.get("timestamp")) not in seen and m.get("sender") in (
                "iprobe",
                "ImpostorsProbe",
            ):
                return m.get("text")
        time.sleep(0.5)
    raise TimeoutError(f"No reply to {text!r}")


def pack_versions() -> dict[int, int]:
    """Version per asteroid id from the pack's index"""
    data = (PACK / "ImpostorsAsteroidPack.bin").open("rb")
    magic, fmt = struct.unpack("<iB", data.read(5))
    data.read(data.read(1)[0])  # the world name, shorter than 128 bytes
    (count,) = struct.unpack("<i", data.read(4))
    versions = {}
    for _ in range(count):
        asteroid, _input, version, _flags, levels = struct.unpack(
            "<qqiBB", data.read(22)
        )
        data.read(5 * levels)
        versions[asteroid] = version
    return versions


def entry(api, asteroid: int) -> tuple[int | None, bool, float]:
    """The server's version of the asteroid's impostor, the game's ContentChanged and the
    size of the voxel map"""
    reply = command(api, f"!iprobe ventry {asteroid}")
    m = re.search(r"version=(-?\d+) changed=(\w+) size=([0-9.]+)", reply)
    return (
        (int(m.group(1)), m.group(2) == "True", float(m.group(3)))
        if m
        else (None, False, 0.0)
    )


def join(ids, eye, target):
    CLIENT.launch(SERVER.port)
    stage(CLIENT.appdata / "Mods", ids)
    api = CLIENT.wait_api()
    rig.wait_world(api, timeout=2400)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    api.character_teleport(*eye)
    time.sleep(5)
    api.character_look_at(*target, tolerance=2)
    return api


def check(ok: bool, text: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'} {text}", flush=True)
    return ok


def wait_for(probe, timeout: float):
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(5)


def reset() -> int:
    if not (PACK / "ImpostorsAsteroidPack.bin").exists():
        raise SystemExit(f"No pack in {PACK}, run write first")
    versions = pack_versions()
    ids = setup_world(True)
    (SERVER.world / "Storage" / "Impostors.asteroids").unlink(missing_ok=True)
    SERVER.write_plugin_config(
        rig.plugin_config(
            MaxDistance=500000,
            MaxImpostorsPerPlayer=4000,
            MaxQuadsPerFrame=16000,
            RebuildSeconds=10,
        )
    )
    center, normal, side, _ = ring.ring_geometry(SERVER.world)
    base = center + side * 120000
    eye = base + normal * (HEIGHT + 300)
    target = base + np.cross(normal, side) * (HEIGHT + 5000)
    builds = lambda asteroid, text: re.findall(
        rf"Impostor build: id={asteroid} kind=Voxel", text
    )
    ok = True
    SERVER.start(timeout=3600)
    for cache in CLIENT.mod_storage.glob("cache-*"):
        cache.unlink()
    try:
        api = join(ids, eye, target)
        time.sleep(120)
        _, impostors = CLIENT.stats()
        asteroid = max(
            (i for i, f in impostors.items() if i in versions),
            key=lambda i: int(impostors[i].get("lod", 0)),
        )
        version, changed, size = entry(api, asteroid)
        ok &= check(
            version == versions[asteroid]
            and not changed
            and not builds(asteroid, log_text()),
            f"unedited {asteroid}: version {version}, pack {versions[asteroid]}, changed={changed}",
        )
        reused = re.findall(r"from the asteroid pack.*took (\d+) ms CPU", log_text())
        print(
            f"  pack hits took {sum(map(int, reused))} ms CPU in the whole minutes so far",
            flush=True,
        )

        print(
            "server:",
            command(
                api, f"!iprobe vcut {asteroid} {size * 0.25:.0f} {size * 0.25:.0f}"
            ),
            flush=True,
        )
        mined = wait_for(
            lambda: (v := entry(api, asteroid))[0] not in (None, versions[asteroid])
            and v,
            120,
        )
        # The build's log line may come a moment after the new version
        wait_for(lambda: builds(asteroid, log_text()), 30)
        ok &= check(
            bool(mined) and mined[1] and len(builds(asteroid, log_text())) == 1,
            f"cut {asteroid}: version {mined and mined[0]}, built {len(builds(asteroid, log_text()))} times",
        )
        CLIENT.stop()
        SERVER.stop()

        SERVER.start(timeout=3600)
        api = join(ids, eye, target)
        # The impostors come in two at a time, this one may take a minute or two
        wait_for(lambda: entry(api, asteroid)[0], 240)
        text = log_text()
        known = re.search(r"Asteroid hashes: (\d+) known from the last save", text)
        version, changed, _ = entry(api, asteroid)
        ok &= check(
            bool(known)
            and mined
            and version == mined[0]
            and not builds(asteroid, text),
            f"after the restart: {known and known.group(1)} hashes loaded, {asteroid} version {version} changed={changed}, "
            f"built {len(builds(asteroid, text))} times, asteroid builds {len(re.findall(r'Impostor build: .*kind=Voxel', text))}",
        )
        reused = re.findall(r"took (\d+) ms CPU", text)
        print(
            f"  reused builds took {sum(map(int, reused))} ms CPU in the whole minutes so far",
            flush=True,
        )

        packed_before = int(CLIENT.stats()[0].get("packed", 0))
        print("server:", command(api, f"!iprobe vreset {asteroid}"), flush=True)
        back = wait_for(lambda: entry(api, asteroid)[0] == versions[asteroid], 120)
        time.sleep(5)
        totals, impostors = CLIENT.stats()
        text = log_text()
        ok &= check(
            back
            and not builds(asteroid, text)
            and int(impostors.get(asteroid, {}).get("version", 0))
            == versions[asteroid],
            f"reset {asteroid}: back to the pack's version {versions[asteroid]}: {bool(back)}, "
            f"built {len(builds(asteroid, text))} times, client version "
            f"{impostors.get(asteroid, {}).get('version')}, client pack shapes {packed_before} -> {totals.get('packed')}",
        )
    finally:
        CLIENT.stop()
        SERVER.stop()
    return 0 if ok else 1


def offline_run(
    tag: str,
    with_pack: bool,
    pack_mod: int = 0,
    again: bool = False,
    save: bool = False,
) -> dict:
    """The world offline in a client with the client plugin and no Impostors mod. With
    again the world saved by the last run with save, as it is."""
    import local

    client = rig.Client(2, "Viktor", plugin=True)
    world_dir = client.appdata / "Saves" / ring.WORLD_NAME
    ring.PLUGIN = True  # no Impostors mod in the world, the plugin draws
    if again:
        ids = [
            int(i)
            for i in re.findall(
                r"<PublishedFileId>(\d+)</PublishedFileId>",
                (world_dir / "Sandbox_config.sbc").read_text(encoding="utf-8"),
            )
        ]
    else:
        ids = ring.copy_world(world_dir)
        for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
            f = world_dir / name
            f.write_text(
                f.read_text(encoding="utf-8").replace(
                    "<OnlineMode>PUBLIC</OnlineMode>",
                    "<OnlineMode>OFFLINE</OnlineMode>",
                ),
                encoding="utf-8",
            )
        if with_pack:
            add_pack(world_dir)
            ids.append(PACK_ID)
    center, normal, side, _ = ring.ring_geometry(world_dir)
    client.appdata.mkdir(parents=True, exist_ok=True)
    (client.appdata / "Impostors.cfg").write_text(
        rig.plugin_config(AsteroidPack=True, AsteroidPackMod=pack_mod), encoding="utf-8"
    )
    shutil.rmtree(client.appdata / "ImpostorsCache", ignore_errors=True)
    args = client.args
    client.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    rig.CLIENT_GIB = 10.0
    client.launch(0)
    stage(client.appdata / "Mods", ids)
    result = {"tag": tag}
    log = lambda: client.log.read_text(errors="replace") if client.log.exists() else ""
    try:
        api = client.wait_api()
        start = time.monotonic()
        api.load(str(world_dir), online_mode="OFFLINE")
        local.wait(lambda: local.past_questions(api), 900, 2)
        rig.ensure_character(api)
        rig.wait_world(api, timeout=1200)
        rig.focus_gameplay(api)
        print(f"{tag} loaded in {time.monotonic() - start:.0f} s", flush=True)
        base = center + side * 120000
        eye = base + normal * (HEIGHT + 300)
        api.character_teleport(*eye)
        time.sleep(5)
        api.character_look_at(
            *(base + np.cross(normal, side) * (HEIGHT + 5000)), tolerance=2
        )
        start = time.monotonic()
        while time.monotonic() - start < SETTLE:
            time.sleep(10)
            totals, impostors = client.stats()
            shaped = sum(
                1 for i in impostors.values() if i.get("shape", "none") != "none"
            )
            print(
                f"{tag} t={time.monotonic() - start:.0f}s impostors={totals.get('impostors')} "
                f"shaped={shaped}",
                flush=True,
            )
        hashes = world_dir / "Storage" / "Impostors.asteroids"
        if save:
            api.save()
            local.wait(lambda: hashes.exists(), 120, 2)
            time.sleep(10)
            result["saved"] = (
                len(hashes.read_text().splitlines()) - 2 if hashes.exists() else None
            )
        text = log()
        known = re.search(r"Asteroid hashes: (\d+) known", text)
        result["known"] = int(known.group(1)) if known else 0
        cpus = [
            float(c)
            for c in re.findall(r"Impostor build: .*kind=Voxel .* cpu=([0-9.]+)", text)
        ]
        packed = re.findall(r"(\d+) from the asteroid pack", text)
        reused = re.findall(r"the reused ones took (\d+) ms CPU", text)
        pack_line = re.search(r"Asteroid pack of mod .*", text)
        result.update(
            impostors=int(totals.get("impostors", 0)),
            builds=len(cpus),
            build_cpu_s=sum(cpus) / 1000,
            packed=sum(map(int, packed)),
            reused_cpu_s=sum(map(int, reused)) / 1000,
            pack=pack_line.group(0) if pack_line else None,
        )
    finally:
        client.stop()
    print(
        f"{tag}: {result.get('impostors')} impostors, {result.get('builds')} asteroid builds "
        f"({result.get('build_cpu_s', 0):.1f} s CPU), {result.get('packed')} from the pack "
        f"(reused builds {result.get('reused_cpu_s', 0):.1f} s CPU, whole minutes), "
        f"{result.get('known')} hashes known at load, {result.get('saved')} saved; {result.get('pack')}",
        flush=True,
    )
    return result


def offline() -> int:
    if not (PACK / "ImpostorsAsteroidPack.bin").exists():
        raise SystemExit(f"No pack in {PACK}, run write first")
    runs = os.environ.get("IMP_RUNS", "nopack,pack,again,packid").split(",")
    results = {}
    if "nopack" in runs:
        results["nopack"] = offline_run("offline-nopack", False)
    if "pack" in runs:
        results["pack"] = offline_run("offline-pack", True, save="again" in runs)
    if "again" in runs:
        results["again"] = offline_run("offline-again", True, again=True)
    if "packid" in runs:
        results["packid"] = offline_run("offline-packid", True, PACK_ID)
    ok = all(
        r.get("builds") == 0 and r.get("packed", 0) > 0
        for k, r in results.items()
        if k != "nopack"
    )
    print(
        f"{'PASS' if ok else 'FAIL'} offline with the pack: no asteroid builds, pack hits",
        flush=True,
    )
    if "again" in results:
        again = results["again"]
        warm = again.get("known", 0) > 0 and again.get("reused_cpu_s", 1) < 1
        ok &= warm
        print(
            f"{'PASS' if warm else 'FAIL'} offline again after a save: {again.get('known')} "
            f"hashes known, the pack hits took {again.get('reused_cpu_s')} s CPU",
            flush=True,
        )
    return 0 if ok else 1


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
    modes = {"write": write, "measure": measure, "reset": reset, "offline": offline}
    if mode not in modes:
        raise SystemExit(__doc__)
    sys.exit(modes[mode]())
