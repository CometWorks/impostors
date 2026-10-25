using System;
using VRageMath;

namespace Impostors
{
    // A decoded impostor shape: quads in the impostor's local frame
    public class ClientShape
    {
        public int Version;
        public byte Lod;
        public float CellSize;
        public int Count;

        // A safe zone's bubble instead of quads, a shape of level 0
        public bool IsZone;
        public ZoneShape Zone;

        // Per quad: center in local meters, face, half extents along the face's U and V axes,
        // color and the normal to shade it with
        public Vector3[] Centers;
        public byte[] Faces;
        public Vector2[] HalfSizes;
        public Vector3[] Colors;
        public Vector3[] Shading;

        // Quads per face, the camera sees the faces whose normals point towards it
        public readonly int[] FaceCounts = new int[6];

        // Quad indices sorted back to front for the local view direction SortedFor
        public int[] Order;
        public Vector3 SortedFor;

        public static readonly Vector3[] Normals =
        {
            Vector3.Right,
            Vector3.Left,
            Vector3.Up,
            Vector3.Down,
            Vector3.Backward,
            Vector3.Forward,
        };

        public static readonly Vector3[] Axis = { Vector3.Right, Vector3.Up, Vector3.Backward };

        public static ClientShape Read(ByteReader r, int version)
        {
            var shape = new ClientShape();
            shape.Version = version;
            shape.Lod = r.Byte();
            if (shape.Lod == 0)
            {
                shape.IsZone = true;
                shape.Zone = ZoneShape.Read(r);
                shape.Count = shape.Zone.Box ? 6 : ZoneMesh.MaxQuads;
                return shape;
            }

            shape.CellSize = r.Float();
            var origin = new Vector3(r.Float(), r.Float(), r.Float());
            var count = r.UShort();
            shape.Count = count;
            shape.Centers = new Vector3[count];
            shape.Faces = new byte[count];
            shape.HalfSizes = new Vector2[count];
            shape.Colors = new Vector3[count];
            shape.Shading = new Vector3[count];

            var size = shape.CellSize;
            shape.Order = new int[count];

            for (var i = 0; i < count; i++)
            {
                var face = r.Byte();
                var cell = new Vector3(r.Byte(), r.Byte(), r.Byte());
                var width = r.Byte();
                var height = r.Byte();
                var color = r.UShort();

                int n,
                    u,
                    v;
                Shape.Axes(face, out n, out u, out v);

                // Center of the quad in cell units: the face plane, then halfway along U and V
                var center = cell + Axis[u] * (width * 0.5f) + Axis[v] * (height * 0.5f);
                if ((face & 1) == 0)
                    center += Axis[n];

                shape.Centers[i] = origin + center * size;
                shape.Faces[i] = face;
                shape.FaceCounts[face]++;
                shape.HalfSizes[i] = new Vector2(width * 0.5f * size, height * 0.5f * size);
                shape.Colors[i] = Shape.FromRgb565(color);
                shape.Shading[i] = Shape.DecodeNormal(r.UShort());
                shape.Order[i] = i;
            }

            return shape;
        }

        // Sorts the quads farthest first along the local view direction, so drawing them in
        // order hides the far ones without a depth buffer (billboards don't write depth)
        public void Sort(Vector3 viewDirection)
        {
            SortedFor = viewDirection;
            var keys = new float[Count];
            for (var i = 0; i < Count; i++)
                keys[i] = -Vector3.Dot(Centers[Order[i]], viewDirection);
            Array.Sort(keys, Order);
        }
    }

    public class ClientImpostor
    {
        public ImpostorState State;
        public int ReceivedTick;
        public ClientShape Shape;
        public int ShapeTick;

        // The size of the shape as the server encoded it, what it counts in the ShapeCache
        public int ShapeBytes;
    }
}
