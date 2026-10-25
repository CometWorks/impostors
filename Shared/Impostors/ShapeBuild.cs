using System.Runtime.InteropServices;
using VRageMath;

namespace Shared.Impostors;

// One build of an entry's shapes, on a background thread. The source hashes the content it
// builds from and calls Reuse with it before the expensive part: the same content gives the
// shapes of the entry's last build, or of an earlier one from the shape cache.
public sealed class ShapeBuild
{
    private readonly long id;
    private readonly long lastInput;
    private readonly byte[][] lastShapes;
    private readonly IShapeCache cache;

    // The hash of the content, 0 when the source didn't call Reuse (not cached)
    public long Input;

    // The shapes are the entry's last ones, or from the cache
    public bool Unchanged;
    public bool Cached;

    public ShapeBuild(ImpostorEntry entry, IShapeCache cache)
    {
        id = entry.Id;
        lastInput = entry.Input;
        lastShapes = entry.Shapes;
        this.cache = cache;
    }

    // The shapes built from this content before, or null to build them
    public byte[][] Reuse(InputHash input) => Reuse(input.Value);

    // The same for a content hash known from before, see VoxelSource
    public byte[][] Reuse(long input)
    {
        Input = input == 0 ? 1 : input;
        if (Input == lastInput && lastShapes != null)
        {
            Unchanged = true;
            return lastShapes;
        }

        var shapes = cache?.Get(id, Input);
        Cached = shapes != null;
        return shapes;
    }
}

// FNV-1a over the values a build depends on, seeded with the cache's algorithm version
public struct InputHash
{
    public long Value;

    public static InputHash Start(byte kind)
    {
        var hash = new InputHash { Value = unchecked((long)14695981039346656037UL) };
        hash.Add(DiskShapeCache.Algorithm);
        hash.Add(kind);
        return hash;
    }

    public void Add(byte value)
    {
        unchecked
        {
            Value = (Value ^ value) * 1099511628211L;
        }
    }

    public void Add(int value)
    {
        Add((byte)value);
        Add((byte)(value >> 8));
        Add((byte)(value >> 16));
        Add((byte)(value >> 24));
    }

    public void Add(long value)
    {
        Add((int)value);
        Add((int)(value >> 32));
    }

    public void Add(float value) => Add(new FloatBits { Float = value }.Int);

    public void Add(byte[] values, int count)
    {
        for (var i = 0; i < count; i++)
            Add(values[i]);
    }

    public void Add(bool value) => Add(value ? (byte)1 : (byte)0);

    public void Add(Vector3 value)
    {
        Add(value.X);
        Add(value.Y);
        Add(value.Z);
    }

    public void Add(Vector3I value)
    {
        Add(value.X);
        Add(value.Y);
        Add(value.Z);
    }
}

[StructLayout(LayoutKind.Explicit)]
internal struct FloatBits
{
    [FieldOffset(0)]
    public float Float;

    [FieldOffset(0)]
    public int Int;
}
