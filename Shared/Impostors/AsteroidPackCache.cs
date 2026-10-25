using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Impostors;
using Shared.Logging;

namespace Shared.Impostors;

// The world's asteroid pack (see AsteroidPack) in front of the shape cache: an asteroid whose
// content hash matches its pack entry takes the pack's shapes, the rest go to the cache as
// before. Nothing from the pack is written to the cache, the pack is there on every start.
public sealed class AsteroidPackCache : IShapeCache
{
    private readonly AsteroidPack pack;
    private readonly IShapeCache inner;

    public AsteroidPackCache(AsteroidPack pack, IShapeCache inner)
    {
        this.pack = pack;
        this.inner = inner;
    }

    public byte[][] Get(long id, long input) => Get(id, input, out _);

    // The same, telling whether the shapes came from the pack
    public byte[][] Get(long id, long input, out bool fromPack)
    {
        var packed = pack.Get(id, input);
        fromPack = packed != null;
        if (packed == null)
            return inner?.Get(id, input);

        // Back into the places of ImpostorEntry.Lods
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
    public static AsteroidPack Open(ulong modId, IPluginLogger log)
    {
        try
        {
            var start = DateTime.UtcNow;
            var pack = AsteroidPack.Open(modId);
            if (pack != null)
                log.Info(
                    $"Asteroid pack of mod {pack.ModName} ({pack.ModId}), built for \"{pack.World}\": "
                        + $"{pack.Count} asteroids, {pack.Bytes / 1024} KB, index read in {(DateTime.UtcNow - start).TotalMilliseconds:0} ms"
                );
            else if (modId != 0)
                log.Warning($"No asteroid pack in the world's mod {modId}");
            return pack;
        }
        catch (Exception e)
        {
            log.Error(e, "Asteroid pack not available");
            return null;
        }
    }

    // Writes the pack of these asteroids: (id, content hash, shapes in the places of
    // ImpostorEntry.Lods, whether nobody edited it). Levels finer than maxLod are left out,
    // those entries then serve only clients. Returns the file's size.
    public static long Write(
        string path,
        string world,
        long settings,
        IEnumerable<(long id, long input, byte[][] shapes, bool original)> asteroids,
        byte maxLod
    )
    {
        var list = asteroids.OrderBy(a => a.id).ToList();
        var temporary = path + ".tmp";
        using (var writer = new BinaryWriter(File.Create(temporary)))
        {
            writer.Write(AsteroidPack.Magic);
            writer.Write(AsteroidPack.Format);
            writer.Write(world ?? "");
            writer.Write(settings);
            writer.Write(list.Count);
            var kept = new List<byte[][]>();
            foreach (var (id, input, shapes, original) in list)
            {
                var levels = shapes.Where(s => s != null && s.Length > 0).ToArray();
                var packed = levels.Where(s => s[0] <= maxLod).ToArray();
                kept.Add(packed);
                writer.Write(id);
                writer.Write(input);
                writer.Write(ImpostorEntry.Hash(shapes));
                writer.Write((byte)((packed.Length == levels.Length ? 1 : 0) | (original ? 2 : 0)));
                writer.Write((byte)packed.Length);
                foreach (var shape in packed)
                {
                    writer.Write(shape[0]);
                    writer.Write(shape.Length);
                }
            }
            foreach (var packed in kept)
            foreach (var shape in packed)
                writer.Write(shape);
        }
        if (File.Exists(path))
            File.Delete(path);
        File.Move(temporary, path);
        return new FileInfo(path).Length;
    }
}
