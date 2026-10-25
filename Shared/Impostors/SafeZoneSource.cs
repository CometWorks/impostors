using System;
using Impostors;
using Sandbox.Common.ObjectBuilders;
using Sandbox.Game.Entities;
using VRage.Game;
using VRageMath;

namespace Shared.Impostors;

// Enabled safe zones, each shown as its bubble: sphere or box, size and color. Disabled zones
// protect nothing, and the game draws them white with a different texture, so they are left out.
public sealed class SafeZoneSource : ImpostorSource
{
    public override ImpostorKind Kind => ImpostorKind.SafeZone;

    public override void Scan(ImpostorRegistry registry)
    {
        foreach (var zone in MySessionComponentSafeZones.SafeZones)
        {
            if (zone.Closed || zone.MarkedForClose || !zone.InScene || !zone.Enabled)
                continue;

            var entry = registry.Touch(zone.EntityId, this);
            entry.Tag = zone;
            entry.WorldMatrix = zone.WorldMatrix;
            entry.Velocity = Vector3.Zero;
            entry.WorldAabb = zone.PositionComp.WorldAABB;

            var shape = Shape(zone);
            long signature = 17;
            unchecked
            {
                signature = signature * 31 + (shape.Box ? 1 : 0);
                signature = signature * 31 + shape.HalfExtents.GetHashCode();
                signature = signature * 31 + shape.Color.PackedValue;
            }
            if (entry.Signature != signature)
            {
                entry.Signature = signature;
                entry.Stale = true;
            }
        }
    }

    public override Func<ShapeBuild, byte[][]> PrepareBuild(ImpostorEntry entry)
    {
        if (entry.Tag is not MySafeZone zone || zone.Closed)
            return null;

        var writer = new ByteWriter(ZoneShape.Size);
        Shape(zone).Write(writer);
        var shapes = new[] { writer.ToArray() };

        // The shape is its own input, reusing it keeps the build log for real work
        var input = InputHash.Start((byte)ImpostorKind.SafeZone);
        input.Add(shapes[0], shapes[0].Length);
        return build => build.Reuse(input) ?? shapes;
    }

    // For the build time log
    public override string Describe(ImpostorEntry entry)
    {
        if (entry.Tag is not MySafeZone zone)
            return "";
        var size = zone.Size;
        return zone.Shape == MySafeZoneShape.Box
            ? $"zone=box-{size.X:0}x{size.Y:0}x{size.Z:0}"
            : $"zone=sphere-{zone.Radius:0}";
    }

    private static ZoneShape Shape(MySafeZone zone)
    {
        var box = zone.Shape == MySafeZoneShape.Box;
        return new ZoneShape
        {
            Box = box,
            HalfExtents = box ? zone.Size / 2 : new Vector3(zone.Radius),
            Color = zone.ModelColor,
        };
    }

    // The zone of a safe zone block moves with the block's grid between cluster nodes, admin
    // zones are on every node
    public static long BlockGridId(ImpostorEntry entry)
    {
        if (entry.Tag is not MySafeZone zone || zone.SafeZoneBlockId == 0)
            return 0;
        return MyEntities.GetEntityById(zone.SafeZoneBlockId) is MyCubeBlock block
            ? block.CubeGrid.EntityId
            : 0;
    }
}

// The space a safe zone covers, for telling which impostors are seen through one
public readonly struct ZoneVolume
{
    private readonly MatrixD toLocal;
    private readonly ZoneShape shape;

    public ZoneVolume(MatrixD worldMatrix, ZoneShape shape)
    {
        toLocal = MatrixD.Invert(worldMatrix);
        this.shape = shape;
    }

    // The zone of an entry with its shape, null for the others
    public static ZoneVolume? Of(ImpostorEntry entry)
    {
        if (entry.Kind != ImpostorKind.SafeZone || entry.Shapes == null)
            return null;
        var bytes = entry.Shapes[0];
        if (bytes.Length < ZoneShape.Size || bytes[0] != 0)
            return null;
        return new ZoneVolume(entry.WorldMatrix, ZoneShape.Read(new ByteReader(bytes, 1)));
    }

    // The zone's color, which tints the impostors inside it
    public Color Color => shape.Color;

    public bool Contains(Vector3D point)
    {
        var p = Vector3D.Transform(point, toLocal);
        var half = (Vector3D)shape.HalfExtents;
        if (!shape.Box)
            return p.LengthSquared() <= half.X * half.X;
        return Math.Abs(p.X) <= half.X && Math.Abs(p.Y) <= half.Y && Math.Abs(p.Z) <= half.Z;
    }

    // The segment from a to b passes through the zone or ends inside it
    public bool Hits(Vector3D a, Vector3D b)
    {
        var from = Vector3D.Transform(a, toLocal);
        var to = Vector3D.Transform(b, toLocal);
        var half = (Vector3D)shape.HalfExtents;

        if (!shape.Box)
        {
            var d = to - from;
            var t =
                d.LengthSquared() > 0
                    ? MathHelper.Clamp(-Vector3D.Dot(from, d) / d.LengthSquared(), 0, 1)
                    : 0;
            return (from + d * t).LengthSquared() <= half.X * half.X;
        }

        // Slabs: the part of the segment within each axis' bounds, the parts must overlap
        double enter = 0,
            exit = 1;
        for (var i = 0; i < 3; i++)
        {
            var start = from.GetDim(i);
            var delta = to.GetDim(i) - start;
            var extent = half.GetDim(i);
            if (Math.Abs(delta) < 1e-9)
            {
                if (Math.Abs(start) > extent)
                    return false;
                continue;
            }
            var t0 = (-extent - start) / delta;
            var t1 = (extent - start) / delta;
            enter = Math.Max(enter, Math.Min(t0, t1));
            exit = Math.Min(exit, Math.Max(t0, t1));
            if (enter > exit)
                return false;
        }
        return true;
    }
}
