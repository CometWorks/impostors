"""Offline check of the safe zone meshes (SE-703): compiles ZoneMesh.cs against the
game's VRage.Math into a throwaway .NET program and asks it for the quads at sizes
on screen up to a camera inside the zone (double.MaxValue), infinity and NaN. Needs
the dotnet SDK and the game client (IMP_BIN64 as for rig.py). Prints one PASS or
FAIL line per size and exits with the number of failures.

    python3 tests/zone_mesh.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# As in rig.py, which needs the test client's packages
STEAM = Path.home() / ".steam/steam/steamapps/common"
if not STEAM.exists():
    STEAM = Path.home() / ".steam/debian-installation/steamapps/common"
BIN64 = Path(os.environ.get("IMP_BIN64", STEAM / "SpaceEngineers/Bin64"))
SOURCE = (
    Path(__file__).resolve().parents[1]
    / "ImpostorsMod/Mod/Data/Scripts/Impostors/Client/ZoneMesh.cs"
)

# Pixel size in C#, and the quads of the sphere expected for it (segments * rings)
CASES = (
    ("0", 8 * 4),
    ("6", 8 * 4),
    ("21", 16 * 8),
    ("29", 32 * 16),
    ("1e30", 32 * 16),
    ("double.MaxValue", 32 * 16),
    ("double.PositiveInfinity", 32 * 16),
    ("double.NaN", None),  # any level, as long as it doesn't throw
)

PROJECT = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>disable</Nullable>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="{SOURCE}" />
    <Reference Include="VRage.Math"><HintPath>{BIN64 / "VRage.Math.dll"}</HintPath></Reference>
  </ItemGroup>
</Project>
"""

PROGRAM = """using System;
foreach (var p in new double[] { %s })
{
    try { Console.WriteLine(Impostors.ZoneMesh.Quads(false, p).Length); }
    catch (Exception e) { Console.WriteLine(e.GetType().Name); }
}

// Stand-ins for what only the box needs, from the rest of the renderer
namespace Impostors
{
    static class Shape { public static void Axes(int f, out int n, out int u, out int v) { n = u = v = 0; } }
    static class ClientShape { public static VRageMath.Vector3[] Normals = null, Axis = null; }
}
""" % ", ".join(c for c, _ in CASES)


def main():
    with tempfile.TemporaryDirectory() as folder:
        (Path(folder) / "ZoneMeshCheck.csproj").write_text(PROJECT)
        (Path(folder) / "Program.cs").write_text(PROGRAM)
        run = subprocess.run(
            ["dotnet", "run", "--project", folder],
            capture_output=True,
            text=True,
        )
    if run.returncode != 0:
        print(run.stdout, run.stderr)
        print("FAILED: build or run")
        sys.exit(1)
    lines = run.stdout.split()
    failures = 0
    for (pixels, expected), got in zip(CASES, lines):
        ok = got.isdigit() and (expected is None or int(got) == expected)
        print(f"{'PASS' if ok else 'FAIL'} sphere at {pixels} px: {got} quads")
        failures += not ok
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


if __name__ == "__main__":
    main()
