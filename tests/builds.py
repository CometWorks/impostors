"""Build check of every plugin leg the loaders compile (SE-1367): the client and server
plugins for net48 the way Pulsar Legacy and Magnetar Legacy compile them, and for net10.0.
Prints one PASS or FAIL line per leg with the compiler errors, and exits with the number
of failures.

    python tests/builds.py

Runs on Linux and Windows. net48 builds against the .NET Framework reference assemblies
(the SDK restores them on Linux) and the game's Bin64, with only the symbols the loaders
define (TRACE, no LOCAL_BUILD), so .NET Core-only APIs fail here instead of at a Legacy
player's start. Needs the game client and, for the server plugin, Magnetar as for any
build (see Directory.Build.props). Deploys nothing and leaves the mod link alone.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ("ClientPlugin/ClientPlugin.csproj", "ServerPlugin/ServerPlugin.csproj")

# Pulsar and Magnetar compile with TRACE only, and the sources declare the assembly
# attributes themselves under !LOCAL_BUILD
LOADER = ["-p:DefineConstants=TRACE", "-p:GenerateAssemblyInfo=false"]


def dotnet(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["dotnet", *args, "-nologo", "-p:RunPostBuildEvent=Never"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def build(project: str, framework: str) -> list[str]:
    props = [f"-p:TargetFramework={framework}", "-p:Configuration=Release"]
    if framework == "net48":
        props += LOADER
        # PluginSdk is netstandard2.0; Linux ships only the Interim launcher's copy
        if sys.platform != "win32":
            props.append("-p:MagnetarLauncher=MagnetarInterim")
    # The assets file holds one framework at a time, so restore for each leg
    result = dotnet("restore", project, *props)
    if result.returncode == 0:
        result = dotnet("build", project, "--no-restore", *props)
    if result.returncode == 0:
        return []
    lines = {line.strip() for line in result.stdout.splitlines() if ": error " in line}
    return sorted(lines) or [result.stdout.strip()[-2000:]]


def main() -> int:
    failures = 0
    for project in PROJECTS:
        for framework in ("net48", "net10.0"):
            errors = build(project, framework)
            print(f"{'FAIL' if errors else 'PASS'} {Path(project).stem} {framework}")
            for error in errors:
                print("  " + error.replace(str(ROOT) + "/", ""))
            failures += bool(errors)
    return failures


if __name__ == "__main__":
    sys.exit(main())
