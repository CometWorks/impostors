"""In-game check of far view across a cluster, on the cluster's own scenario bench
(se1/cluster, Test/): a Gateway, the World Authority and two nodes, all running the
Impostors server plugin, and one headless client without it that draws impostors
through the Impostors mod.

The world is the cluster's Cluster-Simple with two static stations 12 km from the
spawn, which the world split puts on different nodes. The player's node has one of
them, the other comes through the WA's index. Then the far stations' partitions go
offline (no player within 8 km for 3 minutes), and the WA keeps showing them.

Run it from the cluster repository's Test folder, with its host.env or the
environment naming the package, ports and roots, and CLUSTER_EXTRA_PLUGINS naming
the built server plugin (a folder Impostors/ with Impostors.dll and Impostors.xml):

    cd <cluster>/Test && uv run python <impostors>/tests/cluster_scenario.py

Prints one PASS or FAIL line per check, evidence goes to the bench's results root. The
client spawns inside the start station, so its screenshots don't show the far ones; the
mod's stats are what the checks read.
"""

from __future__ import annotations

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
SIZE = (40, 12, 30)


def station_xml(name: str, entity_id: int, position, hue: float) -> str:
    sx, sy, sz = SIZE
    blocks = "".join(
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_CubeBlock">'
        f'<SubtypeName>LargeBlockArmorBlock</SubtypeName><Min x="{x}" y="{y}" z="{z}" />'
        f'<ColorMaskHSV x="{hue}" y="-0.3" z="0.45" /></MyObjectBuilder_CubeBlock>'
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
    row = rows.get(entity_id)
    return bool(row) and row["drawn"] == "1" and row["shape"] != "none"


def run(bench: Bench, stats_file: Path) -> None:
    bench.cluster.start()
    bench.cluster.wait_ready()
    api = bench.start_client(CLIENT_ID)
    character = bench.wait_spawned(CLIENT_ID)
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
        and all(drawn(r, i) for i in ids.values())
        and r,
        300,
    )
    rows = rows or stats(stats_file)
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
