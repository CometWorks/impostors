"""How large and how bright each level of an asteroid's impostor looks next to the real
asteroid (SE-722), offline. Shapes and voxels are drawn orthographic from the 6 directions
along the axes and lit by the mod's lighting (Ambient + Diffuse * N.L) from the 8 cube
corners. Per asteroid and level: the area it covers, the mean brightness over it, and their
product, the brightness of the whole asteroid when it is a few pixels across; each over the
reference's, averaged over the views and suns.

    IMP_TASK=se722 uv run --no-project --with httpx --with pillow --with numpy \\
        python tests/asteroid_levels.py dump        # the voxels of every 20th asteroid
    uv run --no-project --with numpy python tests/asteroid_levels.py voxels [FOLDER]
    uv run --no-project --with numpy python tests/asteroid_levels.py pack A.bin [B.bin]

`dump` loads "Octahedron - Seed 2" (see tests/ring.py) on the server of the rig, whose test
probe writes the content of every IMP_VOXEL_DUMP_EVERY-th asteroid (20) at the voxel levels
of detail from 128 voxels across up into ~/.se-test/<task>-voxels.

`voxels` builds the shapes of the dumped asteroids with this working copy's VoxelShapes
(tests/asteroid_levels/Levels.cs, compiled against the game's VRage.Math like
tests/asteroid_normals.py; IMP_BIN64 as for rig.py) and compares every level with the
voxels themselves, drawn at the finest level of detail of the dump: the real asteroid's
surface at 4 m for a 512 m storage. FOLDER defaults to ~/.se-test/se722-voxels.

`pack` compares the levels of the asteroids in an asteroid pack with the pack's finest
level instead, for packs built before (no voxels needed). With two packs it also counts the
asteroids whose area ratio of a level changed by more than 0.15 between them.

Prints a table per level: the median, 10th and 90th percentile of the area ratio, the share
of asteroids under 0.85 of the reference's area, and the medians of the brightness ratios.
IMP_ASTEROIDS (ids, comma separated) adds a line per asteroid, IMP_SAMPLE=n takes n
asteroids only. PASS when every level from 8 up has a median area ratio between 0.85 and
1.25 and a median brightness (area times mean) ratio of at least 0.8.
"""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
AMBIENT, DIFFUSE = 0.08, 1.6
MIN_AREA, MAX_AREA, MIN_BRIGHTNESS = 0.85, 1.25, 0.8
# The rock's color, as the shapes have it: 170 gray, quantized to RGB565
STONE = (170 >> 3 << 11) | (170 >> 2 << 5) | (170 >> 3)
# Directions towards the sun: the cube's corners
SUNS = np.array(
    [(x, y, z) for x in (1, -1) for y in (1, -1) for z in (1, -1)], dtype=np.float32
) / np.sqrt(3)


def read_pack(path: Path):
    """Per asteroid id: {level: shape bytes}"""
    data = path.read_bytes()
    _magic, fmt = struct.unpack_from("<iB", data, 0)
    o = 5
    o += 1 + data[o]  # the world name, shorter than 128 bytes
    if fmt >= 2:
        o += 8
    (count,) = struct.unpack_from("<i", data, o)
    o += 4
    index = []
    for _ in range(count):
        asteroid, _content, _version, _flags, levels = struct.unpack_from(
            "<qqiBB", data, o
        )
        o += 22
        lengths = [struct.unpack_from("<Bi", data, o + 5 * k) for k in range(levels)]
        o += 5 * levels
        index.append((asteroid, lengths))
    shapes = {}
    for asteroid, lengths in index:
        shapes[asteroid] = {}
        for lod, length in lengths:
            shapes[asteroid][lod] = data[o : o + length]
            o += length
    return shapes


def decode_normals(codes):
    u = (codes >> 8) / 62 * 2 - 1
    v = (codes & 255) / 62 * 2 - 1
    n = np.stack([u, v, 1 - np.abs(u) - np.abs(v)], axis=-1)
    neg = n[:, 2] < 0
    nu, nv = n[neg, 0].copy(), n[neg, 1].copy()
    n[neg, 0] = (1 - np.abs(nv)) * np.where(nu >= 0, 1, -1)
    n[neg, 1] = (1 - np.abs(nu)) * np.where(nv >= 0, 1, -1)
    return n / np.linalg.norm(n, axis=-1, keepdims=True)


def gray(colors):
    """Luminance 0..1 of RGB565 colors"""
    r = (colors >> 11) & 31
    g = (colors >> 5) & 63
    b = colors & 31
    return (0.2126 * r / 31 + 0.7152 * g / 63 + 0.0722 * b / 31).astype(np.float32)


def lighting(albedo, normals):
    """Brightness per sun, (count, suns)"""
    return albedo[:, None] * (AMBIENT + DIFFUSE * np.maximum(0, normals @ SUNS.T))


def quads(shape: bytes):
    (count,) = struct.unpack_from("<H", shape, 17)
    return np.frombuffer(shape, dtype=np.uint8, count=count * 10, offset=19).reshape(
        -1, 10
    )


def bounds(shape: bytes):
    """The corner and the edge length of the box the shape's cells fill, meters"""
    cell, ox, oy, oz = struct.unpack_from("<4f", shape, 1)
    q = quads(shape)
    far = (q[:, 1:4].astype(np.float32) + np.maximum(q[:, 4:5], q[:, 5:6]) + 1).max()
    return np.array([ox, oy, oz]), float(far * cell)


def draw_shape(shape: bytes, corner, scale: float, size: int):
    """Per view (from +X, -X, +Y, -Y, +Z, -Z): coverage and brightness per sun, (suns, h, w).
    corner is the drawing's origin, scale its pixels per meter."""
    cell, ox, oy, oz = struct.unpack_from("<4f", shape, 1)
    q = quads(shape)
    face = q[:, 0].astype(int)
    xyz = q[:, 1:4].astype(np.float32)
    w, h = q[:, 4].astype(np.float32), q[:, 5].astype(np.float32)
    color = q[:, 6].astype(np.uint32) | (q[:, 7].astype(np.uint32) << 8)
    code = q[:, 8].astype(np.uint32) | (q[:, 9].astype(np.uint32) << 8)
    light = lighting(gray(color), decode_normals(code))
    origin = np.array([ox, oy, oz]) - corner
    result = []
    for f in range(6):
        n = f >> 1
        u, v = (n + 1) % 3, (n + 2) % 3
        mask = np.zeros((size, size), bool)
        depth = np.full((size, size), -np.inf, np.float32)
        lit = np.zeros((len(SUNS), size, size), np.float32)
        sign = 1 if f % 2 == 0 else -1
        for k in np.nonzero(face == f)[0]:
            a0 = int(round((origin[u] + xyz[k, u] * cell) * scale))
            b0 = int(round((origin[v] + xyz[k, v] * cell) * scale))
            a1 = int(round((origin[u] + (xyz[k, u] + w[k]) * cell) * scale))
            b1 = int(round((origin[v] + (xyz[k, v] + h[k]) * cell) * scale))
            a0, b0 = max(a0, 0), max(b0, 0)
            d = sign * (origin[n] + (xyz[k, n] + (f % 2 == 0)) * cell)
            region = depth[b0:b1, a0:a1]
            front = region < d
            region[front] = d
            mask[b0:b1, a0:a1] |= front
            lit[:, b0:b1, a0:a1][:, front] = light[k][:, None]
        result.append((mask, lit))
    return result


def draw_voxels(content: np.ndarray):
    """The same views of voxel content (z, y, x), one pixel per voxel: filled above the iso
    level, lit with the normal down the content's gradient at the front voxel"""
    filled = content > 127
    field = content.astype(np.float32)
    gz, gy, gx = np.gradient(field)
    normals = -np.stack([gx, gy, gz], axis=-1)
    albedo = gray(np.array([STONE], np.uint32))[0]
    result = []
    for f in range(6):
        n = f >> 1
        u, v = (n + 1) % 3, (n + 2) % 3
        # Array axes of x, y, z are 2, 1, 0; the image is (v, u)
        occupancy = np.transpose(filled, (2 - n, 2 - v, 2 - u))
        along = occupancy.shape[0]
        mask = occupancy.any(axis=0)
        if f % 2 == 0:
            front = along - 1 - np.argmax(occupancy[::-1], axis=0)
        else:
            front = np.argmax(occupancy, axis=0)
        index = [None, None, None]
        vv, uu = np.nonzero(mask)
        index[n], index[u], index[v] = front[vv, uu], uu, vv
        normal = normals[index[2], index[1], index[0]]
        length = np.linalg.norm(normal, axis=-1, keepdims=True)
        normal = np.where(length > 1e-6, normal / np.maximum(length, 1e-6), 0)
        lit = np.zeros((len(SUNS),) + mask.shape, np.float32)
        lit[:, vv, uu] = lighting(np.full(len(vv), albedo), normal).T
        result.append((mask, lit))
    return result


def ratios(views, reference) -> tuple[float, float, float]:
    """(area, mean brightness, area x mean) over the reference's, view means"""
    area, mean, total = [], [], []
    for (mask, lit), (rmask, rlit) in zip(views, reference):
        a, ra = mask.sum(), rmask.sum()
        if ra == 0:
            continue
        area.append(a / ra)
        m = lit[:, mask].mean() if a else 0.0
        mean.append(m / max(rlit[:, rmask].mean(), 1e-9))
        total.append(lit[:, mask].sum() / max(rlit[:, rmask].sum(), 1e-9))
    return float(np.mean(area)), float(np.mean(mean)), float(np.mean(total))


def measure_pack_entry(levels: dict[int, bytes]) -> dict[int, tuple]:
    levels = {lod: s for lod, s in levels.items() if len(s) > 19}
    if not levels:
        return {}
    finest = max(levels)
    corner, extent = bounds(levels[finest])
    for s in levels.values():
        c, e = bounds(s)
        corner = np.minimum(corner, c)
        extent = max(extent, e + float(np.max(c - corner)))
    drawn = {lod: draw_shape(s, corner, 256 / extent, 258) for lod, s in levels.items()}
    return {lod: ratios(vs, drawn[finest]) for lod, vs in drawn.items()}


def read_dump(path: Path):
    """Storage size and the finest content of a probe dump, (z, y, x), with its voxel size"""
    data = path.read_bytes()
    sx, sy, sz, _count, lod = struct.unpack_from("<5i", data, 0)
    shape = tuple(max(1, s >> lod) for s in (sz, sy, sx))
    content = np.frombuffer(data, np.uint8, count=int(np.prod(shape)), offset=20)
    return np.array([sx, sy, sz]), content.reshape(shape), 1 << lod


def build_shapes(dumps: Path, out: Path) -> None:
    steam = Path.home() / ".steam/steam/steamapps/common"
    if not steam.exists():
        steam = Path.home() / ".steam/debian-installation/steamapps/common"
    bin64 = Path(os.environ.get("IMP_BIN64", steam / "SpaceEngineers/Bin64"))
    sources = [
        REPO / "tests/asteroid_levels/Levels.cs",
        REPO / "Shared/Impostors/VoxelShapes.cs",
        REPO / "Shared/Impostors/CellVolume.cs",
        REPO / "Shared/Impostors/BuildTiming.cs",
        REPO / "Shared/Impostors/ImpostorEntry.cs",
        REPO / "ImpostorsMod/Mod/Data/Scripts/Impostors/Common/Protocol.cs",
    ]
    project = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>disable</Nullable>
    <LangVersion>latest</LangVersion>
    <Optimize>true</Optimize>
    <NoWarn>CS8632</NoWarn>
  </PropertyGroup>
  <ItemGroup>
{chr(10).join(f'    <Compile Include="{s}" />' for s in sources)}
    <Reference Include="VRage.Math"><HintPath>{bin64 / "VRage.Math.dll"}</HintPath></Reference>
    <Reference Include="VRage.Library"><HintPath>{bin64 / "VRage.Library.dll"}</HintPath></Reference>
  </ItemGroup>
</Project>
"""
    stubs = "namespace Shared.Impostors { public class ImpostorRegistry { } public class ShapeBuild { } }"
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "AsteroidLevels.csproj").write_text(project)
        (Path(folder) / "Stubs.cs").write_text(stubs)
        run = subprocess.run(
            ["dotnet", "run", "-c", "Release", "--project", folder, str(dumps), str(out)],
            capture_output=True,
            text=True,
        )
    if run.returncode != 0:
        print(run.stdout[-4000:], run.stderr[-4000:])
        raise SystemExit("FAILED: build or run of the shapes")


def measure_voxels(dump: Path, shapes: Path) -> dict[int, tuple]:
    size, content, voxel = read_dump(dump)
    reference = draw_voxels(content)
    pixels = reference[0][0].shape[0]
    # Voxel p of the dump is the sample at p * voxel (MyVoxelCoordSystems): its pixel spans
    # half a voxel to either side
    corner = np.full(3, -voxel / 2)
    out = {}
    for path in shapes.glob(f"{dump.stem}-*.bin"):
        lod = int(path.stem.rsplit("-", 1)[1])
        shape = path.read_bytes()
        if len(shape) <= 19:
            continue
        drawn = draw_shape(shape, corner, 1 / voxel, pixels)
        out[lod] = ratios(drawn, reference)
    return out


def table(name: str, results: dict[int, dict[int, tuple]]) -> dict[int, tuple]:
    print(f"\n{name}: {len(results)} asteroids")
    print(
        "| Level | Area median | p10 | p90 | Under 0.85 | Mean brightness median "
        "| Brightness (area x mean) median | p10 |"
    )
    print("|---:|---:|---:|---:|---:|---:|---:|---:|")
    medians = {}
    for lod in sorted({lod for r in results.values() for lod in r}):
        rows = np.array([r[lod] for r in results.values() if lod in r])
        area, mean, total = rows[:, 0], rows[:, 1], rows[:, 2]
        medians[lod] = (float(np.median(area)), float(np.median(total)))
        print(
            f"| {lod} | {np.median(area):.3f} | {np.percentile(area, 10):.3f} "
            f"| {np.percentile(area, 90):.3f} | {(area < MIN_AREA).mean() * 100:.1f}% "
            f"| {np.median(mean):.3f} | {np.median(total):.3f} | {np.percentile(total, 10):.3f} |"
        )
    picked = [int(i) for i in os.environ.get("IMP_ASTEROIDS", "").split(",") if i]
    for i in picked:
        if i in results:
            print(
                f"asteroid {i}: "
                + ", ".join(
                    f"level {lod} area {a:.2f} brightness {t:.2f}"
                    for lod, (a, _, t) in sorted(results[i].items())
                )
            )
    return medians


def verdict(medians: dict[int, tuple]) -> int:
    failures = 0
    for lod, (area, total) in sorted(medians.items()):
        if lod >= 8 and not (MIN_AREA <= area <= MAX_AREA and total >= MIN_BRIGHTNESS):
            print(f"FAIL level {lod}: area {area:.3f}, brightness {total:.3f}")
            failures += 1
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    return failures


def sample(ids):
    n = int(os.environ.get("IMP_SAMPLE", "0"))
    ids = sorted(ids)
    return ids[:: max(1, len(ids) // n)] if n else ids


def pack_mode(paths: list[Path]) -> int:
    all_results = []
    for path in paths:
        shapes = read_pack(path)
        results = {i: measure_pack_entry(shapes[i]) for i in sample(shapes)}
        all_results.append(results)
        medians = table(path.as_posix(), results)
    if len(all_results) == 2:
        a, b = all_results
        for lod in sorted({lod for r in b.values() for lod in r}):
            changes = [
                b[i][lod][0] - a[i][lod][0]
                for i in a.keys() & b.keys()
                if lod in a[i] and lod in b[i]
            ]
            print(
                f"level {lod}: area ratio up by more than 0.15 for "
                f"{sum(d > 0.15 for d in changes)}, down for {sum(d < -0.15 for d in changes)}"
                f" of {len(changes)}"
            )
    return verdict(medians)


def voxels_mode(folder: Path) -> int:
    dumps = {int(p.stem): p for p in folder.glob("*.vxd")}
    if not dumps:
        raise SystemExit(f"No voxel dumps in {folder}, run dump first")
    with tempfile.TemporaryDirectory() as out:
        start = time.monotonic()
        build_shapes(folder, Path(out))
        print(f"Built the shapes of {len(dumps)} asteroids in {time.monotonic() - start:.0f} s")
        results = {i: measure_voxels(dumps[i], Path(out)) for i in sample(dumps)}
    return verdict(table(f"{folder} against the voxels", results))


def dump_mode() -> int:
    sys.path.insert(0, str(REPO / "tests"))
    import asteroid_pack  # noqa: E402  (claims the rig's slot)
    import rig  # noqa: E402

    folder = rig.TEST_ROOT / f"{rig.TASK}-voxels"
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.vxd"):
        old.unlink()
    os.environ["IMP_VOXEL_DUMP"] = str(folder)
    asteroid_pack.setup_world(False)
    server = asteroid_pack.SERVER
    server.write_plugin_config(rig.plugin_config(Grids=False, SafeZones=False))
    server.start(timeout=3600)
    try:
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline:
            m = re.search(r"voxel dump of .*", asteroid_pack.log_text())
            if m:
                print(f"PASS {m.group(0)}")
                return 0
            time.sleep(5)
        print("FAIL no voxel dump in the server log")
        return 1
    finally:
        server.stop()


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "pack" and len(sys.argv) > 2:
        return pack_mode([Path(p) for p in sys.argv[2:]])
    if mode == "voxels":
        folder = sys.argv[2] if len(sys.argv) > 2 else Path.home() / ".se-test/se722-voxels"
        return voxels_mode(Path(folder))
    if mode == "dump":
        return dump_mode()
    raise SystemExit(__doc__)


if __name__ == "__main__":
    sys.exit(main())
