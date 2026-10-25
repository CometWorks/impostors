"""Cache consistency and race checks (SE-662): an isolated Magnetar server with
the server's shape cache and two headless clients with only the mod, which keep
their own caches. Repeats IMP_ROUNDS rounds (default 4), each:

- the server restarts with its warm cache (cold in round 0), stopped normally
  after even rounds and killed (SIGKILL) during a burst of rebuilds after odd
  ones, so its cache writes may be cut short and the world goes back to its
  last save while the cache has shapes of newer content
- both clients join at the same time and fill their caches from the same entries
- the content changes: a layer built onto the station and ground off again
  (the shapes go back to an earlier version), a repaint, a cut into the far
  asteroid, in round 0 also a split of the station and a merge of two grids.
  Each change must reach both clients within RebuildSeconds plus the time a
  build and its sending take.
- a burst of repaints faster than the rebuild interval, then the versions must
  settle to the same on the server, on both clients and in the cache files
- client 1 is killed during its cache save and joins again: it must show the
  server's versions

At the end the server restarts with its cache wiped: every entry must come out
with the input and version the cache served. Throughout, no errors of the
caches in the server's and clients' logs, no leftover temporary files, no
impostor without a shape or of a grid that is gone. Prints PASS or FAIL lines
and exits with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/cache_consistency.py
"""

from __future__ import annotations

import os
import re
import signal
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from scenario import CLIENT, SERVER, check, command  # noqa: E402

ROUNDS = int(os.environ.get("IMP_ROUNDS", "4"))
REBUILD = 10
# A change's build waits up to REBUILD after the last one, then the scan (1 s), the build
# and the shape credit of the players; more is a stale shape
CHANGE_LIMIT = REBUILD + 20
# A repaint changes no block, it shows at the next refresh of the viewed impostors
REFRESH = 30
PAINT_LIMIT = REFRESH + 20
CACHE = rig.cache_folder(SERVER.data)
OTHER = rig.Client(2, "Bravo")
CLIENTS = (CLIENT, OTHER)

# Two grids side by side, for the merge; 4 to 5 pixels across from HOME
MERGE_A = world.Grid(
    "MergeA",
    120000000000000021,
    (-7000.0, 2000.0, -3000.0),
    (16, 12, 16),
    True,
    world.GREEN,
)
MERGE_B = world.Grid(
    "MergeB",
    120000000000000022,
    (-7000.0, 2000.0, -2700.0),
    (16, 12, 16),
    True,
    world.RED,
)
STATION = world.STATION
ROCK = world.ROCK_FAR
WATCHED = (world.SHIP, STATION, world.FAR, ROCK, MERGE_A, MERGE_B)
SERVER_ERRORS = re.compile(
    r"Impostor cache: failed|Failed to build impostor|Failed to snapshot impostor"
    r"|Impostor cache not available|IOException|UnauthorizedAccess"
)
CLIENT_ERRORS = re.compile(
    r"Impostors: failed|Impostors:.*(?:exception|Exception|error)"
)


def config() -> str:
    return rig.plugin_config(
        RebuildSeconds=REBUILD, RefreshSeconds=REFRESH, MaxQuadsPerFrame=20000
    )


# ---------------------------------------------------------------------------
# State on each side
# ---------------------------------------------------------------------------


def server_entries(api) -> dict[int, tuple[int, int]]:
    """The server's entries with shapes: id -> (version, input)"""
    reply = command(api, "!iprobe inputs", 30)
    entries = {}
    for item in reply.split(":", 1)[1].split():
        id_, version, input_ = item.split(":")
        entries[int(id_)] = (int(version), int(input_))
    return entries


def server_grids(api) -> dict[int, int]:
    reply = command(api, "!iprobe grids", 30)
    return {
        int(i): int(n)
        for i, n in (item.split(":") for item in reply.split(":", 1)[1].split())
    }


def cache_files() -> dict[int, tuple[int, int]]:
    """The server's cache files: id -> (version, input), from their headers"""
    files = {}
    for path in CACHE.rglob("*.shapes"):
        raw = path.read_bytes()[:20]
        if len(raw) < 20:
            continue
        _, _, input_, version = struct.unpack("<iiqi", raw)
        files[
            int(path.stem, 16) - (1 << 64 if int(path.stem, 16) >= 1 << 63 else 0)
        ] = (
            version,
            input_,
        )
    return files


def client_versions(client) -> dict[int, tuple[int, str]]:
    """id -> (version of the state, version of the shape or 'none')"""
    _, impostors = client.stats()
    out = {}
    for id_, i in impostors.items():
        shape = i.get("shape", "none")
        out[id_] = (
            int(i.get("version", 0)),
            shape.split("/")[0] if shape != "none" else "none",
        )
    return out


def duplicates(client) -> list[str]:
    """Impostor ids listed more than once in the client's stats"""
    try:
        lines = (client.mod_storage / "stats.txt").read_text().splitlines()[1:]
    except FileNotFoundError:
        return []
    ids = [l.split()[0] for l in lines if l.startswith("id=")]
    return sorted({i for i in ids if ids.count(i) > 1})


def has_shape(client, id_: int) -> bool:
    i = client.stats()[1].get(id_)
    return bool(i) and i.get("shape", "none") != "none"


def wait(predicate, timeout: float, interval: float = 1.0):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(interval)


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def kill(pid_file: Path, pid: int | None) -> None:
    """SIGKILL to the process group; the leader is our child, reaped so the group ends"""
    if pid:
        os.killpg(pid, signal.SIGKILL)
        while rig._group_alive(pid):
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(0.5)
    pid_file.unlink(missing_ok=True)


def kill_server() -> None:
    kill(SERVER.pid_file, SERVER.pid())


def kill_client(client) -> None:
    kill(client.pid_file, client.pid())


def join(client):
    client.launch(SERVER.port)


def settle(client):
    api = client.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    api.character_teleport(*world.HOME)
    return api


def leave(client, api) -> None:
    try:
        api.exit_game()
    except Exception:  # noqa: BLE001 -- the connection drops while exiting
        pass
    wait(lambda: not client.pid(), 180, 2)
    client.stop()


def visible_ids(grids: dict[int, int]) -> set[int]:
    """The watched grids still on the server, and the asteroid"""
    return {g.entity_id for g in WATCHED if g.entity_id in grids} | {ROCK.entity_id}


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def agree(api, label: str, ids: set[int], timeout: float = 60) -> dict:
    """Waits until both clients show the server's version of every id with a shape of
    that version; checks it and that the cache files hold the same versions"""

    def same():
        server = server_entries(api)
        views = [client_versions(c) for c in CLIENTS]
        bad = []
        for id_ in ids:
            if id_ not in server:
                bad.append(f"{id_}: not on the server")
                continue
            version = server[id_][0]
            for c, view in zip(CLIENTS, views):
                got = view.get(id_)
                if got is None or got[0] != version or got[1] != str(version):
                    bad.append(f"{id_}@{c.name}: {got} != {version}")
        return (not bad, server, bad)

    deadline = time.monotonic() + timeout
    while True:
        ok, server, bad = same()
        if ok or time.monotonic() > deadline:
            break
        time.sleep(2)
    check(f"{label}: clients show the server's versions", ok, "; ".join(bad[:4]))
    files = cache_files()
    stale = [
        f"{i}: file {files.get(i)} entry {server[i]}"
        for i in ids
        if i in server and i in files and files[i] != server[i]
    ]
    check(
        f"{label}: cache files hold the entries' versions",
        not stale,
        "; ".join(stale[:4]),
    )
    dups = [d for c in CLIENTS for d in duplicates(c)]
    check(f"{label}: no duplicated impostors", not dups, dups)
    return server


def change(
    api,
    label: str,
    text: str,
    ids: set[int],
    gone: set[int] = frozenset(),
    limit: float = CHANGE_LIMIT,
):
    """Runs a probe command and waits until every id got a new version on both clients
    (or, for gone, left them); the delay must stay within CHANGE_LIMIT"""
    before = server_entries(api)
    start = time.monotonic()
    reply = command(api, text, 30)

    def done():
        server = server_entries(api)
        views = [client_versions(c) for c in CLIENTS]
        for id_ in ids:
            new = server.get(id_, (None,))[0]
            if new is None or new == before.get(id_, (None,))[0]:
                # The input changed but the shapes came out the same: that's fine too
                if not (id_ in server and server[id_][1] != before.get(id_, (0, 0))[1]):
                    return False
            for view in views:
                if view.get(id_, (0, ""))[1] != str(server[id_][0]):
                    return False
        return all(id_ not in view for id_ in gone for view in views)

    ok = wait(done, limit + 30)
    delay = time.monotonic() - start
    check(
        f"{label}: reached both clients in {delay:.0f} s (limit {limit:.0f})",
        bool(ok) and delay <= limit,
        reply,
    )
    return delay


def errors(label: str) -> None:
    log = SERVER.game_log()
    lines = [
        l
        for l in (log.read_text(errors="replace").splitlines() if log else [])
        if SERVER_ERRORS.search(l)
    ]
    check(f"{label}: no cache errors on the server", not lines, lines[:3])
    for c in CLIENTS:
        text = c.log.read_text(errors="replace") if c.log.exists() else ""
        bad = [l for l in text.splitlines() if CLIENT_ERRORS.search(l)]
        check(f"{label}: no cache errors on {c.name}", not bad, bad[:3])


# ---------------------------------------------------------------------------
# The rounds
# ---------------------------------------------------------------------------


def round_(n: int, delays: list[float]) -> None:
    label = f"round {n}"
    if CACHE.exists():
        leftovers = list(CACHE.rglob("*.tmp"))
        if n > 0 and n % 2 == 0:  # after a normal stop
            check(
                f"{label}: no temporary files after a normal stop",
                not leftovers,
                leftovers,
            )
    SERVER.start()
    for c in CLIENTS:
        join(c)
    apis = [settle(c) for c in CLIENTS]
    api = apis[0]
    grids = server_grids(api)
    ids = visible_ids(grids)
    wait(lambda: all(has_shape(c, i) for i in ids for c in CLIENTS), 240, 2)
    agree(api, f"{label} join", ids, 120)

    hue = (world.YELLOW, world.BLUE)[n % 2]
    original = server_entries(api).get(STATION.entity_id)
    delays.append(
        change(
            api,
            f"{label} build a layer",
            f"!iprobe grow {STATION.entity_id} {hue}",
            {STATION.entity_id},
        )
    )
    x = server_grids(api)  # The layer is past the old largest x
    layer = _max_x(api)
    delays.append(
        change(
            api,
            f"{label} grind it off",
            f"!iprobe remove {STATION.entity_id} x {layer}",
            {STATION.entity_id},
        )
    )
    back = server_entries(api).get(STATION.entity_id)
    check(
        f"{label} grind it off: the station's first input and version again",
        back == original,
        f"{back} vs {original}",
    )
    delays.append(
        change(
            api,
            f"{label} repaint",
            f"!iprobe paint {world.SHIP.entity_id} {(world.GREEN, world.RED, world.BLUE, world.YELLOW)[n % 4]}",
            {world.SHIP.entity_id},
            limit=PAINT_LIMIT,
        )
    )
    c = ROCK.center
    delays.append(
        change(
            api,
            f"{label} mine the asteroid",
            f"!iprobe cut {ROCK.entity_id} {c[0] + 200 - 15 * n} {c[1]} {c[2]} 80",
            {ROCK.entity_id},
        )
    )
    if n == 0:
        before = set(x)
        delays.append(
            change(
                api,
                f"{label} split the station",
                f"!iprobe remove {STATION.entity_id} z 20",
                {STATION.entity_id},
            )
        )
        new = set(server_grids(api)) - before
        check(f"{label} split: a new grid", len(new) == 1, new)
        if new:
            piece = new.pop()
            ok = wait(
                lambda: all(has_shape(cl, piece) for cl in CLIENTS), CHANGE_LIMIT + 30
            )
            check(
                f"{label} split: the new piece shown on both clients", bool(ok), piece
            )
        delays.append(
            change(
                api,
                f"{label} merge",
                f"!iprobe merge {MERGE_A.entity_id} {MERGE_B.entity_id}",
                {MERGE_A.entity_id},
                gone={MERGE_B.entity_id},
            )
        )

    # A burst of changes faster than the rebuild interval, builds and cache writes in
    # flight: a layer built and ground off three times, the station repainted between
    for k in range(6):
        if k % 2 == 0:
            command(api, f"!iprobe grow {STATION.entity_id} {world.RED}", 30)
        else:
            command(api, f"!iprobe remove {STATION.entity_id} x {_max_x(api)}", 30)
        command(
            api,
            f"!iprobe paint {STATION.entity_id} {(world.RED, world.GREEN, world.BLUE)[k % 3]}",
            30,
        )
        time.sleep(1.5)
    ids = visible_ids(server_grids(api))
    time.sleep(REFRESH + 5)
    agree(api, f"{label} after the burst", ids, CHANGE_LIMIT + 30)

    # Client 1 dies while its cache saves, 30 s after the last new shape
    command(
        api,
        f"!iprobe paint {world.FAR.entity_id} {(world.GREEN, world.YELLOW)[n % 2]}",
        30,
    )
    time.sleep(REFRESH + 5 + 30 + n % 3)
    kill_client(CLIENT)
    join(CLIENT)
    # Probe commands go through client 2: client 1's chat may miss the first replies
    apis[0] = settle(CLIENT)
    api = apis[1]
    agree(api, f"{label} client 1 back after a kill", ids, 180)
    errors(label)

    if n % 2 == 1:
        # Killed right after a change, its build or cache write may be in flight
        command(api, f"!iprobe paint {STATION.entity_id} {world.YELLOW}", 30)
        time.sleep(REFRESH + 0.3 * n)
        for cl, a in zip(CLIENTS, apis):
            leave(cl, a)
        kill_server()
    else:
        for cl, a in zip(CLIENTS, apis):
            leave(cl, a)
        SERVER.stop()


def _max_x(api) -> int:
    reply = command(api, f"!iprobe maxx {STATION.entity_id}", 30)
    return int(reply.split(":", 1)[1])


def final(n: int) -> None:
    """Warm start, then a start with the cache wiped: the same inputs and versions"""
    SERVER.start()
    join(CLIENT)
    api = settle(CLIENT)
    ids = visible_ids(server_grids(api))
    wait(lambda: ids <= set(server_entries(api)), 240, 2)
    warm = server_entries(api)
    leave(CLIENT, api)
    SERVER.stop()
    rig.clear_cache(SERVER.data)
    SERVER.start()
    join(CLIENT)
    api = settle(CLIENT)
    wait(lambda: ids <= set(server_entries(api)), 240, 2)
    cold = server_entries(api)
    leave(CLIENT, api)
    SERVER.stop()
    inputs = [
        f"{i}: {warm.get(i)} vs {cold.get(i)}"
        for i in ids
        if warm.get(i, (0, 0))[1] != cold.get(i, (0, 1))[1]
    ]
    versions = [
        f"{i}: {warm.get(i)} vs {cold.get(i)}"
        for i in ids
        if warm.get(i, (0, 0))[0] != cold.get(i, (1, 0))[0]
    ]
    check("final: the cache served the content's current input", not inputs, inputs[:4])
    check("final: rebuilt shapes have the cached versions", not versions, versions[:4])


def main():
    world.prepare(
        SERVER.world,
        CLIENT.steam_id,
        CLIENT.name,
        grids=world.GRIDS + (MERGE_A, MERGE_B),
        mod=True,
    )
    SERVER.prepare([c.steam_id for c in CLIENTS])
    SERVER.write_plugin_config(config())
    rig.clear_cache(SERVER.data)
    for c in CLIENTS:
        c.mod_storage.mkdir(parents=True, exist_ok=True)
        for f in c.mod_storage.glob("cache-*"):
            f.unlink()
    delays: list[float] = []
    try:
        for n in range(ROUNDS):
            round_(n, delays)
        final(ROUNDS)
    finally:
        for c in CLIENTS:
            c.stop()
        SERVER.stop()
    if delays:
        print(
            f"change delays: max {max(delays):.0f} s, median {sorted(delays)[len(delays) // 2]:.0f} s"
        )
    failures = rig_failures()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


def rig_failures() -> int:
    import scenario

    return scenario.failures


if __name__ == "__main__":
    main()
