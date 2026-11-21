"""The id of an asteroid pack or a grid pack, as the server plugin logs it in its
"Impostors pin:" line and the Impostors mod compares it (SE-1826).

    python tools/pack_id.py <ImpostorsAsteroidPack.dat or ImpostorsGridPack.dat or mod folder>...

Prints one line per file: the id in hex, the kind and the path. A mod folder stands for the
pack file in it. Reads only the index, never the shapes.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

ASTEROID_MAGIC = 0x50414D49  # "IMAP"
GRID_MAGIC = 0x50474D49  # "IMGP"
NAMES = (
    "ImpostorsAsteroidPack.dat",
    "ImpostorsAsteroidPack.bin",
    "ImpostorsGridPack.dat",
)
FNV = 0xCBF29CE484222325
PRIME = 0x100000001B3
MASK = (1 << 64) - 1


def mix(h: int, value: int) -> int:
    """AsteroidPack.Mix: FNV-1a over the value's eight bytes, little endian"""
    value &= MASK
    for b in range(8):
        h ^= (value >> (b * 8)) & 0xFF
        h = (h * PRIME) & MASK
    return h


class Reader:
    def __init__(self, f):
        self.f = f

    def take(self, fmt: str):
        size = struct.calcsize("<" + fmt)
        data = self.f.read(size)
        if len(data) != size:
            raise EOFError("the index ends early")
        return struct.unpack("<" + fmt, data)

    def byte(self) -> int:
        return self.take("B")[0]

    def string(self) -> str:
        """BinaryReader.ReadString: 7-bit encoded length, UTF-8"""
        length = shift = 0
        while True:
            b = self.byte()
            length |= (b & 0x7F) << shift
            shift += 7
            if b < 0x80:
                break
        return self.f.read(length).decode("utf-8")


def asteroid_pack_id(r: Reader) -> int:
    """AsteroidPack.ReadIndex's Id"""
    fmt = r.byte()
    world = r.string()
    settings = r.take("q")[0] if fmt >= 2 else 0
    h = mix(FNV, fmt)
    for unit in struct.unpack(
        f"<{len(world.encode('utf-16-le')) // 2}H", world.encode("utf-16-le")
    ):
        h = mix(h, unit)
    h = mix(h, settings)
    if fmt >= 5:
        colors = r.byte()
        r.take(f"{colors * 3}f")
    (count,) = r.take("i")
    for _ in range(count):
        asteroid, content, version, _complete = r.take("qqiB")
        if fmt >= 3:
            r.take("fdddffff")
        if fmt >= 4:
            for _ in range(r.byte()):
                r.string()
                r.take("i")
        levels = r.byte()
        h = mix(mix(mix(h, asteroid), content), version)
        for _ in range(levels):
            lod, length = r.take("Bi")
            h = mix(h, lod | (length << 8))
    return h


def pack_id(path: Path) -> tuple[int, str]:
    """(id as a signed long, kind) of a pack file"""
    with open(path, "rb") as f:
        r = Reader(f)
        (magic,) = r.take("i")
        if magic == ASTEROID_MAGIC:
            h = asteroid_pack_id(r)
            kind = "asteroid"
        elif magic == GRID_MAGIC:
            r.byte()
            r.string()
            h = r.take("q")[0] & MASK
            kind = "grid"
        else:
            raise ValueError(f"{path}: not an Impostors pack")
    return (h - (1 << 64) if h >= 1 << 63 else h), kind


def pack_file(path: Path) -> Path:
    if path.is_dir():
        for name in NAMES:
            if (path / name).exists():
                return path / name
        raise FileNotFoundError(f"{path}: no pack file")
    return path


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for arg in sys.argv[1:]:
        path = pack_file(Path(arg))
        h, kind = pack_id(path)
        print(f"{h & MASK:016x} {kind} {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
