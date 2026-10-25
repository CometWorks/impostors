"""In-game check of the client's disk cache of impostor shapes: the client joins
the scenario world, leaves, and joins again. The second session must get no
shapes for the unchanged impostors, only the few bytes that tell the client to
take them from its cache. Before the second join the cache file gets entries
retention has to drop (a month old, superseded versions, over the size cap)
and another world's file, so the end checks the cap too. Prints one PASS or
FAIL line per check and exits with the number of failures.

    uv run --no-project --with httpx --with pillow python tests/cache.py
    IMP_PLUGIN=1 ...    # the client runs the client plugin, the world has no mod
"""

from __future__ import annotations

import os
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rig  # noqa: E402
import world  # noqa: E402
from scenario import CLIENT, PLUGIN, SERVER, check, wait_stats  # noqa: E402

# ShapeCache.cs
MAX_BYTES = 64 * 1024 * 1024
MAX_DAYS = 30
MAGIC = 0x43504D49
ENTRY_HEADER = 8 + 4 + 1 + 4 + 4

FAR = (world.SHIP, world.STATION, world.FAR, world.ROCK_FAR)


def shaped(impostors: dict, entity) -> bool:
    """The client has a shape for the impostor; drawn or not depends on the view"""
    i = impostors.get(entity.entity_id)
    return bool(i) and i["shape"] != "none"


def storage() -> Path:
    return CLIENT.plugin_storage if PLUGIN else CLIENT.mod_storage


def prefix() -> str:
    return "Impostors." if PLUGIN else ""


def cache_files() -> list[Path]:
    return sorted(storage().glob(f"{prefix()}cache-*.bin"))


def checksum(data: bytes) -> int:
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def read_pack(path: Path) -> list[tuple[int, int, int, int, bytes]]:
    raw = path.read_bytes()
    (length,) = struct.unpack_from("<i", raw)
    data = raw[4 : 4 + length]
    (stored,) = struct.unpack_from("<I", raw, 4 + length)
    assert checksum(data) == stored, f"{path} checksum"
    magic, fmt, count = struct.unpack_from("<iBi", data)
    assert magic == MAGIC and fmt == 1
    pos, entries = 9, []
    for _ in range(count):
        id_, version, lod, day, size = struct.unpack_from("<qiBii", data, pos)
        pos += ENTRY_HEADER
        entries.append((id_, version, lod, day, data[pos : pos + size]))
        pos += size
    return entries


def write_pack(path: Path, entries) -> None:
    body = bytearray(struct.pack("<iBi", MAGIC, 1, len(entries)))
    for id_, version, lod, day, shape in entries:
        body += struct.pack("<qiBii", id_, version, lod, day, len(shape)) + shape
    path.write_bytes(
        struct.pack("<i", len(body)) + bytes(body) + struct.pack("<I", checksum(body))
    )


def today() -> int:
    return int(time.time() // 86400) + 719162  # days since 0001-01-01, as DateTime


def session(name: str) -> dict:
    """Joins, waits until the far grids and the asteroid have their shapes and the shapes
    settled, leaves through the menu so the cache gets saved. The client's totals."""
    CLIENT.launch(SERVER.port)
    api = CLIENT.wait_api()
    rig.wait_world(api)
    rig.ensure_character(api)
    rig.focus_gameplay(api)
    api.character_teleport(*world.HOME)
    totals, impostors = wait_stats(lambda i: all(shaped(i, g) for g in FAR), 180)
    check(f"{name}: far impostors have shapes", all(shaped(impostors, g) for g in FAR))
    # Finer levels follow the first ones; the totals once nothing arrived for 20 s
    last, still = None, 0
    while still < 20:
        time.sleep(5)
        totals, _ = CLIENT.stats()
        key = (totals.get("shapes"), totals.get("cached"))
        still = still + 5 if key == last else 0
        last = key
    print(
        f"{name}:",
        {
            k: totals.get(k)
            for k in ("shapes", "shapekb", "cached", "cachekb", "stored", "restored")
        },
    )
    try:
        api.exit_game()
    except Exception:  # noqa: BLE001 -- the connection drops while exiting
        pass
    deadline = time.monotonic() + 180
    while CLIENT.pid() and time.monotonic() < deadline:
        time.sleep(2)
    check(f"{name}: client exited by itself", not CLIENT.pid())
    CLIENT.stop()
    return totals


def seed(own: Path) -> dict:
    """Adds what retention must drop to the client's cache, older in use than the
    real entries, and a second world's file used before this one"""
    real = read_pack(own)
    d = today()
    junk_id = 9_000_000_000_000_000
    old = [(junk_id + i, 1, 16, d - MAX_DAYS - 5, os.urandom(1000)) for i in range(50)]
    # An older version of each real impostor, used before the real one
    superseded = [(i, v ^ 0x5A5A, lod, d, s) for i, v, lod, _, s in real]
    # 72 MB of other impostors, pushes the world's file over the cap
    big = [(junk_id + 1000 + i, 7, 64, d, os.urandom(72 * 1024)) for i in range(1024)]
    write_pack(own, old + superseded + big + real)

    other = own.with_name(f"{prefix()}cache-00000000deadbeef.bin")
    write_pack(
        other,
        [(junk_id + 5000 + i, 3, 64, d - 2, os.urandom(64 * 1024)) for i in range(160)],
    )
    index = storage() / f"{prefix()}cache-index.txt"
    lines = [l for l in index.read_text().splitlines() if "deadbeef" not in l]
    lines.insert(0, f"cache-00000000deadbeef.bin {160 * 64 * 1024} {d - 2}")
    index.write_text("\n".join(lines) + "\n")
    return {"real": len(real), "other": other}


def main():
    world.prepare(
        SERVER.world,
        CLIENT.steam_id,
        CLIENT.name,
        grids=world.GRIDS + world.ZONE_GRIDS,
        zones=world.ZONES,
        mod=not PLUGIN,
    )
    SERVER.prepare([CLIENT.steam_id])
    SERVER.write_plugin_config(
        rig.plugin_config(RebuildSeconds=10, RefreshSeconds=20, MaxQuadsPerFrame=20000)
    )
    storage().mkdir(parents=True, exist_ok=True)
    for f in cache_files() + list(storage().glob(f"{prefix()}cache-index.txt")):
        f.unlink()

    try:
        SERVER.start()
        first = session("first join")
        files = cache_files()
        check("cache file saved", len(files) == 1, files)
        if not files:
            return
        own = files[0]
        entries = read_pack(own)
        check(
            "cache holds what the first join got",
            len(entries) >= int(first.get("shapes", 0)) > 0,
            f"{len(entries)} entries, {first.get('shapes')} shapes",
        )

        seeded = seed(own)
        second = session("rejoin")
        check(
            "rejoin: no shapes sent",
            int(second.get("shapes", -1)) == 0,
            f"{second.get('shapes')} shapes, {second.get('shapekb')} KB; first join {first.get('shapekb')} KB",
        )
        check(
            "rejoin: shapes from the cache",
            int(second.get("cached", 0)) > 0,
            second.get("cached"),
        )

        entries = read_pack(own)
        data = sum(len(e[4]) for e in entries)
        size = sum(f.stat().st_size for f in cache_files())
        check(
            "cache under its cap",
            data <= MAX_BYTES,
            f"{data / 1048576:.1f} MB of shapes, {size / 1048576:.1f} MB on disk",
        )
        check(
            "month old entries dropped",
            all(e[3] >= today() - MAX_DAYS for e in entries),
        )
        versions = {}
        for e in entries:
            versions.setdefault(e[0], set()).add(e[1])
        check("one version per impostor", all(len(v) == 1 for v in versions.values()))
        check("other world's file dropped", not seeded["other"].exists())
    finally:
        CLIENT.stop()
        SERVER.stop()
        lines = [
            l
            for l in SERVER.game_log().read_text(errors="replace").splitlines()
            if "disk cache" in l
        ]
        print("server:", *lines[-3:], sep="\n  ")
    print(
        f"{'PASSED' if rig_failures() == 0 else 'FAILED'}: {rig_failures()} failure(s)"
    )
    sys.exit(rig_failures())


def rig_failures() -> int:
    import scenario

    return scenario.failures


if __name__ == "__main__":
    main()
