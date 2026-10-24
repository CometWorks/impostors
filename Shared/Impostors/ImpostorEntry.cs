using System;
using Impostors;
using VRageMath;

namespace Shared.Impostors;

// One thing shown as an impostor: a grid group or a voxel map, keyed by an entity id
public sealed class ImpostorEntry
{
    // Levels of detail: cells along the longest axis of the shape
    public static readonly byte[] Lods = [2, 4, 8, 16, 32, 64];

    public readonly long Id;
    public readonly ImpostorSource Source;
    public ImpostorKind Kind;

    // Frame of the shape, the impostor's position and orientation
    public MatrixD WorldMatrix;
    public Vector3 Velocity;
    public BoundingBoxD WorldAabb;

    // Cheap summary of the content, a change marks the shapes stale
    public long Signature;
    public bool Stale = true;

    // Shapes by level of detail index, null until the first build; the highest levels may
    // be missing when a lower one already shows every detail
    public byte[][] Shapes;

    // Content hash of the shapes, 0 before the first build
    public int Version;

    // Some player gets this impostor, or the cluster may need it
    public bool Wanted;

    // Some player of this server gets this impostor
    public bool Viewed;
    public bool Building;
    public double LastBuild = double.NegativeInfinity;
    public int LastSeen;

    // Source specific data, for example the grids of a group
    public object Tag;

    public ImpostorEntry(long id, ImpostorSource source)
    {
        Id = id;
        Source = source;
        Kind = source.Kind;
    }

    public float Radius =>
        (float)(
            (WorldAabb.Center - WorldMatrix.Translation).Length() + WorldAabb.HalfExtents.Length()
        );

    // Index of the coarsest shape at or above the wanted level of detail, or of the finest one
    // built when none is that fine
    public int ShapeIndex(byte lod)
    {
        var best = 0;
        for (var i = 0; i < Lods.Length && i < Shapes.Length; i++)
        {
            if (Shapes[i] == null)
                break;
            best = i;
            if (Lods[i] >= lod)
                break;
        }
        return best;
    }

    public ImpostorState State(byte lod)
    {
        return new ImpostorState
        {
            Id = Id,
            Kind = Kind,
            Version = Version,
            Lod = lod,
            Radius = Radius,
            Position = WorldMatrix.Translation,
            Orientation = Quaternion.CreateFromRotationMatrix(WorldMatrix),
            Velocity = Velocity,
        };
    }

    public static int Hash(byte[][] shapes)
    {
        unchecked
        {
            var hash = (int)2166136261;
            foreach (var shape in shapes)
            {
                if (shape == null)
                    continue;
                foreach (var b in shape)
                    hash = (hash ^ b) * 16777619;
            }
            return hash == 0 ? 1 : hash;
        }
    }
}

// Finds the entries of one kind and snapshots their content for building shapes
public abstract class ImpostorSource
{
    public abstract ImpostorKind Kind { get; }

    // Entries of other servers (cluster nodes): never replicated to this server's players
    public virtual bool Remote => false;

    // Game thread: adds, updates and refreshes this source's entries through the registry
    public abstract void Scan(ImpostorRegistry registry);

    // Game thread: snapshots the entry's content and returns the work that builds its shapes
    // (one per level of detail) on a background thread, or null if it is gone
    public abstract Func<byte[][]> PrepareBuild(ImpostorEntry entry);

    // The entry's size for the build log, like "blocks=120"
    public virtual string Describe(ImpostorEntry entry) => "";
}
