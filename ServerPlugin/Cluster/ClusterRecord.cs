using System.Text;
using Impostors;
using Shared.Impostors;
using VRageMath;

namespace ServerPlugin.Cluster;

// What the cluster index knows of one impostor of a node: enough to select and place it
// on another node, the shapes are fetched separately by id and version
public struct ClusterRecord
{
    public long Id;
    public ImpostorKind Kind;
    public int Version;
    public Vector3D Position;
    public Quaternion Orientation;
    public Vector3 Velocity;
    public Vector3D AabbCenter;
    public Vector3 AabbHalfExtents;

    public static ClusterRecord From(ImpostorEntry entry) =>
        new()
        {
            Id = entry.Id,
            Kind = entry.Kind,
            Version = entry.Version,
            Position = entry.WorldMatrix.Translation,
            Orientation = Quaternion.CreateFromRotationMatrix(entry.WorldMatrix),
            Velocity = entry.Velocity,
            AabbCenter = entry.WorldAabb.Center,
            AabbHalfExtents = entry.WorldAabb.HalfExtents,
        };

    public MatrixD WorldMatrix
    {
        get
        {
            var matrix = MatrixD.CreateFromQuaternion(Orientation);
            matrix.Translation = Position;
            return matrix;
        }
    }

    public BoundingBoxD WorldAabb =>
        new(AabbCenter - AabbHalfExtents, AabbCenter + AabbHalfExtents);

    // Different enough to report: a new version, or moved, turned or accelerated noticeably
    public bool Differs(ClusterRecord other) =>
        Version != other.Version
        || Kind != other.Kind
        || Vector3D.DistanceSquared(Position, other.Position) > 1
        || Vector3D.DistanceSquared(AabbCenter, other.AabbCenter) > 1
        || Vector3.DistanceSquared(AabbHalfExtents, other.AabbHalfExtents) > 1
        || System.Math.Abs(Quaternion.Dot(Orientation, other.Orientation)) < 0.99995f
        || Vector3.DistanceSquared(Velocity, other.Velocity) > 1;

    public void Write(ByteWriter w)
    {
        w.Long(Id);
        w.Byte((byte)Kind);
        w.Int(Version);
        w.Double(Position.X);
        w.Double(Position.Y);
        w.Double(Position.Z);
        w.Float(Orientation.X);
        w.Float(Orientation.Y);
        w.Float(Orientation.Z);
        w.Float(Orientation.W);
        w.Float(Velocity.X);
        w.Float(Velocity.Y);
        w.Float(Velocity.Z);
        w.Double(AabbCenter.X);
        w.Double(AabbCenter.Y);
        w.Double(AabbCenter.Z);
        w.Float(AabbHalfExtents.X);
        w.Float(AabbHalfExtents.Y);
        w.Float(AabbHalfExtents.Z);
    }

    public static ClusterRecord Read(ByteReader r) =>
        new()
        {
            Id = r.Long(),
            Kind = (ImpostorKind)r.Byte(),
            Version = r.Int(),
            Position = new Vector3D(r.Double(), r.Double(), r.Double()),
            Orientation = new Quaternion(r.Float(), r.Float(), r.Float(), r.Float()),
            Velocity = new Vector3(r.Float(), r.Float(), r.Float()),
            AabbCenter = new Vector3D(r.Double(), r.Double(), r.Double()),
            AabbHalfExtents = new Vector3(r.Float(), r.Float(), r.Float()),
        };

    public const int Size = 8 + 1 + 4 + 24 + 16 + 12 + 24 + 12;
}

public static class ByteExtensions
{
    public static void String(this ByteWriter w, string value)
    {
        var bytes = Encoding.UTF8.GetBytes(value ?? "");
        w.UShort((ushort)bytes.Length);
        w.Bytes(bytes);
    }

    public static string String(this ByteReader r) => Encoding.UTF8.GetString(r.Bytes(r.UShort()));
}
