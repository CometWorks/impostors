"""SE-1412: a ship shot into pieces doesn't build every piece at once, a group keeps its key
when its largest grid changes, and the WA drops the shapes of the deleted pieces.

The cluster's scenario bench runs the WA and one node, with offline partitions and the NPC
sweep turned off. Cluster-Simple, as tests/cluster_scenario.py makes it without the stations,
gets two grids 40 km out:

- Chain: PIECES armor cubes of 27 blocks in a row, joined by single conveyor blocks. The
  node removes the conveyors (the cluster test plugin's /test-remove-block), which splits
  the chain into PIECES grids at once, PIECES - 1 of them new. With NewGridBuildsPerMinute
  at BUDGET their first builds are paced: no minute of the node's log has more than BUDGET
  of them plus the one the pacing starts with. Every piece still reaches the WA's index.
  Then the node deletes every piece, and the WA's shape count (its sweep line) falls
  below what it was before the split.
- Rotor: an armor cube with a rotor base and a tail of TAIL conveyors (grid A, the
  largest), and the rotor's head with a slab of armor (grid B). Both are one impostor keyed
  by A. The node removes the tail, which leaves B the largest grid of the group, but A is
  still more than half its size: the group keeps the key A, the WA's index has A and not B.

Run it from the cluster repository's Test folder, as tests/cluster_scenario.py:

    cd <cluster>/Test && uv run python <impostors>/tests/cluster_split.py

Prints one PASS or FAIL line per check; the evidence goes to the bench's results root.
"""

from __future__ import annotations

import re
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_scenario as cs  # noqa: E402  (activates the bench's host environment)
import cluster_delete as cd  # noqa: E402
from bench import Bench  # noqa: E402

NODE = "node-1"
PROCESSES = ("wa-1", NODE)
PIECES = 20
BUDGET = 6
TAIL = 30
CHAIN = 130000000000002000
CHAIN_POS = (0.0, 0.0, 40000.0)
ROTOR_A = 130000000000003000
ROTOR_B = 130000000000003001
ROTOR_POS = (8000.0, 0.0, 39000.0)
# Fat block ids: the chain's links, the rotor's tail, base and head
LINK = 130000000000004000
TAIL_ID = 130000000000005000
STATOR = 130000000000003100
ROTOR_TOP = 130000000000003101
TIMERS = {
    "IMPOSTORS_CLUSTER_SHAPE_SWEEP_SECONDS": "10",
    "IMPOSTORS_CLUSTER_INDEX_SAVE_SECONDS": "5",
}
CONFIG = (
    '<?xml version="1.0" encoding="utf-8"?>\n<PluginConfig '
    'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
    'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
    "<RebuildSeconds>10</RebuildSeconds>"
    f"<NewGridBuildsPerMinute>{BUDGET}</NewGridBuildsPerMinute></PluginConfig>\n"
)
BUILD = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+).*Impostor build: id=(-?\d+) kind=Grid"
)
SWEEP = re.compile(
    r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+).*cluster shapes: (\d+) in memory"
)


def armor(x: int, y: int, z: int) -> str:
    return (
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_CubeBlock">'
        f'<SubtypeName>LargeBlockArmorBlock</SubtypeName><Min x="{x}" y="{y}" z="{z}" />'
        '<ColorMaskHSV x="0.6" y="-0.3" z="0.45" />'
        "</MyObjectBuilder_CubeBlock>"
    )


def conveyor(entity_id: int, x: int, y: int, z: int) -> str:
    return (
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_Conveyor">'
        f"<SubtypeName>LargeBlockConveyor</SubtypeName><EntityId>{entity_id}</EntityId>"
        f'<Min x="{x}" y="{y}" z="{z}" /></MyObjectBuilder_CubeBlock>'
    )


def cube(dx: int) -> str:
    return "".join(
        armor(dx + x, y, z) for x in range(3) for y in range(3) for z in range(3)
    )


def grid(name: str, entity_id: int, position, blocks: str) -> str:
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


def chain_xml() -> str:
    """Cubes at x = 4k .. 4k+2, a conveyor at x = 4k+3 to the next"""
    blocks = "".join(cube(4 * k) for k in range(PIECES))
    blocks += "".join(conveyor(LINK + k, 4 * k + 3, 1, 1) for k in range(PIECES - 1))
    return grid("Chain", CHAIN, CHAIN_POS, blocks)


def rotor_xml() -> str:
    """A: a cube, the rotor base on top, the tail along x; B: the head and a 5 x 8 slab"""
    a = cube(0)
    a += (
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_MotorAdvancedStator">'
        f"<SubtypeName>LargeAdvancedStator</SubtypeName><EntityId>{STATOR}</EntityId>"
        '<Min x="1" y="3" z="1" />'
        f"<TopBlockId>{ROTOR_TOP}</TopBlockId><RotorEntityId>{ROTOR_TOP}</RotorEntityId>"
        "<TargetVelocity>0</TargetVelocity>"
        "</MyObjectBuilder_CubeBlock>"
    )
    a += "".join(conveyor(TAIL_ID + k, 3 + k, 1, 1) for k in range(TAIL))
    b = (
        '<MyObjectBuilder_CubeBlock xsi:type="MyObjectBuilder_MotorAdvancedRotor">'
        f"<SubtypeName>LargeAdvancedRotor</SubtypeName><EntityId>{ROTOR_TOP}</EntityId>"
        '<Min x="0" y="0" z="0" />'
        "</MyObjectBuilder_CubeBlock>"
    )
    b += "".join(armor(x, 1, z) for x in range(-2, 3) for z in range(-3, 5))
    x, y, z = ROTOR_POS
    return grid("RotorA", ROTOR_A, ROTOR_POS, a) + grid(
        "RotorB", ROTOR_B, (x + 2.5, y + 10.0, z + 2.5), b
    )


def template(root: Path) -> Path:
    cs.STATIONS.clear()
    target = cs.template(root)
    sector = target / "SANDBOX_0_0_0_.sbs"
    sector.write_text(
        sector.read_text().replace(
            "<SectorObjects>", "<SectorObjects>" + chain_xml() + rotor_xml(), 1
        )
    )
    return target


def remove_block(bench: Bench, entity_id: int) -> dict:
    return bench.cluster.node(NODE, f"/test-remove-block?entity={entity_id}", "POST")


def delete(bench: Bench, entity_id: int) -> dict:
    query = urllib.parse.urlencode({"entityId": entity_id, "name": str(entity_id)})
    return bench.cluster.node(NODE, f"/grid-delete?{query}", "POST")


def grids_on_node(bench: Bench) -> set[int]:
    rows = bench.cluster.node(NODE, "/partitions")["partitions"]
    return {int(m["id"]) for row in rows for m in row.get("members") or []}


def epoch(stamp: str) -> float:
    """A game log time (local) as epoch seconds"""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f").timestamp()


def log_lines(bench: Bench, process: str, pattern: re.Pattern) -> list[re.Match]:
    found = []
    for log in (bench.run_root / f"{process}-data").glob(
        "SpaceEngineersDedicated_*.log"
    ):
        for line in log.read_text(errors="replace").splitlines():
            if m := pattern.match(line):
                found.append(m)
    return found


def builds(bench: Bench, ids: set[int], since: float) -> list[float]:
    return sorted(
        t
        for m in log_lines(bench, NODE, BUILD)
        if int(m[2]) in ids and (t := epoch(m[1])) >= since
    )


def shape_counts(bench: Bench) -> list[tuple[float, int]]:
    return sorted((epoch(m[1]), int(m[2])) for m in log_lines(bench, "wa-1", SWEEP))


def run(bench: Bench) -> None:
    env = bench.cluster.config.node_env_by_id
    for process in PROCESSES:
        env[process] = {**env.get(process, {}), **TIMERS}
        (bench.run_root / f"{process}-data" / "Impostors.cfg").write_text(CONFIG)
    bench.cluster.start()
    bench.cluster.wait_ready()
    bench.cluster.handover_config({"offline.enabled": "false", "npc.sweepSeconds": "0"})

    reported = cs.wait(
        lambda: (r := cd.index_ids(bench)) and {CHAIN, ROTOR_A} <= r[0] and r, 300, 5
    )
    bench.check("the WA's index has the chain and the rotor group", bool(reported))
    listed = reported[0] if reported else set()
    bench.check(
        "the rotor's head is in the group of A, no entry of its own",
        ROTOR_B not in listed,
    )

    counted = cs.wait(lambda: shape_counts(bench), 120, 5)
    bench.check("the WA logs its shape count", bool(counted))
    before = counted[-1][1] if counted else 0

    # The rotor: the tail goes, farthest block first, so A never splits
    for k in reversed(range(TAIL)):
        bench.record(f"remove-tail-{k}", remove_block(bench, TAIL_ID + k))
    tail_gone = time.time()

    # The chain: every link goes, the pieces come out at once
    known = grids_on_node(bench)
    split_at = time.time()
    for k in range(PIECES - 1):
        bench.record(f"remove-link-{k}", remove_block(bench, LINK + k))
    pieces = cs.wait(
        lambda: (new := grids_on_node(bench) - known)
        and len(new) >= PIECES - 1
        and new,
        60,
        2,
    )
    bench.record("pieces", sorted(pieces or []))
    bench.check(f"the chain split into {PIECES} grids", bool(pieces))
    pieces = set(pieces or [])

    # Paced: about one build every 60 / BUDGET seconds after the first
    all_listed = cs.wait(
        lambda: (r := cd.index_ids(bench)) and pieces <= r[0] and r,
        (PIECES / BUDGET + 3) * 60,
        5,
    )
    bench.check("every piece reached the WA's index", bool(all_listed))
    times = builds(bench, pieces, split_at - 5)
    worst = max((sum(1 for u in times if t <= u < t + 60) for t in times), default=0)
    bench.record("piece-builds", [round(t - split_at, 1) for t in times])
    bench.check(
        f"new pieces built per minute within the budget ({BUDGET} + 1)",
        0 < worst <= BUDGET + 1,
        f"{len(times)} builds, at most {worst} in a minute",
    )

    saved = cs.wait(
        lambda: (r := cd.index_ids(bench)) and r[1] > tail_gone + 15 and r, 60, 5
    )
    listed = saved[0] if saved else set()
    bench.check("the rotor group kept its key A", ROTOR_A in listed)
    bench.check("the rotor's head got no entry of its own", ROTOR_B not in listed)

    peak = max((c for _, c in shape_counts(bench)), default=0)
    for entity_id in sorted(pieces | {CHAIN}):
        bench.record(f"delete-{entity_id}", delete(bench, entity_id))
    deleted_at = time.time()
    after = cs.wait(
        lambda: [
            c for t, c in shape_counts(bench) if t > deleted_at + 15 and c < before
        ],
        120,
        5,
    )
    counts = shape_counts(bench)
    bench.record("shape-counts", counts)
    bench.check(
        "the WA's shape count fell below the one before the split after the deletes",
        bool(after),
        f"before the split {before}, peak {peak}, last {counts[-1][1] if counts else None}",
    )


def main() -> None:
    results = cs.HOST.results / time.strftime("impostors-split-%Y%m%d-%H%M%S")
    results.mkdir(parents=True, exist_ok=True)
    bench = Bench(
        "IMPOSTORS-CLUSTER-SPLIT",
        "A ship shot into pieces is built at a paced rate, the WA drops the deleted pieces",
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
