"""Offline check of the smooth asteroid normals (SE-710): compiles VoxelShapes and CellVolume
against the game's VRage.Math into a throwaway .NET program (tests/asteroid_normals/Rock.cs),
which builds the shapes of a made-up rock of 256 voxels with flat and with smooth normals.
Needs the dotnet SDK and the game client (IMP_BIN64 as for rig.py).

Prints per level the quads, the bytes and the mean angle between the quads' normals and the
rock's own surface normal, flat against smooth, and the build times. PASS when the smooth
normals are closer to the rock's at every level, the shapes grow by at most a quarter and the
build takes at most half as long again. With pillow installed it also draws both, lit from
three directions, into ~/.se-test/asteroid-normals/rock.png (IMP_OUT for another folder).

    uv run --no-project --with pillow python tests/asteroid_normals.py
"""

from __future__ import annotations

import functools
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STEAM = Path.home() / ".steam/steam/steamapps/common"
if not STEAM.exists():
    STEAM = Path.home() / ".steam/debian-installation/steamapps/common"
BIN64 = Path(os.environ.get("IMP_BIN64", STEAM / "SpaceEngineers/Bin64"))
OUT = Path(os.environ.get("IMP_OUT", Path.home() / ".se-test/asteroid-normals"))
SOURCES = [
    REPO / "tests/asteroid_normals/Rock.cs",
    REPO / "Shared/Impostors/VoxelShapes.cs",
    REPO / "Shared/Impostors/CellVolume.cs",
    REPO / "Shared/Impostors/BuildTiming.cs",
    REPO / "Shared/Impostors/ImpostorEntry.cs",
    REPO / "ImpostorsMod/Mod/Data/Scripts/Impostors/Common/Protocol.cs",
]

PROJECT = """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>disable</Nullable>
    <LangVersion>latest</LangVersion>
    <Optimize>true</Optimize>
    <NoWarn>CS8632</NoWarn>
  </PropertyGroup>
  <ItemGroup>
%s
    <Reference Include="VRage.Math"><HintPath>%s</HintPath></Reference>
    <Reference Include="VRage.Library"><HintPath>%s</HintPath></Reference>
  </ItemGroup>
</Project>
""" % (
    "\n".join(f'    <Compile Include="{s}" />' for s in SOURCES),
    BIN64 / "VRage.Math.dll",
    BIN64 / "VRage.Library.dll",
)

# What ImpostorEntry needs from the rest of the plugin
STUBS = """namespace Shared.Impostors
{
    public class ImpostorRegistry { }
    public class ShapeBuild { }
}
"""

# Smooth shapes may be this much larger and slower to build than flat ones
MAX_GROWTH = 1.25
MAX_SLOWDOWN = 1.5


def build(folder: Path) -> str:
    with tempfile.TemporaryDirectory() as project:
        (Path(project) / "AsteroidNormals.csproj").write_text(PROJECT)
        (Path(project) / "Stubs.cs").write_text(STUBS)
        run = subprocess.run(
            ["dotnet", "run", "-c", "Release", "--project", project, str(folder)],
            capture_output=True,
            text=True,
        )
    if run.returncode != 0:
        print(run.stdout[-4000:], run.stderr[-4000:])
        print("FAILED: build or run")
        sys.exit(1)
    return run.stdout


def quads(shape: bytes):
    """(face, corners, normal) of each quad, in the shape's local frame"""
    cell_size, ox, oy, oz = struct.unpack_from("<4f", shape, 1)
    origin = (ox, oy, oz)
    for o in range(19, len(shape) - 9, 10):
        face, x, y, z, w, h, _color, code = struct.unpack_from("<6B2H", shape, o)
        n = face >> 1
        u, v = (n + 1) % 3, (n + 2) % 3
        base = [x, y, z]
        base[n] += 1 if face % 2 == 0 else 0
        corners = []
        for du, dv in ((0, 0), (w, 0), (w, h), (0, h)):
            c = list(base)
            c[u] += du
            c[v] += dv
            corners.append(tuple(origin[i] + c[i] * cell_size for i in range(3)))
        yield face, corners, decode_normal(code)


def decode_normal(code: int):
    u = (code >> 8) / 62 * 2 - 1
    v = (code & 255) / 62 * 2 - 1
    n = [u, v, 1 - abs(u) - abs(v)]
    if n[2] < 0:
        n[0] = (1 - abs(v)) * (1 if u >= 0 else -1)
        n[1] = (1 - abs(u)) * (1 if v >= 0 else -1)
    length = math.sqrt(sum(c * c for c in n))
    return [c / length for c in n]


def normalize(v):
    length = math.sqrt(sum(c * c for c in v))
    return [c / length for c in v]


# Views and suns of the sheet and the image error: (camera direction, towards the sun)
LIGHTS = [
    ((1, 1, 1), (1, 2, 0.5)),
    ((1, 1, 1), (-1, 1, 1.5)),
    ((0.3, -1, 1), (1, -0.5, 0)),
]

# The rock in Rock.cs, in the storage of 256 voxels, about 184 voxels across: the frame of the
# images is this many voxels wide, and a level is drawn at about one cell per 2.5 pixels, the
# server's estimate for the level it sends
FRAME = 240
ROCK = 184
SUPERSAMPLE = 4


def frame(eye):
    """Unit vectors along the camera's view, right and up for a camera at direction eye"""
    view = normalize([-c for c in eye])
    up = [0, 1, 0] if abs(view[1]) < 0.9 else [1, 0, 0]
    right = normalize(cross(up, view))
    return view, right, cross(view, right)


def cross(a, b):
    return [
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    ]


def lit(normal_dot_sun):
    """The mod's lighting of stone: Ambient + Diffuse * N.L, as a gray value"""
    import numpy as np

    return np.clip(170 * (0.08 + 1.6 * np.maximum(0, normal_dot_sun)), 0, 255)


def render(shape: bytes, eye, sun, pixels: int):
    """Gray image and coverage of the shape's quads, drawn back to front, lit by sun"""
    import numpy as np
    from PIL import Image, ImageDraw

    view, right, up = frame(eye)
    sun = normalize(sun)
    size = pixels * SUPERSAMPLE
    faces = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
    gray = Image.new("F", (size, size), 0)
    cover = Image.new("L", (size, size), 0)
    draw_gray, draw_cover = ImageDraw.Draw(gray), ImageDraw.Draw(cover)
    drawn = []
    for face, corners, normal in quads(shape):
        if sum(a * b for a, b in zip(faces[face], view)) >= 0:
            continue
        depth = sum(sum(c[i] for c in corners) / 4 * view[i] for i in range(3))
        points = [
            (
                size / 2
                + sum((c[i] - 128) * right[i] for i in range(3)) * size / FRAME,
                size / 2 - sum((c[i] - 128) * up[i] for i in range(3)) * size / FRAME,
            )
            for c in corners
        ]
        drawn.append(
            (-depth, points, float(lit(sum(a * b for a, b in zip(normal, sun)))))
        )
    for _, points, value in sorted(drawn, key=lambda d: d[0]):
        draw_gray.polygon(points, fill=value)
        draw_cover.polygon(points, fill=255)
    return downsample(np.asarray(gray)), downsample(
        np.asarray(cover, dtype=np.float32) / 255
    )


def downsample(image):
    n = image.shape[0] // SUPERSAMPLE
    return image.reshape(n, SUPERSAMPLE, n, SUPERSAMPLE).mean(axis=(1, 3))


def distance(p):
    """Rock.Distance for arrays of points, shape (..., 3)"""
    import numpy as np

    center = np.array([128.0, 128.0, 128.0])
    axes = np.array([0.36, 0.3, 0.25]) * 256
    q = (p - center) / axes
    length = np.maximum(np.linalg.norm(q, axis=-1), 1e-6)
    d = q / length[..., None]
    bumps = (
        1
        + 0.14 * np.sin(3 * d[..., 0] + 1) * np.cos(2 * d[..., 1])
        + 0.10 * np.sin(4 * d[..., 2] + 2 * d[..., 0])
        + 0.06 * np.cos(5 * d[..., 1] - 3 * d[..., 2])
        + 0.025 * np.sin(11 * d[..., 0] + 7 * d[..., 2]) * np.sin(9 * d[..., 1] + 1)
    )
    return (length - bumps) * axes.min()


@functools.cache
def truth(eye, sun, pixels: int):
    """Gray image and coverage of the rock itself, ray marched, lit the same way"""
    import numpy as np

    view, right, up = (np.array(v) for v in frame(eye))
    size = pixels * SUPERSAMPLE
    s = (np.arange(size) + 0.5) / size * FRAME - FRAME / 2
    sx, sy = np.meshgrid(s, -s)
    origin = 128 + sx[..., None] * right + sy[..., None] * up - view * 300
    t = np.zeros(sx.shape)
    for _ in range(200):
        d = distance(origin + view * t[..., None])
        t += np.where(d > 0.02, d * 0.9 + 0.01, 0)
    p = origin + view * t[..., None]
    hit = distance(p) < 0.5
    h = 0.5
    normal = np.stack(
        [distance(p + h * e) - distance(p - h * e) for e in np.eye(3)], axis=-1
    )
    normal /= np.maximum(np.linalg.norm(normal, axis=-1, keepdims=True), 1e-9)
    gray = np.where(hit, lit(normal @ np.array(normalize(sun))), 0)
    return downsample(gray), downsample(hit.astype(np.float32))


def image_error(folder: Path, mode: str, lod: int) -> float:
    """RMS gray difference to the ray marched rock where both cover the pixel, over LIGHTS"""
    import numpy as np

    pixels = max(8, round(2.5 * lod * FRAME / ROCK))
    shape = (folder / f"{mode}-{lod}.bin").read_bytes()
    total, count = 0.0, 0
    for eye, sun in LIGHTS:
        gray, cover = render(shape, eye, sun, pixels)
        real, real_cover = truth(eye, sun, pixels)
        both = (cover > 0.99) & (real_cover > 0.99)
        total += float(((gray - real)[both] ** 2).sum())
        count += int(both.sum())
    return math.sqrt(total / max(count, 1))


def sheet(folder: Path, lods: list[int]) -> Path | None:
    from PIL import Image, ImageDraw

    cell = 160
    rows = [
        (mode, eye, sun) for eye, sun in LIGHTS for mode in ("truth", "flat", "smooth")
    ]
    image = Image.new("RGB", (60 + cell * len(lods), 20 + cell * len(rows)), (0, 0, 0))
    draw = ImageDraw.Draw(image)
    for c, lod in enumerate(lods):
        draw.text((60 + c * cell + 4, 4), f"level {lod}", fill=(255, 255, 255))
    for r, (mode, eye, sun) in enumerate(rows):
        draw.text((4, 20 + r * cell + 4), mode, fill=(255, 255, 255))
        for c, lod in enumerate(lods):
            # At the size the level is drawn at, scaled up with nearest neighbor
            pixels = max(8, round(2.5 * lod * FRAME / ROCK))
            if mode == "truth":
                gray, _ = truth(eye, sun, pixels)
            else:
                gray, _ = render(
                    (folder / f"{mode}-{lod}.bin").read_bytes(), eye, sun, pixels
                )
            tile = Image.fromarray(gray.clip(0, 255).astype("uint8")).resize(
                (cell, cell), Image.NEAREST
            )
            image.paste(tile.convert("RGB"), (60 + c * cell, 20 + r * cell))
    path = folder / "rock.png"
    image.save(path)
    return path


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    output = build(OUT)
    times = dict(re.findall(r"build (\w+) ms=([\d.]+)", output))
    levels = {}
    for mode, lod, q, b, err in re.findall(
        r"level (\w+) lod=(\d+) quads=(\d+) bytes=(\d+) error=([\d.]+)", output
    ):
        levels.setdefault(int(lod), {})[mode] = (int(q), int(b), float(err))

    failures = 0
    print(
        "| Level | Quads flat | Quads smooth | Bytes flat | Bytes smooth "
        "| Normal error flat | smooth | Image error flat | smooth |"
    )
    print("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for lod in sorted(levels):
        (qf, bf, ef), (qs, bs, es) = levels[lod]["flat"], levels[lod]["smooth"]
        image_flat = image_error(OUT, "flat", lod)
        image_smooth = image_error(OUT, "smooth", lod)
        print(
            f"| {lod} | {qf} | {qs} | {bf} | {bs} | {ef:.1f}° | {es:.1f}° "
            f"| {image_flat:.1f} | {image_smooth:.1f} |"
        )
        if image_smooth >= image_flat:
            print(f"FAIL level {lod}: smooth looks less like the rock than flat")
            failures += 1
    bytes_flat = sum(levels[l]["flat"][1] for l in levels)
    bytes_smooth = sum(levels[l]["smooth"][1] for l in levels)
    flat_ms, smooth_ms = float(times["flat"]), float(times["smooth"])
    print(
        f"All levels: {bytes_flat} bytes flat, {bytes_smooth} smooth ({bytes_smooth / bytes_flat:.2f}x)"
    )
    print(
        f"Build: {flat_ms:.1f} ms flat, {smooth_ms:.1f} ms smooth ({smooth_ms / flat_ms:.2f}x)"
    )
    if bytes_smooth > bytes_flat * MAX_GROWTH:
        print(f"FAIL shapes grew more than {MAX_GROWTH}x")
        failures += 1
    if smooth_ms > flat_ms * MAX_SLOWDOWN:
        print(f"FAIL build took more than {MAX_SLOWDOWN}x as long")
        failures += 1
    if not os.environ.get("IMP_NO_SHEET"):
        print(f"Sheet: {sheet(OUT, sorted(levels))}")
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


if __name__ == "__main__":
    main()
