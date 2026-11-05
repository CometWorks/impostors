"""Copy of a voxel map (.vx2) with every voxel of one material, for comparing an ore's
asteroid impostor with the real asteroid (SE-1102). It renames every entry of the storage's
material index table, the voxels keep their indexes.

    python tests/vx2_material.py Asteroid512-008.vx2 Uraninite512-008.vx2 Uraninite_01
    IMP_ASTEROID=$PWD/Uraninite512-008.vx2 ... python tests/asteroid_lighting.py
"""

import gzip
import struct
import sys


def read7(data: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, i


def write7(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def main() -> None:
    source, target, material = sys.argv[1:4]
    data = gzip.decompress(open(source, "rb").read())
    # The material index table chunk (MyOctreeStorage.ChunkTypeEnum 2, version 1): its size,
    # the entry count, then 7 bit encoded index and string pairs, the first name a stone's
    i = data.index(b"\x02\x01", 1 + data[0])
    while True:
        _, j = read7(data, i)
        _, j = read7(data, j)
        size, j = read7(data, j)
        if data[j + 6 : j + 10] == b"Ston":
            break
        i = data.index(b"\x02\x01", i + 1)
    count = struct.unpack_from("<i", data, j)[0]
    name = material.encode()
    k, table = j + 4, bytearray(struct.pack("<i", count))
    for _ in range(count):
        index, k = read7(data, k)
        length, k = read7(data, k)
        k += length
        table += write7(index) + write7(len(name)) + name
    assert k == j + size, "not a material index table"
    chunk = write7(2) + write7(1) + write7(len(table)) + bytes(table)
    open(target, "wb").write(gzip.compress(data[:i] + chunk + data[k:]))
    print(f"{count} materials -> {material}")


if __name__ == "__main__":
    main()
