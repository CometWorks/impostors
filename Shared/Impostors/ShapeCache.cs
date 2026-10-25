using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Impostors;
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

// One file per entity id in a subfolder of the first 2 hex digits, the newest build only. Entries not used for MaxAge go,
// and the oldest used ones while the folder is over its size cap. Safe from any thread.
public sealed class DiskShapeCache : IShapeCache
{
    // Bump when a change to the shape building or encoding changes what the same content
    // builds into, so the shapes built by older versions are never used again
    public const int Algorithm = 1;

    private const int Magic = 0x31534D49; // "IMS1"
    private const string Extension = ".shapes";

    private static readonly Dictionary<string, DiskShapeCache> Open = new();

    private readonly string folder;
    private readonly IPluginLogger log;
    private readonly object sync = new();

    // Id to file size and last use, UTC
    private readonly Dictionary<long, (long bytes, DateTime used)> files = new();
    private long total;

    public long CapBytes;
    public TimeSpan MaxAge;

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
            var used = info.LastWriteTimeUtc;
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
            files[id] = (bytes, used);
            total += bytes;
        }
        if (moved > 0)
            log.Info($"Impostor cache: moved {moved} files into their subfolders");
    }

    // The cache of this folder, one instance per folder in the process
    public static DiskShapeCache Get(string folder, long capBytes, int maxDays, IPluginLogger log)
    {
        DiskShapeCache cache;
        lock (Open)
        {
            if (!Open.TryGetValue(folder, out cache))
                Open[folder] = cache = new DiskShapeCache(folder, log);
        }
        cache.CapBytes = capBytes;
        cache.MaxAge = TimeSpan.FromDays(maxDays);
        cache.Trim();
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
            if (!files.ContainsKey(id))
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

                var now = DateTime.UtcNow;
                if (now - files[id].used > TimeSpan.FromHours(1))
                {
                    File.SetLastWriteTimeUtc(path, now);
                    files[id] = (files[id].bytes, now);
                }
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
                files[id] = (bytes, DateTime.UtcNow);
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

            if (total > CapBytes)
                Trim();
        }
    }

    public void Forget(long id)
    {
        lock (sync)
            Delete(id);
    }

    // Drops the entries not used for MaxAge, then the oldest used ones until within the cap
    public void Trim()
    {
        lock (sync)
        {
            var cutoff = DateTime.UtcNow - MaxAge;
            var dropped = 0;
            foreach (var (id, file) in files.OrderBy(f => f.Value.used).ToList())
            {
                if (file.used >= cutoff && total <= CapBytes)
                    break;
                Delete(id);
                dropped++;
            }
            if (dropped > 0)
                log.Info(
                    $"Impostor cache: dropped {dropped} old entries, {files.Count} left, {total / 1024} KB"
                );
        }
    }

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
