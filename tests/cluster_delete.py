"""SE-1090: a grid deleted on a cluster node loses its impostor, for every player.

Every 5 s a node reports its grids to the World Authority. A grid that went away since is
either released (it lives on in an offline partition or on another node, so the WA keeps its
record) or deleted (the WA drops it). Before SE-1090 the node told them apart at the report, by
whether it still owned the grid's last partition. A delete that emptied the partition (the
cluster releases it a frame or two later) or was followed by a split or merge (new partition
ids) passed for a release: the WA kept the record as offline, and every node showed the grid's
impostor for good.

The cluster's scenario bench runs the WA and one node, with offline partitions and the NPC
sweep turned off. Cluster-Simple, as tests/cluster_scenario.py makes it without the stations,
gets 27-block grids 40 km out on a half circle: four alone, each in its own partition, and four
groups of three, 60 m apart. The node deletes each lone grid (/grid-delete, which also releases
the emptied partition) and the first grid of each group, then moves the group's second grid
30 km on, which splits the group's partition. The deletes are 1.3 s apart, so they fall at
different points of the report cycle. Checks: the WA's saved index, which every node's far view
reads for the grids it doesn't host, lists none of the deleted grids and all of the others.

Run it from the cluster repository's Test folder, as tests/cluster_scenario.py:

    cd <cluster>/Test && uv run python <impostors>/tests/cluster_delete.py

Prints one PASS or FAIL line per check; the evidence goes to the bench's results root.
"""

from __future__ import annotations

import math
import struct
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_scenario as cs  # noqa: E402  (activates the bench's host environment)
from bench import Bench  # noqa: E402

NODE = "node-1"
PROCESSES = ("wa-1", NODE)
DISTANCE = 40000.0
SPACING = 60.0
MOVE = 30000.0
STAGGER = 1.3
RECORD = (
    101 + 8
)  # ClusterRecord.Size and the partition, as ClusterBridge.SaveIndex writes them


def grids() -> dict[str, tuple[int, tuple[float, float, float]]]:
    """name: (entity id, position), alternating lone grids and groups on a half circle on the
    positive z side: on Cluster-Simple's bench the negative z half was not on the node
    """
    result = {}
    for i in range(8):
        angle = i * math.pi / 7
        x, z = DISTANCE * math.cos(angle), DISTANCE * math.sin(angle)
        n = i // 2 + 1
        if i % 2 == 0:
            result[f"Lone-{n}"] = (130000000000001000 + i * 10, (x, 0.0, z))
        else:
            for j, member in enumerate("ABC"):
                result[f"Group-{n}-{member}"] = (
                    130000000000001000 + i * 10 + j,
                    (x, j * SPACING, z),
                )
    return result


GRIDS = grids()
IDS = {n: i for n, (i, _) in GRIDS.items()}


def grid_xml(name: str, entity_id: int, position) -> str:
    blocks = "".join(
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_CubeBlock">'
        f'<SubtypeName>LargeBlockArmorBlock</SubtypeName><Min x="{x}" y="{y}" z="{z}" />'
        '<ColorMaskHSV x="0.6" y="-0.3" z="0.45" />'
        "</MyObjectBuilder_CubeBlock>"
        for x in range(3)
        for y in range(3)
        for z in range(3)
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
        f"<DisplayName>{name}</DisplayName><DestructibleBlocks>true</DestructibleBlocks>"
        "</MyObjectBuilder_EntityBase>"
    )


def template(root: Path) -> Path:
    """cluster_scenario's world with these grids instead of its stations"""
    cs.STATIONS.clear()
    target = cs.template(root)
    sector = target / "SANDBOX_0_0_0_.sbs"
    added = "".join(grid_xml(n, i, p) for n, (i, p) in GRIDS.items())
    sector.write_text(
        sector.read_text().replace("<SectorObjects>", "<SectorObjects>" + added, 1)
    )
    return target


def index_ids(bench: Bench) -> tuple[set[int], float] | None:
    """The ids in the WA's saved index and the file's write time"""
    path = bench.run_root / "wa-1-data" / cs.CACHE_FOLDER / "index.bin"
    try:
        data = path.read_bytes()
        written = path.stat().st_mtime
    except FileNotFoundError:
        return None
    offset = 4
    offset += 2 + struct.unpack_from("<H", data, offset)[0]
    count = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    ids = {struct.unpack_from("<q", data, offset + k * RECORD)[0] for k in range(count)}
    return ids, written


def delete(bench: Bench, name: str) -> dict:
    query = urllib.parse.urlencode({"entityId": IDS[name], "name": name})
    return bench.cluster.node(NODE, f"/grid-delete?{query}", "POST")


def move(bench: Bench, name: str) -> dict:
    entity_id, (x, y, z) = GRIDS[name]
    scale = (DISTANCE + MOVE) / DISTANCE
    query = urllib.parse.urlencode(
        {"entityId": entity_id, "x": x * scale, "y": y, "z": z * scale}
    )
    return bench.cluster.node(NODE, f"/grid-move?{query}", "POST")


def partitions(bench: Bench) -> dict[int, int]:
    """grid id: the node's partition holding it"""
    rows = bench.cluster.node(NODE, "/partitions")["partitions"]
    return {m["id"]: int(row["id"]) for row in rows for m in row.get("members") or []}


def run(bench: Bench) -> None:
    bench.cluster.start()
    bench.cluster.wait_ready()
    # Through the gateway, whose config document replaces a node's own. The NPC sweep would
    # delete these ownerless grids after 5 minutes.
    bench.cluster.handover_config({"offline.enabled": "false", "npc.sweepSeconds": "0"})

    reported = cs.wait(
        lambda: (r := index_ids(bench)) and set(IDS.values()) <= r[0], 300, 5
    )
    bench.check("the WA's index has every grid", bool(reported))

    before = partitions(bench)
    deleted = []
    for n in range(1, 5):
        deleted.append(f"Lone-{n}")
        bench.record(f"delete-Lone-{n}", delete(bench, f"Lone-{n}"))
        time.sleep(STAGGER)
        deleted.append(f"Group-{n}-A")
        bench.record(f"delete-Group-{n}-A", delete(bench, f"Group-{n}-A"))
        bench.record(f"move-Group-{n}-B", move(bench, f"Group-{n}-B"))
        time.sleep(STAGGER)
    last = time.time()
    kept = [n for n in GRIDS if n not in deleted]

    def split():
        after = partitions(bench)
        renumbered = all(
            after.get(IDS[f"Group-{n}-C"]) != before.get(IDS[f"Group-{n}-A"])
            for n in range(1, 5)
        )
        return renumbered and after

    after = cs.wait(split, 120, 2)
    bench.record("partitions", {"before": before, "after": after or partitions(bench)})
    bench.check("every group's partition was split or renumbered", bool(after))

    # Reports every 5 s, the WA saves its index at most once a minute
    saved = cs.wait(lambda: (r := index_ids(bench)) and r[1] > last + 20 and r, 150, 5)
    bench.check("the WA saved its index after the deletes", bool(saved))
    listed = saved[0] if saved else set()
    for name in deleted:
        bench.check(f"{name} deleted: not in the WA's index", IDS[name] not in listed)
    for name in kept:
        bench.check(f"{name} kept: in the WA's index", IDS[name] in listed)


def main() -> None:
    results = cs.HOST.results / time.strftime("impostors-delete-%Y%m%d-%H%M%S")
    results.mkdir(parents=True, exist_ok=True)
    bench = Bench(
        "IMPOSTORS-CLUSTER-DELETE",
        "Deleted grids lose their impostors across a cluster",
        "1",
        results_root=results,
        regular_nodes=1,
        world_template=template(results),
    )
    try:
        bench.cluster.kill()
        bench.cluster.prepare(wipe=True)
        cs.PROCESSES = PROCESSES
        cs.stage(bench)
        run(bench)
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
