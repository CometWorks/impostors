"""SE-1145: a grid removed from the game loses its impostor on every node in short order.

A node reports the grids it removed to the World Authority with its next report and the WA
publishes the change to every node with its next publication. Grids could also vanish without
such a report reaching the index. On octahedron-test (2026-11-06) a failed handover's target
reported the grids and released them with the discard, while the source kept them; when an
admin deleted them there later, the WA ignored the deletes, as it took a gone report only from
the node it had last heard the grid from. A crashed node's deleted grids stay the same way. Since
SE-1145 the WA applies a delete of a released record, and it reconciles: it asks the nodes
which of its grid records they hold, leaving out the grids of offline partitions, which the
gateway lists, and drops a record nobody has held for a while.

The cluster's scenario bench runs the WA and two nodes with the NPC sweep and the balancer off.
The plugin's cluster timers come from the environment (IMPOSTORS_CLUSTER_*_SECONDS): reports
every tick, a reconcile every second, orphans dropped after 3 s and the index saved every
second, so the test waits seconds; IMP_FAST=0 keeps the plugin's own timers. The WA's saved
index gets three orphan records before the start, grids of a partition that no longer exists,
like the 14 left on octahedron-test. Cluster-Simple, as tests/cluster_scenario.py makes it
without the stations, gets 27-block grids 40 km out: a yard of three sharing a partition
(Carrier, Drone, Freighter) and two alone that go offline.

1. The yard's handover to the other node fails on purpose, through the cluster test plugin's
   corrupt-import seam, and its hold-import seam keeps the failed import's grids on the target
   for HOLD seconds before the discard (SE-1167). Without the hold the target discards them
   about 0.1 s after they arrive, mostly before it has built the yard's impostors. With it the
   target builds and reports them, then releases them with the discard, as on octahedron-test,
   where a big partition's failed import held them for 37 s.
2. The source removes Carrier with the admin delete (/grid-delete) and Drone with a plain Close
   (/test-close-entity: what cut, grinding, cleanup, Hangar, the shipyard and a merge do).
3. The balancer resumes, as offline admission needs its partition reports, offline partitions
   go on with a 2 s grace and the two lone grids go offline; the test waits two orphan periods.

Checks: the target held the yard past a report before the discard, each removed grid leaves
the WA's saved index within 10 s plus its save interval, Carrier's delete was of the released
record, the reconcile drops the orphans, the WA's saved index lists none of those and still
lists Freighter and both offline grids. The times go to the output.

Run it from the Test folder of a cluster repository whose test plugin has the hold-import
seam (main after cluster 0.2.9), as tests/cluster_scenario.py:

    cd <cluster>/Test && uv run python <impostors>/tests/cluster_removal.py

Prints one PASS or FAIL line per check and the times; the evidence goes to the bench's
results root.
"""

from __future__ import annotations

import math
import os
import re
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cluster_delete as cd  # noqa: E402
import cluster_scenario as cs  # noqa: E402  (activates the bench's host environment)
from bench import Bench  # noqa: E402
from cluster import ClusterError  # noqa: E402
from scenarios import _all_partitions, _force_handover  # noqa: E402

PROCESSES = ("wa-1", "node-1", "node-2")
FAST = os.environ.get("IMP_FAST", "1") != "0"
TIMERS = {"REPORT": 0.01, "RECONCILE": 1, "ORPHAN": 3, "INDEX_SAVE": 1} if FAST else {}
REPORT = TIMERS.get("REPORT", 5)
ORPHAN_WAIT = 2 * (TIMERS.get("ORPHAN", 60) + TIMERS.get("RECONCILE", 15)) + 2 * REPORT
SAVE_WAIT = TIMERS.get("INDEX_SAVE", 60) + 2 * REPORT + 5

# The failed import's hold on the target: a report and time to build the yard. The gateway
# gives the whole import 10 s (DirectGateway._http.Timeout), hold included.
HOLD = REPORT + 2
Z = 40000.0
NAMES = ("Carrier", "Drone", "Freighter", "Offline-1", "Offline-2")
IDS = {n: 130000000000003001 + i for i, n in enumerate(NAMES)}
YARD = ("Carrier", "Drone", "Freighter")
OFFLINE = ("Offline-1", "Offline-2")
REMOVALS = {
    "Carrier": lambda: f"/grid-delete?entityId={IDS['Carrier']}&name=Carrier",
    "Drone": lambda: f"/test-close-entity?entity={IDS['Drone']}",
}
ORPHANS = (130000000000003901, 130000000000003902, 130000000000003903)
GONE_PARTITION = 987654321
STAMP = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)")


def position(name: str) -> tuple[float, float, float]:
    """The yard together at the top of the positive z half circle, the others alone on it, 20 km
    from the yard: on Cluster-Simple's bench the other half is not on node-1. A lone grid on
    the yard pushed Carrier off its record, so the source reported it again"""
    if name in YARD:
        return 60.0 * YARD.index(name), 0.0, Z
    angle = (NAMES.index(name) - 2) * math.pi / 3
    return Z * math.cos(angle), 0.0, Z * math.sin(angle)


def template(root: Path) -> Path:
    cs.STATIONS.clear()
    target = cs.template(root)
    sector = target / "SANDBOX_0_0_0_.sbs"
    added = "".join(cd.grid_xml(n, IDS[n], position(n)) for n in NAMES)
    sector.write_text(
        sector.read_text().replace("<SectorObjects>", "<SectorObjects>" + added, 1)
    )
    return target


def seed_orphans(bench: Bench, world: Path) -> None:
    """The WA's saved index with only the orphans, as ClusterBridge.SaveIndex writes it"""
    path = bench.run_root / "wa-1-data" / cs.CACHE_FOLDER / "index.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    save = str(bench.run_root / "wa-1-data" / "Saves" / world.name).encode()
    data = (
        struct.pack("<iH", 0x31584449, len(save))
        + save
        + struct.pack("<i", len(ORPHANS))
    )
    for k, orphan in enumerate(ORPHANS):
        x, z = 1000.0 * k, -Z
        shape = (orphan, 1, 1000 + k, x, 0, z, 0, 0, 0, 1, 0, 0, 0, x, 0, z, 4, 4, 4)
        data += struct.pack("<qBi3d4f3f3d3f", *shape)
        data += struct.pack("<q", GONE_PARTITION)
    path.write_bytes(data)


def log_lines(bench: Bench, process: str, pattern: str) -> list[tuple[float, str]]:
    """A process's matching log lines with their time as epoch seconds (the log writes local time)"""
    found = []
    for log in (bench.run_root / f"{process}-data").glob(
        "SpaceEngineersDedicated_*.log"
    ):
        for line in log.read_text(errors="replace").splitlines():
            if re.search(pattern, line) and (m := STAMP.match(line)):
                found.append((datetime.fromisoformat(m[1]).timestamp(), line))
    return found


def saved_ids(bench: Bench) -> tuple[set[int], float] | None:
    """The WA's saved index, None while it is missing or half written"""
    try:
        return cd.index_ids(bench)
    except struct.error:
        return None


def owner(bench: Bench, entity_id: int) -> tuple[str, int] | None:
    """The node and partition holding a grid"""
    for node in PROCESSES[1:]:
        for row in bench.cluster.node(node, "/partitions")["partitions"]:
            if any(m["id"] == entity_id for m in row.get("members") or []):
                return node, int(row["id"])
    return None


def offline_grids(bench: Bench) -> set[int]:
    table = bench.cluster.get("/registry/partitions/offline")
    return {
        m for row in table.get("Partitions") or [] for m in row.get("Members") or []
    }


def fail_handover(bench: Bench, source: str, target: str, partition: int) -> None:
    """A failed handover of the yard whose target holds the import HOLD seconds before the
    discard: reporting every tick, it reports the grids then, and releases them with it
    """
    carrier = rf"Impostor[^:]*: id={IDS['Carrier']} "
    discard = rf"partition={partition} phase=discard"
    bench.cluster.node(target, "/debug/corrupt-import?field=DisplayName", "POST")
    bench.cluster.node(target, f"/debug/hold-import?seconds={HOLD}", "POST")
    started = time.time()
    try:
        _force_handover(bench, source, target, partition)
    except ClusterError as error:  # an aborted handover answers with an error
        bench.record("handover", {"error": str(error)[:2000]})
    finally:
        hold = bench.cluster.node(target, "/debug/hold-import?seconds=0", "POST")
        bench.cluster.node(target, "/debug/corrupt-import?field=", "POST")
    stayed = cs.wait(lambda: _all_partitions(bench).get(partition) == source, 60, 0.5)
    bench.check("the yard's handover failed and it stayed on its source", bool(stayed))
    discarded = cs.wait(
        lambda: [t for t, _ in log_lines(bench, target, discard) if t >= started],
        30,
        0.5,
    )
    built = [t for t, _ in log_lines(bench, target, carrier) if t >= started]
    held = discarded[0] - built[0] if built and discarded else None
    bench.record("target", {"built": built, "discarded": discarded, "hold": hold})
    print(f"  [INFO] the target held the built yard {held} s before the discard")
    bench.check(
        "the target held the failed import and reported the yard before the discard",
        hold.get("held", 0) >= 1 and held is not None and held > REPORT + 1 / 30,
        f"held {held} s, {hold}",
    )
    time.sleep(2 * REPORT + 1)  # the target's released report


def run(bench: Bench) -> None:
    env = bench.cluster.config.node_env_by_id
    timers = {f"IMPOSTORS_CLUSTER_{k}_SECONDS": str(v) for k, v in TIMERS.items()}
    for process in PROCESSES:
        env[process] = {**env.get(process, {}), **timers}
    bench.cluster.start()
    bench.cluster.wait_ready()
    # Through the gateway, whose config document replaces a node's own. The NPC sweep would
    # delete these ownerless grids.
    bench.cluster.handover_config({"offline.enabled": "false", "npc.sweepSeconds": "0"})
    bench.cluster.post("/automatic/pause")  # the only partition move is the forced one

    reported = cs.wait(
        lambda: (r := saved_ids(bench)) and set(IDS.values()) <= r[0], 300, 0.5
    )
    bench.check("the WA's index has every grid", bool(reported))
    placed = {n: owner(bench, i) for n, i in IDS.items()}
    bench.record("placed", placed)
    bench.check(
        "the yard shares one partition",
        len({placed[n] for n in YARD}) == 1,
        str(placed),
    )
    source, partition = placed["Carrier"]
    target = next(p for p in PROCESSES[1:] if p != source)
    print(f"  [INFO] yard partition {partition} on {source}, handover to {target}")

    fail_handover(bench, source, target, partition)

    removed_at = {}
    for name, route in REMOVALS.items():
        removed_at[name] = time.time()
        bench.record(
            f"remove-{name}", bench.cluster.node(placed[name][0], route(), "POST")
        )
    # The WA deletes a record with the node's next report and saves its index at most
    # INDEX_SAVE later; the log line has the delete itself (since SE-1145)
    limit = 10 + TIMERS.get("INDEX_SAVE", 60)
    for name, at in removed_at.items():
        gone = cs.wait(
            lambda: (r := saved_ids(bench)) and IDS[name] not in r[0] and r,
            limit + 5,
            0.2,
        )
        saved = round(gone[1] - at, 2) if gone else None
        bench.record(f"saved-{name}", {"seconds": saved})
        bench.check(
            f"{name}: gone from the WA's saved index within {limit} s",
            saved is not None and saved < limit,
            f"{saved}",
        )
        print(f"  [INFO] {name}: {saved} s from the removal to the WA's saved index")

    loaded = log_lines(bench, "wa-1", r"cluster index: loaded \d+ saved records")
    dropped = cs.wait(
        lambda: log_lines(bench, "wa-1", r"cluster index: reconcile dropped"),
        ORPHAN_WAIT,
        0.5,
    )
    delay = dropped[0][0] - loaded[0][0] if loaded and dropped else None
    bench.record(
        "delay-orphans",
        {"seconds": delay, "loaded": loaded[:1], "dropped": dropped or []},
    )
    bench.check("the reconcile dropped the orphans", delay is not None, f"{delay}")
    if delay is not None:
        print(
            f"  [INFO] orphans: {delay:.2f} s from the WA loading its index to the reconcile"
        )

    bench.cluster.post("/automatic/resume")
    bench.cluster.handover_config(
        {
            "offline.enabled": "true",
            "offline.compensate": "false",
            "offline.graceSeconds": "2",
            "offline.minOnlineSeconds": "2",
        }
    )
    went = cs.wait(lambda: {IDS[n] for n in OFFLINE} <= offline_grids(bench), 120, 0.5)
    bench.check("the lone grids' partitions went offline", bool(went))
    # A reconcile that wrongly took them for orphans has dropped them by then
    time.sleep(ORPHAN_WAIT)

    gone = {IDS[n] for n in REMOVALS} | set(ORPHANS)
    kept = {IDS[n] for n in ("Freighter", *OFFLINE)}
    saved = cs.wait(
        lambda: (r := saved_ids(bench)) and not gone & r[0] and kept <= r[0] and r,
        SAVE_WAIT,
        0.5,
    ) or saved_ids(bench)
    listed = saved[0] if saved else set()
    for name in REMOVALS:
        bench.check(
            f"{name} removed: not in the WA's saved index", IDS[name] not in listed
        )
    for orphan in ORPHANS:
        bench.check(
            f"orphan {orphan}: not in the WA's saved index", orphan not in listed
        )
    for name in ("Freighter", *OFFLINE):
        bench.check(f"{name} kept: in the WA's saved index", IDS[name] in listed)

    released = log_lines(bench, "wa-1", rf"deleted {IDS['Carrier']}, a released record")
    bench.check(
        "Carrier's delete was of the failed handover's released record", bool(released)
    )
    # The WA's own delete, from its log, which it writes late (since SE-1145)
    for name, at in removed_at.items():
        line = log_lines(bench, "wa-1", rf"cluster index: \S+ deleted {IDS[name]}\b")
        deleted = f"{line[0][0] - at:.2f} s" if line else "no log line"
        print(f"  [INFO] {name}: {deleted} from the removal to the WA's delete")


def main() -> None:
    results = cs.HOST.results / time.strftime("impostors-removal-%Y%m%d-%H%M%S")
    results.mkdir(parents=True, exist_ok=True)
    world = template(results)
    bench = Bench(
        "IMPOSTORS-CLUSTER-REMOVAL",
        "Removed grids and orphan records lose their impostors across a cluster",
        "1",
        results_root=results,
        regular_nodes=2,
        world_template=world,
    )
    started = time.time()
    try:
        bench.cluster.kill()
        bench.cluster.prepare(wipe=True)
        cs.PROCESSES = PROCESSES
        cs.stage(bench)
        seed_orphans(bench, world)
        run(bench)
    except Exception as error:  # noqa: BLE001 -- reported as a failed check
        bench.check("no error", False, f"{type(error).__name__}: {error}")
    finally:
        bench.close()
    failed = [c for c in bench.result.checks if not c.passed]
    print(f"  [INFO] {time.time() - started:.0f} s in all")
    print(
        f"{'PASSED' if not failed else 'FAILED'}: {len(failed)} failure(s), evidence in {bench.dir}"
    )
    sys.exit(len(failed))


if __name__ == "__main__":
    main()
