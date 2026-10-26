"""Offline check of the server's shape cache retention (SE-711): compiles
DiskShapeCache (Shared/Impostors/ShapeCache.cs) into a throwaway .NET program
and runs it against cache folders of fake entries. No game needed, only the
dotnet SDK. Linux only, for /proc/self/io.

- retention: entries unused for CacheDays, built more than CacheBuildDays ago
  and the least recently used ones over the size cap go; recent ones, old
  builds used lately (uses.bin) and the files that are no cache entries (the
  WA's index.bin, other names, the asteroid pack folder next to the cache)
  stay. A sweep deletes no more files than its limit. A read leaves the file's
  write time alone and its last use survives a restart through uses.bin.
- thread: the background sweep of a cache opened with CacheSweepMinutes 1
  comes about two minutes after the open (give or take a quarter), on its own.
  Skipped with IMP_QUICK=1.
- cost: 50000 sparse files of 4 KB, 500 of them unused for 40 days, measured:
  CPU and I/O of the open, of a sweep that deletes 200 files, of one that
  finds nothing and of saving uses.bin.

Prints one PASS or FAIL line per check, the measurements, and exits with the
number of failures.

    python3 tests/cache_retention.py
    IMP_RETENTION_DIR=<folder>   # where the fake caches go (a temporary folder)
"""

from __future__ import annotations

import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHARED = REPO / "Shared"
DAY = 86400
TICKS_EPOCH = 621355968000000000  # DateTime ticks of 1970-01-01
USES_MAGIC = 0x31554D49

PROJECT = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <OutputType>Exe</OutputType>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>disable</Nullable>
    <LangVersion>14</LangVersion>
  </PropertyGroup>
  <ItemGroup>
    <Compile Include="{SHARED / "Impostors/ShapeCache.cs"}" />
    <Compile Include="{SHARED / "Logging/IPluginLogger.cs"}" />
    <Compile Include="{SHARED / "Config/IPluginConfig.cs"}" />
    <Compile Include="{SHARED / "Config/PluginConfig.cs"}" />
  </ItemGroup>
</Project>
"""

# The cache folder, then commands, one result line each
PROGRAM = r"""
using System;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Threading;
using Shared.Config;
using Shared.Impostors;

var log = new Log();
DiskShapeCache cache = null;
var a = args;
for (var i = 1; i < a.Length; )
{
    var command = a[i++];
    var before = Measure.Take();
    switch (command)
    {
        case "open":
            var config = new PluginConfig
            {
                CacheMegabytes = int.Parse(a[i]),
                CacheDays = int.Parse(a[i + 1]),
                CacheBuildDays = int.Parse(a[i + 2]),
                CacheSweepMinutes = int.Parse(a[i + 3]),
            };
            cache = DiskShapeCache.Get(a[0], config, log);
            i += 4;
            Console.WriteLine($"open {cache.Count} {cache.Bytes} {Measure.Take() - before}");
            break;
        case "sweep":
            var deleted = cache.Sweep(int.Parse(a[i++]), TimeSpan.FromMilliseconds(double.Parse(a[i++], CultureInfo.InvariantCulture)));
            Console.WriteLine($"sweep {deleted} {cache.Count} {cache.Bytes} {Measure.Take() - before}");
            break;
        case "put":
            var id = long.Parse(a[i++], NumberStyles.HexNumber);
            cache.Put(id, 1, 2, new[] { new byte[4096] });
            Console.WriteLine($"put {id:x16}");
            break;
        case "read":
            var hit = cache.Read(long.Parse(a[i++], NumberStyles.HexNumber)) != null;
            Console.WriteLine($"read {(hit ? "hit" : "miss")}");
            break;
        case "save":
            cache.SaveUses();
            Console.WriteLine($"save {Measure.Take() - before}");
            break;
        case "waitswept":
            // Until a sweep logged, up to the seconds given
            var watch = Stopwatch.StartNew();
            var limit = double.Parse(a[i++], CultureInfo.InvariantCulture);
            while (log.Swept == 0 && watch.Elapsed.TotalSeconds < limit)
                Thread.Sleep(250);
            Console.WriteLine($"waitswept {log.Swept} {watch.Elapsed.TotalSeconds:0.0} {cache.Count}");
            break;
        default:
            throw new ArgumentException(command);
    }
}

// CPU and wall time, and the process's I/O counters
readonly record struct Measure(double Cpu, double Wall, long Rchar, long Wchar, long Syscr, long Syscw, long ReadBytes, long WriteBytes)
{
    static readonly Stopwatch Clock = Stopwatch.StartNew();

    public static Measure Take()
    {
        var p = Process.GetCurrentProcess();
        var io = File.ReadAllLines("/proc/self/io").Select(l => l.Split(':')).ToDictionary(s => s[0], s => long.Parse(s[1].Trim()));
        return new Measure(p.TotalProcessorTime.TotalMilliseconds, Clock.Elapsed.TotalMilliseconds, io["rchar"], io["wchar"], io["syscr"], io["syscw"], io["read_bytes"], io["write_bytes"]);
    }

    public static string operator -(Measure b, Measure a) =>
        string.Join(" ", new object[] { (b.Cpu - a.Cpu).ToString("0.0", CultureInfo.InvariantCulture), (b.Wall - a.Wall).ToString("0.0", CultureInfo.InvariantCulture), b.Rchar - a.Rchar, b.Wchar - a.Wchar, b.Syscr - a.Syscr, b.Syscw - a.Syscw, b.ReadBytes - a.ReadBytes, b.WriteBytes - a.WriteBytes });
}

class Log : Shared.Logging.IPluginLogger
{
    public volatile int Swept;
    public bool IsTraceEnabled => false;
    public bool IsDebugEnabled => true;
    public bool IsInfoEnabled => true;
    public bool IsWarningEnabled => true;
    public bool IsErrorEnabled => true;
    public bool IsCriticalEnabled => true;
    void Write(string level, string message, Exception ex = null)
    {
        if (message.Contains("swept"))
            Swept++;
        Console.Error.WriteLine($"{DateTime.UtcNow:HH:mm:ss.fff} {level} {message} {ex}");
    }
    public void Trace(Exception ex, string message, params object[] data) => Write("TRACE", message, ex);
    public void Debug(Exception ex, string message, params object[] data) => Write("DEBUG", message, ex);
    public void Info(Exception ex, string message, params object[] data) => Write("INFO", message, ex);
    public void Warning(Exception ex, string message, params object[] data) => Write("WARN", message, ex);
    public void Error(Exception ex, string message, params object[] data) => Write("ERROR", message, ex);
    public void Critical(Exception ex, string message, params object[] data) => Write("CRIT", message, ex);
    public void Trace(string message, params object[] data) => Write("TRACE", message);
    public void Debug(string message, params object[] data) => Write("DEBUG", message);
    public void Info(string message, params object[] data) => Write("INFO", message);
    public void Warning(string message, params object[] data) => Write("WARN", message);
    public void Error(string message, params object[] data) => Write("ERROR", message);
    public void Critical(string message, params object[] data) => Write("CRIT", message);
}

// Stand-ins for what the cache needs from the rest of the plugin
namespace Impostors { public enum ImpostorKind { Grid } }
namespace Shared.Impostors { static class ImpostorEntry { public static int Hash(byte[][] shapes) => 0; } }
"""

failures = 0
program: Path


def check(name: str, ok: bool, detail="") -> None:
    global failures
    print(
        f"{'PASS' if ok else 'FAIL'} {name}" + (f": {detail}" if detail != "" else "")
    )
    failures += not ok


def run(*args) -> list[list[str]]:
    """The harness's result lines, split"""
    done = subprocess.run(
        [str(program), *map(str, args)], capture_output=True, text=True, timeout=600
    )
    if done.returncode != 0:
        print(done.stdout, done.stderr)
        raise SystemExit("FAILED: harness")
    return [line.split() for line in done.stdout.splitlines()]


def path_of(cache: Path, entity: int) -> Path:
    name = f"{entity:016x}"
    return cache / name[:2] / f"{name}.shapes"


def fake(cache: Path, entity: int, size: int, built_days_ago: float) -> Path:
    """A sparse file of the size, written that many days ago"""
    path = path_of(cache, entity)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    t = time.time() - built_days_ago * DAY
    os.utime(path, (t, t))
    return path


def write_uses(cache: Path, uses: dict[int, float]) -> None:
    """uses.bin: the last use of these entries, days ago"""
    now = time.time()
    body = struct.pack("<ii", USES_MAGIC, len(uses))
    for entity, days in uses.items():
        ticks = TICKS_EPOCH + int((now - days * DAY) * 10_000_000)
        body += struct.pack("<qq", entity, ticks)
    (cache / "uses.bin").write_bytes(body)


def read_uses(cache: Path) -> dict[int, float]:
    raw = (cache / "uses.bin").read_bytes()
    magic, count = struct.unpack_from("<ii", raw)
    assert magic == USES_MAGIC
    result = {}
    for i in range(count):
        entity, ticks = struct.unpack_from("<qq", raw, 8 + 16 * i)
        result[entity] = (time.time() - (ticks - TICKS_EPOCH) / 10_000_000) / DAY
    return result


def retention(root: Path) -> None:
    cache = root / "retention" / "Cache"
    pack = root / "retention" / "AsteroidPack" / "world"
    shutil.rmtree(cache.parent, ignore_errors=True)
    ids = random.Random(711)
    groups = {
        # name: (count, size, built days ago, used days ago or None, should stay)
        "recent": (20, 4096, 0.1, None, True),
        "unused 40 days": (20, 4096, 40, None, False),
        "built 40 days ago, used yesterday": (20, 4096, 40, 1, True),
        "built 100 days ago, used today": (20, 4096, 100, 0.01, False),
    }
    files: dict[str, list[tuple[int, Path]]] = {}
    uses = {}
    for g, (name, (count, size, built, used, _)) in enumerate(groups.items()):
        files[name] = []
        for _ in range(count):
            entity = ids.getrandbits(63)
            files[name].append((entity, fake(cache, entity, size, built)))
            if used is not None:
                uses[entity] = used
    # 40 files of 100 KB used 1 to 20 days ago, 4 MB against a cap of 2 MB: the
    # least recently used go until the rest fits
    filler = []
    for i in range(40):
        entity = ids.getrandbits(63)
        filler.append((entity, fake(cache, entity, 100 * 1024, 1 + i * 0.5)))
    write_uses(cache, uses)
    others = [
        cache / "index.bin",
        cache / "ab" / "notes.txt",
        cache / "zz" / "nohex.shapes",
    ]
    pack.mkdir(parents=True)
    others.append(pack / "1234567890abcdef.shapes")
    for p in others:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"not a cache entry")
    old = time.time() - 200 * DAY
    for p in others:
        os.utime(p, (old, old))

    lines = run(
        cache,
        "open",
        2,
        30,
        90,
        0,
        "sweep",
        10,
        0,
        "sweep",
        1000,
        0,
        "sweep",
        1000,
        0,
        "save",
    )
    first, second, third = lines[1], lines[2], lines[3]
    check(
        "a sweep deletes no more than its limit",
        first[1] == "10",
        f"first pass {first[1]}",
    )
    check(
        "the next sweep finds nothing more", third[1] == "0", f"third pass {third[1]}"
    )
    for name, (_, _, _, _, stays) in groups.items():
        kept = sum(p.exists() for _, p in files[name])
        check(
            f"{name}: {'stay' if stays else 'go'}",
            kept == (len(files[name]) if stays else 0),
            f"{kept} of {len(files[name])} left",
        )
    left = [p.exists() for _, p in filler]
    total = sum(p.stat().st_size for p in cache.glob("*/*.shapes"))
    dropped = left.count(False)
    check(
        "over the cap: the least recently used go, until it fits",
        0 < dropped < len(filler)
        and all(not x for x in left[len(filler) - dropped :])
        and all(left[: len(filler) - dropped])
        and total <= 2 * 1024 * 1024,
        f"{dropped} of {len(filler)} dropped, {total // 1024} KB left",
    )
    check("files that are no cache entries stay", all(p.exists() for p in others))

    # A read is a use, kept in uses.bin and not in the file's write time
    a, b = ids.getrandbits(63), ids.getrandbits(63)
    run(cache, "open", 64, 30, 90, 0, "put", f"{a:x}", "put", f"{b:x}")
    t = time.time() - 40 * DAY
    for e in (a, b):
        os.utime(path_of(cache, e), (t, t))
    lines = run(cache, "open", 64, 30, 90, 0, "read", f"{a:x}", "save")
    check("read: hit", lines[1] == ["read", "hit"])
    check("read: its last use in uses.bin", read_uses(cache).get(a, 99) < 0.01)
    check(
        "read: the file's write time stays its build time",
        abs(path_of(cache, a).stat().st_mtime - t) < 2,
    )
    lines = run(cache, "open", 64, 30, 90, 0, "sweep", 1000, 0)
    check(
        "a read 40 days after the build counts after a restart",
        path_of(cache, a).exists() and not path_of(cache, b).exists(),
        f"read one {path_of(cache, a).exists()}, unread one {path_of(cache, b).exists()}",
    )

    # A day of 0 turns that limit off
    c = ids.getrandbits(63)
    fake(cache, c, 4096, 400)
    run(cache, "open", 64, 0, 0, 0, "sweep", 1000, 0)
    check("CacheDays and CacheBuildDays 0: no age limit", path_of(cache, c).exists())


def thread(root: Path) -> None:
    cache = root / "thread" / "Cache"
    shutil.rmtree(cache.parent, ignore_errors=True)
    for i in range(5):
        fake(cache, 0x2200000000000000 + i, 4096, 40)
    lines = run(cache, "open", 64, 30, 90, 1, "waitswept", 240)
    swept, seconds, count = lines[1][1], float(lines[1][2]), lines[1][3]
    check(
        "background sweep about 2 minutes after the open",
        swept == "1" and 85 <= seconds <= 155 and count == "0",
        f"{swept} sweep after {seconds:.0f} s, {count} entries left",
    )


def cost(root: Path) -> None:
    cache = root / "cost" / "Cache"
    shutil.rmtree(cache.parent, ignore_errors=True)
    count, expired = 50_000, 500
    uses = {}
    ids = random.Random(7110)
    entities = [ids.getrandbits(63) for _ in range(count)]
    for i, entity in enumerate(entities):
        fake(cache, entity, 4096, 40 if i % (count // expired) == 0 else 1)
        if i % (count // expired) != 0:
            uses[entity] = 0.5
    write_uses(cache, uses)
    os.sync()
    lines = run(
        cache,
        "open",
        1024,
        30,
        90,
        0,
        "sweep",
        200,
        5,
        "sweep",
        200,
        5,
        "sweep",
        200,
        5,
        "sweep",
        200,
        5,
        "sweep",
        200,
        5,
        "put",
        "7110",
        "read",
        "7110",
        "save",
    )
    names = (
        "CPU ms",
        "wall ms",
        "rchar",
        "wchar",
        "syscr",
        "syscw",
        "read_bytes",
        "write_bytes",
    )

    def show(label, values):
        print(f"  {label}: " + ", ".join(f"{n} {v}" for n, v in zip(names, values)))

    open_ = lines[0][3:]
    passes = [line for line in lines if line[0] == "sweep"]
    deleting, idle = passes[1], passes[4]
    save = lines[-1][1:]
    print(f"cost, {count} files, {expired} unused for 40 days:")
    show("open (scan)", open_)
    show(f"sweep deleting {deleting[1]}", deleting[4:])
    show("sweep with nothing to do", idle[4:])
    show("save uses.bin", save)
    deleted = sum(int(p[1]) for p in passes)
    check(
        "cost: the unused ones went, 200 per sweep",
        deleted == expired,
        f"{deleted} deleted",
    )
    # Negligible: an hourly sweep that finds nothing costs under 0.01 % of a core,
    # one that deletes 200 files under a second of CPU and no file reads
    check("cost: idle sweep under 360 ms CPU", float(idle[4]) < 360, f"{idle[4]} ms")
    check(
        "cost: deleting sweep under 1 s CPU, no file reads",
        float(deleting[4]) < 1000 and int(deleting[10]) == 0,
        f"{deleting[4]} ms, {deleting[10]} bytes read from disk",
    )
    check(
        "cost: uses.bin under 1 MB for 50000 entries",
        (cache / "uses.bin").stat().st_size < 1024 * 1024,
        f"{(cache / 'uses.bin').stat().st_size // 1024} KB",
    )


def main():
    global program
    root = Path(
        os.environ.get("IMP_RETENTION_DIR") or tempfile.mkdtemp(prefix="imp-retention-")
    )
    root.mkdir(parents=True, exist_ok=True)
    build = root / "harness"
    build.mkdir(exist_ok=True)
    (build / "RetentionCheck.csproj").write_text(PROJECT)
    (build / "Program.cs").write_text(PROGRAM)
    done = subprocess.run(
        ["dotnet", "build", str(build), "-nologo", "-v", "q", "-o", str(build / "out")],
        capture_output=True,
        text=True,
    )
    if done.returncode != 0:
        print(done.stdout, done.stderr)
        print("FAILED: build")
        sys.exit(1)
    program = build / "out" / "RetentionCheck"
    try:
        retention(root)
        if os.environ.get("IMP_QUICK") != "1":
            thread(root)
        cost(root)
    finally:
        if not os.environ.get("IMP_RETENTION_DIR"):
            shutil.rmtree(root, ignore_errors=True)
    print(f"{'PASSED' if failures == 0 else 'FAILED'}: {failures} failure(s)")
    sys.exit(failures)


if __name__ == "__main__":
    main()
