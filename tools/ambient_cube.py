"""Six-color ambient cube of a skybox, for the AsteroidAmbientCube setting.

The game lights voxels in shade from an environment probe: the skybox seen from the camera,
prefiltered so that its last mip holds, per cube face, about the cosine-weighted average of
the sky over the hemisphere around that face's axis (Shaders/EnvProbe/EnvPrefiltering.hlsl,
Shaders/Lighting/EnvAmbient.hlsli). This script computes the same six averages from the
skybox's DDS cube map, one per world axis (+X, -X, +Y, -Y, +Z, -Z), turned by the world's
EnvironmentOrientation, and prints them as the setting's value: colors relative to their mean
brightness, so the impostors keep their fitted brightness and get the sky's tint and
direction.

    uv run --no-project --with numpy --with texture2ddecoder python tools/ambient_cube.py \
        <Content or mod folder>/Textures/BackgroundCube/Final/BackgroundCube.dds

The orientation defaults to the one of the game's Environment.sbc (60.3955536, -61.1861954,
90.90578, used as radians as the game reads them); pass --orientation Y P R for a world whose
environment definition has another. Reads BC7 (sRGB or not), BC1 and BC6H (unsigned) cube maps.
"""

import argparse
import struct
import sys

import numpy as np
import texture2ddecoder

# Face size the averages are taken at: a smooth function of the sky, so a small mip does
GRID = 32

# The colors are of screen values, and in the shade the game's tone mapping and sRGB encoding
# shrink differences in light to about their square root: the real stone asteroid's shade
# under Octahedron's skybox is (0.80, 1.07, 1.23) times its gray, the sky's light (0.66, 1.05,
# 1.48) times (SE-1743)
SCREEN_POWER = 0.5

DXGI = {
    71: ("bc1", False),
    72: ("bc1", True),
    95: ("bc6h", False),
    98: ("bc7", False),
    99: ("bc7", True),
}


def read_faces(path):
    """The six faces at GRID texels, linear RGB float arrays"""
    data = open(path, "rb").read()
    if data[:4] != b"DDS ":
        sys.exit(f"{path}: not a DDS file")
    height, width = struct.unpack_from("<II", data, 12)
    mips = max(1, struct.unpack_from("<I", data, 28)[0])
    fourcc = data[84:88]
    offset = 128
    if fourcc == b"DX10":
        dxgi = struct.unpack_from("<I", data, 128)[0]
        offset += 20
        if dxgi not in DXGI:
            sys.exit(f"{path}: DXGI format {dxgi} not supported")
        codec, srgb = DXGI[dxgi]
    elif fourcc == b"DXT1":
        codec, srgb = "bc1", False
    else:
        sys.exit(f"{path}: format {fourcc!r} not supported")
    block = 8 if codec == "bc1" else 16

    def size(level):
        w = max(1, width >> level)
        h = max(1, height >> level)
        return ((w + 3) // 4) * ((h + 3) // 4) * block

    level = 0
    while level + 1 < mips and max(1, width >> level) > GRID:
        level += 1
    face_bytes = sum(size(k) for k in range(mips))
    n = max(1, width >> level)
    faces = []
    for f in range(6):
        start = offset + f * face_bytes + sum(size(k) for k in range(level))
        raw = data[start : start + size(level)]
        if codec == "bc6h":
            rgb = decode_bc6h(raw, n, n)
        else:
            decode = (
                texture2ddecoder.decode_bc1
                if codec == "bc1"
                else texture2ddecoder.decode_bc7
            )
            bgra = np.frombuffer(decode(raw, n, n), np.uint8).reshape(n, n, 4)
            rgb = bgra[:, :, 2::-1].astype(np.float64) / 255
            if srgb:
                rgb = np.where(
                    rgb <= 0.04045, rgb / 12.92, ((rgb + 0.055) / 1.055) ** 2.4
                )
        faces.append(rgb)
    return faces


class Bits:
    def __init__(self, raw):
        self.value = int.from_bytes(raw, "little")
        self.at = 0

    def read(self, count):
        v = (self.value >> self.at) & ((1 << count) - 1)
        self.at += count
        return v


def _fields(text):
    """A BC6H mode layout: (endpoint, channel, bit, count) per field in bit order,
    endpoints 0..3 = w, x, y, z; rw[10:15] stores bits 10..15 in reverse order"""
    out = []
    for item in text.split():
        name, bits = item.split("[")
        e = "wxyz".index(name[1])
        c = "rgb".index(name[0])
        bits = bits.rstrip("]")
        if ":" in bits:
            hi, lo = map(int, bits.split(":"))
            if hi >= lo:
                out += [(e, c, b, 1) for b in range(lo, hi + 1)]
            else:
                out += [(e, c, b, 1) for b in range(hi, lo + 1)][::-1]
        else:
            out.append((e, c, int(bits), 1))
    return out


# BC6H modes by their mode bits (D3D11 functional spec, BC6H): regions, endpoint bits,
# delta bits r g b, transformed endpoints, field layout
BC6H_MODES = {
    0b00: (
        2,
        10,
        (5, 5, 5),
        True,
        _fields(
            "gy[4] by[4] bz[4] rw[9:0] gw[9:0] bw[9:0] rx[4:0] gz[4] gy[3:0] gx[4:0] bz[0] "
            "gz[3:0] bx[4:0] bz[1] by[3:0] ry[4:0] bz[2] rz[4:0] bz[3]"
        ),
    ),
    0b01: (
        2,
        7,
        (6, 6, 6),
        True,
        _fields(
            "gy[5] gz[4] gz[5] rw[6:0] bz[0] bz[1] by[4] gw[6:0] by[5] bz[2] gy[4] bw[6:0] "
            "bz[3] bz[5] bz[4] rx[5:0] gy[3:0] gx[5:0] gz[3:0] bx[5:0] by[3:0] ry[5:0] rz[5:0]"
        ),
    ),
    0b00010: (
        2,
        11,
        (5, 4, 4),
        True,
        _fields(
            "rw[9:0] gw[9:0] bw[9:0] rx[4:0] rw[10] gy[3:0] gx[3:0] gw[10] bz[0] gz[3:0] "
            "bx[3:0] bw[10] bz[1] by[3:0] ry[4:0] bz[2] rz[4:0] bz[3]"
        ),
    ),
    0b00110: (
        2,
        11,
        (4, 5, 4),
        True,
        _fields(
            "rw[9:0] gw[9:0] bw[9:0] rx[3:0] rw[10] gz[4] gy[3:0] gx[4:0] gw[10] gz[3:0] "
            "bx[3:0] bw[10] bz[1] by[3:0] ry[3:0] bz[0] bz[2] rz[3:0] gy[4] bz[3]"
        ),
    ),
    0b01010: (
        2,
        11,
        (4, 4, 5),
        True,
        _fields(
            "rw[9:0] gw[9:0] bw[9:0] rx[3:0] rw[10] by[4] gy[3:0] gx[3:0] gw[10] bz[0] "
            "gz[3:0] bx[4:0] bw[10] by[3:0] ry[3:0] bz[1] bz[2] rz[3:0] bz[4] bz[3]"
        ),
    ),
    0b01110: (
        2,
        9,
        (5, 5, 5),
        True,
        _fields(
            "rw[8:0] by[4] gw[8:0] gy[4] bw[8:0] bz[4] rx[4:0] gz[4] gy[3:0] gx[4:0] bz[0] "
            "gz[3:0] bx[4:0] bz[1] by[3:0] ry[4:0] bz[2] rz[4:0] bz[3]"
        ),
    ),
    0b10010: (
        2,
        8,
        (6, 5, 5),
        True,
        _fields(
            "rw[7:0] gz[4] by[4] gw[7:0] bz[2] gy[4] bw[7:0] bz[3] bz[4] rx[5:0] gy[3:0] "
            "gx[4:0] bz[0] gz[3:0] bx[4:0] bz[1] by[3:0] ry[5:0] rz[5:0]"
        ),
    ),
    0b10110: (
        2,
        8,
        (5, 6, 5),
        True,
        _fields(
            "rw[7:0] bz[0] by[4] gw[7:0] gy[5] gy[4] bw[7:0] gz[5] bz[4] rx[4:0] gz[4] "
            "gy[3:0] gx[5:0] gz[3:0] bx[4:0] bz[1] by[3:0] ry[4:0] bz[2] rz[4:0] bz[3]"
        ),
    ),
    0b11010: (
        2,
        8,
        (5, 5, 6),
        True,
        _fields(
            "rw[7:0] bz[1] by[4] gw[7:0] by[5] gy[4] bw[7:0] bz[5] bz[4] rx[4:0] gz[4] "
            "gy[3:0] gx[4:0] bz[0] gz[3:0] bx[5:0] by[3:0] ry[4:0] bz[2] rz[4:0] bz[3]"
        ),
    ),
    0b11110: (
        2,
        6,
        (6, 6, 6),
        False,
        _fields(
            "rw[5:0] gz[4] bz[0] bz[1] by[4] gw[5:0] gy[5] by[5] bz[2] gy[4] bw[5:0] gz[5] "
            "bz[3] bz[5] bz[4] rx[5:0] gy[3:0] gx[5:0] gz[3:0] bx[5:0] by[3:0] ry[5:0] rz[5:0]"
        ),
    ),
    0b00011: (
        1,
        10,
        (10, 10, 10),
        False,
        _fields("rw[9:0] gw[9:0] bw[9:0] rx[9:0] gx[9:0] bx[9:0]"),
    ),
    0b00111: (
        1,
        11,
        (9, 9, 9),
        True,
        _fields("rw[9:0] gw[9:0] bw[9:0] rx[8:0] rw[10] gx[8:0] gw[10] bx[8:0] bw[10]"),
    ),
    0b01011: (
        1,
        12,
        (8, 8, 8),
        True,
        _fields(
            "rw[9:0] gw[9:0] bw[9:0] rx[7:0] rw[10:11] gx[7:0] gw[10:11] bx[7:0] bw[10:11]"
        ),
    ),
    0b01111: (
        1,
        16,
        (4, 4, 4),
        True,
        _fields(
            "rw[9:0] gw[9:0] bw[9:0] rx[3:0] rw[10:15] gx[3:0] gw[10:15] bx[3:0] bw[10:15]"
        ),
    ),
}

PARTITIONS = [
    0xCCCC,
    0x8888,
    0xEEEE,
    0xECC8,
    0xC880,
    0xFEEC,
    0xFEC8,
    0xEC80,
    0xC800,
    0xFFEC,
    0xFE80,
    0xE800,
    0xFFE8,
    0xFF00,
    0xFFF0,
    0xF000,
    0xF710,
    0x008E,
    0x7100,
    0x08CE,
    0x008C,
    0x7310,
    0x3100,
    0x8CCE,
    0x088C,
    0x3110,
    0x6666,
    0x366C,
    0x17E8,
    0x0FF0,
    0x718E,
    0x399C,
]
ANCHORS = [15] * 17 + [2, 8, 2, 2, 8, 8, 15, 2, 8, 2, 2, 8, 8, 2, 2]
WEIGHTS3 = [0, 9, 18, 27, 37, 46, 55, 64]
WEIGHTS4 = [0, 4, 9, 13, 17, 21, 26, 30, 34, 38, 43, 47, 51, 55, 60, 64]


def _bc6h_block(raw):
    bits = Bits(raw)
    mode = bits.read(2)
    if mode > 1:
        mode |= bits.read(3) << 2
    if mode not in BC6H_MODES:
        return np.zeros((16, 3))
    regions, epb, deltas, transformed, fields = BC6H_MODES[mode]
    ends = [[0, 0, 0] for _ in range(4)]
    for e, c, b, _ in fields:
        ends[e][c] |= bits.read(1) << b
    partition = bits.read(5) if regions == 2 else 0
    for c in range(3):
        for e in range(1, 2 * regions):
            if transformed:
                v, d = ends[e][c], deltas[c]
                if v & (1 << (d - 1)):
                    v -= 1 << d
                ends[e][c] = (ends[0][c] + v) & ((1 << epb) - 1)
    for e in range(2 * regions):
        for c in range(3):
            v = ends[e][c]
            if epb < 15 and v != 0:
                v = 0xFFFF if v == (1 << epb) - 1 else ((v << 16) + 0x8000) >> epb
            ends[e][c] = v
    out = np.zeros((16, 3), np.uint16)
    mask = PARTITIONS[partition] if regions == 2 else 0
    anchor = ANCHORS[partition]
    weights = WEIGHTS3 if regions == 2 else WEIGHTS4
    index_bits = 3 if regions == 2 else 4
    for i in range(16):
        subset = (mask >> i) & 1
        anchored = i == 0 or (regions == 2 and i == anchor)
        w = weights[bits.read(index_bits - 1 if anchored else index_bits)]
        a, b = ends[2 * subset], ends[2 * subset + 1]
        for c in range(3):
            out[i, c] = (((a[c] * (64 - w) + b[c] * w + 32) >> 6) * 31) >> 6
    return out.view(np.float16).astype(np.float64)


def decode_bc6h(raw, w, h):
    image = np.zeros((max(h, 4), max(w, 4), 3))
    k = 0
    for by in range(0, max(h, 4), 4):
        for bx in range(0, max(w, 4), 4):
            image[by : by + 4, bx : bx + 4] = _bc6h_block(raw[k : k + 16]).reshape(
                4, 4, 3
            )
            k += 16
    return image[:h, :w]


def face_directions(n):
    """Directions of the texels of the six faces (D3D cube map order and axes) and their
    solid angles"""
    s = (np.arange(n) + 0.5) / n * 2 - 1
    u, v = np.meshgrid(s, s)
    one = np.ones_like(u)
    faces = [
        (one, -v, -u),
        (-one, -v, u),
        (u, one, v),
        (u, -one, -v),
        (u, -v, one),
        (-u, -v, -one),
    ]
    dirs = []
    for x, y, z in faces:
        d = np.stack([x, y, z], axis=-1)
        dirs.append(d / np.linalg.norm(d, axis=-1, keepdims=True))
    area = (1 + u * u + v * v) ** -1.5
    return dirs, area


def rotation(yaw, pitch, roll):
    """Matrix.CreateFromQuaternion(Quaternion.CreateFromYawPitchRoll(...)) of VRageMath"""
    sr, cr = np.sin(roll / 2), np.cos(roll / 2)
    sp, cp = np.sin(pitch / 2), np.cos(pitch / 2)
    sy, cy = np.sin(yaw / 2), np.cos(yaw / 2)
    x = cy * sp * cr + sy * cp * sr
    y = sy * cp * cr - cy * sp * sr
    z = cy * cp * sr - sy * sp * cr
    w = cy * cp * cr + sy * sp * sr
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w)],
            [2 * (x * y - z * w), 1 - 2 * (z * z + x * x), 2 * (y * z + x * w)],
            [2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (y * y + x * x)],
        ]
    )


def ambient_cube(faces, orientation):
    n = faces[0].shape[0]
    dirs, area = face_directions(n)
    tex = np.concatenate([d.reshape(-1, 3) for d in dirs])
    color = np.concatenate([f.reshape(-1, 3) for f in faces])
    weight = np.tile(area.reshape(-1), 6)

    # The probe sees the sky in world direction V at texture direction t = (VM.x, VM.y,
    # -VM.z), VM = mul(V, background_orientation) (SkyboxColorReflected in EnvAmbient.hlsli),
    # and the matrix goes into the shader untransposed (MyCommon.UpdateFrameConstants), so
    # VM = R V. Back to world: V = R^T (t.x, t.y, -t.z).
    r = rotation(*orientation)
    world = (tex * np.array([1, 1, -1])) @ r

    axes = np.array(
        [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]]
    )
    cube = []
    for axis in axes:
        cos = np.clip(world @ axis, 0, None) * weight
        cube.append((color * cos[:, None]).sum(axis=0) / cos.sum())
    return np.array(cube)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("dds")
    parser.add_argument(
        "--orientation",
        nargs=3,
        type=float,
        default=[60.3955536, -61.1861954, 90.90578],
        metavar=("YAW", "PITCH", "ROLL"),
    )
    args = parser.parse_args()
    cube = ambient_cube(read_faces(args.dds), args.orientation)
    weights = np.array([0.2126, 0.7152, 0.0722])
    luma = cube @ weights
    screen = (cube / luma.mean()) ** SCREEN_POWER
    relative = screen / (screen @ weights).mean()
    for name, c, l in zip(["+X", "-X", "+Y", "-Y", "+Z", "-Z"], cube, luma):
        print(
            f"# {name} linear {c[0]:.5f} {c[1]:.5f} {c[2]:.5f} luma {l:.5f}",
            file=sys.stderr,
        )
    print(" ".join(",".join(f"{v:.3f}" for v in c) for c in relative))


if __name__ == "__main__":
    main()
