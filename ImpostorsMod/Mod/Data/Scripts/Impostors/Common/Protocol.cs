using System;
using VRageMath;

// Compiled into the mod, the server plugin and the client plugin, so it is C# 6 and uses only
// what the mod whitelist allows.
namespace Impostors
{
    // Mod networking between the server plugin and the Impostors mod on the clients.
    // Every message starts with its type byte. Multi-byte values are little endian.
    public static class Protocol
    {
        public const ushort Channel = 47213;
        public const byte Version = 4;

        // Client to server: byte version. Sent when the session is ready and then every
        // HelloInterval seconds, so a server that starts later or a cluster node the client
        // was moved to learns that this client draws impostors.
        public const byte Hello = 1;

        // Server to client: byte version, long server instance id, int most impostor quads the
        // client draws per frame (MaxQuadsPerFrame). A new instance id means a different
        // server (process or cluster node): the client drops every impostor it has.
        public const byte Welcome = 2;

        // Server to client: ushort count, then count states (see ImpostorState).
        public const byte States = 3;

        // Server to client: ushort count, then count long ids of impostors to drop.
        public const byte Remove = 4;

        // Server to client: ushort count, then count times long id, int version and a shape
        // (see Shape). Small shapes share a message.
        public const byte ShapeData = 5;

        // Client to server: float vertical field of view with zoom (radians), ushort viewport
        // width and height, float forward x, y, z of the camera. Sent while the view is zoomed
        // (see Zoom), when it zoomed or turned noticeably, and once more after leaving the zoom.
        // The server sends finer levels for the impostors inside the view. Older servers
        // ignore it.
        public const byte View = 6;

        // Views at least this much larger than the server's default estimate count as zoomed
        public const double MinZoom = 1.5;

        public const int HelloInterval = 30;

        // The game draws at most this many billboards per frame, shared with every effect
        public const int BillboardLimit = 32768;

        // Reliable messages are split around this size, so a burst doesn't hold up other traffic
        public const int MaxMessageSize = 16 * 1024;

        // Pixels per radian near the screen center the server assumes: a 1080p screen with a
        // 70 degree vertical field of view
        public const double PixelsPerRadian = 540 / 0.7002075382097097;

        // How many times larger a view shows things than the server's default estimate
        public static double Zoom(double fov, double height)
        {
            return height / 2 / Math.Tan(fov / 2) / PixelsPerRadian;
        }
    }

    public enum ImpostorKind : byte
    {
        Grid = 1,
        Voxel = 2,
    }

    // Where an impostor is and how it moves. The shape is in the local frame given by
    // Position and Orientation.
    public struct ImpostorState
    {
        public const int Size = 8 + 1 + 4 + 1 + 4 + 24 + 16 + 12;

        public long Id;
        public ImpostorKind Kind;

        // Content version of the shape, changes whenever the shape does
        public int Version;

        // Level of detail of the shape the server sends for this state
        public byte Lod;

        // Bounding sphere radius around the local origin, meters
        public float Radius;

        public Vector3D Position;
        public Quaternion Orientation;
        public Vector3 Velocity;

        public void Write(ByteWriter w)
        {
            w.Long(Id);
            w.Byte((byte)Kind);
            w.Int(Version);
            w.Byte(Lod);
            w.Float(Radius);
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
        }

        public static ImpostorState Read(ByteReader r)
        {
            var s = new ImpostorState();
            s.Id = r.Long();
            s.Kind = (ImpostorKind)r.Byte();
            s.Version = r.Int();
            s.Lod = r.Byte();
            s.Radius = r.Float();
            s.Position = new Vector3D(r.Double(), r.Double(), r.Double());
            s.Orientation = new Quaternion(r.Float(), r.Float(), r.Float(), r.Float());
            s.Velocity = new Vector3(r.Float(), r.Float(), r.Float());
            return s;
        }
    }

    // The geometry of an impostor: axis aligned quads on the faces of a cell grid, each
    // with a flat color and the surface normal to shade it with. Encoding:
    //   byte lod (cells along the longest axis the shape was built for)
    //   float cellSize, meters
    //   float originX, originY, originZ: local position of the min corner of cell (0,0,0)
    //   ushort quad count
    //   quads, 10 bytes each: byte face, byte x, y, z, byte width, height, ushort RGB565,
    //   ushort normal (see EncodeNormal)
    // Faces are +X, -X, +Y, -Y, +Z, -Z (0 to 5). (x, y, z) is the cell holding the quad's
    // min corner. Width runs along axis U and height along axis V of the face (see Axes).
    // The normal is the face's own for grids, and the smoothed surface's for asteroids, whose
    // stair-stepped faces would otherwise shade as six flat directions.
    public static class Shape
    {
        public const int HeaderSize = 1 + 4 + 12 + 2;
        public const int QuadSize = 10;

        // Octahedral encoding, 6 bits per coordinate (0..62, so 31 is exactly 0, in the high and
        // low byte): within 4 degrees. Coarser steps show: with the sun's weight in the shading,
        // a 17 degree step between neighboring quads made a visible speckle. Finer ones cost
        // quads, fewer neighbors share a normal to merge with.
        public static ushort EncodeNormal(Vector3 n)
        {
            n /= Math.Abs(n.X) + Math.Abs(n.Y) + Math.Abs(n.Z);
            var u = n.X;
            var v = n.Y;
            if (n.Z < 0)
            {
                u = (1 - Math.Abs(n.Y)) * Sign(n.X);
                v = (1 - Math.Abs(n.X)) * Sign(n.Y);
            }
            var iu = (int)Math.Round((u * 0.5f + 0.5f) * 62);
            var iv = (int)Math.Round((v * 0.5f + 0.5f) * 62);
            return (ushort)((MathHelper.Clamp(iu, 0, 62) << 8) | MathHelper.Clamp(iv, 0, 62));
        }

        public static Vector3 DecodeNormal(ushort code)
        {
            var u = (code >> 8) / 62f * 2 - 1;
            var v = (code & 255) / 62f * 2 - 1;
            var n = new Vector3(u, v, 1 - Math.Abs(u) - Math.Abs(v));
            if (n.Z < 0)
            {
                n.X = (1 - Math.Abs(v)) * Sign(u);
                n.Y = (1 - Math.Abs(u)) * Sign(v);
            }
            return Vector3.Normalize(n);
        }

        private static float Sign(float x)
        {
            return x < 0 ? -1f : 1f;
        }

        // The face normal's axis, then the U and V axes of the quad rectangle
        public static void Axes(int face, out int normal, out int u, out int v)
        {
            normal = face >> 1;
            u = (normal + 1) % 3;
            v = (normal + 2) % 3;
        }

        public static ushort ToRgb565(int r, int g, int b)
        {
            return (ushort)(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
        }

        public static Vector3 FromRgb565(ushort c)
        {
            return new Vector3(((c >> 11) & 31) / 31f, ((c >> 5) & 63) / 63f, (c & 31) / 31f);
        }
    }

    // Minimal binary writer, mods have no MemoryStream
    public class ByteWriter
    {
        private byte[] buffer;
        private int length;

        public ByteWriter(int capacity = 256)
        {
            buffer = new byte[capacity];
        }

        public int Length
        {
            get { return length; }
        }

        public void Clear()
        {
            length = 0;
        }

        public byte[] ToArray()
        {
            var result = new byte[length];
            Buffer.BlockCopy(buffer, 0, result, 0, length);
            return result;
        }

        private void Ensure(int size)
        {
            if (length + size <= buffer.Length)
                return;
            var bigger = new byte[Math.Max(buffer.Length * 2, length + size)];
            Buffer.BlockCopy(buffer, 0, bigger, 0, length);
            buffer = bigger;
        }

        public void Byte(byte value)
        {
            Ensure(1);
            buffer[length++] = value;
        }

        public void UShort(ushort value)
        {
            Ensure(2);
            buffer[length++] = (byte)value;
            buffer[length++] = (byte)(value >> 8);
        }

        public void Int(int value)
        {
            Ensure(4);
            buffer[length++] = (byte)value;
            buffer[length++] = (byte)(value >> 8);
            buffer[length++] = (byte)(value >> 16);
            buffer[length++] = (byte)(value >> 24);
        }

        public void Long(long value)
        {
            Int((int)value);
            Int((int)(value >> 32));
        }

        public void Float(float value)
        {
            Bytes(BitConverter.GetBytes(value));
        }

        public void Double(double value)
        {
            Long(BitConverter.DoubleToInt64Bits(value));
        }

        public void Bytes(byte[] value)
        {
            Ensure(value.Length);
            Buffer.BlockCopy(value, 0, buffer, length, value.Length);
            length += value.Length;
        }

        // Overwrites a ushort written earlier, used for counts known only at the end
        public void UShortAt(int position, ushort value)
        {
            buffer[position] = (byte)value;
            buffer[position + 1] = (byte)(value >> 8);
        }
    }

    public class ByteReader
    {
        private readonly byte[] buffer;
        private int position;

        public ByteReader(byte[] buffer, int position = 0)
        {
            this.buffer = buffer;
            this.position = position;
        }

        public int Remaining
        {
            get { return buffer.Length - position; }
        }

        public byte Byte()
        {
            return buffer[position++];
        }

        public ushort UShort()
        {
            var value = (ushort)(buffer[position] | (buffer[position + 1] << 8));
            position += 2;
            return value;
        }

        public int Int()
        {
            var value =
                buffer[position]
                | (buffer[position + 1] << 8)
                | (buffer[position + 2] << 16)
                | (buffer[position + 3] << 24);
            position += 4;
            return value;
        }

        public long Long()
        {
            var low = (uint)Int();
            var high = (long)Int();
            return (high << 32) | low;
        }

        public float Float()
        {
            var value = BitConverter.ToSingle(buffer, position);
            position += 4;
            return value;
        }

        public double Double()
        {
            return BitConverter.Int64BitsToDouble(Long());
        }

        public byte[] Bytes(int count)
        {
            var result = new byte[count];
            Buffer.BlockCopy(buffer, position, result, 0, count);
            position += count;
            return result;
        }
    }
}
