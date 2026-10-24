"""Summarizes the "Impostor build:" lines of server logs as a Markdown table:
one row per object and level, with the median wall clock and CPU time of
its builds in milliseconds and its quad count. The CPU model is that of the
machine the script runs on, so run it where the logs were written.

    python tests/build_times.py ~/.se-test/imp-zoom-ds0/data/SpaceEngineersDedicated*.log
"""

from __future__ import annotations

import re
import socket
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown CPU"


def parse(paths):
    """(kind, size) -> level -> [(wall, cpu, quads)], and the totals per object"""
    levels = defaultdict(lambda: defaultdict(list))
    totals = defaultdict(list)
    for path in paths:
        for line in Path(path).read_text(errors="replace").splitlines():
            if "Impostor build:" not in line:
                continue
            fields = dict(
                re.findall(r"(\w+)=(\S+)", line.split("Impostor build:", 1)[1])
            )
            key = (
                fields["kind"],
                " ".join(
                    f"{k}={fields[k]}"
                    for k in ("blocks", "voxels", "occlusion", "zone")
                    if k in fields
                ),
            )
            totals[key].append(
                (float(fields["snapshot"]), float(fields["wall"]), float(fields["cpu"]))
            )
            for name, value in fields.items():
                if re.fullmatch(r"L\d+", name):
                    wall, cpu, quads = value.split("/")
                    levels[key][int(name[1:])].append(
                        (float(wall), float(cpu), int(quads))
                    )
    return levels, totals


def main():
    levels, totals = parse(sys.argv[1:])
    if not totals:
        sys.exit("No impostor builds in these logs")
    median = lambda values: statistics.median(values)
    print(f"Machine: {socket.gethostname()}, {cpu_model()}\n")
    print(
        "| Object | Size | Builds | Level | Quads | Wall ms | CPU ms |\n"
        "|---|---|---:|---:|---:|---:|---:|"
    )
    for (kind, size), runs in sorted(totals.items()):
        for level, samples in sorted(levels[(kind, size)].items()):
            print(
                f"| {kind} | {size} | {len(samples)} | {level} "
                f"| {median([s[2] for s in samples])} "
                f"| {median([s[0] for s in samples]):.1f} "
                f"| {median([s[1] for s in samples]):.1f} |"
            )
        print(
            f"| {kind} | {size} | {len(runs)} | all, snapshot "
            f"{median([r[0] for r in runs]):.1f} ms on the game thread | "
            f"| {median([r[1] for r in runs]):.1f} "
            f"| {median([r[2] for r in runs]):.1f} |"
        )


if __name__ == "__main__":
    main()
