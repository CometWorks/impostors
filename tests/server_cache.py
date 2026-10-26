"""In-game check of the server's disk cache of built shapes: the server starts
with an empty cache and the client joins, then the server restarts and the
client joins again. The restart must build nothing, take every shape from the
cache, and keep the versions, so the client's own disk cache sends no shapes
either. Before the restart the cache folder gets files retention has to drop
(40 days unused, built 100 days ago, and enough to go over the cap) and one
it must keep (built 40 days ago, used half a day ago as uses.bin says). The
background sweep (SE-711) drops them a couple of minutes after the restart;
its log line has its timing. A third start turns asteroid
occlusion off: the asteroids are built again, the grids still come from the
cache. Shape files sit in subfolders of their first 2 hex digits; the first
start moves a flat file there. The first start also moves the cache folder of older versions
(ImpostorsCache) to the new place, and the restart leaves a second old folder
alone with a log hint. Prints one PASS or FAIL line per check and exits with the number of
failures.

    uv run --no-project --with httpx --with pillow python tests/server_cache.py
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from cache import session  # noqa: E402
from cache_retention import read_uses, write_uses  # noqa: E402
from scenario import CLIENT, SERVER, check  # noqa: E402

CAP_MB = 8
CACHE = rig.cache_folder(SERVER.data)
OLD_CACHE = SERVER.data / "ImpostorsCache"
MARKER = "moved.marker"
JUNK = 0x7E00000000000000
SWEPT = "Impostor cache: swept"
SUMMARY = re.compile(
    r"Impostor shapes in the last minute: (\d+) built, (\d+) from the cache, "
    r"(?:\d+ from the asteroid pack, )?(\d+) unchanged"
)


def config(**options) -> str:
    return rig.plugin_config(
        RebuildSeconds=10,
        RefreshSeconds=20,
        MaxQuadsPerFrame=20000,
        CacheMegabytes=CAP_MB,
        **options,
    )


def shape_files() -> dict[str, int]:
    return {f.name: f.stat().st_size for f in CACHE.glob("*/*.shapes")}


def path_of(entity: int) -> Path:
    """Where the cache keeps an entity's file, in the subfolder of its first 2 hex digits"""
    name = f"{entity:016x}"
    return CACHE / name[:2] / f"{name}.shapes"


def server_log() -> str:
    log = SERVER.game_log()
    return log.read_text(errors="replace") if log else ""


def builds() -> dict[str, int]:
    """Impostor build lines of the current server log, per kind"""
    counts: dict[str, int] = {}
    for kind in re.findall(r"Impostor build: id=\d+ kind=(\w+)", server_log()):
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def summary(timeout: float = 120) -> tuple[int, int, int]:
    """The totals of the per-minute shape lines, once one counts the client's builds"""
    deadline = time.monotonic() + timeout
    while True:
        lines = [tuple(map(int, m)) for m in SUMMARY.findall(server_log())]
        total = tuple(sum(x) for x in zip(*lines)) if lines else (0, 0, 0)
        if sum(total[:2]) > 0 or time.monotonic() > deadline:
            return total
        time.sleep(5)


def seed() -> tuple[list[Path], list[Path], Path]:
    """Files retention must drop: unused for 40 days, built 100 days ago but used
    lately, and 12 MB used yesterday that put the folder over the cap while the real
    entries are newer. And one to keep: built 40 days ago, used half a day ago."""
    now = time.time()
    old, filler = [], []
    uses = read_uses(CACHE) if (CACHE / "uses.bin").exists() else {}
    for i in range(10):
        path = path_of(JUNK + i)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(os.urandom(100 * 1024))
        days = 40 if i < 5 else 100
        os.utime(path, (now - days * 86400, now - days * 86400))
        if days == 100:
            uses[JUNK + i] = 0.1
        old.append(path)
    kept = path_of(JUNK + 300)
    kept.parent.mkdir(exist_ok=True)
    kept.write_bytes(os.urandom(10 * 1024))
    os.utime(kept, (now - 40 * 86400, now - 40 * 86400))
    uses[JUNK + 300] = 0.5
    write_uses(CACHE, uses)
    for i in range(12):
        path = path_of(JUNK + 100 + i)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(os.urandom(1024 * 1024))
        os.utime(path, (now - 86400, now - 86400))
        filler.append(path)
    return old, filler, kept


FLAT = path_of(JUNK + 200)


def restart(**options) -> None:
    SERVER.stop()
    SERVER.write_plugin_config(config(**options))
    SERVER.start()


def main():
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
    shutil.rmtree(CACHE, ignore_errors=True)
    shutil.rmtree(OLD_CACHE, ignore_errors=True)
    OLD_CACHE.mkdir(parents=True)
    (OLD_CACHE / MARKER).write_text("old cache folder")
    (OLD_CACHE / FLAT.name).write_bytes(b"flat layout")
    for f in CLIENT.mod_storage.glob("cache-*"):
        f.unlink()

    try:
        SERVER.start()
        session("first join")
        check(
            "first start: old cache folder moved",
            not OLD_CACHE.exists() and (CACHE / MARKER).exists(),
            f"old exists={OLD_CACHE.exists()}",
        )
        check(
            "first start: flat file moved into its subfolder",
            FLAT.exists() and not (CACHE / FLAT.name).exists(),
        )
        first = builds()
        built, cached, _ = summary()
        real = {n: b for n, b in shape_files().items() if n != FLAT.name}
        check("first start: shapes built", sum(first.values()) > 0, first)
        check(
            "first start: none from the cache",
            cached == 0,
            f"{built} built, {cached} cached",
        )
        check(
            "first start: shapes on disk",
            len(real) > 0,
            f"{len(real)} files, {sum(real.values()) // 1024} KB",
        )

        old, filler, kept = seed()
        OLD_CACHE.mkdir()
        restart()
        rejoin = session("rejoin after restart", lambda: SWEPT in server_log())
        check("restart: the background sweep ran", SWEPT in server_log())
        again = builds()
        built, cached, _ = summary()
        check("restart: nothing built", sum(again.values()) == 0, again)
        check(
            "restart: second old folder left, with a hint",
            OLD_CACHE.exists() and "is no longer used" in server_log(),
        )
        check(
            "restart: shapes from the cache",
            built == 0 and cached >= len(real),
            f"{built} built, {cached} cached, {len(real)} files",
        )
        check(
            "restart: versions kept, the client's cache sends nothing",
            int(rejoin.get("shapes", -1)) == 0,
            f"{rejoin.get('shapes')} shapes, {rejoin.get('shapekb')} KB",
        )
        files = shape_files()
        check(
            "cache under its cap",
            sum(files.values()) <= CAP_MB * 1024 * 1024,
            f"{sum(files.values()) // 1024} KB",
        )
        check(
            "unused and long ago built entries dropped",
            not any(p.exists() for p in old),
        )
        check("old build used lately kept", kept.exists())
        check(
            "real entries kept",
            all(name in files for name in real),
            f"{sum(name in files for name in real)} of {len(real)}",
        )
        check(
            "filler dropped oldest first",
            0 < sum(not p.exists() for p in filler) < len(filler) + 1,
            f"{sum(not p.exists() for p in filler)} of {len(filler)} dropped",
        )

        restart(AsteroidOcclusion=False)
        session("occlusion off")
        third = builds()
        check(
            "occlusion off: asteroids built again, grids from the cache",
            third.get("Voxel", 0) > 0 and third.get("Grid", 0) == 0,
            third,
        )
    finally:
        CLIENT.stop()
        SERVER.stop()
        shutil.rmtree(OLD_CACHE, ignore_errors=True)
        lines = [
            l
            for l in server_log().splitlines()
            if "Impostor cache" in l or "Impostor shapes" in l
        ]
        print("server:", *lines[-6:], sep="\n  ")
    failures = rig_failures()
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


def rig_failures() -> int:
    import scenario

    return scenario.failures


if __name__ == "__main__":
    main()
