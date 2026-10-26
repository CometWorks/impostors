using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using Impostors;
using Shared.Config;
using Shared.Logging;

namespace Shared.Impostors;

// Built shapes kept for later, keyed by entity id and the hash of the content they were built
// from (ShapeBuild.Input), so a restart or a refresh of unchanged content doesn't build them
// again
public interface IShapeCache
{
    // Background thread: the shapes built from this input, or null
    byte[][] Get(long id, long input);

    // Background thread: replaces what the cache had of this id
    void Put(long id, ImpostorKind kind, long input, byte[][] shapes);

    // Game thread: the entity is gone
    void Forget(long id);
}

// One file per entity id in a subfolder of the first 2 hex digits, the newest build only. Safe
// from any thread. A file's write time is when it was built. When it was used last stays in
// memory and goes into uses.bin from time to time, so a read writes nothing. Retention runs on
// a background thread of its own, see Sweep.
public sealed class DiskShapeCache : IShapeCache
{
    // Bump when a change to the shape building or encoding changes what the same content
    // builds into, so the shapes built by older versions are never used again
    public const int Algorithm = 1;

    // Most files a sweep deletes, with a pause after each so it never keeps the disk busy. A
    // sweep that had more to do runs again after Backlog instead of SweepEvery.
    public const int FilesPerSweep = 200;
    private static readonly TimeSpan Pause = TimeSpan.FromMilliseconds(5);
    private static readonly TimeSpan Backlog = TimeSpan.FromMinutes(1);

    // The first sweep, this long after the cache opened (give or take a quarter, as every
    // interval of the sweep)
    private static readonly TimeSpan FirstSweep = TimeSpan.FromMinutes(2);

    // uses.bin is saved after every sweep, and this often in between while it changes
    private static readonly TimeSpan SaveUsesEvery = TimeSpan.FromMinutes(10);

    private const int Magic = 0x31534D49; // "IMS1"
    private const int UsesMagic = 0x31554D49; // "IMU1"
    private const string Extension = ".shapes";
    private const string UsesFile = "uses.bin";

    private static readonly Dictionary<string, DiskShapeCache> Open = new();

    private readonly string folder;
    private readonly IPluginLogger log;
    private readonly object sync = new();
    private readonly Random random = new();
    private readonly AutoResetEvent wake = new(false);

    // Id to file size, when it was built and when it was used last, UTC
    private readonly Dictionary<long, (long bytes, DateTime built, DateTime used)> files = new();
    private long total;
    private bool usesChanged;
    private Thread sweeper;
    private DateTime due;

    // Retention: the size cap, how long after its last use and after its build an entry stays
    // (zero for no limit), and how often the sweep runs (zero turns it off)
    public long CapBytes;
    public TimeSpan MaxUnused;
    public TimeSpan MaxBuilt;
    public TimeSpan SweepEvery;

    public string Folder => folder;

    public long Bytes
    {
        get
        {
            lock (sync)
                return total;
        }
    }

    public int Count
    {
        get
        {
            lock (sync)
                return files.Count;
        }
    }

    private DiskShapeCache(string folder, IPluginLogger log)
    {
        this.folder = folder;
        this.log = log;
        Directory.CreateDirectory(folder);
        foreach (var path in Directory.GetFiles(folder, "*.tmp", SearchOption.AllDirectories))
            File.Delete(path); // An interrupted write
        var moved = 0;
        foreach (
            var path in Directory.GetFiles(folder, "*" + Extension, SearchOption.AllDirectories)
        )
        {
            if (!TryParseId(path, out var id))
                continue;
            var info = new FileInfo(path);
            var bytes = info.Length; // Read before a move, FileInfo loads it lazily
            var built = info.LastWriteTimeUtc;
            var expected = PathOf(id);
            if (path != expected)
            {
                // Flat layout of SE-700 or earlier, or a misplaced file
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(expected));
                    if (File.Exists(expected))
                    {
                        File.Delete(path);
                        continue;
                    }
                    File.Move(path, expected);
                    moved++;
                }
                catch (Exception e)
                {
                    log.Warning(e, $"Impostor cache: could not move {path} to {expected}");
                    continue;
                }
            }

            // Not used since the build, unless uses.bin says otherwise. Before SE-711 a read
            // touched the file, so there the write time is the last use.
            files[id] = (bytes, built, built);
            total += bytes;
        }
        if (moved > 0)
            log.Info($"Impostor cache: moved {moved} files into their subfolders");
        LoadUses();
    }

    // The cache of this folder, one instance per folder in the process, with the retention
    // settings of the config
    public static DiskShapeCache Get(string folder, IPluginConfig config, IPluginLogger log)
    {
        DiskShapeCache cache;
        lock (Open)
        {
            if (!Open.TryGetValue(folder, out cache))
                Open[folder] = cache = new DiskShapeCache(folder, log);
        }
        lock (cache.sync)
        {
            cache.CapBytes = config.CacheMegabytes * 1024L * 1024;
            cache.MaxUnused = TimeSpan.FromDays(Math.Max(0, config.CacheDays));
            cache.MaxBuilt = TimeSpan.FromDays(Math.Max(0, config.CacheBuildDays));
            cache.SweepEvery = TimeSpan.FromMinutes(Math.Max(0, config.CacheSweepMinutes));
            if (cache.SweepEvery > TimeSpan.Zero && cache.sweeper == null)
            {
                cache.due = DateTime.UtcNow + cache.Jitter(FirstSweep);
                cache.sweeper = new Thread(cache.Run)
                {
                    Name = "Impostors cache sweep",
                    IsBackground = true,
                    Priority = ThreadPriority.Lowest,
                };
                cache.sweeper.Start();
            }
        }
        return cache;
    }

    public byte[][] Get(long id, long input)
    {
        var entry = Read(id);
        return entry != null && entry.Value.input == input ? entry.Value.shapes : null;
    }

    // The newest build of this id: its input, version and shapes, or null
    public (long input, int version, byte[][] shapes)? Read(long id)
    {
        lock (sync)
        {
            if (!files.TryGetValue(id, out var file))
                return null;

            var path = PathOf(id);
            try
            {
                using var reader = new BinaryReader(File.OpenRead(path));
                if (reader.ReadInt32() != Magic || reader.ReadInt32() != Algorithm)
                    throw new InvalidDataException("old format");
                var input = reader.ReadInt64();
                var version = reader.ReadInt32();
                var shapes = new byte[reader.ReadByte()][];
                for (var i = 0; i < shapes.Length; i++)
                {
                    var length = reader.ReadInt32();
                    shapes[i] = length < 0 ? null : reader.ReadBytes(length);
                }

                files[id] = (file.bytes, file.built, DateTime.UtcNow);
                usesChanged = true;
                return (input, version, shapes);
            }
            catch (Exception e)
            {
                log.Debug($"Impostor cache: dropped {Path.GetFileName(path)}: {e.Message}");
                Delete(id);
                return null;
            }
        }
    }

    public void Put(long id, ImpostorKind kind, long input, byte[][] shapes) =>
        Put(id, input, ImpostorEntry.Hash(shapes), shapes);

    public void Put(long id, long input, int version, byte[][] shapes)
    {
        lock (sync)
        {
            var path = PathOf(id);
            var temporary = path + ".tmp";
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                using (var writer = new BinaryWriter(File.Create(temporary)))
                {
                    writer.Write(Magic);
                    writer.Write(Algorithm);
                    writer.Write(input);
                    writer.Write(version);
                    writer.Write((byte)shapes.Length);
                    foreach (var shape in shapes)
                    {
                        writer.Write(shape?.Length ?? -1);
                        if (shape != null)
                            writer.Write(shape);
                    }
                }

                Delete(id);
                File.Move(temporary, path);
                var bytes = new FileInfo(path).Length;
                var now = DateTime.UtcNow;
                files[id] = (bytes, now, now);
                total += bytes;
            }
            catch (Exception e)
            {
                log.Warning(e, $"Impostor cache: failed to write {path}");
                try
                {
                    File.Delete(temporary);
                }
                catch (Exception)
                {
                    // Gone already
                }
                return;
            }

            // Over the cap: the sweep comes early, not this build thread deleting files
            if (total > CapBytes && sweeper != null && due > DateTime.UtcNow + Backlog)
            {
                due = DateTime.UtcNow + Backlog;
                wake.Set();
            }
        }
    }

    public void Forget(long id)
    {
        lock (sync)
            Delete(id);
    }

    // Deletes up to limit files: those built longer than MaxBuilt ago or not used for
    // MaxUnused, then the least recently used ones while the rest is over the cap. Each file
    // takes the lock on its own and a pause follows it, so reads and writes go on in between.
    // How many it deleted.
    public int Sweep(int limit, TimeSpan pause)
    {
        var watch = Stopwatch.StartNew();
        var doomed = new List<(long id, DateTime built)>();
        lock (sync)
        {
            var now = DateTime.UtcNow;
            var unused = MaxUnused > TimeSpan.Zero ? now - MaxUnused : DateTime.MinValue;
            var old = MaxBuilt > TimeSpan.Zero ? now - MaxBuilt : DateTime.MinValue;
            var over = total - CapBytes;
            foreach (var pair in files)
            {
                if (doomed.Count == limit)
                    break;
                var file = pair.Value;
                if (file.used < unused || file.built < old)
                {
                    doomed.Add((pair.Key, file.built));
                    over -= file.bytes;
                }
            }
            if (over > 0 && doomed.Count < limit)
            {
                foreach (
                    var pair in files
                        .Where(f => f.Value.used >= unused && f.Value.built >= old)
                        .OrderBy(f => f.Value.used)
                )
                {
                    if (over <= 0 || doomed.Count == limit)
                        break;
                    doomed.Add((pair.Key, pair.Value.built));
                    over -= pair.Value.bytes;
                }
            }
        }
        var picked = watch.Elapsed.TotalMilliseconds;

        var deleted = 0;
        long bytes = 0;
        foreach (var (id, built) in doomed)
        {
            lock (sync)
            {
                // Rebuilt since, the new build stays
                if (!files.TryGetValue(id, out var file) || file.built != built)
                    continue;
                bytes += file.bytes;
                Delete(id);
                deleted++;
            }
            if (pause > TimeSpan.Zero)
                Thread.Sleep(pause);
        }
        if (deleted > 0)
            log.Info(
                $"Impostor cache: swept {deleted} files, {bytes / 1024} KB, {Count} left, {Bytes / 1024} KB; "
                    + $"{picked:0.0} ms to pick, {watch.ElapsedMilliseconds} ms in all"
            );
        return deleted;
    }

    // The last use of every entry used since its build, into uses.bin
    public void SaveUses()
    {
        var data = new MemoryStream();
        lock (sync)
        {
            if (!usesChanged)
                return;
            usesChanged = false;
            var writer = new BinaryWriter(data);
            var used = files.Where(f => f.Value.used > f.Value.built).ToList();
            writer.Write(UsesMagic);
            writer.Write(used.Count);
            foreach (var pair in used)
            {
                writer.Write(pair.Key);
                writer.Write(pair.Value.used.Ticks);
            }
        }
        var path = Path.Combine(folder, UsesFile);
        try
        {
            File.WriteAllBytes(path + ".tmp", data.ToArray());
            File.Delete(path);
            File.Move(path + ".tmp", path);
        }
        catch (Exception e)
        {
            log.Warning(e, $"Impostor cache: failed to write {path}");
        }
    }

    private void LoadUses()
    {
        var path = Path.Combine(folder, UsesFile);
        if (!File.Exists(path))
            return;
        try
        {
            using var reader = new BinaryReader(File.OpenRead(path));
            if (reader.ReadInt32() != UsesMagic)
                throw new InvalidDataException("unknown format");
            var count = reader.ReadInt32();
            for (var i = 0; i < count; i++)
            {
                var id = reader.ReadInt64();
                var used = new DateTime(reader.ReadInt64(), DateTimeKind.Utc);
                if (files.TryGetValue(id, out var file) && used > file.used)
                    files[id] = (file.bytes, file.built, used);
            }
        }
        catch (Exception e)
        {
            // The entries it could not read count as unused since their build
            log.Warning($"Impostor cache: dropped the rest of {path}: {e.Message}");
        }
    }

    // The sweep thread: a sweep when due, uses.bin in between
    private void Run()
    {
        var saved = DateTime.UtcNow;
        while (true)
        {
            TimeSpan wait;
            lock (sync)
                wait = due - DateTime.UtcNow;
            var save = SaveUsesEvery - (DateTime.UtcNow - saved);
            if (wait > TimeSpan.Zero && save > TimeSpan.Zero)
            {
                wake.WaitOne(wait < save ? wait : save);
                continue;
            }

            var more = false;
            try
            {
                if (wait <= TimeSpan.Zero)
                    more = Sweep(FilesPerSweep, Pause) == FilesPerSweep;
                SaveUses();
            }
            catch (Exception e)
            {
                log.Warning(e, "Impostor cache: sweep failed");
            }
            saved = DateTime.UtcNow;
            if (wait <= TimeSpan.Zero)
            {
                lock (sync)
                    due = DateTime.UtcNow + (more ? Backlog : Jitter(SweepEvery));
            }
        }
    }

    // The interval give or take a quarter, so caches started together don't sweep together
    private TimeSpan Jitter(TimeSpan interval) =>
        TimeSpan.FromTicks((long)(interval.Ticks * (0.75 + 0.5 * random.NextDouble())));

    private void Delete(long id)
    {
        if (!files.TryGetValue(id, out var file))
            return;
        files.Remove(id);
        total -= file.bytes;
        try
        {
            File.Delete(PathOf(id));
        }
        catch (Exception e)
        {
            log.Debug($"Impostor cache: failed to delete {id}: {e.Message}");
        }
    }

    // A subfolder per first 2 hex digits keeps the folders small: 01/012baf749a4ba168.shapes
    private string PathOf(long id)
    {
        var name = id.ToString("x16");
        return Path.Combine(folder, name.Substring(0, 2), name + Extension);
    }

    private static bool TryParseId(string path, out long id) =>
        long.TryParse(
            Path.GetFileNameWithoutExtension(path),
            System.Globalization.NumberStyles.HexNumber,
            null,
            out id
        );
}
