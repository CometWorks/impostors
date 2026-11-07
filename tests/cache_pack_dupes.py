"""Counts the entries of a client shape cache file that the world's asteroid pack has too
(SE-1223): the same asteroid id, version and level, with the same bytes. The cache is for what
the server streams, so a cache next to its pack should have none.

    python tests/cache_pack_dupes.py <cache-*.bin> <ImpostorsAsteroidPack.dat> [--json]

Prints the cache's entries and bytes, the asteroid states among them, and how many entries
and bytes the pack has too. seed() adds pack levels to a cache file, as clients before
SE-1223 did, so a test can check that loading drops them.
"""

from __future__ import annotations

import datetime
import json
import struct
import sys
from pathlib import Path

STATE_LOD = 255
CACHE_MAGIC = 0x43504D49
PACK_MAGIC = 0x50414D49


def read_cache(path: Path) -> list[tuple[int, int, int, bytes]]:
    """(id, version, level, shape) per entry, oldest use first"""
    raw = path.read_bytes()
    (length,) = struct.unpack_from("<i", raw, 0)
    data = raw[4 : 4 + length]
    magic, fmt, count = struct.unpack_from("<iBi", data, 0)
    if magic != CACHE_MAGIC:
        raise ValueError(f"{path}: not a shape cache")
    pos = 9
    entries = []
    for _ in range(count):
        id_, version, lod, _day = struct.unpack_from("<qiBi", data, pos)
        pos += 17
        if fmt != 1:
            pos += 4
        (size,) = struct.unpack_from("<i", data, pos)
        pos += 4
        entries.append((id_, version, lod, data[pos : pos + size]))
        pos += size
    return entries


def read_string(f) -> str:
    """BinaryReader.ReadString: 7-bit encoded length, UTF-8"""
    length = shift = 0
    while True:
        b = f.read(1)[0]
        length |= (b & 0x7F) << shift
        shift += 7
        if b < 0x80:
            break
    return f.read(length).decode("utf-8")


def read_pack(path: Path) -> dict[tuple[int, int, int], tuple[int, int]]:
    """(id, version, level) -> (file offset, length)"""
    index = {}
    with path.open("rb") as f:
        magic, fmt = struct.unpack("<iB", f.read(5))
        if magic != PACK_MAGIC:
            raise ValueError(f"{path}: not an asteroid pack")
        read_string(f)
        if fmt >= 2:
            f.read(8)
        (count,) = struct.unpack("<i", f.read(4))
        offset = 0
        for _ in range(count):
            id_, _input, version, _complete = struct.unpack("<qqiB", f.read(21))
            if fmt >= 3:
                f.read(4 + 24 + 16)
            if fmt >= 4:
                for _ in range(f.read(1)[0]):
                    read_string(f)
                    f.read(4)
            for _ in range(f.read(1)[0]):
                lod, size = struct.unpack("<Bi", f.read(5))
                index[(id_, version, lod)] = (offset, size)
                offset += size
        data_start = f.tell()
    return {k: (data_start + o, n) for k, (o, n) in index.items()}


def checksum(data: bytes) -> int:
    """ShapeCache.Checksum: FNV-1a 32 bit, as a signed int"""
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def write_cache(path: Path, entries: list[tuple[int, int, int, bytes]]) -> None:
    """Format 2, every entry used and received today"""
    today = (
        datetime.datetime.now(datetime.timezone.utc).date() - datetime.date(1, 1, 1)
    ).days
    body = bytearray(struct.pack("<iBi", CACHE_MAGIC, 2, len(entries)))
    for id_, version, lod, shape in entries:
        body += (
            struct.pack("<qiBiii", id_, version, lod, today, today, len(shape)) + shape
        )
    path.write_bytes(
        struct.pack("<i", len(body))
        + bytes(body)
        + struct.pack("<i", checksum(bytes(body)))
    )


def seed(cache: Path, pack: Path, count: int) -> int:
    """Appends up to count pack levels the cache doesn't have, as the oldest entries; how many"""
    entries = read_cache(cache) if cache.exists() else []
    have = {e[:3] for e in entries}
    added = []
    with pack.open("rb") as f:
        for key, (offset, length) in read_pack(pack).items():
            if len(added) == count:
                break
            if key in have:
                continue
            f.seek(offset)
            added.append((*key, f.read(length)))
    write_cache(cache, added + entries)
    return len(added)


def compare(cache: Path, pack: Path) -> dict:
    entries = read_cache(cache)
    index = read_pack(pack)
    result = {
        "entries": len(entries),
        "bytes": sum(len(e[3]) for e in entries),
        "states": sum(1 for e in entries if e[2] == STATE_LOD),
        "packed": 0,
        "packed_bytes": 0,
        "packed_differ": 0,
    }
    with pack.open("rb") as f:
        for id_, version, lod, shape in entries:
            where = index.get((id_, version, lod))
            if where is None:
                continue
            f.seek(where[0])
            if f.read(where[1]) == shape:
                result["packed"] += 1
                result["packed_bytes"] += len(shape)
            else:
                result["packed_differ"] += 1
    return result


def main() -> None:
    result = compare(Path(sys.argv[1]), Path(sys.argv[2]))
    if "--json" in sys.argv:
        print(json.dumps(result))
        return
    print(
        f"{result['entries']} entries, {result['bytes'] / 1024:.0f} KB, {result['states']} states; "
        f"in the pack too: {result['packed']} entries, {result['packed_bytes'] / 1024:.0f} KB"
        + (
            f", {result['packed_differ']} with other bytes"
            if result["packed_differ"]
            else ""
        )
    )


if __name__ == "__main__":
    main()
