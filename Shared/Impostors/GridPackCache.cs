using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Impostors;
using Shared.Logging;

namespace Shared.Impostors;

// The world's grid pack (see GridPack) in front of the other caches: a grid group whose content
// hash is in the pack takes its shapes. Nothing from the pack is written to the disk cache.
public sealed class GridPackCache : IPackedShapeCache
{
    private readonly GridPack pack;
    private readonly IShapeCache inner;

    public GridPackCache(GridPack pack, IShapeCache inner)
    {
        this.pack = pack;
        this.inner = inner;
    }

    public byte[][] Get(long id, long input) => Get(id, input, out _);

    public byte[][] Get(long id, long input, out bool fromPack)
    {
        var packed = pack.Get(input);
        fromPack = packed != null;
        if (packed != null)
            return ToLevels(packed);
        return inner is IPackedShapeCache other
            ? other.Get(id, input, out fromPack)
            : inner?.Get(id, input);
    }

    // Back into the places of ImpostorEntry.Lods
    private static byte[][] ToLevels(byte[][] packed)
    {
        var shapes = new byte[ImpostorEntry.Lods.Length][];
        foreach (var shape in packed)
        {
            var k = Array.IndexOf(ImpostorEntry.Lods, shape[0]);
            if (k >= 0)
                shapes[k] = shape;
        }
        return shapes;
    }

    public void Put(long id, ImpostorKind kind, long input, byte[][] shapes) =>
        inner?.Put(id, kind, input, shapes);

    public void Forget(long id) => inner?.Forget(id);

    // The pack of the session's mods, or null when there is none or it can't be read
    public static GridPack Open(IPluginLogger log)
    {
        try
        {
            var start = DateTime.UtcNow;
            var pack = GridPack.Open();
            if (pack != null)
                log.Info(
                    $"Grid pack of mod {pack.ModName} ({pack.ModId}), built for \"{pack.World}\": "
                        + $"{pack.Count} shapes, {pack.Bytes / 1024} KB, index read in {(DateTime.UtcNow - start).TotalMilliseconds:0} ms"
                );
            return pack;
        }
        catch (Exception e)
        {
            log.Error(e, "Grid pack not available");
            return null;
        }
    }

    // Writes the pack of these shapes, by the content hash they were built from. Returns the
    // file's size.
    public static long Write(
        string path,
        string world,
        IEnumerable<(long input, byte[][] shapes)> grids
    )
    {
        var list = grids
            .OrderBy(g => g.input)
            .Select(g =>
                (
                    g.input,
                    version: ImpostorEntry.Hash(g.shapes),
                    levels: g.shapes.Where(s => s != null && s.Length > 0).ToArray()
                )
            )
            .ToList();

        // The client finds a shape by its version: of two different shapes with the same one,
        // the second stays out of the pack and gets built
        var kept = new Dictionary<int, byte[][]>();
        list = list.Where(g =>
                !kept.TryGetValue(g.version, out var first)
                    ? (kept[g.version] = g.levels) != null
                    : first.Length == g.levels.Length
                        && first.Zip(g.levels, (a, b) => a.SequenceEqual(b)).All(same => same)
            )
            .ToList();

        var id = InputHash.Start(0);
        foreach (var (input, version, _) in list)
        {
            id.Add(input);
            id.Add(version);
        }

        var temporary = path + ".tmp";
        using (var writer = new BinaryWriter(File.Create(temporary)))
        {
            writer.Write(GridPack.Magic);
            writer.Write(GridPack.Format);
            writer.Write(world ?? "");
            writer.Write(id.Value);
            writer.Write(list.Count);
            foreach (var (input, version, levels) in list)
            {
                writer.Write(input);
                writer.Write(version);
                writer.Write((byte)levels.Length);
                foreach (var shape in levels)
                {
                    writer.Write(shape[0]);
                    writer.Write(shape.Length);
                }
            }
            foreach (var (_, _, levels) in list)
            foreach (var shape in levels)
                writer.Write(shape);
        }
        if (File.Exists(path))
            File.Delete(path);
        File.Move(temporary, path);
        return new FileInfo(path).Length;
    }
}
