"""Measures what the shape caches save (SE-662): the scenario world on an
isolated Magnetar server and one headless client with only the mod, joined in
four cache states, each repeated IMP_REPEATS times (default 3):

    cold         server and client caches empty: what a server without the
                 caches does on every start
    server warm  the server restarted with its cache, the client's emptied
    both warm    the server restarted with its cache, the client kept its own
    client warm  the server restarted with an empty cache, the client kept its own

Per join: the server's builds and cache hits with their CPU time (from the
"Impostor build:" and "Impostor from the cache:" lines), the first-sight
latency (from the session being ready until every far impostor has a shape)
and the shape bytes the client got. Prints a Markdown table per state and the
build table of the cold joins (tests/build_times.py), and PASS or FAIL lines
for what the caches must do. Exits with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/cache_gains.py
"""

from __future__ import annotations

import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from cache import FAR, shaped  # noqa: E402
from scenario import CLIENT, SERVER, check  # noqa: E402

REPEATS = int(os.environ.get("IMP_REPEATS", "3"))
CACHE = rig.cache_folder(SERVER.data)
LOGS = rig.TEST_ROOT / f"{rig.TASK}-gains"
LINE = re.compile(r"Impostor (build|from the cache): (.*)")
STATES = ("cold", "server warm", "both warm", "client warm")


def config() -> str:
    return rig.plugin_config(
        RebuildSeconds=10, RefreshSeconds=600, MaxQuadsPerFrame=20000
    )


def server_log() -> str:
    log = SERVER.game_log()
    return log.read_text(errors="replace") if log else ""


def server_work() -> dict:
    """Builds and cache hits of the current server log: counts and CPU ms, per kind"""
    work = {"build": {}, "from the cache": {}}
    for what, rest in LINE.findall(server_log()):
        fields = dict(re.findall(r"(\w+)=(\S+)", rest))
        kind = work[what].setdefault(fields["kind"], {"n": 0, "cpu": 0.0, "snap": 0.0})
        kind["n"] += 1
        kind["cpu"] += float(fields["cpu"])
        kind["snap"] += float(fields["snapshot"])
    return work


def clear_client_cache() -> None:
    for f in list(CLIENT.mod_storage.glob("cache-*")):
        f.unlink()


def clear_server_cache() -> None:
    rig.clear_cache(SERVER.data)


def join() -> dict:
    """One session: the latency until the far impostors have shapes, what the client
    got once nothing new came for 20 s, then a quit after the cache saved itself"""
    CLIENT.launch(SERVER.port)
    api = CLIENT.wait_api()
    rig.wait_world(api)
    ready = time.monotonic()
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    api.character_teleport(*world.HOME)
    latency = None
    deadline = ready + 300
    while time.monotonic() < deadline:
        _, impostors = CLIENT.stats()
        if all(shaped(impostors, g) for g in FAR):
            latency = time.monotonic() - ready
            break
        time.sleep(0.5)
    last, still = None, 0
    while still < 20:
        time.sleep(5)
        totals, _ = CLIENT.stats()
        key = (totals.get("shapes"), totals.get("cached"))
        still = still + 5 if key == last else 0
        last = key
    time.sleep(45)  # The client's cache saves 30 s after the last new shape
    try:
        api.exit_game()
    except Exception:  # noqa: BLE001 -- the connection drops while exiting
        pass
    deadline = time.monotonic() + 180
    while CLIENT.pid() and time.monotonic() < deadline:
        time.sleep(2)
    CLIENT.stop()
    return {
        "latency": latency,
        "shapes": int(totals.get("shapes", 0)),
        "shapekb": int(totals.get("shapekb", 0)),
        "cached": int(totals.get("cached", 0)),
    }


def restart() -> None:
    SERVER.stop()
    SERVER.start()


def keep_log(name: str) -> None:
    log = SERVER.game_log()
    if log:
        shutil.copy(log, LOGS / f"{name}.log")


def run_state(state: str, n: int) -> dict:
    if state == "cold":
        clear_server_cache()
        clear_client_cache()
    elif state == "server warm":
        clear_client_cache()
    elif state == "client warm":
        clear_server_cache()
    restart()
    result = join()
    result["work"] = server_work()
    keep_log(f"{state.replace(' ', '-')}-{n}")
    print(state, n, result, flush=True)
    return result


def summarize(state: str, runs: list[dict]) -> str:
    med = lambda xs: statistics.median(xs) if xs else float("nan")
    kinds = sorted({k for r in runs for w in r["work"].values() for k in w})
    rows = []
    for kind in kinds:
        for what in ("build", "from the cache"):
            items = [r["work"][what].get(kind) for r in runs]
            n = med([i["n"] if i else 0 for i in items])
            cpu = med([i["cpu"] if i else 0 for i in items])
            snap = med([i["snap"] if i else 0 for i in items])
            if n:
                rows.append(
                    f"| {state} | {kind} {what} | {n:g} | {cpu:.1f} | {snap:.1f} |"
                )
    lat = [r["latency"] for r in runs if r["latency"] is not None]
    rows.append(
        f"| {state} | client: first sight {med(lat):.1f} s "
        f"(min {min(lat, default=float('nan')):.1f}, max {max(lat, default=float('nan')):.1f}), "
        f"{med([r['shapes'] for r in runs]):g} shapes, {med([r['shapekb'] for r in runs]):g} KB, "
        f"{med([r['cached'] for r in runs]):g} from its cache | | | |"
    )
    return "\n".join(rows)


def main():
    LOGS.mkdir(parents=True, exist_ok=True)
    world.prepare(
        SERVER.world,
        CLIENT.steam_id,
        CLIENT.name,
        grids=world.GRIDS + world.ZONE_GRIDS,
        zones=world.ZONES,
        mod=True,
    )
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(config())
    results: dict[str, list[dict]] = {s: [] for s in STATES}
    try:
        for n in range(REPEATS):
            for state in STATES:
                results[state].append(run_state(state, n))
    finally:
        CLIENT.stop()
        SERVER.stop()

    print(f"\nMachine: {os.uname().nodename}, {REPEATS} joins per state, medians\n")
    print("| State | What | Count | CPU ms | Snapshot ms |\n|---|---|---:|---:|---:|")
    for state in STATES:
        print(summarize(state, results[state]))
    print()
    cold_logs = sorted(LOGS.glob("cold-*.log"))
    if cold_logs:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "build_times.py"),
                *map(str, cold_logs),
            ]
        )

    def builds(state):
        return [
            sum(k["n"] for k in r["work"]["build"].values()) for r in results[state]
        ]

    def hits(state):
        return [
            sum(k["n"] for k in r["work"]["from the cache"].values())
            for r in results[state]
        ]

    check("cold: shapes built", all(b > 0 for b in builds("cold")), builds("cold"))
    for state in ("server warm", "both warm"):
        check(
            f"{state}: nothing built", all(b == 0 for b in builds(state)), builds(state)
        )
        check(
            f"{state}: shapes from the server cache",
            all(h > 0 for h in hits(state)),
            hits(state),
        )
    check(
        "client warm: built again",
        all(b > 0 for b in builds("client warm")),
        builds("client warm"),
    )
    for state in ("both warm", "client warm"):
        sent = [r["shapes"] for r in results[state]]
        check(
            f"{state}: no shapes sent, the client had them",
            all(s == 0 for s in sent),
            sent,
        )
    for state in STATES:
        lat = [r["latency"] for r in results[state]]
        check(f"{state}: far impostors seen", all(l is not None for l in lat), lat)
    failures = rig_failures()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


def rig_failures() -> int:
    import scenario

    return scenario.failures


if __name__ == "__main__":
    main()
