"""In-game check of far view across a cluster, on the cluster's own scenario bench
(se1/cluster, Test/): a Gateway, the World Authority and two nodes, all running the
Impostors server plugin, and one headless client without it that draws impostors
through the Impostors mod.

The world is the cluster's Cluster-Simple with two static stations 12 km from the
spawn, which the world split puts on different nodes. The player's node has one of
them, the other comes through the WA's index. Then the far stations' partitions go
offline (no player within 8 km for 3 minutes), and the WA keeps showing them.

Then the WA restarts (SIGKILL, retained data root). It must load its saved index, so the
offline stations stay drawn, and its shape cache from disk: the player is teleported
next to a station, whose node loads its partition and takes the station's shapes from
the WA instead of building them (SE-648). IMP_RESTART=0 skips this part.

An asteroid 12 km out is on every node: both nodes build it at once and push it to the WA,
which must end up with it in its cache (SE-662). The stations are painted in a checkerboard,
so no two neighboring faces merge and their finest level is over the SDK's 64 KiB payload,
like the asteroid's: shapes cross the cluster in chunks (SE-704).

IMP_CACHE_MB sets CacheMegabytes on every process, 0 turns the caches off for a baseline;
the cache checks are skipped then. The end prints the first-sight latency and every
process's builds and cache hits with their CPU time.

Run it from the cluster repository's Test folder, with its host.env or the
environment naming the package, ports and roots, and CLUSTER_EXTRA_PLUGINS naming
the built server plugin (a folder Impostors/ with Impostors.dll and Impostors.xml):

    cd <cluster>/Test && uv run python <impostors>/tests/cluster_scenario.py

Prints one PASS or FAIL line per check, evidence goes to the bench's results root. The
client spawns inside the start station, so its screenshots don't show the far ones; the
mod's stats are what the checks read.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

TEST_DIR = Path.cwd()
sys.path.insert(0, str(TEST_DIR))

import hostenv  # noqa: E402

HOST = hostenv.resolve()
hostenv.activate(HOST)

from bench import Bench  # noqa: E402

IMPOSTORS = Path(__file__).resolve().parents[1]
MOD = IMPOSTORS / "ImpostorsMod" / "Mod"
MOD_ID = 900000199
CLIENT_ID = 1234567890
PROCESSES = ("wa-1", "node-1", "node-2")

# Static grids, each anchors a partition; consecutive ones go to alternating nodes
STATIONS = {
    "StationEast": (130000000000000101, (12000.0, 0.0, 0.0), 0.6),
    "StationNorth": (130000000000000102, (0.0, 0.0, -12000.0), 0.0),
}
# Hollow boxes of 9224 blocks, 9728 faces: one quad each at level 64, 97 KB
SIZE = (64, 16, 48)
PAYLOAD_LIMIT = 64 * 1024
ASTEROID = (130000000000000201, (-12256.0, -256.0, -256.0), "Asteroid512-001.vx2")
VOXEL_MAPS = Path(
    os.environ.get(
        "IMP_VOXEL_MAPS",
        Path.home()
        / ".steam/steam/steamapps/common/SpaceEngineersDedicatedServer/Content/VoxelMaps",
    )
)
CACHE_MB = os.environ.get("IMP_CACHE_MB")
# The WA's shape cache in its data folder, as rig.CACHE_FOLDER (this runs without rig.py)
CACHE_FOLDER = Path("Storage", "Impostors", "Cache")
CACHING = CACHE_MB != "0"
WORK = re.compile(
    r"Impostor (build|from the cache): id=(\d+) kind=(\w+).*? cpu=([\d.]+)"
)
# A shape level is a header and 10 bytes per quad (Protocol.Shape)
FINEST = re.compile(r" L64=[\d.]+/[\d.]+/(\d+)")
WALL = re.compile(r" wall=([\d.]+)")


def asteroid_xml() -> str:
    entity_id, (x, y, z), _ = ASTEROID
    return (
        '<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_VoxelMap">'
        f"<SubtypeName /><EntityId>{entity_id}</EntityId>"
        "<PersistentFlags>CastShadows InScene</PersistentFlags>"
        f'<PositionAndOrientation><Position x="{x}" y="{y}" z="{z}" />'
        '<Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
        "<StorageName>RockWest</StorageName><MutableStorage>true</MutableStorage>"
        "</MyObjectBuilder_EntityBase>"
    )


def station_xml(name: str, entity_id: int, position, hue: float) -> str:
    sx, sy, sz = SIZE
    hues = (hue, (hue + 0.5) % 1)
    blocks = "".join(
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_CubeBlock">'
        f'<SubtypeName>LargeBlockArmorBlock</SubtypeName><Min x="{x}" y="{y}" z="{z}" />'
        f'<ColorMaskHSV x="{hues[(x + y + z) % 2]}" y="-0.3" z="0.45" />'
        "</MyObjectBuilder_CubeBlock>"
        for x in range(sx)
        for y in range(sy)
        for z in range(sz)
        if x in (0, sx - 1) or y in (0, sy - 1) or z in (0, sz - 1)
    )
    x, y, z = position
    return (
        '<MyObjectBuilder_EntityBase xsi:type="MyObjectBuilder_CubeGrid">'
        f"<SubtypeName /><EntityId>{entity_id}</EntityId>"
        "<PersistentFlags>CastShadows InScene</PersistentFlags>"
        f'<PositionAndOrientation><Position x="{x}" y="{y}" z="{z}" />'
        '<Forward x="0" y="0" z="-1" /><Up x="0" y="1" z="0" /></PositionAndOrientation>'
        "<GridSizeEnum>Large</GridSizeEnum>"
        f"<CubeBlocks>{blocks}</CubeBlocks>"
        "<IsStatic>true</IsStatic><IsUnsupportedStation>true</IsUnsupportedStation>"
        f"<DisplayName>{name}</DisplayName><DestructibleBlocks>true</DestructibleBlocks>"
        "</MyObjectBuilder_EntityBase>"
    )


def template(root: Path) -> Path:
    """Cluster-Simple with the stations, the mod and a 15 km view distance"""
    target = root / "Cluster-Simple-impostors"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(HOST.package / "examples/Cluster-Simple", target)
    for cache in target.glob("*B5"):
        cache.unlink()

    sector = target / "SANDBOX_0_0_0_.sbs"
    text = sector.read_text()
    grids = "".join(station_xml(n, i, p, h) for n, (i, p, h) in STATIONS.items())
    grids += asteroid_xml()
    shutil.copy(VOXEL_MAPS / ASTEROID[2], target / "RockWest.vx2")
    text = text.replace("<SectorObjects>", "<SectorObjects>" + grids, 1)
    sector.write_text(text)

    mod = (
        f'<Mods><ModItem FriendlyName="Impostors"><Name>{MOD_ID}.sbm</Name>'
        f"<PublishedFileId>{MOD_ID}</PublishedFileId>"
        "<PublishedServiceName>Steam</PublishedServiceName></ModItem></Mods>"
    )
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        path = target / name
        text, n = re.subn(r"<Mods\s*/>", mod, path.read_text(), count=1)
        assert n == 1, f"{name}: no empty <Mods />"
        text = re.sub(
            r"<ViewDistance>\d+</ViewDistance>",
            "<ViewDistance>15000</ViewDistance>",
            text,
        )
        path.write_text(text)
    return target


def stage(bench: Bench) -> Path:
    """The mod in every process's and the client's Mods folder; the client's mod writes stats"""
    folders = [bench.run_root / f"{p}-data" / "Mods" for p in PROCESSES]
    user_data = bench.run_root / f"client-{CLIENT_ID}" / "home/.config/SpaceEngineers"
    folders.append(user_data / "Mods")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copytree(MOD, folder / f"{MOD_ID}.sbm", dirs_exist_ok=True)
    if CACHE_MB is not None:
        for p in PROCESSES:
            (bench.run_root / f"{p}-data" / "Impostors.cfg").write_text(
                '<?xml version="1.0" encoding="utf-8"?>\n<PluginConfig '
                'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
                'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
                f"<CacheMegabytes>{CACHE_MB}</CacheMegabytes></PluginConfig>\n"
            )
    storage = user_data / "Storage" / f"{MOD_ID}.sbm_Impostors"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "debug").write_text("")
    return storage / "stats.txt"


def stats(path: Path) -> dict[int, dict]:
    try:
        lines = path.read_text().splitlines()[1:]
    except FileNotFoundError:
        return {}
    rows = [dict(kv.split("=", 1) for kv in line.split()) for line in lines]
    return {int(r["id"]): r for r in rows}


def wait(predicate, timeout: float, interval: float = 3.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


def drawn(rows: dict[int, dict], entity_id: int) -> bool:
    """The client has the impostor with a shape; whether this frame draws it depends on
    the view, the renderer culls those outside the frustum"""
    row = rows.get(entity_id)
    return bool(row) and row["real"] == "0" and row["shape"] != "none"


def game_logs(bench: Bench) -> dict[Path, int]:
    """The nodes' game logs and their line counts"""
    return {
        log: len(log.read_text(errors="replace").splitlines())
        for p in PROCESSES
        if p != "wa-1"
        for log in (bench.run_root / f"{p}-data").glob("SpaceEngineersDedicated_*.log")
    }


def new_lines(bench: Bench, before: dict[Path, int], pattern: str) -> list[str]:
    """Lines of the nodes' game logs past the counts in before that match"""
    lines = []
    for log in game_logs(bench):
        rest = log.read_text(errors="replace").splitlines()[before.get(log, 0) :]
        lines += [l for l in rest if re.search(pattern, l)]
    return lines


def large_shape_checks(bench: Bench) -> None:
    """Shapes over one plugin message crossed the cluster: levels over the payload limit were
    built (by a node, or the WA for the asteroid), and the WA's cache has all of them"""
    folder = bench.run_root / "wa-1-data" / CACHE_FOLDER
    for name, entity_id in [
        *((n, i) for n, (i, _, _) in STATIONS.items()),
        ("asteroid", ASTEROID[0]),
    ]:
        built = [
            l
            for p in PROCESSES
            for log in (bench.run_root / f"{p}-data").glob(
                "SpaceEngineersDedicated_*.log"
            )
            for l in log.read_text(errors="replace").splitlines()
            if f"Impostor build: id={entity_id} " in l
        ]
        finest = max(
            (19 + 10 * int(m.group(1)) for l in built if (m := FINEST.search(l))),
            default=0,
        )
        bench.check(
            f"{name}: finest level over the payload limit",
            finest > PAYLOAD_LIMIT,
            f"{finest} bytes",
        )
        if not CACHING:
            continue
        cached = list(folder.rglob(f"{entity_id:016x}.shapes"))
        size = cached[0].stat().st_size if cached else 0
        bench.check(
            f"{name}: all its shapes in the WA's cache",
            size > finest > 0,
            f"{size} bytes",
        )


def restart_checks(bench: Bench, stats_file: Path, player_node: str, ids: dict) -> None:
    """The WA's saved index and shape cache across a WA restart"""
    time.sleep(70)  # The WA saves its index at most once a minute
    before = game_logs(bench)
    bench.cluster.restart_node("wa-1")
    loaded = bench.cluster.wait_node_log(
        "wa-1", r"cluster index: loaded \d+ saved records", 600
    )
    count = int(re.search(r"loaded (\d+)", loaded).group(1))
    bench.check("WA restart: saved index loaded", count >= len(ids), loaded.strip())

    time.sleep(60)  # Nodes reload the index, a client update
    rows = stats(stats_file)
    for name, entity_id in ids.items():
        bench.check(
            f"WA restart: impostor {name} still drawn",
            drawn(rows, entity_id),
            str(rows.get(entity_id, "missing")),
        )

    # Next to a station: its partition comes back on a node, which takes the shapes from the WA
    name, (entity_id, (x, y, z), _) = next(iter(STATIONS.items()))
    teleport = bench.cluster.node(
        player_node,
        f"/teleport?client={CLIENT_ID}&x={x}&y={y}&z={z + 300}",
        method="POST",
    )
    bench.record("teleport", teleport)

    def online():
        return bench.entity_owner(bench.diag("entities"), entity_id)

    owner = wait(online, 300, 5)
    bench.check(f"{name} back online after the teleport", bool(owner), str(owner))
    time.sleep(60)  # A scan, a build, and the minute's shape counts
    built = new_lines(bench, before, rf"Impostor build: id={entity_id} ")
    counts = new_lines(bench, before, r"Impostor shapes in the last minute")
    hits = new_lines(bench, before, rf"Impostor from the cache: id={entity_id} ")
    bench.record("restart-builds", {"built": built, "counts": counts, "hits": hits})
    for hit in hits:
        print(
            f"{name} from the WA's cache: {WALL.search(hit).group(1)} ms wall",
            flush=True,
        )
    if not CACHING:
        return
    bench.check(
        f"{name} not built again, its shapes came from the WA",
        not built,
        " | ".join(built[:3]),
    )
    bench.check(
        "nodes took shapes from the cache",
        any(int(re.search(r"(\d+) from the cache", l).group(1)) > 0 for l in counts),
        " | ".join(counts[-3:]),
    )


def run(bench: Bench, stats_file: Path) -> None:
    bench.cluster.start()
    bench.cluster.wait_ready()
    api = bench.start_client(CLIENT_ID)
    character = bench.wait_spawned(CLIENT_ID)
    spawned = time.monotonic()
    bench.ensure_gameplay_focus(CLIENT_ID)

    snapshot = bench.diag("entities")
    owners = {
        name: bench.entity_owner(snapshot, i) for name, (i, _, _) in STATIONS.items()
    }
    player_node = (bench.entity_owner(snapshot, character["entityId"]) or [("?", 0)])[
        0
    ][0]
    bench.record("owners", {"player": player_node, **{k: v for k, v in owners.items()}})
    print("player on", player_node, "stations:", owners, flush=True)

    remote = [n for n, o in owners.items() if o and o[0][0] != player_node]
    bench.check("a station on another node than the player", bool(remote), str(owners))

    ids = {name: i for name, (i, _, _) in STATIONS.items()}
    rows = wait(
        lambda: (r := stats(stats_file))
        and all(drawn(r, i) for i in [*ids.values(), ASTEROID[0]])
        and r,
        300,
        0.5,
    )
    first_sight = time.monotonic() - spawned
    bench.record("first-sight", {"seconds": first_sight})
    print(f"first sight of the far impostors: {first_sight:.1f} s after the spawn")
    rows = rows or stats(stats_file)
    bench.check(
        "asteroid impostor",
        drawn(rows, ASTEROID[0]),
        str(rows.get(ASTEROID[0], "missing")),
    )
    for name, entity_id in ids.items():
        bench.check(
            f"impostor {name}",
            drawn(rows, entity_id),
            str(rows.get(entity_id, "missing")),
        )
    for name, (entity_id, position, _) in STATIONS.items():
        api.character_look_at(*position, tolerance=1)
        time.sleep(2)
        bench.screenshot(CLIENT_ID, f"online-{name}")

    # Offline: no player within 8 km for 3 minutes takes the stations' partitions off their nodes
    def offline():
        snapshot = bench.diag("entities")
        return all(not bench.entity_owner(snapshot, i) for i in ids.values())

    gone = wait(offline, 600, 10)
    bench.check("the stations' partitions went offline", bool(gone))
    time.sleep(30)  # A report, a publication and a client update
    rows = stats(stats_file)
    for name, entity_id in ids.items():
        bench.check(
            f"impostor {name} while offline",
            drawn(rows, entity_id),
            str(rows.get(entity_id, "missing")),
        )
    for name, (entity_id, position, _) in STATIONS.items():
        api.character_look_at(*position, tolerance=1)
        time.sleep(2)
        bench.screenshot(CLIENT_ID, f"offline-{name}")

    large_shape_checks(bench)

    if os.environ.get("IMP_RESTART", "1") != "0":
        restart_checks(bench, stats_file, player_node, ids)


def work_summary(bench: Bench) -> None:
    """Builds and cache hits per process and kind over the whole run: count and CPU ms"""
    totals = {}
    for p in PROCESSES:
        for log in (bench.run_root / f"{p}-data").glob("SpaceEngineersDedicated_*.log"):
            for what, _, kind, cpu in WORK.findall(log.read_text(errors="replace")):
                n, total = totals.get((p, what, kind), (0, 0.0))
                totals[(p, what, kind)] = (n + 1, total + float(cpu))
    bench.record("work", {" ".join(k): v for k, v in totals.items()})
    for (p, what, kind), (n, cpu) in sorted(totals.items()):
        print(f"work: {p} {kind} {what}: {n}, {cpu:.1f} ms CPU")
    errors = []
    for p in PROCESSES:
        for log in (bench.run_root / f"{p}-data").glob("SpaceEngineersDedicated_*.log"):
            errors += [
                l
                for l in log.read_text(errors="replace").splitlines()
                if re.search(
                    r"Impostor cache: failed|Failed to build impostor|cache-get|IOException"
                    r"|the WA failed",
                    l,
                )
            ]
    bench.check(
        "no cache errors in the server logs", not errors, " | ".join(errors[:3])
    )


def main() -> None:
    results = HOST.results / time.strftime("impostors-%Y%m%d-%H%M%S")
    results.mkdir(parents=True, exist_ok=True)
    bench = Bench(
        "IMPOSTORS-CLUSTER",
        "Far view across a cluster",
        "1",
        results_root=results,
        world_template=template(results),
        administrators=[CLIENT_ID],
    )
    try:
        bench.cluster.kill()
        bench.cluster.prepare(wipe=True)
        stats_file = stage(bench)
        run(bench, stats_file)
        work_summary(bench)
    except Exception as error:  # noqa: BLE001 -- reported as a failed check
        bench.check("no error", False, f"{type(error).__name__}: {error}")
    finally:
        bench.close()
    failed = [c for c in bench.result.checks if not c.passed]
    print(
        f"{'PASSED' if not failed else 'FAILED'}: {len(failed)} failure(s), evidence in {bench.dir}"
    )
    sys.exit(len(failed))


if __name__ == "__main__":
    main()
