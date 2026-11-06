"""Early asteroids (SE-712): the client shows the asteroids of the asteroid pack and of its
disk cache before the server sends them. Joins above the asteroid ring of "Octahedron -
Seed 2" (like tests/asteroid_pack.py, which writes the pack this needs) and times how soon the
asteroids have shapes and are drawn, with and without the early asteroids.

    IMP_TASK=se712 IMP_WORKSPACE=~/ws/se uv run --no-project --with httpx --with pillow \
        --with numpy python tests/asteroid_pack.py write    # the pack, ~/.se-test/<task>-pack
    ... python tests/early_asteroids.py measure              # the joins below
    ... python tests/early_asteroids.py swap                 # a mined asteroid's early shape
    ... python tests/early_asteroids.py offline              # offline with the client plugin
    ... python tests/early_asteroids.py levels               # MP levels match offline's
    IMP_HEIGHT=10000 IMP_SETTLE=60 ...                       # meters above the ring, seconds

`measure` starts the server once, with the pack in the world, and lets a first client fill
it: every asteroid built or taken from the pack. That client leaves its character above the
ring looking away from it, so every later join starts there. Then two series of joins, a new
client process every time: to the running server, which has every asteroid's shapes, and
each to a server that just restarted (`IMP_SERIES=running,restarted`):

- cold: no shape cache, the pack mod staged empty
- warm, noearly: the cache of the cold join, early asteroids off (the mod's "noearly" file)
- warm: the same cache, early asteroids on
- pack, noearly: no cache, the pack, early asteroids off
- pack: no cache, the pack, early asteroids on

Right after the session is ready the client turns towards the ring. A line per join: when
the voxel impostors first had a shape, counted from the first tick of the session (join),
and when those in view were first drawn after the turn (turn), as median, 90th percentile
and last in seconds; the bytes the server sent and the shapes in them; the early asteroids
shown, kept and swapped. PASS when the early joins have every asteroid's shape within
3 seconds of the session's start and draw them within a second of the turn.

`swap` cuts an asteroid in view, so the server's version no longer is the pack's: a join
with an empty cache shows the pack's shape early and swaps to the server's when its state
comes, the next join takes the server's version from its cache and keeps it. Then the server
starts over from the uncut world, back at the pack's version, while the client's cache still
has the cut one as its last state (SE-1109): the next join must get the pack's version from
the server. PASS/FAIL per step.

`offline` loads the world offline in a client with the client plugin and the pack, without
the early asteroids and with them, and prints the same join numbers for the asteroids beyond
the far plane, which the plugin's service sends at the pace of its build queue. Then
(SE-740) the approach must settle within 0.2 s, and an asteroid cut through the client probe
must show its new shape. `IMP_SKIP_NOEARLY=1` skips the run without the early asteroids.

`levels` (SE-744) compares the levels drawn offline with those of a multiplayer join that
has the pack and the cache of an earlier join without it, from the same view. PASS when 90%
of the asteroids drawn in both are at the same level and the levels are at least 0.92 times
offline's: multiplayer shows near asteroids that are real offline as impostors, from the
same quad budget.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import asteroid_pack as ap  # noqa: E402
import rig  # noqa: E402
import ring  # noqa: E402

HEIGHT = float(os.environ.get("IMP_HEIGHT", "10000"))
SETTLE = float(os.environ.get("IMP_SETTLE", "60"))
EMPTY_PACK = rig.TEST_ROOT / f"{rig.TASK}-nopack"
SERVER = ap.SERVER
CLIENT = ap.CLIENT
CONFIG = dict(
    MaxDistance=500000,
    MaxImpostorsPerPlayer=4000,
    MaxQuadsPerFrame=16000,
    RebuildSeconds=10,
)


def geometry():
    center, normal, side, _ = ring.ring_geometry(SERVER.world)
    base = center + side * 120000
    eye = base + normal * (HEIGHT + 300)
    target = base + np.cross(normal, side) * (HEIGHT + 5000)
    away = eye + normal * 20000
    return eye, target, away


def settle() -> None:
    """At least SETTLE seconds, and until the client got no new shape for 40 s: the shape
    cache saves 30 s after the last one, a restarted server sends them for minutes"""
    start = time.monotonic()
    last, since = None, start
    while time.monotonic() - start < 600:
        time.sleep(5)
        totals = CLIENT.stats()[0]
        now = (totals.get("impostors"), totals.get("shapes"), totals.get("cached"))
        if now != last:
            last, since = now, time.monotonic()
        if time.monotonic() - start >= SETTLE and time.monotonic() - since >= 40:
            return


def stage_client(ids, with_pack: bool) -> None:
    """The world's mods, with the pack mod's folder empty unless with_pack"""
    mods = CLIENT.appdata / "Mods"
    ring.stage_mods(mods, [i for i in ids if i != ap.PACK_ID])
    # Remote stages only folders with something in them, a mod without a pack file
    EMPTY_PACK.mkdir(parents=True, exist_ok=True)
    (EMPTY_PACK / "metadata.mod").write_text("<MyObjectBuilder_ModInfo />")
    link = mods / f"{ap.PACK_ID}.sbm"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(ap.PACK if with_pack else EMPTY_PACK)
    # A published pack the world lists too (Seed 2 has one since SE-739) goes empty with it
    if not with_pack:
        for i in ids:
            if (ring.WORKSHOP / str(i) / ap.PACK_FILE).exists():
                (mods / f"{i}.sbm").unlink()
                (mods / f"{i}.sbm").symlink_to(EMPTY_PACK)


def clear_cache() -> None:
    for cache in CLIENT.mod_storage.glob("cache-*"):
        cache.unlink()


def percentiles(values) -> str:
    if not values:
        return "none"
    v = np.array(sorted(values)) / 60
    return f"{np.median(v):.1f}/{np.percentile(v, 90):.1f}/{v[-1]:.1f} s"


# SE-739: after the settle, a teleport this far towards the ring (0: none), and how long after
# it the drawn asteroids got the level they end with
APPROACH = float(os.environ.get("IMP_APPROACH", str(HEIGHT / 2)))


def approach(api, client, eye, target, tag: str) -> list[int]:
    """Teleports APPROACH meters towards target, looks at it, waits, and returns per drawn
    asteroid the ticks from the teleport to its last shape change"""
    if APPROACH <= 0:
        return []
    eye, target = np.array(eye, dtype=float), np.array(target, dtype=float)
    to = target - eye
    api.character_teleport(*(eye + to / np.linalg.norm(to) * APPROACH))
    time.sleep(2)
    api.character_look_at(*target, tolerance=2)
    time.sleep(25)
    totals, impostors = client.stats()
    jump = int(totals.get("jumptick", 0))
    settled = [
        max(0, int(i["shapetick"]) - jump)
        for i in impostors.values()
        if i.get("kind") == "Voxel"
        and i.get("drawn") == "1"
        and int(i.get("shapetick", -1)) >= 0
    ]
    print(
        f"{tag}, {APPROACH:.0f} m closer: {len(settled)} drawn, at their last level "
        f"{percentiles(settled)} after the teleport",
        flush=True,
    )
    return settled


def join(
    ids,
    tag: str,
    with_pack: bool,
    early: bool,
    eye,
    target,
    away,
    shot: Path | None = None,
) -> dict:
    """A client process joins, turns to the ring, settles, looks away and quits. With shot,
    a screenshot after the settle goes there."""
    noearly = CLIENT.mod_storage / "noearly"
    CLIENT.mod_storage.mkdir(parents=True, exist_ok=True)
    if early:
        noearly.unlink(missing_ok=True)
    else:
        noearly.write_text("")
    CLIENT.launch(SERVER.port)
    stage_client(ids, with_pack)
    result = {"tag": tag}
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        position = np.array(api.get_character()["position"])
        moved = np.linalg.norm(position - eye) > 1000
        if moved:
            api.character_teleport(*eye)
            time.sleep(3)
        api.character_look_at(*target, tolerance=2)
        settle()
        totals, impostors = CLIENT.stats()
        if shot:
            shot.write_bytes(api.screenshot_bytes(ignore_sprites=True))
        result["approach"] = approach(api, CLIENT, eye, target, tag)
        api.character_teleport(*eye)
        time.sleep(2)
        api.character_look_at(*away, tolerance=2)
    finally:
        CLIENT.stop()
        noearly.unlink(missing_ok=True)

    base = int(totals.get("jumptick", 0)) if moved else 0
    turn = int(totals.get("turntick", 0))
    voxels = [i for i in impostors.values() if i.get("kind") == "Voxel"]
    # After a teleport, shapes the client already had before it count as there at once
    shaped = [
        max(0, int(i["firstshape"]) - base) for i in voxels if int(i["firstshape"]) >= 0
    ]
    drawn = [
        max(0, int(i["firstdrawn"]) - turn)
        for i in voxels
        if i.get("drawn") == "1" and int(i["firstdrawn"]) >= 0
    ]
    # When the drawn ones got the level they end with, after the turn (SE-739)
    settled = [
        max(0, int(i["shapetick"]) - turn)
        for i in voxels
        if i.get("drawn") == "1" and int(i.get("shapetick", -1)) >= 0
    ]
    result.update(
        totals=totals,
        impostors=impostors,
        voxels=len(voxels),
        shaped=shaped,
        drawn=drawn,
        settled=settled,
        moved=moved,
        turn_s=(turn - base) / 60,
    )
    print(
        f"{tag}: {len(voxels)} asteroids{' (teleported)' if moved else ''}, join {percentiles(shaped)}, "
        f"turned at {result['turn_s']:.1f} s, {len(drawn)} drawn {percentiles(drawn)} after it, "
        f"at their last level {percentiles(settled)}; "
        f"server sent {totals.get('recvkb')} KB: {totals.get('shapes')} shapes "
        f"({totals.get('shapekb')} KB), {totals.get('cached')} from the cache or pack "
        f"({totals.get('packed')} pack); early {totals.get('earlyloads')} loads in "
        f"{totals.get('earlyms')} ms, kept {totals.get('kept')}, swapped {totals.get('swapped')}; "
        f"client picks {totals.get('picks', '0')}, {totals.get('notices', '0')} changed asteroids "
        f"with {totals.get('noticelevels', '0')} levels",
        flush=True,
    )
    return result


def start_server():
    if not (ap.PACK / ap.PACK_FILE).exists():
        raise SystemExit(f"No pack in {ap.PACK}, run asteroid_pack.py write first")
    ids = ap.setup_world(True)
    SERVER.write_plugin_config(rig.plugin_config(**CONFIG))
    SERVER.start(timeout=3600)
    return ids


def fill(ids, eye, target, away) -> None:
    """The first client: until the impostors stop changing, so the server has every
    asteroid's shapes; leaves its character above the ring"""
    clear_cache()
    CLIENT.launch(SERVER.port)
    stage_client(ids, False)
    try:
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        api.character_teleport(*eye)
        time.sleep(5)
        api.character_look_at(*target, tolerance=2)
        last, since, start = None, time.monotonic(), time.monotonic()
        while time.monotonic() - since < 40 and time.monotonic() - start < 600:
            time.sleep(10)
            count = CLIENT.stats()[0].get("impostors")
            if count != last:
                last, since = count, time.monotonic()
        print(
            f"fill: {last} impostors after {time.monotonic() - start:.0f} s", flush=True
        )
        api.character_look_at(*away, tolerance=2)
        time.sleep(2)
    finally:
        CLIENT.stop()


RUNS = [
    # tag, pack, early, clear the cache first
    ("cold", False, True, True),
    ("warm, noearly", False, False, False),
    ("warm", False, True, False),
    ("pack, noearly", True, False, True),
    ("pack", True, True, True),
]


# IMP_RUNS: only these tags of RUNS, comma separated, for example "pack"
RUN_TAGS = [t for t in os.environ.get("IMP_RUNS", "").split(",") if t]


def measure() -> int:
    series = os.environ.get("IMP_SERIES", "running,restarted").split(",")
    ids = start_server()
    eye, target, away = geometry()
    results = {}
    try:
        fill(ids, eye, target, away)
        for name in series:
            for tag, pack, early, clear in RUNS:
                if RUN_TAGS and tag not in RUN_TAGS:
                    continue
                if clear:
                    clear_cache()
                # A server that just started has no asteroid's shapes yet: it takes two
                # from the pack or its cache every 10 ticks
                if name == "restarted":
                    SERVER.stop()
                    SERVER.start(timeout=3600)
                results[(name, tag)] = join(
                    ids, f"{name}, {tag}", pack, early, eye, target, away
                )
    finally:
        CLIENT.stop()
        SERVER.stop()

    ok = True
    for key in [
        (name, tag)
        for name in series
        for tag in ("warm", "pack")
        if (name, tag) in results
    ]:
        r = results[key]
        fast = (
            r["shaped"]
            and np.percentile(r["shaped"], 90) <= 180
            and r["drawn"]
            and np.percentile(r["drawn"], 90) <= 60
        )
        ok &= bool(fast)
        print(
            f"{'PASS' if fast else 'FAIL'} {', '.join(key)}: 90% of the asteroids within "
            f"{np.percentile(r['shaped'], 90) / 60 if r['shaped'] else -1:.1f} s of the join, "
            f"drawn within {np.percentile(r['drawn'], 90) / 60 if r['drawn'] else -1:.1f} s of the turn",
            flush=True,
        )
    return 0 if ok else 1


def swap() -> int:
    ids = start_server()
    eye, target, away = geometry()
    versions = ap.pack_versions()
    ok = True
    try:
        fill(ids, eye, target, away)

        # An asteroid in view, cut so the server's version differs from the pack's
        clear_cache()
        CLIENT.mod_storage.mkdir(parents=True, exist_ok=True)
        CLIENT.launch(SERVER.port)
        stage_client(ids, True)
        api = CLIENT.wait_api()
        rig.wait_world(api, timeout=2400)
        rig.ensure_character(api)
        rig.focus_gameplay(api)
        api.character_look_at(*target, tolerance=2)
        time.sleep(20)
        _, impostors = CLIENT.stats()
        asteroid = max(
            (
                i
                for i, f in impostors.items()
                if i in versions and f.get("drawn") == "1"
            ),
            key=lambda i: int(impostors[i].get("lod", 0)),
        )
        _, _, size = ap.entry(api, asteroid)
        print(
            "server:",
            ap.command(
                api, f"!iprobe vcut {asteroid} {size * 0.25:.0f} {size * 0.25:.0f}"
            ),
            flush=True,
        )
        mined = ap.wait_for(
            lambda: (v := ap.entry(api, asteroid)[0]) not in (None, versions[asteroid])
            and v,
            120,
        )
        ok &= ap.check(
            bool(mined),
            f"cut {asteroid}: server version {mined}, pack {versions[asteroid]}",
        )
        api.character_look_at(*away, tolerance=2)
        time.sleep(2)
        CLIENT.stop()

        # Only the pack knows it: the pack's shape early, then the server's
        clear_cache()
        first = join(ids, "swap, pack only", True, True, eye, target, away)
        totals = first["totals"]
        logged = [e.split(":") for e in totals.get("swaplog", "").split(",") if e]
        swapped = [e for e in logged if int(e[0]) == asteroid]
        _, impostors = CLIENT.stats()
        final = impostors.get(asteroid, {})
        picks = totals.get("picks") == "1"
        if picks:
            # SE-739: the server announces the changed asteroid with all of its levels, the
            # client keeps picking it itself
            ok &= ap.check(
                int(totals.get("notices", 0)) >= 1
                and int(totals.get("noticelevels", 0)) >= 1
                and final.get("shape", "").split("/")[0] == str(mined),
                f"changed {asteroid} announced: {totals.get('notices')} notices with "
                f"{totals.get('noticelevels')} levels, shape at the end {final.get('shape')}",
            )
        else:
            ok &= ap.check(
                bool(swapped)
                and int(swapped[0][1]) == versions[asteroid]
                and int(swapped[0][2]) == mined
                and final.get("shape", "").split("/")[0] == str(mined),
                f"early pack shape {versions[asteroid]} swapped to {mined}: swap log {swapped}, "
                f"shape at the end {final.get('shape')}",
            )

        # The cache has the server's version now: early with it, kept
        second = join(ids, "swap, cached", True, True, eye, target, away)
        totals = second["totals"]
        logged = [e.split(":") for e in totals.get("swaplog", "").split(",") if e]
        _, impostors = CLIENT.stats()
        final = impostors.get(asteroid, {})
        ok &= ap.check(
            not [e for e in logged if int(e[0]) == asteroid]
            and final.get("shape", "").split("/")[0] == str(mined)
            and int(totals.get("swapped", 1)) == 0
            and int(totals.get("notices", 0)) == 0,
            f"next join from the cache: no swap of {asteroid} ({totals.get('swapped')} swaps, "
            f"{totals.get('kept')} kept, {totals.get('notices')} notices), shape {final.get('shape')}",
        )

        # The server back at the pack's version, the cache's last state still the cut one.
        # The client shows that one early, the server must replace it (SE-1109).
        SERVER.stop()
        start_server()
        third = join(
            ids, "swap, server back at the pack", True, True, eye, target, away
        )
        totals = third["totals"]
        _, impostors = CLIENT.stats()
        final = impostors.get(asteroid, {})
        ok &= ap.check(
            int(totals.get("notices", 0)) >= 1
            and final.get("shape", "").split("/")[0] == str(versions[asteroid]),
            f"server back at the pack's {versions[asteroid]}: {totals.get('notices')} notices, "
            f"shape at the end {final.get('shape')}",
        )
    finally:
        CLIENT.stop()
        SERVER.stop()
    return 0 if ok else 1


def offline_run(
    tag: str, early: bool, cut: bool = False, shot: Path | None = None, **config
) -> dict:
    """The world offline with the client plugin and the pack (see asteroid_pack.offline_run).
    With cut, mines a drawn asteroid of the pack after the approach (see offline_cut). With
    shot, a screenshot after the settle goes there; config goes into the plugin config.
    """
    import local

    client = rig.Client(2, "Viktor", plugin=True, probe=cut)
    world_dir = client.appdata / "Saves" / ring.WORLD_NAME
    ring.PLUGIN = True
    ids = ring.copy_world(world_dir)
    for name in ("Sandbox.sbc", "Sandbox_config.sbc"):
        f = world_dir / name
        f.write_text(
            f.read_text(encoding="utf-8").replace(
                "<OnlineMode>PUBLIC</OnlineMode>", "<OnlineMode>OFFLINE</OnlineMode>"
            ),
            encoding="utf-8",
        )
    ap.add_pack(world_dir)
    ids.append(ap.PACK_ID)
    center, normal, side, _ = ring.ring_geometry(world_dir)
    client.appdata.mkdir(parents=True, exist_ok=True)
    (client.appdata / "Impostors.cfg").write_text(
        rig.plugin_config(AsteroidPack=True, **config), encoding="utf-8"
    )
    shutil.rmtree(client.appdata / "Storage/Impostors/Cache", ignore_errors=True)
    noearly = client.loaded_mod_storage / "noearly"
    client.loaded_mod_storage.mkdir(parents=True, exist_ok=True)
    if early:
        noearly.unlink(missing_ok=True)
    else:
        noearly.write_text("")
    args = client.args
    client.args = lambda _port: [
        a for a in args(0) if a not in ("--connect", "127.0.0.1:0")
    ]
    rig.CLIENT_GIB = 10.0
    client.launch(0)
    ap.stage(client.appdata / "Mods", ids)
    try:
        api = client.wait_api()
        api.load(str(world_dir), online_mode="OFFLINE")
        local.wait(lambda: local.past_questions(api), 900, 2)
        rig.ensure_character(api)
        rig.wait_world(api, timeout=1200)
        rig.focus_gameplay(api)
        base = center + side * 120000
        eye = base + normal * (HEIGHT + 300)
        target = base + np.cross(normal, side) * (HEIGHT + 5000)
        api.character_teleport(*eye)
        time.sleep(5)
        api.character_look_at(*target, tolerance=2)
        time.sleep(max(SETTLE, 150))
        totals, impostors = client.stats()
        if shot:
            shot.write_bytes(api.screenshot_bytes(ignore_sprites=True))
        settled = approach(api, client, eye, target, tag)
        mined = offline_cut(client) if cut else None
    finally:
        client.stop()
        noearly.unlink(missing_ok=True)
    jump = int(totals.get("jumptick", 0))
    voxels = [i for i in impostors.values() if i.get("kind") == "Voxel"]
    shaped = [
        max(0, int(i["firstshape"]) - jump) for i in voxels if int(i["firstshape"]) >= 0
    ]
    print(
        f"{tag}: {len(voxels)} asteroids, after the teleport {percentiles(shaped)}; early "
        f"{totals.get('earlyloads')} loads in {totals.get('earlyms')} ms, kept "
        f"{totals.get('kept')}, swapped {totals.get('swapped')}",
        flush=True,
    )
    return {
        "shaped": shaped,
        "totals": totals,
        "impostors": impostors,
        "settled": settled,
        "mined": mined,
    }


def offline_cut(client) -> bool:
    """SE-740: cuts the largest drawn asteroid still at the pack's version through the client
    probe; True once the impostor shows a new version, from the local service's notice
    """
    versions = ap.pack_versions()
    _, impostors = client.stats()
    drawn = [
        (i, f)
        for i, f in impostors.items()
        if f.get("kind") == "Voxel"
        and f.get("drawn") == "1"
        and i in versions
        and f.get("shape", "").split("/")[0] == str(versions[i])
    ]
    if not drawn:
        print("FAIL offline cut: no drawn asteroid at the pack's version", flush=True)
        return False
    asteroid, f = max(drawn, key=lambda d: int(d[1].get("lod", 0)))
    size = 2 * float(f["r"]) / 3**0.5
    (client.appdata / "vcut.txt").write_text(
        f"{asteroid} {size * 0.25:.0f} {size * 0.25:.0f}", encoding="utf-8"
    )
    start = time.monotonic()
    shape = f.get("shape")
    while time.monotonic() - start < 180:
        time.sleep(5)
        totals, impostors = client.stats()
        shape = impostors.get(asteroid, {}).get("shape", "none")
        version = shape.split("/")[0]
        if version not in ("none", str(versions[asteroid])):
            print(
                f"PASS offline cut {asteroid}: pack version {versions[asteroid]}, shows "
                f"{shape} after {time.monotonic() - start:.0f} s; {totals.get('notices')} "
                f"notices with {totals.get('noticelevels')} levels",
                flush=True,
            )
            return True
    print(
        f"FAIL offline cut {asteroid}: still {shape} after 180 s, pack version "
        f"{versions[asteroid]}",
        flush=True,
    )
    return False


def offline() -> int:
    if not (ap.PACK / ap.PACK_FILE).exists():
        raise SystemExit(f"No pack in {ap.PACK}, run asteroid_pack.py write first")
    if not os.environ.get("IMP_SKIP_NOEARLY"):
        offline_run("offline, noearly", False)
    r = offline_run("offline", True, cut=True)
    fast = bool(r["shaped"]) and np.percentile(r["shaped"], 90) <= 180
    print(
        f"{'PASS' if fast else 'FAIL'} offline: 90% of the asteroids within 3 s of the teleport",
        flush=True,
    )
    # SE-740: the local renderer picks the pack's asteroids itself, as multiplayer clients do
    settled = bool(r["settled"]) and max(r["settled"]) <= 12
    if APPROACH > 0:
        print(
            f"{'PASS' if settled else 'FAIL'} offline: the last level within 0.2 s of the "
            f"approach",
            flush=True,
        )
    else:
        settled = True
    return 0 if fast and settled and r["mined"] else 1


def drawn_levels(impostors: dict) -> dict[int, int]:
    """The level of the shape each drawn asteroid shows"""
    return {
        i: int(f["shape"].split("/")[1])
        for i, f in impostors.items()
        if f.get("kind") == "Voxel"
        and f.get("drawn") == "1"
        and f.get("shape") != "none"
    }


def levels() -> int:
    """SE-744: a client with the pack and a cache from earlier joins, which has a few levels
    of the pack's asteroids, draws them at the levels offline does from the same view. The
    cold join, without the early asteroids and 40 km farther away, fills the cache with the
    levels the server picked; the next join picks from the pack and the cache. Run it low over
    the ring (IMP_HEIGHT=3000), where the game draws many real asteroids. Offline runs with
    the server's config. Screenshots go to IMP_SHOTS (~/.se-test/<task>-shots)."""
    shots = Path(os.environ.get("IMP_SHOTS", rig.TEST_ROOT / f"{rig.TASK}-shots"))
    shots.mkdir(parents=True, exist_ok=True)
    offline_stats = offline_run("offline", True, shot=shots / "offline.png", **CONFIG)[
        "impostors"
    ]
    offline = drawn_levels(offline_stats)
    # The offline run's world has no Impostors mod, the server's needs it
    ring.PLUGIN = False
    ids = start_server()
    eye, target, away = geometry()
    try:
        clear_cache()
        # Without the early asteroids the client doesn't pick: the server sends the levels it
        # picks, and the cache keeps them, as before SE-739. From 40 km farther away, so the
        # cache gets coarser levels than this view wants.
        far = eye + (eye - target) / np.linalg.norm(eye - target) * 40000
        join(ids, "cold", False, False, far, target, away)
        r = join(
            ids, "warm, pack", True, True, eye, target, away, shots / "multiplayer.png"
        )
    finally:
        CLIENT.stop()
        SERVER.stop()
    for name, stats in (("offline", offline_stats), ("multiplayer", r["impostors"])):
        (shots / f"{name}.json").write_text(json.dumps(stats))
    mp = drawn_levels(r["impostors"])
    common = sorted(set(offline) & set(mp))
    if not common:
        print(
            f"FAIL levels: no asteroid drawn in both ({len(offline)} offline, {len(mp)} MP)"
        )
        return 1
    same = sum(mp[i] == offline[i] for i in common)
    coarser = sum(mp[i] < offline[i] for i in common)
    ratio = np.exp(np.mean([np.log(mp[i] / offline[i]) for i in common]))
    # Near asteroids the game draws offline, but not in multiplayer until they replicate,
    # are impostors in multiplayer only: their quads come out of the same budget, so a few
    # of the others get a level coarser
    extra = sum(
        int(f["shape"].split("/")[2]) // 2
        for i, f in r["impostors"].items()
        if f.get("kind") == "Voxel"
        and f.get("shape") != "none"
        and f.get("ready") != "1"
        and i not in offline_stats
    )
    ok = same >= 0.9 * len(common) and ratio >= 0.92
    print(
        f"{'PASS' if ok else 'FAIL'} levels: {len(common)} asteroids drawn offline and in "
        f"multiplayer, {same} at the same level, {coarser} coarser in multiplayer, its "
        f"levels {ratio:.2f} times offline's (geometric mean); offline draws {len(offline)}, "
        f"multiplayer {len(mp)}; {extra} of the {CONFIG['MaxQuadsPerFrame']} quads go to "
        f"asteroids that only multiplayer shows as impostors",
        flush=True,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    modes = {"measure": measure, "swap": swap, "offline": offline, "levels": levels}
    if mode not in modes:
        raise SystemExit(__doc__)
    sys.exit(modes[mode]())
