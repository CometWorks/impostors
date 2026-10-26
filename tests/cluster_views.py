"""The views of tests/level_views.py on a cluster (SE-722): "Octahedron - Seed 2" on the
cluster's scenario bench (se1/cluster, Test/), a Gateway, the World Authority and two nodes
running the Impostors server plugin, and one headless client without it that draws
impostors through the Impostors mod. Times how long the impostors in each view take to show
and settle, and which levels they are drawn at by distance.

The world is a copy of the save with only two of its mods: the ore mod (OctahedronOres-Seed2,
the materials of the asteroids) and Impostors, both under stand-in Workshop ids and staged
into every process's Mods folder. The rest (NPCs, blocks of the stations) don't take part in
the asteroids' impostors. The Sun's rotation is off and the view distance 2 km, as in
tests/level_views.py's impostor run.

Run it from the cluster repository's Test folder, as tests/cluster_scenario.py:

    cd <cluster>/Test && IMP_SEED2=<save folder> IMP_ORE_MOD=<OctahedronOres-Seed2 folder> \\
        uv run --with numpy python <impostors>/tests/cluster_views.py

The client has no camera probe: it teleports its character through the player's node to
each view's point and looks at the target, so the camera's roll differs from the other
setups. Prints the tables and writes them with the screenshots into the bench's evidence
folder. IMP_SETTLE (120) is the most seconds per view.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np

TEST_DIR = Path.cwd()
sys.path.insert(0, str(TEST_DIR))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import hostenv  # noqa: E402

HOST = hostenv.resolve()
hostenv.activate(HOST)

from bench import Bench  # noqa: E402
from view_levels import level_table, timeline, timing_table  # noqa: E402

IMPOSTORS = Path(__file__).resolve().parents[1]
MOD = IMPOSTORS / "ImpostorsMod" / "Mod"
MOD_ID = 900000199
ORE_MOD_ID = 900000202
SEED2 = Path(os.environ["IMP_SEED2"])
ORE_MOD = Path(os.environ["IMP_ORE_MOD"])
CLIENT_ID = 1234567890
PROCESSES = ("wa-1", "node-1", "node-2")
SETTLE = float(os.environ.get("IMP_SETTLE", "120"))
HEIGHT = 2000.0


def template(root: Path) -> Path:
    target = root / "Octahedron-Seed2-impostors"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(SEED2, target, ignore=shutil.ignore_patterns("Backup", "*B5"))
    mods = (
        "<Mods>"
        f'<ModItem FriendlyName="OctahedronOres-Seed2"><Name>{ORE_MOD_ID}.sbm</Name>'
        f"<PublishedFileId>{ORE_MOD_ID}</PublishedFileId>"
        "<PublishedServiceName>Steam</PublishedServiceName></ModItem>"
        f'<ModItem FriendlyName="Impostors"><Name>{MOD_ID}.sbm</Name>'
        f"<PublishedFileId>{MOD_ID}</PublishedFileId>"
        "<PublishedServiceName>Steam</PublishedServiceName></ModItem>"
        "</Mods>"
    )
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        path = target / name
        text = path.read_text(encoding="utf-8")
        text, n = re.subn(r"<Mods>.*?</Mods>", mods, text, count=1, flags=re.S)
        assert n == 1, f"{name}: no <Mods>"
        text = re.sub(
            r"<EnableSunRotation>\w+</EnableSunRotation>",
            "<EnableSunRotation>false</EnableSunRotation>",
            text,
        )
        text = re.sub(
            r"<ViewDistance>\d+</ViewDistance>",
            "<ViewDistance>2000</ViewDistance>",
            text,
        )
        text = text.replace(
            "<OnlineMode>OFFLINE</OnlineMode>", "<OnlineMode>PUBLIC</OnlineMode>"
        )
        path.write_text(text, encoding="utf-8")
    return target


def geometry(world: Path):
    """The views of tests/level_views.py: (name, eye, target)"""
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
    side /= np.linalg.norm(side)
    zone = re.search(
        r"<Position x=\"([^\"]+)\" y=\"([^\"]+)\" z=\"([^\"]+)\"(?:(?!</MyObjectBuilder_EntityBase>).)*?"
        r"<DisplayName>Gamma Station</DisplayName>",
        text,
        re.S,
    )
    gamma = np.array([float(v) for v in zone.groups()]) if zone else None
    if gamma is None:
        raise SystemExit("No Gamma Station in the world")
    inward = (center - gamma) / np.linalg.norm(center - gamma)
    tangent = np.cross(normal, side)
    base = center + side * 121000
    eye = base + normal * HEIGHT
    return [
        ("gamma", gamma + inward * 600, center),
        ("along-a", eye, base + tangent * 20000),
        ("along-b", eye, base - tangent * 20000),
    ]


def stage(bench: Bench) -> Path:
    """Both mods in every process's and the client's Mods folder; the client's mod writes stats"""
    folders = [bench.run_root / f"{p}-data" / "Mods" for p in PROCESSES]
    user_data = bench.run_root / f"client-{CLIENT_ID}" / "home/.config/SpaceEngineers"
    folders.append(user_data / "Mods")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copytree(MOD, folder / f"{MOD_ID}.sbm", dirs_exist_ok=True)
        shutil.copytree(ORE_MOD, folder / f"{ORE_MOD_ID}.sbm", dirs_exist_ok=True)
    for p in PROCESSES:
        (bench.run_root / f"{p}-data" / "Impostors.cfg").write_text(
            '<?xml version="1.0" encoding="utf-8"?>\n<PluginConfig '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema">'
            "<MaxDistance>300000</MaxDistance><MaxImpostorsPerPlayer>4000</MaxImpostorsPerPlayer>"
            "</PluginConfig>\n"
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


def player_node(bench: Bench, character_id: int) -> str:
    owner = bench.entity_owner(bench.diag("entities"), character_id)
    return owner[0][0] if owner else "node-1"


def run(bench: Bench, stats_file: Path, world: Path) -> dict:
    start = time.monotonic()
    bench.cluster.start()
    bench.cluster.wait_ready()
    ready = time.monotonic()
    api = bench.start_client(CLIENT_ID)
    character = bench.wait_spawned(CLIENT_ID)
    spawned = time.monotonic()
    bench.ensure_gameplay_focus(CLIENT_ID)
    result = {
        "cluster_ready_s": ready - start,
        "client_spawned_s": spawned - ready,
        "views": {},
    }
    print(
        f"cluster ready in {ready - start:.0f} s, client spawned {spawned - ready:.0f} s later",
        flush=True,
    )
    for name, eye, target in geometry(world):
        node = player_node(bench, character["entityId"])
        x, y, z = eye
        bench.record(
            f"teleport-{name}",
            bench.cluster.node(
                node, f"/teleport?client={CLIENT_ID}&x={x}&y={y}&z={z}", method="POST"
            ),
        )
        moved = time.monotonic()
        time.sleep(3)
        try:
            api.character_look_at(*target, tolerance=1)
        except Exception as error:  # noqa: BLE001 -- the look is best effort
            print(f"{name}: look_at failed: {error}", flush=True)
        timing = timeline(lambda: stats(stats_file), moved, SETTLE)
        rows = stats(stats_file)
        bench.screenshot(CLIENT_ID, f"view-{name}")
        result["views"][name] = {
            "timing": timing,
            "node": node,
            "impostors": {str(i): r for i, r in rows.items()},
        }
        print(f"{name} (player on {node}): {json.dumps(timing)}", flush=True)
    return result


def main() -> None:
    results = HOST.results / time.strftime("impostors-views-%Y%m%d-%H%M%S")
    results.mkdir(parents=True, exist_ok=True)
    world = template(results)
    bench = Bench(
        "IMPOSTORS-CLUSTER-VIEWS",
        "Impostor levels in views of the Seed 2 ring across a cluster",
        "1",
        results_root=results,
        world_template=world,
        administrators=[CLIENT_ID],
    )
    result = {}
    try:
        bench.cluster.kill()
        bench.cluster.prepare(wipe=True)
        stats_file = stage(bench)
        result = run(bench, stats_file, world)
        bench.record("views", result)
    except Exception as error:  # noqa: BLE001 -- reported as a failed check
        bench.check("no error", False, f"{type(error).__name__}: {error}")
    finally:
        bench.close()
    lines = ["# Level views, cluster", ""]
    if result:
        lines.append(
            f"Cluster ready in {result['cluster_ready_s']:.0f} s, "
            f"client spawned {result['client_spawned_s']:.0f} s later."
        )
        lines.append("")
        lines += timing_table({n: v["timing"] for n, v in result["views"].items()})
        for name, v in result["views"].items():
            lines += ["", f"## {name} (player on {v['node']})", ""] + level_table(
                v["impostors"]
            )
    text = "\n".join(lines) + "\n"
    (bench.dir / "summary.md").write_text(text)
    print(text)
    failed = [c for c in bench.result.checks if not c.passed]
    print(
        f"{'PASSED' if not failed else 'FAILED'}: {len(failed)} failure(s), evidence in {bench.dir}"
    )
    sys.exit(len(failed))


if __name__ == "__main__":
    main()
